"""Smoke-тесты public API orchx.orchestrator (P0.1)."""

from __future__ import annotations

import pytest


def test_orchestrator_exports_public_api():
    """Все главные символы должны быть импортируемы через orchx.orchestrator."""
    from orchx.orchestrator import (
        AttemptInfo,
        OrchXConfig,
        OrchXContext,
        PhaseState,
        TaskState,
        run_orchX,
    )
    assert OrchXConfig is not None
    assert OrchXContext is not None
    assert TaskState is not None
    assert PhaseState is not None
    assert AttemptInfo is not None
    assert callable(run_orchX)


def test_orchx_config_default_values():
    from orchx.orchestrator import OrchXConfig

    cfg = OrchXConfig()
    assert cfg.auto_review is True
    assert cfg.use_debugger_on_retry is True
    assert cfg.effort == "high"
    assert cfg.reviewer_effort == "xhigh"
    # P0.4 / P1.3 / P2.1 newly added flags should default safely:
    assert cfg.pr_watcher_enabled is False
    assert cfg.auto_fixup_chain is True
    assert cfg.cleanup_worktrees_after_merge is False
    assert cfg.max_cost_usd is None


def test_orchx_config_is_frozen():
    from orchx.orchestrator import OrchXConfig

    cfg = OrchXConfig()
    with pytest.raises(Exception):  # FrozenInstanceError
        cfg.effort = "low"


def test_logging_utils_import():
    from orchx.orchestrator.logging_utils import logger, orchx_log
    assert callable(orchx_log)
    assert logger.name == "orchx.orchestrator"


def test_git_utils_import():
    from orchx.orchestrator.git_utils import (
        CONFLICT_MARKER_PREFIXES,
        files_with_conflict_markers,
        git_add_files,
        git_diff_stat,
        git_diff_summary,
        git_unmerged_files,
    )
    assert isinstance(CONFLICT_MARKER_PREFIXES, tuple)
    assert callable(git_unmerged_files)
    assert callable(files_with_conflict_markers)
    assert callable(git_add_files)
    assert callable(git_diff_summary)
    assert callable(git_diff_stat)
