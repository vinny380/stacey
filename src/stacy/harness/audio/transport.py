"""Own audio transport selection and native capture cleanup."""

from __future__ import annotations

import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from stacy.harness.audio import AudioSession
from stacy.runtime.settings import Settings

if TYPE_CHECKING:
    from stacy.harness.feedback import WorkSoundMixer
    from stacy.harness.tracing import VoiceTrace


@asynccontextmanager
async def open_audio_transport(
    settings: Settings,
    *,
    feedback: WorkSoundMixer | None = None,
    trace: VoiceTrace | None = None,
) -> AsyncIterator[AudioSession]:
    from loguru import logger
    from pipecat.transports.local.audio import LocalAudioTransport, LocalAudioTransportParams

    if settings.audio_mode == "speaker" and sys.platform == "darwin":
        from stacy.harness.audio.macos import MacAudioTransport

        try:
            native = await MacAudioTransport.open(settings.data_dir, mixer=feedback)
        except (OSError, RuntimeError, TimeoutError) as error:
            logger.warning(
                f"Native echo cancellation unavailable; using speaker turn taking: {error}"
            )
            if trace is not None:
                trace.record("audio_fallback", reason=str(error))
        else:
            try:
                yield AudioSession(native, native.echo_cancelled)
            finally:
                await native.close()
            return
    params = LocalAudioTransportParams(
        audio_in_enabled=True,
        audio_out_enabled=True,
        audio_out_mixer=feedback,
    )
    transport_type = LocalAudioTransport
    if sys.platform == "linux":
        from stacy.harness.audio.linux import LinuxAudioTransport

        transport_type = LinuxAudioTransport
    yield AudioSession(transport_type(params))
