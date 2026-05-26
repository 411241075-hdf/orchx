"""Тесты на silent rewrite устаревших role-имён в plan.json.

См. :data:`orchx.models.DEPRECATED_AGENT_ALIASES`. Роли ``tester`` и
``implementer`` объединены: тесты пишет тот же агент, что реализует код.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from orchx.models import (
    DEPRECATED_AGENT_ALIASES,
    VALID_AGENTS,
    load_plan,
)


def _write_plan(tmp_path: Path, agent: str) -> Path:
    plan = {
        "task_id": "demo",
        "base_branch": "main",
        "summary": "Demo plan with deprecated agent.",
        "tasks": [
            {
                "id": "t1",
                "agent": agent,
                "goal": "Write tests for the demo widget.",
                "file_scope": ["tests/demo/**"],
                "acceptance": [
                    {
                        "type": "file_exists",
                        "path": "tests/demo/test_widget.py",
                        "description": "test file exists",
                    }
                ],
            }
        ],
    }
    p = tmp_path / "plan.json"
    p.write_text(json.dumps(plan), encoding="utf-8")
    return p


def test_tester_is_rewritten_to_implementer(tmp_path: Path, caplog) -> None:
    """plan.json с ``agent: "tester"`` загружается, agent переписан в implementer.

    Это backward-compat: уже сохранённые run'ы с tester-задачами
    продолжают работать, planner LLM по инерции пишущий "tester" —
    тоже не валится.
    """
    p = _write_plan(tmp_path, agent="tester")
    with caplog.at_level(logging.WARNING):
        plan = load_plan(p)
    assert plan.tasks[0].agent == "implementer"
    # Должен быть warning в логе.
    assert any(
        "deprecated agent" in rec.message.lower()
        and "'tester'" in rec.message.lower()
        for rec in caplog.records
    )


def test_unknown_agent_still_fails(tmp_path: Path) -> None:
    """Произвольное несуществующее имя по-прежнему даёт ValueError.

    Backward-compat работает только для известных alias'ов из
    :data:`DEPRECATED_AGENT_ALIASES`.
    """
    import pytest

    p = _write_plan(tmp_path, agent="random_role")
    with pytest.raises(ValueError, match="invalid agent"):
        load_plan(p)


def test_tester_not_in_valid_agents_anymore() -> None:
    """``tester`` больше не входит в VALID_AGENTS."""
    assert "tester" not in VALID_AGENTS
    assert "implementer" in VALID_AGENTS


def test_deprecated_aliases_map_consistent() -> None:
    """Каждое значение alias-а должно быть валидным агентом."""
    for old, new in DEPRECATED_AGENT_ALIASES.items():
        assert new in VALID_AGENTS, (
            f"deprecated alias {old!r} maps to invalid agent {new!r}"
        )
        assert old not in VALID_AGENTS, (
            f"deprecated alias {old!r} should not be in VALID_AGENTS"
        )
