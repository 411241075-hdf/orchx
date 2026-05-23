"""Главный оркестратор роя: phased обход, параллельный спавн, retry, merge, replan.

Поддерживает полный продакшн-цикл для задач любого размера:

* Иерархические планы: ``phases`` → mini-DAG задач внутри фазы. Фазы выполняются
  строго последовательно, между ними — merge commit в интеграционную ветку
  (=checkpoint, к которому можно откатиться). Внутри фазы задачи идут по
  топологическим уровням с параллелизмом.
* FLAT-планы (legacy) автоматически оборачиваются в одну фазу ``main``.
* Retry на упавшую задачу — первая попытка оригинальным агентом, последующие
  — через ``orchX-debugger`` с контекстом провала.
* Merge-конфликты автоматически эскалируются на ``orchX-merger`` в integration
  worktree.
* **Авто-replanning:** если фаза провалилась и debugger не помог, оркестратор
  вызывает ``orchX-planner`` повторно с контекстом провала, получает новый план
  и продолжает с него. Лимитировано ``global_budget.max_replans``.
* Авто-запуск ``orchX-reviewer`` после прохода всех фаз (если включено).
* Опциональное динамическое расширение DAG через ``needs_followup`` worker'ов.
* Supervisor — фоновая корутина с heartbeat-логированием и enforcement бюджета.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import textwrap
import time
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import acceptance, paths, replanner, runner, worktree
from .agent.llm import LLMClient, LLMConfig
from .dag import phase_levels
from .models import (
    AcceptanceCheck,
    PhaseSpec,
    Plan,
    TaskResult,
    TaskSpec,
    load_plan,
    load_result,
)

logger = logging.getLogger(__name__)


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
    debugger_effort: str = "xhigh"
    """Усиленный effort для debugger'а — диагностика требует глубокого рассуждения."""
    merger_effort: str = "high"
    """Effort для merger'а — обычно достаточно high."""
    auto_replan: bool = True
    """Авто-вызов orchX-planner при провале фазы (если фаза ``allow_replan: true``
    и глобальный ``max_replans`` ещё не исчерпан). При False — оркестратор
    останавливается на провале и открывает PR с маркером ``[failed]``."""
    replanner_effort: str = "xhigh"
    """Effort для orchX-planner при перепланировании — переразбивка задачи требует глубины."""
    allow_dirty: bool = False
    """UNSAFE: пропустить проверку ``ensure_clean`` и стартовать рой даже на
    грязном workdir. Воркеры будут работать против committed-версии файлов;
    последующий merge может конфликтовать. Только для отладки."""
    auto_stash: bool = False
    """Если True, диспетчер сам сделает ``git stash push -m "pre-orchX <task_id>"``
    перед стартом и ``git stash pop`` после завершения роя. Удобно, когда у тебя
    есть локальные правки, которые жалко терять, но коммитить их рано."""


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


@dataclass
class AttemptInfo:
    """Что произошло в одной попытке выполнения задачи."""

    attempt_num: int
    agent_used: str  # короткое имя роли (implementer, debugger) или полное имя из старых логов
    outcome: runner.WorkerOutcome | None = None
    acceptance_outcomes: list[acceptance.CheckOutcome] = field(default_factory=list)
    failure_reason: str = ""
    """Краткое объяснение, почему попытка не удалась (пусто = успешна)."""


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
    run_dir: Path  # .orchx/runs/<task_id>/
    worktrees_root: Path  # .orchx/runs/<task_id>/worktrees/
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
    halt_reason: str = ""
    """Причина остановки роя из-за провалившейся фазы (replan недоступен).
    В отличие от ``abort_reason`` (=супервизорский abort), сюда попадает
    штатное завершение по факту провала фазы с allow_replan=false или
    исчерпанным max_replans."""
    auto_stashed: bool = False
    """True, если диспетчер сделал ``git stash push`` на старте (через
    ``--auto-stash``). В финале нужно сделать ``git stash pop``."""


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def _orchX_log(ctx: OrchXContext, msg: str) -> None:
    """Append-only журнал роя."""
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n"
    with ctx.log_file.open("a", encoding="utf-8") as f:
        f.write(line)
    logger.info(msg)


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------


async def _initialize_context(
    repo_root: Path,
    plan_path: Path,
    config: OrchXConfig,
    on_init_progress=None,
) -> OrchXContext:
    """Подготовить рабочие пути, интеграционную ветку, integration-worktree.

    Args:
        repo_root: корень репозитория.
        plan_path: путь к plan.json.
        config: настройки прогона.
        on_init_progress: опциональный callback ``(stage: str) -> None``,
            вызывается на ключевых шагах подготовки (cleaning previous run,
            creating integration branch, adding worktree). CLI/TUI передаёт
            его, чтобы spinner показывал, что именно сейчас делается.
    """
    def _progress(stage: str) -> None:
        if on_init_progress is not None:
            try:
                on_init_progress(stage)
            except Exception:  # noqa: BLE001
                pass

    _progress("loading plan")
    plan = load_plan(plan_path)
    run_dir = paths.run_dir(repo_root, plan.task_id)
    worktrees_root = paths.worktrees_dir(repo_root, plan.task_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    worktrees_root.mkdir(parents=True, exist_ok=True)
    log_file = paths.orchx_log_path(repo_root, plan.task_id)
    if not log_file.exists():
        log_file.write_text("", encoding="utf-8")

    integration_branch = f"orchX/{plan.task_id}"
    integration_worktree = paths.integration_worktree_path(repo_root, plan.task_id)

    ctx = OrchXContext(
        repo_root=repo_root,
        plan=plan,
        config=config,
        run_dir=run_dir,
        worktrees_root=worktrees_root,
        integration_branch=integration_branch,
        integration_worktree=integration_worktree,
        log_file=log_file,
        llm=LLMClient(LLMConfig.from_env()),
        task_template=paths.task_template_path().read_text(encoding="utf-8"),
        plan_path=plan_path,
        started_at=time.monotonic(),
    )

    _orchX_log(
        ctx,
        f"orchX start task_id={plan.task_id} base={plan.base_branch} "
        f"phased={plan.is_phased} phases={len(plan.phases)} "
        f"total_tasks={len(plan.tasks)}",
    )
    _orchX_log(ctx, f"config: {config}")
    _orchX_log(ctx, f"llm: model={ctx.llm.model} base_url={ctx.llm.base_url}")

    _initialize_task_states_from_plan(ctx, plan)

    _progress("checking working tree is clean")
    if config.auto_stash:
        stash_label = f"pre-orchX {plan.task_id}"
        stashed = await worktree.auto_stash(repo_root, stash_label)
        if stashed:
            ctx.auto_stashed = True
            _orchX_log(ctx, f"auto-stash created: {stash_label}")
            _progress(f"auto-stashed dirty files as {stash_label!r}")
        # После stash workdir уже чистый — проверка пройдёт.
        await worktree.ensure_clean(repo_root)
    else:
        await worktree.ensure_clean(repo_root, allow_dirty=config.allow_dirty)
        if config.allow_dirty:
            _orchX_log(
                ctx,
                "WARNING: --allow-dirty active; workers will see committed "
                "version of files, not your working tree edits",
            )
    _progress("cleaning previous run artefacts")
    await _cleanup_previous_run(ctx)
    _progress(f"creating integration branch {integration_branch}")
    await worktree.create_integration_branch(
        repo_root, plan.base_branch, integration_branch
    )
    if not integration_worktree.exists():
        _progress("adding integration worktree")
        await worktree.add_integration_worktree(
            repo_root, integration_worktree, integration_branch
        )
        _orchX_log(ctx, f"integration worktree at {integration_worktree}")

    _progress("ready")
    return ctx


def _initialize_task_states_from_plan(ctx: OrchXContext, plan: Plan) -> None:
    """Создать TaskState и PhaseState для всех задач/фаз плана.

    Вызывается при первичной инициализации и после replan'а. При replan'е
    задачи могут быть новые — старые TaskState из ctx.states остаются
    (они уже success или skipped, их трогать нельзя).
    """
    for phase in plan.phases:
        if phase.id not in ctx.phase_states:
            ctx.phase_states[phase.id] = PhaseState(spec=phase)
        ps = ctx.phase_states[phase.id]
        ps.spec = phase  # на случай replan'а — освежим спеку
        ps.task_ids = [t.id for t in phase.tasks]
        for spec in phase.tasks:
            if spec.id in ctx.states:
                # Задача с тем же id уже была — оставляем существующий state.
                continue
            branch = f"orchX-tasks/{plan.task_id}/{spec.id}"
            wt = ctx.worktrees_root / spec.id
            ctx.states[spec.id] = TaskState(
                spec=spec, branch=branch, worktree_path=wt
            )


async def _cleanup_previous_run(ctx: OrchXContext) -> None:
    """Снести остатки предыдущего прогона того же task_id (worktrees + ветки).

    Делает рой идемпотентным: если предыдущий прогон упал на середине, новый
    не падает на «branch already exists» / «worktree already exists».
    """
    # Воркеры.
    for state in ctx.states.values():
        if state.worktree_path.exists():
            await worktree.remove_worktree(ctx.repo_root, state.worktree_path)
        await worktree.delete_branch(ctx.repo_root, state.branch)
    # Reviewer (если был).
    review_wt = ctx.worktrees_root / "_review"
    if review_wt.exists():
        await worktree.remove_worktree(ctx.repo_root, review_wt)
    await worktree.delete_branch(ctx.repo_root, f"orchX-review/{ctx.plan.task_id}")
    # Integration.
    if ctx.integration_worktree.exists():
        await worktree.remove_worktree(ctx.repo_root, ctx.integration_worktree)
    await worktree.delete_branch(ctx.repo_root, ctx.integration_branch)


# ---------------------------------------------------------------------------
# Worker execution
# ---------------------------------------------------------------------------


async def _prepare_worktree_for_task(ctx: OrchXContext, state: TaskState) -> None:
    """Создать (или пересоздать) worktree задачи от текущего состояния интеграционной ветки.

    Зависимости задачи к этому моменту уже смержены в интеграционную ветку,
    значит worktree получит их автоматически.
    """
    if state.worktree_path.exists():
        await worktree.remove_worktree(ctx.repo_root, state.worktree_path)
    await worktree.delete_branch(ctx.repo_root, state.branch)
    await worktree.add_worktree(
        repo_root=ctx.repo_root,
        worktree_path=state.worktree_path,
        branch=state.branch,
        base_ref=ctx.integration_branch,
    )


def _write_task_artifacts(
    ctx: OrchXContext, state: TaskState, *, debugger_context: str | None = None
) -> Path:
    """Записать в worktree task.md (вход для воркера) и подготовить место под result.

    Args:
        ctx: Контекст роя.
        state: Состояние задачи.
        debugger_context: Если заполнено, секция «Debugger context» добавится
            в конец task.md. Используется для retry через ``orchX-debugger``.

    Returns:
        Путь к task.md внутри worktree.
    """
    orchX_dir = state.worktree_path / ".orchx"
    orchX_dir.mkdir(parents=True, exist_ok=True)
    (orchX_dir / "results").mkdir(parents=True, exist_ok=True)
    result_path_rel = f".orchx/results/{state.spec.id}.json"
    task_md_content = runner.render_task_md(
        template=ctx.task_template,
        task=state.spec,
        branch=state.branch,
        result_path=result_path_rel,
    )
    if debugger_context:
        task_md_content += "\n\n## Debugger context\n\n" + debugger_context + "\n"
    task_md_path = orchX_dir / "task.md"
    task_md_path.write_text(task_md_content, encoding="utf-8")
    state.result_path = state.worktree_path / result_path_rel
    return task_md_path


def _build_worker_prompt() -> str:
    """Короткое user-сообщение для воркера — всё содержательное в task.md."""
    return (
        "Read .orchx/task.md carefully and execute it as an orchX worker. "
        "Write the result JSON to the path specified in the task file. "
        "Do not exceed the allowed file scope. Finish with a short 'done' line."
    )


# Регулярка для удаления ANSI escape-последовательностей (на случай если
# модель/прокси прислали их в text-дельтах).
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")

# Активность от in-process воркера приходит в двух видах:
#   1. ``→ tool <name>`` — старт tool-вызова (см. orchx.agent.worker).
#   2. ``<verb> <arg>`` от самих tool'ов (``read foo.py``, ``bash git status``).
# Текстовые дельты от LLM (assistant content) тоже падают сюда, но обычно
# это длинные «mind voice»-фразы — их тоже показываем (укорочённо), это
# приятный live-feedback.
_ACTIVITY_TOOL_VERBS = ("→ tool", "read ", "write ", "edit ", "glob ", "grep ", "codesearch ", "bash ", "todo")


def _extract_activity(line: str) -> str:
    """Сократить активность воркера для отображения на live-доске.

    Возвращает короткую строку (<=100 символов) или пустую строку, если
    дельта пустая. Под in-process воркером on_activity получает чистые
    text-дельты и tool-сигналы, парсить kilo-stderr не нужно.
    """
    cleaned = _ANSI_RE.sub("", line).strip()
    if not cleaned:
        return ""
    # Tool-events — приоритетнее всего.
    for prefix in _ACTIVITY_TOOL_VERBS:
        if cleaned.startswith(prefix):
            return cleaned[:100]
    # Остальное — text-дельта; обычно длинная, обрежем и не пускаем
    # многострочные блоки (они ломают layout).
    if "\n" in cleaned:
        cleaned = cleaned.split("\n", 1)[0]
    return cleaned[:100]


def _build_debugger_context(state: TaskState) -> str:
    """Сформировать структурированный контекст провала для orchX-debugger.

    Цель — дать агенту всё нужное для root-cause диагностики одним проходом
    задачи: что упало, как воспроизвести, что воркер думал, какие у него были
    приоритеты. Формат — Markdown с предсказуемыми заголовками, чтобы агенту
    было легко на них ссылаться.
    """
    last = state.attempts[-1] if state.attempts else None
    if last is None:
        return "No prior attempts on record."

    parts: list[str] = []
    parts.append(f"**Original agent:** `{state.spec.agent}`")
    parts.append(f"**Attempt #{last.attempt_num} verdict:** {last.failure_reason or '(unspecified)'}")
    if last.outcome:
        parts.append(
            f"**Wall time:** {last.outcome.duration_s:.1f}s, "
            f"**timeout:** {last.outcome.timed_out}, "
            f"**agent exit:** {last.outcome.returncode}"
        )

    # Acceptance с PASS/FAIL и конкретными командами.
    if last.acceptance_outcomes:
        parts.append("\n### Acceptance outcomes")
        for check, outcome in zip(
            state.spec.acceptance, last.acceptance_outcomes, strict=False
        ):
            verdict = "PASS" if outcome.passed else "FAIL"
            detail = textwrap.shorten(
                outcome.detail.replace("\n", " | "), width=400, placeholder="…"
            )
            line = f"- [{verdict}] {outcome.description}"
            if check.type == "command":
                line += f"\n    - `command:` `{check.command}`"
            elif check.type == "file_exists":
                line += f"\n    - `file_exists:` `{check.path}`"
            elif check.type == "file_contains":
                line += (
                    f"\n    - `file_contains:` `{check.path}` ~ `{check.pattern}`"
                )
            line += f"\n    - `result:` {detail}"
            parts.append(line)
    else:
        parts.append(
            "\n### Acceptance outcomes\n\n"
            "_(не дошли до acceptance — упали раньше; см. Failure reason)_"
        )

    # Reproduction: какие команды можно прогнать прямо сейчас.
    repro_cmds = [
        c.command for c in state.spec.acceptance if c.type == "command" and c.command
    ]
    if repro_cmds:
        parts.append("\n### Reproduce locally")
        parts.append(
            "Прогони эти команды в worktree, чтобы увидеть текущее состояние:"
        )
        for cmd in repro_cmds:
            parts.append(f"```bash\n{cmd}\n```")

    if state.last_result is not None:
        parts.append("\n### Worker self-report from previous attempt")
        parts.append(
            f"- **status:** `{state.last_result.status}`\n"
            f"- **artifacts:** {list(state.last_result.artifacts) or '_none_'}\n"
            f"- **notes:**\n\n"
            + (state.last_result.notes or "_(empty)_")
        )

    if last.outcome and last.outcome.stderr:
        snippet = textwrap.shorten(
            last.outcome.stderr, width=1500, placeholder="…[truncated]"
        )
        parts.append("\n### Last stderr (truncated)")
        parts.append(f"```\n{snippet}\n```")

    parts.append(
        "\n### Your job\n\n"
        "1. **Воспроизведи провал** — прогон команд из `Reproduce locally` или "
        "проверка `file_exists`/`file_contains` через `read`.\n"
        "2. **Поставь диагноз корневой причины** — не симптома (см. три "
        "diagnosis-angles в твоём system prompt).\n"
        "3. **Сделай минимальный фикс** в `file_scope`. Не ослабляй acceptance, "
        "не скипай тесты, не выходи за scope.\n"
        "4. **Прогон acceptance до прохождения**.\n"
        "5. **Запиши result.json** через `write` со `status: success` "
        "и `notes`, описывающими корневую причину и фикс.\n"
    )
    return "\n".join(parts)


def _agent_for_attempt(ctx: OrchXContext, state: TaskState) -> str:
    """Какую роль воркера использовать на текущей попытке.

    Первая попытка — оригинальный агент задачи. Последующие — debugger,
    если включён ``use_debugger_on_retry``. Возвращает короткое role-имя
    (``implementer``, ``debugger``, ...) — :mod:`orchx.agent.frontmatter`
    сам добавляет префикс ``orchX-`` при загрузке spec'а.
    """
    if state.attempt_count == 0 or not ctx.config.use_debugger_on_retry:
        return state.spec.agent
    return "debugger"


async def _run_one_attempt(ctx: OrchXContext, state: TaskState) -> AttemptInfo:
    """Один проход worker'а: подготовить worktree, спавнить in-process агента, прочитать result, прогнать acceptance."""
    attempt_num = state.attempt_count + 1
    state.status = "running"

    is_debugger_retry = (
        attempt_num > 1 and ctx.config.use_debugger_on_retry
    )
    agent_to_use = _agent_for_attempt(ctx, state)
    debug_ctx = _build_debugger_context(state) if is_debugger_retry else None

    _orchX_log(
        ctx,
        f"task {state.spec.id} attempt={attempt_num} agent={agent_to_use}"
        + (" (debugger retry)" if is_debugger_retry else ""),
    )

    info = AttemptInfo(attempt_num=attempt_num, agent_used=agent_to_use)
    state.attempts.append(info)

    await _prepare_worktree_for_task(ctx, state)
    _write_task_artifacts(ctx, state, debugger_context=debug_ctx)

    log_file = ctx.run_dir / "logs" / f"{state.spec.id}.attempt{attempt_num}.log"
    effort = (
        ctx.config.debugger_effort if is_debugger_retry else ctx.config.effort
    )

    def _on_activity(line: str) -> None:
        activity = _extract_activity(line)
        if activity:
            state.current_activity = activity

    outcome = await runner.run_worker(
        llm=ctx.llm,
        cwd=state.worktree_path,
        repo_root=ctx.repo_root,
        role=agent_to_use,
        prompt=_build_worker_prompt(),
        timeout_s=state.spec.timeout_seconds,
        log_file=log_file,
        effort=effort,
        on_activity=_on_activity,
    )
    info.outcome = outcome
    state.current_activity = ""

    if outcome.timed_out:
        info.failure_reason = f"timeout after {state.spec.timeout_seconds}s"
        state.status = "failed"
        state.notes = info.failure_reason
        _orchX_log(ctx, f"task {state.spec.id} TIMEOUT")
        return info
    if outcome.returncode != 0:
        info.failure_reason = f"agent exit={outcome.returncode}"
        state.status = "failed"
        state.notes = info.failure_reason
        _orchX_log(ctx, f"task {state.spec.id} agent exit={outcome.returncode}")
        return info

    # Worker должен был записать result.json.
    if not state.result_path or not state.result_path.exists():
        info.failure_reason = "worker did not write result.json"
        state.status = "failed"
        state.notes = info.failure_reason
        _orchX_log(ctx, f"task {state.spec.id} missing result.json")
        return info

    try:
        result = load_result(state.result_path)
    except (ValueError, json.JSONDecodeError) as e:
        info.failure_reason = f"invalid result.json: {e}"
        state.status = "failed"
        state.notes = info.failure_reason
        _orchX_log(ctx, f"task {state.spec.id} invalid result.json: {e}")
        return info
    state.last_result = result
    if result.status == "failed":
        info.failure_reason = f"worker reported status=failed: {result.notes}"
        state.status = "failed"
        state.notes = info.failure_reason
        _orchX_log(ctx, f"task {state.spec.id} worker reported failure")
        return info

    # Acceptance.
    info.acceptance_outcomes = await acceptance.run_all(
        state.spec.acceptance, state.worktree_path
    )
    if all(o.passed for o in info.acceptance_outcomes):
        state.status = "success"
        state.notes = result.notes
        _orchX_log(ctx, f"task {state.spec.id} SUCCESS (attempt {attempt_num})")
        return info

    failed = [o for o in info.acceptance_outcomes if not o.passed]
    info.failure_reason = "acceptance failed: " + "; ".join(
        o.description for o in failed
    )
    state.status = "failed"
    state.notes = info.failure_reason
    _orchX_log(ctx, f"task {state.spec.id} acceptance failed")
    return info


# ---------------------------------------------------------------------------
# Merge / conflict resolution
# ---------------------------------------------------------------------------


async def _commit_and_merge(ctx: OrchXContext, state: TaskState) -> bool:
    """Закоммитить worktree задачи и смержить ветку в интеграционную.

    При конфликте — спавнит ``orchX-merger`` (если включено), иначе abort+fail.

    Returns:
        True если merge прошёл (включая успешное разрешение конфликта).
    """
    sha = await worktree.commit_all(
        worktree_path=state.worktree_path,
        message=f"orchX({state.spec.id}): {state.spec.goal}",
        author_name="orchX",
        author_email="orchX@local",
    )
    if sha is None:
        _orchX_log(ctx, f"task {state.spec.id} produced no changes; skipping merge")
        state.merge_sha = None
        return True

    state.merge_sha = sha
    success, output = await worktree.merge_branch_into(
        integration_worktree=ctx.integration_worktree,
        source_branch=state.branch,
        no_ff=True,
    )
    if success:
        _orchX_log(ctx, f"task {state.spec.id} merged into {ctx.integration_branch}")
        return True

    _orchX_log(
        ctx,
        f"task {state.spec.id} MERGE CONFLICT into {ctx.integration_branch}:\n{output}",
    )
    if not ctx.config.use_merger_on_conflict:
        await _abort_merge(ctx)
        state.status = "failed"
        state.notes = "merge conflict (auto-resolution disabled)"
        return False

    resolved = await _resolve_merge_conflict(ctx, state, output)
    if resolved:
        _orchX_log(
            ctx,
            f"task {state.spec.id} merge conflict resolved by orchX-merger",
        )
        return True

    await _abort_merge(ctx)
    state.status = "failed"
    state.notes = "merge conflict — orchX-merger could not resolve"
    return False


async def _abort_merge(ctx: OrchXContext) -> None:
    """Отменить незавершённый merge в интеграционном worktree."""
    proc = await asyncio.create_subprocess_exec(
        "git",
        "merge",
        "--abort",
        cwd=str(ctx.integration_worktree),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await proc.communicate()


async def _resolve_merge_conflict(
    ctx: OrchXContext, state: TaskState, merge_output: str
) -> bool:
    """Запустить ``orchX-merger`` в integration worktree для разрешения конфликта.

    Merger работает прямо в integration worktree (где сейчас ``git merge`` оставил
    конфликтные маркеры). Его задача — разрешить конфликты, сделать ``git add``,
    но **не commit** (коммит сделаем мы после).

    Returns:
        True если merger успешно разрешил конфликты и acceptance merger'а пройдены.
    """
    # Список конфликтных файлов.
    conflict_files = await _git_unmerged_files(ctx.integration_worktree)
    if not conflict_files:
        return False

    merger_id = f"merger__{state.spec.id}"
    orchX_dir = ctx.integration_worktree / ".orchx"
    orchX_dir.mkdir(parents=True, exist_ok=True)
    (orchX_dir / "results").mkdir(parents=True, exist_ok=True)
    result_path_rel = f".orchx/results/{merger_id}.json"
    result_path = ctx.integration_worktree / result_path_rel

    task_md = _render_merger_task_md(
        ctx=ctx,
        state=state,
        merger_id=merger_id,
        result_path_rel=result_path_rel,
        conflict_files=conflict_files,
        merge_output=merge_output,
    )
    task_md_path = orchX_dir / "task.md"
    task_md_path.write_text(task_md, encoding="utf-8")
    if result_path.exists():
        result_path.unlink()

    log_file = (
        ctx.run_dir / "logs" / f"{state.spec.id}.merger.attempt{state.attempt_count}.log"
    )
    outcome = await runner.run_worker(
        llm=ctx.llm,
        cwd=ctx.integration_worktree,
        repo_root=ctx.repo_root,
        role="merger",
        prompt=_build_worker_prompt(),
        timeout_s=900,
        log_file=log_file,
        effort=ctx.config.merger_effort,
    )
    if outcome.timed_out or outcome.returncode != 0:
        _orchX_log(
            ctx,
            f"orchX-merger for {state.spec.id}: agent exit={outcome.returncode} timeout={outcome.timed_out}",
        )
        return False

    if not result_path.exists():
        _orchX_log(ctx, f"orchX-merger for {state.spec.id}: no result.json")
        return False
    try:
        merger_result = load_result(result_path)
    except (ValueError, json.JSONDecodeError) as e:
        _orchX_log(ctx, f"orchX-merger for {state.spec.id}: invalid result.json: {e}")
        return False
    if merger_result.status == "failed":
        _orchX_log(
            ctx,
            f"orchX-merger for {state.spec.id} reported failure: {merger_result.notes}",
        )
        return False

    # Если merger не смог сделать `git add` (kilo CLI скрывает bash tool в --auto
    # при глобальном `bash: ask`), сделаем сами для всех файлов из conflict_files
    # — но только если в них действительно нет конфликт-маркеров.
    bad_files = await _files_with_conflict_markers(
        ctx.integration_worktree, conflict_files
    )
    if bad_files:
        _orchX_log(
            ctx,
            f"orchX-merger for {state.spec.id}: conflict markers remain in {bad_files}",
        )
        return False
    await _git_add_files(ctx.integration_worktree, conflict_files)

    # Проверим, что нет оставшихся unmerged файлов.
    remaining = await _git_unmerged_files(ctx.integration_worktree)
    if remaining:
        _orchX_log(
            ctx,
            f"orchX-merger for {state.spec.id}: unresolved files {remaining}",
        )
        return False

    # Финализируем merge: commit без --no-edit (нам нужно сообщение).
    proc = await asyncio.create_subprocess_exec(
        "git",
        "-c",
        "user.name=orchX",
        "-c",
        "user.email=orchX@local",
        "commit",
        "--no-verify",
        "-m",
        f"orchX-merge({state.spec.id}) [resolved by orchX-merger]\n\n"
        f"Files: {', '.join(conflict_files)}",
        cwd=str(ctx.integration_worktree),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout_b, stderr_b = await proc.communicate()
    if proc.returncode != 0:
        _orchX_log(
            ctx,
            f"orchX-merger commit failed: {stdout_b.decode()} {stderr_b.decode()}",
        )
        return False
    return True


async def _git_unmerged_files(cwd: Path) -> list[str]:
    """Список файлов с merge-конфликтами в worktree."""
    proc = await asyncio.create_subprocess_exec(
        "git",
        "diff",
        "--name-only",
        "--diff-filter=U",
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout_b, _ = await proc.communicate()
    return [
        line.strip()
        for line in stdout_b.decode("utf-8", errors="replace").splitlines()
        if line.strip()
    ]


CONFLICT_MARKER_PREFIXES = ("<<<<<<<", "=======", ">>>>>>>")


async def _files_with_conflict_markers(
    cwd: Path, files: list[str]
) -> list[str]:
    """Файлы из ``files``, в которых ещё остались git conflict markers."""
    bad: list[str] = []
    for f in files:
        path = cwd / f
        if not path.is_file():
            # Удалённый файл — нет маркеров.
            continue
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            bad.append(f)
            continue
        for line in content.splitlines():
            if any(line.startswith(p) for p in CONFLICT_MARKER_PREFIXES):
                bad.append(f)
                break
    return bad


async def _git_add_files(cwd: Path, files: list[str]) -> None:
    """`git add` указанных файлов (или удаление, если файл стёрт)."""
    if not files:
        return
    proc = await asyncio.create_subprocess_exec(
        "git",
        "add",
        "--",
        *files,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await proc.communicate()


def _render_merger_task_md(
    *,
    ctx: OrchXContext,
    state: TaskState,
    merger_id: str,
    result_path_rel: str,
    conflict_files: list[str],
    merge_output: str,
) -> str:
    """Сгенерировать task.md для orchX-merger в integration worktree."""
    files_block = "\n".join(f"- `{f}`" for f in conflict_files)
    plan_summary = json.dumps(
        {
            "task": state.spec.id,
            "goal": state.spec.goal,
            "depends_on": list(state.spec.depends_on),
            "agent": state.spec.agent,
        },
        indent=2,
        ensure_ascii=False,
    )
    return f"""# Task {merger_id}

> Ты — orchX-merger. Разреши merge-конфликт между интеграционной веткой и веткой
> воркера. Работаешь прямо в этом worktree, файлы с конфликтами уже на месте.

## Goal

Разрешить merge-конфликт после неудачного `git merge {state.branch}` в ветку
`{ctx.integration_branch}`. Сохранить намерения обеих сторон по возможности.

## Failed merge output

```
{textwrap.shorten(merge_output, width=2000, placeholder="…[truncated]")}
```

## Conflicting files

{files_block}

## Original task being merged

```json
{plan_summary}
```

## Acceptance

- В каждом файле из списка выше нет конфликт-маркеров `<<<<<<<`, `=======`, `>>>>>>>`.
- `git diff --name-only --diff-filter=U` пуст.
- Все нужные файлы добавлены через `git add`.
- НЕ запускай `git commit` и `git merge --abort`. Это сделает диспетчер.

## Result file

Запиши `{result_path_rel}` по схеме result.schema.json:

```json
{{
  "task_id": "{merger_id}",
  "status": "success",
  "artifacts": ["список изменённых файлов"],
  "notes": "Какие конфликты были и какое решение принято для каждого файла",
  "metrics": {{}},
  "needs_followup": []
}}
```

Если конфликт неразрешим автоматически (противоречивые требования двух сторон),
напиши `status: failed` и опиши проблему в notes.

## Запрещено

- `git commit`, `git merge --abort`, `git reset`, `git push`, `git rebase`.
- Удалять/переименовывать ветки.
- Менять файлы вне списка conflicting files, кроме случаев, где это нужно
  для согласованности (например, обновить вызов после переименования).
"""


# ---------------------------------------------------------------------------
# Followup tasks (needs_followup → DAG extension)
# ---------------------------------------------------------------------------


def _spec_from_followup(
    parent: TaskSpec,
    followup_idx: int,
    followup,  # FollowupSuggestion
) -> TaskSpec:
    """Сконструировать TaskSpec из followup-предложения worker'а.

    Безопасные дефолты: scope = scope родителя, retries = 0,
    timeout как у родителя, acceptance — синтетический ``file_exists``
    для парного результирующего JSON-файла (минимально проверяемый).
    """
    new_id = f"{parent.id}__fu{followup_idx}"
    return TaskSpec(
        id=new_id,
        agent=followup.agent,
        depends_on=(parent.id,),
        goal=followup.goal,
        inputs=parent.outputs,
        outputs=(),
        file_scope=parent.file_scope,
        acceptance=(
            AcceptanceCheck(
                type="file_exists",
                description=f"followup {new_id} produced its result.json",
                path=f".orchx/results/{new_id}.json",
            ),
        ),
        max_retries=0,
        timeout_seconds=parent.timeout_seconds,
    )


async def _maybe_enqueue_followups(
    ctx: OrchXContext, state: TaskState, current_depth: int
) -> list[TaskState]:
    """Если включён auto_followup и worker предложил followup'ы — добавить их в DAG.

    Returns:
        Список новых TaskState, которые нужно прогнать на текущем уровне после
        родителя. Они выполняются последовательно после parent (depends_on=parent),
        но параллельно между собой через _run_level.
    """
    if not ctx.config.auto_followup:
        return []
    if current_depth >= ctx.config.max_followup_depth:
        return []
    if state.last_result is None or not state.last_result.needs_followup:
        return []

    new_states: list[TaskState] = []
    for idx, fu in enumerate(state.last_result.needs_followup, start=1):
        if fu.agent not in {
            "architect",
            "implementer",
            "tester",
            "reviewer",
            "debugger",
        }:
            _orchX_log(
                ctx,
                f"followup from {state.spec.id} skipped: unsupported agent {fu.agent!r}",
            )
            continue
        spec = _spec_from_followup(state.spec, idx, fu)
        if spec.id in ctx.states:
            _orchX_log(ctx, f"followup {spec.id} already exists, skipping")
            continue
        branch = f"orchX-tasks/{ctx.plan.task_id}/{spec.id}"
        wt = ctx.worktrees_root / spec.id
        new_state = TaskState(
            spec=spec,
            branch=branch,
            worktree_path=wt,
            is_dynamic=True,
            parent_task_id=state.spec.id,
        )
        ctx.states[spec.id] = new_state
        new_states.append(new_state)
        _orchX_log(
            ctx,
            f"followup enqueued: {spec.id} (agent={spec.agent}, parent={state.spec.id}, "
            f"reason={fu.reason!r})",
        )
    return new_states


# ---------------------------------------------------------------------------
# Per-task driver
# ---------------------------------------------------------------------------


async def _run_task_with_retries(ctx: OrchXContext, state: TaskState) -> None:
    """Прогнать задачу с retry до max_retries, с возможной эскалацией merge-конфликтов.

    Если зависимость failed — задача skipped.
    """
    if ctx.aborted:
        state.status = "skipped"
        state.notes = "orchX aborted by supervisor (budget exceeded)"
        return

    spec = state.spec

    # Skip if any dependency failed/skipped.
    for dep_id in spec.depends_on:
        dep = ctx.states.get(dep_id)
        if dep is None or dep.status != "success":
            state.status = "skipped"
            state.notes = (
                f"dependency {dep_id} status="
                f"{dep.status if dep else 'missing'}"
            )
            _orchX_log(ctx, f"task {spec.id} SKIPPED ({state.notes})")
            return

    while state.attempt_count <= spec.max_retries:
        if ctx.aborted:
            state.status = "skipped"
            state.notes = "aborted mid-flight"
            return
        await _run_one_attempt(ctx, state)
        if state.status == "success":
            merged = await _commit_and_merge(ctx, state)
            if merged:
                return
            # merge conflict → задача провалилась на этом проходе, ретраим.
        if state.attempt_count > spec.max_retries:
            break
        if ctx.total_retries >= ctx.plan.global_budget.max_total_retries:
            _orchX_log(
                ctx, f"global retry budget exhausted; not retrying {spec.id}"
            )
            break
        ctx.total_retries += 1
        _orchX_log(
            ctx,
            f"task {spec.id} retry "
            f"({state.attempt_count}/{spec.max_retries}, "
            f"global {ctx.total_retries}/{ctx.plan.global_budget.max_total_retries})",
        )

    if state.status != "success":
        _orchX_log(ctx, f"task {spec.id} FINAL STATUS=failed")


# ---------------------------------------------------------------------------
# Levels
# ---------------------------------------------------------------------------


async def _run_level(
    ctx: OrchXContext, level: list[TaskSpec], depth: int
) -> list[TaskState]:
    """Запустить все задачи одного уровня DAG параллельно (с лимитом).

    Returns:
        Список добавленных через followup задач, которые надо прогнать
        на следующей итерации.
    """
    sem = asyncio.Semaphore(ctx.plan.global_budget.max_parallel)
    new_followups: list[TaskState] = []
    new_followups_lock = asyncio.Lock()

    async def _bounded(state: TaskState) -> None:
        async with sem:
            await _run_task_with_retries(ctx, state)
            if state.status == "success":
                added = await _maybe_enqueue_followups(ctx, state, depth)
                if added:
                    async with new_followups_lock:
                        new_followups.extend(added)

    tasks = [_bounded(ctx.states[spec.id]) for spec in level]
    await asyncio.gather(*tasks)
    return new_followups


# ---------------------------------------------------------------------------
# Reviewer
# ---------------------------------------------------------------------------


async def _run_reviewer(ctx: OrchXContext) -> TaskState | None:
    """Запустить orchX-reviewer на интеграционной ветке.

    Reviewer работает в собственном worktree от integration_branch и пишет
    отчёт в result.json без правок кода.

    Returns:
        TaskState reviewer'а или None, если не запустился (например, нечего
        ревьюить).
    """
    # Проверим, что есть какой-то дифф для ревью.
    diff_check = await _git_diff_summary(
        ctx.integration_worktree, ctx.plan.base_branch
    )
    if not diff_check:
        _orchX_log(ctx, "auto-review skipped: no diff vs base_branch")
        return None

    review_branch = f"orchX-review/{ctx.plan.task_id}"
    review_wt = ctx.worktrees_root / "_review"
    if review_wt.exists():
        await worktree.remove_worktree(ctx.repo_root, review_wt)
    await worktree.delete_branch(ctx.repo_root, review_branch)
    await worktree.add_worktree(
        repo_root=ctx.repo_root,
        worktree_path=review_wt,
        branch=review_branch,
        base_ref=ctx.integration_branch,
    )

    review_spec = TaskSpec(
        id=f"review__{ctx.plan.task_id}",
        agent="reviewer",
        depends_on=(),
        goal=(
            f"Review the full integration diff between {ctx.plan.base_branch} "
            f"and {ctx.integration_branch}. Report issues; do NOT modify code."
        ),
        inputs=(".orchx/plan.json",),
        outputs=(),
        file_scope=(".orchx/results/**",),
        acceptance=(
            AcceptanceCheck(
                type="file_exists",
                description="reviewer wrote result.json",
                path=f".orchx/results/review__{ctx.plan.task_id}.json",
            ),
        ),
        max_retries=0,
        timeout_seconds=1200,
    )
    review_state = TaskState(
        spec=review_spec, branch=review_branch, worktree_path=review_wt
    )
    ctx.review_state = review_state
    _orchX_log(ctx, f"auto-review starting on {review_branch}")

    # Подготовить task.md и контекст ревью.
    orchX_dir = review_wt / ".orchx"
    orchX_dir.mkdir(parents=True, exist_ok=True)
    (orchX_dir / "results").mkdir(parents=True, exist_ok=True)
    result_path_rel = f".orchx/results/{review_spec.id}.json"
    review_state.result_path = review_wt / result_path_rel

    # Скопируем активный план и журнал прогона прямо в worktree reviewer'а,
    # чтобы он мог читать их по относительным путям от своего cwd. Сами
    # runtime-артефакты живут в `.orchx/runs/<task_id>/` корня репо, но из
    # worktree их не видно (это отдельный checkout).
    if ctx.plan_path is not None and ctx.plan_path.exists():
        (orchX_dir / "plan.json").write_text(
            ctx.plan_path.read_text(encoding="utf-8"), encoding="utf-8"
        )
    if ctx.log_file.exists():
        (orchX_dir / "orchX.log").write_text(
            ctx.log_file.read_text(encoding="utf-8"), encoding="utf-8"
        )

    diff_stat = await _git_diff_stat(review_wt, ctx.plan.base_branch)
    task_md = _render_reviewer_task_md(
        ctx=ctx, review_spec=review_spec, result_path_rel=result_path_rel,
        diff_stat=diff_stat,
    )
    (orchX_dir / "task.md").write_text(task_md, encoding="utf-8")

    log_file = ctx.run_dir / "logs" / f"{review_spec.id}.log"
    info = AttemptInfo(attempt_num=1, agent_used="orchX-reviewer")
    review_state.attempts.append(info)
    outcome = await runner.run_worker(
        llm=ctx.llm,
        cwd=review_wt,
        repo_root=ctx.repo_root,
        role="reviewer",
        prompt=_build_worker_prompt(),
        timeout_s=review_spec.timeout_seconds,
        log_file=log_file,
        effort=ctx.config.reviewer_effort,
    )
    info.outcome = outcome

    if outcome.timed_out or outcome.returncode != 0:
        review_state.status = "failed"
        review_state.notes = (
            f"reviewer agent exit={outcome.returncode} timeout={outcome.timed_out}"
        )
        _orchX_log(ctx, review_state.notes)
        return review_state

    if not review_state.result_path.exists():
        review_state.status = "failed"
        review_state.notes = "reviewer did not write result.json"
        _orchX_log(ctx, review_state.notes)
        return review_state

    try:
        result = load_result(review_state.result_path)
    except (ValueError, json.JSONDecodeError) as e:
        review_state.status = "failed"
        review_state.notes = f"reviewer wrote invalid result.json: {e}"
        _orchX_log(ctx, review_state.notes)
        return review_state

    review_state.last_result = result
    review_state.status = (
        "success"
        if result.status == "success"
        else ("failed" if result.status == "failed" else "success")
    )
    review_state.notes = result.notes
    _orchX_log(
        ctx,
        f"auto-review done: status={result.status} "
        f"followup_count={len(result.needs_followup)}",
    )
    return review_state


def _render_reviewer_task_md(
    *,
    ctx: OrchXContext,
    review_spec: TaskSpec,
    result_path_rel: str,
    diff_stat: str,
) -> str:
    """Шаблон task.md для финального reviewer'а."""
    return f"""# Task {review_spec.id}

> Ты — orchX-reviewer. Финальный просмотр интеграционного диффа после
> успешного прогона роя. КОДА НЕ ПРАВИШЬ — только пишешь отчёт.

## Goal

{review_spec.goal}

## Diff overview

```
{textwrap.shorten(diff_stat, width=4000, placeholder="…[truncated]")}
```

Полный дифф: `git diff {ctx.plan.base_branch}...HEAD`.

## Что проверять

- Соответствие диффа целям из `.orchx/plan.json`.
- Согласованность между задачами (контракты, имена, типы).
- Безопасность: утечки секретов, инъекции, path traversal, hard-coded credentials.
- Стилевые нарушения и нарушения `.kilo/INSTRUCTIONS.md`.
- Оставшиеся `TODO`/`FIXME`, требующие внимания.
- Ненужные изменения вне scope роя.

## Acceptance

- Записан `{result_path_rel}` по схеме result.schema.json.

## Result file

```json
{{
  "task_id": "{review_spec.id}",
  "status": "success | partial | failed",
  "artifacts": [],
  "notes": "Структурированный отчёт. Используй секции:\\n## Blocking issues\\n## Non-blocking issues\\n## Suggestions",
  "metrics": {{}},
  "needs_followup": [
    {{ "agent": "implementer|tester|debugger", "goal": "Что доделать", "reason": "Почему" }}
  ]
}}
```

Статус: `success` — всё чисто; `partial` — есть non-blocking замечания; `failed` —
есть блокирующие проблемы (например, секрет в коммите).

## Запрещено

- Любые правки кода. `edit` ограничен только `.orchx/results/**`.
- Запуск тестов, билдов, форматтеров — это уже сделали воркеры.
- Вызов Task tool / new_task.
"""


async def _git_diff_summary(cwd: Path, base: str) -> str:
    """Краткий вывод `git diff --shortstat base...HEAD`."""
    proc = await asyncio.create_subprocess_exec(
        "git",
        "diff",
        "--shortstat",
        f"{base}...HEAD",
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout_b, _ = await proc.communicate()
    return stdout_b.decode("utf-8", errors="replace").strip()


async def _git_diff_stat(cwd: Path, base: str) -> str:
    """Полный `git diff --stat base...HEAD`."""
    proc = await asyncio.create_subprocess_exec(
        "git",
        "diff",
        "--stat",
        f"{base}...HEAD",
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout_b, _ = await proc.communicate()
    return stdout_b.decode("utf-8", errors="replace").strip()


# ---------------------------------------------------------------------------
# Supervisor
# ---------------------------------------------------------------------------


async def _supervisor_loop(ctx: OrchXContext) -> None:
    """Фоновая корутина: heartbeat, прогресс-репорт, enforcement бюджета."""
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
        _orchX_log(
            ctx,
            f"[supervisor] elapsed={elapsed:.0f}s/{budget}s "
            f"counts={counts} retries={ctx.total_retries}/{ctx.plan.global_budget.max_total_retries}",
        )
        if elapsed > budget:
            _orchX_log(
                ctx,
                f"[supervisor] WALL TIMEOUT exceeded ({elapsed:.0f}s > {budget}s); "
                "aborting remaining tasks",
            )
            ctx.aborted = True
            ctx.abort_reason = f"wall timeout {elapsed:.0f}s > {budget}s"
            return


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


def _budget_exceeded(ctx: OrchXContext) -> bool:
    """Превышен ли глобальный wall-clock budget?"""
    return time.monotonic() - ctx.started_at > ctx.plan.global_budget.max_wall_seconds


async def _run_one_phase(ctx: OrchXContext, phase: PhaseSpec) -> bool:
    """Прогнать одну фазу: задачи по топологическим уровням с параллелизмом.

    Returns:
        True если все задачи фазы — success или skipped-без-провала (то есть
        фаза в целом успешна). False если хотя бы одна задача упала.
    """
    ps = ctx.phase_states[phase.id]
    ps.status = "running"
    ps.started_at = time.monotonic()

    levels = phase_levels(phase)
    _orchX_log(
        ctx,
        f"phase {phase.id} start: {len(levels)} levels, "
        f"sizes={[len(lv) for lv in levels]}, goal={phase.goal!r}",
    )

    for i, level in enumerate(levels):
        if ctx.aborted or _budget_exceeded(ctx):
            _orchX_log(
                ctx,
                f"phase {phase.id} aborted before level {i} "
                f"(reason: {ctx.abort_reason or 'budget exceeded'})",
            )
            ps.status = "failed"
            ps.notes = "aborted (budget/supervisor)"
            ps.finished_at = time.monotonic()
            return False
        _orchX_log(
            ctx,
            f"phase {phase.id} level {i}/{len(levels) - 1}: {[t.id for t in level]}",
        )
        new_followups = await _run_level(ctx, level, depth=0)
        depth = 1
        while new_followups and depth <= ctx.config.max_followup_depth:
            if ctx.aborted or _budget_exceeded(ctx):
                break
            _orchX_log(
                ctx,
                f"phase {phase.id} followup depth={depth}: "
                f"{[s.spec.id for s in new_followups]}",
            )
            next_specs = [s.spec for s in new_followups]
            new_followups = await _run_level(ctx, next_specs, depth=depth)
            depth += 1

    # Оценить итог фазы.
    failed_in_phase = [
        ctx.states[tid]
        for tid in ps.task_ids
        if ctx.states[tid].status == "failed"
    ]
    if failed_in_phase:
        ps.status = "failed"
        ps.notes = (
            f"{len(failed_in_phase)} tasks failed: "
            + ", ".join(s.spec.id for s in failed_in_phase)
        )
        ps.finished_at = time.monotonic()
        _orchX_log(ctx, f"phase {phase.id} FAILED: {ps.notes}")
        return False

    ps.status = "success"
    ps.notes = f"{len(ps.task_ids)} tasks ok"
    ps.finished_at = time.monotonic()
    _orchX_log(ctx, f"phase {phase.id} SUCCESS in {ps.finished_at - ps.started_at:.0f}s")
    ctx.completed_phase_ids.append(phase.id)
    return True


async def _attempt_replan(ctx: OrchXContext, failed_phase: PhaseSpec) -> bool:
    """Попытаться перепланировать остаток работы после провала фазы.

    Returns:
        True если replan удался и оркестратор может продолжать с новым планом.
        False если replan невозможен / запрещён / провалился — оркестратор
        должен остановиться.
    """
    if not ctx.config.auto_replan:
        _orchX_log(ctx, f"replan disabled by config; halting after {failed_phase.id}")
        return False
    if not failed_phase.allow_replan:
        _orchX_log(
            ctx,
            f"phase {failed_phase.id} has allow_replan=false; halting",
        )
        return False
    if ctx.replan_count >= ctx.plan.global_budget.max_replans:
        _orchX_log(
            ctx,
            f"max_replans ({ctx.plan.global_budget.max_replans}) reached; halting",
        )
        return False
    if _budget_exceeded(ctx) or ctx.aborted:
        _orchX_log(ctx, "wall budget exceeded before replan; halting")
        return False

    ctx.replan_count += 1
    failed_tasks = [
        ctx.states[tid]
        for tid in ctx.phase_states[failed_phase.id].task_ids
        if ctx.states[tid].status == "failed"
    ]
    failure_reasons = {s.spec.id: s.notes or "(unknown)" for s in failed_tasks}

    _orchX_log(
        ctx,
        f"REPLAN attempt {ctx.replan_count}/{ctx.plan.global_budget.max_replans}: "
        f"failed_phase={failed_phase.id} failed_tasks={[s.spec.id for s in failed_tasks]}",
    )

    if ctx.plan_path is None:
        _orchX_log(ctx, "replan impossible: plan_path is None")
        return False

    rc = replanner.ReplanContext(
        repo_root=ctx.repo_root,
        plan=ctx.plan,
        failed_phase_id=failed_phase.id,
        failed_task_ids=[s.spec.id for s in failed_tasks],
        failure_reasons=failure_reasons,
        completed_phase_ids=list(ctx.completed_phase_ids),
        replan_attempt=ctx.replan_count,
        max_replans=ctx.plan.global_budget.max_replans,
    )

    try:
        new_plan = await replanner.run_replan(
            repo_root=ctx.repo_root,
            llm=ctx.llm,
            context=rc,
            plan_path=ctx.plan_path,
            run_dir=ctx.run_dir,
            log_dir=ctx.run_dir / "logs",
            effort=ctx.config.replanner_effort,
        )
    except RuntimeError as e:
        _orchX_log(ctx, f"replan FAILED: {e}")
        ctx.replan_history.append(
            {
                "attempt": ctx.replan_count,
                "failed_phase": failed_phase.id,
                "failed_tasks": [s.spec.id for s in failed_tasks],
                "outcome": "planner_error",
                "error": str(e),
            }
        )
        return False

    # Применяем новый план: подменяем ctx.plan, переинициализируем task/phase states.
    ctx.replan_history.append(
        {
            "attempt": ctx.replan_count,
            "failed_phase": failed_phase.id,
            "failed_tasks": [s.spec.id for s in failed_tasks],
            "outcome": "applied",
            "new_phases": [p.id for p in new_plan.phases],
        }
    )
    _orchX_log(
        ctx,
        f"replan applied: new phases={[p.id for p in new_plan.phases]} "
        f"({sum(len(p.tasks) for p in new_plan.phases)} tasks)",
    )
    ctx.plan = new_plan
    _initialize_task_states_from_plan(ctx, new_plan)
    return True


async def run_orchX(
    repo_root: Path,
    plan_path: Path,
    config: OrchXConfig | None = None,
    on_ctx_ready=None,
    on_init_progress=None,
) -> dict[str, Any]:
    """Главная точка входа: прогнать рой целиком.

    Алгоритм:

    1. Загрузить план, инициализировать integration ветку и worktree.
    2. Для каждой фазы (в порядке плана):
       - Если все её задачи уже завершены (success/skipped) — пропустить
         (актуально после replan'а, когда часть задач сохраняется).
       - Прогнать фазу через ``_run_one_phase``.
       - Если фаза провалилась → попытка replan через ``_attempt_replan``.
         При успехе — повторно обойти фазы нового плана с того, что ещё не сделано.
       - Если replan невозможен/провалился — остановить рой.
    3. После последней фазы — auto-review.

    Args:
        repo_root: Корень репозитория.
        plan_path: Путь к plan.json.
        config: Поведенческая конфигурация. По умолчанию — продакшн-настройки.

    Returns:
        Сводка результатов (для печати/JSON-экспорта).
    """
    config = config or OrchXConfig()
    ctx = await _initialize_context(
        repo_root, plan_path, config, on_init_progress=on_init_progress
    )
    if on_ctx_ready is not None:
        # Сигнал внешнему наблюдателю (CLI/TUI), что контекст создан и можно
        # подписываться на изменения статусов задач/фаз.
        try:
            on_ctx_ready(ctx)
        except Exception:  # noqa: BLE001
            logger.warning("on_ctx_ready callback raised", exc_info=True)

    supervisor_task: asyncio.Task | None = None
    if config.supervisor_enabled:
        supervisor_task = asyncio.create_task(_supervisor_loop(ctx))

    halt_reason = ""
    try:
        # Главный цикл по фазам. После replan'а ctx.plan может смениться,
        # поэтому переобходим список фаз с начала (уже завершённые пропускаем).
        while True:
            if ctx.aborted or _budget_exceeded(ctx):
                _orchX_log(ctx, "GLOBAL TIMEOUT or abort; stopping main loop")
                ctx.aborted = True
                halt_reason = ctx.abort_reason or "wall budget exceeded"
                break
            # Найти первую незавершённую фазу.
            next_phase: PhaseSpec | None = None
            for phase in ctx.plan.phases:
                if phase.id in ctx.completed_phase_ids:
                    continue
                ps = ctx.phase_states.get(phase.id)
                if ps is not None and ps.status == "success":
                    if phase.id not in ctx.completed_phase_ids:
                        ctx.completed_phase_ids.append(phase.id)
                    continue
                next_phase = phase
                break
            if next_phase is None:
                _orchX_log(ctx, "all phases completed")
                break

            ok = await _run_one_phase(ctx, next_phase)
            if ok:
                continue

            # Phase failed → try replan.
            replanned = await _attempt_replan(ctx, next_phase)
            if not replanned:
                halt_reason = (
                    f"phase {next_phase.id!r} failed and replan unavailable "
                    f"(allow_replan={next_phase.allow_replan}, "
                    f"replans_used={ctx.replan_count}/{ctx.plan.global_budget.max_replans})"
                )
                _orchX_log(ctx, f"halting: {halt_reason}")
                break
            # После успешного replan продолжаем главный цикл — он возьмёт
            # первую невыполненную фазу из обновлённого ctx.plan.

        # Помечаем все pending-задачи и фазы как skipped с явной причиной —
        # иначе summary показывает их как «pending», что путает (рой не работает).
        if halt_reason:
            ctx.halt_reason = halt_reason
            for state in ctx.states.values():
                if state.status == "pending":
                    state.status = "skipped"
                    state.notes = state.notes or f"halted before run: {halt_reason}"
            for ps in ctx.phase_states.values():
                if ps.status == "pending":
                    ps.status = "skipped"
                    ps.notes = ps.notes or f"halted before run: {halt_reason}"

        # Auto-review на финале.
        if config.auto_review and not ctx.aborted:
            successful = [s for s in ctx.states.values() if s.status == "success"]
            if successful:
                await _run_reviewer(ctx)
            else:
                _orchX_log(ctx, "auto-review skipped: no successful tasks")
    finally:
        if supervisor_task is not None:
            supervisor_task.cancel()
            with suppress(asyncio.CancelledError):
                await supervisor_task
        # Восстановим грязные правки, если делали auto-stash на старте.
        if ctx.auto_stashed:
            _orchX_log(ctx, "popping auto-stash back into working tree")
            try:
                await worktree.stash_pop(ctx.repo_root)
            except Exception as e:  # noqa: BLE001
                _orchX_log(ctx, f"WARNING: stash pop failed: {e}")

    summary = _build_summary(ctx)
    summary_path = ctx.run_dir / "summary.json"
    summary_path.write_text(
        json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    _orchX_log(
        ctx,
        f"orchX done: success={summary['counts']['success']} "
        f"failed={summary['counts']['failed']} skipped={summary['counts']['skipped']}"
        + (
            f" review={summary['review']['status']}"
            if summary.get("review")
            else ""
        ),
    )
    return summary


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------


def _summarize_state(state: TaskState) -> dict[str, Any]:
    """Сериализовать TaskState в dict для summary.json."""
    last = state.attempts[-1] if state.attempts else None
    return {
        "id": state.spec.id,
        "agent": state.spec.agent,
        "status": state.status,
        "branch": state.branch,
        "attempts": state.attempt_count,
        "agents_used": [a.agent_used for a in state.attempts],
        "merge_sha": state.merge_sha,
        "notes": state.notes,
        "duration_s": last.outcome.duration_s if last and last.outcome else None,
        "is_dynamic": state.is_dynamic,
        "parent_task_id": state.parent_task_id,
    }


def _build_summary(ctx: OrchXContext) -> dict[str, Any]:
    """Сводка по результатам прогона."""
    counts = {"success": 0, "failed": 0, "skipped": 0, "pending": 0, "running": 0}
    tasks_summary: list[dict[str, Any]] = []
    for state in ctx.states.values():
        counts[state.status] = counts.get(state.status, 0) + 1
        tasks_summary.append(_summarize_state(state))

    phases_summary: list[dict[str, Any]] = []
    for phase in ctx.plan.phases:
        ps = ctx.phase_states.get(phase.id)
        if ps is None:
            continue
        duration = (
            round(ps.finished_at - ps.started_at, 1)
            if ps.finished_at and ps.started_at
            else None
        )
        phases_summary.append(
            {
                "id": phase.id,
                "goal": phase.goal,
                "status": ps.status,
                "notes": ps.notes,
                "task_count": len(ps.task_ids),
                "task_ids": list(ps.task_ids),
                "duration_s": duration,
                "allow_replan": phase.allow_replan,
            }
        )

    out: dict[str, Any] = {
        "task_id": ctx.plan.task_id,
        "base_branch": ctx.plan.base_branch,
        "integration_branch": ctx.integration_branch,
        "integration_worktree": str(ctx.integration_worktree),
        "summary": ctx.plan.summary,
        "spec_files": list(ctx.plan.spec_files),
        "counts": counts,
        "phases": phases_summary,
        "completed_phase_ids": list(ctx.completed_phase_ids),
        "tasks": tasks_summary,
        "replan_count": ctx.replan_count,
        "replan_history": list(ctx.replan_history),
        "log_file": str(ctx.log_file),
        "wall_seconds": round(time.monotonic() - ctx.started_at, 1),
        "aborted": ctx.aborted,
        "abort_reason": ctx.abort_reason,
        "halt_reason": ctx.halt_reason,
        "config": {
            "auto_review": ctx.config.auto_review,
            "auto_followup": ctx.config.auto_followup,
            "use_debugger_on_retry": ctx.config.use_debugger_on_retry,
            "use_merger_on_conflict": ctx.config.use_merger_on_conflict,
            "supervisor_enabled": ctx.config.supervisor_enabled,
            "auto_replan": ctx.config.auto_replan,
            "effort": ctx.config.effort,
            "reviewer_effort": ctx.config.reviewer_effort,
            "debugger_effort": ctx.config.debugger_effort,
            "merger_effort": ctx.config.merger_effort,
            "replanner_effort": ctx.config.replanner_effort,
        },
    }
    if ctx.review_state is not None:
        out["review"] = {
            "status": ctx.review_state.status,
            "branch": ctx.review_state.branch,
            "worktree": str(ctx.review_state.worktree_path),
            "notes": ctx.review_state.notes,
            "needs_followup": (
                [
                    {"agent": fu.agent, "goal": fu.goal, "reason": fu.reason}
                    for fu in (
                        ctx.review_state.last_result.needs_followup
                        if ctx.review_state.last_result
                        else ()
                    )
                ]
                if ctx.review_state.last_result
                else []
            ),
        }
    return out
