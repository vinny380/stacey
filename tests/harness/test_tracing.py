import asyncio
import json

import pytest

from stacy.harness.context import ConversationContext
from stacy.harness.tracing import VoiceTrace, attach_assistant_transcripts, trace_observers


def test_otel_export_excludes_raw_prompts_links_secrets_and_bad_durations():
    from opentelemetry.sdk.trace import ReadableSpan
    from opentelemetry.trace import Status, StatusCode

    from stacy.harness.tracing import telemetry_json

    span = ReadableSpan(
        "llm_request",
        attributes={
            "gen_ai.request.model": "model",
            "usage.input_tokens": 100,
            "turn.duration_seconds": -1,
            "llm_request.input": "SECRET prompt auth link",
            "llm_request.output": "private text",
            "http.url": "https://auth/SECRET",
        },
        status=Status(StatusCode.ERROR, "SECRET"),
    )
    result = telemetry_json(span)
    assert "SECRET" not in result and "-1" not in result
    assert json.loads(result)["attributes"]["usage.input_tokens"] == 100


def entries(trace):
    return [json.loads(line) for line in trace.path.read_text().splitlines()]


def test_assistant_api_notifications_are_deduplicated_but_repeated_replies_are_visible(tmp_path):
    pytest.importorskip("pipecat.services.openai.realtime.llm")
    from pipecat.services.openai.realtime.events import ConversationItem, ItemContent

    from stacy.harness.voice import create_realtime_service
    from stacy.runtime.settings import Settings

    async def scenario():
        trace = VoiceTrace(tmp_path)
        llm = create_realtime_service(
            Settings(tmp_path, tmp_path / "memory"), "offline-test", context=ConversationContext()
        )
        attach_assistant_transcripts(llm, trace)
        item = ConversationItem(
            id="reply-1",
            type="message",
            role="assistant",
            status="completed",
            content=[ItemContent(type="output_audio", transcript="Timer set.")],
        )
        await llm._call_event_handler("on_conversation_item_updated", item.id, item)
        await llm._call_event_handler("on_conversation_item_updated", item.id, item)
        repeated = item.model_copy(update={"id": "reply-2"})
        await llm._call_event_handler("on_conversation_item_updated", repeated.id, repeated)
        await asyncio.sleep(0)
        assert [row["item_id"] for row in entries(trace)] == ["reply-1", "reply-2"]
        assert trace.path.stat().st_mode & 0o777 == 0o600

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "result",
    [
        "Error calling mcp tool read_memory: disconnected",
        "Sorry, could not call the mcp tool",
    ],
)
def test_mcp_text_failure_is_not_recorded_as_only_a_success(tmp_path, result):
    pytest.importorskip("pipecat.observers.function_call_observer")
    from pipecat.frames.frames import FunctionCallInProgressFrame, FunctionCallResultFrame
    from pipecat.observers.base_observer import FramePushed
    from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

    async def scenario():
        trace = VoiceTrace(tmp_path)
        observers = trace_observers(trace)
        source, destination = FrameProcessor(), FrameProcessor()
        for frame in [
            FunctionCallInProgressFrame("search_memory", "search-1", {"query": "coding"}),
            FunctionCallResultFrame(
                "search_memory",
                "search-1",
                {"query": "coding"},
                result=result,
            ),
        ]:
            data = FramePushed(source, destination, frame, FrameDirection.DOWNSTREAM, 0)
            for observer in observers:
                await observer.on_push_frame(data)
        await asyncio.sleep(0)
        failure = next(r for r in entries(trace) if r["event"] == "tool_reported_error")
        assert failure["tool_call_id"] == "search-1" and failure["error"] == result

    asyncio.run(scenario())


def test_trace_captures_user_speech_tool_failures_and_pipeline_errors(tmp_path):
    pytest.importorskip("pipecat.observers.function_call_observer")
    from pipecat.frames.frames import (
        ErrorFrame,
        FunctionCallInProgressFrame,
        FunctionCallResultFrame,
        TranscriptionFrame,
    )
    from pipecat.observers.base_observer import FramePushed
    from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

    async def scenario():
        trace = VoiceTrace(tmp_path)
        observers = trace_observers(trace)
        source, destination = FrameProcessor(), FrameProcessor()
        frames = [
            TranscriptionFrame("Wake me at seven", "", "now"),
            FunctionCallInProgressFrame(
                "set_alarm", "call-1", {"when": "07:00"}, cancel_on_interruption=True
            ),
            FunctionCallResultFrame(
                "set_alarm",
                "call-1",
                {"when": "07:00"},
                result="Tool failed",
                error="MCP session disconnected",
            ),
            ErrorFrame("Realtime connection lost"),
        ]
        for frame in frames:
            data = FramePushed(source, destination, frame, FrameDirection.DOWNSTREAM, 0)
            for observer in observers:
                await observer.on_push_frame(data)
        await asyncio.sleep(0)
        rows = entries(trace)
        assert any(row["event"] == "user_transcript" for row in rows)
        failure = next(row for row in rows if row["event"] == "function_call_failed")
        assert failure["tool_call_id"] == "call-1"
        assert failure["error"] == "MCP session disconnected"
        assert any(row["event"] == "pipeline_error" for row in rows)

    asyncio.run(scenario())


def test_consumer_account_links_are_not_recorded_in_tool_results(tmp_path):
    pytest.importorskip("pipecat.observers.function_call_observer")
    from pipecat.frames.frames import FunctionCallInProgressFrame, FunctionCallResultFrame
    from pipecat.observers.base_observer import FramePushed
    from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

    async def scenario():
        trace = VoiceTrace(tmp_path)
        observers = trace_observers(trace)
        source, destination = FrameProcessor(), FrameProcessor()
        arguments = {
            "server": "composio",
            "tool": "COMPOSIO_MANAGE_CONNECTIONS",
            "arguments": {"toolkits": [{"name": "googlecalendar", "action": "add"}]},
        }
        link = "https://auth.example/private-authorization-link"
        for frame in [
            FunctionCallInProgressFrame("call_mcp_tool", "auth-1", arguments),
            FunctionCallResultFrame("call_mcp_tool", "auth-1", arguments, result=link),
        ]:
            for observer in observers:
                await observer.on_push_frame(
                    FramePushed(source, destination, frame, FrameDirection.DOWNSTREAM, 0)
                )
        await asyncio.sleep(0)
        rows = entries(trace)
        assert any(row["event"] == "function_call_completed" for row in rows)
        assert link not in trace.path.read_text()

    asyncio.run(scenario())


def test_authorization_error_links_are_redacted_from_every_diagnostic_sink(tmp_path, caplog):
    from pipecat.frames.frames import FunctionCallResultFrame
    from pipecat.observers.base_observer import FramePushed
    from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

    async def scenario():
        trace = VoiceTrace(tmp_path)
        link = "https://auth.example/private-code"
        frame = FunctionCallResultFrame(
            "call_mcp_tool", "auth-error", {}, result=f"Error: sign in at {link}"
        )
        data = FramePushed(FrameProcessor(), FrameProcessor(), frame, FrameDirection.DOWNSTREAM, 0)
        for observer in trace_observers(trace):
            await observer.on_push_frame(data)
        await asyncio.sleep(0)
        assert link not in trace.path.read_text()
        assert link not in caplog.text
        assert any(row["event"] == "tool_reported_error" for row in entries(trace))

    asyncio.run(scenario())


def test_text_diagnostics_redact_nested_json_links_env_and_bound_payloads(tmp_path, monkeypatch):
    from stacy.harness.tracing import TextTrace

    monkeypatch.setenv("CUSTOM_API_KEY", "opaque-configured-secret")
    trace = TextTrace(tmp_path)
    trace.record(
        "payload",
        result={
            "content": json.dumps(
                {
                    "client_secret": "hidden-client-secret",
                    "url": "https://provider.test/auth?code=private-code",
                    "message": "failed with opaque-configured-secret",
                    "device": "Echo",
                }
            ),
            "authorization": "Bearer hidden-bearer",
            "items": list(range(100)),
        },
    )
    raw = trace.path.read_text()
    for secret in (
        "hidden-client-secret",
        "private-code",
        "opaque-configured-secret",
        "hidden-bearer",
    ):
        assert secret not in raw
    assert "Echo" in raw and "_omitted_items" in raw


def test_key_redaction_preserves_spotify_operation_names():
    from stacy.harness.tracing import diagnostic_value

    assert diagnostic_value("SPOTIFY_GET_PLAYBACK_STATE") == "SPOTIFY_GET_PLAYBACK_STATE"
    assert diagnostic_value("CHECKING_PLAYBACK") == "CHECKING_PLAYBACK"
    assert diagnostic_value("ck_privatecredential") == "<REDACTED>"


def test_custom_mcp_authorization_links_are_not_recorded(tmp_path):
    pytest.importorskip("pipecat.observers.function_call_observer")
    from pipecat.frames.frames import FunctionCallInProgressFrame, FunctionCallResultFrame
    from pipecat.observers.base_observer import FramePushed
    from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

    async def scenario():
        trace = VoiceTrace(tmp_path)
        observers = trace_observers(trace)
        source, destination = FrameProcessor(), FrameProcessor()
        for name in ("connect_mcp", "verify_connection", "reconnect_connection"):
            args = {"alias": "research", "url": "https://research.example/mcp"}
            for frame in (
                FunctionCallInProgressFrame(name, name, args),
                FunctionCallResultFrame(
                    name,
                    name,
                    args,
                    result=json.dumps(
                        {
                            "status": "awaiting_authorization",
                            "authorization_url": "https://auth.example/PRIVATE-LINK",
                        }
                    ),
                ),
            ):
                for observer in observers:
                    await observer.on_push_frame(
                        FramePushed(source, destination, frame, FrameDirection.DOWNSTREAM, 0)
                    )
        await asyncio.sleep(0)
        assert "PRIVATE-LINK" not in trace.path.read_text()
        assert any(row["event"] == "function_call_completed" for row in entries(trace))

    asyncio.run(scenario())
