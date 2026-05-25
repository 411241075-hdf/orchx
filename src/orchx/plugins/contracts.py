"""Контракты plugin'ов (Protocol-based, ducktype-friendly).

Все плагины декларируются как :class:`typing.Protocol` — это даёт:

* Lightweight интерфейсы без forcing inheritance.
* Runtime ``isinstance`` checks для валидации регистрации (через
  ``@runtime_checkable``).
* IDE-completion / mypy-checking для авторов плагинов.

Каждый плагин — это **класс с конструктором** ``__init__(self, **config) -> None``,
который вызывается фабрикой в :mod:`orchx.plugins.registry` при инстанциировании.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol, runtime_checkable

# ---------------------------------------------------------------------------
# Общие типы
# ---------------------------------------------------------------------------


class WorkerOutcomeLike(Protocol):
    """Контракт результата работы воркера. Совместим с :class:`orchx.agent.worker.WorkerOutcome`.

    Не используем сам ``WorkerOutcome`` чтобы избежать циклов импорта;
    конкретный runtime может вернуть subclass / dataclass с этими полями.
    """

    returncode: int
    stdout: str
    stderr: str
    timed_out: bool
    duration_s: float
    input_tokens: int
    output_tokens: int
    llm_calls: int
    compactions: int
    cost_usd: float
    """P1.3 — total cost в USD. Может быть 0.0 если runtime не считает стоимость."""


# ---------------------------------------------------------------------------
# Runtime
# ---------------------------------------------------------------------------


@runtime_checkable
class RuntimePlugin(Protocol):
    """Где и как исполняется orchX-worker.

    Дефолтная реализация — :class:`orchx.plugins.runtimes.local.LocalRuntime`
    (asyncio + git worktree). Альтернативы — docker, podman, kubernetes, …

    Lifecycle:

    1. orchestrator зовёт ``spawn_worker(...)``.
    2. Plugin запускает worker'а в своём окружении (subprocess / container).
    3. Возвращает :class:`WorkerOutcomeLike` (returncode, tail'ы, токены).
    """

    async def spawn_worker(
        self,
        *,
        cwd: Path,
        repo_root: Path,
        role: str,
        prompt: str,
        timeout_s: float,
        log_file: Path,
        effort: str | None,
        on_activity: Any = None,
    ) -> WorkerOutcomeLike:
        """Запустить worker'а и дождаться завершения.

        Args:
            cwd: рабочая директория worker'а (его git worktree).
            repo_root: корень исходного репозитория (для context).
            role: имя роли (implementer / reviewer / debugger / merger / planner).
            prompt: текстовый prompt, передаваемый worker'у.
            timeout_s: жёсткий timeout в секундах.
            log_file: куда писать stdout/stderr worker'а.
            effort: reasoning-effort hint (low/medium/high/xhigh/max), может быть None.
            on_activity: опциональный callback ``(line: str) -> None`` для live-tail'а.
        """
        ...


# ---------------------------------------------------------------------------
# Tracker
# ---------------------------------------------------------------------------


@runtime_checkable
class TrackerPlugin(Protocol):
    """Issue-tracker, откуда задачи приходят и куда статусы отправляются.

    По умолчанию — :class:`orchx.plugins.trackers.github.GithubTracker`
    (использует ``gh`` CLI). Альтернативы — linear / jira / gitlab / …
    """

    async def fetch_task_description(self, task_id: str) -> str | None:
        """Получить описание задачи (issue body) по её ID.

        Returns ``None`` если задача не найдена (orchX тогда работает
        с заданным prompt'ом, без tracker-контекста).
        """
        ...

    async def update_status(
        self,
        task_id: str,
        status: str,
        details: str = "",
    ) -> None:
        """Обновить статус задачи в трекере (комментарий / label / state).

        Args:
            status: одно из ``running`` | ``done`` | ``failed`` | ``replanned``.
            details: дополнительная информация (PR-link, error message).
        """
        ...


# ---------------------------------------------------------------------------
# SCM
# ---------------------------------------------------------------------------


@runtime_checkable
class SCMPlugin(Protocol):
    """Где живут ветки и PR/MR. По умолчанию — GitHub через ``gh`` CLI."""

    async def push_branch(self, repo_root: Path, branch: str) -> None:
        """``git push`` указанной ветки в remote."""
        ...

    async def open_pr(
        self,
        *,
        repo_root: Path,
        head_branch: str,
        base_branch: str,
        title: str,
        body: str,
        draft: bool = False,
    ) -> str:
        """Открыть PR/MR. Возвращает URL."""
        ...

    async def get_pr_status(self, repo_root: Path, pr_url: str) -> dict[str, Any]:
        """Получить statusCheckRollup + reviewDecision + comments.

        Используется :mod:`orchx.pr_watcher` (P0.4).
        """
        ...


# ---------------------------------------------------------------------------
# Notifier
# ---------------------------------------------------------------------------


@runtime_checkable
class NotifierPlugin(Protocol):
    """Куда отправлять события orchX (run_started, pr_opened, cost_alert и т.п.).

    События — свободные строки, payload — JSON-сериализуемые dict'ы.

    Дефолтная реализация — :class:`orchx.plugins.notifiers.noop.NoopNotifier`
    (silently игнорирует, ноль конфигурации).
    """

    async def notify(self, event: str, payload: dict[str, Any]) -> None:
        """Отправить событие. Не должно бросать наружу — все исключения
        проглатываются и логируются (плагин ≠ blocking-dependency)."""
        ...


# ---------------------------------------------------------------------------
# Memory
# ---------------------------------------------------------------------------


@runtime_checkable
class MemoryPlugin(Protocol):
    """Долговременная память (RAG для planner / debugger).

    Дефолтная реализация — :class:`orchx.plugins.memory.noop.NoopMemory`
    (silently no-op). Production — :class:`orchx.plugins.memory.sqlite.SqliteMemory`
    (SQLite + FTS5 + опциональные эмбеддинги).
    """

    async def remember(
        self,
        namespace: str,
        key: str,
        value: dict[str, Any],
    ) -> None:
        """Сохранить факт. Namespace типа ``plans`` / ``failures`` / ``fixes`` / ``reviews``."""
        ...

    async def recall(
        self,
        namespace: str,
        query: str,
        k: int = 5,
    ) -> list[dict[str, Any]]:
        """Найти top-k релевантных фактов по семантическому/FTS-поиску."""
        ...

    async def forget_old(self, days: int = 90) -> int:
        """Garbage-collect старые / неиспользуемые записи. Returns: число удалённых."""
        ...
