from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from stacy.harness.audio import VoiceTransport
from stacy.harness.audio.transport import open_audio_transport
from stacy.harness.context import ConversationContext
from stacy.harness.instructions import instructions as instructions
from stacy.harness.tracing import VoiceTrace
from stacy.runtime.settings import Settings

if TYPE_CHECKING:
    from fastmcp import FastMCP
    from pipecat.adapters.schemas.tools_schema import ToolsSchema
    from pipecat.pipeline.pipeline import Pipeline
    from pipecat.pipeline.worker import PipelineWorker
    from pipecat.services.openai.realtime.llm import OpenAIRealtimeLLMService

    from stacy.harness.feedback import WorkSoundMixer


@dataclass(frozen=True)
class VoicePipeline:
    pipeline: Pipeline
    llm: OpenAIRealtimeLLMService


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
    context: ConversationContext,
    memory_source: str = "current conversation",
) -> OpenAIRealtimeLLMService:
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
                context=context,
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
    tools: ToolsSchema,
    transport: VoiceTransport,
    *,
    context: ConversationContext,
    trace: VoiceTrace | None = None,
    echo_cancelled: bool = False,
) -> VoicePipeline:
    """Build the same voice/tool pipeline for local audio and headless checks."""
    from pipecat.adapters.schemas.function_schema import FunctionSchema
    from pipecat.adapters.schemas.tools_schema import ToolsSchema
    from pipecat.pipeline.pipeline import Pipeline

    from stacy.harness.summarization import RealtimeContextBudget, VoiceContext, context_aggregators

    llm = create_realtime_service(
        settings,
        key,
        memory_source=trace.path.name if trace is not None else "current conversation",
        context=context,
    )
    if trace is not None:
        from stacy.harness.tracing import attach_assistant_transcripts

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
        context, llm, key, settings, trace=trace, echo_cancelled=echo_cancelled
    )
    pipeline = Pipeline(
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

    return VoicePipeline(pipeline, llm)


def create_voice_worker(
    voice: VoicePipeline,
    *,
    trace: VoiceTrace | None = None,
    enable_tracing: bool = False,
    feedback: WorkSoundMixer | None = None,
) -> PipelineWorker:
    """Initialize Realtime's context/tools before the first microphone turn."""
    from pipecat.frames.frames import LLMRunFrame
    from pipecat.pipeline.worker import PipelineParams, PipelineWorker

    from stacy.harness.tracing import trace_observers

    observers = trace_observers(trace) if trace is not None else []
    if feedback is not None:
        from stacy.harness.feedback import WorkSoundObserver

        observers.append(WorkSoundObserver(feedback))
    worker = PipelineWorker(
        voice.pipeline,
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


async def run_voice(settings: Settings, mcp_url: str) -> None:
    key = openai_key()
    from loguru import logger

    # Pipecat uses Loguru independently of the CLI's standard logging setup.
    # Avoid dumping prompts and personal memory at DEBUG during normal use.
    logger.remove()
    logger.add(sys.stderr, level=settings.log_level)
    from mcp.client.session_group import StreamableHttpParameters
    from pipecat.services.mcp_service import MCPClient
    from pipecat.workers.runner import WorkerRunner

    from stacy.harness.tracing import VoiceTrace, local_telemetry

    async with MCPClient(StreamableHttpParameters(url=mcp_url, timeout=180)) as mcp:
        tools = await mcp.tools()
        context = await startup_context(mcp_url)
        trace = VoiceTrace(settings.data_dir)
        trace.record(
            "session_started",
            model=settings.realtime_model,
            audio_mode=settings.audio_mode,
            tools=[tool.name for tool in tools.standard_tools],
        )
        print(f"Conversation and tool trace: {trace.path}", file=sys.stderr)
        with local_telemetry(trace, enabled=settings.telemetry_enabled) as tracing_enabled:
            feedback = None
            if settings.work_sound:
                from stacy.alarms.audio import make_typing
                from stacy.harness.feedback import WorkSoundMixer

                feedback = WorkSoundMixer(make_typing(settings.data_dir / "typing.wav"))
            async with open_audio_transport(settings, feedback=feedback, trace=trace) as audio:
                trace.record(
                    "audio_transport_ready",
                    transport=type(audio.transport).__name__,
                    echo_cancelled=audio.echo_cancelled,
                    interruptions=audio.echo_cancelled or settings.audio_mode == "headphones",
                )
                pipeline = build_voice_pipeline(
                    settings,
                    key,
                    tools,
                    audio.transport,
                    trace=trace,
                    context=context,
                    echo_cancelled=audio.echo_cancelled,
                )
                worker = create_voice_worker(
                    pipeline, trace=trace, enable_tracing=tracing_enabled, feedback=feedback
                )

                @worker.event_handler("on_pipeline_started")
                async def audio_started(worker, frame):
                    mode = (
                        "Echo cancellation is active; you can interrupt me. "
                        if audio.echo_cancelled
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


async def startup_context(mcp_url: str | FastMCP) -> ConversationContext:
    from fastmcp import Client

    async with Client(mcp_url) as client:
        schemas = await client.list_tools()
        return await ConversationContext.load(client, schemas)
