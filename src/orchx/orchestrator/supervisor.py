"""Supervisor loop и budget-enforcement helpers (P0.1).

Выделено из ``orchx.orchestrator.core``. Supervisor — фоновая корутина
с heartbeat-логом и enforcement бюджета. Запускается одной задачей через
``asyncio.create_task(supervisor_loop(ctx))`` в основном orchestrator.
"""

from __future__ import annotations

import asyncio
import logging
import time

from .context import OrchXContext, TaskState
from .logging_utils import orchx_log

# P2.2: коэффициент превышения task timeout, при котором supervisor
# сигнализирует mid-phase replan.
_HANG_FACTOR = 2.0

# Минимум секунд, выше которых имеет смысл сигналить (anti-flap).
_HANG_MIN_S = 60.0

logger = logging.getLogger(__name__)


async def supervisor_loop(ctx: OrchXContext) -> None:
    """Фоновая корутина: heartbeat, прогресс-репорт, enforcement бюджета.

    Делает heartbeat-лог каждые ``ctx.config.supervisor_interval_s`` секунд.
    При превышении wall-budget или max_cost_usd — выставляет ``ctx.aborted``,
    оркестратор увидит и завершит run gracefully.
    """
    interval = max(1.0, ctx.config.supervisor_interval_s)
    while True:
        await asyncio.sleep(interval)
        if ctx.aborted:
            return
        elapsed = time.monotonic() - ctx.started_at
        budget = ctx.plan.global_budget.max_wall_seconds
        counts = {"success": 0, "failed": 0, "running": 0, "pending": 0, "skipped": 0}
        for s in ctx.states.values():
            counts[s.status] = counts.get(s.status, 0) + 1
        orchx_log(
            ctx,
            f"[supervisor] elapsed={elapsed:.0f}s/{budget}s "
            f"counts={counts} retries={ctx.total_retries}/"
            f"{ctx.plan.global_budget.max_total_retries} "
            f"cost=${ctx.total_cost_usd:.4f}"
            + (
                f"/${ctx.config.max_cost_usd:.2f}"
                if ctx.config.max_cost_usd
                else ""
            ),
        )
        if elapsed > budget:
            orchx_log(
                ctx,
                f"[supervisor] WALL TIMEOUT exceeded ({elapsed:.0f}s > {budget}s); "
                "aborting remaining tasks",
            )
            ctx.aborted = True
            ctx.abort_reason = f"wall timeout {elapsed:.0f}s > {budget}s"
            return
        # P1.3: cost budget enforcement.
        if (
            ctx.config.max_cost_usd is not None
            and ctx.total_cost_usd > ctx.config.max_cost_usd
        ):
            orchx_log(
                ctx,
                f"[supervisor] COST BUDGET exceeded "
                f"(${ctx.total_cost_usd:.4f} > ${ctx.config.max_cost_usd:.2f}); "
                "aborting remaining tasks",
            )
            ctx.aborted = True
            ctx.abort_reason = (
                f"cost budget ${ctx.total_cost_usd:.4f} > "
                f"${ctx.config.max_cost_usd:.2f}"
            )
            return

        # P2.2: mid-phase replan trigger.
        # Если какая-то running-задача висит > 2× своего timeout — flag.
        _check_hung_tasks(ctx)


def budget_exceeded(ctx: OrchXContext) -> bool:
    """Превышен ли глобальный wall-clock budget?"""
    return time.monotonic() - ctx.started_at > ctx.plan.global_budget.max_wall_seconds


def _check_hung_tasks(ctx: OrchXContext) -> None:
    """P2.2: Найти hung-задачи (висят > 2x timeout) и поднять mid-phase replan flag."""
    if ctx.mid_phase_replan_requested:
        return  # уже сигналили
    for state in ctx.states.values():
        if state.status != "running":
            continue
        if not state.attempts:
            continue
        last = state.attempts[-1]
        if not last.outcome:
            continue
        elapsed = last.outcome.duration_s
        limit = max(_HANG_MIN_S, state.spec.timeout_seconds * _HANG_FACTOR)
        if elapsed > limit:
            ctx.mid_phase_replan_requested = True
            ctx.mid_phase_replan_reason = (
                f"task {state.spec.id} hung: {elapsed:.0f}s > {limit:.0f}s "
                f"({_HANG_FACTOR}x of {state.spec.timeout_seconds}s)"
            )
            orchx_log(
                ctx,
                f"[supervisor] hung-task detected; requesting mid-phase replan: "
                f"{ctx.mid_phase_replan_reason}",
            )
            return


def all_failures_are_env(failed_tasks: list[TaskState]) -> bool:
    """Все провалившиеся задачи упали по категории ``env``?

    Используется replanner-логикой: если да — replan бесполезен, нужно
    остановиться и попросить пользователя починить окружение.

    Считаем categories из последнего attempt'а каждой failed-задачи.
    Если у задачи нет acceptance_outcomes (упал агент / timeout), считаем
    это НЕ env-failure (то есть replan имеет смысл).
    """
    if not failed_tasks:
        return False
    for state in failed_tasks:
        if not state.attempts:
            return False
        last = state.attempts[-1]
        if not last.acceptance_outcomes:
            return False
        failed_outcomes = [o for o in last.acceptance_outcomes if not o.passed]
        if not failed_outcomes:
            # Все checks прошли, но статус failed — что-то странное
            # (например, merge conflict). Лучше попробовать replan.
            return False
        if not all(o.category == "env" for o in failed_outcomes):
            return False
    return True


# Backwards-compat aliases для core.py.
_supervisor_loop = supervisor_loop
_budget_exceeded = budget_exceeded
_all_failures_are_env = all_failures_are_env

__all__ = [
    "supervisor_loop",
    "budget_exceeded",
    "all_failures_are_env",
    "_supervisor_loop",
    "_budget_exceeded",
    "_all_failures_are_env",
]
