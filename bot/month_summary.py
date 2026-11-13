"""Сводка на месяц (этап 4.1): личное сообщение перед первым воскресеньем.

Пользователь включает сводку в /my_notifications и выбирает, за сколько дней
до первого воскресенья месяца и во сколько её прислать. Отметка доставки —
по (пользователь, месяц), поэтому сводка приходит один раз.
"""
from __future__ import annotations

import calendar
import logging
from dataclasses import dataclass
from collections.abc import Awaitable, Callable, Mapping
from datetime import date, datetime, time, timedelta

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError

from .presentation import personal_schedule_lines
from .service_registry import ReplacementResolutionError

MONTHS = (
    "январь", "февраль", "март", "апрель", "май", "июнь",
    "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь",
)


def first_sunday(year: int, month: int) -> date:
    day = date(year, month, 1)
    return day + timedelta(days=(6 - day.weekday()) % 7)


def month_end(year: int, month: int) -> date:
    return date(year, month, calendar.monthrange(year, month)[1])


def next_month(year: int, month: int) -> tuple[int, int]:
    return (year + 1, 1) if month == 12 else (year, month + 1)


def month_key(year: int, month: int) -> str:
    return f"{year:04d}-{month:02d}"


def month_title(year: int, month: int) -> str:
    return f"{MONTHS[month - 1]} {year}"


def due_month(
    now: datetime, reminder_days: int, reminder_time: str
) -> tuple[int, int] | None:
    """Месяц, сводку которого пора прислать: от времени отправки до конца
    первого воскресенья. После перезапуска бот досылает пропущенное."""
    send_time = time.fromisoformat(reminder_time)
    year, month = now.year, now.month
    for candidate in ((year, month), next_month(year, month)):
        sunday = first_sunday(*candidate)
        send_at = datetime.combine(
            sunday - timedelta(days=reminder_days), send_time, tzinfo=now.tzinfo
        )
        last = datetime.combine(sunday, time.max, tzinfo=now.tzinfo)
        if send_at <= now <= last:
            return candidate
    return None


def month_events(events, year: int, month: int) -> list:
    return [
        event for event in events
        if (event.event_date.year, event.event_date.month) == (year, month)
    ]


def month_summary_text(
    events, linked: set, labels: Mapping[str, str], year: int, month: int
) -> str:
    lines = [f"📆 Ваши служения · {month_title(year, month)}", ""]
    body = personal_schedule_lines(month_events(events, year, month), linked, labels)
    lines.extend(body or ["ℹ️ В этом месяце назначений в графике нет."])
    return "\n".join(lines)


def linked_assignments(registry, telegram_id: int) -> set:
    return {
        (item["service_id"], item["task"], item["name"])
        for item in registry.list_user_assignments(telegram_id)
    }


def service_labels(registry) -> dict[str, str]:
    return {
        service["id"]: service["name"].upper()
        for service in registry.list_services()
    }


@dataclass
class SummaryRun:
    sent: int = 0
    # Кто-то ждёт, пока в графике появятся его служения: график для
    # следующих проверок перечитывается реже (month_summary_events в app).
    waiting: bool = False


async def send_due_month_summaries(
    bot,
    registry,
    load_events: Callable[..., Awaitable[list]],
    now: datetime,
    logger: logging.Logger,
) -> SummaryRun:
    """Рассылает сводки, которым пришло время.

    load_events(since=, until=, fresh=) перечитывает утверждение из БД
    для каждого получателя; исходные события можно кэшировать.
    fresh запрашивает обновление источника с его ограничением частоты.

    Сводка ждёт, пока в графике месяца появятся события всех служений
    человека (лист или колонка ещё не заполнены); в первое воскресенье в
    его время уходит то, что есть. У человека нет назначений — отмечается
    без сообщения."""
    run = SummaryRun()
    due: dict[tuple[int, int], list[dict]] = {}
    for item in registry.list_enabled_month_summaries():
        target = due_month(now, item["reminder_days"], item["reminder_time"])
        if target is None or registry.is_month_summary_delivered(
            item["telegram_id"], month_key(*target)
        ):
            continue
        due.setdefault(target, []).append(item)
    if not due:
        return run

    def last_chance(target: tuple[int, int], item: dict) -> datetime:
        return datetime.combine(
            first_sunday(*target),
            time.fromisoformat(item["reminder_time"]),
            tzinfo=now.tzinfo,
        )

    # В крайний срок график читается свежим, без кэша ожидания: служение
    # могли опубликовать после последней проверки.
    fresh = any(
        now >= last_chance(target, item)
        for target, items in due.items() for item in items
    )
    # С 1-го числа: сводка за текущий месяц включает уже прошедшие дни.
    since = date(*min(due), 1)
    until = max(month_end(*target) for target in due)
    labels = service_labels(registry)
    for (year, month), users in due.items():
        key = month_key(year, month)
        for item in users:
            telegram_id = item["telegram_id"]
            service_ids = set(registry.list_user_summary_services(telegram_id))
            try:
                events = await load_events(
                    since=since, until=until, fresh=fresh, service_ids=service_ids,
                )
            except ReplacementResolutionError:
                logger.warning(
                    "Сводка на месяц %s отложена: неоднозначная замена "
                    "в связанном служении.", key, exc_info=True,
                )
                run.waiting = True
                continue
            linked = linked_assignments(registry, telegram_id)
            linked_services = {service_id for service_id, _, _ in linked}
            events_of_month = month_events(events, year, month)
            published = {event.service_id for event in events_of_month}
            if linked_services - published and (
                now < last_chance((year, month), item)
            ):
                run.waiting = True
                continue
            has_tasks = bool(personal_schedule_lines(events_of_month, linked, labels))
            if not registry.claim_month_summary(telegram_id, key) or not has_tasks:
                continue
            text = month_summary_text(events, linked, labels, year, month)
            try:
                await bot.send_message(telegram_id, text, parse_mode=None)
            except (TelegramForbiddenError, TelegramBadRequest) as exc:
                # Бот заблокирован или чат недоступен — повтор не поможет,
                # отметка остаётся.
                logger.warning(
                    "Сводка на месяц %s пользователю %s не доставлена: %s",
                    key, telegram_id, exc,
                )
                continue
            except Exception:  # noqa: BLE001
                registry.release_month_summary(telegram_id, key)
                logger.warning(
                    "Не удалось отправить сводку на месяц %s пользователю %s",
                    key, telegram_id, exc_info=True,
                )
                continue
            run.sent += 1
    return run
