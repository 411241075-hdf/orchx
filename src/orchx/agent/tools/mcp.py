"""MCP-bridge: подключение orchX-воркера к сторонним Model Context Protocol серверам (P1.1).

Подключение настраивается через frontmatter роли:

.. code-block:: yaml

   ---
   mcp_servers:
     - name: github
       command: npx
       args: ["-y", "@modelcontextprotocol/server-github"]
       env:
         GITHUB_TOKEN: ${GITHUB_TOKEN}
     - name: fs
       command: npx
       args: ["-y", "@modelcontextprotocol/server-filesystem", "/Users/x/projects"]
   ---

Tool-имена префиксуются server'ом, чтобы не конфликтовать с native tools
orchX: ``github__list_issues``, ``fs__read_file``.

Реализация требует ``pip install orchx[mcp]`` (extras добавляют ``mcp``
Python SDK).
"""

from __future__ import annotations

import logging
import os
from contextlib import AsyncExitStack
from typing import Any

from . import Tool, ToolContext, ToolResult

logger = logging.getLogger(__name__)


class MCPNotInstalled(RuntimeError):
    """Raised если ``mcp`` package не установлен."""


def _check_mcp_available() -> bool:
    try:
        import mcp  # noqa: F401
        return True
    except ImportError:
        return False


class MCPProxyTool(Tool):
    """Tool, проксирующий вызов в MCP-сервер.

    Каждый MCP-tool превращается в один экземпляр этого класса. Имя
    префиксовано server-namespace'ом: ``<server>__<mcp_tool_name>``.
    """

    permission_attr = None  # MCP-tools не гейтятся orchX permissions (это уже sandbox MCP-сервера).

    def __init__(
        self,
        *,
        server_name: str,
        mcp_tool_def: dict[str, Any],
        session: Any,
    ):
        self.name = f"{server_name}__{mcp_tool_def['name']}"
        self.description = mcp_tool_def.get("description", "") or (
            f"[MCP {server_name}] {mcp_tool_def['name']}"
        )
        self.parameters = mcp_tool_def.get("inputSchema") or {"type": "object"}
        self._server_name = server_name
        self._session = session
        self._mcp_tool_name = mcp_tool_def["name"]

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        ctx.activity(f"mcp {self.name}")
        try:
            result = await self._session.call_tool(self._mcp_tool_name, kwargs)
        except Exception as e:  # noqa: BLE001
            return ToolResult(
                content=f"[MCP {self._server_name}] tool call failed: {e}",
                is_error=True,
            )
        pieces: list[str] = []
        for c in getattr(result, "content", []) or []:
            text = getattr(c, "text", None)
            if text:
                pieces.append(text)
            else:
                # Бывают image / resource — рендерим как JSON-like.
                pieces.append(str(c)[:2000])
        body = "\n".join(pieces) or "(empty MCP response)"
        is_error = bool(getattr(result, "isError", False))
        return ToolResult(content=body, is_error=is_error)


async def build_mcp_tools(
    configs: list[dict[str, Any]],
    *,
    stack: AsyncExitStack,
) -> list[MCPProxyTool]:
    """Поднять MCP-сессии и сконвертировать их tools в orchX-Tool'ы.

    Args:
        configs: список ``mcp_servers``-entries из frontmatter роли.
            Каждая entry — dict с ``name``/``command``/``args``/``env``/``url``.
        stack: внешний :class:`AsyncExitStack`, чьим контекстом будут
            владеть MCP-сессии. Caller должен заэкзить его до окончания
            воркера (обычно — :func:`orchx.agent.worker.run_agent`).

    Returns:
        Список готовых к использованию :class:`MCPProxyTool`.

    Raises:
        MCPNotInstalled: если пакет ``mcp`` не установлен (orchx[mcp]).
    """
    if not configs:
        return []
    if not _check_mcp_available():
        raise MCPNotInstalled(
            "mcp client SDK not installed. Run: pip install 'orchx[mcp]'"
        )

    # Lazy imports — модуль грузим только когда есть конфиги.
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    tools: list[MCPProxyTool] = []
    for cfg in configs:
        name = cfg.get("name")
        if not name:
            logger.warning("MCP config without 'name', skipping: %r", cfg)
            continue
        command = cfg.get("command")
        if not command:
            logger.warning(
                "MCP config %r: only stdio transport is currently implemented "
                "(need 'command' + 'args'). URL/SSE transport — future.",
                name,
            )
            continue

        # env с подстановкой ${VAR}.
        env_in = cfg.get("env") or {}
        env: dict[str, str] = {}
        for k, v in env_in.items():
            if isinstance(v, str) and "${" in v:
                env[k] = os.path.expandvars(v)
            else:
                env[k] = str(v)

        params = StdioServerParameters(
            command=command,
            args=list(cfg.get("args") or []),
            env={**os.environ, **env} if env else None,
        )

        try:
            read, write = await stack.enter_async_context(stdio_client(params))
            session = await stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            tools_resp = await session.list_tools()
        except Exception as e:  # noqa: BLE001
            logger.warning("MCP server %r failed to start: %s", name, e)
            continue

        for t in getattr(tools_resp, "tools", []) or []:
            try:
                tools.append(
                    MCPProxyTool(
                        server_name=name,
                        mcp_tool_def=t.model_dump() if hasattr(t, "model_dump") else dict(t),
                        session=session,
                    )
                )
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "MCP server %r: failed to register tool %r: %s",
                    name,
                    getattr(t, "name", "<?>"),
                    e,
                )
        logger.info(
            "MCP server %r: connected, %s tools imported",
            name,
            sum(1 for t in tools if t.name.startswith(f"{name}__")),
        )

    return tools


__all__ = [
    "MCPNotInstalled",
    "MCPProxyTool",
    "build_mcp_tools",
]
