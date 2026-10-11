import asyncio
import json

from fastmcp import Client, FastMCP

from stacy.mcp_gateway import MCPGateway
from stacy.tool_index import build_tool_index, load_tool_index


def test_index_is_local_bounded_redacted_and_tracks_connection_changes(monkeypatch):
    monkeypatch.setenv("EXAMPLE_API_KEY", "private-example-value")

    async def scenario():
        upstream = FastMCP("unused")

        @upstream.tool
        def unexpected():
            raise AssertionError("Index must not execute upstream tools")

        gateway = MCPGateway(
            {"mail": {"description": "Mail private-example-value https://auth.example/secret"}},
            targets={"mail": upstream},
        )
        host = FastMCP("host")
        gateway.register(host)
        async with Client(host) as client:
            index = await load_tool_index(client, await client.list_tools())
            assert index["servers"][0]["server"] == "mail"
            assert "private-example-value" not in json.dumps(index)
            assert "https://auth.example" not in json.dumps(index)
            assert not gateway._proxies  # no upstream initialization/catalog fetch
            gateway.replace_server("calendar", {"description": "Calendar"}, target=upstream)
            gateway.remove_server("mail")
            refreshed = await load_tool_index(client, await client.list_tools())
            assert [s["server"] for s in refreshed["servers"]] == ["calendar"]
            assert not gateway._proxies

    asyncio.run(scenario())


def test_background_preparation_does_not_block_local_tools_and_is_cancelled(tmp_path, monkeypatch):
    from stacy.alarms import AlarmService
    from stacy.memory import MemoryRepo
    from stacy.tools import create_tools

    for name in ("COMPOSIO_API_KEY", "EXA_API_KEY", "E2B_API_KEY"):
        monkeypatch.delenv(name, raising=False)

    async def scenario():
        preparing, closed = asyncio.Event(), asyncio.Event()

        class FakeWorkflow:
            def __init__(self, *args, **kwargs):
                pass

            def register(self, host):
                pass

            async def prepare(self):
                preparing.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    closed.set()

        monkeypatch.setattr("stacy.spotify_workflow.SpotifyWorkflow", FakeWorkflow)

        async def noop(value):
            pass

        config = tmp_path / "mcp.json"
        config.write_text(
            json.dumps({"mcpServers": {"composio": {"url": "https://example.org/mcp"}}})
        )
        alarms = AlarmService(tmp_path / "state.sqlite3", "America/Toronto", noop, noop)
        host = create_tools(
            alarms, MemoryRepo(tmp_path / "memory"), mcp_config=config, prewarm_spotify=True
        )
        try:
            async with Client(host) as client:
                await asyncio.wait_for(preparing.wait(), 1)
                timer = await asyncio.wait_for(client.call_tool("set_timer", {"seconds": 60}), 1)
                assert timer.data["label"] == "Timer"
                assert not closed.is_set()
            await asyncio.wait_for(closed.wait(), 1)
        finally:
            alarms.close()

    asyncio.run(scenario())
    huge = build_tool_index(
        [], [{"server": str(i), "description": "x" * 10000} for i in range(500)]
    )
    assert len(json.dumps(huge)) <= 6500
    assert huge["omitted_servers"] > 0


def test_agent_context_has_actual_direct_tools_and_server_aliases(tmp_path):
    from types import SimpleNamespace

    from stacy.agent import AgentMessage, TextAgent
    from stacy.config import Settings

    async def scenario():
        host = FastMCP("host")
        MCPGateway({"calendar": {"description": "Work calendars"}}).register(host)

        @host.tool
        def set_timer(seconds: int):
            return {"saved": True}

        class Model:
            def __init__(self):
                self.requests = []
                self.responses = self

            async def create(self, **kwargs):
                self.requests.append(kwargs)
                return SimpleNamespace(output=[], output_text="Ready.")

            async def close(self):
                pass

        model = Model()
        agent = TextAgent(Settings(tmp_path, tmp_path / "memory"), host, model_client=model)
        await agent.respond(AgentMessage("imessage", "dm", "owner", "1", "Set a timer"))
        prompt = model.requests[0]["instructions"]
        assert "<capability_index>" in prompt
        assert "Work calendars" in prompt and '"server": "calendar"' in prompt
        assert '"tool": "set_timer"' in prompt
        await agent.close()

    asyncio.run(scenario())
