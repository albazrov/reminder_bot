"""Persistent configuration for service access and participant contacts."""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import sqlite3
import stat
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from string import Formatter
from dataclasses import replace
from typing import Callable

from .presentation import is_replaced
from .scheduler import periods_overlap, reminder_day_offset
from .secure_fs import open_private_directory
from .services import (
    SERVICES,
    ServiceDefinition,
    ServiceTask,
    normalized_name,
    split_day_key,
)


GROUP_TEMPLATE_FIELDS = frozenset(
    {
        "service_name",
        "sunday_date",
        "task_assignments",
        "informational_notes",
        "task",
        "task_responsibles",
    }
)
# Поля задачи есть только у напоминания задачи (этап 3.1): в общем
# напоминании и шаблоне служения они были бы пустыми.
GENERAL_GROUP_TEMPLATE_FIELDS = GROUP_TEMPLATE_FIELDS - {"task", "task_responsibles"}
PERSONAL_TEMPLATE_FIELDS = frozenset(
    {"name", "task", "service_name", "sunday_date", "telegram_id", "username"}
)
DEFAULT_GROUP_TEMPLATE = (
    "Напоминание о служении «{service_name}» в воскресенье "
    "{sunday_date}:\n{task_assignments}"
)
ADMIN_REPORT_FIELDS = frozenset(
    {
        "service_name",
        "sunday_date",
        "task_assignments",
        "delivery_status",
        "confirmation_status",
    }
)
DEFAULT_ADMIN_REPORT_TEMPLATE = (
    "📋 Отчёт · {service_name} · {sunday_date}\n\n"
    "{task_assignments}\n\n"
    "📨 Личные уведомления:\n{delivery_status}\n\n"
    "✅ Подтверждения:\n{confirmation_status}"
)
# Опрос готовности (этап 5): вступление к опросу в чате служения и личное
# напоминание не ответившим; NULL в настройках — встроенный текст.
READINESS_INTRO_FIELDS = frozenset({"service_name", "month", "deadline", "people"})
READINESS_REMINDER_FIELDS = frozenset({"service_name", "month", "deadline"})
DEFAULT_READINESS_INTRO = (
    "🗳 График {service_name} на {month}\n"
    "Отметь даты, когда можешь служить. Ответ можно менять "
    "до {deadline}.\n"
    "Постараемся составить график, удобный для всех. Спасибо за твое "
    "служение в церкви! ❤️\n"
    "👥 {people}"
)
# Встроенное вступление режима «Наоборот»: отмечают даты, когда НЕ могут.
DEFAULT_READINESS_INTRO_REVERSE = (
    "🗳 График {service_name} на {month}\n"
    "Отметь даты, когда НЕ можешь служить. Ответ можно менять "
    "до {deadline}.\n"
    "Постараемся составить график, удобный для всех. Спасибо за твое "
    "служение в церкви! ❤️\n"
    "👥 {people}"
)
READINESS_CLOSED_FIELDS = frozenset({"service_name", "month"})
# Вопрос Telegram-опроса над вариантами (до 300 символов).
READINESS_QUESTION_FIELDS = frozenset({"service_name", "month", "month_in"})
DEFAULT_READINESS_QUESTION = "Когда можешь служить в {month_in}?"
DEFAULT_READINESS_QUESTION_REVERSE = "Когда НЕ можешь служить в {month_in}?"
DEFAULT_READINESS_CLOSED = (
    "🗳 Опрос «График {service_name} на {month}» завершён. Спасибо всем, "
    "кто ответил! ❤️ График появится вскоре."
)
DEFAULT_READINESS_REMINDER = (
    "🗳 {service_name} · график на {month}\n"
    "Ты ещё не ответил(а) на опрос по служению на {month}. Опрос в чате "
    "служения открыт до {deadline}."
)
# Общее напоминание в чат служения (ответом на опрос), без имён.
DEFAULT_READINESS_CHAT_REMINDER = (
    "🔔 {service_name}: опрос о графике на {month} открыт до {deadline}.\n"
    "Кто ещё не отметил даты — проголосуй, пожалуйста, в опросе "
    "выше 🙏"
)
# Заголовки графика в чате служения: при первой публикации и при
# обновлении — каждый своим текстом (слова не «зашиты»).
PLAN_TITLE_FIELDS = frozenset({"service_name", "month"})
DEFAULT_PLAN_TITLE = "📅 График {service_name} на {month} утверждён"
DEFAULT_PLAN_UPDATE_TITLE = "📅 График {service_name} на {month} обновлён"
# Короткие фразы, которые видят участники (раздел «📝 Тексты» → «🔤 Фразы»):
# имя → (встроенный текст, поля, лимит длины, название).
READINESS_PHRASES = {
    "changes": ("Изменения:", frozenset(), 100,
                "Заголовок списка изменений в обновлённом графике"),
    "empty": ("—", frozenset(), 50, "Задача без людей в графике"),
    "nobody": ("никого", frozenset(), 50,
               "«Никого» в строке изменения («звук: Анна → никого»)"),
    "usual_day": ("день как обычно", frozenset(), 100,
                  "Отменён разовый день человека (в изменениях)"),
    "days_changed": ("• Изменены дни служения", frozenset(), 200,
                     "Строка изменений, когда изменилось окно дней"),
    "cant": ("❌ Не смогу в этом месяце", frozenset(), 100,
             "Последний вариант в опросе"),
    "own_days": ("📅 Свой день:", frozenset(), 100,
                 "Начало сноски под текстом опроса"),
    "free_days": (
        "📣 Ещё не заняты дни: {days}. Пересмотрите, пожалуйста, свой "
        "выбор — может быть, получится в один из них.",
        frozenset({"days"}), 300,
        "Напоминание в чат о незанятых днях (ответили ⅔)",
    ),
    "free_days_vote": ("Кто ещё не отметился — отметьтесь в опросе.",
                       frozenset(), 200,
                       "Добавка к нему, если есть не ответившие"),
}
# Шаблон по умолчанию для напоминания одной задачи (этап 3.1).
DEFAULT_TASK_TEMPLATE = (
    "Напоминание о служении «{service_name}» в воскресенье {sunday_date}.\n"
    "{task}: {task_responsibles}"
)
_UNSET = object()
# Напоминание служения и свой день его задачи (task_day: спевка — вс −4;
# NULL — отсчёт от воскресенья).
_GROUP_REMINDER_COLUMNS = (
    "g.id, g.service_id, g.body, g.reminder_days, g.reminder_time, "
    "g.enabled, g.created_at, g.task, g.active_from, g.active_until, "
    "t.day_offset AS task_day"
)
_GROUP_REMINDER_SOURCE = (
    "group_reminders g LEFT JOIN service_tasks t "
    "ON t.service_id = g.service_id AND t.name = g.task"
)
# Своих личных напоминаний человека в одном служении.
USER_REMINDERS_PER_SERVICE = 10
USER_REMINDER_BODY_LIMIT = 3000


def task_token(task: str) -> str:
    """Короткий устойчивый идентификатор задачи для callback_data.

    Позиции задач перенумеровываются, а название может не поместиться в
    64 байта, поэтому в кнопку кладём хэш названия.
    """
    return hashlib.sha1(task.encode("utf-8")).hexdigest()[:10]


def validate_notification_template(
    template: str,
    allowed_fields: frozenset[str],
    label: str,
) -> None:
    allowed = ", ".join(sorted(f"{{{field}}}" for field in allowed_fields))
    if "\x00" in template:
        raise ValueError(f"{label}: текст содержит недопустимые символы.")
    try:
        for _, field_name, format_spec, conversion in Formatter().parse(template):
            if field_name is None:
                continue
            if field_name not in allowed_fields:
                raise ValueError(
                    f"{label}: поле {{{field_name}}} не поддерживается. "
                    f"Доступны: {allowed}."
                )
            if format_spec or conversion:
                raise ValueError(
                    f"{label}: для шаблонов разрешены только простые "
                    f"поля без форматирования. Доступны: {allowed}."
                )
    except ValueError as exc:
        if str(exc).startswith(f"{label}:"):
            raise
        raise ValueError(f"{label}: некорректный синтаксис шаблона.") from exc


def plan_target_thread(plan: dict) -> int | None:
    """Выбранная тема публикации графика; записи без неё — фактическая."""
    target = plan.get("target_thread_id")
    return target if target is not None else plan.get("thread_id")


class ServiceRegistry:
    def __init__(
        self, path: Path, on_change: Callable[[], None] | None = None
    ) -> None:
        self.path = path
        self.on_change = on_change

    def initialize(self, super_admin_id: int, assembly_group_chat_id: int) -> None:
        if super_admin_id <= 0:
            raise ValueError("super_admin_id должен быть положительным Telegram ID")
        if assembly_group_chat_id == 0:
            raise ValueError("ID группового чата не может быть равен нулю")

        directory_fd = open_private_directory(self.path.parent, create=True)
        os.close(directory_fd)
        self._ensure_private_database_file()
        with self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS services (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    aliases_json TEXT NOT NULL,
                    assignment_layout TEXT NOT NULL
                        CHECK (assignment_layout IN ('pairs', 'single')),
                    group_chat_id INTEGER,
                    enabled INTEGER NOT NULL DEFAULT 0
                        CHECK (enabled IN (0, 1)),
                    reminder_days INTEGER NOT NULL DEFAULT 1
                        CHECK (reminder_days >= 0),
                    reminder_time TEXT NOT NULL DEFAULT '18:00',
                    group_template TEXT,
                    personal_templates_json TEXT
                );

                CREATE TABLE IF NOT EXISTS super_admins (
                    telegram_id INTEGER PRIMARY KEY CHECK (telegram_id > 0),
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS service_admins (
                    service_id TEXT NOT NULL REFERENCES services(id) ON DELETE CASCADE,
                    telegram_id INTEGER NOT NULL CHECK (telegram_id > 0),
                    username TEXT NOT NULL DEFAULT '',
                    created_by INTEGER NOT NULL CHECK (created_by > 0),
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (service_id, telegram_id)
                );

                CREATE TABLE IF NOT EXISTS responsible_contacts (
                    service_id TEXT NOT NULL REFERENCES services(id) ON DELETE CASCADE,
                    task TEXT NOT NULL,
                    name TEXT NOT NULL,
                    telegram_id INTEGER NOT NULL CHECK (telegram_id > 0),
                    username TEXT NOT NULL DEFAULT '',
                    updated_by INTEGER NOT NULL CHECK (updated_by > 0),
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (service_id, task, telegram_id)
                );

                CREATE TABLE IF NOT EXISTS user_notification_prefs (
                    service_id TEXT NOT NULL REFERENCES services(id)
                        ON DELETE CASCADE,
                    actor_id INTEGER NOT NULL CHECK (actor_id > 0),
                    enabled INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0, 1)),
                    PRIMARY KEY (actor_id, service_id)
                );

                CREATE TABLE IF NOT EXISTS user_reminder_texts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    actor_id INTEGER NOT NULL CHECK (actor_id > 0),
                    service_id TEXT NOT NULL REFERENCES services(id)
                        ON DELETE CASCADE,
                    task TEXT NOT NULL,
                    body TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE (actor_id, service_id, task)
                );

                CREATE TABLE IF NOT EXISTS user_reminder_schedules (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    text_id INTEGER NOT NULL REFERENCES user_reminder_texts(id)
                        ON DELETE CASCADE,
                    reminder_days INTEGER NOT NULL
                        CHECK (reminder_days BETWEEN 0 AND 6),
                    reminder_time TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1
                        CHECK (enabled IN (0, 1))
                );

                -- Сводка на месяц (этап 4.1): личная, перед первым
                -- воскресеньем месяца; по умолчанию выключена.
                CREATE TABLE IF NOT EXISTS user_month_summaries (
                    telegram_id INTEGER PRIMARY KEY CHECK (telegram_id > 0),
                    enabled INTEGER NOT NULL DEFAULT 0
                        CHECK (enabled IN (0, 1)),
                    reminder_days INTEGER NOT NULL DEFAULT 1
                        CHECK (reminder_days BETWEEN 0 AND 6),
                    reminder_time TEXT NOT NULL DEFAULT '18:00',
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS month_summary_deliveries (
                    telegram_id INTEGER NOT NULL,
                    month TEXT NOT NULL,
                    delivered_at TEXT NOT NULL,
                    PRIMARY KEY (telegram_id, month)
                );

                -- Опрос готовности (этап 5): настройки служения, опрос
                -- на месяц (один Telegram-опрос по датам), ответы.
                CREATE TABLE IF NOT EXISTS readiness_settings (
                    service_id TEXT PRIMARY KEY REFERENCES services(id)
                        ON DELETE CASCADE,
                    enabled INTEGER NOT NULL DEFAULT 0
                        CHECK (enabled IN (0, 1)),
                    start_time TEXT NOT NULL DEFAULT '10:00',
                    duration_days INTEGER NOT NULL DEFAULT 3
                        CHECK (duration_days BETWEEN 1 AND 6),
                    deadline_time TEXT NOT NULL DEFAULT '20:00',
                    remind_days INTEGER NOT NULL DEFAULT 1
                        CHECK (remind_days BETWEEN 0 AND 5),
                    enabled_at TEXT,
                    intro_template TEXT,
                    reminder_template TEXT,
                    closed_template TEXT,
                    chat_reminder_template TEXT,
                    question_template TEXT,
                    plan_title_template TEXT,
                    plan_update_template TEXT,
                    -- Свои короткие фразы ({имя: текст}), см. READINESS_PHRASES.
                    phrases_json TEXT NOT NULL DEFAULT '{}',
                    -- Лимит служений человека в месяц (для всех): минимум и
                    -- максимум (NULL — без ограничения).
                    month_min INTEGER NOT NULL DEFAULT 0,
                    month_max INTEGER,
                    -- «Нужно людей» служения без задач (оно — одна задача).
                    people_needed INTEGER NOT NULL DEFAULT 1,
                    people_max INTEGER,
                    -- Окно дней служения от воскресенья (Уборка: −1…+1).
                    day_from INTEGER NOT NULL DEFAULT 0,
                    day_to INTEGER NOT NULL DEFAULT 0,
                    reverse_poll INTEGER NOT NULL DEFAULT 0
                        CHECK (reverse_poll IN (0, 1)),
                    updated_at TEXT NOT NULL
                );

                -- Подсказка нижней клавиатуры навигации: ID сообщений
                -- переживают перезапуск бота, старая подсказка удаляется.
                CREATE TABLE IF NOT EXISTS nav_hints (
                    telegram_id INTEGER PRIMARY KEY,
                    message_ids_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS readiness_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    service_id TEXT NOT NULL REFERENCES services(id)
                        ON DELETE CASCADE,
                    month TEXT NOT NULL,
                    status TEXT NOT NULL
                        CHECK (status IN ('open', 'closed', 'skipped')),
                    dates_json TEXT NOT NULL DEFAULT '[]',
                    options_json TEXT NOT NULL DEFAULT '[]',
                    reverse_poll INTEGER NOT NULL DEFAULT 0
                        CHECK (reverse_poll IN (0, 1)),
                    poll_id TEXT UNIQUE,
                    chat_id INTEGER,
                    message_id INTEGER,
                    intro_message_id INTEGER,
                    deadline TEXT,
                    created_at TEXT NOT NULL,
                    closed_at TEXT,
                    poll_stopped INTEGER NOT NULL DEFAULT 0,
                    result_sent INTEGER NOT NULL DEFAULT 0,
                    closed_announced INTEGER NOT NULL DEFAULT 0,
                    UNIQUE (service_id, month)
                );

                -- График месяца по итогам опроса (этап 5.4–5.5): рабочая
                -- копия (slots) и утверждённая (approved — опубликована
                -- в чате). Отличаются — правки ещё не опубликованы.
                CREATE TABLE IF NOT EXISTS schedule_plans (
                    service_id TEXT NOT NULL REFERENCES services(id)
                        ON DELETE CASCADE,
                    month TEXT NOT NULL,
                    run_id INTEGER,
                    -- poll — по итогам опроса, excel — загружен из файла.
                    source TEXT NOT NULL DEFAULT 'poll'
                        CHECK (source IN ('poll', 'excel')),
                    dates_json TEXT NOT NULL,
                    tasks_json TEXT NOT NULL,
                    slots_json TEXT NOT NULL,
                    last_served_json TEXT NOT NULL DEFAULT '{}',
                    note TEXT,
                    edited INTEGER NOT NULL DEFAULT 0,
                    approved_json TEXT,
                    approved_by INTEGER,
                    approved_at TEXT,
                    chat_id INTEGER,
                    message_id INTEGER,
                    -- После утверждения сменились дни служения.
                    republish INTEGER NOT NULL DEFAULT 0,
                    -- День на дату для человека: дата → ключ → сдвиг от вс;
                    -- approved_days_json — как опубликовано.
                    days_json TEXT NOT NULL DEFAULT '{}',
                    approved_days_json TEXT,
                    -- Фактический день служения каждого в опубликованном
                    -- графике (дата → ключ → сдвиг): для интервалов следующего
                    -- месяца, даже если свой день потом изменят.
                    served_offsets_json TEXT,
                    -- Задачи (с днями) опубликованной копии: напоминания
                    -- по утверждённому, а не по правкам рабочей копии.
                    approved_tasks_json TEXT,
                    -- Фактический сдвиг каждого назначения на момент публикации:
                    -- дата → задача → человек → сдвиг от воскресенья.
                    approved_slot_days_json TEXT,
                    -- Закреплённая публикация в чате: id всех её частей
                    -- (правится при обновлении) и график первой публикации
                    -- ({"slots", "days"}) — изменения под графиком от него.
                    message_ids_json TEXT,
                    baseline_json TEXT,
                    -- Тема чата публикации: где сообщение на самом деле
                    -- (закрытая тема — в общей) и какую выбрали (/here):
                    -- выбранная сменилась — новая публикация.
                    thread_id INTEGER,
                    target_thread_id INTEGER,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (service_id, month)
                );

                -- Личные напоминания об опросе — отметка на человека после
                -- доставки: временный сбой повторяется следующей минутой.
                CREATE TABLE IF NOT EXISTS readiness_reminders (
                    run_id INTEGER NOT NULL REFERENCES readiness_runs(id)
                        ON DELETE CASCADE,
                    telegram_id INTEGER NOT NULL,
                    sent_at TEXT NOT NULL,
                    PRIMARY KEY (run_id, telegram_id)
                );

                CREATE TABLE IF NOT EXISTS readiness_answers (
                    run_id INTEGER NOT NULL REFERENCES readiness_runs(id)
                        ON DELETE CASCADE,
                    telegram_id INTEGER NOT NULL CHECK (telegram_id > 0),
                    username TEXT NOT NULL DEFAULT '',
                    full_name TEXT NOT NULL DEFAULT '',
                    dates_json TEXT NOT NULL,
                    answered_at TEXT NOT NULL,
                    PRIMARY KEY (run_id, telegram_id)
                );

                -- Разовые уведомления опроса: превью админам, напоминание.
                CREATE TABLE IF NOT EXISTS readiness_notices (
                    service_id TEXT NOT NULL,
                    month TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    sent_at TEXT NOT NULL,
                    PRIMARY KEY (service_id, month, kind)
                );

                CREATE TABLE IF NOT EXISTS user_reminder_deliveries (
                    schedule_id INTEGER NOT NULL
                        REFERENCES user_reminder_schedules(id) ON DELETE CASCADE,
                    event_date TEXT NOT NULL,
                    delivered_at TEXT NOT NULL,
                    PRIMARY KEY (schedule_id, event_date)
                );

                CREATE TABLE IF NOT EXISTS configuration_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    actor_id INTEGER NOT NULL CHECK (actor_id > 0),
                    action TEXT NOT NULL,
                    service_id TEXT,
                    details_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS runtime_counters (
                    name TEXT PRIMARY KEY,
                    next_value INTEGER NOT NULL
                        CHECK (next_value >= 0 AND next_value <= 2147483647)
                );

                -- Deprecated: retained for migration and rollback compatibility.
                CREATE TABLE IF NOT EXISTS service_reminders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    service_id TEXT NOT NULL REFERENCES services(id)
                        ON DELETE CASCADE,
                    reminder_days INTEGER NOT NULL CHECK (reminder_days >= 0),
                    reminder_time TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS service_reminder_deliveries (
                    service_id TEXT NOT NULL,
                    reminder_id INTEGER NOT NULL,
                    event_date TEXT NOT NULL,
                    recipient_key TEXT NOT NULL,
                    delivered_at TEXT NOT NULL,
                    PRIMARY KEY (
                        service_id, reminder_id, event_date, recipient_key
                    )
                );

                CREATE TABLE IF NOT EXISTS service_reminder_migrations (
                    name TEXT PRIMARY KEY
                );

                CREATE TABLE IF NOT EXISTS group_reminders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    service_id TEXT NOT NULL REFERENCES services(id)
                        ON DELETE CASCADE,
                    body TEXT,
                    reminder_days INTEGER NOT NULL
                        CHECK (reminder_days >= 0),
                    reminder_time TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1
                        CHECK (enabled IN (0, 1)),
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE INDEX IF NOT EXISTS idx_group_reminders_service
                    ON group_reminders(service_id);

                CREATE TABLE IF NOT EXISTS group_reminder_deliveries (
                    reminder_id INTEGER NOT NULL
                        REFERENCES group_reminders(id) ON DELETE CASCADE,
                    event_date TEXT NOT NULL,
                    recipient_key TEXT NOT NULL,
                    delivered_at TEXT NOT NULL,
                    PRIMARY KEY (reminder_id, event_date, recipient_key)
                );

                CREATE TABLE IF NOT EXISTS group_reminder_migrations (
                    name TEXT PRIMARY KEY
                );

                CREATE TABLE IF NOT EXISTS service_tasks (
                    service_id TEXT NOT NULL REFERENCES services(id)
                        ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    position INTEGER NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1
                        CHECK (enabled IN (0, 1)),
                    aliases_json TEXT NOT NULL DEFAULT '[]',
                    source TEXT NOT NULL
                        CHECK (source IN ('catalog', 'admin')),
                    people_needed INTEGER NOT NULL DEFAULT 1
                        CHECK (people_needed BETWEEN 1 AND 5),
                    people_max INTEGER,
                    -- Свой день задачи: сдвиг от воскресенья (спевка — −4).
                    day_offset INTEGER,
                    -- «🔗 В одну неделю»: отмеченные задачи один человек
                    -- берёт вместе (звук + спевка).
                    same_week INTEGER NOT NULL DEFAULT 0
                        CHECK (same_week IN (0, 1)),
                    PRIMARY KEY (service_id, name)
                );

                CREATE TABLE IF NOT EXISTS catalog_migrations (
                    name TEXT PRIMARY KEY
                );

                CREATE TABLE IF NOT EXISTS service_members (
                    service_id TEXT NOT NULL REFERENCES services(id)
                        ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    telegram_id INTEGER
                        CHECK (telegram_id IS NULL OR telegram_id > 0),
                    username TEXT NOT NULL DEFAULT '',
                    tasks_json TEXT NOT NULL DEFAULT '[]',
                    in_list INTEGER NOT NULL DEFAULT 0
                        CHECK (in_list IN (0, 1)),
                    source TEXT NOT NULL
                        CHECK (source IN ('excel', 'manual', 'legacy')),
                    -- Задачи заданы в боте: «Списки» их не перезаписывают.
                    tasks_manual INTEGER NOT NULL DEFAULT 0,
                    -- Личный день служения: сдвиг от воскресенья (чт = −3).
                    day_offset INTEGER,
                    -- Личный лимит служений в месяц (главнее лимита
                    -- служения): month_min NULL — как у служения.
                    month_min INTEGER,
                    month_max INTEGER,
                    -- Задачи «в одну неделю» (звук + спевка): JSON-список;
                    -- NULL — как у служения (задачи с отметкой «🔗»).
                    week_tasks_json TEXT,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (service_id, name)
                );

                -- Убранные из состава в боте: «Списки» Excel их не
                -- возвращают, пока человека не добавят в боте снова.
                CREATE TABLE IF NOT EXISTS service_member_exclusions (
                    service_id TEXT NOT NULL REFERENCES services(id)
                        ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (service_id, name)
                );

                -- Какой Telegram ID носил @username, когда бот его видел
                -- (сообщение, нажатие, ответ на опрос): username может
                -- перейти к другому человеку, правки админа — не наблюдение.
                CREATE TABLE IF NOT EXISTS seen_usernames (
                    username TEXT PRIMARY KEY,
                    telegram_id INTEGER NOT NULL CHECK (telegram_id > 0),
                    seen_at TEXT NOT NULL
                );

                -- Привязка только по @username: Telegram ID станет известен,
                -- когда человек напишет боту (нажмёт кнопку, ответит на
                -- опрос). task = '' — во всех задачах человека (карточка).
                CREATE TABLE IF NOT EXISTS username_bindings (
                    service_id TEXT NOT NULL REFERENCES services(id)
                        ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    task TEXT NOT NULL DEFAULT '',
                    username TEXT NOT NULL,
                    tasks_json TEXT NOT NULL DEFAULT '[]',
                    actor_id INTEGER NOT NULL CHECK (actor_id > 0),
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (service_id, name, task)
                );

                -- Ответы на личные напоминания (этап 3.2): «Принял» или
                -- «Не смогу» по задаче на дату события.
                CREATE TABLE IF NOT EXISTS service_confirmations (
                    service_id TEXT NOT NULL REFERENCES services(id)
                        ON DELETE CASCADE,
                    event_date TEXT NOT NULL,
                    task TEXT NOT NULL,
                    telegram_id INTEGER NOT NULL CHECK (telegram_id > 0),
                    name TEXT NOT NULL,
                    status TEXT NOT NULL
                        CHECK (status IN ('confirmed', 'declined')),
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (service_id, event_date, task, telegram_id)
                );

                -- Отправленные отчёты админам служения: редактируются при
                -- подтверждениях; assignments_json — назначения на дату.
                CREATE TABLE IF NOT EXISTS admin_report_messages (
                    service_id TEXT NOT NULL REFERENCES services(id)
                        ON DELETE CASCADE,
                    event_date TEXT NOT NULL,
                    admin_id INTEGER NOT NULL CHECK (admin_id > 0),
                    message_id INTEGER NOT NULL,
                    assignments_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (service_id, event_date, admin_id)
                );

                -- Замена ответственного на одну дату (этап 3.3); главнее
                -- графика. old_name — кого заменили (из графика или прошлая
                -- замена), new_name — кто вместо него.
                CREATE TABLE IF NOT EXISTS service_replacements (
                    service_id TEXT NOT NULL REFERENCES services(id)
                        ON DELETE CASCADE,
                    event_date TEXT NOT NULL,
                    task TEXT NOT NULL,
                    old_name TEXT NOT NULL,
                    new_name TEXT NOT NULL,
                    telegram_id INTEGER
                        CHECK (telegram_id IS NULL OR telegram_id > 0),
                    username TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL CHECK (source IN ('poll', 'admin')),
                    created_by INTEGER,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (service_id, event_date, task, old_name)
                );

                -- Опросы «Кто сможет…» в чате служения (этап 3.3).
                CREATE TABLE IF NOT EXISTS replacement_polls (
                    poll_id TEXT PRIMARY KEY,
                    service_id TEXT NOT NULL REFERENCES services(id)
                        ON DELETE CASCADE,
                    event_date TEXT NOT NULL,
                    task TEXT NOT NULL,
                    old_name TEXT NOT NULL,
                    old_telegram_id INTEGER,
                    chat_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL,
                    notice_message_id INTEGER,
                    deadline TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open'
                        CHECK (status IN ('open', 'closed')),
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS replacement_poll_answers (
                    poll_id TEXT NOT NULL REFERENCES replacement_polls(poll_id)
                        ON DELETE CASCADE,
                    telegram_id INTEGER NOT NULL CHECK (telegram_id > 0),
                    username TEXT NOT NULL DEFAULT '',
                    full_name TEXT NOT NULL DEFAULT '',
                    can INTEGER NOT NULL CHECK (can IN (0, 1)),
                    answered_at TEXT NOT NULL,
                    PRIMARY KEY (poll_id, telegram_id)
                );

                CREATE TABLE IF NOT EXISTS group_reminder_legacy_ids (
                    legacy_id INTEGER PRIMARY KEY,
                    reminder_id INTEGER NOT NULL
                        REFERENCES group_reminders(id) ON DELETE CASCADE
                );
                """
            )
            reminder_indexes = connection.execute(
                "PRAGMA index_list(service_reminders)"
            ).fetchall()
            if any(row["unique"] for row in reminder_indexes):
                connection.executescript(
                    """
                    BEGIN IMMEDIATE;
                    CREATE TABLE service_reminders_v2 (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        service_id TEXT NOT NULL REFERENCES services(id)
                            ON DELETE CASCADE,
                        reminder_days INTEGER NOT NULL
                            CHECK (reminder_days >= 0),
                        reminder_time TEXT NOT NULL
                    );
                    INSERT INTO service_reminders_v2 (
                        id, service_id, reminder_days, reminder_time
                    )
                    SELECT id, service_id, reminder_days, reminder_time
                    FROM service_reminders;
                    DROP TABLE service_reminders;
                    ALTER TABLE service_reminders_v2
                        RENAME TO service_reminders;
                    """
                )
            admin_columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(service_admins)"
                )
            }
            if "username" not in admin_columns:
                connection.execute(
                    "ALTER TABLE service_admins "
                    "ADD COLUMN username TEXT NOT NULL DEFAULT ''"
                )
            preference_columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(user_notification_prefs)"
                )
            }
            if "actor_id" not in preference_columns:
                enabled_column = (
                    "enabled"
                    if "enabled" in preference_columns
                    else (
                        "user_notifications_enabled"
                        if "user_notifications_enabled" in preference_columns
                        else "0"
                    )
                )
                connection.executescript(
                    f"""
                    BEGIN IMMEDIATE;
                    CREATE TABLE user_notification_prefs_v2 (
                        actor_id INTEGER NOT NULL CHECK (actor_id > 0),
                        service_id TEXT NOT NULL REFERENCES services(id)
                            ON DELETE CASCADE,
                        enabled INTEGER NOT NULL DEFAULT 0
                            CHECK (enabled IN (0, 1)),
                        PRIMARY KEY (actor_id, service_id)
                    );
                    INSERT OR IGNORE INTO user_notification_prefs_v2 (
                        actor_id, service_id, enabled
                    )
                    SELECT telegram_id, service_id, {enabled_column}
                    FROM user_notification_prefs;
                    DROP TABLE user_notification_prefs;
                    ALTER TABLE user_notification_prefs_v2
                    RENAME TO user_notification_prefs;
                    """
                )
                preference_columns = {"actor_id", "service_id", "enabled"}
            if "enabled" not in preference_columns:
                connection.execute(
                    "ALTER TABLE user_notification_prefs "
                    "ADD COLUMN enabled INTEGER NOT NULL DEFAULT 0 "
                    "CHECK (enabled IN (0, 1))"
                )
                if "user_notifications_enabled" in preference_columns:
                    connection.execute(
                        "UPDATE user_notification_prefs "
                        "SET enabled = user_notifications_enabled"
                    )
            contact_primary_key = [
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(responsible_contacts)"
                )
                if row["pk"]
            ]
            if contact_primary_key != ["service_id", "task", "telegram_id"]:
                connection.executescript(
                    """
                    BEGIN IMMEDIATE;
                    DROP TABLE IF EXISTS responsible_contacts_v2;
                    CREATE TABLE responsible_contacts_v2 (
                        service_id TEXT NOT NULL
                            REFERENCES services(id) ON DELETE CASCADE,
                        task TEXT NOT NULL,
                        name TEXT NOT NULL,
                        telegram_id INTEGER NOT NULL CHECK (telegram_id > 0),
                        username TEXT NOT NULL DEFAULT '',
                        updated_by INTEGER NOT NULL CHECK (updated_by > 0),
                        updated_at TEXT NOT NULL,
                        PRIMARY KEY (service_id, task, telegram_id)
                    );
                    INSERT INTO responsible_contacts_v2 (
                        service_id, task, name, telegram_id, username,
                        updated_by, updated_at
                    )
                    SELECT service_id, task, name, telegram_id, username,
                           updated_by, updated_at
                    FROM responsible_contacts
                    WHERE rowid IN (
                        SELECT MAX(rowid)
                        FROM responsible_contacts
                        GROUP BY service_id, task, telegram_id
                    )
                    AND rowid IN (
                        SELECT MAX(rowid)
                        FROM responsible_contacts
                        GROUP BY service_id, task, name
                    );
                    DROP TABLE responsible_contacts;
                    ALTER TABLE responsible_contacts_v2
                    RENAME TO responsible_contacts;
                    COMMIT;
                    """
                )
            connection.execute(
                """
                DELETE FROM responsible_contacts
                WHERE rowid NOT IN (
                    SELECT MAX(rowid)
                    FROM responsible_contacts
                    GROUP BY service_id, task, name
                )
                """
            )
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS "
                "idx_responsible_contacts_name "
                "ON responsible_contacts(service_id, task, name)"
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO user_notification_prefs (
                    actor_id, service_id, enabled
                )
                SELECT DISTINCT telegram_id, service_id, 0
                FROM responsible_contacts
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_urtexts_actor "
                "ON user_reminder_texts(actor_id, service_id)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_urschedules_text "
                "ON user_reminder_schedules(text_id)"
            )
            connection.execute(
                """
                DELETE FROM user_notification_prefs
                WHERE NOT EXISTS (
                    SELECT 1 FROM responsible_contacts
                    WHERE responsible_contacts.service_id =
                              user_notification_prefs.service_id
                      AND responsible_contacts.telegram_id =
                              user_notification_prefs.actor_id
                )
                """
            )
            connection.execute(
                "INSERT OR IGNORE INTO runtime_counters (name, next_value) "
                "VALUES ('telegram_share_request_id', ?)",
                (secrets.randbelow(2**30) + 2**30,),
            )
            now = _utc_now()
            reminder_columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(group_reminders)"
                )
            }
            if "task" not in reminder_columns:
                # Задача напоминания (этап 3.1); NULL — все задачи служения.
                connection.execute(
                    "ALTER TABLE group_reminders ADD COLUMN task TEXT"
                )
            for column in ("active_from", "active_until"):
                if column not in reminder_columns:
                    # Период по датам воскресений (этап 4.2); NULL — без границы.
                    connection.execute(
                        f"ALTER TABLE group_reminders ADD COLUMN {column} TEXT"
                    )
            task_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(service_tasks)")
            }
            if "people_needed" not in task_columns:
                # Сколько людей нужно в задаче (этап 5.4).
                connection.execute(
                    "ALTER TABLE service_tasks ADD COLUMN people_needed "
                    "INTEGER NOT NULL DEFAULT 1 "
                    "CHECK (people_needed BETWEEN 1 AND 5)"
                )
            # Опрос готовности (этап 5): колонки, добавленные после
            # первого развёртывания.
            for table, column, ddl in (
                ("service_tasks", "people_max", "INTEGER"),
                ("service_tasks", "day_offset", "INTEGER"),
                ("service_tasks", "same_week", "INTEGER NOT NULL DEFAULT 0"),
                ("service_members", "week_tasks_json", "TEXT"),
                ("readiness_settings", "enabled_at", "TEXT"),
                ("readiness_settings", "intro_template", "TEXT"),
                ("readiness_settings", "reminder_template", "TEXT"),
                ("readiness_settings", "closed_template", "TEXT"),
                ("readiness_settings", "chat_reminder_template", "TEXT"),
                ("readiness_settings", "question_template", "TEXT"),
                ("readiness_settings", "plan_title_template", "TEXT"),
                ("readiness_settings", "plan_update_template", "TEXT"),
                ("readiness_settings", "phrases_json", "TEXT NOT NULL DEFAULT '{}'"),
                ("readiness_settings", "people_needed",
                 "INTEGER NOT NULL DEFAULT 1"),
                ("readiness_settings", "people_max", "INTEGER"),
                ("readiness_settings", "day_from", "INTEGER NOT NULL DEFAULT 0"),
                ("readiness_settings", "day_to", "INTEGER NOT NULL DEFAULT 0"),
                ("readiness_settings", "reverse_poll",
                 "INTEGER NOT NULL DEFAULT 0"),
                ("service_members", "day_offset", "INTEGER"),
                ("service_members", "month_min", "INTEGER"),
                ("service_members", "month_max", "INTEGER"),
                ("readiness_settings", "month_min", "INTEGER NOT NULL DEFAULT 0"),
                ("readiness_settings", "month_max", "INTEGER"),
                ("schedule_plans", "republish", "INTEGER NOT NULL DEFAULT 0"),
                ("schedule_plans", "days_json", "TEXT NOT NULL DEFAULT '{}'"),
                ("schedule_plans", "approved_days_json", "TEXT"),
                ("schedule_plans", "served_offsets_json", "TEXT"),
                ("schedule_plans", "approved_tasks_json", "TEXT"),
                ("schedule_plans", "approved_slot_days_json", "TEXT"),
                ("schedule_plans", "message_ids_json", "TEXT"),
                ("schedule_plans", "baseline_json", "TEXT"),
                ("schedule_plans", "thread_id", "INTEGER"),
                ("schedule_plans", "target_thread_id", "INTEGER"),
                ("service_members", "tasks_manual",
                 "INTEGER NOT NULL DEFAULT 0"),
                ("readiness_runs", "poll_stopped", "INTEGER NOT NULL DEFAULT 0"),
                ("readiness_runs", "result_sent", "INTEGER NOT NULL DEFAULT 0"),
                ("readiness_runs", "closed_announced",
                 "INTEGER NOT NULL DEFAULT 0"),
                ("readiness_runs", "options_json", "TEXT NOT NULL DEFAULT '[]'"),
                ("readiness_runs", "reverse_poll",
                 "INTEGER NOT NULL DEFAULT 0"),
            ):
                columns = {
                    row["name"]
                    for row in connection.execute(f"PRAGMA table_info({table})")
                }
                if column not in columns:
                    connection.execute(
                        f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"
                    )
                    if table == "readiness_runs" and column in {
                        "poll_stopped", "result_sent", "closed_announced"
                    }:
                        # Закрытые до миграции опросы уже подведены — без
                        # повторного итога админам.
                        connection.execute(
                            f"UPDATE readiness_runs SET {column} = 1 "
                            "WHERE status != 'open'"
                        )
            # Ранняя миграция ошибочно записывала флаг завершения и в
            # новые поля вариантов/режима старых закрытых опросов.
            connection.execute(
                "UPDATE readiness_runs SET options_json = '[]', reverse_poll = 0 "
                "WHERE status != 'open' AND options_json = '1'"
            )
            # Утверждённые до снимка задач — снимок из рабочей копии, пока
            # её не поправили: дальше напоминания не идут за черновиком.
            connection.execute(
                "UPDATE schedule_plans SET approved_tasks_json = tasks_json "
                "WHERE approved_json IS NOT NULL AND approved_tasks_json IS NULL"
            )
            self._sync_service_catalog(connection, assembly_group_chat_id)
            has_super_admin = connection.execute(
                "SELECT 1 FROM super_admins LIMIT 1"
            ).fetchone()
            if has_super_admin is None:
                connection.execute(
                    "INSERT INTO super_admins (telegram_id, created_at) "
                    "VALUES (?, ?)",
                    (super_admin_id, now),
                )
            migrated_reminders = connection.execute(
                "SELECT 1 FROM service_reminder_migrations "
                "WHERE name = 'legacy-reminders'"
            ).fetchone()
            if migrated_reminders is None:
                connection.execute(
                    """
                    INSERT INTO service_reminders (
                        service_id, reminder_days, reminder_time
                    )
                    SELECT services.id, services.reminder_days,
                           services.reminder_time
                    FROM services
                    WHERE NOT EXISTS (
                        SELECT 1 FROM service_reminders
                        WHERE service_reminders.service_id = services.id
                    )
                    """
                )
                connection.execute(
                    "INSERT INTO service_reminder_migrations (name) "
                    "VALUES ('legacy-reminders')"
                )
            self._migrate_to_service_catalog(connection)
            self._migrate_members(connection)
            self._drop_file_telegram_ids(connection)
            self._migrate_service_reminders(connection)
            self._split_plan_title(connection)

    @staticmethod
    def _split_plan_title(connection: sqlite3.Connection) -> None:
        """Однократно: сохранённый заголовок — в два шаблона. Раньше он шёл
        и в первую публикацию, и в обновления; поле {status} заменяется на
        «утверждён» и «обновлён» и больше не поддерживается."""
        if connection.execute(
            "SELECT 1 FROM catalog_migrations WHERE name = 'plan-title-v2'"
        ).fetchone():
            return
        connection.execute(
            "UPDATE readiness_settings SET plan_update_template = "
            "replace(plan_title_template, '{status}', 'обновлён') "
            "WHERE plan_title_template IS NOT NULL "
            "AND plan_update_template IS NULL"
        )
        connection.execute(
            "UPDATE readiness_settings SET plan_title_template = "
            "replace(plan_title_template, '{status}', 'утверждён') "
            "WHERE plan_title_template LIKE '%{status}%'"
        )
        connection.execute(
            "INSERT INTO catalog_migrations (name) VALUES ('plan-title-v2')"
        )

    @staticmethod
    def _drop_file_telegram_ids(connection: sqlite3.Connection) -> None:
        """Однократно снимает Telegram ID, взятые из «Списков».

        Telegram задаётся только в боте. Участник, созданный из «Списков»
        (source='excel'), с Telegram ID без записи о привязке в боте получил
        ID из файла: ID и созданные из него привязки к задачам удаляются.
        """
        if connection.execute(
            "SELECT 1 FROM catalog_migrations WHERE name = 'members-v2'"
        ).fetchone():
            return
        members = connection.execute(
            "SELECT service_id, name, telegram_id FROM service_members "
            "WHERE source = 'excel' AND telegram_id IS NOT NULL"
        ).fetchall()
        for member in members:
            bound_in_bot = connection.execute(
                "SELECT 1 FROM configuration_audit "
                "WHERE service_id = ? AND action IN "
                "('responsible_contact_updated', 'service_member_bound') "
                "AND json_extract(details_json, '$.name') = ? "
                "AND json_extract(details_json, '$.telegram_id') = ?",
                (member["service_id"], member["name"], member["telegram_id"]),
            ).fetchone()
            if bound_in_bot:
                continue
            connection.execute(
                "DELETE FROM responsible_contacts "
                "WHERE service_id = ? AND name = ? AND telegram_id = ?",
                (member["service_id"], member["name"], member["telegram_id"]),
            )
            connection.execute(
                "UPDATE service_members SET telegram_id = NULL, username = '' "
                "WHERE service_id = ? AND name = ?",
                (member["service_id"], member["name"]),
            )
            ServiceRegistry._cleanup_unbound_user_data(
                connection, member["service_id"]
            )
        connection.execute(
            "INSERT INTO catalog_migrations (name) VALUES ('members-v2')"
        )

    @staticmethod
    def _migrate_members(connection: sqlite3.Connection) -> None:
        """Однократно создаёт участников служений из привязок к задачам.

        При нескольких Telegram ID у одного имени берётся последняя привязка;
        сами привязки к задачам не меняются.
        """
        if connection.execute(
            "SELECT 1 FROM catalog_migrations WHERE name = 'members-v1'"
        ).fetchone():
            return
        connection.execute(
            """
            INSERT OR IGNORE INTO service_members (
                service_id, name, telegram_id, username, source, updated_at
            )
            SELECT service_id, name, telegram_id, username, 'legacy',
                   updated_at
            FROM responsible_contacts
            WHERE service_id IN (SELECT id FROM services WHERE archived = 0)
            ORDER BY updated_at DESC
            """
        )
        connection.execute(
            "INSERT INTO catalog_migrations (name) VALUES ('members-v1')"
        )

    @staticmethod
    def _sync_service_catalog(
        connection: sqlite3.Connection, assembly_group_chat_id: int
    ) -> None:
        """Переносит каталог служений и задач из файла в БД.

        Названия, синонимы и колонки задаёт файл; включение задач и задачи,
        добавленные суперадмином, хранятся в БД и файлом не перезаписываются.
        Служения, которых нет в каталоге, уходят в архив без удаления данных.
        """
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(services)")
        }
        if "archived" not in columns:
            connection.execute(
                "ALTER TABLE services ADD COLUMN archived INTEGER NOT NULL "
                "DEFAULT 0 CHECK (archived IN (0, 1))"
            )
        if "admin_report_template" not in columns:
            # Шаблон отчёта админам (этап 3.2); NULL — встроенный.
            connection.execute(
                "ALTER TABLE services ADD COLUMN admin_report_template TEXT"
            )
        if "group_thread_id" not in columns:
            # Тема (topic) группового чата; NULL — общая тема.
            connection.execute(
                "ALTER TABLE services ADD COLUMN group_thread_id INTEGER "
                "CHECK (group_thread_id IS NULL OR group_thread_id > 0)"
            )
        poll_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(replacement_polls)")
        }
        if "filled" not in poll_columns:
            # Запрос замены выполнен (этап 3.3): закрытый опрос без замены
            # и запрос без опроса остаются доступны для /change.
            connection.execute(
                "ALTER TABLE replacement_polls ADD COLUMN filled INTEGER "
                "NOT NULL DEFAULT 0 CHECK (filled IN (0, 1))"
            )
            # Запросы, по которым замену уже назначили, — выполнены.
            connection.execute(
                "UPDATE replacement_polls SET filled = 1 WHERE EXISTS ("
                "SELECT 1 FROM service_replacements r "
                "WHERE r.service_id = replacement_polls.service_id "
                "AND r.event_date = replacement_polls.event_date "
                "AND r.task = replacement_polls.task "
                "AND r.old_name = replacement_polls.old_name)"
            )
        schedule_columns = {
            row["name"]
            for row in connection.execute(
                "PRAGMA table_info(user_reminder_schedules)"
            )
        }
        if "origin" not in schedule_columns:
            # Личные напоминания (отзыв 21.10): «admin» — с текстом админа,
            # пользователь только включает/выключает; «user» — своё, со
            # своим временем и текстом. Свой текст задачи переходит в её
            # правила.
            connection.execute(
                "ALTER TABLE user_reminder_schedules ADD COLUMN origin TEXT "
                "NOT NULL DEFAULT 'admin' CHECK (origin IN ('admin', 'user'))"
            )
            connection.execute(
                "ALTER TABLE user_reminder_schedules ADD COLUMN body TEXT"
            )
            connection.execute(
                "UPDATE user_reminder_schedules SET origin = 'user', body = ("
                "SELECT body FROM user_reminder_texts t WHERE t.id = text_id) "
                "WHERE text_id IN (SELECT id FROM user_reminder_texts "
                "WHERE body IS NOT NULL)"
            )
        if connection.execute(
            "SELECT 1 FROM catalog_migrations WHERE name = 'personal-origin-v2'"
        ).fetchone() is None:
            # Напоминанием админа остаётся только правило по умолчанию
            # («за 1 дн. в 18:00», единственное у задачи, без своего
            # текста); правила, которые человек задавал сам, — свои.
            connection.execute(
                "UPDATE user_reminder_schedules SET origin = 'user' "
                "WHERE origin = 'admin' AND body IS NULL AND ("
                "reminder_days != 1 OR reminder_time != '18:00' OR "
                "(SELECT COUNT(*) FROM user_reminder_schedules other "
                "WHERE other.text_id = user_reminder_schedules.text_id "
                "AND other.origin = 'admin') > 1)"
            )
            # Свой текст задачи — в её правила (если их ещё не скопировали)
            # или, если правил нет, в выключенное своё напоминание.
            connection.execute(
                "UPDATE user_reminder_schedules SET origin = 'user', body = ("
                "SELECT body FROM user_reminder_texts t WHERE t.id = text_id) "
                "WHERE body IS NULL AND text_id IN (SELECT id FROM "
                "user_reminder_texts WHERE body IS NOT NULL)"
            )
            connection.execute(
                "INSERT INTO user_reminder_schedules "
                "(text_id, reminder_days, reminder_time, enabled, origin, body) "
                "SELECT id, 1, '18:00', 0, 'user', body "
                "FROM user_reminder_texts t WHERE body IS NOT NULL "
                "AND NOT EXISTS (SELECT 1 FROM user_reminder_schedules s "
                "WHERE s.text_id = t.id)"
            )
            connection.execute(
                "UPDATE user_reminder_texts SET body = NULL "
                "WHERE body IS NOT NULL"
            )
            connection.execute(
                "INSERT INTO catalog_migrations (name) "
                "VALUES ('personal-origin-v2')"
            )
        for service in SERVICES:
            is_assembly = service.id == "assembly"
            connection.execute(
                """
                INSERT INTO services (
                    id, name, aliases_json, assignment_layout,
                    group_chat_id, enabled
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name = excluded.name,
                    aliases_json = excluded.aliases_json,
                    assignment_layout = excluded.assignment_layout,
                    archived = 0
                """,
                (
                    service.id,
                    service.name,
                    json.dumps(service.aliases, ensure_ascii=False),
                    service.assignment_layout,
                    assembly_group_chat_id if is_assembly else None,
                    int(is_assembly),
                ),
            )
            for position, task in enumerate(service.tasks):
                connection.execute(
                    """
                    INSERT INTO service_tasks (
                        service_id, name, position, enabled, aliases_json,
                        source
                    ) VALUES (?, ?, ?, ?, ?, 'catalog')
                    ON CONFLICT(service_id, name) DO UPDATE SET
                        position = excluded.position,
                        aliases_json = excluded.aliases_json,
                        source = 'catalog'
                    """,
                    (
                        service.id,
                        task.name,
                        position,
                        int(task.enabled),
                        json.dumps(task.aliases, ensure_ascii=False),
                    ),
                )
            # Задачи, убранные из каталога, удаляются, чтобы не занимать
            # позиции текущих задач (позиция — идентификатор кнопки).
            catalog_names = [task.name for task in service.tasks]
            placeholders = ", ".join("?" for _ in catalog_names) or "''"
            connection.execute(
                "DELETE FROM service_tasks WHERE service_id = ? "
                f"AND source = 'catalog' AND name NOT IN ({placeholders})",
                (service.id, *catalog_names),
            )
            ServiceRegistry._detach_unavailable_reminder_tasks(
                connection, service.id
            )
            # Задачи, добавленные суперадмином, идут после задач каталога;
            # позиции уникальны и служат идентификатором кнопок.
            admin_tasks = connection.execute(
                "SELECT name FROM service_tasks "
                "WHERE service_id = ? AND source = 'admin' "
                "ORDER BY position, name",
                (service.id,),
            ).fetchall()
            for offset, row in enumerate(admin_tasks):
                connection.execute(
                    "UPDATE service_tasks SET position = ? "
                    "WHERE service_id = ? AND name = ?",
                    (len(service.tasks) + offset, service.id, row["name"]),
                )
        catalog_ids = [service.id for service in SERVICES]
        connection.execute(
            "UPDATE services SET archived = 1, enabled = 0 "
            f"WHERE id NOT IN ({', '.join('?' for _ in catalog_ids)})",
            catalog_ids,
        )

    @staticmethod
    def _migrate_to_service_catalog(connection: sqlite3.Connection) -> None:
        """Однократно переводит данные на каталог служений и задач.

        Администраторы «Слайдов» становятся администраторами «Трансляции и
        слайдов»; задачи привязок, личных текстов и шаблонов переименовываются
        в названия каталога по синонимам. Выполняется в общей транзакции
        initialize() и отмечается в catalog_migrations.
        """
        if connection.execute(
            "SELECT 1 FROM catalog_migrations WHERE name = 'catalog-v1'"
        ).fetchone():
            return
        # Упразднённые служения стали задачами других служений: их
        # администраторы, привязки, личные тексты и настройки переносятся.
        merged_services = {
            "slides": ("broadcast", "Слайды"),
            "rehearsal": ("sound", "спевка"),
            "coffee": ("kitchen", "кофе"),
        }
        existing = {
            row["id"] for row in connection.execute("SELECT id FROM services")
        }
        for old_id, (new_id, new_task) in merged_services.items():
            if old_id not in existing or new_id not in existing:
                continue
            connection.execute(
                """
                INSERT OR IGNORE INTO service_admins (
                    service_id, telegram_id, username, created_by, created_at
                )
                SELECT ?, telegram_id, username, created_by, created_at
                FROM service_admins WHERE service_id = ?
                """,
                (new_id, old_id),
            )
            for row in connection.execute(
                "SELECT * FROM responsible_contacts WHERE service_id = ?",
                (old_id,),
            ).fetchall():
                _move_contact(connection, row, new_id, new_task)
            for row in connection.execute(
                "SELECT id FROM user_reminder_texts WHERE service_id = ?",
                (old_id,),
            ).fetchall():
                _move_user_text(connection, row["id"], new_id, new_task)
            connection.execute(
                """
                INSERT INTO user_notification_prefs (service_id, actor_id, enabled)
                SELECT ?, actor_id, enabled FROM user_notification_prefs
                WHERE service_id = ?
                ON CONFLICT(actor_id, service_id) DO UPDATE SET
                    enabled = MAX(enabled, excluded.enabled)
                """,
                (new_id, old_id),
            )
        # Строки без задачи раньше получали название служения; в этих
        # служениях так записывались участники «в смене».
        legacy_tasks = {
            ("kitchen", "кухня"): "в смене",
            ("order", "орг. порядка"): "в смене",
        }
        renames: dict[tuple[str, str], str | None] = {}

        def canonical(service_id: str, task: str) -> str | None:
            key = (service_id, task)
            if key not in renames:
                normalized = normalized_name(task)
                target = legacy_tasks.get((service_id, normalized))
                if target is None:
                    target = next(
                        (
                            row["name"]
                            for row in connection.execute(
                                "SELECT name, aliases_json FROM service_tasks "
                                "WHERE service_id = ?",
                                (service_id,),
                            )
                            if normalized in {
                                normalized_name(name)
                                for name in (
                                    row["name"],
                                    *json.loads(row["aliases_json"]),
                                )
                            }
                        ),
                        None,
                    )
                renames[key] = target
            target = renames[key]
            return target if target and target != task else None

        for row in connection.execute(
            "SELECT * FROM responsible_contacts"
        ).fetchall():
            target = canonical(row["service_id"], row["task"])
            if target is not None:
                _move_contact(connection, row, row["service_id"], target)
        for row in connection.execute(
            "SELECT id, service_id, task FROM user_reminder_texts"
        ).fetchall():
            target = canonical(row["service_id"], row["task"])
            if target is not None:
                _move_user_text(connection, row["id"], row["service_id"], target)
        for row in connection.execute(
            "SELECT id, personal_templates_json FROM services "
            "WHERE personal_templates_json IS NOT NULL"
        ).fetchall():
            templates = json.loads(row["personal_templates_json"])
            renamed = {}
            for task, template in templates.items():
                target = canonical(row["id"], task) or task
                # Шаблон под каноническим названием главнее; шаблон под
                # синонимом при конфликте остаётся под своим ключом.
                if target != task and target in templates:
                    renamed[task] = template
                else:
                    renamed[target] = template
            if renamed != templates:
                connection.execute(
                    "UPDATE services SET personal_templates_json = ? "
                    "WHERE id = ?",
                    (json.dumps(renamed, ensure_ascii=False), row["id"]),
                )
        connection.execute(
            "INSERT INTO catalog_migrations (name) VALUES ('catalog-v1')"
        )

    @staticmethod
    def _migrate_service_reminders(
        connection: sqlite3.Connection,
    ) -> None:
        migration_name = "service-reminders-to-group-reminders"
        migrated = connection.execute(
            "SELECT 1 FROM group_reminder_migrations WHERE name = ?",
            (migration_name,),
        ).fetchone()
        if migrated is not None:
            return
        connection.execute("SAVEPOINT group_reminder_migration")
        try:
            rows = connection.execute(
                "SELECT id, service_id, reminder_days, reminder_time "
                "FROM service_reminders "
                "ORDER BY service_id, reminder_days, reminder_time, id"
            ).fetchall()
            deliveries = connection.execute(
                "SELECT service_id, reminder_id, event_date, recipient_key, "
                "delivered_at FROM service_reminder_deliveries"
            ).fetchall()
            migrated_ids: dict[int, int] = {}
            new_ids_by_schedule: dict[tuple[str, int, str], int] = {}
            for row in connection.execute(
                "SELECT id, service_id, reminder_days, reminder_time "
                "FROM group_reminders WHERE task IS NULL ORDER BY id"
            ):
                schedule_key = (
                    row["service_id"],
                    row["reminder_days"],
                    row["reminder_time"],
                )
                new_ids_by_schedule.setdefault(schedule_key, row["id"])
            for row in rows:
                schedule_key = (
                    row["service_id"],
                    row["reminder_days"],
                    row["reminder_time"],
                )
                new_id = new_ids_by_schedule.get(schedule_key)
                if new_id is None:
                    cursor = connection.execute(
                        "INSERT INTO group_reminders "
                        "(service_id, body, reminder_days, reminder_time, "
                        "enabled) VALUES (?, NULL, ?, ?, 1)",
                        schedule_key,
                    )
                    new_id = cursor.lastrowid
                    new_ids_by_schedule[schedule_key] = new_id
                migrated_ids[row["id"]] = new_id
                connection.execute(
                    "INSERT OR IGNORE INTO group_reminder_legacy_ids "
                    "(legacy_id, reminder_id) VALUES (?, ?)",
                    (row["id"], new_id),
                )
            for delivery in deliveries:
                new_id = migrated_ids.get(delivery["reminder_id"])
                if new_id is None:
                    continue
                connection.execute(
                    "INSERT OR IGNORE INTO group_reminder_deliveries "
                    "(reminder_id, event_date, recipient_key, delivered_at) "
                    "VALUES (?, ?, ?, ?)",
                    (
                        new_id,
                        delivery["event_date"],
                        delivery["recipient_key"],
                        delivery["delivered_at"],
                    ),
                )
            connection.execute(
                "INSERT OR IGNORE INTO group_reminder_migrations (name) "
                "VALUES (?)",
                (migration_name,),
            )
            connection.execute("RELEASE SAVEPOINT group_reminder_migration")
        except Exception:
            connection.execute(
                "ROLLBACK TO SAVEPOINT group_reminder_migration"
            )
            connection.execute("RELEASE SAVEPOINT group_reminder_migration")
            raise

    def next_share_request_id(self) -> int:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT next_value FROM runtime_counters "
                "WHERE name = 'telegram_share_request_id'"
            ).fetchone()
            if row is None or row["next_value"] == 0:
                raise ValueError(
                    "Исчерпан счётчик запросов выбора Telegram."
                )
            request_id = row["next_value"]
            if request_id == 2**31 - 1:
                connection.execute(
                    "UPDATE runtime_counters SET next_value = 0 "
                    "WHERE name = 'telegram_share_request_id'"
                )
            else:
                connection.execute(
                    "UPDATE runtime_counters SET next_value = ? "
                    "WHERE name = 'telegram_share_request_id'",
                    (request_id + 1,),
                )
        return request_id

    def _ensure_private_database_file(self) -> None:
        try:
            fd = os.open(
                self.path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
            )
        except FileExistsError:
            file_stat = self.path.lstat()
            if (
                not stat.S_ISREG(file_stat.st_mode)
                or file_stat.st_uid != os.geteuid()
                or stat.S_IMODE(file_stat.st_mode) != 0o600
            ):
                raise PermissionError(
                    f"База данных должна быть обычным файлом владельца "
                    f"с правами 600: {self.path}"
                )
        else:
            os.close(fd)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @contextmanager
    def _connection(self, *, notify: bool = True):
        """notify=False — запись, от которой не зависят задачи планировщика
        (голоса в опросе): без резервной копии и пересборки задач."""
        connection = self._connect()
        changed = False
        try:
            with connection:
                yield connection
            changed = connection.total_changes > 0
        finally:
            connection.close()
        if notify and changed and self.on_change is not None:
            self.on_change()

    def list_services(self) -> list[dict]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT id, name, aliases_json, assignment_layout, "
                "group_chat_id, group_thread_id, enabled, reminder_days, "
                "reminder_time, group_template, personal_templates_json, "
                "admin_report_template "
                "FROM services WHERE archived = 0 ORDER BY id"
            ).fetchall()
            reminders = connection.execute(
                "SELECT id, service_id, body, reminder_days, reminder_time, "
                "enabled, created_at, task, active_from, active_until "
                "FROM group_reminders "
                "ORDER BY service_id, reminder_days, reminder_time, id"
            ).fetchall()
        reminders_by_service: dict[str, list[dict]] = {}
        for reminder in reminders:
            reminders_by_service.setdefault(reminder["service_id"], []).append(
                {
                    "id": reminder["id"],
                    "service_id": reminder["service_id"],
                    "body": reminder["body"],
                    "reminder_days": reminder["reminder_days"],
                    "reminder_time": reminder["reminder_time"],
                    "enabled": bool(reminder["enabled"]),
                    "created_at": reminder["created_at"],
                    "task": reminder["task"],
                    "active_from": reminder["active_from"],
                    "active_until": reminder["active_until"],
                }
            )
        services = []
        for row in rows:
            group_reminders = reminders_by_service.get(row["id"], [])
            services.append({
                **dict(row),
                "aliases": json.loads(row["aliases_json"]),
                "group_reminders": group_reminders,
                # Compatibility alias; both keys use the canonical table.
                "reminders": group_reminders,
                "enabled": bool(row["enabled"]),
                "personal_templates": (
                    json.loads(row["personal_templates_json"])
                    if row["personal_templates_json"]
                    else {}
                ),
            })
        return services

    def get_service(self, service_id: str) -> dict | None:
        return next(
            (
                service
                for service in self.list_services()
                if service["id"] == service_id
            ),
            None,
        )

    def list_service_tasks(self, service_id: str) -> list[dict]:
        """Задачи служения в порядке каталога, включая выключенные."""
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT name, position, enabled, aliases_json, source, "
                "people_needed, people_max, day_offset, same_week "
                "FROM service_tasks WHERE service_id = ? "
                "ORDER BY position, name",
                (service_id,),
            ).fetchall()
        return [
            {
                "name": row["name"],
                "position": row["position"],
                "enabled": bool(row["enabled"]),
                "aliases": json.loads(row["aliases_json"]),
                "source": row["source"],
                "people_needed": row["people_needed"],
                # NULL — ровно people_needed.
                "people_max": max(
                    row["people_max"] or row["people_needed"],
                    row["people_needed"],
                ),
                # Свой день задачи; None — дни служения раздела «📅 График».
                "day_offset": row["day_offset"],
                "same_week": bool(row["same_week"]),
            }
            for row in rows
        ]

    def plan_tasks(self, service_id: str) -> list[dict]:
        """Задачи графика: включённые задачи служения; у служения без задач
        (Уборка, Книги) — одна задача с названием служения и «Нужно людей»
        из настроек раздела «📅 График»."""
        tasks = self.list_service_tasks(service_id)
        if tasks:
            return [
                {
                    "name": task["name"], "needed": task["people_needed"],
                    "max": task["people_max"], "position": str(task["position"]),
                    "implicit": False, "day": task["day_offset"],
                }
                for task in tasks if task["enabled"]
            ]
        service = self.get_service(service_id)
        if service is None:
            return []
        settings = self.get_readiness_settings(service_id)
        return [{
            "name": service["name"], "needed": settings["people_needed"],
            "max": settings["people_max"] or settings["people_needed"],
            "position": "s", "implicit": True, "day": None,
        }]

    def service_catalog(self) -> tuple[ServiceDefinition, ...]:
        """Каталог из файла с задачами в состоянии из БД (для парсера)."""
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT service_id, name, enabled, aliases_json "
                "FROM service_tasks ORDER BY service_id, position, name"
            ).fetchall()
        tasks: dict[str, list[ServiceTask]] = {}
        for row in rows:
            tasks.setdefault(row["service_id"], []).append(
                ServiceTask(
                    row["name"],
                    tuple(json.loads(row["aliases_json"])),
                    bool(row["enabled"]),
                )
            )
        return tuple(
            replace(service, tasks=tuple(tasks.get(service.id, ())))
            for service in SERVICES
        )

    @staticmethod
    def _detach_unavailable_reminder_tasks(
        connection: sqlite3.Connection, service_id: str
    ) -> None:
        """Напоминание задачи, которой больше нет (или она выключена),
        выключается и становится общим: иначе оно не отправлялось бы, а
        админ получал бы ⚠️ на каждую дату."""
        connection.execute(
            "UPDATE group_reminders SET task = NULL, enabled = 0 "
            "WHERE service_id = ? AND task IS NOT NULL AND task NOT IN ("
            "SELECT name FROM service_tasks WHERE service_id = ? "
            "AND enabled = 1)",
            (service_id, service_id),
        )

    def set_service_task_enabled(
        self, actor_id: int, service_id: str, task: str, enabled: bool
    ) -> None:
        _validate_positive_id(actor_id, "actor_id")
        if not isinstance(enabled, bool):
            raise ValueError("enabled должен быть логическим значением")
        with self._connection() as connection:
            self._require_service_editor(connection, actor_id, service_id)
            self._require_service(connection, service_id)
            cursor = connection.execute(
                "UPDATE service_tasks SET enabled = ? "
                "WHERE service_id = ? AND name = ?",
                (int(enabled), service_id, task),
            )
            if cursor.rowcount == 0:
                raise ValueError(f"Задача «{task}» не найдена.")
            self._detach_unavailable_reminder_tasks(connection, service_id)
            self._audit(
                connection, actor_id, "service_task_updated", service_id,
                {"task": task, "enabled": enabled},
            )

    def set_service_task_people_needed(
        self, actor_id: int, service_id: str, task: str, count: int,
        maximum: int | None = None,
    ) -> None:
        """«👥 Нужно людей» в задаче для распределения графика (этап 5.4):
        минимум и, если больше, максимум на одно воскресенье. Доступно
        суперадмину и админу служения."""
        _validate_positive_id(actor_id, "actor_id")
        maximum = count if maximum is None else maximum
        for value in (count, maximum):
            if not isinstance(value, int) or isinstance(value, bool):
                raise ValueError("Нужно людей: число.")
        if not 1 <= count <= 5 or not count <= maximum <= 6:
            raise ValueError("Нужно людей: от 1 до 5, максимум — до 6.")
        stored_max = maximum if maximum > count else None
        with self._connection() as connection:
            self._require_service_editor(connection, actor_id, service_id)
            self._require_service(connection, service_id)
            cursor = connection.execute(
                "UPDATE service_tasks SET people_needed = ?, people_max = ? "
                "WHERE service_id = ? AND name = ? "
                "AND (people_needed != ? OR people_max IS NOT ?)",
                (count, stored_max, service_id, task, count, stored_max),
            )
            if cursor.rowcount == 0:
                exists = connection.execute(
                    "SELECT 1 FROM service_tasks "
                    "WHERE service_id = ? AND name = ?",
                    (service_id, task),
                ).fetchone()
                if exists is None:
                    raise ValueError(f"Задача «{task}» не найдена.")
                return
            self._audit(
                connection, actor_id, "service_task_updated", service_id,
                {"task": task, "people_needed": count, "people_max": maximum},
            )

    def set_service_task_day(
        self, actor_id: int, service_id: str, task: str, offset: int | None,
    ) -> None:
        """Свой день задачи (решение 12): сдвиг от воскресенья графика
        (спевка — вс −4, ср); None — дни служения раздела «📅 График».
        График показывается по неделям; день переносится в графики текущего
        и будущих месяцев, утверждённые публикуются заново."""
        _validate_positive_id(actor_id, "actor_id")
        if offset is not None and (
            isinstance(offset, bool) or not isinstance(offset, int)
            or not -6 <= offset <= 6
        ):
            raise ValueError("День задачи: от вс −6 до вс +6.")
        with self._connection() as connection:
            self._require_service_editor(connection, actor_id, service_id)
            self._require_service(connection, service_id)
            row = connection.execute(
                "SELECT day_offset FROM service_tasks "
                "WHERE service_id = ? AND name = ?",
                (service_id, task),
            ).fetchone()
            if row is None:
                raise ValueError(f"Задача «{task}» не найдена.")
            if row["day_offset"] == offset:
                return
            connection.execute(
                "UPDATE service_tasks SET day_offset = ? "
                "WHERE service_id = ? AND name = ?",
                (offset, service_id, task),
            )
            plans = connection.execute(
                "SELECT month, tasks_json, approved_tasks_json "
                "FROM schedule_plans WHERE service_id = ? AND month >= ?",
                (service_id, _current_month()),
            ).fetchall()

            def moved(raw):
                # Задачи с новым днём; None — задачи нет (или нет снимка).
                items = json.loads(raw) if raw else []
                if not any(item["name"] == task for item in items):
                    return None
                for item in items:
                    if item["name"] == task:
                        item["day"] = offset
                return json.dumps(items, ensure_ascii=False)

            for plan in plans:
                tasks = moved(plan["tasks_json"])
                # Утверждённый график с этой задачей — опубликовать заново,
                # день — и в утверждённых задачах (напоминания как в
                # публикации), даже если в черновике её уже нет; графики
                # без неё не меняются.
                approved = moved(plan["approved_tasks_json"])
                if tasks is None and approved is None:
                    continue
                connection.execute(
                    "UPDATE schedule_plans "
                    "SET tasks_json = COALESCE(?, tasks_json), updated_at = ?, "
                    "approved_tasks_json = COALESCE(?, approved_tasks_json), "
                    "republish = CASE WHEN approved_json IS NOT NULL THEN 1 "
                    "ELSE republish END "
                    "WHERE service_id = ? AND month = ?",
                    (tasks, _utc_now(), approved, service_id, plan["month"]),
                )
            self._audit(
                connection, actor_id, "service_task_updated", service_id,
                {"task": task, "day_offset": offset},
            )

    def set_service_task_same_week(
        self, actor_id: int, service_id: str, task: str, flag: bool,
    ) -> None:
        """«🔗 В одну неделю»: отмеченные задачи служения один человек
        берёт вместе (у кого они есть; личная настройка главнее)."""
        _validate_positive_id(actor_id, "actor_id")
        if not isinstance(flag, bool):
            raise ValueError("Отметка — логическое значение.")
        with self._connection() as connection:
            self._require_service_editor(connection, actor_id, service_id)
            self._require_service(connection, service_id)
            cursor = connection.execute(
                "UPDATE service_tasks SET same_week = ? "
                "WHERE service_id = ? AND name = ?",
                (int(flag), service_id, task),
            )
            if cursor.rowcount == 0:
                raise ValueError(f"Задача «{task}» не найдена.")
            self._audit(
                connection, actor_id, "service_task_updated", service_id,
                {"task": task, "same_week": flag},
            )

    def add_service_task(
        self, actor_id: int, service_id: str, name: str
    ) -> None:
        _validate_positive_id(actor_id, "actor_id")
        name = " ".join(name.split()) if isinstance(name, str) else ""
        if not name or len(name) > 60 or "\x00" in name:
            raise ValueError("Название задачи: от 1 до 60 символов.")
        with self._connection() as connection:
            self._require_service_editor(connection, actor_id, service_id)
            self._require_service(connection, service_id)
            existing = connection.execute(
                "SELECT name, aliases_json, position FROM service_tasks "
                "WHERE service_id = ?",
                (service_id,),
            ).fetchall()
            key = normalized_name(name)
            for row in existing:
                if key in {
                    normalized_name(value)
                    for value in (row["name"], *json.loads(row["aliases_json"]))
                }:
                    raise ValueError(
                        f"Задача «{name}» уже есть: «{row['name']}»."
                    )
            connection.execute(
                "INSERT INTO service_tasks "
                "(service_id, name, position, enabled, source) "
                "VALUES (?, ?, ?, 1, 'admin')",
                (
                    service_id,
                    name,
                    max((row["position"] for row in existing), default=-1) + 1,
                ),
            )
            self._audit(
                connection, actor_id, "service_task_added", service_id,
                {"task": name},
            )

    def is_super_admin(self, telegram_id: int) -> bool:
        with self._connection() as connection:
            return connection.execute(
                "SELECT 1 FROM super_admins WHERE telegram_id = ?",
                (telegram_id,),
            ).fetchone() is not None

    def is_service_admin(self, telegram_id: int, service_id: str) -> bool:
        with self._connection() as connection:
            return connection.execute(
                "SELECT 1 FROM service_admins "
                "WHERE telegram_id = ? AND service_id = ?",
                (telegram_id, service_id),
            ).fetchone() is not None

    def add_service_admin(
        self,
        actor_id: int,
        service_id: str,
        telegram_id: int,
        username: str = "",
    ) -> None:
        _validate_positive_id(actor_id, "actor_id")
        _validate_positive_id(telegram_id, "telegram_id")
        normalized_username = username.strip()
        if normalized_username.startswith("@"):
            normalized_username = normalized_username[1:]
        if normalized_username and not re.fullmatch(
            r"[A-Za-z][A-Za-z0-9_]{4,31}", normalized_username
        ):
            raise ValueError("Некорректный Telegram username.")
        with self._connection() as connection:
            self._require_super_admin(connection, actor_id)
            self._require_service(connection, service_id)
            existing = connection.execute(
                "SELECT 1 FROM service_admins "
                "WHERE service_id = ? AND telegram_id = ?",
                (service_id, telegram_id),
            ).fetchone()
            connection.execute(
                "INSERT INTO service_admins "
                "(service_id, telegram_id, username, created_by, created_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(service_id, telegram_id) DO UPDATE SET "
                "username = CASE WHEN excluded.username != '' "
                "THEN excluded.username ELSE service_admins.username END",
                (
                    service_id, telegram_id, normalized_username,
                    actor_id, _utc_now(),
                ),
            )
            if existing is None:
                self._audit(
                    connection, actor_id, "service_admin_added", service_id,
                    {"telegram_id": telegram_id},
                )

    def remove_service_admin(
        self,
        actor_id: int,
        service_id: str,
        telegram_id: int,
    ) -> None:
        _validate_positive_id(actor_id, "actor_id")
        _validate_positive_id(telegram_id, "telegram_id")
        with self._connection() as connection:
            self._require_super_admin(connection, actor_id)
            self._require_service(connection, service_id)
            cursor = connection.execute(
                "DELETE FROM service_admins "
                "WHERE service_id = ? AND telegram_id = ?",
                (service_id, telegram_id),
            )
            if cursor.rowcount:
                self._audit(
                    connection, actor_id, "service_admin_removed", service_id,
                    {"telegram_id": telegram_id},
                )

    def set_group_chat(
        self,
        actor_id: int,
        service_id: str,
        group_chat_id: int,
        *,
        enabled: bool,
    ) -> None:
        _validate_positive_id(actor_id, "actor_id")
        if group_chat_id == 0:
            raise ValueError("ID группового чата не может быть равен нулю")
        with self._connection() as connection:
            self._require_service_editor(connection, actor_id, service_id)
            self._require_service(connection, service_id)
            is_super_admin = connection.execute(
                "SELECT 1 FROM super_admins WHERE telegram_id = ?",
                (actor_id,),
            ).fetchone() is not None
            if not is_super_admin:
                # Админ служения — только группа и не чужая: иначе он слал
                # бы напоминания в чат другого служения или личный чат.
                if group_chat_id > 0:
                    raise ValueError(
                        "Укажите групповой чат (ID группы отрицательный)."
                    )
                current = connection.execute(
                    "SELECT group_chat_id FROM services WHERE id = ?",
                    (service_id,),
                ).fetchone()
                own_chat = current is not None and (
                    current["group_chat_id"] == group_chat_id
                )
                if not own_chat and connection.execute(
                    "SELECT 1 FROM services WHERE group_chat_id = ? "
                    "AND id != ?",
                    (group_chat_id, service_id),
                ).fetchone() is not None:
                    raise PermissionError(
                        "Этот чат уже привязан к другому служению — "
                        "общий чат для нескольких служений настраивает "
                        "супер-администратор."
                    )
            # Новый чат — тема прежнего чата к нему не относится.
            connection.execute(
                "UPDATE services SET group_thread_id = CASE "
                "WHEN group_chat_id IS ? THEN group_thread_id ELSE NULL END, "
                "group_chat_id = ?, enabled = ? WHERE id = ?",
                (group_chat_id, group_chat_id, int(enabled), service_id),
            )
            self._audit(
                connection, actor_id, "service_chat_updated", service_id,
                {"group_chat_id": group_chat_id, "enabled": enabled},
            )

    def set_group_thread(
        self,
        actor_id: int,
        service_id: str,
        group_chat_id: int,
        thread_id: int | None,
    ) -> None:
        """Тема чата служения для напоминаний (команда /here в теме).

        Задать может суперадмин или админ служения и только в чате,
        привязанном к служению; thread_id None — общая тема.
        """
        _validate_positive_id(actor_id, "actor_id")
        if thread_id is not None:
            _validate_positive_id(thread_id, "thread_id")
        with self._connection() as connection:
            self._require_service(connection, service_id)
            self._require_service_editor(connection, actor_id, service_id)
            current = connection.execute(
                "SELECT group_chat_id FROM services WHERE id = ?",
                (service_id,),
            ).fetchone()
            if current["group_chat_id"] != group_chat_id:
                raise ValueError(
                    "Этот чат не привязан к служению. Сначала выберите его "
                    "в карточке служения."
                )
            connection.execute(
                "UPDATE services SET group_thread_id = ? WHERE id = ?",
                (thread_id, service_id),
            )
            self._audit(
                connection, actor_id, "service_thread_updated", service_id,
                {"group_chat_id": group_chat_id, "thread_id": thread_id},
            )

    def reset_group_thread(self, service_id: str, thread_id: int) -> None:
        """Системный сброс темы, которой больше нет в чате (тема удалена)."""
        with self._connection() as connection:
            connection.execute(
                "UPDATE services SET group_thread_id = NULL "
                "WHERE id = ? AND group_thread_id = ?",
                (service_id, thread_id),
            )

    def set_service_enabled(
        self, actor_id: int, service_id: str, enabled: bool
    ) -> None:
        _validate_positive_id(actor_id, "actor_id")
        if not isinstance(enabled, bool):
            raise ValueError("enabled должен быть логическим значением")
        with self._connection() as connection:
            self._require_service_editor(connection, actor_id, service_id)
            self._require_service(connection, service_id)
            connection.execute(
                "UPDATE services SET enabled = ? WHERE id = ?",
                (int(enabled), service_id),
            )
            self._audit(
                connection,
                actor_id,
                "service_enabled_updated",
                service_id,
                {"enabled": enabled},
            )

    def list_group_reminders(self, service_id: str) -> list[dict]:
        with self._connection() as connection:
            self._require_service(connection, service_id)
            rows = connection.execute(
                f"SELECT {_GROUP_REMINDER_COLUMNS} "
                f"FROM {_GROUP_REMINDER_SOURCE} "
                "WHERE g.service_id = ? "
                "ORDER BY g.reminder_days, g.reminder_time, g.id",
                (service_id,),
            ).fetchall()
        return [
            {**dict(row), "enabled": bool(row["enabled"])}
            for row in rows
        ]

    def list_legacy_group_reminders(self, service_id: str) -> list[dict]:
        """Deprecated fallback used only if the migrated table is empty."""
        with self._connection() as connection:
            self._require_service(connection, service_id)
            rows = connection.execute(
                "SELECT id, service_id, reminder_days, reminder_time "
                "FROM service_reminders WHERE service_id = ? "
                "ORDER BY reminder_days, reminder_time, id",
                (service_id,),
            ).fetchall()
        return [
            {
                **dict(row),
                "body": None,
                "enabled": True,
                "created_at": "",
                "legacy": True,
            }
            for row in rows
        ]

    def get_group_reminder(self, reminder_id: int) -> dict | None:
        _validate_positive_id(reminder_id, "reminder_id")
        with self._connection() as connection:
            row = connection.execute(
                f"SELECT {_GROUP_REMINDER_COLUMNS} "
                f"FROM {_GROUP_REMINDER_SOURCE} WHERE g.id = ?",
                (reminder_id,),
            ).fetchone()
        return (
            {**dict(row), "enabled": bool(row["enabled"])}
            if row is not None
            else None
        )

    def get_legacy_group_reminder(self, reminder_id: int) -> dict | None:
        """Deprecated fallback lookup for legacy scheduler jobs."""
        _validate_positive_id(reminder_id, "reminder_id")
        with self._connection() as connection:
            row = connection.execute(
                "SELECT id, service_id, reminder_days, reminder_time "
                "FROM service_reminders WHERE id = ?",
                (reminder_id,),
            ).fetchone()
        return (
            {
                **dict(row),
                "body": None,
                "enabled": True,
                "created_at": "",
                "legacy": True,
            }
            if row is not None
            else None
        )

    def add_group_reminder(
        self,
        actor_id: int,
        service_id: str,
        days: int,
        time: str,
        body: str | None = None,
        *,
        allow_duplicate: bool = False,
        task: str | None = None,
    ) -> int:
        _validate_positive_id(actor_id, "actor_id")
        self._validate_group_reminder_values(days, time, body, task)
        with self._connection() as connection:
            self._require_service(connection, service_id)
            self._require_service_editor(connection, actor_id, service_id)
            self._require_reminder_task(connection, service_id, task)
            self._ensure_group_schedule_unique(
                connection, service_id, days, time, allow_duplicate, task=task
            )
            cursor = connection.execute(
                "INSERT INTO group_reminders "
                "(service_id, body, reminder_days, reminder_time, task) "
                "VALUES (?, ?, ?, ?, ?)",
                (service_id, body.strip() if body else None, days, time, task),
            )
            reminder_id = cursor.lastrowid
            self._audit(
                connection,
                actor_id,
                "group_reminder_added",
                service_id,
                {
                    "reminder_id": reminder_id,
                    "reminder_days": days,
                    "reminder_time": time,
                    "has_custom_body": body is not None,
                    "task": task,
                },
            )
        return reminder_id

    def update_group_reminder(
        self,
        actor_id: int,
        reminder_id: int,
        days: int | None = None,
        time: str | None = None,
        body=_UNSET,
        enabled: bool | None = None,
        *,
        allow_duplicate: bool = False,
    ) -> None:
        _validate_positive_id(actor_id, "actor_id")
        _validate_positive_id(reminder_id, "reminder_id")
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM group_reminders WHERE id = ?",
                (reminder_id,),
            ).fetchone()
            if row is None:
                raise ValueError("Групповое напоминание больше не существует.")
            self._require_service_editor(
                connection, actor_id, row["service_id"]
            )
            new_days = row["reminder_days"] if days is None else days
            new_time = row["reminder_time"] if time is None else time
            new_body = row["body"] if body is _UNSET else body
            new_enabled = bool(row["enabled"]) if enabled is None else enabled
            # Поля текста проверяются, только когда текст меняют: старый
            # текст не должен мешать выключить или перенести напоминание.
            self._validate_group_reminder_values(
                new_days, new_time,
                new_body if body is not _UNSET else None, row["task"],
            )
            if not isinstance(new_enabled, bool):
                raise ValueError("enabled должен быть логическим значением")
            # При переносе слот проверяется целиком. Без переноса текст и
            # выключение не проверяются, а повторное включение запрещено,
            # только если в слоте уже есть включённое напоминание.
            slot_changed = (new_days, new_time) != (
                row["reminder_days"], row["reminder_time"]
            )
            if slot_changed:
                self._ensure_group_schedule_unique(
                    connection,
                    row["service_id"],
                    new_days,
                    new_time,
                    allow_duplicate,
                    excluding_id=reminder_id,
                    task=row["task"],
                    period=dict(row),
                )
            elif new_enabled and not row["enabled"]:
                self._ensure_group_schedule_unique(
                    connection,
                    row["service_id"],
                    new_days,
                    new_time,
                    allow_duplicate,
                    excluding_id=reminder_id,
                    enabled_only=True,
                    task=row["task"],
                    period=dict(row),
                )
            if slot_changed:
                # Новый срок или время — новая отправка: отметки доставки
                # на предстоящие даты сбрасываются, иначе напоминание,
                # уже ушедшее на это воскресенье, молча не сработает.
                # Только групповое сообщение: предупреждения админу о
                # графике не повторяются. День запаса — на часовой пояс.
                connection.execute(
                    "DELETE FROM group_reminder_deliveries "
                    "WHERE reminder_id = ? AND event_date >= ? "
                    "AND recipient_key LIKE 'group:%'",
                    (reminder_id, (date.today() - timedelta(days=1)).isoformat()),
                )
            connection.execute(
                "UPDATE group_reminders SET body = ?, reminder_days = ?, "
                "reminder_time = ?, enabled = ? WHERE id = ?",
                (
                    new_body.strip() if new_body else None,
                    new_days,
                    new_time,
                    int(new_enabled),
                    reminder_id,
                ),
            )
            self._audit(
                connection,
                actor_id,
                "group_reminder_updated",
                row["service_id"],
                {
                    "reminder_id": reminder_id,
                    "reminder_days": new_days,
                    "reminder_time": new_time,
                    "has_custom_body": new_body is not None,
                    "enabled": new_enabled,
                },
            )

    def delete_group_reminder(self, actor_id: int, reminder_id: int) -> None:
        _validate_positive_id(actor_id, "actor_id")
        _validate_positive_id(reminder_id, "reminder_id")
        with self._connection() as connection:
            row = connection.execute(
                "SELECT service_id FROM group_reminders WHERE id = ?",
                (reminder_id,),
            ).fetchone()
            if row is None:
                raise ValueError("Групповое напоминание больше не существует.")
            self._require_service_editor(
                connection, actor_id, row["service_id"]
            )
            connection.execute(
                "DELETE FROM group_reminders WHERE id = ?", (reminder_id,)
            )
            self._audit(
                connection,
                actor_id,
                "group_reminder_deleted",
                row["service_id"],
                {"reminder_id": reminder_id},
            )

    def merge_group_reminder(
        self,
        actor_id: int,
        target_id: int,
        body=_UNSET,
        *,
        expected_slot: tuple[int, str],
        source_id: int | None = None,
        source_slot: tuple[int, str] | None = None,
    ) -> None:
        """Объединяет напоминания с одинаковым расписанием в target_id.

        body — итоговый текст target (по умолчанию не меняется); source_id —
        напоминание, которое удаляется в той же транзакции. expected_slot и
        source_slot — расписания, показанные пользователю: если их успели
        изменить, объединение отклоняется. Отметки доставки source
        переносятся на target, чтобы уже отправленное не ушло повторно.
        """
        _validate_positive_id(actor_id, "actor_id")
        _validate_positive_id(target_id, "target_id")
        if source_id is not None:
            _validate_positive_id(source_id, "source_id")
            if source_id == target_id:
                raise ValueError("Нельзя объединить напоминание с самим собой.")
            if source_slot is None:
                raise ValueError("Не указано расписание объединяемого напоминания.")
        stale = ValueError(
            "Напоминания изменились. Откройте список напоминаний заново."
        )
        with self._connection() as connection:
            target = connection.execute(
                "SELECT * FROM group_reminders WHERE id = ?", (target_id,)
            ).fetchone()
            if target is None:
                raise ValueError("Групповое напоминание больше не существует.")
            self._require_service_editor(
                connection, actor_id, target["service_id"]
            )
            if (
                (target["reminder_days"], target["reminder_time"])
                != tuple(expected_slot)
                or not target["enabled"]
            ):
                raise stale
            if source_id is not None:
                source = connection.execute(
                    "SELECT service_id, reminder_days, reminder_time, task, "
                    "active_from, active_until FROM group_reminders WHERE id = ?",
                    (source_id,),
                ).fetchone()
                if source is None or source["service_id"] != target["service_id"]:
                    raise ValueError(
                        "Объединяемое напоминание больше не существует."
                    )
                if (
                    (source["reminder_days"], source["reminder_time"])
                    != tuple(source_slot)
                    # Напоминания разных задач не объединяются.
                    or source["task"] != target["task"]
                ):
                    raise stale
                if (source["active_from"], source["active_until"]) != (
                    target["active_from"], target["active_until"]
                ):
                    raise ValueError(
                        "У напоминаний разные периоды — объединить их нельзя. "
                        "Сохраните отдельно или выровняйте период."
                    )
            new_body = target["body"] if body is _UNSET else body
            self._validate_group_reminder_values(
                target["reminder_days"], target["reminder_time"],
                new_body if body is not _UNSET else None, target["task"],
            )
            connection.execute(
                "UPDATE group_reminders SET body = ? WHERE id = ?",
                (new_body.strip() if new_body else None, target_id),
            )
            if source_id is not None:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO group_reminder_deliveries (
                        reminder_id, event_date, recipient_key, delivered_at
                    )
                    SELECT ?, event_date, recipient_key, delivered_at
                    FROM group_reminder_deliveries WHERE reminder_id = ?
                    """,
                    (target_id, source_id),
                )
                connection.execute(
                    "DELETE FROM group_reminders WHERE id = ?", (source_id,)
                )
            self._audit(
                connection,
                actor_id,
                "group_reminder_merged",
                target["service_id"],
                {
                    "reminder_id": target_id,
                    "merged_reminder_id": source_id,
                    "has_custom_body": new_body is not None,
                },
            )

    def resolve_group_text(self, reminder: dict, service: dict) -> str:
        if reminder.get("body") is not None:
            return reminder["body"]
        if reminder.get("task"):
            return DEFAULT_TASK_TEMPLATE
        return service.get("group_template") or DEFAULT_GROUP_TEMPLATE

    def set_admin_report_template(
        self, actor_id: int, service_id: str, template: str | None
    ) -> None:
        """Шаблон отчёта админам служения; None — встроенный."""
        _validate_positive_id(actor_id, "actor_id")
        if template is not None:
            if not isinstance(template, str) or not template.strip():
                raise ValueError("Шаблон отчёта не может быть пустым")
            validate_notification_template(
                template, ADMIN_REPORT_FIELDS, "Шаблон отчёта админам"
            )
        with self._connection() as connection:
            self._require_service(connection, service_id)
            self._require_service_editor(connection, actor_id, service_id)
            connection.execute(
                "UPDATE services SET admin_report_template = ? WHERE id = ?",
                (template.strip() if template else None, service_id),
            )
            self._audit(
                connection, actor_id, "admin_report_template_updated",
                service_id, {"has_custom_template": template is not None},
            )

    def linked_task_by_token(
        self, service_id: str, telegram_id: int, token: str
    ) -> str | None:
        """Задача служения с этим task_token, к которой привязан человек."""
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT DISTINCT task FROM responsible_contacts "
                "WHERE service_id = ? AND telegram_id = ?",
                (service_id, telegram_id),
            ).fetchall()
        return next(
            (row["task"] for row in rows if task_token(row["task"]) == token),
            None,
        )

    def set_confirmation(
        self,
        service_id: str,
        event_date: str,
        task: str,
        telegram_id: int,
        status: str,
        name: str | None = None,
    ) -> tuple[str, bool]:
        """Ответ человека на личное напоминание: (имя, ответ изменился).

        Ответить может только привязанный к задаче пользователь. name —
        имя в графике на эту дату (у заменяющего может отличаться от
        имени контакта); по умолчанию — имя контакта.
        """
        _validate_positive_id(telegram_id, "telegram_id")
        if status not in {"confirmed", "declined"}:
            raise ValueError("Некорректный ответ")
        date.fromisoformat(event_date)
        with self._connection() as connection:
            self._require_service(connection, service_id)
            contact = connection.execute(
                "SELECT name FROM responsible_contacts "
                "WHERE service_id = ? AND task = ? AND telegram_id = ?",
                (service_id, task, telegram_id),
            ).fetchone()
            if contact is None:
                raise PermissionError("Вы не привязаны к этой задаче.")
            cursor = connection.execute(
                """
                INSERT INTO service_confirmations (
                    service_id, event_date, task, telegram_id, name, status,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(service_id, event_date, task, telegram_id)
                DO UPDATE SET status = excluded.status,
                              name = excluded.name,
                              updated_at = excluded.updated_at
                WHERE service_confirmations.status != excluded.status
                """,
                (
                    service_id, event_date, task, telegram_id,
                    name or contact["name"], status, _utc_now(),
                ),
            )
        return name or contact["name"], cursor.rowcount > 0

    def get_confirmation(
        self, service_id: str, event_date: str, task: str, telegram_id: int
    ) -> str | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT status FROM service_confirmations "
                "WHERE service_id = ? AND event_date = ? AND task = ? "
                "AND telegram_id = ?",
                (service_id, event_date, task, telegram_id),
            ).fetchone()
        return row["status"] if row else None

    def list_confirmations(
        self, service_id: str, event_date: str
    ) -> dict[tuple[str, str], str]:
        """{(задача, Telegram ID): status} на дату события."""
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT task, telegram_id, status FROM service_confirmations "
                "WHERE service_id = ? AND event_date = ?",
                (service_id, event_date),
            ).fetchall()
        return {
            (row["task"], row["telegram_id"]): row["status"] for row in rows
        }

    def save_admin_report(
        self,
        service_id: str,
        event_date: str,
        admin_id: int,
        message_id: int,
        assignments: dict,
    ) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO admin_report_messages (
                    service_id, event_date, admin_id, message_id,
                    assignments_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(service_id, event_date, admin_id) DO UPDATE SET
                    message_id = excluded.message_id,
                    assignments_json = excluded.assignments_json,
                    updated_at = excluded.updated_at
                """,
                (
                    service_id, event_date, admin_id, message_id,
                    json.dumps(assignments, ensure_ascii=False, sort_keys=True),
                    _utc_now(),
                ),
            )

    def delete_admin_report(
        self, service_id: str, event_date: str, admin_id: int
    ) -> None:
        with self._connection() as connection:
            connection.execute(
                "DELETE FROM admin_report_messages WHERE service_id = ? "
                "AND event_date = ? AND admin_id = ?",
                (service_id, event_date, admin_id),
            )

    def list_admin_reports(self, service_id: str, event_date: str) -> list[dict]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT admin_id, message_id, assignments_json "
                "FROM admin_report_messages "
                "WHERE service_id = ? AND event_date = ? ORDER BY admin_id",
                (service_id, event_date),
            ).fetchall()
        return [
            {
                "admin_id": row["admin_id"],
                "message_id": row["message_id"],
                "assignments": json.loads(row["assignments_json"]),
            }
            for row in rows
        ]

    # --- Замена ответственного (этап 3.3) ---

    @staticmethod
    def _poll_row(row) -> dict | None:
        return dict(row) if row is not None else None

    def create_replacement_poll(
        self,
        poll_id: str,
        service_id: str,
        event_date: str,
        task: str,
        old_name: str,
        old_telegram_id: int | None,
        chat_id: int,
        message_id: int,
        notice_message_id: int | None,
        deadline: str,
        status: str = "open",
    ) -> None:
        """status="closed" — запрос без опроса (нет чата служения или опрос
        не отправился): ждёт /change."""
        date.fromisoformat(event_date)
        datetime.fromisoformat(deadline)
        if status not in {"open", "closed"}:
            raise ValueError("Некорректное состояние опроса")
        with self._connection() as connection:
            self._require_service(connection, service_id)
            connection.execute(
                """
                INSERT INTO replacement_polls (
                    poll_id, service_id, event_date, task, old_name,
                    old_telegram_id, chat_id, message_id, notice_message_id,
                    deadline, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    poll_id, service_id, event_date, task, old_name,
                    old_telegram_id, chat_id, message_id, notice_message_id,
                    deadline, status, _utc_now(),
                ),
            )

    def get_replacement_poll(self, poll_id: str) -> dict | None:
        with self._connection() as connection:
            return self._poll_row(connection.execute(
                "SELECT * FROM replacement_polls WHERE poll_id = ?",
                (poll_id,),
            ).fetchone())

    def find_active_replacement_request(
        self, service_id: str, event_date: str, task: str, old_name: str
    ) -> dict | None:
        """Невыполненный запрос замены: идёт опрос или ждёт /change."""
        with self._connection() as connection:
            return self._poll_row(connection.execute(
                "SELECT * FROM replacement_polls WHERE service_id = ? "
                "AND event_date = ? AND task = ? AND old_name = ? "
                "AND filled = 0",
                (service_id, event_date, task, old_name),
            ).fetchone())

    def replacement_poll_by_message(
        self, chat_id: int, message_id: int
    ) -> dict | None:
        """Опрос по сообщению опроса или по объявлению перед ним."""
        with self._connection() as connection:
            return self._poll_row(connection.execute(
                "SELECT * FROM replacement_polls WHERE chat_id = ? "
                "AND (message_id = ? OR notice_message_id = ?) "
                "ORDER BY created_at DESC LIMIT 1",
                (chat_id, message_id, message_id),
            ).fetchone())

    def list_unfilled_replacement_requests(self, since: str) -> list[dict]:
        """Запросы без назначенной замены на даты с since — для /change."""
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM replacement_polls WHERE filled = 0 "
                "AND event_date >= ? ORDER BY event_date, created_at",
                (since,),
            ).fetchall()
        return [dict(row) for row in rows]

    def claim_replacement_request(self, poll_id: str) -> bool:
        """Отмечает запрос выполненным; False — замену уже назначили."""
        with self._connection(notify=False) as connection:
            cursor = connection.execute(
                "UPDATE replacement_polls SET filled = 1, status = 'closed' "
                "WHERE poll_id = ? AND filled = 0",
                (poll_id,),
            )
        return cursor.rowcount > 0

    def release_replacement_request(self, poll_id: str) -> None:
        """Назначение не удалось — запрос снова ждёт /change."""
        with self._connection(notify=False) as connection:
            connection.execute(
                "UPDATE replacement_polls SET filled = 0 WHERE poll_id = ?",
                (poll_id,),
            )

    def list_open_replacement_polls(self) -> list[dict]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM replacement_polls WHERE status = 'open' "
                "ORDER BY deadline, poll_id"
            ).fetchall()
        return [dict(row) for row in rows]

    def close_replacement_poll(self, poll_id: str) -> bool:
        """True — опрос закрыт этим вызовом (защита от двойного итога)."""
        with self._connection() as connection:
            cursor = connection.execute(
                "UPDATE replacement_polls SET status = 'closed' "
                "WHERE poll_id = ? AND status = 'open'",
                (poll_id,),
            )
        return cursor.rowcount > 0

    def record_poll_answer(
        self,
        poll_id: str,
        telegram_id: int,
        username: str,
        full_name: str,
        can: bool | None,
    ) -> bool:
        """Ответ в опросе; can=None — голос отозван. False — опрос закрыт."""
        _validate_positive_id(telegram_id, "telegram_id")
        with self._connection(notify=False) as connection:
            poll = connection.execute(
                "SELECT status FROM replacement_polls WHERE poll_id = ?",
                (poll_id,),
            ).fetchone()
            if poll is None or poll["status"] != "open":
                return False
            if can is None:
                connection.execute(
                    "DELETE FROM replacement_poll_answers "
                    "WHERE poll_id = ? AND telegram_id = ?",
                    (poll_id, telegram_id),
                )
                return True
            connection.execute(
                """
                INSERT INTO replacement_poll_answers (
                    poll_id, telegram_id, username, full_name, can,
                    answered_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(poll_id, telegram_id) DO UPDATE SET
                    username = excluded.username,
                    full_name = excluded.full_name,
                    can = excluded.can,
                    answered_at = excluded.answered_at
                """,
                (
                    poll_id, telegram_id, (username or "").lstrip("@"),
                    full_name or "", int(bool(can)), _utc_now(),
                ),
            )
        return True

    def list_poll_answers(self, poll_id: str) -> list[dict]:
        """Ответы в порядке времени: первый «Могу» — первый в списке."""
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT telegram_id, username, full_name, can, answered_at "
                "FROM replacement_poll_answers WHERE poll_id = ? "
                "ORDER BY answered_at, rowid",
                (poll_id,),
            ).fetchall()
        return [
            {**dict(row), "can": bool(row["can"])} for row in rows
        ]

    def task_member_ids(self, service_id: str, task: str) -> set[int]:
        """Telegram ID людей с этой задачей: «Списки» и привязки задачи."""
        with self._connection() as connection:
            members = connection.execute(
                "SELECT telegram_id, tasks_json FROM service_members "
                "WHERE service_id = ? AND telegram_id IS NOT NULL",
                (service_id,),
            ).fetchall()
            contacts = connection.execute(
                "SELECT telegram_id FROM responsible_contacts "
                "WHERE service_id = ? AND task = ?",
                (service_id, task),
            ).fetchall()
        ids = {
            row["telegram_id"] for row in members
            if task in json.loads(row["tasks_json"])
        }
        ids.update(row["telegram_id"] for row in contacts)
        return ids

    def service_person(
        self, service_id: str, telegram_id: int, task: str | None = None
    ) -> dict | None:
        """Человек служения по Telegram ID: имя привязки к задаче, иначе из
        состава, иначе из любой привязки."""
        with self._connection() as connection:
            row = (task is not None and connection.execute(
                "SELECT name, telegram_id, username FROM responsible_contacts "
                "WHERE service_id = ? AND task = ? AND telegram_id = ?",
                (service_id, task, telegram_id),
            ).fetchone()) or connection.execute(
                "SELECT name, telegram_id, username FROM service_members "
                "WHERE service_id = ? AND telegram_id = ? "
                "ORDER BY in_list DESC, rowid LIMIT 1",
                (service_id, telegram_id),
            ).fetchone() or connection.execute(
                "SELECT name, telegram_id, username FROM responsible_contacts "
                "WHERE service_id = ? AND telegram_id = ? "
                "ORDER BY updated_at DESC LIMIT 1",
                (service_id, telegram_id),
            ).fetchone()
        return dict(row) if row else None

    def note_seen_username(self, telegram_id: int, username: str) -> None:
        """Бот видел @username у этого Telegram ID (сам пользователь, не
        правка админа). Прежний username этого ID забывается. Без username
        сохраняется отметка «~ID» (такого username не бывает): старые ответы
        на опрос этого ID с прежним username больше не в счёт."""
        _validate_positive_id(telegram_id, "telegram_id")
        username = (
            (username or "").strip().lstrip("@").casefold()
            or f"~{telegram_id}"
        )
        # От наблюдений не зависят задачи планировщика и резервная копия.
        with self._connection(notify=False) as connection:
            connection.execute(
                "DELETE FROM seen_usernames WHERE telegram_id = ? "
                "AND username != ?",
                (telegram_id, username),
            )
            connection.execute(
                "INSERT INTO seen_usernames (username, telegram_id, seen_at) "
                "VALUES (?, ?, ?) ON CONFLICT(username) DO UPDATE SET "
                "telegram_id = excluded.telegram_id, "
                "seen_at = excluded.seen_at",
                (username, telegram_id, _utc_now()),
            )

    def known_telegram_id(self, username: str) -> int | None:
        """Telegram ID по @username — только по наблюдениям бота: кто
        писал боту или отвечал на опрос с этим username (самое свежее).
        Записи, внесённые админом, не в счёт: username мог перейти к
        другому человеку."""
        username = (username or "").strip().lstrip("@").casefold()
        # «~ID» — отметка «без username», не имя.
        if not username or username.startswith("~"):
            return None
        with self._connection() as connection:
            row = connection.execute(
                "SELECT telegram_id FROM ("
                "SELECT telegram_id, seen_at AS seen FROM seen_usernames "
                "WHERE username = :name "
                # Ответ на опрос не в счёт, если позже бот видел у этого
                # ID другой username (человек сменил его).
                "UNION ALL SELECT telegram_id, answered_at FROM "
                "readiness_answers AS answer WHERE lower(username) = :name "
                "AND NOT EXISTS (SELECT 1 FROM seen_usernames AS seen "
                "WHERE seen.telegram_id = answer.telegram_id "
                "AND seen.username != :name "
                "AND seen.seen_at > answer.answered_at)"
                ") ORDER BY seen DESC LIMIT 1",
                {"name": username},
            ).fetchone()
        return row["telegram_id"] if row else None

    def add_username_binding(
        self, actor_id: int, service_id: str, name: str, task: str,
        username: str, tasks=(),
    ) -> None:
        """Отложенная привязка «имя (задача) → @username»."""
        _validate_positive_id(actor_id, "actor_id")
        name = name.strip() if isinstance(name, str) else ""
        username = (username or "").strip().lstrip("@")
        if not name or not re.fullmatch(
            r"[A-Za-z][A-Za-z0-9_]{4,31}", username
        ):
            raise ValueError("Нужны имя и корректный @username.")
        with self._connection() as connection:
            self._require_service_editor(connection, actor_id, service_id)
            self._require_service(connection, service_id)
            # Новая привязка человека заменяет прежние ожидания (любой
            # задачи): иначе старый username мог бы прийти первым.
            self._drop_username_binding(connection, service_id, name)
            connection.execute(
                "INSERT INTO username_bindings (service_id, name, task, "
                "username, tasks_json, actor_id, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(service_id, name, task) DO UPDATE SET "
                "username = excluded.username, "
                "tasks_json = excluded.tasks_json, "
                "actor_id = excluded.actor_id, "
                "created_at = excluded.created_at",
                (
                    service_id, name, task or "", username,
                    json.dumps(list(tasks), ensure_ascii=False), actor_id,
                    _utc_now(),
                ),
            )
            self._audit(
                connection, actor_id, "username_binding_added", service_id,
                {"name": name, "task": task or "", "username": username},
            )

    def username_binding(self, service_id: str, name: str) -> str | None:
        """@username, который ждёт привязки к человеку (любая задача)."""
        with self._connection() as connection:
            row = connection.execute(
                "SELECT username FROM username_bindings "
                "WHERE service_id = ? AND name = ? AND created_at >= ? "
                "ORDER BY task LIMIT 1",
                (service_id, name, _username_binding_cutoff()),
            ).fetchone()
        return row["username"] if row else None

    def pending_usernames(self) -> set[str]:
        with self._connection() as connection:
            return {
                row["username"].casefold()
                for row in connection.execute(
                    "SELECT username FROM username_bindings "
                    "WHERE created_at >= ?",
                    (_username_binding_cutoff(),),
                )
            }

    def take_username_bindings(self, username: str) -> list[dict]:
        """Забирает (и удаляет) отложенные привязки этого @username."""
        username = (username or "").strip().lstrip("@").casefold()
        if not username:
            return []
        with self._connection() as connection:
            # Просроченные ожидания — опечатка или чужой username — не
            # привязываются: удаляются вместе со взятыми.
            rows = connection.execute(
                "SELECT service_id, name, task, username, tasks_json, actor_id "
                "FROM username_bindings WHERE lower(username) = ? "
                "AND created_at >= ? ORDER BY service_id, name, task",
                (username, _username_binding_cutoff()),
            ).fetchall()
            connection.execute(
                "DELETE FROM username_bindings WHERE lower(username) = ?",
                (username,),
            )
        return [
            {
                "service_id": row["service_id"], "name": row["name"],
                "task": row["task"], "username": row["username"],
                "tasks": json.loads(row["tasks_json"]),
                "actor_id": row["actor_id"],
            }
            for row in rows
        ]

    @staticmethod
    def _drop_username_binding(
        connection: sqlite3.Connection, service_id: str, name: str,
    ) -> None:
        """Человек привязан (или убран) — ожидание по @username не нужно."""
        connection.execute(
            "DELETE FROM username_bindings WHERE service_id = ? AND name = ?",
            (service_id, name),
        )

    def find_service_person_by_username(
        self, service_id: str, username: str
    ) -> dict | None:
        """@username в составе служения или его привязках (без учёта регистра)."""
        username = (username or "").strip().lstrip("@").casefold()
        if not username:
            return None
        with self._connection() as connection:
            self._require_service(connection, service_id)
            row = connection.execute(
                "SELECT name, telegram_id, username FROM service_members "
                "WHERE service_id = ? AND telegram_id IS NOT NULL "
                "AND lower(username) = ? ORDER BY in_list DESC, rowid LIMIT 1",
                (service_id, username),
            ).fetchone() or connection.execute(
                "SELECT name, telegram_id, username FROM responsible_contacts "
                "WHERE service_id = ? AND lower(username) = ? "
                "ORDER BY updated_at DESC LIMIT 1",
                (service_id, username),
            ).fetchone()
        return dict(row) if row else None

    def set_replacement(
        self,
        service_id: str,
        event_date: str,
        task: str,
        old_name: str,
        new_name: str,
        telegram_id: int | None,
        username: str,
        source: str,
        created_by: int | None,
        expect_current: bool = False,
    ) -> bool:
        """Замена на одну дату.

        expect_current — заменённый ещё стоит в задаче (его не заменили,
        пока админ выбирал); иначе возвращается False. Заменяющий из
        состава служения (есть в составе или привязках) получает привязку к
        задаче под этим именем — для упоминаний и личных напоминаний.
        Участник чата не из состава указывается только по имени."""
        date.fromisoformat(event_date)
        if source not in {"poll", "admin"}:
            raise ValueError("Некорректный источник замены")
        if not new_name or not old_name or new_name == old_name:
            raise ValueError("Некорректная замена")
        if telegram_id is not None:
            _validate_positive_id(telegram_id, "telegram_id")
        username = (username or "").strip().lstrip("@")
        now = _utc_now()
        with self._connection() as connection:
            # Проверка и запись — одной транзакцией: два подтверждения
            # подряд не назначат замену дважды.
            connection.execute("BEGIN IMMEDIATE")
            self._require_service(connection, service_id)
            if expect_current:
                rows = connection.execute(
                    "SELECT task, old_name, new_name FROM service_replacements "
                    "WHERE service_id = ? AND event_date = ? "
                    "ORDER BY created_at, rowid",
                    (service_id, event_date),
                ).fetchall()
                if is_replaced([dict(row) for row in rows], task, old_name):
                    return False
            connection.execute(
                """
                INSERT INTO service_replacements (
                    service_id, event_date, task, old_name, new_name,
                    telegram_id, username, source, created_by, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(service_id, event_date, task, old_name)
                DO UPDATE SET new_name = excluded.new_name,
                              telegram_id = excluded.telegram_id,
                              username = excluded.username,
                              source = excluded.source,
                              created_by = excluded.created_by,
                              created_at = excluded.created_at
                """,
                (
                    service_id, event_date, task, old_name, new_name,
                    telegram_id, username, source, created_by, now,
                ),
            )
            known = telegram_id is not None and connection.execute(
                "SELECT 1 FROM service_members WHERE service_id = ? "
                "AND telegram_id = ? UNION SELECT 1 FROM responsible_contacts "
                "WHERE service_id = ? AND telegram_id = ? LIMIT 1",
                (service_id, telegram_id, service_id, telegram_id),
            ).fetchone() is not None
            if known:
                actor = created_by or connection.execute(
                    "SELECT telegram_id FROM super_admins "
                    "ORDER BY created_at LIMIT 1"
                ).fetchone()["telegram_id"]
                connection.execute(
                    """
                    INSERT OR IGNORE INTO responsible_contacts (
                        service_id, task, name, telegram_id, username,
                        updated_by, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        service_id, task, new_name, telegram_id, username,
                        actor, now,
                    ),
                )
        return True

    def list_replacements(self, service_id: str, event_date: str) -> list[dict]:
        """Замены на дату в порядке назначения (цепочки применяются по
        очереди)."""
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT task, old_name, new_name, telegram_id, username, "
                "source FROM service_replacements "
                "WHERE service_id = ? AND event_date = ? "
                "ORDER BY created_at, rowid",
                (service_id, event_date),
            ).fetchall()
        return [dict(row) for row in rows]

    def set_group_reminder_period(
        self,
        actor_id: int,
        reminder_id: int,
        active_from: date | None,
        active_until: date | None,
        *,
        today: date,
    ) -> None:
        """Период напоминания по датам воскресений «с — по» (этап 4.2).

        today — сегодняшняя дата в часовом поясе бота.

        None — без границы. Вне периода напоминание не отправляется,
        после окончания выключается (disable_expired_group_reminders)."""
        _validate_positive_id(actor_id, "actor_id")
        _validate_positive_id(reminder_id, "reminder_id")
        for bound in (active_from, active_until):
            if bound is not None and (
                not isinstance(bound, date) or bound.weekday() != 6
            ):
                raise ValueError("Границы периода — даты воскресений.")
        if active_from and active_until and active_from > active_until:
            raise ValueError("Начало периода позже окончания.")
        with self._connection() as connection:
            row = connection.execute(
                f"SELECT {_GROUP_REMINDER_COLUMNS} "
                f"FROM {_GROUP_REMINDER_SOURCE} WHERE g.id = ?",
                (reminder_id,),
            ).fetchone()
            if row is None:
                raise ValueError("Групповое напоминание больше не существует.")
            self._require_service_editor(connection, actor_id, row["service_id"])
            # Неделя окончания идёт до дня задачи (вс +5 — пятница после).
            if active_until is not None and active_until + timedelta(
                days=reminder_day_offset(dict(row))
            ) < today:
                raise ValueError("Окончание периода уже прошло.")
            values = (
                active_from.isoformat() if active_from else None,
                active_until.isoformat() if active_until else None,
            )
            if row["enabled"]:
                # Новый период не должен пересечься с включённым напоминанием
                # той же задачи на то же время.
                self._ensure_group_schedule_unique(
                    connection, row["service_id"], row["reminder_days"],
                    row["reminder_time"], False, excluding_id=reminder_id,
                    enabled_only=True, task=row["task"],
                    period={"active_from": values[0], "active_until": values[1]},
                )
            connection.execute(
                "UPDATE group_reminders SET active_from = ?, active_until = ? "
                "WHERE id = ?",
                (*values, reminder_id),
            )
            self._audit(
                connection, actor_id, "group_reminder_period",
                row["service_id"],
                {
                    "reminder_id": reminder_id,
                    "active_from": values[0],
                    "active_until": values[1],
                },
            )

    def disable_expired_group_reminders(self, today: date) -> list[int]:
        """Выключает напоминания, чей период закончился до today. У задачи
        со своим днём (вс +1) последняя неделя кончается днём задачи."""
        with self._connection() as connection:
            ids = [
                row["id"]
                for row in connection.execute(
                    f"SELECT {_GROUP_REMINDER_COLUMNS} "
                    f"FROM {_GROUP_REMINDER_SOURCE} WHERE g.enabled = 1 "
                    "AND g.active_until IS NOT NULL",
                )
                if date.fromisoformat(row["active_until"]) + timedelta(
                    days=reminder_day_offset(dict(row))
                ) < today
            ]
            connection.executemany(
                "UPDATE group_reminders SET enabled = 0 WHERE id = ?",
                [(reminder_id,) for reminder_id in ids],
            )
        return ids

    @staticmethod
    def _require_reminder_task(
        connection: sqlite3.Connection, service_id: str, task: str | None,
    ) -> None:
        if task is not None and connection.execute(
            "SELECT 1 FROM service_tasks "
            "WHERE service_id = ? AND name = ? AND enabled = 1",
            (service_id, task),
        ).fetchone() is None:
            raise ValueError(f"Задача «{task}» не из списка служения.")

    def set_group_reminder_task(
        self, actor_id: int, reminder_id: int, task: str | None
    ) -> None:
        """Задача напоминания: только её ответственные; None — все задачи."""
        _validate_positive_id(actor_id, "actor_id")
        _validate_positive_id(reminder_id, "reminder_id")
        with self._connection() as connection:
            row = connection.execute(
                "SELECT service_id, task, enabled, reminder_days, reminder_time, "
                "active_from, active_until FROM group_reminders WHERE id = ?",
                (reminder_id,),
            ).fetchone()
            if row is None:
                raise ValueError("Групповое напоминание больше не существует.")
            self._require_service_editor(connection, actor_id, row["service_id"])
            self._require_reminder_task(connection, row["service_id"], task)
            if task is None:
                body = connection.execute(
                    "SELECT body FROM group_reminders WHERE id = ?",
                    (reminder_id,),
                ).fetchone()["body"]
                if body is not None:
                    self._validate_group_reminder_values(
                        row["reminder_days"], row["reminder_time"], body
                    )
            if row["enabled"]:
                # Иначе в один слот попали бы два включённых напоминания
                # одной задачи (или два общих).
                self._ensure_group_schedule_unique(
                    connection,
                    row["service_id"],
                    row["reminder_days"],
                    row["reminder_time"],
                    False,
                    excluding_id=reminder_id,
                    enabled_only=True,
                    task=task,
                    period=dict(row),
                )
            connection.execute(
                "UPDATE group_reminders SET task = ? WHERE id = ?",
                (task, reminder_id),
            )
            self._audit(
                connection, actor_id, "group_reminder_task_updated",
                row["service_id"], {"reminder_id": reminder_id, "task": task},
            )

    def set_service_default_template(
        self, actor_id: int, service_id: str, template: str | None
    ) -> None:
        _validate_positive_id(actor_id, "actor_id")
        if template is not None:
            if not isinstance(template, str) or not template.strip():
                raise ValueError("Текст группового шаблона не может быть пустым")
            validate_notification_template(
                template,
                GENERAL_GROUP_TEMPLATE_FIELDS,
                "Шаблон группового уведомления",
            )
            template = template.strip()
        with self._connection() as connection:
            self._require_service(connection, service_id)
            self._require_service_editor(connection, actor_id, service_id)
            connection.execute(
                "UPDATE services SET group_template = ? WHERE id = ?",
                (template, service_id),
            )
            self._audit(
                connection,
                actor_id,
                "service_default_template_updated",
                service_id,
                {"is_custom": template is not None},
            )

    def set_service_personal_templates(
        self, actor_id: int, service_id: str, templates: dict[str, str]
    ) -> None:
        _validate_positive_id(actor_id, "actor_id")
        self._validate_personal_templates(templates)
        with self._connection() as connection:
            self._require_service(connection, service_id)
            self._require_service_editor(connection, actor_id, service_id)
            connection.execute(
                "UPDATE services SET personal_templates_json = ? WHERE id = ?",
                (
                    json.dumps(templates, ensure_ascii=False, sort_keys=True),
                    service_id,
                ),
            )
            self._audit(
                connection,
                actor_id,
                "service_personal_templates_updated",
                service_id,
                {"templates": templates},
            )

    @staticmethod
    def _validate_group_reminder_values(
        days: int, time: str, body: str | None, task: str | None = None
    ) -> None:
        if isinstance(days, bool) or not isinstance(days, int) or not 0 <= days <= 6:
            raise ValueError("Число дней должно быть от 0 до 6.")
        if not isinstance(time, str) or not re.fullmatch(
            r"(?:[01]\d|2[0-3]):[0-5]\d", time
        ):
            raise ValueError("Время должно быть в формате HH:MM.")
        if body is not None:
            if not isinstance(body, str) or not body.strip():
                raise ValueError("Текст группового напоминания не может быть пустым.")
            validate_notification_template(
                body,
                GROUP_TEMPLATE_FIELDS if task else GENERAL_GROUP_TEMPLATE_FIELDS,
                "Текст группового напоминания",
            )

    @staticmethod
    def _ensure_group_schedule_unique(
        connection: sqlite3.Connection,
        service_id: str,
        days: int,
        time: str,
        allow_duplicate: bool,
        *,
        excluding_id: int | None = None,
        enabled_only: bool = False,
        task: str | None = None,
        period: dict | None = None,
    ) -> None:
        # Напоминания разных задач в один слот не пересекаются (этап 3.1),
        # как и напоминания с непересекающимися периодами (этап 4.2).
        query = (
            "SELECT active_from, active_until FROM group_reminders "
            "WHERE service_id = ? "
            "AND reminder_days = ? AND reminder_time = ? AND task IS ?"
        )
        parameters: tuple = (service_id, days, time, task)
        if enabled_only:
            query += " AND enabled = 1"
        if excluding_id is not None:
            query += " AND id != ?"
            parameters += (excluding_id,)
        if not allow_duplicate and any(
            periods_overlap(dict(row), period or {})
            for row in connection.execute(query, parameters)
        ):
            raise ValueError(
                f"Напоминание за {days} дн. в {time} уже задано."
            )

    @staticmethod
    def _validate_personal_templates(
        templates: dict[str, str],
    ) -> None:
        if not isinstance(templates, dict) or any(
            not isinstance(task, str)
            or not isinstance(text, str)
            or not task.strip()
            or not text.strip()
            for task, text in templates.items()
        ):
            raise ValueError("Шаблоны личных сообщений должны быть непустыми")
        for task, template in templates.items():
            validate_notification_template(
                template,
                PERSONAL_TEMPLATE_FIELDS,
                f"Личный шаблон для задачи «{task}»",
            )

    def set_notification_settings(
        self,
        actor_id: int,
        service_id: str,
        *,
        reminder_days: int,
        reminder_time: str,
        group_template: str | None,
        personal_templates: dict[str, str],
        reminders: list[dict] | None = None,
        allow_duplicate_reminders: bool = False,
    ) -> None:
        """Deprecated compatibility wrapper; use the dedicated setters."""
        _validate_positive_id(actor_id, "actor_id")
        self._validate_group_reminder_values(
            reminder_days, reminder_time, None
        )
        if group_template is not None:
            if not isinstance(group_template, str) or not group_template.strip():
                raise ValueError("Текст группового уведомления не может быть пустым")
            validate_notification_template(
                group_template,
                GENERAL_GROUP_TEMPLATE_FIELDS,
                "Шаблон группового уведомления",
            )
        self._validate_personal_templates(personal_templates)
        normalized_reminders = (
            self._validate_reminders(
                reminders,
                allow_duplicates=allow_duplicate_reminders,
            )
            if reminders is not None
            else None
        )
        normalized_group_template = (
            group_template.strip() if group_template is not None else None
        )
        with self._connection() as connection:
            self._require_service(connection, service_id)
            self._require_service_editor(connection, actor_id, service_id)
            connection.execute(
                "UPDATE services SET reminder_days = ?, reminder_time = ?, "
                "group_template = ?, personal_templates_json = ? WHERE id = ?",
                (
                    reminder_days,
                    reminder_time,
                    normalized_group_template,
                    json.dumps(
                        personal_templates,
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    service_id,
                ),
            )
            self._audit(
                connection,
                actor_id,
                "service_default_template_updated",
                service_id,
                {"is_custom": normalized_group_template is not None},
            )
            self._audit(
                connection,
                actor_id,
                "service_personal_templates_updated",
                service_id,
                {"templates": personal_templates},
            )
            if normalized_reminders is None:
                primary = connection.execute(
                    "SELECT id FROM group_reminders WHERE service_id = ? "
                    "AND task IS NULL ORDER BY id LIMIT 1",
                    (service_id,),
                ).fetchone()
                if primary is None:
                    connection.execute(
                        "INSERT INTO group_reminders "
                        "(service_id, body, reminder_days, reminder_time) "
                        "VALUES (?, NULL, ?, ?)",
                        (service_id, reminder_days, reminder_time),
                    )
                else:
                    self._ensure_group_schedule_unique(
                        connection,
                        service_id,
                        reminder_days,
                        reminder_time,
                        allow_duplicate_reminders,
                        excluding_id=primary["id"],
                    )
                    connection.execute(
                        "UPDATE group_reminders SET reminder_days = ?, "
                        "reminder_time = ? WHERE id = ?",
                        (reminder_days, reminder_time, primary["id"]),
                    )
            else:
                self._replace_group_reminders(
                    connection,
                    service_id,
                    normalized_reminders,
                    allow_duplicate=allow_duplicate_reminders,
                )

    def _replace_group_reminders(
        self,
        connection: sqlite3.Connection,
        service_id: str,
        reminders: list[dict],
        *,
        allow_duplicate: bool,
    ) -> None:
        existing_rows = connection.execute(
            "SELECT id, reminder_days, reminder_time, task FROM group_reminders "
            "WHERE service_id = ?",
            (service_id,),
        ).fetchall()
        existing = {row["id"] for row in existing_rows}
        task_by_id = {row["id"]: row["task"] for row in existing_rows}
        final_ids = {
            item["id"] for item in reminders if item["id"] is not None
        }
        if final_ids - existing:
            raise ValueError("Одно из уведомлений устарело; обновите настройки.")
        if not allow_duplicate:
            seen = set()
            for item in reminders:
                key = (
                    item["reminder_days"],
                    item["reminder_time"],
                    task_by_id.get(item["id"]),
                )
                if key in seen:
                    raise ValueError(
                        f"Уведомление за {key[0]} дн. в {key[1]} уже задано."
                    )
                seen.add(key)
        removed = existing - final_ids
        if removed:
            placeholders = ",".join("?" for _ in removed)
            connection.execute(
                "DELETE FROM group_reminders WHERE service_id = ? "
                f"AND id IN ({placeholders})",
                (service_id, *sorted(removed)),
            )
        for reminder in reminders:
            if reminder["id"] is None:
                connection.execute(
                    "INSERT INTO group_reminders "
                    "(service_id, body, reminder_days, reminder_time) "
                    "VALUES (?, NULL, ?, ?)",
                    (
                        service_id,
                        reminder["reminder_days"],
                        reminder["reminder_time"],
                    ),
                )
                continue
            connection.execute(
                "UPDATE group_reminders SET reminder_days = ?, "
                "reminder_time = ? WHERE id = ?",
                (
                    reminder["reminder_days"],
                    reminder["reminder_time"],
                    reminder["id"],
                ),
            )

    def is_group_reminder_delivered(
        self,
        reminder_id: int,
        event_date: str,
        recipient_key: str,
    ) -> bool:
        _validate_positive_id(reminder_id, "reminder_id")
        with self._connection() as connection:
            return connection.execute(
                "SELECT 1 FROM group_reminder_deliveries "
                "WHERE reminder_id = ? AND event_date = ? "
                "AND recipient_key = ?",
                (reminder_id, event_date, recipient_key),
            ).fetchone() is not None

    def mark_group_reminder_delivered(
        self,
        reminder_id: int,
        event_date: str,
        recipient_key: str,
    ) -> None:
        _validate_positive_id(reminder_id, "reminder_id")
        with self._connection() as connection:
            if connection.execute(
                "SELECT 1 FROM group_reminders WHERE id = ?",
                (reminder_id,),
            ).fetchone() is None:
                raise ValueError("Групповое напоминание не найдено.")
            connection.execute(
                "INSERT OR IGNORE INTO group_reminder_deliveries "
                "(reminder_id, event_date, recipient_key, delivered_at) "
                "VALUES (?, ?, ?, ?)",
                (reminder_id, event_date, recipient_key, _utc_now()),
            )

    # Deprecated aliases retained for older integrations.
    def is_service_reminder_delivered(
        self,
        service_id: str,
        reminder_id: int,
        event_date: str,
        recipient_key: str,
    ) -> bool:
        reminder = self._resolve_group_reminder_id(service_id, reminder_id)
        return bool(
            reminder
            and self.is_group_reminder_delivered(
                reminder["id"], event_date, recipient_key
            )
        )

    def mark_service_reminder_delivered(
        self,
        service_id: str,
        reminder_id: int,
        event_date: str,
        recipient_key: str,
    ) -> None:
        reminder = self._resolve_group_reminder_id(service_id, reminder_id)
        if reminder is None:
            raise ValueError("Групповое напоминание не найдено.")
        self.mark_group_reminder_delivered(
            reminder["id"], event_date, recipient_key
        )

    def _resolve_group_reminder_id(
        self, service_id: str, reminder_id: int
    ) -> dict | None:
        _validate_positive_id(reminder_id, "reminder_id")
        with self._connection() as connection:
            legacy_mapping = connection.execute(
                "SELECT reminders.id, reminders.service_id, reminders.body, "
                "reminders.reminder_days, reminders.reminder_time, "
                "reminders.enabled, reminders.created_at "
                "FROM group_reminder_legacy_ids AS legacy "
                "JOIN group_reminders AS reminders "
                "ON reminders.id = legacy.reminder_id "
                "WHERE legacy.legacy_id = ? AND reminders.service_id = ?",
                (reminder_id, service_id),
            ).fetchone()
            if legacy_mapping is not None:
                return {
                    **dict(legacy_mapping),
                    "enabled": bool(legacy_mapping["enabled"]),
                }
            current = connection.execute(
                "SELECT id, service_id, body, reminder_days, reminder_time, "
                "enabled, created_at, task, active_from, active_until "
                "FROM group_reminders "
                "WHERE id = ? AND service_id = ?",
                (reminder_id, service_id),
            ).fetchone()
        return (
            {**dict(current), "enabled": bool(current["enabled"])}
            if current is not None
            else None
        )

    @staticmethod
    def _validate_reminders(
        reminders: list[dict], *, allow_duplicates: bool = False
    ) -> list[dict]:
        if not isinstance(reminders, list):
            raise ValueError("Настройки уведомлений имеют неверный формат.")
        normalized = []
        seen: set[tuple[int, str]] = set()
        seen_ids: set[int] = set()
        for reminder in reminders:
            if not isinstance(reminder, dict):
                raise ValueError("Настройка уведомления имеет неверный формат.")
            days = reminder.get("reminder_days")
            reminder_time = reminder.get("reminder_time")
            if isinstance(days, bool) or not isinstance(days, int) or days < 0:
                raise ValueError("Число дней должно быть целым числом от 0 до 6.")
            if days > 6:
                raise ValueError("Число дней должно быть целым числом от 0 до 6.")
            if not isinstance(reminder_time, str) or not re.fullmatch(
                r"(?:[01]\d|2[0-3]):[0-5]\d", reminder_time
            ):
                raise ValueError("Время уведомления должно быть в формате HH:MM.")
            key = (days, reminder_time)
            if key in seen and not allow_duplicates:
                raise ValueError(
                    f"Уведомление за {days} дн. в {reminder_time} уже задано."
                )
            seen.add(key)
            reminder_id = reminder.get("id")
            if reminder_id is not None and (
                isinstance(reminder_id, bool)
                or not isinstance(reminder_id, int)
                or reminder_id <= 0
            ):
                raise ValueError("Идентификатор уведомления некорректен.")
            if reminder_id is not None and reminder_id in seen_ids:
                raise ValueError("Идентификатор уведомления указан повторно.")
            if reminder_id is not None:
                seen_ids.add(reminder_id)
            normalized.append(
                {
                    "id": reminder_id,
                    "reminder_days": days,
                    "reminder_time": reminder_time,
                }
            )
        return normalized

    @staticmethod
    def _replace_service_reminders(
        connection: sqlite3.Connection,
        service_id: str,
        reminders: list[dict],
    ) -> None:
        """Deprecated compatibility helper operating on the new table."""
        existing_rows = connection.execute(
            "SELECT id, reminder_days, reminder_time "
            "FROM group_reminders WHERE service_id = ?",
            (service_id,),
        ).fetchall()
        existing = {row["id"] for row in existing_rows}
        requested_ids = {
            reminder["id"]
            for reminder in reminders
            if reminder["id"] is not None
        }
        if requested_ids - existing:
            raise ValueError("Одно из уведомлений устарело; обновите настройки.")
        final_reminders = []
        for reminder in reminders:
            if reminder["id"] is None:
                cursor = connection.execute(
                    "INSERT INTO group_reminders "
                    "(service_id, body, reminder_days, reminder_time) "
                    "VALUES (?, NULL, ?, ?)",
                    (
                        service_id,
                        reminder["reminder_days"],
                        reminder["reminder_time"],
                    ),
                )
                final_reminders.append({**reminder, "id": cursor.lastrowid})
            else:
                final_reminders.append(reminder)

        final_by_id = {
            reminder["id"]: reminder for reminder in final_reminders
        }
        final_ids = set(final_by_id)
        existing_by_id = {row["id"]: row for row in existing_rows}
        changed_ids = set()
        for existing_row in existing_rows:
            reminder_id = existing_row["id"]
            final = final_by_id.get(reminder_id)
            old_schedule = (
                existing_row["reminder_days"],
                existing_row["reminder_time"],
            )
            unchanged = final is not None and (
                final["reminder_days"], final["reminder_time"]
            ) == old_schedule
            if unchanged:
                continue

            changed_ids.add(reminder_id)

        if changed_ids:
            placeholders = ",".join("?" for _ in changed_ids)
            original_deliveries = connection.execute(
                "SELECT reminder_id, event_date, recipient_key, delivered_at "
                "FROM group_reminder_deliveries "
                "WHERE reminder_id IN "
                f"({placeholders})",
                tuple(sorted(changed_ids)),
            ).fetchall()
            deliveries_by_source = {}
            for delivery in original_deliveries:
                deliveries_by_source.setdefault(
                    delivery["reminder_id"], []
                ).append(delivery)

            # Remove all changing histories before copying the original rows,
            # so a two-way schedule swap cannot consume rows copied earlier.
            connection.execute(
                "DELETE FROM group_reminder_deliveries "
                "WHERE reminder_id IN "
                f"({placeholders})",
                tuple(sorted(changed_ids)),
            )
            for reminder_id in changed_ids:
                old_row = existing_by_id[reminder_id]
                old_schedule = (
                    old_row["reminder_days"],
                    old_row["reminder_time"],
                )
                survivors = [
                    reminder
                    for reminder in final_reminders
                    if (
                        reminder["reminder_days"],
                        reminder["reminder_time"],
                    ) == old_schedule
                ]
                for survivor in survivors:
                    for delivery in deliveries_by_source.get(reminder_id, ()):
                        connection.execute(
                            """
                            INSERT OR IGNORE INTO group_reminder_deliveries (
                                reminder_id, event_date, recipient_key,
                                delivered_at
                            ) VALUES (?, ?, ?, ?)
                            """,
                            (
                                survivor["id"],
                                delivery["event_date"],
                                delivery["recipient_key"],
                                delivery["delivered_at"],
                            ),
                        )

        for reminder in final_reminders:
            existing_row = existing_by_id.get(reminder["id"])
            if existing_row is None:
                continue
            if (
                existing_row["reminder_days"] != reminder["reminder_days"]
                or existing_row["reminder_time"] != reminder["reminder_time"]
            ):
                connection.execute(
                    "UPDATE group_reminders "
                    "SET reminder_days = ?, reminder_time = ? "
                    "WHERE service_id = ? AND id = ?",
                    (
                        reminder["reminder_days"],
                        reminder["reminder_time"],
                        service_id,
                        reminder["id"],
                    ),
                )
        removed_ids = existing - final_ids
        if removed_ids:
            placeholders = ",".join("?" for _ in removed_ids)
            connection.execute(
                "DELETE FROM group_reminders WHERE service_id = ? "
                f"AND id IN ({placeholders})",
                (service_id, *sorted(removed_ids)),
            )

    def set_responsible_contact(
        self,
        actor_id: int,
        service_id: str,
        task: str,
        name: str,
        telegram_id: int,
        username: str = "",
    ) -> None:
        """Привязывает Telegram к имени в задаче и ко всем задачам этого имени.

        Привязка — к человеку: прежний Telegram ID этого имени в других
        задачах заменяется, чтобы у человека не осталось двух ID.
        """
        _validate_positive_id(actor_id, "actor_id")
        _validate_positive_id(telegram_id, "telegram_id")
        task = task.strip()
        name = name.strip()
        if not task or not name:
            raise ValueError("Название задачи и имя ответственного обязательны")
        with self._connection() as connection:
            self._require_service_editor(connection, actor_id, service_id)
            self._require_service(connection, service_id)
            other_tasks = [
                row["task"]
                for row in connection.execute(
                    "SELECT DISTINCT task FROM responsible_contacts "
                    "WHERE service_id = ? AND name = ? AND task != ?",
                    (service_id, name, task),
                )
            ]
            for contact_task in (task, *other_tasks):
                self._write_contact(
                    connection, actor_id, service_id, contact_task, name,
                    telegram_id, username.strip().lstrip("@"),
                )
            connection.execute(
                """
                INSERT INTO service_members (
                    service_id, name, telegram_id, username, source,
                    updated_at
                ) VALUES (?, ?, ?, ?, 'manual', ?)
                ON CONFLICT(service_id, name) DO UPDATE SET
                    telegram_id = excluded.telegram_id,
                    username = excluded.username,
                    updated_at = excluded.updated_at
                """,
                (
                    service_id, name, telegram_id,
                    username.strip().lstrip("@"), _utc_now(),
                ),
            )
            self._clear_member_exclusion(connection, service_id, name)
            self._drop_username_binding(connection, service_id, name)
            self._cleanup_unbound_user_data(connection, service_id)
            self._audit(
                connection, actor_id, "responsible_contact_updated", service_id,
                {"task": task, "name": name, "telegram_id": telegram_id},
            )

    @staticmethod
    def _write_contact(
        connection: sqlite3.Connection,
        actor_id: int,
        service_id: str,
        task: str,
        name: str,
        telegram_id: int,
        username: str,
    ) -> None:
        connection.execute(
            "DELETE FROM responsible_contacts "
            "WHERE service_id = ? AND task = ? "
            "AND (name = ? OR telegram_id = ?)",
            (service_id, task, name, telegram_id),
        )
        connection.execute(
            """
            INSERT INTO responsible_contacts (
                service_id, task, name, telegram_id, username,
                updated_by, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                service_id, task, name, telegram_id, username, actor_id,
                _utc_now(),
            ),
        )

    @staticmethod
    def _cleanup_unbound_user_data(
        connection: sqlite3.Connection, service_id: str
    ) -> None:
        """Удаляет личные настройки задач, к которым пользователь не привязан."""
        connection.execute(
            """
            DELETE FROM user_notification_prefs
            WHERE service_id = ? AND NOT EXISTS (
                SELECT 1 FROM responsible_contacts
                WHERE responsible_contacts.service_id = ?
                  AND responsible_contacts.telegram_id =
                          user_notification_prefs.actor_id
            )
            """,
            (service_id, service_id),
        )
        connection.execute(
            """
            DELETE FROM user_reminder_texts
            WHERE service_id = ? AND NOT EXISTS (
                SELECT 1 FROM responsible_contacts
                WHERE responsible_contacts.service_id =
                          user_reminder_texts.service_id
                  AND responsible_contacts.task = user_reminder_texts.task
                  AND responsible_contacts.telegram_id =
                          user_reminder_texts.actor_id
            )
            """,
            (service_id,),
        )

    def list_service_members(self, service_id: str) -> list[dict]:
        """Состав служения: сначала люди из «Списков», затем остальные."""
        with self._connection() as connection:
            self._require_service(connection, service_id)
            rows = connection.execute(
                "SELECT name, telegram_id, username, tasks_json, in_list, "
                "source, tasks_manual, day_offset, month_min, month_max, "
                "week_tasks_json FROM service_members "
                "WHERE service_id = ? ORDER BY in_list DESC, rowid",
                (service_id,),
            ).fetchall()
        return [
            {
                "name": row["name"],
                "telegram_id": row["telegram_id"],
                "username": row["username"],
                "tasks": json.loads(row["tasks_json"]),
                "in_list": bool(row["in_list"]),
                "source": row["source"],
                "tasks_manual": bool(row["tasks_manual"]),
                "day_offset": row["day_offset"],
                "month_limit": _member_limit(row),
                "week_tasks": _week_tasks(row),
            }
            for row in rows
        ]

    def sync_service_rosters(self, rosters) -> None:
        """Переносит состав служений с листа «Списки» (системная операция).

        rosters — {service_id: [RosterMember]}. Из файла берутся только
        имена и задачи; Telegram ID и username задаются только в боте. Люди,
        которых больше нет в «Списках», остаются с отметкой «нет в списке».
        Убранные из состава в боте не возвращаются.
        """
        now = _utc_now()
        with self._connection() as connection:
            active = {
                row["id"]
                for row in connection.execute(
                    "SELECT id FROM services WHERE archived = 0"
                )
            }
            for service_id, members in rosters.items():
                if service_id not in active:
                    continue
                excluded = {
                    row["name"]
                    for row in connection.execute(
                        "SELECT name FROM service_member_exclusions "
                        "WHERE service_id = ?",
                        (service_id,),
                    )
                }
                members = [
                    member for member in members if member.name not in excluded
                ]
                names = [member.name for member in members]
                for member in members:
                    connection.execute(
                        """
                        INSERT INTO service_members (
                            service_id, name, tasks_json, in_list, source,
                            updated_at
                        ) VALUES (?, ?, ?, 1, 'excel', ?)
                        ON CONFLICT(service_id, name) DO UPDATE SET
                            tasks_json = CASE
                                WHEN service_members.tasks_manual = 1
                                THEN service_members.tasks_json
                                ELSE excluded.tasks_json END,
                            in_list = 1
                        -- Без изменений строка не пишется: запись считается
                        -- изменением данных (бэкап, обновление задач).
                        -- Задачи, заданные в боте, файл не меняет.
                        WHERE (service_members.tasks_manual = 0
                               AND service_members.tasks_json
                                   IS NOT excluded.tasks_json)
                           OR service_members.in_list = 0
                        """,
                        (
                            service_id,
                            member.name,
                            json.dumps(list(member.tasks), ensure_ascii=False),
                            now,
                        ),
                    )
                placeholders = ", ".join("?" for _ in names) or "''"
                connection.execute(
                    "UPDATE service_members SET in_list = 0 "
                    "WHERE service_id = ? AND in_list = 1 "
                    f"AND name NOT IN ({placeholders})",
                    (service_id, *names),
                )
                manual = {
                    row["name"]
                    for row in connection.execute(
                        "SELECT name FROM service_members WHERE service_id = ? "
                        "AND tasks_manual = 1",
                        (service_id,),
                    )
                }
                for member in members:
                    if member.name in manual:
                        continue
                    self._ensure_member_task_contacts(
                        connection, service_id, member.name, member.tasks
                    )

    def bind_service_member(
        self,
        actor_id: int,
        service_id: str,
        name: str,
        telegram_id: int,
        username: str = "",
        tasks=(),
        *,
        from_readiness: bool = False,
    ) -> None:
        """Привязывает Telegram к человеку служения во всех его задачах.

        from_readiness — «➕ В состав» из опроса готовности и «➕ Другой
        человек из Telegram»: имя, уже занятое другим Telegram ID, не
        перезаписывается.

        Задачи — из «Списков», из прежних привязок этого имени и tasks
        (например, задачи из графика). Человек не из состава добавляется.
        """
        _validate_positive_id(actor_id, "actor_id")
        _validate_positive_id(telegram_id, "telegram_id")
        # Имя как в графике и «Списках» (без схлопывания пробелов), иначе
        # привязка не совпала бы с записью графика.
        name = name.strip() if isinstance(name, str) else ""
        if not name or "\x00" in name:
            raise ValueError("Имя участника обязательно.")
        username = username.strip().lstrip("@")
        with self._connection() as connection:
            # Админ служения — как и супер-администратор (кроме назначения
            # админов, всё по своему служению).
            self._require_service_editor(connection, actor_id, service_id)
            self._require_service(connection, service_id)
            member = connection.execute(
                "SELECT tasks_json, telegram_id, tasks_manual "
                "FROM service_members WHERE service_id = ? AND name = ?",
                (service_id, name),
            ).fetchone()
            # Имя уже у другого Telegram — в составе или в привязках
            # «👥 Ответственные»: чужие привязки не перезаписываются.
            if from_readiness and (
                (member is not None and member["telegram_id"] not in (
                    None, telegram_id,
                ))
                or connection.execute(
                    "SELECT 1 FROM responsible_contacts WHERE service_id = ? "
                    "AND name = ? AND telegram_id != ?",
                    (service_id, name, telegram_id),
                ).fetchone() is not None
            ):
                raise ValueError(
                    f"«{name}» в составе уже привязан к другому Telegram — "
                    "добавьте через «👥 Ответственные»."
                )
            now = _utc_now()
            if member is None:
                connection.execute(
                    "INSERT INTO service_members (service_id, name, "
                    "telegram_id, username, source, updated_at) "
                    "VALUES (?, ?, ?, ?, 'manual', ?)",
                    (service_id, name, telegram_id, username, now),
                )
                self._clear_member_exclusion(connection, service_id, name)
                roster_tasks = []
            else:
                # «➕ В состав» возвращает выпавшего из «Списков»: он снова
                # в составе — как добавленный в боте.
                connection.execute(
                    "UPDATE service_members SET telegram_id = ?, "
                    "username = ?, updated_at = ?, source = CASE "
                    "WHEN ? AND in_list = 0 THEN 'manual' ELSE source END "
                    "WHERE service_id = ? AND name = ?",
                    (
                        telegram_id, username, now, int(from_readiness),
                        service_id, name,
                    ),
                )
                roster_tasks = json.loads(member["tasks_json"])
            bound_tasks = [
                row["task"]
                for row in connection.execute(
                    "SELECT DISTINCT task FROM responsible_contacts "
                    "WHERE service_id = ? AND name = ?",
                    (service_id, name),
                )
            ]
            all_tasks = list(dict.fromkeys([*roster_tasks, *bound_tasks, *tasks]))
            if member is not None and member["tasks_manual"]:
                # Задачи заданы в боте — привязка только к ним (снятые
                # вручную не возвращаются из графика).
                all_tasks = roster_tasks
                placeholders = ", ".join("?" for _ in all_tasks) or "''"
                connection.execute(
                    "DELETE FROM responsible_contacts WHERE service_id = ? "
                    "AND name = ? AND telegram_id = ? "
                    f"AND task NOT IN ({placeholders})",
                    (service_id, name, telegram_id, *all_tasks),
                )
            for task in all_tasks:
                self._write_contact(
                    connection, actor_id, service_id, task, name,
                    telegram_id, username,
                )
            self._drop_username_binding(connection, service_id, name)
            self._cleanup_unbound_user_data(connection, service_id)
            self._audit(
                connection, actor_id, "service_member_bound", service_id,
                {"name": name, "telegram_id": telegram_id, "tasks": all_tasks},
            )

    def set_service_member_tasks(
        self, actor_id: int, service_id: str, name: str, tasks,
    ) -> list[str]:
        """Задачи человека в составе задаёт админ служения: дальше «Списки»
        их не меняют. Привязки Telegram — только к выбранным задачам. Без
        задач человек остаётся в составе, но в график не попадает (и
        убирается из составленных графиков)."""
        _validate_positive_id(actor_id, "actor_id")
        name = name.strip() if isinstance(name, str) else ""
        if not name or "\x00" in name:
            raise ValueError("Имя участника обязательно.")
        tasks = list(dict.fromkeys(tasks))
        with self._connection() as connection:
            self._require_service_editor(connection, actor_id, service_id)
            known = {
                row["name"]
                for row in connection.execute(
                    "SELECT name FROM service_tasks WHERE service_id = ?",
                    (service_id,),
                )
            }
            unknown = [task for task in tasks if task not in known]
            if unknown:
                raise ValueError(f"Задача «{unknown[0]}» не найдена.")
            member = connection.execute(
                "SELECT telegram_id, username FROM service_members "
                "WHERE service_id = ? AND name = ?",
                (service_id, name),
            ).fetchone()
            contact = connection.execute(
                "SELECT telegram_id, username FROM responsible_contacts "
                "WHERE service_id = ? AND name = ? LIMIT 1",
                (service_id, name),
            ).fetchone()
            now = _utc_now()
            # Telegram — из состава, иначе из привязок.
            bound = member if member and member["telegram_id"] else contact
            telegram_id = bound["telegram_id"] if bound else None
            username = (bound["username"] if bound else "") or ""
            if member is None:
                # Был только в привязках «👥 Ответственные» или в графике —
                # в состав.
                connection.execute(
                    "INSERT INTO service_members (service_id, name, "
                    "telegram_id, username, tasks_json, source, tasks_manual, "
                    "updated_at) VALUES (?, ?, ?, ?, ?, 'manual', 1, ?)",
                    (
                        service_id, name, telegram_id, username,
                        json.dumps(tasks, ensure_ascii=False), now,
                    ),
                )
                self._clear_member_exclusion(connection, service_id, name)
            else:
                connection.execute(
                    "UPDATE service_members SET tasks_json = ?, "
                    "tasks_manual = 1, updated_at = ? "
                    "WHERE service_id = ? AND name = ?",
                    (json.dumps(tasks, ensure_ascii=False), now, service_id, name),
                )
            # json_each: список задач может быть и пустым.
            chosen = json.dumps(tasks, ensure_ascii=False)
            connection.execute(
                "DELETE FROM responsible_contacts WHERE service_id = ? "
                "AND name = ? AND task NOT IN (SELECT value FROM json_each(?))",
                (service_id, name, chosen),
            )
            # Ожидание по @username для снятой задачи вернуло бы её.
            connection.execute(
                "DELETE FROM username_bindings WHERE service_id = ? "
                "AND name = ? AND task != '' "
                "AND task NOT IN (SELECT value FROM json_each(?))",
                (service_id, name, chosen),
            )
            self._drop_from_plans(connection, service_id, name, set(tasks))
            if telegram_id:
                for task in tasks:
                    self._write_contact(
                        connection, actor_id, service_id, task, name,
                        telegram_id, username,
                    )
            self._cleanup_unbound_user_data(connection, service_id)
            self._audit(
                connection, actor_id, "service_member_tasks", service_id,
                {"name": name, "tasks": tasks},
            )
        return tasks

    @staticmethod
    def _mark_days_changed(connection: sqlite3.Connection, service_id: str) -> None:
        """Дни служения изменились — утверждённые графики текущего и
        будущих месяцев надо опубликовать заново."""
        connection.execute(
            "UPDATE schedule_plans SET republish = 1, updated_at = ? "
            "WHERE service_id = ? AND approved_json IS NOT NULL "
            "AND month >= ?",
            (_utc_now(), service_id, _current_month()),
        )

    def set_service_member_day(
        self, actor_id: int, service_id: str, name: str, offset: int | None,
    ) -> None:
        """Личный день служения человека: сдвиг от воскресенья (−6…+6,
        None — как у всех)."""
        _validate_positive_id(actor_id, "actor_id")
        if offset is not None and (
            isinstance(offset, bool) or not isinstance(offset, int)
            or not -6 <= offset <= 6
        ):
            raise ValueError("Личный день: от вс −6 до вс +6.")
        with self._connection() as connection:
            self._require_service_editor(connection, actor_id, service_id)
            row = connection.execute(
                "SELECT telegram_id FROM service_members "
                "WHERE service_id = ? AND name = ?",
                (service_id, name),
            ).fetchone()
            if row is None:
                raise ValueError(f"«{name}» нет в составе.")
            # Человек — Telegram ID: личный день во всех его строках.
            cursor = connection.execute(
                "UPDATE service_members SET day_offset = ?, updated_at = ? "
                "WHERE service_id = ? AND (name = ? OR (telegram_id IS NOT NULL "
                "AND telegram_id = ?)) AND day_offset IS NOT ?",
                (offset, _utc_now(), service_id, name, row["telegram_id"],
                 offset),
            )
            if cursor.rowcount == 0:
                return
            self._mark_days_changed(connection, service_id)
            self._audit(
                connection, actor_id, "service_member_day", service_id,
                {"name": name, "day_offset": offset},
            )

    def set_service_member_week_tasks(
        self, actor_id: int, service_id: str, name: str,
        tasks: list[str] | None,
    ) -> None:
        """Задачи, которые человек берёт в одну неделю (звук + спевка):
        список от двух задач, [] — не связывать, None — как у служения
        (задачи с отметкой «🔗 В одну неделю»)."""
        _validate_positive_id(actor_id, "actor_id")
        if tasks is not None:
            tasks = list(dict.fromkeys(tasks))
            if len(tasks) == 1:
                raise ValueError("В одну неделю — хотя бы две задачи.")
        with self._connection() as connection:
            self._require_service_editor(connection, actor_id, service_id)
            known = {
                row["name"] for row in connection.execute(
                    "SELECT name FROM service_tasks WHERE service_id = ?",
                    (service_id,),
                )
            }
            unknown = [task for task in tasks or () if task not in known]
            if unknown:
                raise ValueError(f"Задача «{unknown[0]}» не найдена.")
            row = connection.execute(
                "SELECT telegram_id FROM service_members "
                "WHERE service_id = ? AND name = ?",
                (service_id, name),
            ).fetchone()
            if row is None:
                raise ValueError(f"«{name}» нет в составе.")
            value = None if tasks is None else json.dumps(
                tasks, ensure_ascii=False
            )
            # Человек — Telegram ID: во всех его строках.
            connection.execute(
                "UPDATE service_members SET week_tasks_json = ?, updated_at = ? "
                "WHERE service_id = ? AND (name = ? OR (telegram_id IS NOT NULL "
                "AND telegram_id = ?))",
                (value, _utc_now(), service_id, name, row["telegram_id"]),
            )
            self._audit(
                connection, actor_id, "service_member_week_tasks", service_id,
                {"name": name, "week_tasks": tasks},
            )

    def set_service_member_limit(
        self, actor_id: int, service_id: str, name: str,
        limit: tuple[int, int | None] | None,
    ) -> None:
        """Личный лимит служений в месяц: (минимум, максимум или None —
        без верхней границы); None — как у служения."""
        _validate_positive_id(actor_id, "actor_id")
        low, high = limit if limit is not None else (None, None)
        if limit is not None:
            _validate_month_limit(low, high)
        with self._connection() as connection:
            self._require_service_editor(connection, actor_id, service_id)
            row = connection.execute(
                "SELECT telegram_id FROM service_members "
                "WHERE service_id = ? AND name = ?",
                (service_id, name),
            ).fetchone()
            if row is None:
                raise ValueError(f"«{name}» нет в составе.")
            # Человек — Telegram ID: лимит во всех его строках.
            connection.execute(
                "UPDATE service_members SET month_min = ?, month_max = ?, "
                "updated_at = ? WHERE service_id = ? AND (name = ? OR "
                "(telegram_id IS NOT NULL AND telegram_id = ?))",
                (low, high, _utc_now(), service_id, name, row["telegram_id"]),
            )
            self._audit(
                connection, actor_id, "service_member_limit", service_id,
                {"name": name, "month_min": low, "month_max": high},
            )

    def remove_service_member(
        self, actor_id: int, service_id: str, name: str,
    ) -> bool:
        """Убирает человека из состава служения вместе с привязками к
        задачам. Файл его больше не вернёт, пока человека не добавят в боте
        снова (даже если сейчас его нет в «Списках» — он может появиться
        там позже). True — он есть в «Списках»."""
        _validate_positive_id(actor_id, "actor_id")
        with self._connection() as connection:
            self._require_service_editor(connection, actor_id, service_id)
            member = connection.execute(
                "SELECT in_list FROM service_members "
                "WHERE service_id = ? AND name = ?",
                (service_id, name),
            ).fetchone()
            removed = connection.execute(
                "DELETE FROM service_members WHERE service_id = ? AND name = ?",
                (service_id, name),
            ).rowcount + connection.execute(
                "DELETE FROM responsible_contacts "
                "WHERE service_id = ? AND name = ?",
                (service_id, name),
            ).rowcount
            if not removed:
                raise ValueError(f"«{name}» нет в составе.")
            self._drop_username_binding(connection, service_id, name)
            connection.execute(
                "INSERT OR IGNORE INTO service_member_exclusions "
                "(service_id, name, created_at) VALUES (?, ?, ?)",
                (service_id, name, _utc_now()),
            )
            self._drop_from_plans(connection, service_id, name)
            self._cleanup_unbound_user_data(connection, service_id)
            self._audit(
                connection, actor_id, "service_member_removed", service_id,
                {"name": name},
            )
        return bool(member and member["in_list"])

    @staticmethod
    def _drop_from_plans(
        connection: sqlite3.Connection, service_id: str, name: str,
        kept_tasks=None,
    ) -> None:
        """Убирает человека из рабочих копий графиков служения: из всех
        задач или (kept_tasks) из задач, которых у него больше нет.
        Опубликованная копия остаётся — правка станет видна как
        неопубликованная."""
        def is_person(key: str) -> bool:
            # Ключ — имя, при совпадении имён «имя · ID» (как в
            # readiness_people).
            return key == name or bool(re.fullmatch(
                re.escape(name) + r" · (?:\d+|без Telegram)", key,
            ))

        for row in connection.execute(
            "SELECT month, slots_json, days_json FROM schedule_plans "
            "WHERE service_id = ?",
            (service_id,),
        ).fetchall():
            slots = json.loads(row["slots_json"])
            changed = False
            serving: dict[str, set[str]] = {}
            for day, tasks in slots.items():
                for task, people in tasks.items():
                    if kept_tasks is None or task not in kept_tasks:
                        kept = [key for key in people if not is_person(key)]
                        if kept != people:
                            tasks[task] = kept
                            changed = True
                    for key in tasks[task]:
                        serving.setdefault(day, set()).add(key)
            # И разовые дни там, где его больше нет, — иначе вернувшийся
            # получил бы старый день.
            days = {}
            for day, people_days in json.loads(
                row["days_json"] or "{}"
            ).items():
                def stays(key: str) -> bool:
                    person, task = split_day_key(key)
                    if not is_person(person):
                        return True
                    # Разовый день задачи — пока человек в этой задаче.
                    if task is not None:
                        return person in slots.get(day, {}).get(task, [])
                    return person in serving.get(day, set())

                kept_days = {
                    key: offset for key, offset in people_days.items()
                    if stays(key)
                }
                changed |= kept_days != people_days
                if kept_days:
                    days[day] = kept_days
            if changed:
                connection.execute(
                    "UPDATE schedule_plans SET slots_json = ?, "
                    "days_json = ?, edited = 1, updated_at = ? "
                    "WHERE service_id = ? AND month = ?",
                    (
                        json.dumps(slots, ensure_ascii=False),
                        json.dumps(days, ensure_ascii=False), _utc_now(),
                        service_id, row["month"],
                    ),
                )

    @staticmethod
    def _clear_member_exclusion(
        connection: sqlite3.Connection, service_id: str, name: str,
    ) -> None:
        """Человека снова добавили в боте — «Списки» снова его ведут."""
        connection.execute(
            "DELETE FROM service_member_exclusions "
            "WHERE service_id = ? AND name = ?",
            (service_id, name),
        )

    def ensure_member_contacts(self, service_id: str, assignments) -> None:
        """Создаёт привязки «задача + имя» для привязанных людей служения.

        assignments — {задача: [имена] | {"names": [...]}} из графика. Новая
        задача человека в графике сразу считается привязанной. Чужие
        привязки (то же имя у другого Telegram ID) не меняются.
        """
        with self._connection() as connection:
            for task, info in assignments.items():
                if isinstance(info, dict):
                    if info.get("is_informational"):
                        continue
                    names = info.get("names", [])
                else:
                    names = info
                for name in names:
                    self._ensure_member_task_contacts(
                        connection, service_id, name, (task,),
                        scheduled_names=names,
                    )

    @staticmethod
    def _ensure_member_task_contacts(
        connection: sqlite3.Connection,
        service_id: str,
        name: str,
        tasks,
        scheduled_names=None,
    ) -> None:
        """scheduled_names — имена задачи в графике. Привязка этого Telegram
        под другим именем, которого в графике задачи уже нет (человека
        переименовали), переходит на имя из графика.
        """
        # Задачи, заданные в боте, график и «Списки» не расширяют.
        member = connection.execute(
            "SELECT telegram_id, username FROM service_members "
            "WHERE service_id = ? AND name = ? AND telegram_id IS NOT NULL "
            "AND tasks_manual = 0",
            (service_id, name),
        ).fetchone()
        if member is None:
            return
        actor = connection.execute(
            "SELECT telegram_id FROM super_admins ORDER BY created_at LIMIT 1"
        ).fetchone()
        for task in tasks:
            if scheduled_names is not None:
                # Переименование подтверждают «Списки»: новое имя в списке,
                # старого там нет. Иначе (варианты написания, разные даты)
                # привязка не переходит между именами.
                connection.execute(
                    "UPDATE OR IGNORE responsible_contacts SET name = ? "
                    "WHERE service_id = ? AND task = ? AND telegram_id = ? "
                    f"AND name NOT IN ({', '.join('?' for _ in scheduled_names)}) "
                    "AND name NOT IN (SELECT name FROM service_members "
                    "WHERE service_id = ? AND in_list = 1) "
                    "AND EXISTS (SELECT 1 FROM service_members "
                    "WHERE service_id = ? AND name = ? AND in_list = 1)",
                    (
                        name, service_id, task, member["telegram_id"],
                        *scheduled_names, service_id, service_id, name,
                    ),
                )
            connection.execute(
                """
                INSERT OR IGNORE INTO responsible_contacts (
                    service_id, task, name, telegram_id, username,
                    updated_by, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    service_id, task, name, member["telegram_id"],
                    member["username"],
                    actor["telegram_id"] if actor else member["telegram_id"],
                    _utc_now(),
                ),
            )

    def get_user_notifications_enabled(
        self, actor_id: int, service_id: str
    ) -> bool:
        return self.is_user_notifications_enabled(actor_id, service_id)

    def is_user_notifications_enabled(
        self, actor_id: int, service_id: str
    ) -> bool:
        _validate_positive_id(actor_id, "actor_id")
        with self._connection() as connection:
            self._require_service(connection, service_id)
            row = connection.execute(
                "SELECT enabled "
                "FROM user_notification_prefs "
                "WHERE service_id = ? AND actor_id = ?",
                (service_id, actor_id),
            ).fetchone()
        return bool(row["enabled"]) if row else False

    def set_user_notifications_enabled(
        self, actor_id: int, service_id: str, enabled: bool
    ) -> None:
        _validate_positive_id(actor_id, "actor_id")
        if not isinstance(enabled, bool):
            raise ValueError("enabled должен быть bool")
        with self._connection() as connection:
            self._require_service(connection, service_id)
            registered = connection.execute(
                "SELECT 1 FROM responsible_contacts "
                "WHERE service_id = ? AND telegram_id = ? LIMIT 1",
                (service_id, actor_id),
            ).fetchone()
            if registered is None:
                raise ValueError(
                    "Пользователь не привязан к задачам этого служения"
                )
            current = connection.execute(
                "SELECT enabled "
                "FROM user_notification_prefs "
                "WHERE service_id = ? AND actor_id = ?",
                (service_id, actor_id),
            ).fetchone()
            if current is not None and bool(
                current["enabled"]
            ) == enabled:
                return
            if current is None and not enabled:
                return
            connection.execute(
                """
                INSERT INTO user_notification_prefs (
                    service_id, actor_id, enabled
                ) VALUES (?, ?, ?)
                ON CONFLICT(actor_id, service_id) DO UPDATE SET
                    enabled = excluded.enabled
                """,
                (service_id, actor_id, int(enabled)),
            )
            self._audit(
                connection,
                actor_id,
                "user_notification_preference_updated",
                service_id,
                {"enabled": enabled},
            )

    def set_user_notifications_all(
        self, actor_id: int, enabled: bool
    ) -> None:
        _validate_positive_id(actor_id, "actor_id")
        if not isinstance(enabled, bool):
            raise ValueError("enabled должен быть bool")
        with self._connection() as connection:
            services = connection.execute(
                "SELECT DISTINCT service_id FROM responsible_contacts "
                "WHERE telegram_id = ? AND service_id IN (SELECT id FROM services WHERE archived = 0) "
                "ORDER BY service_id",
                (actor_id,),
            ).fetchall()
            for row in services:
                current = connection.execute(
                    "SELECT enabled FROM user_notification_prefs "
                    "WHERE service_id = ? AND actor_id = ?",
                    (row["service_id"], actor_id),
                ).fetchone()
                if current is not None and bool(current["enabled"]) == enabled:
                    continue
                connection.execute(
                    """
                    INSERT INTO user_notification_prefs (
                        service_id, actor_id, enabled
                    ) VALUES (?, ?, ?)
                    ON CONFLICT(actor_id, service_id) DO UPDATE SET
                        enabled = excluded.enabled
                    WHERE enabled != excluded.enabled
                    """,
                    (row["service_id"], actor_id, int(enabled)),
                )
                self._audit(
                    connection,
                    actor_id,
                    "user_notification_preference_updated",
                    row["service_id"],
                    {"enabled": enabled, "bulk": True},
                )

    def list_users_with_notifications_enabled(
        self, service_id: str
    ) -> list[int]:
        with self._connection() as connection:
            self._require_service(connection, service_id)
            rows = connection.execute(
                """
                SELECT DISTINCT preferences.actor_id
                FROM user_notification_prefs AS preferences
                JOIN responsible_contacts AS contacts
                  ON contacts.service_id = preferences.service_id
                 AND contacts.telegram_id = preferences.actor_id
                WHERE preferences.service_id = ?
                  AND preferences.enabled = 1
                ORDER BY preferences.actor_id
                """,
                (service_id,),
            ).fetchall()
        return [row["actor_id"] for row in rows]

    def list_user_services(self, actor_id: int) -> list[str]:
        _validate_positive_id(actor_id, "actor_id")
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT DISTINCT service_id FROM responsible_contacts "
                "WHERE telegram_id = ? AND service_id IN (SELECT id FROM services WHERE archived = 0) "
                "ORDER BY service_id",
                (actor_id,),
            ).fetchall()
        return [row["service_id"] for row in rows]

    def list_user_tasks(self, actor_id: int, service_id: str) -> list[str]:
        """Return only tasks to which this user is currently assigned."""
        _validate_positive_id(actor_id, "actor_id")
        with self._connection() as connection:
            self._require_service(connection, service_id)
            rows = connection.execute(
                "SELECT DISTINCT task FROM responsible_contacts "
                "WHERE telegram_id = ? AND service_id = ? ORDER BY task",
                (actor_id, service_id),
            ).fetchall()
        return [row["task"] for row in rows]

    MONTH_SUMMARY_DEFAULTS = {
        "enabled": False, "reminder_days": 1, "reminder_time": "18:00",
    }

    def get_month_summary(self, telegram_id: int) -> dict:
        """Настройка сводки на месяц (этап 4.1); без записи — по умолчанию."""
        _validate_positive_id(telegram_id, "telegram_id")
        with self._connection() as connection:
            row = connection.execute(
                "SELECT enabled, reminder_days, reminder_time "
                "FROM user_month_summaries WHERE telegram_id = ?",
                (telegram_id,),
            ).fetchone()
        if row is None:
            return dict(self.MONTH_SUMMARY_DEFAULTS)
        return {**dict(row), "enabled": bool(row["enabled"])}

    def set_month_summary(
        self,
        telegram_id: int,
        *,
        enabled: bool | None = None,
        days: int | None = None,
        time: str | None = None,
    ) -> dict:
        current = self.get_month_summary(telegram_id)
        new = {
            "enabled": current["enabled"] if enabled is None else enabled,
            "reminder_days": current["reminder_days"] if days is None else days,
            "reminder_time": current["reminder_time"] if time is None else time,
        }
        if not isinstance(new["enabled"], bool):
            raise ValueError("enabled должен быть логическим значением")
        self._validate_group_reminder_values(
            new["reminder_days"], new["reminder_time"], None
        )
        with self._connection() as connection:
            connection.execute(
                "INSERT INTO user_month_summaries "
                "(telegram_id, enabled, reminder_days, reminder_time, updated_at) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT(telegram_id) DO UPDATE SET "
                "enabled = excluded.enabled, "
                "reminder_days = excluded.reminder_days, "
                "reminder_time = excluded.reminder_time, "
                "updated_at = excluded.updated_at",
                (
                    telegram_id, int(new["enabled"]), new["reminder_days"],
                    new["reminder_time"], _utc_now(),
                ),
            )
        return new

    def list_enabled_month_summaries(self) -> list[dict]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT telegram_id, reminder_days, reminder_time "
                "FROM user_month_summaries WHERE enabled = 1 "
                "ORDER BY telegram_id"
            ).fetchall()
        return [dict(row) for row in rows]

    def is_month_summary_delivered(self, telegram_id: int, month: str) -> bool:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT 1 FROM month_summary_deliveries "
                "WHERE telegram_id = ? AND month = ?",
                (telegram_id, month),
            ).fetchone()
        return row is not None

    def claim_month_summary(self, telegram_id: int, month: str) -> bool:
        """Отметка доставки по (пользователь, месяц); False — уже отмечено."""
        with self._connection(notify=False) as connection:
            cursor = connection.execute(
                "INSERT OR IGNORE INTO month_summary_deliveries "
                "(telegram_id, month, delivered_at) VALUES (?, ?, ?)",
                (telegram_id, month, _utc_now()),
            )
        return cursor.rowcount > 0

    def release_month_summary(self, telegram_id: int, month: str) -> None:
        """Снимает отметку, если сводку отправить не удалось."""
        with self._connection(notify=False) as connection:
            connection.execute(
                "DELETE FROM month_summary_deliveries "
                "WHERE telegram_id = ? AND month = ?",
                (telegram_id, month),
            )

    def list_user_assignments(self, actor_id: int) -> list[dict]:
        """Return every (service, task, schedule name) linked to this user."""
        _validate_positive_id(actor_id, "actor_id")
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT service_id, task, name FROM responsible_contacts "
                "WHERE telegram_id = ? AND service_id IN (SELECT id FROM services WHERE archived = 0) "
                "ORDER BY service_id, task, name",
                (actor_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_user_notifications_state(self, actor_id: int) -> dict:
        _validate_positive_id(actor_id, "actor_id")
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT services.id AS service_id,
                       COALESCE(preferences.enabled, 0) AS enabled,
                       COUNT(DISTINCT texts.id) AS texts_count,
                       COUNT(DISTINCT schedules.id) AS schedules_count
                FROM responsible_contacts AS contacts
                JOIN services ON services.id = contacts.service_id
                LEFT JOIN user_notification_prefs AS preferences
                  ON preferences.service_id = contacts.service_id
                 AND preferences.actor_id = contacts.telegram_id
                LEFT JOIN user_reminder_texts AS texts
                  ON texts.actor_id = contacts.telegram_id
                 AND texts.service_id = contacts.service_id
                LEFT JOIN user_reminder_schedules AS schedules
                  ON schedules.text_id = texts.id
                WHERE contacts.telegram_id = ? AND services.archived = 0
                GROUP BY services.id, preferences.enabled
                ORDER BY services.id
                """,
                (actor_id,),
            ).fetchall()
        per_service = [
            {
                "service_id": row["service_id"],
                "enabled": bool(row["enabled"]),
                "texts_count": row["texts_count"],
                "schedules_count": row["schedules_count"],
            }
            for row in rows
        ]
        enabled_count = sum(item["enabled"] for item in per_service)
        return {
            "total_services": len(per_service),
            "enabled_count": enabled_count,
            "disabled_count": len(per_service) - enabled_count,
            "per_service": per_service,
        }

    def list_user_texts(self, actor_id: int, service_id: str) -> list[dict]:
        _validate_positive_id(actor_id, "actor_id")
        with self._connection() as connection:
            self._require_service(connection, service_id)
            rows = connection.execute(
                "SELECT id, actor_id, service_id, task, body, created_at "
                "FROM user_reminder_texts "
                "WHERE actor_id = ? AND service_id = ? ORDER BY task",
                (actor_id, service_id),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_user_text(
        self, actor_id: int, service_id: str, task: str
    ) -> dict | None:
        _validate_positive_id(actor_id, "actor_id")
        with self._connection() as connection:
            self._require_service(connection, service_id)
            row = connection.execute(
                "SELECT id, actor_id, service_id, task, body, created_at "
                "FROM user_reminder_texts "
                "WHERE actor_id = ? AND service_id = ? AND task = ?",
                (actor_id, service_id, task.strip()),
            ).fetchone()
        return dict(row) if row else None

    def upsert_user_text(
        self, actor_id: int, service_id: str, task: str, body: str | None
    ) -> int:
        _validate_positive_id(actor_id, "actor_id")
        task = task.strip()
        if not task:
            raise ValueError("Название задачи обязательно")
        if body is not None:
            if not isinstance(body, str) or not body.strip():
                raise ValueError("Текст уведомления не может быть пустым")
            validate_notification_template(
                body, PERSONAL_TEMPLATE_FIELDS, "Личный шаблон пользователя"
            )
        with self._connection() as connection:
            self._require_service(connection, service_id)
            registered = connection.execute(
                "SELECT 1 FROM responsible_contacts "
                "WHERE service_id = ? AND task = ? AND telegram_id = ?",
                (service_id, task, actor_id),
            ).fetchone()
            if registered is None:
                raise ValueError(
                    "Пользователь не привязан к этой задаче"
                )
            connection.execute(
                """
                INSERT INTO user_reminder_texts (
                    actor_id, service_id, task, body
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(actor_id, service_id, task) DO UPDATE SET
                    body = excluded.body
                """,
                (actor_id, service_id, task, body),
            )
            row = connection.execute(
                "SELECT id FROM user_reminder_texts "
                "WHERE actor_id = ? AND service_id = ? AND task = ?",
                (actor_id, service_id, task),
            ).fetchone()
            text_id = row["id"]
            self._audit(
                connection,
                actor_id,
                "user_reminder_text_updated",
                service_id,
                {"task": task, "body_overridden": body is not None},
            )
        return text_id

    def reset_user_text(
        self, actor_id: int, service_id: str, task: str
    ) -> None:
        _validate_positive_id(actor_id, "actor_id")
        task = task.strip()
        if not task:
            raise ValueError("Название задачи обязательно")
        with self._connection() as connection:
            self._require_service(connection, service_id)
            cursor = connection.execute(
                "UPDATE user_reminder_texts SET body = NULL "
                "WHERE actor_id = ? AND service_id = ? AND task = ?",
                (actor_id, service_id, task),
            )
            if cursor.rowcount == 0:
                raise ValueError("Личный текст не найден")
            self._audit(
                connection,
                actor_id,
                "user_reminder_text_updated",
                service_id,
                {"task": task, "body_overridden": False},
            )

    def ensure_user_text(
        self, actor_id: int, service_id: str, task: str
    ) -> int:
        _validate_positive_id(actor_id, "actor_id")
        task = task.strip()
        if not task:
            raise ValueError("Название задачи обязательно")
        with self._connection() as connection:
            self._require_service(connection, service_id)
            registered = connection.execute(
                "SELECT 1 FROM responsible_contacts "
                "WHERE service_id = ? AND task = ? AND telegram_id = ?",
                (service_id, task, actor_id),
            ).fetchone()
            if registered is None:
                raise ValueError("Пользователь не привязан к этой задаче")
            connection.execute(
                """
                INSERT INTO user_reminder_texts (
                    actor_id, service_id, task, body
                ) VALUES (?, ?, ?, NULL)
                ON CONFLICT(actor_id, service_id, task) DO NOTHING
                """,
                (actor_id, service_id, task),
            )
            row = connection.execute(
                "SELECT id FROM user_reminder_texts "
                "WHERE actor_id = ? AND service_id = ? AND task = ?",
                (actor_id, service_id, task),
            ).fetchone()
        return row["id"]

    def delete_user_text(
        self, actor_id: int, service_id: str, task: str
    ) -> None:
        _validate_positive_id(actor_id, "actor_id")
        with self._connection() as connection:
            self._require_service(connection, service_id)
            connection.execute(
                "DELETE FROM user_reminder_texts "
                "WHERE actor_id = ? AND service_id = ? AND task = ?",
                (actor_id, service_id, task.strip()),
            )

    _SCHEDULE_COLUMNS = (
        "id, text_id, reminder_days, reminder_time, enabled, origin, body"
    )

    def list_schedules(self, text_id: int) -> list[dict]:
        _validate_positive_id(text_id, "text_id")
        with self._connection() as connection:
            rows = connection.execute(
                f"SELECT {self._SCHEDULE_COLUMNS} "
                "FROM user_reminder_schedules WHERE text_id = ? ORDER BY id",
                (text_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_user_service_schedules(
        self, actor_id: int, service_id: str
    ) -> list[dict]:
        """Все личные напоминания человека в служении (с задачей)."""
        _validate_positive_id(actor_id, "actor_id")
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT s.id, s.text_id, s.reminder_days, s.reminder_time, "
                "s.enabled, s.origin, s.body, t.task "
                "FROM user_reminder_schedules s "
                "JOIN user_reminder_texts t ON t.id = s.text_id "
                "WHERE t.actor_id = ? AND t.service_id = ? "
                "ORDER BY t.task, s.origin, s.reminder_days DESC, "
                "s.reminder_time, s.id",
                (actor_id, service_id),
            ).fetchall()
        return [dict(row) for row in rows]

    def add_schedule(
        self,
        text_id: int,
        days: int,
        time: str,
        *,
        origin: str = "admin",
        body: str | None = None,
        enabled: bool = True,
    ) -> dict:
        _validate_positive_id(text_id, "text_id")
        self._validate_user_schedule(days, time)
        if origin not in {"admin", "user"}:
            raise ValueError("Некорректный вид напоминания")
        body = self._validated_user_body(body)
        with self._connection() as connection:
            text = connection.execute(
                "SELECT actor_id, service_id FROM user_reminder_texts "
                "WHERE id = ?",
                (text_id,),
            ).fetchone()
            if text is None:
                raise ValueError("Текст личного уведомления не найден")
            if origin == "user":
                self._ensure_user_schedule_limit(
                    connection, text["actor_id"], text["service_id"]
                )
            if enabled:
                self._ensure_unique_enabled_rule(connection, text_id, days, time)
            cursor = connection.execute(
                "INSERT INTO user_reminder_schedules "
                "(text_id, reminder_days, reminder_time, enabled, origin, body) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (text_id, days, time, int(enabled), origin, body),
            )
            schedule_id = cursor.lastrowid
            row = connection.execute(
                f"SELECT {self._SCHEDULE_COLUMNS} "
                "FROM user_reminder_schedules WHERE id = ?",
                (schedule_id,),
            ).fetchone()
        return dict(row)

    @staticmethod
    def _validated_user_body(body: str | None) -> str | None:
        if body is None:
            return None
        if not isinstance(body, str) or not body.strip():
            raise ValueError("Текст уведомления не может быть пустым")
        if len(body.encode("utf-16-le")) // 2 > USER_REMINDER_BODY_LIMIT:
            # Запас под заголовок и подставленные поля: иначе сообщение не
            # уйдёт из-за лимита Telegram.
            raise ValueError(
                f"Текст напоминания — не длиннее {USER_REMINDER_BODY_LIMIT} "
                "символов."
            )
        validate_notification_template(
            body, PERSONAL_TEMPLATE_FIELDS, "Текст личного напоминания"
        )
        return body.strip()

    @staticmethod
    def _ensure_unique_enabled_rule(
        connection: sqlite3.Connection, text_id: int, days: int, time: str,
        excluding_id: int | None = None,
    ) -> None:
        """Два включённых напоминания задачи на одно время — два одинаковых
        сообщения; такое не сохраняется."""
        duplicate = connection.execute(
            "SELECT 1 FROM user_reminder_schedules WHERE text_id = ? "
            "AND reminder_days = ? AND reminder_time = ? AND enabled = 1 "
            "AND id IS NOT ?",
            (text_id, days, time, excluding_id),
        ).fetchone()
        if duplicate is not None:
            raise ValueError(
                "На это время у задачи уже есть включённое напоминание. "
                "Выберите другое время."
            )

    @staticmethod
    def _ensure_user_schedule_limit(
        connection: sqlite3.Connection, actor_id: int, service_id: str
    ) -> None:
        # Считаются свои напоминания: напоминания админа бот создаёт сам
        # (по одному на задачу).
        count = connection.execute(
            "SELECT COUNT(*) FROM user_reminder_schedules s "
            "JOIN user_reminder_texts t ON t.id = s.text_id "
            "WHERE t.actor_id = ? AND t.service_id = ? AND s.origin = 'user'",
            (actor_id, service_id),
        ).fetchone()[0]
        if count >= USER_REMINDERS_PER_SERVICE:
            raise ValueError(
                f"В одном служении можно не больше "
                f"{USER_REMINDERS_PER_SERVICE} личных напоминаний."
            )

    def update_schedule(
        self,
        schedule_id: int,
        days: int | None = None,
        time: str | None = None,
        enabled: bool | None = None,
        body=_UNSET,
    ) -> None:
        """body — свой текст напоминания (None — текст админа)."""
        _validate_positive_id(schedule_id, "schedule_id")
        if body is not _UNSET:
            body = self._validated_user_body(body)
        with self._connection() as connection:
            row = connection.execute(
                "SELECT text_id, reminder_days, reminder_time, enabled, body "
                "FROM user_reminder_schedules WHERE id = ?",
                (schedule_id,),
            ).fetchone()
            if row is None:
                raise ValueError("Правило личного расписания не найдено")
            new_days = row["reminder_days"] if days is None else days
            new_time = row["reminder_time"] if time is None else time
            if enabled is not None and not isinstance(enabled, bool):
                raise ValueError("enabled должен быть bool")
            new_enabled = (
                bool(row["enabled"]) if enabled is None else enabled
            )
            self._validate_user_schedule(new_days, new_time)
            if new_enabled and (
                not row["enabled"]
                or (new_days, new_time)
                != (row["reminder_days"], row["reminder_time"])
            ):
                self._ensure_unique_enabled_rule(
                    connection, row["text_id"], new_days, new_time,
                    excluding_id=schedule_id,
                )
            if (
                new_days != row["reminder_days"]
                or new_time != row["reminder_time"]
            ):
                connection.execute(
                    "DELETE FROM user_reminder_deliveries "
                    "WHERE schedule_id = ?",
                    (schedule_id,),
                )
            connection.execute(
                "UPDATE user_reminder_schedules "
                "SET reminder_days = ?, reminder_time = ?, enabled = ?, "
                "body = ? WHERE id = ?",
                (
                    new_days, new_time, int(new_enabled),
                    row["body"] if body is _UNSET else body, schedule_id,
                ),
            )

    def delete_schedule(self, schedule_id: int) -> None:
        _validate_positive_id(schedule_id, "schedule_id")
        with self._connection() as connection:
            connection.execute(
                "DELETE FROM user_reminder_schedules WHERE id = ?",
                (schedule_id,),
            )

    def set_user_service_schedule(
        self,
        actor_id: int,
        service_id: str,
        tasks: list[str],
        days: int,
        time: str,
    ) -> None:
        """Give each listed task exactly one enabled rule with this timing.

        An existing rule is updated in place so its delivery records keep
        preventing a repeated reminder for an event that was already sent.
        """
        _validate_positive_id(actor_id, "actor_id")
        self._validate_user_schedule(days, time)
        tasks = sorted({task.strip() for task in tasks if task.strip()})
        if not tasks:
            raise ValueError("Нет задач для личного напоминания")
        with self._connection() as connection:
            self._require_service(connection, service_id)
            for task in tasks:
                registered = connection.execute(
                    "SELECT 1 FROM responsible_contacts "
                    "WHERE service_id = ? AND task = ? AND telegram_id = ?",
                    (service_id, task, actor_id),
                ).fetchone()
                if registered is None:
                    raise ValueError("Пользователь не привязан к этой задаче")
                connection.execute(
                    "INSERT INTO user_reminder_texts "
                    "(actor_id, service_id, task, body) VALUES (?, ?, ?, NULL) "
                    "ON CONFLICT(actor_id, service_id, task) DO NOTHING",
                    (actor_id, service_id, task),
                )
                text_id = connection.execute(
                    "SELECT id FROM user_reminder_texts "
                    "WHERE actor_id = ? AND service_id = ? AND task = ?",
                    (actor_id, service_id, task),
                ).fetchone()["id"]
                # Только напоминания с текстом админа: свои не меняются.
                schedule_ids = [
                    row["id"]
                    for row in connection.execute(
                        "SELECT id FROM user_reminder_schedules "
                        "WHERE text_id = ? AND origin = 'admin' ORDER BY id",
                        (text_id,),
                    ).fetchall()
                ]
                if schedule_ids:
                    connection.execute(
                        "UPDATE user_reminder_schedules SET reminder_days = ?, "
                        "reminder_time = ?, enabled = 1 WHERE id = ?",
                        (days, time, schedule_ids[0]),
                    )
                    # Отправки удаляемых правил переносятся на оставшееся,
                    # чтобы уже напомненное событие не пришло повторно.
                    connection.executemany(
                        "INSERT OR IGNORE INTO user_reminder_deliveries "
                        "(schedule_id, event_date, delivered_at) "
                        "SELECT ?, event_date, delivered_at "
                        "FROM user_reminder_deliveries WHERE schedule_id = ?",
                        [
                            (schedule_ids[0], schedule_id)
                            for schedule_id in schedule_ids[1:]
                        ],
                    )
                    connection.executemany(
                        "DELETE FROM user_reminder_schedules WHERE id = ?",
                        [(schedule_id,) for schedule_id in schedule_ids[1:]],
                    )
                else:
                    connection.execute(
                        "INSERT INTO user_reminder_schedules "
                        "(text_id, reminder_days, reminder_time) "
                        "VALUES (?, ?, ?)",
                        (text_id, days, time),
                    )

    @staticmethod
    def _validate_user_schedule(days: int, time: str) -> None:
        if isinstance(days, bool) or not isinstance(days, int) or not 0 <= days <= 6:
            raise ValueError("Дни до события должны быть от 0 до 6")
        if not isinstance(time, str) or not re.fullmatch(
            r"(?:[01]\d|2[0-3]):[0-5]\d", time
        ):
            raise ValueError("Время должно быть в формате HH:MM")

    def list_enabled_user_reminder_schedules(
        self, actor_id: int | None = None
    ) -> list[dict]:
        if actor_id is not None:
            _validate_positive_id(actor_id, "actor_id")
        query = """
            SELECT schedules.id AS schedule_id, schedules.text_id,
                   schedules.reminder_days, schedules.reminder_time,
                   texts.actor_id, texts.service_id, texts.task,
                   schedules.body AS body, schedules.origin,
                   services.name AS service_name,
                   services.enabled AS service_enabled,
                   preferences.enabled AS notifications_enabled
            FROM user_reminder_schedules AS schedules
            JOIN user_reminder_texts AS texts ON texts.id = schedules.text_id
            JOIN services ON services.id = texts.service_id
            JOIN user_notification_prefs AS preferences
              ON preferences.service_id = texts.service_id
             AND preferences.actor_id = texts.actor_id
            WHERE schedules.enabled = 1
              AND preferences.enabled = 1
              AND services.enabled = 1
        """
        parameters: tuple = ()
        if actor_id is not None:
            query += " AND texts.actor_id = ?"
            parameters = (actor_id,)
        query += " ORDER BY schedules.id"
        with self._connection() as connection:
            rows = connection.execute(query, parameters).fetchall()
            result = []
            for row in rows:
                contact = connection.execute(
                    "SELECT name, username FROM responsible_contacts "
                    "WHERE service_id = ? AND task = ? AND telegram_id = ? "
                    "ORDER BY name LIMIT 1",
                    (row["service_id"], row["task"], row["actor_id"]),
                ).fetchone()
                if contact is None:
                    continue
                result.append({
                    **dict(row),
                    "name": contact["name"],
                    "username": contact["username"],
                })
        return result

    def get_user_reminder_schedule(self, schedule_id: int) -> dict | None:
        _validate_positive_id(schedule_id, "schedule_id")
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT schedules.id AS schedule_id, schedules.text_id,
                       schedules.reminder_days, schedules.reminder_time,
                       texts.actor_id, texts.service_id, texts.task,
                       schedules.body AS body, schedules.origin,
                       services.name AS service_name,
                       services.enabled AS service_enabled,
                       preferences.enabled AS notifications_enabled,
                       schedules.enabled AS schedule_enabled
                FROM user_reminder_schedules AS schedules
                JOIN user_reminder_texts AS texts ON texts.id = schedules.text_id
                JOIN services ON services.id = texts.service_id
                LEFT JOIN user_notification_prefs AS preferences
                  ON preferences.service_id = texts.service_id
                 AND preferences.actor_id = texts.actor_id
                WHERE schedules.id = ?
                """,
                (schedule_id,),
            ).fetchone()
            if row is None:
                return None
            contact = connection.execute(
                "SELECT name, username FROM responsible_contacts "
                "WHERE service_id = ? AND task = ? AND telegram_id = ? "
                "ORDER BY name LIMIT 1",
                (row["service_id"], row["task"], row["actor_id"]),
            ).fetchone()
        return {
            **dict(row),
            "notifications_enabled": bool(row["notifications_enabled"]),
            "name": contact["name"] if contact else None,
            "username": contact["username"] if contact else "",
        }

    def is_user_reminder_delivered(
        self, schedule_id: int, event_date: str
    ) -> bool:
        _validate_positive_id(schedule_id, "schedule_id")
        with self._connection() as connection:
            return connection.execute(
                "SELECT 1 FROM user_reminder_deliveries "
                "WHERE schedule_id = ? AND event_date = ?",
                (schedule_id, event_date),
            ).fetchone() is not None

    def mark_user_reminder_delivered(
        self, schedule_id: int, event_date: str
    ) -> None:
        _validate_positive_id(schedule_id, "schedule_id")
        with self._connection() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO user_reminder_deliveries "
                "(schedule_id, event_date, delivered_at) VALUES (?, ?, ?)",
                (schedule_id, event_date, _utc_now()),
            )

    def resolve_text_for_user(
        self,
        actor_id: int,
        service_id: str,
        task: str,
        personal_templates: dict[str, str] | None = None,
        body: str | None = None,
    ) -> str:
        """body — свой текст напоминания; иначе текст админа задачи."""
        if body:
            return body
        user_text = self.get_user_text(actor_id, service_id, task)
        if user_text is not None and user_text["body"] is not None:
            return user_text["body"]
        if personal_templates and personal_templates.get(task):
            return personal_templates[task]
        # Служение, задача и дата — уже в заголовке напоминания.
        return "{name}, напоминаем о вашем служении."

    def get_responsible_contact(
        self, service_id: str, task: str, name: str
    ) -> dict | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT telegram_id, username FROM responsible_contacts "
                "WHERE service_id = ? AND task = ? AND name = ? "
                "ORDER BY updated_at DESC LIMIT 1",
                (service_id, task.strip(), name.strip()),
            ).fetchone()
        return dict(row) if row else None

    def contact_name_among(
        self, service_id: str, task: str, names, telegram_id: int
    ) -> str | None:
        """Под каким из этих имён telegram_id привязан к задаче (или None)."""
        names = [name.strip() for name in names if name and name.strip()]
        if not names:
            return None
        marks = ", ".join("?" for _ in names)
        with self._connection() as connection:
            row = connection.execute(
                "SELECT name FROM responsible_contacts WHERE service_id = ? "
                f"AND task = ? AND telegram_id = ? AND name IN ({marks}) "
                "ORDER BY updated_at DESC LIMIT 1",
                (service_id, task.strip(), telegram_id, *names),
            ).fetchone()
        return row["name"] if row else None

    def list_responsible_contacts(
        self, actor_id: int, service_id: str
    ) -> list[dict]:
        _validate_positive_id(actor_id, "actor_id")
        with self._connection() as connection:
            self._require_service_editor(connection, actor_id, service_id)
            self._require_service(connection, service_id)
            rows = connection.execute(
                "SELECT task, name, telegram_id, username "
                "FROM responsible_contacts WHERE service_id = ? "
                "ORDER BY task, name",
                (service_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_service_admins(self, service_id: str) -> list[int]:
        with self._connection() as connection:
            self._require_service(connection, service_id)
            rows = connection.execute(
                "SELECT telegram_id FROM service_admins "
                "WHERE service_id = ? ORDER BY telegram_id",
                (service_id,),
            ).fetchall()
        return [row["telegram_id"] for row in rows]

    def list_admin_ids(self) -> dict[int, bool]:
        """Return every administrator Telegram ID mapped to super-admin status."""
        with self._connection() as connection:
            service_admins = connection.execute(
                "SELECT DISTINCT telegram_id FROM service_admins"
            ).fetchall()
            super_admins = connection.execute(
                "SELECT telegram_id FROM super_admins"
            ).fetchall()
        admins = {row["telegram_id"]: False for row in service_admins}
        admins.update({row["telegram_id"]: True for row in super_admins})
        return admins

    def list_service_admin_details(self, service_id: str) -> list[dict]:
        with self._connection() as connection:
            self._require_service(connection, service_id)
            rows = connection.execute(
                "SELECT telegram_id, username FROM service_admins "
                "WHERE service_id = ? ORDER BY telegram_id",
                (service_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    # --- Опрос готовности (этап 5) ---

    READINESS_DEFAULTS = {
        "enabled": False,
        "start_time": "10:00",
        "duration_days": 3,
        "deadline_time": "20:00",
        "remind_days": 1,
        "intro_template": None,
        "reminder_template": None,
        "closed_template": None,
        "chat_reminder_template": None,
        "question_template": None,
        "plan_title_template": None,
        "plan_update_template": None,
        "phrases": {},
        "month_min": 0,
        "month_max": None,
        "people_needed": 1,
        "people_max": None,
        "day_from": 0,
        "day_to": 0,
        "reverse_poll": False,
    }

    def get_readiness_settings(self, service_id: str) -> dict:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT enabled, start_time, duration_days, deadline_time, "
                "remind_days, enabled_at, intro_template, reminder_template, "
                "closed_template, chat_reminder_template, question_template, "
                "plan_title_template, plan_update_template, phrases_json, "
                "month_min, month_max, "
                "people_needed, people_max, day_from, day_to, reverse_poll "
                "FROM readiness_settings WHERE service_id = ?",
                (service_id,),
            ).fetchone()
        if row is None:
            return {
                **self.READINESS_DEFAULTS, "phrases": {}, "enabled_at": None,
            }
        settings = {
            **dict(row), "enabled": bool(row["enabled"]),
            "reverse_poll": bool(row["reverse_poll"]),
        }
        settings["phrases"] = json.loads(settings.pop("phrases_json") or "{}")
        return settings

    def set_readiness_phrase(
        self, actor_id: int, service_id: str, name: str, text: str | None,
    ) -> dict:
        """Своя короткая фраза (None — встроенная)."""
        if name not in READINESS_PHRASES:
            raise ValueError(f"Неизвестная фраза: {name}.")
        phrases = dict(self.get_readiness_settings(service_id)["phrases"])
        phrases[name] = text
        return self.set_readiness_settings(
            actor_id, service_id, phrases=phrases
        )

    def set_readiness_settings(
        self, actor_id: int, service_id: str, **changes
    ) -> dict:
        _validate_positive_id(actor_id, "actor_id")
        unknown = set(changes) - set(self.READINESS_DEFAULTS)
        if unknown:
            raise ValueError(f"Неизвестные настройки: {', '.join(sorted(unknown))}")
        current = self.get_readiness_settings(service_id)
        new = {**current, **changes}
        if not isinstance(new["enabled"], bool):
            raise ValueError("enabled должен быть логическим значением")
        if not isinstance(new["reverse_poll"], bool):
            raise ValueError("reverse_poll должен быть логическим значением")
        for key in ("start_time", "deadline_time"):
            if not isinstance(new[key], str) or not re.fullmatch(
                r"(?:[01]\d|2[0-3]):[0-5]\d", new[key]
            ):
                raise ValueError("Время должно быть в формате HH:MM.")
        for key, low, high in (("duration_days", 1, 6), ("remind_days", 0, 5)):
            value = new[key]
            if isinstance(value, bool) or not isinstance(value, int) or not (
                low <= value <= high
            ):
                raise ValueError(f"Значение должно быть от {low} до {high}.")
        if new["remind_days"] >= new["duration_days"]:
            new["remind_days"] = new["duration_days"] - 1
        low, high = new["people_needed"], new["people_max"]
        if (
            isinstance(low, bool) or not isinstance(low, int)
            or not 1 <= low <= 5
            or high is not None and (
                isinstance(high, bool) or not isinstance(high, int)
                or not low <= high <= 6
            )
        ):
            raise ValueError("Нужно людей: от 1 до 5, максимум — до 6.")
        if high == low:
            new["people_max"] = None
        day_from, day_to = new["day_from"], new["day_to"]
        if (
            any(isinstance(value, bool) or not isinstance(value, int)
                for value in (day_from, day_to))
            or not -6 <= day_from <= 0 <= day_to <= 6
        ):
            raise ValueError("Дни служения: от вс −6 до вс +6, включая вс.")
        _validate_month_limit(new["month_min"], new["month_max"])
        if not isinstance(new["phrases"], dict):
            raise ValueError("Фразы — словарь.")
        new["phrases"] = {
            name: text for name, text in new["phrases"].items()
            if text is not None
        }
        for name, text in new["phrases"].items():
            if name not in READINESS_PHRASES:
                raise ValueError(f"Неизвестная фраза: {name}.")
            _, fields, limit, label = READINESS_PHRASES[name]
            if not isinstance(text, str) or not text.strip():
                raise ValueError(f"{label}: не может быть пустым.")
            if len(text) > limit or "\n" in text:
                raise ValueError(
                    f"{label}: одной строкой, не больше {limit} символов."
                )
            if fields:
                validate_notification_template(text, fields, label)
            elif "\x00" in text:
                raise ValueError(f"{label}: текст содержит недопустимые символы.")
        for key, fields, label in (
            ("intro_template", READINESS_INTRO_FIELDS, "Текст опроса"),
            ("reminder_template", READINESS_REMINDER_FIELDS,
             "Текст напоминания"),
            ("closed_template", READINESS_CLOSED_FIELDS, "Текст завершения"),
            ("chat_reminder_template", READINESS_REMINDER_FIELDS,
             "Напоминание в чат"),
            ("question_template", READINESS_QUESTION_FIELDS,
             "Вопрос опроса"),
            ("plan_title_template", PLAN_TITLE_FIELDS, "Заголовок графика"),
            ("plan_update_template", PLAN_TITLE_FIELDS,
             "Заголовок обновления"),
        ):
            value = new[key]
            if value is None:
                continue
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{label} не может быть пустым.")
            limit = 300 if key == "question_template" else 1500
            if len(value) > limit:
                raise ValueError(f"{label}: не больше {limit} символов.")
            validate_notification_template(value, fields, label)
        with self._connection() as connection:
            self._require_service_editor(connection, actor_id, service_id)
            self._require_service(connection, service_id)
            now = _utc_now()
            # Когда включён автозапуск: «не запущен вовремя» — только если
            # автозапуск был включён до времени запуска.
            if new["enabled"] and not current["enabled"]:
                new["enabled_at"] = now
            connection.execute(
                "INSERT INTO readiness_settings (service_id, enabled, "
                "start_time, duration_days, deadline_time, remind_days, "
                "enabled_at, intro_template, reminder_template, "
                "closed_template, chat_reminder_template, question_template, "
                "plan_title_template, plan_update_template, phrases_json, "
                "month_min, month_max, "
                "people_needed, people_max, day_from, day_to, reverse_poll, "
                "updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(service_id) DO UPDATE SET "
                "enabled = excluded.enabled, start_time = excluded.start_time, "
                "duration_days = excluded.duration_days, "
                "deadline_time = excluded.deadline_time, "
                "remind_days = excluded.remind_days, "
                "enabled_at = excluded.enabled_at, "
                "intro_template = excluded.intro_template, "
                "reminder_template = excluded.reminder_template, "
                "closed_template = excluded.closed_template, "
                "chat_reminder_template = excluded.chat_reminder_template, "
                "question_template = excluded.question_template, "
                "plan_title_template = excluded.plan_title_template, "
                "plan_update_template = excluded.plan_update_template, "
                "phrases_json = excluded.phrases_json, "
                "month_min = excluded.month_min, "
                "month_max = excluded.month_max, "
                "people_needed = excluded.people_needed, "
                "people_max = excluded.people_max, "
                "day_from = excluded.day_from, day_to = excluded.day_to, "
                "reverse_poll = excluded.reverse_poll, "
                "updated_at = excluded.updated_at",
                (
                    service_id, int(new["enabled"]), new["start_time"],
                    new["duration_days"], new["deadline_time"],
                    new["remind_days"], new["enabled_at"],
                    new["intro_template"], new["reminder_template"],
                    new["closed_template"], new["chat_reminder_template"],
                    new["question_template"], new["plan_title_template"],
                    new["plan_update_template"],
                    json.dumps(new["phrases"], ensure_ascii=False),
                    new["month_min"], new["month_max"],
                    new["people_needed"], new["people_max"],
                    new["day_from"], new["day_to"],
                    int(new["reverse_poll"]), now,
                ),
            )
            if (new["day_from"], new["day_to"]) != (
                current["day_from"], current["day_to"]
            ):
                self._mark_days_changed(connection, service_id)
            self._audit(
                connection, actor_id, "readiness_settings_updated", service_id,
                changes,
            )
        return new

    def readiness_people(self, service_id: str) -> list[dict]:
        """Опрашиваемые: состав («Списки» и добавленные в боте) и привязки
        к задачам. Задачи — только включённые; без задач человек в
        распределении не участвует."""
        with self._connection() as connection:
            enabled = {
                row["name"]
                for row in connection.execute(
                    "SELECT name FROM service_tasks "
                    "WHERE service_id = ? AND enabled = 1",
                    (service_id,),
                )
            }
            # Служение без задач — одна задача с его названием: в ней все.
            implicit = None
            if connection.execute(
                "SELECT 1 FROM service_tasks WHERE service_id = ? LIMIT 1",
                (service_id,),
            ).fetchone() is None:
                row = connection.execute(
                    "SELECT name FROM services WHERE id = ?", (service_id,)
                ).fetchone()
                implicit = row["name"] if row else None
            members = connection.execute(
                "SELECT name, telegram_id, username, tasks_json, in_list, "
                "source, day_offset, month_min, month_max, week_tasks_json "
                "FROM service_members WHERE service_id = ? "
                "ORDER BY in_list DESC, rowid",
                (service_id,),
            ).fetchall()
            contacts = connection.execute(
                "SELECT task, name, telegram_id, username "
                "FROM responsible_contacts WHERE service_id = ? "
                "ORDER BY task, name",
                (service_id,),
            ).fetchall()
        # Человек — Telegram ID, а у непривязанного — имя: два аккаунта с
        # одинаковым именем остаются разными людьми.
        people: list[dict] = []
        by_id: dict[int, dict] = {}
        by_name: dict[str, dict] = {}
        for row in members:
            person = {
                "name": row["name"],
                "telegram_id": row["telegram_id"],
                "username": row["username"] or "",
                "tasks": {
                    task for task in json.loads(row["tasks_json"])
                    if task in enabled
                },
                # В составе: в «Списках» или добавлен в боте (пропавший из
                # «Списков» — нет).
                "active": bool(row["in_list"]) or row["source"] != "excel",
                "day_offset": row["day_offset"],
                "month_limit": _member_limit(row),
                "week_tasks": _week_tasks(row),
            }
            if row["telegram_id"] in by_id:
                by_id[row["telegram_id"]]["tasks"] |= person["tasks"]
                by_id[row["telegram_id"]]["active"] |= person["active"]
                # Личный день и лимит — с любой строки этого человека.
                for field in ("day_offset", "month_limit", "week_tasks"):
                    if by_id[row["telegram_id"]][field] is None:
                        by_id[row["telegram_id"]][field] = person[field]
                continue
            people.append(person)
            if row["telegram_id"]:
                by_id[row["telegram_id"]] = person
            else:
                by_name.setdefault(row["name"], person)
        for row in contacts:
            person = by_id.get(row["telegram_id"])
            if person is None:
                person = by_name.pop(row["name"], None)
                if person is not None:
                    person["telegram_id"] = row["telegram_id"]
                else:
                    person = {
                        "name": row["name"],
                        "telegram_id": row["telegram_id"],
                        "username": "",
                        "tasks": set(),
                        "day_offset": None,
                        "month_limit": None,
                        "week_tasks": None,
                    }
                    people.append(person)
                by_id[row["telegram_id"]] = person
            if not person["username"]:
                person["username"] = row["username"] or ""
            if row["task"] in enabled:
                person["tasks"].add(row["task"])
        names = [person["name"] for person in people]
        result = []
        for person in people:
            # Ключ распределения уникален и при совпадении имён.
            key = person["name"]
            if names.count(key) > 1:
                key = f"{key} · {person['telegram_id'] or 'без Telegram'}"
            active = person.pop("active", True)
            if implicit:
                tasks = {implicit} if active else set()
            else:
                tasks = person["tasks"]
            # in_roster — в «Списках» или добавлен в боте (выбывший — нет).
            result.append({
                **person, "key": key, "tasks": sorted(tasks),
                "in_roster": active,
            })
        return result

    def get_nav_hints(self, telegram_id: int) -> list[int]:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT message_ids_json FROM nav_hints WHERE telegram_id = ?",
                (telegram_id,),
            ).fetchone()
        return json.loads(row["message_ids_json"]) if row else []

    def set_nav_hints(self, telegram_id: int, message_ids: list[int]) -> None:
        # Служебное состояние интерфейса — не повод для резервной копии.
        with self._connection(notify=False) as connection:
            if message_ids:
                connection.execute(
                    "INSERT INTO nav_hints (telegram_id, message_ids_json) "
                    "VALUES (?, ?) ON CONFLICT(telegram_id) DO UPDATE SET "
                    "message_ids_json = excluded.message_ids_json",
                    (telegram_id, json.dumps(list(message_ids))),
                )
            else:
                connection.execute(
                    "DELETE FROM nav_hints WHERE telegram_id = ?",
                    (telegram_id,),
                )

    def get_readiness_run(self, service_id: str, month: str) -> dict | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM readiness_runs WHERE service_id = ? AND month = ?",
                (service_id, month),
            ).fetchone()
        return self._readiness_run(row)

    def get_readiness_run_by_id(self, run_id: int) -> dict | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM readiness_runs WHERE id = ?", (run_id,)
            ).fetchone()
        return self._readiness_run(row)

    def get_readiness_run_by_poll(self, poll_id: str) -> dict | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM readiness_runs WHERE poll_id = ?", (poll_id,)
            ).fetchone()
        return self._readiness_run(row)

    def list_open_readiness_runs(self) -> list[dict]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM readiness_runs WHERE status = 'open' "
                "AND month NOT LIKE '%#%' ORDER BY deadline, id"
            ).fetchall()
        return [self._readiness_run(row) for row in rows]

    @staticmethod
    def _readiness_run(row) -> dict | None:
        if row is None:
            return None
        result = {
            **dict(row), "dates": json.loads(row["dates_json"]),
            "options": json.loads(row["options_json"] or "[]"),
            "reverse_poll": bool(row["reverse_poll"]),
        }
        return result

    def create_readiness_run(
        self,
        service_id: str,
        month: str,
        *,
        status: str,
        dates: list[str] = (),
        options: list[dict] = (),
        reverse_poll: bool = False,
        poll_id: str | None = None,
        chat_id: int | None = None,
        message_id: int | None = None,
        intro_message_id: int | None = None,
        deadline: str | None = None,
    ) -> dict:
        """Опрос месяца; второй на тот же месяц — ValueError."""
        if status not in {"open", "skipped"}:
            raise ValueError("Неизвестный статус опроса")
        with self._connection() as connection:
            self._require_service(connection, service_id)
            try:
                cursor = connection.execute(
                    "INSERT INTO readiness_runs (service_id, month, status, "
                    "dates_json, options_json, reverse_poll, "
                    "poll_id, chat_id, message_id, "
                    "intro_message_id, deadline, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        service_id, month, status, json.dumps(list(dates)),
                        json.dumps(list(options), ensure_ascii=False),
                        int(reverse_poll),
                        poll_id, chat_id, message_id, intro_message_id,
                        deadline, _utc_now(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError("Опрос на этот месяц уже есть.") from exc
            run_id = cursor.lastrowid
        return self.get_readiness_run_by_id(run_id)

    def delete_readiness_run(self, run_id: int) -> None:
        """Отмена пропуска месяца или перезапуск: опрос удаляется вместе
        с ответами и отметками напоминаний."""
        with self._connection() as connection:
            for table in ("readiness_answers", "readiness_reminders"):
                connection.execute(
                    f"DELETE FROM {table} WHERE run_id = ?", (run_id,)
                )
            connection.execute(
                "DELETE FROM readiness_runs WHERE id = ?", (run_id,)
            )

    def list_parked_readiness_runs(self) -> list[dict]:
        """Опросы, отложенные перезапуском (месяц «YYYY-MM#id»): бот
        прервался между отправкой нового опроса и удалением старого."""
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM readiness_runs WHERE month LIKE '%#%' ORDER BY id"
            ).fetchall()
        return [self._readiness_run(row) for row in rows]

    def attach_readiness_poll(
        self,
        run_id: int,
        *,
        poll_id: str,
        chat_id: int,
        message_id: int,
        intro_message_id: int | None,
    ) -> dict:
        """Опрос в чате отправлен — привязать его к записи месяца."""
        with self._connection() as connection:
            connection.execute(
                "UPDATE readiness_runs SET poll_id = ?, chat_id = ?, "
                "message_id = ?, intro_message_id = ? WHERE id = ?",
                (poll_id, chat_id, message_id, intro_message_id, run_id),
            )
        return self.get_readiness_run_by_id(run_id)

    def move_readiness_run(self, run_id: int, month: str) -> None:
        """Переносит опрос под другой ключ месяца: при перезапуске старый
        откладывается, пока не отправлен новый (при сбое — возвращается)."""
        with self._connection() as connection:
            connection.execute(
                "UPDATE readiness_runs SET month = ? WHERE id = ?",
                (month, run_id),
            )

    def close_readiness_run(self, run_id: int) -> bool:
        """True — опрос закрыт этим вызовом (защита от двойного итога)."""
        with self._connection() as connection:
            cursor = connection.execute(
                "UPDATE readiness_runs SET status = 'closed', closed_at = ? "
                "WHERE id = ? AND status = 'open'",
                (_utc_now(), run_id),
            )
        return cursor.rowcount > 0

    def mark_readiness_run(self, run_id: int, **flags: bool) -> None:
        """poll_stopped / result_sent / closed_announced — шаги закрытия,
        которые удались."""
        for name in flags:
            if name not in {"poll_stopped", "result_sent", "closed_announced"}:
                raise ValueError(f"Неизвестный шаг: {name}")
        with self._connection(notify=False) as connection:
            for name, value in flags.items():
                connection.execute(
                    f"UPDATE readiness_runs SET {name} = ? WHERE id = ?",
                    (int(bool(value)), run_id),
                )

    def list_unfinished_readiness_runs(self) -> list[dict]:
        """Закрытые опросы, у которых не остановлен опрос или не дошёл итог."""
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM readiness_runs WHERE status = 'closed' "
                "AND month NOT LIKE '%#%' AND (poll_stopped = 0 "
                "OR result_sent = 0 OR closed_announced = 0) ORDER BY id"
            ).fetchall()
        return [self._readiness_run(row) for row in rows]

    @staticmethod
    def _schedule_plan(row) -> dict | None:
        if row is None:
            return None
        plan = dict(row)
        for name in ("dates", "tasks", "slots", "last_served"):
            plan[name] = json.loads(plan.pop(f"{name}_json"))
        approved = plan.pop("approved_json")
        plan["approved"] = json.loads(approved) if approved else None
        plan["days"] = json.loads(plan.pop("days_json") or "{}")
        served = plan.pop("served_offsets_json", None)
        plan["served_offsets"] = json.loads(served) if served else {}
        # Снимок есть (пусть и пустой — все по воскресеньям) или график
        # утверждён до появления снимка.
        plan["served_snapshot"] = served is not None
        approved_tasks = plan.pop("approved_tasks_json", None)
        plan["approved_tasks"] = (
            json.loads(approved_tasks) if approved_tasks else None
        )
        approved_days = plan.pop("approved_days_json")
        plan["approved_days"] = (
            json.loads(approved_days) if approved_days else {}
        )
        approved_slot_days = plan.pop("approved_slot_days_json", None)
        plan["approved_slot_days"] = (
            json.loads(approved_slot_days) if approved_slot_days else {}
        )
        message_ids = plan.pop("message_ids_json", None)
        # Опубликован до шага 5б — известна только первая часть.
        plan["message_ids"] = (
            json.loads(message_ids) if message_ids
            else [plan["message_id"]] if plan.get("message_id") else []
        )
        baseline = plan.pop("baseline_json", None)
        plan["baseline"] = json.loads(baseline) if baseline else None
        # Чат или тема служения сменились (/here) — опубликованный график
        # не там: его нужно опубликовать заново, даже без правок.
        service_chat = plan.pop("service_chat", None)
        service_thread = plan.pop("service_thread", None)
        plan["moved"] = bool(
            plan["approved"] is not None and plan["message_ids"]
            and service_chat is not None and (
                plan.get("chat_id") != service_chat
                or (plan_target_thread(plan) or None)
                != (service_thread or None)
            )
        )
        # Правки после утверждения ещё не опубликованы (порядок людей в
        # задаче не важен).
        def normal(slots):
            return None if slots is None else {
                day: {task: sorted(people) for task, people in tasks.items()}
                for day, tasks in slots.items()
            }

        # republish — после утверждения сменились дни служения (окно или
        # личный день): опубликованные даты устарели.
        plan["dirty"] = bool(plan.get("republish")) or plan["moved"] or (
            normal(plan["approved"]) != normal(plan["slots"])
        ) or (
            plan["approved"] is not None
            and plan["approved_days"] != plan["days"]
        )
        return plan

    def get_schedule_plan(self, service_id: str, month: str) -> dict | None:
        """График месяца: slots — дата → задача → ключи людей
        (`readiness_people`), approved — опубликованная копия."""
        with self._connection() as connection:
            row = connection.execute(
                "SELECT p.*, s.group_chat_id AS service_chat, "
                "s.group_thread_id AS service_thread "
                "FROM schedule_plans p JOIN services s ON s.id = p.service_id "
                "WHERE p.service_id = ? AND p.month = ?",
                (service_id, month),
            ).fetchone()
        return self._schedule_plan(row)

    def save_schedule_draft(
        self,
        service_id: str,
        month: str,
        *,
        run_id: int | None,
        dates: list[str],
        tasks: list[dict],
        slots: dict,
        last_served: dict,
        note: str | None = None,
    ) -> dict:
        """Предложенный (пересчитанный) график: рабочая копия заменяется,
        ручные правки сбрасываются; утверждённая копия остаётся."""
        with self._connection() as connection:
            self._require_service(connection, service_id)
            connection.execute(
                "INSERT INTO schedule_plans (service_id, month, run_id, "
                "dates_json, tasks_json, slots_json, last_served_json, note, "
                "edited, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?) "
                "ON CONFLICT(service_id, month) DO UPDATE SET "
                "run_id = excluded.run_id, source = 'poll', "
                "dates_json = excluded.dates_json, "
                "tasks_json = excluded.tasks_json, "
                "slots_json = excluded.slots_json, "
                "last_served_json = excluded.last_served_json, "
                "note = excluded.note, edited = 0, days_json = '{}', "
                "updated_at = excluded.updated_at",
                (
                    service_id, month, run_id, json.dumps(dates),
                    json.dumps(tasks, ensure_ascii=False),
                    json.dumps(slots, ensure_ascii=False),
                    json.dumps(last_served, ensure_ascii=False),
                    note, _utc_now(),
                ),
            )
        return self.get_schedule_plan(service_id, month)

    def rename_plan_task(
        self, service_id: str, month: str, old: str, new: str,
    ) -> dict:
        """Переименовать задачу в рабочей копии графика (служение без задач
        переименовано в каталоге). Утверждённая копия — как опубликовано:
        график становится «с правками», и «✅ Утвердить изменения»
        опубликует новое название (одной строкой в списке изменений)."""
        def renamed(slots: dict) -> str:
            return json.dumps({
                day: {(new if task == old else task): people
                      for task, people in day_tasks.items()}
                for day, day_tasks in slots.items()
            }, ensure_ascii=False)

        # Чтение и запись — в одной транзакции: параллельная правка графика
        # не затрётся старой копией.
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT tasks_json, slots_json FROM schedule_plans "
                "WHERE service_id = ? AND month = ?",
                (service_id, month),
            ).fetchone()
            if row is None:
                raise ValueError("График не найден.")
            tasks = [
                {**task, "name": new, "implicit": True}
                if task["name"] == old else task
                for task in json.loads(row["tasks_json"])
            ]
            connection.execute(
                "UPDATE schedule_plans SET tasks_json = ?, slots_json = ?, "
                "updated_at = ? WHERE service_id = ? AND month = ?",
                (
                    json.dumps(tasks, ensure_ascii=False),
                    renamed(json.loads(row["slots_json"])), _utc_now(),
                    service_id, month,
                ),
            )
        return self.get_schedule_plan(service_id, month)

    def set_schedule_day(
        self, actor_id: int, service_id: str, month: str, day: str, key: str,
        offset: int | None,
    ) -> dict:
        """День служения человека только на эту дату графика (сдвиг от
        воскресенья; None — как обычно)."""
        _validate_positive_id(actor_id, "actor_id")
        if offset is not None and (
            isinstance(offset, bool) or not isinstance(offset, int)
            or not -6 <= offset <= 6
        ):
            raise ValueError("День: от вс −6 до вс +6.")
        with self._connection() as connection:
            self._require_service_editor(connection, actor_id, service_id)
            row = connection.execute(
                "SELECT days_json FROM schedule_plans "
                "WHERE service_id = ? AND month = ?",
                (service_id, month),
            ).fetchone()
            if row is None:
                raise ValueError("График не найден — откройте его заново.")
            days = json.loads(row["days_json"] or "{}")
            # День на дату — для всех задач человека: разовые дни задач
            # (из Excel) он заменяет.
            for other in list(days.get(day, {})):
                if split_day_key(other)[0] == key and other != key:
                    days[day].pop(other)
            if offset is None:
                days.get(day, {}).pop(key, None)
                if not days.get(day):
                    days.pop(day, None)
            else:
                days.setdefault(day, {})[key] = offset
            connection.execute(
                "UPDATE schedule_plans SET days_json = ?, edited = 1, "
                "updated_at = ? WHERE service_id = ? AND month = ?",
                (
                    json.dumps(days, ensure_ascii=False), _utc_now(),
                    service_id, month,
                ),
            )
        return self.get_schedule_plan(service_id, month)

    def set_schedule_slots(
        self, actor_id: int, service_id: str, month: str, slots: dict,
    ) -> dict:
        """Ручная правка графика админом служения («✏️ Изменить»). Разовые
        дни убранных из даты людей отбрасываются."""
        _validate_positive_id(actor_id, "actor_id")
        with self._connection() as connection:
            self._require_service_editor(connection, actor_id, service_id)
            row = connection.execute(
                "SELECT days_json FROM schedule_plans "
                "WHERE service_id = ? AND month = ?",
                (service_id, month),
            ).fetchone()
            if row is None:
                raise ValueError("График не найден — откройте его заново.")
            days = {}
            for day, people_days in json.loads(row["days_json"] or "{}").items():
                serving = {
                    key for people in slots.get(day, {}).values()
                    for key in people
                }

                def stays(key: str) -> bool:
                    person, task = split_day_key(key)
                    if task is None:
                        return person in serving
                    return person in slots.get(day, {}).get(task, [])

                kept = {
                    key: offset for key, offset in people_days.items()
                    if stays(key)
                }
                if kept:
                    days[day] = kept
            connection.execute(
                "UPDATE schedule_plans SET slots_json = ?, days_json = ?, "
                "edited = 1, updated_at = ? WHERE service_id = ? AND month = ?",
                (
                    json.dumps(slots, ensure_ascii=False),
                    json.dumps(days, ensure_ascii=False), _utc_now(),
                    service_id, month,
                ),
            )
        return self.get_schedule_plan(service_id, month)

    def import_excel_plan(
        self, actor_id: int, service_id: str, month: str, *,
        dates: list[str], tasks: list[dict], slots: dict, days: dict,
        served_offsets: dict | None = None,
        approved_slot_days: dict | None = None,
    ) -> dict:
        """«📊 График Excel» → «✅ Подтвердить» (решение 15): график месяца
        из Excel как есть — итоговый график в боте (утверждён, без
        публикации в чате). Прежний график месяца (по опросу) заменяется;
        вернуть его — снова «🔄 Пересчитать» в «📅 График»."""
        _validate_positive_id(actor_id, "actor_id")
        now = _utc_now()
        with self._connection() as connection:
            self._require_service_editor(connection, actor_id, service_id)
            self._require_service(connection, service_id)
            values = (
                json.dumps(dates), json.dumps(tasks, ensure_ascii=False),
                json.dumps(slots, ensure_ascii=False),
                json.dumps(days, ensure_ascii=False),
            )
            connection.execute(
                "INSERT INTO schedule_plans (service_id, month, run_id, "
                "source, dates_json, tasks_json, slots_json, "
                "last_served_json, note, edited, approved_json, approved_by, "
                "approved_at, chat_id, message_id, republish, days_json, "
                "approved_days_json, served_offsets_json, approved_tasks_json, "
                "approved_slot_days_json, updated_at) "
                "VALUES (?, ?, NULL, 'excel', ?, ?, ?, '{}', NULL, 0, ?, ?, ?, "
                "NULL, NULL, 0, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(service_id, month) DO UPDATE SET run_id = NULL, "
                "source = 'excel', dates_json = excluded.dates_json, "
                "tasks_json = excluded.tasks_json, "
                "slots_json = excluded.slots_json, last_served_json = '{}', "
                "note = NULL, edited = 0, approved_json = excluded.approved_json, "
                "approved_by = excluded.approved_by, "
                "approved_at = excluded.approved_at, chat_id = NULL, "
                "message_id = NULL, message_ids_json = NULL, "
                "thread_id = NULL, target_thread_id = NULL, "
                "baseline_json = NULL, republish = 0, "
                "days_json = excluded.days_json, "
                "approved_days_json = excluded.approved_days_json, "
                "served_offsets_json = excluded.served_offsets_json, "
                "approved_tasks_json = excluded.approved_tasks_json, "
                "approved_slot_days_json = excluded.approved_slot_days_json, "
                "updated_at = excluded.updated_at",
                (
                    service_id, month, values[0], values[1], values[2],
                    values[2], actor_id, now, values[3], values[3],
                    json.dumps(served_offsets or {}, ensure_ascii=False),
                    values[1],
                    json.dumps(approved_slot_days or {}, ensure_ascii=False),
                    now,
                ),
            )
            self._audit(
                connection, actor_id, "schedule_plan_excel", service_id,
                {"month": month},
            )
        return self.get_schedule_plan(service_id, month)

    def cancel_schedule_approval(
        self, service_id: str, month: str,
    ) -> dict | None:
        """Новый опрос месяца отменяет утверждённый график (решение
        28.10): прежний в чате уже не актуален, даже если совпадёт.
        Публикация (чат, id частей) остаётся — новая её заменит. → прежние
        {chat_id, message_ids}; None — утверждённого не было."""
        with self._connection() as connection:
            row = connection.execute(
                "SELECT chat_id, message_id, message_ids_json "
                "FROM schedule_plans WHERE service_id = ? AND month = ? "
                "AND approved_json IS NOT NULL",
                (service_id, month),
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                "UPDATE schedule_plans SET approved_json = NULL, "
                "approved_by = NULL, approved_at = NULL, "
                "approved_days_json = NULL, approved_tasks_json = NULL, "
                "served_offsets_json = NULL, approved_slot_days_json = NULL, "
                "baseline_json = NULL, "
                # Загруженный из Excel перестаёт быть итоговым: дальше
                # график — по новому опросу.
                "source = 'poll', "
                "republish = 0 WHERE service_id = ? AND month = ?",
                (service_id, month),
            )
        ids = (
            json.loads(row["message_ids_json"]) if row["message_ids_json"]
            else [row["message_id"]] if row["message_id"] else []
        )
        return {"chat_id": row["chat_id"], "message_ids": ids}

    def approve_schedule_plan(
        self,
        actor_id: int,
        service_id: str,
        month: str,
        slots: dict,
        *,
        chat_id: int | None,
        message_id: int | None,
        updated_at: str | None = None,
        days: dict | None = None,
        served_offsets: dict | None = None,
        approved_slot_days: dict | None = None,
        message_ids: list[int] | None = None,
        baseline: dict | None = None,
        thread_id: int | None = None,
        target_thread_id: int | None = None,
    ) -> dict:
        """«✅ Утвердить»: опубликованная копия = slots. updated_at —
        версия опубликованного графика: если дни служения сменились, пока
        шла публикация, отметка «опубликовать заново» остаётся.
        message_ids — части закреплённой публикации; baseline — график
        первой публикации (None — оставить сохранённый)."""
        _validate_positive_id(actor_id, "actor_id")
        if message_ids is None and message_id is not None:
            message_ids = [message_id]
        with self._connection() as connection:
            self._require_service_editor(connection, actor_id, service_id)
            cursor = connection.execute(
                "UPDATE schedule_plans SET approved_json = ?, approved_by = ?, "
                "approved_tasks_json = tasks_json, "
                "approved_at = ?, chat_id = ?, message_id = ?, "
                "message_ids_json = ?, thread_id = ?, target_thread_id = ?, "
                "baseline_json = COALESCE(?, baseline_json), "
                "approved_days_json = ?, served_offsets_json = ?, "
                "approved_slot_days_json = ?, "
                "republish = CASE WHEN ? IS NULL OR updated_at = ? THEN 0 "
                "ELSE republish END "
                "WHERE service_id = ? AND month = ?",
                (
                    json.dumps(slots, ensure_ascii=False), actor_id,
                    _utc_now(), chat_id, message_id,
                    json.dumps(message_ids) if message_ids else None,
                    thread_id, target_thread_id,
                    json.dumps(baseline, ensure_ascii=False)
                    if baseline is not None else None,
                    json.dumps(days or {}, ensure_ascii=False),
                    json.dumps(served_offsets or {}, ensure_ascii=False),
                    json.dumps(approved_slot_days or {}, ensure_ascii=False),
                    updated_at, updated_at, service_id, month,
                ),
            )
            if cursor.rowcount == 0:
                raise ValueError("График не найден — откройте его заново.")
            self._audit(
                connection, actor_id, "schedule_plan_approved", service_id,
                {"month": month},
            )
        return self.get_schedule_plan(service_id, month)

    def reminded_readiness_ids(self, run_id: int) -> set[int]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT telegram_id FROM readiness_reminders WHERE run_id = ?",
                (run_id,),
            ).fetchall()
        return {row["telegram_id"] for row in rows}

    def mark_readiness_reminded(self, run_id: int, telegram_id: int) -> None:
        with self._connection(notify=False) as connection:
            connection.execute(
                "INSERT OR IGNORE INTO readiness_reminders "
                "(run_id, telegram_id, sent_at) VALUES (?, ?, ?)",
                (run_id, telegram_id, _utc_now()),
            )

    def has_readiness_notice(
        self, service_id: str, month: str, kind: str
    ) -> bool:
        with self._connection() as connection:
            return connection.execute(
                "SELECT 1 FROM readiness_notices "
                "WHERE service_id = ? AND month = ? AND kind = ?",
                (service_id, month, kind),
            ).fetchone() is not None

    def set_readiness_deadline(self, run_id: int, deadline: str) -> bool:
        with self._connection() as connection:
            cursor = connection.execute(
                "UPDATE readiness_runs SET deadline = ? "
                "WHERE id = ? AND status = 'open'",
                (deadline, run_id),
            )
        return cursor.rowcount > 0

    def record_readiness_answer(
        self,
        poll_id: str,
        telegram_id: int,
        username: str,
        full_name: str,
        dates: list[str] | None,
    ) -> bool | None:
        """Ответ в опросе готовности; dates=None — голос отозван, [] —
        «Не смогу». None — это не опрос готовности; False — опрос закрыт."""
        _validate_positive_id(telegram_id, "telegram_id")
        with self._connection(notify=False) as connection:
            run = connection.execute(
                "SELECT id, status FROM readiness_runs WHERE poll_id = ?",
                (poll_id,),
            ).fetchone()
            if run is None:
                return None
            if run["status"] != "open":
                return False
            if dates is None:
                connection.execute(
                    "DELETE FROM readiness_answers "
                    "WHERE run_id = ? AND telegram_id = ?",
                    (run["id"], telegram_id),
                )
                return True
            connection.execute(
                """
                INSERT INTO readiness_answers (
                    run_id, telegram_id, username, full_name, dates_json,
                    answered_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id, telegram_id) DO UPDATE SET
                    username = excluded.username,
                    full_name = excluded.full_name,
                    dates_json = excluded.dates_json,
                    answered_at = excluded.answered_at
                """,
                (
                    run["id"], telegram_id, (username or "").lstrip("@"),
                    full_name or "", json.dumps(sorted(dates)), _utc_now(),
                ),
            )
        return True

    def list_readiness_answers(self, run_id: int) -> list[dict]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT telegram_id, username, full_name, dates_json, "
                "answered_at FROM readiness_answers WHERE run_id = ? "
                "ORDER BY answered_at, rowid",
                (run_id,),
            ).fetchall()
        return [
            {**dict(row), "dates": json.loads(row["dates_json"])}
            for row in rows
        ]

    def claim_readiness_notice(
        self, service_id: str, month: str, kind: str
    ) -> bool:
        """True — уведомление этого вида за месяц ещё не отправлялось."""
        with self._connection(notify=False) as connection:
            cursor = connection.execute(
                "INSERT OR IGNORE INTO readiness_notices "
                "(service_id, month, kind, sent_at) VALUES (?, ?, ?, ?)",
                (service_id, month, kind, _utc_now()),
            )
        return cursor.rowcount > 0

    def release_readiness_notice(
        self, service_id: str, month: str, kind: str
    ) -> None:
        with self._connection(notify=False) as connection:
            connection.execute(
                "DELETE FROM readiness_notices "
                "WHERE service_id = ? AND month = ? AND kind = ?",
                (service_id, month, kind),
            )

    @staticmethod
    def _require_super_admin(
        connection: sqlite3.Connection, actor_id: int
    ) -> None:
        if connection.execute(
            "SELECT 1 FROM super_admins WHERE telegram_id = ?",
            (actor_id,),
        ).fetchone() is None:
            raise PermissionError("Операция доступна только супер-администратору")

    @staticmethod
    def _require_service(
        connection: sqlite3.Connection, service_id: str
    ) -> None:
        if connection.execute(
            "SELECT 1 FROM services WHERE id = ? AND archived = 0",
            (service_id,),
        ).fetchone() is None:
            raise ValueError(f"Неизвестное служение: {service_id}")

    @staticmethod
    def _require_service_editor(
        connection: sqlite3.Connection, actor_id: int, service_id: str
    ) -> None:
        is_super_admin = connection.execute(
            "SELECT 1 FROM super_admins WHERE telegram_id = ?",
            (actor_id,),
        ).fetchone()
        is_service_admin = connection.execute(
            "SELECT 1 FROM service_admins "
            "WHERE telegram_id = ? AND service_id = ?",
            (actor_id, service_id),
        ).fetchone()
        if is_super_admin is None and is_service_admin is None:
            raise PermissionError(
                "Настройки доступны супер-администратору и администратору служения"
            )

    @staticmethod
    def _audit(
        connection: sqlite3.Connection,
        actor_id: int,
        action: str,
        service_id: str | None,
        details: dict,
    ) -> None:
        connection.execute(
            "INSERT INTO configuration_audit "
            "(actor_id, action, service_id, details_json, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                actor_id, action, service_id,
                json.dumps(details, ensure_ascii=False, sort_keys=True),
                _utc_now(),
            ),
        )


def _move_contact(
    connection: sqlite3.Connection, row, service_id: str, task: str
) -> None:
    """Переносит привязку под (service_id, task), не теряя людей.

    Если та же привязка (тот же человек с тем же именем) уже есть — старая
    строка удаляется как дубль. Если под целевой задачей то же имя
    принадлежит другому человеку или этот человек записан под другим
    именем, старая строка остаётся как есть: ничья привязка не удаляется.
    """
    conflict = connection.execute(
        "SELECT telegram_id, name FROM responsible_contacts "
        "WHERE service_id = ? AND task = ? "
        "AND (telegram_id = ? OR name = ?)",
        (service_id, task, row["telegram_id"], row["name"]),
    ).fetchall()
    if any(
        (item["telegram_id"], item["name"])
        != (row["telegram_id"], row["name"])
        for item in conflict
    ):
        return
    if conflict:
        connection.execute(
            "DELETE FROM responsible_contacts WHERE service_id = ? "
            "AND task = ? AND telegram_id = ?",
            (row["service_id"], row["task"], row["telegram_id"]),
        )
        return
    connection.execute(
        "UPDATE responsible_contacts SET service_id = ?, task = ? "
        "WHERE service_id = ? AND task = ? AND telegram_id = ?",
        (service_id, task, row["service_id"], row["task"], row["telegram_id"]),
    )


def _move_user_text(
    connection: sqlite3.Connection, text_id: int, service_id: str, task: str
) -> None:
    """Переносит личный текст под (service_id, task) вместе с правилами.

    Текст переезжает только вслед за привязкой пользователя к целевой
    задаче (_move_contact выполняется раньше): иначе текст и правила
    остаются рядом со своей привязкой под старым названием. Если у
    пользователя уже есть текст под целевой задачей, правила переходят к
    нему (свой текст сохраняется, если у цели его нет), а старая запись
    удаляется.
    """
    row = connection.execute(
        "SELECT * FROM user_reminder_texts WHERE id = ?", (text_id,)
    ).fetchone()
    if connection.execute(
        "SELECT 1 FROM responsible_contacts "
        "WHERE service_id = ? AND task = ? AND telegram_id = ?",
        (service_id, task, row["actor_id"]),
    ).fetchone() is None:
        return
    target = connection.execute(
        "SELECT id, body FROM user_reminder_texts "
        "WHERE actor_id = ? AND service_id = ? AND task = ? AND id != ?",
        (row["actor_id"], service_id, task, text_id),
    ).fetchone()
    if target is None:
        connection.execute(
            "UPDATE user_reminder_texts SET service_id = ?, task = ? "
            "WHERE id = ?",
            (service_id, task, text_id),
        )
        return
    connection.execute(
        "UPDATE user_reminder_schedules SET text_id = ? WHERE text_id = ?",
        (target["id"], text_id),
    )
    if target["body"] is None and row["body"] is not None:
        connection.execute(
            "UPDATE user_reminder_texts SET body = ? WHERE id = ?",
            (row["body"], target["id"]),
        )
    connection.execute(
        "DELETE FROM user_reminder_texts WHERE id = ?", (text_id,)
    )


def _validate_positive_id(value: int, field: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field} должен быть положительным целым числом")


def normalize_member_name(name) -> str:
    """Имя нового участника, введённое вручную: пробелы схлопываются,
    1–60 символов. Имена из графика и «Списков» хранятся как записаны."""
    name = " ".join(name.split()) if isinstance(name, str) else ""
    if not name or len(name) > 60 or "\x00" in name:
        raise ValueError("Имя участника: от 1 до 60 символов.")
    return name


def _week_tasks(row) -> list[str] | None:
    """Задачи человека «в одну неделю»; None — как у служения."""
    value = row["week_tasks_json"]
    return None if value is None else json.loads(value)


def _current_month() -> str:
    today = datetime.now(timezone.utc).date()
    return f"{today.year:04d}-{today.month:02d}"


USERNAME_BINDING_DAYS = 30
# Лимит служений человека в месяц — от 0 до MONTH_LIMIT_MAX.
MONTH_LIMIT_MAX = 5


def _validate_month_limit(low, high) -> None:
    if (
        isinstance(low, bool) or not isinstance(low, int)
        or not 0 <= low <= MONTH_LIMIT_MAX
        or high is not None and (
            isinstance(high, bool) or not isinstance(high, int)
            or not low <= high <= MONTH_LIMIT_MAX
        )
    ):
        raise ValueError(
            f"Служений в месяц: от 0 до {MONTH_LIMIT_MAX}, минимум не больше "
            "максимума."
        )


def _member_limit(row) -> tuple[int, int | None] | None:
    """Личный лимит строки состава; None — как у служения."""
    if row["month_min"] is None:
        return None
    return (row["month_min"], row["month_max"])


def _username_binding_cutoff() -> str:
    """Привязка по @username ждёт не дольше USERNAME_BINDING_DAYS."""
    return (
        datetime.now(timezone.utc) - timedelta(days=USERNAME_BINDING_DAYS)
    ).isoformat()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
