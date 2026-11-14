"""Этап 3.3: замена ответственного — опрос, итог, /change, приоритет графика."""
import asyncio
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

from bot.handlers import register
from bot.replacement import (
    finish_replacement,
    parse_change_args,
    poll_answer_choice,
    poll_deadline,
    start_replacement_search,
)
from bot.presentation import apply_replacements
from bot.service_registry import ServiceRegistry, task_token

TZ = ZoneInfo("Europe/Moscow")
SUPER = 67890
ADMIN = 12345
OLD = 4004
MEMBER = 5005
OUTSIDER = 6006
EVENT = date(2099, 1, 4)


class Base(unittest.TestCase):
    def setUp(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.registry = ServiceRegistry(Path(temp_dir.name) / "r.sqlite3")
        self.registry.initialize(SUPER, -100)
        self.registry.set_group_chat(SUPER, "sound", -1005, enabled=True)
        self.registry.add_service_admin(SUPER, "sound", ADMIN, "boss_admin")
        self.registry.set_responsible_contact(
            SUPER, "sound", "звук", "Алексей", OLD, "alex_one"
        )
        self.registry.set_responsible_contact(
            SUPER, "sound", "звук", "Борис", MEMBER, "boris_b"
        )
        self.poll_counter = 0

        async def send_poll(chat_id, question, **kwargs):
            self.poll_counter += 1
            return SimpleNamespace(
                message_id=200 + self.poll_counter,
                poll=SimpleNamespace(id=f"poll{self.poll_counter}"),
            )

        self.bot = SimpleNamespace(
            send_message=AsyncMock(return_value=SimpleNamespace(message_id=100)),
            send_poll=AsyncMock(side_effect=send_poll),
            stop_poll=AsyncMock(),
            edit_message_text=AsyncMock(),
        )
        self.schedule_close = MagicMock()

    def start(self, now=None):
        return asyncio.run(start_replacement_search(
            self.bot, self.registry, "sound", EVENT, "звук", "Алексей", OLD,
            admin_actor_id=SUPER, tz=TZ, logger=MagicMock(),
            schedule_close=self.schedule_close,
            now=now or datetime(2098, 12, 29, 10, 0, tzinfo=timezone.utc),
        ))

    def finish(self, poll, substitute=None):
        return asyncio.run(finish_replacement(
            self.bot, self.registry, poll, substitute,
            source="admin" if substitute else "poll", actor_id=None,
            admin_actor_id=SUPER, tz=TZ, logger=MagicMock(), report_limit=4096,
        ))

    def sent_to(self, chat_id):
        return [
            call.args[1] for call in self.bot.send_message.await_args_list
            if call.args[0] == chat_id
        ]


class PureRulesTest(unittest.TestCase):
    def test_deadline_rules(self):
        event = date(2026, 10, 18)
        monday = datetime(2026, 10, 12, 9, 0, tzinfo=TZ)
        self.assertEqual(poll_deadline(monday, event, TZ), monday + timedelta(hours=12))
        saturday = datetime(2026, 10, 17, 2, 0, tzinfo=TZ)
        self.assertEqual(
            poll_deadline(saturday, event, TZ),
            datetime(2026, 10, 17, 12, 0, tzinfo=TZ),
        )
        evening = datetime(2026, 10, 17, 20, 0, tzinfo=TZ)
        self.assertEqual(poll_deadline(evening, event, TZ), evening + timedelta(hours=1))

    def test_apply_replacements(self):
        assignments = {
            "звук": {"names": ["Алексей", "Вера"], "is_informational": False},
            "заметка": {"names": ["Алексей"], "is_informational": True},
        }
        result = apply_replacements(assignments, [
            {"task": "звук", "old_name": "Алексей", "new_name": "Борис"},
            {"task": "звук", "old_name": "Борис", "new_name": "Глеб"},
            {"task": "заметка", "old_name": "Алексей", "new_name": "Борис"},
        ])
        self.assertEqual(result["звук"]["names"], ["Глеб", "Вера"])
        self.assertEqual(result["заметка"]["names"], ["Алексей"])
        self.assertEqual(assignments["звук"]["names"], ["Алексей", "Вера"])
        # Заменённого в графике уже нет — замена всё равно действует.
        moved = apply_replacements(
            {"звук": ["Вера"]},
            [{"task": "звук", "old_name": "Алексей", "new_name": "Борис"}],
        )
        self.assertEqual(moved["звук"], ["Вера", "Борис"])

    def test_already_replaced_follows_chain(self):
        from bot.presentation import is_replaced as _already_replaced

        chain = [
            {"task": "звук", "old_name": "А", "new_name": "Б"},
            {"task": "звук", "old_name": "Б", "new_name": "А"},
        ]
        self.assertFalse(_already_replaced(chain, "звук", "А"))
        self.assertTrue(_already_replaced(chain[:1], "звук", "А"))
        self.assertFalse(_already_replaced(chain[:1], "сборка", "А"))
        back = [
            {"task": "звук", "old_name": "А", "new_name": "Б"},
            {"task": "звук", "old_name": "В", "new_name": "А"},
        ]
        self.assertFalse(_already_replaced(back, "звук", "А"))

    def test_parsers(self):
        today = date(2026, 10, 21)
        self.assertEqual(
            parse_change_args("звук @alex", today), ("звук", "alex", None)
        )
        self.assertEqual(
            parse_change_args("свет и сцена @alex 25.10", today),
            ("свет и сцена", "alex", date(2026, 10, 25)),
        )
        self.assertEqual(
            parse_change_args("звук @alex 04.01", today)[2], date(2027, 1, 4)
        )
        self.assertEqual(
            parse_change_args("звук @alex 04.01.27", today)[2], date(2027, 1, 4)
        )
        for wrong in ("", "звук", "@alex", "звук alex", "звук @",
                      "звук @alex 32.10", "звук @alex 1.1 x"):
            with self.assertRaises(ValueError):
                parse_change_args(wrong)
        self.assertTrue(poll_answer_choice([0]))
        self.assertFalse(poll_answer_choice([1]))
        self.assertIsNone(poll_answer_choice([]))


class RegistryTest(Base):
    def test_poll_answers_and_single_close(self):
        self.registry.create_replacement_poll(
            "p1", "sound", EVENT.isoformat(), "звук", "Алексей", OLD,
            -1005, 201, 100, "2099-01-01T00:00:00+00:00",
        )
        self.assertTrue(self.registry.record_poll_answer("p1", 7, "a", "A", True))
        self.registry.record_poll_answer("p1", 8, "b", "B", False)
        self.registry.record_poll_answer("p1", 8, "b", "B", None)
        self.assertEqual(
            [(item["telegram_id"], item["can"]) for item in
             self.registry.list_poll_answers("p1")],
            [(7, True)],
        )
        self.assertEqual(
            self.registry.replacement_poll_by_message(-1005, 100)["poll_id"], "p1"
        )
        self.assertTrue(self.registry.close_replacement_poll("p1"))
        self.assertFalse(self.registry.close_replacement_poll("p1"))
        self.assertFalse(self.registry.record_poll_answer("p1", 9, "", "", True))

    def test_replacement_creates_contact_and_lookup(self):
        self.registry.set_replacement(
            "sound", EVENT.isoformat(), "звук", "Алексей", "Гость", OUTSIDER,
            "@guest_user", "poll", None,
        )
        # Участник чата не из состава — только по имени, без привязки.
        self.assertIsNone(self.registry.service_person("sound", OUTSIDER, "звук"))
        self.registry.set_responsible_contact(
            SUPER, "sound", "сборка", "Борис", MEMBER, "boris_b"
        )
        self.registry.set_replacement(
            "sound", EVENT.isoformat(), "звук", "Вера", "Борис", MEMBER,
            "boris_b", "admin", ADMIN,
        )
        self.assertIsNotNone(
            self.registry.service_person("sound", MEMBER, "звук")
        )
        self.assertEqual(
            self.registry.find_service_person_by_username("sound", "@BORIS_B")["telegram_id"],
            MEMBER,
        )
        self.assertIn(MEMBER, self.registry.task_member_ids("sound", "звук"))
        self.assertEqual(
            self.registry.list_replacements("sound", EVENT.isoformat())[0]["new_name"],
            "Гость",
        )


class SearchFlowTest(Base):
    def test_start_sends_notice_and_poll_once(self):
        poll = self.start()
        self.assertEqual(poll["status"], "open")
        self.assertEqual(poll["notice_message_id"], 100)
        notice = self.sent_to(-1005)[0]
        self.assertIn('<a href="tg://user?id=4004">Алексей</a>', notice)
        self.assertNotIn("@alex_one", notice)
        kwargs = self.bot.send_poll.await_args.kwargs
        self.assertEqual(kwargs["options"], ["Могу", "Не смогу"])
        self.assertFalse(kwargs["is_anonymous"])
        self.assertIn("«звук»", self.bot.send_poll.await_args.args[1])
        self.schedule_close.assert_called_once()
        self.assertIn("/change звук @username", self.sent_to(ADMIN)[0])
        self.assertIsNone(self.start())
        self.assertEqual(self.bot.send_poll.await_count, 1)

    def test_without_group_chat_request_waits_for_change(self):
        self.registry.set_group_chat(SUPER, "sound", None, enabled=False)
        request = self.start()
        self.assertEqual((request["status"], request["filled"]), ("closed", 0))
        self.bot.send_poll.assert_not_awaited()
        self.assertIn("Чат служения не привязан", self.sent_to(ADMIN)[0])
        self.assertIsNone(self.start())
        substitute = {"name": "Борис", "telegram_id": MEMBER, "username": ""}
        self.assertTrue(self.finish(request, substitute))
        self.bot.stop_poll.assert_not_awaited()
        self.assertFalse(self.finish(request, substitute))
        self.assertIsNone(self.registry.find_active_replacement_request(
            "sound", EVENT.isoformat(), "звук", "Алексей"
        ))
        self.assertEqual(
            self.registry.list_replacements("sound", EVENT.isoformat())[0]["new_name"],
            "Борис",
        )

    def test_poll_failure_and_storage_failure(self):
        self.bot.send_poll.side_effect = RuntimeError("no rights")
        request = self.start()
        self.assertTrue(request["poll_id"].startswith("manual-"))
        self.assertIn("отправить не удалось", self.sent_to(ADMIN)[-1])

    def test_storage_failure_after_poll_sent_stops_poll(self):
        with patch.object(
            self.registry, "create_replacement_poll",
            side_effect=[RuntimeError("disk"), None],
        ):
            self.assertIsNone(self.start())
        self.bot.stop_poll.assert_awaited_once()
        self.assertIn("не запущен из-за ошибки", self.sent_to(ADMIN)[-1])

    def test_concurrent_declines_start_one_poll(self):
        async def both():
            args = (
                self.bot, self.registry, "sound", EVENT, "звук", "Алексей", OLD,
            )
            kwargs = dict(
                admin_actor_id=SUPER, tz=TZ, logger=MagicMock(),
                schedule_close=self.schedule_close,
                now=datetime(2098, 12, 29, 10, 0, tzinfo=timezone.utc),
            )
            return await asyncio.gather(
                start_replacement_search(*args, **kwargs),
                start_replacement_search(*args, **kwargs),
            )

        first, second = asyncio.run(both())
        self.assertEqual(first["status"], "open")
        self.assertIsNone(second)
        self.assertEqual(self.bot.send_poll.await_count, 1)

    def test_timer_failure_keeps_poll_open(self):
        self.schedule_close.side_effect = RuntimeError("scheduler")
        request = self.start()
        self.assertEqual((request["status"], request["filled"]), ("open", 0))
        self.bot.stop_poll.assert_not_awaited()
        self.assertIn("Таймер итога не поставлен", self.sent_to(ADMIN)[-1])
        # При запуске открытый опрос снова ставится на таймер.
        self.assertEqual(len(self.registry.list_open_replacement_polls()), 1)
        substitute = {"name": "Борис", "telegram_id": MEMBER, "username": ""}
        self.assertTrue(self.finish(request, substitute))

    def test_failed_stop_after_storage_failure_is_reported(self):
        self.bot.stop_poll.side_effect = RuntimeError("no rights")
        with patch.object(
            self.registry, "create_replacement_poll",
            side_effect=RuntimeError("disk"),
        ):
            self.assertIsNone(self.start())
        self.assertIn("остановите его вручную", self.sent_to(ADMIN)[-1])

    def test_task_member_wins_over_earlier_outsider(self):
        poll = self.start()
        self.registry.record_poll_answer(poll["poll_id"], OLD, "alex_one", "Алексей", True)
        self.registry.record_poll_answer(poll["poll_id"], OUTSIDER, "guest_user", "Гость Г", True)
        self.registry.record_poll_answer(poll["poll_id"], MEMBER, "boris_b", "Борис Б", True)
        self.bot.send_message.reset_mock()
        self.assertTrue(self.finish(poll))
        self.assertFalse(self.finish(poll))
        self.bot.stop_poll.assert_awaited_once_with(-1005, poll["message_id"])
        self.assertEqual(
            self.registry.list_replacements("sound", EVENT.isoformat())[0]["new_name"],
            "Борис",
        )
        announcement = self.sent_to(-1005)[0]
        self.assertIn('<a href="tg://user?id=5005">Борис</a>', announcement)
        self.assertIn('<a href="tg://user?id=4004">Алексей</a>', announcement)
        self.assertNotIn("@boris_b", announcement)
        self.assertIn("Вы назначены", self.sent_to(MEMBER)[0])
        self.assertEqual(
            self.registry.get_confirmation("sound", EVENT.isoformat(), "звук", MEMBER),
            "confirmed",
        )

    def test_outsider_used_when_no_task_member(self):
        poll = self.start()
        self.registry.record_poll_answer(poll["poll_id"], OUTSIDER, "guest_user", "Гость Г", True)
        self.finish(poll)
        self.assertEqual(
            self.registry.list_replacements("sound", EVENT.isoformat())[0]["new_name"],
            "Гость Г",
        )

    def test_failed_assignment_tells_admins(self):
        poll = self.start()
        self.bot.send_message.reset_mock()
        with patch.object(
            self.registry, "set_replacement", side_effect=ValueError("x")
        ), self.assertRaises(ValueError):
            self.finish(poll, {"name": "Борис", "telegram_id": MEMBER, "username": ""})
        self.assertIn("Не удалось назначить замену", self.sent_to(ADMIN)[0])

    def test_votes_do_not_trigger_backup_refresh(self):
        poll = self.start()
        self.registry.on_change = MagicMock()
        self.registry.record_poll_answer(poll["poll_id"], MEMBER, "", "", True)
        self.registry.on_change.assert_not_called()

    def test_no_volunteers_tells_admins_and_change_still_works(self):
        poll = self.start()
        self.bot.send_message.reset_mock()
        self.finish(poll)
        self.assertFalse(self.finish(poll))
        self.assertEqual(self.registry.list_replacements("sound", EVENT.isoformat()), [])
        self.assertIn("Замена не найдена", self.sent_to(ADMIN)[0])
        self.assertTrue(self.finish(
            poll, {"name": "Борис", "telegram_id": MEMBER, "username": ""}
        ))
        self.assertEqual(self.bot.stop_poll.await_count, 1)

    def test_report_follows_changed_replacement(self):
        from bot.admin_report import send_admin_reports

        raw = {"звук": {"names": ["Алексей"], "is_informational": False}}
        self.registry.set_replacement(
            "sound", EVENT.isoformat(), "звук", "Алексей", "Борис", MEMBER,
            "", "admin", ADMIN,
        )
        service = self.registry.get_service("sound")
        asyncio.run(send_admin_reports(
            self.bot, self.registry, service, EVENT, raw, SUPER, MagicMock(), 4096
        ))
        self.registry.set_replacement(
            "sound", EVENT.isoformat(), "звук", "Алексей", "Глеб", None,
            "", "admin", ADMIN,
        )
        asyncio.run(send_admin_reports(
            self.bot, self.registry, service, EVENT, raw, SUPER, MagicMock(), 4096
        ))
        text = self.bot.edit_message_text.await_args.args[0]
        self.assertIn("Глеб", text)
        self.assertNotIn("Борис", text)


class HandlersTest(Base):
    def setUp(self):
        super().setUp()
        self.context = SimpleNamespace(
            interaction_logger=MagicMock(), debug_logger=MagicMock(),
            admin_id=SUPER, group_chat_id=-100, timezone=TZ,
            service_registry=self.registry, bot=self.bot,
            load_upcoming_service_events=AsyncMock(return_value=[]),
        )
        dispatcher = SimpleNamespace(include_router=MagicMock())
        register(dispatcher, self.context)
        routers = [call.args[0] for call in dispatcher.include_router.call_args_list]
        self.routers = routers
        self.change = next(
            item.callback for item in routers[0].message.handlers
            if item.callback.__name__ == "on_change_command"
        )
        self.poll_answer = [
            item.callback for item in routers[1].poll_answer.handlers
            if item.callback.__name__ == "on_replacement_poll_answer"
        ][-1]

    def message(self, user_id, text, chat_id=None, reply_to=None):
        chat_id = chat_id or user_id
        return SimpleNamespace(
            from_user=SimpleNamespace(id=user_id),
            chat=SimpleNamespace(
                id=chat_id, type="private" if chat_id > 0 else "supergroup"
            ),
            text=text, reply_to_message=reply_to, answer=AsyncMock(),
        )

    def test_hook_starts_search_and_poll_answers_are_recorded(self):
        asyncio.run(self.context.on_assignment_declined(
            "sound", EVENT, "звук", "Алексей", telegram_id=OLD
        ))
        poll = self.registry.list_open_replacement_polls()[0]
        asyncio.run(self.poll_answer(SimpleNamespace(
            poll_id=poll["poll_id"], option_ids=[0],
            user=SimpleNamespace(
                id=MEMBER, is_bot=False, username="boris_b",
                first_name="Борис", last_name=None,
            ),
        )))
        self.assertEqual(
            self.registry.list_poll_answers(poll["poll_id"])[0]["telegram_id"],
            MEMBER,
        )
        asyncio.run(self.context.close_replacement_poll(poll["poll_id"]))
        self.assertEqual(self.registry.list_open_replacement_polls(), [])

    def test_stale_decline_button_is_rejected(self):
        """«Не смогу» в старом напоминании: в графике уже другой человек."""
        answer = [
            item.callback for router in self.routers
            for item in router.callback_query.handlers
            if item.callback.__name__ == "on_personal_answer"
        ][-1]
        self.context.on_assignment_declined = AsyncMock()

        def press():
            cb = SimpleNamespace(
                from_user=SimpleNamespace(id=OLD),
                data=f"pcf:d:sound:{EVENT:%Y%m%d}:{task_token('звук')}",
                answer=AsyncMock(), message=None,
            )
            asyncio.run(answer(cb))
            return cb.answer.await_args.args[0]

        self.context.load_upcoming_service_events.return_value = [
            SimpleNamespace(
                service_id="sound", event_date=EVENT,
                assignments=[SimpleNamespace(task="звук", name="Борис")],
            )
        ]
        self.assertIn("устарело", press())
        self.assertEqual(
            self.registry.list_confirmations("sound", EVENT.isoformat()), {}
        )
        self.context.on_assignment_declined.assert_not_awaited()

        self.context.load_upcoming_service_events.return_value[0].assignments = [
            SimpleNamespace(task="звук", name="Алексей")
        ]
        self.assertEqual(press(), "Ответ сохранён")
        self.context.on_assignment_declined.assert_awaited_once()

    def test_substitute_linked_under_other_name_can_answer(self):
        """Заменяющий привязан к задаче под другим именем — ответ принимается."""
        answer = [
            item.callback for router in self.routers
            for item in router.callback_query.handlers
            if item.callback.__name__ == "on_personal_answer"
        ][-1]
        self.registry.set_replacement(
            "sound", EVENT.isoformat(), "звук", "Алексей", "Борис Б.",
            MEMBER, "boris_b", "admin", SUPER,
        )
        self.context.load_upcoming_service_events.return_value = [
            SimpleNamespace(
                service_id="sound", event_date=EVENT,
                assignments=[SimpleNamespace(task="звук", name="Борис Б.")],
            )
        ]
        cb = SimpleNamespace(
            from_user=SimpleNamespace(id=MEMBER),
            data=f"pcf:c:sound:{EVENT:%Y%m%d}:{task_token('звук')}",
            answer=AsyncMock(), message=None,
        )
        asyncio.run(answer(cb))
        self.assertEqual(cb.answer.await_args.args[0], "Спасибо!")

        # Отказ заменяющего ищет замену имени из графика, не имени контакта.
        self.context.on_assignment_declined = AsyncMock()
        cb.data = f"pcf:d:sound:{EVENT:%Y%m%d}:{task_token('звук')}"
        cb.answer = AsyncMock()
        asyncio.run(answer(cb))
        self.assertEqual(
            self.context.on_assignment_declined.await_args.args[3], "Борис Б."
        )

        # Цепочка: заменяющего самого заменили — его кнопка устарела.
        self.registry.set_replacement(
            "sound", EVENT.isoformat(), "звук", "Борис Б.", "Алексей",
            OLD, "alex_one", "admin", SUPER,
        )
        self.context.load_upcoming_service_events.return_value[0].assignments = [
            SimpleNamespace(task="звук", name="Алексей")
        ]
        cb.answer = AsyncMock()
        asyncio.run(answer(cb))
        self.assertIn("устарело", cb.answer.await_args.args[0])

    def test_change_in_group_finds_request_without_poll(self):
        self.bot.send_poll.side_effect = RuntimeError("no rights")
        self.start()
        done = self.message(ADMIN, "/change звук @boris_b", -1005)
        asyncio.run(self.change(done))
        self.assertIn("✅ Замена назначена", done.answer.await_args.args[0])

    def test_change_command(self):
        none = self.message(ADMIN, "/change звук @boris_b")
        asyncio.run(self.change(none))
        self.assertIn("нет задачи «звук»", none.answer.await_args.args[0])

        poll = self.start()
        stranger = self.message(OUTSIDER, "/change звук @boris_b")
        asyncio.run(self.change(stranger))
        refusal = stranger.answer.await_args.args[0]
        self.assertIn("только админ служения", refusal)
        self.assertIn("@boss_admin", refusal)

        unknown = self.message(ADMIN, "/change звук @nobody")
        asyncio.run(self.change(unknown))
        self.assertIn("не найден", unknown.answer.await_args.args[0])

        self.context.cancel_replacement_close = MagicMock()
        reply = SimpleNamespace(message_id=poll["message_id"])
        done = self.message(ADMIN, "/change звук @Boris_B", -1005, reply)
        asyncio.run(self.change(done))
        self.assertIn("✅ Замена назначена", done.answer.await_args.args[0])
        replacement = self.registry.list_replacements("sound", EVENT.isoformat())[0]
        self.assertEqual(
            (replacement["new_name"], replacement["source"]), ("Борис", "admin")
        )
        self.assertEqual(self.registry.list_open_replacement_polls(), [])
        self.context.cancel_replacement_close.assert_called_once_with(
            poll["poll_id"]
        )
        again = self.message(ADMIN, "/change звук @boris_b", -1005, reply)
        asyncio.run(self.change(again))
        self.assertIn("уже завершён", again.answer.await_args.args[0])


class DirectChangeTest(HandlersTest):
    """/change и экран «🔁 Замена» без отказа участника (этап 3.4)."""

    def setUp(self):
        super().setUp()
        from bot.excel_parser import ServiceAssignment, ServiceEvent

        today = datetime.now(TZ).date()
        self.day = today + timedelta(days=3)
        self.events = [
            ServiceEvent("sound", self.day, (
                ServiceAssignment("звук", "Алексей"),
                ServiceAssignment("сборка", "Алексей"),
                ServiceAssignment("спевка", "Вера"),
                ServiceAssignment("спевка", "Глеб"),
            )),
        ]
        self.context.load_upcoming_service_events.return_value = self.events
        self.registry.set_responsible_contact(
            SUPER, "sound", "сборка", "Борис", MEMBER, "boris_b"
        )

    def test_change_command(self):
        self.skipTest("сценарий без графика проверяется в HandlersTest")

    def test_change_without_decline(self):
        done = self.message(ADMIN, f"/change звук @boris_b {self.day:%d.%m}")
        asyncio.run(self.change(done))
        self.assertIn("✅ Замена назначена", done.answer.await_args.args[0])
        row = self.registry.list_replacements("sound", self.day.isoformat())[0]
        self.assertEqual((row["old_name"], row["new_name"]), ("Алексей", "Борис"))
        announcement = self.sent_to(-1005)[0]
        self.assertIn('<a href="tg://user?id=5005">Борис</a>', announcement)
        self.assertIn('<a href="tg://user?id=4004">Алексей</a>', announcement)
        self.assertNotIn("@boris_b", announcement)

        many = self.message(ADMIN, "/change спевка @boris_b")
        asyncio.run(self.change(many))
        self.assertIn("несколько человек", many.answer.await_args.args[0])

        stranger = self.message(OUTSIDER, "/change звук @boris_b")
        asyncio.run(self.change(stranger))
        self.assertIn("только админ служения", stranger.answer.await_args.args[0])

    def _callback(self, button):
        return SimpleNamespace(
            data=button.callback_data,
            from_user=SimpleNamespace(id=ADMIN),
            message=SimpleNamespace(
                chat=SimpleNamespace(type="private", id=ADMIN),
                answer=AsyncMock(return_value=SimpleNamespace(message_id=1)),
            ),
            answer=AsyncMock(),
        )

    def _handler(self, name):
        return next(
            item.callback for item in self.routers[0].callback_query.handlers
            if item.callback.__name__ == name
        )

    async def _press(self, message, label):
        markup = message.answer.await_args.kwargs["reply_markup"]
        button = next(
            item for row in markup.inline_keyboard for item in row
            if label in item.text
        )
        callback = self._callback(button)
        await self._handler("on_replace_action")(callback)
        return callback.message

    def test_replace_screen_flow(self):
        """Решение 17, 5в: неделя → задача (с датой и людьми) → кто
        заменит; над кнопками — график месяца; внизу — к неделе и к
        графику."""
        async def run():
            card = self._callback(SimpleNamespace(
                callback_data="service:service_replace:sound"
            ))
            await self._handler("on_service_selected")(card)
            weeks = card.message
            self.assertIn("Выберите неделю", weeks.answer.await_args.args[0])
            week = await self._press(weeks, "нед.")
            text = week.answer.await_args.args[0]
            self.assertIn("📋 График на", text)
            self.assertIn(f"{self.day:%d.%m}:", text)
            self.assertIn("🧰 сборка — Алексей", text)
            self.assertIn("🎶 спевка — Вера, Глеб", text)
            labels = [
                item.text for row in
                week.answer.await_args.kwargs["reply_markup"].inline_keyboard
                for item in row
            ]
            self.assertEqual(labels[-2:], ["⬅️ К неделям", "📅 К графику"])
            # В задаче двое — выбор, кого заменить.
            people = await self._press(week, "спевка ·")
            self.assertIn("Кого нужно заменить", people.answer.await_args.args[0])
            back = await self._press(people, "К неделе")
            candidates = await self._press(back, "сборка ·")
            labels = [
                item.text for row in
                candidates.answer.await_args.kwargs["reply_markup"].inline_keyboard
                for item in row
            ]
            self.assertIn("✅ Борис", labels)
            self.assertIn("👤 Выбрать пользователя Telegram", labels)
            self.assertEqual(labels[-2:], ["⬅️ К неделе", "📅 К графику"])
            self.assertIn("📋 График на", candidates.answer.await_args.args[0])
            self.assertIn(
                "Вместо: Алексей\n\nℹ️",
                candidates.answer.await_args.args[0],
            )
            confirm = await self._press(candidates, "Борис")
            confirm_text = confirm.answer.await_args.args[0]
            self.assertIn(
                "Алексей → Борис (@boris_b)\n\nℹ️ Замена действует",
                confirm_text,
            )
            self.assertIn("📋 График на", confirm_text)
            confirm_rows = confirm.answer.await_args.kwargs[
                "reply_markup"
            ].inline_keyboard
            self.assertEqual(
                [button.text for button in confirm_rows[-1]],
                ["⬅️ К неделе", "📅 К графику"],
            )
            result = await self._press(confirm, "Назначить")
            # После замены — снова неделя, с отметкой.
            text = result.answer.await_args.args[0]
            self.assertIn("✅ Замена назначена", text)
            self.assertIn("\n\n✅ Замена назначена", text)
            self.assertIn("\n\n📋 График на", text)
            self.assertIn("Неделя", text)
            row = self.registry.list_replacements("sound", self.day.isoformat())[0]
            self.assertEqual((row["task"], row["new_name"]), ("сборка", "Борис"))

        asyncio.run(run())

    def test_saturday_event_joins_its_week(self):
        """Доп. событие в субботу — в одной неделе со своим воскресеньем."""
        from bot.excel_parser import ServiceAssignment, ServiceEvent

        today = datetime.now(TZ).date()
        sunday = today + timedelta(days=(6 - today.weekday()) % 7 + 7)
        self.events[:] = [
            ServiceEvent("sound", sunday - timedelta(days=1), (
                ServiceAssignment("сборка", "Алексей"),
            )),
            ServiceEvent("sound", sunday, (
                ServiceAssignment("звук", "Алексей"),
            )),
        ]

        async def run():
            card = self._callback(SimpleNamespace(
                callback_data="service:service_replace:sound"
            ))
            await self._handler("on_service_selected")(card)
            labels = [
                item.text for row in
                card.message.answer.await_args.kwargs["reply_markup"].inline_keyboard
                for item in row if "нед." in item.text
            ]
            monday = sunday - timedelta(days=6)
            self.assertEqual(labels, [
                f"🗓 {monday.isocalendar().week} нед. "
                f"{monday:%d.%m}–{sunday:%d.%m}"
            ])

        asyncio.run(run())

    def test_many_candidates_are_paged(self):
        """Ревью greptile к PR #90: много заменяющих — листание, нижние
        кнопки на странице остаются (лимит Telegram — 100 кнопок)."""
        from bot.excel_parser import RosterMember

        self.registry.sync_service_rosters({"sound": [
            RosterMember("Алексей", ("звук", "сборка")),
            *(RosterMember(f"Сборщик {i:03d}", ("сборка",)) for i in range(120)),
        ]})

        async def run():
            card = self._callback(SimpleNamespace(
                callback_data="service:service_replace:sound"
            ))
            await self._handler("on_service_selected")(card)
            week = await self._press(card.message, "нед.")
            candidates = await self._press(week, "сборка ·")
            rows = candidates.answer.await_args.kwargs[
                "reply_markup"
            ].inline_keyboard
            labels = [item.text for row in rows for item in row]
            self.assertLessEqual(len(labels), 100)
            self.assertTrue(any("Далее" in label for label in labels))
            self.assertIn("⬅️ К неделе", labels)
            self.assertIn("📅 К графику", labels)
            self.assertIn("👤 Выбрать пользователя Telegram", labels)
            # Листание выше выхода с экрана.
            nav = next(i for i, row in enumerate(rows)
                       if any("Далее" in item.text for item in row))
            back = next(i for i, row in enumerate(rows)
                        if any(item.text == "⬅️ К неделе" for item in row))
            self.assertLess(nav, back)
            first_page_candidates = {
                item.text for row in rows for item in row
                if "Сборщик" in item.text
            }
            next_button = next(
                item for row in rows for item in row
                if item.callback_data.startswith("ui_page:")
                and "Далее" in item.text
            )
            callback = self._callback(next_button)
            await self._handler("on_dialog_page")(callback)
            next_rows = callback.message.answer.await_args.kwargs[
                "reply_markup"
            ].inline_keyboard
            next_page_candidates = {
                item.text for row in next_rows for item in row
                if "Сборщик" in item.text
            }
            self.assertEqual(len(next_page_candidates), 6)
            self.assertTrue(first_page_candidates.isdisjoint(next_page_candidates))
            self.assertTrue(any("Далее" in item.text
                                for row in next_rows for item in row))

        asyncio.run(run())

    def test_telegram_picker_shows_graph_and_navigation(self):
        async def run():
            card = self._callback(SimpleNamespace(
                callback_data="service:service_replace:sound"
            ))
            await self._handler("on_service_selected")(card)
            week = await self._press(card.message, "нед.")
            candidates = await self._press(week, "сборка ·")
            picker = await self._press(candidates, "Выбрать пользователя Telegram")

            self.assertIn("📋 График на", picker.answer.await_args.args[0])
            keyboard = picker.answer.await_args.kwargs["reply_markup"].keyboard
            self.assertEqual(
                [button.text for button in keyboard[-1]],
                ["⬅️ К неделе", "📅 К графику"],
            )

        asyncio.run(run())

    def test_month_view_keeps_past_tasks(self):
        """График над кнопками — весь месяц, кнопки — только с сегодня."""
        from bot.excel_parser import ServiceAssignment, ServiceEvent

        today = datetime.now(TZ).date()
        if today.day == 1 or today.weekday() == 0:
            self.skipTest("вчера — другая неделя или другой месяц")
        past = today - timedelta(days=1)
        sunday = today + timedelta(days=(6 - today.weekday()) % 7)
        # Вчера и воскресенье — одна неделя.
        self.events[:] = [
            ServiceEvent("sound", past, (ServiceAssignment("звук", "Прошлый"),)),
            ServiceEvent("sound", sunday, (ServiceAssignment("звук", "Алексей"),)),
        ]

        async def run():
            card = self._callback(SimpleNamespace(
                callback_data="service:service_replace:sound"
            ))
            await self._handler("on_service_selected")(card)
            labels = [
                item.text for row in
                card.message.answer.await_args.kwargs["reply_markup"].inline_keyboard
                for item in row
            ]
            self.assertFalse(any(f"–{past:%d.%m}" in l for l in labels))
            week = await self._press(card.message, "нед.")
            self.assertIn("Прошлый", week.answer.await_args.args[0])
            self.assertNotIn(
                "Прошлый",
                str(week.answer.await_args.kwargs["reply_markup"]),
            )

        asyncio.run(run())

    def test_replace_screen_closes_open_request(self):
        async def run():
            await self.context.on_assignment_declined(
                "sound", self.day, "звук", "Алексей", telegram_id=OLD
            )
            self.assertEqual(len(self.registry.list_open_replacement_polls()), 1)
            done = self.message(ADMIN, "/change звук @boris_b")
            await self.change(done)
            self.assertEqual(self.registry.list_open_replacement_polls(), [])
            self.bot.stop_poll.assert_awaited_once()

        asyncio.run(run())

    def test_undated_change_skips_date_with_several_people(self):
        from bot.excel_parser import ServiceAssignment, ServiceEvent

        later = self.day + timedelta(days=7)
        self.events.append(ServiceEvent("sound", later, (
            ServiceAssignment("спевка", "Вера"),
        )))
        done = self.message(ADMIN, "/change спевка @boris_b")
        asyncio.run(self.change(done))
        self.assertIn("✅ Замена назначена", done.answer.await_args.args[0])
        self.assertEqual(
            self.registry.list_replacements("sound", later.isoformat())[0]["old_name"],
            "Вера",
        )
        kwargs = self.context.load_upcoming_service_events.await_args.kwargs
        self.assertIn("until", kwargs)

    def test_stale_screen_does_not_override_replacement(self):
        async def run():
            card = self._callback(SimpleNamespace(
                callback_data="service:service_replace:sound"
            ))
            await self._handler("on_service_selected")(card)
            week = await self._press(card.message, "нед.")
            candidates = await self._press(week, "сборка ·")
            confirm = await self._press(candidates, "Борис")
            # Пока экран открыт, замену назначили иначе.
            self.registry.set_replacement(
                "sound", self.day.isoformat(), "сборка", "Алексей", "Глеб",
                None, "", "admin", SUPER,
            )
            result = await self._press(confirm, "Назначить")
            self.assertIn("уже назначили", result.answer.await_args.args[0])
            rows = self.registry.list_replacements("sound", self.day.isoformat())
            self.assertEqual([row["new_name"] for row in rows], ["Глеб"])

        asyncio.run(run())

    def test_failed_announcement_warns_acting_admin(self):
        async def send_message(chat_id, text, **kwargs):
            if chat_id < 0:
                raise RuntimeError("chat gone")
            return SimpleNamespace(message_id=1)

        self.bot.send_message.side_effect = send_message
        done = self.message(ADMIN, f"/change звук @boris_b {self.day:%d.%m}")
        asyncio.run(self.change(done))
        self.assertIn("✅ Замена назначена", done.answer.await_args.args[0])
        warnings = [text for text in self.sent_to(ADMIN) if "не дошло" in text]
        self.assertEqual(len(warnings), 1)
        self.assertIn("чат служения", warnings[0])

    def test_change_rejects_busy_substitute_and_mismatched_reply(self):
        self.registry.set_responsible_contact(
            SUPER, "sound", "спевка", "Глеб", OUTSIDER, "gleb_g"
        )
        busy = self.message(ADMIN, f"/change спевка @gleb_g {self.day:%d.%m}")
        asyncio.run(self.change(busy))
        # В задаче двое — сначала выбор в «🔁 Замена».
        self.assertIn("несколько человек", busy.answer.await_args.args[0])
        self.registry.set_responsible_contact(
            SUPER, "sound", "звук", "Глеб", OUTSIDER, "gleb_g"
        )
        from bot.excel_parser import ServiceAssignment, ServiceEvent

        self.events[0] = ServiceEvent("sound", self.day, (
            ServiceAssignment("звук", "Алексей"),
            ServiceAssignment("звук", "Глеб"),
        ))
        asyncio.run(self.context.on_assignment_declined(
            "sound", self.day, "звук", "Алексей", telegram_id=OLD
        ))
        poll = self.registry.list_open_replacement_polls()[0]
        other = self.day + timedelta(days=7)
        mismatch = self.message(
            ADMIN, f"/change звук @boris_b {other:%d.%m}", -1005,
            SimpleNamespace(message_id=poll["message_id"]),
        )
        asyncio.run(self.change(mismatch))
        self.assertIn("Этот опрос — на", mismatch.answer.await_args.args[0])
        taken = self.message(
            ADMIN, "/change звук @gleb_g", -1005,
            SimpleNamespace(message_id=poll["message_id"]),
        )
        asyncio.run(self.change(taken))
        self.assertIn("уже в задаче", taken.answer.await_args.args[0])

    def test_foreign_request_does_not_block_own_service(self):
        self.registry.create_replacement_poll(
            "foreign", "broadcast", self.day.isoformat(), "звук", "Кто-то",
            None, 0, 0, None, "2099-01-01T00:00:00+00:00", status="closed",
        )
        done = self.message(ADMIN, "/change звук @boris_b")
        asyncio.run(self.change(done))
        self.assertIn("✅ Замена назначена", done.answer.await_args.args[0])

    def test_repeat_decline_after_replacement_starts_nothing(self):
        self.registry.set_replacement(
            "sound", self.day.isoformat(), "звук", "Алексей", "Борис", MEMBER,
            "", "admin", ADMIN,
        )
        asyncio.run(self.context.on_assignment_declined(
            "sound", self.day, "звук", "Алексей", telegram_id=OLD
        ))
        self.bot.send_poll.assert_not_awaited()


class MigrationTest(unittest.TestCase):
    def test_filled_backfilled_for_existing_replacements(self):
        import sqlite3

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "r.sqlite3"
            registry = ServiceRegistry(path)
            registry.initialize(SUPER, -100)
            for poll_id in ("done", "waiting"):
                registry.create_replacement_poll(
                    poll_id, "sound", EVENT.isoformat(), "звук",
                    "Алексей" if poll_id == "done" else "Вера", None, -1, 1,
                    None, "2099-01-01T00:00:00+00:00", status="closed",
                )
            registry.set_replacement(
                "sound", EVENT.isoformat(), "звук", "Алексей", "Борис", None,
                "", "admin", SUPER,
            )
            # База до появления колонки filled.
            with sqlite3.connect(path) as connection:
                connection.execute("ALTER TABLE replacement_polls DROP COLUMN filled")
            ServiceRegistry(path).initialize(SUPER, -100)
            self.assertEqual(
                [item["poll_id"] for item in
                 registry.list_unfilled_replacement_requests("2000-01-01")],
                ["waiting"],
            )


class LoaderAppliesReplacementTest(Base):
    def test_schedule_loader_uses_replacement(self):
        from bot import app

        self.registry.set_replacement(
            "sound", EVENT.isoformat(), "звук", "Алексей", "Борис", MEMBER,
            "boris_b", "admin", ADMIN,
        )
        event = SimpleNamespace(
            event_date=EVENT, issues=[],
            assignments=[SimpleNamespace(
                task="звук", name="Алексей", is_informational=False
            )],
        )

        async def run():
            with tempfile.TemporaryDirectory() as shm, \
                    patch.object(app, "download_public", new=AsyncMock()), \
                    patch.object(
                        app, "parse_service_events",
                        return_value={"sound": [event]},
                    ), \
                    patch.object(app, "_sync_rosters_and_contacts"):
                return await app._load_service_task_assignments(
                    SimpleNamespace(yandex_public_url="https://example.invalid"),
                    Path(shm), TZ, "sound", MagicMock(), only_date=EVENT,
                    registry=self.registry,
                )

        self.assertEqual(asyncio.run(run())["звук"]["names"], ["Борис"])

    def test_month_wide_loader_and_upcoming_events(self):
        from bot import app
        from bot.excel_parser import ServiceAssignment, ServiceEvent

        self.registry.set_replacement(
            "sound", EVENT.isoformat(), "звук", "Алексей", "Борис", MEMBER,
            "boris_b", "admin", ADMIN,
        )
        events = [
            ServiceEvent("sound", EVENT, (ServiceAssignment("звук", "Алексей"),)),
            ServiceEvent(
                "sound", EVENT + timedelta(days=7),
                (ServiceAssignment("звук", "Алексей"),),
            ),
        ]
        shown = app._apply_event_replacements(events, self.registry)
        self.assertEqual(shown[0].assignments[0].name, "Борис")
        self.assertEqual(shown[1].assignments[0].name, "Алексей")
        self.assertEqual(events[0].assignments[0].name, "Алексей")

        async def run():
            with tempfile.TemporaryDirectory() as shm, \
                    patch.object(app, "download_public", new=AsyncMock()), \
                    patch.object(
                        app, "parse_service_events",
                        return_value={"sound": events},
                    ), \
                    patch.object(app, "_sync_rosters_and_contacts"):
                return await app._load_service_task_assignments(
                    SimpleNamespace(yandex_public_url="https://example.invalid"),
                    Path(shm), TZ, "sound", MagicMock(), registry=self.registry,
                )

        # Без даты — имена за месяц: заменённый не пропадает из списков.
        self.assertEqual(asyncio.run(run())["звук"]["names"], ["Алексей"])



class BotPlanSourceTest(Base):
    """Решение 15: утверждённый график бота главнее Excel."""

    def approve(self, slots):
        month = f"{EVENT:%Y-%m}"
        tasks = [
            {"name": task["name"], "needed": task["needed"], "max": task["max"],
             "implicit": task["implicit"], "day": task.get("day")}
            for task in self.registry.plan_tasks("sound")
        ]
        self.registry.save_schedule_draft(
            "sound", month, run_id=None, dates=[EVENT.isoformat()],
            tasks=tasks, slots=slots, last_served={},
        )
        self.registry.approve_schedule_plan(
            SUPER, "sound", month, slots, chat_id=None, message_id=None,
        )

    def load(self, events):
        from bot import app

        async def run():
            with tempfile.TemporaryDirectory() as shm, \
                    patch.object(app, "download_public", new=AsyncMock()), \
                    patch.object(
                        app, "parse_service_events",
                        return_value={"sound": events},
                    ), \
                    patch.object(app, "_sync_rosters_and_contacts"):
                return await app._load_service_task_assignments(
                    SimpleNamespace(yandex_public_url="https://example.invalid"),
                    Path(shm), TZ, "sound", MagicMock(), only_date=EVENT,
                    return_event_found=True, registry=self.registry,
                )

        return asyncio.run(run())

    def test_reminders_follow_approved_bot_plan(self):
        from bot import app
        from bot.excel_parser import ServiceAssignment, ServiceEvent

        excel = [
            ServiceEvent("sound", EVENT, (ServiceAssignment("звук", "Алексей"),)),
            ServiceEvent("books", EVENT, (ServiceAssignment("книги", "Ольга"),)),
        ]
        # Нет утверждённого графика — как в Excel.
        result, found = self.load(excel[:1])
        self.assertEqual(result["звук"]["names"], ["Алексей"])
        self.assertTrue(found)
        self.approve({EVENT.isoformat(): {"звук": ["Борис"], "сборка": []}})
        result, found = self.load(excel[:1])
        self.assertEqual(result["звук"]["names"], ["Борис"])
        self.assertTrue(found)
        # Замена — поверх графика бота.
        self.registry.set_replacement(
            "sound", EVENT.isoformat(), "звук", "Борис", "Алексей", OLD,
            "alex_one", "admin", ADMIN,
        )
        result, _ = self.load(excel[:1])
        self.assertEqual(result["звук"]["names"], ["Алексей"])
        # Ближайшие недели: события Excel этого служения и месяца заменены,
        # другие служения — как в файле.
        shown = app._apply_bot_plans(
            excel, self.registry, EVENT - timedelta(days=7),
            EVENT + timedelta(days=7),
        )
        self.assertEqual(
            sorted(
                (event.service_id, [a.name for a in event.assignments])
                for event in shown
            ),
            [("books", ["Ольга"]), ("sound", ["Борис"])],
        )
        # Не воскресенье утверждённого месяца, которого нет в графике бота
        # (среда спевки к воскресенью следующего месяца), — из Excel.
        wednesday = ServiceEvent(
            "sound", EVENT + timedelta(days=24),
            (ServiceAssignment("спевка", "Вера"),),
        )
        self.assertEqual(wednesday.event_date.month, EVENT.month)
        self.assertNotEqual(wednesday.event_date.weekday(), 6)
        shown = app._apply_bot_plans(
            [wednesday], self.registry, EVENT, EVENT + timedelta(days=30),
        )
        self.assertIn(wednesday, shown)
        # Пометки Excel остаются при графике бота.
        noted = ServiceEvent("sound", EVENT, (
            ServiceAssignment("звук", "Алексей"),
            ServiceAssignment("2я часть членское", "", True),
        ))
        shown = app._apply_bot_plans(
            [noted], self.registry, EVENT, EVENT,
        )
        self.assertEqual(
            sorted((a.task, a.name) for a in shown[0].assignments),
            [("2я часть членское", ""), ("звук", "Борис")],
        )
        import hashlib
        self.registry.save_schedule_notes(
            hashlib.sha256(b"https://example.invalid").hexdigest(),
            "test-version", [["sound", EVENT.isoformat(), "2я часть членское", ""]],
        )
        result, _ = self.load([noted])
        self.assertTrue(result["2я часть членское"]["is_informational"])

    def test_previous_month_plan_covers_next_month_date(self):
        from bot import app

        # Воскресенье — последний день месяца, задача «вс +1».
        sunday = next(
            EVENT + timedelta(weeks=week) for week in range(60)
            if (EVENT + timedelta(weeks=week, days=1)).day == 1
        )
        self.registry.set_service_task_day(SUPER, "sound", "звук", 1)
        month = f"{sunday:%Y-%m}"
        tasks = [
            {"name": task["name"], "needed": task["needed"], "max": task["max"],
             "implicit": task["implicit"], "day": task.get("day")}
            for task in self.registry.plan_tasks("sound")
        ]
        self.registry.save_schedule_draft(
            "sound", month, run_id=None, dates=[sunday.isoformat()],
            tasks=tasks, slots={sunday.isoformat(): {"звук": ["Борис"]}},
            last_served={},
        )
        self.registry.approve_schedule_plan(
            SUPER, "sound", month, {sunday.isoformat(): {"звук": ["Борис"]}},
            chat_id=None, message_id=None,
        )
        monday = sunday + timedelta(days=1)
        planned, covered = app._bot_plan_events(self.registry, monday, monday)
        self.assertIn(("sound", monday), covered)
        self.assertEqual(
            [
                (event.event_date, [a.name for a in event.assignments])
                for event in planned if event.event_date == monday
            ],
            [(monday, ["Борис"])],
        )

    def test_task_day_of_approved_sunday_replaced_even_when_empty(self):
        from bot import app
        from bot.excel_parser import ServiceAssignment, ServiceEvent

        # Сборка — вс −4; в графике бота сборку на это воскресенье убрали,
        # в Excel она осталась средой — человек не возвращается.
        self.registry.set_service_task_day(SUPER, "sound", "сборка", -4)
        self.approve({EVENT.isoformat(): {"звук": ["Борис"], "сборка": []}})
        wednesday = ServiceEvent(
            "sound", EVENT - timedelta(days=4),
            (ServiceAssignment("сборка", "Вера"),),
        )
        shown = app._apply_bot_plans(
            [wednesday], self.registry, wednesday.event_date, EVENT,
        )
        self.assertNotIn(
            "Вера", [a.name for event in shown for a in event.assignments]
        )

    def test_bot_plan_reminders_without_excel(self):
        from bot import app

        def load_failing():
            async def run():
                with tempfile.TemporaryDirectory() as shm, \
                        patch.object(
                            app, "download_public",
                            new=AsyncMock(side_effect=RuntimeError("нет")),
                        ), \
                        patch.object(app, "_sync_rosters_and_contacts") as sync:
                    result = await app._load_service_task_assignments(
                        SimpleNamespace(
                            yandex_public_url="https://example.invalid"
                        ),
                        Path(shm), TZ, "sound", MagicMock(), only_date=EVENT,
                        return_event_found=True, registry=self.registry,
                    )
                    sync.assert_not_called()
                    return result

            return asyncio.run(run())

        # Без утверждённого графика бота сбой Excel — как раньше, ошибка.
        with self.assertRaises(RuntimeError):
            load_failing()
        # С графиком бота Excel не нужен.
        self.approve({EVENT.isoformat(): {"звук": ["Борис"]}})
        result, found = load_failing()
        self.assertEqual(result["звук"]["names"], ["Борис"])
        self.assertTrue(found)
        # Замена — и без Excel.
        self.registry.set_replacement(
            "sound", EVENT.isoformat(), "звук", "Борис", "Алексей", OLD,
            "alex_one", "admin", ADMIN,
        )
        result, _ = load_failing()
        self.assertEqual(result["звук"]["names"], ["Алексей"])

    def test_approved_tasks_ignore_draft_edits(self):
        from bot import app
        import sqlite3
        from contextlib import closing

        self.approve({EVENT.isoformat(): {"звук": ["Борис"]}})
        month = f"{EVENT:%Y-%m}"

        def moments():
            planned, _ = app._bot_plan_events(
                self.registry, EVENT, EVENT + timedelta(days=1),
            )
            return [
                (event.event_date, [a.name for a in event.assignments])
                for event in planned if event.assignments
            ]

        # Правка рабочей копии (переименование, свой день) — не в напоминаниях.
        db = self.registry.path
        with closing(sqlite3.connect(db)) as connection, connection:
            connection.execute(
                "UPDATE schedule_plans SET tasks_json = ? WHERE month = ?",
                ('[{"name": "звук2", "day": 1}]', month),
            )
        self.assertEqual(moments(), [(EVENT, ["Борис"])])
        # Утверждён до снимка — снимок заполняется при запуске из рабочей
        # копии, дальше она на напоминания не влияет.
        with closing(sqlite3.connect(db)) as connection, connection:
            connection.execute(
                "UPDATE schedule_plans SET approved_tasks_json = NULL, "
                "tasks_json = ? WHERE month = ?",
                ('[{"name": "звук", "day": null}]', month),
            )
        self.registry.initialize(SUPER, -100)
        # Снимок из уже переименованной копии — назначения не теряются.
        with closing(sqlite3.connect(db)) as connection, connection:
            connection.execute(
                "UPDATE schedule_plans SET approved_tasks_json = ? "
                "WHERE month = ?",
                ('[{"name": "звук2", "day": 1}]', month),
            )
        self.assertEqual(moments(), [(EVENT, ["Борис"])])
        with closing(sqlite3.connect(db)) as connection, connection:
            connection.execute(
                "UPDATE schedule_plans SET approved_tasks_json = tasks_json "
                "WHERE month = ?",
                (month,),
            )
        with closing(sqlite3.connect(db)) as connection, connection:
            connection.execute(
                "UPDATE schedule_plans SET tasks_json = ? WHERE month = ?",
                ('[{"name": "звук", "day": 1}]', month),
            )
        self.assertEqual(moments(), [(EVENT, ["Борис"])])
        # День задачи в настройках — и в утверждённом графике (публикуется
        # заново), напоминание — в новый день.
        self.registry.set_service_task_day(SUPER, "sound", "звук", 1)
        self.assertEqual(
            moments(), [(EVENT + timedelta(days=1), ["Борис"])],
        )


if __name__ == "__main__":
    unittest.main()
