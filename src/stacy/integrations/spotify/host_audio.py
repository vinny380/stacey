from __future__ import annotations

import asyncio
import shutil
import sys


async def run_host_bridge() -> None:
    """Development bridge: Docker PulseAudio PCM -> Mac speakers + Bonjour pairing."""
    if sys.platform != "darwin":
        raise ValueError("soloist-host is for macOS Docker; Linux uses its native audio output")
    import pyaudio

    dns_sd = shutil.which("dns-sd")
    if not dns_sd:
        raise ValueError("macOS dns-sd is required for Spotify device discovery")
    audio = pyaudio.PyAudio()
    stream = None
    writer = None
    advertisement = None
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", 4714)
        stream = audio.open(
            format=pyaudio.paInt16, channels=2, rate=48000, output=True, frames_per_buffer=1024
        )
        advertisement = await asyncio.create_subprocess_exec(
            dns_sd,
            "-R",
            "Stacy",
            "_spotify-connect._tcp",
            "local.",
            "24879",
            "CPath=/zc",
            "VERSION=1.0",
            "Stack=SP",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        print("Soloist audio bridge active. Select Stacy in Spotify's device picker.", flush=True)
        while True:
            chunk = await reader.readexactly(4096)
            if advertisement.returncode is not None:
                raise RuntimeError("Spotify Bonjour advertisement stopped")
            # Keep the voice event loop responsive while CoreAudio clocks music.
            write_task = asyncio.create_task(
                asyncio.to_thread(stream.write, chunk, exception_on_underflow=False)
            )
            try:
                await asyncio.shield(write_task)
            except asyncio.CancelledError:
                # Complete the bounded write before closing its native audio stream.
                await write_task
                raise
    except asyncio.IncompleteReadError as error:
        raise RuntimeError("Soloist audio connection closed; restart its container") from error
    finally:
        if advertisement and advertisement.returncode is None:
            advertisement.terminate()
            await advertisement.wait()
        if writer:
            writer.close()
            await writer.wait_closed()
        if stream:
            stream.stop_stream()
            stream.close()
        audio.terminate()
