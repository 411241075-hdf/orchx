"""orchX — параллельный мультиагентный рой, независимый от kilo runtime.

Пакет содержит:

- :mod:`orchx.cli` — argparse-обёртка с подкомандами ``plan``/``run``/``all``.
- :mod:`orchx.orchestrator` — DAG/фазы/retry/replan/PR (вся бизнес-логика).
- :mod:`orchx.agent` — in-process воркер, читающий ``.kilo/agent/orchX-*.md``
  и общающийся с LLM напрямую через OpenAI-совместимый Proxy.

Старый ``.orchX/dispatcher/`` мигрировал сюда; ``.orchx/`` в корне репо
теперь — runtime data dir (логи, runs, _pending). Схемы и шаблоны переехали
внутрь пакета (``orchx/schemas/``).
"""

from __future__ import annotations
