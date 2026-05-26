"""Тесты парсера ``orchX-<role>.md`` (frontmatter + body).

Тесты используют дефолтные шаблоны промптов, шиппящиеся с пакетом
(`templates/prompts/`), через :class:`OrchXRuntime.from_project_root`,
указывающий на корень тестового репо. Проверяется, что 7 базовых ролей
парсятся, ACL-блоки распознаются, и каскадный поиск отдаёт первый
существующий файл.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from orchx.agent.frontmatter import load_agent_spec, parse_agent_markdown
from orchx.runtime import OrchXRuntime

# project_root для тестов = корень нового orchx-репо. Дефолтные промпты
# лежат в ``<package>/templates/prompts/``, и runtime автоматически их
# подхватит как fallback (в проектной .orchx/prompts/ ничего нет).
REPO_ROOT = Path(__file__).resolve().parents[3]
ALL_ROLES = (
    "planner",
    "architect",
    "implementer",
    "debugger",
    "merger",
    "reviewer",
)


def _runtime() -> OrchXRuntime:
    return OrchXRuntime.from_project_root(REPO_ROOT)


@pytest.mark.parametrize("role", ALL_ROLES)
def test_all_orchx_agents_parse(role: str) -> None:
    """Каждый из 6 базовых agent-файлов должен парситься без ошибок.

    Раньше было 7 ролей; ``tester`` объединён с ``implementer`` —
    тесты пишет тот же агент, что реализует код.
    """
    spec = load_agent_spec(role, _runtime())
    assert spec.name == f"orchX-{role}"
    assert spec.role == role
    assert spec.description, f"{role}: пустой description"
    assert spec.body, f"{role}: пустой body"
    assert spec.max_steps > 0


def test_tester_role_no_longer_shipped() -> None:
    """``orchX-tester.md`` удалён — попытка загрузить должна падать."""
    with pytest.raises(FileNotFoundError):
        load_agent_spec("tester", _runtime())


def test_implementer_permissions_match_file() -> None:
    """Permission-блок implementer'а корректно парсится из frontmatter'а."""
    spec = load_agent_spec("implementer", _runtime())
    # Из файла: bash включает "git status*: allow" и "*: deny".
    ok, _ = spec.permissions.bash_allowed("git status")
    assert ok is True
    ok, _ = spec.permissions.bash_allowed("rm -rf /")
    assert ok is False
    # edit: allow (булева форма).
    assert spec.permissions.edit is True


def test_planner_has_path_gated_edit() -> None:
    """Planner может писать только в ``.orchx/_pending|runs/.../plan.json``."""
    spec = load_agent_spec("planner", _runtime())
    assert spec.permissions.edit_allowed(".orchx/_pending/plan.json") is True
    assert spec.permissions.edit_allowed(".orchx/runs/abc/plan.json") is True
    # Любой код вне плана — запрещено.
    assert spec.permissions.edit_allowed("src/main.py") is False


def test_load_agent_spec_uses_project_override(tmp_path: Path) -> None:
    """Если в ``.orchx/prompts/`` есть свой файл — он выигрывает у дефолта.

    Регрессия на каскад поиска: пакет должен сначала проверить override
    из проекта пользователя, а только потом упасть в дефолт.
    """
    # Создаём fake-project с git и кастомным promp'том.
    import subprocess

    project = tmp_path / "fake-repo"
    project.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=project, check=True)
    prompts_dir = project / ".orchx" / "prompts"
    prompts_dir.mkdir(parents=True)
    (prompts_dir / "orchX-implementer.md").write_text(
        "---\n"
        "description: project-override\n"
        "steps: 7\n"
        "permission:\n"
        "  read: allow\n"
        "---\n"
        "\n"
        "Custom body for this project.\n",
        encoding="utf-8",
    )

    runtime = OrchXRuntime.from_project_root(project)
    spec = load_agent_spec("implementer", runtime)
    assert spec.description == "project-override"
    assert spec.max_steps == 7
    assert "Custom body" in spec.body
    # source_path должен указывать на override, а не на пакет.
    assert spec.source_path is not None
    assert ".orchx/prompts/orchX-implementer.md" in str(spec.source_path)


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
