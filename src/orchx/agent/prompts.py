"""Сборка системного промпта воркера.

System prompt = шапка с environment + список доступных tool'ов и permissions
+ markdown-body из ``orchx/prompts/orchX-<role>.md`` (без frontmatter).
"""

from __future__ import annotations

import platform
from datetime import date
from pathlib import Path

from .frontmatter import AgentSpec
from .permissions import describe_permissions

# Префиксы tool-имён, которые LLM регулярно пытается вызывать, но которых
# в orchX-runtime НЕ существует (это MCP-серверы kilo, доступные только в
# интерактивной CLI). Каждая попытка — потерянный step. Список явно
# проговаривается в system prompt, чтобы модель не гадала.
_FORBIDDEN_TOOL_PREFIXES = (
    "5stars_",
    "finland_",
    "turbocards_",
    "langfuse_",
    "images_",
    "serena_",
)

# Суффиксы для MCP-tool'ов, которые могут жить под разными префиксами
# (например, любой *_execute / *_upload — это всегда удалённый MCP).
_FORBIDDEN_TOOL_SUFFIXES = (
    "_execute",
    "_upload",
    "_download",
    "_analyze_image",
    "_generate_image",
)


def build_system_prompt(
    spec: AgentSpec,
    *,
    cwd: Path,
    repo_root: Path,
    tool_names: list[str],
    tool_descriptions: dict[str, str] | None = None,
) -> str:
    """Собрать system prompt для одного воркера.

    Args:
        spec: Распарсенная спецификация роли (frontmatter + body).
        cwd: Рабочая директория воркера (его worktree).
        repo_root: Корень репозитория.
        tool_names: Имена tool'ов, которые попали в реестр.
        tool_descriptions: Опциональный маппинг ``name → description`` для
            генерации блока «Tool capabilities». Если не задан — блок
            опускается (но имена всё равно перечисляются).
    """
    tools_line = ", ".join(tool_names) if tool_names else "(none)"
    forbidden_prefixes = ", ".join(f"`{p}`" for p in _FORBIDDEN_TOOL_PREFIXES)
    forbidden_suffixes = ", ".join(f"`{s}`" for s in _FORBIDDEN_TOOL_SUFFIXES)

    capabilities_block = ""
    if tool_descriptions:
        lines = ["# Tool capabilities"]
        for name in tool_names:
            desc = tool_descriptions.get(name, "").strip()
            if not desc:
                continue
            # Берём только первое предложение/строку для компактности.
            head = desc.split(". ")[0].rstrip(".").strip()
            lines.append(f"- `{name}` — {head}.")
        capabilities_block = "\n".join(lines) + "\n\n"

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
        f"You DO NOT have access to MCP servers, sub-agents (`task` tool "
        f"unless explicitly listed above), web fetch/search, or any "
        f"kilo-specific skills. Tool names with these prefixes DO NOT EXIST "
        f"in this runtime: {forbidden_prefixes}. Names ending with any of "
        f"these suffixes are also unavailable: {forbidden_suffixes}. "
        f"Calling them produces a tool-not-found error and wastes a step. "
        f"If the role's prompt below references such a tool, ignore that "
        f"reference and use only the tools listed above.\n"
        f"\n"
        f"# Refactor patterns\n"
        f"For multi-file rename / symbol-rename refactors, there is no LSP "
        f"`rename_symbol` tool. Use this sequence instead:\n"
        f"1. `grep` (or `codesearch`) for ALL occurrences of the old name "
        f"across the relevant paths;\n"
        f"2. group the hits by file;\n"
        f"3. for each file, issue ONE `edit` call with `replace_all=true` "
        f"and a sufficiently unique `old_string` (include enough context "
        f"to avoid false positives in comments/strings);\n"
        f"4. re-run `grep` after to confirm zero remaining hits.\n"
        f"This is significantly cheaper than 15 separate point-edits and "
        f"avoids the unique-match error path of single-occurrence `edit`.\n"
        f"\n"
        f"{capabilities_block}"
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
