"""State-классы оркестратора.

Выделены из ``orchx.orchestrator.core`` (P0.1).
Эти классы — фактически runtime-state pre/post одного прогона роя.

Public API (стабильный):

* :class:`OrchXConfig` — CLI-управляемые knobs.
* :class:`OrchXContext` — глобальный контекст прогона.
* :class:`TaskState`, :class:`PhaseState`, :class:`AttemptInfo` —
  состояние задач/фаз/попыток.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .. import acceptance, runner
from ..models import PhaseSpec, Plan, TaskResult, TaskSpec

if TYPE_CHECKING:
    from ..agent.llm import LLMClient


# ---------------------------------------------------------------------------
# Config (CLI-managed knobs)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OrchXConfig:
    """Поведенческие настройки прогона. Управляются CLI-флагами и env."""

    auto_review: bool = True
    """Запустить ``orchX-reviewer`` после успешного прохода всех уровней."""
    auto_followup: bool = False
    """Динамически добавлять задачи из ``needs_followup`` в DAG."""
    max_followup_depth: int = 1
    """Максимальная глубина каскада followup'ов (анти-loop)."""
    use_debugger_on_retry: bool = True
    """На повторных попытках использовать ``orchX-debugger`` вместо оригинального агента."""
    use_merger_on_conflict: bool = True
    """При merge-конфликте спавнить ``orchX-merger`` для разрешения."""
    supervisor_enabled: bool = True
    """Запускать фоновый watchdog с heartbeat-логом и enforcement бюджета."""
    supervisor_interval_s: float = 30.0
    """Период heartbeat'а supervisor'а в секундах."""
    effort: str = "high"
    """Reasoning effort для воркеров (мапится в provider-specific параметр LLM).

    Best-practice для качества: ``high`` или ``xhigh`` (для самых сложных задач).
    ``low``/``medium`` — для скорости/стоимости в ущерб качеству.
    Per-task override доступен через ``TaskSpec.model``-аналогичное поле,
    но мы не выводим его в plan.json — единый effort на весь прогон.
    """
    reviewer_effort: str = "xhigh"
    """Усиленный effort для финального reviewer'а — recall важнее скорости."""
    debugger_effort: str = "high"
    """Effort для debugger'а. По умолчанию ``high``: ANALYSIS.md §5.1.G
    показал, что половина retry-кейсов — это «файл потерялся, переписать
    с нуля» (lost-edits, merge-conflict cleanup), где xhigh неоправдан.
    Поднимай до ``xhigh`` явно через ``--debugger-effort xhigh`` если
    задача действительно сложная (content-failure после нескольких retry'ев)."""
    merger_effort: str = "high"
    """Effort для merger'а — обычно достаточно high."""
    auto_replan: bool = True
    """Авто-вызов orchX-planner при провале фазы (если фаза ``allow_replan: true``
    и глобальный ``max_replans`` ещё не исчерпан). При False — оркестратор
    останавливается на провале и открывает PR с маркером ``[failed]``."""
    replanner_effort: str = "xhigh"
    """Effort для orchX-planner при перепланировании — переразбивка задачи требует глубины."""
    per_task_review: bool = False
    """Если True, после прохождения acceptance каждой задачи и перед
    git merge запускать lightweight reviewer (Angle A: line-by-line) на
    дифф этой задачи vs. integration-ветки. Любые blocking findings
    отправляют задачу на retry через debugger со списком findings'ов
    в качестве failure_context. Цель — поймать корректность-bugs до
    merge'а, когда дифф ещё мал и проще указать на причину."""
    per_task_review_effort: str = "medium"
    """Effort lightweight pre-merge reviewer'а — обычно достаточно ``medium``."""
    allow_dirty: bool = False
    """UNSAFE: пропустить проверку ``ensure_clean`` и стартовать рой даже на
    грязном workdir. Воркеры будут работать против committed-версии файлов;
    последующий merge может конфликтовать. Только для отладки."""
    auto_stash: bool = False
    """Если True, диспетчер сам сделает ``git stash push -m "pre-orchX <task_id>"``
    перед стартом и ``git stash pop`` после завершения роя. Удобно, когда у тебя
    есть локальные правки, которые жалко терять, но коммитить их рано."""
    cleanup_worktrees_after_merge: bool = False
    """P2.1: после успешного merge в integration ветку удалять worktree задачи.
    Экономит диск на больших прогонах, но усложняет debug — по умолчанию off."""
    pr_watcher_enabled: bool = False
    """P0.4: после opening PR запускать фоновый watcher (CI/review-comments).
    Включается флагом CLI ``--watch``."""
    auto_fixup_chain: bool = True
    """P1.8: после reviewer'а blocking findings автоматически конвертируются
    в follow-up debugger-задачи (через расширение DAG)."""
    max_cost_usd: float | None = None
    """P1.3: глобальный budget в долларах. ``None`` = без лимита.
    Supervisor abort'ит рой при превышении."""


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


@dataclass
class AttemptInfo:
    """Что произошло в одной попытке выполнения задачи."""

    attempt_num: int
    agent_used: (
        str  # короткое имя роли (implementer, debugger) или полное имя из старых логов
    )
    outcome: runner.WorkerOutcome | None = None
    acceptance_outcomes: list[acceptance.CheckOutcome] = field(default_factory=list)
    failure_reason: str = ""
    """Краткое объяснение, почему попытка не удалась (пусто = успешна)."""
    pre_merge_findings: list[dict[str, Any]] = field(default_factory=list)
    """Findings, которые pre-merge reviewer вернул для этой попытки.
    Каждый элемент — {file, line, severity, category, description, ...}.
    Используется debugger'ом на retry'е."""


@dataclass
class TaskState:
    """Состояние одной задачи в процессе исполнения роя."""

    spec: TaskSpec
    branch: str
    worktree_path: Path
    attempts: list[AttemptInfo] = field(default_factory=list)
    status: str = "pending"  # pending | running | success | failed | skipped
    result_path: Path | None = None
    """Путь к итоговому result.json (живёт внутри worktree задачи)."""
    last_result: TaskResult | None = None
    merge_sha: str | None = None
    notes: str = ""
    is_dynamic: bool = False
    """Задача добавлена через needs_followup, не была в исходном plan.json."""
    parent_task_id: str | None = None
    """Если задача порождена другой через followup — id родителя."""
    current_activity: str = ""
    """Последняя «полезная» строка из stdout/stderr воркера. Live-доска
    показывает её рядом с задачей, чтобы пользователь видел, что воркер
    действительно работает (Read/Glob/Grep/Write…)."""

    @property
    def attempt_count(self) -> int:
        """Сколько попыток уже было сделано."""
        return len(self.attempts)


@dataclass
class PhaseState:
    """Статус одной фазы в процессе исполнения."""

    spec: PhaseSpec
    status: str = "pending"  # pending | running | success | failed | skipped
    notes: str = ""
    started_at: float | None = None
    finished_at: float | None = None
    task_ids: list[str] = field(default_factory=list)
    """ID задач этой фазы в текущей версии плана (могут меняться после replan)."""


@dataclass
class OrchXContext:
    """Глобальный контекст одного запуска роя."""

    repo_root: Path
    plan: Plan
    config: OrchXConfig
    run_dir: Path  # orchx/runs/<task_id>/
    worktrees_root: Path  # orchx/runs/<task_id>/worktrees/
    integration_branch: str
    integration_worktree: Path
    log_file: Path
    llm: LLMClient
    """Базовый LLM-клиент. Воркеры получают per-role клонов через ``llm.for_role()``."""
    task_template: str
    plan_path: Path | None = None
    """Путь к plan.json — нужен replanner'у для перезаписи."""
    states: dict[str, TaskState] = field(default_factory=dict)
    phase_states: dict[str, PhaseState] = field(default_factory=dict)
    """Состояние каждой фазы по id. Обновляется после каждого replan."""
    completed_phase_ids: list[str] = field(default_factory=list)
    """История завершённых фаз (для replan-контекста и summary)."""
    replan_count: int = 0
    """Сколько раз уже звали planner с момента старта роя."""
    replan_history: list[dict[str, Any]] = field(default_factory=list)
    """История replan'ов для summary: каждый элемент — что упало и что предложил planner."""
    total_retries: int = 0
    started_at: float = 0.0
    review_state: TaskState | None = None
    """Состояние reviewer-задачи, если включён auto_review."""
    aborted: bool = False
    """True если supervisor решил остановить рой по бюджету."""
    abort_reason: str = ""
    # P2.2: mid-phase replan signal от supervisor'а.
    mid_phase_replan_requested: bool = False
    """True если supervisor задетектил, что задача висит > N× timeout и
    нужно прервать фазу + позвать replanner. Orchestrator опрашивает
    этот флаг между ходами."""
    mid_phase_replan_reason: str = ""
    halt_reason: str = ""
    """Причина остановки роя из-за провалившейся фазы (replan недоступен).
    В отличие от ``abort_reason`` (=супервизорский abort), сюда попадает
    штатное завершение по факту провала фазы с allow_replan=false или
    исчерпанным max_replans."""
    auto_stashed: bool = False
    """True, если диспетчер сделал ``git stash push`` на старте (через
    ``--auto-stash``). В финале нужно сделать ``git stash pop``."""
    # P1.3: cost tracking
    total_cost_usd: float = 0.0
    """Накопленная стоимость прогона в USD (P1.3)."""
    cost_by_role: dict[str, float] = field(default_factory=dict)
    """Per-role stoimost (implementer, reviewer, debugger, planner, merger, ...)."""
    cost_by_task: dict[str, float] = field(default_factory=dict)
    """Per-task стоимость."""
    # P0.3: long-term memory plugin
    memory: Any | None = None
    """Опциональный :class:`orchx.plugins.contracts.MemoryPlugin` для
    долговременного контекста (planner / debugger)."""
    # P0.2: notifier plugin
    notifier: Any | None = None
    """Опциональный :class:`orchx.plugins.contracts.NotifierPlugin`
    для отправки event'ов (run_started, pr_opened, cost_alert и т.п.)."""
    # P0.2: runtime plugin (если None — fallback на runner.run_subprocess_agent)
    runtime: Any | None = None
    """Опциональный :class:`orchx.plugins.contracts.RuntimePlugin`. По
    умолчанию None ⇒ legacy-путь через ``orchx.runner``."""
    # 0.2.1: tracker plugin (issue tracker / GitHub Projects).
    tracker: Any | None = None
    """Опциональный :class:`orchx.plugins.contracts.TrackerPlugin` —
    issue tracker. Если задан, orchestrator зовёт ``update_status`` на
    важных событиях (run start, task done, run failed)."""
    # ANALYSIS.md §5.1.C: per-run кэш предзагруженных фрагментов кода
    # для task.md. Ключ ``(rel_path, start, end)`` (start/end могут быть
    # None — для целого файла), значение — _Excerpt.
    preloaded_excerpts_cache: dict = field(default_factory=dict)
