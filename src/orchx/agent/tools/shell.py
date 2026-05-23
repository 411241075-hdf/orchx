"""Bash tool с allow-list-sandbox'ом.

Команда матчится против ``ctx.permissions.bash`` (полная строка, БЕЗ
сплита по ``|``/``;``). Если ни одно правило не разрешает — сразу
``ToolResult(is_error=True)`` без exec'а.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

from . import Tool, ToolContext, ToolResult


class BashTool(Tool):
    """Запустить bash-команду из allow-list'а."""

    name = "bash"
    description = (
        "Run a bash command in the worker working directory. The command is "
        "matched against the agent's bash allow-list — commands that don't "
        "match an allow-rule are rejected before execution. Output is "
        "truncated to ~50KB."
    )
    parameters = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "Full shell command line. Matched verbatim against allow-list.",
            },
            "description": {
                "type": "string",
                "description": "Short human-readable description (optional).",
            },
            "timeout_ms": {
                "type": "integer",
                "minimum": 1000,
                "description": "Timeout in milliseconds. Default 120000.",
            },
            "workdir": {
                "type": "string",
                "description": "Optional working directory for this command. Defaults to worker cwd.",
            },
        },
        "required": ["command"],
    }

    async def run(
        self,
        ctx: ToolContext,
        *,
        command: str,
        description: str | None = None,  # noqa: ARG002 — модель присылает как контекст
        timeout_ms: int = 120000,
        workdir: str | None = None,
    ) -> ToolResult:
        ctx.activity(f"bash {command[:80]}")
        allowed, pattern = ctx.permissions.bash_allowed(command)
        if not allowed:
            allow_list = sorted(
                k for k, v in ctx.permissions.bash.items() if v == "allow"
            )
            return ToolResult(
                content=(
                    f"Permission denied: command does not match any allow-rule.\n"
                    f"Command: {command}\n"
                    f"Allowed patterns: {allow_list or '(none)'}"
                ),
                is_error=True,
            )

        cwd = Path(workdir).resolve() if workdir else ctx.cwd
        try:
            proc = await asyncio.create_subprocess_exec(
                "bash",
                "-c",
                command,
                cwd=str(cwd),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=os.environ.copy(),
            )
        except (OSError, FileNotFoundError) as e:
            return ToolResult(content=f"Failed to start bash: {e}", is_error=True)

        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(), timeout=timeout_ms / 1000.0
            )
        except TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            return ToolResult(
                content=f"Command timed out after {timeout_ms}ms",
                is_error=True,
            )

        rc = proc.returncode if proc.returncode is not None else -1
        out = stdout_b.decode("utf-8", errors="replace")
        err = stderr_b.decode("utf-8", errors="replace")

        body_parts: list[str] = [f"<exit_code>{rc}</exit_code>"]
        if out:
            body_parts.append(f"<stdout>\n{out}</stdout>")
        if err:
            body_parts.append(f"<stderr>\n{err}</stderr>")
        body = "\n".join(body_parts)

        # Truncate to ~50KB.
        if len(body) > 50_000:
            body = body[:50_000] + "\n... (truncated)"

        return ToolResult(content=body, is_error=rc != 0)
