"""Предложенный график по итогам опроса готовности (этап 5.4–5.5).

По сроку опроса бот распределяет отметившихся (`readiness.summarize`)
с учётом прошлого месяца и сохраняет предложение в `schedule_plans`.
Админ служения правит его («✏️ Изменить»), пересчитывает и утверждает:
график публикуется в чат служения, админу — блок для переноса в Excel.

Слоты графика: дата (ISO) → задача → ключи людей (`readiness_people`).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import Callable, Sequence
from datetime import date, timedelta
from io import BytesIO

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import ReplyParameters

from .excel_parser import ServiceAssignment, ServiceEvent, _schedule_sheet_name
from .group_send import send_to_service_group
from . import task_rules
from .month_summary import month_end, month_key, month_title
from .presentation import (
    apply_replacements, display_name, format_task_assignments,
    task_assignment_names, task_emoji,
)
from .readiness import (
    DEFAULT_PLAN_TITLE,
    DEFAULT_PLAN_UPDATE_TITLE,
    Summary,
    month_sundays,
    local_text,
    month_of,
    option_key,
    outside_name,
    phrase,
    render_template,
    summarize,
    task_lines,
)
from .service_registry import plan_target_thread
from .services import (
    ScheduleColumn, normalized_name, split_day_key, task_day_key,
    sort_tasks_for_display,
)
from .ui import (
    plain_text, render_screen_chunks, send_telegram_screen, user_link,
)


# Сколько ждать график прошлого месяца (секунды).
LOADER_TIMEOUT = 30


class PublishedNotSaved(RuntimeError):
    """График ушёл в чат, но отметка об утверждении не сохранилась."""


def revision(plan: dict) -> str:
    """Версия графика для кнопок: правка или пересчёт меняют её, и
    кнопки с прежней версией считаются устаревшими."""
    data = json.dumps(
        [plan["updated_at"], plan["run_id"], plan["slots"],
         plan.get("days", {}), plan.get("tasks", [])],
        ensure_ascii=False, sort_keys=True,
    )
    return hashlib.sha1(data.encode()).hexdigest()[:6]


def previous_month(year: int, month: int) -> tuple[int, int]:
    return (year - 1, 12) if month == 1 else (year, month - 1)


def _short(day: str) -> str:
    return date.fromisoformat(day).strftime("%d.%m")


def served_dates(slots: dict) -> dict[str, list[str]]:
    """Ключ человека → даты, когда он стоит в графике (связка задач «в
    одну неделю» в дату — одна дата)."""
    result: dict[str, set[str]] = {}
    for day, tasks in slots.items():
        for people in tasks.values():
            for person in people:
                result.setdefault(person, set()).add(day)
    return {person: sorted(days) for person, days in result.items()}


async def last_served(
    registry, service_id: str, year: int, month: int, people: list[dict],
    events_loader, logger: logging.Logger,
) -> tuple[dict[str, str], str | None]:
    """Последнее служение каждого в месяце перед (year, month): по
    утверждённому графику бота, иначе по Excel. Второе — пометка, если
    прошлый месяц учесть не удалось."""
    py, pm = previous_month(year, month)
    previous = registry.get_schedule_plan(service_id, month_key(py, pm))
    if previous is not None and previous["approved"] is not None:
        # Фактический день служения — как был при публикации (разовый или
        # свой день), иначе воскресенье графика.
        served = previous.get("served_offsets") or {}
        snapshot = bool(previous.get("served_snapshot") or served)
        by_key = {person["key"]: person for person in people}
        approved_days = previous.get("approved_days") or {}

        def offset_of(person: str, day: str) -> int:
            if snapshot:
                return served.get(day, {}).get(person) or 0
            # Утверждён до снимка: разовый день на дату, иначе свой день.
            offset = approved_days.get(day, {}).get(person)
            if offset is None:
                offset = (by_key.get(person) or {}).get("day_offset")
            return offset or 0

        result = {}
        for person, days in served_dates(previous["approved"]).items():
            result[person] = max(
                date.fromisoformat(day) + timedelta(days=offset_of(person, day))
                for day in days
            ).isoformat()
        return result, None
    if events_loader is None:
        return {}, "прошлый месяц не учтён — график не загружен"
    first, last = date(py, pm, 1), month_end(py, pm)
    try:
        # Под service_lock: зависшая загрузка не должна держать служение.
        events = await asyncio.wait_for(
            events_loader(since=first, until=last, excel_only=True), LOADER_TIMEOUT
        )
    except Exception:  # noqa: BLE001
        logger.warning(
            "График прошлого месяца не загружен для интервалов", exc_info=True
        )
        return {}, "прошлый месяц не учтён — график не загрузился"
    # Имя в Excel → ключ; одинаковые имена у разных людей — неоднозначно.
    by_name: dict[str, list[str]] = {}
    for person in people:
        by_name.setdefault(normalized_name(person["name"]), []).append(
            person["key"]
        )
    result: dict[str, str] = {}
    for event in events:
        if event.service_id != service_id or not first <= event.event_date <= last:
            continue
        for assignment in event.assignments:
            if assignment.is_informational or not assignment.name:
                continue
            keys = by_name.get(normalized_name(assignment.name), [])
            if len(keys) != 1:
                continue
            day = event.event_date.isoformat()
            result[keys[0]] = max(result.get(keys[0], day), day)
    return result, None


def plan_tasks(registry, service_id: str) -> list[dict]:
    """Распределяемые задачи в порядке каталога: минимум и максимум
    (служение без задач — одна задача с его названием)."""
    return [
        {
            "name": task["name"], "needed": task["needed"], "max": task["max"],
            "implicit": task["implicit"], "day": task.get("day"),
            **({"requires_any": task["requires_any"]}
               if task.get("requires_any") else {}),
        }
        for task in registry.plan_tasks(service_id)
    ]


def implicit_plan(plan: dict) -> bool:
    """График служения без задач: одна задача с названием служения —
    в тексте и правке люди сразу под датой."""
    return len(plan["tasks"]) == 1 and bool(plan["tasks"][0].get("implicit"))


def display_tasks(plan: dict) -> list[dict]:
    """Порядок строк; исходный список задач и индексы редактора неизменны."""
    return sort_tasks_for_display(
        plan["tasks"], plan["service_id"], name=lambda task: task["name"],
    )


def free_run(service_id: str, year: int, month: int) -> dict:
    """Опрос-заглушка графика без опроса: все воскресенья месяца."""
    return {
        "id": None, "service_id": service_id,
        "month": month_key(year, month), "status": "free",
        "dates": [day.isoformat() for day in month_sundays(year, month)],
    }


def plan_run(plan: dict, run: dict | None) -> dict:
    """Опрос, по которому составлен график (без опроса — заглушка)."""
    if plan["run_id"] is None:
        year, month = map(int, plan["month"].split("-"))
        return {**free_run(plan["service_id"], year, month),
                "dates": list(plan["dates"])}
    return run


def plan_summary(registry, plan: dict, run: dict | None) -> Summary:
    """Состав и ответы для экранов графика; без опроса — все свободны."""
    return summarize(
        registry, plan_run(plan, run), with_plan=False,
        assume_free=plan["run_id"] is None,
    )


async def build_draft(
    registry, run: dict, events_loader, logger: logging.Logger,
    *, save: bool = True,
) -> dict:
    """Предложенный график по ответам опроса (run["id"] is None — без
    опроса, все свободны); ручные правки сбрасываются. save=False —
    предварительный, пока опрос идёт: по текущим ответам, в базу не
    пишется (утверждать его нельзя)."""
    year, month = month_of(run)
    people = registry.readiness_people(run["service_id"])
    served, note = await last_served(
        registry, run["service_id"], year, month, people, events_loader,
        logger,
    )
    summary = summarize(
        registry, run,
        last_served={key: date.fromisoformat(day) for key, day in served.items()},
        assume_free=run["id"] is None,
    )
    tasks = plan_tasks(registry, run["service_id"])
    default_day = registry.get_readiness_settings(
        run["service_id"]
    )["day_from"]
    if default_day:
        tasks = [{**task, "default_day": default_day} for task in tasks]
    slots = {
        day: {
            task["name"]: list(
                summary.plan.slots.get((date.fromisoformat(day), task["name"]), [])
            )
            for task in tasks
        }
        for day in run["dates"]
    }
    if not save:
        return {
            "service_id": run["service_id"], "month": month_key(year, month),
            "run_id": run["id"], "source": "poll",
            "dates": list(run["dates"]), "tasks": tasks, "slots": slots,
            "last_served": served, "note": note, "edited": 0,
            "approved": None, "approved_at": None, "approved_days": {},
            "approved_tasks": None, "days": {}, "republish": 0,
            "dirty": True, "message_ids": [], "baseline": None,
            "chat_id": None, "message_id": None, "updated_at": None,
            "served_offsets": {}, "served_snapshot": False,
        }
    return registry.save_schedule_draft(
        run["service_id"], month_key(year, month),
        run_id=run["id"], dates=list(run["dates"]), tasks=tasks, slots=slots,
        last_served=served, note=note,
    )


async def ensure_plan(
    registry, run: dict, events_loader, logger: logging.Logger,
) -> dict:
    """График закрытого опроса: сохранённый или (нет, опрос перезапущен)
    — новый предложенный."""
    plan = registry.get_schedule_plan(run["service_id"], month_key(*month_of(run)))
    if plan is not None and plan["run_id"] == run["id"]:
        return sync_implicit_task(registry, plan)
    return await build_draft(registry, run, events_loader, logger)


def sync_implicit_task(registry, plan: dict) -> dict:
    """Служение без задач переименовано в каталоге — задача графика
    (его название) переименовывается вслед, правки сохраняются."""
    current = registry.plan_tasks(plan["service_id"])
    if (
        len(current) == 1 and current[0]["implicit"]
        and len(plan["tasks"]) == 1
        and (
            plan["tasks"][0]["name"] != current[0]["name"]
            or not plan["tasks"][0].get("implicit")
        )
    ):
        return registry.rename_plan_task(
            plan["service_id"], plan["month"], plan["tasks"][0]["name"],
            current[0]["name"],
        )
    return plan


def _names(people: list[dict]) -> dict[str, str]:
    return {person["key"]: person["name"] for person in people}


def covered(plan: dict) -> tuple[int, int]:
    """(закрыто обязательных мест, всего обязательных)."""
    done = total = 0
    for tasks in plan["slots"].values():
        for task in plan["tasks"]:
            people = tasks.get(task["name"], [])
            done += min(len(people), task["needed"])
            total += task["needed"]
    return done, total


def gaps(plan: dict) -> list[str]:
    lines = []
    for day in plan["dates"]:
        for task in display_tasks(plan):
            count = len(plan["slots"].get(day, {}).get(task["name"], []))
            if count < task["needed"]:
                lines.append(
                    f"⚠️ {_short(day)} «{task['name']}»: "
                    + ("никого" if not count else f"{count} из {task['needed']}")
                )
    return lines


WEEKDAYS = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")


def _weekday_date(day: date) -> str:
    return f"{WEEKDAYS[day.weekday()]} {day:%d.%m}"


def day_label(day: str, window: tuple[int, int] = (0, 0)) -> str:
    """Дата графика; с окном дней служения — «01.11 (сб 31.10 – пн 02.11)»."""
    sunday = date.fromisoformat(day)
    start, end = window
    if (start, end) == (0, 0):
        return _short(day)
    return (
        f"{_short(day)} ({_weekday_date(sunday + timedelta(days=start))} – "
        f"{_weekday_date(sunday + timedelta(days=end))})"
    )


def day_offset(plan: dict | None, person: dict | None, day: str) -> int | None:
    """День человека на дату: разовый в графике, иначе личный."""
    key = (person or {}).get("key")
    override = (plan or {}).get("days", {}).get(day, {}).get(key)
    if override is not None:
        return override
    return (person or {}).get("day_offset")


def task_days(plan: dict) -> dict[str, int]:
    """Свои дни задач графика (спевка — вс −4)."""
    return {
        task["name"]: task["day"] for task in plan["tasks"]
        if task.get("day") is not None
    }


def slot_offset(plan: dict, person: dict | None, day: str,
                task: str) -> int | None:
    """День человека в задаче на дату: разовый для задачи, разовый в
    графике, иначе день задачи, иначе личный."""
    key = (person or {}).get("key")
    overrides = plan.get("days", {}).get(day, {})
    override = overrides.get(task_day_key(str(key), task))
    if override is None:
        override = overrides.get(key)
    if override is not None:
        return override
    own = task_days(plan).get(task)
    if own is not None:
        return own
    personal = (person or {}).get("day_offset")
    if personal is not None:
        return personal
    return next(
        (
            item.get("default_day") for item in plan["tasks"]
            if item["name"] == task
        ),
        None,
    )


def _selected_slot(summary: Summary, key: str, day: str, task: str,
                   service_day: date) -> bool:
    available = summary.available.get(key, ())
    return (
        option_key(service_day.isoformat(), task) in available
        or service_day.isoformat() in available
        or day in available
    )


def served_offsets(plan: dict, people: list[dict]) -> dict:
    """Фактический сдвиг дня служения каждого поставленного (разовый, день
    задачи или свой день) — снимок при публикации. У человека в нескольких
    задачах даты — самый поздний день."""
    by_key = {person["key"]: person for person in people}
    latest: dict[tuple[str, str], int] = {}
    for day, tasks in plan["slots"].items():
        for task, keys in tasks.items():
            for key in keys:
                offset = slot_offset(
                    plan, by_key.get(key) or {"key": key}, day, task
                ) or 0
                latest[day, key] = max(offset, latest.get((day, key), offset))
    result: dict = {}
    for (day, key), offset in latest.items():
        if offset:
            result.setdefault(day, {})[key] = offset
    return result


def approved_slot_days(plan: dict, people: list[dict]) -> dict:
    """Resolved assignment offsets, frozen with the published schedule."""
    by_key = {person["key"]: person for person in people}
    return {
        day: {
            task: {
                key: slot_offset(
                    plan, by_key.get(key) or {"key": key}, day, task
                ) or 0
                for key in keys
            }
            for task, keys in tasks.items()
        }
        for day, tasks in plan["slots"].items()
    }


def served_days(plan: dict, people: list[dict]) -> dict[str, set[date]]:
    """Фактические дни задач, с учётом дня задачи и личных переносов."""
    by_key = {person["key"]: person for person in people}
    result: dict[str, set[date]] = {}
    for day, tasks in plan["slots"].items():
        sunday = date.fromisoformat(day)
        for task, keys in tasks.items():
            for key in keys:
                offset = slot_offset(
                    plan, by_key.get(key) or {"key": key}, day, task
                ) or 0
                result.setdefault(key, set()).add(
                    sunday + timedelta(days=offset)
                )
    return result


def workload_lines(
    load: dict[str, set[date]], people: list[dict], previous: dict[str, str],
) -> list[str]:
    by_key = {person["key"]: person for person in people}
    for key in load:
        by_key.setdefault(key, {"key": key, "name": key, "telegram_id": None})
    ordered = sorted(
        by_key.values(),
        key=lambda person: (-len(load.get(person["key"], ())), person["name"]),
    )
    lines = ["", "👥 Нагрузка (дней служения в месяце):"]
    names = [display_name(person) for person in ordered]
    for person in ordered:
        key = person["key"]
        label = display_name(person)
        if names.count(label) > 1 and person.get("username"):
            label += f" (@{person['username'].lstrip('@')})"
        days = sorted(load.get(key, ()))
        last = date.fromisoformat(previous[key]) if previous.get(key) else None
        intervals = []
        for day in days:
            intervals.append(
                f"{(day - last).days / 7:.1f}".rstrip("0").rstrip(".")
                if last is not None and last < day
                else "нет данных"
            )
            last = day
        lines.append(
            f"   {len(days)} — {label}"
            + ("" if person["telegram_id"] else " (без Telegram)")
            + (" · интервалы, нед.: " + ", ".join(intervals) if days else "")
        )
    lines.append(
        "Интервалы: от прошлого известного служения к первому назначению, "
        "затем между назначениями по порядку. «Нет данных» — история неизвестна."
    )
    return lines


def week_groups(plan: dict, day: str, window: tuple[int, int]
                ) -> tuple[str, list[tuple[str, int | None, list[dict]]]]:
    """График по неделям (у задач свои дни): «Неделя 26.10–01.11» и дни
    недели по порядку, у каждого — его задачи. Воскресенье — правая граница
    недели; задачи без своего дня — в день (или окно) служения. Элемент:
    подпись дня, сдвиг дня (None — окно служения), задачи."""
    sunday = date.fromisoformat(day)
    own = task_days(plan)
    groups: dict[int | None, list[dict]] = {}
    base = None if window != (0, 0) else 0
    for task in display_tasks(plan):
        groups.setdefault(
            own.get(task["name"], task.get("default_day", base)),
            [],
        ).append(task)
    offsets = [offset for offset in groups if offset is not None]
    last = max([0, window[1] if None in groups else 0, *offsets])
    title = (
        f"Неделя {sunday - timedelta(days=6):%d.%m}–"
        f"{sunday + timedelta(days=last):%d.%m}"
    )
    result = []
    # Окно служения — после задач со своим днём в тот же день.
    for offset in sorted(
        groups,
        key=lambda value: (window[0], 1) if value is None else (value, 0),
    ):
        if offset is None:
            label = (
                f"{_weekday_date(sunday + timedelta(days=window[0]))} – "
                f"{_weekday_date(sunday + timedelta(days=window[1]))}"
            )
        else:
            label = _weekday_date(sunday + timedelta(days=offset))
        result.append((label, offset, groups[offset]))
    return title, result


def week_events(events: list, own_days: dict[str, int]) -> list[dict]:
    """События графика Excel по воскресеньям: задача со своим днём (спевка
    — вс −4, в файле — отдельной средой) — в блоке своего воскресенья.
    Элемент: дата (воскресенье), назначения и фактический день задач,
    отличный от даты."""
    blocks: dict[date, dict] = {}
    for event in sorted(events, key=lambda item: item.event_date):
        if not event.assignments:
            # Дата без назначений — тоже в обзоре.
            blocks.setdefault(
                event.event_date,
                {"date": event.event_date, "assignments": [], "days": {},
                 "moments": []},
            )
        for assignment in event.assignments:
            offset = own_days.get(assignment.task)
            sunday = event.event_date
            if offset is not None and event.event_date.weekday() != 6:
                candidate = event.event_date - timedelta(days=offset)
                if candidate.weekday() == 6:
                    sunday = candidate
            block = blocks.setdefault(
                sunday,
                {"date": sunday, "assignments": [], "days": {}, "moments": []},
            )
            block["assignments"].append(assignment)
            # Дата каждого назначения — как в файле.
            block["moments"].append(event.event_date)
            if event.event_date != sunday:
                block["days"][assignment.task] = event.event_date
    return [blocks[day] for day in sorted(blocks)]


def week_person_day(plan: dict, person: dict | None, day: str, task: str,
                    group_offset: int | None) -> str:
    """День человека в графике по неделям — в скобках, только если он не
    совпадает с днём строки."""
    offset = slot_offset(plan, person, day, task)
    if offset is None or offset == group_offset:
        return ""
    return (
        " (" + _weekday_date(date.fromisoformat(day) + timedelta(days=offset))
        + ")"
    )


def person_day(person: dict | None, day: str, plan: dict | None = None) -> str:
    """« (чт 29.10)» для человека с личным (или разовым) днём служения —
    в скобках, как окно у даты."""
    offset = day_offset(plan, person, day)
    if offset is None:
        return ""
    return (
        " (" + _weekday_date(date.fromisoformat(day) + timedelta(days=offset))
        + ")"
    )


def date_header(plan: dict, by_key: dict, day: str,
                window: tuple[int, int]) -> tuple[str, bool]:
    """Заголовок даты и скрывать ли дни у людей. У всех поставленных один
    и тот же свой день — он в скобках у даты («29.11 (чт 26.11)»), у имён
    не повторяется; у всех свои, но разные — дата без периода; иначе —
    дата с окном служения."""
    people = [
        key for keys in plan["slots"].get(day, {}).values() for key in keys
    ]
    offsets = {day_offset(plan, by_key.get(key), day) for key in people}
    if people and None not in offsets and len(offsets) == 1:
        (offset,) = offsets
        moment = date.fromisoformat(day) + timedelta(days=offset)
        return f"{_short(day)} ({_weekday_date(moment)})", True
    if people and None not in offsets:
        return _short(day), False
    return day_label(day, window), False


def window_of(registry, service_id: str) -> tuple[int, int]:
    settings = registry.get_readiness_settings(service_id)
    return settings["day_from"], settings["day_to"]


def plan_text(service_label: str, plan: dict, summary: Summary, tz,
              window: tuple[int, int] = (0, 0)) -> str:
    """window — окно дней служения от воскресенья (Уборка: −1, +1)."""
    year, month = map(int, plan["month"].split("-"))
    names = {person["key"]: display_name(person) for person in summary.people}
    by_key = {person["key"]: person for person in summary.people}
    silent = {person["key"] for person in summary.not_answered}
    polled = [person for person in summary.people if person["tasks"]]
    used_unpicked = False

    def mark(person: str, day: str, task: str) -> str:
        """* — не ответил; ° — ответил, но эту дату не отмечал (решение 16:
        его даты заняли другие — вместо не ответившего; или вручную)."""
        nonlocal used_unpicked
        if person in silent:
            return "*"
        service_day = date.fromisoformat(day) + timedelta(
            days=slot_offset(plan, by_key.get(person), day, task) or 0
        )
        selected = _selected_slot(summary, person, day, task, service_day)
        if plan["run_id"] is not None and person in summary.answered and not selected:
            used_unpicked = True
            return "°"
        return ""

    done, total = covered(plan)
    if plan["approved"] is None:
        status = "📝 Черновик — ещё не утверждён"
    elif plan.get("moved") and not plan.get("republish") and (
        plan["approved"] == plan["slots"]
    ):
        status = (
            "📍 Сменился чат или тема служения — «✅ Утвердить изменения» "
            "опубликует график там"
        )
    elif plan["dirty"]:
        status = (
            "✏️ Есть правки после утверждения — «✅ Утвердить» опубликует "
            "обновление"
        )
    else:
        status = f"✅ Утверждён {local_text(plan['approved_at'], tz)}"
    source_label = "Авто" if plan["run_id"] is None else "Опрос"
    lines = [
        f"📋 {service_label} › График › {source_label} › {month_title(year, month)}",
        status,
        "",
        (
            "📥 Загружен из Excel — напоминания и замены по нему"
            if plan.get("source") == "excel"
            else "🧮 Без опроса: все из состава считаются свободными"
            if plan["run_id"] is None
            else f"Ответили {len(summary.answered)} из {len(polled)}"
        ) + f" · закрыто мест {done} из {total}",
    ]
    used_silent = False
    weekly = bool(task_days(plan)) or any(
        task.get("default_day") is not None for task in plan["tasks"]
    )
    for day in plan["dates"]:
        if weekly:
            title, groups = week_groups(plan, day, window)
            lines += ["", f"🗓 {title}"]
            for label, offset, tasks in groups:
                lines.append(f"   {label}")
                for task in tasks:
                    people = plan["slots"].get(day, {}).get(task["name"], [])
                    used_silent |= any(person in silent for person in people)
                    lines += [
                        "  " + line for line in task_lines(
                            task["name"], task["needed"],
                            [
                                names.get(person, person)
                                + mark(person, day, task["name"])
                                + week_person_day(
                                    plan, by_key.get(person), day,
                                    task["name"], offset,
                                )
                                for person in people
                            ],
                        )
                    ]
            continue
        header, shared_day = date_header(plan, by_key, day, window)
        lines += ["", f"🗓 {header}"]
        for task in display_tasks(plan):
            people = plan["slots"].get(day, {}).get(task["name"], [])
            used_silent |= any(person in silent for person in people)
            lines += task_lines(
                task["name"], task["needed"],
                [
                    names.get(person, person) + mark(person, day, task["name"])
                    + ("" if shared_day else person_day(
                        by_key.get(person), day, plan
                    ))
                    for person in people
                ],
                bare=implicit_plan(plan),
            )
    # Нагрузка — фактические дни служения, а не недели графика.
    load = served_days(plan, polled)
    lines += workload_lines(load, polled, plan.get("last_served") or {})
    problems = gaps(plan)
    if problems:
        lines += [""] + problems
    if plan.get("approval_warnings") and plan["approved"] is not None:
        lines += ["", "⚠️ При утверждении приняты предупреждения:"] + plan["approval_warnings"]
    # Задача без людей во все даты, хотя её выполняют люди состава, —
    # обычно её включили после составления черновика.
    able = {task for person in summary.people for task in person["tasks"]}
    idle = [
        task["name"] for task in display_tasks(plan)
        if task["name"] in able and not any(
            plan["slots"].get(day, {}).get(task["name"]) for day in plan["dates"]
        )
    ]
    if idle and plan["approved"] is None and not implicit_plan(plan):
        lines.append(
            "ℹ️ Никого во все даты: " + ", ".join(f"«{name}»" for name in idle)
            + ". Если задачу включили после составления — «🔄 Пересчитать»."
        )
    if used_silent:
        lines.append(
            "* не ответил(а) — поставлен(а) по общей логике, после ответивших"
        )
    if used_unpicked:
        lines.append(
            "° не отмечал(а) эту дату в опросе — отмеченные заняли другие "
            "(поставлен(а) вместо не ответившего) или поставлен(а) вручную"
        )
    for answer in summary.outside:
        days = ", ".join(
            date.fromisoformat(day).strftime("%d") for day in answer["dates"]
        ) or "не сможет"
        lines.append(
            f"⚪ Вне состава отметились: {outside_name(answer)} ({days}) — "
            "нет задач"
        )
    if plan.get("note"):
        lines.append(f"ℹ️ Интервалы: {plan['note']}")
    return "\n".join(lines)


def slot_people(plan: dict, day: str, task: str) -> list[str]:
    return list(plan["slots"].get(day, {}).get(task, []))


def busy_on(plan: dict, day: str, task: str,
            bundles: dict | None = None) -> dict[str, str]:
    """Кто уже стоит в этот день на другой задаче: ключ → задача. Задача
    из его связки «в одну неделю» с этой — не занятость."""
    return {
        person: other
        for other, people in plan["slots"].get(day, {}).items()
        if other != task
        for person in people
        if not {task, other} <= (bundles or {}).get(person, frozenset())
        and not task_rules.compatible(plan["tasks"], task, other)
    }


def candidates(
    plan: dict, summary: Summary, day: str, task: str,
) -> tuple[list[str], list[str], list[str]]:
    """Кого можно поставить: (свободны и приписаны к задаче, не отвечали,
    остальные из состава). Уже стоящие в этот день — не предлагаются."""
    taken = set(slot_people(plan, day, task)) | set(
        busy_on(plan, day, task, summary.bundles)
    )
    silent = {person["key"] for person in summary.not_answered}
    free, quiet, rest = [], [], []
    for person in summary.people:
        key = person["key"]
        if key in taken or not person["tasks"]:
            continue
        anchors = task_rules.dependent_tasks(plan["tasks"]).get(task)
        if anchors and task not in person["tasks"]:
            continue
        if key in silent:
            (quiet if task in person["tasks"] else rest).append(key)
        else:
            service_day = date.fromisoformat(day) + timedelta(
                days=slot_offset(plan, person, day, task) or 0
            )
            if task in person["tasks"] and _selected_slot(
                summary, key, day, task, service_day
            ):
                free.append(key)
            else:
                rest.append(key)
    return free, quiet, rest


def choice_notes(plan: dict, summary: Summary, key: str, day: str,
                 task: str) -> list[str]:
    """Предупреждения, когда админ ставит человека вручную."""
    person = next(
        (item for item in summary.people if item["key"] == key), None
    )
    notes = []
    silent = {item["key"] for item in summary.not_answered}
    event_date = date.fromisoformat(day)
    service_day = event_date + timedelta(
        days=slot_offset(plan, person, day, task) or 0
    )
    if key in silent:
        notes.append("⚪ не отвечал(а) на опрос")
    elif not _selected_slot(summary, key, day, task, service_day):
        notes.append(f"⚠️ не отметил(а) {service_day:%d.%m}")
    if person is not None and task not in person["tasks"]:
        notes.append(f"⚠️ не приписан(а) к «{task}»")
    anchors = task_rules.dependent_tasks(plan["tasks"]).get(task)
    if anchors and not any(
        key in slot_people(plan, day, anchor) for anchor in anchors
    ):
        notes.append("⚠️ ответственный не входит в смену; "
                     "при утверждении потребуется подтверждение")
    near = [
        other for other in sorted(served_days(plan, summary.people).get(key, ()))
        if other != service_day
        and abs((other - service_day).days) <= 7
    ]
    previous = plan["last_served"].get(key)
    if previous and (service_day - date.fromisoformat(previous)).days <= 7:
        near.append(date.fromisoformat(previous))
    for other in sorted(near):
        verb = "служил(а)" if other < service_day else "служит"
        notes.append(f"ℹ️ {verb} {other:%d.%m}")
    return notes


def plan_changes(old: dict, new: dict, names: dict[str, str],
                 nobody: str = "никого",
                 show: Callable[[str], str] | None = None,
                 *, service_id: str | None = None) -> list[str]:
    """Отличия нового графика от опубликованного — для «обновлён»: кто
    кого заменил («• 08.11 Денис → Филипп»; при нескольких задачах — с
    задачей), кто добавлен («никого → Анна») и кто убран («Анна → никого»).
    Имена без склонения. show — как показать
    человека (упоминание), по умолчанию имя.
    Единственная задача сменила название (служение без задач переименовано
    в каталоге) — одной строкой, люди сравниваются под новым названием.
    При нескольких задачах смена набора — это выключение и включение, не
    переименование."""
    lines = []
    old_tasks = {task for tasks in old.values() for task in tasks}
    new_tasks = {task for tasks in new.values() for task in tasks}
    several = len(old_tasks | new_tasks) > 1
    if show is None:
        def show(key: str) -> str:
            return names.get(key, key)
    if len(old_tasks) == len(new_tasks) == 1 and old_tasks != new_tasks:
        (was,), (now,) = old_tasks, new_tasks
        several = False
        lines.append(f"• Задача «{was}» теперь называется «{now}»")
        old = {
            day: {(now if task == was else task): people
                  for task, people in tasks.items()}
            for day, tasks in old.items()
        }
    for day in sorted(set(old) | set(new)):
        tasks = list(dict.fromkeys([*new.get(day, {}), *old.get(day, {})]))
        for task in sort_tasks_for_display(tasks, service_id):
            before = old.get(day, {}).get(task, [])
            after = new.get(day, {}).get(task, [])
            gone = [key for key in before if key not in after]
            came = [key for key in after if key not in before]
            where = (
                f"• {_short(day)} «{task}»: " if several
                else f"• {_short(day)} "
            )
            lines += [
                f"{where}{show(old_key)} → {show(new_key)}"
                for new_key, old_key in zip(came, gone)
            ]
            lines += [
                f"{where}{nobody} → {show(key)}" for key in came[len(gone):]
            ]
            lines += [
                f"{where}{show(key)} → {nobody}" for key in gone[len(came):]
            ]
    return lines


def publication_title(service: dict, month: str, update: bool,
                      settings: dict | None = None) -> str:
    """Заголовок графика в чате служения: при первой публикации или при
    обновлении (шаблоны «📝 Тексты»)."""
    year, month_number = map(int, month.split("-"))
    template = (settings or {}).get(
        "plan_update_template" if update else "plan_title_template"
    ) or (DEFAULT_PLAN_UPDATE_TITLE if update else DEFAULT_PLAN_TITLE)
    return render_template(
        template,
        service_name=service["name"],
        month=month_title(year, month_number),
    )


def published_baseline(plan: dict) -> dict:
    """График первой публикации месяца — от него изменения под закреплённым
    графиком (решение 17, 5б). Опубликован до 5б — прошлая утверждённая
    копия."""
    return plan.get("baseline") or {
        "slots": plan.get("approved") or {},
        "days": plan.get("approved_days") or {},
    }


def change_lines(plan: dict, people: list[dict], old_slots: dict,
                 old_days: dict, settings: dict | None = None,
                 show: Callable[[str], str] | None = None,
                 days_changed: bool | None = None) -> list[str]:
    """Изменения рабочей копии от old_slots/old_days: замены, разовые дни,
    «Изменены дни служения» (days_changed; None — после прошлого
    утверждения)."""
    names = _names(people)
    changes = plan_changes(
        old_slots, plan["slots"], names, phrase(settings, "nobody"), show,
        service_id=plan["service_id"],
    )
    for day in plan["dates"]:
        before = old_days.get(day, {})
        after = plan.get("days", {}).get(day, {})
        serving = {
            key for keys in plan["slots"].get(day, {}).values()
            for key in keys
        }
        for key in sorted(set(before) | set(after)):
            person, task = split_day_key(key)
            if before.get(key) == after.get(key) or person not in serving:
                continue
            offset = after.get(key)
            changes.append(
                f"• {_short(day)} "
                + (show(person) if show else names.get(person, person))
                + (f" ({task})" if task else "") + ": "
                + (
                    _weekday_date(
                        date.fromisoformat(day) + timedelta(days=offset)
                    ) if offset is not None
                    else phrase(settings, "usual_day")
                )
            )
    if plan.get("republish") if days_changed is None else days_changed:
        changes.append(phrase(settings, "days_changed"))
    return changes


def _people_lines(head: str, keys: list[str], indent: str, empty: str,
                  show: Callable[[str], str]) -> list[str]:
    """Задача и под ней каждый человек на своей строке (решение 17, 5б)."""
    if not keys:
        return [f"{head}: {empty}"]
    return [head] + [f"{indent}• {show(key)}" for key in keys]


def publication_text(service: dict, plan: dict, people: list[dict],
                     window: tuple[int, int] = (0, 0),
                     settings: dict | None = None) -> str:
    """Утверждённый график для чата служения (с упоминаниями): дата, под
    ней задачи, под задачей — каждый человек на своей строке. При
    обновлении под графиком — все изменения с первой публикации."""
    by_key = {person["key"]: person for person in people}
    update = plan["approved"] is not None
    empty = phrase(settings, "empty")

    def link(key: str) -> str:
        person = by_key.get(key)
        if person is None:
            return key
        return user_link(person["telegram_id"], display_name(person))

    lines = [
        publication_title(service, plan["month"], update, settings),
    ]
    weekly = bool(task_days(plan)) or any(
        task.get("default_day") is not None for task in plan["tasks"]
    )
    for day in plan["dates"]:
        if weekly:
            # У задач свои дни (спевка — ср): неделя, под ней дни и задачи.
            title, groups = week_groups(plan, day, window)
            lines += ["", f"🗓 {title}"]
            for label, offset, tasks in groups:
                lines.append(f"  {label}")
                for task in tasks:
                    name = task["name"]
                    lines += _people_lines(
                        f"    {task_emoji(name)} {name}",
                        plan["slots"].get(day, {}).get(name, []),
                        "      ", empty,
                        lambda key, name=name, offset=offset: link(key) + (
                            week_person_day(
                                plan, by_key[key], day, name, offset
                            ) if key in by_key else ""
                        ),
                    )
            continue
        label, shared_day = date_header(plan, by_key, day, window)
        lines += ["", f"🗓 {label}"]

        def mention(key: str) -> str:
            person = by_key.get(key)
            return link(key) + (
                "" if shared_day or person is None
                else person_day(person, day, plan)
            )

        for task in display_tasks(plan):
            keys = plan["slots"].get(day, {}).get(task["name"], [])
            if implicit_plan(plan):
                lines += (
                    [f"  • {mention(key)}" for key in keys] or [f"  {empty}"]
                )
                continue
            lines += _people_lines(
                f"  {task_emoji(task['name'])} {task['name']}", keys,
                "    ", empty, mention,
            )
    if plan.get("approval_warnings"):
        lines += ["", "⚠️ Утверждено с предупреждениями:"] + plan["approval_warnings"]
    if update:
        baseline = published_baseline(plan)
        changes = change_lines(
            plan, people, baseline["slots"], baseline["days"], settings,
            days_changed=bool(
                plan.get("republish") or baseline.get("days_changed")
            ),
        )
        if changes:
            lines += ["", phrase(settings, "changes")] + changes
    return "\n".join(lines)


def update_notice(service: dict, plan: dict, people: list[dict],
                  settings: dict | None = None) -> str | None:
    """Короткий ответ на закреплённый график при обновлении: только новые
    изменения (от прошлой утверждённой копии) с упоминаниями — правка
    сообщения в Telegram не уведомляет. None — сообщать нечего."""
    if plan["approved"] is None:
        return None
    by_key = {person["key"]: person for person in people}

    def link(key: str) -> str:
        person = by_key.get(key)
        return (
            user_link(person["telegram_id"], display_name(person)) if person
            else key
        )

    changes = change_lines(
        plan, people, plan["approved"], plan.get("approved_days") or {},
        settings, link,
    )
    if not changes:
        return None
    return "\n".join(
        [publication_title(service, plan["month"], True, settings), ""]
        + changes
    )


def _columns(registry, service_id: str) -> list[ScheduleColumn]:
    service = next(
        (item for item in registry.service_catalog() if item.id == service_id),
        None,
    )
    if service is None or not service.columns:
        return [ScheduleColumn(
            headers=(service.name if service else service_id,), layout="pairs",
        )]
    return list(service.columns)


def task_moments(plan: dict, by_key: dict, day: str,
                 task: str, *, approved: bool = False) -> list[tuple[date, str]]:
    """Фактические дни назначений рабочей копии; approved=True читает
    сохранённые дни утверждённого графика вместо текущих настроек."""
    sunday = date.fromisoformat(day)
    approved_offset = (
        plan.get("approved_slot_days", {})
        .get(day, {}).get(task, {})
    ) if approved else {}
    return [
        (
            sunday + timedelta(
                # Не из состава (имя из Excel) — разовый день по ключу.
                days=approved_offset[key] if key in approved_offset else
                slot_offset(
                    plan, by_key.get(key) or {"key": key}, day, task
                ) or 0
            ),
            key,
        )
        for key in plan["slots"].get(day, {}).get(task, [])
    ]


def excel_plan(registry, service_id: str, year: int, month: int,
               events: list) -> dict:
    """График месяца из событий Excel (решение 15): воскресенья месяца,
    задачи графика, люди — ключами состава (по имени; неизвестные и
    неоднозначные — именем как в файле). Задача со своим днём (среда
    перед воскресеньем) — в его воскресенье; фактический день каждого
    назначения сохранён разовым днём. → dates, tasks, slots, days, unbound (не привязаны),
    skipped (даты не воскресенья месяца: «ср 11.11 спевка» — у задачи нет
    своего дня)."""
    people = registry.readiness_people(service_id)
    keys: dict[str, list[str]] = {}
    for person in people:
        keys.setdefault(normalized_name(person["name"]), []).append(person["key"])
    by_key = {person["key"]: person for person in people}
    tasks = plan_tasks(registry, service_id)
    names = {task["name"] for task in tasks}
    own_days = {
        task["name"]: task["day"] for task in tasks if task.get("day") is not None
    }
    dates = free_run(service_id, year, month)["dates"]
    slots = {day: {task["name"]: [] for task in tasks} for day in dates}
    days: dict = {}
    unbound: list[str] = []
    skipped: list[str] = []
    for block in week_events(
        [event for event in events if event.service_id == service_id],
        own_days,
    ):
        day = block["date"].isoformat()
        if day not in slots:
            if block["date"].weekday() != 6 and (
                block["date"].year, block["date"].month
            ) == (year, month):
                skipped += [
                    f"{_weekday_date(block['date'])} {item.task}"
                    for item in block["assignments"]
                    if not item.is_informational and item.name
                ]
            continue
        for assignment, moment in zip(block["assignments"], block["moments"]):
            if (
                assignment.is_informational or not assignment.name
                or assignment.task not in names
            ):
                continue
            found = keys.get(normalized_name(assignment.name), [])
            key = found[0] if len(found) == 1 else assignment.name
            people_slot = slots[day][assignment.task]
            if key not in people_slot:
                people_slot.append(key)
            if not (by_key.get(key) or {}).get("telegram_id"):
                if assignment.name not in unbound:
                    unbound.append(assignment.name)
            days.setdefault(day, {})[
                task_day_key(key, assignment.task)
            ] = (moment - block["date"]).days
    return {
        "dates": dates, "tasks": tasks, "slots": slots, "days": days,
        "unbound": unbound, "skipped": list(dict.fromkeys(skipped)),
    }


def plan_events(plan: dict, people: list[dict], *, keyed: bool = False) -> list[ServiceEvent]:
    """Утверждённый график бота как события графика (решение 15): по ним
    — напоминания и замены. Задача со своим днём — событием своей даты
    (как в Excel), разовый день человека учитывается."""
    if plan.get("approved") is None:
        return []
    # Задачи и дни — как утверждено (рабочая копия может быть уже
    # переименована или с другим днём задачи). Задача утверждённых
    # назначений не из снимка (снимок миграции из уже переименованной
    # копии) — без своего дня, но не теряется.
    tasks = list(plan.get("approved_tasks") or [])
    known = {task["name"] for task in tasks}
    tasks += [
        {"name": name}
        for name in dict.fromkeys(
            task for day_tasks in plan["approved"].values()
            for task in day_tasks
        )
        if name not in known
    ]
    published = {
        **plan, "slots": plan["approved"], "tasks": tasks,
        "days": plan.get("approved_days") or {},
        "approved_slot_days": plan.get("approved_slot_days") or {},
    }
    names = _names(people)
    by_key = {person["key"]: person for person in people}
    events: dict[date, list[ServiceAssignment]] = {}
    for day in sorted(plan["approved"]):
        events.setdefault(date.fromisoformat(day), [])
        for task in tasks:
            for moment, key in task_moments(
                published, by_key, day, task["name"], approved=True
            ):
                events.setdefault(moment, []).append(
                    ServiceAssignment(task["name"], key if keyed else names.get(key, key))
                )
    return [
        ServiceEvent(plan["service_id"], moment, tuple(items))
        for moment, items in sorted(events.items())
    ]


def _apply_assignment_replacements(
    assignments: dict[str, list[str]], replacements: list[dict], people: list[dict],
    *, keyed: bool = False, other_assignments: dict[str, list[str]] | None = None,
    replacement_people: dict[str, dict] | None = None,
) -> dict[str, list[str]]:
    if not keyed:
        if other_assignments is None:
            return apply_replacements(assignments, replacements)
    by_key = {person["key"]: person for person in people}
    if replacement_people is not None:
        by_key.update(replacement_people)

    def person_key(
        name: str, task: str, telegram_id: int | None = None,
        assigned: Sequence[str] = (), *, require_unique: bool = False,
        username: str = "",
    ) -> str:
        if telegram_id:
            matches = [person["key"] for person in people
                       if person["telegram_id"] == telegram_id]
        else:
            matches = [key for key in assigned
                       if by_key.get(key, {}).get("name", key) == name]
            if not matches:
                matches = [person["key"] for person in people
                           if person["name"] == name and task in person["tasks"]]
            if require_unique and len(matches) > 1:
                from .assignment_fields import AssignmentFieldResolutionError

                raise AssignmentFieldResolutionError(
                    "Нельзя однозначно определить участника сохранённой замены: "
                    "на одну дату назначены тёзки. Уточните назначения или дату задачи."
                )
        if len(matches) == 1:
            return matches[0]
        if not matches and telegram_id and replacement_people is not None:
            key = "\x1freplacement:" + json.dumps(
                [telegram_id, name, username], ensure_ascii=False,
            )
            if key not in by_key:
                person = {
                    "key": key, "name": name, "telegram_id": telegram_id,
                    "username": username,
                    "tasks": [],
                }
                replacement_people[key] = person
                by_key[key] = person
                return key
        return name

    def keyed_assignments(values):
        return {
            task: [key if key in by_key else person_key(key, task) for key in keys]
            for task, keys in values.items()
        } if keyed else values

    current = keyed_assignments(assignments)
    others = keyed_assignments(other_assignments or {})
    for replacement in replacements:
        task = replacement["task"]
        item = {
            **replacement,
            "old_name": person_key(
                replacement["old_name"], task,
                assigned=tuple(dict.fromkeys(
                    [*current.get(task, ()), *others.get(task, ())],
                )),
                require_unique=other_assignments is not None,
            ) if keyed else replacement["old_name"],
            "new_name": person_key(
                replacement["new_name"], task, replacement.get("telegram_id"),
                username=replacement.get("username") or "",
            ) if keyed else replacement["new_name"],
        }
        belongs_elsewhere = (
            item["old_name"] not in current.get(task, ())
            and item["old_name"] in others.get(task, ())
        )
        # Track the other block's chain without dropping unowned saved overrides.
        if item["old_name"] in others.get(task, ()):
            others = apply_replacements(others, [item])
        if not belongs_elsewhere:
            current = apply_replacements(current, [item])
    return current


def _effective_plan_assignments(registry, plan: dict, people: list[dict], *,
                                draft: bool = False, keyed: bool = False,
                                source_sunday: str | None = None,
                                replacement_people: dict[str, dict] | None = None,
                                ) -> dict[date, dict[str, list[str]]]:
    if draft:
        plan = {
            **plan, "approved": plan["slots"],
            "approved_tasks": plan["tasks"], "approved_days": plan["days"],
            "approved_slot_days": approved_slot_days(plan, people),
        }
    other_events = []
    if source_sunday is not None:
        sunday = date.fromisoformat(source_sunday)
        # Offsets -6..+6 can make nearby Sunday blocks overlap across months.
        months = {
            (sunday + timedelta(days=offset)).strftime("%Y-%m")
            for offset in (-14, 0, 14)
        }
        for month in sorted(months):
            candidate = (
                plan if month == source_sunday[:7]
                else registry.get_schedule_plan(plan["service_id"], month)
            )
            if candidate is None or candidate.get("approved") is None:
                continue
            other_events.extend(plan_events({
                **candidate, "approved": {
                    day: slots for day, slots in candidate["approved"].items()
                    if day != source_sunday
                },
            }, people, keyed=keyed))
        plan = {
            **plan, "approved": {
                source_sunday: plan["approved"].get(source_sunday, {}),
            },
        }
    tasks = {task["name"]: task for task in plan.get("approved_tasks") or []}
    for slots in plan["approved"].values():
        for task in slots:
            tasks.setdefault(task, {"name": task})
    assignments: dict[date, dict[str, list[str]]] = {}
    saved_tasks = {"tasks": list(tasks.values())}
    for day_key, slots in plan["approved"].items():
        for task in tasks:
            if slots.get(task):
                continue
            moment = date.fromisoformat(day_key) + timedelta(
                days=slot_offset(saved_tasks, None, day_key, task) or 0,
            )
            assignments.setdefault(moment, {}).setdefault(task, [])
    for event in plan_events(plan, people, keyed=keyed):
        if not event.assignments:
            continue
        day = assignments.setdefault(event.event_date, {})
        event_tasks: dict[str, list[str]] = {}
        for assignment in event.assignments:
            event_tasks.setdefault(assignment.task, []).append(assignment.name)
        replacements = [] if draft else registry.list_replacements(
            plan["service_id"], event.event_date.isoformat(),
            **({"source_sunday": source_sunday} if source_sunday is not None else {}),
        )
        current = _apply_assignment_replacements(
            event_tasks, replacements, people, keyed=keyed,
            other_assignments=(
                {
                    task: [
                        assignment.name for other in other_events
                        if other.event_date == event.event_date
                        for assignment in other.assignments if assignment.task == task
                    ]
                    for task in event_tasks
                } if source_sunday is not None else None
            ),
            replacement_people=replacement_people,
        )
        for task, info in current.items():
            names = day.setdefault(task, [])
            for name in task_assignment_names(info):
                if name not in names:
                    names.append(name)
    return assignments


def month_overview(
    registry, actor_id: int, service_id: str, month: date, *, include_load: bool = False,
) -> str:
    """Saved assignments by actual date; never load Excel or modify a plan."""
    first = month.replace(day=1)
    last = month_end(first.year, first.month)
    contacts = {
        (item["task"], item["name"]): item
        for item in registry.list_responsible_contacts(actor_id, service_id)
    }
    people = registry.readiness_people(service_id)
    display_names = registry.service_display_names(service_id)
    selected = registry.get_schedule_plan(service_id, first.strftime("%Y-%m"))
    if selected is None:
        month_label = "выбранный" if include_load else "текущий"
        status = (
            f"⚪ Графика на {month_label} месяц пока нет.\n"
            "Загрузите его с диска или создайте через «Опрос» / «Авто»."
        )
    elif selected["approved"] is None:
        status = "📝 Не утверждён.\nНапоминания по этому графику не отправляются."
    else:
        status = "✅ Утверждённый график с актуальными заменами."
        if selected["dirty"]:
            status += "\nЕсть неутверждённые правки; ниже действует прежний график."
        if selected.get("approval_warnings"):
            status += "\n\n⚠️ При утверждении приняты предупреждения:\n" + "\n".join(
                selected["approval_warnings"]
            )
    assignments: dict[date, dict[str, list[str]]] = {}
    load: dict[str, set[date]] = {}
    cursor = (first - timedelta(days=7)).replace(day=1)
    while cursor <= last + timedelta(days=7):
        plan = registry.get_schedule_plan(service_id, cursor.strftime("%Y-%m"))
        draft = plan is not None and plan["approved"] is None
        if draft and cursor != first:
            plan = None
        if plan is not None:
            if include_load:
                for moment, current in _effective_plan_assignments(
                    registry, plan, people, draft=draft, keyed=True,
                ).items():
                    if first <= moment <= last:
                        for keys in current.values():
                            for key in keys:
                                load.setdefault(key, set()).add(moment)
            for moment, current in _effective_plan_assignments(
                registry, plan, people, draft=draft,
            ).items():
                if not first <= moment <= last:
                    continue
                day = assignments.setdefault(moment, {})
                for task, values in current.items():
                    names = day.setdefault(task, [])
                    for name in values:
                        if name not in names:
                            names.append(name)
        cursor = (cursor + timedelta(days=32)).replace(day=1)
    lines = [status]
    if assignments and selected is None:
        lines.append("Ниже — даты соседних утверждённых графиков, попавшие в этот месяц.")
    for day, current in sorted(assignments.items()):
        lines += [
            "", f"🗓 {day:%d.%m.%Y}",
            format_task_assignments(
                current, contacts, display_names=display_names,
                service_id=service_id,
            ),
        ]
        if not any(current.values()):
            lines.append("Назначений нет.")
    if not assignments and selected is not None:
        lines += ["", "На этот месяц назначений нет."]
    if include_load:
        previous: dict[str, str] = {}
        history = registry.list_approved_schedule_plans(
            service_id, (last + timedelta(days=7)).strftime("%Y-%m"),
        )
        for plan in history:
            for moment, current in _effective_plan_assignments(
                registry, plan, people, keyed=True,
            ).items():
                if moment >= first:
                    continue
                for keys in current.values():
                    for key in keys:
                        previous[key] = max(previous.get(key, ""), moment.isoformat())
        if selected and not any(plan["month"] < selected["month"] for plan in history):
            for key, day in selected["last_served"].items():
                if day < first.isoformat():
                    previous[key] = max(previous.get(key, ""), day)
        lines += workload_lines(
            load, [person for person in people if person["tasks"]],
            previous,
        )
    return "\n".join(lines)


def excel_rows(registry, plan: dict, people: list[dict]) -> list[list]:
    """Лист как в графике: строка заголовков колонок служения, затем блок
    каждой даты — дата в первой ячейке колонки, ниже назначения. Задача со
    своим днём (спевка) — блоком своей даты (среды), как в файле. У колонки
    со своей задачей («Слайды») — только её люди, остальные задачи — в
    основной колонке; у «задача | имя» следующий человек задачи — без
    повтора задачи."""
    names = _names(people)
    columns = _columns(registry, plan["service_id"])
    own = {
        normalized_name(column.task): index
        for index, column in enumerate(columns) if column.task
    }
    main = next(
        (index for index, column in enumerate(columns) if not column.task), 0
    )
    starts, width = [], 0
    for column in columns:
        starts.append(width)
        width += 2 if column.layout == "pairs" else 1
    header = [""] * width
    for column, start in zip(columns, starts):
        header[start] = column.headers[0]
    rows: list[list] = [header]
    # Задача со своим днём (спевка — ср) — отдельным блоком своей даты, как
    # в файле; блоки по порядку дат. Дата другого месяца (среда перед
    # 01.11 — 28.10) — не на этом листе: она в тексте «Для Excel».
    by_key = {person["key"]: person for person in people}
    year, month = map(int, plan["month"].split("-"))
    blocks: dict[date, list[list[list]]] = {}
    for day in plan["dates"]:
        for task in display_tasks(plan):
            index = own.get(normalized_name(task["name"]), main)
            pairs = columns[index].layout == "pairs"
            # Задача на дату может разойтись по дням (разовый день
            # человека): название задачи — у первого в каждом блоке.
            labelled: set[date] = set()
            for moment, key in task_moments(plan, by_key, day, task["name"]):
                if (moment.year, moment.month) != (year, month):
                    continue
                cells = blocks.setdefault(moment, [[] for _ in columns])
                label = "" if moment in labelled else task["name"]
                labelled.add(moment)
                cells[index].append(
                    [label, names.get(key, key)] if pairs else [names.get(key, key)]
                )
        # Дата графика — блоком, даже если в неё никого.
        blocks.setdefault(date.fromisoformat(day), [[] for _ in columns])
    for moment in sorted(blocks):
        cells = blocks[moment]
        block = [[""] * width for _ in range(1 + max(map(len, cells)))]
        for index, start in enumerate(starts):
            block[0][start] = moment
            for offset, values in enumerate(cells[index], start=1):
                block[offset][start:start + len(values)] = values
        rows += block
    return rows


def has_sheet_columns(registry, service_id: str) -> bool:
    """У служения есть колонки на листе графика (иначе .xlsx не нужен)."""
    return any(
        item.id == service_id and item.columns
        for item in registry.service_catalog()
    )


def excel_text(registry, service: dict, plan: dict, people: list[dict]) -> str:
    year, month = map(int, plan["month"].split("-"))
    names = _names(people)
    lines = [
        f"📋 Для Excel · {service['name']} · лист "
        f"«{_schedule_sheet_name(date(year, month, 1))}»",
        (
            "Файл ниже — те же назначения по колонкам служения, как на "
            "листе графика."
            if has_sheet_columns(registry, service["id"])
            else "У служения нет колонки на листе графика — только текст."
        ),
        "",
    ]
    own_days = task_days(plan)
    by_key = {person["key"]: person for person in people}
    outside = []
    for day in plan["dates"]:
        # Задача со своим днём — в строке своего воскресенья с фактическим
        # днём (на листе — блоком своей даты); разовый день человека — в
        # скобках у имени.
        parts = []
        for task in display_tasks(plan):
            moments = task_moments(plan, by_key, day, task["name"])
            usual = date.fromisoformat(day) + timedelta(
                days=own_days.get(task["name"], 0)
            )
            head = "" if implicit_plan(plan) else (
                f"{task['name']} ({_weekday_date(usual)}) — "
                if task["name"] in own_days else f"{task['name']} — "
            )
            parts.append(head + (", ".join(
                names.get(key, key) + (
                    f" ({_weekday_date(moment)})" if moment != usual else ""
                )
                for moment, key in moments
            ) or "пусто"))
            # Своя дата задачи в другом месяце — на листе того месяца.
            outside += [
                (moment, task["name"], names.get(key, key))
                for moment, key in moments
                if (moment.year, moment.month) != (year, month)
            ]
        lines.append(f"{_short(day)}: " + "; ".join(parts))
    for moment, task, name in outside:
        lines.append(
            f"⚠️ {_weekday_date(moment)} {task} — {name}: это лист "
            f"«{_schedule_sheet_name(moment)}», на листе этого месяца её нет."
        )
    return "\n".join(lines)


def excel_file(registry, service: dict, plan: dict, people: list[dict]) -> bytes:
    from openpyxl import Workbook

    year, month = map(int, plan["month"].split("-"))
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = _schedule_sheet_name(date(year, month, 1))
    for row in excel_rows(registry, plan, people):
        sheet.append(row)
    for cells in sheet.iter_rows():
        for cell in cells:
            if isinstance(cell.value, date):
                cell.number_format = "DD.MM.YYYY"
    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


async def _edit_publication(bot, chat_id: int, message_ids: list[int],
                            text: str, text_limit: int) -> bool:
    """Правка закреплённой публикации (решение 17, 5б). False — не правится
    (удалена, другое число частей): нужна новая."""
    chunks = render_screen_chunks(text, limit=text_limit)
    if not message_ids or len(chunks) != len(message_ids):
        return False
    for message_id, chunk in zip(message_ids, chunks):
        try:
            await bot.edit_message_text(
                chunk, chat_id=chat_id, message_id=message_id,
                parse_mode="HTML",
            )
        except TelegramBadRequest as exc:
            if "not modified" in str(exc).lower():
                continue
            return False
    return True


async def publish(
    bot, registry, service: dict, plan: dict, actor_id: int,
    logger: logging.Logger, text_limit: int, *,
    acknowledged_warnings: list[str] | None = None,
) -> dict:
    """«✅ Утвердить»: график в чат служения, затем отметка в базе.
    Первая публикация закрепляется; обновление правит закреплённую и
    отвечает на неё новыми изменениями (правка не уведомляет). Не
    правится — новая публикация, закреплённая. ValueError — публиковать
    некуда. В результате pinned=False — закрепить не удалось,
    unsent_notice — текст ответа с изменениями, который не ушёл."""
    chat_id = service.get("group_chat_id")
    if not chat_id:
        raise ValueError("Чат служения не привязан — график не опубликован.")
    if not service.get("enabled", True):
        raise ValueError("Служение выключено — график не опубликован.")
    people = registry.readiness_people(service["id"])
    warnings = registry.validate_task_assignments(
        service["id"], plan["slots"], complete=True, month=plan["month"],
        days=plan.get("days"),
        acknowledged_warnings=acknowledged_warnings,
    )
    plan = {**plan, "approval_warnings": warnings}
    settings = registry.get_readiness_settings(service["id"])
    text = publication_text(
        service, plan, people, window_of(registry, service["id"]), settings,
    )
    # Прежняя публикация правится, только если она в том же чате и той же
    # теме (/here могли сменить тему — тогда новая в выбранной).
    thread_id = service.get("group_thread_id") or None
    # Сравнивается выбранная тема (закрытая остаётся выбранной, а
    # сообщение лежит в общей — это то же место).
    same_place = plan.get("chat_id") == chat_id and (
        plan_target_thread(plan) or None
    ) == thread_id
    old_ids = list(plan.get("message_ids") or []) if same_place else []
    # Сменилась только тема — прежнюю публикацию в старой теме убрать.
    stale_ids = (
        list(plan.get("message_ids") or [])
        if plan.get("chat_id") == chat_id and not same_place else []
    )
    edited = plan["approved"] is not None and await _edit_publication(
        bot, chat_id, old_ids, text, text_limit,
    )
    pinned = True
    # Тема, куда на самом деле ушла новая публикация (закрытая тема — в
    # общую): по ответу Telegram, не по настройке служения.
    sent_thread: list[int | None] = []
    # Ответ с изменениями не ушёл — его текст админу (переслать вручную):
    # после утверждения изменений уже нет, повторить нечем.
    unsent_notice = None
    sent: list[int] = []
    if edited:
        message_ids = old_ids
        notice = update_notice(service, plan, people, settings)
        if notice:
            try:
                await send_to_service_group(
                    lambda chat, body, **kwargs: send_telegram_screen(
                        bot.send_message, chat, body, limit=text_limit,
                        **kwargs,
                    ),
                    # Ответ — туда, где лежит закреплённый график.
                    chat_id, notice,
                    {**service, "group_thread_id": plan.get("thread_id")},
                    logger=logger,
                    reply_parameters=ReplyParameters(
                        message_id=old_ids[0],
                        allow_sending_without_reply=True,
                    ),
                )
            except Exception:  # noqa: BLE001 — график уже обновлён
                logger.warning("Изменения графика не отправлены ответом",
                               exc_info=True)
                unsent_notice = plain_text(notice)
    else:
        async def send_chunk(chat, body, **kwargs):
            message = await send_to_service_group(
                bot.send_message, chat, body, service,
                registry=registry, logger=logger, **kwargs,
            )
            sent.append(message.message_id)
            if not sent_thread:
                sent_thread.append(
                    getattr(message, "message_thread_id", None)
                    if getattr(message, "is_topic_message", False) else None
                )
            return message

        try:
            await send_telegram_screen(
                send_chunk, chat_id, text, limit=text_limit,
            )
        except BaseException:
            # Длинный график ушёл не целиком — убрать отправленные части,
            # иначе повторное «Утвердить» продублировало бы их.
            for message_id in sent:
                try:
                    await bot.delete_message(chat_id, message_id)
                except Exception:  # noqa: BLE001
                    logger.info("Часть графика не удалена", exc_info=True)
            raise
        message_ids = sent
    # Закрепить и при правке: раньше могло не хватить прав.
    try:
        await bot.pin_chat_message(
            chat_id, message_ids[0], disable_notification=True,
        )
    except Exception:  # noqa: BLE001 — нет права закреплять
        pinned = False
        logger.warning("График не закреплён", exc_info=True)
    # Изменения под графиком — от первой публикации месяца; смена дней
    # служения после неё помнится там же (republish сбросится).
    baseline = None
    if plan.get("baseline") is None:
        baseline = (
            {"slots": plan["slots"], "days": plan.get("days", {})}
            if plan["approved"] is None else published_baseline(plan)
        )
    if plan["approved"] is not None and plan.get("republish"):
        baseline = {
            **(baseline or plan["baseline"]), "days_changed": True,
        }
    published_thread = (
        sent_thread[0] if sent_thread else None
    ) if not edited else plan.get("thread_id")
    # Выбранная тема — та, в которую шла отправка (а не выбранная после:
    # /here во время публикации — график тогда «не на месте»). Удалённую
    # тему отправка сбросила — сообщение в общей, цель — общая.
    current_thread = (
        (registry.get_service(service["id"]) or {}).get("group_thread_id")
        or None
    )
    target_thread = thread_id
    if not edited and thread_id is not None and current_thread is None and (
        published_thread is None
    ):
        target_thread = None
    for attempt in range(2):
        try:
            result = registry.approve_schedule_plan(
                actor_id, service["id"], plan["month"], plan["slots"],
                chat_id=chat_id,
                message_id=message_ids[0] if message_ids else None,
                message_ids=message_ids,
                baseline=baseline,
                thread_id=published_thread,
                target_thread_id=target_thread,
                updated_at=plan["updated_at"], days=plan.get("days", {}),
                served_offsets=served_offsets(plan, people),
                approved_slot_days=approved_slot_days(plan, people),
                acknowledged_warnings=warnings,
            )
            break
        except Exception as exc:  # noqa: BLE001
            if attempt:
                # Повторное «Утвердить» опубликовало бы график второй раз;
                # прежняя публикация остаётся — в базе ещё её id.
                raise PublishedNotSaved(str(exc)) from exc
            logger.warning("Утверждение графика не сохранено, повтор",
                           exc_info=True)
    if not edited:
        # Прежняя публикация не правится (или правка сорвалась на середине)
        # — убрать её, чтобы в чате не осталось устаревших частей; не
        # удаляется (старше 48 ч) — хотя бы снять с закрепа.
        for message_id in old_ids + stale_ids:
            try:
                await bot.delete_message(chat_id, message_id)
            except Exception:  # noqa: BLE001
                logger.info("Прежний график не удалён", exc_info=True)
                try:
                    await bot.unpin_chat_message(
                        chat_id, message_id=message_id,
                    )
                except Exception:  # noqa: BLE001
                    logger.info("Прежний график не откреплён", exc_info=True)
    return {**result, "pinned": pinned, "unsent_notice": unsent_notice}


def excel_filled(events, service_id: str, year: int, month: int) -> bool:
    """В Excel на этот месяц у служения уже есть назначения."""
    first, last = date(year, month, 1), month_end(year, month)
    return any(
        event.service_id == service_id
        and first <= event.event_date <= last
        and any(
            item.name and not item.is_informational
            for item in event.assignments
        )
        for event in events
    )
