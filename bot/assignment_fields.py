"""Approved weekly assignments used by group reminder template fields."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from string import Formatter
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .service_registry import ServiceRegistry


_REFERENCE = re.compile(
    r"service_assignments\[([a-z][a-z0-9_-]*)(?::([a-f0-9]{10}))?\]"
)


class AssignmentFieldResolutionError(ValueError):
    """A stored assignment reference cannot be resolved unambiguously."""


def assignment_reference(value: str) -> tuple[str, str | None] | None:
    match = _REFERENCE.fullmatch(value)
    return (match[1], match[2]) if match else None


def assignment_field(service_id: str, task: str | None = None) -> str:
    from .service_registry import task_token

    reference = service_id + (f":{task_token(task)}" if task else "")
    value = f"service_assignments[{reference}]"
    if assignment_reference(value) is None:
        raise ValueError("Не удалось создать поле для этого служения.")
    return "{" + value + "}"


@dataclass
class AssignmentFields:
    week: str = ""
    services: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    state: tuple = ()


def resolve_assignment_fields(
    registry: ServiceRegistry, service_id: str, event_day: date, template: str, cutoff: date,
    task: str | None = None,
) -> AssignmentFields:
    from .event_notes import source_sunday_for_event
    from .presentation import display_name, task_emoji
    from .schedule_plan import _effective_plan_assignments
    from .excel_parser import is_informational_name
    from .service_registry import task_token
    from .services import sort_tasks_for_display
    from .ui import user_link

    fields = [name for _, name, _, _ in Formatter().parse(template) if name]
    references = {
        name: reference for name in fields
        if (reference := assignment_reference(name)) is not None
    }
    result = AssignmentFields()
    if "week_task_assignments" not in fields and not references:
        return result
    sunday = source_sunday_for_event(registry, service_id, event_day, task)
    if sunday is None:
        raise AssignmentFieldResolutionError(
            "Не найдено воскресное событие утверждённого графика."
        )
    states = []
    cache: dict[tuple[str, str | None, bool], str] = {}

    def render(source_id: str, token: str | None, *, own: bool) -> str:
        cache_key = source_id, token, own
        if cache_key in cache:
            return cache[cache_key]
        service = registry.get_service(source_id)
        if service is None:
            raise AssignmentFieldResolutionError(
                "Служение в поле назначений больше не существует."
            )
        plan = registry.get_schedule_plan(source_id, sunday[:7])
        if plan is None or plan.get("approved") is None:
            result.warnings.append(
                f"«{service['name']}»: нет утверждённого графика; поле пустое."
            )
            states.append((cache_key, "unapproved"))
            cache[cache_key] = ""
            return ""
        names = {
            entry["name"] for entry in plan.get("approved_tasks") or []
        } | {
            name for slots in plan["approved"].values() for name in slots
        }
        selected = None
        if token:
            matches = [name for name in names if task_token(name) == token]
            if len(matches) != 1:
                raise AssignmentFieldResolutionError(
                    f"«{service['name']}»: задача поля удалена, переименована "
                    "или неоднозначна. Выберите поле заново."
                )
            selected = matches[0]
        people = registry.readiness_people(source_id)
        by_key = {person["key"]: person for person in people}
        assignments = _effective_plan_assignments(
            registry, plan, people, keyed=True, source_sunday=sunday,
        )
        lines = []
        rows = []
        for day, tasks in sorted(assignments.items()):
            if day < cutoff:
                continue
            for name, keys in sort_tasks_for_display(
                tasks.items(), source_id, name=lambda item: item[0],
            ):
                if (selected and name != selected) or is_informational_name(name)[0]:
                    continue
                for key in dict.fromkeys(keys):
                    if not key:
                        continue
                    person = by_key.get(key)
                    label = display_name(person, key)
                    if own and person and person.get("telegram_id"):
                        label = user_link(person["telegram_id"], label)
                    day_label = f" — {day:%d.%m}" if own and day.isoformat() != sunday else ""
                    lines.append(f"{task_emoji(name)} {name}{day_label} — {label}")
                    rows.append((day.isoformat(), name, key, label))
        value = "\n".join(lines)
        states.append((cache_key, "approved", tuple(rows)))
        cache[cache_key] = value
        return value

    if "week_task_assignments" in fields:
        result.week = render(service_id, None, own=True)
    for reference, (source_id, token) in references.items():
        key = reference[len("service_assignments["):-1]
        result.services[key] = render(source_id, token, own=False)
    result.state = (sunday, cutoff.isoformat(), tuple(states))
    return result
