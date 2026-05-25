"""Тесты для опционального ``plan.tracker_task_id``.

Поле хранит composite id из внешнего трекера (например, GitHub Projects
``"PVTI_lAHO...:114"``). Используется orchestrator'ом для
``tracker.update_status`` — двинуть карточку, оставить коммент в issue.
В отличие от ``task_id`` (slug), здесь допустимо двоеточие — это не slug
для git-веток.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from orchx.models import load_plan

_VALID_FLAT_PLAN: dict = {
    "task_id": "demo",
    "base_branch": "main",
    "summary": "Demo plan.",
    "tasks": [
        {
            "id": "t1",
            "agent": "implementer",
            "goal": "Implement demo widget.",
            "file_scope": ["src/demo/**"],
            "acceptance": [
                {
                    "type": "command",
                    "command": "pytest -q tests/demo",
                    "description": "Demo tests pass.",
                }
            ],
        }
    ],
}


def _write_plan(tmp_path: Path, extra: dict | None = None) -> Path:
    raw = dict(_VALID_FLAT_PLAN)
    if extra:
        raw.update(extra)
    p = tmp_path / "plan.json"
    p.write_text(json.dumps(raw), encoding="utf-8")
    return p


def test_load_plan_default_tracker_task_id_empty(tmp_path: Path) -> None:
    plan = load_plan(_write_plan(tmp_path))
    assert plan.tracker_task_id == ""


def test_load_plan_keeps_explicit_composite_id(tmp_path: Path) -> None:
    composite = "PVTI_lAHODwqj7M4BNqAMzgtwHEw:114"
    plan = load_plan(_write_plan(tmp_path, {"tracker_task_id": composite}))
    # Двоеточие специально — это композитный id для GitHub Projects, а не slug.
    assert plan.tracker_task_id == composite
    # task_id остаётся slug-формата (не должно затрагиваться).
    assert plan.task_id == "demo"


def test_load_plan_rejects_non_string_tracker_task_id(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="tracker_task_id"):
        load_plan(_write_plan(tmp_path, {"tracker_task_id": 12345}))
