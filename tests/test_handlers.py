import asyncio
import inspect
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

from aiogram.exceptions import TelegramAPIError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove

from bot.excel_parser import ServiceAssignment, ServiceEvent
from bot.handlers import register, router
from bot.month_summary import month_title, next_month
from bot.navigation import NAV_CANCEL_TEXT, NAV_HINT, NAV_HOME_TEXT
from bot.service_registry import ServiceRegistry
from bot.ui import visible_units


def _confirmation_handler():
    return next(
        item.callback
        for item in reversed(router.callback_query.handlers)
        if item.callback.__name__ == "on_confirm"
    )


class ConfirmationHandlerTest(unittest.TestCase):
    def test_test_notify_offers_selected_service_preview(self):
        async def run():
            context = SimpleNamespace(
                interaction_logger=MagicMock(),
                debug_logger=MagicMock(),
                admin_id=67890,
                group_chat_id=-100,
                service_registry=SimpleNamespace(
                    is_super_admin=lambda actor_id: actor_id == 67890,
                    list_services=MagicMock(
                        return_value=[
                            {
                                "id": "sound",
                                "name": "Звук",
                                "group_chat_id": -200,
                                "enabled": True,
                            }
                        ]
                    ),
                    get_service=MagicMock(
                        return_value={
                            "id": "sound",
                            "name": "Звук",
                            "group_chat_id": -200,
                            "enabled": True,
                        }
                    ),
                ),
                bot=SimpleNamespace(
                    send_message=AsyncMock(), delete_message=AsyncMock()
                ),
                prepare_service_test_previews=AsyncMock(
                    return_value={
                        "sunday_date": date(2026, 10, 11),
                        "group_text": "Звук: {task_assignments}",
                        "personal_messages": [
                            {
                                "task": "Микшер",
                                "name": "Алексей",
                                "telegram_id": "123",
                                "text": "Алексей, проверьте звук",
                            }
                        ],
                        "issues": [],
                    }
                ),
            )
            dispatcher = SimpleNamespace(include_router=MagicMock())
            register(dispatcher, context)
            handler = router.message.handlers[-1].callback
            message = SimpleNamespace(
                from_user=SimpleNamespace(id=67890),
                chat=SimpleNamespace(type="private"),
                answer=AsyncMock(),
            )

            await handler(message)

            selection = message.answer.await_args.kwargs["reply_markup"]
            button = selection.inline_keyboard[0][0]
            self.assertEqual(button.callback_data, "service:test_notify:sound")
            admin_router = dispatcher.include_router.call_args_list[0].args[0]
            service_handler = next(
                item.callback for item in admin_router.callback_query.handlers
                if item.callback.__name__ == "on_service_selected"
            )
            callback = SimpleNamespace(
                data=button.callback_data,
                from_user=SimpleNamespace(id=67890),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            await service_handler(callback)

            self.assertEqual(context.bot.send_message.await_count, 0)
            action_markup = callback.message.answer.await_args.kwargs[
                "reply_markup"
            ]
            preview_button = next(
                button
                for row in action_markup.inline_keyboard
                for button in row
                if button.callback_data == "test_action:preview:sound"
            )
            test_action = next(
                item.callback
                for item in reversed(router.callback_query.handlers)
                if item.callback.__name__ == "on_test_notify_action"
            )
            preview_callback = SimpleNamespace(
                data=preview_button.callback_data,
                from_user=SimpleNamespace(id=67890),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            await test_action(preview_callback)

            context.prepare_service_test_previews.assert_awaited_once_with("sound")
            context.bot.send_message.assert_awaited_once()
            self.assertEqual(
                context.bot.send_message.await_args.args[0], 67890
            )
            self.assertIn("Звук", context.bot.send_message.await_args.args[1])
            self.assertIn("Микшер", context.bot.send_message.await_args.args[1])
            self.assertIn(
                "проверьте звук", context.bot.send_message.await_args.args[1]
            )

        asyncio.run(run())

    def test_test_command_rejects_non_admin_and_non_private_chat(self):
        async def run():
            context = SimpleNamespace(
                interaction_logger=MagicMock(),
                debug_logger=MagicMock(),
                admin_id=67890,
                group_chat_id=-100,
                bot=SimpleNamespace(send_message=AsyncMock()),
                prepare_test_previews=AsyncMock(),
            )
            dispatcher = SimpleNamespace(include_router=MagicMock())
            register(dispatcher, context)
            handler = router.message.handlers[-1].callback

            unauthorized = SimpleNamespace(
                from_user=SimpleNamespace(id=12345),
                chat=SimpleNamespace(type="private"),
                answer=AsyncMock(),
            )
            await handler(unauthorized)
            unauthorized.answer.assert_not_awaited()

            group_chat = SimpleNamespace(
                from_user=SimpleNamespace(id=67890),
                chat=SimpleNamespace(type="group"),
                answer=AsyncMock(),
            )
            await handler(group_chat)
            group_chat.answer.assert_not_awaited()
            context.bot.send_message.assert_not_awaited()

        asyncio.run(run())

    def test_group_test_sends_only_a_marked_message_to_selected_group(self):
        async def run():
            context = SimpleNamespace(
                interaction_logger=MagicMock(),
                debug_logger=MagicMock(),
                admin_id=67890,
                service_registry=SimpleNamespace(
                    is_super_admin=lambda actor_id: actor_id == 67890,
                    list_services=MagicMock(
                        return_value=[
                            {
                                "id": "sound",
                                "name": "Звук",
                                "group_chat_id": -200,
                                "enabled": True,
                            }
                        ]
                    ),
                    get_service=MagicMock(
                        return_value={
                            "id": "sound",
                            "name": "Звук",
                            "group_chat_id": -200,
                            "enabled": True,
                        }
                    ),
                ),
                bot=SimpleNamespace(send_message=AsyncMock()),
                prepare_service_test_previews=AsyncMock(),
            )
            dispatcher = SimpleNamespace(include_router=MagicMock())
            register(dispatcher, context)
            handler = next(
                item.callback
                for item in reversed(router.callback_query.handlers)
                if item.callback.__name__ == "on_test_notify_action"
            )
            callback = SimpleNamespace(
                data="test_action:group:sound",
                from_user=SimpleNamespace(id=67890),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            await handler(callback)

            context.bot.send_message.assert_awaited_once()
            self.assertEqual(
                context.bot.send_message.await_args.args[0], -200
            )
            self.assertIn(
                "ТЕСТ службы «Звук»",
                context.bot.send_message.await_args.args[1],
            )
            self.assertIn(
                "не запускалась",
                callback.message.answer.await_args.args[0],
            )
            context.prepare_service_test_previews.assert_not_awaited()

            malformed = SimpleNamespace(
                data="test_action:group",
                from_user=SimpleNamespace(id=67890),
                message=callback.message,
                answer=AsyncMock(),
            )
            await handler(malformed)
            malformed.answer.assert_awaited_once()
            self.assertIn("устарела", malformed.answer.await_args.args[0])

        asyncio.run(run())

    def test_confirmation_uses_current_registry_group_after_chat_change(self):
        async def run():
            participant = {
                "name": "Алексей",
                "tg_id": 12345,
                "confirmed": True,
                "group_update_sent": False,
                "group_chat_id": -1001,
            }
            context = SimpleNamespace(
                interaction_logger=MagicMock(),
                debug_logger=MagicMock(),
                admin_id=67890,
                state_cache=SimpleNamespace(
                    state={
                        "sunday_date": "2026-10-11",
                        "group_chat_id": -1002,
                        "participants": {"Ответственный": participant},
                    },
                    save=MagicMock(),
                ),
                service_registry=SimpleNamespace(
                    get_service=MagicMock(
                        return_value={
                            "enabled": True,
                            "group_chat_id": -1002,
                        }
                    )
                ),
                bot=SimpleNamespace(send_message=AsyncMock()),
                group_chat_id=-1001,
            )
            dispatcher = SimpleNamespace(include_router=MagicMock())
            register(dispatcher, context)
            handler = _confirmation_handler()
            callback = SimpleNamespace(
                data="cfm:2026-10-11:Ответственный",
                from_user=SimpleNamespace(id=12345, username="alex"),
                message=None,
                answer=AsyncMock(),
            )

            await handler(callback)

            context.bot.send_message.assert_awaited_once()
            self.assertEqual(
                context.bot.send_message.await_args.args[0], -1002
            )
            self.assertEqual(participant["group_chat_id"], -1002)
            self.assertTrue(participant["group_update_sent"])
            from bot.app import _merge_week_state

            merged = _merge_week_state(
                context.state_cache.state,
                {
                    "sunday_date": "2026-10-11",
                    "group_chat_id": -1002,
                    "participants": {
                        "Ответственный": {
                            "name": "Алексей",
                            "tg_id": 12345,
                        }
                    },
                },
                -1002,
            )
            self.assertTrue(
                merged["participants"]["Ответственный"]["group_update_sent"]
            )
            callback.answer.assert_awaited_once_with(
                "Вы уже подтвердили участие"
            )

        asyncio.run(run())

    def test_failed_group_update_can_be_retried_on_duplicate_callback(self):
        async def run():
            participant = {
                "name": "<Алексей>",
                "tg_id": 12345,
                "confirmed": False,
                "group_update_sent": False,
            }
            context = SimpleNamespace(
                interaction_logger=MagicMock(),
                debug_logger=MagicMock(),
                state_cache=SimpleNamespace(
                    state={
                        "sunday_date": "2026-10-11",
                        "participants": {"Ответственный": participant},
                    },
                    save=MagicMock(),
                ),
                bot=SimpleNamespace(
                    send_message=AsyncMock(
                        side_effect=[RuntimeError("temporary"), object()]
                    )
                ),
                group_chat_id=-100,
            )
            dispatcher = SimpleNamespace(include_router=MagicMock())
            register(dispatcher, context)
            handler = _confirmation_handler()
            callback = SimpleNamespace(
                data="cfm:2026-10-11:Ответственный",
                from_user=SimpleNamespace(id=12345, username="alex"),
                message=None,
                answer=AsyncMock(),
            )

            await handler(callback)
            self.assertTrue(participant["confirmed"])
            self.assertFalse(participant["group_update_sent"])

            await handler(callback)

            self.assertTrue(participant["group_update_sent"])
            self.assertEqual(context.bot.send_message.await_count, 2)
            self.assertEqual(
                context.bot.send_message.await_args.kwargs,
                {"parse_mode": None},
            )
            self.assertIn(
                "Нажмите кнопку повторно",
                callback.answer.await_args_list[0].args[0],
            )

        asyncio.run(run())

    def test_confirmation_uses_currently_configured_group(self):
        async def run():
            participant = {
                "name": "Алексей",
                "tg_id": 12345,
                "confirmed": False,
                "group_update_sent": False,
                "group_chat_id": -1001,
            }
            context = SimpleNamespace(
                interaction_logger=MagicMock(),
                debug_logger=MagicMock(),
                state_cache=SimpleNamespace(
                    state={
                        "sunday_date": "2026-10-11",
                        "group_chat_id": -1002,
                        "participants": {"Ответственный": participant},
                    },
                    save=MagicMock(),
                ),
                service_registry=SimpleNamespace(
                    get_service=MagicMock(
                        return_value={
                            "enabled": True,
                            "group_chat_id": -1002,
                        }
                    )
                ),
                bot=SimpleNamespace(send_message=AsyncMock()),
                group_chat_id=-1002,
            )
            dispatcher = SimpleNamespace(include_router=MagicMock())
            register(dispatcher, context)
            callback = SimpleNamespace(
                data="cfm:2026-10-11:Ответственный",
                from_user=SimpleNamespace(id=12345, username="alex"),
                message=None,
                answer=AsyncMock(),
            )

            await _confirmation_handler()(callback)

            self.assertEqual(
                context.bot.send_message.await_args.args[0], -1002
            )

        asyncio.run(run())

    def test_disabled_assembly_saves_confirmation_without_group_notice(self):
        async def run():
            participant = {
                "name": "Алексей",
                "tg_id": 12345,
                "confirmed": False,
                "group_update_sent": False,
                "group_chat_id": -1001,
            }
            context = SimpleNamespace(
                interaction_logger=MagicMock(),
                debug_logger=MagicMock(),
                state_cache=SimpleNamespace(
                    state={
                        "sunday_date": "2026-10-11",
                        "group_chat_id": -1001,
                        "participants": {"Ответственный": participant},
                    },
                    save=MagicMock(),
                ),
                service_registry=SimpleNamespace(
                    get_service=MagicMock(return_value={"enabled": False})
                ),
                bot=SimpleNamespace(send_message=AsyncMock()),
                group_chat_id=-1001,
            )
            dispatcher = SimpleNamespace(include_router=MagicMock())
            register(dispatcher, context)
            callback = SimpleNamespace(
                data="cfm:2026-10-11:Ответственный",
                from_user=SimpleNamespace(id=12345, username="alex"),
                message=None,
                answer=AsyncMock(),
            )

            await _confirmation_handler()(callback)

            self.assertTrue(participant["confirmed"])
            self.assertFalse(participant["group_update_sent"])
            context.bot.send_message.assert_not_awaited()
            self.assertIn("служение отключено", callback.answer.await_args.args[0])

        asyncio.run(run())

    def test_failed_confirmation_save_is_reported_and_retried_when_disabled(self):
        async def run():
            participant = {
                "name": "Алексей",
                "tg_id": 12345,
                "confirmed": False,
                "group_update_sent": False,
                "group_chat_id": -1001,
            }
            context = SimpleNamespace(
                interaction_logger=MagicMock(),
                debug_logger=MagicMock(),
                admin_id=67890,
                state_cache=SimpleNamespace(
                    state={
                        "sunday_date": "2026-10-11",
                        "group_chat_id": -1001,
                        "participants": {"Ответственный": participant},
                    },
                    save=MagicMock(
                        side_effect=[OSError("disk full"), None]
                    ),
                ),
                service_registry=SimpleNamespace(
                    get_service=MagicMock(return_value={"enabled": False})
                ),
                bot=SimpleNamespace(send_message=AsyncMock()),
                group_chat_id=-1001,
            )
            dispatcher = SimpleNamespace(include_router=MagicMock())
            register(dispatcher, context)
            handler = _confirmation_handler()

            def make_callback():
                return SimpleNamespace(
                    data="cfm:2026-10-11:Ответственный",
                    from_user=SimpleNamespace(id=12345, username="alex"),
                    message=None,
                    answer=AsyncMock(),
                )

            with patch("bot.handlers._notify_admin", new=AsyncMock()):
                first = make_callback()
                await handler(first)
                self.assertIn("Не удалось сохранить", first.answer.await_args.args[0])
                self.assertTrue(participant["confirmed"])
                self.assertFalse(participant["confirmation_persisted"])

                second = make_callback()
                await handler(second)

            self.assertIn("служение отключено", second.answer.await_args.args[0])
            self.assertNotIn("confirmation_persisted", participant)
            self.assertFalse(participant["group_update_sent"])
            context.bot.send_message.assert_not_awaited()
            self.assertEqual(context.state_cache.save.call_count, 2)

        asyncio.run(run())

    def test_group_update_save_failure_does_not_repeat_post_on_retry(self):
        async def run():
            participant = {
                "name": "Алексей",
                "tg_id": 12345,
                "confirmed": False,
                "group_update_sent": False,
            }
            state_cache = SimpleNamespace(
                state={
                    "sunday_date": "2026-10-11",
                    "participants": {"Ответственный": participant},
                },
                save=MagicMock(side_effect=[None, OSError("disk full"), None]),
            )
            context = SimpleNamespace(
                interaction_logger=MagicMock(),
                debug_logger=MagicMock(),
                admin_id=67890,
                state_cache=state_cache,
                bot=SimpleNamespace(send_message=AsyncMock(return_value=object())),
                group_chat_id=-100,
            )
            dispatcher = SimpleNamespace(include_router=MagicMock())
            register(dispatcher, context)
            handler = _confirmation_handler()
            callback = SimpleNamespace(
                data="cfm:2026-10-11:Ответственный",
                from_user=SimpleNamespace(id=12345, username="alex"),
                message=None,
                answer=AsyncMock(),
            )

            with patch("bot.handlers._notify_admin", new=AsyncMock()) as notify_admin:
                await handler(callback)

            self.assertTrue(participant["group_update_sent"])
            self.assertFalse(participant["group_update_persisted"])
            self.assertIn("не удалось сохранить", callback.answer.await_args.args[0])
            context.bot.send_message.assert_awaited_once()
            notify_admin.assert_awaited_once()

            with patch("bot.handlers._notify_admin", new=AsyncMock()):
                await handler(callback)

            self.assertTrue(participant["group_update_persisted"])
            self.assertEqual(context.bot.send_message.await_count, 1)
            self.assertEqual(state_cache.save.call_count, 3)

        asyncio.run(run())

    def test_concurrent_callbacks_send_only_one_group_update(self):
        async def run():
            entered = asyncio.Event()
            release = asyncio.Event()

            async def send_message(*_args, **_kwargs):
                entered.set()
                await release.wait()

            participant = {
                "name": "Алексей",
                "tg_id": 12345,
                "confirmed": False,
                "group_update_sent": False,
            }
            context = SimpleNamespace(
                interaction_logger=MagicMock(),
                debug_logger=MagicMock(),
                confirmation_locks={},
                state_cache=SimpleNamespace(
                    state={
                        "sunday_date": "2026-10-11",
                        "participants": {"Ответственный": participant},
                    },
                    save=MagicMock(),
                ),
                bot=SimpleNamespace(send_message=AsyncMock(side_effect=send_message)),
                group_chat_id=-100,
            )
            dispatcher = SimpleNamespace(include_router=MagicMock())
            register(dispatcher, context)
            handler = _confirmation_handler()

            def callback():
                return SimpleNamespace(
                    data="cfm:2026-10-11:Ответственный",
                    from_user=SimpleNamespace(id=12345, username="alex"),
                    message=None,
                    answer=AsyncMock(),
                )

            first = callback()
            second = callback()
            first_task = asyncio.create_task(handler(first))
            await entered.wait()
            second_task = asyncio.create_task(handler(second))
            await asyncio.sleep(0)
            release.set()
            await asyncio.gather(first_task, second_task)

            self.assertEqual(context.bot.send_message.await_count, 1)
            self.assertTrue(participant["group_update_sent"])
            self.assertEqual(
                second.answer.await_args.args[0],
                "Вы уже подтвердили участие",
            )

        asyncio.run(run())


class ServiceManagementHandlerTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.registry = ServiceRegistry(
            Path(self.temp_dir.name) / "registry.sqlite3"
        )
        self.registry.initialize(67890, -100)
        self.context = SimpleNamespace(
            interaction_logger=MagicMock(),
            debug_logger=MagicMock(),
            admin_id=67890,
            group_chat_id=-100,
            timezone=ZoneInfo("UTC"),
            service_registry=self.registry,
            bot=SimpleNamespace(
                send_message=AsyncMock(), delete_message=AsyncMock()
            ),
            load_service_task_assignments=AsyncMock(
                return_value={"Микшер": ["Имя из расписания"]}
            ),
        )
        self.dispatcher = SimpleNamespace(include_router=MagicMock())
        register(self.dispatcher, self.context)
        self.admin_router = self.dispatcher.include_router.call_args_list[0].args[0]

    @staticmethod
    def message(actor_id, text, chat_type="private"):
        return SimpleNamespace(
            from_user=SimpleNamespace(id=actor_id),
            chat=SimpleNamespace(type=chat_type, id=actor_id),
            text=text,
            users_shared=None,
            chat_shared=None,
            answer=AsyncMock(),
        )

    def callback_handler(self, name):
        return next(
            handler.callback
            for handler in self.admin_router.callback_query.handlers
            if handler.callback.__name__ == name
        )

    async def dialog_button(
        self, message, predicate, actor_id, router_instance=None
    ):
        target_router = router_instance or self.admin_router
        for _ in range(20):
            markup = message.answer.await_args.kwargs.get("reply_markup")
            buttons = [
                button
                for row in markup.inline_keyboard
                for button in row
            ] if markup else []
            button = next((item for item in buttons if predicate(item)), None)
            if button is not None:
                return button
            next_page = next(
                (
                    item for item in buttons
                    if item.callback_data
                    and item.callback_data.startswith("ui_page:")
                    and "Далее" in item.text
                ),
                None,
            )
            if next_page is None:
                break
            callback = SimpleNamespace(
                data=next_page.callback_data,
                from_user=SimpleNamespace(id=actor_id),
                message=message,
                answer=AsyncMock(),
            )
            page_handler = next(
                handler.callback
                for handler in target_router.callback_query.handlers
                if handler.callback.__name__ == "on_dialog_page"
            )
            await page_handler(callback)
        self.fail("Не удалось найти кнопку на страницах диалога")

    def test_text_in_personal_notifications_menu_gets_hint(self):
        async def run():
            actor_id = 4406
            self.registry.set_responsible_contact(
                67890, "sound", "Микшер", "Алексей", actor_id
            )
            command = self.message(actor_id, "/my_notifications")
            command_handler = next(
                item.callback
                for item in self.admin_router.message.handlers
                if item.callback.__name__ == "on_my_notifications"
            )
            await command_handler(command)
            text_handler = next(
                item.callback
                for item in self.admin_router.message.handlers
                if item.callback.__name__ == "on_service_parameter"
            )
            text = self.message(actor_id, "привет")

            await text_handler(text)

            self.assertEqual(
                text.answer.await_args.args[0],
                "Выберите действие кнопками меню.",
            )

        asyncio.run(run())

    def test_service_list_shows_all_eight_services_in_requested_order(self):
        async def run():
            handler = next(
                item.callback
                for item in self.admin_router.message.handlers
                if item.callback.__name__ == "on_services"
            )
            message = self.message(67890, "/services")
            await handler(message)

            markup = message.answer.await_args.kwargs["reply_markup"]
            self.assertEqual(
                [
                    [button.callback_data.rsplit(":", 1)[-1] for button in row]
                    for row in markup.inline_keyboard
                ],
                [
                    ["assembly"],
                    ["broadcast"],
                    ["kitchen", "sound"],
                    ["order", "office_cleaning"],
                    ["books", "children"],
                ],
            )
            self.assertEqual(sum(map(len, markup.inline_keyboard)), 8)
            self.assertFalse(
                any(
                    button.callback_data.startswith("ui_page:")
                    for row in markup.inline_keyboard
                    for button in row
                )
            )

        asyncio.run(run())

    def test_super_admin_can_configure_chat_admin_and_contact(self):
        async def run():
            handlers = self.admin_router.message.handlers
            direct_add = self.message(
                67890, "/service_admin_add sound 12345"
            )
            await handlers[2].callback(
                direct_add
            )
            self.assertEqual(self.registry.list_service_admins("sound"), [])
            confirmation_button = direct_add.answer.await_args.kwargs[
                "reply_markup"
            ].inline_keyboard[0][0]
            confirmation = SimpleNamespace(
                data=confirmation_button.callback_data,
                from_user=SimpleNamespace(id=67890),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            await self.callback_handler("on_shared_admin_confirmation")(
                confirmation
            )
            await handlers[4].callback(
                self.message(67890, "/service_chat sound -10022 on")
            )
            await handlers[5].callback(
                self.message(
                    67890,
                    "/service_contact sound Микшер | Алексей Иванов | "
                    "12345 | alex",
                )
            )
            disable = self.message(67890, "/service_chat assembly -10077 off")
            await handlers[4].callback(disable)
            await self.callback_handler("on_service_disable")(SimpleNamespace(
                data=self.callback_data_list(disable)[0],
                from_user=SimpleNamespace(id=67890),
                message=disable, answer=AsyncMock(),
            ))

            self.assertEqual(self.registry.list_service_admins("sound"), [12345])
            self.assertEqual(self.context.group_chat_id, -10077)
            service = next(
                service for service in self.registry.list_services()
                if service["id"] == "sound"
            )
            self.assertEqual(service["group_chat_id"], -10022)
            self.assertTrue(service["enabled"])
            assembly = self.registry.get_service("assembly")
            self.assertEqual(assembly["group_chat_id"], -10077)
            self.assertFalse(assembly["enabled"])
            self.assertEqual(
                self.registry.get_responsible_contact(
                    "sound", "Микшер", "Алексей Иванов"
                ),
                None,
            )

        asyncio.run(run())

    def test_notification_settings_require_guided_flow(self):
        async def run():
            self.registry.add_service_admin(67890, "sound", 12345)
            handler = self.admin_router.message.handlers[6].callback

            manual = self.message(
                12345,
                '/service_notify slides 2 17:30 | Текст группы | {}',
            )
            await handler(manual)
            self.assertIn(
                "пошагово",
                manual.answer.await_args.args[0],
            )
            service = next(
                service for service in self.registry.list_services()
                if service["id"] == "sound"
            )
            self.assertEqual(service["reminder_days"], 1)
            self.assertIsNone(service["group_template"])

        asyncio.run(run())

    def test_nav_keyboard_cancel_steps_back_and_home_opens_start(self):
        async def run():
            self.registry.add_service_admin(67890, "sound", 12345)
            await self.admin_router.message.handlers[6].callback(
                self.message(12345, "/service_notify")
            )
            nav = next(
                item.callback for item in self.admin_router.message.handlers
                if item.callback.__name__ == "on_nav_button"
            )

            async def press(data):
                callback = SimpleNamespace(
                    data=data,
                    from_user=SimpleNamespace(id=12345),
                    message=self.message(12345, ""),
                    answer=AsyncMock(),
                )
                callback.message.answer.return_value = SimpleNamespace(
                    message_id=700
                )
                handler = (
                    "on_service_selected" if data.startswith("service:")
                    else "on_group_reminder_action"
                )
                await self.callback_handler(handler)(callback)
                return callback.message.answer

            def buttons(answer):
                return [
                    item.callback_data
                    for row in answer.await_args.kwargs[
                        "reply_markup"
                    ].inline_keyboard
                    for item in row
                ]

            async def tap(text):
                message = self.message(12345, text)
                message.answer.return_value = SimpleNamespace(message_id=800)
                await nav(message)
                return message.answer

            root = await press("service:service_notify:sound")
            default = next(
                data for data in buttons(root)
                if data.startswith("group_reminder:default:")
            )
            screen = await press(default)
            # Корень настройки («✖️ Закрыть настройку») — уже второй
            # уровень: сначала подсказка с нижней клавиатурой.
            hint = root.await_args_list[0]
            self.assertEqual(hint.args[0], NAV_HINT)
            self.assertEqual(
                [b.text for b in hint.kwargs["reply_markup"].keyboard[0]],
                [NAV_CANCEL_TEXT, NAV_HOME_TEXT],
            )
            # На шаге ввода «Отмена» — как «Назад без изменения».
            edit = next(
                data for data in buttons(screen)
                if data.startswith("group_reminder:edit_default:")
            )
            await press(edit)
            back = await tap(NAV_CANCEL_TEXT)
            self.assertIn("шаблон по умолчанию", back.await_args.args[0])
            self.assertIsNone(
                next(
                    service for service in self.registry.list_services()
                    if service["id"] == "sound"
                )["group_template"]
            )
            # Ещё раз «Отмена» — «⬅️ К настройкам».
            menu = await tap(NAV_CANCEL_TEXT)
            self.assertTrue(any(
                data.startswith("group_reminder:default:")
                for data in buttons(menu)
            ))
            home = await tap(NAV_HOME_TEXT)
            self.assertTrue(any(
                data.startswith("menu:") for data in buttons(home)
            ))
            removal = self.context.bot.send_message.await_args_list[-1]
            self.assertIsInstance(
                removal.kwargs["reply_markup"], ReplyKeyboardRemove
            )

        asyncio.run(run())

    def test_stored_nav_hint_is_deleted_after_restart(self):
        async def run():
            # Подсказка осталась от прошлого запуска бота.
            self.registry.set_nav_hints(12345, [777, 778])
            def delete(chat_id, message_id):
                if message_id == 778:
                    raise TelegramAPIError(MagicMock(), "flood")

            self.context.bot.delete_message.side_effect = delete
            start = self.admin_router.message.handlers[8].callback
            await start(self.message(12345, "/start"))
            deleted = [
                call.kwargs["message_id"]
                for call in self.context.bot.delete_message.await_args_list
            ]
            self.assertIn(777, deleted)
            # Неудавшееся удаление остаётся в базе для следующей попытки.
            self.assertEqual(self.registry.get_nav_hints(12345), [778])

        asyncio.run(run())

    def test_nav_keyboard_middleware_installed_on_real_bot_session(self):
        from aiogram import Bot

        bot = Bot("123456:TEST-TOKEN")
        self.addCleanup(lambda: asyncio.run(bot.session.close()))
        context = SimpleNamespace(**{**vars(self.context), "bot": bot})
        register(SimpleNamespace(include_router=MagicMock()), context)
        self.assertEqual(
            [item.__name__ for item in bot.session.middleware],
            ["track_reply_keyboards"],
        )

    def test_nav_keyboard_removal_is_retried_and_start_without_menu_hides(self):
        async def run():
            self.registry.add_service_admin(67890, "sound", 12345)
            await self.admin_router.message.handlers[6].callback(
                self.message(12345, "/service_notify")
            )
            callback = SimpleNamespace(
                data="service:service_notify:sound",
                from_user=SimpleNamespace(id=12345),
                message=self.message(12345, ""),
                answer=AsyncMock(),
            )
            await self.callback_handler("on_service_selected")(callback)
            default = next(
                button.callback_data
                for row in callback.message.answer.await_args.kwargs[
                    "reply_markup"
                ].inline_keyboard
                for button in row
                if button.callback_data.startswith("group_reminder:default:")
            )
            callback.data = default
            await self.callback_handler("on_group_reminder_action")(callback)
            self.assertIn(
                NAV_HINT,
                [call.args[0] for call in callback.message.answer.await_args_list],
            )

            send = self.context.bot.send_message
            send.side_effect = TelegramAPIError(MagicMock(), "flood")
            start = self.admin_router.message.handlers[8].callback
            await start(self.message(12345, "/start"))
            failed = send.await_count
            self.assertGreaterEqual(failed, 1)
            # Клавиатура осталась у клиента — следующий /start убирает снова.
            send.side_effect = None
            await start(self.message(12345, "/start"))
            self.assertEqual(send.await_count, failed + 1)
            self.assertIsInstance(
                send.await_args.kwargs["reply_markup"], ReplyKeyboardRemove
            )
            await start(self.message(12345, "/start"))
            self.assertEqual(send.await_count, failed + 1)

        asyncio.run(run())

    def test_template_screens_offer_preview_and_field_help(self):
        async def run():
            self.registry.add_service_admin(67890, "sound", 12345)
            self.registry.set_service_default_template(
                67890, "sound", "Свой {sunday_date}"
            )
            await self.admin_router.message.handlers[6].callback(
                self.message(12345, "/service_notify")
            )
            self.context.get_template_preview = AsyncMock(return_value={
                "date": "25.10.2026", "text": "Готовый текст",
                "has_data": True, "error": None,
            })
            action = self.callback_handler("on_group_reminder_action")

            async def press(data):
                callback = SimpleNamespace(
                    data=data,
                    from_user=SimpleNamespace(id=12345),
                    message=self.message(12345, ""),
                    answer=AsyncMock(),
                )
                callback.message.answer.return_value = SimpleNamespace(
                    message_id=600
                )
                if data.startswith("service:"):
                    await self.callback_handler("on_service_selected")(callback)
                elif data.startswith("notify_back:"):
                    await self.callback_handler(
                        "on_notification_edit_back"
                    )(callback)
                else:
                    await action(callback)
                return callback.message.answer.await_args

            def button(call, prefix):
                return next(
                    item.callback_data
                    for row in call.kwargs["reply_markup"].inline_keyboard
                    for item in row
                    if item.callback_data.startswith(prefix)
                )

            root = await press("service:service_notify:sound")
            for screen, edit, kind, fields in (
                ("group_reminder:default:", "group_reminder:edit_default:",
                 "default", "{informational_notes} — "),
                ("group_reminder:report:", "group_reminder:edit_report:",
                 "report", "{confirmation_status} — "),
            ):
                template_screen = await press(button(root, screen))
                preview = await press(
                    button(template_screen, "group_reminder:tpl_preview:")
                )
                self.assertIn("Готовый текст", preview.args[0])
                self.assertIn("25.10.2026", preview.args[0])
                self.assertEqual(
                    self.context.get_template_preview.await_args.args,
                    ("sound", kind,
                     "Свой {sunday_date}" if kind == "default" else None),
                )
                back = await press(button(preview, "group_reminder:tpl_back:"))
                prompt = await press(button(back, edit))
                self.assertIn("\n• {service_name} — ", prompt.args[0])
                self.assertIn(fields, prompt.args[0])
                template_screen = await press(button(prompt, "notify_back:"))
                button(template_screen, "group_reminder:tpl_preview:")
                root = await press(
                    button(template_screen, "group_reminder:root_back:")
                )

        asyncio.run(run())

    def test_notification_step_back_keeps_session_and_root_close_has_navigation(self):
        async def run():
            self.registry.add_service_admin(67890, "sound", 12345)
            command = self.message(12345, "/service_notify")
            await self.admin_router.message.handlers[6].callback(command)
            service_callback = SimpleNamespace(
                data="service:service_notify:sound",
                from_user=SimpleNamespace(id=12345),
                message=self.message(12345, ""),
                answer=AsyncMock(),
            )
            service_callback.message.answer.return_value = SimpleNamespace(
                message_id=501
            )
            await self.callback_handler("on_service_selected")(service_callback)
            root_markup = service_callback.message.answer.await_args.kwargs[
                "reply_markup"
            ]
            group_button = next(
                button
                for row in root_markup.inline_keyboard
                for button in row
                if button.callback_data.startswith(
                    "group_reminder:default:sound:"
                )
            )
            edit_callback = SimpleNamespace(
                data=group_button.callback_data,
                from_user=SimpleNamespace(id=12345),
                message=self.message(12345, ""),
                answer=AsyncMock(),
            )
            edit_callback.message.answer.return_value = SimpleNamespace(
                message_id=502
            )
            await self.callback_handler("on_group_reminder_action")(
                edit_callback
            )
            edit_default_button = next(
                button
                for row in edit_callback.message.answer.await_args.kwargs[
                    "reply_markup"
                ].inline_keyboard
                for button in row
                if button.callback_data.startswith(
                    "group_reminder:edit_default:"
                )
            )
            edit_default_callback = SimpleNamespace(
                data=edit_default_button.callback_data,
                from_user=SimpleNamespace(id=12345),
                message=self.message(12345, ""),
                answer=AsyncMock(),
            )
            edit_default_callback.message.answer.return_value = SimpleNamespace(
                message_id=502
            )
            await self.callback_handler("on_group_reminder_action")(
                edit_default_callback
            )
            prompt_markup = edit_default_callback.message.answer.await_args.kwargs[
                "reply_markup"
            ]
            back_button = next(
                button
                for row in prompt_markup.inline_keyboard
                for button in row
                if button.callback_data.startswith("notify_back:")
            )
            back_callback = SimpleNamespace(
                data=back_button.callback_data,
                from_user=SimpleNamespace(id=12345),
                message=edit_default_callback.message,
                answer=AsyncMock(),
            )
            back_callback.message.answer.return_value = SimpleNamespace(
                message_id=503
            )
            await self.callback_handler("on_notification_edit_back")(
                back_callback
            )
            returned_markup = back_callback.message.answer.await_args.kwargs[
                "reply_markup"
            ]
            root_back = next(
                button
                for row in returned_markup.inline_keyboard
                for button in row
                if button.callback_data.startswith(
                    "group_reminder:root_back:"
                )
            )
            root_callback = SimpleNamespace(
                data=root_back.callback_data,
                from_user=SimpleNamespace(id=12345),
                message=back_callback.message,
                answer=AsyncMock(),
            )
            await self.callback_handler("on_group_reminder_action")(
                root_callback
            )
            returned_markup = root_callback.message.answer.await_args.kwargs[
                "reply_markup"
            ]
            self.assertTrue(
                any(
                    button.callback_data.startswith("notify_finish:")
                    for row in returned_markup.inline_keyboard
                    for button in row
                )
            )
            self.assertEqual(
                self.registry.get_service("sound")["group_template"], None
            )

            close_button = next(
                button
                for row in returned_markup.inline_keyboard
                for button in row
                if button.text == "✖️ Закрыть настройку"
            )
            close_callback = SimpleNamespace(
                data=close_button.callback_data,
                from_user=SimpleNamespace(id=12345),
                message=self.message(12345, ""),
                answer=AsyncMock(),
            )
            close_callback.message.answer.return_value = SimpleNamespace(
                message_id=504
            )
            await self.callback_handler("on_notification_setting_cancel")(
                close_callback
            )
            result_markup = close_callback.message.answer.await_args.kwargs[
                "reply_markup"
            ]
            self.assertIn(
                "svc_tasks:open:sound",
                [button.callback_data for row in result_markup.inline_keyboard
                 for button in row],
            )
            self.assertIn(
                "⚙️ ЗВУК", close_callback.message.answer.await_args.args[0]
            )
            self.assertEqual(
                [
                    call.kwargs["message_id"]
                    for call in self.context.bot.delete_message.await_args_list
                    if isinstance(call.kwargs["message_id"], int)
                ],
                [501, 502, 502, 503, 503],
            )

        asyncio.run(run())

    def test_chat_picker_prompt_is_deleted_after_shared_chat_arrives(self):
        async def run():
            command = self.message(67890, "/service_chat")
            command_handler = next(
                item.callback
                for item in self.admin_router.message.handlers
                if item.callback.__name__ == "on_service_chat"
            )
            await command_handler(command)
            service_button = next(
                button
                for row in command.answer.await_args.kwargs[
                    "reply_markup"
                ].inline_keyboard
                for button in row
                if button.callback_data.startswith("service:service_chat:")
            )
            selection = SimpleNamespace(
                data=service_button.callback_data,
                from_user=SimpleNamespace(id=67890),
                message=self.message(67890, ""),
                answer=AsyncMock(),
            )
            await self.callback_handler("on_service_selected")(selection)
            action_selection = SimpleNamespace(
                data=f"service:service_chat_set:{service_button.callback_data.rsplit(':', 1)[1]}",
                from_user=SimpleNamespace(id=67890),
                message=self.message(67890, ""),
                answer=AsyncMock(),
            )
            action_selection.message.answer.return_value = SimpleNamespace(
                message_id=601
            )
            await self.callback_handler("on_service_selected")(action_selection)
            keyboard = action_selection.message.answer.await_args.kwargs[
                "reply_markup"
            ]
            request_id = keyboard.keyboard[0][0].request_chat.request_id
            shared_message = SimpleNamespace(
                from_user=SimpleNamespace(id=67890),
                chat=SimpleNamespace(type="private", id=67890),
                chat_shared=SimpleNamespace(
                    request_id=request_id,
                    chat_id=-100601,
                    title="Test chat",
                ),
                answer=AsyncMock(),
            )
            chat_shared_handler = next(
                item.callback
                for item in self.admin_router.message.handlers
                if item.callback.__name__ == "on_chat_shared"
            )
            await chat_shared_handler(shared_message)

            self.context.bot.delete_message.assert_awaited_once_with(
                chat_id=67890, message_id=601
            )

        asyncio.run(run())

    def test_service_admin_can_use_guided_notification_setup(self):
        async def run():
            self.registry.add_service_admin(67890, "sound", 12345)
            self.registry.set_notification_settings(
                67890,
                "sound",
                reminder_days=2,
                reminder_time="17:00",
                group_template="Текст группы",
                personal_templates={
                    "Микшер": "Старый текст",
                    "Задача отсутствующая в этом графике": "Не удалять",
                },
            )
            message = self.message(12345, "/service_notify")
            await self.admin_router.message.handlers[6].callback(message)
            service_button = message.answer.await_args.kwargs[
                "reply_markup"
            ].inline_keyboard[0][0]
            service_callback = SimpleNamespace(
                data=service_button.callback_data,
                from_user=SimpleNamespace(id=12345),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            await self.callback_handler("on_service_selected")(service_callback)
            menu = service_callback.message.answer.await_args.kwargs[
                "reply_markup"
            ]
            reminders_button = next(
                button
                for row in menu.inline_keyboard
                for button in row
                if button.callback_data.startswith(
                    "group_reminder:list:sound:"
                )
            )
            edit_callback = SimpleNamespace(
                data=reminders_button.callback_data,
                from_user=SimpleNamespace(id=12345),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            await self.callback_handler(
                "on_group_reminder_action"
            )(edit_callback)
            rules_menu = edit_callback.message.answer.await_args.kwargs[
                "reply_markup"
            ]
            rule_button = next(
                button
                for row in rules_menu.inline_keyboard
                for button in row
                if button.callback_data.startswith("group_reminder:open:sound:")
            )
            rule_callback = SimpleNamespace(
                data=rule_button.callback_data,
                from_user=SimpleNamespace(id=12345),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            await self.callback_handler("on_group_reminder_action")(
                rule_callback
            )
            edit_rule_menu = rule_callback.message.answer.await_args.kwargs[
                "reply_markup"
            ]
            days_button = next(
                button
                for row in edit_rule_menu.inline_keyboard
                for button in row
                if button.text == "📅 Срок"
            )
            days_callback = SimpleNamespace(
                data=days_button.callback_data,
                from_user=SimpleNamespace(id=12345),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            await self.callback_handler("on_group_reminder_action")(
                days_callback
            )
            self.assertIn("0 до 6", days_callback.message.answer.await_args.args[0])
            staged_input = next(
                item.callback
                for item in self.admin_router.message.handlers
                if item.callback.__name__ == "on_service_parameter"
            )
            await staged_input(self.message(12345, "7"))
            updated_days = self.message(12345, "3")
            await staged_input(updated_days)
            rules_markup = updated_days.answer.await_args.kwargs["reply_markup"]
            back_to_list = next(
                button
                for row in rules_markup.inline_keyboard
                for button in row
                if button.callback_data.startswith("group_reminder:back:")
            )
            list_callback = SimpleNamespace(
                data=back_to_list.callback_data,
                from_user=SimpleNamespace(id=12345),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            await self.callback_handler("on_group_reminder_action")(
                list_callback
            )
            rules_menu = list_callback.message.answer.await_args.kwargs[
                "reply_markup"
            ]
            root_back = next(
                button
                for row in rules_menu.inline_keyboard
                for button in row
                if button.callback_data.startswith(
                    "group_reminder:root_back:"
                )
            )
            root_callback = SimpleNamespace(
                data=root_back.callback_data,
                from_user=SimpleNamespace(id=12345),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            await self.callback_handler("on_group_reminder_action")(
                root_callback
            )
            root_menu = root_callback.message.answer.await_args.kwargs[
                "reply_markup"
            ]
            finish_button = next(
                button
                for row in root_menu.inline_keyboard
                for button in row
                if button.text == "✅ Завершить и сохранить"
            )
            finish_callback = SimpleNamespace(
                data=finish_button.callback_data,
                from_user=SimpleNamespace(id=12345),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            await self.callback_handler(
                "on_notification_setting_finish"
            )(finish_callback)

            service = self.registry.get_service("sound")
            self.assertEqual(
                self.registry.list_group_reminders("sound")[0][
                    "reminder_days"
                ],
                3,
            )
            self.assertEqual(service["group_template"], "Текст группы")
            self.assertEqual(
                service["personal_templates"],
                {
                    "Микшер": "Старый текст",
                    "Задача отсутствующая в этом графике": "Не удалять",
                },
            )
            self.context.load_service_task_assignments.assert_not_awaited()
            result_markup = finish_callback.message.answer.await_args.kwargs[
                "reply_markup"
            ]
            self.assertEqual(
                {
                    button.text
                    for row in result_markup.inline_keyboard
                    for button in row
                },
                {"⚙️ К настройкам служения", "📋 К списку служений"},
            )

            retry_command = self.message(12345, "/service_notify")
            await self.admin_router.message.handlers[6].callback(retry_command)
            retry_service_callback = SimpleNamespace(
                data="service:service_notify:sound",
                from_user=SimpleNamespace(id=12345),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            await self.callback_handler("on_service_selected")(
                retry_service_callback
            )
            retry_menu = retry_service_callback.message.answer.await_args.kwargs[
                "reply_markup"
            ]
            cancel_button = next(
                button
                for row in retry_menu.inline_keyboard
                for button in row
                if button.text == "✖️ Закрыть настройку"
            )
            cancel_callback = SimpleNamespace(
                data=cancel_button.callback_data,
                from_user=SimpleNamespace(id=12345),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            await self.callback_handler("on_notification_setting_cancel")(
                cancel_callback
            )
            cancelled_service = self.registry.get_service("sound")
            self.assertEqual(
                self.registry.list_group_reminders("sound")[0][
                    "reminder_days"
                ],
                3,
            )

        asyncio.run(run())

    def test_group_reminder_preview_is_private_and_returns_to_card(self):
        async def run():
            self.context.get_group_reminder_preview = AsyncMock(
                return_value={
                    "date": "25.10.2026",
                    "send_date": "23.10.2026",
                    "time": "18:00",
                    "text": "Отрендеренное сообщение группы " + "🙂" * 5000,
                    "has_data": True,
                    "error": None,
                }
            )
            command = self.message(67890, "/service_notify")
            await self.admin_router.message.handlers[6].callback(command)
            service_callback = SimpleNamespace(
                data="service:service_notify:sound",
                from_user=SimpleNamespace(id=67890),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private", id=67890),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            await self.callback_handler("on_service_selected")(service_callback)
            settings = service_callback.message.answer.await_args.kwargs[
                "reply_markup"
            ]
            reminders_button = next(
                button
                for row in settings.inline_keyboard
                for button in row
                if button.callback_data.startswith("group_reminder:list:sound:")
            )
            reminders_callback = SimpleNamespace(
                data=reminders_button.callback_data,
                from_user=SimpleNamespace(id=67890),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private", id=67890),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            await self.callback_handler("on_group_reminder_action")(
                reminders_callback
            )
            reminder_button = next(
                button
                for row in reminders_callback.message.answer.await_args.kwargs[
                    "reply_markup"
                ].inline_keyboard
                for button in row
                if button.callback_data.startswith(
                    "group_reminder:open:sound:"
                )
            )
            reminder_callback = SimpleNamespace(
                data=reminder_button.callback_data,
                from_user=SimpleNamespace(id=67890),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private", id=67890),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            with patch(
                "bot.handlers.reminder_occurrence",
                return_value=(date(2026, 10, 25), date(2026, 10, 24)),
            ):
                await self.callback_handler("on_group_reminder_action")(
                    reminder_callback
                )
            card_markup = reminder_callback.message.answer.await_args.kwargs[
                "reply_markup"
            ]
            for label in ("👁 Предпросмотр", "📤 Отправить сейчас", "🗑 Удалить"):
                row = next(
                    row for row in card_markup.inline_keyboard
                    if any(button.text == label for button in row)
                )
                self.assertEqual([button.text for button in row], [label])
            card_text = reminder_callback.message.answer.await_args.args[0]
            self.assertIn("📅 Срок: за 1 дн.", card_text)
            self.assertIn("✉️ Отправка: 24.10.2026 в 18:00", card_text)
            preview_button = next(
                button
                for row in card_markup.inline_keyboard
                for button in row
                if button.text == "👁 Предпросмотр"
            )
            reminder_id = self.registry.list_group_reminders("sound")[0]["id"]
            self.assertEqual(
                preview_button.callback_data,
                f"group_reminder:preview:sound:{reminder_id}:"
                f"{preview_button.callback_data.split(':')[-1]}",
            )
            session_id = preview_button.callback_data.split(":")[-1]
            stale_preview = SimpleNamespace(
                data=(
                    f"group_reminder:preview:sound:{reminder_id}:"
                    f"{session_id}stale"
                ),
                from_user=SimpleNamespace(id=67890),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private", id=67890),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            await self.callback_handler("on_group_reminder_action")(
                stale_preview
            )
            self.context.get_group_reminder_preview.assert_not_awaited()

            preview_callback = SimpleNamespace(
                data=preview_button.callback_data,
                from_user=SimpleNamespace(id=67890),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private", id=67890),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            await self.callback_handler("on_group_reminder_action")(
                preview_callback
            )

            self.context.get_group_reminder_preview.assert_awaited_once_with(
                reminder_id
            )
            preview_text = preview_callback.message.answer.await_args.args[0]
            self.assertIn("👁 Предпросмотр · ЗВУК", preview_text)
            self.assertIn("📅 Собрание: 25.10.2026", preview_text)
            self.assertIn("✉️ Отправка: 23.10.2026 в 18:00", preview_text)
            self.assertIn("Отрендеренное сообщение группы", preview_text)
            self.assertIn("... (текст обрезан)", preview_text)
            back_button = preview_callback.message.answer.await_args.kwargs[
                "reply_markup"
            ].inline_keyboard[0][0]
            self.assertEqual(back_button.text, "⬅️ К напоминанию")
            self.assertTrue(
                back_button.callback_data.startswith(
                    "group_reminder:back:sound:"
                )
            )
            self.context.bot.send_message.assert_not_awaited()

            back_callback = SimpleNamespace(
                data=back_button.callback_data,
                from_user=SimpleNamespace(id=67890),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private", id=67890),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            await self.callback_handler("on_group_reminder_action")(
                back_callback
            )
            card_markup = back_callback.message.answer.await_args.kwargs[
                "reply_markup"
            ]
            preview_button = next(
                button
                for row in card_markup.inline_keyboard
                for button in row
                if button.text == "👁 Предпросмотр"
            )
            loader_started = asyncio.Event()
            release_loader = asyncio.Event()

            async def slow_preview(_reminder_id):
                loader_started.set()
                await release_loader.wait()
                return {
                    "date": "25.10.2026",
                    "time": "18:00",
                    "text": "Старый медленный предпросмотр",
                    "has_data": True,
                    "error": None,
                }

            self.context.get_group_reminder_preview.side_effect = slow_preview
            slow_callback = SimpleNamespace(
                data=preview_button.callback_data,
                from_user=SimpleNamespace(id=67890),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private", id=67890),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            slow_preview_task = asyncio.create_task(
                self.callback_handler("on_group_reminder_action")(slow_callback)
            )
            await loader_started.wait()
            interrupt_callback = SimpleNamespace(
                data=f"group_reminder:back:sound:{session_id}",
                from_user=SimpleNamespace(id=67890),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private", id=67890),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            await self.callback_handler("on_group_reminder_action")(
                interrupt_callback
            )
            release_loader.set()
            await slow_preview_task
            self.assertEqual(slow_callback.message.answer.await_count, 1)
            restored_card = interrupt_callback.message.answer.await_args.args[0]
            self.assertIn("<b>📝 Напоминание · ЗВУК</b>", restored_card)
            self.assertNotIn(
                "Старый медленный предпросмотр",
                restored_card,
            )

            card_markup = interrupt_callback.message.answer.await_args.kwargs[
                "reply_markup"
            ]
            preview_button = next(
                button
                for row in card_markup.inline_keyboard
                for button in row
                if button.text == "👁 Предпросмотр"
            )
            first_answer_started = asyncio.Event()
            second_answer_started = asyncio.Event()
            release_first_answer = asyncio.Event()
            release_second_answer = asyncio.Event()
            latest_loader_started = asyncio.Event()
            release_latest_loader = asyncio.Event()

            async def delayed_answer(release, started):
                started.set()
                await release.wait()

            async def delay_first_answer():
                await delayed_answer(
                    release_first_answer, first_answer_started
                )

            async def delay_second_answer():
                await delayed_answer(
                    release_second_answer, second_answer_started
                )

            async def latest_preview(_reminder_id):
                latest_loader_started.set()
                await release_latest_loader.wait()
                return {
                    "date": "25.10.2026",
                    "send_date": "24.10.2026",
                    "time": "18:00",
                    "text": "Только результат нового запроса",
                    "has_data": True,
                    "error": None,
                }

            self.context.get_group_reminder_preview.reset_mock()
            self.context.get_group_reminder_preview.side_effect = latest_preview
            first_request = SimpleNamespace(
                data=preview_button.callback_data,
                from_user=SimpleNamespace(id=67890),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private", id=67890),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(side_effect=delay_first_answer),
            )
            second_request = SimpleNamespace(
                data=preview_button.callback_data,
                from_user=SimpleNamespace(id=67890),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private", id=67890),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(side_effect=delay_second_answer),
            )
            first_request_task = asyncio.create_task(
                self.callback_handler("on_group_reminder_action")(
                    first_request
                )
            )
            await first_answer_started.wait()
            second_request_task = asyncio.create_task(
                self.callback_handler("on_group_reminder_action")(
                    second_request
                )
            )
            await second_answer_started.wait()
            release_second_answer.set()
            await latest_loader_started.wait()
            release_first_answer.set()
            await first_request_task
            release_latest_loader.set()
            await second_request_task
            self.assertEqual(
                self.context.get_group_reminder_preview.await_count,
                1,
            )
            self.assertEqual(first_request.message.answer.await_count, 0)
            self.assertIn(
                "Только результат нового запроса",
                second_request.message.answer.await_args.args[0],
            )

            list_callback = SimpleNamespace(
                data=f"group_reminder:list:sound:{session_id}",
                from_user=SimpleNamespace(id=67890),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private", id=67890),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            await self.callback_handler("on_group_reminder_action")(
                list_callback
            )
            open_button = next(
                button
                for row in list_callback.message.answer.await_args.kwargs[
                    "reply_markup"
                ].inline_keyboard
                for button in row
                if button.callback_data.startswith(
                    "group_reminder:open:sound:"
                )
            )
            reopen_callback = SimpleNamespace(
                data=open_button.callback_data,
                from_user=SimpleNamespace(id=67890),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private", id=67890),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            await self.callback_handler("on_group_reminder_action")(
                reopen_callback
            )
            preview_button = next(
                button
                for row in reopen_callback.message.answer.await_args.kwargs[
                    "reply_markup"
                ].inline_keyboard
                for button in row
                if button.text == "👁 Предпросмотр"
            )
            self.context.get_group_reminder_preview.side_effect = (
                ValueError("Групповое напоминание больше не существует.")
            )
            deleted_callback = SimpleNamespace(
                data=preview_button.callback_data,
                from_user=SimpleNamespace(id=67890),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private", id=67890),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            await self.callback_handler("on_group_reminder_action")(
                deleted_callback
            )
            self.assertIn(
                "Групповое напоминание больше не существует.",
                deleted_callback.message.answer.await_args.args[0],
            )
            self.assertEqual(deleted_callback.answer.await_count, 1)
            deleted_markup = deleted_callback.message.answer.await_args.kwargs[
                "reply_markup"
            ]
            self.assertEqual(
                deleted_markup.inline_keyboard[0][0].text,
                "⬅️ К напоминаниям",
            )

            list_callback = SimpleNamespace(
                data=f"group_reminder:list:sound:{session_id}",
                from_user=SimpleNamespace(id=67890),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private", id=67890),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            await self.callback_handler("on_group_reminder_action")(
                list_callback
            )
            open_button = next(
                button
                for row in list_callback.message.answer.await_args.kwargs[
                    "reply_markup"
                ].inline_keyboard
                for button in row
                if button.callback_data.startswith(
                    "group_reminder:open:sound:"
                )
            )
            open_callback = SimpleNamespace(
                data=open_button.callback_data,
                from_user=SimpleNamespace(id=67890),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private", id=67890),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            await self.callback_handler("on_group_reminder_action")(
                open_callback
            )
            preview_button = next(
                button
                for row in open_callback.message.answer.await_args.kwargs[
                    "reply_markup"
                ].inline_keyboard
                for button in row
                if button.text == "👁 Предпросмотр"
            )
            self.context.get_group_reminder_preview.side_effect = RuntimeError(
                "corrupt workbook"
            )
            failed_callback = SimpleNamespace(
                data=preview_button.callback_data,
                from_user=SimpleNamespace(id=67890),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private", id=67890),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            await self.callback_handler("on_group_reminder_action")(
                failed_callback
            )
            self.assertIn(
                "Не удалось загрузить график. Попробуйте позже.",
                failed_callback.message.answer.await_args.args[0],
            )
            self.assertEqual(failed_callback.answer.await_count, 1)
            self.context.bot.send_message.assert_not_awaited()

        asyncio.run(run())

    async def _click(self, message, predicate, actor_id=12345):
        markup = message.answer.await_args.kwargs["reply_markup"]
        button = next(
            button
            for row in markup.inline_keyboard
            for button in row
            if predicate(button)
        )
        callback = SimpleNamespace(
            data=button.callback_data,
            from_user=SimpleNamespace(id=actor_id),
            message=SimpleNamespace(
                chat=SimpleNamespace(type="private", id=actor_id),
                answer=AsyncMock(),
            ),
            answer=AsyncMock(),
        )
        if button.callback_data.startswith("notify_back:"):
            handler_name = "on_notification_edit_back"
        elif button.callback_data.startswith("svc_tasks:"):
            handler_name = "on_service_tasks"
        elif button.callback_data.startswith("notify_cancel:"):
            handler_name = "on_notification_setting_cancel"
        elif button.callback_data.startswith("rdy:"):
            handler_name = "on_readiness"
        elif button.callback_data.startswith("psum:"):
            handler_name = "on_personal_summary_page"
        elif button.callback_data.startswith("afield:"):
            handler_name = "on_assignment_field"
        elif button.callback_data.startswith("ui_page:"):
            handler_name = "on_dialog_page"
        else:
            handler_name = "on_group_reminder_action"
        await self.callback_handler(handler_name)(callback)
        return callback

    async def _type(self, text, actor_id=12345):
        parameter_handler = next(
            item.callback
            for item in self.admin_router.message.handlers
            if item.callback.__name__ == "on_service_parameter"
        )
        message = self.message(actor_id, text)
        await parameter_handler(message)
        return message

    async def _open_sound_reminders(self, actor_id=12345):
        self.registry.add_service_admin(67890, "sound", actor_id)
        command = self.message(actor_id, "/service_notify")
        await self.admin_router.message.handlers[6].callback(command)
        selection = SimpleNamespace(
            data="service:service_notify:sound",
            from_user=SimpleNamespace(id=actor_id),
            message=SimpleNamespace(
                chat=SimpleNamespace(type="private"),
                answer=AsyncMock(),
            ),
            answer=AsyncMock(),
        )
        await self.callback_handler("on_service_selected")(selection)
        return await self._click(
            selection.message,
            lambda button: button.text.startswith("⏰ Напоминания"),
            actor_id,
        )

    def _sound_slots(self):
        return sorted(
            (
                (item["reminder_days"], item["reminder_time"], item["body"])
                for item in self.registry.list_group_reminders("sound")
            ),
            key=lambda slot: (slot[0], slot[1], slot[2] or ""),
        )

    async def _open_task_notifications(self, task=None):
        self.registry.add_service_admin(67890, "sound", 12345)
        opened = SimpleNamespace(
            data="svc_tasks:open:sound",
            from_user=SimpleNamespace(id=12345),
            message=self.message(12345, ""),
            answer=AsyncMock(),
        )
        await self.callback_handler("on_service_tasks")(opened)
        if task is None:
            return await self._click(
                opened.message, lambda button: button.text == "⏰ Всё служение"
            )
        card = await self._click(
            opened.message, lambda button: button.text == f"🟢 {task}"
        )
        return await self._click(
            card.message,
            lambda button: button.text == "⏰ Напоминания и шаблоны",
        )

    def test_task_reminders_filter_weekday_status_and_empty_list(self):
        async def run():
            self.registry.set_service_task_day(67890, "sound", "звук", -4)
            self.registry.set_service_default_template(
                67890, "sound", "Общий шаблон {service_name}"
            )
            task_id = self.registry.add_group_reminder(
                67890, "sound", 1, "10:30", "Текст задачи\nВторая строка",
                task="звук",
            )
            self.registry.update_group_reminder(
                67890, task_id, enabled=False
            )
            root = await self._open_task_notifications("звук")
            menu = await self._click(
                root.message, lambda button: button.text.startswith("⏰ Напоминания")
            )
            text = menu.message.answer.await_args.args[0]
            self.assertIn("звук · напоминания", text)
            self.assertIn("1. ⏸ вт · 10:30 · Текст задачи", text)
            self.assertNotIn("Общий шаблон", text)
            self.assertNotIn("Вторая строка", text)
            card = await self._click(menu.message, lambda button: button.text == "1")
            self.assertIn("дня задачи — ср", card.message.answer.await_args.args[0])

            root = await self._open_task_notifications()
            menu = await self._click(
                root.message, lambda button: button.text.startswith("⏰ Напоминания")
            )
            text = menu.message.answer.await_args.args[0]
            self.assertIn("Всё служение · напоминания", text)
            self.assertIn("1. ✅ сб · 18:00 · Общий шаблон", text)
            self.assertNotIn("Текст задачи", text)

            root = await self._open_task_notifications("сборка")
            menu = await self._click(
                root.message, lambda button: button.text.startswith("⏰ Напоминания")
            )
            self.assertIn("Напоминаний нет", menu.message.answer.await_args.args[0])
            self.assertTrue(any(
                data.startswith("group_reminder:add:")
                for data in self.callback_data_list(menu.message)
            ))
            self.context.bot.send_message.assert_not_awaited()

        asyncio.run(run())

    def test_task_reminder_creation_keeps_scope_and_same_task_merge(self):
        async def run():
            self._preview_with_data()
            async def add(menu, body):
                await self._click(
                    menu, lambda button: button.text == "➕ Добавить напоминание"
                )
                await self._type("1")
                prompt = await self._type("18:00")
                self.assertIn("{task_responsibles}", prompt.answer.await_args.args[0])
                return await self._type(body)

            root = await self._open_task_notifications("звук")
            menu = await self._click(
                root.message, lambda button: button.text.startswith("⏰ Напоминания")
            )
            card = await add(menu.message, "Звук: {task_responsibles}")
            self.assertIn("📌 Задача: «звук»", card.answer.await_args.args[0])
            # Same time as the general reminder is not a duplicate.
            self.assertEqual(len(self.registry.list_group_reminders("sound")), 2)
            menu = await self._click(
                card, lambda button: button.text == "⬅️ К напоминаниям"
            )
            offer = await add(menu.message, "Второй текст")
            self.assertIn("🔗 Объединить", [
                button.text for row in offer.answer.await_args.kwargs[
                    "reply_markup"
                ].inline_keyboard for button in row
            ])
            separate = await self._click(
                offer, lambda button: button.text == "➕ Отдельным сообщением"
            )
            reminders = self.registry.list_group_reminders("sound")
            self.assertEqual(
                [item["task"] for item in reminders], [None, "звук", "звук"]
            )
            self.assertIn("«звук»", separate.message.answer.await_args.args[0])

        asyncio.run(run())

    def test_scoped_reminder_task_change_moves_list_to_new_task(self):
        async def run():
            self.registry.add_group_reminder(
                67890, "sound", 2, "10:00", task="звук"
            )
            root = await self._open_task_notifications("звук")
            menu = await self._click(
                root.message, lambda button: button.text.startswith("⏰ Напоминания")
            )
            card = await self._click(menu.message, lambda button: button.text == "1")
            choices = await self._click(
                card.message, lambda button: button.text == "📌 Задача"
            )
            moved = await self._click(
                choices.message, lambda button: button.text.endswith(" сборка")
            )
            back = await self._click(
                moved.message, lambda button: button.text == "⬅️ К напоминаниям"
            )
            self.assertIn("сборка · напоминания", back.message.answer.await_args.args[0])
            self.assertEqual(len(self.callback_data_list(back.message)), 4)
            self.assertIn(
                "group_reminder:summary:",
                "\n".join(self.callback_data_list(back.message)),
            )

        asyncio.run(run())

    def test_taskless_service_has_general_notifications_entry(self):
        async def run():
            self.registry.add_service_admin(67890, "books", 12345)
            opened = SimpleNamespace(
                data="svc_tasks:open:books",
                from_user=SimpleNamespace(id=12345),
                message=self.message(12345, ""), answer=AsyncMock(),
            )
            await self.callback_handler("on_service_tasks")(opened)
            root = await self._click(
                opened.message, lambda button: button.text == "⏰ Всё служение"
            )
            self.assertIn("Всё служение", root.message.answer.await_args.args[0])
            menu = await self._click(
                root.message, lambda button: button.text.startswith("⏰ Напоминания")
            )
            self.assertTrue(any(
                data.startswith("group_reminder:add:books:")
                for data in self.callback_data_list(menu.message)
            ))

        asyncio.run(run())

    def test_reminder_list_pages_match_numbers_and_reject_old_page_buttons(self):
        async def run():
            for index in range(1, 8):
                self.registry.add_group_reminder(
                    67890, "sound", 1, f"{index:02}:00",
                    f"Текст {index} " + "длинный " * 20,
                )
            menu = await self._open_sound_reminders()
            labels = [
                button.text for row in menu.message.answer.await_args.kwargs[
                    "reply_markup"
                ].inline_keyboard for button in row
            ]
            self.assertEqual([x for x in labels if x.isdecimal()],
                             ["1", "2", "3", "4", "5", "6"])
            self.assertIn("Страница 1/2", menu.message.answer.await_args.args[0])
            self.assertIn("…", menu.message.answer.await_args.args[0])
            page = await self._click(menu.message, lambda button: button.text == "➡️ Далее")
            self.assertIn("7. ✅", page.message.answer.await_args.args[0])
            stale = await self._click(menu.message, lambda button: button.text == "1")
            self.assertIn("устарел", stale.answer.await_args.args[0])
            stale.message.answer.assert_not_awaited()
            card = await self._click(page.message, lambda button: button.text == "7")
            self.assertIn("Текст 7", card.message.answer.await_args.args[0])
            back = await self._click(
                card.message, lambda button: button.text == "⬅️ К напоминаниям"
            )
            self.assertIn("Страница 2/2", back.message.answer.await_args.args[0])
            previous = await self._click(
                back.message, lambda button: button.text == "⬅️ Предыдущая"
            )
            self.assertIn("Страница 1/2", previous.message.answer.await_args.args[0])
            for callback in self.callback_data_list(previous.message):
                self.assertLessEqual(len(callback.encode("utf-8")), 64)

        asyncio.run(run())

    def test_notification_close_returns_card_rejects_stale_and_revoked_admin(self):
        async def run():
            old = await self._open_task_notifications("звук")
            current = await self._open_task_notifications()
            stale = await self._click(
                old.message, lambda button: button.text == "✖️ Закрыть настройку"
            )
            self.assertIn("устарел", stale.answer.await_args.args[0])
            stale.message.answer.assert_not_awaited()
            closed = await self._click(
                current.message, lambda button: button.text == "✖️ Закрыть настройку"
            )
            self.assertIn("⚙️ ЗВУК", closed.message.answer.await_args.args[0])
            self.assertIn("svc_tasks:open:sound", self.callback_data_list(closed.message))
            root = await self._open_task_notifications()
            self.registry.remove_service_admin(67890, "sound", 12345)
            denied = await self._click(
                root.message, lambda button: button.text == "✖️ Закрыть настройку"
            )
            self.assertIn("Недостаточно прав", denied.answer.await_args.args[0])
            denied.message.answer.assert_not_awaited()

        asyncio.run(run())

    def test_guided_notification_setup_adds_independent_reminder(self):
        async def run():
            menu = await self._open_sound_reminders()
            add = await self._click(
                menu.message,
                lambda button: button.text == "➕ Добавить напоминание",
            )
            # An existing «1 дн. 18:00» reminder must not trigger a warning
            # before the new reminder's schedule is entered.
            self.assertIn(
                "шаг 1 из 3", add.message.answer.await_args.args[0]
            )
            self.assertNotIn(
                "уже есть", add.message.answer.await_args.args[0]
            )
            await self._type("3")
            time_step = await self._type("10:30")
            self.assertIn(
                "шаг 3 из 3", time_step.answer.await_args.args[0]
            )
            card = await self._click(
                time_step,
                lambda button: button.text == "📝 Шаблон по умолчанию",
            )

            self.assertEqual(
                self._sound_slots(),
                [(1, "18:00", None), (3, "10:30", None)],
            )
            self.assertIn(
                "📝 Напоминание",
                card.message.answer.await_args.args[0],
            )

        asyncio.run(run())

    def test_send_now_posts_preview_to_service_chat(self):
        async def run():
            self.registry.set_group_chat(67890, "sound", -100555, enabled=True)
            self.context.get_group_reminder_preview = AsyncMock(return_value={
                "has_data": True, "text": "Напоминание сейчас", "date": "25.10.2026",
                "event_date": "2026-10-25", "schedule_state": ["plan-a"],
            })
            self.context.group_preview_state = MagicMock(return_value=["plan-a"])
            menu = await self._open_sound_reminders()
            card = await self._click(
                menu.message, lambda button: button.text == "1"
            )
            confirm = await self._click(
                card.message, lambda button: button.text == "📤 Отправить сейчас"
            )
            self.assertIn("Напоминание сейчас", confirm.message.answer.await_args.args[0])
            self.context.bot.send_message.reset_mock()
            sent = await self._click(
                confirm.message, lambda button: button.text == "✅ Отправить"
            )
            self.assertEqual(
                self.context.bot.send_message.await_args.args[0], -100555
            )
            self.assertIn("Отправлено", sent.answer.await_args.args[0])
            changed_plan = await self._click(
                card.message, lambda button: button.text == "📤 Отправить сейчас"
            )
            self.context.group_preview_state.return_value = ["plan-b"]
            self.context.bot.send_message.reset_mock()
            rejected = await self._click(
                changed_plan.message, lambda button: button.text == "✅ Отправить"
            )
            self.context.bot.send_message.assert_not_awaited()
            self.assertIn("График изменился", rejected.answer.await_args.args[0])
            self.context.group_preview_state.return_value = ["plan-a"]
            # Смена чата после предпросмотра — нужен новый предпросмотр.
            again = await self._click(
                card.message, lambda button: button.text == "📤 Отправить сейчас"
            )
            self.registry.set_group_chat(67890, "sound", -100777, enabled=True)
            self.context.bot.send_message.reset_mock()
            await self._click(
                again.message, lambda button: button.text == "✅ Отправить"
            )
            self.context.bot.send_message.assert_not_awaited()
            # Повторное нажатие той же кнопки ничего не отправляет.
            self.context.bot.send_message.reset_mock()
            await self._click(
                confirm.message, lambda button: button.text == "✅ Отправить"
            )
            self.context.bot.send_message.assert_not_awaited()

        asyncio.run(run())

    def test_assignment_field_picker_preserves_text_and_rejects_stale_choices(self):
        from bot.assignment_fields import assignment_field

        async def run():
            self.registry.set_group_chat(67890, "order", -100556, enabled=True)
            self.registry.set_group_chat(67890, "kitchen", -100557, enabled=True)
            self.registry.add_service_task(67890, "kitchen", "Скатерти")
            reminder = self.registry.list_group_reminders("sound")[0]
            before = reminder["body"]
            menu = await self._open_sound_reminders()
            card = await self._click(menu.message, lambda button: button.text == "1")
            edit = await self._click(card.message, lambda button: button.text == "✏️ Текст")
            sources = await self._click(
                edit.message,
                lambda button: button.text == "🔗 Назначения другого служения",
            )
            order_name = self.registry.get_service("order")["name"]
            choices = await self._click(
                sources.message, lambda button: button.text.endswith(order_name),
            )
            field = await self._click(
                choices.message, lambda button: button.text.endswith("старший"),
            )
            self.assertIn(
                assignment_field("order", "старший"),
                field.message.answer.await_args.args[0],
            )
            self.assertEqual(self.registry.get_group_reminder(reminder["id"])["body"], before)
            for value in self.callback_data_list(choices.message):
                self.assertLessEqual(len(value.encode()), 64)
            stale = await self._click(
                choices.message, lambda button: button.text.endswith("Все задачи"),
            )
            self.assertIn("устарел", stale.answer.await_args.args[0])
            saved = await self._type(
                "Назначения:\n" + assignment_field("order", "старший"),
            )
            self.assertIn("Напоминание", saved.answer.await_args.args[0])
            self.assertEqual(
                self.registry.get_group_reminder(reminder["id"])["body"],
                "Назначения:\n" + assignment_field("order", "старший"),
            )
            edit = await self._click(saved, lambda button: button.text == "✏️ Текст")
            sources = await self._click(
                edit.message,
                lambda button: button.text == "🔗 Назначения другого служения",
            )
            kitchen_name = self.registry.get_service("kitchen")["name"]
            tasks = await self._click(
                sources.message, lambda button: button.text.endswith(kitchen_name),
            )
            labels = [
                button.text for row in tasks.message.answer.await_args.kwargs[
                    "reply_markup"
                ].inline_keyboard for button in row
                if ":task:" in button.callback_data
            ]
            tasks = await self._click(
                tasks.message,
                lambda button: button.callback_data.startswith("ui_page:")
                and "➡️" in button.text,
            )
            labels.extend(
                button.text for row in tasks.message.answer.await_args.kwargs[
                    "reply_markup"
                ].inline_keyboard for button in row
                if ":task:" in button.callback_data
            )
            self.assertEqual(
                [next(name for name in ("Все задачи", "старший", "в смене", "Скатерти", "пироги")
                      if label.endswith(name)) for label in labels],
                ["Все задачи", "старший", "в смене", "Скатерти", "пироги"],
            )
            self.registry.remove_service_admin(67890, "sound", 12345)
            denied = await self._click(
                tasks.message, lambda button: button.text.endswith("пироги"),
            )
            self.assertIn("Недостаточно прав", denied.answer.await_args.args[0])
            denied.message.answer.assert_not_awaited()

        asyncio.run(run())

    def test_send_now_rechecks_related_assignments_and_passes_manual_context(self):
        async def run():
            self.registry.set_group_chat(67890, "sound", -100555, enabled=True)
            self.context.get_group_reminder_preview = AsyncMock(return_value={
                "has_data": True, "text": "Назначения", "date": "25.10.2026",
                "event_date": "2026-10-25", "schedule_state": ["local"],
                "assignment_state": ("2026-10-25", "related-a"),
            })
            self.context.group_preview_state = MagicMock(return_value=["local"])
            self.context.assignment_fields_state = MagicMock(
                return_value=("2026-10-25", "related-b"),
            )
            menu = await self._open_sound_reminders()
            card = await self._click(menu.message, lambda button: button.text == "1")
            confirm = await self._click(
                card.message, lambda button: button.text == "📤 Отправить сейчас",
            )
            self.assertTrue(
                self.context.get_group_reminder_preview.await_args.kwargs["manual"],
            )
            self.context.bot.send_message.reset_mock()
            rejected = await self._click(
                confirm.message, lambda button: button.text == "✅ Отправить",
            )
            self.context.bot.send_message.assert_not_awaited()
            self.assertIn("Связанные назначения", rejected.answer.await_args.args[0])
            again = await self._click(
                card.message, lambda button: button.text == "📤 Отправить сейчас",
            )
            self.registry.set_service_default_template(
                67890, "sound", "Новый шаблон {week_task_assignments}",
            )
            stale = await self._click(
                again.message, lambda button: button.text == "✅ Отправить",
            )
            self.context.bot.send_message.assert_not_awaited()
            self.assertIn("устарел", stale.answer.await_args.args[0])

        asyncio.run(run())

    def test_assignment_picker_pages_and_preserves_new_reminder_draft(self):
        async def run():
            menu = await self._open_sound_reminders()
            await self._click(
                menu.message, lambda button: button.text == "➕ Добавить напоминание",
            )
            await self._type("3")
            text_step = await self._type("10:30")
            services = [
                {"id": f"source{index}", "name": f"Служение {index}", "enabled": True}
                for index in range(15)
            ]
            with patch.object(self.registry, "list_services", return_value=services):
                picker = await self._click(
                    text_step,
                    lambda button: button.text == "🔗 Назначения другого служения",
                )
            buttons = [
                button for row in picker.message.answer.await_args.kwargs[
                    "reply_markup"
                ].inline_keyboard for button in row
            ]
            page_button = next(
                button for button in buttons
                if button.callback_data.startswith("ui_page:") and "➡️" in button.text
            )
            page = await self._click(
                picker.message, lambda button: button is page_button,
            )
            for value in self.callback_data_list(page.message):
                self.assertLessEqual(len(value.encode()), 64)
            await self._type("Неделя:\n{week_task_assignments}")
            self.assertIn((3, "10:30", "Неделя:\n{week_task_assignments}"), self._sound_slots())
            stale = await self._click(
                picker.message, lambda button: button.callback_data.startswith("afield:"),
            )
            self.assertIn("устарел", stale.answer.await_args.args[0])

        asyncio.run(run())

    def test_reminder_card_navigates_between_service_reminders(self):
        async def run():
            self.registry.add_group_reminder(67890, "sound", 3, "10:30")
            menu = await self._open_sound_reminders()
            first = await self._click(
                menu.message, lambda button: button.text == "1"
            )
            text = first.message.answer.await_args.args[0]
            self.assertIn("📋 Напоминание 1 из 2", text)
            labels = [
                button.text
                for row in first.message.answer.await_args.kwargs[
                    "reply_markup"
                ].inline_keyboard
                for button in row
            ]
            self.assertIn("➡️ Следующее", labels)
            self.assertNotIn("⬅️ Предыдущее", labels)

            second = await self._click(
                first.message, lambda button: button.text == "➡️ Следующее"
            )
            text = second.message.answer.await_args.args[0]
            self.assertIn("за 3 дн.", text)
            self.assertIn("📋 Напоминание 2 из 2", text)
            back = await self._click(
                second.message, lambda button: button.text == "⬅️ Предыдущее"
            )
            self.assertIn("за 1 дн.", back.message.answer.await_args.args[0])

            stale = await self._click(
                first.message, lambda button: button.text == "➡️ Следующее"
            )
            self.assertIsNotNone(stale.answer.await_args)

        asyncio.run(run())

    def test_reminder_task_can_be_chosen_on_card(self):
        async def run():
            menu = await self._open_sound_reminders()
            card = await self._click(
                menu.message, lambda button: button.text == "1"
            )
            self.assertIn("📌 Задача: все задачи", card.message.answer.await_args.args[0])
            tasks = await self._click(
                card.message, lambda button: button.text == "📌 Задача"
            )
            picked = await self._click(
                tasks.message, lambda button: button.text.endswith(" звук")
            )
            self.assertIn("📌 Задача: «звук»", picked.message.answer.await_args.args[0])
            reminder = self.registry.list_group_reminders("sound")[0]
            self.assertEqual(reminder["task"], "звук")
            stale = await self._click(
                tasks.message, lambda button: button.text == "📋 Все задачи"
            )
            self.assertIn("устарел", stale.answer.await_args.args[0])
            again = await self._click(
                picked.message, lambda button: button.text == "📌 Задача"
            )
            await self._click(
                again.message, lambda button: button.text == "📋 Все задачи"
            )
            self.assertIsNone(self.registry.list_group_reminders("sound")[0]["task"])

        asyncio.run(run())

    def test_chat_screens_have_way_back(self):
        async def run():
            menu = SimpleNamespace(
                data="service:service_chat:sound",
                from_user=SimpleNamespace(id=67890),
                message=self.message(67890, ""),
                answer=AsyncMock(),
            )
            await self.callback_handler("on_service_selected")(menu)
            labels = [
                button.text
                for row in menu.message.answer.await_args.kwargs[
                    "reply_markup"
                ].inline_keyboard
                for button in row
            ]
            self.assertIn("⬅️ К карточке служения", labels)

            picker = SimpleNamespace(
                data="service:service_chat_set:sound",
                from_user=SimpleNamespace(id=67890),
                message=self.message(67890, ""),
                answer=AsyncMock(),
            )
            await self.callback_handler("on_service_selected")(picker)
            keyboard = picker.message.answer.await_args.kwargs["reply_markup"]
            self.assertEqual(keyboard.keyboard[1][0].text, "⬅️ Назад")
            back = self.message(67890, "⬅️ Назад")
            await self.message_handler("on_service_parameter")(back)
            self.assertIn("выберите действие", back.answer.await_args.args[0])

        asyncio.run(run())

    def test_here_command_stores_chat_topic_for_service_admin(self):
        async def run():
            handler = self.message_handler("on_here_command")
            private = self.message(67890, "/here")
            await handler(private)
            self.assertIn("в нужной теме", private.answer.await_args.args[0])

            self.registry.set_group_chat(67890, "sound", -100777, enabled=True)
            self.registry.set_group_chat(67890, "broadcast", -100777, enabled=True)
            group = self.message(67890, "/here", chat_type="supergroup")
            group.chat.id = -100777
            group.message_thread_id = 42
            group.is_topic_message = True
            await handler(group)
            self.assertIn("/here sound", group.answer.await_args.args[0])

            card = SimpleNamespace(
                data="service:service_settings:sound",
                from_user=SimpleNamespace(id=67890),
                message=self.message(67890, ""),
                answer=AsyncMock(),
            )
            card.message.answer.return_value = SimpleNamespace(message_id=77)
            await self.callback_handler("on_service_selected")(card)
            other = SimpleNamespace(
                data="service:service_settings:broadcast",
                from_user=SimpleNamespace(id=67890),
                message=self.message(67890, ""),
                answer=AsyncMock(),
            )
            other.message.answer.return_value = SimpleNamespace(message_id=88)
            await self.callback_handler("on_service_selected")(other)
            group.text = "/here Звук"
            # Личный чат недоступен: ссылка на старую карточку сохраняется.
            self.context.bot.send_message.side_effect = RuntimeError("blocked")
            await handler(group)
            self.context.bot.send_message.side_effect = None
            self.context.bot.send_message.return_value = SimpleNamespace(
                message_id=99
            )
            self.context.bot.send_message.reset_mock()
            self.context.bot.delete_message.reset_mock()
            await handler(group)
            deleted = [
                call.kwargs["message_id"]
                for call in self.context.bot.delete_message.await_args_list
            ]
            self.assertEqual(deleted, [77])
            self.assertIn("в эту тему", group.answer.await_args.args[0])
            private_texts = [
                call.args[1]
                for call in self.context.bot.send_message.await_args_list
                if call.args[0] == 67890
            ]
            self.assertIn("✅ Тема чата сохранена", private_texts[0])
            self.assertIn("🧵 Тема", private_texts[-1])
            self.context.bot.delete_message.assert_awaited()
            self.assertEqual(
                self.registry.get_service("sound")["group_thread_id"], 42
            )
            self.assertIsNone(
                self.registry.get_service("broadcast")["group_thread_id"]
            )

            stranger = self.message(55555, "/here sound", chat_type="supergroup")
            stranger.chat.id = -100777
            await handler(stranger)
            self.assertIn("только администратор", stranger.answer.await_args.args[0])

            general = self.message(67890, "/here sound", chat_type="supergroup")
            general.chat.id = -100777
            await handler(general)
            self.assertIn("в общую тему", general.answer.await_args.args[0])
            self.assertIsNone(
                self.registry.get_service("sound")["group_thread_id"]
            )

        asyncio.run(run())

    def test_cancelled_add_wizard_creates_nothing(self):
        async def run():
            menu = await self._open_sound_reminders()
            add = await self._click(
                menu.message,
                lambda button: button.text == "➕ Добавить напоминание",
            )
            await self._type("2")
            time_step = await self._type("09:00")
            back = await self._click(
                time_step,
                lambda button: button.callback_data.startswith("notify_back:"),
            )

            self.assertEqual(self._sound_slots(), [(1, "18:00", None)])
            self.assertIn(
                "напоминания", back.message.answer.await_args.args[0]
            )
            del add

        asyncio.run(run())

    def test_add_wizard_rejects_invalid_days_and_time(self):
        async def run():
            menu = await self._open_sound_reminders()
            await self._click(
                menu.message,
                lambda button: button.text == "➕ Добавить напоминание",
            )
            wrong_days = await self._type("9")
            self.assertIn(
                "от 0 до 6", wrong_days.answer.await_args.args[0]
            )
            await self._type("2")
            wrong_time = await self._type("25:00")
            self.assertIn("HH:MM", wrong_time.answer.await_args.args[0])
            self.assertEqual(self._sound_slots(), [(1, "18:00", None)])

        asyncio.run(run())

    def test_same_slot_offers_merge_with_rendered_preview(self):
        async def run():
            self.context.get_group_reminder_preview = AsyncMock(
                return_value={
                    "date": "25.10.2026",
                    "send_date": "24.10.2026",
                    "time": "18:00",
                    "text": "Итоговый текст для группы",
                    "has_data": True,
                    "error": None,
                }
            )
            existing_id = self.registry.list_group_reminders("sound")[0]["id"]
            menu = await self._open_sound_reminders()
            await self._click(
                menu.message,
                lambda button: button.text == "➕ Добавить напоминание",
            )
            await self._type("1")
            await self._type("18:00")
            merge_screen = await self._type("Не забудьте ноты! {sunday_date}")

            screen_text = merge_screen.answer.await_args.args[0]
            self.assertIn("уже есть напоминание", screen_text)
            self.assertIn("Итоговый текст для группы", screen_text)
            self.assertIn("25.10.2026", screen_text)
            loader_args = self.context.get_group_reminder_preview.await_args
            self.assertEqual(loader_args.args[0], existing_id)
            merged_body = loader_args.args[1]
            self.assertTrue(
                merged_body.endswith("\n\nНе забудьте ноты! {sunday_date}")
            )
            self.assertIn("{task_assignments}", merged_body)

            await self._click(
                merge_screen, lambda button: button.text == "🔗 Объединить"
            )
            self.assertEqual(
                self._sound_slots(), [(1, "18:00", merged_body)]
            )

        asyncio.run(run())

    def test_same_slot_can_still_be_saved_separately(self):
        async def run():
            menu = await self._open_sound_reminders()
            await self._click(
                menu.message,
                lambda button: button.text == "➕ Добавить напоминание",
            )
            await self._type("1")
            await self._type("18:00")
            merge_screen = await self._type("Отдельный текст")
            # Without a preview loader the merged template is shown instead.
            self.assertIn(
                "Отдельный текст", merge_screen.answer.await_args.args[0]
            )
            await self._click(
                merge_screen,
                lambda button: button.text == "➕ Отдельным сообщением",
            )
            self.assertEqual(
                self._sound_slots(),
                [(1, "18:00", None), (1, "18:00", "Отдельный текст")],
            )

        asyncio.run(run())

    def test_same_slot_and_same_text_keeps_one_reminder(self):
        async def run():
            menu = await self._open_sound_reminders()
            await self._click(
                menu.message,
                lambda button: button.text == "➕ Добавить напоминание",
            )
            await self._type("1")
            text_step = await self._type("18:00")
            same = await self._click(
                text_step,
                lambda button: button.text == "📝 Шаблон по умолчанию",
            )
            self.assertIn(
                "с таким же текстом", same.message.answer.await_args.args[0]
            )
            await self._click(
                same.message,
                lambda button: button.text == "🔗 Оставить одно",
            )
            self.assertEqual(self._sound_slots(), [(1, "18:00", None)])

        asyncio.run(run())

    def test_merge_cancel_keeps_reminders_unchanged(self):
        async def run():
            menu = await self._open_sound_reminders()
            await self._click(
                menu.message,
                lambda button: button.text == "➕ Добавить напоминание",
            )
            await self._type("1")
            await self._type("18:00")
            merge_screen = await self._type("Другой текст")
            await self._click(
                merge_screen, lambda button: button.text == "✖️ Отмена"
            )
            self.assertEqual(self._sound_slots(), [(1, "18:00", None)])

        asyncio.run(run())

    def _preview_with_data(self):
        async def preview(reminder_id, body=None):
            return {
                "date": "25.10.2026",
                "send_date": "24.10.2026",
                "time": "18:00",
                "text": body or "Текст",
                "has_data": True,
                "error": None,
            }

        self.context.get_group_reminder_preview = AsyncMock(side_effect=preview)

    def test_changing_schedule_onto_taken_slot_merges_into_existing(self):
        async def run():
            self._preview_with_data()
            existing_id = self.registry.list_group_reminders("sound")[0]["id"]
            self.registry.update_group_reminder(
                67890, existing_id, body="Первый текст"
            )
            other_id = self.registry.add_group_reminder(
                67890, "sound", 2, "18:00", "Второй текст"
            )
            menu = await self._open_sound_reminders()
            card = await self._click(
                menu.message,
                lambda button: button.callback_data.startswith(
                    f"group_reminder:open:sound:{other_id}:"
                ),
            )
            await self._click(
                card.message, lambda button: button.text == "📅 Срок"
            )
            merge_screen = await self._type("1")
            self.assertIn(
                "Первый текст\n\nВторой текст",
                merge_screen.answer.await_args.args[0],
            )
            merged = await self._click(
                merge_screen, lambda button: button.text == "🔗 Объединить"
            )

            self.assertEqual(
                self._sound_slots(),
                [(1, "18:00", "Первый текст\n\nВторой текст")],
            )
            self.assertIsNone(self.registry.get_group_reminder(other_id))
            self.assertIn(
                "Первый текст", merged.message.answer.await_args.args[0]
            )

        asyncio.run(run())

    def test_slot_with_other_period_is_not_merged(self):
        async def run():
            self._preview_with_data()
            existing_id = self.registry.list_group_reminders("sound")[0]["id"]
            self.registry.set_group_reminder_period(
                67890, existing_id, date(2099, 1, 4), None,
                today=date(2026, 10, 20),
            )
            other_id = self.registry.add_group_reminder(
                67890, "sound", 2, "18:00", "Второй текст"
            )
            menu = await self._open_sound_reminders()
            card = await self._click(
                menu.message,
                lambda button: button.callback_data.startswith(
                    f"group_reminder:open:sound:{other_id}:"
                ),
            )
            await self._click(
                card.message, lambda button: button.text == "📅 Срок"
            )
            offer = await self._type("1")
            self.assertIn(
                "разными периодами не объединяются",
                offer.answer.await_args.args[0],
            )
            labels = [
                button.text
                for row in offer.answer.await_args.kwargs[
                    "reply_markup"
                ].inline_keyboard
                for button in row
            ]
            self.assertFalse(any("Объединить" in label for label in labels))
            self.assertEqual(
                self.registry.get_group_reminder(other_id)["reminder_days"], 2
            )
            await self._click(offer, lambda button: button.text == "➕ Отдельно")
            self.assertEqual(
                self.registry.get_group_reminder(other_id)["reminder_days"], 1
            )

        asyncio.run(run())

    def test_merge_is_not_offered_without_schedule_preview(self):
        async def run():
            self.context.get_group_reminder_preview = AsyncMock(
                return_value={
                    "date": "25.10.2026", "send_date": "24.10.2026",
                    "time": "18:00", "text": "", "has_data": False,
                    "error": "Не удалось загрузить график. Попробуйте позже.",
                }
            )
            menu = await self._open_sound_reminders()
            await self._click(
                menu.message,
                lambda button: button.text == "➕ Добавить напоминание",
            )
            await self._type("1")
            await self._type("18:00")
            screen = await self._type("Другой текст")

            labels = [
                button.text
                for row in screen.answer.await_args.kwargs[
                    "reply_markup"
                ].inline_keyboard
                for button in row
            ]
            self.assertNotIn("🔗 Объединить", labels)
            self.assertIn("➕ Отдельным сообщением", labels)
            self.assertIn(
                "нельзя проверить длину", screen.answer.await_args.args[0]
            )

        asyncio.run(run())

    def test_disabled_reminder_is_not_a_merge_target(self):
        async def run():
            existing_id = self.registry.list_group_reminders("sound")[0]["id"]
            self.registry.update_group_reminder(
                67890, existing_id, enabled=False
            )
            menu = await self._open_sound_reminders()
            await self._click(
                menu.message,
                lambda button: button.text == "➕ Добавить напоминание",
            )
            await self._type("1")
            text_step = await self._type("18:00")
            await self._click(
                text_step,
                lambda button: button.text == "📝 Шаблон по умолчанию",
            )

            self.assertEqual(
                self._sound_slots(),
                [(1, "18:00", None), (1, "18:00", None)],
            )

        asyncio.run(run())

    def test_old_merge_button_cannot_confirm_newer_offer(self):
        async def run():
            self._preview_with_data()
            menu = await self._open_sound_reminders()
            await self._click(
                menu.message,
                lambda button: button.text == "➕ Добавить напоминание",
            )
            await self._type("1")
            await self._type("18:00")
            first_offer = await self._type("Первый")
            old_merge = next(
                button.callback_data
                for row in first_offer.answer.await_args.kwargs[
                    "reply_markup"
                ].inline_keyboard
                for button in row
                if button.text == "🔗 Объединить"
            )
            await self._click(
                first_offer, lambda button: button.text == "✖️ Отмена"
            )
            rules = self.registry.list_group_reminders("sound")
            session_id = old_merge.rsplit(":", 1)[1]
            await self.callback_handler("on_group_reminder_action")(
                SimpleNamespace(
                    data=f"group_reminder:add:sound:{session_id}",
                    from_user=SimpleNamespace(id=12345),
                    message=SimpleNamespace(
                        chat=SimpleNamespace(type="private", id=12345),
                        answer=AsyncMock(),
                    ),
                    answer=AsyncMock(),
                )
            )
            await self._type("1")
            await self._type("18:00")
            await self._type("Второй")
            stale = SimpleNamespace(
                data=old_merge,
                from_user=SimpleNamespace(id=12345),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private", id=12345),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            await self.callback_handler("on_group_reminder_action")(stale)

            self.assertIn("устарел", stale.answer.await_args.args[0])
            self.assertEqual(
                [item["body"] for item in self.registry.list_group_reminders(
                    "sound"
                )],
                [rules[0]["body"]],
            )

        asyncio.run(run())

    def test_old_notification_buttons_cannot_change_new_session(self):
        async def run():
            async def begin_session():
                command = self.message(67890, "/service_notify")
                await self.admin_router.message.handlers[6].callback(command)
                service_callback = SimpleNamespace(
                    data="service:service_notify:sound",
                    from_user=SimpleNamespace(id=67890),
                    message=SimpleNamespace(
                        chat=SimpleNamespace(type="private"),
                        answer=AsyncMock(),
                    ),
                    answer=AsyncMock(),
                )
                await self.callback_handler("on_service_selected")(
                    service_callback
                )
                return service_callback.message.answer.await_args.kwargs[
                    "reply_markup"
                ]

            first_menu = await begin_session()
            first_buttons = [
                button
                for row in first_menu.inline_keyboard
                for button in row
            ]
            old_buttons = {
                "reminders": next(
                    button.callback_data for button in first_buttons
                    if button.callback_data.startswith(
                        "group_reminder:list:"
                    )
                ),
                "finish": next(
                    button.callback_data for button in first_buttons
                    if button.callback_data.startswith("notify_finish:")
                ),
                "cancel": next(
                    button.callback_data for button in first_buttons
                    if button.callback_data.startswith("notify_cancel:")
                ),
            }
            second_menu = await begin_session()
            current_finish = next(
                button.callback_data
                for row in second_menu.inline_keyboard
                for button in row
                if button.callback_data.startswith("notify_finish:")
            )
            self.assertNotEqual(
                old_buttons["finish"], current_finish
            )

            for callback_name, old_data in (
                (
                    "on_group_reminder_action",
                    old_buttons["reminders"],
                ),
                (
                    "on_notification_setting_finish",
                    old_buttons["finish"],
                ),
                (
                    "on_notification_setting_cancel",
                    old_buttons["cancel"],
                ),
            ):
                callback = SimpleNamespace(
                    data=old_data,
                    from_user=SimpleNamespace(id=67890),
                    message=SimpleNamespace(
                        chat=SimpleNamespace(type="private"),
                        answer=AsyncMock(),
                    ),
                    answer=AsyncMock(),
                )
                await self.callback_handler(callback_name)(callback)
                self.assertIn(
                    "устарела",
                    callback.answer.await_args.args[0].lower(),
                )

            finish_callback = SimpleNamespace(
                data=current_finish,
                from_user=SimpleNamespace(id=67890),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            await self.callback_handler(
                "on_notification_setting_finish"
            )(finish_callback)
            self.assertEqual(
                self.registry.get_service("sound")["reminder_days"], 1
            )

        asyncio.run(run())

    def test_notification_callbacks_answer_when_access_check_hits_database_error(self):
        async def run():
            edit = SimpleNamespace(
                data="group_reminder:default:sound:session",
                from_user=SimpleNamespace(id=67890),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            finish = SimpleNamespace(
                data="notify_finish:sound:session",
                from_user=SimpleNamespace(id=67890),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            with patch.object(
                self.registry,
                "is_service_admin",
                side_effect=sqlite3.OperationalError("database is locked"),
            ), patch.object(
                self.registry, "is_super_admin", return_value=False
            ):
                await self.callback_handler("on_group_reminder_action")(
                    edit
                )
                await self.callback_handler("on_notification_setting_finish")(
                    finish
                )

            self.assertIn("устарела", edit.answer.await_args.args[0])
            self.assertEqual(finish.answer.await_args.args[0], "Не удалось проверить права.")
            self.context.debug_logger.exception.assert_any_call(
                "Не удалось проверить права на сохранение настроек служения"
            )

        asyncio.run(run())

    def test_personal_template_prompt_displays_single_braced_names(self):
        async def run():
            self.context.load_service_task_assignments = AsyncMock(
                return_value={
                    "Микшер": {
                        "names": ["Алексей"],
                        "is_informational": False,
                    },
                    "Причастие": {
                        "names": [],
                        "is_informational": True,
                    },
                }
            )
            command = self.message(67890, "/service_notify")
            await self.admin_router.message.handlers[6].callback(command)
            service_callback = SimpleNamespace(
                data="service:service_notify:sound",
                from_user=SimpleNamespace(id=67890),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            await self.callback_handler("on_service_selected")(service_callback)
            menu = service_callback.message.answer.await_args.kwargs[
                "reply_markup"
            ]
            personal_button = next(
                button
                for row in menu.inline_keyboard
                for button in row
                if button.text.startswith("📝 Личные шаблоны")
            )
            personal_callback = SimpleNamespace(
                data=personal_button.callback_data,
                from_user=SimpleNamespace(id=67890),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            await self.callback_handler("on_notification_setting_edit")(
                personal_callback
            )
            task_keyboard = personal_callback.message.answer.await_args.kwargs[
                "reply_markup"
            ]
            task_button = next(
                button
                for row in task_keyboard.inline_keyboard
                for button in row
                if button.text == "ℹ️ Причастие · инфо"
            )
            self.assertTrue(
                any(
                    button.text.endswith("Микшер")
                    for row in task_keyboard.inline_keyboard
                    for button in row
                )
            )
            task_callback = SimpleNamespace(
                data=task_button.callback_data,
                from_user=SimpleNamespace(id=67890),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            await self.callback_handler("on_notify_task_choice")(
                task_callback
            )
            prompt = task_callback.message.answer.await_args.args[0]
            self.assertIn("ℹ️ Причастие · инфо", prompt)
            for placeholder in (
                "{name}",
                "{task}",
                "{service_name}",
                "{sunday_date}",
                "{telegram_id}",
                "{username}",
            ):
                self.assertIn(placeholder, prompt)
                self.assertNotIn("{" + placeholder + "}", prompt)

        asyncio.run(run())

    def test_template_edit_prompts_show_saved_text_and_upcoming_preview(self):
        async def run():
            self.registry.set_notification_settings(
                67890,
                "sound",
                reminder_days=1,
                reminder_time="18:00",
                group_template="Сохранённая группа: {service_name}",
                personal_templates={"Микшер": "Сохранённый текст {name}"},
            )
            with closing(sqlite3.connect(self.registry.path)) as connection:
                connection.execute(
                    "UPDATE services SET group_template = ? WHERE id = ?",
                    ("Напоминание: {tg_responsible}", "sound"),
                )
                connection.commit()
            self.context.prepare_service_test_previews = AsyncMock(
                return_value={
                    "sunday_date": date(2026, 10, 11),
                    "group_text": "Пример группы на воскресенье",
                    "example_group_template": (
                        "Напоминание о служении «{service_name}» в воскресенье "
                        "{sunday_date}:\n{task_assignments}"
                    ),
                    "example_group_text": "Пример с задачами и ответственными",
                    "personal_messages": [
                        {
                            "task": "Микшер",
                            "name": "Алексей",
                            "telegram_id": "12345",
                            "text": "Пример личного сообщения",
                        }
                    ],
                    "issues": [],
                }
            )
            command = self.message(67890, "/service_notify")
            await self.admin_router.message.handlers[6].callback(command)
            service_callback = SimpleNamespace(
                data="service:service_notify:sound",
                from_user=SimpleNamespace(id=67890),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            await self.callback_handler("on_service_selected")(
                service_callback
            )
            menu = service_callback.message.answer.await_args.kwargs[
                "reply_markup"
            ]
            group_button = next(
                button
                for row in menu.inline_keyboard
                for button in row
                if button.text == "📝 Шаблон по умолчанию"
            )
            group_callback = SimpleNamespace(
                data=group_button.callback_data,
                from_user=SimpleNamespace(id=67890),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            await self.callback_handler("on_group_reminder_action")(
                group_callback
            )
            group_prompt = group_callback.message.answer.await_args.args[0]
            self.assertIn("Напоминание: {tg_responsible}", group_prompt)
            edit_default_button = next(
                button
                for row in group_callback.message.answer.await_args.kwargs[
                    "reply_markup"
                ].inline_keyboard
                for button in row
                if button.callback_data.startswith(
                    "group_reminder:edit_default:"
                )
            )
            edit_default_callback = SimpleNamespace(
                data=edit_default_button.callback_data,
                from_user=SimpleNamespace(id=67890),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            await self.callback_handler("on_group_reminder_action")(
                edit_default_callback
            )
            self.assertIn(
                "{service_name}",
                edit_default_callback.message.answer.await_args.args[0],
            )
            staged_input = next(
                item.callback
                for item in self.admin_router.message.handlers
                if item.callback.__name__ == "on_service_parameter"
            )
            invalid_group = self.message(67890, "{unknown}")
            await staged_input(invalid_group)
            self.assertIn(
                "не поддерживается",
                invalid_group.answer.await_args.args[0],
            )
            self.assertEqual(
                self.registry.get_service("sound")["group_template"],
                "Напоминание: {tg_responsible}",
            )
            valid_group = self.message(67890, "Новое {service_name}")
            await staged_input(valid_group)
            self.assertEqual(
                self.registry.get_service("sound")["group_template"],
                "Напоминание: {tg_responsible}",
            )
            root_back = next(
                button
                for row in valid_group.answer.await_args.kwargs[
                    "reply_markup"
                ].inline_keyboard
                for button in row
                if button.callback_data.startswith(
                    "group_reminder:root_back:"
                )
            )
            root_callback = SimpleNamespace(
                data=root_back.callback_data,
                from_user=SimpleNamespace(id=67890),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            await self.callback_handler("on_group_reminder_action")(
                root_callback
            )
            menu = root_callback.message.answer.await_args.kwargs[
                "reply_markup"
            ]

            personal_button = next(
                button
                for row in menu.inline_keyboard
                for button in row
                if button.text.startswith("📝 Личные шаблоны")
            )
            personal_callback = SimpleNamespace(
                data=personal_button.callback_data,
                from_user=SimpleNamespace(id=67890),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            await self.callback_handler("on_notification_setting_edit")(
                personal_callback
            )
            task_button = next(
                button
                for row in personal_callback.message.answer.await_args.kwargs[
                    "reply_markup"
                ].inline_keyboard
                for button in row
                if button.text.endswith("Микшер")
            )
            task_callback = SimpleNamespace(
                data=task_button.callback_data,
                from_user=SimpleNamespace(id=67890),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            await self.callback_handler("on_notify_task_choice")(
                task_callback
            )
            personal_prompt = task_callback.message.answer.await_args.args[0]
            self.assertIn("Сохранённый текст {name}", personal_prompt)
            self.assertIn("Пример личного сообщения", personal_prompt)
            self.assertEqual(
                self.context.prepare_service_test_previews.await_args.kwargs[
                    "personal_templates"
                ],
                {"Микшер": "Сохранённый текст {name}"},
            )
            invalid_personal = self.message(67890, "{name.attribute}")
            await staged_input(invalid_personal)
            self.assertIn(
                "не поддерживается",
                invalid_personal.answer.await_args.args[0],
            )
            valid_personal = self.message(67890, "Новое личное {name}")
            await staged_input(valid_personal)
            finish_button = next(
                button
                for row in valid_personal.answer.await_args.kwargs[
                    "reply_markup"
                ].inline_keyboard
                for button in row
                if button.callback_data.startswith("notify_finish:")
            )
            finish_callback = SimpleNamespace(
                data=finish_button.callback_data,
                from_user=SimpleNamespace(id=67890),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            await self.callback_handler("on_notification_setting_finish")(
                finish_callback
            )
            self.assertEqual(
                self.registry.get_service("sound")["group_template"],
                "Новое {service_name}",
            )
            self.assertEqual(
                self.registry.get_service("sound")["personal_templates"][
                    "Микшер"
                ],
                "Новое личное {name}",
            )

        asyncio.run(run())

    def test_service_contact_uses_task_choice_and_native_user_picker(self):
        async def run():
            self.registry.set_responsible_contact(
                67890, "sound", "Микшер", "Имя из расписания", 11111
            )
            command = self.message(67890, "/service_contact sound")
            await self.admin_router.message.handlers[5].callback(command)
            task_button = command.answer.await_args.kwargs[
                "reply_markup"
            ].inline_keyboard[0][0]
            self.assertEqual(task_button.text, "🎚️ Микшер")

            task_callback = SimpleNamespace(
                data=task_button.callback_data,
                from_user=SimpleNamespace(id=67890),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            task_handler = next(
                handler.callback
                for handler in self.admin_router.callback_query.handlers
                if handler.callback.__name__ == "on_task_choice"
            )
            await task_handler(task_callback)
            name_markup = task_callback.message.answer.await_args.kwargs[
                "reply_markup"
            ]
            name_button = name_markup.inline_keyboard[0][0]
            self.assertIn(
                "Привязано 1 из 1",
                task_callback.message.answer.await_args.args[0],
            )
            self.assertTrue(name_button.text.startswith("✅ "))
            self.assertNotIn(
                "11111",
                name_button.text,
            )
            name_callback = SimpleNamespace(
                data=name_button.callback_data,
                from_user=SimpleNamespace(id=67890),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            name_handler = next(
                handler.callback
                for handler in self.admin_router.callback_query.handlers
                if handler.callback.__name__ == "on_contact_person_choice"
            )
            await name_handler(name_callback)
            user_button = name_callback.message.answer.await_args.kwargs[
                "reply_markup"
            ].keyboard[0][0]
            self.assertIsNotNone(user_button.request_users)

            share_message = self.message(67890, None)
            share_message.users_shared = SimpleNamespace(
                request_id=user_button.request_users.request_id,
                users=[
                    SimpleNamespace(
                        user_id=22222,
                        first_name="Новый",
                        last_name="Пользователь",
                        username="new_user",
                    )
                ],
            )
            user_handler = next(
                handler.callback
                for handler in self.admin_router.message.handlers
                if handler.callback.__name__ == "on_users_shared"
            )
            await user_handler(share_message)
            confirmation = share_message.answer.await_args.kwargs[
                "reply_markup"
            ].inline_keyboard[0][0]
            self.assertIn("Микшер", share_message.answer.await_args.args[0])
            self.assertIn(
                "Имя из расписания",
                share_message.answer.await_args.args[0],
            )

            confirm_callback = SimpleNamespace(
                data=confirmation.callback_data,
                from_user=SimpleNamespace(id=67890),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            confirm_handler = next(
                handler.callback
                for handler in self.admin_router.callback_query.handlers
                if handler.callback.__name__ == "on_shared_admin_confirmation"
            )
            await confirm_handler(confirm_callback)
            self.assertEqual(
                self.registry.get_responsible_contact(
                    "sound", "Микшер", "Имя из расписания"
                ),
                {"telegram_id": 22222, "username": "new_user"},
            )

        asyncio.run(run())

    def test_service_sync_refreshes_schedule_and_shows_existing_links(self):
        async def run():
            self.context.load_service_task_assignments = AsyncMock(
                return_value={
                    "Микшер": {
                        "names": ["Имя из расписания"],
                        "is_informational": False,
                    },
                    "Праздник": {
                        "names": ["Пометка"],
                        "is_informational": True,
                    },
                }
            )
            self.registry.set_responsible_contact(
                67890, "sound", "Микшер", "Имя из расписания", 11111
            )
            command = self.message(67890, "/service_sync")
            sync_handler = next(
                item.callback
                for item in self.admin_router.message.handlers
                if item.callback.__name__ == "on_service_sync"
            )
            await sync_handler(command)

            service_button = await self.dialog_button(
                command,
                lambda button: button.callback_data
                == "service:service_sync:sound",
                67890,
            )
            callback = SimpleNamespace(
                data=service_button.callback_data,
                from_user=SimpleNamespace(id=67890),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            await self.callback_handler("on_service_selected")(callback)

            self.context.load_service_task_assignments.assert_awaited_once_with(
                "sound"
            )
            output = "\n".join(
                call.args[0]
                for call in callback.message.answer.await_args_list
                if call.args
            )
            task_button = next(
                button
                for call in callback.message.answer.await_args_list
                for row in (call.kwargs.get("reply_markup").inline_keyboard
                    if call.kwargs.get("reply_markup")
                    else [])
                for button in row
                if button.text.endswith("Микшер")
            )
            task_callback = SimpleNamespace(
                data=task_button.callback_data,
                from_user=SimpleNamespace(id=67890),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            await self.callback_handler("on_task_choice")(task_callback)
            contact_screen = task_callback.message.answer.await_args
            self.assertIn("Привязано 1 из 1", contact_screen.args[0])
            self.assertNotIn("Праздник", output)
            self.assertNotIn("Праздник", contact_screen.args[0])
            self.assertTrue(
                contact_screen.kwargs["reply_markup"]
                .inline_keyboard[0][0].text.startswith("✅ ")
            )
            self.assertIn(
                "Имя из расписания",
                contact_screen.kwargs["reply_markup"]
                .inline_keyboard[0][0].text,
            )
            self.assertNotIn("ID 11111", output)
            self.assertNotIn("11111", output)
            self.assertNotIn("11111", contact_screen.args[0])

        asyncio.run(run())

    def test_service_sync_does_not_claim_success_when_schedule_load_fails(self):
        async def run():
            self.context.load_service_task_assignments = AsyncMock(
                side_effect=RuntimeError("schedule unavailable")
            )
            command = self.message(67890, "/service_sync")
            await next(
                item.callback
                for item in self.admin_router.message.handlers
                if item.callback.__name__ == "on_service_sync"
            )(command)
            service_callback = SimpleNamespace(
                data="service:service_sync:sound",
                from_user=SimpleNamespace(id=67890),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )

            await self.callback_handler("on_service_selected")(
                service_callback
            )

            replies = [
                call.args[0]
                for call in service_callback.message.answer.await_args_list
            ]
            self.assertTrue(any("загружаю" in text for text in replies))
            self.assertTrue(any("Не удалось прочитать график служений" in text for text in replies))
            self.assertFalse(any("График загружен" in text for text in replies))

        asyncio.run(run())

    def test_cancel_during_schedule_load_does_not_crash_or_show_task_list(self):
        async def run():
            async def cancel_while_loading(_service_id):
                cancel_message = self.message(67890, "/cancel")
                await next(
                    item.callback
                    for item in self.admin_router.message.handlers
                    if item.callback.__name__ == "on_cancel_service_command"
                )(cancel_message)
                return {"Микшер": ["Алексей"]}

            self.context.load_service_task_assignments = AsyncMock(
                side_effect=cancel_while_loading
            )
            command = self.message(67890, "/service_contact sound")
            await self.admin_router.message.handlers[5].callback(command)
            service_callback = SimpleNamespace(
                data="service:service_contact:sound",
                from_user=SimpleNamespace(id=67890),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )

            await self.callback_handler("on_service_selected")(
                service_callback
            )

            output = "\n".join(
                call.args[0]
                for call in service_callback.message.answer.await_args_list
                if call.args
            )
            self.assertIn("отменён во время загрузки", output)
            self.assertFalse(
                any(
                    call.kwargs.get("reply_markup")
                    for call in service_callback.message.answer.await_args_list
                )
            )

        asyncio.run(run())

    def test_service_sync_requires_service_admin_before_showing_services(self):
        async def run():
            command = self.message(12345, "/service_sync")
            handler = next(
                item.callback
                for item in self.admin_router.message.handlers
                if item.callback.__name__ == "on_service_sync"
            )

            await handler(command)

            self.assertIn(
                "нет прав администратора",
                command.answer.await_args.args[0].lower(),
            )
            self.assertEqual(self.callback_data_list(command), ["menu:start", "menu:start"])

        asyncio.run(run())

    def test_service_contact_links_same_first_name_users_to_schedule_names(self):
        async def run():
            self.context.load_service_task_assignments = AsyncMock(
                return_value={
                    "Микшер": ["Алексей Один", "Алексей Два"],
                }
            )
            task_handler = self.callback_handler("on_task_choice")
            person_handler = self.callback_handler("on_contact_person_choice")
            user_handler = next(
                handler.callback
                for handler in self.admin_router.message.handlers
                if handler.callback.__name__ == "on_users_shared"
            )
            confirm_handler = self.callback_handler(
                "on_shared_admin_confirmation"
            )

            for person_name, user_id, last_name in (
                ("Алексей Один", 30101, "One"),
                ("Алексей Два", 30202, "Two"),
            ):
                command = self.message(67890, "/service_contact sound")
                await self.admin_router.message.handlers[5].callback(command)
                task_button = command.answer.await_args.kwargs[
                    "reply_markup"
                ].inline_keyboard[0][0]
                task_callback = SimpleNamespace(
                    data=task_button.callback_data,
                    from_user=SimpleNamespace(id=67890),
                    message=SimpleNamespace(
                        chat=SimpleNamespace(type="private"),
                        answer=AsyncMock(),
                    ),
                    answer=AsyncMock(),
                )
                await task_handler(task_callback)
                person_button = next(
                    button
                    for row in task_callback.message.answer.await_args.kwargs[
                        "reply_markup"
                    ].inline_keyboard
                    for button in row
                    if person_name in button.text
                )
                person_callback = SimpleNamespace(
                    data=person_button.callback_data,
                    from_user=SimpleNamespace(id=67890),
                    message=SimpleNamespace(
                        chat=SimpleNamespace(type="private"),
                        answer=AsyncMock(),
                    ),
                    answer=AsyncMock(),
                )
                await person_handler(person_callback)
                request_id = (
                    person_callback.message.answer.await_args.kwargs[
                        "reply_markup"
                    ].keyboard[0][0].request_users.request_id
                )
                shared = self.message(67890, None)
                shared.users_shared = SimpleNamespace(
                    request_id=request_id,
                    users=[
                        SimpleNamespace(
                            user_id=user_id,
                            first_name="Алексей",
                            last_name=last_name,
                            username=None,
                        )
                    ],
                )
                await user_handler(shared)
                confirm_button = shared.answer.await_args.kwargs[
                    "reply_markup"
                ].inline_keyboard[0][0]
                confirmation = SimpleNamespace(
                    data=confirm_button.callback_data,
                    from_user=SimpleNamespace(id=67890),
                    message=SimpleNamespace(
                        chat=SimpleNamespace(type="private"),
                        answer=AsyncMock(),
                    ),
                    answer=AsyncMock(),
                )
                await confirm_handler(confirmation)

            contacts = [
                item
                for item in self.registry.list_responsible_contacts(
                    67890, "sound"
                )
                if item["task"] == "Микшер"
            ]
            self.assertEqual(
                {item["name"]: item["telegram_id"] for item in contacts},
                {"Алексей Один": 30101, "Алексей Два": 30202},
            )

        asyncio.run(run())

    def test_large_schedule_task_lists_are_paginated(self):
        async def run():
            assignments = {
                f"Задача {index:02}": [f"Ответственный {index:02}"]
                for index in range(25)
            }
            self.context.load_service_task_assignments = AsyncMock(
                return_value=assignments
            )
            contact_command = self.message(
                67890, "/service_contact sound"
            )
            await self.admin_router.message.handlers[5].callback(
                contact_command
            )
            contact_keyboard = contact_command.answer.await_args_list[-1].kwargs[
                "reply_markup"
            ]
            self.assertLessEqual(
                sum(len(row) for row in contact_keyboard.inline_keyboard), 6
            )
            next_page = next(
                button
                for row in contact_keyboard.inline_keyboard
                for button in row
                if button.callback_data.startswith("task_page:")
            )
            page_callback = SimpleNamespace(
                data=next_page.callback_data,
                from_user=SimpleNamespace(id=67890),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            await self.callback_handler("on_task_page")(page_callback)
            self.assertLessEqual(
                sum(
                    len(row)
                    for row in page_callback.message.answer.await_args.kwargs[
                        "reply_markup"
                    ].inline_keyboard
                ),
                6,
            )

            notify_command = self.message(67890, "/service_notify")
            await self.admin_router.message.handlers[6].callback(notify_command)
            service_callback = SimpleNamespace(
                data="service:service_notify:sound",
                from_user=SimpleNamespace(id=67890),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            await self.callback_handler("on_service_selected")(service_callback)
            menu = service_callback.message.answer.await_args.kwargs[
                "reply_markup"
            ]
            personal_button = next(
                button
                for row in menu.inline_keyboard
                for button in row
                if button.callback_data.startswith(
                    "notify_edit:personal:sound:"
                )
            )
            personal_callback = SimpleNamespace(
                data=personal_button.callback_data,
                from_user=SimpleNamespace(id=67890),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            await self.callback_handler(
                "on_notification_setting_edit"
            )(personal_callback)
            self.assertEqual(
                self.context.load_service_task_assignments.await_args.args,
                ("sound",),
            )
            notify_keyboard = personal_callback.message.answer.await_args.kwargs[
                "reply_markup"
            ]
            self.assertLessEqual(
                sum(len(row) for row in notify_keyboard.inline_keyboard),
                6,
            )

        asyncio.run(run())

    def test_old_task_page_button_is_rejected_after_task_selection(self):
        async def run():
            assignments = {
                f"Задача {index:02}": [f"Ответственный {index:02}"]
                for index in range(11)
            }
            self.context.load_service_task_assignments = AsyncMock(
                return_value=assignments
            )
            command = self.message(67890, "/service_contact sound")
            await self.admin_router.message.handlers[5].callback(command)
            keyboard = command.answer.await_args_list[-1].kwargs["reply_markup"]
            task_button = next(
                button
                for row in keyboard.inline_keyboard
                for button in row
                if button.text.endswith("Задача 00")
            )
            old_page_button = next(
                button
                for row in keyboard.inline_keyboard
                for button in row
                if button.callback_data.startswith("task_page:")
            )

            task_callback = SimpleNamespace(
                data=task_button.callback_data,
                from_user=SimpleNamespace(id=67890),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            await self.callback_handler("on_task_choice")(task_callback)

            page_callback = SimpleNamespace(
                data=old_page_button.callback_data,
                from_user=SimpleNamespace(id=67890),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            await self.callback_handler("on_task_page")(page_callback)

            page_callback.message.answer.assert_not_awaited()
            self.assertIn("устарел", page_callback.answer.await_args.args[0])

        asyncio.run(run())

    def test_notification_setup_recovers_if_first_schedule_load_fails(self):
        async def run():
            self.context.load_service_task_assignments = AsyncMock(
                side_effect=[RuntimeError("temporary download failure"), {}]
            )
            handler = self.admin_router.message.handlers[6].callback
            command = self.message(67890, "/service_notify")
            await handler(command)
            service_callback = SimpleNamespace(
                data="service:service_notify:sound",
                from_user=SimpleNamespace(id=67890),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            await self.callback_handler("on_service_selected")(service_callback)
            menu = service_callback.message.answer.await_args.kwargs[
                "reply_markup"
            ]
            personal_button = next(
                button
                for row in menu.inline_keyboard
                for button in row
                if button.callback_data.startswith(
                    "notify_edit:personal:sound:"
                )
            )
            def make_personal_callback():
                return SimpleNamespace(
                    data=personal_button.callback_data,
                    from_user=SimpleNamespace(id=67890),
                    message=SimpleNamespace(
                        chat=SimpleNamespace(type="private"),
                        answer=AsyncMock(),
                    ),
                    answer=AsyncMock(),
                )

            failed_load = make_personal_callback()
            await self.callback_handler(
                "on_notification_setting_edit"
            )(failed_load)
            self.assertIn(
                "Не удалось прочитать файл графика",
                failed_load.message.answer.await_args.args[0],
            )

            retry = make_personal_callback()
            await self.callback_handler(
                "on_notification_setting_edit"
            )(retry)
            return_button = next(
                button
                for row in retry.message.answer.await_args.kwargs[
                    "reply_markup"
                ].inline_keyboard
                for button in row
                if button.text == "⬅️ К настройкам"
            )
            return_callback = SimpleNamespace(
                data=return_button.callback_data,
                from_user=SimpleNamespace(id=67890),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            await self.callback_handler("on_notify_task_choice")(
                return_callback
            )
            finish_button = next(
                button
                for row in return_callback.message.answer.await_args.kwargs[
                    "reply_markup"
                ].inline_keyboard
                for button in row
                if button.text == "✅ Завершить и сохранить"
            )
            finish_callback = SimpleNamespace(
                data=finish_button.callback_data,
                from_user=SimpleNamespace(id=67890),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            await self.callback_handler(
                "on_notification_setting_finish"
            )(finish_callback)

            service = self.registry.get_service("sound")
            self.assertIsNone(service["group_template"])
            self.assertEqual(
                self.context.load_service_task_assignments.await_count, 2
            )

        asyncio.run(run())

    def test_service_chat_offers_disable_button_instead_of_command_syntax(self):
        async def run():
            self.registry.set_group_chat(
                67890, "sound", -100222, enabled=True
            )
            command = self.message(67890, "/service_chat")
            await self.admin_router.message.handlers[4].callback(command)
            service_button = await self.dialog_button(
                command,
                lambda button: button.callback_data
                == "service:service_chat:sound",
                67890,
            )
            service_callback = SimpleNamespace(
                data=service_button.callback_data,
                from_user=SimpleNamespace(id=67890),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            service_handler = next(
                handler.callback
                for handler in self.admin_router.callback_query.handlers
                if handler.callback.__name__ == "on_service_selected"
            )
            await service_handler(service_callback)
            self.assertNotIn(
                "service:service_chat_disable:sound",
                self.callback_data_list(service_callback.message),
            )
            service_callback.data = "service:service_setup:sound"
            await service_handler(service_callback)
            action_keyboard = service_callback.message.answer.await_args.kwargs["reply_markup"]
            disable_button = next(
                button
                for row in action_keyboard.inline_keyboard
                for button in row
                if button.text == "⛔ Отключить служение"
            )
            disable_callback = SimpleNamespace(
                data=disable_button.callback_data,
                from_user=SimpleNamespace(id=67890),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            await service_handler(disable_callback)
            self.assertTrue(self.registry.get_service("sound")["enabled"])
            confirm = self.callback_data_list(disable_callback.message)[0]
            disable_callback.data = confirm
            await self.callback_handler("on_service_disable")(disable_callback)
            sound = self.registry.get_service("sound")
            self.assertFalse(sound["enabled"])
            self.assertEqual(sound["group_chat_id"], -100222)

        asyncio.run(run())

    def test_test_notify_selects_service_before_group_delivery(self):
        async def run():
            self.registry.set_group_chat(
                67890, "sound", -100333, enabled=True
            )
            context = SimpleNamespace(
                interaction_logger=MagicMock(),
                debug_logger=MagicMock(),
                admin_id=67890,
                group_chat_id=-100,
                service_registry=self.registry,
                bot=SimpleNamespace(send_message=AsyncMock()),
                prepare_service_test_previews=AsyncMock(),
            )
            dispatcher = SimpleNamespace(include_router=MagicMock())
            register(dispatcher, context)
            admin_router = dispatcher.include_router.call_args_list[0].args[0]

            test_handler = router.message.handlers[-1].callback
            command = self.message(67890, "/test_notify")
            await test_handler(command)
            selection = command.answer.await_args.kwargs["reply_markup"]
            service_button = await self.dialog_button(
                command,
                lambda button: button.callback_data
                == "service:test_notify:sound",
                67890,
                admin_router,
            )

            callback = SimpleNamespace(
                data=service_button.callback_data,
                from_user=SimpleNamespace(id=67890),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            service_handler = next(
                handler.callback
                for handler in admin_router.callback_query.handlers
                if handler.callback.__name__ == "on_service_selected"
            )
            await service_handler(callback)

            context.bot.send_message.assert_not_awaited()
            action_keyboard = callback.message.answer.await_args.kwargs[
                "reply_markup"
            ]
            group_button = next(
                button
                for row in action_keyboard.inline_keyboard
                for button in row
                if button.callback_data == "test_action:group:sound"
            )
            action_handler = next(
                handler.callback
                for handler in reversed(router.callback_query.handlers)
                if handler.callback.__name__ == "on_test_notify_action"
            )
            action_callback = SimpleNamespace(
                data=group_button.callback_data,
                from_user=SimpleNamespace(id=67890),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            await action_handler(action_callback)

            self.assertEqual(
                context.bot.send_message.await_args.args[0], -100333
            )
            self.assertIn(
                "ТЕСТ службы «Звук»",
                context.bot.send_message.await_args.args[1],
            )
            context.prepare_service_test_previews.assert_not_awaited()

        asyncio.run(run())

    def test_test_preview_formatting_error_is_reported_and_callback_answered(self):
        async def run():
            sound = self.registry.get_service("sound")
            context = SimpleNamespace(
                interaction_logger=MagicMock(),
                debug_logger=MagicMock(),
                admin_id=67890,
                service_registry=SimpleNamespace(
                    is_super_admin=lambda actor_id: actor_id == 67890,
                    list_services=MagicMock(return_value=[sound]),
                    get_service=MagicMock(return_value=sound),
                ),
                bot=SimpleNamespace(send_message=AsyncMock()),
                prepare_service_test_previews=AsyncMock(
                    side_effect=ValueError(
                        "Некорректный групповой шаблон служения «Звук»"
                    )
                ),
            )
            dispatcher = SimpleNamespace(include_router=MagicMock())
            register(dispatcher, context)
            command = self.message(67890, "/test_notify")
            await router.message.handlers[-1].callback(command)
            service_button = command.answer.await_args.kwargs[
                "reply_markup"
            ].inline_keyboard[0][0]
            service_callback = SimpleNamespace(
                data=service_button.callback_data,
                from_user=SimpleNamespace(id=67890),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            await next(
                item.callback
                for item in dispatcher.include_router.call_args_list[0]
                .args[0].callback_query.handlers
                if item.callback.__name__ == "on_service_selected"
            )(service_callback)
            preview_callback = SimpleNamespace(
                data="test_action:preview:sound",
                from_user=SimpleNamespace(id=67890),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            await next(
                item.callback
                for item in reversed(router.callback_query.handlers)
                if item.callback.__name__ == "on_test_notify_action"
            )(preview_callback)

            self.assertIn(
                "ValueError",
                preview_callback.message.answer.await_args.args[0],
            )
            preview_callback.answer.assert_awaited_once()
            context.bot.send_message.assert_not_awaited()

        asyncio.run(run())

    def test_service_admin_runs_delivery_check_for_own_service_only(self):
        async def run():
            self.registry.set_group_chat(67890, "sound", -200, enabled=True)
            self.registry.set_group_chat(67890, "books", -300, enabled=True)
            self.registry.add_service_admin(67890, "sound", 24680)
            self.registry.add_service_admin(67890, "books", 13579)
            self.context.prepare_service_test_previews = AsyncMock(
                return_value={
                    "sunday_date": date(2026, 11, 1),
                    "group_text": "Звук",
                    "personal_messages": [],
                    "issues": [],
                }
            )
            test_action = next(
                item.callback
                for item in reversed(router.callback_query.handlers)
                if item.callback.__name__ == "on_test_notify_action"
            )

            def callback(actor_id, data):
                return SimpleNamespace(
                    data=data,
                    from_user=SimpleNamespace(id=actor_id),
                    message=self.message(actor_id, ""),
                    answer=AsyncMock(),
                )

            opened = callback(24680, "service:test_notify:sound")
            await self.callback_handler("on_service_selected")(opened)
            markup = opened.message.answer.await_args.kwargs["reply_markup"]
            self.assertEqual(
                [b.callback_data for row in markup.inline_keyboard for b in row],
                ["test_action:group:sound", "test_action:preview:sound",
                 "service:service_chat:sound"],
            )

            await test_action(callback(24680, "test_action:preview:sound"))
            self.context.prepare_service_test_previews.assert_awaited_once_with(
                "sound"
            )
            self.assertEqual(
                self.context.bot.send_message.await_args.args[0], 24680
            )
            self.context.bot.send_message.reset_mock()
            await test_action(callback(24680, "test_action:group:sound"))
            self.assertEqual(
                self.context.bot.send_message.await_args.args[0], -200
            )

            # Админ другого служения не запускает ни одну проверку.
            self.context.bot.send_message.reset_mock()
            self.context.prepare_service_test_previews.reset_mock()
            denied = callback(13579, "service:test_notify:sound")
            await self.callback_handler("on_service_selected")(denied)
            self.assertIn(
                "только администратору",
                denied.message.answer.await_args.args[0],
            )
            self.assertEqual(
                self.callback_data_list(denied.message), ["menu:start", "menu:start"]
            )
            for action in ("group", "preview"):
                foreign = callback(13579, f"test_action:{action}:sound")
                await test_action(foreign)
                foreign.message.answer.assert_not_awaited()
                self.assertIn(
                    "только администратору", foreign.answer.await_args.args[0]
                )
            self.context.bot.send_message.assert_not_awaited()
            self.context.prepare_service_test_previews.assert_not_awaited()

        asyncio.run(run())

    def test_management_commands_are_silent_outside_private_chat(self):
        async def run():
            message = self.message(
                67890, "/service_admin_add sound 12345", chat_type="group"
            )
            await self.admin_router.message.handlers[2].callback(message)

            message.answer.assert_not_awaited()
            self.assertEqual(self.registry.list_service_admins("sound"), [])

        asyncio.run(run())

    def test_management_commands_report_unavailable_registry(self):
        async def run():
            context = SimpleNamespace(
                interaction_logger=MagicMock(),
                debug_logger=MagicMock(),
                service_registry=None,
            )
            dispatcher = SimpleNamespace(include_router=MagicMock())
            register(dispatcher, context)
            admin_router = dispatcher.include_router.call_args_list[0].args[0]

            for handler_index, command in (
                (0, "/services"),
                (2, "/service_admin_add sound 12345"),
            ):
                message = self.message(67890, command)
                await admin_router.message.handlers[handler_index].callback(
                    message
                )
                self.assertIn(
                    "реестр настроек служений недоступен",
                    message.answer.await_args.args[0].lower(),
                )

        asyncio.run(run())

    def test_service_settings_screen_stays_compact_with_many_contacts(self):
        async def run():
            for index in range(60):
                self.registry.set_responsible_contact(
                    67890,
                    "sound",
                    "Микшер",
                    f"Ответственный {index} " + "И" * 90,
                    10000 + index,
                )
            message = self.message(67890, "/service_settings sound")

            await self.admin_router.message.handlers[1].callback(message)

            chunks = [
                call.args[0] for call in message.answer.await_args_list
                if call.args[0] != NAV_HINT
            ]
            self.assertEqual(len(chunks), 1)
            self.assertTrue(
                all(len(chunk.encode("utf-16-le")) // 2 <= 500 for chunk in chunks)
            )
            self.assertIn("📡 Групповой чат", "".join(chunks))
            self.assertNotIn("Ответственный 59", "".join(chunks))

        asyncio.run(run())

    def test_settings_group_contacts_by_task(self):
        async def run():
            self.registry.set_responsible_contact(
                67890, "sound", "Ведущий", "Аркадий", 718343838, "arkady"
            )
            self.registry.set_responsible_contact(
                67890, "sound", "Ведущий", "Евгений", 306139797, "evgen"
            )
            self.registry.set_responsible_contact(
                67890, "sound", "Проповедник", "Денис", 801625873, "denis"
            )
            self.context.load_service_task_assignments = AsyncMock(
                return_value={
                    "Ведущий": ["Аркадий", "Евгений"],
                    "Проповедник": ["Денис"],
                }
            )
            message = self.message(67890, "/service_contact sound")

            await self.admin_router.message.handlers[5].callback(message)
            task_button = next(
                button
                for row in message.answer.await_args.kwargs[
                    "reply_markup"
                ].inline_keyboard
                for button in row
                if button.callback_data.startswith("task:service_contact:")
                and button.text.endswith("Ведущий")
            )
            callback = SimpleNamespace(
                data=task_button.callback_data,
                from_user=SimpleNamespace(id=67890),
                message=self.message(67890, "task selection"),
                answer=AsyncMock(),
            )
            await self.callback_handler("on_task_choice")(callback)
            text = callback.message.answer.await_args.args[0]
            self.assertIn("Ведущий", text)
            self.assertIn("Привязано 2 из 2", text)
            self.assertTrue(
                any(
                    button.text.endswith("Аркадий · @arkady")
                    for row in callback.message.answer.await_args.kwargs[
                        "reply_markup"
                    ].inline_keyboard
                    for button in row
                )
            )
            for user_id in ("718343838", "306139797"):
                self.assertNotIn(user_id, text)

        asyncio.run(run())

    def test_service_listing_uses_only_readable_russian_labels(self):
        async def run():
            message = self.message(67890, "/services")
            await self.admin_router.message.handlers[0].callback(message)

            assembly = await self.dialog_button(
                message,
                lambda button: button.callback_data
                == "service:service_settings:assembly",
                67890,
            )
            broadcast = await self.dialog_button(
                message,
                lambda button: button.callback_data
                == "service:service_settings:broadcast",
                67890,
            )
            self.assertTrue(assembly.text.endswith("СОБРАНИЕ"))
            self.assertTrue(broadcast.text.endswith("ТРАНСЛЯЦИЯ И СЛАЙДЫ"))

        asyncio.run(run())

    def test_settings_command_without_arguments_offers_service_buttons(self):
        async def run():
            self.registry.add_service_admin(67890, "sound", 12345)
            message = self.message(12345, "/service_settings")

            await self.admin_router.message.handlers[1].callback(message)

            message.answer.assert_awaited_once()
            text = message.answer.await_args.args[0]
            keyboard = message.answer.await_args.kwargs["reply_markup"]
            buttons = [
                button
                for row in keyboard.inline_keyboard
                for button in row
            ]
            self.assertIn("Выберите служение", text)
            self.assertEqual(
                [button.callback_data for button in buttons],
                ["service:service_settings:sound"],
            )

            callback = SimpleNamespace(
                data=buttons[0].callback_data,
                from_user=SimpleNamespace(id=12345),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            await self.callback_handler("on_service_selected")(callback)
            self.assertIn(
                "ЗВУК",
                callback.message.answer.await_args.args[0],
            )
            callback.answer.assert_awaited_once()

        asyncio.run(run())

    def test_service_selection_lists_match_services_command_layout(self):
        async def run():
            command_handlers = {
                item.callback.__name__: item.callback
                for item in self.admin_router.message.handlers
            }
            expected_ids = [
                ["assembly"],
                ["broadcast"],
                ["kitchen", "sound"],
                ["order", "office_cleaning"],
                ["books", "children"],
            ]
            for command_name, operation in (
                ("/service_settings", "service_settings"),
                ("/service_contact", "service_contact"),
                ("/service_notify", "service_notify"),
                ("/service_chat", "service_chat"),
            ):
                message = self.message(67890, command_name)
                handler_name = {
                    "service_settings": "on_service_settings",
                    "service_contact": "on_service_contact",
                    "service_notify": "on_service_notify",
                    "service_chat": "on_service_chat",
                }[operation]
                await command_handlers[handler_name](message)

                rows = message.answer.await_args.kwargs[
                    "reply_markup"
                ].inline_keyboard
                self.assertEqual(
                    [
                        [
                            button.callback_data.rsplit(":", 1)[-1]
                            for button in row
                        ]
                        for row in rows
                    ],
                    expected_ids,
                    operation,
                )

            services_message = self.message(67890, "/services")
            await command_handlers["on_services"](services_message)
            services_rows = services_message.answer.await_args.kwargs[
                "reply_markup"
            ].inline_keyboard
            self.assertEqual(
                [
                    [
                        button.callback_data.rsplit(":", 1)[-1]
                        for button in row
                    ]
                    for row in services_rows
                ],
                expected_ids,
            )

        asyncio.run(run())

    def test_service_selector_paginates_six_buttons_plus_bottom_navigation(self):
        async def run():
            services = [
                {
                    "id": f"service_{index}",
                    "name": f"Служение {index}",
                    "enabled": index % 2 == 0,
                }
                for index in range(9)
            ]
            message = self.message(67890, "/service_settings")
            with patch.object(
                self.registry, "list_services", return_value=services
            ):
                await next(
                    item.callback
                    for item in self.admin_router.message.handlers
                    if item.callback.__name__ == "on_service_settings"
                )(message)

            rows = message.answer.await_args.kwargs[
                "reply_markup"
            ].inline_keyboard
            self.assertEqual([len(row) for row in rows], [3, 3, 1])
            self.assertTrue(rows[-1][0].text.endswith("➡️"))
            self.assertEqual(
                sum(len(row) for row in rows),
                7,
            )

        asyncio.run(run())

    def test_admin_add_command_without_parameters_guides_user_through_flow(self):
        async def run():
            message = self.message(67890, "/service_admin_add")
            await self.admin_router.message.handlers[2].callback(message)

            keyboard = message.answer.await_args.kwargs["reply_markup"]
            button = keyboard.inline_keyboard[0][0]
            self.assertEqual(button.text, "🟢 СОБРАНИЕ")
            callback = SimpleNamespace(
                data=button.callback_data,
                from_user=SimpleNamespace(id=67890),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            await self.callback_handler("on_service_selected")(callback)
            self.assertIn("выберите пользователя", callback.message.answer.await_args.args[0])
            keyboard = callback.message.answer.await_args.kwargs["reply_markup"]
            request = keyboard.keyboard[0][0].request_users
            self.assertEqual(request.max_quantity, 1)
            self.assertFalse(request.user_is_bot)

            self.assertEqual(self.registry.list_service_admins("assembly"), [])

        asyncio.run(run())

    def test_admin_can_add_self_by_entering_id_and_username(self):
        async def run():
            command = self.message(67890, "/service_admin_add")
            await self.admin_router.message.handlers[2].callback(command)
            service_button = await self.dialog_button(
                command,
                lambda button: button.callback_data
                == "service:service_admin_add:assembly",
                67890,
            )
            service_callback = SimpleNamespace(
                data=service_button.callback_data,
                from_user=SimpleNamespace(id=67890),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            await self.callback_handler("on_service_selected")(
                service_callback
            )
            staged_input = next(
                item.callback
                for item in self.admin_router.message.handlers
                if item.callback.__name__ == "on_service_parameter"
            )
            manual_input = self.message(67890, "67890 @self_admin")
            await staged_input(manual_input)
            confirmation = manual_input.answer.await_args.kwargs[
                "reply_markup"
            ].inline_keyboard[0][0]
            callback = SimpleNamespace(
                data=confirmation.callback_data,
                from_user=SimpleNamespace(id=67890),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )

            await self.callback_handler("on_shared_admin_confirmation")(
                callback
            )

            self.assertEqual(
                self.registry.list_service_admin_details("assembly"),
                [{"telegram_id": 67890, "username": "self_admin"}],
            )

        asyncio.run(run())

    def test_direct_admin_add_requires_explicit_confirmation(self):
        async def run():
            message = self.message(
                67890, "/service_admin_add sound 12345 @target_user"
            )

            await self.admin_router.message.handlers[2].callback(message)

            self.assertEqual(self.registry.list_service_admins("sound"), [])
            self.assertIn(
                "target_user",
                message.answer.await_args.args[0],
            )
            button = message.answer.await_args.kwargs[
                "reply_markup"
            ].inline_keyboard[0][0]
            callback = SimpleNamespace(
                data=button.callback_data,
                from_user=SimpleNamespace(id=67890),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )

            await self.callback_handler("on_shared_admin_confirmation")(
                callback
            )

            self.assertEqual(
                self.registry.list_service_admin_details("sound"),
                [{"telegram_id": 12345, "username": "target_user"}],
            )

        asyncio.run(run())

    def test_services_listing_separates_services_and_shows_admin_usernames(self):
        async def run():
            self.registry.add_service_admin(67890, "sound", 12345, "sound_admin")
            self.registry.add_service_admin(67890, "books", 12345, "sound_admin")
            message = self.message(67890, "/services")

            await self.admin_router.message.handlers[0].callback(message)

            result = "\n".join(
                call.args[0] for call in message.answer.await_args_list
            )
            self.assertNotIn("12345", result)
            sound_button = await self.dialog_button(
                message,
                lambda button: button.callback_data
                == "service:service_settings:sound",
                67890,
            )
            slides_button = await self.dialog_button(
                message,
                lambda button: button.callback_data
                == "service:service_settings:books",
                67890,
            )
            self.assertTrue(sound_button.text.endswith("ЗВУК"))
            self.assertTrue(slides_button.text.endswith("КНИГИ"))
            self.assertTrue(self.registry.is_service_admin(12345, "sound"))
            self.assertTrue(self.registry.is_service_admin(12345, "books"))

        asyncio.run(run())

    def test_contact_manual_input_cancel_and_confirm_return_to_contact_list(self):
        async def run():
            self.context.load_service_task_assignments = AsyncMock(
                return_value={"Микшер": ["Алексей", "Иван"]}
            )
            command = self.message(67890, "/service_contact sound")
            await self.admin_router.message.handlers[5].callback(command)
            task_button = command.answer.await_args.kwargs[
                "reply_markup"
            ].inline_keyboard[0][0]
            task_callback = SimpleNamespace(
                data=task_button.callback_data,
                from_user=SimpleNamespace(id=67890),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            await self.callback_handler("on_task_choice")(task_callback)
            person_button = task_callback.message.answer.await_args.kwargs[
                "reply_markup"
            ].inline_keyboard[0][0]
            person_callback = SimpleNamespace(
                data=person_button.callback_data,
                from_user=SimpleNamespace(id=67890),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            await self.callback_handler("on_contact_person_choice")(
                person_callback
            )
            staged_input = next(
                item.callback
                for item in self.admin_router.message.handlers
                if item.callback.__name__ == "on_service_parameter"
            )

            async def enter_user_and_finish(action):
                manual_message = self.message(
                    67890, "33333 @manual_user"
                )
                await staged_input(manual_message)
                button = manual_message.answer.await_args.kwargs[
                    "reply_markup"
                ].inline_keyboard[0][1 if action == "cancel" else 0]
                callback = SimpleNamespace(
                    data=button.callback_data,
                    from_user=SimpleNamespace(id=67890),
                    message=SimpleNamespace(
                        chat=SimpleNamespace(type="private"),
                        answer=AsyncMock(),
                    ),
                    answer=AsyncMock(),
                )
                await self.callback_handler(
                    "on_shared_admin_confirmation"
                )(callback)
                return callback, manual_message

            cancelled, _ = await enter_user_and_finish("cancel")
            self.assertIn(
                "Назначение отменено",
                cancelled.message.answer.await_args_list[0].args[0],
            )
            list_markup = cancelled.message.answer.await_args.kwargs[
                "reply_markup"
            ]
            self.assertTrue(
                any(
                    button.text == "⬅️ К задачам"
                    for row in list_markup.inline_keyboard
                    for button in row
                )
            )

            person_button = list_markup.inline_keyboard[1][0]
            person_callback = SimpleNamespace(
                data=person_button.callback_data,
                from_user=SimpleNamespace(id=67890),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            await self.callback_handler("on_contact_person_choice")(
                person_callback
            )
            confirmed, _ = await enter_user_and_finish("confirm")
            self.assertEqual(
                self.registry.get_responsible_contact(
                    "sound", "Микшер", "Иван"
                ),
                {"telegram_id": 33333, "username": "manual_user"},
            )
            self.assertIn(
                "Ответственный «Иван»",
                confirmed.message.answer.await_args.args[0],
            )
            list_markup = confirmed.message.answer.await_args.kwargs[
                "reply_markup"
            ]
            self.assertEqual(
                [
                    button.text
                    for row in list_markup.inline_keyboard
                    for button in row
                ],
                [
                    "⚪ Алексей · не привязан",
                    "✅ Иван · @manual_user",
                    "⬅️ К задачам",
                ],
            )
            next_person = list_markup.inline_keyboard[0][0]
            next_person_callback = SimpleNamespace(
                data=next_person.callback_data,
                from_user=SimpleNamespace(id=67890),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            await self.callback_handler("on_contact_person_choice")(
                next_person_callback
            )
            self.assertIn(
                "для имени «Алексей»",
                next_person_callback.message.answer.await_args.args[0],
            )

        asyncio.run(run())

    def test_contact_navigation_returns_to_tasks_then_service_picker(self):
        async def run():
            self.context.load_service_task_assignments = AsyncMock(
                return_value={"Микшер": ["Алексей"]}
            )
            command = self.message(67890, "/service_contact sound")
            await self.admin_router.message.handlers[5].callback(command)
            task_button = command.answer.await_args.kwargs[
                "reply_markup"
            ].inline_keyboard[0][0]
            task_callback = SimpleNamespace(
                data=task_button.callback_data,
                from_user=SimpleNamespace(id=67890),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            await self.callback_handler("on_task_choice")(task_callback)

            return_to_tasks = task_callback.message.answer.await_args.kwargs[
                "reply_markup"
            ].inline_keyboard[-1][0]
            tasks_callback = SimpleNamespace(
                data=return_to_tasks.callback_data,
                from_user=SimpleNamespace(id=67890),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            await self.callback_handler("on_contact_navigation")(
                tasks_callback
            )
            task_markup = tasks_callback.message.answer.await_args.kwargs[
                "reply_markup"
            ]
            service_picker_button = task_markup.inline_keyboard[-1][0]
            services_callback = SimpleNamespace(
                data=service_picker_button.callback_data,
                from_user=SimpleNamespace(id=67890),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    from_user=SimpleNamespace(id=99999),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            await self.callback_handler("on_contact_navigation")(
                services_callback
            )
            self.assertIn(
                "Выберите служение",
                services_callback.message.answer.await_args.args[0],
            )
            service_picker = services_callback.message.answer.await_args.kwargs[
                "reply_markup"
            ]
            self.assertTrue(
                any(
                    button.callback_data.startswith(
                        "service:service_contact:"
                    )
                    for row in service_picker.inline_keyboard
                    for button in row
                )
            )

        asyncio.run(run())

    def test_old_chat_picker_response_is_rejected_after_handler_restart(self):
        async def run():
            first_command = self.message(67890, "/service_chat sound")
            await self.admin_router.message.handlers[4].callback(first_command)
            old_request_id = (
                first_command.answer.await_args.kwargs["reply_markup"]
                .keyboard[0][0].request_chat.request_id
            )

            new_dispatcher = SimpleNamespace(include_router=MagicMock())
            register(new_dispatcher, self.context)
            new_admin_router = (
                new_dispatcher.include_router.call_args_list[0].args[0]
            )
            new_command = self.message(67890, "/service_chat children")
            await new_admin_router.message.handlers[4].callback(new_command)
            new_request_id = (
                new_command.answer.await_args.kwargs["reply_markup"]
                .keyboard[0][0].request_chat.request_id
            )
            self.assertNotEqual(old_request_id, new_request_id)

            stale_response = self.message(67890, None)
            stale_response.chat_shared = SimpleNamespace(
                request_id=old_request_id,
                chat_id=-1009911,
                title="Старый выбор",
                username=None,
            )
            await new_admin_router.message.handlers[10].callback(stale_response)

            self.assertIn("не соответствует активному запросу", stale_response.answer.await_args.args[0])
            self.assertIsNone(self.registry.get_service("sound")["group_chat_id"])
            self.assertIsNone(self.registry.get_service("children")["group_chat_id"])

        asyncio.run(run())

    def test_staged_input_filter_does_not_consume_commands(self):
        async def run():
            handler = next(
                handler
                for handler in self.admin_router.message.handlers
                if handler.callback.__name__ == "on_service_parameter"
            )
            filter_object = handler.filters[0]

            command_matches = await filter_object.call(
                value=self.message(67890, "/test_notify")
            )
            text_matches = await filter_object.call(
                value=self.message(67890, "some staged input")
            )

            self.assertFalse(command_matches)
            self.assertTrue(text_matches)

        asyncio.run(run())

    def test_switching_management_commands_clears_old_staged_input(self):
        async def run():
            self.registry.add_service_admin(67890, "sound", 24680)
            remove = self.message(67890, "/service_admin_remove sound")
            await self.admin_router.message.handlers[3].callback(remove)
            settings = self.message(67890, "/service_settings sound")
            await self.admin_router.message.handlers[1].callback(settings)
            staged_input = next(
                handler.callback
                for handler in self.admin_router.message.handlers
                if handler.callback.__name__ == "on_service_parameter"
            )
            numeric_text = self.message(67890, "24680")

            await staged_input(numeric_text)

            self.assertEqual(self.registry.list_service_admins("sound"), [24680])
            numeric_text.answer.assert_not_awaited()

        asyncio.run(run())

    def test_back_to_services_reports_registry_read_errors(self):
        async def run():
            self.context.service_registry = SimpleNamespace(
                is_super_admin=MagicMock(
                    side_effect=sqlite3.OperationalError("database unavailable")
                )
            )
            callback = SimpleNamespace(
                data="service:back_to_services:list",
                from_user=SimpleNamespace(id=67890),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )

            await self.callback_handler("on_service_selected")(callback)

            self.assertEqual(
                callback.message.answer.await_args.args[0],
                "<b>Не удалось прочитать реестр служений.</b>",
            )
            callback.answer.assert_awaited_once()
            self.context.debug_logger.exception.assert_called()

        asyncio.run(run())

    def test_shared_user_is_confirmed_before_becoming_service_admin(self):
        async def run():
            command = self.message(67890, "/service_admin_add")
            await self.admin_router.message.handlers[2].callback(command)
            service_keyboard = command.answer.await_args.kwargs["reply_markup"]
            assembly_button = service_keyboard.inline_keyboard[0][0]
            selected_service_message = self.message(
                67890, "service selection", chat_type="private"
            )
            service_callback = SimpleNamespace(
                data=assembly_button.callback_data,
                from_user=SimpleNamespace(id=67890),
                message=selected_service_message,
                answer=AsyncMock(),
            )
            await self.callback_handler("on_service_selected")(service_callback)

            user_keyboard = selected_service_message.answer.await_args.kwargs[
                "reply_markup"
            ]
            request_id = user_keyboard.keyboard[0][0].request_users.request_id
            share_message = self.message(67890, None)
            share_message.users_shared = SimpleNamespace(
                request_id=request_id,
                users=[
                    SimpleNamespace(
                        user_id=24680,
                        first_name="Анна",
                        username="candidate",
                    )
                ],
            )
            await self.admin_router.message.handlers[9].callback(
                share_message
            )
            self.assertEqual(self.registry.list_service_admins("assembly"), [])
            confirmation = share_message.answer.await_args.kwargs["reply_markup"]
            confirm_button = confirmation.inline_keyboard[0][0]
            self.assertIn("Анна", share_message.answer.await_args.args[0])
            self.assertIn("@candidate", share_message.answer.await_args.args[0])

            admin_callback = SimpleNamespace(
                data=confirm_button.callback_data,
                from_user=SimpleNamespace(id=67890),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            await self.callback_handler(
                "on_shared_admin_confirmation"
            )(admin_callback)
            self.assertEqual(
                self.registry.list_service_admins("assembly"), [24680]
            )
            self.assertNotIn(
                "24680",
                admin_callback.message.answer.await_args.args[0],
            )

        asyncio.run(run())

    def test_shared_group_chat_is_saved_and_service_can_be_disabled(self):
        async def run():
            command = self.message(67890, "/service_chat sound")
            await self.admin_router.message.handlers[4].callback(command)
            keyboard = command.answer.await_args.kwargs["reply_markup"]
            request = keyboard.keyboard[0][0].request_chat
            self.assertFalse(request.chat_is_channel)
            self.assertTrue(request.bot_is_member)

            shared_chat = self.message(67890, None)
            shared_chat.chat_shared = SimpleNamespace(
                request_id=request.request_id,
                chat_id=-1002233,
                title="Звук",
                username=None,
            )
            await self.admin_router.message.handlers[10].callback(shared_chat)
            sound = self.registry.get_service("sound")
            self.assertEqual(sound["group_chat_id"], -1002233)
            self.assertTrue(sound["enabled"])
            self.assertIn(
                "-1002233",
                shared_chat.answer.await_args_list[0].args[0],
            )

            disable = self.message(67890, "/service_chat sound off")
            await self.admin_router.message.handlers[4].callback(disable)
            self.assertTrue(self.registry.get_service("sound")["enabled"])
            confirmation = SimpleNamespace(
                data=self.callback_data_list(disable)[0],
                from_user=SimpleNamespace(id=67890),
                message=disable, answer=AsyncMock(),
            )
            await self.callback_handler("on_service_disable")(confirmation)
            sound = self.registry.get_service("sound")
            self.assertEqual(sound["group_chat_id"], -1002233)
            self.assertFalse(sound["enabled"])

        asyncio.run(run())

    def test_settings_message_has_next_action_button(self):
        async def run():
            self.registry.add_service_admin(67890, "sound", 24680)
            self.registry.set_notification_settings(
                24680,
                "sound",
                reminder_days=1,
                reminder_time="16:00",
                group_template="Сохранённый групповой текст",
                personal_templates={"Микшер": "Сохранённый личный текст"},
            )
            selection_message = self.message(
                67890, "/service_settings sound"
            )
            await self.admin_router.message.handlers[1].callback(
                selection_message
            )
            keyboard = selection_message.answer.await_args.kwargs["reply_markup"]
            edit_button = next(
                button for row in keyboard.inline_keyboard for button in row
                if button.callback_data == "svc_tasks:open:sound"
            )
            self.assertEqual(
                edit_button.callback_data, "svc_tasks:open:sound"
            )
            self.assertIn(
                "выберите параметр для изменения",
                selection_message.answer.await_args.args[0].lower(),
            )
            self.assertIn(
                "⚙️ ЗВУК",
                selection_message.answer.await_args.args[0],
            )
            settings_response = selection_message.answer.await_args.args[0]
            self.assertIn("⏰ Напоминания групп · 🟢 1", settings_response)
            self.assertIn(
                "⚠️ Отправка не выполняется: служение выключено",
                settings_response,
            )
            self.assertIn("в 16:00", settings_response)
            self.assertIn("Шаблон по умолчанию · 🟢 свой", settings_response)
            self.assertIn("Личные шаблоны · 🟢 1", settings_response)
            self.assertNotIn("Сохранённый групповой текст", settings_response)
            self.assertFalse(settings_response.startswith("✅"))

            callback = SimpleNamespace(
                data=edit_button.callback_data,
                from_user=SimpleNamespace(id=24680),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            await self.callback_handler("on_service_tasks")(callback)
            task_card = await self._click(
                callback.message, lambda button: button.text == "🟢 звук",
                actor_id=24680,
            )
            callback = await self._click(
                task_card.message,
                lambda button: button.text == "⏰ Напоминания и шаблоны",
                actor_id=24680,
            )
            menu = callback.message.answer.await_args.kwargs["reply_markup"]
            self.assertIn(
                "ℹ️ выберите, что изменить",
                callback.message.answer.await_args.args[0].lower(),
            )
            self.assertIn(
                next(
                    button.callback_data
                    for row in menu.inline_keyboard
                    for button in row
                    if button.text.startswith("⏰ Напоминания")
                ),
                [
                    button.callback_data
                    for row in menu.inline_keyboard
                    for button in row
                ],
            )

        asyncio.run(run())

    def message_handler(self, name):
        return next(
            handler.callback
            for handler in self.admin_router.message.handlers
            if handler.callback.__name__ == name
        )

    def start_message(self, actor_id):
        message = self.message(actor_id, "/start")
        message.from_user.first_name = "Анна"
        return message

    @staticmethod
    def callback_data_list(message):
        markup = message.answer.await_args.kwargs.get("reply_markup")
        if markup is None:
            return []
        return [
            button.callback_data
            for row in markup.inline_keyboard
            for button in row
        ]

    def test_start_for_super_admin_shows_all_sections(self):
        async def run():
            message = self.start_message(67890)
            await self.message_handler("on_start")(message)

            self.assertIn(
                "супер-администратор", message.answer.await_args.args[0]
            )
            self.assertEqual(
                self.callback_data_list(message),
                [
                    "menu:service_settings",
                    "menu:service_notify",
                    "menu:service_contact",
                    "menu:service_admins",
                ],
            )

        asyncio.run(run())

    def test_start_for_service_admin_lists_only_own_sections(self):
        async def run():
            self.registry.add_service_admin(67890, "sound", 24680)
            message = self.start_message(24680)
            await self.message_handler("on_start")(message)

            text = message.answer.await_args.args[0]
            self.assertIn("администратор служений: ЗВУК", text)
            # Всё по служению, кроме назначения админов.
            self.assertEqual(
                self.callback_data_list(message),
                ["menu:service_settings", "menu:service_notify",
                 "menu:service_contact"],
            )

        asyncio.run(run())

    def test_start_for_linked_user_offers_personal_notifications(self):
        async def run():
            self.registry.set_responsible_contact(
                67890, "sound", "Микшер", "Алексей", 4321
            )
            message = self.start_message(4321)
            await self.message_handler("on_start")(message)

            self.assertEqual(
                self.callback_data_list(message),
                ["menu:my_schedule", "menu:my_month", "menu:my_notifications"],
            )
            text = message.answer.await_args.args[0]
            self.assertIn("Личные напоминания выключены: ЗВУК", text)
            self.assertIn("/my_notifications", text)
            self.registry.set_user_notifications_enabled(4321, "sound", True)
            again = self.start_message(4321)
            await self.message_handler("on_start")(again)
            self.assertIn(
                "Личные напоминания включены", again.answer.await_args.args[0]
            )

        asyncio.run(run())

    def test_start_for_unlinked_user_shows_telegram_id(self):
        async def run():
            message = self.start_message(5555)
            await self.message_handler("on_start")(message)

            text = message.answer.await_args.args[0]
            self.assertIn("<code>5555</code>", text)
            self.assertNotIn("/services", text)
            self.assertEqual(self.callback_data_list(message), [])

        asyncio.run(run())

    def test_start_menu_button_opens_service_selection(self):
        async def run():
            self.registry.add_service_admin(67890, "sound", 24680)
            callback = SimpleNamespace(
                data="menu:service_notify",
                from_user=SimpleNamespace(id=24680),
                message=self.message(24680, ""),
                answer=AsyncMock(),
            )
            await self.callback_handler("on_start_menu")(callback)

            self.assertIn(
                "Выберите служение для настройки уведомлений",
                callback.message.answer.await_args.args[0],
            )
            self.assertIn(
                "service:service_notify:sound",
                self.callback_data_list(callback.message),
            )

        asyncio.run(run())

    def test_start_menu_admins_item_offers_add_and_remove(self):
        async def run():
            callback = SimpleNamespace(
                data="menu:service_admins",
                from_user=SimpleNamespace(id=67890),
                message=self.message(67890, ""),
                answer=AsyncMock(),
            )
            await self.callback_handler("on_start_menu")(callback)
            self.assertEqual(
                self.callback_data_list(callback.message),
                ["menu:service_admin_add", "menu:service_admin_remove",
                 "menu:start"],
            )
            self.registry.add_service_admin(67890, "sound", 24680)
            denied = SimpleNamespace(
                data="menu:service_admins",
                from_user=SimpleNamespace(id=24680),
                message=self.message(24680, ""),
                answer=AsyncMock(),
            )
            await self.callback_handler("on_start_menu")(denied)
            self.assertIn(
                "супер-администратор", denied.message.answer.await_args.args[0]
            )
            self.assertEqual(
                self.callback_data_list(denied.message), ["menu:start", "menu:start"]
            )

        asyncio.run(run())

    def test_start_menu_rejects_unknown_operation(self):
        async def run():
            callback = SimpleNamespace(
                data="menu:drop_everything",
                from_user=SimpleNamespace(id=24680),
                message=self.message(24680, ""),
                answer=AsyncMock(),
            )
            await self.callback_handler("on_start_menu")(callback)

            callback.message.answer.assert_not_awaited()
            self.assertIn("устарело", callback.answer.await_args.args[0])

        asyncio.run(run())

    def test_start_refreshes_command_menu_for_role(self):
        async def run():
            self.context.bot.set_my_commands = AsyncMock()
            self.context.bot.delete_my_commands = AsyncMock()
            self.registry.add_service_admin(67890, "sound", 24680)

            await self.message_handler("on_start")(self.start_message(24680))
            commands = [
                item.command
                for item in self.context.bot.set_my_commands.await_args.args[0]
            ]
            self.assertIn("service_notify", commands)
            self.assertIn("service_contact", commands)
            self.assertNotIn("service_admin_add", commands)
            self.assertEqual(
                self.context.bot.set_my_commands.await_args.kwargs[
                    "scope"
                ].chat_id,
                24680,
            )

            await self.message_handler("on_start")(self.start_message(5555))
            self.assertEqual(
                self.context.bot.delete_my_commands.await_args.kwargs[
                    "scope"
                ].chat_id,
                5555,
            )

        asyncio.run(run())

    def test_settings_card_shows_group_chat_title(self):
        async def run():
            self.registry.set_group_chat(67890, "sound", -100777, enabled=True)
            self.context.bot.get_chat = AsyncMock(
                return_value=SimpleNamespace(title="Звук <команда>")
            )
            message = self.message(67890, "/service_settings sound")
            await self.message_handler("on_service_settings")(message)

            text = message.answer.await_args.args[0]
            self.assertIn("📡 Групповой чат · «Звук &lt;команда&gt;»", text)
            self.assertNotIn("-100777", text)
            self.context.bot.get_chat.assert_awaited_once_with(-100777)

        asyncio.run(run())

    def test_settings_card_shows_next_send_only_when_deliverable(self):
        async def run():
            self.registry.set_group_chat(67890, "sound", -100777, enabled=True)
            message = self.message(67890, "/service_settings sound")
            await self.message_handler("on_service_settings")(message)
            self.assertIn("✉️ Ближайшая отправка", message.answer.await_args.args[0])

            self.registry.set_service_enabled(67890, "sound", False)
            message = self.message(67890, "/service_settings sound")
            await self.message_handler("on_service_settings")(message)
            text = message.answer.await_args.args[0]
            self.assertNotIn("Ближайшая отправка", text)
            self.assertIn("служение выключено", text)

        asyncio.run(run())

    def test_settings_card_warns_when_group_chat_missing(self):
        async def run():
            self.registry.set_service_enabled(67890, "sound", True)
            message = self.message(67890, "/service_settings sound")
            await self.message_handler("on_service_settings")(message)
            text = message.answer.await_args.args[0]
            self.assertNotIn("Ближайшая отправка", text)
            self.assertIn("не задан групповой чат", text)

        asyncio.run(run())

    def test_settings_card_falls_back_to_chat_id(self):
        async def run():
            self.registry.set_group_chat(67890, "sound", -100777, enabled=True)
            self.context.bot.get_chat = AsyncMock(
                side_effect=TelegramAPIError(method=MagicMock(), message="no")
            )
            message = self.message(67890, "/service_settings sound")
            await self.message_handler("on_service_settings")(message)

            self.assertIn(
                "📡 Групповой чат · <code>-100777</code>",
                message.answer.await_args.args[0],
            )

        asyncio.run(run())

    def personal_flow(self, actor_id):
        """Return helpers that drive /my_notifications screens for one user."""
        message_id = [700]

        def message(text=""):
            message_id[0] += 1
            return SimpleNamespace(
                from_user=SimpleNamespace(id=actor_id, first_name="Анна"),
                chat=SimpleNamespace(type="private", id=actor_id),
                text=text,
                answer=AsyncMock(
                    return_value=SimpleNamespace(message_id=message_id[0])
                ),
            )

        handler = self.callback_handler("on_personal_notifications_callback")

        async def press(source, predicate):
            button = next(
                button
                for row in source.answer.await_args.kwargs[
                    "reply_markup"
                ].inline_keyboard
                for button in row
                if predicate(button)
            )
            result = SimpleNamespace(
                data=button.callback_data,
                from_user=SimpleNamespace(id=actor_id),
                message=message(),
                answer=AsyncMock(),
            )
            await handler(result)
            press.last_callback = result
            return result.message

        return message, press

    def rules(self, actor_id, task="Микшер"):
        entry = self.registry.get_user_text(actor_id, "sound", task)
        if entry is None:
            return []
        return [
            (item["reminder_days"], item["reminder_time"], bool(item["enabled"]))
            for item in self.registry.list_schedules(entry["id"])
        ]

    def test_personal_service_screen_admin_rule_is_toggle_only(self):
        async def run():
            actor_id = 4601
            self.registry.set_responsible_contact(
                67890, "sound", "Микшер", "Имя из расписания", actor_id
            )
            message, press = self.personal_flow(actor_id)
            command = message("/my_notifications")
            await self.message_handler("on_my_notifications")(command)
            screen = await press(command, lambda b: "ЗВУК" in b.text)
            self.assertIn("Напоминаний: 0 (своих — до 10)", screen.answer.await_args.args[0])
            screen = await press(screen, lambda b: b.text == "🔔 Включить")
            self.assertEqual(self.rules(actor_id), [(1, "18:00", True)])
            rule = await press(screen, lambda b: "👤 Микшер" in b.text)
            labels = [
                button.text
                for row in rule.answer.await_args.kwargs["reply_markup"].inline_keyboard
                for button in row
            ]
            self.assertIn("🔕 Выключить", labels)
            self.assertIn("📋 Копировать как своё", labels)
            self.assertNotIn("⏰ Изменить время", labels)
            self.assertNotIn("🗑 Удалить", labels)
            self.assertIn("настроил админ", rule.answer.await_args.args[0])
            await press(rule, lambda b: b.text == "🔕 Выключить")
            self.assertEqual(self.rules(actor_id), [(1, "18:00", False)])

        asyncio.run(run())

    def test_personal_own_rule_add_edit_copy_delete(self):
        async def run():
            actor_id = 4602
            self.registry.set_responsible_contact(
                67890, "sound", "Микшер", "Имя из расписания", actor_id
            )
            message, press = self.personal_flow(actor_id)
            command = message("/my_notifications")
            await self.message_handler("on_my_notifications")(command)
            screen = await press(command, lambda b: "ЗВУК" in b.text)
            screen = await press(screen, lambda b: b.text == "➕ Своё напоминание")
            self.assertIn("Когда напомнить", screen.answer.await_args.args[0])
            await press(screen, lambda b: b.text == "✏️ Ввести вручную")
            text_handler = self.message_handler("on_service_parameter")
            invalid = message("9 07:30")
            await text_handler(invalid)
            self.assertIn("от 0 до 6", invalid.answer.await_args.args[0])
            rule = message("0 07:30")
            await text_handler(rule)
            self.assertEqual(self.rules(actor_id), [(0, "07:30", True)])
            self.assertIn("✏️ Своё напоминание", rule.answer.await_args.args[0])
            # Своё напоминание не включает всё служение: иначе заработали бы
            # и остальные напоминания.
            self.assertFalse(
                self.registry.get_user_notifications_enabled(actor_id, "sound")
            )
            self.assertIn(
                "уведомления служения выключены", rule.answer.await_args.args[0]
            )

            await press(rule, lambda b: b.text == "✏️ Изменить текст")
            wrong = message("Текст {unknown}")
            await text_handler(wrong)
            self.assertIn("⚠️", wrong.answer.await_args.args[0])
            saved = message("Не забудь пульт, {name}")
            await text_handler(saved)
            self.assertIn("Не забудь пульт", saved.answer.await_args.args[0])

            copy = await press(saved, lambda b: b.text == "📋 Копировать как своё")
            self.assertIn("выключено", copy.answer.await_args.args[0])
            self.assertEqual(
                sorted(self.rules(actor_id)),
                [(0, "07:30", False), (0, "07:30", True)],
            )
            confirm = await press(copy, lambda b: b.text == "🗑 Удалить")
            service = await press(confirm, lambda b: b.text == "🗑 Да, удалить")
            self.assertEqual(self.rules(actor_id), [(0, "07:30", True)])
            self.assertIn("Напоминаний: 1 (своих — до 10)", service.answer.await_args.args[0])

        asyncio.run(run())

    def test_copy_of_legacy_long_text_is_shortened(self):
        async def run():
            import sqlite3 as _sqlite3

            actor_id = 4604
            self.registry.set_responsible_contact(
                67890, "sound", "Микшер", "Имя из расписания", actor_id
            )
            text_id = self.registry.ensure_user_text(actor_id, "sound", "Микшер")
            rule = self.registry.add_schedule(text_id, 2, "10:00", origin="user")
            with _sqlite3.connect(self.registry.path) as connection:
                connection.execute(
                    "UPDATE user_reminder_schedules SET body = ? WHERE id = ?",
                    ("длинно {name} " * 400 + "{name}", rule["id"]),
                )
            message, press = self.personal_flow(actor_id)
            command = message("/my_notifications")
            await self.message_handler("on_my_notifications")(command)
            screen = await press(command, lambda b: "ЗВУК" in b.text)
            card = await press(screen, lambda b: "✏️ Микшер" in b.text)
            await press(card, lambda b: b.text == "📋 Копировать как своё")
            bodies = [
                item["body"] for item in
                self.registry.list_user_service_schedules(actor_id, "sound")
            ]
            self.assertEqual(len(bodies), 2)
            self.assertLessEqual(min(len(body) for body in bodies), 3000)
            # Длинное слово без пробелов не теряется целиком.
            with _sqlite3.connect(self.registry.path) as connection:
                connection.execute(
                    "UPDATE user_reminder_schedules SET body = ? WHERE id = ?",
                    ("я" * 2999 + "{name} b\nконец", rule["id"]),
                )
            command = message("/my_notifications")
            await self.message_handler("on_my_notifications")(command)
            screen = await press(command, lambda b: "ЗВУК" in b.text)
            original = await press(
                screen, lambda b: "✏️ Микшер" in b.text and "10:00" in b.text
            )
            await press(original, lambda b: b.text == "📋 Копировать как своё")
            copies = [
                item["body"] for item in
                self.registry.list_user_service_schedules(actor_id, "sound")
                if item["id"] != rule["id"]
            ]
            self.assertIn("я" * 2999, copies)

        asyncio.run(run())

    def test_personal_rules_limited_to_ten_per_service(self):
        async def run():
            actor_id = 4603
            self.registry.set_responsible_contact(
                67890, "sound", "Микшер", "Имя из расписания", actor_id
            )
            text_id = self.registry.ensure_user_text(actor_id, "sound", "Микшер")
            for index in range(10):
                self.registry.add_schedule(
                    text_id, index % 7, f"1{index}:00", origin="user"
                )
            with self.assertRaises(ValueError):
                self.registry.add_schedule(text_id, 0, "09:00", origin="user")
            message, press = self.personal_flow(actor_id)
            command = message("/my_notifications")
            await self.message_handler("on_my_notifications")(command)
            screen = await press(command, lambda b: "ЗВУК" in b.text)
            labels = [
                button.text
                for row in screen.answer.await_args.kwargs["reply_markup"].inline_keyboard
                for button in row
            ]
            self.assertNotIn("➕ Своё напоминание", labels)

        asyncio.run(run())

    def personal_toggle(self, actor_id):
        async def run():
            message, press = self.personal_flow(actor_id)
            command = message("/my_notifications")
            await self.message_handler("on_my_notifications")(command)
            screen = await press(command, lambda b: "ЗВУК" in b.text)
            await press(screen, lambda b: b.text == "🔔 Включить")
            args = press.last_callback.answer.await_args.args
            return args[0] if args else None

        return run()

    def test_enable_keeps_existing_enabled_rule_instead_of_default(self):
        async def run():
            actor_id = 4603
            self.registry.set_responsible_contact(
                67890, "sound", "Микшер", "Имя из расписания", actor_id
            )
            text_id = self.registry.upsert_user_text(
                actor_id, "sound", "Микшер", None
            )
            self.registry.add_schedule(text_id, 3, "09:00")
            await self.personal_toggle(actor_id)

            self.assertEqual(self.rules(actor_id), [(3, "09:00", True)])
            self.assertTrue(
                self.registry.get_user_notifications_enabled(actor_id, "sound")
            )

        asyncio.run(run())

    def test_enable_is_refused_when_all_current_rules_are_disabled(self):
        async def run():
            actor_id = 4609
            self.registry.set_responsible_contact(
                67890, "sound", "Микшер", "Имя из расписания", actor_id
            )
            text_id = self.registry.upsert_user_text(
                actor_id, "sound", "Микшер", None
            )
            rule = self.registry.add_schedule(text_id, 3, "09:00")
            self.registry.update_schedule(rule["id"], enabled=False)
            toast = await self.personal_toggle(actor_id)

            self.assertEqual(self.rules(actor_id), [(3, "09:00", False)])
            self.assertFalse(
                self.registry.get_user_notifications_enabled(actor_id, "sound")
            )
            self.assertIn("выключены", toast)

        asyncio.run(run())

    def test_enable_ignores_rules_of_tasks_missing_from_schedule(self):
        async def run():
            actor_id = 4610
            for task in ("Микшер", "Пульт"):
                self.registry.set_responsible_contact(
                    67890, "sound", task, "Имя из расписания", actor_id
                )
            stale = self.registry.upsert_user_text(
                actor_id, "sound", "Пульт", None
            )
            self.registry.add_schedule(stale, 2, "10:00")
            await self.personal_toggle(actor_id)

            self.assertEqual(self.rules(actor_id), [(1, "18:00", True)])
            self.assertEqual(
                self.rules(actor_id, "Пульт"), [(2, "10:00", True)]
            )
            self.assertTrue(
                self.registry.get_user_notifications_enabled(actor_id, "sound")
            )

        asyncio.run(run())

    def test_enable_is_refused_when_only_stale_rules_exist(self):
        async def run():
            actor_id = 4611
            self.registry.set_responsible_contact(
                67890, "sound", "Пульт", "Имя из расписания", actor_id
            )
            stale = self.registry.upsert_user_text(
                actor_id, "sound", "Пульт", None
            )
            self.registry.add_schedule(stale, 2, "10:00")
            toast = await self.personal_toggle(actor_id)

            self.assertFalse(
                self.registry.get_user_notifications_enabled(actor_id, "sound")
            )
            self.assertIn("нет ваших задач", toast)

        asyncio.run(run())

    def _two_current_tasks(self, actor_id):
        for task in ("Микшер", "Пульт"):
            self.registry.set_responsible_contact(
                67890, "sound", task, "Имя из расписания", actor_id
            )
        self.context.load_service_task_assignments = AsyncMock(
            return_value={
                "Микшер": ["Имя из расписания"],
                "Пульт": ["Имя из расписания"],
            }
        )
        return self.registry.upsert_user_text(
            actor_id, "sound", "Микшер", None
        )

    def test_enable_adds_default_rule_for_uncovered_task(self):
        async def run():
            actor_id = 4612
            text_id = self._two_current_tasks(actor_id)
            self.registry.add_schedule(text_id, 3, "09:00")
            await self.personal_toggle(actor_id)

            self.assertEqual(self.rules(actor_id), [(3, "09:00", True)])
            self.assertEqual(
                self.rules(actor_id, "Пульт"), [(1, "18:00", True)]
            )
            self.assertTrue(
                self.registry.get_user_notifications_enabled(actor_id, "sound")
            )

        asyncio.run(run())

    def test_enable_sees_bindings_created_by_schedule_loader(self):
        async def run():
            actor_id = 4612
            self.registry.bind_service_member(
                67890, "sound", "Имя из расписания", actor_id, tasks=("Микшер",),
            )
            assignments = {
                "Микшер": ["Имя из расписания"],
                "Пульт": ["Имя из расписания"],
            }

            async def load(service_id):
                self.registry.ensure_member_contacts(service_id, assignments)
                return assignments

            self.context.load_service_task_assignments = load
            await self.personal_toggle(actor_id)
            self.assertEqual(self.rules(actor_id, "Пульт"), [(1, "18:00", True)])

        asyncio.run(run())

    def test_enable_with_disabled_rule_still_covers_other_task(self):
        async def run():
            actor_id = 4613
            text_id = self._two_current_tasks(actor_id)
            rule = self.registry.add_schedule(text_id, 3, "09:00")
            self.registry.update_schedule(rule["id"], enabled=False)
            await self.personal_toggle(actor_id)

            self.assertEqual(self.rules(actor_id), [(3, "09:00", False)])
            self.assertEqual(
                self.rules(actor_id, "Пульт"), [(1, "18:00", True)]
            )
            self.assertTrue(
                self.registry.get_user_notifications_enabled(actor_id, "sound")
            )

        asyncio.run(run())

    def test_enable_with_stored_rule_is_refused_when_schedule_unavailable(self):
        async def run():
            actor_id = 4614
            self.registry.set_responsible_contact(
                67890, "sound", "Микшер", "Имя из расписания", actor_id
            )
            text_id = self.registry.upsert_user_text(
                actor_id, "sound", "Микшер", None
            )
            self.registry.add_schedule(text_id, 3, "09:00")
            message, press = self.personal_flow(actor_id)
            command = message("/my_notifications")
            await self.message_handler("on_my_notifications")(command)
            screen = await press(command, lambda b: "ЗВУК" in b.text)
            self.context.load_service_task_assignments = AsyncMock(
                side_effect=RuntimeError("offline")
            )
            with patch("bot.handlers.PERSONAL_TASK_CACHE_TTL_SECONDS", -1):
                await press(screen, lambda b: b.text == "🔔 Включить")

            self.assertFalse(
                self.registry.get_user_notifications_enabled(actor_id, "sound")
            )
            self.assertIn(
                "не загрузился",
                press.last_callback.answer.await_args.args[0],
            )

        asyncio.run(run())

    def test_bulk_enable_adds_default_rule_per_service(self):
        async def run():
            actor_id = 4604
            self.registry.set_responsible_contact(
                67890, "sound", "Микшер", "Имя из расписания", actor_id
            )
            message, press = self.personal_flow(actor_id)
            command = message("/my_notifications")
            await self.message_handler("on_my_notifications")(command)
            await press(command, lambda b: b.text == "✅ Включить все")

            self.assertEqual(self.rules(actor_id), [(1, "18:00", True)])

        asyncio.run(run())

    def test_default_rule_skipped_when_schedule_unavailable(self):
        async def run():
            actor_id = 4605
            self.registry.set_responsible_contact(
                67890, "sound", "Микшер", "Имя из расписания", actor_id
            )
            message, press = self.personal_flow(actor_id)
            command = message("/my_notifications")
            await self.message_handler("on_my_notifications")(command)
            self.context.load_service_task_assignments = AsyncMock(
                side_effect=OSError("offline")
            )
            await press(command, lambda b: b.text == "✅ Включить все")

            self.assertFalse(
                self.registry.get_user_notifications_enabled(actor_id, "sound")
            )
            self.assertEqual(self.rules(actor_id), [])

        asyncio.run(run())

    def test_bulk_enable_reports_services_that_could_not_get_a_rule(self):
        async def run():
            actor_id = 4606
            for service_id, task in (("sound", "Микшер"), ("books", "Слайды")):
                self.registry.set_responsible_contact(
                    67890, service_id, task, "Имя из расписания", actor_id
                )
            slides_text = self.registry.upsert_user_text(
                actor_id, "books", "Слайды", None
            )
            self.registry.add_schedule(slides_text, 2, "10:00")
            self.context.load_service_task_assignments = AsyncMock(
                side_effect=OSError("offline")
            )
            message, press = self.personal_flow(actor_id)
            command = message("/my_notifications")
            await self.message_handler("on_my_notifications")(command)
            button = next(
                button
                for row in command.answer.await_args.kwargs[
                    "reply_markup"
                ].inline_keyboard
                for button in row
                if button.text == "✅ Включить все"
            )
            callback = SimpleNamespace(
                data=button.callback_data,
                from_user=SimpleNamespace(id=actor_id),
                message=message(),
                answer=AsyncMock(),
            )
            await self.callback_handler("on_personal_notifications_callback")(
                callback
            )

            # Без графика сохранённое правило не проверить: оба служения
            # остаются выключенными, а не только то, где правил нет.
            self.assertFalse(
                self.registry.get_user_notifications_enabled(actor_id, "sound")
            )
            self.assertFalse(
                self.registry.get_user_notifications_enabled(actor_id, "books")
            )
            toast = callback.answer.await_args.args[0]
            self.assertIn("ЗВУК", toast)
            self.assertIn("график не загрузился", toast)

        asyncio.run(run())

    def test_enable_is_refused_when_rule_cannot_be_created(self):
        async def run():
            actor_id = 4607
            self.registry.set_responsible_contact(
                67890, "sound", "Микшер", "Имя из расписания", actor_id
            )
            message, press = self.personal_flow(actor_id)
            command = message("/my_notifications")
            await self.message_handler("on_my_notifications")(command)
            screen = await press(command, lambda b: "ЗВУК" in b.text)
            self.context.load_service_task_assignments = AsyncMock(
                side_effect=RuntimeError("offline")
            )
            with patch("bot.handlers.PERSONAL_TASK_CACHE_TTL_SECONDS", -1):
                button = next(
                    button
                    for row in screen.answer.await_args.kwargs[
                        "reply_markup"
                    ].inline_keyboard
                    for button in row
                    if button.text == "🔔 Включить"
                )
                callback = SimpleNamespace(
                    data=button.callback_data,
                    from_user=SimpleNamespace(id=actor_id),
                    message=message(),
                    answer=AsyncMock(),
                )
                await self.callback_handler(
                    "on_personal_notifications_callback"
                )(callback)

            self.assertFalse(
                self.registry.get_user_notifications_enabled(actor_id, "sound")
            )
            self.assertIn("не загрузился", callback.answer.await_args.args[0])

        asyncio.run(run())

    def test_enable_is_refused_when_user_has_no_current_tasks(self):
        async def run():
            actor_id = 4608
            self.registry.set_responsible_contact(
                67890, "sound", "Микшер", "Имя из расписания", actor_id
            )
            self.context.load_service_task_assignments = AsyncMock(
                return_value={"Пульт": ["Иван"]}
            )
            message, press = self.personal_flow(actor_id)
            command = message("/my_notifications")
            await self.message_handler("on_my_notifications")(command)
            button = next(
                button
                for row in command.answer.await_args.kwargs[
                    "reply_markup"
                ].inline_keyboard
                for button in row
                if button.text == "✅ Включить все"
            )
            callback = SimpleNamespace(
                data=button.callback_data,
                from_user=SimpleNamespace(id=actor_id),
                message=message(),
                answer=AsyncMock(),
            )
            await self.callback_handler("on_personal_notifications_callback")(
                callback
            )

            self.assertFalse(
                self.registry.get_user_notifications_enabled(actor_id, "sound")
            )
            self.assertEqual(self.rules(actor_id), [])
            self.assertIn(
                "нет ваших задач в графике", callback.answer.await_args.args[0]
            )

        asyncio.run(run())

    def test_my_schedule_lists_only_users_upcoming_tasks(self):
        async def run():
            actor_id = 4701
            self.registry.set_responsible_contact(
                67890, "sound", "Микшер", "Алексей", actor_id
            )
            self.context.load_upcoming_service_events = AsyncMock(
                return_value=[
                    ServiceEvent(
                        "sound",
                        date(2026, 10, 25),
                        (
                            ServiceAssignment("Микшер", "Алексей"),
                            ServiceAssignment("Праздник", "Алексей", True),
                            ServiceAssignment("Пульт", "Мария"),
                        ),
                    ),
                    ServiceEvent(
                        "sound",
                        date(2026, 11, 1),
                        (ServiceAssignment("Микшер", "Мария"),),
                    ),
                    ServiceEvent(
                        "sound",
                        date(2026, 11, 8),
                        (ServiceAssignment("Микшер", "Алексей"),),
                    ),
                ]
            )
            message = self.message(actor_id, "/my_schedule")
            await self.message_handler("on_my_schedule")(message)

            self.context.load_upcoming_service_events.assert_awaited_once_with(
                approved_only=True
            )
            text = message.answer.await_args.args[0]
            self.assertIn("🗓 25.10.2026, вс", text)
            self.assertIn("🎚️ ЗВУК · Микшер", text)
            self.assertIn("🗓 08.11.2026, вс", text)
            self.assertNotIn("01.11.2026", text)
            self.assertNotIn("Праздник", text)
            self.assertNotIn("Пульт", text)
            self.assertEqual(
                self.callback_data_list(message),
                ["menu:my_month", "menu:my_notifications", "menu:start"],
            )

        asyncio.run(run())

    def test_my_schedule_reports_empty_and_failed_loads(self):
        async def run():
            actor_id = 4702
            self.registry.set_responsible_contact(
                67890, "sound", "Микшер", "Алексей", actor_id
            )
            self.context.load_upcoming_service_events = AsyncMock(return_value=[])
            message = self.message(actor_id, "/my_schedule")
            await self.message_handler("on_my_schedule")(message)
            self.assertIn("назначений в графике нет", message.answer.await_args.args[0])

            self.context.load_upcoming_service_events = AsyncMock(
                side_effect=RuntimeError("bad workbook")
            )
            message = self.message(actor_id, "/my_schedule")
            await self.message_handler("on_my_schedule")(message)
            self.assertIn(
                "Не удалось загрузить график", message.answer.await_args.args[0]
            )

            message = self.message(5555, "/my_schedule")
            await self.message_handler("on_my_schedule")(message)
            self.assertIn("не привязаны", message.answer.await_args.args[0])

        asyncio.run(run())

    def test_start_menu_opens_my_schedule_and_home(self):
        async def run():
            actor_id = 4703
            self.registry.set_responsible_contact(
                67890, "sound", "Микшер", "Алексей", actor_id
            )
            self.context.load_upcoming_service_events = AsyncMock(return_value=[])
            for data, expected in (
                ("menu:my_schedule", "📅 Мои служения"),
                ("menu:start", "Здравствуйте, Анна"),
            ):
                callback = SimpleNamespace(
                    data=data,
                    from_user=SimpleNamespace(id=actor_id, first_name="Анна"),
                    message=self.message(actor_id, ""),
                    answer=AsyncMock(),
                )
                await self.callback_handler("on_start_menu")(callback)
                self.assertIn(expected, callback.message.answer.await_args.args[0])

        asyncio.run(run())

    def test_service_card_is_hub_for_super_admin(self):
        async def run():
            message = self.message(67890, "/service_settings sound")
            await self.message_handler("on_service_settings")(message)

            self.assertEqual(
                self.callback_data_list(message),
                [
                    "service:service_contact:sound",
                    "rdy:month:sound",
                    "service:service_reminders:sound",
                    "svc_tasks:open:sound",
                    "service:service_setup:sound",
                    "service:back_to_services:list",
                    "service:service_help:sound",
                ],
            )
            self.assertIn(
                "👤 Администраторы · ⚪ не назначены",
                message.answer.await_args.args[0],
            )

            def callback(data, actor_id=67890):
                return SimpleNamespace(
                    data=data, from_user=SimpleNamespace(id=actor_id),
                    message=self.message(actor_id, ""), answer=AsyncMock(),
                )

            # «⚙️ Настройки»: групповой чат и админы (у суперадмина).
            setup = callback("service:service_setup:sound")
            await self.callback_handler("on_service_selected")(setup)
            self.assertEqual(
                self.callback_data_list(setup.message),
                ["service:service_chat:sound", "service:service_admin_add:sound",
                 "service:service_admin_remove:sound",
                 "service:service_chat_disable:sound",
                 "service:service_settings:sound"],
            )
            # Проверка доставки — в «📡 Групповой чат», когда чат назначен.
            chat = callback("service:service_chat:sound")
            await self.callback_handler("on_service_selected")(chat)
            self.assertNotIn(
                "service:test_notify:sound", self.callback_data_list(chat.message)
            )
            self.registry.set_group_chat(67890, "sound", -200, enabled=True)
            chat = callback("service:service_chat:sound")
            await self.callback_handler("on_service_selected")(chat)
            buttons = self.callback_data_list(chat.message)
            self.assertEqual(
                buttons[buttons.index("service:service_chat_set:sound") + 1],
                "service:test_notify:sound",
            )
            helped = callback("service:service_help:sound")
            await self.callback_handler("on_service_selected")(helped)
            self.assertIn(
                "⚙️ Настройки — групповой чат",
                helped.message.answer.await_args.args[0],
            )
            text = helped.message.answer.await_args.args[0]
            paragraphs = text.split("\n\n")
            self.assertEqual(len(paragraphs), 9)
            for paragraph, prefix in zip(paragraphs, (
                "<b>❓ Кнопки карточки служения</b>",
                "👥 Ответственные — список братьев и сестер служения:\nTelegram",
                "«🗓 По датам графика»", "📅 График",
                "За кнопкой «🗳 Опрос»", "🔔 Напомнить",
                "📋 Задачи служения", "⚙️ Настройки", "⬅️ К списку служений",
            )):
                self.assertTrue(paragraph.startswith(prefix), paragraph)
            self.assertIn("сколько ответственных требуется", text)
            self.assertIn("тексты для сообщений в чат.", text)

        asyncio.run(run())

    def test_super_admin_toggles_and_adds_service_tasks(self):
        async def run():
            card = self.message(67890, "/service_settings sound")
            await self.message_handler("on_service_settings")(card)
            opened = SimpleNamespace(
                data="svc_tasks:open:sound",
                from_user=SimpleNamespace(id=67890),
                message=self.message(67890, ""),
                answer=AsyncMock(),
            )
            opened.message.delete = AsyncMock()
            await self.callback_handler("on_service_tasks")(opened)
            # Карточка служения не остаётся под экраном задач.
            opened.message.delete.assert_awaited_once()
            screen = opened.message.answer.await_args.args[0]
            self.assertIn("ЗВУК · задачи", screen)
            self.assertIn("🔴 🎶 спевка · выключена", screen)

            task_card = await self._click(
                opened.message,
                lambda button: button.text == "🔴 спевка",
                actor_id=67890,
            )
            toggle = await self._click(
                task_card.message,
                lambda button: button.text == "▶️ Включить задачу",
                actor_id=67890,
            )
            self.assertTrue(
                next(
                    task for task in self.registry.list_service_tasks("sound")
                    if task["name"] == "спевка"
                )["enabled"]
            )
            await self._click(
                toggle.message,
                lambda button: button.text == "➕ Добавить задачу",
                actor_id=67890,
            )
            added = await self._type("Перевозка", actor_id=67890)
            self.assertIn(
                "✅ Задача «Перевозка» добавлена",
                added.answer.await_args.args[0],
            )
            self.assertIn(
                "Перевозка",
                [task["name"] for task in self.registry.list_service_tasks("sound")],
            )

        asyncio.run(run())

    def test_admin_sets_own_day_for_task(self):
        async def run():
            self.registry.add_service_admin(67890, "sound", 24680)
            opened = SimpleNamespace(
                data="svc_tasks:open:sound",
                from_user=SimpleNamespace(id=24680),
                message=self.message(24680, ""),
                answer=AsyncMock(),
            )
            await self.callback_handler("on_service_tasks")(opened)
            days = await self._click(
                opened.message,
                lambda button: button.text == "📅 Дни задач",
                actor_id=24680,
            )
            self.assertIn("🎚️ звук — дни служения", days.message.answer.await_args.args[0])
            pick = await self._click(
                days.message,
                lambda button: button.text == "📅 звук",
                actor_id=24680,
            )
            chosen = await self._click(
                pick.message,
                lambda button: button.text == "📅 ср (вс -4)",
                actor_id=24680,
            )
            screen = chosen.message.answer.await_args.args[0]
            self.assertIn("✅ «звук»: ср (вс -4)", screen)
            self.assertIn("🎚️ звук — ср (вс -4)", screen)
            self.assertEqual(
                next(
                    task for task in self.registry.list_service_tasks("sound")
                    if task["name"] == "звук"
                )["day_offset"],
                -4,
            )
            back = await self._click(
                chosen.message,
                lambda button: button.text == "📅 звук",
                actor_id=24680,
            )
            reset = await self._click(
                back.message,
                lambda button: button.text == "↩️ Дни служения",
                actor_id=24680,
            )
            self.assertIn(
                "✅ «звук»: дни служения", reset.message.answer.await_args.args[0]
            )
            tasks = await self._click(
                reset.message,
                lambda button: button.text == "⬅️ К задачам",
                actor_id=24680,
            )
            self.assertIn("ЗВУК · задачи", tasks.message.answer.await_args.args[0])

        asyncio.run(run())

    def test_super_admin_sets_people_needed_for_task(self):
        async def run():
            opened = SimpleNamespace(
                data="svc_tasks:open:sound",
                from_user=SimpleNamespace(id=67890),
                message=self.message(67890, ""),
                answer=AsyncMock(),
            )
            await self.callback_handler("on_service_tasks")(opened)
            people = await self._click(
                opened.message,
                lambda button: button.text == "👥 Нужно людей",
                actor_id=67890,
            )
            choice = await self._click(
                people.message,
                lambda button: button.text.startswith("👥 звук"),
                actor_id=67890,
            )
            saved = await self._click(
                choice.message,
                lambda button: button.text == "👥 3–4",
                actor_id=67890,
            )
            self.assertIn(
                "✅ «звук»: нужно людей — 3–4",
                saved.message.answer.await_args.args[0],
            )
            task = next(
                item for item in self.registry.list_service_tasks("sound")
                if item["name"] == "звук"
            )
            self.assertEqual((task["people_needed"], task["people_max"]), (3, 4))
            back = await self._click(
                saved.message,
                lambda button: button.text == "⬅️ К задачам",
                actor_id=67890,
            )
            self.assertIn("👥 3–4", back.message.answer.await_args.args[0])

        asyncio.run(run())

    def test_readiness_screen_settings_skip_start_and_votes(self):
        self.registry.set_group_chat(67890, "sound", -1005555, enabled=True)
        self.context.bot.send_poll = AsyncMock(return_value=SimpleNamespace(
            message_id=12, poll=SimpleNamespace(id="ready-poll"),
        ))
        self.context.bot.send_message.return_value = SimpleNamespace(
            message_id=11
        )
        handler = self.callback_handler("on_readiness")
        today = datetime.now(ZoneInfo("UTC")).date()
        year, month = (
            (today.year + 1, 1) if today.month == 12
            else (today.year, today.month + 1)
        )
        month_arg = f"{year:04d}{month:02d}"

        async def press(data, actor_id=67890):
            callback = SimpleNamespace(
                data=data,
                from_user=SimpleNamespace(id=actor_id),
                message=self.message(actor_id, ""),
                answer=AsyncMock(),
            )
            await handler(callback)
            return callback

        def buttons(callback):
            return [
                button
                for row in callback.message.answer.await_args.kwargs[
                    "reply_markup"
                ].inline_keyboard
                for button in row
            ]

        async def run():
            screen = await press("rdy:open:sound")
            text = screen.message.answer.await_args.args[0]
            self.assertIn("🗳 Опрос готовности", text)
            self.assertIn("🔴 выключен", text)
            self.assertIn(
                "rdy:rev:sound",
                [button.callback_data for button in buttons(screen)],
            )
            await press("rdy:rev:sound")
            self.assertTrue(
                self.registry.get_readiness_settings("sound")["reverse_poll"]
            )
            await press("rdy:rev:sound")
            self.assertFalse(
                self.registry.get_readiness_settings("sound")["reverse_poll"]
            )
            denied = await press("rdy:open:sound", actor_id=24680)
            self.assertIn("Недостаточно прав", denied.answer.await_args.args[0])

            await press("rdy:toggle:sound")
            self.assertTrue(
                self.registry.get_readiness_settings("sound")["enabled"]
            )
            await press("rdy:dur:sound:2")
            await press("rdy:time:sound:0900")
            settings = self.registry.get_readiness_settings("sound")
            self.assertEqual(
                (settings["duration_days"], settings["start_time"]), (2, "09:00")
            )

            skipped = await press(f"rdy:skip:sound:{month_arg}")
            self.assertIn("опрос не запустится",
                          skipped.message.answer.await_args.args[0])
            await press(f"rdy:unskip:sound:{month_arg}")
            started = await press(f"rdy:start:sound:{month_arg}")
            self.assertIn("✅ Опрос отправлен",
                          started.message.answer.await_args.args[0])
            self.context.bot.send_poll.assert_awaited_once()
            again = await press(f"rdy:start:sound:{month_arg}")
            self.assertIn("⏹ Завершить", str(
                again.message.answer.await_args.kwargs["reply_markup"]
            ))

            poll_answer = [
                item.callback for item in router.poll_answer.handlers
                if item.callback.__name__ == "on_replacement_poll_answer"
            ][-1]
            await poll_answer(SimpleNamespace(
                poll_id="ready-poll", option_ids=[0, 1],
                user=SimpleNamespace(
                    id=12345, is_bot=False, username="alex",
                    first_name="Алексей", last_name=None,
                ),
            ))
            run = next(
                item for item in self.registry.list_open_readiness_runs()
                if item["poll_id"] == "ready-poll"
            )
            answers = self.registry.list_readiness_answers(run["id"])
            self.assertEqual(answers[0]["dates"], run["dates"][:2])

            stale = await press("rdy:finish:sound:200001")
            self.assertIn(
                "Экран устарел", stale.message.answer.await_args.args[0]
            )
            texts = await press("rdy:texts:sound")
            self.assertIn("📝 Тексты опроса", texts.message.answer.await_args.args[0])
            self.assertIn("❓ Вопрос опроса", texts.message.answer.await_args.args[0])
            self.assertIn("📅 Заголовок графика", texts.message.answer.await_args.args[0])
            title = await press("rdy:tplprev:sound:pub")
            self.assertIn(
                "заголовок графика в чате служения",
                title.message.answer.await_args.args[0],
            )
            self.assertIn(" утверждён", title.message.answer.await_args.args[0])
            title_edit = await press("rdy:tpledit:sound:pub")
            self.assertIn(
                "• {month} — ", title_edit.message.answer.await_args.args[0]
            )
            update = await press("rdy:tplprev:sound:pubupd")
            self.assertIn(" обновлён", update.message.answer.await_args.args[0])
            # «🔤 Фразы»: своя фраза и возврат встроенной.
            phrases = await press("rdy:phr:sound")
            self.assertIn(
                "⚪ Последний вариант в опросе: «❌ Не смогу в этом месяце»",
                phrases.message.answer.await_args.args[0],
            )
            phrase = await press("rdy:tpl:sound:ph.cant")
            self.assertNotIn(
                "Предпросмотр",
                str(phrase.message.answer.await_args.kwargs["reply_markup"]),
            )
            await press("rdy:tpledit:sound:ph.cant")
            phrase_input = self.message(67890, "🙈 В этом месяце не смогу")
            await next(
                item.callback for item in self.admin_router.message.handlers
                if item.callback.__name__ == "on_service_parameter"
            )(phrase_input)
            self.assertIn("✅ Текст сохранён", phrase_input.answer.await_args.args[0])
            self.assertEqual(
                self.registry.get_readiness_settings("sound")["phrases"],
                {"cant": "🙈 В этом месяце не смогу"},
            )
            await press("rdy:tplreset:sound:ph.cant")
            self.assertEqual(
                self.registry.get_readiness_settings("sound")["phrases"], {}
            )
            # Лимит служений в месяц для служения: готовый и «Вручную».
            limit = await press("rdy:lim:sound")
            self.assertIn(
                "Служений в месяц для всех", limit.message.answer.await_args.args[0]
            )
            await press("rdy:lim:sound:1_3")
            settings = self.registry.get_readiness_settings("sound")
            self.assertEqual((settings["month_min"], settings["month_max"]), (1, 3))
            manual = await press("rdy:lim:sound:m2")
            labels = [
                button.text for row in manual.message.answer.await_args.kwargs[
                    "reply_markup"
                ].inline_keyboard for button in row
            ]
            self.assertIn("🕒 без верхней границы", labels)
            self.assertNotIn("🕒 1", labels)
            section = await press("rdy:lim:sound:2_x")
            self.assertIn(
                "🔢 Служений в месяц: от 2", section.message.answer.await_args.args[0]
            )
            await press("rdy:lim:sound:n")
            settings = self.registry.get_readiness_settings("sound")
            self.assertEqual((settings["month_min"], settings["month_max"]), (0, None))
            await press("rdy:texts:sound")
            question = await press("rdy:tplprev:sound:q")
            self.assertIn(
                "Когда можешь служить в", question.message.answer.await_args.args[0]
            )
            text_screen = await press("rdy:tpl:sound:intro")
            self.assertIn(
                "Используется встроенный текст",
                text_screen.message.answer.await_args.args[0],
            )
            preview = await press("rdy:tplprev:sound:intro")
            self.assertIn(
                "Спасибо за твое служение",
                preview.message.answer.await_args.args[0],
            )
            prompt = await press("rdy:tpledit:sound:intro")
            self.assertIn(
                "• {people} — ", prompt.message.answer.await_args.args[0]
            )
            text_handler = next(
                item.callback for item in self.admin_router.message.handlers
                if item.callback.__name__ == "on_service_parameter"
            )
            bad = self.message(67890, "Привет {name}")
            await text_handler(bad)
            self.assertIn("не поддерживается", bad.answer.await_args.args[0])
            good = self.message(67890, "Опрос {service_name} на {month}")
            await text_handler(good)
            self.assertIn("✅ Текст сохранён", good.answer.await_args.args[0])
            self.assertEqual(
                self.registry.get_readiness_settings("sound")["intro_template"],
                "Опрос {service_name} на {month}",
            )
            await press("rdy:tplreset:sound:intro")
            self.assertIsNone(
                self.registry.get_readiness_settings("sound")["intro_template"]
            )
            progress = await press(f"rdy:progress:sound:{month_arg}")
            self.assertIn(
                "Ход опроса", progress.message.answer.await_args.args[0]
            )
            # Замены — в разделе «📅 График» (решение 17, 5в).
            section = await press("rdy:month:sound")
            self.assertIn(
                "rdy:repl:sound",
                str(section.message.answer.await_args.kwargs["reply_markup"]),
            )
            # Пока опрос идёт — предварительный график, без утверждения.
            draft_screen = await press(f"rdy:pprev:sound:{month_arg}")
            draft_text = draft_screen.message.answer.await_args.args[0]
            self.assertIn("⏳ Предварительный — опрос идёт", draft_text)
            self.assertNotIn(
                "Утвердить",
                str(draft_screen.message.answer.await_args.kwargs["reply_markup"]),
            )
            # Ответивший вне состава — «➕ В состав» с выбором задач.
            add_button = next(
                button.callback_data
                for row in progress.message.answer.await_args.kwargs[
                    "reply_markup"
                ].inline_keyboard
                for button in row
                if button.text.startswith("➕")
            )
            self.assertEqual(add_button, f"rdy:addm:sound:{month_arg}.12345")
            old_month = await press("rdy:addm:sound:200001.12345")
            self.assertIn("Экран устарел",
                          old_month.message.answer.await_args.args[0])
            # Уже в составе без задач — задачи привязываются к нему, а не
            # ко второму участнику с именем из Telegram.
            self.registry.bind_service_member(
                67890, "sound", "Алексей из Списков", 12345
            )
            member_screen = await press(add_button)
            token = next(
                button.callback_data.split(":")[-1]
                for row in member_screen.message.answer.await_args.kwargs[
                    "reply_markup"
                ].inline_keyboard
                for button in row
                if button.callback_data.startswith("rdy:addms:")
            )
            stale = await press("rdy:addms:sound:old")
            self.assertIn("устарел", stale.answer.await_args.args[0])
            empty = await press(f"rdy:addms:sound:{token}")
            self.assertIn("хотя бы одну задачу",
                          empty.message.answer.await_args.args[0])
            await press(f"rdy:addmt:sound:{token}.0")
            saved = await press(f"rdy:addms:sound:{token}")
            self.assertIn("Алексей из Списков в составе",
                          saved.message.answer.await_args.args[0])
            self.assertEqual(
                [
                    member["name"]
                    for member in self.registry.list_service_members("sound")
                    if member["telegram_id"] == 12345
                ],
                ["Алексей из Списков"],
            )
            self.assertIn(12345, {
                person["telegram_id"]
                for person in self.registry.readiness_people("sound")
                if person["tasks"]
            })
            finished = await press(f"rdy:finish:sound:{month_arg}")
            self.assertIn("Опрос завершён",
                          finished.message.answer.await_args.args[0])
            self.context.bot.stop_poll.assert_awaited_once()
            confirm = await press(f"rdy:restart:sound:{month_arg}")
            self.assertIn("будут удалены",
                          confirm.message.answer.await_args.args[0])
            # Сбой отправки нового опроса — прежние ответы на месте.
            self.context.bot.send_poll.side_effect = RuntimeError("network")
            failed = await press(f"rdy:restartok:sound:{month_arg}")
            self.assertIn("прежние ответы сохранены",
                          failed.message.answer.await_args.args[0])
            kept = self.registry.get_readiness_run("sound", f"{year:04d}-{month:02d}")
            self.assertEqual(kept["poll_id"], "ready-poll")
            self.assertEqual(len(self.registry.list_readiness_answers(kept["id"])), 1)
            self.context.bot.send_poll.side_effect = None
            self.context.bot.send_poll.return_value = SimpleNamespace(
                message_id=13, poll=SimpleNamespace(id="ready-poll-2"),
            )
            restarted = await press(f"rdy:restartok:sound:{month_arg}")
            self.assertIn("Новый опрос отправлен",
                          restarted.message.answer.await_args.args[0])
            run = self.registry.list_open_readiness_runs()[0]
            self.assertEqual(run["poll_id"], "ready-poll-2")
            self.assertEqual(self.registry.list_readiness_answers(run["id"]), [])
            # Ответ Telegram потерян при перезапуске — месяц за новой
            # записью, админ видит, что проверить.
            from aiogram.exceptions import TelegramNetworkError

            self.context.bot.send_poll.side_effect = TelegramNetworkError(
                method=SimpleNamespace(), message="timeout"
            )
            lost = await press(f"rdy:restartok:sound:{month_arg}")
            self.assertIn("проверьте", lost.message.answer.await_args.args[0])
            self.assertIsNone(self.registry.get_readiness_run_by_id(run["id"]))
            self.assertEqual(
                self.registry.get_readiness_run(
                    "sound", f"{year:04d}-{month:02d}"
                )["status"],
                "closed",
            )

        self.context.bot.stop_poll = AsyncMock()
        asyncio.run(run())

    def test_auto_button_builds_current_month_without_poll_or_excel(self):
        from bot.excel_parser import RosterMember

        self.registry.sync_service_rosters({"sound": [
            RosterMember("Алексей", ("звук",)),
        ]})
        self.context.load_upcoming_service_events = AsyncMock(side_effect=AssertionError("Excel"))
        self.context.bot.send_poll = AsyncMock()
        month = datetime.now(ZoneInfo("UTC")).strftime("%Y%m")

        async def run():
            async def press(data):
                cb = SimpleNamespace(
                    data=data, from_user=SimpleNamespace(id=67890),
                    message=self.message(67890, ""), answer=AsyncMock(),
                )
                await self.callback_handler("on_readiness")(cb)
                return cb

            screen = await press("rdy:month:sound")
            rows = screen.message.answer.await_args.kwargs["reply_markup"].inline_keyboard
            row = next(row for row in rows if any(b.text == "🧮 Авто" for b in row))
            self.assertEqual([b.text for b in row], ["🗳 Опрос", "🧮 Авто"])
            auto = next(b.callback_data for b in row if b.text == "🧮 Авто")
            self.assertEqual(auto, f"rdy:pfree:sound:{month}")
            built = await press(auto)
            self.assertIn("Без опроса", built.message.answer.await_args.args[0])
            def back_button(callback):
                return next(
                    button for row in callback.message.answer.await_args.kwargs[
                        "reply_markup"
                    ].inline_keyboard for button in row
                    if button.text.startswith("⬅️ К ")
                )

            back = back_button(built)
            self.assertEqual((back.text, back.callback_data), ("⬅️ К графику", "rdy:month:sound"))
            returned = await press(back.callback_data)
            self.assertIn("ЗВУК › График", returned.message.answer.await_args.args[0])
            self.assertNotIn("График › Опрос", returned.message.answer.await_args.args[0])
            key = month[:4] + "-" + month[4:]
            plan = self.registry.get_schedule_plan("sound", key)
            self.assertIsNone(plan["run_id"])
            self.assertIsNone(plan["approved"])
            self.assertIsNone(self.registry.get_readiness_run("sound", key))
            self.assertTrue(any(values.get("звук") for values in plan["slots"].values()))
            reopened = await press(auto)
            self.assertEqual(back_button(reopened).callback_data, "rdy:month:sound")
            dates = await press(f"rdy:pedit:sound:{month}")
            self.assertEqual(back_button(dates).callback_data, f"rdy:plan:sound:{month}")
            edited_back = await press(back_button(dates).callback_data)
            self.assertEqual(back_button(edited_back).callback_data, "rdy:month:sound")
            self.assertEqual(self.registry.get_schedule_plan("sound", key)["slots"], plan["slots"])
            self.context.load_upcoming_service_events.assert_not_awaited()
            self.context.bot.send_poll.assert_not_awaited()

        asyncio.run(run())

    def test_manual_duty_outside_team_is_available_and_saved_in_draft(self):
        from bot.excel_parser import RosterMember

        self.registry.set_service_task_enabled(67890, "kitchen", "пироги", False)
        self.registry.add_service_task(67890, "kitchen", "Скатерти")
        self.registry.set_task_dependency(
            67890, "kitchen", "Скатерти", ["старший", "в смене"],
        )
        self.registry.sync_service_rosters({"kitchen": [
            RosterMember("А", ("старший",)), RosterMember("Б", ("в смене",)),
            RosterMember("В", ("Скатерти",)),
        ]})
        month = datetime.now(ZoneInfo("UTC")).strftime("%Y%m")
        key = month[:4] + "-" + month[4:]

        async def run():
            async def press(data):
                cb = SimpleNamespace(
                    data=data, from_user=SimpleNamespace(id=67890),
                    message=self.message(67890, ""), answer=AsyncMock(),
                )
                await self.callback_handler("on_readiness")(cb)
                return cb

            def button(cb, prefix):
                return next(data for data in self.callback_data_list(cb.message)
                            if data.startswith(prefix))

            await press(f"rdy:pfree:kitchen:{month}")
            plan = self.registry.get_schedule_plan("kitchen", key)
            self.assertTrue(all(not values.get("Скатерти")
                                for values in plan["slots"].values()))
            dates = await press(f"rdy:pedit:kitchen:{month}")
            day = await press(button(dates, "rdy:pdate:"))
            task_button = next(
                b.callback_data
                for row in day.message.answer.await_args.kwargs["reply_markup"].inline_keyboard
                for b in row if "Скатерти" in b.text
            )
            slot = await press(task_button)
            added = await press(button(slot, "rdy:padd:"))
            text = "".join(call.args[0] for call in added.message.answer.await_args_list)
            self.assertIn("Поставлен(а): В", text)
            self.assertIn("не входит в смену", text)
            plan = self.registry.get_schedule_plan("kitchen", key)
            self.assertEqual(plan["slots"][plan["dates"][0]]["Скатерти"], ["В"])
            self.assertIsNone(plan["approved"])
            shown = await press(f"rdy:plan:kitchen:{month}")
            ask = await press(button(shown, "rdy:pok:"))
            warning = await press(button(ask, "rdy:pokok:"))
            self.assertIn("не входит в смену", "".join(
                call.args[0] for call in warning.message.answer.await_args_list
            ))
            self.assertIsNone(self.registry.get_schedule_plan("kitchen", key)["approved"])

        asyncio.run(run())

    def test_incomplete_plan_extra_confirmation_and_cancel(self):
        from bot import schedule_plan
        from bot.excel_parser import RosterMember
        from unittest.mock import patch

        self.registry.set_service_task_enabled(67890, "kitchen", "пироги", False)
        self.registry.add_service_task(67890, "kitchen", "Скатерти")
        self.registry.set_task_dependency(
            67890, "kitchen", "Скатерти", ["старший", "в смене"],
        )
        self.registry.sync_service_rosters({"kitchen": [
            RosterMember("А", ("старший",)), RosterMember("Б", ("в смене",)),
        ]})
        month = datetime.now(ZoneInfo("UTC")).strftime("%Y%m")

        async def run():
            async def press(data):
                cb = SimpleNamespace(
                    data=data, from_user=SimpleNamespace(id=67890),
                    message=self.message(67890, ""), answer=AsyncMock(),
                )
                await self.callback_handler("on_readiness")(cb)
                return cb

            def button(cb, prefix):
                return next(data for data in self.callback_data_list(cb.message)
                            if data.startswith(prefix))

            built = await press(f"rdy:pfree:kitchen:{month}")
            ask = await press(button(built, "rdy:pok:"))
            ordinary = button(ask, "rdy:pokok:")
            with patch.object(schedule_plan, "publish", new=AsyncMock()) as publish:
                warning = await press(ordinary)
                self.assertIn("не назначен", "".join(
                    call.args[0] for call in warning.message.answer.await_args_list
                ))
                confirm = button(warning, "rdy:pokok:")
                self.assertIn("menu:start", self.callback_data_list(warning.message))
                publish.assert_not_awaited()
                await press(button(warning, "rdy:plan:"))
                await press(confirm)
                publish.assert_not_awaited()
                warning = await press(ordinary)
                current = self.registry.get_schedule_plan(
                    "kitchen", month[:4] + "-" + month[4:],
                )
                changed_warnings = self.registry.task_assignment_warnings(
                    "kitchen", current["slots"], complete=True, month=current["month"],
                ) + ["Дополнительное предупреждение"]
                with patch.object(self.registry, "task_assignment_warnings",
                                  return_value=changed_warnings):
                    await press(button(warning, "rdy:pokok:"))
                publish.assert_not_awaited()
                warning = await press(ordinary)
                publish.return_value = {**current, "approval_warnings": ["saved"]}
                self.context.bot.send_document = AsyncMock()
                await press(button(warning, "rdy:pokok:"))
                publish.assert_awaited_once()
                self.assertTrue(publish.await_args.kwargs["acknowledged_warnings"])

        asyncio.run(run())

    def test_excel_warning_confirmation_and_validation_error_keep_navigation(self):
        from bot.excel_parser import RosterMember

        self.registry.set_service_task_enabled(67890, "kitchen", "пироги", False)
        self.registry.add_service_task(67890, "kitchen", "Скатерти")
        self.registry.set_task_dependency(
            67890, "kitchen", "Скатерти", ["старший", "в смене"],
        )
        self.registry.sync_service_rosters({"kitchen": [
            RosterMember("А", ("старший", "Скатерти")),
            RosterMember("Б", ("в смене",)),
        ]})
        self.context.load_upcoming_service_events = AsyncMock(return_value=[
            ServiceEvent("kitchen", date(2026, 11, 1), (
                ServiceAssignment("старший", "А"), ServiceAssignment("в смене", "Б"),
            )),
        ])

        async def run():
            async def press(data):
                cb = SimpleNamespace(
                    data=data, from_user=SimpleNamespace(id=67890),
                    message=self.message(67890, ""), answer=AsyncMock(),
                )
                await self.callback_handler("on_excel_schedule")(cb)
                return cb

            def button(cb, prefix):
                return next(data for data in self.callback_data_list(cb.message)
                            if data.startswith(prefix))

            loaded = await press("xls:load:kitchen:202611")
            warning = await press(button(loaded, "xls:ok:"))
            self.assertIsNone(self.registry.get_schedule_plan("kitchen", "2026-11"))
            confirm = button(warning, "xls:warnok:")
            self.assertIn("menu:start", self.callback_data_list(warning.message))
            await press(button(warning, "xls:no:"))
            await press(confirm)
            self.assertIsNone(self.registry.get_schedule_plan("kitchen", "2026-11"))
            loaded = await press("xls:load:kitchen:202611")
            warning = await press(button(loaded, "xls:ok:"))
            confirm = button(warning, "xls:warnok:")
            # A hard validation error cannot be bypassed by confirming warnings.
            with patch.object(self.registry, "import_excel_plan",
                              side_effect=ValueError("test validation")):
                error = await press(confirm)
            self.assertTrue(any(data.startswith("service:") for data in
                                self.callback_data_list(error.message)))
            self.assertIn("test validation", "".join(
                call.args[0] for call in error.message.answer.await_args_list
            ))
            with patch.object(self.registry, "import_excel_plan",
                              side_effect=sqlite3.OperationalError("test database")):
                error = await press(confirm)
            self.assertIn("menu:start", self.callback_data_list(error.message))
            self.assertIn("service:service_excel:kitchen",
                          self.callback_data_list(error.message))
            await press(confirm)
            saved = self.registry.get_schedule_plan("kitchen", "2026-11")
            self.assertIsNotNone(saved["approved"])
            self.assertTrue(saved["approval_warnings"])

        asyncio.run(run())

    def test_error_screen_preserves_back_and_home(self):
        from bot.navigation import back_callback

        async def run():
            screen = SimpleNamespace(
                data="service:service_setup:sound",
                from_user=SimpleNamespace(id=67890),
                message=self.message(67890, ""), answer=AsyncMock(),
            )
            await self.callback_handler("on_service_selected")(screen)
            self.registry.add_service_admin(67890, "sound", 24680)
            denied = SimpleNamespace(
                data="menu:service_admins",
                from_user=SimpleNamespace(id=24680),
                message=self.message(24680, ""), answer=AsyncMock(),
            )
            # An error with no earlier screen still has both exits.
            await self.callback_handler("on_start_menu")(denied)
            rows = denied.message.answer.await_args.kwargs["reply_markup"].inline_keyboard
            self.assertEqual([b.text for row in rows for b in row],
                             ["⬅️ Назад", "🏠 В начало"])
            screen.from_user.id = 24680
            screen.message = self.message(24680, "")
            await self.callback_handler("on_service_selected")(screen)
            previous_back = back_callback(
                screen.message.answer.await_args.kwargs["reply_markup"],
            )
            self.assertIsNotNone(previous_back)
            denied.message = self.message(24680, "")
            await self.callback_handler("on_start_menu")(denied)
            rows = denied.message.answer.await_args.kwargs["reply_markup"].inline_keyboard
            self.assertEqual(
                [b.callback_data for row in rows for b in row],
                [previous_back, "menu:start"],
            )

        asyncio.run(run())

    def test_schedule_without_poll_and_telegram_user(self):
        from bot.excel_parser import RosterMember

        self.registry.set_group_chat(67890, "sound", -1005555, enabled=True)
        self.registry.sync_service_rosters({"sound": [
            RosterMember("Алексей", ("звук",)),
            RosterMember("Борис", ("звук",)),
        ]})
        self.context.bot.send_message.return_value = SimpleNamespace(
            message_id=21
        )
        self.context.bot.send_document = AsyncMock()
        today = datetime.now(ZoneInfo("UTC")).date()
        year, month = (
            (today.year + 1, 1) if today.month == 12
            else (today.year, today.month + 1)
        )
        key, month_arg = f"{year:04d}-{month:02d}", f"{year:04d}{month:02d}"
        handler = self.callback_handler("on_readiness")

        async def press(data):
            callback = SimpleNamespace(
                data=data,
                from_user=SimpleNamespace(id=67890),
                message=self.message(67890, ""),
                answer=AsyncMock(),
            )
            await handler(callback)
            return callback

        def text_of(callback):
            return callback.message.answer.await_args.args[0]

        def data_of(callback, prefix):
            return next(
                button.callback_data
                for row in callback.message.answer.await_args.kwargs[
                    "reply_markup"
                ].inline_keyboard
                for button in row
                if button.callback_data.startswith(prefix)
            )

        async def run():
            screen = await press("rdy:open:sound")
            self.assertIn("ЗВУК › График › Опрос", text_of(screen))
            skipped = await press(f"rdy:skip:sound:{month_arg}")
            self.assertIn("Составить без опроса", text_of(skipped))
            built = await press(data_of(skipped, "rdy:pfree:"))
            self.assertIn("🧮 Без опроса", text_of(built))
            plan = self.registry.get_schedule_plan("sound", key)
            self.assertIsNone(plan["run_id"])
            first_day = plan["dates"][0]
            self.assertEqual(len(plan["slots"][first_day]["звук"]), 1)
            again = await press("rdy:open:sound")
            self.assertIn("(без опроса)", text_of(again))

            dates = await press(f"rdy:pedit:sound:{month_arg}")
            day = await press(data_of(dates, "rdy:pdate:"))
            slot = await press(data_of(day, "rdy:ptask:"))
            ask_user = await press(data_of(slot, "rdy:pnew:"))
            markup = ask_user.message.answer.await_args.kwargs["reply_markup"]
            request = markup.keyboard[0][0].request_users
            shared = self.message(67890, "")
            shared.text = None
            shared.users_shared = SimpleNamespace(
                request_id=request.request_id,
                users=[SimpleNamespace(
                    user_id=777, first_name="Сергей", last_name="Новый",
                    username="sergey_new",
                )],
            )
            # Однофамилец в составе без Telegram — новому имя с @username.
            self.registry.sync_service_rosters({"sound": [
                RosterMember("Алексей", ("звук",)),
                RosterMember("Борис", ("звук",)),
                RosterMember("Сергей Новый", ("сборка",)),
            ]})
            await self.message_handler("on_users_shared")(shared)
            self.assertIn(
                "Поставлен(а): Сергей Новый (@sergey_new)",
                shared.answer.await_args_list[-1].args[0],
            )
            plan = self.registry.get_schedule_plan("sound", key)
            self.assertEqual(
                plan["slots"][first_day]["звук"], ["Сергей Новый (@sergey_new)"]
            )
            namesake = next(
                item for item in self.registry.list_service_members("sound")
                if item["name"] == "Сергей Новый"
            )
            self.assertIsNone(namesake["telegram_id"])
            sergey = next(
                person for person in self.registry.readiness_people("sound")
                if person["telegram_id"] == 777
            )
            self.assertEqual(sergey["tasks"], ["звук"])

            plan_screen = await press(f"rdy:plan:sound:{month_arg}")
            ask = await press(data_of(plan_screen, "rdy:pok:"))
            done = await press(data_of(ask, "rdy:pokok:"))
            self.assertIn("✅ График утверждён", text_of(done))

        asyncio.run(run())

    def test_own_day_from_schedule_section(self):
        from bot.excel_parser import RosterMember

        self.registry.sync_service_rosters({"sound": [
            RosterMember("Катя Степанова", ("звук",)),
        ]})
        handler = self.callback_handler("on_readiness")

        async def press(data):
            callback = SimpleNamespace(
                data=data,
                from_user=SimpleNamespace(id=67890),
                message=self.message(67890, ""),
                answer=AsyncMock(),
            )
            await handler(callback)
            return callback

        def find(callback, text):
            return next(
                button.callback_data
                for row in callback.message.answer.await_args.kwargs[
                    "reply_markup"
                ].inline_keyboard
                for button in row
                if button.text.startswith(text)
            )

        async def run():
            section = await press("rdy:open:sound")
            own = await press(find(section, "📅 Свой день"))
            self.assertIn("Дни служения для всех", own.message.answer.await_args.args[0])
            person = await press(find(own, "📅 Катя Степанова"))
            chosen = await press(find(person, "📅 чт (вс -3)"))
            self.assertIn(
                "✅ Катя Степанова: чт (вс -3)",
                chosen.message.answer.await_args.args[0],
            )
            katya = next(
                item for item in self.registry.list_service_members("sound")
                if item["name"] == "Катя Степанова"
            )
            self.assertEqual(katya["day_offset"], -3)
            stale = await press(find(person, "📅 пт (вс -2)"))
            self.assertIn("устарел", stale.answer.await_args.args[0])

        asyncio.run(run())

    def test_section_shows_missed_autostart(self):
        handler = self.callback_handler("on_readiness")
        year, month = 2026, 12
        from bot import readiness as readiness_module

        start, deadline = readiness_module.schedule(
            self.registry.get_readiness_settings("sound"), year, month,
            ZoneInfo("UTC"),
        )

        async def open_at(moment):
            callback = SimpleNamespace(
                data="rdy:open:sound",
                from_user=SimpleNamespace(id=67890),
                message=self.message(67890, ""),
                answer=AsyncMock(),
            )
            with patch("bot.handlers.datetime") as clock:
                clock.now.return_value = moment
                clock.combine = datetime.combine
                clock.fromisoformat = datetime.fromisoformat
                await handler(callback)
            return "\n".join(
                call.args[0]
                for call in callback.message.answer.await_args_list
            )

        async def run():
            unbound = await open_at(deadline + timedelta(hours=1))
            self.assertIn("❗ Служение выключено", unbound)
            self.registry.set_group_chat(67890, "sound", -1005555, enabled=True)
            before = await open_at(start - timedelta(days=2))
            self.assertIn("▶️ Запуск:", before)
            self.assertNotIn("сроки прошли", before)
            after = await open_at(deadline + timedelta(hours=1))
            self.assertIn("Автозапуск выключен", after)
            self.assertIn("❗ Сейчас нужно запустить опрос вручную", after)
            enabled_at = deadline + timedelta(hours=1)
            with patch(
                "bot.service_registry._utc_now",
                return_value=enabled_at.isoformat(),
            ):
                self.registry.set_readiness_settings(
                    67890, "sound", enabled=True
                )
            # Включён только что (после окна) — не «ожидался».
            late = await open_at(enabled_at)
            self.assertIn("Автозапуск включён после времени запуска", late)
            with closing(sqlite3.connect(self.registry.path)) as connection:
                connection.execute(
                    "UPDATE readiness_settings SET enabled_at = ?",
                    ((start - timedelta(days=5)).isoformat(),),
                )
                connection.commit()
            expected = await open_at(deadline + timedelta(hours=1))
            self.assertIn("сроки прошли, опрос не запускался", expected)
            self.registry.set_group_chat(67890, "sound", -1005555, enabled=False)
            disabled = await open_at(deadline + timedelta(hours=1))
            self.assertIn("❗ Служение выключено", disabled)
            self.registry.set_group_chat(67890, "sound", -1005555, enabled=True)
            # Попытка автозапуска была и не удалась.
            self.registry.claim_readiness_notice(
                "sound", f"{year:04d}-{month:02d}", "launch"
            )
            failed = await open_at(start + timedelta(hours=1))
            self.assertIn("не удался", failed)
            # График составлен без опроса — подсказки «запустить» нет.
            self.registry.save_schedule_draft(
                "sound", f"{year:04d}-{month:02d}", run_id=None, dates=[],
                tasks=[], slots={}, last_served={},
            )
            planned = await open_at(deadline + timedelta(hours=1))
            self.assertNotIn("❗", planned)
            self.assertNotIn("сроки прошли", planned)

        asyncio.run(run())

    def test_command_screen_replaces_previous_menu(self):
        handler = self.message_handler("on_services")

        async def run():
            first = self.message(67890, "/services")
            first.answer = AsyncMock(return_value=SimpleNamespace(message_id=677))
            second = self.message(67890, "/services")
            second.answer = AsyncMock(return_value=SimpleNamespace(message_id=678))
            first.message_id, second.message_id = 10, 11
            await handler(first)
            await handler(second)
            self.context.bot.delete_message.assert_any_await(
                chat_id=67890, message_id=677
            )
            # Запоздавшая более ранняя команда свой экран не показывает и
            # экран новой не трогает.
            late = self.message(67890, "/services")
            late.message_id = 9
            late.answer = AsyncMock(return_value=SimpleNamespace(message_id=679))
            await handler(late)
            late.answer.assert_not_awaited()
            self.assertNotIn(
                678,
                [call.kwargs.get("message_id")
                 for call in self.context.bot.delete_message.await_args_list],
            )

        asyncio.run(run())

    def dialog_sender(self):
        return inspect.getclosurevars(self.message_handler("on_services")).nonlocals[
            "_send_dialog"
        ]

    def test_reminder_dialog_disables_link_preview(self):
        async def run():
            message = self.message(67890, "")
            await self.dialog_sender()(
                message, "Напоминание\nhttps://example.com/reminder",
            )
            sent = message.answer.await_args
            self.assertTrue(sent.kwargs["link_preview_options"].is_disabled)
            self.assertIn("https://example.com/reminder", sent.args[0])

        asyncio.run(run())

    def test_multipart_screen_is_cleaned_by_commands_inline_and_home(self):
        async def run():
            sender = self.dialog_sender()
            next_id = 700

            async def answer(*args, **kwargs):
                nonlocal next_id
                next_id += 1
                return SimpleNamespace(message_id=next_id)

            message = self.message(67890, "")
            message.answer.side_effect = answer
            markup = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(
                    text="⬅️ К служению",
                    callback_data="service:service_settings:sound",
                ),
            ]])
            # An unrelated notification is never registered as part of a dialog.
            self.context.bot.send_message.return_value = SimpleNamespace(message_id=450)
            await self.context.bot.send_message(67890, "Независимое уведомление")
            self.context.bot.send_message.return_value = SimpleNamespace(message_id=451)
            for transition in ("command", "inline", "home"):
                ids = []
                await sender(
                    message, "Длинный экран\n" + "строка\n" * 180,
                    reply_markup=markup, _dialog_sent_ids=ids,
                )
                self.assertGreater(len(ids), 1)
                self.context.bot.delete_message.reset_mock()
                if transition == "command":
                    command = self.message(67890, "/services")
                    command.message_id = 100
                    command.answer.side_effect = answer
                    await self.message_handler("on_services")(command)
                elif transition == "inline":
                    callback = SimpleNamespace(
                        data="service:service_settings:sound",
                        from_user=SimpleNamespace(id=67890),
                        message=SimpleNamespace(
                            **{**vars(message), "message_id": ids[-1]},
                        ),
                        answer=AsyncMock(),
                    )
                    await self.callback_handler("on_service_selected")(callback)
                else:
                    home = self.message(67890, NAV_HOME_TEXT)
                    home.delete = AsyncMock()
                    home.answer.side_effect = answer
                    await self.message_handler("on_nav_button")(home)
                deleted = {
                    call.kwargs.get("message_id")
                    for call in self.context.bot.delete_message.await_args_list
                }
                self.assertTrue(set(ids) <= deleted, (transition, ids, deleted))
                self.assertNotIn(450, deleted)
            self.assertEqual(
                sum(call.args == (67890, "Независимое уведомление")
                    for call in self.context.bot.send_message.await_args_list),
                1,
            )

        asyncio.run(run())

    def test_partial_screen_failure_removes_sent_parts_and_propagates_error(self):
        async def run():
            message = self.message(67890, "")
            message.answer.side_effect = [
                SimpleNamespace(message_id=811), RuntimeError("send failed"),
            ]
            with self.assertRaisesRegex(RuntimeError, "send failed"):
                await self.dialog_sender()(message, "строка\n" * 180)
            self.context.bot.delete_message.assert_any_await(
                chat_id=67890, message_id=811,
            )

        asyncio.run(run())

    def test_text_only_multipart_result_is_removed_by_next_dialog(self):
        async def run():
            sender = self.dialog_sender()
            message = self.message(67890, "")
            message.answer.side_effect = [
                SimpleNamespace(message_id=index) for index in range(1100, 1110)
            ]
            ids = []
            await sender(message, "строка\n" * 180, _dialog_sent_ids=ids)
            await sender(message, "Следующий экран")
            deleted = {
                call.kwargs.get("message_id")
                for call in self.context.bot.delete_message.await_args_list
            }
            self.assertTrue(set(ids) <= deleted)
            self.assertGreater(len(ids), 1)

        asyncio.run(run())

    def test_paged_dialog_keeps_back_and_help_on_every_page(self):
        async def run():
            message = self.message(67890, "")
            buttons = [
                InlineKeyboardButton(text=f"Действие {index}", callback_data=f"action:{index}")
                for index in range(8)
            ] + [
                InlineKeyboardButton(text="❓ Справка", callback_data="help"),
                InlineKeyboardButton(text="⬅️ К служению", callback_data="back"),
            ]
            await self.dialog_sender()(
                message, "Выберите действие",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[b] for b in buttons]),
            )
            for _ in range(4):
                rows = message.answer.await_args.kwargs["reply_markup"].inline_keyboard
                self.assertEqual(
                    [button.callback_data for button in rows[-1]], ["back", "help"],
                )
                self.assertLessEqual(sum(map(len, rows)), 6)
                next_button = next(
                    (button for row in rows for button in row if "Далее" in button.text),
                    None,
                )
                if next_button is None:
                    break
                callback = SimpleNamespace(
                    data=next_button.callback_data,
                    from_user=SimpleNamespace(id=67890),
                    message=message, answer=AsyncMock(),
                )
                await self.callback_handler("on_dialog_page")(callback)

        asyncio.run(run())

    def test_concurrent_dialogs_do_not_interleave_parts_or_leave_old_screen(self):
        async def run():
            sender = self.dialog_sender()
            sequence = []
            next_id = 900

            async def answer(text, **kwargs):
                nonlocal next_id
                next_id += 1
                message_id = next_id
                sequence.append("A" if "AAAA" in text else "B")
                await asyncio.sleep(0)
                return SimpleNamespace(message_id=message_id)

            message = self.message(67890, "")
            message.answer.side_effect = answer
            markup = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🏠 В начало", callback_data="menu:start"),
            ]])
            first_ids, second_ids = [], []
            await asyncio.gather(
                sender(message, "AAAA\n" * 200, reply_markup=markup,
                       _dialog_sent_ids=first_ids),
                sender(message, "BBBB\n" * 200, reply_markup=markup,
                       _dialog_sent_ids=second_ids),
            )
            self.assertEqual(sequence, ["A"] * len(first_ids) + ["B"] * len(second_ids))
            deleted = {
                call.kwargs.get("message_id")
                for call in self.context.bot.delete_message.await_args_list
            }
            self.assertTrue(set(first_ids) <= deleted)
            self.assertTrue(set(second_ids).isdisjoint(deleted))

        asyncio.run(run())

    def test_readiness_plan_edit_and_approve(self):
        from bot.excel_parser import RosterMember

        self.registry.set_group_chat(67890, "sound", -1005555, enabled=True)
        self.registry.sync_service_rosters({"sound": [
            RosterMember("Алексей", ("звук",)),
            RosterMember("Борис", ("звук",)),
        ]})
        self.registry.set_responsible_contact(
            67890, "sound", "звук", "Алексей", 501
        )
        self.registry.set_responsible_contact(
            67890, "sound", "звук", "Борис", 502
        )
        self.context.bot.send_message.return_value = SimpleNamespace(
            message_id=21
        )
        self.context.bot.send_document = AsyncMock()
        today = datetime.now(ZoneInfo("UTC")).date()
        year, month = (
            (today.year + 1, 1) if today.month == 12
            else (today.year, today.month + 1)
        )
        key, month_arg = f"{year:04d}-{month:02d}", f"{year:04d}{month:02d}"
        from bot.readiness import month_sundays

        dates = [day.isoformat() for day in month_sundays(year, month)]
        run = self.registry.create_readiness_run(
            "sound", key, status="open", dates=dates, poll_id="plan-poll",
        )
        self.registry.record_readiness_answer(
            "plan-poll", 501, "", "Алексей", dates[:1]
        )
        self.registry.record_readiness_answer(
            "plan-poll", 502, "", "Борис", dates[1:2]
        )
        self.registry.close_readiness_run(run["id"])
        handler = self.callback_handler("on_readiness")

        async def press(data):
            callback = SimpleNamespace(
                data=data,
                from_user=SimpleNamespace(id=67890),
                message=self.message(67890, ""),
                answer=AsyncMock(),
            )
            await handler(callback)
            return callback

        def text_of(callback):
            return callback.message.answer.await_args.args[0]

        def buttons(callback):
            return [
                button
                for row in callback.message.answer.await_args.kwargs[
                    "reply_markup"
                ].inline_keyboard
                for button in row
            ]

        def data_of(callback, prefix):
            return next(
                button.callback_data for button in buttons(callback)
                if button.callback_data.startswith(prefix)
            )

        async def run_test():
            screen = await press("rdy:open:sound")
            self.assertIn(
                f"rdy:plan:sound:{month_arg}",
                [button.callback_data for button in buttons(screen)],
            )
            plan_screen = await press(f"rdy:plan:sound:{month_arg}")
            self.assertIn("📋 ЗВУК › График › Опрос", text_of(plan_screen))
            self.assertIn("📝 Черновик", text_of(plan_screen))
            self.assertTrue(any(
                button.text == "⬅️ К опросу" and button.callback_data == "rdy:open:sound"
                for button in buttons(plan_screen)
            ))
            plan = self.registry.get_schedule_plan("sound", key)
            self.assertEqual(plan["slots"][dates[0]]["звук"], ["Алексей"])

            dates_screen = await press(f"rdy:pedit:sound:{month_arg}")
            day = await press(data_of(dates_screen, "rdy:pdate:"))
            slot = await press(data_of(day, "rdy:ptask:"))
            self.assertIn("Сейчас: Алексей", text_of(slot))
            borys = next(
                button for button in buttons(slot) if button.text == "👥 Борис"
            )
            # «Нужно 1» — Борис ставится вместо Алексея.
            added = await press(borys.callback_data)
            self.assertIn("Поставлен(а): Борис вместо Алексей", text_of(added))
            self.assertIn("не отметил(а)", text_of(added))
            plan = self.registry.get_schedule_plan("sound", key)
            self.assertEqual(plan["slots"][dates[0]]["звук"], ["Борис"])
            self.assertEqual(plan["edited"], 1)
            stale = await press(borys.callback_data)
            self.assertIn("устарел", stale.answer.await_args.args[0])
            day_button = next(
                button for button in buttons(added)
                if button.text.startswith("📅 Борис —")
            )
            day_screen = await press(day_button.callback_data)
            friday = next(
                button for button in buttons(day_screen)
                if button.text == "📅 пт (вс -2)"
            )
            set_day = await press(friday.callback_data)
            self.assertIn("✅ День на эту дату: пт", text_of(set_day))
            self.assertEqual(
                self.registry.get_schedule_plan("sound", key)["days"],
                {dates[0]: {"Борис": -2}},
            )
            added = set_day
            remove = next(
                button for button in buttons(added)
                if button.text == "❌ Борис"
            )
            await press(remove.callback_data)
            plan = self.registry.get_schedule_plan("sound", key)
            self.assertEqual(plan["slots"][dates[0]]["звук"], [])
            # Убранному из даты разовый день больше не нужен.
            self.assertEqual(plan["days"], {})

            plan_screen = await press(f"rdy:plan:sound:{month_arg}")
            # Кнопка прежней версии графика не срабатывает.
            outdated = await press(data_of(slot, "rdy:pdate:"))
            self.assertIn("График изменился", text_of(outdated))
            confirm = await press(data_of(plan_screen, "rdy:precalc:"))
            self.assertIn("Ручные правки сбросятся", text_of(confirm))
            await press(data_of(confirm, "rdy:precalcok:"))
            plan = self.registry.get_schedule_plan("sound", key)
            self.assertEqual(plan["edited"], 0)
            self.assertEqual(plan["slots"][dates[0]]["звук"], ["Алексей"])

            plan_screen = await press(f"rdy:plan:sound:{month_arg}")
            ask = await press(data_of(plan_screen, "rdy:pok:"))
            self.assertIn("Утвердить график", text_of(ask))
            self.context.bot.send_message.reset_mock()
            done = await press(data_of(ask, "rdy:pokok:"))
            self.assertIn("✅ График утверждён", text_of(done))
            chats = [
                call.args[0]
                for call in self.context.bot.send_message.await_args_list
            ]
            self.assertIn(-1005555, chats)
            self.assertIn(67890, chats)
            self.context.bot.send_document.assert_awaited_once()
            plan = self.registry.get_schedule_plan("sound", key)
            self.assertFalse(plan["dirty"])
            again = await press(data_of(ask, "rdy:pokok:"))
            self.assertIn("Изменений нет", text_of(again))
            old = await press("rdy:plan:sound:200001")
            self.assertIn("Экран устарел", text_of(old))

        asyncio.run(run_test())

    def test_service_admin_opens_own_service_tasks_only(self):
        async def run():
            self.registry.add_service_admin(67890, "sound", 24680)
            callback = SimpleNamespace(
                data="svc_tasks:open:sound",
                from_user=SimpleNamespace(id=24680),
                message=self.message(24680, ""),
                answer=AsyncMock(),
            )
            await self.callback_handler("on_service_tasks")(callback)
            self.assertIn(
                "задачи", callback.message.answer.await_args.args[0]
            )
            other = SimpleNamespace(
                data="svc_tasks:open:kitchen",
                from_user=SimpleNamespace(id=24680),
                message=self.message(24680, ""),
                answer=AsyncMock(),
            )
            await self.callback_handler("on_service_tasks")(other)
            self.assertIn("Недостаточно прав", other.answer.await_args.args[0])
            other.message.answer.assert_not_awaited()

        asyncio.run(run())

    def test_service_card_for_service_admin_shows_own_sections(self):
        async def run():
            self.registry.add_service_admin(67890, "sound", 24680)
            callback = SimpleNamespace(
                data="service:service_settings:sound",
                from_user=SimpleNamespace(id=24680),
                message=self.message(24680, ""),
                answer=AsyncMock(),
            )
            await self.callback_handler("on_service_selected")(callback)

            # Всё по служению, кроме назначения и снятия админов.
            self.assertEqual(
                self.callback_data_list(callback.message),
                ["service:service_contact:sound",
                 "rdy:month:sound", "service:service_reminders:sound",
                 "svc_tasks:open:sound",
                 "service:service_setup:sound", "service:back_to_services:list",
                 "service:service_help:sound"],
            )
            # В «⚙️ Настройки» — только групповой чат, без админов.
            setup = SimpleNamespace(
                data="service:service_setup:sound",
                from_user=SimpleNamespace(id=24680),
                message=self.message(24680, ""),
                answer=AsyncMock(),
            )
            await self.callback_handler("on_service_selected")(setup)
            self.assertEqual(
                self.callback_data_list(setup.message),
                ["service:service_chat:sound", "service:service_settings:sound"],
            )
            self.assertIn(
                "👤 Администраторы · 1",
                callback.message.answer.await_args.args[0],
            )

        asyncio.run(run())

    def test_service_admin_manages_own_service_but_not_admins(self):
        async def run():
            self.registry.add_service_admin(67890, "sound", 24680)

            async def press(data):
                callback = SimpleNamespace(
                    data=data,
                    from_user=SimpleNamespace(id=24680),
                    message=self.message(24680, ""),
                    answer=AsyncMock(),
                )
                await self.callback_handler("on_service_selected")(callback)
                return callback

            chat = await press("service:service_chat:sound")
            self.assertIn(
                "выберите действие", chat.message.answer.await_args.args[0]
            )
            contact = await press("service:service_contact:sound")
            self.assertTrue(contact.message.answer.await_args_list)
            self.assertNotIn(
                "Недостаточно", str(contact.message.answer.await_args_list)
            )
            admins = await press("service:service_admin_add:sound")
            self.assertIn(
                "только супер-администратор",
                admins.message.answer.await_args.args[0],
            )

        asyncio.run(run())

    def test_service_card_reminder_shortcut_selects_general_or_task(self):
        async def run():
            self.registry.add_service_admin(67890, "sound", 12345)

            def callback(data):
                return SimpleNamespace(
                    data=data,
                    from_user=SimpleNamespace(id=12345),
                    message=self.message(12345, ""),
                    answer=AsyncMock(),
                )

            choices = callback("service:service_reminders:sound")
            await self.callback_handler("on_service_selected")(choices)
            self.assertIn(
                "🔔 Общие",
                [button.text for row in choices.message.answer.await_args.kwargs[
                    "reply_markup"
                ].inline_keyboard for button in row],
            )
            general = await self._click(
                choices.message, lambda button: button.text == "🔔 Общие"
            )
            self.assertIn(
                "Всё служение",
                general.message.answer.await_args.args[0],
            )

            choices = callback("service:service_reminders:sound")
            await self.callback_handler("on_service_selected")(choices)
            disabled = await self._click(
                choices.message,
                lambda button: button.text.startswith("🔴"),
            )
            self.assertIn(
                "Сначала включите задачу",
                disabled.answer.await_args.args[0],
            )
            disabled.message.answer.assert_not_awaited()

            choices = callback("service:service_reminders:sound")
            await self.callback_handler("on_service_selected")(choices)
            task = await self._click(
                choices.message,
                lambda button: button.text.endswith("🎚️ звук"),
            )
            self.assertIn(
                "звук",
                task.message.answer.await_args.args[0],
            )

        asyncio.run(run())

    def test_service_reminder_summary_includes_disabled_recipients_and_no_approval(self):
        async def run():
            self.registry.add_service_admin(67890, "sound", 24680)
            self.registry.set_group_chat(
                67890, "sound", -10055, enabled=True
            )
            self.registry.set_responsible_contact(
                67890, "sound", "Микшер", "Алексей", 24680
            )
            self.registry.set_user_notifications_enabled(
                24680, "sound", True
            )
            text_id = self.registry.ensure_user_text(
                24680, "sound", "Микшер"
            )
            self.registry.add_schedule(text_id, 2, "18:30")
            self.registry.set_responsible_contact(
                67890, "sound", "Свет", "Мария", 35791
            )
            inactive_text_id = self.registry.ensure_user_text(
                35791, "sound", "Свет"
            )
            self.registry.add_schedule(inactive_text_id, 1, "09:00")

            choices = SimpleNamespace(
                data="service:service_reminders:sound",
                from_user=SimpleNamespace(id=24680),
                message=self.message(24680, ""),
                answer=AsyncMock(),
            )
            await self.callback_handler("on_service_selected")(choices)
            summary_button = next(
                button
                for row in choices.message.answer.await_args.kwargs[
                    "reply_markup"
                ].inline_keyboard
                for button in row
                if button.text == "📊 Сводка"
            )
            summary = SimpleNamespace(
                data=summary_button.callback_data,
                from_user=SimpleNamespace(id=24680),
                message=self.message(24680, ""),
                answer=AsyncMock(),
            )
            await self.callback_handler("on_service_selected")(summary)

            text = summary.message.answer.await_args.args[0]
            self.assertIn("Алексей", text)
            self.assertIn("Микшер", text)
            self.assertIn("за 2 дня в 18:30", text)
            self.assertIn("не утверждён", text)
            self.assertIn("Мария", text)
            self.assertIn("часовой пояс: UTC", text)
            self.assertIn("Прогноз на", text)
            self.assertNotIn("24680", text)

        asyncio.run(run())

    def test_service_reminder_shortcut_uses_task_picker_for_large_list(self):
        async def run():
            self.registry.add_service_admin(67890, "sound", 12345)
            for name in ("Перевозка", "Сцена", "Свет", "Запись", "Фото", "Видео"):
                self.registry.add_service_task(67890, "sound", name)
            choices = SimpleNamespace(
                data="service:service_reminders:sound",
                from_user=SimpleNamespace(id=12345),
                message=self.message(12345, ""),
                answer=AsyncMock(),
            )
            await self.callback_handler("on_service_selected")(choices)
            self.assertIn(
                "📋 Выбрать задачу",
                [button.text for row in choices.message.answer.await_args.kwargs[
                    "reply_markup"
                ].inline_keyboard for button in row],
            )
            picker = await self._click(
                choices.message,
                lambda button: button.text == "📋 Выбрать задачу",
            )
            picker_text = picker.message.answer.await_args.args[0]
            self.assertIn("выберите задачу", picker_text)
            self.assertIn("Страница 1/", picker_text)
            task = await self._click(
                picker.message, lambda button: button.text.endswith("🎚️ звук")
            )
            self.assertIn("звук", task.message.answer.await_args.args[0])

        asyncio.run(run())

    def test_summary_month_refresh_paging_stale_buttons_and_revoked_access(self):
        async def run():
            self.registry.add_service_admin(67890, "sound", 24680)
            for index in range(70):
                person = 30000 + index
                self.registry.set_responsible_contact(
                    67890, "sound", "Микшер", f"Участник {index:02}", person,
                )
                text_id = self.registry.ensure_user_text(person, "sound", "Микшер")
                self.registry.add_schedule(text_id, 1, "18:00")
            opening = SimpleNamespace(
                data="service:service_reminder_summary:sound",
                from_user=SimpleNamespace(id=24680),
                message=self.message(24680, ""), answer=AsyncMock(),
            )
            await self.callback_handler("on_service_selected")(opening)
            shown_names = set()
            page_message = opening.message
            while True:
                page_text = page_message.answer.await_args.args[0]
                self.assertLessEqual(visible_units(page_text), 4000)
                shown_names.update(
                    f"Участник {index:02}" for index in range(70)
                    if f"Участник {index:02}" in page_text
                )
                page_markup = page_message.answer.await_args.kwargs["reply_markup"]
                if not any(
                    item.text == "➡️ Далее"
                    for row in page_markup.inline_keyboard for item in row
                ):
                    break
                moved = await self._click(
                    page_message, lambda item: item.text == "➡️ Далее",
                    actor_id=24680,
                )
                page_message = moved.message
            self.assertEqual(len(shown_names), 70)
            # Start a fresh screen before exercising token invalidation.
            opening = SimpleNamespace(
                data="service:service_reminder_summary:sound",
                from_user=SimpleNamespace(id=24680),
                message=self.message(24680, ""), answer=AsyncMock(),
            )
            await self.callback_handler("on_service_selected")(opening)
            original = opening.message.answer.await_args.kwargs["reply_markup"]
            original_next = next(
                item for row in original.inline_keyboard for item in row
                if item.text == "➡️ Далее"
            )
            second = await self._click(
                opening.message, lambda item: item.text == "➡️ Далее", actor_id=24680,
            )
            text = second.message.answer.await_args.args[0]
            self.assertIn("Страница 2/", text)
            stale = SimpleNamespace(
                data=original_next.callback_data,
                from_user=SimpleNamespace(id=24680),
                message=self.message(24680, ""), answer=AsyncMock(),
            )
            await self.callback_handler("on_personal_summary_page")(stale)
            self.assertIn("устарел", stale.answer.await_args.args[0])
            stale.message.answer.assert_not_awaited()
            following = await self._click(
                second.message, lambda item: item.text == "📅 Следующий месяц",
                actor_id=24680,
            )
            now = datetime.now(self.context.timezone)
            expected = date(*next_month(now.year, now.month), 1)
            self.assertIn(
                month_title(expected.year, expected.month),
                following.message.answer.await_args.args[0],
            )
            refreshed = await self._click(
                following.message, lambda item: item.text == "🔄 Обновить",
                actor_id=24680,
            )
            markup = refreshed.message.answer.await_args.kwargs["reply_markup"]
            self.assertLessEqual(sum(map(len, markup.inline_keyboard)), 6)
            self.registry.remove_service_admin(67890, "sound", 24680)
            denied = await self._click(
                refreshed.message, lambda item: item.text == "🔄 Обновить",
                actor_id=24680,
            )
            denied.message.answer.assert_not_awaited()
            self.assertIn("Недостаточно прав", denied.answer.await_args.args[0])
            self.context.bot.send_message.assert_not_awaited()
            self.context.load_service_task_assignments.assert_not_awaited()

        asyncio.run(run())

    def test_summary_database_failure_is_an_error_with_retry(self):
        async def run():
            self.registry.add_service_admin(67890, "sound", 24680)
            opening = SimpleNamespace(
                data="service:service_reminder_summary:sound",
                from_user=SimpleNamespace(id=24680),
                message=self.message(24680, ""), answer=AsyncMock(),
            )
            with patch.object(
                self.registry, "get_schedule_plan",
                side_effect=sqlite3.OperationalError("database unavailable"),
            ):
                await self.callback_handler("on_service_selected")(opening)
            text = opening.message.answer.await_args.args[0]
            self.assertIn("Прогноз недоступен", text)
            self.assertNotIn("Нет ответственных", text)
            self.context.debug_logger.exception.assert_called()
            retry = await self._click(
                opening.message, lambda item: item.text == "🔄 Обновить",
                actor_id=24680,
            )
            self.assertNotIn("Прогноз недоступен", retry.message.answer.await_args.args[0])

        asyncio.run(run())

    def test_service_reminder_shortcut_opens_general_list_without_tasks(self):
        async def run():
            self.registry.add_service_admin(67890, "books", 12345)
            self.registry.add_group_reminder(
                67890, "books", 1, "10:00", "Напоминание о книгах"
            )
            callback = SimpleNamespace(
                data="service:service_reminders:books",
                from_user=SimpleNamespace(id=12345),
                message=self.message(12345, ""),
                answer=AsyncMock(),
            )
            await self.callback_handler("on_service_selected")(callback)
            text = callback.message.answer.await_args.args[0]
            self.assertIn("нет отдельных задач", text)
            self.assertIn("1. ✅", text)
            self.assertIn("Напоминание о книгах", text)
            self.assertNotIn(
                "Общие",
                [
                    button.text
                    for row in callback.message.answer.await_args.kwargs[
                        "reply_markup"
                    ].inline_keyboard
                    for button in row
                ],
            )
            self.assertTrue(any(
                data.startswith("group_reminder:open:books:")
                for data in self.callback_data_list(callback.message)
            ))
            summary = await self._click(
                callback.message,
                lambda button: button.text == "📊 Сводка личных",
                actor_id=12345,
            )
            summary_text = summary.message.answer.await_args.args[0]
            self.assertIn("Нет ответственных", summary_text)
            back = await self._click(
                summary.message,
                lambda button: button.text == "⬅️ К напоминаниям",
                actor_id=12345,
            )
            self.assertIn(
                "Напоминание о книгах",
                back.message.answer.await_args.args[0],
            )

        asyncio.run(run())

    def test_manual_chat_id_requires_membership_for_service_admin(self):
        async def run():
            self.registry.add_service_admin(67890, "sound", 24680)
            self.context.bot.get_chat = AsyncMock(
                return_value=SimpleNamespace(type="channel")
            )
            self.context.bot.get_chat_member = AsyncMock(
                return_value=SimpleNamespace(status="member")
            )
            self.context.bot.get_chat.side_effect = asyncio.TimeoutError()
            slow = self.message(24680, "/service_chat sound -100555 on")
            await self.message_handler("on_service_chat")(slow)
            self.assertIn("Не удалось проверить", slow.answer.await_args.args[0])
            self.context.bot.get_chat.side_effect = None
            channel = self.message(24680, "/service_chat sound -100555 on")
            await self.message_handler("on_service_chat")(channel)
            self.assertIn("не группа", channel.answer.await_args.args[0])
            self.context.bot.get_chat.return_value = SimpleNamespace(
                type="supergroup"
            )
            self.context.bot.get_chat_member.return_value = SimpleNamespace(
                status="left"
            )
            command = self.message(24680, "/service_chat sound -100555 on")
            await self.message_handler("on_service_chat")(command)
            self.assertIn("не состоите", command.answer.await_args.args[0])
            self.assertNotEqual(
                self.registry.get_service("sound")["group_chat_id"], -100555
            )
            self.context.bot.get_chat_member.return_value = SimpleNamespace(
                status="member"
            )
            command = self.message(24680, "/service_chat sound -100555 on")
            await self.message_handler("on_service_chat")(command)
            self.assertEqual(
                self.registry.get_service("sound")["group_chat_id"], -100555
            )

        asyncio.run(run())

    def test_manual_chat_off_requires_confirmation_before_any_change(self):
        async def run():
            self.registry.set_group_chat(67890, "sound", -100444, enabled=True)
            self.context.bot.get_chat = AsyncMock(return_value=SimpleNamespace(
                type="supergroup", title="Чат", username=None, invite_link=None,
            ))
            self.context.bot.get_chat_member = AsyncMock(
                return_value=SimpleNamespace(status="member"),
            )
            before = self.registry.get_service("sound")
            for actor in (67890, 24680):
                if actor == 24680:
                    self.registry.add_service_admin(67890, "sound", actor)
                picker = SimpleNamespace(
                    data="service:service_chat_set:sound",
                    from_user=SimpleNamespace(id=actor),
                    message=self.message(actor, ""), answer=AsyncMock(),
                )
                await self.callback_handler("on_service_selected")(picker)
                typed = self.message(actor, "-100555 off")
                await self.message_handler("on_service_parameter")(typed)
                self.assertEqual(self.registry.get_service("sound"), before)
                if actor == 24680:
                    self.assertIn("суперадминистратор", typed.answer.await_args.args[0])
                    continue
                confirm = self.callback_data_list(typed)[0]
                self.assertTrue(confirm.startswith("disable:sound:"))
                await self.callback_handler("on_service_disable")(SimpleNamespace(
                    data=confirm, from_user=SimpleNamespace(id=actor),
                    message=typed, answer=AsyncMock(),
                ))
                service = self.registry.get_service("sound")
                self.assertFalse(service["enabled"])
                self.assertEqual(service["group_chat_id"], -100555)
                self.registry.set_group_chat(
                    67890, "sound", before["group_chat_id"], enabled=before["enabled"],
                )

        asyncio.run(run())

    def test_service_card_hub_buttons_open_sections(self):
        async def run():
            for operation, expected in (
                ("service_chat", "выберите действие"),
                ("service_admin_remove", "введите Telegram ID"),
            ):
                callback = SimpleNamespace(
                    data=f"service:{operation}:sound",
                    from_user=SimpleNamespace(id=67890),
                    message=self.message(67890, ""),
                    answer=AsyncMock(),
                )
                await self.callback_handler("on_service_selected")(callback)
                self.assertIn(
                    expected, callback.message.answer.await_args.args[0]
                )

        asyncio.run(run())

    def overview_events(self):
        return [
            ServiceEvent(
                "sound",
                date(2026, 10, 25),
                (
                    ServiceAssignment("Микшер", "Алексей"),
                    ServiceAssignment("Микшер", "Мария"),
                    ServiceAssignment("Пульт", "Иван"),
                    ServiceAssignment("Праздник", "", True),
                ),
            ),
            ServiceEvent(
                "books", date(2026, 10, 25), (ServiceAssignment("Слайды", "Ольга"),)
            ),
            ServiceEvent(
                "sound", date(2026, 11, 1), (ServiceAssignment("Микшер", "Олег"),)
            ),
        ]

    async def open_contact_overview(self):
        self.registry.set_responsible_contact(
            67890, "sound", "Микшер", "Алексей", 11111, "alex"
        )
        self.registry.set_responsible_contact(
            67890, "sound", "Пульт", "Иван", 33333
        )
        self.context.load_upcoming_service_events = AsyncMock(
            return_value=self.overview_events()
        )
        self.context.load_service_task_assignments = AsyncMock(
            return_value={"Микшер": ["Алексей", "Мария"], "Пульт": ["Иван"]}
        )
        message = self.message(67890, "/service_contact sound")
        await self.message_handler("on_service_contact")(message)
        # «👥 Ответственные» открывается списком людей; даты — кнопкой.
        self.assertIn("ЗВУК › 👥 Состав", message.answer.await_args.args[0])
        dates = await self.press_contact(message, "🗓 По датам графика")
        return dates.message

    async def press_contact(self, source, label):
        button = next(
            button
            for row in source.answer.await_args.kwargs["reply_markup"].inline_keyboard
            for button in row
            if button.text == label
        )
        callback = SimpleNamespace(
            data=button.callback_data,
            from_user=SimpleNamespace(id=67890),
            message=self.message(67890, ""),
            answer=AsyncMock(),
        )
        handler = (
            "on_contact_navigation"
            if button.callback_data.startswith("contact_nav:")
            else "on_service_selected"
        )
        await self.callback_handler(handler)(callback)
        return callback

    def test_contact_overview_shows_people_and_link_status_by_date(self):
        async def run():
            message = await self.open_contact_overview()

            text = message.answer.await_args.args[0]
            self.assertIn("👥 Ответственные · ЗВУК", text)
            self.assertIn("🗓 25.10.2026, вс · 1 из 2", text)
            self.assertIn("• Алексей — ✅ @alex", text)
            self.assertIn("• Мария — ⚪ не привязан", text)
            self.assertIn("• Иван — ✅ привязан", text)
            self.assertIn("ℹ️ Праздник", text)
            self.assertIn("📊 Привязано 2 из 3", text)
            self.assertNotIn("Ольга", text)
            self.assertNotIn("11111", text)
            self.assertNotIn("33333", text)
            rows = [
                [button.text for button in row]
                for row in message.answer.await_args.kwargs[
                    "reply_markup"
                ].inline_keyboard
            ]
            self.assertEqual(
                rows,
                [
                    ["➡️ 01.11"],
                    ["👥 К списку ответственных"],
                    ["🔔 Напоминания"],
                    ["⬅️ К карточке служения"],
                ],
            )

        asyncio.run(run())

    def test_contact_overview_navigates_dates_and_rejects_stale_buttons(self):
        async def run():
            message = await self.open_contact_overview()
            following = await self.press_contact(message, "➡️ 01.11")

            text = following.message.answer.await_args.args[0]
            self.assertIn("🗓 01.11.2026, вс · 2 из 2", text)
            self.assertIn("• Олег — ⚪ не привязан", text)
            stale = await self.press_contact(message, "👥 К списку ответственных")
            self.assertIn("устарел", stale.answer.await_args.args[0])
            back = await self.press_contact(following.message, "⬅️ 25.10")
            self.assertIn("1 из 2", back.message.answer.await_args.args[0])

        asyncio.run(run())

    def test_contact_overview_edit_opens_tasks_and_returns(self):
        async def run():
            message = await self.open_contact_overview()
            tasks = await self.press_contact(message, "👥 К списку ответственных")

            self.assertIn(
                "ЗВУК › 👥 Состав", tasks.message.answer.await_args.args[0]
            )
            overview = await self.press_contact(
                tasks.message, "🗓 По датам графика"
            )
            self.assertIn(
                "📊 Привязано 2 из 3", overview.message.answer.await_args.args[0]
            )

        asyncio.run(run())

    def test_contact_overview_opens_notifications_and_card(self):
        async def run():
            message = await self.open_contact_overview()
            notify = await self.press_contact(message, "🔔 Напоминания")
            self.assertIn(
                "ЗВУК", notify.message.answer.await_args.args[0]
            )
            self.assertIsNotNone(
                notify.message.answer.await_args.kwargs.get("reply_markup")
            )

            message = await self.open_contact_overview()
            card = await self.press_contact(message, "⬅️ К карточке служения")
            self.assertIn(
                "⚙️ ЗВУК", card.message.answer.await_args.args[0]
            )

        asyncio.run(run())

    def test_contact_overview_reports_unavailable_dates(self):
        async def run():
            self.context.load_upcoming_service_events = AsyncMock(
                side_effect=RuntimeError("offline")
            )
            message = self.message(67890, "/service_contact sound")
            await self.message_handler("on_service_contact")(message)
            self.assertIn("ЗВУК › 👥 Состав", message.answer.await_args.args[0])
            dates = await self.press_contact(message, "🗓 По датам графика")
            self.assertIn(
                "Не удалось загрузить график по датам",
                dates.message.answer.await_args.args[0],
            )
            tasks = await self.press_contact(
                dates.message, "👥 К списку ответственных"
            )
            self.assertIn(
                "ЗВУК › 👥 Состав", tasks.message.answer.await_args.args[0]
            )

        asyncio.run(run())

    @staticmethod
    def inline_labels(message):
        return [
            button.text
            for row in message.answer.await_args.kwargs[
                "reply_markup"
            ].inline_keyboard
            for button in row
        ]

    async def press_inline(self, source, label, handler):
        button = next(
            button
            for row in source.answer.await_args.kwargs["reply_markup"].inline_keyboard
            for button in row
            if button.text == label
        )
        callback = SimpleNamespace(
            data=button.callback_data,
            from_user=SimpleNamespace(id=67890),
            message=self.message(67890, ""),
            answer=AsyncMock(),
        )
        await self.callback_handler(handler)(callback)
        return callback

    async def open_members(self):
        from bot.excel_parser import RosterMember

        self.registry.sync_service_rosters({
            "sound": [
                RosterMember("Алексей", ("Микшер",)),
                RosterMember("Пётр", ("Пульт",)),
            ]
        })
        message = await self.open_contact_overview()
        return await self.press_contact(message, "👥 К списку ответственных")

    async def open_person(self, source, label):
        """Человек из «Состава» → карточка → «Привязать Telegram»."""
        card = await self.press_inline(
            source, label, "on_contact_person_choice"
        )
        return await self.press_inline(
            card.message, "🔗 Привязать Telegram", "on_member_card"
        )

    async def type_text(self, text):
        message = self.message(67890, text)
        await self.message_handler("on_service_parameter")(message)
        return message

    def test_member_list_marks_roster_bound_and_unlisted_people(self):
        async def run():
            members = await self.open_members()

            text = members.message.answer.await_args.args[0]
            self.assertIn("📊 Привязано 2 из 4", text)
            self.assertIn("⚠️ нет в списке", text)
            self.assertEqual(
                self.inline_labels(members.message),
                [
                    "✅ Алексей · @alex",
                    "⚪ Пётр · не привязан",
                    "✅ 🆕 Иван",
                    "⚠️ Мария · нет в списке",
                    "➕ Добавить участника",
                    "🗓 По датам графика",
                    "⬅️ К карточке служения",
                ],
            )

        asyncio.run(run())

    def test_member_short_name_edit_reset_cancel_and_stale(self):
        async def run():
            members = await self.open_members()
            card = await self.press_inline(
                members.message, "⚪ Пётр · не привязан", "on_contact_person_choice",
            )
            edited = await self.press_inline(
                card.message, "✏️ Короткое имя", "on_member_card",
            )
            self.assertIn("32 символов", "".join(
                call.args[0] for call in edited.message.answer.await_args_list if call.args
            ))
            invalid = await self.type_text("И" * 33)
            self.assertIn("не более 32", "".join(
                call.args[0] for call in invalid.answer.await_args_list if call.args
            ))
            saved = await self.type_text("Петя")
            self.assertEqual(self.registry.service_display_names("sound"), {"Пётр": "Петя"})
            again = await self.press_inline(saved, "✏️ Короткое имя", "on_member_card")
            cancelled = await self.press_inline(
                again.message, "⬅️ К человеку", "on_member_card",
            )
            stale = SimpleNamespace(
                data=self.callback_data_list(again.message)[0],
                from_user=SimpleNamespace(id=67890),
                message=self.message(67890, ""), answer=AsyncMock(),
            )
            await self.callback_handler("on_member_card")(stale)
            self.assertIn("устарел", stale.answer.await_args.args[0])
            self.assertEqual(self.registry.service_display_names("sound")["Пётр"], "Петя")
            again = await self.press_inline(
                cancelled.message, "✏️ Короткое имя", "on_member_card",
            )
            await self.press_inline(
                again.message, "🧹 Убрать короткое имя", "on_member_card",
            )
            self.assertEqual(self.registry.service_display_names("sound"), {})

        asyncio.run(run())

    def test_member_month_limit(self):
        async def run():
            members = await self.open_members()
            card = await self.press_inline(
                members.message, "⚪ Пётр · не привязан", "on_contact_person_choice"
            )
            self.assertIn(
                "🔢 Служений в месяц: как у служения — без лимита",
                card.message.answer.await_args.args[0],
            )
            choice = await self.press_inline(card.message, "🔢 В месяц", "on_member_card")
            manual = await self.press_inline(choice.message, "✏️ Вручную", "on_member_card")
            high = await self.press_inline(manual.message, "🔢 1", "on_member_card")
            result = await self.press_inline(high.message, "🔢 3", "on_member_card")
            self.assertIn(
                "🔢 Служений в месяц: 1–3 (личный)",
                result.message.answer.await_args.args[0],
            )
            person = next(
                item for item in self.registry.readiness_people("sound")
                if item["name"] == "Пётр"
            )
            self.assertEqual(person["month_limit"], (1, 3))
            choice = await self.press_inline(result.message, "🔢 В месяц", "on_member_card")
            await self.press_inline(choice.message, "↩️ Как у служения", "on_member_card")
            person = next(
                item for item in self.registry.readiness_people("sound")
                if item["name"] == "Пётр"
            )
            self.assertIsNone(person["month_limit"])

        asyncio.run(run())

    def test_excel_schedule_loads_month_as_final_plan(self):
        from bot.excel_parser import ServiceAssignment, ServiceEvent

        async def run():
            self.registry.add_service_admin(67890, "sound", 24680)
            self.registry.set_responsible_contact(
                67890, "sound", "звук", "Алексей", 11111, "alex"
            )
            self.context.load_upcoming_service_events = AsyncMock(return_value=[
                ServiceEvent("sound", date(2026, 11, 1), (
                    ServiceAssignment("звук", "Алексей"),
                    ServiceAssignment("сборка", "Пётр"),
                )),
                ServiceEvent("sound", date(2026, 11, 4), (
                    ServiceAssignment("спевка", "Вера"),
                )),
            ])

            def callback(data):
                return SimpleNamespace(
                    data=data, from_user=SimpleNamespace(id=24680),
                    message=self.message(24680, ""), answer=AsyncMock(),
                )

            screen = callback("service:service_excel:sound")
            await self.callback_handler("on_service_selected")(screen)
            text = "".join(
                call.args[0] for call in screen.message.answer.await_args_list
            )
            self.assertIn("ЗВУК › График › Загрузить с диска", text)
            self.assertIn(
                "график в БД бота не утверждён — напоминания приостановлены",
                text,
            )
            load = callback("xls:load:sound:202611")
            await self.callback_handler("on_excel_schedule")(load)
            preview = load.message.answer.await_args.args[0]
            self.assertIn("01.11: звук — Алексей; сборка — Пётр", preview)
            self.assertIn("Не привязаны к Telegram (напоминание не получат): Пётр",
                          preview)
            # Спевка в среду без своего дня задачи — пропущена с подсказкой.
            self.assertIn("ср 04.11 спевка", preview)
            buttons = {
                button.text: button.callback_data
                for row in load.message.answer.await_args.kwargs[
                    "reply_markup"
                ].inline_keyboard
                for button in row
            }
            cancel = callback(buttons["❌ Отмена"])
            await self.callback_handler("on_excel_schedule")(cancel)
            self.assertIsNone(self.registry.get_schedule_plan("sound", "2026-11"))
            stale = callback(buttons["✅ Подтвердить"])
            await self.callback_handler("on_excel_schedule")(stale)
            self.assertIn("устарел", "".join(
                call.args[0] for call in stale.message.answer.await_args_list
            ))
            load = callback("xls:load:sound:202611")
            await self.callback_handler("on_excel_schedule")(load)
            ok = next(
                button.callback_data
                for row in load.message.answer.await_args.kwargs[
                    "reply_markup"
                ].inline_keyboard
                for button in row if button.text == "✅ Подтвердить"
            )
            # Пока смотрели предпросмотр, график месяца поправили.
            self.registry.save_schedule_draft(
                "sound", "2026-11", run_id=None, dates=["2026-11-01"],
                tasks=[], slots={}, last_served={},
            )
            late = callback(ok)
            await self.callback_handler("on_excel_schedule")(late)
            self.assertIn("изменился после предпросмотра", "".join(
                call.args[0] for call in late.message.answer.await_args_list
            ))
            self.assertEqual(
                self.registry.get_schedule_plan("sound", "2026-11")["source"],
                "poll",
            )
            self.context.schedule_candidate_version = MagicMock(return_value="one")

            def loaded_source(**kwargs):
                kwargs["source_out"]["digest"] = self.context.schedule_candidate_version()
                return self.context.load_upcoming_service_events.return_value

            self.context.load_upcoming_service_events.side_effect = loaded_source
            load = callback("xls:load:sound:202611")
            await self.callback_handler("on_excel_schedule")(load)
            ok = next(
                button.callback_data
                for row in load.message.answer.await_args.kwargs[
                    "reply_markup"
                ].inline_keyboard
                for button in row if button.text == "✅ Подтвердить"
            )
            self.context.schedule_candidate_version.return_value = "two"
            changed_candidate = callback(ok)
            await self.callback_handler("on_excel_schedule")(changed_candidate)
            self.assertIn("Excel изменился после предпросмотра", "".join(
                call.args[0]
                for call in changed_candidate.message.answer.await_args_list
            ))
            self.assertEqual(
                self.registry.get_schedule_plan("sound", "2026-11")["source"],
                "poll",
            )
            load = callback("xls:load:sound:202611")
            await self.callback_handler("on_excel_schedule")(load)
            ok = next(
                button.callback_data
                for row in load.message.answer.await_args.kwargs[
                    "reply_markup"
                ].inline_keyboard
                for button in row if button.text == "✅ Подтвердить"
            )
            confirmed = callback(ok)
            await self.callback_handler("on_excel_schedule")(confirmed)
            self.assertIn(
                "из Excel — итоговый в боте",
                "".join(
                    call.args[0]
                    for call in confirmed.message.answer.await_args_list
                ),
            )
            plan = self.registry.get_schedule_plan("sound", "2026-11")
            self.assertEqual(plan["source"], "excel")
            self.assertEqual(plan["approved"], plan["slots"])
            self.assertEqual(plan["slots"]["2026-11-01"]["звук"], ["Алексей"])
            self.assertFalse(plan["dirty"])

        asyncio.run(run())

    def test_excel_preview_tracks_loaded_bytes_not_latest_manifest(self):
        async def run():
            def loaded_source(**kwargs):
                kwargs["source_out"]["digest"] = "loaded"
                return [ServiceEvent("sound", date(2026, 11, 1), (
                    ServiceAssignment("звук", "Алексей"),
                ))]

            self.context.load_upcoming_service_events = AsyncMock(side_effect=loaded_source)
            self.context.schedule_candidate_version = MagicMock(return_value="newer")

            def callback(data):
                return SimpleNamespace(
                    data=data, from_user=SimpleNamespace(id=67890),
                    message=self.message(67890, ""), answer=AsyncMock(),
                )

            load = callback("xls:load:sound:202611")
            await self.callback_handler("on_excel_schedule")(load)
            ok = next(
                button.callback_data
                for row in load.message.answer.await_args.kwargs[
                    "reply_markup"
                ].inline_keyboard
                for button in row if button.text == "✅ Подтвердить"
            )
            confirm = callback(ok)
            await self.callback_handler("on_excel_schedule")(confirm)
            self.assertIn("Excel изменился после предпросмотра", "".join(
                call.args[0] for call in confirm.message.answer.await_args_list
            ))
            self.assertIsNone(self.registry.get_schedule_plan("sound", "2026-11"))

        asyncio.run(run())

    def test_excel_import_cancelled_while_confirmation_waits_for_lock(self):
        from bot import readiness

        async def run():
            self.context.load_upcoming_service_events = AsyncMock(return_value=[
                ServiceEvent("sound", date(2026, 11, 1), (
                    ServiceAssignment("звук", "Алексей"),
                )),
            ])

            def callback(data):
                return SimpleNamespace(
                    data=data, from_user=SimpleNamespace(id=67890),
                    message=self.message(67890, ""), answer=AsyncMock(),
                )

            load = callback("xls:load:sound:202611")
            await self.callback_handler("on_excel_schedule")(load)
            buttons = {
                button.text: button.callback_data
                for row in load.message.answer.await_args.kwargs[
                    "reply_markup"
                ].inline_keyboard
                for button in row
            }
            async with readiness.service_lock("sound"):
                confirm = asyncio.create_task(self.callback_handler("on_excel_schedule")(
                    callback(buttons["✅ Подтвердить"])
                ))
                await asyncio.sleep(0)
                self.assertFalse(confirm.done())
                await self.callback_handler("on_excel_schedule")(
                    callback(buttons["❌ Отмена"])
                )
            await confirm
            self.assertIsNone(self.registry.get_schedule_plan("sound", "2026-11"))

        asyncio.run(run())

    def test_excel_import_preserves_source_date_over_personal_day(self):
        from bot import schedule_plan
        from bot.excel_parser import RosterMember, ServiceAssignment, ServiceEvent

        async def run():
            self.registry.add_service_admin(67890, "sound", 24680)
            self.registry.sync_service_rosters({
                "sound": [RosterMember("Алексей", ("звук",))],
            })
            self.registry.set_responsible_contact(
                67890, "sound", "звук", "Алексей", 11111, "alex"
            )
            self.registry.set_service_member_day(
                67890, "sound", "Алексей", -3
            )
            self.context.load_upcoming_service_events = AsyncMock(return_value=[
                ServiceEvent("sound", date(2026, 11, 8), (
                    ServiceAssignment("звук", "Алексей"),
                )),
            ])

            def callback(data):
                return SimpleNamespace(
                    data=data, from_user=SimpleNamespace(id=24680),
                    message=self.message(24680, ""), answer=AsyncMock(),
                )

            load = callback("xls:load:sound:202611")
            await self.callback_handler("on_excel_schedule")(load)
            with self.subTest(stage="preview"):
                preview = load.message.answer.await_args.args[0]
                self.assertIn("08.11: звук — Алексей;", preview)
            confirm = next(
                button.callback_data
                for row in load.message.answer.await_args.kwargs[
                    "reply_markup"
                ].inline_keyboard
                for button in row
                if button.callback_data.startswith("xls:ok:sound:")
            )
            await self.callback_handler("on_excel_schedule")(callback(confirm))

            plan = self.registry.get_schedule_plan("sound", "2026-11")
            self.assertIsNotNone(plan)
            self.assertEqual(plan["source"], "excel")
            self.assertEqual(plan["approved"], plan["slots"])
            with self.subTest(stage="approved_day"):
                self.assertEqual(
                    plan["approved_slot_days"]["2026-11-08"]["звук"]["Алексей"],
                    0,
                )
            with self.subTest(stage="history_day"):
                self.assertEqual(
                    plan["served_offsets"].get("2026-11-08", {}).get("Алексей", 0),
                    0,
                )
            with self.subTest(stage="reminder_events"):
                self.assertEqual(
                    [
                        (item.event_date, assignment.name)
                        for item in schedule_plan.plan_events(
                            plan, self.registry.readiness_people("sound")
                        )
                        for assignment in item.assignments
                    ],
                    [(date(2026, 11, 8), "Алексей")],
                )

        asyncio.run(run())

    def test_member_week_tasks(self):
        async def run():
            members = await self.open_members()
            self.registry.set_service_member_tasks(
                67890, "sound", "Пётр", ["звук", "сборка"]
            )
            card = await self.press_inline(
                members.message, "⚪ Пётр · не привязан", "on_contact_person_choice"
            )
            self.assertIn(
                "🔗 В одну неделю: как у служения — нет",
                card.message.answer.await_args.args[0],
            )
            week = await self.press_inline(
                card.message, "🔗 В одну неделю", "on_member_card"
            )
            one = await self.press_inline(week.message, "⬜ звук", "on_member_card")
            refused = await self.press_inline(
                one.message, "✅ Сохранить", "on_member_card"
            )
            self.assertIn(
                "хотя бы две", refused.message.answer.await_args.args[0]
            )
            two = await self.press_inline(
                refused.message, "⬜ сборка", "on_member_card"
            )
            saved = await self.press_inline(two.message, "✅ Сохранить", "on_member_card")
            self.assertIn(
                "✅ В одну неделю: звук + сборка (личное)",
                saved.message.answer.await_args.args[0],
            )
            person = next(
                item for item in self.registry.readiness_people("sound")
                if item["name"] == "Пётр"
            )
            self.assertEqual(person["week_tasks"], ["звук", "сборка"])
            week = await self.press_inline(
                saved.message, "🔗 В одну неделю", "on_member_card"
            )
            await self.press_inline(week.message, "↩️ Как у служения", "on_member_card")
            person = next(
                item for item in self.registry.readiness_people("sound")
                if item["name"] == "Пётр"
            )
            self.assertIsNone(person["week_tasks"])

        asyncio.run(run())

    def test_task_same_week_flag(self):
        async def run():
            opened = SimpleNamespace(
                data="svc_tasks:open:sound",
                from_user=SimpleNamespace(id=67890),
                message=self.message(67890, ""),
                answer=AsyncMock(),
            )
            await self.callback_handler("on_service_tasks")(opened)
            week = await self._click(
                opened.message,
                lambda button: button.text == "🔗 В одну неделю",
                actor_id=67890,
            )
            toggled = await self._click(
                week.message, lambda button: button.text == "⬜ звук",
                actor_id=67890,
            )
            self.assertIn(
                "✅ «звук» в связке 🔗", toggled.message.answer.await_args.args[0]
            )
            self.assertTrue(next(
                task for task in self.registry.list_service_tasks("sound")
                if task["name"] == "звук"
            )["same_week"])
            back = await self._click(
                toggled.message, lambda button: button.text == "⬅️ К задачам",
                actor_id=67890,
            )
            self.assertIn("🎚️ звук · 🔗", back.message.answer.await_args.args[0])

        asyncio.run(run())

    def test_member_binding_by_username_only(self):
        async def run():
            members = await self.open_members()
            person = await self.open_person(members.message, "⚪ Пётр · не привязан")
            self.assertIn(
                "или только @username", person.message.answer.await_args.args[0]
            )
            confirmation = await self.type_text("@Petr_New")
            text = confirmation.answer.await_args.args[0]
            self.assertIn("привязать «Пётр» (все его задачи служения) к @Petr_New", text)
            self.assertIn("Бот ещё не видел", text)
            result = await self.press_inline(
                confirmation, "✅ Подтвердить", "on_username_binding"
            )
            self.assertIn(
                "привяжется к @Petr_New", result.message.answer.await_args.args[0]
            )
            self.assertEqual(self.registry.username_binding("sound", "Пётр"), "Petr_New")

            # Человек пишет боту (username в другом регистре) — привязан,
            # админу приходит сообщение.
            middleware = next(
                item for item in self.admin_router.message.outer_middleware
                if item.__name__ == "username_binding_middleware"
            )
            handler = AsyncMock()
            event = SimpleNamespace(
                from_user=SimpleNamespace(id=77777, username="petr_new", is_bot=False)
            )
            await middleware(handler, event, {})
            handler.assert_awaited_once()
            member = next(
                item for item in self.registry.list_service_members("sound")
                if item["name"] == "Пётр"
            )
            self.assertEqual(member["telegram_id"], 77777)
            self.assertIsNone(self.registry.username_binding("sound", "Пётр"))
            sent = self.context.bot.send_message.await_args.kwargs
            self.assertEqual(sent["chat_id"], 67890)
            self.assertIn("«Пётр» привязан(а)", sent["text"])

            # Username ушёл к другому и вернулся — снова за первым.
            for user_id in (88888, 77777):
                await middleware(handler, SimpleNamespace(
                    from_user=SimpleNamespace(
                        id=user_id, username="petr_new", is_bot=False
                    )
                ), {})
            self.assertEqual(self.registry.known_telegram_id("petr_new"), 77777)

            # Повторное сообщение — привязок больше нет.
            self.context.bot.send_message.reset_mock()
            await middleware(handler, event, {})
            self.context.bot.send_message.assert_not_awaited()

            # Уже знакомый боту @username — сразу обычное подтверждение с ID.
            await self.open_person(
                (await self.open_members()).message, "⚠️ Мария · нет в списке"
            )
            confirmation = await self.type_text("@petr_new")
            self.assertIn("77777", confirmation.answer.await_args.args[0])

            # Пришедший уже привязан к другому имени — не перепривязывается.
            await self.open_person(
                (await self.open_members()).message, "⚠️ Мария · нет в списке"
            )
            await self.press_inline(
                await self.type_text("@petr_alt"), "✅ Подтвердить",
                "on_username_binding",
            )
            await middleware(handler, SimpleNamespace(
                from_user=SimpleNamespace(id=77777, username="petr_alt", is_bot=False)
            ), {})
            self.assertIn(
                "уже привязан к «Пётр»",
                self.context.bot.send_message.await_args.kwargs["text"],
            )
            # Смена Telegram по @username — в карточке и текущий, и ожидаемый.
            self.registry.add_username_binding(67890, "sound", "Пётр", "", "petr_next")
            card = await self.press_inline(
                (await self.open_members()).message, "✅ Пётр · @petr_new",
                "on_contact_person_choice",
            )
            self.assertIn(
                "Telegram: @petr_new\n⏳ ждёт @petr_next",
                card.message.answer.await_args.args[0],
            )
            self.assertNotIn(
                ("Мария", 77777),
                {
                    (item["name"], item["telegram_id"])
                    for item in self.registry.list_service_members("sound")
                },
            )

        asyncio.run(run())

    def test_member_binding_covers_all_schedule_tasks(self):
        async def run():
            members = await self.open_members()
            person = await self.open_person(
                members.message, "⚠️ Мария · нет в списке"
            )
            prompt = person.message.answer.await_args
            self.assertIn("все его задачи служения", prompt.args[0])
            keyboard = prompt.kwargs["reply_markup"].keyboard
            self.assertEqual(keyboard[1][0].text, "⬅️ Назад")

            confirmation = await self.type_text("55555 @maria_x")
            self.assertIn(
                "привязать «Мария» во всех задачах служения",
                confirmation.answer.await_args.args[0],
            )
            result = await self.press_inline(
                confirmation, "✅ Подтвердить", "on_shared_admin_confirmation"
            )

            text = result.message.answer.await_args.args[0]
            self.assertIn("«Мария» связан", text)
            self.assertIn("во всех задачах", text)
            self.assertIn("ЗВУК › 👥 Состав", text)
            contacts = {
                (contact["task"], contact["name"]): contact["telegram_id"]
                for contact in self.registry.list_responsible_contacts(
                    67890, "sound"
                )
            }
            self.assertEqual(contacts[("Микшер", "Мария")], 55555)
            member = next(
                item for item in self.registry.list_service_members("sound")
                if item["name"] == "Мария"
            )
            self.assertEqual(member["telegram_id"], 55555)

        asyncio.run(run())

    def test_member_user_step_back_and_cancel_return_to_people(self):
        async def run():
            members = await self.open_members()
            await self.open_person(members.message, "⚪ Пётр · не привязан")
            back = await self.type_text("⬅️ Назад")
            self.assertIn("👥 Состав", back.answer.await_args.args[0])
            self.assertEqual(
                self.registry.list_responsible_contacts(67890, "sound")[0][
                    "name"
                ],
                "Алексей",
            )

            await self.open_person(back, "⚪ Пётр · не привязан")
            cancel = self.message(67890, "/cancel")
            await self.message_handler("on_cancel_service_command")(cancel)
            calls = [call.args[0] for call in cancel.answer.await_args_list]
            self.assertIn("↩️ Шаг отменён.", calls[0])
            self.assertIn("👥 Состав", calls[-1])

            stale = await self.type_text("55555")
            self.assertNotIn("Подтвердить", str(stale.answer.await_args))

        asyncio.run(run())

    def test_member_card_edits_tasks_and_removes(self):
        from bot.excel_parser import RosterMember

        async def run():
            members = await self.open_members()
            card = await self.press_inline(
                members.message, "⚪ Пётр · не привязан",
                "on_contact_person_choice",
            )
            text = card.message.answer.await_args.args[0]
            self.assertIn("👤 Пётр", text)
            self.assertIn("Задачи: Пульт", text)
            tasks = await self.press_inline(
                card.message, "📝 Задачи", "on_member_card"
            )
            labels = self.inline_labels(tasks.message)
            self.assertIn("⬜ звук", labels)
            toggled = await self.press_inline(
                tasks.message, "⬜ звук", "on_member_card"
            )
            saved = await self.press_inline(
                toggled.message, "✅ Сохранить", "on_member_card"
            )
            self.assertIn(
                "✅ Задачи сохранены: звук",
                saved.message.answer.await_args.args[0],
            )
            # «Списки» больше не меняют задачи, заданные в боте.
            self.registry.sync_service_rosters({"sound": [
                RosterMember("Алексей", ("Микшер",)),
                RosterMember("Пётр", ("Пульт",)),
            ]})
            peter = next(
                item for item in self.registry.list_service_members("sound")
                if item["name"] == "Пётр"
            )
            self.assertEqual(peter["tasks"], ["звук"])
            self.assertTrue(peter["tasks_manual"])

            day = await self.press_inline(
                saved.message, "📅 Свой день", "on_member_card"
            )
            chosen = await self.press_inline(
                day.message, "📅 чт (вс -3)", "on_member_card"
            )
            self.assertIn(
                "✅ День служения: чт (вс -3)",
                chosen.message.answer.await_args.args[0],
            )
            ask = await self.press_inline(
                chosen.message, "🗑 Удалить из состава", "on_member_card"
            )
            self.assertIn("Убрать «Пётр»", ask.message.answer.await_args.args[0])
            done = await self.press_inline(
                ask.message, "✅ Да, убрать", "on_member_card"
            )
            text = done.message.answer.await_args.args[0]
            self.assertIn("убран(а) из состава", text)
            self.assertIn("бот его больше не вернёт", text)
            self.assertNotIn(
                "Пётр",
                [item["name"] for item in self.registry.list_service_members("sound")],
            )
            stale = await self.press_inline(
                ask.message, "✅ Да, убрать", "on_member_card"
            )
            self.assertIn("устарел", stale.answer.await_args.args[0])

        asyncio.run(run())

    def test_member_can_be_added_beyond_the_roster(self):
        async def run():
            members = await self.open_members()
            await self.press_contact(members.message, "➕ Добавить участника")
            prompt = await self.type_text("Новый   Человек")
            self.assertIn("«Новый Человек»", prompt.answer.await_args.args[0])
            confirmation = await self.type_text("77777")
            await self.press_inline(
                confirmation, "✅ Подтвердить", "on_shared_admin_confirmation"
            )

            member = next(
                item for item in self.registry.list_service_members("sound")
                if item["name"] == "Новый Человек"
            )
            self.assertEqual(member["telegram_id"], 77777)
            self.assertFalse(member["in_list"])

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
