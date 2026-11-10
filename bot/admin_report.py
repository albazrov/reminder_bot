"""Отчёт администраторам служения о напоминании (этап 3.2).

Отчёт уходит в личные чаты админов служения после группового напоминания
и редактируется, когда участники отвечают «Принял» / «Не смогу». Один
отчёт на админа и дату события: повторные напоминания той же даты
обновляют его, а не присылают новый.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import date

from .presentation import (
    display_name,
    apply_replacements,
    format_task_assignments,
    ordered_task_assignments,
    task_assignment_names,
    task_is_informational,
)
from .service_registry import DEFAULT_ADMIN_REPORT_TEMPLATE
from .ui import render_edit_screen, render_screen_chunks, send_telegram_screen

# Ошибки Telegram, после которых отчёт нужно отправить заново.
_GONE_MESSAGE_ERRORS = (
    "message to edit not found",
    "message can't be edited",
    "message_id_invalid",
)
_TRUNCATED_NOTE = "… отчёт сокращён: не помещается в одно сообщение"

_CONFIRMATION_LABELS = {
    "confirmed": "✅ подтвердил(а)",
    "declined": "🚫 не сможет",
}


class _Values(dict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def _people(assignments: dict) -> list[tuple[str, str]]:
    return [
        (task, name)
        for task, task_info in ordered_task_assignments(assignments)
        if not task_is_informational(task_info)
        for name in task_assignment_names(task_info)
    ]


_SHORTENED = "… сокращено: не помещается в одно сообщение"


def build_report_text(
    template: str | None,
    service_name: str,
    event_date: date,
    assignments: dict,
    contacts_by_name: dict[tuple[str, str], dict],
    notifications_enabled: Callable[[int], bool],
    confirmations: dict[tuple[str, int], str],
    limit: int | None = None,
    *,
    display_names: dict[str, str] | None = None,
) -> str:
    """confirmations — {(задача, Telegram ID): status}: ответ принадлежит
    аккаунту, а не имени, поэтому перепривязка имени не переносит чужой
    ответ. С limit длинный отчёт сокращается так, чтобы ответы
    участников оставались видны: сначала сворачиваются статусы доставки,
    затем список назначений."""
    people = _people(assignments)
    delivery = []
    answers = []
    for task, name in people:
        contact = contacts_by_name.get((task, name))
        shown_name = (display_names or {}).get(name) or display_name(contact, name)
        telegram_id = contact.get("telegram_id") if contact else None
        if not telegram_id:
            delivery.append(f"⚪ {shown_name} ({task}) — тг аккаунт к боту не подключен")
        elif not notifications_enabled(telegram_id):
            delivery.append(f"🔕 {shown_name} ({task}) — уведомления выключены")
        else:
            delivery.append(f"✅ {shown_name} ({task}) — уведомления включены")
        status = confirmations.get((task, telegram_id)) if telegram_id else None
        label = _CONFIRMATION_LABELS.get(status, "⏳ нет ответа")
        answers.append(f"{label} — {shown_name} ({task})")
    values = {
        "service_name": service_name,
        "sunday_date": f"{event_date:%d.%m.%Y}",
        "task_assignments": format_task_assignments(
            assignments, contacts_by_name, mark_unlinked=True, display_names=display_names,
        ),
        "delivery_status": "\n".join(delivery) or "⚪ нет назначений",
        "confirmation_status": "\n".join(answers) or "⚪ нет назначений",
    }
    text_template = template or DEFAULT_ADMIN_REPORT_TEMPLATE

    def render() -> str:
        return text_template.format_map(_Values(values))

    if limit is None:
        return render()
    for field in ("delivery_status", "task_assignments"):
        if _fits(render(), limit):
            break
        values[field] = _SHORTENED
    return fit_one_screen(render(), limit)


def _fits(text: str, limit: int) -> bool:
    return len(render_screen_chunks(text, limit=limit)) == 1


def fit_one_screen(text: str, limit: int) -> str:
    """Отчёт редактируется на месте, поэтому должен влезать в один экран."""
    if _fits(text, limit):
        return text
    lines = text.splitlines()

    def candidate(count: int) -> str:
        return "\n".join([*lines[:count], _TRUNCATED_NOTE])

    # Наибольшее число начальных строк, которое ещё влезает в экран.
    low, high = 0, len(lines) - 1
    while low < high:
        middle = (low + high + 1) // 2
        if _fits(candidate(middle), limit):
            low = middle
        else:
            high = middle - 1
    return candidate(low)


def report_recipients(registry, service_id: str, fallback_admin_id: int) -> list[int]:
    """Админы служения; без них — суперадмин из настроек."""
    return registry.list_service_admins(service_id) or [fallback_admin_id]


def report_text(registry, service: dict, event_date: date, assignments: dict,
                admin_actor_id: int, limit: int,
                template: str | None = None) -> str:
    """template — шаблон отчёта (черновик для предпросмотра); None —
    встроенный."""
    contacts = registry.list_responsible_contacts(admin_actor_id, service["id"])
    # Замены на дату (этап 3.3) главнее назначений из графика.
    assignments = apply_replacements(
        assignments,
        registry.list_replacements(service["id"], event_date.isoformat()),
    )
    return build_report_text(
        template,
        service["name"],
        event_date,
        assignments,
        {(item["task"], item["name"]): item for item in contacts},
        lambda telegram_id: registry.is_user_notifications_enabled(
            telegram_id, service["id"]
        ),
        registry.list_confirmations(service["id"], event_date.isoformat()),
        limit=limit, display_names=registry.service_display_names(service["id"]),
    )


async def send_admin_reports(
    bot,
    registry,
    service: dict,
    event_date: date,
    assignments: dict,
    admin_actor_id: int,
    logger: logging.Logger,
    limit: int,
) -> None:
    """Отчёт текущим админам служения: отправленный на эту дату
    редактируется, недоставленный или удалённый — отправляется заново.
    Снятые с служения админы обновлений больше не получают."""
    event_iso = event_date.isoformat()
    text = report_text(
        registry, service, event_date, assignments, admin_actor_id, limit,
        service.get("admin_report_template"),
    )
    stored = {
        report["admin_id"]: report
        for report in registry.list_admin_reports(service["id"], event_iso)
    }
    recipients = report_recipients(registry, service["id"], admin_actor_id)
    for admin_id in set(stored) - set(recipients):
        registry.delete_admin_report(service["id"], event_iso, admin_id)
    for admin_id in recipients:
        report = stored.get(admin_id)
        if report is not None:
            try:
                await bot.edit_message_text(
                    render_edit_screen(text, limit=limit),
                    chat_id=admin_id,
                    message_id=report["message_id"],
                    parse_mode="HTML",
                )
            except Exception as exc:  # noqa: BLE001
                error = str(exc).lower()
                if any(mark in error for mark in _GONE_MESSAGE_ERRORS):
                    # Сообщение удалено или его нельзя изменить — новое.
                    report = None
                    logger.info(
                        "Отчёт админу %s по служению %s не обновлён (%s); "
                        "отправляется заново",
                        admin_id, service["id"], type(exc).__name__,
                    )
                elif "message is not modified" not in error:
                    # Временный сбой: без дубля, обновится в следующий раз.
                    logger.warning(
                        "Не удалось обновить отчёт админу %s по служению "
                        "%s: %s", admin_id, service["id"], type(exc).__name__,
                    )
                    continue
            if report is not None:
                if report["assignments"] != assignments:
                    registry.save_admin_report(
                        service["id"], event_iso, admin_id,
                        report["message_id"], assignments,
                    )
                continue
        try:
            sent = await send_telegram_screen(
                bot.send_message, admin_id, text, limit=limit
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Не удалось отправить отчёт админу %s по служению %s: %s",
                admin_id, service["id"], type(exc).__name__,
            )
            continue
        message_id = getattr(sent, "message_id", None)
        if message_id is not None:
            registry.save_admin_report(
                service["id"], event_iso, admin_id, message_id, assignments,
            )


async def refresh_admin_reports(
    bot,
    registry,
    service_id: str,
    event_date: date,
    admin_actor_id: int,
    logger: logging.Logger,
    limit: int,
) -> None:
    """Перестраивает отчёты после ответа участника (если отчёт на эту дату
    уже отправлялся — до группового напоминания отчёта нет)."""
    service = registry.get_service(service_id)
    reports = registry.list_admin_reports(service_id, event_date.isoformat())
    if service is None or not reports:
        return
    await send_admin_reports(
        bot, registry, service, event_date, reports[0]["assignments"],
        admin_actor_id, logger, limit,
    )


async def notify_admins_declined(
    bot,
    registry,
    service: dict,
    event_date: date,
    task: str,
    name: str,
    admin_actor_id: int,
    logger: logging.Logger,
) -> int:
    """Сообщает админам служения об ответе «Не смогу»; возвращает число
    доставленных сообщений."""
    text = (
        f"🚫 {name} не сможет участвовать: «{task}» · {service['name']} · "
        f"{event_date:%d.%m.%Y}"
    )
    delivered = 0
    for admin_id in report_recipients(registry, service["id"], admin_actor_id):
        try:
            await bot.send_message(admin_id, text, parse_mode=None)
            delivered += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Не удалось сообщить админу %s об отказе по служению %s: %s",
                admin_id, service["id"], type(exc).__name__,
            )
    return delivered
