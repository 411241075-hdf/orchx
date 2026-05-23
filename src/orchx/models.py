"""Dataclass-модели плана и результатов роя.

Загружают и валидируют JSON по схемам из ``orchx/schemas/`` (внутри пакета).

План поддерживает две формы:

1. **FLAT** (legacy) — плоский список ``tasks`` с зависимостями. Подходит
   для простых задач, которые помещаются в один логический шаг (≤ 8 задач,
   1-2 уровня параллелизма).
2. **PHASED** — иерархическая структура ``phases`` → ``tasks``. Каждая фаза
   мержится в интеграционную ветку до старта следующей. Подходит для
   больших ТЗ, где есть явные этапы (миграции → перенос → API → UI).

Внутренне обе формы нормализуются в ``Plan`` с обязательным полем
``phases``: FLAT превращается в одну фазу ``main``. Дальше оркестратор
работает только с phased-представлением.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

VALID_AGENTS = frozenset(
    {"architect", "implementer", "tester", "reviewer", "debugger", "merger"}
)

# Агенты, которые нельзя планировать вручную — их вызывает диспетчер.
# Если planner всё равно вкладывает их в plan.json (LLM не идеален), мы
# их silently отбрасываем при загрузке плана.
DISPATCHER_MANAGED_AGENTS = frozenset({"reviewer", "debugger", "merger"})

VALID_ACCEPTANCE_TYPES = frozenset({"command", "file_exists", "file_contains"})

VALID_RESULT_STATUSES = frozenset({"success", "partial", "failed"})

# Жёсткий потолок wall-времени всего роя — 24 часа.
# Защищает от runaway-прогонов при ошибках планнера или зацикливании replan'а.
MAX_WALL_SECONDS_HARDCAP = 24 * 60 * 60  # 86400s

# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AcceptanceCheck:
    """Одна проверка успешности задачи."""

    type: Literal["command", "file_exists", "file_contains"]
    description: str
    command: str | None = None
    path: str | None = None
    pattern: str | None = None
    timeout_seconds: int = 300


@dataclass(frozen=True)
class GlobalBudget:
    """Лимиты на весь рой."""

    max_parallel: int = 6
    max_wall_seconds: int = 7200
    max_total_retries: int = 10
    max_replans: int = 3


@dataclass(frozen=True)
class TaskSpec:
    """Спецификация одной задачи в plan.json."""

    id: str
    agent: str
    depends_on: tuple[str, ...]
    goal: str
    file_scope: tuple[str, ...]
    acceptance: tuple[AcceptanceCheck, ...]
    inputs: tuple[str, ...] = ()
    outputs: tuple[str, ...] = ()
    max_retries: int = 1
    timeout_seconds: int = 1800
    model: str | None = None


@dataclass(frozen=True)
class PhaseSpec:
    """Одна фаза иерархического плана.

    Фазы выполняются строго последовательно (фаза N+1 стартует только
    после успешного завершения фазы N и merge всех её задач в интеграционную
    ветку). Внутри фазы задачи образуют свой mini-DAG и могут выполняться
    параллельно.
    """

    id: str
    goal: str
    tasks: tuple[TaskSpec, ...]
    depends_on: tuple[str, ...] = ()
    allow_replan: bool = True


@dataclass(frozen=True)
class Plan:
    """Полный план роя.

    Всегда содержит хотя бы одну фазу. FLAT-форма (просто ``tasks``)
    нормализуется в одну фазу ``main`` при загрузке.
    """

    task_id: str
    base_branch: str
    phases: tuple[PhaseSpec, ...]
    global_budget: GlobalBudget = field(default_factory=GlobalBudget)
    summary: str = ""
    spec_files: tuple[str, ...] = ()

    @property
    def tasks(self) -> tuple[TaskSpec, ...]:
        """Все задачи всех фаз — для legacy-кода, ожидающего плоский список."""
        return tuple(t for p in self.phases for t in p.tasks)

    @property
    def is_phased(self) -> bool:
        """Был ли план изначально многофазным (а не FLAT, обёрнутым в одну фазу)?"""
        return len(self.phases) > 1 or (
            len(self.phases) == 1 and self.phases[0].id != "main"
        )


def _parse_acceptance(raw: dict[str, Any]) -> AcceptanceCheck:
    """Распарсить и провалидировать одну acceptance-проверку."""
    typ = raw.get("type")
    if typ not in VALID_ACCEPTANCE_TYPES:
        raise ValueError(f"Unknown acceptance type: {typ!r}")
    description = raw.get("description") or _autodescribe(raw)
    if typ == "command":
        cmd = raw.get("command")
        if not isinstance(cmd, str) or not cmd.strip():
            raise ValueError("acceptance.command must be a non-empty string")
        return AcceptanceCheck(
            type="command",
            command=cmd,
            description=description,
            timeout_seconds=int(raw.get("timeout_seconds", 300)),
        )
    if typ == "file_exists":
        path = raw.get("path")
        if not isinstance(path, str) or not path.strip():
            raise ValueError("acceptance.path must be a non-empty string")
        return AcceptanceCheck(type="file_exists", path=path, description=description)
    # file_contains
    path = raw.get("path")
    pattern = raw.get("pattern")
    if not isinstance(path, str) or not path.strip():
        raise ValueError("acceptance.path must be a non-empty string")
    if not isinstance(pattern, str) or not pattern.strip():
        raise ValueError("acceptance.pattern must be a non-empty string")
    # raise on bad regex early
    re.compile(pattern)
    return AcceptanceCheck(
        type="file_contains", path=path, pattern=pattern, description=description
    )


def _autodescribe(raw: dict[str, Any]) -> str:
    """Сгенерировать читаемое описание acceptance-проверки, если не задано явно."""
    typ = raw["type"]
    if typ == "command":
        return f"command: {raw.get('command', '')}"
    if typ == "file_exists":
        return f"file exists: {raw.get('path', '')}"
    return f"file matches: {raw.get('path', '')} ~ {raw.get('pattern', '')}"


def _parse_task(raw: dict[str, Any]) -> TaskSpec:
    """Распарсить и провалидировать одну задачу."""
    task_id = raw.get("id")
    if not isinstance(task_id, str) or not SLUG_RE.match(task_id):
        raise ValueError(f"Invalid task id: {task_id!r}")
    agent = raw.get("agent")
    if agent not in VALID_AGENTS:
        raise ValueError(f"Task {task_id}: invalid agent {agent!r}")
    goal = raw.get("goal", "")
    if not isinstance(goal, str) or len(goal.strip()) < 10:
        raise ValueError(f"Task {task_id}: goal must be a non-empty sentence")
    file_scope = raw.get("file_scope") or []
    if not isinstance(file_scope, list) or not file_scope:
        raise ValueError(f"Task {task_id}: file_scope must be a non-empty list")
    acceptance_raw = raw.get("acceptance") or []
    if not isinstance(acceptance_raw, list) or not acceptance_raw:
        raise ValueError(f"Task {task_id}: acceptance must be a non-empty list")
    return TaskSpec(
        id=task_id,
        agent=agent,
        depends_on=tuple(raw.get("depends_on") or ()),
        goal=goal,
        inputs=tuple(raw.get("inputs") or ()),
        outputs=tuple(raw.get("outputs") or ()),
        file_scope=tuple(file_scope),
        acceptance=tuple(_parse_acceptance(a) for a in acceptance_raw),
        max_retries=int(raw.get("max_retries", 1)),
        timeout_seconds=int(raw.get("timeout_seconds", 1800)),
        model=raw.get("model"),
    )


def _parse_phase(raw: dict[str, Any], prev_phase_id: str | None) -> PhaseSpec:
    """Распарсить и провалидировать одну фазу.

    Если ``depends_on`` не задано — фаза автоматически зависит от непосредственно
    предыдущей фазы в массиве (обычное последовательное поведение).

    Задачи с агентами ``reviewer``/``debugger``/``merger`` silently отбрасываются:
    их вызывает диспетчер автоматически на финале и при retry'ях, и planner не
    должен их планировать вручную (даже если LLM иногда забывает это правило).
    Аналогично отбрасываются задачи с пустым ``file_scope`` — это всегда
    «плейсхолдеры» от planner'а, не реальная работа.
    """
    import logging as _lg
    _logger = _lg.getLogger(__name__)
    phase_id = raw.get("id")
    if not isinstance(phase_id, str) or not SLUG_RE.match(phase_id):
        raise ValueError(f"Invalid phase id: {phase_id!r}")
    goal = raw.get("goal", "")
    if not isinstance(goal, str) or len(goal.strip()) < 10:
        raise ValueError(f"Phase {phase_id}: goal must be a non-empty sentence")
    tasks_raw = raw.get("tasks") or []
    if not isinstance(tasks_raw, list) or not tasks_raw:
        raise ValueError(f"Phase {phase_id}: tasks must be a non-empty list")

    # Pre-filter: убираем dispatcher-managed агентов и задачи с пустым scope.
    filtered_raw: list[dict[str, Any]] = []
    for t in tasks_raw:
        if not isinstance(t, dict):
            continue
        agent = t.get("agent")
        tid = t.get("id", "<no-id>")
        if agent in DISPATCHER_MANAGED_AGENTS:
            _logger.warning(
                "plan: phase %r task %r uses dispatcher-managed agent %r — "
                "silently dropped (it is invoked automatically by the dispatcher "
                "on retry/finalize)",
                phase_id, tid, agent,
            )
            continue
        scope = t.get("file_scope")
        if not isinstance(scope, list) or not scope:
            _logger.warning(
                "plan: phase %r task %r has empty file_scope — silently dropped "
                "(use file_scope=['<file>'] for real edits, or move the task "
                "out of the plan if it has no concrete output)",
                phase_id, tid,
            )
            continue
        filtered_raw.append(t)

    if not filtered_raw:
        raise ValueError(
            f"Phase {phase_id}: no valid tasks after filtering "
            f"dispatcher-managed agents and empty-scope placeholders. "
            f"Original task count: {len(tasks_raw)}."
        )

    tasks = tuple(_parse_task(t) for t in filtered_raw)
    # Внутри фазы id уникальны и depends_on ссылается только на свои задачи.
    ids = [t.id for t in tasks]
    if len(set(ids)) != len(ids):
        raise ValueError(f"Phase {phase_id}: duplicate task ids")
    id_set = set(ids)
    # Также чистим depends_on от ссылок на отброшенные задачи (если такие были).
    dropped_ids = {
        t.get("id") for t in tasks_raw
        if isinstance(t, dict) and t.get("id") not in id_set
    }
    if dropped_ids:
        # Перестроим задачи с очищенными depends_on.
        cleaned: list[TaskSpec] = []
        for t in tasks:
            new_deps = tuple(d for d in t.depends_on if d not in dropped_ids)
            if new_deps != t.depends_on:
                _logger.warning(
                    "plan: phase %r task %r had depends_on referencing dropped "
                    "tasks %s — cleaned",
                    phase_id, t.id, dropped_ids & set(t.depends_on),
                )
                cleaned.append(
                    TaskSpec(
                        id=t.id,
                        agent=t.agent,
                        depends_on=new_deps,
                        goal=t.goal,
                        inputs=t.inputs,
                        outputs=t.outputs,
                        file_scope=t.file_scope,
                        acceptance=t.acceptance,
                        max_retries=t.max_retries,
                        timeout_seconds=t.timeout_seconds,
                        model=t.model,
                    )
                )
            else:
                cleaned.append(t)
        tasks = tuple(cleaned)
    for t in tasks:
        for dep in t.depends_on:
            if dep not in id_set:
                raise ValueError(
                    f"Phase {phase_id}, task {t.id}: "
                    f"depends_on {dep!r} not in this phase"
                )
    depends_raw = raw.get("depends_on")
    if depends_raw is None and prev_phase_id is not None:
        depends_on: tuple[str, ...] = (prev_phase_id,)
    elif depends_raw is None:
        depends_on = ()
    else:
        depends_on = tuple(depends_raw)
    return PhaseSpec(
        id=phase_id,
        goal=goal,
        tasks=tasks,
        depends_on=depends_on,
        allow_replan=bool(raw.get("allow_replan", True)),
    )


def load_plan(path: Path) -> Plan:
    """Загрузить и провалидировать plan.json.

    Поддерживает обе формы (FLAT и PHASED) — FLAT нормализуется в одну фазу
    ``main``, чтобы оркестратор работал только с phased-моделью.

    Args:
        path: Путь к ``plan.json``.

    Returns:
        Валидный объект ``Plan`` (всегда с непустым ``phases``).

    Raises:
        ValueError: Если план не соответствует схеме или содержит циклы.
    """
    if not path.exists():
        raise FileNotFoundError(f"Plan not found: {path}")
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("plan.json must be a JSON object")
    task_id = raw.get("task_id")
    if not isinstance(task_id, str) or not SLUG_RE.match(task_id):
        raise ValueError(f"Invalid plan task_id: {task_id!r}")
    base_branch = raw.get("base_branch")
    if not isinstance(base_branch, str) or not base_branch:
        raise ValueError("plan.base_branch must be a non-empty string")

    has_tasks = isinstance(raw.get("tasks"), list)
    has_phases = isinstance(raw.get("phases"), list)
    if has_tasks and has_phases:
        raise ValueError("plan must contain either 'tasks' or 'phases', not both")
    if not has_tasks and not has_phases:
        raise ValueError("plan must contain either 'tasks' or 'phases'")

    if has_tasks:
        tasks_raw = raw["tasks"]
        if not tasks_raw:
            raise ValueError("plan.tasks must be a non-empty list")
        # Оборачиваем плоский список в фиктивную фазу, чтобы переиспользовать
        # фильтрацию dispatcher-managed агентов и empty-scope задач.
        synthetic_phase = _parse_phase(
            {"id": "main", "goal": "Main phase (flat plan normalized)", "tasks": tasks_raw},
            prev_phase_id=None,
        )
        phases: tuple[PhaseSpec, ...] = (synthetic_phase,)
    else:
        phases_raw = raw["phases"]
        if not phases_raw:
            raise ValueError("plan.phases must be a non-empty list")
        phases_list: list[PhaseSpec] = []
        prev_id: str | None = None
        seen_phase_ids: set[str] = set()
        for p_raw in phases_raw:
            phase = _parse_phase(p_raw, prev_phase_id=prev_id)
            if phase.id in seen_phase_ids:
                raise ValueError(f"Duplicate phase id: {phase.id}")
            seen_phase_ids.add(phase.id)
            for dep in phase.depends_on:
                if dep not in seen_phase_ids:
                    raise ValueError(
                        f"Phase {phase.id}: depends_on {dep!r} unknown "
                        "(must reference earlier phase)"
                    )
            phases_list.append(phase)
            prev_id = phase.id
        # Глобальная уникальность task id across all phases.
        all_task_ids = [t.id for ph in phases_list for t in ph.tasks]
        if len(set(all_task_ids)) != len(all_task_ids):
            raise ValueError("Duplicate task ids across phases")
        phases = tuple(phases_list)

    budget_raw = raw.get("global_budget") or {}
    requested_wall = int(budget_raw.get("max_wall_seconds", 7200))
    if requested_wall > MAX_WALL_SECONDS_HARDCAP:
        raise ValueError(
            f"max_wall_seconds={requested_wall}s exceeds hardcap "
            f"{MAX_WALL_SECONDS_HARDCAP}s (24h)"
        )
    budget = GlobalBudget(
        max_parallel=int(budget_raw.get("max_parallel", 6)),
        max_wall_seconds=requested_wall,
        max_total_retries=int(budget_raw.get("max_total_retries", 10)),
        max_replans=int(budget_raw.get("max_replans", 3)),
    )
    spec_files_raw = raw.get("spec_files") or []
    if not isinstance(spec_files_raw, list) or not all(
        isinstance(s, str) for s in spec_files_raw
    ):
        raise ValueError("plan.spec_files must be a list of strings")
    return Plan(
        task_id=task_id,
        base_branch=base_branch,
        phases=phases,
        global_budget=budget,
        summary=str(raw.get("summary") or ""),
        spec_files=tuple(spec_files_raw),
    )


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FollowupSuggestion:
    """Подсказка worker'а: какую задачу добавить в DAG."""

    agent: str
    goal: str
    reason: str = ""


@dataclass(frozen=True)
class TaskResult:
    """Результат одной задачи, записанный worker'ом."""

    task_id: str
    status: Literal["success", "partial", "failed"]
    artifacts: tuple[str, ...]
    notes: str
    metrics: dict[str, Any] = field(default_factory=dict)
    needs_followup: tuple[FollowupSuggestion, ...] = ()


def load_result(path: Path) -> TaskResult:
    """Загрузить и провалидировать result.json одной задачи."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: result must be a JSON object")
    task_id = raw.get("task_id")
    status = raw.get("status")
    if not isinstance(task_id, str):
        raise ValueError(f"{path}: missing task_id")
    if status not in VALID_RESULT_STATUSES:
        raise ValueError(f"{path}: invalid status {status!r}")
    artifacts = raw.get("artifacts") or []
    if not isinstance(artifacts, list) or not all(isinstance(a, str) for a in artifacts):
        raise ValueError(f"{path}: artifacts must be a list of strings")
    notes = raw.get("notes") or ""
    followups_raw = raw.get("needs_followup") or []
    followups = tuple(
        FollowupSuggestion(
            agent=str(f.get("agent", "")),
            goal=str(f.get("goal", "")),
            reason=str(f.get("reason", "")),
        )
        for f in followups_raw
        if isinstance(f, dict)
    )
    return TaskResult(
        task_id=task_id,
        status=status,
        artifacts=tuple(artifacts),
        notes=str(notes),
        metrics=raw.get("metrics") or {},
        needs_followup=followups,
    )
