import asyncio
import json
import sys

import pytest
from fastmcp import Client, FastMCP
from fastmcp.server.middleware import Middleware

from stacy.mcp_gateway import MCPGateway, load_servers


class CountCatalog(Middleware):
    def __init__(self):
        self.requests = 0

    async def on_list_tools(self, context, call_next):
        self.requests += 1
        return await call_next(context)


def test_startup_is_constant_and_discovery_only_contacts_selected_server():
    async def scenario():
        servers, targets, counts = {}, {}, {}
        for i in range(12):
            name = f"calendar-{i}"
            servers[name] = {"description": f"Google calendar events account {i}"}
            upstream = FastMCP(name)
            counter = CountCatalog()
            upstream.add_middleware(counter)
            for j in range(25):

                def find_events(day: str) -> dict:
                    return {"day": day}

                upstream.tool(
                    find_events, name=f"find_events_{j}", description="Search calendar events"
                )
            targets[name], counts[name] = upstream, counter
        host = FastMCP("test host")
        gateway = MCPGateway(servers, targets=targets)
        gateway.register(host)
        async with Client(host) as client:
            names = {tool.name for tool in await client.list_tools()}
            assert names == {"search_mcp_servers", "search_mcp_tools", "call_mcp_tool"}
            assert not any(count.requests for count in counts.values())
            listing = (await client.call_tool("search_mcp_servers", {})).data
            assert len(listing["servers"]) == 5 and listing["configured_count"] == 12
            page = (await client.call_tool("search_mcp_servers", {"offset": 5})).data
            assert page["servers"][0]["server"] == "calendar-5"
            match = (
                await client.call_tool("search_mcp_servers", {"query": "Google calendar"})
            ).data
            assert len(match["servers"]) == 5
            exact = (await client.call_tool("search_mcp_servers", {"query": "calendar-9"})).data
            assert [item["server"] for item in exact["servers"]] == ["calendar-9"]
            assert not any(count.requests for count in counts.values())
            result = (
                await client.call_tool(
                    "search_mcp_tools",
                    {
                        "server": "calendar-4",
                        "query": "calendar events",
                    },
                )
            ).data
            assert result["server"] == "calendar-4" and len(result["tools"]) == 5
            schema = result["tools"][0]
            assert schema["inputSchema"]["required"] == ["day"]
            assert counts["calendar-4"].requests > 0
            assert all(
                count.requests == 0 for name, count in counts.items() if name != "calendar-4"
            )
            call = await client.call_tool(
                "call_mcp_tool",
                {
                    "server": "calendar-4",
                    "tool": schema["name"],
                    "arguments": {"day": "Monday"},
                },
            )
            assert call.data == {"day": "Monday"}
            # Discovering does not expand the model's advertised tool catalog.
            assert {tool.name for tool in await client.list_tools()} == names

    asyncio.run(scenario())


def test_collisions_empty_search_validation_and_remote_errors():
    async def scenario():
        targets = {}
        for name in ("personal", "work"):
            upstream = FastMCP(name)

            def make_tool(account):
                def read_events(day: str) -> dict:
                    return {"account": account, "day": day}

                return read_events

            upstream.tool(make_tool(name), name="read_events", description="Read calendar events")

            @upstream.tool
            def fail() -> dict:
                raise ValueError("Calendar rejected the action")

            @upstream.tool
            def no_content():
                from fastmcp.tools import ToolResult

                return ToolResult(content=[])

            # Common names used by other search-first servers must remain callable.
            upstream.tool(make_tool(name), name="search_tools", description="Search app tools")
            targets[name] = upstream
        host = FastMCP("host")
        MCPGateway({name: {"description": name} for name in targets}, targets=targets).register(
            host
        )
        async with Client(host) as client:
            for name in targets:
                result = await client.call_tool(
                    "call_mcp_tool",
                    {
                        "server": name,
                        "tool": "read_events",
                        "arguments": {"day": "today"},
                    },
                )
                assert result.data["account"] == name
            empty = await client.call_tool(
                "search_mcp_tools", {"server": "work", "query": "zzzzzzz"}
            )
            assert empty.data == {"server": "work", "tools": []} and empty.content
            search = await client.call_tool(
                "call_mcp_tool",
                {
                    "server": "work",
                    "tool": "search_tools",
                    "arguments": {"day": "today"},
                },
            )
            assert search.data["account"] == "work"
            for tool, arguments in (("read_events", {}), ("nonexistent", {}), ("fail", {})):
                result = await client.call_tool(
                    "call_mcp_tool",
                    {
                        "server": "work",
                        "tool": tool,
                        "arguments": arguments,
                    },
                    raise_on_error=False,
                )
                assert result.is_error
                assert result.content[0].text.startswith("Error")
            void = await client.call_tool(
                "call_mcp_tool",
                {
                    "server": "work",
                    "tool": "no_content",
                    "arguments": {},
                },
            )
            assert not void.is_error
            assert '"is_error": false' in void.content[0].text
            unknown = await client.call_tool(
                "search_mcp_tools",
                {
                    "server": "unknown",
                    "query": "calendar",
                },
                raise_on_error=False,
            )
            assert unknown.is_error
            assert (await client.call_tool("search_mcp_servers", {"query": "zzzzzz"})).data[
                "servers"
            ] == []

    asyncio.run(scenario())


def test_standard_stdio_config_executes_and_env_is_not_in_directory(tmp_path, monkeypatch):
    script = tmp_path / "upstream.py"
    script.write_text("""import os
from fastmcp import FastMCP
server = FastMCP("fixture")
@server.tool
def read_events(day: str) -> dict:
    return {"day": day, "credential_present": os.environ.get("SERVICE_KEY") == "test-secret"}
server.run(transport="stdio", show_banner=False)
""")
    config = tmp_path / "mcp.json"
    config.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "local": {
                        "description": "Google calendar",
                        "command": sys.executable,
                        "args": [str(script)],
                        "env": {"SERVICE_KEY": "${TEST_MCP_KEY}"},
                    },
                    "broken": {
                        "url": "https://unused.example/mcp",
                        "headers": {"x-api-key": "${MISSING_KEY}"},
                    },
                    "disabled": {"enabled": False},
                }
            }
        )
    )
    monkeypatch.setenv("TEST_MCP_KEY", "test-secret")
    monkeypatch.delenv("MISSING_KEY", raising=False)

    async def scenario():
        host = FastMCP("host")
        gateway = MCPGateway(load_servers(config))
        gateway.register(host)
        async with Client(host) as client:
            listing = await client.call_tool("search_mcp_servers", {})
            assert "test-secret" not in str(listing) and "SERVICE_KEY" not in str(listing)
            assert listing.data["configured_count"] == 2
            tools = await client.call_tool(
                "search_mcp_tools", {"server": "local", "query": "events"}
            )
            assert tools.data["tools"][0]["name"] == "read_events"
            result = await client.call_tool(
                "call_mcp_tool",
                {
                    "server": "local",
                    "tool": "read_events",
                    "arguments": {"day": "today"},
                },
            )
            assert result.data == {"day": "today", "credential_present": True}
            missing = await client.call_tool(
                "search_mcp_tools",
                {
                    "server": "broken",
                    "query": "events",
                },
                raise_on_error=False,
            )
            assert missing.is_error and "MISSING_KEY" in str(missing.content)
            # A misconfigured provider does not break other providers.
            assert not (
                await client.call_tool(
                    "search_mcp_tools",
                    {
                        "server": "local",
                        "query": "events",
                    },
                )
            ).is_error

    asyncio.run(scenario())


def test_config_failure_is_clear_and_does_not_expose_credentials(tmp_path):
    config = tmp_path / "mcp.json"
    config.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "bad": {
                        "url": "https://unused.example/mcp",
                        "headers": "secret-token",
                    }
                }
            }
        )
    )
    with pytest.raises(ValueError, match="Invalid transport configuration") as failure:
        load_servers(config)
    assert "secret-token" not in str(failure.value)
    assert load_servers(None) == {}


def test_remote_http_transport_auth_and_execution_without_network(tmp_path, monkeypatch):
    import httpx2

    import stacy.mcp_gateway as module

    async def scenario():
        upstream = FastMCP("http fixture")

        @upstream.tool
        def find_events(day: str) -> dict:
            return {"day": day, "transport": "http"}

        app = upstream.http_app(path="/mcp")
        seen_headers = []

        async def capture(scope, receive, send):
            seen_headers.append(dict(scope.get("headers", [])))
            await app(scope, receive, send)

        # Exercise FastMCP's real HTTP transport with an in-process ASGI server,
        # so this test needs neither an external API nor permission to bind ports.
        original_client = module._http_client

        def local_client(**kwargs):
            client = original_client(
                **{
                    **kwargs,
                    "transport": httpx2.ASGITransport(app=capture),
                }
            )
            assert not client.follow_redirects
            return client

        monkeypatch.setattr(module, "_http_client", local_client)
        monkeypatch.setenv("HTTP_TEST_KEY", "private-test-token")
        config = tmp_path / "mcp.json"
        config.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "remote": {
                            "description": "Calendar events",
                            "url": "http://mcp.test/mcp",
                            "transport": "http",
                            "headers": {"x-api-key": "${HTTP_TEST_KEY}"},
                        }
                    }
                }
            )
        )
        host = FastMCP("host")
        MCPGateway(load_servers(config)).register(host)
        async with app.router.lifespan_context(app):
            async with Client(host) as client:
                discovery = await client.call_tool(
                    "search_mcp_tools",
                    {
                        "server": "remote",
                        "query": "events",
                    },
                )
                assert discovery.data["tools"][0]["name"] == "find_events"
                result = await client.call_tool(
                    "call_mcp_tool",
                    {
                        "server": "remote",
                        "tool": "find_events",
                        "arguments": {"day": "today"},
                    },
                )
                assert result.data == {"day": "today", "transport": "http"}
                assert "private-test-token" not in str(discovery)
        assert seen_headers
        assert all(headers[b"x-api-key"] == b"private-test-token" for headers in seen_headers)

    asyncio.run(scenario())


def test_unreachable_server_is_an_error_not_an_empty_catalog(tmp_path):
    async def scenario():
        host = FastMCP("host")
        gateway = MCPGateway(
            {
                "broken": {
                    "command": str(tmp_path / "nonexistent-command"),
                    "description": "Broken calendar provider",
                }
            }
        )
        gateway.register(host)
        async with Client(host) as client:
            failed = await client.call_tool(
                "search_mcp_tools",
                {
                    "server": "broken",
                    "query": "events",
                },
                raise_on_error=False,
            )
            assert failed.is_error and failed.content[0].text.startswith("Error")
            # Local directory discovery still works after a connection failure.
            assert (await client.call_tool("search_mcp_servers", {})).data["configured_count"] == 1

    asyncio.run(scenario())


def test_exact_tool_discovery_returns_one_full_schema_and_caches_success(monkeypatch):
    from types import SimpleNamespace

    async def scenario():
        gateway = MCPGateway({"calendar": {"description": "Calendar"}})
        calls = []
        schema = {"type": "object", "required": ["day"], "properties": {"day": {"type": "string"}}}

        async def request(*args):
            calls.append(args)
            return SimpleNamespace(
                is_error=False,
                data=[
                    {"name": "list_other", "inputSchema": {}},
                    {
                        "name": "list_events",
                        "description": "Events",
                        "inputSchema": schema,
                        "outputSchema": {"large": "irrelevant"},
                    },
                ],
            )

        monkeypatch.setattr(gateway, "_request", request)
        first = await gateway.search_tools("calendar", "list_events")
        second = await gateway.search_tools("calendar", "LIST_EVENTS")
        assert first.structured_content == second.structured_content
        assert first.structured_content["tools"] == [
            {"name": "list_events", "description": "Events", "inputSchema": schema}
        ]
        assert len(calls) == 1

    asyncio.run(scenario())
