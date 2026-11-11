import asyncio
import copy
import logging
import sqlite3
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from bot import app
from bot.assignment_fields import (
    AssignmentFieldResolutionError, assignment_field, resolve_assignment_fields,
)
from bot.event_notes import EventNoteResolutionError
from bot.service_registry import (
    ADMIN_REPORT_FIELDS, GENERAL_GROUP_TEMPLATE_FIELDS, PERSONAL_TEMPLATE_FIELDS,
    ServiceRegistry, validate_notification_template,
)
from bot.ui import plain_text, render_screen_chunks


LOG = logging.getLogger("test-assignment-fields")
SUNDAY = date(2027, 1, 3)
TZ = ZoneInfo("Europe/Moscow")


class AssignmentFieldsTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name)
        self.registry = ServiceRegistry(self.path / "registry.sqlite3")
        self.registry.initialize(1001, -10001)
        self.registry.set_group_chat(1001, "sound", -10002, enabled=True)
        self.registry.set_service_task_enabled(1001, "sound", "спевка", True)
        for service, task, name, user in (
            ("sound", "звук", "A", 501), ("sound", "сборка", "B", 502),
            ("sound", "спевка", "C", 503), ("order", "старший", "D", 504),
            ("order", "в смене", "E", 505),
        ):
            self.registry.set_responsible_contact(1001, service, task, name, user)
        self.plan("sound", {"звук": ["A"], "сборка": ["B"], "спевка": ["C"]},
                  {"спевка": -3})
        self.plan("order", {"старший": ["D"], "в смене": ["E"]})
        self.credentials = SimpleNamespace(admin_tg_id=1001, yandex_public_url="unused")

    def plan(self, service, slots, offsets=None, *, sunday=SUNDAY, approve=True):
        offsets = offsets or {}
        key = sunday.isoformat()
        tasks = [
            {"name": name, "needed": 1, "max": 1, "day": offsets.get(name, 0)}
            for name in slots
        ]
        self.registry.save_schedule_draft(
            service, key[:7], run_id=None, dates=[key], tasks=tasks,
            slots={key: slots}, last_served={},
        )
        if approve:
            return self.registry.approve_schedule_plan(
                1001, service, key[:7], {key: slots},
                chat_id=None, message_id=None,
                approved_slot_days={
                    key: {task: {person: offsets.get(task, 0) for person in people}
                          for task, people in slots.items()},
                },
            )

    def resolve(self, template="{week_task_assignments}", *,
                cutoff=date(2026, 12, 31), event=SUNDAY, task=None):
        return resolve_assignment_fields(
            self.registry, "sound", event, template, cutoff, task,
        )

    def test_week_uses_actual_dates_today_and_order_without_mutation(self):
        before = copy.deepcopy(self.registry.get_schedule_plan("sound", "2027-01"))
        text = plain_text(self.resolve().week)
        self.assertIn("спевка — C — 31.12.2026", text)
        self.assertIn("звук — A — 03.01.2027", text)
        self.assertLess(text.index("звук"), text.index("сборка"))
        self.assertNotIn("спевка", plain_text(self.resolve(cutoff=date(2027, 1, 1)).week))
        self.assertEqual(self.resolve(cutoff=date(2027, 1, 4)).week, "")
        self.assertEqual(self.registry.get_schedule_plan("sound", "2027-01"), before)

    def test_week_in_task_reminder_includes_all_local_tasks(self):
        text = self.resolve(event=date(2026, 12, 31), task="спевка").week
        for name in ("звук", "сборка", "спевка"):
            self.assertIn(name, text)

    def test_approved_snapshot_ignores_draft_and_changed_personal_day(self):
        expected = self.resolve()
        self.plan("sound", {"звук": ["B"], "спевка": []}, approve=False)
        self.registry.set_service_member_day(1001, "sound", "C", 2)
        self.registry.set_service_task_enabled(1001, "sound", "спевка", False)
        self.assertEqual(self.resolve(), expected)

    def test_unassigned_hidden_and_unbound_person_still_shown(self):
        self.plan("sound", {"звук": ["Guest"], "сборка": [], "спевка": []})
        text = plain_text(self.resolve().week)
        self.assertIn("звук — Guest — 03.01.2027", text)
        self.assertNotIn("сборка", text)
        self.assertNotIn("спевка", text)
        self.assertNotIn("не назначен", text)

    def test_other_services_all_or_selected_tasks_and_no_draft_fallback(self):
        all_fields = assignment_field("order")
        one_field = assignment_field("order", "старший")
        result = self.resolve(all_fields + "\n" + one_field)
        self.assertIn("в смене", result.services["order"])
        one = next(value for key, value in result.services.items() if ":" in key)
        self.assertIn("старший", one)
        self.assertNotIn("в смене", one)
        self.assertEqual(self.resolve(assignment_field("kitchen")).services, {"kitchen": ""})
        self.assertIn("нет утверждённого", self.resolve(assignment_field("kitchen")).warnings[0])

    def test_namesakes_keep_exact_identity_and_safe_mentions(self):
        self.registry.set_responsible_contact(
            1001, "order", "старший", "Same <name>", 601, username="first",
        )
        self.registry.set_responsible_contact(
            1001, "order", "в смене", "Same <name>", 602, username="second",
        )
        with self.registry._connection() as connection:
            connection.execute(
                "UPDATE responsible_contacts SET telegram_id = 601, username = 'first' "
                "WHERE service_id = 'order' AND task = 'старший' AND name = 'Same <name>'",
            )
        people = {
            person["telegram_id"]: person["key"]
            for person in self.registry.readiness_people("order")
        }
        self.plan("order", {
            "старший": [people[601]], "в смене": [people[602]],
        })
        value = next(iter(self.resolve(
            assignment_field("order", "старший"),
        ).services.values()))
        self.assertIn("LINK:601:", value)
        self.assertNotIn("LINK:602:", value)
        rendered = render_screen_chunks(value)[0]
        self.assertIn("&lt;name&gt;", rendered)
        self.assertIn("(@first)", rendered)
        self.assertNotIn("@second", rendered)

    def test_post_sunday_and_frozen_slot_dates_stay_in_same_block(self):
        self.plan("sound", {"звук": ["A"], "спевка": ["C"]}, {"спевка": 2})
        value = self.resolve(cutoff=date(2027, 1, 4))
        self.assertNotIn("звук", value.week)
        self.assertIn("05.01.2027", value.week)
        self.assertEqual(value.week, self.resolve(
            event=date(2027, 1, 5), task="спевка", cutoff=date(2027, 1, 4),
        ).week)

    def test_unapproved_source_appearing_changes_state_but_unrelated_source_does_not(self):
        template = assignment_field("kitchen")
        before = self.resolve(template).state
        self.plan("order", {"старший": ["D"], "в смене": []})
        self.assertEqual(self.resolve(template).state, before)
        self.plan("kitchen", {"старший": ["Guest"]})
        self.assertNotEqual(self.resolve(template).state, before)

    def test_replacements_and_midnight_change_state(self):
        template = assignment_field("order", "старший")
        before = self.resolve(template)
        self.registry.set_replacement(
            "order", SUNDAY.isoformat(), "старший", "D", "E",
            telegram_id=505, username="", source="admin", created_by=1001,
        )
        after = self.resolve(template)
        self.assertIn("LINK:505:", next(iter(after.services.values())))
        self.assertNotEqual(after.state, before.state)
        self.assertNotEqual(after.state, self.resolve(
            template, cutoff=date(2027, 1, 1),
        ).state)

    def test_overlapping_week_dates_keep_replacements_in_their_source_block(self):
        first, second = "2027-01-03", "2027-01-10"
        slots = {first: {"старший": ["D"]}, second: {"старший": ["E"]}}
        self.registry.save_schedule_draft(
            "order", "2027-01", run_id=None, dates=[first, second],
            tasks=[{"name": "старший", "needed": 1, "day": 2}],
            slots=slots, last_served={},
        )
        self.registry.approve_schedule_plan(
            1001, "order", "2027-01", slots, chat_id=None, message_id=None,
            approved_slot_days={
                first: {"старший": {"D": 2}}, second: {"старший": {"E": -5}},
            },
        )
        for old, new in (
            ("E", "Other"), ("Other", "Other final"),
            ("D", "Substitute"), ("Substitute", "Final"),
        ):
            self.registry.set_replacement(
                "order", "2027-01-05", "старший", old, new, telegram_id=None,
                username="", source="admin", created_by=1001,
            )
        value = next(iter(self.resolve(assignment_field("order")).services.values()))
        self.assertIn("старший — Final — 05.01.2027", plain_text(value))
        self.assertNotIn("Other", value)
        self.assertNotIn("Substitute", value)
        self.assertNotIn("E —", plain_text(value))

    def test_saved_replacement_survives_reapproval_without_old_person(self):
        self.registry.set_replacement(
            "order", SUNDAY.isoformat(), "старший", "D", "Substitute",
            telegram_id=None, username="", source="admin", created_by=1001,
        )
        self.plan("order", {"старший": ["E"]})
        value = next(iter(self.resolve(assignment_field("order")).services.values()))
        self.assertIn("Substitute", value)
        self.assertIn("E —", plain_text(value))

    def test_other_month_replacement_chain_does_not_leak_into_selected_week(self):
        self.plan("order", {"старший": ["D"]}, {"старший": -3})
        self.plan("order", {"старший": ["E"]}, {"старший": 4}, sunday=date(2026, 12, 27))
        for old, new in (("E", "Other"), ("Other", "Other final")):
            self.registry.set_replacement(
                "order", "2026-12-31", "старший", old, new, telegram_id=None,
                username="", source="admin", created_by=1001,
            )
        value = next(iter(self.resolve(assignment_field("order")).services.values()))
        self.assertIn("старший — D — 31.12.2026", plain_text(value))
        self.assertNotIn("Other", value)

    def test_namesakes_in_overlapping_blocks_fail_explicitly_for_ambiguous_replacement(self):
        people = [
            {"key": f"Twin · {user}", "name": "Twin", "telegram_id": user,
             "tasks": ["старший"], "username": ""}
            for user in (601, 602)
        ]
        first, second = "2027-01-03", "2027-01-10"
        slots = {
            first: {"старший": [people[0]["key"]]},
            second: {"старший": [people[1]["key"]]},
        }
        self.registry.save_schedule_draft(
            "order", "2027-01", run_id=None, dates=[first, second],
            tasks=[{"name": "старший", "needed": 1, "day": 2}],
            slots=slots, last_served={},
        )
        self.registry.approve_schedule_plan(
            1001, "order", "2027-01", slots, chat_id=None, message_id=None,
            approved_slot_days={
                first: {"старший": {people[0]["key"]: 2}},
                second: {"старший": {people[1]["key"]: -5}},
            },
        )
        self.registry.set_replacement(
            "order", "2027-01-05", "старший", "Twin", "Guest", telegram_id=None,
            username="", source="admin", created_by=1001,
        )
        reader = self.registry.readiness_people
        with patch.object(
            self.registry, "readiness_people",
            side_effect=lambda source: people if source == "order" else reader(source),
        ):
            with self.assertRaisesRegex(AssignmentFieldResolutionError, "тёзки"):
                self.resolve(assignment_field("order"))
            preview = self.preview(assignment_field("order"))
        self.assertFalse(preview["has_data"])
        self.assertIn("тёзки", preview["error"])

    def test_plain_namesake_assignment_without_replacement_remains_unlinked(self):
        people = [
            {"key": f"Twin · {user}", "name": "Twin", "telegram_id": user,
             "tasks": ["старший"], "username": ""}
            for user in (601, 602)
        ]
        self.plan("order", {"старший": ["Twin"]})
        reader = self.registry.readiness_people
        with patch.object(
            self.registry, "readiness_people",
            side_effect=lambda source: people if source == "order" else reader(source),
        ):
            result = self.resolve(assignment_field("order"))
            preview = self.preview(assignment_field("order"))
        self.assertIn("старший — Twin — 03.01.2027", result.services["order"])
        self.assertNotIn("LINK:", result.services["order"])
        self.assertTrue(preview["has_data"], preview)

    def test_strict_group_only_grammar_and_stable_task_token(self):
        field = assignment_field("order", "старший")
        for allowed in (GENERAL_GROUP_TEMPLATE_FIELDS,):
            validate_notification_template(field, allowed, "Test")
            validate_notification_template("{week_task_assignments}", allowed, "Test")
        for allowed in (ADMIN_REPORT_FIELDS, PERSONAL_TEMPLATE_FIELDS):
            for value in (field, "{week_task_assignments}"):
                with self.assertRaises(ValueError):
                    validate_notification_template(value, allowed, "Test")
        for value in (
            "{service_assignments[order].name}", "{service_assignments[order]!r}",
            "{service_assignments[order]:>5}", "{service_assignments[order:bad]}",
            "{service_assignments[order][x]}", "{service_assignments}",
        ):
            with self.assertRaises(ValueError):
                validate_notification_template(value, GENERAL_GROUP_TEMPLATE_FIELDS, "Test")
        with patch("bot.service_registry.task_token", return_value="0" * 10):
            with self.assertRaisesRegex(ValueError, "неоднозначна"):
                self.resolve("{service_assignments[order:" + "0" * 10 + "]}")
        with self.assertRaisesRegex(ValueError, "удалена"):
            self.resolve(assignment_field("order", "Нет такой задачи"))

    def test_old_fields_do_not_read_database(self):
        with patch.object(self.registry, "get_schedule_plan", side_effect=AssertionError):
            self.assertEqual(self.resolve("{task_assignments}").state, ())

    def test_errors_are_not_success_shaped_empty_fields(self):
        with patch.object(
            self.registry, "get_schedule_plan", side_effect=sqlite3.OperationalError("test"),
        ), self.assertRaises(sqlite3.Error):
            self.resolve()
        with self.assertRaisesRegex(ValueError, "не существует"):
            self.resolve(assignment_field("missing"))
        self.plan("sound", {"звук": ["A"]}, {"звук": 6}, sunday=date(2026, 12, 27))
        self.plan("sound", {"сборка": ["B"]}, {"сборка": -1})
        with self.assertRaises(EventNoteResolutionError):
            self.resolve(event=date(2027, 1, 2))

    def preview(self, body, *, manual=False):
        reminder = self.registry.list_group_reminders("sound")[0]
        with patch("bot.app.reminder_occurrence", return_value=(
            SUNDAY, date(2026, 12, 31),
        )), patch("bot.app.datetime") as clock:
            clock.now.return_value = datetime(2027, 1, 1, 9, tzinfo=TZ)
            return asyncio.run(app.get_group_reminder_preview(
                reminder["id"], self.credentials, self.path, TZ, self.registry, LOG,
                body=body, manual=manual,
            ))

    def test_scheduled_preview_uses_send_date_manual_uses_today(self):
        body = "{task_assignments}\n{week_task_assignments}"
        scheduled = self.preview(body)
        manual = self.preview(body, manual=True)
        self.assertTrue(scheduled["has_data"], scheduled)
        self.assertTrue(manual["has_data"], manual)
        self.assertIn("спевка", scheduled["text"])
        self.assertNotIn("спевка", manual["text"])
        self.assertIn("звук", manual["text"])

    def test_preview_surfaces_missing_source_and_database_errors(self):
        result = self.preview("Text\n" + assignment_field("kitchen"))
        self.assertTrue(result["has_data"])
        self.assertEqual(result["text"], "Text\n")
        self.assertTrue(result["warnings"])
        with patch("bot.app.resolve_assignment_fields", side_effect=sqlite3.OperationalError("test")):
            result = self.preview("{week_task_assignments}")
        self.assertFalse(result["has_data"])
        self.assertIsNotNone(result["error"])

    def test_template_preview_uses_same_fields_and_exposes_warnings(self):
        template = "{week_task_assignments}\n" + assignment_field("kitchen")
        with patch("bot.app._next_sunday_in_tz", return_value=SUNDAY), patch(
            "bot.app.datetime",
        ) as clock:
            clock.now.return_value = datetime(2026, 12, 31, 9, tzinfo=TZ)
            result = asyncio.run(app.get_template_preview(
                "sound", "default", template, self.credentials, self.path,
                TZ, self.registry, LOG,
            ))
        self.assertTrue(result["has_data"], result)
        self.assertEqual(result["text"], self.resolve(template).week + "\n")
        self.assertTrue(result["warnings"])

    def test_scheduled_send_aborts_without_delivery_mark_if_related_source_changes(self):
        from bot.assignment_fields import AssignmentFields

        reminder = self.registry.list_group_reminders("sound")[0]
        template = assignment_field("order")
        self.registry.update_group_reminder(1001, reminder["id"], days=3, body=template)
        bot = SimpleNamespace(send_message=AsyncMock())
        values = self.resolve(template)
        stale = AssignmentFields(state=("changed",))
        with patch("bot.app.datetime") as clock, patch(
            "bot.app.resolve_assignment_fields", side_effect=[values, stale],
        ), patch("bot.app._next_sunday_in_tz", return_value=SUNDAY):
            clock.now.return_value = datetime(2026, 12, 31, 18, tzinfo=TZ)
            asyncio.run(app._process_service_reminder(
                bot, self.credentials, self.path, TZ, self.registry,
                "sound", reminder["id"], LOG, LOG, run_date=date(2026, 12, 31),
            ))
        self.assertFalse(any(
            call.args[0] == -10002 for call in bot.send_message.await_args_list
        ))
        self.assertFalse(self.registry.is_group_reminder_delivered(
            reminder["id"], SUNDAY.isoformat(), "group:-10002",
        ))

    def test_mock_group_delivery_matches_preview_and_never_resends_on_related_change(self):
        reminder = self.registry.list_group_reminders("sound")[0]
        template = "{week_task_assignments}\n" + assignment_field("order", "старший")
        self.registry.update_group_reminder(
            1001, reminder["id"], days=3, body=template,
        )
        bot = SimpleNamespace(send_message=AsyncMock())
        now = datetime(2026, 12, 31, 18, tzinfo=TZ)

        async def run():
            with patch("bot.app.datetime") as clock, patch(
                "bot.app.send_admin_reports", new=AsyncMock(),
            ), patch("bot.app._next_sunday_in_tz", return_value=SUNDAY):
                clock.now.return_value = now
                preview = await app.get_group_reminder_preview(
                    reminder["id"], self.credentials, self.path, TZ, self.registry, LOG,
                )
                await app._process_service_reminder(
                    bot, self.credentials, self.path, TZ, self.registry,
                    "sound", reminder["id"], LOG, LOG, run_date=now.date(),
                )
                return preview

        preview = asyncio.run(run())
        sends = [call for call in bot.send_message.await_args_list if call.args[0] == -10002]
        self.assertEqual(len(sends), 1)
        self.assertEqual(sends[0].args[1], render_screen_chunks(preview["text"])[0])
        self.registry.set_replacement(
            "order", SUNDAY.isoformat(), "старший", "D", "E",
            telegram_id=505, username="", source="admin", created_by=1001,
        )
        asyncio.run(run())
        self.assertEqual(len([
            call for call in bot.send_message.await_args_list if call.args[0] == -10002
        ]), 1)
