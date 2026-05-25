"""orchX — параллельный мультиагентный рой, независимый от kilo runtime.

Пакет содержит:

- :mod:`orchx.cli` — argparse-обёртка с подкомандами ``plan``/``run``/``all``.
- :mod:`orchx.orchestrator` — DAG/фазы/retry/replan/PR (вся бизнес-логика).
- :mod:`orchx.agent` — in-process воркер, читающий ``orchx/prompts/orchX-*.md``
  и общающийся с LLM напрямую через OpenAI-совместимый Proxy.

Старый ``orchx/dispatcher/`` мигрировал сюда; ``orchx/`` в корне репо
теперь — runtime data dir (логи, runs, _pending). Схемы и шаблоны переехали
внутрь пакета (``orchx/schemas/``).
"""

from __future__ import annotations

try:
    from importlib.metadata import PackageNotFoundError
    from importlib.metadata import version as _pkg_version

    try:
        __version__ = _pkg_version("orchx")
    except PackageNotFoundError:
        __version__ = "0.0.0+local"
except ImportError:  # pragma: no cover — Python <3.8 не поддерживается
    __version__ = "0.0.0+local"

__all__ = ["__version__"]
