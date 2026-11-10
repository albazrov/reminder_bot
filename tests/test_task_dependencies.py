import asyncio
import logging
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

from bot import handlers, readiness, replacement, schedule_plan
from bot.excel_parser import RosterMember
from bot.schedule_planner import PlanInput, build_plan
from bot.service_registry import ServiceRegistry, task_token
from bot.task_rules import assignment_errors
from bot.task_rules import AssignmentWarnings


SUNDAYS = tuple(date(2026, 11, 1) + timedelta(weeks=i) for i in range(5))
DUTY = "Скатерти"
HEAD = "старший"
TEAM = "в смене"


class DependentPlannerTest(unittest.TestCase):
    def data(self, tasks, dates=SUNDAYS):
        return PlanInput(
            dates=dates, needed={HEAD: 1, TEAM: 1, DUTY: 1},
            free={person: frozenset(dates) for person in tasks},
            tasks={person: frozenset(names) for person, names in tasks.items()},
            dependencies={DUTY: (HEAD, TEAM)},
        )

    def test_selects_team_to_cover_duty_and_counts_one_service_day(self):
        data = self.data({
            "A": [HEAD], "B": [TEAM], "C": [TEAM, DUTY],
        }, SUNDAYS[:1])
        plan = build_plan(data)
        self.assertEqual(plan.slots[(SUNDAYS[0], TEAM)], ["C"])
        self.assertEqual(plan.slots[(SUNDAYS[0], DUTY)], ["C"])
        self.assertEqual(plan.load(), {"A": 1, "C": 1})
        self.assertFalse(plan.gaps())

    def test_duty_rotates_separately_and_respects_previous_month(self):
        data = self.data({"A": [HEAD, DUTY], "B": [TEAM, DUTY]})
        plan = build_plan(data)
        counts = {person: sum(
            person in plan.slots[(day, DUTY)] for day in SUNDAYS
        ) for person in "AB"}
        self.assertLessEqual(abs(counts["A"] - counts["B"]), 1)
        previous = replace(data, dates=SUNDAYS[:1], last_duty={"A": date(2026, 10, 25)})
        self.assertEqual(build_plan(previous).slots[(SUNDAYS[0], DUTY)], ["B"])

    def test_no_permit_or_no_primary_role_leaves_explicit_gap(self):
        for skills in (
            {"A": [HEAD], "B": [TEAM]},
            {"A": [HEAD], "B": [TEAM], "C": [DUTY]},
        ):
            with self.subTest(skills=skills):
                plan = build_plan(self.data(skills, SUNDAYS[:1]))
                self.assertEqual(plan.slots[(SUNDAYS[0], DUTY)], [])
                self.assertIn((SUNDAYS[0], DUTY, 1), plan.gaps())

    def test_unavailable_duty_candidate_not_used_and_reserve_is_last(self):
        data = self.data({
            "A": [HEAD, DUTY], "B": [TEAM, DUTY], "C": [TEAM, DUTY],
        }, SUNDAYS[:2])
        data = replace(
            data, reluctant=frozenset({"C"}),
            unavailable={"A": frozenset({(SUNDAYS[0], DUTY)})},
        )
        plan = build_plan(data)
        self.assertEqual(plan.slots[(SUNDAYS[0], DUTY)], ["B"])
        self.assertNotIn("C", plan.load())

    def test_all_small_eligibility_combinations_obey_rules(self):
        for mask in range(1, 64):
            skills = {
                person: ([HEAD, TEAM] + ([DUTY] if mask & (1 << index) else []))
                for index, person in enumerate("ABCDEF")
            }
            data = self.data(skills, SUNDAYS[:1])
            plan = build_plan(data)
            tasks = [{"name": HEAD}, {"name": TEAM}, {
                "name": DUTY, "requires_any": [HEAD, TEAM],
            }]
            slots = {SUNDAYS[0].isoformat(): {
                task: people for (day, task), people in plan.slots.items()
            }}
            self.assertEqual(assignment_errors(
                tasks, slots, {key: set(value) for key, value in skills.items()},
                complete=True,
            ), [])

    def test_rejects_self_dependency(self):
        with self.assertRaises(ValueError):
            build_plan(replace(
                self.data({"A": [HEAD, DUTY]}),
                dependencies={DUTY: (DUTY,)},
            ))

    def test_unrelated_week_bundle_is_preserved(self):
        day = SUNDAYS[0]
        data = self.data({
            "A": [HEAD, DUTY], "B": [TEAM], "C": ["X", "Y"], "D": ["Y"],
        }, (day,))
        data = replace(
            data, needed={**data.needed, "X": 1, "Y": 1},
            bundles={"C": frozenset({"X", "Y"})},
            slot_dates={(day, "Y", key): day + timedelta(days=2) for key in "CD"},
        )
        plan = build_plan(data)
        self.assertEqual(plan.slots[(day, "X")], ["C"])
        self.assertEqual(plan.slots[(day, "Y")], ["C"])
        self.assertEqual(plan.slots[(day, DUTY)], ["A"])

    def test_repairs_shared_day_chain_without_losing_duty_or_other_assignments(self):
        first, second = SUNDAYS[:2]
        shared = first + timedelta(days=1)
        skills = {
            "A": {"X", "Y"}, "B": {"X", "Z"}, "C": {"Z"}, "D": {"W"},
            "E": {HEAD, DUTY}, "F": {TEAM},
        }
        data = replace(
            self.data(skills, (first, second)),
            needed=dict.fromkeys((HEAD, TEAM, DUTY, "X", "Y", "Z", "W"), 1),
            availability={
                person: frozenset((shared, task) for task in tasks)
                for person, tasks in skills.items() if person in "ABCD"
            },
            limits={person: (0, 1) for person in skills},
            slot_dates={
                (second if task == "Y" else first, task, person): shared
                for person, tasks in skills.items() if person in "ABCD"
                for task in tasks
            },
        )
        plan = build_plan(data)
        self.assertEqual(plan.covered_required(), 10)
        for task, person in (("X", "B"), ("Y", "A"), ("Z", "C"), ("W", "D")):
            self.assertEqual(
                plan.slots[(second if task == "Y" else first, task)], [person],
            )
        for day in (first, second):
            self.assertEqual(plan.slots[(day, HEAD)], ["E"])
            self.assertEqual(plan.slots[(day, DUTY)], ["E"])
            self.assertEqual(plan.slots[(day, TEAM)], ["F"])

    def test_repair_does_not_take_duty_holders_primary_seat_or_double_book_them(self):
        first, second = SUNDAYS[:2]
        data = replace(
            self.data({"A": [HEAD, DUTY, "X"], "B": [TEAM], "C": [HEAD]}, (first, second)),
            needed={HEAD: 1, TEAM: 1, DUTY: 1, "X": 1},
            availability={"A": frozenset({
                (first, HEAD), (first, DUTY), (first, "X"),
                (second, HEAD), (second, DUTY),
            })},
            slot_dates={
                (first, "X", "A"): first + timedelta(days=1),
                (second, "X", "A"): first,
            },
        )
        plan = build_plan(data)
        for day in (first, second):
            self.assertEqual(plan.slots[(day, HEAD)], ["A"])
            self.assertEqual(plan.slots[(day, DUTY)], ["A"])
            self.assertEqual(plan.slots[(day, "X")], [])


class DependentRegistryTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.registry = ServiceRegistry(Path(temp.name) / "registry.sqlite3")
        self.registry.initialize(1001, -10001)
        self.registry.set_service_task_enabled(1001, "kitchen", "пироги", False)
        self.registry.add_service_task(1001, "kitchen", DUTY)
        self.registry.set_task_dependency(1001, "kitchen", DUTY, [HEAD, TEAM])
        self.registry.sync_service_rosters({"kitchen": [
            RosterMember("A", (HEAD, DUTY)),
            RosterMember("B", (TEAM, DUTY)),
            RosterMember("C", (TEAM,)),
        ]})
        for index, (person, tasks) in enumerate((
            ("A", [HEAD, DUTY]), ("B", [TEAM, DUTY]), ("C", [TEAM]),
        ), 501):
            for task in tasks:
                self.registry.set_responsible_contact(1001, "kitchen", task, person, index)
        self.slots = {day.isoformat(): {HEAD: ["A"], TEAM: ["B"], DUTY: ["A"]}
                      for day in SUNDAYS}
        self.registry.save_schedule_draft(
            "kitchen", "2026-11", run_id=None, dates=list(self.slots),
            tasks=schedule_plan.plan_tasks(self.registry, "kitchen"),
            slots=self.slots, last_served={},
        )

    def approve(self):
        return self.registry.approve_schedule_plan(
            1001, "kitchen", "2026-11", self.slots, chat_id=None, message_id=None,
        )

    def test_editor_rejects_missing_permit_duplicate_and_primary_conflicts(self):
        for changes, error in (
            ({DUTY: ["C"]}, "допуска"),
            ({DUTY: ["B", "A"]}, "ровно один"),
            ({HEAD: ["A", "A"]}, "дважды"),
            ({TEAM: ["A"]}, "разными"),
            ({TEAM: ["C"], DUTY: ["C"]}, "допуска"),
        ):
            slots = {**self.slots, SUNDAYS[0].isoformat(): {
                **self.slots[SUNDAYS[0].isoformat()], **changes,
            }}
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, error):
                self.registry.set_schedule_slots(1001, "kitchen", "2026-11", slots)
        self.assertEqual(self.registry.get_schedule_plan("kitchen", "2026-11")["slots"], self.slots)

    def test_manual_outside_team_can_be_selected_edited_and_approved(self):
        day = SUNDAYS[0].isoformat()
        slots = {**self.slots, day: {HEAD: ["A"], TEAM: ["C"], DUTY: []}}
        plan = self.registry.set_schedule_slots(1001, "kitchen", "2026-11", slots)
        summary = schedule_plan.plan_summary(self.registry, plan, None)
        self.assertIn("B", sum(schedule_plan.candidates(plan, summary, day, DUTY), []))
        self.assertNotIn("C", sum(schedule_plan.candidates(plan, summary, day, DUTY), []))
        slots[day][DUTY] = ["B"]
        edited = self.registry.set_schedule_slots(1001, "kitchen", "2026-11", slots)
        self.assertEqual(edited["slots"][day][DUTY], ["B"])
        self.assertIsNone(edited["approved"])
        self.assertTrue(any("не входит в смену" in note for note in
                            schedule_plan.choice_notes(edited, summary, "B", day, DUTY)))
        self.registry.set_schedule_day(1001, "kitchen", "2026-11", day, "B", 0)
        with self.assertRaisesRegex(ValueError, "воскресенье"):
            self.registry.set_schedule_day(1001, "kitchen", "2026-11", day, "B", -1)
        warnings = self.registry.task_assignment_warnings(
            "kitchen", slots, complete=True, month="2026-11",
        )
        self.assertEqual(warnings, [f"{day} · {DUTY}: ответственный не входит в смену."])
        with self.assertRaises(AssignmentWarnings):
            self.registry.approve_schedule_plan(
                1001, "kitchen", "2026-11", slots, chat_id=None, message_id=None,
            )
        saved = self.registry.approve_schedule_plan(
            1001, "kitchen", "2026-11", slots, chat_id=None, message_id=None,
            acknowledged_warnings=warnings,
        )
        self.assertEqual(saved["approved"][day][DUTY], ["B"])
        self.assertEqual(saved["approval_warnings"], warnings)

    def test_missing_duty_or_sunday_or_team_blocks_import_and_approval(self):
        for slots in (
            {day: {**values, DUTY: []} for day, values in self.slots.items()},
            {day: values for day, values in self.slots.items() if day != "2026-11-08"},
            {day: {**values, TEAM: []} for day, values in self.slots.items()},
        ):
            with self.subTest(slots=slots), self.assertRaises(ValueError):
                self.registry.approve_schedule_plan(
                    1001, "kitchen", "2026-11", slots, chat_id=None, message_id=None,
                )
            with self.assertRaises(ValueError):
                self.registry.import_excel_plan(
                    1001, "kitchen", "2026-11", dates=list(slots),
                    tasks=schedule_plan.plan_tasks(self.registry, "kitchen"),
                    slots=slots, days={},
                )

    def test_publish_validates_before_any_telegram_call(self):
        plan = self.registry.get_schedule_plan("kitchen", "2026-11")
        plan["slots"]["2026-11-01"][DUTY] = []
        bot = SimpleNamespace(send_message=AsyncMock(), edit_message_text=AsyncMock())
        with self.assertRaises(ValueError):
            asyncio.run(schedule_plan.publish(
                bot, self.registry,
                {**self.registry.get_service("kitchen"), "enabled": True, "group_chat_id": -123},
                plan, 1001, logging.getLogger("test-dependencies"), 4096,
            ))
        bot.send_message.assert_not_awaited()

    def test_sunday_is_enforced_and_setting_guards_preserve_rule(self):
        with self.assertRaisesRegex(ValueError, "воскресенье"):
            self.registry.set_schedule_day(
                1001, "kitchen", "2026-11", "2026-11-01", "A", -1,
            )
        for change in (
            lambda: self.registry.set_service_task_day(1001, "kitchen", HEAD, 2),
            lambda: self.registry.set_service_task_enabled(1001, "kitchen", TEAM, False),
            lambda: self.registry.set_service_task_people_needed(1001, "kitchen", DUTY, 2),
            lambda: self.registry.set_service_task_same_week(1001, "kitchen", DUTY, True),
        ):
            with self.assertRaises(ValueError):
                change()
        with self.assertRaises(PermissionError):
            self.registry.set_task_dependency(501, "kitchen", DUTY, [])
        with self.assertRaises(ValueError):
            self.registry.set_task_dependency(1001, "kitchen", HEAD, [DUTY])

    def test_replacements_require_duty_to_stay_inside_team(self):
        self.approve()
        replace_person = self.registry.set_replacement
        with self.assertRaisesRegex(ValueError, "в смену"):
            replace_person("kitchen", "2026-11-01", HEAD, "A", "C", 503, "", "admin", 1001)
        with self.assertRaisesRegex(ValueError, "допуска"):
            replace_person("kitchen", "2026-11-01", DUTY, "A", "C", 503, "", "admin", 1001)
        self.assertTrue(replace_person(
            "kitchen", "2026-11-01", DUTY, "A", "B", 502, "", "admin", 1001,
        ))
        self.assertTrue(replace_person(
            "kitchen", "2026-11-01", HEAD, "A", "C", 503, "", "admin", 1001,
        ))

    def test_primary_decline_cascades_but_duty_decline_does_not(self):
        self.approve()
        duty = self.registry.set_assignment_answer(
            "kitchen", "2026-11-01", DUTY, 501, "declined",
        )
        self.assertEqual([item[0] for item in duty], [DUTY])
        self.assertIsNone(self.registry.get_confirmation("kitchen", "2026-11-01", HEAD, 501))
        primary = self.registry.set_assignment_answer(
            "kitchen", "2026-11-08", HEAD, 501, "declined",
        )
        self.assertEqual([item[0] for item in primary], [HEAD, DUTY])
        self.assertEqual(self.registry.get_confirmation("kitchen", "2026-11-08", DUTY, 501), "declined")

    def test_replacements_preserve_accepted_outsider_warning_but_cannot_change_outsider(self):
        self.registry.set_service_task_enabled(1001, "kitchen", "пироги", True)
        self.registry.set_responsible_contact(1001, "kitchen", "пироги", "P", 505)
        self.registry.set_responsible_contact(1001, "kitchen", "пироги", "Q", 506)
        slots = {day: {HEAD: ["A"], TEAM: ["C"], DUTY: ["B"], "пироги": ["P"]}
                 for day in self.slots}
        warnings = self.registry.task_assignment_warnings("kitchen", slots, complete=True)
        self.registry.approve_schedule_plan(
            1001, "kitchen", "2026-11", slots, chat_id=None, message_id=None,
            acknowledged_warnings=warnings,
        )
        for task in (TEAM, DUTY):
            self.registry.set_responsible_contact(1001, "kitchen", task, "D", 504)
        for source, day in (("admin", "2026-11-01"), ("poll", "2026-11-08")):
            with self.subTest(source=source):
                self.assertTrue(self.registry.set_replacement(
                    "kitchen", day, "пироги", "P", "Q", 506, "", source, 1001,
                ))
                poll = {
                    "poll_id": "test", "service_id": "kitchen", "event_date": day,
                    "task": TEAM, "old_name": "C", "old_telegram_id": 503,
                }
                answer = {"telegram_id": 504, "can": True, "full_name": "D", "username": ""}
                with patch.object(self.registry, "list_poll_answers", return_value=[answer]):
                    self.assertEqual(replacement.choose_volunteer(self.registry, poll), answer)
                with self.assertRaisesRegex(ValueError, "в смену"):
                    self.registry.set_replacement(
                        "kitchen", day, DUTY, "B", "D", 504, "", source, 1001,
                    )
                self.assertTrue(self.registry.set_replacement(
                    "kitchen", day, TEAM, "C", "D", 504, "", source, 1001,
                ))
                self.assertEqual(
                    self.registry.effective_assignments("kitchen", day)[DUTY], ["B"],
                )
                self.assertTrue(self.registry.set_replacement(
                    "kitchen", day, DUTY, "B", "D", 504, "", source, 1001,
                ))
        self.assertEqual(
            self.registry.get_schedule_plan("kitchen", "2026-11")["approval_warnings"], warnings,
        )
        self.registry.set_service_member_tasks(1001, "kitchen", "B", [TEAM])
        with self.assertRaisesRegex(ValueError, "допуска"):
            self.registry.set_replacement(
                "kitchen", "2026-11-15", "пироги", "P", "Q", 506, "", "admin", 1001,
            )

    def test_primary_decline_survives_removed_duty_permission_without_bypassing_authorization(self):
        self.approve()
        self.registry.set_service_member_tasks(1001, "kitchen", "A", [HEAD])
        day = "2026-11-01"
        with self.assertRaises(PermissionError):
            self.registry.set_assignment_answer("kitchen", day, DUTY, 501, "declined")
        with self.assertRaises(PermissionError):
            self.registry.set_assignment_answer("kitchen", day, HEAD, 503, "declined")
        self.assertIsNone(self.registry.get_confirmation("kitchen", day, DUTY, 501))
        answers = self.registry.set_assignment_answer("kitchen", day, HEAD, 501, "declined")
        self.assertEqual(answers, [(HEAD, "A", True), (DUTY, "A", True)])
        self.assertEqual(self.registry.get_confirmation("kitchen", day, HEAD, 501), "declined")
        self.assertEqual(self.registry.get_confirmation("kitchen", day, DUTY, 501), "declined")
        self.assertEqual(
            self.registry.set_assignment_answer("kitchen", day, HEAD, 501, "declined"),
            [(HEAD, "A", False), (DUTY, "A", False)],
        )

    def test_linked_decline_remains_atomic_on_database_failure(self):
        self.approve()
        with self.registry._connection() as connection:
            connection.execute(
                "CREATE TRIGGER reject_duty_answer BEFORE INSERT ON service_confirmations "
                "WHEN NEW.task = 'Скатерти' BEGIN SELECT RAISE(ABORT, 'test'); END"
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.registry.set_assignment_answer("kitchen", "2026-11-01", HEAD, 501, "declined")
        self.assertIsNone(self.registry.get_confirmation("kitchen", "2026-11-01", HEAD, 501))

    def test_rule_changes_leave_approved_snapshot_and_guards_intact(self):
        approved = self.approve()
        self.registry.initialize(1001, -10001)
        duty = next(item for item in self.registry.plan_tasks("kitchen") if item["name"] == DUTY)
        self.assertEqual(duty["requires_any"], [HEAD, TEAM])
        self.registry.set_task_dependency(1001, "kitchen", DUTY, [])
        current = self.registry.get_schedule_plan("kitchen", "2026-11")
        self.assertEqual(current["approved_tasks"], approved["approved_tasks"])
        self.assertTrue(self.registry.replacement_assignment_errors(
            "kitchen", "2026-11-01", HEAD, "A", "C",
        ))
        self.registry.initialize(1001, -10001)
        self.assertEqual(self.registry.get_schedule_plan("kitchen", "2026-11")["approved"], self.slots)

    def test_valid_excel_import_preserves_rule_and_rejects_hidden_day_override(self):
        tasks = schedule_plan.plan_tasks(self.registry, "kitchen")
        with self.assertRaisesRegex(ValueError, "воскресенье"):
            self.registry.import_excel_plan(
                1001, "kitchen", "2026-11", dates=list(self.slots), tasks=tasks,
                slots=self.slots, days={},
                approved_slot_days={"2026-11-01": {DUTY: {"A": -1}}},
            )
        with self.assertRaisesRegex(ValueError, "отсутствуют"):
            self.registry.import_excel_plan(
                1001, "kitchen", "2026-11", dates=list(self.slots),
                tasks=[task for task in tasks if task["name"] != DUTY],
                slots=self.slots, days={},
            )
        saved = self.registry.import_excel_plan(
            1001, "kitchen", "2026-11", dates=list(self.slots), tasks=tasks,
            slots=self.slots, days={},
        )
        self.assertEqual(saved["approved"], self.slots)
        duty = next(item for item in saved["approved_tasks"] if item["name"] == DUTY)
        self.assertEqual(duty["requires_any"], [HEAD, TEAM])

    def test_readiness_and_manual_candidate_share_rule(self):
        run = schedule_plan.free_run("kitchen", 2026, 11)
        summary = readiness.summarize(self.registry, run, assume_free=True)
        self.assertFalse(summary.plan.gaps())
        plan = self.registry.get_schedule_plan("kitchen", "2026-11")
        plan["slots"]["2026-11-01"][DUTY] = []
        candidates = schedule_plan.candidates(plan, summary, "2026-11-01", DUTY)
        self.assertEqual(set(sum(candidates, [])), {"A", "B"})

    def test_incomplete_plan_requires_exact_warning_acknowledgement(self):
        slots = {day: {**values, DUTY: [], TEAM: []}
                 for day, values in self.slots.items()}
        del slots["2026-11-08"]
        warnings = self.registry.task_assignment_warnings(
            "kitchen", slots, complete=True, month="2026-11",
        )
        self.assertTrue(any("не назначен" in item for item in warnings))
        self.assertTrue(any("не хватает" in item for item in warnings))
        self.assertTrue(any("отсутствует" in item for item in warnings))
        for acknowledged in (None, warnings[:-1]):
            with self.assertRaises(AssignmentWarnings):
                self.registry.approve_schedule_plan(
                    1001, "kitchen", "2026-11", slots, chat_id=None,
                    message_id=None, acknowledged_warnings=acknowledged,
                )
        saved = self.registry.approve_schedule_plan(
            1001, "kitchen", "2026-11", slots, chat_id=None,
            message_id=None, acknowledged_warnings=warnings,
        )
        self.assertEqual(saved["approved"], slots)
        self.assertEqual(saved["approval_warnings"], warnings)
        self.registry.initialize(1001, -10001)
        self.assertEqual(
            self.registry.get_schedule_plan("kitchen", "2026-11")["approval_warnings"],
            warnings,
        )

    def test_excel_outside_team_preserved_after_warning_confirmation(self):
        slots = {day: {**values, TEAM: ["C"], DUTY: ["B"]}
                 for day, values in self.slots.items()}
        warnings = self.registry.task_assignment_warnings(
            "kitchen", slots, complete=True, month="2026-11",
        )
        self.assertTrue(all("не входит в смену" in item for item in warnings))
        kwargs = dict(
            dates=list(slots), tasks=schedule_plan.plan_tasks(self.registry, "kitchen"),
            slots=slots, days={},
        )
        with self.assertRaises(AssignmentWarnings):
            self.registry.import_excel_plan(1001, "kitchen", "2026-11", **kwargs)
        saved = self.registry.import_excel_plan(
            1001, "kitchen", "2026-11", **kwargs,
            acknowledged_warnings=warnings,
        )
        self.assertEqual(saved["approved"], slots)
        self.assertEqual(saved["approval_warnings"], warnings)
        self.assertIn("не входит в смену", schedule_plan.month_overview(
            self.registry, 1001, "kitchen", SUNDAYS[0],
        ))

    def test_acknowledgement_does_not_override_structural_errors(self):
        slots = {**self.slots, "2026-11-01": {
            HEAD: ["A"], TEAM: ["B"], DUTY: ["A", "B"],
        }}
        with self.assertRaisesRegex(ValueError, "ровно один"):
            self.registry.approve_schedule_plan(
                1001, "kitchen", "2026-11", slots, chat_id=None, message_id=None,
                acknowledged_warnings=["anything"],
            )

    def test_publish_keeps_accepted_warnings_in_message_and_snapshot(self):
        plan = self.registry.get_schedule_plan("kitchen", "2026-11")
        for assigned in plan["slots"].values():
            assigned[DUTY] = []
        warnings = self.registry.task_assignment_warnings(
            "kitchen", plan["slots"], complete=True, month="2026-11",
        )
        bot = SimpleNamespace(
            send_message=AsyncMock(return_value=SimpleNamespace(message_id=55)),
            pin_chat_message=AsyncMock(), delete_message=AsyncMock(),
        )
        service = {
            **self.registry.get_service("kitchen"),
            "enabled": True, "group_chat_id": -123,
        }
        with self.assertRaises(AssignmentWarnings):
            asyncio.run(schedule_plan.publish(
                bot, self.registry, service, plan, 1001,
                logging.getLogger("test-dependencies"), 4096,
            ))
        bot.send_message.assert_not_awaited()
        saved = asyncio.run(schedule_plan.publish(
            bot, self.registry, service,
            plan, 1001, logging.getLogger("test-dependencies"), 4096,
            acknowledged_warnings=warnings,
        ))
        self.assertEqual(saved["approval_warnings"], warnings)
        self.assertIn("Утверждено с предупреждениями", "".join(
            call.args[1] for call in bot.send_message.await_args_list
        ))


class DependentHandlerTest(unittest.TestCase):
    def setUp(self):
        DependentRegistryTest.setUp(self)

    def test_primary_refusal_notifies_admins_and_starts_both_replacements_after_permit_removed(self):
        self.registry.approve_schedule_plan(
            1001, "kitchen", "2026-11", self.slots, chat_id=None, message_id=None,
        )
        self.registry.set_service_member_tasks(1001, "kitchen", "A", [HEAD])
        log = logging.getLogger("test-dependencies")
        context = SimpleNamespace(
            service_registry=self.registry, admin_id=1001, group_chat_id=-10001,
            timezone=ZoneInfo("UTC"), interaction_logger=log, debug_logger=log,
            bot=SimpleNamespace(send_message=AsyncMock(), delete_message=AsyncMock()),
            on_assignment_declined=AsyncMock(),
        )
        dispatcher = SimpleNamespace(include_router=MagicMock())
        with patch.object(handlers, "router", handlers.Router()):
            handlers.register(dispatcher, context)
        handler = next(item.callback
                       for call in dispatcher.include_router.call_args_list
                       for item in call.args[0].callback_query.handlers
                       if item.callback.__name__ == "on_personal_answer")
        callback = SimpleNamespace(
            data=f"pcf:d:kitchen:20261115:{task_token(HEAD)}",
            from_user=SimpleNamespace(id=501), answer=AsyncMock(),
            message=SimpleNamespace(
                text="Напоминание", edit_text=AsyncMock(), edit_reply_markup=AsyncMock(),
            ),
        )
        with patch("bot.handlers.datetime") as clock, patch(
            "bot.handlers.notify_admins_declined", new=AsyncMock(return_value=1),
        ) as notify:
            clock.now.return_value = SimpleNamespace(date=lambda: date(2026, 11, 10))
            clock.strptime.side_effect = datetime.strptime
            asyncio.run(handler(callback))
        self.assertEqual([call.args[4] for call in notify.await_args_list], [HEAD, DUTY])
        self.assertEqual(
            [call.args[2] for call in context.on_assignment_declined.await_args_list], [HEAD, DUTY],
        )
        self.assertEqual(self.registry.get_confirmation("kitchen", "2026-11-15", HEAD, 501), "declined")
        self.assertEqual(self.registry.get_confirmation("kitchen", "2026-11-15", DUTY, 501), "declined")

    def test_dependency_configuration_checks_session_and_permissions(self):
        for task in ("X", "Y", "Z"):
            self.registry.add_service_task(1001, "kitchen", task)
        log = logging.getLogger("test-dependencies")
        context = SimpleNamespace(
            service_registry=self.registry, admin_id=1001, group_chat_id=-10001,
            timezone=ZoneInfo("UTC"), interaction_logger=log, debug_logger=log,
            bot=SimpleNamespace(
                send_message=AsyncMock(return_value=SimpleNamespace(message_id=55)),
                delete_message=AsyncMock(),
            ),
        )
        dispatcher = SimpleNamespace(include_router=MagicMock())
        handlers.register(dispatcher, context)
        admin = dispatcher.include_router.call_args_list[0].args[0]
        handler = next(item.callback for item in admin.callback_query.handlers
                       if item.callback.__name__ == "on_service_tasks")
        message = SimpleNamespace(
            from_user=SimpleNamespace(id=1001),
            chat=SimpleNamespace(id=1001, type="private"),
            message_id=1, answer=AsyncMock(return_value=SimpleNamespace(message_id=22)),
        )

        def click(data, actor=1001):
            callback = SimpleNamespace(
                data=data, from_user=SimpleNamespace(id=actor),
                message=message, answer=AsyncMock(),
            )
            asyncio.run(handler(callback))
            return callback

        def button(prefix, text=None):
            markup = message.answer.await_args.kwargs["reply_markup"]
            return next(item.callback_data for row in markup.inline_keyboard
                        for item in row if item.callback_data.startswith(prefix)
                        and (text is None or text in item.text))

        click("svc_tasks:open:kitchen")
        click(button("svc_tasks:card:", DUTY))
        click(button("svc_tasks:dep:"))
        click(button("svc_tasks:danchor:", HEAD))
        click(button("svc_tasks:dpage:", "➡️"))
        save = button("svc_tasks:dsave:")
        click(save)
        task = next(item for item in self.registry.plan_tasks("kitchen") if item["name"] == DUTY)
        self.assertEqual(task["requires_any"], [TEAM])
        stale = click(save)
        self.assertIn("устарел", stale.answer.await_args.args[0])
        forbidden = click("svc_tasks:open:kitchen", 501)
        self.assertIn("прав", forbidden.answer.await_args.args[0])
