"""Сборка приложения: логгеры, PID-файл, scheduler, polling и обработчики."""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import signal
import sqlite3
import stat
import tempfile
import time
from contextlib import suppress
from dataclasses import replace as dataclass_replace
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import MenuButtonWebApp, WebAppInfo
from apscheduler.jobstores.base import JobLookupError

if __package__:
    from .cli import CliArgs
    from .commands import sync_admin_commands
    from .config import (
        PROJECT_DIR,
        get_tz,
        load_credentials,
        load_schedule,
        load_templates,
        load_webapp_config,
        service_registry_path,
    )
    from .excel_parser import (
        ROLES,
        parse_people,
        parse_schedule,
        SheetEmptyError,
        SheetNotFoundError,
        ServiceEvent,
        ServiceAssignment,
        parse_workbook_service_events,
        parse_service_events,
        parse_service_rosters,
        resolve_assignments,
    )
    from .admin_report import report_text, send_admin_reports
    from .event_notes import EventNoteResolutionError, resolve_event_note
    from .handlers import register, send_to_service_group
    from .logger import setup_loggers
    from .notifier import (
        TELEGRAM_TEXT_LIMIT,
        _notify_admin,
        _utf16_length,
        build_general_message,
        build_personal_message,
        build_personal_answer_kb,
        send_assignments,
    )
    from .presentation import (
        apply_replacements,
        display_name,
        format_informational_notes,
        format_task_assignments,
        format_task_responsibles,
        task_assignment_names,
        task_is_informational,
    )
    from .ui import send_telegram_screen, visible_units
    from .secure_fs import open_private_directory
    from .month_summary import send_due_month_summaries
    from .personal_reminders import personal_reminder_block_reason
    from .readiness import run_readiness_tick
    from .schedule_plan import plan_events
    from .scheduler import (
        add_service_reminder_jobs,
        build_scheduler,
        in_reminder_period,
        is_reminder_event_date,
        reminder_occurrence,
        next_sunday_date,
        refresh_user_reminder_jobs as refresh_user_reminder_schedule_jobs,
        scheduled_reminder_date,
    )
    from .service_registry import DEFAULT_GROUP_TEMPLATE, ServiceRegistry
    from .state import StateCache
    from .yandex_disk import RETRY_ATTEMPTS, RETRY_INTERVAL, download_public, source_version
    from .backup import (
        BackupManager,
        application_instance_lock,
        environment_name,
    )
else:  # pragma: no cover - поддержка запуска файла напрямую
    from bot.cli import CliArgs
    from bot.commands import sync_admin_commands
    from bot.config import (
        PROJECT_DIR,
        get_tz,
        load_credentials,
        load_schedule,
        load_templates,
        load_webapp_config,
        service_registry_path,
    )
    from bot.excel_parser import (
        ROLES,
        parse_people,
        parse_schedule,
        SheetEmptyError,
        SheetNotFoundError,
        ServiceEvent,
        ServiceAssignment,
        parse_workbook_service_events,
        parse_service_events,
        parse_service_rosters,
        resolve_assignments,
    )
    from bot.admin_report import report_text, send_admin_reports
    from bot.event_notes import EventNoteResolutionError, resolve_event_note
    from bot.handlers import register, send_to_service_group
    from bot.logger import setup_loggers
    from bot.notifier import (
        TELEGRAM_TEXT_LIMIT,
        _notify_admin,
        _utf16_length,
        build_general_message,
        build_personal_message,
        build_personal_answer_kb,
        send_assignments,
    )
    from bot.presentation import (
        apply_replacements,
        display_name,
        format_informational_notes,
        format_task_assignments,
        format_task_responsibles,
        task_assignment_names,
        task_is_informational,
    )
    from bot.ui import send_telegram_screen, visible_units
    from bot.secure_fs import open_private_directory
    from bot.month_summary import send_due_month_summaries
    from bot.personal_reminders import personal_reminder_block_reason
    from bot.readiness import run_readiness_tick
    from bot.schedule_plan import plan_events
    from bot.scheduler import (
        add_service_reminder_jobs,
        build_scheduler,
        in_reminder_period,
        is_reminder_event_date,
        reminder_occurrence,
        next_sunday_date,
        refresh_user_reminder_jobs as refresh_user_reminder_schedule_jobs,
        scheduled_reminder_date,
    )
    from bot.service_registry import DEFAULT_GROUP_TEMPLATE, ServiceRegistry
    from bot.state import StateCache
    from bot.yandex_disk import RETRY_ATTEMPTS, RETRY_INTERVAL, download_public, source_version
    from bot.backup import (
        BackupManager,
        application_instance_lock,
        environment_name,
    )


UPCOMING_EVENTS_CACHE_SECONDS = 300
# «Мои служения» и обзор ответственных — ближайшие недели.
UPCOMING_WEEKS_DEFAULT = 6


def _next_sunday_in_tz(tz) -> date:
    return next_sunday_date(datetime.now(tz).date())


def _pid_path(shm_dir: Path) -> Path:
    return shm_dir / "reminder_bot_bot.pid"


def _current_process_starttime() -> str:
    stat_fields = (
        Path("/proc/self/stat")
        .read_text(encoding="utf-8")
        .rsplit(")", 1)[1]
        .split()
    )
    return stat_fields[19]


def _open_directory_without_symlinks(path: Path) -> int:
    return open_private_directory(path)


def _write_pid(path: Path) -> None:
    directory_fd = _open_directory_without_symlinks(path.parent)
    pid_fd = -1
    created = False
    try:
        pid_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        pid_fd = os.open(path.name, pid_flags, 0o600, dir_fd=directory_fd)
        created = True
        try:
            os.fchmod(pid_fd, 0o600)
            with os.fdopen(pid_fd, "w", encoding="utf-8") as pid_file:
                pid_fd = -1
                pid_file.write(f"{os.getpid()}:{_current_process_starttime()}")
        except BaseException:
            if created:
                os.unlink(path.name, dir_fd=directory_fd)
            raise
    finally:
        if pid_fd >= 0:
            os.close(pid_fd)
        os.close(directory_fd)


def _remove_pid_if_owned(path: Path) -> None:
    directory_fd = _open_directory_without_symlinks(path.parent)
    try:
        try:
            pid_fd = os.open(
                path.name,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                dir_fd=directory_fd,
            )
        except FileNotFoundError:
            return
        try:
            pid_stat = os.fstat(pid_fd)
            if (
                not stat.S_ISREG(pid_stat.st_mode)
                or pid_stat.st_uid != os.geteuid()
                or stat.S_IMODE(pid_stat.st_mode) != 0o600
            ):
                return
            with os.fdopen(pid_fd, "r", encoding="utf-8") as pid_file:
                pid_fd = -1
                if pid_file.read().strip() != (
                    f"{os.getpid()}:{_current_process_starttime()}"
                ):
                    return
        finally:
            if pid_fd >= 0:
                os.close(pid_fd)
        os.unlink(path.name, dir_fd=directory_fd)
    finally:
        os.close(directory_fd)


def _reschedule_open_polls(registry, schedule_close, logger) -> None:
    """Таймеры итога открытых опросов замены после перезапуска.

    Сбой одного таймера не мешает запуску: запрос остаётся открытым,
    замену можно назначить через /change."""
    for poll in registry.list_open_replacement_polls():
        try:
            schedule_close(
                poll["poll_id"], datetime.fromisoformat(poll["deadline"])
            )
        except Exception:
            logger.exception(
                "Не удалось поставить таймер опроса замены %s", poll["poll_id"]
            )


TELEGRAM_RETRY_FIRST_DELAY = 5
TELEGRAM_RETRY_MAX_DELAY = 300


async def _retry_delays():
    """Паузы между повторами: 5 с, затем вдвое больше, но не дольше 5 минут."""
    delay = TELEGRAM_RETRY_FIRST_DELAY
    while True:
        await asyncio.sleep(delay)
        delay = min(delay * 2, TELEGRAM_RETRY_MAX_DELAY)
        yield


async def _wait_for_telegram(bot: Bot, logger) -> bool:
    """Ждёт, пока Telegram ответит на getMe.

    aiogram вызывает getMe в начале start_polling без повторов: сбой DNS после
    перезагрузки сервера или 502 от Telegram иначе завершает процесс.
    Обработчики SIGTERM/SIGINT aiogram ставит только в start_polling, поэтому
    на время ожидания стоп-сигнал обрабатывается здесь. Возвращает False,
    если пришёл сигнал остановки.
    """
    if not callable(getattr(bot, "me", None)):
        return True
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    installed = []
    for sig in (signal.SIGTERM, signal.SIGINT):
        with suppress(NotImplementedError, RuntimeError, ValueError):
            loop.add_signal_handler(sig, stop.set)
            installed.append(sig)
    stop_wait = asyncio.create_task(stop.wait())
    delay = TELEGRAM_RETRY_FIRST_DELAY
    try:
        while True:
            request = asyncio.create_task(bot.me())
            await asyncio.wait({request, stop_wait}, return_when=asyncio.FIRST_COMPLETED)
            if stop.is_set():
                request.cancel()
                await asyncio.gather(request, return_exceptions=True)
                return False
            try:
                request.result()
                return True
            except (TelegramNetworkError, TelegramServerError, TelegramRetryAfter) as exc:
                logger.warning("Telegram недоступен при запуске, повторим: %s", exc)
                pause = delay
                if isinstance(exc, TelegramRetryAfter):
                    pause = max(pause, exc.retry_after)
            await asyncio.wait({stop_wait}, timeout=pause)
            if stop.is_set():
                return False
            delay = min(delay * 2, TELEGRAM_RETRY_MAX_DELAY)
    finally:
        stop_wait.cancel()
        for sig in installed:
            loop.remove_signal_handler(sig)


async def _configure_webapp_menu(bot: Bot, project_dir: Path, logger) -> None:
    config_candidates = (
        project_dir / "reminder_bot_webapp_config.json",
        project_dir / "webapp_config.json",
    )
    if not any(path.is_file() for path in config_candidates):
        return
    try:
        config = load_webapp_config(project_dir)
    except (OSError, RuntimeError, ValueError):
        logger.exception("Не удалось загрузить конфигурацию Telegram Web App")
        return
    if config.public_url is None:
        return
    try:
        await bot.set_chat_menu_button(
            menu_button=MenuButtonWebApp(
                text="Открыть приложение",
                web_app=WebAppInfo(url=config.public_url),
            )
        )
    except TelegramAPIError:
        logger.exception("Не удалось настроить кнопку Telegram Web App")


def _existing_pid_process_status(path: Path) -> tuple[str, bool] | None:
    directory_fd = _open_directory_without_symlinks(path.parent)
    try:
        try:
            pid_fd = os.open(
                path.name,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                dir_fd=directory_fd,
            )
        except FileNotFoundError:
            return None
        except OSError:
            return None
        try:
            pid_stat = os.fstat(pid_fd)
            if (
                not stat.S_ISREG(pid_stat.st_mode)
                or pid_stat.st_uid != os.geteuid()
                or pid_stat.st_nlink != 1
                or stat.S_IMODE(pid_stat.st_mode) & 0o022
            ):
                return None
            content_bytes = os.read(pid_fd, 129)
            if len(content_bytes) > 128:
                return None
            content = content_bytes.decode("ascii").strip()
        except UnicodeDecodeError:
            return None
        finally:
            os.close(pid_fd)
    finally:
        os.close(directory_fd)

    pid, separator, starttime = content.partition(":")
    if not separator or not pid.isdigit() or not starttime.isdigit():
        return None

    try:
        process_fields = (
            Path(f"/proc/{pid}/stat")
            .read_text(encoding="utf-8")
            .rsplit(")", 1)[1]
            .split()
        )
    except FileNotFoundError:
        return pid, False
    except PermissionError:
        return None
    running = (
        len(process_fields) > 19
        and process_fields[0] != "Z"
        and process_fields[19] == starttime
    )
    return pid, running


def _build_state(
    assignments: dict[str, dict], group_chat_id: int | None = None
) -> dict:
    return {
        "sunday_date": None,
        "group_chat_id": group_chat_id,
        "general_message_id": None,
        "general_sent": False,
        "general_text": None,
        "complete": False,
        "participants": {
            role: {
                "name": info["name"],
                "username": info["username"],
                "tg_id": info["tg_id"],
                "is_informational": bool(
                    info.get("is_informational", False)
                ),
                "delivered": False,
                "notification_skipped": False,
                "confirmed": False,
                "message_id": None,
                "chat_id": None,
            }
            for role, info in assignments.items()
        },
    }


def _merge_week_state(
    previous: dict | None,
    current: dict,
    group_chat_id: int | None = None,
    legacy_group_chat_id: int | None = None,
) -> dict:
    if not previous or previous.get("sunday_date") != current["sunday_date"]:
        return current

    if group_chat_id is None:
        group_chat_id = current.get("group_chat_id")
    old_group_chat_id = previous.get("group_chat_id")
    if old_group_chat_id is None:
        old_group_chat_id = legacy_group_chat_id
    current["group_chat_id"] = group_chat_id
    same_group_chat = old_group_chat_id == group_chat_id
    if same_group_chat:
        current["general_message_id"] = previous.get("general_message_id")
        current["general_sent"] = previous.get(
            "general_sent", previous.get("general_message_id") is not None
        )
        current["general_text"] = previous.get("general_text")
    old_participants = previous.get("participants", {})
    for role, participant in current["participants"].items():
        old = old_participants.get(role, {})
        if old.get("tg_id") == participant["tg_id"]:
            old_participant_group_chat_id = old.get("group_chat_id")
            if old_participant_group_chat_id is None:
                old_participant_group_chat_id = old_group_chat_id
            same_participant_group = (
                old_participant_group_chat_id == group_chat_id
            )
            participant["delivered"] = old.get(
                "delivered", old.get("message_id") is not None
            )
            participant["notification_skipped"] = old.get(
                "notification_skipped", False
            )
            participant["message_id"] = old.get("message_id")
            participant["chat_id"] = old.get("chat_id")
            participant["group_chat_id"] = group_chat_id
            participant["confirmed"] = old.get("confirmed", False)
            participant["group_update_sent"] = (
                old.get("group_update_sent", False)
                if same_participant_group
                else False
            )
            if not same_participant_group:
                participant["group_update_persisted"] = False
    return current


async def _download_schedule(
    public_url: str,
    path: Path,
    logger: logging.Logger,
    *,
    backup_manager: BackupManager | None = None,
    cache_dir: Path | None = None,
    **options: Any,
) -> Path:
    if cache_dir is not None:
        options.update(
            cache_dir=cache_dir,
            validate=lambda candidate: parse_workbook_service_events(candidate, logger),
        )
    await download_public(public_url, path, logger, **options)
    if backup_manager is not None:
        try:
            await backup_manager.queue_schedule_download(path)
        except Exception as exc:
            logger.exception("Не удалось сохранить постоянную копию Excel")
            await backup_manager.report_failure(
                "schedule-copy",
                "Не удалось сохранить постоянную копию Excel: "
                f"{type(exc).__name__}: {exc}",
            )
            await backup_manager.check_storage()
    return path


async def _prepare_test_notification_previews(
    credentials,
    templates: dict,
    shm_dir: Path,
    tz,
    debug_logger,
    backup_manager: BackupManager | None = None,
) -> dict:
    sunday_date = _next_sunday_in_tz(tz)
    debug_dir = shm_dir / "debug"
    directory_fd = open_private_directory(debug_dir, create=True)
    os.close(directory_fd)

    file_fd, file_name = tempfile.mkstemp(
        prefix=f"schedule-{sunday_date:%Y%m%d}-",
        suffix=".xlsx",
        dir=debug_dir,
    )
    os.close(file_fd)
    xlsx_path = Path(file_name)
    try:
        await _download_schedule(
            credentials.yandex_public_url,
            xlsx_path,
            debug_logger,
            attempts=1,
            interval=0,
            backup_manager=backup_manager,
            cache_dir=shm_dir / "schedule-source",
        )
        schedule = parse_schedule(xlsx_path, sunday_date, debug_logger)
        people = parse_people(xlsx_path, debug_logger)
        issues = [
            f"На {sunday_date:%d.%m.%Y} не найдено назначение для роли «{role}»"
            for role in ROLES
            if role not in schedule
        ]
        assignments = resolve_assignments(
            schedule, people, debug_logger, issues
        )
        sunday_str = sunday_date.strftime("%d.%m.%Y")
        return {
            "sunday_date": sunday_date,
            "group_text": build_general_message(
                assignments, templates, sunday_str
            ),
            "personal_messages": [
                (
                    role,
                    info["name"],
                    build_personal_message(role, info, templates, sunday_str),
                )
                for role, info in assignments.items()
                if info.get("tg_id")
            ],
            "issues": issues,
        }
    finally:
        xlsx_path.unlink(missing_ok=True)


async def _download_backup_schedule(
    public_url: str,
    path: Path,
    logger: logging.Logger,
    *,
    shm_dir: Path | None = None,
    registry: ServiceRegistry | None = None,
) -> None:
    options = {}
    if shm_dir is not None:
        options = {
            "cache_dir": shm_dir / "schedule-source",
            "validate": lambda candidate: parse_workbook_service_events(candidate, logger),
        }
    await download_public(
        public_url,
        path,
        logger,
        attempts=RETRY_ATTEMPTS,
        interval=RETRY_INTERVAL,
        **options,
    )
    if registry is not None:
        await _refresh_schedule_reference_data(
            registry, public_url, path, logger,
            shm_dir / "schedule-source" if shm_dir is not None else None,
        )


async def _refresh_schedule_reference_data(
    registry, public_url: str, path: Path, logger, cache_dir: Path | None = None,
) -> None:
    services = registry.service_catalog()
    events = await asyncio.to_thread(parse_workbook_service_events, path, logger, services)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    current = source_version(public_url, cache_dir) if cache_dir is not None else None
    if current is not None and current != digest:
        logger.info("Устаревший результат обновления Excel пропущен")
        return
    notes = [
        [event.service_id, event.event_date.isoformat(), item.task, item.name]
        for event in events for item in event.assignments if item.is_informational
    ]
    source_key = hashlib.sha256(public_url.encode()).hexdigest()
    registry.save_schedule_notes(source_key, digest, notes)
    service_rows = registry.list_services()
    for index, service in enumerate(service_rows):
        assignments = _service_event_assignments([
            event for event in events if event.service_id == service["id"]
        ])
        if index == 0:
            _sync_rosters_and_contacts(
                registry, path, service["id"], assignments, services, logger, None
            )
        else:
            try:
                registry.ensure_member_contacts(service["id"], assignments)
            except Exception:
                logger.exception("Не удалось дополнить привязки участников служения %s", service["id"])


def _saved_note_events(registry, public_url: str, first: date, last: date) -> list:
    notes = registry.get_schedule_notes(hashlib.sha256(public_url.encode()).hexdigest())
    return [
        ServiceEvent(service, date.fromisoformat(day), (
            ServiceAssignment(task, name, is_informational=True),
        ))
        for service, day, task, name in notes
        if first.isoformat() <= day <= last.isoformat()
    ]


def _approved_service_events(
    registry, public_url: str, first: date, last: date, *,
    with_replacements: bool = True,
) -> list:
    notes = _saved_note_events(registry, public_url, first, last)
    events = _apply_bot_plans(notes, registry, first, last, approved_only=True)
    for service_id in sorted({event.service_id for event in events}):
        registry.ensure_member_contacts(service_id, _service_event_assignments([
            event for event in events if event.service_id == service_id
        ]))
    return _apply_event_replacements(events, registry) if with_replacements else events


async def _notify_super_admins(
    bot: Bot,
    service_registry: ServiceRegistry | None,
    message: str,
    debug_logger: logging.Logger,
    *,
    primary_admin_id: int | None = None,
    delivered_ids: set[int] | None = None,
) -> None:
    delivered_ids = delivered_ids if delivered_ids is not None else set()
    try:
        admins = (
            service_registry.list_admin_ids()
            if service_registry is not None else {}
        )
    except (sqlite3.Error, OSError, RuntimeError):
        debug_logger.exception(
            "Не удалось прочитать суперадминистраторов для уведомления о бэкапе"
        )
        admins = {}
    super_admin_ids = sorted(key for key, is_super in admins.items() if is_super)
    if not super_admin_ids and primary_admin_id is not None:
        debug_logger.warning(
            "Реестр суперадминистраторов недоступен или пуст; "
            "уведомление о бэкапе получит настроенный главный администратор"
        )
        super_admin_ids = [primary_admin_id]
    if not super_admin_ids:
        raise RuntimeError("В реестре не настроены суперадминистраторы")

    failed = False
    for telegram_id in super_admin_ids:
        if telegram_id in delivered_ids:
            continue
        if await _notify_admin(bot, telegram_id, message, debug_logger):
            delivered_ids.add(telegram_id)
        else:
            failed = True
    if failed:
        raise RuntimeError("Не удалось уведомить всех суперадминистраторов")


async def _load_service_task_assignments(
    credentials,
    shm_dir: Path,
    tz,
    service_id: str,
    debug_logger,
    only_date: date | None = None,
    *,
    return_event_found: bool = False,
    services=None,
    issues: list[str] | None = None,
    registry: ServiceRegistry | None = None,
    raw_out: dict | None = None,
    own_roster_only: bool = False,
    backup_manager: BackupManager | None = None,
    require_approved_plan: bool = False,
) -> dict[str, dict[str, object]] | tuple[dict[str, dict[str, object]], bool]:
    """Назначения служения из графика; issues пополняется предупреждениями.

    Люди с Telegram получают привязки к новым задачам графика из БД.
    Только явная загрузка Excel обновляет состав из «Списков».
    """
    sunday_date = only_date or _next_sunday_in_tz(tz)
    planned: list = []
    covered: set = set()
    if registry is not None and only_date is not None:
        planned, covered = _bot_plan_events(
            registry, only_date, only_date, service_id=service_id
        )
    bot_plan = _covered(
        SimpleNamespace(service_id=service_id, event_date=only_date),
        covered,
    ) if only_date is not None else False
    if require_approved_plan and not bot_plan:
        # Нельзя заменять отсутствующий или недоступный утверждённый график
        # живым Excel при подготовке доставки.
        if return_event_found:
            return {}, False
        return {}
    if bot_plan:
        events = [
            event for event in planned
            if event.service_id == service_id and event.event_date == only_date
        ]
        event_found = bool(events)
        notes = _saved_note_events(
            registry, credentials.yandex_public_url, only_date, only_date
        )
        events += [event for event in notes if event.service_id == service_id]
        result = _service_event_assignments(events)
        registry.ensure_member_contacts(service_id, result)
        if raw_out is not None:
            raw_out.clear()
            raw_out.update(result)
        result = apply_replacements(
            result, registry.list_replacements(service_id, only_date.isoformat())
        )
        return (result, event_found) if return_event_found else result
    debug_dir = shm_dir / "debug"
    directory_fd = open_private_directory(debug_dir, create=True)
    os.close(directory_fd)
    file_fd, file_name = tempfile.mkstemp(
        prefix=f"schedule-tasks-{sunday_date:%Y%m%d}-",
        suffix=".xlsx",
        dir=debug_dir,
    )
    os.fchmod(file_fd, 0o600)
    os.close(file_fd)
    xlsx_path = Path(file_name)
    try:
        await _download_schedule(
            credentials.yandex_public_url,
            xlsx_path,
            debug_logger,
            attempts=1,
            interval=0,
            backup_manager=backup_manager,
            cache_dir=shm_dir / "schedule-source",
            force=own_roster_only,
        )
        events_by_service = parse_service_events(
            xlsx_path, sunday_date, debug_logger, services
        )
        service_events = events_by_service.get(service_id, [])
        event_found = any(
            event.event_date == sunday_date for event in service_events
        )
        selected = []
        for event in service_events:
            if only_date is not None and event.event_date != only_date:
                continue
            if issues is not None:
                issues.extend(event.issues)
            selected.append(event)
        result = _service_event_assignments(selected)
        if raw_out is not None:
            # Назначения из графика без замен: отчёт админам применяет
            # действующие замены сам при каждом обновлении.
            raw_out.clear()
            raw_out.update(result)
        if registry is not None:
            _sync_rosters_and_contacts(
                registry, xlsx_path, service_id, result, services,
                debug_logger, issues, own_roster_only,
            )
            if only_date is not None:
                # Замена на одну дату (этап 3.3) главнее графика. Без
                # only_date здесь имена за весь месяц — замены не применяются.
                result = apply_replacements(
                    result,
                    registry.list_replacements(service_id, only_date.isoformat()),
                )
        if return_event_found:
            return result, event_found
        return result
    finally:
        xlsx_path.unlink(missing_ok=True)


def _service_event_assignments(events: list) -> dict:
    assignments = {}
    for event in events:
        for assignment in event.assignments:
            info = assignments.setdefault(
                assignment.task, {"names": set(), "is_informational": True}
            )
            if assignment.name:
                info["names"].add(assignment.name)
            info["is_informational"] &= assignment.is_informational
    return {
        task: {"names": sorted(info["names"]), "is_informational": info["is_informational"]}
        for task, info in sorted(assignments.items())
    }


# Проблемы листа «Списки» в целом (неизвестное служение) — не одного
# служения: админ получает их с первым отправленным уведомлением дня.
SHEET_ISSUE_PREFIX = "«Списки»:"
_REPORTED_SHEET_ISSUES: dict[str, date] = {}


def _sync_rosters_and_contacts(
    registry: ServiceRegistry,
    xlsx_path: Path,
    service_id: str,
    assignments: dict,
    services,
    debug_logger,
    issues: list[str] | None,
    own_roster_only: bool = False,
) -> None:
    """Сбой «Списков» не мешает напоминаниям: он только записывается в лог.
    own_roster_only — сверка из бота (её запускает админ служения):
    обновляется состав только этого служения."""
    roster_issues: list[str] = []
    try:
        rosters = parse_service_rosters(
            xlsx_path, debug_logger, services, roster_issues
        )
        if own_roster_only:
            rosters = (
                {service_id: rosters[service_id]}
                if service_id in rosters else {}
            )
        registry.sync_service_rosters(rosters)
    except Exception:
        debug_logger.exception("Не удалось обновить состав служений из «Списков»")
    else:
        service = registry.get_service(service_id)
        prefixes = (SHEET_ISSUE_PREFIX,) + (
            (f"«Списки», {service['name']}:",) if service else ()
        )
        if issues is not None:
            issues.extend(
                issue for issue in roster_issues if issue.startswith(prefixes)
            )
    try:
        registry.ensure_member_contacts(service_id, assignments)
    except Exception:
        debug_logger.exception("Не удалось дополнить привязки участников служения")


async def _load_upcoming_service_events(
    credentials,
    shm_dir: Path,
    tz,
    debug_logger,
    weeks: int = 6,
    services=None,
    until: date | None = None,
    since: date | None = None,
    backup_manager: BackupManager | None = None,
    force: bool = False,
    registry: ServiceRegistry | None = None,
    source_out: dict[str, str] | None = None,
) -> list:
    """Read schedule events from today (or since, if it is earlier) through
    the given number of weeks (or through until, if it is later)."""
    today = datetime.now(tz).date()
    first_day = min(since or today, today)
    last_day = max(today + timedelta(days=weeks * 7), until or today)
    months = []
    month = first_day.replace(day=1)
    while month <= last_day:
        months.append(month)
        month = (month + timedelta(days=32)).replace(day=1)
    debug_dir = shm_dir / "debug"
    directory_fd = open_private_directory(debug_dir, create=True)
    os.close(directory_fd)
    file_fd, file_name = tempfile.mkstemp(
        prefix=f"schedule-upcoming-{today:%Y%m%d}-",
        suffix=".xlsx",
        dir=debug_dir,
    )
    os.fchmod(file_fd, 0o600)
    os.close(file_fd)
    xlsx_path = Path(file_name)
    try:
        await _download_schedule(
            credentials.yandex_public_url,
            xlsx_path,
            debug_logger,
            attempts=1,
            interval=0,
            backup_manager=backup_manager,
            cache_dir=shm_dir / "schedule-source",
            force=force,
        )
        if registry is not None:
            await _refresh_schedule_reference_data(
                registry, credentials.yandex_public_url, xlsx_path, debug_logger,
                shm_dir / "schedule-source",
            )
        events = []
        for month in months:
            try:
                events_by_service = parse_service_events(
                    xlsx_path, month, debug_logger, services
                )
            except (SheetNotFoundError, SheetEmptyError):
                # Лист следующего месяца может быть ещё не создан или пуст.
                if month == months[0]:
                    raise
                debug_logger.info(
                    "Лист графика на %s пока недоступен", f"{month:%m.%Y}"
                )
                continue
            except RuntimeError:
                debug_logger.exception(
                    "Лист графика на %s повреждён", f"{month:%m.%Y}"
                )
                raise
            events.extend(
                event
                for service_events in events_by_service.values()
                for event in service_events
                if first_day <= event.event_date <= last_day
            )
        if source_out is not None:
            source_out["digest"] = hashlib.sha256(xlsx_path.read_bytes()).hexdigest()
        return sorted(events, key=lambda event: (event.event_date, event.service_id))
    finally:
        xlsx_path.unlink(missing_ok=True)


def _bot_plan_events(
    registry, first: date, last: date, *, service_id: str | None = None
) -> tuple[list, set]:
    """Утверждённые графики бота (решение 15) на месяцы окна: события и
    (служение, месяц) / (служение, дата), которые они заменяют в Excel."""
    if registry is None:
        return [], set()
    months = []
    # Своя дата задачи (среда) — может быть до первого воскресенья месяца,
    # а «вс +1» — уже в следующем: месяцы с запасом в обе стороны.
    month = (first - timedelta(days=7)).replace(day=1)
    while month <= last + timedelta(days=7):
        months.append(f"{month:%Y-%m}")
        month = (month + timedelta(days=32)).replace(day=1)
    events, covered = [], set()
    services = registry.list_services()
    if service_id is not None:
        services = [item for item in services if item["id"] == service_id]
    for service in services:
        people = None
        for key in months:
            plan = registry.get_schedule_plan(service["id"], key)
            if plan is None or plan["approved"] is None:
                continue
            if people is None:
                people = registry.readiness_people(service["id"])
            covered.add((service["id"], key))
            # Свой день задачи (среда спевки) утверждённого воскресенья
            # заменяется и без назначений: убранный из графика человек не
            # возвращается из Excel.
            for day in plan["approved"]:
                for task in plan.get("approved_tasks") or []:
                    if task.get("day") is not None:
                        covered.add((
                            service["id"],
                            date.fromisoformat(day) + timedelta(days=task["day"]),
                        ))
            for event in plan_events(plan, people):
                covered.add((service["id"], event.event_date))
                events.append(event)
    return events, covered


def _has_approved_schedule_for_event(
    registry, service_id: str, event_date: date
) -> bool:
    """Whether an approved DB plan owns this Sunday or shifted task date."""
    _, covered = _bot_plan_events(
        registry, event_date, event_date, service_id=service_id
    )
    return _covered(
        SimpleNamespace(service_id=service_id, event_date=event_date),
        covered,
    )


def _approved_assignments_state(
    registry, service_id: str, event_date: date,
) -> tuple[bool, tuple[ServiceEvent, ...]]:
    events, covered = _bot_plan_events(
        registry, event_date, event_date, service_id=service_id
    )
    return (
        _covered(
            SimpleNamespace(service_id=service_id, event_date=event_date),
            covered,
        ),
        tuple(event for event in events if event.event_date == event_date),
    )


def _covered(event, covered: set) -> bool:
    """Событие Excel заменено графиком бота: воскресенье утверждённого
    месяца, дата, которая есть в графике бота, или свой день задачи
    утверждённого воскресенья. Среда спевки 28.10
    относится к графику ноября — утверждённый октябрь её не заменяет."""
    return (event.service_id, event.event_date) in covered or (
        event.event_date.weekday() == 6
        and (event.service_id, f"{event.event_date:%Y-%m}") in covered
    )


def _apply_bot_plans(
    events: list, registry, first: date, last: date, *,
    approved_only: bool = False,
) -> list:
    """События Excel, где на месяц есть утверждённый график бота, заменяются
    его событиями: дальше график живёт в боте (решение 15)."""
    try:
        planned, covered = _bot_plan_events(registry, first, last)
    except Exception:
        if approved_only:
            raise
        # Для не-доставочных экранов сохраняем прежнее поведение при
        # недоступности БД; доставка запрашивает approved_only и fail-closed.
        return events
    if approved_only:
        events = [event for event in events if _covered(event, covered)]
    if not covered:
        return events
    kept, notes = [], {}
    for event in events:
        if not _covered(event, covered):
            kept.append(event)
            continue
        # Пометки Excel («📌 2я часть членское») остаются: график бота
        # заменяет только людей.
        informational = [a for a in event.assignments if a.is_informational]
        if informational:
            notes.setdefault((event.service_id, event.event_date), []).extend(
                informational
            )
    merged = []
    for event in planned:
        if not first <= event.event_date <= last:
            continue
        extra = notes.pop((event.service_id, event.event_date), [])
        merged.append(
            dataclass_replace(
                event, assignments=tuple(event.assignments) + tuple(extra)
            ) if extra else event
        )
    merged += [
        ServiceEvent(service_id, day, tuple(extra))
        for (service_id, day), extra in notes.items()
    ]
    return sorted(
        kept + merged,
        key=lambda event: (event.event_date, event.service_id),
    )


async def _load_month_summary_events(
    loader, registry, cache: dict, now: datetime, *,
    fresh: bool = False, **kwargs,
) -> list:
    public_url = kwargs.pop("public_url", None)
    if public_url is not None:
        today = now.date()
        return _approved_service_events(
            registry, public_url, min(kwargs.get("since") or today, today),
            kwargs.get("until") or today + timedelta(weeks=UPCOMING_WEEKS_DEFAULT),
        )
    forced_at = cache.get("forced_at")
    fresh = fresh and (
        forced_at is None or now - forced_at >= timedelta(minutes=10)
    )
    if (
        not fresh
        and cache.get("kwargs") == kwargs
        and now - cache["at"] < timedelta(minutes=30)
    ):
        events = cache["events"]
    else:
        events = await loader(
            force=fresh, with_replacements=False, approved_only=True, **kwargs
        )
        if fresh:
            cache["forced_at"] = now
        cache.update(kwargs=kwargs, at=now, events=events)
    today = now.date()
    start = min(kwargs.get("since") or today, today)
    limit = kwargs.get("until") or today + timedelta(
        weeks=UPCOMING_WEEKS_DEFAULT
    )
    # Кэш не фиксирует утверждение: отмена или замена плана действует
    # немедленно, даже пока другой пользователь ждёт свой график.
    current = _apply_bot_plans(
        events, registry, start, limit, approved_only=True
    )
    return _apply_event_replacements(current, registry)


def _apply_event_replacements(events: list, registry) -> list:
    """События графика с заменами на их даты (этап 3.3): заменённый
    уходит из задачи, заменяющий появляется."""
    if registry is None:
        return events
    result = []
    for event in events:
        replacements = registry.list_replacements(
            event.service_id, event.event_date.isoformat()
        )
        if not replacements:
            result.append(event)
            continue
        assignments = list(event.assignments)
        for item in replacements:
            task_rows = [
                row for row in assignments
                if row.task == item["task"] and not row.is_informational
            ]
            if not task_rows:
                continue
            names = [row.name for row in task_rows]
            if item["old_name"] in names:
                assignments = [
                    dataclass_replace(row, name=item["new_name"])
                    if row in task_rows and row.name == item["old_name"]
                    else row
                    for row in assignments
                ]
            elif item["new_name"] not in names:
                assignments.append(
                    dataclass_replace(task_rows[0], name=item["new_name"])
                )
        unique = list(dict.fromkeys(assignments))
        result.append(dataclass_replace(event, assignments=tuple(unique)))
    return result


async def get_group_reminder_preview(
    reminder_id: int,
    credentials,
    shm_dir: Path,
    tz,
    registry: ServiceRegistry,
    debug_logger,
    *,
    body: str | None = None,
    backup_manager: BackupManager | None = None,
) -> dict:
    """Готовит предпросмотр; body подменяет текст напоминания (объединение)."""
    reminder = registry.get_group_reminder(reminder_id)
    if reminder is None:
        raise ValueError("Групповое напоминание больше не существует.")
    service = registry.get_service(reminder["service_id"])
    if service is None:
        raise ValueError("Служение для напоминания не найдено.")

    occurrence = reminder_occurrence(datetime.now(tz), reminder)
    if occurrence is None:
        raise ValueError("Период напоминания завершён.")
    event_date, send_date = occurrence
    date_text = event_date.strftime("%d.%m.%Y")
    result = {
        "date": date_text,
        "send_date": send_date.strftime("%d.%m.%Y"),
        "time": reminder["reminder_time"],
        "text": "",
        "has_data": False,
        "error": None,
    }
    try:
        assignments, event_found = await _load_service_task_assignments(
            credentials,
            shm_dir,
            tz,
            service["id"],
            debug_logger,
            only_date=event_date,
            return_event_found=True,
            services=registry.service_catalog(),
            registry=registry,
            backup_manager=backup_manager,
            require_approved_plan=True,
        )
    except Exception:
        debug_logger.exception(
            "Не удалось загрузить график для предпросмотра напоминания %s",
            reminder_id,
        )
        result["error"] = "Не удалось загрузить график. Попробуйте позже."
        return result

    if not event_found:
        result["error"] = f"Нет данных из графика на {date_text}."
        return result

    if not any(
        not task_is_informational(task_info)
        for task_info in assignments.values()
    ):
        result["error"] = (
            "На эту дату нет задач с ответственными. "
            "Уведомление не будет отправлено."
        )
        return result

    if _reminder_task_names(reminder, assignments) == []:
        result["error"] = (
            f"На {date_text} для задачи «{reminder['task']}» никто не "
            "назначен. Напоминание не будет отправлено."
        )
        return result

    contacts = registry.list_responsible_contacts(
        credentials.admin_tg_id, service["id"]
    )
    contacts_by_name = {
        (contact["task"], contact["name"]): contact for contact in contacts
    }
    result["event_date"] = event_date.isoformat()
    result["schedule_state"] = [
        event for event in _approved_service_events(
            registry, credentials.yandex_public_url, event_date, event_date
        ) if event.service_id == service["id"]
    ]
    template = (
        body if body is not None
        else registry.resolve_group_text(reminder, service)
    )
    try:
        result["text"] = _render_group_reminder(
            template, reminder, service, assignments, contacts_by_name,
            date_text, registry.service_display_names(service["id"]),
            event_notes=resolve_event_note(
                registry, service["id"], event_date, reminder.get("task"),
                template=template,
            ),
        )
    except (sqlite3.Error, EventNoteResolutionError) as exc:
        debug_logger.exception("Не удалось прочитать заметки для предпросмотра")
        result["error"] = (
            f"Не удалось прочитать заметки события: {exc}"
            if isinstance(exc, EventNoteResolutionError)
            else "Не удалось прочитать заметки события. Попробуйте позже."
        )
        return result
    except (AttributeError, IndexError, ValueError):
        result["error"] = "Ошибка в синтаксисе шаблона."
        return result
    result["has_data"] = True
    return result


async def get_template_preview(
    service_id: str,
    kind: str,
    template: str | None,
    credentials,
    shm_dir: Path,
    tz,
    registry: ServiceRegistry,
    debug_logger,
    backup_manager: BackupManager | None = None,
) -> dict:
    """Предпросмотр шаблона служения на ближайшее воскресенье.

    kind: "default" — шаблон группового напоминания по умолчанию,
    "report" — отчёт админам. template — черновик из настройки;
    None — встроенный шаблон.
    """
    service = registry.get_service(service_id)
    if service is None:
        raise ValueError("Служение не найдено.")
    event_date = _next_sunday_in_tz(tz)
    date_text = event_date.strftime("%d.%m.%Y")
    result = {"date": date_text, "text": "", "has_data": False, "error": None}
    raw: dict = {}
    try:
        assignments, event_found = await _load_service_task_assignments(
            credentials,
            shm_dir,
            tz,
            service_id,
            debug_logger,
            only_date=event_date,
            return_event_found=True,
            services=registry.service_catalog(),
            registry=registry,
            raw_out=raw,
            backup_manager=backup_manager,
        )
    except Exception:
        debug_logger.exception(
            "Не удалось загрузить график для предпросмотра шаблона %s",
            service_id,
        )
        result["error"] = "Не удалось загрузить график. Попробуйте позже."
        return result
    if not event_found:
        result["error"] = f"Нет данных из графика на {date_text}."
        return result
    try:
        if kind == "report":
            # Как при отправке: замены применяет сам отчёт.
            result["text"] = report_text(
                registry, service, event_date, raw,
                credentials.admin_tg_id, TELEGRAM_TEXT_LIMIT, template,
            )
        else:
            contacts = registry.list_responsible_contacts(
                credentials.admin_tg_id, service_id
            )
            result["text"] = _render_group_reminder(
                template or DEFAULT_GROUP_TEMPLATE,
                {"task": None},
                service,
                assignments,
                {(item["task"], item["name"]): item for item in contacts},
                date_text,
                registry.service_display_names(service_id),
                event_notes=resolve_event_note(
                    registry, service_id, event_date, template=template or DEFAULT_GROUP_TEMPLATE
                ),
            )
    except (sqlite3.Error, EventNoteResolutionError) as exc:
        debug_logger.exception("Не удалось прочитать заметки для предпросмотра шаблона")
        result["error"] = (
            f"Не удалось прочитать заметки события: {exc}"
            if isinstance(exc, EventNoteResolutionError)
            else "Не удалось прочитать заметки события. Попробуйте позже."
        )
        return result
    except (AttributeError, IndexError, ValueError):
        result["error"] = "Ошибка в синтаксисе шаблона."
        return result
    result["has_data"] = True
    return result


class _TemplateValues(dict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def _reminder_task_names(reminder: dict, assignments: dict) -> list[str] | None:
    """Ответственные задачи напоминания на дату; None — напоминание общее."""
    task = reminder.get("task")
    if not task:
        return None
    task_info = assignments.get(task)
    if task_info is None or task_is_informational(task_info):
        return []
    return task_assignment_names(task_info)


def _render_group_reminder(
    template: str,
    reminder: dict,
    service: dict,
    assignments: dict,
    contacts_by_name: dict,
    date_text: str,
    display_names: dict[str, str] | None = None,
    *,
    event_notes: str = "",
) -> str:
    """Текст группового напоминания; для задачи — только её ответственные."""
    task = reminder.get("task") or ""
    task_names = _reminder_task_names(reminder, assignments)
    scoped = (
        {task: assignments[task]} if task_names else assignments
    )
    return template.format_map(
        _TemplateValues(
            service_name=service["name"],
            sunday_date=date_text,
            task_assignments=format_task_assignments(
                scoped, contacts_by_name, mark_unlinked=True,
                display_names=display_names,
            ),
            informational_notes=format_informational_notes(assignments),
            event_notes=event_notes,
            task=task,
            task_responsibles=(
                format_task_responsibles(
                    task, task_names, contacts_by_name, display_names=display_names,
                )
                if task_names
                else ""
            ),
        )
    )


def _same_reminder_slot(registry: ServiceRegistry, reminder: dict) -> bool:
    """Срок и время напоминания не менялись с начала обработки."""
    current = registry.get_group_reminder(reminder["id"])
    return current is not None and (
        current["reminder_days"], current["reminder_time"]
    ) == (reminder["reminder_days"], reminder["reminder_time"])


async def _notify_unapproved_schedule(
    bot, credentials, registry: ServiceRegistry, service_id: str,
    event_date: date, debug_logger,
) -> None:
    month = f"{event_date:%Y-%m}"
    if not registry.claim_readiness_notice(
        service_id, month, "schedule-unapproved"
    ):
        return
    service = registry.get_service(service_id)
    service_name = service["name"] if service else service_id
    delivered = await _notify_admin(
        bot,
        credentials.admin_tg_id,
        f"Напоминания для служения «{service_name}» на "
        f"{event_date:%m.%Y} не отправляются: администратор ещё не "
        "утвердил график и не сохранил его в БД бота. Откройте "
        "«📊 График Excel» или «📅 График», проверьте назначения и "
        "утвердите график.",
        debug_logger,
    )
    if not delivered:
        registry.release_readiness_notice(
            service_id, month, "schedule-unapproved"
        )


async def _process_service_reminder(
    bot: Bot,
    credentials,
    shm_dir: Path,
    tz,
    registry: ServiceRegistry,
    service_id: str,
    reminder_id: int,
    interaction_logger,
    debug_logger,
    *,
    run_date: date | None = None,
    state_cache: StateCache | None = None,
    backup_manager: BackupManager | None = None,
) -> None:
    service = registry.get_service(service_id)
    if service is None or not service["enabled"]:
        return
    reminder = registry.get_group_reminder(reminder_id)
    if (
        reminder is None
        or reminder["service_id"] != service_id
        or not reminder["enabled"]
    ):
        return
    event_date = (run_date or datetime.now(tz).date()) + timedelta(
        days=reminder["reminder_days"]
    )
    # Задание срабатывает каждый день; событие — в день, от которого
    # считается напоминание (решение 17, 5а): общее — воскресенье, задачи —
    # её день. Задача в другой день (разовый день человека: спевка в
    # воскресенье) — тоже, если в графике она на эту дату есть.
    on_day = is_reminder_event_date(event_date, reminder)
    if not on_day and not reminder.get("task"):
        return
    # Период — по воскресенью недели. Разовый день: понедельник — после
    # своего воскресенья (окно «вс +1»), вт–сб — до него (спевка «вс −4»).
    week_sunday = (
        event_date - timedelta(days=1) if event_date.weekday() == 0
        else event_date + timedelta(days=(6 - event_date.weekday()) % 7)
    )
    if not (
        in_reminder_period(event_date, reminder) if on_day
        else in_reminder_period(week_sunday, {**reminder, "task_day": None})
    ):
        interaction_logger.info(
            "Уведомление %s служения %s пропущено: %s вне периода",
            reminder_id, service_id, event_date,
        )
        return
    try:
        approved_plan = _has_approved_schedule_for_event(
            registry, service_id, event_date
        )
        approved_state = _approved_assignments_state(
            registry, service_id, event_date
        )
    except Exception:
        debug_logger.exception(
            "Не удалось проверить утверждённый график служения %s",
            service_id,
        )
        await _notify_admin(
            bot,
            credentials.admin_tg_id,
            f"Напоминание для служения «{service['name']}» на "
            f"{event_date:%d.%m.%Y} не отправлено: не удалось проверить, "
            "утверждён ли график в БД бота.",
            debug_logger,
        )
        return
    if not approved_plan:
        interaction_logger.warning(
            "Напоминание служения %s пропущено: график на %s не утверждён",
            service_id, event_date.strftime("%Y-%m"),
        )
        await _notify_unapproved_schedule(
            bot, credentials, registry, service_id, event_date, debug_logger
        )
        return
    schedule_issues: list[str] = []
    raw_assignments: dict = {}
    try:
        assignments, event_found = await _load_service_task_assignments(
            credentials,
            shm_dir,
            tz,
            service_id,
            debug_logger,
            only_date=event_date,
            return_event_found=True,
            services=registry.service_catalog(),
            issues=schedule_issues,
            registry=registry,
            raw_out=raw_assignments,
            backup_manager=backup_manager,
            require_approved_plan=True,
        )
    except Exception as exc:
        debug_logger.exception(
            "Не удалось подготовить уведомление служения %s на %s",
            service_id,
            event_date,
        )
        if not on_day:
            # Проверка разового дня идёт ежедневно — сбой только в лог,
            # админ узнает о нём в день задачи.
            return
        await _notify_admin(
            bot,
            credentials.admin_tg_id,
            f"Не удалось подготовить уведомление «{service['name']}» "
            f"на {event_date:%d.%m.%Y}: {type(exc).__name__}: {exc}",
            debug_logger,
        )
        return
    if not on_day and (
        not event_found or not _reminder_task_names(reminder, assignments)
    ):
        # Не день задачи, и разового дня этой задачи на дату нет.
        return
    if not event_found:
        interaction_logger.info(
            "Уведомление служения %s пропущено: событие на %s не найдено",
            service_id,
            event_date,
        )
        return
    issues_recipient = "admin:schedule-issues"
    today = datetime.now(tz).date()
    for reported, reported_on in list(_REPORTED_SHEET_ISSUES.items()):
        if reported_on != today:
            _REPORTED_SHEET_ISSUES.pop(reported, None)
    schedule_issues = [
        issue for issue in schedule_issues
        if not (
            issue.startswith(SHEET_ISSUE_PREFIX)
            and issue in _REPORTED_SHEET_ISSUES
        )
    ]
    if schedule_issues and not registry.is_group_reminder_delivered(
        reminder["id"], event_date.isoformat(), issues_recipient
    ):
        unique_issues = list(dict.fromkeys(schedule_issues))
        notified = await _notify_admin(
            bot,
            credentials.admin_tg_id,
            f"Проверьте график «{service['name']}» на "
            f"{event_date:%d.%m.%Y}:\n"
            + "\n".join(f"• {issue}" for issue in unique_issues),
            debug_logger,
        )
        if notified:
            registry.mark_group_reminder_delivered(
                reminder["id"], event_date.isoformat(), issues_recipient
            )
            for issue in unique_issues:
                if issue.startswith(SHEET_ISSUE_PREFIX):
                    _REPORTED_SHEET_ISSUES[issue] = today

    try:
        contacts = registry.list_responsible_contacts(
            credentials.admin_tg_id, service_id
        )
    except Exception as exc:
        debug_logger.exception(
            "Не удалось загрузить контакты служения %s", service_id
        )
        await _notify_admin(
            bot,
            credentials.admin_tg_id,
            f"Не удалось загрузить контакты служения «{service['name']}»: "
            f"{type(exc).__name__}: {exc}",
            debug_logger,
        )
        return
    contacts_by_name = {
        (contact["task"], contact["name"]): contact for contact in contacts
    }
    for task, task_info in assignments.items():
        if not task_is_informational(task_info):
            continue
        stale_contacts = [
            contact
            for contact in contacts
            if contact["task"] == task
        ]
        if stale_contacts:
            debug_logger.debug(
                "Информационная задача исключена из привязок служения %s: "
                "%s (в БД сохранено привязок: %s)",
                service_id,
                task,
                len(stale_contacts),
            )
    date_text = event_date.strftime("%d.%m.%Y")
    group_template = registry.resolve_group_text(reminder, service)
    try:
        group_text = _render_group_reminder(
            group_template, reminder, service, assignments, contacts_by_name,
            date_text, registry.service_display_names(service_id),
            event_notes=resolve_event_note(
                registry, service_id, event_date, reminder.get("task"),
                template=group_template,
            ),
        )
    except (AttributeError, IndexError, KeyError, ValueError, sqlite3.Error) as exc:
        debug_logger.exception("Не удалось подготовить групповое напоминание")
        await _notify_admin(
            bot,
            credentials.admin_tg_id,
            f"Не удалось подготовить текст напоминания служения «{service['name']}»: "
            f"{type(exc).__name__}",
            debug_logger,
        )
        return

    if _reminder_task_names(reminder, assignments) == []:
        # Напоминание задачи без ответственных на дату не отправляется.
        missing_recipient = "admin:no-task-responsibles"
        if not registry.is_group_reminder_delivered(
            reminder["id"], event_date.isoformat(), missing_recipient
        ) and await _notify_admin(
            bot,
            credentials.admin_tg_id,
            f"⚠️ Напоминание «{service['name']}» для задачи "
            f"«{reminder['task']}» на {date_text} не отправлено: "
            "в графике никто не назначен.",
            debug_logger,
        ):
            registry.mark_group_reminder_delivered(
                reminder["id"], event_date.isoformat(), missing_recipient
            )
        return

    if not any(
        not task_is_informational(task_info)
        for task_info in assignments.values()
    ):
        await _notify_admin(
            bot,
            credentials.admin_tg_id,
            f"В графике найдено служение «{service['name']}» на "
            f"{date_text}, но задачи не назначены.",
            debug_logger,
        )
        return

    group_chat_id = service["group_chat_id"]
    group_recipient = f"group:{group_chat_id}"
    if group_chat_id is None:
        await _notify_admin(
            bot,
            credentials.admin_tg_id,
            f"Для служения «{service['name']}» не задан групповой чат.",
            debug_logger,
        )
    elif not _same_reminder_slot(registry, reminder):
        # Пока скачивался график, срок или время напоминания изменили:
        # отправит задача нового расписания, эта не ставит отметку.
        debug_logger.info(
            "Напоминание %s перенесено во время подготовки — отправка "
            "отменена", reminder["id"],
        )
        return
    elif not registry.is_group_reminder_delivered(
        reminder["id"], event_date.isoformat(), group_recipient
    ):
        if visible_units(group_text) > TELEGRAM_TEXT_LIMIT:
            await _notify_admin(
                bot,
                credentials.admin_tg_id,
                f"Групповое уведомление «{service['name']}» на {date_text} "
                "превышает ограничение Telegram и не отправлено.",
                debug_logger,
            )
        else:
            try:
                still_approved = _has_approved_schedule_for_event(
                    registry, service_id, event_date
                )
                same_assignments = approved_state == _approved_assignments_state(
                    registry, service_id, event_date
                )
            except Exception:
                debug_logger.exception(
                    "Не удалось повторно проверить утверждённый график "
                    "служения %s",
                    service_id,
                )
                await _notify_admin(
                    bot,
                    credentials.admin_tg_id,
                    f"Групповое уведомление «{service['name']}» на "
                    f"{date_text} не отправлено: не удалось повторно "
                    "проверить утверждённый график в БД бота.",
                    debug_logger,
                )
                return
            if not still_approved:
                await _notify_unapproved_schedule(
                    bot, credentials, registry, service_id, event_date,
                    debug_logger,
                )
                return
            if not same_assignments:
                debug_logger.info(
                    "Утверждённые назначения %s на %s изменились "
                    "во время подготовки — отправка отменена",
                    service_id, event_date,
                )
                return
            try:
                await send_to_service_group(
                    lambda chat_id, text, **kwargs: send_telegram_screen(
                        bot.send_message, chat_id, text,
                        limit=TELEGRAM_TEXT_LIMIT, **kwargs,
                    ),
                    group_chat_id,
                    group_text,
                    service,
                    registry=registry,
                    logger=debug_logger,
                )
            except Exception as exc:
                debug_logger.exception(
                    "Не удалось отправить групповое уведомление %s на %s",
                    service_id,
                    event_date,
                )
                await _notify_admin(
                    bot,
                    credentials.admin_tg_id,
                    f"Не удалось отправить групповое уведомление "
                    f"«{service['name']}» на {date_text}: "
                    f"{type(exc).__name__}.",
                    debug_logger,
                )
            else:
                if _same_reminder_slot(registry, reminder):
                    registry.mark_group_reminder_delivered(
                        reminder["id"],
                        event_date.isoformat(),
                        group_recipient,
                    )
                interaction_logger.info(
                    "Групповое уведомление служения %s отправлено на %s",
                    service_id,
                    event_date,
                )
    if group_chat_id is not None and registry.is_group_reminder_delivered(
        reminder["id"], event_date.isoformat(), group_recipient
    ):
        # Отчёт отдельно от группового сообщения: недоставленный ранее
        # отчёт отправится при следующем запуске, не повторяя группу.
        try:
            await send_admin_reports(
                bot, registry, service, event_date,
                raw_assignments or assignments,
                credentials.admin_tg_id, debug_logger, TELEGRAM_TEXT_LIMIT,
            )
        except Exception:  # noqa: BLE001
            debug_logger.exception(
                "Не удалось подготовить отчёт админам служения %s",
                service_id,
            )


async def _process_user_reminder_schedule(
    bot: Bot,
    credentials,
    shm_dir: Path,
    tz,
    registry: ServiceRegistry,
    schedule_id: int,
    interaction_logger,
    debug_logger,
    *,
    run_date: date | None = None,
    state_cache: StateCache | None = None,
    backup_manager: BackupManager | None = None,
) -> None:
    schedule = registry.get_user_reminder_schedule(schedule_id)
    if (
        schedule is None
        or not schedule["schedule_enabled"]
        or not schedule["notifications_enabled"]
        or not schedule["service_enabled"]
    ):
        return
    if schedule["name"] is None:
        debug_logger.debug(
            "Личное расписание %s пропущено: пользователь больше не привязан "
            "к задаче %s",
            schedule_id,
            schedule["task"],
        )
        return
    run_date = run_date or scheduled_reminder_date(
        datetime.now(tz), schedule["reminder_time"]
    )
    event_date = run_date + timedelta(days=schedule["reminder_days"])
    event_iso = event_date.isoformat()
    if registry.is_user_reminder_delivered(schedule_id, event_iso):
        return

    try:
        approved_plan = _has_approved_schedule_for_event(
            registry, schedule["service_id"], event_date
        )
        approved_state = _approved_assignments_state(
            registry, schedule["service_id"], event_date
        )
        if not approved_plan:
            month_plan = registry.get_schedule_plan(
                schedule["service_id"], event_date.strftime("%Y-%m")
            )
            if month_plan is not None and month_plan["approved"] is not None:
                debug_logger.debug(
                    "Личное расписание %s пропущено: в утверждённом "
                    "графике нет события на %s", schedule_id, event_iso,
                )
                return
    except Exception:
        debug_logger.exception(
            "Не удалось проверить утверждённый график для личного "
            "расписания %s",
            schedule_id,
        )
        await _notify_admin(
            bot,
            credentials.admin_tg_id,
            f"Личное напоминание на {event_date:%d.%m.%Y} не отправлено: "
            "не удалось проверить, утверждён ли график в БД бота.",
            debug_logger,
        )
        return
    if not approved_plan:
        interaction_logger.warning(
            "Личное напоминание %s пропущено: график для %s на %s "
            "не утверждён",
            schedule_id, schedule["service_id"], event_date.strftime("%Y-%m"),
        )
        await _notify_unapproved_schedule(
            bot, credentials, registry, schedule["service_id"],
            event_date, debug_logger,
        )
        return

    try:
        assignments, event_found = await _load_service_task_assignments(
            credentials,
            shm_dir,
            tz,
            schedule["service_id"],
            debug_logger,
            only_date=event_date,
            return_event_found=True,
            services=registry.service_catalog(),
            registry=registry,
            backup_manager=backup_manager,
            require_approved_plan=True,
        )
    except Exception:
        debug_logger.exception(
            "Не удалось загрузить назначения для личного расписания %s",
            schedule_id,
        )
        return
    reason = personal_reminder_block_reason(
        schedule, assignments, event_found=event_found, approved=approved_plan,
    )
    if reason is not None:
        debug_logger.debug("Личное расписание %s пропущено: %s", schedule_id, reason)
        return

    service = registry.get_service(schedule["service_id"])
    if service is None or not service["enabled"]:
        return
    template = registry.resolve_text_for_user(
        schedule["actor_id"],
        schedule["service_id"],
        schedule["task"],
        service["personal_templates"],
        body=schedule.get("body"),
    )
    try:
        text = template.format_map(
            _TemplateValues(
                name=display_name(schedule),
                task=schedule["task"],
                service_name=schedule["service_name"],
                sunday_date=event_date.strftime("%d.%m.%Y"),
                telegram_id=str(schedule["actor_id"]),
                username=schedule["username"],
                event_notes=resolve_event_note(
                    registry, schedule["service_id"], event_date,
                    schedule["task"],
                    template=template,
                ),
            )
        )
    except (AttributeError, IndexError, KeyError, ValueError, sqlite3.Error) as exc:
        debug_logger.exception(
            "Некорректный личный текст для расписания %s", schedule_id
        )
        await _notify_admin(
            bot,
            credentials.admin_tg_id,
            f"Личное уведомление для задачи «{schedule['task']}» "
            f"не отправлено: {type(exc).__name__}.",
            debug_logger,
        )
        return
    # Заголовок всегда: к чему относятся «Принял» / «Не смогу», даже если
    # текст напоминания свой.
    header = (
        f"🔔 {schedule['service_name']} · {schedule['task']} · "
        f"{event_date:%d.%m.%Y}"
    )
    body_text = text.removeprefix("🔔").strip()
    text = f"{header}\n{body_text}"
    if _utf16_length(text) > TELEGRAM_TEXT_LIMIT:
        # Поля шаблона раздули текст: напоминание всё равно уходит,
        # сокращённым, — иначе человек остался бы без него.
        while body_text and _utf16_length(f"{header}\n{body_text}…") > TELEGRAM_TEXT_LIMIT:
            body_text = body_text[: max(len(body_text) - 200, 0)]
        text = f"{header}\n{body_text}…"
    if _utf16_length(text) > TELEGRAM_TEXT_LIMIT:
        await _notify_admin(
            bot,
            credentials.admin_tg_id,
            f"Личное уведомление для задачи «{schedule['task']}» "
            f"превышает лимит Telegram и не отправлено.",
            debug_logger,
        )
        return

    # Ответ уже дан («Принял» или «Не смогу») — повторно не напоминаем.
    if registry.get_confirmation(
        schedule["service_id"], event_iso, schedule["task"], schedule["actor_id"]
    ):
        return
    send_kwargs = {
        "reply_markup": build_personal_answer_kb(
            schedule["service_id"], event_date, schedule["task"]
        )
    }
    try:
        still_approved = _has_approved_schedule_for_event(
            registry, schedule["service_id"], event_date
        )
        same_assignments = approved_state == _approved_assignments_state(
            registry, schedule["service_id"], event_date
        )
    except Exception:
        debug_logger.exception(
            "Не удалось повторно проверить утверждённый график для "
            "личного расписания %s",
            schedule_id,
        )
        await _notify_admin(
            bot,
            credentials.admin_tg_id,
            f"Личное напоминание на {event_date:%d.%m.%Y} не отправлено: "
            "не удалось повторно проверить утверждённый график в БД бота.",
            debug_logger,
        )
        return
    if not still_approved:
        await _notify_unapproved_schedule(
            bot, credentials, registry, schedule["service_id"],
            event_date, debug_logger,
        )
        return
    if not same_assignments:
        debug_logger.info(
            "Утверждённые назначения личного расписания %s изменились "
            "во время подготовки — отправка отменена", schedule_id,
        )
        return
    try:
        sent_message = await send_telegram_screen(
            bot.send_message,
            schedule["actor_id"],
            text,
            limit=TELEGRAM_TEXT_LIMIT,
            **send_kwargs,
        )
    except Exception as exc:
        debug_logger.exception(
            "Не удалось отправить личное расписание %s", schedule_id
        )
        await _notify_admin(
            bot,
            credentials.admin_tg_id,
            f"Не удалось отправить личное уведомление для "
            f"{schedule['name']}: {type(exc).__name__}.",
            debug_logger,
        )
        return

    registry.mark_user_reminder_delivered(schedule_id, event_iso)
    interaction_logger.info(
        "Личное уведомление отправлено: service=%s task=%s tg_id=%s "
        "event_date=%s schedule_id=%s",
        schedule["service_id"],
        schedule["task"],
        schedule["actor_id"],
        event_iso,
        schedule_id,
    )


async def _prepare_service_test_previews(
    service_id: str,
    credentials,
    shm_dir: Path,
    tz,
    registry: ServiceRegistry,
    debug_logger,
    group_template: str | None = None,
    personal_templates: dict[str, str] | None = None,
    backup_manager: BackupManager | None = None,
) -> dict:
    sunday_date = _next_sunday_in_tz(tz)
    service = registry.get_service(service_id)
    if service is None:
        raise ValueError(f"Служение '{service_id}' не найдено")
    assignments = await _load_service_task_assignments(
        credentials,
        shm_dir,
        tz,
        service_id,
        debug_logger,
        only_date=sunday_date,
        services=registry.service_catalog(),
        registry=registry,
        backup_manager=backup_manager,
    )
    contacts = registry.list_responsible_contacts(
        credentials.admin_tg_id, service_id
    )
    contacts_by_name = {
        (contact["task"], contact["name"]): contact for contact in contacts
    }
    rendered_assignment_names: dict[str, list[str]] = {}
    personal_messages = []
    issues = []
    sunday_str = sunday_date.strftime("%d.%m.%Y")
    templates = (
        service["personal_templates"]
        if personal_templates is None
        else personal_templates
    )
    for task, task_info in assignments.items():
        if task_is_informational(task_info):
            if any(contact["task"] == task for contact in contacts):
                debug_logger.debug(
                    "Информационная задача исключена из предпросмотра "
                    "привязок: %s",
                    task,
                )
            continue
        names = task_assignment_names(task_info)
        for name in names:
            contact = contacts_by_name.get((task, name))
            if contact is None:
                rendered_assignment_names.setdefault(task, []).append(name)
                issues.append(
                    f"Не привязан Telegram-пользователь: {task} — {name}"
                )
                telegram_id = ""
                username = ""
            else:
                username = contact["username"]
                telegram_id = str(contact["telegram_id"])
                rendered_assignment_names.setdefault(task, []).append(name)
            template = templates.get(
                task,
                "{name}, вы назначены на задачу «{task}» службы "
                "«{service_name}» на {sunday_date}.",
            )
            try:
                personal_text = template.format_map(
                    _TemplateValues(
                        name=display_name(contact, name),
                        task=task,
                        service_name=service["name"],
                        sunday_date=sunday_str,
                        telegram_id=telegram_id,
                        username=username,
                        event_notes=resolve_event_note(
                            registry, service_id, sunday_date, task, template=template,
                        ),
                    )
                )
            except (AttributeError, IndexError, KeyError, ValueError) as exc:
                raise ValueError(
                    f"Некорректный личный шаблон для задачи «{task}» "
                    f"служения «{service['name']}»"
                ) from exc
            personal_messages.append(
                {
                    "task": task,
                    "name": name,
                    "telegram_id": telegram_id,
                    "text": personal_text,
                }
            )
    if not rendered_assignment_names:
        issues.append("В актуальном графике для службы нет назначенных задач.")
    assignment_text = format_task_assignments(
        rendered_assignment_names,
        contacts_by_name,
        mark_unlinked=True,
        display_names=registry.service_display_names(service["id"]),
    )
    effective_group_template = (
        service["group_template"]
        if group_template is None
        else group_template
    ) or (
        "{service_name} — {sunday_date}\n{task_assignments}"
    )
    try:
        group_text = effective_group_template.format_map(
            _TemplateValues(
                service_name=service["name"],
                sunday_date=sunday_str,
                task_assignments=assignment_text,
                informational_notes=format_informational_notes(assignments),
                event_notes=resolve_event_note(
                    registry, service_id, sunday_date, template=effective_group_template,
                ),
            )
        )
    except (AttributeError, IndexError, KeyError, ValueError) as exc:
        raise ValueError(
            f"Некорректный групповой шаблон служения «{service['name']}»"
        ) from exc
    example_group_template = (
        "Напоминание о служении «{service_name}» в воскресенье "
        "{sunday_date}:\n{task_assignments}"
    )
    example_group_text = example_group_template.format_map(
        _TemplateValues(
            service_name=service["name"],
            sunday_date=sunday_str,
            task_assignments=assignment_text,
            informational_notes=format_informational_notes(assignments),
        )
    )
    return {
        "sunday_date": sunday_date,
        "group_text": group_text,
        "example_group_template": example_group_template,
        "example_group_text": example_group_text,
        "personal_messages": personal_messages,
        "issues": issues,
    }


async def _process_upcoming_sunday(
    bot: Bot,
    credentials,
    templates: dict,
    state_cache: StateCache,
    shm_dir: Path,
    interaction_logger,
    debug_logger,
    tz,
    service_registry=None,
    backup_manager: BackupManager | None = None,
) -> None:
    delivery_group_chat_id = credentials.group_chat_id
    if service_registry is not None:
        assembly = service_registry.get_service("assembly")
        if assembly is None:
            raise RuntimeError("В реестре не настроено служение «Собрание»")
        if not assembly["enabled"]:
            interaction_logger.info(
                "Рассылка «Собрания» пропущена: служение отключено в реестре"
            )
            return
        delivery_group_chat_id = assembly["group_chat_id"]
        if delivery_group_chat_id is None:
            await _notify_admin(
                bot,
                credentials.admin_tg_id,
                "Рассылка «Собрания» не выполнена: не задан ID группового чата.",
                debug_logger,
            )
            return

    sunday_date = _next_sunday_in_tz(tz)
    sunday_iso = sunday_date.isoformat()
    if state_cache.matches_week(
        sunday_iso,
        delivery_group_chat_id,
        legacy_group_chat_id=credentials.group_chat_id,
    ):
        interaction_logger.info("Рассылка для %s уже отправлена", sunday_iso)
        return

    directory_fd = open_private_directory(shm_dir, create=True)
    os.close(directory_fd)
    file_fd, file_name = tempfile.mkstemp(
        prefix=".reminder-delivery-", suffix=".xlsx", dir=shm_dir
    )
    os.close(file_fd)
    xlsx_path = Path(file_name)
    debug_logger.info("Подготовка рассылки на %s", sunday_iso)

    try:
        await _download_schedule(
            credentials.yandex_public_url,
            xlsx_path,
            debug_logger,
            backup_manager=backup_manager,
            cache_dir=shm_dir / "schedule-source",
        )
        schedule = parse_schedule(xlsx_path, sunday_date, debug_logger)
        people = parse_people(xlsx_path, debug_logger)
        assignment_issues = [
            f"На {sunday_date:%d.%m.%Y} не найдено назначение для роли «{role}»"
            for role in ROLES
            if role not in schedule
        ]
        assignments = resolve_assignments(
            schedule, people, debug_logger, assignment_issues
        )
    except Exception as exc:
        debug_logger.exception(
            "Не удалось подготовить расписание на %s", sunday_iso
        )
        await _notify_admin(
            bot,
            credentials.admin_tg_id,
            (
                f"Не удалось загрузить или разобрать расписание "
                f"на {sunday_date:%d.%m.%Y}: {type(exc).__name__}: {exc}"
            ),
            debug_logger,
        )
        raise
    finally:
        xlsx_path.unlink(missing_ok=True)

    if assignment_issues:
        await _notify_admin(
            bot,
            credentials.admin_tg_id,
            (
                f"Неполные данные расписания на {sunday_date:%d.%m.%Y}. "
                "Рассылка продолжится для найденных участников. "
                + "; ".join(assignment_issues)
            ),
            debug_logger,
        )

    state = _build_state(assignments, delivery_group_chat_id)
    state["sunday_date"] = sunday_iso
    state = _merge_week_state(
        state_cache.state,
        state,
        delivery_group_chat_id,
        legacy_group_chat_id=credentials.group_chat_id,
    )
    state_cache.replace(state)

    try:
        general_delivery_complete = await send_assignments(
            bot=bot,
            group_chat_id=delivery_group_chat_id,
            admin_id=credentials.admin_tg_id,
            assignments=assignments,
            state=state,
            templates=templates,
            sunday_str=sunday_date.strftime("%d.%m.%Y"),
            sunday_iso=sunday_iso,
            interaction_logger=interaction_logger,
            debug_logger=debug_logger,
            save_state=state_cache.save,
            user_notifications_enabled=(
                (
                    lambda telegram_id: (
                        service_registry.get_user_notifications_enabled(
                            telegram_id, "assembly"
                        )
                    )
                )
                if service_registry is not None
                else None
            ),
            send_personal_notifications=service_registry is None,
        )
        state["complete"] = (
            not assignment_issues
            and general_delivery_complete
            and state.get("general_sent") is True
            and all(
                participant.get("delivered") is True
                or participant.get("notification_skipped") is True
                for participant in state["participants"].values()
                if participant.get("tg_id")
                and not participant.get("is_informational", False)
            )
            and all(
                participant.get("tg_id")
                for participant in state["participants"].values()
                if not participant.get("is_informational", False)
            )
        )
        state_cache.save()
        if state["complete"]:
            interaction_logger.info("Рассылка для %s успешно завершена", sunday_iso)
        else:
            interaction_logger.info(
                "Рассылка для %s частично выполнена; незавершённые отправки "
                "будут повторены при следующем запуске",
                sunday_iso,
            )
    except Exception:
        debug_logger.exception("Не удалось отправить рассылку для %s", sunday_iso)
        raise


async def _run_locked(args: CliArgs) -> None:
    shm_dir = Path(args.shm_dir)
    log_dir = Path(args.log_dir)
    for directory in (shm_dir, log_dir):
        directory_fd = open_private_directory(directory, create=True)
        os.close(directory_fd)

    interaction_logger, debug_logger = setup_loggers(
        log_dir, trace=getattr(args, "trace", False)
    )
    if getattr(args, "trace", False):
        debug_logger.info("Отслеживание навигации (TRACE) включено")
    credentials = load_credentials()
    templates = load_templates()
    tz = get_tz(credentials.timezone)
    try:
        backup_manager = BackupManager(
            environment_name(PROJECT_DIR),
            shm_dir,
            tz,
            debug_logger,
            registry_path=service_registry_path(),
        )
    except RuntimeError as exc:
        debug_logger.warning("Резервирование отключено: %s", exc)
        backup_manager = None
    change_callback = backup_manager.mark_dirty if backup_manager else None
    state_cache = StateCache(
        shm_dir / "reminder_state_cache.json", on_change=change_callback
    )
    service_registry = ServiceRegistry(
        service_registry_path(), on_change=change_callback
    )
    registry_context_group_chat_id = credentials.group_chat_id
    try:
        service_registry.initialize(
            credentials.admin_tg_id, credentials.group_chat_id
        )
        assembly = service_registry.get_service("assembly")
        if assembly and assembly["group_chat_id"] is not None:
            registry_context_group_chat_id = assembly["group_chat_id"]
    except Exception:
        debug_logger.exception(
            "Не удалось инициализировать реестр служений; "
            "продолжаем запуск существующих напоминаний без него"
        )
        service_registry = None
    jobs = load_schedule() if service_registry is None else []
    pid_file = _pid_path(shm_dir)
    bot = None
    scheduler = None
    nightly_backup_task: asyncio.Task | None = None
    schedule_refresh_task: asyncio.Task | None = None
    storage_check_task: asyncio.Task | None = None
    commands_task: asyncio.Task | None = None
    pid_created = False
    active_jobs: set[asyncio.Task[None]] = set()
    service_reminder_locks: dict[tuple[str, date], asyncio.Lock] = {}
    user_reminder_locks: dict[tuple[int, date], asyncio.Lock] = {}

    try:
        bot = Bot(
            token=credentials.bot_token,
            default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        )
        dp = Dispatcher(storage=MemoryStorage())
        context = SimpleNamespace(
            bot=bot,
            group_chat_id=registry_context_group_chat_id,
            admin_id=credentials.admin_tg_id,
            timezone=tz,
            state_cache=state_cache,
            interaction_logger=interaction_logger,
            debug_logger=debug_logger,
            confirmation_locks={},
            service_registry=service_registry,
        )

        async def prepare_test_previews() -> dict:
            return await _prepare_test_notification_previews(
                credentials, templates, shm_dir, tz, debug_logger,
                backup_manager=backup_manager,
            )

        async def load_service_task_assignments(
            service_id: str,
            *,
            force: bool = False,
        ) -> dict[str, dict[str, object]]:
            if not force:
                today = datetime.now(tz).date()
                next_month = (today.replace(day=1) + timedelta(days=32)).replace(day=1)
                end = (next_month + timedelta(days=32)).replace(day=1) - timedelta(days=1)
                events = _approved_service_events(
                    service_registry, credentials.yandex_public_url,
                    today.replace(day=1), end,
                )
                return _service_event_assignments([
                    event for event in events if event.service_id == service_id
                ])
            return await _load_service_task_assignments(
                credentials, shm_dir, tz, service_id, debug_logger,
                services=(
                    service_registry.service_catalog()
                    if service_registry is not None else None
                ),
                registry=service_registry,
                # Сверка из бота — только состав этого служения; плановые
                # загрузки графика обновляют состав всех служений.
                own_roster_only=True,
                backup_manager=backup_manager,
            )

        async def prepare_service_test_previews(
            service_id: str,
            group_template: str | None = None,
            personal_templates: dict[str, str] | None = None,
        ) -> dict:
            if service_registry is None:
                raise ValueError("Реестр настроек служений недоступен")
            return await _prepare_service_test_previews(
                service_id,
                credentials,
                shm_dir,
                tz,
                service_registry,
                debug_logger,
                group_template,
                personal_templates,
                backup_manager=backup_manager,
            )

        async def prepare_group_reminder_preview(
            reminder_id: int,
            body: str | None = None,
        ) -> dict:
            if service_registry is None:
                raise ValueError("Реестр настроек служений недоступен")
            return await get_group_reminder_preview(
                reminder_id,
                credentials,
                shm_dir,
                tz,
                service_registry,
                debug_logger,
                body=body,
                backup_manager=backup_manager,
            )

        context.prepare_test_previews = prepare_test_previews
        context.load_service_task_assignments = load_service_task_assignments
        context.group_preview_state = lambda service_id, event_day: [
            event for event in _approved_service_events(
                service_registry, credentials.yandex_public_url,
                date.fromisoformat(event_day), date.fromisoformat(event_day),
            ) if event.service_id == service_id
        ]
        context.schedule_candidate_version = lambda: source_version(
            credentials.yandex_public_url, shm_dir / "schedule-source"
        )

        upcoming_cache: dict[str, Any] = {
            "loaded_at": None, "events": [], "until": None, "since": None,
        }
        upcoming_lock = asyncio.Lock()

        async def load_upcoming_service_events(
            force: bool = False, until: date | None = None,
            since: date | None = None, with_replacements: bool = True,
            excel_only: bool = False, approved_only: bool | None = None,
            source_out: dict[str, str] | None = None,
        ) -> list:
            # Утверждённый график читается только из БД. Кэш ниже нужен
            # для Excel-кандидатов; force обновляет проверку источника.
            today = datetime.now(tz).date()
            limit = until or today + timedelta(days=UPCOMING_WEEKS_DEFAULT * 7)
            start = min(since or today, today)
            month_start = today.replace(day=1)
            if approved_only is None:
                approved_only = not excel_only
            if approved_only:
                if excel_only:
                    raise ValueError(
                        "Нельзя одновременно запросить Excel-кандидат "
                        "и только утверждённые графики"
                    )
                return _approved_service_events(
                    service_registry, credentials.yandex_public_url, start, limit,
                    with_replacements=with_replacements,
                )

            def window(events: list) -> list:
                if source_out is not None and upcoming_cache.get("digest") is not None:
                    source_out["digest"] = upcoming_cache["digest"]
                selected = [
                    event for event in events
                    if start <= event.event_date <= limit
                ]
                # excel_only — лист как есть («📥 Загрузить» в боте).
                if excel_only:
                    if approved_only:
                        raise ValueError(
                            "Нельзя одновременно запросить Excel-кандидат "
                            "и только утверждённые графики"
                        )
                    return selected
                # Для сводок исключаем месяцы без утверждённого графика.
                # Проверка каждый раз использует актуальное состояние БД;
                # кэш содержит только исходные Excel-события.
                selected = _apply_bot_plans(
                    selected, service_registry, start, limit,
                    approved_only=approved_only,
                )
                # with_replacements=False — график как в файле (замены
                # вызывающий применит сам, например к своему кэшу).
                if not with_replacements:
                    return selected
                return _apply_event_replacements(selected, service_registry)

            async with upcoming_lock:
                loaded_at = upcoming_cache["loaded_at"]
                if (
                    not force
                    and loaded_at is not None
                    and time.monotonic() - loaded_at
                    < UPCOMING_EVENTS_CACHE_SECONDS
                    # После смены месяца кэш может не покрывать окно.
                    and upcoming_cache["until"] is not None
                    and upcoming_cache["until"] >= limit
                    and upcoming_cache.get("since") is not None
                    and upcoming_cache["since"] <= start
                ):
                    # Замены — из базы при каждом чтении: кэш хранит график.
                    return window(upcoming_cache["events"])
                next_month = (today.replace(day=1) + timedelta(days=32)).replace(day=1)
                end_of_next_month = (
                    (next_month + timedelta(days=32)).replace(day=1)
                    - timedelta(days=1)
                )
                loaded_source: dict[str, str] = {}
                events = await _load_upcoming_service_events(
                    credentials, shm_dir, tz, debug_logger,
                    weeks=UPCOMING_WEEKS_DEFAULT,
                    services=(
                        service_registry.service_catalog()
                        if service_registry is not None else None
                    ),
                    until=max(end_of_next_month, limit),
                    since=min(month_start, start),
                    backup_manager=backup_manager,
                    force=force,
                    registry=service_registry,
                    source_out=loaded_source,
                )
                upcoming_cache.update(
                    loaded_at=time.monotonic(), events=events,
                    until=max(end_of_next_month, limit),
                    since=min(month_start, start),
                    digest=loaded_source.get("digest"),
                )
                return window(events)

        context.load_upcoming_service_events = load_upcoming_service_events
        context.prepare_service_test_previews = prepare_service_test_previews
        context.get_group_reminder_preview = prepare_group_reminder_preview

        async def prepare_template_preview(
            service_id: str, kind: str, template: str | None
        ) -> dict:
            if service_registry is None:
                raise ValueError("Реестр настроек служений недоступен")
            return await get_template_preview(
                service_id,
                kind,
                template,
                credentials,
                shm_dir,
                tz,
                service_registry,
                debug_logger,
                backup_manager=backup_manager,
            )

        context.get_template_preview = prepare_template_preview
        register(dp, context)

        if backup_manager is not None:
            async def notify_backup_failure(
                message: str, delivered_ids: set[int],
            ) -> None:
                await _notify_super_admins(
                    bot, service_registry, message, debug_logger,
                    primary_admin_id=credentials.admin_tg_id,
                    delivered_ids=delivered_ids,
                )

            backup_manager.set_failure_callback(notify_backup_failure)

            async def download_backup_schedule(path: Path) -> None:
                await _download_backup_schedule(
                    credentials.yandex_public_url,
                    path,
                    debug_logger,
                    shm_dir=shm_dir,
                    registry=service_registry,
                )

        legacy_registry_notice = {"sent": False}

        async def scheduled_job() -> None:
            task = asyncio.current_task()
            if task is not None:
                active_jobs.add(task)
            try:
                if service_registry is None:
                    debug_logger.error(
                        "Старая рассылка «Собрания» пропущена: не удалось "
                        "проверить утверждённый график в БД бота"
                    )
                    if not legacy_registry_notice["sent"]:
                        legacy_registry_notice["sent"] = await _notify_admin(
                            bot,
                            credentials.admin_tg_id,
                            "Рассылка «Собрания» приостановлена: не удалось "
                            "проверить утверждённый график в БД бота. "
                            "Проверьте журнал и состояние базы.",
                            debug_logger,
                        )
                    return
                await _process_upcoming_sunday(
                    bot=bot,
                    credentials=credentials,
                    templates=templates,
                    state_cache=state_cache,
                    backup_manager=backup_manager,
                    shm_dir=shm_dir,
                    interaction_logger=interaction_logger,
                    debug_logger=debug_logger,
                    tz=tz,
                    service_registry=service_registry,
                )
            finally:
                if task is not None:
                    active_jobs.discard(task)

        scheduler = build_scheduler(
            tz=tz,
            jobs=jobs,
            callback=scheduled_job,
            debug_logger=debug_logger,
        )
        if service_registry is not None:
            async def scheduled_service_reminder(
                service_id: str,
                reminder_id: int,
                scheduled_run_date: date | None = None,
            ) -> None:
                reminder = service_registry.get_group_reminder(reminder_id)
                if reminder is None:
                    debug_logger.warning(
                        "Запланированное групповое напоминание %s не найдено",
                        reminder_id,
                    )
                    return
                run_date = scheduled_run_date or scheduled_reminder_date(
                    datetime.now(tz), reminder["reminder_time"]
                )
                event_date = run_date + timedelta(
                    days=reminder["reminder_days"]
                )
                lock = service_reminder_locks.setdefault(
                    (service_id, event_date), asyncio.Lock()
                )
                task = asyncio.current_task()
                if task is not None:
                    active_jobs.add(task)
                try:
                    async with lock:
                        await _process_service_reminder(
                            bot,
                            credentials,
                            shm_dir,
                            tz,
                            service_registry,
                            service_id,
                            reminder_id,
                            interaction_logger,
                            debug_logger,
                            run_date=run_date,
                            state_cache=state_cache,
                            backup_manager=backup_manager,
                        )
                except Exception:
                    debug_logger.exception(
                        "Ошибка обработки напоминания служения %s",
                        service_id,
                    )
                    await _notify_admin(
                        bot,
                        credentials.admin_tg_id,
                        f"Ошибка обработки напоминания служения "
                        f"«{service_id}». Подробности записаны в журнал.",
                        debug_logger,
                    )
                finally:
                    if task is not None:
                        active_jobs.discard(task)

            async def refresh_service_reminders() -> None:
                if service_registry is None or scheduler is None:
                    return
                # Напоминания с завершённым периодом выключаются (этап 4.2).
                expired = service_registry.disable_expired_group_reminders(
                    datetime.now(tz).date()
                )
                if expired:
                    interaction_logger.info(
                        "Период напоминаний завершён, выключены: %s",
                        ", ".join(map(str, expired)),
                    )
                await add_service_reminder_jobs(
                    scheduler,
                    service_registry.list_services(),
                    scheduled_service_reminder,
                    tz,
                    debug_logger,
                    registry=service_registry,
                )

            context.refresh_service_reminder_jobs = refresh_service_reminders

            async def scheduled_user_reminder(schedule_id: int) -> None:
                schedule = service_registry.get_user_reminder_schedule(
                    schedule_id
                )
                if schedule is None:
                    return
                run_date = scheduled_reminder_date(
                    datetime.now(tz), schedule["reminder_time"]
                )
                event_date = run_date + timedelta(
                    days=schedule["reminder_days"]
                )
                lock = user_reminder_locks.setdefault(
                    (schedule["actor_id"], event_date), asyncio.Lock()
                )
                task = asyncio.current_task()
                if task is not None:
                    active_jobs.add(task)
                try:
                    async with lock:
                        await _process_user_reminder_schedule(
                            bot,
                            credentials,
                            shm_dir,
                            tz,
                            service_registry,
                            schedule_id,
                            interaction_logger,
                            debug_logger,
                            run_date=run_date,
                            state_cache=state_cache,
                            backup_manager=backup_manager,
                        )
                except Exception:
                    debug_logger.exception(
                        "Ошибка обработки личного расписания %s",
                        schedule_id,
                    )
                    await _notify_admin(
                        bot,
                        credentials.admin_tg_id,
                        "Ошибка обработки личного напоминания. "
                        "Подробности записаны в журнал.",
                        debug_logger,
                    )
                finally:
                    if task is not None:
                        active_jobs.discard(task)

            async def refresh_user_reminder_jobs(
                actor_id: int | None = None,
            ) -> None:
                if service_registry is None or scheduler is None:
                    return
                await refresh_user_reminder_schedule_jobs(
                    scheduler,
                    service_registry,
                    scheduled_user_reminder,
                    tz,
                    debug_logger,
                    actor_id=actor_id,
                )

            context.refresh_user_reminder_jobs = refresh_user_reminder_jobs

            previous_registry_callback = service_registry.on_change

            def schedule_user_job_refresh() -> None:
                if previous_registry_callback is not None:
                    previous_registry_callback()
                if scheduler is None or not scheduler.running:
                    return
                refresh_task = asyncio.create_task(
                    refresh_user_reminder_jobs(),
                    name="reminder-bot-refresh-user-reminder-jobs",
                )
                active_jobs.add(refresh_task)
                refresh_task.add_done_callback(active_jobs.discard)

            service_registry.on_change = schedule_user_job_refresh

            async def scheduled_replacement_close(poll_id: str) -> None:
                task = asyncio.current_task()
                if task is not None:
                    active_jobs.add(task)
                try:
                    await context.close_replacement_poll(poll_id)
                except Exception:
                    debug_logger.exception(
                        "Ошибка подведения итога опроса замены %s", poll_id
                    )
                finally:
                    if task is not None:
                        active_jobs.discard(task)

            def schedule_replacement_close(poll_id: str, deadline) -> None:
                """Итог опроса замены по сроку (этап 3.3)."""
                if scheduler is None:
                    return
                scheduler.add_job(
                    scheduled_replacement_close,
                    "date",
                    run_date=max(deadline, datetime.now(tz)),
                    args=[poll_id],
                    id=f"replacement-poll-{poll_id}",
                    replace_existing=True,
                    misfire_grace_time=None,
                )

            def cancel_replacement_close(poll_id: str) -> None:
                if scheduler is None:
                    return
                try:
                    scheduler.remove_job(f"replacement-poll-{poll_id}")
                except JobLookupError:
                    pass

            context.schedule_replacement_close = schedule_replacement_close
            context.cancel_replacement_close = cancel_replacement_close

        try:
            _write_pid(pid_file)
            pid_created = True
        except FileExistsError as exc:
            existing_process = _existing_pid_process_status(pid_file)
            if existing_process is None:
                status = "Содержимое PID-файла не удалось безопасно проверить."
            elif existing_process[1]:
                status = f"Процесс с PID {existing_process[0]} уже запущен."
            else:
                status = (
                    f"PID-файл устарел (PID {existing_process[0]} "
                    "не соответствует живому процессу)."
                )
            message = (
                f"PID-файл уже существует: {pid_file}. {status} "
                "Удалите его вручную, если он устарел."
            )
            debug_logger.error(message)
            raise FileExistsError(message) from exc

        # Меню команд обновляется в фоне: после перезагрузки сервера DNS
        # бывает недоступен минутами, а manage.sh ждёт PID-файл лишь 30 с.
        if callable(getattr(bot, "set_my_commands", None)):
            async def sync_commands_in_background() -> None:
                retries = _retry_delays()
                while True:
                    try:
                        if await sync_admin_commands(
                            bot, service_registry, credentials.admin_tg_id, debug_logger
                        ):
                            return
                    except Exception:
                        debug_logger.exception("Не удалось обновить меню команд")
                        return
                    await anext(retries)

            commands_task = asyncio.create_task(
                sync_commands_in_background(),
                name="reminder-bot-sync-commands",
            )
        if service_registry is not None:
            await refresh_service_reminders()
            # Ночью выключаются напоминания с завершённым периодом (этап 4.2).
            scheduler.add_job(
                refresh_service_reminders,
                "cron",
                hour=0,
                minute=5,
                id="reminder-period-expiry",
                replace_existing=True,
                misfire_grace_time=None,
                coalesce=True,
            )

            month_summary_pause: dict[str, datetime] = {}
            # Утверждение, назначения и пометки перечитываются из БД
            # для каждого получателя, без сетевого обновления.
            waiting_events: dict[str, Any] = {}

            async def month_summary_events(
                *, fresh: bool = False, **kwargs
            ) -> list:
                return await _load_month_summary_events(
                    context.load_upcoming_service_events,
                    service_registry, waiting_events, datetime.now(tz),
                    fresh=fresh, **kwargs,
                    public_url=credentials.yandex_public_url,
                )

            async def month_summary_tick() -> None:
                """Сводка на месяц (этап 4.1): раз в минуту — кому пора.

                После сбоя загрузки графика — пауза 10 минут."""
                now = datetime.now(tz)
                resume_at = month_summary_pause.get("until")
                if resume_at is not None and now < resume_at:
                    return
                month_summary_pause.clear()
                try:
                    run = await send_due_month_summaries(
                        bot, service_registry, month_summary_events, now,
                        debug_logger,
                    )
                except Exception as exc:
                    waiting_events.clear()
                    month_summary_pause["until"] = now + timedelta(minutes=10)
                    debug_logger.warning(
                        "Сводка на месяц отложена на 10 минут: %s: %s",
                        type(exc).__name__, exc, exc_info=True,
                    )
                    return
                if not run.waiting:
                    # Никто не ждёт — следующий раз график читается свежим.
                    forced_at = waiting_events.get("forced_at")
                    waiting_events.clear()
                    if forced_at is not None:
                        waiting_events["forced_at"] = forced_at
                if run.sent:
                    interaction_logger.info(
                        "Сводок на месяц отправлено: %s", run.sent
                    )

            scheduler.add_job(
                month_summary_tick,
                "cron",
                minute="*",
                id="month-summary",
                replace_existing=True,
                misfire_grace_time=None,
                coalesce=True,
                max_instances=1,
            )

            async def readiness_tick() -> None:
                """Опрос готовности (этап 5): превью, запуск, напоминание,
                срок — раз в минуту; после перезапуска досылается."""
                try:
                    await run_readiness_tick(
                        bot, service_registry, datetime.now(tz), tz,
                        credentials.admin_tg_id, debug_logger,
                        TELEGRAM_TEXT_LIMIT,
                        # Прошлый месяц для интервалов предложенного графика.
                        events_loader=context.load_upcoming_service_events,
                    )
                except Exception:
                    debug_logger.exception("Сбой проверки опроса готовности")

            scheduler.add_job(
                readiness_tick,
                "cron",
                minute="*",
                id="readiness",
                replace_existing=True,
                misfire_grace_time=None,
                coalesce=True,
                max_instances=1,
            )
            await refresh_user_reminder_jobs()
            # Открытые опросы замены переживают перезапуск бота.
            _reschedule_open_polls(
                service_registry, schedule_replacement_close, debug_logger
            )
        scheduler.start()
        if backup_manager is not None:
            schedule_refresh_task = asyncio.create_task(
                backup_manager.run_schedule_refresh(download_backup_schedule),
                name="reminder-bot-schedule-refresh",
            )
            storage_check_task = asyncio.create_task(
                backup_manager.run_storage_checks(),
                name="reminder-bot-storage-check",
            )
            nightly_backup_task = asyncio.create_task(
                backup_manager.run_nightly(),
                name="reminder-bot-nightly-backup",
            )
        if not await _wait_for_telegram(bot, debug_logger):
            debug_logger.info("Остановка до подключения к Telegram")
            return
        await _configure_webapp_menu(bot, PROJECT_DIR, debug_logger)
        await dp.start_polling(bot, close_bot_session=False)
    finally:
        for background_task in (
            nightly_backup_task, schedule_refresh_task, storage_check_task, commands_task
        ):
            if background_task is not None:
                background_task.cancel()
                await asyncio.gather(background_task, return_exceptions=True)
        try:
            if scheduler is not None and scheduler.running:
                scheduler.pause()
                await asyncio.sleep(0)
                if active_jobs:
                    _done, pending = await asyncio.wait(
                        tuple(active_jobs), timeout=30,
                    )
                    if pending:
                        for task in pending:
                            task.cancel()
                        await asyncio.gather(*pending, return_exceptions=True)
                scheduler.shutdown(wait=False)
        finally:
            try:
                if backup_manager is not None:
                    await backup_manager.close()
            finally:
                try:
                    if bot is not None:
                        await bot.session.close()
                finally:
                    if pid_created:
                        _remove_pid_if_owned(pid_file)


async def run(args: CliArgs) -> None:
    with application_instance_lock(Path(args.shm_dir)):
        await _run_locked(args)


__all__ = ["run"]
