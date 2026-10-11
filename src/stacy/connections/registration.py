"""Composio Connect supplies discovery and phone-based account authorization."""

from __future__ import annotations

from pathlib import Path

from stacy.connections.composio import SEARCH_DESCRIPTION, ComposioContext
from stacy.connections.gateway import MCPGateway

DESCRIPTION = (
    "Spotify music/playback; Google Gmail email, Calendar events, Sheets, Drive and Slack messages"
)
CONSUMER_MCP_URL = "https://connect.composio.dev/mcp"


def register_composio(
    gateway: MCPGateway,
    key: str,
    *,
    state_path: Path | None = None,
    timezone: str = "America/Toronto",
) -> ComposioContext:
    if not key.startswith("ck_"):
        raise ValueError(
            "COMPOSIO_API_KEY must be a Composio Connect consumer key (ck_) from "
            "For You > Sessions & API Key. Project API keys need a separate MCP configuration."
        )

    context = ComposioContext(state_path, timezone)
    gateway.add_server(
        "composio",
        {"url": CONSUMER_MCP_URL, "transport": "http", "headers": {"x-consumer-api-key": key}},
        description=DESCRIPTION,
        adapter=context,
        tool_descriptions={"COMPOSIO_SEARCH_TOOLS": SEARCH_DESCRIPTION},
    )
    return context
