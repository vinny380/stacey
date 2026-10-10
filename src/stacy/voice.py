from __future__ import annotations

import os
import sys

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
):
    from pipecat.services.openai.realtime.events import (
        AudioConfiguration,
        AudioInput,
        AudioOutput,
        InputAudioNoiseReduction,
        InputAudioTranscription,
        SemanticTurnDetection,
        SessionProperties,
    )
    from pipecat.services.openai.realtime.llm import OpenAIRealtimeLLMService

    return OpenAIRealtimeLLMService(
        api_key=key,
        settings=OpenAIRealtimeLLMService.Settings(
            model=settings.realtime_model,
            system_instruction=instructions(
                settings,
                memory_source=memory_source,
                retrieved_notes=retrieved_notes,
                recent_texts=recent_texts,
            ),
            session_properties=SessionProperties(
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
):
    """Build the same voice/tool pipeline for local audio and headless checks."""
    from pipecat.adapters.schemas.function_schema import FunctionSchema
    from pipecat.adapters.schemas.tools_schema import ToolsSchema
    from pipecat.pipeline.pipeline import Pipeline

    from stacy.context_management import VoiceContext, context_aggregators

    llm = create_realtime_service(
        settings,
        key,
        memory_source=trace.path.name if trace is not None else "current conversation",
        retrieved_notes=retrieved_notes,
        recent_texts=recent_texts,
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
    aggregators = context_aggregators(context, llm, key, settings, trace=trace)
    return Pipeline(
        [
            transport.input(),
            aggregators.user(),
            llm,
            transport.output(),
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


async def run_voice(settings: Settings, mcp_url: str) -> None:
    key = openai_key()
    from loguru import logger

    # Pipecat uses Loguru independently of the CLI's standard logging setup.
    # Avoid dumping prompts and personal memory at DEBUG during normal use.
    logger.remove()
    logger.add(sys.stderr, level=os.environ.get("STACY_LOG_LEVEL", "INFO").upper())
    from mcp.client.session_group import StreamableHttpParameters
    from pipecat.services.mcp_service import MCPClient
    from pipecat.transports.local.audio import LocalAudioTransport, LocalAudioTransportParams
    from pipecat.workers.runner import WorkerRunner

    from stacy.tracing import VoiceTrace, local_telemetry

    async with MCPClient(StreamableHttpParameters(url=mcp_url, timeout=180)) as mcp:
        tools = await mcp.tools()
        notes, recent = await startup_context(mcp_url)
        trace = VoiceTrace(settings.data_dir)
        trace.record(
            "session_started",
            model=settings.realtime_model,
            tools=[tool.name for tool in tools.standard_tools],
        )
        print(f"Conversation and tool trace: {trace.path}", file=sys.stderr)
        with local_telemetry(trace) as tracing_enabled:
            feedback = None
            if os.environ.get("STACY_WORK_SOUND", "typing").lower() not in {"off", "false", "0"}:
                from stacy.feedback import WorkSoundMixer
                from stacy.sound import make_typing

                feedback = WorkSoundMixer(make_typing(settings.data_dir / "typing.wav"))
            transport = LocalAudioTransport(
                LocalAudioTransportParams(
                    audio_in_enabled=True,
                    audio_out_enabled=True,
                    audio_out_mixer=feedback,
                )
            )
            pipeline = build_voice_pipeline(
                settings,
                key,
                tools,
                transport,
                trace=trace,
                retrieved_notes=notes,
                recent_texts=recent,
            )
            worker = create_voice_worker(
                pipeline, trace=trace, enable_tracing=tracing_enabled, feedback=feedback
            )

            @worker.event_handler("on_pipeline_started")
            async def audio_started(worker, frame):
                print(
                    "Stacy's microphone/speaker pipeline is active. "
                    "Use headphones to avoid speaker feedback; Ctrl+C to stop.",
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
