"""Compare production text turns with simulated Composio Spotify (no real playback).

The live model incurs normal API usage. Provider delays and tool results are fixtures;
reported latency is model/local-harness latency, not live Composio performance.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
import time
from dataclasses import replace
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from unittest.mock import patch

from fastmcp import FastMCP

from stacy.agent import AgentMessage, TextAgent
from stacy.agent_instructions import SPOTIFY_ROUTING, instructions
from stacy.composio_context import json_result
from stacy.config import Settings
from stacy.spotify_workflow import SpotifyWorkflow


def schema(properties, required=()):
    return {"type": "object", "properties": properties, "required": list(required)}


def fixture_tools(*, dual_backend=False):
    host = FastMCP("simulated Composio")
    state = {
        "is_playing": False,
        "device_id": "echo-fixture",
        "writes": [],
        "calls": [],
        "batches": [],
        "local_calls": [],
        "known_schemas": [],
        "schema_violations": [],
        "context_uri": None,
        "track_number": 0,
    }
    if dual_backend:

        @host.tool
        def spotify_devices():
            """List this Soloist player only; it cannot select other Spotify devices."""
            state["local_calls"].append("devices")
            return {"devices": [{"id": "soloist", "name": "Stacy", "is_active": False}]}

        @host.tool
        def spotify_control(action: str, uri: str | None = None, device_id: str | None = None):
            """Control this Soloist player. After a command, verify with spotify_status."""
            state["local_calls"].append(action)
            return {"accepted": True, "command": action, "verified": False}

        @host.tool
        def spotify_status():
            """Read actual Soloist playback state, track, device and position to verify commands."""
            state["local_calls"].append("status")
            return {
                "is_playing": False,
                "device": {"id": "soloist", "name": "Stacy", "is_active": False},
            }

    apps = {
        "SPOTIFY_GET_AVAILABLE_DEVICES": schema({}),
        "SPOTIFY_GET_PLAYBACK_STATE": schema({}),
        "SPOTIFY_SEARCH_FOR_ITEM": schema(
            {
                "q": {"type": "string"},
                "type": {"type": "array", "items": {"type": "string"}},
                "limit": {"type": "integer"},
            },
            ["q", "type"],
        ),
        "SPOTIFY_START_RESUME_PLAYBACK": schema(
            {"device_id": {"type": "string"}, "context_uri": {"type": "string"}},
            ["device_id", "context_uri"],
        ),
        "SPOTIFY_PAUSE_PLAYBACK": schema({"device_id": {"type": "string"}}, ["device_id"]),
        "SPOTIFY_SKIP_TO_NEXT": schema({"device_id": {"type": "string"}}),
        "SPOTIFY_SKIP_TO_PREVIOUS": schema({"device_id": {"type": "string"}}),
    }
    meta = {
        "COMPOSIO_SEARCH_TOOLS": schema(
            {
                "queries": {"type": "array", "items": {"type": "object"}},
                "session": {"type": "object"},
                "search_strategy": {"type": "string"},
            },
            ["queries"],
        ),
        "COMPOSIO_MULTI_EXECUTE_TOOL": schema(
            {
                "tools": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "tool_slug": {"type": "string"},
                            "arguments": {"type": "object"},
                            "account": {"type": "string"},
                        },
                        "required": ["tool_slug", "arguments", "account"],
                    },
                },
                "session_id": {"type": "string"},
                "thought": {"type": "string"},
                "sync_response_to_workbench": {"type": "boolean"},
                "current_step": {"type": "string"},
                "current_step_metric": {"type": "string"},
            },
            ["tools", "session_id"],
        ),
        "COMPOSIO_GET_TOOL_SCHEMAS": schema(
            {
                "tool_slugs": {"type": "array", "items": {"type": "string"}},
                "session_id": {"type": "string"},
            },
            ["tool_slugs"],
        ),
    }

    @host.tool
    def search_mcp_servers(query: str = "", offset: int = 0):
        state["calls"].append("search_mcp_servers")
        return {
            "servers": [
                {
                    "server": "composio",
                    "description": "Spotify catalog and playback on connected devices",
                }
            ],
            "configured_count": 1,
        }

    @host.tool
    def search_mcp_tools(server: str, query: str):
        state["calls"].append("search_mcp_tools")
        return {
            "server": server,
            "tools": [
                {"name": name, "description": description, "inputSchema": meta[name]}
                for name, description in {
                    "COMPOSIO_SEARCH_TOOLS": (
                        "Discover app tools and connection status. "
                        "Search first for a new workflow. "
                        "Reuse returned session ID for a continuation."
                    ),
                    "COMPOSIO_MULTI_EXECUTE_TOOL": (
                        "Execute known app slugs using complete schemas and an active account."
                    ),
                    "COMPOSIO_GET_TOOL_SCHEMAS": (
                        "Get complete app tool input schemas when missing."
                    ),
                }.items()
            ],
        }

    @host.tool
    def call_mcp_tool(server: str, tool: str, arguments: dict):
        state["calls"].append(tool)
        if tool in {"COMPOSIO_SEARCH_TOOLS", "COMPOSIO_GET_TOOL_SCHEMAS"}:
            available = apps
            if dual_backend and tool == "COMPOSIO_SEARCH_TOOLS":
                query = json.dumps(arguments.get("queries", [])).casefold()
                if "pause" not in query and "stop" not in query:
                    available = {k: v for k, v in apps.items() if k != "SPOTIFY_PAUSE_PLAYBACK"}
            if tool == "COMPOSIO_GET_TOOL_SCHEMAS":
                requested = arguments.get("tool_slugs", list(apps))
                available = {k: v for k, v in apps.items() if k in requested}
            state["known_schemas"] = list(set(state["known_schemas"]) | set(available))
            return {
                "successful": True,
                "data": {
                    "session": {"id": "fixture-session"},
                    "toolkit_connection_statuses": [
                        {
                            "toolkit": "spotify",
                            "status": "ACTIVE",
                            "accounts": [{"id": "fixture-account", "status": "ACTIVE"}],
                        }
                    ],
                    "tool_schemas": {
                        slug: {"toolkit": "spotify", "input_schema": definition}
                        for slug, definition in available.items()
                    },
                    "additional_tool_slugs": [slug for slug in apps if slug not in available],
                    "results": [
                        {
                            "primary_tool_slugs": list(available),
                            "recommended_plan_steps": [
                                (
                                    "Search music and resolve the requested device, "
                                    "start playback and verify. "
                                    "For stop, pause the previously resolved device "
                                    "then verify fresh playback."
                                )
                            ],
                        }
                    ],
                },
            }
        if tool != "COMPOSIO_MULTI_EXECUTE_TOOL":
            raise ValueError("Unknown fixture meta tool")
        # Composio batches can run concurrently: a read in a write batch may
        # observe pre-action state. Never count list ordering as verification ordering.
        state["batches"].append([action["tool_slug"] for action in arguments["tools"]])
        snapshot = state["is_playing"]
        results = []
        for action in arguments["tools"]:
            slug, args = action["tool_slug"], action.get("arguments", {})
            state["calls"].append(slug)
            if dual_backend and slug not in state["known_schemas"]:
                state["schema_violations"].append(slug)
                results.append(
                    {
                        "tool_slug": slug,
                        "response": {
                            "successful": False,
                            "error": "Complete input schema was not fetched; no action executed.",
                        },
                    }
                )
                continue
            if action.get("account") != "fixture-account":
                raise ValueError("Fixture account must be explicit")
            if slug == "SPOTIFY_GET_AVAILABLE_DEVICES":
                data = {
                    "devices": [
                        {
                            "id": "echo-fixture",
                            "name": "Echo Show",
                            "is_active": True,
                            "is_restricted": False,
                        },
                        {"id": "mac-fixture", "name": "MacBook", "is_active": False},
                    ]
                }
            elif slug == "SPOTIFY_SEARCH_FOR_ITEM":
                data = {
                    "playlists": {
                        "items": [
                            {
                                "id": "03szYyfzDxfplwydvzRyv3",
                                "name": "Chill Classical",
                                "uri": "spotify:playlist:03szYyfzDxfplwydvzRyv3",
                            }
                        ]
                    }
                }
            elif slug in {"SPOTIFY_START_RESUME_PLAYBACK", "SPOTIFY_PAUSE_PLAYBACK"}:
                if args.get("device_id") != "echo-fixture":
                    raise ValueError("Wrong fixture target")
                state["writes"].append(slug)
                state["is_playing"] = slug == "SPOTIFY_START_RESUME_PLAYBACK"
                if slug == "SPOTIFY_START_RESUME_PLAYBACK":
                    state["context_uri"] = args.get("context_uri")
                data = {"success": True}
            elif slug in {"SPOTIFY_SKIP_TO_NEXT", "SPOTIFY_SKIP_TO_PREVIOUS"}:
                if args.get("device_id") != "echo-fixture":
                    raise ValueError("Wrong fixture target")
                state["writes"].append(slug)
                state["track_number"] += 1 if slug == "SPOTIFY_SKIP_TO_NEXT" else -1
                data = {"success": True}
            elif slug == "SPOTIFY_GET_PLAYBACK_STATE":
                data = {
                    "is_playing": snapshot,
                    "device": {"id": "echo-fixture", "name": "Echo Show", "is_active": True},
                    "context": {"uri": state["context_uri"]},
                    "item": {
                        "name": "Bluebird",
                        "artists": [{"name": "Alexis Ffrench"}],
                        "uri": f"spotify:track:fixture{state['track_number']}",
                    },
                }
            else:
                raise ValueError("Unknown fixture app tool")
            results.append({"tool_slug": slug, "response": {"successful": True, "data": data}})
        errors = sum(not item["response"]["successful"] for item in results)
        return {
            "successful": True,
            "data": {
                "results": results,
                "error_count": errors,
                "session": {"id": "fixture-session"},
            },
        }

    return host, state


def verification_checks(state, calls, batches, expected, *, playing, local_begin=0):
    write_batch = next((i for i, batch in enumerate(batches) if expected in batch), None)
    return {
        "one_requested_write": state["writes"].count(expected) == 1 and calls.count(expected) == 1,
        "fresh_verification": write_batch is not None
        and any("SPOTIFY_GET_PLAYBACK_STATE" in batch for batch in batches[write_batch + 1 :]),
        "expected_state": state["is_playing"] is playing,
        "correct_backend": not state["local_calls"][local_begin:],
        "schema_before_execution": not state["schema_violations"],
    }


async def evaluate(
    settings,
    retain,
    repeat,
    report=None,
    *,
    routing=None,
    workflow=None,
    prefix=None,
    index=None,
    playlist=None,
):
    rows = []
    with tempfile.TemporaryDirectory(prefix="stacy-harness-") as directory:
        root = Path(directory)
        tools, state = fixture_tools(dual_backend=routing is not None or workflow is not None)
        if workflow:
            from fastmcp import Client

            async def invoke(name, arguments):
                async with Client(tools) as client:
                    return await client.call_tool(
                        "call_mcp_tool",
                        {"server": "composio", "tool": name, "arguments": arguments},
                    )

            async def metadata():
                async with Client(tools) as client:
                    result = await client.call_tool(
                        "search_mcp_tools",
                        {"server": "composio", "query": "search schemas execute"},
                    )
                return {t["name"]: t["inputSchema"] for t in json_result(result)["tools"]}

            SpotifyWorkflow(invoke, metadata=metadata).register(tools, playlist=playlist is True)
        if index:
            from stacy.tool_index import TOOL_INDEX_URI

            @tools.resource(TOOL_INDEX_URI)
            def capability_index():
                return {"servers": [{"server": "composio", "description": "Connected app actions"}]}

        agent = TextAgent(
            replace(settings, data_dir=root, memory_dir=root / "memory"),
            tools,
            retain_tool_context=retain,
            cache_prompt=prefix is True,
            include_tool_index=index is True,
        )
        try:
            for turn, text in enumerate(
                [
                    "Please play some chill classical music on my Echo Show."
                    + (
                        " Use Composio Spotify and verify it's playing there."
                        if routing is not None or workflow is not None
                        else ""
                    ),
                    "Alright stop it please.",
                ]
            ):
                begin = len(state["calls"])
                batch_begin = len(state["batches"])
                local_begin = len(state["local_calls"])
                started = time.perf_counter()
                message = AgentMessage("benchmark", "fixture-dm", "fixture-owner", str(turn), text)

                def prompt(*args, **kwargs):
                    value = instructions(*args, **kwargs)
                    return value if routing is not False else value.replace(SPOTIFY_ROUTING, "")

                with patch("stacy.agent.instructions", prompt):
                    await agent.respond(message)
                elapsed = time.perf_counter() - started
                calls = state["calls"][begin:]
                expected = (
                    "SPOTIFY_START_RESUME_PLAYBACK" if turn == 0 else "SPOTIFY_PAUSE_PLAYBACK"
                )
                batches = state["batches"][batch_begin:]
                checks = verification_checks(
                    state, calls, batches, expected, playing=turn == 0, local_begin=local_begin
                )
                before = json.dumps(state)
                await agent.respond(message)
                assert json.dumps(state) == before, "Redelivery repeated tools"
                traces = sorted(
                    (root / "traces").glob("text-*.jsonl"), key=lambda p: p.stat().st_mtime
                )
                events = [json.loads(line) for line in traces[-2].read_text().splitlines()]
                ack = next((e for e in events if e["event"] == "workflow_action_ack"), None)
                row = {
                    "variant": ("playlist-workflow" if playlist else "playlist-baseline")
                    if playlist is not None
                    else ("capability-index" if index else "index-baseline")
                    if index is not None
                    else ("cached-prefix" if prefix else "prefix-baseline")
                    if prefix is not None
                    else ("verified-workflow" if workflow else "workflow-baseline")
                    if workflow is not None
                    else ("device-routing" if routing else "routing-baseline")
                    if routing is not None
                    else ("retained" if retain else "baseline"),
                    "repeat": repeat,
                    "turn": "play" if turn == 0 else "stop",
                    "seconds": round(elapsed, 3),
                    "action_ack_seconds": round(
                        (
                            datetime.fromisoformat(ack["time"])
                            - datetime.fromisoformat(events[0]["time"])
                        ).total_seconds(),
                        3,
                    )
                    if ack
                    else None,
                    "model_calls": sum(e["event"] == "model_request" for e in events),
                    "input_tokens": sum(
                        e.get("input_tokens", 0) for e in events if e["event"] == "model_usage"
                    ),
                    "cached_tokens": sum(
                        e.get("cached_tokens", 0) for e in events if e["event"] == "model_usage"
                    ),
                    "tool_calls": calls,
                    "local_calls": list(state["local_calls"][local_begin:]),
                    "checks": checks,
                    "verified": all(checks.values()),
                    "case_id": "cold-play" if turn == 0 else "warm-stop",
                    "model": settings.text_model,
                    "config_hash": sha256(
                        json.dumps(
                            {
                                "retain": retain,
                                "routing": routing,
                                "workflow": workflow,
                                "prefix": prefix,
                                "index": index,
                                "playlist": playlist,
                                "model": settings.text_model,
                            },
                            sort_keys=True,
                        ).encode()
                    ).hexdigest()[:16],
                }
                print(json.dumps(row), flush=True)
                rows.append(row)
                if report:
                    destination = report.parent / (report.stem + "-traces")
                    destination.mkdir(exist_ok=True, mode=0o700)
                    trace_path = destination / f"{row['variant']}-{repeat}-{turn}.jsonl"
                    trace_path.write_text("\n".join(json.dumps(e) for e in events) + "\n")
                    trace_path.chmod(0o600)
                    row["trace"] = str(trace_path)
                    with report.open("a") as file:
                        file.write(json.dumps(row) + "\n")
                if routing is not False:
                    assert row["verified"], f"Candidate failed: {checks}"
        finally:
            await agent.close()
    return rows


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--report", type=Path, help="Save benchmark rows as JSONL")
    parser.add_argument(
        "--experiment", choices=["context", "routing", "workflow", "prefix"], default="context"
    )
    args = parser.parse_args()
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text("")
    settings = Settings.load()
    for repeat in range(args.repeats):
        # Alternate ordering to reduce a systematic warm-cache/order advantage.
        for candidate in [False, True] if repeat % 2 == 0 else [True, False]:
            await evaluate(
                settings,
                candidate if args.experiment == "context" else True,
                repeat,
                args.report,
                routing=candidate if args.experiment == "routing" else None,
                workflow=candidate
                if args.experiment == "workflow"
                else True
                if args.experiment == "prefix"
                else None,
                prefix=candidate if args.experiment == "prefix" else None,
            )


if __name__ == "__main__":
    asyncio.run(main())
