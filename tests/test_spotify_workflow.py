import asyncio
import json

import pytest
from fastmcp import Client
from fastmcp.tools import ToolResult

from scripts.check_text_harness import fixture_tools
from stacy.composio_context import json_result
from stacy.spotify_workflow import SpotifyWorkflow
from stacy.tracing import TextTrace


def workflow(host, *, transform=None, clock=None):
    async def invoke(name, arguments):
        async with Client(host) as client:
            result = await client.call_tool(
                "call_mcp_tool", {"server": "composio", "tool": name, "arguments": arguments}
            )
        if transform:
            return transform(name, arguments, result)
        return result

    async def metadata():
        async with Client(host) as client:
            result = await client.call_tool(
                "search_mcp_tools", {"server": "composio", "query": "search schemas execute"}
            )
        return {t["name"]: t["inputSchema"] for t in json_result(result)["tools"]}

    return SpotifyWorkflow(invoke, metadata=metadata, **({"clock": clock} if clock else {}))


def changed(result, update):
    payload = json_result(result)
    update(payload["data"])
    return ToolResult(structured_content=payload)


@pytest.mark.parametrize(
    "action,slug", [("next", "SPOTIFY_SKIP_TO_NEXT"), ("previous", "SPOTIFY_SKIP_TO_PREVIOUS")]
)
def test_skip_keeps_account_device_and_verifies_track_change(action, slug):
    async def scenario():
        host, state = fixture_tools(dual_backend=True)
        api = workflow(host)
        state["is_playing"] = True
        result = await api.control(action, "echo-fixture", "fixture-account")
        assert result["verified"] is True
        assert state["writes"] == [slug]
        assert state["batches"][-3:] == [
            ["SPOTIFY_GET_PLAYBACK_STATE"],
            [slug],
            ["SPOTIFY_GET_PLAYBACK_STATE"],
        ]
        assert not state["schema_violations"] and not state["local_calls"]

    asyncio.run(scenario())


def test_find_then_play_and_pause_use_explicit_target_and_ordered_fresh_reads():
    async def scenario():
        host, state = fixture_tools(dual_backend=True)
        api = workflow(host)
        found = await api.find("chill classical", "Echo Show")
        assert found["account"] == "fixture-account"
        assert found["device"] == {"id": "echo-fixture", "name": "Echo Show"}
        assert found["candidates"][0]["uri"].startswith("spotify:playlist:")
        played = await api.control(
            "play", "echo-fixture", "fixture-account", found["candidates"][0]["uri"]
        )
        assert played["verified"] is True
        assert played["is_playing"] is True
        discovery_before = state["calls"].count("COMPOSIO_SEARCH_TOOLS")
        paused = await api.control("pause", "echo-fixture", "fixture-account")
        assert paused["verified"] is True
        assert paused["is_playing"] is False
        assert state["batches"][-2:] == [["SPOTIFY_PAUSE_PLAYBACK"], ["SPOTIFY_GET_PLAYBACK_STATE"]]
        assert state["calls"].count("COMPOSIO_SEARCH_TOOLS") == discovery_before
        assert state["writes"] == ["SPOTIFY_START_RESUME_PLAYBACK", "SPOTIFY_PAUSE_PLAYBACK"]
        assert not state["schema_violations"]
        assert not state["local_calls"]
        assert len(json.dumps(found)) < 2000

    asyncio.run(scenario())


def test_playlist_play_is_one_tool_with_one_targeted_write_and_fresh_verification():
    async def scenario():
        host, state = fixture_tools(dual_backend=True)
        api = workflow(host)
        api.register(host)
        async with Client(host) as client:
            result = await client.call_tool(
                "composio_spotify_play", {"query": "chill classical", "device_name": "Echo"}
            )
        assert result.data["verified"] is True
        assert result.data["selection"]["uri"] == state["context_uri"]
        assert result.data["device"]["id"] == "echo-fixture"
        assert state["writes"] == ["SPOTIFY_START_RESUME_PLAYBACK"]
        assert state["batches"][-2:] == [
            ["SPOTIFY_START_RESUME_PLAYBACK"],
            ["SPOTIFY_GET_PLAYBACK_STATE"],
        ]
        assert not state["schema_violations"]
        assert not state["local_calls"]

    asyncio.run(scenario())


def test_preparation_is_read_only_and_playlist_retry_cannot_replay_uncertain_write(tmp_path):
    async def scenario():
        host, state = fixture_tools(dual_backend=True)

        def transform(name, args, result):
            if name == "COMPOSIO_MULTI_EXECUTE_TOOL" and any(
                t["tool_slug"] == "SPOTIFY_START_RESUME_PLAYBACK" for t in args["tools"]
            ):
                raise TimeoutError("response lost after playback started")
            return result

        api = workflow(host, transform=transform)
        assert (await api.prepare())["ready"] is True
        assert not state["writes"] and not state["batches"]
        discovery_count = state["calls"].count("COMPOSIO_SEARCH_TOOLS")
        with TextTrace(tmp_path).activate():
            result = await api.play("classical", "Echo")
            assert result["status"] == "uncertain"
            repeated = await api.play("classical", "Echo", "fixture-account")
            assert repeated == result
        assert state["calls"].count("COMPOSIO_SEARCH_TOOLS") == discovery_count
        assert state["writes"] == ["SPOTIFY_START_RESUME_PLAYBACK"]

    asyncio.run(scenario())


@pytest.mark.parametrize("ambiguity", ["account", "device", "music"])
def test_playlist_tool_never_bypasses_missing_or_ambiguous_choices(ambiguity):
    async def scenario():
        host, state = fixture_tools(dual_backend=True)

        def transform(name, args, result):
            def update(data):
                if name == "COMPOSIO_SEARCH_TOOLS" and ambiguity == "account":
                    data["toolkit_connection_statuses"][0]["accounts"].append(
                        {"id": "another-account", "status": "ACTIVE"}
                    )
                for row in data.get("results", []):
                    if (
                        row.get("tool_slug") == "SPOTIFY_GET_AVAILABLE_DEVICES"
                        and ambiguity == "device"
                    ):
                        row["response"]["data"]["devices"].append(
                            {"id": "another-echo", "name": "Echo upstairs"}
                        )
                    if row.get("tool_slug") == "SPOTIFY_SEARCH_FOR_ITEM" and ambiguity == "music":
                        row["response"]["data"]["playlists"]["items"] = []

            return changed(result, update)

        result = await workflow(host, transform=transform).play("classical", "Echo")
        assert result["status"] == ambiguity + "_required"
        assert not state["writes"]

    asyncio.run(scenario())


def test_schema_reference_is_fetched_before_write_and_invalid_schema_blocks_it():
    async def scenario(invalid):
        host, state = fixture_tools(dual_backend=True)

        def transform(name, arguments, result):
            if name == "COMPOSIO_SEARCH_TOOLS":

                def hide(data):
                    data["tool_schemas"]["SPOTIFY_PAUSE_PLAYBACK"] = {
                        "toolkit": "spotify",
                        "schemaRef": {"tool": "COMPOSIO_GET_TOOL_SCHEMAS"},
                    }
                    state["known_schemas"].remove("SPOTIFY_PAUSE_PLAYBACK")

                return changed(result, hide)
            if name == "COMPOSIO_GET_TOOL_SCHEMAS" and invalid:

                def constrain(data):
                    definition = data["tool_schemas"]["SPOTIFY_PAUSE_PLAYBACK"]["input_schema"]
                    definition["required"].append("unsupported_required_field")
                    definition["properties"]["unsupported_required_field"] = {"type": "string"}

                return changed(result, constrain)
            return result

        result = await workflow(host, transform=transform).control(
            "pause", "echo-fixture", "fixture-account"
        )
        assert "COMPOSIO_GET_TOOL_SCHEMAS" in state["calls"]
        if invalid:
            assert result["verified"] is False
            assert not state["writes"]
        else:
            assert result["verified"] is True
            assert not state["schema_violations"]

    asyncio.run(scenario(False))
    asyncio.run(scenario(True))


def test_plausible_schema_preview_is_not_treated_as_complete():
    async def scenario():
        host, state = fixture_tools(dual_backend=True)

        def transform(name, args, result):
            if name == "COMPOSIO_SEARCH_TOOLS":

                def preview(data):
                    data["tool_schemas"]["SPOTIFY_PAUSE_PLAYBACK"]["hasFullSchema"] = False
                    state["known_schemas"].remove("SPOTIFY_PAUSE_PLAYBACK")

                return changed(result, preview)
            return result

        result = await workflow(host, transform=transform).control(
            "pause", "echo-fixture", "fixture-account"
        )
        assert result["verified"] is True
        assert "COMPOSIO_GET_TOOL_SCHEMAS" in state["calls"]
        assert not state["schema_violations"]

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "accounts",
    [[], [{"id": "fixture-account", "status": "ACTIVE"}, {"id": "other", "status": "ACTIVE"}]],
)
def test_missing_or_ambiguous_account_never_selects_default_or_executes(accounts):
    async def scenario():
        host, state = fixture_tools(dual_backend=True)

        def transform(name, args, result):
            if name == "COMPOSIO_SEARCH_TOOLS":
                return changed(
                    result, lambda d: d["toolkit_connection_statuses"][0].update(accounts=accounts)
                )
            return result

        result = await workflow(host, transform=transform).find("jazz", "Echo")
        assert result["status"] in {"connection_required", "account_required"}
        assert not state["batches"]
        assert not state["writes"]

    asyncio.run(scenario())


def test_ambiguous_device_returns_choices_without_playing():
    async def scenario():
        host, state = fixture_tools(dual_backend=True)

        def transform(name, args, result):
            if name == "COMPOSIO_MULTI_EXECUTE_TOOL":

                def duplicate(data):
                    for row in data["results"]:
                        if row["tool_slug"] == "SPOTIFY_GET_AVAILABLE_DEVICES":
                            row["response"]["data"]["devices"].append(
                                {"id": "other-echo", "name": "Echo Show upstairs"}
                            )

                return changed(result, duplicate)
            return result

        result = await workflow(host, transform=transform).find("jazz", "echo")
        assert result["status"] == "device_required"
        assert len(result["devices"]) == 2
        assert not state["writes"]

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "state_override",
    [
        {"device": {"id": "mac-fixture", "name": "MacBook"}},
        {"is_playing": True},
    ],
)
def test_wrong_target_or_stale_state_does_not_confirm_or_repeat_pause(state_override):
    async def scenario():
        host, state = fixture_tools(dual_backend=True)

        def transform(name, args, result):
            if name == "COMPOSIO_MULTI_EXECUTE_TOOL":

                def override(data):
                    for row in data["results"]:
                        if row["tool_slug"] == "SPOTIFY_GET_PLAYBACK_STATE":
                            row["response"]["data"].update(state_override)

                return changed(result, override)
            return result

        result = await workflow(host, transform=transform).control(
            "pause", "echo-fixture", "fixture-account"
        )
        assert result["accepted"] is True
        assert result["verified"] is False
        assert state["writes"] == ["SPOTIFY_PAUSE_PLAYBACK"]

    asyncio.run(scenario())


def test_uncertain_write_is_not_replayed_in_same_turn(tmp_path):
    async def scenario():
        host, state = fixture_tools(dual_backend=True)

        def transform(name, args, result):
            if name == "COMPOSIO_SEARCH_TOOLS":
                return changed(
                    result,
                    lambda d: d["toolkit_connection_statuses"][0]["accounts"][0].update(
                        alias="personal"
                    ),
                )
            if name == "COMPOSIO_MULTI_EXECUTE_TOOL" and any(
                t["tool_slug"] == "SPOTIFY_PAUSE_PLAYBACK" for t in args["tools"]
            ):
                raise TimeoutError("response lost after device acted")
            return result

        api = workflow(host, transform=transform)
        with TextTrace(tmp_path).activate():
            result = await api.control("pause", "echo-fixture", "personal")
            assert result["status"] == "uncertain"
            assert result["verified"] is False
            repeated = await api.control("pause", "echo-fixture", "fixture-account")
            assert repeated == result
        assert state["writes"] == ["SPOTIFY_PAUSE_PLAYBACK"]

    asyncio.run(scenario())


def test_expiry_and_restart_recheck_account_before_mutation():
    async def scenario():
        host, state = fixture_tools(dual_backend=True)
        now = [0]
        revoked = [False]

        def transform(name, args, result):
            if name == "COMPOSIO_SEARCH_TOOLS" and revoked[0]:
                return changed(
                    result,
                    lambda d: d["toolkit_connection_statuses"][0].update(
                        accounts=[{"id": "fixture-account", "status": "REVOKED"}]
                    ),
                )
            return result

        api = workflow(host, transform=transform, clock=lambda: now[0])
        assert (await api.control("pause", "echo-fixture", "fixture-account"))["verified"]
        revoked[0] = True
        now[0] = 301
        result = await api.control("pause", "echo-fixture", "fixture-account")
        assert result["status"] == "connection_required"
        restored = workflow(host, transform=transform)
        result = await restored.control("pause", "echo-fixture", "fixture-account")
        assert result["status"] == "connection_required"
        assert state["writes"] == ["SPOTIFY_PAUSE_PLAYBACK"]

    asyncio.run(scenario())
