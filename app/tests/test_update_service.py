import subprocess
from pathlib import Path

import pytest

from backend.services import update_service


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd, check=True, capture_output=True
    )


@pytest.fixture
def clone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-b", "main")
    (origin / "app").mkdir()
    (origin / "app" / "VERSION").write_text("1.0.0\n")
    (origin / "app" / "requirements.txt").write_text("")
    _git(origin, "add", "-A")
    _git(origin, "commit", "-m", "v1.0.0")
    local = tmp_path / "local"
    subprocess.run(["git", "clone", str(origin), str(local)], check=True, capture_output=True)
    monkeypatch.setattr(update_service, "REPO_ROOT", local)
    return origin, local


def _release(origin: Path, version: str) -> None:
    (origin / "app" / "VERSION").write_text(version + "\n")
    _git(origin, "commit", "-am", f"v{version}")


def test_check_reports_up_to_date(clone) -> None:
    status = update_service.check()
    assert status["behind"] == 0 and status["current"] == "1.0.0" and not status["can_update"]


def test_check_and_apply_pull_a_newer_version(clone) -> None:
    origin, local = clone
    (local / "my_wordlist.txt").write_text("a\n")  # untracked user file: must survive
    _release(origin, "1.0.1")

    status = update_service.check()
    assert status["behind"] == 1 and status["latest"] == "1.0.1" and status["can_update"]

    result = update_service.apply()
    assert result["updated"] and result["current"] == "1.0.1" and result["restart_required"]
    assert (local / "app" / "VERSION").read_text().strip() == "1.0.1"
    assert (local / "my_wordlist.txt").exists()


def test_apply_refuses_when_tracked_files_were_modified(clone) -> None:
    origin, local = clone
    _release(origin, "1.0.1")
    (local / "app" / "requirements.txt").write_text("# edited\n")
    assert update_service.check()["can_update"] is False
    with pytest.raises(update_service.UpdateError):
        update_service.apply()
    assert (local / "app" / "VERSION").read_text().strip() == "1.0.0"


def test_not_a_git_checkout_is_a_clear_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(update_service, "REPO_ROOT", tmp_path)
    with pytest.raises(update_service.UpdateError, match="not installed with git"):
        update_service.check()



def test_pip_install_flags_isolate_the_linux_venv_from_system_packages(monkeypatch: pytest.MonkeyPatch) -> None:
    # An old system httpcore (Kali) reused by pip instead of a venv copy crashes dirsearch on Python 3.14.
    monkeypatch.setattr(update_service.sys, "platform", "linux")
    assert "--ignore-installed" in update_service._pip_install_flags()
    monkeypatch.setattr(update_service.sys, "platform", "win32")
    assert update_service._pip_install_flags() == []


def test_apply_runs_pip_with_those_flags(clone, monkeypatch: pytest.MonkeyPatch) -> None:
    origin, _local = clone
    _release(origin, "1.0.1")
    seen: list[list[str]] = []
    real_run = subprocess.run

    def spy(cmd, *args, **kwargs):
        if "pip" in cmd:
            seen.append(list(cmd))
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(update_service.subprocess, "run", spy)
    monkeypatch.setattr(update_service, "_pip_install_flags", lambda: ["--ignore-installed", "--no-warn-conflicts"])

    update_service.apply()

    (cmd,) = seen
    assert cmd[cmd.index("install") + 1:cmd.index("-r")] == ["--ignore-installed", "--no-warn-conflicts"]


# ---- automatic restart after an update --------------------------------------------------------


def test_restart_needs_the_desktop_app_and_a_fresh_update(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(update_service, "_restart_close_window", None)
    monkeypatch.setattr(update_service, "_restart_command", None)
    assert update_service.can_restart() is False
    with pytest.raises(update_service.UpdateError, match="desktop app"):
        update_service.restart()

    monkeypatch.setattr(update_service, "_restart_close_window", lambda: None)
    monkeypatch.setattr(update_service, "_restart_command", ["python", "run_desktop.py"])
    monkeypatch.setattr(update_service, "_updated_since_start", False)
    assert update_service.can_restart() is True
    with pytest.raises(update_service.UpdateError, match="no update"):
        update_service.restart()


def test_restart_starts_the_helper_then_closes_the_window(monkeypatch: pytest.MonkeyPatch) -> None:
    import threading

    closed = threading.Event()
    launched: list[list[str]] = []
    monkeypatch.setattr(update_service, "_restart_close_window", closed.set)
    monkeypatch.setattr(update_service, "_restart_command", ["python", "run_desktop.py"])
    monkeypatch.setattr(update_service, "_updated_since_start", True)
    monkeypatch.setattr(update_service, "_CLOSE_DELAY_SECONDS", 0.05)
    monkeypatch.setattr(update_service.subprocess, "Popen", lambda cmd, **kw: launched.append(cmd))

    update_service.restart()

    assert closed.wait(5)
    (cmd,) = launched
    assert cmd[1].endswith("relaunch.py") and cmd[2] == str(__import__("os").getpid())
    assert cmd[-2:] == ["python", "run_desktop.py"]


def _run_relaunch(pid: int, marker: Path) -> subprocess.Popen:
    import sys

    helper = Path(update_service.__file__).resolve().parent.parent / "relaunch.py"
    command = [sys.executable, "-c", f"open({str(marker)!r}, 'w').write('started')"]
    return subprocess.Popen([sys.executable, str(helper), str(pid), str(marker.parent), *command])


def _wait_for(path: Path, seconds: float) -> bool:
    import time

    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(0.1)
    return False


def test_relaunch_helper_starts_the_command_once_the_old_process_is_gone(tmp_path: Path) -> None:
    import sys
    import time

    old = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(2)"])
    marker = tmp_path / "started.txt"
    helper = _run_relaunch(old.pid, marker)
    try:
        time.sleep(0.7)
        assert not marker.exists(), "must wait while the old process is still running"
        assert _wait_for(marker, 20), "must start the command after the old process exits"
    finally:
        old.kill()
        helper.wait(timeout=20)
