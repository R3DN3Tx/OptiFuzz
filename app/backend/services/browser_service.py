from __future__ import annotations

import os
import shutil
import subprocess
import webbrowser
from pathlib import Path

from backend.platform_compat import IS_WINDOWS

if IS_WINDOWS:
    import ctypes
    import winreg

_ASFW_ANY = -1  # ctypes.windll.user32.AllowSetForegroundWindow(ASFW_ANY)


def _allow_foreground_steal() -> None:
    """Windows normally refuses to let a background process (this server)
    bring a newly-launched window to the front -- verified: Chrome only
    "worked" because it was already the focused app, while Edge opened
    behind everything and Firefox's new tab landed in an existing,
    unfocused window. This is the standard opt-in for a background process
    to hand focus to whatever it launches next; best-effort since Windows
    can still refuse depending on policy/version, so never fails the call.
    No-op elsewhere: Linux window managers have no equivalent restriction
    that a process can opt out of.
    """
    if not IS_WINDOWS:
        return
    try:
        ctypes.windll.user32.AllowSetForegroundWindow(_ASFW_ANY)
    except (AttributeError, OSError):
        pass


# Standard install locations, checked in this order -- Firefox first, since
# that's the pentester default this app targets (dedicated attack browser,
# often with FoxyProxy/Burp already configured, kept separate from whatever
# browser is running OptiFuzz itself).
_WINDOWS_CANDIDATE_PATHS: dict[str, tuple[str, ...]] = {
    "Firefox": (
        r"C:\Program Files\Mozilla Firefox\firefox.exe",
        r"C:\Program Files (x86)\Mozilla Firefox\firefox.exe",
    ),
    "Google Chrome": (
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ),
    "Microsoft Edge": (
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ),
    "Brave": (
        r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
        r"C:\Program Files (x86)\BraveSoftware\Brave-Browser\Application\brave.exe",
    ),
}

# Linux: the usual package-manager / snap locations. Anything installed
# elsewhere (or under a different name) is caught by the PATH lookup in
# _lookup_browser_path().
_LINUX_CANDIDATE_PATHS: dict[str, tuple[str, ...]] = {
    "Firefox": (
        "/usr/bin/firefox",
        "/usr/bin/firefox-esr",
        "/snap/bin/firefox",
        "/usr/local/bin/firefox",
    ),
    "Google Chrome": (
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/opt/google/chrome/chrome",
    ),
    "Chromium": (
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/snap/bin/chromium",
    ),
    "Microsoft Edge": (
        "/usr/bin/microsoft-edge",
        "/usr/bin/microsoft-edge-stable",
    ),
    "Brave": (
        "/usr/bin/brave-browser",
        "/usr/bin/brave",
        "/snap/bin/brave",
    ),
}

_CANDIDATE_PATHS: dict[str, tuple[str, ...]] = (
    _WINDOWS_CANDIDATE_PATHS if IS_WINDOWS else _LINUX_CANDIDATE_PATHS
)


# Name each browser is registered under, used by _lookup_browser_path(): on
# Windows, the "App Paths" registry key (HKLM/HKCU ...\App Paths\<exe>) that
# Windows itself uses to resolve e.g. `firefox` from an arbitrary install
# location; on Linux, the first match on $PATH. Either way this is what makes
# detection more than just the hardcoded folder guesses above.
_WINDOWS_EXE_NAMES: dict[str, tuple[str, ...]] = {
    "Firefox": ("firefox.exe",),
    "Google Chrome": ("chrome.exe",),
    "Microsoft Edge": ("msedge.exe",),
    "Brave": ("brave.exe",),
}

_LINUX_EXE_NAMES: dict[str, tuple[str, ...]] = {
    "Firefox": ("firefox", "firefox-esr"),
    "Google Chrome": ("google-chrome", "google-chrome-stable"),
    "Chromium": ("chromium", "chromium-browser"),
    "Microsoft Edge": ("microsoft-edge", "microsoft-edge-stable"),
    "Brave": ("brave-browser", "brave"),
}

_EXE_NAMES: dict[str, tuple[str, ...]] = _WINDOWS_EXE_NAMES if IS_WINDOWS else _LINUX_EXE_NAMES


def _linux_search_path() -> str:
    """$PATH to search. Under WSL, the Windows drives (/mnt/c/...) are
    appended to $PATH and live on a slow 9p filesystem -- every lookup walks
    all of them (measured: most of a ~1s detection pass), and a Windows .exe
    found there couldn't be launched as the Linux browser anyway."""
    path = os.environ.get("PATH", os.defpath)
    if os.environ.get("WSL_DISTRO_NAME"):
        path = os.pathsep.join(p for p in path.split(os.pathsep) if not p.startswith("/mnt/"))
    return path


def _lookup_browser_path(exe_name: str) -> str | None:
    if not exe_name:
        return None
    if not IS_WINDOWS:
        found = shutil.which(exe_name, path=_linux_search_path())
        return found if found and Path(found).is_file() else None
    key_path = rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{exe_name}"
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            with winreg.OpenKey(hive, key_path) as key:
                value, _ = winreg.QueryValueEx(key, None)  # default value = full exe path
        except OSError:
            continue
        if value and Path(value).is_file():
            return value
    return None


def detect_installed_browsers() -> list[dict[str, str]]:
    """Real filesystem detection, never a fixed answer: a browser only
    appears here if a file actually exists at a resolved path right now.
    Checks the standard install folders first (fast), then falls back to the
    OS's own lookup (Windows' App Paths registry key, Linux's $PATH) --
    covers an install in a non-default location the hardcoded folder list
    can't."""
    found = []
    for name, candidates in _CANDIDATE_PATHS.items():
        path = next((c for c in candidates if Path(c).is_file()), None)
        if path is None:
            for exe_name in _EXE_NAMES.get(name, ()):
                path = _lookup_browser_path(exe_name)
                if path:
                    break
        if path:
            found.append({"name": name, "path": path})
    return found


def validate_browser_executable(path_str: str) -> str | None:
    """None if `path_str` looks like a real, launchable executable for this
    OS; otherwise a short human-readable reason it doesn't. Checked before a
    custom browser path is accepted, rather than trusting whatever the user
    typed (a typo'd or non-executable path should fail loudly here, not
    silently at the moment a pentester actually tries to open a result)."""
    path = Path(path_str)
    if not path.is_file():
        return "File not found"
    if IS_WINDOWS:
        if path.suffix.lower() != ".exe":
            return "Not a .exe file"
        try:
            with open(path, "rb") as file_handle:
                header = file_handle.read(2)
        except OSError as exc:
            return f"Could not read file: {exc}"
        if header != b"MZ":  # the DOS/PE header every real Windows .exe starts with
            return "Not a valid Windows executable"
        return None
    if not os.access(path, os.X_OK):
        return "Not an executable file (missing execute permission)"
    try:
        with open(path, "rb") as file_handle:
            header = file_handle.read(4)
    except OSError as exc:
        return f"Could not read file: {exc}"
    # A native binary (ELF) or a launcher script -- distro packages commonly
    # ship the browser itself as a `#!/bin/sh` wrapper.
    if not (header == b"\x7fELF" or header[:2] == b"#!"):
        return "Not a valid Linux executable"
    return None


def default_browser_path() -> str | None:
    for browser in detect_installed_browsers():
        if browser["name"] == "Firefox":
            return browser["path"]
    return None


def open_url(url: str, browser_path: str | None) -> bool:
    """Launches `url` in `browser_path` if it's a real executable, otherwise
    falls back to the OS-registered default browser. Never shells out --
    `url` (attacker-influenced: it's a path discovered on the pentest
    target) is always a single argv element, never shell-interpreted.

    Returns whether something was actually launched. `webbrowser.open()`
    doesn't raise when nothing is registered to handle it (a real
    possibility on a locked-down pentest VM) -- it just returns False and
    silently does nothing, which the caller needs to know about instead of
    reporting success anyway.
    """
    _allow_foreground_steal()
    if browser_path and Path(browser_path).is_file():
        subprocess.Popen([browser_path, url])
        return True
    return webbrowser.open(url)


def open_with_system(path: str) -> None:
    """Opens a local file with the program the system associates with it, in front of other windows.
    `path` is built by this app (never taken from a request), and is always a single argument."""
    _allow_foreground_steal()
    if IS_WINDOWS:
        os.startfile(path)  # noqa: S606
    else:
        subprocess.Popen(["xdg-open", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def show_in_folder(path: str) -> None:
    """Shows a local file in the system's file manager (selected, on Windows)."""
    _allow_foreground_steal()
    if IS_WINDOWS:
        subprocess.Popen(["explorer", f"/select,{path}"])
    else:
        subprocess.Popen(["xdg-open", str(Path(path).parent)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
