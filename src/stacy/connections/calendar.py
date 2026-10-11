"""Calendar request preparation and verification; mutations are never repeated."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastmcp.exceptions import ToolError
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from stacy.connections.results import JsonObject, provider_data

CREATE_EVENT = "GOOGLECALENDAR_CREATE_EVENT"


class Execution(BaseModel):
    model_config = ConfigDict(extra="allow")
    tool_slug: str
    account: str | None = None
    arguments: JsonObject = Field(default_factory=dict)


class ExecutionRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    tools: list[Execution]
    session_id: str | None = None


class EventTime(BaseModel):
    date_time: datetime | None = Field(default=None, alias="dateTime")
    timezone: str | None = Field(default=None, alias="timeZone")


class CalendarEvent(BaseModel):
    id: str | None = None
    summary: str | None = None
    start: EventTime = Field(default_factory=EventTime)
    end: EventTime = Field(default_factory=EventTime)


class EventResult(BaseModel):
    tool_slug: str
    index: int | None = Field(default=None, strict=True)
    response: JsonObject = Field(default_factory=dict)


def prepare_calendar(action: Execution, timezone: str) -> None:
    if action.tool_slug != CREATE_EVENT:
        return
    fields = action.arguments
    fields["timezone"] = fields.get("timezone") or timezone
    try:
        ZoneInfo(fields["timezone"])
    except (ZoneInfoNotFoundError, TypeError, ValueError):
        raise ToolError("Error: Use an IANA calendar timezone; no event created") from None
    if not fields.get("calendar_id"):
        raise ToolError(
            "Error: Choose calendar_id explicitly before creating an event; no event created"
        )


def _verify_event(response: JsonObject, requested: JsonObject) -> list[str]:
    data = provider_data(response)
    event = CalendarEvent.model_validate(data.get("response_data", data))
    problems = []
    if not response.get("successful") or not event.id:
        problems.append("Event creation was not confirmed")
    for field, native, actual in (
        ("start_datetime", "start", event.start),
        ("end_datetime", "end", event.end),
    ):
        try:
            expected = datetime.fromisoformat(requested[field])
            if expected.tzinfo is None:
                expected = expected.replace(tzinfo=ZoneInfo(requested["timezone"]))
            if actual.date_time is None:
                problems.append(f"Could not verify {native} time")
            elif expected != actual.date_time:
                problems.append(f"Returned {native} time differs from request")
        except (KeyError, ValueError, TypeError):
            problems.append(f"Could not verify {native} time")
        if actual.timezone != requested.get("timezone"):
            problems.append(f"Returned {native} timezone differs from request")
    if event.summary != requested.get("summary"):
        problems.append("Returned event title differs from request")
    return problems


def verify_events(payload: JsonObject, arguments: JsonObject) -> None:
    request = ExecutionRequest.model_validate(arguments)
    for entry in provider_data(payload).get("results", []):
        if entry.get("tool_slug") != CREATE_EVENT:
            continue
        action = None
        problems = []
        try:
            result = EventResult.model_validate(entry)
            if result.index is None or not 0 <= result.index < len(request.tools):
                problems.append("Could not correlate event result with its request")
            else:
                action = request.tools[result.index]
                if action.tool_slug != CREATE_EVENT:
                    problems.append("Could not correlate event result with its request")
                else:
                    problems.extend(_verify_event(result.response, action.arguments))
        except ValidationError:
            problems.append("Could not verify provider event result")
        entry["stacy_verification"] = {
            "confirmed": not problems,
            "problems": problems,
            "account": action.account if action else None,
            "calendar_id": action.arguments.get("calendar_id") if action else None,
            "instruction": "Do not retry creation. Report any verification uncertainty.",
        }
