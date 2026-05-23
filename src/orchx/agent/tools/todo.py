"""Лёгкий TodoWrite — для самоорганизации LLM-воркера.

Список хранится в ``ctx.todos`` (list[dict]). Перезаписывается полностью
на каждый вызов, как у kilo.
"""

from __future__ import annotations

from typing import Any

from . import Tool, ToolContext, ToolResult


class TodoWriteTool(Tool):
    """Перезаписать in-memory TODO-список воркера."""

    name = "todowrite"
    description = (
        "Replace the worker's todo list. Use this to plan multi-step work "
        "and track progress (states: pending / in_progress / completed / "
        "cancelled). Only one item should be `in_progress` at a time."
    )
    parameters = {
        "type": "object",
        "properties": {
            "todos": {
                "type": "array",
                "description": "Full updated todo list (replaces previous list).",
                "items": {
                    "type": "object",
                    "properties": {
                        "content": {"type": "string"},
                        "status": {
                            "type": "string",
                            "enum": [
                                "pending",
                                "in_progress",
                                "completed",
                                "cancelled",
                            ],
                        },
                        "priority": {
                            "type": "string",
                            "enum": ["low", "medium", "high"],
                        },
                    },
                    "required": ["content", "status", "priority"],
                },
            },
        },
        "required": ["todos"],
    }

    async def run(
        self,
        ctx: ToolContext,
        *,
        todos: list[dict[str, Any]] | None = None,
    ) -> ToolResult:
        """Заменить in-memory TODO-список воркера (см. описание класса)."""
        items = todos or []
        if not isinstance(items, list):
            return ToolResult(content="todos must be a list", is_error=True)
        clean: list[dict[str, Any]] = []
        for it in items:
            if not isinstance(it, dict):
                continue
            clean.append(
                {
                    "content": str(it.get("content", "")),
                    "status": str(it.get("status", "pending")),
                    "priority": str(it.get("priority", "medium")),
                }
            )
        ctx.todos = clean
        in_progress = [t for t in clean if t["status"] == "in_progress"]
        completed = [t for t in clean if t["status"] == "completed"]
        ctx.activity(
            f"todo: {len(clean)} items ({len(completed)} done, {len(in_progress)} in progress)"
        )
        summary = (
            f"Updated todo list: {len(clean)} item(s); "
            f"{len(completed)} completed, {len(in_progress)} in progress."
        )
        return ToolResult(content=summary)
