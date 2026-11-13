import asyncio
import hashlib
import html
import logging
import re
import sqlite3
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

from bot import app, handlers, schedule_plan
from bot.event_notes import (
    EventNoteResolutionError, resolve_event_note, source_sunday_for_event,
)
from bot.service_registry import ServiceRegistry
from bot.ui import plain_text, render_screen_chunks, visible_units


LOG = logging.getLogger("test-event-notes")
LOG.addHandler(logging.NullHandler())
LOG.propagate = False


class EventNotesTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name)
        self.registry = ServiceRegistry(self.path / "registry.sqlite3")
        self.registry.initialize(1001, -10001)
        self.registry.set_group_chat(1001, "sound", -10002, enabled=True)
        self.registry.set_responsible_contact(1001, "sound", "звук", "A", 501)

    def note(self, sunday="2026-11-01", targets=None):
        return self.registry.save_event_note_draft(
            1001, sunday, "После собрания общение <в зале>.", targets,
        )

    def plan(self, sunday="2026-11-01", offset=0, task="звук",
             service_id="sound", person="A"):
        slots = {sunday: {task: [person]}}
        tasks = [{"name": task, "needed": 1, "max": 1, "day": offset}]
        self.registry.save_schedule_draft(
            service_id, sunday[:7], run_id=None, dates=[sunday],
            tasks=tasks, slots=slots, last_served={},
        )
        return self.registry.approve_schedule_plan(
            1001, service_id, sunday[:7], slots, chat_id=None, message_id=None,
            approved_slot_days={sunday: {task: {person: offset}}},
        )

    def test_draft_published_snapshot_targets_and_unpublish(self):
        draft = self.note(targets=["sound"])
        self.assertEqual(self.registry.resolve_event_note("sound", "2026-11-01"), "")
        published = self.registry.publish_event_note(
            1001, "2026-11-01", expected_revision=draft["revision"],
        )
        self.assertEqual(self.registry.resolve_event_note("sound", "2026-11-01"), draft["draft_body"])
        self.assertEqual(self.registry.resolve_event_note("kitchen", "2026-11-01"), "")
        changed = self.registry.save_event_note_draft(
            1001, "2026-11-01", "Новый черновик", None,
            expected_revision=published["revision"],
        )
        self.assertEqual(self.registry.resolve_event_note("sound", "2026-11-01"), draft["draft_body"])
        self.registry.publish_event_note(1001, "2026-11-01", expected_revision=changed["revision"])
        self.assertEqual(self.registry.resolve_event_note("kitchen", "2026-11-01"), "Новый черновик")
        self.registry.unpublish_event_note(1001, "2026-11-01")
        self.assertEqual(self.registry.resolve_event_note("sound", "2026-11-01"), "")
        self.assertEqual(self.registry.get_event_note("2026-11-01")["draft_body"], "Новый черновик")

    def test_permissions_revision_and_reinitialization(self):
        draft = self.note()
        for change in (
            lambda: self.registry.save_event_note_draft(501, "2026-11-01", "text"),
            lambda: self.registry.publish_event_note(501, "2026-11-01"),
            lambda: self.registry.unpublish_event_note(501, "2026-11-01"),
        ):
            with self.assertRaises(PermissionError):
                change()
        self.registry.publish_event_note(1001, "2026-11-01")
        for change in (
            lambda: self.registry.publish_event_note(1001, "2026-11-01", expected_revision=draft["revision"]),
            lambda: self.registry.save_event_note_draft(
                1001, "2026-11-01", "stale", expected_revision=draft["revision"],
            ),
            lambda: self.registry.unpublish_event_note(1001, "2026-11-01", expected_revision=draft["revision"]),
        ):
            with self.assertRaisesRegex(ValueError, "изменилась"):
                change()
        self.registry.initialize(1001, -10001)
        self.assertTrue(self.registry.get_event_note("2026-11-01")["published"])

    def test_invalid_input_and_long_unicode_are_safe(self):
        for text in ("", " ", "a\0b", "a\tb", "a\x85b", "x" * 2001):
            with self.subTest(text=repr(text[:10])), self.assertRaises(ValueError):
                self.registry.save_event_note_draft(1001, "2026-11-01", text)
        for sunday in ("2026-11-02", "20261101", "bad"):
            with self.assertRaises(ValueError):
                self.note(sunday)
        for targets in ([], ["missing"]):
            with self.assertRaises(ValueError):
                self.note(targets=targets)
        self.registry.save_event_note_draft(1001, "2026-11-01", "🙂" * 2000)
        for chunk in render_screen_chunks("🙂" * 2000, limit=1000):
            self.assertLessEqual(visible_units(
                re.sub(r"</?b>|</?code>", "", html.unescape(chunk)),
            ), 1000)

    def test_only_approved_event_sunday_is_used_across_year_boundary(self):
        self.note("2027-01-03")
        self.registry.publish_event_note(1001, "2027-01-03")
        moment = date(2026, 12, 31)
        self.assertEqual(resolve_event_note(self.registry, "sound", moment, "звук"), "")
        self.plan("2027-01-03", -3)
        self.assertEqual(source_sunday_for_event(self.registry, "sound", moment, "звук"), "2027-01-03")
        self.assertIn("После собрания", resolve_event_note(self.registry, "sound", moment, "звук"))
        self.assertIn("После собрания", resolve_event_note(self.registry, "sound", moment))
        self.assertEqual(resolve_event_note(self.registry, "sound", moment, "другое"), "")
        # Current settings and a later draft never move the approved snapshot.
        self.registry.set_service_member_day(1001, "sound", "A", 2)
        self.assertEqual(source_sunday_for_event(self.registry, "sound", moment, "звук"), "2027-01-03")

    def test_ambiguous_event_is_reported_instead_of_guessing(self):
        self.plan("2026-11-29", 6)
        self.plan("2026-12-06", -1, "сборка")
        with self.assertRaises(EventNoteResolutionError):
            resolve_event_note(self.registry, "sound", date(2026, 12, 5))
        self.assertEqual(source_sunday_for_event(
            self.registry, "sound", date(2026, 12, 5), "звук",
        ), "2026-11-29")

    def test_publishing_never_changes_delivery_marks(self):
        reminder = self.registry.list_group_reminders("sound")[0]
        self.registry.mark_group_reminder_delivered(reminder["id"], "2026-11-01", "group:-10002")
        self.note()
        self.registry.publish_event_note(1001, "2026-11-01")
        self.registry.unpublish_event_note(1001, "2026-11-01")
        self.assertTrue(self.registry.is_group_reminder_delivered(
            reminder["id"], "2026-11-01", "group:-10002",
        ))

    def excel_notes(self):
        source = hashlib.sha256(b"unused").hexdigest()
        self.registry.save_schedule_notes(source, "test-digest", [
            ["assembly", "2026-11-01", "2я часть", "членское"],
            ["assembly", "2026-11-01", "Причастие", ""],
            ["assembly", "2026-11-08", "2я часть", "другая дата"],
            ["kitchen", "2026-11-01", "местная заметка", "не общая"],
        ])

    def test_excel_assembly_notes_fallback_preview_and_renderer_parity(self):
        self.plan()
        self.excel_notes()
        text = resolve_event_note(
            self.registry, "sound", date(2026, 11, 1),
            template="{event_notes}", public_url="unused",
        )
        self.assertIn("2я часть членское", text)
        self.assertIn("Причастие", text)
        self.assertNotIn("другая дата", text)
        self.assertNotIn("местная заметка", text)
        preview = self.preview()
        self.assertTrue(preview["has_data"])
        rendered = app._render_group_reminder(
            "{informational_notes}\n{event_notes}", {}, self.registry.get_service("sound"),
            {"звук": ["A"]}, {}, "01.11.2026", event_notes=text,
        )
        self.assertEqual(preview["text"], rendered)
        self.assertIn("2я часть членское", preview["text"])
        original = self.registry.get_schedule_notes
        reads = 0

        def fail_on_resolve(source):
            nonlocal reads
            reads += 1
            if reads == 2:
                raise sqlite3.OperationalError("test")
            return original(source)

        with patch.object(self.registry, "get_schedule_notes", side_effect=fail_on_resolve):
            broken = self.preview()
        self.assertFalse(broken["has_data"])
        self.assertIn("Не удалось прочитать заметки", broken["error"])

    def test_published_note_adds_to_excel_and_preserves_audience(self):
        self.plan()
        self.excel_notes()
        self.note(targets=["sound"])
        unpublished = resolve_event_note(
            self.registry, "sound", date(2026, 11, 1), public_url="unused",
        )
        self.assertIn("членское", unpublished)
        self.assertNotIn("После собрания", unpublished)
        self.registry.publish_event_note(1001, "2026-11-01")
        sound_note = resolve_event_note(
            self.registry, "sound", date(2026, 11, 1), public_url="unused",
        )
        self.assertIn("членское", sound_note)
        self.assertIn("После собрания", sound_note)
        self.registry.set_service_enabled(1001, "kitchen", True)
        self.registry.set_responsible_contact(1001, "kitchen", "в смене", "K", 502)
        self.plan(task="в смене", service_id="kitchen", person="K")
        kitchen_note = resolve_event_note(
            self.registry, "kitchen", date(2026, 11, 1), public_url="unused",
        )
        self.assertIn("членское", kitchen_note)
        self.assertNotIn("После собрания", kitchen_note)
        self.registry.save_event_note_draft(1001, "2026-11-01", "Только для Кухни", ["kitchen"])
        self.registry.publish_event_note(1001, "2026-11-01")
        sound_note = resolve_event_note(
            self.registry, "sound", date(2026, 11, 1), public_url="unused",
        )
        self.assertIn("членское", sound_note)
        self.assertNotIn("Только для Кухни", sound_note)
        kitchen_note = resolve_event_note(
            self.registry, "kitchen", date(2026, 11, 1), public_url="unused",
        )
        self.assertIn("членское", kitchen_note)
        self.assertIn("Только для Кухни", kitchen_note)
        self.registry.unpublish_event_note(1001, "2026-11-01")
        self.assertIn("членское", resolve_event_note(
            self.registry, "sound", date(2026, 11, 1), public_url="unused",
        ))

    def test_excel_note_uses_source_sunday_not_shifted_day_and_requires_approval(self):
        self.excel_notes()
        self.assertEqual(resolve_event_note(
            self.registry, "sound", date(2026, 11, 1), public_url="unused",
        ), "")
        self.plan(offset=3)
        self.assertIn("членское", resolve_event_note(
            self.registry, "sound", date(2026, 11, 4), "звук", public_url="unused",
        ))
        self.assertEqual(resolve_event_note(
            self.registry, "sound", date(2026, 11, 4), "звук", public_url="another-source",
        ), "")
        with patch.object(self.registry, "get_schedule_notes", side_effect=AssertionError("not used")):
            self.assertEqual(resolve_event_note(
                self.registry, "sound", date(2026, 11, 4),
                template="{task_assignments}", public_url="unused",
            ), "")

    def preview(self):
        reminder = self.registry.list_group_reminders("sound")[0]
        credentials = SimpleNamespace(admin_tg_id=1001, yandex_public_url="unused")
        with patch("bot.app.reminder_occurrence", return_value=(
            date(2026, 11, 1), date(2026, 10, 31),
        )), patch("bot.app._load_service_task_assignments", new=AsyncMock(return_value=(
            {"звук": ["A"]}, True,
        ))):
            return asyncio.run(app.get_group_reminder_preview(
                reminder["id"], credentials, self.path, ZoneInfo("UTC"),
                self.registry, LOG, body="{informational_notes}\n{event_notes}",
            ))

    def test_preview_delivery_resolver_parity_and_explicit_db_error(self):
        self.plan()
        self.excel_notes()
        self.note()
        self.registry.publish_event_note(1001, "2026-11-01")
        preview = self.preview()
        self.assertTrue(preview["has_data"])
        actual = app._render_group_reminder(
            "{informational_notes}\n{event_notes}", {}, self.registry.get_service("sound"),
            {"звук": ["A"]}, {}, "01.11.2026",
            event_notes=resolve_event_note(
                self.registry, "sound", date(2026, 11, 1), public_url="unused",
            ),
        )
        self.assertEqual(preview["text"], actual)
        self.assertIn("2я часть членское", preview["text"])
        self.assertIn("<в зале>", plain_text(actual))
        with patch.object(self.registry, "resolve_event_note", side_effect=sqlite3.OperationalError("test")):
            broken = self.preview()
        self.assertFalse(broken["has_data"])
        self.assertIn("Не удалось прочитать заметки", broken["error"])

    def test_old_templates_do_not_read_notes(self):
        with patch.object(self.registry, "get_schedule_plan", side_effect=AssertionError("unexpected read")):
            self.assertEqual(resolve_event_note(
                self.registry, "sound", date(2026, 11, 1), template="{task_assignments}",
            ), "")

    def test_excel_notes_group_and_personal_delivery_match_previews(self):
        self.plan()
        self.excel_notes()
        reminder = self.registry.list_group_reminders("sound")[0]
        self.registry.update_group_reminder(
            1001, reminder["id"], body="{event_notes}",
        )
        self.registry.set_service_personal_templates(
            1001, "sound", {"звук": "{name}\n{event_notes}"},
        )
        self.registry.set_user_notifications_enabled(501, "sound", True)
        text_id = self.registry.ensure_user_text(501, "sound", "звук")
        rule = self.registry.add_schedule(text_id, 1, "18:00")
        credentials = SimpleNamespace(admin_tg_id=1001, yandex_public_url="unused")
        bot = SimpleNamespace(send_message=AsyncMock())

        async def run():
            with patch("bot.app._load_service_task_assignments", new=AsyncMock(
                return_value=({"звук": ["A"]}, True),
            )), patch("bot.app.send_admin_reports", new=AsyncMock()):
                group_preview = await app.get_group_reminder_preview(
                    reminder["id"], credentials, self.path, ZoneInfo("UTC"),
                    self.registry, LOG,
                )
                await app._process_service_reminder(
                    bot, credentials, self.path, ZoneInfo("UTC"), self.registry,
                    "sound", reminder["id"], LOG, LOG,
                    run_date=date(2026, 11, 1) - timedelta(days=reminder["reminder_days"]),
                )
                await app._process_user_reminder_schedule(
                    bot, credentials, self.path, ZoneInfo("UTC"), self.registry,
                    rule["id"], LOG, LOG, run_date=date(2026, 10, 31),
                )
            with patch("bot.app._load_service_task_assignments", new=AsyncMock(
                return_value={"звук": ["A"]},
            )):
                personal_preview = await app._prepare_service_test_previews(
                    "sound", credentials, self.path, ZoneInfo("UTC"), self.registry, LOG,
                    group_template="{event_notes}",
                )
            return group_preview, personal_preview

        with patch("bot.app._next_sunday_in_tz", return_value=date(2026, 11, 1)), patch(
            "bot.app.reminder_occurrence",
            return_value=(date(2026, 11, 1), date(2026, 10, 31)),
        ):
            group_preview, personal_preview = asyncio.run(run())
        group_messages = [call for call in bot.send_message.await_args_list if call.args[0] == -10002]
        personal_messages = [call for call in bot.send_message.await_args_list if call.args[0] == 501]
        self.assertEqual(len(group_messages), 1)
        self.assertEqual(len(personal_messages), 1)
        self.assertEqual(group_messages[0].args[1], render_screen_chunks(group_preview["text"])[0])
        self.assertIn("2я часть членское", group_messages[0].args[1])
        expected = "🔔 Звук · звук · 01.11.2026\n" + personal_preview["personal_messages"][0]["text"]
        self.assertEqual(personal_messages[0].args[1], render_screen_chunks(expected)[0])
        self.assertIn("2я часть членское", personal_messages[0].args[1])

    def test_group_delivery_matches_preview_and_note_change_does_not_resend(self):
        self.plan()
        self.note()
        self.registry.publish_event_note(1001, "2026-11-01")
        reminder = self.registry.list_group_reminders("sound")[0]
        self.registry.update_group_reminder(
            1001, reminder["id"], body="{informational_notes}\n{event_notes}",
        )
        preview = self.preview()
        bot = SimpleNamespace(send_message=AsyncMock())
        credentials = SimpleNamespace(admin_tg_id=1001, yandex_public_url="unused")

        async def send():
            with patch("bot.app._load_service_task_assignments", new=AsyncMock(
                return_value=({"звук": ["A"]}, True),
            )), patch("bot.app.send_admin_reports", new=AsyncMock()):
                await app._process_service_reminder(
                    bot, credentials, self.path, ZoneInfo("UTC"), self.registry,
                    "sound", reminder["id"], LOG, LOG,
                    run_date=date(2026, 11, 1) - timedelta(days=reminder["reminder_days"]),
                )
        asyncio.run(send())
        group_messages = [call for call in bot.send_message.await_args_list if call.args[0] == -10002]
        self.assertEqual(len(group_messages), 1)
        expected = render_screen_chunks(preview["text"])[0]
        self.assertEqual(group_messages[0].args[1], expected)
        self.registry.save_event_note_draft(1001, "2026-11-01", "Изменено")
        self.registry.publish_event_note(1001, "2026-11-01")
        asyncio.run(send())
        self.assertEqual(len([
            call for call in bot.send_message.await_args_list if call.args[0] == -10002
        ]), 1)

    def test_personal_preview_matches_real_delivery_and_db_failure_is_not_delivered(self):
        self.plan()
        self.note()
        self.registry.publish_event_note(1001, "2026-11-01")
        self.registry.set_service_personal_templates(
            1001, "sound", {"звук": "{name}\n{event_notes}"},
        )
        self.registry.set_user_notifications_enabled(501, "sound", True)
        text_id = self.registry.ensure_user_text(501, "sound", "звук")
        rule = self.registry.add_schedule(text_id, 1, "18:00")
        credentials = SimpleNamespace(admin_tg_id=1001, yandex_public_url="unused")
        bot = SimpleNamespace(send_message=AsyncMock())

        async def run():
            with patch("bot.app._load_service_task_assignments", new=AsyncMock(
                return_value=({"звук": ["A"]}, True),
            )):
                await app._process_user_reminder_schedule(
                    bot, credentials, self.path, ZoneInfo("UTC"), self.registry,
                    rule["id"], LOG, LOG, run_date=date(2026, 10, 31),
                )
        with patch.object(self.registry, "resolve_event_note", side_effect=sqlite3.OperationalError("test")):
            asyncio.run(run())
        self.assertFalse(self.registry.is_user_reminder_delivered(rule["id"], "2026-11-01"))
        self.assertFalse(any(call.args[0] == 501 for call in bot.send_message.await_args_list))
        with patch("bot.app._next_sunday_in_tz", return_value=date(2026, 11, 1)), patch(
            "bot.app._load_service_task_assignments", new=AsyncMock(return_value={"звук": ["A"]}),
        ):
            preview = asyncio.run(app._prepare_service_test_previews(
                "sound", credentials, self.path, ZoneInfo("UTC"), self.registry, LOG,
            ))
        asyncio.run(run())
        delivered = next(call.args[1] for call in bot.send_message.await_args_list if call.args[0] == 501)
        expected = "🔔 Звук · звук · 01.11.2026\n" + preview["personal_messages"][0]["text"]
        self.assertEqual(delivered, render_screen_chunks(expected)[0])
        self.assertTrue(self.registry.is_user_reminder_delivered(rule["id"], "2026-11-01"))


class EventNoteHandlerTest(unittest.TestCase):
    def setUp(self):
        EventNotesTest.setUp(self)
        self.context = SimpleNamespace(
            service_registry=self.registry, admin_id=1001, group_chat_id=-10001,
            timezone=ZoneInfo("UTC"), interaction_logger=LOG, debug_logger=LOG,
            bot=SimpleNamespace(
                send_message=AsyncMock(return_value=SimpleNamespace(message_id=55)),
                delete_message=AsyncMock(), edit_message_reply_markup=AsyncMock(),
            ),
        )
        dispatcher = SimpleNamespace(include_router=MagicMock())
        handlers.register(dispatcher, self.context)
        admin_router = dispatcher.include_router.call_args_list[0].args[0]
        self.callback = next(item.callback for item in admin_router.callback_query.handlers
                             if item.callback.__name__ == "on_event_notes")
        self.input = next(item.callback for item in admin_router.message.handlers
                          if item.callback.__name__ == "on_event_note_text")
        self.message = SimpleNamespace(
            from_user=SimpleNamespace(id=1001), chat=SimpleNamespace(id=1001, type="private"),
            message_id=1, answer=AsyncMock(return_value=SimpleNamespace(message_id=22)),
        )

    def click(self, data, actor=1001):
        cb = SimpleNamespace(
            data=data, from_user=SimpleNamespace(id=actor),
            message=self.message, answer=AsyncMock(),
        )
        asyncio.run(self.callback(cb))
        return cb

    def button(self, prefix):
        markup = self.message.answer.await_args.kwargs["reply_markup"]
        return next(button.callback_data for row in markup.inline_keyboard
                    for button in row if button.callback_data.startswith(prefix))

    def test_full_ui_text_publish_stale_and_unpublish(self):
        self.click("evn:home")
        self.click(self.button("evn:day:"))
        sunday = self.button("evn:text:")
        self.click(sunday)
        self.message.text = "Новая заметка"
        asyncio.run(self.input(self.message))
        publish = self.button("evn:publish:")
        self.click(publish)
        confirm = self.button("evn:publishok:")
        self.click(confirm)
        repeated = self.click(confirm)
        self.assertIn("устарел", repeated.answer.await_args.args[0])
        self.click(self.button("evn:unpublish:"))
        self.click(self.button("evn:unpublishok:"))
        self.assertFalse(self.registry.list_event_notes()[0]["published"])
        forbidden = self.click("evn:home", actor=501)
        self.assertIn("администраторам", forbidden.answer.await_args.args[0])

    def test_concurrent_edit_cannot_publish_newer_unseen_text(self):
        self.click("evn:home")
        self.click(self.button("evn:day:"))
        self.click(self.button("evn:text:"))
        self.message.text = "Первый текст"
        asyncio.run(self.input(self.message))
        self.click(self.button("evn:publish:"))
        confirm = self.button("evn:publishok:")
        sunday = self.registry.list_event_notes()[0]["event_sunday"]
        self.registry.save_event_note_draft(1001, sunday, "Другой текст")
        response = self.click(confirm)
        self.assertIn("изменилась", response.answer.await_args.args[0])
        self.assertEqual(self.registry.resolve_event_note("sound", sunday), "")

    def test_target_selection_is_saved_to_draft_only(self):
        for service in self.registry.list_services():
            self.registry.set_service_enabled(1001, service["id"], True)
        self.click("evn:home")
        self.click(self.button("evn:day:"))
        self.click(self.button("evn:text:"))
        self.message.text = "Текст для служений"
        asyncio.run(self.input(self.message))
        self.click(self.button("evn:targets:"))
        self.click(self.button("evn:target:"))
        markup = self.message.answer.await_args.kwargs["reply_markup"]
        self.assertFalse(any(item.callback_data.startswith("ui_page:")
                             for row in markup.inline_keyboard for item in row))
        self.click(self.button("evn:page:"))
        self.click(self.button("evn:targetsok:"))
        note = self.registry.list_event_notes()[0]
        self.assertIsInstance(note["draft_targets"], list)
        self.assertFalse(note["published"])

    def test_disabled_saved_recipient_can_be_removed_but_not_selected_again(self):
        self.registry.set_service_enabled(1001, "kitchen", True)
        self.click("evn:home")
        self.click(self.button("evn:day:"))
        self.click(self.button("evn:text:"))
        self.message.text = "Текст для служений"
        asyncio.run(self.input(self.message))
        sunday = self.registry.list_event_notes()[0]["event_sunday"]
        self.registry.save_event_note_draft(
            1001, sunday, "Текст для служений", ["sound", "kitchen"],
        )
        self.registry.set_service_enabled(1001, "sound", False)
        self.click("evn:home")
        self.click(self.button("evn:day:"))
        self.click(self.button("evn:targets:"))
        markup = self.message.answer.await_args.kwargs["reply_markup"]
        remove = next(
            button for row in markup.inline_keyboard for button in row
            if button.callback_data.endswith(":sound")
        )
        self.assertIn("недоступно", remove.text)
        old_button = remove.callback_data
        self.click(old_button)
        markup = self.message.answer.await_args.kwargs["reply_markup"]
        self.assertFalse(any(
            button.callback_data.endswith(":sound")
            for row in markup.inline_keyboard for button in row
        ))
        forged = self.button("evn:target:").rsplit(":", 1)[0] + ":sound"
        rejected = self.click(forged)
        self.assertIn("недоступно", rejected.answer.await_args.args[0])
        self.click(self.button("evn:targetsok:"))
        self.assertEqual(self.registry.get_event_note(sunday)["draft_targets"], ["kitchen"])
        self.click(self.button("evn:text:"))
        self.message.text = "Исправленный текст"
        asyncio.run(self.input(self.message))
        self.assertEqual(self.registry.get_event_note(sunday)["draft_body"], "Исправленный текст")
