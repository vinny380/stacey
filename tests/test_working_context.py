import asyncio
import json
from types import SimpleNamespace

import pytest
from fastmcp import FastMCP

from stacy.agent import AgentMessage, TextAgent
from stacy.config import Settings
from stacy.working_context import WorkingContext


def test_follow_up_reuses_full_schema_and_target_after_restart_without_replaying_tools(tmp_path):
    async def scenario():
        tools = FastMCP("test")
        calls = []
        schema = {
            "type": "object",
            "properties": {"device_id": {"type": "string"}},
            "required": ["device_id"],
        }

        @tools.tool
        def discover():
            calls.append("discover")
            return {
                "input_schema": schema,
                "session_id": "session-1",
                "device_id": "echo-1",
                "access_token": "provider-secret",
            }

        @tools.tool
        def pause(device_id: str):
            calls.append(("pause", device_id))
            return {"is_playing": False}

        @tools.tool
        def verify(device_id: str):
            calls.append(("verify", device_id))
            return {"is_playing": False, "device_id": device_id}

        class Item:
            def __init__(self, name):
                self.type, self.name = "function_call", name
                self.call_id = name
                self.arguments = json.dumps({} if name == "discover" else {"device_id": "echo-1"})

            def model_dump(self, **kwargs):
                return vars(self)

        class Model:
            def __init__(self, sequence):
                self.responses = self
                self.sequence = iter(sequence)
                self.requests = []

            async def create(self, **kwargs):
                self.requests.append(json.loads(json.dumps(kwargs)))
                name = next(self.sequence)
                return SimpleNamespace(output=[Item(name)] if name else [], output_text="done")

            async def close(self):
                pass

        settings = Settings(tmp_path, tmp_path / "memory")
        first = TextAgent(settings, tools, model_client=Model(["discover", None]))
        await first.respond(AgentMessage("imessage", "dm", "owner", "1", "find my device"))
        await first.close()
        model = Model(["pause", "verify", None])
        second = TextAgent(settings, tools, model_client=model)
        await second.respond(AgentMessage("imessage", "dm", "owner", "2", "stop it"))
        input_items = model.requests[0]["input"]
        retained = next(
            x["content"] for x in input_items if "previous_tool_context" in x.get("content", "")
        )
        assert "echo-1" in retained and "session-1" in retained
        assert "provider-secret" not in retained
        assert '"required": ["device_id"]' in retained
        assert calls == ["discover", ("pause", "echo-1"), ("verify", "echo-1")]
        assert second.working_context.message('["imessage", "other", "owner"]') is None
        assert second.working_context.message('["imessage", "dm", "stranger"]') is None
        raw = second.db.execute("SELECT payload FROM working_context").fetchall()
        assert all("provider-secret" not in row[0] for row in raw)
        await second.close()

    asyncio.run(scenario())


def test_context_expires_prunes_whole_exchanges_and_excludes_auth_private_data(tmp_path):
    import sqlite3

    db = sqlite3.connect(tmp_path / "test.sqlite3")
    db.execute("CREATE TABLE turns(thread TEXT,id TEXT,status TEXT)")
    now = [1000.0]
    context = WorkingContext(db, clock=lambda: now[0], max_characters=1200, max_turns=2)
    assert context.capture("read_memory", {}, {"content": "private memory"}) is None
    assert context.capture("call_mcp_tool", {"tool": "COMPOSIO_MANAGE_CONNECTIONS"}, {}) is None
    assert context.capture("tool", {}, {"huge": "x" * 2000}) is None
    for i in range(3):
        db.execute("INSERT INTO turns VALUES ('thread',?,'complete')", (str(i),))
        record = context.capture("tool", {"device_id": str(i)}, {"session": {"id": "known"}})
        context.save("thread", str(i), [record])
        now[0] += 1
    assert len(context.load("thread")) == 2
    assert "known" in context.message("thread")["content"]
    now[0] += 901
    assert context.message("thread") is None
    context.save("other", "4", [])
    assert db.execute("SELECT count(*) FROM working_context").fetchone()[0] == 0
    db.close()


def test_uncertain_turn_discards_working_state_and_never_retries(tmp_path):
    async def scenario():
        class Broken:
            responses = None

            def __init__(self):
                self.responses = self
                self.calls = 0

            async def create(self, **kwargs):
                self.calls += 1
                raise RuntimeError("failed")

            async def close(self):
                pass

        model = Broken()
        agent = TextAgent(
            Settings(tmp_path, tmp_path / "memory"), FastMCP("tools"), model_client=model
        )
        old = AgentMessage("imessage", "dm", "owner", "old", "old request")
        agent.db.execute(
            "INSERT INTO turns VALUES (?,?,?,'done','complete')",
            (old.thread, old.message_id, old.text),
        )
        agent.working_context.save(old.thread, "old", [{"tool": "old"}])
        agent.db.commit()
        message = AgentMessage("imessage", "dm", "owner", "new", "new request")
        with pytest.raises(RuntimeError, match="failed"):
            await agent.respond(message)
        assert agent.working_context.message(message.thread) is None
        with pytest.raises(RuntimeError, match="Earlier attempt"):
            await agent.respond(message)
        assert model.calls == 1
        await agent.close()

    asyncio.run(scenario())
