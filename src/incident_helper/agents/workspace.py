"""Disk-based shared workspace: workers exchange Markdown files, not message history."""

from __future__ import annotations

import re
from pathlib import Path

from langchain_core.tools import BaseTool, tool

_SAFE_NAME = re.compile(r"^[A-Za-z0-9_\-]+\.md$")


class Workspace:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, name: str) -> Path:
        """Only flat `name.md` paths: an agent cannot read or write outside its run folder."""
        if not _SAFE_NAME.fullmatch(name):
            raise ValueError(f"Invalid draft name {name!r}: use a plain name like timeline.md")
        return self.root / name

    def write(self, name: str, content: str) -> int:
        self.path(name).write_text(content, encoding="utf-8")
        return len(content)

    def read(self, name: str) -> str:
        path = self.path(name)
        if not path.exists():
            available = ", ".join(sorted(p.name for p in self.root.glob("*.md"))) or "none"
            raise FileNotFoundError(f"Draft {name} does not exist. Available: {available}")
        return path.read_text(encoding="utf-8")

    def exists(self, name: str) -> bool:
        path = self.path(name)
        return path.exists() and path.stat().st_size > 0


def workspace_tools(workspace: Workspace) -> list[BaseTool]:
    @tool
    def write_draft(filename: str, content: str) -> str:
        """Save your findings as a Markdown file in the shared workspace.

        Put full detail in the file. Your chat reply should only confirm that it was saved.

        Args:
            filename: Plain file name ending in .md, for example timeline.md.
            content: The complete Markdown findings.
        """
        return f"Saved {filename} ({workspace.write(filename, content)} characters)."

    @tool
    def read_draft(filename: str) -> str:
        """Read a Markdown file written earlier by another investigator.

        Args:
            filename: Plain file name ending in .md, for example timeline.md.
        """
        return workspace.read(filename)

    return [write_draft, read_draft]
