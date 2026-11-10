import asyncio
import hashlib
import importlib
import logging
import os
import stat
import tempfile
import unittest
from contextlib import ExitStack
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo


def group_sends(bot):
    """Отправки в группы (отрицательный chat_id), без отчётов админам."""
    return [
        call for call in bot.send_message.await_args_list
        if isinstance(call.args[0], int) and call.args[0] < 0
    ]


# Процессы бота проверяются через /proc — он есть только в Linux (бот
# работает на Raspberry Pi); на macOS эти тесты пропускаются.
NEEDS_PROC = unittest.skipUnless(
    Path("/proc/self/stat").exists(), "нужен /proc (Linux)"
)


class ReminderBotEntrypointTest(unittest.TestCase):
    def setUp(self):
        from bot import app

        self._approved_schedule = patch.object(
            app, "_has_approved_schedule_for_event", return_value=True
        )
        self._approved_schedule.start()

    def tearDown(self):
        self._approved_schedule.stop()

    def test_backup_schedule_download_uses_configured_yandex_retries(self):
        from bot import app
        from bot.yandex_disk import RETRY_ATTEMPTS, RETRY_INTERVAL

        async def download():
            path = Path("schedule.xlsx")
            logger = MagicMock()
            with patch.object(
                app, "download_public", new=AsyncMock()
            ) as download_public:
                await app._download_backup_schedule(
                    "https://example.invalid/schedule", path, logger
                )
            download_public.assert_awaited_once_with(
                "https://example.invalid/schedule",
                path,
                logger,
                attempts=RETRY_ATTEMPTS,
                interval=RETRY_INTERVAL,
            )

        asyncio.run(download())

    def test_backup_failure_notifies_only_super_admins(self):
        from bot import app

        async def notify():
            registry = SimpleNamespace(
                list_admin_ids=lambda: {1001: True, 2002: False, 3003: True}
            )
            with patch.object(
                app, "_notify_admin", new=AsyncMock(return_value=True)
            ) as send_notice:
                await app._notify_super_admins(
                    SimpleNamespace(),
                    registry,
                    "Ошибка обновления",
                    MagicMock(),
                )
            self.assertEqual(
                [call.args[1] for call in send_notice.await_args_list],
                [1001, 3003],
            )

        asyncio.run(notify())

    def test_backup_failure_uses_primary_admin_if_registry_is_unavailable(self):
        from bot import app
        import sqlite3

        async def notify():
            for registry in (
                None,
                SimpleNamespace(list_admin_ids=MagicMock(
                    side_effect=sqlite3.OperationalError("database unavailable")
                )),
            ):
                with patch.object(
                    app, "_notify_admin", new=AsyncMock(return_value=True)
                ) as send_notice:
                    await app._notify_super_admins(
                        SimpleNamespace(), registry, "Ошибка", MagicMock(),
                        primary_admin_id=1001,
                    )
                self.assertEqual(
                    [call.args[1] for call in send_notice.await_args_list],
                    [1001],
                )

        asyncio.run(notify())

    @staticmethod
    def approve_plan(registry, service_id, event_date, task, name):
        slots = {event_date.isoformat(): {task: [name]}}
        month = event_date.strftime("%Y-%m")
        registry.save_schedule_draft(
            service_id, month, run_id=None, dates=list(slots),
            tasks=[{"name": task, "needed": 1}], slots=slots, last_served={},
        )
        registry.approve_schedule_plan(
            1001, service_id, month, slots, chat_id=None, message_id=None,
        )

    def test_reminders_reject_replaced_approved_assignments_during_load(self):
        from bot import app
        from bot.service_registry import ServiceRegistry

        async def process():
            for personal in (False, True):
                with tempfile.TemporaryDirectory() as tmp_dir:
                    registry = ServiceRegistry(Path(tmp_dir) / "registry.sqlite3")
                    registry.initialize(1001, -10001)
                    registry.set_group_chat(1001, "sound", -10055, enabled=True)
                    registry.set_responsible_contact(
                        1001, "sound", "Микшер", "Алексей", 2002
                    )
                    registry.set_user_notifications_enabled(2002, "sound", True)
                    text_id = registry.ensure_user_text(2002, "sound", "Микшер")
                    schedule = registry.add_schedule(text_id, 1, "18:00")
                    event_date = date(2026, 10, 18)
                    self.approve_plan(
                        registry, "sound", event_date, "Микшер", "Алексей"
                    )
                    reminder_id = registry.add_group_reminder(
                        1001, "sound", 1, "18:00", "{task_assignments}",
                        allow_duplicate=True,
                    )
                    bot = SimpleNamespace(send_message=AsyncMock())
                    logger = MagicMock()

                    async def load(*args, **kwargs):
                        self.approve_plan(
                            registry, "sound", event_date, "Микшер", "Мария"
                        )
                        return {"Микшер": ["Алексей"]}, True

                    with patch.object(
                        app, "_load_service_task_assignments", side_effect=load,
                    ):
                        if personal:
                            await app._process_user_reminder_schedule(
                                bot, SimpleNamespace(admin_tg_id=1001),
                                Path(tmp_dir), ZoneInfo("UTC"), registry,
                                schedule["id"], logger, logger,
                                run_date=event_date - timedelta(days=1),
                            )
                        else:
                            await app._process_service_reminder(
                                bot, SimpleNamespace(admin_tg_id=1001),
                                Path(tmp_dir), ZoneInfo("UTC"), registry,
                                "sound", reminder_id, logger, logger,
                                run_date=event_date - timedelta(days=1),
                            )
                    self.assertFalse(group_sends(bot))
                    self.assertFalse(any(
                        call.args[0] == 2002
                        for call in bot.send_message.await_args_list
                    ))
                    self.assertFalse(registry.is_user_reminder_delivered(
                        schedule["id"], event_date.isoformat(),
                    ))
                    self.assertFalse(registry.is_group_reminder_delivered(
                        reminder_id, event_date.isoformat(), "group:-10055",
                    ))

        asyncio.run(process())

    def test_month_summary_cache_rechecks_current_approved_assignments(self):
        from bot import app
        from bot.service_registry import ServiceRegistry
        import sqlite3

        async def load():
            with tempfile.TemporaryDirectory() as tmp_dir:
                registry = ServiceRegistry(Path(tmp_dir) / "registry.sqlite3")
                registry.initialize(1001, -10001)
                event_date = date(2026, 10, 18)
                self.approve_plan(
                    registry, "sound", event_date, "Микшер", "Алексей"
                )
                events, _ = app._bot_plan_events(
                    registry, event_date, event_date, service_id="sound"
                )
                loader = AsyncMock(return_value=events)
                cache = {}
                now = datetime(2026, 10, 10, 18, tzinfo=ZoneInfo("UTC"))
                kwargs = dict(since=date(2026, 10, 1), until=date(2026, 10, 31))
                initial = await app._load_month_summary_events(
                    loader, registry, cache, now, **kwargs
                )
                self.assertEqual(initial[0].assignments[0].name, "Алексей")
                self.approve_plan(
                    registry, "sound", event_date, "Микшер", "Мария"
                )
                changed = await app._load_month_summary_events(
                    loader, registry, cache, now + timedelta(minutes=1), **kwargs
                )
                self.assertEqual(changed[0].assignments[0].name, "Мария")
                with patch.object(
                    registry, "get_schedule_plan",
                    side_effect=sqlite3.OperationalError("unavailable"),
                ), self.assertRaises(sqlite3.OperationalError):
                    await app._load_month_summary_events(
                        loader, registry, cache, now + timedelta(minutes=2),
                        **kwargs,
                    )
                with patch.object(registry, "get_schedule_plan", return_value=None):
                    withdrawn = await app._load_month_summary_events(
                        loader, registry, cache, now + timedelta(minutes=3),
                        **kwargs,
                    )
                self.assertEqual(withdrawn, [])
                loader.assert_awaited_once()

        asyncio.run(load())

    def test_approved_delivery_and_summary_read_db_without_network(self):
        from bot import app
        from bot.service_registry import ServiceRegistry
        import hashlib
        import sqlite3

        async def process():
            with tempfile.TemporaryDirectory() as tmp_dir:
                root = Path(tmp_dir)
                registry = ServiceRegistry(root / "registry.sqlite3")
                registry.initialize(1001, -10001)
                day = date(2026, 11, 1)
                url = "https://example.invalid/schedule"
                key = hashlib.sha256(url.encode()).hexdigest()
                self.approve_plan(registry, "sound", day, "Микшер", "Алексей")
                registry.bind_service_member(
                    1001, "sound", "Алексей", 5005, tasks=("звук",),
                )
                self.assertNotIn("Микшер", registry.list_user_tasks(5005, "sound"))
                registry.save_schedule_notes(
                    key, "version-one",
                    [["sound", day.isoformat(), "2я часть", "членское"]],
                )
                loader = AsyncMock(side_effect=AssertionError("network"))
                with patch.object(app, "download_public", loader):
                    result, found = await app._load_service_task_assignments(
                        SimpleNamespace(yandex_public_url=url), root, ZoneInfo("UTC"),
                        "sound", MagicMock(), only_date=day, registry=registry,
                        require_approved_plan=True, return_event_found=True,
                    )
                    self.assertTrue(found)
                    self.assertEqual(result["Микшер"]["names"], ["Алексей"])
                    self.assertIn("Микшер", registry.list_user_tasks(5005, "sound"))
                    self.assertEqual(result["2я часть"]["names"], ["членское"])
                    summary = await app._load_month_summary_events(
                        loader, registry, {}, datetime(2026, 10, 31),
                        public_url=url, since=day, until=date(2026, 11, 30),
                    )
                    self.assertEqual(len(summary), 1)
                    # A successful source update removes deleted notes without changing assignments.
                    registry.save_schedule_notes(key, "version-two", [])
                    summary = await app._load_month_summary_events(
                        loader, registry, {}, datetime(2026, 10, 31),
                        public_url=url, since=day, until=date(2026, 11, 30),
                    )
                    self.assertEqual(
                        [item.task for item in summary[0].assignments], ["Микшер"]
                    )
                    # Notes belonging to another URL are never reused.
                    self.assertEqual(app._saved_note_events(
                        registry, url + "-other", day, day
                    ), [])
                    with patch.object(
                        registry, "get_schedule_plan",
                        side_effect=sqlite3.OperationalError("unavailable"),
                    ), self.assertRaises(sqlite3.OperationalError):
                        await app._load_service_task_assignments(
                            SimpleNamespace(yandex_public_url=url), root, ZoneInfo("UTC"),
                            "sound", MagicMock(), only_date=day, registry=registry,
                            require_approved_plan=True,
                        )
                loader.assert_not_awaited()

        asyncio.run(process())

    def test_approved_overview_adds_local_task_bindings_without_network(self):
        from bot import app
        from bot.service_registry import ServiceRegistry

        with tempfile.TemporaryDirectory() as tmp_dir:
            registry = ServiceRegistry(Path(tmp_dir) / "registry.sqlite3")
            registry.initialize(1001, -10001)
            day = date(2026, 11, 1)
            registry.bind_service_member(
                1001, "sound", "Алексей", 5005, tasks=("звук",),
            )
            self.approve_plan(registry, "sound", day, "Микшер", "Алексей")
            with patch.object(app, "download_public", new=AsyncMock()) as download:
                app._approved_service_events(
                    registry, "https://example.invalid/schedule", day, day,
                )
                download.assert_not_awaited()
            self.assertIn("Микшер", registry.list_user_tasks(5005, "sound"))

    def test_reference_refresh_changes_notes_but_not_approved_assignments(self):
        from bot import app
        from bot.service_registry import ServiceRegistry
        from openpyxl import Workbook

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            registry = ServiceRegistry(root / "registry.sqlite3")
            registry.initialize(1001, -10001)
            day = date(2026, 11, 1)
            self.approve_plan(registry, "sound", day, "звук", "Алексей")
            approved = registry.get_schedule_plan("sound", "2026-11")["approved"]
            path = root / "candidate.xlsx"
            workbook = Workbook()
            sheet = workbook.active
            sheet.title = "НОЯБРЬ26"
            for row in (["ЗВУК"], [day], ["звук", "Мария"], ["*2я часть", "членское"]):
                sheet.append(row)
            roster = workbook.create_sheet("Списки")
            for row in (["ЗВУК"], ["звук"], ["Алексей"]):
                roster.append(row)
            workbook.save(path)
            url = "https://example.invalid/schedule"
            asyncio.run(app._refresh_schedule_reference_data(registry, url, path, MagicMock()))
            shown = app._approved_service_events(registry, url, day, day)
            self.assertEqual(
                [(item.task, item.name) for item in shown[0].assignments],
                [("звук", "Алексей"), ("2я часть", "членское")],
            )
            sheet.delete_rows(4)
            workbook.save(path)
            workbook.close()
            asyncio.run(app._refresh_schedule_reference_data(registry, url, path, MagicMock()))
            shown = app._approved_service_events(registry, url, day, day)
            self.assertEqual(
                [(item.task, item.name) for item in shown[0].assignments],
                [("звук", "Алексей")],
            )
            self.assertEqual(
                registry.get_schedule_plan("sound", "2026-11")["approved"], approved
            )

    def test_service_reminder_without_approved_schedule_is_suppressed(self):
        from bot import app
        from bot.service_registry import ServiceRegistry

        async def process():
            with tempfile.TemporaryDirectory() as tmp_dir:
                registry = ServiceRegistry(Path(tmp_dir) / "registry.sqlite3")
                registry.initialize(1001, -10001)
                reminder = registry.list_group_reminders("assembly")[0]
                event_date = date(2026, 10, 11)
                bot = SimpleNamespace(send_message=AsyncMock())
                logger = MagicMock()
                with patch.object(
                    app, "_has_approved_schedule_for_event",
                    return_value=False,
                ), patch.object(
                    app, "_load_service_task_assignments",
                    new=AsyncMock(),
                ) as load, patch.object(
                    app, "_notify_admin", new=AsyncMock(return_value=True)
                ) as notify:
                    await app._process_service_reminder(
                        bot, SimpleNamespace(admin_tg_id=1001), Path(tmp_dir),
                        ZoneInfo("UTC"), registry, "assembly", reminder["id"],
                        logger, logger,
                        run_date=event_date - timedelta(
                            days=reminder["reminder_days"]
                        ),
                    )

                load.assert_not_awaited()
                notify.assert_awaited_once()
                self.assertIn("не сохранил его в БД бота", notify.await_args.args[2])
                self.assertFalse(any(
                    call.args[0] == -10001
                    for call in bot.send_message.await_args_list
                ))
                self.assertTrue(registry.has_readiness_notice(
                    "assembly", "2026-10", "schedule-unapproved"
                ))

        asyncio.run(process())

    def test_personal_reminder_without_approved_schedule_is_suppressed(self):
        from bot import app
        from bot.service_registry import ServiceRegistry

        async def process():
            with tempfile.TemporaryDirectory() as tmp_dir:
                registry = ServiceRegistry(Path(tmp_dir) / "registry.sqlite3")
                registry.initialize(1001, -10001)
                registry.set_group_chat(1001, "sound", -10055, enabled=True)
                registry.set_responsible_contact(
                    1001, "sound", "Микшер", "Алексей", 2002
                )
                registry.set_user_notifications_enabled(2002, "sound", True)
                user_text = registry.upsert_user_text(
                    2002, "sound", "Микшер", "Напоминание {name}"
                )
                schedule = registry.add_schedule(user_text, 1, "18:00")
                event_date = date(2026, 10, 18)
                bot = SimpleNamespace(send_message=AsyncMock())
                logger = MagicMock()
                with patch.object(
                    app, "_has_approved_schedule_for_event",
                    return_value=False,
                ), patch.object(
                    app, "_load_service_task_assignments",
                    new=AsyncMock(),
                ) as load, patch.object(
                    app, "_notify_admin", new=AsyncMock(return_value=True)
                ) as notify:
                    await app._process_user_reminder_schedule(
                        bot, SimpleNamespace(admin_tg_id=1001), Path(tmp_dir),
                        ZoneInfo("UTC"), registry, schedule["id"], logger,
                        logger,
                        run_date=event_date - timedelta(
                            days=schedule["reminder_days"]
                        ),
                    )

                load.assert_not_awaited()
                notify.assert_awaited_once()
                self.assertFalse(any(
                    call.args[0] == 2002
                    for call in bot.send_message.await_args_list
                ))

        asyncio.run(process())

    def test_personal_reminder_skips_weekday_in_approved_month_without_warning(self):
        from bot import app
        from bot.service_registry import ServiceRegistry

        async def process():
            with tempfile.TemporaryDirectory() as tmp_dir:
                registry = ServiceRegistry(Path(tmp_dir) / "registry.sqlite3")
                registry.initialize(1001, -10001)
                registry.set_responsible_contact(
                    1001, "sound", "Микшер", "Алексей", 2002
                )
                registry.set_user_notifications_enabled(2002, "sound", True)
                text_id = registry.ensure_user_text(2002, "sound", "Микшер")
                schedule = registry.add_schedule(text_id, 0, "18:00")
                self.approve_plan(
                    registry, "sound", date(2026, 10, 18), "Микшер", "Алексей"
                )
                # Restore the real approval check, normally mocked by setUp.
                self._approved_schedule.stop()
                bot = SimpleNamespace(send_message=AsyncMock())
                logger = MagicMock()
                with patch.object(
                    app, "_load_service_task_assignments", new=AsyncMock()
                ) as load:
                    await app._process_user_reminder_schedule(
                        bot, SimpleNamespace(admin_tg_id=1001), Path(tmp_dir),
                        ZoneInfo("UTC"), registry, schedule["id"], logger, logger,
                        run_date=date(2026, 10, 20),
                    )
                load.assert_not_awaited()
                bot.send_message.assert_not_awaited()
                logger.warning.assert_not_called()
                self.assertFalse(registry.has_readiness_notice(
                    "sound", "2026-10", "schedule-unapproved"
                ))
                self.assertFalse(registry.is_user_reminder_delivered(
                    schedule["id"], "2026-10-20"
                ))

        asyncio.run(process())

    def test_personal_reminder_preparation_failure_is_logged_not_sent_to_admin(self):
        from bot import app
        from bot.service_registry import ServiceRegistry

        async def process():
            with tempfile.TemporaryDirectory() as tmp_dir:
                registry = ServiceRegistry(Path(tmp_dir) / "registry.sqlite3")
                registry.initialize(1001, -10001)
                registry.set_group_chat(1001, "sound", -10055, enabled=True)
                registry.set_responsible_contact(
                    1001, "sound", "Микшер", "Алексей", 2002
                )
                registry.set_user_notifications_enabled(2002, "sound", True)
                text_id = registry.ensure_user_text(2002, "sound", "Микшер")
                schedule = registry.add_schedule(text_id, 1, "18:00")
                event_date = date(2026, 10, 18)
                logger = MagicMock()
                with patch.object(
                    app, "_has_approved_schedule_for_event",
                    return_value=True,
                ), patch.object(
                    app, "_load_service_task_assignments",
                    new=AsyncMock(side_effect=RuntimeError("workbook unavailable")),
                ), patch.object(
                    app, "_notify_admin", new=AsyncMock(return_value=True)
                ) as notify:
                    await app._process_user_reminder_schedule(
                        SimpleNamespace(send_message=AsyncMock()),
                        SimpleNamespace(admin_tg_id=1001),
                        Path(tmp_dir),
                        ZoneInfo("UTC"),
                        registry,
                        schedule["id"],
                        logger,
                        logger,
                        run_date=event_date - timedelta(
                            days=schedule["reminder_days"]
                        ),
                    )

                notify.assert_not_awaited()
                logger.exception.assert_called_once()

        asyncio.run(process())

    def test_group_reminder_is_suppressed_if_approval_is_revoked_during_load(self):
        from bot import app
        from bot.service_registry import ServiceRegistry

        async def process():
            with tempfile.TemporaryDirectory() as tmp_dir:
                registry = ServiceRegistry(Path(tmp_dir) / "registry.sqlite3")
                registry.initialize(1001, -10001)
                reminder = registry.list_group_reminders("assembly")[0]
                event_date = date(2026, 10, 11)
                bot = SimpleNamespace(send_message=AsyncMock())
                logger = MagicMock()
                with patch.object(
                    app, "_has_approved_schedule_for_event",
                    side_effect=(True, False),
                ), patch.object(
                    app, "_load_service_task_assignments",
                    new=AsyncMock(return_value=({"Ведущий": ["Анна"]}, True)),
                ):
                    await app._process_service_reminder(
                        bot, SimpleNamespace(admin_tg_id=1001), Path(tmp_dir),
                        ZoneInfo("UTC"), registry, "assembly", reminder["id"],
                        logger, logger,
                        run_date=event_date - timedelta(
                            days=reminder["reminder_days"]
                        ),
                    )

                self.assertFalse(any(
                    call.args[0] == -10001
                    for call in bot.send_message.await_args_list
                ))

        asyncio.run(process())

    def test_personal_reminder_is_suppressed_if_approval_is_revoked_during_load(self):
        from bot import app
        from bot.service_registry import ServiceRegistry

        async def process():
            with tempfile.TemporaryDirectory() as tmp_dir:
                registry = ServiceRegistry(Path(tmp_dir) / "registry.sqlite3")
                registry.initialize(1001, -10001)
                registry.set_group_chat(1001, "sound", -10055, enabled=True)
                registry.set_responsible_contact(
                    1001, "sound", "Микшер", "Алексей", 2002
                )
                registry.set_user_notifications_enabled(2002, "sound", True)
                user_text = registry.upsert_user_text(
                    2002, "sound", "Микшер", "Напоминание {name}"
                )
                schedule = registry.add_schedule(user_text, 1, "18:00")
                event_date = date(2026, 10, 18)
                bot = SimpleNamespace(send_message=AsyncMock())
                logger = MagicMock()
                with patch.object(
                    app, "_has_approved_schedule_for_event",
                    side_effect=(True, False),
                ), patch.object(
                    app, "_load_service_task_assignments",
                    new=AsyncMock(return_value=({"Микшер": ["Алексей"]}, True)),
                ):
                    await app._process_user_reminder_schedule(
                        bot, SimpleNamespace(admin_tg_id=1001), Path(tmp_dir),
                        ZoneInfo("UTC"), registry, schedule["id"], logger,
                        logger,
                        run_date=event_date - timedelta(
                            days=schedule["reminder_days"]
                        ),
                    )

                self.assertFalse(any(
                    call.args[0] == 2002
                    for call in bot.send_message.await_args_list
                ))

        asyncio.run(process())

    def test_delivery_loader_does_not_download_excel_without_approved_plan(self):
        from bot import app
        from bot.service_registry import ServiceRegistry

        async def load():
            with tempfile.TemporaryDirectory() as tmp_dir:
                registry = ServiceRegistry(Path(tmp_dir) / "registry.sqlite3")
                registry.initialize(1001, -10001)
                with patch.object(
                    app, "download_public", new=AsyncMock()
                ) as download:
                    result = await app._load_service_task_assignments(
                        SimpleNamespace(
                            yandex_public_url="https://example.invalid/schedule"
                        ),
                        Path(tmp_dir),
                        ZoneInfo("UTC"),
                        "sound",
                        MagicMock(),
                        only_date=date(2026, 10, 18),
                        return_event_found=True,
                        registry=registry,
                        require_approved_plan=True,
                    )
                download.assert_not_awaited()
                self.assertEqual(result, ({}, False))

        asyncio.run(load())

    def test_approved_only_event_overlay_excludes_excel_candidates(self):
        from bot import app
        from bot.excel_parser import ServiceEvent
        from bot.service_registry import ServiceRegistry

        with tempfile.TemporaryDirectory() as tmp_dir:
            registry = ServiceRegistry(Path(tmp_dir) / "registry.sqlite3")
            registry.initialize(1001, -10001)
            candidate = ServiceEvent("sound", date(2026, 10, 18), ())
            self.assertEqual(
                app._apply_bot_plans(
                    [candidate], registry, date(2026, 10, 18),
                    date(2026, 10, 18), approved_only=True,
                ),
                [],
            )

    def test_legacy_delivery_parses_own_download_not_shared_backup_file(self):
        from bot import app

        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                shared = root / "reminder_bot_current.xlsx"
                manager = MagicMock()

                async def download(url, path, logger, **options):
                    path.write_bytes(b"fresh assignments")

                async def record(path):
                    shared.write_bytes(b"older backup download")

                def parse(path, *args):
                    self.assertNotEqual(path, shared)
                    self.assertEqual(path.read_bytes(), b"fresh assignments")
                    raise ValueError("stop after verifying input")

                manager.queue_schedule_download = AsyncMock(side_effect=record)
                cache = MagicMock()
                cache.matches_week.return_value = False
                credentials = SimpleNamespace(
                    group_chat_id=-100, admin_tg_id=1001,
                    yandex_public_url="https://example.invalid/schedule",
                )
                with patch.object(app, "download_public", side_effect=download), \
                        patch.object(app, "parse_schedule", side_effect=parse), \
                        patch.object(app, "_notify_admin", new=AsyncMock()), \
                        self.assertRaisesRegex(ValueError, "stop after verifying"):
                    await app._process_upcoming_sunday(
                        MagicMock(), credentials, {}, cache, root,
                        MagicMock(), MagicMock(), ZoneInfo("UTC"),
                        backup_manager=manager,
                    )
                self.assertFalse(list(root.glob(".reminder-delivery-*")))

        asyncio.run(scenario())

    def test_schedule_download_records_fresh_bytes_before_returning(self):
        from bot import app

        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "schedule.xlsx"
                manager = MagicMock()
                manager.queue_schedule_download = AsyncMock()

                async def download(url, target, logger, **options):
                    target.write_bytes(b"fresh workbook")

                with patch.object(app, "download_public", side_effect=download):
                    result = await app._download_schedule(
                        "https://example.invalid/schedule", path, MagicMock(),
                        backup_manager=manager, attempts=1, interval=0,
                    )
                self.assertEqual(result.read_bytes(), b"fresh workbook")
                manager.queue_schedule_download.assert_awaited_once_with(path)

        asyncio.run(scenario())

    def test_schedule_backup_failure_is_reported_without_hiding_fresh_download(self):
        from bot import app

        async def scenario():
            manager = MagicMock()
            manager.queue_schedule_download = AsyncMock(
                side_effect=OSError("disk full")
            )
            manager.report_failure = AsyncMock()
            manager.check_storage = AsyncMock()
            path = Path("fresh.xlsx")
            with patch.object(app, "download_public", new=AsyncMock()) as download:
                result = await app._download_schedule(
                    "https://example.invalid/schedule", path, MagicMock(),
                    backup_manager=manager,
                )
            download.assert_awaited_once()
            self.assertEqual(result, path)
            manager.report_failure.assert_awaited_once()
            manager.check_storage.assert_awaited_once()
            manager.clear_failure.assert_not_called()

        asyncio.run(scenario())

    def test_failed_live_download_does_not_read_backup_copy(self):
        from bot import app

        async def scenario():
            manager = MagicMock()
            manager.queue_schedule_download = AsyncMock()
            with patch.object(
                app, "download_public", new=AsyncMock(side_effect=OSError("offline"))
            ), self.assertRaises(OSError):
                await app._download_schedule(
                    "https://example.invalid/schedule", Path("fresh.xlsx"),
                    MagicMock(), backup_manager=manager,
                )
            manager.queue_schedule_download.assert_not_awaited()

        asyncio.run(scenario())

    def test_webapp_menu_button_uses_configured_https_url(self):
        from bot.app import _configure_webapp_menu

        async def configure():
            with tempfile.TemporaryDirectory() as tmp_dir:
                project = Path(tmp_dir)
                (project / "webapp_config.json").write_text(
                    '{"host":"127.0.0.1","port":8080,'
                    '"public_url":"https://example.org/app"}',
                    encoding="utf-8",
                )
                bot = SimpleNamespace(set_chat_menu_button=AsyncMock())
                await _configure_webapp_menu(bot, project, MagicMock())
                bot.set_chat_menu_button.assert_awaited_once()
                button = bot.set_chat_menu_button.await_args.kwargs["menu_button"]
                self.assertEqual(button.web_app.url, "https://example.org/app")

        asyncio.run(configure())

    def test_test_notification_previews_use_upcoming_assignments(self):
        from bot import app

        async def prepare():
            with tempfile.TemporaryDirectory() as tmp_dir:
                shm_dir = Path(tmp_dir) / "shm"
                shm_dir.mkdir(mode=0o700)
                target = date(2026, 10, 18)
                credentials = SimpleNamespace(
                    yandex_public_url="https://example.invalid/schedule"
                )
                schedule = {
                    "Ответственный": "Алексей",
                    "Ведущий": "Павел",
                }
                people = {
                    "Ответственный": {
                        "Алексей": {"username": "alex", "tg_id": 12345}
                    },
                    "Ведущий": {
                        "Павел": {"username": "pavel", "tg_id": None}
                    },
                }
                templates = {
                    "general": (
                        "{sunday_date}: @{tg_responsible}; "
                        "@{tg_leader}; @{tg_preacher}"
                    ),
                    "roles": {
                        "Ответственный": "{name}, {sunday_date}",
                        "Ведущий": "{name}, {sunday_date}",
                    },
                }
                logger = MagicMock()

                async def fake_download(_url, path, _logger, **_kwargs):
                    path.write_bytes(b"test workbook")
                    return path

                with ExitStack() as stack:
                    stack.enter_context(
                        patch.object(
                            app, "_next_sunday_in_tz", return_value=target
                        )
                    )
                    download = stack.enter_context(
                        patch.object(
                            app,
                            "download_public",
                            new=AsyncMock(side_effect=fake_download),
                        )
                    )
                    parse_schedule = stack.enter_context(
                        patch.object(app, "parse_schedule", return_value=schedule)
                    )
                    parse_people = stack.enter_context(
                        patch.object(app, "parse_people", return_value=people)
                    )

                    previews = await app._prepare_test_notification_previews(
                        credentials,
                        templates,
                        shm_dir,
                        ZoneInfo("UTC"),
                        logger,
                    )

                download.assert_awaited_once()
                parse_schedule.assert_called_once()
                parse_people.assert_called_once()
                self.assertEqual(previews["sunday_date"], target)
                self.assertEqual(
                    previews["group_text"],
                    "18.10.2026: @alex; @pavel; не найден",
                )
                self.assertEqual(
                    previews["personal_messages"],
                    [("Ответственный", "Алексей", "🔔 Алексей, 18.10.2026")],
                )
                self.assertTrue(any("Ведущий" in issue for issue in previews["issues"]))
                self.assertTrue(any("Проповедник" in issue for issue in previews["issues"]))
                self.assertEqual(list((shm_dir / "debug").iterdir()), [])

        asyncio.run(prepare())

    def test_service_reminder_job_sends_only_the_group_message(self):
        from bot import app
        from bot.service_registry import ServiceRegistry

        async def send_reminder():
            with tempfile.TemporaryDirectory() as tmp_dir:
                registry = ServiceRegistry(Path(tmp_dir) / "registry.sqlite3")
                registry.initialize(1001, -10001)
                registry.set_responsible_contact(
                    1001, "assembly", "Камеры", "Анна", 2002, "anna"
                )
                registry.set_responsible_contact(
                    1001, "assembly", "Камеры", "Борис", 2003, "boris"
                )
                registry.set_user_notifications_enabled(2002, "assembly", True)
                registry.set_user_notifications_enabled(2003, "assembly", True)
                registry.set_service_default_template(
                    1001,
                    "assembly",
                    "{task_assignments}\n{informational_notes}",
                )
                reminder = registry.get_service("assembly")["reminders"][0]
                bot = SimpleNamespace(send_message=AsyncMock())
                credentials = SimpleNamespace(admin_tg_id=1001)
                logger = MagicMock()
                state_cache = SimpleNamespace(
                    state={},
                    save=MagicMock(),
                )
                assignments = {
                    "Камеры": ["Анна", "Борис"],
                    "2-я часть": {
                        "names": ["нет"],
                        "is_informational": True,
                    },
                }
                with patch.object(
                    app,
                    "_load_service_task_assignments",
                    new=AsyncMock(return_value=(assignments, True)),
                ) as load_assignments, patch.object(
                    app, "_notify_admin", new=AsyncMock()
                ) as notify_admin:
                    await app._process_service_reminder(
                        bot,
                        credentials,
                        Path(tmp_dir),
                        ZoneInfo("UTC"),
                        registry,
                        "assembly",
                        reminder["id"],
                        logger,
                        logger,
                        run_date=(
                            date(2026, 10, 11)
                            - timedelta(days=reminder["reminder_days"])
                        ),
                        state_cache=state_cache,
                    )

                self.assertEqual(len(group_sends(bot)), 1)
                self.assertEqual(
                    group_sends(bot)[0].args[0], -10001
                )
                self.assertIn(
                    "📌 2-я часть нет",
                    group_sends(bot)[0].args[1],
                )
                state_cache.save.assert_not_called()
                notify_admin.assert_not_awaited()
                load_assignments.assert_awaited_once()

        asyncio.run(send_reminder())

    def test_upcoming_events_cover_next_months_and_skip_missing_sheets(self):
        from bot import app
        from bot.excel_parser import ServiceEvent, SheetNotFoundError

        today = datetime.now(ZoneInfo("UTC")).date()
        parsed_months = []

        def parse(path, month, logger, services=None):
            parsed_months.append(month)
            if month != today.replace(day=1):
                raise SheetNotFoundError("Лист расписания не найден")
            return {
                "sound": [
                    ServiceEvent("sound", today - timedelta(days=1), ()),
                    ServiceEvent("sound", today, ()),
                    ServiceEvent("sound", today + timedelta(days=60), ()),
                ]
            }

        async def load():
            with tempfile.TemporaryDirectory() as tmp_dir, patch.object(
                app, "download_public", new=AsyncMock()
            ), patch.object(app, "parse_service_events", side_effect=parse):
                source = {}
                events = await app._load_upcoming_service_events(
                    SimpleNamespace(yandex_public_url="https://example.invalid"),
                    Path(tmp_dir),
                    ZoneInfo("UTC"),
                    MagicMock(),
                    source_out=source,
                )
                self.assertEqual(source["digest"], hashlib.sha256(b"").hexdigest())
                leftovers = list((Path(tmp_dir) / "debug").iterdir())
            return events, leftovers

        events, leftovers = asyncio.run(load())
        self.assertEqual([event.event_date for event in events], [today])
        self.assertGreaterEqual(len(parsed_months), 2)
        self.assertEqual(leftovers, [])

    def test_roster_sync_failure_is_logged_and_issues_are_per_service(self):
        from bot import app
        from bot.excel_parser import RosterMember
        from bot.service_registry import ServiceRegistry


        with tempfile.TemporaryDirectory() as tmp_dir:
            registry = ServiceRegistry(Path(tmp_dir) / "registry.sqlite3")
            registry.initialize(1001, -10001)
            logger = MagicMock()
            with patch.object(
                app, "parse_service_rosters",
                side_effect=RuntimeError("Лист 'Списки' не найден"),
            ):
                app._sync_rosters_and_contacts(
                    registry, Path(tmp_dir) / "x.xlsx", "sound", {}, None,
                    logger, [],
                )
            logger.exception.assert_called_once()

            def parse(path, logger, services, issues):
                issues.extend([
                    "«Списки», Звук: «Иван» указан без задачи",
                    "«Списки», Кухня: «Анна» указан без задачи",
                    "«Списки»: служение «Хор» не найдено в каталоге",
                ])
                return {"sound": [RosterMember("Иван", ("звук",))]}

            registry.bind_service_member(1001, "sound", "Иван", 7007)
            issues = []
            with patch.object(app, "parse_service_rosters", side_effect=parse):
                app._sync_rosters_and_contacts(
                    registry, Path(tmp_dir) / "x.xlsx", "sound",
                    {"сборка": {"names": ["Иван"], "is_informational": False}},
                    None, logger, issues,
                )
            self.assertEqual(
                issues,
                [
                    "«Списки», Звук: «Иван» указан без задачи",
                    "«Списки»: служение «Хор» не найдено в каталоге",
                ],
            )
            repeated = []
            with patch.object(app, "parse_service_rosters", side_effect=parse):
                app._sync_rosters_and_contacts(
                    registry, Path(tmp_dir) / "x.xlsx", "kitchen", {}, None,
                    logger, repeated,
                )
            self.assertEqual(
                repeated,
                [
                    "«Списки», Кухня: «Анна» указан без задачи",
                    "«Списки»: служение «Хор» не найдено в каталоге",
                ],
            )
            tasks = sorted(
                contact["task"]
                for contact in registry.list_responsible_contacts(1001, "sound")
                if contact["name"] == "Иван"
            )
            self.assertEqual(tasks, ["звук", "сборка"])

    def test_upcoming_events_skip_empty_later_sheet(self):
        from bot import app
        from bot.excel_parser import ServiceEvent, SheetEmptyError

        today = datetime.now(ZoneInfo("UTC")).date()

        def parse(path, month, logger, services=None):
            if month != today.replace(day=1):
                raise SheetEmptyError("Лист расписания пуст")
            return {"sound": [ServiceEvent("sound", today, ())]}

        async def load():
            with tempfile.TemporaryDirectory() as tmp_dir, patch.object(
                app, "download_public", new=AsyncMock()
            ), patch.object(app, "parse_service_events", side_effect=parse):
                return await app._load_upcoming_service_events(
                    SimpleNamespace(yandex_public_url="https://example.invalid"),
                    Path(tmp_dir),
                    ZoneInfo("UTC"),
                    MagicMock(),
                )

        events = asyncio.run(load())
        self.assertEqual([event.event_date for event in events], [today])

    def test_upcoming_events_propagate_broken_later_sheet(self):
        from bot import app

        today = datetime.now(ZoneInfo("UTC")).date()

        def parse(path, month, logger, services=None):
            if month != today.replace(day=1):
                raise RuntimeError("Лист расписания пуст")
            return {}

        async def load():
            with tempfile.TemporaryDirectory() as tmp_dir, patch.object(
                app, "download_public", new=AsyncMock()
            ), patch.object(app, "parse_service_events", side_effect=parse):
                await app._load_upcoming_service_events(
                    SimpleNamespace(yandex_public_url="https://example.invalid"),
                    Path(tmp_dir),
                    ZoneInfo("UTC"),
                    MagicMock(),
                )

        with self.assertRaises(RuntimeError):
            asyncio.run(load())

    def test_upcoming_events_fail_when_current_month_is_missing(self):
        from bot import app
        from bot.excel_parser import SheetNotFoundError

        async def load():
            with tempfile.TemporaryDirectory() as tmp_dir, patch.object(
                app, "download_public", new=AsyncMock()
            ), patch.object(
                app,
                "parse_service_events",
                side_effect=SheetNotFoundError("Лист расписания не найден"),
            ):
                await app._load_upcoming_service_events(
                    SimpleNamespace(yandex_public_url="https://example.invalid"),
                    Path(tmp_dir),
                    ZoneInfo("UTC"),
                    MagicMock(),
                )

        with self.assertRaises(RuntimeError):
            asyncio.run(load())

    def test_service_reminder_limit_counts_visible_text_of_links(self):
        from bot import app
        from bot.notifier import TELEGRAM_TEXT_LIMIT
        from bot.service_registry import ServiceRegistry

        async def send_reminder():
            with tempfile.TemporaryDirectory() as tmp_dir:
                registry = ServiceRegistry(Path(tmp_dir) / "registry.sqlite3")
                registry.initialize(1001, -10001)
                names = [
                    f"Участник {index:02d} " + "Ж" * 45 for index in range(50)
                ]
                for index, name in enumerate(names):
                    registry.set_responsible_contact(
                        1001, "assembly", "Камеры", name, 3000 + index,
                        f"user{index:02d}",
                    )
                registry.set_service_default_template(
                    1001, "assembly", "{task_assignments}"
                )
                reminder = registry.get_service("assembly")["reminders"][0]
                bot = SimpleNamespace(send_message=AsyncMock())
                logger = MagicMock()
                with patch.object(
                    app,
                    "_load_service_task_assignments",
                    new=AsyncMock(return_value=({"Камеры": names}, True)),
                ), patch.object(
                    app, "_notify_admin", new=AsyncMock()
                ) as notify_admin:
                    await app._process_service_reminder(
                        bot,
                        SimpleNamespace(admin_tg_id=1001),
                        Path(tmp_dir),
                        ZoneInfo("UTC"),
                        registry,
                        "assembly",
                        reminder["id"],
                        logger,
                        logger,
                        run_date=(
                            date(2026, 10, 11)
                            - timedelta(days=reminder["reminder_days"])
                        ),
                        state_cache=SimpleNamespace(state={}, save=MagicMock()),
                    )

                raw_text = app.format_task_assignments(
                    {"Камеры": names},
                    {
                        (item["task"], item["name"]): item
                        for item in registry.list_responsible_contacts(
                            1001, "assembly"
                        )
                    },
                )
                self.assertGreater(
                    len(raw_text.encode("utf-16-le")) // 2, TELEGRAM_TEXT_LIMIT
                )
                notify_admin.assert_not_awaited()
                self.assertEqual(len(group_sends(bot)), 1)
                sent = group_sends(bot)[-1].args[1]
                self.assertIn('<a href="tg://user?id=3049">', sent)
                self.assertNotIn("\x00", sent)

        asyncio.run(send_reminder())

    def test_confirmed_duplicate_reminders_send_each_reminders_own_text(self):
        from bot import app
        from bot.service_registry import ServiceRegistry

        async def process():
            with tempfile.TemporaryDirectory() as tmp_dir:
                registry = ServiceRegistry(Path(tmp_dir) / "registry.sqlite3")
                registry.initialize(1001, -10001)
                reminders = registry.list_group_reminders("assembly")
                original = reminders[0]
                duplicate_id = registry.add_group_reminder(
                    1001,
                    "assembly",
                    original["reminder_days"],
                    original["reminder_time"],
                    "Отдельный текст {service_name}",
                    allow_duplicate=True,
                )
                bot = SimpleNamespace(send_message=AsyncMock())
                credentials = SimpleNamespace(admin_tg_id=1001)
                logger = MagicMock()
                run_date = date(2026, 10, 11) - timedelta(
                    days=original["reminder_days"]
                )
                with patch.object(
                    app,
                    "_load_service_task_assignments",
                    new=AsyncMock(
                        return_value=({"Камеры": ["Анна"]}, True)
                    ),
                ):
                    for reminder_id in (original["id"], duplicate_id):
                        await app._process_service_reminder(
                            bot,
                            credentials,
                            Path(tmp_dir),
                            ZoneInfo("UTC"),
                            registry,
                            "assembly",
                            reminder_id,
                            logger,
                            logger,
                            run_date=run_date,
                        )

                self.assertEqual(len(group_sends(bot)), 2)
                sent_texts = [
                    call.args[1]
                    for call in group_sends(bot)
                ]
                self.assertTrue(
                    any("Отдельный текст Собрание" in text for text in sent_texts)
                )
                self.assertTrue(
                    any("Напоминание о служении" in text for text in sent_texts)
                )
                event_date = date(2026, 10, 11).isoformat()
                self.assertTrue(
                    registry.is_group_reminder_delivered(
                        original["id"], event_date, "group:-10001"
                    )
                )
                self.assertTrue(
                    registry.is_group_reminder_delivered(
                        duplicate_id, event_date, "group:-10001"
                    )
                )

        asyncio.run(process())

    def test_schedule_issues_are_sent_to_admin_once_per_event(self):
        from bot import app
        from bot.service_registry import ServiceRegistry

        async def load(*args, issues=None, **kwargs):
            issues.append("«Собрание», 11.10.2026: строка без задачи")
            return {"Ведущий": ["Анна"]}, True

        async def process():
            with tempfile.TemporaryDirectory() as tmp_dir:
                registry = ServiceRegistry(Path(tmp_dir) / "registry.sqlite3")
                registry.initialize(1001, -10001)
                reminder = registry.list_group_reminders("assembly")[0]
                bot = SimpleNamespace(send_message=AsyncMock())
                credentials = SimpleNamespace(admin_tg_id=1001)
                logger = MagicMock()
                run_date = date(2026, 10, 11) - timedelta(
                    days=reminder["reminder_days"]
                )
                with patch.object(
                    app, "_load_service_task_assignments",
                    new=AsyncMock(side_effect=load),
                ):
                    for _ in range(2):
                        await app._process_service_reminder(
                            bot, credentials, Path(tmp_dir), ZoneInfo("UTC"),
                            registry, "assembly", reminder["id"],
                            logger, logger, run_date=run_date,
                        )

                admin_messages = [
                    call.args[1]
                    for call in bot.send_message.await_args_list
                    if call.args[0] == 1001
                    and "Проверьте график" in call.args[1]
                ]
                self.assertEqual(len(admin_messages), 1)
                self.assertIn("строка без задачи", admin_messages[0])
                self.assertIn("Проверьте график «Собрание»", admin_messages[0])

        asyncio.run(process())

    def test_sheet_wide_roster_issue_is_sent_once_after_delivery(self):
        from bot import app
        from bot.service_registry import ServiceRegistry

        sheet_issue = "«Списки»: служение «Хор» не найдено в каталоге"
        found = {"sound": False, "assembly": True, "broadcast": True}

        async def load(credentials, shm_dir, tz, service_id, *args,
                       issues=None, **kwargs):
            issues.append(sheet_issue)
            return {"Ведущий": ["Анна"]}, found[service_id]

        async def process():
            app._REPORTED_SHEET_ISSUES.clear()
            with tempfile.TemporaryDirectory() as tmp_dir:
                registry = ServiceRegistry(Path(tmp_dir) / "registry.sqlite3")
                registry.initialize(1001, -10001)
                for service_id in ("sound", "broadcast"):
                    registry.set_group_chat(
                        1001, service_id, -10002, enabled=True
                    )
                bot = SimpleNamespace(send_message=AsyncMock())
                logger = MagicMock()
                with patch.object(
                    app, "_load_service_task_assignments",
                    new=AsyncMock(side_effect=load),
                ), patch.object(
                    app, "_notify_admin", new=AsyncMock(return_value=True)
                ) as notify_admin:
                    for service_id in ("sound", "assembly", "broadcast"):
                        reminder = registry.list_group_reminders(service_id)[0]
                        await app._process_service_reminder(
                            bot, SimpleNamespace(admin_tg_id=1001),
                            Path(tmp_dir), ZoneInfo("UTC"), registry,
                            service_id, reminder["id"], logger, logger,
                            run_date=date(2026, 10, 11) - timedelta(
                                days=reminder["reminder_days"]
                            ),
                        )
                texts = [call.args[2] for call in notify_admin.await_args_list]
                self.assertEqual(
                    [text for text in texts if sheet_issue in text
                     and "Проверьте график" in text],
                    [next(t for t in texts if "«Собрание»" in t)],
                )
            app._REPORTED_SHEET_ISSUES.clear()

        asyncio.run(process())

    def test_group_reminder_is_sent_to_chosen_topic(self):
        from bot import app
        from bot.service_registry import ServiceRegistry

        async def process():
            with tempfile.TemporaryDirectory() as tmp_dir:
                registry = ServiceRegistry(Path(tmp_dir) / "registry.sqlite3")
                registry.initialize(1001, -10001)
                registry.set_group_thread(1001, "assembly", -10001, 9)
                reminder = registry.list_group_reminders("assembly")[0]
                bot = SimpleNamespace(send_message=AsyncMock())
                logger = MagicMock()
                with patch.object(
                    app, "_load_service_task_assignments",
                    new=AsyncMock(return_value=({"Ведущий": ["Анна"]}, True)),
                ):
                    await app._process_service_reminder(
                        bot, SimpleNamespace(admin_tg_id=1001), Path(tmp_dir),
                        ZoneInfo("UTC"), registry, "assembly", reminder["id"],
                        logger, logger,
                        run_date=date(2026, 10, 11) - timedelta(
                            days=reminder["reminder_days"]
                        ),
                    )
                group_calls = [
                    call for call in bot.send_message.await_args_list
                    if call.args[0] == -10001
                ]
                self.assertEqual(len(group_calls), 1)
                self.assertEqual(
                    group_calls[0].kwargs.get("message_thread_id"), 9
                )

        asyncio.run(process())

    def test_deleted_topic_falls_back_to_general_and_is_reset(self):
        from aiogram.exceptions import TelegramBadRequest
        from bot import app
        from bot.service_registry import ServiceRegistry

        errors = iter((
            "Bad Request: message thread not found",
            "Bad Request: TOPIC_CLOSED",
        ))

        async def send(chat_id, text, **kwargs):
            if kwargs.get("message_thread_id"):
                raise TelegramBadRequest(method=MagicMock(), message=next(errors))
            return SimpleNamespace(message_id=1)

        async def process():
            with tempfile.TemporaryDirectory() as tmp_dir:
                registry = ServiceRegistry(Path(tmp_dir) / "registry.sqlite3")
                registry.initialize(1001, -10001)
                registry.set_group_thread(1001, "assembly", -10001, 9)
                reminder = registry.list_group_reminders("assembly")[0]
                bot = SimpleNamespace(send_message=AsyncMock(side_effect=send))
                logger = MagicMock()
                event_date = date(2026, 10, 11)
                with patch.object(
                    app, "_load_service_task_assignments",
                    new=AsyncMock(return_value=({"Ведущий": ["Анна"]}, True)),
                ):
                    await app._process_service_reminder(
                        bot, SimpleNamespace(admin_tg_id=1001), Path(tmp_dir),
                        ZoneInfo("UTC"), registry, "assembly", reminder["id"],
                        logger, logger,
                        run_date=event_date - timedelta(
                            days=reminder["reminder_days"]
                        ),
                    )
                self.assertIsNone(
                    registry.get_service("assembly")["group_thread_id"]
                )
                self.assertTrue(
                    registry.is_group_reminder_delivered(
                        reminder["id"], event_date.isoformat(), "group:-10001"
                    )
                )
                from bot.handlers import send_to_service_group

                registry.set_group_thread(1001, "assembly", -10001, 9)
                await send_to_service_group(
                    bot.send_message, -10001, "тест",
                    registry.get_service("assembly"), registry=registry,
                )
                self.assertEqual(
                    registry.get_service("assembly")["group_thread_id"], 9
                )

        asyncio.run(process())

    def test_task_reminder_lists_only_its_responsibles_or_warns(self):
        from bot import app
        from bot.service_registry import ServiceRegistry

        async def process():
            with tempfile.TemporaryDirectory() as tmp_dir:
                registry = ServiceRegistry(Path(tmp_dir) / "registry.sqlite3")
                registry.initialize(1001, -10001)
                registry.set_responsible_contact(
                    1001, "assembly", "Ведущий", "Анна", 2002, "anna_lead"
                )
                reminder = registry.list_group_reminders("assembly")[0]
                registry.set_group_reminder_task(1001, reminder["id"], "Ведущий")
                bot = SimpleNamespace(send_message=AsyncMock())
                logger = MagicMock()
                run_date = date(2026, 10, 11) - timedelta(
                    days=reminder["reminder_days"]
                )
                assignments = {
                    "Ведущий": {"names": ["Анна"], "is_informational": False},
                    "Проповедь": {"names": ["Иван"], "is_informational": False},
                }
                with patch.object(
                    app, "_load_service_task_assignments",
                    new=AsyncMock(return_value=(assignments, True)),
                ):
                    await app._process_service_reminder(
                        bot, SimpleNamespace(admin_tg_id=1001), Path(tmp_dir),
                        ZoneInfo("UTC"), registry, "assembly", reminder["id"],
                        logger, logger, run_date=run_date,
                    )
                group_text = next(
                    call.args[1] for call in bot.send_message.await_args_list
                    if call.args[0] == -10001
                )
                self.assertIn("Ведущий:", group_text)
                self.assertIn("anna_lead", group_text)
                self.assertNotIn("Иван", group_text)

                other_date = date(2026, 10, 18) - timedelta(
                    days=reminder["reminder_days"]
                )
                info_only = {"Праздник": {"names": [], "is_informational": True}}
                with patch.object(
                    app, "_load_service_task_assignments",
                    new=AsyncMock(side_effect=[
                        ({"Проповедь": ["Иван"]}, True),
                        (info_only, True),
                    ]),
                ), patch.object(
                    app, "_notify_admin", new=AsyncMock(return_value=True)
                ) as notify_admin:
                    for _ in range(2):
                        await app._process_service_reminder(
                            bot, SimpleNamespace(admin_tg_id=1001),
                            Path(tmp_dir), ZoneInfo("UTC"), registry,
                            "assembly", reminder["id"], logger, logger,
                            run_date=other_date,
                        )
                warnings = [
                    call.args[2] for call in notify_admin.await_args_list
                    if "никто не назначен" in call.args[2]
                ]
                self.assertEqual(len(warnings), 1)
                self.assertFalse(
                    registry.is_group_reminder_delivered(
                        reminder["id"], "2026-10-18", "group:-10001"
                    )
                )

        asyncio.run(process())

    def test_informational_only_service_reminder_is_not_sent_or_recorded(self):
        from bot import app
        from bot.service_registry import ServiceRegistry

        async def process():
            with tempfile.TemporaryDirectory() as tmp_dir:
                registry = ServiceRegistry(Path(tmp_dir) / "registry.sqlite3")
                registry.initialize(1001, -10001)
                reminder = registry.get_service("assembly")["reminders"][0]
                bot = SimpleNamespace(send_message=AsyncMock())
                logger = MagicMock()
                event_date = date(2026, 10, 11)
                assignments = {
                    "Праздник": {
                        "names": [],
                        "is_informational": True,
                    }
                }
                with patch.object(
                    app,
                    "_load_service_task_assignments",
                    new=AsyncMock(return_value=(assignments, True)),
                ), patch.object(
                    app, "_notify_admin", new=AsyncMock()
                ) as notify_admin:
                    await app._process_service_reminder(
                        bot,
                        SimpleNamespace(admin_tg_id=1001),
                        Path(tmp_dir),
                        ZoneInfo("UTC"),
                        registry,
                        "assembly",
                        reminder["id"],
                        logger,
                        logger,
                        run_date=(
                            event_date
                            - timedelta(days=reminder["reminder_days"])
                        ),
                    )

                bot.send_message.assert_not_awaited()
                notify_admin.assert_awaited_once()
                self.assertFalse(
                    registry.is_group_reminder_delivered(
                        reminder["id"],
                        event_date.isoformat(),
                        "group:-10001",
                    )
                )

        asyncio.run(process())

    def test_opted_out_user_is_skipped_but_group_reminder_is_sent(self):
        from bot import app
        from bot.service_registry import ServiceRegistry

        async def process():
            with tempfile.TemporaryDirectory() as tmp_dir:
                registry = ServiceRegistry(Path(tmp_dir) / "registry.sqlite3")
                registry.initialize(1001, -10001)
                registry.set_responsible_contact(
                    1001, "assembly", "Камеры", "Анна", 2002, "anna"
                )
                reminder = registry.get_service("assembly")["reminders"][0]
                bot = SimpleNamespace(send_message=AsyncMock())
                credentials = SimpleNamespace(admin_tg_id=1001)
                logger = MagicMock()
                state_cache = SimpleNamespace(state={}, save=MagicMock())
                assignments = {"Камеры": ["Анна"]}
                with patch.object(
                    app,
                    "_load_service_task_assignments",
                    new=AsyncMock(return_value=(assignments, True)),
                ):
                    await app._process_service_reminder(
                        bot,
                        credentials,
                        Path(tmp_dir),
                        ZoneInfo("UTC"),
                        registry,
                        "assembly",
                        reminder["id"],
                        logger,
                        logger,
                        run_date=(
                            date(2026, 10, 11)
                            - timedelta(days=reminder["reminder_days"])
                        ),
                        state_cache=state_cache,
                    )

                self.assertEqual(len(group_sends(bot)), 1)
                self.assertEqual(
                    group_sends(bot)[-1].args[0], -10001
                )
                self.assertTrue(
                    registry.is_group_reminder_delivered(
                        reminder["id"],
                        "2026-10-11",
                        "group:-10001",
                    )
                )
                self.assertFalse(
                    registry.is_group_reminder_delivered(
                        reminder["id"],
                        "2026-10-11",
                        "personal:Камеры:2002",
                    )
                )
                logger.info.assert_called()

        asyncio.run(process())

    def test_reminder_moved_during_download_is_not_marked(self):
        from bot import app
        from bot.service_registry import ServiceRegistry

        async def process():
            with tempfile.TemporaryDirectory() as tmp_dir:
                registry = ServiceRegistry(Path(tmp_dir) / "registry.sqlite3")
                registry.initialize(1001, -10001)
                reminder = registry.get_service("assembly")["reminders"][0]
                bot = SimpleNamespace(
                    send_message=AsyncMock(return_value=SimpleNamespace(message_id=1)),
                    edit_message_text=AsyncMock(),
                )

                async def load(*args, **kwargs):
                    # Админ меняет время, пока скачивается график.
                    registry.update_group_reminder(1001, reminder["id"], time="23:58")
                    return {"Камеры": ["Анна"]}, True

                with patch.object(app, "_load_service_task_assignments", new=load):
                    await app._process_service_reminder(
                        bot, SimpleNamespace(admin_tg_id=1001), Path(tmp_dir),
                        ZoneInfo("UTC"), registry, "assembly", reminder["id"],
                        MagicMock(), MagicMock(),
                        run_date=(
                            date(2026, 10, 11)
                            - timedelta(days=reminder["reminder_days"])
                        ),
                        state_cache=SimpleNamespace(state={}, save=MagicMock()),
                    )
                self.assertEqual(group_sends(bot), [])
                self.assertFalse(registry.is_group_reminder_delivered(
                    reminder["id"], "2026-10-11", "group:-10001"
                ))

        asyncio.run(process())

    def test_failed_admin_report_is_retried_without_repeating_group(self):
        from bot import app
        from bot.service_registry import ServiceRegistry

        async def process():
            with tempfile.TemporaryDirectory() as tmp_dir:
                registry = ServiceRegistry(Path(tmp_dir) / "registry.sqlite3")
                registry.initialize(1001, -10001)
                reminder = registry.get_service("assembly")["reminders"][0]
                admin_fails = {"value": True}

                async def send_message(chat_id, text, **kwargs):
                    if chat_id == 1001 and admin_fails["value"]:
                        raise RuntimeError("blocked")
                    return SimpleNamespace(message_id=70)

                bot = SimpleNamespace(
                    send_message=AsyncMock(side_effect=send_message),
                    edit_message_text=AsyncMock(),
                )
                logger = MagicMock()

                async def run_reminder():
                    await app._process_service_reminder(
                        bot, SimpleNamespace(admin_tg_id=1001), Path(tmp_dir),
                        ZoneInfo("UTC"), registry, "assembly", reminder["id"],
                        logger, logger,
                        run_date=(
                            date(2026, 10, 11)
                            - timedelta(days=reminder["reminder_days"])
                        ),
                        state_cache=SimpleNamespace(state={}, save=MagicMock()),
                    )

                with patch.object(
                    app,
                    "_load_service_task_assignments",
                    new=AsyncMock(return_value=({"Камеры": ["Анна"]}, True)),
                ):
                    await run_reminder()
                    self.assertEqual(
                        registry.list_admin_reports("assembly", "2026-10-11"), []
                    )
                    admin_fails["value"] = False
                    await run_reminder()

                self.assertEqual(len(group_sends(bot)), 1)
                self.assertEqual(
                    len(registry.list_admin_reports("assembly", "2026-10-11")), 1
                )

        asyncio.run(process())

    def test_user_text_with_three_schedules_sends_once_per_enabled_rule(self):
        from bot import app
        from bot.service_registry import ServiceRegistry

        async def process():
            with tempfile.TemporaryDirectory() as tmp_dir:
                registry = ServiceRegistry(Path(tmp_dir) / "registry.sqlite3")
                registry.initialize(1001, -10001)
                registry.set_group_chat(1001, "sound", -10055, enabled=True)
                registry.set_responsible_contact(
                    1001, "sound", "Микшер", "Алексей", 2002, "alex"
                )
                registry.set_user_notifications_enabled(2002, "sound", True)
                text_id = registry.upsert_user_text(
                    2002,
                    "sound",
                    "Микшер",
                    "Личное: {name}, {task}, {service_name}, {sunday_date}",
                )
                schedules = [
                    registry.add_schedule(text_id, days, time)
                    for days, time in (
                        (3, "09:00"),
                        (1, "18:00"),
                        (0, "07:00"),
                    )
                ]
                bot = SimpleNamespace(
                    send_message=AsyncMock(
                        return_value=SimpleNamespace(message_id=55)
                    )
                )
                credentials = SimpleNamespace(admin_tg_id=1001)
                logger = MagicMock()
                assignments = {"Микшер": ["Алексей"]}
                with patch.object(
                    app,
                    "_load_service_task_assignments",
                    new=AsyncMock(return_value=(assignments, True)),
                ) as load_assignments:
                    for schedule in schedules:
                        await app._process_user_reminder_schedule(
                            bot,
                            credentials,
                            Path(tmp_dir),
                            ZoneInfo("UTC"),
                            registry,
                            schedule["id"],
                            logger,
                            logger,
                            run_date=(
                                date(2026, 10, 18)
                                - timedelta(days=schedule["reminder_days"])
                            ),
                        )
                    await app._process_user_reminder_schedule(
                        bot,
                        credentials,
                        Path(tmp_dir),
                        ZoneInfo("UTC"),
                        registry,
                        schedules[0]["id"],
                        logger,
                        logger,
                        run_date=date(2026, 10, 15),
                    )

                self.assertEqual(bot.send_message.await_count, 3)
                self.assertTrue(
                    all(
                        call.args[0] == 2002
                        for call in bot.send_message.await_args_list
                    )
                )
                self.assertEqual(
                    bot.send_message.await_args_list[0].args[1],
                    "<b>🔔 Звук · Микшер · 18.10.2026</b>\n"
                    "Личное: Алексей, Микшер, Звук, 18.10.2026",
                )
                self.assertEqual(load_assignments.await_count, 3)

        asyncio.run(process())

    def test_user_reminder_has_answer_buttons_and_stops_after_answer(self):
        from bot import app
        from bot.service_registry import ServiceRegistry

        async def process():
            with tempfile.TemporaryDirectory() as tmp_dir:
                registry = ServiceRegistry(Path(tmp_dir) / "registry.sqlite3")
                registry.initialize(1001, -10001)
                registry.set_group_chat(1001, "sound", -10055, enabled=True)
                registry.set_responsible_contact(
                    1001, "sound", "звук", "Алексей", 2002, "alex"
                )
                registry.set_user_notifications_enabled(2002, "sound", True)
                text_id = registry.upsert_user_text(
                    2002, "sound", "звук", "Личное: {name}"
                )
                first = registry.add_schedule(text_id, 3, "09:00")
                second = registry.add_schedule(text_id, 1, "18:00")
                bot = SimpleNamespace(
                    send_message=AsyncMock(
                        return_value=SimpleNamespace(message_id=55)
                    )
                )
                logger = MagicMock()

                async def remind(schedule):
                    await app._process_user_reminder_schedule(
                        bot, SimpleNamespace(admin_tg_id=1001), Path(tmp_dir),
                        ZoneInfo("UTC"), registry, schedule["id"], logger,
                        logger,
                        run_date=(
                            date(2026, 10, 18)
                            - timedelta(days=schedule["reminder_days"])
                        ),
                    )

                with patch.object(
                    app,
                    "_load_service_task_assignments",
                    new=AsyncMock(return_value=({"звук": ["Алексей"]}, True)),
                ):
                    await remind(first)
                    markup = bot.send_message.await_args.kwargs["reply_markup"]
                    labels = [b.text for b in markup.inline_keyboard[0]]
                    self.assertEqual(labels, ["✅ Принял", "🚫 Не смогу"])
                    registry.set_confirmation(
                        "sound", "2026-10-18", "звук", 2002, "declined"
                    )
                    await remind(second)

                self.assertEqual(bot.send_message.await_count, 1)

        asyncio.run(process())

    def test_oversized_personal_reminder_is_shortened_not_dropped(self):
        from bot import app
        from bot.service_registry import ServiceRegistry

        async def process():
            with tempfile.TemporaryDirectory() as tmp_dir:
                registry = ServiceRegistry(Path(tmp_dir) / "registry.sqlite3")
                registry.initialize(1001, -10001)
                registry.set_group_chat(1001, "sound", -10055, enabled=True)
                registry.set_responsible_contact(
                    1001, "sound", "звук", "Алексей" * 30, 2002
                )
                registry.set_user_notifications_enabled(2002, "sound", True)
                text_id = registry.ensure_user_text(2002, "sound", "звук")
                schedule = registry.add_schedule(
                    text_id, 1, "18:00", origin="user", body="{name} " * 300
                )
                bot = SimpleNamespace(
                    send_message=AsyncMock(return_value=SimpleNamespace(message_id=5))
                )
                with patch.object(
                    app, "_load_service_task_assignments",
                    new=AsyncMock(return_value=({"звук": ["Алексей" * 30]}, True)),
                ):
                    await app._process_user_reminder_schedule(
                        bot, SimpleNamespace(admin_tg_id=1001), Path(tmp_dir),
                        ZoneInfo("UTC"), registry, schedule["id"], MagicMock(),
                        MagicMock(), run_date=date(2026, 10, 17),
                    )
                sent = [c for c in bot.send_message.await_args_list if c.args[0] == 2002]
                self.assertEqual(len(sent), 1)
                self.assertIn("…", sent[0].args[1])

        asyncio.run(process())

    def test_user_reminder_is_skipped_when_task_becomes_informational(self):
        from bot import app
        from bot.service_registry import ServiceRegistry

        async def process():
            with tempfile.TemporaryDirectory() as tmp_dir:
                registry = ServiceRegistry(Path(tmp_dir) / "registry.sqlite3")
                registry.initialize(1001, -10001)
                registry.set_group_chat(1001, "sound", -10055, enabled=True)
                registry.set_responsible_contact(
                    1001, "sound", "Праздник", "Дежурный", 2002
                )
                registry.set_user_notifications_enabled(2002, "sound", True)
                text_id = registry.upsert_user_text(
                    2002, "sound", "Праздник", "Не отправлять {name}"
                )
                schedule = registry.add_schedule(text_id, 1, "18:00")
                bot = SimpleNamespace(send_message=AsyncMock())
                logger = MagicMock()
                info_assignments = {
                    "Праздник": {
                        "names": ["Дежурный"],
                        "is_informational": True,
                    }
                }
                with patch.object(
                    app,
                    "_load_service_task_assignments",
                    new=AsyncMock(return_value=(info_assignments, True)),
                ):
                    await app._process_user_reminder_schedule(
                        bot,
                        SimpleNamespace(admin_tg_id=1001),
                        Path(tmp_dir),
                        ZoneInfo("UTC"),
                        registry,
                        schedule["id"],
                        logger,
                        logger,
                        run_date=date(2026, 10, 17),
                    )

                bot.send_message.assert_not_awaited()
                self.assertFalse(
                    registry.is_user_reminder_delivered(
                        schedule["id"], "2026-10-18"
                    )
                )
                logger.debug.assert_any_call(
                    "Личное расписание %s пропущено: %s",
                    schedule["id"],
                    "информационная задача — без личных уведомлений",
                )

        asyncio.run(process())

    def test_scheduler_initialization_failure_closes_bot_without_pid(self):
        from bot import app

        async def run_bot():
            with tempfile.TemporaryDirectory() as tmp_dir:
                shm_dir = Path(tmp_dir) / "shm"
                log_dir = Path(tmp_dir) / "logs"
                shm_dir.mkdir(mode=0o700)
                log_dir.mkdir(mode=0o700)
                bot = SimpleNamespace(session=SimpleNamespace(close=AsyncMock()))
                with ExitStack() as stack:
                    stack.enter_context(
                        patch.object(
                            app,
                            "setup_loggers",
                            return_value=(MagicMock(), MagicMock()),
                        )
                    )
                    stack.enter_context(
                        patch.object(
                            app,
                            "load_credentials",
                            return_value=SimpleNamespace(
                                timezone="UTC",
                                bot_token="token",
                                group_chat_id=1,
                                admin_tg_id=2,
                            ),
                        )
                    )
                    stack.enter_context(
                        patch.object(
                            app,
                            "service_registry_path",
                            return_value=Path(tmp_dir) / "service-registry.sqlite3",
                        )
                    )
                    stack.enter_context(patch.object(app, "load_templates", return_value={}))
                    stack.enter_context(patch.object(app, "get_tz", return_value=ZoneInfo("UTC")))
                    stack.enter_context(patch.object(app, "StateCache"))
                    stack.enter_context(patch.object(app, "load_schedule", return_value=[]))
                    stack.enter_context(patch.object(app, "Bot", return_value=bot))
                    stack.enter_context(patch.object(app, "Dispatcher"))
                    stack.enter_context(patch.object(app, "register"))
                    stack.enter_context(
                        patch.object(
                            app,
                            "build_scheduler",
                            side_effect=RuntimeError("invalid scheduler"),
                        )
                    )
                    with self.assertRaisesRegex(RuntimeError, "invalid scheduler"):
                        await app.run(
                            SimpleNamespace(
                                shm_dir=str(shm_dir), log_dir=str(log_dir)
                            )
                        )

                bot.session.close.assert_awaited_once()
                self.assertFalse((shm_dir / "reminder_bot_bot.pid").exists())

    def test_corrected_schedule_retries_only_new_deliveries(self):
        from bot import app
        from bot.service_registry import ServiceRegistry
        from bot.state import StateCache

        async def process():
            with tempfile.TemporaryDirectory() as tmp_dir:
                cache = StateCache(Path(tmp_dir) / "state.json")
                registry = ServiceRegistry(Path(tmp_dir) / "registry.sqlite3")
                registry.initialize(12345, -100)
                for task, (name, telegram_id) in {
                    "Ответственный": ("Алексей", 101),
                    "Ведущий": ("Павел", 102),
                    "Проповедник": ("Иван", 103),
                }.items():
                    registry.set_responsible_contact(
                        12345, "assembly", task, name, telegram_id
                    )
                    registry.set_user_notifications_enabled(
                        telegram_id, "assembly", True
                    )
                bot = SimpleNamespace(
                    send_message=AsyncMock(
                        side_effect=[
                            SimpleNamespace(message_id=10),
                            SimpleNamespace(message_id=11, chat=SimpleNamespace(id=101)),
                            SimpleNamespace(message_id=12, chat=SimpleNamespace(id=102)),
                            SimpleNamespace(message_id=13, chat=SimpleNamespace(id=103)),
                        ]
                    ),
                    edit_message_text=AsyncMock(),
                )
                credentials = SimpleNamespace(
                    admin_tg_id=12345,
                    group_chat_id=-100,
                    yandex_public_url="https://example.invalid/schedule",
                )
                partial = {"Ответственный": "Алексей"}
                complete = {
                    "Ответственный": "Алексей",
                    "Ведущий": "Павел",
                    "Проповедник": "Иван",
                }
                contacts = {
                    "Ответственный": {
                        "Алексей": {"username": "alex", "tg_id": 101}
                    },
                    "Ведущий": {
                        "Павел": {"username": "pavel", "tg_id": 102}
                    },
                    "Проповедник": {
                        "Иван": {"username": "ivan", "tg_id": 103}
                    },
                }
                with ExitStack() as stack:
                    stack.enter_context(
                        patch.object(
                            app, "_next_sunday_in_tz",
                            return_value=date(2026, 10, 11),
                        )
                    )
                    stack.enter_context(
                        patch.object(app, "download_public", new=AsyncMock())
                    )
                    parse_schedule = stack.enter_context(
                        patch.object(
                            app, "parse_schedule", side_effect=[partial, complete]
                        )
                    )
                    stack.enter_context(
                        patch.object(app, "parse_people", return_value=contacts)
                    )
                    stack.enter_context(
                        patch.object(app, "_notify_admin", new=AsyncMock())
                    )
                    stack.enter_context(
                        patch("bot.notifier.asyncio.sleep", new=AsyncMock())
                    )
                    for _ in range(3):
                        await app._process_upcoming_sunday(
                            bot=bot,
                            credentials=credentials,
                            templates={
                                "general": (
                                    "Ответственный: @{tg_responsible}; "
                                    "Ведущий: @{tg_leader}; "
                                    "Проповедник: @{tg_preacher}"
                                ),
                                "roles": {
                                    role: "{name}"
                                    for role in complete
                                },
                            },
                            state_cache=cache,
                            shm_dir=Path(tmp_dir),
                            interaction_logger=MagicMock(),
                            debug_logger=MagicMock(),
                            tz=ZoneInfo("UTC"),
                            service_registry=registry,
                        )

                self.assertEqual(parse_schedule.call_count, 2)
                self.assertEqual(bot.send_message.await_count, 1)
                bot.edit_message_text.assert_awaited_once()
                self.assertTrue(cache.state["complete"])
                self.assertTrue(cache.matches_week("2026-10-11"))
                self.assertEqual(
                    cache.state["participants"]["Ответственный"]["message_id"],
                    None,
                )

        asyncio.run(process())

    def test_disabled_assembly_skips_scheduled_delivery(self):
        from bot import app
        from bot.service_registry import ServiceRegistry

        async def process():
            with tempfile.TemporaryDirectory() as tmp_dir:
                registry = ServiceRegistry(Path(tmp_dir) / "registry.sqlite3")
                registry.initialize(1001, -100)
                registry.set_group_chat(1001, "assembly", -100, enabled=False)
                bot = SimpleNamespace(send_message=AsyncMock())
                credentials = SimpleNamespace(
                    admin_tg_id=1001,
                    group_chat_id=-100,
                    yandex_public_url="https://example.invalid/schedule",
                )
                with ExitStack() as stack:
                    download = stack.enter_context(
                        patch.object(app, "download_public", new=AsyncMock())
                    )
                    send = stack.enter_context(
                        patch.object(app, "send_assignments", new=AsyncMock())
                    )
                    interaction_logger = MagicMock()
                    await app._process_upcoming_sunday(
                        bot=bot,
                        credentials=credentials,
                        templates={},
                        state_cache=MagicMock(),
                        shm_dir=Path(tmp_dir),
                        interaction_logger=interaction_logger,
                        debug_logger=MagicMock(),
                        tz=ZoneInfo("UTC"),
                        service_registry=registry,
                    )

                download.assert_not_awaited()
                send.assert_not_awaited()
                interaction_logger.info.assert_called_once()

        asyncio.run(process())

    def test_scheduled_assembly_delivery_uses_registry_chat_id(self):
        from bot import app
        from bot.service_registry import ServiceRegistry

        async def process():
            with tempfile.TemporaryDirectory() as tmp_dir:
                registry = ServiceRegistry(Path(tmp_dir) / "registry.sqlite3")
                registry.initialize(1001, -100)
                registry.set_group_chat(1001, "assembly", -10055, enabled=True)
                state_cache = SimpleNamespace(
                    state=None,
                    matches_week=MagicMock(return_value=False),
                    replace=MagicMock(),
                    save=MagicMock(),
                )
                credentials = SimpleNamespace(
                    admin_tg_id=1001,
                    group_chat_id=-100,
                    yandex_public_url="https://example.invalid/schedule",
                )
                assignments = {
                    "Ответственный": "Алексей",
                    "Ведущий": "Павел",
                    "Проповедник": "Иван",
                }
                people = {
                    role: {
                        name: {"username": "", "tg_id": index}
                    }
                    for index, (role, name) in enumerate(assignments.items(), 1)
                }
                with ExitStack() as stack:
                    stack.enter_context(
                        patch.object(
                            app, "_next_sunday_in_tz",
                            return_value=date(2026, 10, 18),
                        )
                    )
                    stack.enter_context(
                        patch.object(app, "download_public", new=AsyncMock())
                    )
                    stack.enter_context(
                        patch.object(
                            app, "parse_schedule", return_value=assignments
                        )
                    )
                    stack.enter_context(
                        patch.object(app, "parse_people", return_value=people)
                    )
                    send_assignments = stack.enter_context(
                        patch.object(
                            app, "send_assignments", new=AsyncMock(return_value=False)
                        )
                    )
                    await app._process_upcoming_sunday(
                        bot=SimpleNamespace(),
                        credentials=credentials,
                        templates={"general": "", "roles": {}},
                        state_cache=state_cache,
                        shm_dir=Path(tmp_dir),
                        interaction_logger=MagicMock(),
                        debug_logger=MagicMock(),
                        tz=ZoneInfo("UTC"),
                        service_registry=registry,
                    )

                self.assertEqual(
                    send_assignments.await_args.kwargs["group_chat_id"], -10055
                )

        asyncio.run(process())

    def test_missing_assignment_data_notifies_admin_and_continues_partial_send(self):
        from bot import app

        async def process():
            bot = SimpleNamespace()
            credentials = SimpleNamespace(
                admin_tg_id=12345,
                yandex_public_url="https://example.invalid/schedule",
            )
            state_cache = SimpleNamespace(
                matches_week=MagicMock(return_value=False),
                state=None,
                replace=MagicMock(),
                save=MagicMock(),
            )
            debug_logger = MagicMock()

            with ExitStack() as stack:
                stack.enter_context(
                    patch.object(app, "_next_sunday_in_tz", return_value=date(2026, 10, 11))
                )
                stack.enter_context(patch.object(app, "download_public", new=AsyncMock()))
                stack.enter_context(
                    patch.object(
                        app,
                        "parse_schedule",
                        return_value={
                            "Ответственный": "Найденный",
                            "Ведущий": "Без ID",
                        },
                    )
                )
                stack.enter_context(
                    patch.object(
                        app,
                        "parse_people",
                        return_value={
                            "Ответственный": {
                                "Найденный": {"username": "found", "tg_id": 98765},
                            },
                            "Ведущий": {
                                "Без ID": {"username": "", "tg_id": None},
                            },
                        },
                    )
                )
                stack.enter_context(patch.object(app, "_notify_admin", new=AsyncMock()))
                stack.enter_context(patch.object(app, "send_assignments", new=AsyncMock()))

                await app._process_upcoming_sunday(
                    bot=bot,
                    credentials=credentials,
                    templates={},
                    state_cache=state_cache,
                    shm_dir=Path("/tmp"),
                    interaction_logger=MagicMock(),
                    debug_logger=debug_logger,
                    tz=ZoneInfo("UTC"),
                )

                app._notify_admin.assert_awaited_once()
                args = app._notify_admin.await_args.args
                self.assertIs(args[0], bot)
                self.assertEqual(args[1], credentials.admin_tg_id)
                self.assertIn("11.10.2026", args[2])
                self.assertIn("отсутствует Telegram_ID", args[2])
                app.send_assignments.assert_awaited_once()
                self.assertTrue(
                    app.send_assignments.await_args.kwargs[
                        "send_personal_notifications"
                    ]
                )
                self.assertIsNone(
                    app.send_assignments.await_args.kwargs[
                        "user_notifications_enabled"
                    ]
                )
                sent_assignments = app.send_assignments.await_args.kwargs["assignments"]
                self.assertEqual(
                    list(sent_assignments), ["Ответственный", "Ведущий"]
                )
                self.assertIsNone(sent_assignments["Ведущий"]["tg_id"])

    @NEEDS_PROC
    def test_existing_pid_file_stops_startup_with_clear_error(self):
        from bot import app

        async def run_bot():
            with tempfile.TemporaryDirectory() as tmp_dir:
                shm_dir = Path(tmp_dir) / "shm"
                log_dir = Path(tmp_dir) / "logs"
                shm_dir.mkdir(mode=0o700)
                log_dir.mkdir(mode=0o700)
                pid_file = shm_dir / "reminder_bot_bot.pid"
                from bot.app import _current_process_starttime
                pid_file.write_text(
                    f"{os.getpid()}:{_current_process_starttime()}",
                    encoding="utf-8",
                )
                debug_logger = MagicMock()

                with ExitStack() as stack:
                    stack.enter_context(
                        patch.object(
                            app,
                            "setup_loggers",
                            return_value=(MagicMock(), debug_logger),
                        )
                    )
                    stack.enter_context(
                        patch.object(
                            app,
                            "load_credentials",
                            return_value=SimpleNamespace(
                                timezone="UTC",
                                bot_token="token",
                                group_chat_id=1,
                                admin_tg_id=2,
                            ),
                        )
                    )
                    stack.enter_context(
                        patch.object(
                            app,
                            "service_registry_path",
                            return_value=Path(tmp_dir) / "service-registry.sqlite3",
                        )
                    )
                    stack.enter_context(patch.object(app, "load_templates", return_value={}))
                    stack.enter_context(patch.object(app, "get_tz", return_value=ZoneInfo("UTC")))
                    stack.enter_context(patch.object(app, "StateCache"))
                    stack.enter_context(patch.object(
                        app,
                        "Bot",
                        return_value=SimpleNamespace(
                            session=SimpleNamespace(close=AsyncMock())
                        ),
                    ))
                    stack.enter_context(patch.object(app, "Dispatcher"))
                    stack.enter_context(patch.object(app, "register"))
                    stack.enter_context(patch.object(app, "load_schedule", return_value=[]))
                    stack.enter_context(patch.object(
                        app,
                        "build_scheduler",
                        return_value=SimpleNamespace(running=False),
                    ))
                    with self.assertRaisesRegex(FileExistsError, "PID-файл уже существует"):
                        await app.run(
                            SimpleNamespace(shm_dir=str(shm_dir), log_dir=str(log_dir))
                        )

                debug_logger.error.assert_called_once()
                self.assertIn("PID-файл уже существует", debug_logger.error.call_args.args[0])
                self.assertEqual(
                    pid_file.read_text(encoding="utf-8"),
                    f"{os.getpid()}:{_current_process_starttime()}",
                )

        asyncio.run(run_bot())

    def test_existing_pid_file_status_detects_stale_process(self):
        from bot.app import _existing_pid_process_status

        with tempfile.TemporaryDirectory() as tmp_dir:
            pid_file = Path(tmp_dir) / "bot.pid"
            pid_file.write_text("999999999:1", encoding="utf-8")
            os.chmod(pid_file, 0o600)

            self.assertEqual(
                _existing_pid_process_status(pid_file),
                ("999999999", False),
            )

    @NEEDS_PROC
    def test_existing_pid_file_status_detects_live_process(self):
        from bot.app import _current_process_starttime, _existing_pid_process_status

        with tempfile.TemporaryDirectory() as tmp_dir:
            pid_file = Path(tmp_dir) / "bot.pid"
            pid_file.write_text(
                f"{os.getpid()}:{_current_process_starttime()}",
                encoding="utf-8",
            )
            os.chmod(pid_file, 0o600)

            self.assertEqual(
                _existing_pid_process_status(pid_file),
                (str(os.getpid()), True),
            )

    @NEEDS_PROC
    def test_shutdown_runs_once_after_polling_stops(self):
        from bot import app

        async def run_bot():
            with tempfile.TemporaryDirectory() as tmp_dir:
                shm_dir = Path(tmp_dir) / "shm"
                log_dir = Path(tmp_dir) / "logs"
                shm_dir.mkdir(mode=0o700)
                log_dir.mkdir(mode=0o700)
                bot = SimpleNamespace(session=SimpleNamespace(close=AsyncMock()))
                dispatcher = SimpleNamespace()
                debug_logger = MagicMock()
                scheduler = SimpleNamespace(
                    running=True,
                    start=MagicMock(),
                    pause=MagicMock(),
                    shutdown=MagicMock(),
                )

                async def start_polling(_bot, **_kwargs):
                    return None

                dispatcher.start_polling = AsyncMock(side_effect=start_polling)
                with ExitStack() as stack:
                    stack.enter_context(
                        patch.object(
                            app,
                            "setup_loggers",
                            return_value=(MagicMock(), debug_logger),
                        )
                    )
                    stack.enter_context(patch.object(
                        app,
                        "load_credentials",
                        return_value=SimpleNamespace(
                            timezone="UTC",
                            bot_token="token",
                            group_chat_id=1,
                            admin_tg_id=2,
                        ),
                    ))
                    stack.enter_context(patch.object(
                        app,
                        "service_registry_path",
                        return_value=Path(tmp_dir) / "service-registry.sqlite3",
                    ))
                    stack.enter_context(patch.object(app, "load_templates", return_value={}))
                    stack.enter_context(patch.object(app, "get_tz", return_value=ZoneInfo("UTC")))
                    stack.enter_context(patch.object(app, "StateCache"))
                    stack.enter_context(patch.object(app, "Bot", return_value=bot))
                    stack.enter_context(patch.object(app, "Dispatcher", return_value=dispatcher))
                    register = stack.enter_context(patch.object(app, "register"))
                    stack.enter_context(
                        patch.object(
                            app.ServiceRegistry,
                            "initialize",
                            side_effect=PermissionError("invalid registry permissions"),
                        )
                    )
                    stack.enter_context(patch.object(app, "load_schedule", return_value=[]))
                    stack.enter_context(patch.object(app, "build_scheduler", return_value=scheduler))
                    await app.run(SimpleNamespace(shm_dir=str(shm_dir), log_dir=str(log_dir)))

                scheduler.pause.assert_called_once_with()
                scheduler.shutdown.assert_called_once_with(wait=False)
                bot.session.close.assert_awaited_once()
                dispatcher.start_polling.assert_awaited_once_with(
                    bot, close_bot_session=False
                )
                self.assertIsNone(register.call_args.args[1].service_registry)
                debug_logger.exception.assert_called_once()
                self.assertFalse((shm_dir / "reminder_bot_bot.pid").exists())

        asyncio.run(run_bot())

    def test_wait_for_telegram_retries_until_get_me_succeeds(self):
        from aiogram.exceptions import TelegramNetworkError, TelegramServerError

        from bot import app

        bot = SimpleNamespace(
            me=AsyncMock(
                side_effect=[
                    TelegramNetworkError(method=MagicMock(), message="DNS"),
                    TelegramServerError(method=MagicMock(), message="Bad Gateway"),
                    None,
                ]
            )
        )
        logger = MagicMock()
        with patch.object(app, "TELEGRAM_RETRY_FIRST_DELAY", 0.01):
            self.assertTrue(asyncio.run(app._wait_for_telegram(bot, logger)))

        self.assertEqual(bot.me.await_count, 3)
        self.assertEqual(logger.warning.call_count, 2)

    def test_wait_for_telegram_stops_on_sigterm(self):
        """manage.sh stop во время ожидания: выход без getMe и с очисткой."""
        import signal

        from aiogram.exceptions import TelegramNetworkError

        from bot import app

        bot = SimpleNamespace(
            me=AsyncMock(side_effect=TelegramNetworkError(method=MagicMock(), message="DNS"))
        )

        async def scenario():
            asyncio.get_running_loop().call_later(
                0.05, os.kill, os.getpid(), signal.SIGTERM
            )
            return await asyncio.wait_for(
                app._wait_for_telegram(bot, MagicMock()), timeout=5
            )

        self.assertFalse(asyncio.run(scenario()))
        self.assertEqual(bot.me.await_count, 1)
        self.assertIs(signal.getsignal(signal.SIGTERM), signal.SIG_DFL)

    def test_poll_timer_failure_does_not_stop_startup(self):
        from bot import app

        registry = SimpleNamespace(list_open_replacement_polls=lambda: [
            {"poll_id": "a", "deadline": "2099-01-03T10:00:00+00:00"},
            {"poll_id": "b", "deadline": "2099-01-03T11:00:00+00:00"},
        ])
        schedule = MagicMock(side_effect=[RuntimeError("scheduler"), None])
        logger = MagicMock()
        app._reschedule_open_polls(registry, schedule, logger)
        self.assertEqual([call.args[0] for call in schedule.call_args_list], ["a", "b"])
        logger.exception.assert_called_once()

    @NEEDS_PROC
    def test_command_menu_sync_does_not_block_startup(self):
        """Без DNS обновление меню висит, но PID-файл и опрос уже работают."""
        from bot import app

        async def run_bot():
            with tempfile.TemporaryDirectory() as tmp_dir:
                shm_dir = Path(tmp_dir) / "shm"
                log_dir = Path(tmp_dir) / "logs"
                shm_dir.mkdir(mode=0o700)
                log_dir.mkdir(mode=0o700)
                bot = SimpleNamespace(
                    session=SimpleNamespace(close=AsyncMock()),
                    set_my_commands=AsyncMock(),
                )
                dispatcher = SimpleNamespace()
                scheduler = SimpleNamespace(
                    running=True,
                    start=MagicMock(),
                    pause=MagicMock(),
                    shutdown=MagicMock(),
                )
                sync_started = asyncio.Event()
                sync_cancelled = asyncio.Event()
                pid_seen = []

                async def hanging_sync(*_args):
                    sync_started.set()
                    try:
                        await asyncio.Event().wait()
                    except asyncio.CancelledError:
                        sync_cancelled.set()
                        raise

                async def start_polling(_bot, **_kwargs):
                    pid_seen.append((shm_dir / "reminder_bot_bot.pid").exists())
                    await sync_started.wait()

                dispatcher.start_polling = AsyncMock(side_effect=start_polling)
                with ExitStack() as stack:
                    stack.enter_context(patch.object(
                        app, "setup_loggers", return_value=(MagicMock(), MagicMock()),
                    ))
                    stack.enter_context(patch.object(
                        app,
                        "load_credentials",
                        return_value=SimpleNamespace(
                            timezone="UTC",
                            bot_token="token",
                            group_chat_id=1,
                            admin_tg_id=2,
                        ),
                    ))
                    stack.enter_context(patch.object(
                        app,
                        "service_registry_path",
                        return_value=Path(tmp_dir) / "service-registry.sqlite3",
                    ))
                    stack.enter_context(patch.object(app, "load_templates", return_value={}))
                    stack.enter_context(patch.object(app, "get_tz", return_value=ZoneInfo("UTC")))
                    stack.enter_context(patch.object(app, "StateCache"))
                    stack.enter_context(patch.object(app, "Bot", return_value=bot))
                    stack.enter_context(patch.object(app, "Dispatcher", return_value=dispatcher))
                    stack.enter_context(patch.object(app, "register"))
                    stack.enter_context(patch.object(
                        app.ServiceRegistry,
                        "initialize",
                        side_effect=PermissionError("invalid registry permissions"),
                    ))
                    stack.enter_context(patch.object(app, "load_schedule", return_value=[]))
                    stack.enter_context(patch.object(app, "build_scheduler", return_value=scheduler))
                    stack.enter_context(patch.object(
                        app, "sync_admin_commands", side_effect=hanging_sync,
                    ))
                    stack.enter_context(patch.object(
                        app, "_configure_webapp_menu", AsyncMock(),
                    ))
                    await asyncio.wait_for(
                        app.run(SimpleNamespace(shm_dir=str(shm_dir), log_dir=str(log_dir))),
                        timeout=5,
                    )

                self.assertEqual(pid_seen, [True])
                self.assertTrue(sync_cancelled.is_set())

        asyncio.run(run_bot())

    @NEEDS_PROC
    def test_write_pid_creates_private_file(self):
        from bot.app import _current_process_starttime, _write_pid

        with tempfile.TemporaryDirectory() as tmp_dir:
            pid_path = Path(tmp_dir) / "bot.pid"
            _write_pid(pid_path)

            pid, starttime = pid_path.read_text(encoding="utf-8").split(":")
            self.assertEqual(pid, str(os.getpid()))
            self.assertTrue(starttime.isdigit())
            self.assertEqual(starttime, _current_process_starttime())
            self.assertEqual(stat.S_IMODE(pid_path.stat().st_mode), 0o600)

    def test_write_pid_does_not_follow_symlink(self):
        from bot.app import _write_pid

        with tempfile.TemporaryDirectory() as tmp_dir:
            target_path = Path(tmp_dir) / "target"
            target_path.write_text("unchanged", encoding="utf-8")
            pid_path = Path(tmp_dir) / "bot.pid"
            pid_path.symlink_to(target_path)

            with self.assertRaises(FileExistsError):
                _write_pid(pid_path)

            self.assertEqual(target_path.read_text(encoding="utf-8"), "unchanged")

    def test_write_pid_does_not_follow_symlinked_ancestor(self):
        from bot.app import _write_pid

        with tempfile.TemporaryDirectory() as tmp_dir:
            target_dir = Path(tmp_dir) / "target"
            target_dir.mkdir()
            (target_dir / "nested").mkdir()
            linked_dir = Path(tmp_dir) / "linked"
            linked_dir.symlink_to(target_dir, target_is_directory=True)

            with self.assertRaises(OSError):
                _write_pid(linked_dir / "nested" / "bot.pid")

            self.assertFalse((target_dir / "nested" / "bot.pid").exists())

    def test_write_pid_does_not_overwrite_existing_hardlink(self):
        from bot.app import _write_pid

        with tempfile.TemporaryDirectory() as tmp_dir:
            target_path = Path(tmp_dir) / "target"
            target_path.write_text("unchanged", encoding="utf-8")
            pid_path = Path(tmp_dir) / "bot.pid"
            os.link(target_path, pid_path)

            with self.assertRaises(FileExistsError):
                _write_pid(pid_path)

            self.assertEqual(target_path.read_text(encoding="utf-8"), "unchanged")

    def test_write_pid_does_not_overwrite_existing_file(self):
        from bot.app import _write_pid

        with tempfile.TemporaryDirectory() as tmp_dir:
            pid_path = Path(tmp_dir) / "bot.pid"
            pid_path.write_text("stale-pid", encoding="utf-8")

            with self.assertRaises(FileExistsError):
                _write_pid(pid_path)

            self.assertEqual(pid_path.read_text(encoding="utf-8"), "stale-pid")

    def test_write_pid_rejects_group_writable_directory(self):
        from bot.app import _write_pid

        with tempfile.TemporaryDirectory() as tmp_dir:
            os.chmod(tmp_dir, 0o770)
            pid_path = Path(tmp_dir) / "bot.pid"

            with self.assertRaises(PermissionError):
                _write_pid(pid_path)

            self.assertFalse(pid_path.exists())

    def test_run_rejects_symlinked_runtime_directory(self):
        from bot import app

        async def run_bot():
            with tempfile.TemporaryDirectory() as tmp_dir:
                root = Path(tmp_dir)
                target = root / "target"
                target.mkdir(mode=0o700)
                linked = root / "linked"
                linked.symlink_to(target, target_is_directory=True)

                with self.assertRaises(OSError):
                    await app.run(
                        SimpleNamespace(
                            shm_dir=str(linked),
                            log_dir=str(root / "logs"),
                        )
                    )
                    stack.enter_context(
                        patch.object(
                            app,
                            "service_registry_path",
                            return_value=Path(tmp_dir) / "service-registry.sqlite3",
                        )
                    )

                self.assertFalse((target / "reminder_bot_bot.pid").exists())

        asyncio.run(run_bot())

    def test_log_files_are_private(self):
        from bot.logger import setup_loggers

        with tempfile.TemporaryDirectory() as tmp_dir:
            interaction_logger, debug_logger = setup_loggers(Path(tmp_dir))

            for log_name in ("reminder_bot_interaction.log", "reminder_bot_debug.log"):
                self.assertEqual(
                    stat.S_IMODE((Path(tmp_dir) / log_name).stat().st_mode),
                    0o600,
                )

            for logger in (interaction_logger, debug_logger):
                for handler in logger.handlers:
                    handler.close()

    def test_trace_level_only_when_enabled(self):
        from bot.cli import parse_args
        from bot.logger import TRACE, setup_loggers

        self.assertFalse(parse_args(["--shm-dir", "a", "--log-dir", "b"]).trace)
        self.assertTrue(
            parse_args(["--shm-dir", "a", "--log-dir", "b", "--trace"]).trace
        )
        for trace in (False, True):
            with tempfile.TemporaryDirectory() as tmp_dir:
                interaction_logger, debug_logger = setup_loggers(
                    Path(tmp_dir), trace=trace
                )
                try:
                    debug_logger.log(TRACE, "Нажатие 1: menu:start")
                    for handler in debug_logger.handlers:
                        handler.flush()
                    debug_log = (
                        Path(tmp_dir) / "reminder_bot_debug.log"
                    ).read_text(encoding="utf-8")
                    self.assertEqual("menu:start" in debug_log, trace)
                    if trace:
                        self.assertIn("TRACE", debug_log)
                finally:
                    for logger in (interaction_logger, debug_logger):
                        for handler in logger.handlers:
                            handler.close()

    def test_assignment_details_are_written_only_to_debug_log(self):
        from bot.logger import setup_loggers

        with tempfile.TemporaryDirectory() as tmp_dir:
            interaction_logger, debug_logger = setup_loggers(Path(tmp_dir))
            try:
                debug_logger.debug(
                    "Назначение сопоставлено: роль=%s, имя=%s, username=@%s, tg_id=%s",
                    "Ответственный", "Участник", "user", 123456789,
                )
                for handler in debug_logger.handlers:
                    handler.flush()

                interaction_log = (
                    Path(tmp_dir) / "reminder_bot_interaction.log"
                ).read_text(encoding="utf-8")
                debug_log = (
                    Path(tmp_dir) / "reminder_bot_debug.log"
                ).read_text(encoding="utf-8")

                self.assertNotIn("123456789", interaction_log)
                self.assertIn("123456789", debug_log)
            finally:
                for logger in (interaction_logger, debug_logger):
                    for handler in logger.handlers:
                        handler.close()

    def test_logger_does_not_follow_symlink(self):
        from bot.logger import INTERACTION_LOG, setup_loggers

        with tempfile.TemporaryDirectory() as tmp_dir:
            target_path = Path(tmp_dir) / "target"
            target_path.write_text("unchanged", encoding="utf-8")
            (Path(tmp_dir) / INTERACTION_LOG).symlink_to(target_path)

            with self.assertRaises(OSError):
                setup_loggers(Path(tmp_dir))

            self.assertEqual(target_path.read_text(encoding="utf-8"), "unchanged")

    def test_logger_rejects_hardlinked_log_file(self):
        from bot.logger import INTERACTION_LOG, setup_loggers

        with tempfile.TemporaryDirectory() as tmp_dir:
            target_path = Path(tmp_dir) / "target"
            target_path.write_text("unchanged", encoding="utf-8")
            os.link(target_path, Path(tmp_dir) / INTERACTION_LOG)

            with self.assertRaises(PermissionError):
                setup_loggers(Path(tmp_dir))

            self.assertEqual(target_path.read_text(encoding="utf-8"), "unchanged")

    def test_reminder_bot_entrypoint_imports(self):
        module = importlib.import_module("bot.reminder_bot")
        self.assertTrue(hasattr(module, "main"))

    def test_register_includes_router_in_dispatcher(self):
        from bot import handlers

        dp = MagicMock()
        context = SimpleNamespace(
            dispatcher=dp,
            interaction_logger=logging.getLogger("interaction"),
            debug_logger=logging.getLogger("debug"),
            state_cache=SimpleNamespace(state={}),
        )

        handlers.register(dp, context)

        self.assertEqual(dp.include_router.call_count, 2)
        self.assertIs(
            dp.include_router.call_args_list[-1].args[0], handlers.router
        )
        self.assertIsNot(
            dp.include_router.call_args_list[0].args[0], handlers.router
        )

    def test_register_requires_dispatcher(self):
        from bot import handlers

        context = SimpleNamespace(
            interaction_logger=logging.getLogger("interaction"),
            debug_logger=logging.getLogger("debug"),
            state_cache=SimpleNamespace(state={}),
        )

        with self.assertRaises(TypeError):
            handlers.register(context)

    def test_next_sunday_in_tz_skips_current_sunday(self):
        from bot.app import _next_sunday_in_tz

        tz = ZoneInfo("Europe/Moscow")

        with patch("bot.app.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 10, 11, 12, 0, 0, tzinfo=tz)
            self.assertEqual(_next_sunday_in_tz(tz), date(2026, 10, 18))

        with patch("bot.app.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 10, 12, 12, 0, 0, tzinfo=tz)
            self.assertEqual(_next_sunday_in_tz(tz), date(2026, 10, 18))


if __name__ == "__main__":
    unittest.main()
