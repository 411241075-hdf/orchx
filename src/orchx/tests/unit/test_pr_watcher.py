"""Тесты PR feedback loop (P0.4)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

# NB: глобальный pytestmark.asyncio не ставим — sync-тесты в этом файле
# (parse_yaml / extract_*) не должны быть async-обёрнуты. async-тесты ниже
# помечены вручную.
from orchx.pr_watcher import (
    DEFAULT_REACTIONS,
    ReactionConfig,
    _extract_change_request_comments,
    _extract_ci_rollup,
    _extract_review_decision,
    _parse_duration_min,
    parse_reactions_yaml,
    watch_pr,
)

# ---- parse_reactions_yaml ----


def test_parse_reactions_default_when_empty():
    out = parse_reactions_yaml({})
    assert set(out.keys()) == set(DEFAULT_REACTIONS.keys())
    assert out["ci_failed"].auto is True


def test_parse_reactions_override_ci_failed():
    out = parse_reactions_yaml(
        {"ci_failed": {"auto": False, "max_retries": 5}}
    )
    assert out["ci_failed"].auto is False
    assert out["ci_failed"].max_retries == 5
    # Остальные дефолтные.
    assert out["changes_requested"].auto is True


def test_parse_reactions_invalid_key_warns_but_continues(caplog):
    out = parse_reactions_yaml({"unknown_event": {"auto": True}})
    assert "unknown_event" not in out


def test_parse_reactions_invalid_value_skipped():
    out = parse_reactions_yaml({"ci_failed": "not a dict"})
    # Должны вернуть default.
    assert out["ci_failed"].auto == DEFAULT_REACTIONS["ci_failed"].auto


# ---- _parse_duration_min ----


@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, 30),
        (15, 15),
        (15.5, 15),
        ("30", 30),
        ("30m", 30),
        ("30min", 30),
        ("2h", 120),
        ("1hour", 60),
        ("bad", 30),
        ("", 30),
    ],
)
def test_parse_duration_various(raw, expected):
    assert _parse_duration_min(raw, default=30) == expected


# ---- _extract_ci_rollup ----


def test_extract_ci_rollup_no_data():
    assert _extract_ci_rollup({}) is None
    assert _extract_ci_rollup({"statusCheckRollup": []}) is None


def test_extract_ci_rollup_all_success():
    pr = {"statusCheckRollup": [{"conclusion": "SUCCESS"}, {"state": "SUCCESS"}]}
    assert _extract_ci_rollup(pr) == "SUCCESS"


def test_extract_ci_rollup_any_failure_wins():
    pr = {
        "statusCheckRollup": [
            {"conclusion": "SUCCESS"},
            {"conclusion": "FAILURE"},
        ]
    }
    assert _extract_ci_rollup(pr) == "FAILURE"


def test_extract_ci_rollup_pending():
    pr = {
        "statusCheckRollup": [
            {"conclusion": "SUCCESS"},
            {"status": "IN_PROGRESS"},
        ]
    }
    assert _extract_ci_rollup(pr) == "PENDING"


def test_extract_ci_rollup_skipped_treated_as_success():
    pr = {"statusCheckRollup": [{"conclusion": "SKIPPED"}, {"conclusion": "SUCCESS"}]}
    assert _extract_ci_rollup(pr) == "SUCCESS"


# ---- review decision / comments ----


def test_extract_review_decision():
    assert _extract_review_decision({"reviewDecision": "APPROVED"}) == "APPROVED"
    assert _extract_review_decision({"reviewDecision": "approved"}) == "APPROVED"
    assert _extract_review_decision({}) is None


def test_extract_change_request_comments():
    pr = {
        "reviews": [
            {"id": "r1", "state": "APPROVED", "body": "lgtm"},
            {
                "id": "r2",
                "state": "CHANGES_REQUESTED",
                "body": "please rename X",
                "author": {"login": "alice"},
            },
        ]
    }
    out = _extract_change_request_comments(pr)
    assert len(out) == 1
    assert out[0]["id"] == "r2"
    assert out[0]["author"] == "alice"
    assert out[0]["body"] == "please rename X"


# ---- watch_pr (integration с fake SCM) ----


class _FakeSCM:
    """Подменяет :class:`GithubSCM` для тестов."""

    def __init__(self, snapshots: list[dict[str, Any]]):
        self._snaps = snapshots
        self.calls = 0

    async def get_pr_status(self, repo_root: Path, pr_url: str) -> dict[str, Any]:
        i = min(self.calls, len(self._snaps) - 1)
        self.calls += 1
        return self._snaps[i]


class _RecordingNotifier:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    async def notify(self, event: str, payload: dict) -> None:
        self.events.append((event, payload))


@pytest.mark.asyncio
async def test_watch_pr_exits_on_merged():
    scm = _FakeSCM([{"state": "MERGED", "statusCheckRollup": []}])
    notifier = _RecordingNotifier()
    await watch_pr(
        repo_root=Path("."),
        pr_url="https://example/pr/1",
        task_id="T1",
        reactions=DEFAULT_REACTIONS,
        scm=scm,
        notifier=notifier,
        poll_interval_s=0.01,
        max_wall_s=5.0,
    )
    assert any(e == "pr_closed" for e, _ in notifier.events)


async def test_watch_pr_triggers_ci_failed_callback():
    scm = _FakeSCM(
        [
            {
                "state": "OPEN",
                "statusCheckRollup": [{"conclusion": "FAILURE"}],
                "reviews": [],
            },
            {"state": "CLOSED", "statusCheckRollup": []},  # завершить
        ]
    )
    ci_calls: list[int] = []

    async def on_ci(_logs: str, retry_count: int):
        ci_calls.append(retry_count)

    notifier = _RecordingNotifier()
    await watch_pr(
        repo_root=Path("."),
        pr_url="https://example/pr/1",
        task_id="T1",
        reactions=DEFAULT_REACTIONS,
        scm=scm,
        notifier=notifier,
        on_ci_failed=on_ci,
        poll_interval_s=0.01,
        max_wall_s=5.0,
    )
    assert ci_calls == [1]
    assert any(e == "ci_failed" for e, _ in notifier.events)


async def test_watch_pr_changes_requested_callback():
    scm = _FakeSCM(
        [
            {
                "state": "OPEN",
                "statusCheckRollup": [{"conclusion": "SUCCESS"}],
                "reviews": [
                    {
                        "id": "r1",
                        "state": "CHANGES_REQUESTED",
                        "body": "rename X to Y",
                        "author": {"login": "bob"},
                    }
                ],
            },
            {"state": "CLOSED", "statusCheckRollup": []},
        ]
    )
    received: list[list[dict]] = []

    async def on_cr(comments):
        received.append(comments)

    await watch_pr(
        repo_root=Path("."),
        pr_url="https://example/pr/1",
        task_id="T1",
        reactions=DEFAULT_REACTIONS,
        scm=scm,
        on_changes_requested=on_cr,
        poll_interval_s=0.01,
        max_wall_s=5.0,
    )
    assert received and received[0][0]["body"] == "rename X to Y"


async def test_watch_pr_approved_and_green_notifies():
    scm = _FakeSCM(
        [
            {
                "state": "OPEN",
                "statusCheckRollup": [{"conclusion": "SUCCESS"}],
                "reviewDecision": "APPROVED",
                "reviews": [],
            },
            {"state": "CLOSED", "statusCheckRollup": []},
        ]
    )
    notifier = _RecordingNotifier()
    await watch_pr(
        repo_root=Path("."),
        pr_url="https://example/pr/1",
        task_id="T1",
        reactions=DEFAULT_REACTIONS,
        scm=scm,
        notifier=notifier,
        poll_interval_s=0.01,
        max_wall_s=5.0,
    )
    assert any(e == "approved_and_green" for e, _ in notifier.events)


async def test_watch_pr_max_wall_exceeded():
    # SCM никогда не вернёт MERGED → watcher должен сам выйти по max_wall_s.
    scm = _FakeSCM(
        [{"state": "OPEN", "statusCheckRollup": [{"conclusion": "PENDING"}], "reviews": []}]
    )
    notifier = _RecordingNotifier()
    started = asyncio.get_event_loop().time()
    await watch_pr(
        repo_root=Path("."),
        pr_url="https://example/pr/1",
        task_id="T1",
        reactions=DEFAULT_REACTIONS,
        scm=scm,
        notifier=notifier,
        poll_interval_s=0.01,
        max_wall_s=0.05,
    )
    elapsed = asyncio.get_event_loop().time() - started
    assert elapsed < 1.0
    assert any(e == "pr_watcher_timeout" for e, _ in notifier.events)


async def test_reaction_config_max_retries_honored():
    """ci_failed повторяется не чаще чем max_retries."""
    snaps = []
    for _ in range(10):
        snaps.append(
            {
                "state": "OPEN",
                "statusCheckRollup": [{"conclusion": "FAILURE"}],
                "reviews": [],
            }
        )
    snaps.append({"state": "CLOSED", "statusCheckRollup": []})

    scm = _FakeSCM(snaps)
    ci_count = 0

    async def on_ci(_logs: str, _retry_count: int):
        nonlocal ci_count
        ci_count += 1

    reactions = dict(DEFAULT_REACTIONS)
    reactions["ci_failed"] = ReactionConfig(
        auto=True, action="send-to-debugger", max_retries=2
    )
    # Чтобы тригерится повторно — на каждой итерации state.last_ci_status
    # сбрасываем «не FAILURE». Имитируем чередованием SUCCESS/FAILURE.
    snaps2 = []
    for i in range(10):
        snaps2.append(
            {
                "state": "OPEN",
                "statusCheckRollup": [
                    {"conclusion": "FAILURE" if i % 2 == 0 else "SUCCESS"}
                ],
                "reviews": [],
            }
        )
    snaps2.append({"state": "CLOSED", "statusCheckRollup": []})
    scm = _FakeSCM(snaps2)
    ci_count = 0

    await watch_pr(
        repo_root=Path("."),
        pr_url="https://example/pr/1",
        task_id="T1",
        reactions=reactions,
        scm=scm,
        on_ci_failed=on_ci,
        poll_interval_s=0.001,
        max_wall_s=5.0,
    )
    assert ci_count <= 2  # max_retries=2
