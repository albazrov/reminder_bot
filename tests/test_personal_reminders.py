import asyncio
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

from bot.personal_reminders import (
    personal_reminder_block_reason,
    personal_reminder_forecast,
    personal_rule_label,
)
from bot.scheduler import reminder_send_datetime
from bot.service_registry import ServiceRegistry


class PersonalReminderForecastTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.registry = ServiceRegistry(Path(self.tmp.name) / "registry.sqlite3")
        self.registry.initialize(1001, -10001)
        self.registry.set_group_chat(1001, "sound", -10055, enabled=True)
        self.now = datetime(2026, 11, 10, 12, tzinfo=ZoneInfo("Europe/Moscow"))
        self.month = date(2026, 11, 1)

    def rule(self, name="Алексей", person=2001, task="Микшер", **kwargs):
        self.registry.set_responsible_contact(1001, "sound", task, name, person)
        self.registry.set_user_notifications_enabled(person, "sound", True)
        text_id = self.registry.ensure_user_text(person, "sound", task)
        return self.registry.add_schedule(
            text_id, kwargs.pop("days", 1), kwargs.pop("clock", "18:00"), **kwargs,
        )

    def approve(self, slots, *, month="2026-11", tasks=None):
        self.registry.save_schedule_draft(
            "sound", month, run_id=None, dates=list(slots),
            tasks=tasks or [{"name": "Микшер", "needed": 1}],
            slots=slots, last_served={},
        )
        self.registry.approve_schedule_plan(
            1001, "sound", month, slots, chat_id=None, message_id=None,
        )

    def forecast(self):
        return "\n\n".join(personal_reminder_forecast(
            self.registry, 1001, "sound", self.month, self.now,
        ))

    def test_exact_dates_timezone_and_past_time(self):
        self.rule()
        self.approve({
            "2026-11-08": {"Микшер": ["Алексей"]},
            "2026-11-15": {"Микшер": ["Алексей"]},
        })
        text = self.forecast()
        self.assertIn("14.11.2026 18:00 — запланировано", text)
        self.assertIn(
            "07.11.2026 18:00 — время прошло, отправка не зафиксирована", text,
        )
        self.assertEqual(
            reminder_send_datetime(date(2026, 11, 15), 1, "18:00", self.now.tzinfo),
            datetime(2026, 11, 14, 18, tzinfo=self.now.tzinfo),
        )

    def test_send_date_outside_selected_month_and_year(self):
        self.rule(days=6)
        self.month = date(2027, 1, 1)
        self.now = datetime(2026, 12, 1, tzinfo=self.now.tzinfo)
        self.approve({"2027-01-03": {"Микшер": ["Алексей"]}}, month="2027-01")
        self.assertIn("28.12.2026 18:00 — запланировано", self.forecast())

    def test_own_day_from_adjacent_plan_is_included_by_actual_date(self):
        self.rule()
        self.approve(
            {"2026-12-06": {"Микшер": ["Алексей"]}}, month="2026-12",
            tasks=[{"name": "Микшер", "needed": 1, "day": -6}],
        )
        text = self.forecast()
        self.assertIn("30.11.2026 · Микшер · Алексей", text)
        self.assertIn("29.11.2026 18:00 — запланировано", text)
        self.assertNotIn("06.12.2026 ·", text)
        self.assertIn("График выбранного месяца не утверждён", text)

    def test_disabled_notifications_and_disabled_rule(self):
        first = self.rule()
        self.rule("Мария", 3001, enabled=False)
        self.registry.set_user_notifications_enabled(2001, "sound", False)
        self.approve({"2026-11-15": {"Микшер": ["Алексей", "Мария"]}})
        text = self.forecast()
        self.assertIn("уведомления выключены", text)
        self.assertIn("правило выключено", text)
        listed = self.registry.list_service_user_reminder_schedules(1001, "sound")
        self.assertEqual(len(listed), 2)
        self.assertEqual(listed[0]["schedule_id"], first["id"])

    def test_delivery_and_answers(self):
        rule = self.rule()
        for answer, expected in ((None, "уже отправлено"), ("confirmed", "принял"),
                                 ("declined", "отказался")):
            with self.subTest(answer=answer):
                self.approve({"2026-11-15": {"Микшер": ["Алексей"]}})
                self.registry.mark_user_reminder_delivered(rule["id"], "2026-11-15")
                if answer:
                    self.registry.set_confirmation(
                        "sound", "2026-11-15", "Микшер", 2001, answer,
                    )
                self.assertIn(f"18:00 — {expected}", self.forecast())

    def test_multiple_rules_and_no_assignment(self):
        self.rule()
        self.rule("Мария", 3001)
        text_id = self.registry.ensure_user_text(2001, "sound", "Микшер")
        self.registry.add_schedule(text_id, 0, "09:00", origin="user")
        self.approve({"2026-11-15": {"Микшер": ["Алексей"]}})
        text = self.forecast()
        self.assertIn("14.11.2026 18:00", text)
        self.assertIn("15.11.2026 09:00", text)
        self.assertIn("Правило: своё, в день служения", text)
        self.assertIn("Мария · Микшер", text)
        self.assertIn("нет назначения", text)

    def test_no_binding_and_no_rules(self):
        self.registry.set_responsible_contact(
            1001, "sound", "Микшер", "Мария", 3001,
        )
        self.registry.set_user_notifications_enabled(3001, "sound", True)
        self.approve({"2026-11-15": {"Микшер": ["Алексей", "Мария"]}})
        text = self.forecast()
        self.assertIn("Алексей\nНе отправляется: нет привязки Telegram", text)
        self.assertIn("Мария\nНе отправляется: нет включённого правила", text)

    def test_answer_without_remaining_rule_is_visible(self):
        self.registry.set_responsible_contact(
            1001, "sound", "Микшер", "Мария", 3001,
        )
        self.approve({"2026-11-15": {"Микшер": ["Мария"]}})
        self.registry.set_confirmation(
            "sound", "2026-11-15", "Микшер", 3001, "confirmed",
        )
        self.assertIn("Статус: принял.", self.forecast())

    def test_approved_empty_date_is_not_treated_as_missing_approval(self):
        self.approve({"2026-11-15": {"Микшер": []}})
        text = self.forecast()
        self.assertIn("В утверждённом графике нет назначений", text)
        self.assertNotIn("не утверждён", text)

    def test_replacement_is_applied_and_former_assignee_not_predicted(self):
        self.rule()
        self.rule("Мария", 3001)
        self.approve({"2026-11-15": {"Микшер": ["Алексей"]}})
        self.registry.set_replacement(
            "sound", "2026-11-15", "Микшер", "Алексей", "Мария",
            3001, "", "admin", 1001,
        )
        text = self.forecast()
        self.assertIn("15.11.2026 · Микшер · Мария", text)
        self.assertNotIn("15.11.2026 · Микшер · Алексей", text)

    def test_empty_approved_plan_differs_from_no_approval(self):
        self.rule()
        self.assertIn("график не утверждён", self.forecast())
        self.approve({})
        text = self.forecast()
        self.assertIn("В утверждённом графике нет назначений", text)
        self.assertNotIn("график не утверждён", text)
        self.assertIn("нет назначения", text)

    def test_removed_person_key_does_not_expose_internal_telegram_id(self):
        self.approve({"2026-11-15": {"Микшер": ["Алексей · 2001"]}})
        text = self.forecast()
        self.assertIn("Алексей (нет актуальной привязки)", text)
        self.assertNotIn("2001", text)

    def test_changed_draft_does_not_replace_approved_assignments(self):
        self.rule()
        self.approve({"2026-11-15": {"Микшер": ["Алексей"]}})
        self.registry.save_schedule_draft(
            "sound", "2026-11", run_id=None, dates=["2026-11-15"],
            tasks=[{"name": "Микшер", "needed": 1}],
            slots={"2026-11-15": {"Микшер": ["Черновик"]}}, last_served={},
        )
        text = self.forecast()
        self.assertIn("15.11.2026 · Микшер · Алексей", text)
        self.assertNotIn("Черновик", text)

    def test_taskless_service(self):
        service_id = "books"
        task = self.registry.get_service(service_id)["name"]
        self.registry.set_group_chat(1001, service_id, -10066, enabled=True)
        self.registry.set_responsible_contact(1001, service_id, task, "Мария", 3001)
        self.registry.set_user_notifications_enabled(3001, service_id, True)
        text_id = self.registry.ensure_user_text(3001, service_id, task)
        self.registry.add_schedule(text_id, 0, "09:00")
        slots = {"2026-11-15": {task: ["Мария"]}}
        self.registry.save_schedule_draft(
            service_id, "2026-11", run_id=None, dates=list(slots),
            tasks=[{"name": task, "needed": 1, "implicit": True}],
            slots=slots, last_served={},
        )
        self.registry.approve_schedule_plan(
            1001, service_id, "2026-11", slots, chat_id=None, message_id=None,
        )
        text = "\n".join(personal_reminder_forecast(
            self.registry, 1001, service_id, self.month, self.now,
        ))
        self.assertIn("Мария", text)
        self.assertIn("15.11.2026 09:00 — запланировано", text)

    def test_database_error_propagates_instead_of_empty_success(self):
        with patch.object(self.registry, "get_schedule_plan", side_effect=RuntimeError("db")):
            with self.assertRaisesRegex(RuntimeError, "db"):
                self.forecast()

    def test_admin_access_and_read_only(self):
        self.rule()
        self.approve({"2026-11-15": {"Микшер": ["Алексей"]}})
        with self.registry._connection() as connection:
            before = list(connection.iterdump())
        with patch.object(self.registry, "ensure_member_contacts") as ensure:
            self.forecast()
        ensure.assert_not_called()
        with self.registry._connection() as connection:
            self.assertEqual(before, list(connection.iterdump()))
        with self.assertRaises(PermissionError):
            personal_reminder_forecast(
                self.registry, 2001, "sound", self.month, self.now,
            )

    def test_unbound_rule_is_not_hidden(self):
        rule = self.rule()
        with self.registry._connection() as connection:
            connection.execute("DELETE FROM responsible_contacts WHERE telegram_id = 2001")
        self.approve({})
        text = self.forecast()
        self.assertIn("Получатель без привязки", text)
        self.assertIn("нет привязки Telegram", text)
        self.assertFalse(self.registry.is_user_reminder_delivered(rule["id"], "2026-11-15"))

    def test_shared_eligibility(self):
        self.rule()
        rule = self.registry.list_service_user_reminder_schedules(1001, "sound")[0]
        assignments = {"Микшер": ["Алексей"]}
        self.assertIsNone(personal_reminder_block_reason(
            rule, assignments, event_found=True, approved=True,
        ))
        for key, value, reason in (
            ("service_enabled", False, "служение выключено"),
            ("schedule_enabled", False, "правило выключено"),
            ("notifications_enabled", False, "уведомления выключены"),
            ("name", None, "нет привязки Telegram к задаче"),
        ):
            with self.subTest(key=key):
                self.assertEqual(personal_reminder_block_reason(
                    {**rule, key: value}, assignments, event_found=True, approved=True,
                ), reason)
        self.assertEqual(personal_reminder_block_reason(
            rule, {"Микшер": {"names": ["Алексей"], "is_informational": True}},
            event_found=True, approved=True,
        ), "информационная задача — без личных уведомлений")

    def test_rule_wording(self):
        for days, words in ((0, "в день служения"), (1, "за 1 день"),
                            (2, "за 2 дня"), (6, "за 6 дней")):
            self.assertEqual(personal_rule_label(days, "09:00"), f"{words} в 09:00")

    def test_forecast_matches_delivery_on_same_fixtures(self):
        from bot.app import _process_user_reminder_schedule

        async def run():
            rule = self.rule()
            self.approve({"2026-11-15": {"Микшер": ["Алексей"]}})
            for scenario in ("disabled", "answered", "delivered", "scheduled"):
                with self.subTest(scenario=scenario):
                    self.registry.set_user_notifications_enabled(
                        2001, "sound", scenario != "disabled",
                    )
                    with patch.object(
                        self.registry, "get_confirmation",
                        return_value="confirmed" if scenario == "answered" else None,
                    ), patch.object(
                        self.registry, "is_user_reminder_delivered",
                        return_value=scenario == "delivered",
                    ), patch("bot.app._download_schedule", new=AsyncMock()) as download:
                        text = self.forecast()
                        self.assertEqual(
                            "— запланировано" in text, scenario == "scheduled",
                        )
                        bot = SimpleNamespace(send_message=AsyncMock())
                        await _process_user_reminder_schedule(
                            bot,
                            SimpleNamespace(admin_tg_id=1001, yandex_public_url="unused"),
                            Path(self.tmp.name), self.now.tzinfo, self.registry,
                            rule["id"], MagicMock(), MagicMock(),
                            run_date=date(2026, 11, 14),
                        )
                        self.assertEqual(
                            bot.send_message.await_count, int(scenario == "scheduled"),
                        )
                        download.assert_not_awaited()

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
