from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from backend.platform_compat import no_window_kwargs

# In-app update (Settings -> Updates): `git pull --ff-only` on the checkout the app runs from, then
# `pip install -r requirements.txt` so a changed dependency (e.g. the pinned dirsearch commit) comes
# along. The user's data never lives in the checkout (workspace is under %APPDATA% / ~/.local/share),
# so projects, targets and jobs are untouched. Nothing is contacted until the user clicks.
REPO_ROOT = Path(__file__).resolve().parent.parent.parent

_ENV = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}  # never hang waiting for credentials


class UpdateError(Exception):
    pass


def _git(*args: str, timeout: int = 60) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(REPO_ROOT), *args],
            capture_output=True, text=True, timeout=timeout, env=_ENV, **no_window_kwargs(),
        )
    except FileNotFoundError as exc:
        raise UpdateError("Git is not installed or not on PATH.") from exc
    except subprocess.TimeoutExpired as exc:
        raise UpdateError("Git did not answer in time (is there a network connection?).") from exc
    if result.returncode != 0:
        raise UpdateError((result.stderr or result.stdout).strip() or f"git {args[0]} failed")
    return result.stdout.strip()


def _require_checkout() -> None:
    if not (REPO_ROOT / ".git").exists():
        raise UpdateError(
            "This copy of OptiFuzz was not installed with git (no .git folder), so it cannot update "
            "itself. Clone the repository again to get updates."
        )


def current_version() -> str:
    try:
        return (REPO_ROOT / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        return "0.0.0"


def check() -> dict[str, Any]:
    _require_checkout()
    _git("fetch", "--quiet")
    try:
        _git("rev-parse", "--abbrev-ref", "@{u}")
    except UpdateError as exc:
        raise UpdateError(
            "The current branch does not track a remote branch, so there is nothing to compare with."
        ) from exc
    behind = int(_git("rev-list", "--count", "HEAD..@{u}") or 0)
    ahead = int(_git("rev-list", "--count", "@{u}..HEAD") or 0)
    dirty = bool(_git("status", "--porcelain", "--untracked-files=no"))
    latest = current_version()
    if behind:
        try:
            latest = _git("show", "@{u}:VERSION").strip()
        except UpdateError:
            latest = "unknown"
    return {
        "current": current_version(),
        "latest": latest,
        "behind": behind,
        "ahead": ahead,
        "dirty": dirty,
        "can_update": behind > 0 and ahead == 0 and not dirty,
    }


def _pip_python() -> str:
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe":  # pip wants a console-subsystem interpreter
        candidate = exe.with_name("python.exe")
        if candidate.exists():
            return str(candidate)
    return str(exe)


def apply() -> dict[str, Any]:
    status = check()
    if status["dirty"]:
        raise UpdateError("Some tracked files were modified locally; update aborted so nothing is overwritten.")
    if status["ahead"]:
        raise UpdateError("This copy has local commits that are not on the remote; update it manually with git.")
    if not status["behind"]:
        return {**status, "updated": False, "restart_required": False}
    _git("pull", "--ff-only", "--quiet", timeout=300)
    pip = subprocess.run(
        [_pip_python(), "-m", "pip", "install", "-r", str(REPO_ROOT / "requirements.txt")],
        capture_output=True, text=True, timeout=900, env=_ENV, cwd=str(REPO_ROOT), **no_window_kwargs(),
    )
    if pip.returncode != 0:
        tail = (pip.stderr or pip.stdout).strip().splitlines()[-5:]
        raise UpdateError("Code updated, but installing the dependencies failed: " + " | ".join(tail))
    return {
        **status, "current": current_version(), "updated": True, "restart_required": True,
        "behind": 0, "can_update": False,
    }
