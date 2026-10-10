"""Bounded, authoritative personal context shared by voice and text startup."""

import re
from collections.abc import Awaitable, Callable


async def load_memory_context(read: Callable[[str], Awaitable[str]]) -> dict[str, str]:
    budget = 24000
    index = (await read("MEMORY.md"))[:budget]
    notes = {"MEMORY.md": index}
    budget -= len(index)
    for link in dict.fromkeys(re.findall(r"\[\[([^\]\n]+)\]\]", index)):
        path = link.split("|", 1)[0].split("#", 1)[0].strip()
        if not path:
            continue
        if not path.endswith(".md"):
            path += ".md"
        if path in notes:
            continue
        if len(notes) >= 9 or budget <= 0:
            break
        content = await read(path)
        notes[path] = content[:budget]
        budget -= len(notes[path])
    return notes
