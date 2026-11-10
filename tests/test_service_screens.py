import asyncio
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

from bot.handlers import register
from bot.presentation import display_name, person_label
from bot.schedule_plan import excel_text, month_overview, plan_events, publication_text
from bot.service_registry import ServiceRegistry
from bot.ui import plain_text, render_screen_chunks, visible_units


class ServiceScreensTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.registry = ServiceRegistry(Path(self.tmp.name) / "registry.sqlite3")
        self.registry.initialize(1001, -10001)
        self.registry.set_group_chat(1001, "sound", -10055, enabled=True)
        self.registry.add_service_admin(1001, "sound", 2001)
        self.now = datetime(2026, 11, 10, 12, tzinfo=ZoneInfo("Europe/Moscow"))
        self.context = SimpleNamespace(
            interaction_logger=MagicMock(), debug_logger=MagicMock(),
            admin_id=1001, group_chat_id=-10001, timezone=self.now.tzinfo,
            service_registry=self.registry,
            bot=SimpleNamespace(send_message=AsyncMock(), delete_message=AsyncMock()),
            load_service_task_assignments=AsyncMock(return_value={}),
            load_upcoming_service_events=AsyncMock(return_value=[]),
        )
        dispatcher = SimpleNamespace(include_router=MagicMock())
        register(dispatcher, self.context)
        self.router = dispatcher.include_router.call_args_list[0].args[0]

    def message(self, actor=1001, text=""):
        return SimpleNamespace(
            from_user=SimpleNamespace(id=actor),
            chat=SimpleNamespace(type="private", id=actor), text=text,
            answer=AsyncMock(), users_shared=None, chat_shared=None,
        )

    async def press(self, data, actor=1001):
        prefix = {
            "rdy": "on_readiness", "service": "on_service_selected",
            "disable": "on_service_disable", "mbr": "on_member_card",
            "xls": "on_excel_schedule", "rpl": "on_replace_action",
        }[data.split(":")[0]]
        callback = SimpleNamespace(
            data=data, from_user=SimpleNamespace(id=actor),
            message=self.message(actor), answer=AsyncMock(),
        )
        handler = next(
            item.callback for item in self.router.callback_query.handlers
            if item.callback.__name__ == prefix
        )
        with patch("bot.handlers.datetime", wraps=datetime) as clock:
            clock.now.return_value = self.now
            await handler(callback)
        return callback

    @staticmethod
    def buttons(callback):
        markup = callback.message.answer.await_args.kwargs.get("reply_markup")
        return [
            item for row in markup.inline_keyboard for item in row
        ] if markup else []

    @staticmethod
    def text(callback):
        return "\n".join(
            item.args[0] for item in callback.message.answer.await_args_list
            if item.args
        )

    def plan(self, slots, *, approved=True, month="2026-11", tasks=None):
        self.registry.save_schedule_draft(
            "sound", month, run_id=None, dates=list(slots),
            tasks=tasks or [{"name": "Микшер", "needed": 1}],
            slots=slots, last_served={},
        )
        if approved:
            self.registry.approve_schedule_plan(
                1001, "sound", month, slots, chat_id=None, message_id=None,
            )

    def overview(self):
        return plain_text(month_overview(
            self.registry, 1001, "sound", self.now.date(),
        ))

    def test_graph_uses_approved_copy_replacements_and_no_excel(self):
        async def run():
            self.registry.set_responsible_contact(
                1001, "sound", "Микшер", "Алексей Полный", 3001,
            )
            self.registry.set_responsible_contact(
                1001, "sound", "Микшер", "Мария Полная", 3002,
            )
            self.registry.set_service_member_short_name(
                1001, "sound", "Мария Полная", "Мария",
            )
            self.plan({"2026-11-15": {"Микшер": ["Алексей Полный"]}})
            self.plan(
                {"2026-11-15": {"Микшер": ["Черновик"]}}, approved=False,
            )
            self.registry.set_replacement(
                "sound", "2026-11-15", "Микшер", "Алексей Полный",
                "Мария Полная", 3002, "", "admin", 1001,
            )
            with self.registry._connection() as connection:
                before = list(connection.iterdump())
            shown = await self.press("rdy:month:sound", 2001)
            text = self.text(shown)
            self.assertIn("ЗВУК › График", text)
            self.assertIn("15.11.2026", text)
            self.assertIn("Мария", text)
            self.assertNotIn("Мария Полная", text)
            self.assertNotIn("Алексей Полный", text)
            self.assertNotIn("Черновик", text)
            self.assertIn("неутверждённые правки", text)
            self.assertEqual(
                [item.text for item in self.buttons(shown)],
                ["🔁 Замены", "📥 Загрузить с диска", "🗳 Опрос", "⬅️ К служению"],
            )
            self.context.load_upcoming_service_events.assert_not_awaited()
            self.context.load_service_task_assignments.assert_not_awaited()
            with self.registry._connection() as connection:
                # UI may persist navigation hints, never business data.
                after = [
                    line for line in connection.iterdump()
                    if "INSERT INTO \"nav_hints\"" not in line
                ]
            self.assertEqual(
                [line for line in before if "INSERT INTO \"nav_hints\"" not in line],
                after,
            )
            poll = await self.press("rdy:open:sound", 2001)
            self.assertIn("График › Опрос", self.text(poll))
            self.assertIn("rdy:month:sound", [b.callback_data for b in self.buttons(poll)])
            back = await self.press("rdy:month:sound", 2001)
            self.assertIn("15.11.2026", self.text(back))
            denied = await self.press("rdy:month:sound", 9999)
            self.assertIn("Недостаточно прав", denied.answer.await_args.args[0])

        asyncio.run(run())

    def test_overview_missing_draft_and_empty_approved(self):
        self.assertIn("пока нет", self.overview())
        self.plan({"2026-11-15": {"Микшер": ["Черновой человек"]}}, approved=False)
        text = self.overview()
        self.assertIn("Не утверждён", text)
        self.assertIn("Напоминания по этому графику не отправляются", text)
        self.assertIn("Черновой человек", text)
        self.plan({})
        text = self.overview()
        self.assertIn("Утверждённый", text)
        self.assertIn("назначений нет", text)
        self.assertNotIn("Не утверждён", text)

    def test_actual_days_from_adjacent_month_and_year(self):
        self.now = self.now.replace(year=2027, month=1)
        self.plan(
            {"2026-12-27": {"Микшер": ["На границе"], "Пульт": []}},
            month="2026-12",
            tasks=[
                {"name": "Микшер", "needed": 1, "day": 5},
                {"name": "Пульт", "needed": 1, "day": 5},
            ],
        )
        text = self.overview()
        self.assertIn("01.01.2027", text)
        self.assertIn("На границе", text)
        self.assertIn("Пульт", text)
        self.assertIn("Ответственные не указаны", text)
        self.assertNotIn("27.12.2026", text)
        self.assertIn("соседних утверждённых", text)

    def test_empty_tasks_remain_visible_in_draft_and_approved_snapshots(self):
        tasks = [
            {"name": "Микшер", "needed": 1},
            {"name": "Пульт", "needed": 1, "day": -3},
        ]
        slots = {"2026-11-15": {"Микшер": ["Человек"], "Пульт": []}}
        self.plan(slots, approved=False, tasks=tasks)
        draft = self.overview()
        self.assertIn("Пульт", draft)
        self.assertIn("12.11.2026", draft)
        self.assertIn("Ответственные не указаны", draft)
        self.plan(slots, tasks=tasks)
        self.plan(
            {"2026-11-15": {"Черновая задача": ["Черновик"]}},
            approved=False, tasks=[{"name": "Черновая задача", "needed": 1}],
        )
        approved = self.overview()
        self.assertIn("Пульт", approved)
        self.assertIn("12.11.2026", approved)
        self.assertIn("Человек", approved)
        self.assertNotIn("Черновая задача", approved)
        self.assertNotIn("Черновик", approved)

    def test_shifted_tasks_do_not_create_empty_sunday_in_overview(self):
        tasks = [{"name": "Микшер", "needed": 1, "day": -3}]
        for approved in (False, True):
            with self.subTest(approved=approved):
                self.plan(
                    {"2026-11-15": {"Микшер": ["Человек"]}},
                    approved=approved, tasks=tasks,
                )
                text = self.overview()
                self.assertIn("12.11.2026", text)
                self.assertIn("Человек", text)
                self.assertNotIn("15.11.2026", text)
                self.assertNotIn("Назначений нет.", text)

    def test_big_graph_pages_keep_actions_and_all_people(self):
        async def run():
            names = [f"Участник {index:03d} " + "И" * 25 for index in range(120)]
            self.plan({"2026-11-15": {"Микшер": names}})
            shown = await self.press("rdy:month:sound")
            texts = []
            for _ in range(10):
                texts.append(self.text(shown))
                buttons = self.buttons(shown)
                self.assertLessEqual(len(buttons), 6)
                self.assertTrue(all(len(item.callback_data.encode()) <= 64 for item in buttons))
                self.assertIn("rdy:repl:sound", [b.callback_data for b in buttons])
                self.assertIn("rdy:open:sound", [b.callback_data for b in buttons])
                self.assertIn("service:service_excel:sound", [b.callback_data for b in buttons])
                self.assertLessEqual(visible_units(self.text(shown)), 4096)
                next_page = next((item for item in buttons if item.text == "➡️ Далее"), None)
                if next_page is None:
                    break
                shown = await self.press(next_page.callback_data)
            self.assertGreater(len(texts), 1)
            for name in names:
                self.assertIn(name, "\n".join(texts))

        asyncio.run(run())

    def test_graph_database_error_is_not_an_empty_graph(self):
        async def run():
            with patch.object(self.registry, "get_schedule_plan", side_effect=RuntimeError("db")):
                shown = await self.press("rdy:month:sound")
            self.assertIn("Не удалось прочитать", self.text(shown))
            self.assertNotIn("пока нет", self.text(shown))
            self.context.debug_logger.exception.assert_called()
        asyncio.run(run())

    def test_disable_confirmation_rejects_foreign_stale_and_revoked(self):
        async def run():
            denied = await self.press("service:service_chat_disable:sound", 2001)
            self.assertIn("суперадминистратор", self.text(denied))
            shown = await self.press("service:service_chat_disable:sound")
            confirm = self.buttons(shown)[0].callback_data
            self.assertTrue(self.registry.get_service("sound")["enabled"])
            foreign = await self.press(confirm, 2001)
            self.assertIn("Недостаточно прав", foreign.answer.await_args.args[0])
            await self.press("service:service_setup:sound")
            stale = await self.press(confirm)
            self.assertIn("устарело", stale.answer.await_args.args[0])
            self.assertTrue(self.registry.get_service("sound")["enabled"])
            shown = await self.press("service:service_chat_disable:sound")
            confirm = self.buttons(shown)[0].callback_data
            await self.press(confirm)
            service = self.registry.get_service("sound")
            self.assertFalse(service["enabled"])
            self.assertEqual(service["group_chat_id"], -10055)
            repeat = await self.press(confirm)
            self.assertIn("устарело", repeat.answer.await_args.args[0])
            with self.assertRaises(PermissionError):
                self.registry.set_service_enabled(2001, "sound", False)
            with self.assertRaises(PermissionError):
                self.registry.set_group_chat(2001, "sound", -10055, enabled=False)
        asyncio.run(run())

    def test_settings_links_and_no_fake_chat_link(self):
        async def run():
            self.context.bot.get_chat = AsyncMock(return_value=SimpleNamespace(
                title="Чат <служения>", username="sound_chat", invite_link=None,
            ))
            shown = await self.press("service:service_setup:sound", 2001)
            self.assertIn('href="https://t.me/sound_chat"', self.text(shown))
            self.assertIn("Чат &lt;служения&gt;", self.text(shown))
            self.assertIn('href="tg://user?id=2001"', self.text(shown))
            self.assertNotIn("service:service_chat_disable:sound", [
                item.callback_data for item in self.buttons(shown)
            ])
            self.context.bot.get_chat.return_value.username = None
            shown = await self.press("service:service_setup:sound")
            self.assertNotIn("https://t.me/", self.text(shown))
            self.assertIn("Чат &lt;служения&gt;", self.text(shown))
        asyncio.run(run())

    def test_short_name_storage_limits_identity_and_restart(self):
        self.registry.set_responsible_contact(1001, "sound", "Микшер", "Полное А", 3001)
        self.registry.set_responsible_contact(1001, "sound", "Микшер", "Полное Б", 3002)
        self.plan({"2026-11-15": {"Микшер": ["Полное А", "Полное Б"]}})
        before = self.registry.get_schedule_plan("sound", "2026-11")
        for name in ("Полное А", "Полное Б"):
            self.registry.set_service_member_short_name(2001, "sound", name, "Имя")
        contacts = self.registry.list_responsible_contacts(1001, "sound")
        by_name = {(row["task"], row["name"]): row for row in contacts}
        self.assertIn("LINK:3001:", person_label("Микшер", "Полное А", by_name))
        self.assertIn("LINK:3002:", person_label("Микшер", "Полное Б", by_name))
        self.assertEqual(display_name(None, "Полное"), "Полное")
        self.assertEqual(
            self.registry.get_schedule_plan("sound", "2026-11"), before,
        )
        events = plan_events(before, self.registry.readiness_people("sound"))
        self.assertEqual(
            {item.name for event in events for item in event.assignments},
            {"Полное А", "Полное Б"},
        )
        people = self.registry.readiness_people("sound")
        published = plain_text(publication_text(
            self.registry.get_service("sound"), before, people,
        ))
        self.assertIn("Имя", published)
        self.assertNotIn("Полное А", published)
        exported = excel_text(self.registry, self.registry.get_service("sound"), before, people)
        self.assertIn("Полное А", exported)
        self.assertIn("Полное Б", exported)
        with self.assertRaises(PermissionError):
            self.registry.set_service_member_short_name(9999, "sound", "Полное А", "Имя")
        for invalid in ("И" * 33, "Строка\nдва", "\x00LINK"):
            with self.assertRaises(ValueError):
                self.registry.set_service_member_short_name(2001, "sound", "Полное А", invalid)
        self.registry.set_service_member_short_name(2001, "sound", "Полное А", "И" * 32)
        self.registry.initialize(1001, -10001)
        self.assertEqual(self.registry.service_display_names("sound")["Полное А"], "И" * 32)
        self.registry.set_service_member_short_name(2001, "sound", "Полное А", "  ")
        self.assertNotIn("Полное А", self.registry.service_display_names("sound"))
        self.registry.set_service_member_short_name(2001, "sound", "Полное А", "Имя")
        self.registry.set_service_member_short_name(2001, "sound", "Полное А", "")
        self.assertNotIn("Полное А", self.registry.service_display_names("sound"))

    def test_slow_chat_lookup_does_not_replace_a_newer_screen(self):
        async def run():
            started, release = asyncio.Event(), asyncio.Event()

            async def get_chat(chat_id):
                started.set()
                await release.wait()
                return SimpleNamespace(title="Чат", username=None, invite_link=None)

            self.context.bot.get_chat = get_chat
            old_request = asyncio.create_task(self.press("service:service_setup:sound"))
            await started.wait()
            current = await self.press("rdy:month:sound")
            release.set()
            old = await old_request
            old.message.answer.assert_not_awaited()
            self.assertIn("ЗВУК › График", self.text(current))
        asyncio.run(run())

    def test_short_name_for_unbound_member_does_not_create_a_contact(self):
        from bot.excel_parser import RosterMember
        self.registry.sync_service_rosters({
            "sound": [RosterMember("Полное имя", ("Микшер",))],
        })
        self.registry.set_service_member_short_name(2001, "sound", "Полное имя", "Подпись")
        self.plan({"2026-11-15": {"Микшер": ["Полное имя"]}})
        self.assertIn("Подпись", self.overview())
        self.assertNotIn("Полное имя", self.overview())
        self.assertEqual(self.registry.list_responsible_contacts(1001, "sound"), [])


if __name__ == "__main__":
    unittest.main()
