"""Acquire the same bounded context for every conversation channel."""

from dataclasses import dataclass, field

from fastmcp import Client
from mcp.types import Tool
from pydantic import BaseModel

from stacy.connections.results import JsonObject
from stacy.harness.tool_index import load_tool_index
from stacy.memory.context import load_memory_context


class MemoryNote(BaseModel):
    content: str


class RecentTexts(BaseModel):
    messages: list[JsonObject]


@dataclass(frozen=True)
class ConversationContext:
    notes: dict[str, str] = field(default_factory=dict)
    recent_texts: list[JsonObject] = field(default_factory=list)
    tool_index: JsonObject | None = None

    @classmethod
    async def load(
        cls, client: Client, schemas: list[Tool], *, include_tool_index: bool = True
    ) -> "ConversationContext":
        tool_names = {tool.name for tool in schemas}

        async def read(path: str) -> str:
            result = await client.call_tool("read_memory", {"path": path})
            return MemoryNote.model_validate(result.data).content

        notes = await load_memory_context(read) if "read_memory" in tool_names else {}
        recent = []
        if "recent_texts" in tool_names:
            result = await client.call_tool("recent_texts", {})
            recent = RecentTexts.model_validate(result.data).messages
        tool_index = await load_tool_index(client, schemas) if include_tool_index else None
        return cls(notes, recent, tool_index)
