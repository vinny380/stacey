import asyncio
import copy

import pytest
from fastmcp.exceptions import ToolError
from fastmcp.tools import ToolResult
from mcp.types import TextContent

from stacy.composio_context import ComposioContext, compact_search, json_result


def accounts(*ids):
    return {
        "successful": True,
        "data": {
            "results": {
                "googlecalendar": {
                    "accounts": [
                        {
                            "id": i,
                            "status": "active",
                            "user_info": {
                                "timeZone": "America/Toronto",
                                "items": [{"summary": "2020 sample event"}],
                            },
                        }
                        for i in ids
                    ]
                }
            }
        },
    }


def test_search_filters_unrequested_apps_but_preserves_full_schemas_and_errors():
    schema = {
        "type": "object",
        "required": ["calendar_id"],
        "properties": {"calendar_id": {"type": "string", "enum": ["primary"]}},
    }
    payload = {
        "successful": True,
        "data": {
            "session": {"id": "session"},
            "error": None,
            "results": [
                {
                    "primary_tool_slugs": ["GMAIL_FETCH", "GOOGLECALENDAR_EVENTS_LIST"],
                    "recommended_plan_steps": ["Cached unrelated email plan"],
                    "known_pitfalls": ["Follow pagination"],
                }
            ],
            "tool_schemas": {
                "GMAIL_FETCH": {"toolkit": "gmail", "input_schema": schema},
                "GOOGLECALENDAR_EVENTS_LIST": {"toolkit": "googlecalendar", "input_schema": schema},
            },
            "toolkit_connection_statuses": [{"toolkit": "gmail"}, {"toolkit": "googlecalendar"}],
        },
    }
    result = compact_search(payload, {"queries": [{"use_case": "Show Google Calendar tomorrow"}]})
    assert list(result["data"]["tool_schemas"]) == ["GOOGLECALENDAR_EVENTS_LIST"]
    assert result["data"]["tool_schemas"]["GOOGLECALENDAR_EVENTS_LIST"]["input_schema"] == schema
    assert result["data"]["results"][0]["known_pitfalls"] == ["Follow pagination"]
    assert result["data"]["session"]["id"] == "session"
    both = compact_search(payload, {"queries": [{"use_case": "Calendar and Gmail"}]})
    assert len(both["data"]["tool_schemas"]) == 2


def test_mixed_json_and_prose_response_is_parsed():
    result = ToolResult(content=[TextContent(type="text", text='{"successful": false}\nAdvice')])
    assert json_result(result) == {"successful": False}


def test_pending_auth_survives_restart_and_done_requires_intended_account(tmp_path):
    async def scenario():
        path = tmp_path / "connections.json"
        adapter = ComposioContext(path, "America/Toronto")
        adapter.connections(accounts("old"))
        calls = []

        async def invoke(tool, args):
            calls.append((tool, args))
            return ToolResult(
                structured_content={
                    "successful": True,
                    "data": {
                        "results": {
                            "googlecalendar": {
                                "account_id": "new",
                                "redirect_url": "https://auth.example/new",
                                "status": "initiated",
                            }
                        }
                    },
                }
            )

        request = {"toolkits": [{"name": "googlecalendar", "action": "add", "alias": "work"}]}
        first = await adapter.call("COMPOSIO_MANAGE_CONNECTIONS", request, invoke)
        assert "2020 sample" not in path.read_text()
        restarted = ComposioContext(path, "America/Toronto")
        assert (
            await restarted.call("COMPOSIO_MANAGE_CONNECTIONS", request, invoke)
        ).structured_content == first.structured_content
        assert len(calls) == 1
        restarted.connections(accounts("old"))
        assert "googlecalendar" in restarted.state["pending"]
        restarted.connections(accounts("old", "new"))
        assert restarted.state["pending"] == {}
        assert path.stat().st_mode & 0o777 == 0o600

    asyncio.run(scenario())


@pytest.mark.parametrize("ids", [(), ("personal", "work")])
def test_calendar_ambiguity_cannot_execute_batch(ids):
    async def scenario():
        adapter = ComposioContext(None, "America/Toronto")
        adapter.connections(accounts(*ids))

        async def invoke(*args):
            pytest.fail("An ambiguous account must not execute")

        with pytest.raises(ToolError, match="Select the intended"):
            await adapter.call(
                "COMPOSIO_MULTI_EXECUTE_TOOL",
                {
                    "tools": [
                        {
                            "tool_slug": "GOOGLECALENDAR_CREATE_EVENT",
                            "arguments": {"calendar_id": "primary"},
                        }
                    ]
                },
                invoke,
            )

    asyncio.run(scenario())


@pytest.mark.parametrize("wrong_time", [False, True])
def test_calendar_timezone_account_and_result_verification_without_retry(wrong_time):
    async def scenario():
        adapter = ComposioContext(None, "America/Toronto")
        adapter.connections(accounts("personal"))
        request = {
            "tools": [
                {
                    "tool_slug": "GOOGLECALENDAR_CREATE_EVENT",
                    "arguments": {
                        "summary": "BJJ",
                        "calendar_id": "primary",
                        "start_datetime": "2026-10-10T11:15:00-04:00",
                        "end_datetime": "2026-10-10T12:15:00-04:00",
                    },
                }
            ]
        }
        original = copy.deepcopy(request)
        calls = []

        async def invoke(tool, args):
            calls.append(args)
            assert args["tools"][0]["account"] == "personal"
            fields = args["tools"][0]["arguments"]
            assert fields["timezone"] == "America/Toronto"
            return ToolResult(
                structured_content={
                    "successful": True,
                    "data": {
                        "results": [
                            {
                                "index": 0,
                                "tool_slug": "GOOGLECALENDAR_CREATE_EVENT",
                                "response": {
                                    "successful": True,
                                    "data": {
                                        "response_data": {
                                            "id": "event",
                                            "summary": "BJJ",
                                            "start": {
                                                "dateTime": fields["start_datetime"],
                                                "timeZone": fields["timezone"],
                                            },
                                            "end": {
                                                "dateTime": "2026-10-10T13:15:00-04:00"
                                                if wrong_time
                                                else fields["end_datetime"],
                                                "timeZone": fields["timezone"],
                                            },
                                        }
                                    },
                                },
                            }
                        ]
                    },
                }
            )

        result = await adapter.call("COMPOSIO_MULTI_EXECUTE_TOOL", request, invoke)
        assert request == original
        verification = result.structured_content["data"]["results"][0]["stacy_verification"]
        assert verification["confirmed"] is not wrong_time
        assert len(calls) == 1

    asyncio.run(scenario())
