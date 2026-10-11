"""A small local capability map; provider catalogs stay behind lazy discovery."""

from __future__ import annotations

import json

from stacy.tracing import diagnostic_value

TOOL_INDEX_URI = "stacy://capability-index"


def build_tool_index(schemas, servers=(), skills=()) -> dict:
    def summary(value):
        clean = diagnostic_value(str(value))
        return " ".join(str(clean).split())[:110]

    # The callable schemas already contain native descriptions and parameters.
    # Repeating them here adds prompt work without adding routing information.
    direct = [{"tool": t.name} for t in sorted(schemas, key=lambda t: t.name)]
    remote = [
        {"server": s["server"], "does": summary(s.get("description", s.get("does", "")))}
        for s in servers
        if isinstance(s, dict) and isinstance(s.get("server"), str)
    ]
    index = {
        "direct": [],
        "servers": [],
        "skills": [],
        "omitted_tools": 0,
        "omitted_servers": 0,
        "omitted_skills": 0,
    }
    # Reserve space for both maps; never grow with all N upstream tool catalogs.
    for key, items, budget in (("direct", direct, 3800), ("servers", remote, 2200)):
        for item in items:
            if len(json.dumps(index[key] + [item])) > budget:
                break
            index[key].append(item)
        index["omitted_tools" if key == "direct" else "omitted_servers"] = len(items) - len(
            index[key]
        )
    for item in skills:
        if len(json.dumps(index["skills"] + [item])) > 2200:
            break
        index["skills"].append(item)
    index["omitted_skills"] = len(skills) - len(index["skills"])
    return index


async def load_tool_index(client, schemas) -> dict:
    """Read local server descriptors. This never contacts a provider or resolves credentials."""
    servers = []
    omitted = 0
    resources = await client.list_resources()
    skills = [
        {
            "name": r.name.removesuffix("/SKILL.md"),
            "uri": str(r.uri),
            "does": " ".join((r.description or "").split())[:220],
        }
        for r in resources
        if str(r.uri).startswith("skill://")
        and str(r.uri).endswith("/SKILL.md")
        and str(r.uri) != "skill://agent-memory-repo/SKILL.md"
    ]
    if any(str(r.uri) == TOOL_INDEX_URI for r in resources):
        blocks = await client.read_resource(TOOL_INDEX_URI)
        for block in blocks:
            if getattr(block, "text", None):
                value = json.loads(block.text)
                servers = value.get("servers", [])
                omitted = value.get("omitted_servers", 0)
                break
    index = build_tool_index(schemas, servers, skills)
    index["omitted_servers"] += omitted
    return index
