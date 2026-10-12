"""Pi hardware regression: generated silence must play at the correct speed.

Run with the voice extras and configured Linux audio. No microphone, account
credentials or external service is used. --baseline exercises Pipecat's
unmodified output transport to reproduce the undersized-buffer failure.
"""

import argparse
import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path


async def render_output(transport):
    from pipecat.frames.frames import EndFrame, TTSAudioRawFrame
    from pipecat.pipeline.pipeline import Pipeline
    from pipecat.pipeline.worker import PipelineParams, PipelineWorker
    from pipecat.workers.runner import WorkerRunner
    transport._params.audio_out_end_silence_secs = 0
    worker = PipelineWorker(
        Pipeline([transport.output()]), params=PipelineParams(audio_out_sample_rate=24000)
    )
    runner = WorkerRunner(handle_sigint=False, handle_sigterm=False)
    await runner.add_workers(worker)
    await worker.queue_frames([TTSAudioRawFrame(bytes(230400), 24000, 1), EndFrame()])
    started = time.monotonic()
    await asyncio.wait_for(runner.run(), 10)
    return time.monotonic() - started


async def check(directory, baseline):
    from loguru import logger
    from pipecat.transports.local.audio import LocalAudioTransport, LocalAudioTransportParams

    from stacy.alarms.audio import make_typing
    from stacy.harness.audio.transport import open_audio_transport
    from stacy.harness.feedback import WorkSoundMixer
    from stacy.runtime.settings import Settings

    logger.remove()
    mixer = WorkSoundMixer(make_typing(directory / "typing.wav"))
    if baseline:
        return await render_output(
            LocalAudioTransport(
                LocalAudioTransportParams(audio_out_enabled=True, audio_out_mixer=mixer)
            )
        )
    async with open_audio_transport(
        Settings(directory, directory / "memory"), feedback=mixer
    ) as session:
        return await render_output(session.transport)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", action="store_true")
    args = parser.parse_args()
    if sys.platform != "linux":
        parser.error("Run this hardware check on Linux with the voice extras installed.")
    with tempfile.TemporaryDirectory(prefix="stacy-audio-check-") as name:
        directory = Path(name)
        log = directory / "driver.log"
        saved_stderr = os.dup(2)
        fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.dup2(fd, 2)
            elapsed = asyncio.run(check(directory, args.baseline))
        finally:
            os.dup2(saved_stderr, 2)
            os.close(saved_stderr)
            os.close(fd)
        underruns = log.read_text().count("underrun occurred")
    passed = 4.3 <= elapsed <= 8 and underruns == 0
    print(
        json.dumps(
            {
                "passed": passed,
                "pcm_seconds": 4.8,
                "elapsed_seconds": round(elapsed, 3),
                "underruns": underruns,
            }
        )
    )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
