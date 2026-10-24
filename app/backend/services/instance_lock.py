from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import IO

from backend.persistence.paths import workspace_root

# One OptiFuzz per data folder. Two copies sharing one workspace each load every job: the second one marks
# the first one's running jobs as paused on disk and can resume them, so the same job would be scanned twice.
# The lock is an operating-system file lock held for as long as the process lives (the OS releases it even
# if the app is killed, so a crash never leaves a stale lock behind).

_PID_OFFSET = 16  # the locked byte is offset 0; the pid is written after it so other processes can still read it
_held: IO[bytes] | None = None


def _lock_file() -> Path:
    return workspace_root() / ".optifuzz.lock"


def acquire() -> bool:
    """True when this process now owns the data folder; False when another OptiFuzz already does."""
    global _held
    path = _lock_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+b")
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return False
    handle.seek(_PID_OFFSET)
    handle.truncate(_PID_OFFSET)
    handle.write(str(os.getpid()).encode("ascii"))
    handle.flush()
    _held = handle
    return True


def release() -> None:
    """Gives the data folder back (only needed by tests; the OS does it when the process ends)."""
    global _held
    if _held is None:
        return
    try:
        if os.name == "nt":
            import msvcrt

            _held.seek(0)
            msvcrt.locking(_held.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(_held.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass
    _held.close()
    _held = None


def owner_pid() -> int | None:
    """The pid written by the process that holds the lock."""
    try:
        with open(_lock_file(), "rb") as handle:
            handle.seek(_PID_OFFSET)
            return int(handle.read().decode("ascii").strip())
    except (OSError, ValueError):
        return None


def bring_owner_to_front() -> bool:
    """Best effort: shows the window of the OptiFuzz that already runs. True when a window was found."""
    pid = owner_pid()
    if pid is None:
        return False
    if os.name == "nt":
        return _front_windows(pid)
    return _front_linux()


def _front_windows(pid: int) -> bool:
    import ctypes
    from ctypes import wintypes

    try:
        import psutil

        pids = {pid} | {child.pid for child in psutil.Process(pid).children(recursive=True)}
    except Exception:
        pids = {pid}
    user32 = ctypes.windll.user32
    handles: list[int] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def collect(hwnd, _lparam):
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value in pids and user32.IsWindowVisible(hwnd):
            title = ctypes.create_unicode_buffer(64)
            user32.GetWindowTextW(hwnd, title, 64)
            if title.value == "OptiFuzz":
                handles.append(hwnd)
        return True

    user32.EnumWindows(collect, 0)
    for hwnd in handles:
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE: undo a minimized state, keep maximized as it is
        user32.SetForegroundWindow(hwnd)
    return bool(handles)


def _front_linux() -> bool:
    for command in (["wmctrl", "-a", "OptiFuzz"], ["xdotool", "search", "--name", "^OptiFuzz$", "windowactivate"]):
        try:
            if subprocess.run(command, capture_output=True, timeout=5).returncode == 0:
                return True
        except (OSError, subprocess.SubprocessError):
            continue
    return False

