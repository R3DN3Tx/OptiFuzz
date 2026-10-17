import socket
import sys
from pathlib import Path

import pytest

import run_desktop


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
