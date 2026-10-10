import asyncio
import json
from types import SimpleNamespace

import httpx2
import pytest
from fastmcp import Client, FastMCP

from stacy.agent import AgentMessage, TextAgent
from stacy.alarms import AlarmService
from stacy.config import Settings
from stacy.connections import register_composio
from stacy.mcp_gateway import MCPGateway
from stacy.memory import MemoryRepo
from stacy.tools import create_tools


def test_consumer_key_type_error_does_not_expose_key():
    key = "ak_private-project-key"
    with pytest.raises(ValueError, match="consumer key") as error:
        register_composio(MCPGateway({}), key)
    assert key not in str(error.value)


def test_consumer_connection_is_lazy_and_keeps_credentials_out_of_discovery():
    async def scenario():
        host = FastMCP("host")
        gateway = MCPGateway({})
        gateway.register(host)
        register_composio(gateway, "ck_private-consumer-key")
        async with Client(host) as client:
            assert len(await client.list_tools()) == 3
            result = await client.call_tool("search_mcp_servers", {"query": "Google calendar"})
            assert result.data["servers"][0]["server"] == "composio"
            assert "ck_private" not in str(result)
            assert "headers" not in str(result)
            assert gateway._proxies == {}

    asyncio.run(scenario())


def test_text_link_and_completion_use_native_mcp_over_consumer_authenticated_http(
    tmp_path, monkeypatch
):
    import stacy.mcp_gateway as module

    monkeypatch.setenv("COMPOSIO_API_KEY", "ck_fixture-key")
    for key in ("EXA_API_KEY", "E2B_API_KEY"):
        monkeypatch.delenv(key, raising=False)

    class Output:
        def __init__(self, **values):
            self.__dict__.update(values)

        def model_dump(self, **kwargs):
            return self.__dict__

    class Model:
        def __init__(self):
            self.responses = self
            self.requests = []
            self.steps = iter(
                [
                    ("search_mcp_servers", {"query": "Google calendar"}),
                    ("search_mcp_tools", {"server": "composio", "query": "COMPOSIO_SEARCH_TOOLS"}),
                    (
                        "call_mcp_tool",
                        {
                            "server": "composio",
                            "tool": "COMPOSIO_SEARCH_TOOLS",
                            "arguments": {
                                "queries": [{"use_case": "Connect Google Calendar"}],
                                "session": {"generate_id": True},
                            },
                        },
                    ),
                    (
                        "search_mcp_tools",
                        {"server": "composio", "query": "COMPOSIO_MANAGE_CONNECTIONS"},
                    ),
                    (
                        "call_mcp_tool",
                        {
                            "server": "composio",
                            "tool": "COMPOSIO_MANAGE_CONNECTIONS",
                            "arguments": {
                                "toolkits": [{"name": "googlecalendar", "action": "list"}],
                                "session_id": "fixture-session",
                            },
                        },
                    ),
                    (
                        "call_mcp_tool",
                        {
                            "server": "composio",
                            "tool": "COMPOSIO_MANAGE_CONNECTIONS",
                            "arguments": {
                                "toolkits": [{"name": "googlecalendar", "action": "add"}],
                                "session_id": "fixture-session",
                            },
                        },
                    ),
                    "link",
                    (
                        "call_mcp_tool",
                        {
                            "server": "composio",
                            "tool": "COMPOSIO_SEARCH_TOOLS",
                            "arguments": {
                                "queries": [{"use_case": "Check Calendar connection"}],
                                "session": {"generate_id": True},
                            },
                        },
                    ),
                    (
                        "call_mcp_tool",
                        {
                            "server": "composio",
                            "tool": "COMPOSIO_MANAGE_CONNECTIONS",
                            "arguments": {
                                "toolkits": [{"name": "googlecalendar", "action": "list"}],
                                "session_id": "fixture-session",
                            },
                        },
                    ),
                    "status",
                ]
            )

        async def create(self, **kwargs):
            self.requests.append(kwargs)
            step = next(self.steps)
            assert "ck_fixture-key" not in json.dumps(kwargs)
            assert "action='list'" in kwargs["instructions"]
            if isinstance(step, tuple):
                name, arguments = step
                return SimpleNamespace(
                    output_text="",
                    output=[
                        Output(
                            type="function_call",
                            name=name,
                            arguments=json.dumps(arguments),
                            call_id=f"call-{len(self.requests)}",
                        )
                    ],
                )
            # Use the actual MCP result passed back by TextAgent, not a hardcoded reply.
            result = next(
                item
                for item in reversed(kwargs["input"])
                if item.get("type") == "function_call_output"
            )
            result = json.loads(result["output"])
            assert not result["is_error"]
            data = result["structured_content"]
            if step == "link":
                reply = f"[Connect Calendar]({data['redirect_url']}). Reply done after signing in."
            else:
                assert data["accounts"][0]["status"] == "ACTIVE"
                reply = "Your calendar is connected."
            return SimpleNamespace(
                output_text=reply,
                output=[
                    Output(
                        type="message",
                        role="assistant",
                        content=[],
                    )
                ],
            )

        async def close(self):
            pass

    async def scenario():
        upstream = FastMCP("consumer fixture")
        calls, seen_headers = [], []
        active = False

        @upstream.tool
        def COMPOSIO_SEARCH_TOOLS(queries: list[dict], session: dict) -> dict:
            assert queries and session == {"generate_id": True}
            calls.append("search")
            return {"session_id": "fixture-session", "toolkit": "googlecalendar"}

        @upstream.tool
        def COMPOSIO_MANAGE_CONNECTIONS(toolkits: list[dict], session_id: str) -> dict:
            assert session_id == "fixture-session"
            assert toolkits[0]["name"] == "googlecalendar"
            action = toolkits[0]["action"]
            calls.append(action)
            if action == "add":
                return {
                    "redirect_url": "https://auth.example/calendar/fixture",
                    "status": "PENDING",
                }
            assert action == "list"
            return {
                "accounts": [{"id": "personal-calendar", "alias": "personal", "status": "ACTIVE"}]
                if active
                else []
            }

        app = upstream.http_app(path="/mcp")

        async def capture(scope, receive, send):
            seen_headers.append(dict(scope.get("headers", [])))
            await app(scope, receive, send)

        original_client = module._http_client

        def local_client(**kwargs):
            return original_client(**{**kwargs, "transport": httpx2.ASGITransport(app=capture)})

        monkeypatch.setattr(module, "_http_client", local_client)

        async def noop(value):
            pass

        settings = Settings(tmp_path, tmp_path / "memory")
        alarms = AlarmService(tmp_path / "alarms.sqlite3", settings.timezone, noop, noop)
        host = create_tools(alarms, MemoryRepo(settings.memory_dir))
        model = Model()
        agent = TextAgent(settings, host, model_client=model)
        first = AgentMessage("imessage", "dm", "owner", "1", "Connect my Google Calendar")
        try:
            async with app.router.lifespan_context(app):
                reply = await agent.respond(first)
                assert "https://auth.example/calendar/fixture" in reply
                assert "Reply done" in reply
                assert await agent.respond(first) == reply  # redelivery never creates another link
                active = True  # provider status changes after phone sign-in
                assert (
                    await agent.respond(AgentMessage("imessage", "dm", "owner", "2", "done"))
                    == "Your calendar is connected."
                )
            assert calls == ["search", "list", "add", "search", "list"]
            assert seen_headers
            assert all(h[b"x-consumer-api-key"] == b"ck_fixture-key" for h in seen_headers)
            assert all(b"x-api-key" not in h for h in seen_headers)
            assert any(
                item.get("content") == "Connect my Google Calendar"
                for item in model.requests[-1]["input"]
            )
        finally:
            await agent.close()
            alarms.close()
            await asyncio.sleep(0)

    asyncio.run(scenario())
