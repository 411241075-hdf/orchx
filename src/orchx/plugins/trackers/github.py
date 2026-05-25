"""GitHub issue tracker plugin (через ``gh`` CLI).

Minimum-viable implementation: умеет получить body issue и оставить
комментарий. Расширения (labels / project-board / status) — на будущее.

Требуется установленный ``gh`` (``brew install gh``) и аутентификация
``gh auth login``.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any


class GithubTracker:
    """GitHub Issues через ``gh`` CLI.

    Config:
        owner_repo: ``owner/repo``. Если None — берётся из текущего git remote.
    """

    name = "github"

    def __init__(self, *, owner_repo: str | None = None, **_: Any) -> None:
        self.owner_repo = owner_repo

    async def fetch_task_description(self, task_id: str) -> str | None:
        """``gh issue view <id> --json body``."""
        cmd = ["gh", "issue", "view", task_id, "--json", "body,title"]
        if self.owner_repo:
            cmd.extend(["--repo", self.owner_repo])
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_b, stderr_b = await proc.communicate()
        if proc.returncode != 0:
            return None
        try:
            data = json.loads(stdout_b.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            return None
        title = data.get("title", "")
        body = data.get("body", "")
        if title and body:
            return f"# {title}\n\n{body}"
        return body or title or None

    async def update_status(
        self,
        task_id: str,
        status: str,
        details: str = "",
    ) -> None:
        """Оставить комментарий с обновлением статуса."""
        body = f"**orchX status:** `{status}`"
        if details:
            body += f"\n\n{details}"
        cmd = ["gh", "issue", "comment", task_id, "--body", body]
        if self.owner_repo:
            cmd.extend(["--repo", self.owner_repo])
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()


__all__ = ["GithubTracker"]
