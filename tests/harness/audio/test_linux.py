import asyncio
import time
from types import SimpleNamespace

import pytest

pytest.importorskip("pipecat.transports.local.audio")

from pipecat.frames.frames import EndFrame, TTSAudioRawFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.transports.local import audio
from pipecat.workers.runner import WorkerRunner

from stacy.harness.audio.transport import open_audio_transport
from stacy.runtime.settings import Settings


def test_linux_speaker_renders_pcm_without_blocking_conversation(tmp_path, monkeypatch):
    """An undersized ALSA buffer loses PCM instead of clocking speech writes."""
    streams = []
    payload = b"\x32\x00" * 4800

    class Speaker:
        def __init__(self, clocked):
            self.clocked = clocked
            self.rendered = bytearray()
            self.closed = False

        def start_stream(self):
            pass

        def write(self, pcm):
            # Model the measured Pi failure: a small automatic buffer drops
            # PCM and returns immediately instead of clocking speech.
            if self.clocked:
                time.sleep(0.02)
                self.rendered.extend(pcm)

        def stop_stream(self):
            pass

        def close(self):
            self.closed = True

    class Devices:
        def get_format_from_width(self, width):
            return width

        def open(self, **kwargs):
            speaker = Speaker(clocked=kwargs.get("frames_per_buffer", 0) >= 2048)
            streams.append(speaker)
            return speaker

    monkeypatch.setattr(audio.pyaudio, "PyAudio", Devices)
    monkeypatch.setattr("stacy.harness.audio.transport.sys", SimpleNamespace(platform="linux"))

    async def scenario():
        async with open_audio_transport(Settings(tmp_path, tmp_path / "memory")) as session:
            worker = PipelineWorker(
                Pipeline([session.transport.output()]),
                params=PipelineParams(audio_out_sample_rate=24000),
            )
            runner = WorkerRunner(handle_sigint=False, handle_sigterm=False)
            await runner.add_workers(worker)
            await worker.queue_frames([TTSAudioRawFrame(payload, 24000, 1), EndFrame()])
            ticks = 0

            async def conversation():
                nonlocal ticks
                while True:
                    await asyncio.sleep(0.005)
                    ticks += 1

            heartbeat = asyncio.create_task(conversation())
            try:
                await asyncio.wait_for(runner.run(), 3)
            finally:
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)
        assert len(streams) == 1
        assert streams[0].rendered.startswith(payload)
        assert streams[0].closed
        assert ticks >= 3, "Blocking speaker writes must leave the conversation responsive"

    asyncio.run(scenario())
