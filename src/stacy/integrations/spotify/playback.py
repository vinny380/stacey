"""Shared playback contract and tool registration for Spotify backends."""

from typing import Literal, Protocol, TypedDict
from urllib.parse import urlparse

from fastmcp import FastMCP

PlaybackAction = Literal["play", "pause", "next", "previous"]


class PlaybackDevice(TypedDict):
    id: str
    name: str | None
    is_active: bool


class PlaybackItem(TypedDict):
    name: str | None
    uri: str
    artists: list[str]


class PlaybackStatus(TypedDict):
    playback_available: bool
    is_playing: bool
    device: PlaybackDevice | None
    item: PlaybackItem | None
    progress_ms: int | None


class Playback(Protocol):
    async def status(self) -> PlaybackStatus: ...

    async def control(
        self, action: PlaybackAction, uri: str | None, device_id: str | None
    ) -> dict: ...


def spotify_uri(value: str) -> str:
    """Accept a real Spotify URI or share link."""
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


def register_playback_tools(server: FastMCP, playback: Playback, *, description: str) -> None:
    @server.tool
    async def spotify_status() -> dict:
        """Read actual playback state, track, device and position to verify commands."""
        return await playback.status()

    @server.tool(description=description)
    async def spotify_control(
        action: PlaybackAction, uri: str | None = None, device_id: str | None = None
    ) -> dict:
        if uri and action != "play":
            raise ValueError("A Spotify URI can only be used with play.")
        return await playback.control(action, spotify_uri(uri) if uri else None, device_id)
