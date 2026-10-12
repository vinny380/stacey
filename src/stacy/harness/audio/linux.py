"""Local Linux audio with enough speaker buffering for streamed speech."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pipecat.transports.base_output import BaseOutputTransport
from pipecat.transports.local.audio import LocalAudioOutputTransport, LocalAudioTransport

if TYPE_CHECKING:
    from pipecat.processors.frame_processor import FrameProcessor, FrameProcessorSetup


class LinuxAudioOutputTransport(LocalAudioOutputTransport):
    """Avoid the Pi's undersized automatic ALSA/PulseAudio playback buffer."""

    async def setup(self, setup: FrameProcessorSetup) -> None:
        await BaseOutputTransport.setup(self, setup)
        # The automatic buffer underruns with Pipecat's 40 ms speech chunks
        # on the Pi. 2048 frames keeps blocking writes clocked at 24 kHz.
        self._out_stream = self._py_audio.open(
            format=self._py_audio.get_format_from_width(2),
            channels=self._params.audio_out_channels,
            rate=self.sample_rate,
            output=True,
            output_device_index=self._params.output_device_index,
            frames_per_buffer=2048,
        )


class LinuxAudioTransport(LocalAudioTransport):
    def output(self) -> FrameProcessor:
        if self._output is None:
            self._output = LinuxAudioOutputTransport(self._pyaudio, self._params)
        return self._output
