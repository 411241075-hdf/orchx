"""Реестр инструментов воркера + общие типы.

Каждый tool — экземпляр :class:`Tool`. Реестр — это просто dict, который
:func:`build_tool_registry` возвращает с привязкой к :class:`ToolContext`
(cwd, permissions, todos, activity-callback).

Tool-схема в формате OpenAI tool-calling строится из ``name``/``description``/
``parameters``-полей через :func:`to_openai_schema`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..permissions import Permissions


@dataclass
class ToolContext:
    """Per-worker состояние, доступное всем tool-инстансам."""

    cwd: Path
    """Корень worktree — все относительные пути резолвятся отсюда."""

    repo_root: Path
    """Корень репозитория (для info-сообщений; tools работают в ``cwd``)."""

    permissions: Permissions
    activity: Callable[[str], None] = field(default=lambda _: None)
    """Callback для TUI: «воркер сейчас вызывает tool X / читает файл Y»."""

    todos: list[dict[str, Any]] = field(default_factory=list)
    """In-memory TodoWrite — сбрасывается при каждом ходе LLM."""


@dataclass
class ToolResult:
    """Что улетит обратно в LLM в качестве ``role=tool`` сообщения."""

    content: str
    is_error: bool = False


class Tool:
    """Базовый класс tool'а. Подклассы переопределяют ``run``."""

    name: str = ""
    description: str = ""
    parameters: dict[str, Any] = {}
    """JSON Schema для ``tools=[…]`` в OpenAI Chat Completions API."""

    permission_attr: str | None = None
    """Имя атрибута :class:`Permissions`, которое гейтит tool.

    ``None`` — не гейтится (например, ``todowrite``).
    Для path/command-gated tool'ов (``edit``, ``bash``) gating делается
    внутри ``run``, а этот атрибут пуст.
    """

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        """Выполнить tool. Подклассы переопределяют."""
        raise NotImplementedError


def to_openai_schema(tool: Tool) -> dict[str, Any]:
    """Сконвертировать Tool в OpenAI ``tools=[…]``-элемент."""
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.parameters,
        },
    }


def permission_denied(
    *,
    tool: str,
    target: str,
    reason: str,
    hint: str | None = None,
) -> ToolResult:
    """Унифицированный «Permission denied»-ответ для всех tool'ов.

    Хелпер даёт стабильный prefix «Permission denied:», который ловят
    существующие тесты, и одинаковый формат тела сообщения. Bash-tool
    дополняет hint allow-list-листингом отдельно — но через тот же
    конструктор, чтобы prefix не разъезжался.

    Args:
        tool: Имя tool'а, который отказал (``"read"``, ``"bash"`` и т.д.).
        target: На что был отказ — путь, команда, URL.
        reason: Короткая причина (одно предложение, без точки).
        hint: Опциональная подсказка LLM (что попробовать вместо этого).

    Returns:
        ``ToolResult`` с ``is_error=True`` и форматированным сообщением.
    """
    body = f"Permission denied: {tool} on {target} — {reason}."
    if hint:
        body += f"\nHint: {hint}"
    return ToolResult(content=body, is_error=True)


def build_tool_registry(ctx: ToolContext) -> dict[str, Tool]:
    """Построить реестр tool'ов для одного воркера.

    Tool'ы сами отфильтрованы по ``ctx.permissions``: если read=False, ни
    ``read``, ни ``glob`` не попадут в реестр (LLM их не увидит вообще).
    Path/command-gated tool'ы (``edit``, ``bash``) попадают всегда, но их
    ``run`` отвечает 403-style ошибкой при denied-аргументах.

    Args:
        ctx: Контекст воркера.

    Returns:
        Маппинг ``name → Tool``. Используется и для генерации OpenAI-схемы,
        и для диспатча tool-вызовов.
    """
    from .fs import EditTool, GlobTool, ReadTool, WriteTool
    from .search import CodeSearchTool, GrepTool
    from .shell import BashTool
    from .task import TaskTool
    from .todo import TodoWriteTool

    registry: dict[str, Tool] = {}
    p = ctx.permissions
    if p.read:
        registry["read"] = ReadTool()
    if p.glob:
        registry["glob"] = GlobTool()
    if p.grep:
        registry["grep"] = GrepTool()
    if p.codesearch:
        registry["codesearch"] = CodeSearchTool()
    # edit/write — path-gated; всегда в реестре, deny внутри run().
    # Полный deny (edit: false) — убираем целиком, чтобы LLM не видел tool.
    if not (isinstance(p.edit, bool) and not p.edit):
        registry["write"] = WriteTool()
        registry["edit"] = EditTool()
    # bash — командный allow-list; полный deny ("*": "deny") — убираем.
    if any(action == "allow" for action in p.bash.values()):
        registry["bash"] = BashTool()
    if p.task:
        registry["task"] = TaskTool()
    if p.webfetch:
        from .web import WebFetchTool

        registry["webfetch"] = WebFetchTool()
    # P1.6: LSP-tools (symbol intelligence). Opt-in через ``lsp: allow``.
    if p.lsp:
        from .symbols import FindReferencesTool, FindSymbolTool, RenameSymbolTool

        registry["find_symbol"] = FindSymbolTool()
        registry["find_references"] = FindReferencesTool()
        registry["rename_symbol"] = RenameSymbolTool()
    # P1.7: Browser tool (Playwright). Требует extras orchx[browser].
    if p.browser:
        from .browser import BrowserTool

        registry["browser"] = BrowserTool()
    registry["todowrite"] = TodoWriteTool()
    return registry
