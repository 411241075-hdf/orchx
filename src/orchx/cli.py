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
import os
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


def _detect_default_branch(repo_root: Path) -> str:
    """Угадать дефолтную ветку репо.

    Порядок попыток:
    1. ``git symbolic-ref refs/remotes/origin/HEAD`` (если есть upstream).
    2. ``main`` если такая локальная ветка есть.
    3. ``master`` если такая локальная ветка есть.
    4. Текущая ветка (``git rev-parse --abbrev-ref HEAD``).
    5. ``main`` как последний fallback.
    """
    def _git(*args: str) -> str | None:
        try:
            return subprocess.check_output(
                ["git", *args], cwd=str(repo_root), text=True,
                stderr=subprocess.DEVNULL,
            ).strip() or None
        except (subprocess.CalledProcessError, FileNotFoundError):
            return None

    # 1. origin/HEAD → ``origin/main``
    sym = _git("symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD")
    if sym and sym.startswith("origin/"):
        return sym.split("/", 1)[1]

    # 2-3. main / master
    branches = (_git("branch", "--list", "main", "master") or "").splitlines()
    names = {b.strip(" *+").strip() for b in branches if b.strip()}
    if "main" in names:
        return "main"
    if "master" in names:
        return "master"

    # 4. Текущая ветка
    cur = _git("rev-parse", "--abbrev-ref", "HEAD")
    if cur and cur != "HEAD":
        return cur

    return "main"


def _parse_dotenv_file(env_path: Path) -> dict[str, str]:
    """Минимальный парсер ``.env`` без внешних зависимостей.

    Поддерживает: ``KEY=value``, ``export KEY=value``, инлайн-комментарии
    после непустого значения, кавычки (одинарные/двойные) вокруг значения,
    пустые строки и комментарии (``#``). Этого достаточно для формата
    ``orchx/.env``; если в проекте появятся сложные dotenv-фичи (multiline,
    подстановка ``${VAR}``), стоит зависеть на ``python-dotenv`` явно.
    """
    out: dict[str, str] = {}
    try:
        text = env_path.read_text(encoding="utf-8")
    except OSError:
        return out
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key or not key.replace("_", "").isalnum():
            continue
        value = value.strip()
        # Снять окружающие кавычки одного типа.
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        else:
            # Обрезать инлайн-комментарий после пробела (только для unquoted).
            hash_idx = value.find(" #")
            if hash_idx != -1:
                value = value[:hash_idx].rstrip()
        out[key] = value
    return out


def _load_orchx_env(repo_root: Path) -> None:
    """Автозагрузка ``orchx/.env`` без обязательной зависимости от python-dotenv.

    Удобно, чтобы пользователь не делал ``source orchx/.env`` руками каждый
    раз. Непустые переменные окружения, выставленные в shell, имеют
    приоритет над файлом (явный экспорт побеждает). Пустые строки в shell
    трактуются как «не задано» и перезаписываются значениями из файла —
    иначе пустой ``ORCHX_LLM_BASE_URL=``, оставленный в активационном
    скрипте conda/direnv, маскирует настройки и приводит к ложному
    ``missing required env vars``.

    Раньше эта функция требовала ``python-dotenv`` и тихо возвращалась без
    загрузки, если пакета нет в venv. Это давало contra-intuitive поведение:
    ``orchx/.env`` лежит на диске и заполнен, но рой падает с
    ``missing required env vars``. Теперь используем встроенный парсер
    (``_parse_dotenv_file``) — никакой внешней зависимости.
    """
    # Новая раскладка: <project>/.orchx/.env. Раньше (до выделения orchx
    # в отдельный пакет) использовалась открытая папка orchx/.env — её
    # тоже подхватываем как fallback, чтобы апдейт не сломал старые проекты.
    env_path_new = repo_root / ".orchx" / ".env"
    env_path_legacy = repo_root / "orchx" / ".env"
    env_path = env_path_new if env_path_new.exists() else env_path_legacy
    if not env_path.exists():
        return
    file_vars = _parse_dotenv_file(env_path)
    for key, file_val in file_vars.items():
        if not file_val:
            continue
        shell_val = os.environ.get(key)
        if shell_val is None or shell_val.strip() == "":
            os.environ[key] = file_val


_VERBOSE: bool = False
_FILE_HANDLER: logging.FileHandler | None = None


def _make_file_handler(path: Path, verbose: bool) -> logging.FileHandler:
    """Создать ``FileHandler`` с привычным форматированием."""
    path.parent.mkdir(parents=True, exist_ok=True)
    h = logging.FileHandler(path, mode="a")
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    h.setLevel(logging.DEBUG if verbose else logging.WARNING)
    return h


def _setup_logging(verbose: bool, repo_root: Path) -> None:
    """Конфигурируем root logger.

    Когда работает TUI (live-доска перерисовывается каждые 0.4s), любая
    запись в stderr ломает курсор и порождает «вырвиглазные» промежуточные
    кадры. Поэтому все WARNING/INFO-логи диспетчера направляем в файл.

    На старте мы ещё не знаем ``task_id`` (planner его только генерирует),
    поэтому пишем в ``orchx/_pending/dispatcher.log``. Как только task_id
    известен — вызывается :func:`_attach_run_log`, который перенаправляет
    последующие записи в ``orchx/runs/<task_id>/dispatcher.log`` и
    переносит туда уже накопленный pending-лог.

    С ``-v/--verbose`` лог становится подробным (DEBUG) и дублируется в
    stderr — для отладки.
    """
    global _VERBOSE, _FILE_HANDLER
    _VERBOSE = verbose

    file_handler = _make_file_handler(paths.pending_dispatcher_log(repo_root), verbose)
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
    """Перенаправить root logger на ``orchx/runs/<task_id>/dispatcher.log``.

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
        debugger_effort=getattr(args, "debugger_effort", "high"),
        merger_effort=getattr(args, "merger_effort", "high"),
        auto_replan=not getattr(args, "no_replan", False),
        replanner_effort=getattr(args, "replanner_effort", "xhigh"),
        allow_dirty=getattr(args, "allow_dirty", False),
        auto_stash=getattr(args, "auto_stash", False),
        per_task_review=getattr(args, "per_task_review", False),
        per_task_review_effort=getattr(args, "per_task_review_effort", "medium"),
        cleanup_worktrees_after_merge=getattr(args, "cleanup_worktrees", False),
        max_cost_usd=getattr(args, "max_cost_usd", None),
        auto_fixup_chain=not getattr(args, "no_auto_fixup", False),
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
        default="high",
        help=(
            "Effort для orchX-debugger (по умолчанию high). Половина "
            "retry-кейсов — это lost-edits / merge-cleanup, где xhigh "
            "неоправдан. Поднимай до xhigh для content-failure после "
            "нескольких неуспешных retry'ев."
        ),
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
    p.add_argument(
        "--per-task-review",
        action="store_true",
        help=(
            "Запускать lightweight orchX-reviewer на дифф каждой задачи "
            "перед её merge'ем в integration ветку. Любые blocking findings "
            "отправляют задачу на retry через debugger. Полезно для крупных "
            "задач — ловит bugs до того, как они накопятся."
        ),
    )
    p.add_argument(
        "--per-task-review-effort",
        choices=["minimal", "low", "medium", "high", "xhigh", "max"],
        default="medium",
        help="Effort для pre-merge reviewer'а (по умолчанию medium).",
    )
    # P2.1: cleanup worktrees after merge.
    p.add_argument(
        "--cleanup-worktrees",
        action="store_true",
        help=(
            "P2.1: после успешного merge задачи в integration ветку удалять "
            "её worktree. Экономит диск на больших прогонах; для debug удобнее "
            "оставлять (default off)."
        ),
    )
    # P1.3: cost budget.
    p.add_argument(
        "--max-cost-usd",
        type=float,
        default=None,
        help=(
            "P1.3: жёсткий budget в USD на весь прогон. Supervisor abort'ит "
            "рой при превышении. По умолчанию unlimited."
        ),
    )
    # P1.8: auto-fixup chain.
    p.add_argument(
        "--no-auto-fixup",
        action="store_true",
        help=(
            "P1.8: НЕ конвертировать blocking review findings в follow-up "
            "debugger-задачи (по умолчанию конвертирует)."
        ),
    )
    p.add_argument(
        "--resume",
        action="store_true",
        help=(
            "Продолжить незавершённый прогон того же task_id вместо того, "
            "чтобы стереть `orchx/runs/<task_id>/` и начать с нуля. "
            "Уже выполненные задачи (с success-result.json в интеграционной "
            "ветке) пропускаются. Полезно для длинных runs, упавших по "
            "infra-причине (ctrl+c, провал хост-машины, временный 5xx)."
        ),
    )
    p.add_argument(
        "--tracker-task",
        default=None,
        metavar="ID",
        help=(
            "Composite id задачи во внешнем трекере (например, GitHub "
            "Projects: 'PVTI_lAHO...:114'). Записывается в "
            "plan.tracker_task_id и используется orchestrator'ом для "
            "автоматического update_status (двинуть карточку в Done / "
            "оставить коммент в issue со ссылкой на PR) по завершении "
            "прогона. Также читается из env ORCHX_TRACKER_TASK_ID."
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


def _resolve_tracker_task_id(args: argparse.Namespace) -> str:
    """Вернуть composite tracker task id из CLI или env, если задан.

    Приоритет: явный CLI-флаг ``--tracker-task`` > env ``ORCHX_TRACKER_TASK_ID``.
    Возвращает пустую строку, если ни там, ни там не задано.

    Composite id (например, GitHub Projects ``"PVTI_lAHO...:114"``) не годится
    как ``plan.task_id`` (там в slug-имени бранчей запрещено двоеточие),
    поэтому хранится отдельно — в ``plan.tracker_task_id``.
    """
    cli_val = getattr(args, "tracker_task", None)
    if cli_val:
        return str(cli_val).strip()
    env_val = os.environ.get("ORCHX_TRACKER_TASK_ID", "").strip()
    return env_val


async def _build_planner_memory_context(repo_root: Path, task_text: str) -> str:
    """Собрать relevant'ный context из memory.db для подмешивания в planner prompt.

    Стратегия:

    - Достаём ``plans`` namespace по recall(query=user-task) — топ-3 похожих
      прошлых планов; даём их task_id + summary + counts.
    - ``task_archive`` namespace — топ-5 семантически похожих subtask'ов
      (notes описывают «что работало» в этом проекте).
    - ``known_pitfalls`` — топ-5 известных подводных камней для этого репо.
    - ``code_locations`` — выдержка для упомянутых в task_text имён символов.

    Если memory plugin не настроен (или ничего не найдено), возвращаем "".
    """
    # Lazy-import: не тащим plugins при простых subcommand'ах.
    try:
        from .plugins import registry as _registry
    except Exception:  # noqa: BLE001
        return ""
    try:
        plugins = _registry.load_from_config(
            repo_root / ".orchx" / "config.yaml", repo_root=repo_root
        )
    except Exception:  # noqa: BLE001
        return ""
    memory = plugins.get("memory")
    if memory is None or memory.__class__.__name__ == "NoopMemory":
        return ""
    sections: list[str] = []
    try:
        plans = await memory.recall("plans", task_text, k=3)
    except Exception:  # noqa: BLE001
        plans = []
    if plans:
        items = []
        for p in plans:
            v = p.get("value", {})
            counts = v.get("counts", {})
            items.append(
                f"- **{v.get('task_id', '?')}** — {v.get('summary', '')[:200]} "
                f"(counts={counts}, wall={v.get('wall_seconds', 0):.0f}s)"
            )
        if items:
            sections.append(
                "### Похожие прошлые планы (из memory.db)\n\n"
                "Учитывай их структуру и фазирование при декомпозиции:\n\n"
                + "\n".join(items)
            )
    try:
        archives = await memory.recall("task_archive", task_text, k=5)
    except Exception:  # noqa: BLE001
        archives = []
    if archives:
        items = []
        for a in archives:
            v = a.get("value", {})
            items.append(
                f"- **{v.get('subtask_id', '?')}** ({v.get('agent', '?')}) — "
                f"{v.get('goal', '')[:140]} → "
                f"changed: {', '.join((v.get('files_changed') or [])[:5])}\n"
                f"  notes: {v.get('notes', '')[:300]}"
            )
        if items:
            sections.append(
                "### Релевантные прошлые subtask'и (notes воркеров)\n\n"
                "Это что воркеры РЕАЛЬНО делали в похожих задачах в этом проекте — "
                "учитывай при формировании file_scope и acceptance:\n\n"
                + "\n".join(items)
            )
    try:
        pitfalls = await memory.recall("known_pitfalls", task_text, k=5)
    except Exception:  # noqa: BLE001
        pitfalls = []
    if pitfalls:
        items = []
        for p in pitfalls:
            v = p.get("value", {})
            items.append(
                f"- **{v.get('subtask_id', '?')}** ({v.get('agent', '?')}) — "
                f"{v.get('failure_reason', '')[:200]}"
            )
        if items:
            sections.append(
                "### Известные подводные камни (из прошлых провалов)\n\n"
                "Эти задачи проваливались раньше — учитывай при планировании, "
                "не повторяй ту же декомпозицию или формулировку:\n\n"
                + "\n".join(items)
            )
    if not sections:
        return ""
    return (
        "## Memory recall (knowledge from past orchX runs in this repo)\n\n"
        "Используй эти факты как context'ный prior, но не как догму — "
        "если задача семантически другая, игнорируй.\n\n"
        + "\n\n".join(sections)
    )


async def _cmd_plan(args: argparse.Namespace) -> int:
    """Запустить orchX-planner и записать план в ``orchx/runs/<task_id>/plan.json``.

    Поскольку до запуска planner'а task_id ещё неизвестен, planner пишет
    промежуточный план в ``orchx/_pending/plan.json``. После успешного
    планирования cli читает task_id из плана и перемещает всё в
    ``orchx/runs/<task_id>/`` (полностью затирая старую папку с тем же
    task_id, если она была).

    Если задан ``--tracker-task <composite_id>`` (или env
    ``ORCHX_TRACKER_TASK_ID``), CLI после успешного планирования
    впишет это значение в ``plan.tracker_task_id`` — так orchestrator
    сможет двинуть карточку в трекере по композитному id, не полагаясь
    на то, что planner LLM сам угадает формат.
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

    # Подмешиваем relevant'ные факты из memory.db (предыдущие планы,
    # code_locations, known_pitfalls) в prompt планировщика. Это даёт
    # эффект «жить между задачами»: когда planner стартует на похожей
    # теме, он видит уже накопленные факты вместо холодного reset'а.
    memory_context = await _build_planner_memory_context(repo_root, args.task)

    prompt = (
        f"User task:\n\n{args.task}\n\n"
        "Build an orchX plan and write it to .orchx/_pending/plan.json. "
        "Follow the planner agent rules strictly."
    )
    if memory_context:
        prompt += "\n\n" + memory_context

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
        tui.print_error("Planner finished but did not write .orchx/_pending/plan.json.")
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

    # Инжектим composite tracker id (если задан CLI-флагом / env) — так
    # orchestrator сможет двинуть карточку, не надеясь, что planner сам
    # угадал формат. Делаем это до wipe/move, чтобы поле попало в финальный
    # plan.json.
    tracker_task_id = _resolve_tracker_task_id(args)
    if tracker_task_id:
        existing = plan_data.get("tracker_task_id") or ""
        if existing and existing != tracker_task_id:
            tui.print_dim(
                f"  overriding plan.tracker_task_id ({existing!r}) "
                f"with CLI/env value ({tracker_task_id!r})"
            )
        plan_data["tracker_task_id"] = tracker_task_id
        pending_plan.write_text(
            json.dumps(plan_data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    # Автокоррекция base_branch: если планировщик угадал ветку, которой
    # нет в этом репо — заменим на реальную дефолтную (origin/HEAD → main →
    # master → текущая). Это типичная история, когда LLM пишет "main", а
    # репо на "master".
    declared_base = plan_data.get("base_branch") or "main"
    try:
        subprocess.check_output(
            ["git", "rev-parse", "--verify", "--quiet", str(declared_base)],
            cwd=str(repo_root), stderr=subprocess.DEVNULL,
        )
    except subprocess.CalledProcessError:
        detected = _detect_default_branch(repo_root)
        if detected != declared_base:
            tui.print_dim(
                f"  base_branch '{declared_base}' not found in repo, "
                f"using detected default '{detected}'"
            )
            plan_data["base_branch"] = detected
            # Перепишем pending_plan чтобы дальше всё работало.
            pending_plan.write_text(
                json.dumps(plan_data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

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
        await worktree._git(
            "branch",
            "-D",
            *await _list_worker_branches(repo_root, task_id),
            cwd=repo_root,
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
    2. Самый свежий ``orchx/runs/<task_id>/plan.json`` по времени модификации.
    3. Legacy ``orchx/plan.json`` (для совместимости со старыми скриптами).

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
            'plan not found. Run `orchx plan "<task>"` first or pass an '
            "explicit path: `orchx run path/to/plan.json`."
        )
        return 2
    # Fail-fast: проверим обязательные env до того, как стартуем spinner и
    # cleanup worktrees. Если ORCHX_LLM_BASE_URL/API_KEY/MODEL не заданы —
    # все равно дальше не пойдём, лучше сказать сейчас.
    try:
        LLMConfig.from_env()
    except RuntimeError as e:
        tui.print_error(str(e))
        return 2
    config = _orchX_config_from_args(args)

    # Если ``--tracker-task`` (или env) задан и в plan.json ещё нет этого
    # поля — впишем сейчас, чтобы orchestrator подхватил его и двинул
    # карточку по завершении. Это полезно когда plan.json сгенерирован
    # отдельно (``orchx plan ...``), а потом запускается ``orchx run`` с
    # tracker'ом.
    tracker_task_override = _resolve_tracker_task_id(args)
    if tracker_task_override:
        try:
            _peek_for_tracker = json.loads(plan_path.read_text(encoding="utf-8"))
            if (_peek_for_tracker.get("tracker_task_id") or "") != tracker_task_override:
                _peek_for_tracker["tracker_task_id"] = tracker_task_override
                plan_path.write_text(
                    json.dumps(_peek_for_tracker, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
        except (OSError, json.JSONDecodeError) as e:
            tui.print_dim(
                f"  could not inject --tracker-task into {plan_path.name}: {e}"
            )

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

    # P0.2 / P1.5: загрузка плагинов из .orchx/config.yaml.
    from .plugins import load_from_config

    plugins_bag = load_from_config(
        repo_root / ".orchx" / "config.yaml",
        repo_root=repo_root,
    )

    try:
        try:
            summary = await orchestrator.run_orchX(
                repo_root,
                plan_path,
                config,
                on_ctx_ready=_on_ctx,
                on_init_progress=_on_init_progress,
                resume=getattr(args, "resume", False),
                plugins=plugins_bag,
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
    tracker_task = getattr(args, "tracker_task", None)
    plan_args = argparse.Namespace(
        task=args.task, force=True, tracker_task=tracker_task
    )
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
        per_task_review=getattr(args, "per_task_review", False),
        per_task_review_effort=getattr(args, "per_task_review_effort", "medium"),
        cleanup_worktrees=getattr(args, "cleanup_worktrees", False),
        max_cost_usd=getattr(args, "max_cost_usd", None),
        no_auto_fixup=getattr(args, "no_auto_fixup", False),
        resume=getattr(args, "resume", False),
        tracker_task=tracker_task,
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
    try:
        from orchx import __version__ as _orchx_version
    except ImportError:
        _orchx_version = "unknown"
    p.add_argument(
        "-V",
        "--version",
        action="version",
        version=f"orchX {_orchx_version}",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    plan_p = sub.add_parser("plan", help="Сгенерировать plan.json через orchX-planner")
    plan_p.add_argument("task", help="Свободное описание задачи для роя")
    plan_p.add_argument(
        "--force", action="store_true", help="Перезаписать существующий plan.json"
    )
    plan_p.add_argument(
        "--tracker-task",
        default=None,
        metavar="ID",
        help=(
            "Composite id задачи во внешнем трекере (например, GitHub "
            "Projects: 'PVTI_lAHO...:114'). Запишется в "
            "plan.tracker_task_id для авто-обновления статуса карточки "
            "после прогона. Также читается из env ORCHX_TRACKER_TASK_ID."
        ),
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
            "orchx/runs/<task_id>/plan.json (или legacy orchx/plan.json)."
        ),
    )
    _add_behavior_flags(run_p)

    all_p = sub.add_parser(
        "all",
        help="plan + run + PR одной командой",
    )
    all_p.add_argument("task", help="Свободное описание задачи для роя")
    _add_behavior_flags(all_p)

    list_p = sub.add_parser(
        "list",
        help="Список существующих run'ов (orchx/runs/<task_id>/)",
    )
    list_p.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Сколько последних run'ов показать (по умолчанию 20).",
    )

    init_p = sub.add_parser(
        "init",
        help=(
            "Развернуть .orchx/ в текущем проекте (env.example, PROJECT.md, "
            "дефолтные промпты ролей) и обновить .gitignore."
        ),
    )
    init_p.add_argument(
        "--force",
        action="store_true",
        help=(
            "Перезаписать существующие файлы в .orchx/ (по умолчанию они "
            "сохраняются — повторный init безопасен и просто добавляет "
            "недостающее)."
        ),
    )
    init_p.add_argument(
        "--minimal",
        action="store_true",
        help=(
            "Не копировать дефолтные промпты в .orchx/prompts/. Рой будет "
            "использовать промпты, шиппящиеся с пакетом. Удобно если "
            "кастомизировать роли пока не нужно."
        ),
    )

    watch_p = sub.add_parser(
        "watch",
        help=(
            "P0.4: PR feedback loop — polling CI status / review comments "
            "и автоматическая реакция (debugger на CI failure, implementer "
            "на change-request, notify/merge на approved+green)."
        ),
    )
    watch_p.add_argument(
        "task_id",
        nargs="?",
        default=None,
        help="task_id run'а. Если не задан — берём самый свежий.",
    )
    watch_p.add_argument(
        "--pr-url",
        default=None,
        help=(
            "URL pull-request'а. Если не задан — пытаемся прочитать из "
            "runs/<task_id>/pr.url (создаётся orchx при opening PR)."
        ),
    )
    watch_p.add_argument(
        "--poll-interval",
        type=float,
        default=60.0,
        help="Период опроса GitHub в секундах (default: 60).",
    )
    watch_p.add_argument(
        "--max-wall-hours",
        type=float,
        default=24.0,
        help="Жёсткий timeout watcher'а в часах (default: 24).",
    )
    watch_p.add_argument(
        "--auto-fix-ci",
        action="store_true",
        help="Шорткат: ставит reactions.ci_failed.auto=true.",
    )
    watch_p.add_argument(
        "--auto-merge",
        action="store_true",
        help="Шорткат: ставит reactions.approved_and_green.action=auto-merge.",
    )

    plugins_p = sub.add_parser(
        "plugins",
        help="P0.2: управление plugin-системой (list / info).",
    )
    plugins_sub = plugins_p.add_subparsers(dest="plugins_cmd", required=True)
    plugins_sub.add_parser("list", help="Список всех зарегистрированных plugin'ov.")

    tasks_p = sub.add_parser(
        "tasks",
        help=(
            "Работа с tracker-задачами (GitHub Projects и т.п.): "
            "ready / pick / move."
        ),
    )
    tasks_sub = tasks_p.add_subparsers(dest="tasks_cmd", required=True)
    ready_sp = tasks_sub.add_parser(
        "ready", help="Список задач в колонке 'готово к работе'."
    )
    ready_sp.add_argument(
        "--limit", type=int, default=20, help="Сколько задач вывести (default 20)."
    )
    pick_sp = tasks_sub.add_parser(
        "pick",
        help=(
            "Атомарно забрать следующую задачу из Ready (переместит её в "
            "In Progress) и напечатать тело. С --run сразу запустит "
            "`orchx all` с правильным tracker-id и body issue как prompt."
        ),
    )
    pick_sp.add_argument(
        "--run",
        action="store_true",
        help=(
            "После pick'а сразу запустить `orchx all`: tracker_task_id "
            "будет проставлен в plan.json автоматически, после успешного "
            "PR карточка двинется в Done. Эквивалент:\n"
            '  TID=$(orchx tasks pick) && orchx all --tracker-task "$TID" "$BODY"'
        ),
    )
    # Behavior-флаги для случая --run (чтобы можно было ``orchx tasks pick
    # --run --no-review --effort xhigh`` и т.п.).
    _add_behavior_flags(pick_sp)
    move_sp = tasks_sub.add_parser(
        "move", help="Передвинуть задачу в указанную колонку."
    )
    move_sp.add_argument("task_id", help="Composite task id (см. `orchx tasks ready`).")
    move_sp.add_argument(
        "column", help='Имя целевой колонки (например, "In Progress").'
    )

    dash_p = sub.add_parser(
        "dashboard",
        help=(
            "P1.4: запустить web-dashboard (REST + SSE) на указанном порту. "
            "Требует pip install 'orchx[server]'."
        ),
    )
    dash_p.add_argument("--host", default="127.0.0.1")
    dash_p.add_argument("--port", type=int, default=8421)

    logs_p = sub.add_parser(
        "logs",
        help="Просмотр логов run'а",
    )
    logs_p.add_argument(
        "task_id",
        nargs="?",
        default=None,
        help=("task_id run'а. Если не задан — берём самый свежий по mtime."),
    )
    logs_p.add_argument(
        "--task",
        default=None,
        help="Subtask id. Покажет attempt-логи именно этой задачи.",
    )
    logs_p.add_argument(
        "--tail",
        type=int,
        default=80,
        help="Сколько последних строк показать (по умолчанию 80; 0 — весь файл).",
    )

    return p


async def _cmd_init(args: argparse.Namespace) -> int:
    """Развернуть `.orchx/` в текущем git-проекте.

    Идемпотентно: можно гонять много раз, существующие файлы не теряются
    (если только не передан ``--force``). После успешного init'а рой
    готов к ``orchx all "<task>"`` — нужно только заполнить ``.orchx/.env``
    своими ORCHX_LLM_* переменными.
    """
    from .init_project import init_project

    repo_root = _detect_repo_root()
    report = init_project(
        repo_root,
        force=getattr(args, "force", False),
        minimal=getattr(args, "minimal", False),
    )
    tui.banner("orchX init")
    for ln in report.describe().splitlines():
        tui.out(ln)

    env_dst = report.runtime_dir / ".env"
    env_example = report.runtime_dir / ".env.example"
    if env_example.exists() and not env_dst.exists():
        tui.out("")
        tui.out("Next steps:")
        tui.out(f"  1. cp {env_example.relative_to(repo_root)} "
                f"{env_dst.relative_to(repo_root)}")
        tui.out(f"  2. edit {env_dst.relative_to(repo_root)} — set "
                f"ORCHX_LLM_BASE_URL, ORCHX_LLM_API_KEY, ORCHX_MODEL")
        tui.out("  3. orchx all \"<your task>\"")
    return 0


async def _cmd_list(args: argparse.Namespace) -> int:
    """Показать список run'ов в `.orchx/runs/`."""
    repo_root = _detect_repo_root()
    runs_dir = paths.runs_dir(repo_root)
    if not runs_dir.exists():
        tui.print_dim("(no runs yet)")
        return 0
    candidates = sorted(
        (p for p in runs_dir.iterdir() if p.is_dir()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )[: max(1, args.limit)]
    if not candidates:
        tui.print_dim("(no runs yet)")
        return 0
    tui.banner("orchX runs", f"{len(candidates)} most recent")
    for p in candidates:
        summary = paths.summary_path(repo_root, p.name)
        plan = paths.plan_path(repo_root, p.name)
        info_bits: list[str] = []
        if plan.exists():
            try:
                pl = json.loads(plan.read_text(encoding="utf-8"))
                phases = len(pl.get("phases") or []) or 1
                tasks = len(pl.get("tasks") or []) or sum(
                    len(ph.get("tasks") or []) for ph in (pl.get("phases") or [])
                )
                info_bits.append(f"{phases}ph/{tasks}t")
            except Exception:  # noqa: BLE001
                pass
        if summary.exists():
            try:
                s = json.loads(summary.read_text(encoding="utf-8"))
                c = s.get("counts", {})
                ok = c.get("success", 0)
                fail = c.get("failed", 0)
                skip = c.get("skipped", 0)
                info_bits.append(f"{ok}✓ {fail}✗ {skip}⊘")
            except Exception:  # noqa: BLE001
                pass
        else:
            info_bits.append("(no summary)")
        info = " · ".join(info_bits) if info_bits else "—"
        tui.kv(p.name, info)
    return 0


async def _cmd_logs(args: argparse.Namespace) -> int:
    """Показать логи run'а."""
    repo_root = _detect_repo_root()
    runs_dir = paths.runs_dir(repo_root)
    if not runs_dir.exists():
        tui.print_error("no runs found in orchx/runs/")
        return 1
    if args.task_id:
        run_dir = runs_dir / args.task_id
        if not run_dir.is_dir():
            tui.print_error(f"run {args.task_id!r} not found at {run_dir}")
            return 1
    else:
        candidates = sorted(
            (p for p in runs_dir.iterdir() if p.is_dir()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if not candidates:
            tui.print_error("no runs found")
            return 1
        run_dir = candidates[0]
        tui.print_dim(f"(using latest run: {run_dir.name})")

    if args.task:
        # Покажем все attempt'ы конкретной задачи.
        log_glob = list((run_dir / "logs").glob(f"{args.task}.attempt*.log"))
        if not log_glob:
            tui.print_error(f"no attempt logs for task {args.task!r} in {run_dir}")
            return 1
        log_glob.sort(key=lambda p: p.name)
        for log_file in log_glob:
            tui.banner(log_file.name)
            _print_tail(log_file, args.tail)
        return 0

    # Иначе показываем главный orchx.log.
    main_log = paths.orchx_log_path(repo_root, run_dir.name)
    if main_log.exists():
        tui.banner(main_log.name)
        _print_tail(main_log, args.tail)
    dispatcher_log = paths.dispatcher_log_path(repo_root, run_dir.name)
    if dispatcher_log.exists():
        tui.banner(dispatcher_log.name)
        _print_tail(dispatcher_log, args.tail)
    return 0


def _print_tail(path: Path, n: int) -> None:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as e:
        tui.print_error(f"could not read {path}: {e}")
        return
    if n <= 0 or len(lines) <= n:
        tail = lines
    else:
        tail = lines[-n:]
        tui.print_dim(
            f"(showing last {len(tail)} of {len(lines)} lines; full file: {path})"
        )
    for ln in tail:
        tui.out(ln)


async def _cmd_watch(args: argparse.Namespace) -> int:
    """P0.4: PR feedback loop.

    Опрашивает GitHub PR (через ``GithubSCM`` plugin) и реагирует на:
    CI failures (→ debugger), change-requests (→ implementer),
    approved+green (→ notify/auto-merge).
    """
    repo_root = _detect_repo_root()
    task_id = getattr(args, "task_id", None) or _latest_task_id(repo_root)
    if not task_id:
        tui.print_error("no runs found in orchx/runs/")
        return 2

    pr_url = getattr(args, "pr_url", None)
    if not pr_url:
        pr_file = paths.run_dir(repo_root, task_id) / "pr.url"
        if pr_file.exists():
            pr_url = pr_file.read_text(encoding="utf-8").strip()
    if not pr_url:
        tui.print_error(
            f"no PR url for task {task_id}. Use --pr-url or "
            f"create runs/{task_id}/pr.url with the PR url."
        )
        return 2

    from .plugins import load_from_config, load_plugin
    from .pr_watcher import DEFAULT_REACTIONS, parse_reactions_yaml, watch_pr

    config_path = repo_root / ".orchx" / "config.yaml"
    plugin_bag = load_from_config(config_path, repo_root=repo_root)
    scm = plugin_bag.get("scm") or load_plugin("scm", "github")
    notifiers = plugin_bag.get("notifiers") or []
    notifier = notifiers[0] if notifiers else None

    # Reactions из config + CLI shortcuts.
    reactions = dict(DEFAULT_REACTIONS)
    if config_path.exists():
        import yaml

        try:
            raw_cfg = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
            reactions = parse_reactions_yaml(raw_cfg.get("reactions", {}))
        except yaml.YAMLError:
            pass
    if getattr(args, "auto_fix_ci", False):
        reactions["ci_failed"].auto = True
        reactions["ci_failed"].action = "send-to-debugger"
    if getattr(args, "auto_merge", False):
        reactions["approved_and_green"].action = "auto-merge"

    tui.banner(f"orchX watch {task_id}")
    tui.out(f"PR: {pr_url}")
    tui.out(f"Poll: {args.poll_interval}s   Max wall: {args.max_wall_hours}h")
    tui.out("Reactions:")
    for k, v in reactions.items():
        tui.out(f"  {k}: auto={v.auto} action={v.action} max_retries={v.max_retries}")

    # Watch — это долгий процесс; пользователю нужно видеть прогресс
    # без `-v`. Включаем INFO-handler для ``orchx.pr_watcher`` в stderr.
    _watcher_logger = logging.getLogger("orchx.pr_watcher")
    if not any(
        isinstance(h, logging.StreamHandler) and h.stream is sys.stderr
        for h in _watcher_logger.handlers
    ):
        _ch = logging.StreamHandler(sys.stderr)
        _ch.setLevel(logging.INFO)
        _ch.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        _watcher_logger.addHandler(_ch)
        _watcher_logger.setLevel(logging.INFO)

    # NB: callback'и debug/implementer/auto-merge requires polishing для real
    # orchestrator-respawn. P0.4 даёт инфраструктуру, follow-up tasks
    # (P1.8 auto-fixup chain) подключат их через orchestrator runtime.
    await watch_pr(
        repo_root=repo_root,
        pr_url=pr_url,
        task_id=task_id,
        reactions=reactions,
        scm=scm,
        notifier=notifier,
        on_ci_failed=None,
        on_changes_requested=None,
        on_approved_and_green=None,
        poll_interval_s=float(args.poll_interval),
        max_wall_s=float(args.max_wall_hours) * 3600,
    )
    return 0


async def _cmd_dashboard(args: argparse.Namespace) -> int:
    """P1.4: запустить web-dashboard (FastAPI + SSE) на указанном порту."""
    try:
        from .web.server import serve as _serve
    except ImportError as e:
        tui.print_error(f"orchx[server] not installed: {e}")
        return 2
    repo_root = _detect_repo_root()
    tui.banner(f"orchX dashboard @ http://{args.host}:{args.port}")
    tui.out("Open the URL above in your browser.")
    tui.out("Live SSE events stream at /api/events.")
    await _serve(repo_root=repo_root, host=args.host, port=args.port)
    return 0


async def _cmd_plugins(args: argparse.Namespace) -> int:
    """P0.2: список зарегистрированных plugin'ов по slot'ам."""
    from .plugins import registered_plugins

    plugins = registered_plugins()
    tui.banner("orchX plugins")
    for slot, names in plugins.items():
        tui.out(f"  {slot}:")
        for n in names:
            tui.out(f"    - {n}")
    if not any(plugins.values()):
        tui.out("  (no plugins registered)")
    _ = args
    return 0


async def _cmd_tasks(args: argparse.Namespace) -> int:
    """0.2.1: работа с tracker-задачами (GitHub Projects и т.п.).

    Подкоманды:

    * ``orchx tasks ready`` — список задач в Ready колонке.
    * ``orchx tasks pick``  — атомарно забрать первую задачу из Ready
      (переместит в In Progress) и напечатать её как ``orchx all``-prompt.
    * ``orchx tasks move <id> <column>`` — передвинуть карточку.
    """
    repo_root = _detect_repo_root()
    from .plugins import load_from_config

    plugin_bag = load_from_config(
        repo_root / ".orchx" / "config.yaml", repo_root=repo_root
    )
    tracker = plugin_bag.get("tracker")
    if tracker is None:
        tui.print_error(
            "tracker plugin is not configured. Add `tracker: github-projects` "
            "(or another tracker) to .orchx/config.yaml."
        )
        return 2

    sub = getattr(args, "tasks_cmd", None)
    if sub == "ready":
        if not hasattr(tracker, "list_ready_tasks"):
            tui.print_error(
                f"tracker {type(tracker).__name__} does not support Kanban API "
                "(list_ready_tasks)."
            )
            return 2
        try:
            tasks = await tracker.list_ready_tasks(limit=args.limit)
        except Exception as e:  # noqa: BLE001
            tui.print_error(f"list_ready_tasks failed: {e}")
            return 1
        tui.banner(f"orchX tasks: {len(tasks)} ready")
        if not tasks:
            tui.out("  (Ready column is empty)")
            return 0
        for t in tasks:
            tui.out(f"  • [{t.id}] {t.title}")
            if t.url:
                tui.print_dim(f"      {t.url}")
        return 0

    if sub == "pick":
        if not hasattr(tracker, "pick_next_ready_task"):
            tui.print_error(
                f"tracker {type(tracker).__name__} does not support Kanban API "
                "(pick_next_ready_task)."
            )
            return 2
        try:
            task = await tracker.pick_next_ready_task()
        except Exception as e:  # noqa: BLE001
            tui.print_error(f"pick_next_ready_task failed: {e}")
            return 1
        if task is None:
            tui.print_dim("Ready column is empty — nothing to pick.")
            return 0
        tui.banner(f"orchX task picked: {task.id}")
        tui.out(f"Title: {task.title}")
        if task.url:
            tui.print_dim(f"URL:   {task.url}")
        tui.out("")
        tui.out("--- task body ---")
        tui.out(task.body or "(empty)")
        tui.out("")

        # --run: сразу замкнуть цикл (plan + run + PR + auto-move Done).
        if getattr(args, "run", False):
            prompt = (
                f"{task.title}\n\n"
                f"{task.body or ''}\n\n"
                f"Tracker reference: {task.url or task.id}"
            ).strip()
            tui.print_step(
                f"Launching `orchx all --tracker-task {task.id}`"
            )
            all_args = argparse.Namespace(
                task=prompt,
                tracker_task=task.id,
                no_review=getattr(args, "no_review", False),
                auto_followup=getattr(args, "auto_followup", False),
                max_followup_depth=getattr(args, "max_followup_depth", 1),
                no_debugger=getattr(args, "no_debugger", False),
                no_merger=getattr(args, "no_merger", False),
                no_supervisor=getattr(args, "no_supervisor", False),
                supervisor_interval_s=getattr(args, "supervisor_interval_s", 30.0),
                effort=getattr(args, "effort", "high"),
                reviewer_effort=getattr(args, "reviewer_effort", "xhigh"),
                debugger_effort=getattr(args, "debugger_effort", "high"),
                merger_effort=getattr(args, "merger_effort", "high"),
                no_replan=getattr(args, "no_replan", False),
                replanner_effort=getattr(args, "replanner_effort", "xhigh"),
                auto_stash=getattr(args, "auto_stash", False),
                allow_dirty=getattr(args, "allow_dirty", False),
                per_task_review=getattr(args, "per_task_review", False),
                per_task_review_effort=getattr(
                    args, "per_task_review_effort", "medium"
                ),
                cleanup_worktrees=getattr(args, "cleanup_worktrees", False),
                max_cost_usd=getattr(args, "max_cost_usd", None),
                no_auto_fixup=getattr(args, "no_auto_fixup", False),
                resume=getattr(args, "resume", False),
            )
            return await _cmd_all(all_args)

        tui.print_dim(
            f'Next: orchx all --tracker-task "{task.id}" "<body>"  '
            f"(or `orchx tasks pick --run` next time)"
        )
        return 0

    if sub == "move":
        if not hasattr(tracker, "move_task"):
            tui.print_error(
                f"tracker {type(tracker).__name__} does not support Kanban API "
                "(move_task)."
            )
            return 2
        try:
            await tracker.move_task(args.task_id, args.column)
        except Exception as e:  # noqa: BLE001
            tui.print_error(f"move_task failed: {e}")
            return 1
        tui.print_done("moved", f"{args.task_id} → {args.column}")
        return 0

    tui.print_error(f"unknown tasks subcommand: {sub!r}")
    return 2


def _latest_task_id(repo_root: Path) -> str | None:
    """Утилита: вернуть task_id самого свежего run'а в orchx/runs/."""
    runs_dir = paths.runs_dir(repo_root)
    if not runs_dir.exists():
        return None
    candidates = [d for d in runs_dir.iterdir() if d.is_dir()]
    if not candidates:
        return None
    latest = max(candidates, key=lambda d: d.stat().st_mtime)
    return latest.name


def main(argv: list[str] | None = None) -> int:
    """Entrypoint console-команды ``orchx`` (или ``python -m orchx``)."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    repo_root = _detect_repo_root()
    # Подгрузим orchx/.env (если есть) до setup_logging — там пока нет
    # env-зависимостей, но дальше LLMConfig.from_env() уже потребует переменных.
    _load_orchx_env(repo_root)
    _setup_logging(args.verbose, repo_root)
    handler = {
        "plan": _cmd_plan,
        "run": _cmd_run,
        "all": _cmd_all,
        "init": _cmd_init,
        "list": _cmd_list,
        "logs": _cmd_logs,
        "watch": _cmd_watch,
        "plugins": _cmd_plugins,
        "dashboard": _cmd_dashboard,
        "tasks": _cmd_tasks,
    }[args.cmd]
    try:
        return asyncio.run(handler(args))
    except KeyboardInterrupt:
        tui.print_error("interrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
