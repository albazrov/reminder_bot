"""Этап 3.2: ответы на личные напоминания и отчёт админам служения."""
import asyncio
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

from bot.admin_report import (
    build_report_text,
    fit_one_screen,
    refresh_admin_reports,
    send_admin_reports,
)
from bot.handlers import register
from bot.notifier import build_personal_answer_kb
from bot.service_registry import ServiceRegistry, task_token
from bot.ui import render_screen_chunks

SUPER = 67890
ADMIN = 12345
MEMBER = 4004
FUTURE = date(2099, 1, 4)
ASSIGNMENTS = {"звук": {"names": ["Алексей"]}, "сборка": {"names": ["Борис"]}}


class RegistryTestCase(unittest.TestCase):
    def setUp(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.registry = ServiceRegistry(Path(temp_dir.name) / "r.sqlite3")
        self.registry.initialize(SUPER, -100)
        self.registry.set_responsible_contact(
            SUPER, "sound", "звук", "Алексей", MEMBER, "@alex"
        )


class ConfirmationRegistryTest(RegistryTestCase):
    def test_answer_is_stored_and_change_detected(self):
        iso = FUTURE.isoformat()
        self.assertEqual(
            self.registry.set_confirmation("sound", iso, "звук", MEMBER, "confirmed"),
            ("Алексей", True),
        )
        self.assertEqual(
            self.registry.set_confirmation("sound", iso, "звук", MEMBER, "confirmed"),
            ("Алексей", False),
        )
        self.registry.set_confirmation("sound", iso, "звук", MEMBER, "declined")
        self.assertEqual(
            self.registry.get_confirmation("sound", iso, "звук", MEMBER), "declined"
        )
        self.assertEqual(
            self.registry.list_confirmations("sound", iso),
            {("звук", MEMBER): "declined"},
        )

    def test_only_linked_person_can_answer(self):
        with self.assertRaises(PermissionError):
            self.registry.set_confirmation(
                "sound", FUTURE.isoformat(), "сборка", MEMBER, "confirmed"
            )

    def test_task_token_resolves_only_linked_tasks(self):
        self.assertEqual(
            self.registry.linked_task_by_token("sound", MEMBER, task_token("звук")),
            "звук",
        )
        self.assertIsNone(
            self.registry.linked_task_by_token("sound", MEMBER, task_token("сборка"))
        )
        self.assertIsNone(
            self.registry.linked_task_by_token("sound", 5555, task_token("звук"))
        )

    def test_report_template_validation(self):
        with self.assertRaises(ValueError):
            self.registry.set_admin_report_template(SUPER, "sound", "{unknown}")
        self.registry.set_admin_report_template(SUPER, "sound", "{service_name}")
        self.assertEqual(
            self.registry.get_service("sound")["admin_report_template"],
            "{service_name}",
        )
        self.registry.set_admin_report_template(SUPER, "sound", None)
        self.assertIsNone(self.registry.get_service("sound")["admin_report_template"])


class ReportTextTest(unittest.TestCase):
    def test_default_report_lists_delivery_and_answers(self):
        text = build_report_text(
            None, "Звук", FUTURE, ASSIGNMENTS,
            {("звук", "Алексей"): {"telegram_id": MEMBER}},
            lambda telegram_id: True,
            {("звук", MEMBER): "confirmed"},
        )
        self.assertIn("📋 Отчёт · Звук · 04.01.2099", text)
        self.assertIn("✅ Алексей (звук) — уведомления включены", text)
        self.assertIn("⚪ Борис (сборка) — тг аккаунт к боту не подключен", text)
        self.assertNotIn("⚪ Борис (сборка) — не привязан", text)
        self.assertIn("✅ подтвердил(а) — Алексей (звук)", text)
        self.assertIn("⏳ нет ответа — Борис (сборка)", text)

    def test_senior_precedes_shift_in_admin_report(self):
        assignments = {
            "в смене": {"names": ["Алексей"]},
            "старший": {"names": ["Борис"]},
        }
        text = build_report_text(
            None,
            "Орг. команда",
            FUTURE,
            assignments,
            {},
            lambda telegram_id: True,
            {},
        )

        self.assertLess(text.index("⭐ старший"), text.index("👥 в смене"))
        self.assertLess(
            text.index("Борис (старший)"),
            text.index("Алексей (в смене)"),
        )

    def test_custom_template_and_muted_person(self):
        text = build_report_text(
            "{service_name}: {confirmation_status} | {delivery_status}",
            "Звук", FUTURE, {"звук": {"names": ["Алексей"]}},
            {("звук", "Алексей"): {"telegram_id": MEMBER}},
            lambda telegram_id: False,
            {("звук", MEMBER): "declined"},
        )
        self.assertEqual(
            text,
            "Звук: 🚫 не сможет — Алексей (звук) | "
            "🔕 Алексей (звук) — уведомления выключены",
        )


class ReportAnswersTest(RegistryTestCase):
    def test_rebinding_name_does_not_inherit_old_answer(self):
        iso = FUTURE.isoformat()
        self.registry.set_confirmation("sound", iso, "звук", MEMBER, "confirmed")
        self.registry.set_responsible_contact(
            SUPER, "sound", "звук", "Алексей", 7007, "@other"
        )
        text = build_report_text(
            None, "Звук", FUTURE, {"звук": {"names": ["Алексей"]}},
            {("звук", "Алексей"): {"telegram_id": 7007}},
            lambda telegram_id: True,
            self.registry.list_confirmations("sound", iso),
        )
        self.assertIn("⏳ нет ответа — Алексей (звук)", text)

    def test_long_report_keeps_answers_visible(self):
        names = [f"Участник с длинным именем {index}" for index in range(300)]
        text = build_report_text(
            None, "Звук", FUTURE, {"звук": {"names": names}},
            {("звук", names[-1]): {"telegram_id": MEMBER}},
            lambda telegram_id: True,
            {("звук", MEMBER): "confirmed"},
            limit=4096,
        )
        self.assertEqual(len(render_screen_chunks(text, limit=4096)), 1)
        self.assertIn("✅ Подтверждения:", text)
        self.assertIn("сокращено", text)


class FitOneScreenTest(unittest.TestCase):
    def test_long_report_is_cut_to_one_screen(self):
        text = "\n".join(f"Строка {index}" for index in range(2000))
        fitted = fit_one_screen(text, 4096)
        self.assertEqual(len(render_screen_chunks(fitted, limit=4096)), 1)
        self.assertTrue(fitted.startswith("Строка 0"))
        self.assertIn("отчёт сокращён", fitted)
        self.assertEqual(fit_one_screen("Коротко", 4096), "Коротко")


class ReportDeliveryTest(RegistryTestCase):
    def test_report_sent_once_then_edited(self):
        async def run():
            self.registry.add_service_admin(SUPER, "sound", ADMIN)
            bot = SimpleNamespace(
                send_message=AsyncMock(return_value=SimpleNamespace(message_id=5)),
                edit_message_text=AsyncMock(),
            )
            service = self.registry.get_service("sound")
            await send_admin_reports(
                bot, self.registry, service, FUTURE, ASSIGNMENTS, SUPER,
                MagicMock(), 4096,
            )
            self.assertEqual(bot.send_message.await_args.args[0], ADMIN)
            self.assertEqual(
                self.registry.list_admin_reports("sound", FUTURE.isoformat()),
                [{"admin_id": ADMIN, "message_id": 5, "assignments": ASSIGNMENTS}],
            )
            # Повторное напоминание той же даты редактирует отчёт.
            await send_admin_reports(
                bot, self.registry, service, FUTURE, ASSIGNMENTS, SUPER,
                MagicMock(), 4096,
            )
            self.assertEqual(bot.send_message.await_count, 1)
            self.assertEqual(bot.edit_message_text.await_args.kwargs["message_id"], 5)

            self.registry.set_confirmation(
                "sound", FUTURE.isoformat(), "звук", MEMBER, "confirmed"
            )
            await refresh_admin_reports(
                bot, self.registry, "sound", FUTURE, SUPER, MagicMock(), 4096
            )
            self.assertIn(
                "подтвердил(а) — Алексей",
                bot.edit_message_text.await_args.args[0],
            )

        asyncio.run(run())

    def test_removed_admin_stops_getting_updates_and_deleted_report_resent(self):
        async def run():
            other = 23456
            self.registry.add_service_admin(SUPER, "sound", ADMIN)
            self.registry.add_service_admin(SUPER, "sound", other)
            bot = SimpleNamespace(
                send_message=AsyncMock(return_value=SimpleNamespace(message_id=5)),
                edit_message_text=AsyncMock(),
            )
            service = self.registry.get_service("sound")
            await send_admin_reports(
                bot, self.registry, service, FUTURE, ASSIGNMENTS, SUPER,
                MagicMock(), 4096,
            )
            self.registry.remove_service_admin(SUPER, "sound", other)
            # Админ удалил отчёт: правка не удаётся — отчёт отправляется заново.
            bot.edit_message_text.side_effect = RuntimeError("message to edit not found")
            bot.send_message.reset_mock()
            bot.send_message.return_value = SimpleNamespace(message_id=9)
            await refresh_admin_reports(
                bot, self.registry, "sound", FUTURE, SUPER, MagicMock(), 4096
            )
            edited = [
                call.kwargs["chat_id"]
                for call in bot.edit_message_text.await_args_list
            ]
            self.assertNotIn(other, edited)
            self.assertEqual(
                [call.args[0] for call in bot.send_message.await_args_list],
                [ADMIN],
            )
            self.assertEqual(
                self.registry.list_admin_reports("sound", FUTURE.isoformat()),
                [{"admin_id": ADMIN, "message_id": 9, "assignments": ASSIGNMENTS}],
            )

        asyncio.run(run())

    def test_without_service_admins_report_goes_to_super_admin(self):
        async def run():
            bot = SimpleNamespace(
                send_message=AsyncMock(return_value=SimpleNamespace(message_id=7)),
                edit_message_text=AsyncMock(),
            )
            await send_admin_reports(
                bot, self.registry, self.registry.get_service("sound"), FUTURE,
                ASSIGNMENTS, SUPER, MagicMock(), 4096,
            )
            self.assertEqual(bot.send_message.await_args.args[0], SUPER)

        asyncio.run(run())


class PersonalAnswerHandlerTest(RegistryTestCase):
    def setUp(self):
        super().setUp()
        self.context = SimpleNamespace(
            interaction_logger=MagicMock(),
            debug_logger=MagicMock(),
            admin_id=SUPER,
            group_chat_id=-100,
            timezone=ZoneInfo("UTC"),
            service_registry=self.registry,
            bot=SimpleNamespace(
                send_message=AsyncMock(),
                delete_message=AsyncMock(),
                edit_message_text=AsyncMock(),
            ),
            on_assignment_declined=AsyncMock(),
        )
        dispatcher = SimpleNamespace(include_router=MagicMock())
        register(dispatcher, self.context)
        routers = [call.args[0] for call in dispatcher.include_router.call_args_list]
        self.admin_router = routers[0]
        self.handlers = {
            handler.callback.__name__: handler.callback
            for router in routers
            for handler in router.callback_query.handlers
        }

    def _press(self, data, user_id=MEMBER):
        callback = SimpleNamespace(
            data=data,
            from_user=SimpleNamespace(id=user_id),
            message=SimpleNamespace(
                text="Напоминание", edit_text=AsyncMock(),
                chat=SimpleNamespace(type="private", id=user_id),
            ),
            answer=AsyncMock(),
        )
        asyncio.run(self.handlers["on_personal_answer"](callback))
        return callback

    def _button(self, label, event_date=FUTURE):
        keyboard = build_personal_answer_kb("sound", event_date, "звук")
        return next(
            button.callback_data
            for button in keyboard.inline_keyboard[0]
            if button.text == label
        )

    def test_confirm_is_saved_and_message_marked(self):
        callback = self._press(self._button("✅ Принял"))
        self.assertEqual(
            self.registry.get_confirmation(
                "sound", FUTURE.isoformat(), "звук", MEMBER
            ),
            "confirmed",
        )
        self.assertIn("✅ Подтверждено", callback.message.edit_text.await_args.args[0])
        self.context.on_assignment_declined.assert_not_awaited()

    def test_decline_notifies_admins_and_starts_replacement(self):
        self.registry.add_service_admin(SUPER, "sound", ADMIN)
        self._press(self._button("🚫 Не смогу"))
        self.context.on_assignment_declined.assert_awaited_once_with(
            "sound", FUTURE, "звук", "Алексей", telegram_id=MEMBER
        )
        notices = [
            call.args for call in self.context.bot.send_message.await_args_list
            if call.args[0] == ADMIN
        ]
        self.assertEqual(len(notices), 1)
        self.assertIn("Алексей не сможет", notices[0][1])
        self.assertIn("«звук»", notices[0][1])
        # Повторное нажатие ничего не запускает заново.
        self._press(self._button("🚫 Не смогу"))
        self.assertEqual(self.context.on_assignment_declined.await_count, 1)
        self.assertEqual(self.context.bot.send_message.await_count, 1)

    def test_decline_without_reachable_admins_says_so(self):
        self.context.bot.send_message.side_effect = RuntimeError("blocked")
        callback = self._press(self._button("🚫 Не смогу"))
        text = callback.message.edit_text.await_args.args[0]
        self.assertIn("сообщить админам не удалось", text)
        self.assertEqual(
            callback.message.edit_text.await_args.kwargs["parse_mode"], "HTML"
        )

    def test_answer_keeps_formatting_and_long_text_only_drops_buttons(self):
        callback = SimpleNamespace(
            data=self._button("✅ Принял"),
            from_user=SimpleNamespace(id=MEMBER),
            message=SimpleNamespace(
                text="Заголовок", html_text="<b>Заголовок</b>",
                edit_text=AsyncMock(), edit_reply_markup=AsyncMock(),
                chat=SimpleNamespace(type="private", id=MEMBER),
            ),
            answer=AsyncMock(),
        )
        asyncio.run(self.handlers["on_personal_answer"](callback))
        self.assertEqual(
            callback.message.edit_text.await_args.args[0],
            "<b>Заголовок</b>\n\n✅ Подтверждено",
        )
        self.registry.set_confirmation(
            "sound", FUTURE.isoformat(), "звук", MEMBER, "declined"
        )
        callback.message.html_text = "x" * 4096
        callback.message.edit_text.reset_mock()
        asyncio.run(self.handlers["on_personal_answer"](callback))
        callback.message.edit_text.assert_not_awaited()
        callback.message.edit_reply_markup.assert_awaited_with(reply_markup=None)

    def test_stranger_and_stale_buttons_rejected(self):
        callback = self._press(self._button("✅ Принял"), user_id=5555)
        self.assertIn("не привязаны", callback.answer.await_args.args[0])
        stale = self._press(self._button("✅ Принял", date(2000, 1, 2)))
        self.assertIn("устарело", stale.answer.await_args.args[0])
        broken = self._press("pcf:x:sound:20990104:abc")
        self.assertIn("Некорректные", broken.answer.await_args.args[0])


class ReportTemplateEditorTest(RegistryTestCase):
    def setUp(self):
        super().setUp()
        self.registry.add_service_admin(SUPER, "sound", ADMIN)
        self.context = SimpleNamespace(
            interaction_logger=MagicMock(),
            debug_logger=MagicMock(),
            admin_id=SUPER,
            group_chat_id=-100,
            timezone=ZoneInfo("UTC"),
            service_registry=self.registry,
            bot=SimpleNamespace(send_message=AsyncMock(), delete_message=AsyncMock()),
        )
        dispatcher = SimpleNamespace(include_router=MagicMock())
        register(dispatcher, self.context)
        self.router = dispatcher.include_router.call_args_list[0].args[0]

    def _handler(self, name, kind="callback_query"):
        return next(
            item.callback
            for item in getattr(self.router, kind).handlers
            if item.callback.__name__ == name
        )

    def _message(self, text):
        return SimpleNamespace(
            from_user=SimpleNamespace(id=ADMIN),
            chat=SimpleNamespace(type="private", id=ADMIN),
            text=text, users_shared=None, chat_shared=None,
            answer=AsyncMock(),
        )

    async def _click(self, message, label):
        markup = message.answer.await_args.kwargs["reply_markup"]
        button = next(
            button for row in markup.inline_keyboard for button in row
            if button.text.endswith(label)
        )
        callback = SimpleNamespace(
            data=button.callback_data,
            from_user=SimpleNamespace(id=ADMIN),
            message=SimpleNamespace(
                chat=SimpleNamespace(type="private", id=ADMIN),
                answer=AsyncMock(),
            ),
            answer=AsyncMock(),
        )
        prefix = button.callback_data.split(":", 1)[0]
        name = {
            "notify_back": "on_notification_edit_back",
            "notify_finish": "on_notification_setting_finish",
        }.get(prefix, "on_group_reminder_action")
        await self._handler(name)(callback)
        return callback.message

    async def _type(self, text):
        message = self._message(text)
        await self._handler("on_service_parameter", "message")(message)
        return message

    def test_edit_back_reset_and_save(self):
        async def run():
            selection = SimpleNamespace(
                data="service:service_notify:sound",
                from_user=SimpleNamespace(id=ADMIN),
                message=self._message(""),
                answer=AsyncMock(),
            )
            await self._handler("on_service_selected")(selection)
            report = await self._click(selection.message, "Отчёт админам")
            self.assertIn("встроенный шаблон", report.answer.await_args.args[0])
            prompt = await self._click(report, "Изменить")
            back = await self._click(prompt, "Назад без изменения")
            self.assertIn("отчёт админам", back.answer.await_args.args[0])

            await self._click(back, "Изменить")
            wrong = await self._type("{task}")
            self.assertIn("❌", wrong.answer.await_args.args[0])
            saved = await self._type("{service_name}: {confirmation_status}")
            self.assertIn(
                "{service_name}: {confirmation_status}",
                saved.answer.await_args.args[0],
            )
            # До «Завершить» шаблон в базе не меняется.
            self.assertIsNone(
                self.registry.get_service("sound")["admin_report_template"]
            )
            menu = await self._click(saved, "К настройкам")
            await self._click(menu, "Завершить и сохранить")
            self.assertEqual(
                self.registry.get_service("sound")["admin_report_template"],
                "{service_name}: {confirmation_status}",
            )

        asyncio.run(run())

    def test_untouched_session_keeps_other_admins_template(self):
        async def run():
            selection = SimpleNamespace(
                data="service:service_notify:sound",
                from_user=SimpleNamespace(id=ADMIN),
                message=self._message(""),
                answer=AsyncMock(),
            )
            await self._handler("on_service_selected")(selection)
            self.registry.set_admin_report_template(
                SUPER, "sound", "{service_name}"
            )
            await self._click(selection.message, "Завершить и сохранить")
            self.assertEqual(
                self.registry.get_service("sound")["admin_report_template"],
                "{service_name}",
            )

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
