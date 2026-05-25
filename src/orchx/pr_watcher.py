"""PR feedback loop watcher (P0.4 — см. docs/recommendations.md).

После opening PR orchX-watcher не завершается, а опрашивает GitHub
через :class:`GithubSCM` plugin и реагирует на события:

* **ci_failed** — спавнит ``orchX-debugger`` со списком CI-логов
  как failure_context.
* **changes_requested** — спавнит ``orchX-implementer`` с цитатой
  review-комментариев.
* **approved_and_green** — отправляет notification (или auto-merge
  если ``action: auto-merge``).

Конфигурация — через ``.orchx/config.yaml`` секция ``reactions:``
(см. docs/recommendations.md → P0.4). По умолчанию все реакции
выключены, watcher не запускается.

CLI:

.. code-block:: bash

   # Запуск ВО ВРЕМЯ прогона (через --watch):
   orchx all "..." --watch

   # Отдельный запуск (если уже есть открытый PR):
   orchx watch <task_id> --pr-url https://github.com/.../pull/123
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

logger = logging.getLogger(__name__)


ReactionEvent = Literal["ci_failed", "changes_requested", "approved_and_green"]


@dataclass
class ReactionConfig:
    """Конфигурация одной реакции."""

    auto: bool = True
    """Применять реакцию автоматически? Если False — только notify."""
    action: str = "notify"
    """Action: ``send-to-debugger`` | ``send-to-implementer`` |
    ``notify`` | ``auto-merge``."""
    max_retries: int = 3
    """Сколько раз пытаться авто-починить (для ci_failed / changes_requested)."""
    escalate_after_min: int = 30
    """Если no auto-resolution — notify через X минут."""


DEFAULT_REACTIONS: dict[ReactionEvent, ReactionConfig] = {
    "ci_failed": ReactionConfig(auto=True, action="send-to-debugger", max_retries=3),
    "changes_requested": ReactionConfig(
        auto=True, action="send-to-implementer", escalate_after_min=30
    ),
    "approved_and_green": ReactionConfig(auto=False, action="notify"),
}


@dataclass
class WatcherState:
    """Хранит уже viewed PR-state, чтобы не реагировать дважды."""

    last_ci_status: str | None = None
    seen_comment_ids: set[str] = field(default_factory=set)
    ci_retry_count: int = 0
    notified_approved: bool = False


def parse_reactions_yaml(raw: dict[str, Any]) -> dict[ReactionEvent, ReactionConfig]:
    """Распарсить секцию ``reactions:`` из YAML-конфига.

    Поддерживает поля per-reaction: ``auto``, ``action``, ``max_retries``,
    ``escalate_after`` (с суффиксом ``m``/``min``/``h``/``hour``).
    """
    out: dict[ReactionEvent, ReactionConfig] = dict(DEFAULT_REACTIONS)
    for key, cfg in (raw or {}).items():
        if key not in DEFAULT_REACTIONS:
            logger.warning("unknown reaction key %r, skipping", key)
            continue
        if not isinstance(cfg, dict):
            logger.warning("reaction %r config must be a dict, got %r", key, type(cfg))
            continue
        rc = ReactionConfig(
            auto=bool(cfg.get("auto", DEFAULT_REACTIONS[key].auto)),
            action=str(cfg.get("action", DEFAULT_REACTIONS[key].action)),
            max_retries=int(cfg.get("max_retries", DEFAULT_REACTIONS[key].max_retries)),
            escalate_after_min=_parse_duration_min(
                cfg.get("escalate_after"),
                default=DEFAULT_REACTIONS[key].escalate_after_min,
            ),
        )
        out[key] = rc  # type: ignore[index]
    return out


def _parse_duration_min(raw: Any, default: int) -> int:
    """Принимает ``30``, ``"30m"``, ``"30min"``, ``"1h"``, ``"1hour"``."""
    if raw is None:
        return default
    if isinstance(raw, (int, float)):
        return int(raw)
    s = str(raw).strip().lower()
    multipliers = {"m": 1, "min": 1, "minute": 1, "minutes": 1, "h": 60, "hour": 60, "hours": 60}
    for suffix, mult in multipliers.items():
        if s.endswith(suffix):
            try:
                return int(float(s.removesuffix(suffix).strip()) * mult)
            except ValueError:
                return default
    try:
        return int(s)
    except ValueError:
        return default


def _extract_ci_rollup(pr_data: dict[str, Any]) -> str | None:
    """Из ``gh pr view --json statusCheckRollup`` достать общий статус.

    Returns: ``"SUCCESS"`` | ``"FAILURE"`` | ``"PENDING"`` | ``None``.
    """
    rollup = pr_data.get("statusCheckRollup") or []
    if not rollup:
        return None
    statuses = []
    for check in rollup:
        # GitHub API формат: для commit_status это ``state``; для check_run — ``conclusion``.
        st = check.get("conclusion") or check.get("state") or check.get("status")
        if not st:
            continue
        statuses.append(str(st).upper())
    if not statuses:
        return None
    if any(s in ("FAILURE", "ERROR", "CANCELLED", "TIMED_OUT") for s in statuses):
        return "FAILURE"
    if any(s in ("PENDING", "IN_PROGRESS", "QUEUED", "REQUESTED") for s in statuses):
        return "PENDING"
    if all(s in ("SUCCESS", "COMPLETED", "NEUTRAL", "SKIPPED") for s in statuses):
        return "SUCCESS"
    return "PENDING"


def _extract_review_decision(pr_data: dict[str, Any]) -> str | None:
    """``APPROVED`` | ``CHANGES_REQUESTED`` | ``REVIEW_REQUIRED`` | ``None``."""
    decision = pr_data.get("reviewDecision")
    return str(decision).upper() if decision else None


def _extract_change_request_comments(pr_data: dict[str, Any]) -> list[dict[str, Any]]:
    """Все reviews с state=CHANGES_REQUESTED + их comments."""
    out: list[dict[str, Any]] = []
    for r in pr_data.get("reviews") or []:
        if str(r.get("state", "")).upper() != "CHANGES_REQUESTED":
            continue
        out.append(
            {
                "id": str(r.get("id") or ""),
                "author": r.get("author", {}).get("login")
                or r.get("user", {}).get("login")
                or "unknown",
                "body": r.get("body") or "",
                "submitted_at": r.get("submittedAt") or r.get("submitted_at"),
            }
        )
    return out


async def watch_pr(
    *,
    repo_root: Path,
    pr_url: str,
    task_id: str,
    reactions: dict[ReactionEvent, ReactionConfig],
    scm: Any,
    notifier: Any | None = None,
    on_ci_failed: Any = None,
    on_changes_requested: Any = None,
    on_approved_and_green: Any = None,
    poll_interval_s: float = 60.0,
    max_wall_s: float = 24 * 3600.0,
) -> None:
    """Бесконечный watcher PR'а до merge / close / max_wall_s.

    Args:
        repo_root: корень репо.
        pr_url: URL pull request'а.
        task_id: orchX task_id (для логов / нотификаций).
        reactions: конфиг реакций.
        scm: :class:`orchx.plugins.contracts.SCMPlugin` — для poll'а PR.
        notifier: опциональный :class:`NotifierPlugin`.
        on_ci_failed: ``async (ci_logs: str, retry_count: int) -> None`` для запуска debugger.
        on_changes_requested: ``async (comments: list[dict]) -> None`` для запуска implementer.
        on_approved_and_green: ``async () -> None`` — необязательный hook.
        poll_interval_s: период опроса.
        max_wall_s: жёсткий ограничитель, чтобы watcher не висел вечно.
    """
    started = time.monotonic()
    state = WatcherState()
    logger.info(
        "[pr-watcher %s] started: pr=%s poll=%ss", task_id, pr_url, poll_interval_s
    )

    while True:
        if time.monotonic() - started > max_wall_s:
            logger.warning("[pr-watcher %s] max wall exceeded, exiting", task_id)
            if notifier:
                await notifier.notify(
                    "pr_watcher_timeout",
                    {"task_id": task_id, "pr_url": pr_url},
                )
            return

        try:
            pr_data = await scm.get_pr_status(repo_root, pr_url)
        except Exception as e:  # noqa: BLE001
            logger.warning("[pr-watcher %s] get_pr_status failed: %s", task_id, e)
            await asyncio.sleep(poll_interval_s)
            continue

        pr_state = str(pr_data.get("state") or "").upper()
        if pr_state in ("MERGED", "CLOSED"):
            logger.info(
                "[pr-watcher %s] PR %s -> %s, exiting", task_id, pr_url, pr_state
            )
            if notifier:
                await notifier.notify(
                    "pr_closed",
                    {"task_id": task_id, "pr_url": pr_url, "state": pr_state},
                )
            return

        # 1. CI failed?
        ci_status = _extract_ci_rollup(pr_data)
        if (
            ci_status == "FAILURE"
            and state.last_ci_status != "FAILURE"
            and on_ci_failed is not None
        ):
            rc = reactions.get("ci_failed", DEFAULT_REACTIONS["ci_failed"])
            if rc.auto and state.ci_retry_count < rc.max_retries:
                state.ci_retry_count += 1
                logger.info(
                    "[pr-watcher %s] CI FAILED → spawning debugger (try %s/%s)",
                    task_id,
                    state.ci_retry_count,
                    rc.max_retries,
                )
                if notifier:
                    await notifier.notify(
                        "ci_failed",
                        {
                            "task_id": task_id,
                            "pr_url": pr_url,
                            "retry": state.ci_retry_count,
                        },
                    )
                try:
                    await on_ci_failed("", state.ci_retry_count)
                except Exception as e:  # noqa: BLE001
                    logger.warning(
                        "[pr-watcher %s] on_ci_failed callback failed: %s",
                        task_id,
                        e,
                    )
        state.last_ci_status = ci_status

        # 2. Changes requested?
        change_comments = _extract_change_request_comments(pr_data)
        new_comments = [
            c for c in change_comments if c.get("id") not in state.seen_comment_ids
        ]
        for c in new_comments:
            state.seen_comment_ids.add(c.get("id") or "")
        if new_comments and on_changes_requested is not None:
            rc = reactions.get(
                "changes_requested", DEFAULT_REACTIONS["changes_requested"]
            )
            if rc.auto:
                logger.info(
                    "[pr-watcher %s] CHANGES_REQUESTED (%s new) → spawning implementer",
                    task_id,
                    len(new_comments),
                )
                if notifier:
                    await notifier.notify(
                        "changes_requested",
                        {
                            "task_id": task_id,
                            "pr_url": pr_url,
                            "comments": len(new_comments),
                        },
                    )
                try:
                    await on_changes_requested(new_comments)
                except Exception as e:  # noqa: BLE001
                    logger.warning(
                        "[pr-watcher %s] on_changes_requested callback failed: %s",
                        task_id,
                        e,
                    )

        # 3. Approved and green?
        decision = _extract_review_decision(pr_data)
        if (
            decision == "APPROVED"
            and ci_status == "SUCCESS"
            and not state.notified_approved
        ):
            state.notified_approved = True
            rc = reactions.get(
                "approved_and_green", DEFAULT_REACTIONS["approved_and_green"]
            )
            logger.info(
                "[pr-watcher %s] APPROVED + green CI → action=%s",
                task_id,
                rc.action,
            )
            if notifier:
                await notifier.notify(
                    "approved_and_green",
                    {
                        "task_id": task_id,
                        "pr_url": pr_url,
                        "auto_merge": rc.action == "auto-merge",
                    },
                )
            if rc.action == "auto-merge":
                try:
                    await _auto_merge(repo_root, pr_url)
                    return
                except Exception as e:  # noqa: BLE001
                    logger.warning(
                        "[pr-watcher %s] auto-merge failed: %s", task_id, e
                    )
            elif on_approved_and_green is not None:
                try:
                    await on_approved_and_green()
                except Exception as e:  # noqa: BLE001
                    logger.warning(
                        "[pr-watcher %s] on_approved_and_green failed: %s",
                        task_id,
                        e,
                    )

        await asyncio.sleep(poll_interval_s)


async def _auto_merge(repo_root: Path, pr_url: str) -> None:
    """``gh pr merge <url> --squash --delete-branch``."""
    proc = await asyncio.create_subprocess_exec(
        "gh",
        "pr",
        "merge",
        pr_url,
        "--squash",
        "--delete-branch",
        "--auto",
        cwd=str(repo_root),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout_b, stderr_b = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(
            f"gh pr merge failed: {stderr_b.decode('utf-8', errors='replace')}"
        )


__all__ = [
    "DEFAULT_REACTIONS",
    "ReactionConfig",
    "ReactionEvent",
    "WatcherState",
    "parse_reactions_yaml",
    "watch_pr",
]
