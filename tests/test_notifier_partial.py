import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram.types import LinkPreviewOptions

from bot.notifier import (
    _notify_admin,
    build_general_message,
    build_personal_message,
    send_assignments,
)


class PartialNotificationTest(unittest.TestCase):
    def test_admin_warning_uses_safe_html_screen(self):
        async def notify():
            bot = SimpleNamespace(send_message=AsyncMock())

            await _notify_admin(
                bot,
                12345,
                "Ошибка: <untrusted diagnostic>",
                MagicMock(),
            )

            bot.send_message.assert_awaited_once_with(
                12345,
                "<b>⚠️ Ошибка: &lt;untrusted diagnostic&gt;</b>",
                parse_mode="HTML",
                reply_markup=None,
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )

        asyncio.run(notify())

    def test_dynamic_html_fields_are_escaped(self):
        templates = {
            "general": "Ответственный: @{tg_responsible}",
            "roles": {"Ответственный": "{name}"},
        }

        group_text = build_general_message(
            {"Ответственный": {"username": "<b>alex</b>"}},
            templates,
            "11.10.2026",
        )
        personal_text = build_personal_message(
            "Ответственный",
            {"name": "<b>Алексей</b>"},
            templates,
            "11.10.2026",
        )

        self.assertEqual(group_text, "Ответственный: @<b>alex</b>")
        self.assertEqual(personal_text, "🔔 <b>Алексей</b>")

    def test_sends_group_and_available_participant_with_missing_roles(self):
        async def send():
            bot = SimpleNamespace(
                send_message=AsyncMock(
                    side_effect=[
                        SimpleNamespace(message_id=10),
                        SimpleNamespace(message_id=11, chat=SimpleNamespace(id=12345)),
                    ]
                )
            )
            assignments = {
                "Ответственный": {
                    "name": "Алексей",
                    "username": "alex",
                    "tg_id": 12345,
                }
            }
            state = {
                "participants": {
                    "Ответственный": {
                        "message_id": None,
                        "chat_id": None,
                    }
                }
            }
            debug_logger = MagicMock()
            templates = {
                "general": (
                    "Дата {sunday_date}: "
                    "ответственный @{tg_responsible}; "
                    "ведущий @{tg_leader}; проповедник @{tg_preacher}"
                ),
                "roles": {
                    "Ответственный": "{name}, {sunday_date}",
                },
            }

            with patch("bot.notifier.asyncio.sleep", new=AsyncMock()):
                await send_assignments(
                    bot=bot,
                    group_chat_id=-100,
                    admin_id=67890,
                    assignments=assignments,
                    state=state,
                    templates=templates,
                    sunday_str="11.10.2026",
                    sunday_iso="2026-10-11",
                    interaction_logger=MagicMock(),
                    debug_logger=debug_logger,
                )

            first_call = bot.send_message.await_args_list[0]
            group_text = first_call.args[1]
            self.assertIn("@alex", group_text)
            self.assertIn("ведущий не найден", group_text)
            self.assertEqual(len(bot.send_message.await_args_list), 2)
            self.assertEqual(
                bot.send_message.await_args_list[1].args,
                (12345, "<b>🔔 Алексей, 11.10.2026</b>"),
            )
            self.assertEqual(state["participants"]["Ответственный"]["message_id"], 11)
            self.assertEqual(
                state["participants"]["Ответственный"]["group_chat_id"],
                -100,
            )
            debug_messages = [
                call.args[0] for call in debug_logger.debug.call_args_list
            ]
            self.assertIn("Подготовлено сообщение в групповой чат:\n%s", debug_messages)
            self.assertIn("Подготовлено личное сообщение: роль=%s, tg_id=%s, username=@%s\n%s", debug_messages)

        asyncio.run(send())

    def test_informational_participant_is_not_sent_a_personal_message(self):
        async def send():
            bot = SimpleNamespace(
                send_message=AsyncMock(
                    return_value=SimpleNamespace(message_id=10)
                )
            )
            assignments = {
                "Ответственный": {
                    "name": "Причастие",
                    "username": "",
                    "tg_id": 12345,
                    "is_informational": True,
                }
            }
            state = {
                "participants": {
                    "Ответственный": {
                        "message_id": None,
                        "chat_id": None,
                    }
                }
            }
            templates = {
                "general": "Ответственный: @{tg_responsible}",
                "roles": {"Ответственный": "{name}"},
            }

            await send_assignments(
                bot=bot,
                group_chat_id=-100,
                admin_id=67890,
                assignments=assignments,
                state=state,
                templates=templates,
                sunday_str="11.10.2026",
                sunday_iso="2026-10-11",
                interaction_logger=MagicMock(),
                debug_logger=MagicMock(),
            )

            bot.send_message.assert_awaited_once()
            self.assertEqual(bot.send_message.await_args.args[0], -100)
            self.assertTrue(
                state["participants"]["Ответственный"]["notification_skipped"]
            )

        asyncio.run(send())

    def test_long_personal_message_is_rejected_with_admin_warning(self):
        async def send():
            bot = SimpleNamespace(
                send_message=AsyncMock(
                    return_value=SimpleNamespace(
                        message_id=10, chat=SimpleNamespace(id=67890)
                    )
                )
            )
            assignments = {
                "Ответственный": {
                    "name": "😀" * 2100,
                    "username": "alex",
                    "tg_id": 12345,
                }
            }
            state = {
                "participants": {
                    "Ответственный": {
                        "message_id": None,
                        "chat_id": None,
                    }
                }
            }
            templates = {
                "general": "Ответственный: @{tg_responsible}",
                "roles": {"Ответственный": "{name}"},
            }
            with patch("bot.notifier.asyncio.sleep", new=AsyncMock()):
                await send_assignments(
                    bot=bot,
                    group_chat_id=-100,
                    admin_id=67890,
                    assignments=assignments,
                    state=state,
                    templates=templates,
                    sunday_str="11.10.2026",
                    sunday_iso="2026-10-11",
                    interaction_logger=MagicMock(),
                    debug_logger=MagicMock(),
                )
            self.assertGreater(bot.send_message.await_count, 2)
            self.assertEqual(bot.send_message.await_args_list[0].args[0], -100)
            warning_chunks = bot.send_message.await_args_list[1:]
            self.assertTrue(all(call.args[0] == 67890 for call in warning_chunks))
            self.assertIn("не отправлено", warning_chunks[-1].args[1])
            self.assertFalse(state["participants"]["Ответственный"].get("delivered"))

        asyncio.run(send())

    def test_long_group_update_edits_one_message_instead_of_sending_chunks(self):
        async def send():
            bot = SimpleNamespace(
                send_message=AsyncMock(),
                edit_message_text=AsyncMock(),
            )
            assignments = {
                "Ответственный": {
                    "name": "Алексей",
                    "username": "a" * 700,
                    "tg_id": None,
                }
            }
            state = {
                "general_sent": True,
                "general_message_id": 77,
                "general_text": "Старый текст",
                "participants": {"Ответственный": {"delivered": True}},
            }
            templates = {
                "general": "Ответственный: @{tg_responsible}",
                "roles": {},
            }

            await send_assignments(
                bot=bot,
                group_chat_id=-100,
                admin_id=67890,
                assignments=assignments,
                state=state,
                templates=templates,
                sunday_str="11.10.2026",
                sunday_iso="2026-10-11",
                interaction_logger=MagicMock(),
                debug_logger=MagicMock(),
            )

            bot.edit_message_text.assert_awaited_once()
            self.assertEqual(
                bot.edit_message_text.await_args.kwargs["message_id"], 77
            )
            self.assertEqual(bot.send_message.await_count, 0)
            self.assertEqual(state["general_text"], "Ответственный: @" + "a" * 700)

        asyncio.run(send())

    def test_oversized_group_message_is_not_split_or_marked_delivered(self):
        async def send():
            bot = SimpleNamespace(
                send_message=AsyncMock(
                    return_value=SimpleNamespace(message_id=10)
                ),
                edit_message_text=AsyncMock(),
            )
            state = {
                "general_sent": False,
                "general_message_id": None,
                "general_text": None,
                "participants": {"Ответственный": {"delivered": False}},
            }

            complete = await send_assignments(
                bot=bot,
                group_chat_id=-100,
                admin_id=67890,
                assignments={
                    "Ответственный": {
                        "name": "Алексей",
                        "username": "a" * 4100,
                        "tg_id": None,
                    }
                },
                state=state,
                templates={
                    "general": "Ответственный: @{tg_responsible}",
                    "roles": {},
                },
                sunday_str="11.10.2026",
                sunday_iso="2026-10-11",
                interaction_logger=MagicMock(),
                debug_logger=MagicMock(),
            )

            self.assertFalse(complete)
            self.assertEqual(bot.send_message.await_count, 1)
            self.assertEqual(bot.send_message.await_args.args[0], 67890)
            self.assertFalse(state["general_sent"])

        asyncio.run(send())

    def test_user_preference_gates_personal_messages_not_group_messages(self):
        async def send(enabled):
            bot = SimpleNamespace(
                send_message=AsyncMock(
                    side_effect=[
                        SimpleNamespace(message_id=10),
                        SimpleNamespace(
                            message_id=11, chat=SimpleNamespace(id=12345)
                        ),
                    ]
                ),
                edit_message_text=AsyncMock(),
            )
            assignment = {
                "Ответственный": {
                    "name": "Алексей",
                    "username": "alex",
                    "tg_id": 12345,
                }
            }
            state = {
                "general_sent": False,
                "participants": {
                    "Ответственный": {"delivered": False},
                },
            }
            await send_assignments(
                bot=bot,
                group_chat_id=-100,
                admin_id=67890,
                assignments=assignment,
                state=state,
                templates={
                    "general": "Ответственный: @{tg_responsible}",
                    "roles": {"Ответственный": "{name}"},
                },
                sunday_str="11.10.2026",
                sunday_iso="2026-10-11",
                interaction_logger=MagicMock(),
                debug_logger=MagicMock(),
                user_notifications_enabled=lambda _telegram_id: enabled,
            )
            return bot, state

        async def verify():
            opted_in_bot, opted_in_state = await send(True)
            self.assertEqual(opted_in_bot.send_message.await_count, 2)
            self.assertTrue(
                opted_in_state["participants"]["Ответственный"]["delivered"]
            )

            opted_out_bot, opted_out_state = await send(False)
            self.assertEqual(opted_out_bot.send_message.await_count, 1)
            self.assertEqual(
                opted_out_bot.send_message.await_args.args[0], -100
            )
            self.assertFalse(
                opted_out_state["participants"]["Ответственный"]["delivered"]
            )

        asyncio.run(verify())

    def test_failed_personal_delivery_retries_without_resending_successes(self):
        from aiogram.exceptions import TelegramAPIError

        async def send():
            assignments = {
                "Ответственный": {
                    "name": "Алексей", "username": "alex", "tg_id": 12345,
                },
                "Ведущий": {
                    "name": "Павел", "username": "pavel", "tg_id": 23456,
                },
            }
            state = {
                "general_sent": False,
                "general_message_id": None,
                "general_text": None,
                "participants": {
                    role: {"tg_id": info["tg_id"], "delivered": False}
                    for role, info in assignments.items()
                },
            }
            bot = SimpleNamespace(
                send_message=AsyncMock(
                    side_effect=[
                        SimpleNamespace(message_id=10),
                        SimpleNamespace(message_id=11, chat=SimpleNamespace(id=12345)),
                        TelegramAPIError(method=MagicMock(), message="temporary"),
                        SimpleNamespace(message_id=12, chat=SimpleNamespace(id=23456)),
                    ]
                ),
                edit_message_text=AsyncMock(),
            )
            persist = MagicMock()
            templates = {
                "general": "Ответственный: @{tg_responsible}; ведущий: @{tg_leader}; "
                "проповедник: @{tg_preacher}",
                "roles": {
                    "Ответственный": "{name}",
                    "Ведущий": "{name}",
                },
            }
            common = dict(
                bot=bot,
                group_chat_id=-100,
                admin_id=67890,
                assignments=assignments,
                state=state,
                templates=templates,
                sunday_str="11.10.2026",
                sunday_iso="2026-10-11",
                interaction_logger=MagicMock(),
                debug_logger=MagicMock(),
                save_state=persist,
            )

            with patch("bot.notifier._notify_admin", new=AsyncMock()), patch(
                "bot.notifier.asyncio.sleep", new=AsyncMock()
            ):
                await send_assignments(**common)
                self.assertTrue(state["participants"]["Ответственный"]["delivered"])
                self.assertFalse(state["participants"]["Ведущий"]["delivered"])

                await send_assignments(**common)

            self.assertEqual(bot.send_message.await_count, 4)
            self.assertEqual(
                bot.send_message.await_args_list[3].args,
                (23456, "<b>🔔 Павел</b>"),
            )
            bot.edit_message_text.assert_not_awaited()
            self.assertTrue(state["participants"]["Ведущий"]["delivered"])
            self.assertGreaterEqual(persist.call_count, 3)

        asyncio.run(send())

    def test_group_state_save_failure_does_not_skip_personal_delivery(self):
        async def send():
            assignments = {
                "Ответственный": {
                    "name": "Алексей", "username": "alex", "tg_id": 12345,
                }
            }
            state = {
                "general_sent": False,
                "general_message_id": None,
                "general_text": None,
                "participants": {"Ответственный": {"delivered": False}},
            }
            bot = SimpleNamespace(
                send_message=AsyncMock(
                    side_effect=[
                        SimpleNamespace(message_id=10),
                        SimpleNamespace(message_id=11, chat=SimpleNamespace(id=12345)),
                    ]
                ),
                edit_message_text=AsyncMock(),
            )
            persist = MagicMock(side_effect=[OSError("disk full"), None])
            templates = {
                "general": "Ответственный: @{tg_responsible}",
                "roles": {"Ответственный": "{name}"},
            }

            with patch("bot.notifier._notify_admin", new=AsyncMock()) as notify_admin, patch(
                "bot.notifier.asyncio.sleep", new=AsyncMock()
            ):
                result = await send_assignments(
                    bot=bot,
                    group_chat_id=-100,
                    admin_id=67890,
                    assignments=assignments,
                    state=state,
                    templates=templates,
                    sunday_str="11.10.2026",
                    sunday_iso="2026-10-11",
                    interaction_logger=MagicMock(),
                    debug_logger=MagicMock(),
                    save_state=persist,
                )

            self.assertFalse(result)
            self.assertTrue(state["general_sent"])
            self.assertTrue(state["participants"]["Ответственный"]["delivered"])
            self.assertEqual(bot.send_message.await_count, 2)
            notify_admin.assert_awaited_once()
            persist.assert_has_calls([unittest.mock.call(), unittest.mock.call()])

        asyncio.run(send())

    def test_personal_state_save_failure_does_not_skip_next_recipient(self):
        async def send():
            assignments = {
                role: {"name": name, "username": role, "tg_id": tg_id}
                for role, name, tg_id in (
                    ("Ответственный", "Алексей", 12345),
                    ("Ведущий", "Павел", 23456),
                )
            }
            state = {
                "general_sent": False,
                "participants": {
                    role: {"delivered": False} for role in assignments
                },
            }
            bot = SimpleNamespace(
                send_message=AsyncMock(
                    side_effect=[
                        SimpleNamespace(message_id=10),
                        SimpleNamespace(message_id=11, chat=SimpleNamespace(id=12345)),
                        SimpleNamespace(message_id=12, chat=SimpleNamespace(id=23456)),
                    ]
                ),
                edit_message_text=AsyncMock(),
            )
            persist = MagicMock(side_effect=[None, OSError("disk full"), None])

            with patch("bot.notifier._notify_admin", new=AsyncMock()) as notify_admin, patch(
                "bot.notifier.asyncio.sleep", new=AsyncMock()
            ):
                result = await send_assignments(
                    bot=bot,
                    group_chat_id=-100,
                    admin_id=67890,
                    assignments=assignments,
                    state=state,
                    templates={
                        "general": "@{tg_responsible} @{tg_leader}",
                        "roles": {
                            "Ответственный": "{name}",
                            "Ведущий": "{name}",
                        },
                    },
                    sunday_str="11.10.2026",
                    sunday_iso="2026-10-11",
                    interaction_logger=MagicMock(),
                    debug_logger=MagicMock(),
                    save_state=persist,
                )

            self.assertFalse(result)
            self.assertTrue(state["participants"]["Ответственный"]["delivered"])
            self.assertTrue(state["participants"]["Ведущий"]["delivered"])
            self.assertEqual(bot.send_message.await_count, 3)
            notify_admin.assert_awaited_once()

        asyncio.run(send())

    def test_group_fallback_is_not_rendered_as_a_fake_mention(self):
        message = build_general_message(
            {"Ответственный": {"username": ""}},
            {"general": "Ответственный: @{tg_responsible}"},
            "11.10.2026",
        )

        self.assertEqual(message, "Ответственный: не найден")

    def test_group_failure_is_retried_before_private_deliveries(self):
        from aiogram.exceptions import TelegramAPIError

        async def send():
            assignments = {
                "Ответственный": {
                    "name": "Алексей", "username": "alex", "tg_id": 12345,
                }
            }
            state = {
                "general_sent": False,
                "participants": {
                    "Ответственный": {"tg_id": 12345, "delivered": False}
                },
            }
            bot = SimpleNamespace(
                send_message=AsyncMock(
                    side_effect=[
                        TelegramAPIError(method=MagicMock(), message="temporary"),
                        SimpleNamespace(message_id=10),
                        SimpleNamespace(message_id=11, chat=SimpleNamespace(id=12345)),
                    ]
                )
            )
            common = dict(
                bot=bot,
                group_chat_id=-100,
                admin_id=67890,
                assignments=assignments,
                state=state,
                templates={
                    "general": "Ответственный: @{tg_responsible}",
                    "roles": {"Ответственный": "{name}"},
                },
                sunday_str="11.10.2026",
                sunday_iso="2026-10-11",
                interaction_logger=MagicMock(),
                debug_logger=MagicMock(),
            )
            with patch("bot.notifier._notify_admin", new=AsyncMock()), patch(
                "bot.notifier.asyncio.sleep", new=AsyncMock()
            ):
                with self.assertRaises(TelegramAPIError):
                    await send_assignments(**common)
                self.assertFalse(state["general_sent"])
                self.assertFalse(state["participants"]["Ответственный"]["delivered"])

                await send_assignments(**common)

            self.assertEqual(bot.send_message.await_count, 3)
            self.assertEqual(bot.send_message.await_args_list[1].args[0], -100)
            self.assertEqual(bot.send_message.await_args_list[2].args[0], 12345)
            self.assertTrue(state["general_sent"])
            self.assertTrue(state["participants"]["Ответственный"]["delivered"])

        asyncio.run(send())

    def test_failed_group_edit_does_not_block_personal_delivery(self):
        from aiogram.exceptions import TelegramAPIError

        async def send():
            assignments = {
                "Ответственный": {
                    "name": "Алексей", "username": "alex", "tg_id": 12345,
                }
            }
            state = {
                "general_sent": True,
                "general_message_id": 10,
                "general_text": "старый текст",
                "participants": {
                    "Ответственный": {"tg_id": 12345, "delivered": False}
                },
            }
            bot = SimpleNamespace(
                edit_message_text=AsyncMock(
                    side_effect=TelegramAPIError(
                        method=MagicMock(), message="message missing"
                    )
                ),
                send_message=AsyncMock(
                    return_value=SimpleNamespace(
                        message_id=11, chat=SimpleNamespace(id=12345)
                    )
                ),
            )
            with patch("bot.notifier._notify_admin", new=AsyncMock()), patch(
                "bot.notifier.asyncio.sleep", new=AsyncMock()
            ):
                group_complete = await send_assignments(
                    bot=bot,
                    group_chat_id=-100,
                    admin_id=67890,
                    assignments=assignments,
                    state=state,
                    templates={
                        "general": "Ответственный: @{tg_responsible}",
                        "roles": {"Ответственный": "{name}"},
                    },
                    sunday_str="11.10.2026",
                    sunday_iso="2026-10-11",
                    interaction_logger=MagicMock(),
                    debug_logger=MagicMock(),
                )

            bot.edit_message_text.assert_awaited_once()
            bot.send_message.assert_awaited_once()
            self.assertFalse(group_complete)
            self.assertTrue(state["participants"]["Ответственный"]["delivered"])
            self.assertEqual(state["general_text"], "старый текст")

        asyncio.run(send())


if __name__ == "__main__":
    unittest.main()
