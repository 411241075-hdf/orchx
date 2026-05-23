"""CLI-entrypoint для роя.

После любого прогона (`run` или `all`) диспетчер ВСЕГДА пушит интеграционную
ветку и открывает GitHub PR — независимо от того, упали ли часть задач или
reviewer нашёл блокирующие проблемы. Принимать или отклонять PR — задача
человека на стороне GitHub. Поэтому отдельных флагов `--open-pr`/`--force-pr`
больше нет: PR — это часть контракта.

Использование (после ``uv sync --extra orchx`` или ``pip install -e ".[orchx]"``):

    orchx plan "<задача в свободной форме>"
    orchx run [path/to/plan.json] [behavior flags]
    orchx all "<задача>" [behavior flags]

Или напрямую через Python (без install'а):

    python -m orchx plan "<задача>"

Behavior flags (action на этапе run/all):
    --no-review            не запускать orchX-reviewer на финале
    --auto-followup        включить динамические followup-задачи (по умолчанию off)
    --max-followup-depth N максимальная глубина каскада followup'ов (по умолч. 1)
    --no-debugger          retry'ять оригинальным агентом, не orchX-debugger
    --no-merger            при merge-конфликте просто abort+fail, без orchX-merger
    --no-supervisor        не запускать heartbeat-supervisor
    --supervisor-interval-s F  период heartbeat'а (по умолч. 30s)
    --effort               reasoning-effort для воркеров
    --reviewer-effort      reasoning-effort для orchX-reviewer
    --debugger-effort      reasoning-effort для orchX-debugger
    --merger-effort        reasoning-effort для orchX-merger

Output:
    По умолчанию вывод в TTY — компактный TUI с лайв-доской прогресса.
    Установите ``ORCHX_NO_TUI=1`` или ``NO_COLOR=1`` для plain-режима.
    Установите ``-v/--verbose`` для подробного DEBUG-лога.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import subprocess
import sys
from pathlib import Path

from . import orchestrator, paths, pr, runner, tui, worktree
from .agent.llm import LLMClient, LLMConfig


def _detect_repo_root() -> Path:
    """Детектируем корень репозитория через ``git rev-parse --show-toplevel``."""
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--show-toplevel"], text=True
        ).strip()
    except subprocess.CalledProcessError as e:
        raise SystemExit(f"Not a git repository: {e}") from e
    return Path(out)


_VERBOSE: bool = False
_FILE_HANDLER: logging.FileHandler | None = None


def _make_file_handler(path: Path, verbose: bool) -> logging.FileHandler:
    """Создать ``FileHandler`` с привычным форматированием."""
    path.parent.mkdir(parents=True, exist_ok=True)
    h = logging.FileHandler(path, mode="a")
    h.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    h.setLevel(logging.DEBUG if verbose else logging.WARNING)
    return h


def _setup_logging(verbose: bool, repo_root: Path) -> None:
    """Конфигурируем root logger.

    Когда работает TUI (live-доска перерисовывается каждые 0.4s), любая
    запись в stderr ломает курсор и порождает «вырвиглазные» промежуточные
    кадры. Поэтому все WARNING/INFO-логи диспетчера направляем в файл.

    На старте мы ещё не знаем ``task_id`` (planner его только генерирует),
    поэтому пишем в ``.orchx/_pending/dispatcher.log``. Как только task_id
    известен — вызывается :func:`_attach_run_log`, который перенаправляет
    последующие записи в ``.orchx/runs/<task_id>/dispatcher.log`` и
    переносит туда уже накопленный pending-лог.

    С ``-v/--verbose`` лог становится подробным (DEBUG) и дублируется в
    stderr — для отладки.
    """
    global _VERBOSE, _FILE_HANDLER
    _VERBOSE = verbose

    file_handler = _make_file_handler(
        paths.pending_dispatcher_log(repo_root), verbose
    )
    _FILE_HANDLER = file_handler

    handlers: list[logging.Handler] = [file_handler]
    stderr_handler = logging.StreamHandler(sys.stderr)
    if verbose:
        # В verbose-режиме TUI-лог в stderr полезен для отладки — пусть будет.
        stderr_handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        )
        stderr_handler.setLevel(logging.DEBUG)
    else:
        # Только ERROR в stderr, чтобы критику не пропустить, но TUI не ломать.
        stderr_handler.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
        stderr_handler.setLevel(logging.ERROR)
    handlers.append(stderr_handler)

    root = logging.getLogger()
    # Сбросим возможные ранее установленные хендлеры (повторные вызовы main()).
    for h in list(root.handlers):
        root.removeHandler(h)
    for h in handlers:
        root.addHandler(h)
    root.setLevel(logging.DEBUG if verbose else logging.WARNING)


def _attach_run_log(repo_root: Path, task_id: str) -> None:
    """Перенаправить root logger на ``.orchx/runs/<task_id>/dispatcher.log``.

    Перенесёт уже записанные строки из ``_pending/dispatcher.log`` в новый
    файл (append), чтобы не потерять контекст начала прогона.
    """
    global _FILE_HANDLER
    pending = paths.pending_dispatcher_log(repo_root)
    target = paths.dispatcher_log_path(repo_root, task_id)
    target.parent.mkdir(parents=True, exist_ok=True)

    # Скопируем накопленный pending-лог в финальный файл (если он есть).
    if pending.exists():
        try:
            existing = target.read_text(encoding="utf-8") if target.exists() else ""
            target.write_text(
                existing + pending.read_text(encoding="utf-8"), encoding="utf-8"
            )
        except OSError:  # pragma: no cover — дисковые ошибки не должны валить прогон
            pass

    new_handler = _make_file_handler(target, _VERBOSE)

    root = logging.getLogger()
    if _FILE_HANDLER is not None:
        root.removeHandler(_FILE_HANDLER)
        try:
            _FILE_HANDLER.close()
        except OSError:
            pass
    root.addHandler(new_handler)
    _FILE_HANDLER = new_handler

    # Удалим pending-файл — он больше не нужен.
    if pending.exists():
        try:
            pending.unlink()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Behavior config from args
# ---------------------------------------------------------------------------


def _orchX_config_from_args(args: argparse.Namespace) -> orchestrator.OrchXConfig:
    """Сконструировать OrchXConfig из CLI-аргументов (с дефолтами для отсутствующих)."""
    return orchestrator.OrchXConfig(
        auto_review=not getattr(args, "no_review", False),
        auto_followup=getattr(args, "auto_followup", False),
        max_followup_depth=getattr(args, "max_followup_depth", 1),
        use_debugger_on_retry=not getattr(args, "no_debugger", False),
        use_merger_on_conflict=not getattr(args, "no_merger", False),
        supervisor_enabled=not getattr(args, "no_supervisor", False),
        supervisor_interval_s=getattr(args, "supervisor_interval_s", 30.0),
        effort=getattr(args, "effort", "high"),
        reviewer_effort=getattr(args, "reviewer_effort", "xhigh"),
        debugger_effort=getattr(args, "debugger_effort", "xhigh"),
        merger_effort=getattr(args, "merger_effort", "high"),
        auto_replan=not getattr(args, "no_replan", False),
        replanner_effort=getattr(args, "replanner_effort", "xhigh"),
        allow_dirty=getattr(args, "allow_dirty", False),
        auto_stash=getattr(args, "auto_stash", False),
    )


def _add_behavior_flags(p: argparse.ArgumentParser) -> None:
    """Добавить общие флаги поведения для run/all."""
    p.add_argument(
        "--no-review",
        action="store_true",
        help="Не запускать orchX-reviewer на финале (по умолчанию запускается).",
    )
    p.add_argument(
        "--auto-followup",
        action="store_true",
        help=(
            "Динамически добавлять задачи из needs_followup worker'ов в DAG. "
            "По умолчанию выключено — нужно явно опт-ин."
        ),
    )
    p.add_argument(
        "--max-followup-depth",
        type=int,
        default=1,
        help="Максимальная глубина каскада followup'ов (anti-loop, default 1).",
    )
    p.add_argument(
        "--no-debugger",
        action="store_true",
        help="На retry использовать оригинального агента, не orchX-debugger.",
    )
    p.add_argument(
        "--no-merger",
        action="store_true",
        help="При merge-конфликте делать abort+fail вместо orchX-merger.",
    )
    p.add_argument(
        "--no-supervisor",
        action="store_true",
        help="Отключить фоновый supervisor (heartbeat + budget enforcement).",
    )
    p.add_argument(
        "--supervisor-interval-s",
        type=float,
        default=30.0,
        help="Период heartbeat'а supervisor'а (по умолчанию 30s).",
    )
    p.add_argument(
        "--effort",
        choices=["minimal", "low", "medium", "high", "xhigh", "max"],
        default="high",
        help=(
            "Reasoning effort для воркеров (мапится в LLM provider-specific). По умолчанию high — "
            "хороший баланс качества и скорости. xhigh для самых сложных задач."
        ),
    )
    p.add_argument(
        "--reviewer-effort",
        choices=["minimal", "low", "medium", "high", "xhigh", "max"],
        default="xhigh",
        help="Effort для orchX-reviewer (по умолчанию xhigh — recall важнее скорости).",
    )
    p.add_argument(
        "--debugger-effort",
        choices=["minimal", "low", "medium", "high", "xhigh", "max"],
        default="xhigh",
        help="Effort для orchX-debugger (по умолчанию xhigh — диагностика требует глубины).",
    )
    p.add_argument(
        "--merger-effort",
        choices=["minimal", "low", "medium", "high", "xhigh", "max"],
        default="high",
        help="Effort для orchX-merger (по умолчанию high).",
    )
    p.add_argument(
        "--no-replan",
        action="store_true",
        help=(
            "Отключить авто-перепланирование. По умолчанию: при провале фазы "
            "(если allow_replan=true и max_replans не исчерпан) диспетчер "
            "вызывает orchX-planner на остаток."
        ),
    )
    p.add_argument(
        "--replanner-effort",
        choices=["minimal", "low", "medium", "high", "xhigh", "max"],
        default="xhigh",
        help="Effort для orchX-planner при replan'е (по умолчанию xhigh).",
    )
    p.add_argument(
        "--auto-stash",
        action="store_true",
        help=(
            "Автоматически сделать `git stash push` для tracked-правок перед "
            "стартом и `git stash pop` после завершения. Удобно, когда у тебя "
            "локальные правки, но коммитить их рано."
        ),
    )
    p.add_argument(
        "--allow-dirty",
        action="store_true",
        help=(
            "UNSAFE: разрешить запуск с грязным workdir. Воркеры будут видеть "
            "committed-версию файлов, а не working tree. Только для отладки."
        ),
    )


# ---------------------------------------------------------------------------
# subcommand: plan
# ---------------------------------------------------------------------------


async def _run_planner_quiet(
    *,
    llm: LLMClient,
    repo_root: Path,
    prompt: str,
    log_path: Path,
    spinner: tui.Spinner,
    effort: str = "xhigh",
) -> int:
    """Запустить in-process planner-воркера с обновлением спиннера.

    Текстовые дельты от LLM и tool-события (через ``on_activity``) короткими
    строчками улетают в ``spinner``, чтобы пользователь видел, что
    planner живой и что-то делает. Полный transcript идёт в ``log_path``.
    """

    def _on_activity(line: str) -> None:
        cleaned = re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]", "", line).strip()
        if not cleaned:
            return
        # Многострочные дельты обрезаем до первой строки.
        if "\n" in cleaned:
            cleaned = cleaned.split("\n", 1)[0]
        if not cleaned:
            return
        spinner.update(cleaned[:120])

    outcome = await runner.run_worker(
        llm=llm,
        cwd=repo_root,
        repo_root=repo_root,
        role="planner",
        prompt=prompt,
        timeout_s=1800,
        log_file=log_path,
        effort=effort,
        on_activity=_on_activity,
    )
    if outcome.timed_out:
        return 124
    return outcome.returncode


async def _cmd_plan(args: argparse.Namespace) -> int:
    """Запустить orchX-planner и записать план в ``.orchx/runs/<task_id>/plan.json``.

    Поскольку до запуска planner'а task_id ещё неизвестен, planner пишет
    промежуточный план в ``.orchx/_pending/plan.json``. После успешного
    планирования cli читает task_id из плана и перемещает всё в
    ``.orchx/runs/<task_id>/`` (полностью затирая старую папку с тем же
    task_id, если она была).
    """
    repo_root = _detect_repo_root()
    try:
        llm = LLMClient(LLMConfig.from_env())
    except RuntimeError as e:
        tui.print_error(str(e))
        return 2

    pending_plan = paths.pending_plan_path(repo_root)
    pending_log = paths.pending_planner_log(repo_root)
    pending_plan.parent.mkdir(parents=True, exist_ok=True)

    # Чистим staging от прошлых попыток — он временный.
    if pending_plan.exists():
        pending_plan.unlink()
    if pending_log.exists():
        pending_log.unlink()

    prompt = (
        f"User task:\n\n{args.task}\n\n"
        "Build an orchX plan and write it to .orchx/_pending/plan.json. "
        "Follow the planner agent rules strictly."
    )

    tui.banner("orchX · planning", args.task[:80])
    spinner = tui.Spinner("running orchX-planner")
    async with spinner:
        rc = await _run_planner_quiet(
            llm=llm,
            repo_root=repo_root,
            prompt=prompt,
            log_path=pending_log,
            spinner=spinner,
        )

    if rc != 0:
        tui.print_error(f"orchX-planner exited with code {rc}")
        tui.print_dim(f"  full log: {pending_log}")
        return rc
    if not pending_plan.exists():
        tui.print_error(
            "Planner finished but did not write .orchx/_pending/plan.json."
        )
        tui.print_dim(f"  full log: {pending_log}")
        return 1

    # Прочитаем task_id и переместим план в финальную папку.
    try:
        plan_data = json.loads(pending_plan.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        tui.print_error(f"Planner wrote invalid JSON: {e}")
        tui.print_dim(f"  full log: {pending_log}")
        return 1
    task_id = plan_data.get("task_id")
    if not isinstance(task_id, str) or not task_id:
        tui.print_error("Planner output missing required `task_id` field.")
        return 1

    final_run_dir = paths.run_dir(repo_root, task_id)
    if final_run_dir.exists() and not args.force:
        tui.print_error(
            f"Run dir already exists at {final_run_dir}. "
            "Use --force to wipe it and start fresh."
        )
        return 2

    # Полностью убираем старый прогон (включая worktrees), как договорились —
    # повторный запуск того же task_id начинается с чистого листа.
    if final_run_dir.exists():
        await _wipe_run_dir(repo_root, task_id, final_run_dir)

    final_run_dir.mkdir(parents=True, exist_ok=True)
    final_plan = paths.plan_path(repo_root, task_id)
    final_planner_log = paths.planner_log_path(repo_root, task_id)
    pending_plan.replace(final_plan)
    if pending_log.exists():
        pending_log.replace(final_planner_log)
    paths.cleanup_pending(repo_root)

    # Переключим dispatcher logger на финальный файл.
    _attach_run_log(repo_root, task_id)

    n_phases = len(plan_data.get("phases") or [])
    n_tasks = len(plan_data.get("tasks") or []) or sum(
        len(ph.get("tasks") or []) for ph in (plan_data.get("phases") or [])
    )
    tui.print_done(
        "plan written",
        f"{n_phases or 1} phase(s) · {n_tasks} task(s) · {final_plan}",
    )
    return 0


async def _wipe_run_dir(repo_root: Path, task_id: str, run_dir: Path) -> None:
    """Полностью снести предыдущий прогон того же task_id.

    Это включает:
    - все worktree-ы внутри ``run_dir/worktrees/`` (через ``git worktree remove``),
    - все ветки роя для этого task_id (`orchX/`, `orchX-tasks/`, `orchX-review/`),
    - саму папку ``run_dir``.

    Делает повторный запуск идемпотентным.
    """
    import shutil

    # Снять все worktree'ы (если есть). git worktree remove корректно
    # обрабатывает grязные/исчезнувшие — мы просто игнорируем ошибки.
    wts = paths.worktrees_dir(repo_root, task_id)
    if wts.exists():
        for child in wts.iterdir():
            if child.is_dir():
                try:
                    await worktree.remove_worktree(repo_root, child)
                except Exception:  # noqa: BLE001
                    pass

    # Удалить ветки роя.
    for branch in (
        f"orchX/{task_id}",
        f"orchX-review/{task_id}",
    ):
        try:
            await worktree.delete_branch(repo_root, branch)
        except Exception:  # noqa: BLE001
            pass
    # Дочерние ветки воркеров — пройдёмся по списку (имена нам ещё неизвестны
    # без plan.json, но git справится сам через шаблон). Используем worktree.git.
    try:
        await worktree._git(  # type: ignore[attr-defined]
            repo_root, "branch", "-D", *await _list_worker_branches(repo_root, task_id)
        )
    except Exception:  # noqa: BLE001
        pass

    # И наконец сама папка.
    if run_dir.exists():
        shutil.rmtree(run_dir, ignore_errors=True)


async def _list_worker_branches(repo_root: Path, task_id: str) -> list[str]:
    """Найти ветки воркеров `orchX-tasks/<task_id>/*` (для batch-удаления)."""
    proc = await asyncio.create_subprocess_exec(
        "git",
        "branch",
        "--list",
        f"orchX-tasks/{task_id}/*",
        cwd=str(repo_root),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, _ = await proc.communicate()
    return [
        ln.strip().lstrip("* ")
        for ln in out.decode("utf-8", errors="replace").splitlines()
        if ln.strip()
    ]


# ---------------------------------------------------------------------------
# subcommand: run
# ---------------------------------------------------------------------------


def _resolve_plan_path(repo_root: Path, explicit: str | None) -> Path | None:
    """Найти plan.json для запуска.

    Порядок:

    1. Явный путь, если задан (резолвим как абсолютный).
    2. Самый свежий ``.orchx/runs/<task_id>/plan.json`` по времени модификации.
    3. Legacy ``.orchx/plan.json`` (для совместимости со старыми скриптами).

    Возвращает ``None``, если не нашли ничего.
    """
    if explicit:
        return Path(explicit).resolve()

    runs = paths.runs_dir(repo_root)
    if runs.exists():
        candidates = sorted(
            (p for p in runs.glob("*/plan.json") if p.is_file()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if candidates:
            return candidates[0]

    legacy = paths.orchx_root(repo_root) / "plan.json"
    if legacy.exists():
        return legacy

    return None


async def _cmd_run(args: argparse.Namespace) -> int:
    """Прогнать рой по существующему plan.json и всегда открыть PR.

    PR создаётся всегда — даже если часть задач упала или reviewer нашёл
    блокирующие проблемы. Решение мержить или нет принимает человек на
    стороне GitHub. Это упрощает поведение и убирает «тихие» успешные
    прогоны без артефакта для ревью.
    """
    repo_root = _detect_repo_root()
    plan_path = _resolve_plan_path(repo_root, args.plan_path)
    if plan_path is None or not plan_path.exists():
        tui.print_error(
            "plan not found. Run `orchx plan \"<task>\"` first or pass an "
            "explicit path: `orchx run path/to/plan.json`."
        )
        return 2
    config = _orchX_config_from_args(args)

    # Подгрузим план только для красивого intro-баннера; настоящая загрузка
    # происходит в orchestrator.run_orchX.
    try:
        peek = json.loads(plan_path.read_text(encoding="utf-8"))
        task_id = peek.get("task_id", "orchX")
        base_branch = peek.get("base_branch", "main")
        n_phases = max(1, len(peek.get("phases") or []) or 1)
        n_tasks = len(peek.get("tasks") or []) or sum(
            len(ph.get("tasks") or []) for ph in (peek.get("phases") or [])
        )
        tui.print_intro(task_id, base_branch, n_phases, n_tasks)
        # Переключаем dispatcher logger на runs/<task_id>/dispatcher.log,
        # чтобы все логи прогона лежали в одной папке.
        if isinstance(task_id, str) and task_id and task_id != "orchX":
            _attach_run_log(repo_root, task_id)
    except Exception:  # noqa: BLE001
        pass

    # Инициализация контекста (cleanup worktrees, integration branch, worktree)
    # занимает заметное время, поэтому держим spinner до момента, когда
    # LiveBoard будет готова (то есть когда orchestrator вызовет on_ctx_ready).
    # Затем гасим spinner и стартуем board, а после завершения роя — board
    # тоже гасим (даже если упал на этапе init).
    tui.print_step("Starting orchX")
    init_spinner = tui.Spinner("preparing integration branch & worktrees")
    init_spinner_task = asyncio.create_task(init_spinner._run())
    init_spinner._task = init_spinner_task

    board_ref: dict[str, tui.LiveBoard] = {}

    def _on_init_progress(stage: str) -> None:
        # Обновляем подзаголовок спиннера, чтобы пользователь видел
        # текущий шаг (cleaning, creating integration branch, и т.п.).
        init_spinner.update(stage)

    def _on_ctx(ctx) -> None:
        # Останавливаем init-spinner, запускаем live-доску.
        init_spinner.stop()
        board = tui.LiveBoard(ctx)
        board.start()
        board_ref["board"] = board

    try:
        try:
            summary = await orchestrator.run_orchX(
                repo_root,
                plan_path,
                config,
                on_ctx_ready=_on_ctx,
                on_init_progress=_on_init_progress,
            )
        except worktree.DirtyWorkingTreeError as e:
            # Грязный workdir — пользовательская ошибка, не баг кода. Показываем
            # человекочитаемое сообщение без traceback'а.
            init_spinner.stop()
            try:
                await init_spinner_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            tui.print_error("Cannot start orchX: working tree has uncommitted changes")
            tui.out("")
            for line in str(e).splitlines():
                tui.out("  " + line)
            return 2
    finally:
        # На всякий случай — если on_ctx_ready не успел вызваться (ранний сбой),
        # всё равно гасим spinner, чтобы курсор вернулся на место.
        init_spinner.stop()
        try:
            await init_spinner_task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        board = board_ref.get("board")
        if board is not None:
            await board.stop()

    # Полный JSON-дамп — в файл, не в терминал.
    summary_path = paths.summary_path(repo_root, summary["task_id"])
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(
        json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    tui.print_run_summary(summary)
    tui.print_dim(f"  full summary: {summary_path}")
    tui.print_dim(f"  full log:     {summary.get('log_file', '')}")

    body = pr.render_pr_body(summary)
    title_prefix = "orchX"
    failed = summary["counts"].get("failed", 0)
    skipped = summary["counts"].get("skipped", 0)
    success = summary["counts"].get("success", 0)
    review = summary.get("review")
    halted = bool(summary.get("halt_reason") or summary.get("abort_reason"))
    if halted and success == 0:
        # Самый плохой случай: рой остановился без единой смерженной задачи.
        # Чтобы PR не выглядел как «всё ок» — явно маркируем.
        title_prefix = "orchX[halted-empty]"
    elif halted:
        title_prefix = "orchX[halted]"
    elif failed > 0 or skipped > 0:
        title_prefix = "orchX[partial]"
    elif review and review.get("status") == "failed":
        title_prefix = "orchX[review-blocked]"
    title = f"{title_prefix}: {summary['task_id']}"

    tui.print_step("Pushing integration branch and opening PR")
    async with tui.Spinner("pushing + gh pr create"):
        pr_result = await pr.push_and_open_pr(
            repo_root=repo_root,
            integration_worktree=Path(summary["integration_worktree"]),
            integration_branch=summary["integration_branch"],
            base_branch=summary["base_branch"],
            title=title,
            body=body,
        )
    tui.print_pr_result(pr_result)
    if pr_result.get("error"):
        return 1
    return 0 if failed == 0 else 1


# ---------------------------------------------------------------------------
# subcommand: all
# ---------------------------------------------------------------------------


async def _cmd_all(args: argparse.Namespace) -> int:
    """Plan + run + PR одной командой."""
    plan_args = argparse.Namespace(task=args.task, force=True)
    rc = await _cmd_plan(plan_args)
    if rc != 0:
        return rc
    # Перебрасываем все behavior-флаги из all в run.
    run_args = argparse.Namespace(
        plan_path=None,
        no_review=args.no_review,
        auto_followup=args.auto_followup,
        max_followup_depth=args.max_followup_depth,
        no_debugger=args.no_debugger,
        no_merger=args.no_merger,
        no_supervisor=args.no_supervisor,
        supervisor_interval_s=args.supervisor_interval_s,
        effort=args.effort,
        reviewer_effort=args.reviewer_effort,
        debugger_effort=args.debugger_effort,
        merger_effort=args.merger_effort,
        no_replan=args.no_replan,
        replanner_effort=args.replanner_effort,
        auto_stash=args.auto_stash,
        allow_dirty=args.allow_dirty,
    )
    return await _cmd_run(run_args)


# ---------------------------------------------------------------------------
# argparse
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    """Собираем argparse-парсер для CLI."""
    p = argparse.ArgumentParser(
        prog="orchX",
        description="Параллельный мультиагентный рой orchX.",
    )
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    plan_p = sub.add_parser("plan", help="Сгенерировать plan.json через orchX-planner")
    plan_p.add_argument("task", help="Свободное описание задачи для роя")
    plan_p.add_argument(
        "--force", action="store_true", help="Перезаписать существующий plan.json"
    )

    run_p = sub.add_parser(
        "run",
        help="Прогнать рой по plan.json и открыть PR",
    )
    run_p.add_argument(
        "plan_path",
        nargs="?",
        default=None,
        help=(
            "Путь к plan.json. По умолчанию — самый свежий "
            ".orchx/runs/<task_id>/plan.json (или legacy .orchx/plan.json)."
        ),
    )
    _add_behavior_flags(run_p)

    all_p = sub.add_parser(
        "all",
        help="plan + run + PR одной командой",
    )
    all_p.add_argument("task", help="Свободное описание задачи для роя")
    _add_behavior_flags(all_p)

    return p


def main(argv: list[str] | None = None) -> int:
    """Entrypoint console-команды ``orchx`` (или ``python -m orchx``)."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    repo_root = _detect_repo_root()
    _setup_logging(args.verbose, repo_root)
    handler = {
        "plan": _cmd_plan,
        "run": _cmd_run,
        "all": _cmd_all,
    }[args.cmd]
    try:
        return asyncio.run(handler(args))
    except KeyboardInterrupt:
        tui.print_error("interrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
