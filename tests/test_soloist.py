import asyncio
import json

import pytest
from fastmcp import Client, FastMCP

pytest.importorskip("websockets")
from stacy import soloist as module  # noqa: E402
from stacy.soloist import Soloist, spotify_uri  # noqa: E402

AUTH = {"type": "auth_state", "logged_in": True, "device_name": "Stacy", "is_active": True}


class Socket:
    def __init__(self, events):
        self.events = iter(events)
        self.sent = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def recv(self):
        return json.dumps(next(self.events))

    async def send(self, data):
        self.sent.append(json.loads(data))


def test_play_ack_does_not_claim_verified_playback(monkeypatch):
    async def scenario():
        socket = Socket(
            [
                AUTH,
                {"type": "playback_state", "status": "paused"},
                {"type": "volume_changed", "volume": 40},
                {"type": "command_result", "command": "play"},
            ]
        )
        monkeypatch.setattr(module, "connect", lambda *args, **kwargs: socket)
        server = FastMCP("test")
        Soloist().register(server)
        async with Client(server) as client:
            result = (
                await client.call_tool(
                    "spotify_control",
                    {
                        "action": "play",
                        "uri": "https://open.spotify.com/track/68oU2YXg4kwPpertTUFrC6?si=test",
                    },
                )
            ).data
        assert result == {"accepted": True, "command": "play", "verified": False}
        assert socket.sent == [
            {"type": "command", "command": "play", "uri": "spotify:track:68oU2YXg4kwPpertTUFrC6"}
        ]

    asyncio.run(scenario())


def test_status_consumes_initial_snapshot_and_reads_fresh_state(monkeypatch):
    async def scenario():
        socket = Socket(
            [
                AUTH,
                {"type": "playback_state", "status": "paused"},
                {
                    "type": "playback_state",
                    "status": "playing",
                    "is_active": True,
                    "item": {
                        "uri": "spotify:track:68oU2YXg4kwPpertTUFrC6",
                        "decorations": {"identity": {"name": "Song"}},
                    },
                    "position": {"position_ms": 1000, "timestamp_ms": 100000, "speed": 1},
                },
            ]
        )
        monkeypatch.setattr(module, "connect", lambda *args, **kwargs: socket)
        monkeypatch.setattr(module.time, "time", lambda: 101)
        result = await Soloist().status()
        assert result["is_playing"] is True
        assert result["progress_ms"] == 2000
        assert result["item"]["name"] == "Song"
        assert socket.sent == [{"type": "command", "command": "get_state"}]

    asyncio.run(scenario())


def test_unpaired_reports_pairing_and_rejects_controls(monkeypatch):
    async def scenario():
        socket = Socket([{**AUTH, "logged_in": False}])
        monkeypatch.setattr(module, "connect", lambda *args, **kwargs: socket)
        server = FastMCP("test")
        Soloist().register(server)
        async with Client(server) as client:
            result = (await client.call_tool("spotify_connect", {})).data
            assert result["connected"] is False
        socket = Socket([{**AUTH, "logged_in": False}])
        with pytest.raises(ValueError, match="device picker"):
            await Soloist().exchange("play")
        assert socket.sent == []

    asyncio.run(scenario())


def test_server_error_is_not_success(monkeypatch):
    socket = Socket(
        [
            AUTH,
            {"type": "playback_state", "status": "paused"},
            {"type": "error", "message": "playback unavailable"},
        ]
    )
    monkeypatch.setattr(module, "connect", lambda *args, **kwargs: socket)
    with pytest.raises(ValueError, match="playback unavailable"):
        asyncio.run(Soloist().exchange("play"))


@pytest.mark.parametrize(
    "value",
    [
        "https://evil.example/track/68oU2YXg4kwPpertTUFrC6",
        "spotify:track:invented",
        "spotify:user:test",
    ],
)
def test_uri_rejects_unverified_or_unsupported_links(value):
    with pytest.raises(ValueError):
        spotify_uri(value)


def test_websocket_control_stays_on_loopback():
    with pytest.raises(ValueError, match="loopback"):
        Soloist("ws://0.0.0.0:9090")
