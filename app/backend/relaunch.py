"""Starts OptiFuzz again once the running copy has exited (the restart after Settings -> Updates).

Run as a standalone script by backend/services/update_service.py, in its own detached process:

    python backend/relaunch.py <pid of the old app> <working directory> <command> [args...]

It waits for the old process to disappear (so the new one gets the same port and WebView profile
back instead of racing for them), then starts the command. If the old process is still alive after
WAIT_SECONDS it does nothing: starting a second copy next to a hung one would be worse than none.

Standard library only, and no import of the backend: it must keep working while the code on disk is
being replaced.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time

WAIT_SECONDS = 60
SETTLE_SECONDS = 0.5  # lets the OS release the port and the window's profile folder


def _pid_alive(pid: int) -> bool:
    if sys.platform == "win32":
        import ctypes

        synchronize = 0x00100000
        handle = ctypes.windll.kernel32.OpenProcess(synchronize, False, pid)
        if not handle:
            return False  # cannot open it: it no longer exists
        try:
            return ctypes.windll.kernel32.WaitForSingleObject(handle, 0) == 0x102  # WAIT_TIMEOUT
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)  # signal 0 only checks existence on POSIX
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # A process that already exited but whose parent has not collected it yet (a zombie) still
    # answers signal 0; on Linux its state is "Z" in /proc, and it is as good as gone.
    try:
        with open(f"/proc/{pid}/stat", "rb") as stat:
            return stat.read().rsplit(b")", 1)[1].split()[0] != b"Z"
    except (OSError, IndexError):
        return True


def main(argv: list[str]) -> int:
    pid, cwd, command = int(argv[0]), argv[1], argv[2:]
    deadline = time.monotonic() + WAIT_SECONDS
    while _pid_alive(pid):
        if time.monotonic() > deadline:
            return 1
        time.sleep(0.2)
    time.sleep(SETTLE_SECONDS)

    options: dict = {"cwd": cwd, "close_fds": True}
    if sys.platform == "win32":
        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP: not tied to this helper's (or any) console.
        options["creationflags"] = 0x00000008 | 0x00000200
    else:
        options["start_new_session"] = True
    subprocess.Popen(command, **options)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
