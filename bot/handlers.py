"""Обработчик нажатия инлайн-кнопки «Принял, буду»."""
from __future__ import annotations

import asyncio
import html
import logging
import re
import secrets
import sqlite3
import time
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from typing import Any

from aiogram import F, Dispatcher, Router
from aiogram.enums import ChatType
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
)
from aiogram.dispatcher.event.bases import UNHANDLED
from aiogram.filters import Command
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    KeyboardButtonRequestChat,
    KeyboardButtonRequestUsers,
    LinkPreviewOptions,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

from .commands import (
    ROLE_SERVICE_ADMIN,
    ROLE_SUPER_ADMIN,
    ROLE_USER,
    sync_user_commands,
    user_role,
)
from .admin_report import notify_admins_declined, refresh_admin_reports
from .group_send import send_to_service_group, thread_kwargs
from .logger import TRACE
from .navigation import (
    NAV_CALLBACK_ID,
    NAV_CANCEL_TEXT,
    NAV_HINT,
    NAV_HOME_TEXT,
    NAV_REMOVED_TEXT,
    NAV_TEXTS,
    ScreenMessage,
    back_callback,
    nav_keyboard,
    replaces_nav_keyboard,
)
from .replacement import (
    assign_replacement,
    finish_replacement,
    parse_change_args,
    person_label,
    poll_answer_choice,
    start_replacement_search,
)
from .notifier import CONFIRM_PREFIX, TELEGRAM_TEXT_LIMIT, _notify_admin
from . import readiness, schedule_plan
from .personal_reminders import personal_reminder_forecast
from .month_summary import (
    linked_assignments,
    month_summary_text,
    month_title,
    next_month,
    month_end,
    service_labels,
)
from .presentation import (
    format_task_assignments,
    informational_label,
    personal_schedule_lines,
    task_assignment_names,
    task_emoji,
    task_is_informational,
)
from .ui import code as ui_code
from .service_registry import (
    ADMIN_REPORT_FIELDS,
    DEFAULT_ADMIN_REPORT_TEMPLATE,
    GENERAL_GROUP_TEMPLATE_FIELDS,
    GROUP_TEMPLATE_FIELDS,
    PERSONAL_TEMPLATE_FIELDS,
    READINESS_PHRASES,
    ServiceRegistry,
    USER_REMINDER_BODY_LIMIT,
    USER_REMINDERS_PER_SERVICE,
    SHORT_NAME_LIMIT,
    normalize_member_name,
    validate_notification_template,
)
from .scheduler import (
    periods_overlap,
    reminder_day_offset,
    reminder_occurrence,
    reminder_period_status,
)
from .services import SERVICES, normalized_name, sort_tasks_for_display
from .ui import (
    callback_text,
    clean_previous_menu as clean_previous_message,
    clean_previous_menu_by_id as clean_previous_message_by_id,
    layout_buttons,
    pair_back_help,
    nav_row,
    plain_text,
    render_screen_chunks,
    screen_text_chunks,
    send_telegram_screen,
    validate_inline_keyboard,
    visible_units,
    telegram_link,
    user_link,
)

router = Router()
PERSONAL_TASK_CACHE_TTL_SECONDS = 300


async def _answer_callback(
    callback: CallbackQuery, text: str | None = None, **kwargs
) -> None:
    normalized = callback_text(text)
    if getattr(callback, "id", None) == NAV_CALLBACK_ID:
        # Нажатие «↩️ Отмена» имитирует бот: всплывающего ответа нет,
        # поэтому предупреждения приходят обычным сообщением.
        if text and str(text).startswith(("⚠️", "❌")):
            await callback.message.notify(str(text))
        return
    if normalized is None:
        await callback.answer(**kwargs)
    else:
        await callback.answer(normalized, **kwargs)


def _status_text(text: str, icon: str) -> str:
    if text.startswith(("✅", "❌", "⚠️", "ℹ️", "⏳")):
        return text
    return f"{icon} {text}"


def _telegram_text_chunks(text: str, limit: int = 4000) -> list[str]:
    chunks = []
    current = []
    current_units = 0
    for character in text:
        character_units = len(character.encode("utf-16-le")) // 2
        if current and current_units + character_units > limit:
            chunks.append("".join(current))
            current = []
            current_units = 0
        current.append(character)
        current_units += character_units
    if current:
        chunks.append("".join(current))
    return chunks or [""]


PERSONAL_ITEM_BUTTONS_PER_PAGE = 6
DEFAULT_PERSONAL_RULE = (1, "18:00")
PERSONAL_SCHEDULE_UNAVAILABLE = (
    "График сейчас не загрузился, поэтому нельзя проверить ваши задачи "
    "и включить напоминания. Попробуйте позже."
)
PERSONAL_NO_TASKS = (
    "В текущем графике нет ваших задач в этом служении, "
    "поэтому напоминать пока не о чем."
)
PERSONAL_RULES_DISABLED = (
    "Все ваши напоминания в этом служении выключены. Включите нужное "
    "в списке напоминаний служения или добавьте своё."
)
PERSONAL_PROBLEM_SHORT = {
    PERSONAL_SCHEDULE_UNAVAILABLE: "график не загрузился",
    PERSONAL_NO_TASKS: "нет ваших задач в графике",
    PERSONAL_RULES_DISABLED: "все правила выключены",
}
PERSONAL_RULE_PRESETS = (
    (1, "18:00"),
    (1, "20:00"),
    (2, "18:00"),
    (0, "08:00"),
)
UPCOMING_WEEKS = 6
# Только супер-администратор: назначение и снятие админов. Остальное по
# служению доступно и его администратору.
ADMIN_OPERATIONS = frozenset({"service_admin_add", "service_admin_remove"})
# С каких шагов сценария привязки доступна каждая кнопка навигации.
CONTACT_NAVIGATION_STEPS = {
    "tasks": {"choose_contact_person"},
    "services": {"choose_task", "choose_member"},
    "overview": {"overview", "choose_task", "choose_member"},
    "edit": {"overview"},
    "notify": {"overview"},
    "card": {"overview", "choose_member"},
    "members": {"choose_member"},
    "add_member": {"choose_member"},
}
# Кнопка reply-клавиатуры на шаге выбора Telegram-пользователя.
CONTACT_BACK_TEXT = "⬅️ Назад"
# Тип служебной обёртки для карточки, отправляемой после /here.
SERVICE_CARD_CHAT_TYPE = "service_card"
MEMBER_PAGE_SIZE = 5
_WEEKDAYS = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")
GROUP_REMINDER_NEW_STEPS = frozenset({
    "group_reminder_new_days",
    "group_reminder_new_time",
    "group_reminder_new_text",
})


def _parse_reminder_days(text: str) -> int:
    try:
        days = int(text)
    except ValueError as exc:
        raise ValueError("Введите целое число от 0 до 6.") from exc
    if not 0 <= days <= 6:
        raise ValueError("Число дней должно быть от 0 до 6.")
    return days


def _parse_reminder_time(text: str) -> str:
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", text):
        raise ValueError("Укажите время в формате HH:MM, например 17:15.")
    return text


def _parse_period(text: str) -> tuple[date | None, date | None]:
    """«01.11.2026 27.12.2026»; «-» вместо даты — без этой границы."""
    fields = text.replace("—", " ").replace("–", " ").split()
    if len(fields) != 2:
        raise ValueError(
            "Введите две даты воскресений через пробел, например: "
            "01.11.2026 27.12.2026 («-» — без границы)."
        )
    bounds = []
    for field in fields:
        if field == "-":
            bounds.append(None)
            continue
        try:
            bounds.append(datetime.strptime(field, "%d.%m.%Y").date())
        except ValueError:
            raise ValueError(f"Не понял дату «{field}», нужно ДД.ММ.ГГГГ.") from None
    return bounds[0], bounds[1]


PERIOD_CHOICE_BASE = 100
PERIOD_STATUS_LABELS = {
    "active": "🟢 активно",
    "pending": "⏳ ещё не началось",
    "ended": "⏸ период завершён",
}


def period_text(reminder: dict, today: date) -> str:
    """«с 01.11.2026 по 27.12.2026 · 🟢 активно» или «без ограничений»."""
    status = reminder_period_status(reminder, today)
    if status is None:
        return "без ограничений"
    bounds = []
    if reminder.get("active_from"):
        bounds.append(f"с {date.fromisoformat(reminder['active_from']):%d.%m.%Y}")
    if reminder.get("active_until"):
        bounds.append(f"по {date.fromisoformat(reminder['active_until']):%d.%m.%Y}")
    return f"{' '.join(bounds)} · {PERIOD_STATUS_LABELS[status]}"


TEMPLATE_FIELD_HELP = {
    "service_name": "название служения",
    "sunday_date": "дата собрания (воскресенье)",
    "task_assignments": "задачи и ответственные",
    "week_task_assignments": "задачи служения в том же воскресном блоке; "
    "для другого дня указана дата; только на день отправки и позже",
    "informational_notes": "информационные заметки графика",
    "event_notes": "опубликованные заметки к собранию",
    "task": "задача напоминания",
    "task_responsibles": "ответственные этой задачи",
    "delivery_status": "кому включены личные уведомления",
    "confirmation_status": "ответы «Принял» / «Не смогу»",
    "month": "месяц опроса (ноябрь 2026)",
    "month_in": "месяц опроса после «в» (ноябре)",
    "deadline": "срок ответа (дд.мм чч:мм)",
    "people": "упоминания опрашиваемых",
}
GROUP_FIELDS = ("service_name", "sunday_date", "task_assignments", "week_task_assignments",
                "informational_notes", "event_notes")
REPORT_FIELDS = ("service_name", "sunday_date", "task_assignments",
                 "delivery_status", "confirmation_status")
READINESS_INTRO_HELP = ("service_name", "month", "deadline", "people")
READINESS_REMINDER_HELP = ("service_name", "month", "deadline")
READINESS_CLOSED_HELP = ("service_name", "month")
READINESS_QUESTION_HELP = ("service_name", "month", "month_in")
PLAN_TITLE_HELP = ("service_name", "month")


def template_fields_help(fields) -> str:
    """Поля шаблона — каждое с новой строки и с пояснением."""
    if not fields:
        return "Одной строкой, без полей — текст подставляется как есть."
    return "Доступные поля:\n" + "\n".join(
        f"• {{{field}}} — {TEMPLATE_FIELD_HELP[field]}" for field in fields
    )


def month_summary_label(setting: dict) -> str:
    days = setting["reminder_days"]
    when = (
        "в первое воскресенье" if days == 0
        else f"за {days} дн. до первого воскресенья"
    )
    return f"{when} в {setting['reminder_time']}"


def _rule_label(days: int, reminder_time: str) -> str:
    when = "в день служения" if days == 0 else f"за {days} дн."
    return f"{when} в {reminder_time}"


def _personal_item_pages(
    items: list[list[InlineKeyboardButton]],
    limit: int = PERSONAL_ITEM_BUTTONS_PER_PAGE,
) -> list[list[list[InlineKeyboardButton]]]:
    """Split item rows into pages without breaking a row across pages."""
    pages: list[list[list[InlineKeyboardButton]]] = [[]]
    used = 0
    for group in items:
        if pages[-1] and used + len(group) > limit:
            pages.append([])
            used = 0
        pages[-1].append(group)
        used += len(group)
    return pages


async def _persist_confirmation(context: Any, debug: logging.Logger) -> bool:
    try:
        context.state_cache.save()
    except OSError as exc:
        debug.exception("Не удалось сохранить состояние подтверждения")
        await _notify_admin(
            context.bot,
            context.admin_id,
            f"Не удалось сохранить состояние подтверждения: {exc}",
            debug,
        )
        return False
    return True


def register(dispatcher: Dispatcher, context: Any) -> None:
    """Регистрирует хэндлер, замыкая его на context."""

    interaction: logging.Logger = context.interaction_logger
    debug: logging.Logger = context.debug_logger
    admin_router = Router()
    pending_inputs: dict[int, dict[str, Any]] = {}
    pending_shares: dict[
        int, tuple[int, str, str, str | None, str | None]
    ] = {}
    pending_admin_confirmations: dict[
        tuple[int, str, int],
        tuple[str, str | None, str | None, str | None, str, str],
    ] = {}
    pending_task_choices: dict[str, tuple[int, str, str, str | None]] = {}
    pending_task_pages: dict[str, tuple[int, str, str, int]] = {}
    pending_contact_person_choices: dict[
        str, tuple[int, str, str, str]
    ] = {}
    pending_contact_navigation: dict[str, tuple[int, str, str]] = {}
    pending_notify_task_choices: dict[str, tuple[int, str, str | None]] = {}
    pending_dialog_pages: dict[
        str, tuple[int, str, InlineKeyboardMarkup, int, int, bool, int]
    ] = {}
    # Клавиатура навигации: shown, helpers (подсказки), back и screen
    # (кнопка «⬅️ …» и ID последнего экрана).
    nav_state: dict[int, dict[str, Any]] = {}
    nav_locks: dict[int, asyncio.Lock] = {}

    async def track_reply_keyboards(make_request, bot, method):
        chat_id = replaces_nav_keyboard(method)
        if chat_id in nav_state:
            # Другая нижняя клавиатура заменила или убрала навигацию.
            nav_state[chat_id]["shown"] = False
        return await make_request(bot, method)

    async def trace_callback(handler, event, data):
        """Отладка навигации: что нажал пользователь."""
        debug.log(
            TRACE, "Нажатие %s: %s",
            getattr(getattr(event, "from_user", None), "id", None),
            getattr(event, "data", None),
        )
        return await handler(event, data)

    async def trace_message(handler, event, data):
        """Отладка навигации: команды и кнопки навигации — текстом,
        остальное — только длиной (личный текст в лог не пишется).
        Сообщение пользователя «отрывает» подсказку от экранов — со
        следующим экраном она присылается заново."""
        text = getattr(event, "text", None) or ""
        actor = getattr(getattr(event, "from_user", None), "id", None)
        chat_type = getattr(getattr(event, "chat", None), "type", None)
        if (
            chat_type == ChatType.PRIVATE
            and actor in nav_state
            and nav_state[actor].get("shown")
            and text not in NAV_TEXTS
        ):
            nav_state[actor]["hint_stale"] = True
        shown = text.split()[0] if text.startswith("/") else text if (
            text in NAV_TEXTS
        ) else (
            f"текст ({len(text)} симв.)" if text else "не текст"
        )
        debug.log(
            TRACE, "Сообщение %s: %s",
            getattr(getattr(event, "from_user", None), "id", None), shown,
        )
        return await handler(event, data)

    # На уровне диспетчера — все нажатия (и кнопки напоминаний в
    # соседнем роутере); в тестах диспетчер-заглушка — тогда admin_router.
    for observer_name, middleware in (
        ("callback_query", trace_callback), ("message", trace_message),
    ):
        observer = getattr(dispatcher, observer_name, None)
        if not callable(getattr(observer, "outer_middleware", None)):
            observer = getattr(admin_router, observer_name)
        observer.outer_middleware(middleware)

    # Отложенные привязки по @username: None — перечитать из базы.
    username_waiting: dict[str, set[str] | None] = {"names": None}
    # Последний виденный username по Telegram ID и владелец username
    # (чтобы не писать в базу на каждое сообщение).
    username_seen: dict[int, str] = {}
    username_owner: dict[str, int] = {}

    async def complete_username_bindings(user) -> None:
        """Человек с ожидаемым @username написал боту — привязываем его
        и сообщаем админу, который задал привязку."""
        username = getattr(user, "username", None) or ""
        user_id = getattr(user, "id", None)
        if getattr(user, "is_bot", False) or not isinstance(user_id, int):
            return
        registry = service_registry()
        if registry is None:
            return
        try:
            # Наблюдение «ID ↔ username» — только при изменении (в том
            # числе когда username вернулся к прежнему владельцу).
            seen = username.casefold()
            if username_seen.get(user_id) != seen or (
                seen and username_owner.get(seen) != user_id
            ):
                registry.note_seen_username(user_id, username)
                previous = username_seen.get(user_id)
                if previous and username_owner.get(previous) == user_id:
                    username_owner.pop(previous, None)
                username_seen[user_id] = seen
                if seen:
                    username_owner[seen] = user_id
            if not username:
                return
            if username_waiting["names"] is None:
                username_waiting["names"] = registry.pending_usernames()
            if username.casefold() not in username_waiting["names"]:
                return
            bindings = registry.take_username_bindings(username)
        except sqlite3.Error:
            debug.exception("Не удалось прочитать привязки по username")
            return
        username_waiting["names"].discard(username.casefold())
        for binding in bindings:
            service_id, name = binding["service_id"], binding["name"]
            try:
                # Этот Telegram уже привязан к другому имени в служении —
                # чужую привязку молча не перезаписываем.
                bound = registry.service_person(service_id, user.id)
                if bound is not None and bound["name"] != name:
                    raise ValueError(
                        f"этот Telegram уже привязан к «{bound['name']}». "
                        "Проверьте и привяжите вручную"
                    )
                if binding["task"]:
                    registry.set_responsible_contact(
                        binding["actor_id"], service_id, binding["task"],
                        name, user.id, username,
                    )
                else:
                    registry.bind_service_member(
                        binding["actor_id"], service_id, name, user.id,
                        username, tasks=binding["tasks"],
                    )
            except (PermissionError, ValueError, sqlite3.Error) as exc:
                debug.warning(
                    "Привязка по username не выполнена (%s): %s",
                    service_id, exc,
                )
                text = (
                    f"❌ @{username} написал(а) боту, но привязать "
                    f"«{name}» не удалось: {exc}"
                )
            else:
                actor_pending = pending_inputs.get(binding["actor_id"]) or {}
                if actor_pending.get("service_id") == service_id:
                    refresh_registered_contacts(binding["actor_id"], service_id)
                service = service_by_id(registry, service_id)
                text = (
                    f"✅ @{username} написал(а) боту — «{name}» привязан(а) "
                    + (f"к задаче «{binding['task']}»" if binding["task"]
                       else "во всех задачах")
                    + (f" · {service_label(service)}" if service else "")
                )
            try:
                await context.bot.send_message(
                    chat_id=binding["actor_id"], text=text, parse_mode=None,
                )
            except Exception:  # noqa: BLE001
                debug.warning(
                    "Не удалось сообщить о привязке по username", exc_info=True
                )

    async def username_binding_middleware(handler, event, data):
        user = getattr(event, "from_user", None) or getattr(event, "user", None)
        if user is not None:
            try:
                await complete_username_bindings(user)
            except Exception:  # noqa: BLE001
                debug.exception("Сбой отложенной привязки по username")
        return await handler(event, data)

    for observer_name in ("callback_query", "message", "poll_answer"):
        observer = getattr(dispatcher, observer_name, None)
        if not callable(getattr(observer, "outer_middleware", None)):
            observer = getattr(admin_router, observer_name)
        observer.outer_middleware(username_binding_middleware)

    session_middleware = getattr(
        getattr(getattr(context, "bot", None), "session", None),
        "middleware",
        None,
    )
    if callable(session_middleware):
        # Как в документации aiogram: bot.session.middleware(...).
        session_middleware(track_reply_keyboards)

    async def delete_quietly(chat_id: int, message_ids: list[int]) -> list[int]:
        """Удаляет сообщения; возвращает ID, которые удалить не удалось
        (уже удалённые считаются удалёнными)."""
        failed = []
        for message_id in message_ids:
            try:
                await context.bot.delete_message(
                    chat_id=chat_id, message_id=message_id
                )
            except TelegramAPIError as exc:
                text = str(exc).lower()
                if isinstance(exc, (TelegramBadRequest, TelegramForbiddenError)):
                    # Уже удалено, старше 48 часов или бот заблокирован —
                    # повтор не поможет.
                    debug.debug(
                        "Сообщение навигации %s не удалить: %s", message_id, text
                    )
                    continue
                debug.debug(
                    "Не удалось удалить сообщение навигации %s", message_id
                )
                failed.append(message_id)
        return failed

    async def drop_old_hints(
        chat_id: int, actor_id: int, keep: int | None = None
    ) -> None:
        """Удаляет сохранённые подсказки, кроме keep; неудалённые остаются
        в базе, чтобы следующая попытка (и после перезапуска) их нашла."""
        state = nav_state.get(actor_id) or {}
        known = list(dict.fromkeys([
            *state.get("helpers", []), *stored_hints(actor_id),
        ]))
        stale = [
            message_id for message_id in known
            if message_id != keep and isinstance(message_id, int)
        ]
        failed = await delete_quietly(chat_id, stale)
        # Не больше 10 неудалённых: список не растёт бесконечно.
        remaining = [
            *failed[-10:], *([keep] if isinstance(keep, int) else []),
        ]
        if state:
            state["helpers"] = remaining
        if remaining != known:
            store_hints(actor_id, remaining)

    async def show_nav_keyboard(
        target: Message, actor_id: int, *, force: bool = False
    ) -> None:
        async with nav_locks.setdefault(actor_id, asyncio.Lock()):
            await _show_nav_keyboard(target, actor_id, force=force)

    async def hide_nav_keyboard(chat_id: int, actor_id: int) -> None:
        # Показ и скрытие одного пользователя — по очереди, иначе ответы
        # Telegram могут прийти в обратном порядке и разойтись с nav_state.
        async with nav_locks.setdefault(actor_id, asyncio.Lock()):
            await _hide_nav_keyboard(chat_id, actor_id)

    def stored_hints(actor_id: int) -> list[int]:
        load = getattr(service_registry(), "get_nav_hints", None)
        try:
            return load(actor_id) if callable(load) else []
        except sqlite3.Error:
            debug.debug("Не удалось прочитать подсказки навигации", exc_info=True)
            return []

    def store_hints(actor_id: int, message_ids: list[int]) -> None:
        """ID подсказок — в базе: после перезапуска бота старая подсказка
        всё равно удаляется, а не остаётся второй в чате."""
        save = getattr(service_registry(), "set_nav_hints", None)
        if not callable(save):
            return
        try:
            save(actor_id, message_ids)
        except sqlite3.Error:
            debug.debug("Не удалось сохранить подсказки навигации", exc_info=True)

    async def _show_nav_keyboard(
        target: Message, actor_id: int, *, force: bool = False
    ) -> None:
        """force — подсказку «оторвало» сообщение пользователя: прислать
        её заново над новым экраном, старую удалить."""
        state = nav_state.setdefault(actor_id, {"shown": False, "helpers": []})
        if state["shown"] and not force:
            helpers = state.get("helpers", [])
            if len(helpers) > 1:
                # Прежнюю подсказку не удалось удалить — ещё попытка.
                await drop_old_hints(target.chat.id, actor_id, keep=helpers[-1])
            return
        try:
            hint = await target.answer(
                NAV_HINT, reply_markup=nav_keyboard(), parse_mode=None
            )
        except TelegramAPIError:
            debug.warning("Не удалось показать клавиатуру навигации %s", actor_id)
            return
        state["shown"] = True
        state["hint_stale"] = False
        state["helpers"] = [*state.get("helpers", []), hint.message_id]
        if isinstance(hint.message_id, int):
            # Сначала сохранить новую вместе со старыми: старые удаляются
            # из базы только после успешного удаления в чате.
            store_hints(actor_id, list(dict.fromkeys([
                *stored_hints(actor_id), hint.message_id,
            ])))
        await drop_old_hints(target.chat.id, actor_id, keep=hint.message_id)

    async def _hide_nav_keyboard(chat_id: int, actor_id: int) -> None:
        state = nav_state.get(actor_id)
        if state is None or not state["shown"]:
            # Клавиатуру уже заменила другая (или бот перезапущен) —
            # подсказки всё равно убрать.
            if (state or {}).get("helpers") or stored_hints(actor_id):
                await drop_old_hints(chat_id, actor_id)
            return
        try:
            removed = await context.bot.send_message(
                chat_id, NAV_REMOVED_TEXT, reply_markup=ReplyKeyboardRemove()
            )
        except TelegramAPIError:
            # Клавиатура осталась у клиента: следующий корневой экран
            # попробует убрать её снова.
            debug.warning("Не удалось убрать клавиатуру навигации %s", actor_id)
            state["shown"] = True
            return
        state["shown"] = False
        state["hint_stale"] = False
        await delete_quietly(chat_id, [removed.message_id])
        await drop_old_hints(chat_id, actor_id)
    pending_personal_actions: dict[
        str, tuple[int, str, str, str, dict[str, Any]]
    ] = {}

    dialog_locks: dict[int, asyncio.Lock] = {}
    screen_parts: dict[tuple[int, int], tuple[int, ...]] = {}

    def take_screen_parts(chat_id: int, message_id: int) -> tuple[int, ...]:
        parts = screen_parts.get((chat_id, message_id), (message_id,))
        for part in parts:
            screen_parts.pop((chat_id, part), None)
        return parts

    async def clean_previous_menu_by_id(
        bot, chat_id, message_id, logger, **kwargs,
    ) -> None:
        if chat_id is None or message_id is None:
            return
        for part in take_screen_parts(chat_id, message_id):
            await clean_previous_message_by_id(
                bot, chat_id, part, logger, **kwargs,
            )

    async def clean_previous_menu(message, logger, **kwargs) -> None:
        if message is None:
            return
        chat_id = getattr(getattr(message, "chat", None), "id", None)
        message_id = getattr(message, "message_id", None)
        if (chat_id, message_id) in screen_parts:
            await clean_previous_menu_by_id(
                context.bot, chat_id, message_id, logger, **kwargs,
            )
        else:
            await clean_previous_message(message, logger, **kwargs)

    async def _send_dialog(target: Message, text: str, **kwargs) -> Message | None:
        actor_id = getattr(getattr(target, "chat", None), "id", None)
        if actor_id is not None and target.chat.type == ChatType.PRIVATE:
            async with dialog_locks.setdefault(actor_id, asyncio.Lock()):
                return await _send_dialog_locked(target, text, **kwargs)
        return await _send_dialog_locked(target, text, **kwargs)

    async def _send_dialog_locked(
        target: Message,
        text: str,
        *,
        _dialog_page: int = 0,
        clean_previous: bool = True,
        **kwargs,
    ) -> Message | None:
        kwargs.pop("parse_mode", None)
        kwargs.setdefault("link_preview_options", LinkPreviewOptions(is_disabled=True))
        keyboard_limit = kwargs.pop("_dialog_keyboard_limit", 6)
        keep_rows = kwargs.pop("_dialog_keep_rows", False)
        text_limit = kwargs.pop("_dialog_text_limit", 500)
        markup = kwargs.pop("reply_markup", None)
        sent_ids = kwargs.pop("_dialog_sent_ids", None)
        actor_id = getattr(getattr(target, "chat", None), "id", None)
        chat_type = getattr(getattr(target, "chat", None), "type", None)
        pending = (
            pending_inputs.get(actor_id)
            if chat_type == ChatType.PRIVATE
            else None
        )
        if (chat_type == ChatType.PRIVATE
                and text.lstrip().startswith(("❌", "⚠️"))
                and (markup is None or isinstance(markup, InlineKeyboardMarkup))):
            rows = (
                [list(row) for row in markup.inline_keyboard]
                if isinstance(markup, InlineKeyboardMarkup) else []
            )
            previous_back = (nav_state.get(actor_id) or {}).get("back")
            if not rows or back_callback(InlineKeyboardMarkup(inline_keyboard=rows)) is None:
                rows.append([InlineKeyboardButton(
                    text="⬅️ Назад",
                    callback_data=previous_back or "menu:start",
                )])
            if not any(
                button.text == "🏠 В начало"
                for row in rows for button in row
            ):
                rows.append([InlineKeyboardButton(
                    text="🏠 В начало", callback_data="menu:start",
                )])
            markup = InlineKeyboardMarkup(inline_keyboard=rows)
        # Карточка после /here не сбрасывает листание других меню админа.
        if chat_type != SERVICE_CARD_CHAT_TYPE:
            for token, page_state in list(pending_dialog_pages.items()):
                if page_state[0] == actor_id:
                    pending_dialog_pages.pop(token, None)
        if (
            clean_previous
            and pending is not None
            and pending.get("menu_message_id") is not None
            and pending.get("menu_chat_id") is not None
        ):
            await clean_previous_menu_by_id(
                context.bot,
                pending.get("menu_chat_id"),
                pending.get("menu_message_id"),
                debug,
            )
        if isinstance(markup, InlineKeyboardMarkup):
            buttons = [
                button
                for row in markup.inline_keyboard
                for button in row
            ]
            for button in buttons:
                label = button.text
                if not label.isdecimal() and not label.startswith(
                    (
                        "✅", "❌", "⚠️", "ℹ️", "⏳", "🟢", "🔴", "⚪",
                        "⚙️", "🔗", "👥", "👤", "📡", "🔔", "📝", "📊",
                        "📋", "📅", "🕒", "🎤", "📖", "🍞", "🧪", "👁",
                        "⏰", "📷", "🎛️", "💻", "🎬", "🎚️", "🖥️",
                        "🍽️", "☕", "📌", "🔕",
                        "🔧", "🧹", "🔄", "⬅️", "➡️", "🏠", "✖️", "➕",
                        "➖", "🗑", "✏️", "1️⃣", "2️⃣", "3️⃣", "4️⃣",
                        "📤", "🔁", "🗓", "🧵", "🚫", "📆", "♾", "▶️", "⏸",
                        "🗳", "⏹", "⏭", "↩️", "🏁", "💬", "⬜", "☑️",
                        "🔢", "🔤", "⛔", "↔️", "📥", "🌐", "🧮", "❓",
                    )
                ):
                    button.text = f"📋 {label}"

            if keep_rows:
                total = sum(len(row) for row in markup.inline_keyboard)
                if total > keyboard_limit:
                    # Экран важнее лимита оформления: показать как есть
                    # (Telegram допускает до 100 кнопок) и записать в лог.
                    debug.warning(
                        "Кнопок на экране %s больше лимита %s", total,
                        keyboard_limit,
                    )
                    keyboard_limit = total
                validate_inline_keyboard(
                    markup.inline_keyboard, max_buttons=keyboard_limit
                )
            elif len(buttons) > keyboard_limit:
                pinned_buttons = [
                    button
                    for button in buttons
                    if button.text.startswith((
                        "⬅️ К ", "↩️ К ", "📅 К графику",
                        "👤 Выбрать пользователя Telegram",
                        "🏠 В начало",
                    ))
                    or (
                        back_callback(markup) is not None
                        and button.text.startswith(("❓ Справка", "❓ Помощь"))
                    )
                    or (
                        button.text == "⬅️ Назад"
                        and text.lstrip().startswith(("❌", "⚠️"))
                    )
                ]
                pageable_buttons = [
                    button for button in buttons
                    if button not in pinned_buttons
                ]
                page_size = max(
                    1,
                    min(6, keyboard_limit - len(pinned_buttons) - 2),
                )
                page_count = max(
                    1, (len(pageable_buttons) + page_size - 1) // page_size
                )
                _dialog_page = min(max(_dialog_page, 0), page_count - 1)
                page_buttons = pageable_buttons[
                    _dialog_page * page_size:(_dialog_page + 1) * page_size
                ]
                navigation = []
                for action, next_page, label in (
                    ("prev", _dialog_page - 1, "⬅️ Назад"),
                    ("next", _dialog_page + 1, "Далее ➡️"),
                ):
                    if 0 <= next_page < page_count:
                        token = secrets.token_urlsafe(8)
                        if actor_id is not None:
                            pending_dialog_pages[token] = (
                                actor_id, text, markup, next_page,
                                keyboard_limit, keep_rows, text_limit,
                            )
                        navigation.append(
                            InlineKeyboardButton(
                                text=label,
                                callback_data=f"ui_page:{token}",
                            )
                        )
                navigation = nav_row(
                    back=next(
                        (
                            button for button in navigation
                            if button.text.startswith("⬅️")
                        ),
                        None,
                    ),
                    save=next(
                        (
                            button for button in navigation
                            if button.text.endswith("➡️")
                        ),
                        None,
                    ),
                )
                # Листание — над «⬅️ К …» (решение 17, 5в): выход с экрана
                # всегда последний.
                rows = layout_buttons(page_buttons)
                if navigation:
                    rows.append(navigation)
                rows += layout_buttons(pinned_buttons) if pinned_buttons else []
                validate_inline_keyboard(rows, max_buttons=keyboard_limit)
                markup = InlineKeyboardMarkup(inline_keyboard=rows)
                text = f"{text}\n📊 Страница {_dialog_page + 1}/{page_count}"
            else:
                rows = layout_buttons(buttons)
                validate_inline_keyboard(
                    rows, max_buttons=keyboard_limit
                )
                markup = InlineKeyboardMarkup(inline_keyboard=rows)

        if isinstance(markup, InlineKeyboardMarkup):
            rows = pair_back_help(markup.inline_keyboard)
            validate_inline_keyboard(rows, max_buttons=keyboard_limit)
            markup = InlineKeyboardMarkup(inline_keyboard=rows)

        nav_actor = (
            actor_id
            if chat_type == ChatType.PRIVATE and actor_id is not None
            else None
        )
        command = (
            nav_actor is not None
            and isinstance(markup, InlineKeyboardMarkup)
            and (getattr(target, "text", None) or "").startswith("/")
        )
        command_id = getattr(target, "message_id", None) if command else None
        if not isinstance(command_id, int):
            command_id = None

        def late_command() -> bool:
            # Более поздняя команда уже показала свой экран.
            return command_id is not None and command_id < nav_state.get(
                nav_actor, {}
            ).get("last_command", 0)

        if late_command():
            return None
        if command_id is not None:
            nav_state.setdefault(
                nav_actor, {"shown": False, "helpers": []}
            )["last_command"] = command_id
        nav_back = None
        nav_action = "без изменений"
        if nav_actor is not None and isinstance(markup, InlineKeyboardMarkup):
            # Со второго уровня (есть «⬅️ …») — навигация внизу,
            # на корневых экранах она убирается.
            nav_back = back_callback(markup)
            if nav_back is not None:
                nav_action = "показать"
                await show_nav_keyboard(
                    target, nav_actor,
                    force=nav_state.get(nav_actor, {}).get("hint_stale", False),
                )
            else:
                nav_action = "убрать"
                await hide_nav_keyboard(target.chat.id, nav_actor)
        elif nav_actor in nav_state and isinstance(
            markup, (ReplyKeyboardMarkup, ReplyKeyboardRemove)
        ):
            # Своя нижняя клавиатура экрана заменяет навигацию.
            nav_state[nav_actor]["shown"] = False

        chunks = render_screen_chunks(text, limit=text_limit)
        sent_message = None
        parts = []
        completed = False
        try:
            for index, chunk in enumerate(chunks):
                sent_message = await target.answer(
                    chunk,
                    parse_mode="HTML",
                    reply_markup=(
                        markup if index == len(chunks) - 1 else None
                    ),
                    **kwargs,
                )
                parts.append(sent_message.message_id)
                if sent_ids is not None:
                    sent_ids.append(sent_message.message_id)
            completed = True
        finally:
            if not completed:
                for part in parts:
                    await clean_previous_message_by_id(
                        context.bot, target.chat.id, part, debug
                    )
        if len(parts) > 1 and nav_actor is not None:
            for part in parts:
                screen_parts[(actor_id, part)] = tuple(parts)
        if sent_message is not None and late_command():
            # Пока отправлялся этот экран, более поздняя команда показала
            # свой — этот лишний.
            await clean_previous_menu_by_id(
                context.bot, target.chat.id, sent_message.message_id, debug
            )
            return None
        if sent_message is not None and pending is not None:
            pending["menu_message_id"] = sent_message.message_id
            pending["menu_chat_id"] = target.chat.id
        if nav_actor is not None and sent_message is not None:
            state = nav_state.setdefault(
                nav_actor, {"shown": False, "helpers": []}
            )
            previous_menu = state.get("screen") if (
                state.get("menu")
                or (target.chat.id, state.get("screen")) in screen_parts
            ) else None
            state["menu"] = isinstance(markup, InlineKeyboardMarkup)
            # Сразу, до await: параллельный обработчик увидит этот экран.
            state["screen"] = sent_message.message_id
            if (
                clean_previous
                and previous_menu is not None
                and (command or (target.chat.id, previous_menu) in screen_parts)
                and previous_menu != sent_message.message_id
            ):
                # Экран команды заменяет прежнее меню. Удаление — после
                # отправки: два быстрых /services не оставят оба экрана.
                await clean_previous_menu_by_id(
                    context.bot, target.chat.id, previous_menu, debug
                )
            if markup is not None or not text.startswith("⏳"):
                state["back"] = nav_back
            # «⏳ Загружаю…» шаг назад не меняет: «Отмена» вернёт с прежнего
            # экрана. Итоговое сообщение без кнопок — «Отмена» ведёт в /start.
            state["screen"] = sent_message.message_id
            debug.log(
                TRACE,
                "Экран %s → %s: назад=%s, клавиатура: %s (показана: %s)",
                sent_message.message_id, nav_actor, nav_back, nav_action,
                state["shown"],
            )
        return sent_message

    @admin_router.callback_query(F.data.startswith("ui_page:"))
    async def on_dialog_page(callback: CallbackQuery) -> None:
        if (
            not callback.from_user
            or not callback.message
            or callback.message.chat.type != ChatType.PRIVATE
        ):
            await _answer_callback(callback)
            return
        _, token = callback.data.split(":", 1)
        page_state = pending_dialog_pages.pop(token, None)
        if page_state is None or page_state[0] != callback.from_user.id:
            await _answer_callback(callback, "Выбор устарел")
            return
        _, text, markup, page, keyboard_limit, keep_rows, text_limit = page_state
        await clean_previous_menu(callback.message, debug)
        await _send_dialog(
            callback.message,
            text,
            reply_markup=markup,
            _dialog_page=page,
            _dialog_keyboard_limit=keyboard_limit,
            _dialog_keep_rows=keep_rows,
            _dialog_text_limit=text_limit,
            clean_previous=False,
        )
        await _answer_callback(callback)

    def new_share_request_id() -> int:
        registry = service_registry()
        if registry is None:
            raise ValueError("Реестр служений временно недоступен.")
        return registry.next_share_request_id()

    def clear_pending_actor(actor_id: int) -> None:
        pending_inputs.pop(actor_id, None)
        for token, action in list(pending_personal_actions.items()):
            if action[0] == actor_id:
                pending_personal_actions.pop(token, None)
        for token, page in list(pending_dialog_pages.items()):
            if page[0] == actor_id:
                pending_dialog_pages.pop(token, None)
        for request_id, pending in list(pending_shares.items()):
            if pending[0] == actor_id:
                pending_shares.pop(request_id, None)
        for key in list(pending_admin_confirmations):
            if key[0] == actor_id:
                pending_admin_confirmations.pop(key, None)
        for key in list(pending_task_choices):
            if pending_task_choices[key][0] == actor_id:
                pending_task_choices.pop(key, None)
        for key in list(pending_task_pages):
            if pending_task_pages[key][0] == actor_id:
                pending_task_pages.pop(key, None)
        for key in list(pending_contact_person_choices):
            if pending_contact_person_choices[key][0] == actor_id:
                pending_contact_person_choices.pop(key, None)
        for key in list(pending_contact_navigation):
            if pending_contact_navigation[key][0] == actor_id:
                pending_contact_navigation.pop(key, None)
        for key in list(pending_notify_task_choices):
            if pending_notify_task_choices[key][0] == actor_id:
                pending_notify_task_choices.pop(key, None)

    # (админ, служение) -> (чат, ID всех сообщений карточки).
    last_service_cards: dict[tuple[int, str], tuple[int, list[int]]] = {}

    async def clean_and_clear_pending_actor(actor_id: int) -> None:
        pending = pending_inputs.get(actor_id)
        if pending is not None:
            if pending.get("operation", "").startswith("my_notifications"):
                await clean_previous_personal_menu(pending)
            else:
                await clean_previous_menu_by_id(
                    context.bot,
                    pending.get("menu_chat_id"),
                    pending.get("menu_message_id"),
                    debug,
                )
        clear_pending_actor(actor_id)

    def service_registry():
        registry = getattr(context, "service_registry", None)
        if registry is None:
            return None
        return registry

    def is_service_admin(actor_id: int, service_id: str) -> bool:
        registry = service_registry()
        return bool(
            registry
            and (
                registry.is_super_admin(actor_id)
                or registry.is_service_admin(actor_id, service_id)
            )
        )

    def can_check_delivery(actor_id: int, service_id: str) -> bool:
        """Проверка доставки — внутри служения, когда групповой чат
        назначен: её запускает тот, кто настраивает чат."""
        if not is_service_admin(actor_id, service_id):
            return False
        service = service_by_id(service_registry(), service_id)
        return bool(service and service["group_chat_id"] is not None)
    async def require_chat_member(actor_id: int, chat_id: int) -> None:
        """Чат по ID вручную: админ служения должен сам состоять в группе
        (выбор кнопкой Telegram это гарантирует). Супер-администратор — без
        проверки."""
        registry = service_registry()
        if registry is None or registry.is_super_admin(actor_id):
            return
        try:
            chat = await context.bot.get_chat(chat_id)
            if getattr(chat, "type", None) not in {"group", "supergroup"}:
                raise PermissionError(
                    "Это не группа — привязать можно только групповой чат."
                )
            member = await context.bot.get_chat_member(chat_id, actor_id)
        except (TelegramAPIError, asyncio.TimeoutError) as exc:
            # Сбой или таймаут проверки — понятное сообщение, не падение.
            raise PermissionError(
                "Не удалось проверить, что вы состоите в этом чате, — "
                "выберите чат кнопкой."
            ) from exc
        status = getattr(member, "status", None)
        if status not in {
            "creator", "administrator", "member", "restricted",
        } or (status == "restricted" and not getattr(member, "is_member", True)):
            raise PermissionError(
                "Вы не состоите в этом чате — привязать можно только свой "
                "групповой чат."
            )

    def is_user_linked(actor_id: int) -> bool:
        """Проверяет, привязан ли пользователь хотя бы к одной задаче."""
        registry = service_registry()
        if registry is None:
            return False
        try:
            return bool(registry.list_user_services(actor_id))
        except sqlite3.Error:
            debug.exception("Ошибка при проверке привязки в /start")
            return False

    async def refresh_user_commands(telegram_id: int) -> None:
        """Обновляет меню команд пользователя после смены его роли."""
        if not callable(getattr(context.bot, "set_my_commands", None)):
            return
        await sync_user_commands(
            context.bot,
            service_registry(),
            telegram_id,
            context.admin_id,
            debug,
        )

    def service_by_id(registry: Any, service_id: str) -> dict | None:
        return next(
            (service for service in registry.list_services()
             if service["id"] == service_id),
            None,
        )

    def service_label(service: dict) -> str:
        return service["name"].upper()

    async def group_chat_title(chat_id: int | None) -> str | None:
        get_chat = getattr(context.bot, "get_chat", None)
        if chat_id is None or not callable(get_chat):
            return None
        try:
            chat = await get_chat(chat_id)
        except (TelegramAPIError, asyncio.TimeoutError):
            debug.exception("Не удалось получить название группового чата")
            return None
        return getattr(chat, "title", None) or None

    def settings_text(
        registry: Any,
        actor_id: int,
        service_id: str,
        chat_title: str | None = None,
    ) -> str:
        if not is_service_admin(actor_id, service_id):
            raise PermissionError(
                "Настройки доступны только администратору этого служения."
            )
        service = service_by_id(registry, service_id)
        if service is None:
            raise ValueError("Служение не найдено.")
        enabled_status = "🟢 включено" if service["enabled"] else "🔴 выключено"
        chat_id = service["group_chat_id"]
        if chat_id is None:
            chat_status = "⚪ не задан"
        elif chat_title:
            chat_status = f"«{chat_title}»"
        else:
            chat_status = ui_code(chat_id)
        reminders = registry.list_group_reminders(service_id)
        enabled_reminders = [item for item in reminders if item["enabled"]]
        if not reminders:
            reminders_status = "⚪ не настроены"
        elif len(enabled_reminders) == len(reminders):
            reminders_status = f"🟢 {len(reminders)}"
        else:
            reminders_status = (
                f"🟢 {len(enabled_reminders)} из {len(reminders)} включены"
            )
        lines = [
            f"⚙️ {service_label(service)} · {enabled_status}",
            "",
            f"📡 Групповой чат · {chat_status}",
        ]
        if chat_id is not None:
            thread_id = service.get("group_thread_id")
            lines.append(
                f"🧵 Тема · {ui_code(thread_id)}"
                if thread_id
                else "🧵 Тема · общая (/here в нужной теме — сменить)"
            )
        lines.append(f"⏰ Напоминания групп · {reminders_status}")
        now = datetime.now(context.timezone)
        upcoming = [
            (*occurrence, item)
            for item in enabled_reminders
            if (occurrence := reminder_occurrence(now, item)) is not None
        ]
        if upcoming:
            event_date, send_date, reminder = min(
                upcoming,
                key=lambda entry: (entry[1], entry[2]["reminder_time"]),
            )
            schedule = (
                f"{send_date:%d.%m.%Y} в {reminder['reminder_time']} "
                f"(собрание {event_date:%d.%m.%Y})"
            )
            if not service["enabled"]:
                lines.append(
                    f"⚠️ Отправка не выполняется: служение выключено "
                    f"· по расписанию {schedule}"
                )
            elif chat_id is None:
                lines.append(
                    f"⚠️ Отправка не выполняется: не задан групповой чат "
                    f"· по расписанию {schedule}"
                )
            else:
                lines.append(f"✉️ Ближайшая отправка · {schedule}")
        personal_templates = service["personal_templates"]
        admins_count = len(registry.list_service_admins(service_id))
        lines.extend(
            [
                f"👤 Администраторы · {admins_count or '⚪ не назначены'}",
                "📝 Шаблон по умолчанию · "
                + ("🟢 свой" if service["group_template"] else "⚪ встроенный"),
                "📝 Личные шаблоны · "
                + (
                    f"🟢 {len(personal_templates)}"
                    if personal_templates
                    else "⚪ не заданы"
                ),
                "",
                "ℹ️ Выберите параметр для изменения.",
            ]
        )
        return "\n".join(lines)

    def ordered_services(services: list[dict]) -> list[dict]:
        service_rank = {
            service.id: index for index, service in enumerate(SERVICES)
        }
        return sorted(
            services,
            key=lambda service: service_rank.get(
                service["id"], len(service_rank)
            ),
        )

    def service_selection_keyboard(
        services: list[dict],
        operation: str,
    ) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=(
                            f"{'🟢' if service['enabled'] else '🔴'} "
                            f"{service_label(service)}"
                        ),
                        callback_data=f"service:{operation}:{service['id']}",
                    )
                ]
                for service in ordered_services(services)
            ]
        )

    async def request_service_input(
        message: Message,
        operation: str,
        service_id: str | None = None,
        actor_id: int | None = None,
    ) -> None:
        registry = service_registry()
        if registry is None:
            await _send_dialog(message, "Реестр настроек служений недоступен. "
                "Попробуйте позже или сообщите супер-администратору.",
                parse_mode=None,
            )
            return
        if actor_id is None:
            actor_id = message.from_user.id
        try:
            if service_id:
                service = service_by_id(registry, service_id)
                if service is None:
                    raise ValueError("Служение не найдено.")
                if operation == "service_settings":
                    chat_title = await group_chat_title(
                        service["group_chat_id"]
                    )
                    keyboard = settings_keyboard(service_id, actor_id)
                    await run_registry_command(
                        message,
                        lambda user_id: settings_text(
                            registry, user_id, service_id, chat_title
                        ),
                        reply_markup=keyboard,
                        success_icon=None,
                        keyboard_limit=keyboard_size(keyboard),
                    )
                    return
                if operation in ADMIN_OPERATIONS:
                    if not registry.is_super_admin(actor_id):
                        raise PermissionError(
                            "Назначать и снимать администраторов может только "
                            "супер-администратор."
                        )
                elif not is_service_admin(actor_id, service_id):
                    raise PermissionError(
                        "Настройки доступны только администратору этого служения."
                    )
                if operation == "service_admin_add":
                    await offer_user_selection(
                        message, actor_id, service_id
                    )
                    return
                await ask_for_service_parameters(
                    message,
                    actor_id,
                    operation,
                    service_id,
                    service_label(service),
                )
                return

            is_super_admin = registry.is_super_admin(actor_id)
            services = registry.list_services()
            if operation in ADMIN_OPERATIONS:
                if not is_super_admin:
                    raise PermissionError(
                        "Назначать и снимать администраторов может только "
                        "супер-администратор."
                    )
            else:
                # Остальное — админу своих служений.
                services = [
                    item for item in services
                    if is_super_admin
                    or registry.is_service_admin(actor_id, item["id"])
                ]
                if not services:
                    raise PermissionError(
                        "У вас нет прав администратора ни для одного служения."
                    )
        except (PermissionError, ValueError) as exc:
            await _send_dialog(message, _status_text(str(exc), "❌"), parse_mode=None)
            return
        except sqlite3.Error:
            debug.exception("Не удалось загрузить список служений")
            await _send_dialog(message, "Не удалось загрузить список служений.", parse_mode=None
            )
            return

        keyboard = service_selection_keyboard(services, operation)
        descriptions = {
            "service_settings": "ℹ️ Выберите служение для просмотра настроек.",
            "service_admin_add": "ℹ️ Выберите служение для назначения администратора.",
            "service_admin_remove": "ℹ️ Выберите служение для снятия администратора.",
            "service_chat": "ℹ️ Выберите служение для настройки группового чата.",
            "service_contact": "ℹ️ Выберите служение для привязки ответственных.",
            "service_sync": "ℹ️ Выберите служение для сверки привязок.",
            "service_notify": "ℹ️ Выберите служение для настройки уведомлений.",
        }
        await _send_dialog(message, descriptions[operation],
            reply_markup=keyboard,
            parse_mode=None,
            _dialog_keyboard_limit=8,
        )

    async def ask_for_service_parameters(
        message: Message,
        actor_id: int,
        operation: str,
        service_id: str,
        label: str,
        *,
        notify_task: str | None = None,
        notify_scoped: bool = False,
        notify_no_tasks: bool = False,
        open_reminders: bool = False,
    ) -> None:
        if operation == "service_chat":
            pending_inputs[actor_id] = {
                "operation": "service_chat",
                "service_id": service_id,
                "step": "await_chat",
            }
            request_id = new_share_request_id()
            pending_shares[request_id] = (
                actor_id, "service_chat", service_id, None, None
            )
            keyboard = ReplyKeyboardMarkup(
                keyboard=[
                    [
                        KeyboardButton(
                            text="📡 Выбрать групповой чат",
                            request_chat=KeyboardButtonRequestChat(
                                request_id=request_id,
                                chat_is_channel=False,
                                bot_is_member=True,
                                request_title=True,
                                request_username=True,
                            ),
                        )
                    ],
                    [KeyboardButton(text=CONTACT_BACK_TEXT)],
                ],
                resize_keyboard=True,
                one_time_keyboard=True,
            )
            await _send_dialog(message, f"ℹ️ {label}: нажмите кнопку и выберите группу, в которой "
                "бот уже состоит. После выбора служение будет включено. "
                f"«{CONTACT_BACK_TEXT}» — вернуться к настройке чата.",
                reply_markup=keyboard,
                parse_mode=None,
            )
            return

        registry = service_registry()
        if registry is None:
            await _send_dialog(message, "Реестр настроек служений недоступен.", parse_mode=None
            )
            return
        prompts = {
            "service_admin_add": (
                f"ℹ️ {label}: выберите пользователя кнопкой Telegram."
            ),
            "service_admin_remove": (
                f"ℹ️ {label}: введите Telegram ID администратора, которого "
                "нужно снять."
            ),
            "service_chat": (
                f"ℹ️ {label}: выберите групповой чат кнопкой Telegram."
            ),
            "service_contact": f"ℹ️ {label}: выберите или добавьте задачу.",
            "service_notify": (
                f"ℹ️ {label}: за сколько дней до воскресенья отправлять "
                "напоминание? Введите число от 0 до 6."
            ),
        }
        if operation == "service_notify":
            service = service_by_id(registry, service_id)
            if service is None:
                raise ValueError("Служение не найдено.")
            pending_inputs[actor_id] = {
                "operation": operation,
                "service_id": service_id,
                "session_id": secrets.token_urlsafe(9),
                "step": "menu",
                "notify_scoped": notify_scoped,
                "notify_task": notify_task,
                "notify_no_tasks": notify_no_tasks,
                "reminder_days": service["reminder_days"],
                "reminder_time": service["reminder_time"],
                "group_template": service["group_template"],
                "admin_report_template": service.get("admin_report_template"),
                "personal_templates": dict(
                    service["personal_templates"] if service else {}
                ),
            }
            if open_reminders:
                await show_reminder_rules_menu(message, actor_id, service_id)
            else:
                await show_notification_settings_menu(
                    message, actor_id, service_id
                )
            return
        elif operation == "service_admin_remove":
            pending_inputs[actor_id] = {
                "operation": operation,
                "service_id": service_id,
                "step": "text",
            }
        elif operation in {"service_contact", "service_sync"}:
            pending_inputs[actor_id] = {
                "operation": "service_contact",
                "service_id": service_id,
                "session_id": secrets.token_urlsafe(9),
                "step": "choose_task",
            }
            if operation == "service_sync":
                await _send_dialog(message, f"⏳ {label}: загружаю и проверяю актуальный график...",
                    parse_mode=None,
                )
            await offer_contact_overview(
                message, actor_id, service_id,
                force_reload=operation == "service_sync",
                members_first=operation == "service_contact",
            )
            return
        await _send_dialog(message, prompts[operation], parse_mode=None)

    async def run_registry_command(
        message: Message,
        operation,
        reply_markup: InlineKeyboardMarkup | None = None,
        success_icon: str | None = "✅",
        keyboard_limit: int = 6,
    ) -> None:
        if (
            not message.from_user
            or message.chat.type != ChatType.PRIVATE
        ):
            return
        await clean_and_clear_pending_actor(message.from_user.id)
        if service_registry() is None:
            await _send_dialog(message, "Реестр настроек служений недоступен. "
                "Попробуйте позже или сообщите супер-администратору.",
                parse_mode=None,
            )
            return
        try:
            response = operation(message.from_user.id)
        except (PermissionError, ValueError) as exc:
            await _send_dialog(message, _status_text(str(exc), "❌"), parse_mode=None)
            return
        except sqlite3.Error:
            debug.exception("Ошибка выполнения команды управления служениями")
            await _send_dialog(message, "Не удалось выполнить команду из-за ошибки базы настроек.",
                parse_mode=None,
            )
            return
        formatted_response = (
            _status_text(response, success_icon)
            if success_icon is not None
            else response
        )
        chunks = _telegram_text_chunks(formatted_response)
        for index, chunk in enumerate(chunks):
            await _send_dialog(message, chunk,
                parse_mode=None,
                reply_markup=reply_markup if index == len(chunks) - 1 else None,
                _dialog_keyboard_limit=keyboard_limit,
            )

    def settings_keyboard(
        service_id: str, actor_id: int
    ) -> InlineKeyboardMarkup:
        # Групповой чат (с проверкой доставки) и админы — в «⚙️ Настройки».
        items = [
            ("👥 Ответственные", "service_contact"),
        ]
        rows = [
            [
                InlineKeyboardButton(
                    text=label,
                    callback_data=f"service:{operation}:{service_id}",
                )
            ]
            for label, operation in items
        ]
        rows.append(
            [
                InlineKeyboardButton(
                    text="📅 График",
                    callback_data=f"rdy:month:{service_id}",
                )
            ]
        )
        rows.append(
            [
                InlineKeyboardButton(
                    text="🔔 Напомнить",
                    callback_data=f"service:service_reminders:{service_id}",
                )
            ]
        )
        rows.append(
            [
                InlineKeyboardButton(
                    text="📋 Задачи служения",
                    callback_data=f"svc_tasks:open:{service_id}",
                )
            ]
        )
        rows.append([
            InlineKeyboardButton(
                text="⚙️ Настройки",
                callback_data=f"service:service_setup:{service_id}",
            ),
            InlineKeyboardButton(
                text="❓ Помощь",
                callback_data=f"service:service_help:{service_id}",
            ),
        ])
        rows.append(
            [
                InlineKeyboardButton(
                    text="⬅️ К списку служений",
                    callback_data="service:back_to_services:list",
                )
            ]
        )
        return InlineKeyboardMarkup(inline_keyboard=rows)

    def service_setup_keyboard(
        service_id: str, actor_id: int
    ) -> InlineKeyboardMarkup:
        """«⚙️ Настройки» служения: групповой чат, у супер-администратора —
        назначение и снятие админов."""
        registry = service_registry()
        items = [("📡 Групповой чат", "service_chat")]
        if service_id == "assembly":
            items.append(("📝 Заметки к собранию", "event_notes"))
        if registry and registry.is_super_admin(actor_id):
            items += [
                ("👤 Назначить админа", "service_admin_add"),
                ("➖ Снять админа", "service_admin_remove"),
            ]
        rows = [
            [InlineKeyboardButton(
                text=label,
                callback_data=(
                    "evn:home" if operation == "event_notes"
                    else f"service:{operation}:{service_id}"
                ),
            )]
            for label, operation in items
        ]
        if registry and registry.is_super_admin(actor_id):
            rows.append([InlineKeyboardButton(
                text="⛔ Отключить служение",
                callback_data=f"service:service_chat_disable:{service_id}",
            )])
        rows.append([InlineKeyboardButton(
            text="⬅️ К карточке служения",
            callback_data=f"service:service_settings:{service_id}",
        )])
        return InlineKeyboardMarkup(inline_keyboard=rows)

    async def show_service_setup(
        message: Message, actor_id: int, service: dict, notice: str = "",
    ) -> None:
        registry = service_registry()
        pending = {"operation": "service_setup", "service_id": service["id"]}
        pending_inputs[actor_id] = pending
        chat_label = "не подключён"
        chat_id = service.get("group_chat_id")
        if chat_id is not None:
            chat_label = "подключён"
            get_chat = getattr(context.bot, "get_chat", None)
            if get_chat is not None:
                try:
                    chat = await get_chat(chat_id)
                except (TelegramAPIError, OSError, asyncio.TimeoutError):
                    debug.exception("Не удалось прочитать название чата служения")
                    chat_label = "подключён; название сейчас недоступно"
                else:
                    title = getattr(chat, "title", None) or "Групповой чат"
                    username = getattr(chat, "username", None)
                    invite = getattr(chat, "invite_link", None)
                    url = f"https://t.me/{username}" if username else invite
                    chat_label = telegram_link(url, title) if isinstance(url, str) else title
            if pending_inputs.get(actor_id) is not pending:
                debug.debug("Экран настроек служения сменился во время чтения чата")
                return
            if not is_service_admin(actor_id, service["id"]):
                raise PermissionError("Недостаточно прав.")
        admins = registry.list_service_admin_details(service["id"])
        admin_lines = [
            user_link(item["telegram_id"], (
                f"@{item['username']}" if item["username"] else f"Администратор {index}"
            ))
            for index, item in enumerate(admins, 1)
        ]
        await _send_dialog(
            message,
            f"⚙️ {service_label(service)} › Настройки\n\n"
            f"📡 Чат: {chat_label}\n"
            "👤 Администраторы: " + (", ".join(admin_lines) or "не назначены")
            + ("\n⛔ Служение выключено." if not service["enabled"] else "")
            + (f"\n\n{notice}" if notice else ""),
            reply_markup=service_setup_keyboard(service["id"], actor_id),
            parse_mode=None, _dialog_text_limit=2000,
        )

    async def ask_service_disable(
        message: Message, actor_id: int, service_id: str,
        *, group_chat_id: int | None = None,
    ) -> None:
        registry = service_registry()
        if not registry.is_super_admin(actor_id):
            raise PermissionError("Отключить служение может только суперадминистратор.")
        service = service_by_id(registry, service_id)
        if service is None:
            raise ValueError("Служение не найдено.")
        token = secrets.token_urlsafe(8)
        pending_inputs[actor_id] = {
            "operation": "service_disable", "service_id": service_id, "token": token,
            "group_chat_id": group_chat_id,
        }
        await _send_dialog(
            message,
            f"⛔ {service_label(service)} › Настройки › Отключение\n\n"
            "Отключить служение? Напоминания будут остановлены. "
            "Графики и привязки сохранятся. "
            + ("Чат будет изменён на указанный в команде." if group_chat_id is not None
               else "Групповой чат не изменится."),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(
                    text="⛔ Да, отключить", callback_data=f"disable:{service_id}:{token}",
                )],
                [InlineKeyboardButton(
                    text="⬅️ К настройкам", callback_data=f"service:service_setup:{service_id}",
                )],
            ]),
            parse_mode=None, _dialog_text_limit=1000,
        )

    @admin_router.callback_query(F.data.startswith("disable:"))
    async def on_service_disable(callback: CallbackQuery) -> None:
        if not callback.from_user or not callback.message or (
            callback.message.chat.type != ChatType.PRIVATE
        ):
            await _answer_callback(callback)
            return
        parts = (callback.data or "").split(":")
        actor_id = callback.from_user.id
        pending = pending_inputs.get(actor_id)
        registry = service_registry()
        if registry is None:
            await _answer_callback(callback, "❌ Реестр служений недоступен.")
            return
        try:
            if not registry.is_super_admin(actor_id):
                raise PermissionError("Недостаточно прав.")
            if len(parts) != 3 or pending is None or (
                pending.get("operation"), pending.get("service_id"), pending.get("token")
            ) != ("service_disable", parts[1], parts[2]):
                await _answer_callback(callback, "⚠️ Подтверждение устарело.")
                return
            if pending.get("group_chat_id") is not None:
                registry.set_group_chat(
                    actor_id, parts[1], pending["group_chat_id"], enabled=False,
                )
                if parts[1] == "assembly":
                    context.group_chat_id = pending["group_chat_id"]
            else:
                registry.set_service_enabled(actor_id, parts[1], False)
            pending["operation"] = "service_disabled"
            await clean_and_clear_pending_actor(actor_id)
            await show_service_setup(
                callback.message, actor_id, registry.get_service(parts[1]),
                "✅ Служение отключено. Сохранённые данные не изменены.",
            )
        except (ValueError, PermissionError) as exc:
            await _answer_callback(callback, f"❌ {exc}")
            return
        except sqlite3.Error:
            debug.exception("Ошибка отключения служения или обновления настроек")
            await _answer_callback(callback, "❌ Ошибка базы данных. Откройте настройки заново.")
            return
        await _answer_callback(callback)

    # «❓ Помощь» карточки служения: что делает каждая кнопка.
    SERVICE_HELP = (
        "❓ Кнопки карточки служения\n\n"
        "👥 Ответственные — список братьев и сестер служения:\n"
        "Telegram, задачи, свой день, сколько раз в месяц;\n\n"
        "«🗓 По датам графика» — кто служит в ближайшие даты.\n\n"
        "📅 График — текущий месяц: даты, задачи и люди; замены, загрузка "
        "с диска с подтверждением.\n\n"
        "За кнопкой «🗳 Опрос» — составить, править и утвердить график, "
        "настройки опроса и тексты для сообщений в чат.\n\n"
        "🔔 Напомнить — выбрать общие напоминания служения или напоминания "
        "конкретной задачи.\n\n"
        "📋 Задачи служения — задачи, сколько ответственных требуется, дни задач, "
        "задачи «в одну неделю» и настройки напоминаний конкретной задачи.\n\n"
        "⚙️ Настройки — групповой чат и проверка доставки; у "
        "супер-администратора — назначить и снять админа, отключить служение.\n\n"
        "⬅️ К списку служений — выбрать другое служение."
    )

    def plan_version(plan: dict | None) -> str:
        """Версия графика месяца: рабочая и утверждённая копии."""
        return ServiceRegistry.schedule_plan_version(plan)

    def excel_months() -> list[tuple[int, int]]:
        """Месяцы экрана «📊 График Excel»: текущий и следующий."""
        today = datetime.now(context.timezone).date()
        following = (today.replace(day=1) + timedelta(days=32)).replace(day=1)
        return [(today.year, today.month), (following.year, following.month)]

    def plan_source_line(plan: dict | None) -> str:
        """Откуда напоминания месяца (решение 15)."""
        if plan is None:
            return (
                "⚪ график в БД бота не утверждён — напоминания приостановлены"
            )
        if plan["approved"] is None:
            return "📝 черновик не утверждён — напоминания приостановлены"
        when = readiness.local_text(plan["approved_at"], context.timezone)
        if plan["source"] == "excel":
            return (
                f"📥 график из Excel утверждён {when} — напоминания "
                "по графику в боте"
            )
        return f"✅ по опросу, утверждён {when} — напоминания по графику бота"

    async def show_excel_schedule(
        message: Message, actor_id: int, service: dict,
        notice: str | None = None,
    ) -> None:
        """«📊 График Excel» (решение 15): откуда график месяцев, загрузка
        графика из Excel в бот, сверка «кто не привязан»."""
        registry = service_registry()
        service_id = service["id"]
        lines = [f"📥 {service_label(service)} › График › Загрузить с диска", ""]
        rows = []
        for year, month in excel_months():
            plan = registry.get_schedule_plan(service_id, f"{year:04d}-{month:02d}")
            title = month_title(year, month)
            lines.append(f"🗓 {title}: {plan_source_line(plan)}")
            rows.append([InlineKeyboardButton(
                text=f"📥 Загрузить: {title}"[:60],
                callback_data=f"xls:load:{service_id}:{year:04d}{month:02d}",
            )])
        lines += [
            "",
            "ℹ️ График месяца готовится в Excel или опросом в боте. "
            "«📥 Загрузить» — бот прочитает лист месяца и покажет график; "
            "после «✅ Подтвердить» он станет итоговым графиком в боте: "
            "по нему напоминания и замены, править — в «📅 График». Вернуть "
            "график по опросу — «🔄 Пересчитать» там же и «✅ Утвердить».",
        ]
        if notice:
            lines = [notice, ""] + lines
        rows.append([InlineKeyboardButton(
            text="🔄 Сверить: кто не привязан",
            callback_data=f"service:service_sync:{service_id}",
        )])
        rows.append([InlineKeyboardButton(
            text="⬅️ К графику",
            callback_data=f"rdy:month:{service_id}",
        )])
        await _send_dialog(
            message, "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
            _dialog_keyboard_limit=sum(len(row) for row in rows),
            parse_mode=None,
        )

    def keyboard_size(markup: InlineKeyboardMarkup) -> int:
        return sum(len(row) for row in markup.inline_keyboard)

    async def send_service_card(
        message: Message, actor_id: int, service: dict
    ) -> None:
        response = settings_text(
            service_registry(),
            actor_id,
            service["id"],
            await group_chat_title(service["group_chat_id"]),
        )
        chunks = _telegram_text_chunks(response)
        keyboard = settings_keyboard(service["id"], actor_id)
        sent_ids: list[int] = []
        key = (actor_id, service["id"])
        chat_id = getattr(message.chat, "id", actor_id)
        try:
            for index, chunk in enumerate(chunks):
                await _send_dialog(message, chunk,
                    parse_mode=None,
                    reply_markup=(
                        keyboard if index == len(chunks) - 1 else None
                    ),
                    _dialog_keyboard_limit=keyboard_size(keyboard),
                    _dialog_sent_ids=sent_ids,
                )
        except Exception:
            # Недосланные куски удалятся вместе со старой карточкой.
            previous = last_service_cards.get(key)
            if sent_ids and previous is not None and previous[0] == chat_id:
                previous[1].extend(sent_ids)
            elif sent_ids:
                last_service_cards[key] = (chat_id, sent_ids)
            raise
        # Последняя карточка служения: её заменяет обновлённая после /here
        # в группе.
        last_service_cards[key] = (chat_id, sent_ids)

    def _task_button(
        actor_id: int,
        service_id: str,
        task: str,
        *,
        operation: str,
        task_choices: dict[str, tuple[int, str, str, str | None]],
    ) -> InlineKeyboardButton:
        token = secrets.token_urlsafe(8)
        task_choices[token] = (actor_id, operation, service_id, task)
        return InlineKeyboardButton(
            text=(
                f"{task_emoji(task)} {task}"
                if len(task) <= 44
                else f"{task_emoji(task)} {task[:41]}..."
            ),
            callback_data=f"task:{operation}:{token}",
        )

    def contact_navigation_callback(
        actor_id: int, service_id: str, action: str
    ) -> str:
        token = secrets.token_urlsafe(8)
        pending_contact_navigation[token] = (actor_id, service_id, action)
        return f"contact_nav:{token}"

    def contact_navigation_button(
        actor_id: int, service_id: str, action: str, text: str | None = None,
    ) -> InlineKeyboardButton:
        labels = {
            "tasks": "⬅️ К задачам",
            "services": "⬅️ К служениям",
            "finish": "✅ Завершить привязку",
            "edit": "👥 К списку ответственных",
            "notify": "🔔 Напоминания",
            "card": "⬅️ К карточке служения",
            "overview": "⬅️ К обзору",
        }
        label = text or labels.get(action) or labels[action.partition(":")[0]]
        return InlineKeyboardButton(
            text=label,
            callback_data=contact_navigation_callback(
                actor_id, service_id, action
            ),
        )

    async def send_task_choices_page(
        message: Message,
        actor_id: int,
        service_id: str,
        operation: str,
        page: int = 0,
    ) -> None:
        pending = pending_inputs.get(actor_id)
        if (
            pending is None
            or pending.get("operation") != operation
            or pending.get("service_id") != service_id
        ):
            await _send_dialog(message, "Выбор задач устарел. Запустите команду заново.",
                parse_mode=None,
            )
            return

        tasks = pending.get("schedule_tasks", [])
        if operation == "service_contact":
            tasks = [
                task for task in tasks
                if not task_is_informational(
                    pending.get("schedule_assignments", {}).get(task, [])
                )
            ]
        tasks = sort_tasks_for_display(tasks, service_id)
        page_size = 3
        page_count = max(1, (len(tasks) + page_size - 1) // page_size)
        page = min(max(page, 0), page_count - 1)
        page_tasks = tasks[page * page_size:(page + 1) * page_size]
        registry = service_registry()
        service = (
            service_by_id(registry, service_id) if registry is not None else None
        )
        service_context = (
            service_label(service) if service is not None else service_id.upper()
        )
        rows = []
        if operation == "service_contact":
            rows.extend(
                [
                    _task_button(
                        actor_id,
                        service_id,
                        task,
                        operation=operation,
                        task_choices=pending_task_choices,
                    )
                ]
                for task in page_tasks
            )
            rows.append([
                contact_navigation_button(
                    actor_id,
                    service_id,
                    (
                        f"overview:{pending.get('overview_index', 0)}"
                        if "overview_events" in pending
                        else "services"
                    ),
                )
            ])
        else:
            for task in page_tasks:
                token = secrets.token_urlsafe(8)
                pending_notify_task_choices[token] = (
                    actor_id, service_id, task
                )
                label = task if len(task) <= 48 else task[:45] + "..."
                if task_is_informational(
                    pending.get("schedule_assignments", {}).get(task, [])
                ):
                    label = f"ℹ️ {label} · инфо"
                rows.append([
                    InlineKeyboardButton(
                        text=(
                            label
                            if label.startswith("ℹ️ ")
                            else f"{task_emoji(task)} {label}"
                        ),
                        callback_data=f"notify_task:{token}",
                    )
                ])
            token = secrets.token_urlsafe(8)
            pending_notify_task_choices[token] = (actor_id, service_id, None)
            rows.append([
                InlineKeyboardButton(
                    text="⬅️ К настройкам",
                    callback_data=f"notify_task:{token}",
                )
            ])

        navigation = []
        if page > 0:
            token = secrets.token_urlsafe(8)
            pending_task_pages[token] = (
                actor_id, operation, service_id, page - 1
            )
            navigation.append(
                InlineKeyboardButton(
                    text="⬅️ Назад", callback_data=f"task_page:{token}"
                )
            )
        if page + 1 < page_count:
            token = secrets.token_urlsafe(8)
            pending_task_pages[token] = (
                actor_id, operation, service_id, page + 1
            )
            navigation.append(
                InlineKeyboardButton(
                    text="➡️ Далее", callback_data=f"task_page:{token}"
                )
            )
        if navigation:
            rows.append(navigation)

        if operation == "service_contact":
            prompt = (
                f"🔗 Привязка ответственных · "
                f"{service_context}"
                f"\n📊 Задачи {page * page_size + 1}-"
                f"{min((page + 1) * page_size, len(tasks))} из {len(tasks)}"
                "\nℹ️ Выберите задачу из графика."
                if tasks
                else "🔗 Привязка ответственных\n"
                "⚪ В графике нет задач с ответственными."
            )
        else:
            prompt = (
                "📝 Личный шаблон\n"
                f"📊 Задачи {page * page_size + 1}-"
                f"{min((page + 1) * page_size, len(tasks))} из {len(tasks)}"
                "\nℹ️ Выберите задачу из графика."
                if tasks
                else "📝 Личный шаблон\n"
                "⚪ В графике нет задач с ответственными."
            )
        await _send_dialog(message, prompt,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
            parse_mode=None,
        )

    async def offer_contact_name_choices(
        message: Message,
        actor_id: int,
        service_id: str,
        task: str,
        notice: str | None = None,
    ) -> None:
        pending = pending_inputs.get(actor_id)
        if (
            pending is None
            or pending.get("operation") != "service_contact"
            or pending.get("service_id") != service_id
            or task not in pending.get("schedule_assignments", {})
        ):
            await _send_dialog(message, "Задача отсутствует в актуальном файле расписания.",
                parse_mode=None,
            )
            return

        if task_is_informational(
            pending["schedule_assignments"].get(task, [])
        ):
            debug.debug(
                "Отклонён выбор информационной записи для привязки: %s",
                task,
            )
            await _send_dialog(
                message,
                "Эта запись — не задача, а пометка о дне служения.",
                parse_mode=None,
            )
            return
        pending["step"] = "choose_contact_person"
        pending["active_task"] = task
        pending.pop("contact_mode", None)
        for token, choice in list(pending_contact_person_choices.items()):
            if choice[:3] == (actor_id, service_id, task):
                pending_contact_person_choices.pop(token, None)
        for token, choice in list(pending_contact_navigation.items()):
            if choice[:2] == (actor_id, service_id):
                pending_contact_navigation.pop(token, None)
        registered = pending.get("registered_contacts", {}).get(task, [])
        contacts_by_name = {
            contact["name"]: contact for contact in registered
        }
        rows = []
        bound_count = 0
        names = task_assignment_names(
            pending["schedule_assignments"][task]
        )
        for person_name in names:
            contact = contacts_by_name.get(person_name)
            if contact:
                bound_count += 1
                status = "✅"
                suffix = (
                    f" · @{contact['username']}"
                    if contact["username"]
                    else " · привязан"
                )
            else:
                status = "⚪"
                suffix = " · не привязан"
            token = secrets.token_urlsafe(8)
            pending_contact_person_choices[token] = (
                actor_id, service_id, task, person_name
            )
            label = f"{status} {person_name}{suffix}"
            if len(label) > 60:
                label = label[:57] + "..."
            rows.append([
                InlineKeyboardButton(
                    text=label,
                    callback_data=f"contact_person:{token}",
                )
            ])
        rows.append([
            contact_navigation_button(actor_id, service_id, "tasks")
        ])
        text = (
            f"👥 Ответственные · {task_emoji(task)} {task}\n"
            f"📊 Привязано {bound_count} из {len(names)} участников\n"
            "ℹ️ Выберите имя для привязки или изменения."
        )
        if notice:
            text = notice + "\n\n" + text
        await _send_dialog(message, text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
            parse_mode=None,
        )

    def contact_members(
        registry, pending: dict, service_id: str
    ) -> list[dict]:
        """Состав служения для привязки: «Списки», привязанные и люди графика.

        Для каждого человека — задачи в графике месяца и статус привязки.
        """
        schedule_tasks: dict[str, list[str]] = {}
        # Убранные из состава в этой сессии не возвращаются из графика.
        removed = set(pending.get("removed_members", ()))
        for task, task_info in pending.get("schedule_assignments", {}).items():
            if task_is_informational(task_info):
                continue
            for name in task_assignment_names(task_info):
                if name not in removed:
                    schedule_tasks.setdefault(name, []).append(task)
        bound = {
            contact["name"]: contact
            for contacts in pending.get("registered_contacts", {}).values()
            for contact in contacts
        }
        people: dict[str, dict] = {}
        for member in registry.list_service_members(service_id):
            people[member["name"]] = {
                "name": member["name"],
                "in_list": member["in_list"],
                "telegram_id": member["telegram_id"],
                "username": member["username"],
                "source": member["source"],
            }
        for name in schedule_tasks:
            people.setdefault(
                name,
                {
                    "name": name, "in_list": False,
                    "telegram_id": None, "username": "", "source": "",
                },
            )
        for person in people.values():
            contact = bound.get(person["name"])
            if not person["telegram_id"] and contact:
                person["telegram_id"] = contact["telegram_id"]
                person["username"] = contact.get("username") or ""
            person["schedule_tasks"] = sort_tasks_for_display(
                schedule_tasks.get(person["name"], []), service_id,
            )
        return list(people.values())

    async def send_member_choices(
        message: Message,
        actor_id: int,
        service_id: str,
        page: int = 0,
        notice: str | None = None,
    ) -> None:
        pending = pending_inputs.get(actor_id)
        registry = service_registry()
        service = service_by_id(registry, service_id) if registry else None
        if (
            pending is None
            or pending.get("operation") != "service_contact"
            or pending.get("service_id") != service_id
            or service is None
        ):
            await _send_dialog(
                message, "Привязка устарела. Откройте её заново.",
                parse_mode=None,
            )
            return
        people = contact_members(registry, pending, service_id)
        has_roster = any(person["in_list"] for person in people)
        page_count = max(1, (len(people) + MEMBER_PAGE_SIZE - 1) // MEMBER_PAGE_SIZE)
        page = min(max(page, 0), page_count - 1)
        pending.update(
            {
                "step": "choose_member",
                "contact_mode": "person",
                "member_page": page,
                "member_names": [person["name"] for person in people],
            }
        )
        pending.pop("active_task", None)
        pending.pop("active_contact_name", None)
        for token, choice in list(pending_contact_person_choices.items()):
            if choice[:2] == (actor_id, service_id):
                pending_contact_person_choices.pop(token, None)
        for token, choice in list(pending_contact_navigation.items()):
            if choice[:2] == (actor_id, service_id):
                pending_contact_navigation.pop(token, None)
        rows = []
        for person in people[page * MEMBER_PAGE_SIZE:(page + 1) * MEMBER_PAGE_SIZE]:
            added = person["source"] == "manual" and not person["in_list"]
            if person["telegram_id"]:
                label = f"✅ {'🆕 ' if added else ''}{person['name']}" + (
                    f" · @{person['username']}" if person["username"] else ""
                )
            elif added:
                label = f"⚪ 🆕 {person['name']} · не привязан"
            elif has_roster and not person["in_list"]:
                label = f"⚠️ {person['name']} · нет в списке"
            else:
                label = f"⚪ {person['name']} · не привязан"
            if len(label) > 60:
                label = label[:57] + "..."
            token = secrets.token_urlsafe(8)
            pending_contact_person_choices[token] = (
                actor_id, service_id, None, person["name"]
            )
            rows.append([
                InlineKeyboardButton(
                    text=label, callback_data=f"contact_person:{token}"
                )
            ])
        navigation = [
            InlineKeyboardButton(
                text=text,
                callback_data=contact_navigation_callback(
                    actor_id, service_id, f"members:{target}"
                ),
            )
            for target, text in (
                (page - 1, "⬅️ Назад"), (page + 1, "Далее ➡️"),
            )
            if 0 <= target < page_count
        ]
        if navigation:
            rows.append(navigation)
        rows.append([
            InlineKeyboardButton(
                text="➕ Добавить участника",
                callback_data=contact_navigation_callback(
                    actor_id, service_id, "add_member"
                ),
            )
        ])
        if "overview_events" in pending:
            # «👥 Ответственные» открывается списком людей; кто служит по
            # датам графика — отдельной кнопкой.
            rows.append([
                contact_navigation_button(
                    actor_id, service_id,
                    f"overview:{pending.get('overview_index', 0)}",
                    "🗓 По датам графика",
                ),
                contact_navigation_button(actor_id, service_id, "card"),
            ])
        else:
            rows.append([
                contact_navigation_button(actor_id, service_id, "services")
            ])
        linked = sum(1 for person in people if person["telegram_id"])
        lines = [
            f"{service_label(service)} › 👥 Состав",
            f"📊 Привязано {linked} из {len(people)}"
            + (f" · стр. {page + 1} из {page_count}" if page_count > 1 else ""),
            "ℹ️ Выберите человека: Telegram, задачи или удаление из "
            "состава. Telegram привязывается сразу ко всем его задачам.",
        ]
        if not people:
            lines[1] = "⚪ В «Списках» и графике пока никого нет."
        if any(
            person["source"] == "manual" and not person["in_list"]
            for person in people
        ):
            lines.append("🆕 — добавлен(а) в боте (например, из опроса), в конце списка.")
        if has_roster and any(
            not person["in_list"] and person["source"] != "manual"
            for person in people
        ):
            lines.append("⚠️ нет в списке — человек есть в графике, но не в «Списках».")
        text = "\n".join(lines)
        if notice:
            text = notice + "\n\n" + text
        await _send_dialog(
            message,
            text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
            parse_mode=None,
            _dialog_keyboard_limit=MEMBER_PAGE_SIZE + 4,
            _dialog_keep_rows=True,
        )

    def refresh_registered_contacts(actor_id: int, service_id: str) -> None:
        """Привязки сессии «Состава» — заново из базы после правки."""
        pending = pending_inputs.get(actor_id)
        if pending is None:
            return
        try:
            contacts = service_registry().list_responsible_contacts(
                actor_id, service_id
            )
        except (PermissionError, ValueError, sqlite3.Error):
            debug.exception("Не удалось обновить список привязок")
            return
        pending["registered_contacts"] = {}
        for contact in contacts:
            pending["registered_contacts"].setdefault(
                contact["task"], []
            ).append(contact)

    def member_card_state(actor_id: int, service_id: str, token: str) -> dict | None:
        """Карточка человека в «Составе» — только из своей сессии привязки."""
        pending = pending_inputs.get(actor_id)
        card = (pending or {}).get("member_card")
        if (
            pending is None
            or pending.get("operation") != "service_contact"
            or pending.get("service_id") != service_id
            or card is None
            or card.get("token") != token
        ):
            return None
        return card

    MEMBER_DAYS = (-6, -5, -4, -3, -2, -1, 0, 1, 2)

    def member_day_label(offset: int) -> str:
        weekday = schedule_plan.WEEKDAYS[(6 + offset) % 7]
        return f"{weekday} (вс {offset:+d})"

    def reminder_anchor_text(reminder: dict) -> str:
        """От какого дня считается срок напоминания (решение 17, 5а)."""
        offset = reminder_day_offset(reminder)
        if offset:
            return f"дня задачи — {member_day_label(offset)}"
        return "воскресенья"

    # Лимит служений в месяц (решение 12): готовые диапазоны и «Вручную» —
    # минимум, затем максимум кнопками.
    LIMIT_PRESETS = ((0, 2), (1, 2), (1, 3))

    def month_limit_label(limit) -> str:
        if limit is None or limit == (0, None):
            return "без лимита"
        low, high = limit
        if high == 0:
            return "0 — не ставить"
        if high == low:
            return f"ровно {low}"
        return f"от {low}" if high is None else f"{low}–{high}"

    def service_month_limit(registry, service_id: str):
        """Лимит служения для всех; None — без лимита."""
        settings = registry.get_readiness_settings(service_id)
        if not settings["month_min"] and settings["month_max"] is None:
            return None
        return (settings["month_min"], settings["month_max"])

    def month_limit_value(value: str):
        """«n» — без лимита, «1_3» — диапазон, «2_x» — от 2."""
        if value == "n":
            return (0, None)
        low, _, high = value.partition("_")
        if not low.isdigit() or not (high == "x" or high.isdigit()):
            raise ValueError("Неизвестный лимит.")
        return (int(low), None if high == "x" else int(high))

    def month_limit_choices(value: str) -> tuple[str, list[tuple[str, str]]]:
        """Шаг выбора лимита: «m» — минимум, «m2» — максимум от 2."""
        if value == "m":
            return "Минимум служений в месяц:", [
                (str(low), f"m{low}") for low in range(0, 6)
            ]
        if value.startswith("m") and value[1:].isdigit():
            low = int(value[1:])
            return f"Минимум {low}. Максимум служений в месяц:", [
                ("0 — не ставить" if high == 0 else str(high), f"{low}_{high}")
                for high in range(low, 6)
            ] + [("без верхней границы", f"{low}_x")]
        return "", [("без лимита", "n")] + [
            (f"{low}–{high}", f"{low}_{high}") for low, high in LIMIT_PRESETS
        ] + [("✏️ Вручную", "m")]

    def member_button(text: str, action: str, service_id: str, token: str,
                      arg: str | None = None) -> InlineKeyboardButton:
        data = f"mbr:{action}:{service_id}:{token}" + (f".{arg}" if arg else "")
        return InlineKeyboardButton(text=text, callback_data=data)

    def member_week_label(
        registry: Any, service_id: str, own: list[str] | None,
        tasks: list[str],
    ) -> str:
        """Задачи человека «в одну неделю»: личные или как у служения."""
        if own is not None:
            if not own:
                return "не связывать"
            working = [task for task in own if task in tasks]
            return " + ".join(own) + (
                " (личное)" if len(working) > 1
                else " (личное, не действует — у человека нет двух из них)"
            )
        linked = [
            task["name"] for task in registry.list_service_tasks(service_id)
            if task["same_week"] and task["name"] in tasks
        ]
        return "как у служения — " + (
            " + ".join(linked) if len(linked) > 1 else "нет"
        )

    async def show_member_week(
        message: Message, actor_id: int, service_id: str,
        notice: str | None = None,
    ) -> None:
        """«🔗 В одну неделю» человека: какие его задачи он берёт вместе."""
        card = pending_inputs[actor_id]["member_card"]
        selected = card["week_sel"]
        token = card["token"]
        lines = [
            f"🔗 В одну неделю · {card['name']}",
            "",
            "Отметьте задачи, которые человек берёт вместе в одну неделю "
            "(звук в вс и спевка в ср) — хотя бы две. «↩️ Как у служения» — "
            "по отметкам «🔗» в задачах служения.",
        ]
        if notice:
            lines = [notice, ""] + lines
        buttons = [
            member_button(
                f"{'☑️' if task in selected else '⬜'} {task}"[:60],
                "wt", service_id, token, str(index),
            )
            for index, task in enumerate(card["tasks"])
        ]
        rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
        rows.append([member_button("✅ Сохранить", "ws", service_id, token)])
        rows.append([
            member_button("↩️ Как у служения", "wa", service_id, token),
            member_button("🚫 Не связывать", "wn", service_id, token),
        ])
        rows.append([member_button("⬅️ К человеку", "card", service_id, token)])
        await _send_dialog(
            message, "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
            _dialog_keep_rows=True,
            _dialog_keyboard_limit=sum(len(row) for row in rows),
            parse_mode=None,
        )

    async def show_member_card(
        message: Message, actor_id: int, service_id: str, name: str,
        notice: str | None = None,
    ) -> None:
        """Человек в составе: Telegram, задачи, удаление."""
        registry = service_registry()
        pending = pending_inputs[actor_id]
        service = service_by_id(registry, service_id)
        member = next(
            (item for item in registry.list_service_members(service_id)
             if item["name"] == name),
            None,
        )
        person = next(
            (item for item in registry.readiness_people(service_id)
             if item["name"] == name),
            None,
        )
        # «Списки» (или заданные в боте) и привязки к задачам.
        tasks = sort_tasks_for_display(dict.fromkeys(
            (member or {}).get("tasks", []) + (person or {}).get("tasks", [])
        ), service_id)
        token = secrets.token_urlsafe(6)
        pending["step"] = "member_card"
        pending["member_card"] = {"token": token, "name": name, "tasks": tasks}
        telegram_id = (person or member or {}).get("telegram_id")
        # Действующий личный день (у человека может быть несколько строк).
        day_offset = (person or member or {}).get("day_offset")
        username = (person or member or {}).get("username")
        if member is None:
            origin = "⚠️ нет в «Списках» (есть в графике или привязках)"
        elif member["in_list"]:
            origin = "«Списки» Excel"
        elif member["source"] == "manual":
            origin = "добавлен(а) в боте"
        else:
            origin = "⚠️ нет в «Списках»"
        lines = [
            f"👤 {name} · {service_label(service)}",
            "",
            "Telegram: " + (
                (f"@{username}" if username else f"ID {telegram_id}")
                if telegram_id else "⚪ не привязан"
            ) + (
                f"\n⏳ ждёт @{waiting} — привяжется, когда он(а) напишет боту"
                if (waiting := registry.username_binding(service_id, name))
                else ""
            ),
            f"Источник: {origin}",
            "Короткое имя: " + (
                registry.service_display_names(service_id).get(name) or "не задано"
            ),
            "Задачи: " + (", ".join(tasks) or "⚠️ нет — в график не попадает")
            + (" (заданы в боте)" if member and member["tasks_manual"] else ""),
            "📅 День служения: " + (
                member_day_label(day_offset)
                if day_offset is not None else "как у всех"
            ),
            "🔢 Служений в месяц: " + (
                month_limit_label(personal) + " (личный)"
                if (personal := (member or {}).get("month_limit")) is not None
                else "как у служения — " + month_limit_label(
                    service_month_limit(registry, service_id)
                )
            ),
        ]
        if len(tasks) > 1:
            lines.append("🔗 В одну неделю: " + member_week_label(
                registry, service_id, (member or {}).get("week_tasks"), tasks,
            ))
        scheduled = next(
            (item["schedule_tasks"]
             for item in contact_members(registry, pending, service_id)
             if item["name"] == name),
            [],
        )
        if scheduled:
            lines.append("В графике по датам: " + ", ".join(scheduled))
        if notice:
            lines = [notice, ""] + lines
        rows = [
            [member_button(
                "🔄 Изменить Telegram" if telegram_id else "🔗 Привязать Telegram",
                "bind", service_id, token,
            )],
            [member_button("✏️ Короткое имя", "short", service_id, token)],
        ]
        if registry.list_service_tasks(service_id):
            # У служения без задач все в нём — выбирать нечего.
            rows.append([member_button("📝 Задачи", "tasks", service_id, token)])
        if member is not None:
            rows.append([
                member_button("📅 Свой день", "day", service_id, token),
                member_button("🔢 В месяц", "lim", service_id, token),
            ])
            if len(tasks) > 1:
                rows.append([
                    member_button("🔗 В одну неделю", "wk", service_id, token)
                ])
        if member is not None or person is not None:
            # Только в графике — удалять из состава нечего.
            rows.append([
                member_button("🗑 Удалить из состава", "del", service_id, token)
            ])
        rows.append([member_button("⬅️ К составу", "back", service_id, token)])
        await _send_dialog(
            message, "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
            _dialog_keep_rows=True,
            parse_mode=None,
        )

    async def show_short_name_input(
        message: Message, service_id: str, card: dict, notice: str = "",
    ) -> None:
        await _send_dialog(
            message,
            f"✏️ Короткое имя › {card['name']}\n\n"
            f"Введите имя до {SHORT_NAME_LIMIT} символов, например «Василий» "
            "или «В. П.». Оно будет видно в графиках и напоминаниях. "
            "Полное имя для Excel и привязки не изменится."
            + (f"\n\n{notice}" if notice else ""),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [member_button(
                    "🧹 Убрать короткое имя", "shortreset", service_id, card["token"],
                )],
                [member_button("⬅️ К человеку", "card", service_id, card["token"])],
            ]),
            parse_mode=None, _dialog_text_limit=1000, _dialog_keep_rows=True,
        )

    async def show_member_tasks(
        message: Message, actor_id: int, service_id: str,
        notice: str | None = None,
    ) -> None:
        """Выбор задач человека (⬜/☑️)."""
        card = pending_inputs[actor_id]["member_card"]
        # Включённые задачи и выключенные, которые у человека уже есть
        # (иначе сохранение молча сняло бы их).
        options = card.setdefault("options", [
            task["name"]
            for task in service_registry().list_service_tasks(service_id)
            if task["enabled"] or task["name"] in card["tasks"]
        ])
        selected = card.setdefault("selected", list(card["tasks"]))
        token = card["token"]
        buttons = [
            member_button(
                f"{'☑️' if task in selected else '⬜'} {task}"[:60],
                "tt", service_id, token, str(index),
            )
            for index, task in enumerate(options)
        ]
        rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
        rows.append([member_button("✅ Сохранить", "ts", service_id, token)])
        rows.append([member_button("⬅️ К человеку", "card", service_id, token)])
        text = (
            f"📝 Задачи · {card['name']}\n\nОтметьте задачи, на которые человек "
            "ставится в график. Заданные здесь задачи «Списки» Excel больше "
            "не меняют."
        )
        if notice:
            text = f"{notice}\n\n{text}"
        await _send_dialog(
            message, text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
            _dialog_keep_rows=True,
            _dialog_keyboard_limit=sum(len(row) for row in rows),
            parse_mode=None,
        )

    @admin_router.callback_query(F.data.startswith("mbr:"))
    async def on_member_card(callback: CallbackQuery) -> None:
        if (
            not callback.from_user
            or not callback.message
            or callback.message.chat.type != ChatType.PRIVATE
        ):
            await _answer_callback(callback)
            return
        parts = callback.data.split(":")
        if len(parts) != 4:
            await _answer_callback(callback, "⚠️ Меню устарело.")
            return
        _, action, service_id, rest = parts
        token, _, arg = rest.partition(".")
        actor_id = callback.from_user.id
        card = member_card_state(actor_id, service_id, token)
        if card is None:
            await answer_quietly(callback, "⚠️ Этот экран уже устарел.")
            return
        registry = service_registry()
        if not is_service_admin(actor_id, service_id):
            await answer_quietly(callback, "❌ Недостаточно прав.")
            return
        name = card["name"]
        message = callback.message
        try:
            await clean_previous_menu(message, debug)
            if action == "card":
                await show_member_card(message, actor_id, service_id, name)
            elif action == "back":
                pending_inputs[actor_id].pop("member_card", None)
                await send_member_choices(
                    message, actor_id, service_id,
                    pending_inputs[actor_id].get("member_page", 0),
                )
            elif action == "bind":
                pending_inputs[actor_id].pop("member_card", None)
                await offer_contact_user_selection(
                    message, actor_id, service_id, None, name
                )
            elif action == "short":
                pending_inputs[actor_id]["step"] = "member_short_name"
                await show_short_name_input(message, service_id, card)
            elif action == "shortreset":
                registry.set_service_member_short_name(actor_id, service_id, name, "")
                await show_member_card(
                    message, actor_id, service_id, name, "✅ Короткое имя убрано",
                )
            elif action == "tasks":
                card.pop("selected", None)
                card.pop("options", None)
                await show_member_tasks(message, actor_id, service_id)
            elif action == "tt" and arg.isdigit() and "options" in card:
                index = int(arg)
                if index < len(card["options"]):
                    task = card["options"][index]
                    selected = card["selected"]
                    if task in selected:
                        selected.remove(task)
                    else:
                        selected.append(task)
                await show_member_tasks(message, actor_id, service_id)
            elif action == "ts" and "selected" in card:
                try:
                    tasks = registry.set_service_member_tasks(
                        actor_id, service_id, name,
                        [task for task in card["options"] if task in card["selected"]],
                    )
                except ValueError as exc:
                    await show_member_tasks(
                        message, actor_id, service_id,
                        _status_text(str(exc), "⚠️"),
                    )
                else:
                    refresh_registered_contacts(actor_id, service_id)
                    removed = pending_inputs[actor_id].get("removed_members", [])
                    if name in removed:
                        removed.remove(name)
                    await show_member_card(
                        message, actor_id, service_id, name,
                        (
                            "✅ Задачи сохранены: " + ", ".join(tasks)
                            if tasks else
                            "✅ Задачи сняты: в составе остаётся, в график не "
                            "попадает (из составленных графиков убран)"
                        ),
                    )
            elif action == "wk":
                member = next(
                    (item for item in registry.list_service_members(service_id)
                     if item["name"] == name),
                    None,
                )
                own = (member or {}).get("week_tasks")
                card["week_sel"] = [
                    task for task in card["tasks"]
                    if task in (own if own is not None else [
                        item["name"]
                        for item in registry.list_service_tasks(service_id)
                        if item["same_week"]
                    ])
                ]
                await show_member_week(message, actor_id, service_id)
            elif action == "wt" and arg.isdigit() and "week_sel" in card:
                index = int(arg)
                if index < len(card["tasks"]):
                    task = card["tasks"][index]
                    selected = card["week_sel"]
                    if task in selected:
                        selected.remove(task)
                    else:
                        selected.append(task)
                await show_member_week(message, actor_id, service_id)
            elif action in {"ws", "wa", "wn"} and "week_sel" in card:
                chosen = {
                    "ws": [t for t in card["tasks"] if t in card["week_sel"]],
                    "wa": None,
                    "wn": [],
                }[action]
                try:
                    registry.set_service_member_week_tasks(
                        actor_id, service_id, name, chosen
                    )
                except ValueError as exc:
                    await show_member_week(
                        message, actor_id, service_id,
                        _status_text(str(exc), "⚠️"),
                    )
                else:
                    card.pop("week_sel", None)
                    await show_member_card(
                        message, actor_id, service_id, name,
                        "✅ В одну неделю: " + member_week_label(
                            registry, service_id, chosen, card["tasks"],
                        ),
                    )
            elif action == "day":
                buttons = [
                    member_button(
                        f"📅 {member_day_label(offset)}", "dset", service_id,
                        token, str(offset),
                    )
                    for offset in MEMBER_DAYS
                ]
                rows = [buttons[i:i + 3] for i in range(0, len(buttons), 3)]
                rows.append([member_button(
                    "↩️ Как у всех", "dset", service_id, token, "n"
                )])
                rows.append([member_button(
                    "⬅️ К человеку", "card", service_id, token
                )])
                await _send_dialog(
                    message,
                    f"📅 День служения · {name}\n\nВ какой день человек "
                    "выполняет служение относительно воскресенья графика? "
                    "Например, чт — «вс −3». Показывается в графике и "
                    "публикации.",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
                    _dialog_keep_rows=True,
                    _dialog_keyboard_limit=sum(len(row) for row in rows),
                    parse_mode=None,
                )
            elif action == "lim" or (
                action == "lset" and arg and arg.startswith("m")
            ):
                title, options = month_limit_choices(arg or "")
                buttons = [
                    member_button(
                        label if label.startswith("✏️") else f"🔢 {label}",
                        "lset", service_id, token, value,
                    )
                    for label, value in options
                ]
                if not title:
                    buttons.insert(0, member_button(
                        "↩️ Как у служения", "lset", service_id, token, "s"
                    ))
                rows = [buttons[i:i + 3] for i in range(0, len(buttons), 3)]
                rows.append([member_button(
                    "⬅️ К человеку", "card", service_id, token
                )])
                await _send_dialog(
                    message,
                    f"🔢 Служений в месяц · {name}\n\n" + (
                        title or
                        "Сколько раз в месяц человека можно ставить в "
                        "график. Максимум — желательный, при необходимости "
                        "может быть превышен; минимум — бот старается дать "
                        "не меньше. Личный "
                        "лимит главнее лимита служения."
                    ),
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
                    _dialog_keep_rows=True,
                    _dialog_keyboard_limit=sum(len(row) for row in rows),
                    parse_mode=None,
                )
            elif action == "lset" and arg:
                limit = None if arg == "s" else month_limit_value(arg)
                registry.set_service_member_limit(
                    actor_id, service_id, name, limit
                )
                await show_member_card(
                    message, actor_id, service_id, name,
                    "✅ Служений в месяц: " + (
                        "как у служения" if limit is None
                        else month_limit_label(limit)
                    ),
                )
            elif action == "dset" and (
                arg == "n" or arg.lstrip("-").isdigit()
            ):
                offset = None if arg == "n" else int(arg)
                registry.set_service_member_day(
                    actor_id, service_id, name, offset
                )
                await show_member_card(
                    message, actor_id, service_id, name,
                    "✅ День служения: " + (
                        member_day_label(offset) if offset is not None
                        else "как у всех"
                    ),
                )
            elif action == "del":
                await _send_dialog(
                    message,
                    f"🗑 Убрать «{name}» из состава служения?\n\nПривязки к "
                    "задачам удалятся, в распределение графика человек больше "
                    "не попадёт.",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [member_button("✅ Да, убрать", "delok", service_id, token)],
                        [member_button("⬅️ К человеку", "card", service_id, token)],
                    ]),
                    _dialog_keep_rows=True,
                    parse_mode=None,
                )
            elif action == "delok":
                in_list = registry.remove_service_member(
                    actor_id, service_id, name
                )
                refresh_registered_contacts(actor_id, service_id)
                pending_inputs[actor_id].pop("member_card", None)
                pending_inputs[actor_id].setdefault(
                    "removed_members", []
                ).append(name)
                notice = f"🗑 «{name}» убран(а) из состава"
                if in_list:
                    notice += (
                        "\nВ «Списках» Excel он(а) остаётся, но бот его "
                        "больше не вернёт — пока не добавите в боте снова."
                    )
                await send_member_choices(
                    message, actor_id, service_id,
                    pending_inputs[actor_id].get("member_page", 0), notice,
                )
            else:
                await show_member_card(
                    message, actor_id, service_id, name, "⚠️ Экран устарел"
                )
            await _answer_callback(callback)
        except (PermissionError, ValueError) as exc:
            await show_member_card(
                message, actor_id, service_id, name, _status_text(str(exc), "⚠️")
            )
            await answer_quietly(callback, _status_text(str(exc), "⚠️"))
        except Exception:  # noqa: BLE001
            debug.exception("Ошибка карточки участника")
            await answer_quietly(callback, "❌ Не удалось выполнить действие.")

    async def return_to_contact_choices(
        message: Message, actor_id: int, notice: str
    ) -> None:
        """Шаг назад из выбора Telegram-пользователя или ввода имени."""
        pending = pending_inputs.get(actor_id)
        if pending is None or pending.get("operation") != "service_contact":
            return
        for request_id, share in list(pending_shares.items()):
            if share[0] == actor_id:
                pending_shares.pop(request_id, None)
        for key in list(pending_admin_confirmations):
            if key[0] == actor_id:
                pending_admin_confirmations.pop(key, None)
        pending.pop("active_contact_name", None)
        await _send_dialog(
            message, notice, parse_mode=None,
            reply_markup=ReplyKeyboardRemove(),
        )
        service_id = pending["service_id"]
        if pending.get("contact_mode") == "person":
            await send_member_choices(
                message, actor_id, service_id, pending.get("member_page", 0)
            )
        elif pending.get("active_task"):
            await offer_contact_name_choices(
                message, actor_id, service_id, pending["active_task"]
            )

    async def offer_contact_task_choices(
        message: Message,
        actor_id: int,
        service_id: str,
    ) -> bool:
        if not await load_contact_context(message, actor_id, service_id):
            return False
        await send_task_choices_page(
            message, actor_id, service_id, "service_contact"
        )
        return True

    async def load_contact_context(
        message: Message,
        actor_id: int,
        service_id: str,
        *,
        force_reload: bool = False,
    ) -> bool:
        registry = service_registry()
        if registry is None:
            await _send_dialog(message, "Реестр настроек служений недоступен.", parse_mode=None
            )
            return False
        task_loader = getattr(context, "load_service_task_assignments", None)
        if not callable(task_loader):
            await _send_dialog(message, "Загрузка задач из файла расписания недоступна.",
                parse_mode=None,
            )
            return False
        current_pending = pending_inputs.get(actor_id)
        if (
            current_pending is None
            or current_pending.get("operation") != "service_contact"
            or current_pending.get("service_id") != service_id
        ):
            await _send_dialog(message, "Сценарий выбора служения отменён или устарел.",
                parse_mode=None,
            )
            return False
        session_id = current_pending.get("session_id")
        try:
            assignments = await (
                task_loader(service_id, force=True) if force_reload
                else task_loader(service_id)
            )
        except (PermissionError, ValueError) as exc:
            await _send_dialog(message, _status_text(str(exc), "❌"), parse_mode=None)
            return False
        except Exception:
            debug.exception("Не удалось прочитать задачи из графика служений")
            await _send_dialog(message, "Не удалось прочитать график служений. "
                "Попробуйте снова; если ошибка повторяется, обратитесь к суперадминистратору.",
                parse_mode=None,
            )
            return False
        try:
            contacts = registry.list_responsible_contacts(actor_id, service_id)
        except (PermissionError, ValueError) as exc:
            await _send_dialog(message, _status_text(str(exc), "❌"), parse_mode=None)
            return False
        except sqlite3.Error:
            debug.exception("Не удалось прочитать привязки ответственных")
            await _send_dialog(message, "Не удалось прочитать список привязанных пользователей.",
                parse_mode=None,
            )
            return False
        pending = pending_inputs.get(actor_id)
        if (
            pending is None
            or pending.get("operation") != "service_contact"
            or pending.get("service_id") != service_id
            or pending.get("session_id") != session_id
        ):
            await _send_dialog(message, "Сценарий выбора служения был отменён во время загрузки графика.",
                parse_mode=None,
            )
            return False
        pending["schedule_assignments"] = assignments
        pending["schedule_tasks"] = sorted(
            task
            for task, task_info in assignments.items()
            if not task_is_informational(task_info)
        )
        informational_tasks = sorted(
            task
            for task, task_info in assignments.items()
            if task_is_informational(task_info)
        )
        if informational_tasks:
            debug.debug(
                "Информационные записи исключены из привязок для %s: %s",
                service_id,
                informational_tasks,
            )
        pending["registered_contacts"] = {}
        for contact in contacts:
            pending["registered_contacts"].setdefault(
                contact["task"], []
            ).append(contact)
        return True

    async def offer_contact_overview(
        message: Message, actor_id: int, service_id: str,
        force_reload: bool = False, members_first: bool = False,
    ) -> None:
        """Show who serves on each upcoming date and whether Telegram is linked."""
        loader = getattr(context, "load_upcoming_service_events", None)
        if loader is None:
            await offer_contact_task_choices(message, actor_id, service_id)
            return
        if not await load_contact_context(
            message, actor_id, service_id, force_reload=force_reload
        ):
            return
        pending = pending_inputs[actor_id]
        try:
            events = await (loader(force=True) if force_reload else loader())
        except (OSError, RuntimeError, ValueError, sqlite3.Error):
            debug.exception("Не удалось загрузить график для обзора ответственных")
            events = None
        if pending_inputs.get(actor_id) is not pending:
            return
        # Задача со своим днём (спевка — в файле отдельной средой) — в блоке
        # своего воскресенья.
        own_days = {
            task["name"]: task["day_offset"]
            for task in service_registry().list_service_tasks(service_id)
            if task["day_offset"] is not None
        }
        pending["overview_events"] = (
            None
            if events is None
            else [
                SimpleNamespace(
                    event_date=block["date"],
                    assignments=tuple(block["assignments"]),
                    days=block["days"],
                )
                for block in schedule_plan.week_events(
                    [event for event in events if event.service_id == service_id],
                    own_days,
                )
            ]
        )
        if members_first:
            pending["overview_index"] = 0
            await send_member_choices(message, actor_id, service_id)
            return
        await send_contact_overview(message, actor_id, service_id, 0)

    def contact_overview_text(
        service: dict, events: list | None, index: int, contacts: list[dict]
    ) -> str:
        title = f"👥 Ответственные · {service_label(service)}"
        if events is None:
            return (
                f"{title}\n⚠️ Не удалось загрузить график по датам. "
                "Привязки можно изменить по задачам."
            )
        if not events:
            return (
                f"{title}\n⚪ В ближайшие {UPCOMING_WEEKS} недель событий "
                "этого служения в графике нет."
            )
        event = events[index]
        by_person = {
            (contact["task"], contact["name"]): contact for contact in contacts
        }
        lines = [
            title,
            f"🗓 {event.event_date:%d.%m.%Y}, "
            f"{_WEEKDAYS[event.event_date.weekday()]} · "
            f"{index + 1} из {len(events)}",
        ]
        names_by_task: dict[str, list[str]] = {}
        notes = []
        for assignment in event.assignments:
            if assignment.is_informational:
                notes.append(
                    informational_label(assignment.task, [assignment.name])
                )
                continue
            names = names_by_task.setdefault(assignment.task, [])
            if assignment.name and assignment.name not in names:
                names.append(assignment.name)
        linked = total = 0
        days = getattr(event, "days", {})
        for task, names in sort_tasks_for_display(
            names_by_task.items(), service["id"], name=lambda item: item[0],
        ):
            moment = days.get(task)
            lines.extend(["", f"{task_emoji(task)} {task}" + (
                f" · {_WEEKDAYS[moment.weekday()]} {moment:%d.%m}"
                if moment else ""
            )])
            if not names:
                lines.append("• ⚪ ответственный не указан")
            for name in names:
                total += 1
                contact = by_person.get((task, name))
                if contact is None or not contact.get("telegram_id"):
                    lines.append(f"• {name} — ⚪ не привязан")
                    continue
                linked += 1
                account = (
                    f"@{contact['username']}"
                    if contact.get("username")
                    else "привязан"
                )
                lines.append(f"• {name} — ✅ {account}")
        if notes:
            lines.extend(["", *(f"ℹ️ {note}" for note in notes)])
        lines.extend(["", f"📊 Привязано {linked} из {total}"])
        return "\n".join(lines)

    async def send_contact_overview(
        message: Message, actor_id: int, service_id: str, index: int
    ) -> None:
        pending = pending_inputs.get(actor_id)
        registry = service_registry()
        service = service_by_id(registry, service_id) if registry else None
        if pending is None or service is None:
            await _send_dialog(
                message, "Обзор ответственных устарел. Откройте его заново.",
                parse_mode=None,
            )
            return
        events = pending.get("overview_events")
        index = min(max(index, 0), max(len(events or []) - 1, 0))
        pending["overview_index"] = index
        pending["step"] = "overview"
        pending.pop("active_task", None)
        pending.pop("active_contact_name", None)
        for token, choice in list(pending_contact_navigation.items()):
            if choice[:2] == (actor_id, service_id):
                pending_contact_navigation.pop(token, None)
        text = contact_overview_text(
            service,
            events,
            index,
            registry.list_responsible_contacts(actor_id, service_id),
        )
        rows = []
        dates = []
        if events and index > 0:
            dates.append(
                InlineKeyboardButton(
                    text=f"⬅️ {events[index - 1].event_date:%d.%m}",
                    callback_data=contact_navigation_callback(
                        actor_id, service_id, f"overview:{index - 1}"
                    ),
                )
            )
        if events and index + 1 < len(events):
            dates.append(
                InlineKeyboardButton(
                    text=f"➡️ {events[index + 1].event_date:%d.%m}",
                    callback_data=contact_navigation_callback(
                        actor_id, service_id, f"overview:{index + 1}"
                    ),
                )
            )
        if dates:
            rows.append(dates)
        rows.append([contact_navigation_button(actor_id, service_id, "edit")])
        if is_service_admin(actor_id, service_id):
            rows.append(
                [contact_navigation_button(actor_id, service_id, "notify")]
            )
        rows.append([contact_navigation_button(actor_id, service_id, "card")])
        await _send_dialog(
            message,
            text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
            parse_mode=None,
            _dialog_keep_rows=True,
            _dialog_keyboard_limit=sum(len(row) for row in rows),
        )

    async def offer_notify_task_choices(
        message: Message, actor_id: int, service_id: str
    ) -> bool:
        pending = pending_inputs.get(actor_id)
        registry = service_registry()
        if (
            pending is None
            or pending.get("operation") != "service_notify"
            or registry is None
        ):
            await _send_dialog(message, "Пошаговая настройка уведомлений завершена или недоступна.",
                parse_mode=None,
            )
            return False
        if "schedule_assignments" in pending:
            assignments = pending["schedule_assignments"]
        else:
            task_loader = getattr(context, "load_service_task_assignments", None)
            if not callable(task_loader):
                await _send_dialog(message, "Загрузка задач из файла расписания недоступна.",
                    parse_mode=None,
                )
                return False
            try:
                assignments = await task_loader(service_id)
            except Exception:
                debug.exception("Не удалось прочитать задачи из графика служений")
                await _send_dialog(message, "Не удалось прочитать файл графика служений. "
                    "Проверьте доступность расписания и попробуйте снова.",
                    parse_mode=None,
                )
                return False
        pending["schedule_assignments"] = assignments
        pending["schedule_tasks"] = sorted(assignments)
        pending["step"] = "choose_notify_task"
        await send_task_choices_page(
            message, actor_id, service_id, "service_notify"
        )
        return True

    def notification_reminders(
        registry: ServiceRegistry, pending: dict,
    ) -> list[dict]:
        reminders = registry.list_group_reminders(pending["service_id"])
        if pending.get("notify_scoped"):
            return [
                item for item in reminders
                if item.get("task") == pending.get("notify_task")
            ]
        return reminders

    async def show_notification_settings_menu(
        message: Message, actor_id: int, service_id: str
    ) -> None:
        pending = pending_inputs.get(actor_id)
        if (
            pending is None
            or pending.get("operation") != "service_notify"
            or pending.get("service_id") != service_id
        ):
            await _send_dialog(message, "Настройка устарела. Запустите /service_notify заново.",
                parse_mode=None,
            )
            return
        service = service_by_id(service_registry(), service_id)
        if service is None:
            await _send_dialog(message, "Служение не найдено.", parse_mode=None)
            return
        pending["step"] = "menu"
        reminders = notification_reminders(service_registry(), pending)
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=(
                            f"⏰ Напоминания "
                            f"({len(reminders)})"
                        ),
                        callback_data=(
                            f"group_reminder:list:{service_id}:"
                            f"{pending['session_id']}"
                        ),
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="📝 Шаблон по умолчанию",
                        callback_data=(
                            f"group_reminder:default:{service_id}:"
                            f"{pending['session_id']}"
                        ),
                    )
                ],
                [
                    InlineKeyboardButton(
                        text=(
                            "📝 Личные шаблоны "
                            f"({len(pending['personal_templates'])})"
                        ),
                        callback_data=(
                            f"notify_edit:personal:{service_id}:"
                            f"{pending['session_id']}"
                        ),
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="📋 Отчёт админам",
                        callback_data=(
                            f"group_reminder:report:{service_id}:"
                            f"{pending['session_id']}"
                        ),
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="✅ Завершить и сохранить",
                        callback_data=(
                            f"notify_finish:{service_id}:"
                            f"{pending['session_id']}"
                        ),
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="✖️ Закрыть настройку",
                        callback_data=(
                            f"notify_cancel:{service_id}:"
                            f"{pending['session_id']}"
                        ),
                    )
                ],
            ]
        )
        scope = (
            f" · {pending.get('notify_task') or 'Всё служение'}"
            if pending.get("notify_scoped") else ""
        )
        await _send_dialog(message, f"🔔 {service_label(service)}{scope}\n"
            "ℹ️ Выберите, что изменить. Текущие значения сохраняются, "
            "пока вы их не измените.\n"
            "Шаблон по умолчанию и отчёт админам — общие для служения.",
            reply_markup=keyboard,
            parse_mode=None,
        )

    async def show_reminder_rules_menu(
        message: Message, actor_id: int, service_id: str,
        page: int | None = None,
    ) -> None:
        pending = pending_inputs.get(actor_id)
        if (
            pending is None
            or pending.get("operation") != "service_notify"
            or pending.get("service_id") != service_id
        ):
            await _send_dialog(message, "Настройка устарела. Запустите /service_notify заново.",
                parse_mode=None,
            )
            return
        pending["step"] = "reminder_menu"
        registry = service_registry()
        if registry is None:
            await _send_dialog(
                message, "Реестр настроек служений недоступен.",
                parse_mode=None,
            )
            return
        service = service_by_id(registry, service_id)
        if service is None:
            await _send_dialog(message, "Служение не найдено.", parse_mode=None)
            return
        reminders = notification_reminders(registry, pending)
        page_size = 6
        pages = max(1, (len(reminders) + page_size - 1) // page_size)
        page = max(0, min(
            pending.get("reminder_page", 0) if page is None else page, pages - 1
        ))
        pending["reminder_page"] = page
        pending["reminder_menu_ids"] = [
            item["id"] for item in
            reminders[page * page_size:(page + 1) * page_size]
        ]
        scope = (
            f" · {pending.get('notify_task') or 'Всё служение'}"
            if pending.get("notify_scoped") else ""
        )
        lines = [f"⏰ {service_label(service)}{scope} · напоминания", ""]
        if pending.get("notify_no_tasks"):
            lines += [
                "ℹ️ У служения нет отдельных задач — показаны общие "
                "напоминания.",
                "",
            ]
        if not reminders:
            lines += [
                "⚪ Напоминаний нет.",
                "Добавьте первое напоминание, чтобы настроить отправку.",
            ]
        elif pages > 1:
            lines.append(f"📊 Страница {page + 1}/{pages}")
        buttons = []
        today = datetime.now(context.timezone).date()
        for index, reminder in enumerate(
            reminders[page * page_size:(page + 1) * page_size],
            start=page * page_size + 1,
        ):
            period_mark = {"pending": "⏳ ", "ended": "⏸ "}.get(
                reminder_period_status(reminder, today), ""
            )
            weekday = _WEEKDAYS[
                (6 + reminder_day_offset(reminder) - reminder["reminder_days"]) % 7
            ]
            body = " ".join(
                plain_text(
                    registry.resolve_group_text(reminder, service)
                ).strip().split("\n", 1)[0].split()
            )
            preview = body[:64] + ("…" if len(body) > 64 else "")
            task = (
                f" · {reminder.get('task') or 'Всё служение'}"
                if not pending.get("notify_scoped") else ""
            )
            lines.append(
                f"{index}. {'✅' if reminder['enabled'] else '⏸'} "
                f"{weekday} · {reminder['reminder_time']}{task} · "
                f"{period_mark}{preview}"
            )
            buttons.append(
                InlineKeyboardButton(
                    text=str(index),
                    callback_data=(
                        f"group_reminder:open:{service_id}:"
                        f"{reminder['id']}:{pending['session_id']}"
                    ),
                ),
            )
        if reminders:
            lines += ["", "Выберите номер напоминания, чтобы изменить его."]
        rows = [buttons[index:index + 3] for index in range(0, len(buttons), 3)]
        navigation = []
        for target, label in ((page - 1, "⬅️ Предыдущая"), (page + 1, "➡️ Далее")):
            if 0 <= target < pages:
                navigation.append(InlineKeyboardButton(
                    text=label,
                    callback_data=(
                        f"group_reminder:page:{service_id}:{target}:"
                        f"{pending['session_id']}"
                    ),
                ))
        if navigation:
            rows.append(navigation)
        rows.extend(
            [
                [
                    InlineKeyboardButton(
                        text="📊 Сводка личных",
                        callback_data=(
                            f"group_reminder:summary:{service_id}:"
                            f"{pending['session_id']}"
                        ),
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="➕ Добавить напоминание",
                        callback_data=(
                            f"group_reminder:add:{service_id}:"
                            f"{pending['session_id']}"
                        ),
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="⬅️ К настройкам",
                        callback_data=(
                            f"group_reminder:root_back:{service_id}:"
                            f"{pending['session_id']}"
                        ),
                    )
                ],
            ]
        )
        await _send_dialog(message, "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
            parse_mode=None,
            _dialog_keep_rows=True,
            _dialog_keyboard_limit=sum(len(row) for row in rows),
            _dialog_text_limit=4000,
        )

    async def enter_group_reminder_card(
        message: Message,
        actor_id: int,
        service_id: str,
        reminder_id: int,
    ) -> None:
        pending = pending_inputs.get(actor_id)
        registry = service_registry()
        if (
            pending is None
            or pending.get("operation") != "service_notify"
            or pending.get("service_id") != service_id
            or registry is None
        ):
            await _send_dialog(message, "Настройка устарела.", parse_mode=None)
            return
        reminder = registry.get_group_reminder(reminder_id)
        service = registry.get_service(service_id)
        if reminder is None or reminder["service_id"] != service_id:
            await _send_dialog(
                message, "Групповое напоминание больше не существует.",
                parse_mode=None,
            )
            return
        if service is None:
            await _send_dialog(message, "Служение не найдено.", parse_mode=None)
            return
        pending.pop("preview_request_id", None)
        pending["step"] = "group_reminder_card"
        pending["reminder_id"] = reminder_id
        now = datetime.now(context.timezone)
        occurrence = reminder_occurrence(now, reminder)
        send_text = (
            f"{occurrence[1]:%d.%m.%Y} в {reminder['reminder_time']}"
            if occurrence else "— (период завершён)"
        )
        body_status = (
            "собственный" if reminder["body"] else
            "используется шаблон по умолчанию"
        )
        current_text = reminder["body"] or registry.resolve_group_text(
            reminder, service
        )
        task_status = (
            f"«{reminder['task']}»" if reminder.get("task") else "все задачи"
        )
        text = (
            f"📝 Напоминание · {service_label(service)}\n\n"
            f"📌 Задача: {task_status}\n"
            f"📅 Срок: за {reminder['reminder_days']} дн. до "
            f"{reminder_anchor_text(reminder)}\n"
            f"🗓 Период: {period_text(reminder, now.date())}\n"
            f"✉️ Отправка: {send_text}\n"
            f"📝 Текст: {body_status}\n"
            f"🔔 Статус: {'включено' if reminder['enabled'] else 'выключено'}"
            f"\n\nТекущий текст напоминания:\n{current_text}"
        )
        buttons = [
            [
                InlineKeyboardButton(
                    text="👁 Предпросмотр",
                    callback_data=(
                        f"group_reminder:preview:{service_id}:{reminder_id}:"
                        f"{pending['session_id']}"
                    ),
                ),
            ],
            [
                InlineKeyboardButton(
                    text="📤 Отправить сейчас",
                    callback_data=(
                        f"group_reminder:send_now:{service_id}:{reminder_id}:"
                        f"{pending['session_id']}"
                    ),
                ),
            ],
            [
                InlineKeyboardButton(
                    text="✏️ Текст",
                    callback_data=(
                        f"group_reminder:edit_text:{service_id}:"
                        f"{reminder_id}:{pending['session_id']}"
                    ),
                ),
                InlineKeyboardButton(
                    text="📅 Срок",
                    callback_data=(
                        f"group_reminder:edit_days:{service_id}:"
                        f"{reminder_id}:{pending['session_id']}"
                    ),
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🕒 Время",
                    callback_data=(
                        f"group_reminder:edit_time:{service_id}:"
                        f"{reminder_id}:{pending['session_id']}"
                    ),
                ),
                InlineKeyboardButton(
                    text="🔄 Выключить" if reminder["enabled"] else "🔄 Включить",
                    callback_data=(
                        f"group_reminder:toggle:{service_id}:"
                        f"{reminder_id}:{pending['session_id']}"
                    ),
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🗓 Период",
                    callback_data=(
                        f"group_reminder:period_menu:{service_id}:"
                        f"{reminder_id}:{pending['session_id']}"
                    ),
                ),
            ],
            [
                InlineKeyboardButton(
                    text="📌 Задача",
                    callback_data=(
                        f"group_reminder:task_menu:{service_id}:"
                        f"{reminder_id}:{pending['session_id']}"
                    ),
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🗑 Удалить",
                    callback_data=(
                        f"group_reminder:delete_prompt:{service_id}:"
                        f"{reminder_id}:{pending['session_id']}"
                    ),
                ),
            ],
        ]
        if reminder["body"]:
            buttons.append(
                [
                    InlineKeyboardButton(
                        text="🔄 Вернуть шаблон по умолчанию",
                        callback_data=(
                            f"group_reminder:reset_text:{service_id}:"
                            f"{reminder_id}:{pending['session_id']}"
                        ),
                    )
                ],
            )
        # Переход к соседним напоминаниям служения в порядке списка.
        reminder_ids = [
            item["id"] for item in notification_reminders(registry, pending)
        ]
        position = (
            reminder_ids.index(reminder_id) if reminder_id in reminder_ids else -1
        )
        neighbours = []
        if position > 0:
            neighbours.append(
                InlineKeyboardButton(
                    text="⬅️ Предыдущее",
                    callback_data=(
                        f"group_reminder:nav:{service_id}:"
                        f"{reminder_ids[position - 1]}:{pending['session_id']}"
                    ),
                )
            )
        if 0 <= position < len(reminder_ids) - 1:
            neighbours.append(
                InlineKeyboardButton(
                    text="➡️ Следующее",
                    callback_data=(
                        f"group_reminder:nav:{service_id}:"
                        f"{reminder_ids[position + 1]}:{pending['session_id']}"
                    ),
                )
            )
        if neighbours:
            buttons.append(neighbours)
            text += f"\n\n📋 Напоминание {position + 1} из {len(reminder_ids)}"
        buttons.append(
            [
                InlineKeyboardButton(
                    text="⬅️ К напоминаниям",
                    callback_data=(
                        f"group_reminder:back:{service_id}:"
                        f"{pending['session_id']}"
                    ),
                )
            ]
        )
        await _send_dialog(
            message,
            text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
            # Свой текст и соседи — до 13 кнопок: лимит по факту.
            _dialog_keyboard_limit=sum(len(row) for row in buttons),
            _dialog_keep_rows=True,
            parse_mode=None,
        )

    def send_now_snapshot(reminder: dict | None):
        """Что должно не измениться между предпросмотром и отправкой:
        напоминание (с периодом) и чат (тема) служения."""
        if reminder is None:
            return None
        service = service_registry().get_service(reminder["service_id"]) or {}
        return (
            reminder["service_id"], reminder["body"], reminder.get("task"),
            reminder["reminder_days"], reminder["reminder_time"],
            reminder.get("active_from"), reminder.get("active_until"),
            service.get("group_chat_id"), service.get("group_thread_id"),
            service_registry().resolve_group_text(reminder, service),
        )

    async def show_send_now_confirm(
        message: Message, actor_id: int, service_id: str, reminder_id: int
    ) -> None:
        """«📤 Отправить сейчас»: текст на ближайшую дату и подтверждение."""
        pending = pending_inputs.get(actor_id)
        registry = service_registry()
        service = registry.get_service(service_id) if registry else None
        loader = getattr(context, "get_group_reminder_preview", None)
        session_id = pending["session_id"]
        back = InlineKeyboardButton(
            text="⬅️ К напоминанию",
            callback_data=(
                f"group_reminder:nav:{service_id}:{reminder_id}:{session_id}"
            ),
        )
        result = None
        current = registry.get_group_reminder(reminder_id) if registry else None
        snapshot = send_now_snapshot(current)
        period_ended = current is not None and reminder_occurrence(
            datetime.now(context.timezone), current
        ) is None
        if callable(loader) and not period_ended:
            try:
                result = await loader(reminder_id, manual=True)
            except Exception:  # noqa: BLE001
                debug.exception("Не удалось подготовить отправку сейчас")
        reminder = registry.get_group_reminder(reminder_id) if registry else None
        if (
            pending_inputs.get(actor_id) is not pending
            or pending.get("step") != "group_reminder_card"
            or pending.get("reminder_id") != reminder_id
        ):
            return
        if reminder is None or reminder["service_id"] != service_id:
            text, rows = "⚠️ Напоминание больше не существует.", [[back]]
        elif service is None or not service.get("group_chat_id"):
            text, rows = (
                "⚠️ У служения не привязан групповой чат — отправлять некуда.",
                [[back]],
            )
        elif period_ended:
            text, rows = (
                "⚠️ Период напоминания завершён — задайте новый период "
                "в «🗓 Период».",
                [[back]],
            )
        elif send_now_snapshot(reminder) != snapshot:
            text, rows = (
                "⚠️ Напоминание изменилось. Откройте предпросмотр заново.",
                [[back]],
            )
        elif not result or not result.get("has_data"):
            reason = (result or {}).get("error") or "Не удалось загрузить график."
            text, rows = f"⚠️ Сейчас отправить нельзя: {reason}", [[back]]
        elif visible_units(result["text"]) > TELEGRAM_TEXT_LIMIT:
            text, rows = (
                "⚠️ Сообщение длиннее лимита Telegram и не будет отправлено.",
                [[back]],
            )
        else:
            pending["send_now"] = {
                "reminder_id": reminder_id, "text": result["text"],
                "snapshot": snapshot,
                "event_date": result.get("event_date"),
                "schedule_state": result.get("schedule_state"),
                "assignment_state": result.get("assignment_state"),
                "template": registry.resolve_group_text(reminder, service),
                "task": reminder.get("task"),
            }
            text = (
                f"📤 Отправить сейчас в чат служения? Так оно будет "
                f"выглядеть на {result['date']}:\n\n"
                "──────────────────\n\n"
                f"{result['text']}\n\n"
                + "".join(f"⚠️ {warning}\n" for warning in result.get("warnings", []))
                + "ℹ️ Запланированная отправка останется в силе."
            )
            rows = [[
                InlineKeyboardButton(
                    text="✅ Отправить",
                    callback_data=(
                        f"group_reminder:send_now_ok:{service_id}:"
                        f"{reminder_id}:{session_id}"
                    ),
                ),
                back,
            ]]
        await _send_dialog(
            message, text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
            _dialog_text_limit=4096,
            parse_mode=None,
        )

    async def show_group_reminder_period(
        message: Message, actor_id: int, service_id: str, reminder_id: int,
        active_from: date | None = None, *, pick_until: bool = False,
    ) -> None:
        """Период напоминания: сначала воскресенье начала, затем окончания."""
        pending = pending_inputs.get(actor_id)
        registry = service_registry()
        if pending is None or registry is None:
            await _send_dialog(message, "Настройка устарела.", parse_mode=None)
            return
        reminder = registry.get_group_reminder(reminder_id)
        if reminder is None or reminder["service_id"] != service_id:
            raise ValueError("Групповое напоминание больше не существует.")
        today = datetime.now(context.timezone).date()
        # Первое воскресенье, неделя которого ещё идёт: у задачи со своим
        # днём (вс +5) — до дня задачи.
        start = today - timedelta(days=reminder_day_offset(reminder))
        first = start + timedelta(days=(6 - start.weekday()) % 7)
        if pick_until:
            start = max(first, active_from or first)
            choices = [start + timedelta(weeks=week) for week in range(12)]
            title = (
                f"🗓 Период · по какое воскресенье?\n"
                f"Начало: {f'{active_from:%d.%m.%Y}' if active_from else 'сразу'}"
            )
            open_label = "♾ Без окончания"
        else:
            choices = [first + timedelta(weeks=week) for week in range(9)]
            title = (
                f"🗓 Период напоминания\nСейчас: {period_text(reminder, today)}"
                "\n\nС какого воскресенья отправлять?"
            )
            open_label = "▶️ Сразу"
        pending["step"] = "group_reminder_period"
        pending["reminder_id"] = reminder_id
        pending["period_choices"] = choices
        pending["period_from"] = active_from
        pending["period_pick_until"] = pick_until
        action = "period_until" if pick_until else "period_from"
        session = pending["session_id"]
        # Номер воскресенья вместе с напоминанием: кнопка с экрана другого
        # напоминания не изменит текущее.
        def choice_data(index: int) -> str:
            value = reminder_id * PERIOD_CHOICE_BASE + index
            return f"group_reminder:{action}:{service_id}:{value}:{session}"

        buttons = [
            InlineKeyboardButton(text=f"🗓 {day:%d.%m}", callback_data=choice_data(index))
            for index, day in enumerate(choices, start=1)
        ]
        rows = [[
            InlineKeyboardButton(text=open_label, callback_data=choice_data(0))
        ]]
        rows.extend(buttons[index:index + 3] for index in range(0, len(buttons), 3))
        rows.append([
            InlineKeyboardButton(
                text="✏️ Ввести вручную",
                callback_data=(
                    f"group_reminder:period_input:{service_id}:"
                    f"{reminder_id}:{session}"
                ),
            ),
            InlineKeyboardButton(
                text="♾ Без периода",
                callback_data=(
                    f"group_reminder:period_clear:{service_id}:"
                    f"{reminder_id}:{session}"
                ),
            ),
        ])
        rows.append([
            InlineKeyboardButton(
                text="⬅️ К напоминанию",
                callback_data=f"group_reminder:back:{service_id}:{session}",
            )
        ])
        # Даты — сетка по 3 в ряд на одном экране, без страниц.
        await _send_dialog(
            message,
            title + "\nℹ️ Вне периода напоминание не отправляется, после "
            "окончания выключается.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
            parse_mode=None,
            _dialog_keep_rows=True,
            _dialog_keyboard_limit=sum(len(row) for row in rows),
        )

    async def show_group_reminder_tasks(
        message: Message, actor_id: int, service_id: str, reminder_id: int
    ) -> None:
        """Выбор задачи напоминания: все задачи или одна задача служения."""
        pending = pending_inputs.get(actor_id)
        registry = service_registry()
        if pending is None or registry is None:
            await _send_dialog(message, "Настройка устарела.", parse_mode=None)
            return
        tasks = [
            task["name"]
            for task in registry.list_service_tasks(service_id)
            if task["enabled"]
        ]
        pending["step"] = "group_reminder_task"
        pending["reminder_id"] = reminder_id
        pending["task_choices"] = tasks
        rows = [[
            InlineKeyboardButton(
                text="📋 Все задачи",
                callback_data=(
                    f"group_reminder:task_pick:{service_id}:0:"
                    f"{pending['session_id']}"
                ),
            )
        ]]
        rows.extend(
            [InlineKeyboardButton(
                text=f"{task_emoji(task)} {task}"[:60],
                callback_data=(
                    f"group_reminder:task_pick:{service_id}:{index}:"
                    f"{pending['session_id']}"
                ),
            )]
            for index, task in enumerate(tasks, start=1)
        )
        rows.append([
            InlineKeyboardButton(
                text="⬅️ К напоминанию",
                callback_data=(
                    f"group_reminder:back:{service_id}:{pending['session_id']}"
                ),
            )
        ])
        await _send_dialog(
            message,
            "📌 Задача напоминания\n"
            "ℹ️ Для одной задачи в шаблоне доступны {task} и "
            "{task_responsibles}; если на дату никто не назначен, "
            "напоминание не отправляется, админ получает ⚠️.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
            parse_mode=None,
        )

    async def enter_default_template(
        message: Message, actor_id: int, service_id: str
    ) -> None:
        pending = pending_inputs.get(actor_id)
        registry = service_registry()
        service = registry.get_service(service_id) if registry else None
        if (
            pending is None
            or pending.get("operation") != "service_notify"
            or pending.get("service_id") != service_id
            or service is None
        ):
            await _send_dialog(message, "Настройка устарела.", parse_mode=None)
            return
        pending["step"] = "default_template"
        group_template = pending.get(
            "group_template", service["group_template"]
        )
        current_template = (
            group_template or "Используется встроенный шаблон."
        )
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="✏️ Изменить",
                        callback_data=(
                            f"group_reminder:edit_default:{service_id}:"
                            f"{pending['session_id']}"
                        ),
                    ),
                    InlineKeyboardButton(
                        text="👁 Предпросмотр",
                        callback_data=(
                            f"group_reminder:tpl_preview:{service_id}:"
                            f"{pending['session_id']}"
                        ),
                    ),
                ],
                [
                    InlineKeyboardButton(
                        text="🔄 Вернуть встроенный",
                        callback_data=(
                            f"group_reminder:reset_default:{service_id}:"
                            f"{pending['session_id']}"
                        ),
                    ),
                ],
                [
                    InlineKeyboardButton(
                        text="⬅️ К настройкам",
                        callback_data=(
                            f"group_reminder:root_back:{service_id}:"
                            f"{pending['session_id']}"
                        ),
                    )
                ],
            ]
        )
        await _send_dialog(
            message,
            f"📝 {service_label(service)} · шаблон по умолчанию\n\n"
            "ℹ️ Используется напоминаниями, у которых не задан свой текст.\n\n"
            f"Текущий текст:\n{current_template}",
            reply_markup=keyboard,
            parse_mode=None,
        )

    async def enter_admin_report_template(
        message: Message, actor_id: int, service_id: str
    ) -> None:
        """Шаблон отчёта админам после группового напоминания (этап 3.2)."""
        pending = pending_inputs.get(actor_id)
        registry = service_registry()
        service = registry.get_service(service_id) if registry else None
        if (
            pending is None
            or pending.get("operation") != "service_notify"
            or pending.get("service_id") != service_id
            or service is None
        ):
            await _send_dialog(message, "Настройка устарела.", parse_mode=None)
            return
        pending["step"] = "admin_report"
        template = pending.get("admin_report_template")
        current = template or (
            "Используется встроенный шаблон:\n" + DEFAULT_ADMIN_REPORT_TEMPLATE
        )
        session_id = pending["session_id"]
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="✏️ Изменить",
                        callback_data=(
                            f"group_reminder:edit_report:{service_id}:"
                            f"{session_id}"
                        ),
                    ),
                    InlineKeyboardButton(
                        text="👁 Предпросмотр",
                        callback_data=(
                            f"group_reminder:tpl_preview:{service_id}:"
                            f"{session_id}"
                        ),
                    ),
                ],
                [
                    InlineKeyboardButton(
                        text="🔄 Вернуть встроенный",
                        callback_data=(
                            f"group_reminder:reset_report:{service_id}:"
                            f"{session_id}"
                        ),
                    ),
                ],
                [
                    InlineKeyboardButton(
                        text="⬅️ К настройкам",
                        callback_data=(
                            f"group_reminder:root_back:{service_id}:"
                            f"{session_id}"
                        ),
                    )
                ],
            ]
        )
        await _send_dialog(
            message,
            f"📋 {service_label(service)} · отчёт админам\n\n"
            "ℹ️ Приходит админам служения в личный чат после группового "
            "напоминания и обновляется, когда участники отвечают "
            "«Принял» или «Не смогу».\n\n"
            f"Текущий текст:\n{current}",
            reply_markup=keyboard,
            parse_mode=None,
        )

    async def show_template_preview(
        callback: CallbackQuery, actor_id: int, service_id: str,
        session_id: str,
    ) -> None:
        """Шаблон по умолчанию или отчёт админам на ближайшее воскресенье;
        несохранённый черновик показывается так, как будет отправлен."""
        pending = pending_inputs.get(actor_id)
        step = pending.get("step") if pending else None
        if step not in {"default_template", "admin_report"}:
            await _answer_callback(callback, "⚠️ Этот экран уже устарел.")
            return
        await _answer_callback(callback)
        registry = service_registry()
        service = registry.get_service(service_id) if registry else None
        if service is None:
            await _send_dialog(
                callback.message, "Служение не найдено.", parse_mode=None
            )
            return
        if step == "admin_report":
            kind, title = "report", "отчёт админам"
            template = pending.get("admin_report_template")
        else:
            kind, title = "default", "шаблон по умолчанию"
            template = pending.get("group_template", service["group_template"])
        request_id = secrets.token_urlsafe(12)
        pending["step"] = "template_preview"
        pending["template_preview_kind"] = kind
        pending["preview_request_id"] = request_id

        def is_current() -> bool:
            active = pending_inputs.get(actor_id)
            return (
                active is pending
                and active.get("session_id") == session_id
                and active.get("step") == "template_preview"
                and active.get("preview_request_id") == request_id
            )

        await _send_dialog(
            callback.message,
            "⏳ Загружаю данные графика для предпросмотра...",
            parse_mode=None,
        )
        loader = getattr(context, "get_template_preview", None)
        result = None
        if callable(loader):
            try:
                result = await loader(service_id, kind, template)
            except Exception:
                debug.exception("Не удалось подготовить предпросмотр шаблона")
        if not is_current():
            return
        if result is None:
            result = {"error": "Не удалось загрузить график. Попробуйте позже."}
        text = (
            f"👁 Предпросмотр · {service_label(service)} · {title}\n\n"
            f"📅 Собрание: {result.get('date', '—')}\n\n"
            "──────────────────\n\n"
        )
        if result.get("error"):
            text += f"⚠️ {result['error']}"
        else:
            preview_text = result.get("text") or "⚪ Текст пуст."
            chunks = _telegram_text_chunks(preview_text, limit=3800)
            if len(chunks) > 1:
                preview_text = chunks[0] + "\n... (текст обрезан)"
            text += preview_text
            text += "".join(
                f"\n\n⚠️ {warning}" for warning in result.get("warnings", [])
            )
        await _send_dialog(
            callback.message,
            text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(
                    text="⬅️ К шаблону",
                    callback_data=(
                        f"group_reminder:tpl_back:{service_id}:{session_id}"
                    ),
                )
            ]]),
            _dialog_text_limit=4096,
            parse_mode=None,
        )

    def notification_edit_controls(
        service_id: str, session_id: str
    ) -> InlineKeyboardMarkup:
        rows = []
        pending = next((
            item for item in pending_inputs.values()
            if item.get("session_id") == session_id
            and item.get("service_id") == service_id
        ), None)
        if pending and pending.get("step") in {
            "group_reminder_new_text", "group_reminder_text",
            "group_default_text", "group_template",
        }:
            rows.append([InlineKeyboardButton(
                text="🔗 Назначения другого служения",
                callback_data=f"afield:{session_id}:open",
            )])
        return InlineKeyboardMarkup(
            inline_keyboard=rows + [[
                InlineKeyboardButton(
                    text="⬅️ Назад без изменения",
                    callback_data=f"notify_back:{service_id}:{session_id}",
                )
            ]]
        )

    @admin_router.callback_query(F.data.startswith("afield:"))
    async def on_assignment_field(callback: CallbackQuery) -> None:
        from .assignment_fields import assignment_field

        if (
            not callback.from_user or not callback.message
            or callback.message.chat.type != ChatType.PRIVATE
        ):
            await _answer_callback(callback)
            return
        parts = callback.data.split(":")
        pending = pending_inputs.get(callback.from_user.id)
        registry = service_registry()
        if (
            len(parts) not in {3, 5} or not pending or registry is None
            or pending.get("operation") != "service_notify"
            or pending.get("session_id") != parts[1]
            or pending.get("step") not in {
                "group_reminder_new_text", "group_reminder_text",
                "group_default_text", "group_template",
            }
        ):
            await _answer_callback(callback, "⚠️ Этот экран уже устарел.")
            return
        try:
            if not is_service_admin(callback.from_user.id, pending["service_id"]):
                raise PermissionError("Недостаточно прав.")
            picker = pending.get("assignment_picker")
            if len(parts) == 3:
                if parts[2] != "open":
                    raise ValueError("Этот экран уже устарел.")
                picker = {
                    "step": pending["step"], "token": secrets.token_hex(4),
                    "services": ordered_services([
                        service for service in registry.list_services()
                        if service["enabled"] and service["id"] != pending["service_id"]
                    ]),
                    "stage": "services",
                }
                pending["assignment_picker"] = picker
            elif (
                not picker or parts[2] != picker["token"]
                or picker["step"] != pending["step"]
            ):
                raise ValueError("Этот экран уже устарел.")
            else:
                action, raw_index = parts[3:]
                index = int(raw_index)
                if action == "service" and picker["stage"] == "services":
                    if not 0 <= index < len(picker["services"]):
                        raise ValueError("Этот экран уже устарел.")
                    source = picker["services"][index]
                    tasks = sort_tasks_for_display([
                        task["name"] for task in registry.list_service_tasks(source["id"])
                        if task["enabled"]
                    ], source["id"])
                    picker.update(
                        source=source, tasks=tasks, stage="tasks",
                        token=secrets.token_hex(4),
                    )
                elif action == "task" and picker["stage"] == "tasks":
                    if not 0 <= index <= len(picker["tasks"]):
                        raise ValueError("Этот экран уже устарел.")
                    task = picker["tasks"][index - 1] if index else None
                    value = assignment_field(picker["source"]["id"], task)
                    pending.pop("assignment_picker", None)
                    await _send_dialog(
                        callback.message,
                        "Скопируйте поле в нужное место шаблона и отправьте "
                        "полный текст сообщения:\n\n" + ui_code(value)
                        + "\n\nЗадача — ответственный — дата; только на день "
                        "отправки и позже. Если график другого служения "
                        "не утверждён или никто не назначен, поле будет пустым."
                        "\nТекущий текст и настройки пока не изменены.",
                        reply_markup=notification_edit_controls(
                            pending["service_id"], pending["session_id"],
                        ),
                        parse_mode=None,
                    )
                    await _answer_callback(callback)
                    return
                else:
                    raise ValueError("Этот экран уже устарел.")
            prefix = f"afield:{pending['session_id']}:{picker['token']}"
            if picker["stage"] == "services":
                rows = [[InlineKeyboardButton(
                    text=service["name"], callback_data=f"{prefix}:service:{index}",
                )] for index, service in enumerate(picker["services"])]
                text = (
                    "🔗 Выберите служение, чьи назначения добавить в шаблон."
                    if rows else
                    "⚪ Других включённых служений пока нет. Настройте нужное "
                    "служение и вернитесь к редактированию текста."
                )
            else:
                rows = [[InlineKeyboardButton(
                    text="Все задачи", callback_data=f"{prefix}:task:0",
                )]] + [[InlineKeyboardButton(
                    text=task, callback_data=f"{prefix}:task:{index}",
                )] for index, task in enumerate(picker["tasks"], 1)]
                rows.append([InlineKeyboardButton(
                    text="⬅️ К служениям",
                    callback_data=f"afield:{pending['session_id']}:open",
                )])
                text = f"🔗 {picker['source']['name']} — выберите задачи."
            rows.extend(notification_edit_controls(
                pending["service_id"], pending["session_id"],
            ).inline_keyboard[-1:])
            await _send_dialog(
                callback.message, text, reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=rows,
                ), parse_mode=None,
            )
        except (ValueError, PermissionError) as exc:
            await _answer_callback(callback, f"⚠️ {exc}")
            return
        except sqlite3.Error:
            debug.exception("Не удалось выбрать поле назначений")
            await _answer_callback(callback, "❌ Не удалось прочитать назначения.")
            return
        await _answer_callback(callback)

    async def show_service_tasks(
        message: Message, actor_id: int, service_id: str,
        notice: str | None = None,
    ) -> None:
        """Tasks and their notification settings for service administrators."""
        registry = service_registry()
        service = service_by_id(registry, service_id) if registry else None
        if service is None:
            await _send_dialog(message, "Служение не найдено.", parse_mode=None)
            return
        await clean_and_clear_pending_actor(actor_id)
        session_id = secrets.token_urlsafe(9)
        pending_inputs[actor_id] = {
            "operation": "service_tasks",
            "service_id": service_id,
            "session_id": session_id,
            "step": "tasks",
        }
        tasks = registry.list_service_tasks(service_id)
        lines = [f"📋 {service_label(service)} · задачи", ""]
        if tasks:
            for task in tasks:
                status = "🟢" if task["enabled"] else "🔴"
                aliases = (
                    f" · также: {', '.join(task['aliases'])}"
                    if task["aliases"] else ""
                )
                off = "" if task["enabled"] else " · выключена"
                people = (
                    f" · 👥 {people_label(task)}"
                    if task["people_max"] > 1 else ""
                )
                own_day = (
                    f" · 📅 {member_day_label(task['day_offset'])}"
                    if task["day_offset"] is not None else ""
                )
                linked = " · 🔗" if task["same_week"] else ""
                lines.append(
                    f"{status} {task_emoji(task['name'])} {task['name']}"
                    f"{people}{own_day}{linked}{off}{aliases}"
                )
            lines += [
                "",
                "ℹ️ Выберите задачу для настройки и напоминаний. "
                "«Всё служение» — общие напоминания без привязки к задаче.",
            ]
        else:
            lines.append(
                "⚪ задачи не заданы · в графике и напоминаниях служение "
                "без деления на задачи"
            )
        if notice:
            lines = [notice, ""] + lines
        buttons = [
            InlineKeyboardButton(
                text=f"{'🟢' if task['enabled'] else '🔴'} {task['name']}"[:60],
                callback_data=(
                    f"svc_tasks:card:{service_id}:{task['position']}:"
                    f"{session_id}"
                ),
            )
            for task in tasks
        ]
        rows = layout_buttons(buttons) if buttons else []
        rows.append([InlineKeyboardButton(
            text="⏰ Всё служение",
            callback_data=f"svc_tasks:notify:{service_id}:s:{session_id}",
        )])
        add_row = [
            InlineKeyboardButton(
                text="➕ Добавить задачу",
                callback_data=f"svc_tasks:add:{service_id}:{session_id}",
            )
        ]
        if any(task["enabled"] for task in tasks):
            add_row.append(InlineKeyboardButton(
                text="👥 Нужно людей",
                callback_data=f"rdy:people:{service_id}:t",
            ))
        rows.append(add_row)
        if any(task["enabled"] for task in tasks):
            rows.append([
                InlineKeyboardButton(
                    text="📅 Дни задач",
                    callback_data=f"svc_tasks:days:{service_id}:{session_id}",
                ),
                InlineKeyboardButton(
                    text="🔗 В одну неделю",
                    callback_data=f"svc_tasks:week:{service_id}:{session_id}",
                ),
            ])
        rows.append([
            InlineKeyboardButton(
                text="⬅️ К карточке служения",
                callback_data=f"service:service_settings:{service_id}",
            )
        ])
        await _send_dialog(
            message,
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
            _dialog_keyboard_limit=sum(len(row) for row in rows),
            parse_mode=None,
        )

    async def show_service_task_card(
        message: Message, actor_id: int, service_id: str, task: dict,
    ) -> None:
        pending = pending_inputs[actor_id]
        pending["step"] = "task_card"
        pending["task_position"] = task["position"]
        session_id = pending["session_id"]
        await _send_dialog(
            message,
            f"📋 {task['name']}\n\n"
            f"Статус: {'🟢 включена' if task['enabled'] else '🔴 выключена'}.\n"
            + (
                "Дополнительная обязанность: ровно один человек из назначенных на "
                + ", ".join(task["requires_any"]) + ".\n"
                if task.get("requires_any") else "Основная задача.\n"
            ) +
            "Выключенные задачи не читаются из графика. "
            "Для настройки напоминаний сначала включите задачу.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(
                    text="⏰ Напоминания и шаблоны",
                    callback_data=(
                        f"svc_tasks:notify:{service_id}:{task['position']}:"
                        f"{session_id}"
                    ),
                )],
                [InlineKeyboardButton(
                    text="⏸ Выключить задачу" if task["enabled"]
                    else "▶️ Включить задачу",
                    callback_data=(
                        f"svc_tasks:tog:{service_id}:{task['position']}:"
                        f"{session_id}"
                    ),
                )],
                [InlineKeyboardButton(
                    text="🔗 Обязанность внутри смены",
                    callback_data=f"svc_tasks:dep:{service_id}:{session_id}",
                )],
                [InlineKeyboardButton(
                    text="⬅️ К задачам",
                    callback_data=f"svc_tasks:open:{service_id}",
                )],
            ]),
            parse_mode=None,
        )

    async def show_task_dependency(message, actor_id, service_id, page=None):
        registry = service_registry()
        pending = pending_inputs[actor_id]
        tasks = registry.list_service_tasks(service_id)
        task = next(
            (item for item in tasks if item["position"] == pending["task_position"]), None,
        )
        if task is None:
            raise ValueError("Задача больше не существует — откройте список заново.")
        pending["step"] = "task_dependency"
        selected = pending.setdefault("dependency_anchors", list(task.get("requires_any", [])))
        session = pending["session_id"]
        choices = [item for item in tasks if item["name"] != task["name"] and item["enabled"]]
        page = pending.get("dependency_page", 0) if page is None else page
        page = max(0, min(page, max(0, (len(choices) - 1) // 3)))
        pending["dependency_page"] = page
        rows = layout_buttons([
            InlineKeyboardButton(
                text=("✅ " if item["name"] in selected else "⬜ ") + item["name"],
                callback_data=f"svc_tasks:danchor:{service_id}:{item['position']}:{session}",
            )
            for item in choices[page * 3:(page + 1) * 3]
        ])
        if len(choices) > 3:
            rows.append([
                InlineKeyboardButton(
                    text="⬅️ Стр.",
                    callback_data=f"svc_tasks:dpage:{service_id}:{max(0, page - 1)}:{session}",
                ),
                InlineKeyboardButton(
                    text="➡️ Стр.",
                    callback_data=f"svc_tasks:dpage:{service_id}:{page + 1}:{session}",
                ),
            ])
        rows += [
            [InlineKeyboardButton(text="✅ Сохранить",
                                  callback_data=f"svc_tasks:dsave:{service_id}:{session}")],
            [InlineKeyboardButton(text="Убрать зависимость",
                                  callback_data=f"svc_tasks:dclear:{service_id}:{session}")],
            [InlineKeyboardButton(text="⬅️ К задаче",
                                  callback_data=f"svc_tasks:dback:{service_id}:{session}")],
        ]
        await _send_dialog(
            message,
            f"📋 {task['name']} › Обязанность внутри смены\n\n"
            "Выберите основные задачи. Ответственным будет ровно один "
            "допущенный человек, назначенный на любую из выбранных задач "
            "в это воскресенье. Например: «старший» или «в смене».\n"
            "Одна дополнительная обязанность на служение. После сохранения "
            "пересчитайте черновик; утверждённый график автоматически не меняется.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
            _dialog_keep_rows=True,
            _dialog_keyboard_limit=sum(len(row) for row in rows),
            parse_mode=None,
        )

    def event_note_state(actor_id, **values):
        state = {
            "operation": "event_note", "service_id": "assembly",
            "token": secrets.token_hex(4), **values,
        }
        pending_inputs[actor_id] = state
        return state

    def event_note_button(label, action, state, value=""):
        return InlineKeyboardButton(
            text=label,
            callback_data=f"evn:{action}:{state['token']}:{value}",
        )

    async def show_event_note_month(message, actor_id, month=None):
        month = month or datetime.now(context.timezone).date().replace(day=1)
        state = event_note_state(actor_id, month=month.isoformat())
        rows = layout_buttons([
            event_note_button(
                f"📝 {day:%d.%m}", "day", state, day.isoformat(),
            )
            for day in (
                date.fromisoformat(value)
                for value in schedule_plan.free_run("assembly", month.year, month.month)["dates"]
            )
        ])
        rows.append([
            event_note_button("⬅️ Месяц", "month", state,
                              (month - timedelta(days=1)).replace(day=1).isoformat()),
            event_note_button("➡️ Месяц", "month", state,
                              (month_end(month.year, month.month) + timedelta(days=1)).isoformat()),
        ])
        rows.append([InlineKeyboardButton(
            text="⬅️ К настройкам Собрания",
            callback_data="service:service_setup:assembly",
        )])
        await _send_dialog(
            message, f"СОБРАНИЕ › Заметки › {month_title(month.year, month.month)}\n\n"
            "Выберите воскресное событие. Для отдельной отправки в чаты "
            "служений задайте дату и время и опубликуйте заметку.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
            _dialog_keep_rows=True,
            _dialog_keyboard_limit=sum(len(row) for row in rows),
            parse_mode=None,
        )

    async def show_event_note_detail(message, actor_id, sunday, notice=""):
        note = service_registry().get_event_note(sunday)
        state = event_note_state(
            actor_id, event_sunday=sunday,
            revision=note["revision"] if note else 0,
            targets=note["draft_targets"] if note else None,
        )
        targets = state["targets"]
        names = {item["id"]: item["name"] for item in service_registry().list_services()}
        audience = "все служения" if targets is None else ", ".join(
            names.get(item, item) for item in targets
        )
        send_at = note.get("draft_send_at") if note else None
        published_at = note.get("published_send_at") if note else None
        def display_send_at(value):
            return datetime.fromisoformat(value).strftime("%d.%m.%Y в %H:%M") if value else "не задано"
        text = (
            f"СОБРАНИЕ › Заметка › {date.fromisoformat(sunday):%d.%m.%Y}\n\n"
            f"{notice}\n\n" if notice else
            f"СОБРАНИЕ › Заметка › {date.fromisoformat(sunday):%d.%m.%Y}\n\n"
        )
        text += (
            f"Статус: {'опубликована' if note and note['published'] else 'черновик'}.\n"
            f"Адресаты черновика: {audience}.\n\n"
            f"Отправка черновика: {display_send_at(send_at)}.\n"
            f"Отправка опубликованной заметки: {display_send_at(published_at)}.\n\n"
            + (note["draft_body"] if note else "Текста пока нет. Нажмите «Изменить текст».")
            + "\n\nИзменения вступают в силу после публикации. "
              "Уже доставленная заметка повторно в тот же чат не отправляется."
        )
        buttons = [
            event_note_button("✏️ Изменить текст", "text", state),
            event_note_button("👥 Адресаты", "targets", state),
            event_note_button("🕒 Время отправки", "schedule", state),
            event_note_button("👁 Предпросмотр", "preview", state),
            event_note_button("✅ Опубликовать", "publish", state),
        ]
        if note and note["published"]:
            buttons.append(event_note_button("⛔ Снять с публикации", "unpublish", state))
        buttons.append(event_note_button("⬅️ К датам", "dates", state))
        await _send_dialog(
            message, text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[item] for item in buttons]),
            _dialog_keep_rows=True, _dialog_keyboard_limit=len(buttons),
            parse_mode=None,
        )

    async def show_event_note_targets(message, actor_id, page=0):
        state = pending_inputs[actor_id]
        selected = state["targets"]
        known = {item["id"]: item for item in service_registry().list_services()}
        services = [item for item in known.values() if item["enabled"]]
        services += [
            {"id": service_id, "name": known.get(service_id, {}).get("name", service_id),
             "enabled": False}
            for service_id in selected or []
            if service_id not in known or not known[service_id]["enabled"]
        ]
        state["token"] = secrets.token_hex(4)
        state["step"] = "targets"
        page = max(0, min(page, (len(services) - 1) // 5))
        state["page"] = page
        rows = [[event_note_button(
            ("✅ " if selected is None or item["id"] in selected else "⬜ ") + item["name"]
            + (" (недоступно — убрать)" if not item["enabled"] else ""),
            "target", state, item["id"],
        )] for item in services[page * 5:(page + 1) * 5]]
        if len(services) > 5:
            rows.append([
                event_note_button("⬅️ Стр.", "page", state, str(max(0, page - 1))),
                event_note_button("➡️ Стр.", "page", state, str(page + 1)),
            ])
        rows += [
            [event_note_button("🌐 Все служения", "all", state)],
            [event_note_button("✅ Сохранить адресатов", "targetsok", state)],
            [event_note_button("⬅️ К заметке", "edit", state)],
        ]
        await _send_dialog(
            message, "СОБРАНИЕ › Заметка › Адресаты\n\n"
            "Выберите служения. «Все служения» включает и добавленные в будущем. "
            "Недоступных сохранённых адресатов можно убрать. "
            "Адресаты сохранятся в черновике; публикация — отдельным действием.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
            _dialog_keep_rows=True,
            _dialog_keyboard_limit=sum(len(row) for row in rows),
            parse_mode=None,
        )

    @admin_router.callback_query(F.data.startswith("evn:"))
    async def on_event_notes(callback: CallbackQuery) -> None:
        if (not callback.from_user or not callback.message
                or callback.message.chat.type != ChatType.PRIVATE):
            await _answer_callback(callback)
            return
        actor_id = callback.from_user.id
        registry = service_registry()
        try:
            if registry is None or not is_service_admin(actor_id, "assembly"):
                raise PermissionError("Заметки доступны администраторам Собрания.")
            if callback.data == "evn:home":
                await clean_and_clear_pending_actor(actor_id)
                await show_event_note_month(callback.message, actor_id)
            else:
                parts = callback.data.split(":")
                state = pending_inputs.get(actor_id) or {}
                if (len(parts) != 4 or state.get("operation") not in
                        {"event_note", "event_note_input", "event_note_schedule_input"}
                        or state.get("token") != parts[2]):
                    raise ValueError("Этот экран устарел. Откройте заметки заново.")
                _, action, _, value = parts
                sunday = state.get("event_sunday")
                if action == "month":
                    await show_event_note_month(callback.message, actor_id, date.fromisoformat(value))
                elif action == "day":
                    await show_event_note_detail(callback.message, actor_id, value)
                elif action == "dates":
                    await show_event_note_month(
                        callback.message, actor_id, date.fromisoformat(sunday).replace(day=1),
                    )
                elif action == "edit":
                    await show_event_note_detail(callback.message, actor_id, sunday)
                elif action == "text":
                    state.update(operation="event_note_input", token=secrets.token_hex(4))
                    await _send_dialog(
                        callback.message, "Отправьте текст заметки (до 2000 символов).\n"
                        "Например: «После собрания общение в большом зале».\n"
                        "Это текст без автоматических упоминаний людей.",
                        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                            event_note_button("⬅️ К заметке", "edit", state),
                        ]]), parse_mode=None,
                    )
                elif action == "schedule":
                    if not registry.get_event_note(sunday):
                        raise ValueError("Сначала сохраните текст заметки.")
                    state.update(operation="event_note_schedule_input", token=secrets.token_hex(4))
                    await _send_dialog(
                        callback.message,
                        "Введите дату и время отправки в часовом поясе бота: "
                        "ДД.ММ.ГГГГ ЧЧ:ММ. Например: 18.11.2026 19:00.\n"
                        "Отправка должна быть в будущем и не позже дня собрания.\n"
                        "Для отключения отдельной отправки введите «нет».",
                        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                            event_note_button("⬅️ К заметке", "edit", state),
                        ]]), parse_mode=None,
                    )
                elif action in {"targets", "target", "all", "page"}:
                    if action == "target":
                        available = {item["id"] for item in registry.list_services() if item["enabled"]}
                        if value not in available and value not in (state["targets"] or []):
                            raise ValueError("Служение недоступно — откройте заметку заново.")
                        selected = set(available if state["targets"] is None else state["targets"])
                        selected.symmetric_difference_update({value})
                        state["targets"] = sorted(selected)
                    elif action == "all":
                        state["targets"] = None
                    await show_event_note_targets(
                        callback.message, actor_id, int(value) if action == "page" else state.get("page", 0),
                    )
                elif action == "targetsok":
                    note = registry.get_event_note(sunday)
                    if not note:
                        raise ValueError("Сначала сохраните текст заметки.")
                    registry.save_event_note_draft(
                        actor_id, sunday, note["draft_body"], state["targets"],
                        expected_revision=state["revision"],
                    )
                    await show_event_note_detail(callback.message, actor_id, sunday, "Адресаты сохранены в черновике.")
                elif action == "preview":
                    note = registry.get_event_note(sunday)
                    state["token"] = secrets.token_hex(4)
                    await _send_dialog(
                        callback.message,
                        "👁 Черновик:\n" + (note["draft_body"] if note else "Нет текста.")
                        + "\n\nСейчас опубликовано:\n"
                        + (note["published_body"] if note and note["published"] else "Не опубликовано."),
                        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                            event_note_button("⬅️ К заметке", "edit", state),
                        ]]), parse_mode=None,
                    )
                elif action in {"publish", "unpublish"}:
                    state["token"] = secrets.token_hex(4)
                    state["step"] = action
                    await _send_dialog(
                        callback.message,
                        ("Опубликовать черновик для выбранных служений?"
                         if action == "publish" else "Убрать заметку из будущих напоминаний?")
                        + "\nЕсли задано время, сообщение будет отправлено в чаты "
                          "по расписанию. Уже доставленные сообщения не изменятся "
                          "и повторно не отправятся.",
                        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                            [event_note_button("✅ Подтвердить", action + "ok", state)],
                            [event_note_button("⬅️ К заметке", "edit", state)],
                        ]), parse_mode=None,
                    )
                elif action in {"publishok", "unpublishok"}:
                    if state.get("step") != action[:-2]:
                        raise ValueError("Подтверждение устарело.")
                    if action == "publishok":
                        note = registry.get_event_note(sunday)
                        send_at = note.get("draft_send_at") if note else None
                        now = datetime.now(context.timezone)
                        previous_schedule = (
                            note and note["published"]
                            and send_at == note["published_send_at"]
                            and now.date() <= date.fromisoformat(sunday)
                        )
                        if send_at and not previous_schedule and datetime.fromisoformat(
                            send_at
                        ).replace(tzinfo=context.timezone) <= now:
                            raise ValueError("Время отправки прошло. Укажите новое время.")
                    method = registry.publish_event_note if action == "publishok" else registry.unpublish_event_note
                    method(actor_id, sunday, expected_revision=state["revision"])
                    await show_event_note_detail(callback.message, actor_id, sunday, "✅ Сохранено.")
                else:
                    raise ValueError("Неизвестное действие — откройте заметки заново.")
        except (ValueError, PermissionError) as exc:
            await _answer_callback(callback, f"❌ {exc}")
            return
        except sqlite3.Error:
            debug.exception("Не удалось открыть или изменить заметку события")
            await _answer_callback(callback, "❌ Ошибка базы данных. Попробуйте ещё раз.")
            return
        await _answer_callback(callback)

    async def show_personal_reminder_summary(
        message: Message,
        actor_id: int,
        service_id: str,
        back_callback_data: str,
        *,
        month: date | None = None,
        page: int = 0,
    ) -> None:
        registry = service_registry()
        service = service_by_id(registry, service_id) if registry else None
        if registry is None or service is None:
            await _send_dialog(message, "Служение не найдено.", parse_mode=None)
            return

        now = datetime.now(context.timezone)
        current = now.date().replace(day=1)
        following = date(*next_month(current.year, current.month), 1)
        month = month or current
        if month not in (current, following):
            raise ValueError("Доступны только текущий и следующий месяц.")
        pending = pending_inputs[actor_id]
        token = secrets.token_urlsafe(8)
        pending["step"] = "personal_summary"
        pending["personal_summary"] = {
            "token": token, "service_id": service_id,
            "back": back_callback_data,
        }
        try:
            entries = personal_reminder_forecast(
                registry, actor_id, service_id, month, now,
            )
        except (sqlite3.Error, OSError, RuntimeError, ValueError):
            debug.exception("Не удалось построить сводку личных напоминаний")
            entries = [
                "❌ Не удалось прочитать график и настройки уведомлений. "
                "Прогноз недоступен. Нажмите «Обновить» или вернитесь позже."
            ]
        pages = [""]
        for entry in entries:
            for chunk in _telegram_text_chunks(entry, 2800):
                combined = "\n\n".join(filter(None, (pages[-1], chunk)))
                if visible_units(combined) > 2800:
                    pages.append(chunk)
                else:
                    pages[-1] = combined
        page = min(max(page, 0), len(pages) - 1)

        def action(
            label: str, selected: date, number: int = 0,
        ) -> InlineKeyboardButton:
            return InlineKeyboardButton(
                text=label,
                callback_data=f"psum:{token}:{selected:%Y%m}:{number}",
            )

        rows = [[
            action("📅 Текущий месяц", current),
            action("📅 Следующий месяц", following),
        ]]
        paging = []
        if page:
            paging.append(action("⬅️ Предыдущее", month, page - 1))
        if page + 1 < len(pages):
            paging.append(action("➡️ Далее", month, page + 1))
        if paging:
            rows.append(paging)
        rows.append([action("🔄 Обновить", month, page)])
        rows.append([InlineKeyboardButton(
            text="⬅️ К напоминаниям", callback_data=back_callback_data,
        )])
        await _send_dialog(
            message,
            f"📊 {service_label(service)} › Напомнить › Сводка\n"
            f"{month_title(month.year, month.month)} · часовой пояс: {context.timezone}\n"
            f"Прогноз на {now:%d.%m.%Y %H:%M}, не гарантия доставки Telegram.\n\n"
            f"{pages[page]}\n\nСтраница {page + 1}/{len(pages)}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
            parse_mode=None,
            _dialog_text_limit=4000,
            _dialog_keep_rows=True,
            _dialog_keyboard_limit=6,
        )

    @admin_router.callback_query(F.data.startswith("psum:"))
    async def on_personal_summary_page(callback: CallbackQuery) -> None:
        if (
            not callback.from_user or not callback.message
            or callback.message.chat.type != ChatType.PRIVATE
        ):
            await _answer_callback(callback)
            return
        parts = callback.data.split(":")
        pending = pending_inputs.get(callback.from_user.id)
        state = pending.get("personal_summary") if pending else None
        if (
            len(parts) != 4 or state is None
            or pending.get("step") != "personal_summary"
            or state["token"] != parts[1]
        ):
            await _answer_callback(callback, "⚠️ Этот экран уже устарел.")
            return
        try:
            if not is_service_admin(callback.from_user.id, state["service_id"]):
                raise PermissionError("Недостаточно прав.")
            month = datetime.strptime(parts[2], "%Y%m").date()
            page = int(parts[3])
            if page < 0:
                raise ValueError("Некорректная страница.")
            await show_personal_reminder_summary(
                callback.message, callback.from_user.id, state["service_id"],
                state["back"], month=month, page=page,
            )
        except (PermissionError, ValueError) as exc:
            await _answer_callback(callback, f"⚠️ {exc}")
            return
        except sqlite3.Error:
            debug.exception("Не удалось проверить права на сводку")
            await _answer_callback(callback, "❌ Не удалось проверить права.")
            return
        await _answer_callback(callback)

    async def show_service_reminder_choices(
        message: Message, actor_id: int, service_id: str,
    ) -> None:
        registry = service_registry()
        service = service_by_id(registry, service_id) if registry else None
        if registry is None or service is None:
            await _send_dialog(message, "Служение не найдено.", parse_mode=None)
            return
        tasks = registry.list_service_tasks(service_id)
        if not tasks:
            await ask_for_service_parameters(
                message,
                actor_id,
                "service_notify",
                service_id,
                service_label(service),
                notify_scoped=True,
                notify_no_tasks=True,
                open_reminders=True,
            )
            return

        session_id = secrets.token_urlsafe(9)
        pending_inputs[actor_id] = {
            "operation": "service_tasks",
            "service_id": service_id,
            "session_id": session_id,
            "step": "reminder_choice",
        }
        lines = [
            f"🔔 {service_label(service)} · напоминания",
            "",
            "ℹ️ Выберите общие напоминания или задачу.",
        ]
        if any(not task["enabled"] for task in tasks):
            lines += [
                "",
                "🔴 Выключенную задачу сначала включите через "
                "«📋 Задачи служения».",
            ]
        buttons = [
            InlineKeyboardButton(
                text="🔔 Общие",
                callback_data=(
                    f"svc_tasks:notify:{service_id}:s:{session_id}"
                ),
            )
        ]
        buttons.append(InlineKeyboardButton(
            text="📊 Сводка",
            callback_data=f"service:service_reminder_summary:{service_id}",
        ))
        if len(tasks) <= 3:
            buttons.extend(
                InlineKeyboardButton(
                    text=(
                        f"{'🟢' if task['enabled'] else '🔴'} "
                        f"{task_emoji(task['name'])} {task['name']}"
                    )[:60],
                    callback_data=(
                        f"svc_tasks:notify:{service_id}:"
                        f"{task['position']}:{session_id}"
                    ),
                )
                for task in tasks
            )
        else:
            buttons.append(InlineKeyboardButton(
                text="📋 Выбрать задачу",
                callback_data=(
                    f"svc_tasks:pick_notify:{service_id}:{session_id}"
                ),
            ))
        buttons.append(InlineKeyboardButton(
            text="⬅️ К карточке служения",
            callback_data=f"service:service_settings:{service_id}",
        ))
        await _send_dialog(
            message,
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=layout_buttons(buttons)
            ),
            _dialog_keyboard_limit=6,
            parse_mode=None,
        )

    async def show_service_reminder_tasks(
        message: Message, service_id: str, session_id: str,
    ) -> None:
        registry = service_registry()
        service = service_by_id(registry, service_id) if registry else None
        if service is None or registry is None:
            await _send_dialog(message, "Служение не найдено.", parse_mode=None)
            return
        tasks = registry.list_service_tasks(service_id) if registry else []
        buttons = [
            InlineKeyboardButton(
                text=(
                    f"{'🟢' if task['enabled'] else '🔴'} "
                    f"{task_emoji(task['name'])} {task['name']}"
                )[:60],
                callback_data=(
                    f"svc_tasks:pick_notify:{service_id}:"
                    f"{task['position']}:{session_id}"
                ),
            )
            for task in tasks
        ]
        buttons.append(InlineKeyboardButton(
            text="⬅️ К выбору напоминаний",
            callback_data=f"service:service_reminders:{service_id}",
        ))
        lines = [
            f"📋 {service_label(service)} · выберите задачу для напоминаний"
        ]
        if any(not task["enabled"] for task in tasks):
            lines += [
                "",
                "🔴 Выключенную задачу сначала включите через "
                "«📋 Задачи служения».",
            ]
        await _send_dialog(
            message,
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=layout_buttons(buttons)
            ),
            _dialog_keyboard_limit=6,
            parse_mode=None,
        )

    async def show_task_days(
        message: Message, service_id: str, session_id: str,
        notice: str | None = None,
    ) -> None:
        """«📅 Дни задач»: у задачи свой день недели (спевка — ср, вс −4).
        Тогда график показывается по неделям."""
        registry = service_registry()
        service = service_by_id(registry, service_id)
        tasks = [
            task for task in registry.list_service_tasks(service_id)
            if task["enabled"]
        ]
        lines = [
            f"📅 {service_label(service)} · дни задач",
            "",
            "ℹ️ Задача со своим днём выполняется в этот день недели "
            "воскресенья графика (спевка — ср, «вс −4»). Тогда график "
            "показывается по неделям: «Неделя 26.10–01.11», под ней дни и "
            "задачи. Без своего дня — дни служения раздела «📅 График».",
            "",
            *(
                f"{task_emoji(task['name'])} {task['name']} — "
                + (
                    member_day_label(task["day_offset"])
                    if task["day_offset"] is not None else "дни служения"
                )
                for task in tasks
            ),
        ]
        if notice:
            lines = [notice, ""] + lines
        buttons = [
            InlineKeyboardButton(
                text=f"📅 {task['name']}"[:60],
                callback_data=(
                    f"svc_tasks:dpick:{service_id}:{task['position']}:"
                    f"{session_id}"
                ),
            )
            for task in tasks
        ]
        rows = layout_buttons(buttons) if buttons else []
        rows.append([InlineKeyboardButton(
            text="⬅️ К задачам",
            callback_data=f"svc_tasks:open:{service_id}",
        )])
        await _send_dialog(
            message,
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
            _dialog_keyboard_limit=sum(len(row) for row in rows),
            parse_mode=None,
        )

    async def show_task_week(
        message: Message, service_id: str, session_id: str,
        notice: str | None = None,
    ) -> None:
        """«🔗 В одну неделю»: отмеченные задачи один человек берёт вместе
        (звук в вс и спевка в ср) — у всех, у кого они есть; личная
        настройка в карточке человека главнее."""
        registry = service_registry()
        service = service_by_id(registry, service_id)
        tasks = [
            task for task in registry.list_service_tasks(service_id)
            if task["enabled"]
        ]
        lines = [
            f"🔗 {service_label(service)} · в одну неделю",
            "",
            "ℹ️ Отмеченные задачи один человек берёт вместе в одну неделю "
            "(звук в вс и спевка в ср) — у всех, у кого есть обе. Если "
            "вместе некем — по отдельности. Для одного человека — "
            "«🔗 В одну неделю» в его карточке («👥 Ответственные»).",
        ]
        if notice:
            lines = [notice, ""] + lines
        buttons = [
            InlineKeyboardButton(
                text=f"{'🔗' if task['same_week'] else '⬜'} {task['name']}"[:60],
                callback_data=(
                    f"svc_tasks:wtog:{service_id}:{task['position']}:"
                    f"{session_id}"
                ),
            )
            for task in tasks
        ]
        rows = layout_buttons(buttons) if buttons else []
        rows.append([InlineKeyboardButton(
            text="⬅️ К задачам",
            callback_data=f"svc_tasks:open:{service_id}",
        )])
        await _send_dialog(
            message,
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
            _dialog_keyboard_limit=sum(len(row) for row in rows),
            parse_mode=None,
        )

    def people_label(task: dict) -> str:
        # Задача служения (people_needed / people_max) или задача графика
        # (registry.plan_tasks: needed / max).
        low = task.get("needed", task.get("people_needed"))
        high = task.get("max", task.get("people_max"))
        return str(low) if high == low else f"{low}–{high}"

    PEOPLE_CHOICES = (
        (1, 1), (2, 2), (3, 3), (4, 4), (5, 5),
        (1, 2), (2, 3), (3, 4), (4, 5),
    )

    async def show_task_people(
        message: Message, service_id: str, back: str = "o",
        notice: str | None = None,
    ) -> None:
        """«👥 Нужно людей»: сколько человек ставить в задачу на одно
        воскресенье при распределении графика по опросу (этап 5) — число
        или диапазон «минимум–максимум». back: «o» — к опросу, «t» — к
        задачам служения."""
        registry = service_registry()
        service = service_by_id(registry, service_id) if registry else None
        if service is None:
            await _send_dialog(message, "Служение не найдено.", parse_mode=None)
            return
        # Служение без задач — одна строка с его названием.
        tasks = sort_tasks_for_display(
            registry.plan_tasks(service_id), service_id, name=lambda task: task["name"],
        )
        lines = [
            f"👥 {service_label(service)} · нужно людей",
            "",
            "ℹ️ Сколько человек бот ставит в задачу на одно воскресенье при "
            "составлении графика. Диапазон «3–4»: минимум обязателен (иначе "
            "⚠️), сверх него — если есть кем.",
            "",
            *(
                f"{task_emoji(task['name'])} {task['name']} — "
                f"{people_label(task)}"
                for task in tasks
            ),
        ]
        if notice:
            lines = [notice, ""] + lines
        buttons = [
            readiness_button(
                f"👥 {task['name']} · {people_label(task)}"[:60],
                "ppick", service_id, f"{task['position']}.{back}",
            )
            for task in tasks
        ]
        rows = layout_buttons(buttons) if buttons else []
        rows.append([
            readiness_button("⬅️ К опросу", "open", service_id)
            if back == "o" else InlineKeyboardButton(
                text="⬅️ К задачам",
                callback_data=f"svc_tasks:open:{service_id}",
            )
        ])
        await _send_dialog(
            message,
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
            _dialog_keyboard_limit=sum(len(row) for row in rows),
            _dialog_keep_rows=True,
            parse_mode=None,
        )

    async def show_task_people_choice(
        message: Message, service_id: str, task: dict, back: str
    ) -> None:
        current = (task["needed"], task["max"])
        buttons = [
            readiness_button(
                f"{'✅' if (low, high) == current else '👥'} "
                + (str(low) if low == high else f"{low}–{high}"),
                "pset", service_id, f"{task['position']}.{low}.{high}.{back}",
            )
            for low, high in PEOPLE_CHOICES
        ]
        rows = [buttons[index:index + 3] for index in range(0, len(buttons), 3)]
        rows.append([readiness_button(
            "⬅️ К списку", "people", service_id, back
        )])
        await _send_dialog(
            message,
            f"👥 {task_emoji(task['name'])} {task['name']}\n\n"
            f"Сейчас: {people_label(task)}. Сколько человек нужно на одно "
            "воскресенье? Диапазон — минимум и максимум.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
            _dialog_keep_rows=True,
            _dialog_keyboard_limit=sum(len(row) for row in rows),
            parse_mode=None,
        )

    # Окно дней служения от воскресенья (Уборка — сб–пн).
    DAY_WINDOWS = {
        (0, 0): "только вс", (-1, 0): "сб–вс", (0, 1): "вс–пн",
        (-1, 1): "сб–пн", (-2, 0): "пт–вс", (-2, 1): "пт–пн",
    }
    READINESS_TIMES = ("09:00", "10:00", "12:00", "18:00", "19:00", "20:00")
    READINESS_DEADLINES = ("12:00", "18:00", "20:00", "21:00", "22:00", "23:00")

    def readiness_button(text: str, action: str, service_id: str,
                         arg: str | None = None) -> InlineKeyboardButton:
        data = f"rdy:{action}:{service_id}" + (f":{arg}" if arg else "")
        return InlineKeyboardButton(text=text, callback_data=data)

    async def show_month_graph(
        message: Message, actor_id: int, service_id: str, page: int = 0,
        *, summary: bool = False, month: date | None = None,
    ) -> None:
        registry = service_registry()
        service = registry.get_service(service_id)
        current_month = datetime.now(context.timezone).date().replace(day=1)
        month = month or current_month
        request = {"operation": "graph_view", "service_id": service_id}
        pending_inputs[actor_id] = request
        try:
            text = await asyncio.to_thread(
                schedule_plan.month_overview, registry, actor_id, service_id, month,
                include_load=summary,
            )
        except (sqlite3.Error, OSError, RuntimeError, ValueError):
            debug.exception("Не удалось прочитать месячный график")
            text = (
                "❌ Не удалось прочитать сохранённый график. "
                "Вернитесь к служению и откройте «График» повторно."
            )
        if pending_inputs.get(actor_id) is not request:
            return
        chunks = screen_text_chunks(text, 2800)
        page = min(max(page, 0), len(chunks) - 1)
        rows = [[
            readiness_button("🔁 Замены", "repl", service_id),
            InlineKeyboardButton(
                text="📥 Загрузить с диска",
                callback_data=f"service:service_excel:{service_id}",
            ),
        ], [
            readiness_button("🗳 Опрос", "open", service_id),
            readiness_button(
                "🧮 Авто", "pfree", service_id, month.strftime("%Y%m"),
            ),
        ], [readiness_button("📊 Сводка", "summary", service_id)]]
        if summary:
            following = month_end(current_month.year, current_month.month) + timedelta(days=1)
            rows = [[
                readiness_button("📅 Текущий месяц", "summary", service_id,
                                 current_month.strftime("%Y%m")),
                readiness_button("📅 Следующий месяц", "summary", service_id,
                                 following.strftime("%Y%m")),
            ]]
        paging = []
        action = "summary" if summary else "month"

        def page_arg(number):
            return f"{month:%Y%m}.{number}" if summary else str(number)

        if page:
            paging.append(readiness_button(
                "⬅️ Предыдущее", action, service_id, page_arg(page - 1),
            ))
        if page + 1 < len(chunks):
            paging.append(readiness_button(
                "➡️ Далее", action, service_id, page_arg(page + 1),
            ))
        if paging:
            rows.append(paging)
        if summary:
            rows += [
                [readiness_button("🔄 Обновить", "summary", service_id, page_arg(page))],
                [readiness_button("⬅️ К графику", "month", service_id)],
            ]
        else:
            rows.append([InlineKeyboardButton(
                text="⬅️ К служению",
                callback_data=f"service:service_settings:{service_id}",
            )])
        await _send_dialog(
            message,
            f"📅 {service_label(service)} › График"
            + (" › Сводка" if summary else "") + "\n"
            f"{month_title(month.year, month.month)}\n\n{chunks[page]}"
            + (f"\n\nСтраница {page + 1}/{len(chunks)}" if len(chunks) > 1 else ""),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
            parse_mode=None, _dialog_text_limit=4000,
            _dialog_keyboard_limit=max(6, sum(len(row) for row in rows)),
            _dialog_keep_rows=True,
        )

    async def show_readiness(
        message: Message, service_id: str, notice: str | None = None
    ) -> None:
        """Карточка служения → «📅 График»: опрос готовности и график месяца
        (этап 5)."""
        registry = service_registry()
        service = registry.get_service(service_id) if registry else None
        if service is None:
            await _send_dialog(message, "Служение не найдено.", parse_mode=None)
            return
        tz = context.timezone
        now = datetime.now(tz)
        settings = registry.get_readiness_settings(service_id)
        year, month = readiness.target_month(now.date())
        start, deadline = readiness.schedule(settings, year, month, tz)
        run = registry.get_readiness_run(
            service_id, f"{year:04d}-{month:02d}"
        )
        title = month_title(year, month)
        month_arg = f"{year:04d}{month:02d}"
        weekdays = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")
        key_month = f"{year:04d}-{month:02d}"
        early_plan = registry.get_schedule_plan(service_id, key_month)
        planned_free = early_plan is not None and early_plan["run_id"] is None
        # Автозапуск месяца не состоялся: попытка не удалась, окно прошло
        # или автозапуск выключен и время прошло, — а опроса и графика без
        # опроса нет.
        failed = run is None and registry.has_readiness_notice(
            service_id, key_month, "launch"
        )
        missed = run is None and not planned_free and (
            failed or now >= deadline
            or (now >= start and not settings["enabled"])
        )

        def autostart_lines() -> list[str]:
            when_start = (
                f"{weekdays[start.weekday()]} {start:%d.%m} в "
                f"{settings['start_time']}"
            )
            when_end = (
                f"{weekdays[deadline.weekday()]} {deadline:%d.%m} в "
                f"{settings['deadline_time']}"
            )
            if missed:
                if failed:
                    head = (
                        f"▶️ Автозапуск {when_start} не удался — повторно "
                        "бот не запускает."
                    )
                elif not settings["enabled"]:
                    head = (
                        f"▶️ Автозапуск выключен — опрос на {title} не "
                        f"запускался (окно: {when_start} – {when_end})."
                    )
                elif settings.get("enabled_at") and datetime.fromisoformat(
                    settings["enabled_at"]
                ) > start:
                    # Включён после начала окна — этот месяц не ожидался.
                    head = (
                        f"▶️ Автозапуск включён после времени запуска "
                        f"({when_start}) — опрос на {title} не запускался."
                    )
                else:
                    head = (
                        f"▶️ Опрос на {title} ожидался {when_start} – "
                        f"{when_end} — сроки прошли, опрос не запускался."
                    )
                return [
                    head, f"⏳ Срок опроса: {settings['duration_days']} дн.",
                ]
            return [
                f"▶️ Запуск: {when_start} (последняя полная неделя)",
                f"⏳ Срок: {settings['duration_days']} дн. → {when_end}",
            ]
        lines = [
            f"🗳 {service_label(service)} › График › Опрос",
            "",
            "🗳 Опрос готовности — автозапуск: " + (
                "🟢 включён" if settings["enabled"]
                else "🔴 выключен (запуск только вручную)"
            ),
            *autostart_lines(),
            "🔔 Напомнить не ответившим: " + (
                f"за {settings['remind_days']} дн. до срока"
                if settings["remind_days"] else "выключено"
            ),
            "📡 Чат: " + (
                "привязан" if service.get("group_chat_id")
                else "⚠️ не привязан — опрос не запустится"
            ),
            "",
        ]
        rows: list[list[InlineKeyboardButton]] = []
        plan = registry.get_schedule_plan(service_id, f"{year:04d}-{month:02d}")

        def plan_status(plan: dict) -> str:
            return "📋 График: " + (
                "черновик, не утверждён" if plan["approved"] is None
                else "есть неопубликованные правки" if plan["dirty"]
                else "✅ утверждён"
            ) + (" (без опроса)" if plan["run_id"] is None else "")

        # График без опроса — пока опроса месяца нет или он пропущен.
        free_plan = plan if plan is not None and plan["run_id"] is None and (
            run is None or run["status"] == "skipped"
        ) else None
        if run is None:
            lines.append(
                f"Опрос на {title}: ещё не запускался"
                + (
                    " — график составлен без опроса, автоматически опрос "
                    "не запустится (запуск вручную составит график по "
                    "ответам заново)."
                    if free_plan is not None else "."
                )
            )
            if missed and not service.get("enabled", True):
                lines.append(
                    "❗ Служение выключено — опрос не запустится. Включите "
                    "служение или составьте график без опроса («🧮 "
                    "Составить без опроса») и отправьте его."
                )
            elif missed and not service.get("group_chat_id"):
                lines.append(
                    "❗ Чат служения не привязан — опрос не запустится. "
                    "Привяжите чат или составьте график без опроса («🧮 "
                    "Составить без опроса»)."
                )
            elif missed:
                lines.append(
                    "❗ Сейчас нужно запустить опрос вручную («🗳 Запустить "
                    "опрос») или составить график без опроса («🧮 Составить "
                    "без опроса») и отправить его."
                )
            rows.append([
                readiness_button(
                    f"🗳 Запуск {title.split()[0][:3].capitalize()}",
                    "start", service_id, month_arg,
                ),
                readiness_button(
                    f"⏭ Пропуск {title.split()[0][:3].capitalize()}",
                    "skip", service_id, month_arg,
                ),
            ])
        elif run["status"] == "skipped":
            lines.append(
                f"Опрос на {title}: ⏭ пропущен — автоматически не "
                "запустится. График можно составить без опроса."
            )
            rows.append([
                readiness_button(
                    "↩️ Вернуть опрос", "unskip", service_id, month_arg
                ),
            ])
        elif run["status"] == "open":
            lines.append(
                f"Опрос на {title}: 🟢 идёт до "
                f"{readiness.local_text(run['deadline'], tz)}."
            )
            rows.append([
                readiness_button("📊 Ход опроса", "progress", service_id, month_arg),
                readiness_button("⏳ +1 день", "extend", service_id, month_arg),
                readiness_button("⏹ Завершить", "finish", service_id, month_arg),
            ])
        else:
            lines.append(f"Опрос на {title}: ✅ завершён.")
            if plan is not None and plan["run_id"] == run["id"]:
                lines.append(plan_status(plan))
            rows.append([
                readiness_button("📋 График", "plan", service_id, month_arg),
                readiness_button("📊 Итог опроса", "progress", service_id, month_arg),
            ])
        if free_plan is not None:
            lines.append(plan_status(free_plan))
        window = (settings["day_from"], settings["day_to"])
        lines.append("📆 Дни служения: " + DAY_WINDOWS.get(window, "свои"))
        lines.append(
            "🔢 Служений в месяц: "
            + month_limit_label(service_month_limit(registry, service_id))
        )
        lines.append(
            "🗳 Наоборот: " + (
                "включён — отмеченные даты недоступны"
                if settings["reverse_poll"]
                else "выключен — отмеченные даты доступны"
            )
        )
        current_key = f"{now.year:04d}-{now.month:02d}"
        current_plan = registry.get_schedule_plan(service_id, current_key)
        current_run = registry.get_readiness_run(service_id, current_key)
        current_title = month_title(now.year, now.month).split()[0].capitalize()
        target_month_short = title.split()[0][:3].capitalize()
        current_graph_action = (
            "plan" if plan_current(current_run, current_plan) else "cgraph"
        )
        if run is not None and run["status"] == "open":
            target_graph_action = "pprev"
            target_graph_text = f"📊 График {target_month_short}"
        elif free_plan is not None:
            target_graph_action = "plan"
            target_graph_text = f"📋 График {target_month_short} (авто)"
        elif run is not None and run["status"] == "closed" and plan is not None:
            target_graph_action = "plan"
            target_graph_text = f"📋 График {target_month_short}"
        else:
            target_graph_action = "pfree"
            target_graph_text = f"🧮 График {target_month_short} (авто)"

        rows = [
            [
                readiness_button(
                    "🔕 Выключить" if settings["enabled"] else "🟢 Включить",
                    "toggle", service_id,
                ),
                readiness_button("👁 Обзор", "overview", service_id),
            ],
            *rows,
            [
                readiness_button(
                    "↔️ «Наоборот» вкл" if settings["reverse_poll"]
                    else "↔️ «Наоборот» выкл",
                    "rev", service_id,
                ),
                readiness_button("📝 Тексты", "texts", service_id),
            ],
            [
                readiness_button("⏳ Срок", "dur", service_id),
                readiness_button("🕒 Начало", "time", service_id),
                readiness_button("🏁 Окончание", "dlt", service_id),
            ],
            [
                readiness_button("👥 Команда", "people", service_id, "o"),
                readiness_button("🔢 В месяц", "lim", service_id),
                readiness_button("🔔 Напоминание", "rem", service_id),
            ],
            [
                readiness_button("📆 Дни служения", "days", service_id),
                readiness_button("📅 Свой день", "own", service_id),
            ],
            [
                readiness_button(
                    f"📋 График {current_title}", current_graph_action,
                    service_id, current_key.replace("-", ""),
                ),
                readiness_button(
                    target_graph_text, target_graph_action, service_id,
                    month_arg,
                ),
            ],
            [
                readiness_button("⬅️ К графику", "month", service_id),
                readiness_button("❓ Справка", "help", service_id),
            ],
        ]
        if run is not None and run["status"] == "open":
            rows.append([
                readiness_button("📋 Предварительный график", "pprev",
                                 service_id, month_arg),
                readiness_button("🔄 Перезапустить", "restart", service_id,
                                 month_arg),
            ])
        elif run is not None and run["status"] == "closed":
            rows.append([
                readiness_button("▶️ Запустить заново", "restart", service_id,
                                 month_arg),
            ])
        if notice:
            lines = [notice, ""] + lines
        await _send_dialog(
            message,
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
            _dialog_keyboard_limit=sum(len(row) for row in rows),
            _dialog_keep_rows=True,
            # Экран раздела — одним сообщением (кнопки под всем текстом).
            _dialog_text_limit=TELEGRAM_TEXT_LIMIT,
            parse_mode=None,
        )

    async def show_readiness_overview(message: Message, service_id: str) -> None:
        """Preview the actual intro, Telegram poll question, and options."""
        registry = service_registry()
        service = registry.get_service(service_id)
        if service is None:
            await _send_dialog(message, "Служение не найдено.", parse_mode=None)
            return
        now = datetime.now(context.timezone)
        year, month = readiness.target_month(now.date())
        key = f"{year:04d}-{month:02d}"
        settings = registry.get_readiness_settings(service_id)
        run = registry.get_readiness_run(service_id, key)
        deadline = readiness.preview_deadline(
            run, settings, year, month, now,
        )
        intro = readiness.intro_text(
            service, registry.readiness_people(service_id), year, month,
            deadline, context.timezone, settings.get("intro_template"),
            readiness.phrase(settings, "own_days"), settings["reverse_poll"],
        )
        question = readiness.question_text(
            service, year, month, settings.get("question_template"),
            settings["reverse_poll"],
        )
        options = readiness.poll_options(
            registry, service_id, year, month,
            reverse_poll=settings["reverse_poll"],
        )
        labels = [item["label"] for item in options]
        if settings["reverse_poll"]:
            labels.append("✅ Смогу в любые даты")
        labels.append(readiness.phrase(settings, "cant"))
        chat_note = (
            "⚠️ Чат служения не привязан — опрос не удастся отправить.\n\n"
            if not service.get("group_chat_id") else ""
        )
        text = (
            f"👁 Обзор опроса · {service_label(service)} · "
            f"{month_title(year, month)}\n\n"
            "💬 Сообщение перед опросом:\n"
            f"{intro}\n\n"
            "📊 Опрос в Telegram:\n"
            f"{question}\n\n"
            "Варианты ответа (можно выбрать несколько):\n"
            + "\n".join(f"• {label}" for label in labels)
            + "\n\nℹ️ Это только предпросмотр — сообщения в чат не отправлены."
        )
        await _send_dialog(
            message, chat_note + text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                readiness_button("⬅️ К опросу", "open", service_id),
            ]]),
            _dialog_text_limit=TELEGRAM_TEXT_LIMIT,
            parse_mode=None,
        )

    READINESS_TEXTS = {
        "intro": (
            "intro_template", "📝 Текст опроса",
            "Сообщение в чате служения перед опросом.", READINESS_INTRO_HELP,
        ),
        "rem": (
            "reminder_template", "💬 Текст напоминания",
            "Личное сообщение тем, кто не ответил, за «Напоминание» дней "
            "до срока.", READINESS_REMINDER_HELP,
        ),
        "q": (
            "question_template", "❓ Вопрос опроса",
            "Вопрос над вариантами опроса в чате служения (до 300 "
            "символов).", READINESS_QUESTION_HELP,
        ),
        "chat": (
            "chat_reminder_template", "📣 Напоминание в чат",
            "Общее сообщение в чате служения (ответом на опрос) в день "
            "напоминания — без имён, для всех, кто ещё не проголосовал.",
            READINESS_REMINDER_HELP,
        ),
        "done": (
            "closed_template", "✅ Текст завершения",
            "Сообщение в чате служения (ответом на опрос), когда опрос "
            "завершён.", READINESS_CLOSED_HELP,
        ),
        "pub": (
            "plan_title_template", "📅 Заголовок графика",
            "Первая строка графика в чате служения, когда график "
            "утверждён.", PLAN_TITLE_HELP,
        ),
        "pubupd": (
            "plan_update_template", "📅 Заголовок обновления",
            "Первая строка графика в чате служения, когда опубликованный "
            "график изменили.", PLAN_TITLE_HELP,
        ),
    }
    # Короткие фразы — тоже тексты («ph.<имя>»), поле в настройках — phrases.
    for _name, (_default, _fields, _limit, _label) in READINESS_PHRASES.items():
        READINESS_TEXTS[f"ph.{_name}"] = (
            "phrases", f"🔤 «{_default}»", _label, tuple(sorted(_fields)),
        )

    def readiness_text_value(settings: dict, kind: str) -> str | None:
        """Свой текст шаблона или фразы (None — встроенный)."""
        if kind.startswith("ph."):
            return settings["phrases"].get(kind[3:])
        return settings[READINESS_TEXTS[kind][0]]

    def save_readiness_text(
        registry, actor_id: int, service_id: str, kind: str, text: str | None,
    ) -> None:
        if kind.startswith("ph."):
            registry.set_readiness_phrase(actor_id, service_id, kind[3:], text)
        else:
            registry.set_readiness_settings(
                actor_id, service_id, **{READINESS_TEXTS[kind][0]: text}
            )

    async def show_readiness_phrases(message: Message, service_id: str) -> None:
        """«🔤 Фразы»: короткие слова и фразы в сообщениях участникам."""
        settings = service_registry().get_readiness_settings(service_id)
        lines = [
            "🔤 Фразы", "",
            "Короткие слова в сообщениях для участников — их можно заменить "
            "своими. ✏️ — своя фраза, ⚪ — встроенная.", "",
        ]
        rows = []
        for name, (default, _, _, label) in READINESS_PHRASES.items():
            own = settings["phrases"].get(name)
            lines.append(
                f"{'✏️' if own else '⚪'} {label}: «{own or default}»"
            )
            rows.append([readiness_button(
                f"🔤 {own or default}"[:40], "tpl", service_id, f"ph.{name}"
            )])
        rows.append([readiness_button("⬅️ К текстам", "texts", service_id)])
        await _send_dialog(
            message, "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
            _dialog_keep_rows=True,
            _dialog_keyboard_limit=sum(len(row) for row in rows),
            _dialog_text_limit=TELEGRAM_TEXT_LIMIT,
            parse_mode=None,
        )

    async def show_readiness_texts(
        message: Message, service_id: str,
    ) -> None:
        """«📝 Тексты» раздела «📅 График»: все шаблоны опроса."""
        registry = service_registry()
        settings = registry.get_readiness_settings(service_id)
        rows = []
        lines = ["📝 Тексты опроса и графика", "",
                 "✏️ — свой текст, ⚪ — встроенный."]
        for kind in ("intro", "q", "rem", "chat", "done", "pub", "pubupd"):
            field, title, about, _ = READINESS_TEXTS[kind]
            mark = "✏️" if settings[field] else "⚪"
            lines.append(f"{mark} {title} — {about}")
            rows.append([readiness_button(title, "tpl", service_id, kind)])
        own_phrases = len(settings["phrases"])
        lines.append(
            "🔤 Фразы — короткие слова в сообщениях участникам"
            + (f" (своих: {own_phrases})" if own_phrases else "") + "."
        )
        rows.append([readiness_button("🔤 Фразы", "phr", service_id)])
        rows.append([readiness_button("⬅️ К опросу", "open", service_id)])
        await _send_dialog(
            message, "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
            _dialog_keep_rows=True,
            _dialog_keyboard_limit=sum(len(row) for row in rows),
            _dialog_text_limit=TELEGRAM_TEXT_LIMIT,
            parse_mode=None,
        )

    async def show_readiness_text(
        message: Message, service_id: str, kind: str,
        notice: str | None = None,
    ) -> None:
        registry = service_registry()
        service = registry.get_service(service_id) if registry else None
        if service is None or kind not in READINESS_TEXTS:
            await _send_dialog(message, "Настройка устарела.", parse_mode=None)
            return
        field, title, about, _ = READINESS_TEXTS[kind]
        phrase_kind = kind.startswith("ph.")
        template = readiness_text_value(
            registry.get_readiness_settings(service_id), kind
        )
        default = READINESS_PHRASES[kind[3:]][0] if phrase_kind else {
            "intro": readiness.DEFAULT_READINESS_INTRO,
            "rem": readiness.DEFAULT_READINESS_REMINDER,
            "chat": readiness.DEFAULT_READINESS_CHAT_REMINDER,
            "q": readiness.DEFAULT_READINESS_QUESTION,
            "done": readiness.DEFAULT_READINESS_CLOSED,
            "pub": readiness.DEFAULT_PLAN_TITLE,
            "pubupd": readiness.DEFAULT_PLAN_UPDATE_TITLE,
        }[kind]
        lines = [
            f"{service_label(service)} › График › Опрос › {title}",
            "",
            f"ℹ️ {about}",
            "",
            "Текущий текст:" if template else "Используется встроенный текст:",
            template or default,
        ]
        if notice:
            lines = [notice, ""] + lines
        rows = [[
            readiness_button("✏️ Изменить", "tpledit", service_id, kind),
        ]]
        if not phrase_kind:
            rows[0].append(
                readiness_button("👁 Предпросмотр", "tplprev", service_id, kind)
            )
        if template:
            rows[0].append(
                readiness_button("🔄 Вернуть встроенный", "tplreset",
                                 service_id, kind)
            )
        rows.append([
            readiness_button("⬅️ К фразам", "phr", service_id) if phrase_kind
            else readiness_button("⬅️ К текстам", "texts", service_id)
        ])
        await _send_dialog(
            message, "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
            _dialog_keep_rows=True,
            _dialog_text_limit=TELEGRAM_TEXT_LIMIT,
            parse_mode=None,
        )

    async def ask_readiness_text(
        message: Message, actor_id: int, service_id: str, kind: str,
        notice: str | None = None,
    ) -> None:
        _, title, _, fields = READINESS_TEXTS[kind]
        pending_inputs[actor_id] = {
            "operation": "readiness_text",
            "service_id": service_id,
            "kind": kind,
            "step": "readiness_text",
            "session_id": secrets.token_urlsafe(9),
        }
        text = (
            f"{title}: введите новый текст.\n\n"
            + template_fields_help(fields)
        )
        if notice:
            text = f"{notice}\n\n{text}"
        await _send_dialog(
            message, text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                readiness_button(
                    "⬅️ Назад без изменения", "tpl", service_id, kind
                )
            ]]),
            parse_mode=None,
        )

    async def show_readiness_progress(
        message: Message, service: dict, run: dict, notice: str | None = None,
    ) -> None:
        """Ход опроса / итог; у ответивших вне состава — «➕ В состав»."""
        summary = await asyncio.to_thread(
            readiness.summarize, service_registry(), run,
            # Закрытый опрос — распределение в «📋 График».
            with_plan=run["status"] == "open",
        )
        reminder = None
        if run["status"] == "open":
            reminder = readiness.reminder_line(
                run, service_registry().get_readiness_settings(service["id"]),
                datetime.now(context.timezone), context.timezone,
                readiness.reminder_sent(service_registry(), run),
            )
        text = readiness.progress_text(
            service_label(service), run, summary, context.timezone, reminder,
        )
        if notice:
            text = f"{notice}\n\n{text}"
        year, month = readiness.month_of(run)
        rows = [
            [readiness_button(
                f"➕ {readiness.outside_name(answer)} в состав"[:60],
                "addm", service["id"],
                f"{year:04d}{month:02d}.{answer['telegram_id']}",
            )]
            for answer in summary.outside[:20]
        ]
        if run["status"] == "closed":
            rows.append([readiness_button(
                "📋 График", "plan", service["id"], f"{year:04d}{month:02d}"
            )])
        rows.append([readiness_button("⬅️ К опросу", "open", service["id"])])
        await _send_dialog(
            message, text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
            _dialog_keep_rows=True,
            _dialog_keyboard_limit=sum(len(row) for row in rows),
            _dialog_text_limit=TELEGRAM_TEXT_LIMIT,
            parse_mode=None,
        )

    async def show_readiness_member(
        message: Message, actor_id: int, service_id: str,
        notice: str | None = None,
    ) -> None:
        """«➕ В состав»: выбор задач ответившего вне состава."""
        pending = pending_inputs.get(actor_id) or {}
        tasks = pending.get("tasks", [])
        selected = pending.get("selected", [])
        lines = [
            f"➕ {pending.get('name')} · в состав",
            "",
            "Отметил(а): " + (
                ", ".join(
                    date.fromisoformat(day).strftime("%d.%m")
                    for day in pending.get("dates", [])
                ) or "не сможет в этом месяце"
            ),
            "Выберите задачи — по ним бот будет ставить человека в график.",
        ]
        if notice:
            lines = [notice, ""] + lines
        token = pending.get("token", "")
        buttons = [
            readiness_button(
                f"{'☑️' if task in selected else '⬜'} {task}"[:60],
                "addmt", service_id, f"{token}.{index}",
            )
            for index, task in sort_tasks_for_display(
                enumerate(tasks), service_id, name=lambda pair: pair[1],
            )
        ]
        rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
        rows.append([readiness_button("✅ Сохранить", "addms", service_id, token)])
        rows.append([readiness_button("⬅️ К опросу", "open", service_id)])
        await _send_dialog(
            message, "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
            _dialog_keep_rows=True,
            _dialog_keyboard_limit=sum(len(row) for row in rows),
            parse_mode=None,
        )

    PLAN_ACTIONS = {
        "plan", "precalc", "precalcok", "pedit", "pdate", "ptask", "padd",
        "prem", "pclear", "pok", "pokok", "pfree", "pnew", "pday", "pdset",
        "pprev", "phelp",
    }

    def plan_month(arg: str | None, now: datetime) -> str | None:
        """Месяц графика из кнопки: месяц опроса, текущий или прошлый
        (график правится и после начала месяца, итог мог прийти позже)."""
        value = (arg or "").split(".")[0]
        if len(value) != 6 or not value.isdigit():
            return None
        key = f"{value[:4]}-{value[4:]}"
        allowed = {
            f"{now.year:04d}-{now.month:02d}",
            "{:04d}-{:02d}".format(*schedule_plan.previous_month(
                now.year, now.month
            )),
            "{:04d}-{:02d}".format(*readiness.target_month(now.date())),
        }
        return key if key in allowed else None

    def plan_arg(plan: dict, *extra) -> str:
        """Аргумент кнопки: месяц, версия графика и номера даты / задачи."""
        return ".".join(
            [plan["month"].replace("-", ""), schedule_plan.revision(plan)]
            + [str(item) for item in extra]
        )

    def plan_current(run: dict | None, plan: dict | None) -> bool:
        """График действующий: по закрытому опросу, который сейчас у
        месяца, или без опроса — если опроса месяца нет (или он пропущен)."""
        if plan is None:
            return False
        # Импортированный и подтверждённый график доступен для редактирования.
        if plan.get("source") == "excel":
            return True
        if plan["run_id"] is None:
            return run is None or run["status"] == "skipped"
        return (
            run is not None and run["status"] == "closed"
            and plan["run_id"] == run["id"]
        )

    async def load_plan(service_id: str, key: str) -> tuple[dict | None, dict | None]:
        """(опрос, график) закрытого опроса месяца; график создаётся при
        первом открытии (опрос закрыт до обновления бота)."""
        registry = service_registry()
        async with readiness.service_lock(service_id):
            # Под блокировкой: перезапуск мог только что заменить опрос.
            run = registry.get_readiness_run(service_id, key)
            plan = registry.get_schedule_plan(service_id, key)
            if run is None or run["status"] != "closed":
                # График без опроса — пока опрос месяца не идёт.
                if not plan_current(run, plan):
                    return run, None
                return run, schedule_plan.sync_implicit_task(registry, plan)
            plan = await schedule_plan.ensure_plan(
                registry, run,
                getattr(context, "load_upcoming_service_events", None), debug,
            )
        return run, plan

    async def show_plan(
        message: Message, service: dict, run: dict, plan: dict,
        notice: str | None = None,
    ) -> None:
        """«📋 График»: предложенный (или утверждённый) график месяца."""
        summary = await asyncio.to_thread(
            schedule_plan.plan_summary, service_registry(), plan, run
        )
        text = schedule_plan.plan_text(
            service_label(service), plan, summary, context.timezone,
            schedule_plan.window_of(service_registry(), service["id"]),
        )
        if notice:
            text = f"{notice}\n\n{text}"
        month_arg = plan["month"].replace("-", "")
        service_id = service["id"]
        rows = [[
            readiness_button("✏️ Изменить", "pedit", service_id, month_arg),
            readiness_button(
                "🔄 Пересчитать", "precalc", service_id, plan_arg(plan)
            ),
        ]]
        if plan["approved"] is None:
            rows.append([
                readiness_button("✅ Утвердить", "pok", service_id, plan_arg(plan))
            ])
        elif plan["dirty"]:
            rows.append([readiness_button(
                "✅ Утвердить изменения", "pok", service_id, plan_arg(plan)
            )])
        target = "{:04d}{:02d}".format(
            *readiness.target_month(datetime.now(context.timezone).date())
        )
        if month_arg == target:
            # «➕ В состав» — только для опроса текущего месяца опроса.
            rows += [
                [readiness_button(
                    f"➕ {readiness.outside_name(answer)} в состав"[:60],
                    "addm", service_id, f"{target}.{answer['telegram_id']}",
                )]
                for answer in summary.outside[:20]
            ]
        if plan.get("run_id") is None:
            current = datetime.now(context.timezone).date().replace(day=1)
            following = month_end(current.year, current.month) + timedelta(days=1)
            other = following if month_arg == current.strftime("%Y%m") else current
            auto_button = readiness_button(
                f"🧮 Авто {month_title(other.year, other.month).split()[0][:3]}",
                "pfree", service_id, other.strftime("%Y%m"),
            )
            if len(rows) > 1 and len(rows[1]) == 1 and rows[1][0].text.startswith("✅"):
                rows[1].append(auto_button)
            else:
                rows.append([auto_button])
        rows.append([readiness_button("❓ Справка", "phelp", service_id, month_arg)])
        rows.append([readiness_button(
            "⬅️ К графику" if plan.get("run_id") is None else "⬅️ К опросу",
            "month" if plan.get("run_id") is None else "open",
            service_id,
        )])
        await _send_dialog(
            message, text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
            _dialog_keep_rows=True,
            _dialog_keyboard_limit=sum(len(row) for row in rows),
            _dialog_text_limit=TELEGRAM_TEXT_LIMIT,
            parse_mode=None,
        )

    async def show_plan_dates(
        message: Message, service_id: str, plan: dict,
    ) -> None:
        month_arg = plan["month"].replace("-", "")
        buttons = []
        for index, day in enumerate(plan["dates"]):
            short = date.fromisoformat(day).strftime("%d.%m")
            mark = "⚠️ " if any(
                gap.startswith(f"⚠️ {short} ") for gap in schedule_plan.gaps(plan)
            ) else ""
            buttons.append(readiness_button(
                f"{mark}🗓 {short}", "pdate", service_id, plan_arg(plan, index)
            ))
        rows = [buttons[i:i + 3] for i in range(0, len(buttons), 3)]
        rows.append([readiness_button("⬅️ К графику", "plan", service_id, month_arg)])
        await _send_dialog(
            message, "✏️ Изменить график — выберите дату.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
            _dialog_keep_rows=True,
            _dialog_keyboard_limit=sum(len(row) for row in rows),
            parse_mode=None,
        )

    async def show_plan_day(
        message: Message, service_id: str, plan: dict, day_index: int,
        names: dict[str, str],
    ) -> None:
        month_arg = plan["month"].replace("-", "")
        day = plan["dates"][day_index]
        rows = []
        for index, task in sort_tasks_for_display(
            enumerate(plan["tasks"]), service_id, name=lambda pair: pair[1]["name"],
        ):
            people = schedule_plan.slot_people(plan, day, task["name"])
            shown = ", ".join(names.get(key, key) for key in people) or "⚠️ никого"
            rows.append([readiness_button(
                f"{task_emoji(task['name'])} {task['name']} — {shown}"[:60],
                "ptask", service_id, plan_arg(plan, day_index, index),
            )])
        rows.append([readiness_button("⬅️ К датам", "pedit", service_id, month_arg)])
        await _send_dialog(
            message,
            f"✏️ {date.fromisoformat(day):%d.%m} — выберите задачу.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
            _dialog_keep_rows=True,
            _dialog_keyboard_limit=sum(len(row) for row in rows),
            parse_mode=None,
        )

    async def show_plan_slot(
        message: Message, actor_id: int, service: dict, run: dict,
        plan: dict, day_index: int, task_index: int,
        notice: str | None = None,
    ) -> None:
        """Кто стоит на задаче в дату: ❌ — убрать, имя — поставить."""
        registry = service_registry()
        summary = await asyncio.to_thread(
            schedule_plan.plan_summary, registry, plan, run
        )
        names = {person["key"]: person["name"] for person in summary.people}
        day = plan["dates"][day_index]
        task = plan["tasks"][task_index]
        current = schedule_plan.slot_people(plan, day, task["name"])
        free, quiet, rest = schedule_plan.candidates(
            plan, summary, day, task["name"]
        )
        busy = schedule_plan.busy_on(plan, day, task["name"], summary.bundles)
        token = secrets.token_urlsafe(6)
        keys = current + free + quiet + rest
        pending_inputs[actor_id] = {
            "operation": "readiness_plan",
            "service_id": service["id"],
            "month": plan["month"],
            "day_index": day_index,
            "task_index": task_index,
            # Задача — по имени: пересчёт мог поменять список задач.
            "day": day,
            "task": task["name"],
            "revision": schedule_plan.revision(plan),
            "keys": keys,
            "step": "readiness_plan",
            "token": token,
        }
        need = task["needed"] if not task["max"] else f"{task['needed']}–{task['max']}"

        def listed(people: list[str]) -> str:
            return ", ".join(names.get(key, key) for key in people) or "никто"

        implicit = schedule_plan.implicit_plan(plan)
        task_day = task.get("day")
        lines = [
            f"✏️ {date.fromisoformat(day):%d.%m}"
            + ("" if implicit else f" · «{task['name']}»")
            + (
                "" if task_day is None else
                f" · {member_day_label(task_day)}"
            )
            + f" (нужно {need})",
            "Сейчас: " + (listed(current) if current else "⚠️ никого"),
            "",
            ("✅ Свободны: " if implicit
             else "✅ Свободны и приписаны к задаче: ") + listed(free),
            f"⚪ Не отвечали: {listed(quiet)}",
            f"👥 Остальные из состава: {listed(rest)}",
        ]
        if busy:
            lines.append("🔒 Заняты в этот день: " + ", ".join(
                f"{names.get(key, key)} ({other})" for key, other in busy.items()
            ))
        lines += ["", "Нажмите имя — поставить, ❌ — убрать."]
        if notice:
            lines = [notice, ""] + lines
        buttons = [
            readiness_button(
                f"❌ {names.get(key, key)}"[:60], "prem", service["id"],
                f"{token}.{index}",
            )
            for index, key in enumerate(current)
        ]
        for group, mark in ((free, "✅"), (quiet, "⚪"), (rest, "👥")):
            for key in group:
                buttons.append(readiness_button(
                    f"{mark} {names.get(key, key)}"[:60], "padd", service["id"],
                    f"{token}.{keys.index(key)}",
                ))
        rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
        by_key = {person["key"]: person for person in summary.people}
        # Обычный день служения — окно служения (без него — вс).
        usual = DAY_WINDOWS.get(
            schedule_plan.window_of(registry, service["id"]), "свои дни"
        )
        for index, key in enumerate(current):
            # День только на эту дату (иначе — день задачи, свой день
            # человека или окно).
            offset = schedule_plan.slot_offset(
                plan, by_key.get(key), day, task["name"]
            )
            label = member_day_label(offset) if offset is not None else usual
            rows.append([readiness_button(
                f"📅 {names.get(key, key)} — {label}"[:60],
                "pday", service["id"], f"{token}.{index}",
            )])
        rows.append([readiness_button(
            "➕ Другой человек из Telegram", "pnew", service["id"], token
        )])
        if current:
            rows.append([readiness_button(
                "🚫 Оставить пустым", "pclear", service["id"], token
            )])
        rows.append([
            readiness_button(
                "⬅️ К датам", "pedit", service["id"],
                plan["month"].replace("-", ""),
            ) if implicit else readiness_button(
                "⬅️ К дате", "pdate", service["id"], plan_arg(plan, day_index),
            )
        ])
        await _send_dialog(
            message, "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
            _dialog_keep_rows=True,
            _dialog_keyboard_limit=sum(len(row) for row in rows),
            _dialog_text_limit=TELEGRAM_TEXT_LIMIT,
            parse_mode=None,
        )

    async def send_excel_block(
        actor_id: int, service: dict, plan: dict,
    ) -> None:
        """Админу — утверждённый график для переноса в Excel: текст и
        .xlsx с колонками служения."""
        registry = service_registry()
        people = registry.readiness_people(service["id"])
        await send_telegram_screen(
            context.bot.send_message, actor_id,
            schedule_plan.excel_text(registry, service, plan, people),
            limit=TELEGRAM_TEXT_LIMIT,
        )
        if not schedule_plan.has_sheet_columns(registry, service["id"]):
            # У служения нет колонки на листе графика — файл не нужен.
            return
        data = await asyncio.to_thread(
            schedule_plan.excel_file, registry, service, plan, people
        )
        await context.bot.send_document(
            actor_id,
            BufferedInputFile(data, filename=f"{service['id']}-{plan['month']}.xlsx"),
        )

    async def change_slot(
        actor_id: int, service_id: str, key: str, pending: dict, action: str,
        person: str | None, new_member=None,
    ) -> tuple:
        """Правка задачи даты (padd / prem / pclear) на свежем графике под
        блокировкой; → (опрос, график, № даты, № задачи, пояснение).
        new_member — добавить человека в состав (→ его ключ) под той же
        блокировкой, только если место в задаче есть."""
        registry = service_registry()
        day_index, task_index = pending["day_index"], pending["task_index"]
        notice = None
        async with readiness.service_lock(service_id):
            # Свежий график: другой админ мог поправить его только что,
            # а опрос — быть перезапущен.
            run = registry.get_readiness_run(service_id, key)
            plan = registry.get_schedule_plan(service_id, key)
            if not plan_current(run, plan) or (
                schedule_plan.revision(plan) != pending.get("revision")
            ):
                raise ValueError("График изменился — откройте его заново.")
            day = pending["day"]
            task = next(
                (item for item in plan["tasks"]
                 if item["name"] == pending["task"]),
                None,
            )
            if day not in plan["dates"] or task is None:
                raise ValueError("График пересчитан — откройте его заново.")
            day_index = plan["dates"].index(day)
            task_index = plan["tasks"].index(task)
            slots = plan["slots"]
            people = slots.setdefault(day, {}).setdefault(task["name"], [])
            changed = False
            if new_member is not None:
                limit = task["max"] or task["needed"]
                if limit > 1 and len(people) >= limit:
                    raise ValueError(
                        f"Мест нет ({limit}) — сначала уберите кого-нибудь."
                    )
                person = new_member()
            if action == "pclear":
                changed = bool(people)
                people.clear()
                notice = "🚫 Задача оставлена пустой"
            elif person is None:
                notice = "⚠️ Экран устарел"
            elif action == "prem":
                if person in people:
                    people.remove(person)
                    changed = True
                notice = "✅ Убран(а) из задачи"
            elif person in people:
                notice = "ℹ️ Уже стоит на этой задаче"
            elif person in schedule_plan.busy_on(
                plan, day, task["name"],
                readiness.week_bundles(registry, service_id),
            ):
                notice = "⚠️ В этот день уже на другой задаче — одна задача в день"
            else:
                limit = task["max"] or task["needed"]
                replaced = None
                if len(people) >= limit and limit == 1:
                    replaced = people.pop()
                if len(people) >= limit:
                    notice = (
                        f"⚠️ Мест нет ({limit}) — сначала уберите кого-нибудь"
                    )
                else:
                    people.append(person)
                    changed = True
                    summary = await asyncio.to_thread(
                        schedule_plan.plan_summary, registry, plan, run
                    )
                    names = {
                        item["key"]: item["name"] for item in summary.people
                    }
                    plan_view = {**plan, "slots": slots}
                    notes = schedule_plan.choice_notes(
                        plan_view, summary, person, day, task["name"]
                    )
                    notice = "\n".join(
                        [f"✅ Поставлен(а): {names.get(person, person)}"
                         + (f" вместо {names.get(replaced, replaced)}"
                            if replaced else "")]
                        + notes
                    )
            if changed:
                plan = registry.set_schedule_slots(
                    actor_id, service_id, key, slots
                )
        return run, plan, day_index, task_index, notice

    async def reshow_plan_slot(
        message: Message, actor_id: int, notice: str | None = None,
    ) -> None:
        """Вернуться к экрану задачи даты после выбора в Telegram."""
        pending = pending_inputs.get(actor_id) or {}
        registry = service_registry()
        service = registry.get_service(pending.get("service_id", ""))
        run, plan = await load_plan(pending["service_id"], pending["month"])
        task = next(
            (item for item in (plan or {}).get("tasks", [])
             if item["name"] == pending.get("task")),
            None,
        )
        if service is None or not plan_current(run, plan) or task is None or (
            pending.get("day") not in plan["dates"]
        ):
            pending_inputs.pop(actor_id, None)
            await _send_dialog(
                message, "⚠️ График изменился — откройте его заново.",
                reply_markup=ReplyKeyboardRemove(), parse_mode=None,
            )
            return
        await show_plan_slot(
            message, actor_id, service, run, plan,
            plan["dates"].index(pending["day"]), plan["tasks"].index(task),
            notice,
        )

    async def on_plan_user_shared(message: Message, share: tuple) -> None:
        """«➕ Другой человек из Telegram»: в состав на задачу и в дату."""
        actor_id = message.from_user.id
        pending = pending_inputs.get(actor_id)
        if (
            pending is None
            or pending.get("operation") != "readiness_plan"
            or pending.get("step") != "plan_user"
            or pending.get("service_id") != share[2]
            or pending.get("share_request") != message.users_shared.request_id
        ):
            await _send_dialog(
                message, "Выбор пользователя устарел. Откройте график заново.",
                reply_markup=ReplyKeyboardRemove(), parse_mode=None,
            )
            return
        pending["step"] = "readiness_plan"
        user = message.users_shared.users[0]
        username = (getattr(user, "username", None) or "").strip()
        name = " ".join(
            part for part in (
                getattr(user, "first_name", None), getattr(user, "last_name", None)
            ) if part
        ).strip() or (f"@{username}" if username else "")
        registry = service_registry()
        service_id = pending["service_id"]

        def person_key() -> str | None:
            return next(
                (
                    person["key"]
                    for person in registry.readiness_people(service_id)
                    if person["telegram_id"] == user.user_id
                ),
                None,
            )

        try:
            key = person_key()
            if key is None:
                if not name:
                    raise ValueError(
                        "У пользователя нет имени в Telegram — добавьте его "
                        "через «👥 Ответственные»."
                    )
                # Имя из Telegram может совпасть с чужим в составе —
                # привязка ушла бы к однофамильцу.
                taken = {
                    person["name"]
                    for person in registry.readiness_people(service_id)
                } | {
                    member["name"]
                    for member in registry.list_service_members(service_id)
                }
                name = name[:60]
                if name in taken:
                    unique = (
                        f"{name} (@{username})" if username
                        else f"{name} ({user.user_id})"
                    )
                    if unique in taken:
                        raise ValueError(
                            f"«{name}» уже есть в составе — добавьте через "
                            "«👥 Ответственные»."
                        )
                    name = unique

            def add_member() -> str:
                # В состав — под блокировкой графика, когда место проверено.
                registry.bind_service_member(
                    actor_id, service_id, name, user.user_id, username,
                    tasks=[pending["task"]], from_readiness=True,
                )
                added = person_key()
                if added is None:
                    raise ValueError(
                        "Не удалось добавить пользователя в состав — "
                        "попробуйте через «👥 Ответственные»."
                    )
                return added

            run, plan, _, _, notice = await change_slot(
                actor_id, service_id, pending["month"], pending, "padd", key,
                new_member=add_member if key is None else None,
            )
        except (PermissionError, ValueError) as exc:
            await reshow_plan_slot(
                message, actor_id, _status_text(str(exc), "⚠️")
            )
            return
        except sqlite3.Error:
            debug.exception("Не удалось добавить пользователя в график")
            await reshow_plan_slot(
                message, actor_id, "❌ Не удалось сохранить — попробуйте позже"
            )
            return
        await reshow_plan_slot(message, actor_id, notice)

    async def on_plan_action(
        callback: CallbackQuery, action: str, service: dict, arg: str | None,
        actor_id: int, now: datetime,
    ) -> None:
        """Действия «📋 График» (этап 5.4–5.5)."""
        registry = service_registry()
        service_id = service["id"]
        if action in {"plan", "pok"}:
            pending = pending_inputs.get(actor_id) or {}
            if pending.get("operation") == "plan_warning":
                pending_inputs.pop(actor_id, None)
        if action in {"padd", "prem", "pclear", "pnew", "pday", "pdset"}:
            pending = pending_inputs.get(actor_id) or {}
            key = plan_month((pending.get("month") or "").replace("-", ""), now)
        else:
            key = plan_month(arg, now)
        if key is None:
            await show_readiness(
                callback.message, service_id, "⚠️ Экран устарел — откройте график заново"
            )
            await _answer_callback(callback)
            return
        loader = getattr(context, "load_upcoming_service_events", None)
        if action == "pfree":
            await _answer_callback(callback, "⏳ Составляю…")
            year, month = map(int, key.split("-"))
            notice = "🧮 График составлен без опроса — проверьте и утвердите"
            async with readiness.service_lock(service_id):
                run = registry.get_readiness_run(service_id, key)
                plan = registry.get_schedule_plan(service_id, key)
                if run is not None and run["status"] != "skipped":
                    notice = (
                        "⚠️ Опрос этого месяца уже есть — график составляется "
                        "по его ответам"
                    )
                    plan = None
                elif plan_current(run, plan):
                    notice = "ℹ️ График уже составлен"
                else:
                    plan = await schedule_plan.build_draft(
                        registry, schedule_plan.free_run(service_id, year, month),
                        None, debug,
                    )
            if plan is None:
                await show_readiness(callback.message, service_id, notice)
            else:
                await show_plan(callback.message, service, run, plan, notice)
            return
        if action == "pprev":
            # Пока опрос идёт — каким получится график по текущим ответам
            # (общая логика); не сохраняется, утвердить — после завершения.
            await _answer_callback(callback, "⏳ Составляю…")
            run = registry.get_readiness_run(service_id, key)
            if run is None or run["status"] != "open":
                await show_readiness(
                    callback.message, service_id,
                    "⚠️ Опрос уже не идёт — откройте «📋 График»",
                )
                return
            plan = await schedule_plan.build_draft(
                registry, run, loader, debug, save=False,
            )
            summary = await asyncio.to_thread(
                schedule_plan.plan_summary, registry, plan, run
            )
            text = schedule_plan.plan_text(
                service_label(service), plan, summary, context.timezone,
                schedule_plan.window_of(registry, service_id),
            ).replace(
                "📝 Черновик — ещё не утверждён",
                "⏳ Предварительный — опрос идёт; окончательный составится "
                "после завершения, тогда его можно поправить и утвердить",
                1,
            )
            await _send_dialog(
                callback.message, text,
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [readiness_button(
                        "🔄 Обновить", "pprev", service_id,
                        key.replace("-", ""),
                    )],
                    [readiness_button(
                        "⬅️ К опросу", "open", service_id,
                    )],
                ]),
                _dialog_keep_rows=True,
                _dialog_text_limit=TELEGRAM_TEXT_LIMIT,
                parse_mode=None,
            )
            return
        run, plan = await load_plan(service_id, key)
        if not plan_current(run, plan):
            await show_readiness(
                callback.message, service_id,
                "⚠️ Графика пока нет: он появится, когда опрос будет "
                "завершён, или «🧮 Составить без опроса»",
            )
            await _answer_callback(callback)
            return
        parts = (arg or "").split(".")
        if action in {"precalc", "precalcok", "pdate", "ptask", "pok", "pokok"}:
            if len(parts) < 2 or parts[1] != schedule_plan.revision(plan):
                # Кнопка с прежней версией: график поправили, пересчитали или
                # опрос перезапустили — действие относилось бы к другому.
                await show_plan(
                    callback.message, service, run, plan,
                    "⚠️ График изменился — проверьте его и повторите",
                )
                await _answer_callback(callback)
                return
            del parts[1]
        if action == "phelp":
            route = "Авто" if plan.get("run_id") is None else "Опрос"
            year, month = map(int, plan["month"].split("-"))
            await _send_dialog(
                callback.message,
                f"❓ {service_label(service)} › График › {route} › "
                f"{month_title(year, month)} › Справка\n\n"
                "«Авто» открывает существующий график выбранного месяца. "
                "Если его нет, создаёт черновик без Excel и опроса: "
                "все участники считаются доступными по воскресеньям.\n\n"
                "✏️ Изменить — вручную выбрать ответственных и дни; "
                "автоподбор не запускается.\n\n"
                "🔄 Пересчитать — заново распределить задачи по текущему "
                "составу, допускам и ограничениям. Для графика по опросу "
                "учитываются ответы; без опроса — доступность всех участников. "
                "Если есть ручные правки, бот попросит подтвердить их сброс.\n\n"
                "✅ Утвердить / Утвердить изменения — сохранить проверенный "
                "график как действующий и опубликовать его в чате служения. "
                "До утверждения правки не заменяют прежний утверждённый график.\n\n"
                "🧮 Авто с названием месяца — открыть или составить график "
                "другого месяца отдельно, не меняя этот. Если для месяца "
                "уже есть опрос, бот направит к нему.\n\n"
                "⬅️ К графику — вернуться на основной экран с «Сводкой».",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [readiness_button("⬅️ К графику месяца", "plan", service_id,
                                      key.replace("-", ""))],
                ]),
                _dialog_keep_rows=True, _dialog_text_limit=TELEGRAM_TEXT_LIMIT,
                parse_mode=None,
            )
        elif action == "plan":
            await show_plan(callback.message, service, run, plan)
        elif action == "precalc" and plan["edited"]:
            month_arg = key.replace("-", "")
            await _send_dialog(
                callback.message,
                "🔄 Пересчитать график заново?\n\nРучные правки "
                "сбросятся (утверждённый график в чате не изменится, пока "
                "вы его снова не утвердите).",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [readiness_button(
                        "✅ Да, пересчитать", "precalcok", service_id,
                        plan_arg(plan),
                    )],
                    [readiness_button("⬅️ К графику", "plan", service_id, month_arg)],
                ]),
                _dialog_keep_rows=True,
                parse_mode=None,
            )
        elif action in {"precalc", "precalcok"}:
            await _answer_callback(callback, "⏳ Считаю…")
            rev = schedule_plan.revision(plan)
            notice = "🔄 График пересчитан по текущему составу и задачам"
            async with readiness.service_lock(service_id):
                # Под блокировкой: опрос мог быть перезапущен, а график —
                # поправлен другим админом после подтверждения.
                run = registry.get_readiness_run(service_id, key)
                plan = registry.get_schedule_plan(service_id, key)
                if not plan_current(run, plan) or (
                    schedule_plan.revision(plan) != rev
                ):
                    notice = "⚠️ График изменился — проверьте его и повторите"
                    if run is not None and run["status"] == "closed":
                        # Опрос перезапущен — показать график нового.
                        plan = await schedule_plan.ensure_plan(
                            registry, run, getattr(context, "load_upcoming_service_events", None), debug,
                        )
                else:
                    plan = await schedule_plan.build_draft(
                        registry, schedule_plan.plan_run(plan, run), loader,
                        debug,
                    )
            if not plan_current(run, plan):
                await show_readiness(callback.message, service_id, notice)
            else:
                await show_plan(callback.message, service, run, plan, notice)
            return
        elif action == "pedit":
            await show_plan_dates(callback.message, service_id, plan)
        elif action == "pdate" and len(parts) == 2 and parts[1].isdigit() and (
            int(parts[1]) < len(plan["dates"])
        ) and schedule_plan.implicit_plan(plan):
            # Служение без задач — сразу выбор людей.
            await show_plan_slot(
                callback.message, actor_id, service, run, plan,
                int(parts[1]), 0,
            )
        elif action == "pdate" and len(parts) == 2 and parts[1].isdigit() and (
            int(parts[1]) < len(plan["dates"])
        ):
            names = {
                person["key"]: person["name"]
                for person in registry.readiness_people(service_id)
            }
            await show_plan_day(
                callback.message, service_id, plan, int(parts[1]), names
            )
        elif action == "ptask" and len(parts) == 3 and all(
            part.isdigit() for part in parts[1:]
        ) and int(parts[1]) < len(plan["dates"]) and (
            int(parts[2]) < len(plan["tasks"])
        ):
            await show_plan_slot(
                callback.message, actor_id, service, run, plan,
                int(parts[1]), int(parts[2]),
            )
        elif action == "pday":
            # День только на эту дату для поставленного человека.
            pending = pending_inputs[actor_id]
            index = int(parts[-1]) if parts[-1].isdigit() else -1
            if not 0 <= index < len(pending["keys"]):
                raise ValueError("Экран устарел — откройте график заново.")
            names = {
                person["key"]: person["name"]
                for person in registry.readiness_people(service_id)
            }
            token = pending["token"]
            buttons = [
                readiness_button(
                    f"📅 {member_day_label(offset)}", "pdset", service_id,
                    f"{token}.{index}.{offset}",
                )
                for offset in MEMBER_DAYS
            ]
            rows = [buttons[i:i + 3] for i in range(0, len(buttons), 3)]
            rows.append([readiness_button(
                "↩️ Как обычно", "pdset", service_id, f"{token}.{index}.n"
            )])
            rows.append([readiness_button(
                "⬅️ К задаче", "pdset", service_id, f"{token}.{index}.back"
            )])
            key_person = pending["keys"][index]
            await _send_dialog(
                callback.message,
                f"📅 {names.get(key_person, key_person)} · "
                f"{date.fromisoformat(pending['day']):%d.%m}\n\nВ какой день "
                "человек служит на этой неделе? Только для этой даты; «Как "
                "обычно» — его свой день, без него — дни служения ("
                + DAY_WINDOWS.get(
                    schedule_plan.window_of(registry, service_id), "свои дни"
                ) + ").",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
                _dialog_keep_rows=True,
                _dialog_keyboard_limit=sum(len(row) for row in rows),
                parse_mode=None,
            )
        elif action == "pdset":
            pending = pending_inputs[actor_id]
            _, index_text, value = (arg or "..").split(".", 2)
            index = int(index_text) if index_text.isdigit() else -1
            if not 0 <= index < len(pending["keys"]):
                raise ValueError("Экран устарел — откройте график заново.")
            notice = None
            if value != "back":
                offset = None if value == "n" else int(value)
                async with readiness.service_lock(service_id):
                    run = registry.get_readiness_run(service_id, key)
                    plan = registry.get_schedule_plan(service_id, key)
                    if not plan_current(run, plan) or (
                        schedule_plan.revision(plan) != pending.get("revision")
                    ):
                        raise ValueError(
                            "График изменился — откройте его заново."
                        )
                    plan = registry.set_schedule_day(
                        actor_id, service_id, key, pending["day"],
                        pending["keys"][index], offset,
                    )
                notice = "✅ День на эту дату: " + (
                    member_day_label(offset) if offset is not None
                    else "как обычно"
                )
            task_index = next(
                (i for i, task in enumerate(plan["tasks"])
                 if task["name"] == pending["task"]),
                None,
            )
            if task_index is None or pending["day"] not in plan["dates"]:
                raise ValueError("График изменился — откройте его заново.")
            await show_plan_slot(
                callback.message, actor_id, service, run, plan,
                plan["dates"].index(pending["day"]), task_index, notice,
            )
        elif action == "pnew":
            pending = pending_inputs[actor_id]
            pending["step"] = "plan_user"
            for request_id, share in list(pending_shares.items()):
                if share[0] == actor_id:
                    pending_shares.pop(request_id, None)
            request_id = new_share_request_id()
            pending_shares[request_id] = (
                actor_id, "plan_user", service_id, None, None
            )
            pending["share_request"] = request_id
            day = date.fromisoformat(pending["day"])
            await _send_dialog(
                callback.message,
                f"➕ {day:%d.%m} · «{pending['task']}»: нажмите кнопку и "
                "выберите пользователя Telegram. Он добавится в состав "
                "служения на эту задачу и встанет в эту дату. "
                f"«{CONTACT_BACK_TEXT}» — вернуться к выбору.",
                reply_markup=ReplyKeyboardMarkup(
                    keyboard=[
                        [KeyboardButton(
                            text="Выбрать пользователя",
                            request_users=KeyboardButtonRequestUsers(
                                request_id=request_id,
                                user_is_bot=False,
                                max_quantity=1,
                                request_name=True,
                                request_username=True,
                            ),
                        )],
                        [KeyboardButton(text=CONTACT_BACK_TEXT)],
                    ],
                    resize_keyboard=True,
                    one_time_keyboard=True,
                ),
                parse_mode=None,
            )
        elif action in {"padd", "prem", "pclear"}:
            pending = pending_inputs[actor_id]
            index = int(parts[-1]) if parts[-1].isdigit() else -1
            person = (
                pending["keys"][index]
                if 0 <= index < len(pending["keys"]) else None
            )
            run, plan, day_index, task_index, notice = await change_slot(
                actor_id, service_id, key, pending, action, person
            )
            await show_plan_slot(
                callback.message, actor_id, service, run, plan,
                day_index, task_index, notice,
            )
        elif action == "pok":
            month_arg = key.replace("-", "")
            year, month = map(int, key.split("-"))
            lines = [
                "✅ Утвердить график "
                f"{service_label(service)} на {month_title(year, month)}?",
                "",
                (
                    "Закреплённый график в чате служения обновится, под ним "
                    "— все изменения месяца; новые изменения уйдут ответом "
                    "на него (с упоминаниями)"
                    if plan["approved"] is not None
                    else "График уйдёт в чат служения и закрепится"
                )
                + "; вам — блок для переноса в Excel.",
            ]
            problems = schedule_plan.gaps(plan)
            if problems:
                lines += ["", f"Не закрыто мест: {len(problems)}"] + problems[:10]
            loader = getattr(context, "load_upcoming_service_events", None)
            if loader is not None and plan["approved"] is None:
                try:
                    events = await loader(
                        since=date(year, month, 1),
                        until=month_end(year, month),
                    )
                    if schedule_plan.excel_filled(events, service_id, year, month):
                        lines += ["", (
                            f"📋 На {month_title(year, month)} график в Excel "
                            "уже есть — утвердить план всё равно?"
                        )]
                except Exception:  # noqa: BLE001
                    debug.info("Excel для сверки не загружен", exc_info=True)
            await _send_dialog(
                callback.message, "\n".join(lines),
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [readiness_button(
                        "📢 Да, опубликовать", "pokok", service_id,
                        plan_arg(plan),
                    )],
                    [readiness_button("⬅️ К графику", "plan", service_id, month_arg)],
                ]),
                _dialog_keep_rows=True,
                _dialog_text_limit=TELEGRAM_TEXT_LIMIT,
                parse_mode=None,
            )
        elif action == "pokok":
            await _answer_callback(callback, "⏳ Проверяю график…")
            rev = schedule_plan.revision(plan)
            async with readiness.service_lock(service_id):
                run = registry.get_readiness_run(service_id, key)
                plan = registry.get_schedule_plan(service_id, key)
                if not plan_current(run, plan) or (
                    schedule_plan.revision(plan) != rev
                ):
                    # Подтверждали другой график — опубликовать без проверки
                    # нельзя.
                    notice = "⚠️ График изменился — проверьте его и утвердите снова"
                    if run is not None and run["status"] == "closed":
                        plan = await schedule_plan.ensure_plan(
                            registry, run, getattr(context, "load_upcoming_service_events", None), debug,
                        )
                elif plan["approved"] is not None and not plan["dirty"]:
                    notice = "ℹ️ Изменений нет — график уже опубликован"
                else:
                    try:
                        warnings = registry.task_assignment_warnings(
                            service_id, plan["slots"], complete=True, month=key,
                            days=plan.get("days"),
                        )
                        pending = pending_inputs.get(actor_id) or {}
                        warning_token = parts[1] if len(parts) == 2 else None
                        acknowledged = (
                            warnings if warning_token
                            and pending.get("operation") == "plan_warning"
                            and pending.get("service_id") == service_id
                            and pending.get("month") == key
                            and pending.get("revision") == rev
                            and pending.get("token") == warning_token
                            and pending.get("warnings") == warnings
                            else None
                        )
                        if warnings and acknowledged is None:
                            token = secrets.token_hex(4)
                            pending_inputs[actor_id] = {
                                "operation": "plan_warning", "service_id": service_id,
                                "month": key, "revision": rev, "token": token,
                                "warnings": warnings,
                            }
                            await _send_dialog(
                                callback.message,
                                "⚠️ График требует внимания\n\n"
                                + "\n".join(warnings)
                                + "\n\nОтветственные не будут удалены или заменены. "
                                "График можно утвердить как есть, приняв эти "
                                "предупреждения, или вернуться и исправить его.",
                                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                    [readiness_button(
                                        "⚠️ Утвердить с предупреждениями", "pokok",
                                        service_id, plan_arg(plan, token),
                                    )],
                                    [readiness_button(
                                        "⬅️ К графику", "plan", service_id,
                                        key.replace("-", ""),
                                    )],
                                ]),
                                _dialog_keep_rows=True,
                                _dialog_text_limit=TELEGRAM_TEXT_LIMIT,
                                parse_mode=None,
                            )
                            return
                        plan = await schedule_plan.publish(
                            context.bot, registry, service, plan, actor_id,
                            debug, TELEGRAM_TEXT_LIMIT,
                            acknowledged_warnings=acknowledged,
                        )
                    except ValueError as exc:
                        notice = f"⚠️ {exc}"
                    except schedule_plan.PublishedNotSaved:
                        debug.exception("Утверждение графика не сохранено")
                        plan = registry.get_schedule_plan(service_id, key)
                        notice = (
                            "⚠️ График опубликован в чате, но отметка об "
                            "утверждении не сохранилась — не публикуйте его "
                            "повторно, сообщите суперадмину"
                        )
                    except Exception:  # noqa: BLE001
                        debug.exception("Не удалось опубликовать график")
                        notice = "❌ Не удалось опубликовать график в чате"
                    else:
                        if pending_inputs.get(actor_id, {}).get("operation") == "plan_warning":
                            pending_inputs.pop(actor_id, None)
                        notice = "✅ График утверждён и опубликован в чате служения"
                        if plan.get("approval_warnings"):
                            notice += "\n⚠️ Предупреждения сохранены вместе с графиком."
                        if not plan.get("pinned", True):
                            notice += (
                                "\n⚠️ Закрепить не удалось — сделайте бота "
                                "администратором чата с правом «Закреплять "
                                "сообщения»"
                            )
                        if plan.get("unsent_notice"):
                            notice += (
                                "\n⚠️ Закреплённый график обновлён, но "
                                "сообщение с изменениями в чат не ушло — "
                                "перешлите его участникам:\n\n"
                                + plan["unsent_notice"]
                            )
                        try:
                            await send_excel_block(actor_id, service, plan)
                        except Exception:  # noqa: BLE001
                            debug.exception("Не удалось отправить блок для Excel")
                            notice += "\n⚠️ Блок для Excel отправить не удалось"
            if not plan_current(run, plan):
                await show_readiness(callback.message, service_id, notice)
            else:
                await show_plan(callback.message, service, run, plan, notice)
            return
        else:
            await show_plan(callback.message, service, run, plan, "⚠️ Экран устарел")
        await _answer_callback(callback)

    async def show_own_days(
        message: Message, actor_id: int, service_id: str,
        notice: str | None = None,
    ) -> None:
        """«📅 Свой день» из раздела «📅 График»: у кого служение в другой
        день недели, чем у всех."""
        registry = service_registry()
        members = registry.list_service_members(service_id)
        usual = DAY_WINDOWS.get(
            schedule_plan.window_of(registry, service_id), "свои дни"
        )
        token = secrets.token_urlsafe(6)
        pending_inputs[actor_id] = {
            "operation": "readiness_own",
            "service_id": service_id,
            "names": [member["name"] for member in members],
            "step": "readiness_own",
            "token": token,
        }
        lines = [
            "📅 Свой день",
            "",
            f"Дни служения для всех: {usual}. Выберите человека, который "
            "служит в другой день недели (каждую неделю). Для одной недели — "
            "в правке графика: «📅 Имя — день».",
        ]
        if not members:
            lines.append("⚪ В составе пока никого нет.")
        if notice:
            lines = [notice, ""] + lines
        rows = [
            [readiness_button(
                f"📅 {member['name']} — "
                + (
                    member_day_label(member["day_offset"])
                    if member["day_offset"] is not None else usual
                ),
                "ownp", service_id, f"{token}.{index}",
            )]
            for index, member in enumerate(members)
        ]
        for row in rows:
            row[0].text = row[0].text[:60]
        rows.append([readiness_button("⬅️ К опросу", "open", service_id)])
        await _send_dialog(
            message, "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
            _dialog_keyboard_limit=8,
            parse_mode=None,
        )

    async def show_readiness_choice(
        message: Message, service_id: str, title: str,
        options: list[tuple[str, str]], action: str, current: str,
    ) -> None:
        buttons = [
            readiness_button(
                f"{'✅' if value == current else '🕒'} {label}",
                action, service_id, value,
            )
            for label, value in options
        ]
        rows = [buttons[index:index + 3] for index in range(0, len(buttons), 3)]
        rows.append([readiness_button("⬅️ К опросу", "open", service_id)])
        await _send_dialog(
            message, title,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
            _dialog_keyboard_limit=sum(len(row) for row in rows),
            _dialog_keep_rows=True,
            parse_mode=None,
        )

    @admin_router.callback_query(F.data.startswith("rdy:"))
    async def on_readiness(callback: CallbackQuery) -> None:
        if (
            not callback.from_user
            or not callback.message
            or callback.message.chat.type != ChatType.PRIVATE
        ):
            await _answer_callback(callback)
            return
        parts = callback.data.split(":")
        if len(parts) not in {3, 4}:
            await _answer_callback(callback, "⚠️ Меню устарело.")
            return
        _, action, service_id, *rest = parts
        arg = rest[0] if rest else None
        actor_id = callback.from_user.id
        registry = service_registry()
        try:
            if registry is None or not is_service_admin(actor_id, service_id):
                await _answer_callback(callback, "❌ Недостаточно прав.")
                return
            service = registry.get_service(service_id)
            if service is None:
                await _answer_callback(callback, "⚠️ Служение не найдено.")
                return
            tz = context.timezone
            now = datetime.now(tz)
            year, month = readiness.target_month(now.date())
            key = f"{year:04d}-{month:02d}"
            settings = registry.get_readiness_settings(service_id)
            member_pending = pending_inputs.get(actor_id)
            if action in {"addmt", "addms"} and not (
                member_pending
                and member_pending.get("operation") == "readiness_member"
                and member_pending.get("service_id") == service_id
                and (arg or "").split(".")[0] == member_pending.get("token")
            ):
                await answer_quietly(callback, "⚠️ Этот экран уже устарел.")
                return
            if action in {"ownp", "owns"} and not (
                member_pending
                and member_pending.get("operation") == "readiness_own"
                and member_pending.get("service_id") == service_id
                and (arg or "").split(".")[0] == member_pending.get("token")
            ):
                await answer_quietly(callback, "⚠️ Этот экран уже устарел.")
                return
            plan_pending = member_pending
            if action in {
                "padd", "prem", "pclear", "pnew", "pday", "pdset",
            } and not (
                plan_pending
                and plan_pending.get("operation") == "readiness_plan"
                and plan_pending.get("service_id") == service_id
                and (arg or "").split(".")[0] == plan_pending.get("token")
            ):
                await answer_quietly(callback, "⚠️ Этот экран уже устарел.")
                return
            if (action == "pokok" and member_pending
                    and member_pending.get("operation") == "plan_warning"):
                await clean_previous_menu(callback.message, debug)
                member_pending.pop("menu_message_id", None)
            elif action in {
                "addmt", "addms", "padd", "prem", "pclear", "pnew", "pday",
                "pdset", "ownp", "owns",
            }:
                # Выбор задач идёт в одной сессии — pending не сбрасывается.
                await clean_previous_menu(callback.message, debug)
                pending_inputs[actor_id].pop("menu_message_id", None)
            else:
                await clean_and_clear_pending_actor(actor_id)
                await clean_previous_menu(callback.message, debug)
            notice = None
            if action == "summary":
                current = now.date().replace(day=1)
                following = month_end(current.year, current.month) + timedelta(days=1)
                raw_month, separator, raw_page = (arg or current.strftime("%Y%m")).partition(".")
                if raw_month not in {current.strftime("%Y%m"), following.strftime("%Y%m")}:
                    raise ValueError("Сводка доступна только за текущий и следующий месяц.")
                if separator and not raw_page.isdecimal():
                    raise ValueError("Некорректная страница сводки.")
                await show_month_graph(
                    callback.message, actor_id, service_id, int(raw_page or 0),
                    summary=True, month=date(int(raw_month[:4]), int(raw_month[4:]), 1),
                )
                await _answer_callback(callback)
                return
            if action == "month":
                if arg is not None and not arg.isdecimal():
                    raise ValueError("Некорректная страница графика.")
                await show_month_graph(
                    callback.message, actor_id, service_id, int(arg or 0),
                )
                await _answer_callback(callback)
                return
            if action == "cgraph":
                current = now.date().replace(day=1)
                if arg != current.strftime("%Y%m"):
                    raise ValueError("Этот график больше не относится к текущему месяцу.")
                await show_month_graph(
                    callback.message, actor_id, service_id,
                    month=current,
                )
                await _answer_callback(callback)
                return
            if action in PLAN_ACTIONS:
                await on_plan_action(
                    callback, action, service, arg, actor_id, now
                )
                return
            if action in {
                "start", "skip", "unskip", "progress", "extend", "finish",
                "restart", "restartok",
            } and arg != f"{year:04d}{month:02d}":
                # Экран показан до смены месяца — действие относилось бы
                # к другому месяцу.
                await show_readiness(
                    callback.message, service_id,
                    "⚠️ Экран устарел — месяц опроса сменился",
                )
                await _answer_callback(callback)
                return
            if action == "open":
                pass
            elif action == "overview":
                await show_readiness_overview(callback.message, service_id)
                await _answer_callback(callback)
                return
            elif action == "help":
                await _send_dialog(
                    callback.message,
                    f"❓ {service_label(service)} › График › Опрос › Справка\n\n"
                    "👁 Обзор показывает вступительное сообщение, вопрос и "
                    "варианты опроса — без отправки в чат.\n\n"
                    "🟢 Включить — разрешить автоматический запуск. Запуск "
                    "вручную доступен отдельно.\n\n"
                    "🗳 Запуск / ⏭ Пропуск — запустить опрос на месяц или "
                    "не запускать его автоматически.\n\n"
                    "↔️ «Наоборот» меняет смысл выбора: отмечайте даты, когда "
                    "не сможете служить.\n\n"
                    "«Команда», сроки и дни задают состав и параметры "
                    "распределения. График становится действующим только "
                    "после проверки и утверждения.",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                        readiness_button("⬅️ К опросу", "open", service_id),
                    ]]),
                    parse_mode=None,
                )
                await _answer_callback(callback)
                return
            elif action == "toggle":
                registry.set_readiness_settings(
                    actor_id, service_id, enabled=not settings["enabled"]
                )
                notice = (
                    "🔕 Автозапуск выключен" if settings["enabled"]
                    else "🟢 Автозапуск включён"
                )
            elif action == "rev":
                enabled = not settings["reverse_poll"]
                registry.set_readiness_settings(
                    actor_id, service_id, reverse_poll=enabled
                )
                notice = (
                    "↔️ Режим «Наоборот» включён — отмеченные даты "
                    "недоступны"
                    if enabled else
                    "↔️ Режим «Наоборот» выключен — отмеченные даты доступны"
                )
            elif action == "start":
                await _answer_callback(callback, "⏳ Запускаю опрос…")
                async with readiness.service_lock(service_id):
                    try:
                        await readiness.launch_run(
                            context.bot, registry, service, year, month,
                            readiness.manual_deadline(now, settings), tz,
                            debug, TELEGRAM_TEXT_LIMIT,
                        )
                        notice = "✅ Опрос отправлен в чат служения"
                    except ValueError as exc:
                        notice = f"⚠️ {exc}"
                    except Exception:  # noqa: BLE001
                        debug.exception("Не удалось запустить опрос готовности")
                        notice = "❌ Опрос отправить не удалось"
                await show_readiness(callback.message, service_id, notice)
                return
            elif action == "restart":
                run = registry.get_readiness_run(service_id, key)
                running = run is not None and run["status"] == "open"
                question = (
                    f"🔄 Перезапустить опрос на {month_title(year, month)}?\n\n"
                    "Опрос в чате будет остановлен, все ответы удалены, и "
                    "в чат уйдёт новый опрос."
                    if running else
                    f"▶️ Запустить опрос на {month_title(year, month)} "
                    "заново?\n\nОтветы завершённого опроса будут удалены, "
                    "в чат уйдёт новый опрос."
                )
                approved = registry.get_schedule_plan(service_id, key)
                if approved is not None and approved["approved"] is not None:
                    question += (
                        " Утверждённый график месяца отменится: прежний "
                        "открепится, новый опубликуется после завершения "
                        "опроса и утверждения."
                    )
                await _send_dialog(
                    callback.message,
                    question + " Отвечать придётся заново.",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [readiness_button(
                            "✅ Да, перезапустить" if running
                            else "✅ Да, запустить заново",
                            "restartok", service_id, arg,
                        )],
                        [readiness_button("⬅️ К опросу", "open", service_id)],
                    ]),
                    _dialog_keep_rows=True,
                    parse_mode=None,
                )
                await _answer_callback(callback)
                return
            elif action == "restartok":
                await _answer_callback(callback, "⏳ Перезапускаю опрос…")
                async with readiness.service_lock(service_id):
                    run = registry.get_readiness_run(service_id, key)
                    if run is not None and run["status"] == "skipped":
                        notice = "⚠️ Месяц пропущен — сначала «↩️ Не пропускать»"
                    elif run is not None and not run["poll_stopped"] and not (
                        await readiness.stop_run_poll(context.bot, run, debug)
                    ):
                        # Опрос в чате не подтверждён остановленным — иначе
                        # в чате остались бы два открытых опроса.
                        notice = (
                            "⚠️ Не удалось остановить опрос в чате — "
                            "перезапуск отменён, попробуйте позже"
                        )
                    else:
                        if run is not None:
                            registry.mark_readiness_run(
                                run["id"], poll_stopped=True
                            )
                            # Старый опрос откладывается: при сбое отправки
                            # нового он вернётся вместе с ответами; если бот
                            # прервётся — его разберёт ежеминутная проверка.
                            registry.move_readiness_run(
                                run["id"], f"{key}#{run['id']}"
                            )
                        try:
                            await readiness.launch_run(
                                context.bot, registry, service, year, month,
                                readiness.manual_deadline(now, settings), tz,
                                debug, TELEGRAM_TEXT_LIMIT,
                            )
                        except Exception as exc:  # noqa: BLE001
                            if run is not None and registry.get_readiness_run(
                                service_id, key
                            ) is not None:
                                # Новый опрос мог появиться в чате (ответ
                                # Telegram потерян) — месяц за ним; прежние
                                # ответы, как и обещал перезапуск, удаляются.
                                registry.delete_readiness_run(run["id"])
                            elif run is not None:
                                # Опрос в чате остановлен — вернуть закрытым;
                                # итог шлётся, только если ещё не был.
                                registry.move_readiness_run(run["id"], key)
                                registry.close_readiness_run(run["id"])
                            if isinstance(exc, ValueError):
                                notice = f"⚠️ {exc}"
                            else:
                                debug.exception(
                                    "Не удалось перезапустить опрос готовности"
                                )
                                notice = (
                                    "❌ Новый опрос отправить не удалось — "
                                    "прежние ответы сохранены"
                                )
                        else:
                            notice = "✅ Новый опрос отправлен в чат служения"
                            if run is not None:
                                try:
                                    registry.delete_readiness_run(run["id"])
                                except sqlite3.Error:
                                    # Разберёт ежеминутная проверка.
                                    debug.exception(
                                        "Не удалось удалить прежний опрос %s",
                                        run["id"],
                                    )
                await show_readiness(callback.message, service_id, notice)
                return
            elif action == "skip":
                # Под той же блокировкой, что и автозапуск: пропуск либо
                # успевает раньше запуска, либо видит идущий опрос.
                async with readiness.service_lock(service_id):
                    parked = any(
                        item["service_id"] == service_id
                        and item["month"].split("#")[0] == key
                        for item in registry.list_parked_readiness_runs()
                    )
                    if parked:
                        # Прерванный перезапуск ещё не разобран — пропуск
                        # стёр бы ответы прежнего опроса.
                        notice = (
                            "⚠️ Опрос этого месяца восстанавливается после "
                            "перезапуска — попробуйте через минуту"
                        )
                    else:
                        try:
                            registry.create_readiness_run(
                                service_id, key, status="skipped"
                            )
                            notice = (
                                f"⏭ {month_title(year, month)}: опрос не "
                                "запустится автоматически. График можно "
                                "составить без опроса — «🧮 Составить без "
                                "опроса»"
                            )
                        except ValueError as exc:
                            notice = f"⚠️ {exc}"
            elif action == "unskip":
                async with readiness.service_lock(service_id):
                    run = registry.get_readiness_run(service_id, key)
                    if run is not None and run["status"] == "skipped":
                        registry.delete_readiness_run(run["id"])
                notice = "↩️ Пропуск отменён"
            elif action in {"progress", "extend", "finish"}:
                run = registry.get_readiness_run(service_id, key)
                if run is None or run["status"] == "skipped":
                    notice = "⚠️ Опрос на этот месяц не идёт"
                elif action == "extend":
                    async with readiness.service_lock(service_id):
                        run = registry.get_readiness_run(service_id, key)
                        expired = readiness.deadline_of(run) <= now
                        # +1 день по местному времени (переход на зимнее
                        # время не сдвигает час окончания).
                        local = readiness.deadline_of(run).astimezone(tz)
                        new_deadline = datetime.combine(
                            local.date() + timedelta(days=1),
                            local.time(), tzinfo=tz,
                        )
                        if expired:
                            # Срок прошёл, итог подведёт ближайшая проверка.
                            notice = "⚠️ Срок опроса уже прошёл"
                        elif (
                            new_deadline.astimezone(tz).date()
                            > readiness.latest_deadline_day(run)
                        ):
                            notice = (
                                "⚠️ Срок не может выйти за начало месяца "
                                "опроса"
                            )
                        elif registry.set_readiness_deadline(
                            run["id"], new_deadline.isoformat()
                        ):
                            notice = (
                                "⏳ Срок продлён до "
                                + readiness.local_text(new_deadline, tz)
                            )
                        else:
                            notice = "⚠️ Опрос уже завершён"
                elif action == "finish":
                    await _answer_callback(callback, "⏳ Завершаю опрос…")
                    async with readiness.service_lock(service_id):
                        closed = await readiness.close_run(
                            context.bot, registry, run, service, tz,
                            context.admin_id, debug, TELEGRAM_TEXT_LIMIT,
                            getattr(
                                context, "load_upcoming_service_events", None
                            ),
                        )
                    notice = (
                        "⏹ Опрос завершён, итог отправлен админам"
                        if closed else "⚠️ Опрос уже завершён"
                    )
                    await show_readiness(callback.message, service_id, notice)
                    return
                else:
                    await _answer_callback(callback, "⏳ Считаю…")
                    await show_readiness_progress(
                        callback.message, service, run
                    )
                    return
            elif action == "dur" and arg is None:
                await show_readiness_choice(
                    callback.message, service_id,
                    "⏳ Сколько дней идёт опрос?",
                    [(f"{days} дн.", str(days)) for days in range(1, 7)],
                    "dur", str(settings["duration_days"]),
                )
                await _answer_callback(callback)
                return
            elif action == "time" and arg is None:
                await show_readiness_choice(
                    callback.message, service_id,
                    "🕒 Во сколько запускать опрос?",
                    [(value, value.replace(":", "")) for value in READINESS_TIMES],
                    "time", settings["start_time"].replace(":", ""),
                )
                await _answer_callback(callback)
                return
            elif action == "dlt" and arg is None:
                await show_readiness_choice(
                    callback.message, service_id,
                    "⏳ Во сколько заканчивается опрос?",
                    [
                        (value, value.replace(":", ""))
                        for value in READINESS_DEADLINES
                    ],
                    "dlt", settings["deadline_time"].replace(":", ""),
                )
                await _answer_callback(callback)
                return
            elif action == "texts":
                await show_readiness_texts(callback.message, service_id)
                await _answer_callback(callback)
                return
            elif action == "phr":
                await show_readiness_phrases(callback.message, service_id)
                await _answer_callback(callback)
                return
            elif action == "own":
                await show_own_days(callback.message, actor_id, service_id)
                await _answer_callback(callback)
                return
            elif action == "repl":
                # Замены (решение 17, 5в) — в разделе «📅 График».
                await open_replace_dates(callback.message, actor_id, service_id)
                await _answer_callback(callback)
                return
            elif action in {"ownp", "owns"}:
                own = pending_inputs[actor_id]
                parts_arg = (arg or "").split(".")
                index = int(parts_arg[1]) if len(parts_arg) > 1 and (
                    parts_arg[1].isdigit()
                ) else -1
                if not 0 <= index < len(own["names"]):
                    raise ValueError("Экран устарел — откройте список заново.")
                name = own["names"][index]
                if action == "ownp":
                    buttons = [
                        readiness_button(
                            f"📅 {member_day_label(offset)}", "owns",
                            service_id, f"{own['token']}.{index}.{offset}",
                        )
                        for offset in MEMBER_DAYS
                    ]
                    rows = [buttons[i:i + 3] for i in range(0, len(buttons), 3)]
                    rows.append([readiness_button(
                        "↩️ Как у всех", "owns", service_id,
                        f"{own['token']}.{index}.n",
                    )])
                    rows.append([readiness_button(
                        "⬅️ К списку", "own", service_id
                    )])
                    await _send_dialog(
                        callback.message,
                        f"📅 Свой день · {name}\n\nВ какой день человек служит "
                        "каждую неделю относительно воскресенья графика? "
                        "Например, чт — «вс −3». «Как у всех» — дни служения "
                        "раздела. Для одной недели — в правке графика.",
                        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
                        _dialog_keep_rows=True,
                        _dialog_keyboard_limit=sum(len(row) for row in rows),
                        parse_mode=None,
                    )
                    await _answer_callback(callback)
                    return
                value = parts_arg[2] if len(parts_arg) > 2 else ""
                offset = None if value == "n" else int(value)
                registry.set_service_member_day(
                    actor_id, service_id, name, offset
                )
                await show_own_days(
                    callback.message, actor_id, service_id,
                    f"✅ {name}: " + (
                        member_day_label(offset) if offset is not None
                        else "как у всех"
                    ),
                )
                await _answer_callback(callback)
                return
            elif action == "lim" and (arg is None or arg.startswith("m")):
                title, options = month_limit_choices(arg or "")
                current = service_month_limit(registry, service_id)
                await show_readiness_choice(
                    callback.message, service_id,
                    "🔢 Служений в месяц для всех в служении\n\n" + (
                        title or
                        "Максимум — желательный, при необходимости может "
                        "быть превышен; минимум — бот старается дать не "
                        "меньше. Личный "
                        "лимит — в карточке человека, он главнее."
                    ),
                    options, "lim",
                    "n" if current is None else
                    f"{current[0]}_{'x' if current[1] is None else current[1]}",
                )
                await _answer_callback(callback)
                return
            elif action == "lim":
                low, high = month_limit_value(arg)
                registry.set_readiness_settings(
                    actor_id, service_id, month_min=low, month_max=high,
                )
                notice = "✅ Служений в месяц: " + month_limit_label((low, high))
            elif action == "days" and arg is None:
                await show_readiness_choice(
                    callback.message, service_id,
                    "📆 В какие дни выполняется служение? Окно вокруг "
                    "воскресенья — в графике и публикации (Уборка: сб–пн).",
                    [(label, f"{low}_{high}") for (low, high), label
                     in DAY_WINDOWS.items()],
                    "days",
                    f"{settings['day_from']}_{settings['day_to']}",
                )
                await _answer_callback(callback)
                return
            elif action == "days" and arg is not None:
                low, _, high = arg.partition("_")
                window = (int(low), int(high))
                if window not in DAY_WINDOWS:
                    raise ValueError("Неизвестное окно дней.")
                registry.set_readiness_settings(
                    actor_id, service_id, day_from=window[0], day_to=window[1],
                )
                notice = f"✅ Дни служения: {DAY_WINDOWS[window]}"
            elif action == "rem" and arg is None:
                await show_readiness_choice(
                    callback.message, service_id,
                    "🔔 Когда напомнить не ответившим?",
                    [("выключено", "0")] + [
                        (f"за {days} дн.", str(days))
                        for days in range(1, settings["duration_days"])
                    ],
                    "rem", str(settings["remind_days"]),
                )
                await _answer_callback(callback)
                return
            elif action == "tpl" and arg in READINESS_TEXTS:
                await show_readiness_text(callback.message, service_id, arg)
                await _answer_callback(callback)
                return
            elif action == "addm" and arg and arg.count(".") == 1 and (
                arg.split(".")[0] != f"{year:04d}{month:02d}"
            ):
                notice = "⚠️ Экран устарел — месяц опроса сменился"
            elif action == "addm" and arg and arg.split(".")[-1].isdigit():
                arg = arg.split(".")[-1]
                run = registry.get_readiness_run(service_id, key)
                answer = next(
                    (
                        item for item in (
                            registry.list_readiness_answers(run["id"])
                            if run is not None else []
                        )
                        if item["telegram_id"] == int(arg)
                    ),
                    None,
                )
                # Человек уже в составе (без задач) — привязать задачи к нему,
                # а не заводить второго по имени из Telegram.
                known = next(
                    (
                        person for person in registry.readiness_people(service_id)
                        if answer is not None
                        and person["telegram_id"] == answer["telegram_id"]
                    ),
                    None,
                )
                name = (
                    known["name"] if known is not None
                    else (answer or {}).get("full_name", "").strip()
                    or (
                        f"@{answer['username']}"
                        if answer and answer["username"] else ""
                    )
                )
                if answer is None:
                    notice = "⚠️ Ответ не найден — откройте ход опроса заново"
                elif not name:
                    notice = (
                        "⚠️ У человека нет имени в Telegram — добавьте его "
                        "через «👥 Ответственные»"
                    )
                else:
                    pending_inputs[actor_id] = {
                        "operation": "readiness_member",
                        "service_id": service_id,
                        "telegram_id": answer["telegram_id"],
                        "username": answer["username"],
                        "name": name,
                        "dates": answer["dates"],
                        # Список задач экрана: отметка — по индексу в нём.
                        # Служение без задач — одна «задача» с его названием
                        # (как в графике Excel), сразу отмечена.
                        "tasks": [
                            task["name"]
                            for task in registry.plan_tasks(service_id)
                        ],
                        "selected": [
                            task["name"]
                            for task in registry.plan_tasks(service_id)
                            if task["implicit"]
                        ],
                        "step": "readiness_member",
                        # Кнопки экрана — только для этого человека.
                        "token": secrets.token_urlsafe(6),
                    }
                    await show_readiness_member(
                        callback.message, actor_id, service_id
                    )
                    await _answer_callback(callback)
                    return
            elif action == "addmt" and arg and arg.split(".")[-1].isdigit():
                tasks = pending_inputs[actor_id]["tasks"]
                selected = pending_inputs[actor_id]["selected"]
                index = int(arg.split(".")[-1])
                if index < len(tasks):
                    task = tasks[index]
                    if task in selected:
                        selected.remove(task)
                    else:
                        selected.append(task)
                await show_readiness_member(callback.message, actor_id, service_id)
                await _answer_callback(callback)
                return
            elif action == "addms":
                member = pending_inputs[actor_id]
                if not member["selected"]:
                    await show_readiness_member(
                        callback.message, actor_id, service_id,
                        "⚠️ Выберите хотя бы одну задачу",
                    )
                    await _answer_callback(callback)
                    return
                try:
                    registry.bind_service_member(
                        actor_id, service_id, member["name"],
                        member["telegram_id"], member["username"],
                        tasks=member["selected"], from_readiness=True,
                    )
                except ValueError as exc:
                    # Выбор задач сохраняется — можно вернуться к опросу.
                    await show_readiness_member(
                        callback.message, actor_id, service_id,
                        _status_text(str(exc), "⚠️"),
                    )
                    await _answer_callback(callback)
                    return
                pending_inputs.pop(actor_id, None)
                notice = (
                    f"✅ {member['name']} в составе: "
                    + ", ".join(member["selected"])
                )
                async with readiness.service_lock(service_id):
                    run = registry.get_readiness_run(service_id, key)
                    plan = registry.get_schedule_plan(service_id, key)
                    if run is not None and run["status"] == "closed" and (
                        plan is not None
                    ):
                        if (plan["edited"] or plan["approved"] is not None) and (
                            plan["run_id"] == run["id"]
                        ):
                            # Правки или утверждённый график не пересчитываются
                            # без спроса.
                            notice += (
                                "\nℹ️ График не пересчитан (есть правки или он "
                                "утверждён) — «🔄 Пересчитать», чтобы учесть "
                                "нового участника"
                            )
                        else:
                            plan = await schedule_plan.build_draft(
                                registry, run,
                                getattr(
                                    context, "load_upcoming_service_events", None
                                ),
                                debug,
                            )
                            notice += "\n🔄 График пересчитан"
                if run is not None and run["status"] == "closed" and (
                    plan is not None
                ):
                    await show_plan(callback.message, service, run, plan, notice)
                    await _answer_callback(callback)
                    return
                if run is not None and run["status"] != "skipped":
                    await show_readiness_progress(
                        callback.message, service, run, notice
                    )
                    await _answer_callback(callback)
                    return
            elif action == "people" and arg in {"o", "t"}:
                await show_task_people(callback.message, service_id, arg)
                await _answer_callback(callback)
                return
            elif action in {"ppick", "pset"} and arg:
                parts_arg = arg.split(".")
                back = parts_arg[-1] if parts_arg[-1] in {"o", "t"} else "o"
                # Позиция задачи; «s» — служение без задач.
                task = next(
                    (
                        item for item in registry.plan_tasks(service_id)
                        if item["position"] == parts_arg[0]
                    ),
                    None,
                )
                if task is None:
                    raise ValueError("Задача больше не существует.")
                if action == "ppick":
                    await show_task_people_choice(
                        callback.message, service_id, task, back
                    )
                else:
                    low, high = int(parts_arg[1]), int(parts_arg[2])
                    if task["implicit"]:
                        registry.set_readiness_settings(
                            actor_id, service_id,
                            people_needed=low, people_max=high,
                        )
                    else:
                        registry.set_service_task_people_needed(
                            actor_id, service_id, task["name"], low, high
                        )
                    await show_task_people(
                        callback.message, service_id, back,
                        f"✅ «{task['name']}»: нужно людей — "
                        + (str(low) if low == high else f"{low}–{high}"),
                    )
                await _answer_callback(callback)
                return
            elif action == "tplprev" and arg in READINESS_TEXTS and not (
                arg.startswith("ph.")
            ):
                # Как сообщение будет выглядеть: месяц и срок опроса этого
                # месяца (идущего или по расписанию), упоминания состава.
                run = registry.get_readiness_run(service_id, key)
                deadline = readiness.preview_deadline(
                    run, settings, year, month, now
                )
                if arg == "intro":
                    body = readiness.intro_text(
                        service, registry.readiness_people(service_id),
                        year, month, deadline, tz, settings["intro_template"],
                        readiness.phrase(settings, "own_days"),
                        settings["reverse_poll"],
                    )
                    where = "в чате служения перед опросом"
                elif arg == "q":
                    body = readiness.question_text(
                        service, year, month, settings["question_template"],
                        settings["reverse_poll"],
                    )
                    where = "вопрос над вариантами опроса"
                elif arg in {"rem", "chat"}:
                    body = readiness.reminder_text(
                        service,
                        {"month": key, "deadline": deadline.isoformat()},
                        tz, settings[READINESS_TEXTS[arg][0]],
                        chat=arg == "chat",
                    )
                    where = (
                        "в чате служения, ответом на опрос" if arg == "chat"
                        else "лично тем, кто не ответил"
                    )
                elif arg in {"pub", "pubupd"}:
                    body = schedule_plan.publication_title(
                        service, key, arg == "pubupd", settings,
                    )
                    where = (
                        "заголовок обновлённого графика в чате служения"
                        if arg == "pubupd"
                        else "заголовок графика в чате служения"
                    )
                else:
                    body = readiness.closed_text(
                        service, {"month": key}, settings["closed_template"],
                    )
                    where = "в чате служения, когда опрос завершён"
                await _send_dialog(
                    callback.message,
                    f"👁 Предпросмотр · {where}\n"
                    "ℹ️ " + readiness.preview_note(
                        run, settings, year, month, now
                    )
                    + (
                        " Упоминания — текущий состав."
                        if arg == "intro" else ""
                    )
                    + "\n\n──────────────────\n\n" + body,
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                        readiness_button("⬅️ К тексту", "tpl", service_id, arg)
                    ]]),
                    _dialog_text_limit=TELEGRAM_TEXT_LIMIT,
                    parse_mode=None,
                )
                await _answer_callback(callback)
                return
            elif action == "tpledit" and arg in READINESS_TEXTS:
                await ask_readiness_text(
                    callback.message, actor_id, service_id, arg
                )
                await _answer_callback(callback)
                return
            elif action == "tplreset" and arg in READINESS_TEXTS:
                save_readiness_text(registry, actor_id, service_id, arg, None)
                await show_readiness_text(
                    callback.message, service_id, arg,
                    "🔄 Возвращён встроенный текст",
                )
                await _answer_callback(callback)
                return
            elif action in {"dur", "rem"} and arg is not None:
                field = "duration_days" if action == "dur" else "remind_days"
                registry.set_readiness_settings(
                    actor_id, service_id, **{field: int(arg)}
                )
                notice = "✅ Сохранено"
            elif action in {"time", "dlt"} and arg is not None:
                if len(arg) != 4 or not arg.isdigit():
                    raise ValueError("Некорректное время.")
                field = "start_time" if action == "time" else "deadline_time"
                registry.set_readiness_settings(
                    actor_id, service_id, **{field: f"{arg[:2]}:{arg[2:]}"}
                )
                notice = "✅ Сохранено"
            else:
                notice = "⚠️ Неизвестное действие"
            await show_readiness(callback.message, service_id, notice)
            await _answer_callback(callback)
        except PermissionError as exc:
            await answer_quietly(callback, _status_text(str(exc), "⚠️"))
        except ValueError as exc:
            await show_readiness_safely(
                callback.message, service_id, _status_text(str(exc), "⚠️")
            )
            await answer_quietly(callback, _status_text(str(exc), "⚠️"))
        except Exception:  # noqa: BLE001
            # Меню уже удалено — показать экран заново, а не пустоту.
            debug.exception("Ошибка опроса готовности")
            await show_readiness_safely(
                callback.message, service_id, "❌ Не удалось выполнить действие"
            )
            await answer_quietly(callback, "❌ Не удалось выполнить действие.")

    async def answer_quietly(callback: CallbackQuery, text: str) -> None:
        """Ответ на нажатие мог уже уйти (или устареть) — не мешает экрану."""
        try:
            await _answer_callback(callback, text)
        except TelegramAPIError:
            debug.debug("Ответ на нажатие не отправлен", exc_info=True)

    async def show_readiness_safely(
        message: Message, service_id: str, notice: str
    ) -> None:
        try:
            await show_readiness(message, service_id, notice)
        except Exception:  # noqa: BLE001
            debug.exception("Не удалось показать экран опроса готовности")

    @admin_router.callback_query(F.data.startswith("xls:"))
    async def on_excel_schedule(callback: CallbackQuery) -> None:
        """«📊 График Excel»: «📥 Загрузить» месяц → предпросмотр →
        «✅ Подтвердить» (график из Excel — итоговый в боте) / «❌ Отмена»."""
        if (
            not callback.from_user
            or not callback.message
            or callback.message.chat.type != ChatType.PRIVATE
        ):
            await _answer_callback(callback)
            return
        parts = callback.data.split(":")
        if len(parts) != 4:
            await _answer_callback(callback, "⚠️ Меню устарело.")
            return
        _, action, service_id, arg = parts
        actor_id = callback.from_user.id
        registry = service_registry()
        service = service_by_id(registry, service_id) if registry else None
        if service is None or not is_service_admin(actor_id, service_id):
            await _answer_callback(callback, "❌ Недостаточно прав.")
            return
        message = callback.message
        await _answer_callback(callback)
        await clean_previous_menu(message, debug)
        try:
            if action == "load" and re.fullmatch(r"\d{6}", arg):
                await clean_and_clear_pending_actor(actor_id)
                year, month = int(arg[:4]), int(arg[4:])
                loader = getattr(context, "load_upcoming_service_events", None)
                if not 1 <= month <= 12 or loader is None:
                    raise ValueError("Загрузка графика недоступна.")
                key = f"{year:04d}-{month:02d}"
                token = secrets.token_urlsafe(6)
                pending = {
                    "operation": "excel_import", "service_id": service_id,
                    "month": key, "token": token,
                    "version": plan_version(registry.get_schedule_plan(service_id, key)),
                }
                pending_inputs[actor_id] = pending
                await _send_dialog(
                    message,
                    f"⏳ {service_label(service)}: читаю лист "
                    f"«{month_title(year, month)}»…",
                    parse_mode=None,
                )
                first = date(year, month, 1)
                if pending_inputs.get(actor_id) is not pending:
                    return
                source: dict[str, str] = {}
                try:
                    # Среда перед первым воскресеньем — в прошлом месяце,
                    # «вс +1» последнего воскресенья — уже в следующем.
                    events = await loader(
                        force=True, since=first - timedelta(days=6),
                        until=month_end(year, month) + timedelta(days=6),
                        with_replacements=False, excel_only=True,
                        source_out=source,
                    )
                except Exception:  # noqa: BLE001 — показать и не упасть
                    debug.exception("Не удалось прочитать график Excel")
                    if pending_inputs.get(actor_id) is not pending:
                        return
                    pending_inputs.pop(actor_id, None)
                    await show_excel_schedule(
                        message, actor_id, service,
                        "⚠️ Не удалось прочитать лист месяца — проверьте "
                        "файл и попробуйте снова.",
                    )
                    return
                if pending_inputs.get(actor_id) is not pending:
                    return
                if not is_service_admin(actor_id, service_id):
                    pending_inputs.pop(actor_id, None)
                    raise PermissionError("Недостаточно прав.")
                loaded = schedule_plan.excel_plan(
                    registry, service_id, year, month, events
                )
                if not any(
                    people for tasks in loaded["slots"].values()
                    for people in tasks.values()
                ):
                    pending_inputs.pop(actor_id, None)
                    await show_excel_schedule(
                        message, actor_id, service,
                        f"⚠️ На листе «{month_title(year, month)}» для "
                        "служения нет назначений.",
                    )
                    return
                pending["loaded"] = loaded
                pending["candidate_version"] = source.get("digest")
                people = registry.readiness_people(service_id)
                preview = {
                    "service_id": service_id, "month": key,
                    "dates": loaded["dates"], "tasks": loaded["tasks"],
                    "slots": loaded["slots"], "days": loaded["days"],
                }
                lines = [
                    f"📥 {service_label(service)} · график из Excel · "
                    f"{month_title(year, month)}",
                    "",
                    # Строки по воскресеньям — как в блоке «Для Excel».
                    *schedule_plan.excel_text(
                        registry, service, preview, people
                    ).split("\n")[3:],
                ]
                if loaded["unbound"]:
                    lines += ["", "⚪ Не привязаны к Telegram (напоминание "
                              "не получат): " + ", ".join(loaded["unbound"])]
                if loaded["skipped"]:
                    lines += ["", "⚠️ Не воскресенье — пропущено (задайте "
                              "задаче «📅 Дни задач»): "
                              + ", ".join(loaded["skipped"])]
                current = registry.get_schedule_plan(service_id, key)
                # Версия графика месяца на момент предпросмотра: если его
                # успели поправить или утвердить — подтвердить нельзя.
                if current is not None and current["source"] != "excel":
                    lines += ["", (
                        "⚠️ Утверждённый график по опросу будет заменён."
                        if current["approved"] is not None else
                        "⚠️ Черновик графика по опросу будет заменён."
                    ) + " Вернуть — «🔄 Пересчитать» в «📅 График»."]
                lines += ["", "«✅ Подтвердить» — этот график станет итоговым "
                          "в боте: по нему напоминания и замены."]
                rows = [[
                    InlineKeyboardButton(
                        text="✅ Подтвердить",
                        callback_data=f"xls:ok:{service_id}:{token}",
                    ),
                    InlineKeyboardButton(
                        text="❌ Отмена",
                        callback_data=f"xls:no:{service_id}:{token}",
                    ),
                ]]
                for chunk_index, chunk in enumerate(
                    chunks := _telegram_text_chunks("\n".join(lines))
                ):
                    if pending_inputs.get(actor_id) is not pending:
                        return
                    await _send_dialog(
                        message, chunk, parse_mode=None,
                        reply_markup=(
                            InlineKeyboardMarkup(inline_keyboard=rows)
                            if chunk_index == len(chunks) - 1 else None
                        ),
                    )
                return
            pending = pending_inputs.get(actor_id)
            if (
                action not in {"ok", "warnok", "no"} or pending is None
                or pending.get("operation") != "excel_import"
                or pending.get("service_id") != service_id
                or pending.get("token") != arg
            ):
                await show_excel_schedule(
                    message, actor_id, service, "⚠️ Этот экран уже устарел."
                )
                return
            if action == "no":
                pending_inputs.pop(actor_id, None)
                await show_excel_schedule(
                    message, actor_id, service, "↩️ Загрузка отменена."
                )
                return
            loaded = pending["loaded"]
            async with readiness.service_lock(service_id):
                if pending_inputs.get(actor_id) is not pending:
                    return
                candidate_version = getattr(context, "schedule_candidate_version", None)
                if candidate_version is not None and (
                    candidate_version() != pending.get("candidate_version")
                ):
                    raise ValueError(
                        "Excel изменился после предпросмотра — загрузите график заново."
                    )
                if plan_version(registry.get_schedule_plan(
                    service_id, pending["month"]
                )) != pending.get("version"):
                    raise ValueError(
                        "График месяца изменился после предпросмотра — "
                        "загрузите Excel заново."
                    )
                # Фактический день каждого — для интервалов следующего
                # месяца (как снимок при публикации).
                people = registry.readiness_people(service_id)
                offsets = schedule_plan.served_offsets(
                    {**loaded, "service_id": service_id},
                    people,
                )
                approved_days = schedule_plan.approved_slot_days(
                    {**loaded, "service_id": service_id},
                    people,
                )
                warnings = registry.task_assignment_warnings(
                    service_id, loaded["slots"], tasks=loaded["tasks"],
                    complete=True, month=pending["month"], days=loaded["days"],
                    approved_slot_days=approved_days,
                )
                acknowledged = (
                    warnings if action == "warnok"
                    and pending.get("warnings") == warnings else None
                )
                if warnings and acknowledged is None:
                    token = secrets.token_urlsafe(6)
                    pending.update(token=token, warnings=warnings)
                    lines = [
                        "⚠️ График из Excel требует внимания", "",
                        *warnings, "",
                        "Ответственные сохранятся как в Excel. "
                        "Утвердить график с этими предупреждениями?",
                    ]
                    rows = [
                        [InlineKeyboardButton(
                            text="⚠️ Утвердить с предупреждениями",
                            callback_data=f"xls:warnok:{service_id}:{token}",
                        )],
                        [InlineKeyboardButton(
                            text="⬅️ К загрузке графика",
                            callback_data=f"xls:no:{service_id}:{token}",
                        )],
                    ]
                    chunks = _telegram_text_chunks("\n".join(lines))
                    for index, chunk in enumerate(chunks):
                        if pending_inputs.get(actor_id) is not pending:
                            return
                        await _send_dialog(
                            message, chunk, parse_mode=None,
                            reply_markup=(
                                InlineKeyboardMarkup(inline_keyboard=rows)
                                if index == len(chunks) - 1 else None
                            ),
                            _dialog_keep_rows=True,
                        )
                    return
                registry.import_excel_plan(
                    actor_id, service_id, pending["month"],
                    dates=loaded["dates"], tasks=loaded["tasks"],
                    slots=loaded["slots"], days=loaded["days"],
                    served_offsets=offsets,
                    approved_slot_days=approved_days,
                    expected_version=pending["version"],
                    acknowledged_warnings=acknowledged,
                )
                pending_inputs.pop(actor_id, None)
            year, month = map(int, pending["month"].split("-"))
            await show_excel_schedule(
                message, actor_id, service,
                f"✅ График на {month_title(year, month)} из Excel — итоговый "
                "в боте: по нему напоминания и замены.",
            )
        except (PermissionError, ValueError) as exc:
            await _send_dialog(
                message, _status_text(str(exc), "❌")
                + "\n\nЗагрузите исправленный график заново.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(
                        text="⬅️ К загрузке графика",
                        callback_data=f"service:service_excel:{service_id}",
                    ),
                ]]),
                parse_mode=None,
            )
        except sqlite3.Error:
            debug.exception("Не удалось сохранить график из Excel")
            await _send_dialog(
                message,
                "❌ Ошибка базы данных. Загрузите график заново и повторите.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(
                        text="⬅️ К загрузке графика",
                        callback_data=f"service:service_excel:{service_id}",
                    ),
                ]]),
                parse_mode=None,
            )

    @admin_router.callback_query(F.data.startswith("svc_tasks:"))
    async def on_service_tasks(callback: CallbackQuery) -> None:
        if (
            not callback.from_user
            or not callback.message
            or callback.message.chat.type != ChatType.PRIVATE
        ):
            await _answer_callback(callback)
            return
        actor_id = callback.from_user.id
        registry = service_registry()
        parts = callback.data.split(":")
        action, service_id = (parts[1], parts[2]) if len(parts) >= 3 else ("", "")
        try:
            if registry is None or not is_service_admin(actor_id, service_id):
                await _answer_callback(callback, "❌ Недостаточно прав.")
                return
            if action == "open" and len(parts) == 3:
                # Карточка служения не хранится в pending — убрать её здесь,
                # иначе под экраном задач остаётся старая карточка.
                await clean_previous_menu(callback.message, debug)
                card = last_service_cards.get((actor_id, service_id))
                message_id = getattr(callback.message, "message_id", None)
                if card is not None and message_id in card[1]:
                    # Длинная карточка — несколько сообщений.
                    last_service_cards.pop((actor_id, service_id), None)
                    for card_message_id in card[1]:
                        if card_message_id != message_id:
                            await clean_previous_menu_by_id(
                                context.bot, card[0], card_message_id, debug
                            )
                previous = pending_inputs.get(actor_id)
                if previous is not None and previous.get(
                    "menu_message_id"
                ) == getattr(callback.message, "message_id", None):
                    previous.pop("menu_message_id", None)
                await show_service_tasks(callback.message, actor_id, service_id)
                await _answer_callback(callback)
                return
            pending = pending_inputs.get(actor_id)
            session_id = parts[-1]
            if (
                pending is None
                or pending.get("operation") != "service_tasks"
                or pending.get("service_id") != service_id
                or pending.get("session_id") != session_id
            ):
                await _answer_callback(callback, "⚠️ Этот экран уже устарел.")
                return
            if action == "pick_notify" and len(parts) == 4:
                if pending.get("step") != "reminder_choice":
                    await _answer_callback(
                        callback, "⚠️ Этот экран уже устарел."
                    )
                    return
                await show_service_reminder_tasks(
                    callback.message, service_id, session_id
                )
                await _answer_callback(callback)
                return
            if action in {"dep", "danchor", "dpage", "dsave", "dclear", "dback"}:
                if len(parts) != (5 if action in {"danchor", "dpage"} else 4):
                    raise ValueError("Некорректная кнопка — откройте задачу заново.")
                expected = "task_card" if action == "dep" else "task_dependency"
                if pending.get("step") != expected:
                    await _answer_callback(callback, "⚠️ Этот экран уже устарел.")
                    return
                task = next(
                    (item for item in registry.list_service_tasks(service_id)
                     if item["position"] == pending.get("task_position")), None,
                )
                if task is None:
                    raise ValueError("Задача больше не существует.")
                if action == "dep":
                    pending.pop("dependency_anchors", None)
                    pending.pop("dependency_page", None)
                elif action == "dpage":
                    pending["dependency_page"] = int(parts[3])
                elif action == "danchor":
                    anchor = next(
                        (item for item in registry.list_service_tasks(service_id)
                         if str(item["position"]) == parts[3] and item["enabled"]),
                        None,
                    )
                    if anchor is None or anchor["name"] == task["name"]:
                        raise ValueError("Выберите основную задачу из списка.")
                    selected = pending["dependency_anchors"]
                    if anchor["name"] in selected:
                        selected.remove(anchor["name"])
                    else:
                        selected.append(anchor["name"])
                elif action in {"dsave", "dclear"}:
                    selected = pending["dependency_anchors"] if action == "dsave" else []
                    if action == "dsave" and not selected:
                        raise ValueError("Выберите хотя бы одну основную задачу.")
                    registry.set_task_dependency(actor_id, service_id, task["name"], selected)
                if action in {"dsave", "dclear", "dback"}:
                    task = next(
                        item for item in registry.list_service_tasks(service_id)
                        if item["name"] == task["name"]
                    )
                    await show_service_task_card(callback.message, actor_id, service_id, task)
                else:
                    await show_task_dependency(callback.message, actor_id, service_id)
                await _answer_callback(callback)
                return
            if action in {
                "card", "notify", "pick_notify", "tog"
            } and len(parts) == 5:
                task = None
                if parts[3] != "s":
                    position = int(parts[3])
                    task = next(
                        (
                            item for item in registry.list_service_tasks(service_id)
                            if item["position"] == position
                        ),
                        None,
                    )
                    if task is None:
                        raise ValueError("Задача больше не существует.")
                if action == "card":
                    if task is None or pending.get("step") != "tasks":
                        await _answer_callback(callback, "⚠️ Этот экран уже устарел.")
                        return
                    await show_service_task_card(
                        callback.message, actor_id, service_id, task
                    )
                    await _answer_callback(callback)
                    return
                expected_step = "task_card" if task else "tasks"
                if (
                    pending.get("step") == "reminder_choice"
                    and action in {"notify", "pick_notify"}
                ):
                    expected_step = "reminder_choice"
                if (
                    pending.get("step") != expected_step
                    or (
                        task
                        and expected_step == "task_card"
                        and pending.get("task_position") != task["position"]
                    )
                ):
                    await _answer_callback(callback, "⚠️ Этот экран уже устарел.")
                    return
                if action in {"notify", "pick_notify"}:
                    if task and not task["enabled"]:
                        await _answer_callback(
                            callback, "⚠️ Сначала включите задачу."
                        )
                        return
                    service = registry.get_service(service_id)
                    await clean_and_clear_pending_actor(actor_id)
                    await ask_for_service_parameters(
                        callback.message, actor_id, "service_notify",
                        service_id, service_label(service),
                        notify_task=task["name"] if task else None,
                        notify_scoped=True,
                    )
                    await _answer_callback(callback)
                    return
                if task is None:
                    raise ValueError("Выберите задачу для изменения.")
                registry.set_service_task_enabled(
                    actor_id, service_id, task["name"], not task["enabled"]
                )
                # Выключение задачи могло выключить её напоминания.
                refresh = getattr(context, "refresh_service_reminder_jobs", None)
                if callable(refresh):
                    await refresh()
                await show_service_tasks(
                    callback.message, actor_id, service_id,
                    f"✅ «{task['name']}» "
                    f"{'выключена' if task['enabled'] else 'включена'}",
                )
            elif action == "days" and len(parts) == 4:
                await show_task_days(callback.message, service_id, session_id)
            elif action == "week" and len(parts) == 4:
                await show_task_week(callback.message, service_id, session_id)
            elif action == "wtog" and len(parts) == 5:
                position = int(parts[3])
                task = next(
                    (
                        item for item in registry.list_service_tasks(service_id)
                        if item["position"] == position
                    ),
                    None,
                )
                if task is None:
                    raise ValueError("Задача больше не существует.")
                registry.set_service_task_same_week(
                    actor_id, service_id, task["name"], not task["same_week"]
                )
                await show_task_week(
                    callback.message, service_id, session_id,
                    f"✅ «{task['name']}» "
                    + ("не в связке" if task["same_week"] else "в связке 🔗"),
                )
            elif action in {"dpick", "dset"} and len(parts) in {5, 6}:
                position = int(parts[3])
                task = next(
                    (
                        item for item in registry.list_service_tasks(service_id)
                        if item["position"] == position
                    ),
                    None,
                )
                if task is None:
                    raise ValueError("Задача больше не существует.")
                if action == "dset" and len(parts) == 6:
                    offset = None if parts[4] == "n" else int(parts[4])
                    registry.set_service_task_day(
                        actor_id, service_id, task["name"], offset
                    )
                    await show_task_days(
                        callback.message, service_id, session_id,
                        f"✅ «{task['name']}»: " + (
                            member_day_label(offset) if offset is not None
                            else "дни служения"
                        ),
                    )
                elif action == "dpick" and len(parts) == 5:
                    current = task["day_offset"]
                    buttons = [
                        InlineKeyboardButton(
                            text=(
                                f"{'✅' if offset == current else '📅'} "
                                f"{member_day_label(offset)}"
                            ),
                            callback_data=(
                                f"svc_tasks:dset:{service_id}:{position}:"
                                f"{offset}:{session_id}"
                            ),
                        )
                        for offset in MEMBER_DAYS
                    ]
                    rows = [buttons[i:i + 3] for i in range(0, len(buttons), 3)]
                    rows.append([InlineKeyboardButton(
                        text=(
                            f"{'✅' if current is None else '↩️'} "
                            "Дни служения"
                        ),
                        callback_data=(
                            f"svc_tasks:dset:{service_id}:{position}:n:"
                            f"{session_id}"
                        ),
                    )])
                    rows.append([InlineKeyboardButton(
                        text="⬅️ К дням задач",
                        callback_data=(
                            f"svc_tasks:days:{service_id}:{session_id}"
                        ),
                    )])
                    await _send_dialog(
                        callback.message,
                        f"📅 {task_emoji(task['name'])} {task['name']}\n\n"
                        "В какой день недели выполняется задача относительно "
                        "воскресенья графика? Например, спевка в среду — "
                        "«ср (вс −4)». «Дни служения» — как в разделе "
                        "«📅 График».",
                        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
                        _dialog_keep_rows=True,
                        _dialog_keyboard_limit=sum(len(row) for row in rows),
                        parse_mode=None,
                    )
                else:
                    await _answer_callback(callback, "⚠️ Неизвестное действие.")
                    return
            elif action == "add" and len(parts) == 4:
                pending["step"] = "add_task"
                await _send_dialog(
                    callback.message,
                    "ℹ️ Введите название новой задачи (до 60 символов).",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                        InlineKeyboardButton(
                            text="⬅️ Назад без изменения",
                            callback_data=f"svc_tasks:open:{service_id}",
                        )
                    ]]),
                    parse_mode=None,
                )
            else:
                await _answer_callback(callback, "⚠️ Неизвестное действие.")
                return
        except (PermissionError, ValueError) as exc:
            await _answer_callback(callback, _status_text(str(exc), "⚠️"))
            return
        except sqlite3.Error:
            debug.exception("Не удалось изменить задачи служения")
            await _answer_callback(callback, "❌ Ошибка базы данных.")
            return
        await _answer_callback(callback)

    async def refresh_group_reminder_jobs() -> None:
        refresh = getattr(context, "refresh_service_reminder_jobs", None)
        if callable(refresh):
            await refresh()

    async def ask_new_group_reminder_step(
        message: Message, pending: dict, step: str
    ) -> None:
        """Show the next step of adding a group reminder."""
        service_id = pending["service_id"]
        session_id = pending["session_id"]
        pending["step"] = step
        draft = pending["group_reminder_draft"]
        if step == "group_reminder_new_days":
            anchor = (
                f"дня задачи «{draft['task']}»" if draft.get("task")
                else "воскресенья"
            )
            text = (
                "➕ Новое напоминание · шаг 1 из 3\n\n"
                f"📅 За сколько дней до {anchor} отправить?\n"
                "ℹ️ Введите число от 0 до 6 (0 — в день служения)."
            )
            markup = notification_edit_controls(service_id, session_id)
        elif step == "group_reminder_new_time":
            text = (
                "➕ Новое напоминание · шаг 2 из 3\n\n"
                "📅 Срок: "
                + (
                    "в день служения" if draft["days"] == 0
                    else f"за {draft['days']} дн."
                )
                + "\n"
                "🕒 Введите время отправки в формате ЧЧ:ММ (например, 18:00)."
            )
            markup = notification_edit_controls(service_id, session_id)
        else:
            text = (
                "➕ Новое напоминание · шаг 3 из 3\n\n"
                f"📅 Отправка: {_rule_label(draft['days'], draft['time'])}\n"
                "📝 Введите текст напоминания или используйте шаблон "
                "по умолчанию.\n"
                + template_fields_help(
                    GROUP_TEMPLATE_FIELDS if draft.get("task")
                    else GENERAL_GROUP_TEMPLATE_FIELDS
                )
            )
            markup = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="📝 Шаблон по умолчанию",
                            callback_data=(
                                f"group_reminder:new_default:{service_id}:"
                                f"{session_id}"
                            ),
                        )
                    ],
                    *notification_edit_controls(
                        service_id, session_id
                    ).inline_keyboard,
                ]
            )
        await _send_dialog(
            message, text, reply_markup=markup,
            _dialog_text_limit=TELEGRAM_TEXT_LIMIT, parse_mode=None,
        )

    def enabled_group_reminder_at(
        registry: Any,
        service_id: str,
        days: int,
        reminder_time: str,
        *,
        excluding_id: int | None = None,
        task: str | None = None,
        period: dict | None = None,
    ) -> dict | None:
        """Включённое напоминание служения с тем же сроком, временем и
        задачей, чей период пересекается с period (None — без периода)."""
        return next(
            (
                item
                for item in registry.list_group_reminders(service_id)
                if item["enabled"]
                and item["id"] != excluding_id
                and item["reminder_days"] == days
                and item["reminder_time"] == reminder_time
                and item.get("task") == task
                and periods_overlap(item, period or {})
            ),
            None,
        )

    async def finish_new_group_reminder(
        message: Message, actor_id: int, service_id: str
    ) -> None:
        """Save the drafted reminder or offer merging with a same-time one."""
        pending = pending_inputs.get(actor_id)
        registry = service_registry()
        if pending is None or registry is None:
            await _send_dialog(message, "Настройка устарела.", parse_mode=None)
            return
        draft = pending["group_reminder_draft"]
        target = enabled_group_reminder_at(
            registry, service_id, draft["days"], draft["time"],
            task=draft.get("task"),
        )
        if target is not None:
            await offer_group_reminder_merge(
                message,
                actor_id,
                service_id,
                target,
                days=draft["days"],
                time=draft["time"],
                body=draft["body"],
                source_id=None,
            )
            return
        # Совпадение только с выключенным напоминанием не дублирует отправку.
        reminder_id = registry.add_group_reminder(
            actor_id, service_id, draft["days"], draft["time"], draft["body"],
            task=draft.get("task"),
            allow_duplicate=True,
        )
        pending.pop("group_reminder_draft", None)
        await refresh_group_reminder_jobs()
        await enter_group_reminder_card(
            message, actor_id, service_id, reminder_id
        )

    async def offer_group_reminder_merge(
        message: Message,
        actor_id: int,
        service_id: str,
        target: dict,
        *,
        days: int,
        time: str,
        body: str | None,
        source_id: int | None,
        source_slot: tuple[int, str] | None = None,
        period: dict | None = None,
    ) -> None:
        """Offer to merge a reminder into an existing one with the same slot.

        source_id is None for a reminder that is being added and the id of an
        existing reminder whose schedule is being changed otherwise.
        """
        pending = pending_inputs.get(actor_id)
        registry = service_registry()
        service = registry.get_service(service_id) if registry else None
        if pending is None or service is None:
            await _send_dialog(message, "Настройка устарела.", parse_mode=None)
            return
        existing_text = registry.resolve_group_text(target, service)
        # Объединяются только напоминания одной задачи: текст источника
        # без своего тела — шаблон этой задачи (или служения для общих).
        new_text = registry.resolve_group_text(
            {"body": body, "task": target.get("task")}, service
        )
        same_text = existing_text.strip() == new_text.strip()
        merged_body = (
            None if same_text
            else existing_text.rstrip() + "\n\n" + new_text.strip()
        )
        request_id = secrets.token_urlsafe(6)
        session_id = pending["session_id"]
        pending["step"] = "group_reminder_merge"
        pending["group_reminder_merge"] = {
            "target_id": target["id"],
            "target_slot": (target["reminder_days"], target["reminder_time"]),
            "source_id": source_id,
            "source_slot": source_slot,
            "days": days,
            "time": time,
            "body": body,
            "task": target.get("task"),
            "merged_body": merged_body,
            "request_id": request_id,
        }

        def merge_is_current() -> bool:
            active = pending_inputs.get(actor_id)
            return (
                active is pending
                and active.get("session_id") == session_id
                and active.get("step") == "group_reminder_merge"
                and (active.get("group_reminder_merge") or {}).get(
                    "request_id"
                ) == request_id
            )

        def merge_button(text: str, action: str) -> InlineKeyboardButton:
            return InlineKeyboardButton(
                text=text,
                callback_data=(
                    f"group_reminder:{action}:{service_id}:{request_id}:"
                    f"{session_id}"
                ),
            )

        slot = _rule_label(days, time)
        cancel_row = [merge_button("✖️ Отмена", "mrg_no")]
        period = period or {}
        if (target.get("active_from"), target.get("active_until")) != (
            period.get("active_from"), period.get("active_until")
        ):
            # Напоминания с разными периодами не объединяются (этап 4.2).
            today = datetime.now(context.timezone).date()
            await _send_dialog(
                message,
                f"⚠️ На «{slot}» уже есть напоминание с периодом "
                f"{period_text(target, today)}.\n"
                f"Период этого напоминания: {period_text(period, today)}.\n\n"
                "Напоминания с разными периодами не объединяются. Сохраните "
                "отдельно и задайте периоды в «🗓 Период» так, чтобы они не "
                "пересекались, — иначе в общие воскресенья уйдут оба.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [merge_button("➕ Отдельно", "mrg_sep")],
                    cancel_row,
                ]),
                parse_mode=None,
            )
            return
        if same_text:
            await _send_dialog(
                message,
                f"ℹ️ Напоминание «{slot}» с таким же текстом уже есть.\n\n"
                "Можно оставить одно напоминание или отправлять этот текст "
                "дважды отдельными сообщениями.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [
                        merge_button("🔗 Оставить одно", "mrg_ok"),
                        merge_button("➕ Отдельно", "mrg_sep"),
                    ],
                    cancel_row,
                ]),
                parse_mode=None,
            )
            return

        await _send_dialog(
            message,
            "⏳ Готовлю предпросмотр объединённого сообщения...",
            parse_mode=None,
        )
        result = None
        preview_loader = getattr(context, "get_group_reminder_preview", None)
        if callable(preview_loader):
            try:
                result = await preview_loader(target["id"], merged_body)
            except Exception:
                debug.exception(
                    "Не удалось подготовить предпросмотр объединения "
                    "групповых напоминаний"
                )
        if not merge_is_current():
            return

        header = (
            f"⚠️ На «{slot}» уже есть напоминание.\n"
            "Их можно объединить в одно сообщение"
        )
        too_long = False
        if result and result.get("has_data"):
            preview_text = result.get("text") or "⚪ Текст напоминания пуст."
            too_long = visible_units(preview_text) > TELEGRAM_TEXT_LIMIT
            preview_chunks = _telegram_text_chunks(preview_text, limit=3000)
            if len(preview_chunks) > 1:
                preview_text = preview_chunks[0] + "\n... (текст обрезан)"
            text = (
                f"{header}. Так оно будет выглядеть на {result['date']}:\n\n"
                "──────────────────\n\n"
                f"{preview_text}"
            )
        else:
            # Без графика итоговую длину не проверить — объединение
            # не предлагается, чтобы не сохранить неотправляемый текст.
            too_long = True
            reason = (
                result.get("error") if result and result.get("error")
                else "Не удалось загрузить график."
            )
            template_chunks = _telegram_text_chunks(merged_body, limit=3000)
            template_text = template_chunks[0] + (
                "\n... (текст обрезан)" if len(template_chunks) > 1 else ""
            )
            text = (
                f"{header}. Текст объединённого сообщения "
                "(поля в скобках заполнятся из графика):\n\n"
                "──────────────────\n\n"
                f"{template_text}\n\n"
                f"ℹ️ {reason}"
            )
        rows = []
        if too_long and result and result.get("has_data"):
            text += (
                "\n\n⚠️ Объединённое сообщение длиннее лимита Telegram "
                "и не будет отправлено. Сохраните напоминания отдельно."
            )
        elif too_long:
            text += (
                "\n\n⚠️ Без графика нельзя проверить длину итогового "
                "сообщения, поэтому объединение сейчас недоступно. "
                "Попробуйте позже или сохраните отдельно."
            )
        else:
            rows.append([merge_button("🔗 Объединить", "mrg_ok")])
        rows.append([merge_button("➕ Отдельным сообщением", "mrg_sep")])
        rows.append(cancel_row)
        await _send_dialog(
            message,
            text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
            _dialog_text_limit=4096,
            parse_mode=None,
        )

    @admin_router.callback_query(F.data.startswith("group_reminder:"))
    async def on_group_reminder_action(callback: CallbackQuery) -> None:
        if (
            not callback.from_user
            or not callback.message
            or callback.message.chat.type != ChatType.PRIVATE
        ):
            await _answer_callback(callback)
            return
        parts = callback.data.split(":")
        if len(parts) not in {4, 5}:
            await _answer_callback(callback, "⚠️ Настройка устарела.")
            return
        _, action, service_id, *tail = parts
        reminder_id = None
        merge_token = None
        is_preview = action == "preview"
        if action in {"mrg_ok", "mrg_sep", "mrg_no"}:
            if len(tail) != 2:
                await _answer_callback(callback, "⚠️ Настройка устарела.")
                return
            merge_token, session_id = tail
        elif is_preview:
            if len(tail) != 2:
                await _answer_callback(callback, "⚠️ Настройка устарела.")
                return
            try:
                reminder_id = int(tail[0])
            except ValueError:
                await _answer_callback(callback, "⚠️ Некорректный идентификатор.")
                return
            session_id = tail[1]
        elif len(tail) == 2:
            try:
                reminder_id = int(tail[0])
            except ValueError:
                await _answer_callback(callback, "⚠️ Некорректный идентификатор.")
                return
            session_id = tail[1]
        else:
            session_id = tail[0]
        actor_id = callback.from_user.id
        pending = pending_inputs.get(actor_id)
        registry = service_registry()
        if (
            pending is None
            or pending.get("operation") != "service_notify"
            or pending.get("service_id") != service_id
            or pending.get("session_id") != session_id
            or registry is None
        ):
            await _answer_callback(
                callback, "⚠️ Настройка устарела. Запустите /service_notify заново."
            )
            return
        if is_preview and (
            pending.get("step") != "group_reminder_card"
            or pending.get("reminder_id") != reminder_id
        ):
            await _answer_callback(callback, "⚠️ Этот экран уже устарел.")
            return
        try:
            if not is_service_admin(actor_id, service_id):
                await _answer_callback(callback, "❌ Недостаточно прав.")
                return
        except sqlite3.Error:
            debug.exception("Не удалось проверить права на групповые напоминания")
            await _answer_callback(callback, "❌ Не удалось проверить права.")
            return

        async def refresh_jobs() -> None:
            refresh = getattr(context, "refresh_service_reminder_jobs", None)
            if callable(refresh):
                await refresh()

        callback_answered = False
        try:
            if action == "summary":
                if pending.get("step") != "reminder_menu":
                    await _answer_callback(callback, "⚠️ Этот экран уже устарел.")
                    return
                pending["step"] = "personal_summary"
                await show_personal_reminder_summary(
                    callback.message,
                    actor_id,
                    service_id,
                    f"group_reminder:summary_back:{service_id}:{session_id}",
                )
            elif action == "summary_back":
                if pending.get("step") != "personal_summary":
                    await _answer_callback(callback, "⚠️ Этот экран уже устарел.")
                    return
                pending["step"] = "reminder_menu"
                await show_reminder_rules_menu(
                    callback.message, actor_id, service_id
                )
            elif action == "default":
                if pending.get("step") != "menu":
                    await _answer_callback(callback, "⚠️ Этот экран уже устарел.")
                    return
                await enter_default_template(callback.message, actor_id, service_id)
            elif action == "report":
                if pending.get("step") != "menu":
                    await _answer_callback(callback, "⚠️ Этот экран уже устарел.")
                    return
                await enter_admin_report_template(
                    callback.message, actor_id, service_id
                )
            elif action == "edit_report":
                if pending.get("step") != "admin_report":
                    await _answer_callback(callback, "⚠️ Этот экран уже устарел.")
                    return
                pending["step"] = "admin_report_text"
                await _send_dialog(
                    callback.message,
                    "Введите шаблон отчёта админам.\n\n"
                    + template_fields_help(REPORT_FIELDS),
                    reply_markup=notification_edit_controls(
                        service_id, session_id
                    ),
                    parse_mode=None,
                )
            elif action == "tpl_preview":
                await show_template_preview(
                    callback, actor_id, service_id, session_id
                )
                callback_answered = True
            elif action == "tpl_back":
                if pending.get("step") != "template_preview":
                    await _answer_callback(callback, "⚠️ Этот экран уже устарел.")
                    return
                if pending.get("template_preview_kind") == "report":
                    await enter_admin_report_template(
                        callback.message, actor_id, service_id
                    )
                else:
                    await enter_default_template(
                        callback.message, actor_id, service_id
                    )
            elif action == "reset_report":
                if pending.get("step") != "admin_report":
                    await _answer_callback(callback, "⚠️ Этот экран уже устарел.")
                    return
                pending["admin_report_template"] = None
                pending["admin_report_template_edited"] = True
                await enter_admin_report_template(
                    callback.message, actor_id, service_id
                )
            elif action == "root_back":
                if pending.get("step") not in {
                    "default_template",
                    "admin_report",
                    "reminder_menu",
                }:
                    await _answer_callback(callback, "⚠️ Этот экран уже устарел.")
                    return
                await show_notification_settings_menu(
                    callback.message, actor_id, service_id
                )
            elif action == "list":
                if pending.get("step") not in {
                    "menu",
                    "group_reminder_preview",
                }:
                    await _answer_callback(callback, "⚠️ Этот экран уже устарел.")
                    return
                await show_reminder_rules_menu(
                    callback.message, actor_id, service_id
                )
            elif action == "page":
                if pending.get("step") != "reminder_menu" or reminder_id is None:
                    await _answer_callback(callback, "⚠️ Этот экран уже устарел.")
                    return
                pages = max(
                    1, (len(notification_reminders(registry, pending)) + 5) // 6
                )
                if (
                    not 0 <= reminder_id < pages
                    or abs(reminder_id - pending.get("reminder_page", 0)) != 1
                ):
                    await _answer_callback(callback, "⚠️ Этот экран уже устарел.")
                    return
                await show_reminder_rules_menu(
                    callback.message, actor_id, service_id, page=reminder_id
                )
            elif action == "send_now":
                reminder = registry.get_group_reminder(reminder_id)
                if (
                    pending.get("step") != "group_reminder_card"
                    or pending.get("reminder_id") != reminder_id
                    or reminder is None
                    or reminder["service_id"] != service_id
                ):
                    await _answer_callback(callback, "⚠️ Этот экран уже устарел.")
                    return
                await show_send_now_confirm(
                    callback.message, actor_id, service_id, reminder_id
                )
            elif action == "send_now_ok":
                prepared = pending.pop("send_now", None)
                service = registry.get_service(service_id)
                if (
                    prepared is None
                    or prepared["reminder_id"] != reminder_id
                    or prepared["snapshot"] != send_now_snapshot(
                        registry.get_group_reminder(reminder_id)
                    )
                    or service is None
                    or not service.get("group_chat_id")
                ):
                    await _answer_callback(callback, "⚠️ Этот экран уже устарел.")
                    return
                try:
                    check_state = getattr(context, "group_preview_state", None)
                    if check_state is not None and (
                        prepared.get("event_date") is None
                        or check_state(service_id, prepared["event_date"])
                        != prepared.get("schedule_state")
                    ):
                        await _answer_callback(
                            callback, "⚠️ График изменился. Откройте предпросмотр заново."
                        )
                        return
                    if prepared.get("assignment_state"):
                        field_state = getattr(context, "assignment_fields_state", None)
                        if field_state is None or field_state(
                            service_id, prepared["event_date"],
                            prepared["template"], prepared["task"],
                        ) != prepared["assignment_state"]:
                            await _answer_callback(
                                callback,
                                "⚠️ Связанные назначения или дата изменились. "
                                "Откройте предпросмотр заново.",
                            )
                            return
                    await send_to_service_group(
                        lambda chat_id, text, **kwargs: send_telegram_screen(
                            context.bot.send_message, chat_id, text,
                            limit=TELEGRAM_TEXT_LIMIT, **kwargs,
                        ),
                        service["group_chat_id"], prepared["text"], service,
                        registry=registry, logger=debug,
                    )
                except Exception:  # noqa: BLE001
                    debug.exception("Не удалось отправить напоминание сейчас")
                    await _answer_callback(
                        callback, "❌ Не удалось отправить в чат служения."
                    )
                    return
                interaction.info(
                    "Групповое напоминание отправлено вручную: service=%s "
                    "reminder=%s actor=%s", service_id, reminder_id, actor_id,
                )
                await enter_group_reminder_card(
                    callback.message, actor_id, service_id, reminder_id
                )
                await _answer_callback(callback, "✅ Отправлено в чат служения")
                callback_answered = True
            elif action == "preview":
                preview_session_id = session_id
                reminder = registry.get_group_reminder(reminder_id)
                if reminder is None or reminder["service_id"] != service_id:
                    raise ValueError(
                        "Групповое напоминание больше не существует."
                    )
                service = registry.get_service(service_id)
                if service is None:
                    raise ValueError("Служение не найдено.")
                occurrence = reminder_occurrence(
                    datetime.now(context.timezone), reminder
                )
                if occurrence is None:
                    raise ValueError("Период напоминания завершён.")
                event_date, send_date = occurrence
                preview_dates = {
                    "date": event_date.strftime("%d.%m.%Y"),
                    "send_date": send_date.strftime("%d.%m.%Y"),
                    "time": reminder["reminder_time"],
                }
                preview_request_id = secrets.token_urlsafe(12)
                pending["preview_request_id"] = preview_request_id

                def preview_is_current(*, require_preview_step: bool) -> bool:
                    active = pending_inputs.get(actor_id)
                    return (
                        active is pending
                        and active.get("session_id") == preview_session_id
                        and active.get("reminder_id") == reminder_id
                        and active.get("preview_request_id")
                        == preview_request_id
                        and active.get("step")
                        == (
                            "group_reminder_preview"
                            if require_preview_step
                            else "group_reminder_card"
                        )
                    )

                await _answer_callback(callback)
                callback_answered = True
                if not preview_is_current(require_preview_step=False):
                    return
                pending["step"] = "group_reminder_preview"
                await _send_dialog(
                    callback.message,
                    "⏳ Загружаю данные графика для предпросмотра...",
                    parse_mode=None,
                )

                if not preview_is_current(require_preview_step=True):
                    return

                preview_loader = getattr(
                    context, "get_group_reminder_preview", None
                )
                if not callable(preview_loader):
                    debug.error(
                        "Функция предпросмотра группового напоминания "
                        "не настроена"
                    )
                    result = {
                        **preview_dates,
                        "text": "",
                        "has_data": False,
                        "error": "Не удалось загрузить график. Попробуйте позже.",
                    }
                else:
                    try:
                        result = await preview_loader(reminder_id)
                    except sqlite3.Error:
                        debug.exception(
                            "Не удалось прочитать настройки напоминания "
                            "для предпросмотра"
                        )
                        result = {
                            **preview_dates,
                            "text": "",
                            "has_data": False,
                            "error": (
                                "Не удалось прочитать настройки напоминания."
                            ),
                        }
                    except ValueError as exc:
                        debug.exception(
                            "Предпросмотр группового напоминания "
                            "стал недоступен во время загрузки"
                        )
                        result = {
                            **preview_dates,
                            "text": "",
                            "has_data": False,
                            "error": str(exc),
                        }
                    except Exception:
                        debug.exception(
                            "Не удалось загрузить график для предпросмотра"
                        )
                        result = {
                            **preview_dates,
                            "text": "",
                            "has_data": False,
                            "error": (
                                "Не удалось загрузить график. Попробуйте позже."
                            ),
                        }

                if not preview_is_current(require_preview_step=True):
                    return

                preview_date = result.get("date", "")
                header = (
                    f"👁 Предпросмотр · {service_label(service)}\n\n"
                    f"📅 Собрание: {preview_date}\n"
                    f"✉️ Отправка: {result.get('send_date', '—')} "
                    f"в {result['time']}\n\n"
                    "──────────────────\n\n"
                )
                if result.get("error") == (
                    "Не удалось прочитать настройки напоминания."
                ):
                    text = f"❌ {result['error']}"
                elif result.get("error"):
                    text = header
                    if result["error"].startswith(
                        "Нет данных из графика"
                    ):
                        text += (
                            f"⚠️ {result['error']}\n"
                            "Предпросмотр временно недоступен."
                        )
                    elif result["error"].startswith(
                        "Не удалось загрузить график"
                    ):
                        text += f"⚠️ {result['error']}"
                    else:
                        text += f"❌ {result['error']}"
                elif result.get("has_data"):
                    preview_text = (
                        result.get("text") or "⚪ Текст напоминания пуст."
                    )
                    preview_chunks = _telegram_text_chunks(
                        preview_text, limit=3800
                    )
                    if len(preview_chunks) > 1:
                        preview_text = (
                            preview_chunks[0] + "\n... (текст обрезан)"
                        )
                    text = header + preview_text
                    text += "".join(
                        f"\n\n⚠️ {warning}" for warning in result.get("warnings", [])
                    )
                else:
                    text = header + "⚠️ Предпросмотр временно недоступен."
                back_button = InlineKeyboardMarkup(
                    inline_keyboard=[[
                        InlineKeyboardButton(
                            text=(
                                "⬅️ К напоминаниям"
                                if result.get("error")
                                else "⬅️ К напоминанию"
                            ),
                            callback_data=(
                                (
                                    f"group_reminder:list:{service_id}:"
                                    f"{preview_session_id}"
                                )
                                if result.get("error")
                                else (
                                    f"group_reminder:back:{service_id}:"
                                    f"{preview_session_id}"
                                )
                            ),
                        )
                    ]]
                )
                await _send_dialog(
                    callback.message,
                    text,
                    reply_markup=back_button,
                    _dialog_text_limit=4096,
                    parse_mode=None,
                )
            elif action == "edit_default":
                if pending.get("step") != "default_template":
                    await _answer_callback(callback, "⚠️ Этот экран уже устарел.")
                    return
                pending["step"] = "group_default_text"
                await _send_dialog(
                    callback.message,
                    "Введите шаблон по умолчанию.\n\n"
                    + template_fields_help(GROUP_FIELDS),
                    reply_markup=notification_edit_controls(
                        service_id, session_id
                    ),
                    parse_mode=None,
                )
            elif action == "reset_default":
                if pending.get("step") != "default_template":
                    await _answer_callback(callback, "⚠️ Этот экран уже устарел.")
                    return
                pending["group_template"] = None
                await enter_default_template(
                    callback.message, actor_id, service_id
                )
            elif action == "add":
                if pending.get("step") != "reminder_menu":
                    await _answer_callback(callback, "⚠️ Этот экран уже устарел.")
                    return
                pending["group_reminder_draft"] = {
                    "task": pending.get("notify_task")
                    if pending.get("notify_scoped") else None,
                }
                await ask_new_group_reminder_step(
                    callback.message, pending, "group_reminder_new_days"
                )
            elif action == "new_default":
                if (
                    pending.get("step") != "group_reminder_new_text"
                    or pending.get("group_reminder_draft") is None
                ):
                    await _answer_callback(callback, "⚠️ Этот экран уже устарел.")
                    return
                pending["group_reminder_draft"]["body"] = None
                await finish_new_group_reminder(
                    callback.message, actor_id, service_id
                )
            elif action in {"mrg_ok", "mrg_sep", "mrg_no"}:
                merge = pending.get("group_reminder_merge")
                if (
                    pending.get("step") != "group_reminder_merge"
                    or merge is None
                    or merge["request_id"] != merge_token
                ):
                    await _answer_callback(callback, "⚠️ Этот экран уже устарел.")
                    return
                pending.pop("group_reminder_merge", None)
                pending.pop("group_reminder_draft", None)
                source_id = merge["source_id"]
                if action == "mrg_ok":
                    if source_id is not None or merge["merged_body"] is not None:
                        merge_changes = (
                            {"body": merge["merged_body"]}
                            if merge["merged_body"] is not None
                            else {}
                        )
                        registry.merge_group_reminder(
                            actor_id,
                            merge["target_id"],
                            expected_slot=merge["target_slot"],
                            source_id=source_id,
                            source_slot=merge["source_slot"],
                            **merge_changes,
                        )
                        await refresh_jobs()
                    await enter_group_reminder_card(
                        callback.message, actor_id, service_id,
                        merge["target_id"],
                    )
                elif action == "mrg_sep":
                    if source_id is None:
                        opened_id = registry.add_group_reminder(
                            actor_id,
                            service_id,
                            merge["days"],
                            merge["time"],
                            merge["body"],
                            task=merge["task"],
                            allow_duplicate=True,
                        )
                    else:
                        registry.update_group_reminder(
                            actor_id,
                            source_id,
                            days=merge["days"],
                            time=merge["time"],
                            allow_duplicate=True,
                        )
                        opened_id = source_id
                    await refresh_jobs()
                    await enter_group_reminder_card(
                        callback.message, actor_id, service_id, opened_id
                    )
                elif source_id is None:
                    await show_reminder_rules_menu(
                        callback.message, actor_id, service_id
                    )
                else:
                    await enter_group_reminder_card(
                        callback.message, actor_id, service_id, source_id
                    )
            elif action == "back":
                if pending.get("step") not in {
                    "group_reminder_card",
                    "group_reminder_delete",
                    "group_reminder_preview",
                    "group_reminder_task",
                    "group_reminder_period",
                    "group_reminder_period_input",
                }:
                    await _answer_callback(callback, "⚠️ Этот экран уже устарел.")
                    return
                if pending["step"] in {
                    "group_reminder_delete",
                    "group_reminder_preview",
                    "group_reminder_task",
                    "group_reminder_period",
                    "group_reminder_period_input",
                }:
                    await enter_group_reminder_card(
                        callback.message,
                        actor_id,
                        service_id,
                        pending["reminder_id"],
                    )
                else:
                    await show_reminder_rules_menu(
                        callback.message, actor_id, service_id
                    )
            elif action == "period_menu":
                if (
                    pending.get("step") != "group_reminder_card"
                    or reminder_id != pending.get("reminder_id")
                ):
                    await _answer_callback(callback, "⚠️ Этот экран уже устарел.")
                    return
                await show_group_reminder_period(
                    callback.message, actor_id, service_id, reminder_id
                )
            elif action in {"period_from", "period_until"}:
                # reminder_id здесь — напоминание × PERIOD_CHOICE_BASE + номер
                # воскресенья (0 — без границы).
                target_id, index = divmod(reminder_id, PERIOD_CHOICE_BASE)
                choices = pending.get("period_choices", [])
                if (
                    pending.get("step") != "group_reminder_period"
                    or target_id != pending.get("reminder_id")
                    or pending.get("period_pick_until") != (action == "period_until")
                    or not 0 <= index <= len(choices)
                ):
                    await _answer_callback(callback, "⚠️ Этот экран уже устарел.")
                    return
                picked = choices[index - 1] if index else None
                if action == "period_from":
                    await show_group_reminder_period(
                        callback.message, actor_id, service_id,
                        pending["reminder_id"], picked, pick_until=True,
                    )
                else:
                    registry.set_group_reminder_period(
                        actor_id, pending["reminder_id"],
                        pending.get("period_from"), picked,
                        today=datetime.now(context.timezone).date(),
                    )
                    await refresh_jobs()
                    await enter_group_reminder_card(
                        callback.message, actor_id, service_id,
                        pending["reminder_id"],
                    )
            elif action == "period_clear":
                if (
                    pending.get("step") != "group_reminder_period"
                    or reminder_id != pending.get("reminder_id")
                ):
                    await _answer_callback(callback, "⚠️ Этот экран уже устарел.")
                    return
                registry.set_group_reminder_period(
                    actor_id, pending["reminder_id"], None, None,
                    today=datetime.now(context.timezone).date(),
                )
                await refresh_jobs()
                await enter_group_reminder_card(
                    callback.message, actor_id, service_id, pending["reminder_id"]
                )
            elif action == "period_input":
                if (
                    pending.get("step") != "group_reminder_period"
                    or reminder_id != pending.get("reminder_id")
                ):
                    await _answer_callback(callback, "⚠️ Этот экран уже устарел.")
                    return
                pending["step"] = "group_reminder_period_input"
                pending["group_reminder_input"] = {
                    "reminder_id": pending["reminder_id"], "field": "period",
                }
                await _send_dialog(
                    callback.message,
                    "Введите даты воскресений «с — по», например: "
                    "01.11.2026 27.12.2026. Без начала: «- 27.12.2026», "
                    "без окончания: «01.11.2026 -».",
                    reply_markup=notification_edit_controls(
                        service_id, session_id
                    ),
                    parse_mode=None,
                )
            elif action == "task_menu":
                if pending.get("step") != "group_reminder_card":
                    await _answer_callback(callback, "⚠️ Этот экран уже устарел.")
                    return
                await show_group_reminder_tasks(
                    callback.message, actor_id, service_id, reminder_id
                )
            elif action == "task_pick":
                # reminder_id здесь — номер варианта (0 — все задачи).
                choices = pending.get("task_choices", [])
                if (
                    pending.get("step") != "group_reminder_task"
                    or not 0 <= reminder_id <= len(choices)
                ):
                    await _answer_callback(callback, "⚠️ Этот экран уже устарел.")
                    return
                registry.set_group_reminder_task(
                    actor_id,
                    pending["reminder_id"],
                    choices[reminder_id - 1] if reminder_id else None,
                )
                if pending.get("notify_scoped"):
                    pending["notify_task"] = (
                        choices[reminder_id - 1] if reminder_id else None
                    )
                    pending["reminder_page"] = 0
                await enter_group_reminder_card(
                    callback.message, actor_id, service_id,
                    pending["reminder_id"],
                )
            elif action in {"open", "nav"}:
                expected_step = (
                    "reminder_menu" if action == "open" else "group_reminder_card"
                )
                allowed_ids = {
                    item["id"]
                    for item in notification_reminders(registry, pending)
                }
                if action == "open":
                    allowed_ids.intersection_update(
                        pending.get("reminder_menu_ids", [])
                    )
                if (
                    pending.get("step") != expected_step
                    or reminder_id not in allowed_ids
                ):
                    await _answer_callback(callback, "⚠️ Этот экран уже устарел.")
                    return
                await enter_group_reminder_card(
                    callback.message, actor_id, service_id, reminder_id
                )
            elif action in {"edit_days", "edit_time", "edit_text"}:
                if pending.get("step") != "group_reminder_card":
                    await _answer_callback(callback, "⚠️ Этот экран уже устарел.")
                    return
                reminder = registry.get_group_reminder(reminder_id)
                if reminder is None or reminder["service_id"] != service_id:
                    raise ValueError("Групповое напоминание больше не существует.")
                field = action.removeprefix("edit_")
                pending["group_reminder_input"] = {
                    "reminder_id": reminder_id,
                    "field": field,
                }
                pending["step"] = f"group_reminder_{field}"
                prompts = {
                    "days": f"Введите срок до события от 0 до 6. Сейчас: "
                    f"{reminder['reminder_days']}.",
                    "time": f"Введите локальное время HH:MM. Сейчас: "
                    f"{reminder['reminder_time']}.",
                    "text": "Введите собственный текст напоминания.\n\n"
                    + template_fields_help(
                        GROUP_FIELDS + ("task", "task_responsibles")
                        if reminder.get("task")
                        else GROUP_FIELDS
                    ),
                }
                await _send_dialog(
                    callback.message,
                    prompts[field],
                    reply_markup=notification_edit_controls(
                        service_id, session_id
                    ),
                    parse_mode=None,
                )
            elif action in {"toggle", "reset_text"}:
                if pending.get("step") != "group_reminder_card":
                    await _answer_callback(callback, "⚠️ Этот экран уже устарел.")
                    return
                reminder = registry.get_group_reminder(reminder_id)
                if reminder is None or reminder["service_id"] != service_id:
                    raise ValueError("Групповое напоминание больше не существует.")
                if action == "toggle":
                    if not reminder["enabled"] and reminder_period_status(
                        reminder, datetime.now(context.timezone).date()
                    ) == "ended":
                        raise ValueError(
                            "Период напоминания завершён — задайте новый "
                            "период в «🗓 Период»."
                        )
                    registry.update_group_reminder(
                        actor_id, reminder_id,
                        enabled=not reminder["enabled"],
                    )
                    await refresh_jobs()
                else:
                    registry.update_group_reminder(
                        actor_id, reminder_id, body=None
                    )
                await enter_group_reminder_card(
                    callback.message, actor_id, service_id, reminder_id
                )
            elif action == "delete_prompt":
                if pending.get("step") != "group_reminder_card":
                    await _answer_callback(callback, "⚠️ Этот экран уже устарел.")
                    return
                pending["step"] = "group_reminder_delete"
                confirm = InlineKeyboardMarkup(
                    inline_keyboard=[[
                        InlineKeyboardButton(
                            text="🗑 Да, удалить",
                            callback_data=(
                                f"group_reminder:delete:{service_id}:"
                                f"{reminder_id}:{session_id}"
                            ),
                        ),
                        InlineKeyboardButton(
                            text="⬅️ Отмена",
                            callback_data=(
                                f"group_reminder:back:{service_id}:"
                                f"{reminder_id}:{session_id}"
                            ),
                        ),
                    ]]
                )
                await _send_dialog(
                    callback.message,
                    "Удалить это групповое напоминание?",
                    reply_markup=confirm,
                    parse_mode=None,
                )
            elif action == "delete":
                if pending.get("step") != "group_reminder_delete":
                    await _answer_callback(callback, "⚠️ Подтверждение устарело.")
                    return
                registry.delete_group_reminder(actor_id, reminder_id)
                await refresh_jobs()
                await show_reminder_rules_menu(
                    callback.message, actor_id, service_id
                )
            elif action == "cancel_input":
                input_info = pending.pop("group_reminder_input", None)
                if input_info is None:
                    await _answer_callback(callback, "⚠️ Действие устарело.")
                    return
                await enter_group_reminder_card(
                    callback.message, actor_id, service_id,
                    input_info["reminder_id"],
                )
            elif action == "cancel_delete":
                if pending.get("step") != "group_reminder_delete":
                    await _answer_callback(callback, "⚠️ Подтверждение устарело.")
                    return
                await enter_group_reminder_card(
                    callback.message, actor_id, service_id, reminder_id
                )
            else:
                await _answer_callback(callback, "⚠️ Неизвестное действие.")
                return
        except (PermissionError, ValueError) as exc:
            if action == "preview" and callback_answered:
                return
            await _answer_callback(callback, _status_text(str(exc), "⚠️"))
            return
        except sqlite3.Error:
            debug.exception("Ошибка управления групповым напоминанием")
            if action == "preview":
                if callback_answered:
                    return
                back_button = InlineKeyboardMarkup(
                    inline_keyboard=[[
                        InlineKeyboardButton(
                            text="⬅️ К напоминаниям",
                            callback_data=(
                                f"group_reminder:list:{service_id}:"
                                f"{pending['session_id']}"
                            ),
                        )
                    ]]
                )
                await _send_dialog(
                    callback.message,
                    "❌ Не удалось прочитать настройки напоминания.",
                    reply_markup=back_button,
                    parse_mode=None,
                )
                await _answer_callback(callback)
                callback_answered = True
                return
            await _notify_admin(
                context.bot,
                context.admin_id,
                "Не удалось сохранить групповое напоминание из-за ошибки базы.",
                debug,
            )
            await _answer_callback(callback, "❌ Ошибка базы данных.")
            return
        except (OSError, RuntimeError):
            if action == "preview" and callback_answered:
                debug.exception(
                    "Не удалось завершить предпросмотр группового напоминания"
                )
                return
            debug.exception("Не удалось обновить задания групповых напоминаний")
            await _notify_admin(
                context.bot,
                context.admin_id,
                "Не удалось обновить задания групповых напоминаний.",
                debug,
            )
            await _answer_callback(callback, "❌ Не удалось обновить расписание.")
            return
        if not callback_answered:
            await _answer_callback(callback)

    def service_result_keyboard(
        service_id: str, delivery_check: bool = False
    ) -> InlineKeyboardMarkup:
        rows = []
        if delivery_check:
            rows.append([
                InlineKeyboardButton(
                    text="🧪 Проверка доставки",
                    callback_data=f"service:test_notify:{service_id}",
                )
            ])
        rows.extend([
            [
                InlineKeyboardButton(
                    text="⚙️ К настройкам служения",
                    callback_data=f"service:service_settings:{service_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="📋 К списку служений",
                    callback_data="service:back_to_services:list",
                )
            ],
        ])
        return InlineKeyboardMarkup(inline_keyboard=rows)

    def test_result_keyboard(service_id: str) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="🧪 Ещё одна проверка",
                        callback_data=f"service:test_notify:{service_id}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="📋 К списку служений",
                        callback_data="service:back_to_services:list",
                    )
                ],
            ]
        )

    async def notification_template_preview(
        actor_id: int, service_id: str, task: str | None = None
    ) -> str:
        pending = pending_inputs.get(actor_id)
        registry = service_registry()
        if pending is None or registry is None:
            return "Предпросмотр недоступен: настройки служения не найдены."
        service = service_by_id(registry, service_id)
        if service is None:
            return "Предпросмотр недоступен: служение не найдено."
        prepare_previews = getattr(
            context, "prepare_service_test_previews", None
        )
        if not callable(prepare_previews):
            return "Предпросмотр на предстоящее воскресенье недоступен."
        try:
            previews = await prepare_previews(
                service_id,
                group_template=pending["group_template"],
                personal_templates=pending["personal_templates"],
            )
        except (
            AttributeError,
            IndexError,
            KeyError,
            OSError,
            PermissionError,
            RuntimeError,
            sqlite3.Error,
            TypeError,
            ValueError,
        ) as exc:
            debug.exception(
                "Не удалось подготовить предпросмотр шаблона службы %s",
                service_id,
            )
            return (
                "Предпросмотр на предстоящее воскресенье недоступен "
                f"({type(exc).__name__})."
            )
        if task is None:
            current = pending["group_template"] or (
                "(не задан — используется текст по умолчанию)"
            )
            return (
                f"Текущий текст группового шаблона:\n{current}\n\n"
                f"Пример текста для ввода:\n"
                f"{previews['example_group_template']}\n\n"
                f"Так будет выглядеть сообщение на "
                f"{previews['sunday_date']:%d.%m.%Y}:\n"
                f"{previews['example_group_text']}\n\n"
                f"Результат текущего шаблона на "
                f"{previews['sunday_date']:%d.%m.%Y}:\n"
                f"{previews['group_text']}"
            )

        default_template = (
            "{name}, вы назначены на задачу «{task}» службы "
            "«{service_name}» на {sunday_date}."
        )
        current = pending["personal_templates"].get(task, default_template)
        examples = [
            item
            for item in previews["personal_messages"]
            if item["task"] == task
        ]
        example_text = "\n\n".join(
            f"{item['name']}:\n{item['text']}" for item in examples
        ) or "На предстоящее воскресенье для этой задачи нет назначенных пользователей."
        return (
            f"Текущий личный шаблон для задачи «{task}»:\n{current}\n\n"
            f"Пример на {previews['sunday_date']:%d.%m.%Y} "
            f"для текущих привязок:\n{example_text}"
        )

    async def begin_notification_edit(
        message: Message, actor_id: int, service_id: str, field: str
    ) -> None:
        pending = pending_inputs.get(actor_id)
        if (
            pending is None
            or pending.get("operation") != "service_notify"
            or pending.get("service_id") != service_id
        ):
            await _send_dialog(message, "Настройка устарела. Запустите /service_notify заново.",
                parse_mode=None,
            )
            return
        if field == "reminders":
            pending["step"] = "menu"
            await show_reminder_rules_menu(message, actor_id, service_id)
            return
        if field == "personal":
            if "schedule_assignments" not in pending:
                task_loader = getattr(
                    context, "load_service_task_assignments", None
                )
                if not callable(task_loader):
                    await _send_dialog(message, "Загрузка задач из файла расписания недоступна.",
                        parse_mode=None,
                    )
                    return
                try:
                    pending["schedule_assignments"] = await task_loader(
                        service_id
                    )
                except Exception:
                    debug.exception(
                        "Не удалось прочитать задачи для личных шаблонов"
                    )
                    await _send_dialog(message, "Не удалось прочитать файл графика. "
                        "Попробуйте выбрать этот пункт ещё раз.",
                        parse_mode=None,
                    )
                    return
                pending["schedule_tasks"] = sorted(
                    pending["schedule_assignments"]
                )
            pending["step"] = "choose_notify_task"
            await send_task_choices_page(
                message, actor_id, service_id, "service_notify"
            )
            return
        prompts = {
            "days": (
                "Введите число дней до события от 0 до 6. "
                f"Текущее значение: {pending['reminder_days']}."
            ),
            "time": (
                "Введите время в формате HH:MM. "
                f"Текущее значение: {pending['reminder_time']}."
            ),
            "group_template": (
                "Введите новый текст для группы.\n\n"
                + template_fields_help(GROUP_FIELDS)
                + "\n\nПустое значение не меняет текущий текст."
            ),
        }
        if field not in prompts:
            await _send_dialog(message, "Неизвестный параметр.", parse_mode=None)
            return
        pending["step"] = field
        text = prompts[field]
        if field == "group_template":
            registry = service_registry()
            service = service_by_id(registry, service_id) if registry else None
            current_template = service["group_template"] if service else None
            if current_template:
                try:
                    validate_notification_template(
                        current_template,
                        GENERAL_GROUP_TEMPLATE_FIELDS,
                        "Шаблон группового уведомления",
                    )
                except ValueError:
                    text += (
                        "\n\n⚠️ В сохранённом шаблоне есть неподдерживаемые "
                        "поля (например, {tg_responsible}). Они не подставляются. "
                        "Замените их на {task_assignments} или "
                        "{informational_notes}."
                    )
            text += "\n\n" + await notification_template_preview(
                actor_id, service_id
            )
        chunks = _telegram_text_chunks(text)
        for index, chunk in enumerate(chunks):
            await _send_dialog(message, chunk,
                reply_markup=(
                    notification_edit_controls(
                        service_id, pending["session_id"]
                    )
                    if index == len(chunks) - 1
                    else None
                ),
                parse_mode=None,
            )

    def service_chat_keyboard(
        service_id: str, actor_id: int | None = None
    ) -> InlineKeyboardMarkup:
        rows = [
            [
                InlineKeyboardButton(
                    text="📡 Выбрать или изменить групповой чат",
                    callback_data=f"service:service_chat_set:{service_id}",
                )
            ],
        ]
        # Проверка доставки — здесь: она про назначенный групповой чат.
        if actor_id is not None and can_check_delivery(actor_id, service_id):
            rows.append([
                InlineKeyboardButton(
                    text="🧪 Проверка доставки",
                    callback_data=f"service:test_notify:{service_id}",
                )
            ])
        rows += [
            [
                InlineKeyboardButton(
                    text="⬅️ К настройкам",
                    callback_data=f"service:service_setup:{service_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="⬅️ К карточке служения",
                    callback_data=f"service:service_settings:{service_id}",
                )
            ],
        ]
        return InlineKeyboardMarkup(inline_keyboard=rows)

    async def begin_notify_template(
        message: Message, actor_id: int, service_id: str, task: str
    ) -> None:
        pending = pending_inputs.get(actor_id)
        if pending is None or pending.get("operation") != "service_notify":
            await _send_dialog(message, "Запрос настройки устарел. Запустите /service_notify заново.",
                parse_mode=None,
            )
            return
        pending["active_task"] = task
        pending["step"] = "notify_template"
        is_informational = task_is_informational(
            pending.get("schedule_assignments", {}).get(task, [])
        )
        display_task = (
            f"ℹ️ {task} · инфо" if is_informational else task
        )
        text = (
            f"Введите индивидуальный текст напоминания для задачи "
            f"«{display_task}». Шаблоны: {{name}}, {{task}}, {{service_name}}, "
            f"{{sunday_date}}, {{telegram_id}}, {{username}}, {{event_notes}}.\n\n"
            + await notification_template_preview(
                actor_id, service_id, task
            )
        )
        chunks = _telegram_text_chunks(text)
        for index, chunk in enumerate(chunks):
            await _send_dialog(message, chunk,
                reply_markup=(
                    notification_edit_controls(
                        service_id, pending["session_id"]
                    )
                    if index == len(chunks) - 1
                    else None
                ),
                parse_mode=None,
            )

    async def save_notification_setup(
        message: Message, actor_id: int
    ) -> None:
        pending = pending_inputs.get(actor_id)
        registry = service_registry()
        if (
            pending is None
            or pending.get("operation") != "service_notify"
            or registry is None
        ):
            await _send_dialog(message, "Пошаговая настройка уведомлений устарела.",
                parse_mode=None,
            )
            return
        service = service_by_id(registry, pending["service_id"])
        if service is None:
            await _send_dialog(message, "Служение больше не существует.", parse_mode=None)
            return
        try:
            registry.set_service_default_template(
                actor_id,
                pending["service_id"],
                pending["group_template"],
            )
            registry.set_service_personal_templates(
                actor_id,
                pending["service_id"],
                pending["personal_templates"],
            )
            # Только если шаблон меняли в этой настройке: иначе снимок
            # сессии затёр бы правку другого админа.
            if pending.get("admin_report_template_edited"):
                registry.set_admin_report_template(
                    actor_id, pending["service_id"],
                    pending.get("admin_report_template"),
                )
            refresh_reminders = getattr(
                context, "refresh_service_reminder_jobs", None
            )
            if callable(refresh_reminders):
                await refresh_reminders()
        except (PermissionError, ValueError) as exc:
            await _send_dialog(message, _status_text(str(exc), "❌"), parse_mode=None)
            return
        except sqlite3.Error:
            debug.exception("Не удалось сохранить настройки уведомлений")
            await _send_dialog(message, "Не удалось сохранить настройки уведомлений из-за ошибки базы.",
                parse_mode=None,
            )
            return
        except (OSError, RuntimeError):
            debug.exception("Не удалось обновить задания уведомлений")
            await _send_dialog(
                message,
                "Не удалось обновить расписание уведомлений. Попробуйте позже.",
                parse_mode=None,
            )
            return
        pending_inputs.pop(actor_id, None)
        await clean_previous_menu_by_id(
            context.bot,
            pending.get("menu_chat_id"),
            pending.get("menu_message_id"),
            debug,
        )
        await _send_dialog(
            message,
            f"✅ Настройки уведомлений служения "
            f"«{service_label(service)}» "
            "сохранены.",
            reply_markup=service_result_keyboard(pending["service_id"]),
            parse_mode=None,
        )

    @admin_router.callback_query(F.data.startswith("task:"))
    async def on_task_choice(callback: CallbackQuery) -> None:
        if (
            not callback.from_user
            or not callback.message
            or callback.message.chat.type != ChatType.PRIVATE
        ):
            await _answer_callback(callback,)
            return
        parts = callback.data.split(":")
        if len(parts) != 3:
            await _answer_callback(callback,"❌ Некорректный выбор задачи.")
            return
        _, operation, token = parts
        selection = pending_task_choices.pop(token, None)
        if (
            selection is None
            or selection[0] != callback.from_user.id
            or selection[1] != operation
        ):
            await _answer_callback(callback,"⚠️ Выбор устарел. Начните команду заново.")
            return
        _, _, service_id, task = selection
        pending = pending_inputs.get(callback.from_user.id)
        if (
            pending is None
            or pending.get("operation") != "service_contact"
            or pending.get("service_id") != service_id
            or pending.get("step") != "choose_task"
            or task is None
            or task not in pending.get("schedule_tasks", [])
        ):
            await _answer_callback(callback,"⚠️ Запрос задачи устарел.")
            return
        await offer_contact_name_choices(
            callback.message, callback.from_user.id, service_id, task
        )
        await _answer_callback(callback,)

    @admin_router.callback_query(F.data.startswith("notify_task:"))
    async def on_notify_task_choice(callback: CallbackQuery) -> None:
        if (
            not callback.from_user
            or not callback.message
            or callback.message.chat.type != ChatType.PRIVATE
        ):
            await _answer_callback(callback,)
            return
        token = callback.data.partition(":")[2]
        selection = pending_notify_task_choices.pop(token, None)
        pending = pending_inputs.get(callback.from_user.id)
        if (
            selection is None
            or selection[0] != callback.from_user.id
            or pending is None
            or pending.get("operation") != "service_notify"
            or pending.get("service_id") != selection[1]
            or pending.get("step") != "choose_notify_task"
            or (
                selection[2] is not None
                and selection[2] not in pending.get("schedule_tasks", [])
            )
        ):
            await _answer_callback(callback,"⚠️ Выбор устарел. Начните настройку заново.")
            return
        task = selection[2]
        if task is None:
            await show_notification_settings_menu(
                callback.message, callback.from_user.id, selection[1]
            )
        else:
            await begin_notify_template(
                callback.message, callback.from_user.id, selection[1], task
            )
        await _answer_callback(callback,)

    @admin_router.callback_query(F.data.startswith("notify_task_new:"))
    async def on_notify_new_task(callback: CallbackQuery) -> None:
        await _answer_callback(callback,
            "Добавление произвольных задач отключено. "
            "Используйте задачи из файла расписания."
        )

    async def offer_user_selection(
        message: Message, actor_id: int, service_id: str
    ) -> None:
        pending = pending_inputs.setdefault(actor_id, {})
        pending.update(
            {
                "operation": "service_admin_add",
                "service_id": service_id,
                "step": "await_user",
            }
        )
        request_id = new_share_request_id()
        pending_shares[request_id] = (
            actor_id, "service_admin_add", service_id, None, None
        )
        keyboard = ReplyKeyboardMarkup(
            keyboard=[
                [
                    KeyboardButton(
                        text="Выбрать пользователя",
                        request_users=KeyboardButtonRequestUsers(
                            request_id=request_id,
                            user_is_bot=False,
                            max_quantity=1,
                            request_name=True,
                            request_username=True,
                        ),
                    )
                ]
            ],
            resize_keyboard=True,
            one_time_keyboard=True,
        )
        await _send_dialog(message, "Нажмите кнопку и выберите пользователя в Telegram. "
            "Либо отправьте его Telegram ID, при желании дополнив "
            "username через пробел: 123456789 @username. "
            "Администратор также может назначить самого себя. "
            "Назначение подтвердите на следующем шаге.",
            reply_markup=keyboard,
            parse_mode=None,
        )

    async def offer_contact_user_selection(
        message: Message,
        actor_id: int,
        service_id: str,
        task: str,
        contact_name: str,
    ) -> None:
        pending = pending_inputs.get(actor_id)
        if (
            pending is None
            or pending.get("operation") != "service_contact"
            or pending.get("service_id") != service_id
            or pending.get("active_task") != task
        ):
            await _send_dialog(message, "Сценарий привязки устарел. Запустите /service_contact заново.",
                parse_mode=None,
            )
            return
        pending["step"] = "await_user"
        pending["active_contact_name"] = contact_name
        for request_id, share in list(pending_shares.items()):
            if share[0] == actor_id and share[1] == "service_contact":
                pending_shares.pop(request_id, None)
        request_id = new_share_request_id()
        pending_shares[request_id] = (
            actor_id, "service_contact", service_id, task, contact_name
        )
        keyboard = ReplyKeyboardMarkup(
            keyboard=[
                [
                    KeyboardButton(
                        text="Выбрать ответственного",
                        request_users=KeyboardButtonRequestUsers(
                            request_id=request_id,
                            user_is_bot=False,
                            max_quantity=1,
                            request_name=True,
                            request_username=True,
                        ),
                    )
                ],
                [KeyboardButton(text=CONTACT_BACK_TEXT)],
            ],
            resize_keyboard=True,
            one_time_keyboard=True,
        )
        scope = (
            f"(задача «{task}»)" if task else "(все его задачи служения)"
        )
        await _send_dialog(message, f"Нажмите кнопку и выберите пользователя Telegram для имени "
            f"«{contact_name}» {scope}. "
            "Или отправьте Telegram ID, при желании дополнив username "
            "через пробел: 123456789 @username, — или только @username: "
            "привязка завершится, когда человек напишет боту. "
            f"«{CONTACT_BACK_TEXT}» — вернуться к выбору человека. "
            "Проверьте выбор — назначение подтвердите на следующем шаге.",
            reply_markup=keyboard,
            parse_mode=None,
        )

    def parse_manual_user(
        text: str, username_only: bool = False,
    ) -> tuple[int | None, str]:
        """«ID [@username]»; username_only — можно и один «@username»
        (ID тогда None: привязка завершится, когда человек напишет боту)."""
        parts = text.split()
        if len(parts) not in {1, 2}:
            raise ValueError(
                "Введите Telegram ID и при необходимости username "
                "через пробел, например: 123456789 @username."
            )
        if username_only and len(parts) == 1 and parts[0].startswith("@"):
            username = parts[0][1:]
            if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{4,31}", username):
                raise ValueError("Некорректный Telegram username.")
            return None, username
        try:
            user_id = int(parts[0])
        except ValueError as exc:
            raise ValueError(
                "Telegram ID должен быть положительным числом."
            ) from exc
        if user_id <= 0:
            raise ValueError("Telegram ID должен быть положительным числом.")
        username = ""
        if len(parts) == 2:
            username = parts[1]
            if username.startswith("@"):
                username = username[1:]
        if username and not re.fullmatch(
            r"[A-Za-z][A-Za-z0-9_]{4,31}", username
        ):
            raise ValueError("Некорректный Telegram username.")
        return user_id, username

    async def ask_user_assignment_confirmation(
        message: Message,
        actor_id: int,
        service_id: str,
        operation: str,
        user_id: int,
        username: str,
        task: str | None = None,
        contact_name: str | None = None,
        display_name: str | None = None,
    ) -> None:
        if operation == "service_admin_add":
            pending = pending_inputs.setdefault(actor_id, {})
            pending.update(
                {
                    "operation": operation,
                    "service_id": service_id,
                    "step": "confirm",
                }
            )
        key = (actor_id, service_id, user_id)
        confirmation_id = secrets.token_urlsafe(8)
        pending_admin_confirmations[key] = (
            operation,
            task,
            contact_name,
            display_name,
            username,
            confirmation_id,
        )
        callback_prefix = (
            "service_admin" if operation == "service_admin_add"
            else "service_contact"
        )
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="✅ Подтвердить",
                        callback_data=(
                            f"{callback_prefix}_confirm:{service_id}:"
                            f"{user_id}:{confirmation_id}"
                        ),
                    ),
                    InlineKeyboardButton(
                        text="✖️ Отмена",
                        callback_data=(
                            f"{callback_prefix}_cancel:{service_id}:"
                            f"{user_id}:{confirmation_id}"
                        ),
                    ),
                ]
            ]
        )
        title = (
            "назначить администратором служения"
            if operation == "service_admin_add"
            else (
                f"привязать к записи «{contact_name}» из файла "
                f"для задачи «{task}» и других задач этого имени"
            )
            if task
            else f"привязать «{contact_name}» во всех задачах служения"
        )
        user_display = display_name or f"Пользователь {user_id}"
        await _send_dialog(message, f"ℹ️ Подтвердите: {title}\n"
            f"👤 Пользователь · {user_display}"
            + (f" · @{username}" if username else "")
            + (
                f"\n🆔 Telegram ID · {ui_code(user_id)}"
                if display_name is None
                else ""
            ),
            reply_markup=keyboard,
            parse_mode=None,
        )

    def contact_member_tasks(actor_id: int, contact_name: str) -> list[str]:
        """Задачи имени в графике (для привязки во всех задачах человека)."""
        return sorted(
            schedule_task
            for schedule_task, task_info in (
                pending_inputs.get(actor_id) or {}
            ).get("schedule_assignments", {}).items()
            if not task_is_informational(task_info)
            and contact_name in task_assignment_names(task_info)
        )

    async def ask_username_binding_confirmation(
        message: Message, actor_id: int, service_id: str, username: str,
        task: str | None, contact_name: str,
    ) -> None:
        """Привязка только по @username: ID бот узнает, когда человек
        напишет ему, — до этого привязка ждёт."""
        token = secrets.token_urlsafe(6)
        pending_inputs.setdefault(actor_id, {})["username_binding"] = {
            "token": token, "service_id": service_id, "username": username,
            "task": task or "", "name": contact_name,
        }
        scope = f"задача «{task}»" if task else "все его задачи служения"
        await _send_dialog(
            message,
            f"ℹ️ Подтвердите: привязать «{contact_name}» ({scope}) к "
            f"@{username}\n\n"
            "⏳ Бот ещё не видел этого пользователя, поэтому Telegram ID "
            "пока неизвестен. Привязка завершится, когда он(а) напишет "
            "боту, нажмёт кнопку или ответит на опрос, — вам придёт "
            "сообщение. Попросите человека написать боту /start.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(
                    text="✅ Подтвердить",
                    callback_data=f"ubind:ok:{service_id}:{token}",
                ),
                InlineKeyboardButton(
                    text="✖️ Отмена",
                    callback_data=f"ubind:no:{service_id}:{token}",
                ),
            ]]),
            parse_mode=None,
        )

    @admin_router.callback_query(F.data.startswith("ubind:"))
    async def on_username_binding(callback: CallbackQuery) -> None:
        if (
            not callback.from_user
            or not callback.message
            or callback.message.chat.type != ChatType.PRIVATE
        ):
            await _answer_callback(callback)
            return
        actor_id = callback.from_user.id
        parts = callback.data.split(":")
        pending = pending_inputs.get(actor_id) or {}
        binding = pending.get("username_binding")
        if (
            len(parts) != 4 or binding is None
            or binding["service_id"] != parts[2]
            or binding["token"] != parts[3]
        ):
            await _answer_callback(
                callback, "⚠️ Запрос устарел. Начните привязку заново."
            )
            return
        pending.pop("username_binding", None)
        service_id, task, name = (
            binding["service_id"], binding["task"], binding["name"]
        )
        notice = "✅ Назначение отменено."
        if parts[1] == "ok":
            registry = service_registry()
            try:
                if registry is None:
                    raise ValueError("Реестр настроек служений недоступен.")
                registry.add_username_binding(
                    actor_id, service_id, name, task, binding["username"],
                    () if task else contact_member_tasks(actor_id, name),
                )
            except (PermissionError, ValueError) as exc:
                notice = _status_text(str(exc), "❌")
            except sqlite3.Error:
                debug.exception("Не удалось сохранить привязку по username")
                notice = "❌ Не удалось сохранить привязку из-за ошибки базы данных."
            else:
                username_waiting["names"] = None
                notice = (
                    f"⏳ «{name}» привяжется к @{binding['username']}, когда "
                    "он(а) напишет боту — вам придёт сообщение."
                )
        if task:
            contact_pending = pending_inputs.get(actor_id)
            if contact_pending is not None:
                contact_pending["step"] = "choose_contact_person"
                contact_pending.pop("active_contact_name", None)
            await offer_contact_name_choices(
                callback.message, actor_id, service_id, task, notice=notice,
            )
        else:
            await send_member_choices(
                callback.message, actor_id, service_id,
                pending.get("member_page", 0), notice=notice,
            )
        await _answer_callback(callback)

    @admin_router.message(Command("services"))
    async def on_services(message: Message) -> None:
        if (
            not message.from_user
            or message.chat.type != ChatType.PRIVATE
        ):
            return
        if service_registry() is None:
            await _send_dialog(message, "Реестр настроек служений недоступен. "
                "Попробуйте позже или сообщите супер-администратору.",
                parse_mode=None,
            )
            return

        registry = service_registry()
        try:
            is_super_admin = registry.is_super_admin(message.from_user.id)
            services = registry.list_services()
            visible = [
                service for service in services
                if is_super_admin
                or registry.is_service_admin(
                    message.from_user.id, service["id"]
                )
            ]
            if not visible:
                await _send_dialog(message, "ℹ️ Вы не назначены администратором служений.",
                    parse_mode=None,
                )
                return
            keyboard = service_selection_keyboard(
                visible, "service_settings"
            )
        except sqlite3.Error:
            debug.exception("Не удалось прочитать список служений")
            await _send_dialog(message, "Не удалось прочитать реестр служений.", parse_mode=None
            )
            return
        await _send_dialog(
            message,
            "📋 Служения\nℹ️ Выберите служение для просмотра настроек.",
            reply_markup=keyboard,
            _dialog_keyboard_limit=8,
        )

    @admin_router.message(Command("service_settings"))
    async def on_service_settings(message: Message) -> None:
        if not message.from_user or message.chat.type != ChatType.PRIVATE:
            return
        await clean_and_clear_pending_actor(message.from_user.id)
        parts = (message.text or "").split()
        if len(parts) == 1:
            await request_service_input(message, "service_settings")
            return
        await request_service_input(
            message, "service_settings", parts[1]
        )

    @admin_router.message(Command("service_admin_add"))
    async def on_service_admin_add(message: Message) -> None:
        if not message.from_user or message.chat.type != ChatType.PRIVATE:
            return
        await clean_and_clear_pending_actor(message.from_user.id)
        parts = (message.text or "").split()
        if len(parts) == 1:
            await request_service_input(message, "service_admin_add")
            return
        if len(parts) == 2:
            await request_service_input(
                message, "service_admin_add", parts[1]
            )
            return
        if len(parts) not in {3, 4}:
            await _send_dialog(message, "Укажите ID пользователя и необязательный username.",
                parse_mode=None,
            )
            return
        service_id = parts[1]
        try:
            user_id, username = parse_manual_user(" ".join(parts[2:]))
            registry = service_registry()
            if registry is None:
                raise ValueError("Реестр настроек служений недоступен.")
            if not registry.is_super_admin(message.from_user.id):
                raise PermissionError(
                    "Эта команда доступна только супер-администратору."
                )
            service = service_by_id(registry, service_id)
            if service is None:
                raise ValueError("Служение не найдено.")
        except (PermissionError, ValueError) as exc:
            await _send_dialog(message, _status_text(str(exc), "❌"), parse_mode=None)
            return
        except sqlite3.Error:
            debug.exception("Не удалось проверить прямое назначение администратора")
            await _send_dialog(message, "Не удалось проверить служение и права из-за ошибки базы данных.",
                parse_mode=None,
            )
            return
        await ask_user_assignment_confirmation(
            message,
            message.from_user.id,
            service_id,
            "service_admin_add",
            user_id,
            username,
        )

    @admin_router.message(Command("service_admin_remove"))
    async def on_service_admin_remove(message: Message) -> None:
        if not message.from_user or message.chat.type != ChatType.PRIVATE:
            return
        await clean_and_clear_pending_actor(message.from_user.id)
        parts = (message.text or "").split()
        if len(parts) == 1:
            await request_service_input(message, "service_admin_remove")
            return
        if len(parts) == 2:
            await request_service_input(
                message, "service_admin_remove", parts[1]
            )
            return
        if len(parts) != 3:
            await _send_dialog(message, "Лишние параметры. Запустите команду ещё раз.", parse_mode=None)
            return
        service_id, raw_telegram_id = parts[1:]

        def remove_admin(actor_id: int) -> str:
            telegram_id = int(raw_telegram_id)
            registry = service_registry()
            registry.remove_service_admin(actor_id, service_id, telegram_id)
            return "Администратор служения удалён (если он был назначен)."

        await run_registry_command(message, remove_admin)
        if raw_telegram_id.isdecimal():
            await refresh_user_commands(int(raw_telegram_id))

    @admin_router.message(Command("service_chat"))
    async def on_service_chat(message: Message) -> None:
        if not message.from_user or message.chat.type != ChatType.PRIVATE:
            return
        await clean_and_clear_pending_actor(message.from_user.id)
        parts = (message.text or "").split()
        if len(parts) == 1:
            await request_service_input(message, "service_chat")
            return
        if len(parts) == 2:
            if parts[1] == "off":
                await _send_dialog(message, "Выберите служение в команде /service_chat и нажмите "
                    "«Отключить служение».",
                    parse_mode=None,
                )
                return
            await request_service_input(message, "service_chat", parts[1])
            return
        if len(parts) == 3 and parts[2] == "off":
            service_id = parts[1]
            try:
                await ask_service_disable(message, message.from_user.id, service_id)
            except (PermissionError, ValueError) as exc:
                await _send_dialog(message, f"❌ {exc}", parse_mode=None)
            return
        if len(parts) != 4:
            await _send_dialog(message, "Используйте /service_chat, чтобы выбрать служение и "
                "действие кнопками.",
                parse_mode=None,
            )
            return
        service_id, raw_chat_id, raw_enabled = parts[1:]
        try:
            # Сначала права и формат — запрос в Telegram только админу
            # служения.
            if not is_service_admin(message.from_user.id, service_id):
                raise PermissionError(
                    "Настройки доступны только администратору этого служения."
                )
            if raw_enabled not in {"on", "off"}:
                raise ValueError("Последний параметр должен быть on или off.")
            try:
                chat_id = int(raw_chat_id)
            except ValueError as exc:
                raise ValueError("ID чата должен быть числом.") from exc
            await require_chat_member(message.from_user.id, chat_id)
            if raw_enabled == "off":
                await ask_service_disable(
                    message, message.from_user.id, service_id, group_chat_id=chat_id,
                )
                return
        except (PermissionError, ValueError) as exc:
            await _send_dialog(
                message, _status_text(str(exc), "❌"), parse_mode=None
            )
            return

        def update_chat(actor_id: int) -> str:
            if raw_enabled not in {"on", "off"}:
                raise ValueError("Последний параметр должен быть on или off.")
            chat_id = int(raw_chat_id)
            registry = service_registry()
            registry.set_group_chat(
                actor_id,
                service_id,
                chat_id,
                enabled=raw_enabled == "on",
            )
            if service_id == "assembly":
                context.group_chat_id = chat_id
            return "Групповой чат и статус служения сохранены."

        await run_registry_command(message, update_chat)

    @admin_router.message(Command("service_contact"))
    async def on_service_contact(message: Message) -> None:
        if not message.from_user or message.chat.type != ChatType.PRIVATE:
            return
        await clean_and_clear_pending_actor(message.from_user.id)
        payload = (message.text or "").partition(" ")[2].strip()
        parts = payload.split("|")
        prefix_parts = parts[0].strip().split(maxsplit=1) if parts else []
        details = [field.strip() for field in parts[1:]]
        if not payload:
            await request_service_input(message, "service_contact")
            return
        if len(prefix_parts) == 1 and not details:
            await request_service_input(
                message, "service_contact", prefix_parts[0]
            )
            return
        if len(prefix_parts) != 1 or details:
            await _send_dialog(message, "Ручной ввод задач и ответственных отключён. "
                "Укажите только служение, например /service_contact sound; "
                "задачу и имя нужно выбрать из файла расписания.",
                parse_mode=None,
            )
            return

    @admin_router.message(Command("service_notify"))
    async def on_service_notify(message: Message) -> None:
        if not message.from_user or message.chat.type != ChatType.PRIVATE:
            return
        await invalidate_personal_session(message.from_user.id)
        payload = (message.text or "").partition(" ")[2]
        fields = [field.strip() for field in payload.split("|", 2)]
        if not payload.strip():
            await request_service_input(message, "service_notify")
            return
        if len(fields) == 1:
            service_parts = fields[0].split()
            if len(service_parts) != 1:
                await _send_dialog(message, "ℹ️ Настройки вводятся пошагово; индивидуальные задачи "
                    "выбираются из файла расписания. Используйте "
                    "/service_notify <service_id>.",
                    parse_mode=None,
                )
                return
            await request_service_input(
                message, "service_notify", service_parts[0]
            )
            return
        await _send_dialog(message, "ℹ️ Настройки вводятся пошагово; индивидуальные задачи "
            "выбираются из файла расписания. Используйте /service_notify "
            "<service_id>.",
            parse_mode=None,
        )

    @admin_router.message(Command("cancel"))
    async def on_cancel_service_command(message: Message) -> None:
        if (
            not message.from_user
            or message.chat.type != ChatType.PRIVATE
        ):
            return
        previous = pending_inputs.get(message.from_user.id)
        if (
            previous is not None
            and previous.get("operation") == "service_contact"
            and previous.get("step") in {"await_user", "await_member_name"}
            and (
                previous.get("contact_mode") == "person"
                or previous.get("active_task")
            )
        ):
            # В привязке /cancel отменяет только текущий шаг.
            await return_to_contact_choices(
                message, message.from_user.id, "↩️ Шаг отменён."
            )
            return
        had_request = previous is not None
        if previous is not None:
            await clean_and_clear_pending_actor(message.from_user.id)
        for request_id, pending in list(pending_shares.items()):
            if pending[0] == message.from_user.id:
                pending_shares.pop(request_id, None)
                had_request = True
        for key in list(pending_admin_confirmations):
            if key[0] == message.from_user.id:
                pending_admin_confirmations.pop(key, None)
                had_request = True
        for key in list(pending_task_choices):
            if pending_task_choices[key][0] == message.from_user.id:
                pending_task_choices.pop(key, None)
                had_request = True
        for key in list(pending_task_pages):
            if pending_task_pages[key][0] == message.from_user.id:
                pending_task_pages.pop(key, None)
                had_request = True
        for key in list(pending_contact_person_choices):
            if pending_contact_person_choices[key][0] == message.from_user.id:
                pending_contact_person_choices.pop(key, None)
                had_request = True
        for key in list(pending_contact_navigation):
            if pending_contact_navigation[key][0] == message.from_user.id:
                pending_contact_navigation.pop(key, None)
                had_request = True
        for key in list(pending_notify_task_choices):
            if pending_notify_task_choices[key][0] == message.from_user.id:
                pending_notify_task_choices.pop(key, None)
                had_request = True
        if not had_request:
            await _send_dialog(message, "ℹ️ Нет активного запроса параметров.", parse_mode=None
            )
            return
        await _send_dialog(message, "✅ Текущий запрос отменён.",
            parse_mode=None,
            reply_markup=ReplyKeyboardRemove(),
        )

    start_menu_items = {
        "service_settings": "📋 Служения",
        "service_notify": "🔔 Напоминания групп",
        "service_contact": "👥 Ответственные",
        "service_admins": "👤 Администраторы",
        "service_admin_add": "👤 Назначить админа",
        "service_admin_remove": "➖ Снять админа",
        "my_schedule": "📅 Мои служения",
        "my_month": "📆 На месяц",
        "my_notifications": "⏰ Мои уведомления",
        "start": "🏠 Главное меню",
    }

    def start_menu_operations(
        actor_id: int, role: str, linked: bool
    ) -> list[str]:
        operations = []
        # Сверка с Excel, групповой чат и проверка доставки — в карточке
        # служения: их ведут админы служений.
        if role != ROLE_USER:
            operations.extend([
                "service_settings", "service_notify", "service_contact",
            ])
        if role == ROLE_SUPER_ADMIN:
            operations.append("service_admins")
        if linked:
            operations.extend(["my_schedule", "my_month", "my_notifications"])
        return operations

    def start_menu_keyboard(
        operations: list[str],
    ) -> InlineKeyboardMarkup | None:
        if not operations:
            return None
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=start_menu_items[operation],
                        callback_data=f"menu:{operation}",
                    )
                ]
                for operation in operations
            ]
        )

    async def open_service_admins_menu(
        message: Message, actor_id: int
    ) -> None:
        registry = service_registry()
        if registry is None or not registry.is_super_admin(actor_id):
            await _send_dialog(
                message,
                "❌ Назначать и снимать администраторов может только "
                "супер-администратор.",
                parse_mode=None,
            )
            return
        await _send_dialog(
            message,
            "👤 Администраторы\nℹ️ Назначьте или снимите администратора "
            "служения.",
            reply_markup=start_menu_keyboard(
                ["service_admin_add", "service_admin_remove", "start"]
            ),
            parse_mode=None,
        )

    def start_text(
        actor_id: int, user_name: str, role: str, linked: bool
    ) -> str:
        if role == ROLE_SUPER_ADMIN:
            return (
                f"👋 Здравствуйте, {user_name}!\n"
                "Вы — супер-администратор: вам доступны все служения.\n\n"
                "ℹ️ Выберите раздел кнопкой ниже или командой из меню.\n"
                "💡 Новое служение настраивается в его карточке "
                "(«📋 Служения»): групповой чат и проверка доставки → "
                "ответственные → напоминания групп."
            )
        if role == ROLE_SERVICE_ADMIN:
            registry = service_registry()
            names = [
                service_label(service)
                for service in registry.list_services()
                if registry.is_service_admin(actor_id, service["id"])
            ]
            return (
                f"👋 Здравствуйте, {user_name}!\n"
                f"Вы — администратор служений: {', '.join(names)}.\n\n"
                "ℹ️ Выберите раздел кнопкой ниже или командой из меню."
            )
        if linked:
            return (
                f"👋 Здравствуйте, {user_name}!\n"
                "Вы привязаны к расписанию служений. Бот поможет "
                "не пропускать назначения.\n\n"
                "📅 «Мои служения» — ваши назначения на ближайшие недели.\n"
                "⏰ «Мои уведомления» — личные напоминания, по умолчанию "
                "выключены."
            )
        return (
            f"👋 Здравствуйте, {user_name}!\n"
            "Вы пока не привязаны ни к одной задаче в расписании служений.\n\n"
            "ℹ️ Перешлите это сообщение администратору вашего служения — "
            "ему понадобится ваш Telegram ID:\n"
            f"🆔 {ui_code(actor_id)}\n\n"
            "После привязки здесь появятся настройки личных напоминаний."
        )

    def notifications_hint(actor_id: int) -> str:
        """Подсказка на /start: человек может не знать, что личные
        напоминания нужно включить."""
        registry = service_registry()
        try:
            services = registry.get_user_notifications_state(actor_id)[
                "per_service"
            ]
        except (sqlite3.Error, AttributeError):
            debug.exception("Не удалось прочитать личные уведомления для /start")
            services = []
        disabled = [
            service_label(service_by_id(registry, item["service_id"]) or {
                "id": item["service_id"], "name": item["service_id"],
            })
            for item in services if not item["enabled"]
        ]
        if not disabled:
            return (
                "🔔 Личные напоминания включены. Изменить: "
                "«⏰ Мои уведомления» или /my_notifications."
            )
        return (
            f"🔕 Личные напоминания выключены: {', '.join(disabled)}.\n"
            "Включите их, чтобы бот напоминал о ваших задачах: "
            "«⏰ Мои уведомления» ниже или команда /my_notifications."
        )

    @admin_router.message(Command("start"))
    async def on_start(message: Message) -> None:
        if not message.from_user or message.chat.type != ChatType.PRIVATE:
            return
        await show_start(message, message.from_user)

    async def show_start(message: Message, user: Any) -> None:
        actor_id = user.id
        await clean_and_clear_pending_actor(actor_id)
        user_name = getattr(user, "first_name", None) or "друг"
        try:
            role = user_role(service_registry(), actor_id)
        except sqlite3.Error:
            debug.exception("Ошибка при проверке прав в /start")
            role = ROLE_USER
        linked = is_user_linked(actor_id)
        try:
            text = start_text(actor_id, user_name, role, linked)
        except sqlite3.Error:
            debug.exception("Ошибка при чтении служений в /start")
            role = ROLE_USER
            text = start_text(actor_id, user_name, role, linked)
        if linked:
            text += "\n\n" + notifications_hint(actor_id)
        operations = start_menu_operations(actor_id, role, linked)
        # После перезапуска бота нижняя клавиатура могла остаться у
        # клиента: /start убирает её, даже если бот о ней не знает.
        nav_state.setdefault(actor_id, {"shown": True, "helpers": []})
        await _send_dialog(
            message,
            text,
            reply_markup=start_menu_keyboard(operations),
            _dialog_keyboard_limit=max(6, len(operations)),
        )
        # Меню без кнопок (_send_dialog скрывает навигацию только у экранов
        # с кнопками) — убрать её отдельно.
        await hide_nav_keyboard(message.chat.id, actor_id)
        await refresh_user_commands(actor_id)

    @admin_router.callback_query(F.data.startswith("menu:"))
    async def on_start_menu(callback: CallbackQuery) -> None:
        if (
            not callback.from_user
            or not callback.message
            or callback.message.chat.type != ChatType.PRIVATE
        ):
            await _answer_callback(callback)
            return
        operation = callback.data.partition(":")[2]
        if operation not in start_menu_items:
            await _answer_callback(callback, "⚠️ Меню устарело. Откройте /start.")
            return
        actor_id = callback.from_user.id
        await _answer_callback(callback)
        await clean_and_clear_pending_actor(actor_id)
        await clean_previous_menu(callback.message, debug)
        if operation == "my_notifications":
            await open_my_notifications(callback.message, actor_id)
        elif operation == "my_schedule":
            await open_my_schedule(callback.message, actor_id)
        elif operation == "my_month":
            await open_my_month(callback.message, actor_id)
        elif operation == "start":
            await show_start(callback.message, callback.from_user)
        elif operation == "service_admins":
            await open_service_admins_menu(callback.message, actor_id)
        else:
            await request_service_input(
                callback.message, operation, actor_id=actor_id
            )

    @admin_router.callback_query(F.data.startswith("service:"))
    async def on_service_selected(callback: CallbackQuery) -> None:
        if (
            not callback.from_user
            or not callback.message
            or callback.message.chat.type != ChatType.PRIVATE
        ):
            await _answer_callback(callback,)
            return
        callback_parts = callback.data.split(":", 2)
        if len(callback_parts) != 3:
            await _answer_callback(callback,"❌ Некорректный выбор служения.")
            return
        _, operation, service_id = callback_parts
        if callback.from_user.id in pending_inputs:
            await clean_and_clear_pending_actor(callback.from_user.id)
        else:
            await clean_previous_menu(callback.message, debug)
        if operation == "back_to_services":
            registry = service_registry()
            if registry is None:
                await _send_dialog(callback.message, "Реестр настроек служений недоступен.",
                    parse_mode=None,
                )
            else:
                try:
                    is_super_admin = registry.is_super_admin(
                        callback.from_user.id
                    )
                    services = registry.list_services()
                    services = [
                        service for service in services
                        if is_super_admin
                        or registry.is_service_admin(
                            callback.from_user.id, service["id"]
                        )
                    ]
                    keyboard = service_selection_keyboard(
                        services, "service_settings"
                    )
                    await _send_dialog(callback.message, "📋 Служения\nℹ️ Выберите служение для просмотра настроек.",
                        reply_markup=keyboard,
                        parse_mode=None,
                        _dialog_keyboard_limit=8,
                    )
                except sqlite3.Error:
                    debug.exception(
                        "Не удалось загрузить список служений для возврата"
                    )
                    await _send_dialog(callback.message, "Не удалось прочитать реестр служений.",
                        parse_mode=None,
                    )
            await _answer_callback(callback,)
            return
        if operation not in {
            "service_settings",
            "service_setup",
            "service_excel",
            "service_help",
            "edit_notify",
            "service_admin_add",
            "service_admin_remove",
            "service_chat",
            "service_chat_set",
            "service_chat_disable",
            "service_contact",
            "service_sync",
            "service_notify",
            "service_reminders",
            "service_reminder_summary",
            "service_replace",
            "test_notify",
        }:
            await _answer_callback(callback,"❌ Неизвестное действие.")
            return
        registry = service_registry()
        if registry is None:
            await _send_dialog(callback.message, "Реестр настроек служений недоступен. Попробуйте позже.",
                parse_mode=None,
            )
            await _answer_callback(callback,)
            return
        try:
            service = service_by_id(registry, service_id)
            if service is None:
                raise ValueError("Служение не найдено.")
            if operation == "service_settings":
                await send_service_card(
                    callback.message, callback.from_user.id, service
                )
            elif operation == "service_reminders":
                if not is_service_admin(callback.from_user.id, service_id):
                    raise PermissionError(
                        "Настройки доступны только администратору этого служения."
                    )
                await show_service_reminder_choices(
                    callback.message, callback.from_user.id, service_id
                )
            elif operation == "service_reminder_summary":
                if not is_service_admin(callback.from_user.id, service_id):
                    raise PermissionError(
                        "Настройки доступны только администратору этого служения."
                    )
                pending_inputs[callback.from_user.id] = {
                    "operation": "personal_summary", "service_id": service_id,
                }
                await show_personal_reminder_summary(
                    callback.message,
                    callback.from_user.id,
                    service_id,
                    f"service:service_reminders:{service_id}",
                )
            elif operation == "service_setup":
                if not is_service_admin(callback.from_user.id, service_id):
                    raise PermissionError(
                        "Настройки доступны только администратору этого служения."
                    )
                await show_service_setup(
                    callback.message, callback.from_user.id, service,
                )
            elif operation == "service_excel":
                if not is_service_admin(callback.from_user.id, service_id):
                    raise PermissionError(
                        "Настройки доступны только администратору этого служения."
                    )
                await show_excel_schedule(
                    callback.message, callback.from_user.id, service
                )
            elif operation == "service_help":
                await _send_dialog(
                    callback.message,
                    SERVICE_HELP,
                    _dialog_text_limit=TELEGRAM_TEXT_LIMIT,
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                        InlineKeyboardButton(
                            text="⬅️ К карточке служения",
                            callback_data=f"service:service_settings:{service_id}",
                        )
                    ]]),
                    parse_mode=None,
                )
            elif operation == "service_replace":
                if not is_service_admin(callback.from_user.id, service_id):
                    raise PermissionError(
                        "Замена доступна только администратору этого служения."
                    )
                await open_replace_dates(
                    callback.message, callback.from_user.id, service_id
                )
            elif operation in {"edit_notify", "service_notify"}:
                if not is_service_admin(
                    callback.from_user.id, service_id
                ):
                    raise PermissionError(
                        "Настройки доступны только администратору этого служения."
                    )
                await ask_for_service_parameters(
                    callback.message,
                    callback.from_user.id,
                    "service_notify",
                    service_id,
                    service_label(service),
                )
            elif operation == "service_chat":
                if not is_service_admin(callback.from_user.id, service_id):
                    raise PermissionError(
                        "Настройки доступны только администратору этого служения."
                    )
                await _send_dialog(callback.message, f"{service_label(service)}: выберите действие.",
                    reply_markup=service_chat_keyboard(
                        service_id, callback.from_user.id
                    ),
                    parse_mode=None,
                )
            elif operation == "service_chat_set":
                if not is_service_admin(callback.from_user.id, service_id):
                    raise PermissionError(
                        "Настройки доступны только администратору этого служения."
                    )
                await ask_for_service_parameters(
                    callback.message,
                    callback.from_user.id,
                    "service_chat",
                    service_id,
                    service_label(service),
                )
            elif operation == "service_chat_disable":
                await ask_service_disable(
                    callback.message, callback.from_user.id, service_id,
                )
            elif operation == "test_notify":
                if not is_service_admin(callback.from_user.id, service_id):
                    raise PermissionError(
                        "Проверка доставки доступна только администратору "
                        "этого служения."
                    )
                rows = []
                if service["group_chat_id"] is not None:
                    rows.append([
                        InlineKeyboardButton(
                            text="Проверить отправку в групповой чат",
                            callback_data=(
                                f"test_action:group:{service_id}"
                            ),
                        )
                    ])
                rows.append([
                    InlineKeyboardButton(
                        text="Показать тексты админу",
                        callback_data=f"test_action:preview:{service_id}",
                    )
                ])
                rows.append([
                    InlineKeyboardButton(
                        text="⬅️ К групповому чату",
                        callback_data=f"service:service_chat:{service_id}",
                    )
                ])
                await _send_dialog(callback.message, f"{service_label(service)}: выберите проверку.",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
                    parse_mode=None,
                )
            elif operation == "service_sync":
                if not is_service_admin(callback.from_user.id, service_id):
                    raise PermissionError(
                        "Настройки доступны только администратору этого служения."
                    )
                await ask_for_service_parameters(
                    callback.message,
                    callback.from_user.id,
                    "service_sync",
                    service_id,
                    service_label(service),
                )
            else:
                if operation in ADMIN_OPERATIONS:
                    if not registry.is_super_admin(callback.from_user.id):
                        raise PermissionError(
                            "Назначать и снимать администраторов может только "
                            "супер-администратор."
                        )
                elif not is_service_admin(callback.from_user.id, service_id):
                    raise PermissionError(
                        "Настройки доступны только администратору этого служения."
                    )
                if operation == "service_admin_add":
                    await offer_user_selection(
                        callback.message,
                        callback.from_user.id,
                        service_id,
                    )
                else:
                    await ask_for_service_parameters(
                        callback.message,
                        callback.from_user.id,
                        operation,
                        service_id,
                        service_label(service),
                    )
        except (PermissionError, ValueError) as exc:
            await _send_dialog(callback.message, _status_text(str(exc), "❌"), parse_mode=None
            )
        except sqlite3.Error:
            debug.exception("Не удалось обработать выбор служения")
            await _send_dialog(callback.message, "Не удалось прочитать реестр служений.", parse_mode=None
            )
        await _answer_callback(callback,)

    @admin_router.callback_query(F.data.startswith("notify_rule:"))
    async def on_notification_rule_action(callback: CallbackQuery) -> None:
        if (
            not callback.from_user
            or not callback.message
            or callback.message.chat.type != ChatType.PRIVATE
        ):
            await _answer_callback(callback,)
            return
        parts = callback.data.split(":")
        if len(parts) not in {4, 5}:
            await _answer_callback(callback,"⚠️ Настройка устарела.")
            return
        _, action, service_id, session_id, *raw_id = parts
        pending = pending_inputs.get(callback.from_user.id)
        if (
            pending is None
            or pending.get("operation") != "service_notify"
            or pending.get("service_id") != service_id
            or pending.get("session_id") != session_id
        ):
            await _answer_callback(callback,"⚠️ Настройка устарела.")
            return
        step = pending.get("step")
        allowed_steps = {
            "root_back": {"reminder_menu"},
            "back": {"reminder_options", "reminder_delete"},
            "add": {"reminder_menu"},
            "edit": {"reminder_menu"},
            "days": {"reminder_options"},
            "time": {"reminder_options"},
            "delete_prompt": {"reminder_menu"},
            "delete": {"reminder_delete"},
        }
        if step not in allowed_steps.get(action, set()):
            await _answer_callback(callback, "⚠️ Этот экран уже устарел.")
            return
        try:
            if not is_service_admin(callback.from_user.id, service_id):
                await _answer_callback(callback,"❌ Недостаточно прав.")
                return
        except sqlite3.Error:
            debug.exception("Не удалось проверить права на расписание уведомлений")
            await _answer_callback(callback,"❌ Не удалось проверить права.")
            return

        reminder_id = None
        if raw_id:
            try:
                reminder_id = int(raw_id[0])
            except ValueError:
                await _answer_callback(callback,"❌ Некорректный идентификатор.")
                return
        reminder = next(
            (
                item for item in pending["reminders"]
                if item.get("id") == reminder_id
                or item.get("draft_id") == reminder_id
            ),
            None,
        )
        if action == "root_back":
            await show_notification_settings_menu(
                callback.message, callback.from_user.id, service_id
            )
        elif action == "back":
            await show_reminder_rules_menu(
                callback.message, callback.from_user.id, service_id
            )
        elif action == "add":
            if any(
                item.get("id") is None and not item.get("complete")
                for item in pending["reminders"]
            ):
                await _answer_callback(callback,
                    "Сначала завершите настройку нового уведомления."
                )
                return
            pending["reminders"].append(
                {
                    "id": None,
                    "draft_id": min(
                        (
                            item["draft_id"]
                            for item in pending["reminders"]
                            if item.get("id") is None
                        ),
                        default=0,
                    ) - 1,
                    "reminder_days": 1,
                    "reminder_time": "18:00",
                    "complete": False,
                }
            )
            pending["active_reminder_id"] = pending["reminders"][-1][
                "draft_id"
            ]
            pending["reminder_operation"] = "add"
            pending["step"] = "reminder_days"
            await _send_dialog(callback.message, "Введите число дней до события для нового уведомления "
                "(от 0 до 6).",
                reply_markup=notification_edit_controls(
                    service_id, session_id
                ),
                parse_mode=None,
            )
        elif action == "edit" and reminder is not None:
            pending["step"] = "reminder_options"
            buttons = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=f"📅 Дни: {reminder['reminder_days']}",
                            callback_data=(
                                f"notify_rule:days:{service_id}:"
                                f"{session_id}:{reminder_id}"
                            ),
                        ),
                        InlineKeyboardButton(
                            text=f"🕒 Время: {reminder['reminder_time']}",
                            callback_data=(
                                f"notify_rule:time:{service_id}:"
                                f"{session_id}:{reminder_id}"
                            ),
                        ),
                    ],
                    [
                        InlineKeyboardButton(
                            text="⬅️ К списку",
                            callback_data=(
                                f"notify_rule:back:{service_id}:{session_id}"
                            ),
                        )
                    ],
                ]
            )
            await _send_dialog(callback.message, "Выберите параметр уведомления для изменения.",
                reply_markup=buttons,
                parse_mode=None,
            )
        elif action in {"days", "time"} and reminder is not None:
            pending["active_reminder_id"] = reminder_id
            pending["reminder_operation"] = "edit"
            pending["active_reminder_original"] = {
                "reminder_days": reminder["reminder_days"],
                "reminder_time": reminder["reminder_time"],
            }
            pending["step"] = f"reminder_{action}"
            prompt = (
                f"Введите число дней до события от 0 до 6. "
                f"Текущее значение: {reminder['reminder_days']}."
                if action == "days"
                else "Введите время в формате HH:MM, например 09:00. "
                f"Текущее значение: {reminder['reminder_time']}."
            )
            await _send_dialog(callback.message, prompt,
                reply_markup=notification_edit_controls(
                    service_id, session_id
                ),
                parse_mode=None,
            )
        elif action == "delete_prompt" and reminder is not None:
            pending["step"] = "reminder_delete"
            buttons = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="🗑 Да, удалить",
                            callback_data=(
                                f"notify_rule:delete:{service_id}:"
                                f"{session_id}:{reminder_id}"
                            ),
                        ),
                        InlineKeyboardButton(
                            text="Отмена",
                            callback_data=(
                                f"notify_rule:back:{service_id}:{session_id}"
                            ),
                        ),
                    ]
                ]
            )
            await _send_dialog(callback.message, f"Удалить уведомление за {reminder['reminder_days']} дн. "
                f"в {reminder['reminder_time']}?",
                reply_markup=buttons,
                parse_mode=None,
            )
        elif action == "delete" and reminder is not None:
            pending["reminders"].remove(reminder)
            pending["step"] = "menu"
            await show_reminder_rules_menu(
                callback.message, callback.from_user.id, service_id
            )
        else:
            await _answer_callback(callback,"⚠️ Настройка устарела.")
            return
        await _answer_callback(callback,)

    @admin_router.callback_query(F.data.startswith("notify_back:"))
    async def on_notification_edit_back(callback: CallbackQuery) -> None:
        if (
            not callback.from_user
            or not callback.message
            or callback.message.chat.type != ChatType.PRIVATE
        ):
            await _answer_callback(callback)
            return
        parts = callback.data.split(":", 2)
        if len(parts) != 3:
            await _answer_callback(callback, "⚠️ Настройка устарела.")
            return
        _, service_id, session_id = parts
        actor_id = callback.from_user.id
        pending = pending_inputs.get(actor_id)
        if (
            pending is None
            or pending.get("operation") != "service_notify"
            or pending.get("service_id") != service_id
            or pending.get("session_id") != session_id
        ):
            await _answer_callback(callback, "⚠️ Настройка устарела.")
            return
        try:
            if not is_service_admin(actor_id, service_id):
                await _answer_callback(callback, "❌ Недостаточно прав.")
                return
        except sqlite3.Error:
            debug.exception("Не удалось проверить доступ к настройкам")
            await _answer_callback(callback, "❌ Не удалось проверить права.")
            return

        step = pending.get("step")
        if step in GROUP_REMINDER_NEW_STEPS:
            pending.pop("group_reminder_draft", None)
            await show_reminder_rules_menu(
                callback.message, actor_id, service_id
            )
        elif step in {
            "group_reminder_days",
            "group_reminder_time",
            "group_reminder_text",
            "group_reminder_period_input",
        }:
            input_info = pending.get("group_reminder_input")
            if input_info is None:
                await _answer_callback(callback, "⚠️ Нет активного шага для отмены.")
                return
            pending.pop("group_reminder_input", None)
            await enter_group_reminder_card(
                callback.message,
                actor_id,
                service_id,
                input_info["reminder_id"],
            )
        elif step == "group_default_text":
            pending["step"] = "default_template"
            await enter_default_template(
                callback.message, actor_id, service_id
            )
        elif step == "admin_report_text":
            await enter_admin_report_template(
                callback.message, actor_id, service_id
            )
        elif step in {"reminder_days", "reminder_time"}:
            active_id = pending.get("active_reminder_id")
            reminder = next(
                (
                    item for item in pending["reminders"]
                    if item.get("id") == active_id
                    or item.get("draft_id") == active_id
                ),
                None,
            )
            if pending.get("reminder_operation") == "add":
                if reminder is not None:
                    pending["reminders"].remove(reminder)
            elif reminder is not None:
                original = pending.get("active_reminder_original", {})
                reminder.update(original)
            pending.pop("active_reminder_original", None)
            pending.pop("active_reminder_id", None)
            pending.pop("reminder_operation", None)
            pending["step"] = "menu"
            await show_reminder_rules_menu(callback.message, actor_id, service_id)
        elif step == "notify_template":
            pending.pop("active_task", None)
            pending["step"] = "choose_notify_task"
            await send_task_choices_page(
                callback.message, actor_id, service_id, "service_notify"
            )
        elif step in {"days", "time", "group_template"}:
            pending["step"] = "menu"
            await show_notification_settings_menu(
                callback.message, actor_id, service_id
            )
        else:
            await _answer_callback(callback, "⚠️ Нет активного шага для отмены.")
            return
        await _answer_callback(callback, "Изменения шага отменены")

    @admin_router.callback_query(F.data.startswith("notify_edit:"))
    async def on_notification_setting_edit(callback: CallbackQuery) -> None:
        if (
            not callback.from_user
            or not callback.message
            or callback.message.chat.type != ChatType.PRIVATE
        ):
            await _answer_callback(callback,)
            return
        parts = callback.data.split(":", 3)
        if len(parts) != 4:
            await _answer_callback(callback,"⚠️ Настройка устарела. Запустите её заново.")
            return
        _, field, service_id, session_id = parts
        registry = service_registry()
        if registry is None:
            await _answer_callback(callback,"❌ Реестр настроек недоступен.")
            return
        try:
            has_access = is_service_admin(
                callback.from_user.id, service_id
            )
        except sqlite3.Error:
            debug.exception(
                "Не удалось проверить права на изменение настроек служения"
            )
            await _answer_callback(callback,"❌ Не удалось проверить права.")
            return
        if not has_access:
            await _answer_callback(callback,"❌ Недостаточно прав.")
            return
        pending = pending_inputs.get(callback.from_user.id)
        if (
            pending is None
            or pending.get("operation") != "service_notify"
            or pending.get("service_id") != service_id
            or pending.get("session_id") != session_id
            or pending.get("step") != "menu"
        ):
            await _answer_callback(callback,"⚠️ Настройка устарела. Запустите её заново.")
            return
        await begin_notification_edit(
            callback.message, callback.from_user.id, service_id, field
        )
        await _answer_callback(callback,)

    @admin_router.callback_query(F.data.startswith("notify_finish:"))
    async def on_notification_setting_finish(callback: CallbackQuery) -> None:
        if (
            not callback.from_user
            or not callback.message
            or callback.message.chat.type != ChatType.PRIVATE
        ):
            await _answer_callback(callback,)
            return
        parts = callback.data.split(":", 2)
        if len(parts) != 3:
            await _answer_callback(callback,"⚠️ Настройка устарела. Запустите её заново.")
            return
        _, service_id, session_id = parts
        pending = pending_inputs.get(callback.from_user.id)
        try:
            has_access = is_service_admin(
                callback.from_user.id, service_id
            )
        except sqlite3.Error:
            debug.exception(
                "Не удалось проверить права на сохранение настроек служения"
            )
            await _answer_callback(callback,"❌ Не удалось проверить права.")
            return
        if not has_access:
            await _answer_callback(callback,"❌ Недостаточно прав.")
            return
        if (
            pending is None
            or pending.get("operation") != "service_notify"
            or pending.get("service_id") != service_id
            or pending.get("session_id") != session_id
            or pending.get("step") != "menu"
        ):
            await _answer_callback(callback,"⚠️ Настройка устарела. Запустите её заново.")
            return
        await save_notification_setup(
            callback.message, callback.from_user.id
        )
        await _answer_callback(callback,)

    @admin_router.callback_query(F.data.startswith("notify_cancel:"))
    async def on_notification_setting_cancel(callback: CallbackQuery) -> None:
        if not callback.from_user or not callback.message:
            await _answer_callback(callback,)
            return
        parts = callback.data.split(":", 2)
        if len(parts) != 3:
            await _answer_callback(callback,"⚠️ Настройка устарела.")
            return
        _, service_id, session_id = parts
        pending = pending_inputs.get(callback.from_user.id)
        if (
            callback.message.chat.type != ChatType.PRIVATE
            or pending is None
            or pending.get("operation") != "service_notify"
            or pending.get("service_id") != service_id
            or pending.get("session_id") != session_id
            or pending.get("step") != "menu"
        ):
            await _answer_callback(callback,"⚠️ Настройка устарела.")
            return
        try:
            if not is_service_admin(callback.from_user.id, service_id):
                await _answer_callback(callback, "❌ Недостаточно прав.")
                return
            service = service_by_id(service_registry(), service_id)
            if service is None:
                raise ValueError("Служение не найдено.")
            await clean_and_clear_pending_actor(callback.from_user.id)
            await send_service_card(
                callback.message, callback.from_user.id, service
            )
        except (PermissionError, ValueError) as exc:
            await _answer_callback(callback, f"❌ {exc}")
            return
        except sqlite3.Error:
            debug.exception("Не удалось открыть карточку после закрытия настройки")
            await _answer_callback(callback, "❌ Не удалось открыть служение.")
            return
        await _answer_callback(callback,)

    @admin_router.callback_query(
        F.data.startswith("service_admin_confirm:")
        | F.data.startswith("service_admin_cancel:")
        | F.data.startswith("service_contact_confirm:")
        | F.data.startswith("service_contact_cancel:")
    )
    async def on_shared_admin_confirmation(callback: CallbackQuery) -> None:
        if (
            not callback.from_user
            or not callback.message
            or callback.message.chat.type != ChatType.PRIVATE
        ):
            await _answer_callback(callback,)
            return
        parts = callback.data.split(":")
        if len(parts) != 4:
            await _answer_callback(callback,"❌ Некорректное подтверждение.")
            return
        action, service_id, raw_user_id, confirmation_id = parts
        action_type = action.rsplit("_", 1)[1]
        try:
            user_id = int(raw_user_id)
        except ValueError:
            await _answer_callback(callback,"❌ Некорректный Telegram ID.")
            return
        key = (callback.from_user.id, service_id, user_id)
        selected_user = pending_admin_confirmations.get(key)
        expected_prefix = (
            "service_admin"
            if selected_user and selected_user[0] == "service_admin_add"
            else "service_contact"
        )
        if (
            selected_user is None
            or selected_user[0] not in {"service_admin_add", "service_contact"}
            or selected_user[5] != confirmation_id
            or action.rsplit("_", 1)[0] != expected_prefix
        ):
            await _answer_callback(callback,"⚠️ Запрос устарел. Начните команду заново.")
            return
        pending_admin_confirmations.pop(key, None)
        (
            operation,
            task,
            contact_name,
            selected_display_name,
            username,
            _,
        ) = selected_user
        if action_type == "cancel":
            if operation == "service_admin_add":
                await _send_dialog(
                    callback.message,
                    "Изменение отменено. Выберите пользователя заново.",
                    parse_mode=None,
                )
                await offer_user_selection(
                    callback.message,
                    callback.from_user.id,
                    service_id,
                )
            elif not task:
                await send_member_choices(
                    callback.message,
                    callback.from_user.id,
                    service_id,
                    (pending_inputs.get(callback.from_user.id) or {}).get(
                        "member_page", 0
                    ),
                    notice="✅ Назначение отменено.",
                )
            else:
                contact_pending = pending_inputs.get(callback.from_user.id)
                if contact_pending is not None:
                    contact_pending["step"] = "choose_contact_person"
                    contact_pending.pop("active_contact_name", None)
                await offer_contact_name_choices(
                    callback.message,
                    callback.from_user.id,
                    service_id,
                    task or "",
                    notice="✅ Назначение отменено.",
                )
            await _answer_callback(callback,)
            return
        registry = service_registry()
        if registry is None:
            if operation == "service_admin_add":
                await _send_dialog(callback.message, "Реестр настроек служений недоступен.",
                    parse_mode=None,
                )
            elif not task:
                await _send_dialog(
                    callback.message,
                    "Реестр настроек служений недоступен.",
                    parse_mode=None,
                )
            else:
                contact_pending = pending_inputs.get(callback.from_user.id)
                if contact_pending is not None:
                    contact_pending["step"] = "choose_contact_person"
                    contact_pending.pop("active_contact_name", None)
                await offer_contact_name_choices(
                    callback.message,
                    callback.from_user.id,
                    service_id,
                    task or "",
                    notice="Реестр настроек служений недоступен.",
                )
            await _answer_callback(callback,)
            return
        response = ""
        assignment_saved = False
        try:
            if operation == "service_admin_add":
                registry.add_service_admin(
                    callback.from_user.id, service_id, user_id, username or ""
                )
            elif not task:
                member_tasks = sorted(
                    schedule_task
                    for schedule_task, task_info in (
                        pending_inputs.get(callback.from_user.id) or {}
                    ).get("schedule_assignments", {}).items()
                    if not task_is_informational(task_info)
                    and contact_name in task_assignment_names(task_info)
                )
                registry.bind_service_member(
                    callback.from_user.id,
                    service_id,
                    contact_name or "",
                    user_id,
                    username or "",
                    tasks=member_tasks,
                )
            else:
                registry.set_responsible_contact(
                    callback.from_user.id,
                    service_id,
                    task or "",
                    contact_name or f"Пользователь {user_id}",
                    user_id,
                    username,
                )
        except (PermissionError, ValueError) as exc:
            response = str(exc)
        except sqlite3.Error:
            debug.exception("Не удалось сохранить выбор пользователя")
            response = (
                "Не удалось сохранить выбор из-за ошибки базы данных."
            )
        else:
            assignment_saved = True
            display_name = selected_display_name or f"Пользователь {user_id}"
            if operation == "service_admin_add":
                await refresh_user_commands(user_id)
                response = (
                    f"{display_name}"
                    + (f" (@{username})" if username else "")
                    + " назначен администратором служения."
                )
            elif not task:
                response = (
                    f"«{contact_name}» связан с {display_name}"
                    + (f" (@{username})" if username else "")
                    + " во всех задачах служения."
                )
            else:
                response = (
                    f"Ответственный «{contact_name}» из файла расписания "
                    f"связан с {display_name}"
                    + (f" (@{username})" if username else "")
                    + f" для задачи «{task}» и других задач этого имени."
                )
        if operation == "service_admin_add":
            await clean_and_clear_pending_actor(callback.from_user.id)
            await _send_dialog(callback.message, _status_text(
                    response, "✅" if assignment_saved else "❌"
                ),
                reply_markup=(
                    service_result_keyboard(service_id)
                    if assignment_saved
                    else None
                ),
                parse_mode=None,
            )
        elif not task:
            contact_pending = pending_inputs.get(callback.from_user.id)
            if contact_pending is not None and assignment_saved:
                try:
                    contacts = registry.list_responsible_contacts(
                        callback.from_user.id, service_id
                    )
                except (PermissionError, ValueError, sqlite3.Error):
                    debug.exception("Не удалось обновить список привязок")
                else:
                    contact_pending["registered_contacts"] = {}
                    for contact in contacts:
                        contact_pending["registered_contacts"].setdefault(
                            contact["task"], []
                        ).append(contact)
            await send_member_choices(
                callback.message,
                callback.from_user.id,
                service_id,
                (contact_pending or {}).get("member_page", 0),
                notice=_status_text(
                    response, "✅" if assignment_saved else "❌"
                ),
            )
        else:
            contact_pending = pending_inputs.get(callback.from_user.id)
            if contact_pending is not None:
                if assignment_saved:
                    registered = contact_pending.setdefault(
                        "registered_contacts", {}
                    ).setdefault(task or "", [])
                    registered[:] = [
                        item for item in registered
                        if (
                            item["name"] != contact_name
                            and item["telegram_id"] != user_id
                        )
                    ]
                    registered.append(
                        {
                            "task": task,
                            "name": contact_name,
                            "telegram_id": user_id,
                            "username": username or "",
                        }
                    )
                contact_pending["step"] = "choose_contact_person"
                contact_pending.pop("active_contact_name", None)
            if task:
                await offer_contact_name_choices(
                    callback.message,
                    callback.from_user.id,
                    service_id,
                    task,
                    notice=response,
                )
        await _answer_callback(callback,)

    @admin_router.callback_query(F.data.startswith("task_page:"))
    async def on_task_page(callback: CallbackQuery) -> None:
        if (
            not callback.from_user
            or not callback.message
            or callback.message.chat.type != ChatType.PRIVATE
        ):
            await _answer_callback(callback,)
            return
        token = callback.data.partition(":")[2]
        selection = pending_task_pages.pop(token, None)
        if selection is None or selection[0] != callback.from_user.id:
            await _answer_callback(callback,"⚠️ Список устарел. Запустите команду заново.")
            return
        _, operation, service_id, page = selection
        pending = pending_inputs.get(callback.from_user.id)
        expected_step = {
            "service_contact": "choose_task",
            "service_notify": "choose_notify_task",
        }.get(operation)
        if (
            expected_step is None
            or pending is None
            or pending.get("operation") != operation
            or pending.get("service_id") != service_id
            or pending.get("step") != expected_step
        ):
            await _answer_callback(callback,"⚠️ Список устарел. Запустите команду заново.")
            return
        await send_task_choices_page(
            callback.message,
            callback.from_user.id,
            service_id,
            operation,
            page,
        )
        await _answer_callback(callback,)

    @admin_router.callback_query(F.data.startswith("contact_person:"))
    async def on_contact_person_choice(callback: CallbackQuery) -> None:
        if (
            not callback.from_user
            or not callback.message
            or callback.message.chat.type != ChatType.PRIVATE
        ):
            await _answer_callback(callback,)
            return
        token = callback.data.partition(":")[2]
        selection = pending_contact_person_choices.pop(token, None)
        pending = pending_inputs.get(callback.from_user.id)
        member_mode = selection is not None and selection[2] is None
        if (
            selection is None
            or selection[0] != callback.from_user.id
            or pending is None
            or pending.get("operation") != "service_contact"
            or pending.get("service_id") != selection[1]
            or (
                member_mode
                and (
                    pending.get("step") != "choose_member"
                    or selection[3] not in pending.get("member_names", [])
                )
            )
            or (
                not member_mode
                and (
                    pending.get("step") != "choose_contact_person"
                    or pending.get("active_task") != selection[2]
                    or selection[3]
                    not in task_assignment_names(
                        pending.get("schedule_assignments", {}).get(
                            selection[2], []
                        )
                    )
                )
            )
        ):
            await _answer_callback(callback,"⚠️ Выбор устарел. Начните команду заново.")
            return
        try:
            if member_mode:
                # Человек из «Состава» — карточка: Telegram, задачи, удаление.
                await show_member_card(
                    callback.message, callback.from_user.id, selection[1],
                    selection[3],
                )
            else:
                await offer_contact_user_selection(
                    callback.message,
                    callback.from_user.id,
                    selection[1],
                    selection[2],
                    selection[3],
                )
        except ValueError as exc:
            await _send_dialog(callback.message, _status_text(str(exc), "❌"), parse_mode=None
            )
        except sqlite3.Error:
            debug.exception(
                "Не удалось создать запрос выбора ответственного"
            )
            await _send_dialog(callback.message, "Не удалось открыть выбор пользователя из-за ошибки базы.",
                parse_mode=None,
            )
        await _answer_callback(callback,)

    @admin_router.callback_query(F.data.startswith("contact_nav:"))
    async def on_contact_navigation(callback: CallbackQuery) -> None:
        if (
            not callback.from_user
            or not callback.message
            or callback.message.chat.type != ChatType.PRIVATE
        ):
            await _answer_callback(callback,)
            return
        token = callback.data.partition(":")[2]
        selection = pending_contact_navigation.pop(token, None)
        pending = pending_inputs.get(callback.from_user.id)
        if (
            selection is None
            or selection[0] != callback.from_user.id
            or pending is None
            or pending.get("operation") != "service_contact"
            or pending.get("service_id") != selection[1]
            or pending.get("step")
            not in CONTACT_NAVIGATION_STEPS[selection[2].partition(":")[0]]
        ):
            await _answer_callback(callback,"⚠️ Выбор устарел. Начните команду заново.")
            return
        _, service_id, action = selection
        if action.startswith("overview:"):
            await send_contact_overview(
                callback.message,
                callback.from_user.id,
                service_id,
                int(action.partition(":")[2]),
            )
        elif action == "edit":
            await send_member_choices(
                callback.message, callback.from_user.id, service_id
            )
        elif action.startswith("members:"):
            await send_member_choices(
                callback.message,
                callback.from_user.id,
                service_id,
                int(action.partition(":")[2]),
            )
        elif action == "add_member":
            pending["step"] = "await_member_name"
            await _send_dialog(
                callback.message,
                "➕ Новый участник\n"
                "ℹ️ Отправьте имя так, как оно записано в графике, например "
                "«Иван Петров». Затем выберите его Telegram.",
                reply_markup=ReplyKeyboardMarkup(
                    keyboard=[[KeyboardButton(text=CONTACT_BACK_TEXT)]],
                    resize_keyboard=True,
                    one_time_keyboard=True,
                ),
                parse_mode=None,
            )
        elif action in {"notify", "card"}:
            await clean_and_clear_pending_actor(callback.from_user.id)
            registry = service_registry()
            service = service_by_id(registry, service_id) if registry else None
            if service is None:
                await _send_dialog(
                    callback.message, "Служение не найдено.", parse_mode=None
                )
            elif action == "card":
                await send_service_card(
                    callback.message, callback.from_user.id, service
                )
            else:
                await ask_for_service_parameters(
                    callback.message,
                    callback.from_user.id,
                    "service_notify",
                    service_id,
                    service_label(service),
                )
        elif action == "tasks":
            pending["step"] = "choose_task"
            pending.pop("active_task", None)
            pending.pop("active_contact_name", None)
            await send_task_choices_page(
                callback.message,
                callback.from_user.id,
                service_id,
                "service_contact",
            )
        elif action == "services":
            await clean_and_clear_pending_actor(callback.from_user.id)
            await request_service_input(
                callback.message,
                "service_contact",
                actor_id=callback.from_user.id,
            )
        await _answer_callback(callback,)

    @admin_router.message(F.users_shared)
    async def on_users_shared(message: Message) -> None:
        if (
            not message.from_user
            or message.chat.type != ChatType.PRIVATE
            or not message.users_shared
        ):
            return
        request_id = message.users_shared.request_id
        pending = pending_shares.pop(request_id, None)
        if (
            pending is not None
            and pending[0] == message.from_user.id
            and pending[1] == "service_replace"
            and len(message.users_shared.users) == 1
        ):
            await on_replace_user_shared(message, pending)
            return
        if (
            pending is not None
            and pending[0] == message.from_user.id
            and pending[1] == "plan_user"
            and len(message.users_shared.users) == 1
        ):
            await on_plan_user_shared(message, pending)
            return
        if (
            pending is None
            or pending[0] != message.from_user.id
            or pending[1] not in {"service_admin_add", "service_contact"}
        ):
            await _send_dialog(message, "Выбор пользователя не соответствует активному запросу. "
                "Запустите команду назначения заново.",
                parse_mode=None,
                reply_markup=ReplyKeyboardRemove(),
            )
            return
        if len(message.users_shared.users) != 1:
            await _send_dialog(message, "Нужно выбрать ровно одного пользователя.",
                parse_mode=None,
                reply_markup=ReplyKeyboardRemove(),
            )
            return
        selected_user = message.users_shared.users[0]
        user_id = selected_user.user_id
        operation = pending[1]
        task = pending[3]
        contact_name = pending[4]
        active = pending_inputs.get(message.from_user.id)
        if (
            active is None
            or active.get("operation") != operation
            or active.get("service_id") != pending[2]
            or active.get("step") != "await_user"
            or (
                operation == "service_contact"
                and (
                    active.get("active_task") != task
                    or active.get("active_contact_name") != contact_name
                )
            )
        ):
            await _send_dialog(message, "Выбор пользователя устарел. Выберите задачу и имя заново.",
                parse_mode=None,
                reply_markup=ReplyKeyboardRemove(),
            )
            return
        if operation == "service_contact" and (
            not contact_name
            or (not task and active.get("contact_mode") != "person")
        ):
            await _send_dialog(message, "Не удалось определить задачу. Запустите привязку заново.",
                parse_mode=None,
                reply_markup=ReplyKeyboardRemove(),
            )
            return
        selected_display_name = " ".join(
            part.strip()
            for part in (
                selected_user.first_name or "",
                getattr(selected_user, "last_name", "") or "",
            )
            if part and part.strip()
        )
        await ask_user_assignment_confirmation(
            message,
            message.from_user.id,
            pending[2],
            operation,
            user_id,
            selected_user.username or "",
            task,
            contact_name,
            selected_display_name,
        )

    @admin_router.message(F.chat_shared)
    async def on_chat_shared(message: Message) -> None:
        if (
            not message.from_user
            or message.chat.type != ChatType.PRIVATE
            or not message.chat_shared
        ):
            return
        pending = pending_shares.pop(message.chat_shared.request_id, None)
        if (
            pending is None
            or pending[0] != message.from_user.id
            or pending[1] != "service_chat"
        ):
            await _send_dialog(message, "Выбор чата не соответствует активному запросу. "
                "Запустите /service_chat заново.",
                parse_mode=None,
                reply_markup=ReplyKeyboardRemove(),
            )
            return
        registry = service_registry()
        if registry is None:
            await _send_dialog(message, "Реестр настроек служений недоступен.",
                parse_mode=None,
                reply_markup=ReplyKeyboardRemove(),
            )
            return
        shared_chat = message.chat_shared
        try:
            registry.set_group_chat(
                message.from_user.id,
                pending[2],
                shared_chat.chat_id,
                enabled=True,
            )
        except (PermissionError, ValueError) as exc:
            await _send_dialog(message, _status_text(str(exc), "❌"), parse_mode=None)
            return
        except sqlite3.Error:
            debug.exception("Не удалось сохранить выбранный групповой чат")
            await _send_dialog(message, "Не удалось сохранить групповой чат из-за ошибки базы данных.",
                parse_mode=None,
            )
            return
        if pending[2] == "assembly":
            context.group_chat_id = shared_chat.chat_id
        await clean_and_clear_pending_actor(message.from_user.id)
        title = shared_chat.title or "Групповой чат"
        await _send_dialog(
            message,
            f"✅ Чат сохранён · {title}\n"
            f"📡 ID чата · {ui_code(shared_chat.chat_id)}\n"
            "🟢 Служение включено",
            parse_mode=None,
            reply_markup=ReplyKeyboardRemove(),
        )
        await _send_dialog(
            message,
            "Выберите следующий шаг.",
            reply_markup=service_result_keyboard(
                pending[2], delivery_check=True
            ),
            parse_mode=None,
        )

    def personal_action_button(
        actor_id: int,
        state: dict[str, Any],
        label: str,
        action: str,
        **payload: Any,
    ) -> InlineKeyboardButton:
        token = secrets.token_urlsafe(6)
        pending_personal_actions[token] = (
            actor_id,
            state["session_id"],
            state["screen_id"],
            action,
            payload,
        )
        return InlineKeyboardButton(
            text=label, callback_data=f"pmn:{state['session_id']}:{token}"
        )

    def push_personal_level(
        state: dict[str, Any], level: dict[str, Any]
    ) -> None:
        state["levels"].append({**level, "page": 0})

    def pop_personal_level(state: dict[str, Any]) -> None:
        if len(state["levels"]) > 1:
            state["levels"].pop()

    async def clean_previous_personal_menu(
        state: dict[str, Any]
    ) -> None:
        for token, action in list(pending_personal_actions.items()):
            if action[1] == state.get("session_id"):
                pending_personal_actions.pop(token, None)
        message_id = state.get("menu_message_id")
        chat_id = state.get("menu_chat_id")
        await clean_previous_menu_by_id(
            context.bot, chat_id, message_id, debug
        )

    async def finish_personal_session(actor_id: int) -> None:
        state = pending_inputs.get(actor_id)
        if state and state.get("operation", "").startswith("my_notifications"):
            await clean_previous_personal_menu(state)
        clear_pending_actor(actor_id)

    async def invalidate_personal_session(actor_id: int) -> None:
        state = pending_inputs.get(actor_id)
        if state and state.get("operation", "").startswith("my_notifications"):
            await clean_previous_personal_menu(state)
        clear_pending_actor(actor_id)

    async def current_personal_tasks(
        registry: Any,
        actor_id: int,
        service_id: str,
        state: dict[str, Any],
    ) -> list[str]:
        task_cache = state.setdefault("task_cache", {})
        task_cache_updated = state.setdefault("task_cache_updated", {})
        now = time.monotonic()
        if (
            service_id not in task_cache
            or now - task_cache_updated.get(service_id, 0)
            >= PERSONAL_TASK_CACHE_TTL_SECONDS
        ):
            loader = getattr(context, "load_service_task_assignments", None)
            if loader is None:
                raise ValueError("Не удалось проверить актуальный график служения.")
            current_assignments = await loader(service_id)
            assigned = set(registry.list_user_tasks(actor_id, service_id))
            task_cache[service_id] = [
                task
                for task, task_info in current_assignments.items()
                if task in assigned and not task_is_informational(task_info)
            ]
            informational_tasks = sorted(
                task
                for task, task_info in current_assignments.items()
                if task_is_informational(task_info)
            )
            if informational_tasks:
                debug.debug(
                    "Информационные задачи исключены из личных уведомлений "
                    "для пользователя %s: %s",
                    actor_id,
                    informational_tasks,
                )
            task_cache_updated[service_id] = time.monotonic()
        assigned_now = set(registry.list_user_tasks(actor_id, service_id))
        return [task for task in task_cache[service_id] if task in assigned_now]

    async def ensure_default_personal_rule(
        registry: Any, actor_id: int, service_id: str, state: dict[str, Any]
    ) -> str | None:
        """Make sure enabling the service will actually send something.

        Returns the reason it cannot: an enabled rule must cover a task the
        user has in the current schedule. Current tasks without any rule get
        the default rule; disabled rules are left as the user set them.
        """
        rules_by_task = {
            entry["task"]: registry.list_schedules(entry["id"])
            for entry in registry.list_user_texts(actor_id, service_id)
        }
        try:
            tasks = await current_personal_tasks(
                registry, actor_id, service_id, state
            )
        except (OSError, RuntimeError, ValueError):
            # Без графика нельзя проверить, что сохранённые правила
            # относятся к текущим задачам, поэтому включение откладывается.
            debug.exception(
                "Не удалось загрузить задачи для правила по умолчанию"
            )
            return PERSONAL_SCHEDULE_UNAVAILABLE
        if not tasks:
            return PERSONAL_NO_TASKS
        uncovered = [task for task in tasks if not rules_by_task.get(task)]
        if uncovered:
            registry.set_user_service_schedule(
                actor_id, service_id, uncovered, *DEFAULT_PERSONAL_RULE
            )
            return None
        if any(
            rule["enabled"]
            for task in tasks
            for rule in rules_by_task[task]
        ):
            return None
        return PERSONAL_RULES_DISABLED

    def personal_rule_label(rule: dict) -> str:
        return (
            f"{'🟢' if rule['enabled'] else '⚪'} "
            f"{'✏️' if rule['origin'] == 'user' else '👤'} {rule['task'][:24]} · "
            f"{_rule_label(rule['reminder_days'], rule['reminder_time'])}"
        )

    def personal_rule(
        registry: Any, actor_id: int, service_id: str, schedule_id: int
    ) -> dict:
        """Напоминание человека в служении; чужое — как отсутствующее."""
        rule = next(
            (
                item
                for item in registry.list_user_service_schedules(
                    actor_id, service_id
                )
                if item["id"] == schedule_id
            ),
            None,
        )
        if rule is None:
            raise ValueError("Напоминание больше не существует.")
        return rule

    def shortened_user_body(body: str | None) -> str | None:
        """Прежние тексты могли быть длиннее нынешнего лимита: копия
        берёт самое длинное начало в пределах лимита, а если обрезка
        задела поле шаблона «{…}», отрезает только его."""
        if not body or len(body.encode("utf-16-le")) // 2 <= USER_REMINDER_BODY_LIMIT:
            return body
        units = 0
        for index, character in enumerate(body):
            units += len(character.encode("utf-16-le")) // 2
            if units > USER_REMINDER_BODY_LIMIT:
                body = body[:index]
                break
        while body:
            try:
                validate_notification_template(
                    body, PERSONAL_TEMPLATE_FIELDS, "Текст личного напоминания"
                )
                return body.rstrip() or None
            except ValueError:
                cut = max(body.rfind("{"), body.rfind("}"))
                body = body[:cut] if cut >= 0 else ""
        return None

    async def add_own_personal_rule(
        registry: Any, actor_id: int, service_id: str, task: str,
        days: int, reminder_time: str, state: dict[str, Any],
    ) -> dict:
        if task not in await current_personal_tasks(
            registry, actor_id, service_id, state
        ):
            raise ValueError("Вы больше не привязаны к этой задаче.")
        text_id = registry.ensure_user_text(actor_id, service_id, task)
        return registry.add_schedule(
            text_id, days, reminder_time, origin="user"
        )

    async def render_current_personal_level(
        target: Message,
        actor_id: int,
        state: dict[str, Any],
        *,
        clean_previous: bool = True,
    ) -> None:
        registry = service_registry()
        if registry is None:
            await _send_dialog(
                target,
                "Реестр настроек служений недоступен. Попробуйте позже.",
                parse_mode=None,
            )
            return
        if clean_previous:
            await clean_previous_personal_menu(state)
        state["screen_id"] = secrets.token_urlsafe(8)

        level = state["levels"][-1]
        name = level["name"]
        # items листаются по страницам; actions и navigation видны всегда.
        items: list[list[InlineKeyboardButton]] = []
        actions: list[InlineKeyboardButton] = []
        navigation: list[InlineKeyboardButton] = []
        if name == "summary":
            summary = registry.get_user_notifications_state(actor_id)
            services = summary["per_service"]
            lines = [
                "🔔 Мои личные уведомления",
                f"Включено: {summary['enabled_count']} · "
                f"выключено: {summary['disabled_count']}",
                "",
                "Выберите служение или измените все настройки:",
            ]
            for item in services:
                service = service_by_id(registry, item["service_id"])
                label = service_label(service) if service else item["service_id"]
                items.append([
                    personal_action_button(
                        actor_id,
                        state,
                        f"{'🟢' if item['enabled'] else '⚪'} {label}",
                        "open_service",
                        service_id=item["service_id"],
                    )
                ])
            month = registry.get_month_summary(actor_id)
            items.append([
                personal_action_button(
                    actor_id, state,
                    "📆 Сводка на месяц: " + (
                        f"🟢 {month_summary_label(month)}"
                        if month["enabled"] else "⚪ выключена"
                    ),
                    "open_month",
                )
            ])
            text = "\n".join(lines)
            if not services:
                text = (
                    "ℹ️ Вы пока не привязаны ни к одной задаче. "
                    "Обратитесь к администратору служения."
                )
            if services:
                actions.extend(
                    [
                        personal_action_button(
                            actor_id, state, "✅ Включить все", "bulk",
                            enabled=True,
                        ),
                        personal_action_button(
                            actor_id, state, "🔕 Выключить все", "bulk",
                            enabled=False,
                        ),
                    ]
                )
                navigation.append(
                    personal_action_button(
                        actor_id, state, "✖️ Закрыть", "finish"
                    )
                )
        elif name == "service":
            service_id = level["service_id"]
            if service_id not in registry.list_user_services(actor_id):
                raise ValueError("Вы больше не привязаны к этому служению.")
            service = service_by_id(registry, service_id)
            if service is None:
                raise ValueError("Служение не найдено.")
            enabled = registry.get_user_notifications_enabled(
                actor_id, service_id
            )
            tasks = sort_tasks_for_display(
                await current_personal_tasks(registry, actor_id, service_id, state),
                service_id,
            )
            rules = sort_tasks_for_display(
                registry.list_user_service_schedules(actor_id, service_id),
                service_id, name=lambda rule: rule["task"],
            )
            text = (
                f"🔔 {service_label(service)}\n\n"
                f"Личные уведомления: "
                f"{'🟢 включены' if enabled else '⚪ выключены'}\n"
                f"📝 Ваши задачи: {', '.join(tasks) if tasks else 'нет'}\n"
                f"⏰ Напоминаний: {len(rules)} (своих — до {USER_REMINDERS_PER_SERVICE})\n\n"
                "👤 — от админа: можно только включить или выключить "
                "(или скопировать как своё). ✏️ — ваши: своё время и текст."
            )
            for rule in rules:
                items.append([
                    personal_action_button(
                        actor_id, state, personal_rule_label(rule),
                        "open_rule", service_id=service_id,
                        schedule_id=rule["id"],
                    )
                ])
            actions = [
                personal_action_button(
                    actor_id, state,
                    "🔕 Выключить все" if enabled else "🔔 Включить",
                    "toggle_service", service_id=service_id,
                ),
            ]
            own_count = sum(rule["origin"] == "user" for rule in rules)
            if tasks and own_count < USER_REMINDERS_PER_SERVICE:
                actions.append(
                    personal_action_button(
                        actor_id, state, "➕ Своё напоминание", "add_rule",
                        service_id=service_id,
                    )
                )
            navigation = [
                personal_action_button(
                    actor_id, state, "⬅️ К служениям", "back"
                ),
            ]
        elif name in {"rule", "delete_rule"}:
            service_id = level["service_id"]
            rule = personal_rule(registry, actor_id, service_id, level["schedule_id"])
            service = service_by_id(registry, service_id)
            own = rule["origin"] == "user"
            if name == "delete_rule":
                text = (
                    f"Удалить напоминание «{rule['task']}» · "
                    f"{_rule_label(rule['reminder_days'], rule['reminder_time'])}?"
                )
                actions = [
                    personal_action_button(
                        actor_id, state, "🗑 Да, удалить", "confirm_delete_rule",
                        service_id=service_id, schedule_id=rule["id"],
                    ),
                ]
                navigation = [
                    personal_action_button(actor_id, state, "⬅️ Отмена", "back"),
                ]
            else:
                admin_text = (
                    (service or {}).get("personal_templates", {}).get(rule["task"])
                    or "стандартный текст бота"
                )
                body = rule["body"] or admin_text
                if len(body) > 300:
                    # Карточка — одно сообщение; полный текст придёт в
                    # самом напоминании.
                    body = body[:300].rstrip() + "…"
                text = (
                    f"{'✏️ Своё' if own else '👤 От админа'} напоминание · "
                    f"{rule['task']}\n"
                    f"⏰ {_rule_label(rule['reminder_days'], rule['reminder_time'])}"
                    f" · {'🟢 включено' if rule['enabled'] else '⚪ выключено'}\n\n"
                    f"Заголовок: 🔔 {(service or {}).get('name', service_id)} · "
                    f"{rule['task']} · дата служения\n"
                    f"Текст: {body}"
                )
                if not registry.get_user_notifications_enabled(
                    actor_id, service_id
                ):
                    text += (
                        "\n\n⚠️ Личные уведомления служения выключены — "
                        "напоминания не придут. Включите их на экране служения."
                    )
                if not own:
                    text += (
                        "\n\nℹ️ Напоминание настроил админ — его можно только "
                        "включить или выключить. Чтобы изменить время или "
                        "текст, скопируйте его как своё."
                    )
                actions = [
                    personal_action_button(
                        actor_id, state,
                        "🔕 Выключить" if rule["enabled"] else "🔔 Включить",
                        "toggle_rule", service_id=service_id,
                        schedule_id=rule["id"],
                    ),
                ]
                if own:
                    actions.extend([
                        personal_action_button(
                            actor_id, state, "⏰ Изменить время", "rule_time",
                            service_id=service_id, schedule_id=rule["id"],
                        ),
                        personal_action_button(
                            actor_id, state, "✏️ Изменить текст", "rule_text",
                            service_id=service_id, schedule_id=rule["id"],
                        ),
                    ])
                    if rule["body"]:
                        actions.append(
                            personal_action_button(
                                actor_id, state, "🔄 Текст админа",
                                "reset_rule_text", service_id=service_id,
                                schedule_id=rule["id"],
                            )
                        )
                if sum(
                    item["origin"] == "user"
                    for item in registry.list_user_service_schedules(
                        actor_id, service_id
                    )
                ) < USER_REMINDERS_PER_SERVICE:
                    actions.append(
                        personal_action_button(
                            actor_id, state, "📋 Копировать как своё",
                            "copy_rule", service_id=service_id,
                            schedule_id=rule["id"],
                        )
                    )
                if own:
                    actions.append(
                        personal_action_button(
                            actor_id, state, "🗑 Удалить", "delete_rule",
                            service_id=service_id, schedule_id=rule["id"],
                        )
                    )
                navigation.append(
                    personal_action_button(actor_id, state, "⬅️ Назад", "back")
                )
        elif name == "add_task":
            service_id = level["service_id"]
            tasks = sort_tasks_for_display(
                await current_personal_tasks(registry, actor_id, service_id, state),
                service_id,
            )
            text = "➕ Своё напоминание\nℹ️ Выберите задачу, о которой напомнить."
            for task in tasks:
                items.append([
                    personal_action_button(
                        actor_id, state, f"{task_emoji(task)} {task}",
                        "add_task_pick", service_id=service_id, task=task,
                    )
                ])
            navigation.append(
                personal_action_button(actor_id, state, "⬅️ Назад", "back")
            )
        elif name == "month":
            month = registry.get_month_summary(actor_id)
            text = (
                "📆 Сводка на месяц\n\n"
                f"Статус: {'🟢 включена' if month['enabled'] else '⚪ выключена'}\n"
                f"⏰ Когда: {month_summary_label(month)}\n\n"
                "ℹ️ Один раз в месяц, перед первым воскресеньем, бот пришлёт "
                "все ваши служения на этот месяц. Посмотреть сразу — "
                "«📆 На месяц» в /start или /my_schedule."
            )
            for days, reminder_time in PERSONAL_RULE_PRESETS:
                current = (days, reminder_time) == (
                    month["reminder_days"], month["reminder_time"]
                )
                items.append([
                    personal_action_button(
                        actor_id, state,
                        f"{'✅' if current else '⏰'} "
                        f"{month_summary_label({'reminder_days': days, 'reminder_time': reminder_time})}",
                        "month_time_pick", days=days, reminder_time=reminder_time,
                    )
                ])
            actions = [
                personal_action_button(
                    actor_id, state,
                    "🔕 Выключить" if month["enabled"] else "🔔 Включить",
                    "toggle_month",
                ),
                personal_action_button(
                    actor_id, state, "✏️ Ввести вручную", "month_time_input"
                ),
            ]
            navigation.append(
                personal_action_button(actor_id, state, "⬅️ Назад", "back")
            )
        elif name == "add_time":
            service_id, task = level["service_id"], level["task"]
            text = (
                f"➕ Своё напоминание · {task}\n"
                "ℹ️ Когда напомнить? Свой текст можно задать на следующем "
                "шаге; без него придёт текст админа."
            )
            for days, reminder_time in PERSONAL_RULE_PRESETS:
                items.append([
                    personal_action_button(
                        actor_id, state, f"⏰ {_rule_label(days, reminder_time)}",
                        "add_time_pick", service_id=service_id, task=task,
                        days=days, reminder_time=reminder_time,
                    )
                ])
            actions.append(
                personal_action_button(
                    actor_id, state, "✏️ Ввести вручную", "add_time_input",
                    service_id=service_id, task=task,
                )
            )
            navigation.append(
                personal_action_button(actor_id, state, "⬅️ Назад", "back")
            )
        else:
            raise ValueError("Экран личных уведомлений устарел.")

        pages = _personal_item_pages(items)
        page = min(max(level.get("page", 0), 0), len(pages) - 1)
        level["page"] = page
        rows = [list(group) for group in pages[page]]
        rows.extend(layout_buttons(actions))
        page_navigation = [
            personal_action_button(
                actor_id, state, label, "page", page=next_page
            )
            for next_page, label in (
                (page - 1, f"⬅️ Стр. {page}"),
                (page + 1, f"➡️ Стр. {page + 2}"),
            )
            if 0 <= next_page < len(pages)
        ]
        if page_navigation:
            rows.append(page_navigation)
        if navigation:
            rows.append(navigation)
        if len(pages) > 1:
            text = f"{text}\n📊 Страница {page + 1}/{len(pages)}"
        sent = await _send_dialog(
            target,
            text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
            clean_previous=False,
            _dialog_keep_rows=True,
            _dialog_keyboard_limit=sum(len(row) for row in rows),
        )
        if sent is not None:
            state["menu_message_id"] = sent.message_id
            state["menu_chat_id"] = target.chat.id

    def upcoming_text(registry: Any, actor_id: int, events: list) -> str:
        lines = [f"📅 Мои служения · ближайшие {UPCOMING_WEEKS} недель", ""]
        body = personal_schedule_lines(
            events, linked_assignments(registry, actor_id),
            service_labels(registry),
        )
        lines.extend(body or [
            f"ℹ️ В ближайшие {UPCOMING_WEEKS} недель назначений в графике нет."
        ])
        return "\n".join(lines)

    async def open_my_month(
        message: Message, actor_id: int, year: int | None = None,
        month: int | None = None,
    ) -> None:
        """«📆 На месяц» (этап 4.1): назначения человека за месяц."""
        registry = service_registry()
        loader = getattr(context, "load_upcoming_service_events", None)
        today = datetime.now(context.timezone).date()
        current = (today.year, today.month)
        following = next_month(*current)
        year, month = (year, month) if year else current
        other, arrow = (
            (following, "➡️") if (year, month) == current else (current, "⬅️")
        )
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text=f"{arrow} {month_title(*other).capitalize()}",
                callback_data=f"mymonth:{other[0]:04d}{other[1]:02d}",
            )],
            [InlineKeyboardButton(
                text=start_menu_items["my_schedule"],
                callback_data="menu:my_schedule",
            )],
            [InlineKeyboardButton(
                text=start_menu_items["start"], callback_data="menu:start",
            )],
        ])
        if registry is None or loader is None:
            await _send_dialog(
                message, "⚠️ График сейчас недоступен. Попробуйте позже.",
                parse_mode=None,
            )
            return
        try:
            linked = linked_assignments(registry, actor_id)
            if not linked:
                await _send_dialog(
                    message,
                    "ℹ️ Вы пока не привязаны ни к одной задаче. "
                    "Обратитесь к администратору служения.",
                    parse_mode=None,
                )
                return
            events = await loader(
                since=date(year, month, 1), until=month_end(year, month),
                approved_only=True,
            )
            text = month_summary_text(
                events, linked, service_labels(registry), year, month
            )
        except sqlite3.Error:
            debug.exception("Не удалось прочитать назначения пользователя")
            text = "❌ Не удалось прочитать ваши назначения. Попробуйте позже."
        except (OSError, RuntimeError, ValueError):
            debug.exception("Не удалось загрузить график для «На месяц»")
            text = "⚠️ Не удалось загрузить график. Попробуйте позже."
        await _send_dialog(message, text, reply_markup=keyboard)

    @admin_router.callback_query(F.data.startswith("mymonth:"))
    async def on_my_month(callback: CallbackQuery) -> None:
        if (
            not callback.from_user
            or not callback.message
            or callback.message.chat.type != ChatType.PRIVATE
        ):
            await _answer_callback(callback)
            return
        raw = callback.data.partition(":")[2]
        today = datetime.now(context.timezone).date()
        allowed = {
            (today.year, today.month), next_month(today.year, today.month)
        }
        try:
            year, month = int(raw[:4]), int(raw[4:])
        except ValueError:
            year = month = 0
        if len(raw) != 6 or (year, month) not in allowed:
            await _answer_callback(callback, "⚠️ Экран устарел. Откройте /my_schedule.")
            return
        await _answer_callback(callback)
        await open_my_month(callback.message, callback.from_user.id, year, month)

    async def open_my_schedule(message: Message, actor_id: int) -> None:
        registry = service_registry()
        loader = getattr(context, "load_upcoming_service_events", None)
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=start_menu_items["my_month"],
                        callback_data="menu:my_month",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text=start_menu_items["my_notifications"],
                        callback_data="menu:my_notifications",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text=start_menu_items["start"],
                        callback_data="menu:start",
                    )
                ],
            ]
        )
        if registry is None or loader is None:
            await _send_dialog(
                message, "⚠️ График сейчас недоступен. Попробуйте позже.",
                parse_mode=None,
            )
            return
        try:
            if not registry.list_user_services(actor_id):
                await _send_dialog(
                    message,
                    "ℹ️ Вы пока не привязаны ни к одной задаче. "
                    "Обратитесь к администратору служения.",
                    parse_mode=None,
                )
                return
            text = upcoming_text(
                registry, actor_id, await loader(approved_only=True)
            )
        except sqlite3.Error:
            debug.exception("Не удалось прочитать назначения пользователя")
            text = "❌ Не удалось прочитать ваши назначения. Попробуйте позже."
        except (OSError, RuntimeError, ValueError):
            debug.exception("Не удалось загрузить график для «Мои служения»")
            text = "⚠️ Не удалось загрузить график. Попробуйте позже."
        await _send_dialog(message, text, reply_markup=keyboard)

    # --- Замена ответственного (этап 3.3) ---

    async def start_replacement(
        service_id: str, event_date, task: str, name: str,
        telegram_id: int | None = None,
    ) -> None:
        registry = service_registry()
        if registry is None:
            return
        await start_replacement_search(
            context.bot, registry, service_id, event_date, task, name,
            telegram_id, admin_actor_id=context.admin_id,
            tz=context.timezone, logger=debug,
            schedule_close=getattr(context, "schedule_replacement_close", None),
        )

    async def close_replacement_poll(
        poll_id: str, substitute: dict | None = None,
        actor_id: int | None = None,
    ) -> bool:
        """Итог опроса по сроку (substitute=None) или замена по /change
        (в том числе для запроса без опроса или с закрытым опросом)."""
        registry = service_registry()
        poll = registry.get_replacement_poll(poll_id) if registry else None
        if poll is None:
            return False
        closed = await finish_replacement(
            context.bot, registry, poll, substitute,
            source="admin" if substitute else "poll", actor_id=actor_id,
            admin_actor_id=context.admin_id, tz=context.timezone,
            logger=debug, report_limit=TELEGRAM_TEXT_LIMIT,
        )
        cancel = getattr(context, "cancel_replacement_close", None)
        if closed and substitute is not None and callable(cancel):
            cancel(poll_id)
        return closed

    if not callable(getattr(context, "on_assignment_declined", None)):
        context.on_assignment_declined = start_replacement
    context.close_replacement_poll = close_replacement_poll

    @router.poll_answer()
    async def on_replacement_poll_answer(poll_answer) -> None:
        user = getattr(poll_answer, "user", None)
        registry = service_registry()
        if user is None or registry is None or user.is_bot:
            return
        full_name = " ".join(
            part for part in (user.first_name, user.last_name) if part
        )
        try:
            run = registry.get_readiness_run_by_poll(poll_answer.poll_id)
            if run is None and registry.get_replacement_poll(
                poll_answer.poll_id
            ) is None:
                # Опрос готовности только что отправлен и ещё не записан —
                # ответ учтётся сразу после записи.
                if readiness.hold_early_answer(
                    poll_answer.poll_id, user.id, user.username or "",
                    full_name, list(poll_answer.option_ids or ()),
                ):
                    return
            if run is not None:
                # Опрос готовности (этап 5): варианты — даты месяца.
                registry.record_readiness_answer(
                    poll_answer.poll_id, user.id, user.username or "",
                    full_name,
                    readiness.answer_dates(
                        poll_answer.option_ids,
                        run.get("options") or run["dates"],
                        reverse_poll=run.get("reverse_poll", False),
                    ),
                )
                return
        except (ValueError, sqlite3.Error):
            debug.exception("Не удалось сохранить ответ в опросе готовности")
            return
        try:
            registry.record_poll_answer(
                poll_answer.poll_id, user.id, user.username or "", full_name,
                poll_answer_choice(poll_answer.option_ids),
            )
        except (ValueError, sqlite3.Error):
            debug.exception("Не удалось сохранить ответ в опросе замены")

    def service_admins_text(registry, service_id: str) -> str:
        people = []
        for admin in registry.list_service_admin_details(service_id):
            person = registry.service_person(service_id, admin["telegram_id"])
            name = person["name"] if person else None
            username = admin["username"] or (person or {}).get("username")
            label = " / ".join(
                part for part in (name, f"@{username}" if username else None)
                if part
            )
            people.append(label or "админ без имени")
        return ", ".join(people) or "админы не назначены — обратитесь к суперадмину"

    async def admin_assign_replacement(
        actor_id: int, service_id: str, event_date, task: str,
        old_name: str, substitute: dict,
    ) -> bool:
        """Замена от админа — без отказа участника. Если по этому человеку
        уже есть запрос (опрос), он закрывается этой заменой."""
        registry = service_registry()
        request = registry.find_active_replacement_request(
            service_id, event_date.isoformat(), task, old_name
        )
        if request is not None:
            return await close_replacement_poll(
                request["poll_id"], substitute, actor_id
            )
        service = registry.get_service(service_id)
        if service is None:
            return False
        if not await assign_replacement(
            context.bot, registry, service, event_date, task, old_name,
            substitute, source="admin", actor_id=actor_id,
            admin_actor_id=context.admin_id, logger=debug,
            report_limit=TELEGRAM_TEXT_LIMIT, expect_current=True,
        ):
            return False
        interaction.info(
            "Замена назначена админом: service=%s task=%s date=%s actor=%s",
            service_id, task, event_date, actor_id,
        )
        return True

    def substitute_by_username(
        registry, service_id: str, task: str, username: str
    ) -> dict | None:
        person = registry.find_service_person_by_username(service_id, username)
        if person is None or not person.get("telegram_id"):
            return None
        named = registry.service_person(
            service_id, person["telegram_id"], task
        ) or person
        return {
            "name": named["name"],
            "telegram_id": person["telegram_id"],
            "username": named.get("username") or person.get("username") or "",
        }

    # --- Экран «📅 График» → «🔁 Замены» (этап 3.4) ---

    def replace_button(text: str, pending: dict, action: str, arg="") -> InlineKeyboardButton:
        return InlineKeyboardButton(
            text=text,
            callback_data=f"rpl:{pending['session_id']}:{action}:{arg}",
        )

    def replace_window_end():
        """Последний день следующего месяца."""
        today = datetime.now(context.timezone).date()
        next_month = (today.replace(day=1) + timedelta(days=32)).replace(day=1)
        return (next_month + timedelta(days=32)).replace(day=1) - timedelta(days=1)

    def replace_events_window(events: list, service_id: str) -> list:
        """События служения с сегодня: текущий и следующий месяц."""
        today = datetime.now(context.timezone).date()
        last = replace_window_end()
        return [
            event for event in events
            if event.service_id == service_id
            and today <= event.event_date <= last
            and any(
                item.name and not item.is_informational
                for item in event.assignments
            )
        ]

    def replace_weeks(events: list, service_id: str) -> list[dict]:
        """События окна замен по неделям (воскресенье — правая граница;
        задача со своим днём — в неделе своего воскресенья), только недели
        с назначениями."""
        registry = service_registry()
        own = {
            task["name"]: task["day"]
            for task in registry.plan_tasks(service_id)
            if task.get("day") is not None
        }
        # Событие не в воскресенье (доп. суббота) — в неделю своего
        # воскресенья, вместе с ним.
        weeks: dict = {}
        for block in schedule_plan.week_events(events, own):
            day = block["date"]
            sunday = day + timedelta(days=(6 - day.weekday()) % 7)
            week = weeks.setdefault(sunday, {
                "date": sunday, "assignments": [], "days": {}, "moments": [],
            })
            week["assignments"] += block["assignments"]
            week["moments"] += block["moments"]
        return [
            weeks[sunday] for sunday in sorted(weeks)
            if week_items(weeks[sunday], service_id)
        ]

    def week_title(block: dict) -> str:
        """«44 нед. 26.10–01.11» — номер недели по ISO (как в календарях
        телефонов)."""
        monday = block["date"] - timedelta(days=6)
        end = max([block["date"], *block["moments"]])
        return (
            f"{monday.isocalendar().week} нед. {monday:%d.%m}–{end:%d.%m}"
        )

    def week_items(block: dict, service_id: str) -> list[tuple]:
        """Задачи недели по порядку дней: (день, задача, люди)."""
        items: dict[tuple, list[str]] = {}
        for item, moment in zip(block["assignments"], block["moments"]):
            if item.is_informational or not item.name:
                continue
            names = items.setdefault((moment, item.task), [])
            if item.name not in names:
                names.append(item.name)
        ordered = sort_tasks_for_display(
            items.items(), service_id, name=lambda entry: entry[0][1],
        )
        return [
            (day, task, names)
            for (day, task), names in sorted(
                ordered, key=lambda entry: entry[0][0]
            )
        ]

    def replace_day_groups(
        items: list[tuple],
    ) -> list[tuple[date, list[tuple]]]:
        days: list[tuple[date, list[tuple]]] = []
        for day, task, names in items:
            if not days or days[-1][0] != day:
                days.append((day, []))
            days[-1][1].append((task, names))
        return days

    def replace_month_text(pending: dict) -> str:
        """Текущий график месяца выбранной недели (решение 17, 5в): все
        задачи и назначения с заменами, выбранная неделя — ▶️."""
        registry = service_registry()
        sunday = pending["week"]["date"]
        plan = registry.get_schedule_plan(
            pending["service_id"], f"{sunday:%Y-%m}"
        )
        source = (
            "утверждённый" if plan is not None and plan["approved"] is not None
            else "из Excel"
        )
        lines = [
            f"📋 График на {month_title(sunday.year, sunday.month)} "
            f"({source}, с заменами):"
        ]
        first_week = True
        for block in pending.get("month_weeks") or pending["weeks"]:
            if (block["date"].year, block["date"].month) != (
                sunday.year, sunday.month
            ):
                continue
            if not first_week:
                lines.append("")
            first_week = False
            mark = "▶️" if block["date"] == sunday else "🗓"
            lines.append(f"{mark} {week_title(block)}")
            for day_index, (day, tasks) in enumerate(
                replace_day_groups(week_items(block, pending["service_id"]))
            ):
                if day_index:
                    lines.append("")
                lines.append(f"   {_WEEKDAYS[day.weekday()]} {day:%d.%m}:")
                lines.extend(
                    f"      {task_emoji(task)} {task}: {', '.join(names)}"
                    for task, names in tasks
                )
        return "\n".join(lines)

    def replace_bottom(pending: dict, back: str = "week") -> list:
        """Нижний ряд экранов замены: к неделе (к неделям) и к графику."""
        return [
            replace_button(
                "⬅️ К неделе" if back == "week" else "⬅️ К неделям",
                pending, "weekback" if back == "week" else "dates",
            ),
            replace_button("📅 К графику", pending, "graph"),
        ]

    async def send_replace_screen(message: Message, text: str,
                                  rows: list, *, paged: bool = False) -> None:
        """paged — длинный список (заменяющие): листание по страницам,
        «⬅️ К …» и «📅 К графику» на каждой странице внизу."""
        await _send_dialog(
            message, text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
            _dialog_keep_rows=not paged,
            _dialog_keyboard_limit=(
                12 if paged else sum(len(row) for row in rows)
            ),
            _dialog_text_limit=TELEGRAM_TEXT_LIMIT,
            parse_mode=None,
        )

    async def open_replace_dates(
        message: Message, actor_id: int, service_id: str, notice: str = "",
        week: date | None = None,
    ) -> None:
        """«📅 График» → «🔁 Замены»: недели текущего и следующего месяца
        (решение 17, 5в). week — сразу открыть неделю (после замены)."""
        registry = service_registry()
        service = service_by_id(registry, service_id) if registry else None
        loader = getattr(context, "load_upcoming_service_events", None)
        if service is None or loader is None:
            await _send_dialog(message, "Замена сейчас недоступна.", parse_mode=None)
            return
        await clean_and_clear_pending_actor(actor_id)
        today = datetime.now(context.timezone).date()
        try:
            # С начала месяца — для графика над кнопками (прошедшие недели
            # и задачи месяца тоже); кнопки замены — только с сегодня.
            # Понедельник недели 1-го числа: первая неделя месяца целиком.
            month_start = today.replace(day=1)
            month_start -= timedelta(days=month_start.weekday())
            loaded = await loader(
                since=month_start, until=replace_window_end(),
            )
            events = replace_events_window(loaded, service_id)
            month_events = [
                event for event in loaded
                if event.service_id == service_id
                and month_start <= event.event_date <= replace_window_end()
            ]
        except (OSError, RuntimeError, ValueError, sqlite3.Error):
            debug.exception("Не удалось загрузить график для замены")
            events = None
            month_events = []
        pending = {
            "operation": "service_replace",
            "service_id": service_id,
            "session_id": secrets.token_urlsafe(9),
            "step": "replace_date",
            "events": events or [],
            "weeks": replace_weeks(events or [], service_id),
            "month_weeks": replace_weeks(month_events, service_id),
            "label": service_label(service),
        }
        pending_inputs[actor_id] = pending
        chosen = next(
            (block for block in pending["weeks"] if block["date"] == week),
            None,
        )
        if chosen is not None:
            pending["week"] = chosen
            await show_replace_week(message, pending, notice)
            return
        title = f"🔁 {pending['label']} › График › Замены"
        if events is None:
            text = f"{title}\n⚠️ Не удалось загрузить график. Попробуйте позже."
        elif not pending["weeks"]:
            text = (
                f"{title}\n⚪ В этом и следующем месяце назначений в графике "
                "нет."
            )
        elif notice:
            text = (
                f"{title}\n\n{notice}\n\n"
                "ℹ️ Выберите неделю, затем задачу — замена действует только "
                "на её дату."
            )
        else:
            text = (
                f"{title}\n"
                "ℹ️ Выберите неделю, затем задачу — замена действует только "
                "на её дату."
            )
        rows = [
            [replace_button(f"🗓 {week_title(block)}", pending, "week", index)]
            for index, block in enumerate(pending["weeks"])
        ]
        rows.append([replace_button("📅 К графику", pending, "graph")])
        await send_replace_screen(message, text, rows)

    async def show_replace_week(message: Message, pending: dict,
                                notice: str = "") -> None:
        """Неделя: все её задачи с датами и людьми, выбор задачи."""
        items = week_items(pending["week"], pending["service_id"])
        pending["items"] = items
        pending["step"] = "replace_week"
        for key in ("event", "task", "old_name", "task_people", "substitute"):
            pending.pop(key, None)
        lines = [f"🔁 {pending['label']} › График › Замены"]
        if notice:
            lines.extend(["", notice])
        lines.extend([
            "",
            replace_month_text(pending),
            "",
            f"Неделя {week_title(pending['week'])}:",
        ])
        for day_index, (day, tasks) in enumerate(replace_day_groups(items)):
            if day_index:
                lines.append("")
            lines.append(f"   {_WEEKDAYS[day.weekday()]} {day:%d.%m}:")
            lines.extend(
                f"      {task_emoji(task)} {task} — {', '.join(names)}"
                for task, names in tasks
            )
        lines.extend(["", "ℹ️ Выберите задачу."])
        rows = [
            [replace_button(
                f"{task_emoji(task)} {task} · {_WEEKDAYS[day.weekday()]} "
                f"{day:%d.%m}"[:60],
                pending, "item", index,
            )]
            for index, (day, task, _names) in enumerate(items)
        ]
        rows.append(replace_bottom(pending, back="weeks"))
        await send_replace_screen(message, "\n".join(lines), rows)

    async def show_replace_task_people(message: Message, pending: dict) -> None:
        """В задаче несколько человек — кого заменить."""
        pending["step"] = "replace_person"
        rows = [
            [replace_button(f"👤 {name}", pending, "person", index)]
            for index, name in enumerate(pending["task_people"])
        ]
        rows.append(replace_bottom(pending))
        await send_replace_screen(
            message,
            f"🔁 Замены · {pending['label']}\n\n"
            f"{replace_month_text(pending)}\n\n"
            f"«{pending['task']}» · {pending['event'].event_date:%d.%m.%Y}"
            "\n\nℹ️ Кого нужно заменить?",
            rows,
        )

    def replace_candidates(registry, pending: dict) -> list[dict]:
        """Люди служения с этой задачей, которых нет в ней на эту дату."""
        service_id, task = pending["service_id"], pending["task"]
        busy = {
            item.name for item in pending["event"].assignments
            if item.task == task
        }
        candidates: dict[str, dict] = {}
        for member in registry.list_service_members(service_id):
            if task in member["tasks"] and member["name"] not in busy:
                candidates.setdefault(member["name"], {
                    "name": member["name"],
                    "telegram_id": member["telegram_id"],
                    "username": member["username"] or "",
                })
        for contact in registry.list_responsible_contacts(
            context.admin_id, service_id
        ):
            if contact["task"] == task and contact["name"] not in busy:
                candidates.setdefault(contact["name"], {
                    "name": contact["name"],
                    "telegram_id": contact["telegram_id"],
                    "username": contact["username"] or "",
                })
        return list(candidates.values())

    async def show_replace_candidates(
        message: Message, actor_id: int, pending: dict
    ) -> None:
        registry = service_registry()
        candidates = replace_candidates(registry, pending)
        pending["candidates"] = candidates
        pending["step"] = "replace_candidate"
        rows = [
            [replace_button(
                f"{'✅' if item['telegram_id'] else '⚪'} {item['name']}",
                pending, "pick", index,
            )]
            for index, item in enumerate(candidates)
        ]
        rows.append([replace_button("👤 Выбрать пользователя Telegram", pending, "tg")])
        if len(pending.get("task_people", [])) > 1:
            rows.append([replace_button("⬅️ К людям", pending, "people")])
        rows.append(replace_bottom(pending))
        text = (
            f"🔁 Замены · {pending['label']}\n\n"
            f"{replace_month_text(pending)}\n\n"
            f"«{pending['task']}» · "
            f"{pending['event'].event_date:%d.%m.%Y}\n"
            f"Вместо: {pending['old_name']}\n\n"
            + (
                "ℹ️ Выберите, кто заменит (люди служения с этой задачей; ⚪ — "
                "без Telegram), или выберите пользователя Telegram."
                if candidates
                else "ℹ️ В составе служения нет других людей с этой задачей — "
                "выберите пользователя Telegram."
            )
        )
        await send_replace_screen(message, text, rows, paged=len(rows) > 10)

    async def show_replace_confirm(message: Message, pending: dict) -> None:
        pending["step"] = "replace_confirm"
        substitute = pending["substitute"]
        rows = [[
            replace_button("✅ Назначить", pending, "confirm"),
            replace_button("⬅️ Назад", pending, "candidates"),
        ]]
        rows.append(replace_bottom(pending))
        await send_replace_screen(
            message,
            f"🔁 Замены · {pending['label']}\n\n"
            f"{replace_month_text(pending)}\n\n"
            f"Подтвердите замену\n"
            f"🗓 {pending['event'].event_date:%d.%m.%Y} · "
            f"«{pending['task']}»\n"
            f"{pending['old_name']} → "
            f"{person_label(substitute['name'], substitute.get('username'))}"
            "\n\n"
            "ℹ️ Замена действует только на эту дату и главнее графика. Чат "
            "служения, админы и заменяющий получат сообщение.",
            rows,
        )

    async def ask_replace_telegram_user(message: Message, actor_id: int, pending: dict) -> None:
        pending["step"] = "await_user"
        for request_id, share in list(pending_shares.items()):
            if share[0] == actor_id and share[1] == "service_replace":
                pending_shares.pop(request_id, None)
        request_id = new_share_request_id()
        pending_shares[request_id] = (
            actor_id, "service_replace", pending["service_id"],
            pending["task"], pending["old_name"],
        )
        keyboard = ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(
                    text="Выбрать пользователя",
                    request_users=KeyboardButtonRequestUsers(
                        request_id=request_id, user_is_bot=False,
                        max_quantity=1, request_name=True,
                        request_username=True,
                    ),
                )],
                [KeyboardButton(text=CONTACT_BACK_TEXT)],
                [
                    KeyboardButton(text="⬅️ К неделе"),
                    KeyboardButton(text="📅 К графику"),
                ],
            ],
            resize_keyboard=True,
            one_time_keyboard=True,
        )
        await _send_dialog(
            message,
            f"🔁 Замены · {pending['label']}\n\n"
            f"{replace_month_text(pending)}\n\n"
            f"Нажмите кнопку и выберите, кто заменит {pending['old_name']} "
            f"(«{pending['task']}»). «{CONTACT_BACK_TEXT}» — вернуться к "
            "списку заменяющих.",
            reply_markup=keyboard,
            parse_mode=None,
        )

    async def on_replace_user_shared(message: Message, share: tuple) -> None:
        actor_id = message.from_user.id
        pending = pending_inputs.get(actor_id)
        if (
            pending is None
            or pending.get("operation") != "service_replace"
            or pending.get("step") != "await_user"
            or pending.get("service_id") != share[2]
            or pending.get("old_name") != share[4]
        ):
            await _send_dialog(
                message, "Выбор устарел. Откройте «🔁 Замены» заново.",
                parse_mode=None, reply_markup=ReplyKeyboardRemove(),
            )
            return
        user = message.users_shared.users[0]
        registry = service_registry()
        known = registry.service_person(
            pending["service_id"], user.user_id, pending["task"]
        )
        full_name = " ".join(
            part for part in (
                user.first_name or "", getattr(user, "last_name", "") or "",
            ) if part
        )
        name = (known or {}).get("name") or full_name or (
            f"@{user.username}" if user.username else "Участник"
        )
        busy = {
            item.name for item in pending["event"].assignments
            if item.task == pending["task"]
        }
        if name in busy:
            await _send_dialog(
                message,
                "❌ Это тот же человек, которого нужно заменить."
                if name == pending["old_name"]
                else f"❌ {name} уже в задаче «{pending['task']}» на эту дату.",
                parse_mode=None, reply_markup=ReplyKeyboardRemove(),
            )
            await show_replace_candidates(message, actor_id, pending)
            return
        pending["substitute"] = {
            "name": name,
            "telegram_id": user.user_id,
            "username": (known or {}).get("username") or user.username or "",
        }
        await message.answer("Пользователь выбран.", reply_markup=ReplyKeyboardRemove())
        await show_replace_confirm(message, pending)

    @admin_router.callback_query(F.data.startswith("rpl:"))
    async def on_replace_action(callback: CallbackQuery) -> None:
        if not callback.from_user or not callback.message:
            await _answer_callback(callback)
            return
        parts = (callback.data or "").split(":", 3)
        actor_id = callback.from_user.id
        pending = pending_inputs.get(actor_id)
        if (
            len(parts) != 4
            or pending is None
            or pending.get("operation") != "service_replace"
            or pending.get("session_id") != parts[1]
        ):
            await _answer_callback(callback, "⚠️ Экран устарел. Откройте «🔁 Замены» заново.")
            return
        _, _, action, arg = parts
        service_id = pending["service_id"]
        registry = service_registry()
        if registry is None or not is_service_admin(actor_id, service_id):
            await _answer_callback(callback, "❌ Недостаточно прав.")
            return

        def pick(items: list, raw: str):
            try:
                return items[int(raw)]
            except (ValueError, IndexError):
                return None

        message = callback.message
        if action in {"graph", "card"}:
            # «📅 К графику» — раздел «📅 График» (замены теперь там).
            await clean_and_clear_pending_actor(actor_id)
            await show_month_graph(message, actor_id, service_id)
        elif action == "dates":
            await open_replace_dates(message, actor_id, service_id)
        elif action == "week":
            block = pick(pending.get("weeks", []), arg)
            if block is None:
                await _answer_callback(callback, "⚠️ Неделя устарела.")
                return
            pending["week"] = block
            await show_replace_week(message, pending)
        elif action == "weekback" and "week" in pending:
            await show_replace_week(message, pending)
        elif action == "item" and "week" in pending:
            item = pick(pending.get("items", []), arg)
            event = item and next(
                (
                    event for event in pending["events"]
                    if event.event_date == item[0]
                ),
                None,
            )
            if event is None:
                await _answer_callback(callback, "⚠️ Выбор устарел.")
                return
            pending["event"] = event
            pending["task"] = item[1]
            pending["task_people"] = list(item[2])
            if len(item[2]) == 1:
                pending["old_name"] = item[2][0]
                await show_replace_candidates(message, actor_id, pending)
            else:
                await show_replace_task_people(message, pending)
        elif action == "people" and "task" in pending:
            await show_replace_task_people(message, pending)
        elif action == "person" and "task" in pending:
            name = pick(pending.get("task_people", []), arg)
            if name is None:
                await _answer_callback(callback, "⚠️ Выбор устарел.")
                return
            pending["old_name"] = name
            await show_replace_candidates(message, actor_id, pending)
        elif action == "candidates" and "task" in pending:
            await show_replace_candidates(message, actor_id, pending)
        elif action == "pick" and "task" in pending:
            candidate = pick(pending.get("candidates", []), arg)
            if candidate is None:
                await _answer_callback(callback, "⚠️ Выбор устарел.")
                return
            pending["substitute"] = dict(candidate)
            await show_replace_confirm(message, pending)
        elif action == "tg" and "task" in pending:
            await ask_replace_telegram_user(message, actor_id, pending)
        elif action == "confirm" and pending.get("step") == "replace_confirm":
            substitute = pending["substitute"]
            event = pending["event"]
            # Повторное нажатие во время назначения не дублирует замену.
            pending["step"] = "replace_assigning"
            try:
                done = await admin_assign_replacement(
                    actor_id, service_id, event.event_date, pending["task"],
                    pending["old_name"], substitute,
                )
            except Exception:  # noqa: BLE001
                debug.exception("Не удалось назначить замену с экрана")
                pending["step"] = "replace_confirm"
                await _answer_callback(callback, "❌ Не удалось назначить замену.")
                return
            notice = (
                f"✅ Замена назначена: «{pending['task']}» "
                f"{event.event_date:%d.%m} — {substitute['name']} вместо "
                f"{pending['old_name']}."
                if done else "ℹ️ Эту замену уже назначили."
            )
            await open_replace_dates(
                message, actor_id, service_id, notice,
                week=pending["week"]["date"] if "week" in pending else None,
            )
        else:
            await _answer_callback(callback, "⚠️ Этот экран уже устарел.")
            return
        await _answer_callback(callback)

    async def task_names_on_date(service_id: str, event_date, task: str) -> list:
        """Кто стоит в задаче на дату (график с заменами)."""
        loader = getattr(context, "load_upcoming_service_events", None)
        if loader is None:
            return []
        events = await loader(until=max(event_date, replace_window_end()))
        return [
            item.name
            for event in events
            if event.service_id == service_id and event.event_date == event_date
            for item in event.assignments
            if item.task == task and item.name
        ]

    @admin_router.message(Command("change"))
    async def on_change_command(message: Message) -> None:
        """/change {задача} @username [дд.мм] — замена от админа.

        Есть запрос (участник нажал «Не смогу», ответ на опрос) — замена
        по нему. Иначе — безусловно: на указанную или ближайшую дату, где у
        задачи один ответственный."""
        if not message.from_user:
            return
        registry = service_registry()
        if registry is None:
            return
        reply = lambda text: message.answer(text, parse_mode=None)  # noqa: E731
        today = datetime.now(context.timezone).date()
        try:
            task_text, username, event_date = parse_change_args(
                (message.text or "").partition(" ")[2], today
            )
        except ValueError as exc:
            await reply(f"ℹ️ {exc}")
            return
        is_private = message.chat.type == ChatType.PRIVATE

        def in_this_chat(service_id: str, chat_id: int | None = None) -> bool:
            if is_private or chat_id == message.chat.id:
                return True
            service = registry.get_service(service_id) or {}
            return service.get("group_chat_id") == message.chat.id

        # 1. Запрос замены: ответ на опрос или невыполненный запрос задачи.
        polls = []
        reply_to = getattr(message, "reply_to_message", None)
        if not is_private and reply_to is not None:
            poll = registry.replacement_poll_by_message(
                message.chat.id, reply_to.message_id
            )
            if poll is not None and poll["filled"]:
                await reply("ℹ️ Этот поиск замены уже завершён.")
                return
            if (
                poll is not None and event_date is not None
                and poll["event_date"] != event_date.isoformat()
            ):
                await reply(
                    f"ℹ️ Этот опрос — на {date.fromisoformat(poll['event_date']):%d.%m}, "
                    f"а в команде указано {event_date:%d.%m}. Уберите дату или "
                    "отправьте команду без ответа на опрос."
                )
                return
            if poll is not None:
                polls = [poll]
        if not polls:
            polls = [
                poll
                for poll in registry.list_unfilled_replacement_requests(
                    today.isoformat()
                )
                if normalized_name(poll["task"]) == normalized_name(task_text)
                and (event_date is None or poll["event_date"] == event_date.isoformat())
                and in_this_chat(poll["service_id"], poll["chat_id"])
            ]
        # Запросы чужих служений не мешают замене в своих.
        foreign_polls = [
            poll for poll in polls
            if not is_service_admin(message.from_user.id, poll["service_id"])
        ]
        polls = [poll for poll in polls if poll not in foreign_polls]
        # 2. Без запроса — задача служения на дату по графику.
        targets = [
            (poll["service_id"], date.fromisoformat(poll["event_date"]),
             poll["task"], [poll["old_name"]])
            for poll in polls
        ]
        if not targets:
            loader = getattr(context, "load_upcoming_service_events", None)
            try:
                events = await loader(until=replace_window_end()) if loader else []
            except (OSError, RuntimeError, ValueError, sqlite3.Error):
                debug.exception("Не удалось загрузить график для /change")
                await reply("⚠️ Не удалось загрузить график. Попробуйте позже.")
                return
            nearest: dict[str, tuple] = {}
            first_any: dict[str, tuple] = {}
            for event in events or []:
                if not in_this_chat(event.service_id) or event.event_date < today:
                    continue
                if event_date is not None and event.event_date != event_date:
                    continue
                matched = [
                    assignment for assignment in event.assignments
                    if normalized_name(assignment.task) == normalized_name(task_text)
                    and assignment.name and not assignment.is_informational
                ]
                if not matched:
                    continue
                target = (
                    event.service_id, event.event_date, matched[0].task,
                    list(dict.fromkeys(item.name for item in matched)),
                )
                # Без даты — ближайшая дата с одним ответственным; если
                # таких нет, первая (с подсказкой выбрать в «🔁 Замены»).
                first_any.setdefault(event.service_id, target)
                if len(target[3]) == 1 and event.service_id not in nearest:
                    nearest[event.service_id] = target
            targets = list({**first_any, **nearest}.values())
        if not targets and foreign_polls:
            await reply(
                "❌ Назначить замену может только админ служения: "
                + service_admins_text(registry, foreign_polls[0]["service_id"])
            )
            return
        if not targets:
            when = f" на {event_date:%d.%m.%Y}" if event_date else ""
            await reply(
                f"ℹ️ В графике{when} нет задачи «{task_text}» с ответственным."
            )
            return
        allowed = [
            target for target in targets
            if is_service_admin(message.from_user.id, target[0])
        ]
        if not allowed:
            await reply(
                "❌ Назначить замену может только админ служения: "
                + service_admins_text(registry, targets[0][0])
            )
            return
        if len(allowed) > 1:
            choices = "; ".join(
                f"{(registry.get_service(item[0]) or {}).get('name', item[0])} · "
                f"{item[1]:%d.%m}" for item in allowed
            )
            await reply(
                f"ℹ️ Задача «{task_text}» есть в нескольких местах: {choices}. "
                "Ответьте командой на нужный опрос, укажите дату или "
                "откройте «📅 График» → «🔁 Замены»."
            )
            return
        service_id, target_date, task, names = allowed[0]
        if len(names) != 1:
            await reply(
                f"ℹ️ В задаче «{task}» {target_date:%d.%m} несколько человек: "
                f"{', '.join(names)}. Выберите, кого заменить, в «📅 График» "
                "→ «🔁 Замены»."
            )
            return
        substitute = substitute_by_username(registry, service_id, task, username)
        if substitute is None:
            await reply(
                f"❌ @{username} не найден в составе служения. Сначала "
                "привяжите человека к Telegram (/service_contact) или "
                "выберите его в «📅 График» → «🔁 Замены»."
            )
            return
        if substitute["name"] == names[0]:
            await reply("❌ Это тот же человек, которого нужно заменить.")
            return
        try:
            busy = await task_names_on_date(service_id, target_date, task)
        except (OSError, RuntimeError, ValueError, sqlite3.Error):
            debug.exception("Не удалось проверить назначения для /change")
            busy = []
        if substitute["name"] in busy:
            await reply(
                f"❌ {substitute['name']} уже в задаче «{task}» на "
                f"{target_date:%d.%m} — выберите другого человека."
            )
            return
        try:
            done = await admin_assign_replacement(
                message.from_user.id, service_id, target_date, task,
                names[0], substitute,
            )
        except Exception:  # noqa: BLE001
            debug.exception("Не удалось назначить замену")
            await reply("❌ Не удалось назначить замену. Подробности в журнале.")
            return
        if not done:
            await reply("ℹ️ Поиск замены уже завершён.")
            return
        await reply(
            f"✅ Замена назначена: «{task}» {target_date:%d.%m.%Y} — "
            f"{person_label(substitute['name'], substitute['username'])} "
            f"вместо {names[0]}."
        )

    @admin_router.message(Command("here"))
    async def on_here_command(message: Message) -> None:
        """Выбор темы чата служения: /here [служение] в нужной теме группы."""
        if not message.from_user:
            return
        if message.chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP}:
            await message.answer(
                "Отправьте /here в нужной теме группы служения: туда будут "
                "приходить напоминания.",
                parse_mode=None,
            )
            return
        registry = service_registry()
        if registry is None:
            return
        bound = [
            service for service in registry.list_services()
            if service["group_chat_id"] == message.chat.id
        ]
        if not bound:
            await message.answer(
                "К этому чату не привязано ни одно служение.", parse_mode=None
            )
            return
        argument = normalized_name(
            (message.text or "").partition(" ")[2]
        )
        if argument:
            bound = [
                service for service in bound
                if argument in {
                    normalized_name(service["id"]),
                    normalized_name(service["name"]),
                    *(normalized_name(alias) for alias in service["aliases"]),
                }
            ]
        if len(bound) != 1:
            choices = ", ".join(
                f"/here {service['id']} ({service['name']})"
                for service in registry.list_services()
                if service["group_chat_id"] == message.chat.id
            )
            await message.answer(
                (
                    "Служение не найдено в этом чате. "
                    if argument
                    else "К чату привязано несколько служений. "
                )
                + f"Укажите служение: {choices}",
                parse_mode=None,
            )
            return
        service = bound[0]
        thread_id = (
            message.message_thread_id
            if getattr(message, "is_topic_message", False)
            else None
        )
        try:
            registry.set_group_thread(
                message.from_user.id, service["id"], message.chat.id, thread_id
            )
        except PermissionError:
            await message.answer(
                "Тему может выбрать только администратор служения.",
                parse_mode=None,
            )
            return
        except (ValueError, sqlite3.Error) as exc:
            debug.warning("Не удалось сохранить тему чата: %s", exc)
            await message.answer(
                "Не удалось сохранить тему чата.", parse_mode=None
            )
            return
        await message.answer(
            f"✅ Напоминания «{service['name']}» будут приходить "
            + ("в эту тему." if thread_id else "в общую тему чата."),
            parse_mode=None,
        )
        await refresh_card_after_here(message.from_user.id, service["id"])

    async def refresh_card_after_here(actor_id: int, service_id: str) -> None:
        """В личном чате: подтверждение и свежая карточка вместо старой."""
        registry = service_registry()
        service = service_by_id(registry, service_id) if registry else None
        if service is None:
            return
        # Тип чата не «private»: _send_dialog не трогает меню и состояние
        # других сценариев админа, открытых в личном чате.
        private = SimpleNamespace(
            chat=SimpleNamespace(id=actor_id, type=SERVICE_CARD_CHAT_TYPE),
            answer=lambda text, **kwargs: context.bot.send_message(
                actor_id, text, **kwargs
            ),
        )
        previous = last_service_cards.get((actor_id, service_id))
        thread_id = service.get("group_thread_id")
        try:
            await context.bot.send_message(
                actor_id,
                f"✅ Тема чата сохранена · {service['name']}\n"
                + (
                    "Напоминания и подтверждения будут приходить в выбранную "
                    "тему."
                    if thread_id
                    else "Напоминания будут приходить в общую тему чата."
                ),
                parse_mode=None,
            )
            await send_service_card(private, actor_id, service)
        except Exception:  # noqa: BLE001
            # Например, личный чат с ботом не начат — хватит ответа в группе.
            debug.warning(
                "Не удалось обновить карточку служения в личном чате",
                exc_info=True,
            )
            return
        if previous is not None:
            chat_id, message_ids = previous
            for message_id in message_ids:
                await clean_previous_menu_by_id(
                    context.bot, chat_id, message_id, debug
                )

    @admin_router.message(Command("my_schedule"))
    async def on_my_schedule(message: Message) -> None:
        if not message.from_user or message.chat.type != ChatType.PRIVATE:
            return
        await clean_and_clear_pending_actor(message.from_user.id)
        await open_my_schedule(message, message.from_user.id)

    @admin_router.message(Command("my_notifications"))
    async def on_my_notifications(message: Message) -> None:
        if not message.from_user or message.chat.type != ChatType.PRIVATE:
            return
        await open_my_notifications(message, message.from_user.id)

    async def open_my_notifications(message: Message, actor_id: int) -> None:
        await invalidate_personal_session(actor_id)
        registry = service_registry()
        if registry is None:
            await _send_dialog(
                message,
                "Реестр настроек служений недоступен. Попробуйте позже.",
                parse_mode=None,
            )
            return
        try:
            if not registry.list_user_services(actor_id):
                await _send_dialog(
                    message,
                    "ℹ️ Вы пока не привязаны ни к одной задаче. "
                    "Обратитесь к администратору служения.",
                    parse_mode=None,
                )
                return
            state = {
                "operation": "my_notifications",
                "session_id": secrets.token_urlsafe(8),
                "levels": [{"name": "summary", "page": 0}],
            }
            pending_inputs[actor_id] = state
            await render_current_personal_level(
                message, actor_id, state, clean_previous=False
            )
        except (sqlite3.Error, ValueError):
            debug.exception("Не удалось открыть личные уведомления")
            await _send_dialog(
                message, "Не удалось открыть настройки личных уведомлений.",
                parse_mode=None,
            )

    @admin_router.callback_query(F.data.startswith("pmn:"))
    async def on_personal_notifications_callback(
        callback: CallbackQuery,
    ) -> None:
        if (
            not callback.from_user
            or not callback.message
            or callback.message.chat.type != ChatType.PRIVATE
        ):
            await _answer_callback(callback)
            return
        parts = callback.data.split(":")
        if len(parts) != 3:
            await _answer_callback(callback, "⚠️ Действие устарело.")
            return
        _, session_id, token = parts
        action = pending_personal_actions.pop(token, None)
        actor_id = callback.from_user.id
        state = pending_inputs.get(actor_id)
        if (
            action is None
            or action[0] != actor_id
            or action[1] != session_id
            or state is None
            or action[2] != state.get("screen_id")
            or state.get("operation") not in {
                "my_notifications", "my_notifications_input"
            }
            or state.get("session_id") != session_id
        ):
            await _answer_callback(callback, "⚠️ Меню устарело. Откройте /my_notifications.")
            return
        _, _, _, action_name, payload = action
        notice: str | None = None
        registry = service_registry()
        if registry is None:
            await _answer_callback(callback, "❌ Реестр настроек недоступен.")
            return
        try:
            if action_name == "finish":
                await finish_personal_session(actor_id)
                await _answer_callback(callback, "Меню закрыто")
                return
            if state.get("operation") == "my_notifications_input":
                state["operation"] = "my_notifications"
                state.pop("input", None)
            current = state["levels"][-1]
            if action_name == "page":
                current["page"] = payload["page"]
            elif action_name == "back":
                pop_personal_level(state)
            elif action_name == "open_service":
                service_id = payload["service_id"]
                if service_id not in registry.list_user_services(actor_id):
                    raise ValueError("Вы не привязаны к этому служению.")
                push_personal_level(state, {"name": "service", "service_id": service_id})
            elif action_name == "toggle_service":
                service_id = payload["service_id"]
                if service_id not in registry.list_user_services(actor_id):
                    raise ValueError("Вы не привязаны к этому служению.")
                enabled = registry.get_user_notifications_enabled(actor_id, service_id)
                if not enabled:
                    problem = await ensure_default_personal_rule(
                        registry, actor_id, service_id, state
                    )
                    if problem:
                        raise ValueError(problem)
                registry.set_user_notifications_enabled(
                    actor_id, service_id, not enabled
                )
            elif action_name == "bulk":
                if not payload["enabled"]:
                    registry.set_user_notifications_all(actor_id, False)
                else:
                    failed: dict[str, list[str]] = {}
                    for service_id in registry.list_user_services(actor_id):
                        problem = await ensure_default_personal_rule(
                            registry, actor_id, service_id, state
                        )
                        if problem is None:
                            registry.set_user_notifications_enabled(
                                actor_id, service_id, True
                            )
                        else:
                            service = service_by_id(registry, service_id)
                            failed.setdefault(problem, []).append(
                                service_label(service) if service else service_id
                            )
                    if failed:
                        notice = "⚠️ Не включено: " + "; ".join(
                            f"{', '.join(labels)} — "
                            f"{PERSONAL_PROBLEM_SHORT.get(problem, problem)}"
                            for problem, labels in failed.items()
                        ) + "."
            elif action_name == "open_month":
                push_personal_level(state, {"name": "month"})
            elif action_name == "toggle_month":
                month = registry.get_month_summary(actor_id)
                registry.set_month_summary(
                    actor_id, enabled=not month["enabled"]
                )
                notice = (
                    "Сводка на месяц выключена" if month["enabled"]
                    else "Сводка на месяц включена"
                )
            elif action_name == "month_time_pick":
                registry.set_month_summary(
                    actor_id, enabled=True, days=payload["days"],
                    time=payload["reminder_time"],
                )
                notice = "Время сводки сохранено"
            elif action_name == "open_rule":
                personal_rule(
                    registry, actor_id, payload["service_id"],
                    payload["schedule_id"],
                )
                push_personal_level(state, {
                    "name": "rule", "service_id": payload["service_id"],
                    "schedule_id": payload["schedule_id"],
                })
            elif action_name == "toggle_rule":
                rule = personal_rule(
                    registry, actor_id, payload["service_id"],
                    payload["schedule_id"],
                )
                registry.update_schedule(rule["id"], enabled=not rule["enabled"])
                if not rule["enabled"] and not registry.get_user_notifications_enabled(
                    actor_id, payload["service_id"]
                ):
                    # Флаг служения включил бы и все остальные напоминания.
                    notice = (
                        "Напоминание включено, но личные уведомления служения "
                        "выключены — включите их на экране служения"
                    )
            elif action_name == "copy_rule":
                rule = personal_rule(
                    registry, actor_id, payload["service_id"],
                    payload["schedule_id"],
                )
                if rule["task"] not in await current_personal_tasks(
                    registry, actor_id, payload["service_id"], state
                ):
                    raise ValueError("Вы больше не привязаны к этой задаче.")
                # Копия выключена: два одинаковых напоминания не придут, пока
                # у копии не изменят время и не включат её.
                body = shortened_user_body(rule["body"])
                copy = registry.add_schedule(
                    rule["text_id"], rule["reminder_days"],
                    rule["reminder_time"], origin="user", body=body or None,
                    enabled=False,
                )
                pop_personal_level(state)
                push_personal_level(state, {
                    "name": "rule", "service_id": payload["service_id"],
                    "schedule_id": copy["id"],
                })
                notice = "Копия создана выключенной — измените время и включите её"
            elif action_name in {"delete_rule", "confirm_delete_rule", "reset_rule_text"}:
                rule = personal_rule(
                    registry, actor_id, payload["service_id"],
                    payload["schedule_id"],
                )
                if rule["origin"] != "user":
                    raise ValueError("Напоминание админа можно только выключить.")
                if action_name == "delete_rule":
                    push_personal_level(state, {
                        "name": "delete_rule",
                        "service_id": payload["service_id"],
                        "schedule_id": rule["id"],
                    })
                elif action_name == "confirm_delete_rule":
                    registry.delete_schedule(rule["id"])
                    pop_personal_level(state)
                    pop_personal_level(state)
                else:
                    registry.update_schedule(rule["id"], body=None)
            elif action_name in {
                "rule_time", "rule_text", "add_time_input", "month_time_input",
            }:
                if action_name not in {"add_time_input", "month_time_input"}:
                    rule = personal_rule(
                        registry, actor_id, payload["service_id"],
                        payload["schedule_id"],
                    )
                    if rule["origin"] != "user":
                        raise ValueError(
                            "Напоминание админа можно только выключить."
                        )
                state["operation"] = "my_notifications_input"
                state["screen_id"] = secrets.token_urlsafe(8)
                state["input"] = {"type": action_name, **payload}
                await clean_previous_personal_menu(state)
                prompt = (
                    "Отправьте текст напоминания. Заголовок «🔔 служение · "
                    "задача · дата» бот добавит сам. Можно использовать поля "
                    "{name}, {task}, {service_name}, {sunday_date}, "
                    "{telegram_id}, {username}, {event_notes}."
                    if action_name == "rule_text"
                    else "Введите, за сколько дней до первого воскресенья "
                    "месяца (0–6) и во сколько прислать сводку, например: 2 18:30."
                    if action_name == "month_time_input"
                    else "Введите, за сколько дней до служения (0–6) и во "
                    "сколько напомнить, например: 1 18:30."
                )
                sent = await _send_dialog(
                    callback.message, prompt, parse_mode=None,
                    clean_previous=False,
                )
                if sent is not None:
                    state["menu_message_id"] = sent.message_id
                    state["menu_chat_id"] = callback.message.chat.id
                await _answer_callback(callback)
                return
            elif action_name == "add_rule":
                service_id = payload["service_id"]
                tasks = await current_personal_tasks(
                    registry, actor_id, service_id, state
                )
                if not tasks:
                    raise ValueError(PERSONAL_NO_TASKS)
                if len(tasks) == 1:
                    push_personal_level(state, {
                        "name": "add_time", "service_id": service_id,
                        "task": tasks[0],
                    })
                else:
                    push_personal_level(
                        state, {"name": "add_task", "service_id": service_id}
                    )
            elif action_name == "add_task_pick":
                if payload["task"] not in await current_personal_tasks(
                    registry, actor_id, payload["service_id"], state
                ):
                    raise ValueError("Вы больше не привязаны к этой задаче.")
                push_personal_level(state, {
                    "name": "add_time", "service_id": payload["service_id"],
                    "task": payload["task"],
                })
            elif action_name == "add_time_pick":
                rule = await add_own_personal_rule(
                    registry, actor_id, payload["service_id"], payload["task"],
                    payload["days"], payload["reminder_time"], state,
                )
                state["levels"] = [
                    level for level in state["levels"]
                    if level["name"] not in {"add_task", "add_time"}
                ]
                push_personal_level(state, {
                    "name": "rule", "service_id": payload["service_id"],
                    "schedule_id": rule["id"],
                })
                notice = "Напоминание добавлено. Задайте свой текст при желании"
            else:
                raise ValueError("Неизвестное действие личных уведомлений.")
            await render_current_personal_level(
                callback.message, actor_id, state
            )
        except PermissionError:
            await _answer_callback(callback, "❌ Недостаточно прав.")
            return
        except ValueError as exc:
            await _answer_callback(callback, f"⚠️ {exc}")
            return
        except sqlite3.Error:
            debug.exception("Ошибка базы данных в личных уведомлениях")
            await _answer_callback(callback, "❌ Не удалось сохранить изменение.")
            return
        except (OSError, RuntimeError):
            debug.exception("Не удалось сверить задачи с актуальным графиком")
            await _answer_callback(
                callback,
                "❌ Не удалось загрузить актуальный график. Попробуйте позже.",
            )
            return
        await _answer_callback(callback, notice)

    async def drop_screen(
        message: Message, actor_id: int, screen_id: int | None
    ) -> None:
        """Удаляет последний экран; если он же — меню в pending, /start
        не пытается удалить его второй раз."""
        await clean_previous_menu_by_id(
            context.bot, message.chat.id, screen_id, debug
        )
        pending = pending_inputs.get(actor_id)
        if pending is not None and pending.get("menu_message_id") == screen_id:
            pending.pop("menu_message_id", None)

    @admin_router.message(F.text.in_(NAV_TEXTS))
    async def on_nav_button(message: Message) -> None:
        """«↩️ Отмена» — кнопка «⬅️ …» последнего экрана, «🏠 В начало» —
        /start. Срабатывает и на шаге ввода текста."""
        if not message.from_user or message.chat.type != ChatType.PRIVATE:
            return
        actor_id = message.from_user.id
        delete = getattr(message, "delete", None)
        if callable(delete):
            try:
                await delete()
            except TelegramAPIError:
                debug.debug("Не удалось удалить нажатие навигации")
        state = nav_state.get(actor_id) or {}
        back = state.get("back")
        if message.text == NAV_HOME_TEXT or back is None:
            await drop_screen(message, actor_id, state.get("screen"))
            await show_start(message, message.from_user)
            return
        interaction.info("Навигация «Отмена» %s: %s", actor_id, back)
        screen = ScreenMessage(
            message, state.get("screen"), context.bot,
            datetime.now().astimezone(),
        )
        callback = CallbackQuery.model_construct(
            id=NAV_CALLBACK_ID,
            from_user=message.from_user,
            chat_instance=NAV_CALLBACK_ID,
            message=screen,
            data=back,
        )
        state["back"] = None
        try:
            for target_router in (admin_router, router):
                result = await target_router.propagate_event(
                    "callback_query", callback
                )
                if result is not UNHANDLED:
                    break
        except Exception:
            if not screen.sent and state.get("back") is None:
                # Экран не сменился — «Отмена» можно повторить.
                state["back"] = back
            raise
        debug.log(
            TRACE, "Отмена %s: повтор %s, новых экранов: %s", actor_id, back,
            screen.sent,
        )
        if not screen.sent:
            # Кнопка устарела и нового экрана нет — не оставлять пустоту.
            await drop_screen(message, actor_id, screen.message_id)
            await show_start(message, message.from_user)

    @admin_router.message(
        F.text & ~F.text.startswith("/"),
        lambda message: bool(
            message.from_user
            and (
                pending_inputs.get(message.from_user.id) or {}
            ).get("operation") in {"event_note_input", "event_note_schedule_input"}
        ),
    )
    async def on_event_note_text(message: Message) -> None:
        if (
            not message.from_user
            or message.chat.type != ChatType.PRIVATE
            or not message.text
        ):
            return
        actor_id = message.from_user.id
        pending = pending_inputs.get(actor_id)
        registry = service_registry()
        if (
            not pending
            or pending.get("operation") not in {"event_note_input", "event_note_schedule_input"}
            or registry is None
            or not is_service_admin(actor_id, "assembly")
        ):
            pending_inputs.pop(actor_id, None)
            await _send_dialog(
                message, "Этот ввод устарел. Откройте заметки заново.",
                parse_mode=None,
            )
            return
        try:
            if pending["operation"] == "event_note_schedule_input":
                if message.text.strip().lower() == "нет":
                    send_at = None
                else:
                    try:
                        moment = datetime.strptime(message.text.strip(), "%d.%m.%Y %H:%M")
                    except ValueError:
                        raise ValueError("Введите дату и время: ДД.ММ.ГГГГ ЧЧ:ММ.") from None
                    if moment.replace(tzinfo=context.timezone) <= datetime.now(context.timezone):
                        raise ValueError("Время отправки должно быть в будущем.")
                    send_at = moment.isoformat(timespec="minutes")
                registry.set_event_note_send_at(
                    actor_id, pending["event_sunday"], send_at,
                    expected_revision=pending["revision"],
                )
            else:
                registry.save_event_note_draft(
                    actor_id, pending["event_sunday"], message.text,
                    pending.get("targets"), expected_revision=pending["revision"],
                )
        except (ValueError, PermissionError) as exc:
            await _send_dialog(
                message,
                f"❌ {exc}\n\nПопробуйте ещё раз "
                "или нажмите «Отмена».",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(
                        text="⬅️ К заметке",
                        callback_data=(
                            f"evn:edit:{pending['token']}:"
                        ),
                    ),
                ]]),
                parse_mode=None,
            )
            return
        except sqlite3.Error:
            debug.exception("Не удалось сохранить черновик заметки")
            await _send_dialog(
                message, "❌ Не удалось сохранить заметку. Попробуйте ещё раз.",
                parse_mode=None,
            )
            return
        await show_event_note_detail(
            message,
            actor_id,
            pending["event_sunday"],
            "Черновик сохранён. Изменения вступят в силу после публикации.",
        )

    @admin_router.message(F.text & ~F.text.startswith("/"))
    async def on_service_parameter(message: Message) -> None:
        if (
            not message.from_user
            or message.chat.type != ChatType.PRIVATE
            or not message.text
        ):
            return
        pending = pending_inputs.get(message.from_user.id)
        if pending is None:
            return
        if pending.get("operation") == "service_replace":
            if pending.get("step") == "await_user":
                if message.text == "⬅️ К неделе":
                    await message.answer(
                        "Выбор отменён.", reply_markup=ReplyKeyboardRemove()
                    )
                    await show_replace_week(message, pending)
                elif message.text == "📅 К графику":
                    service_id = pending["service_id"]
                    await clean_and_clear_pending_actor(message.from_user.id)
                    await show_month_graph(message, message.from_user.id, service_id)
                elif message.text == CONTACT_BACK_TEXT:
                    await message.answer(
                        "Выбор отменён.", reply_markup=ReplyKeyboardRemove()
                    )
                    await show_replace_candidates(
                        message, message.from_user.id, pending
                    )
            return
        if pending.get("operation") == "readiness_plan":
            if pending.get("step") == "plan_user":
                if message.text == CONTACT_BACK_TEXT:
                    pending["step"] = "readiness_plan"
                    await reshow_plan_slot(message, message.from_user.id)
                else:
                    await message.answer(
                        "Выберите пользователя кнопкой внизу или нажмите "
                        f"«{CONTACT_BACK_TEXT}».", parse_mode=None,
                    )
            else:
                await message.answer(
                    "Выберите действие кнопками меню.", parse_mode=None
                )
            return
        if pending.get("operation") == "my_notifications_input":
            registry = service_registry()
            if registry is None:
                await _send_dialog(
                    message, "Реестр настроек служений недоступен.",
                    parse_mode=None,
                )
                return
            input_state = pending.get("input", {})
            actor_id = message.from_user.id
            try:
                kind = input_state.get("type")
                service_id = input_state.get("service_id")
                if kind == "rule_text":
                    rule = personal_rule(
                        registry, actor_id, service_id,
                        input_state["schedule_id"],
                    )
                    if rule["origin"] != "user":
                        raise ValueError("Напоминание админа можно только выключить.")
                    registry.update_schedule(rule["id"], body=message.text.strip())
                else:
                    fields = message.text.strip().split()
                    if (
                        len(fields) != 2
                        or not fields[0].isdigit()
                        or not re.fullmatch(
                            r"(?:[01]\d|2[0-3]):[0-5]\d", fields[1]
                        )
                    ):
                        raise ValueError(
                            "Введите количество дней и время HH:MM, например: 2 18:30."
                        )
                    days, time_text = int(fields[0]), fields[1]
                    if kind == "add_time_input":
                        rule = await add_own_personal_rule(
                            registry, actor_id, service_id,
                            input_state["task"], days, time_text, pending,
                        )
                        pending["levels"] = [
                            level for level in pending["levels"]
                            if level["name"] not in {"add_task", "add_time"}
                        ]
                        push_personal_level(pending, {
                            "name": "rule", "service_id": service_id,
                            "schedule_id": rule["id"],
                        })
                    elif kind == "month_time_input":
                        registry.set_month_summary(
                            actor_id, enabled=True, days=days, time=time_text
                        )
                    elif kind == "rule_time":
                        rule = personal_rule(
                            registry, actor_id, service_id,
                            input_state["schedule_id"],
                        )
                        if rule["origin"] != "user":
                            raise ValueError(
                                "Напоминание админа можно только выключить."
                            )
                        registry.update_schedule(rule["id"], days, time_text)
                    else:
                        raise ValueError("Ввод устарел. Откройте /my_notifications.")
                pending["operation"] = "my_notifications"
                pending.pop("input", None)
                await render_current_personal_level(
                    message, message.from_user.id, pending
                )
            except ValueError as exc:
                await _send_dialog(message, f"⚠️ {exc}", parse_mode=None)
            except sqlite3.Error:
                debug.exception("Не удалось сохранить настройки личного уведомления")
                await _send_dialog(
                    message, "❌ Не удалось сохранить настройки. Попробуйте позже.",
                    parse_mode=None,
                )
            except (OSError, RuntimeError):
                debug.exception(
                    "Не удалось сверить задачи с актуальным графиком"
                )
                await _send_dialog(
                    message,
                    "Не удалось сверить задачи с актуальным графиком. "
                    "Попробуйте позже.",
                    parse_mode=None,
                )
            return
        operation = pending["operation"]
        service_id = pending.get("service_id")
        if service_id is None:
            # Меню без текстового ввода (например, «Мои уведомления»).
            await message.answer(
                "Выберите действие кнопками меню.", parse_mode=None
            )
            return
        registry = service_registry()
        if registry is None:
            await clean_previous_menu_by_id(
                context.bot,
                pending.get("menu_chat_id"),
                pending.get("menu_message_id"),
                debug,
            )
            pending_inputs.pop(message.from_user.id, None)
            await _send_dialog(message, "Реестр настроек служений недоступен. Попробуйте позже.",
                parse_mode=None,
            )
            return
        text = message.text.strip()
        if operation == "service_contact" and pending.get("step") == "member_short_name":
            actor_id = message.from_user.id
            card = pending.get("member_card")
            if card is None:
                await _send_dialog(message, "⚠️ Откройте карточку человека заново.")
                return
            try:
                registry.set_service_member_short_name(
                    actor_id, service_id, card["name"], text,
                )
            except ValueError as exc:
                await show_short_name_input(message, service_id, card, f"⚠️ {exc}")
                return
            except PermissionError as exc:
                await clean_and_clear_pending_actor(actor_id)
                await _send_dialog(message, f"❌ {exc}", parse_mode=None)
                return
            except sqlite3.Error:
                debug.exception("Не удалось сохранить короткое имя")
                await show_short_name_input(
                    message, service_id, card, "❌ Ошибка базы данных. Попробуйте позже.",
                )
                return
            await show_member_card(
                message, actor_id, service_id, card["name"], "✅ Короткое имя сохранено",
            )
            return
        if operation == "readiness_text":
            kind = pending.get("kind")
            if kind not in READINESS_TEXTS:
                pending_inputs.pop(message.from_user.id, None)
                return
            try:
                save_readiness_text(
                    registry, message.from_user.id, service_id, kind, text
                )
            except PermissionError as exc:
                pending_inputs.pop(message.from_user.id, None)
                await _send_dialog(
                    message, _status_text(str(exc), "❌"), parse_mode=None
                )
                return
            except ValueError as exc:
                await ask_readiness_text(
                    message, message.from_user.id, service_id, kind,
                    _status_text(str(exc), "⚠️"),
                )
                return
            except sqlite3.Error:
                debug.exception("Не удалось сохранить текст опроса готовности")
                await _send_dialog(
                    message, "❌ Не удалось сохранить текст. Попробуйте позже.",
                    parse_mode=None,
                )
                return
            await clean_and_clear_pending_actor(message.from_user.id)
            await show_readiness_text(
                message, service_id, kind, "✅ Текст сохранён"
            )
            return
        try:
            if operation == "service_tasks":
                if pending.get("step") != "add_task":
                    raise ValueError(
                        "Чтобы добавить задачу, нажмите «➕ Добавить задачу»."
                    )
                registry.add_service_task(message.from_user.id, service_id, text)
                await show_service_tasks(
                    message, message.from_user.id, service_id,
                    f"✅ Задача «{' '.join(text.split())}» добавлена",
                )
                return
            if operation == "service_admin_add":
                if pending.get("step") != "await_user":
                    raise ValueError(
                        "Сначала выберите пользователя или введите "
                        "его Telegram ID."
                    )
                user_id, username = parse_manual_user(text)
                await ask_user_assignment_confirmation(
                    message,
                    message.from_user.id,
                    service_id,
                    operation,
                    user_id,
                    username,
                )
                return
            elif operation == "service_admin_remove":
                registry.remove_service_admin(
                    message.from_user.id, service_id, int(text)
                )
                await refresh_user_commands(int(text))
                response = "Администратор служения снят."
            elif operation == "service_chat":
                if text == CONTACT_BACK_TEXT:
                    for request_id, share in list(pending_shares.items()):
                        if share[0] == message.from_user.id:
                            pending_shares.pop(request_id, None)
                    await clean_and_clear_pending_actor(message.from_user.id)
                    service = service_by_id(registry, service_id)
                    await _send_dialog(
                        message, "↩️ Выбор чата отменён.", parse_mode=None,
                        reply_markup=ReplyKeyboardRemove(),
                    )
                    await _send_dialog(
                        message,
                        f"{service_label(service)}: выберите действие.",
                        reply_markup=service_chat_keyboard(
                            service_id, message.from_user.id
                        ),
                        parse_mode=None,
                    )
                    return
                parts = text.split()
                if len(parts) != 2 or parts[1] not in {"on", "off"}:
                    raise ValueError(
                        "Введите ID чата и on или off, например: "
                        "-1001234567890 on."
                    )
                chat_id = int(parts[0])
                await require_chat_member(message.from_user.id, chat_id)
                if parts[1] == "off":
                    await ask_service_disable(
                        message, message.from_user.id, service_id, group_chat_id=chat_id,
                    )
                    return
                registry.set_group_chat(
                    message.from_user.id,
                    service_id,
                    chat_id,
                    enabled=True,
                )
                if service_id == "assembly":
                    context.group_chat_id = chat_id
                response = "Групповой чат и статус служения сохранены."
            elif operation == "service_contact":
                step = pending.get("step")
                if text == CONTACT_BACK_TEXT and step in {
                    "await_user", "await_member_name",
                }:
                    await return_to_contact_choices(
                        message, message.from_user.id, "↩️ Возврат к выбору человека."
                    )
                    return
                if step == "await_member_name":
                    name = normalize_member_name(text)
                    await offer_contact_user_selection(
                        message, message.from_user.id, service_id, None, name
                    )
                    return
                if step != "await_user":
                    raise ValueError(
                        "Сначала выберите задачу и ответственного кнопками."
                    )
                task = pending.get("active_task")
                contact_name = pending.get("active_contact_name")
                if not contact_name or (
                    not task and pending.get("contact_mode") != "person"
                ):
                    raise ValueError(
                        "Выбор ответственного устарел. Выберите его заново."
                    )
                user_id, username = parse_manual_user(text, username_only=True)
                if user_id is None:
                    # Бот уже видел этот @username — ID известен.
                    user_id = registry.known_telegram_id(username)
                if user_id is None:
                    await ask_username_binding_confirmation(
                        message, message.from_user.id, service_id, username,
                        task, contact_name,
                    )
                    return
                await ask_user_assignment_confirmation(
                    message,
                    message.from_user.id,
                    service_id,
                    operation,
                    user_id,
                    username,
                    task,
                    contact_name,
                )
                return
            elif operation == "service_notify":
                step = pending["step"]
                if step in GROUP_REMINDER_NEW_STEPS:
                    draft = pending.get("group_reminder_draft")
                    if draft is None:
                        raise ValueError(
                            "Ввод устарел. Нажмите «Добавить напоминание» "
                            "заново."
                        )
                    if step == "group_reminder_new_days":
                        draft["days"] = _parse_reminder_days(text)
                        await ask_new_group_reminder_step(
                            message, pending, "group_reminder_new_time"
                        )
                    elif step == "group_reminder_new_time":
                        draft["time"] = _parse_reminder_time(text)
                        await ask_new_group_reminder_step(
                            message, pending, "group_reminder_new_text"
                        )
                    else:
                        if not text:
                            raise ValueError(
                                "Текст группового напоминания не может быть пустым."
                            )
                        validate_notification_template(
                            text,
                            GROUP_TEMPLATE_FIELDS if draft.get("task")
                            else GENERAL_GROUP_TEMPLATE_FIELDS,
                            "Текст группового напоминания",
                        )
                        draft["body"] = text
                        await finish_new_group_reminder(
                            message, message.from_user.id, service_id
                        )
                    return
                if step == "group_reminder_period_input":
                    input_info = pending.get("group_reminder_input")
                    if input_info is None:
                        raise ValueError(
                            "Ввод устарел. Откройте напоминание заново."
                        )
                    active_from, active_until = _parse_period(text)
                    registry.set_group_reminder_period(
                        message.from_user.id, input_info["reminder_id"],
                        active_from, active_until,
                        today=datetime.now(context.timezone).date(),
                    )
                    pending.pop("group_reminder_input", None)
                    refresh = getattr(
                        context, "refresh_service_reminder_jobs", None
                    )
                    if callable(refresh):
                        await refresh()
                    await enter_group_reminder_card(
                        message, message.from_user.id, service_id,
                        input_info["reminder_id"],
                    )
                    return
                if step in {
                    "group_reminder_days",
                    "group_reminder_time",
                    "group_reminder_text",
                }:
                    input_info = pending.get("group_reminder_input")
                    if input_info is None:
                        raise ValueError(
                            "Ввод устарел. Откройте напоминание заново."
                        )
                    reminder = registry.get_group_reminder(
                        input_info["reminder_id"]
                    )
                    if (
                        reminder is None
                        or reminder["service_id"] != service_id
                    ):
                        raise ValueError(
                            "Групповое напоминание больше не существует."
                        )
                    changes = {}
                    if step == "group_reminder_days":
                        changes["days"] = _parse_reminder_days(text)
                    elif step == "group_reminder_time":
                        changes["time"] = _parse_reminder_time(text)
                    else:
                        if not text:
                            raise ValueError(
                                "Текст группового напоминания не может быть пустым."
                            )
                        validate_notification_template(
                            text,
                            (
                                GROUP_TEMPLATE_FIELDS
                                if reminder.get("task")
                                else GENERAL_GROUP_TEMPLATE_FIELDS
                            ),
                            "Текст группового напоминания",
                        )
                        changes["body"] = text
                    try:
                        registry.update_group_reminder(
                            message.from_user.id,
                            input_info["reminder_id"],
                            **changes,
                        )
                    except ValueError as exc:
                        if (
                            "уже задано" not in str(exc)
                            or "days" not in changes
                            and "time" not in changes
                        ):
                            raise
                        updated_days = changes.get(
                            "days", reminder["reminder_days"]
                        )
                        updated_time = changes.get(
                            "time", reminder["reminder_time"]
                        )
                        target = (
                            enabled_group_reminder_at(
                                registry,
                                service_id,
                                updated_days,
                                updated_time,
                                excluding_id=reminder["id"],
                                task=reminder.get("task"),
                                period=reminder,
                            )
                            if reminder["enabled"]
                            else None
                        )
                        if target is None:
                            # Совпадение с выключенным напоминанием (или
                            # изменение выключенного) не даёт двойной отправки.
                            registry.update_group_reminder(
                                message.from_user.id,
                                input_info["reminder_id"],
                                allow_duplicate=True,
                                **changes,
                            )
                        else:
                            pending.pop("group_reminder_input", None)
                            await offer_group_reminder_merge(
                                message,
                                message.from_user.id,
                                service_id,
                                target,
                                days=updated_days,
                                time=updated_time,
                                body=reminder["body"],
                                source_id=reminder["id"],
                                source_slot=(
                                    reminder["reminder_days"],
                                    reminder["reminder_time"],
                                ),
                                period=reminder,
                            )
                            return
                    has_schedule_change = bool(
                        {"days", "time"} & changes.keys()
                    )
                    pending.pop("group_reminder_input", None)
                    pending["step"] = "group_reminder_card"
                    if has_schedule_change:
                        refresh = getattr(
                            context, "refresh_service_reminder_jobs", None
                        )
                        if callable(refresh):
                            await refresh()
                    await enter_group_reminder_card(
                        message,
                        message.from_user.id,
                        service_id,
                        input_info["reminder_id"],
                    )
                    return
                if step == "group_default_text":
                    if not text:
                        raise ValueError(
                            "Шаблон группового уведомления не может быть пустым."
                        )
                    validate_notification_template(
                        text,
                        GENERAL_GROUP_TEMPLATE_FIELDS,
                        "Шаблон группового уведомления",
                    )
                    pending["group_template"] = text
                    pending["step"] = "default_template"
                    await enter_default_template(
                        message, message.from_user.id, service_id
                    )
                    return
                if step == "admin_report_text":
                    if not text:
                        raise ValueError(
                            "Шаблон отчёта админам не может быть пустым."
                        )
                    validate_notification_template(
                        text, ADMIN_REPORT_FIELDS, "Шаблон отчёта админам"
                    )
                    pending["admin_report_template"] = text
                    pending["admin_report_template_edited"] = True
                    await enter_admin_report_template(
                        message, message.from_user.id, service_id
                    )
                    return
                if step == "days":
                    try:
                        days = int(text)
                    except ValueError as exc:
                        raise ValueError(
                            "Введите целое число от 0 до 6."
                        ) from exc
                    if not 0 <= days <= 6:
                        raise ValueError("Число дней должно быть от 0 до 6.")
                    pending["reminder_days"] = days
                    pending["step"] = "menu"
                    await show_notification_settings_menu(
                        message, message.from_user.id, service_id
                    )
                    return
                if step == "reminder_days":
                    try:
                        days = int(text)
                    except ValueError as exc:
                        raise ValueError(
                            "Введите целое число от 0 до 6."
                        ) from exc
                    if not 0 <= days <= 6:
                        raise ValueError("Число дней должно быть от 0 до 6.")
                    reminder = next(
                        (
                            item for item in pending["reminders"]
                            if item.get("id")
                            == pending.get("active_reminder_id")
                            or item.get("draft_id")
                            == pending.get("active_reminder_id")
                        ),
                        None,
                    )
                    if reminder is None:
                        raise ValueError(
                            "Выбранное уведомление устарело. Откройте список заново."
                        )
                    reminder["reminder_days"] = days
                    if pending.get("reminder_operation") == "add":
                        pending["step"] = "reminder_time"
                        await _send_dialog(message, "Введите локальное время отправки в формате HH:MM, "
                            "например 09:00.",
                            reply_markup=notification_edit_controls(
                                service_id, pending["session_id"]
                            ),
                            parse_mode=None,
                        )
                    else:
                        pending.pop("active_reminder_original", None)
                        pending.pop("active_reminder_id", None)
                        pending.pop("reminder_operation", None)
                        pending["step"] = "menu"
                        await show_reminder_rules_menu(
                            message, message.from_user.id, service_id
                        )
                    return
                if step == "time":
                    if (
                        len(text) != 5
                        or text[2] != ":"
                        or not text[:2].isdigit()
                        or not text[3:].isdigit()
                        or not 0 <= int(text[:2]) <= 23
                        or not 0 <= int(text[3:]) <= 59
                    ):
                        raise ValueError(
                            "Укажите время в формате HH:MM, например 17:15."
                        )
                    pending["reminder_time"] = text
                    pending["step"] = "menu"
                    await show_notification_settings_menu(
                        message, message.from_user.id, service_id
                    )
                    return
                if step == "reminder_time":
                    if (
                        len(text) != 5
                        or text[2] != ":"
                        or not text[:2].isdigit()
                        or not text[3:].isdigit()
                        or not 0 <= int(text[:2]) <= 23
                        or not 0 <= int(text[3:]) <= 59
                    ):
                        raise ValueError(
                            "Укажите время в формате HH:MM, например 09:00."
                        )
                    reminder = next(
                        (
                            item for item in pending["reminders"]
                            if item.get("id")
                            == pending.get("active_reminder_id")
                            or item.get("draft_id")
                            == pending.get("active_reminder_id")
                        ),
                        None,
                    )
                    if reminder is None:
                        raise ValueError(
                            "Выбранное уведомление устарело. Откройте список заново."
                        )
                    reminder["reminder_time"] = text
                    reminder["complete"] = True
                    pending.pop("active_reminder_original", None)
                    pending.pop("active_reminder_id", None)
                    pending["reminder_operation"] = None
                    pending["step"] = "menu"
                    await show_reminder_rules_menu(
                        message, message.from_user.id, service_id
                    )
                    return
                if step == "group_template":
                    if not text:
                        raise ValueError("Текст для группы не может быть пустым.")
                    validate_notification_template(
                        text,
                        GENERAL_GROUP_TEMPLATE_FIELDS,
                        "Шаблон группового уведомления",
                    )
                    pending["group_template"] = text
                    pending["step"] = "menu"
                    await show_notification_settings_menu(
                        message, message.from_user.id, service_id
                    )
                    return
                if step == "notify_template":
                    if not text:
                        raise ValueError(
                            "Текст индивидуального напоминания не может быть пустым."
                        )
                    active_task = pending.get("active_task")
                    if not active_task:
                        raise ValueError(
                            "Выбор задачи устарел. Запустите /service_notify заново."
                        )
                    validate_notification_template(
                        text,
                        PERSONAL_TEMPLATE_FIELDS,
                        f"Личный шаблон для задачи «{active_task}»",
                    )
                    pending["personal_templates"][active_task] = text
                    pending.pop("active_task", None)
                    pending["step"] = "menu"
                    await show_notification_settings_menu(
                        message, message.from_user.id, service_id
                    )
                    return
                raise ValueError("Шаг настройки устарел. Запустите команду заново.")
            else:
                await clean_previous_menu_by_id(
                    context.bot,
                    pending.get("menu_chat_id"),
                    pending.get("menu_message_id"),
                    debug,
                )
                pending_inputs.pop(message.from_user.id, None)
                return
        except (PermissionError, ValueError) as exc:
            await _send_dialog(message, _status_text(str(exc), "❌"), parse_mode=None)
            return
        except sqlite3.Error:
            debug.exception("Ошибка сохранения настроек служения")
            await _send_dialog(message, "Не удалось сохранить настройки из-за ошибки базы данных.",
                parse_mode=None,
            )
            return
        except (OSError, RuntimeError):
            debug.exception("Не удалось обновить расписание уведомлений")
            await _send_dialog(
                message,
                "Не удалось обновить расписание уведомлений. "
                "Изменение сохранено; попробуйте позже.",
                parse_mode=None,
            )
            return

        await clean_previous_menu_by_id(
            context.bot,
            pending.get("menu_chat_id"),
            pending.get("menu_message_id"),
            debug,
        )
        pending_inputs.pop(message.from_user.id, None)
        await _send_dialog(
            message,
            _status_text(response, "✅"),
            reply_markup=service_result_keyboard(
                service_id,
                delivery_check=operation == "service_chat"
                and can_check_delivery(message.from_user.id, service_id),
            ),
            parse_mode=None,
        )

    @admin_router.message(Command("service_sync"))
    async def on_service_sync(message: Message) -> None:
        if not message.from_user or message.chat.type != ChatType.PRIVATE:
            return
        await clean_and_clear_pending_actor(message.from_user.id)
        payload = (message.text or "").partition(" ")[2].strip()
        if payload:
            await _send_dialog(message, "Служение нужно выбрать кнопкой. Используйте /service_sync "
                "без параметров.",
                parse_mode=None,
            )
            return
        await request_service_input(message, "service_sync")

    async def execute_test_notification(
        message: Message, service: dict, action: str, actor_id: int
    ) -> None:
        if action == "group":
            group_chat_id = service["group_chat_id"]
            if group_chat_id is None:
                await _send_dialog(message, "Для этого служения не настроен групповой чат.",
                    reply_markup=test_result_keyboard(service["id"]),
                    parse_mode=None,
                )
                return
            try:
                await send_to_service_group(
                    context.bot.send_message,
                    group_chat_id,
                    f"🔧 ТЕСТ службы «{service['name']}»: проверка доставки. "
                    "Обычная рассылка не запускалась.",
                    service,
                    registry=service_registry(),
                    logger=debug,
                    parse_mode=None,
                )
            except TelegramAPIError as exc:
                debug.exception("Тестовая отправка в группу не удалась")
                await _send_dialog(message, f"Не удалось отправить тест в группу: {type(exc).__name__}.",
                    reply_markup=test_result_keyboard(service["id"]),
                    parse_mode=None,
                )
            else:
                await _send_dialog(message, f"Тестовое сообщение службы «{service['name']}» отправлено "
                    "в настроенную группу. Рассылка участникам не запускалась.",
                    reply_markup=test_result_keyboard(service["id"]),
                    parse_mode=None,
                )
            return

        prepare_previews = getattr(
            context, "prepare_service_test_previews", None
        )
        if not callable(prepare_previews):
            await _send_dialog(message, "Предпросмотр сообщений для служений недоступен.",
                reply_markup=test_result_keyboard(service["id"]),
                parse_mode=None,
            )
            return
        try:
            previews = await prepare_previews(service["id"])
        except (
            AttributeError,
            IndexError,
            KeyError,
            OSError,
            PermissionError,
            RuntimeError,
            sqlite3.Error,
            TypeError,
            ValueError,
        ) as exc:
            debug.exception(
                "Не удалось подготовить предпросмотр службы %s",
                service["id"],
            )
            await _send_dialog(message, f"Не удалось подготовить предпросмотр: {type(exc).__name__}.",
                reply_markup=test_result_keyboard(service["id"]),
                parse_mode=None,
            )
            return

        output = [
            f"👁 Предпросмотр службы «{service['name']}» "
            f"на {previews['sunday_date']:%d.%m.%Y}:",
            "",
            previews["group_text"],
        ]
        output.extend(
            f"\nЛичное сообщение для {item['name']} "
            f"(задача «{item['task']}»):\n{item['text']}"
            for item in previews["personal_messages"]
        )
        if previews["issues"]:
            output.extend(("", "Замечания:", *previews["issues"]))
        for chunk in _telegram_text_chunks(plain_text("\n".join(output))):
            try:
                await context.bot.send_message(
                    actor_id, chunk, parse_mode=None
                )
            except TelegramAPIError:
                debug.exception(
                    "Не удалось отправить администратору предпросмотр службы %s",
                    service["id"],
                )
                await _send_dialog(message, "Не удалось доставить администратору полный предпросмотр.",
                    reply_markup=test_result_keyboard(service["id"]),
                    parse_mode=None,
                )
                return
        await _send_dialog(message, "Предпросмотр выбранной службы отправлен вам в личные сообщения. "
            "Сообщения в группу и участникам не отправлялись.",
            reply_markup=test_result_keyboard(service["id"]),
            parse_mode=None,
        )

    @router.message(Command("test_notify"))
    async def on_test_notify(message: Message) -> None:
        if not message.from_user or message.from_user.id != context.admin_id:
            return
        if message.chat.type != ChatType.PRIVATE:
            return
        await open_test_notify(message, message.from_user.id)

    async def open_test_notify(message: Message, actor_id: int) -> None:
        await clean_and_clear_pending_actor(actor_id)
        registry = service_registry()
        if registry is None:
            await _send_dialog(message, "Реестр служений недоступен; нельзя выбрать чат проверки.",
                parse_mode=None,
            )
            return
        try:
            services = registry.list_services()
        except sqlite3.Error:
            debug.exception("Не удалось загрузить служения для теста доставки")
            await _send_dialog(message, "Не удалось прочитать список служений.", parse_mode=None
            )
            return
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=service_label(service),
                        callback_data=f"service:test_notify:{service['id']}",
                    )
                ]
                for service in services
            ]
        )
        await _send_dialog(message, "🧪 Сначала выберите служение, затем — вид проверки.",
            reply_markup=keyboard,
            parse_mode=None,
        )

    @router.callback_query(F.data.startswith("test_action:"))
    async def on_test_notify_action(callback: CallbackQuery) -> None:
        if (
            not callback.from_user
            or not callback.message
            or callback.message.chat.type != ChatType.PRIVATE
        ):
            await _answer_callback(callback,)
            return
        parts = callback.data.split(":", 2)
        if len(parts) != 3:
            await _answer_callback(callback, "⚠️ Кнопка устарела.")
            return
        _, action, service_id = parts
        if not is_service_admin(callback.from_user.id, service_id):
            await _answer_callback(
                callback,
                "❌ Проверка доставки доступна только администратору "
                "этого служения.",
            )
            return
        await clean_previous_menu(callback.message, debug)
        registry = service_registry()
        if registry is None:
            await _answer_callback(callback,"❌ Реестр служений недоступен.")
            return
        try:
            service = service_by_id(registry, service_id)
            if service is None:
                raise ValueError("Служение не найдено.")
            if action not in {"group", "preview"}:
                raise ValueError("Неизвестный вид проверки.")
            await execute_test_notification(
                callback.message, service, action, callback.from_user.id
            )
        except (ValueError, sqlite3.Error) as exc:
            await _send_dialog(callback.message, _status_text(str(exc), "❌"), parse_mode=None
            )
        await _answer_callback(callback,)

    @router.callback_query(F.data.startswith(f"{CONFIRM_PREFIX}:"))
    async def on_confirm(cb: CallbackQuery) -> None:  # noqa: D401
        parts = cb.data.split(":", 2)
        if len(parts) != 3:
            await _answer_callback(cb,"❌ Некорректные данные кнопки")
            return
        _, sunday_iso, role = parts
        state = context.state_cache.state
        participant = (
            state.get("participants", {}).get(role)
            if state and state.get("sunday_date") == sunday_iso
            else None
        )
        if not participant or participant.get("tg_id") != cb.from_user.id:
            await _confirm_locked(cb, context, sunday_iso, role, interaction, debug)
            return

        locks = getattr(context, "confirmation_locks", None)
        if locks is None:
            locks = {}
            context.confirmation_locks = locks
        for key in tuple(locks):
            if key[0] != sunday_iso and not locks[key].locked():
                del locks[key]
        lock = locks.setdefault((sunday_iso, role), asyncio.Lock())
        async with lock:
            await _confirm_locked(cb, context, sunday_iso, role, interaction, debug)

    async def scheduled_name(
        registry: Any, service_id: str, event_date, task: str, telegram_id: int
    ) -> tuple[bool, str | None]:
        """(стоит ли человек в задаче на дату, его имя в графике).

        График с заменами. Без данных графика проверить нельзя — ответ
        принимается, имя берётся из контакта."""
        try:
            names = await task_names_on_date(service_id, event_date, task)
            if not names:
                return True, None
            name = registry.contact_name_among(
                service_id, task, names, telegram_id
            )
            if name:
                return True, name
            # Заменяющий по /change мог быть привязан к задаче под другим
            # именем — его узнаём по Telegram ID в записи замены.
            name = next(
                (
                    item["new_name"]
                    for item in reversed(registry.list_replacements(
                        service_id, event_date.isoformat()
                    ))
                    if item["task"] == task and item["new_name"] in names
                    and item["telegram_id"] == telegram_id
                ),
                None,
            )
            return name is not None, name
        except Exception:  # noqa: BLE001
            debug.exception("Не удалось сверить ответ с графиком")
            return True, None

    @router.callback_query(F.data.startswith("pcf:"))
    async def on_personal_answer(cb: CallbackQuery) -> None:
        """«✅ Принял» / «🚫 Не смогу» в личном напоминании (этап 3.2)."""
        if not cb.from_user:
            await _answer_callback(cb, "❌ Не удалось определить пользователя")
            return
        parts = (cb.data or "").split(":")
        registry = service_registry()
        try:
            _, kind, service_id, raw_date, token = parts
            event_date = datetime.strptime(raw_date, "%Y%m%d").date()
            if kind not in {"c", "d"}:
                raise ValueError(kind)
        except ValueError:
            await _answer_callback(cb, "❌ Некорректные данные кнопки")
            return
        if registry is None:
            await _answer_callback(cb, "❌ Реестр служений недоступен.")
            return
        if event_date < datetime.now(context.timezone).date():
            await _answer_callback(cb, "⚠️ Это напоминание устарело")
            return
        try:
            task = registry.linked_task_by_token(
                service_id, cb.from_user.id, token
            )
        except sqlite3.Error:
            debug.exception("Не удалось найти задачу ответа на напоминание")
            task = None
        if task is None:
            await _answer_callback(cb, "❌ Вы не привязаны к этой задаче")
            return
        assigned, schedule_name = await scheduled_name(
            registry, service_id, event_date, task, cb.from_user.id
        )
        if not assigned:
            await _answer_callback(
                cb, "⚠️ Это напоминание устарело: в графике на эту дату "
                "в задаче уже другой человек."
            )
            return
        status = "confirmed" if kind == "c" else "declined"
        try:
            answers = registry.set_assignment_answer(
                service_id, event_date.isoformat(), task, cb.from_user.id,
                status, name=schedule_name,
            )
            _, name, _ = answers[0]
            changed = any(item[2] for item in answers)
        except PermissionError:
            await _answer_callback(cb, "❌ Вы не привязаны к этой задаче")
            return
        except (ValueError, sqlite3.Error):
            debug.exception("Не удалось сохранить ответ на напоминание")
            await _answer_callback(
                cb, "❌ Не удалось сохранить ответ. Нажмите кнопку повторно."
            )
            return
        service = registry.get_service(service_id)
        admins_told = None
        if changed and status == "declined" and service:
            try:
                admins_told = 0
                for answered_task, answered_name, answer_changed in answers:
                    if answer_changed:
                        admins_told += await notify_admins_declined(
                            context.bot, registry, service, event_date, answered_task,
                            answered_name, context.admin_id, debug,
                        )
            except Exception:  # noqa: BLE001
                debug.exception("Не удалось сообщить админам об отказе")
                admins_told = 0
            if not admins_told:
                debug.warning(
                    "Об отказе %s по служению %s не узнал ни один админ",
                    task, service_id,
                )
        if status == "confirmed":
            note = "✅ Подтверждено"
        elif admins_told:
            note = "🚫 Вы отметили, что не сможете. Админы служения получили сообщение."
        elif admins_told is None:
            note = "🚫 Вы отметили, что не сможете."
        else:
            note = (
                "🚫 Ответ сохранён, но сообщить админам не удалось — "
                "предупредите их сами."
            )
        if status == "declined" and len(answers) > 1:
            note += "\nТакже снята готовность к обязанности: " + ", ".join(
                item[0] for item in answers[1:]
            ) + ". Администратор должен переназначить её внутри смены."
        await _mark_answered_reminder(cb, note, debug)
        if changed:
            interaction.info(
                "Ответ на напоминание: service=%s task=%s tg_id=%s date=%s "
                "status=%s",
                service_id, task, cb.from_user.id, event_date, status,
            )
            if status == "confirmed" and service_id == "assembly" and service:
                # Как прежде у Собрания: подтверждение видно в группе.
                try:
                    await send_to_service_group(
                        context.bot.send_message,
                        service["group_chat_id"],
                        f"🔔 Участник {name} подтвердил задачу «{task}»",
                        service,
                        registry=registry,
                        logger=debug,
                        parse_mode=None,
                    )
                except Exception:  # noqa: BLE001
                    debug.exception("Не удалось отправить апдейт в группу")
            if status == "declined" and service:
                # Поиск замены (этап 3.3).
                on_declined = getattr(context, "on_assignment_declined", None)
                if callable(on_declined):
                    try:
                        for answered_task, answered_name, answer_changed in answers:
                            if answer_changed:
                                await on_declined(
                                    service_id, event_date, answered_task, answered_name,
                                    telegram_id=cb.from_user.id,
                                )
                    except Exception:  # noqa: BLE001
                        debug.exception("Не удалось запустить поиск замены")
            try:
                await refresh_admin_reports(
                    context.bot, registry, service_id, event_date,
                    context.admin_id, debug, TELEGRAM_TEXT_LIMIT,
                )
            except Exception:  # noqa: BLE001
                debug.exception("Не удалось обновить отчёт админам")
        await _answer_callback(
            cb, "✅ Спасибо!" if status == "confirmed" else "Ответ сохранён"
        )

    @router.callback_query(F.data.startswith("scf:"))
    async def on_service_confirm(cb: CallbackQuery) -> None:
        if not cb.from_user:
            await _answer_callback(cb,"❌ Не удалось определить пользователя")
            return
        parts = cb.data.split(":", 2)
        if len(parts) != 3:
            await _answer_callback(cb,"❌ Некорректные данные кнопки")
            return
        _, event_date, participant_key = parts
        if (
            len(event_date) != 10
            or event_date[4] != "-"
            or event_date[7] != "-"
            or not participant_key.startswith("service_")
        ):
            await _answer_callback(cb,"❌ Некорректные данные кнопки")
            return
        locks = getattr(context, "confirmation_locks", None)
        if locks is None:
            locks = {}
            context.confirmation_locks = locks
        lock_key = (event_date, participant_key)
        lock = locks.setdefault(lock_key, asyncio.Lock())
        async with lock:
            await _confirm_locked(
                cb, context, event_date, participant_key, interaction, debug
            )

    dispatcher.include_router(admin_router)
    dispatcher.include_router(router)


async def _mark_answered_reminder(
    cb: CallbackQuery, note: str, debug: logging.Logger
) -> None:
    """Дописывает ответ к личному напоминанию с сохранением оформления и
    убирает кнопки. Если текст уже не помещается — убирает только кнопки."""
    message = cb.message
    if message is None:
        return
    original = getattr(message, "html_text", None) or html.escape(
        getattr(message, "text", None) or ""
    )
    if original:
        text = f"{original}\n\n{html.escape(note)}"
        if len(text) <= TELEGRAM_TEXT_LIMIT:
            try:
                await message.edit_text(text, parse_mode="HTML")
                return
            except Exception:  # noqa: BLE001
                debug.exception("Не удалось отредактировать личное напоминание")
    try:
        await message.edit_reply_markup(reply_markup=None)
    except Exception:  # noqa: BLE001
        debug.exception("Не удалось убрать кнопки личного напоминания")


async def _confirm_locked(
    cb: CallbackQuery,
    context: Any,
    sunday_iso: str,
    role: str,
    interaction: logging.Logger,
    debug: logging.Logger,
) -> None:
    state = context.state_cache.state
    if not state or state.get("sunday_date") != sunday_iso:
        await _answer_callback(cb,"⚠️ Это уведомление устарело")
        return

    participant = state.get("participants", {}).get(role)
    if not participant or participant.get("tg_id") != cb.from_user.id:
        await _answer_callback(cb,"❌ Вы не назначены на эту роль")
        return

    first_confirmation = not participant.get("confirmed")
    if first_confirmation:
        participant["confirmed"] = True
    needs_confirmation_persist = (
        first_confirmation
        or participant.get("confirmation_persisted") is False
    )
    newly_persisted = needs_confirmation_persist
    if needs_confirmation_persist:
        participant.pop("confirmation_persisted", None)
        confirmation_persisted = await _persist_confirmation(context, debug)
        if not confirmation_persisted:
            participant["confirmation_persisted"] = False
            await _answer_callback(cb,
                "❌ Не удалось сохранить подтверждение. "
                "Нажмите кнопку повторно."
            )
            return

    if participant.get("service_id") and participant.get("task"):
        # Кнопка прежнего формата: ответ попадает и в общий учёт (этап 3.2).
        # Запись идемпотентна, поэтому повторное нажатие повторяет её после
        # сбоя.
        registry = getattr(context, "service_registry", None)
        try:
            if registry is not None:
                _, answer_changed = registry.set_confirmation(
                    participant["service_id"], sunday_iso, participant["task"],
                    cb.from_user.id, "confirmed",
                )
                if answer_changed:
                    await refresh_admin_reports(
                        context.bot, registry, participant["service_id"],
                        date.fromisoformat(sunday_iso), context.admin_id,
                        debug, TELEGRAM_TEXT_LIMIT,
                    )
        except Exception:  # noqa: BLE001
            debug.exception("Не удалось записать подтверждение в реестр")
    if newly_persisted:
        await _mark_answered_reminder(cb, "✅ Подтверждено", debug)

    group_update_blocked_reason = None
    active_group_chat_id = None
    registry = getattr(context, "service_registry", None)
    if not participant.get("group_update_sent") and registry is not None:
        try:
            assembly = registry.get_service("assembly")
        except sqlite3.Error:
            debug.exception(
                "Не удалось проверить состояние служения «Собрание»"
            )
            group_update_blocked_reason = (
                "не удалось проверить состояние служения"
            )
        else:
            if assembly is None or not assembly["enabled"]:
                group_update_blocked_reason = "служение отключено"
            elif assembly.get("group_chat_id") is None:
                group_update_blocked_reason = "групповой чат не настроен"
            else:
                active_group_chat_id = assembly.get("group_chat_id")

    if not participant.get("group_update_sent") and not group_update_blocked_reason:
        group_chat_id = (
            active_group_chat_id
            if active_group_chat_id is not None
            else participant.get(
                "group_chat_id",
                state.get("group_chat_id", context.group_chat_id),
            )
        )
        participant_service = None
        if registry is not None:
            try:
                participant_service = registry.get_service(
                    participant.get("service_id") or "assembly"
                )
            except Exception:  # noqa: BLE001
                debug.exception("Не удалось прочитать тему чата служения")
        if (
            participant_service is None
            or participant_service.get("group_chat_id") != group_chat_id
        ):
            participant_service = None
        try:
            await send_to_service_group(
                context.bot.send_message,
                group_chat_id,
                f"🔔 Участник {participant['name']} подтвердил "
                f"{'задачу' if participant.get('task') else 'роль'} "
                f"«{participant.get('task', role)}»",
                participant_service,
                registry=registry,
                logger=debug,
                parse_mode=None,
            )
        except Exception:  # noqa: BLE001
            debug.exception("Не удалось отправить апдейт в группу")
        else:
            participant["group_chat_id"] = group_chat_id
            participant["group_update_sent"] = True
            participant["group_update_persisted"] = await _persist_confirmation(
                context, debug
            )
    elif not participant.get("group_update_persisted", True):
        participant["group_update_persisted"] = await _persist_confirmation(
            context, debug
        )

    interaction.info(
        "Подтверждение: role=%s tg_id=%s username=@%s name=%s",
        role, cb.from_user.id, cb.from_user.username, participant["name"],
    )
    if participant.get("group_update_sent") and participant.get(
        "group_update_persisted", True
    ):
        await _answer_callback(cb,
            "✅ Участие подтверждено"
            if newly_persisted
            else "ℹ️ Вы уже подтвердили участие"
        )
    elif participant.get("group_update_sent"):
        await _answer_callback(cb,
            "⚠️ Уведомление отправлено, но состояние не удалось сохранить. "
            "Нажмите кнопку повторно для повтора сохранения."
        )
    elif group_update_blocked_reason:
        await _answer_callback(cb,
            "⚠️ Подтверждение сохранено, но уведомление в группу не отправлено: "
            f"{group_update_blocked_reason}."
        )
    else:
        await _answer_callback(cb,
            "⚠️ Подтверждение сохранено, но уведомление не отправлено. "
            "Нажмите кнопку повторно для повтора."
        )
