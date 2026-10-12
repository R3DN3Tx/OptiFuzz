from __future__ import annotations

import os
import shutil
from pathlib import Path


def long_path(path: Path) -> str:
    """Windows' extended-length path prefix, so a raw OS file operation
    (open, os.replace, os.remove, shutil.copy2, ...) against `path` can
    exceed the classic MAX_PATH (260 characters) -- works unconditionally,
    regardless of the system-wide "enable long paths" registry policy (off
    by default, requires admin; confirmed off on this project's own dev
    machine). A no-op on any non-Windows OS, and on a path that's already
    prefixed.

    Real, reported bug this exists for: a genuinely long fuzzed path
    (docs/DEVELOPMENT_PLAN.md's 2026-09-22 entries) needed its saved output
    filename to show the whole path, which can push the on-disk path well
    past MAX_PATH. Renaming to that long name (job_manager/manager.py)
    turned out to only be half the fix -- reproduced directly: a PLAIN
    Path.is_file()/open() on a >260-char path came back "doesn't exist"
    even for a file that had just been renamed there successfully, because
    those calls never see the extended-length prefix on their own. Every
    read/write/exists/delete this app's own code does against a path that
    might be this long has to go through here (or one of the small
    wrappers below) instead of calling pathlib/shutil directly.
    """
    if os.name != "nt":
        return str(path)
    resolved = str(path.resolve())
    if resolved.startswith("\\\\?\\") or resolved.startswith("\\\\.\\"):
        return resolved
    if resolved.startswith("\\\\"):  # UNC: \\server\share\... -> \\?\UNC\server\share\...
        return "\\\\?\\UNC\\" + resolved[2:]
    return "\\\\?\\" + resolved


def path_is_file(path: Path) -> bool:
    """Like Path.is_file(), but tolerant of a path that exceeds MAX_PATH --
    see long_path()'s own docstring for why a plain Path.is_file() can't be
    trusted for that here."""
    try:
        return os.path.isfile(long_path(path))
    except OSError:
        return False


def read_text(path: Path, *, encoding: str = "utf-8") -> str:
    with open(long_path(path), "r", encoding=encoding) as file_handle:
        return file_handle.read()


def write_text(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    with open(long_path(path), "w", encoding=encoding) as file_handle:
        file_handle.write(text)


def unlink(path: Path, *, missing_ok: bool = False) -> None:
    """Matches Path.unlink(missing_ok=...)'s own semantics -- only a
    missing file is ever suppressed, any other OSError still propagates."""
    try:
        os.remove(long_path(path))
    except FileNotFoundError:
        if not missing_ok:
            raise


def copy_file(src: Path, dst: Path) -> None:
    shutil.copy2(long_path(src), long_path(dst))


def move_file(src: Path, dst: Path) -> None:
    shutil.move(long_path(src), long_path(dst))


def app_data_dir() -> Path:
    """Per-user directory for everything OptiFuzz persists (the workspace,
    the desktop window's browser-engine profile, ...).

    Windows: %APPDATA%\\OptiFuzz. Linux: $XDG_DATA_HOME/OptiFuzz, i.e.
    ~/.local/share/OptiFuzz by default. If the platform's usual variable is
    missing, falls back to ~/.optifuzz.
    """
    if os.name == "nt":
        appdata = os.environ.get("APPDATA")
        if appdata:
            return Path(appdata) / "OptiFuzz"
    else:
        data_home = os.environ.get("XDG_DATA_HOME")
        base = Path(data_home) if data_home else Path.home() / ".local" / "share"
        return base / "OptiFuzz"
    return Path.home() / ".optifuzz"


def workspace_root() -> Path:
    """Root directory holding all Projects/Targets/Jobs data.

    Override with OPTIFUZZ_WORKSPACE_ROOT (used by tests, and by any user who
    wants their workspace somewhere other than the default). Default is
    `app_data_dir()/workspace` (docs/ARCHITECTURE.md §10 item 3).
    """
    override = os.environ.get("OPTIFUZZ_WORKSPACE_ROOT")
    if override:
        return Path(override)

    return app_data_dir() / "workspace"
