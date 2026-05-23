"""Search tools: grep, codesearch.

Если в системе есть ``rg`` (ripgrep) — используем его (быстрый, поддерживает
``--type``, ``--include``). Иначе fallback на Python ``re``-обход дерева
от ``ctx.cwd``.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
from pathlib import Path
from typing import Any

from . import Tool, ToolContext, ToolResult


# ---------------------------------------------------------------------------
# Common: rg detection
# ---------------------------------------------------------------------------


def _has_rg() -> bool:
    return shutil.which("rg") is not None


async def _run_rg(args: list[str], cwd: Path) -> tuple[int, str, str]:
    """Запустить ripgrep и вернуть ``(rc, stdout, stderr)``."""
    proc = await asyncio.create_subprocess_exec(
        "rg",
        *args,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out_b, err_b = await proc.communicate()
    return (
        proc.returncode if proc.returncode is not None else -1,
        out_b.decode("utf-8", errors="replace"),
        err_b.decode("utf-8", errors="replace"),
    )


# Файлы/директории, которые мы не сканируем в Python-fallback'е.
_PY_FALLBACK_SKIP_DIRS = {
    ".git",
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "dist",
    "build",
    ".next",
}


def _python_grep(
    *,
    pattern: str,
    base: Path,
    include: str | None,
) -> list[str]:
    """Fallback Python-grep по дереву от ``base``."""
    try:
        regex = re.compile(pattern)
    except re.error as e:
        return [f"regex error: {e}"]
    out: list[str] = []
    for root, dirs, files in os.walk(base):
        # фильтруем «шумные» директории
        dirs[:] = [d for d in dirs if d not in _PY_FALLBACK_SKIP_DIRS]
        for fname in files:
            if include and not _fnmatch_simple(fname, include):
                continue
            p = Path(root) / fname
            try:
                with p.open("r", encoding="utf-8", errors="replace") as fh:
                    for lineno, line in enumerate(fh, start=1):
                        if regex.search(line):
                            try:
                                rel = str(p.relative_to(base))
                            except ValueError:
                                rel = str(p)
                            out.append(f"{rel}:{lineno}:{line.rstrip()}")
                            if len(out) >= 5000:
                                return out
            except (OSError, UnicodeDecodeError):
                continue
    return out


def _fnmatch_simple(name: str, pattern: str) -> bool:
    """Сопоставление имени файла с include-glob ('*.py', '*.{ts,tsx}')."""
    import fnmatch

    # Простое расширение pattern с {a,b} (rg-стиль).
    if "{" in pattern and "}" in pattern:
        head, _, rest = pattern.partition("{")
        body, _, tail = rest.partition("}")
        for opt in body.split(","):
            if fnmatch.fnmatchcase(name, head + opt.strip() + tail):
                return True
        return False
    return fnmatch.fnmatchcase(name, pattern)


# ---------------------------------------------------------------------------
# grep
# ---------------------------------------------------------------------------


class GrepTool(Tool):
    """Поиск содержимого файлов по regex."""

    name = "grep"
    description = (
        "Search file contents using a regular expression. Returns matching "
        "`path:line:text` rows. Optional `path` (directory to search) and "
        "`include` (file glob like `*.py` or `*.{ts,tsx}`)."
    )
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "Regular expression to search for.",
            },
            "path": {
                "type": "string",
                "description": "Directory to search in. Defaults to the worker cwd.",
            },
            "include": {
                "type": "string",
                "description": "File glob to filter, e.g. `*.py` or `*.{ts,tsx}`.",
            },
        },
        "required": ["pattern"],
    }
    permission_attr = "grep"

    async def run(
        self,
        ctx: ToolContext,
        *,
        pattern: str,
        path: str | None = None,
        include: str | None = None,
    ) -> ToolResult:
        ctx.activity(f"grep {pattern}")
        base = (
            (ctx.cwd / path).resolve() if path and not Path(path).is_absolute()
            else (Path(path).resolve() if path else ctx.cwd)
        )
        if not base.exists():
            return ToolResult(content=f"Path not found: {base}", is_error=True)
        if _has_rg():
            args = ["--no-heading", "-n", "-S", pattern]
            if include:
                args += ["-g", include]
            args.append(str(base))
            rc, out, err = await _run_rg(args, ctx.cwd)
            if rc not in (0, 1):  # rc==1 — нет матчей; это норма
                return ToolResult(content=f"rg error: {err.strip()}", is_error=True)
            if not out.strip():
                return ToolResult(content="(no matches)")
            lines = out.splitlines()[:5000]
            return ToolResult(content="\n".join(lines))
        # fallback
        rows = _python_grep(pattern=pattern, base=base, include=include)
        if not rows:
            return ToolResult(content="(no matches)")
        return ToolResult(content="\n".join(rows[:5000]))


# ---------------------------------------------------------------------------
# codesearch
# ---------------------------------------------------------------------------


class CodeSearchTool(Tool):
    """То же что ``grep``, но с фильтрацией по типу файлов (rg --type)."""

    name = "codesearch"
    description = (
        "Search code files by regex with optional type filter. Same as `grep` "
        "but accepts `type` (rg --type, e.g. `py`, `ts`, `js`)."
    )
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string"},
            "path": {"type": "string"},
            "type": {
                "type": "string",
                "description": "Optional rg --type, e.g. py, ts, rust, md.",
            },
        },
        "required": ["pattern"],
    }
    permission_attr = "codesearch"

    async def run(
        self,
        ctx: ToolContext,
        *,
        pattern: str,
        path: str | None = None,
        type: str | None = None,  # noqa: A002 (param name matches OpenAI schema)
    ) -> ToolResult:
        ctx.activity(f"codesearch {pattern}")
        base = (
            (ctx.cwd / path).resolve() if path and not Path(path).is_absolute()
            else (Path(path).resolve() if path else ctx.cwd)
        )
        if not base.exists():
            return ToolResult(content=f"Path not found: {base}", is_error=True)
        if _has_rg():
            args = ["--no-heading", "-n", "-S", pattern]
            if type:
                args += ["--type", type]
            args.append(str(base))
            rc, out, err = await _run_rg(args, ctx.cwd)
            if rc not in (0, 1):
                return ToolResult(content=f"rg error: {err.strip()}", is_error=True)
            if not out.strip():
                return ToolResult(content="(no matches)")
            lines = out.splitlines()[:5000]
            return ToolResult(content="\n".join(lines))
        # fallback — игнорим type-фильтр
        rows = _python_grep(pattern=pattern, base=base, include=None)
        return ToolResult(content="\n".join(rows[:5000]) if rows else "(no matches)")
