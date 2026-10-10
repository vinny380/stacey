from types import SimpleNamespace

import pytest

from stacy.config import Settings
from stacy.voice import build_voice_pipeline, openai_key


def test_direct_key_required(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        openai_key()
    monkeypatch.setenv("OPENAI_API_KEY", "sk-or-offline-test")
    with pytest.raises(RuntimeError, match="direct OpenAI"):
        openai_key()
    monkeypatch.setenv("OPENAI_API_KEY", "direct-offline-test")
    assert openai_key() == "direct-offline-test"


@pytest.mark.parametrize("noise_reduction", ["near_field", "far_field"])
def test_realtime_pipeline_keeps_memory_tools_and_native_turn_detection(tmp_path, noise_reduction):
    pytest.importorskip("pipecat.services.openai.realtime.llm")
    from pipecat.adapters.schemas.function_schema import FunctionSchema
    from pipecat.adapters.schemas.tools_schema import ToolsSchema
    from pipecat.processors.frame_processor import FrameProcessor
    from pipecat.services.openai.realtime.llm import OpenAIRealtimeLLMService

    memory = tmp_path / "memory"
    memory.mkdir()
    (memory / "MEMORY.md").write_text("# Memory\nLikes jazz.\n")
    settings = Settings(tmp_path, memory, noise_reduction=noise_reduction)

    async def handler(params):
        await params.result_callback({"saved": True})

    tools = ToolsSchema(
        standard_tools=[
            FunctionSchema(
                name="set_timer",
                description="Set a timer.",
                properties={"seconds": {"type": "number"}},
                required=["seconds"],
                handler=handler,
            )
        ]
    )
    incoming, outgoing = FrameProcessor(), FrameProcessor()
    pipeline = build_voice_pipeline(
        settings,
        "offline-test",
        tools,
        SimpleNamespace(input=lambda: incoming, output=lambda: outgoing),
    )
    llms = [p for p in pipeline.processors if isinstance(p, OpenAIRealtimeLLMService)]
    assert len(llms) == 1
    llm = llms[0]
    assert llm.base_url == "wss://api.openai.com/v1/realtime?model=" + settings.realtime_model
    assert "Likes jazz" in llm.settings.system_instruction
    assert llm.settings.session_properties.audio.input.turn_detection.type == "semantic_vad"
    session = llm.settings.session_properties.model_dump(exclude_none=True)
    assert session["audio"]["input"]["noise_reduction"] == {"type": noise_reduction}
    assert llm.settings.session_properties.audio.output.voice == "marin"
    assert llm._functions["set_timer"].cancel_on_interruption is True
    assert session["audio"]["input"]["transcription"]["model"] == "gpt-4o-mini-transcribe"


def test_invalid_microphone_noise_reduction_fails_early(monkeypatch):
    monkeypatch.setenv("STACY_NOISE_REDUCTION", "typo")
    with pytest.raises(ValueError, match="STACY_NOISE_REDUCTION"):
        Settings.load()


def test_production_worker_sends_initial_tool_context_before_audio(tmp_path, monkeypatch):
    import asyncio

    pytest.importorskip("pipecat.services.openai.realtime.llm")
    from pipecat.adapters.schemas.function_schema import FunctionSchema
    from pipecat.adapters.schemas.tools_schema import ToolsSchema
    from pipecat.processors.frame_processor import FrameProcessor
    from pipecat.services.openai.realtime.llm import OpenAIRealtimeLLMService
    from pipecat.workers.runner import WorkerRunner

    from stacy.voice import create_voice_worker

    class Passthrough(FrameProcessor):
        async def process_frame(self, frame, direction):
            await super().process_frame(frame, direction)
            await self.push_frame(frame, direction)

    async def scenario():
        async def handler(params):
            await params.result_callback({"saved": True})

        tools = ToolsSchema(
            standard_tools=[
                FunctionSchema(
                    name="set_timer",
                    description="Set a timer.",
                    properties={},
                    required=[],
                    handler=handler,
                )
            ]
        )
        incoming, outgoing = Passthrough(), Passthrough()
        pipeline = build_voice_pipeline(
            Settings(tmp_path, tmp_path / "memory"),
            "offline-test",
            tools,
            SimpleNamespace(input=lambda: incoming, output=lambda: outgoing),
            retrieved_notes={"profile.md": "Name is Test User. Practices jiu-jitsu."},
        )
        llm = next(p for p in pipeline.processors if isinstance(p, OpenAIRealtimeLLMService))
        sent = []
        configured = asyncio.Event()

        async def connect():
            llm._api_session_ready = True

        async def send(event):
            sent.append(event)
            if event["type"] == "session.update":
                configured.set()

        monkeypatch.setattr(llm, "_connect", connect)
        monkeypatch.setattr(llm, "_ws_send", send)
        worker = create_voice_worker(pipeline)
        runner = WorkerRunner(handle_sigint=False, handle_sigterm=False)
        await runner.add_workers(worker)
        running = asyncio.create_task(runner.run())
        try:
            # No user text or audio is injected. Startup itself must advertise tools.
            await asyncio.wait_for(configured.wait(), 3)
            update = next(event["session"] for event in sent if event["type"] == "session.update")
            assert any(tool["name"] == "set_timer" for tool in update["tools"])
            assert "You are Stacy" in update["instructions"]
            assert "Practices jiu-jitsu" in update["instructions"]
            await worker.stop_when_done()
            await asyncio.wait_for(running, 3)
        finally:
            if not running.done():
                await runner.cancel()
                await running

    asyncio.run(scenario())


def test_voice_bootstrap_loads_linked_profile_and_cross_channel_handoff():
    import asyncio

    from fastmcp import FastMCP

    from stacy.voice import startup_context

    async def scenario():
        server = FastMCP("memory fixture")
        notes = {
            "MEMORY.md": "# Memory\n[[profile]]\n[[music]]\n[[profile]]",
            "profile.md": "Name is Test User. Practices jiu-jitsu.",
            "music.md": "Likes jazz.",
        }
        reads = []

        @server.tool
        def read_memory(path: str) -> dict:
            reads.append(path)
            return {"content": notes[path]}

        @server.tool
        def recent_texts() -> dict:
            return {
                "messages": [
                    {"text": "Calendar sign-in: https://auth.example/link", "status": "sent"}
                ]
            }

        loaded, handoff = await startup_context(server)
        assert loaded == notes
        assert reads == ["MEMORY.md", "profile.md", "music.md"]
        assert handoff[0]["status"] == "sent"

    asyncio.run(scenario())
