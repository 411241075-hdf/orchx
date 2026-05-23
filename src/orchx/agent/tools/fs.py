"""Filesystem tools: read, write, edit, glob.

Все пути резолвятся относительно ``ctx.cwd``. Запись/правка дополнительно
гейтится через ``ctx.permissions.edit_allowed(rel_path)`` — если denied,
возвращаем ``ToolResult(is_error=True)`` БЕЗ обращения к диску.
"""

from __future__ import annotations

from pathlib import Path

from . import Tool, ToolContext, ToolResult


def _resolve(ctx: ToolContext, p: str) -> Path:
    """Превратить путь от LLM в абсолютный, относительно cwd."""
    pp = Path(p)
    if pp.is_absolute():
        return pp
    return (ctx.cwd / pp).resolve()


def _rel(ctx: ToolContext, abs_path: Path) -> str:
    """Получить путь относительно cwd для permission-check'а.

    Резолвим обе стороны (cwd и target), чтобы symlink'и/префиксы
    типа ``/var`` ↔ ``/private/var`` на macOS не ломали relative_to.
    """
    try:
        return str(abs_path.resolve().relative_to(ctx.cwd.resolve()))
    except ValueError:
        # Если путь снаружи cwd — возвращаем как есть. edit_allowed
        # построит решение по полному пути (обычно отвергнет).
        return str(abs_path)


# ---------------------------------------------------------------------------
# read
# ---------------------------------------------------------------------------


class ReadTool(Tool):
    """Прочитать файл (с номерами строк) или показать содержимое директории."""

    name = "read"
    description = (
        "Read a file from the local filesystem. Returns up to `limit` lines "
        "starting from `offset` (1-indexed). Each line is prefixed with its "
        "line number. For directories, returns one entry per line (with `/` "
        "suffix for subdirectories)."
    )
    parameters = {
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "Absolute or repo-relative path to the file or directory.",
            },
            "offset": {
                "type": "integer",
                "minimum": 1,
                "description": "1-indexed line number to start reading from. Default 1.",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "description": "Maximum number of lines to read. Default 2000.",
            },
        },
        "required": ["file_path"],
    }
    permission_attr = "read"

    async def run(
        self,
        ctx: ToolContext,
        *,
        file_path: str,
        offset: int = 1,
        limit: int = 2000,
    ) -> ToolResult:
        """Прочитать файл/директорию (см. описание класса)."""
        ctx.activity(f"read {file_path}")
        path = _resolve(ctx, file_path)
        if not path.exists():
            return ToolResult(
                content=f"File not found: {file_path}",
                is_error=True,
            )
        if path.is_dir():
            try:
                entries = sorted(path.iterdir(), key=lambda p: p.name)
            except OSError as e:
                return ToolResult(content=f"OS error: {e}", is_error=True)
            lines = [f"{p.name}/" if p.is_dir() else p.name for p in entries]
            return ToolResult(content="\n".join(lines))
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            return ToolResult(content=f"OS error: {e}", is_error=True)
        lines = text.split("\n")
        start_idx = max(0, offset - 1)
        end_idx = min(len(lines), start_idx + limit)
        out_lines = []
        for i in range(start_idx, end_idx):
            ln = lines[i]
            if len(ln) > 2000:
                ln = ln[:2000] + " ...(truncated)"
            out_lines.append(f"{i + 1}: {ln}")
        body = "\n".join(out_lines)
        if end_idx < len(lines):
            body += f"\n\n(Showing lines {offset}-{end_idx} of {len(lines)}. Pass offset={end_idx + 1} to continue.)"
        return ToolResult(content=body)


# ---------------------------------------------------------------------------
# write
# ---------------------------------------------------------------------------


class WriteTool(Tool):
    """Записать файл целиком (создаёт parent-директории)."""

    name = "write"
    description = (
        "Write `content` to `file_path`, overwriting any existing contents. "
        "Parent directories are created automatically. Path is gated by the "
        "agent's edit permission."
    )
    parameters = {
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "Absolute or repo-relative path to the file.",
            },
            "content": {
                "type": "string",
                "description": "Full new contents of the file.",
            },
        },
        "required": ["file_path", "content"],
    }

    async def run(
        self,
        ctx: ToolContext,
        *,
        file_path: str,
        content: str,
    ) -> ToolResult:
        """Записать файл целиком (см. описание класса)."""
        ctx.activity(f"write {file_path}")
        path = _resolve(ctx, file_path)
        rel = _rel(ctx, path)
        if not ctx.permissions.edit_allowed(rel):
            return ToolResult(
                content=(
                    f"Permission denied: write to {rel} is not allowed by this "
                    f"agent's edit-policy."
                ),
                is_error=True,
            )
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        except OSError as e:
            return ToolResult(content=f"OS error: {e}", is_error=True)
        return ToolResult(
            content=f"Wrote {len(content)} bytes to {file_path}.",
        )


# ---------------------------------------------------------------------------
# edit
# ---------------------------------------------------------------------------


class EditTool(Tool):
    """Точечная замена строки в файле (с проверкой уникальности матча)."""

    name = "edit"
    description = (
        "Replace `old_string` with `new_string` in `file_path`. By default the "
        "match must be unique — if `old_string` occurs zero or multiple times, "
        "the call fails. Set `replace_all=true` to replace every occurrence."
    )
    parameters = {
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "Absolute or repo-relative path to the file.",
            },
            "old_string": {
                "type": "string",
                "description": "The exact string to find. Must be unique unless `replace_all` is true.",
            },
            "new_string": {
                "type": "string",
                "description": "What to replace `old_string` with.",
            },
            "replace_all": {
                "type": "boolean",
                "description": "If true, replace every occurrence. Default false.",
            },
        },
        "required": ["file_path", "old_string", "new_string"],
    }

    async def run(
        self,
        ctx: ToolContext,
        *,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> ToolResult:
        """Заменить ``old_string`` на ``new_string`` (см. описание класса)."""
        ctx.activity(f"edit {file_path}")
        path = _resolve(ctx, file_path)
        rel = _rel(ctx, path)
        if not ctx.permissions.edit_allowed(rel):
            return ToolResult(
                content=f"Permission denied: edit to {rel} is not allowed.",
                is_error=True,
            )
        if not path.exists():
            return ToolResult(
                content=f"File not found: {file_path}",
                is_error=True,
            )
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as e:
            return ToolResult(content=f"OS error: {e}", is_error=True)

        if replace_all:
            count = text.count(old_string)
            if count == 0:
                return ToolResult(
                    content=f"old_string not found in {file_path}",
                    is_error=True,
                )
            new_text = text.replace(old_string, new_string)
        else:
            count = text.count(old_string)
            if count == 0:
                return ToolResult(
                    content=f"old_string not found in {file_path}",
                    is_error=True,
                )
            if count > 1:
                return ToolResult(
                    content=(
                        f"Found {count} matches for old_string in {file_path}. "
                        f"Provide more surrounding context to make it unique, "
                        f"or set replace_all=true."
                    ),
                    is_error=True,
                )
            new_text = text.replace(old_string, new_string, 1)
        try:
            path.write_text(new_text, encoding="utf-8")
        except OSError as e:
            return ToolResult(content=f"OS error: {e}", is_error=True)
        return ToolResult(
            content=f"Edited {file_path}: replaced {count} occurrence(s).",
        )


# ---------------------------------------------------------------------------
# glob
# ---------------------------------------------------------------------------


class GlobTool(Tool):
    """Найти файлы по glob-паттерну."""

    name = "glob"
    description = (
        "Find files matching a glob pattern (e.g. `**/*.py` or `src/**/*.ts`). "
        "Results are sorted by modification time (most recent first)."
    )
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "Glob pattern relative to `path` (or cwd if path is omitted).",
            },
            "path": {
                "type": "string",
                "description": "Directory to search in. Defaults to the worker cwd.",
            },
        },
        "required": ["pattern"],
    }
    permission_attr = "glob"

    async def run(
        self,
        ctx: ToolContext,
        *,
        pattern: str,
        path: str | None = None,
    ) -> ToolResult:
        """Найти файлы по glob-паттерну (см. описание класса)."""
        ctx.activity(f"glob {pattern}")
        base = _resolve(ctx, path) if path else ctx.cwd
        if not base.exists() or not base.is_dir():
            return ToolResult(content=f"Not a directory: {base}", is_error=True)
        try:
            matches = list(base.glob(pattern))
        except (OSError, ValueError) as e:
            return ToolResult(content=f"Glob error: {e}", is_error=True)

        # Сортируем по mtime desc.
        def _mtime(p: Path) -> float:
            try:
                return p.stat().st_mtime
            except OSError:
                return 0.0

        matches_sorted = sorted(matches, key=_mtime, reverse=True)
        if not matches_sorted:
            return ToolResult(content="(no matches)")
        out = "\n".join(str(m) for m in matches_sorted[:1000])
        if len(matches_sorted) > 1000:
            out += f"\n\n... and {len(matches_sorted) - 1000} more"
        return ToolResult(content=out)
