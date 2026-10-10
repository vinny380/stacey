import asyncio
import hashlib
import json
import time
from base64 import urlsafe_b64encode
from urllib.parse import parse_qs, urlparse

import pytest
from fastmcp import Client, FastMCP

httpx = pytest.importorskip("httpx")
oauth_http = pytest.importorskip("httpx2")
pytest.importorskip("authlib")
from stacy import spotify as spotify_module  # noqa: E402
from stacy.spotify import Spotify  # noqa: E402


def test_pkce_connection_rejects_bad_state_and_replay_and_saves_private_token(
    tmp_path, monkeypatch
):
    async def scenario():
        spotify = Spotify("test-client", "http://127.0.0.1:8765/callback", tmp_path / "token.json")
        server = FastMCP("test")
        spotify.register(server)
        app = server.http_app()
        original_oauth_type = spotify_module.AsyncOAuth2Client
        exchanges = []

        def exchange(request):
            body = parse_qs(request.content.decode())
            exchanges.append(body)
            assert body["client_id"] == ["test-client"]
            assert body["redirect_uri"] == [spotify.redirect_uri]
            assert "client_secret" not in body
            return oauth_http.Response(
                200,
                json={
                    "access_token": "test-access",
                    "refresh_token": "test-refresh",
                    "expires_in": 3600,
                    "token_type": "Bearer",
                },
            )

        def oauth(**kwargs):
            return original_oauth_type(transport=oauth_http.MockTransport(exchange), **kwargs)

        monkeypatch.setattr(spotify_module, "AsyncOAuth2Client", oauth)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8765"
        ) as client:
            assert (await client.get("/callback?code=stolen&state=wrong")).status_code == 400
            redirect = await client.get("/spotify/connect")
            query = parse_qs(urlparse(redirect.headers["location"]).query)
            state = query["state"][0]
            verifier = spotify.pending[state][0]
            challenge = urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=")
            assert query["code_challenge"] == [challenge.decode()]
            assert query["code_challenge_method"] == ["S256"]
            callback = f"/callback?code=test-code&state={state}"
            assert (await client.get(callback)).status_code == 200
            assert (await client.get(callback)).status_code == 400
        assert exchanges[0]["code_verifier"] == [verifier]
        assert json.loads(spotify.token_file.read_text())["refresh_token"] == "test-refresh"
        assert spotify.token_file.stat().st_mode & 0o777 == 0o600

    asyncio.run(scenario())


def test_refresh_and_playback_target_existing_device(tmp_path, monkeypatch):
    async def scenario():
        spotify = Spotify("test-client", "http://127.0.0.1:8765/callback", tmp_path / "token.json")
        spotify.save(
            {"access_token": "old", "refresh_token": "saved-refresh", "expires_at": time.time() - 1}
        )
        original_oauth_type = spotify_module.AsyncOAuth2Client
        original_http_client = httpx.AsyncClient
        requests = []

        def respond(request):
            requests.append(request)
            if request.url.path == "/api/token":
                assert parse_qs(request.content.decode())["refresh_token"] == ["saved-refresh"]
                return oauth_http.Response(
                    200, json={"access_token": "new", "expires_in": 3600, "token_type": "Bearer"}
                )
            assert request.headers["Authorization"] == "Bearer new"
            return httpx.Response(204)

        transport = httpx.MockTransport(respond)

        def oauth(**kwargs):
            return original_oauth_type(transport=oauth_http.MockTransport(respond), **kwargs)

        monkeypatch.setattr(spotify_module, "AsyncOAuth2Client", oauth)
        monkeypatch.setattr(
            httpx,
            "AsyncClient",
            lambda **kwargs: original_http_client(transport=transport, **kwargs),
        )
        server = FastMCP("test")
        spotify.register(server)
        async with Client(server) as client:
            result = await client.call_tool(
                "spotify_control",
                {"action": "play", "uri": "spotify:track:example", "device_id": "speaker"},
            )
            assert result.data == {"success": True}
        assert requests[-1].url.params["device_id"] == "speaker"
        assert json.loads(requests[-1].content) == {"uris": ["spotify:track:example"]}
        assert json.loads(spotify.token_file.read_text())["refresh_token"] == "saved-refresh"

    asyncio.run(scenario())


def test_status_verifies_playing_paused_and_absent_playback(tmp_path, monkeypatch):
    async def scenario():
        spotify = Spotify("test-client", "http://127.0.0.1:8765/callback", tmp_path / "token.json")
        spotify.save({"access_token": "test-access", "expires_at": time.time() + 3600})
        original_http_client = httpx.AsyncClient
        responses = iter(
            [
                httpx.Response(
                    200,
                    json={
                        "is_playing": True,
                        "device": {"id": "speaker", "name": "Mac"},
                        "item": {
                            "name": "Test song",
                            "uri": "spotify:track:test",
                            "artists": [{"name": "Test artist"}],
                        },
                        "progress_ms": 1000,
                    },
                ),
                httpx.Response(200, json={"is_playing": False, "item": None}),
                httpx.Response(204),
            ]
        )

        def respond(request):
            assert request.url.path == "/v1/me/player"
            assert request.headers["Authorization"] == "Bearer test-access"
            return next(responses)

        monkeypatch.setattr(
            httpx,
            "AsyncClient",
            lambda **kwargs: original_http_client(transport=httpx.MockTransport(respond), **kwargs),
        )
        server = FastMCP("test")
        spotify.register(server)
        async with Client(server) as client:
            playing = (await client.call_tool("spotify_status", {})).data
            assert playing["is_playing"] is True
            assert playing["device"]["id"] == "speaker"
            assert playing["item"]["artists"] == ["Test artist"]
            assert playing["item"]["uri"] == "spotify:track:test"
            assert playing["progress_ms"] == 1000
            paused = (await client.call_tool("spotify_status", {})).data
            assert paused["playback_available"] is True
            assert paused["is_playing"] is False
            absent = (await client.call_tool("spotify_status", {})).data
            assert absent["playback_available"] is False
            assert absent["is_playing"] is False
            assert absent["device"] is None

    asyncio.run(scenario())


@pytest.mark.parametrize("status,hint", [(401, "Reconnect Spotify"), (404, "Open Spotify")])
def test_api_failures_explain_how_to_recover(tmp_path, monkeypatch, status, hint):
    async def scenario():
        spotify = Spotify("test-client", "http://127.0.0.1:8765/callback", tmp_path / "token.json")
        spotify.save({"access_token": "test-access", "expires_at": time.time() + 3600})
        original_http_client = httpx.AsyncClient
        monkeypatch.setattr(
            httpx,
            "AsyncClient",
            lambda **kwargs: original_http_client(
                transport=httpx.MockTransport(
                    lambda request: httpx.Response(
                        status, json={"error": {"message": "Unavailable"}}
                    )
                ),
                **kwargs,
            ),
        )
        with pytest.raises(ValueError, match=hint):
            await spotify.request("PUT", "/me/player/play", json={})

    asyncio.run(scenario())
