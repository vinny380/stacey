import asyncio

import httpx
import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError

from stacy.integrations.messaging.notifications import Notifications


def test_owner_text_ack_and_handoff_survive_restart(tmp_path):
    async def scenario():
        sent = []

        async def adapter(request):
            assert request.headers["authorization"] == "Bearer " + "x" * 32
            assert request.url.path == "/send-text"
            sent.append(request.read())
            return httpx.Response(200, json={"status": "sent", "message_id": "provider-message"})

        service = Notifications(tmp_path, "x" * 32, transport=httpx.MockTransport(adapter))
        server = FastMCP("host")
        service.register(server)
        text = "Connect your calendar: https://auth.example/sign-in. Reply done."
        async with Client(server) as client:
            result = await client.call_tool("send_text", {"text": text})
            assert result.data == {
                "status": "sent",
                "message_id": "provider-message",
                "channel": "imessage",
            }
            assert "Bearer" not in str(result)
            handoff = await client.call_tool("recent_texts", {})
            assert handoff.data == {"messages": [{"text": text, "status": "sent"}]}
        service.close()
        restored = Notifications(tmp_path, "x" * 32)
        assert restored.recent() == [{"text": text, "status": "sent"}]
        assert len(sent) == 1
        assert (tmp_path / "notifications.sqlite3").stat().st_mode & 0o777 == 0o600
        restored.close()

    asyncio.run(scenario())


def test_unknown_send_keeps_uncertainty_and_never_retries(tmp_path):
    async def scenario():
        attempts = []

        async def adapter(request):
            attempts.append(request)
            raise httpx.ReadTimeout("Lost acknowledgement")

        service = Notifications(tmp_path, "x" * 32, transport=httpx.MockTransport(adapter))
        with pytest.raises(ToolError, match="not confirmed"):
            await service.send("Here is your link")
        assert len(attempts) == 1
        assert service.recent() == [{"text": "Here is your link", "status": "uncertain"}]
        service.close()

    asyncio.run(scenario())


def test_outbound_bridge_cannot_forward_token_to_remote_host(tmp_path, monkeypatch):
    monkeypatch.setenv("STACY_OUTBOUND_URL", "https://external.example/send")
    with pytest.raises(ValueError, match="local HTTP"):
        Notifications(tmp_path, "x" * 32)
