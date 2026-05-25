"""Тесты orchx.orchestrator.git_utils (P0.1)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from orchx.orchestrator.git_utils import (
    CONFLICT_MARKER_PREFIXES,
    files_with_conflict_markers,
    git_add_files,
    git_diff_stat,
    git_diff_summary,
    git_unmerged_files,
)


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


@pytest.fixture
def tmp_repo(tmp_path: Path) -> Path:
    """Initialised empty git repo."""
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test User")
    return tmp_path


@pytest.mark.asyncio
async def test_unmerged_files_on_clean_repo_returns_empty(tmp_repo: Path):
    (tmp_repo / "x.txt").write_text("hello", encoding="utf-8")
    _git(tmp_repo, "add", "x.txt")
    _git(tmp_repo, "commit", "-q", "-m", "init")
    result = await git_unmerged_files(tmp_repo)
    assert result == []


@pytest.mark.asyncio
async def test_unmerged_files_after_conflict(tmp_repo: Path):
    # Создаём конфликт между двумя ветками.
    (tmp_repo / "x.txt").write_text("base\n", encoding="utf-8")
    _git(tmp_repo, "add", "x.txt")
    _git(tmp_repo, "commit", "-q", "-m", "base")
    _git(tmp_repo, "checkout", "-q", "-b", "branch-a")
    (tmp_repo / "x.txt").write_text("change A\n", encoding="utf-8")
    _git(tmp_repo, "commit", "-q", "-am", "change A")
    _git(tmp_repo, "checkout", "-q", "-b", "branch-b", "HEAD^")
    (tmp_repo / "x.txt").write_text("change B\n", encoding="utf-8")
    _git(tmp_repo, "commit", "-q", "-am", "change B")
    # Merge → conflict
    result = subprocess.run(
        ["git", "merge", "branch-a"],
        cwd=str(tmp_repo),
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0  # ожидаем конфликт
    out = await git_unmerged_files(tmp_repo)
    assert "x.txt" in out


def test_conflict_marker_prefixes_constants():
    assert "<<<<<<<" in CONFLICT_MARKER_PREFIXES
    assert ">>>>>>>" in CONFLICT_MARKER_PREFIXES
    assert "=======" in CONFLICT_MARKER_PREFIXES


@pytest.mark.asyncio
async def test_files_with_conflict_markers(tmp_repo: Path):
    f = tmp_repo / "a.txt"
    f.write_text(
        "first line\n<<<<<<< HEAD\nmine\n=======\ntheirs\n>>>>>>> branch\nlast line\n",
        encoding="utf-8",
    )
    g = tmp_repo / "b.txt"
    g.write_text("clean file\n", encoding="utf-8")
    result = await files_with_conflict_markers(tmp_repo, ["a.txt", "b.txt"])
    assert "a.txt" in result
    assert "b.txt" not in result


@pytest.mark.asyncio
async def test_files_with_conflict_markers_handles_missing_file(tmp_repo: Path):
    result = await files_with_conflict_markers(tmp_repo, ["nonexistent.txt"])
    assert result == []  # missing files skipped (treated as deleted)


@pytest.mark.asyncio
async def test_git_add_files_with_empty_list_is_noop(tmp_repo: Path):
    await git_add_files(tmp_repo, [])  # not raise


@pytest.mark.asyncio
async def test_git_add_files_stages_files(tmp_repo: Path):
    (tmp_repo / "new.txt").write_text("hi", encoding="utf-8")
    await git_add_files(tmp_repo, ["new.txt"])
    status = _git(tmp_repo, "status", "--porcelain")
    assert "A  new.txt" in status


@pytest.mark.asyncio
async def test_git_diff_summary_returns_shortstat(tmp_repo: Path):
    (tmp_repo / "base.txt").write_text("base", encoding="utf-8")
    _git(tmp_repo, "add", "base.txt")
    _git(tmp_repo, "commit", "-q", "-m", "init")
    _git(tmp_repo, "checkout", "-q", "-b", "feature")
    (tmp_repo / "new.txt").write_text("new\nfile\nwith\nlines\n", encoding="utf-8")
    _git(tmp_repo, "add", "new.txt")
    _git(tmp_repo, "commit", "-q", "-m", "feat")
    summary = await git_diff_summary(tmp_repo, "main" if _has_branch(tmp_repo, "main") else "master")
    assert summary  # shortstat возвращает что-то типа "1 file changed, 4 insertions(+)"


@pytest.mark.asyncio
async def test_git_diff_stat_returns_file_list(tmp_repo: Path):
    (tmp_repo / "base.txt").write_text("base", encoding="utf-8")
    _git(tmp_repo, "add", "base.txt")
    _git(tmp_repo, "commit", "-q", "-m", "init")
    _git(tmp_repo, "checkout", "-q", "-b", "feature")
    (tmp_repo / "new.txt").write_text("new\n", encoding="utf-8")
    _git(tmp_repo, "add", "new.txt")
    _git(tmp_repo, "commit", "-q", "-m", "feat")
    stat = await git_diff_stat(tmp_repo, "main" if _has_branch(tmp_repo, "main") else "master")
    assert "new.txt" in stat


def _has_branch(repo: Path, branch: str) -> bool:
    try:
        subprocess.run(
            ["git", "rev-parse", "--verify", branch],
            cwd=str(repo),
            check=True,
            capture_output=True,
        )
        return True
    except subprocess.CalledProcessError:
        return False
