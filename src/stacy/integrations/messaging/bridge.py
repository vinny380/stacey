"""Authenticated local bridge for trusted channel adapters."""

from __future__ import annotations

import hmac
import os
import secrets
from pathlib import Path

from fastmcp import FastMCP
from pydantic import TypeAdapter, ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse

from stacy.harness.text import Agent, AgentMessage


def bridge_token() -> str:
    explicit = os.environ.get("STACY_AGENT_TOKEN")
    if explicit:
        return explicit
    path = Path(os.environ.get("STACY_AGENT_TOKEN_FILE", ".cache/agent-token"))
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return path.read_text().strip()
    with os.fdopen(fd, "w") as file:
        token = secrets.token_urlsafe(32)
        file.write(token)
    return token


def register_messaging(server: FastMCP, agent: Agent, token: str) -> None:
    if len(token) < 32:
        raise ValueError("STACY_AGENT_TOKEN must be at least 32 characters")

    @server.custom_route("/agent/message", methods=["POST"])
    async def message(request: Request):
        if not hmac.compare_digest(request.headers.get("authorization", ""), f"Bearer {token}"):
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
        raw = await request.body()
        if len(raw) > 64000:
            return JSONResponse({"error": "Message too large"}, status_code=413)
        try:
            incoming = TypeAdapter(AgentMessage).validate_json(raw)
        except ValidationError:
            return JSONResponse({"error": "Expected nonempty message fields"}, status_code=400)
        try:
            reply = await agent.respond(incoming)
            return JSONResponse({"text": reply})
        except Exception:
            return JSONResponse(
                {
                    "error": "Turn failed; actions may have completed. "
                    "Check their state before retrying."
                },
                status_code=500,
            )
