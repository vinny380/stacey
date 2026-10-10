"""Lazy MCP discovery using FastMCP's proxy and search transform."""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx2
from fastmcp import Client, FastMCP
from fastmcp.client.transports import SSETransport, StreamableHttpTransport
from fastmcp.exceptions import ToolError
from fastmcp.mcp_config import MCPConfig
from fastmcp.server import create_proxy
from fastmcp.server.transforms.search import BM25SearchTransform
from fastmcp.tools import ToolResult
from mcp.types import TextContent

MAX_RESULTS = 5
_SEARCH = "_stacy_discover_tools"
_CALL = "_stacy_execute_tool"
_ENV = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class ExactFirstSearch(BM25SearchTransform):
    async def _search(self, tools, query):
        exact = [tool for tool in tools if tool.name.casefold() == query.strip().casefold()]
        return exact or await super()._search(tools, query)


def _http_client(**kwargs) -> httpx2.AsyncClient:
    # Custom API-key headers must never follow redirects to another origin.
    # Use a server's canonical MCP URL, including its trailing slash if required.
    return httpx2.AsyncClient(**{**kwargs, "follow_redirects": False})


def _resolve_env(value: Any) -> Any:
    if isinstance(value, str):

        def replace(match):
            name = match[1]
            if not os.environ.get(name):
                raise ToolError(f"Error: Set {name} locally before connecting this MCP server")
            return os.environ[name]

        return _ENV.sub(replace, value)
    if isinstance(value, dict):
        return {key: _resolve_env(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_resolve_env(item) for item in value]
    return value


def load_servers(path: Path | None) -> dict[str, dict]:
    """Validate the directory without connecting or resolving credential values."""
    if path is None:
        return {}
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        raise ValueError(f"Cannot read MCP config at {path}; expected valid JSON") from None
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    if not isinstance(servers, dict):
        raise ValueError("MCP config must contain an mcpServers object")
    result = {}
    for name, entry in servers.items():
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name) or name in {"search_tools", "call_tool"}:
            raise ValueError(
                "MCP server names must be unique short letters/numbers/underscore/dash aliases"
            )
        if not isinstance(entry, dict):
            raise ValueError(f"MCP server {name} must be an object")
        if not isinstance(entry.get("enabled", True), bool):
            raise ValueError(f"MCP server {name}: enabled must be true or false")
        if not entry.get("enabled", True):
            continue
        if bool(entry.get("url")) == bool(entry.get("command")):
            raise ValueError(f"MCP server {name} needs exactly one of url or command")
        try:
            MCPConfig.from_dict({"mcpServers": {name: entry}})
        except ValueError:
            # Pydantic errors may contain credentials from the original config.
            raise ValueError(f"Invalid transport configuration for MCP server {name}") from None
        result[name] = entry
    return result


class MCPGateway:
    def __init__(self, servers: dict[str, dict], *, targets: dict[str, FastMCP] | None = None):
        self.servers = servers
        self._targets = targets or {}
        self._proxies: dict[str, FastMCP] = {}
        self._factories: dict[str, Callable[[], Awaitable[dict]]] = {}
        self._search_cache: OrderedDict = OrderedDict()
        self.adapters: dict[str, Any] = {}
        self._build_directory()

    def _build_directory(self) -> None:
        self._directory = FastMCP("Stacy MCP directory")
        for name, entry in self.servers.items():
            # Only these local descriptions enter the directory index. No upstream
            # tools/list request happens until a particular server is searched.
            def descriptor() -> dict:
                return {}

            self._directory.tool(
                descriptor,
                name=name,
                description=entry.get("description") or f"MCP server {name}",
            )
        self._directory.add_transform(BM25SearchTransform(max_results=MAX_RESULTS))

    def add_lazy_server(
        self, name: str, description: str, factory: Callable[[], Awaitable[dict]]
    ) -> None:
        self.servers[name] = {"description": description}
        self._factories[name] = factory
        self._build_directory()

    def metadata(self) -> list[dict]:
        return [
            {"server": name, "description": entry.get("description") or f"MCP server {name}"}
            for name, entry in self.servers.items()
        ]

    async def search_servers(self, query: str, offset: int = 0) -> dict:
        if offset < 0:
            raise ToolError("Error: offset must be nonnegative")
        if query.strip():
            exact = [
                item
                for item in self.metadata()
                if item["server"].casefold() == query.strip().casefold()
            ]
            if exact:
                return {"servers": exact, "configured_count": len(self.servers)}
            async with Client(self._directory) as client:
                result = await client.call_tool("search_tools", {"query": query})
            matches = [
                {"server": item["name"], "description": item.get("description", "")}
                for item in result.data
            ]
            return {"servers": matches, "configured_count": len(self.servers)}
        matches = self.metadata()[offset : offset + MAX_RESULTS]
        next_offset = offset + len(matches)
        return {
            "servers": matches,
            "configured_count": len(self.servers),
            "next_offset": next_offset if next_offset < len(self.servers) else None,
        }

    def _proxy(self, server: str) -> FastMCP:
        if server not in self.servers:
            raise ToolError(
                "Error: Unknown MCP server. Use search_mcp_servers for its exact alias."
            )
        if server not in self._proxies:
            if server in self._targets:
                target = self._targets[server]
            else:
                entry = _resolve_env(self.servers[server])
                entry = {k: v for k, v in entry.items() if k not in {"enabled", "description"}}
                if "command" in entry:
                    # No orphaned subprocess after a request; servers must support
                    # reconnecting. cwd is explicit for portability to the Pi.
                    entry["keep_alive"] = False
                config = MCPConfig.from_dict({"mcpServers": {server: entry}})
                target = config.mcpServers[server].to_transport()
                if isinstance(target, (StreamableHttpTransport, SSETransport)):
                    target.httpx_client_factory = _http_client
            # A regular Client preserves its default of not forwarding Stacy's
            # inbound HTTP credentials to a different upstream server.
            target = Client(target, timeout=55, init_timeout=15)
            proxy = create_proxy(target, mask_error_details=True, provider_error_strategy="raise")
            proxy.add_transform(
                ExactFirstSearch(
                    max_results=MAX_RESULTS,
                    search_tool_name=_SEARCH,
                    call_tool_name=_CALL,
                )
            )
            self._proxies[server] = proxy
        return self._proxies[server]

    async def _request(self, server: str, name: str, arguments: dict):
        try:
            if server in self._factories and server not in self._proxies:
                entry = await self._factories[server]()
                self.servers[server] = entry
            proxy = self._proxy(server)
            # Bound connection/discovery as well as execution. Unknown/failed
            # servers cannot take down the local alarm and memory server.
            async with asyncio.timeout(60):
                async with Client(proxy, timeout=55, init_timeout=15) as client:
                    return await client.call_tool(name, arguments, raise_on_error=False)
        except ToolError:
            raise
        except Exception:
            # Exception text may contain a URL with a token or auth headers.
            raise ToolError(
                f"Error: MCP server {server} could not complete the request. Check its process, "
                "connection and authentication locally. Do not assume the action succeeded "
                "or retry a write automatically."
            ) from None

    async def search_tools(self, server: str, query: str) -> ToolResult:
        if not query.strip():
            raise ToolError("Error: Use capability keywords to search tools, e.g. calendar events")
        key = (server, query.strip().casefold())
        cached = self._search_cache.get(key)
        if cached and time.monotonic() - cached[0] < 300:
            self._search_cache.move_to_end(key)
            return ToolResult(structured_content={"server": server, "tools": cached[1]})
        result = await self._request(server, _SEARCH, {"query": query})
        if result.is_error:
            return self._error_result(result.content)
        # A nonempty object keeps Pipecat from treating [] as a failed MCP call.
        matches = result.data
        exact = [t for t in matches if t["name"].casefold() == key[1]]
        matches = exact or matches
        # Keep complete input schemas; output schemas and transport metadata do
        # not help the model construct a call and can dominate its context.
        matches = [
            {k: v for k, v in t.items() if k in {"name", "description", "inputSchema"}}
            for t in matches
        ]
        if server == "composio":
            from stacy.composio_context import SEARCH_DESCRIPTION

            for item in matches:
                if item["name"] == "COMPOSIO_SEARCH_TOOLS":
                    item["description"] = SEARCH_DESCRIPTION
        self._search_cache[key] = (time.monotonic(), matches)
        self._search_cache.move_to_end(key)
        while len(self._search_cache) > 128:
            self._search_cache.popitem(last=False)
        return ToolResult(structured_content={"server": server, "tools": matches})

    async def call_tool(self, server: str, tool: str, arguments: dict) -> ToolResult:
        async def invoke(name, args):
            return await self._request(server, _CALL, {"name": name, "arguments": args})

        adapter = self.adapters.get(server)
        result = (
            await adapter.call(tool, arguments, invoke)
            if adapter
            else await invoke(tool, arguments)
        )
        if result.is_error:
            return self._error_result(result.content)
        content = result.content
        if not any(isinstance(block, TextContent) and block.text for block in content):
            # Pipecat currently reads text only and ignores isError/structuredContent.
            # A valid void or image-only response must not look like a failed call.
            content = [
                TextContent(
                    type="text",
                    text=json.dumps(
                        {
                            "server": server,
                            "tool": tool,
                            "is_error": False,
                            "result": result.structured_content,
                        }
                    ),
                ),
                *content,
            ]
        # Preserve remote error flags and all content types, not just plain text.
        return ToolResult(
            content=content,
            structured_content=result.structured_content,
            is_error=result.is_error,
        )

    @staticmethod
    def _error_result(content: list) -> ToolResult:
        # Pipecat discards MCP's error flag, so make it explicit in spoken-model
        # context and detectable by Stacy's tool_reported_error trace observer.
        return ToolResult(
            content=[
                TextContent(
                    type="text",
                    text="Error calling MCP tool. Action not confirmed; "
                    "do not retry writes blindly.\n",
                ),
                *content,
            ],
            is_error=True,
        )

    def register(self, host: FastMCP) -> None:
        @host.tool
        async def search_mcp_servers(query: str = "", offset: int = 0) -> dict:
            """Find configured MCP servers by capability or alias (e.g. Gmail, calendar, Slack).

            Returns at most five descriptions, never tool catalogs. Empty query lists
            servers; use next_offset to browse. A good description makes a server searchable.
            """
            return await self.search_servers(query, offset)

        @host.tool
        async def search_mcp_tools(server: str, query: str) -> ToolResult:
            """Find tools within an exact server alias from search_mcp_servers.

            Returns at most five matching tool definitions with their full input schemas.
            Search uses keywords (names, descriptions, parameters). Try other keywords
            if empty. Only this server is contacted; other servers stay disconnected.
            """
            return await self.search_tools(server, query)

        @host.tool
        async def call_mcp_tool(server: str, tool: str, arguments: dict[str, Any]) -> ToolResult:
            """Execute a discovered MCP tool on its exact server, following its input schema.

            Reuse previously discovered tools when appropriate. Supply {} for no arguments.
            Tool/server names are separate so identical names on different servers are safe.
            Never assume success after an error/timeout or blindly retry a write.
            """
            return await self.call_tool(server, tool, arguments)
