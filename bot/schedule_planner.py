"""Распределение отметившихся по воскресеньям и задачам (этап 5.4).

Правила заказчика:
1. В один день у человека не больше одной задачи служения.
2. Основной задачи нет — человек идёт туда, где он нужнее.
3. Нагрузка в месяце выравнивается.
4. Между служениями человека — как можно больший интервал, с учётом
   прошлого месяца. Служение две недели подряд — только если иначе
   место не закрыть: интервал важнее ответа на опрос.

Алгоритм:
1. Покрытие: на каждую неделю ищется паросочетание «человек → место
   в задаче», затем поток по фактическим дням устраняет недобор из-за
   пересечений дней соседних недель.
2. Справедливость: даты идут по порядку, на каждой берётся
   паросочетание минимальной стоимости среди максимальных; стоимость
   человека — прирост общей стоимости плана от ещё одного служения.
3. Улучшение: замены и обмены людьми, которые не уменьшают покрытие,
   но уменьшают общую стоимость.

Общая стоимость плана: Σ LOAD_WEIGHT × нагрузка² по людям + штрафы за
близкие служения (неделя, две) подряд у одного человека + RELUCTANT_WEIGHT
за каждое служение не ответившего (его ставят, только если ответивших не
хватает). Штраф за неделю подряд (BACK_TO_BACK_WEIGHT) больше
RELUCTANT_WEIGHT: лучше не ответивший, чем служение подряд.

У задачи — минимум людей (`needed`, недобор — ⚠️) и сверх него до
максимума (`extra`). Два прохода по месяцу: сначала обязательные места
всех задач на все даты (ответившие дешевле не ответивших), затем места
сверх минимума — только ответившими, у кого нет служения ни за неделю
до, ни через неделю после (иначе место сверх минимума остаётся пустым).

Задачи «в одну неделю» (решение 12: звук в вс и спевка в ср — один
человек): у кого они связаны, тот ставится на них вместе — одно составное
место, одно служение в неделю. Места связки, которые некем закрыть
вместе, закрываются по отдельности.

Конкуренция за дату (решение 16): при равной стоимости место получает
тот, у кого меньше отмеченных дат, затем кто дольше не служил, затем кто
раньше ответил. Ответивший, который остался без служения (его даты
заняли другие), ставится на другую дату вместо не ответившего — с
интервалом от прошлого служения.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import date

LOAD_WEIGHT = 3
# Не ответивший — только резерв после ответивших.
RELUCTANT_WEIGHT = 1_000_000_000_000
# Желательный месячный максимум — мягкий: превышение дороже обычной
# нагрузки, но дешевле назначения не ответившего.
ABOVE_MAX_WEIGHT = 100_000
# Служение неделю подряд штрафуется, но ответивший остаётся приоритетнее
# не ответившего, который используется только как резерв.
BACK_TO_BACK_WEIGHT = 100_000_000
# Штраф за интервал между служениями человека, в неделях.
GAP_PENALTY = {1: BACK_TO_BACK_WEIGHT, 2: 2}
# Чередование, когда людей больше, чем мест: кто служил в прошлом месяце
# позже, тот в этом ставится позже — штраф за первое служение месяца
# ROTATION_WEEKS − (недель от прошлого служения до начала месяца), не
# меньше 0. Меньше шага нагрузки (2 × LOAD_WEIGHT): ровнее нагрузка
# главнее чередования.
ROTATION_WEEKS = 5
# Лимит служений в месяц (решение 12): максимум — желательный, превышение
# штрафуется; минимум — штраф за каждое недостающее служение.
BELOW_MIN_WEIGHT = 50
# Предел проходов улучшения: план маленький, улучшения быстро кончаются.
MAX_IMPROVE_ROUNDS = 50


@dataclass(frozen=True)
class PlanInput:
    dates: tuple[date, ...]
    # Задача → сколько людей нужно (только задачи, которые распределяются).
    needed: Mapping[str, int]
    # Человек → даты, когда он свободен.
    free: Mapping[str, frozenset[date]]
    # Человек → его задачи («Списки» ∪ привязки в боте).
    tasks: Mapping[str, frozenset[str]]
    # Человек → подтверждённые фактические дни; отсутствующая запись —
    # старый опрос или резервный участник без ответа.
    availability: Mapping[
        str, frozenset[date | tuple[date, str]]
    ] = field(default_factory=dict)
    # Явные исключения; связка не может назначить человека на них.
    unavailable: Mapping[
        str, frozenset[date | tuple[date, str]]
    ] = field(default_factory=dict)
    # Человек → последнее служение до месяца (из прошлого месяца).
    last_served: Mapping[str, date] = field(default_factory=dict)
    # Задача → сколько ещё людей сверх минимума можно поставить.
    extra: Mapping[str, int] = field(default_factory=dict)
    # Не ответившие на опрос: ставятся после ответивших.
    reluctant: frozenset[str] = frozenset()
    # Человек → (минимум, максимум) служений в месяц; максимум None — без
    # ограничения. Нет записи — без лимита.
    limits: Mapping[str, tuple[int, int | None]] = field(default_factory=dict)
    # Человек → задачи, которые он берёт в одну неделю (звук + спевка).
    bundles: Mapping[str, frozenset[str]] = field(default_factory=dict)
    dependencies: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    last_duty: Mapping[str, date] = field(default_factory=dict)
    # Человек → порядок ответа на опрос (0 — первый); нет записи — после.
    answer_order: Mapping[str, int] = field(default_factory=dict)
    # Фактический день задачи в неделе графика, с учётом личного дня.
    slot_dates: Mapping[tuple[date, str, str], date] = field(default_factory=dict)
    # Результаты дорогих проверок (вход неизменяем, значения стабильны);
    # replace() даёт новому входу пустой кэш.
    _cache: dict = field(
        default_factory=dict, init=False, repr=False, compare=False
    )


@dataclass
class Plan:
    # (дата, задача) → люди в порядке назначения.
    slots: dict[tuple[date, str], list[str]]
    needed: Mapping[str, int]
    slot_dates: Mapping[tuple[date, str, str], date] = field(default_factory=dict)

    def load(self) -> dict[str, int]:
        """Количество фактических дней служения у каждого человека."""
        dates: dict[str, set[date]] = {}
        for (event_date, task), people in self.slots.items():
            for person in people:
                service_date = self.slot_dates.get(
                    (event_date, task, person), event_date
                )
                dates.setdefault(person, set()).add(service_date)
        return {person: len(days) for person, days in dates.items()}

    def covered(self) -> int:
        return sum(len(people) for people in self.slots.values())

    def required(self) -> int:
        return sum(self.needed[task] for _, task in self.slots)

    def covered_required(self) -> int:
        """Закрытые обязательные места (минимум задачи)."""
        return sum(
            min(len(people), self.needed[task])
            for (_, task), people in self.slots.items()
        )

    def gaps(self) -> list[tuple[date, str, int]]:
        """Незакрытые места: (дата, задача, сколько не хватает)."""
        return [
            (event_date, task, self.needed[task] - len(people))
            for (event_date, task), people in sorted(self.slots.items())
            if len(people) < self.needed[task]
        ]


def _weeks(earlier: date, later: date) -> int:
    # Интервал — в неделях графика: даты месяца — воскресенья, прошлое
    # служение — фактический день. День до воскресенья своей недели (свой
    # день человека или задачи, вс −1…−6) даёт то же число недель.
    return (later - earlier).days // 7


def _gap_penalty(earlier: date | None, later: date) -> int:
    if earlier is None:
        return 0
    # Меньше недели (служение в начале месяца с прошлого) — как неделя.
    return GAP_PENALTY.get(max(1, _weeks(earlier, later)), 0)


def _limit(data: PlanInput, person: str) -> tuple[int, int | None]:
    return data.limits.get(person, (0, None))


def _rotation_penalty(data: PlanInput, person: str) -> int:
    """Штраф первого служения месяца: кто дольше не служил, тот первый."""
    previous = data.last_served.get(person)
    if previous is None or not data.dates:
        return 0
    weeks = max(0, _weeks(previous, min(data.dates)))
    return max(0, ROTATION_WEEKS - weeks)


def _priority(data: PlanInput) -> dict[str, int]:
    """Решение 16: кто получает дату при равной стоимости — меньше
    отмеченных дат, дольше не служил, раньше ответил (0 — первый)."""
    order = sorted(data.free, key=lambda person: (
        len(data.free[person]),
        data.last_served.get(person, date.min),
        data.answer_order.get(person, len(data.answer_order)),
        person,
    ))
    return {person: index for index, person in enumerate(order)}


def open_dates(data: PlanInput) -> list[date]:
    """Недели, где ответившие не закрывают минимум задач по дням задач."""
    required, _ = _seats(data)
    result = []
    for event_date in sorted(set(data.dates)):
        people = [
            person for person in sorted(data.free)
            if person not in data.reluctant
            and event_date in data.free[person]
        ]
        possible_days = sorted({
            data.slot_dates.get((event_date, task, person), event_date)
            for person in people for task in set(required)
            if _can_serve(data, person, event_date, task)
        })
        tokens, token_info = [], {}
        for person in people:
            for service_day in possible_days:
                token = f"{person}{BUNDLE_SEPARATOR}{service_day.isoformat()}"
                allowed_tasks = {
                    index for index, task in enumerate(required)
                    if _can_serve(data, person, event_date, task)
                    and data.slot_dates.get(
                        (event_date, task, person), event_date
                    ) == service_day
                }
                if allowed_tasks:
                    tokens.append(token)
                    token_info[token] = allowed_tasks
        allowed = {token: token_info[token] for token in tokens}
        matching = _min_cost_matching(
            tokens, required, allowed, dict.fromkeys(tokens, 0)
        )
        if len(matching) < len(required):
            result.append(event_date)
    return result


def plan_cost(plan: Plan, data: PlanInput) -> int:
    served: dict[str, list[date]] = {}
    anchors: dict[str, set[date]] = {}
    for (event_date, task), people in plan.slots.items():
        for person in people:
            served.setdefault(person, []).append(
                plan.slot_dates.get((event_date, task, person), event_date)
            )
            anchors.setdefault(person, set()).add(event_date)
    return sum(
        _person_cost(data, person, dates, anchors[person])
        for person, dates in served.items()
    )


def _available_on(data: PlanInput, person: str,
                  service_date: date, task: str) -> bool:
    unavailable = data.unavailable.get(person, ())
    if service_date in unavailable or (service_date, task) in unavailable:
        return False
    available = data.availability.get(person)
    return (
        available is None
        or service_date in available
        or (service_date, task) in available
    )


def _serves_directly(data: PlanInput, person: str, event_date: date,
                     task: str) -> bool:
    """Человек отметил именно этот день задачи (без запасной связки)."""
    if (
        event_date not in data.free.get(person, ())
        or task not in data.tasks.get(person, ())
    ):
        return False
    service_date = data.slot_dates.get((event_date, task, person), event_date)
    unavailable = data.unavailable.get(person, ())
    if service_date in unavailable or (service_date, task) in unavailable:
        return False
    return (
        data.availability.get(person) is None
        or _available_on(data, person, service_date, task)
    )


def _alternate_covers(data: PlanInput, person: str, event_date: date,
                      task: str) -> bool:
    """Закроет ли место другой ответивший, так что запасная связка
    `person` не нужна.

    Мало того, что такой человек есть: он может быть нужен на другом
    месте того же дня, где больше некому. Поэтому сравниваются максимальные
    паросочетания обязательных мест даты без запасного варианта и с ним —
    связка допускается, только если она увеличивает покрытие. Результат
    кэшируется во входе: `_can_serve` вызывается очень часто."""
    key = ("alternate", event_date, task, person)
    cached = data._cache.get(key)
    if cached is None:
        cached = data._cache[key] = _alternate_covers_uncached(
            data, person, event_date, task
        )
    return cached


def _alternate_covers_uncached(data: PlanInput, person: str,
                               event_date: date, task: str) -> bool:
    if not any(
        candidate != person
        and candidate in data.availability
        and _serves_directly(data, candidate, event_date, task)
        for candidate in data.free
    ):
        return False
    seats, _ = _seats(data)
    if task not in seats:
        return True

    def matched(with_fallback: bool) -> int:
        tokens: list[str] = []
        allowed: dict[str, set[int]] = {}
        for member in sorted(data.free):
            if member not in data.availability:
                continue
            by_day: dict[date, set[int]] = {}
            for index, seat in enumerate(seats):
                if _serves_directly(data, member, event_date, seat) or (
                    with_fallback and member == person
                    and seat == task
                    and event_date in data.free.get(member, ())
                ):
                    by_day.setdefault(data.slot_dates.get(
                        (event_date, seat, member), event_date
                    ), set()).add(index)
            for service_day, indexes in sorted(by_day.items()):
                token = f"{member}{BUNDLE_SEPARATOR}{service_day.isoformat()}"
                tokens.append(token)
                allowed[token] = indexes
        return len(_min_cost_matching(
            tokens, seats, allowed, dict.fromkeys(tokens, 0)
        ))

    # Без запасного варианта покрытие не зависит от человека — одно на дату.
    base_key = ("direct-matching", event_date)
    base = data._cache.get(base_key)
    if base is None:
        base = data._cache[base_key] = matched(False)
    # Все места уже закрыты — связка покрытие не увеличит.
    return base >= len(seats) or matched(True) <= base


def _can_serve(data: PlanInput, person: str, event_date: date, task: str) -> bool:
    if (
        event_date not in data.free.get(person, ())
        or task not in data.tasks.get(person, ())
    ):
        return False
    service_date = data.slot_dates.get((event_date, task, person), event_date)
    unavailable = data.unavailable.get(person, ())
    if service_date in unavailable or (service_date, task) in unavailable:
        return False
    available = data.availability.get(person)
    if available is None:
        return True
    if _available_on(data, person, service_date, task):
        return True
    bundle = data.bundles.get(person, frozenset())
    has_linked_date = any(
        linked_task != task
        and data.slot_dates.get(
            (event_date, linked_task, person), event_date
        ) > service_date
        and _available_on(
            data,
            person,
            data.slot_dates.get((event_date, linked_task, person), event_date),
            linked_task,
        )
        for linked_task in bundle
    )
    return (
        task in bundle and has_linked_date
        and not _alternate_covers(data, person, event_date, task)
    )


def _min_cost_matching(
    people: list[str],
    seats: list[str],
    allowed: Mapping[str, set[int]],
    cost: Mapping[str, int],
    seat_cost: Mapping[tuple[str, int], int] | None = None,
    person_groups: Mapping[str, str] | None = None,
    group_capacity: Mapping[str, int] | None = None,
) -> dict[int, str]:
    """Место → человек: максимальное паросочетание минимальной стоимости.

    Поток минимальной стоимости последовательными кратчайшими путями
    (Беллман — Форд): исток → группа → человек (стоимость человека) →
    место → сток. Каждый путь увеличивает поток на 1
    с наименьшим приростом стоимости, поэтому итог — минимальная
    стоимость среди максимальных паросочетаний. Размеры крошечные.
    seat_cost — доплата (человек, место) на ребре человек → место.
    """
    groups = {
        person: (person_groups or {}).get(person, person)
        for person in people
    }
    group_names = sorted(set(groups.values()))
    group_nodes = {
        group: index + 1 for index, group in enumerate(group_names)
    }
    person_start = 1 + len(group_names)
    person_nodes = {
        person: person_start + index for index, person in enumerate(people)
    }
    seat_start = person_start + len(people)
    source, sink = 0, seat_start + len(seats)
    # Ребро: [куда, остаток, стоимость, индекс обратного ребра].
    graph: list[list[list[int]]] = [[] for _ in range(sink + 1)]

    def add(start: int, end: int, price: int, capacity: int = 1) -> None:
        graph[start].append([end, capacity, price, len(graph[end])])
        graph[end].append([start, 0, -price, len(graph[start]) - 1])

    for group in group_names:
        capacity = (group_capacity or {}).get(group, len(people))
        add(source, group_nodes[group], 0, max(0, capacity))
    for person in people:
        index = person_nodes[person]
        add(group_nodes[groups[person]], index, cost[person])
        for seat in sorted(allowed.get(person, ())):
            add(
                index, seat_start + seat,
                (seat_cost or {}).get((person, seat), 0),
            )
    for seat in range(len(seats)):
        add(seat_start + seat, sink, 0)

    infinity = 1 << 60
    while True:
        distance = [infinity] * (sink + 1)
        came: list[tuple[int, int] | None] = [None] * (sink + 1)
        distance[source] = 0
        for _ in range(sink + 1):
            changed = False
            for node in range(sink + 1):
                if distance[node] == infinity:
                    continue
                for edge_index, (end, left, price, _) in enumerate(graph[node]):
                    if left and distance[node] + price < distance[end]:
                        distance[end] = distance[node] + price
                        came[end] = (node, edge_index)
                        changed = True
            if not changed:
                break
        if distance[sink] == infinity:
            break
        node = sink
        while node != source:
            previous, edge_index = came[node]
            edge = graph[previous][edge_index]
            edge[1] -= 1
            graph[node][edge[3]][1] += 1
            node = previous

    result: dict[int, str] = {}
    for person in people:
        for end, left, _, _ in graph[person_nodes[person]]:
            seat = end - seat_start
            if 0 <= seat < len(seats) and left == 0:
                result[seat] = person
    return result


def _seats(data: PlanInput) -> tuple[list[str], list[str]]:
    """Места на дату: обязательные (минимум задачи) и сверх минимума."""
    required = [
        task for task, count in data.needed.items()
        if task not in data.dependencies for _ in range(max(0, count))
    ]
    optional = [
        task for task, count in data.extra.items()
        if task in data.needed and task not in data.dependencies
        for _ in range(max(0, count))
    ]
    return required, optional


# Составное место связки задач во внутреннем плане: «звук\x1fспевка».
BUNDLE_SEPARATOR = "\x1f"


def build_plan(data: PlanInput) -> Plan:
    """Максимальное покрытие по фактическим дням, не по неделям."""
    if data.dependencies:
        if len(data.dependencies) != 1:
            raise ValueError("Поддерживается одна дополнительная обязанность на служение.")
        duty, anchors = next(iter(data.dependencies.items()))
        if (duty not in data.needed or data.needed[duty] != 1 or not anchors
                or duty in anchors or any(anchor not in data.needed for anchor in anchors)):
            raise ValueError("Обязанность требует одну основную роль и ровно одного ответственного.")
        plan = _build_plan(data)
        protected = {duty, *anchors}
        _prefer_bundles(plan, replace(data, bundles={
            person: bundle for person, bundle in data.bundles.items()
            if not bundle & protected
        }))
        return plan
    plan = _build_plan(data)
    _spread_soft_limits(plan, data)
    _prefer_bundles(plan, data)
    _spread_soft_limits(plan, data)
    improve_plan(plan, data)
    return plan


def _spread_soft_limits(plan: Plan, data: PlanInput) -> None:
    """Переносит назначение с превышающего лимит на человека ниже лимита."""
    for _ in range(sum(len(names) for names in plan.slots.values())):
        loads = plan.load()
        excess = sum(
            max(0, loads.get(person, 0) - high)
            for person in data.free
            if (high := _limit(data, person)[1]) is not None
        )
        if not excess:
            return
        move = None
        for (event_date, task), assigned in sorted(plan.slots.items()):
            for person in assigned:
                high = _limit(data, person)[1]
                if high is None or loads.get(person, 0) <= high:
                    continue
                for candidate in sorted(data.free):
                    if (
                        candidate == person
                        or (person not in data.reluctant
                            and candidate in data.reluctant)
                        or not _can_serve(data, candidate, event_date, task)
                    ):
                        continue
                    candidate_day = plan.slot_dates.get(
                        (event_date, task, candidate), event_date
                    )
                    if any(
                        candidate_day == plan.slot_dates.get(
                            (week, other_task, candidate), week
                        )
                        for (week, other_task), names in plan.slots.items()
                        if candidate in names
                    ):
                        continue
                    candidate_high = _limit(data, candidate)[1]
                    if (
                        candidate_high is not None
                        and loads.get(candidate, 0) >= candidate_high
                    ):
                        continue
                    move = ((event_date, task), person, candidate)
                    break
                if move:
                    break
            if move:
                break
        if not move:
            # Прямой замены нет — перестановка цепочкой по всему месяцу.
            if _rearrange_over_limit(plan, data, loads):
                continue
            return
        key, person, candidate = move
        assigned = plan.slots[key]
        assigned[assigned.index(person)] = candidate
        assigned.sort()


def _rearrange_over_limit(plan: Plan, data: PlanInput,
                          loads: Mapping[str, int]) -> bool:
    """Снимает превышение месячного максимума цепочкой замен.

    Прямая замена не работает, когда все, кто может взять место, уже на
    пределе. Тогда такой человек берёт это место, а своё отдаёт следующему,
    и так до того, у кого лимит ещё не исчерпан: каждый в цепочке остаётся с
    прежней нагрузкой, превышение у первого уходит. Цепочки ищутся в ширину
    (кратчайшие первыми, порядок детерминирован); применяется первая, после
    которой превышение в сумме меньше, покрытие то же, а у каждого
    затронутого человека — не больше одной задачи в день."""
    def excess(slots: dict[tuple[date, str], list[str]]) -> int:
        current = replace(plan, slots=slots).load()
        return sum(
            max(0, current.get(person, 0) - high)
            for person in data.free
            if (high := _limit(data, person)[1]) is not None
        )

    def one_task_per_day(slots, people) -> bool:
        for person in people:
            days = [
                plan.slot_dates.get((event_date, task, person), event_date)
                for (event_date, task), assigned in slots.items()
                if person in assigned
            ]
            if len(days) != len(set(days)):
                return False
        return True

    before = excess(plan.slots)
    for start_key, start_person in [
        (key, person)
        for key, assigned in sorted(plan.slots.items())
        for person in assigned
        if (high := _limit(data, person)[1]) is not None
        and loads.get(person, 0) > high
    ]:
        # Шаг цепочки: (место, кто уходит, кто приходит).
        queue = [([], start_key, start_person, {start_person})]
        seen = {start_key}
        for steps, key, leaver, used in queue:
            event_date, task = key
            for candidate in sorted(data.free):
                if (
                    candidate in used
                    or candidate in plan.slots[key]
                    or (leaver not in data.reluctant
                        and candidate in data.reluctant)
                    or not _can_serve(data, candidate, event_date, task)
                ):
                    continue
                chain = [*steps, (key, leaver, candidate)]
                high = _limit(data, candidate)[1]
                if high is None or loads.get(candidate, 0) < high:
                    slots = {
                        slot: list(assigned)
                        for slot, assigned in plan.slots.items()
                    }
                    for slot, out, came in chain:
                        slots[slot][slots[slot].index(out)] = came
                    if excess(slots) < before and one_task_per_day(
                        slots, {came for _, _, came in chain}
                    ):
                        for slot, assigned in slots.items():
                            plan.slots[slot] = sorted(assigned)
                        return True
                    continue
                # Кандидат на пределе: берёт это место, отдаёт своё другое.
                for other in sorted(plan.slots):
                    if (
                        other not in seen
                        and candidate in plan.slots[other]
                    ):
                        seen.add(other)
                        queue.append(
                            (chain, other, candidate, used | {candidate})
                        )
    return False


def _prefer_bundles(plan: Plan, data: PlanInput) -> None:
    """По возможности объединяет связанные задачи одного человека в неделе."""
    for event_date in sorted(set(data.dates)):
        for person, bundle in sorted(data.bundles.items()):
            linked_slots = [
                task for task in bundle
                if person in plan.slots.get((event_date, task), ())
            ]
            if not linked_slots:
                continue
            for task in sorted(bundle - set(linked_slots)):
                if not _can_serve(data, person, event_date, task):
                    continue
                service_day = plan.slot_dates.get(
                    (event_date, task, person), event_date
                )
                current_days = {
                    plan.slot_dates.get((week, current_task, person), week)
                    for (week, current_task), names in plan.slots.items()
                    if person in names
                }
                high = _limit(data, person)[1]
                if (
                    service_day in current_days
                    or (high is not None and len(current_days) >= high)
                ):
                    continue
                target = plan.slots.get((event_date, task), [])
                for other in target:
                    if person in data.reluctant and other not in data.reluctant:
                        continue
                    target[target.index(other)] = person
                    target.sort()
                    break


def _place_losers(plan: Plan, data: PlanInput) -> None:
    """Решение 16: ответивший, отметивший даты, но оставшийся без служения
    (его даты заняли другие), — на другую дату вместо не ответившего:
    туда, где это сильнее всего снижает стоимость плана (служение подряд у
    не ответившего), не подряд со своим прошлым служением, один раз.
    Связка «в одну неделю» не ответившего переходит целиком — к тому, у
    кого она тоже связана. Очередь — как при конкуренции за дату."""
    served: dict[str, set[date]] = {}
    held: dict[tuple[str, date], list[str]] = {}
    for (event_date, task), people in plan.slots.items():
        for person in people:
            served.setdefault(person, set()).add(event_date)
            held.setdefault((person, event_date), []).append(task)
    rank = _priority(data)
    losers = sorted(
        (
            person for person in data.free
            if person not in data.reluctant and data.free[person]
            and data.tasks.get(person) and not served.get(person)
        ),
        key=rank.__getitem__,
    )
    for person in losers:
        own = data.tasks[person]
        best = None
        for (other, event_date), tasks in sorted(held.items()):
            if other not in data.reluctant or not set(tasks) <= own or (
                len(tasks) > 1
                and not set(tasks) <= data.bundles.get(person, frozenset())
            ) or _gap_penalty(
                data.last_served.get(person), event_date
            ) >= BACK_TO_BACK_WEIGHT:
                continue
            dates = served[other]
            delta = (
                _person_cost(data, other, dates - {event_date})
                - _person_cost(data, other, dates)
                + _person_cost(data, person, {event_date})
            )
            if delta < 0 and (best is None or delta < best[0]):
                best = (delta, other, event_date)
        if best is None:
            continue
        _, other, event_date = best
        for task in held.pop((other, event_date)):
            people = plan.slots[(event_date, task)]
            people[people.index(other)] = person
            people.sort()
            held.setdefault((person, event_date), []).append(task)
        served[other].discard(event_date)
        served[person] = {event_date}


def _bundled_plan(data: PlanInput) -> Plan:
    """План месяца; связки задач «в одну неделю» — составными местами."""
    groups = sorted(
        {
            bundle for bundle in data.bundles.values()
            if len(bundle) > 1 and bundle <= set(data.needed)
        },
        key=lambda bundle: (
            -sum(value == bundle for value in data.bundles.values()),
            sorted(bundle),
        ),
    )
    if not groups:
        return _build_plan(data)
    needed = dict(data.needed)
    tasks = {person: set(items) for person, items in data.tasks.items()}
    virtual: dict[str, list[str]] = {}
    for bundle in groups:
        count = min(needed[task] for task in bundle)
        if count <= 0:
            continue
        members = [task for task in data.needed if task in bundle]
        name = BUNDLE_SEPARATOR.join(members)
        virtual[name] = members
        needed[name] = count
        for task in members:
            needed[task] -= count
        # Составное место может занять и человек без связки — тогда он
        # берёт одну задачу связки, остальные закрываются по отдельности:
        # иначе единственного со связкой ставили бы каждую неделю.
        for person, own in tasks.items():
            if own & bundle:
                own.add(name)
    inner = _build_plan(replace(
        data,
        needed={
            task: count for task, count in needed.items()
            if count > 0 or data.extra.get(task, 0) > 0
        },
        tasks={person: frozenset(items) for person, items in tasks.items()},
        # Места сверх минимума — после обязательных мест связок: иначе
        # лишнее место могло бы вынудить служить две недели подряд.
        extra={},
    ))
    plan = Plan(
        slots={
            (event_date, task): []
            for event_date in data.dates
            for task in data.needed
        },
        needed=data.needed,
        slot_dates=data.slot_dates,
    )
    for (event_date, task), people in inner.slots.items():
        if task not in virtual:
            plan.slots[(event_date, task)].extend(people)
    on_inner: dict[date, set[str]] = {}
    for (event_date, _), people in inner.slots.items():
        on_inner.setdefault(event_date, set()).update(people)
    for (event_date, task), people in sorted(inner.slots.items()):
        if task not in virtual:
            continue
        members = virtual[task]
        # Свободные в дату, кого во внутреннем плане нет (в пределах
        # максимума), — ими потом закрываются места связки по отдельности.
        spare = [
            person for person in data.free
            if event_date in data.free[person]
            and person not in on_inner.get(event_date, ())
        ]

        def unfillable(member: str) -> int:
            """Сколько мест задач связки в дату некем будет закрыть, если
            человек возьмёт member."""
            total = 0
            for other in members:
                left = (
                    data.needed[other] - len(plan.slots[(event_date, other)])
                    - (other == member)
                )
                able = sum(
                    other in data.tasks.get(candidate, ())
                    for candidate in spare
                )
                total += max(0, left - able)
            return total

        for person in people:
            if data.bundles.get(person) == frozenset(members):
                chosen = members
            else:
                # Без связки — одна задача: чтобы остальные места связки
                # было кем закрыть, затем где на эту дату меньше людей.
                chosen = [min(
                    (
                        member for member in members
                        if member in data.tasks.get(person, ())
                    ),
                    key=lambda member: (
                        unfillable(member),
                        len(plan.slots[(event_date, member)]),
                        members.index(member),
                    ),
                )]
            for member in chosen:
                plan.slots[(event_date, member)].append(person)
    _fill_bundle_gaps(plan, data)
    _fill_optional(plan, data)
    for people in plan.slots.values():
        people.sort()
    # Закрыть места важнее связки: если без связок закрывается больше
    # обязательных мест — план без них.
    plain = _build_plan(replace(data, bundles={}))
    if plain.covered_required() > plan.covered_required():
        return plain
    return plan


def _fill_bundle_gaps(plan: Plan, data: PlanInput) -> None:
    """Обязательные места связки, которые некем было закрыть вместе, —
    по отдельности. Сначала тот, кто в эту дату уже на задаче своей связки
    (ещё одна задача связки — не лишнее служение); затем свободный в дату,
    без служения в неё, в пределах максимума: без служения неделей раньше
    или позже (интервал важнее ответа на опрос), ответившие, без связки с
    этой задачей, с меньшей нагрузкой."""
    served: dict[str, set[date]] = {}
    on_date: dict[tuple[str, date], set[str]] = {}
    for (event_date, task), people in plan.slots.items():
        for person in people:
            served.setdefault(person, set()).add(event_date)
            on_date.setdefault((person, event_date), set()).add(task)
    queue = _priority(data)
    for event_date in sorted(set(data.dates)):
        for task, count in data.needed.items():
            people = plan.slots[(event_date, task)]
            while len(people) < count:
                def extends(person: str) -> bool:
                    current = on_date.get((person, event_date))
                    return bool(current) and current | {task} <= data.bundles.get(
                        person, frozenset()
                    )

                candidates = [
                    person for person in data.free
                    if _can_serve(data, person, event_date, task)
                    and person not in people
                    and (
                        extends(person)
                        or event_date not in served.get(person, set())
                    )
                ]
                if not candidates:
                    break

                def rank(person: str) -> tuple:
                    days = served.get(person, set())
                    previous = data.last_served.get(person)
                    close = any(
                        abs((other - event_date).days) <= 7 for other in days
                    ) or _gap_penalty(previous, event_date) >= BACK_TO_BACK_WEIGHT
                    # Связавший задачи — по отдельности в последнюю очередь.
                    bound = task in data.bundles.get(person, ())
                    return (
                        not extends(person), close, person in data.reluctant,
                        bound, len(days), queue[person],
                    )

                chosen = min(candidates, key=rank)
                people.append(chosen)
                served.setdefault(chosen, set()).add(event_date)
                on_date.setdefault((chosen, event_date), set()).add(task)


def _fill_optional(plan: Plan, data: PlanInput) -> None:
    """Места сверх минимума при связках — как второй проход основного
    алгоритма: на каждую дату все такие места вместе (паросочетание
    минимальной стоимости), только ответившие, без служения ни за неделю
    до, ни через неделю после, в пределах максимума; стоимость — прирост
    стоимости плана человека (недобравший минимум — дешевле)."""
    rank = _priority(data)
    served: dict[str, set[date]] = {}
    for (event_date, _), people in plan.slots.items():
        for person in people:
            served.setdefault(person, set()).add(event_date)
    for event_date in sorted(set(data.dates)):
        seats = [
            task
            for task, extra in data.extra.items() if task in data.needed
            for _ in range(max(
                0,
                data.needed[task] + extra
                - len(plan.slots[(event_date, task)]),
            ))
        ]
        if not seats:
            continue
        people = [
            person for person in sorted(data.free)
            if event_date in data.free[person]
            and person not in data.reluctant
            and not any(
                abs((other - event_date).days) <= 7
                for other in served.get(person, set())
            )
            and _gap_penalty(data.last_served.get(person), event_date)
            < BACK_TO_BACK_WEIGHT
        ]
        allowed = {
            person: {
                index for index, task in enumerate(seats)
                if _can_serve(data, person, event_date, task)
            }
            for person in people
        }
        # BELOW_MIN_WEIGHT — чтобы стоимость не была отрицательной.
        # Равная стоимость — по очереди конкуренции (младший разряд).
        scale = (len(rank) + 1) * (len(seats) + 1)
        cost = {
            person: (BELOW_MIN_WEIGHT + _person_cost(
                data, person, served.get(person, set()) | {event_date}
            ) - _person_cost(data, person, served.get(person, set())))
            * scale + rank[person]
            for person in people
        }
        loads = plan.load()
        capacities = {
            person: (
                len(people) if (high := _limit(data, person)[1]) is None
                else max(0, high - loads.get(person, 0))
            )
            for person in people
        }
        for seat, person in sorted(_min_cost_matching(
            people, seats, allowed, cost,
            person_groups={person: person for person in people},
            group_capacity=capacities,
        ).items()):
            plan.slots[(event_date, seats[seat])].append(person)
            served.setdefault(person, set()).add(event_date)


def _build_plan(data: PlanInput) -> Plan:
    required, optional = _seats(data)
    plan = Plan(
        slots={
            (event_date, task): []
            for event_date in data.dates
            for task in data.needed
        },
        needed=data.needed,
        slot_dates=data.slot_dates,
    )
    load: dict[str, int] = {}
    last: dict[str, date] = dict(data.last_served)
    rank = _priority(data)
    used_days: dict[str, set[date]] = {}
    served_weeks: dict[str, set[date]] = {}
    duty_counts: dict[str, int] = {}
    duty_choices: dict[date, str] = {}

    def assign_week(event_date: date, seats: list[str], *, required_pass: bool):
        if not seats:
            return
        people = sorted(
            person for person in data.free
            if event_date in data.free[person]
            and (required_pass or person not in data.reluctant)
            and (
                required_pass or _limit(data, person)[1] is None
                or load.get(person, 0) < _limit(data, person)[1]
            )
            and (required_pass or not any(
                abs((week - event_date).days) <= 7
                for week in served_weeks.get(person, set())
            ))
            and (
                required_pass
                or _gap_penalty(data.last_served.get(person), event_date)
                < BACK_TO_BACK_WEIGHT
            )
        )
        possible_days = sorted({
            data.slot_dates.get((event_date, task, person), event_date)
            for person in people for task in set(seats)
            if _can_serve(data, person, event_date, task)
        })
        tokens: list[str] = []
        token_person: dict[str, str] = {}
        token_day: dict[str, date] = {}
        for person in people:
            for service_day in possible_days:
                if service_day in used_days.get(person, set()):
                    continue
                if not any(
                    _can_serve(data, person, event_date, task)
                    and data.slot_dates.get(
                        (event_date, task, person), event_date
                    ) == service_day
                    for task in set(seats)
                ):
                    continue
                token = f"{person}{BUNDLE_SEPARATOR}{service_day.isoformat()}"
                tokens.append(token)
                token_person[token] = person
                token_day[token] = service_day
        allowed = {
            token: {
                index for index, task in enumerate(seats)
                if _can_serve(
                    data, token_person[token], event_date, task
                ) and data.slot_dates.get(
                    (event_date, task, token_person[token]), event_date
                ) == token_day[token]
            }
            for token in tokens
        }
        scale = (len(rank) + 1) * (len(seats) + 1)
        costs = {}
        for token in tokens:
            person = token_person[token]
            count = load.get(person, 0)
            high = _limit(data, person)[1]
            low = _limit(data, person)[0]
            week_gap = (
                0 if last.get(person) == event_date
                else _gap_penalty(last.get(person), event_date)
            )
            price = LOAD_WEIGHT * (2 * count + 1) + week_gap
            if count == 0:
                price += _rotation_penalty(data, person)
            if high is not None and count >= high:
                price += ABOVE_MAX_WEIGHT
            if count < low:
                price -= BELOW_MIN_WEIGHT
            if person in data.reluctant:
                price += RELUCTANT_WEIGHT
            costs[token] = price * scale + rank[person]
        group_capacity = {}
        for token in tokens:
            person = token_person[token]
            high = _limit(data, person)[1]
            group_capacity[person] = (
                len(tokens) if high is None
                else max(0, high - load.get(person, 0))
            )
        matching = _min_cost_matching(
            tokens, seats, allowed, costs,
            person_groups={token: token_person[token] for token in tokens},
            group_capacity=group_capacity,
        ) if not required_pass else _min_cost_matching(
            tokens, seats, allowed, costs
        )
        if required_pass and data.dependencies:
            duty, anchors = next(iter(data.dependencies.items()))
            choices = []
            for token in tokens:
                person = token_person[token]
                if (
                    token_day[token] != event_date
                    or duty not in data.tasks.get(person, ())
                    or not _can_serve(data, person, event_date, duty)
                    or data.slot_dates.get((event_date, duty, person), event_date) != event_date
                ):
                    continue
                for seat in sorted(allowed[token]):
                    if seats[seat] not in anchors:
                        continue
                    remaining = [
                        other for other in tokens
                        if token_person[other] != person or token_day[other] != event_date
                    ]
                    candidate = _min_cost_matching(
                        remaining, seats,
                        {other: allowed[other] - {seat} for other in remaining},
                        costs,
                    )
                    candidate[seat] = token
                    score = (
                        -len(candidate),
                        sum(costs[value] // scale for value in candidate.values()),
                        duty_counts.get(person, 0),
                        data.last_duty.get(person, date.min),
                        sum(costs[value] for value in candidate.values()),
                        person, seats[seat],
                    )
                    choices.append((score, candidate, person))
            if choices:
                _, chosen, person = min(choices, key=lambda item: item[0])
                if len(chosen) >= len(matching):
                    matching = chosen
                    duty_choices[event_date] = person
                    duty_counts[person] = duty_counts.get(person, 0) + 1
        for seat, token in sorted(matching.items()):
            person = token_person[token]
            task = seats[seat]
            service_day = token_day[token]
            plan.slots[(event_date, task)].append(person)
            used_days.setdefault(person, set()).add(service_day)
            served_weeks.setdefault(person, set()).add(event_date)
            load[person] = load.get(person, 0) + 1
            last[person] = event_date

    # Обязательные места покрываются первыми; несколько задач одной недели
    # могут назначаться одному человеку, только если это разные фактические дни.
    for event_date in sorted(set(data.dates)):
        assign_week(event_date, required, required_pass=True)
    if plan.gaps() and not data.dependencies:
        repair_gaps(plan, data)
        used_days.clear()
        served_weeks.clear()
        last = dict(data.last_served)
        for (event_date, task), people in sorted(plan.slots.items()):
            for person in people:
                used_days.setdefault(person, set()).add(
                    plan.slot_dates.get((event_date, task, person), event_date)
                )
                served_weeks.setdefault(person, set()).add(event_date)
                last[person] = event_date
        load = plan.load()
    for event_date in sorted(set(data.dates)):
        optional_seats = [
            task for task, extra in data.extra.items()
            if task in data.needed and task not in data.dependencies
            for _ in range(max(
                0, data.needed[task] + extra
                - len(plan.slots[(event_date, task)])
            ))
        ]
        assign_week(event_date, optional_seats, required_pass=False)
    if data.dependencies:
        duty = next(iter(data.dependencies))
        for event_date, person in duty_choices.items():
            plan.slots[(event_date, duty)] = [person]
    for people in plan.slots.values():
        people.sort()
    return plan


def _person_cost(data: PlanInput, person: str, dates: Iterable[date],
                 event_dates: Iterable[date] | None = None) -> int:
    """Вклад человека в стоимость плана: нагрузка² и штрафы интервала."""
    ordered = sorted(set(dates))
    weeks = sorted(set(event_dates if event_dates is not None else dates))
    total = LOAD_WEIGHT * len(ordered) ** 2
    total += BELOW_MIN_WEIGHT * max(0, _limit(data, person)[0] - len(ordered))
    high = _limit(data, person)[1]
    if high is not None:
        total += ABOVE_MAX_WEIGHT * max(0, len(ordered) - high)
    if weeks:
        total += _rotation_penalty(data, person)
    if person in data.reluctant:
        total += RELUCTANT_WEIGHT * len(ordered)
    previous = data.last_served.get(person)
    for event_date in weeks:
        total += _gap_penalty(previous, event_date)
        previous = event_date
    return total


def repair_gaps(plan: Plan, data: PlanInput) -> None:
    """Дополняет обязательное покрытие перестановками по всему месяцу.

    Поток: исток → человек → (человек, фактический день) → место → сток.
    Сначала сохраняется месячный максимум (или уже имеющаяся нагрузка),
    затем мягкий максимум снимается, если иначе остаются пустые места.
    Увеличивающие пути меняют только назначения своей цепочки.
    Вызывается до заполнения необязательных мест.
    """
    source, sink = ("source",), ("sink",)
    capacity: dict[tuple, int] = {}
    edges: dict[tuple, list[tuple]] = {}

    def add(start: tuple, end: tuple, amount: int) -> None:
        if (start, end) not in capacity:
            edges.setdefault(start, []).append(end)
            edges.setdefault(end, []).append(start)
            capacity.setdefault((end, start), 0)
        capacity[(start, end)] = capacity.get((start, end), 0) + amount

    seats = sorted(plan.slots)
    total = sum(plan.needed[key[1]] for key in seats)
    loads = plan.load()
    for person in sorted(data.free):
        high = _limit(data, person)[1]
        add(
            source, ("person", person),
            total if high is None else max(high, loads.get(person, 0)),
        )
        days = set()
        for key in seats:
            if not _can_serve(data, person, key[0], key[1]):
                continue
            service_day = plan.slot_dates.get((*key, person), key[0])
            if service_day not in days:
                add(("person", person), ("day", person, service_day), 1)
                days.add(service_day)
            add(("day", person, service_day), ("seat", key), 1)
    for key in seats:
        add(("seat", key), sink, plan.needed[key[1]])
    # Текущий план — уже пущенный поток.
    for key in seats:
        for person in plan.slots[key]:
            service_day = plan.slot_dates.get((*key, person), key[0])
            for start, end in (
                (source, ("person", person)),
                (("person", person), ("day", person, service_day)),
                (("day", person, service_day), ("seat", key)),
                (("seat", key), sink),
            ):
                capacity[(start, end)] -= 1
                capacity[(end, start)] += 1
    for unlimited in (False, True):
        if unlimited:
            for person in sorted(data.free):
                add(source, ("person", person), total)
        while True:
            came: dict[tuple, tuple | None] = {source: None}
            queue = [source]
            for node in queue:
                for end in edges.get(node, ()):
                    if end not in came and capacity[(node, end)] > 0:
                        came[end] = node
                        queue.append(end)
            if sink not in came:
                break
            node = sink
            while came[node] is not None:
                start = came[node]
                capacity[(start, node)] -= 1
                capacity[(node, start)] += 1
                node = start
    # Назначение — поток по ребру (человек, дата) → место; уже стоявшие
    # остаются в прежнем порядке, новые — в конце.
    for key in seats:
        assigned = {
            node[1] for node in edges.get(("seat", key), ())
            if node[0] == "day" and capacity[(("seat", key), node)] > 0
        }
        plan.slots[key] = [
            person for person in plan.slots[key] if person in assigned
        ] + sorted(assigned - set(plan.slots[key]))


def improve_plan(plan: Plan, data: PlanInput) -> None:
    """Локальные улучшения: покрытие не меняется, стоимость падает.

    Ходы — замена человека и обмен назначениями двух людей. Собранные
    связки переносятся целиком; занятость и нагрузка — по фактическим
    дням. Превышение мягких лимитов не растёт. Ход оценивается по вкладу
    затронутых людей; план меняется лишь лучшим ходом.
    """
    served: dict[str, set[tuple[date, str]]] = {}
    for key, people in plan.slots.items():
        for person in people:
            served.setdefault(person, set()).add(key)
    people_all = sorted(data.free)
    eligible = {
        key: {
            person for person in people_all
            if _can_serve(data, person, key[0], key[1])
        }
        for key in plan.slots
    }
    costs = {}

    def actual_day(person, key):
        return plan.slot_dates.get((key[0], key[1], person), key[0])

    def cost_of(person, positions):
        cache_key = (person, frozenset(positions))
        if cache_key not in costs:
            costs[cache_key] = _person_cost(
                data, person,
                {actual_day(person, key) for key in positions},
                {key[0] for key in positions},
            )
        return costs[cache_key]

    def excess_of(person, positions):
        high = _limit(data, person)[1]
        if high is None:
            return 0
        return max(0, len({actual_day(person, key) for key in positions}) - high)

    def can_take(person, key, positions):
        if person not in eligible[key]:
            return False
        days = {actual_day(person, other) for other in positions}
        return actual_day(person, key) not in days

    def can_take_group(person, keys, positions):
        if len(keys) > 1 and not {key[1] for key in keys}.issubset(
            data.bundles.get(person, ())
        ):
            return False
        taken = set(positions)
        for key in sorted(keys):
            if not can_take(person, key, taken):
                return False
            taken.add(key)
        return True

    extra_slots = {
        key: len(assigned) - plan.needed[key[1]]
        for key, assigned in plan.slots.items()
        if len(assigned) > plan.needed[key[1]]
    }

    def extras_valid(changes):
        if not extra_slots:
            return True
        updated = {}
        for keys, leaver, candidate in changes:
            updated.setdefault(leaver, set(served.get(leaver, ())))
            updated.setdefault(candidate, set(served.get(candidate, ())))
            updated[leaver].difference_update(keys)
            updated[candidate].update(keys)
        for key, required_extras in extra_slots.items():
            assigned = set(plan.slots[key])
            for keys, leaver, candidate in changes:
                if key in keys:
                    assigned.remove(leaver)
                    assigned.add(candidate)
            qualified = 0
            for person in assigned:
                positions = updated.get(person, served.get(person, set()))
                high = _limit(data, person)[1]
                if (
                    person not in data.reluctant
                    and (high is None or len({
                        actual_day(person, other) for other in positions
                    }) <= high)
                    and not any(
                        other != key and abs((other[0] - key[0]).days) <= 7
                        for other in positions
                    )
                    and _gap_penalty(data.last_served.get(person), key[0])
                    < BACK_TO_BACK_WEIGHT
                ):
                    qualified += 1
            if qualified < required_extras:
                return False
        return True

    for _ in range(MAX_IMPROVE_ROUNDS):
        positions = []
        seen = set()
        for key in sorted(plan.slots):
            for person in plan.slots[key]:
                bundle = data.bundles.get(person, ())
                keys = frozenset(
                    other for other in served[person]
                    if other[0] == key[0] and other[1] in bundle
                ) if key[1] in bundle else frozenset({key})
                if (person, keys) not in seen:
                    seen.add((person, keys))
                    positions.append((keys, person))
        best_delta = 0
        best_move = None
        for keys, person in positions:
            mine = served[person]
            before_mine = cost_of(person, mine)
            after_mine = cost_of(person, mine - keys)
            for other in people_all:
                theirs = served.get(other, set())
                if (
                    other == person
                    or (person not in data.reluctant and other in data.reluctant)
                    or not can_take_group(other, keys, theirs)
                    or excess_of(person, mine - keys) + excess_of(other, theirs | keys)
                    > excess_of(person, mine) + excess_of(other, theirs)
                ):
                    continue
                delta = (
                    after_mine - before_mine
                    + cost_of(other, theirs | keys) - cost_of(other, theirs)
                )
                if delta < best_delta and extras_valid([(keys, person, other)]):
                    best_delta = delta
                    best_move = [(keys, person, other)]
        for first, (keys_first, person_first) in enumerate(positions):
            for keys_second, person_second in positions[first + 1:]:
                if person_first == person_second or keys_first & keys_second:
                    continue
                first_positions = served[person_first]
                second_positions = served[person_second]
                if (
                    not can_take_group(
                        person_second, keys_first,
                        second_positions - keys_second,
                    )
                    or not can_take_group(
                        person_first, keys_second,
                        first_positions - keys_first,
                    )
                    or (
                        excess_of(person_first, (first_positions - keys_first) | keys_second)
                        + excess_of(person_second, (second_positions - keys_second) | keys_first)
                        > excess_of(person_first, first_positions)
                        + excess_of(person_second, second_positions)
                    )
                ):
                    continue
                delta = (
                    cost_of(person_first, (first_positions - keys_first) | keys_second)
                    + cost_of(person_second, (second_positions - keys_second) | keys_first)
                    - cost_of(person_first, first_positions)
                    - cost_of(person_second, second_positions)
                )
                if delta < best_delta and extras_valid([
                    (keys_first, person_first, person_second),
                    (keys_second, person_second, person_first),
                ]):
                    best_delta = delta
                    best_move = [
                        (keys_first, person_first, person_second),
                        (keys_second, person_second, person_first),
                    ]
        if best_move is None:
            break
        for keys, leaver, candidate in best_move:
            for key in keys:
                assigned = plan.slots[key]
                assigned[assigned.index(leaver)] = candidate
            served[leaver].difference_update(keys)
            served.setdefault(candidate, set()).update(keys)
    for assigned in plan.slots.values():
        assigned.sort()
