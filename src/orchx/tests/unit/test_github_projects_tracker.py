"""Unit-тесты для :mod:`orchx.plugins.trackers.github_projects`.

Сетевые вызовы (``gh api graphql``) мокаются через
``orchx.plugins.trackers.github_projects._gh_graphql``.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from orchx.plugins import KanbanTrackerPlugin, TrackerPlugin
from orchx.plugins.trackers.github_projects import (
    GithubProjectsTracker,
    GithubTask,
)

# ---------------------------------------------------------------------------
# Базовые свойства класса
# ---------------------------------------------------------------------------


def test_implements_tracker_protocol():
    t = GithubProjectsTracker(project_owner="acme", project_number=1)
    assert isinstance(t, TrackerPlugin)
    assert isinstance(t, KanbanTrackerPlugin)
    assert t.name == "github-projects"


def test_split_task_id_composite():
    pid, num = GithubProjectsTracker._split_task_id("PVTI_lAH:42")
    assert pid == "PVTI_lAH"
    assert num == "42"


def test_split_task_id_legacy_issue_number():
    pid, num = GithubProjectsTracker._split_task_id("42")
    assert pid == ""
    assert num == "42"


def test_github_task_from_item_builds_composite_id():
    item = {
        "id": "PVTI_xxx",
        "content": {
            "number": 17,
            "title": "Refactor X",
            "body": "Do it",
            "url": "https://github.com/owner/repo/issues/17",
        },
    }
    t = GithubTask.from_item(item)
    assert t.id == "PVTI_xxx:17"
    assert t.title == "Refactor X"
    assert t.body == "Do it"
    assert t.url == "https://github.com/owner/repo/issues/17"
    assert t.project_item_id == "PVTI_xxx"
    assert t.issue_number == "17"


# ---------------------------------------------------------------------------
# Lazy metadata resolve + Kanban API (mock _gh_graphql)
# ---------------------------------------------------------------------------


@pytest.fixture
def project_meta_response() -> dict[str, Any]:
    """Стандартный ответ projectV2 со statusField + 3 опции."""
    return {
        "data": {
            "organization": {
                "projectV2": {
                    "id": "PVT_org_1",
                    "fields": {
                        "nodes": [
                            {
                                "id": "PVTF_status",
                                "name": "Status",
                                "options": [
                                    {"id": "opt_ready", "name": "Ready"},
                                    {"id": "opt_progress", "name": "In progress"},
                                    {"id": "opt_done", "name": "Done"},
                                ],
                            },
                        ]
                    },
                }
            }
        }
    }


@pytest.fixture
def items_response() -> dict[str, Any]:
    """Ответ с 3 items: один в Ready, один in progress, один без статуса."""
    return {
        "data": {
            "node": {
                "items": {
                    "nodes": [
                        {
                            "id": "PVTI_a",
                            "fieldValues": {
                                "nodes": [
                                    {
                                        "optionId": "opt_ready",
                                        "field": {"id": "PVTF_status"},
                                    }
                                ]
                            },
                            "content": {
                                "number": 1,
                                "title": "Task A",
                                "body": "A body",
                                "url": "https://x/1",
                                "state": "OPEN",
                            },
                        },
                        {
                            "id": "PVTI_b",
                            "fieldValues": {
                                "nodes": [
                                    {
                                        "optionId": "opt_progress",
                                        "field": {"id": "PVTF_status"},
                                    }
                                ]
                            },
                            "content": {
                                "number": 2,
                                "title": "Task B (in progress)",
                                "body": "B body",
                                "url": "https://x/2",
                                "state": "OPEN",
                            },
                        },
                        {
                            "id": "PVTI_c",
                            "fieldValues": {"nodes": []},
                            "content": {
                                "number": 3,
                                "title": "Untriaged",
                                "body": "",
                                "url": "https://x/3",
                                "state": "OPEN",
                            },
                        },
                    ]
                }
            }
        }
    }


@pytest.fixture
def tracker_with_mock(
    monkeypatch, project_meta_response, items_response
) -> tuple[GithubProjectsTracker, AsyncMock]:
    """GithubProjectsTracker + замокированный ``_gh_graphql``."""
    calls: list[tuple[str, dict[str, Any]]] = []

    async def fake_graphql(query: str, variables: dict[str, Any]) -> dict[str, Any]:
        calls.append((query, variables))
        if "projectV2(number:" in query or "projectV2(number: " in query:
            return project_meta_response
        if "items(first:" in query:
            return items_response
        if "updateProjectV2ItemFieldValue" in query:
            return {"data": {"updateProjectV2ItemFieldValue": {"projectV2Item": {"id": variables["itemId"]}}}}
        raise AssertionError(f"unexpected graphql query: {query[:80]}")

    monkeypatch.setattr(
        "orchx.plugins.trackers.github_projects._gh_graphql", fake_graphql
    )
    tracker = GithubProjectsTracker(project_owner="orgs/acme", project_number=7)
    # Возвращаем mock-обертку для assert_called_with не нужно — нам нужны calls.
    return tracker, calls  # type: ignore[return-value]


async def test_list_ready_tasks_filters_by_status(tracker_with_mock):
    tracker, _ = tracker_with_mock
    tasks = await tracker.list_ready_tasks()
    # Из 3 items только Task A (#1) в Ready колонке.
    assert len(tasks) == 1
    assert tasks[0].title == "Task A"
    assert tasks[0].id == "PVTI_a:1"


async def test_pick_next_ready_task_moves_to_in_progress(tracker_with_mock):
    tracker, calls = tracker_with_mock
    task = await tracker.pick_next_ready_task()
    assert task is not None
    assert task.title == "Task A"
    # Проверим, что был вызов update mutation с opt_progress.
    mutation_calls = [
        c for c in calls if "updateProjectV2ItemFieldValue" in c[0]
    ]
    assert len(mutation_calls) == 1
    _, variables = mutation_calls[0]
    assert variables["itemId"] == "PVTI_a"
    assert variables["optionId"] == "opt_progress"


async def test_move_task_to_done(tracker_with_mock):
    tracker, calls = tracker_with_mock
    await tracker.move_task("PVTI_a:1", "Done")
    mutation_calls = [
        c for c in calls if "updateProjectV2ItemFieldValue" in c[0]
    ]
    assert len(mutation_calls) == 1
    _, variables = mutation_calls[0]
    assert variables["itemId"] == "PVTI_a"
    assert variables["optionId"] == "opt_done"


async def test_move_task_requires_project_item_id(tracker_with_mock):
    tracker, _ = tracker_with_mock
    # task_id без ":" — не композитный, move невозможен.
    with pytest.raises(RuntimeError, match="does not include project_item_id"):
        await tracker.move_task("42", "Done")


async def test_move_task_unknown_column_raises(tracker_with_mock):
    tracker, _ = tracker_with_mock
    with pytest.raises(RuntimeError, match="column 'Backlog' not found"):
        await tracker.move_task("PVTI_a:1", "Backlog")


async def test_update_status_done_moves_to_done(
    monkeypatch, tracker_with_mock
):
    tracker, calls = tracker_with_mock

    # Подменим _issue_tracker.update_status, чтобы не звать gh.
    issue_calls: list[tuple[str, str, str]] = []

    async def fake_issue_update(task_id, status, details=""):
        issue_calls.append((task_id, status, details))

    tracker._issue_tracker.update_status = fake_issue_update

    await tracker.update_status("PVTI_a:1", "done", "all green")
    # Issue comment был оставлен.
    assert issue_calls == [("1", "done", "all green")]
    # И задача передвинута в Done.
    mutation_calls = [
        c for c in calls if "updateProjectV2ItemFieldValue" in c[0]
    ]
    assert mutation_calls and mutation_calls[0][1]["optionId"] == "opt_done"


async def test_missing_project_meta_raises():
    tracker = GithubProjectsTracker()
    with pytest.raises(RuntimeError, match="project_owner и project_number"):
        await tracker._ensure_project_meta()
