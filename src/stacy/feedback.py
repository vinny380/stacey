"""Work sounds through Pipecat's speaker mixer, never through speech context."""

from __future__ import annotations

import time

from pipecat.audio.mixers.soundfile_mixer import SoundfileMixer
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    CancelFrame,
    EndFrame,
    ErrorFrame,
    FunctionCallCancelFrame,
    FunctionCallResultFrame,
    FunctionCallsStartedFrame,
    InterruptionFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    MixerEnableFrame,
    TTSAudioRawFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.observers.base_observer import BaseObserver
from pipecat.processors.frame_processor import FrameDirection


class WorkSoundMixer(SoundfileMixer):
    def __init__(self, path, *, delay=0.65, clock=time.monotonic):
        super().__init__(
            sound_files={"typing": str(path)},
            default_sound="typing",
            volume=0.35,
            mixing=False,
            loop=True,
        )
        self.clock, self.delay = clock, delay
        self.busy = self.user_speaking = self.bot_speaking = False
        self.waiting_since = self.clock()

    def working(self):
        if not self.busy:
            self.waiting_since = self.clock()
        self.busy = True

    async def mix(self, audio):
        enabled = (
            self.busy
            and not self.user_speaking
            and not self.bot_speaking
            and self.clock() - self.waiting_since >= self.delay
        )
        # Never mix typing into a speech PCM chunk, including the small gap
        # before transport emits its BotStartedSpeakingFrame.
        await self.process_frame(MixerEnableFrame(enable=enabled and not any(audio)))
        return await super().mix(audio)


class WorkSoundObserver(BaseObserver):
    def __init__(self, mixer):
        super().__init__(observe_every_push=False)
        self.mixer = mixer
        self.pending = set()
        self.response_audio = False

    async def on_push_frame(self, data):
        frame, mixer = data.frame, self.mixer
        if frame.broadcast_sibling_id is not None and data.direction != FrameDirection.DOWNSTREAM:
            return
        if isinstance(frame, UserStartedSpeakingFrame):
            mixer.user_speaking, mixer.busy = True, False
        elif isinstance(frame, UserStoppedSpeakingFrame):
            mixer.user_speaking = False
            mixer.working()
        elif isinstance(frame, (BotStartedSpeakingFrame, TTSAudioRawFrame)):
            mixer.bot_speaking = True
            self.response_audio = True
        elif isinstance(frame, BotStoppedSpeakingFrame):
            mixer.bot_speaking = False
            mixer.busy = bool(self.pending)
            mixer.waiting_since = mixer.clock()
        elif isinstance(frame, FunctionCallsStartedFrame):
            self.pending.update(call.tool_call_id for call in frame.function_calls)
            mixer.working()
        elif isinstance(frame, FunctionCallResultFrame):
            self.pending.discard(frame.tool_call_id)
            mixer.working()
        elif isinstance(frame, FunctionCallCancelFrame):
            self.pending.discard(frame.tool_call_id)
            if not self.pending:
                mixer.busy = False
        elif isinstance(frame, LLMFullResponseStartFrame):
            self.response_audio = False
            mixer.working()
        elif isinstance(frame, LLMFullResponseEndFrame):
            if not self.pending and not self.response_audio:
                mixer.busy = False
        elif isinstance(frame, InterruptionFrame):
            mixer.busy = mixer.bot_speaking = False
        elif isinstance(frame, (EndFrame, CancelFrame, ErrorFrame)):
            mixer.busy = mixer.bot_speaking = mixer.user_speaking = False
            self.pending.clear()
