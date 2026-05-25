"""GitHub SCM plugin (push / open PR / get-PR-status через ``gh`` CLI).

Является тонкой обёрткой над :mod:`orchx.pr` (которая уже умеет работать
с ``gh``). Главная ценность — единый contract для будущих
gitlab/bitbucket plugin'ов.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any


class GithubSCM:
    """``gh``-based реализация :class:`SCMPlugin`."""

    name = "github"

    def __init__(self, **_: Any) -> None:
        pass

    async def push_branch(self, repo_root: Path, branch: str) -> None:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "push",
            "-u",
            "origin",
            branch,
            cwd=str(repo_root),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_b, stderr_b = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(
                f"git push failed for {branch}: "
                f"{stderr_b.decode('utf-8', errors='replace')}"
            )

    async def open_pr(
        self,
        *,
        repo_root: Path,
        head_branch: str,
        base_branch: str,
        title: str,
        body: str,
        draft: bool = False,
    ) -> str:
        cmd = [
            "gh",
            "pr",
            "create",
            "--head",
            head_branch,
            "--base",
            base_branch,
            "--title",
            title,
            "--body",
            body,
        ]
        if draft:
            cmd.append("--draft")
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(repo_root),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_b, stderr_b = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(
                f"gh pr create failed: {stderr_b.decode('utf-8', errors='replace')}"
            )
        return stdout_b.decode("utf-8", errors="replace").strip()

    async def get_pr_status(self, repo_root: Path, pr_url: str) -> dict[str, Any]:
        """Получить statusCheckRollup + reviewDecision + comments + state."""
        cmd = [
            "gh",
            "pr",
            "view",
            pr_url,
            "--json",
            "state,statusCheckRollup,reviewDecision,reviews,comments,headRefName,baseRefName",
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(repo_root),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_b, stderr_b = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(
                f"gh pr view failed: {stderr_b.decode('utf-8', errors='replace')}"
            )
        try:
            return json.loads(stdout_b.decode("utf-8", errors="replace"))
        except json.JSONDecodeError as e:
            raise RuntimeError(f"gh pr view returned invalid JSON: {e}") from e


__all__ = ["GithubSCM"]
