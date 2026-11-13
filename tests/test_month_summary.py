"""Этап 4.1: личная сводка на месяц перед первым воскресеньем."""
import asyncio
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

from bot.excel_parser import ServiceAssignment, ServiceEvent
from bot.handlers import register
from bot.month_summary import (
    due_month,
    first_sunday,
    month_summary_text,
    send_due_month_summaries,
)
from bot.service_registry import ReplacementResolutionError, ServiceRegistry

TZ = ZoneInfo("UTC")
SUPER = 67890
USER = 4004


def event(day, task, name, service_id="sound"):
    return ServiceEvent(service_id, day, (ServiceAssignment(task, name),))


class RulesTest(unittest.TestCase):
    def test_first_sunday_and_due_window(self):
        self.assertEqual(first_sunday(2026, 11), date(2026, 11, 1))
        self.assertEqual(first_sunday(2026, 12), date(2026, 12, 6))
        # Сводка на ноябрь: за 2 дня до 01.11 в 18:00 — с 30.10 18:00 по 01.11.
        self.assertIsNone(due_month(datetime(2026, 10, 30, 17, 59, tzinfo=TZ), 2, "18:00"))
        self.assertEqual(
            due_month(datetime(2026, 10, 30, 18, 0, tzinfo=TZ), 2, "18:00"), (2026, 11)
        )
        self.assertEqual(
            due_month(datetime(2026, 11, 1, 23, 0, tzinfo=TZ), 2, "18:00"), (2026, 11)
        )
        self.assertIsNone(due_month(datetime(2026, 11, 2, 9, 0, tzinfo=TZ), 2, "18:00"))
        # Декабрь → январь следующего года.
        self.assertEqual(
            due_month(datetime(2026, 12, 31, 12, 0, tzinfo=TZ), 3, "08:00"), (2027, 1)
        )

    def test_text_lists_only_linked_tasks_of_month(self):
        events = [
            event(date(2026, 11, 1), "звук", "Алексей"),
            event(date(2026, 11, 8), "звук", "Борис"),
            event(date(2026, 12, 6), "звук", "Алексей"),
        ]
        text = month_summary_text(
            events, {("sound", "звук", "Алексей")}, {"sound": "ЗВУК"}, 2026, 11
        )
        self.assertIn("ноябрь 2026", text)
        self.assertIn("01.11.2026", text)
        self.assertNotIn("08.11.2026", text)
        self.assertNotIn("06.12.2026", text)


class SendTest(unittest.TestCase):
    def setUp(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.registry = ServiceRegistry(Path(temp_dir.name) / "r.sqlite3")
        self.registry.initialize(SUPER, -100)
        self.registry.set_responsible_contact(
            SUPER, "sound", "звук", "Алексей", USER, "alex_one"
        )
        self.bot = SimpleNamespace(send_message=AsyncMock())
        self.events = [event(date(2026, 11, 1), "звук", "Алексей")]
        self.loader = AsyncMock(side_effect=lambda **_: self.events)
        self.now = datetime(2026, 10, 31, 18, 5, tzinfo=TZ)

    def run_summaries(self, now=None):
        return asyncio.run(send_due_month_summaries(
            self.bot, self.registry, self.loader, now or self.now, MagicMock()
        ))

    def send(self, now=None):
        return self.run_summaries(now).sent

    def test_defaults_off_and_sent_once_per_month(self):
        self.assertFalse(self.registry.get_month_summary(USER)["enabled"])
        self.assertEqual(self.send(), 0)
        self.loader.assert_not_awaited()
        self.registry.set_month_summary(USER, enabled=True)
        self.assertEqual(self.send(), 1)
        self.assertIn("ноябрь 2026", self.bot.send_message.await_args.args[1])
        self.assertEqual(self.send(), 0)
        self.assertTrue(self.registry.is_month_summary_delivered(USER, "2026-11"))

    def test_failed_send_is_retried_and_missing_month_waits(self):
        self.registry.set_month_summary(USER, enabled=True)
        self.events = []
        self.assertEqual(self.send(), 0)
        self.assertFalse(self.registry.is_month_summary_delivered(USER, "2026-11"))
        self.events = [event(date(2026, 11, 1), "звук", "Алексей")]
        self.bot.send_message.side_effect = RuntimeError("blocked")
        self.assertEqual(self.send(), 0)
        self.assertFalse(self.registry.is_month_summary_delivered(USER, "2026-11"))
        self.bot.send_message.side_effect = None
        self.assertEqual(self.send(), 1)

    def test_blocked_user_keeps_mark(self):
        from aiogram.exceptions import TelegramForbiddenError

        self.registry.set_month_summary(USER, enabled=True)
        self.bot.send_message.side_effect = TelegramForbiddenError(
            method=MagicMock(), message="Forbidden: bot was blocked by the user"
        )
        self.assertEqual(self.send(), 0)
        self.assertTrue(self.registry.is_month_summary_delivered(USER, "2026-11"))

    def test_waits_for_every_linked_service(self):
        """Звук опубликован, кухня — ещё нет: ждём до первого воскресенья."""
        self.registry.set_responsible_contact(
            SUPER, "kitchen", "старший", "Алексей", USER, "alex_one"
        )
        self.registry.set_month_summary(USER, enabled=True)
        run = self.run_summaries()
        self.assertEqual((run.sent, run.waiting), (0, True))
        self.assertFalse(self.registry.is_month_summary_delivered(USER, "2026-11"))
        self.assertFalse(self.loader.await_args.kwargs["fresh"])
        # В первое воскресенье в его время уходит то, что есть, по свежему
        # графику.
        run = self.run_summaries(datetime(2026, 11, 1, 18, 0, tzinfo=TZ))
        self.assertEqual((run.sent, run.waiting), (1, False))
        self.assertTrue(self.loader.await_args.kwargs["fresh"])
        self.assertIn("ЗВУК", self.bot.send_message.await_args.args[1])

    def test_no_own_tasks_marks_without_message(self):
        self.registry.set_month_summary(USER, enabled=True)
        self.events = [event(date(2026, 11, 1), "звук", "Борис")]
        self.assertEqual(self.send(), 0)
        self.bot.send_message.assert_not_awaited()
        self.assertTrue(self.registry.is_month_summary_delivered(USER, "2026-11"))

    def test_ambiguous_replacement_isolated_to_linked_recipients(self):
        from bot import app

        other = USER + 1
        self.registry.set_responsible_contact(
            SUPER, "kitchen", "старший", "Другой", other,
        )
        for user in (other, USER):
            self.registry.set_month_summary(user, enabled=True)
        day = "2026-11-01"
        for service, task, name in (
            ("sound", "звук", "Алексей"), ("kitchen", "старший", "Другой"),
        ):
            slots = {day: {task: [name]}}
            self.registry.save_schedule_draft(
                service, "2026-11", run_id=None, dates=[day],
                tasks=[{"name": task, "needed": 1}], slots=slots, last_served={},
            )
            self.registry.approve_schedule_plan(
                SUPER, service, "2026-11", slots, chat_id=None, message_id=None,
            )
        original = self.registry.list_replacements

        def replacements(service, day, **kwargs):
            if service == "kitchen":
                raise ReplacementResolutionError("Неоднозначная замена")
            return original(service, day, **kwargs)

        async def load(**kwargs):
            return await app._load_month_summary_events(
                AsyncMock(), self.registry, {}, self.now,
                public_url="unused", **kwargs,
            )

        with patch.object(self.registry, "list_replacements", side_effect=replacements):
            run = asyncio.run(send_due_month_summaries(
                self.bot, self.registry, load, self.now, MagicMock(),
            ))
            async def check_cached_loader():
                cache = {}
                source = AsyncMock(return_value=[])
                kwargs = {"since": date(2026, 11, 1), "until": date(2026, 11, 30)}
                events = await app._load_month_summary_events(
                    source, self.registry, cache, self.now,
                    service_ids={"sound"}, **kwargs,
                )
                self.assertEqual({event.service_id for event in events}, {"sound"})
                with self.assertRaises(ReplacementResolutionError):
                    await app._load_month_summary_events(
                        source, self.registry, cache, self.now,
                        service_ids={"kitchen"}, **kwargs,
                    )
                self.assertEqual(source.await_count, 1)
            asyncio.run(check_cached_loader())
        self.assertEqual((run.sent, run.waiting), (1, True))
        self.assertTrue(self.registry.is_month_summary_delivered(USER, "2026-11"))
        self.assertFalse(self.registry.is_month_summary_delivered(other, "2026-11"))
        self.assertEqual(self.bot.send_message.await_args.args[0], USER)

    def test_summary_refreshes_new_task_contact_before_sending(self):
        from bot import app

        self.registry.bind_service_member(SUPER, "sound", "Алексей", USER)
        self.registry.set_month_summary(USER, enabled=True)
        day = "2026-11-01"
        slots = {day: {"сборка": ["Алексей"]}}
        self.registry.save_schedule_draft(
            "sound", "2026-11", run_id=None, dates=[day],
            tasks=[{"name": "сборка", "needed": 1}], slots=slots, last_served={},
        )
        self.registry.approve_schedule_plan(
            SUPER, "sound", "2026-11", slots, chat_id=None, message_id=None,
        )
        self.assertNotIn(
            ("sound", "сборка", "Алексей"),
            {(item["service_id"], item["task"], item["name"])
             for item in self.registry.list_user_assignments(USER)},
        )

        async def load(**kwargs):
            return await app._load_month_summary_events(
                AsyncMock(), self.registry, {}, self.now,
                public_url="unused", **kwargs,
            )

        run = asyncio.run(send_due_month_summaries(
            self.bot, self.registry, load, self.now, MagicMock(),
        ))
        self.assertEqual(run.sent, 1)
        self.assertIn("сборка", self.bot.send_message.await_args.args[1])

    def test_summary_includes_member_service_without_existing_task_contact(self):
        from bot import app

        self.registry.bind_service_member(SUPER, "sound", "Алексей", USER)
        with self.registry._connection() as connection:
            connection.execute(
                "DELETE FROM responsible_contacts WHERE service_id=? AND telegram_id=?",
                ("sound", USER),
            )
        self.registry.set_month_summary(USER, enabled=True)
        day = "2026-11-01"
        slots = {day: {"сборка": ["Алексей"]}}
        self.registry.save_schedule_draft(
            "sound", "2026-11", run_id=None, dates=[day],
            tasks=[{"name": "сборка", "needed": 1}], slots=slots, last_served={},
        )
        self.registry.approve_schedule_plan(
            SUPER, "sound", "2026-11", slots, chat_id=None, message_id=None,
        )
        self.assertEqual(self.registry.list_user_assignments(USER), [])

        async def load(**kwargs):
            return await app._load_month_summary_events(
                AsyncMock(), self.registry, {}, self.now,
                public_url="unused", **kwargs,
            )

        run = asyncio.run(send_due_month_summaries(
            self.bot, self.registry, load, self.now, MagicMock(),
        ))
        self.assertEqual(run.sent, 1)
        self.assertIn("сборка", self.bot.send_message.await_args.args[1])

    def test_batch_rechecks_approved_plan_between_recipients_without_download(self):
        from bot import app

        for withdraw in (False, True):
            with self.subTest(withdraw=withdraw):
                self.setUp()
                second_user = USER + 1
                self.registry.set_responsible_contact(
                    SUPER, "sound", "звук", "Мария", second_user
                )
                for user in (USER, second_user):
                    self.registry.set_month_summary(user, enabled=True)
                slots = {
                    "2026-11-01": {"звук": ["Алексей"]},
                    "2026-11-08": {"звук": ["Мария"]},
                }

                def approve():
                    self.registry.save_schedule_draft(
                        "sound", "2026-11", run_id=None, dates=list(slots),
                        tasks=[{"name": "звук", "needed": 1}],
                        slots=slots, last_served={},
                    )
                    self.registry.approve_schedule_plan(
                        SUPER, "sound", "2026-11", slots,
                        chat_id=None, message_id=None,
                    )

                approve()
                events, _ = app._bot_plan_events(
                    self.registry, date(2026, 11, 1), date(2026, 11, 30)
                )
                source = AsyncMock(return_value=events)
                cache = {}

                async def load(**kwargs):
                    return await app._load_month_summary_events(
                        source, self.registry, cache, self.now, **kwargs
                    )

                async def send(user, text, **kwargs):
                    if user == USER:
                        if withdraw:
                            self.registry.cancel_schedule_approval("sound", "2026-11")
                        else:
                            slots["2026-11-15"] = slots.pop("2026-11-08")
                            approve()

                self.loader = load
                self.bot.send_message.side_effect = send
                result = self.run_summaries()
                source.assert_awaited_once()
                if withdraw:
                    self.assertEqual((result.sent, result.waiting), (1, True))
                    self.assertFalse(self.registry.is_month_summary_delivered(
                        second_user, "2026-11"
                    ))
                else:
                    self.assertEqual(result.sent, 2)
                    text = self.bot.send_message.await_args.args[1]
                    self.assertIn("15.11.2026", text)
                    self.assertNotIn("08.11.2026", text)

    def test_validation(self):
        with self.assertRaises(ValueError):
            self.registry.set_month_summary(USER, days=7)
        with self.assertRaises(ValueError):
            self.registry.set_month_summary(USER, time="25:00")


class ScreensTest(SendTest):
    def setUp(self):
        super().setUp()
        self.context = SimpleNamespace(
            interaction_logger=MagicMock(), debug_logger=MagicMock(),
            admin_id=SUPER, group_chat_id=-100, timezone=TZ,
            service_registry=self.registry,
            bot=SimpleNamespace(send_message=AsyncMock(), delete_message=AsyncMock()),
            load_upcoming_service_events=self.loader,
        )
        dispatcher = SimpleNamespace(include_router=MagicMock())
        register(dispatcher, self.context)
        self.admin_router = dispatcher.include_router.call_args_list[0].args[0]

    def handler(self, name, kind="callback_query"):
        return [
            item.callback for item in getattr(self.admin_router, kind).handlers
            if item.callback.__name__ == name
        ][-1]

    def callback(self, data):
        return SimpleNamespace(
            data=data, from_user=SimpleNamespace(id=USER),
            message=SimpleNamespace(
                chat=SimpleNamespace(type="private", id=USER), answer=AsyncMock(),
            ),
            answer=AsyncMock(),
        )

    @staticmethod
    def buttons(source):
        markup = source.answer.await_args.kwargs["reply_markup"]
        return [item for row in markup.inline_keyboard for item in row]

    def test_month_view_and_navigation(self):
        menu = self.callback("menu:my_month")
        asyncio.run(self.handler("on_start_menu")(menu))
        text = menu.message.answer.await_args.args[0]
        self.assertIn("Ваши служения", text)
        # Месяц целиком, включая уже прошедшие воскресенья.
        self.assertEqual(self.loader.await_args.kwargs["since"].day, 1)
        self.assertTrue(self.loader.await_args.kwargs["approved_only"])
        forward = next(
            item for item in self.buttons(menu.message)
            if item.callback_data.startswith("mymonth:")
        )
        nav = self.callback(forward.callback_data)
        asyncio.run(self.handler("on_my_month")(nav))
        self.assertIn("Ваши служения", nav.message.answer.await_args.args[0])
        stale = self.callback("mymonth:203001")
        asyncio.run(self.handler("on_my_month")(stale))
        self.assertIn("устарел", stale.answer.await_args.args[0])

    def test_settings_in_my_notifications(self):
        command = SimpleNamespace(
            from_user=SimpleNamespace(id=USER),
            chat=SimpleNamespace(type="private", id=USER),
            text="/my_notifications", users_shared=None, chat_shared=None,
            answer=AsyncMock(),
        )
        asyncio.run(self.handler("on_my_notifications", "message")(command))
        item = next(
            button for button in self.buttons(command)
            if button.text.startswith("📆 Сводка на месяц")
        )
        self.assertIn("⚪ выключена", item.text)
        screen = self.callback(item.callback_data)
        asyncio.run(self.handler("on_personal_notifications_callback")(screen))
        pick = next(
            button for button in self.buttons(screen.message)
            if "за 2 дн." in button.text
        )
        picked = self.callback(pick.callback_data)
        asyncio.run(self.handler("on_personal_notifications_callback")(picked))
        self.assertEqual(
            self.registry.get_month_summary(USER),
            {"enabled": True, "reminder_days": 2, "reminder_time": "18:00"},
        )
        toggle = next(
            button for button in self.buttons(picked.message)
            if button.text == "🔕 Выключить"
        )
        asyncio.run(self.handler("on_personal_notifications_callback")(
            self.callback(toggle.callback_data)
        ))
        self.assertFalse(self.registry.get_month_summary(USER)["enabled"])


if __name__ == "__main__":
    unittest.main()
