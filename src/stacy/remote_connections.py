"""Conversational remote MCP lifecycle; OAuth secrets never cross the tool interface."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import os
import re
import secrets
import socket
import tempfile
import time
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

import httpx2
from fastmcp import Client, FastMCP
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.exceptions import ToolError
from mcp.client.auth import OAuthClientProvider
from mcp.client.auth.oauth2 import (
    PKCEParameters,
    build_oauth_authorization_server_metadata_discovery_urls,
    build_protected_resource_metadata_discovery_urls,
    handle_auth_metadata_response,
    handle_protected_resource_response,
    issuers_match,
    validate_metadata_issuer,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata, OAuthToken
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route

from stacy.mcp_gateway import MCPGateway

CALLBACK_PATH = "/connections/oauth/callback"
CLIENT_METADATA_PATH = "/connections/oauth/client.json"
RESERVED = {"composio", "search_tools", "call_tool"}


def public_url(value: str, *, query: bool = False) -> str:
    """Reject credentials and local addresses before anything enters state/model results."""
    try:
        parts = urlsplit(value)
        host = parts.hostname
        if (
            parts.scheme != "https"
            or not host
            or parts.username is not None
            or parts.password is not None
            or parts.fragment
            or (parts.query and not query)
            or "${" in value
            or any(ord(c) < 33 for c in value)
            or host.rstrip(".").lower() == "localhost"
            or host.rstrip(".").lower().endswith((".localhost", ".local"))
        ):
            raise ValueError()
        _ = parts.port
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            if not address.is_global:
                raise ValueError()
    except ValueError:
        raise ToolError(
            "Use a public HTTPS endpoint without embedded credentials, query or fragment. "
            "Do not send API keys in conversation."
        ) from None
    return value


class PublicHTTPTransport(httpx2.AsyncBaseTransport):
    """Resolve once and connect to that checked IP, retaining HTTPS hostname verification."""

    def __init__(self):
        # No cross-host TLS connection reuse when distinct hostnames resolve to the same IP.
        self.inner = httpx2.AsyncHTTPTransport(
            trust_env=False, retries=0, limits=httpx2.Limits(max_keepalive_connections=0)
        )

    async def handle_async_request(self, request):
        public_url(str(request.url), query=True)
        addresses = await asyncio.get_running_loop().getaddrinfo(
            request.url.host, request.url.port or 443, type=socket.SOCK_STREAM
        )
        if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
            raise ToolError("Connection destination is not publicly reachable")
        pinned = httpx2.Request(
            request.method,
            request.url.copy_with(host=addresses[0][4][0]),
            headers=request.headers,
            stream=request.stream,
            extensions={**request.extensions, "sni_hostname": request.url.host},
        )
        return await self.inner.handle_async_request(pinned)

    async def aclose(self):
        await self.inner.aclose()


def remote_http_client(**kwargs) -> httpx2.AsyncClient:
    return httpx2.AsyncClient(
        **{
            **kwargs,
            "follow_redirects": False,
            "trust_env": False,
            "transport": PublicHTTPTransport(),
        }
    )


class PrivateFile:
    def __init__(self, path: Path):
        self.path = path
        self.data = json.loads(path.read_text()) if path.exists() else {}
        if path.exists():
            path.chmod(0o600)

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, name = tempfile.mkstemp(dir=self.path.parent, prefix=".connections-")
        try:
            with os.fdopen(fd, "w") as file:
                json.dump(self.data, file)
                file.flush()
                os.fsync(file.fileno())
            os.replace(name, self.path)
        finally:
            if os.path.exists(name):
                os.unlink(name)


class Credentials:
    """SDK token storage scoped to one connection, separate from its public identity."""

    def __init__(self, store: PrivateFile, alias: str):
        self.store, self.alias = store, alias

    @property
    def data(self) -> dict:
        return self.store.data.setdefault(self.alias, {})

    async def get_tokens(self):
        value = self.data.get("tokens")
        return OAuthToken.model_validate(value) if value else None

    async def set_tokens(self, tokens: OAuthToken):
        self.data["tokens"] = tokens.model_dump(mode="json")
        self.data["expires_at"] = (
            time.time() + tokens.expires_in if tokens.expires_in is not None else None
        )
        self.data.pop("pending", None)
        self.store.save()

    async def get_client_info(self):
        value = self.data.get("client")
        return OAuthClientInformationFull.model_validate(value) if value else None

    async def set_client_info(self, client_info: OAuthClientInformationFull):
        self.data["client"] = client_info.model_dump(mode="json")
        self.store.save()


class OAuthLogFilter(logging.Filter):
    """SDK auth exceptions can embed tokens/callbacks; retain only level and event identity."""

    def filter(self, record):
        record.msg = "MCP authorization event (details omitted)"
        record.args = ()
        record.exc_info = None
        record.exc_text = None
        record.stack_info = None
        return True


class AuthorizationPending(Exception):
    """Safe exception used to suspend SDK authorization without a browser or waiting task."""


class OAuthAccessError(ToolError):
    """A public diagnostic code without provider responses or credentials."""

    def __init__(self, reason):
        self.reason = reason
        super().__init__("OAuth access could not be verified")


def access_reason(error):
    """Unwrap transport/async errors without exposing their messages."""
    if isinstance(error, OAuthAccessError):
        return error.reason
    for child in getattr(error, "exceptions", ()):
        reason = access_reason(child)
        if reason != "verification_failed":
            return reason
    if error.__cause__ is not None:
        return access_reason(error.__cause__)
    return "verification_failed"


class PhoneOAuth(OAuthClientProvider):
    """Use SDK discovery/registration/refresh with a restartable phone authorization grant.

    The SDK's standard grant holds PKCE/state in a waiting coroutine. Persist them here
    instead; the next verification resumes after the public callback stores its response.
    This override is covered by a real HTTP protocol fixture against the locked MCP SDK.
    """

    def __init__(
        self,
        url: str,
        callback: str,
        credentials: Credentials,
        client_metadata_url: str | None = None,
        allow_authorization: bool = True,
    ):
        self.credentials = credentials
        self.allow_authorization = allow_authorization
        logger = logging.getLogger("mcp.client.auth.oauth2")
        if not any(isinstance(f, OAuthLogFilter) for f in logger.filters):
            logger.addFilter(OAuthLogFilter())
        super().__init__(
            server_url=url,
            client_metadata=OAuthClientMetadata(
                client_name="Stacy",
                redirect_uris=[callback],
                token_endpoint_auth_method="none",
                application_type="web",
            ),
            storage=credentials,
            client_metadata_url=client_metadata_url,
        )

    async def _initialize(self):
        await super()._initialize()
        # The SDK's token model has relative lifetime; preserve absolute expiry on restart.
        self.context.token_expiry_time = self.credentials.data.get("expires_at")

    async def _refresh_token(self):
        # The SDK refreshes before discovery and otherwise guesses origin/token.
        # Our transport instances are short-lived, so rediscover the real endpoint
        # before handing any refresh credential to a server after restart.
        context = self.context
        if context.oauth_metadata is None:
            async with remote_http_client(timeout=15) as client:
                for url in build_protected_resource_metadata_discovery_urls(
                    None, context.server_url
                ):
                    response = await client.get(public_url(url, query=True))
                    if response.status_code >= 500 or response.status_code == 429:
                        raise OAuthAccessError("authorization_server_unavailable")
                    prm = await handle_protected_resource_response(response)
                    if prm:
                        await self._validate_resource_match(prm)
                        context.protected_resource_metadata = prm
                        context.auth_server_url = self._select_authorization_server(
                            [str(value) for value in prm.authorization_servers]
                        )
                        break
                issuer = self._expected_issuer()
                bound = context.client_info.issuer if context.client_info else None
                if not bound or not issuers_match(bound, issuer):
                    raise OAuthAccessError("authorization_issuer_changed")
                for url in build_oauth_authorization_server_metadata_discovery_urls(
                    context.auth_server_url, context.server_url
                ):
                    response = await client.get(public_url(url, query=True))
                    if response.status_code >= 500 or response.status_code == 429:
                        raise OAuthAccessError("authorization_server_unavailable")
                    ok, metadata = await handle_auth_metadata_response(response)
                    if not ok:
                        break
                    if metadata:
                        validate_metadata_issuer(metadata, issuer)
                        context.oauth_metadata = metadata
                        break
            if context.oauth_metadata is None or not context.oauth_metadata.token_endpoint:
                raise OAuthAccessError("token_endpoint_unavailable")
        public_url(str(context.oauth_metadata.token_endpoint))
        return await super()._refresh_token()

    async def _handle_refresh_response(self, response):
        # A transient failure must not clear a grant and start new consent.
        # Preserve persisted credentials on rejection too, for an explicit repair.
        if response.status_code != 200:
            reason = (
                "authorization_server_unavailable"
                if response.status_code >= 500 or response.status_code == 429
                else "refresh_rejected"
            )
            raise OAuthAccessError(reason)
        return await super()._handle_refresh_response(response)

    async def _perform_authorization_code_grant(self) -> tuple[str, str]:
        if not self.allow_authorization:
            raise ToolError("A reachable OAuth callback is required for new authorization")
        context = self.context
        metadata = context.oauth_metadata
        if not metadata or not context.client_info or not metadata.authorization_endpoint:
            raise ToolError("Provider did not advertise a supported authorization flow")
        issuer = str(metadata.issuer)
        pending = self.credentials.data.get("pending")
        if pending and pending["expires_at"] > time.time() and pending["issuer"] == issuer:
            callback = pending.get("callback")
            if callback:
                if pending.get("exchange_started"):
                    raise ToolError(
                        "Authorization exchange was uncertain; reconnect this connection"
                    )
                # Verify against the original grant, not newly discovered provider metadata.
                if (
                    (callback.get("iss") is not None and callback["iss"] != issuer)
                    or (pending["require_iss"] and callback.get("iss") is None)
                    or not secrets.compare_digest(callback["state"], pending["state"])
                ):
                    raise ToolError("Authorization response did not match the requested provider")
                if pending["client_id"] != context.client_info.client_id:
                    raise ToolError("Authorization registration changed; reconnect this connection")
                pending["exchange_started"] = True
                self.credentials.store.save()
                return callback["code"], pending["verifier"]
            raise AuthorizationPending("Phone authorization is still pending")
        pkce = PKCEParameters.generate()
        state = secrets.token_urlsafe(32)
        params = {
            "response_type": "code",
            "client_id": context.client_info.client_id,
            "redirect_uri": str(context.client_metadata.redirect_uris[0]),
            "state": state,
            "code_challenge": pkce.code_challenge,
            "code_challenge_method": "S256",
        }
        if context.should_include_resource_param(context.protocol_version):
            params["resource"] = context.get_resource_url()
        if context.client_metadata.scope:
            params["scope"] = context.client_metadata.scope
            if "offline_access" in context.client_metadata.scope.split():
                params["prompt"] = "consent"
        endpoint = public_url(str(metadata.authorization_endpoint), query=True)
        parts = urlsplit(endpoint)
        # Preserve provider endpoint parameters without creating duplicate OAuth parameters.
        params = {**{k: v[-1] for k, v in parse_qs(parts.query).items()}, **params}
        link = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(params), ""))
        self.credentials.data["pending"] = {
            "state": state,
            "verifier": pkce.code_verifier,
            "issuer": issuer,
            "require_iss": metadata.authorization_response_iss_parameter_supported,
            "client_id": context.client_info.client_id,
            "link": link,
            "expires_at": time.time() + 600,
        }
        self.credentials.store.save()
        raise AuthorizationPending("Phone authorization is required")


class RemoteConnections:
    def __init__(
        self,
        gateway: MCPGateway,
        data_dir: Path,
        *,
        callback_url: str | None = None,
        local_callback_test: bool = False,
        client_metadata_url: str | None = None,
    ):
        self.gateway = gateway
        self.state = PrivateFile(data_dir / "remote-connections.json")
        self.secrets = PrivateFile(data_dir / "remote-credentials.json")
        if local_callback_test:
            parts = urlsplit(callback_url or "")
            if (
                parts.scheme != "http"
                or parts.hostname != "127.0.0.1"
                or parts.username
                or parts.password
                or parts.query
                or parts.fragment
                or not parts.port
            ):
                raise ValueError("Local callback tests require an explicit 127.0.0.1 HTTP port")
            self.callback_url = callback_url
        else:
            self.callback_url = public_url(callback_url) if callback_url else None
        if self.callback_url and urlsplit(self.callback_url).path != CALLBACK_PATH:
            raise ValueError("STACY_MCP_CALLBACK_URL must end with " + CALLBACK_PATH)
        self.client_metadata_url = public_url(client_metadata_url) if client_metadata_url else None
        # CIMD is a public identity, not a temporary login page. Keep serving its
        # nonsecret document after a backend restart even when setup-only callback
        # environment variables are absent. Do not infer live callback ingress.
        self.public_identity = PrivateFile(data_dir / "oauth-client-metadata.json")
        if self._configured_client_metadata():
            self.public_identity.data = {
                "client_id": self.client_metadata_url,
                "client_name": "Stacy",
                "redirect_uris": [self.callback_url],
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "none",
            }
            self.public_identity.save()
        self.lock = asyncio.Lock()
        self.reserved = RESERVED | set(gateway.servers)
        for alias, entry in self.state.data.items():
            if alias in self.reserved:
                raise ValueError("Managed MCP alias conflicts with a developer-configured server")
            if entry["status"] == "connected":
                self.activate(alias)

    def transport(self, alias: str):
        entry = self.state.data[alias]
        stored = self.secrets.data.get(alias, {})
        bearer = stored.get("bearer")
        callback = self.callback_url
        metadata_url = self.client_metadata_url
        client = stored.get("client", {})
        # An approved grant belongs to this connection, not to the setup process's
        # environment. Restore its identity for access/refresh without requiring
        # the old callback to be online. New authorization still requires live ingress.
        if not callback and stored.get("tokens") and client.get("redirect_uris"):
            callback = public_url(client["redirect_uris"][0])
            client_id = client.get("client_id", "")
            if client_id.startswith("https://"):
                metadata_url = public_url(client_id)
        auth = (
            PhoneOAuth(
                entry["url"],
                callback,
                Credentials(self.secrets, alias),
                client_metadata_url=metadata_url,
                allow_authorization=bool(self.callback_url),
            )
            if callback and not bearer
            else None
        )
        return StreamableHttpTransport(
            entry["url"],
            auth=auth,
            headers={"Authorization": "Bearer " + bearer} if bearer else None,
            httpx_client_factory=remote_http_client,
        )

    async def import_bearer(self, alias: str, token: str) -> dict:
        """Credential-entry adapter seam; deliberately never registered as a model tool.

        The caller must obtain the credential through an authenticated secret-entry flow.
        No credential appears in public state, arguments/results of conversational tools,
        personal memory or diagnostics. Verify before advertising the connection.
        """
        if not token or len(token) > 8192 or any(ord(c) < 33 or ord(c) > 126 for c in token):
            raise ToolError("Invalid connection credential")
        async with self.lock:
            if alias not in self.state.data:
                raise ToolError("Unknown managed connection")
            self.gateway.remove_server(alias)
            self.secrets.data[alias] = {"bearer": token}
            self.secrets.save()
            return await self._verify(alias)

    def activate(self, alias: str):
        entry = self.state.data[alias]
        self.gateway.replace_server(
            alias,
            {"url": entry["url"], "description": entry["description"]},
            target=self.transport(alias),
        )
        self.gateway.adapters[alias] = ManagedCalls(self, alias)

    def status(self, alias: str | None = None) -> dict:
        if alias and alias not in self.state.data:
            raise ToolError("Unknown managed connection; list connections to select its alias")
        entries = self.state.data.items() if alias is None else [(alias, self.state.data[alias])]
        return {
            "connections": [{"alias": name, **entry} for name, entry in entries],
            "instruction": "Stored status is cached. Verify before confirming a new authorization.",
        }

    async def connect(self, alias: str, url: str, description: str = "") -> dict:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", alias) or alias in self.reserved:
            raise ToolError("Choose a unique short connection alias using letters/numbers/_/-")
        url = public_url(url)
        if len(description) > 1000:
            raise ToolError("Connection description must be at most 1000 characters")
        async with self.lock:
            old = self.state.data.get(alias)
            if old and old["url"] != url:
                raise ToolError(
                    "Alias belongs to another endpoint; use another alias or disconnect it"
                )
            if not old:
                self.state.data[alias] = {
                    "url": url,
                    "description": description or f"Remote MCP {alias}",
                    "status": "verifying",
                    "tool_count": 0,
                }
                self.state.save()
            return await self._verify(alias)

    async def verify(self, alias: str) -> dict:
        async with self.lock:
            if alias not in self.state.data:
                raise ToolError("Unknown managed connection; list connections to select its alias")
            if self.state.data[alias]["status"] == "disconnected":
                raise ToolError("Connection is disconnected; use reconnect_connection first")
            return await self._verify(alias)

    async def _verify(self, alias: str) -> dict:
        entry = self.state.data[alias]
        self.gateway.remove_server(alias)
        entry["status"] = "verifying"
        self.state.save()
        try:
            async with asyncio.timeout(40):
                async with Client(self.transport(alias), timeout=30, init_timeout=15) as client:
                    tools = await client.list_tools()
            if not tools:
                raise ToolError("No callable tools were discovered")
        except Exception as error:
            pending = self.secrets.data.get(alias, {}).get("pending")
            if pending and pending["expires_at"] > time.time() and not pending.get("callback"):
                entry["status"] = "awaiting_authorization"
                result = {
                    "alias": alias,
                    "status": entry["status"],
                    "authorization_url": pending["link"],
                    "expires_at": pending["expires_at"],
                    "instruction": "Text this exact provider link using send_text during voice. "
                    "In chat, include the link. Ask for done, then verify_connection. "
                    "Link creation is not connection success.",
                }
            else:
                entry["status"] = "needs_attention"
                result = {
                    "alias": alias,
                    "status": entry["status"],
                    "reason": access_reason(error),
                    "instruction": "Access or tool discovery was not verified. No tools activated. "
                    "Do not request credentials in chat or suggest host-browser/file setup. "
                    + (
                        "Retry verification or reconnect if authorization was denied/expired."
                        if self.callback_url or self.secrets.data.get(alias, {}).get("tokens")
                        else "Phone OAuth callback is not provisioned on this installation; "
                        "authenticated servers need that deployment capability first."
                    ),
                }
            self.state.save()
            return result
        except BaseException:
            entry["status"] = "needs_attention"
            self.state.save()
            raise
        entry.update(status="connected", tool_count=len(tools), verified_at=time.time())
        self.state.save()
        self.activate(alias)
        return {"alias": alias, "status": "connected", "tool_count": len(tools)}

    async def reconnect(self, alias: str) -> dict:
        async with self.lock:
            if alias not in self.state.data:
                raise ToolError("Unknown managed connection")
            # Explicit reconnect replaces the old grant; ordinary verify preserves pending setup.
            self.secrets.data.pop(alias, None)
            self.secrets.save()
            return await self._verify(alias)

    async def disconnect(self, alias: str) -> dict:
        async with self.lock:
            if alias not in self.state.data:
                raise ToolError("Unknown managed connection")
            self.gateway.remove_server(alias)
            self.secrets.data.pop(alias, None)
            self.secrets.save()
            self.state.data[alias]["status"] = "disconnected"
            self.state.save()
            return {
                "alias": alias,
                "status": "disconnected",
                "instruction": "Local access and stored credentials removed. "
                "This does not revoke the provider's grant; revoke it with the provider if needed.",
            }

    async def callback(self, request: Request):
        async with self.lock:
            if len(request.scope["query_string"]) > 16384:
                return PlainTextResponse("Invalid authorization response", status_code=400)
            params = request.query_params
            if any(len(params.getlist(k)) > 1 for k in ("state", "code", "iss", "error")):
                return PlainTextResponse("Invalid authorization response", status_code=400)
            state = params.get("state", "")
            if not state or len(state) > 256:
                return PlainTextResponse("Invalid authorization response", status_code=400)
            for alias, credentials in self.secrets.data.items():
                pending = credentials.get("pending")
                if not pending or not secrets.compare_digest(state, pending["state"]):
                    continue
                if pending["expires_at"] <= time.time() or pending.get("callback"):
                    return PlainTextResponse(
                        "Authorization expired or already received", status_code=400
                    )
                if params.get("error"):
                    credentials.pop("pending")
                    self.secrets.save()
                    self.state.data[alias]["status"] = "needs_attention"
                    self.state.save()
                    return PlainTextResponse("Authorization declined. Ask Stacy to reconnect.")
                code, issuer = params.get("code"), params.get("iss")
                if (
                    not code
                    or len(code) > 4096
                    or (issuer is not None and issuer != pending["issuer"])
                    or (pending["require_iss"] and issuer is None)
                ):
                    return PlainTextResponse("Invalid authorization response", status_code=400)
                pending["callback"] = {"code": code, "state": state, "iss": issuer}
                self.secrets.save()
                return PlainTextResponse(
                    "Authorization received. Tell Stacy done to verify the connection."
                )
            return PlainTextResponse("Invalid authorization response", status_code=400)

    async def client_metadata(self, request: Request):
        """Public OAuth identity; no account, connection state or secrets are exposed."""
        return JSONResponse(
            self.public_identity.data,
            headers={"Cache-Control": "public, max-age=300"},
        )

    def _configured_client_metadata(self) -> bool:
        metadata = urlsplit(self.client_metadata_url or "")
        callback = urlsplit(self.callback_url or "")
        return (
            bool(self.callback_url)
            and metadata.path == CLIENT_METADATA_PATH
            and (metadata.scheme, metadata.netloc) == (callback.scheme, callback.netloc)
        )

    def serves_client_metadata(self) -> bool:
        return bool(self.public_identity.data)

    def oauth_app(self) -> Starlette:
        """Narrow public ingress for a proxy/tunnel; never expose the MCP/agent server.

        Authorization codes are accepted only for an unexpired, privately stored state
        and expected issuer. PKCE remains private; this surface cannot redeem a code or
        read credentials. The caller must disable proxy and server query/access logging.
        """
        routes = []
        if self.callback_url:
            routes.append(Route(CALLBACK_PATH, self.callback, methods=["GET"]))
        if self.serves_client_metadata():
            routes.append(Route(CLIENT_METADATA_PATH, self.client_metadata, methods=["GET"]))
        return Starlette(routes=routes)

    def register(self, host: FastMCP):
        @host.tool
        async def connect_mcp(alias: str, url: str, description: str = "") -> dict:
            """Connect a user-requested public HTTPS MCP endpoint and verify its tools.

            Ask only for missing URL/alias. Never accept credentials, headers or commands.
            Reuses pending authorization. Ready connections activate without a restart.
            """
            return await self.connect(alias, url, description)

        @host.tool
        async def list_connections() -> dict:
            """List managed remote MCP identities and cached setup status, excluding secrets."""
            return self.status()

        @host.tool
        async def verify_connection(alias: str) -> dict:
            """After phone sign-in/done, verify the exact connection and activate discovered tools.

            Use list_connections to resolve pending setup across voice and chat. Ask which alias
            when several setups are pending. Never assume an unrelated account completed it.
            """
            return await self.verify(alias)

        @host.tool
        async def reconnect_connection(alias: str) -> dict:
            """Explicitly restart authorization/verification for one managed remote connection."""
            return await self.reconnect(alias)

        @host.tool
        async def disconnect_connection(alias: str) -> dict:
            """Remove one managed remote connection's local access and credentials immediately."""
            return await self.disconnect(alias)

        if self.callback_url:
            host.custom_route(CALLBACK_PATH, methods=["GET"])(self.callback)
        if self.serves_client_metadata():
            host.custom_route(CLIENT_METADATA_PATH, methods=["GET"])(self.client_metadata)


class ManagedCalls:
    """Keep access failures out of cached connected state; never repeat an action."""

    def __init__(self, connections: RemoteConnections, alias: str):
        self.connections, self.alias = connections, alias

    async def discover(self, invoke):
        async with self.connections.lock:
            if self.connections.state.data[self.alias]["status"] != "connected":
                raise ToolError("Connection needs verification")
            try:
                result = await invoke()
                if result.is_error:
                    raise ToolError("Discovery failed")
                return result
            except Exception:
                self.connections.state.data[self.alias]["status"] = "needs_attention"
                self.connections.state.save()
                self.connections.gateway.remove_server(self.alias)
                raise ToolError(
                    "Connection discovery failed; verify access before using tools"
                ) from None

    async def call(self, tool, arguments, invoke):
        # Serialize against disconnect/reconnect so an old call cannot reactivate a connection.
        async with self.connections.lock:
            entry = self.connections.state.data[self.alias]
            if entry["status"] != "connected":
                raise ToolError("Connection needs verification; no action executed")
            try:
                result = await invoke(tool, arguments)
                if result.is_error:
                    raise ToolError("Remote tool did not confirm its result")
                return result
            except Exception:
                entry["status"] = "needs_attention"
                self.connections.state.save()
                self.connections.gateway.remove_server(self.alias)
                raise ToolError(
                    "Connection request failed; action may have completed. "
                    "Verify access and check the result before retrying a write."
                ) from None
