from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import threading

# Reported bug: while a fuzz job is active, OS keyboard focus briefly jumps
# away from an unrelated app (losing keystrokes, and even triggering an
# unintended Ctrl+C-style clipboard copy), sometimes with a visible brief
# white window flash. Root cause, confirmed by a system-wide window-event
# capture (docs/ARCHITECTURE.md) taken while a real job ran on a real
# instance: every dirsearch chunk's ConPTY session creates a real, sized,
# eventually-visible `ConsoleWindowClass` window titled "OptiFuzz.DesktopApp"
# (conhost.exe renders it, but GetWindowThreadProcessId attributes it to the
# app's own pythonw.exe PID -- a known console-hosting quirk, not a bug in
# this reasoning) -- and it briefly takes real OS foreground focus every
# single time, confirmed on repeat across chunk after chunk. An earlier
# version of this fix tracked specific spawn PIDs (the dirsearch child, its
# ConPTY host) and failed silently: PID attribution for this exact window is
# the *app's own* PID, not any tracked child's, which a PID-based guard can
# never distinguish from the app's own real WebView2 window ever holding
# focus. This version keys off the window itself instead of who technically
# created it: class `ConsoleWindowClass` + title "OptiFuzz.DesktopApp" is a
# signature OptiFuzz's own real window never has (it's WebView2-hosted, an
# entirely different window class) and an ordinary user-opened console never
# has either (a real cmd.exe/PowerShell window is never titled that) --
# specific enough to restore focus away from it unconditionally, without
# ever fighting the user's own legitimate console usage.
#
# LockSetForegroundWindow(LSFW_LOCK), the standard API for blocking exactly
# this kind of steal, was tried first and made no difference -- console
# windows get a special OS-level exemption from the restriction it guards.
# This reacts instead of preventing: a low-level EVENT_SYSTEM_FOREGROUND
# hook watches for the OS foreground window matching that signature, and
# immediately hands focus back to whatever window legitimately had it.
#
# Deliberately fail-open everywhere: any error here (a stale/closed window
# handle, a Win32 call failing) is swallowed and skipped rather than risking
# ever getting stuck unable to restore -- worst case on a bug here is a
# missed steal (the original complaint, unimproved), never a *new* problem
# (the user unable to Alt-Tab, or the hook thread wedging).

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
user32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
user32.IsWindow.argtypes = [wt.HWND]
user32.GetForegroundWindow.restype = wt.HWND
user32.SetForegroundWindow.argtypes = [wt.HWND]

EVENT_SYSTEM_FOREGROUND = 0x0003
EVENT_OBJECT_SHOW = 0x8002
WINEVENT_OUTOFCONTEXT = 0x0000
SW_HIDE = 0

_GUARDED_CLASS = "ConsoleWindowClass"
_GUARDED_TITLE = "OptiFuzz.DesktopApp"

_WinEventProcType = ctypes.WINFUNCTYPE(
    None, wt.HANDLE, wt.DWORD, wt.HWND, wt.LONG, wt.LONG, wt.DWORD, wt.DWORD
)

_lock = threading.Lock()
_last_good_hwnd: int | None = None
_started = False


def _window_class(hwnd: int) -> str:
    try:
        buf = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, buf, 256)
        return buf.value
    except OSError:
        return ""


def _window_title(hwnd: int) -> str:
    try:
        buf = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(hwnd, buf, 256)
        return buf.value
    except OSError:
        return ""


def _is_phantom_console(hwnd: int) -> bool:
    return _window_class(hwnd) == _GUARDED_CLASS and _window_title(hwnd) == _GUARDED_TITLE


def _restore_foreground(target_hwnd: int | None) -> None:
    if not target_hwnd:
        return
    try:
        if not user32.IsWindow(target_hwnd):
            return
        current_hwnd = user32.GetForegroundWindow()
        this_thread = kernel32.GetCurrentThreadId()
        current_thread = user32.GetWindowThreadProcessId(current_hwnd, None) if current_hwnd else 0
        target_thread = user32.GetWindowThreadProcessId(target_hwnd, None)
        # AttachThreadInput is the standard, documented way to make
        # SetForegroundWindow succeed reliably even when the calling
        # thread wasn't the one that last received user input.
        if current_thread:
            user32.AttachThreadInput(this_thread, current_thread, True)
        if target_thread:
            user32.AttachThreadInput(this_thread, target_thread, True)
        try:
            user32.SetForegroundWindow(target_hwnd)
        finally:
            if current_thread:
                user32.AttachThreadInput(this_thread, current_thread, False)
            if target_thread:
                user32.AttachThreadInput(this_thread, target_thread, False)
    except OSError:
        pass


def _win_event_proc(hWinEventHook, event, hwnd, idObject, idChild, idEventThread, dwmsEventTime) -> None:
    global _last_good_hwnd
    try:
        if not hwnd:
            return
        if event == EVENT_OBJECT_SHOW:
            # Caught the instant it becomes visible, before it has any real
            # chance to grab foreground at all -- an invisible window can't
            # normally hold OS foreground, so removing WS_VISIBLE outright
            # is a stronger guarantee than fighting over who currently has
            # focus after the fact (SetForegroundWindow-based restore,
            # below, verified NOT reliably effective against this specific
            # window on its own -- the console-window foreground exemption
            # out-prioritizes it).
            if _is_phantom_console(hwnd):
                try:
                    user32.ShowWindow(hwnd, SW_HIDE)
                except OSError:
                    pass
            return
        if event == EVENT_SYSTEM_FOREGROUND:
            if _is_phantom_console(hwnd):
                with _lock:
                    target = _last_good_hwnd
                _restore_foreground(target)
            else:
                with _lock:
                    _last_good_hwnd = hwnd
    except Exception:
        # A hook callback that raises can destabilize the whole hook chain
        # for the process that installed it -- never let anything escape.
        pass


# Kept as a module-level reference -- ctypes doesn't keep the underlying
# callback trampoline alive on its own, and a garbage-collected callback
# would crash the process the next time Windows tries to invoke it.
_callback_ref = _WinEventProcType(_win_event_proc)


def _run_hook_thread() -> None:
    global _last_good_hwnd
    # Without this, _last_good_hwnd stays None until the first NON-phantom
    # foreground event happens to fire after the hook starts -- if the
    # very first event this hook ever sees is itself a phantom (plausible:
    # a chunk can spawn moments after ensure_started() runs), there was
    # nothing to restore to, and the phantom sat in the foreground
    # uncontested until it lost focus on its own. Verified: this was
    # exactly what happened on a live run -- the first steal after startup
    # held foreground for ~11s (no restore attempted), later ones (after
    # a real restore had already happened once, populating this) were
    # corrected within single-digit milliseconds.
    _last_good_hwnd = user32.GetForegroundWindow()
    hooks = [
        user32.SetWinEventHook(ev, ev, 0, _callback_ref, 0, 0, WINEVENT_OUTOFCONTEXT)
        for ev in (EVENT_OBJECT_SHOW, EVENT_SYSTEM_FOREGROUND)
    ]
    if not any(hooks):
        return
    try:
        msg = wt.MSG()
        # WINEVENT_OUTOFCONTEXT delivers callbacks through this thread's
        # own message queue -- needs an actual running message loop, not
        # just a sleep loop, to ever receive them.
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
    finally:
        for h in hooks:
            if h:
                user32.UnhookWinEvent(h)


def ensure_started() -> None:
    """Starts the background hook thread once, lazily, on first use --
    never touches anything if no job ever runs through PtyChunkProcess."""
    global _started
    with _lock:
        if _started:
            return
        _started = True
    threading.Thread(target=_run_hook_thread, daemon=True, name="optifuzz-focus-guard").start()
