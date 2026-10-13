import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from fastmcp import FastMCP

from stacy.harness.text import AgentMessage, TextAgent
from stacy.integrations.messaging.bridge import register_messaging
from stacy.runtime.settings import Settings


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
                id="fixture",
                usage=None,
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
            id="fixture",
            usage=None,
            output=[Output(type="message", role="assistant", content=[])],
            output_text="Timer set.",
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


@pytest.mark.parametrize("cache_prompt", [False, True])
@pytest.mark.parametrize("final_tool_call", [False, True])
def test_tool_limit_finishes_without_repeating_work(tmp_path, cache_prompt, final_tool_call):
    class ExhaustiveSearch(Model):
        async def create(self, **kwargs):
            self.requests.append({**kwargs, "input": list(kwargs["input"])})
            if kwargs.get("tool_choice") == "none" and not final_tool_call:
                return SimpleNamespace(
                    id="summary",
                    usage=None,
                    output=[Output(type="message", role="assistant", content=[])],
                    output_text=(
                        "Checked 30 pages. Reply to the review request. Coverage is incomplete."
                    ),
                )
            page = len(self.requests)
            return SimpleNamespace(
                id=f"page-{page}",
                usage=None,
                output=[
                    Output(
                        type="function_call",
                        name="read_page",
                        arguments=json.dumps({"page": page}),
                        call_id=f"call-{page}",
                    )
                ],
                output_text="",
            )

    async def scenario():
        tools = FastMCP("test")
        pages = []

        @tools.tool
        def read_page(page: int):
            pages.append(page)
            return {"page": page, "needs_reply": "Review request", "has_more": True}

        model = ExhaustiveSearch()
        agent = TextAgent(
            Settings(tmp_path, tmp_path / "memory"),
            tools,
            model_client=model,
            cache_prompt=cache_prompt,
        )
        message = AgentMessage("imessage", "dm", "owner", "brief", "Summarize pending replies")
        try:
            if final_tool_call:
                with pytest.raises(RuntimeError, match="tools during final summarization"):
                    await agent.respond(message)
                with pytest.raises(RuntimeError, match="Earlier attempt"):
                    await agent.respond(message)
            else:
                reply = await agent.respond(message)
                assert "review request" in reply.lower()
                assert "incomplete" in reply
                assert await agent.respond(message) == reply
            assert pages == list(range(1, 31))
            final = model.requests[-1]
            assert final["tool_choice"] == "none"
            assert len([i for i in final["input"] if i.get("type") == "function_call_output"]) == 30
            assert final["input"][-1]["role"] == "developer"
            assert "coverage" in final["input"][-1]["content"].lower()
            assert len(model.requests) == 31
        finally:
            await agent.close()

    asyncio.run(scenario())


def test_cacheable_prompt_keeps_dynamic_time_memory_and_source_after_breakpoint(tmp_path):
    from stacy.harness.context import ConversationContext
    from stacy.harness.instructions import instructions

    settings = Settings(tmp_path, tmp_path / "memory")
    prompt = instructions(
        settings,
        channel="imessage",
        memory_source="imessage message private-id",
        context=ConversationContext(notes={"profile.md": "Name is Vinny"}),
        cacheable=True,
    )
    prefix, marker, context = prompt.partition("\n<current_turn_context>\n")
    assert marker
    assert "Today is" not in prefix
    assert "private-id" not in prefix and "Name is Vinny" not in prefix
    assert "Today is" in context and "private-id" in context and "Name is Vinny" in context

    async def scenario():
        tools = FastMCP("test")

        @tools.tool
        def set_timer(seconds: int):
            return {"saved": True}

        model = Model()
        agent = TextAgent(settings, tools, model_client=model, cache_prompt=True)
        await agent.respond(AgentMessage("imessage", "dm", "owner", "1", "Set a timer"))
        first, second = model.requests
        assert first["instructions"] is None and second["instructions"] is None
        assert first["input"][0] == second["input"][0]
        assert first["input"][0]["role"] == "developer"
        block = first["input"][0]["content"][0]
        assert block["prompt_cache_breakpoint"] == {"mode": "explicit"}
        assert "Today is" not in block["text"]
        assert "Today is" in first["input"][1]["content"]
        assert first["input"][2]["content"] == "Set a timer"
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
    from stacy.alarms.service import AlarmService
    from stacy.memory.repository import MemoryRepo
    from stacy.runtime.tools import create_tools

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
                return SimpleNamespace(
                    id="fixture", usage=None, output=[], output_text="You're Vinny."
                )

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


def test_text_traces_capture_model_tools_usage_and_hide_payloads(tmp_path):
    async def scenario():
        tools = FastMCP("test")

        @tools.tool
        def set_timer(seconds: int):
            return {"saved": True, "secret": "provider-secret"}

        class UsageModel(Model):
            async def create(self, **kwargs):
                response = await super().create(**kwargs)
                response.usage = SimpleNamespace(
                    input_tokens=10,
                    output_tokens=5,
                    total_tokens=15,
                    output_tokens_details=SimpleNamespace(reasoning_tokens=3),
                    input_tokens_details=SimpleNamespace(cached_tokens=2),
                )
                return response

        agent = TextAgent(Settings(tmp_path, tmp_path / "memory"), tools, model_client=UsageModel())
        try:
            await agent.respond(
                AgentMessage(
                    "imessage", "private-dm", "private-owner", "private-id", "private-message"
                )
            )
        finally:
            await agent.close()
        (path,) = (tmp_path / "traces").glob("text-*.jsonl")
        raw = path.read_text()
        events = [json.loads(line) for line in raw.splitlines()]
        assert path.stat().st_mode & 0o777 == 0o600
        assert [e["event"] for e in events].count("model_request") == 2
        assert any(e["event"] == "tool_request" and e["tool"] == "set_timer" for e in events)
        assert any(e["event"] == "model_usage" and e["reasoning_tokens"] == 3 for e in events)
        assert events[-1]["event"] == "turn" and events[-1]["status"] == "complete"
        assert all(e["seconds"] >= 0 for e in events if "seconds" in e)
        for secret in (
            "private-dm",
            "private-owner",
            "private-id",
            "private-message",
            "provider-secret",
            "Timer set.",
        ):
            assert secret not in raw

    asyncio.run(scenario())
