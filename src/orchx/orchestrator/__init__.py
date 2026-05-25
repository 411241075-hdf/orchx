"""Главный оркестратор роя orchX — пакетный фасад.

Пакет разбит на сегменты для уменьшения cognitive bloat (исходный
``orchestrator.py`` был 2671 строк, см. P0.1):

* :mod:`orchx.orchestrator.context` — dataclass'ы состояния
  (``OrchXConfig``, ``OrchXContext``, ``TaskState``, ``PhaseState``,
  ``AttemptInfo``).
* :mod:`orchx.orchestrator.logging_utils` — append-only журнал роя.
* :mod:`orchx.orchestrator.core` — бизнес-логика прогона (setup,
  phases, retry, merge, reviewer, replan, supervisor, summary).
  В будущем будет дополнительно расщеплён на ``phases.py`` /
  ``retry.py`` / ``merge.py`` / ``review.py`` / ``supervisor.py``
  (см. roadmap).

Публичный API (стабилен между релизами):

* :func:`run_orchX` — точка входа из CLI.
* :class:`OrchXConfig` — настройки прогона.
* :class:`OrchXContext` — runtime-контекст (для plugin'ов и тестов).
* :class:`TaskState`, :class:`PhaseState`, :class:`AttemptInfo` —
  state-классы (для observability / dashboard'а).
"""

from __future__ import annotations

# Реэкспорт state-моделей: они импортируются как из ``orchx.orchestrator``,
# так и из ``orchx.orchestrator.context``.
from .context import (
    AttemptInfo,
    OrchXConfig,
    OrchXContext,
    PhaseState,
    TaskState,
)

# Реэкспорт публичной функции запуска роя.
from .core import run_orchX

__all__ = [
    "AttemptInfo",
    "OrchXConfig",
    "OrchXContext",
    "PhaseState",
    "TaskState",
    "run_orchX",
]
