"""Замена ответственного (этап 3.3).

«🚫 Не смогу» в личном напоминании запускает поиск замены: объявление и
неанонимный опрос «Кто сможет…» в чате служения, сообщение админам. Итог
подводится по сроку опроса или раньше — командой админа /change. Замена
действует на одну дату и главнее графика.
"""
from __future__ import annotations

import asyncio
import logging
import secrets
from collections.abc import Callable
from datetime import date, datetime, time, timedelta, timezone

from .admin_report import refresh_admin_reports, report_recipients
from .group_send import send_to_service_group
from .presentation import apply_replacements, is_replaced  # noqa: F401

POLL_COLLECT = timedelta(hours=12)
# Ответы собираются не позже чем за 12 часов до начала дня события.
POLL_EVENT_MARGIN = timedelta(hours=12)
# Если до этого рубежа уже меньше часа — короткий срок.
POLL_SHORT = timedelta(hours=1)
POLL_OPTIONS = ("Могу", "Не смогу")

# Один поиск замены на (служение, дату, задачу, человека): между проверкой
# «запрос уже есть» и его записью идут отправки в Telegram.
_search_locks: dict[tuple[str, str, str, str], asyncio.Lock] = {}

ScheduleClose = Callable[[str, datetime], None]


def poll_deadline(now: datetime, event_date: date, tz) -> datetime:
    """Срок опроса: 12 часов, но не позже чем за 12 часов до дня события;
    если так выходит меньше часа — один час."""
    # Арифметика в UTC: переход на летнее время не сдвигает срок.
    now_utc = now.astimezone(timezone.utc)
    event_start = datetime.combine(
        event_date, time.min, tzinfo=tz
    ).astimezone(timezone.utc)
    deadline = min(now_utc + POLL_COLLECT, event_start - POLL_EVENT_MARGIN)
    return max(deadline, now_utc + POLL_SHORT).astimezone(tz)


def person_label(name: str, username: str | None) -> str:
    return f"{name} (@{username})" if username else name


def _local(deadline: str | datetime, tz) -> str:
    moment = (
        datetime.fromisoformat(deadline) if isinstance(deadline, str)
        else deadline
    )
    return f"{moment.astimezone(tz):%d.%m %H:%M}"


async def _notify(bot, chat_ids, text: str, logger: logging.Logger) -> list:
    """Отправляет сообщение; возвращает чаты, куда доставить не удалось."""
    failed = []
    for chat_id in chat_ids:
        try:
            await bot.send_message(chat_id, text, parse_mode=None)
        except Exception:  # noqa: BLE001
            failed.append(chat_id)
            logger.warning(
                "Не удалось отправить сообщение о замене в чат %s",
                chat_id, exc_info=True,
            )
    return failed


async def start_replacement_search(
    bot,
    registry,
    service_id: str,
    event_date: date,
    task: str,
    old_name: str,
    old_telegram_id: int | None,
    *,
    admin_actor_id: int,
    tz,
    logger: logging.Logger,
    schedule_close: ScheduleClose | None,
    now: datetime | None = None,
) -> dict | None:
    """Объявление и опрос в чате служения; админам — как назначить самому.

    Запрос замены сохраняется всегда: без опроса (нет чата, опрос не
    отправился) он сразу ждёт /change. Возвращает запрос или None (уже
    есть невыполненный запрос на это)."""
    key = (service_id, event_date.isoformat(), task, old_name)
    # Блокировки прошедших дат больше не нужны; текущие не удаляются, чтобы
    # ожидающий вызов и новый не получили разные блокировки.
    today = datetime.now(tz).date().isoformat()
    for stale in [item for item in _search_locks if item[1] < today]:
        if not _search_locks[stale].locked():
            del _search_locks[stale]
    lock = _search_locks.setdefault(key, asyncio.Lock())
    async with lock:
        return await _start_replacement_search_locked(
            bot, registry, service_id, event_date, task, old_name,
            old_telegram_id, admin_actor_id=admin_actor_id, tz=tz,
            logger=logger, schedule_close=schedule_close, now=now,
        )


async def _start_replacement_search_locked(
    bot,
    registry,
    service_id: str,
    event_date: date,
    task: str,
    old_name: str,
    old_telegram_id: int | None,
    *,
    admin_actor_id: int,
    tz,
    logger: logging.Logger,
    schedule_close: ScheduleClose | None,
    now: datetime | None = None,
) -> dict | None:
    service = registry.get_service(service_id)
    if service is None:
        return None
    event_iso = event_date.isoformat()
    if registry.find_active_replacement_request(
        service_id, event_iso, task, old_name
    ) or is_replaced(
        registry.list_replacements(service_id, event_iso), task, old_name
    ):
        return None
    admins = report_recipients(registry, service_id, admin_actor_id)
    date_text = f"{event_date:%d.%m.%Y}"
    manual = f"Назначить самому: /change {task} @username"
    now = now or datetime.now(timezone.utc)
    deadline = poll_deadline(now.astimezone(tz), event_date, tz)

    async def manual_request(reason: str) -> dict | None:
        request_id = f"manual-{secrets.token_hex(8)}"
        registry.create_replacement_poll(
            request_id, service_id, event_iso, task, old_name,
            old_telegram_id, 0, 0, None,
            deadline.astimezone(timezone.utc).isoformat(), status="closed",
        )
        await _notify(
            bot, admins,
            f"⚠️ Нужна замена: «{task}» · {service['name']} · {date_text} "
            f"вместо {old_name}. {reason}\n{manual}",
            logger,
        )
        return registry.get_replacement_poll(request_id)

    chat_id = service.get("group_chat_id")
    if not chat_id or not service.get("enabled", True):
        return await manual_request(
            "Чат служения не привязан — опрос не запущен."
        )
    try:
        notice = await send_to_service_group(
            bot.send_message, chat_id,
            f"🚫 {old_name} не сможет участвовать в задаче «{task}» "
            f"{date_text}. Нужна замена — ответьте в опросе до "
            f"{_local(deadline, tz)}.",
            service, registry=registry, logger=logger, parse_mode=None,
        )
        poll_message = await send_to_service_group(
            bot.send_poll, chat_id,
            f"Кто сможет участвовать в задаче «{task}» {date_text}?"[:300],
            service, registry=registry, logger=logger,
            options=list(POLL_OPTIONS), is_anonymous=False,
        )
    except Exception:  # noqa: BLE001
        logger.exception("Не удалось запустить опрос замены")
        return await manual_request(
            "Опрос в чате служения отправить не удалось."
        )
    poll_id = poll_message.poll.id
    try:
        registry.create_replacement_poll(
            poll_id, service_id, event_iso, task, old_name, old_telegram_id,
            chat_id, poll_message.message_id,
            getattr(notice, "message_id", None),
            deadline.astimezone(timezone.utc).isoformat(),
        )
    except Exception:  # noqa: BLE001
        # Опрос без записи нельзя подсчитать — останавливаем его.
        logger.exception("Не удалось сохранить опрос замены %s", poll_id)
        stopped = True
        try:
            await bot.stop_poll(chat_id, poll_message.message_id)
        except Exception:  # noqa: BLE001
            stopped = False
            logger.warning(
                "Не удалось остановить опрос замены %s", poll_id,
                exc_info=True,
            )
        state = (
            "Опрос в чате остановлен." if stopped else
            "Опрос в чате остановить не удалось — остановите его вручную, "
            "голоса в нём не учитываются."
        )
        await _notify(
            bot, admins,
            f"⚠️ Поиск замены не запущен из-за ошибки: «{task}» · "
            f"{service['name']} · {date_text} вместо {old_name}. "
            f"{state}\n{manual}",
            logger,
        )
        return None
    timer_note = ""
    if schedule_close is not None:
        try:
            schedule_close(poll_id, deadline)
        except Exception:  # noqa: BLE001
            # Опрос и запись остаются открытыми: при запуске бот снова
            # ставит открытые опросы на таймер итога, /change работает всегда.
            logger.exception("Не удалось поставить таймер опроса %s", poll_id)
            timer_note = (
                "\n⚠️ Таймер итога не поставлен: итог подведётся после "
                "перезапуска бота, или назначьте замену сами."
            )
    await _notify(
        bot, admins,
        f"🔎 Ищем замену: «{task}» · {service['name']} · {date_text} вместо "
        f"{old_name}. Опрос в чате служения до {_local(deadline, tz)}.\n"
        f"{manual}{timer_note}",
        logger,
    )
    return registry.get_replacement_poll(poll_id)


def choose_volunteer(registry, poll: dict) -> dict | None:
    """Первый «Могу» из людей с этой задачей, иначе первый «Могу»."""
    volunteers = [
        answer for answer in registry.list_poll_answers(poll["poll_id"])
        if answer["can"] and answer["telegram_id"] != poll["old_telegram_id"]
        and substitute_from_answer(registry, poll, answer)["name"]
        != poll["old_name"]
    ]
    volunteers = [
        answer for answer in volunteers
        if not registry.replacement_assignment_errors(
            poll["service_id"], poll["event_date"], poll["task"], poll["old_name"],
            substitute_from_answer(registry, poll, answer)["name"],
        )
    ]
    if not volunteers:
        return None
    task_people = registry.task_member_ids(poll["service_id"], poll["task"])
    return next(
        (item for item in volunteers if item["telegram_id"] in task_people),
        volunteers[0],
    )


def substitute_from_answer(registry, poll: dict, answer: dict) -> dict:
    person = registry.service_person(
        poll["service_id"], answer["telegram_id"], poll["task"]
    )
    name = (
        person["name"] if person
        else answer["full_name"] or (
            f"@{answer['username']}" if answer["username"]
            else "Участник чата"
        )
    )
    return {
        "name": name,
        "telegram_id": answer["telegram_id"],
        "username": (person or {}).get("username") or answer["username"],
    }


async def finish_replacement(
    bot,
    registry,
    poll: dict,
    substitute: dict | None,
    *,
    source: str,
    actor_id: int | None,
    admin_actor_id: int,
    tz,
    logger: logging.Logger,
    report_limit: int,
) -> bool:
    """Закрывает опрос и назначает замену (substitute=None — по ответам).

    Опрос без подходящего «Могу» закрывается, а запрос ждёт /change.
    False — итог по сроку уже подведён или замену уже назначили."""
    was_open = registry.close_replacement_poll(poll["poll_id"])
    if not was_open and substitute is None:
        return False
    if was_open and poll["message_id"]:
        try:
            await bot.stop_poll(poll["chat_id"], poll["message_id"])
        except Exception:  # noqa: BLE001
            logger.info(
                "Опрос замены %s не остановлен", poll["poll_id"],
                exc_info=True,
            )
    if substitute is None:
        answer = choose_volunteer(registry, poll)
        substitute = (
            substitute_from_answer(registry, poll, answer) if answer else None
        )
    service = registry.get_service(poll["service_id"])
    if service is None:
        return True
    event_date = date.fromisoformat(poll["event_date"])
    date_text = f"{event_date:%d.%m.%Y}"
    admins = report_recipients(registry, service["id"], admin_actor_id)
    if substitute is None:
        await _notify(
            bot, admins,
            f"⚠️ Замена не найдена: «{poll['task']}» · {service['name']} · "
            f"{date_text} вместо {poll['old_name']} — нет подходящего участника "
            f"среди ответивших «Могу». Проверьте допуск и обязанности внутри смены."
            f"\nНазначьте сами: /change {poll['task']} @username",
            logger,
        )
        return True
    if not registry.claim_replacement_request(poll["poll_id"]):
        return False
    try:
        await assign_replacement(
            bot, registry, service, event_date, poll["task"], poll["old_name"],
            substitute, source=source, actor_id=actor_id,
            admin_actor_id=admin_actor_id, logger=logger,
            report_limit=report_limit,
        )
    except Exception:  # noqa: BLE001
        logger.exception("Не удалось назначить замену по опросу %s", poll["poll_id"])
        try:
            registry.release_replacement_request(poll["poll_id"])
        except Exception:  # noqa: BLE001
            logger.exception("Не удалось вернуть запрос замены %s", poll["poll_id"])
        await _notify(
            bot, admins,
            f"⚠️ Не удалось назначить замену: «{poll['task']}» · "
            f"{service['name']} · {date_text} вместо {poll['old_name']}.\n"
            f"Назначьте сами: /change {poll['task']} @username",
            logger,
        )
        raise
    return True


async def assign_replacement(
    bot,
    registry,
    service: dict,
    event_date: date,
    task: str,
    old_name: str,
    substitute: dict,
    *,
    source: str,
    actor_id: int | None,
    admin_actor_id: int,
    logger: logging.Logger,
    report_limit: int,
    expect_current: bool = False,
) -> bool:
    """Сохраняет замену и сообщает о ней чату, админам и заменяющему.

    Исключение — только если не удалось сохранить саму замену; сбои
    уведомлений после сохранения записываются в журнал. False — с
    expect_current человека уже заменили (экран устарел)."""
    event_iso = event_date.isoformat()
    if not registry.set_replacement(
        service["id"], event_iso, task, old_name, substitute["name"],
        substitute.get("telegram_id"), substitute.get("username") or "",
        source, actor_id, expect_current=expect_current,
    ):
        return False
    try:
        failed = await _announce_replacement(
            bot, registry, service, event_date, task, old_name, substitute,
            source=source, admin_actor_id=admin_actor_id, logger=logger,
            report_limit=report_limit,
        )
    except Exception:  # noqa: BLE001
        logger.exception("Замена сохранена, но оповещение о ней не завершено")
        failed = ["оповещение прервано"]
    if failed:
        try:
            # Предупреждение — и админам служения, и тому, кто назначил.
            recipients = list(dict.fromkeys(
                item for item in (
                    *report_recipients(registry, service["id"], admin_actor_id),
                    actor_id,
                ) if item
            ))
            await _notify(
                bot, recipients,
                f"⚠️ Замена «{task}» {event_date:%d.%m.%Y} ({substitute['name']} "
                f"вместо {old_name}) сохранена, но сообщение не дошло: "
                f"{', '.join(failed)}. Предупредите их сами.",
                logger,
            )
        except Exception:  # noqa: BLE001
            logger.exception("Не удалось сообщить о сбое оповещения замены")
    return True


async def _announce_replacement(
    bot,
    registry,
    service: dict,
    event_date: date,
    task: str,
    old_name: str,
    substitute: dict,
    *,
    source: str,
    admin_actor_id: int,
    logger: logging.Logger,
    report_limit: int,
) -> list[str]:
    """Оповещения о замене; возвращает, кому доставить не удалось."""
    event_iso = event_date.isoformat()
    if substitute.get("telegram_id"):
        try:
            registry.set_confirmation(
                service["id"], event_iso, task, substitute["telegram_id"],
                "confirmed",
            )
        except (PermissionError, ValueError):
            pass
    date_text = f"{event_date:%d.%m.%Y}"
    label = person_label(substitute["name"], substitute.get("username"))
    text = (
        f"✅ Замена: «{task}» {date_text} — {label} вместо {old_name}."
    )
    how = "по опросу" if source == "poll" else "назначил админ"
    failed: list[str] = []
    if service.get("group_chat_id") and service.get("enabled", True):
        try:
            await send_to_service_group(
                bot.send_message, service["group_chat_id"], text, service,
                registry=registry, logger=logger, parse_mode=None,
            )
        except Exception:  # noqa: BLE001
            logger.exception("Не удалось объявить замену в чате служения")
            failed.append("чат служения")
    if await _notify(
        bot, report_recipients(registry, service["id"], admin_actor_id),
        f"{text} ({service['name']}, {how})", logger,
    ):
        failed.append("админы")
    if substitute.get("telegram_id") and await _notify(
        bot, [substitute["telegram_id"]],
        f"✅ Вы назначены: «{task}» · {service['name']} · {date_text} "
        f"вместо {old_name}. Спасибо!",
        logger,
    ):
        failed.append("заменяющий")
    try:
        await refresh_admin_reports(
            bot, registry, service["id"], event_date, admin_actor_id, logger,
            report_limit,
        )
    except Exception:  # noqa: BLE001
        logger.exception("Не удалось обновить отчёт админам после замены")
    return failed


def poll_answer_choice(option_ids) -> bool | None:
    """Вариант опроса: True — «Могу», False — «Не смогу», None — отозван."""
    if not option_ids:
        return None
    return option_ids[0] == 0


CHANGE_USAGE = "Формат: /change {задача} @username [дд.мм]"


def parse_change_args(
    args: str | None, today: date | None = None
) -> tuple[str, str, date | None]:
    """«/change звук @alex 18.10» → ("звук", "alex", date(…, 10, 18)).

    Дата необязательна; без года — ближайшая такая дата не раньше today."""
    parts = (args or "").split()
    at = [index for index, part in enumerate(parts) if part.startswith("@")]
    if not at or at[-1] == 0 or len(parts[at[-1]]) < 2:
        raise ValueError(CHANGE_USAGE)
    index = at[-1]
    rest = parts[index + 1:]
    if len(rest) > 1:
        raise ValueError(CHANGE_USAGE)
    event_date = parse_event_date(rest[0], today) if rest else None
    return " ".join(parts[:index]), parts[index][1:], event_date


def parse_event_date(text: str, today: date | None = None) -> date:
    today = today or date.today()
    pieces = text.strip().split(".")
    try:
        if len(pieces) == 2:
            day, month = int(pieces[0]), int(pieces[1])
            candidate = date(today.year, month, day)
            if candidate < today:
                candidate = date(today.year + 1, month, day)
            return candidate
        if len(pieces) == 3:
            year = int(pieces[2])
            year = year + 2000 if year < 100 else year
            return date(year, int(pieces[1]), int(pieces[0]))
    except ValueError:
        pass
    raise ValueError("Дата — в формате дд.мм или дд.мм.гггг. " + CHANGE_USAGE)
