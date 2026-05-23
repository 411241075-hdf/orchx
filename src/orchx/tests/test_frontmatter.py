"""Тесты парсера ``.kilo/agent/orchX-*.md``."""

from __future__ import annotations

from pathlib import Path

import pytest

from orchx.agent.frontmatter import load_agent_spec, parse_agent_markdown

REPO_ROOT = Path(__file__).resolve().parents[2]
ALL_ROLES = (
    "planner",
    "architect",
    "implementer",
    "tester",
    "debugger",
    "merger",
    "reviewer",
)


@pytest.mark.parametrize("role", ALL_ROLES)
def test_all_orchx_agents_parse(role: str) -> None:
    """Каждый из 7 реальных agent-файлов должен парситься без ошибок."""
    spec = load_agent_spec(role, REPO_ROOT)
    assert spec.name == f"orchX-{role}"
    assert spec.role == role
    assert spec.description, f"{role}: пустой description"
    assert spec.body, f"{role}: пустой body"
    assert spec.max_steps > 0


def test_implementer_permissions_match_file() -> None:
    spec = load_agent_spec("implementer", REPO_ROOT)
    # Из файла: bash включает "git status*: allow" и "*: deny".
    ok, _ = spec.permissions.bash_allowed("git status")
    assert ok is True
    ok, _ = spec.permissions.bash_allowed("rm -rf /")
    assert ok is False
    # edit: allow (булева форма).
    assert spec.permissions.edit is True


def test_planner_has_path_gated_edit() -> None:
    spec = load_agent_spec("planner", REPO_ROOT)
    # planner может редактировать только plan.json в _pending/runs.
    assert spec.permissions.edit_allowed(".orchx/_pending/plan.json") is True
    assert spec.permissions.edit_allowed(".orchx/runs/abc/plan.json") is True
    assert spec.permissions.edit_allowed("backend/main.py") is False


def test_parse_synthetic_no_frontmatter() -> None:
    spec = parse_agent_markdown(
        "Just a markdown body, no frontmatter.",
        role="test",
        name="orchX-test",
    )
    assert spec.body.startswith("Just a markdown body")
    assert spec.description == ""
    assert spec.max_steps == 80  # default


def test_parse_synthetic_full_frontmatter() -> None:
    text = (
        "---\n"
        "description: synthetic agent\n"
        "steps: 12\n"
        "permission:\n"
        "  read: allow\n"
        "  edit: deny\n"
        "  bash:\n"
        '    "ls *": allow\n'
        '    "*": deny\n'
        "---\n"
        "\n"
        "Body content here.\n"
    )
    spec = parse_agent_markdown(text, role="syn", name="orchX-syn")
    assert spec.description == "synthetic agent"
    assert spec.max_steps == 12
    assert spec.permissions.edit is False
    ok, _ = spec.permissions.bash_allowed("ls -la")
    assert ok is True
    assert spec.body.strip() == "Body content here."
