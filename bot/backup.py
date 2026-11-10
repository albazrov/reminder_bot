"""Create and validate private, restorable application backup snapshots."""
from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import logging
import os
import secrets
import shutil
import sqlite3
import stat
import tempfile
import re
from contextlib import closing, contextmanager
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any, Awaitable, Callable, Coroutine, TypeVar
from zoneinfo import ZoneInfo

from .config import service_registry_path
from .excel_parser import parse_people, parse_schedule
from .secure_fs import open_private_directory


BACKUP_MOUNT = Path("/mnt/external_data")
BACKUP_BASE = BACKUP_MOUNT / "reminder_bot" / "backups"
SNAPSHOT_FORMAT_VERSION = 1
BOT_PID_FILENAME = "reminder_bot_bot.pid"
INSTANCE_LOCK_FILENAME = "reminder_bot_instance.lock"
BACKUP_LOCK_FILENAME = ".backup.lock"
MAX_SNAPSHOTS_PER_ENVIRONMENT = 14
MIN_RETAINED_SNAPSHOTS = 7
STALE_SNAPSHOT_STAGE_AGE = timedelta(hours=24)
SCHEDULE_RETRY_SECONDS = 300
SCHEDULE_REFRESH_SECONDS = 1800
STORAGE_WARNING_PERCENT = 90
STORAGE_CRITICAL_PERCENT = 95
STORAGE_MIN_FREE_BYTES = 256 * 1024 * 1024
_T = TypeVar("_T")


@contextmanager
def application_instance_lock(shm_dir: Path):
    directory_fd = open_private_directory(shm_dir, create=True)
    lock_fd = -1
    try:
        lock_fd = os.open(
            INSTANCE_LOCK_FILENAME,
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
            0o600,
            dir_fd=directory_fd,
        )
        info = os.fstat(lock_fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) & 0o077
        ):
            raise PermissionError("Файл блокировки экземпляра небезопасен")
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(
                "Экземпляр бота или операция восстановления уже выполняется"
            ) from exc
        yield
    finally:
        if lock_fd >= 0:
            os.close(lock_fd)
        os.close(directory_fd)


def assert_bot_stopped(project_dir: Path, shm_dir: Path) -> None:
    """Refuse restoration when the managed bot or a legacy bot is running."""
    pid_path = shm_dir / BOT_PID_FILENAME
    if pid_path.exists() or pid_path.is_symlink():
        if not _is_regular_private_file(pid_path):
            raise RuntimeError(f"PID-файл бота небезопасен: {pid_path}")
        try:
            pid_contents = pid_path.read_text("ascii")
        except (OSError, UnicodeError) as exc:
            raise RuntimeError(f"Не удалось безопасно прочитать PID-файл: {pid_path}") from exc
        match = re.fullmatch(r"([1-9][0-9]*):([0-9]+)", pid_contents)
        if match is None:
            raise RuntimeError(f"PID-файл бота имеет неверный формат: {pid_path}")
        pid, starttime = match.groups()
        proc_stat = Path("/proc") / pid / "stat"
        try:
            actual_starttime = proc_stat.read_text("ascii").rsplit(")", 1)[1].split()[19]
        except FileNotFoundError:
            actual_starttime = None
        except (OSError, UnicodeError, IndexError) as exc:
            raise RuntimeError(
                f"Не удалось проверить процесс из PID-файла (PID {pid})"
            ) from exc
        if actual_starttime == starttime:
            raise RuntimeError(
                f"Бот ещё работает (PID {pid}); остановите его перед восстановлением"
            )

    proc_root = Path("/proc")
    try:
        entries = tuple(proc_root.iterdir())
    except OSError as exc:
        raise RuntimeError("Не удалось проверить запущенные процессы бота") from exc
    expected_script = (project_dir / "reminder_bot.py").resolve()
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            executable = (entry / "exe").resolve().name
            if not executable.startswith("python"):
                continue
            argv = (entry / "cmdline").read_bytes().decode(
                "utf-8", errors="replace"
            ).split("\0")
            argv = [value for value in argv if value]
            cwd = (entry / "cwd").resolve()
        except (OSError, PermissionError):
            continue
        module_bot = any(
            argv[index:index + 2] == ["-m", "bot.reminder_bot"]
            for index in range(max(0, len(argv) - 1))
        )
        script_bot = False
        for value in argv[1:]:
            candidate = Path(value)
            if candidate.name != "reminder_bot.py":
                continue
            if not candidate.is_absolute():
                candidate = cwd / candidate
            try:
                script_bot = candidate.resolve() == expected_script
            except OSError:
                script_bot = False
            if script_bot:
                break
        if (module_bot and cwd == project_dir.resolve()) or script_bot:
            raise RuntimeError(
                f"Обнаружен работающий процесс бота (PID {entry.name}); "
                "остановите его перед восстановлением"
            )


def _is_regular_private_file(path: Path) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    return (
        stat.S_ISREG(info.st_mode)
        and info.st_uid == os.geteuid()
        and info.st_nlink == 1
        and (stat.S_IMODE(info.st_mode) & 0o077) == 0
    )


def _is_owned_regular_file(path: Path) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    return (
        stat.S_ISREG(info.st_mode)
        and info.st_uid == os.geteuid()
        and info.st_nlink == 1
    )


def _secure_directory(path: Path) -> None:
    if ".." in path.parts:
        raise ValueError(f"Путь не должен содержать '..': {path}")
    current = Path(path.anchor) if path.is_absolute() else Path(".")
    components = path.parts[1:] if path.is_absolute() else path.parts
    for component in components:
        current = current / component
        try:
            info = current.lstat()
        except FileNotFoundError:
            try:
                current.mkdir(mode=0o700)
            except FileExistsError:
                pass
            info = current.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise PermissionError(f"Небезопасный каталог резервирования: {current}")
    info = path.stat()
    if info.st_uid != os.geteuid():
        raise PermissionError(f"Каталог должен принадлежать текущему пользователю: {path}")
    path.chmod(0o700)


def _write_private(path: Path, data: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            fd = -1
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        if fd >= 0:
            os.close(fd)


def _fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def environment_name(project_dir: Path) -> str:
    configured = os.environ.get("REMINDER_BOT_ENV", project_dir.name)
    if configured not in {"test", "prod"}:
        raise RuntimeError(
            "Окружение резервного копирования должно быть 'test' или 'prod' "
            "(REMINDER_BOT_ENV)"
        )
    return configured


class BackupManager:
    def __init__(
        self,
        environment: str,
        shm_dir: Path,
        timezone: ZoneInfo,
        logger: logging.Logger,
        *,
        backup_base: Path = BACKUP_BASE,
        mount_point: Path = BACKUP_MOUNT,
        require_mount: bool = True,
        debounce_seconds: int = 600,
        registry_path: Path | None = None,
    ) -> None:
        if environment not in {"test", "prod"}:
            raise ValueError("Окружение резервирования должно быть test или prod")
        self.environment = environment
        self.shm_dir = shm_dir
        self.timezone = timezone
        self.logger = logger
        self.backup_base = backup_base
        self.mount_point = mount_point
        self.require_mount = require_mount
        self.debounce_seconds = debounce_seconds
        self.registry_path = registry_path or service_registry_path()
        self._debounce_handle: asyncio.TimerHandle | None = None
        self._retry_armed = False
        self._backup_lock = asyncio.Lock()
        self._tasks: set[asyncio.Task] = set()
        self._failure_callback: Callable[[str, set[int]], Awaitable[None]] | None = None
        self._snapshot_validation_cache: dict[Path, tuple[tuple, bool]] = {}
        self._failure_notices: set[str] = set()
        self._failure_recipients: dict[str, set[int]] = {}
        self._failure_notice_lock = asyncio.Lock()
        self._storage_levels: dict[Path, str] = {}
        self._schedule_copy_task: asyncio.Task | None = None
        self._pending_schedule_copy: tuple[int, Path] | None = None
        self._schedule_generation = 0
        self._published_schedule_generation = 0
        self._closing = False

    def set_failure_callback(
        self, callback: Callable[[str, set[int]], Awaitable[None]]
    ) -> None:
        self._failure_callback = callback

    async def report_failure(self, kind: str, message: str) -> None:
        self.logger.error("%s", message)
        async with self._failure_notice_lock:
            if kind in self._failure_notices or self._failure_callback is None:
                return
            delivered = self._failure_recipients.setdefault(kind, set())
            try:
                await self._failure_callback(message, delivered)
            except Exception:
                self.logger.exception("Не удалось уведомить администратора о бэкапе")
            else:
                self._failure_notices.add(kind)

    def clear_failure(self, kind: str) -> None:
        self._failure_notices.discard(kind)
        self._failure_recipients.pop(kind, None)

    async def _complete_io(self, action: Callable[[], _T]) -> _T:
        worker = asyncio.create_task(asyncio.to_thread(action))
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError:
            # Do not release the write lock or remove the input mid-write.
            try:
                await worker
            except Exception:
                self.logger.exception("Ошибка файловой операции при остановке бэкапа")
            raise

    @property
    def saved_schedule(self) -> Path:
        return self.backup_base / self.environment / "latest-schedule.xlsx"

    def _replace_schedule(self, path: Path, data: bytes) -> None:
        if path.is_symlink() or (
            path.exists() and not _is_regular_private_file(path)
        ):
            raise PermissionError(f"Файл расписания небезопасен: {path}")
        fd, name = tempfile.mkstemp(prefix=".schedule-", dir=path.parent)
        os.close(fd)
        stage = Path(name)
        try:
            stage.chmod(0o600)
            with stage.open("wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(stage, path)
            _fsync_directory(path.parent)
        finally:
            stage.unlink(missing_ok=True)

    async def record_schedule_download(self, path: Path) -> bool:
        """Publish verified bytes for backups only, never for delivery fallback."""
        self._schedule_generation += 1
        return await self._publish_schedule_download(path, self._schedule_generation)

    async def _publish_schedule_download(self, path: Path, generation: int) -> bool:
        async with self._backup_lock:
            if generation < self._published_schedule_generation:
                return False

            def publish() -> bool:
                changed = self._record_schedule_download(path)
                self._published_schedule_generation = generation
                return changed

            changed = await self._complete_io(publish)
        self.clear_failure("schedule-copy")
        if changed and self._debounce_handle is None:
            self.mark_dirty()
        return changed

    async def queue_schedule_download(self, source: Path) -> None:
        """Retain a RAM copy; external disk work never blocks reminder rendering."""
        self._schedule_generation += 1
        generation = self._schedule_generation
        fd, name = tempfile.mkstemp(
            prefix=".schedule-queued-", suffix=".xlsx", dir=self.shm_dir
        )
        os.close(fd)
        retained = Path(name)
        queued = False
        try:
            def retain_valid_download() -> None:
                if not _is_owned_regular_file(source):
                    raise PermissionError(f"Скачанное расписание небезопасно: {source}")
                shutil.copyfile(source, retained)
                self._parse_schedule(retained)

            await self._complete_io(retain_valid_download)
            if generation < self._published_schedule_generation:
                return
            if self._pending_schedule_copy is not None:
                previous_generation, previous_path = self._pending_schedule_copy
                if generation < previous_generation:
                    return
                previous_path.unlink(missing_ok=True)
            self._pending_schedule_copy = (generation, retained)
            queued = True
            if self._schedule_copy_task is None or self._schedule_copy_task.done():
                self._schedule_copy_task = self.start_task(
                    self._drain_schedule_copies(), name="reminder-bot-schedule-copy"
                )
        finally:
            if not queued:
                retained.unlink(missing_ok=True)

    async def _drain_schedule_copies(self) -> None:
        while self._pending_schedule_copy is not None:
            generation, path = self._pending_schedule_copy
            self._pending_schedule_copy = None
            try:
                await self._publish_schedule_download(path, generation)
            except Exception as exc:
                self.logger.exception("Не удалось сохранить постоянную копию Excel")
                await self.report_failure(
                    "schedule-copy",
                    "Не удалось сохранить постоянную копию Excel: "
                    f"{type(exc).__name__}: {exc}",
                )
                await self.check_storage()
            finally:
                path.unlink(missing_ok=True)

    async def run_storage_checks(self) -> None:
        while True:
            await self.check_storage()
            await asyncio.sleep(SCHEDULE_RETRY_SECONDS)

    def _record_schedule_download(self, path: Path) -> bool:
        self._require_external_mount()
        for directory in (
            self.backup_base.parent, self.backup_base,
            self.saved_schedule.parent, self.shm_dir,
        ):
            _secure_directory(directory)
        if not _is_owned_regular_file(path):
            raise PermissionError(f"Скачанное расписание небезопасно: {path}")
        data = path.read_bytes()
        # Validate the exact immutable bytes that will be published.
        fd, name = tempfile.mkstemp(
            prefix=".schedule-check-", suffix=".xlsx", dir=self.shm_dir
        )
        os.close(fd)
        stage = Path(name)
        try:
            stage.write_bytes(data)
            self._parse_schedule(stage)
            with self._locked_backup_directory() as backup_dir:
                self._prune_for_space(backup_dir)
                current = self.shm_dir / "reminder_bot_current.xlsx"
                changed = any(
                    not _is_regular_private_file(copy)
                    or copy.read_bytes() != data
                    for copy in (self.saved_schedule, current)
                )
                self._replace_schedule(self.saved_schedule, data)
                self._replace_schedule(current, data)
        finally:
            stage.unlink(missing_ok=True)
        self.logger.info("Проверенная копия Excel сохранена: %s", self.saved_schedule)
        return changed

    async def run_schedule_refresh(
        self,
        download: Callable[[Path], Awaitable[Any]],
    ) -> None:
        """Check every half hour; retry failures without blocking polling."""
        first_attempt = True
        needs_snapshot = True
        while True:
            path: Path | None = None
            try:
                directory_fd = open_private_directory(self.shm_dir, create=True)
                os.close(directory_fd)
                fd, name = tempfile.mkstemp(
                    prefix=".schedule-refresh-", suffix=".xlsx", dir=self.shm_dir
                )
                os.close(fd)
                path = Path(name)
                await download(path)
                needs_snapshot |= await self.record_schedule_download(path)
                if needs_snapshot:
                    await self.create_snapshot("обновление Excel")
                    needs_snapshot = False
            except Exception as exc:
                await self.report_failure(
                    "schedule",
                    "Не удалось обновить Excel и резервную копию: "
                    f"{type(exc).__name__}: {exc}. "
                    "Повтор через 5 минут; сохранённая копия не используется "
                    "для отправки напоминаний.",
                )
                if first_attempt:
                    await self._run_background_backup("сохранённый Excel после запуска")
                await self.check_storage()
                delay = SCHEDULE_RETRY_SECONDS
            else:
                self.clear_failure("schedule")
                delay = SCHEDULE_REFRESH_SECONDS
            finally:
                first_attempt = False
                if path is not None:
                    path.unlink(missing_ok=True)
            await asyncio.sleep(delay)

    def mark_dirty(self) -> None:
        """Restart the quiet-period timer after a persistent data change."""
        if self._closing:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self.logger.warning(
                "Изменение данных не поставлено в очередь резервирования: "
                "нет активного event loop"
            )
            return
        if self._debounce_handle is not None:
            self._debounce_handle.cancel()
        self._debounce_handle = loop.call_later(
            self.debounce_seconds,
            self._start_background_backup,
        )

    def start_background_backup(self, reason: str) -> asyncio.Task:
        return self.start_task(
            self._run_background_backup(reason),
            name=f"reminder-bot-backup-{reason}",
        )

    def start_task(
        self, coroutine: Coroutine[Any, Any, Any], *, name: str
    ) -> asyncio.Task:
        task = asyncio.create_task(coroutine, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def _start_background_backup(self) -> None:
        self._debounce_handle = None
        self.start_background_backup("изменение данных")

    async def close(self) -> None:
        self._closing = True
        if self._debounce_handle is not None:
            self._debounce_handle.cancel()
            self._debounce_handle = None
        if self._tasks:
            await asyncio.gather(*tuple(self._tasks), return_exceptions=True)

    async def create_snapshot(self, reason: str) -> Path:
        async with self._backup_lock:
            pending = self._debounce_handle
            if pending is not None:
                pending.cancel()
                self._debounce_handle = None
            try:
                snapshot = await self._complete_io(self._create_snapshot)
            except Exception:
                # Отменённый отложенный снимок повторяется один раз до
                # следующего успешного снимка, чтобы не спамить при отказе.
                if (
                    pending is not None
                    and self._debounce_handle is None
                    and not self._retry_armed
                ):
                    self._retry_armed = True
                    self.mark_dirty()
                raise
            self._retry_armed = False
            self.clear_failure("backup")
            self.logger.info("Резервная копия создана (%s): %s", reason, snapshot)
            await self.check_storage()
            return snapshot

    async def _run_background_backup(self, reason: str) -> None:
        # Отложенный таймер не сбрасывается здесь: create_snapshot отменяет
        # его под блокировкой, иначе он запустил бы второй снимок.
        try:
            await self.create_snapshot(reason)
        except Exception as exc:
            self.logger.exception("Не удалось создать резервную копию (%s)", reason)
            await self.report_failure(
                "backup",
                f"Не удалось создать резервную копию данных ({reason}): "
                f"{type(exc).__name__}: {exc}",
            )
            await self.check_storage()

    @staticmethod
    def _storage_level(usage) -> str:
        percent = (usage.total - usage.free) * 100 / usage.total
        if percent >= STORAGE_CRITICAL_PERCENT or usage.free < STORAGE_MIN_FREE_BYTES:
            return "critical"
        return "warning" if percent >= STORAGE_WARNING_PERCENT else "ok"

    async def check_storage(self) -> None:
        for path in (self.backup_base, self.registry_path.parent):
            try:
                usage = await asyncio.to_thread(shutil.disk_usage, path)
            except OSError as exc:
                await self.report_failure(
                    f"storage:{path}", f"Не удалось проверить место на {path}: {exc}"
                )
                continue
            self.clear_failure(f"storage:{path}")
            level = self._storage_level(usage)
            for other_level in ("warning", "critical"):
                if other_level != level:
                    self.clear_failure(f"space:{path}:{other_level}")
            if level == "ok":
                self._storage_levels.pop(path, None)
                continue
            if self._storage_levels.get(path) == level:
                continue
            percent = (usage.total - usage.free) * 100 / usage.total
            kind = f"space:{path}:{level}"
            await self.report_failure(
                kind,
                f"{'Критически мало места' if level == 'critical' else 'Мало места'} "
                f"на носителе {path}: занято {percent:.1f}%, "
                f"свободно {usage.free / (1024 * 1024):.0f} МиБ. "
                f"Минимум {MIN_RETAINED_SNAPSHOTS} проверенных копий сохраняется.",
            )
            if kind in self._failure_notices:
                self._storage_levels[path] = level

    async def run_nightly(self) -> None:
        while True:
            now = datetime.now(self.timezone)
            target = datetime.combine(now.date(), time(3, 0), self.timezone)
            if target <= now:
                target += timedelta(days=1)
            await asyncio.sleep((target - now).total_seconds())
            await self._run_background_backup("ежедневное ночное резервирование")

    def _create_snapshot(self) -> Path:
        with self._locked_backup_directory() as backup_dir:
            self._prune_for_space(backup_dir)
            return self._create_snapshot_locked(backup_dir)

    @contextmanager
    def _locked_backup_directory(self):
        self._require_external_mount()

        backup_dir = self.backup_base / self.environment
        _secure_directory(self.backup_base.parent)
        _secure_directory(self.backup_base)
        _secure_directory(backup_dir)
        _secure_directory(self.shm_dir)
        lock_fd = os.open(
            backup_dir / BACKUP_LOCK_FILENAME,
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
            0o600,
        )
        try:
            lock_info = os.fstat(lock_fd)
            if (
                not stat.S_ISREG(lock_info.st_mode)
                or lock_info.st_uid != os.geteuid()
                or lock_info.st_nlink != 1
                or stat.S_IMODE(lock_info.st_mode) & 0o077
            ):
                raise PermissionError("Файл блокировки резервной копии небезопасен")
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            self._require_external_mount()
            yield backup_dir
        finally:
            os.close(lock_fd)

    def _create_snapshot_locked(self, backup_dir: Path) -> Path:
        db_path = self.registry_path
        xlsx_path = self.shm_dir / "reminder_bot_current.xlsx"
        if not _is_regular_private_file(db_path):
            raise RuntimeError(f"SQLite-файл отсутствует или небезопасен: {db_path}")
        if not xlsx_path.exists() and not xlsx_path.is_symlink():
            xlsx_path = self.saved_schedule
            self.logger.warning(
                "Для бэкапа используется сохранённый Excel, не свежая загрузка: %s",
                xlsx_path,
            )
        if not _is_owned_regular_file(xlsx_path):
            raise RuntimeError(
                f"Исходное расписание отсутствует или небезопасно: {xlsx_path}"
            )

        timestamp = datetime.now(self.timezone).strftime("%Y-%m-%dT%H-%M-%S")
        stage = Path(tempfile.mkdtemp(prefix=".snapshot-", dir=backup_dir))
        stage.chmod(0o700)
        try:
            copied_db = stage / "database.sqlite3"
            with closing(
                sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
            ) as source, closing(sqlite3.connect(copied_db)) as target:
                os.chmod(copied_db, 0o600)
                source.backup(target)
            with closing(sqlite3.connect(copied_db)) as connection:
                check = connection.execute("PRAGMA integrity_check").fetchone()
                if not check or check[0] != "ok":
                    raise RuntimeError("SQLite integrity_check не пройден")
                sqlite_user_version = connection.execute(
                    "PRAGMA user_version"
                ).fetchone()[0]
                sql_dump = "\n".join(connection.iterdump()) + "\n"
            _write_private(stage / "database.sql", sql_dump.encode("utf-8"))

            cache_path = self.shm_dir / "reminder_state_cache.json"
            if cache_path.exists():
                if not _is_owned_regular_file(cache_path):
                    raise RuntimeError(f"Кэш состояния небезопасен: {cache_path}")
                cache_data = cache_path.read_bytes()
                json.loads(cache_data)
                _write_private(stage / "reminder_state_cache.json", cache_data)

            schedule_saved_at = xlsx_path.stat().st_mtime
            shutil.copyfile(xlsx_path, stage / "schedule.xlsx", follow_symlinks=False)
            os.chmod(stage / "schedule.xlsx", 0o600)
            staged_xlsx = stage / "schedule.xlsx"
            os.utime(staged_xlsx, (schedule_saved_at, schedule_saved_at))
            target_date, schedule_data, contacts = self._parse_schedule(staged_xlsx)
            schedule_json = {
                "format_version": SNAPSHOT_FORMAT_VERSION,
                "processed_at": datetime.now(self.timezone).isoformat(),
                "source_sha256": _sha256(staged_xlsx),
                "target_date": target_date.isoformat(),
                "processed_month": target_date.month,
                "processed_year": target_date.year,
                "assignments": schedule_data,
                "contacts": contacts,
            }
            _write_private(
                stage / "schedule.json",
                json.dumps(
                    schedule_json, ensure_ascii=False, indent=2
                ).encode("utf-8"),
            )

            files = {}
            for path in sorted(stage.iterdir()):
                files[path.name] = {
                    "size_bytes": path.stat().st_size,
                    "sha256": _sha256(path),
                }
            manifest = {
                "format_version": SNAPSHOT_FORMAT_VERSION,
                "environment": self.environment,
                "created_at": datetime.now(self.timezone).isoformat(),
                "schedule_saved_at": datetime.fromtimestamp(
                    schedule_saved_at, self.timezone
                ).isoformat(),
                "sqlite_user_version": sqlite_user_version,
                "files": files,
            }
            _write_private(
                stage / "manifest.json",
                json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"),
            )
            manifest = self._validate_snapshot(stage, self.environment)
            self._require_external_mount()

            final = backup_dir / timestamp
            if final.exists() or final.is_symlink():
                final = backup_dir / f"{timestamp}-{secrets.token_hex(3)}"
            if final.exists() or final.is_symlink():
                raise FileExistsError(f"Имя резервного снимка уже существует: {final}")
            os.replace(stage, final)
            directory_fd = os.open(backup_dir, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            self._prune_old_snapshots(backup_dir, final, manifest)
            return final
        except BaseException:
            shutil.rmtree(stage, ignore_errors=True)
            raise

    def _prune_old_snapshots(
        self,
        backup_dir: Path,
        committed_snapshot: Path,
        committed_manifest: dict | None = None,
    ) -> None:
        self._prune_abandoned_stages(backup_dir)
        snapshots = []
        try:
            candidates = tuple(backup_dir.iterdir())
        except OSError:
            self.logger.exception(
                "Новый снимок создан, но каталог не удалось прочитать "
                "для очистки старых копий: %s",
                backup_dir,
            )
            return
        for candidate in candidates:
            if candidate == committed_snapshot or candidate.name.startswith(
                ".snapshot-"
            ):
                continue
            if candidate.is_symlink() or not candidate.is_dir():
                continue
            try:
                manifest = self._read_snapshot_metadata(
                    candidate, self.environment
                )
                created_at = datetime.fromisoformat(manifest["created_at"])
                if created_at.tzinfo is None or created_at.utcoffset() is None:
                    raise ValueError("created_at не содержит часовой пояс")
            except Exception:
                self.logger.warning(
                    "Метаданные старого снимка некорректны; оставляю его без изменений: %s",
                    candidate,
                    exc_info=True,
                )
                continue
            snapshots.append((created_at, candidate.name, candidate))

        try:
            if committed_manifest is None:
                committed_manifest = self._read_snapshot_metadata(
                    committed_snapshot, self.environment
                )
            committed_at = datetime.fromisoformat(
                committed_manifest["created_at"]
            )
        except Exception:
            self.logger.exception(
                "Новый снимок создан, но не прошёл повторную проверку "
                "для очистки старых копий: %s",
                committed_snapshot,
            )
            return

        snapshots.append(
            (committed_at, committed_snapshot.name, committed_snapshot)
        )
        snapshots.sort(key=lambda item: (item[0], item[1]))

        pressure = self._storage_level(shutil.disk_usage(backup_dir)) != "ok"
        if len(snapshots) <= MAX_SNAPSHOTS_PER_ENVIRONMENT and not pressure:
            return

        validated_snapshots = []
        for item in snapshots:
            _, _, candidate = item
            try:
                fingerprint = self._snapshot_fingerprint(candidate)
            except OSError:
                self.logger.exception(
                    "Не удалось проверить состояние снимка для retention: %s",
                    candidate,
                )
                continue
            cached = self._snapshot_validation_cache.get(candidate)
            if cached is not None and cached[0] == fingerprint:
                if cached[1]:
                    validated_snapshots.append(item)
                continue
            if candidate == committed_snapshot and committed_manifest is not None:
                self._snapshot_validation_cache[candidate] = (
                    fingerprint,
                    True,
                )
                validated_snapshots.append(item)
                continue
            try:
                self._validate_snapshot(candidate, self.environment)
            except Exception:
                self._snapshot_validation_cache[candidate] = (
                    fingerprint,
                    False,
                )
                self.logger.warning(
                    "Снимок не прошёл полную проверку; оставляю его "
                    "без изменений и не учитываю в retention: %s",
                    candidate,
                    exc_info=True,
                )
                continue
            self._snapshot_validation_cache[candidate] = (
                fingerprint,
                True,
            )
            validated_snapshots.append(item)

        removable = [
            item for item in validated_snapshots
            if item[2] != committed_snapshot
        ]

        removed_any = False
        retained = len(validated_snapshots)
        for _, _, old_snapshot in removable:
            pressure = self._storage_level(shutil.disk_usage(backup_dir)) != "ok"
            if retained <= MIN_RETAINED_SNAPSHOTS or (
                retained <= MAX_SNAPSHOTS_PER_ENVIRONMENT and not pressure
            ):
                break
            try:
                shutil.rmtree(old_snapshot)
            except OSError:
                self.logger.exception(
                    "Не удалось удалить старый проверенный снимок %s",
                    old_snapshot,
                )
                continue
            self._snapshot_validation_cache.pop(old_snapshot, None)
            self.logger.info(
                "Удалён старый проверенный снимок: %s", old_snapshot
            )
            removed_any = True
            retained -= 1

        if removed_any:
            try:
                directory_fd = os.open(
                    backup_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                )
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError:
                self.logger.exception(
                    "Не удалось синхронизировать каталог после очистки снимков"
                )

    def _prune_for_space(self, backup_dir: Path) -> None:
        if self._storage_level(shutil.disk_usage(backup_dir)) == "ok":
            return
        candidates = []
        for path in backup_dir.iterdir():
            if path.is_symlink() or not path.is_dir() or path.name.startswith("."):
                continue
            try:
                manifest = self._read_snapshot_metadata(path, self.environment)
                candidates.append((manifest["created_at"], path))
            except Exception:
                self.logger.warning("Не удаляю непроверенный снимок: %s", path)
        for _, path in sorted(candidates, reverse=True):
            try:
                self._validate_snapshot(path, self.environment)
            except Exception:
                self.logger.warning("Не удаляю повреждённый снимок: %s", path)
                continue
            self._prune_old_snapshots(backup_dir, path)
            break

    @staticmethod
    def _snapshot_fingerprint(snapshot: Path) -> tuple:
        def file_fingerprint(path: Path) -> tuple:
            info = path.stat(follow_symlinks=False)
            return (
                path.name,
                info.st_dev,
                info.st_ino,
                info.st_size,
                info.st_mtime_ns,
                info.st_ctime_ns,
                info.st_mode,
                info.st_uid,
            )

        return (
            file_fingerprint(snapshot),
            *(
                file_fingerprint(path)
                for path in sorted(snapshot.iterdir(), key=lambda item: item.name)
            ),
        )

    def _prune_abandoned_stages(self, backup_dir: Path) -> None:
        cutoff = datetime.now().timestamp() - STALE_SNAPSHOT_STAGE_AGE.total_seconds()
        try:
            candidates = tuple(backup_dir.iterdir())
        except OSError:
            self.logger.exception(
                "Не удалось проверить незавершённые резервные копии: %s",
                backup_dir,
            )
            return

        for candidate in candidates:
            if not candidate.name.startswith(".snapshot-"):
                continue
            try:
                info = candidate.stat(follow_symlinks=False)
                if (
                    not stat.S_ISDIR(info.st_mode)
                    or info.st_uid != os.geteuid()
                    or stat.S_IMODE(info.st_mode) & 0o077
                    or info.st_mtime > cutoff
                ):
                    continue
                shutil.rmtree(candidate)
            except OSError:
                self.logger.exception(
                    "Не удалось удалить старый незавершённый снимок: %s",
                    candidate,
                )
            else:
                self.logger.info(
                    "Удалён незавершённый снимок старше %s: %s",
                    STALE_SNAPSHOT_STAGE_AGE,
                    candidate,
                )

    @staticmethod
    def _read_snapshot_metadata(
        snapshot: Path, expected_environment: str
    ) -> dict:
        """Check retention metadata and file sizes without rehashing full backups."""
        if snapshot.is_symlink() or not snapshot.is_dir():
            raise RuntimeError(f"Каталог снимка небезопасен: {snapshot}")
        directory_info = snapshot.stat()
        if (
            directory_info.st_uid != os.geteuid()
            or stat.S_IMODE(directory_info.st_mode) & 0o077
        ):
            raise PermissionError(f"Каталог снимка имеет небезопасные права: {snapshot}")
        manifest_path = snapshot / "manifest.json"
        if not _is_regular_private_file(manifest_path):
            raise RuntimeError("Manifest отсутствует или небезопасен")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if (
            manifest.get("format_version") != SNAPSHOT_FORMAT_VERSION
            or manifest.get("environment") != expected_environment
            or not isinstance(manifest.get("sqlite_user_version"), int)
            or not isinstance(manifest.get("files"), dict)
        ):
            raise RuntimeError("Manifest имеет неизвестный формат или окружение")

        required = {
            "database.sqlite3",
            "database.sql",
            "schedule.xlsx",
            "schedule.json",
        }
        files = manifest["files"]
        if (
            not required.issubset(files)
            or set(files) - required - {"reminder_state_cache.json"}
        ):
            raise RuntimeError("Manifest содержит неверный набор файлов")
        if {path.name for path in snapshot.iterdir()} != set(files) | {
            "manifest.json"
        }:
            raise RuntimeError("Каталог снимка содержит незаявленные файлы")
        for name, details in files.items():
            path = snapshot / name
            if not _is_regular_private_file(path) or not isinstance(details, dict):
                raise RuntimeError(f"Файл снимка небезопасен: {name}")
            if path.stat().st_size != details.get("size_bytes"):
                raise RuntimeError(f"Размер файла снимка не совпадает: {name}")
        created_at = datetime.fromisoformat(manifest["created_at"])
        if created_at.tzinfo is None or created_at.utcoffset() is None:
            raise ValueError("created_at не содержит часовой пояс")
        return manifest

    def _parse_schedule(self, xlsx_path: Path):
        # A saved workbook can legitimately lack next month's sheet.
        # Backups describe its original date, not a fresh delivery schedule.
        today = datetime.fromtimestamp(
            xlsx_path.stat().st_mtime, self.timezone
        ).date()
        days_until_sunday = (6 - today.weekday()) % 7
        target = today + timedelta(days=days_until_sunday or 7)
        parser_logger = logging.Logger(
            "reminder_bot.backup.parser", level=logging.CRITICAL + 1
        )
        schedule = parse_schedule(xlsx_path, target, parser_logger)
        people = parse_people(xlsx_path, parser_logger)
        contacts: dict[str, dict[str, dict[str, Any]]] = {}
        for role, people_by_name in people.items():
            contacts[role] = {
                name: {
                    "username": record.get("username", ""),
                    "telegram_id": record.get("tg_id"),
                }
                for name, record in people_by_name.items()
            }
        return target, schedule, contacts

    @staticmethod
    def _validate_snapshot(snapshot: Path, expected_environment: str) -> dict:
        if snapshot.is_symlink() or not snapshot.is_dir():
            raise RuntimeError(f"Каталог снимка небезопасен: {snapshot}")
        directory_info = snapshot.stat()
        if (
            directory_info.st_uid != os.geteuid()
            or (stat.S_IMODE(directory_info.st_mode) & 0o077)
        ):
            raise PermissionError(f"Каталог снимка имеет небезопасные права: {snapshot}")
        manifest_path = snapshot / "manifest.json"
        if not _is_regular_private_file(manifest_path):
            raise RuntimeError("Manifest отсутствует или небезопасен")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if (
            manifest.get("format_version") != SNAPSHOT_FORMAT_VERSION
            or manifest.get("environment") != expected_environment
            or not isinstance(manifest.get("sqlite_user_version"), int)
            or not isinstance(manifest.get("files"), dict)
        ):
            raise RuntimeError("Manifest имеет неизвестный формат или окружение")

        required = {
            "database.sqlite3",
            "database.sql",
            "schedule.xlsx",
            "schedule.json",
        }
        file_entries = manifest["files"]
        if not required.issubset(file_entries):
            raise RuntimeError("В снимке отсутствуют обязательные файлы")
        if set(file_entries) - required - {"reminder_state_cache.json"}:
            raise RuntimeError("Manifest содержит неожиданные файлы")
        expected_names = set(file_entries) | {"manifest.json"}
        if {path.name for path in snapshot.iterdir()} != expected_names:
            raise RuntimeError("Каталог снимка содержит незаявленные файлы")
        for name, details in file_entries.items():
            path = snapshot / name
            if not _is_regular_private_file(path):
                raise RuntimeError(f"Файл снимка небезопасен: {name}")
            if not isinstance(details, dict):
                raise RuntimeError(f"Некорректная запись manifest для {name}")
            if (
                path.stat().st_size != details.get("size_bytes")
                or _sha256(path) != details.get("sha256")
            ):
                raise RuntimeError(f"Проверка размера или SHA-256 не пройдена: {name}")

        schedule = json.loads((snapshot / "schedule.json").read_text(encoding="utf-8"))
        if not isinstance(schedule, dict):
            raise RuntimeError("schedule.json должен содержать JSON-объект")
        if schedule.get("source_sha256") != _sha256(snapshot / "schedule.xlsx"):
            raise RuntimeError("schedule.json не соответствует исходному XLSX")
        cache_path = snapshot / "reminder_state_cache.json"
        if cache_path.exists():
            cache_data = json.loads(cache_path.read_text(encoding="utf-8"))
            if not isinstance(cache_data, dict):
                raise RuntimeError("Кэш состояния имеет неверный формат")
        with closing(
            sqlite3.connect(
                f"file:{snapshot / 'database.sqlite3'}?mode=ro", uri=True
            )
        ) as connection:
            result = connection.execute("PRAGMA integrity_check").fetchone()
        if not result or result[0] != "ok":
            raise RuntimeError("SQLite integrity_check не пройден")
        return manifest

    def restore_snapshot(self, snapshot: Path) -> None:
        with application_instance_lock(self.shm_dir):
            self._restore_snapshot_locked(snapshot)

    def _restore_snapshot_locked(self, snapshot: Path) -> None:
        assert_bot_stopped(Path(__file__).resolve().parent.parent, self.shm_dir)
        self._require_external_mount()
        if snapshot.is_symlink():
            raise ValueError("Каталог снимка не может быть символической ссылкой")
        snapshot = snapshot.resolve(strict=True)
        backup_dir = (self.backup_base / self.environment).resolve()
        if not snapshot.is_relative_to(backup_dir):
            raise ValueError("Снимок должен находиться в каталоге своего окружения")
        self._validate_snapshot(snapshot, self.environment)
        self._require_external_mount()
        for suffix in ("-wal", "-shm", "-journal"):
            sidecar = Path(f"{self.registry_path}{suffix}")
            if sidecar.exists() or sidecar.is_symlink():
                raise RuntimeError(
                    "Перед восстановлением обнаружен SQLite sidecar; "
                    f"проверьте остановку бота: {sidecar}"
                )

        destinations = {
            "database.sqlite3": self.registry_path,
            "schedule.xlsx": self.shm_dir / "reminder_bot_current.xlsx",
            "reminder_state_cache.json": self.shm_dir / "reminder_state_cache.json",
        }
        manifest = json.loads((snapshot / "manifest.json").read_text(encoding="utf-8"))
        staged: dict[Path, Path | None] = {}
        originals: dict[Path, Path | None] = {}
        installed: set[Path] = set()
        try:
            for name, destination in destinations.items():
                if name not in manifest["files"]:
                    if name == "reminder_state_cache.json":
                        staged[destination] = None
                        continue
                    raise RuntimeError(f"Снимок не содержит {name}")
                if destination.exists() and not _is_regular_private_file(destination):
                    raise PermissionError(
                        f"Рабочий файл восстановления небезопасен: {destination}"
                    )
                _secure_directory(destination.parent)
                fd, temp_name = tempfile.mkstemp(
                    prefix=".restore-", dir=destination.parent
                )
                temp_path = Path(temp_name)
                staged[destination] = temp_path
                with os.fdopen(fd, "wb") as target, (snapshot / name).open("rb") as source:
                    os.fchmod(target.fileno(), 0o600)
                    shutil.copyfileobj(source, target)
                    target.flush()
                    os.fsync(target.fileno())
                if name == "schedule.xlsx":
                    saved_at = (snapshot / name).stat().st_mtime
                    os.utime(temp_path, (saved_at, saved_at))
            for destination in staged:
                if destination.exists():
                    fd, old_name = tempfile.mkstemp(
                        prefix=".restore-old-", dir=destination.parent
                    )
                    os.close(fd)
                    old_path = Path(old_name)
                    old_path.unlink()
                    os.replace(destination, old_path)
                    originals[destination] = old_path
                else:
                    originals[destination] = None
                if staged[destination] is not None:
                    os.replace(staged[destination], destination)
                    installed.add(destination)
            for parent in {path.parent for path in destinations.values()}:
                _fsync_directory(parent)
        except BaseException as install_error:
            rollback_error = None
            for destination, old_path in reversed(tuple(originals.items())):
                try:
                    if old_path is not None and old_path.exists():
                        destination.unlink(missing_ok=True)
                        os.replace(old_path, destination)
                    elif old_path is None and destination in installed:
                        destination.unlink(missing_ok=True)
                except OSError as exc:
                    rollback_error = rollback_error or exc
                    self.logger.exception(
                        "Не удалось откатить восстановление файла %s",
                        destination,
                    )
            try:
                for parent in {path.parent for path in destinations.values()}:
                    _fsync_directory(parent)
            except OSError as exc:
                rollback_error = rollback_error or exc
                self.logger.exception(
                    "Не удалось синхронизировать каталог после отката"
                )
            if rollback_error is not None:
                raise RuntimeError(
                    "Восстановление не завершилось, а полный откат не удался; "
                    "сохраните файлы .restore-old-* для ручного восстановления"
                ) from rollback_error
            raise install_error
        else:
            cleanup_error = None
            for old_path in originals.values():
                if old_path is not None:
                    try:
                        old_path.unlink(missing_ok=True)
                    except OSError as exc:
                        cleanup_error = cleanup_error or exc
                        self.logger.exception(
                            "Не удалось удалить временный оригинал %s", old_path
                        )
            try:
                for parent in {path.parent for path in destinations.values()}:
                    _fsync_directory(parent)
            except OSError as exc:
                cleanup_error = cleanup_error or exc
                self.logger.exception(
                    "Не удалось синхронизировать каталоги после очистки"
                )
            if cleanup_error is not None:
                raise RuntimeError(
                    "Файлы восстановлены и синхронизированы, но очистка "
                    "временных оригиналов завершилась ошибкой"
                ) from cleanup_error
        finally:
            for temp_path in staged.values():
                if temp_path is not None:
                    temp_path.unlink(missing_ok=True)

    def _require_external_mount(self) -> None:
        if self.require_mount and not self.mount_point.is_mount():
            raise RuntimeError(f"Внешний диск не смонтирован: {self.mount_point}")
