"""Speaker turn taking through Pipecat's user-input mute boundary."""

from __future__ import annotations

import time

from pipecat.frames.frames import BotStoppedSpeakingFrame, Frame
from pipecat.turns.user_mute import AlwaysUserMuteStrategy


class SpeakerMuteStrategy(AlwaysUserMuteStrategy):
    """Ignore microphone bleed during actual playback and its short acoustic tail.

    Output transport speaking events follow playback, whereas model response-end
    events can arrive while several seconds of speech are still queued. Input
    continues streaming locally; Pipecat suppresses it before Realtime and VAD.
    This is half-duplex turn taking, not acoustic echo cancellation.
    """

    def __init__(self, *, tail_seconds=0.35, clock=time.monotonic):
        super().__init__()
        self.clock, self.tail_seconds = clock, tail_seconds
        self.listen_after = float("-inf")

    async def process_frame(self, frame: Frame) -> bool:
        speaking = await super().process_frame(frame)
        if isinstance(frame, BotStoppedSpeakingFrame):
            self.listen_after = self.clock() + self.tail_seconds
        return speaking or self.clock() < self.listen_after
