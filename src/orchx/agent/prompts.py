"""Сборка системного промпта воркера.

System prompt = шапка с environment + список доступных tool'ов и permissions
+ markdown-body из ``.kilo/agent/orchX-<role>.md`` (без frontmatter).
"""

from __future__ import annotations

import platform
from datetime import date
from pathlib import Path

from .frontmatter import AgentSpec
from .permissions import describe_permissions


def build_system_prompt(
    spec: AgentSpec,
    *,
    cwd: Path,
    repo_root: Path,
    tool_names: list[str],
) -> str:
    """Собрать system prompt для одного воркера."""
    tools_line = ", ".join(tool_names) if tool_names else "(none)"
    return (
        f"You are {spec.name}, a worker in the orchX swarm.\n"
        f"\n"
        f"# Environment\n"
        f"- Working directory: {cwd}\n"
        f"- Repo root: {repo_root}\n"
        f"- Today: {date.today().isoformat()}\n"
        f"- Platform: {platform.system()}\n"
        f"\n"
        f"# Available tools\n"
        f"You have these tools: {tools_line}.\n"
        f"Use them to inspect and modify the repository. Always prefer the "
        f"dedicated tool over a bash equivalent (e.g. `read` instead of "
        f"`cat`, `glob` instead of `find`, `grep` instead of shelling out).\n"
        f"\n"
        f"You DO NOT have access to MCP servers, sub-agents (`task` tool), "
        f"web fetch/search, or any kilo-specific skills. If your role's "
        f"prompt mentions tools that aren't in the list above, ignore those "
        f"references.\n"
        f"\n"
        f"# Permissions\n"
        f"{describe_permissions(spec.permissions)}\n"
        f"\n"
        f"# Task contract\n"
        f"Your task contract is in `orchx/task.md` inside the working "
        f"directory. Read it first. Write your final result to the JSON path "
        f"it specifies. After writing, finish with the short reply that the "
        f"role section instructs (e.g. `done` for implementer/tester, "
        f"`plan written` for planner).\n"
        f"\n"
        f"# Agent role\n"
        f"{spec.body}"
    )
