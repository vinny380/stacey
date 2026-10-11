from __future__ import annotations

import os
import sys
from contextlib import AsyncExitStack

from stacy.agent_instructions import instructions as instructions
from stacy.config import Settings


def openai_key() -> str:
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not key:
        raise RuntimeError("Set OPENAI_API_KEY in .env before starting live voice")
    if key.startswith("sk-or-"):
        raise RuntimeError("Live voice requires a direct OpenAI API key, not an OpenRouter key")
    return key


def create_realtime_service(
    settings: Settings,
    key: str,
    *,
    memory_source="current conversation",
    retrieved_notes=None,
    recent_texts=None,
    tool_index=None,
):
    from typing import Literal

    from pipecat.services.openai.realtime.events import (
        AudioConfiguration,
        AudioInput,
        AudioOutput,
        InputAudioNoiseReduction,
        InputAudioTranscription,
        SemanticTurnDetection,
        SessionProperties,
        SessionUpdateEvent,
    )
    from pipecat.services.openai.realtime.llm import OpenAIRealtimeLLMService
    from pydantic import SerializeAsAny

    class ContextSessionProperties(SessionProperties):
        # Pipecat 1.12 omits this API field. Keep it typed so model_dump
        # actually sends it; the default BaseModel ignores unknown extras.
        truncation: Literal["disabled"] = "disabled"

    class ContextSessionUpdateEvent(SessionUpdateEvent):
        # Pydantic otherwise serializes a subclass as the base SessionProperties
        # type and silently strips truncation inside the actual outgoing event.
        session: SerializeAsAny[SessionProperties]

    class ContextRealtimeService(OpenAIRealtimeLLMService):
        async def send_client_event(self, event):
            if isinstance(event, SessionUpdateEvent):
                event = ContextSessionUpdateEvent(event_id=event.event_id, session=event.session)
            await super().send_client_event(event)

    return ContextRealtimeService(
        api_key=key,
        settings=OpenAIRealtimeLLMService.Settings(
            model=settings.realtime_model,
            system_instruction=instructions(
                settings,
                memory_source=memory_source,
                retrieved_notes=retrieved_notes,
                recent_texts=recent_texts,
                tool_index=tool_index,
            ),
            session_properties=ContextSessionProperties(
                max_output_tokens=4096,
                audio=AudioConfiguration(
                    input=AudioInput(
                        transcription=InputAudioTranscription(model="gpt-4o-mini-transcribe"),
                        turn_detection=SemanticTurnDetection(eagerness="auto"),
                        noise_reduction=InputAudioNoiseReduction(type=settings.noise_reduction),
                    ),
                    output=AudioOutput(voice=settings.voice),
                ),
            ),
        ),
    )


def build_voice_pipeline(
    settings: Settings,
    key: str,
    tools,
    transport,
    *,
    trace=None,
    retrieved_notes=None,
    recent_texts=None,
    tool_index=None,
):
    """Build the same voice/tool pipeline for local audio and headless checks."""
    from pipecat.adapters.schemas.function_schema import FunctionSchema
    from pipecat.adapters.schemas.tools_schema import ToolsSchema
    from pipecat.pipeline.pipeline import Pipeline

    from stacy.context_management import RealtimeContextBudget, VoiceContext, context_aggregators

    llm = create_realtime_service(
        settings,
        key,
        memory_source=trace.path.name if trace is not None else "current conversation",
        retrieved_notes=retrieved_notes,
        recent_texts=recent_texts,
        tool_index=tool_index,
    )
    if trace is not None:
        from stacy.tracing import attach_assistant_transcripts

        attach_assistant_transcripts(llm, trace)
    advertised = []
    for schema in tools.standard_tools:
        # Quick actions use Pipecat's normal request/result flow. Making every
        # tool non-blocking injects late-result messages and extra response turns.
        # An interruption cannot undo an alarm already committed by the server.
        llm.register_function(
            schema.name,
            schema.handler,
            cancel_on_interruption=schema.name not in {"web_search", "run_python"},
            timeout_secs=180,
            cancellable_by_llm=schema.name == "run_python",
        )
        advertised.append(
            FunctionSchema(
                name=schema.name,
                description=schema.description,
                properties=schema.properties,
                required=schema.required,
            )
        )
    context = VoiceContext(tools=ToolsSchema(standard_tools=advertised))
    # Let the Realtime service supply Pipecat's compatible turn strategies.
    aggregators = context_aggregators(
        context,
        llm,
        key,
        settings,
        trace=trace,
        echo_cancelled=getattr(transport, "echo_cancelled", False),
    )
    return Pipeline(
        [
            transport.input(),
            aggregators.user(),
            llm,
            transport.output(),
            # Keep the output -> upstream speaking notification path direct;
            # an extra queue there can let speaker echo precede fallback mute.
            RealtimeContextBudget(trace=trace),
            aggregators.assistant(),
        ]
    )


def create_voice_worker(pipeline, *, trace=None, enable_tracing=False, feedback=None):
    """Initialize Realtime's context/tools before the first microphone turn."""
    from pipecat.frames.frames import LLMRunFrame
    from pipecat.pipeline.worker import PipelineParams, PipelineWorker

    from stacy.tracing import trace_observers

    observers = trace_observers(trace) if trace is not None else []
    if feedback is not None:
        from stacy.feedback import WorkSoundObserver

        observers.append(WorkSoundObserver(feedback))
    worker = PipelineWorker(
        pipeline,
        name="stacy",
        idle_timeout_secs=None,
        params=PipelineParams(
            audio_in_sample_rate=24000,
            audio_out_sample_rate=24000,
            enable_metrics=True,
            enable_usage_metrics=True,
        ),
        observers=observers,
        enable_tracing=enable_tracing,
        enable_turn_tracking=True,
        conversation_id=trace.path.stem if trace is not None else None,
    )

    @worker.event_handler("on_pipeline_started")
    async def initialize_context(worker, frame):
        # In Realtime mode the user aggregator does not automatically push
        # initial context. Without this, audio-first sessions have no tools or
        # system instructions until after their first assistant response.
        await worker.queue_frame(LLMRunFrame())

    return worker


async def create_audio_transport(settings, *, feedback=None, trace=None):
    from loguru import logger
    from pipecat.transports.local.audio import LocalAudioTransport, LocalAudioTransportParams

    if settings.audio_mode == "speaker" and sys.platform == "darwin":
        from stacy.macos_audio import MacAudioTransport

        try:
            return await MacAudioTransport.open(settings.data_dir, mixer=feedback)
        except (OSError, RuntimeError, TimeoutError) as error:
            logger.warning(
                f"Native echo cancellation unavailable; using speaker turn taking: {error}"
            )
            if trace is not None:
                trace.record("audio_fallback", reason=str(error))
    return LocalAudioTransport(
        LocalAudioTransportParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            audio_out_mixer=feedback,
        )
    )


async def run_voice(settings: Settings, mcp_url: str) -> None:
    key = openai_key()
    from loguru import logger

    # Pipecat uses Loguru independently of the CLI's standard logging setup.
    # Avoid dumping prompts and personal memory at DEBUG during normal use.
    logger.remove()
    logger.add(sys.stderr, level=os.environ.get("STACY_LOG_LEVEL", "INFO").upper())
    from mcp.client.session_group import StreamableHttpParameters
    from pipecat.services.mcp_service import MCPClient
    from pipecat.workers.runner import WorkerRunner

    from stacy.tracing import VoiceTrace, local_telemetry

    async with MCPClient(StreamableHttpParameters(url=mcp_url, timeout=180)) as mcp:
        tools = await mcp.tools()
        notes, recent = await startup_context(mcp_url)
        from fastmcp import Client

        from stacy.tool_index import load_tool_index

        async with Client(mcp_url) as index_client:
            tool_index = await load_tool_index(index_client, await index_client.list_tools())
        trace = VoiceTrace(settings.data_dir)
        trace.record(
            "session_started",
            model=settings.realtime_model,
            audio_mode=settings.audio_mode,
            tools=[tool.name for tool in tools.standard_tools],
        )
        print(f"Conversation and tool trace: {trace.path}", file=sys.stderr)
        async with AsyncExitStack() as audio_resources:
            with local_telemetry(trace) as tracing_enabled:
                feedback = None
                if os.environ.get("STACY_WORK_SOUND", "typing").lower() not in {
                    "off",
                    "false",
                    "0",
                }:
                    from stacy.feedback import WorkSoundMixer
                    from stacy.sound import make_typing

                    feedback = WorkSoundMixer(make_typing(settings.data_dir / "typing.wav"))
                transport = await create_audio_transport(settings, feedback=feedback, trace=trace)
                if hasattr(transport, "close"):
                    audio_resources.push_async_callback(transport.close)
                echo_cancelled = getattr(transport, "echo_cancelled", False)
                trace.record(
                    "audio_transport_ready",
                    transport=type(transport).__name__,
                    echo_cancelled=echo_cancelled,
                    interruptions=echo_cancelled or settings.audio_mode == "headphones",
                )
                pipeline = build_voice_pipeline(
                    settings,
                    key,
                    tools,
                    transport,
                    trace=trace,
                    retrieved_notes=notes,
                    recent_texts=recent,
                    tool_index=tool_index,
                )
                worker = create_voice_worker(
                    pipeline, trace=trace, enable_tracing=tracing_enabled, feedback=feedback
                )

                @worker.event_handler("on_pipeline_started")
                async def audio_started(worker, frame):
                    mode = (
                        "Echo cancellation is active; you can interrupt me. "
                        if echo_cancelled
                        else (
                            "Speaker fallback listens between replies. "
                            if settings.audio_mode == "speaker"
                            else "Headphone mode allows interruptions. "
                        )
                    )
                    print(
                        f"Stacy's microphone/speaker pipeline is active. {mode}Ctrl+C to stop.",
                        file=sys.stderr,
                    )

                runner = WorkerRunner(handle_sigint=False, handle_sigterm=False)
                await runner.add_workers(worker)
                try:
                    await runner.run()
                finally:
                    trace.record("session_ended")


async def startup_context(mcp_url: str) -> tuple[dict[str, str], list[dict]]:
    """Load the same bounded notes as text before enabling the microphone."""
    from fastmcp import Client

    from stacy.memory_context import load_memory_context

    async with Client(mcp_url) as client:

        async def read(path: str) -> str:
            result = await client.call_tool("read_memory", {"path": path})
            if result.is_error or not isinstance(result.data, dict):
                raise RuntimeError("Voice startup could not load personal memory")
            return result.data["content"]

        notes = await load_memory_context(read)
        names = {tool.name for tool in await client.list_tools()}
        recent = (
            (await client.call_tool("recent_texts", {})).data["messages"]
            if "recent_texts" in names
            else []
        )
        return notes, recent
