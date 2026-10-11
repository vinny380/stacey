"""Transport-independent text turns using the same instructions and MCP tools as voice."""

from __future__ import annotations

import asyncio
import json
import time
from collections import defaultdict
from hashlib import sha256
from typing import TYPE_CHECKING, Any, Protocol

from fastmcp import Client, FastMCP

from stacy.connections.results import OBJECT, PRIVATE_TOOLS, JsonObject, result_payload
from stacy.harness.context import ConversationContext
from stacy.harness.conversations import AgentMessage as AgentMessage
from stacy.harness.conversations import ConversationTurn, TurnJournal
from stacy.harness.instructions import instructions
from stacy.harness.tracing import TextTrace
from stacy.runtime.settings import Settings

if TYPE_CHECKING:
    from openai.types.responses import Response


class Responses(Protocol):
    async def create(self, **kwargs: Any) -> Response: ...


class ModelClient(Protocol):
    responses: Responses

    async def close(self) -> None: ...


class Agent(Protocol):
    async def respond(self, message: AgentMessage) -> str: ...


class TextAgent:
    def __init__(
        self,
        settings: Settings,
        tools: FastMCP,
        *,
        model_client: ModelClient | None = None,
        retain_tool_context: bool = True,
        cache_prompt: bool = False,
        include_tool_index: bool = True,
    ) -> None:
        self.settings, self.tools = settings, tools
        self.model_client = model_client
        self.include_tool_index = include_tool_index
        self.cache_prompt = cache_prompt and settings.text_model.startswith(("gpt-6", "gpt-5.6"))
        self.journal = TurnJournal(
            settings.data_dir / "conversations.sqlite3", retain_tool_context=retain_tool_context
        )
        self.locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def respond(self, message: AgentMessage) -> str:
        trace = TextTrace(self.settings.data_dir)
        trace.record(
            "turn_started",
            channel=message.channel,
            model=self.settings.text_model,
            message_ref=sha256(message.message_id.encode()).hexdigest()[:16],
            input_characters=len(message.text),
            trace_version=2,
            harness_variant="retained-tool-context" if self.journal.working_context else "baseline",
            prompt_cache=self.cache_prompt,
        )
        with trace.activate(), trace.measure("turn"):
            return await self._respond(message, trace)

    async def _respond(self, message: AgentMessage, trace: TextTrace) -> str:
        queued = time.perf_counter()
        async with self.locks[message.thread]:
            trace.record("queue_wait", seconds=time.perf_counter() - queued)
            turn = self.journal.begin(message)
            if turn.reply is not None:
                trace.record("cached_reply")
                return turn.reply
            exchanges: list[JsonObject] = []
            try:
                reply = await self._run(message, turn.history, trace, exchanges)
                self.journal.complete(message, reply, exchanges)
            except BaseException:
                self.journal.uncertain(message)
                raise
            return reply

    async def _run(
        self,
        message: AgentMessage,
        history: list[ConversationTurn],
        trace: TextTrace,
        exchanges: list[JsonObject],
    ) -> str:
        if self.model_client is None:
            from openai import AsyncOpenAI

            # Disable SDK retries: a lost response could already contain executed work.
            self.model_client = AsyncOpenAI(max_retries=0, timeout=180)
        context = []
        for turn in history:
            context.extend(
                [
                    {"role": "user", "content": turn.text},
                    {"role": "assistant", "content": turn.reply},
                ]
            )
        working = (
            self.journal.working_context.message(message.thread)
            if self.journal.working_context
            else None
        )
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
                with trace.measure("memory_context_load"):
                    loaded = await ConversationContext.load(
                        mcp, schemas, include_tool_index=self.include_tool_index
                    )
            prompt = instructions(
                self.settings,
                channel=message.channel,
                memory_source=f"{message.channel} message {message.message_id}",
                context=loaded,
                cacheable=self.cache_prompt,
            )
            if self.cache_prompt:
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
            trace.record(
                "context_summary",
                history_turns=len(history),
                memory_notes=len(loaded.notes),
                memory_characters=sum(len(v) for v in loaded.notes.values()),
                recent_text_count=len(loaded.recent_texts),
                tool_count=len(schemas),
                tool_index_characters=len(json.dumps(loaded.tool_index))
                if loaded.tool_index
                else 0,
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
            for step in range(16):
                with trace.measure("model_request", step=step, model=self.settings.text_model):
                    response = await self.model_client.responses.create(
                        model=self.settings.text_model,
                        instructions=None if self.cache_prompt else prompt,
                        input=context,
                        tools=functions,
                        store=False,
                        parallel_tool_calls=False,
                    )
                trace.record(
                    "model_response",
                    step=step,
                    response_id=response.id,
                    output_types=[item.type for item in response.output],
                    function_calls=[
                        {"name": item.name, "call_id": item.call_id}
                        for item in response.output
                        if item.type == "function_call"
                    ],
                    reply_characters=len(response.output_text),
                )
                if response.usage is not None:
                    usage = response.usage
                    trace.record(
                        "model_usage",
                        step=step,
                        input_tokens=usage.input_tokens,
                        output_tokens=usage.output_tokens,
                        total_tokens=usage.total_tokens,
                        reasoning_tokens=usage.output_tokens_details.reasoning_tokens,
                        cached_tokens=usage.input_tokens_details.cached_tokens,
                    )
                context.extend(item.model_dump(exclude_none=True) for item in response.output)
                calls = [item for item in response.output if item.type == "function_call"]
                if not calls:
                    if not response.output_text.strip():
                        raise RuntimeError("Agent returned no reply")
                    return response.output_text.strip()
                for call in calls:
                    arguments: JsonObject = {}
                    private = call.name in PRIVATE_TOOLS
                    try:
                        arguments = OBJECT.validate_json(call.arguments)
                        trace.record(
                            "tool_started",
                            step=step,
                            tool=call.name,
                            call_id=call.call_id,
                            arguments={"payload": "<private context omitted>"}
                            if private
                            else arguments,
                        )
                        with trace.measure(
                            "tool_request", step=step, tool=call.name, call_id=call.call_id
                        ):
                            result = await mcp.call_tool(call.name, arguments, raise_on_error=False)

                        payload_result = result_payload(result)
                        trace.record(
                            "tool_result",
                            step=step,
                            tool=call.name,
                            call_id=call.call_id,
                            is_error=bool(result.is_error),
                            result={"payload": "<private context omitted>"}
                            if private
                            else payload_result,
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
                        payload_result = {
                            "is_error": True,
                            "error": "Tool failed or timed out. Action not confirmed; "
                            "do not retry writes.",
                        }
                    if self.journal.working_context:
                        retained = self.journal.working_context.capture(
                            call.name, arguments, payload_result
                        )
                        if retained is not None:
                            exchanges.append(retained)
                    context.append(
                        dict(
                            type="function_call_output",
                            call_id=call.call_id,
                            output=json.dumps(payload_result),
                        )
                    )
        raise RuntimeError("Agent exceeded the tool step limit; check state before retrying")

    async def close(self) -> None:
        self.journal.close()
        if self.model_client is not None:
            await self.model_client.close()
