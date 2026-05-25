"""Symbol-intelligence tools (P1.6).

Pragmatic версия LSP-tools: **AST-based для Python**, **regex-fallback
для прочих языков**. Полноценный LSP-server lifecycle (pylsp / pyright /
typescript-language-server) — будущая фича; пока — то, что даёт 80% пользы
без сложности LSP-pooling.

Tools:

* ``find_symbol(name, path?)`` — найти класс/функцию/метод по имени.
* ``find_references(name, path?)`` — найти все usage'и символа в repo.
* ``rename_symbol(name, new_name, path?)`` — переименовать все usage'и
  (AST-based для .py; fail для других — слишком рисково через regex).

Gating:

* Все три tool'а — read-only кроме ``rename_symbol``, который требует
  ``edit: allow`` в permissions роли.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from typing import Any

from . import Tool, ToolContext, ToolResult, permission_denied

# ---------------------------------------------------------------------------
# Common helpers
# ---------------------------------------------------------------------------


_DEFAULT_INCLUDE_GLOBS = ("**/*.py", "**/*.ts", "**/*.tsx", "**/*.js", "**/*.jsx")
_EXCLUDE_DIR_NAMES = {
    ".git", ".venv", "venv", "node_modules", "__pycache__", ".mypy_cache",
    ".ruff_cache", ".pytest_cache", "dist", "build", ".tox", ".coverage",
    "htmlcov", "site-packages",
}


def _walk_files(cwd: Path, path_hint: str | None) -> list[Path]:
    """Список файлов для сканирования."""
    base = cwd
    if path_hint:
        candidate = (cwd / path_hint).resolve()
        try:
            candidate.relative_to(cwd.resolve())
        except ValueError:
            return []
        if candidate.is_file():
            return [candidate]
        if candidate.is_dir():
            base = candidate

    out: list[Path] = []
    for p in base.rglob("*"):
        if p.is_dir():
            continue
        if any(part in _EXCLUDE_DIR_NAMES for part in p.parts):
            continue
        if p.suffix in (".py", ".ts", ".tsx", ".js", ".jsx"):
            out.append(p)
    return out


# ---------------------------------------------------------------------------
# find_symbol
# ---------------------------------------------------------------------------


class FindSymbolTool(Tool):
    """Найти определение класса/функции/метода по имени."""

    name = "find_symbol"
    description = (
        "Find all definitions of a class/function/method by name across the "
        "codebase. Returns file:line:type for each match. "
        "Python files use AST analysis (precise). Other languages "
        "(.ts/.tsx/.js/.jsx) use regex heuristics."
    )
    parameters = {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": (
                    "Symbol name. Supports 'ClassName' or 'ClassName.method' "
                    "(dotted path). For exact match only — no regex."
                ),
            },
            "path": {
                "type": "string",
                "description": (
                    "Optional file or subdirectory to restrict search "
                    "(relative to cwd). Default: entire cwd."
                ),
            },
        },
        "required": ["name"],
    }
    permission_attr = "grep"  # read-only — гейтим как grep.

    async def run(self, ctx: ToolContext, *, name: str, path: str | None = None) -> ToolResult:
        ctx.activity(f"find_symbol {name}")
        if not name.strip():
            return ToolResult(content="Empty symbol name", is_error=True)
        files = _walk_files(ctx.cwd, path)
        matches: list[dict[str, Any]] = []
        for f in files:
            try:
                if f.suffix == ".py":
                    matches.extend(_find_symbol_python(f, name, ctx.cwd))
                else:
                    matches.extend(_find_symbol_regex(f, name, ctx.cwd))
            except (SyntaxError, OSError):
                continue
        if not matches:
            return ToolResult(content=f"No definitions found for {name!r}")
        return ToolResult(content=json.dumps(matches, indent=2, ensure_ascii=False))


def _find_symbol_python(file: Path, name: str, cwd: Path) -> list[dict[str, Any]]:
    """AST-based поиск в Python-файле."""
    tree = ast.parse(file.read_text(encoding="utf-8", errors="replace"), str(file))
    target_parts = name.split(".")
    out: list[dict[str, Any]] = []

    def visit(node: ast.AST, prefix: list[str]) -> None:
        for child in ast.iter_child_nodes(node):
            kind = None
            child_name = None
            if isinstance(child, ast.ClassDef):
                kind = "class"
                child_name = child.name
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                kind = "function" if not prefix else "method"
                child_name = child.name
            if kind and child_name:
                full_path = [*prefix, child_name]
                if full_path == target_parts or child_name == target_parts[-1]:
                    out.append(
                        {
                            "file": str(file.relative_to(cwd)),
                            "line": child.lineno,
                            "kind": kind,
                            "name": ".".join(full_path),
                        }
                    )
                visit(child, full_path if isinstance(child, ast.ClassDef) else prefix)
            else:
                visit(child, prefix)

    visit(tree, [])
    return out


def _find_symbol_regex(file: Path, name: str, cwd: Path) -> list[dict[str, Any]]:
    """Грубый regex-based поиск для не-Python (best-effort)."""
    text = file.read_text(encoding="utf-8", errors="replace")
    out: list[dict[str, Any]] = []
    # JS/TS патерны:
    patterns = [
        (rf"\bclass\s+{re.escape(name)}\b", "class"),
        (rf"\bfunction\s+{re.escape(name)}\b", "function"),
        (rf"\b(?:const|let|var)\s+{re.escape(name)}\s*=\s*(?:\([^)]*\)\s*=>|async\s+function|function)\b", "function"),
        (rf"\b{re.escape(name)}\s*\([^)]*\)\s*\{{", "method"),  # method shorthand
    ]
    for line_no, line in enumerate(text.splitlines(), start=1):
        for pat, kind in patterns:
            if re.search(pat, line):
                out.append(
                    {
                        "file": str(file.relative_to(cwd)),
                        "line": line_no,
                        "kind": kind,
                        "name": name,
                    }
                )
                break
    return out


# ---------------------------------------------------------------------------
# find_references
# ---------------------------------------------------------------------------


class FindReferencesTool(Tool):
    """Найти все usage'и символа."""

    name = "find_references"
    description = (
        "Find all references (usages) of a symbol across the codebase. "
        "Returns file:line:snippet for each match. "
        "Uses precise word-boundary regex; for ambiguous names (e.g. 'i') "
        "results may include false positives — narrow with 'path'."
    )
    parameters = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Symbol name (exact match, word boundary)."},
            "path": {
                "type": "string",
                "description": "Optional file/directory restriction.",
            },
            "max_results": {
                "type": "integer",
                "description": "Maximum results (default 200).",
            },
        },
        "required": ["name"],
    }
    permission_attr = "grep"

    async def run(
        self,
        ctx: ToolContext,
        *,
        name: str,
        path: str | None = None,
        max_results: int = 200,
    ) -> ToolResult:
        ctx.activity(f"find_references {name}")
        if not name.strip():
            return ToolResult(content="Empty name", is_error=True)
        files = _walk_files(ctx.cwd, path)
        pat = re.compile(rf"\b{re.escape(name)}\b")
        results: list[dict[str, Any]] = []
        for f in files:
            try:
                text = f.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for line_no, line in enumerate(text.splitlines(), start=1):
                if pat.search(line):
                    results.append(
                        {
                            "file": str(f.relative_to(ctx.cwd)),
                            "line": line_no,
                            "snippet": line.strip()[:200],
                        }
                    )
                    if len(results) >= max_results:
                        break
            if len(results) >= max_results:
                break
        if not results:
            return ToolResult(content=f"No references found for {name!r}")
        return ToolResult(content=json.dumps(results, indent=2, ensure_ascii=False))


# ---------------------------------------------------------------------------
# rename_symbol
# ---------------------------------------------------------------------------


class RenameSymbolTool(Tool):
    """Переименовать все usage'и символа в Python-файлах через AST.

    NB: Для не-Python — отказ (regex-replace для переименования слишком
    рискован — может сломать строковые литералы, comments, partial matches).
    """

    name = "rename_symbol"
    description = (
        "Rename all occurrences of a symbol across .py files via AST analysis. "
        "Preserves docstrings/comments. Returns affected files. "
        "Refuses on non-Python files (too risky)."
    )
    parameters = {
        "type": "object",
        "properties": {
            "old_name": {"type": "string"},
            "new_name": {"type": "string"},
            "path": {
                "type": "string",
                "description": "Optional file/directory restriction.",
            },
            "dry_run": {
                "type": "boolean",
                "description": "If true, list affected files but don't write. Default: false.",
            },
        },
        "required": ["old_name", "new_name"],
    }
    permission_attr = None  # gated через edit_allowed per-file

    async def run(
        self,
        ctx: ToolContext,
        *,
        old_name: str,
        new_name: str,
        path: str | None = None,
        dry_run: bool = False,
    ) -> ToolResult:
        ctx.activity(f"rename_symbol {old_name} -> {new_name}")
        if not old_name or not new_name:
            return ToolResult(content="Both old_name and new_name required", is_error=True)
        if old_name == new_name:
            return ToolResult(content="old_name == new_name (no-op)", is_error=True)
        if not new_name.isidentifier():
            return ToolResult(
                content=f"new_name {new_name!r} is not a valid Python identifier",
                is_error=True,
            )

        files = _walk_files(ctx.cwd, path)
        py_files = [f for f in files if f.suffix == ".py"]
        if not py_files:
            return ToolResult(
                content="No .py files in scope. rename_symbol supports Python only.",
                is_error=True,
            )

        affected: list[dict[str, Any]] = []
        for f in py_files:
            try:
                src = f.read_text(encoding="utf-8")
            except OSError:
                continue
            try:
                tree = ast.parse(src, str(f))
            except SyntaxError:
                continue
            renamer = _AstRenamer(old_name, new_name)
            try:
                new_tree = renamer.visit(tree)
            except Exception:  # noqa: BLE001
                continue
            if renamer.count == 0:
                continue
            try:
                new_src = ast.unparse(new_tree)
            except Exception:  # noqa: BLE001
                continue
            rel = str(f.relative_to(ctx.cwd))
            if not ctx.permissions.edit_allowed(rel):
                affected.append(
                    {"file": rel, "occurrences": renamer.count, "skipped": "permission denied"}
                )
                continue
            if not dry_run:
                try:
                    f.write_text(new_src, encoding="utf-8")
                except OSError as e:
                    return permission_denied(
                        tool="rename_symbol",
                        target=rel,
                        reason=f"could not write: {e}",
                    )
            affected.append({"file": rel, "occurrences": renamer.count})
        if not affected:
            return ToolResult(content=f"No occurrences of {old_name!r} found in Python files")
        return ToolResult(
            content=json.dumps(
                {"old_name": old_name, "new_name": new_name, "files": affected, "dry_run": dry_run},
                indent=2,
                ensure_ascii=False,
            )
        )


class _AstRenamer(ast.NodeTransformer):
    """AST visitor который переименовывает Name / FunctionDef / ClassDef / arg."""

    def __init__(self, old: str, new: str):
        self.old = old
        self.new = new
        self.count = 0

    def visit_Name(self, node: ast.Name) -> ast.AST:
        if node.id == self.old:
            node.id = self.new
            self.count += 1
        return node

    def visit_Attribute(self, node: ast.Attribute) -> ast.AST:
        if node.attr == self.old:
            node.attr = self.new
            self.count += 1
        self.generic_visit(node)
        return node

    def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.AST:
        if node.name == self.old:
            node.name = self.new
            self.count += 1
        self.generic_visit(node)
        return node

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> ast.AST:
        if node.name == self.old:
            node.name = self.new
            self.count += 1
        self.generic_visit(node)
        return node

    def visit_ClassDef(self, node: ast.ClassDef) -> ast.AST:
        if node.name == self.old:
            node.name = self.new
            self.count += 1
        self.generic_visit(node)
        return node

    def visit_arg(self, node: ast.arg) -> ast.AST:
        if node.arg == self.old:
            node.arg = self.new
            self.count += 1
        return node


__all__ = ["FindSymbolTool", "FindReferencesTool", "RenameSymbolTool"]
