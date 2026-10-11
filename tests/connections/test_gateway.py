import asyncio
import json
import sys
from contextlib import asynccontextmanager

import pytest
from fastmcp import Client, FastMCP
from fastmcp.client.transports import FastMCPTransport
from fastmcp.server.middleware import Middleware

from stacy.connections.gateway import MCPGateway, load_servers


class CountCatalog(Middleware):
    def __init__(self):
        self.requests = 0

    async def on_list_tools(self, context, call_next):
        self.requests += 1
        return await call_next(context)


def test_workflow_session_reuses_one_connection_and_closes_it_without_context_leakage():
    async def scenario():
        counts = {"opened": 0, "closed": 0}

        class CountTransport(FastMCPTransport):
            @asynccontextmanager
            async def connect_session(self, **kwargs):
                counts["opened"] += 1
                try:
                    async with super().connect_session(**kwargs) as session:
                        yield session
                finally:
                    counts["closed"] += 1

        upstream = FastMCP("upstream")

        @upstream.tool
        def read_value(value: str):
            return {"value": value}

        gateway = MCPGateway(
            {"remote": {"description": "test"}}, targets={"remote": CountTransport(upstream)}
        )
        async with gateway.session("remote"):
            first = await gateway.call_tool("remote", "read_value", {"value": "first"})
            second = await gateway.call_tool("remote", "read_value", {"value": "second"})
            assert first.structured_content["value"] == "first"
            assert second.structured_content["value"] == "second"
            assert counts == {"opened": 1, "closed": 0}
        assert counts == {"opened": 1, "closed": 1}

        async def isolated(value):
            async with gateway.session("remote"):
                await asyncio.sleep(0)
                result = await gateway.call_tool("remote", "read_value", {"value": value})
                return result.structured_content

        results = await asyncio.gather(isolated("alpha"), isolated("beta"))
        assert [r["value"] for r in results] == ["alpha", "beta"]
        assert counts == {"opened": 3, "closed": 3}
        with pytest.raises(RuntimeError):
            async with gateway.session("remote"):
                raise RuntimeError("cancel workflow")
        assert counts == {"opened": 4, "closed": 4}
        await gateway.call_tool("remote", "read_value", {"value": "fresh"})
        assert counts["opened"] > 4
        assert counts["closed"] == counts["opened"]

        ready = asyncio.Event()

        async def cancelled():
            async with gateway.session("remote"):
                ready.set()
                await asyncio.Event().wait()

        task = asyncio.create_task(cancelled())
        await ready.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert counts["closed"] == counts["opened"]
        before = counts["opened"]
        async with gateway.session("remote"):
            # ContextVars are inherited by children; each child workflow must still
            # own a fresh client rather than borrowing its parent's session.
            results = await asyncio.gather(isolated("child-one"), isolated("child-two"))
            assert [r["value"] for r in results] == ["child-one", "child-two"]
            assert counts["opened"] == before + 3
        assert counts["closed"] == counts["opened"]

    asyncio.run(scenario())


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

    import stacy.connections.gateway as module

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
    async def scenario():
        upstream = FastMCP("calendar")

        @upstream.tool
        def list_events(day: str) -> dict:
            """Events."""
            return {}

        @upstream.tool
        def list_other() -> dict:
            return {}

        gateway = MCPGateway(
            {"calendar": {"description": "Calendar"}}, targets={"calendar": upstream}
        )
        first = await gateway.search_tools("calendar", "list_events")

        async def unexpected_request(*args):
            pytest.fail("An exact repeated search should use the cache")

        monkeypatch.setattr(gateway, "_request", unexpected_request)
        second = await gateway.search_tools("calendar", "LIST_EVENTS")
        assert first.structured_content == second.structured_content
        selected = first.structured_content["tools"]
        assert [tool["name"] for tool in selected] == ["list_events"]
        assert selected[0]["inputSchema"]["required"] == ["day"]
        assert "outputSchema" not in selected[0]

    asyncio.run(scenario())


def test_text_trace_follows_real_mcp_gateway_and_cache(tmp_path):
    from stacy.harness.tracing import TextTrace, active_text_trace

    async def scenario():
        upstream = FastMCP("upstream")

        @upstream.tool
        def SPOTIFY_PAUSE_PLAYBACK(device_id: str):
            return {"device_id": device_id, "access_token": "never-log-this"}

        host = FastMCP("host")
        gateway = MCPGateway(
            {"spotify": {"description": "Spotify playback"}}, targets={"spotify": upstream}
        )
        gateway.register(host)
        trace = TextTrace(tmp_path)
        with trace.activate():
            async with Client(host) as client:
                for _ in range(2):
                    await client.call_tool(
                        "search_mcp_tools", {"server": "spotify", "query": "SPOTIFY_PAUSE_PLAYBACK"}
                    )
                await client.call_tool(
                    "call_mcp_tool",
                    {
                        "server": "spotify",
                        "tool": "SPOTIFY_PAUSE_PLAYBACK",
                        "arguments": {"device_id": "echo"},
                    },
                )
        assert active_text_trace.get() is None
        raw = trace.path.read_text()
        events = [json.loads(line) for line in raw.splitlines()]
        assert [e["hit"] for e in events if e["event"] == "discovery_cache"] == [False, True]
        assert any(
            e["event"] == "mcp_remote_request" and e["tool"] == "SPOTIFY_PAUSE_PLAYBACK"
            for e in events
        )
        assert "never-log-this" not in raw
        assert "echo" in raw

    asyncio.run(scenario())
