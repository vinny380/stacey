"""Opt-in live Realtime/MCP memory check; never touches personal memory or audio devices.

Run: uv run --all-extras python scripts/check_memory.py
On macOS, add --audio to send generated PCM speech for the initial preferences.
Uses the key in .env and incurs normal Realtime API charges.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import socket
import tempfile
import wave
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import uvicorn
from loguru import logger
from mcp.client.session_group import StreamableHttpParameters
from pipecat.frames.frames import InputAudioRawFrame, LLMFullResponseEndFrame, TTSAudioRawFrame
from pipecat.processors.frame_processor import FrameProcessor
from pipecat.services.mcp_service import MCPClient
from pipecat.services.openai.realtime.events import (
    ConversationItem,
    ConversationItemCreateEvent,
    ItemContent,
    ResponseCreateEvent,
)
from pipecat.services.openai.realtime.llm import OpenAIRealtimeLLMService
from pipecat.workers.runner import WorkerRunner

from stacy.alarms import AlarmService
from stacy.config import Settings
from stacy.memory import MemoryRepo
from stacy.tools import create_tools
from stacy.tracing import VoiceTrace
from stacy.voice import build_voice_pipeline, create_voice_worker, openai_key


def entries(trace):
    return [json.loads(line) for line in trace.path.read_text().splitlines()]


async def session(settings, prompts, *, audio=False):
    """New tool server, memory adapter, pipeline and provider connection on every call."""

    async def noop(value):
        pass

    memory = MemoryRepo(settings.memory_dir)
    alarms = AlarmService(settings.data_dir / "state.sqlite3", settings.timezone, noop, noop)
    await alarms.start()
    trace = VoiceTrace(Path.cwd() / ".cache")
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(create_tools(alarms, memory).http_app(path="/mcp"), log_level="warning")
    )
    serving = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        async with asyncio.timeout(10):
            while not server.started:
                if serving.done():
                    await serving
                    raise RuntimeError("Test MCP server stopped before startup")
                await asyncio.sleep(0.05)
        async with MCPClient(
            StreamableHttpParameters(url=f"http://127.0.0.1:{port}/mcp", timeout=60)
        ) as mcp:
            last_end = 0.0
            audio_bytes = 0

            class Passthrough(FrameProcessor):
                async def process_frame(self, frame, direction):
                    await super().process_frame(frame, direction)
                    await self.push_frame(frame, direction)

            class Output(Passthrough):
                async def process_frame(self, frame, direction):
                    nonlocal last_end, audio_bytes
                    if isinstance(frame, LLMFullResponseEndFrame):
                        last_end = asyncio.get_running_loop().time()
                    if isinstance(frame, TTSAudioRawFrame):
                        audio_bytes += len(frame.audio)
                    await super().process_frame(frame, direction)

            incoming, outgoing = Passthrough(), Output()
            pipeline = build_voice_pipeline(
                settings,
                openai_key(),
                await mcp.tools(),
                SimpleNamespace(input=lambda: incoming, output=lambda: outgoing),
                trace=trace,
            )
            llm = next(p for p in pipeline.processors if isinstance(p, OpenAIRealtimeLLMService))
            worker = create_voice_worker(pipeline, trace=trace)
            runner = WorkerRunner(handle_sigint=False, handle_sigterm=False)
            await runner.add_workers(worker)
            running = asyncio.create_task(runner.run())

            async def settled(since):
                async with asyncio.timeout(60):
                    while True:
                        if running.done():
                            await running
                            raise RuntimeError("Voice worker stopped during a turn")
                        rows = entries(trace)
                        failures = [
                            r
                            for r in rows
                            if r["event"]
                            in {
                                "pipeline_error",
                                "tool_reported_error",
                                "function_call_failed",
                            }
                        ]
                        if failures:
                            raise RuntimeError(failures)
                        started = {
                            r["tool_call_id"] for r in rows if r["event"] == "function_call_started"
                        }
                        finished = {
                            r["tool_call_id"]
                            for r in rows
                            if r["event"] == "function_call_completed"
                        }
                        assistant_times = [
                            r["time"] for r in rows if r["event"] == "assistant_transcript"
                        ]
                        tool_times = [
                            r["time"] for r in rows if r["event"] == "function_call_completed"
                        ]
                        if (
                            last_end > since
                            and started <= finished
                            and assistant_times
                            and (not tool_times or max(assistant_times) > max(tool_times))
                            and asyncio.get_running_loop().time() - last_end > 2
                        ):
                            return
                        await asyncio.sleep(0.1)

            replies = []
            try:
                await settled(0)  # Startup greeting also proves initial context is ready.
                for prompt in prompts:
                    offset = len(entries(trace))
                    since = asyncio.get_running_loop().time()
                    if audio:
                        trace.record("test_user_audio", text=prompt)
                        await speak_to_pipeline(worker, prompt, settings.data_dir)
                    else:
                        trace.record("test_user_text", text=prompt)
                        await llm.send_client_event(
                            ConversationItemCreateEvent(
                                item=ConversationItem(
                                    type="message",
                                    role="user",
                                    content=[ItemContent(type="input_text", text=prompt)],
                                )
                            )
                        )
                        await llm.send_client_event(ResponseCreateEvent())
                    await settled(since)
                    rows = entries(trace)[offset:]
                    texts = {
                        r["item_id"]: r["text"]
                        for r in rows
                        if r["event"] == "assistant_transcript"
                    }
                    reply = " ".join(texts.values())
                    if len(texts) > 1:
                        print(
                            "STYLE NOTE: assistant included a preamble before its answer.",
                            flush=True,
                        )
                    replies.append(reply)
                    print("USER:", prompt, flush=True)
                    print("STACY:", reply, flush=True)
                    print(
                        "TOOLS:",
                        [r["function_name"] for r in rows if r["event"] == "function_call_started"],
                        flush=True,
                    )
                    assert not re.search(
                        r"\bSaved[.!]|I've saved|memory updated|I'll save|tuck that away",
                        reply,
                        re.I,
                    ), "Routine memory maintenance was narrated"
                assert audio_bytes > 0, "No generated speech received"
                await worker.stop_when_done()
                await asyncio.wait_for(running, 5)
            finally:
                if not running.done():
                    await runner.cancel()
                    await running
            print("TRACE:", trace.path, flush=True)
            return replies, entries(trace)
    finally:
        server.should_exit = True
        await serving
        listener.close()
        alarms.close()


async def speak_to_pipeline(worker, text, directory):
    """macOS speech synthesis goes into the production audio frames, never speakers."""
    aiff, wav = directory / "request.aiff", directory / "request.wav"
    for command in (
        ["say", "-r", "185", "-o", str(aiff), text],
        ["afconvert", "-f", "WAVE", "-d", "LEI16@24000", "-c", "1", str(aiff), str(wav)],
    ):
        process = await asyncio.create_subprocess_exec(*command)
        if await process.wait():
            raise RuntimeError("macOS test speech generation failed")
    with wave.open(str(wav)) as source:
        assert source.getframerate() == 24000 and source.getnchannels() == 1
        assert source.getsampwidth() == 2
        pcm = source.readframes(source.getnframes())
    pcm = b"\0" * 19200 + pcm + b"\0" * 96000
    for offset in range(0, len(pcm), 960):
        await worker.queue_frame(InputAudioRawFrame(pcm[offset : offset + 960], 24000, 1))
        await asyncio.sleep(0.02)


def notes(memory):
    paths = memory._git("ls-files", "--", "*.md").splitlines()
    return "\n".join(memory.read(path)["content"] for path in paths)


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audio", action="store_true", help="Use macOS generated PCM speech")
    args = parser.parse_args()
    logger.remove()
    logger.add(lambda message: print(str(message).strip()), level="WARNING")
    loaded = Settings.load()
    with tempfile.TemporaryDirectory(prefix="stacy-memory-check-") as directory:
        settings = replace(loaded, data_dir=Path(directory), memory_dir=Path(directory) / "memory")
        _, initial_rows = await session(
            settings,
            [
                "For coding, my favourite music is instrumental jazz without vocals. "
                "Also, I prefer your answers short and to the point.",
            ],
            audio=args.audio,
        )
        if args.audio:
            assert any(
                r["event"] == "user_transcript" and "jazz" in r["text"].lower()
                for r in initial_rows
            ), "Audio preference wasn't transcribed"
        memory = MemoryRepo(settings.memory_dir)
        assert "jazz" in notes(memory).lower(), "Natural preference wasn't saved"
        assert re.search(r"short|concise|brief", notes(memory), re.I), (
            "Second preference wasn't saved"
        )
        assert "[[" in memory.read()["content"], "Topic note wasn't indexed"
        replies, rows = await session(
            settings,
            [
                "What kind of music would suit me for a coding session? Don't start playback.",
                "Actually, I've changed my mind. For coding I now prefer ambient electronic music "
                "without vocals, instead of jazz. Keep your replies short, though.",
            ],
        )
        assert "jazz" in replies[0].lower(), "Preference wasn't recalled after restart"
        assert any(r.get("function_name") in {"read_memory", "search_memory"} for r in rows)
        corrected = notes(memory).lower()
        assert "ambient" in corrected and "electronic" in corrected, "Correction wasn't saved"
        assert "jazz" not in corrected, "Correction left the old preference in current notes"
        assert re.search(r"short|concise|brief", corrected), "Correction lost another preference"
        replies, _ = await session(
            settings,
            [
                "What music do I prefer while coding, and how do I like you to answer?",
            ],
        )
        assert "ambient" in replies[0].lower(), "Corrected preference wasn't recalled"
        assert "jazz" not in replies[0].lower(), "Stale preference was used"
        assert re.search(r"short|concise|brief", replies[0], re.I)
        assert not memory._git("status", "--porcelain").strip(), "Uncommitted memory left behind"
        print("PASS: automatic save, indexed retrieval, restart, correction, preservation, speech.")
        print("Git history:", memory._git("log", "--oneline").strip())


if __name__ == "__main__":
    asyncio.run(main())
