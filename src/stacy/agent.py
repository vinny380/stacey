"""Transport-independent text turns using the same instructions and MCP tools as voice."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from dataclasses import dataclass
from hashlib import sha256
from typing import Protocol

from fastmcp import Client, FastMCP

from stacy.agent_instructions import instructions
from stacy.config import Settings
from stacy.memory_context import load_memory_context
from stacy.tracing import TextTrace
from stacy.working_context import WorkingContext


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
    def __init__(
        self,
        settings: Settings,
        tools: FastMCP,
        *,
        model_client=None,
        retain_tool_context: bool = True,
        cache_prompt: bool = False,
        include_tool_index: bool = True,
    ):
        self.settings, self.tools = settings, tools
        self.model_client = model_client
        self.include_tool_index = include_tool_index
        self.cache_prompt = cache_prompt and settings.text_model.startswith(("gpt-6", "gpt-5.6"))
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        path = settings.data_dir / "conversations.sqlite3"
        self.db = sqlite3.connect(path)
        path.chmod(0o600)
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS turns (thread TEXT, id TEXT, text TEXT, "
            "reply TEXT, status TEXT, PRIMARY KEY(thread, id))"
        )
        self.db.commit()
        self.working_context = WorkingContext(self.db) if retain_tool_context else None
        self.locks: dict[str, asyncio.Lock] = {}

    async def respond(self, message: AgentMessage) -> str:
        trace = TextTrace(self.settings.data_dir)
        trace.record(
            "turn_started",
            channel=message.channel,
            model=self.settings.text_model,
            message_ref=sha256(message.message_id.encode()).hexdigest()[:16],
            input_characters=len(message.text),
            trace_version=2,
            harness_variant="retained-tool-context" if self.working_context else "baseline",
            prompt_cache=self.cache_prompt,
        )
        with trace.activate(), trace.measure("turn"):
            return await self._respond(message, trace)

    async def _respond(self, message: AgentMessage, trace: TextTrace) -> str:
        queued = time.perf_counter()
        async with self.locks.setdefault(message.thread, asyncio.Lock()):
            trace.record("queue_wait", seconds=time.perf_counter() - queued)
            existing = self.db.execute(
                "SELECT text, reply, status FROM turns WHERE thread=? AND id=?",
                (message.thread, message.message_id),
            ).fetchone()
            if existing:
                if existing[0] != message.text:
                    raise ValueError("Message ID already used with different text")
                if existing[2] == "complete":
                    trace.record("cached_reply")
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
            exchanges = []
            try:
                reply = await self._run(message, list(reversed(history)), trace, exchanges)
            except BaseException:
                self.db.execute(
                    "UPDATE turns SET status='uncertain' WHERE thread=? AND id=?",
                    (message.thread, message.message_id),
                )
                if self.working_context:
                    self.db.execute("DELETE FROM working_context WHERE thread=?", (message.thread,))
                self.db.commit()
                raise
            if self.working_context:
                self.working_context.save(message.thread, message.message_id, exchanges)
            self.db.execute(
                "UPDATE turns SET reply=?, status='complete' WHERE thread=? AND id=?",
                (reply, message.thread, message.message_id),
            )
            self.db.commit()
            return reply

    async def _run(
        self,
        message: AgentMessage,
        history: list,
        trace: TextTrace | None = None,
        exchanges: list | None = None,
    ) -> str:
        trace = trace or TextTrace(self.settings.data_dir)
        if self.model_client is None:
            from openai import AsyncOpenAI

            # Disable SDK retries: a lost response could already contain executed work.
            self.model_client = AsyncOpenAI(max_retries=0, timeout=180)
        context = []
        for text, reply in history:
            context.extend(
                [{"role": "user", "content": text}, {"role": "assistant", "content": reply}]
            )
        working = self.working_context.message(message.thread) if self.working_context else None
        if working:
            context.append(working)
        trace.record(
            "working_context",
            retained=bool(working),
            characters=len(working["content"]) if working else 0,
        )
        context.append({"role": "user", "content": message.text})
        async with Client(self.tools) as mcp:
            with trace.measure("context_load"):
                with trace.measure("tool_schema_load"):
                    schemas = await mcp.list_tools()
                from stacy.tool_index import load_tool_index

                with trace.measure("tool_index_load"):
                    tool_index = (
                        await load_tool_index(mcp, schemas) if self.include_tool_index else None
                    )
                with trace.measure("memory_context_load"):
                    notes = await self._memory_context(mcp, {t.name for t in schemas})
                recent_texts = (
                    (await mcp.call_tool("recent_texts", {})).data["messages"]
                    if any(t.name == "recent_texts" for t in schemas)
                    else []
                )
            trace.record(
                "context_summary",
                history_turns=len(history),
                memory_notes=len(notes),
                memory_characters=sum(len(v) for v in notes.values()),
                recent_text_count=len(recent_texts),
                tool_count=len(schemas),
                tool_index_characters=len(json.dumps(tool_index)) if tool_index else 0,
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
            prompt_arguments = dict(
                channel=message.channel,
                memory_source=f"{message.channel} message {message.message_id}",
                retrieved_notes=notes,
                recent_texts=recent_texts,
                tool_index=tool_index,
            )
            if self.cache_prompt:
                prompt = instructions(self.settings, **prompt_arguments, cacheable=True)
                prefix, marker, dynamic = prompt.partition("\n<current_turn_context>\n")
                if not marker:
                    raise RuntimeError("Missing prompt cache boundary")
                context[:0] = [
                    {
                        "role": "developer",
                        "content": [
                            {
                                "type": "input_text",
                                "text": prefix,
                                "prompt_cache_breakpoint": {"mode": "explicit"},
                            }
                        ],
                    },
                    {"role": "developer", "content": marker + dynamic},
                ]
            for step in range(16):
                with trace.measure("model_request", step=step, model=self.settings.text_model):
                    response = await self.model_client.responses.create(
                        model=self.settings.text_model,
                        instructions=None
                        if self.cache_prompt
                        else instructions(
                            self.settings,
                            **prompt_arguments,
                        ),
                        input=context,
                        tools=functions,
                        store=False,
                        parallel_tool_calls=False,
                    )
                trace.record(
                    "model_response",
                    step=step,
                    response_id=getattr(response, "id", None),
                    request_id=getattr(response, "_request_id", None),
                    output_types=[item.type for item in response.output],
                    function_calls=[
                        {"name": item.name, "call_id": item.call_id}
                        for item in response.output
                        if item.type == "function_call"
                    ],
                    reply_characters=len(response.output_text),
                )
                usage = getattr(response, "usage", None)
                if usage is not None:
                    fields = {}
                    for key in ("input_tokens", "output_tokens", "total_tokens"):
                        value = getattr(usage, key, None)
                        if isinstance(value, int):
                            fields[key] = value
                    for name, key in (
                        ("output_tokens_details", "reasoning_tokens"),
                        ("input_tokens_details", "cached_tokens"),
                    ):
                        value = getattr(getattr(usage, name, None), key, None)
                        if isinstance(value, int):
                            fields[key] = value
                    trace.record("model_usage", step=step, **fields)
                context.extend(item.model_dump(exclude_none=True) for item in response.output)
                calls = [item for item in response.output if item.type == "function_call"]
                if not calls:
                    if not response.output_text.strip():
                        raise RuntimeError("Agent returned no reply")
                    return response.output_text.strip()
                for call in calls:
                    arguments = {}
                    try:
                        parsed_arguments = json.loads(call.arguments)
                        if not isinstance(parsed_arguments, dict):
                            raise ValueError("Tool arguments must be an object")
                        arguments = parsed_arguments
                        payload = (
                            arguments
                            if call.name
                            not in {
                                "send_text",
                                "recent_texts",
                                "read_memory",
                                "write_memory",
                                "update_memory",
                                "search_memory",
                                "connection_status",
                            }
                            else {"payload": "<private context omitted>"}
                        )
                        trace.record(
                            "tool_started",
                            step=step,
                            tool=call.name,
                            call_id=call.call_id,
                            arguments=payload,
                        )
                        with trace.measure(
                            "tool_request", step=step, tool=call.name, call_id=call.call_id
                        ):
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
                        trace.record(
                            "tool_result",
                            step=step,
                            tool=call.name,
                            call_id=call.call_id,
                            is_error=bool(result.is_error),
                            result=json.loads(output)
                            if payload is arguments
                            else {"payload": "<private context omitted>"},
                        )
                    except Exception as error:
                        trace.record(
                            "tool_exception",
                            step=step,
                            tool=call.name,
                            call_id=call.call_id,
                            error_type=type(error).__name__,
                        )
                        # Preserve uncertainty without exposing credentials in exception strings.
                        output = json.dumps(
                            {
                                "is_error": True,
                                "error": "Tool failed or timed out. Action not confirmed; "
                                "do not retry writes.",
                            }
                        )
                    if self.working_context and exchanges is not None:
                        retained = self.working_context.capture(
                            call.name, arguments, json.loads(output)
                        )
                        if retained is not None:
                            exchanges.append(retained)
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
