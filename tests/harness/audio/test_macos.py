import asyncio
import base64
import json
import sys

import pytest

pytest.importorskip("pipecat.services.openai.realtime.llm")

from stacy.harness.audio.macos import MacAudioTransport, NativeAudioBridge
from stacy.harness.context import ConversationContext
from stacy.harness.voice import build_voice_pipeline, create_voice_worker
from stacy.runtime.settings import Settings


def audio_helper(tmp_path, *, verified=True):
    """A device that holds playback until clear, then emits a stale completion."""
    log = tmp_path / "commands.jsonl"
    helper = tmp_path / "audio-helper"
    helper.write_text(
        f"#!{sys.executable}\n"
        "import base64,json,sys,threading,time\n"
        "lock=threading.Lock()\n"
        "def send(event):\n"
        "    with lock: print(json.dumps(event),flush=True)\n"
        f"send({{'type':'ready','echo_cancelled':{verified!r},'sample_rate':24000}})\n"
        "def capture():\n"
        "    while True:\n"
        "        send({'type':'audio','pcm':base64.b64encode(b'\\x01\\x00'*480).decode()})\n"
        "        time.sleep(.02)\n"
        "threading.Thread(target=capture,daemon=True).start()\n"
        "cleared=False; pending=[]\n"
        "for line in sys.stdin:\n"
        "    message=json.loads(line)\n"
        f"    with open({str(log)!r},'a') as out:\n"
        "        out.write(json.dumps({k:v for k,v in message.items() if k!='pcm'})+'\\n')\n"
        "    if message['type']=='play':\n"
        "        if cleared: send({'type':'played','id':message['id']})\n"
        "        else: pending.append(message['id'])\n"
        "    elif message['type']=='clear':\n"
        "        cleared=True; send({'type':'cleared','id':message['id']})\n"
        "        for identifier in pending: send({'type':'played','id':identifier})\n"
        "        pending=[]\n"
        "    elif message['type']=='stop': break\n"
    )
    helper.chmod(0o700)
    return helper, log


def commands(log):
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


async def eventually(condition):
    async with asyncio.timeout(3):
        while not condition():
            await asyncio.sleep(0.01)


def test_native_bridge_requires_verified_echo_cancellation_and_closes_on_failure(tmp_path):
    helper, _ = audio_helper(tmp_path, verified=False)

    async def scenario():
        bridge = NativeAudioBridge(helper)
        with pytest.raises(RuntimeError, match="did not verify"):
            await bridge.start()
        assert not bridge.echo_cancelled
        assert bridge._process.returncode is not None

    asyncio.run(scenario())


def test_barge_in_clears_actual_device_queue_and_keeps_microphone_open(tmp_path, monkeypatch):
    from pipecat.adapters.schemas.tools_schema import ToolsSchema
    from pipecat.frames.frames import TTSAudioRawFrame, TTSStoppedFrame
    from pipecat.services.openai.realtime.events import (
        ConversationItemInputAudioTranscriptionCompleted,
    )
    from pipecat.workers.runner import WorkerRunner

    helper, log = audio_helper(tmp_path)

    async def scenario():
        bridge = NativeAudioBridge(helper)
        await bridge.start()
        transport = MacAudioTransport(bridge)
        events = []
        pipeline = build_voice_pipeline(
            Settings(tmp_path, tmp_path / "memory"),
            "offline-test",
            ToolsSchema(standard_tools=[]),
            transport,
            context=ConversationContext(),
            echo_cancelled=transport.echo_cancelled,
        )
        llm = pipeline.llm

        async def connect():
            llm._api_session_ready = True

        async def send(event):
            events.append(event)

        monkeypatch.setattr(llm, "_connect", connect)
        monkeypatch.setattr(llm, "_ws_send", send)

        def user_audio_count():
            return sum(event["type"] == "input_audio_buffer.append" for event in events)

        worker = create_voice_worker(pipeline)
        runner = WorkerRunner(handle_sigint=False, handle_sigterm=False)
        await runner.add_workers(worker)
        running = asyncio.create_task(runner.run())
        try:
            await eventually(lambda: user_audio_count() >= 2)
            await llm.push_frame(TTSAudioRawFrame(b"\x32\x00" * 24000, 24000, 1))
            await llm.push_frame(TTSStoppedFrame())
            await eventually(lambda: len(commands(log)) == 3)
            count_during_playback = user_audio_count()
            await eventually(lambda: user_audio_count() > count_during_playback + 2)
            # Only an addressed utterance may interrupt playback; room VAD alone
            # must leave the actual device queue and the pending reply alone.
            await llm.handle_evt_input_audio_transcription_completed(
                ConversationItemInputAudioTranscriptionCompleted(
                    event_id="event_barge",
                    type="conversation.item.input_audio_transcription.completed",
                    item_id="item_barge",
                    content_index=0,
                    transcript="Stacy, stop.",
                )
            )
            await eventually(lambda: any(c["type"] == "clear" for c in commands(log)))
            assert [c["type"] for c in commands(log)[:4]] == ["play", "play", "play", "clear"]
            await asyncio.sleep(0.08)
            count_after_clear = user_audio_count()
            await eventually(lambda: user_audio_count() > count_after_clear + 2)
            assert len([c for c in commands(log) if c["type"] == "play"]) == 3
            # A fresh reply still plays; stale acknowledgements did not satisfy it.
            await llm.push_frame(TTSAudioRawFrame(b"\x64\x00" * 2880, 24000, 1))
            await llm.push_frame(TTSStoppedFrame())
            await eventually(lambda: len([c for c in commands(log) if c["type"] == "play"]) == 6)
            await eventually(lambda: not bridge._pending)
            assert all(
                base64.b64decode(e["audio"]) == b"\x01\x00" * 480
                for e in events
                if e["type"] == "input_audio_buffer.append"
            )
        finally:
            await runner.cancel()
            await running
            await transport.close()
        assert bridge._process.returncode == 0
        assert commands(log)[-1]["type"] == "stop"

    asyncio.run(scenario())


def test_native_playback_end_waits_for_device_acknowledgement(tmp_path):
    helper, log = audio_helper(tmp_path)

    async def scenario():
        bridge = NativeAudioBridge(helper)
        await bridge.start()
        try:
            await bridge.play(b"\x01\x00" * 960)
            draining = asyncio.create_task(bridge.drain())
            await asyncio.sleep(0.04)
            assert not draining.done(), "Generation completion must not precede actual playback"
            await bridge.clear()
            await asyncio.wait_for(draining, 1)
            await bridge.play(b"\x02\x00" * 960)
            await bridge.drain()
        finally:
            await bridge.close()
        assert [c["type"] for c in commands(log)] == ["play", "clear", "play", "stop"]

    asyncio.run(scenario())
