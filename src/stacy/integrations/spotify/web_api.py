from __future__ import annotations

import asyncio
import json
import secrets
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx
from authlib.integrations.httpx_client import AsyncOAuth2Client
from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import PlainTextResponse, RedirectResponse

from stacy.integrations.spotify.playback import (
    PlaybackAction,
    PlaybackStatus,
    register_playback_tools,
)
from stacy.runtime.storage import atomic_write

AUTHORIZE = "https://accounts.spotify.com/authorize"
TOKEN = "https://accounts.spotify.com/api/token"
API = "https://api.spotify.com/v1"
SCOPE = "user-read-playback-state user-modify-playback-state"


class Spotify:
    """One-user PKCE connection using Authlib and Spotify's Web API."""

    def __init__(self, client_id: str, redirect_uri: str, token_file: Path):
        self.client_id = client_id
        self.redirect_uri = redirect_uri
        self.token_file = token_file
        self.pending: dict[str, tuple[str, float]] = {}
        self.lock = asyncio.Lock()

    def oauth(self, token: dict | None = None) -> AsyncOAuth2Client:
        return AsyncOAuth2Client(
            client_id=self.client_id,
            redirect_uri=self.redirect_uri,
            scope=SCOPE,
            token=token,
            code_challenge_method="S256",
            token_endpoint_auth_method="none",
            timeout=20,
        )

    def save(self, token: dict) -> None:
        atomic_write(self.token_file, json.dumps(token))

    async def request(self, method: str, path: str, **kwargs):
        async with self.lock:
            if not self.token_file.exists():
                raise ValueError("Connect Spotify first using spotify_connect")
            token = json.loads(self.token_file.read_text())
            async with self.oauth(token) as oauth:
                if token.get("expires_at", 0) <= time.time() + 60:
                    refreshed = await oauth.refresh_token(
                        TOKEN, refresh_token=token["refresh_token"]
                    )
                    token = {**token, **refreshed}
                    self.save(token)
            async with httpx.AsyncClient(timeout=20) as client:
                response = await client.request(
                    method,
                    API + path,
                    headers={"Authorization": "Bearer " + token["access_token"]},
                    **kwargs,
                )
                if response.is_error:
                    try:
                        error = response.json().get("error", {})
                        message = error.get("message", "") if isinstance(error, dict) else error
                    except (ValueError, AttributeError):
                        message = ""
                    hint = {
                        401: "Reconnect Spotify using spotify_connect.",
                        403: "Check Spotify Premium, app user access, and playback permissions.",
                        404: "Open Spotify on your device and select it for playback.",
                        429: "Spotify is limiting requests; try again later.",
                    }.get(response.status_code, "Try again later.")
                    raise ValueError(f"Spotify {response.status_code}: {message}. {hint}")
                if response.content:
                    return response.json()
                return {} if method == "GET" else {"success": True}

    def register(self, server: FastMCP) -> None:
        redirect = urlparse(self.redirect_uri)
        if (
            redirect.scheme != "http"
            or redirect.hostname != "127.0.0.1"
            or redirect.path != "/callback"
            or redirect.port is None
            or redirect.query
            or redirect.fragment
            or redirect.username
        ):
            raise ValueError("Spotify redirect must use http://127.0.0.1:<port>/callback")
        connect_url = f"{redirect.scheme}://{redirect.netloc}/spotify/connect"

        @server.custom_route("/spotify/connect", methods=["GET"])
        async def connect(request: Request):
            verifier = secrets.token_urlsafe(64)
            async with self.oauth() as client:
                uri, state = client.create_authorization_url(AUTHORIZE, code_verifier=verifier)
            self.pending = {
                key: value
                for key, value in self.pending.items()
                if value[1] > time.monotonic() - 600
            }
            self.pending[state] = (verifier, time.monotonic())
            return RedirectResponse(uri)

        @server.custom_route("/callback", methods=["GET"])
        async def callback(request: Request):
            pending = self.pending.pop(request.query_params.get("state", ""), None)
            if not pending or pending[1] < time.monotonic() - 600:
                return PlainTextResponse(
                    "Authorization expired or invalid. Start again.", status_code=400
                )
            if request.query_params.get("error") or not request.query_params.get("code"):
                return PlainTextResponse(
                    "Spotify authorization was not completed.", status_code=400
                )
            async with self.lock:
                async with self.oauth() as client:
                    token = await client.fetch_token(
                        TOKEN,
                        code=request.query_params["code"],
                        code_verifier=pending[0],
                    )
                self.save(token)
            return PlainTextResponse("Spotify connected. You can close this tab and talk to Stacy.")

        @server.tool
        async def spotify_connect() -> dict:
            """Return the local link for one-time Spotify account authorization."""
            return {"url": connect_url, "connected": self.token_file.exists()}

        @server.tool
        async def spotify_devices() -> dict:
            """List the user's Spotify devices. Playback control requires Spotify Premium."""
            return await self.request("GET", "/me/player/devices")

        @server.tool
        async def spotify_search(query: str) -> dict:
            """Search Spotify for tracks and return their names and Spotify URIs."""
            return await self.request(
                "GET", "/search", params={"q": query, "type": "track", "limit": 5}
            )

        register_playback_tools(
            server,
            self,
            description=(
                "Control Spotify on an existing device, defaulting to the active device. "
                "Play accepts a Spotify URI/share link or resumes. "
                "Verify commands with spotify_status."
            ),
        )

    async def status(self) -> PlaybackStatus:
        playback = await self.request("GET", "/me/player")
        item = playback.get("item") or {}
        device = playback.get("device")
        return {
            "playback_available": bool(playback),
            "is_playing": playback.get("is_playing", False),
            "device": {
                "id": device["id"],
                "name": device.get("name"),
                "is_active": device.get("is_active", False),
            }
            if device
            else None,
            "item": {
                "name": item.get("name"),
                "uri": item.get("uri"),
                "artists": [artist["name"] for artist in item.get("artists", [])],
            }
            if item
            else None,
            "progress_ms": playback.get("progress_ms"),
        }

    async def control(self, action: PlaybackAction, uri: str | None, device_id: str | None) -> dict:
        params = {"device_id": device_id} if device_id else {}
        method = "PUT" if action in {"play", "pause"} else "POST"
        body = {}
        if action == "play" and uri:
            body = (
                {"uris": [uri]}
                if uri.startswith(("spotify:track:", "spotify:episode:"))
                else {"context_uri": uri}
            )
        kwargs = {"params": params}
        if action == "play":
            kwargs["json"] = body
        return await self.request(method, "/me/player/" + action, **kwargs)
