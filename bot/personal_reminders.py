"""Read-only personal reminder forecast and shared delivery conditions."""
from __future__ import annotations

from datetime import date, datetime, timedelta

from .month_summary import month_end
from .presentation import (
    apply_replacements,
    display_name,
    ordered_task_assignments,
    task_assignment_names,
    task_is_informational,
)
from .schedule_plan import plan_events
from .scheduler import reminder_send_datetime
from .service_registry import ServiceRegistry
from .services import sort_tasks_for_display


def personal_rule_label(days: int, clock: str) -> str:
    if days == 0:
        return f"в день служения в {clock}"
    unit = "день" if days == 1 else "дня" if days <= 4 else "дней"
    return f"за {days} {unit} в {clock}"


def personal_reminder_block_reason(
    schedule: dict,
    assignments: dict,
    *,
    event_found: bool,
    approved: bool,
    delivered: bool = False,
    confirmation: str | None = None,
) -> str | None:
    if confirmation:
        return "принял" if confirmation == "confirmed" else "отказался"
    if delivered:
        return "уже отправлено"
    if not schedule["service_enabled"]:
        return "служение выключено"
    if not approved:
        return "график не утверждён"
    if not schedule.get("name"):
        return "нет привязки Telegram к задаче"
    if not schedule["notifications_enabled"]:
        return "уведомления выключены"
    if not schedule["schedule_enabled"]:
        return "правило выключено"
    task_info = assignments.get(schedule["task"], [])
    if task_is_informational(task_info):
        return "информационная задача — без личных уведомлений"
    if not event_found or schedule["name"] not in task_assignment_names(task_info):
        return "нет назначения"
    return None


def personal_reminder_forecast(
    registry: ServiceRegistry,
    actor_id: int, service_id: str, month: date, now: datetime,
) -> list[str]:
    """Use actual event dates, including shifted slots of adjacent plans."""
    schedules = registry.list_service_user_reminder_schedules(actor_id, service_id)
    contacts = registry.list_responsible_contacts(actor_id, service_id)
    display_names = registry.service_display_names(service_id)
    service = registry.get_service(service_id)
    if service is None:
        raise ValueError("Служение не найдено.")
    people = registry.readiness_people(service_id)
    known_names = {person["name"] for person in people}
    first, last = month.replace(day=1), month_end(month.year, month.month)
    deliveries, confirmations, enabled_recipients = (
        registry.get_personal_reminder_forecast_state(
            actor_id, service_id, first, last,
        )
    )
    selected_plan = registry.get_schedule_plan(service_id, f"{first:%Y-%m}")
    approved = selected_plan is not None and selected_plan["approved"] is not None
    assignments_by_date: dict[date, dict] = {}
    cursor = (first - timedelta(days=7)).replace(day=1)
    while cursor <= last + timedelta(days=7):
        plan = registry.get_schedule_plan(service_id, f"{cursor:%Y-%m}")
        if plan is not None and plan["approved"] is not None:
            for event in plan_events(plan, people):
                if not first <= event.event_date <= last:
                    continue
                assignments = assignments_by_date.setdefault(event.event_date, {})
                for item in event.assignments:
                    info = assignments.setdefault(
                        item.task, {"names": [], "is_informational": True}
                    )
                    if item.name not in info["names"]:
                        info["names"].append(item.name)
                    info["is_informational"] &= item.is_informational
        cursor = (cursor + timedelta(days=32)).replace(day=1)

    entries = []
    if not approved:
        entries.append(
            "⚠️ График выбранного месяца не утверждён. Прогноз по Excel "
            "не строится. Ниже могут быть даты соседнего утверждённого графика."
        )
    scheduled_ids = set()
    for event_date, raw_assignments in sorted(assignments_by_date.items()):
        event_iso = event_date.isoformat()
        assignments = apply_replacements(
            raw_assignments, registry.list_replacements(service_id, event_iso)
        )
        for task, info in ordered_task_assignments(assignments, service_id):
            if task_is_informational(info):
                continue
            for name in sorted(task_assignment_names(info), key=str.casefold):
                contact = next(
                    (item for item in contacts
                     if item["task"] == task and item["name"] == name),
                    None,
                )
                person_id = contact["telegram_id"] if contact else None
                rules = [
                    item for item in schedules
                    if item["actor_id"] == person_id and item["task"] == task
                    and item["name"] == name
                ]
                label = display_names.get(name) or display_name(contact, name)
                base, separator, suffix = name.rpartition(" · ")
                if name not in known_names and separator and suffix.isdecimal():
                    label = f"{base} (нет актуальной привязки)"
                title = f"📅 {event_date:%d.%m.%Y} · {task} · {label}"
                if not rules:
                    confirmation = confirmations.get((event_iso, task, person_id))
                    if confirmation:
                        reason = "принял" if confirmation == "confirmed" else "отказался"
                        entries.append(f"{title}\nСтатус: {reason}.")
                        continue
                    if not person_id or person_id <= 0:
                        reason = "нет привязки Telegram к задаче"
                    elif not service["enabled"]:
                        reason = "служение выключено"
                    elif person_id not in enabled_recipients:
                        reason = "уведомления выключены"
                    else:
                        reason = "нет включённого правила"
                    entries.append(f"{title}\nНе отправляется: {reason}.")
                for rule in rules:
                    scheduled_ids.add(rule["schedule_id"])
                    reason = personal_reminder_block_reason(
                        rule, assignments, event_found=True, approved=True,
                        delivered=(rule["schedule_id"], event_iso) in deliveries,
                        confirmation=confirmations.get(
                            (event_iso, task, rule["actor_id"])
                        ),
                    )
                    send_at = reminder_send_datetime(
                        event_date, rule["reminder_days"],
                        rule["reminder_time"], now.tzinfo,
                    )
                    if reason is None:
                        reason = (
                            "время прошло, отправка не зафиксирована"
                            if send_at < now else "запланировано"
                        )
                    origin = "своё" if rule["origin"] == "user" else "общее"
                    entries.append(
                        f"{title}\nПравило: {origin}, "
                        f"{personal_rule_label(rule['reminder_days'], rule['reminder_time'])}.\n"
                        f"Отправка: {send_at:%d.%m.%Y %H:%M} — {reason}."
                    )

    for rule in sort_tasks_for_display(
        schedules, service_id, name=lambda rule: rule["task"],
    ):
        if rule["schedule_id"] in scheduled_ids:
            continue
        reason = personal_reminder_block_reason(
            rule, {}, event_found=False, approved=approved,
        )
        name = display_name(rule, "Получатель без привязки")
        entries.append(
            f"• {name} · {rule['task']}\n"
            f"Правило: {personal_rule_label(rule['reminder_days'], rule['reminder_time'])}"
            f" — {reason}."
        )
    has_assignments = any(
        task_assignment_names(info) and not task_is_informational(info)
        for assignments in assignments_by_date.values() for info in assignments.values()
    )
    if not has_assignments and approved:
        entries.insert(0, "В утверждённом графике нет назначений на этот месяц.")
    if not schedules and not has_assignments:
        entries.append("Нет ответственных с личными напоминаниями или назначениями.")
    return entries
