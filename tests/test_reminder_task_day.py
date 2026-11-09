"""Решение 17, шаг 5а: напоминание служения считается от воскресенья,
напоминание задачи — от дня своей задачи (спевка — вс −4, среда)."""
import asyncio
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

from bot.scheduler import (
    in_reminder_period,
    is_reminder_event_date,
    next_reminder_occurrence,
    reminder_occurrence,
    reminder_period_status,
)
from bot.service_registry import ServiceRegistry

TZ = ZoneInfo("UTC")
SUPER = 1001
WEDNESDAY = date(2026, 10, 28)
SUNDAY = date(2026, 11, 1)


class TaskDayRulesTest(unittest.TestCase):
    def test_event_date_matches_reminder_day(self):
        common = {"task": None}
        sound = {"task": "звук", "task_day": None}
        rehearsal = {"task": "спевка", "task_day": -4}
        self.assertTrue(is_reminder_event_date(SUNDAY, common))
        self.assertFalse(is_reminder_event_date(WEDNESDAY, common))
        self.assertTrue(is_reminder_event_date(SUNDAY, sound))
        self.assertFalse(is_reminder_event_date(WEDNESDAY, sound))
        self.assertTrue(is_reminder_event_date(WEDNESDAY, rehearsal))
        self.assertFalse(is_reminder_event_date(SUNDAY, rehearsal))
        # День задачи без задачи не действует.
        self.assertFalse(
            is_reminder_event_date(WEDNESDAY, {"task": None, "task_day": -4})
        )

    def test_occurrence_from_task_day(self):
        monday = datetime(2026, 10, 26, 12, 0, tzinfo=TZ)
        self.assertEqual(
            next_reminder_occurrence(monday, 1, "18:00", day_offset=-4),
            (WEDNESDAY, date(2026, 10, 27)),
        )
        # Отправка на эту среду прошла — следующая среда.
        late = datetime(2026, 10, 27, 19, 0, tzinfo=TZ)
        self.assertEqual(
            next_reminder_occurrence(late, 1, "18:00", day_offset=-4),
            (date(2026, 11, 4), date(2026, 11, 3)),
        )
        # После воскресенья: «вс +1» — понедельник этой же недели.
        self.assertEqual(
            next_reminder_occurrence(
                datetime(2026, 11, 2, 9, 0, tzinfo=TZ), 0, "18:00",
                day_offset=1,
            ),
            (date(2026, 11, 2), date(2026, 11, 2)),
        )
        self.assertEqual(
            reminder_occurrence(monday, {
                "reminder_days": 0, "reminder_time": "18:00",
                "task": "спевка", "task_day": -4,
            }),
            (WEDNESDAY, WEDNESDAY),
        )

    def test_period_counts_week_sunday(self):
        # Период «с 01.11»: среда 28.10 — уже неделя 01.11.
        reminder = {
            "task": "спевка", "task_day": -4,
            "active_from": "2026-11-01", "active_until": "2026-11-08",
        }
        self.assertTrue(in_reminder_period(WEDNESDAY, reminder))
        self.assertTrue(in_reminder_period(date(2026, 11, 4), reminder))
        self.assertFalse(in_reminder_period(date(2026, 11, 11), reminder))
        monday = datetime(2026, 10, 26, 12, 0, tzinfo=TZ)
        self.assertEqual(
            reminder_occurrence(monday, {
                **reminder, "reminder_days": 1, "reminder_time": "18:00",
            }),
            (WEDNESDAY, date(2026, 10, 27)),
        )
        self.assertIsNone(reminder_occurrence(
            datetime(2026, 11, 5, 12, 0, tzinfo=TZ),
            {**reminder, "reminder_days": 1, "reminder_time": "18:00"},
        ))
        # Статус: среда 28.10 — уже активно, четверг 05.11 — завершено.
        self.assertEqual(reminder_period_status(reminder, WEDNESDAY), "active")
        self.assertEqual(
            reminder_period_status(reminder, date(2026, 10, 27)), "pending"
        )
        self.assertEqual(
            reminder_period_status(reminder, date(2026, 11, 5)), "ended"
        )
        # «вс +1»: понедельник после последнего воскресенья — ещё активно.
        monday_task = {**reminder, "task_day": 1}
        self.assertEqual(
            reminder_period_status(monday_task, date(2026, 11, 9)), "active"
        )


class TaskDayProcessingTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.registry = ServiceRegistry(self.tmp / "registry.sqlite3")
        self.registry.initialize(SUPER, -10001)
        self.registry.set_service_task_enabled(SUPER, "sound", "спевка", True)
        self.registry.set_service_task_day(SUPER, "sound", "спевка", -4)
        self.registry.set_group_chat(SUPER, "sound", -20002, enabled=True)

    def tearDown(self):
        self._tmp.cleanup()

    def reminder(self, task: str | None, days: int = 0) -> int:
        reminder_id = self.registry.add_group_reminder(
            SUPER, "sound", days, "10:00", f"Текст {task}",
            allow_duplicate=True,
        )
        if task:
            self.registry.set_group_reminder_task(SUPER, reminder_id, task)
        return reminder_id

    def process(self, reminder_id: int, run_date: date, assignments: dict):
        from bot import app

        bot = SimpleNamespace(send_message=AsyncMock())
        logger = MagicMock()

        async def run():
            with patch.object(
                app, "_load_service_task_assignments",
                new=AsyncMock(return_value=(assignments, True)),
            ) as load, patch.object(
                app, "_has_approved_schedule_for_event", return_value=True,
            ), patch.object(
                app, "_notify_admin", new=AsyncMock(return_value=True)
            ) as notify, patch.object(
                app, "send_admin_reports", new=AsyncMock()
            ):
                await app._process_service_reminder(
                    bot, SimpleNamespace(admin_tg_id=SUPER), self.tmp, TZ,
                    self.registry, "sound", reminder_id, logger, logger,
                    run_date=run_date,
                    state_cache=SimpleNamespace(state={}, save=MagicMock()),
                )
            return load, notify

        load, notify = asyncio.run(run())
        return bot, load, notify

    def test_registry_returns_task_day(self):
        rehearsal = self.reminder("спевка")
        sound = self.reminder("звук")
        common = self.reminder(None)
        days = {
            item["id"]: item["task_day"]
            for item in self.registry.list_group_reminders("sound")
        }
        self.assertEqual(days[rehearsal], -4)
        self.assertIsNone(days[sound])
        self.assertIsNone(days[common])
        self.assertEqual(
            self.registry.get_group_reminder(rehearsal)["task_day"], -4
        )

    def test_expiry_waits_for_task_day_of_last_week(self):
        self.registry.set_service_task_day(SUPER, "sound", "сборка", 1)
        monday = self.reminder("сборка")
        common = self.reminder(None)
        for reminder_id in (monday, common):
            self.registry.set_group_reminder_period(
                SUPER, reminder_id, date(2026, 10, 25), date(2026, 11, 1),
                today=date(2026, 10, 20),
            )
        # Понедельник 02.11 — последний день задачи «сборка» (вс +1).
        self.assertEqual(
            self.registry.disable_expired_group_reminders(date(2026, 11, 2)),
            [common],
        )
        self.assertEqual(
            self.registry.disable_expired_group_reminders(date(2026, 11, 3)),
            [monday],
        )

    def test_period_may_end_with_current_task_week(self):
        # Пятница после воскресенья (вс +5): во вторник 03.11 неделя
        # 01.11 ещё идёт — окончание «по 01.11» допустимо.
        self.registry.set_service_task_day(SUPER, "sound", "сборка", 5)
        friday = self.reminder("сборка")
        common = self.reminder(None)
        tuesday = date(2026, 11, 3)
        self.registry.set_group_reminder_period(
            SUPER, friday, None, date(2026, 11, 1), today=tuesday,
        )
        self.assertEqual(
            self.registry.get_group_reminder(friday)["active_until"],
            "2026-11-01",
        )
        with self.assertRaisesRegex(ValueError, "уже прошло"):
            self.registry.set_group_reminder_period(
                SUPER, common, None, date(2026, 11, 1), today=tuesday,
            )

    def test_common_reminder_skips_rehearsal_wednesday(self):
        reminder_id = self.reminder(None)
        bot, load, notify = self.process(
            reminder_id, WEDNESDAY, {"спевка": ["Елисей"]}
        )
        load.assert_not_awaited()
        notify.assert_not_awaited()
        bot.send_message.assert_not_awaited()
        bot, load, _ = self.process(
            reminder_id, SUNDAY, {"звук": ["Василий"]}
        )
        self.assertEqual(load.await_args.kwargs["only_date"], SUNDAY)
        bot.send_message.assert_awaited()

    def test_sunday_task_reminder_no_warning_on_wednesday(self):
        reminder_id = self.reminder("звук")
        bot, load, notify = self.process(
            reminder_id, WEDNESDAY, {"спевка": ["Елисей"]}
        )
        # В среду звука нет — ни отправки, ни ⚠️ админу.
        notify.assert_not_awaited()
        bot.send_message.assert_not_awaited()

    def test_rehearsal_reminder_counts_from_its_day(self):
        reminder_id = self.reminder("спевка", days=1)
        # Суббота перед воскресеньем — не день спевки: ни отправки, ни ⚠️.
        bot, load, notify = self.process(
            reminder_id, date(2026, 10, 31), {"звук": ["Василий"]}
        )
        notify.assert_not_awaited()
        bot.send_message.assert_not_awaited()
        # Вторник — за день до среды спевки.
        bot, load, notify = self.process(
            reminder_id, date(2026, 10, 27), {"спевка": ["Елисей"]}
        )
        self.assertEqual(load.await_args.kwargs["only_date"], WEDNESDAY)
        notify.assert_not_awaited()
        bot.send_message.assert_awaited()


    def test_task_reminder_on_one_off_day(self):
        """Ревью greptile к PR #88: спевка разово в воскресенье (день на
        одну неделю) — напоминание задачи приходит и в этот день."""
        reminder_id = self.reminder("спевка", days=1)
        bot, load, notify = self.process(
            reminder_id, date(2026, 10, 31),
            {"звук": ["Василий"], "спевка": ["Елисей"]},
        )
        self.assertEqual(load.await_args.kwargs["only_date"], SUNDAY)
        notify.assert_not_awaited()
        bot.send_message.assert_awaited()
        # Общее напоминание в среду по-прежнему не приходит.
        common = self.reminder(None)
        bot, load, _ = self.process(common, WEDNESDAY, {"спевка": ["Елисей"]})
        load.assert_not_awaited()
        bot.send_message.assert_not_awaited()

    def test_one_off_day_period_and_load_failure(self):
        """Разовый день — период по воскресенью его недели; сбой загрузки
        в чужой день не шлёт админу сообщение каждый день."""
        from bot import app

        reminder_id = self.reminder("спевка")
        self.registry.set_group_reminder_period(
            SUPER, reminder_id, SUNDAY, None, today=date(2026, 10, 20),
        )
        # Пятница 30.10 — неделя 01.11, в периоде.
        bot, load, _ = self.process(
            reminder_id, date(2026, 10, 30), {"спевка": ["Елисей"]},
        )
        bot.send_message.assert_awaited()
        # Понедельник 02.11 («вс +1») — неделя 01.11; при «по 01.11» тоже
        # в периоде.
        self.registry.set_group_reminder_period(
            SUPER, reminder_id, None, SUNDAY, today=date(2026, 10, 20),
        )
        bot, _, _ = self.process(
            reminder_id, date(2026, 11, 2), {"спевка": ["Елисей"]},
        )
        bot.send_message.assert_awaited()
        logger = MagicMock()

        async def failing():
            with patch.object(
                app, "_load_service_task_assignments",
                new=AsyncMock(side_effect=OSError("disk")),
            ), patch.object(
                app, "_notify_admin", new=AsyncMock(return_value=True)
            ) as notify:
                await app._process_service_reminder(
                    SimpleNamespace(send_message=AsyncMock()),
                    SimpleNamespace(admin_tg_id=SUPER), self.tmp, TZ,
                    self.registry, "sound", reminder_id, logger, logger,
                    run_date=date(2026, 11, 6),
                    state_cache=SimpleNamespace(state={}, save=MagicMock()),
                )
            return notify

        asyncio.run(failing()).assert_not_awaited()

if __name__ == "__main__":
    unittest.main()
