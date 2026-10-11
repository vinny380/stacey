from __future__ import annotations

import asyncio
import json
import time
from urllib.parse import urlparse

from fastmcp import FastMCP
from pydantic import BaseModel, Field, ValidationError
from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException

from stacy.connections.results import JsonObject
from stacy.integrations.spotify.playback import (
    PlaybackAction,
    PlaybackStatus,
    register_playback_tools,
)
from stacy.integrations.spotify.playback import spotify_uri as spotify_uri


class SoloistEvent(BaseModel):
    type: str
    command: str | None = None
    message: str = "command failed"


class AuthState(BaseModel):
    logged_in: bool
    device_name: str | None = None
    is_active: bool = False


class PlaybackPosition(BaseModel):
    position_ms: int | None = None
    timestamp_ms: int | None = None
    speed: float = 0


class PlaybackState(BaseModel):
    status: str = Field(min_length=1)
    is_active: bool = False
    item: JsonObject | None = None
    position: PlaybackPosition | None = None


class SoloistReply(BaseModel):
    auth: AuthState
    result: JsonObject


class Soloist:
    """Local Spotify playback through the official Soloist WebSocket protocol."""

    def __init__(self, url: str = "ws://127.0.0.1:9090"):
        parsed = urlparse(url)
        if (
            parsed.scheme not in {"ws", "wss"}
            or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            or parsed.port is None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or parsed.username
        ):
            raise ValueError("SOLOIST_WS_URL must be a loopback WebSocket URL with a port.")
        self.url = url

    async def exchange(self, command: str, **fields) -> SoloistReply:
        async def event(socket, expected):
            while True:
                data = json.loads(await socket.recv())
                parsed = SoloistEvent.model_validate(data)
                if parsed.type == "error":
                    raise ValueError("Soloist: " + parsed.message)
                if parsed.type == expected and (
                    expected != "command_result" or parsed.command == command
                ):
                    return data

        try:
            async with asyncio.timeout(10):
                async with connect(self.url, open_timeout=5, close_timeout=1, proxy=None) as socket:
                    auth = AuthState.model_validate(await event(socket, "auth_state"))
                    if command == "get_auth_state":
                        return SoloistReply(auth=auth, result={})
                    if not auth.logged_in:
                        raise ValueError(
                            "Select Stacy in your Spotify app's device picker to pair."
                        )
                    # Consume the unsolicited initial snapshot before requesting
                    # state, so the reply reflects our query rather than an old event.
                    await event(socket, "playback_state")
                    await socket.send(json.dumps({"type": "command", "command": command, **fields}))
                    expected = "playback_state" if command == "get_state" else "command_result"
                    return SoloistReply(auth=auth, result=await event(socket, expected))
        except ValidationError:
            raise ValueError("Soloist returned an invalid event.") from None
        except (OSError, TimeoutError, WebSocketException) as error:
            raise ValueError(
                "Soloist is not reachable. Start Soloist with its WebSocket API."
            ) from error

    async def status(self) -> PlaybackStatus:
        reply = await self.exchange("get_state")
        try:
            state = PlaybackState.model_validate(reply.result)
        except ValidationError:
            raise ValueError("Soloist returned invalid playback state.") from None
        item = state.item or {}
        decorations = item.get("decorations") or {}
        position = state.position
        progress = position.position_ms if position else None
        if (
            position
            and progress is not None
            and position.timestamp_ms is not None
            and state.status == "playing"
        ):
            progress += max(0, time.time() * 1000 - position.timestamp_ms) * position.speed
            duration = (decorations.get("playback") or {}).get("duration_ms")
            if duration is not None:
                progress = min(progress, duration)
            progress = int(progress)
        return {
            "playback_available": state.status != "idle",
            "is_playing": state.status == "playing",
            "device": {
                "id": "soloist",
                "name": reply.auth.device_name,
                "is_active": state.is_active,
            },
            "item": {
                "name": (decorations.get("identity") or {}).get("name"),
                "uri": item["uri"],
                "artists": [
                    creator["entity"]["decorations"]["identity"]["name"]
                    for creator in decorations.get("creators", [])
                ],
            }
            if item.get("uri")
            else None,
            "progress_ms": progress,
        }

    async def control(self, action: PlaybackAction, uri: str | None, device_id: str | None) -> dict:
        if device_id not in {None, "soloist"}:
            raise ValueError("Soloist can play only on its own device.")
        fields = {"uri": uri} if uri else {}
        command = {"next": "skip_next", "previous": "skip_prev"}.get(action, action)
        reply = await self.exchange(command, **fields)
        return {"accepted": True, "command": reply.result["command"], "verified": False}

    def register(self, server: FastMCP) -> None:
        @server.tool
        async def spotify_connect() -> dict:
            """Check Soloist pairing. Select Stacy in the Spotify device picker to connect."""
            auth = (await self.exchange("get_auth_state")).auth
            return {
                "backend": "soloist",
                "connected": auth.logged_in,
                "device_name": auth.device_name,
                "instructions": "Select this device in Spotify on the same local network.",
            }

        @server.tool
        async def spotify_devices() -> dict:
            """Show this Soloist device only. Soloist cannot control other Spotify devices."""
            auth = (await self.exchange("get_auth_state")).auth
            return {
                "devices": [
                    {
                        "id": "soloist",
                        "name": auth.device_name,
                        "is_active": auth.is_active,
                    }
                ],
                "connected": auth.logged_in,
            }

        register_playback_tools(
            server,
            self,
            description=(
                "Control this Soloist player. Play accepts a Spotify URI/share link or resumes. "
                "Soloist has no catalog search; obtain real links from web_search or the user. "
                "Verify commands with spotify_status."
            ),
        )
