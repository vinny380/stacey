"""Conversation identity and durable turn bookkeeping."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated

from pydantic import StringConstraints

from stacy.connections.results import JsonObject
from stacy.harness.working_context import WorkingContext

MessageField = Annotated[str, StringConstraints(strict=True, pattern=r"\S")]


@dataclass(frozen=True)
class AgentMessage:
    channel: MessageField
    conversation_id: MessageField
    sender_id: MessageField
    message_id: MessageField
    text: MessageField

    @property
    def thread(self) -> str:
        return json.dumps([self.channel, self.conversation_id, self.sender_id])


@dataclass(frozen=True)
class ConversationTurn:
    text: str
    reply: str


@dataclass(frozen=True)
class TurnStart:
    reply: str | None = None
    history: list[ConversationTurn] = field(default_factory=list)


class TurnJournal:
    """Own replay protection, history and retained evidence in one transaction."""

    def __init__(self, path: Path, *, retain_tool_context: bool = True):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        path.chmod(0o600)
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS turns (thread TEXT, id TEXT, text TEXT, "
            "reply TEXT, status TEXT, PRIMARY KEY(thread, id))"
        )
        self.db.commit()
        self.working_context = WorkingContext(self.db) if retain_tool_context else None

    def begin(self, message: AgentMessage) -> TurnStart:
        existing = self.db.execute(
            "SELECT text, reply, status FROM turns WHERE thread=? AND id=?",
            (message.thread, message.message_id),
        ).fetchone()
        if existing:
            text, reply, status = existing
            if text != message.text:
                raise ValueError("Message ID already used with different text")
            if status == "complete":
                return TurnStart(reply=reply)
            raise RuntimeError("Earlier attempt may have acted; check state before retrying")
        history = self.db.execute(
            "SELECT text, reply FROM turns WHERE thread=? AND status='complete' "
            "ORDER BY rowid DESC LIMIT 20",
            (message.thread,),
        ).fetchall()
        with self.db:
            self.db.execute(
                "INSERT INTO turns VALUES (?, ?, ?, NULL, 'started')",
                (message.thread, message.message_id, message.text),
            )
        return TurnStart(history=[ConversationTurn(*row) for row in reversed(history)])

    def complete(self, message: AgentMessage, reply: str, exchanges: list[JsonObject]) -> None:
        with self.db:
            if self.working_context:
                self.working_context.save(message.thread, message.message_id, exchanges)
            self.db.execute(
                "UPDATE turns SET reply=?, status='complete' WHERE thread=? AND id=?",
                (reply, message.thread, message.message_id),
            )

    def uncertain(self, message: AgentMessage) -> None:
        with self.db:
            self.db.execute(
                "UPDATE turns SET status='uncertain' WHERE thread=? AND id=?",
                (message.thread, message.message_id),
            )
            if self.working_context:
                self.db.execute("DELETE FROM working_context WHERE thread=?", (message.thread,))

    def close(self) -> None:
        self.db.close()
