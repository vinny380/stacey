import asyncio

from fastmcp import Client

from stacy.alarms import AlarmService
from stacy.config import Settings
from stacy.memory import MemoryRepo
from stacy.tools import create_tools
from stacy.voice import instructions


def test_existing_skill_provider_and_alarm_tools_work_through_mcp(tmp_path):
    async def noop(value):
        pass

    async def scenario():
        alarms = AlarmService(tmp_path / "state.sqlite3", "America/Toronto", noop, noop)
        memory = MemoryRepo(tmp_path / "memory")
        await alarms.start()
        try:
            async with Client(create_tools(alarms, memory)) as client:
                names = {tool.name for tool in await client.list_tools()}
                assert {
                    "set_timer",
                    "set_alarm",
                    "update_memory",
                    "list_resources",
                    "read_resource",
                } <= names
                resources = await client.call_tool("list_resources", {})
                assert "agent-memory-repo" in str(resources)
                timer = await client.call_tool("set_timer", {"seconds": 60})
                assert timer.data["status"] == "pending"
                await client.call_tool("cancel_alarm", {"alarm_id": timer.data["id"]})
                assert alarms.get(timer.data["id"])["status"] == "cancelled"
                note = await client.call_tool("read_memory", {})
                await client.call_tool(
                    "update_memory",
                    {
                        "path": "MEMORY.md",
                        "content": "# Memory\n- Likes jazz\n\n## Index\n",
                        "revision": note.data["revision"],
                    },
                )
                assert "jazz" in memory.read()["content"]
        finally:
            alarms.close()
            await asyncio.sleep(0)

    asyncio.run(scenario())


def test_memory_topic_retrieval_and_correction_through_mcp_after_restart(tmp_path):
    async def noop(value):
        pass

    async def scenario():
        settings = Settings(tmp_path, tmp_path / "memory")
        memory = MemoryRepo(settings.memory_dir)
        alarms = AlarmService(tmp_path / "state.sqlite3", settings.timezone, noop, noop)
        try:
            async with Client(create_tools(alarms, memory)) as client:
                topic = (await client.call_tool("read_memory", {"path": "music.md"})).data
                saved = await client.call_tool(
                    "update_memory",
                    {
                        "path": "music.md",
                        "content": "# Music\n"
                        "- Coding music: instrumental jazz [source: first session]\n"
                        "- Avoid vocals [source: first session]\n",
                        "revision": topic["revision"],
                    },
                )
                assert saved.data["commit"]
                index = (await client.call_tool("read_memory", {})).data
                await client.call_tool(
                    "update_memory",
                    {
                        "path": "MEMORY.md",
                        "content": "# Memory\n- Prefers concise answers\n\n## Index\n- [[music]]\n",
                        "revision": index["revision"],
                    },
                )

            restored = MemoryRepo(settings.memory_dir)
            # A fresh session sees the entry point, not the full topic contents.
            startup = instructions(settings, memory_source="second-session.jsonl")
            assert "Prefers concise answers" in startup and "[[music]]" in startup
            assert "Coding music: instrumental jazz" not in startup
            assert "second-session.jsonl" in startup
            async with Client(create_tools(alarms, restored)) as client:
                found = await client.call_tool("search_memory", {"query": "Coding music"})
                assert "music.md" in found.data["matches"][0]["match"]
                empty = await client.call_tool("search_memory", {"query": "unmentioned-term"})
                assert empty.data == {"matches": []}
                assert empty.content and '"matches"' in empty.content[0].text
                topic = (await client.call_tool("read_memory", {"path": "music.md"})).data
                assert "instrumental jazz" in topic["content"]
                await client.call_tool(
                    "update_memory",
                    {
                        "path": "music.md",
                        "content": topic["content"].replace(
                            "instrumental jazz [source: first session]",
                            "ambient electronic [source: second session]",
                        ),
                        "revision": topic["revision"],
                    },
                )
            final = MemoryRepo(settings.memory_dir)
            assert "ambient electronic" in final.read("music.md")["content"]
            assert "jazz" not in final.read("music.md")["content"]
            assert "Avoid vocals" in final.read("music.md")["content"]
            assert "Prefers concise answers" in final.read()["content"]
            assert not final._git("status", "--porcelain").strip()
        finally:
            alarms.close()

    asyncio.run(scenario())
