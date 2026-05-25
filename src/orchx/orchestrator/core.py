"""Главный оркестратор роя: phased обход, параллельный спавн, retry, merge, replan.

.. note::

   P0.1: этот модуль постепенно расщепляется. Уже вынесены:

   * :mod:`orchx.orchestrator.context` — state dataclasses.
   * :mod:`orchx.orchestrator.logging_utils` — append-only журнал.
   * :mod:`orchx.orchestrator.git_utils` — git-обёртки.
   * :mod:`orchx.orchestrator.supervisor` — supervisor loop + budget helpers.

   Следующие safe extractions (требуют E2E-тестов на reviewer-pipeline):

   * ``review.py`` — финальный reviewer + 3-state verifier.
   * ``merge.py`` — merge в integration + merger spawn.
   * ``retry.py`` — retry-логика + debugger spawn + pre-merge review.
   * ``phases.py`` — phase-loop, level execution.

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
from pathlib import Path
from typing import Any

from .. import acceptance, paths, replanner, runner, worktree
from ..agent.llm import LLMClient, LLMConfig
from ..dag import phase_levels
from ..models import (
    AcceptanceCheck,
    PhaseSpec,
    Plan,
    ReviewFinding,
    ReviewReport,
    TaskResult,
    TaskSpec,
    load_plan,
    load_result,
)
from ..runtime import WORKER_RUNTIME_DIR_NAME

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config / State (P0.1: вынесено в :mod:`orchx.orchestrator.context`)
# ---------------------------------------------------------------------------

from .context import (  # noqa: E402
    AttemptInfo,
    OrchXConfig,
    OrchXContext,
    PhaseState,
    TaskState,
)

# P0.1: вынесено в :mod:`orchx.orchestrator.git_utils`
from .git_utils import (  # noqa: E402,F401
    CONFLICT_MARKER_PREFIXES,
    _files_with_conflict_markers,
    _git_add_files,
    _git_diff_stat,
    _git_diff_summary,
    _git_unmerged_files,
)

# ---------------------------------------------------------------------------
# Logging (P0.1: вынесено в :mod:`orchx.orchestrator.logging_utils`)
# ---------------------------------------------------------------------------
from .logging_utils import _orchX_log  # noqa: E402,F401

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------


async def _initialize_context(
    repo_root: Path,
    plan_path: Path,
    config: OrchXConfig,
    on_init_progress=None,
    resume: bool = False,
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

    _progress("cleaning macOS '<file> N' duplicates from .git/")
    _purged = worktree.cleanup_git_internal_duplicates(repo_root)
    if _purged:
        _orchX_log(
            ctx,
            f"purged {len(_purged)} macOS-duplicate entries from .git/ "
            f"(stale `<file> N` artefacts from previous runs that would have "
            f"caused `bad object refs/heads/...` errors): {_purged[:5]}"
            + ("..." if len(_purged) > 5 else ""),
        )

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
    if resume:
        _progress("resuming previous run — preserving worktrees and branches")
        _orchX_log(
            ctx,
            f"resume mode: reusing existing run_dir, integration branch "
            f"{integration_branch}, integration worktree {integration_worktree}",
        )
        # Если интеграционная ветка ещё не существует — создадим (на случай
        # если предыдущий прогон упал на initialize). worktree оставим как
        # есть.
        if not await worktree.branch_exists(repo_root, integration_branch):
            await worktree.create_integration_branch(
                repo_root, plan.base_branch, integration_branch
            )
        if not integration_worktree.exists():
            await worktree.add_integration_worktree(
                repo_root, integration_worktree, integration_branch
            )
            _orchX_log(ctx, f"integration worktree at {integration_worktree}")
        # Восстановим статусы из ранее записанных results/*.json.
        await _restore_states_from_results(ctx)
        _progress("ready (resumed)")
        return ctx

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


async def _restore_states_from_results(ctx: OrchXContext) -> None:
    """В resume-режиме: пометить уже выполненные задачи как success.

    Для каждой задачи плана проверяем наличие ``orchx/results/<id>.json`` в
    integration worktree. Если есть и `status: success` — пропускаем
    задачу при следующем запуске. Уже завершённые фазы помечаем тоже.
    """
    integration_results_dir = (
        ctx.integration_worktree / WORKER_RUNTIME_DIR_NAME / "results"
    )
    if not integration_results_dir.is_dir():
        return
    for state in ctx.states.values():
        result_path = integration_results_dir / f"{state.spec.id}.json"
        if not result_path.is_file():
            continue
        try:
            result = load_result(result_path)
        except (ValueError, json.JSONDecodeError):
            continue
        if result.status == "success":
            state.status = "success"
            state.last_result = result
            state.notes = result.notes
            _orchX_log(
                ctx, f"resume: task {state.spec.id} restored as success"
            )
    # Помечаем фазы — если все её задачи success, фаза тоже success.
    for ps in ctx.phase_states.values():
        if all(
            ctx.states[tid].status == "success" for tid in ps.task_ids
        ) and ps.task_ids:
            ps.status = "success"
            if ps.spec.id not in ctx.completed_phase_ids:
                ctx.completed_phase_ids.append(ps.spec.id)
            _orchX_log(ctx, f"resume: phase {ps.spec.id} restored as success")


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
            ctx.states[spec.id] = TaskState(spec=spec, branch=branch, worktree_path=wt)


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

    Между attempt'ами worktree пересоздаётся ПОЛНОСТЬЮ от свежего ref'а
    интеграционной ветки. Это важно для debugger-retry: если оригинальный
    воркер запускался параллельно с соседями, его base_ref был старее, и
    при retry'е нужен новый снимок (иначе debugger увидит «несуществующий»
    sibling-код и попробует его восстановить с устаревшего merge-base'а).
    """
    if state.worktree_path.exists():
        await worktree.remove_worktree(ctx.repo_root, state.worktree_path)
    await worktree.delete_branch(ctx.repo_root, state.branch)
    # Превентивно почистим .git/worktrees/<name>* и refs от macOS-дубликатов,
    # оставшихся от предыдущего attempt'а или от параллельных воркеров.
    _purged = worktree.cleanup_git_internal_duplicates(ctx.repo_root)
    if _purged:
        _orchX_log(
            ctx,
            f"task {state.spec.id} purged {len(_purged)} stale macOS-duplicate "
            f"entries from .git/ before recreating worktree",
        )
    await worktree.add_worktree(
        repo_root=ctx.repo_root,
        worktree_path=state.worktree_path,
        branch=state.branch,
        base_ref=ctx.integration_branch,
    )


def _build_integration_state_section(ctx: OrchXContext, state: TaskState) -> str:
    """Сформировать секцию «Integration branch state» для task.md воркера.

    Эта секция КРИТИЧЕСКИ важна для воркеров фаз 2+: они работают в
    worktree, отделённой от текущего состояния интеграционной ветки.
    Без этой секции воркер не знает, какие соседние задачи уже смержены и
    какие глобальные файлы (``backend/webapp.py``, ``backend/api/admin/__init__.py``)
    уже содержат изменения от соседей. В прошлых прогонах это приводило
    к молчаливому удалению чужих регистраций роутеров (см. api-admin-db
    в admin-subdomain run).

    Возвращает:
        Markdown-блок (без trailing newline) или пустую строку, если
        соседних задач нет (первая задача первой фазы).
    """
    already_merged: list[tuple[str, str]] = []
    # Собираем все успешные задачи плана, которые ушли в integration к
    # этому моменту (status=success И merge_sha не None).
    for s in ctx.states.values():
        if s.spec.id == state.spec.id:
            continue
        if s.status == "success" and s.merge_sha:
            already_merged.append((s.spec.id, s.spec.goal))
    if not already_merged:
        return ""
    lines: list[str] = [
        "<integration_branch_state>",
        "На момент твоего запуска в интеграционную ветку уже смержены",
        f"следующие задачи (всего {len(already_merged)}). Их код доступен",
        "в твоём worktree через обычные `read`/`grep`. **НИКОГДА не**",
        "**удаляй и не перезаписывай результаты этих задач** — твоя",
        "правка должна быть АДДИТИВНОЙ относительно их состояния.",
        "",
        (
            "Особо внимательно — общие файлы (`backend/webapp.py`,"
            " `backend/api/admin/__init__.py`, `backend/__init__.py`,"
            " `frontend/src/App.jsx`, `pyproject.toml`): соседи могли"
            " уже добавить в них импорты/регистрации. Перед `write`"
            " на такой файл — `read` его и сохрани все существующие"
            " import/include_router/routes/exports."
        ),
        "",
        "**Уже смержено:**",
        "",
    ]
    for tid, goal in already_merged[:40]:
        short_goal = goal[:140] + ("…" if len(goal) > 140 else "")
        lines.append(f"- `{tid}` — {short_goal}")
    if len(already_merged) > 40:
        lines.append(f"- _(+{len(already_merged) - 40} more)_")
    lines.append("")
    lines.append(
        "Если в твоём `file_scope` есть общий файл и ты собираешься его"
        " ПЕРЕЗАПИСАТЬ через `write` — **сначала прочти его текущее**"
        " **содержимое в этом worktree** (`read backend/<path>`), и сохрани"
        " всё, что добавили соседи. Если файл отсутствует в worktree, но"
        " по логике должен существовать (его создавала задача из списка"
        " выше) — это сигнал ошибки чек-аута: остановись со"
        " `status: \"failed\"` и опиши проблему в `notes`."
    )
    lines.append("</integration_branch_state>")
    return "\n".join(lines)


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
    orchX_dir = state.worktree_path / WORKER_RUNTIME_DIR_NAME
    orchX_dir.mkdir(parents=True, exist_ok=True)
    (orchX_dir / "results").mkdir(parents=True, exist_ok=True)
    result_path_rel = f"{WORKER_RUNTIME_DIR_NAME}/results/{state.spec.id}.json"
    task_md_content = runner.render_task_md(
        template=ctx.task_template,
        task=state.spec,
        branch=state.branch,
        result_path=result_path_rel,
    )
    # Inject «what's already merged» — без этого воркер слепой к контексту
    # роя и может откатить работу соседних задач (см. apologue в
    # _build_integration_state_section).
    integration_section = _build_integration_state_section(ctx, state)
    if integration_section:
        task_md_content += "\n\n" + integration_section + "\n"
    if debugger_context:
        task_md_content += "\n\n## Debugger context\n\n" + debugger_context + "\n"
    task_md_path = orchX_dir / "task.md"
    task_md_path.write_text(task_md_content, encoding="utf-8")
    state.result_path = state.worktree_path / result_path_rel
    return task_md_path


def _build_worker_prompt() -> str:
    """Короткое user-сообщение для воркера — всё содержательное в task.md."""
    return (
        f"Read {WORKER_RUNTIME_DIR_NAME}/task.md carefully and execute it as "
        f"an orchX worker. Write the result JSON to the path specified in "
        f"the task file. Do not exceed the allowed file scope. Finish with "
        f"a short 'done' line."
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
_ACTIVITY_TOOL_VERBS = (
    "→ tool",
    "read ",
    "write ",
    "edit ",
    "glob ",
    "grep ",
    "codesearch ",
    "bash ",
    "todo",
)


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
    parts.append(
        f"**Attempt #{last.attempt_num} verdict:** {last.failure_reason or '(unspecified)'}"
    )
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
                line += f"\n    - `file_contains:` `{check.path}` ~ `{check.pattern}`"
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
        parts.append("Прогони эти команды в worktree, чтобы увидеть текущее состояние:")
        for cmd in repro_cmds:
            parts.append(f"```bash\n{cmd}\n```")

    if state.last_result is not None:
        parts.append("\n### Worker self-report from previous attempt")
        parts.append(
            f"- **status:** `{state.last_result.status}`\n"
            f"- **artifacts:** {list(state.last_result.artifacts) or '_none_'}\n"
            f"- **notes:**\n\n" + (state.last_result.notes or "_(empty)_")
        )

    if last.outcome and last.outcome.stderr:
        snippet = textwrap.shorten(
            last.outcome.stderr, width=1500, placeholder="…[truncated]"
        )
        parts.append("\n### Last stderr (truncated)")
        parts.append(f"```\n{snippet}\n```")

    # Pre-merge review findings (если был запущен и нашёл blocking).
    if last.pre_merge_findings:
        parts.append("\n### Pre-merge code review findings")
        parts.append(
            "Reviewer прогнал твою задачу до merge'а и нашёл blocking-проблемы. "
            "Acceptance прошёл, но эти находки **обязательны к фиксу** — иначе "
            "следующий attempt снова заблокирует review."
        )
        for f in last.pre_merge_findings:
            loc: str = ""
            file_val = f.get("file")
            if file_val:
                loc = str(file_val)
                if f.get("line"):
                    loc = f"{loc}:{f.get('line')}"
            cat = f.get("category", "other")
            parts.append(
                f"- **[{cat}]** `{loc or '?'}` — {f.get('description', '')}"
            )
            if f.get("failure_scenario"):
                parts.append(f"    - **Сценарий:** {f['failure_scenario']}")
            if f.get("suggestion"):
                parts.append(f"    - **Подсказка:** {f['suggestion']}")

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

    is_debugger_retry = attempt_num > 1 and ctx.config.use_debugger_on_retry
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
    # Per-task override побеждает над глобальным role-default'ом, но
    # debugger-retry всегда поднимает effort до debugger_effort
    # (диагностика часто требует больше глубины, чем оригинальная задача).
    if is_debugger_retry:
        effort = ctx.config.debugger_effort
    elif state.spec.effort:
        effort = state.spec.effort
    else:
        effort = ctx.config.effort

    def _on_activity(line: str) -> None:
        activity = _extract_activity(line)
        if activity:
            state.current_activity = activity

    outcome = await _invoke_runtime(
        ctx,
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

    # P1.3: накапливаем cost в ctx (per-task / per-role / total).
    _accumulate_cost(ctx, state.spec.id, state.spec.agent, outcome)

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
        # P2.1: освободить worktree если включено в config.
        if ctx.config.cleanup_worktrees_after_merge and state.worktree_path:
            try:
                await worktree.remove_worktree(
                    ctx.repo_root, state.worktree_path
                )
                _orchX_log(
                    ctx,
                    f"task {state.spec.id} worktree cleaned: {state.worktree_path}",
                )
                # NB: state.worktree_path не None'им, чтобы summary мог его показать.
            except Exception as e:  # noqa: BLE001
                _orchX_log(
                    ctx,
                    f"task {state.spec.id} worktree cleanup failed (non-fatal): {e}",
                )
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
    orchX_dir = ctx.integration_worktree / WORKER_RUNTIME_DIR_NAME
    orchX_dir.mkdir(parents=True, exist_ok=True)
    (orchX_dir / "results").mkdir(parents=True, exist_ok=True)
    result_path_rel = f"{WORKER_RUNTIME_DIR_NAME}/results/{merger_id}.json"
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
        ctx.run_dir
        / "logs"
        / f"{state.spec.id}.merger.attempt{state.attempt_count}.log"
    )
    outcome = await _invoke_runtime(
        ctx,
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
                path=f"{WORKER_RUNTIME_DIR_NAME}/results/{new_id}.json",
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
                f"dependency {dep_id} status=" f"{dep.status if dep else 'missing'}"
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
            # Pre-merge review (если включён).
            if ctx.config.per_task_review:
                review_ok, review_note = await _run_pre_merge_review(ctx, state)
                if not review_ok:
                    # Финальный info и retry — debugger получит findings
                    # как часть failure_context.
                    state.status = "failed"
                    state.notes = (
                        f"pre-merge review found blocking findings: {review_note}"
                    )
                    _orchX_log(
                        ctx,
                        f"task {spec.id} pre-merge review BLOCKED — {review_note}",
                    )
                    if state.attempts:
                        state.attempts[-1].failure_reason = state.notes
                    # fall through to retry-loop
                else:
                    merged = await _commit_and_merge(ctx, state)
                    if merged:
                        return
            else:
                merged = await _commit_and_merge(ctx, state)
                if merged:
                    return
            # merge conflict / blocked review → задача упала, ретраим.
        if state.attempt_count > spec.max_retries:
            break
        if ctx.total_retries >= ctx.plan.global_budget.max_total_retries:
            _orchX_log(ctx, f"global retry budget exhausted; not retrying {spec.id}")
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
# Pre-merge code review (lightweight)
# ---------------------------------------------------------------------------


async def _run_pre_merge_review(
    ctx: OrchXContext, state: TaskState
) -> tuple[bool, str]:
    """Запустить lightweight orchX-reviewer на дифф одной задачи.

    Reviewer работает прямо в worktree задачи (не в integration), читает
    дифф против integration-ветки. Если он находит хотя бы одну
    blocking-находку — задача отправляется на retry; debugger получит
    findings'ы как часть `failure_context` следующего attempt'а.

    Returns:
        (passed, note). ``passed=True`` означает, что blocking-замечаний
        нет и можно мерджить. ``note`` — короткое описание для лога/
        debugger-context'а.
    """
    review_id = f"premerge__{state.spec.id}__attempt{state.attempt_count}"
    orchX_dir = state.worktree_path / WORKER_RUNTIME_DIR_NAME
    orchX_dir.mkdir(parents=True, exist_ok=True)
    (orchX_dir / "results").mkdir(parents=True, exist_ok=True)
    result_rel = f"{WORKER_RUNTIME_DIR_NAME}/results/{review_id}.json"
    result_path = state.worktree_path / result_rel

    task_md = _render_pre_merge_review_task_md(
        ctx=ctx,
        state=state,
        review_id=review_id,
        result_rel=result_rel,
    )
    task_md_path = orchX_dir / "task.md"
    task_md_path.write_text(task_md, encoding="utf-8")
    if result_path.exists():
        result_path.unlink()

    log_file = (
        ctx.run_dir
        / "logs"
        / f"{state.spec.id}.premerge-review.attempt{state.attempt_count}.log"
    )
    outcome = await _invoke_runtime(
        ctx,
        cwd=state.worktree_path,
        repo_root=ctx.repo_root,
        role="reviewer",
        prompt=(
            f"Read {WORKER_RUNTIME_DIR_NAME}/task.md. Run a focused "
            f"pre-merge review of THIS task's diff (not the whole "
            f"integration branch). Write `{result_rel}` with a structured "
            f"review_report. Finish with the literal word `done`."
        ),
        timeout_s=600,
        log_file=log_file,
        effort=ctx.config.per_task_review_effort,
    )
    if outcome.timed_out or outcome.returncode != 0:
        # Reviewer упал — по best-effort не блокируем merge, чтобы не
        # сорвать прогон из-за самого ревью. Но логируем.
        _orchX_log(
            ctx,
            f"pre-merge review for {state.spec.id} failed to run "
            f"(rc={outcome.returncode} timed_out={outcome.timed_out}); "
            "proceeding to merge",
        )
        return True, "reviewer-tooling-failed"

    if not result_path.exists():
        _orchX_log(
            ctx,
            f"pre-merge review for {state.spec.id}: no result.json; proceeding",
        )
        return True, "reviewer-skipped"

    try:
        result = load_result(result_path)
    except (ValueError, json.JSONDecodeError) as e:
        _orchX_log(
            ctx,
            f"pre-merge review for {state.spec.id}: invalid result.json: {e}",
        )
        return True, f"invalid-review:{e!r}"

    report = result.review_report
    if report is None or not report.findings:
        _orchX_log(
            ctx,
            f"pre-merge review for {state.spec.id}: clean (no findings)",
        )
        return True, "clean"
    if report.blocking_count == 0:
        _orchX_log(
            ctx,
            f"pre-merge review for {state.spec.id}: "
            f"non-blocking findings only ({report.non_blocking_count} non-blocking, "
            f"{report.nit_count} nits) — allowing merge",
        )
        return True, (
            f"non-blocking-only "
            f"(non={report.non_blocking_count} nit={report.nit_count})"
        )
    # Blocking — задача провалилась по review.
    summary_parts: list[str] = []
    findings_for_attempt: list[dict[str, Any]] = []
    for f in report.findings:
        if f.severity != "blocking":
            continue
        loc = f"{f.file}:{f.line}" if f.file and f.line else (f.file or "?")
        summary_parts.append(f"[{f.category}] {loc} — {f.description}")
        findings_for_attempt.append(
            {
                "file": f.file,
                "line": f.line,
                "severity": f.severity,
                "category": f.category,
                "description": f.description,
                "failure_scenario": f.failure_scenario,
                "suggestion": f.suggestion,
            }
        )
    if state.attempts:
        state.attempts[-1].pre_merge_findings = findings_for_attempt
    note = "; ".join(summary_parts[:3])
    if len(summary_parts) > 3:
        note += f"; (+{len(summary_parts) - 3} more)"
    return False, note


def _render_pre_merge_review_task_md(
    *,
    ctx: OrchXContext,
    state: TaskState,
    review_id: str,
    result_rel: str,
) -> str:
    """Сгенерировать task.md для pre-merge reviewer'а."""
    return f"""# Pre-merge code review: `{state.spec.id}`

> Ты — orchX-reviewer в режиме PRE-MERGE. Этот worktree содержит результат
> работы воркера `{state.spec.agent}` над задачей `{state.spec.id}`. Сейчас
> задача прошла acceptance, но **ещё НЕ смержена** в интеграционную ветку.
> Твой джоб — поймать blocking-bugs до merge'а.

## Goal задачи (исходный)

{state.spec.goal}

## Что ревьюить

```bash
git diff {ctx.integration_branch}...HEAD
```

Это дифф **только этой задачи** против интеграционной ветки.

## File scope задачи

{chr(10).join(f"- `{p}`" for p in state.spec.file_scope) or "_(не задан)_"}

## Что искать (фокус на Angle A — line-by-line)

- Логические ошибки (инверсия условий, off-by-one, falsy-zero, copy-paste).
- Контракт-breaking изменения публичных функций без обновления callers.
- Hard-coded секреты, пути, debug print'ы, забытые TODO.
- Циклические импорты при изменениях `**/__init__.py`.
- Регистрация роутеров/handlers в FastAPI app (а не только импорт).

## Чего НЕ делать

- Не запускай тесты — они уже прошли.
- Не отмечай stylistic nits как blocking. **Reviewer на этом этапе
  либо ставит `blocking` и блокирует merge, либо `non-blocking`/`nit`
  и пропускает merge. False positives дороги.**
- Не редактируй код. Только запись review_report в JSON.

## Result file

Запиши `{result_rel}` строго по схеме (см. orchX-reviewer.md):

```json
{{
  "task_id": "{review_id}",
  "status": "success",
  "artifacts": [],
  "notes": "1-2 предложения о результате review",
  "review_report": {{
    "summary": "Опционально",
    "findings": [
      {{
        "severity": "blocking",
        "category": "bug",
        "file": "backend/foo.py",
        "line": 42,
        "description": "...",
        "failure_scenario": "...",
        "suggestion": "..."
      }}
    ]
  }}
}}
```

`status: "success"` если нет blocking-замечаний (даже если есть nits).
`status: "failed"` если есть blocking — диспетчер автоматически отправит
задачу на retry через debugger.

Финальная реплика — ровно `done`.
"""


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
    diff_check = await _git_diff_summary(ctx.integration_worktree, ctx.plan.base_branch)
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
        inputs=(f"{WORKER_RUNTIME_DIR_NAME}/plan.json",),
        outputs=(),
        file_scope=(f"{WORKER_RUNTIME_DIR_NAME}/results/**",),
        acceptance=(
            AcceptanceCheck(
                type="file_exists",
                description="reviewer wrote result.json",
                path=(
                    f"{WORKER_RUNTIME_DIR_NAME}/results/"
                    f"review__{ctx.plan.task_id}.json"
                ),
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
    orchX_dir = review_wt / WORKER_RUNTIME_DIR_NAME
    orchX_dir.mkdir(parents=True, exist_ok=True)
    (orchX_dir / "results").mkdir(parents=True, exist_ok=True)
    result_path_rel = f"{WORKER_RUNTIME_DIR_NAME}/results/{review_spec.id}.json"
    review_state.result_path = review_wt / result_path_rel

    # Скопируем активный план и журнал прогона прямо в worktree reviewer'а,
    # чтобы он мог читать их по относительным путям от своего cwd. Сами
    # runtime-артефакты живут в `orchx/runs/<task_id>/` корня репо, но из
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
        ctx=ctx,
        review_spec=review_spec,
        result_path_rel=result_path_rel,
        diff_stat=diff_stat,
    )
    (orchX_dir / "task.md").write_text(task_md, encoding="utf-8")

    log_file = ctx.run_dir / "logs" / f"{review_spec.id}.log"
    info = AttemptInfo(attempt_num=1, agent_used="orchX-reviewer")
    review_state.attempts.append(info)
    outcome = await _invoke_runtime(
        ctx,
        cwd=review_wt,
        repo_root=ctx.repo_root,
        role="reviewer",
        prompt=_build_worker_prompt(),
        timeout_s=review_spec.timeout_seconds,
        log_file=log_file,
        effort=ctx.config.reviewer_effort,
    )
    info.outcome = outcome
    # P1.3: cost для reviewer'а.
    _accumulate_cost(ctx, review_spec.id, "reviewer", outcome)

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

    # Verifier phase (3-state). Каждое finding получает verdict
    # `confirmed`/`plausible`/`refuted`. REFUTED-findings отбрасываются.
    if result.review_report and result.review_report.findings:
        verified_report = await _verify_review_findings(
            ctx=ctx,
            report=result.review_report,
            review_wt=review_wt,
        )
        if verified_report is not None:
            # Перезаписываем result.json через простой rewrite (reviewer
            # уже завершил работу, мы держим только TaskResult).
            review_state.last_result = TaskResult(
                task_id=result.task_id,
                status=result.status,
                artifacts=result.artifacts,
                notes=result.notes,
                metrics=result.metrics,
                needs_followup=result.needs_followup,
                review_report=verified_report,
            )
            try:
                _persist_review_result(review_state.result_path, review_state.last_result)
            except OSError as e:
                _orchX_log(
                    ctx,
                    f"could not persist verified review report: {e}",
                )
            result = review_state.last_result

    # Blocking findings всегда означают failed — даже если reviewer
    # самостоятельно поставил success/partial. Это даёт диспетчеру
    # источник правды для PR-маркера и автоматической генерации
    # follow-up задач.
    blocking = (
        result.review_report.blocking_count if result.review_report else 0
    )
    if blocking > 0:
        review_state.status = "failed"
    elif result.status == "failed":
        review_state.status = "failed"
    else:
        review_state.status = "success"
    review_state.notes = result.notes
    _orchX_log(
        ctx,
        f"auto-review done: status={review_state.status} "
        f"reported={result.status} "
        f"findings={len(result.review_report.findings) if result.review_report else 0} "
        f"blocking={blocking} "
        f"followup_count={len(result.needs_followup)}",
    )
    return review_state


_VERIFIER_SYSTEM_PROMPT = """Ты — verifier code-review findings. Тебе \
дают finding от reviewer'а с указанием файла, строки и описания. \
Твоя задача — проголосовать за один из трёх вердиктов:

- **CONFIRMED** — finding точный: ты можешь воспроизвести проблему \
(назвать input/state, при котором код упадёт или вернёт неверное значение). \
Цитируй конкретную строку.
- **PLAUSIBLE** — механизм реален, но триггер неуверен (зависит от env, \
конфига, тайминга). Назови, что подтвердило бы вердикт.
- **REFUTED** — finding фактически неверен. Например, в коде написано \
не то, что утверждает finding; есть guard в другом месте, который \
покрывает сценарий; или это вообще не баг (например, intentional fallback).

Формат твоего ответа — ТОЛЬКО одна строка, начинающаяся со слова \
CONFIRMED, PLAUSIBLE или REFUTED, без объяснений и без префиксов. \
Никаких списков, никаких заголовков."""


async def _verify_review_findings(
    *,
    ctx: OrchXContext,
    report: ReviewReport,
    review_wt: Path,
) -> ReviewReport | None:
    """3-state verifier: для каждого finding'а получаем verdict.

    Findings со вердиктом ``REFUTED`` отбрасываются. ``CONFIRMED``/
    ``PLAUSIBLE`` остаются в отчёте, дополненные полем ``verifier_verdict``.

    Args:
        ctx: orchX context.
        report: исходный review report.
        review_wt: worktree, где лежит код, которое можно прочитать
            для верификации.

    Returns:
        Новый ``ReviewReport`` с обновлённым списком findings, либо
        ``None`` если verifier-этап не удался (тогда оставляем оригинал).
    """
    if not report.findings:
        return None
    _orchX_log(ctx, f"verifier: starting on {len(report.findings)} findings")
    role_llm = ctx.llm.for_role("reviewer", effort=ctx.config.reviewer_effort)
    verified: list[ReviewFinding] = []
    refuted: list[ReviewFinding] = []
    for f in report.findings:
        verdict = await _verify_one_finding(
            llm=role_llm, finding=f, review_wt=review_wt
        )
        if verdict == "refuted":
            refuted.append(f)
            continue
        verified.append(
            ReviewFinding(
                severity=f.severity,
                category=f.category,
                description=f.description,
                file=f.file,
                line=f.line,
                failure_scenario=f.failure_scenario,
                suggestion=f.suggestion,
                verifier_verdict=verdict,
            )
        )
    _orchX_log(
        ctx,
        f"verifier: {len(verified)} kept "
        f"({sum(1 for v in verified if v.verifier_verdict == 'confirmed')} confirmed, "
        f"{sum(1 for v in verified if v.verifier_verdict == 'plausible')} plausible), "
        f"{len(refuted)} refuted",
    )
    return ReviewReport(findings=tuple(verified), summary=report.summary)


async def _verify_one_finding(
    *,
    llm,
    finding: ReviewFinding,
    review_wt: Path,
) -> str:
    """Один verifier-проход. Возвращает ``confirmed`` / ``plausible`` / ``refuted``.

    На любом сбое (LLM error, неожиданный текст) возвращает ``plausible``
    как safe-fallback — не отбрасываем потенциально-реальный finding.
    """
    # Прочитаем релевантные строки файла, чтобы verifier'у было что цитировать.
    file_excerpt = ""
    if finding.file:
        path = review_wt / finding.file
        if path.is_file():
            try:
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
                if finding.line:
                    start = max(0, finding.line - 8)
                    end = min(len(lines), finding.line + 8)
                    excerpt_lines = [
                        f"{i + 1}: {lines[i]}" for i in range(start, end)
                    ]
                    file_excerpt = "\n".join(excerpt_lines)
                else:
                    excerpt_lines = [
                        f"{i + 1}: {lines[i]}" for i in range(min(60, len(lines)))
                    ]
                    file_excerpt = "\n".join(excerpt_lines)
            except OSError:
                pass

    user_prompt = (
        f"Finding to verify:\n\n"
        f"- File: {finding.file or '(none)'}\n"
        f"- Line: {finding.line or '(none)'}\n"
        f"- Severity: {finding.severity}\n"
        f"- Category: {finding.category}\n"
        f"- Description: {finding.description}\n"
        f"- Failure scenario: {finding.failure_scenario or '(none)'}\n\n"
    )
    if file_excerpt:
        user_prompt += f"Excerpt of {finding.file}:\n```\n{file_excerpt}\n```\n\n"
    user_prompt += "Verdict:"

    try:
        resp = await llm.chat(
            messages=[
                {"role": "system", "content": _VERIFIER_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            tools=None,
        )
    except Exception:  # noqa: BLE001
        return "plausible"
    text = (resp.text or "").strip().lower()
    if text.startswith("confirmed"):
        return "confirmed"
    if text.startswith("refuted"):
        return "refuted"
    return "plausible"


def _persist_review_result(path: Path | None, result: TaskResult) -> None:
    """Перезаписать result.json reviewer'а с обновлённым review_report.

    Используется после verifier-фазы. Ничего не делает, если ``path`` нет.
    """
    if path is None or not path.parent.exists():
        return
    serialized = {
        "task_id": result.task_id,
        "status": result.status,
        "artifacts": list(result.artifacts),
        "notes": result.notes,
        "metrics": result.metrics,
        "needs_followup": [
            {"agent": fu.agent, "goal": fu.goal, "reason": fu.reason}
            for fu in result.needs_followup
        ],
    }
    if result.review_report:
        serialized["review_report"] = {
            "summary": result.review_report.summary,
            "findings": [
                {
                    "severity": f.severity,
                    "category": f.category,
                    "description": f.description,
                    **({"file": f.file} if f.file else {}),
                    **({"line": f.line} if f.line else {}),
                    **(
                        {"failure_scenario": f.failure_scenario}
                        if f.failure_scenario
                        else {}
                    ),
                    **({"suggestion": f.suggestion} if f.suggestion else {}),
                    **(
                        {"verifier_verdict": f.verifier_verdict}
                        if f.verifier_verdict
                        else {}
                    ),
                }
                for f in result.review_report.findings
            ],
        }
    path.write_text(
        json.dumps(serialized, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


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

- Соответствие диффа целям из `orchx/plan.json`.
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

- Любые правки кода. `edit` ограничен только `orchx/results/**`.
- Запуск тестов, билдов, форматтеров — это уже сделали воркеры.
- Вызов Task tool / new_task.
"""


# ---------------------------------------------------------------------------
# Supervisor
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
from .supervisor import (  # noqa: E402,F401
    _all_failures_are_env,
    _budget_exceeded,
    _supervisor_loop,  # noqa: E402,F401
)


async def _record_run_to_memory(ctx: OrchXContext, summary: dict[str, Any]) -> None:
    """P0.3 / P2.4: после прогона сохранить план + результат в memory plugin.

    Сохраняем 3 факта:

    * ``plans/<task_id>`` — task_id + summary + repo_root для будущих похожих planner-вызовов.
    * ``failures/<task_id>`` — если есть failed-задачи (для debugger context recall).
    * ``reviews/<task_id>`` — если был reviewer.
    """
    if ctx.memory is None:
        return
    try:
        repo_root_str = str(ctx.repo_root)
        plan_payload = {
            "task_id": ctx.plan.task_id,
            "summary": ctx.plan.summary,
            "base_branch": ctx.plan.base_branch,
            "phases": [{"id": p.id, "goal": p.goal} for p in ctx.plan.phases],
            "counts": summary.get("counts", {}),
            "wall_seconds": summary.get("wall_seconds"),
            "aborted": summary.get("aborted", False),
            "__repo_root__": repo_root_str,
        }
        await ctx.memory.remember("plans", ctx.plan.task_id, plan_payload)

        failed = [
            t for t in summary.get("tasks", []) if t.get("status") == "failed"
        ]
        if failed:
            fail_payload = {
                "task_id": ctx.plan.task_id,
                "failed_tasks": failed,
                "halt_reason": summary.get("halt_reason"),
                "__repo_root__": repo_root_str,
            }
            await ctx.memory.remember(
                "failures", ctx.plan.task_id, fail_payload
            )
        if ctx.review_state and ctx.review_state.last_result:
            review = ctx.review_state.last_result.review_report
            if review:
                review_payload = {
                    "task_id": ctx.plan.task_id,
                    "summary": review.summary,
                    "findings": [
                        {
                            "severity": f.severity,
                            "category": f.category,
                            "description": f.description[:200],
                            "verifier_verdict": f.verifier_verdict,
                        }
                        for f in review.findings
                    ],
                    "__repo_root__": repo_root_str,
                }
                await ctx.memory.remember(
                    "reviews", ctx.plan.task_id, review_payload
                )
    except Exception:  # noqa: BLE001
        logger.warning("memory.remember failed at run end", exc_info=True)


async def _invoke_runtime(
    ctx: OrchXContext,
    *,
    cwd: Path,
    role: str,
    prompt: str,
    timeout_s: int,
    log_file: Path,
    effort: str | None,
    on_activity: Any = None,
    repo_root: Path | None = None,
) -> runner.WorkerOutcome:
    """Спавнить worker через ctx.runtime (плагин), если есть; иначе fallback на runner.

    Все вызовы runner.run_worker во внутреннем коде orchestrator'а
    должны идти через этот хелпер (см. P0.2 / P1.2).
    """
    _repo_root = repo_root if repo_root is not None else ctx.repo_root
    if ctx.runtime is not None and hasattr(ctx.runtime, "spawn_worker"):
        try:
            return await ctx.runtime.spawn_worker(
                cwd=cwd,
                repo_root=_repo_root,
                role=role,
                prompt=prompt,
                timeout_s=timeout_s,
                log_file=log_file,
                effort=effort,
                on_activity=on_activity,
                llm=ctx.llm,
            )
        except TypeError:
            # Старый runtime без llm kw — fallback.
            return await ctx.runtime.spawn_worker(
                cwd=cwd,
                repo_root=_repo_root,
                role=role,
                prompt=prompt,
                timeout_s=timeout_s,
                log_file=log_file,
                effort=effort,
                on_activity=on_activity,
            )
    return await runner.run_worker(
        llm=ctx.llm,
        cwd=cwd,
        repo_root=_repo_root,
        role=role,
        prompt=prompt,
        timeout_s=timeout_s,
        log_file=log_file,
        effort=effort,
        on_activity=on_activity,
    )


async def _maybe_spawn_followup_fixups(ctx: OrchXContext) -> int:
    """P1.8: для каждого blocking finding в reviewer.review_report создать
    TaskSpec для debugger'а и сохранить в ``ctx.run_dir/auto_fixup_plan.json``.

    Возвращает количество сгенерированных задач. NB: текущая версия
    **только сохраняет** план fixup'ов в файл и отсылает notification
    (наблюдатель может реагировать), но не блокирует основной цикл и
    не вызывает _run_one_phase повторно — это требует более глубокой
    интеграции с DAG (планируется на P2.x).
    """
    if ctx.review_state is None or ctx.review_state.last_result is None:
        return 0
    report = ctx.review_state.last_result.review_report
    if not report or report.blocking_count == 0:
        return 0

    fixup_specs: list[dict[str, Any]] = []
    for i, f in enumerate(report.findings):
        if f.severity != "blocking":
            continue
        fixup_specs.append(
            {
                "id": f"orchX-autofix-{i + 1}",
                "agent": "debugger",
                "depends_on": [],
                "goal": (
                    f"Fix blocking review finding ({f.category}): {f.description}"
                ),
                "file_scope": [f.file] if f.file else [],
                "acceptance": [
                    {
                        "type": "command",
                        "description": "Build / tests pass after fix",
                        "command": f.failure_scenario or "true",
                    }
                ],
                "context": {
                    "review_finding": {
                        "severity": f.severity,
                        "category": f.category,
                        "description": f.description,
                        "file": f.file,
                        "line": f.line,
                        "failure_scenario": f.failure_scenario,
                        "suggestion": f.suggestion,
                        "verifier_verdict": f.verifier_verdict,
                    }
                },
                "max_retries": 2,
                "timeout_seconds": 900,
            }
        )

    if not fixup_specs:
        return 0

    plan_path = ctx.run_dir / "auto_fixup_plan.json"
    plan_path.write_text(
        json.dumps(
            {
                "source_task_id": ctx.plan.task_id,
                "source_review_state": ctx.review_state.spec.id
                if ctx.review_state
                else None,
                "tasks": fixup_specs,
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    _orchX_log(
        ctx,
        f"auto-fixup: wrote {len(fixup_specs)} debugger task(s) to {plan_path.name}",
    )
    if ctx.notifier is not None:
        try:
            await ctx.notifier.notify(
                "auto_fixup_planned",
                {
                    "task_id": ctx.plan.task_id,
                    "count": len(fixup_specs),
                    "plan_path": str(plan_path),
                },
            )
        except Exception:  # noqa: BLE001
            pass
    return len(fixup_specs)


class _CompoundNotifier:
    """Fan-out нескольких NotifierPlugin'ов в один интерфейс.

    Используется, когда в конфиге указано несколько ``notifiers:``.
    Ошибки одного notifier'а не блокируют другие.
    """

    def __init__(self, notifiers: list[Any]):
        self._notifiers = list(notifiers)

    async def notify(self, event: str, payload: dict[str, Any]) -> None:
        for n in self._notifiers:
            try:
                await n.notify(event, payload)
            except Exception:  # noqa: BLE001
                logger.warning(
                    "notifier %s.notify(%s) failed",
                    type(n).__name__,
                    event,
                    exc_info=True,
                )


def _accumulate_cost(
    ctx: OrchXContext,
    task_id: str,
    role: str,
    outcome: runner.WorkerOutcome,
) -> None:
    """P1.3: накопить cost в ctx.total/by_role/by_task + триггерить notifications.

    Опционально шлёт ``cost_alert`` notification на пересечении 50/75/90% бюджета
    (если ``ctx.notifier`` не None и ``config.max_cost_usd`` задан).
    """
    cost = float(getattr(outcome, "cost_usd", 0.0) or 0.0)
    if cost <= 0:
        return
    ctx.total_cost_usd += cost
    ctx.cost_by_role[role] = ctx.cost_by_role.get(role, 0.0) + cost
    ctx.cost_by_task[task_id] = ctx.cost_by_task.get(task_id, 0.0) + cost
    budget = ctx.config.max_cost_usd
    if budget and budget > 0 and ctx.notifier is not None:
        ratio = ctx.total_cost_usd / budget
        prev = (ctx.total_cost_usd - cost) / budget
        for threshold in (0.5, 0.75, 0.9):
            if prev < threshold <= ratio:
                try:
                    asyncio.ensure_future(
                        ctx.notifier.notify(
                            "cost_alert",
                            {
                                "task_id": ctx.plan.task_id,
                                "threshold_pct": int(threshold * 100),
                                "total_usd": round(ctx.total_cost_usd, 4),
                                "budget_usd": budget,
                            },
                        )
                    )
                except Exception:  # noqa: BLE001
                    pass


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
        ctx.states[tid] for tid in ps.task_ids if ctx.states[tid].status == "failed"
    ]
    if failed_in_phase:
        ps.status = "failed"
        ps.notes = f"{len(failed_in_phase)} tasks failed: " + ", ".join(
            s.spec.id for s in failed_in_phase
        )
        ps.finished_at = time.monotonic()
        _orchX_log(ctx, f"phase {phase.id} FAILED: {ps.notes}")
        return False

    ps.status = "success"
    ps.notes = f"{len(ps.task_ids)} tasks ok"
    ps.finished_at = time.monotonic()
    _orchX_log(
        ctx, f"phase {phase.id} SUCCESS in {ps.finished_at - ps.started_at:.0f}s"
    )
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

    failed_tasks = [
        ctx.states[tid]
        for tid in ctx.phase_states[failed_phase.id].task_ids
        if ctx.states[tid].status == "failed"
    ]

    # ENV-aware bailout: если все провалившиеся задачи фазы упали по
    # категории `env` (битый venv, отсутствующий бинарь), replan не
    # поможет — planner создаст новый план с таким же `uv run pytest` и
    # тот тоже упадёт. Лучше остановиться с advisory и дать пользователю
    # починить окружение.
    if _all_failures_are_env(failed_tasks):
        _orchX_log(
            ctx,
            f"phase {failed_phase.id}: all failures are environment "
            "(missing tooling / broken venv) — replan would not help. "
            "Halting and pointing the user at the env-setup hints in "
            "the failure notes.",
        )
        ctx.halt_reason = (
            "Все задачи фазы провалились из-за окружения "
            "(например, отсутствующий бинарь или сломанный venv). "
            "Replan не поможет. Поправь окружение и перезапусти рой."
        )
        return False

    ctx.replan_count += 1
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
    resume: bool = False,
    *,
    plugins: dict[str, Any] | None = None,
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
        repo_root,
        plan_path,
        config,
        on_init_progress=on_init_progress,
        resume=resume,
    )
    # P0.2 / P1.5 / P0.3 / P1.2: подключаем плагины из переданного bag'а.
    if plugins:
        ctx.runtime = plugins.get("runtime") or ctx.runtime
        ctx.memory = plugins.get("memory") or ctx.memory
        ctx.tracker = plugins.get("tracker") or ctx.tracker
        notifiers = plugins.get("notifiers") or []
        if notifiers:
            # Compose multiple notifiers в одну fan-out обёртку.
            ctx.notifier = _CompoundNotifier(notifiers)

    # P1.5: notify start.
    if ctx.notifier is not None:
        try:
            await ctx.notifier.notify(
                "run_started",
                {
                    "task_id": ctx.plan.task_id,
                    "phases": len(ctx.plan.phases),
                    "tasks": sum(len(p.tasks) for p in ctx.plan.phases),
                    "base_branch": ctx.plan.base_branch,
                    "integration_branch": ctx.integration_branch,
                },
            )
        except Exception:  # noqa: BLE001
            logger.warning("notifier run_started failed", exc_info=True)

    # 0.2.1: tracker update — отметим задачу как «в работе».
    # ``tracker_task_id`` (если задан) — composite id из внешнего трекера
    # (например, GitHub Projects ``PVTI_xxx:114``). Без него tracker может
    # лишь оставить коммент по issue number, но не подвинет карточку.
    tracker_id = ctx.plan.tracker_task_id or ctx.plan.task_id
    if ctx.tracker is not None:
        try:
            await ctx.tracker.update_status(
                tracker_id,
                "running",
                f"orchX run started — integration: `{ctx.integration_branch}`",
            )
        except Exception:  # noqa: BLE001
            logger.warning("tracker update_status(running) failed", exc_info=True)

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
                # Replanner мог уже выставить halt_reason (например, ENV
                # bailout — менее общая причина, чем generic «replan
                # unavailable»). Перезаписывать не нужно.
                if ctx.halt_reason:
                    halt_reason = ctx.halt_reason
                else:
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
                # P1.8: auto-fixup chain — blocking findings → debugger tasks.
                if config.auto_fixup_chain:
                    fixup_count = await _maybe_spawn_followup_fixups(ctx)
                    if fixup_count > 0:
                        _orchX_log(
                            ctx,
                            f"auto-fixup: spawned {fixup_count} debugger task(s) "
                            f"from blocking findings; re-running affected phase",
                        )
                        # NB: пока не запускаем повторный pass — это требует
                        # phase-extension, что-то рисковее. v1 P1.8: только
                        # генерируем follow-up TaskSpecs и логируем; v2:
                        # повторный _run_one_phase для синтетической фазы.
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
        + (f" review={summary['review']['status']}" if summary.get("review") else ""),
    )

    # P1.5: notify finish.
    if ctx.notifier is not None:
        try:
            await ctx.notifier.notify(
                "run_finished",
                {
                    "task_id": ctx.plan.task_id,
                    "counts": summary["counts"],
                    "total_cost_usd": round(ctx.total_cost_usd, 4),
                    "halt_reason": halt_reason or None,
                    "aborted": ctx.aborted,
                },
            )
        except Exception:  # noqa: BLE001
            logger.warning("notifier run_finished failed", exc_info=True)

    # 0.2.1: tracker — обновить статус задачи (done / failed).
    if ctx.tracker is not None:
        counts = summary.get("counts", {})
        all_ok = (
            not ctx.aborted
            and counts.get("failed", 0) == 0
            and counts.get("success", 0) > 0
        )
        tracker_status = "done" if all_ok else "failed"
        details_parts: list[str] = [
            f"orchX finished — success={counts.get('success', 0)} "
            f"failed={counts.get('failed', 0)} "
            f"skipped={counts.get('skipped', 0)}",
            f"Cost: ${round(ctx.total_cost_usd, 4)}",
        ]
        if halt_reason:
            details_parts.append(f"Halt: {halt_reason}")
        try:
            await ctx.tracker.update_status(
                ctx.plan.tracker_task_id or ctx.plan.task_id,
                tracker_status,
                "\n".join(details_parts),
            )
        except Exception:  # noqa: BLE001
            logger.warning(
                "tracker update_status(%s) failed", tracker_status, exc_info=True
            )

    # P0.3 / P2.4: записать результаты в memory plugin (если включён).
    await _record_run_to_memory(ctx, summary)

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
    # Расширенные метрики для cost/quality-анализа.
    total_input_tokens = 0
    total_output_tokens = 0
    total_llm_calls = 0
    total_compactions = 0
    failure_categories: dict[str, int] = {}
    for state in ctx.states.values():
        counts[state.status] = counts.get(state.status, 0) + 1
        tasks_summary.append(_summarize_state(state))
        for attempt in state.attempts:
            if attempt.outcome:
                total_input_tokens += attempt.outcome.input_tokens
                total_output_tokens += attempt.outcome.output_tokens
                total_llm_calls += attempt.outcome.llm_calls
                total_compactions += attempt.outcome.compactions
            for outcome in attempt.acceptance_outcomes:
                if not outcome.passed:
                    cat = outcome.category or "unknown"
                    failure_categories[cat] = failure_categories.get(cat, 0) + 1
    if ctx.review_state and ctx.review_state.attempts:
        for attempt in ctx.review_state.attempts:
            if attempt.outcome:
                total_input_tokens += attempt.outcome.input_tokens
                total_output_tokens += attempt.outcome.output_tokens
                total_llm_calls += attempt.outcome.llm_calls
                total_compactions += attempt.outcome.compactions

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

    counts["total"] = sum(counts.values())
    metrics = {
        "total_input_tokens": total_input_tokens,
        "total_output_tokens": total_output_tokens,
        "total_tokens": total_input_tokens + total_output_tokens,
        "total_llm_calls": total_llm_calls,
        "total_compactions": total_compactions,
        "total_retries": ctx.total_retries,
        "failure_categories": dict(
            sorted(failure_categories.items(), key=lambda kv: -kv[1])
        ),
    }
    # P1.3: cost block.
    cost_block: dict[str, Any] = {
        "total_usd": round(ctx.total_cost_usd, 6),
        "by_role": {k: round(v, 6) for k, v in ctx.cost_by_role.items()},
        "by_task": {k: round(v, 6) for k, v in ctx.cost_by_task.items()},
    }
    if ctx.config.max_cost_usd is not None:
        cost_block["budget_usd"] = ctx.config.max_cost_usd
        cost_block["budget_used_pct"] = (
            round(100 * ctx.total_cost_usd / ctx.config.max_cost_usd, 1)
            if ctx.config.max_cost_usd > 0
            else None
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
        "metrics": metrics,
        "cost": cost_block,
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
            "per_task_review": ctx.config.per_task_review,
            "per_task_review_effort": ctx.config.per_task_review_effort,
        },
    }
    if ctx.review_state is not None:
        review_block: dict[str, Any] = {
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
        # Структурированные findings в summary — для PR body и downstream'ов.
        if ctx.review_state.last_result and ctx.review_state.last_result.review_report:
            rr = ctx.review_state.last_result.review_report
            review_block["report"] = {
                "summary": rr.summary,
                "blocking_count": rr.blocking_count,
                "non_blocking_count": rr.non_blocking_count,
                "nit_count": rr.nit_count,
                "findings": [
                    {
                        "severity": f.severity,
                        "category": f.category,
                        "file": f.file,
                        "line": f.line,
                        "description": f.description,
                        "failure_scenario": f.failure_scenario,
                        "suggestion": f.suggestion,
                        "verifier_verdict": f.verifier_verdict,
                    }
                    for f in rr.findings
                ],
            }
        out["review"] = review_block
    return out
