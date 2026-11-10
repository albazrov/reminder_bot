import stat
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from bot.service_registry import ServiceRegistry


class ServiceRegistryTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.path = Path(self.temp_dir.name) / "private" / "services.sqlite3"
        self.registry = ServiceRegistry(self.path)
        self.registry.initialize(1001, -10001)

    def test_add_group_reminder_with_task_is_atomic_and_task_unique(self):
        baseline = self.registry.list_group_reminders("sound")
        for task in ("спевка", "несуществующая"):
            with self.subTest(task=task), self.assertRaises(ValueError):
                self.registry.add_group_reminder(
                    1001, "sound", 1, "18:00", task=task
                )
        self.assertEqual(self.registry.list_group_reminders("sound"), baseline)
        first = self.registry.add_group_reminder(
            1001, "sound", 1, "18:00", "{task_responsibles}", task="звук"
        )
        self.assertEqual(self.registry.get_group_reminder(first)["task"], "звук")
        with self.assertRaises(ValueError):
            self.registry.add_group_reminder(
                1001, "sound", 1, "18:00", task="звук"
            )
        second = self.registry.add_group_reminder(
            1001, "sound", 1, "18:00", task="сборка"
        )
        self.assertEqual(self.registry.get_group_reminder(second)["task"], "сборка")
        with self.assertRaises(PermissionError):
            self.registry.add_group_reminder(
                9999, "sound", 2, "18:00", task="звук"
            )
        self.assertEqual(
            len(self.registry.list_group_reminders("sound")), len(baseline) + 2
        )

    def test_change_callback_runs_only_for_committed_writes(self):
        changes = []
        registry = ServiceRegistry(
            self.path, on_change=lambda: changes.append("changed")
        )
        registry.initialize(1001, -10001)
        changes.clear()

        registry.list_services()
        self.assertEqual(changes, [])

        registry.set_group_chat(1001, "sound", -10055, enabled=True)
        self.assertEqual(changes, ["changed"])

    def test_legacy_readiness_migration_only_backfills_completion_flags(self):
        closed = self.registry.create_readiness_run(
            "sound", "2026-09", status="open", dates=["2026-09-06"]
        )
        self.registry.close_readiness_run(closed["id"])
        opened = self.registry.create_readiness_run(
            "sound", "2026-11", status="open", dates=["2026-11-01"]
        )
        skipped = self.registry.create_readiness_run(
            "sound", "2026-10", status="skipped"
        )
        columns = (
            "poll_stopped", "result_sent", "closed_announced",
            "options_json", "reverse_poll",
        )
        with closing(sqlite3.connect(self.path)) as connection, connection:
            for column in columns:
                connection.execute(
                    f"ALTER TABLE readiness_runs DROP COLUMN {column}"
                )

        for _ in range(2):
            self.registry.initialize(1001, -10001)
            for original in (closed, opened, skipped):
                with self.subTest(status=original["status"]):
                    run = self.registry.get_readiness_run_by_id(original["id"])
                    self.assertEqual(run["options"], [])
                    self.assertFalse(run["reverse_poll"])
                    self.assertEqual(run["dates"], original["dates"])
                    for column in columns[:3]:
                        self.assertEqual(
                            run[column], int(run["status"] != "open")
                        )

    def test_readiness_migration_repairs_corrupt_options_preserving_valid_runs(self):
        broken = self.registry.create_readiness_run(
            "sound", "2026-10", status="open", dates=["2026-10-04"]
        )
        self.registry.close_readiness_run(broken["id"])
        options = [{"date": "2026-11-01", "task": "звук"}]
        valid = self.registry.create_readiness_run(
            "sound", "2026-11", status="open",
            options=options, reverse_poll=True,
        )
        self.registry.close_readiness_run(valid["id"])
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute(
                "UPDATE readiness_runs SET options_json = '1', reverse_poll = 1 "
                "WHERE id = ?", (broken["id"],)
            )

        for _ in range(2):
            self.registry.initialize(1001, -10001)
            repaired = self.registry.get_readiness_run_by_id(broken["id"])
            self.assertEqual(repaired["options"], [])
            self.assertFalse(repaired["reverse_poll"])
            self.assertEqual(repaired["dates"], broken["dates"])
            untouched = self.registry.get_readiness_run_by_id(valid["id"])
            self.assertEqual(untouched["options"], options)
            self.assertTrue(untouched["reverse_poll"])

    def test_bootstrap_seeds_catalog_and_configures_only_assembly(self):
        services = {service["id"]: service for service in self.registry.list_services()}

        self.assertEqual(len(services), 8)
        self.assertTrue(services["assembly"]["enabled"])
        self.assertEqual(services["assembly"]["group_chat_id"], -10001)
        for service_id in (
            "broadcast", "kitchen", "sound", "order",
            "office_cleaning", "books", "children",
        ):
            self.assertFalse(services[service_id]["enabled"])
            self.assertIsNone(services[service_id]["group_chat_id"])
        self.assertTrue(self.registry.is_super_admin(1001))
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(
            stat.S_IMODE(self.path.parent.stat().st_mode) & 0o077, 0
        )

    def test_catalog_tasks_are_seeded_and_admin_toggles_survive_restart(self):
        self.assertEqual(
            [
                (task["name"], task["enabled"])
                for task in self.registry.list_service_tasks("sound")
            ],
            [("звук", True), ("сборка", True), ("спевка", False)],
        )
        self.registry.set_service_task_enabled(1001, "sound", "спевка", True)
        self.registry.add_service_task(1001, "sound", "  Перевозка  техники ")

        self.registry.initialize(1001, -10001)

        tasks = self.registry.list_service_tasks("sound")
        self.assertEqual(
            [(task["name"], task["enabled"], task["source"]) for task in tasks],
            [
                ("звук", True, "catalog"),
                ("сборка", True, "catalog"),
                ("спевка", True, "catalog"),
                ("Перевозка техники", True, "admin"),
            ],
        )
        catalog = {
            service.id: service for service in self.registry.service_catalog()
        }
        self.assertIn(
            "Перевозка техники",
            [task.name for task in catalog["sound"].tasks],
        )
        self.assertEqual(
            [column.headers for column in catalog["sound"].columns],
            [("звук",), ("спевка",)],
        )

    def test_service_task_changes_by_service_admin_and_unique_names(self):
        self.registry.add_service_admin(1001, "sound", 2002)
        # Админ служения — своё служение, чужое — нет.
        self.registry.set_service_task_enabled(2002, "sound", "звук", False)
        self.registry.add_service_task(2002, "sound", "Новая")
        with self.assertRaises(PermissionError):
            self.registry.add_service_task(2002, "kitchen", "Новая")
        with self.assertRaises(PermissionError):
            self.registry.add_service_admin(2002, "sound", 3003)
        with self.assertRaises(ValueError):
            self.registry.add_service_task(1001, "sound", "Сборка оборудования")
        with self.assertRaises(ValueError):
            self.registry.add_service_task(1001, "sound", " ")
        with self.assertRaises(ValueError):
            self.registry.set_service_task_enabled(1001, "sound", "нет", True)

    def test_people_needed_validated_and_migrated(self):
        self.registry.add_service_admin(1001, "sound", 2002)
        with self.assertRaises(PermissionError):
            self.registry.set_service_task_people_needed(3003, "sound", "звук", 2)
        # Админ служения может; диапазон «3–4».
        self.registry.set_service_task_people_needed(2002, "sound", "звук", 3, 4)
        task = next(
            item for item in self.registry.list_service_tasks("sound")
            if item["name"] == "звук"
        )
        self.assertEqual((task["people_needed"], task["people_max"]), (3, 4))
        with self.assertRaises(ValueError):
            self.registry.set_service_task_people_needed(1001, "sound", "звук", 3, 2)
        for bad in (0, 6, True, "2"):
            with self.assertRaises(ValueError):
                self.registry.set_service_task_people_needed(
                    1001, "sound", "звук", bad
                )
        with self.assertRaises(ValueError):
            self.registry.set_service_task_people_needed(1001, "sound", "нет", 2)
        self.registry.set_service_task_people_needed(1001, "sound", "звук", 3)
        self.assertEqual(
            {
                task["name"]: task["people_needed"]
                for task in self.registry.list_service_tasks("sound")
            }["звук"],
            3,
        )
        # Старая база без колонки получает её со значением 1.
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("ALTER TABLE service_tasks DROP COLUMN people_needed")
            connection.commit()
        self.registry.initialize(1001, -10001)
        self.assertTrue(all(
            task["people_needed"] == 1
            for task in self.registry.list_service_tasks("sound")
        ))

    def test_services_missing_from_catalog_are_archived_not_deleted(self):
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                "INSERT INTO services (id, name, aliases_json, "
                "assignment_layout, enabled) "
                "VALUES ('slides', 'Слайды', '[]', 'single', 1)"
            )
        self.registry.initialize(1001, -10001)

        self.assertNotIn(
            "slides",
            [service["id"] for service in self.registry.list_services()],
        )
        with self.assertRaises(ValueError):
            self.registry.add_group_reminder(1001, "slides", 1, "18:00")
        with sqlite3.connect(self.path) as connection:
            archived = connection.execute(
                "SELECT archived, enabled FROM services WHERE id = 'slides'"
            ).fetchone()
        self.assertEqual(archived, (1, 0))

    def test_catalog_migration_renames_tasks_and_moves_slides_admins(self):
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                "INSERT INTO services (id, name, aliases_json, "
                "assignment_layout) "
                "VALUES ('slides', 'Слайды', '[]', 'single')"
            )
            connection.execute(
                "INSERT INTO service_admins (service_id, telegram_id, "
                "username, created_by, created_at) "
                "VALUES ('slides', 3003, 'slides_admin', 1001, 'now')"
            )
            for service_id, task, name, telegram_id in (
                ("assembly", "Проповедник", "Иван", 4001),
                ("assembly", "Ответственный", "Алексей", 4002),
                ("broadcast", "трансляция", "Даша", 4003),
                ("kitchen", "Кухня", "Ольга", 4004),
                ("order", "Орг. порядка", "Сергей", 4005),
                ("order", "в смене", "Сергей", 4005),
                ("sound", "сборка оборудования", "Денис", 4006),
                ("assembly", "Вторая часть", "Павел", 4007),
            ):
                connection.execute(
                    "INSERT INTO responsible_contacts (service_id, task, "
                    "name, telegram_id, username, updated_by, updated_at) "
                    "VALUES (?, ?, ?, ?, '', 1001, 'now')",
                    (service_id, task, name, telegram_id),
                )
            connection.execute(
                "INSERT INTO user_reminder_texts (actor_id, service_id, task) "
                "VALUES (4001, 'assembly', 'Проповедник')"
            )
            connection.execute(
                "UPDATE services SET personal_templates_json = ? "
                "WHERE id = 'assembly'",
                ('{"Проповедник": "{name}, проповедь {sunday_date}"}',),
            )
            connection.execute("DELETE FROM catalog_migrations")

        self.registry.initialize(1001, -10001)
        self.registry.initialize(1001, -10001)

        with sqlite3.connect(self.path) as connection:
            contacts = sorted(connection.execute(
                "SELECT service_id, task, name FROM responsible_contacts"
            ).fetchall())
            text_task = connection.execute(
                "SELECT task FROM user_reminder_texts WHERE actor_id = 4001"
            ).fetchone()[0]
        self.assertEqual(
            contacts,
            [
                ("assembly", "Вторая часть", "Павел"),
                ("assembly", "Ответственный по ХКЦ", "Алексей"),
                ("assembly", "Проповедь", "Иван"),
                ("broadcast", "Компьютер", "Даша"),
                ("kitchen", "в смене", "Ольга"),
                ("order", "в смене", "Сергей"),
                ("sound", "сборка", "Денис"),
            ],
        )
        self.assertEqual(text_task, "Проповедь")
        self.assertEqual(
            self.registry.get_service("assembly")["personal_templates"],
            {"Проповедь": "{name}, проповедь {sunday_date}"},
        )
        self.assertTrue(self.registry.is_service_admin(3003, "broadcast"))

    def _rerun_catalog_migration(self, *statements):
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                "INSERT OR IGNORE INTO services (id, name, aliases_json, "
                "assignment_layout) VALUES ('slides', 'Слайды', '[]', 'single')"
            )
            for statement, parameters in statements:
                connection.execute(statement, parameters)
            connection.execute("DELETE FROM catalog_migrations")
        self.registry.initialize(1001, -10001)

    @staticmethod
    def _contact(service_id, task, name, telegram_id):
        return (
            "INSERT INTO responsible_contacts (service_id, task, name, "
            "telegram_id, username, updated_by, updated_at) "
            "VALUES (?, ?, ?, ?, '', 1001, 'now')",
            (service_id, task, name, telegram_id),
        )

    def test_migration_keeps_contact_when_name_belongs_to_other_person(self):
        self._rerun_catalog_migration(
            self._contact("assembly", "Проповедник", "Иван", 4001),
            self._contact("assembly", "Проповедь", "Иван", 4009),
        )

        with sqlite3.connect(self.path) as connection:
            rows = sorted(connection.execute(
                "SELECT task, telegram_id FROM responsible_contacts "
                "WHERE service_id = 'assembly'"
            ).fetchall())
        self.assertEqual(rows, [("Проповедник", 4001), ("Проповедь", 4009)])

    def test_migration_keeps_same_person_bound_under_other_name(self):
        self._rerun_catalog_migration(
            self._contact("kitchen", "Кухня", "Оля", 4030),
            self._contact("kitchen", "в смене", "Ольга", 4030),
        )

        with sqlite3.connect(self.path) as connection:
            rows = sorted(connection.execute(
                "SELECT task, name FROM responsible_contacts "
                "WHERE telegram_id = 4030"
            ).fetchall())
        self.assertEqual(rows, [("Кухня", "Оля"), ("в смене", "Ольга")])

    def test_migration_merges_personal_rules_of_alias_and_canonical_task(self):
        with sqlite3.connect(self.path) as connection:
            connection.execute(*self._contact(
                "assembly", "Проповедник", "Иван", 4001
            ))
            connection.execute(
                "INSERT INTO user_reminder_texts (id, actor_id, service_id, "
                "task, body) VALUES (901, 4001, 'assembly', 'Проповедник', "
                "'Свой текст')"
            )
            connection.execute(
                "INSERT INTO user_reminder_texts (id, actor_id, service_id, "
                "task) VALUES (902, 4001, 'assembly', 'Проповедь')"
            )
            connection.execute(
                "INSERT INTO user_reminder_schedules (text_id, reminder_days, "
                "reminder_time) VALUES (901, 2, '10:00')"
            )
        self._rerun_catalog_migration()

        texts = self.registry.list_user_texts(4001, "assembly")
        self.assertEqual(
            [(text["id"], text["task"], text["body"]) for text in texts],
            [(902, "Проповедь", None)],
        )
        # Свой текст задачи перешёл в её правило (личные напоминания v2).
        self.assertEqual(
            [rule["body"] for rule in self.registry.list_schedules(902)],
            ["Свой текст"],
        )
        self.assertEqual(
            [
                (rule["reminder_days"], rule["reminder_time"])
                for rule in self.registry.list_schedules(902)
            ],
            [(2, "10:00")],
        )

    def test_migration_keeps_text_with_binding_left_under_old_task(self):
        with sqlite3.connect(self.path) as connection:
            connection.execute(*self._contact(
                "assembly", "Проповедник", "Иван", 4001
            ))
            connection.execute(*self._contact(
                "assembly", "Проповедь", "Иван", 4009
            ))
            connection.execute(
                "INSERT INTO user_reminder_texts (id, actor_id, service_id, "
                "task) VALUES (903, 4001, 'assembly', 'Проповедник')"
            )
            connection.execute(
                "INSERT INTO user_reminder_schedules (text_id, reminder_days, "
                "reminder_time) VALUES (903, 2, '10:00')"
            )
        self._rerun_catalog_migration()

        self.assertEqual(
            self.registry.list_user_tasks(4001, "assembly"), ["Проповедник"]
        )
        self.assertEqual(
            [
                (text["id"], text["task"])
                for text in self.registry.list_user_texts(4001, "assembly")
            ],
            [(903, "Проповедник")],
        )
        self.assertEqual(len(self.registry.list_schedules(903)), 1)

    def test_migration_moves_slides_people_to_broadcast(self):
        self._rerun_catalog_migration(
            self._contact("slides", "Слайды", "Анна", 4020),
            (
                "INSERT INTO user_reminder_texts (actor_id, service_id, task) "
                "VALUES (4020, 'slides', 'Слайды')",
                (),
            ),
            (
                "INSERT INTO user_notification_prefs (service_id, actor_id, "
                "enabled) VALUES ('slides', 4020, 1)",
                (),
            ),
        )

        self.assertEqual(self.registry.list_user_services(4020), ["broadcast"])
        self.assertEqual(
            self.registry.list_user_tasks(4020, "broadcast"), ["Слайды"]
        )
        self.assertEqual(
            [text["task"] for text in self.registry.list_user_texts(4020, "broadcast")],
            ["Слайды"],
        )
        self.assertTrue(
            self.registry.get_user_notifications_enabled(4020, "broadcast")
        )

    def test_archived_services_are_hidden_from_user_lists(self):
        self._rerun_catalog_migration(
            self._contact("slides", "Старая задача", "Анна", 4021),
        )
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                "UPDATE responsible_contacts SET service_id = 'slides' "
                "WHERE telegram_id = 4021"
            )
        self.registry.set_responsible_contact(
            1001, "sound", "звук", "Анна", 4021
        )

        self.assertEqual(self.registry.list_user_services(4021), ["sound"])
        self.assertEqual(
            {item["service_id"] for item in self.registry.list_user_assignments(4021)},
            {"sound"},
        )
        self.registry.set_user_notifications_all(4021, True)
        self.assertEqual(
            [
                item["service_id"]
                for item in self.registry.get_user_notifications_state(4021)[
                    "per_service"
                ]
            ],
            ["sound"],
        )

    def test_migration_keeps_conflicting_personal_template_under_alias(self):
        self._rerun_catalog_migration((
            "UPDATE services SET personal_templates_json = ? "
            "WHERE id = 'assembly'",
            ('{"Проповедник": "старый", "Проповедь": "новый", '
             '"ХКЦ": "ответственный"}',),
        ))

        self.assertEqual(
            self.registry.get_service("assembly")["personal_templates"],
            {
                "Проповедник": "старый",
                "Проповедь": "новый",
                "Ответственный по ХКЦ": "ответственный",
            },
        )

    def test_admin_task_positions_stay_unique_after_catalog_sync(self):
        self.registry.add_service_task(1001, "sound", "Перевозка")
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                "UPDATE service_tasks SET position = 0 "
                "WHERE service_id = 'sound' AND name = 'Перевозка'"
            )
        self.registry.initialize(1001, -10001)

        positions = [
            (task["name"], task["position"])
            for task in self.registry.list_service_tasks("sound")
        ]
        self.assertEqual(
            positions,
            [("звук", 0), ("сборка", 1), ("спевка", 2), ("Перевозка", 3)],
        )

    def test_catalog_sync_removes_tasks_dropped_from_catalog(self):
        self.registry.add_service_task(1001, "sound", "Перевозка")
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                "INSERT INTO service_tasks "
                "(service_id, name, position, enabled, aliases_json, source) "
                "VALUES ('sound', 'старая', 1, 1, '[]', 'catalog')"
            )
        self.registry.initialize(1001, -10001)

        positions = [
            (task["name"], task["position"])
            for task in self.registry.list_service_tasks("sound")
        ]
        self.assertEqual(
            positions,
            [("звук", 0), ("сборка", 1), ("спевка", 2), ("Перевозка", 3)],
        )

    def test_roster_sync_keeps_bot_bindings_and_skips_noop_writes(self):
        from bot.excel_parser import RosterMember

        self.registry.set_responsible_contact(
            1001, "sound", "звук", "Алексей", 2002, "alex_bot"
        )
        rosters = {
            "sound": [
                RosterMember("Алексей", ("звук", "сборка")),
                RosterMember("Пётр", ("сборка",)),
            ]
        }
        self.registry.sync_service_rosters(rosters)

        members = {
            member["name"]: member
            for member in self.registry.list_service_members("sound")
        }
        self.assertEqual(members["Алексей"]["telegram_id"], 2002)
        self.assertEqual(members["Алексей"]["username"], "alex_bot")
        self.assertEqual(members["Алексей"]["tasks"], ["звук", "сборка"])
        self.assertTrue(members["Алексей"]["in_list"])
        self.assertIsNone(members["Пётр"]["telegram_id"])
        contacts = {
            (contact["task"], contact["name"]): contact["telegram_id"]
            for contact in self.registry.list_responsible_contacts(
                1001, "sound"
            )
        }
        self.assertEqual(contacts[("сборка", "Алексей")], 2002)
        self.assertNotIn(("сборка", "Пётр"), contacts)

        changes = []
        self.registry.on_change = lambda: changes.append(True)
        self.registry.sync_service_rosters(rosters)
        self.assertEqual(changes, [])

        self.registry.sync_service_rosters({"sound": rosters["sound"][1:]})
        members = {
            member["name"]: member
            for member in self.registry.list_service_members("sound")
        }
        self.assertFalse(members["Алексей"]["in_list"])
        self.registry.sync_service_rosters({"kitchen": []})
        self.assertTrue(
            any(m["in_list"] for m in self.registry.list_service_members("sound"))
        )
        self.registry.sync_service_rosters({"sound": []})
        self.assertFalse(
            any(m["in_list"] for m in self.registry.list_service_members("sound"))
        )

    def test_member_binding_and_schedule_tasks(self):
        from bot.excel_parser import RosterMember

        self.registry.sync_service_rosters(
            {"sound": [RosterMember("Мария", ("звук",))]}
        )
        self.registry.bind_service_member(
            1001, "sound", "Мария", 5005, "@maria_x", tasks=("сборка",)
        )
        self.registry.ensure_member_contacts(
            "sound",
            {
                "спевка": {"names": ["Мария"], "is_informational": False},
                "Праздник": {"names": ["Мария"], "is_informational": True},
                "звук": ["Кто-то"],
            },
        )

        contacts = sorted(
            (contact["task"], contact["telegram_id"], contact["username"])
            for contact in self.registry.list_responsible_contacts(
                1001, "sound"
            )
            if contact["name"] == "Мария"
        )
        self.assertEqual(
            contacts,
            [
                ("звук", 5005, "maria_x"),
                ("сборка", 5005, "maria_x"),
                ("спевка", 5005, "maria_x"),
            ],
        )
        long_name = "Иван  " + "Очень" * 15
        self.registry.bind_service_member(1001, "sound", f" {long_name} ", 7007)
        self.assertIn(
            long_name,
            [m["name"] for m in self.registry.list_service_members("sound")],
        )
        with self.assertRaisesRegex(ValueError, "обязательно"):
            self.registry.bind_service_member(1001, "sound", "  ", 7007)
        with self.assertRaises(PermissionError):
            self.registry.bind_service_member(2002, "sound", "Мария", 6006)

    def test_rebinding_one_task_moves_all_tasks_of_the_name(self):
        self.registry.set_responsible_contact(1001, "sound", "звук", "Олег", 2002)
        self.registry.set_responsible_contact(1001, "sound", "сборка", "Олег", 2002)

        self.registry.set_responsible_contact(
            1001, "sound", "звук", "Олег", 3003, "oleg_new"
        )

        contacts = sorted(
            (contact["task"], contact["telegram_id"])
            for contact in self.registry.list_responsible_contacts(1001, "sound")
            if contact["name"] == "Олег"
        )
        self.assertEqual(contacts, [("звук", 3003), ("сборка", 3003)])

    def test_renamed_person_binding_follows_schedule_name(self):
        from bot.excel_parser import RosterMember

        self.registry.set_responsible_contact(1001, "sound", "звук", "Саша", 2002)
        self.registry.bind_service_member(1001, "sound", "Александр", 2002)
        self.registry.set_responsible_contact(1001, "sound", "сборка", "Шура", 2002)
        self.registry.ensure_member_contacts("sound", {"сборка": ["Александр"]})
        self.assertIn(
            ("сборка", "Шура"),
            [
                (c["task"], c["name"])
                for c in self.registry.list_responsible_contacts(1001, "sound")
            ],
        )
        self.registry.sync_service_rosters(
            {"sound": [RosterMember("Александр", ("звук",))]}
        )

        self.registry.ensure_member_contacts("sound", {"звук": ["Александр"]})

        names = [
            contact["name"]
            for contact in self.registry.list_responsible_contacts(1001, "sound")
            if contact["task"] == "звук"
        ]
        self.assertEqual(names, ["Александр"])

    def test_file_telegram_ids_are_dropped_once(self):
        self.registry.bind_service_member(1001, "sound", "Олег", 2002)
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                "INSERT INTO service_members (service_id, name, telegram_id, "
                "username, tasks_json, in_list, source, updated_at) VALUES "
                "('sound', 'Файл', 3003, 'from_file', '[\"звук\"]', 1, "
                "'excel', '2026-10-20T00:00:00+00:00')"
            )
            connection.execute(
                "INSERT INTO responsible_contacts (service_id, task, name, "
                "telegram_id, username, updated_by, updated_at) VALUES "
                "('sound', 'звук', 'Файл', 3003, 'from_file', 1001, "
                "'2026-10-20T00:00:00+00:00')"
            )
            connection.execute(
                "UPDATE service_members SET source = 'excel' WHERE name = 'Олег'"
            )
            connection.execute(
                "DELETE FROM catalog_migrations WHERE name = 'members-v2'"
            )

        self.registry.initialize(1001, -10001)

        members = {
            member["name"]: member["telegram_id"]
            for member in self.registry.list_service_members("sound")
        }
        self.assertIsNone(members["Файл"])
        self.assertEqual(members["Олег"], 2002)
        names = {
            contact["name"]
            for contact in self.registry.list_responsible_contacts(1001, "sound")
        }
        self.assertNotIn("Файл", names)

    def test_group_thread_belongs_to_bound_chat(self):
        self.registry.set_group_chat(1001, "sound", -10002, enabled=True)
        with self.assertRaisesRegex(ValueError, "не привязан"):
            self.registry.set_group_thread(1001, "sound", -19999, 7)
        with self.assertRaises(PermissionError):
            self.registry.set_group_thread(2002, "sound", -10002, 7)
        self.registry.add_service_admin(1001, "sound", 2002)
        self.registry.set_group_thread(2002, "sound", -10002, 7)
        self.assertEqual(self.registry.get_service("sound")["group_thread_id"], 7)

        self.registry.set_group_chat(1001, "sound", -10002, enabled=False)
        self.assertEqual(self.registry.get_service("sound")["group_thread_id"], 7)
        self.registry.set_group_chat(1001, "sound", -10003, enabled=True)
        self.assertIsNone(self.registry.get_service("sound")["group_thread_id"])

    def test_reminder_task_must_be_enabled_service_task(self):
        reminder = self.registry.list_group_reminders("sound")[0]
        with self.assertRaisesRegex(ValueError, "не из списка"):
            self.registry.set_group_reminder_task(1001, reminder["id"], "спевка")
        self.registry.set_group_reminder_task(1001, reminder["id"], "звук")
        self.assertEqual(
            self.registry.get_group_reminder(reminder["id"])["task"], "звук"
        )
        # Напоминание другой задачи в тот же слот — не дубликат.
        self.registry.add_group_reminder(
            1001, "sound", reminder["reminder_days"], reminder["reminder_time"]
        )
        with self.assertRaisesRegex(ValueError, "уже задано"):
            self.registry.add_group_reminder(
                1001, "sound", reminder["reminder_days"],
                reminder["reminder_time"],
            )
        self.assertIn(
            "{task_responsibles}",
            self.registry.resolve_group_text(
                self.registry.get_group_reminder(reminder["id"]),
                self.registry.get_service("sound"),
            ),
        )
        # Общее напоминание в том же слоте уже есть — сброс на «все задачи»
        # дал бы два общих напоминания в один слот.
        with self.assertRaisesRegex(ValueError, "уже задано"):
            self.registry.set_group_reminder_task(1001, reminder["id"], None)
        other = next(
            item for item in self.registry.list_group_reminders("sound")
            if item["id"] != reminder["id"]
        )
        with self.assertRaisesRegex(ValueError, "изменились"):
            self.registry.merge_group_reminder(
                1001, other["id"],
                expected_slot=(other["reminder_days"], other["reminder_time"]),
                source_id=reminder["id"],
                source_slot=(reminder["reminder_days"], reminder["reminder_time"]),
            )
        with self.assertRaisesRegex(ValueError, "не поддерживается"):
            self.registry.add_group_reminder(1001, "sound", 5, "07:00", "{task}")
        with self.assertRaisesRegex(ValueError, "не поддерживается"):
            self.registry.set_service_default_template(
                1001, "sound", "{task_responsibles}"
            )
        self.registry.update_group_reminder(
            1001, reminder["id"], body="{task}: {task_responsibles}"
        )
        with self.assertRaisesRegex(ValueError, "не поддерживается"):
            self.registry.set_group_reminder_task(1001, other["id"], None)
            self.registry.update_group_reminder(1001, other["id"], body="{task}")
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                "UPDATE group_reminders SET body = '{task}' WHERE id = ?",
                (other["id"],),
            )
        self.registry.update_group_reminder(1001, other["id"], enabled=False)
        self.registry.set_service_task_enabled(1001, "sound", "звук", False)
        detached = self.registry.get_group_reminder(reminder["id"])
        self.assertIsNone(detached["task"])
        self.assertFalse(detached["enabled"])

    def test_bootstrap_is_idempotent_and_does_not_promote_new_env_admin(self):
        self.registry.set_group_chat(1001, "sound", -10002, enabled=True)

        self.registry.initialize(2002, -19999)

        services = {service["id"]: service for service in self.registry.list_services()}
        self.assertTrue(self.registry.is_super_admin(1001))
        self.assertFalse(self.registry.is_super_admin(2002))
        self.assertEqual(services["assembly"]["group_chat_id"], -10001)
        self.assertEqual(services["sound"]["group_chat_id"], -10002)

    def test_initialization_preserves_assembly_chat_and_enabled_state(self):
        self.registry.set_group_chat(1001, "assembly", -10055, enabled=False)

        self.registry.initialize(1001, -19999)

        assembly = self.registry.get_service("assembly")
        self.assertEqual(assembly["group_chat_id"], -10055)
        self.assertFalse(assembly["enabled"])

    def test_disabling_service_preserves_its_group_chat(self):
        self.registry.set_group_chat(1001, "sound", -10055, enabled=True)

        self.registry.set_service_enabled(1001, "sound", False)

        sound = self.registry.get_service("sound")
        self.assertEqual(sound["group_chat_id"], -10055)
        self.assertFalse(sound["enabled"])

    def test_share_request_ids_remain_unique_after_registry_restart(self):
        first_request_id = self.registry.next_share_request_id()
        self.registry.initialize(1001, -19999)

        second_request_id = self.registry.next_share_request_id()

        self.assertGreater(second_request_id, first_request_id)

    def test_no_op_admin_changes_do_not_create_audit_entries(self):
        self.registry.add_service_admin(1001, "sound", 3003)
        self.registry.add_service_admin(1001, "sound", 3003)
        self.registry.remove_service_admin(1001, "sound", 9999)

        with closing(sqlite3.connect(self.path)) as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM configuration_audit "
                "WHERE action IN ('service_admin_added', 'service_admin_removed')"
            ).fetchone()[0]

        self.assertEqual(count, 1)

    def test_only_super_admin_can_manage_service_admins_and_contacts(self):
        with self.assertRaises(PermissionError):
            self.registry.add_service_admin(2002, "sound", 3003)

        self.registry.add_service_admin(1001, "sound", 3003)
        self.assertTrue(self.registry.is_service_admin(3003, "sound"))
        self.assertFalse(self.registry.is_service_admin(3003, "books"))
        self.assertEqual(self.registry.list_service_admins("sound"), [3003])

        self.registry.remove_service_admin(1001, "sound", 3003)
        self.assertFalse(self.registry.is_service_admin(3003, "sound"))

    def test_admins_can_be_shared_across_services_and_have_usernames(self):
        self.registry.add_service_admin(1001, "sound", 3003, "sound_user")
        self.registry.add_service_admin(1001, "sound", 4004, "@other_user")
        self.registry.add_service_admin(1001, "books", 3003, "sound_user")

        self.assertEqual(
            self.registry.list_service_admin_details("sound"),
            [
                {"telegram_id": 3003, "username": "sound_user"},
                {"telegram_id": 4004, "username": "other_user"},
            ],
        )
        self.assertEqual(
            self.registry.list_service_admin_details("books"),
            [{"telegram_id": 3003, "username": "sound_user"}],
        )
        self.assertTrue(self.registry.is_service_admin(3003, "sound"))
        self.assertTrue(self.registry.is_service_admin(3003, "books"))

    def test_existing_admin_username_can_be_refreshed_without_duplicate_audit(self):
        self.registry.add_service_admin(1001, "sound", 3003)

        self.registry.add_service_admin(1001, "sound", 3003, "@new_user")
        self.registry.add_service_admin(1001, "sound", 3003, "@new_user")

        self.assertEqual(
            self.registry.list_service_admin_details("sound"),
            [{"telegram_id": 3003, "username": "new_user"}],
        )
        with closing(sqlite3.connect(self.path)) as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM configuration_audit "
                "WHERE action = 'service_admin_added'"
            ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_initialization_adds_username_column_to_existing_admin_table(self):
        self.registry.add_service_admin(1001, "sound", 3003)
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("PRAGMA foreign_keys = OFF")
            connection.executescript(
                """
                ALTER TABLE service_admins RENAME TO service_admins_with_username;
                CREATE TABLE service_admins (
                    service_id TEXT NOT NULL REFERENCES services(id)
                        ON DELETE CASCADE,
                    telegram_id INTEGER NOT NULL CHECK (telegram_id > 0),
                    created_by INTEGER NOT NULL CHECK (created_by > 0),
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (service_id, telegram_id)
                );
                INSERT INTO service_admins (
                    service_id, telegram_id, created_by, created_at
                )
                SELECT service_id, telegram_id, created_by, created_at
                FROM service_admins_with_username;
                DROP TABLE service_admins_with_username;
                """
            )
            connection.commit()

        self.registry.initialize(1001, -10001)

        self.assertEqual(
            self.registry.list_service_admin_details("sound"),
            [{"telegram_id": 3003, "username": ""}],
        )

    def test_super_admin_configures_group_and_manual_telegram_mapping(self):
        self.registry.set_group_chat(1001, "sound", -10022, enabled=True)
        self.registry.set_responsible_contact(
            1001, "sound", "Микшер", "Алексей", 4004, "@alex"
        )
        self.registry.set_responsible_contact(
            1001, "sound", "Микшер", "Алексей", 5005, "alex_new"
        )

        services = {service["id"]: service for service in self.registry.list_services()}
        self.assertEqual(services["sound"]["group_chat_id"], -10022)
        self.assertTrue(services["sound"]["enabled"])
        self.assertEqual(
            self.registry.get_responsible_contact(
                "sound", "Микшер", "Алексей"
            ),
            {"telegram_id": 5005, "username": "alex_new"},
        )

    def test_notification_template_validation_rejects_invalid_fields_and_syntax(self):
        for group_template, personal_templates in (
            ("{unknown}", {}),
            ("{service_name", {}),
            ("{service_name!r}", {}),
            (None, {"Микшер": "{name.attribute}"}),
            (None, {"Микшер": "{missing}"}),
            (None, {"Микшер": "{name"}),
        ):
            with self.subTest(
                group_template=group_template,
                personal_templates=personal_templates,
            ):
                with self.assertRaises(ValueError):
                    self.registry.set_notification_settings(
                        1001,
                        "sound",
                        reminder_days=1,
                        reminder_time="18:00",
                        group_template=group_template,
                        personal_templates=personal_templates,
                    )

    def test_notification_template_validation_accepts_supported_fields(self):
        self.registry.set_notification_settings(
            1001,
            "sound",
            reminder_days=1,
            reminder_time="18:00",
            group_template="{service_name} {sunday_date} {task_assignments}",
            personal_templates={
                "Микшер": (
                    "{name} {task} {service_name} {sunday_date} "
                    "{telegram_id} {username}"
                )
            },
        )

        service = self.registry.get_service("sound")
        self.assertIn("{service_name}", service["group_template"])
        self.assertIn("{telegram_id}", service["personal_templates"]["Микшер"])

    def test_user_notification_preference_defaults_to_disabled_and_is_per_service(self):
        self.registry.set_responsible_contact(
            1001, "sound", "Микшер", "Алексей", 4004
        )
        self.registry.set_responsible_contact(
            1001, "books", "Слайды", "Алексей", 4004
        )

        self.assertFalse(
            self.registry.get_user_notifications_enabled(4004, "sound")
        )
        self.assertEqual(
            self.registry.list_users_with_notifications_enabled("sound"), []
        )

        self.registry.set_user_notifications_enabled(4004, "sound", True)

        self.assertTrue(
            self.registry.get_user_notifications_enabled(4004, "sound")
        )
        self.assertFalse(
            self.registry.get_user_notifications_enabled(4004, "books")
        )
        self.assertEqual(
            self.registry.list_users_with_notifications_enabled("sound"),
            [4004],
        )

        self.registry.set_user_notifications_enabled(4004, "sound", False)
        self.assertEqual(
            self.registry.list_users_with_notifications_enabled("sound"), []
        )

    def test_preference_table_migration_preserves_contacts_and_defaults_false(self):
        self.registry.set_responsible_contact(
            1001, "sound", "Микшер", "Алексей", 4004
        )
        with sqlite3.connect(self.path) as connection:
            connection.execute("DROP TABLE user_notification_prefs")

        self.registry.initialize(1001, -10001)

        self.assertEqual(
            self.registry.get_responsible_contact(
                "sound", "Микшер", "Алексей"
            )["telegram_id"],
            4004,
        )
        self.assertFalse(
            self.registry.get_user_notifications_enabled(4004, "sound")
        )

    def test_migrates_previous_preference_column_without_losing_enabled_value(self):
        self.registry.set_responsible_contact(
            1001, "sound", "Микшер", "Алексей", 4004
        )
        with sqlite3.connect(self.path) as connection:
            connection.execute("DROP TABLE user_notification_prefs")
            connection.execute(
                """
                CREATE TABLE user_notification_prefs (
                    service_id TEXT NOT NULL,
                    telegram_id INTEGER NOT NULL,
                    user_notifications_enabled INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (service_id, telegram_id)
                )
                """
            )
            connection.execute(
                "INSERT INTO user_notification_prefs VALUES ('sound', 4004, 1)"
            )

        self.registry.initialize(1001, -10001)

        self.assertTrue(
            self.registry.is_user_notifications_enabled(4004, "sound")
        )

    def test_notification_preference_requires_registered_user_and_boolean(self):
        with self.assertRaisesRegex(ValueError, "не привязан"):
            self.registry.set_user_notifications_enabled(4004, "sound", True)
        self.registry.set_responsible_contact(
            1001, "sound", "Микшер", "Алексей", 4004
        )
        with self.assertRaisesRegex(ValueError, "должен быть bool"):
            self.registry.set_user_notifications_enabled(4004, "sound", 1)

    def test_user_notification_state_and_bulk_toggle_keep_services_independent(self):
        self.registry.set_responsible_contact(
            1001, "sound", "Микшер", "Алексей", 4004
        )
        self.registry.set_responsible_contact(
            1001, "books", "Слайды", "Алексей", 4004
        )
        sound_text_id = self.registry.upsert_user_text(
            4004, "sound", "Микшер", None
        )
        self.registry.add_schedule(sound_text_id, 2, "09:00")
        self.registry.add_schedule(sound_text_id, 0, "18:00")
        slides_text_id = self.registry.upsert_user_text(
            4004, "books", "Слайды", None
        )
        self.registry.add_schedule(slides_text_id, 1, "07:00")

        self.assertEqual(
            self.registry.list_user_services(4004), ["books", "sound"]
        )
        self.assertEqual(
            self.registry.get_user_notifications_state(4004),
            {
                "total_services": 2,
                "enabled_count": 0,
                "disabled_count": 2,
                "per_service": [
                    {
                        "service_id": "books",
                        "enabled": False,
                        "texts_count": 1,
                        "schedules_count": 1,
                    },
                    {
                        "service_id": "sound",
                        "enabled": False,
                        "texts_count": 1,
                        "schedules_count": 2,
                    },
                ],
            },
        )

        self.registry.set_user_notifications_all(4004, True)
        self.assertEqual(
            self.registry.get_user_notifications_state(4004)["enabled_count"],
            2,
        )
        self.registry.set_user_notifications_all(4004, False)
        self.assertEqual(
            self.registry.get_user_notifications_state(4004)["enabled_count"],
            0,
        )
        self.assertEqual(len(self.registry.list_schedules(sound_text_id)), 2)
        self.assertEqual(len(self.registry.list_schedules(slides_text_id)), 1)

    def test_user_task_list_only_exposes_the_callers_assignments(self):
        self.registry.set_responsible_contact(
            1001, "sound", "Микшер", "Алексей", 4004
        )
        self.registry.set_responsible_contact(
            1001, "sound", "Свет", "Борис", 5005
        )

        self.assertEqual(
            self.registry.list_user_tasks(4004, "sound"), ["Микшер"]
        )
        self.assertEqual(self.registry.list_user_tasks(6006, "sound"), [])

    def test_user_text_fallback_override_and_schedule_cascade(self):
        self.registry.set_responsible_contact(
            1001, "sound", "Микшер", "Алексей", 4004
        )
        template = "Default {name} for {task} at {service_name}, {sunday_date}"
        text_id = self.registry.upsert_user_text(
            4004, "sound", "Микшер", None
        )
        self.assertEqual(
            self.registry.resolve_text_for_user(
                4004, "sound", "Микшер", {"Микшер": template}
            ),
            template,
        )
        schedule = self.registry.add_schedule(text_id, 2, "09:00")
        self.registry.add_schedule(text_id, 0, "18:00")
        self.assertEqual(len(self.registry.list_schedules(text_id)), 2)

        custom = "Personal {name}: {task}, {sunday_date}"
        self.registry.upsert_user_text(4004, "sound", "Микшер", custom)
        self.assertEqual(
            self.registry.resolve_text_for_user(
                4004, "sound", "Микшер", {"Микшер": template}
            ),
            custom,
        )
        self.registry.update_schedule(schedule["id"], enabled=False)
        self.assertFalse(
            self.registry.list_schedules(text_id)[0]["enabled"]
        )
        self.registry.delete_user_text(4004, "sound", "Микшер")
        self.assertIsNone(self.registry.get_user_text(4004, "sound", "Микшер"))
        with sqlite3.connect(self.path) as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM user_reminder_schedules "
                "WHERE text_id = ?",
                (text_id,),
            ).fetchone()[0]
        self.assertEqual(count, 0)

    def test_ensure_user_text_preserves_custom_body(self):
        self.registry.set_responsible_contact(
            1001, "sound", "Микшер", "Алексей", 4004
        )
        custom = "Personal {name}: {task}"
        text_id = self.registry.upsert_user_text(
            4004, "sound", "Микшер", custom
        )

        self.assertEqual(
            self.registry.ensure_user_text(4004, "sound", "Микшер"),
            text_id,
        )
        self.assertEqual(
            self.registry.get_user_text(4004, "sound", "Микшер")["body"],
            custom,
        )

    def test_reset_user_text_restores_default_body(self):
        self.registry.set_responsible_contact(
            1001, "sound", "Микшер", "Алексей", 4004
        )
        self.registry.upsert_user_text(
            4004, "sound", "Микшер", "Personal {name}: {task}"
        )

        self.registry.reset_user_text(4004, "sound", "Микшер")

        self.assertIsNone(
            self.registry.get_user_text(4004, "sound", "Микшер")["body"]
        )

    def test_changing_user_schedule_time_clears_only_its_delivery_history(self):
        self.registry.set_responsible_contact(
            1001, "sound", "Микшер", "Алексей", 4004
        )
        text_id = self.registry.ensure_user_text(4004, "sound", "Микшер")
        first = self.registry.add_schedule(text_id, 3, "09:00")
        second = self.registry.add_schedule(text_id, 1, "18:00")
        event_date = "2026-10-18"
        self.registry.mark_user_reminder_delivered(first["id"], event_date)
        self.registry.mark_user_reminder_delivered(second["id"], event_date)

        self.registry.update_schedule(first["id"], days=2)

        self.assertFalse(
            self.registry.is_user_reminder_delivered(first["id"], event_date)
        )
        self.assertTrue(
            self.registry.is_user_reminder_delivered(second["id"], event_date)
        )
        self.registry.mark_user_reminder_delivered(first["id"], event_date)
        self.registry.update_schedule(first["id"], enabled=False)
        self.assertTrue(
            self.registry.is_user_reminder_delivered(first["id"], event_date)
        )

    def test_multiple_reminders_keep_ids_and_update_independently(self):
        current = self.registry.get_service("sound")["reminders"][0]
        self.registry.set_notification_settings(
            1001,
            "sound",
            reminder_days=3,
            reminder_time="18:00",
            group_template=None,
            personal_templates={},
            reminders=[
                {
                    "id": current["id"],
                    "reminder_days": 3,
                    "reminder_time": "18:00",
                },
                {"reminder_days": 1, "reminder_time": "09:15"},
            ],
        )

        reminders = self.registry.get_service("sound")["reminders"]
        self.assertEqual(len(reminders), 2)
        self.assertEqual(
            next(item["id"] for item in reminders if item["reminder_days"] == 3),
            current["id"],
        )
        self.assertEqual(
            [(item["reminder_days"], item["reminder_time"]) for item in reminders],
            [(1, "09:15"), (3, "18:00")],
        )

    def test_intentionally_empty_reminder_list_stays_empty_after_restart(self):
        self.registry.set_notification_settings(
            1001,
            "sound",
            reminder_days=1,
            reminder_time="18:00",
            group_template=None,
            personal_templates={},
            reminders=[],
        )

        self.registry.initialize(1001, -10001)

        self.assertEqual(
            self.registry.get_service("sound")["reminders"], []
        )

    def test_merge_group_reminder_updates_target_and_removes_source(self):
        target_id = self.registry.list_group_reminders("sound")[0]["id"]
        source_id = self.registry.add_group_reminder(
            1001, "sound", 2, "09:00", "Второй"
        )

        self.registry.merge_group_reminder(
            1001, target_id, "Первый\n\nВторой",
            expected_slot=(1, "18:00"),
            source_id=source_id, source_slot=(2, "09:00"),
        )

        self.assertEqual(
            [
                (item["id"], item["body"])
                for item in self.registry.list_group_reminders("sound")
            ],
            [(target_id, "Первый\n\nВторой")],
        )
        with sqlite3.connect(self.path) as connection:
            action = connection.execute(
                "SELECT action FROM configuration_audit ORDER BY id DESC"
            ).fetchone()[0]
        self.assertEqual(action, "group_reminder_merged")

    def test_reminder_sharing_slot_keeps_text_and_toggle_editable(self):
        target_id = self.registry.list_group_reminders("sound")[0]["id"]
        self.registry.update_group_reminder(1001, target_id, enabled=False)
        live_id = self.registry.add_group_reminder(
            1001, "sound", 1, "18:00", allow_duplicate=True
        )

        self.registry.update_group_reminder(1001, live_id, body="Новый")
        self.registry.update_group_reminder(1001, live_id, body=None)
        self.registry.update_group_reminder(1001, live_id, enabled=False)

        reminder = self.registry.get_group_reminder(live_id)
        self.assertIsNone(reminder["body"])
        self.assertFalse(reminder["enabled"])
        self.registry.update_group_reminder(1001, target_id, enabled=True)
        with self.assertRaisesRegex(ValueError, "уже задано"):
            self.registry.update_group_reminder(1001, live_id, enabled=True)
        with self.assertRaisesRegex(ValueError, "уже задано"):
            self.registry.update_group_reminder(
                1001,
                self.registry.add_group_reminder(1001, "sound", 2, "09:00"),
                days=1,
                time="18:00",
            )

    def test_merge_group_reminder_without_body_keeps_target_text(self):
        target_id = self.registry.list_group_reminders("sound")[0]["id"]
        source_id = self.registry.add_group_reminder(
            1001, "sound", 1, "18:00", allow_duplicate=True
        )

        self.registry.merge_group_reminder(
            1001, target_id, expected_slot=(1, "18:00"),
            source_id=source_id, source_slot=(1, "18:00"),
        )

        reminders = self.registry.list_group_reminders("sound")
        self.assertEqual([item["id"] for item in reminders], [target_id])
        self.assertIsNone(reminders[0]["body"])

    def test_merge_group_reminder_keeps_source_delivery_records(self):
        target_id = self.registry.list_group_reminders("sound")[0]["id"]
        source_id = self.registry.add_group_reminder(
            1001, "sound", 2, "09:00", "Второй"
        )
        self.registry.mark_group_reminder_delivered(
            source_id, "2026-10-25", "group:-10001"
        )

        self.registry.merge_group_reminder(
            1001, target_id, "Первый\n\nВторой",
            expected_slot=(1, "18:00"),
            source_id=source_id, source_slot=(2, "09:00"),
        )

        self.assertTrue(
            self.registry.is_group_reminder_delivered(
                target_id, "2026-10-25", "group:-10001"
            )
        )

    def test_merge_group_reminder_rejects_changed_or_disabled_reminders(self):
        target_id = self.registry.list_group_reminders("sound")[0]["id"]
        source_id = self.registry.add_group_reminder(
            1001, "sound", 2, "09:00", "Второй"
        )
        self.registry.update_group_reminder(1001, source_id, days=3)
        with self.assertRaises(ValueError):
            self.registry.merge_group_reminder(
                1001, target_id, "Новый", expected_slot=(1, "18:00"),
                source_id=source_id, source_slot=(2, "09:00"),
            )
        with self.assertRaises(ValueError):
            self.registry.merge_group_reminder(
                1001, target_id, "Новый", expected_slot=(1, "19:00"),
            )
        self.registry.update_group_reminder(1001, target_id, enabled=False)
        with self.assertRaises(ValueError):
            self.registry.merge_group_reminder(
                1001, target_id, "Новый", expected_slot=(1, "18:00"),
            )

        self.assertIsNone(self.registry.get_group_reminder(target_id)["body"])
        self.assertIsNotNone(self.registry.get_group_reminder(source_id))

    def test_merge_group_reminder_is_atomic_and_checks_service(self):
        target_id = self.registry.list_group_reminders("sound")[0]["id"]
        other_service_id = self.registry.list_group_reminders("kitchen")[0][
            "id"
        ]

        with self.assertRaises(ValueError):
            self.registry.merge_group_reminder(
                1001, target_id, "Новый", expected_slot=(1, "18:00"),
                source_id=other_service_id, source_slot=(1, "18:00"),
            )
        with self.assertRaises(ValueError):
            self.registry.merge_group_reminder(
                1001, target_id, "Неизвестное {поле}",
                expected_slot=(1, "18:00"),
            )
        with self.assertRaises(PermissionError):
            self.registry.merge_group_reminder(
                2002, target_id, "Чужой", expected_slot=(1, "18:00"),
            )

        self.assertIsNone(self.registry.get_group_reminder(target_id)["body"])
        self.assertIsNotNone(self.registry.get_group_reminder(other_service_id))

    def test_duplicate_reminders_keep_independent_delivery_records(self):
        current = self.registry.get_service("sound")["reminders"][0]
        duplicate_rules = [
            {
                "id": current["id"],
                "reminder_days": 2,
                "reminder_time": "17:30",
            },
            {"reminder_days": 2, "reminder_time": "17:30"},
        ]
        with self.assertRaisesRegex(ValueError, "уже задано"):
            self.registry.set_notification_settings(
                1001,
                "sound",
                reminder_days=2,
                reminder_time="17:30",
                group_template=None,
                personal_templates={},
                reminders=duplicate_rules,
            )

        self.registry.set_notification_settings(
            1001,
            "sound",
            reminder_days=2,
            reminder_time="17:30",
            group_template=None,
            personal_templates={},
            reminders=duplicate_rules,
            allow_duplicate_reminders=True,
        )
        reminders = self.registry.get_service("sound")["reminders"]
        event_date = "2026-10-18"
        self.registry.mark_group_reminder_delivered(
            reminders[0]["id"], event_date, "group:-10001"
        )
        self.assertFalse(
            self.registry.is_group_reminder_delivered(
                reminders[1]["id"], event_date, "group:-10001"
            )
        )
        self.registry.mark_group_reminder_delivered(
            reminders[1]["id"], event_date, "group:-10001"
        )
        self.assertTrue(
            self.registry.is_group_reminder_delivered(
                reminders[1]["id"], event_date, "group:-10001"
            )
        )

    def test_editing_reminder_keeps_delivery_history_for_same_identity(self):
        reminder = self.registry.get_service("sound")["reminders"][0]
        event_date = "2026-10-18"
        recipient = "group:-10001"
        self.registry.mark_group_reminder_delivered(
            reminder["id"], event_date, recipient
        )

        self.registry.set_notification_settings(
            1001,
            "sound",
            reminder_days=2,
            reminder_time="09:00",
            group_template=None,
            personal_templates={},
            reminders=[
                {
                    "id": reminder["id"],
                    "reminder_days": 2,
                    "reminder_time": "09:00",
                }
            ],
        )

        edited = self.registry.get_service("sound")["reminders"][0]
        self.assertEqual(edited["id"], reminder["id"])
        self.assertTrue(
            self.registry.is_group_reminder_delivered(
                edited["id"], event_date, recipient
            )
        )

    def test_replacing_rule_with_new_identity_does_not_carry_delivery_history(self):
        reminder = self.registry.get_service("sound")["reminders"][0]
        event_date = "2026-10-18"
        recipient = "group:-10001"
        self.registry.mark_group_reminder_delivered(
            reminder["id"], event_date, recipient
        )

        self.registry.set_notification_settings(
            1001,
            "sound",
            reminder_days=reminder["reminder_days"],
            reminder_time=reminder["reminder_time"],
            group_template=None,
            personal_templates={},
            reminders=[
                {
                    "id": None,
                    "reminder_days": reminder["reminder_days"],
                    "reminder_time": reminder["reminder_time"],
                }
            ],
        )

        replacement = self.registry.get_service("sound")["reminders"][0]
        self.assertNotEqual(replacement["id"], reminder["id"])
        self.assertFalse(
            self.registry.is_group_reminder_delivered(
                replacement["id"], event_date, recipient
            )
        )

    def test_swapping_two_reminder_schedules_preserves_original_histories(self):
        first = self.registry.get_service("sound")["reminders"][0]
        self.registry.set_notification_settings(
            1001,
            "sound",
            reminder_days=first["reminder_days"],
            reminder_time=first["reminder_time"],
            group_template=None,
            personal_templates={},
            reminders=[
                {
                    "id": first["id"],
                    "reminder_days": 1,
                    "reminder_time": "08:00",
                },
                {"reminder_days": 3, "reminder_time": "18:00"},
            ],
        )
        first, second = self.registry.get_service("sound")["reminders"]
        event_date = "2026-10-18"
        self.registry.mark_group_reminder_delivered(
            first["id"], event_date, "group:-10001"
        )
        self.registry.mark_group_reminder_delivered(
            second["id"], event_date, "personal:task:2002"
        )

        self.registry.set_notification_settings(
            1001,
            "sound",
            reminder_days=first["reminder_days"],
            reminder_time=first["reminder_time"],
            group_template=None,
            personal_templates={},
            reminders=[
                {
                    "id": first["id"],
                    "reminder_days": second["reminder_days"],
                    "reminder_time": second["reminder_time"],
                },
                {
                    "id": second["id"],
                    "reminder_days": first["reminder_days"],
                    "reminder_time": first["reminder_time"],
                },
            ],
        )

        self.assertFalse(
            self.registry.is_group_reminder_delivered(
                first["id"], event_date, "personal:task:2002"
            )
        )
        self.assertTrue(
            self.registry.is_group_reminder_delivered(
                first["id"], event_date, "group:-10001"
            )
        )
        self.assertFalse(
            self.registry.is_group_reminder_delivered(
                second["id"], event_date, "group:-10001"
            )
        )
        self.assertTrue(
            self.registry.is_group_reminder_delivered(
                second["id"], event_date, "personal:task:2002"
            )
        )

    def test_legacy_reminders_and_deliveries_migrate_once(self):
        collision_id = self.registry.list_group_reminders("assembly")[0]["id"]
        with self.registry._connection() as connection:
            connection.execute("DELETE FROM group_reminders")
            connection.execute("DELETE FROM group_reminder_migrations")
            connection.execute("DELETE FROM service_reminders")
            connection.execute(
                "INSERT INTO group_reminders "
                "(id, service_id, body, reminder_days, reminder_time) "
                "VALUES (?, 'assembly', NULL, 0, '10:00')",
                (collision_id,),
            )
            connection.execute(
                "INSERT INTO service_reminders "
                "(id, service_id, reminder_days, reminder_time) "
                "VALUES (?, 'sound', 3, '18:00'), "
                "(102, 'sound', 3, '18:00'), "
                "(103, 'sound', 1, '09:15')",
                (collision_id,),
            )
            connection.execute(
                "INSERT INTO service_reminder_deliveries "
                "(service_id, reminder_id, event_date, recipient_key, "
                "delivered_at) VALUES "
                "('sound', ?, '2026-10-18', 'group:-10001', "
                "'2026-10-15T10:00:00+00:00'), "
                "('sound', 102, '2026-10-18', 'group:-10001', "
                "'2026-10-15T10:01:00+00:00')",
                (collision_id,),
            )

        self.registry.initialize(1001, -10001)
        reminders = self.registry.list_group_reminders("sound")
        self.assertEqual(
            [(item["reminder_days"], item["reminder_time"]) for item in reminders],
            [(1, "09:15"), (3, "18:00")],
        )
        migrated = next(item for item in reminders if item["reminder_days"] == 3)
        self.assertTrue(
            self.registry.is_group_reminder_delivered(
                migrated["id"], "2026-10-18", "group:-10001"
            )
        )
        self.assertNotEqual(migrated["id"], collision_id)
        self.assertEqual(
            self.registry.get_group_reminder(collision_id)["service_id"],
            "assembly",
        )
        self.assertTrue(
            self.registry.is_service_reminder_delivered(
                "sound", collision_id, "2026-10-18", "group:-10001"
            )
        )
        self.registry.mark_service_reminder_delivered(
            "sound", collision_id, "2026-10-18", "group:legacy-api"
        )
        self.assertTrue(
            self.registry.is_group_reminder_delivered(
                migrated["id"], "2026-10-18", "group:legacy-api"
            )
        )
        with self.registry._connection() as connection:
            marker_count = connection.execute(
                "SELECT COUNT(*) FROM group_reminder_migrations "
                "WHERE name = 'service-reminders-to-group-reminders'"
            ).fetchone()[0]
        self.assertEqual(marker_count, 1)

        self.registry.initialize(1001, -10001)
        self.assertEqual(self.registry.list_group_reminders("sound"), reminders)

    def test_recreated_reminder_does_not_inherit_legacy_deliveries(self):
        event_date = "2026-10-19"
        recipient_key = "group:-10001"
        with self.registry._connection() as connection:
            connection.execute("DELETE FROM group_reminders")
            connection.execute(
                "DELETE FROM group_reminder_migrations "
                "WHERE name = 'service-reminders-to-group-reminders'"
            )
            connection.execute("DELETE FROM service_reminders")
            connection.execute("DELETE FROM service_reminder_deliveries")
            connection.execute(
                "INSERT INTO service_reminders "
                "(service_id, reminder_days, reminder_time) "
                "VALUES ('assembly', 1, '09:00')"
            )
            legacy_id = connection.execute(
                "SELECT id FROM service_reminders WHERE service_id = 'assembly'"
            ).fetchone()["id"]
            connection.execute(
                "INSERT INTO service_reminder_deliveries "
                "(service_id, reminder_id, event_date, recipient_key, "
                "delivered_at) "
                "VALUES ('assembly', ?, ?, ?, '2026-10-18T09:00:00')",
                (legacy_id, event_date, recipient_key),
            )

        self.registry.initialize(1001, -10001)
        migrated = self.registry.list_group_reminders("assembly")
        self.assertEqual(len(migrated), 1)
        migrated_id = migrated[0]["id"]
        self.assertTrue(
            self.registry.is_group_reminder_delivered(
                migrated_id, event_date, recipient_key
            )
        )
        with self.registry._connection() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT reminder_id FROM group_reminder_legacy_ids "
                    "WHERE legacy_id = ?",
                    (legacy_id,),
                ).fetchone()["reminder_id"],
                migrated_id,
            )

        self.registry.delete_group_reminder(1001, migrated_id)
        replacement_id = self.registry.add_group_reminder(
            1001, "assembly", 1, "09:00"
        )
        self.assertNotEqual(replacement_id, migrated_id)

        self.registry.initialize(1001, -10001)

        self.assertEqual(
            self.registry.list_group_reminders("assembly")[0]["id"],
            replacement_id,
        )
        self.assertFalse(
            self.registry.is_group_reminder_delivered(
                replacement_id, event_date, recipient_key
            )
        )
        with self.registry._connection() as connection:
            self.assertIsNone(
                connection.execute(
                    "SELECT 1 FROM group_reminder_legacy_ids "
                    "WHERE legacy_id = ?",
                    (legacy_id,),
                ).fetchone()
            )

    def test_compatibility_setter_updates_primary_and_preserves_other_reminders(self):
        original = self.registry.list_group_reminders("sound")
        second_id = self.registry.add_group_reminder(
            1001, "sound", 3, "09:15", "Custom {service_name}"
        )
        self.registry.set_notification_settings(
            1001,
            "sound",
            reminder_days=2,
            reminder_time="17:30",
            group_template=None,
            personal_templates={},
        )

        current = self.registry.list_group_reminders("sound")
        primary = next(item for item in current if item["id"] == original[0]["id"])
        second = next(item for item in current if item["id"] == second_id)
        self.assertEqual(
            (primary["reminder_days"], primary["reminder_time"]),
            (2, "17:30"),
        )
        self.assertEqual(
            (second["reminder_days"], second["reminder_time"], second["body"]),
            (3, "09:15", "Custom {service_name}"),
        )

    def test_compatibility_settings_update_rolls_back_on_stale_reminder_id(self):
        before = self.registry.get_service("sound")
        reminders_before = self.registry.list_group_reminders("sound")
        with self.assertRaisesRegex(ValueError, "устарело"):
            self.registry.set_notification_settings(
                1001,
                "sound",
                reminder_days=4,
                reminder_time="16:30",
                group_template="New {service_name}",
                personal_templates={"Микшер": "New {name}"},
                reminders=[
                    {
                        "id": 999999,
                        "reminder_days": 4,
                        "reminder_time": "16:30",
                    }
                ],
            )

        after = self.registry.get_service("sound")
        self.assertEqual(after["reminder_days"], before["reminder_days"])
        self.assertEqual(after["reminder_time"], before["reminder_time"])
        self.assertEqual(after["group_template"], before["group_template"])
        self.assertEqual(
            after["personal_templates"], before["personal_templates"]
        )
        self.assertEqual(
            self.registry.list_group_reminders("sound"), reminders_before
        )

    def test_group_reminder_text_and_schedule_are_independent_records(self):
        default_template = "Общее {service_name} · {sunday_date}"
        self.registry.set_service_default_template(
            1001, "sound", default_template
        )
        reminders = self.registry.list_group_reminders("sound")
        first = reminders[0]
        second_id = self.registry.add_group_reminder(
            1001, "sound", 3, "09:15", "Свой текст {task_assignments}"
        )
        second = self.registry.get_group_reminder(second_id)
        service = self.registry.get_service("sound")

        self.assertEqual(
            self.registry.resolve_group_text(first, service),
            default_template,
        )
        self.assertEqual(
            self.registry.resolve_group_text(second, service),
            "Свой текст {task_assignments}",
        )

        self.registry.update_group_reminder(
            1001, first["id"], days=2, time="17:30", enabled=False
        )
        updated = self.registry.get_group_reminder(first["id"])
        self.assertEqual(updated["reminder_days"], 2)
        self.assertEqual(updated["reminder_time"], "17:30")
        self.assertFalse(updated["enabled"])
        self.assertEqual(
            self.registry.resolve_group_text(updated, service),
            default_template,
        )

        self.registry.update_group_reminder(
            1001, first["id"], body="Напоминание {service_name}"
        )
        updated = self.registry.get_group_reminder(first["id"])
        self.assertEqual(
            self.registry.resolve_group_text(updated, service),
            "Напоминание {service_name}",
        )
        self.registry.update_group_reminder(
            1001, first["id"], body=None
        )
        self.registry.set_service_default_template(1001, "sound", None)
        service = self.registry.get_service("sound")
        self.assertEqual(
            self.registry.resolve_group_text(
                self.registry.get_group_reminder(first["id"]), service
            ),
            "Напоминание о служении «{service_name}» в воскресенье "
            "{sunday_date}:\n{task_assignments}",
        )

    def test_legacy_unique_reminder_constraint_is_migrated(self):
        legacy_path = Path(self.temp_dir.name) / "legacy" / "services.sqlite3"
        legacy_path.parent.mkdir(mode=0o700)
        with sqlite3.connect(legacy_path) as connection:
            connection.execute(
                """
                CREATE TABLE service_reminders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    service_id TEXT NOT NULL,
                    reminder_days INTEGER NOT NULL,
                    reminder_time TEXT NOT NULL,
                    UNIQUE(service_id, reminder_days, reminder_time)
                )
                """
            )
        legacy_path.chmod(0o600)
        legacy_registry = ServiceRegistry(legacy_path)
        legacy_registry.initialize(1001, -10001)
        existing = legacy_registry.get_service("sound")["reminders"][0]
        legacy_registry.set_notification_settings(
            1001,
            "sound",
            reminder_days=1,
            reminder_time="18:00",
            group_template=None,
            personal_templates={},
            reminders=[
                {
                    "id": existing["id"],
                    "reminder_days": 1,
                    "reminder_time": "18:00",
                },
                {"reminder_days": 1, "reminder_time": "18:00"},
            ],
            allow_duplicate_reminders=True,
        )
        self.assertEqual(
            len(legacy_registry.get_service("sound")["reminders"]), 2
        )

    def test_rebinding_contact_replaces_old_name_and_user_rows(self):
        self.registry.set_responsible_contact(
            1001, "sound", "Микшер", "Алексей", 4004, "alex_one"
        )
        self.registry.set_responsible_contact(
            1001, "sound", "Микшер", "Алексей", 5005, "alex_two"
        )
        self.registry.set_responsible_contact(
            1001, "sound", "Микшер", "Павел", 5005, "pavel"
        )

        contacts = self.registry.list_responsible_contacts(1001, "sound")
        self.assertEqual(
            [
                (item["name"], item["telegram_id"], item["username"])
                for item in contacts
                if item["task"] == "Микшер"
            ],
            [("Павел", 5005, "pavel")],
        )

    def test_initialization_cleans_existing_duplicate_contact_names(self):
        self.registry.set_responsible_contact(
            1001, "sound", "Микшер", "Алексей", 4004, "alex_old"
        )
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute(
                "DROP INDEX idx_responsible_contacts_name"
            )
            connection.execute(
                """
                INSERT INTO responsible_contacts (
                    service_id, task, name, telegram_id, username,
                    updated_by, updated_at
                ) VALUES ('sound', 'Микшер', 'Алексей', 5005, 'alex_new',
                          1001, '2026-10-13T00:00:00+00:00')
                """
            )
            connection.commit()

        self.registry.initialize(1001, -10001)

        contacts = [
            item
            for item in self.registry.list_responsible_contacts(1001, "sound")
            if item["task"] == "Микшер"
        ]
        self.assertEqual(
            [(item["name"], item["telegram_id"], item["username"]) for item in contacts],
            [("Алексей", 5005, "alex_new")],
        )

    def test_migrates_contact_primary_key_from_name_to_telegram_id(self):
        self.registry.set_responsible_contact(
            1001, "sound", "Микшер", "Алексей", 4004, "alex"
        )
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("PRAGMA foreign_keys = OFF")
            connection.executescript(
                """
                ALTER TABLE responsible_contacts
                RENAME TO contacts_new;
                CREATE TABLE responsible_contacts (
                    service_id TEXT NOT NULL REFERENCES services(id)
                        ON DELETE CASCADE,
                    task TEXT NOT NULL,
                    name TEXT NOT NULL,
                    telegram_id INTEGER NOT NULL CHECK (telegram_id > 0),
                    username TEXT NOT NULL DEFAULT '',
                    updated_by INTEGER NOT NULL CHECK (updated_by > 0),
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (service_id, task, name)
                );
                INSERT INTO responsible_contacts
                SELECT * FROM contacts_new;
                INSERT INTO responsible_contacts (
                    service_id, task, name, telegram_id, username,
                    updated_by, updated_at
                ) VALUES (
                    'sound', 'Микшер', 'Алексей-дубликат', 4004,
                    'alex_duplicate', 1001, '2026-10-13T00:00:00+00:00'
                );
                DROP TABLE contacts_new;
                CREATE TABLE responsible_contacts_v2 (stale TEXT);
                """
            )

        self.registry.initialize(1001, -10001)

        with closing(sqlite3.connect(self.path)) as connection:
            migrated = connection.execute(
                "SELECT name, telegram_id, username "
                "FROM responsible_contacts WHERE task = 'Микшер'"
            ).fetchall()
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            leftover = connection.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type = 'table' AND name = 'responsible_contacts_v2'"
            ).fetchone()
        self.assertEqual(integrity, "ok")
        self.assertIsNone(leftover)
        self.assertEqual(len(migrated), 1)
        self.assertEqual(migrated[0][1], 4004)

        self.registry.set_responsible_contact(
            1001, "sound", "Микшер", "Алексей-дубликат", 5005, "alex2"
        )
        contacts = [
            item for item in self.registry.list_responsible_contacts(1001, "sound")
            if item["task"] == "Микшер"
        ]
        self.assertEqual(
            [(item["name"], item["telegram_id"]) for item in contacts],
            [("Алексей-дубликат", 5005)],
        )

    def test_service_admin_can_edit_only_own_notification_settings(self):
        self.registry.add_service_admin(1001, "sound", 3003)
        self.registry.add_service_admin(1001, "books", 4004)
        self.registry.set_notification_settings(
            3003,
            "sound",
            reminder_days=2,
            reminder_time="16:45",
            group_template="Звук: {service_name}",
            personal_templates={"Микшер": "{name}, проверьте звук"},
        )

        services = {service["id"]: service for service in self.registry.list_services()}
        self.assertEqual(services["sound"]["reminder_days"], 2)
        self.assertEqual(services["sound"]["reminder_time"], "16:45")
        self.assertEqual(
            services["sound"]["group_template"], "Звук: {service_name}"
        )
        self.assertEqual(
            services["sound"]["personal_templates"],
            {"Микшер": "{name}, проверьте звук"},
        )
        with self.assertRaises(PermissionError):
            self.registry.set_notification_settings(
                3003,
                "books",
                reminder_days=1,
                reminder_time="17:00",
                group_template="Слайды",
                personal_templates={},
            )
        # Чат своего служения — можно, чужого — нет.
        self.registry.set_group_chat(3003, "sound", -10023, enabled=True)
        with self.assertRaises(PermissionError):
            self.registry.set_group_chat(3003, "books", -10024, enabled=True)
        # Чат другого служения или личный чат админ служения не привяжет;
        # супер-администратор — может (общий чат для нескольких служений).
        self.registry.set_group_chat(1001, "books", -10024, enabled=True)
        with self.assertRaises(PermissionError):
            self.registry.set_group_chat(3003, "sound", -10024, enabled=True)
        with self.assertRaises(ValueError):
            self.registry.set_group_chat(3003, "sound", 777, enabled=True)
        self.registry.set_group_chat(1001, "sound", -10024, enabled=True)
        # Свой общий чат админ служения может привязать заново.
        with self.assertRaises(PermissionError):
            self.registry.set_group_chat(3003, "sound", -10024, enabled=False)
        with self.assertRaises(PermissionError):
            self.registry.set_service_enabled(3003, "sound", False)
        self.registry.set_service_enabled(3003, "sound", True)
        self.registry.set_group_chat(3003, "sound", -10024, enabled=True)

    def test_invalid_ids_and_unknown_services_are_rejected(self):
        with self.assertRaises(ValueError):
            self.registry.add_service_admin(1001, "sound", 0)
        with self.assertRaises(ValueError):
            self.registry.set_group_chat(1001, "unknown", -10003, enabled=True)
        with self.assertRaises(ValueError):
            self.registry.set_group_chat(1001, "sound", 0, enabled=True)
        with self.assertRaises(ValueError):
            self.registry.set_responsible_contact(
                1001, "sound", "Микшер", "Алексей", True
            )
        with self.assertRaises(ValueError):
            self.registry.set_notification_settings(
                1001,
                "sound",
                reminder_days=0,
                reminder_time="25:00",
                group_template="Звук",
                personal_templates={},
            )

    def test_symlink_database_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target.sqlite3"
            target.write_bytes(b"must remain unchanged")
            link = Path(self.temp_dir.name) / "private" / "symlink.sqlite3"
            link.symlink_to(target)
            registry = ServiceRegistry(link)

            with self.assertRaises(PermissionError):
                registry.initialize(1001, -10001)

            self.assertEqual(target.read_bytes(), b"must remain unchanged")

    def test_list_user_assignments_returns_linked_tasks(self):
        self.registry.set_responsible_contact(1001, "sound", "Микшер", "Анна", 2002)
        self.registry.set_responsible_contact(1001, "books", "Слайды", "Анна", 2002)
        self.registry.set_responsible_contact(1001, "sound", "Пульт", "Борис", 2003)

        self.assertEqual(
            self.registry.list_user_assignments(2002),
            [
                {"service_id": "books", "task": "Слайды", "name": "Анна"},
                {"service_id": "sound", "task": "Микшер", "name": "Анна"},
            ],
        )

    def test_service_schedule_creates_one_rule_per_task(self):
        for task in ("Микшер", "Пульт"):
            self.registry.set_responsible_contact(1001, "sound", task, "Анна", 2002)

        self.registry.set_user_service_schedule(
            2002, "sound", ["Микшер", "Пульт"], 1, "18:00"
        )

        for task in ("Микшер", "Пульт"):
            entry = self.registry.get_user_text(2002, "sound", task)
            self.assertEqual(
                [
                    (item["reminder_days"], item["reminder_time"], item["enabled"])
                    for item in self.registry.list_schedules(entry["id"])
                ],
                [(1, "18:00", 1)],
            )

    def test_service_schedule_updates_first_rule_and_keeps_deliveries(self):
        self.registry.set_responsible_contact(1001, "sound", "Микшер", "Анна", 2002)
        text_id = self.registry.upsert_user_text(2002, "sound", "Микшер", None)
        first = self.registry.add_schedule(text_id, 3, "09:00")
        self.registry.add_schedule(text_id, 2, "10:00")
        self.registry.update_schedule(first["id"], enabled=False)
        self.registry.mark_user_reminder_delivered(first["id"], "2026-10-25")

        self.registry.set_user_service_schedule(2002, "sound", ["Микшер"], 0, "07:30")

        schedules = self.registry.list_schedules(text_id)
        self.assertEqual(
            schedules,
            [
                {
                    "id": first["id"],
                    "text_id": text_id,
                    "reminder_days": 0,
                    "reminder_time": "07:30",
                    "enabled": 1,
                    "origin": "admin",
                    "body": None,
                }
            ],
        )
        self.assertTrue(
            self.registry.is_user_reminder_delivered(first["id"], "2026-10-25")
        )

    def test_service_schedule_rejects_unlinked_tasks_atomically(self):
        self.registry.set_responsible_contact(1001, "sound", "Микшер", "Анна", 2002)

        with self.assertRaises(ValueError):
            self.registry.set_user_service_schedule(
                2002, "sound", ["Микшер", "Чужая"], 1, "18:00"
            )
        with self.assertRaises(ValueError):
            self.registry.set_user_service_schedule(2002, "sound", [], 1, "18:00")
        with self.assertRaises(ValueError):
            self.registry.set_user_service_schedule(
                2002, "sound", ["Микшер"], 7, "18:00"
            )
        self.assertIsNone(self.registry.get_user_text(2002, "sound", "Микшер"))

    def test_service_schedule_keeps_deliveries_of_removed_rules(self):
        self.registry.set_responsible_contact(1001, "sound", "Микшер", "Анна", 2002)
        text_id = self.registry.upsert_user_text(2002, "sound", "Микшер", None)
        kept = self.registry.add_schedule(text_id, 3, "09:00")
        removed = self.registry.add_schedule(text_id, 1, "18:00")
        self.registry.mark_user_reminder_delivered(removed["id"], "2026-10-25")

        self.registry.set_user_service_schedule(2002, "sound", ["Микшер"], 1, "20:00")

        self.assertEqual(
            [item["id"] for item in self.registry.list_schedules(text_id)],
            [kept["id"]],
        )
        self.assertTrue(
            self.registry.is_user_reminder_delivered(kept["id"], "2026-10-25")
        )

    def test_templates_reject_control_markers(self):
        with self.assertRaises(ValueError):
            self.registry.set_service_default_template(
                1001, "sound", "{task_assignments}\x00CODE:A\x00"
            )


if __name__ == "__main__":
    unittest.main()


class RescheduleResetsDeliveryTest(unittest.TestCase):
    def test_changed_slot_clears_future_delivery_marks(self):
        import tempfile
        from datetime import date as _date, timedelta as _timedelta
        from pathlib import Path as _Path

        from bot.service_registry import ServiceRegistry as _Registry

        with tempfile.TemporaryDirectory() as tmp:
            registry = _Registry(_Path(tmp) / "r.sqlite3")
            registry.initialize(1001, -100)
            reminder = registry.add_group_reminder(1001, "sound", 3, "10:00")
            reminder_id = reminder["id"] if isinstance(reminder, dict) else reminder
            future = (_date.today() + _timedelta(days=3)).isoformat()
            past = (_date.today() - _timedelta(days=7)).isoformat()
            for event in (future, past):
                registry.mark_group_reminder_delivered(reminder_id, event, "group:-1")
            registry.update_group_reminder(1001, reminder_id, body="Новый текст")
            self.assertTrue(
                registry.is_group_reminder_delivered(reminder_id, future, "group:-1")
            )
            registry.update_group_reminder(1001, reminder_id, days=0, time="23:59")
            self.assertFalse(
                registry.is_group_reminder_delivered(reminder_id, future, "group:-1")
            )
            self.assertTrue(
                registry.is_group_reminder_delivered(reminder_id, past, "group:-1")
            )


class UserRuleRulesTest(unittest.TestCase):
    def test_duplicates_limit_and_length(self):
        import tempfile
        from pathlib import Path as _Path

        from bot.service_registry import ServiceRegistry as _Registry

        with tempfile.TemporaryDirectory() as tmp:
            registry = _Registry(_Path(tmp) / "r.sqlite3")
            registry.initialize(1001, -100)
            registry.set_responsible_contact(1001, "sound", "звук", "Алексей", 2002)
            text_id = registry.ensure_user_text(2002, "sound", "звук")
            admin = registry.add_schedule(text_id, 1, "18:00")
            with self.assertRaises(ValueError):
                registry.add_schedule(text_id, 1, "18:00", origin="user")
            copy = registry.add_schedule(
                text_id, 1, "18:00", origin="user", enabled=False
            )
            with self.assertRaises(ValueError):
                registry.update_schedule(copy["id"], enabled=True)
            registry.update_schedule(copy["id"], days=0, time="08:00")
            registry.update_schedule(copy["id"], enabled=True)
            with self.assertRaises(ValueError):
                registry.update_schedule(copy["id"], days=1, time="18:00")
            registry.update_schedule(admin["id"], enabled=False)
            registry.update_schedule(copy["id"], days=1, time="18:00")
            with self.assertRaises(ValueError):
                registry.update_schedule(copy["id"], body="x" * 3001)
            # Лимит — только на свои напоминания.
            for index in range(9):
                registry.add_schedule(
                    text_id, 2, f"0{index}:30", origin="user"
                )
            with self.assertRaises(ValueError):
                registry.add_schedule(text_id, 3, "09:00", origin="user")
            registry.add_schedule(text_id, 3, "09:00")


class PersonalOriginMigrationTest(unittest.TestCase):
    def test_legacy_rules_and_body_only_texts(self):
        import sqlite3 as _sqlite3
        import tempfile
        from pathlib import Path as _Path

        from bot.service_registry import ServiceRegistry as _Registry

        with tempfile.TemporaryDirectory() as tmp:
            path = _Path(tmp) / "r.sqlite3"
            registry = _Registry(path)
            registry.initialize(1001, -100)
            registry.set_responsible_contact(1001, "sound", "звук", "А", 2002)
            registry.set_responsible_contact(1001, "sound", "сборка", "А", 2002)
            sound = registry.ensure_user_text(2002, "sound", "звук")
            build = registry.ensure_user_text(2002, "sound", "сборка")
            registry.add_schedule(sound, 1, "18:00")
            registry.add_schedule(sound, 0, "08:00")
            with _sqlite3.connect(path) as connection:
                connection.execute(
                    "UPDATE user_reminder_texts SET body = 'Мой текст' WHERE id = ?",
                    (build,),
                )
                connection.execute(
                    "DELETE FROM catalog_migrations WHERE name = 'personal-origin-v2'"
                )
            _Registry(path).initialize(1001, -100)
            rules = registry.list_user_service_schedules(2002, "sound")
            self.assertEqual(
                sorted((r["task"], r["origin"], r["enabled"], r["body"]) for r in rules),
                [
                    ("звук", "user", 1, None),
                    ("звук", "user", 1, None),
                    ("сборка", "user", 0, "Мой текст"),
                ],
            )


class InteractiveRosterSyncTest(unittest.TestCase):
    def test_own_roster_only_updates_just_that_service(self):
        from unittest.mock import MagicMock, patch

        from bot import app
        from bot.excel_parser import RosterMember

        registry = MagicMock()
        rosters = {
            "sound": [RosterMember("Алексей", ("звук",))],
            "kitchen": [RosterMember("Вера", ("в смене",))],
        }
        with patch.object(app, "parse_service_rosters", return_value=rosters):
            app._sync_rosters_and_contacts(
                registry, Path("x.xlsx"), "sound", {}, None, MagicMock(),
                None, own_roster_only=True,
            )
            registry.sync_service_rosters.assert_called_once_with(
                {"sound": rosters["sound"]}
            )
            registry.reset_mock()
            app._sync_rosters_and_contacts(
                registry, Path("x.xlsx"), "sound", {}, None, MagicMock(), None,
            )
            registry.sync_service_rosters.assert_called_once_with(rosters)


class UsernameBindingTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.registry = ServiceRegistry(
            Path(self.temp_dir.name) / "services.sqlite3"
        )
        self.registry.initialize(1001, -10001)

    def test_pending_binding_for_task_is_taken_once(self):
        registry = self.registry
        with self.assertRaises(PermissionError):
            registry.add_username_binding(2002, "sound", "Борис", "звук", "boris_b")
        registry.add_username_binding(1001, "sound", "Борис", "звук", "@Boris_B")
        self.assertEqual(registry.pending_usernames(), {"boris_b"})
        self.assertIsNone(registry.known_telegram_id("boris_b"))
        taken = registry.take_username_bindings("BORIS_B")
        self.assertEqual(
            [(item["name"], item["task"], item["actor_id"]) for item in taken],
            [("Борис", "звук", 1001)],
        )
        self.assertEqual(registry.take_username_bindings("boris_b"), [])
        # Правка админа — не наблюдение: ID по ней не подставляется.
        registry.set_responsible_contact(1001, "sound", "звук", "Борис", 555, "Boris_B")
        self.assertIsNone(registry.known_telegram_id("@boris_b"))
        registry.note_seen_username(555, "Boris_B")
        self.assertEqual(registry.known_telegram_id("@boris_b"), 555)

    def test_binding_by_id_or_removal_drops_pending(self):
        registry = self.registry
        registry.add_username_binding(1001, "sound", "Борис", "", "boris_b")
        registry.bind_service_member(1001, "sound", "Борис", 555)
        self.assertIsNone(registry.username_binding("sound", "Борис"))
        registry.add_username_binding(1001, "sound", "Борис", "", "boris_b")
        registry.remove_service_member(1001, "sound", "Борис")
        self.assertEqual(registry.pending_usernames(), set())

    def test_expired_binding_is_ignored_and_newest_id_wins(self):
        registry = self.registry
        registry.add_username_binding(1001, "sound", "Борис", "", "boris_b")
        with closing(sqlite3.connect(registry.path)) as connection, connection:
            connection.execute(
                "UPDATE username_bindings SET created_at = '2000-01-01T00:00:00+00:00'"
            )
        self.assertEqual(registry.pending_usernames(), set())
        self.assertIsNone(registry.username_binding("sound", "Борис"))
        self.assertEqual(registry.take_username_bindings("boris_b"), [])
        with self.assertRaises(ValueError):
            registry.add_username_binding(1001, "sound", "Борис", "", "bad name")
        # Username перешёл к другому — по последнему наблюдению.
        registry.note_seen_username(111, "foo_user")
        registry.note_seen_username(222, "Foo_User")
        self.assertEqual(registry.known_telegram_id("foo_user"), 222)
        # Сменил username — прежний за ним больше не числится.
        registry.note_seen_username(222, "bar_user")
        self.assertIsNone(registry.known_telegram_id("foo_user"))
        self.assertEqual(registry.known_telegram_id("bar_user"), 222)

    def test_removed_username_retires_old_poll_answers(self):
        registry = self.registry
        with closing(sqlite3.connect(registry.path)) as connection, connection:
            connection.execute(
                "INSERT INTO readiness_runs (service_id, month, status, "
                "created_at) VALUES ('sound', '2026-11', 'closed', "
                "'2026-10-01T00:00:00+00:00')"
            )
            connection.execute(
                "INSERT INTO readiness_answers (run_id, telegram_id, username, "
                "dates_json, answered_at) VALUES (1, 111, 'Old_Name', '[]', "
                "'2026-10-01T00:00:00+00:00')"
            )
        self.assertEqual(registry.known_telegram_id("old_name"), 111)
        # Убрал username — ответ со старым больше не в счёт: его мог
        # занять другой человек.
        registry.note_seen_username(111, "")
        self.assertIsNone(registry.known_telegram_id("old_name"))
        self.assertIsNone(registry.known_telegram_id("~111"))
        registry.note_seen_username(222, "old_name")
        self.assertEqual(registry.known_telegram_id("old_name"), 222)

    def test_removal_excludes_member_missing_from_lists(self):
        from bot.excel_parser import RosterMember

        registry = self.registry
        registry.sync_service_rosters({"sound": [RosterMember("Борис", ("звук",))]})
        # Следующий файл без Бориса — в составе он остаётся, но не в «Списках».
        registry.sync_service_rosters({"sound": []})
        boris = next(
            m for m in registry.list_service_members("sound")
            if m["name"] == "Борис"
        )
        self.assertFalse(boris["in_list"])
        self.assertFalse(registry.remove_service_member(1001, "sound", "Борис"))
        registry.sync_service_rosters({"sound": [RosterMember("Борис", ("звук",))]})
        self.assertNotIn(
            "Борис", [m["name"] for m in registry.list_service_members("sound")]
        )

    def test_new_binding_replaces_pending_and_task_removal_drops_it(self):
        registry = self.registry
        registry.add_username_binding(1001, "sound", "Борис", "", "old_name")
        registry.add_username_binding(1001, "sound", "Борис", "звук", "new_name")
        self.assertEqual(registry.pending_usernames(), {"new_name"})
        registry.set_service_member_tasks(1001, "sound", "Борис", ["сборка"])
        self.assertEqual(registry.pending_usernames(), set())


class PlanTitleMigrationTest(unittest.TestCase):
    def test_status_field_split_into_two_titles(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        path = Path(temp_dir.name) / "services.sqlite3"
        registry = ServiceRegistry(path)
        registry.initialize(1001, -10001)
        registry.set_readiness_settings(1001, "sound", enabled=False)
        with closing(sqlite3.connect(path)) as connection, connection:
            connection.execute(
                "UPDATE readiness_settings SET plan_title_template = "
                "'График {service_name}: {status}' WHERE service_id = 'sound'"
            )
            connection.execute(
                "DELETE FROM catalog_migrations WHERE name = 'plan-title-v2'"
            )
        registry = ServiceRegistry(path)
        registry.initialize(1001, -10001)
        settings = registry.get_readiness_settings("sound")
        self.assertEqual(settings["plan_title_template"], "График {service_name}: утверждён")
        self.assertEqual(settings["plan_update_template"], "График {service_name}: обновлён")

    def test_title_without_status_kept_for_updates(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        path = Path(temp_dir.name) / "services.sqlite3"
        registry = ServiceRegistry(path)
        registry.initialize(1001, -10001)
        registry.set_readiness_settings(1001, "sound", enabled=False)
        with closing(sqlite3.connect(path)) as connection, connection:
            connection.execute(
                "UPDATE readiness_settings SET plan_title_template = "
                "'Расписание {service_name} на {month}' WHERE service_id = 'sound'"
            )
            connection.execute(
                "DELETE FROM catalog_migrations WHERE name = 'plan-title-v2'"
            )
        registry = ServiceRegistry(path)
        registry.initialize(1001, -10001)
        settings = registry.get_readiness_settings("sound")
        self.assertEqual(settings["plan_title_template"], "Расписание {service_name} на {month}")
        self.assertEqual(settings["plan_update_template"], "Расписание {service_name} на {month}")
