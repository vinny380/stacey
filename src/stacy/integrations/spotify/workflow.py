"""Small, schema-validated Spotify workflows over the existing Composio connection.

The model chooses music and the target. Code owns discovery, compact results and
command/read ordering. Cached capabilities are never playback verification.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from contextlib import nullcontext
from hashlib import sha256
from typing import Literal
from weakref import WeakKeyDictionary

from fastmcp import FastMCP
from jsonschema import validators

from stacy.connections.results import result_object
from stacy.harness.tracing import active_text_trace

SEARCH = "SPOTIFY_SEARCH_FOR_ITEM"
DEVICES = "SPOTIFY_GET_AVAILABLE_DEVICES"
PLAY = "SPOTIFY_START_RESUME_PLAYBACK"
PAUSE = "SPOTIFY_PAUSE_PLAYBACK"
STATE = "SPOTIFY_GET_PLAYBACK_STATE"
NEXT = "SPOTIFY_SKIP_TO_NEXT"
PREVIOUS = "SPOTIFY_SKIP_TO_PREVIOUS"
CAPABILITIES = {SEARCH, DEVICES, PLAY, PAUSE, STATE, NEXT, PREVIOUS}


class WorkflowUnavailable(Exception):
    pass


def _complete_schema(value):
    if not isinstance(value, dict) or value.get("type") != "object":
        return False
    if not isinstance(value.get("properties"), dict):
        return False

    def external_reference(item):
        if isinstance(item, dict):
            return (isinstance(item.get("$ref"), str) and not item["$ref"].startswith("#")) or any(
                external_reference(v) for v in item.values()
            )
        return isinstance(item, list) and any(external_reference(v) for v in item)

    if external_reference(value):
        return False
    try:
        validators.validator_for(value).check_schema(value)
        return True
    except Exception:
        return False


class SpotifyWorkflow:
    def __init__(
        self,
        invoke: Callable[[str, dict], Awaitable],
        *,
        metadata: Callable[[], Awaitable[dict]],
        clock: Callable[[], float] = time.monotonic,
        ttl_seconds: float = 300,
        scope: Callable | None = None,
    ):
        self.invoke, self.metadata, self.clock = invoke, metadata, clock
        self.ttl = ttl_seconds
        self.scope = scope or nullcontext
        self.lock = asyncio.Lock()
        self.schemas: dict = {}
        self.meta: dict = {}
        self.accounts: list[dict] = []
        self.session: str | None = None
        self.observed_at = float("-inf")
        # A model retry after a lost write response must not replay that write.
        # Weak turn keys disappear when the trace/turn ends; no personal data is persisted.
        self.attempts: WeakKeyDictionary = WeakKeyDictionary()

    def invalidate(self):
        self.schemas, self.meta, self.accounts = {}, {}, []
        self.session = None
        self.observed_at = float("-inf")

    async def prepare(self) -> dict:
        """Warm the bounded provider schema/account registry; never play, connect or authorize."""
        try:
            async with self.scope(), self.lock:
                await self._ensure(CAPABILITIES)
            return {"ready": True, "capability_count": len(self.schemas)}
        except Exception as error:
            self._record_failure("prepare", error)
            self.invalidate()
            return {"ready": False}

    def _record_failure(self, operation, error):
        trace = active_text_trace.get()
        if trace:
            trace.record(
                "workflow_failed",
                workflow="spotify",
                operation=operation,
                error_type=type(error).__name__,
                reason=str(error)
                if isinstance(error, WorkflowUnavailable)
                else "Provider request failed",
            )

    @staticmethod
    def _validate(definition, arguments):
        if not _complete_schema(definition):
            raise WorkflowUnavailable("Complete provider input schema unavailable")
        try:
            validators.validator_for(definition)(definition).validate(arguments)
        except Exception:
            # Validation details can contain provider data; return no raw exceptions.
            raise WorkflowUnavailable(
                "Arguments do not satisfy the current provider schema"
            ) from None

    async def _meta_call(self, name, arguments):
        self._validate(self.meta.get(name), arguments)
        result = await self.invoke(name, arguments)
        payload = result_object(result)
        if (
            result.is_error
            or not isinstance(payload, dict)
            or payload.get("successful") is False
            or payload.get("success") is False
        ):
            raise WorkflowUnavailable("Provider request failed; outcome is unconfirmed")
        data = payload.get("data", payload)
        if not isinstance(data, dict) or data.get("success") is False:
            raise WorkflowUnavailable("Provider returned an unsupported or failed result")
        return data

    async def _ensure(self, required):
        valid = self.clock() - self.observed_at < self.ttl
        trace = active_text_trace.get()
        if trace:
            trace.record("workflow_capability_cache", workflow="spotify", hit=valid)
        if not valid:
            self.invalidate()
            self.meta = await self.metadata()
            data = await self._meta_call(
                "COMPOSIO_SEARCH_TOOLS",
                {
                    "queries": [
                        {"use_case": query}
                        for query in (
                            "Search Spotify music catalog",
                            "Get available Spotify playback devices",
                            "Start Spotify playback on a selected device",
                            "Pause Spotify on a selected device",
                            "Get current Spotify playback state to verify a command",
                            "Skip to the next Spotify track on a selected playback device",
                            "Skip to the previous Spotify track on a selected playback device",
                        )
                    ],
                    "session": {"generate_id": True},
                    "search_strategy": "auto",
                },
            )
            self.session = data.get("session", {}).get("id")
            if not isinstance(self.session, str) or not self.session:
                raise WorkflowUnavailable("Provider workflow session unavailable")
            self.accounts = [
                {k: a[k] for k in ("id", "alias", "status") if k in a}
                for toolkit in data.get("toolkit_connection_statuses", [])
                if toolkit.get("toolkit", "").casefold() == "spotify"
                for a in toolkit.get("accounts", [])
                if a.get("status", "").upper() == "ACTIVE" and a.get("id")
            ]
            definitions = data.get("tool_schemas", {})
            known = set(definitions) | set(data.get("additional_tool_slugs", []))
            for row in data.get("results", []):
                known.update(row.get("primary_tool_slugs", []))
            self.schemas = {
                slug: item["input_schema"]
                for slug, item in definitions.items()
                if slug in CAPABILITIES
                and item.get("hasFullSchema") is not False
                and _complete_schema(item.get("input_schema"))
            }
            # Preload the tiny fixed capability set when returned by search. Only fetch
            # discovered slugs, and retain full definitions outside the model context.
            missing = (CAPABILITIES & known) - self.schemas.keys()
            if missing:
                loaded = await self._meta_call(
                    "COMPOSIO_GET_TOOL_SCHEMAS",
                    {
                        "tool_slugs": sorted(missing),
                        "include": ["input_schema"],
                        "session_id": self.session,
                    },
                )
                for slug, item in loaded.get("tool_schemas", {}).items():
                    if (
                        slug in missing
                        and item.get("hasFullSchema") is not False
                        and _complete_schema(item.get("input_schema"))
                    ):
                        self.schemas[slug] = item["input_schema"]
            self.observed_at = self.clock()
            if trace:
                for slug, definition in self.schemas.items():
                    trace.record(
                        "workflow_schema_ready",
                        workflow="spotify",
                        tool_slug=slug,
                        input_schema=definition,
                        schema_sha256=sha256(
                            json.dumps(definition, sort_keys=True).encode()
                        ).hexdigest(),
                    )
        if not required <= self.schemas.keys():
            raise WorkflowUnavailable(
                "Required Spotify capability/schema unavailable; use discovery"
            )

    def _account(self, requested):
        if not self.accounts:
            return {
                "status": "connection_required",
                "verified": False,
                "instruction": "Connect Spotify conversationally before executing.",
            }
        matches = [a for a in self.accounts if requested in {a["id"], a.get("alias")}]
        if requested is None and len(self.accounts) == 1:
            matches = self.accounts
        if len(matches) != 1:
            return {
                "status": "account_required",
                "verified": False,
                "accounts": self.accounts,
                "instruction": "Ask which Spotify account; do not select a default.",
            }
        return matches[0]["id"]

    async def _batch(self, actions, account):
        for slug, arguments in actions:
            self._validate(self.schemas.get(slug), arguments)
            trace = active_text_trace.get()
            if trace:
                trace.record(
                    "workflow_schema_validated",
                    workflow="spotify",
                    tool_slug=slug,
                    account=account,
                    arguments=arguments,
                    schema_age_seconds=self.clock() - self.observed_at,
                )
        data = await self._meta_call(
            "COMPOSIO_MULTI_EXECUTE_TOOL",
            {
                "tools": [
                    {"tool_slug": slug, "arguments": args, "account": account}
                    for slug, args in actions
                ],
                "session_id": self.session,
                "sync_response_to_workbench": False,
                "current_step": "SPOTIFY_WORKFLOW",
                "current_step_metric": "1/1 operations",
                "thought": "Execute the requested Spotify operation with the selected account.",
            },
        )
        rows = data.get("results", [])
        if data.get("error_count", 0) or len(rows) != len(actions):
            raise WorkflowUnavailable("Provider operation failed or returned incomplete results")
        output = {}
        for slug, _ in actions:
            matches = [r for r in rows if r.get("tool_slug") == slug]
            if len(matches) != 1 or matches[0].get("response", {}).get("successful") is not True:
                raise WorkflowUnavailable("Provider operation did not confirm success")
            response = matches[0]["response"]
            value = response.get("data", response.get("response_data"))
            if not isinstance(value, dict) or value.get("success") is False:
                raise WorkflowUnavailable("Provider operation result unavailable")
            output[slug] = value
        return output

    @staticmethod
    def _failure(status="unavailable", *, accepted=False):
        return {
            "status": status,
            "accepted": accepted,
            "verified": False,
            "instruction": "Result unconfirmed. Read current playback; never retry a write blindly."
            if status == "uncertain"
            else "No command executed. Resolve connection/schema/account through discovery.",
        }

    async def find(
        self,
        query: str,
        device_name: str,
        account: str | None = None,
        kind: Literal["playlist", "track", "album"] = "playlist",
    ) -> dict:
        async with self.lock:
            try:
                await self._ensure({SEARCH, DEVICES})
                selected = self._account(account)
                if isinstance(selected, dict):
                    return selected
                data = await self._batch(
                    [(SEARCH, {"q": query, "type": [kind], "limit": 10}), (DEVICES, {})], selected
                )
                devices = [
                    d
                    for d in data[DEVICES].get("devices", [])
                    if d.get("id") and not d.get("is_restricted")
                ]
                name = device_name.strip().casefold()
                exact = [d for d in devices if name in {d["id"].casefold(), d["name"].casefold()}]
                matches = exact or [
                    d for d in devices if len(name) >= 3 and name in d["name"].casefold()
                ]
                targets = [{"id": d["id"], "name": d["name"]} for d in matches]
                if len(targets) != 1:
                    return {
                        "status": "device_required",
                        "account": selected,
                        "devices": targets or [{"id": d["id"], "name": d["name"]} for d in devices],
                        "instruction": "Resolve the requested device before playing.",
                    }
                candidates = [
                    {
                        "name": item.get("name"),
                        "uri": item["uri"],
                        "artists": [a.get("name") for a in item.get("artists", [])],
                    }
                    for item in data[SEARCH].get(kind + "s", {}).get("items", [])
                    if isinstance(item, dict)
                    and isinstance(item.get("uri"), str)
                    and item["uri"].startswith(f"spotify:{kind}:")
                ][:5]
                return {
                    "status": "ready",
                    "account": selected,
                    "device": targets[0],
                    "candidates": candidates,
                    "instruction": "Choose a returned URI and use composio_spotify_control.",
                }
            except Exception as error:
                self._record_failure("find", error)
                self.invalidate()
                return self._failure()

    @staticmethod
    def _playback(data, account):
        item = data.get("item") or {}
        device = data.get("device") or {}
        return {
            "account": account,
            "is_playing": data.get("is_playing"),
            "device": {k: device[k] for k in ("id", "name") if k in device},
            "item": {
                "name": item.get("name"),
                "uri": item.get("uri"),
                "artists": [a.get("name") for a in item.get("artists", [])],
            },
            "context_uri": (data.get("context") or {}).get("uri"),
        }

    async def play(self, query: str, device_name: str, account: str | None = None) -> dict:
        """Choose Spotify's highest ranked playlist for an open genre/mood request."""
        if not query.strip():
            return {"status": "music_required", "accepted": False, "verified": False}
        trace = active_text_trace.get()
        attempts = self.attempts.setdefault(trace, {}) if trace else {}
        key = ("playlist", device_name.strip().casefold(), account, query.strip().casefold())
        if key in attempts:
            return attempts[key]
        selected_account = self._account(account)
        canonical = (
            (*key[:2], selected_account, key[3]) if isinstance(selected_account, str) else key
        )
        if canonical in attempts:
            return attempts[canonical]
        found = await self.find(query, device_name, account)
        if found.get("status") != "ready":
            return found
        if not found["candidates"]:
            return {
                "status": "music_required",
                "accepted": False,
                "verified": False,
                "instruction": "No playlist matched; ask for different music.",
            }
        choice = found["candidates"][0]
        result = await self.control("play", found["device"]["id"], found["account"], choice["uri"])
        result = {**result, "selection": choice}
        if result.get("accepted") is not False:
            canonical = (*key[:2], found["account"], key[3])
            attempts[key] = attempts[canonical] = result
        return result

    async def control(
        self,
        action: Literal["play", "pause", "next", "previous"],
        device_id: str,
        account: str,
        uri: str | None = None,
    ) -> dict:
        async with self.lock:
            trace = active_text_trace.get()
            key = (action, device_id, account, uri)
            canonical_key = key
            attempts = self.attempts.setdefault(trace, {}) if trace else {}
            if key in attempts:
                return attempts[key]
            issued, accepted = False, False
            try:
                if (
                    action not in {"play", "pause", "next", "previous"}
                    or not device_id
                    or not account
                ):
                    raise WorkflowUnavailable("An explicit action, account and target are required")
                slug = {"play": PLAY, "pause": PAUSE, "next": NEXT, "previous": PREVIOUS}[action]
                await self._ensure({slug, STATE})
                selected = self._account(account)
                if isinstance(selected, dict):
                    return selected
                canonical_key = (action, device_id, selected, uri)
                # An account alias and its provider ID refer to the same write.
                # Remember both spellings so a lost response cannot be replayed
                # merely by changing how the account is named.
                if canonical_key in attempts:
                    return attempts[canonical_key]
                arguments = {"device_id": device_id}
                # Explicit device targeting must be supported, even if a permissive
                # schema would otherwise accept unknown fields.
                if "device_id" not in self.schemas[slug]["properties"]:
                    raise WorkflowUnavailable("Explicit device targeting unavailable")
                if uri is not None:
                    if action != "play" or not isinstance(uri, str):
                        raise WorkflowUnavailable("Invalid playback URI")
                    if uri.startswith("spotify:track:"):
                        arguments["uris"] = [uri]
                    elif uri.startswith(("spotify:playlist:", "spotify:album:", "spotify:artist:")):
                        arguments["context_uri"] = uri
                    else:
                        raise WorkflowUnavailable("Use an actual Spotify URI")
                self._validate(self.schemas[slug], arguments)
                self._validate(self.schemas[STATE], {})
                before = None
                if action in {"next", "previous"}:
                    before = self._playback(
                        (await self._batch([(STATE, {})], selected))[STATE], selected
                    )
                    if before["device"].get("id") != device_id:
                        raise WorkflowUnavailable(
                            "Requested Spotify device is not the current player"
                        )
                issued = True
                await self._batch([(slug, arguments)], selected)
                accepted = True
                if trace:
                    trace.record(
                        "workflow_action_ack",
                        workflow="spotify",
                        action=action,
                        account=selected,
                        device_id=device_id,
                    )
                # A Composio multi-execute batch is parallel: dependent verification
                # is a separate awaited request, never another item in the write batch.
                observed = (await self._batch([(STATE, {})], selected))[STATE]
                result = self._playback(observed, selected)
                result.update(
                    accepted=True,
                    verified=(
                        result["device"].get("id") == device_id
                        and (
                            result["is_playing"] is (action == "play")
                            if before is None
                            else bool(result["item"].get("uri"))
                            and result["item"]["uri"] != before["item"].get("uri")
                        )
                        and (uri is None or uri in {result["context_uri"], result["item"]["uri"]})
                    ),
                )
                result["status"] = "verified" if result["verified"] else "unverified"
                if not result["verified"]:
                    result["instruction"] = (
                        "Command accepted; state/target not verified. Read again; do not replay."
                    )
            except Exception as error:
                self._record_failure(action, error)
                self.invalidate()
                result = self._failure(
                    "uncertain" if issued else "unavailable",
                    accepted=True if accepted else None if issued else False,
                )
            if issued:
                attempts[key] = result
                attempts[canonical_key] = result
            return result

    async def read(self, account: str) -> dict:
        async with self.lock:
            try:
                await self._ensure({STATE})
                selected = self._account(account)
                if isinstance(selected, dict):
                    return selected
                data = (await self._batch([(STATE, {})], selected))[STATE]
                return self._playback(data, selected)
            except Exception as error:
                self._record_failure("read", error)
                self.invalidate()
                return self._failure()

    def register(self, host: FastMCP, *, playlist: bool = True):
        if playlist:

            @host.tool
            async def composio_spotify_play(
                query: str, device_name: str, account: str | None = None
            ) -> dict:
                """Play some music by genre/mood on a named Spotify device and verify fresh state.

                For open requests like 'some chill classical on my Echo', choose Spotify's top
                ranked matching playlist. Resolves account/device, full schemas and actual URI,
                plays once, then separately verifies state. Ask on account/device ambiguity.
                No preceding discovery/find call needed. For an exact track/album or deciding
                between named choices, use composio_spotify_find then composio_spotify_control.
                If uncertain, only read playback; never repeat this command for confirmation.
                """
                async with self.scope():
                    return await self.play(query, device_name, account)

        @host.tool
        async def composio_spotify_find(
            query: str,
            device_name: str,
            account: str | None = None,
            kind: Literal["playlist", "track", "album"] = "playlist",
        ) -> dict:
            """Find actual Spotify music URIs and uniquely resolve a playback device via Composio.

            Handles discovery/schemas internally. Returns choices and explicit account/device.
            Ask when account/device is ambiguous. Choose a URI and call composio_spotify_control.
            """
            async with self.scope():
                return await self.find(query, device_name, account, kind)

        @host.tool
        async def composio_spotify_control(
            action: Literal["play", "pause", "next", "previous"],
            device_id: str,
            account: str,
            uri: str | None = None,
        ) -> dict:
            """Play, pause or skip next/previous on an explicit Spotify device; verify fresh state.

            Supply the account/device from previous results; uri is needed for new music only.
            Validates full schemas, executes once, then reads state. Confirm only if verified.
            For 'stop it', pause the previous Composio account/device directly; no discovery call.
            If uncertain/unverified, read playback; never repeat a write to get confirmation.
            """
            async with self.scope():
                return await self.control(action, device_id, account, uri)

        @host.tool
        async def composio_spotify_read(account: str) -> dict:
            """Read fresh Composio Spotify playback for an explicit account, without any mutation.

            Use after uncertain/unverified results. Check device ID and state before confirming.
            """
            async with self.scope():
                return await self.read(account)
