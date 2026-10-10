from __future__ import annotations

import json
import logging
import os
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

logger = logging.getLogger(__name__)


def provider_failed(value):
    if isinstance(value, list):
        return any(provider_failed(v) for v in value)
    if not isinstance(value, dict):
        return False
    if (
        value.get("successful") is False
        or value.get("is_error") is True
        or value.get("confirmed") is False
        or value.get("errors_by_calendar")
        or (isinstance(value.get("error_count"), int) and value["error_count"] > 0)
    ):
        return True
    return any(provider_failed(v) for v in value.values())


def telemetry_json(span):
    """Export timing/usage only; SDK spans otherwise embed the entire prompt."""
    data = json.loads(span.to_json(indent=None))
    safe = {
        "service.name",
        "conversation.id",
        "turn.number",
        "function_name",
        "tool.name",
        "gen_ai.system",
        "gen_ai.request.model",
        "gen_ai.response.model",
    }
    data["attributes"] = {
        key: value
        for key, value in data.get("attributes", {}).items()
        if key in safe
        or (
            isinstance(value, (int, float, bool))
            and any(word in key for word in ("tokens", "duration", "latency", "ttfb", "usage"))
            and not ("duration" in key and value < 0)
        )
    }
    # Exception events/status descriptions can contain URLs, keys or prompts.
    data["events"] = []
    if isinstance(data.get("status"), dict):
        data["status"].pop("description", None)
    data["resource"] = {"attributes": {"service.name": "stacy"}, "schema_url": ""}
    return json.dumps(data) + "\n"


@contextmanager
def local_telemetry(trace):
    """Use Pipecat's OpenTelemetry setup and the SDK's standard file exporter."""
    if os.environ.get("STACY_OTEL_ENABLED", "true").lower() in {"false", "0", "off"}:
        yield False
        return

    from opentelemetry import trace as otel
    from opentelemetry.sdk.trace.export import ConsoleSpanExporter
    from pipecat.utils.tracing.setup import setup_tracing

    path = trace.path.with_name(trace.path.name.replace("voice-", "otel-", 1))
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as output:
        exporter = ConsoleSpanExporter(out=output, formatter=telemetry_json)
        if not setup_tracing(service_name="stacy", exporter=exporter):
            raise RuntimeError("Pipecat OpenTelemetry setup failed")
        logger.info("OpenTelemetry trace: %s", path)
        try:
            yield True
        finally:
            # The SDK flushes its batch exporter before the local file is closed.
            otel.get_tracer_provider().shutdown()


class VoiceTrace:
    """Local, owner-only conversation/tool traces; no raw audio or system prompts."""

    def __init__(self, data_dir: Path):
        directory = data_dir / "traces"
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
        self.path = directory / f"voice-{stamp}-{uuid4().hex[:8]}.jsonl"
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)

    def record(self, event: str, **fields) -> None:
        entry = {"time": datetime.now(UTC).isoformat(), "event": event, **fields}
        with self.path.open("a") as file:
            file.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")


def attach_assistant_transcripts(llm, trace: VoiceTrace) -> None:
    # Pipecat reports a completed item through both item.done and response.done.
    # Deduplicate those notifications by item ID; distinct repeated replies remain visible.
    recorded: dict[str, tuple[str, str | None]] = {}

    @llm.event_handler("on_conversation_item_updated")
    async def item_updated(service, item_id, item):
        if item is None or item.type != "message" or item.role != "assistant":
            return
        text = "".join(part.transcript or part.text or "" for part in item.content or [])
        if not text or recorded.get(item_id) == (text, item.status):
            return
        recorded[item_id] = (text, item.status)
        trace.record("assistant_transcript", item_id=item_id, status=item.status, text=text)
        logger.info("Stacy: %s", text)


def trace_observers(trace: VoiceTrace) -> list:
    from pipecat.frames.frames import (
        BotStartedSpeakingFrame,
        BotStoppedSpeakingFrame,
        InterruptionFrame,
        TranscriptionFrame,
        UserStartedSpeakingFrame,
        UserStoppedSpeakingFrame,
    )
    from pipecat.observers.base_observer import BaseObserver
    from pipecat.observers.error_observer import ErrorObserver
    from pipecat.observers.function_call_observer import FunctionCallObserver
    from pipecat.processors.frame_processor import FrameDirection

    calls = FunctionCallObserver(include_arguments=True, include_results=True)
    account_calls: set[str] = set()

    @calls.event_handler("on_function_call_event")
    async def tool_event(observer, event):
        fields = event.model_dump(mode="json", exclude_none=True)
        kind = fields.pop("kind")
        if event.function_name == "send_text":
            fields.pop("arguments", None)
        # Account tools must never put credentials into diagnostic files.
        if event.function_name == "call_mcp_tool" and isinstance(event.arguments, dict):
            if event.arguments.get("tool") == "COMPOSIO_MANAGE_CONNECTIONS":
                account_calls.add(event.tool_call_id)
        # Pipecat's completion events omit arguments; retain the call identity
        # from its start/progress event to redact authorization links on completion.
        account_result = event.tool_call_id in account_calls
        if (
            event.function_name.startswith("spotify_")
            or account_result
            or event.function_name in {"recent_texts", "connection_status"}
        ):
            fields.pop("result", None)
        if kind in {
            "function_call_completed",
            "function_call_failed",
            "function_call_cancelled",
            "function_call_timed_out",
        }:
            account_calls.discard(event.tool_call_id)
        if event.in_progress_at is not None and kind != "function_call_in_progress":
            duration = event.timestamp - event.in_progress_at
            if duration >= 0:
                fields["execution_seconds"] = duration
        trace.record(kind, **fields)
        logger.info("%s: %s [%s]", kind, event.function_name, event.tool_call_id)
        # The MCP adapter can return an isError response as text rather than
        # raising. Preserve that distinction from a successful function return.
        if isinstance(event.result, str) and (
            event.result.startswith("Error") or event.result == "Sorry, could not call the mcp tool"
        ):
            trace.record(
                "tool_reported_error",
                function_name=event.function_name,
                tool_call_id=event.tool_call_id,
                error=event.result,
            )
            logger.warning("Tool %s returned an error: %s", event.function_name, event.result)
        elif isinstance(event.result, str):
            try:
                result = json.JSONDecoder().raw_decode(event.result.lstrip())[0]
            except ValueError:
                result = None
            if provider_failed(result):
                trace.record(
                    "tool_reported_error",
                    function_name=event.function_name,
                    tool_call_id=event.tool_call_id,
                    error="Provider reported failure or action verification uncertainty",
                )

    errors = ErrorObserver()

    @errors.event_handler("on_error")
    async def error_event(observer, event):
        trace.record("pipeline_error", **event.model_dump(mode="json", exclude_none=True))

    class SpeechObserver(BaseObserver):
        def __init__(self):
            super().__init__(observe_every_push=False)
            self.user_finished = None
            self.first_audio = False

        async def on_push_frame(self, data):
            frame = data.frame
            if (
                frame.broadcast_sibling_id is not None
                and data.direction != FrameDirection.DOWNSTREAM
            ):
                return
            if isinstance(frame, TranscriptionFrame):
                item_id = getattr(frame.result, "item_id", None)
                trace.record("user_transcript", item_id=item_id, text=frame.text)
                logger.info("You: %s", frame.text)
            elif isinstance(
                frame,
                (
                    UserStartedSpeakingFrame,
                    UserStoppedSpeakingFrame,
                    BotStartedSpeakingFrame,
                    BotStoppedSpeakingFrame,
                    InterruptionFrame,
                ),
            ):
                trace.record(type(frame).__name__, pipeline_time_ns=data.timestamp)
                if isinstance(frame, UserStoppedSpeakingFrame):
                    self.user_finished, self.first_audio = data.timestamp, False
                elif isinstance(frame, UserStartedSpeakingFrame):
                    self.user_finished = None
                elif isinstance(frame, BotStartedSpeakingFrame) and self.user_finished is not None:
                    duration = (data.timestamp - self.user_finished) / 1e9
                    if duration >= 0:
                        trace.record(
                            "response_audio_latency",
                            seconds=duration,
                            first_audio=not self.first_audio,
                        )
                    self.first_audio = True

    return [calls, errors, SpeechObserver()]
