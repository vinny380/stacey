"""Send owner texts through the existing local iMessage adapter; retain handoff context."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

import httpx
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError


class Notifications:
    def __init__(self, data_dir: Path, token: str, *, transport=None):
        self.token, self.transport = token, transport
        self.url = os.environ.get("STACY_OUTBOUND_URL", "http://127.0.0.1:8766")
        parsed = urlparse(self.url)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("STACY_OUTBOUND_URL must be a local HTTP adapter URL")
        if len(token) < 32:
            raise ValueError("Outbound bridge token must be at least 32 characters")
        data_dir.mkdir(parents=True, exist_ok=True)
        path = data_dir / "notifications.sqlite3"
        self.db = sqlite3.connect(path)
        path.chmod(0o600)
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS texts (id TEXT PRIMARY KEY, text TEXT, "
            "status TEXT, message_id TEXT)"
        )
        self.db.execute("UPDATE texts SET status='uncertain' WHERE status='started'")
        self.db.commit()

    async def send(self, text: str) -> dict:
        if not text.strip() or len(text) > 12000:
            raise ToolError("Text must contain between 1 and 12000 characters")
        request_id = uuid4().hex
        self.db.execute("INSERT INTO texts VALUES (?, ?, 'started', NULL)", (request_id, text))
        self.db.commit()
        try:
            async with httpx.AsyncClient(
                timeout=45, follow_redirects=False, trust_env=False, transport=self.transport
            ) as client:
                response = await client.post(
                    self.url.rstrip("/") + "/send-text",
                    headers={"Authorization": "Bearer " + self.token},
                    json={"request_id": request_id, "text": text},
                )
                response.raise_for_status()
                result = response.json()
                if (
                    result.get("status") != "sent"
                    or not isinstance(result.get("message_id"), str)
                    or not result["message_id"]
                ):
                    raise ValueError("Adapter did not confirm a send")
        except BaseException as error:
            self.db.execute("UPDATE texts SET status='uncertain' WHERE id=?", (request_id,))
            self.db.commit()
            if not isinstance(error, Exception):
                raise
            raise ToolError(
                "Text send was not confirmed. Check your phone; do not retry automatically."
            ) from None
        self.db.execute(
            "UPDATE texts SET status='sent', message_id=? WHERE id=?",
            (result["message_id"], request_id),
        )
        self.db.commit()
        # Provider acceptance confirms sending, not delivery or that the user opened the link.
        return {"status": "sent", "message_id": result["message_id"], "channel": "imessage"}

    def recent(self) -> list[dict]:
        return [
            {"text": row[0], "status": row[1]}
            for row in self.db.execute(
                "SELECT text, status FROM texts WHERE status IN ('sent', 'uncertain') "
                "ORDER BY rowid DESC LIMIT 5"
            )
        ]

    def register(self, server: FastMCP) -> None:
        @server.tool
        async def send_text(text: str) -> dict:
            """Send a text or authorization link to the owner's phone via iMessage.

            Use for voice-to-phone handoff. Include the exact link. Wait for status='sent'
            before saying it was sent. Never retry an uncertain send automatically.
            """
            return await self.send(text)

        @server.tool
        async def recent_texts() -> dict:
            """Read the latest owner text handoffs to understand 'done' across voice and iMessage.

            These are operational messages, not personal memory. Never save auth links in memory.
            """
            return {"messages": self.recent()}

    def close(self):
        self.db.close()
