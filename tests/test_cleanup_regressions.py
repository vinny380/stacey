import asyncio
import json

import pytest
from fastmcp.exceptions import ToolError
from fastmcp.tools import ToolResult
from opentelemetry.sdk.trace import ReadableSpan

from stacy.connections.composio import ComposioContext, compact_search
from stacy.connections.gateway import MCPGateway
from stacy.harness.context import ConversationContext
from stacy.harness.instructions import instructions
from stacy.harness.tracing import provider_failed, telemetry_json
from stacy.runtime.settings import Settings


def test_prompt_uses_bounded_retrieved_memory_once(tmp_path):
    (tmp_path / "MEMORY.md").write_text("unbounded disk copy" * 10000)
    prompt = instructions(
        Settings(tmp_path, tmp_path), context=ConversationContext(notes={"MEMORY.md": "index"})
    )
    assert "unbounded disk copy" not in prompt
    assert prompt.count('"MEMORY.md": "index"') == 1


def test_search_keeps_provider_ranking_and_user_info_schema():
    schema = {
        "toolkit": "notion",
        "input_schema": {"properties": {"user_info": {"type": "string"}}},
    }
    payload = {"tool_schemas": {"NOTION_CALENDAR": schema}, "results": []}
    result = compact_search(payload)
    assert result["tool_schemas"] == {"NOTION_CALENDAR": schema}


def test_pending_authorizations_for_two_aliases_are_reused_independently(tmp_path):
    async def scenario():
        adapter = ComposioContext(tmp_path / "connections.json", "America/Toronto")
        calls = []

        async def invoke(tool, args):
            alias = args["toolkits"][0]["alias"]
            calls.append(alias)
            return ToolResult(structured_content={"redirect_url": f"https://auth.test/{alias}"})

        for alias in ("work", "personal", "work", "personal"):
            await adapter.call(
                "COMPOSIO_MANAGE_CONNECTIONS",
                {"toolkits": [{"name": "googlecalendar", "action": "add", "alias": alias}]},
                invoke,
            )
        assert calls == ["work", "personal"]

    asyncio.run(scenario())


def test_calendar_refresh_is_specific_to_toolkit_and_accepts_flat_accounts():
    async def scenario():
        adapter = ComposioContext(None, "America/Toronto")
        adapter.connections(
            {"results": {"slack": {"accounts": [{"id": "slack", "status": "active"}]}}}
        )
        calls = []

        async def invoke(tool, args):
            calls.append(tool)
            if tool == "COMPOSIO_MANAGE_CONNECTIONS":
                return ToolResult(
                    structured_content={"accounts": [{"id": "calendar", "status": "active"}]}
                )
            assert args["tools"][0]["account"] == "calendar"
            return ToolResult(structured_content={"results": []})

        await adapter.call(
            "COMPOSIO_MULTI_EXECUTE_TOOL",
            {"tools": [{"tool_slug": "GOOGLECALENDAR_EVENTS_LIST", "arguments": {}}]},
            invoke,
        )
        assert calls == ["COMPOSIO_MANAGE_CONNECTIONS", "COMPOSIO_MULTI_EXECUTE_TOOL"]

    asyncio.run(scenario())


@pytest.mark.parametrize("index", [None, -1, 3])
def test_calendar_verification_needs_valid_result_correlation(index):
    entry = {"tool_slug": "GOOGLECALENDAR_CREATE_EVENT", "response": {"successful": True}}
    if index is not None:
        entry["index"] = index
    payload = {"results": [entry]}
    request = {"tools": [{"tool_slug": "GOOGLECALENDAR_CREATE_EVENT", "arguments": {}}]}
    ComposioContext.verify_events(payload, request)
    assert entry["stacy_verification"]["confirmed"] is False
    assert "correlate" in json.dumps(entry["stacy_verification"]).lower()


def test_business_data_is_not_a_provider_error():
    assert not provider_failed({"successful": True, "data": {"confirmed": False, "error_count": 3}})
    assert provider_failed(
        {"successful": True, "data": {"results": [{"response": {"successful": False}}]}}
    )


def test_connection_result_mapping_is_not_a_batch_result():
    assert not provider_failed(
        {"successful": True, "data": {"results": {"gmail": {"accounts": []}}}}
    )


def test_connection_status_hides_auth_links_and_migrates_existing_state(tmp_path):
    path = tmp_path / "connections.json"
    path.write_text(
        json.dumps(
            {
                "accounts": {},
                "pending": {
                    "googlecalendar": {
                        "alias": "work",
                        "expires_at": 9999999999,
                        "result": {"redirect_url": "https://auth.test/private"},
                    }
                },
            }
        )
    )
    adapter = ComposioContext(path, "America/Toronto")
    status = adapter.status()
    assert "https://auth" not in json.dumps(status)
    assert next(iter(status["pending"].values()))["toolkit"] == "googlecalendar"


def test_invalid_soloist_state_cannot_claim_available_playback(monkeypatch):
    from stacy.integrations.spotify.soloist import AuthState, Soloist, SoloistReply

    async def incomplete(*args):
        return SoloistReply(auth=AuthState(logged_in=True), result={"type": "playback_state"})

    player = Soloist()
    monkeypatch.setattr(player, "exchange", incomplete)
    with pytest.raises(ValueError, match="invalid playback state"):
        asyncio.run(player.status())


@pytest.mark.parametrize(
    "response", [{}, {"results": {"googlecalendar": {"successful": False, "accounts": []}}}]
)
def test_failed_refresh_cannot_reuse_stale_calendar_accounts(response):
    async def scenario():
        adapter = ComposioContext(None, "America/Toronto")
        adapter.connections({"accounts": [{"id": "old", "status": "active"}]}, ["googlecalendar"])
        adapter._checked.clear()

        async def invoke(tool, args):
            assert tool == "COMPOSIO_MANAGE_CONNECTIONS"
            return ToolResult(structured_content={"successful": True, "data": response})

        with pytest.raises(ToolError, match="unverified"):
            await adapter.call(
                "COMPOSIO_MULTI_EXECUTE_TOOL",
                {"tools": [{"tool_slug": "GOOGLECALENDAR_EVENTS_LIST"}]},
                invoke,
            )

    asyncio.run(scenario())


def test_discovery_preserves_schemas_and_plans_while_removing_only_account_previews(tmp_path):
    async def scenario():
        schema = {"input_schema": {"properties": {"user_info": {"type": "string"}}}}
        payload = {
            "data": {
                "tool_schemas": {f"TOOL_{i}": schema for i in range(7)},
                "results": [
                    {
                        "primary_tool_slugs": ["TOOL_6"],
                        "recommended_plan_steps": ["Fetch prerequisites"],
                    }
                ],
                "toolkit_connection_statuses": [
                    {
                        "toolkit": "notion",
                        "accounts": [
                            {
                                "id": "notion-account",
                                "status": "active",
                                "user_info": {"email": "owner@example.test", "items": ["preview"]},
                            }
                        ],
                    }
                ],
            }
        }

        async def invoke(tool, args):
            return ToolResult(structured_content=payload)

        adapter = ComposioContext(tmp_path / "connections.json", "America/Toronto")
        result = await adapter.call(
            "COMPOSIO_SEARCH_TOOLS", {"queries": [{"use_case": "Notion calendar"}]}, invoke
        )
        data = result.structured_content["data"]
        assert len(data["tool_schemas"]) == 5
        assert list(data["tool_schemas"])[0] == "TOOL_6"
        assert data["tool_schemas"]["TOOL_6"] == schema
        assert data["results"][0]["recommended_plan_steps"] == ["Fetch prerequisites"]
        assert len(data["additional_tool_slugs"]) == 2
        identity = data["toolkit_connection_statuses"][0]["accounts"][0]["user_info"]
        assert identity == {"email": "owner@example.test"}
        assert "preview" not in (tmp_path / "connections.json").read_text()

    asyncio.run(scenario())


def test_empty_native_span_can_be_exported():
    exported = json.loads(telemetry_json(ReadableSpan("empty")))
    assert exported["attributes"] == {}


def test_directory_description_is_not_transport_configuration(monkeypatch):
    monkeypatch.delenv("MISSING_DESCRIPTION_VALUE", raising=False)
    gateway = MCPGateway(
        {
            "example": {
                "url": "http://example.test/mcp",
                "description": "Literal ${MISSING_DESCRIPTION_VALUE}",
            }
        }
    )
    gateway._proxy("example")
