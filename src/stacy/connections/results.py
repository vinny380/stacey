"""MCP result conversion shared by conversation and provider adapters."""

from __future__ import annotations

import copy
import json
from collections.abc import Awaitable, Callable
from typing import Any

from fastmcp.client.client import CallToolResult
from fastmcp.tools import ToolResult
from pydantic import TypeAdapter, ValidationError

JsonObject = dict[str, Any]
OBJECT = TypeAdapter(JsonObject)
Invoke = Callable[[str, JsonObject], Awaitable[CallToolResult | ToolResult]]
PRIVATE_TOOLS = {
    "send_text",
    "recent_texts",
    "read_memory",
    "search_memory",
    "update_memory",
    "write_memory",
    "connection_status",
    "connect_mcp",
    "list_connections",
    "verify_connection",
    "reconnect_connection",
    "disconnect_connection",
}


def result_object(result: CallToolResult | ToolResult) -> JsonObject | None:
    if result.structured_content is not None:
        return copy.deepcopy(result.structured_content)
    for block in result.content:
        if block.type != "text":
            continue
        try:
            value, _ = json.JSONDecoder().raw_decode(block.text.lstrip())
            return OBJECT.validate_python(value)
        except (ValueError, ValidationError):
            continue
    return None


def result_payload(result: CallToolResult | ToolResult) -> JsonObject:
    payload: JsonObject = {"is_error": result.is_error}
    if result.structured_content is not None:
        payload["structured_content"] = result.structured_content
    else:
        payload["content"] = [block.model_dump(mode="json") for block in result.content]
    return payload


def provider_data(payload: JsonObject) -> JsonObject:
    return OBJECT.validate_python(payload.get("data", payload))
