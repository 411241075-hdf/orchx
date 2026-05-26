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

import os
import shutil
import sys
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
    "snapshots_dir",
    "snapshot_path",
    "integration_worktree_path",
    "review_worktree_path",
    "task_worktree_path",
    "external_worktrees_root",
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


def external_worktrees_root() -> Path | None:
    """Базовая директория для хранения worktree вне репо.

    На macOS git-worktree в ``.orchx/runs/`` страдает от Finder/Spotlight/
    iCloud, которые периодически создают «<file> 2» дубли. Чтобы убрать
    источник проблемы (а не лечить симптом), worktree выносим в
    ``~/Library/Caches/orchx/worktrees/``, которая Finder'ом не
    индексируется.

    На Linux/Windows возвращаем None — worktree остаются in-tree
    (``.orchx/runs/<task>/worktrees/``).

    Можно явно переопределить через env var ``ORCHX_WORKTREES_ROOT``
    (абсолютный путь). Это полезно для CI, где cache-папки эфемерны,
    или для пользователя, который хочет хранить worktree на быстром SSD.

    Returns:
        Path к корневой директории, либо None для in-tree режима.
    """
    env_override = os.environ.get("ORCHX_WORKTREES_ROOT", "").strip()
    if env_override:
        return Path(os.path.expanduser(os.path.expandvars(env_override)))
    if sys.platform == "darwin":
        # ~/Library/Caches/orchx/worktrees/  — стандартное место для
        # ephemeral cache-данных на macOS, не индексируется Finder'ом.
        return Path.home() / "Library" / "Caches" / "orchx" / "worktrees"
    return None


def worktrees_dir(repo_root: Path, task_id: str) -> Path:
    """Папка со всеми worktree-ами этого прогона.

    Поведение:

    - macOS (или явный ``ORCHX_WORKTREES_ROOT``): ``<external>/<repo_hash>/<task_id>/``,
      где ``<repo_hash>`` — стабильный хеш абсолютного пути репо
      (избегает коллизий между разными чекаутами одного и того же проекта).
    - Linux/Windows (default): ``.orchx/runs/<task_id>/worktrees/`` (legacy).

    Эта функция возвращает ВСЕГДА один и тот же путь для одной (repo_root,
    task_id) пары — кешируется внутри run_dir'а через symlink (см.
    :func:`ensure_worktrees_symlink`).
    """
    external = external_worktrees_root()
    if external is None:
        return run_dir(repo_root, task_id) / "worktrees"
    # Используем стабильный slug от абсолютного пути репо, чтобы разные
    # чекауты одного проекта (orchx-A/, orchx-B/) не конфликтовали.
    import hashlib

    repo_slug = (
        repo_root.name
        + "-"
        + hashlib.sha1(str(repo_root.resolve()).encode("utf-8")).hexdigest()[:8]
    )
    return external / repo_slug / task_id


def ensure_worktrees_symlink(repo_root: Path, task_id: str) -> None:
    """Создать symlink ``.orchx/runs/<task>/worktrees/`` → внешний путь.

    Это нужно, чтобы TUI / pr_watcher / любой другой код, который
    смотрит в ``run_dir/worktrees/`` (исторически — local), продолжал
    работать. Symlink — это zero-copy, всегда актуален.

    Если worktree-root внутренний (Linux), функция ничего не делает.
    """
    real = worktrees_dir(repo_root, task_id)
    legacy = run_dir(repo_root, task_id) / "worktrees"
    if real == legacy:
        return  # in-tree режим, ничего делать не надо
    real.mkdir(parents=True, exist_ok=True)
    legacy.parent.mkdir(parents=True, exist_ok=True)
    if legacy.is_symlink():
        try:
            if legacy.readlink() == real:
                return
        except OSError:
            pass
        legacy.unlink()
    elif legacy.exists():
        # Уже существует как директория (legacy run без symlink'а)
        # — оставляем как есть, не трогаем.
        return
    try:
        legacy.symlink_to(real, target_is_directory=True)
    except OSError:
        # symlink не удался (Windows без admin / FS не поддерживает) —
        # тихо игнорируем, worktree-функции продолжат использовать
        # реальный путь.
        pass


def snapshots_dir(repo_root: Path, task_id: str) -> Path:
    """Каталог со snapshot'ами worktree'ов перед retry.

    См. :func:`snapshot_path`.
    """
    return run_dir(repo_root, task_id) / "snapshots"


def snapshot_path(
    repo_root: Path, task_id: str, subtask_id: str, attempt: int
) -> Path:
    """Путь к snapshot'у одной попытки задачи.

    Snapshot содержит копию worktree (без ``.git/``) перед тем, как
    debugger пересоздаст его для retry'я. Позволяет debugger'у видеть
    что именно сделал предыдущий attempt и продолжить с этого места,
    а не переписывать с нуля.
    """
    return snapshots_dir(repo_root, task_id) / f"{subtask_id}.attempt{attempt}"


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
