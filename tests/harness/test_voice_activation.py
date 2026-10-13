"""Exercise voice activation at the production pipeline / Realtime boundary."""

import asyncio
from types import SimpleNamespace

import pytest

from stacy.harness.context import ConversationContext
from stacy.harness.voice import build_voice_pipeline, create_voice_worker
from stacy.runtime.settings import Settings


def test_invocation_and_expired_followups_use_the_production_pipeline(tmp_path, monkeypatch):
    from pipecat.adapters.schemas.tools_schema import ToolsSchema
    from pipecat.frames.frames import BotStoppedSpeakingFrame
    from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
    from pipecat.services.openai.realtime.events import (
        ConversationItem,
        ConversationItemAdded,
        ConversationItemInputAudioTranscriptionCompleted,
        ItemContent,
    )
    from pipecat.workers.runner import WorkerRunner

    class Passthrough(FrameProcessor):
        async def process_frame(self, frame, direction):
            await super().process_frame(frame, direction)
            await self.push_frame(frame, direction)

    async def scenario():
        incoming, outgoing = Passthrough(), Passthrough()
        voice = build_voice_pipeline(
            Settings(tmp_path, tmp_path / "memory"),
            "offline-test",
            ToolsSchema(standard_tools=[]),
            SimpleNamespace(input=lambda: incoming, output=lambda: outgoing),
            context=ConversationContext(),
            echo_cancelled=True,  # This test isolates activation from the separate echo guard.
        )
        llm = voice.llm
        configured = asyncio.Event()
        session = {}
        responses = []
        wire = []
        native_items = []

        @llm.event_handler("on_conversation_item_created")
        async def item_created(service, item_id, item):
            native_items.append(item_id)

        async def connect():
            llm._api_session_ready = True

        async def send(event):
            wire.append(event)
            if event["type"] == "session.update":
                session.update(event["session"])
                configured.set()
            if event["type"] == "response.create":
                responses.append("client")

        async def speech(item_id, text):
            # Model Realtime's documented server-VAD response contract, using
            # the production service's actual outgoing configuration.
            detection = session["audio"]["input"]["turn_detection"]
            if detection.get("create_response", True):
                responses.append("server VAD")
            await llm._handle_evt_conversation_item_added(
                ConversationItemAdded(
                    event_id="event_added_" + item_id,
                    type="conversation.item.added",
                    previous_item_id=None,
                    item=ConversationItem(
                        id=item_id,
                        type="message",
                        role="user",
                        content=[ItemContent(type="input_audio", transcript=text)],
                    ),
                )
            )
            await llm.handle_evt_input_audio_transcription_completed(
                ConversationItemInputAudioTranscriptionCompleted(
                    event_id="event_" + item_id,
                    type="conversation.item.input_audio_transcription.completed",
                    item_id=item_id,
                    content_index=0,
                    transcript=text,
                )
            )
            await asyncio.sleep(0.05)

        monkeypatch.setattr(llm, "_connect", connect)
        monkeypatch.setattr(llm, "_ws_send", send)
        runner = WorkerRunner(handle_sigint=False, handle_sigterm=False)
        worker = create_voice_worker(voice)
        await runner.add_workers(worker)
        running = asyncio.create_task(runner.run())
        try:
            await asyncio.wait_for(configured.wait(), 3)
            await asyncio.sleep(0.05)
            assert not responses, "Booting must configure tools without an uninvoked greeting"
            now = [100.0]
            llm.activation.clock = lambda: now[0]
            await speech("item_room", "Please.")
            assert not responses, "Uninvoked room speech generated a spoken response"
            assert "item_room" not in native_items
            assert any(
                e["type"] == "conversation.item.delete" and e["item_id"] == "item_room"
                for e in wire
            ), "Ignored audio must also leave the provider's conversation context"
            await speech("item_invoked", "Hey Stacey, what day is it?")
            assert responses == ["client"], "A named invocation must still get an answer"
            assert "item_invoked" in native_items
            await speech("item_invoked", "Hey Stacey, what day is it?")
            assert responses == ["client"], "Duplicate transcript events must not repeat answers"
            await outgoing.push_frame(BotStoppedSpeakingFrame(), FrameDirection.UPSTREAM)
            await asyncio.sleep(0.05)
            now[0] = 104.0
            await speech("item_followup", "And tomorrow?")
            assert responses == ["client", "client"]
            await outgoing.push_frame(BotStoppedSpeakingFrame(), FrameDirection.UPSTREAM)
            await asyncio.sleep(0.05)
            now[0] = 109.0  # Five seconds after actual playback stops.
            await speech("item_late", "Super.")
            assert len(responses) == 2, "An expired conversation must ignore unrelated room speech"
            await speech("item_again", "Stacy, what time is it?")
            assert len(responses) == 3, "Expiry must still allow a fresh named invocation"
        finally:
            await runner.cancel()
            await running

    asyncio.run(scenario())


@pytest.mark.parametrize("name", ["Stacy", "Stacey", "hey Stacy", "Okay, Stacey", "Yo Stacy"])
def test_invocation_recognizes_the_assistants_name(name):
    from stacy.harness.voice_activation import InvocationWindow

    window = InvocationWindow()
    assert window.accept("turn", f"{name}, can you hear me?") == "invocation"


def test_followup_is_measured_from_playback_and_preserves_speech_started_before_expiry():
    from stacy.harness.voice_activation import InvocationWindow

    now = [0.0]
    window = InvocationWindow(clock=lambda: now[0])
    assert window.accept("room", "Someone mentioned Stacy earlier.") is None
    assert window.accept("invoke", "Hey Stacy, hello.") == "invocation"
    now[0] = 20.0  # Tool/model processing is not a follow-up window.
    assert window.accept("working", "Please.") is None
    window.playback_started()
    now[0] = 30.0
    window.playback_stopped()
    now[0] = 34.9
    window.speech_started("followup")
    now[0] = 38.0  # The utterance itself can extend past the deadline.
    assert window.accept("followup", "And tomorrow?") == "followup"
    assert window.accept("room_again", "Super.") is None
