from __future__ import annotations

import asyncio
import logging
import math
import random
import shutil
import struct
import sys
import wave
from pathlib import Path

from stacy.alarms.service import Alarm

logger = logging.getLogger(__name__)


def make_typing(path: Path) -> Path:
    """A quiet, deterministic keyboard loop, synthesized locally at 24 kHz."""
    if path.exists():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    rate, length = 24000, 4
    values = [0.0] * (rate * length)
    rng = random.Random(42)
    position = 0.08
    while position < length - 0.08:
        start = int(position * rate)
        amplitude = rng.uniform(1600, 2600)
        for i in range(int(rate * 0.027)):
            t = i / rate
            click = rng.uniform(-1, 1) * math.exp(-t * 180)
            body = math.sin(2 * math.pi * 720 * t) * math.exp(-t * 260)
            values[start + i] += amplitude * (0.65 * click + 0.35 * body)
        position += rng.uniform(0.095, 0.25)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(b"".join(struct.pack("<h", int(v)) for v in values))
    return path


def make_tone(path: Path) -> Path:
    """Generate an offline alarm tone without adding an audio synthesis dependency."""
    if path.exists():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    rate = 24000
    samples = bytearray()
    for i in range(rate * 2):
        t = i / rate
        audible = (t % 0.5) < 0.3
        envelope = min(1.0, (t % 0.5) * 100, max(0.0, (0.3 - t % 0.5) * 100))
        value = int(10000 * envelope * math.sin(2 * math.pi * 880 * t)) if audible else 0
        samples.extend(struct.pack("<h", value))
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(samples)
    return path


class Ringer:
    def __init__(self, sound: Path):
        player = "afplay" if sys.platform == "darwin" else "aplay"
        self.player = shutil.which(player)
        if not self.player:
            raise RuntimeError(f"Local alarm playback requires {player} on this computer")
        self.sound = make_tone(sound)
        self.active: set[str] = set()
        self.task: asyncio.Task | None = None

    async def start(self, alarm: Alarm) -> None:
        logger.warning("Ringing %s (%s): %s", alarm["kind"], alarm["id"], alarm["label"])
        self.active.add(alarm["id"])
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self._play())

    async def stop(self, alarm_id: str) -> None:
        self.active.discard(alarm_id)
        if not self.active:
            await self.close()

    async def close(self) -> None:
        self.active.clear()
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
            self.task = None

    async def _play(self) -> None:
        while self.active:
            process = await asyncio.create_subprocess_exec(self.player, str(self.sound))
            try:
                code = await process.wait()
                if code:
                    logger.error("Alarm audio player exited with status %s", code)
            finally:
                if process.returncode is None:
                    process.terminate()
                    await process.wait()
            await asyncio.sleep(0.5)
