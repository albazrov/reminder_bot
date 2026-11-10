"""Formatting helpers for user-facing assignment summaries."""
from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any

from .ui import user_link


_TASK_EMOJIS = (
    ("аудио", "🎧"),
    ("камер", "📷"),
    ("пульт", "🎛️"),
    ("компьютер", "💻"),
    ("монтаж", "🎬"),
    ("микшер", "🎚️"),
    ("звук", "🎚️"),
    ("слайд", "🖥️"),
    ("презентац", "🖥️"),
    ("трансляц", "📡"),
    ("поряд", "🧹"),
    ("кухн", "🍽️"),
    ("кофе", "☕"),
    ("ведущ", "🎤"),
    ("проповед", "📖"),
    ("хкц", "🧭"),
    ("сборк", "🧰"),
    ("спевк", "🎶"),
    ("пирог", "🥧"),
    ("старш", "⭐"),
    ("смен", "👥"),
    ("книг", "📚"),
    ("уборк", "🧹"),
    ("детск", "🧸"),
)


def task_emoji(task: str) -> str:
    normalized = task.casefold()
    return next(
        (emoji for fragment, emoji in _TASK_EMOJIS if fragment in normalized),
        "📌",
    )


def display_name(person: Mapping | None, fallback: str = "") -> str:
    """Only a label: matching and stored assignment keys keep the full name."""
    return (person or {}).get("short_name") or (person or {}).get("name") or fallback


def task_assignment_names(task_info: Mapping | list[str]) -> list[str]:
    if isinstance(task_info, Mapping):
        names = task_info.get("names", [])
        return list(names) if isinstance(names, (list, tuple, set)) else []
    return list(task_info)


def task_is_informational(task_info: Mapping | list[str]) -> bool:
    return (
        bool(task_info.get("is_informational", False))
        if isinstance(task_info, Mapping)
        else False
    )


def ordered_task_assignments(
    assignments: dict[str, dict[str, Any] | list[str]],
) -> list[tuple[str, dict[str, Any] | list[str]]]:
    priority = {"старший": 0, "в смене": 1}
    return sorted(
        assignments.items(),
        key=lambda item: priority.get(item[0].strip().casefold(), 2),
    )


def person_label(
    task: str,
    name: str,
    contacts: dict[tuple[str, str], dict] | None = None,
    *,
    mark_unlinked: bool = False,
    display_names: Mapping[str, str] | None = None,
) -> str:
    """Имя ответственного: ссылка на Telegram, если человек привязан."""
    contact = (contacts or {}).get((task, name))
    username = contact.get("username") if contact else None
    shown_name = (display_names or {}).get(name) or display_name(contact, name)
    label = f"{shown_name} (@{username})" if username else shown_name
    telegram_id = contact.get("telegram_id") if contact else None
    if telegram_id:
        return user_link(telegram_id, label)
    if contact is None and mark_unlinked:
        return f"{name} · не привязан"
    return label


def format_task_responsibles(
    task: str,
    names: list[str],
    contacts: dict[tuple[str, str], dict] | None = None,
    *,
    display_names: Mapping[str, str] | None = None,
) -> str:
    """Поле {task_responsibles}: ответственные одной задачи через запятую."""
    return ", ".join(
        person_label(
            task, name, contacts, mark_unlinked=True, display_names=display_names,
        ) for name in names
    )


def format_task_assignments(
    assignments: dict[str, dict[str, Any] | list[str]],
    contacts: dict[tuple[str, str], dict] | None = None,
    *,
    mark_unlinked: bool = False,
    display_names: Mapping[str, str] | None = None,
) -> str:
    if not assignments:
        return "⚪ Ответственные не указаны"

    groups = []
    for task, task_info in ordered_task_assignments(assignments):
        if task_is_informational(task_info):
            continue
        names = task_assignment_names(task_info)
        lines = [f"{task_emoji(task)} {task}"]
        if not names:
            lines.append("• Ответственные не указаны")
        for name in names:
            lines.append(
                "• " + person_label(
                    task, name, contacts, mark_unlinked=mark_unlinked,
                    display_names=display_names,
                )
            )
        groups.append("\n".join(lines))
    return "\n\n".join(groups) or "⚪ Ответственные не указаны"

def informational_label(task: str, values) -> str:
    """«2я часть» + соседняя ячейка «нет» → «2я часть нет»."""
    values = [value for value in values if value]
    return " ".join([task, ", ".join(values)]) if values else task


def format_informational_notes(
    assignments: dict[str, dict[str, Any] | list[str]],
) -> str:
    notes = []
    for task, task_info in assignments.items():
        if not task_is_informational(task_info):
            continue
        notes.append(f"📌 {informational_label(task, task_assignment_names(task_info))}")
    return "\n".join(notes)


def apply_replacements(assignments: dict, replacements: list[dict]) -> dict:
    """Копия назначений с заменами (по порядку назначения: цепочки
    «А → Б → В» дают В). Замена главнее графика: если в графике на эту
    задачу уже нет заменённого, заменяющий всё равно остаётся."""
    if not replacements:
        return assignments
    result = copy.deepcopy(assignments)
    for item in replacements:
        task_info = result.get(item["task"])
        if task_info is None:
            continue
        if isinstance(task_info, dict):
            if task_info.get("is_informational"):
                continue
            names = list(task_info.get("names", []))
        else:
            names = list(task_info)
        if item["old_name"] in names:
            names = [
                item["new_name"] if name == item["old_name"] else name
                for name in names
            ]
        elif item["new_name"] not in names:
            names.append(item["new_name"])
        names = list(dict.fromkeys(names))
        if isinstance(task_info, dict):
            task_info["names"] = names
        else:
            result[item["task"]] = names
    return result


def is_replaced(replacements: list[dict], task: str, name: str) -> bool:
    """Человек уже заменён на эту дату (отказ из старого напоминания).
    Если любая замена вернула его в задачу (А→Б→А или В→А), он снова в
    задаче."""
    current = {name}
    for item in replacements:
        if item["task"] != task:
            continue
        if item["old_name"] in current:
            current.discard(item["old_name"])
            current.add(item["new_name"])
        elif item["new_name"] == name:
            current.add(name)
    return name not in current


WEEKDAYS = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")


def personal_schedule_lines(
    events, linked: set, labels: Mapping[str, str]
) -> list[str]:
    """Строки «🗓 дата, день» и «эмодзи СЛУЖЕНИЕ · задача» для человека.

    linked — {(service_id, task, имя в графике)} его привязок."""
    lines: list[str] = []
    for event in events:
        tasks = sorted(
            {
                assignment.task
                for assignment in event.assignments
                if not assignment.is_informational
                and (event.service_id, assignment.task, assignment.name) in linked
            }
        )
        if not tasks:
            continue
        label = labels.get(event.service_id, event.service_id)
        day = (
            f"🗓 {event.event_date:%d.%m.%Y}, "
            f"{WEEKDAYS[event.event_date.weekday()]}"
        )
        if day not in lines:
            if lines:
                lines.append("")
            lines.append(day)
        lines.extend(f"{task_emoji(task)} {label} · {task}" for task in tasks)
    return lines
