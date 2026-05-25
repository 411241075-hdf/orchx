"""Тесты MCP-bridge (P1.1)."""

from __future__ import annotations

from contextlib import AsyncExitStack

import pytest

from orchx.agent.tools.mcp import (
    MCPNotInstalled,
    MCPProxyTool,
    _check_mcp_available,
    build_mcp_tools,
)


def test_mcp_proxy_tool_name_prefixed():
    """Tool name: <server>__<mcp_tool_name>."""

    class FakeSession:
        async def call_tool(self, name, args):  # noqa: ARG002
            pass

    tool = MCPProxyTool(
        server_name="github",
        mcp_tool_def={
            "name": "list_issues",
            "description": "List GitHub issues",
            "inputSchema": {"type": "object", "properties": {"repo": {"type": "string"}}},
        },
        session=FakeSession(),
    )
    assert tool.name == "github__list_issues"
    assert "List GitHub issues" in tool.description
    assert tool.parameters["properties"]["repo"]["type"] == "string"


def test_mcp_proxy_tool_fallback_description():
    class FakeSession:
        async def call_tool(self, name, args):  # noqa: ARG002
            pass

    tool = MCPProxyTool(
        server_name="fs",
        mcp_tool_def={"name": "read_file"},
        session=FakeSession(),
    )
    # Без description в schema — fallback "[MCP fs] read_file"
    assert "[MCP fs]" in tool.description


@pytest.mark.asyncio
async def test_mcp_proxy_tool_runs_via_session():
    """run() вызывает session.call_tool с переданными kwargs."""
    calls: list[tuple[str, dict]] = []

    class FakeContent:
        def __init__(self, text):
            self.text = text

    class FakeResult:
        def __init__(self, text):
            self.content = [FakeContent(text)]
            self.isError = False

    class FakeSession:
        async def call_tool(self, name, args):
            calls.append((name, args))
            return FakeResult(f"called {name}({args})")

    tool = MCPProxyTool(
        server_name="fs",
        mcp_tool_def={"name": "read_file"},
        session=FakeSession(),
    )
    from pathlib import Path

    from orchx.agent.permissions import Permissions
    from orchx.agent.tools import ToolContext
    ctx = ToolContext(cwd=Path("."), repo_root=Path("."), permissions=Permissions())
    result = await tool.run(ctx, path="/x/y.txt")
    assert not result.is_error
    assert "read_file" in result.content
    assert calls == [("read_file", {"path": "/x/y.txt"})]


@pytest.mark.asyncio
async def test_mcp_proxy_tool_propagates_error():
    class BrokenSession:
        async def call_tool(self, name, args):  # noqa: ARG002
            raise RuntimeError("MCP server crashed")

    tool = MCPProxyTool(
        server_name="x",
        mcp_tool_def={"name": "fail"},
        session=BrokenSession(),
    )
    from pathlib import Path

    from orchx.agent.permissions import Permissions
    from orchx.agent.tools import ToolContext
    ctx = ToolContext(cwd=Path("."), repo_root=Path("."), permissions=Permissions())
    result = await tool.run(ctx)
    assert result.is_error
    assert "MCP server crashed" in result.content


@pytest.mark.asyncio
async def test_build_mcp_tools_empty_returns_empty():
    """Без configs — пустой список без import'а mcp."""
    async with AsyncExitStack() as stack:
        tools = await build_mcp_tools([], stack=stack)
        assert tools == []


@pytest.mark.asyncio
async def test_build_mcp_tools_without_mcp_installed_raises(monkeypatch):
    """Если mcp package недоступен — MCPNotInstalled."""
    if _check_mcp_available():
        pytest.skip("mcp package installed; cannot test missing-package path")
    async with AsyncExitStack() as stack:
        with pytest.raises(MCPNotInstalled):
            await build_mcp_tools(
                [{"name": "x", "command": "echo"}], stack=stack
            )
