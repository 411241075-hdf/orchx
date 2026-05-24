"""Bash tool с allow-list-sandbox'ом.

Команда матчится против ``ctx.permissions.bash_check(...)``: если
обнаружена injection (``&&``, ``||``, ``|``, ``;``, ``$(...)``, backticks)
или нет матча в allow-list — команда отвергается до exec'а.

Wall-clock timeout — независимый от ``proc.communicate()``: stdout/stderr
читаются параллельными корутинами, и если процесс не завершается за
``timeout_ms``, он принудительно убивается с фиксацией partial-вывода.

Output обрезается до ~50KB; полный transcript параллельно пишется в
sidecar-файл рядом с лог-файлом воркера, чтобы debugger мог прочитать
полный результат при retry'е.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
from collections import deque
from pathlib import Path

from . import Tool, ToolContext, ToolResult


_TRUNCATION_LIMIT = 50_000  # ~50KB на каждый из stdout/stderr.


class BashTool(Tool):
    """Запустить bash-команду из allow-list'а."""

    name = "bash"
    description = (
        "Run a single bash command in the worker working directory. The "
        "command is parsed for prefix and matched against the agent's "
        "bash allow-list. Composite commands (``&&``, ``||``, ``;``, "
        "``|``, ``$(...)``, backticks) are blocked as command injection. "
        "Output is hard-capped to ~50KB per stream; if the process "
        "doesn't finish within timeout_ms it is killed and partial "
        "output is returned."
    )
    parameters = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "Single shell command (no chaining). Matched against allow-list by extracted prefix.",
            },
            "description": {
                "type": "string",
                "description": "Short human-readable description (optional).",
            },
            "timeout_ms": {
                "type": "integer",
                "minimum": 1000,
                "description": "Wall-clock timeout in milliseconds. Default 120000.",
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
        """Запустить bash-команду с allow-list-проверкой (см. описание класса)."""
        ctx.activity(f"bash {command[:80]}")
        hit = ctx.permissions.bash_check(command)
        if not hit.allowed:
            allow_list = sorted(
                k for k, v in ctx.permissions.bash.items() if v == "allow"
            )
            return ToolResult(
                content=(
                    f"Permission denied: {hit.reason}\n"
                    f"Command: {command}\n"
                    f"Extracted prefix: {hit.prefix or '(none)'}\n"
                    f"Allowed patterns: {allow_list or '(none)'}\n"
                    f"Hint: run a single command at a time. Composite "
                    f"commands (`&&`, `||`, `;`, `|`, `$(...)`, backticks) "
                    f"are blocked as injection. Split into multiple bash "
                    f"calls if needed."
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
        except OSError as e:
            return ToolResult(content=f"Failed to start bash: {e}", is_error=True)

        # Параллельное чтение stdout/stderr. Каждый поток независимо
        # ограничен `_TRUNCATION_LIMIT`. Это исключает ситуацию, когда
        # `proc.communicate()` ждёт всю буферизацию и игнорирует
        # wall-deadline, пока процесс что-то медленно пишет.
        wall_deadline_s = timeout_ms / 1000.0
        started = time.monotonic()

        async def _read_stream(stream):
            chunks: deque[bytes] = deque()
            collected = 0
            try:
                while True:
                    if stream is None:
                        return b""
                    chunk = await stream.read(8192)
                    if not chunk:
                        return b"".join(chunks)
                    if collected < _TRUNCATION_LIMIT:
                        chunks.append(chunk)
                        collected += len(chunk)
                    # После лимита читаем без сохранения, чтобы не
                    # переполнить пайп процесса (он бы залип на write).
            except asyncio.CancelledError:
                return b"".join(chunks)

        stdout_task = asyncio.create_task(_read_stream(proc.stdout))
        stderr_task = asyncio.create_task(_read_stream(proc.stderr))
        proc_wait = asyncio.create_task(proc.wait())

        timed_out = False
        try:
            await asyncio.wait_for(proc_wait, timeout=wall_deadline_s)
        except TimeoutError:
            timed_out = True
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            with contextlib.suppress(Exception):
                await proc.wait()

        # Дождёмся читателей — даже если процесс убит, stream'ы могут
        # ещё содержать буфер.
        async def _await_or_zero(t: asyncio.Task[bytes]) -> bytes:
            try:
                data = await asyncio.wait_for(t, timeout=2.0)
                return data if isinstance(data, bytes) else b""
            except (TimeoutError, asyncio.CancelledError):
                t.cancel()
                try:
                    data = await t
                    return data if isinstance(data, bytes) else b""
                except Exception:  # noqa: BLE001
                    return b""
            except Exception:  # noqa: BLE001
                return b""

        stdout_b = await _await_or_zero(stdout_task)
        stderr_b = await _await_or_zero(stderr_task)

        elapsed = time.monotonic() - started
        rc = proc.returncode if proc.returncode is not None else -1
        out = stdout_b.decode("utf-8", errors="replace")
        err = stderr_b.decode("utf-8", errors="replace")

        if timed_out:
            return ToolResult(
                content=(
                    f"<exit_code>killed-by-timeout</exit_code>\n"
                    f"<wall_clock>{elapsed:.1f}s of {wall_deadline_s:.0f}s budget</wall_clock>\n"
                    f"Command timed out after {timeout_ms}ms (wall-clock).\n"
                    f"<stdout>\n{out[:_TRUNCATION_LIMIT]}\n"
                    f"{'... (stdout truncated)' if len(out) >= _TRUNCATION_LIMIT else ''}"
                    f"</stdout>\n"
                    f"<stderr>\n{err[:_TRUNCATION_LIMIT]}\n"
                    f"{'... (stderr truncated)' if len(err) >= _TRUNCATION_LIMIT else ''}"
                    f"</stderr>"
                ),
                is_error=True,
            )

        # Sidecar-лог: если worker'у задан логфайл, пишем рядом полный output.
        # Сделаем это best-effort через переменную окружения.
        sidecar_path = os.environ.get("ORCHX_BASH_SIDECAR_LOG")
        if sidecar_path:
            try:
                p = Path(sidecar_path)
                p.parent.mkdir(parents=True, exist_ok=True)
                with p.open("a", encoding="utf-8") as fh:
                    fh.write(
                        f"\n=== bash @ {cwd} ===\n"
                        f"$ {command}\n"
                        f"# rc={rc} wall={elapsed:.2f}s\n"
                        f"{out}\n--- stderr ---\n{err}\n"
                    )
            except OSError:
                pass

        body_parts: list[str] = [f"<exit_code>{rc}</exit_code>"]
        if elapsed > 5.0:
            body_parts.append(f"<wall_clock>{elapsed:.1f}s</wall_clock>")
        out_truncated = len(out) >= _TRUNCATION_LIMIT
        err_truncated = len(err) >= _TRUNCATION_LIMIT
        if out:
            display_out = out[:_TRUNCATION_LIMIT]
            if out_truncated:
                display_out += "\n... (stdout truncated at 50KB)"
            body_parts.append(f"<stdout>\n{display_out}</stdout>")
        if err:
            display_err = err[:_TRUNCATION_LIMIT]
            if err_truncated:
                display_err += "\n... (stderr truncated at 50KB)"
            body_parts.append(f"<stderr>\n{display_err}</stderr>")
        body = "\n".join(body_parts)

        return ToolResult(content=body, is_error=rc != 0)
