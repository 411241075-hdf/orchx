"""Единая раскладка runtime-артефактов orchX.

Контракт: всё, что относится к одному прогону роя (одному ``task_id``),
живёт в ``<project_root>/.orchx/runs/<task_id>/``. Воркеры по-прежнему
пишут свои ``.orchx/task.md`` и ``.orchx/results/<id>.json`` ВНУТРИ своего
изолированного worktree — это не корень репозитория, а отдельный checkout,
поэтому путь не конфликтует с раскладкой основного репо.

Структура одного запуска::

    .orchx/runs/<task_id>/
    ├── plan.json                    # активный план (после replan'а — последняя версия)
    ├── plan.before-replan-N.json    # бэкапы плана перед N-м replan'ом
    ├── replan-context.md            # бриф для planner'а при replan'е
    ├── dispatcher.log               # лог Python-диспетчера (root logger)
    ├── planner.log                  # лог orchX-planner на старте
    ├── orchx.log                    # человекочитаемый журнал прогона
    ├── summary.json                 # итоговая сводка
    ├── logs/
    │   ├── <subtask>.attemptN.log
    │   ├── <subtask>.merger.attemptN.log
    │   ├── replan-N.log
    │   └── review__<task_id>.log
    └── worktrees/
        ├── _integration/            # интеграционная ветка orchX/<task_id>
        ├── _review/                 # рабочая зона reviewer'а
        └── <subtask_id>/            # worktree-ы воркеров

Кроме того, ``.orchx/_pending/`` — staging-каталог для этапа `orchx plan`,
когда task_id ещё не известен (planner'у негде взять его до записи плана).
После успешного планирования CLI перекладывает содержимое в
``.orchx/runs/<task_id>/``.

JSON-схемы и шаблон task.md шипятся ВНУТРИ Python-пакета (``orchx/schemas/``),
не в runtime-каталоге — это код, а не данные. Доступ через
:func:`package_schemas_dir` или :class:`orchx.runtime.OrchXRuntime`.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from .runtime import RUNTIME_DIR_NAME, WORKER_RUNTIME_DIR_NAME

__all__ = [
    "RUNTIME_DIR_NAME",
    "WORKER_RUNTIME_DIR_NAME",
    "package_schemas_dir",
    "task_template_path",
    "orchx_root",
    "orchX_root",  # noqa
    "pending_dir",
    "pending_plan_path",
    "pending_planner_log",
    "pending_dispatcher_log",
    "runs_dir",
    "run_dir",
    "plan_path",
    "replan_backup_path",
    "replan_context_path",
    "dispatcher_log_path",
    "planner_log_path",
    "orchx_log_path",
    "orchX_log_path",  # noqa
    "summary_path",
    "logs_dir",
    "worktrees_dir",
    "integration_worktree_path",
    "review_worktree_path",
    "task_worktree_path",
    "ORCHX_ARTEFACT_PREFIXES",
    "cleanup_pending",
]


# ---------------------------------------------------------------------------
# Package-shipped schemas (read-only)
# ---------------------------------------------------------------------------


def package_schemas_dir() -> Path:
    """Каталог со схемами/шаблонами внутри пакета orchx.

    Используется для чтения ``task.template.md``, ``plan.schema.json``,
    ``result.schema.json`` — это статические артефакты, лежащие рядом с
    кодом, а не в runtime-каталоге.
    """
    return Path(__file__).resolve().parent / "schemas"


def task_template_path() -> Path:
    """Шаблон ``task.md`` для рендера контракта воркера."""
    return package_schemas_dir() / "task.template.md"


# ---------------------------------------------------------------------------
# Runtime data dir
# ---------------------------------------------------------------------------


def orchx_root(repo_root: Path) -> Path:
    """``.orchx/`` в корне репозитория (runtime-каталог)."""
    return repo_root / RUNTIME_DIR_NAME


# Алиас для обратной совместимости с внутренним API.
def orchX_root(repo_root: Path) -> Path:  # noqa: N802 — legacy mixed-case name
    """Алиас для :func:`orchx_root` (для старых импортов)."""
    return orchx_root(repo_root)


def pending_dir(repo_root: Path) -> Path:
    """Staging-каталог для plan.json/planner.log до момента, пока task_id не известен."""
    return orchx_root(repo_root) / "_pending"


def pending_plan_path(repo_root: Path) -> Path:
    """Promejutochnyj plan.json до того, как мы прочитаем task_id."""
    return pending_dir(repo_root) / "plan.json"


def pending_planner_log(repo_root: Path) -> Path:
    """Лог планнера при `orchx plan` без активного run_dir."""
    return pending_dir(repo_root) / "planner.log"


def pending_dispatcher_log(repo_root: Path) -> Path:
    """Лог диспетчера до того, как стал известен task_id."""
    return pending_dir(repo_root) / "dispatcher.log"


def runs_dir(repo_root: Path) -> Path:
    """Корень всех run-каталогов."""
    return orchx_root(repo_root) / "runs"


def run_dir(repo_root: Path, task_id: str) -> Path:
    """`.orchx/runs/<task_id>/`."""
    return runs_dir(repo_root) / task_id


def plan_path(repo_root: Path, task_id: str) -> Path:
    """Активный plan.json для конкретного запуска."""
    return run_dir(repo_root, task_id) / "plan.json"


def replan_backup_path(repo_root: Path, task_id: str, attempt: int) -> Path:
    """Бэкап плана перед N-м replan'ом."""
    return run_dir(repo_root, task_id) / f"plan.before-replan-{attempt}.json"


def replan_context_path(repo_root: Path, task_id: str) -> Path:
    """Бриф для planner'а при replan'е."""
    return run_dir(repo_root, task_id) / "replan-context.md"


def dispatcher_log_path(repo_root: Path, task_id: str) -> Path:
    """Лог Python-диспетчера для конкретного запуска."""
    return run_dir(repo_root, task_id) / "dispatcher.log"


def planner_log_path(repo_root: Path, task_id: str) -> Path:
    """Лог initial-planning run'а (orchX-planner role)."""
    return run_dir(repo_root, task_id) / "planner.log"


def orchx_log_path(repo_root: Path, task_id: str) -> Path:
    """Человекочитаемый журнал прогона."""
    return run_dir(repo_root, task_id) / "orchx.log"


# Legacy alias.
def orchX_log_path(repo_root: Path, task_id: str) -> Path:  # noqa: N802
    """Алиас для :func:`orchx_log_path`."""
    return orchx_log_path(repo_root, task_id)


def summary_path(repo_root: Path, task_id: str) -> Path:
    """Итоговая сводка прогона."""
    return run_dir(repo_root, task_id) / "summary.json"


def logs_dir(repo_root: Path, task_id: str) -> Path:
    """Папка с логами воркеров/мерджеров/replan'ов/reviewer'а."""
    return run_dir(repo_root, task_id) / "logs"


def worktrees_dir(repo_root: Path, task_id: str) -> Path:
    """Папка со всеми worktree-ами этого прогона."""
    return run_dir(repo_root, task_id) / "worktrees"


def integration_worktree_path(repo_root: Path, task_id: str) -> Path:
    """Worktree интеграционной ветки `orchX/<task_id>`."""
    return worktrees_dir(repo_root, task_id) / "_integration"


def review_worktree_path(repo_root: Path, task_id: str) -> Path:
    """Worktree reviewer'а."""
    return worktrees_dir(repo_root, task_id) / "_review"


def task_worktree_path(repo_root: Path, task_id: str, subtask_id: str) -> Path:
    """Worktree одного worker'а."""
    return worktrees_dir(repo_root, task_id) / subtask_id


# ---------------------------------------------------------------------------
# Diff classification (PR artefact filtering)
# ---------------------------------------------------------------------------


# Префиксы (относительно корня репо), которые считаются служебными
# артефактами роя и игнорируются при оценке «значимости» дельты PR.
# Поддерживаем и новый путь ``.orchx/``, и legacy ``orchx/`` — на случай
# существующих веток с историей до миграции на скрытый каталог.
ORCHX_ARTEFACT_PREFIXES: tuple[str, ...] = (
    # Текущая раскладка (.orchx/ — скрытый каталог).
    ".orchx/runs/",
    ".orchx/_pending/",
    ".orchx/results/",
    ".orchx/subtasks/",
    ".orchx/task.md",
    ".orchx/plan.json",
    ".orchx/replan-context.md",
    ".orchx/plan.before-replan-",
    ".orchx/worktrees/",
    # Legacy раскладка до 0.1.0 (открытая папка orchx/ внутри 5STARS).
    "orchx/runs/",
    "orchx/_pending/",
    "orchx/results/",
    "orchx/task.md",
    "orchx/plan.json",
    "orchx/replan-context.md",
    "orchx/plan.before-replan-",
    "orchx/worktrees/",
)


def cleanup_pending(repo_root: Path) -> None:
    """Удалить ``.orchx/_pending/`` целиком (после успешного промоушена)."""
    p = pending_dir(repo_root)
    if p.exists():
        shutil.rmtree(p, ignore_errors=True)
