"""Composio Connect supplies discovery and phone-based account authorization."""

from __future__ import annotations

from pathlib import Path

from stacy.mcp_gateway import MCPGateway

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
) -> None:
    if not key.startswith("ck_"):
        raise ValueError(
            "COMPOSIO_API_KEY must be a Composio Connect consumer key (ck_) from "
            "For You > Sessions & API Key. Project API keys need a separate MCP configuration."
        )

    async def config() -> dict:
        return {
            "url": CONSUMER_MCP_URL,
            "transport": "http",
            "headers": {"x-consumer-api-key": key},
            "description": DESCRIPTION,
        }

    # Catalog stays local until requested. Consumer identity, connection aliases,
    # authorization links and account status are managed by the hosted MCP tools.
    gateway.add_lazy_server("composio", DESCRIPTION, config)
    from stacy.composio_context import ComposioContext

    gateway.adapters["composio"] = ComposioContext(state_path, timezone)
