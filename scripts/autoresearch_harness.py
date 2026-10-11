"""Hill climb bounded harness features using the real model and simulated providers.

No provider mutations or messages are sent. Alternating paired runs retain redacted
traces, feature/source fingerprints and failed trials. Promotion is a recommendation;
this script never edits production code or settings.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import subprocess
import sys
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

from scripts.check_text_harness import evaluate
from stacy.config import Settings


@dataclass(frozen=True)
class Variant:
    workflow: bool = False
    prefix: bool = False
    index: bool = False
    playlist: bool = False


def decision(baseline: list[dict], candidate: list[dict], *, objective: str = "latency") -> dict:
    """A fast invalid outcome is never an improvement; cache savings are separate."""
    if not baseline or not candidate or len(baseline) != len(candidate):
        return {"accepted": False, "reason": "Incomplete paired evidence"}
    if not all(row.get("verified") for row in baseline + candidate):
        return {"accepted": False, "reason": "Correctness gate failed"}
    if sorted((r["repeat"], r["turn"]) for r in baseline) != sorted(
        (r["repeat"], r["turn"]) for r in candidate
    ):
        return {"accepted": False, "reason": "Cases are not paired"}
    before = statistics.median(r["seconds"] for r in baseline)
    after = statistics.median(r["seconds"] for r in candidate)
    tail_safe = max(r["seconds"] for r in candidate) <= 1.25 * max(r["seconds"] for r in baseline)
    speed_win = after <= 0.95 * before
    before_uncached = sum(r["input_tokens"] - r.get("cached_tokens", 0) for r in baseline)
    after_uncached = sum(r["input_tokens"] - r.get("cached_tokens", 0) for r in candidate)
    cache_win = after_uncached < 0.5 * before_uncached and after <= 1.1 * before
    case_safe = all(
        statistics.median(r["seconds"] for r in candidate if r["turn"] == turn)
        <= 1.1 * statistics.median(r["seconds"] for r in baseline if r["turn"] == turn)
        for turn in {r["turn"] for r in baseline}
    )
    accepted = tail_safe and case_safe and (speed_win if objective == "latency" else cache_win)
    return {
        "accepted": accepted,
        "reason": "Lower median latency"
        if accepted and speed_win
        else "Lower uncached input with latency within 10%"
        if accepted
        else "No measured improvement within the latency guard",
        "median_seconds": {"incumbent": round(before, 3), "candidate": round(after, 3)},
        "uncached_input_tokens": {"incumbent": before_uncached, "candidate": after_uncached},
        "tail_guard_passed": tail_safe,
        "case_latency_guard_passed": case_safe,
        "objective": objective,
        "sample_note": "Small paired sample; keep evaluating production traces.",
    }


def source_hash() -> str:
    root = Path(__file__).resolve().parents[1]
    sources = [
        "src/stacy/agent.py",
        "src/stacy/agent_instructions.py",
        "src/stacy/spotify_workflow.py",
        "src/stacy/mcp_gateway.py",
        "src/stacy/composio_context.py",
        "src/stacy/tool_index.py",
        "scripts/check_text_harness.py",
    ]
    digest = sha256()
    for name in sources:
        digest.update(name.encode())
        digest.update((root / name).read_bytes())
    return digest.hexdigest()


async def research(
    output: Path,
    repeats: int,
    features: list[str],
    objective: str = "latency",
    *,
    incumbent_workflow: bool = False,
):
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    settings = Settings.load()
    initial_hash = source_hash()
    incumbent = Variant(workflow=incumbent_workflow)
    ledger = []
    # Regression gates exercise provider schemas, ambiguity, uncertainty, expiry,
    # isolation and redelivery without making model calls or touching real accounts.
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "tests/test_spotify_workflow.py",
            "tests/test_text_harness_eval.py",
            "tests/test_mcp_gateway.py",
            "tests/test_agent.py",
            "tests/test_working_context.py",
            "tests/test_tool_index.py",
        ],
        check=True,
    )
    for feature in features:
        if feature in {"prefix", "playlist"} and not incumbent.workflow:
            ledger.append(
                {
                    "feature": feature,
                    "accepted": False,
                    "reason": "Workflow candidate was not accepted; preserve isolation",
                }
            )
            continue
        proposal = replace(incumbent, **{feature: True})
        report = output / f"{feature}.jsonl"
        samples = {False: [], True: []}
        for repeat in range(repeats):
            for is_candidate in [False, True] if repeat % 2 == 0 else [True, False]:
                variant = proposal if is_candidate else incumbent
                try:
                    rows = await evaluate(
                        settings,
                        True,
                        repeat,
                        report,
                        workflow=variant.workflow,
                        prefix=variant.prefix if feature == "prefix" else None,
                        index=variant.index
                        if feature == "index"
                        else True
                        if variant.index
                        else None,
                        playlist=variant.playlist
                        if feature == "playlist"
                        else True
                        if variant.playlist
                        else None,
                    )
                    samples[is_candidate].extend(rows)
                except Exception as error:
                    # Avoid logging raw provider exception strings or prompts.
                    ledger.append(
                        {
                            "feature": feature,
                            "candidate": is_candidate,
                            "repeat": repeat,
                            "error_type": type(error).__name__,
                        }
                    )
        verdict = decision(samples[False], samples[True], objective=objective)
        ledger.append(
            {
                "feature": feature,
                "incumbent": asdict(incumbent),
                "proposal": asdict(proposal),
                **verdict,
            }
        )
        print(json.dumps(ledger[-1]), flush=True)
        if verdict["accepted"]:
            incumbent = proposal
        if source_hash() != initial_hash:
            raise RuntimeError("Sources changed during experiment; do not promote mixed evidence")
    result = {
        "model": settings.text_model,
        "objective": objective,
        "repeats": repeats,
        "source_sha256": initial_hash,
        "recommended_features": asdict(incumbent),
        "experiments": ledger,
    }
    (output / "results.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--objective", choices=["latency", "cost"], default="latency")
    parser.add_argument(
        "--features",
        nargs="+",
        choices=["workflow", "prefix", "index", "playlist"],
        default=["workflow", "prefix"],
    )
    parser.add_argument("--incumbent-workflow", action="store_true")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(".scratch/harness-optimization")
        / ("autoresearch-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S")),
    )
    args = parser.parse_args()
    if args.repeats < 2:
        parser.error("Use at least two alternating paired repeats")
    await research(
        args.output,
        args.repeats,
        list(dict.fromkeys(args.features)),
        args.objective,
        incumbent_workflow=args.incumbent_workflow,
    )


if __name__ == "__main__":
    asyncio.run(main())
