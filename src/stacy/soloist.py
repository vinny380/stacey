from __future__ import annotations

import asyncio
import json
import time
from typing import Literal
from urllib.parse import urlparse

from fastmcp import FastMCP
from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException


def spotify_uri(value: str) -> str:
    """Accept a Spotify URI or a real Spotify share link, never invent an ID."""
    parsed = urlparse(value)
    if parsed.scheme == "https" and parsed.hostname == "open.spotify.com":
        parts = parsed.path.strip("/").split("/")
        if parts and parts[0].startswith("intl-"):
            parts = parts[1:]
        value = "spotify:" + ":".join(parts)
    parts = value.split(":")
    if (
        len(parts) != 3
        or parts[0] != "spotify"
        or parts[1] not in {"track", "album", "playlist", "episode"}
        or len(parts[2]) != 22
        or not parts[2].isascii()
        or not parts[2].isalnum()
    ):
        raise ValueError("Use a Spotify track, album, playlist or episode URI/share link.")
    return value


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

    async def exchange(self, command: str, **fields) -> tuple[dict, dict]:
        async def event(socket, expected):
            while True:
                data = json.loads(await socket.recv())
                if not isinstance(data, dict):
                    raise ValueError("Soloist returned an invalid event.")
                if data.get("type") == "error":
                    raise ValueError("Soloist: " + data.get("message", "command failed"))
                if data.get("type") == expected:
                    if expected != "command_result" or data.get("command") == command:
                        return data

        try:
            async with asyncio.timeout(10):
                async with connect(self.url, open_timeout=5, close_timeout=1, proxy=None) as socket:
                    auth = await event(socket, "auth_state")
                    if command == "get_auth_state":
                        return auth, auth
                    if not auth.get("logged_in"):
                        raise ValueError(
                            "Select Stacy in your Spotify app's device picker to pair."
                        )
                    # Consume the unsolicited initial snapshot before requesting
                    # state, so the reply reflects our query rather than an old event.
                    await event(socket, "playback_state")
                    await socket.send(json.dumps({"type": "command", "command": command, **fields}))
                    expected = "playback_state" if command == "get_state" else "command_result"
                    return auth, await event(socket, expected)
        except (OSError, TimeoutError, WebSocketException) as error:
            raise ValueError(
                "Soloist is not reachable. Start Soloist with its WebSocket API."
            ) from error

    async def status(self) -> dict:
        auth, state = await self.exchange("get_state")
        item = state.get("item") or {}
        decorations = item.get("decorations") or {}
        position = state.get("position") or {}
        progress = position.get("position_ms")
        if (
            progress is not None
            and position.get("timestamp_ms")
            and state.get("status") == "playing"
        ):
            progress += max(0, time.time() * 1000 - position["timestamp_ms"]) * position.get(
                "speed", 0
            )
            duration = (decorations.get("playback") or {}).get("duration_ms")
            if duration:
                progress = min(progress, duration)
            progress = int(progress)
        return {
            "playback_available": state.get("status") != "idle",
            "is_playing": state.get("status") == "playing",
            "status": state.get("status"),
            "device": {
                "id": "soloist",
                "name": auth.get("device_name"),
                "is_active": state.get("is_active", False),
            },
            "item": {
                "name": (decorations.get("identity") or {}).get("name"),
                "uri": item.get("uri"),
                "artists": [
                    creator.get("entity", {}).get("decorations", {}).get("identity", {}).get("name")
                    for creator in decorations.get("creators", [])
                ],
            }
            if item.get("uri")
            else None,
            "progress_ms": progress,
        }

    def register(self, server: FastMCP) -> None:
        @server.tool
        async def spotify_connect() -> dict:
            """Check Soloist pairing. Select Stacy in the Spotify device picker to connect."""
            auth, _ = await self.exchange("get_auth_state")
            return {
                "backend": "soloist",
                "connected": auth.get("logged_in", False),
                "device_name": auth.get("device_name"),
                "instructions": "Select this device in Spotify on the same local network.",
            }

        @server.tool
        async def spotify_devices() -> dict:
            """Show this Soloist device only. Soloist cannot control other Spotify devices."""
            auth, _ = await self.exchange("get_auth_state")
            return {
                "devices": [
                    {
                        "id": "soloist",
                        "name": auth.get("device_name"),
                        "is_active": auth.get("is_active", False),
                    }
                ],
                "connected": auth.get("logged_in", False),
            }

        @server.tool
        async def spotify_status() -> dict:
            """Read actual Soloist playback state, track, device and position to verify commands."""
            return await self.status()

        @server.tool
        async def spotify_control(
            action: Literal["play", "pause", "next", "previous"],
            uri: str | None = None,
            device_id: str | None = None,
        ) -> dict:
            """Control this Soloist player. Play accepts a Spotify URI or share link, or resumes.

            Soloist has no catalog search. Obtain actual Spotify links from web_search,
            memory or the user. After a command, verify its effect with spotify_status.
            """
            if device_id not in {None, "soloist"}:
                raise ValueError("Soloist can play only on its own device.")
            if uri and action != "play":
                raise ValueError("A Spotify URI can only be used with play.")
            fields = {"uri": spotify_uri(uri)} if uri else {}
            command = {"next": "skip_next", "previous": "skip_prev"}.get(action, action)
            _, result = await self.exchange(command, **fields)
            return {"accepted": True, "command": result["command"], "verified": False}
