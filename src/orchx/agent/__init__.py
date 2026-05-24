"""In-process воркер orchX.

Заменяет спавн kilo CLI: парсит markdown-агентов из ``orchx/prompts/orchX-*.md``,
собирает системный промпт + tool-схемы, гоняет цикл «LLM → tool → LLM» против
OpenAI-совместимого Proxy.

Точка входа — :func:`orchx.agent.worker.run_agent`.
"""

from __future__ import annotations
