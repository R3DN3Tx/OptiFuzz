"""The few OS-specific behaviors the backend needs, in one place.

Everything else in the backend is platform-neutral; code that would otherwise
branch on `os.name` calls into here instead.
"""
from __future__ import annotations

import os
import subprocess
from typing import Any

IS_WINDOWS = os.name == "nt"


def no_window_kwargs() -> dict[str, Any]:
    """Popen/run kwargs that keep a console-subsystem child from getting its
    own visible console window. Windows only: a console-subsystem python.exe
    spawned from a console-less pythonw.exe parent otherwise gets one
    auto-allocated (see JobManager._run_segment). POSIX has no such concept."""
    if IS_WINDOWS:
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {}


def terminate_process(proc: subprocess.Popen) -> None:
    """The primitive behind Pause/Stop: end the child immediately.

    Windows: `.terminate()` is already `TerminateProcess()`, a hard kill.
    POSIX: `.terminate()` is SIGTERM, which dirsearch traps to open its own
    interactive pause menu instead of exiting (verified on Linux: still alive
    8s later), so a plain terminate() would make every Pause/Stop wait out the
    grace period. SIGKILL gives the same immediate end Windows does -- the
    app never relies on dirsearch shutting down gracefully (progress is
    checkpointed from its live output, not from a clean exit)."""
    if IS_WINDOWS:
        proc.terminate()
    else:
        proc.kill()
