import asyncio
from pathlib import Path

from fastmcp import Client, FastMCP
from fastmcp.server.providers.skills import SkillsDirectoryProvider
from fastmcp.server.transforms.resources_as_tools import ResourcesAsTools

from stacy.agent_instructions import instructions
from stacy.config import Settings
from stacy.tool_index import load_tool_index


def test_shared_skill_catalog_loads_only_descriptions_and_can_read_the_selected_workflow(tmp_path):
    async def scenario():
        host = FastMCP("skills")
        host.add_provider(SkillsDirectoryProvider(roots=Path("src/stacy/skills")))
        host.add_transform(ResourcesAsTools(host))
        async with Client(host) as client:
            index = await load_tool_index(client, await client.list_tools())
            skills = {s["name"]: s for s in index["skills"]}
            assert set(skills) == {"calendar", "tasks", "spotify", "connected-apps"}
            for channel in ("voice", "imessage"):
                prompt = instructions(
                    Settings(tmp_path, tmp_path / "memory"), channel=channel, tool_index=index
                )
                assert skills["calendar"]["uri"] in prompt
                assert "midnight inclusive" not in prompt
            loaded = await client.call_tool("read_resource", {"uri": skills["calendar"]["uri"]})
            assert not loaded.is_error
            assert "midnight" in str(loaded.content)
            assert "Google accounts distinct" in str(loaded.content)
            # Catalog listing exposes no additional provider schema fetches;
            # one generic resource reader serves any number of skills.
            assert {t.name for t in await client.list_tools()} == {
                "read_resource",
                "list_resources",
            }

    asyncio.run(scenario())
