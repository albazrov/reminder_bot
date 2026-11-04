import asyncio
import json
import sqlite3
from contextlib import closing
import logging
import tempfile
import unittest
import unittest.mock
from datetime import date, datetime, timedelta
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

from openpyxl import load_workbook

from bot import readiness, schedule_plan
from bot.presentation import task_emoji
from bot.excel_parser import (
    RosterMember,
    ServiceAssignment,
    ServiceEvent,
    parse_service_events,
)
from bot.service_registry import ServiceRegistry

TZ = ZoneInfo("Europe/Moscow")
LOG = logging.getLogger("test-schedule-plan")
LOG.addHandler(logging.NullHandler())
LOG.propagate = False


def fake_bot():
    return SimpleNamespace(
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=11)),
        send_poll=AsyncMock(return_value=SimpleNamespace(
            message_id=12, poll=SimpleNamespace(id="poll-1"),
        )),
        stop_poll=AsyncMock(),
        edit_message_text=AsyncMock(),
        pin_chat_message=AsyncMock(),
        unpin_chat_message=AsyncMock(),
        delete_message=AsyncMock(),
    )


def event(day: date, *pairs: tuple[str, str]) -> ServiceEvent:
    return ServiceEvent(
        "sound", day,
        tuple(ServiceAssignment(task, name) for task, name in pairs),
    )


class SchedulePlanTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.registry = ServiceRegistry(
            Path(self.temp_dir.name) / "registry.sqlite3"
        )
        self.registry.initialize(1001, -10001)
        self.registry.set_group_chat(1001, "sound", -1005555, enabled=True)
        self.registry.sync_service_rosters({"sound": [
            RosterMember("Алексей", ("звук", "сборка")),
            RosterMember("Борис", ("звук",)),
            RosterMember("Дмитрий", ("сборка",)),
        ]})
        self.registry.set_responsible_contact(
            1001, "sound", "звук", "Алексей", 501, "alex"
        )
        self.registry.set_responsible_contact(
            1001, "sound", "звук", "Борис", 502
        )
        self.registry.set_responsible_contact(
            1001, "sound", "сборка", "Глеб", 503
        )
        self.bot = fake_bot()
        self.run = asyncio.run(readiness.launch_run(
            self.bot, self.registry, self.service(), 2026, 11,
            datetime(2026, 10, 22, 20, 0, tzinfo=TZ), TZ, LOG, 4096,
        ))
        record = self.registry.record_readiness_answer
        record("poll-1", 501, "alex", "Алексей", ["2026-11-01", "2026-11-08"])
        record("poll-1", 502, "", "Борис", ["2026-11-01", "2026-11-15"])
        record("poll-1", 503, "", "Глеб", ["2026-11-01", "2026-11-08"])
        self.registry.close_readiness_run(self.run["id"])
        self.run = self.registry.get_readiness_run_by_id(self.run["id"])

    def service(self):
        return self.registry.get_service("sound")

    def people(self):
        return self.registry.readiness_people("sound")

    def draft(self, loader=None):
        return asyncio.run(
            schedule_plan.build_draft(self.registry, self.run, loader, LOG)
        )

    def test_last_served_from_excel_and_approved_plan(self):
        loader = AsyncMock(return_value=[
            event(date(2026, 10, 18), ("звук", "Алексей")),
            event(date(2026, 10, 25), ("звук", "алексей"), ("сборка", "Глеб")),
            event(date(2026, 11, 1), ("звук", "Борис")),
        ])
        served, note = asyncio.run(schedule_plan.last_served(
            self.registry, "sound", 2026, 11, self.people(), loader, LOG,
        ))
        self.assertIsNone(note)
        self.assertEqual(served, {"Алексей": "2026-10-25", "Глеб": "2026-10-25"})
        # Утверждённый график прошлого месяца главнее Excel.
        self.registry.save_schedule_draft(
            "sound", "2026-10", run_id=None, dates=["2026-10-18"],
            tasks=[], slots={"2026-10-18": {"звук": ["Борис"]}}, last_served={},
        )
        self.registry.approve_schedule_plan(
            1001, "sound", "2026-10", {"2026-10-18": {"звук": ["Борис"]}},
            chat_id=None, message_id=None,
        )
        served, _ = asyncio.run(schedule_plan.last_served(
            self.registry, "sound", 2026, 11, self.people(), loader, LOG,
        ))
        self.assertEqual(served, {"Борис": "2026-10-18"})
        # Служил не в воскресенье (разовый день пт) — интервал от пятницы.
        self.registry.set_schedule_day(
            1001, "sound", "2026-10", "2026-10-18", "Борис", -2
        )
        plan = self.registry.get_schedule_plan("sound", "2026-10")
        self.registry.approve_schedule_plan(
            1001, "sound", "2026-10", plan["slots"], chat_id=None,
            message_id=None, days=plan["days"],
            served_offsets=schedule_plan.served_offsets(plan, self.people()),
        )
        # Утверждён до снимка (snapshot NULL) — разовый день из approved_days.
        with closing(sqlite3.connect(self.registry.path)) as connection:
            connection.execute(
                "UPDATE schedule_plans SET served_offsets_json = NULL "
                "WHERE month = '2026-10'"
            )
            connection.commit()
        legacy, _ = asyncio.run(schedule_plan.last_served(
            self.registry, "sound", 2026, 11, self.people(), loader, LOG,
        ))
        self.assertEqual(legacy, {"Борис": "2026-10-16"})
        self.registry.approve_schedule_plan(
            1001, "sound", "2026-10", plan["slots"], chat_id=None,
            message_id=None, days=plan["days"],
            served_offsets=schedule_plan.served_offsets(plan, self.people()),
        )
        # Личный день меняют потом — прошлое служение остаётся в пятницу.
        self.registry.set_service_member_day(1001, "sound", "Борис", -5)
        served, _ = asyncio.run(schedule_plan.last_served(
            self.registry, "sound", 2026, 11, self.people(), loader, LOG,
        ))
        self.assertEqual(served, {"Борис": "2026-10-16"})

    def test_last_served_failure_is_noted(self):
        loader = AsyncMock(side_effect=OSError("disk"))
        served, note = asyncio.run(schedule_plan.last_served(
            self.registry, "sound", 2026, 11, self.people(), loader, LOG,
        ))
        self.assertEqual(served, {})
        self.assertIn("не учтён", note)

    def test_previous_month_pushes_person_later(self):
        # Алексей служил 25.10 — 01.11 ставится в последнюю очередь.
        loader = AsyncMock(return_value=[
            event(date(2026, 10, 25), ("звук", "Алексей")),
        ])
        plan = self.draft(loader)
        self.assertEqual(plan["slots"]["2026-11-01"]["звук"], ["Борис"])
        self.assertEqual(plan["last_served"], {"Алексей": "2026-10-25"})

    def test_draft_edit_approve_and_recalculate(self):
        plan = self.draft()
        self.assertEqual(plan["run_id"], self.run["id"])
        self.assertIsNone(plan["approved"])
        self.assertTrue(plan["dirty"])
        self.assertEqual(
            [task["name"] for task in plan["tasks"]], ["звук", "сборка"]
        )
        # ensure_plan не пересчитывает сохранённый график того же опроса.
        slots = plan["slots"]
        slots["2026-11-15"]["сборка"] = ["Дмитрий"]
        edited = self.registry.set_schedule_slots(1001, "sound", "2026-11", slots)
        self.assertEqual(edited["edited"], 1)
        again = asyncio.run(
            schedule_plan.ensure_plan(self.registry, self.run, None, LOG)
        )
        self.assertEqual(again["slots"]["2026-11-15"]["сборка"], ["Дмитрий"])
        with self.assertRaises(PermissionError):
            self.registry.set_schedule_slots(9999, "sound", "2026-11", slots)

        approved = asyncio.run(schedule_plan.publish(
            self.bot, self.registry, self.service(), again, 1001, LOG, 4096,
        ))
        self.assertFalse(approved["dirty"])
        self.assertEqual(approved["message_id"], 11)
        chat, text = self.bot.send_message.await_args.args[:2]
        self.assertEqual(chat, -1005555)
        self.assertIn("График Звук на ноябрь 2026 утверждён", text)
        self.assertIn('href="tg://user?id=501"', text)

        # Правка после утверждения — закреплённый график правится,
        # под ним список изменений.
        slots = approved["slots"]
        slots["2026-11-15"]["сборка"] = ["Глеб"]
        changed = self.registry.set_schedule_slots(1001, "sound", "2026-11", slots)
        self.assertTrue(changed["dirty"])
        asyncio.run(schedule_plan.publish(
            self.bot, self.registry, self.service(), changed, 1001, LOG, 4096,
        ))
        text = self.bot.edit_message_text.await_args.args[0]
        self.assertIn("обновлён", text)
        self.assertIn("15.11 «сборка»: Дмитрий → ", text)

        # Пересчёт сбрасывает правки, утверждённая копия остаётся.
        recalculated = self.draft()
        self.assertEqual(recalculated["edited"], 0)
        self.assertEqual(
            recalculated["approved"]["2026-11-15"]["сборка"], ["Глеб"]
        )

    def test_publication_pinned_edited_and_answered(self):
        """Решение 17, 5б: первая публикация закрепляется, обновление
        правит её (под графиком — все изменения месяца) и отвечает на неё
        только новыми изменениями; не правится — новая и закреплённая."""
        from aiogram.exceptions import TelegramBadRequest

        plan = self.draft()
        plan["slots"]["2026-11-01"] = {"звук": ["Алексей"], "сборка": []}
        plan["slots"]["2026-11-08"] = {"звук": ["Борис"], "сборка": []}
        plan = self.registry.set_schedule_slots(
            1001, "sound", "2026-11", plan["slots"]
        )
        first = asyncio.run(schedule_plan.publish(
            self.bot, self.registry, self.service(), plan, 1001, LOG, 4096,
        ))
        self.assertTrue(first["pinned"])
        self.bot.pin_chat_message.assert_awaited_once_with(
            -1005555, 11, disable_notification=True,
        )
        self.assertEqual(first["message_ids"], [11])
        self.assertEqual(
            first["baseline"]["slots"]["2026-11-01"]["звук"], ["Алексей"]
        )
        text = self.bot.send_message.await_args.args[1]
        # Каждый человек — на своей строке.
        self.assertIn("🎚️ звук\n    • ", text)
        self.assertIn("🧰 сборка: —", text)

        # Первое обновление: 01.11 Борис вместо Алексея.
        self.bot.send_message.reset_mock()
        slots = first["slots"]
        slots["2026-11-01"]["звук"] = ["Борис"]
        changed = self.registry.set_schedule_slots(
            1001, "sound", "2026-11", slots
        )
        second = asyncio.run(schedule_plan.publish(
            self.bot, self.registry, self.service(), changed, 1001, LOG, 4096,
        ))
        edit = self.bot.edit_message_text.await_args
        self.assertEqual(
            (edit.kwargs["chat_id"], edit.kwargs["message_id"]),
            (-1005555, 11),
        )
        self.assertIn("Изменения:\n• 01.11 «звук»: Алексей → Борис",
                      edit.args[0])
        # Ответ на закреплённый — с упоминанием, правка не уведомляет.
        reply = self.bot.send_message.await_args
        self.assertEqual(reply.kwargs["reply_parameters"].message_id, 11)
        self.assertIn("01.11 «звук»: ", reply.args[1])
        self.assertIn('href="tg://user?id=502"', reply.args[1])
        # Закрепляется и при правке (вдруг права дали позже).
        self.assertEqual(self.bot.pin_chat_message.await_count, 2)
        self.assertEqual(second["message_ids"], [11])

        # Второе обновление: в закреплённом — оба изменения, в ответе —
        # только новое.
        self.bot.send_message.reset_mock()
        slots = second["slots"]
        slots["2026-11-08"]["звук"] = ["Алексей"]
        changed = self.registry.set_schedule_slots(
            1001, "sound", "2026-11", slots
        )
        third = asyncio.run(schedule_plan.publish(
            self.bot, self.registry, self.service(), changed, 1001, LOG, 4096,
        ))
        pinned_text = self.bot.edit_message_text.await_args.args[0]
        self.assertIn("• 01.11 «звук»: Алексей → Борис", pinned_text)
        self.assertIn("• 08.11 «звук»: Борис → Алексей", pinned_text)
        reply_text = self.bot.send_message.await_args.args[1]
        self.assertIn("08.11", reply_text)
        self.assertNotIn("01.11", reply_text)

        # Закреплённое удалено — новая публикация, закреплённая; прежняя
        # откреплена; изменения — всё ещё от первой публикации.
        self.bot.send_message.reset_mock()
        self.bot.send_message.return_value = SimpleNamespace(message_id=21)
        self.bot.edit_message_text.side_effect = TelegramBadRequest(
            method=unittest.mock.MagicMock(),
            message="Bad Request: message to edit not found",
        )
        slots = third["slots"]
        slots["2026-11-22"]["звук"] = ["Борис"]
        changed = self.registry.set_schedule_slots(
            1001, "sound", "2026-11", slots
        )
        fourth = asyncio.run(schedule_plan.publish(
            self.bot, self.registry, self.service(), changed, 1001, LOG, 4096,
        ))
        self.assertEqual(fourth["message_ids"], [21])
        self.bot.pin_chat_message.assert_awaited_with(
            -1005555, 21, disable_notification=True,
        )
        # Прежняя публикация удалена; не удаляется — откреплена.
        self.bot.delete_message.assert_awaited_with(-1005555, 11)
        self.bot.unpin_chat_message.assert_not_awaited()
        # Новое сообщение само уведомляет — ответа нет, оно одно.
        self.assertEqual(self.bot.send_message.await_count, 1)
        text = self.bot.send_message.await_args.args[1]
        self.assertIn("• 01.11 «звук»: Алексей → Борис", text)
        self.assertIn("• 22.11 «звук»: никого → Борис", text)

        # Закрепить нельзя (нет права) — график всё равно утверждён.
        self.bot.pin_chat_message.side_effect = RuntimeError("no rights")
        # Публикации нет — следующая уходит новой.
        with closing(sqlite3.connect(self.registry.path)) as connection:
            connection.execute(
                "UPDATE schedule_plans SET message_ids_json = NULL, "
                "message_id = NULL WHERE month = '2026-11'"
            )
            connection.commit()
        slots = fourth["slots"]
        slots["2026-11-29"]["звук"] = ["Алексей"]
        changed = self.registry.set_schedule_slots(
            1001, "sound", "2026-11", slots
        )
        fifth = asyncio.run(schedule_plan.publish(
            self.bot, self.registry, self.service(), changed, 1001, LOG, 4096,
        ))
        self.assertFalse(fifth["pinned"])
        self.assertFalse(fifth["dirty"])

    def test_failed_save_keeps_previous_publication(self):
        """Утверждение не сохранилось — прежняя публикация не удаляется:
        в базе ещё её id."""
        from aiogram.exceptions import TelegramBadRequest

        plan = self.draft()
        first = asyncio.run(schedule_plan.publish(
            self.bot, self.registry, self.service(), plan, 1001, LOG, 4096,
        ))
        slots = first["slots"]
        slots["2026-11-29"]["звук"] = ["Борис"]
        changed = self.registry.set_schedule_slots(
            1001, "sound", "2026-11", slots
        )
        self.bot.edit_message_text.side_effect = TelegramBadRequest(
            method=unittest.mock.MagicMock(),
            message="Bad Request: message to edit not found",
        )
        self.bot.send_message.return_value = SimpleNamespace(message_id=21)
        with unittest.mock.patch.object(
            self.registry, "approve_schedule_plan",
            side_effect=sqlite3.OperationalError("database is locked"),
        ):
            with self.assertRaises(schedule_plan.PublishedNotSaved):
                asyncio.run(schedule_plan.publish(
                    self.bot, self.registry, self.service(), changed, 1001,
                    LOG, 4096,
                ))
        self.bot.delete_message.assert_not_awaited()
        self.assertEqual(
            self.registry.get_schedule_plan("sound", "2026-11")["message_ids"],
            [11],
        )

    @unittest.mock.patch(
        "bot.service_registry._current_month", return_value="2026-10"
    )
    def test_days_change_stays_in_pinned_and_unsent_reply_reported(self, _m):
        plan = self.draft()
        first = asyncio.run(schedule_plan.publish(
            self.bot, self.registry, self.service(), plan, 1001, LOG, 4096,
        ))
        # Сменились дни служения — строка в закреплённом.
        self.registry.set_readiness_settings(1001, "sound", day_from=-1, day_to=1)
        changed = self.registry.get_schedule_plan("sound", "2026-11")
        self.assertTrue(changed["republish"])
        asyncio.run(schedule_plan.publish(
            self.bot, self.registry, self.service(), changed, 1001, LOG, 4096,
        ))
        # Следующая правка людей — строка о днях не пропадает, хотя
        # republish уже сброшен; ответ не ушёл — текст админу.
        slots = self.registry.get_schedule_plan("sound", "2026-11")["slots"]
        slots["2026-11-29"]["звук"] = ["Борис"]
        changed = self.registry.set_schedule_slots(
            1001, "sound", "2026-11", slots
        )
        self.assertFalse(changed["republish"])
        self.bot.send_message.side_effect = RuntimeError("network")
        third = asyncio.run(schedule_plan.publish(
            self.bot, self.registry, self.service(), changed, 1001, LOG, 4096,
        ))
        pinned_text = self.bot.edit_message_text.await_args.args[0]
        self.assertIn("• Изменены дни служения", pinned_text)
        self.assertIn("29.11 «звук»: никого → Борис", pinned_text)
        self.assertIn("29.11 «звук»: никого → Борис", third["unsent_notice"])
        self.assertNotIn("\x00", third["unsent_notice"])
        self.assertNotIn("Изменены дни", third["unsent_notice"])
        self.assertEqual(first["message_ids"], third["message_ids"])

    def test_new_poll_cancels_approval_and_preview_is_not_saved(self):
        """Решение 28.10: запуск опроса отменяет утверждённый график —
        прежний откреплён; пока опрос идёт — предварительный график по
        текущим ответам без записи в базу; новое утверждение — новая
        публикация, прежняя удаляется."""
        plan = self.draft()
        approved = asyncio.run(schedule_plan.publish(
            self.bot, self.registry, self.service(), plan, 1001, LOG, 4096,
        ))
        self.assertIsNotNone(approved["approved"])
        # «Запустить заново»: прежний опрос откладывается, новый — в чат.
        self.registry.move_readiness_run(self.run["id"], f"2026-11#{self.run['id']}")
        self.bot.send_poll.return_value = SimpleNamespace(
            message_id=13, poll=SimpleNamespace(id="poll-2"),
        )
        run = asyncio.run(readiness.launch_run(
            self.bot, self.registry, self.service(), 2026, 11,
            datetime(2026, 10, 30, 20, 0, tzinfo=TZ), TZ, LOG, 4096,
        ))
        cancelled = self.registry.get_schedule_plan("sound", "2026-11")
        self.assertIsNone(cancelled["approved"])
        self.assertIsNone(cancelled["baseline"])
        self.assertEqual(cancelled["message_ids"], [11])
        self.bot.unpin_chat_message.assert_awaited_once_with(
            -1005555, message_id=11,
        )
        # Напоминания больше не по отменённому графику.
        self.assertEqual(schedule_plan.plan_events(cancelled, self.people()), [])
        # Предварительный: по текущим ответам (их нет — все не ответили),
        # в базе ничего не меняется.
        before = self.registry.get_schedule_plan("sound", "2026-11")
        preview = asyncio.run(schedule_plan.build_draft(
            self.registry, run, None, LOG, save=False,
        ))
        self.assertEqual(preview["run_id"], run["id"])
        self.assertIsNone(preview["approved"])
        self.assertEqual(
            self.registry.get_schedule_plan("sound", "2026-11"), before
        )
        summary = schedule_plan.plan_summary(self.registry, preview, run)
        self.assertIn(
            "📝 Черновик", schedule_plan.plan_text("ЗВУК", preview, summary, TZ)
        )
        # Опрос завершён — новый черновик, утверждение — новая публикация
        # и удаление прежней.
        self.registry.close_readiness_run(run["id"])
        run = self.registry.get_readiness_run_by_id(run["id"])
        fresh = asyncio.run(
            schedule_plan.ensure_plan(self.registry, run, None, LOG)
        )
        self.assertEqual(fresh["run_id"], run["id"])
        self.bot.send_message.reset_mock()
        self.bot.send_message.return_value = SimpleNamespace(message_id=31)
        published = asyncio.run(schedule_plan.publish(
            self.bot, self.registry, self.service(), fresh, 1001, LOG, 4096,
        ))
        self.assertEqual(published["message_ids"], [31])
        self.assertIn("утверждён", self.bot.send_message.await_args.args[1])
        self.bot.edit_message_text.assert_not_awaited()
        self.bot.delete_message.assert_awaited_with(-1005555, 11)

    def test_new_poll_cancels_excel_plan(self):
        """Загруженный из Excel график после запуска опроса — не итоговый:
        утвердить его, пока опрос идёт, нельзя."""
        events = [event(date(2026, 11, 8), ("звук", "Алексей"))]
        loaded = schedule_plan.excel_plan(self.registry, "sound", 2026, 11, events)
        self.registry.import_excel_plan(
            1001, "sound", "2026-11", dates=loaded["dates"],
            tasks=loaded["tasks"], slots=loaded["slots"], days=loaded["days"],
        )
        self.assertEqual(
            self.registry.get_schedule_plan("sound", "2026-11")["source"], "excel"
        )
        self.registry.move_readiness_run(self.run["id"], f"2026-11#{self.run['id']}")
        self.bot.send_poll.return_value = SimpleNamespace(
            message_id=13, poll=SimpleNamespace(id="poll-3"),
        )
        asyncio.run(readiness.launch_run(
            self.bot, self.registry, self.service(), 2026, 11,
            datetime(2026, 10, 30, 20, 0, tzinfo=TZ), TZ, LOG, 4096,
        ))
        plan = self.registry.get_schedule_plan("sound", "2026-11")
        self.assertIsNone(plan["approved"])
        self.assertEqual(plan["source"], "poll")
        self.assertIsNone(plan["run_id"])

    def test_next_month_poll_keeps_current_month_approved(self):
        """Опрос следующего месяца не отменяет утверждённый график текущего:
        по нему идут напоминания, пока его месяц не закончился."""
        plan = self.draft()
        asyncio.run(schedule_plan.publish(
            self.bot, self.registry, self.service(), plan, 1001, LOG, 4096,
        ))
        self.bot.send_poll.return_value = SimpleNamespace(
            message_id=14, poll=SimpleNamespace(id="poll-dec"),
        )
        asyncio.run(readiness.launch_run(
            self.bot, self.registry, self.service(), 2026, 12,
            datetime(2026, 11, 26, 20, 0, tzinfo=TZ), TZ, LOG, 4096,
        ))
        november = self.registry.get_schedule_plan("sound", "2026-11")
        self.assertIsNotNone(november["approved"])
        self.assertNotEqual(schedule_plan.plan_events(november, self.people()), [])
        self.bot.unpin_chat_message.assert_not_awaited()

    def test_topic_change_republishes_in_new_topic(self):
        """Ревью greptile к PR #88: тему чата сменили (/here) — график не
        правится в старой теме, а публикуется в новой; старый удаляется."""
        plan = self.draft()
        first = asyncio.run(schedule_plan.publish(
            self.bot, self.registry, self.service(), plan, 1001, LOG, 4096,
        ))
        self.assertIsNone(first["thread_id"])
        self.registry.set_group_thread(1001, "sound", -1005555, 77)
        slots = first["slots"]
        slots["2026-11-29"]["звук"] = ["Борис"]
        changed = self.registry.set_schedule_slots(
            1001, "sound", "2026-11", slots
        )
        # Смена темы — график «с правками», даже без правок людей.
        self.assertTrue(
            self.registry.get_schedule_plan("sound", "2026-11")["dirty"]
        )
        self.bot.send_message.return_value = SimpleNamespace(
            message_id=41, message_thread_id=77, is_topic_message=True,
        )
        moved = asyncio.run(schedule_plan.publish(
            self.bot, self.registry, self.service(), changed, 1001, LOG, 4096,
        ))
        self.bot.edit_message_text.assert_not_awaited()
        self.assertEqual(
            self.bot.send_message.await_args.kwargs["message_thread_id"], 77
        )
        self.bot.delete_message.assert_awaited_with(-1005555, 11)
        self.assertEqual(
            (moved["message_ids"], moved["thread_id"]), ([41], 77)
        )
        self.assertFalse(moved["dirty"])
        # Дальше — правка в новой теме.
        slots = moved["slots"]
        slots["2026-11-22"]["звук"] = ["Алексей"]
        changed = self.registry.set_schedule_slots(
            1001, "sound", "2026-11", slots
        )
        asyncio.run(schedule_plan.publish(
            self.bot, self.registry, self.service(), changed, 1001, LOG, 4096,
        ))
        self.assertEqual(
            self.bot.edit_message_text.await_args.kwargs["message_id"], 41
        )

    def test_closed_topic_saves_actual_topic(self):
        """Тема закрыта — график ушёл в общую: записана фактическая тема
        (по ответу Telegram) и выбранная; дальше — правка, а не новая
        публикация каждый раз; ответ — туда, где лежит график."""
        self.registry.set_group_thread(1001, "sound", -1005555, 88)
        # Ответ Telegram без темы — сообщение в общей теме.
        self.bot.send_message.return_value = SimpleNamespace(message_id=51)
        plan = self.draft()
        published = asyncio.run(schedule_plan.publish(
            self.bot, self.registry, self.service(), plan, 1001, LOG, 4096,
        ))
        self.assertEqual(
            (published["thread_id"], published["target_thread_id"]), (None, 88)
        )
        self.assertFalse(published["moved"])
        self.assertFalse(published["dirty"])
        slots = published["slots"]
        slots["2026-11-29"]["звук"] = ["Борис"]
        changed = self.registry.set_schedule_slots(
            1001, "sound", "2026-11", slots
        )
        self.bot.send_message.reset_mock()
        asyncio.run(schedule_plan.publish(
            self.bot, self.registry, self.service(), changed, 1001, LOG, 4096,
        ))
        self.assertEqual(
            self.bot.edit_message_text.await_args.kwargs["message_id"], 51
        )
        reply = self.bot.send_message.await_args
        self.assertNotIn("message_thread_id", reply.kwargs)
        # Выбрали другую тему — график «не на месте», статус на экране.
        self.registry.set_group_thread(1001, "sound", -1005555, 99)
        moved = self.registry.get_schedule_plan("sound", "2026-11")
        self.assertTrue(moved["moved"])
        summary = schedule_plan.plan_summary(self.registry, moved, self.run)
        self.assertIn(
            "📍 Сменился чат или тема служения",
            schedule_plan.plan_text("ЗВУК", moved, summary, TZ),
        )

    def test_deleted_topic_does_not_stay_moved(self):
        """Тема удалена: отправка уходит в общую и сбрасывает тему
        служения — график «на месте», дальше правка, не новая публикация."""
        from aiogram.exceptions import TelegramBadRequest

        self.registry.set_group_thread(1001, "sound", -1005555, 66)

        async def send(chat_id, text, **kwargs):
            if kwargs.get("message_thread_id"):
                raise TelegramBadRequest(
                    method=unittest.mock.MagicMock(),
                    message="Bad Request: message thread not found",
                )
            return SimpleNamespace(message_id=61)

        self.bot.send_message.side_effect = send
        published = asyncio.run(schedule_plan.publish(
            self.bot, self.registry, self.service(), self.draft(), 1001, LOG,
            4096,
        ))
        self.assertIsNone(self.service()["group_thread_id"])
        self.assertEqual(
            (published["thread_id"], published["target_thread_id"]),
            (None, None),
        )
        self.assertFalse(published["moved"])
        # Запись без выбранной темы (до этой правки) — по фактической.
        self.assertEqual(
            schedule_plan.plan_target_thread({"thread_id": 5}), 5
        )

    def test_topic_changed_during_publication(self):
        """/here во время публикации: записана тема отправки, график
        «не на месте» — следующее утверждение опубликует его в новой."""
        self.registry.set_group_thread(1001, "sound", -1005555, 70)
        service = self.service()

        async def send(chat_id, text, **kwargs):
            # Пока сообщение уходит, админ выбрал другую тему.
            self.registry.set_group_thread(1001, "sound", -1005555, 71)
            return SimpleNamespace(
                message_id=81, message_thread_id=70, is_topic_message=True,
            )

        self.bot.send_message.side_effect = send
        published = asyncio.run(schedule_plan.publish(
            self.bot, self.registry, service, self.draft(), 1001, LOG, 4096,
        ))
        self.assertEqual(
            (published["thread_id"], published["target_thread_id"]), (70, 70)
        )
        self.assertTrue(published["moved"])

    def test_change_lines_format(self):
        names = {"f": "Филипп", "d": "Денис", "a": "Анна"}
        self.assertEqual(
            schedule_plan.plan_changes(
                {"2026-11-08": {"Уборка": ["d"]}},
                {"2026-11-08": {"Уборка": ["f"]}}, names,
            ),
            ["• 08.11 Денис → Филипп"],
        )
        self.assertEqual(
            schedule_plan.plan_changes(
                {"2026-11-08": {"звук": ["d"], "сборка": ["a"]}},
                {"2026-11-08": {"звук": ["d", "f"], "сборка": []}}, names,
            ),
            ["• 08.11 «звук»: никого → Филипп", "• 08.11 «сборка»: Анна → никого"],
        )

    def test_excel_plan_round_trip_with_task_day(self):
        self.registry.set_service_task_day(1001, "sound", "сборка", -4)
        events = [
            event(date(2026, 11, 8), ("звук", "Алексей")),
            event(date(2026, 11, 4), ("сборка", "Дмитрий")),
            event(date(2026, 11, 15), ("сборка", "Незнакомец")),
        ]
        loaded = schedule_plan.excel_plan(self.registry, "sound", 2026, 11, events)
        self.assertEqual(loaded["slots"]["2026-11-08"]["звук"], ["Алексей"])
        # Среда — в своё воскресенье; в воскресенье — разовый день.
        self.assertEqual(loaded["slots"]["2026-11-08"]["сборка"], ["Дмитрий"])
        self.assertEqual(loaded["slots"]["2026-11-15"]["сборка"], ["Незнакомец"])
        self.assertEqual(loaded["days"], {
            "2026-11-08": {
                schedule_plan.task_day_key("Алексей", "звук"): 0,
                schedule_plan.task_day_key("Дмитрий", "сборка"): -4,
            },
            "2026-11-15": {schedule_plan.task_day_key("Незнакомец", "сборка"): 0}
        })
        self.assertIn("Незнакомец", loaded["unbound"])
        plan = self.registry.import_excel_plan(
            1001, "sound", "2026-11", dates=loaded["dates"],
            tasks=loaded["tasks"], slots=loaded["slots"], days=loaded["days"],
        )
        self.assertEqual(plan["source"], "excel")
        self.assertFalse(plan["dirty"])
        # Обратно — события для напоминаний: среда — своей датой.
        back = {
            item.event_date: sorted((a.task, a.name) for a in item.assignments)
            for item in schedule_plan.plan_events(plan, self.people())
        }
        self.assertEqual(back[date(2026, 11, 4)], [("сборка", "Дмитрий")])
        self.assertEqual(back[date(2026, 11, 8)], [("звук", "Алексей")])
        self.assertEqual(back[date(2026, 11, 15)], [("сборка", "Незнакомец")])
        self.registry.set_service_task_day(1001, "sound", "сборка", None)
        plan = self.registry.get_schedule_plan("sound", "2026-11")
        back = {
            item.event_date: sorted((a.task, a.name) for a in item.assignments)
            for item in schedule_plan.plan_events(plan, self.people())
        }
        self.assertEqual(back[date(2026, 11, 4)], [("сборка", "Дмитрий")])
        self.assertEqual(back[date(2026, 11, 8)], [("звук", "Алексей")])
        self.registry.set_service_task_day(1001, "sound", "сборка", -4)
        # Разные дни у людей одной задачи — у каждого свой, как в файле.
        mixed = schedule_plan.excel_plan(self.registry, "sound", 2026, 11, [
            event(date(2026, 11, 18), ("сборка", "Дмитрий")),
            event(date(2026, 11, 22), ("сборка", "Алексей")),
        ])
        self.assertEqual(
            mixed["slots"]["2026-11-22"]["сборка"], ["Дмитрий", "Алексей"]
        )
        self.assertEqual(mixed["days"], {
            "2026-11-22": {
                schedule_plan.task_day_key("Алексей", "сборка"): 0,
                schedule_plan.task_day_key("Дмитрий", "сборка"): -4,
            }
        })
        # Новый опрос пересчитывает рабочую копию, итоговым остаётся Excel,
        # пока не утвердят.
        draft = self.draft()
        self.assertEqual(draft["source"], "poll")
        self.assertEqual(draft["approved"], plan["approved"])
        self.assertTrue(draft["dirty"])
        # Разовый день одной задачи не сдвигает другие задачи человека той
        # же недели: сборка в воскресенье, звук (вс −4) — в среду.
        self.registry.set_service_task_day(1001, "sound", "звук", -4)
        both = schedule_plan.excel_plan(self.registry, "sound", 2026, 11, [
            event(date(2026, 11, 22), ("сборка", "Алексей")),
            event(date(2026, 11, 18), ("звук", "Алексей")),
        ])
        imported = self.registry.import_excel_plan(
            1001, "sound", "2026-11", dates=both["dates"],
            tasks=both["tasks"], slots=both["slots"], days=both["days"],
        )
        back = {
            item.event_date: sorted((a.task, a.name) for a in item.assignments)
            for item in schedule_plan.plan_events(imported, self.people())
        }
        self.assertEqual(back[date(2026, 11, 18)], [("звук", "Алексей")])
        self.assertEqual(back[date(2026, 11, 22)], [("сборка", "Алексей")])
        # Правка графика день задачи сохраняет; день на дату его заменяет.
        key = schedule_plan.task_day_key("Алексей", "сборка")
        edited = self.registry.set_schedule_slots(
            1001, "sound", "2026-11", imported["slots"],
        )
        self.assertEqual(edited["days"]["2026-11-22"], both["days"]["2026-11-22"])
        slots = imported["slots"]
        slots["2026-11-22"]["сборка"] = []
        edited = self.registry.set_schedule_slots(1001, "sound", "2026-11", slots)
        self.assertEqual(edited["days"]["2026-11-22"], {
            schedule_plan.task_day_key("Алексей", "звук"): -4,
        })
        slots["2026-11-22"]["сборка"] = ["Алексей"]
        self.registry.set_schedule_slots(1001, "sound", "2026-11", slots)
        with closing(sqlite3.connect(self.registry.path)) as connection, \
                connection:
            connection.execute(
                "UPDATE schedule_plans SET days_json = ? WHERE month = ?",
                (json.dumps({"2026-11-22": {key: 0}}), "2026-11"),
            )
        edited = self.registry.set_schedule_day(
            1001, "sound", "2026-11", "2026-11-22", "Алексей", -1,
        )
        self.assertEqual(edited["days"]["2026-11-22"], {"Алексей": -1})
        # Снят со сборки, но остался в звуке — день сборки уходит.
        slots["2026-11-22"]["звук"] = ["Алексей"]
        self.registry.set_schedule_slots(1001, "sound", "2026-11", slots)
        with closing(sqlite3.connect(self.registry.path)) as connection, \
                connection:
            connection.execute(
                "UPDATE schedule_plans SET days_json = ? WHERE month = ?",
                (json.dumps({"2026-11-22": {key: 0}}), "2026-11"),
            )
        self.registry.set_service_member_tasks(1001, "sound", "Алексей", ["звук"])
        plan = self.registry.get_schedule_plan("sound", "2026-11")
        self.assertEqual(plan["slots"]["2026-11-22"]["звук"], ["Алексей"])
        self.assertNotIn("2026-11-22", plan["days"])
        self.registry.set_service_task_day(1001, "sound", "звук", None)

    def test_week_bundles_from_task_flag_and_member(self):
        # Алексей — звук и сборка; связка у служения: звук + сборка.
        with self.assertRaises(ValueError):
            self.registry.set_service_task_same_week(1001, "sound", "нет", True)
        self.registry.set_service_task_same_week(1001, "sound", "звук", True)
        self.registry.set_service_task_same_week(1001, "sound", "сборка", True)
        flags = {
            task["name"]: task["same_week"]
            for task in self.registry.list_service_tasks("sound")
        }
        self.assertEqual(flags, {"звук": True, "сборка": True, "спевка": False})
        plan = self.draft()
        # Связка не разрешает две задачи в один фактический день.
        for day in plan["dates"]:
            assigned = [
                person for people in plan["slots"][day].values()
                for person in people
            ]
            self.assertEqual(len(assigned), len(set(assigned)))
        # В разные дни одной недели связка собирается: звук в воскресенье,
        # сборка в среду перед ним.
        self.registry.set_service_task_day(1001, "sound", "сборка", -4)
        plan = self.draft()
        self.assertEqual(
            (plan["slots"]["2026-11-01"]["звук"],
             plan["slots"]["2026-11-01"]["сборка"]),
            (["Алексей"], ["Алексей"]),
        )
        self.assertEqual(
            schedule_plan.slot_offset(
                plan, None, "2026-11-01", "сборка"
            ),
            -4,
        )
        self.registry.set_service_task_day(1001, "sound", "сборка", None)
        plan = self.draft()
        # Правка графика: убранного со второй задачи связки можно вернуть
        # — задача из его связки не «занятость».
        edited = {**plan, "slots": {
            **plan["slots"],
            "2026-11-01": {"звук": ["Алексей"], "сборка": [], "спевка": []},
        }}
        summary = readiness.summarize(self.registry, self.run, with_plan=False)
        self.assertEqual(
            summary.bundles, {"Алексей": frozenset({"звук", "сборка"})}
        )
        self.assertEqual(
            schedule_plan.busy_on(edited, "2026-11-01", "сборка", summary.bundles),
            {},
        )
        self.assertEqual(
            schedule_plan.busy_on(edited, "2026-11-01", "сборка"),
            {"Алексей": "звук"},
        )
        self.assertIn(
            "Алексей",
            sum(schedule_plan.candidates(edited, summary, "2026-11-01", "сборка"), []),
        )
        # Личная настройка главнее: «не связывать».
        with self.assertRaises(ValueError):
            self.registry.set_service_member_week_tasks(
                1001, "sound", "Алексей", ["звук"]
            )
        self.registry.set_service_member_week_tasks(1001, "sound", "Алексей", [])
        alex_person = next(p for p in self.people() if p["name"] == "Алексей")
        self.assertEqual(alex_person["week_tasks"], [])
        plan = self.draft()
        for day in plan["dates"]:
            self.assertFalse(
                "Алексей" in plan["slots"][day]["звук"]
                and "Алексей" in plan["slots"][day]["сборка"]
            )
        self.registry.set_service_member_week_tasks(1001, "sound", "Алексей", None)
        self.assertIsNone(
            next(p for p in self.people() if p["name"] == "Алексей")["week_tasks"]
        )
        # Нагрузка: связка в дату — одно воскресенье.
        self.assertEqual(
            schedule_plan.served_dates(
                {"2026-11-01": {"звук": ["Алексей"], "сборка": ["Алексей"]}}
            ),
            {"Алексей": ["2026-11-01"]},
        )

    def test_removing_all_member_tasks_keeps_member_out_of_plan(self):
        plan = self.draft()
        slots = plan["slots"]
        slots["2026-11-01"]["сборка"] = ["Глеб"]
        slots["2026-11-08"]["звук"] = ["Глеб"]
        self.registry.set_schedule_slots(1001, "sound", "2026-11", slots)
        self.registry.set_schedule_day(
            1001, "sound", "2026-11", "2026-11-08", "Глеб", -1
        )
        # Глеб только в привязках («сборка»); сняли сначала одну задачу —
        # уходит только из неё.
        self.registry.set_service_member_tasks(1001, "sound", "Глеб", ["звук"])
        plan = self.registry.get_schedule_plan("sound", "2026-11")
        self.assertNotIn("Глеб", plan["slots"]["2026-11-01"]["сборка"])
        self.assertIn("Глеб", plan["slots"]["2026-11-08"]["звук"])
        self.assertEqual(plan["days"], {"2026-11-08": {"Глеб": -1}})
        # Все задачи сняты: в составе, но не в графике и без привязок.
        self.assertEqual(
            self.registry.set_service_member_tasks(1001, "sound", "Глеб", []),
            [],
        )
        plan = self.registry.get_schedule_plan("sound", "2026-11")
        self.assertNotIn(
            "Глеб",
            [key for tasks in plan["slots"].values()
             for people in tasks.values() for key in people],
        )
        self.assertEqual(plan["days"], {})
        gleb = next(
            m for m in self.registry.list_service_members("sound")
            if m["name"] == "Глеб"
        )
        self.assertEqual(gleb["tasks"], [])
        self.assertEqual(
            [p["tasks"] for p in self.people() if p["name"] == "Глеб"], [[]]
        )
        # Пересчёт его тоже не ставит.
        self.assertNotIn("Глеб", [
            key for tasks in self.draft()["slots"].values()
            for people in tasks.values() for key in people
        ])

    def test_member_edits_and_removal_keep_plan_and_contacts_consistent(self):
        plan = self.draft()
        slots = plan["slots"]
        slots["2026-11-01"]["звук"] = ["Борис"]
        self.registry.set_schedule_slots(1001, "sound", "2026-11", slots)
        # Задачи Алексея заданы в боте: график не возвращает снятую задачу.
        self.registry.set_service_member_tasks(1001, "sound", "Алексей", ["сборка"])
        self.registry.ensure_member_contacts("sound", {"звук": ["Алексей"]})
        alex = next(
            person for person in self.people() if person["name"] == "Алексей"
        )
        self.assertEqual(alex["tasks"], ["сборка"])
        # Человек только из графика получает запись состава.
        self.registry.set_service_member_tasks(1001, "sound", "Олег", ["звук"])
        self.assertIn(
            "Олег",
            [item["name"] for item in self.registry.list_service_members("sound")],
        )
        # Убранный из состава уходит и из рабочей копии графика — вместе
        # с разовым днём.
        self.registry.set_schedule_day(
            1001, "sound", "2026-11", "2026-11-01", "Борис", -1
        )
        self.registry.remove_service_member(1001, "sound", "Борис")
        self.assertEqual(
            self.registry.get_schedule_plan("sound", "2026-11")["days"], {}
        )
        plan = self.registry.get_schedule_plan("sound", "2026-11")
        self.assertNotIn(
            "Борис",
            [key for tasks in plan["slots"].values()
             for people in tasks.values() for key in people],
        )
        # «Списки» Excel убранного не возвращают (решение 12).
        roster = {"sound": [
            RosterMember("Алексей", ("звук", "сборка")),
            RosterMember("Борис", ("звук",)),
        ]}
        self.registry.sync_service_rosters(roster)
        names = [m["name"] for m in self.registry.list_service_members("sound")]
        self.assertNotIn("Борис", names)
        # Добавили в боте снова — «Списки» снова его ведут.
        self.registry.bind_service_member(1001, "sound", "Борис", 5005)
        self.registry.sync_service_rosters(roster)
        boris = next(
            m for m in self.registry.list_service_members("sound")
            if m["name"] == "Борис"
        )
        self.assertTrue(boris["in_list"])

    def test_service_without_tasks_is_one_task(self):
        self.registry.sync_service_rosters({"office_cleaning": [
            RosterMember("Анна", ()), RosterMember("Вера", ()),
            RosterMember("Галя", ()),
        ]})
        name = self.registry.get_service("office_cleaning")["name"]
        people = self.registry.readiness_people("office_cleaning")
        self.assertEqual({tuple(item["tasks"]) for item in people}, {(name,)})
        # Пропавший из «Списков» в служение без задач не попадает.
        self.registry.sync_service_rosters({"office_cleaning": [
            RosterMember("Анна", ()), RosterMember("Вера", ()),
        ]})
        gone = next(
            item for item in self.registry.readiness_people("office_cleaning")
            if item["name"] == "Галя"
        )
        self.assertEqual(gone["tasks"], [])
        self.registry.sync_service_rosters({"office_cleaning": [
            RosterMember("Анна", ()), RosterMember("Вера", ()),
            RosterMember("Галя", ()),
        ]})
        self.registry.set_readiness_settings(
            1001, "office_cleaning", people_needed=1, people_max=2
        )
        tasks = self.registry.plan_tasks("office_cleaning")
        self.assertEqual(
            [(task["name"], task["needed"], task["max"], task["position"])
             for task in tasks],
            [(name, 1, 2, "s")],
        )
        plan = asyncio.run(schedule_plan.build_draft(
            self.registry, schedule_plan.free_run("office_cleaning", 2026, 11),
            None, LOG,
        ))
        # Служение без задач: люди сразу под датой, без названия служения.
        summary = schedule_plan.plan_summary(self.registry, plan, None)
        text = schedule_plan.plan_text("УБОРКА", plan, summary, TZ)
        self.assertNotIn(f"   {task_emoji(name)} {name}", text)
        self.assertIn("🗓 01.11\n   • ", text)
        published = schedule_plan.publication_text(
            self.registry.get_service("office_cleaning"), plan,
            self.registry.readiness_people("office_cleaning"),
        )
        self.assertNotIn(f"{name}:", published)
        counts = [len(plan["slots"][day][name]) for day in plan["dates"]]
        # Без опроса свободны все: минимум закрыт везде, место сверх
        # минимума — только без служения подряд (трое на 2 места в неделю).
        self.assertTrue(all(1 <= count <= 2 for count in counts))
        self.assertIn(2, counts)
        for days in schedule_plan.served_dates(plan["slots"]).values():
            for earlier, later in zip(days, days[1:]):
                self.assertGreater(
                    (date.fromisoformat(later) - date.fromisoformat(earlier)).days, 7
                )
        load = [len(days) for days in schedule_plan.served_dates(plan["slots"]).values()]
        self.assertLessEqual(max(load) - min(load), 1)
        with self.assertRaises(ValueError):
            self.registry.set_readiness_settings(
                1001, "office_cleaning", people_needed=3, people_max=2
            )
        # Выпавший из «Списков» и возвращённый «➕ В состав» — снова в нём.
        self.registry.sync_service_rosters({"office_cleaning": [
            RosterMember("Анна", ()), RosterMember("Вера", ()),
        ]})
        self.registry.bind_service_member(
            1001, "office_cleaning", "Галя", 909, from_readiness=True
        )
        galya = next(
            item for item in self.registry.readiness_people("office_cleaning")
            if item["name"] == "Галя"
        )
        self.assertEqual(galya["tasks"], [name])
        # Переименование служения — задача графика вслед, правки целы.
        with closing(sqlite3.connect(self.registry.path)) as connection:
            connection.execute(
                "UPDATE services SET name = 'Уборка' WHERE id = 'office_cleaning'"
            )
            connection.commit()
        self.registry.approve_schedule_plan(
            1001, "office_cleaning", plan["month"], plan["slots"],
            chat_id=None, message_id=None,
        )
        renamed = schedule_plan.sync_implicit_task(self.registry, plan)
        self.assertEqual(renamed["tasks"][0]["name"], "Уборка")
        # Опубликован со старым названием — смена названия публикуется
        # одной строкой.
        self.assertTrue(renamed["dirty"])
        self.assertEqual(
            schedule_plan.plan_changes(renamed["approved"], renamed["slots"], {}),
            [f"• Задача «{name}» теперь называется «Уборка»"],
        )
        # Несколько задач: выключили одну, включили другую — не
        # переименование.
        swap = schedule_plan.plan_changes(
            {"2026-11-01": {"звук": ["А"], "сборка": ["Б"]}},
            {"2026-11-01": {"звук": ["А"], "спевка": ["Б"]}},
            {},
        )
        self.assertNotIn("теперь называется", " ".join(swap))
        self.assertEqual(
            renamed["slots"][plan["dates"][0]]["Уборка"],
            plan["slots"][plan["dates"][0]][name],
        )

    @unittest.mock.patch(
        "bot.service_registry._current_month", return_value="2026-10"
    )
    def test_day_window_and_personal_day_in_texts(self, _month):
        self.assertEqual(
            schedule_plan.day_label("2026-11-01", (-1, 1)),
            "01.11 (сб 31.10 – пн 02.11)",
        )
        self.assertEqual(schedule_plan.day_label("2026-11-01"), "01.11")
        self.registry.set_readiness_settings(1001, "sound", day_from=-1, day_to=1)
        with self.assertRaises(ValueError):
            self.registry.set_readiness_settings(1001, "sound", day_from=1)
        self.registry.set_service_member_day(1001, "sound", "Алексей", -3)
        plan = self.draft()
        self.assertEqual(
            schedule_plan.slot_offset(
                plan, {"key": "Борис"}, "2026-11-01", "звук"
            ),
            -1,
        )
        plan["slots"]["2026-11-01"] = {"звук": ["Алексей"], "сборка": []}
        summary = readiness.summarize(self.registry, self.run, with_plan=False)
        text = schedule_plan.plan_text(
            "ЗВУК", plan, summary, TZ, schedule_plan.window_of(self.registry, "sound")
        )
        # У единственного на дату свой день — общий период не показывается.
        self.assertIn(
            "🗓 Неделя 26.10–01.11\n   сб 31.10\n     🎚️ звук",
            text,
        )
        self.assertIn("      • Алексей (чт 05.11)", text)
        # Рядом служит Глеб без своего дня — период для него показывается.
        self.assertIn(
            "🗓 Неделя 02.11–08.11\n   сб 07.11", text
        )
        published = schedule_plan.publication_text(
            self.service(), plan, self.people(), (-1, 1)
        )
        self.assertIn(
            "📅 График Звук на ноябрь 2026 утверждён\n\n"
            "🗓 Неделя 26.10–01.11\n  сб 31.10\n    🎚️ звук\n"
            "      • \x00", published,
        )
        self.assertIn(" (чт 29.10)", published)
        self.assertIn(
            "\n    🧰 сборка: —\n\n🗓 Неделя 02.11–08.11\n"
            "  сб 07.11", published
        )

        # Заголовок — свой шаблон из «📝 Тексты».
        self.registry.set_readiness_settings(
            1001, "sound",
            plan_title_template="График {service_name} на {month} подготовлен",
        )
        self.registry.set_readiness_phrase(1001, "sound", "empty", "пока никого")
        custom = schedule_plan.publication_text(
            self.service(), plan, self.people(), (-1, 1),
            self.registry.get_readiness_settings("sound"),
        )
        self.assertTrue(custom.startswith(
            "График Звук на ноябрь 2026 подготовлен\n\n"
            "🗓 Неделя 26.10–01.11"
        ))
        self.assertIn("🧰 сборка: пока никого", custom)
        for bad in ("{deadline}", "{status}"):
            with self.assertRaises(ValueError):
                self.registry.set_readiness_settings(
                    1001, "sound", plan_title_template=bad
                )
        with self.assertRaises(ValueError):
            self.registry.set_readiness_phrase(1001, "sound", "empty", "две\nстроки")
        self.assertIn("\x00 (чт 05.11)", published)
        self.registry.set_service_member_day(1001, "sound", "Алексей", None)
        alex = next(p for p in self.people() if p["name"] == "Алексей")
        self.assertIsNone(alex["day_offset"])
        # Утверждён, затем сменились дни — «с правками», в публикации строка.
        plan = self.registry.set_schedule_slots(
            1001, "sound", "2026-11", plan["slots"]
        )
        asyncio.run(schedule_plan.publish(
            self.bot, self.registry, self.service(), plan, 1001, LOG, 4096,
        ))
        self.assertFalse(self.registry.get_schedule_plan("sound", "2026-11")["dirty"])
        self.registry.set_readiness_settings(1001, "sound", day_from=0, day_to=0)
        changed = self.registry.get_schedule_plan("sound", "2026-11")
        self.assertTrue(changed["dirty"])
        self.assertIn(
            "• Изменены дни служения",
            schedule_plan.publication_text(self.service(), changed, self.people()),
        )
        asyncio.run(schedule_plan.publish(
            self.bot, self.registry, self.service(), changed, 1001, LOG, 4096,
        ))
        self.assertFalse(self.registry.get_schedule_plan("sound", "2026-11")["dirty"])
        self.registry.set_service_member_day(1001, "sound", "Борис", 0)
        self.assertTrue(self.registry.get_schedule_plan("sound", "2026-11")["dirty"])

    def test_saved_fallback_day_matches_reminder_and_excel_dates(self):
        self.registry.set_readiness_settings(
            1001, "sound", day_from=-1, day_to=1
        )
        plan = self.draft()
        plan["slots"]["2026-11-01"] = {
            "звук": ["Алексей"], "сборка": [],
        }
        moment, key = schedule_plan.task_moments(
            plan, {}, "2026-11-01", "звук"
        )[0]
        self.assertEqual((moment, key), (date(2026, 10, 31), "Алексей"))

        approved = {
            **plan,
            "approved": plan["slots"],
            "approved_tasks": plan["tasks"],
            "approved_days": {},
        }
        events = schedule_plan.plan_events(approved, self.people())
        self.assertEqual(
            [
                (event.event_date, assignment.name)
                for event in events for assignment in event.assignments
                if event.event_date == date(2026, 10, 31)
            ],
            [(date(2026, 10, 31), "Алексей")],
        )
        self.assertIn(
            "⚠️ сб 31.10 звук — Алексей: это лист «ОКТЯБРЬ26»",
            schedule_plan.excel_text(
                self.registry, self.service(), plan, self.people()
            ),
        )

    @unittest.mock.patch(
        "bot.service_registry._current_month", return_value="2026-10"
    )
    def test_service_window_updates_working_fallback_but_not_approved_dates(
        self, _month,
    ):
        self.registry.set_readiness_settings(
            1001, "sound", day_from=-1, day_to=1
        )
        plan = self.draft()
        plan["slots"]["2026-11-08"] = {"звук": ["Алексей"], "сборка": []}
        people = self.people()
        self.registry.approve_schedule_plan(
            1001, "sound", "2026-11", plan["slots"],
            chat_id=None, message_id=None, days=plan["days"],
            served_offsets=schedule_plan.served_offsets(plan, people),
            approved_slot_days=schedule_plan.approved_slot_days(plan, people),
        )
        for offset, end in ((0, 0), (-2, 1)):
            self.registry.set_readiness_settings(
                1001, "sound", day_from=offset, day_to=end
            )
            changed = self.registry.get_schedule_plan("sound", "2026-11")
            self.assertTrue(changed["dirty"])
            if offset == 0:
                self.assertTrue(all(
                    "default_day" not in task for task in changed["tasks"]
                ))
            self.assertEqual(
                schedule_plan.task_moments(
                    changed, {}, "2026-11-08", "звук"
                )[0][0],
                date(2026, 11, 8) + timedelta(days=offset),
            )
            self.assertEqual(
                schedule_plan.task_moments(
                    changed, {}, "2026-11-08", "звук", approved=True
                )[0][0],
                date(2026, 11, 7),
            )
        self.registry.approve_schedule_plan(
            1001, "sound", "2026-11", changed["slots"],
            chat_id=None, message_id=None, days=changed["days"],
            served_offsets=schedule_plan.served_offsets(changed, people),
            approved_slot_days=schedule_plan.approved_slot_days(changed, people),
        )
        published = self.registry.get_schedule_plan("sound", "2026-11")
        self.assertEqual(
            schedule_plan.task_moments(
                published, {}, "2026-11-08", "звук", approved=True
            )[0][0],
            date(2026, 11, 6),
        )

    def test_approved_reminder_keeps_published_personal_day(self):
        self.registry.set_readiness_settings(
            1001, "sound", day_from=0, day_to=0
        )
        self.registry.set_service_member_day(
            1001, "sound", "Алексей", None
        )
        plan = self.draft()
        plan["slots"]["2026-11-01"] = {
            "звук": ["Алексей"], "сборка": [],
        }
        people = self.people()
        self.registry.approve_schedule_plan(
            1001, "sound", "2026-11", plan["slots"],
            chat_id=None, message_id=None,
            days=plan["days"],
            served_offsets=schedule_plan.served_offsets(plan, people),
            approved_slot_days=schedule_plan.approved_slot_days(plan, people),
        )

        self.registry.set_service_member_day(
            1001, "sound", "Алексей", -3
        )
        approved = self.registry.get_schedule_plan("sound", "2026-11")
        events = schedule_plan.plan_events(approved, self.people())
        assignments = [
            (event.event_date, assignment.name)
            for event in events for assignment in event.assignments
        ]
        self.assertIn((date(2026, 11, 1), "Алексей"), assignments)
        self.assertNotIn((date(2026, 10, 29), "Алексей"), assignments)

    def test_working_copy_excel_exports_use_edited_day_after_approval(self):
        day = "2026-11-08"
        plan = self.draft()
        slots = {
            moment: {task["name"]: [] for task in plan["tasks"]}
            for moment in plan["dates"]
        }
        slots[day]["звук"] = ["Алексей"]
        plan = self.registry.set_schedule_slots(
            1001, "sound", "2026-11", slots
        )
        people = self.people()
        self.registry.approve_schedule_plan(
            1001, "sound", "2026-11", plan["slots"],
            chat_id=None, message_id=None, days=plan["days"],
            served_offsets=schedule_plan.served_offsets(plan, people),
            approved_slot_days=schedule_plan.approved_slot_days(plan, people),
        )
        working = self.registry.set_schedule_day(
            1001, "sound", "2026-11", day, "Алексей", -2
        )

        self.assertTrue(working["dirty"])
        self.assertEqual(
            working["approved_slot_days"][day]["звук"]["Алексей"], 0
        )
        self.assertEqual(
            [
                (item.event_date, assignment.name)
                for item in schedule_plan.plan_events(working, people)
                for assignment in item.assignments
            ],
            [(date(2026, 11, 8), "Алексей")],
        )
        with self.subTest(output="text"):
            text = schedule_plan.excel_text(
                self.registry, self.service(), working, people
            )
            self.assertIn("Алексей (пт 06.11)", text)
        with self.subTest(output="xlsx"):
            workbook = load_workbook(BytesIO(schedule_plan.excel_file(
                self.registry, self.service(), working, people
            )))
            self.addCleanup(workbook.close)
            current_day = None
            assignments = []
            for row in workbook.active.iter_rows(values_only=True):
                if isinstance(row[0], datetime):
                    current_day = row[0].date()
                elif isinstance(row[0], date):
                    current_day = row[0]
                elif row[1] == "Алексей":
                    assignments.append((current_day, row[1]))
            self.assertEqual(assignments, [(date(2026, 11, 6), "Алексей")])

        approved = self.registry.approve_schedule_plan(
            1001, "sound", "2026-11", working["slots"],
            chat_id=None, message_id=None, days=working["days"],
            served_offsets=schedule_plan.served_offsets(working, people),
            approved_slot_days=schedule_plan.approved_slot_days(working, people),
        )
        self.assertEqual(
            [
                (item.event_date, assignment.name)
                for item in schedule_plan.plan_events(approved, people)
                for assignment in item.assignments
            ],
            [(date(2026, 11, 6), "Алексей")],
        )

    def test_excel_import_replaces_approved_reminder_date_snapshot(self):
        self.registry.set_service_task_day(
            1001, "sound", "сборка", -4
        )
        people = self.people()
        old = self.draft()
        old["slots"]["2026-11-08"] = {
            "звук": [], "сборка": ["Дмитрий"],
        }
        self.registry.approve_schedule_plan(
            1001, "sound", "2026-11", old["slots"],
            chat_id=None, message_id=None,
            days=old["days"],
            served_offsets=schedule_plan.served_offsets(old, people),
            approved_slot_days=schedule_plan.approved_slot_days(old, people),
        )
        imported = schedule_plan.excel_plan(
            self.registry, "sound", 2026, 11,
            [event(date(2026, 11, 8), ("сборка", "Дмитрий"))],
        )
        self.assertEqual(imported["days"]["2026-11-08"], {
            schedule_plan.task_day_key("Дмитрий", "сборка"): 0
        })
        self.registry.import_excel_plan(
            1001, "sound", "2026-11",
            dates=imported["dates"], tasks=imported["tasks"],
            slots=imported["slots"], days=imported["days"],
            served_offsets=schedule_plan.served_offsets(imported, people),
            approved_slot_days=schedule_plan.approved_slot_days(
                imported, people
            ),
        )
        plan = self.registry.get_schedule_plan("sound", "2026-11")
        assignments = [
            (item.event_date, assignment.name)
            for item in schedule_plan.plan_events(plan, people)
            for assignment in item.assignments
            if assignment.task == "сборка"
        ]
        self.assertEqual(assignments, [(date(2026, 11, 8), "Дмитрий")])

    def test_excel_import_keeps_unknown_person_date_in_snapshots(self):
        self.registry.set_service_task_day(1001, "sound", "сборка", -4)
        loaded = schedule_plan.excel_plan(
            self.registry, "sound", 2026, 11,
            [event(date(2026, 11, 8), ("сборка", "Незнакомец"))],
        )
        people = self.people()
        approved_days = schedule_plan.approved_slot_days(loaded, people)
        offsets = schedule_plan.served_offsets(loaded, people)

        self.assertEqual(approved_days["2026-11-08"]["сборка"]["Незнакомец"], 0)
        self.assertEqual(offsets.get("2026-11-08", {}).get("Незнакомец", 0), 0)
        self.assertEqual(
            schedule_plan.served_days(loaded, people)["Незнакомец"],
            {date(2026, 11, 8)},
        )
        plan = self.registry.import_excel_plan(
            1001, "sound", "2026-11", dates=loaded["dates"],
            tasks=loaded["tasks"], slots=loaded["slots"], days=loaded["days"],
            served_offsets=offsets, approved_slot_days=approved_days,
        )
        self.assertEqual(
            [
                (item.event_date, assignment.name)
                for item in schedule_plan.plan_events(plan, people)
                for assignment in item.assignments
            ],
            [(date(2026, 11, 8), "Незнакомец")],
        )

    def test_task_day_shows_plan_by_weeks(self):
        plan = self.draft()
        plan["slots"]["2026-11-01"] = {"звук": ["Алексей"], "сборка": ["Борис"]}
        plan = self.registry.set_schedule_slots(
            1001, "sound", "2026-11", plan["slots"]
        )
        plan = asyncio.run(schedule_plan.publish(
            self.bot, self.registry, self.service(), plan, 1001, LOG, 4096,
        ))
        self.assertFalse(plan["dirty"])
        with self.assertRaises(ValueError):
            self.registry.set_service_task_day(1001, "sound", "сборка", 7)
        with self.assertRaises(ValueError):
            self.registry.set_service_task_day(1001, "sound", "нет такой", -4)
        self.registry.set_service_task_day(1001, "sound", "сборка", -4)
        task = next(
            item for item in self.registry.list_service_tasks("sound")
            if item["name"] == "сборка"
        )
        self.assertEqual(task["day_offset"], -4)
        # День задачи — в уже составленном графике, утверждённый — заново.
        plan = self.registry.get_schedule_plan("sound", "2026-11")
        self.assertTrue(plan["dirty"])
        self.assertEqual(schedule_plan.task_days(plan), {"сборка": -4})
        self.assertEqual(
            next(t for t in self.draft()["tasks"] if t["name"] == "сборка")["day"],
            -4,
        )
        plan = self.registry.get_schedule_plan("sound", "2026-11")
        assembly_person = plan["slots"]["2026-11-01"]["сборка"][0]
        sound_person = plan["slots"]["2026-11-01"]["звук"][0]
        summary = readiness.summarize(self.registry, self.run, with_plan=False)
        text = schedule_plan.plan_text("ЗВУК", plan, summary, TZ)
        self.assertIn(
            "🗓 Неделя 26.10–01.11\n   ср 28.10\n     🧰 сборка\n"
            f"        • {assembly_person}\n   вс 01.11\n     🎚️ звук\n"
            f"        • {sound_person}",
            text,
        )
        published = schedule_plan.publication_text(
            self.service(), plan, self.people()
        )
        self.assertIn(
            "\n\n🗓 Неделя 26.10–01.11\n  ср 28.10\n    🧰 сборка\n      • ",
            published,
        )
        self.assertIn("\n  вс 01.11\n    🎚️ звук\n      • ", published)
        self.assertIn("• Изменены дни служения", published)
        # Задача без людей во всех датах черновика — подсказка пересчитать.
        draft = {**plan, "approved": None, "slots": {
            day: {**tasks, "сборка": []} for day, tasks in plan["slots"].items()
        }}
        self.assertIn(
            "ℹ️ Никого во все даты: «сборка». Если задачу включили после "
            "составления — «🔄 Пересчитать».",
            schedule_plan.plan_text("ЗВУК", draft, summary, TZ),
        )
        self.assertNotIn(
            "Никого во все даты", schedule_plan.plan_text("ЗВУК", plan, summary, TZ)
        )
        # Блок «Для Excel» — с фактическим днём задачи.
        self.assertIn(
            f"01.11: звук — {sound_person}; сборка (ср 28.10) — {assembly_person}",
            schedule_plan.excel_text(
                self.registry, self.service(), plan, self.people()
            ),
        )
        self.assertEqual(
            schedule_plan.slot_offset(
                plan, {"key": assembly_person}, "2026-11-01", "сборка"
            ),
            -4,
        )
        served = schedule_plan.served_days(plan, self.people())
        self.assertIn(
            date(2026, 10, 28), served[assembly_person]
        )
        self.assertIn(
            date(2026, 11, 1), served[sound_person]
        )
        # Разовый день человека главнее дня задачи — в скобках.
        plan = self.registry.set_schedule_day(
            1001, "sound", "2026-11", "2026-11-01", assembly_person, -3
        )
        self.assertEqual(
            schedule_plan.slot_offset(plan, {"key": assembly_person}, "2026-11-01",
                                      "сборка"),
            -3,
        )
        self.assertIn(
            f"• {assembly_person} (чт 29.10)",
            schedule_plan.plan_text("ЗВУК", plan, summary, TZ),
        )
        # С окном служения задачи без своего дня — под периодом окна.
        title, groups = schedule_plan.week_groups(plan, "2026-11-01", (-1, 1))
        self.assertEqual(title, "Неделя 26.10–02.11")
        self.assertEqual(
            [(label, offset) for label, offset, _ in groups],
            [("ср 28.10", -4), ("сб 31.10 – пн 02.11", None)],
        )
        # Только задачи со своим днём — окно служения не растягивает неделю.
        only_days = {**plan, "tasks": [
            task for task in plan["tasks"] if task["name"] == "сборка"
        ]}
        self.assertEqual(
            schedule_plan.week_groups(only_days, "2026-11-01", (-1, 1))[0],
            "Неделя 26.10–01.11",
        )
        # Задача, которой нет в графике, не трогает утверждённый график.
        plan = asyncio.run(schedule_plan.publish(
            self.bot, self.registry, self.service(), plan, 1001, LOG, 4096,
        ))
        self.assertFalse(plan["dirty"])
        self.registry.set_service_task_day(1001, "sound", "спевка", -4)
        self.assertFalse(
            self.registry.get_schedule_plan("sound", "2026-11")["dirty"]
        )
        self.registry.set_service_task_day(1001, "sound", "сборка", None)
        plan = self.registry.get_schedule_plan("sound", "2026-11")
        self.assertTrue(plan["dirty"])
        self.assertEqual(schedule_plan.task_days(plan), {})
        self.assertIn(
            "🗓 01.11", schedule_plan.publication_text(
                self.service(), plan, self.people()
            ),
        )

    def test_day_for_one_date(self):
        plan = self.draft()
        plan["slots"]["2026-11-15"]["звук"] = ["Борис"]
        plan = self.registry.set_schedule_slots(1001, "sound", "2026-11", plan["slots"])
        plan = asyncio.run(schedule_plan.publish(
            self.bot, self.registry, self.service(), plan, 1001, LOG, 4096,
        ))
        self.assertFalse(plan["dirty"])
        plan = self.registry.set_schedule_day(
            1001, "sound", "2026-11", "2026-11-15", "Борис", -2
        )
        self.assertTrue(plan["dirty"])
        summary = readiness.summarize(self.registry, self.run, with_plan=False)
        self.assertIn(
            "• Борис (пт 13.11)",
            schedule_plan.plan_text("ЗВУК", plan, summary, TZ),
        )
        published = schedule_plan.publication_text(
            self.service(), plan, self.people()
        )
        self.assertIn("• 15.11 Борис: пт 13.11", published)
        plan = asyncio.run(schedule_plan.publish(
            self.bot, self.registry, self.service(), plan, 1001, LOG, 4096,
        ))
        self.assertFalse(plan["dirty"])
        plan = self.registry.set_schedule_day(
            1001, "sound", "2026-11", "2026-11-15", "Борис", None
        )
        self.assertEqual(plan["days"], {})
        self.assertTrue(plan["dirty"])
        # Пересчёт сбрасывает разовые дни, как и другие правки.
        self.registry.set_schedule_day(
            1001, "sound", "2026-11", "2026-11-15", "Борис", -2
        )
        self.assertEqual(self.draft()["days"], {})

    def test_order_only_change_is_not_dirty(self):
        plan = self.draft()
        slots = plan["slots"]
        slots["2026-11-01"]["звук"] = ["Алексей", "Борис"]
        plan = self.registry.set_schedule_slots(1001, "sound", "2026-11", slots)
        self.registry.approve_schedule_plan(
            1001, "sound", "2026-11", plan["slots"], chat_id=None,
            message_id=None,
        )
        slots["2026-11-01"]["звук"] = ["Борис", "Алексей"]
        plan = self.registry.set_schedule_slots(1001, "sound", "2026-11", slots)
        self.assertFalse(plan["dirty"])

    def test_save_failure_after_publish_is_reported(self):
        plan = self.draft()
        with unittest.mock.patch.object(
            self.registry, "approve_schedule_plan",
            side_effect=RuntimeError("locked"),
        ):
            with self.assertRaises(schedule_plan.PublishedNotSaved):
                asyncio.run(schedule_plan.publish(
                    self.bot, self.registry, self.service(), plan, 1001, LOG,
                    4096,
                ))

    def test_publish_without_chat_is_refused(self):
        plan = self.draft()
        self.registry.set_group_chat(1001, "sound", -1005555, enabled=False)
        with self.assertRaises(ValueError):
            asyncio.run(schedule_plan.publish(
                self.bot, self.registry, self.service(), plan, 1001, LOG, 4096,
            ))
        self.assertIsNone(
            self.registry.get_schedule_plan("sound", "2026-11")["approved"]
        )

    def test_plan_text_candidates_and_notes(self):
        plan = self.draft()
        summary = readiness.summarize(self.registry, self.run, with_plan=False)
        text = schedule_plan.plan_text("ЗВУК", plan, summary, TZ)
        self.assertIn("📋 График · ЗВУК · ноябрь 2026", text)
        self.assertIn("📝 Черновик", text)
        self.assertIn("Ответили 3 из 4", text)
        self.assertIn("🗓 01.11", text)
        self.assertIn("👥 Нагрузка (дней служения в месяце):", text)
        # Глеб отметил 01 и 08 — подряд не ставится, 01.11 у Дмитрия.
        self.assertIn("   3 — Дмитрий (без Telegram)", text)
        self.assertIn("   🎚️ звук\n      • Борис", text)
        self.assertIn("Дмитрий", text)

        day = "2026-11-08"
        plan["slots"][day] = {"звук": ["Алексей"], "сборка": []}
        alex_option = readiness.option_key(day, "звук")
        summary.answered["Алексей"] = [alex_option]
        summary.available["Алексей"] = [alex_option]
        text = schedule_plan.plan_text("ЗВУК", plan, summary, TZ)
        sound_week = text.split("🗓 08.11", 1)[1].split("🗓 15.11", 1)[0]
        self.assertIn("🎚️ звук\n      • Алексей\n", sound_week)
        self.assertNotIn(
            "⚠️ не отметил(а)",
            schedule_plan.choice_notes(plan, summary, "Алексей", day, "звук"),
        )
        free, _, _ = schedule_plan.candidates({
            **plan, "slots": {
                **plan["slots"], day: {"звук": [], "сборка": []},
            },
        }, summary, day, "звук")
        self.assertIn("Алексей", free)
        free, quiet, rest = schedule_plan.candidates(plan, summary, day, "сборка")
        # Алексей занят на «звук» — не предлагается (одна задача в день).
        self.assertNotIn("Алексей", free + quiet + rest)
        self.assertEqual(free, ["Глеб"])
        self.assertEqual(quiet, ["Дмитрий"])
        self.assertEqual(rest, ["Борис"])
        self.assertEqual(
            schedule_plan.busy_on(plan, day, "сборка"), {"Алексей": "звук"}
        )
        plan["slots"]["2026-11-01"]["звук"] = ["Борис"]
        notes = schedule_plan.choice_notes(plan, summary, "Борис", day, "сборка")
        self.assertIn("⚠️ не отметил(а) 08.11", notes)
        self.assertIn("⚠️ не приписан(а) к «сборка»", notes)
        self.assertIn("ℹ️ служил(а) 01.11", notes)
        notes = schedule_plan.choice_notes(plan, summary, "Дмитрий", day, "сборка")
        self.assertEqual(notes[0], "⚪ не отвечал(а) на опрос")
        self.assertIn("ℹ️ служит 15.11", notes)
        plan["slots"]["2026-11-15"] = {"звук": [], "сборка": []}
        self.assertIn("⚠️ 15.11 «звук»: никого", schedule_plan.gaps(plan))
        # Ответивший на неотмеченной дате (решение 16) — «°» и сноска.
        plan["slots"][day] = {"звук": ["Борис"], "сборка": []}
        text = schedule_plan.plan_text("ЗВУК", plan, summary, TZ)
        self.assertIn("🗓 01.11\n   🎚️ звук\n      • Борис\n", text)
        self.assertIn("• Борис°", text)
        self.assertIn("° не отмечал(а) эту дату в опросе", text)

    def test_served_days_counts_distinct_actual_dates(self):
        plan = {
            "slots": {
                "2026-11-01": {
                    "звук": ["Алексей"], "спевка": ["Алексей"],
                },
            },
            "tasks": [
                {"name": "звук", "day": 0},
                {"name": "спевка", "day": -3},
            ],
            "days": {},
        }
        people = [{"key": "Алексей", "day_offset": None}]
        self.assertEqual(
            schedule_plan.served_days(plan, people),
            {"Алексей": {date(2026, 11, 1), date(2026, 10, 29)}},
        )

    def test_excel_block_and_file(self):
        plan = self.draft()
        plan["slots"]["2026-11-01"] = {"звук": ["Алексей", "Борис"], "сборка": []}
        people = self.people()
        rows = schedule_plan.excel_rows(self.registry, plan, people)
        self.assertEqual(rows[0][:3], ["звук", "", "спевка"])
        self.assertEqual(rows[1][0], date(2026, 11, 1))
        self.assertEqual(rows[2][:2], ["звук", "Алексей"])
        self.assertEqual(rows[3][:2], ["", "Борис"])
        text = schedule_plan.excel_text(self.registry, self.service(), plan, people)
        self.assertIn("лист «НОЯБРЬ26»", text)
        self.assertIn("01.11: звук — Алексей, Борис; сборка — пусто", text)
        data = schedule_plan.excel_file(self.registry, self.service(), plan, people)
        sheet = load_workbook(BytesIO(data)).active
        self.assertEqual(sheet.title, "НОЯБРЬ26")
        self.assertEqual(sheet["A2"].number_format, "DD.MM.YYYY")
        # Файл читается тем же парсером, что и лист графика.
        path = Path(self.temp_dir.name) / "plan.xlsx"
        path.write_bytes(data)
        events = parse_service_events(
            path, date(2026, 11, 1), LOG, self.registry.service_catalog()
        )["sound"]
        by_day = {
            item.event_date: sorted(
                (row.task, row.name) for row in item.assignments
            )
            for item in events
        }
        self.assertEqual(
            by_day[date(2026, 11, 1)], [("звук", "Алексей"), ("звук", "Борис")]
        )
        self.assertEqual(
            by_day[date(2026, 11, 8)],
            sorted(
                (task, name)
                for task, keys in plan["slots"]["2026-11-08"].items()
                for name in keys
            ),
        )

    def test_task_day_goes_to_its_own_date_in_excel(self):
        self.registry.set_service_task_day(1001, "sound", "сборка", -4)
        plan = self.draft()
        plan["slots"]["2026-11-08"] = {"звук": ["Алексей"], "сборка": ["Дмитрий"]}
        people = self.people()
        rows = schedule_plan.excel_rows(self.registry, plan, people)
        dated = [row[0] for row in rows if isinstance(row[0], date)]
        # Среда — своим блоком перед своим воскресеньем.
        # 28.10 (к 01.11) — октябрь: не на листе ноября, а в тексте.
        self.assertEqual(
            dated[:3],
            [date(2026, 11, 1), date(2026, 11, 4), date(2026, 11, 8)],
        )
        plan["slots"]["2026-11-01"]["сборка"] = ["Дмитрий"]
        self.assertIn(
            "⚠️ ср 28.10 сборка — Дмитрий: это лист «ОКТЯБРЬ26»",
            schedule_plan.excel_text(
                self.registry, self.service(), plan, people
            ),
        )
        self.assertNotIn(
            date(2026, 10, 28),
            [r[0] for r in schedule_plan.excel_rows(self.registry, plan, people)],
        )
        path = Path(self.temp_dir.name) / "plan.xlsx"
        path.write_bytes(
            schedule_plan.excel_file(self.registry, self.service(), plan, people)
        )
        events = parse_service_events(
            path, date(2026, 11, 1), LOG, self.registry.service_catalog()
        )["sound"]
        by_day = {
            item.event_date: sorted((row.task, row.name) for row in item.assignments)
            for item in events
        }
        self.assertEqual(by_day[date(2026, 11, 4)], [("сборка", "Дмитрий")])
        self.assertEqual(by_day[date(2026, 11, 8)], [("звук", "Алексей")])
        # Разовый день человека главнее дня задачи и в файле.
        plan["days"] = {"2026-11-08": {"Дмитрий": 0}}
        dated = [
            row[0] for row in schedule_plan.excel_rows(self.registry, plan, people)
            if isinstance(row[0], date)
        ]
        self.assertNotIn(date(2026, 11, 4), dated)
        # Дмитрий не потерян: в файле — в блоке 08.11 вместе с Алексеем.
        path.write_bytes(
            schedule_plan.excel_file(self.registry, self.service(), plan, people)
        )
        moved = parse_service_events(
            path, date(2026, 11, 1), LOG, self.registry.service_catalog()
        )["sound"]
        self.assertEqual(
            next(
                sorted((row.task, row.name) for row in item.assignments)
                for item in moved if item.event_date == date(2026, 11, 8)
            ),
            [("звук", "Алексей"), ("сборка", "Дмитрий")],
        )
        self.assertIn(
            "08.11: звук — Алексей; сборка (ср 04.11) — Дмитрий (вс 08.11)",
            schedule_plan.excel_text(
                self.registry, self.service(), plan, people
            ),
        )
        plan["days"] = {}
        # «Ответственные»: среда из файла — в блоке своего воскресенья.
        blocks = schedule_plan.week_events(events, {"сборка": -4})
        first = next(b for b in blocks if b["date"] == date(2026, 11, 8))
        self.assertEqual(
            sorted((row.task, row.name) for row in first["assignments"]),
            [("звук", "Алексей"), ("сборка", "Дмитрий")],
        )
        self.assertEqual(first["days"], {"сборка": date(2026, 11, 4)})
        self.assertNotIn(date(2026, 11, 4), [b["date"] for b in blocks])
        # Дата без назначений не пропадает.
        empty = [ServiceEvent("sound", date(2026, 11, 15), ())]
        self.assertEqual(
            [b["date"] for b in schedule_plan.week_events(empty, {})],
            [date(2026, 11, 15)],
        )
        # Без дня задачи — события как в файле.
        self.assertIn(
            date(2026, 11, 4),
            [b["date"] for b in schedule_plan.week_events(events, {})],
        )

    def test_excel_rows_put_own_task_in_its_column(self):
        plan = {
            "service_id": "broadcast", "month": "2026-11",
            "dates": ["2026-11-01"],
            "tasks": [
                {"name": "Пульт", "needed": 1, "max": None},
                {"name": "Слайды", "needed": 1, "max": None},
            ],
            "slots": {"2026-11-01": {"Пульт": ["Ира"], "Слайды": ["Оля"]}},
        }
        rows = schedule_plan.excel_rows(self.registry, plan, [])
        self.assertEqual(rows[0], ["трансляция", "", "слайды"])
        self.assertEqual(rows[2], ["Пульт", "Ира", "Оля"])

    def test_excel_filled(self):
        events = [event(date(2026, 11, 1), ("звук", "Борис"))]
        self.assertTrue(schedule_plan.excel_filled(events, "sound", 2026, 11))
        self.assertFalse(schedule_plan.excel_filled(events, "sound", 2026, 12))
        self.assertFalse(schedule_plan.excel_filled(events, "kitchen", 2026, 11))

    def test_finish_run_sends_plan_with_button(self):
        bot = fake_bot()
        run = self.registry.get_readiness_run_by_id(self.run["id"])
        asyncio.run(readiness.finish_run(
            bot, self.registry, run, self.service(), TZ, 1001, LOG, 4096,
        ))
        admin_call = next(
            call for call in bot.send_message.await_args_list
            if call.args[0] == 1001
        )
        self.assertIn("Опрос завершён", admin_call.args[1])
        self.assertIn("📋 График · Звук", admin_call.args[1])
        buttons = [
            button.callback_data
            for row in admin_call.kwargs["reply_markup"].inline_keyboard
            for button in row
        ]
        self.assertIn("rdy:plan:sound:202611", buttons)
        self.assertIsNotNone(self.registry.get_schedule_plan("sound", "2026-11"))


    def test_failed_draft_is_retried_without_repeating_notice(self):
        bot = fake_bot()
        run = self.registry.get_readiness_run_by_id(self.run["id"])
        with unittest.mock.patch(
            "bot.schedule_plan.ensure_plan", side_effect=RuntimeError("db")
        ):
            for _ in range(2):
                asyncio.run(readiness.finish_run(
                    bot, self.registry, run, self.service(), TZ, 1001, LOG,
                    4096,
                ))
        admin_texts = [
            call.args[1] for call in bot.send_message.await_args_list
            if call.args[0] == 1001
        ]
        self.assertEqual(len(admin_texts), 1)
        self.assertIn("график составить пока не удалось", admin_texts[0])
        self.assertFalse(
            self.registry.get_readiness_run_by_id(run["id"])["result_sent"]
        )
        asyncio.run(readiness.finish_run(
            bot, self.registry, run, self.service(), TZ, 1001, LOG, 4096,
        ))
        self.assertTrue(
            self.registry.get_readiness_run_by_id(run["id"])["result_sent"]
        )
        self.assertIn("📋 График", bot.send_message.await_args.args[1])

    def test_partial_publication_is_removed(self):
        plan = self.draft()
        bot = fake_bot()
        bot.delete_message = AsyncMock()
        bot.send_message.side_effect = [
            SimpleNamespace(message_id=31), RuntimeError("network"),
        ]
        with unittest.mock.patch(
            "bot.schedule_plan.publication_text", return_value="x " * 3000
        ):
            with self.assertRaises(RuntimeError):
                asyncio.run(schedule_plan.publish(
                    bot, self.registry, self.service(), plan, 1001, LOG, 4096,
                ))
        bot.delete_message.assert_awaited_once_with(-1005555, 31)
        self.assertIsNone(
            self.registry.get_schedule_plan("sound", "2026-11")["approved"]
        )


if __name__ == "__main__":
    unittest.main()
