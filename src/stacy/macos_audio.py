"""Headless macOS capture/render through one native echo-cancelling engine.

The helper owns both hardware streams. Only PCM and playback acknowledgements
cross its local pipes; microphone audio is never written to disk or logs.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import os
import shutil
import tempfile
from collections import OrderedDict
from pathlib import Path

from pipecat.frames.frames import BotStoppedSpeakingFrame, InputAudioRawFrame, InterruptionFrame
from pipecat.processors.frame_processor import FrameDirection
from pipecat.transports.base_input import BaseInputTransport
from pipecat.transports.base_output import BaseOutputTransport
from pipecat.transports.base_transport import BaseTransport, TransportParams


async def native_binary(data_dir: Path) -> Path:
    """Build the bundled helper once per source revision, without an app UI."""
    compiler = shutil.which("clang")
    if not compiler:
        raise RuntimeError("Native speaker audio requires Apple's command line compiler")
    source = Path(__file__).with_name("native") / "voice_processing.m"
    metadata = source.with_name("Info.plist")
    revision = hashlib.sha256(source.read_bytes() + metadata.read_bytes()).hexdigest()[:16]
    directory = data_dir / "audio-runtime"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = directory / f"voice-processing-{revision}"
    if target.is_file():
        return target
    descriptor, temporary = tempfile.mkstemp(prefix="build-", dir=directory)
    os.close(descriptor)
    try:
        process = await asyncio.create_subprocess_exec(
            compiler,
            "-fobjc-arc",
            "-fblocks",
            "-fno-modules",
            "-framework",
            "AVFoundation",
            "-framework",
            "Foundation",
            f"-Wl,-sectcreate,__TEXT,__info_plist,{metadata}",
            str(source),
            "-o",
            temporary,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, error = await asyncio.wait_for(process.communicate(), 45)
        except BaseException:
            if process.returncode is None:
                process.kill()
                await process.wait()
            raise
        if process.returncode:
            raise RuntimeError(
                f"Native audio build failed: {error.decode(errors='replace')[-1500:]}"
            )
        os.chmod(temporary, 0o700)
        os.replace(temporary, target)
        return target
    finally:
        Path(temporary).unlink(missing_ok=True)


class NativeAudioBridge:
    """Bound playback to three 40 ms chunks; interruptions flush the device."""

    def __init__(self, binary: Path):
        self.binary = binary
        self.echo_cancelled = False
        self.on_audio = None
        self.on_error = None
        self._process = None
        self._reader = None
        self._ready = None
        self._commands = asyncio.Lock()
        self._pending = OrderedDict()
        self._clears = {}
        self._next_id = 0
        self._closed = False
        self._failure = None

    async def start(self):
        self._ready = asyncio.get_running_loop().create_future()
        self._process = await asyncio.create_subprocess_exec(
            str(self.binary),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            limit=131072,
        )
        self._reader = asyncio.create_task(self._read(), name="native-audio-capture")
        try:
            await asyncio.wait_for(asyncio.shield(self._ready), 12)
        except BaseException:
            await self.close()
            raise

    async def _read(self):
        try:
            while line := await self._process.stdout.readline():
                message = json.loads(line)
                kind = message["type"]
                if kind == "ready":
                    if (
                        message.get("echo_cancelled") is not True
                        or message.get("sample_rate") != 24000
                    ):
                        raise RuntimeError("Native audio did not verify 24 kHz echo cancellation")
                    self.echo_cancelled = True
                    if not self._ready.done():
                        self._ready.set_result(None)
                elif kind == "audio":
                    if self.echo_cancelled and self.on_audio:
                        pcm = base64.b64decode(message["pcm"], validate=True)
                        if len(pcm) % 2:
                            raise RuntimeError("Invalid native microphone PCM")
                        # Native capture taps batch ~100 ms; give Pipecat 20 ms frames.
                        for offset in range(0, len(pcm), 960):
                            await self.on_audio(pcm[offset : offset + 960])
                elif kind in {"played", "cleared"}:
                    pending = self._pending if kind == "played" else self._clears
                    future = pending.pop(message["id"], None)
                    if future is not None and not future.done():
                        future.set_result(None)
                elif kind == "error":
                    raise RuntimeError(message.get("message", "Native audio failed"))
                else:
                    raise RuntimeError("Unknown native audio event")
            if not self._closed:
                raise RuntimeError("Native audio engine exited unexpectedly")
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._failure = error
            self.echo_cancelled = False
            if not self._ready.done():
                self._ready.set_exception(error)
            for future in [*self._pending.values(), *self._clears.values()]:
                if not future.done():
                    future.set_exception(error)
                    # Playback may have been interrupted; consume unobserved errors.
                    future.exception()
            self._pending.clear()
            self._clears.clear()
            if self.on_error and not self._closed:
                await self.on_error(str(error))

    def _identifier(self):
        self._next_id += 1
        return self._next_id

    async def _send(self, message):
        if self._failure:
            raise self._failure
        if self._closed or self._process is None or self._process.returncode is not None:
            raise RuntimeError("Native audio engine is closed")
        self._process.stdin.write(json.dumps(message).encode() + b"\n")
        await self._process.stdin.drain()

    async def play(self, pcm: bytes):
        if not pcm or len(pcm) % 2 or len(pcm) > 48000:
            raise ValueError("Playback requires at most one second of mono PCM16 at 24 kHz")
        async with self._commands:
            identifier = self._identifier()
            future = asyncio.get_running_loop().create_future()
            self._pending[identifier] = future
            await self._send(
                {"type": "play", "id": identifier, "pcm": base64.b64encode(pcm).decode()}
            )
            oldest = next(iter(self._pending.values())) if len(self._pending) >= 3 else None
        if oldest is not None:
            await asyncio.wait_for(asyncio.shield(oldest), 3)

    async def drain(self):
        for future in list(self._pending.values()):
            await asyncio.wait_for(asyncio.shield(future), 3)

    async def clear(self):
        async with self._commands:
            identifier = self._identifier()
            acknowledgement = asyncio.get_running_loop().create_future()
            self._clears[identifier] = acknowledgement
            # Stale played acknowledgements cannot complete the next reply.
            for future in self._pending.values():
                if not future.done():
                    future.set_result(None)
            self._pending.clear()
            await self._send({"type": "clear", "id": identifier})
        await asyncio.wait_for(asyncio.shield(acknowledgement), 3)

    async def close(self):
        if self._closed:
            return
        self.on_audio = self.on_error = None
        if self._process is not None and self._process.returncode is None:
            with contextlib.suppress(ConnectionError, RuntimeError):
                await self._send({"type": "stop"})
        self._closed = True
        if self._process is not None:
            try:
                await asyncio.wait_for(self._process.wait(), 3)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    self._process.kill()
                await self._process.wait()
        if self._reader is not None:
            self._reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader
        for future in [*self._pending.values(), *self._clears.values()]:
            if not future.done():
                future.cancel()
        self._pending.clear()
        self._clears.clear()


class MacAudioInput(BaseInputTransport):
    def __init__(self, bridge, params):
        super().__init__(params)
        self.bridge = bridge

    async def start(self, frame):
        await super().start(frame)
        await self.set_transport_ready(frame)
        self.bridge.on_audio = self.capture
        self.bridge.on_error = self.audio_failed
        if not self.bridge.echo_cancelled:
            await self.audio_failed("Native echo cancellation was lost before voice started")

    async def capture(self, pcm):
        await self.push_audio_frame(InputAudioRawFrame(pcm, 24000, 1))

    async def audio_failed(self, message):
        await self.push_error(message, force_treat_as_permanent=True)

    async def cleanup(self):
        self.bridge.on_audio = self.bridge.on_error = None
        await super().cleanup()


class MacAudioOutput(BaseOutputTransport):
    def __init__(self, bridge, params):
        super().__init__(params)
        self.bridge = bridge
        self._interrupting = False

    async def start(self, frame):
        await super().start(frame)
        await self.set_transport_ready(frame)

    async def write_audio_frame(self, frame):
        if frame.sample_rate != 24000 or frame.num_channels != 1:
            raise ValueError("Native audio output requires 24 kHz mono")
        await self.bridge.play(frame.audio)
        return True

    async def push_frame(self, frame, direction=FrameDirection.DOWNSTREAM):
        if isinstance(frame, BotStoppedSpeakingFrame) and not self._interrupting:
            await self.bridge.drain()
        await super().push_frame(frame, direction)

    async def process_frame(self, frame, direction):
        if isinstance(frame, InterruptionFrame):
            self._interrupting = True
            try:
                # Cancel Pipecat's writer before flushing native scheduled buffers.
                # During this cancellation BotStoppedSpeaking must not wait for playback.
                await super().process_frame(frame, direction)
                await self.bridge.clear()
            finally:
                self._interrupting = False
        else:
            await super().process_frame(frame, direction)


class MacAudioTransport(BaseTransport):
    def __init__(self, bridge, *, mixer=None):
        super().__init__()
        if not bridge.echo_cancelled:
            raise RuntimeError("Native audio must verify echo cancellation before pipeline setup")
        self.bridge = bridge
        params = TransportParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            audio_out_mixer=mixer,
        )
        self._input = MacAudioInput(bridge, params)
        self._output = MacAudioOutput(bridge, params)

    @property
    def echo_cancelled(self):
        return self.bridge.echo_cancelled

    @classmethod
    async def open(cls, data_dir, *, mixer=None):
        bridge = NativeAudioBridge(await native_binary(data_dir))
        try:
            await bridge.start()
            return cls(bridge, mixer=mixer)
        except BaseException:
            await bridge.close()
            raise

    def input(self):
        return self._input

    def output(self):
        return self._output

    async def close(self):
        await self.bridge.close()
