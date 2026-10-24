"""Run OptiFuzz as its own desktop window instead of a browser tab.

Starts the same FastAPI app (backend.main:app) in a background thread on a
free local port, waits for it to actually be listening, then opens it in a
native window via pywebview. On Windows this is backed by the system's own
WebView2 runtime (already present with any current Edge/Windows install, via
the pythonnet/WinForms integration pywebview uses) -- not a bundled Chromium
like Electron, so this adds one small pure-Python dependency rather than a
whole separate toolchain.

Run via `pythonw.exe` -- no console window is created at all, not even
briefly. This used to require `python.exe` plus code to hide that console
again at startup (a real console, kept alive but hidden, was believed
necessary for ConPTY, the pseudo-console dirsearch's subprocess is spawned
through; that approach is retired). That
belief was wrong: reproduced directly, a fuzz job COMPLETES normally with
zero console ever created for *this* process, as long as the dirsearch
CHILD subprocess itself is never spawned via `pythonw.exe` --
`backend/engine/dirsearch.py::_default_python_executable()` now guarantees
that regardless of what interpreter this app itself is running under.

Usage: .venv/Scripts/OptiFuzz.exe run_desktop.py
(`OptiFuzz.exe` is a renamed copy of `pythonw.exe`, same folder -- purely so
Task Manager shows "OptiFuzz" instead of the generic "Python"; plain
`pythonw.exe` still works identically otherwise. Rebuilding the venv from scratch loses this copy, it
needs recreating by hand.)
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

from backend.persistence.paths import app_data_dir
from backend.platform_compat import IS_WINDOWS

if IS_WINDOWS:
    import ctypes
    import winreg

# Launched via a genuine double-click (the Start Menu shortcut, a taskbar
# pin, ...) rather than from a terminal, `pythonw.exe`/`OptiFuzz.exe` gets no
# console AND no inherited pipes -- sys.stdout/sys.stderr are then literally
# `None`, not just redirected. uvicorn.Config() unconditionally calls
# `sys.stdout.isatty()` while setting up its default logging formatter, which
# raises `AttributeError: 'NoneType' object has no attribute 'isatty'` on
# that None -- an *uncaught* exception, so the whole process exits before the
# window ever opens. And because stderr is also None, nothing ever prints
# the traceback either: the app just silently fails to appear. (Ran fine in
# every dev-session test throughout this project so far only because a
# terminal/IDE always gave it a real, non-None stdout/stderr -- this only
# reproduces launched fully detached, e.g. `Start-Process` on the .lnk
# itself, which is exactly how a real double-click launches it too.)
# Fix: give both a harmless real file object before anything else can touch
# them, instead of `None`.
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")

import webview

# uvicorn, backend.main (FastAPI + every router/service) and app_settings are
# deliberately NOT imported here at module level any more: that import chain
# alone is ~1s warm and several times that on a cold start (disk cache
# dropped, antivirus scanning each .pyc), and it all ran BEFORE any window
# existed -- the app looked dead for 10-20s on a cold launch. They're
# imported by _boot_backend() below, on a worker thread, while the window is
# already up showing a loading indicator.

_FRONTEND_DIR = Path(__file__).resolve().parent / "frontend"
# Two ink colors, picked to actually stay legible against whichever the
# *shell chrome* (taskbar/Start menu/Alt-Tab) is -- not to match OptiFuzz's
# own always-dark in-page theme. Verified by compositing each onto a dark
# and a light swatch locally: the light-ink mark all but disappears on a
# dark background and vice versa.
# .ico on Windows (what the Win32 shell wants); .png elsewhere (what GTK's
# icon loader handles reliably).
_ICON_EXT = ".ico" if IS_WINDOWS else ".png"
_ICON_FOR_DARK_SHELL = _FRONTEND_DIR / f"icon-dark{_ICON_EXT}"  # light ink, for a dark taskbar
_ICON_FOR_LIGHT_SHELL = _FRONTEND_DIR / f"icon-light{_ICON_EXT}"  # dark ink, for a light taskbar


def _system_uses_light_theme() -> bool | None:
    """Whether Windows' *shell chrome* (taskbar, Start menu, Alt-Tab) is in
    light or dark mode. Deliberately reads `SystemUsesLightTheme`, not
    `AppsUseLightTheme` (the value pywebview's own dark-title-bar check
    uses, see _style_title_bar) -- Windows lets apps and shell chrome run
    opposite themes independently in Settings, and it's the chrome one that
    actually determines what our taskbar/Start-menu/Alt-Tab icon renders
    against. Returns None if it can't be read (missing key on older
    Windows, permissions, ...) -- callers should treat that as "assume dark".
    Always None off Windows: there is no single shell-wide theme setting to
    read, so the dark-shell icon is used.
    """
    if not IS_WINDOWS:
        return None
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
            0,
            winreg.KEY_READ,
        ) as key:
            value, _ = winreg.QueryValueEx(key, "SystemUsesLightTheme")
        return bool(value)
    except OSError:
        return None


def _select_icon_path() -> Path | None:
    """Picks whichever of the two ink-color icon variants actually exists
    and matches the current shell theme -- falls back to the other variant
    if the preferred one is missing, and to None (pywebview's own default,
    the interpreter's icon) only if neither file is present at all."""
    uses_light = _system_uses_light_theme()
    preferred = _ICON_FOR_LIGHT_SHELL if uses_light else _ICON_FOR_DARK_SHELL
    fallback = _ICON_FOR_DARK_SHELL if uses_light else _ICON_FOR_LIGHT_SHELL
    if preferred.is_file():
        return preferred
    if fallback.is_file():
        return fallback
    return None

# Without this, Windows groups this window's taskbar button under whichever
# generic AppUserModelID python.exe/pythonw.exe has (if any) and shows the
# interpreter's own icon there instead of the window's -- the title bar icon
# (set via webview.start(icon=...) below) is unaffected either way, only the
# taskbar button/thumbnail is. Must be called before any window is created.
def _claim_windows_app_identity() -> None:
    if not IS_WINDOWS:
        return
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("OptiFuzz.DesktopApp")
    except (AttributeError, OSError):
        pass


_DWMWA_USE_IMMERSIVE_DARK_MODE = 20
_DWMWA_TEXT_COLOR = 36  # Windows 11 (build 22000+) only
_TITLE_BAR_WATCH_SECONDS = 1.0

# Windows' own default dark-mode caption background (RGB 32,32,32) -- not an
# officially documented constant, but the consistently observed shade every
# dark-mode Win32/WinForms app gets from DWMWA_USE_IMMERSIVE_DARK_MODE alone
# with no DWMWA_CAPTION_COLOR override. Matching the text to *this* (rather
# than overriding the caption to an exact app color) keeps the title bar
# visually distinct from the page content below it -- a custom exact match
# made the whole window read as one undifferentiated block, which the user
# didn't want.
_DEFAULT_DARK_CAPTION_COLOR = "#202020"


def _colorref(hex_color: str) -> int:
    """DWM text color attribute takes a Win32 COLORREF, which is
    0x00BBGGRR -- byte order reversed from the usual #RRGGBB."""
    hex_color = hex_color.lstrip("#")
    r, g, b = (int(hex_color[i : i + 2], 16) for i in (0, 2, 4))
    return (b << 16) | (g << 8) | r


def _style_title_bar(window: webview.Window) -> None:
    """Dark title bar, with its text invisible against it.

    - DWMWA_USE_IMMERSIVE_DARK_MODE: pywebview only darkens the title bar
      when *Windows'* own system theme is dark -- OptiFuzz has no light
      variant at all, so this is forced unconditionally rather than left to
      follow the OS setting. Works on Windows 10 2004+ and 11. The caption
      *background* itself is left as whatever this puts there (Windows'
      standard dark default) -- deliberately not overridden to an exact app
      color; see _DEFAULT_DARK_CAPTION_COLOR above for why.
    - DWMWA_TEXT_COLOR (Windows 11 only): painted the same as that default
      dark background. The window's title string is still the real
      "OptiFuzz" (so the taskbar tooltip and Alt-Tab switcher, which read
      the title directly rather than rendering DWM's caption text, still
      show it) -- painting the caption text the same color as its
      background just makes it visually disappear there specifically,
      without touching either of those. On Windows 10 (no attribute 36),
      the title text stays visible in the title bar itself -- a known gap.

    window.native (the underlying WinForms Form, with a real HWND) only
    exists once the window has actually been shown, hence the event hook
    rather than calling this immediately after create_window().
    """
    def _apply(*_args, **_kwargs) -> None:
        try:
            hwnd = window.native.Handle.ToInt32()
        except (AttributeError, OSError):
            return

        def _set(attr: int, value: int) -> None:
            try:
                cval = ctypes.c_int(value)
                ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(cval), ctypes.sizeof(cval))
            except OSError:
                pass  # attribute not supported on this Windows version -- skip it, not fatal

        _set(_DWMWA_USE_IMMERSIVE_DARK_MODE, 1)
        _set(_DWMWA_TEXT_COLOR, _colorref(_DEFAULT_DARK_CAPTION_COLOR))

    # Not just `shown` (once, at startup) -- real, reported bug: the title
    # bar's dark styling sometimes reverted to Windows' own light default
    # later in a session. DWM repaints the non-client area from the system
    # theme on its own in several situations this app has no direct hook
    # for (a system light/dark toggle while the window is open, waking from
    # sleep, certain display/DPI changes) -- `_apply` is deliberately cheap
    # and idempotent (just re-asserts the same two attributes), so
    # re-running it on every un-minimize/un-maximize is a safe, low-cost
    # way to correct that without needing to catch the underlying
    # WM_SETTINGCHANGE/theme-change message itself (pywebview exposes no
    # hook for that).
    window.events.shown += _apply
    window.events.restored += _apply
    window.events.maximized += _apply

    # Those events are not enough: the bar still turned white at random moments (Windows
    # re-themes the caption on focus/theme changes no event reports). So once shown, a daemon
    # thread keeps checking the dark flag and puts it back when it was lost. Raw dwmapi/user32
    # calls on the stored HWND only, safe from any thread (see _hide_title_bar_icon).
    def _watch(*_args, **_kwargs) -> None:
        try:
            hwnd = window.native.Handle.ToInt32()
        except (AttributeError, OSError):
            return

        def _guard() -> None:
            while ctypes.windll.user32.IsWindow(hwnd):
                try:
                    current = ctypes.c_int(-1)
                    ctypes.windll.dwmapi.DwmGetWindowAttribute(
                        hwnd, _DWMWA_USE_IMMERSIVE_DARK_MODE, ctypes.byref(current), ctypes.sizeof(current),
                    )
                    if current.value != 1:
                        for attr, value in (
                            (_DWMWA_USE_IMMERSIVE_DARK_MODE, 1),
                            (_DWMWA_TEXT_COLOR, _colorref(_DEFAULT_DARK_CAPTION_COLOR)),
                        ):
                            cval = ctypes.c_int(value)
                            ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(cval), ctypes.sizeof(cval))
                except OSError:
                    pass
                time.sleep(_TITLE_BAR_WATCH_SECONDS)

        threading.Thread(target=_guard, name="title-bar-guard", daemon=True).start()

    window.events.shown += _watch


_GWL_EXSTYLE = -20
_WS_EX_DLGMODALFRAME = 0x00000001
_SWP_NOMOVE = 0x0002
_SWP_NOSIZE = 0x0001
_SWP_NOZORDER = 0x0004
_SWP_FRAMECHANGED = 0x0020
_WM_SETICON = 0x0080
_ICON_SMALL = 0


# The small icon of the title bar (and of the taskbar's hover-preview header) always uses the
# dark-ink variant: the title bar is forced dark, so it blends in instead of repeating the
# light logo of the app's own header (a light one stood out next to it). The big icon, the
# taskbar button and Alt-Tab, keeps following the shell theme (_select_icon_path).
_TITLE_BAR_ICON = _FRONTEND_DIR / "icon-light.ico"
_SM_CXSMICON = 49
_IMAGE_ICON = 1
_LR_LOADFROMFILE = 0x00000010


def _set_small_icon(hwnd: int, icon_path: Path) -> bool:
    """Gives the window this .ico as its small (title bar) icon. False if it cannot be loaded.
    Raw user32 calls on the HWND only, so it is safe from any thread (see _hide_title_bar_icon)."""
    if not icon_path.is_file():
        return False
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.GetSystemMetrics.restype = ctypes.c_int
    user32.LoadImageW.restype = ctypes.c_void_p
    user32.LoadImageW.argtypes = [
        ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint, ctypes.c_int, ctypes.c_int, ctypes.c_uint,
    ]
    user32.SendMessageW.restype = ctypes.c_void_p
    user32.SendMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p]
    size = user32.GetSystemMetrics(_SM_CXSMICON)
    handle = user32.LoadImageW(None, str(icon_path), _IMAGE_ICON, size, size, _LR_LOADFROMFILE)
    if not handle:
        return False
    user32.SendMessageW(hwnd, _WM_SETICON, _ICON_SMALL, handle)
    return True


def _hide_title_bar_icon(window: webview.Window) -> None:
    """Makes the small caption icon of the native title bar (and of the taskbar's
    hover-preview thumbnail header, which shows the same glyph) the dark-ink one --
    the app's own in-page header already shows the logo, and a light icon duplicated it
    in the OS chrome above it. (Hiding it outright was tried and cannot be done reliably
    on this window; the dark one blends into the forced-dark title bar instead.)

    Deliberately only raw Win32 API calls (ctypes against the HWND directly)
    -- NOT `window.native.ShowIcon = False`, WinForms' own documented
    property for this, which was tried here first. pywebview's `shown`
    event runs its handlers on a freshly spawned Python thread, never the
    window's own UI thread (see webview/event.py: `Event.set()` always
    starts a `threading.Thread` unless the event was constructed with
    `should_lock=True`, and pywebview's own `shown` isn't). WinForms
    objects are single-threaded-apartment and not safe to touch from any
    other thread; `ShowIcon`'s setter did exactly that and froze the whole
    window (verified: removing it was what fixed the freeze). Reading
    `window.native.Handle` below is fine -- it's a plain stored field, not
    a call into WinForms' own UI-thread-affine logic -- and every call after
    that is a direct `user32`/`dwmapi` Win32 API call against that raw HWND,
    which Windows explicitly supports from any thread.
    """
    def _apply(*_args, **_kwargs) -> None:
        try:
            hwnd = window.native.Handle.ToInt32()
            ex_style = ctypes.windll.user32.GetWindowLongW(hwnd, _GWL_EXSTYLE)
            ctypes.windll.user32.SetWindowLongW(hwnd, _GWL_EXSTYLE, ex_style | _WS_EX_DLGMODALFRAME)
            ctypes.windll.user32.SetWindowPos(
                hwnd, 0, 0, 0, 0, 0,
                _SWP_NOMOVE | _SWP_NOSIZE | _SWP_NOZORDER | _SWP_FRAMECHANGED,
            )
            if not _set_small_icon(hwnd, _TITLE_BAR_ICON):
                ctypes.windll.user32.SendMessageW(hwnd, _WM_SETICON, _ICON_SMALL, 0)  # no dark icon: clear it
        except (AttributeError, OSError):
            pass

    window.events.shown += _apply


def _webview_storage_path() -> str:
    """A stable WebView2 profile directory, next to the workspace data but
    not inside it (workspace_root() is specifically Projects/Targets/Jobs
    data). Without this, pywebview's own default (`private_mode=True`)
    picks a fresh temp folder every single launch -- verified directly: a
    running instance's `msedgewebview2.exe --user-data-dir=...` pointed at
    a path under `%TEMP%`, a different one than a previous launch's. That
    silently discards the page's own localStorage on every restart, which
    breaks anything meant to be *remembered* (e.g. the last-used wordlist)
    across sessions, not just across in-app navigation.
    """
    path = app_data_dir() / "webview_profile"
    path.mkdir(parents=True, exist_ok=True)
    return str(path)


class _Api:
    """Exposed to the frontend as `window.pywebview.api.*` (pywebview's own
    js_api bridge -- every public method becomes an async-callable JS
    function). Only `pick_folder` so far: the native "choose a folder"
    dialog the outputs-mirror-directory setting needs, which a browser
    (even a Chromium one like WebView2) never exposes to a plain web page
    for security reasons -- this is the one thing that genuinely needs the
    desktop app specifically, not just "a web page pywebview happens to be
    showing". `_window` is set once main() actually creates it (this class
    is instantiated before that), since create_file_dialog needs the real
    window object.

    STANDING RULE: this attribute MUST stay named with a leading underscore
    -- real, reproduced bug otherwise. pywebview's own js_api bridge
    (webview/util.py::get_functions) walks every PUBLIC, non-callable
    attribute of this object with `dir()`/`getattr()` to auto-generate the
    JS-side function list, recursing into any attribute that's itself a
    non-callable object. A plain `self.window = window` (no underscore) got
    walked straight into the live `webview.Window` instance, which
    eventually reaches `window.native` -- the raw WinForms Form/COM object,
    whose reflected property surface (`.native.AccessibilityObject.Bounds
    .Empty.Empty.Empty...`, endlessly) is effectively unbounded and never
    trips get_functions' own id()-based cycle guard (each COM property
    access hands back a distinct wrapper object). Confirmed directly: a
    `py-spy dump` of a hung, unresponsive launch showed the MAIN thread
    stuck forever inside `webview.create_window()`, waiting on exactly this
    recursive walk on a *separate* thread to finish -- it never does.
    get_functions skips any attribute starting with `_` unconditionally, so
    the leading underscore here is the actual, real fix -- not cosmetic.
    This is also what those `[pywebview] Error while processing
    window.native.AccessibilityObject...` log lines from earlier testing
    actually were (misread then as some external accessibility tool
    walking the window -- it was this, self-inflicted, the whole time).
    """

    _window: webview.Window | None = None

    def pick_folder(self, initial_dir: str = "") -> str | None:
        if self._window is None:
            return None
        result = self._window.create_file_dialog(webview.FileDialog.FOLDER, directory=initial_dir or "")
        return result[0] if result else None

    # Files this app saved: the only ones open_download / show_download will act on.
    _saved_paths: set[str] = set()

    def save_download(self, url: str) -> dict | None:
        """Saves one of this app's own downloads (an export) where the user chooses, with a Save As dialog over
        the window, and returns {path, name}. The web view's own download popup is not used any more: it
        could open outside the window, on another screen. None when cancelled, {error} on failure."""
        import email.message
        import urllib.parse
        import urllib.request

        if self._window is None:
            return {"error": "the window is not ready"}
        parts = urllib.parse.urlparse(url)
        if parts.scheme != "http" or parts.hostname not in ("127.0.0.1", "localhost") or not parts.path.startswith("/api/"):
            return {"error": "not a download of this app"}
        try:
            with urllib.request.urlopen(url, timeout=120) as response:
                data = response.read()
                disposition = response.headers.get("Content-Disposition", "")
        except OSError as exc:
            return {"error": str(exc)}
        header = email.message.Message()
        header["Content-Disposition"] = disposition
        name = os.path.basename(header.get_filename() or "") or "optifuzz-export"
        downloads = Path.home() / "Downloads"
        chosen = self._window.create_file_dialog(
            webview.FileDialog.SAVE,
            directory=str(downloads if downloads.is_dir() else Path.home()),
            save_filename=name,
        )
        if not chosen:
            return None
        path = Path(chosen if isinstance(chosen, str) else chosen[0])
        try:
            path.write_bytes(data)
        except OSError as exc:
            return {"error": str(exc)}
        self._saved_paths.add(str(path))
        return {"path": str(path), "name": path.name}

    def update_app(self) -> dict:
        """Used by the "could not start" page: restores the program files to the official version and
        updates, then restarts. Projects and settings live in another folder and are never touched."""
        from backend.services import update_service

        try:
            update_service.restore_program_files(_damaged_program_file)
            result = update_service.apply()
        except update_service.UpdateError as exc:
            return {"error": str(exc)}
        if not result.get("updated"):
            return {"message": "The program files were restored. Close and open OptiFuzz again."}
        update_service.register_desktop_restart(self._window.destroy, [sys.executable, str(Path(__file__).resolve())])
        try:
            update_service.restart()
        except update_service.UpdateError as exc:
            return {"error": str(exc)}
        return {"message": "Updated. OptiFuzz is restarting..."}

    def open_download(self, path: str) -> None:
        """Opens a file saved by save_download. An HTML report goes to the browser chosen in Settings, brought
        to the front like every other URL this app opens; anything else opens with the system's own program."""
        if path not in self._saved_paths or not Path(path).is_file():
            raise ValueError("that file was not saved by this app")
        from backend.persistence import app_settings
        from backend.services import browser_service

        if Path(path).suffix.lower() in (".html", ".htm"):
            browser_path = app_settings.get_browser_path() or browser_service.default_browser_path()
            if browser_service.open_url(Path(path).as_uri(), browser_path):
                return
        browser_service.open_with_system(path)

    def show_download(self, path: str) -> None:
        """Shows a file saved by save_download in the system's file manager."""
        if path not in self._saved_paths or not Path(path).is_file():
            raise ValueError("that file was not saved by this app")
        from backend.services import browser_service

        browser_service.show_in_folder(path)


def _free_port(preferred: int) -> int:
    for candidate in (preferred, 0):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            if not IS_WINDOWS:
                # Same option uvicorn binds with: without it a port still in TIME_WAIT (right after
                # the previous copy closed, e.g. the restart after an update) looks taken and the app
                # would move to a random port. Not set on Windows, where it would also let a port
                # that another program is listening on look free.
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("127.0.0.1", candidate))
                return sock.getsockname()[1]
            except OSError:
                continue
    raise RuntimeError("Could not bind to any local port")


def _wait_until_listening(port: int, timeout: float = 60.0) -> None:
    # 60s (was 10s): this no longer gates the window appearing, so a very
    # slow cold start just keeps showing the loading indicator instead of
    # having to fit inside a tight budget.
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError(f"OptiFuzz backend did not start listening on port {port} in time")


# Shown the instant the window opens, until the backend is up and the real
# page replaces it. Same dark background as styles.css's --bg so there is no
# flash between this and the app itself.
_LOADING_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>OptiFuzz</title>
<style>
html, body { height: 100%; margin: 0; background: #0f1115; color: #8b90a0;
  font-family: "Segoe UI", system-ui, sans-serif; font-size: 14px; }
body { display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 18px; }
.spinner { width: 30px; height: 30px; border: 3px solid #232833; border-top-color: #4f8cff;
  border-radius: 50%; animation: spin .9s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }
</style></head>
<body><div class="spinner"></div><div>Loading OptiFuzz&hellip;</div></body></html>"""


_damaged_program_file: str | None = None  # the program file named by the error the start page is showing


def _error_html(message: str, *, offer_update: bool = False) -> str:
    import html as html_lib

    body = (
        "<div style='max-width:640px;padding:0 24px;text-align:center'>"
        "<div style='color:#ff6b6b;font-size:16px;margin-bottom:10px'>OptiFuzz could not start</div>"
        f"<div style='white-space:pre-wrap'>{html_lib.escape(message)}</div>"
    )
    if offer_update:
        body += (
            "<div style='margin-top:18px;line-height:1.5'>One of the program's own files is damaged or missing. "
            "Updating OptiFuzz restores the program files to the official version. "
            "<b>Your projects, results and settings are not touched.</b></div>"
            "<button id='fix' style='margin-top:16px;padding:8px 18px;border:0;border-radius:6px;"
            "background:#4f8cff;color:#fff;font-size:14px;cursor:pointer'>Update OptiFuzz</button>"
            "<div id='fixmsg' style='margin-top:12px;white-space:pre-wrap'></div>"
            "<script>document.getElementById('fix').addEventListener('click', async function () {"
            "var b = this, m = document.getElementById('fixmsg'); b.disabled = true; m.textContent = 'Updating...';"
            "try { var r = await window.pywebview.api.update_app();"
            "m.textContent = r.error || r.message || 'Done.'; if (r.error) b.disabled = false; }"
            "catch (e) { m.textContent = String(e); b.disabled = false; } });</script>"
        )
    return _LOADING_HTML.split("<body>")[0] + "<body>" + body + "</div></body></html>"


_auto_ui_scale: float | None = None
_auto_ui_scale_known = False


def _auto_scale() -> float | None:
    """The automatic zoom (see _linux_ui_scale), worked out once: under WSL it asks Windows and
    can take a second, which a later change of the zoom setting must not wait for again."""
    global _auto_ui_scale, _auto_ui_scale_known
    if not _auto_ui_scale_known:
        _auto_ui_scale = _linux_ui_scale()
        _auto_ui_scale_known = True
    return _auto_ui_scale


def _effective_zoom_scale(configured_percent: int | None, auto_scale: float | None) -> float | None:
    """The zoom to apply: the one chosen in Settings -> Display zoom, else the automatic one
    (None = leave the view as it is)."""
    if configured_percent:
        return configured_percent / 100
    return auto_scale


def _zoom_for_display(window: webview.Window) -> None:
    from backend.persistence.app_settings import get_ui_zoom_percent

    scale = _effective_zoom_scale(get_ui_zoom_percent(), _auto_scale())
    if scale:
        _apply_page_zoom(window, scale)


def _boot_backend(window: webview.Window) -> None:
    """Runs on pywebview's worker thread (webview.start(func)), after the
    window already exists: imports the heavy backend, starts uvicorn on a
    free port, waits for it, then points the window at it. Importing here
    (rather than at module level) is what lets the window appear
    immediately instead of after the whole import chain."""
    if not IS_WINDOWS:
        # Own thread: querying the display scaling can take ~1s under WSL
        # and must not delay the backend starting.
        threading.Thread(target=_zoom_for_display, args=(window,), daemon=True).start()
    try:
        import uvicorn

        from backend.main import app
        from backend.persistence.app_settings import DEFAULT_PORT, get_custom_port
        from backend.services import update_service

        # Lets Settings -> Updates restart the app by itself: close this window, start this script again.
        update_service.register_desktop_restart(window.destroy, [sys.executable, str(Path(__file__).resolve())])
        if not IS_WINDOWS:
            # Settings -> Display zoom (Linux only): applies a new zoom to this window right away.
            from backend.services import display_service

            display_service.register_zoom_applier(
                lambda percent: _apply_page_zoom(window, _effective_zoom_scale(percent, _auto_scale()) or 1.0)
            )

        from backend.services import port_service

        custom_port = get_custom_port()
        # A busy port is never silently swapped for a random one: the fallback is chosen away from commonly
        # used ports, and Settings shows a notice (red dot on the gear) saying which port is in use and why.
        port = port_service.pick_port(custom_port or DEFAULT_PORT, chosen_by_user=bool(custom_port))
        config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
        server = uvicorn.Server(config)
        # daemon=True: this thread (and the server it runs) dies with the
        # process once the desktop window closes and main() returns -- no
        # separate shutdown step needed for a local dev tool.
        threading.Thread(target=server.run, daemon=True).start()
        _wait_until_listening(port)
    except Exception as exc:  # shown in the window itself -- pythonw has no console to print to
        _wait_for_first_page(window)
        global _damaged_program_file
        damaged_file = getattr(exc, "filename", None) or getattr(exc, "path", None)
        _damaged_program_file = str(damaged_file) if damaged_file else None
        detail = f"{type(exc).__name__}: {exc}" + (f"\nFile: {damaged_file}" if damaged_file else "")
        window.load_html(_error_html(detail, offer_update=isinstance(exc, (ImportError, SyntaxError, OSError))))
        return
    _wait_for_first_page(window)
    window.load_url(f"http://127.0.0.1:{port}")


# pywebview's load_url()/load_html() give up with "Main window failed to start" if the window has
# not finished loading its first page (the "Loading OptiFuzz..." one) within 20 seconds. A cold
# WebKit start, software rendering or a busy virtual machine can take longer -- observed on
# GitHub's Linux runners -- and the app then sat on the loading page forever. Waiting here, for up
# to three minutes, makes pywebview's own 20s wait return immediately. On Windows the first page
# loads at once, so this changes nothing there.
_FIRST_PAGE_TIMEOUT_SECONDS = 180


def _wait_for_first_page(window: webview.Window) -> None:
    window.events.loaded.wait(_FIRST_PAGE_TIMEOUT_SECONDS)


def _prefer_dark_gtk_theme() -> None:
    """Linux counterpart of _style_title_bar: OptiFuzz has no light variant,
    so ask GTK for its dark theme variant (dark title bar and scrollbars)
    regardless of the desktop's own setting. Best-effort -- a missing
    PyGObject or a desktop that ignores the hint just keeps its normal
    chrome. Runs on the main thread before any window exists, so it never
    touches GTK from pywebview's worker threads."""
    try:
        import gi

        gi.require_version("Gtk", "3.0")
        from gi.repository import Gtk

        settings = Gtk.Settings.get_default()
        if settings is not None:
            settings.set_property("gtk-application-prefer-dark-theme", True)
            # GTK's built-in default layout is "menu:close" -- what you get
            # when nothing configured the desktop's own (no dconf, e.g. WSLg
            # or a minimal install): a window with only a close button.
            # Anything else was set deliberately by the user's desktop, so
            # it's left alone.
            if settings.get_property("gtk-decoration-layout") == "menu:close":
                settings.set_property("gtk-decoration-layout", ":minimize,maximize,close")
    except (ImportError, ValueError):
        pass


def _use_fractional_font_metrics() -> None:
    """Makes WebKitGTK lay text out like Chromium/DirectWrite on Windows.

    GTK's default cairo font options hint the *metrics*, rounding every
    glyph's advance to a whole pixel. Measured against the Windows build:
    the same 14px sentence came out 560px wide (hinted) vs 540.2px
    (unhinted) vs 538.9px on Windows, and Cascadia Mono 648px vs exactly
    664.45px -- so every button, input and column rendered visibly wider
    than on Windows (a "Resume" button wrapped onto two lines). Fractional
    advances remove that. Grayscale antialiasing is kept as it is (no
    subpixel colour fringes depending on the desktop's setting).

    Like _prefer_dark_gtk_theme: main thread, before any window exists;
    best-effort."""
    try:
        import gi

        gi.require_foreign("cairo")
        import cairo
        from gi.repository import Gdk

        screen = Gdk.Screen.get_default()
        if screen is None:
            return
        options = cairo.FontOptions()
        options.set_hint_metrics(cairo.HINT_METRICS_OFF)
        options.set_hint_style(cairo.HINT_STYLE_NONE)
        options.set_antialias(cairo.ANTIALIAS_GRAY)
        screen.set_font_options(options)
    except (ImportError, ValueError, RuntimeError):
        pass


def _download_attachments_instead_of_navigating() -> None:
    """Makes `Content-Disposition: attachment` responses (Export HTML) save a
    file instead of replacing the app's own page.

    WebView2 honours that header natively and shows a Save As dialog.
    pywebview's GTK backend only starts a download when WebKit cannot
    *display* the response's MIME type -- an HTML export can, so it simply
    navigated the app window to the downloaded file (the app disappeared,
    replaced by the export). This wraps its policy handler so an attachment
    is always downloaded; pywebview's own download-started handler then
    shows its Save As dialog.

    Patches the class before any window exists (pywebview connects the
    handler when it builds the window); a no-op if pywebview's GTK backend
    isn't importable."""
    try:
        from webview.platforms import gtk as pywebview_gtk
    except ImportError:
        return

    browser_view = pywebview_gtk.BrowserView
    webkit = pywebview_gtk.webkit
    original = browser_view.on_navigation

    def on_navigation(self, web_view, decision, decision_type):
        if type(decision) is webkit.ResponsePolicyDecision:
            headers = decision.get_response().get_http_headers()
            disposition = headers.get_one("Content-Disposition") if headers else None
            if disposition and disposition.strip().lower().startswith("attachment"):
                decision.download()
                return True
        return original(self, web_view, decision, decision_type)

    browser_view.on_navigation = on_navigation


def _linux_ui_scale() -> float | None:
    """Extra zoom to apply on Linux, or None for none.

    OPTIFUZZ_UI_SCALE overrides (e.g. "1.5"). Otherwise, only under WSLg:
    it hands GTK scale 1 whatever the Windows display scaling is, so on a
    150% display the whole UI renders 1.5x too small next to the Windows
    build. Reads the scaling Windows itself applied. A native Linux desktop
    does its own scaling, so nothing is done there."""
    override = os.environ.get("OPTIFUZZ_UI_SCALE")
    if override:
        try:
            return float(override)
        except ValueError:
            return None
    if not os.environ.get("WSL_DISTRO_NAME"):
        return None
    try:
        out = subprocess.run(
            [
                "powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                r"(Get-ItemProperty 'HKCU:\Control Panel\Desktop\WindowMetrics').AppliedDPI",
            ],
            capture_output=True, text=True, timeout=10,
        )
        scale = int(out.stdout.strip()) / 96
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    return scale if scale > 1.0 else None


def _apply_page_zoom(window: webview.Window, scale: float) -> None:
    """Zooms the web view. Scheduled onto GTK's own main loop: the WebView
    is only safe to touch from the UI thread, and this is called from
    pywebview's worker thread (same rule as the Windows-side window.native
    handlers). window.native is deliberately not used here."""
    try:
        from gi.repository import GLib
        from webview.platforms.gtk import BrowserView
    except ImportError:
        return

    def _do() -> bool:
        view = BrowserView.instances.get(window.uid)
        if view is not None:
            view.webview.set_zoom_level(scale)
        return False  # run once

    GLib.idle_add(_do)


_REFRESH_SHORTCUT_ICON_SCRIPT = (
    "$s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:OPTIFUZZ_LNK); "
    "if ($s.WorkingDirectory.TrimEnd('\\') -ne $env:OPTIFUZZ_ROOT.TrimEnd('\\')) { exit 0 }; "
    "$want = $env:OPTIFUZZ_ICON + ',0'; "
    "if ($s.IconLocation -ne $want) { $s.IconLocation = $want; $s.Save() }"
)


def _keep_start_menu_icon_in_step(icon_path: Path | None) -> None:
    """The app list of the Start menu shows the icon stored in the shortcut, and a shortcut cannot follow the
    Windows light/dark theme by itself (the taskbar icon can, because the app sets it). If this copy's shortcut
    exists, point it at the icon that matches the current theme (the dark ink one in light mode), so it follows a
    theme change the next time the app starts. Only this copy's own shortcut is touched. Best effort, never raises."""
    if not IS_WINDOWS or icon_path is None:
        return
    shortcut = Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "OptiFuzz.lnk"
    if not shortcut.is_file():
        return
    env = dict(
        os.environ,
        OPTIFUZZ_LNK=str(shortcut),
        OPTIFUZZ_ICON=str(icon_path),
        OPTIFUZZ_ROOT=str(Path(__file__).resolve().parent.parent),  # <repo>/app/<this file>
    )
    try:
        subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command",
             _REFRESH_SHORTCUT_ICON_SCRIPT],
            env=env, timeout=30, capture_output=True, creationflags=0x08000000,  # CREATE_NO_WINDOW
        )
    except (OSError, subprocess.SubprocessError):
        pass


def main() -> None:
    # One OptiFuzz per data folder: a second copy would load the same jobs as the first one and pause them
    # on disk or scan them twice. It shows the window that is already open and ends.
    from backend.services import instance_lock

    if not instance_lock.acquire():
        instance_lock.bring_owner_to_front()
        return
    _claim_windows_app_identity()
    if not IS_WINDOWS:
        _prefer_dark_gtk_theme()
        _use_fractional_font_metrics()
        _download_attachments_instead_of_navigating()
    # pywebview disables downloads by default (webview.settings['ALLOW_DOWNLOADS']
    # is False out of the box) -- without this, a Content-Disposition: attachment
    # response (Export HTML) is just silently swallowed by the WebView2 view: no
    # error, no dialog, nothing happens at all. Must be set before
    # create_window()/start(), not just before start() -- webview.settings is read
    # at window-creation time on the WebView2 backend.
    webview.settings["ALLOW_DOWNLOADS"] = True

    api = _Api()
    window = webview.create_window(
        # Kept as a real title (not blanked) -- the taskbar tooltip and
        # Alt-Tab switcher read this string directly, and the user wants
        # "OptiFuzz" to still show up *there*. _style_title_bar() paints the
        # caption text the same color as its own background instead, which
        # makes it invisible in the title bar specifically without touching
        # either of those.
        "OptiFuzz",
        # The loading page, not the app: the real URL needs the backend up
        # first, which now happens on a worker thread (see _boot_backend)
        # AFTER this window is already visible.
        html=_LOADING_HTML,
        width=1280,
        height=820,
        min_size=(900, 600),
        # Starts maximized (not a fixed 1280x820 box) -- that fixed size
        # rendered noticeably off-center on some displays/multi-monitor
        # setups; width/height above are still what it restores to if the
        # user un-maximizes it by hand.
        maximized=True,
        # Matches styles.css's --bg -- avoids a flash of white while the
        # page's own CSS is still loading, before the dark background paints.
        background_color="#0f1115",
        js_api=api,
    )
    api._window = window
    if IS_WINDOWS:
        _style_title_bar(window)
        _hide_title_bar_icon(window)
    icon_path = _select_icon_path()
    threading.Thread(target=_keep_start_menu_icon_in_step, args=(icon_path,), daemon=True).start()
    webview.start(
        _boot_backend,
        (window,),
        icon=str(icon_path) if icon_path else None,
        private_mode=False,
        storage_path=_webview_storage_path(),
    )


if __name__ == "__main__":
    main()
