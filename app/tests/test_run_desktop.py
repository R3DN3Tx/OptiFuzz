import socket
import sys
from pathlib import Path

import pytest

import run_desktop


class _FakeWindow:
    """Stands in for the pywebview window: its Save As dialog 'picks' a fixed path."""

    def __init__(self, chosen: Path | None) -> None:
        self.chosen = chosen
        self.asked_for: str | None = None

    def create_file_dialog(self, _kind, directory="", save_filename=""):
        self.asked_for = save_filename
        return str(self.chosen) if self.chosen else None


def _serve_an_export(body: bytes):
    import http.server
    import threading

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Disposition", 'attachment; filename="demo_OptiFuzz_Report.html"')
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args) -> None:
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def test_save_download_saves_an_export_where_the_dialog_says(tmp_path: Path) -> None:
    # Reported: the web view's own download popup opened outside the window on another screen, so the
    # desktop app saves exports itself (Save As over the window) and shows its own notice in the page.
    server = _serve_an_export(b"<html>report</html>")
    try:
        api = run_desktop._Api()
        api._window = _FakeWindow(tmp_path / "chosen.html")
        saved = api.save_download(f"http://127.0.0.1:{server.server_address[1]}/api/x/export")

        assert api._window.asked_for == "demo_OptiFuzz_Report.html"  # the name the server proposed
        assert saved == {"path": str(tmp_path / "chosen.html"), "name": "chosen.html"}
        assert (tmp_path / "chosen.html").read_bytes() == b"<html>report</html>"

        api._window = _FakeWindow(None)  # the user cancels the dialog
        assert api.save_download(f"http://127.0.0.1:{server.server_address[1]}/api/x/export") is None
    finally:
        server.shutdown()


def test_save_download_refuses_anything_that_is_not_this_apps_api(tmp_path: Path) -> None:
    api = run_desktop._Api()
    api._window = _FakeWindow(tmp_path / "x.html")

    assert "error" in api.save_download("http://example.com/api/x")
    assert "error" in api.save_download("http://127.0.0.1:1/not-api")
    assert "error" in api.save_download("file:///etc/passwd")
    assert not (tmp_path / "x.html").exists()


def test_open_and_show_download_only_act_on_files_this_app_saved(tmp_path: Path) -> None:
    stranger = tmp_path / "other.html"
    stranger.write_text("x")
    api = run_desktop._Api()

    with pytest.raises(ValueError):
        api.open_download(str(stranger))
    with pytest.raises(ValueError):
        api.show_download(str(stranger))


def _powershell(script: str, env: dict) -> str:
    import subprocess

    done = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        env=env, capture_output=True, text=True, timeout=60,
    )
    return done.stdout.strip()


@pytest.mark.skipif(sys.platform != "win32", reason="Start menu shortcuts are Windows-only")
def test_start_menu_shortcut_icon_follows_the_theme_icon_but_only_for_this_copy(tmp_path: Path) -> None:
    # Reported: the app list of the Start menu showed the white-ink icon in light mode (the shortcut keeps one
    # static icon). The app re-points ITS OWN shortcut at the icon matching the theme at every start.
    import os

    mine, other = tmp_path / "mine", tmp_path / "other"
    mine.mkdir()
    other.mkdir()
    lnk = tmp_path / "OptiFuzz.lnk"
    dark, light = tmp_path / "icon-dark.ico", tmp_path / "icon-light.ico"
    make = (
        "$s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:LNK); "
        "$s.TargetPath = $env:ComSpec; $s.WorkingDirectory = $env:WORKDIR; $s.IconLocation = $env:ICON + ',0'; $s.Save()"
    )
    read = "(New-Object -ComObject WScript.Shell).CreateShortcut($env:LNK).IconLocation"
    base = dict(os.environ, LNK=str(lnk))

    _powershell(make, dict(base, WORKDIR=str(mine), ICON=str(dark)))
    refresh = dict(base, OPTIFUZZ_LNK=str(lnk), OPTIFUZZ_ICON=str(light), OPTIFUZZ_ROOT=str(mine))
    _powershell(run_desktop._REFRESH_SHORTCUT_ICON_SCRIPT, refresh)
    assert _powershell(read, base) == f"{light},0"  # light mode: the dark-ink icon

    # A shortcut that belongs to another copy of the program is left alone.
    _powershell(make, dict(base, WORKDIR=str(other), ICON=str(dark)))
    _powershell(run_desktop._REFRESH_SHORTCUT_ICON_SCRIPT, refresh)
    assert _powershell(read, base) == f"{dark},0"


def test_free_port_returns_a_bindable_port() -> None:
    port = run_desktop._free_port(0)

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", port))  # raises OSError if already in use


def test_free_port_prefers_the_given_port_when_available() -> None:
    # Real, reported bug this exists to prevent: a random port every launch
    # meant the app's own origin (http://127.0.0.1:<port>) changed every
    # restart, and localStorage (starred/last-used wordlist, etc.) is
    # partitioned by origin including the port -- so it silently never
    # carried over, regardless of the WebView2 profile-directory fix.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        free_port = probe.getsockname()[1]

    assert run_desktop._free_port(free_port) == free_port


def test_free_port_falls_back_when_the_preferred_port_is_taken() -> None:
    holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    holder.bind(("127.0.0.1", 0))
    holder.listen(1)
    taken_port = holder.getsockname()[1]
    try:
        got = run_desktop._free_port(taken_port)
        assert got != taken_port
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", got))  # raises OSError if somehow still in use
    finally:
        holder.close()


def test_wait_until_listening_returns_once_something_is_listening() -> None:
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]
    try:
        run_desktop._wait_until_listening(port, timeout=2.0)
    finally:
        server.close()


def test_wait_until_listening_times_out_when_nothing_listens() -> None:
    port = run_desktop._free_port(0)

    with pytest.raises(RuntimeError):
        run_desktop._wait_until_listening(port, timeout=0.3)


def test_colorref_matches_win32_bbggrr_byte_order() -> None:
    # styles.css's --panel, #171a21 -- COLORREF packs it as 0x00BBGGRR, the
    # reverse of the usual #RRGGBB, so this is exactly the kind of thing
    # worth pinning down with a test rather than trusting by eye.
    assert run_desktop._colorref("#171a21") == 0x00211A17


def test_colorref_accepts_hash_or_bare_hex() -> None:
    assert run_desktop._colorref("#ffffff") == run_desktop._colorref("ffffff")
    assert run_desktop._colorref("#000000") == 0


def test_select_icon_path_prefers_dark_shell_icon_when_theme_unknown_or_dark(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dark_icon = tmp_path / "dark.ico"
    light_icon = tmp_path / "light.ico"
    dark_icon.write_bytes(b"")
    light_icon.write_bytes(b"")
    monkeypatch.setattr(run_desktop, "_ICON_FOR_DARK_SHELL", dark_icon)
    monkeypatch.setattr(run_desktop, "_ICON_FOR_LIGHT_SHELL", light_icon)

    monkeypatch.setattr(run_desktop, "_system_uses_light_theme", lambda: False)
    assert run_desktop._select_icon_path() == dark_icon

    monkeypatch.setattr(run_desktop, "_system_uses_light_theme", lambda: None)
    assert run_desktop._select_icon_path() == dark_icon


def test_select_icon_path_prefers_light_shell_icon_when_theme_is_light(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dark_icon = tmp_path / "dark.ico"
    light_icon = tmp_path / "light.ico"
    dark_icon.write_bytes(b"")
    light_icon.write_bytes(b"")
    monkeypatch.setattr(run_desktop, "_ICON_FOR_DARK_SHELL", dark_icon)
    monkeypatch.setattr(run_desktop, "_ICON_FOR_LIGHT_SHELL", light_icon)
    monkeypatch.setattr(run_desktop, "_system_uses_light_theme", lambda: True)

    assert run_desktop._select_icon_path() == light_icon


def test_select_icon_path_falls_back_to_whichever_variant_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    only_icon = tmp_path / "only.ico"
    only_icon.write_bytes(b"")
    missing = tmp_path / "missing.ico"
    monkeypatch.setattr(run_desktop, "_ICON_FOR_DARK_SHELL", missing)
    monkeypatch.setattr(run_desktop, "_ICON_FOR_LIGHT_SHELL", only_icon)
    monkeypatch.setattr(run_desktop, "_system_uses_light_theme", lambda: False)

    assert run_desktop._select_icon_path() == only_icon


def test_select_icon_path_returns_none_when_neither_variant_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(run_desktop, "_ICON_FOR_DARK_SHELL", tmp_path / "nope1.ico")
    monkeypatch.setattr(run_desktop, "_ICON_FOR_LIGHT_SHELL", tmp_path / "nope2.ico")
    monkeypatch.setattr(run_desktop, "_system_uses_light_theme", lambda: True)

    assert run_desktop._select_icon_path() is None


def test_effective_zoom_scale_prefers_the_setting_over_the_automatic_one() -> None:
    assert run_desktop._effective_zoom_scale(125, 1.5) == 1.25
    assert run_desktop._effective_zoom_scale(None, 1.5) == 1.5
    assert run_desktop._effective_zoom_scale(None, None) is None


def test_title_bar_icon_is_the_dark_ink_variant() -> None:
    # icon-light is the black-ink one: it blends into the forced-dark title bar instead of
    # repeating the light logo of the app's own header.
    assert run_desktop._TITLE_BAR_ICON.name == "icon-light.ico"
    assert run_desktop._TITLE_BAR_ICON.is_file()


def test_set_small_icon_reports_a_missing_icon_file(tmp_path: Path) -> None:
    assert run_desktop._set_small_icon(0, tmp_path / "missing.ico") is False


@pytest.mark.skipif(sys.platform != "win32", reason="Win32 title bar icon")
def test_set_small_icon_sets_the_icon_on_a_window_that_is_never_shown() -> None:
    import ctypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.CreateWindowExW.restype = ctypes.c_void_p
    user32.CreateWindowExW.argtypes = [
        ctypes.c_uint, ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint, ctypes.c_int, ctypes.c_int,
        ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
    ]
    user32.SendMessageW.restype = ctypes.c_void_p
    user32.SendMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p]
    user32.DestroyWindow.argtypes = [ctypes.c_void_p]
    hwnd = user32.CreateWindowExW(0, "STATIC", "icon test", 0, 0, 0, 10, 10, None, None, None, None)
    assert hwnd  # style 0: no WS_VISIBLE, so nothing appears on the desktop
    try:
        assert not user32.SendMessageW(hwnd, 0x007F, 0, 0)  # WM_GETICON, small: none yet
        assert run_desktop._set_small_icon(hwnd, run_desktop._TITLE_BAR_ICON) is True
        assert user32.SendMessageW(hwnd, 0x007F, 0, 0)  # WM_GETICON, small: now set
    finally:
        user32.DestroyWindow(hwnd)
