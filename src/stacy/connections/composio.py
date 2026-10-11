"""Composio discovery and operational account state, separate from personal memory."""

from __future__ import annotations

import asyncio
import copy
import json
import time
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path

from fastmcp.client.client import CallToolResult
from fastmcp.exceptions import ToolError
from fastmcp.tools import ToolResult
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from stacy.connections.calendar import ExecutionRequest, prepare_calendar, verify_events
from stacy.connections.results import Invoke, JsonObject, provider_data, result_object
from stacy.runtime.storage import atomic_write

SEARCH_DESCRIPTION = """Discover app tools, prerequisites, plans and connection status.
Call first for every external workflow; never claim an app is unavailable before searching.
New or changed task: session.generate_id=true. Continuation: session.id=returned ID.
Reuse returned session ID in all following meta calls. Use normalized English atomic queries
(one tool call per query), name the app in every query, include hidden prerequisites and
known_fields key:value hints. Start with auto; retry tool_search if the plan mismatches or
a tool is missing. Review plans/pitfalls, adapt to the user's scope and execute prerequisites
in order. Review COMPLETE input schemas and obey every constraint; schemaRef requires
COMPOSIO_GET_TOOL_SCHEMAS first. Never execute without an ACTIVE connection; use
COMPOSIO_MANAGE_CONNECTIONS when needed. Execute slugs through COMPOSIO_MULTI_EXECUTE_TOOL
with exact schema arguments. Paginate until exhaustion when complete results are requested.
Search again for additional tools after missing details, errors or a changed use case."""


class Account(BaseModel):
    id: str
    status: str
    alias: str | None = None
    user_info: JsonObject = Field(default_factory=dict)

    @field_validator("user_info")
    @classmethod
    def identity_only(cls, value: JsonObject) -> JsonObject:
        return {
            key: item for key, item in value.items() if key in {"email", "name", "id", "timeZone"}
        }

    @property
    def active(self) -> bool:
        return self.status.casefold() == "active"


class PendingConnection(BaseModel):
    toolkit: str = ""  # Older state used toolkit names as dictionary keys.
    alias: str | None = None
    account_id: str | None = None
    existing_ids: list[str] = Field(default_factory=list)
    expires_at: float
    result: JsonObject

    @property
    def key(self) -> str:
        return json.dumps([self.toolkit, self.alias])


class ConnectionState(BaseModel):
    pending: dict[str, PendingConnection] = Field(default_factory=dict)
    accounts: dict[str, list[Account]] = Field(default_factory=dict)


class ConnectionSpec(BaseModel):
    model_config = ConfigDict(extra="allow")
    name: str
    action: str
    alias: str | None = None

    @property
    def key(self) -> str:
        return json.dumps([self.name, self.alias])


class ConnectionRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    toolkits: list[ConnectionSpec]


class ConnectionListing(BaseModel):
    successful: bool | None = None
    accounts: list[Account] | None = None
    redirect_url: str | None = None
    account_id: str | None = None
    id: str | None = None


class ConnectionResponse(ConnectionListing):
    results: dict[str, ConnectionListing] = Field(default_factory=dict)


class SearchData(BaseModel):
    tool_schemas: dict[str, JsonObject] = Field(default_factory=dict)
    results: list[JsonObject] = Field(default_factory=list)
    toolkit_connection_statuses: list[JsonObject] = Field(default_factory=list)


def compact_search(payload: JsonObject) -> JsonObject:
    """Bound schemas using provider ranking; query words cannot identify a toolkit."""
    data = provider_data(payload)
    search = SearchData.model_validate(data)
    primary = [slug for result in search.results for slug in result.get("primary_tool_slugs", [])]
    ranked = list(
        dict.fromkeys(
            slug for slug in [*primary, *search.tool_schemas] if slug in search.tool_schemas
        )
    )
    data["tool_schemas"] = {slug: search.tool_schemas[slug] for slug in ranked[:5]}
    data["additional_tool_slugs"] = ranked[5:]
    return {**payload, "data": data} if "data" in payload else data


class ComposioContext:
    def __init__(self, path: Path | None, timezone: str) -> None:
        self.path = path
        self.timezone = timezone
        self.lock = asyncio.Lock()
        self._state = ConnectionState()
        if path and path.exists():
            self._state = ConnectionState.model_validate_json(path.read_text())
            pending = {}
            for key, item in self._state.pending.items():
                item.toolkit = item.toolkit or key
                pending[item.key] = item
            self._state.pending = pending
        self._checked: dict[str, float] = {}  # Verify again after process restart.

    def status(self) -> JsonObject:
        return {
            **self._state.model_dump(
                exclude={"pending": {"__all__": {"result"}}}, exclude_none=True
            ),
            "instruction": "Verify intended account via provider action=list before confirming",
        }

    def _save(self) -> None:
        if self.path is not None:
            atomic_write(self.path, self._state.model_dump_json(exclude_none=True))

    def _update_accounts(self, toolkit: str, accounts: list[Account]) -> None:
        previous = {account.id: account.alias for account in self._state.accounts.get(toolkit, [])}
        active = {account.id: account for account in accounts if account.active}
        pending = [item for item in self._state.pending.values() if item.toolkit == toolkit]
        for account in accounts:
            account.alias = account.alias or previous.get(account.id)
        for item in pending:
            candidates = [active[item.account_id]] if item.account_id in active else []
            if item.account_id is None and len(pending) == 1:
                candidates = [
                    account for account in active.values() if account.id not in item.existing_ids
                ]
            if len(candidates) == 1:
                candidates[0].alias = item.alias or candidates[0].alias
                del self._state.pending[item.key]
        self._state.accounts[toolkit] = accounts
        self._checked[toolkit] = time.monotonic()

    def connections(self, payload: JsonObject, toolkits: Sequence[str] = ()) -> set[str]:
        data = provider_data(payload)
        response = ConnectionResponse.model_validate(data)
        listings = response.results
        if not listings and len(toolkits) == 1:
            listings = {toolkits[0]: response}
        updated = set()
        for toolkit, entry in listings.items():
            if entry.accounts is not None and entry.successful is not False:
                self._update_accounts(toolkit, entry.accounts)
                updated.add(toolkit)
                target = data["results"][toolkit] if "results" in data else data
                target["accounts"] = [
                    account.model_dump(exclude_none=True) for account in entry.accounts
                ]
        if "data" in payload:
            payload["data"] = data
        else:
            payload.update(data)
        self._save()
        return updated

    def _account(self, selection: str | None) -> str:
        active = [
            account for account in self._state.accounts.get("googlecalendar", []) if account.active
        ]
        matches = (
            [account for account in active if selection in {account.id, account.alias}]
            if selection
            else active
        )
        if len(matches) != 1:
            raise ToolError(
                "Error: Select the intended verified active Google account; "
                "no calendar action executed"
            )
        return matches[0].id

    async def _calendar_request(self, args: JsonObject, invoke: Invoke) -> JsonObject:
        request = ExecutionRequest.model_validate(args)
        calendars = [
            action for action in request.tools if action.tool_slug.startswith("GOOGLECALENDAR_")
        ]
        if not calendars:
            return args
        if time.monotonic() - self._checked.get("googlecalendar", float("-inf")) > 30:
            query: JsonObject = {"toolkits": [{"name": "googlecalendar", "action": "list"}]}
            if request.session_id:
                query["session_id"] = request.session_id
            status = await invoke("COMPOSIO_MANAGE_CONNECTIONS", query)
            payload = result_object(status)
            if status.is_error or payload is None or payload.get("successful") is False:
                raise ToolError("Error: Calendar account unverified; no action executed")
            updated = self.connections(payload, ["googlecalendar"])
            if "googlecalendar" not in updated:
                raise ToolError("Error: Calendar account unverified; no action executed")
        for action in calendars:
            action.account = self._account(action.account)
            prepare_calendar(action, self.timezone)
        return request.model_dump(exclude_none=True)

    async def discover(self, invoke: Callable[[], Awaitable[CallToolResult]]) -> CallToolResult:
        return await invoke()

    async def call(
        self, tool: str, arguments: JsonObject, invoke: Invoke
    ) -> CallToolResult | ToolResult:
        async with self.lock:
            args = copy.deepcopy(arguments)
            try:
                request = (
                    ConnectionRequest.model_validate(args)
                    if tool == "COMPOSIO_MANAGE_CONNECTIONS"
                    else None
                )
                if request and len(request.toolkits) == 1:
                    spec = request.toolkits[0]
                    pending = self._state.pending.get(spec.key)
                    if spec.action == "add" and pending and pending.expires_at > time.time():
                        return ToolResult(structured_content=copy.deepcopy(pending.result))
                if tool == "COMPOSIO_MULTI_EXECUTE_TOOL":
                    args = await self._calendar_request(args, invoke)
            except ValidationError:
                raise ToolError(
                    "Error: Invalid connection or calendar arguments; no action executed"
                ) from None
            result = await invoke(tool, args)
            payload = result_object(result)
            if result.is_error or payload is None or payload.get("successful") is False:
                return result
            if tool == "COMPOSIO_SEARCH_TOOLS":
                payload = compact_search(payload)
                for status in provider_data(payload).get("toolkit_connection_statuses", []):
                    if "accounts" in status:
                        accounts = [Account.model_validate(a) for a in status["accounts"]]
                        self._update_accounts(status["toolkit"], accounts)
                        status["accounts"] = [
                            account.model_dump(exclude_none=True) for account in accounts
                        ]
                self._save()
            elif request is not None:
                self.connections(payload, [spec.name for spec in request.toolkits])
                response = ConnectionResponse.model_validate(provider_data(payload))
                for spec in request.toolkits:
                    entry = response.results.get(spec.name)
                    if entry is None and len(request.toolkits) == 1:
                        entry = response
                    if spec.action == "add" and entry is not None and entry.redirect_url:
                        self._state.pending[spec.key] = PendingConnection(
                            toolkit=spec.name,
                            alias=spec.alias,
                            account_id=entry.account_id or entry.id,
                            existing_ids=[
                                account.id
                                for account in self._state.accounts.get(spec.name, [])
                                if account.active
                            ],
                            expires_at=time.time() + 600,
                            result=copy.deepcopy(payload),
                        )
                self._save()
            elif tool == "COMPOSIO_MULTI_EXECUTE_TOOL":
                verify_events(payload, args)
            return ToolResult(structured_content=payload)

    verify_events = staticmethod(verify_events)
