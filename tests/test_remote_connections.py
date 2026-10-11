import asyncio
import hashlib
import json
import time
from base64 import urlsafe_b64encode
from urllib.parse import parse_qs

import httpx2
import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
from starlette.requests import Request

from stacy.mcp_gateway import MCPGateway
from stacy.remote_connections import (
    CALLBACK_PATH,
    CLIENT_METADATA_PATH,
    RemoteConnections,
    public_url,
)

URL = "https://research.example/mcp"
CALLBACK = "https://stacy.example" + CALLBACK_PATH


class Provider:
    """HTTP MCP + OAuth protocol fixture, including real SDK discovery/registration/PKCE."""

    def __init__(self, protected=True):
        self.protected = protected
        self.calls = []
        self.registrations = 0
        self.exchanges = 0
        self.refreshes = 0
        self.fail = False
        self.empty = False
        self.expected_challenge = None
        self.client_id = "stacy-fixture"
        self.metadata_supported = False
        self.token_endpoint = "https://login.example/token"

    async def request(self, request):
        self.calls.append((request.method, str(request.url)))
        path = request.url.path
        if path == "/.well-known/oauth-protected-resource/mcp":
            return httpx2.Response(
                200,
                json={
                    "resource": URL,
                    "authorization_servers": ["https://login.example"],
                    "scopes_supported": ["research"],
                },
            )
        if path == "/.well-known/oauth-authorization-server":
            return httpx2.Response(
                200,
                json={
                    "issuer": "https://login.example",
                    "authorization_endpoint": "https://login.example/authorize",
                    "token_endpoint": self.token_endpoint,
                    "registration_endpoint": "https://login.example/register",
                    "response_types_supported": ["code"],
                    "grant_types_supported": ["authorization_code", "refresh_token"],
                    "code_challenge_methods_supported": ["S256"],
                    "token_endpoint_auth_methods_supported": ["none"],
                    "authorization_response_iss_parameter_supported": True,
                    "client_id_metadata_document_supported": self.metadata_supported,
                },
            )
        if path == "/register":
            self.registrations += 1
            data = json.loads(request.content)
            assert data["redirect_uris"] == [CALLBACK]
            return httpx2.Response(201, json={**data, "client_id": self.client_id})
        if path == "/token":
            if str(request.url) != self.token_endpoint:
                return httpx2.Response(404)
            params = parse_qs(request.content.decode())
            assert params["client_id"] == [self.client_id]
            if params["grant_type"] == ["authorization_code"]:
                self.exchanges += 1
                assert params["code"] == ["private-code"]
                assert params["redirect_uri"] == [CALLBACK]
                challenge = (
                    urlsafe_b64encode(hashlib.sha256(params["code_verifier"][0].encode()).digest())
                    .decode()
                    .rstrip("=")
                )
                assert challenge == self.expected_challenge
            else:
                self.refreshes += 1
                assert params["refresh_token"] == ["private-refresh"]
            return httpx2.Response(
                200,
                json={
                    "access_token": "private-access",
                    "refresh_token": "private-refresh",
                    "token_type": "Bearer",
                    "expires_in": 3600,
                },
            )
        assert path == "/mcp"
        if self.fail:
            return httpx2.Response(503)
        if self.protected and request.headers.get("authorization") != "Bearer private-access":
            return httpx2.Response(
                401,
                headers={
                    "WWW-Authenticate": 'Bearer resource_metadata="https://research.example/.well-known/'
                    'oauth-protected-resource/mcp"'
                },
            )
        if request.method == "GET":
            return httpx2.Response(405)
        if request.method == "DELETE":
            return httpx2.Response(200)
        data = json.loads(request.content)
        method = data["method"]
        if "id" not in data:
            return httpx2.Response(202)
        if method == "server/discover":
            return httpx2.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": data["id"],
                    "error": {"code": -32601, "message": "Method not found"},
                },
            )
        if method == "initialize":
            result = {
                "protocolVersion": data["params"]["protocolVersion"],
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fixture", "version": "1"},
            }
        elif method == "tools/list":
            result = {
                "tools": []
                if self.empty
                else [
                    {
                        "name": "research",
                        "description": "Research a topic",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"topic": {"type": "string"}},
                            "required": ["topic"],
                        },
                    }
                ]
            }
        elif method == "tools/call":
            result = {"content": [{"type": "text", "text": "Research complete"}]}
        else:
            pytest.fail(f"Unexpected MCP method {method}")
        return httpx2.Response(200, json={"jsonrpc": "2.0", "id": data["id"], "result": result})

    def install(self, monkeypatch):
        import stacy.remote_connections as module

        monkeypatch.setattr(
            module,
            "remote_http_client",
            lambda **kwargs: httpx2.AsyncClient(
                **{
                    **kwargs,
                    "transport": httpx2.MockTransport(self.request),
                    "follow_redirects": False,
                    "trust_env": False,
                }
            ),
        )


def callback_request(link, **overrides):
    params = parse_qs(httpx2.URL(link).query.decode())
    query = {"state": params["state"][0], "code": "private-code", "iss": "https://login.example"}
    query.update(overrides)
    url = httpx2.URL(CALLBACK, params=query)
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": CALLBACK_PATH,
            "query_string": url.query,
            "headers": [],
        }
    )


def test_phone_oauth_survives_restart_then_activates_without_expanding_catalog(
    tmp_path, monkeypatch
):
    provider = Provider()
    provider.install(monkeypatch)

    async def scenario():
        gateway = MCPGateway({})
        host = FastMCP("stacy")
        gateway.register(host)
        manager = RemoteConnections(gateway, tmp_path, callback_url=CALLBACK)
        manager.register(host)
        async with Client(host) as client:
            names = {t.name for t in await client.list_tools()}
            first = (
                await client.call_tool(
                    "connect_mcp",
                    {
                        "alias": "research",
                        "url": URL,
                        "description": "Research bot",
                    },
                )
            ).data
            assert first["status"] == "awaiting_authorization"
            link = first["authorization_url"]
            params = parse_qs(httpx2.URL(link).query.decode())
            provider.expected_challenge = params["code_challenge"][0]
            assert params["redirect_uri"] == [CALLBACK]
            assert gateway.servers == {}
            assert provider.registrations == 1
            assert (await manager.connect("research", URL))["authorization_url"] == link
            assert provider.registrations == 1
        restored_gateway = MCPGateway({})
        restored = RemoteConnections(restored_gateway, tmp_path, callback_url=CALLBACK)
        status = json.dumps(restored.status())
        assert "private-" not in status and "authorization_url" not in status
        assert (await restored.callback(callback_request(link, state="wrong"))).status_code == 400
        assert (
            await restored.callback(callback_request(link, iss="https://attacker.example"))
        ).status_code == 400
        assert (await restored.callback(callback_request(link))).status_code == 200
        assert (await restored.callback(callback_request(link))).status_code == 400
        # Restart after callback, before token exchange: PKCE/state/code are durable.
        ready = RemoteConnections(restored_gateway, tmp_path, callback_url=CALLBACK)
        assert (await ready.verify("research"))["status"] == "connected"
        assert provider.exchanges == 1
        assert (await restored_gateway.search_servers("research"))["servers"][0][
            "server"
        ] == "research"
        tools = (await restored_gateway.search_tools("research", "research")).structured_content
        assert tools["tools"][0]["name"] == "research"
        assert "Research complete" in str(
            await restored_gateway.call_tool("research", "research", {"topic": "MCP"})
        )
        # A new process restores only this alias and does not do network I/O at startup.
        count = len(provider.calls)
        fresh_gateway = MCPGateway({})
        fresh = RemoteConnections(fresh_gateway, tmp_path, callback_url=CALLBACK)
        assert len(provider.calls) == count
        assert fresh_gateway.servers.keys() == {"research"}
        fresh_host = FastMCP("fresh")
        fresh_gateway.register(fresh_host)
        fresh.register(fresh_host)
        async with Client(fresh_host) as client:
            assert {t.name for t in await client.list_tools()} == names
        assert (await fresh.verify("research"))["status"] == "connected"
        assert provider.registrations == provider.exchanges == 1
        assert "private-" not in (tmp_path / "remote-connections.json").read_text()
        assert "verifier" not in (tmp_path / "remote-credentials.json").read_text()
        assert all(p.stat().st_mode & 0o777 == 0o600 for p in tmp_path.glob("*.json"))
        # Expiry does not reset when the process starts; the SDK refreshes instead of reauth.
        fresh.secrets.data["research"]["expires_at"] = time.time() - 10
        fresh.secrets.save()
        again = RemoteConnections(MCPGateway({}), tmp_path, callback_url=CALLBACK)
        assert (await again.verify("research"))["status"] == "connected"
        assert provider.refreshes == 1
        await again.disconnect("research")
        assert again.gateway.servers == {} and again.gateway._search_cache == {}
        assert again.secrets.data == {}
        assert (
            RemoteConnections(MCPGateway({}), tmp_path, callback_url=CALLBACK).gateway.servers == {}
        )
        with pytest.raises(ToolError, match="disconnected"):
            await again.verify("research")
        assert (await again.reconnect("research"))["status"] == "awaiting_authorization"

    asyncio.run(scenario())


def test_public_mcp_failure_disconnect_and_alias_protection(tmp_path, monkeypatch):
    provider = Provider(protected=False)
    provider.install(monkeypatch)

    async def scenario():
        gateway = MCPGateway({"composio": {"description": "Google"}})
        manager = RemoteConnections(gateway, tmp_path)
        assert (await manager.connect("research", URL))["status"] == "connected"
        await gateway.search_tools("research", "research")
        assert gateway._search_cache
        with pytest.raises(ToolError, match="another endpoint"):
            await manager.connect("research", "https://other.example/mcp")
        with pytest.raises(ToolError, match="unique"):
            await manager.connect("composio", URL)
        provider.fail = True
        with pytest.raises(ToolError, match="may have completed"):
            await gateway.call_tool("research", "research", {"topic": "test"})
        assert manager.status()["connections"][0]["status"] == "needs_attention"
        assert "research" not in gateway.servers
        assert not gateway._search_cache
        provider.fail = False
        assert (await manager.verify("research"))["status"] == "connected"
        await manager.disconnect("research")
        assert "composio" in gateway.servers
        provider.empty = True
        assert (await manager.reconnect("research"))["status"] == "needs_attention"
        assert "research" not in gateway.servers

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["transient", "rejected", "issuer_changed"])
def test_refresh_failures_preserve_grant_and_return_safe_specific_reason(
    tmp_path, monkeypatch, failure
):
    from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

    from stacy.remote_connections import Credentials

    provider = Provider()
    original = provider.request

    async def failing(request):
        if request.url.path == "/token":
            return httpx2.Response(
                503 if failure == "transient" else 401,
                json={"error": "invalid_client", "error_description": "private-data"},
            )
        response = await original(request)
        if failure == "issuer_changed" and request.url.path.startswith(
            "/.well-known/oauth-protected"
        ):
            data = response.json()
            data["authorization_servers"] = ["https://attacker.example"]
            return httpx2.Response(200, json=data)
        return response

    provider.request = failing
    provider.install(monkeypatch)

    async def scenario():
        manager = RemoteConnections(MCPGateway({}), tmp_path)
        manager.state.data["research"] = {
            "url": URL,
            "description": "Research",
            "status": "needs_attention",
            "tool_count": 1,
        }
        manager.state.save()
        storage = Credentials(manager.secrets, "research")
        await storage.set_client_info(
            OAuthClientInformationFull(
                client_id=provider.client_id,
                issuer="https://login.example",
                redirect_uris=[CALLBACK],
                token_endpoint_auth_method="none",
            )
        )
        await storage.set_tokens(
            OAuthToken(
                access_token="private-access",
                refresh_token="private-refresh",
                token_type="Bearer",
                expires_in=3600,
            )
        )
        storage.data["expires_at"] = time.time() - 1
        manager.secrets.save()
        before = json.dumps(storage.data, sort_keys=True)
        result = await manager.verify("research")
        expected = {
            "transient": "authorization_server_unavailable",
            "rejected": "refresh_rejected",
            "issuer_changed": "authorization_issuer_changed",
        }[failure]
        assert result["reason"] == expected
        assert "private" not in json.dumps(result)
        assert "not provisioned" not in result["instruction"]
        assert json.dumps(storage.data, sort_keys=True) == before
        assert provider.registrations == 0
        assert not manager.gateway.servers

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "url",
    [
        "http://research.example/mcp",
        "https://localhost/mcp",
        "https://127.0.0.1/mcp",
        "https://[::1]/mcp",
        "https://10.0.0.1/mcp",
        "https://pi.local/mcp",
        "https://user:private-password@research.example/mcp",
        "https://research.example/mcp?key=secret",
        "https://research.example/mcp#secret",
        "https://research.example:bad/mcp",
    ],
)
def test_endpoint_validation_does_not_echo_secrets(url):
    with pytest.raises(ToolError) as error:
        public_url(url)
    assert url not in str(error.value) and "secret" not in str(error.value)


def test_denied_expired_setup_and_unprovisioned_auth_are_honest(tmp_path, monkeypatch):
    provider = Provider()
    provider.install(monkeypatch)

    async def scenario():
        manager = RemoteConnections(MCPGateway({}), tmp_path, callback_url=CALLBACK)
        first = await manager.connect("research", URL)
        link = first["authorization_url"]
        assert (
            await manager.callback(callback_request(link, error="access_denied"))
        ).status_code == 200
        assert manager.status()["connections"][0]["status"] == "needs_attention"
        new = await manager.reconnect("research")
        assert new["authorization_url"] != link
        manager.secrets.data["research"]["pending"]["expires_at"] = time.time() - 1
        manager.secrets.save()
        assert (
            await manager.callback(callback_request(new["authorization_url"]))
        ).status_code == 400
        renewed = await manager.verify("research")
        assert renewed["authorization_url"] != new["authorization_url"]
        assert provider.registrations == 2
        missing = RemoteConnections(MCPGateway({}), tmp_path / "missing")
        result = await missing.connect("research", URL)
        assert result["status"] == "needs_attention"
        assert "not provisioned" in result["instruction"]
        assert "authorization_url" not in result

    asyncio.run(scenario())


def test_shared_voice_tools_text_handoff_and_done_after_restart(tmp_path, monkeypatch):
    """Shared MCP tools -> existing iMessage bridge -> callback -> incoming text agent turn."""
    from types import SimpleNamespace

    import httpx

    from stacy.agent import AgentMessage, TextAgent
    from stacy.alarms import AlarmService
    from stacy.config import Settings
    from stacy.memory import MemoryRepo
    from stacy.notifications import Notifications
    from stacy.tools import create_tools

    provider = Provider()
    provider.install(monkeypatch)
    monkeypatch.setenv("STACY_MCP_CALLBACK_URL", CALLBACK)
    for key in ("COMPOSIO_API_KEY", "EXA_API_KEY", "E2B_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    sent = []

    async def deliver(request):
        sent.append(json.loads(request.read())["text"])
        return httpx.Response(200, json={"status": "sent", "message_id": "phone-message"})

    async def noop(value):
        pass

    class Output:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

        def model_dump(self, **kwargs):
            return self.__dict__

    class Model:
        def __init__(self):
            self.responses = self
            self.step = 0

        async def create(self, **kwargs):
            assert "private-code" not in json.dumps(kwargs)
            assert "private-access" not in json.dumps(kwargs)
            self.step += 1
            if self.step < 3:
                name, args = (
                    ("list_connections", {})
                    if self.step == 1
                    else ("verify_connection", {"alias": "research"})
                )
                return SimpleNamespace(
                    output_text="",
                    output=[
                        Output(
                            type="function_call",
                            name=name,
                            arguments=json.dumps(args),
                            call_id=str(self.step),
                        )
                    ],
                )
            result = json.loads(kwargs["input"][-1]["output"])["structured_content"]
            assert result["status"] == "connected"
            return SimpleNamespace(output_text="Your research bot is connected.", output=[])

        async def close(self):
            pass

    async def scenario():
        settings = Settings(tmp_path, tmp_path / "memory")
        alarms = AlarmService(tmp_path / "alarms.sqlite3", settings.timezone, noop, noop)
        notifications = Notifications(tmp_path, "x" * 32, transport=httpx.MockTransport(deliver))
        host = create_tools(alarms, MemoryRepo(settings.memory_dir))
        notifications.register(host)
        try:
            async with Client(host) as voice_tools:
                pending = (
                    await voice_tools.call_tool(
                        "connect_mcp",
                        {
                            "alias": "research",
                            "url": URL,
                            "description": "Research bot",
                        },
                    )
                ).data
                link = pending["authorization_url"]
                provider.expected_challenge = parse_qs(httpx2.URL(link).query.decode())[
                    "code_challenge"
                ][0]
                result = await voice_tools.call_tool("send_text", {"text": link})
                assert result.data["status"] == "sent" and sent == [link]
            app = host.http_app()
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="https://stacy.example"
            ) as browser:
                callback = callback_request(link)
                response = await browser.get(
                    CALLBACK_PATH + "?" + callback.scope["query_string"].decode()
                )
                assert response.status_code == 200
                assert "private-code" not in response.text and "done" in response.text
            # New tool host + new agent, receiving only done through iMessage.
            restarted = create_tools(alarms, MemoryRepo(settings.memory_dir))
            notifications.register(restarted)
            agent = TextAgent(settings, restarted, model_client=Model())
            try:
                reply = await agent.respond(
                    AgentMessage("imessage", "dm", "owner", "done-1", "done")
                )
                assert reply == "Your research bot is connected."
                async with Client(restarted) as tools:
                    result = await tools.call_tool("search_mcp_servers", {"query": "research"})
                    assert result.data["servers"][0]["server"] == "research"
            finally:
                await agent.close()
        finally:
            notifications.close()
            alarms.close()

    asyncio.run(scenario())


def test_oauth_error_logs_and_results_never_contain_token_response(tmp_path, monkeypatch, caplog):
    provider = Provider()
    original = provider.request

    async def broken(request):
        if request.url.path == "/token":
            provider.exchanges += 1
            return httpx2.Response(400, text="private-provider-secret")
        return await original(request)

    provider.request = broken
    provider.install(monkeypatch)

    async def scenario():
        manager = RemoteConnections(MCPGateway({}), tmp_path, callback_url=CALLBACK)
        link = (await manager.connect("research", URL))["authorization_url"]
        await manager.callback(callback_request(link))
        result = await manager.verify("research")
        assert result["status"] == "needs_attention"
        assert "private-provider-secret" not in json.dumps(result)
        # A lost/failed token exchange is not repeated with the same one-time code.
        await manager.verify("research")
        assert provider.exchanges == 1
        assert "private-provider-secret" not in caplog.text

    asyncio.run(scenario())


def test_discovery_during_disconnect_cannot_restore_stale_schemas(tmp_path, monkeypatch):
    provider = Provider(protected=False)
    original = provider.request
    started, finish = asyncio.Event(), asyncio.Event()
    block = False

    async def delayed(request):
        if (
            block
            and request.method == "POST"
            and json.loads(request.content).get("method") == "tools/list"
        ):
            started.set()
            await finish.wait()
        return await original(request)

    provider.request = delayed
    provider.install(monkeypatch)

    async def scenario():
        nonlocal block
        gateway = MCPGateway({})
        manager = RemoteConnections(gateway, tmp_path)
        await manager.connect("research", URL)
        block = True
        task = asyncio.create_task(gateway.search_tools("research", "research"))
        await asyncio.wait_for(started.wait(), 5)
        gateway.remove_server("research")
        finish.set()
        with pytest.raises(ToolError, match="changed during discovery"):
            await task
        assert not gateway._search_cache and not gateway.servers

    asyncio.run(scenario())


def test_dns_destination_is_checked_and_pinned_without_losing_tls_hostname(monkeypatch):
    import socket

    from stacy.remote_connections import PublicHTTPTransport

    async def scenario():
        loop = asyncio.get_running_loop()
        calls = []
        private = False

        async def resolve(host, port, **kwargs):
            calls.append(host)
            return [
                (
                    socket.AF_INET,
                    socket.SOCK_STREAM,
                    6,
                    "",
                    ("127.0.0.1" if private else "1.1.1.1", port),
                )
            ]

        monkeypatch.setattr(loop, "getaddrinfo", resolve)
        transport = PublicHTTPTransport()
        await transport.inner.aclose()
        seen = []

        async def network(request):
            seen.append(request)
            assert request.url.host == "1.1.1.1"
            assert request.headers["host"] == "research.example"
            assert request.extensions["sni_hostname"] == "research.example"
            return httpx2.Response(200)

        transport.inner = httpx2.MockTransport(network)
        async with httpx2.AsyncClient(transport=transport) as client:
            assert (await client.get(URL)).status_code == 200
            assert calls == ["research.example"]
            private = True
            with pytest.raises(ToolError, match="not publicly reachable"):
                await client.get(URL)
            assert len(seen) == 1

    asyncio.run(scenario())


def test_loopback_callback_requires_explicit_local_test_mode(tmp_path):
    callback = "http://127.0.0.1:8767" + CALLBACK_PATH
    with pytest.raises(ToolError):
        RemoteConnections(MCPGateway({}), tmp_path, callback_url=callback)
    manager = RemoteConnections(
        MCPGateway({}), tmp_path, callback_url=callback, local_callback_test=True
    )
    assert manager.callback_url == callback
    with pytest.raises(ValueError, match="explicit 127.0.0.1"):
        RemoteConnections(MCPGateway({}), tmp_path, callback_url=CALLBACK, local_callback_test=True)


def test_headless_token_is_internal_and_verified_without_oauth_registration(tmp_path, monkeypatch):
    provider = Provider()
    provider.install(monkeypatch)

    async def scenario():
        gateway = MCPGateway({})
        manager = RemoteConnections(gateway, tmp_path)
        assert (await manager.connect("stanley", URL))["status"] == "needs_attention"
        assert (await manager.import_bearer("stanley", "private-access"))["status"] == "connected"
        assert provider.registrations == provider.exchanges == 0
        host = FastMCP("test")
        manager.register(host)
        async with Client(host) as client:
            names = {t.name for t in await client.list_tools()}
            assert "import_bearer" not in names
            assert "private-access" not in json.dumps(
                (await client.call_tool("list_connections", {})).data
            )
        assert "private-access" not in (tmp_path / "remote-connections.json").read_text()
        restored = RemoteConnections(MCPGateway({}), tmp_path)
        assert (await restored.verify("stanley"))["status"] == "connected"
        await restored.disconnect("stanley")
        assert "private-access" not in (tmp_path / "remote-credentials.json").read_text()

    asyncio.run(scenario())


def test_metadata_document_client_identity_is_passed_to_sdk(tmp_path):
    manager = RemoteConnections(
        MCPGateway({}),
        tmp_path,
        callback_url=CALLBACK,
        client_metadata_url="https://stacy.example/oauth-client.json",
    )
    manager.state.data["research"] = {"url": URL}
    assert (
        manager.transport("research").auth.context.client_metadata_url
        == "https://stacy.example/oauth-client.json"
    )


def test_metadata_identity_skips_registration_and_completes_oauth(tmp_path, monkeypatch):
    metadata_url = "https://stacy.example" + CLIENT_METADATA_PATH
    provider = Provider()
    provider.metadata_supported = True
    provider.client_id = metadata_url
    provider.install(monkeypatch)

    async def scenario():
        manager = RemoteConnections(
            MCPGateway({}),
            tmp_path,
            callback_url=CALLBACK,
            client_metadata_url=metadata_url,
        )
        result = await manager.connect("research", URL)
        assert result["status"] == "awaiting_authorization"
        params = parse_qs(httpx2.URL(result["authorization_url"]).query.decode())
        assert params["client_id"] == [metadata_url]
        assert params["redirect_uri"] == [CALLBACK]
        assert params["response_type"] == ["code"]
        assert params["code_challenge_method"] == ["S256"]
        assert params["state"][0]
        assert provider.registrations == 0
        assert manager.gateway.servers == {}
        provider.expected_challenge = params["code_challenge"][0]
        response = await manager.callback(callback_request(result["authorization_url"]))
        assert response.status_code == 200
        assert (await manager.verify("research"))["status"] == "connected"
        assert provider.exchanges == 1
        assert provider.registrations == 0

    asyncio.run(scenario())


def test_public_metadata_route_contains_identity_only(tmp_path):
    import httpx

    metadata_url = "https://stacy.example" + CLIENT_METADATA_PATH
    manager = RemoteConnections(
        MCPGateway({}),
        tmp_path,
        callback_url=CALLBACK,
        client_metadata_url=metadata_url,
    )
    manager.secrets.data["private"] = {"tokens": "must-not-be-public"}
    host = FastMCP("test")
    manager.register(host)

    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=host.http_app()),
            base_url="https://stacy.example",
        ) as client:
            response = await client.get(CLIENT_METADATA_PATH)
            assert response.status_code == 200
            assert response.json() == {
                "client_id": metadata_url,
                "client_name": "Stacy",
                "redirect_uris": [CALLBACK],
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "none",
            }
            assert "must-not-be-public" not in response.text

    asyncio.run(scenario())


def test_oauth_ingress_exposes_only_metadata_and_validated_callback(tmp_path):
    import httpx

    manager = RemoteConnections(
        MCPGateway({}),
        tmp_path,
        callback_url=CALLBACK,
        client_metadata_url="https://stacy.example" + CLIENT_METADATA_PATH,
    )
    manager.secrets.data["private"] = {"tokens": "private-access-token"}

    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=manager.oauth_app()),
            base_url="https://stacy.example",
        ) as client:
            assert (await client.get(CLIENT_METADATA_PATH)).status_code == 200
            for path in ("/mcp", "/agent/message", "/", "/remote-credentials.json", "/docs"):
                assert (await client.get(path)).status_code == 404
                assert (await client.post(path, json={})).status_code == 404
            response = await client.get(CALLBACK_PATH, params={"state": "unknown", "code": "code"})
            assert response.status_code == 400
            assert "private-access-token" not in response.text
            assert (await client.post(CALLBACK_PATH)).status_code == 405
            oversized = await client.get(CALLBACK_PATH + "?state=" + "s" * 17000)
            assert oversized.status_code == 400
            assert manager.secrets.data["private"] == {"tokens": "private-access-token"}

    asyncio.run(scenario())


def test_saved_oauth_grant_is_usable_by_shared_runtime_without_callback_env(tmp_path, monkeypatch):
    provider = Provider()
    provider.install(monkeypatch)

    async def scenario():
        setup = RemoteConnections(MCPGateway({}), tmp_path, callback_url=CALLBACK)
        result = await setup.connect("research", URL)
        link = result["authorization_url"]
        provider.expected_challenge = parse_qs(httpx2.URL(link).query.decode())["code_challenge"][0]
        assert (await setup.callback(callback_request(link))).status_code == 200
        assert (await setup.verify("research"))["status"] == "connected"
        # The regular messaging runtime restores operational state, even with no
        # current public callback configured. It must retain the approved grant.
        gateway = MCPGateway({})
        live = RemoteConnections(gateway, tmp_path)
        assert (await live.verify("research"))["status"] == "connected"
        assert provider.exchanges == 1 and provider.registrations == 1
        result = await gateway.search_tools("research", "research")
        assert [t["name"] for t in result.structured_content["tools"]] == ["research"]
        assert "authorization_url" not in live.status()
        saved = live.secrets.data["research"]
        saved["expires_at"] = time.time() - 1
        live.secrets.save()
        refreshed = RemoteConnections(MCPGateway({}), tmp_path)
        assert (await refreshed.verify("research"))["status"] == "connected"
        assert provider.refreshes == 1
        # Lost/revoked access must not produce a link to the offline setup callback.
        saved = refreshed.secrets.data["research"]
        saved["tokens"]["access_token"] = "revoked-access"
        saved["tokens"].pop("refresh_token", None)
        refreshed.secrets.save()
        revoked = RemoteConnections(MCPGateway({}), tmp_path)
        result = await revoked.verify("research")
        assert result["status"] == "needs_attention"
        assert "authorization_url" not in result
        assert not revoked.secrets.data["research"].get("pending")

    asyncio.run(scenario())


def test_public_client_metadata_survives_restart_without_reenabling_phone_callback(tmp_path):
    metadata = "https://stacy.example" + CLIENT_METADATA_PATH
    RemoteConnections(MCPGateway({}), tmp_path, callback_url=CALLBACK, client_metadata_url=metadata)
    fresh = RemoteConnections(MCPGateway({}), tmp_path)

    async def scenario():
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=fresh.oauth_app()), base_url="https://stacy.example"
        ) as client:
            response = await client.get(CLIENT_METADATA_PATH)
            assert response.status_code == 200
            assert response.json()["client_id"] == metadata
            assert response.json()["redirect_uris"] == [CALLBACK]
            # An identity document remains available for refresh. New consent
            # still requires a currently configured, reachable phone callback.
            assert (await client.get(CALLBACK_PATH)).status_code == 404

    asyncio.run(scenario())
