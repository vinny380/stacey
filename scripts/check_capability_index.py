"""Measure index routing where no direct workflow covers the request (fixture provider)."""

from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
import time
from dataclasses import replace
from pathlib import Path

from scripts.autoresearch_harness import decision, source_hash
from scripts.check_text_harness import fixture_tools
from stacy.harness.text import AgentMessage, TextAgent
from stacy.harness.tool_index import TOOL_INDEX_URI
from stacy.runtime.settings import Settings


async def evaluate(settings, indexed, repeat, output):
    with tempfile.TemporaryDirectory(prefix="stacy-index-") as directory:
        root = Path(directory)
        host, state = fixture_tools(dual_backend=True)

        @host.resource(TOOL_INDEX_URI)
        def index():
            return {"servers": [{"server": "composio", "description": "Connected app actions"}]}

        agent = TextAgent(
            replace(settings, data_dir=root, memory_dir=root / "memory"),
            host,
            include_tool_index=indexed,
        )
        try:
            started = time.perf_counter()
            await agent.respond(
                AgentMessage(
                    "benchmark",
                    "fixture-dm",
                    "fixture-owner",
                    "devices",
                    "List my available Spotify playback devices "
                    "through the configured Composio connection.",
                )
            )
            elapsed = time.perf_counter() - started
        finally:
            await agent.close()
        path = next((root / "traces").glob("text-*.jsonl"))
        events = [json.loads(line) for line in path.read_text().splitlines()]
        checks = {
            "actual_devices_read": "SPOTIFY_GET_AVAILABLE_DEVICES" in state["calls"],
            "correct_backend": not state["local_calls"],
            "no_mutation": not state["writes"],
            "schema_before_execution": not state["schema_violations"],
        }
        row = {
            "variant": "indexed" if indexed else "unindexed",
            "repeat": repeat,
            "turn": "devices",
            "case_id": "generic-spotify-devices",
            "seconds": round(elapsed, 3),
            "model_calls": sum(e["event"] == "model_request" for e in events),
            "input_tokens": sum(
                e.get("input_tokens", 0) for e in events if e["event"] == "model_usage"
            ),
            "cached_tokens": sum(
                e.get("cached_tokens", 0) for e in events if e["event"] == "model_usage"
            ),
            "calls": state["calls"],
            "checks": checks,
            "verified": all(checks.values()),
        }
        destination = output / f"{row['variant']}-{repeat}.jsonl"
        destination.write_text(path.read_text())
        destination.chmod(0o600)
        row["trace"] = str(destination)
        with (output / "rows.jsonl").open("a") as file:
            file.write(json.dumps(row) + "\n")
        print(json.dumps(row), flush=True)
        return row


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False, mode=0o700)
    settings = Settings.load()
    fingerprint = source_hash()
    samples = {False: [], True: []}
    for repeat in range(args.repeats):
        for indexed in [False, True] if repeat % 2 == 0 else [True, False]:
            samples[indexed].append(await evaluate(settings, indexed, repeat, args.output))
    if source_hash() != fingerprint:
        raise RuntimeError("Sources changed during experiment")
    verdict = {
        "source_sha256": fingerprint,
        "repeats": args.repeats,
        **decision(samples[False], samples[True]),
    }
    (args.output / "results.json").write_text(json.dumps(verdict, indent=2) + "\n")
    print(json.dumps(verdict), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
