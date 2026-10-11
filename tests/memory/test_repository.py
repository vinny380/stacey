import asyncio

import pytest

from stacy.memory.repository import MemoryRepo


def test_memory_survives_restart_and_rejects_stale_updates(tmp_path):
    async def scenario():
        memory = MemoryRepo(tmp_path / "memory")
        old = memory.read()
        await memory.write("MEMORY.md", "# Memory\n\n- Prefers jazz\n\n## Index\n", old["revision"])
        restored = MemoryRepo(tmp_path / "memory")
        assert "jazz" in restored.read()["content"]
        assert restored.search("jazz")
        with pytest.raises(ValueError, match="changed"):
            await memory.write("MEMORY.md", "stale replacement", old["revision"])

    asyncio.run(scenario())


def test_memory_preserves_uncommitted_user_edits(tmp_path):
    async def scenario():
        memory = MemoryRepo(tmp_path / "memory")
        note = memory.read()
        (memory.root / "personal.md").write_text("User's unfinished note")
        with pytest.raises(ValueError, match="uncommitted"):
            await memory.write("MEMORY.md", "replacement", note["revision"])
        assert memory.read()["content"] == note["content"]

    asyncio.run(scenario())


@pytest.mark.parametrize("path", ["../outside.md", ".git/config.md", "/tmp/outside.md"])
def test_memory_rejects_paths_outside_notes(tmp_path, path):
    memory = MemoryRepo(tmp_path / "memory")
    with pytest.raises(ValueError):
        memory.read(path)
