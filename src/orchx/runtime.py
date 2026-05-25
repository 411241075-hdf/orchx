"""Runtime-конфигурация orchX: где лежат данные пользователя и шаблоны пакета.

Раньше пакет ``orchx`` жил **внутри** одного конкретного репо (5STARS), и все
пути типа ``orchx/prompts/``, ``orchx/runs/``, ``orchx/.env`` хардкодились
от ``repo_root``. После выделения orchx в отдельный пакет/PyPI ему нужно
работать в **любом** проекте: пользовательский data dir теперь
``<project>/.orchx/`` (с точкой, скрытый, gitignored), а дефолтные ресурсы
(промпты ролей, схемы) живут внутри установленного пакета.

:class:`OrchXRuntime` — единственное место, которое знает раскладку. Все
остальные модули принимают экземпляр и спрашивают у него пути.

Каскад загрузки промптов:

1. ``<project>/.orchx/prompts/orchX-<role>.md`` — переопределение под проект
   (можно подправить planner/implementer под свой стек, добавить кастомную
   роль).
2. ``<package>/templates/prompts/orchX-<role>.md`` — дефолт, шиппится
   с пакетом.

Схемы (``plan.schema.json``, ``result.schema.json``, ``task.template.md``)
не переопределяются — они часть контракта пакета и читаются всегда из
ресурсов пакета.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

# Имя runtime-каталога в пользовательском проекте. Скрытое (с точкой),
# по аналогии с ``.git/``, ``.kilo/``, ``.idea/``. Всегда gitignored.
RUNTIME_DIR_NAME = ".orchx"

# Имя поддиректории воркера ВНУТРИ его worktree, где он пишет task.md и
# results/. Тоже скрытое — чтобы dirty-tree-чек репо-пользователя её не
# спутал с реальными файлами проекта.
WORKER_RUNTIME_DIR_NAME = ".orchx"


def _detect_project_root(start: Path) -> Path:
    """Найти корень git-репозитория, начиная от ``start``.

    Падает с :class:`SystemExit`, если ``start`` не внутри репо. orchX
    принципиально работает только в git-репозиториях — без них нечего
    мерджить и в чём делать worktree-ы.
    """
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=start,
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        raise SystemExit(
            f"orchX must run inside a git repository (cwd={start}): {e}"
        ) from e
    return Path(out).resolve()


def _package_root() -> Path:
    """Корень установленного пакета orchx (где лежит этот файл runtime.py)."""
    return Path(__file__).resolve().parent


@dataclass(frozen=True)
class OrchXRuntime:
    """Раскладка путей для одного запуска orchX в конкретном проекте.

    Attributes:
        project_root: Корень git-репозитория пользователя.
        runtime_dir: ``<project_root>/.orchx/`` — пользовательский data dir.
        package_root: Корень установленного пакета orchx (read-only).
        prompts_dirs: Список каталогов с промптами в порядке приоритета.
            Поиск ``orchX-<role>.md`` идёт по этому списку, первый
            существующий файл выигрывает.
        schemas_dir: Каталог JSON-схем плана/результата (всегда из пакета).
        env_file: ``.orchx/.env`` пользователя.
        project_md: ``.orchx/PROJECT.md`` (если существует) — описание стека
            проекта, которое все роли подгружают как контекст. ``None`` если
            файл не создан.
    """

    project_root: Path
    runtime_dir: Path
    package_root: Path
    prompts_dirs: tuple[Path, ...]
    schemas_dir: Path
    env_file: Path
    project_md: Path | None = None
    extra: dict[str, str] = field(default_factory=dict)

    @classmethod
    def detect(
        cls,
        cwd: Path | None = None,
        *,
        extra_prompts_dirs: Iterable[Path] = (),
    ) -> "OrchXRuntime":
        """Сконструировать runtime для текущей рабочей директории.

        Args:
            cwd: Откуда искать корень git-репо. По умолчанию ``os.getcwd()``.
            extra_prompts_dirs: Дополнительные каталоги, которые подмешать в
                начало ``prompts_dirs`` (например, тестовая площадка).

        Раскладка:

        - ``project_root`` ← ``git rev-parse --show-toplevel`` от ``cwd``.
        - ``runtime_dir`` = ``project_root / ".orchx"``.
        - ``prompts_dirs`` = [extra..., runtime_dir/"prompts", package/"templates/prompts"].
        - ``schemas_dir`` = package/"schemas".
        - ``env_file`` = runtime_dir/".env".
        - ``project_md`` = runtime_dir/"PROJECT.md" если файл существует, иначе ``None``.

        Каталоги лениво — фактическую mkdir делает CLI/orchestrator при
        первой записи, чтобы read-only команды не оставляли пустых папок.
        """
        start = (cwd or Path.cwd()).resolve()
        project_root = _detect_project_root(start)
        return cls.from_project_root(
            project_root, extra_prompts_dirs=extra_prompts_dirs
        )

    @classmethod
    def from_project_root(
        cls,
        project_root: Path,
        *,
        extra_prompts_dirs: Iterable[Path] = (),
    ) -> "OrchXRuntime":
        """Сконструировать runtime, когда ``project_root`` уже известен.

        Используется в местах, где :func:`detect` уже сработал и положил
        ``repo_root`` в существующую переменную (worker, orchestrator), —
        чтобы не звать ``git rev-parse`` повторно.
        """
        project_root = Path(project_root).resolve()
        package_root = _package_root()

        runtime_dir = project_root / RUNTIME_DIR_NAME
        prompts_dirs: list[Path] = [Path(p).resolve() for p in extra_prompts_dirs]
        prompts_dirs.append(runtime_dir / "prompts")
        prompts_dirs.append(package_root / "templates" / "prompts")
        schemas_dir = package_root / "schemas"
        env_file = runtime_dir / ".env"
        project_md_path = runtime_dir / "PROJECT.md"
        project_md = project_md_path if project_md_path.exists() else None

        return cls(
            project_root=project_root,
            runtime_dir=runtime_dir,
            package_root=package_root,
            prompts_dirs=tuple(prompts_dirs),
            schemas_dir=schemas_dir,
            env_file=env_file,
            project_md=project_md,
        )

    # --- удобные алиасы под старые имена -----------------------------------

    @property
    def repo_root(self) -> Path:
        """Алиас для совместимости со старым кодом, который оперировал ``repo_root``."""
        return self.project_root

    def runtime_subdir(self, *parts: str) -> Path:
        """Удобный конструктор путей внутри ``runtime_dir``."""
        return self.runtime_dir.joinpath(*parts)


# ---------------------------------------------------------------------------
# Worker runtime paths (внутри worktree воркера, относительно его cwd)
# ---------------------------------------------------------------------------

# Воркер пишет свои artefacts в ``<worktree>/.orchx/``:
#   .orchx/task.md           — контракт от диспетчера
#   .orchx/results/<id>.json — итоговый отчёт
#   .orchx/subtasks/         — sub-call логи (TaskTool)
# Это скрытая папка, добавленная в .gitignore проекта; merger/reviewer
# читает её через relative path внутри своего worktree.
WORKER_TASK_FILE = f"{WORKER_RUNTIME_DIR_NAME}/task.md"
WORKER_RESULTS_DIR = f"{WORKER_RUNTIME_DIR_NAME}/results"
WORKER_SUBTASKS_DIR = f"{WORKER_RUNTIME_DIR_NAME}/subtasks"


def worker_result_rel_path(task_id: str) -> str:
    """Относительный путь result.json внутри worktree воркера."""
    return f"{WORKER_RESULTS_DIR}/{task_id}.json"


def worker_subtask_log_rel_path(sub_id: str) -> str:
    """Относительный путь sub-task лога внутри worktree."""
    return f"{WORKER_SUBTASKS_DIR}/{sub_id}.log"


# ---------------------------------------------------------------------------
# Gitignore management
# ---------------------------------------------------------------------------

# Эти строки добавляет команда `orchx init` в проектный .gitignore.
GITIGNORE_BLOCK = """
# orchX runtime (managed by orchx)
.orchx/runs/
.orchx/_pending/
.orchx/.env
"""

GITIGNORE_MARKER = "# orchX runtime (managed by orchx)"


def ensure_gitignore(project_root: Path) -> bool:
    """Добавить orchX-блок в ``<project>/.gitignore``, если его там нет.

    Идемпотентно: если маркер уже присутствует, ничего не делает.

    Returns:
        True, если файл был изменён (или создан), иначе False.
    """
    gi = project_root / ".gitignore"
    existing = ""
    if gi.exists():
        existing = gi.read_text(encoding="utf-8")
        if GITIGNORE_MARKER in existing:
            return False
    sep = "" if existing.endswith("\n") or not existing else "\n"
    new_content = existing + sep + GITIGNORE_BLOCK.lstrip("\n")
    gi.write_text(new_content, encoding="utf-8")
    return True


# ---------------------------------------------------------------------------
# Project context (PROJECT.md)
# ---------------------------------------------------------------------------


def read_project_context(runtime: OrchXRuntime) -> str:
    """Прочитать ``.orchx/PROJECT.md`` если существует, иначе вернуть ''.

    Используется для того, чтобы все role-промпты могли подмешать описание
    стека проекта без перезаписи самих промптов.
    """
    if runtime.project_md is None:
        return ""
    try:
        return runtime.project_md.read_text(encoding="utf-8")
    except OSError:
        return ""


# ---------------------------------------------------------------------------
# Optional: legacy migration helper
# ---------------------------------------------------------------------------


def migrate_legacy_runtime_dir(project_root: Path) -> Path | None:
    """Переехать со старого ``orchx/`` на ``.orchx/`` в проекте пользователя.

    Если в проекте есть открытая папка ``orchx/`` (legacy раскладка из
    времён, когда пакет жил внутри 5STARS) и ещё нет ``.orchx/`` — эта
    функция переименовывает её. Используется при обновлении пакета.

    Returns:
        Путь к новому ``.orchx/``, если миграция была выполнена. ``None``
        если миграция не нужна (новая папка уже есть, или старой не было).
    """
    legacy = project_root / "orchx"
    target = project_root / RUNTIME_DIR_NAME
    if not legacy.exists() or not legacy.is_dir():
        return None
    if target.exists():
        return None
    # Переезжаем только если внутри есть характерные runtime-маркеры,
    # чтобы не задеть случайную папку с таким именем (например, src/orchx).
    markers = [".env", "runs", "_pending", "PROJECT.md"]
    if not any((legacy / m).exists() for m in markers):
        return None
    legacy.rename(target)
    return target


# Удобно для тестов / диагностики.
def env_summary() -> dict[str, str]:
    """Снимок env-переменных, относящихся к runtime."""
    keys = [
        "ORCHX_LLM_BASE_URL",
        "ORCHX_LLM_API_KEY",
        "ORCHX_MODEL",
        "ORCHX_PLANNER_MODEL",
        "ORCHX_REVIEWER_MODEL",
        "ORCHX_DEBUGGER_MODEL",
        "ORCHX_MERGER_MODEL",
        "ORCHX_TIMEOUT_S",
        "ORCHX_NO_TUI",
        "NO_COLOR",
    ]
    out: dict[str, str] = {}
    for k in keys:
        v = os.environ.get(k)
        if v is not None:
            out[k] = "***" if k == "ORCHX_LLM_API_KEY" and v else v
    return out
