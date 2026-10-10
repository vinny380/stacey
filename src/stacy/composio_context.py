"""Bound Composio context and retain operational connection state outside memory."""

from __future__ import annotations

import asyncio
import copy
import json
import os
import time
from datetime import datetime
from pathlib import Path

from fastmcp.exceptions import ToolError
from fastmcp.tools import ToolResult

ALIASES = {
    "googlecalendar": ("calendar",),
    "gmail": ("gmail", "email"),
    "slack": ("slack",),
    "googlesheets": ("spreadsheet", "sheets"),
    "googledrive": ("drive",),
}

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


def json_result(result):
    if isinstance(result.structured_content, dict):
        return copy.deepcopy(result.structured_content)
    # MCP providers may append prose or provide several content blocks.
    for block in result.content:
        value = getattr(block, "text", "").lstrip()
        try:
            parsed, _ = json.JSONDecoder().raw_decode(value)
            if isinstance(parsed, dict):
                return parsed
        except ValueError:
            continue
    return None


def without_previews(value):
    if isinstance(value, list):
        return [without_previews(v) for v in value]
    if not isinstance(value, dict):
        return value
    return {
        k: (
            {i: v for i, v in item.items() if i in {"email", "name", "id", "timeZone"}}
            if k == "user_info" and isinstance(item, dict)
            else without_previews(item)
        )
        for k, item in value.items()
    }


def compact_search(payload, arguments):
    data = payload.get("data", payload)
    if not isinstance(data, dict) or not isinstance(data.get("tool_schemas"), dict):
        return payload
    text = " ".join(q.get("use_case", "") for q in arguments.get("queries", [])).casefold()
    requested = {slug for slug, aliases in ALIASES.items() if any(a in text for a in aliases)}
    schemas = data["tool_schemas"]
    allowed = {
        slug: schema
        for slug, schema in schemas.items()
        if not requested or schema.get("toolkit", "").casefold() in requested
    }
    primary = [
        slug
        for result in data.get("results", [])
        for slug in result.get("primary_tool_slugs", [])
        if slug in allowed
    ]
    ranked = list(dict.fromkeys([*primary, *allowed]))
    selected = ranked[:5]
    out = {k: v for k, v in data.items() if k not in {"results", "tool_schemas"}}
    out["tool_schemas"] = {slug: allowed[slug] for slug in selected}
    out["additional_tool_slugs"] = ranked[5:]
    out["results"] = []
    for result in data.get("results", []):
        kept = {
            k: v
            for k, v in result.items()
            if k
            in {
                "index",
                "use_case",
                "execution_guidance",
                "known_pitfalls",
                "difficulty",
                "recommended_plan_steps",
            }
        }
        for field in ("primary_tool_slugs", "related_tool_slugs"):
            kept[field] = [slug for slug in result.get(field, []) if slug in allowed]
        out["results"].append(kept)
    if requested:
        out["toolkit_connection_statuses"] = [
            s
            for s in data.get("toolkit_connection_statuses", [])
            if s.get("toolkit", "").casefold() in requested
        ]
    return {**payload, "data": out} if "data" in payload else out


class ComposioContext:
    def __init__(self, path: Path | None, timezone: str):
        self.path, self.timezone = path, timezone
        self.lock = asyncio.Lock()
        self.state = {"pending": {}, "accounts": {}}
        if path and path.exists():
            self.state = json.loads(path.read_text())
        self.accounts_checked = 0.0  # refresh after process restart

    def save(self):
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temp = self.path.with_suffix(".tmp")
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as file:
            json.dump(self.state, file)
        os.replace(temp, self.path)

    def connections(self, payload):
        payload = without_previews(payload)
        data = payload.get("data", payload)
        results = data.get("results", {})
        if not isinstance(results, dict):
            return
        for toolkit, entry in results.items():
            if not isinstance(entry, dict):
                continue
            if "accounts" in entry:
                previous = {
                    a["id"]: a.get("alias") for a in self.state["accounts"].get(toolkit, [])
                }
                for account in entry["accounts"]:
                    if previous.get(account.get("id")) and not account.get("alias"):
                        account["alias"] = previous[account["id"]]
                self.state["accounts"][toolkit] = entry["accounts"]
                self.accounts_checked = time.monotonic()
                pending = self.state["pending"].get(toolkit)
                completed = [
                    a
                    for a in entry["accounts"]
                    if pending
                    and (
                        (
                            a.get("id") == pending.get("account_id")
                            or (
                                pending.get("account_id") is None
                                and a.get("id") not in pending.get("existing_ids", [])
                                and sum(
                                    1
                                    for new in entry["accounts"]
                                    if new.get("id") not in pending.get("existing_ids", [])
                                    and new.get("status", "").casefold() == "active"
                                )
                                == 1
                            )
                        )
                        and a.get("status", "").casefold() == "active"
                    )
                ]
                if len(completed) == 1:
                    if pending.get("alias"):
                        completed[0]["alias"] = pending["alias"]
                    del self.state["pending"][toolkit]
        self.save()

    async def call(self, tool, arguments, invoke):
        async with self.lock:
            args = copy.deepcopy(arguments)
            if tool == "COMPOSIO_MANAGE_CONNECTIONS":
                specs = args.get("toolkits", [])
                if len(specs) == 1 and specs[0].get("action") == "add":
                    spec = specs[0]
                    pending = self.state["pending"].get(spec["name"])
                    if (
                        pending
                        and pending["expires_at"] > time.time()
                        and pending.get("alias") == spec.get("alias")
                    ):
                        return ToolResult(structured_content=pending["result"])
            if tool == "COMPOSIO_MULTI_EXECUTE_TOOL":
                calendars = [
                    t
                    for t in args.get("tools", [])
                    if t.get("tool_slug", "").startswith("GOOGLECALENDAR_")
                ]
                if calendars and time.monotonic() - self.accounts_checked > 30:
                    status = await invoke(
                        "COMPOSIO_MANAGE_CONNECTIONS",
                        {
                            "toolkits": [{"name": "googlecalendar", "action": "list"}],
                            **(
                                {"session_id": args["session_id"]} if args.get("session_id") else {}
                            ),
                        },
                    )
                    parsed = json_result(status)
                    if status.is_error or not parsed or parsed.get("successful") is False:
                        raise ToolError("Error: Calendar account unverified; no action executed")
                    self.connections(without_previews(parsed))
                active = [
                    a
                    for a in self.state["accounts"].get("googlecalendar", [])
                    if a.get("status", "").casefold() == "active"
                ]
                for action in calendars:
                    account = action.get("account")
                    if not account and len(active) == 1:
                        action["account"] = active[0]["id"]
                    elif not account:
                        raise ToolError(
                            "Error: Select the intended connected Google account; "
                            "no calendar action executed"
                        )
                    elif not any(account in {a.get("id"), a.get("alias")} for a in active):
                        raise ToolError(
                            "Error: Selected calendar account is not verified active; "
                            "no action executed"
                        )
                    else:
                        matches = [a for a in active if account in {a.get("id"), a.get("alias")}]
                        if len(matches) != 1:
                            raise ToolError("Error: Account alias is ambiguous; no action executed")
                        action["account"] = matches[0]["id"]
                    if action["tool_slug"] == "GOOGLECALENDAR_CREATE_EVENT":
                        fields = action.setdefault("arguments", {})
                        if not fields.get("timezone"):
                            fields["timezone"] = self.timezone
                        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

                        try:
                            ZoneInfo(fields["timezone"])
                        except (ZoneInfoNotFoundError, TypeError, ValueError):
                            raise ToolError(
                                "Error: Use an IANA calendar timezone; no event created"
                            )
                        if not fields.get("calendar_id"):
                            raise ToolError(
                                "Error: Choose calendar_id explicitly before creating "
                                "an event (primary for the selected account's main "
                                "calendar); no event created"
                            )
            result = await invoke(tool, args)
            payload = json_result(result)
            if result.is_error or payload is None:
                return result
            if tool == "COMPOSIO_SEARCH_TOOLS":
                payload = without_previews(compact_search(payload, args))
                data = payload.get("data", payload)
                for status in data.get("toolkit_connection_statuses", []):
                    if "accounts" in status:
                        self.state["accounts"][status["toolkit"]] = status["accounts"]
                self.save()
            elif tool == "COMPOSIO_MANAGE_CONNECTIONS":
                payload = without_previews(payload)
                self.connections(payload)
                if payload.get("successful") is not False:
                    for spec in args.get("toolkits", []):
                        if spec.get("action") != "add":
                            continue
                        data = payload.get("data", payload)
                        entry = data.get("results", {}).get(spec["name"], data)
                        if entry.get("redirect_url"):
                            self.state["pending"][spec["name"]] = {
                                "alias": spec.get("alias"),
                                "account_id": entry.get("account_id") or entry.get("id"),
                                "existing_ids": [
                                    a.get("id")
                                    for a in self.state["accounts"].get(spec["name"], [])
                                    if a.get("status", "").casefold() == "active"
                                ],
                                "expires_at": time.time() + 600,
                                "result": payload,
                            }
                            self.save()
            elif tool == "COMPOSIO_MULTI_EXECUTE_TOOL":
                # Verify returned event fields against the requested values.
                # This never repeats the mutation when verification fails.
                self.verify_events(payload, args)
            return ToolResult(structured_content=payload)

    @staticmethod
    def verify_events(payload, arguments):
        data = payload.get("data", payload)
        for entry in data.get("results", []):
            if entry.get("tool_slug") != "GOOGLECALENDAR_CREATE_EVENT":
                continue
            response = entry.get("response", {})
            event = response.get("data", {})
            event = event.get("response_data", event)
            index = entry.get("index", 0)
            requested = arguments["tools"][index]["arguments"]
            problems = []
            if not response.get("successful") or not event.get("id"):
                problems.append("Event creation was not confirmed")
            for field, native in (("start_datetime", "start"), ("end_datetime", "end")):
                actual = event.get(native, {})
                try:
                    expected = datetime.fromisoformat(requested[field])
                    returned = datetime.fromisoformat(actual["dateTime"])
                    if expected.tzinfo is None:
                        from zoneinfo import ZoneInfo

                        expected = expected.replace(tzinfo=ZoneInfo(requested["timezone"]))
                    if expected != returned:
                        problems.append(f"Returned {native} time differs from request")
                except (KeyError, ValueError):
                    problems.append(f"Could not verify {native} time")
                if actual.get("timeZone") != requested["timezone"]:
                    problems.append(f"Returned {native} timezone differs from request")
            if event.get("summary") != requested.get("summary"):
                problems.append("Returned event title differs from request")
            entry["stacy_verification"] = {
                "confirmed": not problems,
                "problems": problems,
                "account": arguments["tools"][index].get("account"),
                "calendar_id": requested.get("calendar_id"),
                "instruction": "Do not retry creation. Report any verification uncertainty.",
            }
