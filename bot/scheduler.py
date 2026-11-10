"""Планировщик APScheduler на базе cron-триггеров."""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import date, datetime, time, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger


class ServiceReminderCallback(Protocol):
    def __call__(
        self,
        service_id: str,
        reminder_id: int,
        scheduled_run_date: date | None = None,
    ) -> Awaitable[None]: ...


def next_sunday_date(today: date) -> date:
    days_until_sunday = (6 - today.weekday()) % 7
    if days_until_sunday == 0:
        days_until_sunday = 7
    return today + timedelta(days=days_until_sunday)


def reminder_send_date(event_date: date, reminder_days: int) -> date:
    return event_date - timedelta(days=reminder_days)


def reminder_send_datetime(event_date: date, days: int, clock: str, tz) -> datetime:
    return datetime.combine(
        reminder_send_date(event_date, days), time.fromisoformat(clock), tzinfo=tz
    )


def _period_bound(value) -> date | None:
    if value is None or isinstance(value, date):
        return value
    return date.fromisoformat(value)


def in_reminder_period(event_date: date, reminder: dict) -> bool:
    """Дата события в периоде напоминания «с — по» (этап 4.2); у задачи со
    своим днём — воскресенье её недели."""
    event_date = event_date - timedelta(days=reminder_day_offset(reminder))
    active_from = _period_bound(reminder.get("active_from"))
    active_until = _period_bound(reminder.get("active_until"))
    return (active_from is None or event_date >= active_from) and (
        active_until is None or event_date <= active_until
    )


def periods_overlap(first: dict, second: dict) -> bool:
    """Пересекаются ли периоды двух напоминаний (NULL — открытая граница)."""
    first_from = _period_bound(first.get("active_from"))
    first_until = _period_bound(first.get("active_until"))
    second_from = _period_bound(second.get("active_from"))
    second_until = _period_bound(second.get("active_until"))
    return (
        first_from is None or second_until is None or first_from <= second_until
    ) and (
        second_from is None or first_until is None or second_from <= first_until
    )


def reminder_period_status(reminder: dict, today: date) -> str | None:
    """None — без периода; иначе active / pending / ended. У задачи со
    своим днём границы — дни задачи первой и последней недели."""
    active_from = _period_bound(reminder.get("active_from"))
    active_until = _period_bound(reminder.get("active_until"))
    if active_from is None and active_until is None:
        return None
    shift = timedelta(days=reminder_day_offset(reminder))
    active_from = active_from + shift if active_from else None
    active_until = active_until + shift if active_until else None
    if active_until is not None and active_until < today:
        return "ended"
    if active_from is not None and active_from > today:
        return "pending"
    return "active"


def reminder_day_offset(reminder: dict) -> int:
    """От какого дня недели считается срок: у напоминания задачи со своим
    днём — от дня задачи (спевка — вс −4, среда), иначе от воскресенья."""
    if not reminder.get("task"):
        return 0
    return reminder.get("task_day") or 0


def is_reminder_event_date(event_date: date, reminder: dict) -> bool:
    """Дата — день, от которого считается напоминание (решение 17, 5а):
    общее — только воскресенье, напоминание задачи — день задачи. Иначе
    «вс −0» пришло бы и в среду спевки."""
    offset = reminder_day_offset(reminder)
    return (event_date - timedelta(days=offset)).weekday() == 6


def next_reminder_occurrence(
    now: datetime,
    reminder_days: int,
    reminder_time: str,
    *,
    active_from: date | str | None = None,
    active_until: date | str | None = None,
    day_offset: int = 0,
) -> tuple[date, date] | None:
    """Ближайшее (день события, дата отправки); None — период завершён.
    День события — воскресенье, а у задачи со своим днём — воскресенье
    плюс day_offset. Период «с — по» — по воскресеньям недели."""
    # Первое воскресенье, день события которого — сегодня или позже.
    start = now.date() - timedelta(days=day_offset)
    sunday = start + timedelta(days=(6 - start.weekday()) % 7)
    active_from = _period_bound(active_from)
    active_until = _period_bound(active_until)
    if active_from is not None and sunday < active_from:
        sunday = active_from

    while True:
        if active_until is not None and sunday > active_until:
            return None
        event_date = sunday + timedelta(days=day_offset)
        send_date = reminder_send_date(event_date, reminder_days)
        send_datetime = reminder_send_datetime(
            event_date, reminder_days, reminder_time, now.tzinfo
        )
        if send_datetime >= now:
            return event_date, send_date
        sunday += timedelta(days=7)


def reminder_occurrence(now: datetime, reminder: dict) -> tuple[date, date] | None:
    return next_reminder_occurrence(
        now,
        reminder["reminder_days"],
        reminder["reminder_time"],
        active_from=reminder.get("active_from"),
        active_until=reminder.get("active_until"),
        day_offset=reminder_day_offset(reminder),
    )


def scheduled_reminder_date(now: datetime, reminder_time: str) -> date:
    """Recover yesterday's cron date when execution is delayed past midnight."""
    scheduled_time = time(
        hour=int(reminder_time[:2]), minute=int(reminder_time[3:])
    )
    run_date = now.date()
    if now.timetz().replace(tzinfo=None) < scheduled_time:
        run_date -= timedelta(days=1)
    return run_date


async def add_service_reminder_jobs(
    scheduler: AsyncIOScheduler,
    services: list[dict],
    callback: ServiceReminderCallback,
    tz: ZoneInfo,
    debug_logger: logging.Logger,
    *,
    registry=None,
) -> None:
    now = datetime.now(tz)
    desired: dict[str, tuple[str, int, CronTrigger]] = {}
    for service in services:
        if registry is not None:
            reminders = registry.list_group_reminders(service["id"])
        else:
            reminders = service.get("group_reminders", [])
        for reminder in reminders:
            if not reminder["enabled"]:
                continue
            reminder_id = reminder["id"]
            trigger = CronTrigger(
                hour=int(reminder["reminder_time"][:2]),
                minute=int(reminder["reminder_time"][3:]),
                timezone=tz,
            )
            job_id = f"group-reminder-{service['id']}-{reminder_id}"
            desired[job_id] = (
                service["id"],
                reminder_id,
                trigger,
            )

    jobs = scheduler.get_jobs()
    legacy = [
        job for job in jobs if job.id.startswith("service-reminder-")
    ]
    for job in legacy:
        scheduler.remove_job(job.id)
        debug_logger.info(
            "Удалено устаревшее задание планировщика %s", job.id
        )
    existing = {
        job.id: job
        for job in jobs
        if job.id.startswith("group-reminder-")
    }
    unchanged = set()
    for job_id, (service_id, reminder_id, trigger) in desired.items():
        job = existing.get(job_id)
        if (
            job is not None
            and str(job.trigger) == str(trigger)
            and tuple(job.args) == (service_id, reminder_id)
            and not job.kwargs
        ):
            unchanged.add(job_id)
    for job_id, job in existing.items():
        if job_id in unchanged:
            continue
        next_run_time = getattr(job, "next_run_time", None)
        if next_run_time is not None and next_run_time <= now:
            await callback(
                job.args[0],
                job.args[1],
                next_run_time.astimezone(tz).date(),
            )
        scheduler.remove_job(job_id)
    for job_id, (service_id, reminder_id, trigger) in desired.items():
        if job_id in unchanged:
            continue
        scheduler.add_job(
            callback,
            trigger,
            args=(service_id, reminder_id),
            id=job_id,
            replace_existing=True,
            coalesce=True,
            max_instances=1,
            misfire_grace_time=24 * 60 * 60,
        )
        debug_logger.debug(
            "Зарегистрировано уведомление служения %s (%s): %s",
            service_id,
            reminder_id,
            trigger,
        )


async def refresh_user_reminder_jobs(
    scheduler: AsyncIOScheduler,
    registry,
    callback: Callable[[int], Awaitable[None]],
    tz: ZoneInfo,
    debug_logger: logging.Logger,
    actor_id: int | None = None,
) -> None:
    """Refresh independent per-user schedule jobs without touching group jobs."""
    now = datetime.now(tz)
    records = registry.list_enabled_user_reminder_schedules(actor_id)
    desired = {}
    for record in records:
        schedule_id = record["schedule_id"]
        job_id = (
            f"user-reminder-{record['actor_id']}-schedule-{schedule_id}"
        )
        trigger = CronTrigger(
            hour=int(record["reminder_time"][:2]),
            minute=int(record["reminder_time"][3:]),
            timezone=tz,
        )
        desired[job_id] = (schedule_id, trigger)

    prefix = (
        f"user-reminder-{actor_id}-"
        if actor_id is not None
        else "user-reminder-"
    )
    existing = {
        job.id: job
        for job in scheduler.get_jobs()
        if job.id.startswith(prefix)
    }
    unchanged = set()
    for job_id, (schedule_id, trigger) in desired.items():
        job = existing.get(job_id)
        if (
            job is not None
            and str(job.trigger) == str(trigger)
            and tuple(job.args) == (schedule_id,)
            and not job.kwargs
        ):
            unchanged.add(job_id)
    for job_id in existing.keys() - unchanged:
        scheduler.remove_job(job_id)
    for job_id, (schedule_id, trigger) in desired.items():
        if job_id in unchanged:
            continue
        scheduler.add_job(
            callback,
            trigger,
            args=(schedule_id,),
            id=job_id,
            replace_existing=True,
            coalesce=True,
            max_instances=1,
            misfire_grace_time=24 * 60 * 60,
        )
        debug_logger.debug(
            "Зарегистрировано личное уведомление, расписание %s: %s",
            schedule_id,
            trigger,
        )


def build_scheduler(
    tz: ZoneInfo,
    jobs: list[dict],
    callback: Callable[[], Awaitable[None]],
    debug_logger: logging.Logger,
) -> AsyncIOScheduler:
    sched = AsyncIOScheduler(timezone=tz)
    for job in jobs:
        try:
            trigger = CronTrigger(
                day_of_week=job.get("day_of_week"),
                hour=int(job.get("hour", 0)),
                minute=int(job.get("minute", 0)),
                timezone=tz,
            )
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                f"Некорректная запись расписания {job!r}: {exc}"
            ) from exc

        job_id = job.get("id") or (
            f"job_{job.get('day_of_week')}_{job.get('hour')}_{job.get('minute', 0)}"
        )
        sched.add_job(callback, trigger, id=job_id, replace_existing=True)
        debug_logger.debug("Зарегистрировано задание '%s' → %s", job_id, trigger)
    return sched
