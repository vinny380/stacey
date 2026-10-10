import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from fastmcp import FastMCP

from stacy.agent import AgentMessage, TextAgent
from stacy.config import Settings
from stacy.messaging import register_messaging


class Output:
    def __init__(self, **values):
        self.__dict__.update(values)

    def model_dump(self, **kwargs):
        return self.__dict__


class Model:
    def __init__(self):
        self.requests = []
        self.responses = self

    async def create(self, **kwargs):
        self.requests.append(kwargs)
        if len(self.requests) == 1:
            return SimpleNamespace(
                output=[
                    Output(
                        type="function_call",
                        name="set_timer",
                        arguments='{"seconds": 60}',
                        call_id="call-1",
                    )
                ],
                output_text="",
            )
        return SimpleNamespace(
            output=[Output(type="message", role="assistant", content=[])], output_text="Timer set."
        )

    async def close(self):
        pass


def test_shared_tool_execution_history_dedup_and_restart(tmp_path):
    async def scenario():
        tools = FastMCP("test")
        calls = []

        @tools.tool
        def set_timer(seconds: int):
            calls.append(seconds)
            return {"saved": True}

        settings = Settings(tmp_path, tmp_path / "memory")
        model = Model()
        agent = TextAgent(settings, tools, model_client=model)
        first = AgentMessage("imessage", "dm", "owner", "1", "Set a timer")
        results = await asyncio.gather(agent.respond(first), agent.respond(first))
        assert results == ["Timer set.", "Timer set."]
        assert calls == [60]
        second_request = model.requests[1]
        output = next(i for i in second_request["input"] if i.get("type") == "function_call_output")
        assert json.loads(output["output"])["is_error"] is False
        assert "messaging conversation" in second_request["instructions"]
        await agent.close()
        restored = TextAgent(settings, tools, model_client=Model())
        assert await restored.respond(first) == "Timer set."
        assert restored.model_client.requests == []
        # Further turns see the existing chat, but another chat does not.
        restored.model_client.requests.append({})  # make fake model return text directly
        await restored.respond(AgentMessage("imessage", "dm", "owner", "2", "thanks"))
        assert any(
            i.get("content") == "Set a timer" for i in restored.model_client.requests[-1]["input"]
        )
        await restored.respond(AgentMessage("imessage", "other", "owner", "3", "hello"))
        assert not any(
            i.get("content") == "Set a timer" for i in restored.model_client.requests[-1]["input"]
        )
        await restored.close()

    asyncio.run(scenario())


def test_failed_turn_is_not_reexecuted(tmp_path):
    class Broken(Model):
        async def create(self, **kwargs):
            self.requests.append(kwargs)
            raise RuntimeError("offline")

    async def scenario():
        model = Broken()
        agent = TextAgent(
            Settings(tmp_path, tmp_path / "memory"), FastMCP("test"), model_client=model
        )
        incoming = AgentMessage("imessage", "dm", "owner", "1", "play music")
        with pytest.raises(RuntimeError, match="offline"):
            await agent.respond(incoming)
        with pytest.raises(RuntimeError, match="Earlier attempt"):
            await agent.respond(incoming)
        assert len(model.requests) == 1
        await agent.close()

    asyncio.run(scenario())


def test_bridge_authentication_and_validation():
    class FakeAgent:
        def __init__(self):
            self.received = []

        async def respond(self, message):
            self.received.append(message)
            return "done"

    async def scenario():
        tools = FastMCP("test")
        agent = FakeAgent()
        register_messaging(tools, agent, "x" * 32)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=tools.http_app()), base_url="http://test"
        ) as client:
            payload = dict(
                channel="imessage",
                conversation_id="dm",
                sender_id="owner",
                message_id="1",
                text="hi",
            )
            assert (await client.post("/agent/message", json=payload)).status_code == 401
            headers = {"authorization": "Bearer " + "x" * 32}
            assert (
                await client.post("/agent/message", json={}, headers=headers)
            ).status_code == 400
            result = await client.post("/agent/message", json=payload, headers=headers)
            assert result.json() == {"text": "done"}
            assert len(agent.received) == 1

    asyncio.run(scenario())


def test_http_turn_uses_real_alarm_service_while_server_is_running(tmp_path, monkeypatch):
    from stacy.alarms import AlarmService
    from stacy.memory import MemoryRepo
    from stacy.tools import create_tools

    for credential in ("EXA_API_KEY", "E2B_API_KEY"):
        monkeypatch.delenv(credential, raising=False)

    async def noop(value):
        pass

    async def scenario():
        settings = Settings(tmp_path, tmp_path / "memory")
        alarms = AlarmService(tmp_path / "state.sqlite3", settings.timezone, noop, noop)
        await alarms.start()
        tools = create_tools(alarms, MemoryRepo(settings.memory_dir))
        agent = TextAgent(settings, tools, model_client=Model())
        register_messaging(tools, agent, "x" * 32)
        app = tools.http_app()
        try:
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://test"
                ) as client:
                    payload = dict(
                        channel="imessage",
                        conversation_id="dm",
                        sender_id="owner",
                        message_id="one",
                        text="Set a one minute timer",
                    )
                    headers = {"authorization": "Bearer " + "x" * 32}
                    assert (
                        await client.post("/agent/message", json=payload, headers=headers)
                    ).json() == {"text": "Timer set."}
                    assert len(alarms.list()) == 1
                    assert (
                        await client.post("/agent/message", json=payload, headers=headers)
                    ).status_code == 200
                    assert len(alarms.list()) == 1
        finally:
            await agent.close()
            alarms.close()
            await asyncio.sleep(0)

    asyncio.run(scenario())


def test_linked_profile_is_loaded_before_model_answers_identity_question(tmp_path):
    async def scenario():
        tools = FastMCP("memory")
        reads = []

        @tools.tool
        def read_memory(path: str = "MEMORY.md"):
            reads.append(path)
            return {
                "content": {
                    "MEMORY.md": "# Memory\n## Index\n[[profile]]\n",
                    "profile.md": "- Name is Vinny.\n",
                }[path]
            }

        class IdentityModel(Model):
            async def create(self, **kwargs):
                assert reads == ["MEMORY.md", "profile.md"]
                assert "Name is Vinny" in kwargs["instructions"]
                return SimpleNamespace(output=[], output_text="You're Vinny.")

        agent = TextAgent(
            Settings(tmp_path, tmp_path / "memory"), tools, model_client=IdentityModel()
        )
        try:
            assert (
                await agent.respond(
                    AgentMessage("imessage", "dm", "owner", "identity", "Who am I?")
                )
                == "You're Vinny."
            )
        finally:
            await agent.close()

    asyncio.run(scenario())
