from __future__ import annotations

import os
import subprocess
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from backend.platform_compat import no_window_kwargs

# In-app update (Settings -> Updates): `git pull --ff-only` on the checkout the app runs from, then
# `pip install -r app/requirements.txt` so a changed dependency (e.g. the pinned dirsearch commit) comes
# along. The user's data never lives in the checkout (workspace is under %APPDATA% / ~/.local/share),
# so projects, targets and jobs are untouched. Nothing is contacted until the user clicks.
REPO_ROOT = Path(__file__).resolve().parents[3]  # <repo>/app/backend/services/update_service.py

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
        return (REPO_ROOT / "app" / "VERSION").read_text(encoding="utf-8").strip()
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
            latest = _git("show", "@{u}:app/VERSION").strip()
        except UpdateError:
            latest = "unknown"
    return {
        "current": current_version(),
        "latest": latest,
        "behind": behind,
        "ahead": ahead,
        "dirty": dirty,
        "can_update": behind > 0 and ahead == 0 and not dirty,
        "can_restart": can_restart(),
    }


# Automatic restart after an update. Only the desktop window can offer it: run_desktop.py registers
# how to close its own window and how to start itself again (a bare uvicorn run has
# neither, so there the user restarts by hand, as before).
_restart_close_window: Callable[[], None] | None = None
_restart_command: list[str] | None = None
_updated_since_start = False  # set by apply(); restart() refuses to run without a fresh update

_RELAUNCH_SCRIPT = Path(__file__).resolve().parent.parent / "relaunch.py"
_CLOSE_DELAY_SECONDS = 1.0  # lets the HTTP response that triggered the restart reach the page first


def register_desktop_restart(close_window: Callable[[], None], command: list[str]) -> None:
    global _restart_close_window, _restart_command
    _restart_close_window = close_window
    _restart_command = command


def can_restart() -> bool:
    return _restart_close_window is not None and _restart_command is not None


def restart() -> None:
    """Starts a detached helper that relaunches OptiFuzz once this process is gone, then closes
    the window (which ends this process) a moment later."""
    if not can_restart():
        raise UpdateError("Automatic restart is only available in the OptiFuzz desktop app.")
    if not _updated_since_start:
        raise UpdateError("There is no update waiting for a restart.")
    options: dict[str, Any] = {"close_fds": True}
    if sys.platform == "win32":
        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP: no console, and it outlives this process.
        options["creationflags"] = 0x00000008 | 0x00000200
    else:
        options["start_new_session"] = True
    try:
        subprocess.Popen(
            [_pip_python(), str(_RELAUNCH_SCRIPT), str(os.getpid()), str(REPO_ROOT), *_restart_command],
            cwd=str(REPO_ROOT), **options,
        )
    except OSError as exc:
        raise UpdateError(f"Could not start the restart helper: {exc}") from exc
    close = _restart_close_window
    timer = threading.Timer(_CLOSE_DELAY_SECONDS, close)
    timer.daemon = True
    timer.start()


def _pip_python() -> str:
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe":  # pip wants a console-subsystem interpreter
        candidate = exe.with_name("python.exe")
        if candidate.exists():
            return str(candidate)
    return str(exe)


def _pip_install_flags() -> list[str]:
    """Extra `pip install` flags. On Linux the venv is created with --system-site-packages (the window
    toolkit, PyGObject, comes from the distribution), so without --ignore-installed pip reuses a system
    copy of a dependency (Kali's old httpcore, which crashes on Python 3.14) instead of installing it
    in the venv; --no-warn-conflicts hides its complaints about unrelated system tools. Same flags as
    app/scripts/linux/install-linux.sh. Windows has no system packages in its venv."""
    return [] if sys.platform == "win32" else ["--ignore-installed", "--no-warn-conflicts"]


def restore_program_files(damaged_file: str | None) -> None:
    """Puts the one damaged program file back as it is in the installed version. Only that file, and only
    when it is a file git tracks inside this copy: nothing else (other edits, the data folder) is touched."""
    _require_checkout()
    if not damaged_file:
        return
    try:
        relative = Path(damaged_file).resolve().relative_to(REPO_ROOT.resolve())
    except (ValueError, OSError):
        return
    try:
        _git("checkout", "--", relative.as_posix())
    except UpdateError:
        pass  # not tracked, or already fine: the update below decides what else is needed


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
        [_pip_python(), "-m", "pip", "install", *_pip_install_flags(), "-r", str(REPO_ROOT / "app" / "requirements.txt")],
        capture_output=True, text=True, timeout=900, env=_ENV, cwd=str(REPO_ROOT), **no_window_kwargs(),
    )
    if pip.returncode != 0:
        tail = (pip.stderr or pip.stdout).strip().splitlines()[-5:]
        raise UpdateError("Code updated, but installing the dependencies failed: " + " | ".join(tail))
    global _updated_since_start
    _updated_since_start = True
    return {
        **status, "current": current_version(), "updated": True, "restart_required": True,
        "behind": 0, "can_update": False,
    }
