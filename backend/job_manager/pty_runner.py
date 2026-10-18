from __future__ import annotations

import os
import threading

import psutil
import winpty

from backend.job_manager import focus_guard

# (rows, cols) -- wide enough that dirsearch's progress bar text (~90 chars)
# never exceeds the terminal width dirsearch queries, which is what makes it
# silently stop drawing the bar at all.
DIMENSIONS = (50, 250)

# pywinpty's ConPTY backend spawns its own console-host process(es)
# (OpenConsole.exe, plus a nested conhost.exe underneath it) as *siblings* of
# the dirsearch child -- direct children of this app's own process -- and
# never terminates them itself: PtyProcess.close()/terminate()/__del__ (see
# .venv/Lib/site-packages/winpty/ptyprocess.py) only ever signal the *child*
# PID, never call ClosePseudoConsole or otherwise touch the pty host. Left
# alone, one of each leaks per chunk -- confirmed directly on a real session:
# ~450 orphaned conhost.exe accumulated as
# children of the app process after ~1.5h of fuzzing. A genuine resource
# leak in its own right (fixed below, _reap_leaked_pty_hosts), but NOT what
# actually causes the reported focus-stealing bug -- see focus_guard.py:
# each newly spawned console window briefly grabs real OS foreground focus
# at CREATION time regardless of whether old ones are cleaned up afterward
# (confirmed via a system-wide window-event capture on a real running
# instance: a `ConsoleWindowClass` window titled "OptiFuzz.DesktopApp"
# becomes visible and foreground on every single chunk spawn).
_LEAKED_PTY_HOST_NAMES = {"conhost.exe", "openconsole.exe"}

# Guards the "snapshot our own children -> spawn -> snapshot again" sequence
# below so two chunks' PtyChunkProcess.__init__ calls (different jobs, run
# concurrently by JobManager) can never race and misattribute each other's
# host processes -- only held briefly around process creation, not the
# chunk's whole lifetime, so concurrent jobs still run their chunks in
# parallel as before.
_spawn_lock = threading.Lock()


def _own_child_pids() -> set[int]:
    try:
        return {c.pid for c in psutil.Process(os.getpid()).children(recursive=False)}
    except psutil.Error:
        return set()


class PtyChunkProcess:
    """Thin wrapper around winpty.PtyProcess exposing only what JobManager
    needs, so the rest of the code doesn't depend on the winpty API shape
    directly (Windows-only)."""

    def __init__(self, argv: list[str]) -> None:
        focus_guard.ensure_started()
        with _spawn_lock:
            before = _own_child_pids()
            self._proc = winpty.PtyProcess.spawn(argv, dimensions=DIMENSIONS)
            after = _own_child_pids()
        # Includes the dirsearch child itself plus its ConPTY host
        # process(es) -- wait()/terminate() below only ever kill whichever
        # of these are still alive once the dirsearch child is confirmed
        # done, so this can never touch a concurrently-running chunk's
        # still-legitimate host.
        self._new_pids_from_spawn = after - before

    @property
    def pid(self) -> int:
        return self._proc.pid

    def is_alive(self) -> bool:
        return self._proc.isalive()

    def read(self, size: int = 4096) -> str | None:
        """Blocks until data is available. Returns None at EOF."""
        try:
            data = self._proc.read(size)
        except EOFError:
            return None
        return data or None

    def wait(self) -> int:
        self._proc.wait()
        status = self._proc.exitstatus
        self._reap_leaked_pty_hosts()
        return status if status is not None else 0

    def terminate(self, force: bool = False) -> None:
        if self._proc.isalive():
            self._proc.terminate(force=force)
        self._reap_leaked_pty_hosts()

    def _reap_leaked_pty_hosts(self) -> None:
        if self._proc.isalive():
            # terminate(force=False) can return with the child still alive
            # (e.g. it ignored SIGINT) -- never sever its pty host out from
            # under it while it's still using it.
            return
        for pid in self._new_pids_from_spawn:
            if pid == self._proc.pid:
                continue  # the dirsearch child itself -- already handled above
            try:
                proc = psutil.Process(pid)
                if proc.name().lower() in _LEAKED_PTY_HOST_NAMES:
                    proc.kill()
            except psutil.Error:
                pass  # already gone, or gone by the time we got here -- fine
