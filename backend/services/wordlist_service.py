from __future__ import annotations

from pathlib import Path
from typing import Any

# The app's own bundled wordlists, browsable from the New Fuzz form's
# wordlist picker -- separate from any wordlist a user points at elsewhere
# on disk (spec §80: no hardcoded paths, this is just a convenience default).
WORDLISTS_DIR = Path(__file__).resolve().parent.parent.parent / "wordlists"


def list_wordlists() -> dict[str, Any]:
    return _build_tree(WORDLISTS_DIR)


def _build_tree(directory: Path) -> dict[str, Any]:
    children: list[dict[str, Any]] = []
    if directory.is_dir():
        entries = sorted(directory.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
        for entry in entries:
            if entry.name.startswith("."):
                continue
            if entry.is_dir():
                children.append(
                    {"type": "dir", "name": entry.name, "children": _build_tree(entry)["children"]}
                )
            elif entry.is_file():
                children.append(
                    {"type": "file", "name": entry.name, "path": str(entry.resolve())}
                )
    return {"type": "dir", "name": directory.name, "children": children}
