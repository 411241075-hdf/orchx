"""Низкоуровневые git-обёртки, используемые оркестратором.

Выделено из ``orchx.orchestrator.core`` (P0.1).

Все функции — pure async-обёртки над ``git`` CLI. Не зависят от
``OrchXContext``. Тестируемы изолированно (см.
``tests/integration/test_git_utils.py`` для интеграционных тестов на
временных репо).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

CONFLICT_MARKER_PREFIXES = ("<<<<<<<", "=======", ">>>>>>>")


async def git_unmerged_files(cwd: Path) -> list[str]:
    """Список файлов с merge-конфликтами в worktree (``--diff-filter=U``)."""
    proc = await asyncio.create_subprocess_exec(
        "git",
        "diff",
        "--name-only",
        "--diff-filter=U",
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout_b, _ = await proc.communicate()
    return [
        line.strip()
        for line in stdout_b.decode("utf-8", errors="replace").splitlines()
        if line.strip()
    ]


async def files_with_conflict_markers(cwd: Path, files: list[str]) -> list[str]:
    """Файлы из ``files``, в которых остались git conflict markers (``<<<<<<<`` и т.п.)."""
    bad: list[str] = []
    for f in files:
        path = cwd / f
        if not path.is_file():
            # Удалённый файл — нет маркеров.
            continue
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            bad.append(f)
            continue
        for line in content.splitlines():
            if any(line.startswith(p) for p in CONFLICT_MARKER_PREFIXES):
                bad.append(f)
                break
    return bad


async def git_add_files(cwd: Path, files: list[str]) -> None:
    """``git add`` указанных файлов (или удаление, если файл стёрт)."""
    if not files:
        return
    proc = await asyncio.create_subprocess_exec(
        "git",
        "add",
        "--",
        *files,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await proc.communicate()


async def git_diff_summary(cwd: Path, base: str) -> str:
    """Краткий вывод ``git diff --shortstat base...HEAD``."""
    proc = await asyncio.create_subprocess_exec(
        "git",
        "diff",
        "--shortstat",
        f"{base}...HEAD",
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout_b, _ = await proc.communicate()
    return stdout_b.decode("utf-8", errors="replace").strip()


async def git_diff_stat(cwd: Path, base: str) -> str:
    """Полный ``git diff --stat base...HEAD``."""
    proc = await asyncio.create_subprocess_exec(
        "git",
        "diff",
        "--stat",
        f"{base}...HEAD",
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout_b, _ = await proc.communicate()
    return stdout_b.decode("utf-8", errors="replace").strip()


# Старые имена со подчёркиванием — для backward-compat внутри core.py.
_git_unmerged_files = git_unmerged_files
_files_with_conflict_markers = files_with_conflict_markers
_git_add_files = git_add_files
_git_diff_summary = git_diff_summary
_git_diff_stat = git_diff_stat

__all__ = [
    "CONFLICT_MARKER_PREFIXES",
    "git_unmerged_files",
    "files_with_conflict_markers",
    "git_add_files",
    "git_diff_summary",
    "git_diff_stat",
    "_git_unmerged_files",
    "_files_with_conflict_markers",
    "_git_add_files",
    "_git_diff_summary",
    "_git_diff_stat",
]
