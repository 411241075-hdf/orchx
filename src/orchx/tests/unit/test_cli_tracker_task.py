"""Тесты CLI-резолвера ``--tracker-task`` / ``ORCHX_TRACKER_TASK_ID``.

Проверяет приоритет (CLI > env), trim'ование и пустые значения.
"""

from __future__ import annotations

import argparse

import pytest

from orchx.cli import _resolve_tracker_task_id


def _ns(**kw: object) -> argparse.Namespace:
    return argparse.Namespace(**kw)


def test_returns_empty_when_nothing_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ORCHX_TRACKER_TASK_ID", raising=False)
    assert _resolve_tracker_task_id(_ns(tracker_task=None)) == ""


def test_returns_env_when_no_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ORCHX_TRACKER_TASK_ID", "PVTI_xxx:42")
    assert _resolve_tracker_task_id(_ns(tracker_task=None)) == "PVTI_xxx:42"


def test_cli_overrides_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ORCHX_TRACKER_TASK_ID", "PVTI_env:1")
    assert (
        _resolve_tracker_task_id(_ns(tracker_task="PVTI_cli:2"))
        == "PVTI_cli:2"
    )


def test_strips_whitespace(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ORCHX_TRACKER_TASK_ID", raising=False)
    assert (
        _resolve_tracker_task_id(_ns(tracker_task="  PVTI_x:1  "))
        == "PVTI_x:1"
    )


def test_empty_env_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ORCHX_TRACKER_TASK_ID", "   ")
    assert _resolve_tracker_task_id(_ns(tracker_task=None)) == ""


def test_handles_namespace_without_attribute(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``getattr`` fallback — если в Namespace нет ``tracker_task``."""
    monkeypatch.delenv("ORCHX_TRACKER_TASK_ID", raising=False)
    assert _resolve_tracker_task_id(_ns()) == ""
