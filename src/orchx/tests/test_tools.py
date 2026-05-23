"""Тесты for ``orchx.agent.tools``: fs, search, shell, todo, registry."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from orchx.agent.permissions import Permissions
from orchx.agent.tools import (
    ToolContext,
    build_tool_registry,
    to_openai_schema,
)


def _ctx(cwd: Path, perms: Permissions | None = None) -> ToolContext:
    return ToolContext(
        cwd=cwd,
        repo_root=cwd,
        permissions=perms or Permissions(edit=True, bash={"echo*": "allow", "*": "deny"}),
    )


# ---------------------------------------------------------------------------
# Registry / schema
# ---------------------------------------------------------------------------


def test_registry_respects_permissions(tmp_path: Path) -> None:
    # Полностью «open» воркер.
    ctx = _ctx(tmp_path)
    reg = build_tool_registry(ctx)
    assert {"read", "write", "edit", "glob", "grep", "codesearch", "bash", "todowrite"} <= set(
        reg.keys()
    )

    # Read запрещён → ни read, ни glob, ни grep, ни codesearch.
    perms = Permissions(read=False, glob=False, grep=False, codesearch=False)
    perms.bash = {"*": "deny"}
    perms.edit = False
    reg2 = build_tool_registry(_ctx(tmp_path, perms))
    assert "read" not in reg2
    assert "glob" not in reg2
    assert "edit" not in reg2
    assert "bash" not in reg2
    assert "todowrite" in reg2  # всегда есть


def test_tool_schemas_are_valid_openai_shape(tmp_path: Path) -> None:
    reg = build_tool_registry(_ctx(tmp_path))
    for tool in reg.values():
        sch = to_openai_schema(tool)
        assert sch["type"] == "function"
        assert sch["function"]["name"] == tool.name
        params = sch["function"]["parameters"]
        assert params.get("type") == "object"
        assert "properties" in params


# ---------------------------------------------------------------------------
# fs.read
# ---------------------------------------------------------------------------


async def test_read_returns_numbered_lines(tmp_path: Path) -> None:
    f = tmp_path / "foo.txt"
    f.write_text("line one\nline two\nline three\n")
    ctx = _ctx(tmp_path)
    reg = build_tool_registry(ctx)
    r = await reg["read"].run(ctx, file_path="foo.txt")
    assert not r.is_error
    assert "1: line one" in r.content
    assert "3: line three" in r.content


async def test_read_missing_file(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    reg = build_tool_registry(ctx)
    r = await reg["read"].run(ctx, file_path="nope.txt")
    assert r.is_error
    assert "not found" in r.content.lower()


# ---------------------------------------------------------------------------
# fs.write / edit
# ---------------------------------------------------------------------------


async def test_write_to_allowed_path(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, Permissions(edit=True, bash={"*": "deny"}))
    reg = build_tool_registry(ctx)
    r = await reg["write"].run(ctx, file_path="a.txt", content="hi")
    assert not r.is_error
    assert (tmp_path / "a.txt").read_text() == "hi"


async def test_write_to_denied_path(tmp_path: Path) -> None:
    perms = Permissions(
        edit={"allowed/*": "allow", "*": "deny"},
        bash={"*": "deny"},
    )
    ctx = _ctx(tmp_path, perms)
    reg = build_tool_registry(ctx)
    r = await reg["write"].run(ctx, file_path="forbidden.txt", content="x")
    assert r.is_error
    assert "Permission denied" in r.content
    # Файл не должен появиться.
    assert not (tmp_path / "forbidden.txt").exists()

    r = await reg["write"].run(ctx, file_path="allowed/ok.txt", content="x")
    assert not r.is_error
    assert (tmp_path / "allowed" / "ok.txt").read_text() == "x"


async def test_edit_unique_match(tmp_path: Path) -> None:
    (tmp_path / "f.py").write_text("def foo():\n    return 1\n")
    ctx = _ctx(tmp_path)
    reg = build_tool_registry(ctx)
    r = await reg["edit"].run(
        ctx, file_path="f.py", old_string="return 1", new_string="return 42"
    )
    assert not r.is_error
    assert "return 42" in (tmp_path / "f.py").read_text()


async def test_edit_not_found(tmp_path: Path) -> None:
    (tmp_path / "f.py").write_text("x = 1\n")
    ctx = _ctx(tmp_path)
    reg = build_tool_registry(ctx)
    r = await reg["edit"].run(ctx, file_path="f.py", old_string="zzz", new_string="x")
    assert r.is_error
    assert "not found" in r.content.lower()


async def test_edit_ambiguous_match_requires_replace_all(tmp_path: Path) -> None:
    (tmp_path / "f.py").write_text("x = 1\nx = 1\n")
    ctx = _ctx(tmp_path)
    reg = build_tool_registry(ctx)
    r = await reg["edit"].run(ctx, file_path="f.py", old_string="x = 1", new_string="x = 2")
    assert r.is_error
    assert "2 matches" in r.content
    # С replace_all=True — успех.
    r = await reg["edit"].run(
        ctx, file_path="f.py", old_string="x = 1", new_string="x = 2", replace_all=True
    )
    assert not r.is_error
    assert (tmp_path / "f.py").read_text() == "x = 2\nx = 2\n"


# ---------------------------------------------------------------------------
# glob / grep
# ---------------------------------------------------------------------------


async def test_glob_finds_files(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("")
    (tmp_path / "b.py").write_text("")
    (tmp_path / "c.txt").write_text("")
    ctx = _ctx(tmp_path)
    reg = build_tool_registry(ctx)
    r = await reg["glob"].run(ctx, pattern="*.py")
    assert not r.is_error
    lines = r.content.splitlines()
    assert any("a.py" in ln for ln in lines)
    assert any("b.py" in ln for ln in lines)
    assert not any("c.txt" in ln for ln in lines)


async def test_grep_finds_pattern(tmp_path: Path) -> None:
    (tmp_path / "f.py").write_text("def hello():\n    pass\n")
    ctx = _ctx(tmp_path)
    reg = build_tool_registry(ctx)
    r = await reg["grep"].run(ctx, pattern="def hello")
    assert not r.is_error
    assert "def hello" in r.content


# ---------------------------------------------------------------------------
# shell.bash
# ---------------------------------------------------------------------------


async def test_bash_allowed(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    reg = build_tool_registry(ctx)
    r = await reg["bash"].run(ctx, command="echo hello")
    assert not r.is_error
    assert "hello" in r.content
    assert "<exit_code>0</exit_code>" in r.content


async def test_bash_denied_without_exec(tmp_path: Path) -> None:
    """Запрещённая команда должна возвращать is_error без побочных эффектов."""
    sentinel = tmp_path / "should_not_exist.txt"
    ctx = _ctx(tmp_path)
    reg = build_tool_registry(ctx)
    # Команда не матчит "echo*" — должна быть отвергнута.
    r = await reg["bash"].run(ctx, command=f"touch {sentinel}")
    assert r.is_error
    assert "Permission denied" in r.content
    # Главное: файла нет — exec НЕ случился.
    assert not sentinel.exists()


async def test_bash_timeout(tmp_path: Path) -> None:
    perms = Permissions(edit=True, bash={"sleep*": "allow", "*": "deny"})
    ctx = _ctx(tmp_path, perms)
    reg = build_tool_registry(ctx)
    r = await reg["bash"].run(ctx, command="sleep 5", timeout_ms=200)
    assert r.is_error
    assert "timed out" in r.content.lower()


# ---------------------------------------------------------------------------
# todo
# ---------------------------------------------------------------------------


async def test_todowrite_updates_context(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    reg = build_tool_registry(ctx)
    r = await reg["todowrite"].run(
        ctx,
        todos=[
            {"content": "first", "status": "in_progress", "priority": "high"},
            {"content": "second", "status": "pending", "priority": "medium"},
        ],
    )
    assert not r.is_error
    assert len(ctx.todos) == 2
    assert ctx.todos[0]["status"] == "in_progress"
