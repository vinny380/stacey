from __future__ import annotations

import asyncio
import fcntl
import hashlib
import shutil
import subprocess
from pathlib import Path

from stacy.runtime.storage import atomic_write


class MemoryRepo:
    """File and Git adapter for the upstream Agent Memory Repo format."""

    def __init__(self, root: Path):
        self.root = root.resolve()
        project = Path(__file__).resolve().parents[2]
        if (project / "pyproject.toml").exists() and self.root.is_relative_to(project):
            raise ValueError("Personal memory must live outside Stacy's source checkout")
        if not shutil.which("git"):
            raise RuntimeError("Agent Memory Repo requires Git")
        if not self.root.exists() or not any(self.root.iterdir()):
            self.root.mkdir(parents=True, exist_ok=True)
            self._git("init", "--quiet")
            (self.root / "MEMORY.md").write_text("# Memory\n\n## Index\n")
            self._git("add", "--", "MEMORY.md")
            self._git("commit", "--quiet", "-m", "Initialize personal memory")
        elif not (self.root / "MEMORY.md").is_file() or not (self.root / ".git").is_dir():
            raise ValueError("Memory path must be empty or an existing separate memory Git repo")
        if Path(self._git("rev-parse", "--show-toplevel").strip()).resolve() != self.root:
            raise ValueError("Memory must be a separate Git repo")

    def _git(self, *args: str) -> str:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(self.root),
                "-c",
                "user.name=Stacy",
                "-c",
                "user.email=stacy@localhost",
                *args,
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout

    def _path(self, relative: str) -> Path:
        path = (self.root / relative).resolve()
        if not path.is_relative_to(self.root) or path.suffix != ".md":
            raise ValueError("Memory tools access Markdown files inside the memory repo only")
        if ".git" in path.relative_to(self.root).parts:
            raise ValueError("Git internals are not memory notes")
        return path

    def read(self, path: str = "MEMORY.md") -> dict[str, str]:
        note = self._path(path)
        content = note.read_text() if note.exists() else ""
        return {
            "path": path,
            "content": content,
            "revision": hashlib.sha256(content.encode()).hexdigest(),
        }

    def search(self, query: str) -> list[dict]:
        if not query.strip():
            raise ValueError("Provide a search term")
        # Git searches tracked notes without traversing private Git internals.
        result = subprocess.run(
            ["git", "-C", str(self.root), "grep", "-n", "-i", "-F", "-e", query, "--", "*.md"],
            capture_output=True,
            text=True,
        )
        if result.returncode not in (0, 1):
            raise RuntimeError("Memory search failed")
        return [{"match": line} for line in result.stdout.splitlines()[:30]]

    async def write(self, path: str, content: str, revision: str) -> dict:
        if len(content.encode()) > 100_000:
            raise ValueError("Keep memory notes under 100 KB")
        return await asyncio.to_thread(self._write, path, content, revision)

    def _write(self, path: str, content: str, revision: str) -> dict:
        note = self._path(path)
        # A file lock also serializes updates from another local Stacy process.
        with (self.root / ".git/stacy-write.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if self._git("status", "--porcelain").strip():
                raise ValueError("Memory has uncommitted changes; resolve them before updating it")
            old = self.read(path)
            if old["revision"] != revision:
                raise ValueError("Memory changed since it was read; read it again before writing")
            if old["content"] == content:
                return old
            atomic_write(note, content)
            self._git("add", "--", str(note.relative_to(self.root)))
            self._git("commit", "--quiet", "-m", f"Update memory: {note.relative_to(self.root)}")
            return {**self.read(path), "commit": self._git("rev-parse", "HEAD").strip()}
