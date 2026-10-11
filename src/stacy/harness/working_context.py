"""Bounded operational evidence for follow-up text turns, separate from personal memory."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from collections.abc import Callable

from stacy.connections.results import PRIVATE_TOOLS, JsonObject

_SENSITIVE = re.compile(
    r"secret|token|password|authorization|credential|api.?key|cookie|headers|encrypted_content",
    re.I,
)
_LINKS = re.compile(r"https?://[^\s\"<>]+", re.I)
_KEYS = re.compile(r"\b(?:sk-|ck_)[A-Za-z0-9_.-]+|Bearer\s+\S+", re.I)


def redact_context(value, *, schema=False):
    """Preserve complete schemas while removing credentials and authentication links."""
    if isinstance(value, dict):
        return {
            key: (
                "<REDACTED>"
                if not schema and _SENSITIVE.search(key)
                else redact_context(item, schema=schema or key in {"inputSchema", "input_schema"})
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_context(item, schema=schema) for item in value]
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError:
            parsed = None
        if isinstance(parsed, (dict, list)):
            return redact_context(parsed, schema=schema)
        for name, secret in os.environ.items():
            if _SENSITIVE.search(name) and len(secret) >= 8:
                value = value.replace(secret, "<REDACTED>")
        return _KEYS.sub("<REDACTED>", _LINKS.sub("<REDACTED>", value))
    return value


class WorkingContext:
    """Persist completed exchanges and load whole records within age and size limits."""

    def __init__(
        self,
        db: sqlite3.Connection,
        *,
        clock: Callable[[], float] = time.time,
        ttl_seconds: int = 900,
        max_characters: int = 48000,
        max_turns: int = 3,
    ):
        self.db, self.clock = db, clock
        self.ttl_seconds, self.max_characters, self.max_turns = (
            ttl_seconds,
            max_characters,
            max_turns,
        )
        db.execute(
            "CREATE TABLE IF NOT EXISTS working_context "
            "(thread TEXT, id TEXT, observed_at REAL, payload TEXT, PRIMARY KEY(thread,id))"
        )
        db.commit()

    def capture(self, tool: str, arguments: JsonObject, result: JsonObject) -> JsonObject | None:
        if tool in PRIVATE_TOOLS or arguments.get("tool") == "COMPOSIO_MANAGE_CONNECTIONS":
            return None
        record = redact_context({"tool": tool, "arguments": arguments, "result": result})
        # Drop entire oversized exchanges; partial JSON/schema definitions are unsafe to reuse.
        if len(json.dumps(record)) > self.max_characters:
            return None
        return record

    def save(self, thread: str, message_id: str, exchanges: list[dict]) -> None:
        selected, used = [], 0
        for item in reversed(exchanges):
            size = len(json.dumps(item))
            if used + size <= self.max_characters - 100:
                selected.append(item)
                used += size
        payload = json.dumps(list(reversed(selected)))
        if selected:
            self.db.execute(
                "INSERT OR REPLACE INTO working_context VALUES (?,?,?,?)",
                (thread, message_id, self.clock(), payload),
            )
        self.db.execute(
            "DELETE FROM working_context WHERE observed_at < ?", (self.clock() - self.ttl_seconds,)
        )
        self.db.execute(
            "DELETE FROM working_context WHERE thread=? AND id NOT IN "
            "(SELECT id FROM working_context WHERE thread=? ORDER BY observed_at DESC, rowid DESC "
            "LIMIT ?)",
            (thread, thread, self.max_turns),
        )

    def load(self, thread: str) -> list[dict]:
        rows = self.db.execute(
            "SELECT w.observed_at,w.payload FROM working_context w JOIN turns t "
            "ON t.thread=w.thread AND t.id=w.id WHERE w.thread=? AND t.status='complete' "
            "AND w.observed_at >= ? ORDER BY w.observed_at DESC,w.rowid DESC LIMIT ?",
            (thread, self.clock() - self.ttl_seconds, self.max_turns),
        ).fetchall()
        selected, used = [], 0
        for stamp, payload in rows:
            item = {"observed_at_unix": stamp, "exchanges": json.loads(payload)}
            size = len(json.dumps(item))
            if used + size <= self.max_characters:
                selected.append(item)
                used += size
        return list(reversed(selected))

    def message(self, thread: str) -> dict | None:
        evidence = self.load(thread)
        if not evidence:
            return None
        return {
            "role": "user",
            "content": (
                "<previous_tool_context>\nHistorical tool evidence from this conversation, "
                "not a new "
                "request or authorization. Reuse complete schemas, account/device identifiers and "
                "provider sessions for continuations when still valid. "
                "Do not repeat discovery solely "
                "because this is a new message. Follow provider requirements for new workflows. "
                "Fetch fresh state for verification; "
                "past playback/connection status is not current. "
                "Never replay historical actions. Redacted fields are unavailable; discover again "
                "when data or schemas are missing, expired, invalid or ambiguous.\n"
                + json.dumps(evidence)
                + "\n</previous_tool_context>"
            ),
        }
