"""Тесты symbol-intelligence tools (P1.6)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from orchx.agent.permissions import Permissions
from orchx.agent.tools import ToolContext
from orchx.agent.tools.symbols import (
    FindReferencesTool,
    FindSymbolTool,
    RenameSymbolTool,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture
def tmp_workspace(tmp_path: Path) -> Path:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "pkg" / "models.py").write_text(
        '''
class UserAccount:
    """Test class."""
    def __init__(self, name: str):
        self.name = name

    def display(self) -> str:
        return f"User: {self.name}"


def make_account(name: str) -> UserAccount:
    return UserAccount(name)
''',
        encoding="utf-8",
    )
    (tmp_path / "pkg" / "views.py").write_text(
        '''
from .models import UserAccount, make_account


def show_user(name: str) -> str:
    acc = make_account(name)
    other = UserAccount(name)
    return acc.display() + other.display()
''',
        encoding="utf-8",
    )
    return tmp_path


def _ctx(cwd: Path) -> ToolContext:
    return ToolContext(cwd=cwd, repo_root=cwd, permissions=Permissions(lsp=True))


# ---- find_symbol ----


async def test_find_symbol_class(tmp_workspace: Path):
    t = FindSymbolTool()
    r = await t.run(_ctx(tmp_workspace), name="UserAccount")
    assert not r.is_error
    data = json.loads(r.content)
    files = {m["file"] for m in data}
    assert "pkg/models.py" in files
    # Класс objavлен только в models.py.
    class_matches = [m for m in data if m["kind"] == "class"]
    assert any(m["file"] == "pkg/models.py" for m in class_matches)


async def test_find_symbol_method(tmp_workspace: Path):
    t = FindSymbolTool()
    r = await t.run(_ctx(tmp_workspace), name="display")
    assert not r.is_error
    data = json.loads(r.content)
    assert any(m["kind"] == "method" and "display" in m["name"] for m in data)


async def test_find_symbol_not_found(tmp_workspace: Path):
    t = FindSymbolTool()
    r = await t.run(_ctx(tmp_workspace), name="DoesNotExist")
    assert "No definitions" in r.content


async def test_find_symbol_empty_name_errors(tmp_workspace: Path):
    t = FindSymbolTool()
    r = await t.run(_ctx(tmp_workspace), name="")
    assert r.is_error


# ---- find_references ----


async def test_find_references_class(tmp_workspace: Path):
    t = FindReferencesTool()
    r = await t.run(_ctx(tmp_workspace), name="UserAccount")
    assert not r.is_error
    data = json.loads(r.content)
    files = {m["file"] for m in data}
    # Class определён в models.py, использован в models.py (return type)
    # + в views.py (import + 2 usage'а).
    assert "pkg/models.py" in files
    assert "pkg/views.py" in files


async def test_find_references_word_boundary(tmp_workspace: Path):
    """Не должен матчить 'displayName' для 'display'."""
    (tmp_workspace / "other.py").write_text(
        "displayName = 'x'\ndef other(): pass\n", encoding="utf-8"
    )
    t = FindReferencesTool()
    r = await t.run(_ctx(tmp_workspace), name="display")
    data = json.loads(r.content)
    # 'displayName' не должен попасть.
    assert not any("displayName" in m["snippet"] for m in data)


# ---- rename_symbol ----


async def test_rename_symbol_class(tmp_workspace: Path):
    t = RenameSymbolTool()
    r = await t.run(
        _ctx(tmp_workspace),
        old_name="UserAccount",
        new_name="Account",
    )
    assert not r.is_error
    data = json.loads(r.content)
    assert "files" in data
    affected = {f["file"] for f in data["files"]}
    assert "pkg/models.py" in affected
    # Проверим, что код перезаписался.
    models_src = (tmp_workspace / "pkg" / "models.py").read_text(encoding="utf-8")
    assert "class Account" in models_src
    assert "UserAccount" not in models_src


async def test_rename_symbol_dry_run(tmp_workspace: Path):
    t = RenameSymbolTool()
    original = (tmp_workspace / "pkg" / "models.py").read_text(encoding="utf-8")
    r = await t.run(
        _ctx(tmp_workspace),
        old_name="UserAccount",
        new_name="Account",
        dry_run=True,
    )
    assert not r.is_error
    # Файл не должен измениться.
    assert (tmp_workspace / "pkg" / "models.py").read_text(encoding="utf-8") == original


async def test_rename_symbol_invalid_new_name(tmp_workspace: Path):
    t = RenameSymbolTool()
    r = await t.run(
        _ctx(tmp_workspace),
        old_name="UserAccount",
        new_name="123 invalid",
    )
    assert r.is_error
    assert "identifier" in r.content


async def test_rename_symbol_same_name_noop(tmp_workspace: Path):
    t = RenameSymbolTool()
    r = await t.run(
        _ctx(tmp_workspace),
        old_name="UserAccount",
        new_name="UserAccount",
    )
    assert r.is_error
    assert "no-op" in r.content


async def test_rename_symbol_no_python_files_in_scope(tmp_path: Path):
    (tmp_path / "x.txt").write_text("hi", encoding="utf-8")
    t = RenameSymbolTool()
    r = await t.run(_ctx(tmp_path), old_name="x", new_name="y")
    assert r.is_error
    assert "Python only" in r.content
