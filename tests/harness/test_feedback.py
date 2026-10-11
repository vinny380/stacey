import asyncio

import pytest

pytest.importorskip("pipecat")
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    CancelFrame,
    FunctionCallResultFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.observers.base_observer import FramePushed
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from stacy.alarms.audio import make_typing
from stacy.harness.feedback import WorkSoundMixer, WorkSoundObserver


def test_typing_pcm_is_audible_only_during_silent_work_and_preserves_speech(tmp_path):
    async def scenario():
        now = [0.0]
        mixer = WorkSoundMixer(make_typing(tmp_path / "typing.wav"), clock=lambda: now[0])
        observer = WorkSoundObserver(mixer)
        source, target = FrameProcessor(), FrameProcessor()

        async def frame(value):
            await observer.on_push_frame(
                FramePushed(source, target, value, FrameDirection.DOWNSTREAM, 0)
            )

        silence = b"\x00" * 24000  # enough to include several quiet clicks
        await mixer.start(24000)
        assert await mixer.mix(silence) == silence  # idle
        await frame(UserStoppedSpeakingFrame())
        assert await mixer.mix(silence) == silence  # no click on every quick turn
        now[0] = 0.7
        assert any(await mixer.mix(silence))
        speech = b"\x01\x00" * 12000
        assert await mixer.mix(speech) == speech  # never alter actual speech PCM
        await frame(BotStartedSpeakingFrame())
        assert await mixer.mix(silence) == silence
        await frame(BotStoppedSpeakingFrame())
        now[0] = 2
        assert await mixer.mix(silence) == silence  # completed response, idle
        await frame(FunctionCallResultFrame("web_search", "call", {}, "result"))
        now[0] = 3
        assert any(await mixer.mix(silence))  # waiting for spoken answer
        await frame(UserStartedSpeakingFrame())
        assert await mixer.mix(silence) == silence
        await frame(UserStoppedSpeakingFrame())
        now[0] = 4
        await frame(CancelFrame())
        assert await mixer.mix(silence) == silence
        await mixer.stop()

    asyncio.run(scenario())
