"""Каталог служений, их задач и колонок месячного графика.

Каталог читается из reminder_bot_services.json в корне проекта. Суперадмин
может включать, выключать и добавлять задачи в боте; эти правки хранятся в
SQLite и накладываются на каталог (см. ServiceRegistry.list_service_tasks).
"""
from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TypeVar

CATALOG_PATH = Path(__file__).resolve().parent.parent / "reminder_bot_services.json"
_SERVICE_ID_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789_")


# Разовый день одной задачи человека (из Excel: спевка в воскресенье) —
# ключ «человек␟задача» в днях графика: другие задачи той же недели он не
# сдвигает.
TASK_DAY_SEPARATOR = "\x1f"


def task_day_key(key: str, task: str) -> str:
    return f"{key}{TASK_DAY_SEPARATOR}{task}"


def split_day_key(key: str) -> tuple[str, str | None]:
    """Ключ дня графика → (человек, задача или None)."""
    person, _, task = key.partition(TASK_DAY_SEPARATOR)
    return person, task or None


def normalized_name(value: str) -> str:
    """Ключ сравнения названий: регистр, «ё», лишние пробелы и пробелы у скобок."""
    text = " ".join(str(value).casefold().replace("ё", "е").split())
    return text.replace("( ", "(").replace(" )", ")")


_TASK_DISPLAY_ORDER = {
    "order": ("старший", "в смене"),
    "sound": ("звук", "сборка"),
    "kitchen": ("старший", "в смене", "скатерти", "пироги"),
}
_T = TypeVar("_T")


def sort_tasks_for_display(
    tasks: Iterable[_T], service_id: str | None = None, *,
    name: Callable[[_T], str] = str,
) -> list[_T]:
    """Новая коллекция только для показа; позиции и данные не меняются.

    Незаданные задачи остаются в исходном стабильном порядке в конце.
    Без service_id поддерживаются старые форматтеры одного служения.
    Не использовать порядок показа как вход распределения.
    """
    order = (
        _TASK_DISPLAY_ORDER.get(service_id, ())
        if service_id is not None else
        tuple(dict.fromkeys(
            task for tasks in _TASK_DISPLAY_ORDER.values() for task in tasks
        ))
    )
    ranks = {task: index for index, task in enumerate(order)}
    return sorted(
        tasks, key=lambda task: ranks.get(normalized_name(name(task)), len(ranks)),
    )


@dataclass(frozen=True)
class ServiceTask:
    name: str
    aliases: tuple[str, ...] = ()
    enabled: bool = True


@dataclass(frozen=True)
class ScheduleColumn:
    headers: tuple[str, ...]
    layout: Literal["pairs", "single"]
    # Задача всех строк колонки без своей задачи («Слайды», «кофе»).
    task: str | None = None
    # Задача строк без задачи в начале блока даты («в смене» у Кухни).
    default_task: str | None = None


@dataclass(frozen=True)
class ServiceDefinition:
    id: str
    name: str
    aliases: tuple[str, ...]
    columns: tuple[ScheduleColumn, ...] = ()
    tasks: tuple[ServiceTask, ...] = ()

    @property
    def assignment_layout(self) -> Literal["pairs", "single"]:
        return self.columns[0].layout if self.columns else "pairs"


def _strings(value, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise ValueError(f"{field}: ожидается список непустых строк")
    return tuple(item.strip() for item in value)


def _parse_task(raw, service_id: str) -> ServiceTask:
    if isinstance(raw, str) and raw.strip():
        return ServiceTask(raw.strip())
    if not isinstance(raw, dict) or not isinstance(raw.get("name"), str):
        raise ValueError(f"{service_id}: задача должна быть строкой или объектом с name")
    name = raw["name"].strip()
    enabled = raw.get("enabled", True)
    if not name or not isinstance(enabled, bool):
        raise ValueError(f"{service_id}: некорректная задача {raw!r}")
    return ServiceTask(
        name, _strings(raw.get("aliases", []), f"{service_id}.{name}.aliases"),
        enabled,
    )


def _parse_column(raw, service_id: str) -> ScheduleColumn:
    if not isinstance(raw, dict) or raw.get("layout") not in {"pairs", "single"}:
        raise ValueError(f"{service_id}: колонка должна иметь layout pairs или single")
    values = {}
    for field in ("task", "default_task"):
        value = raw.get(field)
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise ValueError(f"{service_id}: некорректное поле колонки {field}")
        values[field] = value.strip() if value else None
    return ScheduleColumn(
        _strings(raw.get("headers"), f"{service_id}.columns.headers"),
        raw["layout"],
        **values,
    )


def parse_catalog(data) -> tuple[ServiceDefinition, ...]:
    """Проверяет структуру каталога и возвращает определения служений."""
    if not isinstance(data, dict) or not isinstance(data.get("services"), list):
        raise ValueError("Каталог служений: ожидается объект с полем services")
    services = []
    seen_ids: set[str] = set()
    seen_headers: dict[str, str] = {}
    for raw in data["services"]:
        if not isinstance(raw, dict):
            raise ValueError("Каталог служений: служение должно быть объектом")
        service_id = raw.get("id")
        name = raw.get("name")
        if (
            not isinstance(service_id, str)
            or not service_id
            or not set(service_id) <= _SERVICE_ID_CHARS
            or service_id in seen_ids
        ):
            raise ValueError(f"Каталог служений: некорректный id {service_id!r}")
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"{service_id}: не задано название")
        seen_ids.add(service_id)
        tasks = tuple(_parse_task(task, service_id) for task in raw.get("tasks", []))
        task_keys: dict[str, str] = {}
        for task in tasks:
            for key in (task.name, *task.aliases):
                owner = task_keys.setdefault(normalized_name(key), task.name)
                if owner != task.name:
                    raise ValueError(
                        f"{service_id}: «{key}» указано у задач «{owner}» и «{task.name}»"
                    )
        columns = tuple(
            _parse_column(column, service_id) for column in raw.get("columns", [])
        )
        for column in columns:
            for column_task in (column.task, column.default_task):
                if column_task and normalized_name(column_task) not in task_keys:
                    raise ValueError(
                        f"{service_id}: задача колонки «{column_task}» "
                        "не описана в tasks"
                    )
            for header in column.headers:
                owner = seen_headers.setdefault(normalized_name(header), service_id)
                if owner != service_id:
                    raise ValueError(
                        f"Заголовок графика «{header}» указан у {owner} и {service_id}"
                    )
        services.append(
            ServiceDefinition(
                id=service_id,
                name=name.strip(),
                aliases=_strings(raw.get("aliases", []), f"{service_id}.aliases"),
                columns=columns,
                tasks=tasks,
            )
        )
    if not services:
        raise ValueError("Каталог служений пуст")
    return tuple(services)


def load_catalog(path: Path = CATALOG_PATH) -> tuple[ServiceDefinition, ...]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Не удалось прочитать каталог служений {path}: {exc}") from exc
    try:
        return parse_catalog(data)
    except ValueError as exc:
        raise RuntimeError(f"Ошибка в каталоге служений {path}: {exc}") from exc


SERVICES = load_catalog()
SERVICES_BY_ID = {service.id: service for service in SERVICES}
