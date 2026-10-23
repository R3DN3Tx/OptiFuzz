from __future__ import annotations

from pathlib import Path
from typing import Any

from backend.persistence import app_settings

# The app's own bundled wordlists, browsable from the New Fuzz form's
# wordlist picker -- the default when no other folder is configured in
# Settings.
WORDLISTS_DIR = Path(__file__).resolve().parents[3] / "wordlists"  # at the repository root, outside app/


def effective_wordlists_dir() -> Path:
    """The folder the picker browses: the one set in Settings, else the bundled one."""
    configured = app_settings.get_wordlists_dir()
    return Path(configured) if configured else WORDLISTS_DIR


def set_wordlists_dir(path: str | None) -> None:
    """Stores the picker folder; empty/None goes back to the bundled one.
    Raises ValueError when the path is not an existing folder."""
    path = (path or "").strip()
    if path and not Path(path).is_dir():
        raise ValueError(f"Not an existing folder: {path}")
    app_settings.set_wordlists_dir(path or None)


def list_wordlists() -> dict[str, Any]:
    return _build_tree(effective_wordlists_dir(), set())


def _build_tree(directory: Path, visited: set[Path]) -> dict[str, Any]:
    # The folder can now be one the user already had (symlinks, unreadable
    # subfolders): remember the real folders on the current branch so a link
    # loop ends instead of recursing forever, and skip what cannot be read.
    children: list[dict[str, Any]] = []
    try:
        real = directory.resolve()
        if directory.is_dir() and real not in visited:
            entries = sorted(directory.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
            for entry in entries:
                if entry.name.startswith("."):
                    continue
                if entry.is_dir():
                    children.append(
                        {"type": "dir", "name": entry.name,
                         "children": _build_tree(entry, visited | {real})["children"]}
                    )
                elif entry.is_file():
                    children.append(
                        {"type": "file", "name": entry.name, "path": str(entry.resolve())}
                    )
    except OSError:
        pass
    return {"type": "dir", "name": directory.name, "children": children}
