"""Published notes attached to an approved event Sunday."""

from __future__ import annotations

import hashlib
import unicodedata
from datetime import date, datetime, time, timedelta

from .schedule_plan import task_moments
from .presentation import format_informational_notes
from .group_send import send_to_service_group
from .ui import plain_text


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
                       public_url: str | None = None,
                       include_scheduled: bool = True) -> str:
    if template is not None and "{event_notes" not in template:
        return ""
    source_sunday = source_sunday_for_event(
        registry, service_id, event_day, task
    )
    if source_sunday is None:
        return ""
    notes = []
    if include_scheduled:
        published = registry.resolve_event_note(service_id, source_sunday)
    else:
        published = registry.resolve_event_note(service_id, source_sunday,
                                                 include_scheduled=False)
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


async def send_scheduled_event_notes(bot, registry, now: datetime, logger,
                                     notify_failure) -> None:
    """Deliver published notes independently of service reminder schedules."""
    for note in registry.list_event_notes():
        send_at = note["published_send_at"]
        if not note["published"] or not send_at:
            continue
        due = datetime.fromisoformat(send_at).replace(tzinfo=now.tzinfo)
        end = datetime.combine(date.fromisoformat(note["event_sunday"]),
                               time.max, tzinfo=now.tzinfo)
        if now < due:
            continue
        targets = note["published_targets"]
        if targets is None and now > end + timedelta(days=1):
            pending = any(
                service["enabled"] and not registry.event_note_was_delivered(
                    note["event_sunday"], service["id"],
                )
                for service in registry.list_services()
            )
            if not registry.event_note_expiry_alerted(note["event_sunday"], "*"):
                if not pending or await notify_failure(
                    note["event_sunday"], "все служения",
                    "срок отправки прошёл; проверьте доставку адресатам",
                ):
                    registry.mark_event_note_expiry_alerted(note["event_sunday"], "*")
            continue
        available = {service["id"]: service for service in registry.list_services()}
        selected = (
            {service_id for service_id, service in available.items() if service["enabled"]}
            if targets is None else set(targets)
        )
        services = []
        for service_id in sorted(selected):
            if registry.event_note_was_delivered(note["event_sunday"], service_id):
                continue
            if now > end:
                if not registry.event_note_expiry_alerted(note["event_sunday"], service_id):
                    if await notify_failure(note["event_sunday"], service_id,
                                            "срок отправки прошёл"):
                        registry.mark_event_note_expiry_alerted(
                            note["event_sunday"], service_id,
                        )
                continue
            service = available.get(service_id)
            if service is None or not service["enabled"]:
                if targets is not None:
                    await notify_failure(note["event_sunday"], service_id,
                                         "служение отключено или недоступно")
                continue
            services.append(service)
        for service in services:
            service_id = service["id"]
            current = registry.get_event_note(note["event_sunday"])
            if not current or any(
                current[key] != note[key] for key in
                ("published_body", "published_targets", "published_send_at")
            ):
                break
            service = registry.get_service(service_id)
            if service is None or not service["enabled"]:
                await notify_failure(note["event_sunday"], service_id,
                                     "служение отключено или недоступно")
                continue
            if not service["group_chat_id"]:
                await notify_failure(note["event_sunday"], service_id,
                                     "чат служения не подключён")
                continue
            text = (
                f"📣 Информация к собранию {date.fromisoformat(note['event_sunday']):%d.%m.%Y}\n\n"
                f"{note['published_body']}"
            )
            try:
                await send_to_service_group(
                    bot.send_message, service["group_chat_id"], plain_text(text),
                    service, registry=registry, logger=logger, parse_mode=None,
                )
            except Exception:
                logger.exception("Не удалось отправить общую заметку %s служению %s",
                                 note["event_sunday"], service_id)
                await notify_failure(note["event_sunday"], service_id,
                                     "ошибка отправки; подробности в журнале")
                continue
            registry.mark_event_note_delivered(note["event_sunday"], service_id)
