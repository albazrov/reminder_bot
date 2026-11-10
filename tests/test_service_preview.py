import asyncio
import logging
import sqlite3
import tempfile
import unittest
import zipfile
from contextlib import closing
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from bot.app import (
    _prepare_service_test_previews,
    get_group_reminder_preview,
    get_template_preview,
)
from bot.scheduler import next_reminder_occurrence, reminder_send_date
from bot.service_registry import ServiceRegistry
from bot.ui import plain_text, render_screen_chunks


class ServicePreviewTest(unittest.TestCase):
    def test_group_preview_uses_short_names_but_exact_full_name_bindings(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            registry = ServiceRegistry(Path(temp_dir) / "registry.sqlite3")
            registry.initialize(67890, -10001)
            registry.set_group_chat(67890, "sound", -10002, enabled=True)
            registry.set_responsible_contact(
                67890, "sound", "Микшер", "Алексей Полный", 3001,
            )
            registry.set_service_member_short_name(
                67890, "sound", "Алексей Полный", "Лёша",
            )
            preview, _ = self._group_reminder_preview(
                registry, temp_dir, {"Микшер": ["Алексей Полный"]},
                body="{task_assignments}",
            )
            self.assertTrue(preview["has_data"])
            self.assertIn("Лёша", plain_text(preview["text"]))
            self.assertNotIn("Алексей Полный", plain_text(preview["text"]))
            self.assertIn("LINK:3001:", preview["text"])
            unbound, _ = self._group_reminder_preview(
                registry, temp_dir, {"Другая задача": ["Алексей Полный"]},
                body="{task_assignments}",
            )
            self.assertNotIn("LINK:3001:", unbound["text"])
            self.assertIn("не привязан", plain_text(unbound["text"]))

    @staticmethod
    def _fixed_occurrence(_now, reminder):
        event_date = date(2026, 10, 25)
        return event_date, event_date - timedelta(days=reminder["reminder_days"])

    def _group_reminder_preview(
        self, registry, temp_dir, assignments, body=None
    ):
        reminder = registry.list_group_reminders("sound")[0]
        credentials = SimpleNamespace(
            admin_tg_id=67890,
            yandex_public_url="https://example.invalid/schedule",
        )

        async def run():
            with patch(
                "bot.app.reminder_occurrence",
                side_effect=self._fixed_occurrence,
            ), patch(
                "bot.app._load_service_task_assignments",
                new=AsyncMock(return_value=(assignments, True)),
            ):
                return await get_group_reminder_preview(
                    reminder["id"],
                    credentials,
                    Path(temp_dir),
                    ZoneInfo("UTC"),
                    registry,
                    logging.getLogger("test-group-reminder-preview"),
                    body=body,
                )

        return asyncio.run(run()), reminder

    def _template_preview(self, registry, temp_dir, kind, template,
                          assignments, found=True):
        credentials = SimpleNamespace(admin_tg_id=67890)

        async def load(*_args, raw_out, **_kwargs):
            raw_out.update(assignments)
            return assignments, found

        async def run():
            with patch(
                "bot.app._next_sunday_in_tz", return_value=date(2026, 10, 25)
            ), patch(
                "bot.app._load_service_task_assignments",
                new=AsyncMock(side_effect=load),
            ):
                return await get_template_preview(
                    "sound", kind, template, credentials, Path(temp_dir),
                    ZoneInfo("UTC"), registry,
                    logging.getLogger("test-template-preview"),
                )

        return asyncio.run(run())

    def test_template_preview_renders_draft_and_builtin_templates(self):
        assignments = {"Микшер": {"names": ["Алексей"], "is_informational": False}}
        with tempfile.TemporaryDirectory() as temp_dir:
            registry = ServiceRegistry(Path(temp_dir) / "registry.sqlite3")
            registry.initialize(67890, -100)
            draft = self._template_preview(
                registry, temp_dir, "default", "Черновик {sunday_date}",
                assignments,
            )
            self.assertEqual(draft["text"], "Черновик 25.10.2026")
            builtin = self._template_preview(
                registry, temp_dir, "default", None, assignments
            )
            self.assertIn("в воскресенье 25.10.2026", builtin["text"])
            report = self._template_preview(
                registry, temp_dir, "report", None, assignments
            )
            self.assertTrue(report["has_data"])
            self.assertIn("📋 Отчёт · Звук · 25.10.2026", report["text"])
            self.assertIn("⏳ нет ответа — Алексей (Микшер)", report["text"])
            custom = self._template_preview(
                registry, temp_dir, "report", "{confirmation_status}",
                assignments,
            )
            self.assertEqual(custom["text"], "⏳ нет ответа — Алексей (Микшер)")

    def test_template_preview_reports_missing_event_data(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            registry = ServiceRegistry(Path(temp_dir) / "registry.sqlite3")
            registry.initialize(67890, -100)
            result = self._template_preview(
                registry, temp_dir, "default", None, {}, found=False
            )
            self.assertFalse(result["has_data"])
            self.assertEqual(result["error"], "Нет данных из графика на 25.10.2026.")

    def test_group_reminder_preview_renders_body_override(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            registry = ServiceRegistry(Path(temp_dir) / "registry.sqlite3")
            registry.initialize(67890, -100)
            result, reminder = self._group_reminder_preview(
                registry,
                temp_dir,
                {"Микшер": {"names": ["Алексей"], "is_informational": False}},
                body="Общий текст {sunday_date}\n\nВторой {service_name}",
            )

            self.assertTrue(result["has_data"])
            self.assertEqual(
                result["text"], "Общий текст 25.10.2026\n\nВторой Звук"
            )
            self.assertIsNone(
                registry.get_group_reminder(reminder["id"])["body"]
            )

    def test_group_reminder_preview_uses_default_template_and_informational_notes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            registry = ServiceRegistry(Path(temp_dir) / "registry.sqlite3")
            registry.initialize(67890, -100)
            registry.set_service_default_template(
                67890,
                "sound",
                "{service_name} · {sunday_date}\n{task_assignments}\n"
                "{informational_notes}",
            )
            registry.set_responsible_contact(
                67890, "sound", "Микшер", "Алексей", 12345, "alex"
            )
            result, reminder = self._group_reminder_preview(
                registry,
                temp_dir,
                {
                    "Микшер": {
                        "names": ["Алексей"],
                        "is_informational": False,
                    },
                    "2-я часть": {
                        "names": ["нет"],
                        "is_informational": True,
                    },
                },
            )

            self.assertTrue(result["has_data"])
            self.assertIsNone(result["error"])
            self.assertEqual(result["date"], "25.10.2026")
            self.assertEqual(result["send_date"], "24.10.2026")
            self.assertEqual(result["time"], reminder["reminder_time"])
            self.assertIn("Звук · 25.10.2026", result["text"])
            self.assertIn("Алексей (@alex)", plain_text(result["text"]))
            self.assertIn("📌 2-я часть нет", result["text"])

    def test_group_reminder_preview_falls_back_to_builtin_template(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            registry = ServiceRegistry(Path(temp_dir) / "registry.sqlite3")
            registry.initialize(67890, -100)
            result, _ = self._group_reminder_preview(
                registry,
                temp_dir,
                {"Микшер": ["Алексей"]},
            )

            self.assertTrue(result["has_data"])
            self.assertIn(
                "Напоминание о служении «Звук» в воскресенье 25.10.2026:",
                result["text"],
            )
            self.assertIn("Микшер", result["text"])

    def test_group_reminder_preview_uses_reminder_body_when_set(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            registry = ServiceRegistry(Path(temp_dir) / "registry.sqlite3")
            registry.initialize(67890, -100)
            reminder_id = registry.add_group_reminder(
                67890,
                "sound",
                2,
                "11:00",
                "Свое сообщение {service_name}: {informational_notes}",
            )
            reminder = registry.get_group_reminder(reminder_id)
            credentials = SimpleNamespace(
                admin_tg_id=67890,
                yandex_public_url="https://example.invalid/schedule",
            )

            async def run():
                with patch(
                    "bot.app.reminder_occurrence",
                    side_effect=self._fixed_occurrence,
                ), patch(
                    "bot.app._load_service_task_assignments",
                    new=AsyncMock(
                        return_value=({"Микшер": ["Алексей"]}, True)
                    ),
                ):
                    return await get_group_reminder_preview(
                        reminder_id,
                        credentials,
                        Path(temp_dir),
                        ZoneInfo("UTC"),
                        registry,
                        logging.getLogger("test-custom-reminder-preview"),
                    )

            result = asyncio.run(run())

            self.assertTrue(result["has_data"])
            self.assertEqual(result["time"], "11:00")
            self.assertEqual(result["text"], "Свое сообщение Звук: ")

    def test_group_reminder_preview_leaves_informational_notes_empty_when_absent(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            registry = ServiceRegistry(Path(temp_dir) / "registry.sqlite3")
            registry.initialize(67890, -100)
            registry.set_service_default_template(
                67890, "sound", "Текст {informational_notes}"
            )

            result, _ = self._group_reminder_preview(
                registry,
                temp_dir,
                {
                    "Микшер": {
                        "names": ["Алексей"],
                        "is_informational": False,
                    }
                },
            )

            self.assertTrue(result["has_data"])
            self.assertEqual(result["text"], "Текст ")
            self.assertNotIn("📌", result["text"])

    def test_group_reminder_preview_rejects_informational_only_assignments(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            registry = ServiceRegistry(Path(temp_dir) / "registry.sqlite3")
            registry.initialize(67890, -100)
            result, _ = self._group_reminder_preview(
                registry,
                temp_dir,
                {
                    "Причастие": {
                        "names": [],
                        "is_informational": True,
                    }
                },
            )

            self.assertFalse(result["has_data"])
            self.assertEqual(
                result["error"],
                "На эту дату нет задач с ответственными. "
                "Уведомление не будет отправлено.",
            )
            self.assertEqual(result["text"], "")

    def test_reminder_send_date_crosses_year_boundary(self):
        self.assertEqual(
            reminder_send_date(date(2027, 1, 1), 1),
            date(2026, 12, 31),
        )

    def test_next_reminder_occurrence_skips_past_saturday_send_date(self):
        tz = ZoneInfo("UTC")
        event_date, send_date = next_reminder_occurrence(
            datetime(2026, 10, 17, 12, 0, tzinfo=tz),
            3,
            "18:00",
        )

        self.assertEqual(event_date, date(2026, 10, 25))
        self.assertEqual(send_date, date(2026, 10, 22))

    def test_next_reminder_occurrence_includes_sunday_before_send_time(self):
        tz = ZoneInfo("UTC")
        event_date, send_date = next_reminder_occurrence(
            datetime(2026, 10, 18, 10, 0, tzinfo=tz),
            0,
            "18:00",
        )

        self.assertEqual(event_date, date(2026, 10, 18))
        self.assertEqual(send_date, date(2026, 10, 18))

    def test_next_reminder_occurrence_skips_sunday_after_send_time(self):
        tz = ZoneInfo("UTC")
        event_date, send_date = next_reminder_occurrence(
            datetime(2026, 10, 18, 19, 0, tzinfo=tz),
            0,
            "18:00",
        )

        self.assertEqual(event_date, date(2026, 10, 25))
        self.assertEqual(send_date, date(2026, 10, 25))

    def test_next_reminder_occurrence_skips_send_time_already_passed_today(self):
        tz = ZoneInfo("UTC")
        event_date, send_date = next_reminder_occurrence(
            datetime(2026, 10, 20, 19, 0, tzinfo=tz),
            5,
            "18:00",
        )

        self.assertEqual(event_date, date(2026, 11, 1))
        self.assertEqual(send_date, date(2026, 10, 27))

    def test_group_reminder_preview_preserves_unknown_legacy_fields(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            registry = ServiceRegistry(Path(temp_dir) / "registry.sqlite3")
            registry.initialize(67890, -100)
            reminder_id = registry.list_group_reminders("sound")[0]["id"]
            with closing(sqlite3.connect(registry.path)) as connection:
                connection.execute(
                    "UPDATE group_reminders SET body = ? WHERE id = ?",
                    ("Назначенный: {tg_responsible}", reminder_id),
                )
                connection.commit()

            result, _ = self._group_reminder_preview(
                registry,
                temp_dir,
                {"Микшер": ["Алексей"]},
            )

            self.assertTrue(result["has_data"])
            self.assertIsNone(result["error"])
            self.assertEqual(
                result["text"], "Назначенный: {tg_responsible}"
            )

    def test_group_reminder_preview_reports_missing_event_data(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            registry = ServiceRegistry(Path(temp_dir) / "registry.sqlite3")
            registry.initialize(67890, -100)
            reminder = registry.list_group_reminders("sound")[0]
            credentials = SimpleNamespace(
                admin_tg_id=67890,
                yandex_public_url="https://example.invalid/schedule",
            )

            async def run():
                with patch(
                    "bot.app.reminder_occurrence",
                    side_effect=self._fixed_occurrence,
                ), patch(
                    "bot.app._load_service_task_assignments",
                    new=AsyncMock(return_value=({}, False)),
                ):
                    return await get_group_reminder_preview(
                        reminder["id"],
                        credentials,
                        Path(temp_dir),
                        ZoneInfo("UTC"),
                        registry,
                        logging.getLogger("test-missing-reminder-data"),
                    )

            result = asyncio.run(run())

            self.assertFalse(result["has_data"])
            self.assertEqual(result["error"], "Нет данных из графика на 25.10.2026.")
            self.assertEqual(result["text"], "")

    def test_group_reminder_preview_reports_schedule_download_failure(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            registry = ServiceRegistry(Path(temp_dir) / "registry.sqlite3")
            registry.initialize(67890, -100)
            reminder = registry.list_group_reminders("sound")[0]
            credentials = SimpleNamespace(
                admin_tg_id=67890,
                yandex_public_url="https://example.invalid/schedule",
            )

            async def run():
                with patch(
                    "bot.app.reminder_occurrence",
                    side_effect=self._fixed_occurrence,
                ), patch(
                    "bot.app._load_service_task_assignments",
                    new=AsyncMock(
                        side_effect=zipfile.BadZipFile("corrupt workbook")
                    ),
                ):
                    return await get_group_reminder_preview(
                        reminder["id"],
                        credentials,
                        Path(temp_dir),
                        ZoneInfo("UTC"),
                        registry,
                        logging.getLogger("test-download-failure-preview"),
                    )

            result = asyncio.run(run())

            self.assertFalse(result["has_data"])
            self.assertEqual(
                result["error"],
                "Не удалось загрузить график. Попробуйте позже.",
            )

    def test_group_reminder_preview_preserves_unsupported_template_field(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            registry = ServiceRegistry(Path(temp_dir) / "registry.sqlite3")
            registry.initialize(67890, -100)
            reminder_id = registry.list_group_reminders("sound")[0]["id"]
            with closing(sqlite3.connect(registry.path)) as connection:
                connection.execute(
                    "UPDATE group_reminders SET body = ? WHERE id = ?",
                    ("Значение: {foo}", reminder_id),
                )
                connection.commit()
            credentials = SimpleNamespace(
                admin_tg_id=67890,
                yandex_public_url="https://example.invalid/schedule",
            )

            async def run():
                with patch(
                    "bot.app.reminder_occurrence",
                    side_effect=self._fixed_occurrence,
                ), patch(
                    "bot.app._load_service_task_assignments",
                    new=AsyncMock(
                        return_value=({"Микшер": ["Алексей"]}, True)
                    ),
                ):
                    return await get_group_reminder_preview(
                        reminder_id,
                        credentials,
                        Path(temp_dir),
                        ZoneInfo("UTC"),
                        registry,
                        logging.getLogger("test-template-error-preview"),
                    )

            result = asyncio.run(run())

            self.assertTrue(result["has_data"])
            self.assertIsNone(result["error"])
            self.assertEqual(result["text"], "Значение: {foo}")

    def test_preview_uses_selected_service_assignments_and_contacts(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            registry = ServiceRegistry(Path(temp_dir) / "registry.sqlite3")
            registry.initialize(67890, -100)
            registry.set_notification_settings(
                67890,
                "sound",
                reminder_days=1,
                reminder_time="18:00",
                group_template=(
                    "{service_name} {sunday_date}\n{task_assignments}"
                ),
                personal_templates={
                    "Микшер": "{name}: {task} для {service_name} "
                    "на {sunday_date} ({telegram_id}, {username})"
                },
            )
            registry.set_responsible_contact(
                67890, "sound", "Микшер", "Алексей", 12345, "alex"
            )
            assignments = {
                "Микшер": ["Алексей", "Мария"],
                "Презентация": ["Иван"],
            }

            async def run():
                with patch(
                    "bot.app._load_service_task_assignments",
                    new=AsyncMock(return_value=assignments),
                ) as load_assignments:
                    result = await _prepare_service_test_previews(
                        "sound",
                        SimpleNamespace(admin_tg_id=67890),
                        Path(temp_dir),
                        ZoneInfo("UTC"),
                        registry,
                        logging.getLogger("test-service-preview"),
                    )
                load_assignments.assert_awaited_once()
                self.assertEqual(
                    load_assignments.await_args.args[3], "sound"
                )
                self.assertEqual(
                    load_assignments.await_args.kwargs["only_date"],
                    result["sunday_date"],
                )
                return result

            preview = asyncio.run(run())

            self.assertIn("Звук", preview["group_text"])
            group_text = plain_text(preview["group_text"])
            self.assertIn("🎚️ Микшер\n• Алексей (@alex)", group_text)
            self.assertNotIn("12345", group_text)
            self.assertIn(
                '<a href="tg://user?id=12345">Алексей (@alex)</a>',
                "".join(render_screen_chunks(preview["group_text"], limit=4096)),
            )
            self.assertIn("• Мария · не привязан", preview["group_text"])
            self.assertIn("{task_assignments}", preview["example_group_template"])
            example_text = plain_text(preview["example_group_text"])
            self.assertIn("Алексей (@alex)", example_text)
            self.assertNotIn("12345", example_text)
            self.assertEqual(len(preview["personal_messages"]), 3)
            alex_message = preview["personal_messages"][0]
            self.assertEqual(alex_message["telegram_id"], "12345")
            self.assertIn("Алексей: Микшер для Звук", alex_message["text"])
            self.assertTrue(
                any("Микшер — Мария" in issue for issue in preview["issues"])
            )
            self.assertTrue(
                any("Презентация — Иван" in issue for issue in preview["issues"])
            )

    def test_preview_excludes_informational_tasks_from_group_and_personal_texts(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            registry = ServiceRegistry(Path(temp_dir) / "registry.sqlite3")
            registry.initialize(67890, -100)
            registry.set_responsible_contact(
                67890, "sound", "Праздник", "Дежурный", 12345
            )
            assignments = {
                "Микшер": {
                    "names": ["Алексей"],
                    "is_informational": False,
                },
                "Праздник": {
                    "names": ["Дежурный"],
                    "is_informational": True,
                },
            }

            async def run():
                with patch(
                    "bot.app._next_sunday_in_tz",
                    return_value=date(2026, 10, 25),
                ), patch(
                    "bot.app._load_service_task_assignments",
                    new=AsyncMock(return_value=assignments),
                ):
                    return await _prepare_service_test_previews(
                        "sound",
                        SimpleNamespace(admin_tg_id=67890),
                        Path(temp_dir),
                        ZoneInfo("UTC"),
                        registry,
                        logging.getLogger("test-info-preview"),
                    )

            preview = asyncio.run(run())

            self.assertIn("Микшер", preview["group_text"])
            self.assertNotIn("Праздник", preview["group_text"])
            self.assertEqual(
                [item["task"] for item in preview["personal_messages"]],
                ["Микшер"],
            )

    def test_task_loader_can_filter_assignments_to_one_sunday(self):
        from bot.app import _load_service_task_assignments

        target_sunday = date(2026, 10, 25)
        next_sunday = date(2026, 11, 1)
        parsed_events = {
            "sound": [
                SimpleNamespace(
                    event_date=target_sunday,
                    assignments=[
                        SimpleNamespace(
                            task="Звук",
                            name="Алексей",
                            is_informational=False,
                        ),
                        SimpleNamespace(
                            task="Праздник",
                            name="",
                            is_informational=True,
                        ),
                    ],
                ),
                SimpleNamespace(
                    event_date=next_sunday,
                    assignments=[
                        SimpleNamespace(
                            task="Звук", name="Мария",
                            is_informational=False,
                        ),
                        SimpleNamespace(
                            task="Звук", name="",
                            is_informational=True,
                        ),
                        SimpleNamespace(
                            task="Слайды", name="Иван",
                            is_informational=False,
                        ),
                    ],
                ),
            ]
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            credentials = SimpleNamespace(yandex_public_url="public-url")
            logger = logging.getLogger("test-service-date-filter")

            async def run():
                with (
                    patch(
                        "bot.app._next_sunday_in_tz",
                        side_effect=[target_sunday, next_sunday],
                    ),
                    patch("bot.app.download_public", new=AsyncMock()),
                    patch(
                        "bot.app.parse_service_events",
                        return_value=parsed_events,
                    ) as parse_events,
                ):
                    month_assignments = await _load_service_task_assignments(
                        credentials,
                        Path(temp_dir),
                        ZoneInfo("UTC"),
                        "sound",
                        logger,
                    )
                    sunday_assignments = await _load_service_task_assignments(
                        credentials,
                        Path(temp_dir),
                        ZoneInfo("UTC"),
                        "sound",
                        logger,
                        only_date=target_sunday,
                    )
                self.assertEqual(parse_events.call_count, 2)
                self.assertEqual(
                    [call.args[1] for call in parse_events.call_args_list],
                    [target_sunday, target_sunday],
                )
                return month_assignments, sunday_assignments

            month_assignments, sunday_assignments = asyncio.run(run())

        self.assertEqual(
            month_assignments,
            {
                "Звук": {
                    "names": ["Алексей", "Мария"],
                    "is_informational": False,
                },
                "Праздник": {"names": [], "is_informational": True},
                "Слайды": {"names": ["Иван"], "is_informational": False},
            },
        )
        self.assertEqual(
            sunday_assignments,
            {
                "Звук": {
                    "names": ["Алексей"],
                    "is_informational": False,
                },
                "Праздник": {"names": [], "is_informational": True},
            },
        )

    def test_malformed_template_is_reported_as_clear_value_error(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            registry = ServiceRegistry(Path(temp_dir) / "registry.sqlite3")
            registry.initialize(67890, -100)
            with closing(sqlite3.connect(registry.path)) as connection:
                connection.execute(
                    "UPDATE services SET group_template = ? WHERE id = ?",
                    ("{service_name.attribute}", "sound"),
                )
                connection.commit()
            with patch(
                "bot.app._load_service_task_assignments",
                new=AsyncMock(return_value={"Звук": ["Алексей"]}),
            ):
                with self.assertRaisesRegex(
                    ValueError, "Некорректный групповой шаблон"
                ):
                    asyncio.run(
                        _prepare_service_test_previews(
                            "sound",
                            SimpleNamespace(admin_tg_id=67890),
                            Path(temp_dir),
                            ZoneInfo("UTC"),
                            registry,
                            logging.getLogger("test-invalid-template"),
                        )
                    )

    def test_preview_renders_templates_under_guided_setup_overrides(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            registry = ServiceRegistry(Path(temp_dir) / "registry.sqlite3")
            registry.initialize(67890, -100)
            registry.set_responsible_contact(
                67890, "sound", "Микшер", "Алексей", 12345, "alex"
            )
            assignments = {"Микшер": ["Алексей"]}

            async def run():
                with patch(
                    "bot.app._load_service_task_assignments",
                    new=AsyncMock(return_value=assignments),
                ):
                    return await _prepare_service_test_previews(
                        "sound",
                        SimpleNamespace(admin_tg_id=67890),
                        Path(temp_dir),
                        ZoneInfo("UTC"),
                        registry,
                        logging.getLogger("test-service-template-overrides"),
                        group_template="Новый {service_name}: {task_assignments}",
                        personal_templates={
                            "Микшер": "{name} ({telegram_id}, @{username})"
                        },
                    )

            previews = asyncio.run(run())
            self.assertIn("Новый Звук", previews["group_text"])
            group_text = plain_text(previews["group_text"])
            self.assertIn("Алексей (@alex)", group_text)
            self.assertNotIn("12345", group_text)
            self.assertEqual(
                previews["personal_messages"][0]["text"],
                "Алексей (12345, @alex)",
            )


if __name__ == "__main__":
    unittest.main()
