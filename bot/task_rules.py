"""Rules for an additional duty performed by a member of the day's team."""
from __future__ import annotations

from collections.abc import Mapping


def dependent_tasks(tasks: list[dict]) -> dict[str, tuple[str, ...]]:
    return {
        task["name"]: tuple(task["requires_any"])
        for task in tasks if task.get("requires_any")
    }


class AssignmentWarnings(ValueError):
    def __init__(self, warnings: list[str]):
        self.warnings = warnings
        super().__init__("\n".join(warnings))


def assignment_issues(
    tasks: list[dict], slots: Mapping[str, Mapping[str, list[str]]],
    skills: Mapping[str, set[str]], *, complete: bool,
) -> tuple[list[str], list[str]]:
    rules = dependent_tasks(tasks)
    errors = []
    warnings = []
    known = {task["name"] for task in tasks}
    if any(not set(anchors) <= known for anchors in rules.values()):
        return ["В графике отсутствует основная задача дополнительной обязанности."], []
    for day, assigned in sorted(slots.items()):
        for task, anchors in rules.items():
            people = assigned.get(task, [])
            if len(people) > 1 or len(people) != len(set(people)):
                errors.append(f"{day} · {task}: нужен ровно один ответственный.")
            if complete and not people:
                warnings.append(f"{day} · {task}: ответственный не назначен.")
            team = {
                person for anchor in anchors for person in assigned.get(anchor, [])
            }
            if complete:
                for anchor in anchors:
                    minimum = next(
                        item.get("needed", 1) for item in tasks
                        if item["name"] == anchor
                    )
                    if len(assigned.get(anchor, [])) < minimum:
                        warnings.append(
                            f"{day} · {anchor}: не хватает ответственных "
                            f"({len(assigned.get(anchor, []))} из {minimum})."
                        )
            for name in (task, *anchors):
                values = assigned.get(name, [])
                if len(values) != len(set(values)):
                    errors.append(f"{day} · {name}: человек назначен дважды.")
            for person in people:
                if person not in team:
                    warnings.append(f"{day} · {task}: ответственный не входит в смену.")
                if task not in skills.get(person, set()):
                    errors.append(f"{day} · {task}: у человека нет допуска к задаче.")
            for anchor in anchors:
                for person in assigned.get(anchor, []):
                    if sum(person in assigned.get(other, []) for other in anchors) > 1:
                        errors.append(
                            f"{day}: старший и участник смены должны быть разными людьми."
                        )
    return list(dict.fromkeys(errors)), list(dict.fromkeys(warnings))


def assignment_errors(
    tasks: list[dict], slots: Mapping[str, Mapping[str, list[str]]],
    skills: Mapping[str, set[str]], *, complete: bool,
) -> list[str]:
    errors, warnings = assignment_issues(tasks, slots, skills, complete=complete)
    return errors + warnings


def compatible(tasks: list[dict], first: str, second: str) -> bool:
    rules = dependent_tasks(tasks)
    return second in rules.get(first, ()) or first in rules.get(second, ())
