"""Published notes attached to an approved event Sunday."""

from __future__ import annotations

import hashlib
import unicodedata
from datetime import date, timedelta

from .schedule_plan import task_moments
from .presentation import format_informational_notes


MAX_EVENT_NOTE_LENGTH = 2000


class EventNoteResolutionError(ValueError):
    pass


def validate_event_note_text(text: str) -> str:
    if not isinstance(text, str):
        raise ValueError("Введите текст заметки.")
    if len(text) > MAX_EVENT_NOTE_LENGTH:
        raise ValueError(
            f"Заметка длиннее {MAX_EVENT_NOTE_LENGTH} символов."
        )
    if not text.strip():
        raise ValueError("Заметка не может быть пустой.")
    if any(
        unicodedata.category(char) == "Cc" and char != "\n"
        for char in text
    ):
        raise ValueError(
            "В заметке нельзя использовать управляющие символы "
            "(кроме переноса строки)."
        )
    return text


def validate_event_sunday(value: str | date) -> date:
    if isinstance(value, date):
        result = value
    else:
        try:
            result = date.fromisoformat(value)
        except (TypeError, ValueError):
            raise ValueError("Укажите дату в формате ГГГГ-ММ-ДД.") from None
        if result.isoformat() != value:
            raise ValueError("Укажите дату в формате ГГГГ-ММ-ДД.")
    if result.weekday() != 6:
        raise ValueError("Дата собрания должна быть воскресеньем.")
    return result


def source_sunday_for_event(registry, service_id: str, event_day: date,
                            task: str | None = None) -> str | None:
    """Find the approved Sunday whose snapshot places this task on event_day."""
    people = registry.readiness_people(service_id)
    by_key = {person["key"]: person for person in people}
    months = {
        (event_day + timedelta(days=offset)).strftime("%Y-%m")
        for offset in range(-6, 7)
    }
    matches: list[str] = []
    for month in sorted(months):
        plan = registry.get_schedule_plan(service_id, month)
        if not plan or plan.get("approved") is None:
            continue
        approved = plan["approved"]
        published = {
            **plan,
            "slots": approved,
            "tasks": plan.get("approved_tasks") or plan.get("tasks", []),
            "days": plan.get("approved_days") or {},
            "approved_slot_days": plan.get("approved_slot_days") or {},
        }
        for sunday, tasks in approved.items():
            for name in tasks:
                if task and name != task:
                    continue
                moments = task_moments(
                    published, by_key, sunday, name, approved=True
                )
                if any(moment == event_day for moment, _ in moments):
                    matches.append(sunday)
    if len(set(matches)) > 1:
        raise EventNoteResolutionError(
            "На эту дату приходятся разные воскресные события. "
            "Создайте отдельное напоминание для каждой задачи."
        )
    return matches[0] if matches else None


def resolve_event_note(registry, service_id: str, event_day: date,
                       task: str | None = None, *, template: str | None = None,
                       public_url: str | None = None) -> str:
    if template is not None and "{event_notes" not in template:
        return ""
    source_sunday = source_sunday_for_event(
        registry, service_id, event_day, task
    )
    if source_sunday is None:
        return ""
    notes = []
    published = registry.resolve_event_note(service_id, source_sunday)
    if public_url is not None:
        source_key = hashlib.sha256(public_url.encode()).hexdigest()
        assignments: dict[str, dict] = {}
        for service, day, name, value in registry.get_schedule_notes(source_key):
            if service != "assembly" or day != source_sunday:
                continue
            info = assignments.setdefault(name, {"names": [], "is_informational": True})
            if value and value not in info["names"]:
                info["names"].append(value)
        excel_notes = format_informational_notes(assignments)
        if excel_notes:
            notes.append(excel_notes)
    if published:
        notes.append(published)
    return "\n\n".join(notes)
