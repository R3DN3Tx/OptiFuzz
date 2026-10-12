import sys
from pathlib import Path

import pytest

from backend.services import browser_service

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows executable validation")
linux_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX executable validation")


def test_detect_installed_browsers_only_returns_existing_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_firefox = tmp_path / "firefox.exe"
    fake_firefox.write_text("")
    monkeypatch.setattr(
        browser_service,
        "_CANDIDATE_PATHS",
        {
            "Firefox": (str(fake_firefox),),
            "Ghost Browser": (str(tmp_path / "does-not-exist.exe"),),
        },
    )

    found = browser_service.detect_installed_browsers()

    assert found == [{"name": "Firefox", "path": str(fake_firefox)}]


def test_default_browser_path_prefers_firefox_over_others(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_firefox = tmp_path / "firefox.exe"
    fake_firefox.write_text("")
    fake_chrome = tmp_path / "chrome.exe"
    fake_chrome.write_text("")
    monkeypatch.setattr(
        browser_service,
        "_CANDIDATE_PATHS",
        {"Google Chrome": (str(fake_chrome),), "Firefox": (str(fake_firefox),)},
    )

    assert browser_service.default_browser_path() == str(fake_firefox)


def test_default_browser_path_none_when_nothing_detected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        browser_service, "_CANDIDATE_PATHS", {"Firefox": (str(tmp_path / "nope.exe"),)}
    )
    # Firefox really is installed on some machines -- neutralize the
    # registry fallback too, or this becomes a test of the real machine's
    # state instead of a test of "nothing found anywhere".
    monkeypatch.setattr(browser_service, "_lookup_browser_path", lambda exe: None)

    assert browser_service.default_browser_path() is None


def test_open_url_launches_the_given_executable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_browser = tmp_path / "browser.exe"
    fake_browser.write_text("")
    calls = []
    monkeypatch.setattr(browser_service.subprocess, "Popen", lambda argv: calls.append(argv))

    result = browser_service.open_url("https://example.com/admin", str(fake_browser))

    assert calls == [[str(fake_browser), "https://example.com/admin"]]
    assert result is True


def test_open_url_falls_back_to_os_default_when_no_path_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []
    monkeypatch.setattr(browser_service.webbrowser, "open", lambda url: calls.append(url) or True)

    result = browser_service.open_url("https://example.com/admin", None)

    assert calls == ["https://example.com/admin"]
    assert result is True


def test_open_url_falls_back_when_configured_path_does_not_exist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = []
    monkeypatch.setattr(browser_service.webbrowser, "open", lambda url: calls.append(url) or True)

    result = browser_service.open_url("https://example.com/x", str(tmp_path / "missing.exe"))

    assert calls == ["https://example.com/x"]
    assert result is True


def test_open_url_reports_failure_when_nothing_can_be_launched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # webbrowser.open() doesn't raise when nothing is registered to handle
    # it -- it just returns False, which open_url() must propagate rather
    # than reporting success regardless.
    monkeypatch.setattr(browser_service.webbrowser, "open", lambda url: False)

    result = browser_service.open_url("https://example.com/x", None)

    assert result is False


def test_detect_installed_browsers_falls_back_to_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No hardcoded candidate exists on disk, but the registry "finds" it --
    # covers a browser installed somewhere other than the standard folder.
    fake_firefox = tmp_path / "firefox.exe"
    fake_firefox.write_text("")
    monkeypatch.setattr(
        browser_service, "_CANDIDATE_PATHS", {"Firefox": (str(tmp_path / "does-not-exist.exe"),)}
    )
    monkeypatch.setattr(browser_service, "_lookup_browser_path", lambda exe: str(fake_firefox))

    assert browser_service.detect_installed_browsers() == [
        {"name": "Firefox", "path": str(fake_firefox)}
    ]


@windows_only
def test_validate_browser_executable_accepts_a_real_pe_header(tmp_path: Path) -> None:
    exe = tmp_path / "browser.exe"
    exe.write_bytes(b"MZ" + b"\x00" * 62)

    assert browser_service.validate_browser_executable(str(exe)) is None


def test_validate_browser_executable_rejects_missing_file(tmp_path: Path) -> None:
    reason = browser_service.validate_browser_executable(str(tmp_path / "nope.exe"))
    assert reason == "File not found"


@windows_only
def test_validate_browser_executable_rejects_non_exe_extension(tmp_path: Path) -> None:
    not_exe = tmp_path / "browser.txt"
    not_exe.write_bytes(b"MZ" + b"\x00" * 62)

    reason = browser_service.validate_browser_executable(str(not_exe))
    assert reason == "Not a .exe file"


@windows_only
def test_validate_browser_executable_rejects_fake_content(tmp_path: Path) -> None:
    fake = tmp_path / "browser.exe"
    fake.write_text("not a real executable")

    reason = browser_service.validate_browser_executable(str(fake))
    assert reason == "Not a valid Windows executable"


def test_validate_browser_executable_rejects_missing_file_anywhere(tmp_path: Path) -> None:
    assert browser_service.validate_browser_executable(str(tmp_path / "nope")) == "File not found"


@linux_only
def test_validate_browser_executable_accepts_elf_binary(tmp_path: Path) -> None:
    exe = tmp_path / "browser"
    exe.write_bytes(b"\x7fELF" + b"\x00" * 60)
    exe.chmod(0o755)

    assert browser_service.validate_browser_executable(str(exe)) is None


@linux_only
def test_validate_browser_executable_accepts_shebang_wrapper(tmp_path: Path) -> None:
    exe = tmp_path / "firefox"
    exe.write_text("#!/bin/sh\nexec true\n")
    exe.chmod(0o755)

    assert browser_service.validate_browser_executable(str(exe)) is None


@linux_only
def test_validate_browser_executable_rejects_non_executable_file(tmp_path: Path) -> None:
    exe = tmp_path / "browser"
    exe.write_bytes(b"\x7fELF" + b"\x00" * 60)
    exe.chmod(0o644)

    reason = browser_service.validate_browser_executable(str(exe))
    assert reason is not None and "execute permission" in reason


@linux_only
def test_validate_browser_executable_rejects_fake_content_on_linux(tmp_path: Path) -> None:
    fake = tmp_path / "browser"
    fake.write_text("not a real executable")
    fake.chmod(0o755)

    assert browser_service.validate_browser_executable(str(fake)) == "Not a valid Linux executable"


@linux_only
def test_lookup_browser_path_uses_path_lookup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = tmp_path / "firefox"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))

    assert browser_service._lookup_browser_path("firefox") == str(fake)
    assert browser_service._lookup_browser_path("definitely-not-a-browser") is None


@linux_only
def test_linux_search_path_skips_windows_drives_under_wsl(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", "/usr/bin:/mnt/c/Windows:/bin:/mnt/c/Program Files/Git/cmd")
    monkeypatch.setenv("WSL_DISTRO_NAME", "kali-linux")

    assert browser_service._linux_search_path() == "/usr/bin:/bin"


@linux_only
def test_linux_search_path_is_untouched_outside_wsl(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", "/usr/bin:/mnt/data/bin")
    monkeypatch.delenv("WSL_DISTRO_NAME", raising=False)

    assert browser_service._linux_search_path() == "/usr/bin:/mnt/data/bin"
