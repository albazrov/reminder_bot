import unittest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from bot.scheduler import (
    add_service_reminder_jobs,
    group_reminder_period_offsets,
    refresh_user_reminder_jobs,
    scheduled_reminder_date,
)


class ServiceReminderSchedulerTest(unittest.IsolatedAsyncioTestCase):
    async def test_range_period_offsets_use_both_window_boundaries(self):
        registry = MagicMock()
        reminder = {
            "service_id": "sound",
            "task": "звук",
            "task_day": 0,
            "active_from": None,
            "active_until": "2026-11-29",
        }
        with patch(
            "bot.schedule_plan.approved_range_week",
            return_value=((-3, -1), "звук", {}),
        ):
            self.assertEqual(
                group_reminder_period_offsets(registry, reminder),
                (-3, -1),
            )

    async def test_registers_each_reminder_and_preserves_unchanged_jobs(self):
        scheduler = AsyncIOScheduler(timezone=ZoneInfo("UTC"))
        callback = AsyncMock()
        services = [
            {
                "id": "assembly",
                "group_reminders": [
                    {"id": 11, "reminder_days": 3, "reminder_time": "18:00",
                     "enabled": True},
                    {"id": 12, "reminder_days": 1, "reminder_time": "09:15",
                     "enabled": True},
                    {"id": 13, "reminder_days": 3, "reminder_time": "18:00",
                     "enabled": True},
                ],
            },
            {
                "id": "broadcast",
                "group_reminders": [
                    {"id": 19, "reminder_days": 2, "reminder_time": "12:30",
                     "enabled": True}
                ],
            },
        ]
        logger = MagicMock()

        await add_service_reminder_jobs(
            scheduler, services, callback, ZoneInfo("UTC"), logger
        )
        original = scheduler.get_job("group-reminder-broadcast-19")
        await add_service_reminder_jobs(
            scheduler, services, callback, ZoneInfo("UTC"), logger
        )

        self.assertEqual(
            {job.id for job in scheduler.get_jobs()},
            {
                "group-reminder-assembly-11",
                "group-reminder-assembly-12",
                "group-reminder-assembly-13",
                "group-reminder-broadcast-19",
            },
        )
        self.assertIs(
            scheduler.get_job("group-reminder-broadcast-19"), original
        )
        self.assertEqual(callback.await_count, 0)

    async def test_changed_due_job_runs_before_it_is_replaced(self):
        tz = ZoneInfo("UTC")
        scheduler = AsyncIOScheduler(timezone=tz)
        callback = AsyncMock()
        logger = MagicMock()
        old_rule = {
            "id": 11, "reminder_days": 3, "reminder_time": "18:00",
            "enabled": True,
        }
        await add_service_reminder_jobs(
            scheduler,
            [{"id": "assembly", "group_reminders": [old_rule]}],
            callback,
            tz,
            logger,
        )
        scheduler.start(paused=True)
        old_job = scheduler.get_job("group-reminder-assembly-11")
        scheduled_time = datetime.now(tz) - timedelta(minutes=1)
        old_job.modify(next_run_time=scheduled_time)

        new_rule = {
            "id": 11, "reminder_days": 2, "reminder_time": "19:00",
            "enabled": True,
        }
        await add_service_reminder_jobs(
            scheduler,
            [{"id": "assembly", "group_reminders": [new_rule]}],
            callback,
            tz,
            logger,
        )

        callback.assert_awaited_once_with(
            "assembly", 11, scheduled_time.date()
        )
        self.assertEqual(
            scheduler.get_job("group-reminder-assembly-11").args,
            ("assembly", 11),
        )

    async def test_removes_legacy_jobs_and_does_not_fallback_to_legacy_rows(self):
        tz = ZoneInfo("UTC")
        scheduler = AsyncIOScheduler(timezone=tz)
        callback = AsyncMock()
        scheduler.add_job(
            callback,
            CronTrigger(hour=10, minute=0, timezone=tz),
            args=("assembly", 11),
            id="service-reminder-assembly-11",
        )
        registry = MagicMock()
        registry.list_group_reminders.return_value = []
        await add_service_reminder_jobs(
            scheduler,
            [{"id": "assembly"}],
            callback,
            tz,
            MagicMock(),
            registry=registry,
        )

        self.assertEqual(scheduler.get_jobs(), [])
        registry.list_legacy_group_reminders.assert_not_called()
        callback.assert_not_awaited()

    async def test_confirmed_duplicate_schedules_get_independent_jobs(self):
        scheduler = AsyncIOScheduler(timezone=ZoneInfo("UTC"))
        callback = AsyncMock()
        reminders = [
            {
                "id": reminder_id,
                "reminder_days": 1,
                "reminder_time": "18:00",
                "enabled": True,
            }
            for reminder_id in (11, 12)
        ]

        await add_service_reminder_jobs(
            scheduler,
            [{"id": "assembly", "group_reminders": reminders}],
            callback,
            ZoneInfo("UTC"),
            MagicMock(),
        )

        self.assertEqual(
            {job.id for job in scheduler.get_jobs()},
            {
                "group-reminder-assembly-11",
                "group-reminder-assembly-12",
            },
        )

    async def test_user_reminder_jobs_are_independent_and_refreshable(self):
        tz = ZoneInfo("UTC")
        scheduler = AsyncIOScheduler(timezone=tz)
        group_callback = AsyncMock()
        user_callback = AsyncMock()
        scheduler.add_job(
            group_callback,
            CronTrigger(hour=10, minute=0, timezone=tz),
            id="service-reminder-assembly-11",
        )
        records = [
            {
                "schedule_id": 101,
                "actor_id": 2001,
                "reminder_time": "09:00",
            },
            {
                "schedule_id": 102,
                "actor_id": 2001,
                "reminder_time": "18:00",
            },
        ]
        registry = MagicMock()
        registry.list_enabled_user_reminder_schedules.return_value = records

        await refresh_user_reminder_jobs(
            scheduler, registry, user_callback, tz, MagicMock()
        )
        await refresh_user_reminder_jobs(
            scheduler, registry, user_callback, tz, MagicMock()
        )

        self.assertEqual(
            {
                job.id
                for job in scheduler.get_jobs()
                if job.id.startswith("user-reminder-")
            },
            {
                "user-reminder-2001-schedule-101",
                "user-reminder-2001-schedule-102",
            },
        )
        registry.list_enabled_user_reminder_schedules.return_value = []
        await refresh_user_reminder_jobs(
            scheduler, registry, user_callback, tz, MagicMock()
        )
        self.assertEqual(
            [job.id for job in scheduler.get_jobs()],
            ["service-reminder-assembly-11"],
        )

    def test_delayed_cron_execution_recovers_previous_local_date(self):
        tz = ZoneInfo("Europe/Moscow")
        now = datetime(2026, 10, 15, 0, 20, tzinfo=tz)

        self.assertEqual(
            scheduled_reminder_date(now, "23:55").isoformat(),
            "2026-10-14",
        )
        self.assertEqual(
            scheduled_reminder_date(now, "00:15").isoformat(),
            "2026-10-15",
        )


if __name__ == "__main__":
    unittest.main()
