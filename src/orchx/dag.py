"""Топологическая сортировка DAG задач orchX.

Работает на уровне отдельной фазы. Между фазами зависимости управляются
оркестратором (фазы выполняются последовательно, mini-DAG только внутри фазы).
"""

from __future__ import annotations

from collections import defaultdict, deque

from .models import PhaseSpec, Plan, TaskSpec


class CycleError(ValueError):
    """В DAG обнаружен цикл."""


def topological_levels_for_tasks(tasks: list[TaskSpec]) -> list[list[TaskSpec]]:
    """Разбить список задач на параллельные уровни (Kahn's algorithm).

    Каждый уровень — список задач, которые можно запускать одновременно
    (все их зависимости разрешены задачами с предыдущих уровней).

    Args:
        tasks: Задачи одной фазы (id уникальны, depends_on внутри списка).

    Returns:
        Список уровней. Каждый уровень — список ``TaskSpec``.

    Raises:
        CycleError: Если в DAG есть цикл.
    """
    by_id: dict[str, TaskSpec] = {t.id: t for t in tasks}
    indegree: dict[str, int] = {t.id: len(t.depends_on) for t in tasks}
    dependents: dict[str, list[str]] = defaultdict(list)
    for t in tasks:
        for dep in t.depends_on:
            dependents[dep].append(t.id)

    queue: deque[str] = deque(tid for tid, deg in indegree.items() if deg == 0)
    levels: list[list[TaskSpec]] = []
    seen = 0
    while queue:
        level_size = len(queue)
        current_level: list[TaskSpec] = []
        for _ in range(level_size):
            tid = queue.popleft()
            current_level.append(by_id[tid])
            seen += 1
            for child in dependents[tid]:
                indegree[child] -= 1
                if indegree[child] == 0:
                    queue.append(child)
        # Сортировка для детерминизма прогона.
        current_level.sort(key=lambda t: t.id)
        levels.append(current_level)

    if seen != len(tasks):
        unresolved = [tid for tid, deg in indegree.items() if deg > 0]
        raise CycleError(f"Cycle in DAG, unresolved tasks: {unresolved}")
    return levels


def topological_levels(plan: Plan) -> list[list[TaskSpec]]:
    """Совместимость с legacy-кодом: вернуть плоские уровни всех задач плана.

    Для phased-плана это просто конкатенация уровней всех фаз. Оркестратор
    эту функцию больше не использует — он обходит фазы по отдельности.
    """
    out: list[list[TaskSpec]] = []
    for phase in plan.phases:
        out.extend(topological_levels_for_tasks(list(phase.tasks)))
    return out


def phase_levels(phase: PhaseSpec) -> list[list[TaskSpec]]:
    """Топологические уровни задач одной фазы."""
    return topological_levels_for_tasks(list(phase.tasks))
