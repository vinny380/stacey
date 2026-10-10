import asyncio
from types import SimpleNamespace

import httpx

from stacy.cli import command, existing_backend
from stacy.config import Settings


def test_start_attaches_without_constructing_second_alarm_server(tmp_path, monkeypatch):
    import stacy.cli as module
    import stacy.voice as voice

    async def scenario():
        calls = []

        async def exists(port):
            return True

        async def run(settings, url):
            calls.append(url)

        async def serve(*args):
            raise AssertionError("Do not start a second scheduler")

        monkeypatch.setattr(module, "existing_backend", exists)
        monkeypatch.setattr(module, "serve", serve)
        monkeypatch.setattr(voice, "run_voice", run)
        settings = Settings(tmp_path, tmp_path / "memory")
        await command(SimpleNamespace(command="start", port=8765), settings)
        assert calls == ["http://127.0.0.1:8765/mcp"]
        await command(SimpleNamespace(command="serve", port=8765), settings)
        assert len(calls) == 1

    asyncio.run(scenario())


def test_backend_probe_checks_health_with_no_redirects_or_proxy(monkeypatch):
    original = httpx.AsyncClient
    requests = []

    def client(**kwargs):
        assert kwargs["trust_env"] is False and kwargs["follow_redirects"] is False

        def handle(request):
            requests.append(request)
            return httpx.Response(200, json={"service": "stacy", "ready": True})

        return original(**kwargs, transport=httpx.MockTransport(handle))

    monkeypatch.setattr(httpx, "AsyncClient", client)
    assert asyncio.run(existing_backend(8765))
    assert requests[0].url.path == "/health"
