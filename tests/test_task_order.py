import copy
import tempfile
import unittest
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

from bot import admin_report, presentation, readiness, schedule_plan
from bot.excel_parser import RosterMember, ServiceAssignment, ServiceEvent
from bot.service_registry import ServiceRegistry
from bot.services import sort_tasks_for_display


DAY = "2026-11-15"
KITCHEN = ["старший", "в смене", "Скатерти", "пироги"]


class TaskOrderTest(unittest.TestCase):
    def assert_order(self, text, tasks):
        offsets = [text.index(task) for task in tasks]
        self.assertEqual(offsets, sorted(offsets), text)

    def test_fixed_order_normalization_stability_and_no_mutation(self):
        cases = [
            ("kitchen", ["новая Б", "пироги", " СКАТЕРТИ ", "в смене",
                         "старший", "новая А"],
             ["старший", "в смене", " СКАТЕРТИ ", "пироги", "новая Б", "новая А"]),
            ("order", ["новая", "в смене", "старший"],
             ["старший", "в смене", "новая"]),
            ("sound", ["спевка", "сборка", "звук", "новая"],
             ["звук", "сборка", "спевка", "новая"]),
            ("broadcast", ["Пульт", "Компьютер"], ["Пульт", "Компьютер"]),
        ]
        for service_id, tasks, expected in cases:
            with self.subTest(service=service_id):
                original = list(tasks)
                self.assertEqual(sort_tasks_for_display(tasks, service_id), expected)
                self.assertEqual(tasks, original)
                self.assertEqual(
                    sort_tasks_for_display(expected, service_id), expected,
                )
        self.assertEqual(sort_tasks_for_display(["пироги"], "kitchen"), ["пироги"])
        self.assertEqual(sort_tasks_for_display([], "kitchen"), [])

    def test_shared_assignment_report_and_personal_rendering(self):
        for service_id, tasks in (
            ("kitchen", KITCHEN),
            ("order", ["старший", "в смене"]),
            ("sound", ["звук", "сборка", "спевка"]),
        ):
            with self.subTest(service=service_id):
                assignments = {task: ["Участник"] for task in reversed(tasks)}
                original = copy.deepcopy(assignments)
                self.assertEqual(
                    [task for task, _ in presentation.ordered_task_assignments(
                        assignments, service_id,
                    )], tasks,
                )
                self.assert_order(presentation.format_task_assignments(
                    assignments, service_id=service_id,
                ), tasks)
                report = admin_report.build_report_text(
                    "{task_assignments}\n{delivery_status}\n{confirmation_status}",
                    service_id, date.fromisoformat(DAY), assignments, {},
                    lambda _: True, {}, service_id=service_id,
                )
                for section in (
                    report.split("⚪ Участник")[0],
                    "\n".join(line for line in report.splitlines()
                              if "аккаунт" in line),
                    "\n".join(line for line in report.splitlines()
                              if "нет ответа" in line),
                ):
                    self.assert_order(section, tasks)
                event = ServiceEvent(
                    service_id, date.fromisoformat(DAY),
                    tuple(ServiceAssignment(task, "Участник") for task in assignments),
                )
                self.assert_order("\n".join(presentation.personal_schedule_lines(
                    [event], {(service_id, task, "Участник") for task in tasks},
                    {service_id: "Служение"},
                )), tasks)
                self.assertEqual(assignments, original)


class RegistryTaskOrderTest(unittest.TestCase):
    assert_order = TaskOrderTest.assert_order

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.registry = ServiceRegistry(Path(directory.name) / "registry.sqlite3")
        self.registry.initialize(1001, -10001)
        self.registry.add_service_task(1001, "kitchen", "Скатерти")
        self.registry.add_service_task(1001, "kitchen", "Новая Б")
        self.registry.add_service_task(1001, "kitchen", "Новая А")
        self.registry.sync_service_rosters({
            "kitchen": [RosterMember(
                "Участник", ("пироги", "Скатерти", "в смене", "старший"),
            )],
        })

    def snapshot(self):
        with self.registry._connection() as connection:
            return "\n".join(connection.iterdump())

    def plan(self):
        tasks = [
            {"name": task, "needed": 1, "max": 1}
            for task in reversed(KITCHEN)
        ]
        return self.registry.save_schedule_draft(
            "kitchen", "2026-11", run_id=None, dates=[DAY], tasks=tasks,
            slots={DAY: {task: [f"Человек {i}"] for i, task in enumerate(KITCHEN)}},
            last_served={},
        )

    def test_registry_positions_disabled_and_planner_input_are_unchanged(self):
        before = self.snapshot()
        tasks = self.registry.list_service_tasks("kitchen")
        self.assertEqual(
            [task["name"] for task in tasks if task["enabled"]],
            KITCHEN + ["Новая Б", "Новая А"],
        )
        self.assertEqual(
            {task["name"]: task["position"] for task in tasks},
            {"старший": 0, "в смене": 1, "пироги": 2, "кофе": 3,
             "Скатерти": 4, "Новая Б": 5, "Новая А": 6},
        )
        self.assertFalse(next(task for task in tasks if task["name"] == "кофе")["enabled"])
        self.assertEqual(
            [task["name"] for task in self.registry.plan_tasks("kitchen")],
            ["старший", "в смене", "пироги", "Скатерти", "Новая Б", "Новая А"],
        )
        self.assertEqual(
            self.registry.list_service_members("kitchen")[0]["tasks"], KITCHEN,
        )
        self.assertEqual(self.snapshot(), before)
        self.registry.set_service_task_enabled(1001, "kitchen", "Скатерти", False)
        self.assertNotIn(
            "Скатерти",
            [task["name"] for task in self.registry.plan_tasks("kitchen")],
        )

    def test_saved_draft_approved_publication_excel_only_change_display(self):
        plan = self.plan()
        people = [
            {"key": key, "name": key, "telegram_id": i + 2000, "tasks": [task]}
            for i, (task, keys) in enumerate(plan["slots"][DAY].items())
            for key in keys
        ]
        summary = readiness.Summary(people)
        for approved in (False, True):
            with self.subTest(approved=approved):
                if approved:
                    plan = self.registry.approve_schedule_plan(
                        1001, "kitchen", "2026-11", plan["slots"],
                        chat_id=None, message_id=None,
                    )
                original = copy.deepcopy(plan)
                before = self.snapshot()
                for text in (
                    schedule_plan.plan_text(
                        "Кухня", plan, summary, ZoneInfo("Europe/Moscow"),
                    ),
                    schedule_plan.publication_text(
                        self.registry.get_service("kitchen"), plan, people,
                    ),
                    schedule_plan.excel_text(
                        self.registry, self.registry.get_service("kitchen"),
                        plan, people,
                    ),
                    schedule_plan.month_overview(
                        self.registry, 1001, "kitchen", date(2026, 11, 1),
                    ),
                ):
                    self.assert_order(text, KITCHEN)
                rows = schedule_plan.excel_rows(self.registry, plan, people)
                self.assertEqual([row[0] for row in rows[2:]], KITCHEN)
                # Sorting enumerated pairs preserves the raw callback index.
                indexed = sort_tasks_for_display(
                    enumerate(plan["tasks"]), "kitchen",
                    name=lambda pair: pair[1]["name"],
                )
                self.assertEqual([index for index, _ in indexed], [3, 2, 1, 0])
                self.assertEqual(plan, original)
                self.assertEqual(self.snapshot(), before)

    def test_excel_candidate_reversed_rows_preserve_assignments(self):
        event = ServiceEvent(
            "kitchen", date.fromisoformat(DAY),
            tuple(ServiceAssignment(task, f"Человек {i}")
                  for i, task in enumerate(reversed(KITCHEN))),
        )
        original = copy.deepcopy(event)
        before = self.snapshot()
        candidate = schedule_plan.excel_plan(
            self.registry, "kitchen", 2026, 11, [event],
        )
        self.assertEqual(candidate["slots"][DAY]["пироги"], ["Человек 0"])
        self.assertEqual(candidate["slots"][DAY]["старший"], ["Человек 3"])
        self.assertEqual(
            [task["name"] for task in schedule_plan.display_tasks({
                **candidate, "service_id": "kitchen",
            })], KITCHEN + ["Новая Б", "Новая А"],
        )
        self.assertEqual(event, original)
        self.assertEqual(self.snapshot(), before)

    def test_week_groups_and_gap_messages_keep_dates_and_slots(self):
        plan = self.plan()
        plan["tasks"][0]["day"] = -4
        original = copy.deepcopy(plan)
        _, groups = schedule_plan.week_groups(plan, DAY, (0, 0))
        self.assertEqual([offset for _, offset, _ in groups], [-4, 0])
        self.assertEqual(
            [[task["name"] for task in tasks] for _, _, tasks in groups],
            [["пироги"], KITCHEN[:-1]],
        )
        self.assertEqual(plan, original)
        plan["slots"] = {DAY: {}}
        self.assert_order("\n".join(schedule_plan.gaps(plan)), KITCHEN)

    def test_plan_breadcrumb_distinguishes_auto_and_poll_without_mutation(self):
        plan = self.plan()
        for run_id, label in ((None, "Авто"), (123, "Опрос")):
            with self.subTest(run_id=run_id):
                plan["run_id"] = run_id
                original = copy.deepcopy(plan)
                heading = schedule_plan.plan_text(
                    "Кухня", plan, readiness.Summary([]),
                    ZoneInfo("Europe/Moscow"),
                ).splitlines()[0]
                self.assertEqual(
                    heading, f"📋 Кухня › График › {label} › ноябрь 2026",
                )
                self.assertEqual(plan, original)

    def test_sound_and_order_plan_surfaces(self):
        for service_id, names in (
            ("sound", ["звук", "сборка"]),
            ("order", ["старший", "в смене"]),
        ):
            with self.subTest(service=service_id):
                plan = {
                    **self.plan(), "service_id": service_id,
                    "tasks": [{"name": task, "needed": 1, "max": 1}
                              for task in reversed(names)],
                    "slots": {DAY: {task: [f"Человек {i}"]
                                    for i, task in enumerate(names)}},
                }
                original = copy.deepcopy(plan)
                service = self.registry.get_service(service_id)
                self.assert_order(schedule_plan.plan_text(
                    "Служение", plan, readiness.Summary([]),
                    ZoneInfo("Europe/Moscow")),
                    names,
                )
                self.assert_order(schedule_plan.publication_text(
                    {**service, "name": "Служение"}, plan, []),
                    names,
                )
                self.assertEqual(
                    [row[0] for row in schedule_plan.excel_rows(
                        self.registry, plan, [],
                    )[2:]], names,
                )
                self.assertEqual(plan, original)
