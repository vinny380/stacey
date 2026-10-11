from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from pipecat.processors.frame_processor import FrameProcessor


class VoiceTransport(Protocol):
    def input(self) -> FrameProcessor: ...
    def output(self) -> FrameProcessor: ...


@dataclass(frozen=True)
class AudioSession:
    transport: VoiceTransport
    echo_cancelled: bool = False
