import asyncio
import sqlite3
from contextlib import closing
import logging
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from bot import readiness
from bot.excel_parser import RosterMember
from bot.service_registry import ServiceRegistry
from bot.ui import plain_text, render_screen_chunks

TZ = ZoneInfo("Europe/Moscow")
LOG = logging.getLogger("test-readiness")
LOG.addHandler(logging.NullHandler())
LOG.propagate = False


def fake_bot():
    return SimpleNamespace(
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=11)),
        send_poll=AsyncMock(return_value=SimpleNamespace(
            message_id=12, poll=SimpleNamespace(id="poll-1"),
        )),
        stop_poll=AsyncMock(),
    )


class ReadinessDatesTest(unittest.TestCase):
    def test_launch_is_monday_of_last_full_week(self):
        self.assertEqual(readiness.launch_date(2026, 11), date(2026, 10, 19))
        self.assertEqual(readiness.launch_date(2027, 1), date(2026, 12, 21))
        self.assertEqual(
            readiness.month_sundays(2026, 11),
            [date(2026, 11, day) for day in (1, 8, 15, 22, 29)],
        )

    def test_schedule_and_manual_deadline(self):
        settings = ServiceRegistry.READINESS_DEFAULTS
        start, deadline = readiness.schedule(settings, 2026, 11, TZ)
        self.assertEqual(start, datetime(2026, 10, 19, 10, 0, tzinfo=TZ))
        self.assertEqual(deadline, datetime(2026, 10, 22, 20, 0, tzinfo=TZ))
        late = datetime(2026, 10, 30, 21, 0, tzinfo=TZ)
        # Не позже конца месяца и не раньше чем через час.
        self.assertEqual(
            readiness.manual_deadline(late, settings),
            datetime(2026, 10, 31, 20, 0, tzinfo=TZ),
        )
        last = datetime(2026, 10, 31, 21, 0, tzinfo=TZ)
        self.assertEqual(
            readiness.manual_deadline(last, settings), last + timedelta(hours=1)
        )
        with self.assertRaises(ValueError):
            readiness.manual_deadline(
                datetime(2026, 10, 31, 23, 30, tzinfo=TZ), settings
            )

    def test_reminder_line(self):
        run = {
            "deadline": datetime(2026, 10, 22, 20, 0, tzinfo=TZ).isoformat(),
            "created_at": datetime(2026, 10, 19, 10, 0, tzinfo=TZ).isoformat(),
        }
        settings = {"remind_days": 1}
        before = datetime(2026, 10, 20, 12, 0, tzinfo=TZ)
        self.assertEqual(
            readiness.reminder_line(run, settings, before, TZ),
            "🔔 Напоминание: 21.10 20:00 — лично не ответившим и общее в "
            "чат служения",
        )
        after = datetime(2026, 10, 21, 21, 0, tzinfo=TZ)
        self.assertIn("отправлено 21.10 20:00",
                      readiness.reminder_line(run, settings, after, TZ))
        self.assertIn("выключено", readiness.reminder_line(
            run, {"remind_days": 0}, before, TZ
        ))
        self.assertIn("не отправлено", readiness.reminder_line(
            run, settings, after, TZ, sent=False
        ))
        late = {**run, "created_at": after.isoformat()}
        self.assertIn("не будет", readiness.reminder_line(
            late, settings, after, TZ
        ))

    def test_preview_deadline_is_not_in_the_past(self):
        settings = ServiceRegistry.READINESS_DEFAULTS
        before = datetime(2026, 10, 18, 12, 0, tzinfo=TZ)
        self.assertEqual(
            readiness.preview_deadline(None, settings, 2026, 11, before),
            datetime(2026, 10, 22, 20, 0, tzinfo=TZ),
        )
        # Автозапуск уже прошёл — срок как при запуске сейчас.
        late = datetime(2026, 10, 24, 15, 28, tzinfo=TZ)
        self.assertEqual(
            readiness.preview_deadline(None, settings, 2026, 11, late),
            datetime(2026, 10, 27, 20, 0, tzinfo=TZ),
        )
        self.assertIn(
            "автозапуска (19.10 10:00) прошло",
            readiness.preview_note(None, settings, 2026, 11, late),
        )
        self.assertIn(
            "как при автозапуске 19.10 10:00",
            readiness.preview_note(None, settings, 2026, 11, before),
        )
        run = {
            "status": "open",
            "deadline": datetime(2026, 10, 30, 20, 0, tzinfo=TZ).isoformat(),
        }
        self.assertEqual(
            readiness.preview_deadline(run, settings, 2026, 11, late),
            datetime(2026, 10, 30, 20, 0, tzinfo=TZ),
        )
        # Завершённый опрос — его прошедший срок не показывается.
        closed = {**run, "status": "closed",
                  "deadline": datetime(2026, 10, 22, 20, 0, tzinfo=TZ).isoformat()}
        self.assertEqual(
            readiness.preview_deadline(closed, settings, 2026, 11, late),
            datetime(2026, 10, 27, 20, 0, tzinfo=TZ),
        )
        self.assertNotIn(
            "идёт", readiness.preview_note(closed, settings, 2026, 11, late)
        )
        # Открыт, но срок уже прошёл (проверка ещё не закрыла).
        overdue = {**closed, "status": "open"}
        self.assertNotIn(
            "идёт", readiness.preview_note(overdue, settings, 2026, 11, late)
        )
        last_hour = datetime(2026, 10, 31, 23, 30, tzinfo=TZ)
        self.assertEqual(
            readiness.preview_deadline(None, settings, 2026, 11, last_hour),
            last_hour + timedelta(hours=1),
        )

    def test_option_labels_and_own_days_note(self):
        self.assertEqual(readiness.option_label("2026-11-01"), "01.11")
        self.assertEqual(
            readiness.option_label("2026-11-01", (-1, 1)), "сб 31.10–пн 02.11"
        )
        people = [
            {"name": "Катя Степанова", "tasks": ["уборка"], "day_offset": -3,
             "telegram_id": 321, "username": "katya"},
            {"name": "Анна", "tasks": ["уборка"], "day_offset": None},
            {"name": "Ушёл", "tasks": [], "day_offset": -1},
        ]
        self.assertEqual(
            plain_text(readiness.own_days_note(people)),
            "📅 Свой день: Катя Степанова — по четвергам",
        )
        self.assertEqual(readiness.own_days_note(people[1:]), "")
        twins = [
            {"name": "Аня", "key": "Аня · 123", "tasks": ["у"], "day_offset": -3,
             "username": "anya1", "telegram_id": 123},
            {"name": "Аня", "key": "Аня · 456", "tasks": ["у"], "day_offset": -1,
             "telegram_id": 456},
            {"name": "Вика", "key": "Вика", "tasks": ["у"], "day_offset": -2,
             "in_roster": False},
        ]
        self.assertEqual(
            plain_text(readiness.own_days_note(twins)),
            "📅 Свой день: Аня (1) — по четвергам; Аня (2) — по субботам",
        )
        self.assertIn(
            '<a href="tg://user?id=123">Аня (1)</a>',
            "".join(render_screen_chunks(readiness.own_days_note(twins))),
        )
        self.assertIn(
            '<a href="tg://user?id=456">Аня (2)</a>',
            "".join(render_screen_chunks(readiness.own_days_note(twins))),
        )
        note = readiness.own_days_note(twins)
        self.assertNotIn("123", plain_text(note))
        self.assertNotIn("456", plain_text(note))
        self.assertNotIn("@anya1", readiness.own_days_note(twins))
        service = {"name": "Звук"}
        self.assertEqual(
            readiness.question_text(service, 2026, 11, "👥"),
            "Когда можешь служить в ноябре?",
        )

    def test_answer_dates(self):
        dates = ["2026-11-01", "2026-11-08"]
        self.assertIsNone(readiness.answer_dates([], dates))
        self.assertEqual(readiness.answer_dates([2], dates), [])
        self.assertEqual(
            readiness.answer_dates([1, 0, 2], dates), ["2026-11-01", "2026-11-08"]
        )
        options = [{"date": day} for day in dates]
        self.assertEqual(
            readiness.answer_dates([1], options, reverse_poll=True),
            ["2026-11-08"],
        )
        self.assertEqual(
            readiness.answer_dates([2], options, reverse_poll=True),
            [readiness.AVAILABLE_ALL],
        )
        self.assertEqual(
            readiness.answer_dates([3], options, reverse_poll=True),
            [readiness.UNAVAILABLE_ALL],
        )


class ReadinessFlowTest(unittest.TestCase):
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
        # Добавлен в боте, в «Списках» его нет.
        self.registry.set_responsible_contact(
            1001, "sound", "сборка", "Глеб", 503
        )
        self.registry.set_readiness_settings(1001, "sound", enabled=True)

    def service(self):
        return self.registry.get_service("sound")

    def test_people_merge_lists_and_bot_bindings(self):
        people = {
            person["name"]: person
            for person in self.registry.readiness_people("sound")
        }
        self.assertEqual(people["Алексей"]["tasks"], ["звук", "сборка"])
        self.assertEqual(people["Глеб"]["tasks"], ["сборка"])
        self.assertIsNone(people["Дмитрий"]["telegram_id"])

    def test_settings_validation(self):
        with self.assertRaises(PermissionError):
            self.registry.set_readiness_settings(9999, "sound", enabled=False)
        with self.assertRaises(ValueError):
            self.registry.set_readiness_settings(1001, "sound", start_time="25:00")
        with self.assertRaises(ValueError):
            self.registry.set_readiness_settings(1001, "sound", duration_days=7)
        settings = self.registry.set_readiness_settings(
            1001, "sound", duration_days=2, remind_days=3
        )
        self.assertEqual(settings["remind_days"], 1)
        settings = self.registry.set_readiness_settings(
            1001, "sound", reverse_poll=True
        )
        self.assertTrue(settings["reverse_poll"])
        self.assertTrue(
            self.registry.get_readiness_settings("sound")["reverse_poll"]
        )
        with self.assertRaises(ValueError):
            self.registry.set_readiness_settings(
                1001, "sound", reverse_poll=1
            )
        self.assertFalse(settings["dates_only"])
        self.registry.set_readiness_settings(1001, "sound", dates_only=True)
        self.assertTrue(self.registry.get_readiness_settings("sound")["dates_only"])
        with self.assertRaises(ValueError):
            self.registry.set_readiness_settings(1001, "sound", dates_only=1)

    def test_dates_only_setting_migrates_existing_database(self):
        with self.registry._connection() as connection:
            connection.execute("ALTER TABLE readiness_settings DROP COLUMN dates_only")
        self.registry.initialize(1001, -10001)
        self.assertFalse(self.registry.get_readiness_settings("sound")["dates_only"])
        self.assertTrue(self.registry.get_readiness_settings("sound")["enabled"])

    def test_dates_only_launch_and_snapshot(self):
        self.registry.set_readiness_settings(1001, "sound", dates_only=True)
        bot = fake_bot()
        run = self.launch(bot)
        self.assertEqual(bot.send_poll.await_args.kwargs["options"], [
            "01.11", "08.11", "15.11", "22.11", "29.11",
            "❌ Не смогу в этом месяце",
        ])
        self.assertEqual(readiness.run_option_dates(run), run["dates"])
        dates = readiness.answer_dates([0], run["options"])
        self.assertEqual(dates, ["2026-11-01"])
        self.registry.set_readiness_settings(1001, "sound", dates_only=False)
        self.registry.record_readiness_answer(
            run["poll_id"], 501, "alex", "Алексей", dates,
        )
        summary = readiness.summarize(self.registry, run)
        self.assertEqual(summary.available["Алексей"], ["2026-11-01"])
        self.assertEqual(readiness.run_option_label(run, dates[0]), "01.11")
        self.assertTrue(any(
            "Алексей" in names for (day, _), names in summary.plan.slots.items()
            if day == date(2026, 11, 1)
        ))

    def test_dates_only_respects_actual_task_and_personal_days(self):
        people = {"А": {"tasks": ["раньше", "позже"]}}
        week = date(2026, 11, 1)
        later = date(2026, 11, 2)
        slots = {(week, "раньше", "А"): week, (week, "позже", "А"): later}
        options = [
            {"date": day.isoformat(), "date_only": True, "tasks": [task],
             "weeks": [week.isoformat()], "label": day.strftime("%d.%m")}
            for day, task in ((week, "раньше"), (later, "позже"))
        ]
        self.assertEqual(readiness.answer_dates([0], options), ["2026-11-01"])
        available = readiness.slot_availability(
            {"А": ["2026-11-01"]}, people, (week,), slots, options,
        )
        self.assertEqual(available["А"], frozenset({(week, "раньше")}))
        available = readiness.slot_availability(
            {"А": ["2026-11-02"]}, people, (week,), slots, options,
        )
        self.assertEqual(available["А"], frozenset({(later, "позже")}))

    def test_dates_only_reverse_and_distinct_task_dates(self):
        self.registry.set_service_task_day(1001, "sound", "сборка", -3)
        self.registry.set_readiness_settings(
            1001, "sound", dates_only=True, reverse_poll=True,
        )
        run = self.launch(fake_bot())
        self.assertEqual(len(run["options"]), 10)
        self.assertEqual(run["options"][0]["label"], "29.10")
        marked = readiness.answer_dates([0], run["options"], reverse_poll=True)
        self.registry.record_readiness_answer(
            run["poll_id"], 501, "alex", "Алексей", marked,
        )
        summary = readiness.summarize(self.registry, run)
        self.assertNotIn("2026-10-29", summary.available["Алексей"])
        self.assertIn("2026-11-01", summary.available["Алексей"])
        self.assertIn(
            (date(2026, 10, 29), "сборка"), summary.unavailable["Алексей"],
        )

    def test_dates_only_keeps_exact_task_weeks_on_overlapping_calendar_day(self):
        self.registry.set_readiness_settings(1001, "sound", dates_only=True)
        tasks = [{"name": "А", "day": None}, {"name": "Б", "day": None}]
        people = [
            {"tasks": ["А"], "day_offset": 1},
            {"tasks": ["Б"], "day_offset": -6},
        ]
        with (
            patch.object(self.registry, "plan_tasks", return_value=tasks),
            patch.object(self.registry, "readiness_people", return_value=people),
        ):
            options = readiness.poll_options(self.registry, "sound", 2026, 11)
        overlapping = next(item for item in options if item["date"] == "2026-11-02")
        self.assertEqual(set(readiness.option_slots(overlapping)), {
            (date(2026, 11, 1), "А"), (date(2026, 11, 8), "Б"),
        })

    def launch(self, bot):
        return asyncio.run(readiness.launch_run(
            bot, self.registry, self.service(), 2026, 11,
            datetime(2026, 10, 22, 20, 0, tzinfo=TZ), TZ, LOG, 4096,
        ))

    def test_launch_uses_day_window_and_own_day_note(self):
        self.registry.set_readiness_settings(1001, "sound", day_from=-1, day_to=1)
        self.registry.set_service_member_day(1001, "sound", "Алексей", -3)
        self.registry.set_service_member_day(1001, "sound", "Борис", -3)
        self.registry.set_service_member_day(1001, "sound", "Дмитрий", -3)
        self.registry.set_service_member_day(1001, "sound", "Глеб", -3)
        bot = fake_bot()
        self.launch(bot)
        options = bot.send_poll.await_args.kwargs["options"]
        self.assertEqual(options[0], "29.10 — звук")
        intro = bot.send_message.await_args.args[1]
        self.assertIn(
            '<a href="tg://user?id=501">Алексей</a> — по четвергам', intro,
        )
        self.assertNotIn("@", intro)

    def test_launch_snapshots_date_options_and_reverse_mode(self):
        self.registry.set_service_task_day(1001, "sound", "сборка", -3)
        self.registry.set_readiness_settings(
            1001, "sound", reverse_poll=True
        )
        bot = fake_bot()
        run = self.launch(bot)
        options = run["options"]
        self.assertEqual(len(options), 10)
        self.assertEqual(options[0]["label"], "29.10 — сборка")
        self.assertEqual(options[1]["label"], "01.11 — звук")
        self.assertTrue(run["reverse_poll"])
        self.assertIn(
            "отмечай только даты, когда не сможешь",
            bot.send_message.await_args.args[1].lower(),
        )
        # После запуска настройка не меняет интерпретацию уже собранного опроса.
        self.registry.set_readiness_settings(
            1001, "sound", reverse_poll=False
        )
        marked = options[0]["date"]
        dates = readiness.answer_dates([0], options, reverse_poll=True)
        self.registry.record_readiness_answer(
            run["poll_id"], 501, "alex", "Алексей", dates
        )
        summary = readiness.summarize(
            self.registry, self.registry.get_readiness_run("sound", "2026-11"),
            with_plan=False,
        )
        marked_option = readiness.option_key(marked, options[0]["task"])
        self.assertEqual(summary.answered["Алексей"], [marked_option])
        self.assertNotIn(marked_option, summary.available["Алексей"])
        self.assertIn(
            readiness.option_key(options[1]["date"], options[1]["task"]),
            summary.available["Алексей"],
        )
        self.assertTrue(summary.reverse_poll)

    def test_reverse_poll_can_confirm_all_dates_available(self):
        self.registry.set_readiness_settings(
            1001, "sound", reverse_poll=True
        )
        bot = fake_bot()
        run = self.launch(bot)
        options = run["options"]
        sent_options = bot.send_poll.await_args.kwargs["options"]
        self.assertEqual(sent_options[-2], "✅ Смогу в любые даты")
        self.assertEqual(sent_options[-1], "❌ Не смогу в этом месяце")
        dates = readiness.answer_dates(
            [len(options)], options, reverse_poll=True
        )
        self.assertEqual(dates, [readiness.AVAILABLE_ALL])
        self.registry.record_readiness_answer(
            run["poll_id"], 501, "alex", "Алексей", dates
        )
        saved = self.registry.get_readiness_run("sound", "2026-11")
        summary = readiness.summarize(self.registry, saved)
        self.assertEqual(
            set(summary.available["Алексей"]),
            {
                readiness.option_key(item["date"], item["task"])
                for item in options
            },
        )
        self.assertEqual(summary.unavailable, {})

    def test_poll_options_refuse_more_than_telegram_limit(self):
        tasks = [
            {"name": name, "needed": 1, "max": 1, "position": name,
             "implicit": False, "day": offset}
            for name, offset in (("сб", -1), ("вс", 0), ("пн", 1))
        ]
        people = [
            {**self.registry.readiness_people("sound")[0],
             "tasks": [task["name"]]}
            for task in tasks
        ]
        with (
            patch.object(self.registry, "plan_tasks", return_value=tasks),
            patch.object(self.registry, "readiness_people", return_value=people),
        ):
            with self.assertRaisesRegex(ValueError, "не более 12 вариантов"):
                readiness.poll_options(self.registry, "sound", 2026, 11)
            self.assertEqual(len(readiness.poll_options(
                self.registry, "sound", 2026, 11, validate_limit=False,
            )), 15)

        one_task = [{**tasks[0], "day": 0}]
        one_person = [{**people[0], "tasks": ["сб"]}]
        sundays = [
            date(2026, 11, 1) + timedelta(weeks=week)
            for week in range(11)
        ]
        with (
            patch.object(self.registry, "plan_tasks", return_value=one_task),
            patch.object(
                self.registry, "readiness_people", return_value=one_person
            ),
            patch.object(readiness, "month_sundays", return_value=sundays),
        ):
            self.assertEqual(
                len(readiness.poll_options(self.registry, "sound", 2026, 11)),
                11,
            )
            with self.assertRaisesRegex(ValueError, "не более 12 вариантов"):
                readiness.poll_options(
                    self.registry, "sound", 2026, 11, reverse_poll=True
                )

    def test_launch_uses_own_phrases(self):
        self.registry.set_service_member_day(1001, "sound", "Алексей", -3)
        for name in ("Борис", "Дмитрий", "Глеб"):
            self.registry.set_service_member_day(1001, "sound", name, -3)
        self.registry.set_readiness_phrase(1001, "sound", "cant", "🙈 Пропускаю месяц")
        self.registry.set_readiness_phrase(1001, "sound", "own_days", "🗓 Особые дни:")
        bot = fake_bot()
        self.launch(bot)
        self.assertEqual(
            bot.send_poll.await_args.kwargs["options"][-1], "🙈 Пропускаю месяц"
        )
        self.assertIn(
            '<a href="tg://user?id=501">Алексей</a> — по четвергам',
            bot.send_message.await_args.args[1],
        )
        self.assertNotIn("@", bot.send_message.await_args.args[1])

    def test_poll_question_template(self):
        self.registry.set_readiness_settings(
            1001, "sound", question_template="{service_name}: когда в {month_in}?"
        )
        bot = fake_bot()
        self.launch(bot)
        self.assertEqual(bot.send_poll.await_args.args[1], "Звук: когда в ноябре?")

    def test_reverse_mode_texts_ask_for_unavailable_dates(self):
        service = {"name": "Звук"}
        deadline = datetime(2026, 10, 20, 12, tzinfo=TZ)
        people = [{"name": "Анна", "telegram_id": None, "tasks": ["звук"]}]

        def intro(template=None, reverse=False):
            return readiness.intro_text(
                service, people, 2026, 11, deadline, TZ, template,
                reverse_poll=reverse,
            )

        self.assertEqual(
            readiness.question_text(service, 2026, 11),
            "Когда можешь служить в ноябре?",
        )
        self.assertEqual(
            readiness.question_text(service, 2026, 11, None, True),
            "Когда НЕ можешь служить в ноябре?",
        )
        custom = readiness.question_text(
            service, 2026, 11, "{service_name}: когда в {month_in}?", True
        )
        self.assertTrue(custom.startswith("Звук: когда в ноябре?"))
        self.assertIn("НЕ сможешь", custom)
        long_custom = readiness.question_text(service, 2026, 11, "я" * 400, True)
        self.assertEqual(len(long_custom), 300)
        self.assertIn("НЕ сможешь", long_custom)

        self.assertIn("Отметь даты, когда можешь служить", intro())
        self.assertNotIn("Режим «Наоборот»", intro())
        reverse = intro(reverse=True)
        self.assertIn("Отметь даты, когда НЕ можешь служить", reverse)
        self.assertNotIn("Отметь даты, когда можешь служить", reverse)
        self.assertIn("Режим «Наоборот»", reverse)
        custom_intro = intro("Когда можешь служить в {month}?", reverse=True)
        self.assertTrue(custom_intro.startswith("Когда можешь служить"))
        self.assertIn("НЕ сможешь", custom_intro)

    def test_launch_in_reverse_mode_asks_for_unavailable_dates(self):
        self.registry.set_readiness_settings(
            1001, "sound", reverse_poll=True
        )
        bot = fake_bot()
        self.launch(bot)
        self.assertEqual(
            bot.send_poll.await_args.args[1],
            "Когда НЕ можешь служить в ноябре?",
        )
        self.assertIn(
            "Отметь даты, когда НЕ можешь служить",
            bot.send_message.await_args.args[1],
        )
        self.assertIsNone(bot.send_poll.await_args.kwargs["question_parse_mode"])
        with self.assertRaises(ValueError):
            self.registry.set_readiness_settings(
                1001, "sound", question_template="x" * 301
            )
        with self.assertRaises(ValueError):
            self.registry.set_readiness_settings(
                1001, "sound", question_template="Когда {deadline}?"
            )

    def test_launch_sends_intro_and_poll_once(self):
        bot = fake_bot()
        run = self.launch(bot)
        self.assertEqual(run["status"], "open")
        self.assertEqual(len(run["dates"]), 5)
        poll = bot.send_poll.await_args
        self.assertEqual(poll.args[1], "Когда можешь служить в ноябре?")
        self.assertEqual(
            poll.kwargs["options"][-1], "❌ Не смогу в этом месяце"
        )
        self.assertTrue(poll.kwargs["allows_multiple_answers"])
        intro = bot.send_message.await_args.args[1]
        self.assertIn("Спасибо за твое служение в церкви! ❤️", intro)
        with self.assertRaises(ValueError):
            self.launch(bot)

    def test_launch_without_chat_is_refused(self):
        self.registry.set_group_chat(1001, "sound", -1005555, enabled=False)
        with self.assertRaises(ValueError):
            self.launch(fake_bot())

    def test_answers_summary_and_outside_voter(self):
        run = self.launch(fake_bot())
        record = self.registry.record_readiness_answer
        record("poll-1", 501, "alex", "Алексей", ["2026-11-01", "2026-11-08"])
        record("poll-1", 503, "", "Глеб", ["2026-11-01"])
        record("poll-1", 777, "serg", "Сергей", ["2026-11-15"])
        record("poll-1", 502, "", "Борис", [])
        self.assertIsNone(record("other", 501, "", "", []))
        summary = readiness.summarize(self.registry, run)
        self.assertEqual(set(summary.answered), {"Алексей", "Глеб", "Борис"})
        self.assertEqual([item["full_name"] for item in summary.outside], ["Сергей"])
        self.assertEqual(
            [person["name"] for person in summary.not_answered], ["Дмитрий"]
        )
        self.assertEqual(summary.plan.slots[(date(2026, 11, 1), "звук")], ["Алексей"])
        self.assertEqual(summary.plan.slots[(date(2026, 11, 1), "сборка")], ["Глеб"])
        text = readiness.progress_text("ЗВУК", run, summary, TZ)
        self.assertIn("Ответили 3 из 4 · вне состава: 1", text)
        self.assertIn("01.11 — звук — 2", text)
        self.assertIn("01.11 — сборка — 2", text)
        self.assertIn("08.11 — звук — 1", text)
        self.assertIn("08.11 — сборка — 1", text)
        self.assertIn("⚪ Вне состава: Сергей (15)", text)
        self.assertIn("Дмитрий (без Telegram)", text)
        # Отзыв голоса.
        record("poll-1", 503, "", "Глеб", None)
        self.assertNotIn(
            "Глеб", readiness.summarize(self.registry, run).answered
        )

    def test_tick_preview_launch_reminder_and_close(self):
        bot = fake_bot()

        def tick(moment):
            asyncio.run(readiness.run_readiness_tick(
                bot, self.registry, moment, TZ, 1001, LOG, 4096,
            ))

        tick(datetime(2026, 10, 18, 10, 30, tzinfo=TZ))
        bot.send_poll.assert_not_awaited()
        preview = bot.send_message.await_args.args
        self.assertEqual(preview[0], 1001)
        self.assertIn("опрос готовности", preview[1])
        bot.send_message.reset_mock()
        tick(datetime(2026, 10, 18, 10, 31, tzinfo=TZ))
        bot.send_message.assert_not_awaited()

        tick(datetime(2026, 10, 19, 10, 0, tzinfo=TZ))
        bot.send_poll.assert_awaited_once()
        # Время создания — по часам теста, а не по реальным.
        with closing(sqlite3.connect(self.registry.path)) as connection:
            connection.execute(
                "UPDATE readiness_runs SET created_at = ?",
                (datetime(2026, 10, 19, 7, 0, tzinfo=timezone.utc).isoformat(),),
            )
            connection.commit()
        tick(datetime(2026, 10, 19, 10, 1, tzinfo=TZ))
        bot.send_poll.assert_awaited_once()

        self.registry.record_readiness_answer(
            "poll-1", 501, "alex", "Алексей", ["2026-11-01"]
        )
        bot.send_message.reset_mock()
        tick(datetime(2026, 10, 21, 20, 0, tzinfo=TZ))
        reminded = {call.args[0] for call in bot.send_message.await_args_list}
        # Лично не ответившим и общее — в чат служения, ответом на опрос.
        self.assertEqual(reminded, {502, 503, -1005555})
        chat = next(
            call for call in bot.send_message.await_args_list
            if call.args[0] == -1005555
        )
        self.assertIn("Кто ещё не отметил даты", chat.args[1])
        self.assertNotIn("Борис", chat.args[1])
        self.assertEqual(chat.kwargs["reply_parameters"].message_id, 12)
        bot.send_message.reset_mock()
        tick(datetime(2026, 10, 21, 20, 1, tzinfo=TZ))
        bot.send_message.assert_not_awaited()

        tick(datetime(2026, 10, 22, 20, 0, tzinfo=TZ))
        bot.stop_poll.assert_awaited_once_with(-1005555, 12)
        announced = [
            call for call in bot.send_message.await_args_list
            if call.args[0] == -1005555
        ]
        self.assertIn("завершён. Спасибо всем", announced[-1].args[1])
        self.assertEqual(
            announced[-1].kwargs["reply_parameters"].message_id, 12
        )
        result = bot.send_message.await_args.args
        self.assertEqual(result[0], 1001)
        self.assertIn("Опрос завершён", result[1])
        run = self.registry.get_readiness_run("sound", "2026-11")
        self.assertEqual(run["status"], "closed")
        self.assertFalse(self.registry.record_readiness_answer(
            "poll-1", 502, "", "Борис", ["2026-11-01"]
        ))

    def test_late_launch_gets_no_reminder_and_failed_launch_is_not_retried(self):
        settings = self.registry.get_readiness_settings("sound")
        run = {
            "deadline": datetime(2026, 10, 22, 17, 0, tzinfo=timezone.utc)
            .isoformat(),
            "created_at": datetime(2026, 10, 21, 18, 0, tzinfo=timezone.utc)
            .isoformat(),
        }
        self.assertFalse(readiness.reminder_due(
            run, settings, datetime(2026, 10, 21, 22, 0, tzinfo=TZ)
        ))
        bot = fake_bot()
        bot.send_poll.side_effect = RuntimeError("no rights")
        for minute in range(3):
            asyncio.run(readiness.run_readiness_tick(
                bot, self.registry,
                datetime(2026, 10, 19, 10, minute, tzinfo=TZ), TZ, 1001, LOG,
                4096,
            ))
        bot.send_poll.assert_awaited_once()
        self.assertIn(
            "отправить не удалось", bot.send_message.await_args.args[1]
        )

    def test_member_without_tasks_counts_as_outside(self):
        self.registry.sync_service_rosters({"sound": [
            RosterMember("Алексей", ("звук", "сборка")),
            RosterMember("Борис", ("звук",)),
            RosterMember("Дмитрий", ("сборка",)),
            RosterMember("Евгений", ()),
        ]})
        self.registry.bind_service_member(1001, "sound", "Евгений", 504, "")
        run = self.launch(fake_bot())
        self.registry.record_readiness_answer(
            "poll-1", 504, "", "Женя", ["2026-11-01"]
        )
        summary = readiness.summarize(self.registry, run)
        self.assertNotIn("Евгений", summary.answered)
        self.assertEqual(
            [item["full_name"] for item in summary.outside], ["Евгений"]
        )

    def set_enabled_at(self, moment):
        with closing(sqlite3.connect(self.registry.path)) as connection:
            connection.execute(
                "UPDATE readiness_settings SET enabled_at = ?",
                (moment.astimezone(timezone.utc).isoformat(),),
            )
            connection.commit()

    def tick(self, bot, moment):
        asyncio.run(readiness.run_readiness_tick(
            bot, self.registry, moment, TZ, 1001, LOG, 4096,
        ))

    def test_missed_window_alert_only_when_bot_was_down(self):
        bot = fake_bot()
        # Автозапуск включён после времени запуска — не пропуск.
        self.set_enabled_at(datetime(2026, 10, 25, 12, 0, tzinfo=TZ))
        self.tick(bot, datetime(2026, 10, 25, 12, 5, tzinfo=TZ))
        bot.send_message.assert_not_awaited()
        # Был включён заранее, бот лежал всё окно — одно предупреждение.
        self.set_enabled_at(datetime(2026, 10, 1, 12, 0, tzinfo=TZ))
        self.tick(bot, datetime(2026, 10, 25, 12, 5, tzinfo=TZ))
        self.tick(bot, datetime(2026, 10, 25, 12, 6, tzinfo=TZ))
        self.assertEqual(bot.send_message.await_count, 1)
        self.assertIn("не был запущен вовремя", bot.send_message.await_args.args[1])
        bot.send_poll.assert_not_awaited()

    def test_missed_alert_survives_month_rollover(self):
        bot = fake_bot()
        self.set_enabled_at(datetime(2026, 10, 1, 12, 0, tzinfo=TZ))
        # Бот поднялся уже 1 ноября: ноябрьский опрос пропущен.
        self.tick(bot, datetime(2026, 11, 1, 9, 0, tzinfo=TZ))
        texts = [call.args[1] for call in bot.send_message.await_args_list]
        self.assertTrue(any("ноябрь 2026" in text for text in texts))

    def test_no_missed_alert_after_failed_launch(self):
        bot = fake_bot()
        self.set_enabled_at(datetime(2026, 10, 1, 12, 0, tzinfo=TZ))
        bot.send_poll.side_effect = RuntimeError("no rights")
        self.tick(bot, datetime(2026, 10, 19, 10, 0, tzinfo=TZ))
        bot.send_message.reset_mock()
        self.tick(bot, datetime(2026, 10, 23, 10, 0, tzinfo=TZ))
        bot.send_message.assert_not_awaited()

    def test_reminder_retried_after_transient_failure(self):
        bot = fake_bot()
        self.tick(bot, datetime(2026, 10, 19, 10, 0, tzinfo=TZ))
        with closing(sqlite3.connect(self.registry.path)) as connection:
            connection.execute(
                "UPDATE readiness_runs SET created_at = ?",
                (datetime(2026, 10, 19, 7, 0, tzinfo=timezone.utc).isoformat(),),
            )
            connection.commit()
        bot.send_message.reset_mock()
        bot.send_message.side_effect = [
            RuntimeError("network"), SimpleNamespace(message_id=1),
            SimpleNamespace(message_id=2), RuntimeError("network"),
        ]
        self.tick(bot, datetime(2026, 10, 21, 20, 0, tzinfo=TZ))
        bot.send_message.side_effect = None
        self.tick(bot, datetime(2026, 10, 21, 20, 1, tzinfo=TZ))
        self.tick(bot, datetime(2026, 10, 21, 20, 2, tzinfo=TZ))
        recipients = [call.args[0] for call in bot.send_message.await_args_list]
        # 501 и чат — сбой и повтор; доставленным второй раз не пишем.
        self.assertEqual(
            sorted(recipients), [-1005555, -1005555, 501, 501, 502, 503]
        )

    def test_no_autostart_when_planned_without_poll(self):
        self.registry.save_schedule_draft(
            "sound", "2026-11", run_id=None, dates=["2026-11-01"], tasks=[],
            slots={}, last_served={},
        )
        bot = fake_bot()
        self.tick(bot, datetime(2026, 10, 18, 10, 30, tzinfo=TZ))
        self.tick(bot, datetime(2026, 10, 19, 10, 0, tzinfo=TZ))
        bot.send_poll.assert_not_awaited()
        bot.send_message.assert_not_awaited()
        # И в ноябре — без «опрос не был запущен вовремя».
        self.tick(bot, datetime(2026, 11, 2, 10, 0, tzinfo=TZ))
        bot.send_message.assert_not_awaited()

    def test_no_chat_reminder_when_everyone_answered(self):
        bot = fake_bot()
        run = self.launch(bot)
        for telegram_id in (501, 502, 503):
            self.registry.record_readiness_answer(
                "poll-1", telegram_id, "", "", ["2026-11-01"]
            )
        bot.send_message.reset_mock()
        asyncio.run(readiness.send_chat_reminder(
            bot, self.registry, run, self.service(), TZ, LOG,
        ))
        bot.send_message.assert_not_awaited()

    def test_free_days_reminder_once_after_two_thirds(self):
        bot = fake_bot()
        run = self.launch(bot)
        bot.send_message.reset_mock()

        def remind():
            asyncio.run(readiness.send_free_days_reminder(
                bot, self.registry, run, self.service(), LOG,
            ))

        self.registry.record_readiness_answer(
            "poll-1", 501, "", "", ["2026-11-01"]
        )
        # Ответил 1 из 3 с Telegram — рано.
        remind()
        bot.send_message.assert_not_awaited()
        self.registry.record_readiness_answer(
            "poll-1", 502, "", "", ["2026-11-01", "2026-11-08"]
        )
        remind()
        chat, text = bot.send_message.await_args.args[:2]
        self.assertEqual(chat, -1005555)
        # 01.11 закрыто (Алексей — сборка, Борис — звук), остальные — нет.
        self.assertIn("08.11 — сборка", text)
        self.assertNotIn("08.11 — звук", text)
        self.assertIn("08.11 — сборка", text)
        self.assertIn("29.11 — звук", text)
        self.assertIn("29.11 — сборка", text)
        self.assertNotIn("01.11", text)
        # Глеб не ответил — просьба отметиться.
        self.assertIn("отметьтесь", text)
        remind()
        self.assertEqual(bot.send_message.await_count, 1)

    def test_free_days_reminder_lists_only_the_open_task(self):
        run = self.launch(fake_bot())
        sound = next(
            item for item in run["options"]
            if item["date"] == "2026-11-01" and item["task"] == "звук"
        )
        sound_key = readiness.option_key(sound["date"], sound["task"])
        for telegram_id in (501, 502):
            self.registry.record_readiness_answer(
                "poll-1", telegram_id, "", "", [sound_key]
            )
        summary = readiness.summarize(self.registry, run, with_plan=False)
        available = readiness.free_days(self.registry, run, summary)
        assembly_key = readiness.option_key("2026-11-01", "сборка")
        self.assertIn(assembly_key, available)
        self.assertNotIn(sound_key, available)

    def test_free_days_reminder_without_silent_and_when_all_covered(self):
        bot = fake_bot()
        run = self.launch(bot)
        bot.send_message.reset_mock()
        self.registry.set_readiness_phrase(
            1001, "sound", "free_days", "Свободно: {days}"
        )
        for telegram_id in (501, 502, 503):
            self.registry.record_readiness_answer(
                "poll-1", telegram_id, "", "", list(run["dates"])
            )
        # Все даты закрыты — напоминать не о чем.
        asyncio.run(readiness.send_free_days_reminder(
            bot, self.registry, run, self.service(), LOG,
        ))
        bot.send_message.assert_not_awaited()
        self.registry.record_readiness_answer(
            "poll-1", 501, "", "", ["2026-11-01"]
        )
        self.registry.record_readiness_answer(
            "poll-1", 503, "", "", ["2026-11-01"]
        )
        asyncio.run(readiness.send_free_days_reminder(
            bot, self.registry, run, self.service(), LOG,
        ))
        # Ответили все — без просьбы отметиться; своя фраза.
        self.assertEqual(
            bot.send_message.await_args.args[1],
            "Свободно: 08.11 — сборка, 15.11 — сборка, "
            "22.11 — сборка, 29.11 — сборка",
        )

    def test_sent_chat_reminder_skips_summary(self):
        bot = fake_bot()
        run = self.launch(bot)
        asyncio.run(readiness.send_chat_reminder(
            bot, self.registry, run, self.service(), TZ, LOG,
        ))
        with patch.object(readiness, "summarize") as summarize:
            asyncio.run(readiness.send_chat_reminder(
                bot, self.registry, run, self.service(), TZ, LOG,
            ))
        summarize.assert_not_called()

    def test_unreachable_chat_reminder_is_not_reported_sent(self):
        from aiogram.exceptions import TelegramForbiddenError

        bot = fake_bot()
        run = self.launch(bot)
        bot.send_message.side_effect = TelegramForbiddenError(
            method=SimpleNamespace(), message="bot was kicked"
        )
        asyncio.run(readiness.send_chat_reminder(
            bot, self.registry, run, self.service(), TZ, LOG,
        ))
        self.assertFalse(readiness.reminder_sent(self.registry, run))
        bot.send_message.side_effect = None
        bot.send_message.reset_mock()
        asyncio.run(readiness.send_chat_reminder(
            bot, self.registry, run, self.service(), TZ, LOG,
        ))
        bot.send_message.assert_not_awaited()

    def test_missing_poll_completes_close_without_retry(self):
        from aiogram.exceptions import TelegramBadRequest

        bot = fake_bot()
        run = self.launch(bot)
        bot.stop_poll.side_effect = TelegramBadRequest(
            method=SimpleNamespace(),
            message="Bad Request: message with poll to stop not found",
        )

        asyncio.run(readiness.close_run(
            bot, self.registry, run, self.service(), TZ, 1001, LOG, 4096,
        ))

        closed = self.registry.get_readiness_run_by_id(run["id"])
        self.assertEqual(closed["status"], "closed")
        self.assertTrue(closed["result_sent"])
        with self.subTest(step="poll_stopped"):
            self.assertTrue(closed["poll_stopped"])
        with self.subTest(step="closed_announced"):
            self.assertTrue(closed["closed_announced"])
        with self.subTest(step="unfinished_runs"):
            self.assertEqual(
                [item["id"] for item in self.registry.list_unfinished_readiness_runs()],
                [],
            )

        bot.stop_poll.reset_mock()
        bot.send_message.reset_mock()
        self.tick(bot, datetime.now(TZ))
        with self.subTest(step="stop_not_retried"):
            bot.stop_poll.assert_not_awaited()
        with self.subTest(step="messages_not_repeated"):
            bot.send_message.assert_not_awaited()

    def test_stop_poll_preserves_retry_for_other_errors(self):
        from aiogram.exceptions import TelegramBadRequest

        run = {"id": 7, "chat_id": -1005555, "message_id": 12}
        for error in (
            TimeoutError("network timeout"),
            TelegramBadRequest(
                method=SimpleNamespace(),
                message="Bad Request: not enough rights to stop poll",
            ),
        ):
            with self.subTest(error=type(error).__name__):
                bot = fake_bot()
                bot.stop_poll.side_effect = error
                self.assertFalse(asyncio.run(
                    readiness.stop_run_poll(bot, run, LOG)
                ))

    def test_close_retries_stop_and_result(self):
        bot = fake_bot()
        run = self.launch(bot)
        bot.stop_poll.side_effect = RuntimeError("network")
        bot.send_message.side_effect = RuntimeError("network")
        asyncio.run(readiness.close_run(
            bot, self.registry, run, self.service(), TZ, 1001, LOG, 4096,
        ))
        self.assertEqual(
            [item["id"] for item in self.registry.list_unfinished_readiness_runs()],
            [run["id"]],
        )
        bot.stop_poll.side_effect = None
        bot.send_message.side_effect = None
        self.tick(bot, datetime.now(TZ))
        self.assertEqual(self.registry.list_unfinished_readiness_runs(), [])
        self.assertIn("Опрос завершён", bot.send_message.await_args.args[1])

    def test_unreachable_admin_is_not_retried(self):
        from aiogram.exceptions import TelegramBadRequest

        bot = fake_bot()
        run = self.launch(bot)
        bot.send_message.side_effect = TelegramBadRequest(
            method=SimpleNamespace(), message="Bad Request: chat not found"
        )
        asyncio.run(readiness.close_run(
            bot, self.registry, run, self.service(), TZ, 1001, LOG, 4096,
        ))
        self.assertEqual(self.registry.list_unfinished_readiness_runs(), [])

    def test_result_retried_only_for_failed_admin(self):
        self.registry.add_service_admin(1001, "sound", 2002)
        self.registry.add_service_admin(1001, "sound", 2003)
        bot = fake_bot()
        run = self.launch(bot)
        bot.send_message.reset_mock()

        def send(chat_id, *args, **kwargs):
            if chat_id == 2003 and send.fail:
                raise RuntimeError("network")
            return SimpleNamespace(message_id=1)

        send.fail = True
        bot.send_message.side_effect = send
        asyncio.run(readiness.close_run(
            bot, self.registry, run, self.service(), TZ, 1001, LOG, 4096,
        ))
        self.assertEqual(len(self.registry.list_unfinished_readiness_runs()), 1)
        send.fail = False
        bot.send_message.reset_mock()
        self.tick(bot, datetime.now(TZ))
        # Повтор — только тому, кому не дошло.
        self.assertEqual(
            [call.args[0] for call in bot.send_message.await_args_list], [2003]
        )
        self.assertEqual(self.registry.list_unfinished_readiness_runs(), [])

    def test_other_bad_request_is_retried(self):
        from aiogram.exceptions import TelegramBadRequest

        self.assertTrue(readiness.unreachable(TelegramBadRequest(
            method=SimpleNamespace(), message="Bad Request: chat not found"
        )))
        self.assertFalse(readiness.unreachable(TelegramBadRequest(
            method=SimpleNamespace(), message="Bad Request: can't parse entities"
        )))

    def test_parked_run_is_settled(self):
        run = self.launch(fake_bot())
        self.registry.move_readiness_run(run["id"], f"2026-11#{run['id']}")
        self.assertEqual(self.registry.list_open_readiness_runs(), [])
        self.tick(fake_bot(), datetime(2026, 10, 20, 9, 0, tzinfo=TZ))
        restored = self.registry.get_readiness_run("sound", "2026-11")
        self.assertEqual(restored["id"], run["id"])
        self.assertEqual(restored["status"], "closed")
        # Пропуск месяца не стирает ответы отложенного опроса.
        self.registry.move_readiness_run(run["id"], f"2026-11#{run['id']}")
        self.registry.create_readiness_run("sound", "2026-11", status="skipped")
        self.tick(fake_bot(), datetime(2026, 10, 20, 9, 1, tzinfo=TZ))
        self.assertEqual(
            self.registry.get_readiness_run("sound", "2026-11")["id"], run["id"]
        )
        # Запись нового опроса без опроса в чате — не новый опрос.
        self.registry.move_readiness_run(run["id"], f"2026-11#{run['id']}")
        orphan = self.registry.create_readiness_run(
            "sound", "2026-11", status="open"
        )
        self.tick(fake_bot(), datetime(2026, 10, 20, 9, 2, tzinfo=TZ))
        self.assertEqual(
            self.registry.get_readiness_run("sound", "2026-11")["id"], run["id"]
        )
        self.assertIsNone(self.registry.get_readiness_run_by_id(orphan["id"]))
        # Новый опрос уже есть — отложенный удаляется.
        self.registry.move_readiness_run(run["id"], f"2026-11#{run['id']}")
        new = self.registry.create_readiness_run(
            "sound", "2026-11", status="open"
        )
        self.registry.attach_readiness_poll(
            new["id"], poll_id="p2", chat_id=-1005555, message_id=20,
            intro_message_id=None,
        )
        self.tick(fake_bot(), datetime(2026, 10, 20, 9, 3, tzinfo=TZ))
        self.assertIsNone(self.registry.get_readiness_run_by_id(run["id"]))

    def test_add_member_does_not_take_over_bound_name(self):
        # «Глеб» привязан через «👥 Ответственные» к 503.
        with self.assertRaises(ValueError):
            self.registry.bind_service_member(
                1001, "sound", "Глеб", 777, tasks=["звук"], from_readiness=True
            )
        self.assertEqual(
            {
                person["telegram_id"]
                for person in self.registry.readiness_people("sound")
                if person["name"] == "Глеб"
            },
            {503},
        )

    def test_reserved_run_without_poll_is_cleaned(self):
        orphan = self.registry.create_readiness_run(
            "sound", "2026-11", status="open",
            deadline=datetime(2026, 10, 22, 17, 0, tzinfo=timezone.utc).isoformat(),
        )
        created = datetime.fromisoformat(orphan["created_at"])
        bot = fake_bot()
        self.tick(bot, created.astimezone(TZ) + timedelta(minutes=1))
        self.assertIsNotNone(self.registry.get_readiness_run_by_id(orphan["id"]))
        self.tick(bot, created.astimezone(TZ) + timedelta(minutes=6))
        self.assertIsNone(self.registry.get_readiness_run_by_id(orphan["id"]))
        bot.send_message.assert_not_awaited()

    def test_early_answer_is_replayed_after_attach(self):
        # Вне отправки опроса ответы на неизвестный опрос не придерживаются.
        self.assertFalse(
            readiness.hold_early_answer("other", 501, "", "", [0])
        )
        bot = fake_bot()

        async def send_poll(*args, **kwargs):
            # Буфер полон чужим ответом — вытесняется он, а не новый.
            readiness.hold_early_answer("foreign", 9, "", "", [0])
            # Голос пришёл раньше, чем бот записал ID опроса.
            self.assertTrue(readiness.hold_early_answer(
                "poll-1", 501, "alex", "Алексей", [0, 1]
            ))
            return SimpleNamespace(
                message_id=12, poll=SimpleNamespace(id="poll-1")
            )

        bot.send_poll = AsyncMock(side_effect=send_poll)
        with patch.object(readiness, "_EARLY_LIMIT", 1):
            run = self.launch(bot)
        answers = self.registry.list_readiness_answers(run["id"])
        self.assertEqual(
            answers[0]["dates"],
            readiness.answer_dates([0, 1], run["options"]),
        )

    def test_lost_poll_response_keeps_month_and_asks_to_check(self):
        from aiogram.exceptions import TelegramNetworkError

        bot = fake_bot()
        bot.send_poll.side_effect = TelegramNetworkError(
            method=SimpleNamespace(), message="timeout"
        )
        with self.assertRaises(ValueError) as caught:
            self.launch(bot)
        self.assertIn("проверьте", str(caught.exception))
        run = self.registry.get_readiness_run("sound", "2026-11")
        self.assertEqual(run["status"], "closed")
        self.assertEqual(self.registry.list_unfinished_readiness_runs(), [])

    def test_unattached_poll_that_cannot_be_stopped_is_kept(self):
        bot = fake_bot()
        bot.stop_poll.side_effect = RuntimeError("network")
        original = self.registry.attach_readiness_poll
        calls = []

        def attach(*args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("disk")
            return original(*args, **kwargs)

        self.registry.attach_readiness_poll = attach
        with self.assertRaises(RuntimeError):
            self.launch(bot)
        run = self.registry.get_readiness_run("sound", "2026-11")
        self.assertEqual((run["status"], run["poll_id"]), ("closed", "poll-1"))
        # Ежеминутная проверка ещё раз попробует остановить опрос.
        self.assertEqual(
            [item["id"] for item in self.registry.list_unfinished_readiness_runs()],
            [run["id"]],
        )

    def test_same_names_stay_distinct(self):
        self.registry.set_responsible_contact(
            1001, "sound", "сборка", "Глеб", 503
        )
        with closing(sqlite3.connect(self.registry.path)) as connection:
            connection.execute(
                "INSERT INTO responsible_contacts (service_id, task, name, "
                "telegram_id, username, updated_by, updated_at) "
                "VALUES ('sound', 'звук', 'Глеб', 599, '', 1001, 'x')"
            )
            connection.commit()
        people = [
            person for person in self.registry.readiness_people("sound")
            if person["name"] == "Глеб"
        ]
        self.assertEqual(len(people), 2)
        self.assertEqual(len({person["key"] for person in people}), 2)

    def test_custom_intro_and_reminder_templates(self):
        with self.assertRaises(ValueError):
            self.registry.set_readiness_settings(
                1001, "sound", intro_template="{name}"
            )
        self.registry.set_readiness_settings(
            1001, "sound",
            intro_template="Опрос {service_name} · {month} до {deadline}\n👥 {people}",
            reminder_template="Напоминаю: {month}, до {deadline}",
        )
        bot = fake_bot()
        run = self.launch(bot)
        intro = bot.send_message.await_args.args[1]
        self.assertIn("Опрос Звук · ноябрь 2026 до 22.10 20:00", intro)
        self.assertIn("Алексей", intro)
        self.assertEqual(
            readiness.reminder_text(
                self.service(), run, TZ,
                self.registry.get_readiness_settings("sound")["reminder_template"],
            ),
            "Напоминаю: ноябрь 2026, до 22.10 20:00",
        )
        # Встроенный текст не привязан к смыслу выбора (в т. ч. «Наоборот»).
        default = readiness.reminder_text(self.service(), run, TZ)
        self.assertIn("не ответил(а) на опрос по служению на ноябрь 2026", default)
        self.assertNotIn("можешь служить", default)
        self.registry.set_readiness_settings(
            1001, "sound", closed_template="Всё, {month} — спасибо!"
        )
        self.assertEqual(
            readiness.closed_text(
                self.service(), run,
                self.registry.get_readiness_settings("sound")["closed_template"],
            ),
            "Всё, ноябрь 2026 — спасибо!",
        )
        with self.assertRaises(ValueError):
            self.registry.set_readiness_settings(
                1001, "sound", closed_template="{deadline}"
            )
        # Без опрашиваемых строка «👥» не остаётся пустой.
        self.assertEqual(
            readiness.render_template("А\n👥 {people}", people=""), "А"
        )

    def test_skipped_month_is_not_launched(self):
        self.registry.create_readiness_run("sound", "2026-11", status="skipped")
        bot = fake_bot()
        asyncio.run(readiness.run_readiness_tick(
            bot, self.registry, datetime(2026, 10, 19, 10, 0, tzinfo=TZ), TZ,
            1001, LOG, 4096,
        ))
        bot.send_poll.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
