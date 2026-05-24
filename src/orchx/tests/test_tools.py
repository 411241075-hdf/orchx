"""Тесты for ``orchx.agent.tools``: fs, search, shell, todo, registry."""

from __future__ import annotations

import os
from pathlib import Path

from orchx.agent.permissions import Permissions
from orchx.agent.tools import (
    ToolContext,
    build_tool_registry,
    permission_denied,
    to_openai_schema,
)


def _ctx(
    cwd: Path,
    perms: Permissions | None = None,
    *,
    repo_root: Path | None = None,
) -> ToolContext:
    return ToolContext(
        cwd=cwd,
        repo_root=repo_root if repo_root is not None else cwd,
        permissions=perms
        or Permissions(edit=True, bash={"echo*": "allow", "*": "deny"}),
    )


# ---------------------------------------------------------------------------
# Registry / schema
# ---------------------------------------------------------------------------


def test_registry_respects_permissions(tmp_path: Path) -> None:
    # Полностью «open» воркер.
    ctx = _ctx(tmp_path)
    reg = build_tool_registry(ctx)
    assert {
        "read",
        "write",
        "edit",
        "glob",
        "grep",
        "codesearch",
        "bash",
        "todowrite",
    } <= set(reg.keys())

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
    r = await reg["edit"].run(
        ctx, file_path="f.py", old_string="x = 1", new_string="x = 2"
    )
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


async def test_bash_output_truncation_marker(tmp_path: Path) -> None:
    """Если stdout > 50KB, в результате должен быть маркер truncation."""
    perms = Permissions(edit=True, bash={"yes*": "allow", "head*": "allow", "*": "deny"})
    ctx = _ctx(tmp_path, perms)
    reg = build_tool_registry(ctx)
    # Генерим ~60KB stdout командой yes (одна команда без injection).
    # `yes "padding"` без head'а вечно работает; используем `head` напрямую.
    # Чтобы избежать injection-операторов — записываем в файл сначала
    # большой контент, потом cat'аем. Но cat не в allowlist.
    # Простейший workaround — `head -c 60000 /dev/urandom | base64` имеет |.
    # Используем `head` от /dev/zero.
    r = await reg["bash"].run(
        ctx,
        command="head -c 60000 /dev/zero",
        timeout_ms=5000,
    )
    if r.is_error and "Permission denied" in r.content:
        import pytest

        pytest.skip(f"head not allowed: {r.content[:200]}")
    assert "truncated at 50KB" in r.content


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


# ---------------------------------------------------------------------------
# Sandbox path traversal — TASK-1
# ---------------------------------------------------------------------------


async def test_read_blocks_path_outside_repo(tmp_path: Path) -> None:
    """Чтение абсолютного пути вне cwd и repo_root должно быть отклонено."""
    # cwd и repo_root — оба внутри tmp_path; /etc явно снаружи.
    ctx = _ctx(tmp_path, repo_root=tmp_path)
    reg = build_tool_registry(ctx)
    r = await reg["read"].run(ctx, file_path="/etc/hosts")
    assert r.is_error
    assert "Permission denied" in r.content
    assert "sandbox" in r.content.lower()


async def test_read_can_access_repo_root_files(tmp_path: Path) -> None:
    """Read с cwd-worktree должен видеть файлы из repo_root."""
    # Имитируем сетап: repo_root содержит worktree + общий файл.
    repo_root = tmp_path / "repo"
    worktree = repo_root / "wt"
    worktree.mkdir(parents=True)
    shared = repo_root / "AGENTS.md"
    shared.write_text("shared agent instructions\n")
    ctx = _ctx(worktree, repo_root=repo_root)
    reg = build_tool_registry(ctx)
    r = await reg["read"].run(ctx, file_path=str(shared))
    assert not r.is_error
    assert "shared agent instructions" in r.content


async def test_write_blocks_path_outside_worktree(tmp_path: Path) -> None:
    """Запись через `../foo.txt` за пределы cwd — блокировка без побочных эффектов."""
    repo_root = tmp_path / "repo"
    worktree = repo_root / "sub"
    worktree.mkdir(parents=True)
    ctx = _ctx(worktree, repo_root=repo_root)
    reg = build_tool_registry(ctx)
    r = await reg["write"].run(
        ctx, file_path="../escape.txt", content="malicious"
    )
    assert r.is_error
    assert "Permission denied" in r.content
    # Файл не должен появиться ни в repo_root, ни нигде ещё.
    assert not (repo_root / "escape.txt").exists()


async def test_edit_blocks_path_outside_worktree(tmp_path: Path) -> None:
    """Edit с абсолютным путём вне worktree — блокировка."""
    repo_root = tmp_path / "repo"
    worktree = repo_root / "sub"
    worktree.mkdir(parents=True)
    outside = tmp_path / "outside.txt"
    outside.write_text("original\n")
    ctx = _ctx(worktree, repo_root=repo_root)
    reg = build_tool_registry(ctx)
    r = await reg["edit"].run(
        ctx,
        file_path=str(outside),
        old_string="original",
        new_string="hacked",
    )
    assert r.is_error
    assert "Permission denied" in r.content
    # Файл не должен быть изменён.
    assert outside.read_text() == "original\n"


async def test_bash_workdir_must_be_inside_cwd(tmp_path: Path) -> None:
    """bash с workdir вне cwd — блокировка до exec'а."""
    repo_root = tmp_path / "repo"
    worktree = repo_root / "sub"
    worktree.mkdir(parents=True)
    perms = Permissions(edit=True, bash={"echo*": "allow", "*": "deny"})
    ctx = _ctx(worktree, perms, repo_root=repo_root)
    reg = build_tool_registry(ctx)
    r = await reg["bash"].run(ctx, command="echo hi", workdir="/tmp")
    assert r.is_error
    assert "Permission denied" in r.content
    assert "workdir" in r.content.lower()


async def test_grep_blocks_path_outside_repo(tmp_path: Path) -> None:
    """grep с path за пределы cwd+repo_root — блокировка."""
    repo_root = tmp_path / "repo"
    worktree = repo_root / "sub"
    worktree.mkdir(parents=True)
    ctx = _ctx(worktree, repo_root=repo_root)
    reg = build_tool_registry(ctx)
    r = await reg["grep"].run(ctx, pattern="root", path="/etc")
    assert r.is_error
    assert "Permission denied" in r.content


async def test_read_blocks_symlink_escape(tmp_path: Path) -> None:
    """Symlink из worktree наружу не должен пропускать чтение наружу."""
    if os.name != "posix":
        return  # symlinks без админ-прав на Windows проблематичны
    repo_root = tmp_path / "repo"
    worktree = repo_root / "sub"
    worktree.mkdir(parents=True)
    # Готовим файл-жертву вне worktree и repo_root.
    victim_dir = tmp_path / "victim"
    victim_dir.mkdir()
    (victim_dir / "secrets.txt").write_text("topsecret\n")
    # Внутри worktree симлинк на жертву.
    link = worktree / "shortcut"
    link.symlink_to(victim_dir)
    ctx = _ctx(worktree, repo_root=repo_root)
    reg = build_tool_registry(ctx)
    r = await reg["read"].run(ctx, file_path="shortcut/secrets.txt")
    assert r.is_error
    assert "Permission denied" in r.content


# ---------------------------------------------------------------------------
# TaskTool — TASK-4
# ---------------------------------------------------------------------------


def test_task_tool_in_registry_when_permission_enabled(tmp_path: Path) -> None:
    """`task` появляется в registry только если permissions.task=True."""
    perms_with = Permissions(edit=True, bash={"*": "deny"}, task=True)
    perms_without = Permissions(edit=True, bash={"*": "deny"}, task=False)
    assert "task" in build_tool_registry(_ctx(tmp_path, perms_with))
    assert "task" not in build_tool_registry(_ctx(tmp_path, perms_without))


async def test_task_tool_blocks_nested_subagents(tmp_path: Path) -> None:
    """Если уже sub-agent (depth>=1), новый task-вызов отвергается."""
    import os

    from orchx.agent.tools.task import TaskTool

    perms = Permissions(edit=True, bash={"*": "deny"}, task=True)
    ctx = _ctx(tmp_path, perms)
    tool = TaskTool()
    os.environ["ORCHX_SUBAGENT_DEPTH"] = "1"
    try:
        r = await tool.run(
            ctx,
            description="nested",
            prompt="dive deeper",
            subagent_role="explore",
        )
    finally:
        os.environ.pop("ORCHX_SUBAGENT_DEPTH", None)
    assert r.is_error
    assert "Permission denied" in r.content
    assert "nested" in r.content.lower() or "depth" in r.content.lower()


def test_task_subagent_spec_explore_is_read_only(tmp_path: Path) -> None:
    """Sub-agent с role=explore не должен иметь edit/bash/task в реестре."""
    from orchx.agent.tools.task import _build_subagent_spec

    parent_perms = Permissions(
        edit=True,
        bash={"git status*": "allow", "*": "deny"},
        task=True,
    )
    sub_spec = _build_subagent_spec("explore", parent_perms)
    sub_ctx = _ctx(tmp_path, sub_spec.permissions)
    reg = build_tool_registry(sub_ctx)
    assert "edit" not in reg
    assert "write" not in reg
    assert "bash" not in reg
    assert "task" not in reg
    # Read-tools остаются.
    assert "read" in reg
    assert "grep" in reg


def test_task_subagent_spec_general_strips_only_task(tmp_path: Path) -> None:
    """Sub-agent с role=general наследует permissions, но без вложенного task."""
    from orchx.agent.tools.task import _build_subagent_spec

    parent_perms = Permissions(
        edit=True,
        bash={"echo*": "allow", "*": "deny"},
        task=True,
    )
    sub_spec = _build_subagent_spec("general", parent_perms)
    sub_ctx = _ctx(tmp_path, sub_spec.permissions)
    reg = build_tool_registry(sub_ctx)
    assert "edit" in reg
    assert "bash" in reg
    assert "task" not in reg


# ---------------------------------------------------------------------------
# WebFetchTool — TASK-7
# ---------------------------------------------------------------------------


def test_webfetch_in_registry_when_permission_enabled(tmp_path: Path) -> None:
    """`webfetch` появляется в registry только при permission.webfetch=True."""
    perms_with = Permissions(edit=True, bash={"*": "deny"}, webfetch=True)
    perms_without = Permissions(edit=True, bash={"*": "deny"}, webfetch=False)
    assert "webfetch" in build_tool_registry(_ctx(tmp_path, perms_with))
    assert "webfetch" not in build_tool_registry(_ctx(tmp_path, perms_without))


async def test_webfetch_blocks_private_ip(monkeypatch, tmp_path: Path) -> None:
    """webfetch на хост, резолвящийся в private IP — блокируется."""
    import socket

    from orchx.agent.tools.web import WebFetchTool

    def fake_resolve(host, *args, **kwargs):  # noqa: ANN001, ARG001
        return [(socket.AF_INET, None, None, "", ("10.0.0.5", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_resolve)
    perms = Permissions(edit=False, bash={"*": "deny"}, webfetch=True)
    ctx = _ctx(tmp_path, perms)
    tool = WebFetchTool()
    r = await tool.run(ctx, url="https://internal.lan/foo")
    assert r.is_error
    assert "Permission denied" in r.content
    assert "private" in r.content.lower() or "10.0.0.5" in r.content


async def test_webfetch_blocks_cloud_metadata(monkeypatch, tmp_path: Path) -> None:
    """169.254.169.254 (AWS/GCP/Azure metadata) — блокируется."""
    import socket

    from orchx.agent.tools.web import WebFetchTool

    def fake_resolve(host, *args, **kwargs):  # noqa: ANN001, ARG001
        return [(socket.AF_INET, None, None, "", ("169.254.169.254", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_resolve)
    perms = Permissions(edit=False, bash={"*": "deny"}, webfetch=True)
    ctx = _ctx(tmp_path, perms)
    tool = WebFetchTool()
    r = await tool.run(ctx, url="https://metadata.example/")
    assert r.is_error
    assert "Permission denied" in r.content


async def test_webfetch_rejects_non_http_scheme(tmp_path: Path) -> None:
    """file:// и ftp:// — отказ без сетевого запроса."""
    from orchx.agent.tools.web import WebFetchTool

    perms = Permissions(edit=False, bash={"*": "deny"}, webfetch=True)
    ctx = _ctx(tmp_path, perms)
    tool = WebFetchTool()
    r = await tool.run(ctx, url="file:///etc/passwd")
    assert r.is_error
    assert "scheme" in r.content.lower()


def test_webfetch_strip_html_removes_tags() -> None:
    """HTML с тегами должен превратиться в plain text при format=markdown."""
    from orchx.agent.tools.web import _strip_html_to_text

    html_str = (
        "<html><head><style>x { color: red; }</style></head>"
        "<body><h1>Title</h1><p>hello <b>world</b></p>"
        '<a href="https://x.com">link</a></body></html>'
    )
    out = _strip_html_to_text(html_str)
    assert "<" not in out and ">" not in out
    assert "# Title" in out
    assert "hello world" in out
    assert "[link](https://x.com)" in out
    # style-блок должен быть удалён.
    assert "color: red" not in out


def test_permission_denied_helper_format() -> None:
    """Хелпер выдаёт стабильный 'Permission denied:' prefix + hint."""
    r = permission_denied(
        tool="write", target="foo.txt", reason="not allowed", hint="try X"
    )
    assert r.is_error
    assert r.content.startswith("Permission denied: write on foo.txt — not allowed.")
    assert "Hint: try X" in r.content
    # Без hint — без второй строки.
    r2 = permission_denied(tool="read", target="x", reason="oops")
    assert r2.is_error
    assert "Hint" not in r2.content
