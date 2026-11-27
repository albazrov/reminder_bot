import asyncio
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

from bot import schedule_plan
from bot.handlers import register
from bot.presentation import display_name, person_label
from bot.schedule_plan import (
    excel_text, month_overview, plan_events, publication_text, workload_lines,
)
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
                ["✏️ Изменить", "🔁 Замены", "📥 Загрузить с диска",
                 "🗳 Опрос", "🧮 Авто", "📊 Сводка",
                 "⬅️ К служению", "❓ Справка"],
            )
            rows = shown.message.answer.await_args.kwargs["reply_markup"].inline_keyboard
            self.assertEqual(
                [[button.text for button in row] for row in rows],
                [
                    ["✏️ Изменить", "🔁 Замены", "📥 Загрузить с диска"],
                    ["🗳 Опрос", "🧮 Авто", "📊 Сводка"],
                    ["⬅️ К служению", "❓ Справка"],
                ],
            )
            self.assertIn(
                "rdy:pedit:sound:202611",
                [item.callback_data for item in self.buttons(shown)],
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

    def test_graph_help_works_without_plan_and_preserves_page(self):
        async def run():
            main = await self.press("rdy:month:sound", 2001)
            help_button = next(b for b in self.buttons(main) if b.text == "❓ Справка")
            help_screen = await self.press(help_button.callback_data, 2001)
            for name in ("Изменить", "Замены", "Загрузить с диска", "Опрос", "Авто", "Сводка"):
                self.assertIn(name, self.text(help_screen))
            self.assertEqual(self.buttons(help_screen)[0].callback_data, "rdy:month:sound:0")
            back = await self.press(self.buttons(help_screen)[0].callback_data, 2001)
            self.assertIn("График", self.text(back))
            denied = await self.press(help_button.callback_data, 9999)
            self.assertIn("Недостаточно прав", denied.answer.await_args.args[0])

            names = [f"Участник {index:03d} " + "И" * 25 for index in range(120)]
            self.plan({"2026-11-15": {"Микшер": names}})
            page = await self.press("rdy:month:sound:1", 2001)
            self.assertIn("Страница 2/", self.text(page))
            help_button = next(b for b in self.buttons(page) if b.text == "❓ Справка")
            help_screen = await self.press(help_button.callback_data, 2001)
            back = await self.press(self.buttons(help_screen)[0].callback_data, 2001)
            self.assertIn("Страница 2/", self.text(back))

        asyncio.run(run())

    def test_day_help_explains_actions_and_returns_to_selected_date(self):
        slots = {
            "2026-11-15": {"Микшер": ["Анна"]},
            "2026-11-22": {"Микшер": ["Борис"]},
        }
        self.plan(slots)

        async def run():
            dates = await self.press("rdy:pedit:sound:202611", 2001)
            day_button = [
                b for b in self.buttons(dates)
                if b.callback_data.startswith("rdy:pdate:")
            ][1]
            day = await self.press(day_button.callback_data, 2001)
            self.assertIn("«Удалить» либо «Добавить»", self.text(day))
            rows = day.message.answer.await_args.kwargs["reply_markup"].inline_keyboard
            self.assertEqual([b.text for b in rows[-1]], ["⬅️ К датам", "❓ Справка"])
            help_button = rows[-1][1]
            help_screen = await self.press(help_button.callback_data, 2001)
            for text in ("22.11", "название задачи", "со всех задач", "Добавить", "утверждения"):
                self.assertIn(text, self.text(help_screen))
            back_button = self.buttons(help_screen)[0]
            self.assertEqual(back_button.callback_data, day_button.callback_data)
            back = await self.press(back_button.callback_data, 2001)
            self.assertIn("Борис", self.text(back))
            denied = await self.press(help_button.callback_data, 9999)
            self.assertIn("Недостаточно прав", denied.answer.await_args.args[0])

            changed = {"2026-11-15": {"Микшер": []}, "2026-11-22": {"Микшер": []}}
            self.registry.set_schedule_slots(1001, "sound", "2026-11", changed)
            stale = await self.press(help_button.callback_data, 2001)
            self.assertIn("График изменился", self.text(stale))

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

    def test_member_month_assignments_use_approved_replacements_and_history(self):
        self.registry.add_service_task(1001, "sound", "Микшер")
        for name, user_id in (("Алексей", 3001), ("Мария", 3002)):
            self.registry.set_responsible_contact(1001, "sound", "Микшер", name, user_id)
        tasks = [{"name": "Микшер", "needed": 1}]
        self.plan({"2026-10-04": {"Микшер": ["Мария"]}}, month="2026-10", tasks=tasks)
        self.plan({
            "2026-11-15": {"Микшер": ["Алексей"]},
            "2026-11-22": {"Микшер": []},
        }, tasks=tasks)
        self.registry.set_replacement(
            "sound", "2026-11-15", "Микшер", "Алексей", "Мария", 3002, "", "admin", 1001,
        )
        self.registry.set_schedule_slots(
            1001, "sound", "2026-11", {
                "2026-11-15": {"Микшер": []},
                "2026-11-22": {"Микшер": ["Алексей"]},
            },
        )
        people = self.registry.readiness_people("sound")
        selected, dates, previous = schedule_plan.member_month_assignments(
            self.registry, "sound", date(2026, 11, 1), people,
        )
        self.assertIsNotNone(selected["approved"])
        self.assertEqual(dates.get("Мария"), {date(2026, 11, 15)})
        self.assertNotIn("Алексей", dates)
        self.assertEqual(previous.get("Мария"), date(2026, 10, 4))

    def test_member_history_does_not_restore_replaced_person_from_snapshot(self):
        self.registry.add_service_task(1001, "sound", "Микшер")
        for name, user_id in (("Алексей", 3001), ("Мария", 3002)):
            self.registry.set_responsible_contact(1001, "sound", "Микшер", name, user_id)
        tasks = [{"name": "Микшер", "needed": 1}]
        self.plan({"2026-10-04": {"Микшер": ["Алексей"]}}, month="2026-10", tasks=tasks)
        self.registry.set_replacement(
            "sound", "2026-10-04", "Микшер", "Алексей", "Мария", 3002, "", "admin", 1001,
        )
        self.registry.save_schedule_draft(
            "sound", "2026-11", run_id=None, dates=["2026-11-15"],
            tasks=tasks, slots={"2026-11-15": {"Микшер": []}},
            last_served={"Алексей": "2026-10-04"},
        )
        _, _, previous = schedule_plan.member_month_assignments(
            self.registry, "sound", date(2026, 11, 1),
            self.registry.readiness_people("sound"),
        )
        self.assertNotIn("Алексей", previous)
        self.assertEqual(previous["Мария"], date(2026, 10, 4))

    def test_editing_approved_plan_saves_working_copy_immediately(self):
        from bot.excel_parser import RosterMember

        self.registry.sync_service_rosters({"sound": [
            RosterMember("Алексей", ("Микшер",)),
            RosterMember("Борис", ("Микшер",)),
        ]})
        self.registry.add_service_task(1001, "sound", "Микшер")
        slots = {"2026-11-15": {"Микшер": ["Алексей"]}}
        self.plan(slots, approved=True, tasks=[{"name": "Микшер", "needed": 1}])
        approved = self.registry.get_schedule_plan("sound", "2026-11")["approved"]

        async def run():
            dates = await self.press("rdy:pedit:sound:202611")
            day = await self.press(next(
                button.callback_data for button in self.buttons(dates)
                if button.callback_data.startswith("rdy:pdate:")
            ))
            task_list = await self.press(next(
                button.callback_data for button in self.buttons(day)
                if button.callback_data.startswith("rdy:pquick:")
                and button.callback_data.endswith(".add")
            ))
            add_stage = await self.press(next(
                button.callback_data for button in self.buttons(task_list)
                if button.callback_data.startswith("rdy:pbulktask:")
            ))
            self.assertIn("1. Борис", self.text(add_stage))
            input_handler = next(
                item.callback for item in self.router.message.handlers
                if item.callback.__name__ == "on_service_parameter"
            )
            saved = self.message(1001, "1")
            await input_handler(saved)
            current = self.registry.get_schedule_plan("sound", "2026-11")
            self.assertEqual(current["slots"]["2026-11-15"]["Микшер"],
                             ["Алексей", "Борис"])
            self.assertEqual(current["approved"], approved)
            self.assertTrue(current["dirty"])
            self.assertIn("Микшер", saved.answer.await_args.args[0])

        asyncio.run(run())

    def test_slot_editor_separates_people_and_changes_only_selected_task_day(self):
        from bot.excel_parser import RosterMember

        self.registry.add_service_task(1001, "sound", "Микшер")
        self.registry.add_service_task(1001, "sound", "Запись")
        self.registry.sync_service_rosters({"sound": [
            RosterMember("Анна", ("Микшер", "Запись")),
            RosterMember("Борис", ("Микшер",)),
            RosterMember("Виктор", ("Микшер",)),
            RosterMember("Дмитрий", ("Микшер",)),
            RosterMember("Елена", ("Запись",)),
            RosterMember("Занят", ("Запись",)),
        ]})
        self.registry.set_responsible_contact(1001, "sound", "Микшер", "Анна", 3001)
        self.registry.set_service_member_day(1001, "sound", "Анна", -4)
        self.registry.set_service_member_day(1001, "sound", "Борис", -3)
        day = "2026-11-15"
        slots = {day: {"Микшер": ["Анна", "Борис", "Виктор"],
                       "Запись": ["Анна", "Занят"]}}
        self.plan(slots, tasks=[
            {"name": "Микшер", "needed": 1, "max": 6, "day": -3},
            {"name": "Запись", "needed": 1, "max": 6},
        ])
        self.registry.set_schedule_day(1001, "sound", "2026-11", day, "Анна", -1)

        async def run():
            dates = await self.press("rdy:pedit:sound:202611")
            day_screen = await self.press(next(
                b.callback_data for b in self.buttons(dates)
                if b.callback_data.startswith("rdy:pdate:")
            ))
            slot = await self.press(next(
                b.callback_data for b in self.buttons(day_screen)
                if b.callback_data.startswith("rdy:ptask:") and "Микшер" in b.text
            ))
            text = self.text(slot)
            self.assertIn("ЗВУК › График › 15.11 › Микшер", text)
            self.assertIn('<a href="tg://user?id=3001">Анна</a> (сб 14.11)', text)
            labels = [b.text for b in self.buttons(slot)]
            self.assertTrue(any("Дмитрий" in label for label in labels))
            self.assertFalse(any("Елена" in label or "Занят" in label for label in labels))
            self.assertEqual(
                [b.text for b in self.buttons(slot) if b.callback_data.startswith("rdy:pday:")],
                ["📅 Анна (ср)"],
            )
            help_screen = await self.press(next(
                b.callback_data for b in self.buttons(slot)
                if b.callback_data.startswith("rdy:pslothelp:")
            ))
            self.assertIn("только этой задачи", self.text(help_screen))
            slot = await self.press(self.buttons(help_screen)[0].callback_data)
            other = await self.press(next(
                b.callback_data for b in self.buttons(slot)
                if b.callback_data.startswith("rdy:pslotother:")
            ))
            labels = [b.text for b in self.buttons(other)]
            self.assertTrue(any("Елена" in label for label in labels))
            self.assertFalse(any("Занят" in label or "Дмитрий" in label for label in labels))
            slot = await self.press(next(
                b.callback_data for b in self.buttons(other)
                if b.callback_data.startswith("rdy:pslotmain:")
            ))
            pick_day = await self.press(next(
                b.callback_data for b in self.buttons(slot)
                if b.callback_data.startswith("rdy:pday:")
            ))
            self.assertIn("Другие задачи", self.text(pick_day))
            self.assertIn('<a href="tg://user?id=3001">Анна</a>', self.text(pick_day))
            self.assertIn("свой день участника или день служения", self.text(pick_day))
            slot = await self.press(next(
                b.callback_data for b in self.buttons(pick_day)
                if b.callback_data.startswith("rdy:pdset:") and b.callback_data.endswith(".-2")
            ))
            self.assertIn('Анна</a> (пт 13.11)', self.text(slot))
            plan = self.registry.get_schedule_plan("sound", "2026-11")
            person = next(p for p in self.registry.readiness_people("sound") if p["name"] == "Анна")
            self.assertEqual(schedule_plan.slot_offset(plan, person, day, "Микшер"), -2)
            self.assertEqual(schedule_plan.slot_offset(plan, person, day, "Запись"), -1)
            pick_day = await self.press(next(
                b.callback_data for b in self.buttons(slot)
                if b.callback_data.startswith("rdy:pday:")
            ))
            await self.press(next(
                b.callback_data for b in self.buttons(pick_day)
                if b.callback_data.startswith("rdy:pdset:") and b.callback_data.endswith(".n")
            ))
            plan = self.registry.get_schedule_plan("sound", "2026-11")
            self.assertEqual(schedule_plan.slot_offset(plan, person, day, "Микшер"), -3)
            self.assertEqual(schedule_plan.slot_offset(plan, person, day, "Запись"), -1)
            slot = await self.press(next(
                b.callback_data for b in self.buttons(day_screen)
                if b.callback_data.startswith("rdy:ptask:") and "Микшер" in b.text
            ))
            # Кнопка прежней версии графика не открывает устаревшее назначение.
            self.assertIn("График изменился", self.text(slot))
            dates = await self.press("rdy:pedit:sound:202611")
            day_screen = await self.press(next(
                b.callback_data for b in self.buttons(dates)
                if b.callback_data.startswith("rdy:pdate:")
            ))
            slot = await self.press(next(
                b.callback_data for b in self.buttons(day_screen)
                if b.callback_data.startswith("rdy:ptask:") and "Микшер" in b.text
            ))
            help_data = next(
                b.callback_data for b in self.buttons(slot)
                if b.callback_data.startswith("rdy:pslothelp:")
            )
            denied = await self.press(help_data, 9999)
            self.assertIn("Недостаточно прав", denied.answer.await_args.args[0])
            other = await self.press(next(
                b.callback_data for b in self.buttons(slot)
                if b.callback_data.startswith("rdy:pslotother:")
            ))
            added = await self.press(next(
                b.callback_data for b in self.buttons(other)
                if b.callback_data.startswith("rdy:padd:") and "Елена" in b.text
            ))
            self.assertIn("не приписан", self.text(added))
            plan = self.registry.get_schedule_plan("sound", "2026-11")
            self.assertIn("Елена", plan["slots"][day]["Микшер"])
            self.assertEqual(plan["approved"], slots)

        asyncio.run(run())

    def test_slot_editor_does_not_replace_newer_screen_after_async_load(self):
        self.plan(
            {"2026-11-15": {"Микшер": ["Анна"]}},
            tasks=[{"name": "Микшер", "needed": 1, "max": 1}],
        )

        async def run():
            dates = await self.press("rdy:pedit:sound:202611")
            day = await self.press(next(
                b.callback_data for b in self.buttons(dates)
                if b.callback_data.startswith("rdy:pdate:")
            ))
            task = next(
                b.callback_data for b in self.buttons(day)
                if b.callback_data.startswith("rdy:ptask:")
            )
            started, release = asyncio.Event(), asyncio.Event()
            original = asyncio.to_thread

            async def delay(func, *args, **kwargs):
                if func is schedule_plan.plan_summary:
                    started.set()
                    await release.wait()
                return await original(func, *args, **kwargs)

            with patch("bot.handlers.asyncio.to_thread", side_effect=delay):
                old = asyncio.create_task(self.press(task))
                await asyncio.wait_for(started.wait(), timeout=2)
                newer = await self.press("rdy:pedit:sound:202611")
                release.set()
                stale = await old
            self.assertIn("выберите дату", self.text(newer))
            stale.message.answer.assert_not_awaited()

        asyncio.run(run())

    def test_slot_callback_does_not_reopen_editor_after_cancel_during_load(self):
        from bot import readiness

        self.plan(
            {"2026-11-15": {"Микшер": ["Анна"]}},
            tasks=[{"name": "Микшер", "needed": 1, "max": 1}],
        )

        async def run():
            dates = await self.press("rdy:pedit:sound:202611")
            day = await self.press(next(
                b.callback_data for b in self.buttons(dates)
                if b.callback_data.startswith("rdy:pdate:")
            ))
            slot = await self.press(next(
                b.callback_data for b in self.buttons(day)
                if b.callback_data.startswith("rdy:ptask:")
            ))
            other = next(
                b.callback_data for b in self.buttons(slot)
                if b.callback_data.startswith("rdy:pslotother:")
            )
            started, release = asyncio.Event(), asyncio.Event()

            class DelayedLock:
                async def __aenter__(self):
                    started.set()
                    await release.wait()

                async def __aexit__(self, exc_type, exc, traceback):
                    return False

            cancel = next(
                item.callback for item in self.router.message.handlers
                if item.callback.__name__ == "on_cancel_service_command"
            )
            with patch.object(readiness, "service_lock", return_value=DelayedLock()):
                old = asyncio.create_task(self.press(other))
                await asyncio.wait_for(started.wait(), timeout=2)
                await cancel(self.message(text="/cancel"))
                release.set()
                stale = await old
            stale.message.answer.assert_not_awaited()
            self.assertIn("устарел", stale.answer.await_args.args[0])

        asyncio.run(run())

    def test_slot_changes_reject_cancel_while_waiting_for_mutation_lock(self):
        from bot import readiness

        self.registry.add_service_task(1001, "sound", "Микшер")
        self.registry.set_responsible_contact(
            1001, "sound", "Микшер", "Анна", 3001,
        )
        self.registry.set_service_member_day(1001, "sound", "Анна", -3)

        async def run():
            cancel = next(
                item.callback for item in self.router.message.handlers
                if item.callback.__name__ == "on_cancel_service_command"
            )
            for action in ("prem", "pclear", "pdset"):
                with self.subTest(action=action):
                    self.plan(
                        {"2026-11-15": {"Микшер": ["Анна"]}},
                        tasks=[{"name": "Микшер", "needed": 1, "max": 1}],
                    )
                    dates = await self.press("rdy:pedit:sound:202611")
                    day = await self.press(next(
                        b.callback_data for b in self.buttons(dates)
                        if b.callback_data.startswith("rdy:pdate:")
                    ))
                    slot = await self.press(next(
                        b.callback_data for b in self.buttons(day)
                        if b.callback_data.startswith("rdy:ptask:")
                    ))
                    if action == "pdset":
                        slot = await self.press(next(
                            b.callback_data for b in self.buttons(slot)
                            if b.callback_data.startswith("rdy:pday:")
                        ))
                        data = next(
                            b.callback_data for b in self.buttons(slot)
                            if b.callback_data.startswith("rdy:pdset:")
                            and b.callback_data.endswith(".-2")
                        )
                    else:
                        data = next(
                            b.callback_data for b in self.buttons(slot)
                            if b.callback_data.startswith(f"rdy:{action}:")
                        )
                    before = self.registry.get_schedule_plan("sound", "2026-11")
                    started, release = asyncio.Event(), asyncio.Event()
                    original_lock = readiness.service_lock
                    calls = 0

                    class DelayedLock:
                        async def __aenter__(self):
                            started.set()
                            await release.wait()

                        async def __aexit__(self, exc_type, exc, traceback):
                            return False

                    def delay_mutation_lock(service_id):
                        nonlocal calls
                        calls += 1
                        return DelayedLock() if calls == 2 else original_lock(service_id)

                    with patch.object(readiness, "service_lock", side_effect=delay_mutation_lock):
                        old = asyncio.create_task(self.press(data))
                        await asyncio.wait_for(started.wait(), timeout=2)
                        await cancel(self.message(text="/cancel"))
                        release.set()
                        stale = await old
                    self.assertIn("устарел", stale.answer.await_args.args[0])
                    stale.message.answer.assert_not_awaited()
                    after = self.registry.get_schedule_plan("sound", "2026-11")
                    self.assertEqual(after["slots"], before["slots"])
                    self.assertEqual(after["days"], before["days"])

        asyncio.run(run())

    def test_slot_add_rejects_cancel_during_summary(self):
        from bot.excel_parser import RosterMember

        self.registry.add_service_task(1001, "sound", "Микшер")
        self.registry.sync_service_rosters({"sound": [
            RosterMember("Анна", ("Микшер",)),
        ]})
        self.plan(
            {"2026-11-15": {"Микшер": []}},
            tasks=[{"name": "Микшер", "needed": 1, "max": 1}],
        )

        async def run():
            dates = await self.press("rdy:pedit:sound:202611")
            day = await self.press(next(
                b.callback_data for b in self.buttons(dates)
                if b.callback_data.startswith("rdy:pdate:")
            ))
            slot = await self.press(next(
                b.callback_data for b in self.buttons(day)
                if b.callback_data.startswith("rdy:ptask:")
            ))
            add = next(
                b.callback_data for b in self.buttons(slot)
                if b.callback_data.startswith("rdy:padd:")
            )
            started, release = asyncio.Event(), asyncio.Event()
            original = asyncio.to_thread

            async def delay(func, *args, **kwargs):
                if func is schedule_plan.plan_summary:
                    started.set()
                    await release.wait()
                return await original(func, *args, **kwargs)

            cancel = next(
                item.callback for item in self.router.message.handlers
                if item.callback.__name__ == "on_cancel_service_command"
            )
            with patch("bot.handlers.asyncio.to_thread", side_effect=delay):
                old = asyncio.create_task(self.press(add))
                await asyncio.wait_for(started.wait(), timeout=2)
                await cancel(self.message(text="/cancel"))
                release.set()
                stale = await old
            self.assertIn("устарел", stale.answer.await_args.args[0])
            stale.message.answer.assert_not_awaited()
            plan = self.registry.get_schedule_plan("sound", "2026-11")
            self.assertEqual(plan["slots"]["2026-11-15"]["Микшер"], [])

        asyncio.run(run())

    def test_slot_shared_user_rejects_cancel_while_waiting_for_lock(self):
        from bot import readiness

        self.registry.add_service_task(1001, "sound", "Микшер")
        self.plan(
            {"2026-11-15": {"Микшер": []}},
            tasks=[{"name": "Микшер", "needed": 1, "max": 1}],
        )

        async def run():
            dates = await self.press("rdy:pedit:sound:202611")
            day = await self.press(next(
                b.callback_data for b in self.buttons(dates)
                if b.callback_data.startswith("rdy:pdate:")
            ))
            slot = await self.press(next(
                b.callback_data for b in self.buttons(day)
                if b.callback_data.startswith("rdy:ptask:")
            ))
            picker = await self.press(next(
                b.callback_data for b in self.buttons(slot)
                if b.callback_data.startswith("rdy:pnew:")
            ))
            request = picker.message.answer.await_args.kwargs[
                "reply_markup"
            ].keyboard[0][0].request_users
            shared = self.message(text=None)
            shared.users_shared = SimpleNamespace(
                request_id=request.request_id,
                users=[SimpleNamespace(
                    user_id=3001, first_name="Анна", last_name="", username="",
                )],
            )
            handler = next(
                item.callback for item in self.router.message.handlers
                if item.callback.__name__ == "on_users_shared"
            )
            cancel = next(
                item.callback for item in self.router.message.handlers
                if item.callback.__name__ == "on_cancel_service_command"
            )
            started, release = asyncio.Event(), asyncio.Event()

            class DelayedLock:
                async def __aenter__(self):
                    started.set()
                    await release.wait()

                async def __aexit__(self, exc_type, exc, traceback):
                    return False

            with patch.object(readiness, "service_lock", return_value=DelayedLock()):
                old = asyncio.create_task(handler(shared))
                await asyncio.wait_for(started.wait(), timeout=2)
                await cancel(self.message(text="/cancel"))
                release.set()
                await old
            self.assertIn("устарел", shared.answer.await_args.args[0])
            plan = self.registry.get_schedule_plan("sound", "2026-11")
            self.assertEqual(plan["slots"]["2026-11-15"]["Микшер"], [])
            self.assertNotIn(
                "Анна", [p["name"] for p in self.registry.readiness_people("sound")]
            )

        asyncio.run(run())

    def test_slot_shared_user_rejects_cancel_during_summary(self):
        self.registry.add_service_task(1001, "sound", "Микшер")
        self.plan(
            {"2026-11-15": {"Микшер": []}},
            tasks=[{"name": "Микшер", "needed": 1, "max": 1}],
        )

        async def run():
            dates = await self.press("rdy:pedit:sound:202611")
            day = await self.press(next(
                b.callback_data for b in self.buttons(dates)
                if b.callback_data.startswith("rdy:pdate:")
            ))
            slot = await self.press(next(
                b.callback_data for b in self.buttons(day)
                if b.callback_data.startswith("rdy:ptask:")
            ))
            picker = await self.press(next(
                b.callback_data for b in self.buttons(slot)
                if b.callback_data.startswith("rdy:pnew:")
            ))
            request = picker.message.answer.await_args.kwargs[
                "reply_markup"
            ].keyboard[0][0].request_users
            shared = self.message(text=None)
            shared.users_shared = SimpleNamespace(
                request_id=request.request_id,
                users=[SimpleNamespace(
                    user_id=3001, first_name="Анна", last_name="", username="",
                )],
            )
            handler = next(
                item.callback for item in self.router.message.handlers
                if item.callback.__name__ == "on_users_shared"
            )
            cancel = next(
                item.callback for item in self.router.message.handlers
                if item.callback.__name__ == "on_cancel_service_command"
            )
            started, release = asyncio.Event(), asyncio.Event()
            original = asyncio.to_thread

            async def delay(func, *args, **kwargs):
                if func is schedule_plan.plan_summary:
                    started.set()
                    await release.wait()
                return await original(func, *args, **kwargs)

            with patch("bot.handlers.asyncio.to_thread", side_effect=delay):
                old = asyncio.create_task(handler(shared))
                await asyncio.wait_for(started.wait(), timeout=2)
                await cancel(self.message(text="/cancel"))
                release.set()
                await old
            self.assertIn("устарел", shared.answer.await_args.args[0])
            plan = self.registry.get_schedule_plan("sound", "2026-11")
            self.assertEqual(plan["slots"]["2026-11-15"]["Микшер"], [])
            self.assertNotIn(
                "Анна", [p["name"] for p in self.registry.readiness_people("sound")]
            )

            dates = await self.press("rdy:pedit:sound:202611")
            day = await self.press(next(
                b.callback_data for b in self.buttons(dates)
                if b.callback_data.startswith("rdy:pdate:")
            ))
            slot = await self.press(next(
                b.callback_data for b in self.buttons(day)
                if b.callback_data.startswith("rdy:ptask:")
            ))
            picker = await self.press(next(
                b.callback_data for b in self.buttons(slot)
                if b.callback_data.startswith("rdy:pnew:")
            ))
            request = picker.message.answer.await_args.kwargs[
                "reply_markup"
            ].keyboard[0][0].request_users
            shared.users_shared.request_id = request.request_id
            await handler(shared)
            plan = self.registry.get_schedule_plan("sound", "2026-11")
            self.assertEqual(plan["slots"]["2026-11-15"]["Микшер"], ["Анна"])
            self.assertIn(
                "Анна", [p["name"] for p in self.registry.readiness_people("sound")]
            )

        asyncio.run(run())

    def test_bulk_delete_rejects_selection_not_shown_on_pressed_screen(self):
        self.registry.add_service_task(1001, "sound", "Микшер")
        self.plan(
            {"2026-11-15": {"Микшер": ["Анна", "Борис"]}},
            tasks=[{"name": "Микшер", "needed": 1, "max": 2}],
        )

        async def run():
            dates = await self.press("rdy:pedit:sound:202611")
            day = await self.press(next(
                b.callback_data for b in self.buttons(dates)
                if b.callback_data.startswith("rdy:pdate:")
            ))
            quick = next(
                b.callback_data for b in self.buttons(day)
                if b.callback_data.startswith("rdy:pquick:") and b.callback_data.endswith(".remove")
            )
            await self.press(quick.replace("rdy:pquick:", "rdy:pbulkinit:").removesuffix(".remove"))
            input_handler = next(
                item.callback for item in self.router.message.handlers
                if item.callback.__name__ == "on_service_parameter"
            )
            first = self.message(text="1")
            sending, send_release = asyncio.Event(), asyncio.Event()
            started, release = asyncio.Event(), asyncio.Event()
            original = asyncio.to_thread

            async def delay_send(text, **kwargs):
                if "Выбраны номера: 1" in text:
                    sending.set()
                    await send_release.wait()
                return SimpleNamespace(message_id=4567)

            async def delay(func, *args, **kwargs):
                if func is schedule_plan.plan_summary:
                    started.set()
                    await release.wait()
                return await original(func, *args, **kwargs)

            first.answer.side_effect = delay_send
            pending_first = asyncio.create_task(input_handler(first))
            await asyncio.wait_for(sending.wait(), timeout=2)
            second = self.message(text="2")
            with patch("bot.handlers.asyncio.to_thread", side_effect=delay):
                pending_render = asyncio.create_task(input_handler(second))
                await asyncio.wait_for(started.wait(), timeout=2)
                send_release.set()
                await pending_first
                release.set()
                await pending_render
            second.answer.assert_not_awaited()
            old_delete = next(
                b.callback_data for b in self.buttons(SimpleNamespace(message=first))
                if b.callback_data.startswith("rdy:pbulkdel:")
            )
            stale = await self.press(old_delete)
            self.assertIn("устарел", stale.answer.await_args.args[0])
            stale.message.answer.assert_not_awaited()
            self.assertEqual(
                self.registry.get_schedule_plan("sound", "2026-11")["slots"]["2026-11-15"]["Микшер"],
                ["Анна", "Борис"],
            )
            retry = self.message(text="2")
            await input_handler(retry)
            self.assertIn("Выбраны номера: 2", retry.answer.await_args.args[0])
            current = next(
                b.callback_data for b in self.buttons(SimpleNamespace(message=retry))
                if b.callback_data.startswith("rdy:pbulkdel:")
            )
            task_list = await self.press(current)
            saved = await self.press(next(
                b.callback_data for b in self.buttons(task_list)
                if b.callback_data.startswith("rdy:pbulksave:")
            ))
            self.assertIn("Анна", self.text(saved))
            self.assertEqual(
                self.registry.get_schedule_plan("sound", "2026-11")["slots"]["2026-11-15"]["Микшер"],
                ["Анна"],
            )

        asyncio.run(run())

    def test_bulk_add_rejects_selection_from_an_older_screen(self):
        from bot import readiness
        from bot.excel_parser import RosterMember

        self.registry.add_service_task(1001, "sound", "Микшер")
        self.registry.sync_service_rosters({"sound": [
            RosterMember("Анна", ("Микшер",)),
            RosterMember("Борис", ("Микшер",)),
        ]})
        self.plan(
            {"2026-11-15": {"Микшер": []}},
            tasks=[{"name": "Микшер", "needed": 1, "max": 2}],
        )

        async def run():
            dates = await self.press("rdy:pedit:sound:202611")
            day = await self.press(next(
                b.callback_data for b in self.buttons(dates)
                if b.callback_data.startswith("rdy:pdate:")
            ))
            quick = next(
                b.callback_data for b in self.buttons(day)
                if b.callback_data.startswith("rdy:pquick:") and b.callback_data.endswith(".remove")
            )
            removal = await self.press(
                quick.replace("rdy:pquick:", "rdy:pbulkinit:").removesuffix(".remove")
            )
            tasks = await self.press(next(
                b.callback_data for b in self.buttons(removal)
                if b.callback_data.startswith("rdy:pbulkkeep:")
            ))
            candidates = await self.press(next(
                b.callback_data for b in self.buttons(tasks)
                if b.callback_data.startswith("rdy:pbulktask:")
            ))
            input_handler = next(
                item.callback for item in self.router.message.handlers
                if item.callback.__name__ == "on_service_parameter"
            )
            picker = next(
                b.callback_data for b in self.buttons(candidates)
                if b.callback_data.startswith("rdy:pnew:")
            )
            started, release = asyncio.Event(), asyncio.Event()
            original_lock = readiness.service_lock
            calls = 0

            class DelayedLock:
                async def __aenter__(self):
                    started.set()
                    await release.wait()

                async def __aexit__(self, exc_type, exc, traceback):
                    return False

            def delay_first_lock(service_id):
                nonlocal calls
                calls += 1
                return DelayedLock() if calls == 1 else original_lock(service_id)

            first = self.message(text="1")
            with patch.object(readiness, "service_lock", side_effect=delay_first_lock):
                pending_picker = asyncio.create_task(self.press(picker))
                await asyncio.wait_for(started.wait(), timeout=2)
                await input_handler(first)
                release.set()
                stale_picker = await pending_picker
            stale_picker.message.answer.assert_not_awaited()
            self.assertIn("устарел", stale_picker.answer.await_args.args[0])
            old_add = next(
                b.callback_data for b in self.buttons(SimpleNamespace(message=first))
                if b.callback_data.startswith("rdy:pbulkadd:")
            )
            second = self.message(text="2")
            await input_handler(second)
            stale = await self.press(old_add)
            self.assertIn("устарел", stale.answer.await_args.args[0])
            stale.message.answer.assert_not_awaited()
            added = await self.press(next(
                b.callback_data for b in self.buttons(SimpleNamespace(message=second))
                if b.callback_data.startswith("rdy:pbulkadd:")
            ))
            saved = await self.press(next(
                b.callback_data for b in self.buttons(added)
                if b.callback_data.startswith("rdy:pbulksave:")
            ))
            self.assertIn("Борис", self.text(saved))
            self.assertEqual(
                self.registry.get_schedule_plan("sound", "2026-11")["slots"]["2026-11-15"]["Микшер"],
                ["Борис"],
            )

        asyncio.run(run())

    def test_slot_editor_paginates_candidates_and_keeps_navigation(self):
        from bot.excel_parser import RosterMember

        self.registry.add_service_task(1001, "sound", "Микшер")
        self.registry.sync_service_rosters({"sound": [
            RosterMember(f"Участник {i}", ("Микшер",)) for i in range(12)
        ]})
        self.plan({"2026-11-15": {"Микшер": []}},
                  tasks=[{"name": "Микшер", "needed": 1, "max": 1}])

        async def run():
            dates = await self.press("rdy:pedit:sound:202611")
            day = await self.press(next(
                b.callback_data for b in self.buttons(dates)
                if b.callback_data.startswith("rdy:pdate:")
            ))
            slot = await self.press(next(
                b.callback_data for b in self.buttons(day)
                if b.callback_data.startswith("rdy:ptask:")
            ))
            self.assertIn("Страница 1/2", self.text(slot))
            buttons = self.buttons(slot)
            self.assertEqual(sum(b.callback_data.startswith("rdy:padd:") for b in buttons), 6)
            self.assertTrue(any(b.text == "❓ Справка" for b in buttons))
            first_candidate = next(b.callback_data for b in buttons if b.callback_data.startswith("rdy:padd:"))
            page = await self.press(next(
                b.callback_data for b in buttons if b.callback_data.startswith("rdy:pslotpage:")
            ))
            self.assertIn("Страница 2/2", self.text(page))
            self.assertTrue(any(b.text == "❓ Справка" for b in self.buttons(page)))
            stale = await self.press(first_candidate)
            self.assertIn("устарел", stale.answer.await_args.args[0].lower())
            help_screen = await self.press(next(
                b.callback_data for b in self.buttons(page)
                if b.callback_data.startswith("rdy:pslothelp:")
            ))
            page = await self.press(self.buttons(help_screen)[0].callback_data)
            self.assertIn("Страница 2/2", self.text(page))
            chosen = next(b for b in self.buttons(page) if b.callback_data.startswith("rdy:padd:"))
            name = chosen.text.split(" ", 1)[1]
            await self.press(chosen.callback_data)
            self.assertEqual(
                self.registry.get_schedule_plan("sound", "2026-11")["slots"]["2026-11-15"]["Микшер"],
                [name],
            )

        asyncio.run(run())

    def test_slot_editor_keeps_task_members_with_different_poll_answers(self):
        from bot.excel_parser import RosterMember

        self.registry.add_service_task(1001, "sound", "Микшер")
        self.registry.sync_service_rosters({"sound": [
            RosterMember(name, ("Микшер",)) for name in ("Анна", "Борис", "Вера")
        ]})
        for index, name in enumerate(("Анна", "Борис", "Вера"), 3001):
            self.registry.set_responsible_contact(1001, "sound", "Микшер", name, index)
        run = self.registry.create_readiness_run(
            "sound", "2026-11", status="open",
            dates=["2026-11-15", "2026-11-22"], poll_id="editor-poll",
        )
        self.registry.record_readiness_answer(
            "editor-poll", 3001, "", "Анна", ["2026-11-22"],
        )
        self.registry.record_readiness_answer(
            "editor-poll", 3003, "", "Вера", ["2026-11-15"],
        )
        self.registry.close_readiness_run(run["id"])
        self.registry.save_schedule_draft(
            "sound", "2026-11", run_id=run["id"], dates=["2026-11-15"],
            tasks=[{"name": "Микшер", "needed": 1, "max": 1}],
            slots={"2026-11-15": {"Микшер": []}}, last_served={},
        )

        async def run_test():
            dates = await self.press("rdy:pedit:sound:202611")
            day = await self.press(next(
                b.callback_data for b in self.buttons(dates)
                if b.callback_data.startswith("rdy:pdate:")
            ))
            slot = await self.press(next(
                b.callback_data for b in self.buttons(day)
                if b.callback_data.startswith("rdy:ptask:")
            ))
            labels = [b.text for b in self.buttons(slot)]
            self.assertIn("⚠️ Анна", labels)
            self.assertIn("⚪ Борис", labels)
            self.assertIn("✅ Вера", labels)
            self.assertIn("Никого", self.text(slot))

        asyncio.run(run_test())

    def test_slot_day_button_compares_with_selected_date_without_task_day(self):
        from bot.excel_parser import RosterMember

        self.registry.add_service_task(1001, "sound", "Микшер")
        self.registry.sync_service_rosters({"sound": [
            RosterMember("Анна", ("Микшер",)),
            RosterMember("Борис", ("Микшер",)),
        ]})
        self.registry.set_service_member_day(1001, "sound", "Анна", 0)
        self.registry.set_service_member_day(1001, "sound", "Борис", -1)
        self.plan({"2026-11-15": {"Микшер": ["Анна", "Борис"]}},
                  tasks=[{"name": "Микшер", "needed": 1, "max": 2}])

        async def run():
            dates = await self.press("rdy:pedit:sound:202611")
            day = await self.press(next(
                b.callback_data for b in self.buttons(dates)
                if b.callback_data.startswith("rdy:pdate:")
            ))
            slot = await self.press(next(
                b.callback_data for b in self.buttons(day)
                if b.callback_data.startswith("rdy:ptask:")
            ))
            self.assertEqual(
                [b.text for b in self.buttons(slot) if b.callback_data.startswith("rdy:pday:")],
                ["📅 Борис (сб)"],
            )

        asyncio.run(run())

    def test_add_day_tasks_are_paired_in_quick_and_legacy_editors(self):
        names = ["Микшер", "Запись", "Пульт", "Сборка", "Камера"]
        self.plan(
            {"2026-11-15": {name: [] for name in names}},
            tasks=[{"name": name, "needed": 1} for name in names],
        )

        async def run():
            dates = await self.press("rdy:pedit:sound:202611")
            day = await self.press(next(
                b.callback_data for b in self.buttons(dates)
                if b.callback_data.startswith("rdy:pdate:")
            ))
            quick_add = next(
                b.callback_data for b in self.buttons(day)
                if b.callback_data.startswith("rdy:pquick:")
                and b.callback_data.endswith(".add")
            )
            quick = await self.press(quick_add)
            quick_rows = quick.message.answer.await_args.kwargs[
                "reply_markup"
            ].inline_keyboard
            self.assertEqual([len(row) for row in quick_rows], [2, 2, 1, 1])
            self.assertEqual(quick_rows[-1][0].text, "⬅️ К дате")
            quick_buttons = [button for row in quick_rows[:-1] for button in row]
            self.assertEqual(len(quick_buttons), len(names))
            for button in quick_buttons:
                self.assertTrue(button.callback_data.startswith("rdy:pbulktask:"))
                screen = await self.press(button.callback_data)
                index = int(button.callback_data.rsplit(".", 1)[1])
                self.assertIn(f"«{names[index]}»", self.text(screen))
                quick = await self.press(next(
                    b.callback_data for b in self.buttons(screen)
                    if b.callback_data.startswith("rdy:pbulkback:")
                ))

            legacy_init = quick_add.replace(
                "rdy:pquick:", "rdy:pbulkinit:", 1,
            ).removesuffix(".add")
            removal = await self.press(legacy_init)
            legacy = await self.press(next(
                b.callback_data for b in self.buttons(removal)
                if b.callback_data.startswith("rdy:pbulkkeep:")
            ))
            legacy_rows = legacy.message.answer.await_args.kwargs[
                "reply_markup"
            ].inline_keyboard
            self.assertEqual([len(row) for row in legacy_rows], [2, 2, 1, 1, 1])
            self.assertEqual(
                [button.text for row in legacy_rows[:3] for button in row],
                [button.text for button in quick_buttons],
            )
            self.assertEqual(legacy_rows[-1][0].text, "⬅️ К участникам дня")

        asyncio.run(run())

    def test_bulk_editor_does_not_replace_newer_editor_after_async_load(self):
        self.registry.add_service_task(1001, "sound", "Микшер")
        self.plan(
            {"2026-11-15": {"Микшер": ["Алексей"]}},
            tasks=[{"name": "Микшер", "needed": 1}],
        )

        async def run():
            dates = await self.press("rdy:pedit:sound:202611")
            day = await self.press(next(
                b.callback_data for b in self.buttons(dates)
                if b.callback_data.startswith("rdy:pdate:")
            ))
            remove = next(
                b.callback_data for b in self.buttons(day)
                if b.callback_data.startswith("rdy:pquick:") and b.callback_data.endswith(".remove")
            )
            add = next(
                b.callback_data for b in self.buttons(day)
                if b.callback_data.startswith("rdy:pquick:") and b.callback_data.endswith(".add")
            )
            started, release = asyncio.Event(), asyncio.Event()
            original_to_thread = asyncio.to_thread
            calls = 0

            async def delay_first(func, *args, **kwargs):
                nonlocal calls
                if func is schedule_plan.plan_summary:
                    calls += 1
                    if calls == 1:
                        started.set()
                        await release.wait()
                return await original_to_thread(func, *args, **kwargs)

            with patch("bot.handlers.asyncio.to_thread", side_effect=delay_first):
                old_task = asyncio.create_task(self.press(remove))
                await asyncio.wait_for(started.wait(), timeout=2)
                newer = await self.press(add)
                release.set()
                old = await old_task
            self.assertIn("Выберите задачу", self.text(newer))
            old.message.answer.assert_not_awaited()

            started.clear()
            release.clear()
            calls = 0
            with patch("bot.handlers.asyncio.to_thread", side_effect=delay_first):
                old_task = asyncio.create_task(self.press(remove))
                await asyncio.wait_for(started.wait(), timeout=2)
                newer = await self.press("rdy:pedit:sound:202611")
                release.set()
                old = await old_task
            self.assertIn("выберите дату", self.text(newer))
            old.message.answer.assert_not_awaited()

        asyncio.run(run())

    def test_removing_from_day_is_immediate_and_rejects_stale_input(self):
        from bot.excel_parser import RosterMember

        self.registry.add_service_task(1001, "sound", "Микшер")
        self.registry.add_service_task(1001, "sound", "Запись")
        self.registry.sync_service_rosters({"sound": [
            RosterMember("Анна", ("Микшер", "Запись")),
            RosterMember("Борис", ("Микшер",)),
        ]})
        for task in ("Микшер", "Запись"):
            self.registry.set_responsible_contact(
                1001, "sound", task, "Анна", 3001,
            )
        slots = {"2026-11-15": {"Микшер": ["Анна", "Борис"], "Запись": ["Анна"]}}
        self.plan(slots, tasks=[
            {"name": "Микшер", "needed": 1},
            {"name": "Запись", "needed": 1, "day": -3},
            {"name": "Пустая задача", "needed": 1},
        ])
        approved = self.registry.get_schedule_plan("sound", "2026-11")["approved"]

        async def run():
            dates = await self.press("rdy:pedit:sound:202611")
            day = await self.press(next(
                b.callback_data for b in self.buttons(dates)
                if b.callback_data.startswith("rdy:pdate:")
            ))
            for button in self.buttons(day):
                if button.callback_data.startswith("rdy:ptask:"):
                    self.assertNotIn("Анна", button.text)
                    self.assertNotIn("Борис", button.text)
            remove = next(
                b.callback_data for b in self.buttons(day)
                if b.callback_data.startswith("rdy:pquick:") and b.callback_data.endswith(".remove")
            )
            removal = await self.press(remove)
            text = self.text(removal)
            self.assertIn("🗓 Неделя", text)
            self.assertIn("Микшер", text)
            self.assertIn("Запись", text)
            self.assertIn("Пустая задача", text)
            self.assertEqual(text.count('1. <a href="tg://user?id=3001">Анна</a>'), 2)
            self.assertIn("2. Борис", text)
            self.assertNotIn("Анна —", text)
            handler = next(
                item.callback for item in self.router.message.handlers
                if item.callback.__name__ == "on_service_parameter"
            )
            invalid = self.message(1001, "99")
            await handler(invalid)
            self.assertEqual(self.registry.get_schedule_plan("sound", "2026-11")["slots"], slots)
            await handler(self.message(1001, "1"))
            current = self.registry.get_schedule_plan("sound", "2026-11")
            self.assertEqual(current["slots"]["2026-11-15"]["Микшер"], ["Борис"])
            self.assertEqual(current["slots"]["2026-11-15"]["Запись"], [])
            self.assertEqual(current["approved"], approved)

            dates = await self.press("rdy:pedit:sound:202611")
            day = await self.press(next(
                b.callback_data for b in self.buttons(dates)
                if b.callback_data.startswith("rdy:pdate:")
            ))
            await self.press(next(
                b.callback_data for b in self.buttons(day)
                if b.callback_data.startswith("rdy:pquick:") and b.callback_data.endswith(".remove")
            ))
            self.registry.set_schedule_slots(1001, "sound", "2026-11", slots)
            stale = self.message(1001, "1")
            await handler(stale)
            self.assertIn("График изменился", stale.answer.await_args.args[0])
            self.assertEqual(self.registry.get_schedule_plan("sound", "2026-11")["slots"], slots)

        asyncio.run(run())

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
                self.assertLessEqual(len(buttons), 10)
                self.assertTrue(all(len(item.callback_data.encode()) <= 64 for item in buttons))
                self.assertIn(
                    "rdy:pedit:sound:202611",
                    [b.callback_data for b in buttons],
                )
                self.assertIn("rdy:open:sound", [b.callback_data for b in buttons])
                self.assertIn("service:service_excel:sound", [b.callback_data for b in buttons])
                self.assertIn("rdy:summary:sound", [b.callback_data for b in buttons])
                self.assertTrue(any(b.text == "❓ Справка" for b in buttons))
                self.assertLessEqual(visible_units(self.text(shown)), 4096)
                next_page = next((item for item in buttons if item.text == "➡️ Далее"), None)
                if next_page is None:
                    break
                shown = await self.press(next_page.callback_data)
            self.assertGreater(len(texts), 1)
            for name in names:
                self.assertIn(name, "\n".join(texts))

        asyncio.run(run())

    def summary(self):
        return plain_text(month_overview(
            self.registry, 1001, "sound", self.now.date(), include_load=True,
        ))

    def test_auto_help_and_next_month_preserve_approved_current_graph(self):
        self.registry.add_service_task(1001, "sound", "Микшер")
        self.registry.set_responsible_contact(1001, "sound", "Микшер", "A", 3001)
        self.plan({"2026-11-15": {"Микшер": ["A"]}})
        before = self.registry.get_schedule_plan("sound", "2026-11")

        async def run():
            shown = await self.press("rdy:pfree:sound:202611", 2001)
            self.assertIn("График уже составлен", self.text(shown))
            buttons = self.buttons(shown)
            self.assertLessEqual(len(buttons), 6)
            help_button = next(b for b in buttons if b.text == "❓ Справка")
            help_screen = await self.press(help_button.callback_data, 2001)
            for text in ("Изменить", "Пересчитать", "ручные правки", "Утвердить",
                         "До утверждения", "другого месяца"):
                self.assertIn(text, self.text(help_screen))
            returned = await self.press(self.buttons(help_screen)[0].callback_data, 2001)
            self.assertIn("График", self.text(returned))
            next_button = next(b for b in self.buttons(returned) if b.text == "🧮 Авто дек")
            self.assertEqual(next_button.callback_data, "rdy:pfree:sound:202612")
            following = await self.press(next_button.callback_data, 2001)
            self.assertIn("декабрь 2026", self.text(following))
            self.assertIn("🧮 Авто ноя", [b.text for b in self.buttons(following)])
            draft = self.registry.get_schedule_plan("sound", "2026-12")
            self.assertIsNone(draft["run_id"])
            self.assertIsNone(draft["approved"])
            self.assertTrue(any(names for tasks in draft["slots"].values()
                                for names in tasks.values()))
            await self.press(next_button.callback_data, 2001)
            self.assertEqual(self.registry.get_schedule_plan("sound", "2026-12"), draft)
            denied = await self.press(help_button.callback_data, 9999)
            self.assertIn("Недостаточно прав", denied.answer.await_args.args[0])
            self.assertEqual(self.registry.get_schedule_plan("sound", "2026-11"), before)
            self.context.load_upcoming_service_events.assert_not_awaited()
            self.context.bot.send_message.assert_not_awaited()
        asyncio.run(run())

    def test_auto_month_link_crosses_year_and_blocks_unrelated_months(self):
        self.now = self.now.replace(month=12)
        self.plan({"2026-12-06": {"Микшер": ["A"]}}, month="2026-12")

        async def run():
            shown = await self.press("rdy:pfree:sound:202612")
            button = next(b for b in self.buttons(shown) if b.text == "🧮 Авто янв")
            self.assertEqual(button.callback_data, "rdy:pfree:sound:202701")
            self.assertTrue(all(len(b.callback_data.encode()) <= 64 for b in self.buttons(shown)))
            await self.press("rdy:pfree:sound:202702")
            self.assertIsNone(self.registry.get_schedule_plan("sound", "2027-02"))
        asyncio.run(run())

    def test_auto_layout_and_breadcrumb_preserve_draft_and_approved_data(self):
        async def run():
            for month, day in (("2026-11", "2026-11-22"), ("2026-12", "2026-12-06")):
                for state in ("draft", "approved", "dirty"):
                    slots = {day: {"Микшер": ["A"]}}
                    self.plan(slots, approved=state != "draft", month=month)
                    if state == "dirty":
                        self.plan({day: {"Микшер": ["B"]}}, approved=False, month=month)
                    before = self.registry.get_schedule_plan("sound", month)
                    shown = await self.press(f"rdy:pfree:sound:{month.replace('-', '')}")
                    self.assertIn("ЗВУК › График › Авто ›", self.text(shown))
                    self.assertNotIn("График › Опрос", self.text(shown))
                    rows = shown.message.answer.await_args.kwargs["reply_markup"].inline_keyboard
                    self.assertEqual(
                        [b.text for b in rows[-1]], ["⬅️ К графику", "❓ Справка"],
                    )
                    self.assertEqual([b.text for b in rows[0]], ["✏️ Изменить", "🔄 Пересчитать"])
                    if state != "approved":
                        self.assertEqual(len(rows[1]), 2)
                        self.assertTrue(rows[1][0].text.startswith("✅ Утвердить"))
                        self.assertTrue(rows[1][1].text.startswith("🧮 Авто"))
                    self.assertEqual(self.registry.get_schedule_plan("sound", month), before)
            self.context.bot.send_message.assert_not_awaited()

        asyncio.run(run())

    def test_plan_editor_orders_tasks_without_changing_original_callback_indices(self):
        names = ["пироги", "Скатерти", "в смене", "старший"]
        self.registry.add_service_task(1001, "kitchen", "Скатерти")
        self.registry.save_schedule_draft(
            "kitchen", "2026-11", run_id=None, dates=["2026-11-22"],
            tasks=[{"name": name, "needed": 1, "max": 1} for name in names],
            slots={"2026-11-22": {name: [f"A{index}"] for index, name in enumerate(names)}},
            last_served={},
        )
        before = self.registry.get_schedule_plan("kitchen", "2026-11")

        async def run():
            dates = await self.press("rdy:pedit:kitchen:202611")
            day_button = next(
                b for b in self.buttons(dates) if b.callback_data.startswith("rdy:pdate:")
            )
            day = await self.press(day_button.callback_data)
            choices = [
                b for b in self.buttons(day) if b.callback_data.startswith("rdy:ptask:")
            ]
            self.assertEqual(
                [int(b.callback_data.split(".")[-1]) for b in choices], [3, 2, 1, 0],
            )
            self.assertEqual(
                [next(name for name in names if name in b.text) for b in choices],
                ["старший", "в смене", "Скатерти", "пироги"],
            )
            for button in choices:
                task = await self.press(button.callback_data)
                index = int(button.callback_data.split(".")[-1])
                self.assertIn(f"› {names[index]}", self.text(task))
            self.assertEqual(self.registry.get_schedule_plan("kitchen", "2026-11"), before)

        asyncio.run(run())

    def test_auto_next_month_does_not_bypass_existing_readiness_poll(self):
        run = self.registry.create_readiness_run(
            "sound", "2026-12", status="open", dates=["2026-12-06"],
            deadline="2026-11-25T18:00:00+00:00",
        )

        async def check():
            shown = await self.press("rdy:pfree:sound:202612", 2001)
            self.assertIn("Опрос этого месяца уже есть", self.text(shown))
            self.assertIsNone(self.registry.get_schedule_plan("sound", "2026-12"))
            self.assertEqual(self.registry.get_readiness_run("sound", "2026-12"), run)
        asyncio.run(check())

    def test_summary_load_uses_effective_graph_previous_replacements_and_ignores_draft(self):
        self.registry.add_service_task(1001, "sound", "Микшер")
        for name, telegram_id in (("Алексей", 3001), ("Мария", 3002), ("Борис", 3003)):
            self.registry.set_responsible_contact(1001, "sound", "Микшер", name, telegram_id)
        self.plan({"2026-10-04": {"Микшер": ["Алексей"]}}, month="2026-10")
        self.registry.set_replacement(
            "sound", "2026-10-04", "Микшер", "Алексей", "Мария", 3002, "", "admin", 1001,
        )
        self.plan({
            "2026-11-01": {"Микшер": ["Алексей"]},
            "2026-11-15": {"Микшер": ["Мария"]},
        })
        self.registry.set_replacement(
            "sound", "2026-11-01", "Микшер", "Алексей", "Мария", 3002, "", "admin", 1001,
        )
        self.plan({"2026-11-22": {"Микшер": ["Черновик"]}}, approved=False)
        text = self.summary()
        self.assertIn("2 — Мария · интервалы, нед.: 4, 2", text)
        self.assertIn("0 — Алексей", text)
        self.assertIn("0 — Борис", text)
        self.assertNotIn("Черновик", text)
        self.assertIn("неутверждённые правки", text)
        self.assertNotIn("0 — Алексей · интервалы", text)

    def test_summary_keeps_namesakes_load_history_and_replacements_separate(self):
        for task in ("Микшер", "Монтаж", "Запас"):
            self.registry.add_service_task(1001, "sound", task)
        for task, telegram_id, username in (
            ("Микшер", 3101, "gleb_one"), ("Монтаж", 3102, "gleb_two"),
            ("Запас", 3103, "gleb_three"),
        ):
            self.registry.set_responsible_contact(
                1001, "sound", task, "Глеб", telegram_id, username,
            )
        # Legacy per-task bindings can contain separate accounts with one name.
        with self.registry._connection() as connection:
            for task, telegram_id, username in (
                ("Микшер", 3101, "gleb_one"), ("Монтаж", 3102, "gleb_two"),
            ):
                connection.execute(
                    "UPDATE responsible_contacts SET telegram_id = ?, username = ? "
                    "WHERE service_id = 'sound' AND task = ?",
                    (telegram_id, username, task),
                )
        people = self.registry.readiness_people("sound")
        keys = {person["telegram_id"]: person["key"] for person in people}
        self.assertEqual(len(set(keys.values())), 3)
        tasks = [{"name": task, "needed": 1} for task in ("Микшер", "Монтаж", "Запас")]
        self.plan({
            "2026-10-04": {"Микшер": [keys[3101]]},
            "2026-10-25": {"Монтаж": [keys[3102]]},
        }, month="2026-10", tasks=tasks)
        self.plan({
            "2026-11-01": {"Микшер": [keys[3101]]},
            "2026-11-08": {"Монтаж": [keys[3102]]},
            "2026-11-15": {"Микшер": [keys[3101]]},
        }, tasks=tasks)
        text = self.summary()
        self.assertIn("2 — Глеб (@gleb_one) · интервалы, нед.: 4, 2", text)
        self.assertIn("1 — Глеб (@gleb_two) · интервалы, нед.: 2", text)
        self.assertIn("0 — Глеб (@gleb_three)", text)
        saved = self.registry.get_schedule_plan("sound", "2026-11")
        self.registry.save_schedule_draft(
            "sound", "2026-11", run_id=None, dates=saved["dates"],
            tasks=tasks, slots=saved["slots"],
            last_served={keys[3101]: "2026-10-04", keys[3102]: "2026-10-25"},
        )
        with patch.object(self.registry, "list_approved_schedule_plans", return_value=[]):
            text = self.summary()
        self.assertIn("2 — Глеб (@gleb_one) · интервалы, нед.: 4, 2", text)
        self.assertIn("1 — Глеб (@gleb_two) · интервалы, нед.: 2", text)
        # A same-name substitute is identified by Telegram, not by the task permit.
        with patch.object(self.registry, "list_replacements", side_effect=lambda service, day: [
            {"task": "Микшер", "old_name": "Глеб", "new_name": "Глеб",
             "telegram_id": 3102},
        ] if day in {"2026-10-04", "2026-11-01"} else []):
            text = self.summary()
        self.assertIn("1 — Глеб (@gleb_one) · интервалы, нед.: нет данных", text)
        self.assertIn("2 — Глеб (@gleb_two) · интервалы, нед.: 1, 1", text)
        self.assertIn("0 — Глеб (@gleb_three)", text)

    def test_summary_draft_missing_history_own_days_and_same_day_duty(self):
        self.plan({
            "2026-11-01": {"Микшер": ["A"], "Дополнительная": ["A"]},
            "2026-11-08": {"Микшер": ["A"], "Дополнительная": []},
        }, approved=False, tasks=[
            {"name": "Микшер", "needed": 1, "day": 0},
            {"name": "Дополнительная", "needed": 1, "day": 0},
        ])
        text = self.summary()
        self.assertIn("Не утверждён", text)
        self.assertIn("2 — A (без Telegram) · интервалы, нед.: нет данных, 1", text)
        self.registry.set_schedule_day(1001, "sound", "2026-11", "2026-11-08", "A", 3)
        text = self.summary()
        self.assertIn("11.11.2026", text)
        self.assertIn("нет данных, 1.4", text)

    def test_summary_uses_saved_history_when_no_prior_approved_graph(self):
        self.registry.add_service_task(1001, "sound", "Микшер")
        self.registry.set_responsible_contact(1001, "sound", "Микшер", "A", 3001)
        self.registry.save_schedule_draft(
            "sound", "2026-11", run_id=None, dates=["2026-11-01"],
            tasks=[{"name": "Микшер", "needed": 1}], slots={"2026-11-01": {"Микшер": ["A"]}},
            last_served={"A": "2026-10-04"},
        )
        self.assertIn("1 — A · интервалы, нед.: 4", self.summary())
        plan = self.registry.get_schedule_plan("sound", "2026-11")
        before_approval = plain_text(schedule_plan.plan_text(
            "ЗВУК", plan, schedule_plan.plan_summary(self.registry, plan, None),
            self.context.timezone,
        ))
        self.assertIn("1 — A · интервалы, нед.: 4", before_approval)

    def test_workload_intervals_are_chronological_unique_actual_days(self):
        people = [{"key": "A", "name": "A", "telegram_id": 1}]
        text = "\n".join(workload_lines(
            {"A": {date(2026, 11, 15), date(2026, 11, 1), date(2026, 11, 11)}},
            people, {"A": "2026-10-04"},
        ))
        self.assertIn("3 — A · интервалы, нед.: 4, 1.4, 0.6", text)

    def test_summary_button_months_refresh_permissions_and_no_writes(self):
        async def run():
            self.plan({"2026-11-15": {"Микшер": ["Ноябрь"]}})
            self.plan({"2026-12-06": {"Микшер": ["Декабрь"]}}, month="2026-12")
            with self.registry._connection() as connection:
                before = [line for line in connection.iterdump() if "nav_hints" not in line]
            main = await self.press("rdy:month:sound", 2001)
            entry = next(button.callback_data for button in self.buttons(main)
                         if button.text == "📊 Сводка")
            shown = await self.press(entry, 2001)
            self.assertIn("График › Сводка", self.text(shown))
            self.assertIn("Ноябрь", self.text(shown))
            self.assertIn("Нагрузка", self.text(shown))
            following = next(button.callback_data for button in self.buttons(shown)
                             if button.text == "📅 Следующий месяц")
            shown = await self.press(following, 2001)
            self.assertIn("Декабрь", self.text(shown))
            self.assertNotIn("Ноябрь", self.text(shown))
            refresh = next(button.callback_data for button in self.buttons(shown)
                           if button.text == "🔄 Обновить")
            refreshed = await self.press(refresh, 2001)
            self.assertIn("Декабрь", self.text(refreshed))
            self.assertIn("rdy:month:sound", [button.callback_data
                                            for button in self.buttons(refreshed)])
            denied = await self.press(entry, 9999)
            self.assertIn("Недостаточно прав", denied.answer.await_args.args[0])
            self.context.load_upcoming_service_events.assert_not_awaited()
            self.context.load_service_task_assignments.assert_not_awaited()
            self.assertFalse(any(
                call.kwargs.get("chat_id", call.args[0] if call.args else None) == -10055
                for call in self.context.bot.send_message.await_args_list
            ))
            with self.registry._connection() as connection:
                after = [line for line in connection.iterdump() if "nav_hints" not in line]
            self.assertEqual(before, after)
        asyncio.run(run())

    def test_summary_big_graph_pages_keep_navigation_and_all_workload(self):
        async def run():
            names = [f"Участник {index:03d} " + "И" * 25 for index in range(120)]
            self.plan({"2026-11-15": {"Микшер": names}})
            shown = await self.press("rdy:summary:sound")
            texts = []
            for _ in range(30):
                texts.append(self.text(shown))
                buttons = self.buttons(shown)
                self.assertLessEqual(len(buttons), 6)
                self.assertIn("rdy:month:sound", [button.callback_data for button in buttons])
                self.assertTrue(all(len(button.callback_data.encode()) <= 64 for button in buttons))
                self.assertLessEqual(visible_units(self.text(shown)), 4096)
                forward = next((button for button in buttons if button.text == "➡️ Далее"), None)
                if forward is None:
                    break
                shown = await self.press(forward.callback_data)
            self.assertGreater(len(texts), 1)
            text = "\n".join(texts)
            for name in names:
                self.assertIn(f"1 — {name}", text)
        asyncio.run(run())

    def test_summary_errors_and_empty_state_keep_graph_exit(self):
        async def run():
            empty = await self.press("rdy:summary:sound")
            self.assertIn("Графика на выбранный месяц пока нет", self.text(empty))
            self.assertIn("rdy:month:sound", [button.callback_data for button in self.buttons(empty)])
            for value, error in (
                ("202611.bad", "Некорректная страница"),
                ("202710", "только за текущий и следующий"),
                ("202611.1.2", "Некорректная страница"),
            ):
                rejected = await self.press(f"rdy:summary:sound:{value}")
                self.assertIn(error, rejected.answer.await_args.args[0])
            with patch.object(self.registry, "list_approved_schedule_plans",
                              side_effect=RuntimeError("test")):
                failed = await self.press("rdy:summary:sound")
            self.assertIn("Не удалось прочитать", self.text(failed))
            self.assertNotIn("пока нет", self.text(failed))
            self.assertIn("rdy:month:sound", [button.callback_data for button in self.buttons(failed)])
        asyncio.run(run())

    def test_slow_summary_cannot_replace_newer_graph_screen(self):
        async def run():
            started, release = asyncio.Event(), asyncio.Event()

            async def delayed(function, *args, **kwargs):
                if kwargs.get("include_load"):
                    started.set()
                    await release.wait()
                return function(*args, **kwargs)

            with patch("bot.handlers.asyncio.to_thread", side_effect=delayed):
                slow = asyncio.create_task(self.press("rdy:summary:sound"))
                await started.wait()
                main = await self.press("rdy:month:sound")
                release.set()
                stale = await slow
            stale.message.answer.assert_not_awaited()
            self.assertIn("ЗВУК › График", self.text(main))
        asyncio.run(run())

    def test_summary_current_and_next_month_cross_year_boundary(self):
        self.now = self.now.replace(month=12)
        self.plan({"2027-01-03": {"Микшер": ["Январь"]}}, month="2027-01")

        async def run():
            shown = await self.press("rdy:summary:sound:202701")
            self.assertIn("январь 2027", self.text(shown))
            self.assertIn("Январь", self.text(shown))
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
