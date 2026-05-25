"""Append-only журнал прогона (orchX-log).

Выделено из ``orchx.orchestrator.core`` (P0.1).
"""

from __future__ import annotations

import logging
import time

from .context import OrchXContext

logger = logging.getLogger("orchx.orchestrator")


def orchx_log(ctx: OrchXContext, msg: str) -> None:
    """Append-only журнал роя.

    Пишется в ``ctx.log_file`` (orchx/runs/<task_id>/orchx.log) и
    параллельно — в стандартный ``logging`` (для CLI/TUI/dashboard).
    """
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n"
    with ctx.log_file.open("a", encoding="utf-8") as f:
        f.write(line)
    logger.info(msg)


# Старое имя со подчёркиванием — для backward-compat внутри core.py.
_orchX_log = orchx_log

__all__ = ["orchx_log", "_orchX_log", "logger"]
