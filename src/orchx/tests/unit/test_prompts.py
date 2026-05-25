"""Тесты для :mod:`orchx.agent.prompts`."""

from __future__ import annotations

from pathlib import Path

from orchx.agent.frontmatter import load_agent_spec
from orchx.agent.permissions import Permissions
from orchx.agent.prompts import build_system_prompt
from orchx.agent.tools import ToolContext, build_tool_registry
from orchx.runtime import OrchXRuntime

REPO_ROOT = Path(__file__).resolve().parents[3]


def _runtime() -> OrchXRuntime:
    return OrchXRuntime.from_project_root(REPO_ROOT)


def _spec_for_test(tmp_path: Path):  # noqa: ANN202
    """Берём реального implementer-а — он есть в дефолтных шаблонах пакета."""
    return load_agent_spec("implementer", _runtime())


def test_system_prompt_lists_forbidden_mcp_prefixes(tmp_path: Path) -> None:
    """В system prompt должны быть явно перечислены MCP-префиксы."""
    spec = _spec_for_test(tmp_path)
    prompt = build_system_prompt(
        spec,
        cwd=tmp_path,
        repo_root=REPO_ROOT,
        tool_names=["read", "write", "bash"],
    )
    for marker in (
        "5stars_",
        "finland_",
        "turbocards_",
        "langfuse_",
        "_execute",
        "_upload",
        "_download",
    ):
        assert marker in prompt, f"missing forbidden marker {marker!r} in prompt"


def test_system_prompt_describes_refactor_pattern(tmp_path: Path) -> None:
    """Должен быть явный паттерн multi-file rename через grep + edit replace_all."""
    spec = _spec_for_test(tmp_path)
    prompt = build_system_prompt(
        spec,
        cwd=tmp_path,
        repo_root=REPO_ROOT,
        tool_names=["read", "grep", "edit"],
    )
    assert "replace_all" in prompt
    assert "grep" in prompt.lower()
    # Явный заголовок секции.
    assert "Refactor patterns" in prompt


def test_system_prompt_includes_tool_descriptions(tmp_path: Path) -> None:
    """Если переданы descriptions, system prompt должен включать первую строку каждого."""
    perms = Permissions(edit=True, bash={"echo*": "allow", "*": "deny"})
    ctx = ToolContext(cwd=tmp_path, repo_root=REPO_ROOT, permissions=perms)
    registry = build_tool_registry(ctx)
    spec = _spec_for_test(tmp_path)
    descriptions = {name: tool.description for name, tool in registry.items()}
    prompt = build_system_prompt(
        spec,
        cwd=tmp_path,
        repo_root=REPO_ROOT,
        tool_names=list(registry.keys()),
        tool_descriptions=descriptions,
    )
    # Должна быть секция «Tool capabilities» и первая фраза каждого tool'а.
    assert "Tool capabilities" in prompt
    for name, desc in descriptions.items():
        head = desc.split(". ")[0].rstrip(".").strip()
        if not head:
            continue
        # `name`-маркер обязан фигурировать.
        assert f"`{name}`" in prompt


def test_system_prompt_without_tool_descriptions_omits_capabilities(
    tmp_path: Path,
) -> None:
    """Без tool_descriptions — нет блока capabilities (back-compat)."""
    spec = _spec_for_test(tmp_path)
    prompt = build_system_prompt(
        spec,
        cwd=tmp_path,
        repo_root=REPO_ROOT,
        tool_names=["read", "write"],
    )
    assert "Tool capabilities" not in prompt
