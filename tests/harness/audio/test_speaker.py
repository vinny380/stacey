import asyncio
import base64
from types import SimpleNamespace

import pytest

from stacy.harness.context import ConversationContext
from stacy.harness.voice import build_voice_pipeline, create_voice_worker
from stacy.runtime.settings import Settings


@pytest.mark.parametrize(
    "audio_mode,echo_cancelled", [("speaker", False), ("headphones", False), ("speaker", True)]
)
def test_speaker_echo_does_not_reach_realtime_and_listening_resumes(
    tmp_path,
    monkeypatch,
    audio_mode,
    echo_cancelled,
):
    """Replay played PCM into the real input transport as loudspeaker microphone bleed."""
    pytest.importorskip("pipecat.services.openai.realtime.llm")
    from pipecat.adapters.schemas.tools_schema import ToolsSchema
    from pipecat.frames.frames import (
        BotStoppedSpeakingFrame,
        InputAudioRawFrame,
        LLMFullResponseEndFrame,
        TTSAudioRawFrame,
        TTSStoppedFrame,
    )
    from pipecat.processors.frame_processor import FrameDirection
    from pipecat.transports.base_input import BaseInputTransport
    from pipecat.transports.base_output import BaseOutputTransport
    from pipecat.transports.base_transport import TransportParams
    from pipecat.workers.runner import WorkerRunner

    async def scenario():
        configured, playing, stopped = asyncio.Event(), asyncio.Event(), asyncio.Event()
        events = []
        params = TransportParams(audio_in_enabled=True, audio_out_enabled=True)

        class Input(BaseInputTransport):
            async def start(self, frame):
                await super().start(frame)
                await self.set_transport_ready(frame)

        incoming = Input(params)

        class Output(BaseOutputTransport):
            async def start(self, frame):
                await super().start(frame)
                await self.set_transport_ready(frame)

            async def write_audio_frame(self, frame):
                playing.set()
                await incoming.push_audio_frame(
                    InputAudioRawFrame(frame.audio, frame.sample_rate, frame.num_channels)
                )
                await asyncio.sleep(0.01)
                return True

        outgoing = Output(params)
        original_push = outgoing.push_frame

        async def pushed(frame, direction=FrameDirection.DOWNSTREAM):
            await original_push(frame, direction)
            if isinstance(frame, BotStoppedSpeakingFrame):
                stopped.set()

        monkeypatch.setattr(outgoing, "push_frame", pushed)
        pipeline = build_voice_pipeline(
            Settings(tmp_path, tmp_path / "memory", audio_mode=audio_mode),
            "offline-test",
            ToolsSchema(standard_tools=[]),
            SimpleNamespace(
                input=lambda: incoming,
                output=lambda: outgoing,
            ),
            context=ConversationContext(),
            echo_cancelled=echo_cancelled,
        )
        llm = pipeline.llm

        async def connect():
            llm._api_session_ready = True

        async def send(event):
            events.append(event)
            if event["type"] == "session.update":
                configured.set()

        monkeypatch.setattr(llm, "_connect", connect)
        monkeypatch.setattr(llm, "_ws_send", send)

        def audio_sent():
            return [
                base64.b64decode(e["audio"])
                for e in events
                if e["type"] == "input_audio_buffer.append"
            ]

        async def microphone(pcm):
            await incoming.push_audio_frame(InputAudioRawFrame(pcm, 24000, 1))
            await asyncio.sleep(0.05)

        worker = create_voice_worker(pipeline)
        runner = WorkerRunner(handle_sigint=False, handle_sigterm=False)
        await runner.add_workers(worker)
        running = asyncio.create_task(runner.run())
        try:
            await asyncio.wait_for(configured.wait(), 3)
            user = b"\x01\x00" * 480
            echo = b"\x32\x00" * 2880
            await microphone(user)
            assert user in audio_sent(), "Healthy user microphone audio must pass"
            await llm.push_frame(TTSAudioRawFrame(echo, 24000, 1))
            await llm.push_frame(LLMFullResponseEndFrame())  # Generation ends before playback.
            await llm.push_frame(TTSStoppedFrame())
            await asyncio.wait_for(playing.wait(), 3)
            await asyncio.wait_for(stopped.wait(), 3)
            await microphone(b"\x32\x00" * 480)  # Residual echo after playback completes.
            echo_received = any(b"\x32\x00" in pcm for pcm in audio_sent())
            if audio_mode == "speaker" and not echo_cancelled:
                assert not echo_received, (
                    "Stacy's own speaker audio reached Realtime as user microphone input"
                )
            else:
                assert echo_received, (
                    "Headphones and echo-cancelled speakers must retain interruption audio"
                )
            await asyncio.sleep(0.4)
            await microphone(bytes(len(user)))  # The real mic continuously streams silence.
            await microphone(user)
            assert audio_sent().count(user) == 2, "Listening must resume after the echo tail"
        finally:
            await runner.cancel()
            await running

    asyncio.run(scenario())


def test_speaker_tail_is_extended_by_the_next_reply():
    pytest.importorskip("pipecat")
    from pipecat.frames.frames import (
        BotStartedSpeakingFrame,
        BotStoppedSpeakingFrame,
        InputAudioRawFrame,
    )

    from stacy.harness.audio.speaker import SpeakerMuteStrategy

    async def scenario():
        now = [0.0]
        strategy = SpeakerMuteStrategy(clock=lambda: now[0])
        audio = InputAudioRawFrame(b"\x01\x00" * 480, 24000, 1)
        assert not await strategy.process_frame(audio)
        assert await strategy.process_frame(BotStartedSpeakingFrame())
        assert await strategy.process_frame(BotStoppedSpeakingFrame())
        now[0] = 0.2
        assert await strategy.process_frame(BotStartedSpeakingFrame())
        now[0] = 0.6  # First reply's tail expired, but the second reply is still playing.
        assert await strategy.process_frame(audio)
        assert await strategy.process_frame(BotStoppedSpeakingFrame())
        now[0] = 0.8
        assert await strategy.process_frame(audio)
        now[0] = 1.0
        assert not await strategy.process_frame(audio)

    asyncio.run(scenario())


def test_audio_mode_defaults_to_speakers_and_rejects_unknown_mode(monkeypatch):
    monkeypatch.delenv("STACY_AUDIO_MODE", raising=False)
    assert Settings.load().audio_mode == "speaker"
    monkeypatch.setenv("STACY_AUDIO_MODE", "typo")
    with pytest.raises(ValueError, match="STACY_AUDIO_MODE"):
        Settings.load()
