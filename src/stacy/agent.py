"""Transport-independent text turns using the same instructions and MCP tools as voice."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from dataclasses import dataclass
from typing import Protocol

from fastmcp import Client, FastMCP

from stacy.agent_instructions import instructions
from stacy.config import Settings
from stacy.memory_context import load_memory_context


@dataclass(frozen=True)
class AgentMessage:
    channel: str
    conversation_id: str
    sender_id: str
    message_id: str
    text: str

    @property
    def thread(self) -> str:
        return json.dumps([self.channel, self.conversation_id, self.sender_id])


class Agent(Protocol):
    async def respond(self, message: AgentMessage) -> str: ...


class TextAgent:
    def __init__(self, settings: Settings, tools: FastMCP, *, model_client=None):
        self.settings, self.tools = settings, tools
        self.model_client = model_client
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        path = settings.data_dir / "conversations.sqlite3"
        self.db = sqlite3.connect(path)
        path.chmod(0o600)
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS turns (thread TEXT, id TEXT, text TEXT, "
            "reply TEXT, status TEXT, PRIMARY KEY(thread, id))"
        )
        self.db.commit()
        self.locks: dict[str, asyncio.Lock] = {}

    async def respond(self, message: AgentMessage) -> str:
        async with self.locks.setdefault(message.thread, asyncio.Lock()):
            existing = self.db.execute(
                "SELECT text, reply, status FROM turns WHERE thread=? AND id=?",
                (message.thread, message.message_id),
            ).fetchone()
            if existing:
                if existing[0] != message.text:
                    raise ValueError("Message ID already used with different text")
                if existing[2] == "complete":
                    return existing[1]
                raise RuntimeError("Earlier attempt may have acted; check state before retrying")
            history = self.db.execute(
                "SELECT text, reply FROM turns WHERE thread=? AND status='complete' "
                "ORDER BY rowid DESC LIMIT 20",
                (message.thread,),
            ).fetchall()
            self.db.execute(
                "INSERT INTO turns VALUES (?, ?, ?, NULL, 'started')",
                (message.thread, message.message_id, message.text),
            )
            self.db.commit()
            try:
                reply = await self._run(message, list(reversed(history)))
            except BaseException:
                self.db.execute(
                    "UPDATE turns SET status='uncertain' WHERE thread=? AND id=?",
                    (message.thread, message.message_id),
                )
                self.db.commit()
                raise
            self.db.execute(
                "UPDATE turns SET reply=?, status='complete' WHERE thread=? AND id=?",
                (reply, message.thread, message.message_id),
            )
            self.db.commit()
            return reply

    async def _run(self, message: AgentMessage, history: list) -> str:
        if self.model_client is None:
            from openai import AsyncOpenAI

            # Disable SDK retries: a lost response could already contain executed work.
            self.model_client = AsyncOpenAI(max_retries=0, timeout=180)
        context = []
        for text, reply in history:
            context.extend(
                [{"role": "user", "content": text}, {"role": "assistant", "content": reply}]
            )
        context.append({"role": "user", "content": message.text})
        async with Client(self.tools) as mcp:
            schemas = await mcp.list_tools()
            notes = await self._memory_context(mcp, {t.name for t in schemas})
            recent_texts = (
                (await mcp.call_tool("recent_texts", {})).data["messages"]
                if any(t.name == "recent_texts" for t in schemas)
                else []
            )
            functions = [
                dict(
                    type="function",
                    name=t.name,
                    description=t.description or "",
                    parameters=t.input_schema,
                    strict=False,
                )
                for t in schemas
            ]
            for _ in range(16):
                response = await self.model_client.responses.create(
                    model=self.settings.text_model,
                    instructions=instructions(
                        self.settings,
                        channel=message.channel,
                        memory_source=f"{message.channel} message {message.message_id}",
                        retrieved_notes=notes,
                        recent_texts=recent_texts,
                    ),
                    input=context,
                    tools=functions,
                    store=False,
                    parallel_tool_calls=False,
                )
                context.extend(item.model_dump(exclude_none=True) for item in response.output)
                calls = [item for item in response.output if item.type == "function_call"]
                if not calls:
                    if not response.output_text.strip():
                        raise RuntimeError("Agent returned no reply")
                    return response.output_text.strip()
                for call in calls:
                    try:
                        arguments = json.loads(call.arguments)
                        result = await mcp.call_tool(call.name, arguments, raise_on_error=False)
                        output = json.dumps(
                            {
                                "is_error": result.is_error,
                                **(
                                    {"structured_content": result.structured_content}
                                    if result.structured_content is not None
                                    else {
                                        "content": [
                                            c.model_dump(mode="json") for c in result.content
                                        ]
                                    }
                                ),
                            }
                        )
                    except Exception:
                        # Preserve uncertainty without exposing credentials in exception strings.
                        output = json.dumps(
                            {
                                "is_error": True,
                                "error": "Tool failed or timed out. Action not confirmed; "
                                "do not retry writes.",
                            }
                        )
                    context.append(
                        dict(type="function_call_output", call_id=call.call_id, output=output)
                    )
        raise RuntimeError("Agent exceeded the tool step limit; check state before retrying")

    async def _memory_context(self, mcp: Client, tool_names: set[str]) -> dict[str, str]:
        """Load the index and a bounded set of linked notes through the authoritative tools.

        A short index alone is insufficient for identity/personalization. Remaining
        notes can still be retrieved by the model using read_memory/search_memory.
        """
        if "read_memory" not in tool_names:
            return {}

        async def read(path: str) -> str:
            result = await mcp.call_tool("read_memory", {"path": path})
            data = result.data
            if not isinstance(data, dict) or not isinstance(data.get("content"), str):
                raise RuntimeError("Memory context could not be loaded")
            return data["content"]

        return await load_memory_context(read)

    async def close(self):
        self.db.close()
        if self.model_client is not None:
            await self.model_client.close()
