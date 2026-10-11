"""Protect the evaluation from counting wrong-player or concurrent reads as success."""

import asyncio

from fastmcp import Client

from scripts.autoresearch_harness import decision
from scripts.check_text_harness import fixture_tools, verification_checks


def test_autoresearch_rejects_faster_invalid_trials_and_requires_paired_cases():
    baseline = [
        {"repeat": 0, "turn": "stop", "verified": True, "seconds": 10, "input_tokens": 1000}
    ]
    failed = [{"repeat": 0, "turn": "stop", "verified": False, "seconds": 1, "input_tokens": 1000}]
    assert decision(baseline, failed)["accepted"] is False
    assert decision(baseline, [dict(failed[0], verified=True, turn="play")])["accepted"] is False
    assert decision(baseline, [dict(failed[0], verified=True)])["accepted"] is True


def test_cost_savings_do_not_promote_a_slower_feature_for_latency_objective():
    before = [
        {
            "repeat": 0,
            "turn": "stop",
            "verified": True,
            "seconds": 2,
            "input_tokens": 10000,
            "cached_tokens": 0,
        }
    ]
    after = [dict(before[0], seconds=2.1, cached_tokens=9500)]
    assert decision(before, after)["accepted"] is False
    assert decision(before, after, objective="cost")["accepted"] is True


def test_fixture_requires_verification_after_the_write_batch():
    async def scenario():
        tools, state = fixture_tools(dual_backend=True)
        state["is_playing"] = True

        async with Client(tools) as client:
            await client.call_tool(
                "call_mcp_tool",
                {
                    "server": "composio",
                    "tool": "COMPOSIO_GET_TOOL_SCHEMAS",
                    "arguments": {
                        "tool_slugs": ["SPOTIFY_PAUSE_PLAYBACK", "SPOTIFY_GET_PLAYBACK_STATE"],
                        "session_id": "fixture-session",
                    },
                },
            )

            async def execute(slugs):
                return await client.call_tool(
                    "call_mcp_tool",
                    {
                        "server": "composio",
                        "tool": "COMPOSIO_MULTI_EXECUTE_TOOL",
                        "arguments": {
                            "session_id": "fixture-session",
                            "tools": [
                                {
                                    "tool_slug": slug,
                                    "arguments": {"device_id": "echo-fixture"},
                                    "account": "fixture-account",
                                }
                                for slug in slugs
                            ],
                        },
                    },
                )

            result = await execute(["SPOTIFY_PAUSE_PLAYBACK", "SPOTIFY_GET_PLAYBACK_STATE"])
            observed = result.data["data"]["results"][1]["response"]["data"]["is_playing"]
            assert observed is True  # read and write in one batch are not ordered
            checks = verification_checks(
                state, state["calls"], state["batches"], "SPOTIFY_PAUSE_PLAYBACK", playing=False
            )
            assert checks["fresh_verification"] is False
            result = await execute(["SPOTIFY_GET_PLAYBACK_STATE"])
            assert result.data["data"]["results"][0]["response"]["data"]["is_playing"] is False
            assert all(
                verification_checks(
                    state, state["calls"], state["batches"], "SPOTIFY_PAUSE_PLAYBACK", playing=False
                ).values()
            )
            await client.call_tool("spotify_control", {"action": "pause"})
            checks = verification_checks(
                state, state["calls"], state["batches"], "SPOTIFY_PAUSE_PLAYBACK", playing=False
            )
            assert checks["correct_backend"] is False

    asyncio.run(scenario())


def test_missing_schema_blocks_fixture_write_and_fails_the_evaluation():
    async def scenario():
        tools, state = fixture_tools(dual_backend=True)
        state["is_playing"] = True
        async with Client(tools) as client:
            result = await client.call_tool(
                "call_mcp_tool",
                {
                    "server": "composio",
                    "tool": "COMPOSIO_MULTI_EXECUTE_TOOL",
                    "arguments": {
                        "session_id": "fixture-session",
                        "tools": [
                            {
                                "tool_slug": "SPOTIFY_PAUSE_PLAYBACK",
                                "arguments": {"device_id": "echo-fixture"},
                                "account": "fixture-account",
                            }
                        ],
                    },
                },
            )
            assert result.data["data"]["error_count"] == 1
            assert state["is_playing"] is True
            assert state["writes"] == []
            checks = verification_checks(
                state, state["calls"], state["batches"], "SPOTIFY_PAUSE_PLAYBACK", playing=False
            )
            assert checks["one_requested_write"] is False
            assert checks["schema_before_execution"] is False

    asyncio.run(scenario())
