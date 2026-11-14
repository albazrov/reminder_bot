import unittest

from bot.presentation import format_task_assignments
from bot.ui import plain_text, render_screen_chunks, user_link


class FormatTaskAssignmentsTest(unittest.TestCase):
    def setUp(self):
        self.assignments = {
            "Микшер": {"names": ["Алексей", "Мария", "Иван", "Олег"]},
        }
        self.contacts = {
            ("Микшер", "Алексей"): {"telegram_id": 12345, "username": "alex"},
            ("Микшер", "Мария"): {"telegram_id": 67890, "username": None},
            ("Микшер", "Иван"): {"telegram_id": None, "username": "ivan"},
        }

    def test_contacts_with_telegram_id_become_mentions(self):
        text = format_task_assignments(
            self.assignments, self.contacts, mark_unlinked=True
        )

        self.assertIn(f"• {user_link(12345, 'Алексей')}", text)
        self.assertIn(f"• {user_link(67890, 'Мария')}", text)
        self.assertEqual(
            plain_text(text),
            "🎚️ Микшер\n"
            "• Алексей\n"
            "• Мария\n"
            "• Иван\n"
            "• Олег · не привязан",
        )
        rendered = "".join(render_screen_chunks(text, limit=4096))
        self.assertIn('<a href="tg://user?id=12345">Алексей</a>', rendered)
        self.assertIn('<a href="tg://user?id=67890">Мария</a>', rendered)
        self.assertNotIn("@alex", rendered)
        self.assertNotIn("\x00", rendered)

    def test_unlinked_mark_is_optional(self):
        text = format_task_assignments(self.assignments, self.contacts)

        self.assertIn("• Олег", plain_text(text))
        self.assertNotIn("не привязан", text)

    def test_short_name_for_unbound_person_keeps_unlinked_status(self):
        text = format_task_assignments(
            {"Микшер": ["Олег"]}, self.contacts, mark_unlinked=True,
            display_names={"Олег": "О."},
        )
        self.assertIn("• О. · не привязан", plain_text(text))
        self.assertNotIn("Олег", plain_text(text))
        self.assertNotIn("LINK:", text)

    def test_senior_is_listed_before_shift_and_other_tasks_keep_order(self):
        assignments = {
            "в смене": {"names": ["Алексей", "Мария"]},
            "звук": {"names": ["Иван"]},
            "старший": {"names": ["Олег"]},
            "пироги": {"names": ["Вера"]},
        }

        self.assertEqual(
            plain_text(format_task_assignments(assignments)),
            "⭐ старший\n"
            "• Олег\n\n"
            "👥 в смене\n"
            "• Алексей\n"
            "• Мария\n\n"
            "🎚️ звук\n"
            "• Иван\n\n"
            "🥧 пироги\n"
            "• Вера",
        )


if __name__ == "__main__":
    unittest.main()


class InformationalLabelTest(unittest.TestCase):
    def test_label_and_notes(self):
        from bot.presentation import format_informational_notes, informational_label

        self.assertEqual(informational_label("2я часть", ["нет"]), "2я часть нет")
        self.assertEqual(informational_label("Праздник", [""]), "Праздник")
        self.assertEqual(
            format_informational_notes({
                "Причастие": {"names": ["В.П."], "is_informational": True},
                "звук": {"names": ["Алексей"], "is_informational": False},
            }),
            "📌 Причастие В.П.",
        )
