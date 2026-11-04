import asyncio
import hashlib
import json
import logging
import os
import shutil
import sqlite3
import tempfile
import threading
import unittest
import zipfile
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

from openpyxl import Workbook

from bot.backup import (
    BackupManager,
    MAX_SNAPSHOTS_PER_ENVIRONMENT,
    MIN_RETAINED_SNAPSHOTS,
    STALE_SNAPSHOT_STAGE_AGE,
    application_instance_lock,
    assert_bot_stopped,
)
from bot.excel_parser import _MONTH_SHEETS


# Процессы бота проверяются через /proc — он есть только в Linux (бот
# работает на Raspberry Pi); на macOS эти тесты пропускаются.
NEEDS_PROC = unittest.skipUnless(
    Path("/proc/self/stat").exists(), "нужен /proc (Linux)"
)


class BackupManagerTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.shm_dir = self.root / "shm"
        self.shm_dir.mkdir(mode=0o700)
        self.database = self.root / "registry.sqlite3"
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("BEGIN")
            connection.execute(
                "CREATE TABLE settings (name TEXT PRIMARY KEY, value TEXT)"
            )
            connection.execute("INSERT INTO settings VALUES ('mode', 'original')")
            connection.commit()
        self.database.chmod(0o600)
        self.xlsx = self.shm_dir / "reminder_bot_current.xlsx"
        self._write_workbook(self.xlsx)
        self.cache = self.shm_dir / "reminder_state_cache.json"
        self.cache.write_text(
            json.dumps({"sunday_date": "2030-01-06"}),
            encoding="utf-8",
        )
        self.cache.chmod(0o600)
        self.backup_base = self.root / "external" / "reminder_bot" / "backups"
        self.manager = BackupManager(
            "test",
            self.shm_dir,
            ZoneInfo("Europe/Moscow"),
            logging.getLogger("test.backup"),
            backup_base=self.backup_base,
            mount_point=self.root / "external",
            require_mount=False,
            registry_path=self.database,
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    @staticmethod
    def _write_workbook(path):
        now = datetime.now(ZoneInfo("Europe/Moscow")).date()
        until_sunday = (6 - now.weekday()) % 7
        target = now + timedelta(days=until_sunday or 7)
        workbook = Workbook()
        schedule = workbook.active
        schedule.title = f"{_MONTH_SHEETS[target.month - 1]}{target.year % 100:02d}"
        schedule.append([target])
        schedule.append(["ХКЦ", "Алексей"])
        schedule.append(["Ведущий", "Павел"])
        schedule.append(["Проповедует", "Мария"])
        contacts = workbook.create_sheet("Списки")
        contacts.append(["ХКЦ"])
        contacts.append(["Алексей", "alex", 101])
        contacts.append(["Ведущий"])
        contacts.append(["Павел", "pavel", 102])
        contacts.append(["Проповедует"])
        contacts.append(["Мария", "maria", 103])
        workbook.save(path)
        path.chmod(0o600)

    def test_snapshot_contains_verified_private_restore_set(self):
        snapshot = asyncio.run(self.manager.create_snapshot("test"))
        manifest = json.loads((snapshot / "manifest.json").read_text("utf-8"))
        self.assertEqual(manifest["environment"], "test")
        self.assertEqual(
            set(manifest["files"]),
            {
                "database.sqlite3",
                "database.sql",
                "reminder_state_cache.json",
                "schedule.xlsx",
                "schedule.json",
            },
        )
        self.assertEqual((snapshot.stat().st_mode & 0o777), 0o700)
        for filename in manifest["files"]:
            self.assertEqual((snapshot / filename).stat().st_mode & 0o777, 0o600)
        data = json.loads((snapshot / "schedule.json").read_text("utf-8"))
        self.assertEqual(data["assignments"]["Ответственный"], "Алексей")
        self.assertEqual(data["contacts"]["Ответственный"]["Алексей"]["telegram_id"], 101)

    def test_download_preserves_private_copy_and_supports_backup_after_ram_loss(self):
        original = self.xlsx.read_bytes()
        asyncio.run(self.manager.record_schedule_download(self.xlsx))
        saved = self.manager.saved_schedule
        self.assertEqual(saved.read_bytes(), original)
        self.assertEqual(saved.stat().st_mode & 0o777, 0o600)
        self.xlsx.unlink()
        snapshot = asyncio.run(self.manager.create_snapshot("RAM cleared"))
        self.assertEqual((snapshot / "schedule.xlsx").read_bytes(), original)
        manifest = json.loads((snapshot / "manifest.json").read_text())
        self.assertIn("schedule_saved_at", manifest)
        self.assertFalse(self.xlsx.exists())

    def test_old_saved_workbook_can_back_up_database_after_month_changes(self):
        asyncio.run(self.manager.record_schedule_download(self.xlsx))
        saved_time = self.manager.saved_schedule.stat().st_mtime
        self.xlsx.unlink()
        future = datetime.now(self.manager.timezone) + timedelta(days=62)
        with patch("bot.backup.datetime", wraps=datetime) as clock:
            clock.now.return_value = future
            snapshot = asyncio.run(self.manager.create_snapshot("offline next month"))
        manifest = json.loads((snapshot / "manifest.json").read_text())
        self.assertAlmostEqual(
            datetime.fromisoformat(manifest["schedule_saved_at"]).timestamp(),
            saved_time,
            places=5,
        )
        self.assertEqual(manifest["created_at"], future.isoformat())

    def test_bad_download_never_replaces_last_good_copy(self):
        asyncio.run(self.manager.record_schedule_download(self.xlsx))
        original = self.manager.saved_schedule.read_bytes()
        bad = self.shm_dir / "invalid.xlsx"
        bad.write_bytes(b"not an Excel file")
        with self.assertRaises(zipfile.BadZipFile):
            asyncio.run(self.manager.record_schedule_download(bad))
        self.assertEqual(self.manager.saved_schedule.read_bytes(), original)
        self.assertEqual(self.xlsx.read_bytes(), original)
        self.assertFalse(list(self.shm_dir.glob(".schedule-check-*")))

    def test_interrupted_schedule_publish_keeps_previous_copy(self):
        asyncio.run(self.manager.record_schedule_download(self.xlsx))
        original = self.manager.saved_schedule.read_bytes()
        with patch("bot.backup.os.replace", side_effect=OSError("write failed")):
            with self.assertRaises(OSError):
                asyncio.run(self.manager.record_schedule_download(self.xlsx))
        self.assertEqual(self.manager.saved_schedule.read_bytes(), original)
        self.assertFalse(list(self.manager.saved_schedule.parent.glob(".schedule-*")))

    def test_missing_external_mount_prevents_schedule_copy(self):
        self.manager.require_mount = True
        with patch.object(Path, "is_mount", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "не смонтирован"):
                asyncio.run(self.manager.record_schedule_download(self.xlsx))
        self.assertFalse(self.manager.saved_schedule.exists())

    def test_cancelled_download_waits_for_file_write_before_releasing_lock(self):
        async def scenario():
            started = threading.Event()
            release = threading.Event()

            def write():
                started.set()
                if not release.wait(5):
                    raise TimeoutError("test did not release writer")

            with patch.object(self.manager, "_record_schedule_download", side_effect=lambda _: write()):
                task = asyncio.create_task(
                    self.manager.record_schedule_download(self.xlsx)
                )
                try:
                    self.assertTrue(await asyncio.to_thread(started.wait, 5))
                    task.cancel()
                    await asyncio.sleep(0)
                    self.assertTrue(self.manager._backup_lock.locked())
                    self.assertFalse(task.done())
                finally:
                    release.set()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                self.assertFalse(self.manager._backup_lock.locked())

        asyncio.run(scenario())

    def test_saved_copy_is_environment_specific_and_symlinks_are_rejected(self):
        asyncio.run(self.manager.record_schedule_download(self.xlsx))
        original = self.xlsx.read_bytes()
        saved = self.manager.saved_schedule
        saved.unlink()
        saved.symlink_to(self.xlsx)
        with self.assertRaises(PermissionError):
            asyncio.run(self.manager.record_schedule_download(self.xlsx))
        self.assertEqual(self.xlsx.read_bytes(), original)
        self.assertFalse((self.backup_base / "prod" / "latest-schedule.xlsx").exists())
        self.xlsx.unlink()
        with self.assertRaises(RuntimeError):
            asyncio.run(self.manager.create_snapshot("broken link"))

    def test_refresh_retries_then_saves_and_switches_to_daily_interval(self):
        async def scenario():
            good = self.xlsx.read_bytes()
            notices = AsyncMock()
            self.manager.set_failure_callback(notices)
            calls = 0

            async def download(path):
                nonlocal calls
                calls += 1
                if calls <= 2:
                    raise OSError("network unavailable")
                path.write_bytes(good)

            delays = []

            async def sleep(delay):
                delays.append(delay)
                if len(delays) == 3:
                    raise asyncio.CancelledError

            self.xlsx.unlink()
            with patch("bot.backup.asyncio.sleep", side_effect=sleep):
                with self.assertRaises(asyncio.CancelledError):
                    await self.manager.run_schedule_refresh(download)
            self.assertEqual(delays, [300, 300, 86400])
            self.assertEqual(self.xlsx.read_bytes(), good)
            self.assertEqual(self.manager.saved_schedule.read_bytes(), good)
            schedule_notices = [
                call for call in notices.await_args_list
                if "Не удалось обновить Excel" in call.args[0]
            ]
            self.assertEqual(len(schedule_notices), 1)
            self.assertFalse(list(self.shm_dir.glob(".schedule-refresh-*")))
            await self.manager.close()

        asyncio.run(scenario())

    def test_storage_thresholds_and_notice_deduplication(self):
        usage = shutil._ntuple_diskusage
        gib = 1024 ** 3
        self.assertEqual(
            self.manager._storage_level(usage(10*gib, 9*gib, gib)), "warning"
        )
        self.assertEqual(
            self.manager._storage_level(usage(20*gib, 19*gib, gib)), "critical"
        )
        self.assertEqual(
            self.manager._storage_level(usage(gib, 800*1024**2, 224*1024**2)),
            "critical",
        )
        self.assertEqual(
            self.manager._storage_level(usage(10*gib, 8*gib, 2*gib)), "ok"
        )

        async def scenario():
            notices = AsyncMock()
            self.manager.set_failure_callback(notices)
            for free in (gib, gib, gib // 2, gib // 2, 3*gib, gib):
                with patch(
                    "bot.backup.shutil.disk_usage",
                    return_value=usage(10*gib, 10*gib-free, free),
                ):
                    await self.manager.check_storage()
            # Two paths: warn once, escalate once, then warn after recovery.
            self.assertEqual(notices.await_count, 6)

        asyncio.run(scenario())

    def test_failed_notice_is_retried_until_delivery_then_deduplicated(self):
        async def scenario():
            notices = AsyncMock(side_effect=[RuntimeError("offline"), None])
            self.manager.set_failure_callback(notices)
            for _ in range(3):
                await self.manager.report_failure("schedule", "failed download")
            self.assertEqual(notices.await_count, 2)
            self.assertIn("schedule", self.manager._failure_notices)

        asyncio.run(scenario())

    def test_disk_pressure_prunes_only_valid_snapshots_and_keeps_seven(self):
        snapshot = asyncio.run(self.manager.create_snapshot("seed"))
        paths = [snapshot]
        for index in range(9):
            path = snapshot.parent / f"copy-{index}"
            shutil.copytree(snapshot, path)
            paths.append(path)
        invalid = snapshot.parent / "invalid"
        shutil.copytree(snapshot, invalid)
        (invalid / "database.sql").write_bytes(b"corrupt")
        usage = shutil._ntuple_diskusage(1024**3, 1000*1024**2, 24*1024**2)
        with patch("bot.backup.shutil.disk_usage", return_value=usage):
            self.manager._prune_for_space(snapshot.parent)
        self.assertEqual(sum(path.exists() for path in paths), 7)
        self.assertTrue(invalid.exists())

    def test_snapshot_parses_and_hashes_the_staged_workbook(self):
        from datetime import date
        from unittest.mock import patch

        observed_paths = []
        parsed_workbook_bytes = []

        def parse_staged(path):
            observed_paths.append(path)
            parsed_workbook_bytes.append(path.read_bytes())
            self.xlsx.write_bytes(b"live workbook changed during snapshot")
            return date(2030, 1, 6), {"Ответственный": "Алексей"}, {}

        with patch.object(self.manager, "_parse_schedule", side_effect=parse_staged):
            snapshot = asyncio.run(self.manager.create_snapshot("race test"))
        workbook = snapshot / "schedule.xlsx"
        schedule = json.loads((snapshot / "schedule.json").read_text("utf-8"))
        self.assertEqual(len(observed_paths), 1)
        self.assertEqual(observed_paths[0].name, "schedule.xlsx")
        self.assertEqual(parsed_workbook_bytes, [workbook.read_bytes()])
        self.assertEqual(
            schedule["source_sha256"],
            hashlib.sha256(workbook.read_bytes()).hexdigest(),
        )
        self.assertEqual(self.xlsx.read_bytes(), b"live workbook changed during snapshot")

    def test_prunes_only_old_valid_snapshots_after_commit(self):
        snapshot = asyncio.run(self.manager.create_snapshot("retention test"))
        backup_dir = snapshot.parent
        first_created_at = datetime(2020, 1, 1, tzinfo=ZoneInfo("UTC"))

        def set_created_at(path, created_at):
            manifest_path = path / "manifest.json"
            manifest = json.loads(manifest_path.read_text("utf-8"))
            manifest["created_at"] = created_at.isoformat()
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False),
                encoding="utf-8",
            )
            manifest_path.chmod(0o600)

        set_created_at(snapshot, first_created_at)
        valid_snapshots = [snapshot]
        for index in range(1, MAX_SNAPSHOTS_PER_ENVIRONMENT + 3):
            candidate = backup_dir / f"snapshot-{index:02}"
            shutil.copytree(snapshot, candidate)
            set_created_at(candidate, first_created_at + timedelta(days=index))
            valid_snapshots.append(candidate)

        committed_snapshot = valid_snapshots[-1]
        invalid_snapshot = backup_dir / "invalid-snapshot"
        shutil.copytree(snapshot, invalid_snapshot)
        with (invalid_snapshot / "database.sql").open("ab") as stream:
            stream.write(b"-- tampered\n")

        with self.assertLogs("test.backup", level="WARNING"):
            self.manager._prune_old_snapshots(backup_dir, committed_snapshot)

        remaining_valid = [
            path
            for path in valid_snapshots
            if path.exists()
        ]
        self.assertEqual(len(remaining_valid), MAX_SNAPSHOTS_PER_ENVIRONMENT)
        self.assertGreaterEqual(len(remaining_valid), MIN_RETAINED_SNAPSHOTS)
        self.assertTrue(committed_snapshot.exists())
        self.assertTrue(invalid_snapshot.exists())
        self.assertFalse(valid_snapshots[0].exists())
        self.assertFalse(valid_snapshots[1].exists())
        self.assertFalse(valid_snapshots[2].exists())

    def test_same_size_corruption_does_not_displace_usable_snapshot(self):
        committed = asyncio.run(self.manager.create_snapshot("retention test"))
        backup_dir = committed.parent
        valid_snapshots = [committed]
        origin = datetime(2020, 1, 1, tzinfo=ZoneInfo("UTC"))

        for index in range(1, MAX_SNAPSHOTS_PER_ENVIRONMENT):
            candidate = backup_dir / f"snapshot-valid-{index:02}"
            shutil.copytree(committed, candidate)
            manifest_path = candidate / "manifest.json"
            manifest = json.loads(manifest_path.read_text("utf-8"))
            manifest["created_at"] = (
                origin + timedelta(days=index)
            ).isoformat()
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            manifest_path.chmod(0o600)
            valid_snapshots.append(candidate)

        corrupted = backup_dir / "snapshot-corrupted"
        shutil.copytree(committed, corrupted)
        manifest_path = corrupted / "manifest.json"
        manifest = json.loads(manifest_path.read_text("utf-8"))
        manifest["created_at"] = (
            origin + timedelta(days=MAX_SNAPSHOTS_PER_ENVIRONMENT)
        ).isoformat()
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        manifest_path.chmod(0o600)
        target = corrupted / "database.sql"
        original = target.read_bytes()
        target.write_bytes(
            bytes([original[0] ^ 1]) + original[1:]
        )
        self.assertEqual(target.stat().st_size, len(original))

        with self.assertLogs("test.backup", level="WARNING"):
            self.manager._prune_old_snapshots(backup_dir, committed)

        self.assertTrue(all(path.exists() for path in valid_snapshots))
        self.assertTrue(corrupted.exists())

    def test_pruning_reuses_validation_for_unchanged_snapshots(self):
        from unittest.mock import patch

        committed = asyncio.run(self.manager.create_snapshot("retention test"))
        backup_dir = committed.parent
        for index in range(MAX_SNAPSHOTS_PER_ENVIRONMENT):
            shutil.copytree(
                committed, backup_dir / f"snapshot-copy-{index:02}"
            )
        self.manager._prune_old_snapshots(backup_dir, committed)

        next_snapshot = backup_dir / "snapshot-next"
        shutil.copytree(committed, next_snapshot)
        manifest_path = next_snapshot / "manifest.json"
        manifest = json.loads(manifest_path.read_text("utf-8"))
        manifest["created_at"] = (
            datetime.now(ZoneInfo("UTC")) + timedelta(seconds=1)
        ).isoformat()
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        manifest_path.chmod(0o600)
        next_manifest = self.manager._validate_snapshot(
            next_snapshot, self.manager.environment
        )

        with patch.object(
            self.manager,
            "_validate_snapshot",
            wraps=self.manager._validate_snapshot,
        ) as validate:
            self.manager._prune_old_snapshots(
                backup_dir, next_snapshot, next_manifest
            )

        validate.assert_not_called()

    def test_prunes_only_abandoned_staging_directories_older_than_cutoff(self):
        backup_dir = self.backup_base / "test"
        backup_dir.mkdir(parents=True, mode=0o700)
        stale_stage = backup_dir / ".snapshot-abandoned"
        stale_stage.mkdir(mode=0o700)
        recent_stage = backup_dir / ".snapshot-active"
        recent_stage.mkdir(mode=0o700)
        stale_time = datetime.now().timestamp() - (
            STALE_SNAPSHOT_STAGE_AGE.total_seconds() + 60
        )
        os.utime(stale_stage, (stale_time, stale_time))

        self.manager._prune_abandoned_stages(backup_dir)

        self.assertFalse(stale_stage.exists())
        self.assertTrue(recent_stage.exists())

    @NEEDS_PROC
    def test_restore_replaces_database_schedule_and_state(self):
        snapshot = asyncio.run(self.manager.create_snapshot("test"))
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("BEGIN")
            connection.execute("UPDATE settings SET value='changed' WHERE name='mode'")
            connection.commit()
        self.cache.write_text(json.dumps({"sunday_date": "2030-02-03"}), "utf-8")
        self.cache.chmod(0o600)
        self.xlsx.write_bytes(b"changed workbook")

        self.manager.restore_snapshot(snapshot)

        with closing(sqlite3.connect(self.database)) as connection:
            value = connection.execute(
                "SELECT value FROM settings WHERE name='mode'"
            ).fetchone()[0]
        self.assertEqual(value, "original")
        self.assertEqual(json.loads(self.cache.read_text("utf-8"))["sunday_date"], "2030-01-06")
        self.assertTrue(self.xlsx.read_bytes().startswith(b"PK"))

    @NEEDS_PROC
    def test_restore_refuses_leftover_rollback_journal(self):
        snapshot = asyncio.run(self.manager.create_snapshot("test"))
        journal = Path(f"{self.database}-journal")
        journal.write_bytes(b"hot journal")
        try:
            with self.assertRaisesRegex(RuntimeError, "sidecar"):
                self.manager.restore_snapshot(snapshot)
        finally:
            journal.unlink()

    def test_restore_cannot_overlap_a_running_instance(self):
        snapshot = asyncio.run(self.manager.create_snapshot("test"))

        with application_instance_lock(self.shm_dir):
            with self.assertRaisesRegex(RuntimeError, "уже выполняется"):
                self.manager.restore_snapshot(snapshot)

    @NEEDS_PROC
    def test_rejects_tampered_snapshot_before_restore(self):
        snapshot = asyncio.run(self.manager.create_snapshot("test"))
        with (snapshot / "database.sql").open("ab") as stream:
            stream.write(b"-- tampered\n")
        with self.assertRaisesRegex(RuntimeError, "SHA-256"):
            self.manager.restore_snapshot(snapshot)

    @NEEDS_PROC
    def test_rejects_snapshot_from_another_environment(self):
        snapshot = asyncio.run(self.manager.create_snapshot("test"))
        prod_manager = BackupManager(
            "prod",
            self.shm_dir,
            ZoneInfo("Europe/Moscow"),
            logging.getLogger("test.backup"),
            backup_base=self.backup_base,
            mount_point=self.root / "external",
            require_mount=False,
            registry_path=self.database,
        )
        with self.assertRaisesRegex(ValueError, "своего окружения"):
            prod_manager.restore_snapshot(snapshot)

    def test_requires_external_disk_mount_for_production_snapshot(self):
        manager = BackupManager(
            "prod",
            self.shm_dir,
            ZoneInfo("Europe/Moscow"),
            logging.getLogger("test.backup"),
            backup_base=self.backup_base,
            mount_point=self.root / "not-mounted",
            registry_path=self.database,
        )
        with self.assertRaisesRegex(RuntimeError, "не смонтирован"):
            manager._create_snapshot()


class BackupDebounceTest(unittest.IsolatedAsyncioTestCase):
    async def test_new_change_resets_quiet_period(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = BackupManager(
                "test",
                Path(tmp) / "shm",
                ZoneInfo("Europe/Moscow"),
                logging.getLogger("test.backup.debounce"),
                backup_base=Path(tmp) / "backups",
                mount_point=Path(tmp),
                require_mount=False,
                debounce_seconds=0.03,
            )
            manager._run_background_backup = AsyncMock()
            manager.mark_dirty()
            await asyncio.sleep(0.015)
            manager.mark_dirty()
            await asyncio.sleep(0.04)
            self.assertEqual(manager._run_background_backup.await_count, 1)

    async def test_close_cancels_debounce_and_waits_for_started_tasks(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = BackupManager(
                "test",
                Path(tmp) / "shm",
                ZoneInfo("Europe/Moscow"),
                logging.getLogger("test.backup.close"),
                backup_base=Path(tmp) / "backups",
                mount_point=Path(tmp),
                require_mount=False,
                debounce_seconds=60,
            )
            manager._run_background_backup = AsyncMock()
            manager.mark_dirty()
            handle = manager._debounce_handle
            task = manager.start_background_backup("active")
            await manager.close()
            self.assertTrue(handle.cancelled())
            manager._run_background_backup.assert_awaited_once_with("active")
            self.assertTrue(task.done())


    async def test_background_backup_cancels_pending_debounce(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = BackupManager(
                "test",
                Path(tmp) / "shm",
                ZoneInfo("Europe/Moscow"),
                logging.getLogger("test.backup.nightly"),
                backup_base=Path(tmp) / "backups",
                mount_point=Path(tmp),
                require_mount=False,
                debounce_seconds=60,
            )
            manager._create_snapshot = MagicMock(return_value=Path(tmp))
            manager.mark_dirty()
            handle = manager._debounce_handle

            await manager._run_background_backup("ночное")

            self.assertTrue(handle.cancelled())
            self.assertIsNone(manager._debounce_handle)
            manager._create_snapshot.assert_called_once_with()

    async def test_failed_background_backup_rearms_debounce(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = BackupManager(
                "test",
                Path(tmp) / "shm",
                ZoneInfo("Europe/Moscow"),
                logging.getLogger("test.backup.retry"),
                backup_base=Path(tmp) / "backups",
                mount_point=Path(tmp),
                require_mount=False,
                debounce_seconds=60,
            )
            manager._create_snapshot = MagicMock(side_effect=OSError("disk"))
            manager.mark_dirty()
            handle = manager._debounce_handle

            await manager._run_background_backup("ночное")

            self.assertTrue(handle.cancelled())
            retry = manager._debounce_handle
            self.assertIsNotNone(retry)
            self.assertIsNot(retry, handle)

            await manager._run_background_backup("повтор")

            self.assertTrue(retry.cancelled())
            self.assertIsNone(manager._debounce_handle)
            await manager.close()


class RestoreProcessGuardTest(unittest.TestCase):
    def test_rejects_malformed_pid_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shm = root / "shm"
            shm.mkdir(mode=0o700)
            pid_file = shm / "reminder_bot_bot.pid"
            pid_file.write_text("not-a-pid", "ascii")
            pid_file.chmod(0o600)
            with self.assertRaisesRegex(RuntimeError, "неверный формат"):
                assert_bot_stopped(root, shm)

    def test_rejects_unsafe_pid_file_permissions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shm = root / "shm"
            shm.mkdir(mode=0o700)
            pid_file = shm / "reminder_bot_bot.pid"
            pid_file.write_text("999999999:1", "ascii")
            pid_file.chmod(0o644)
            with self.assertRaisesRegex(RuntimeError, "небезопасен"):
                assert_bot_stopped(root, shm)

    @NEEDS_PROC
    def test_rejects_pid_file_for_live_process(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shm = root / "shm"
            shm.mkdir(mode=0o700)
            stat_fields = (
                Path("/proc/self/stat")
                .read_text("ascii")
                .rsplit(")", 1)[1]
                .split()
            )
            pid_file = shm / "reminder_bot_bot.pid"
            pid_file.write_text(
                f"{os.getpid()}:{stat_fields[19]}",
                "ascii",
            )
            pid_file.chmod(0o600)
            with self.assertRaisesRegex(RuntimeError, "ещё работает"):
                assert_bot_stopped(root, shm)


if __name__ == "__main__":
    unittest.main()
