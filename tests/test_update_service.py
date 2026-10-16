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
    (origin / "VERSION").write_text("1.0.0\n")
    (origin / "requirements.txt").write_text("")
    _git(origin, "add", "-A")
    _git(origin, "commit", "-m", "v1.0.0")
    local = tmp_path / "local"
    subprocess.run(["git", "clone", str(origin), str(local)], check=True, capture_output=True)
    monkeypatch.setattr(update_service, "REPO_ROOT", local)
    return origin, local


def _release(origin: Path, version: str) -> None:
    (origin / "VERSION").write_text(version + "\n")
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
    assert (local / "VERSION").read_text().strip() == "1.0.1"
    assert (local / "my_wordlist.txt").exists()


def test_apply_refuses_when_tracked_files_were_modified(clone) -> None:
    origin, local = clone
    _release(origin, "1.0.1")
    (local / "requirements.txt").write_text("# edited\n")
    assert update_service.check()["can_update"] is False
    with pytest.raises(update_service.UpdateError):
        update_service.apply()
    assert (local / "VERSION").read_text().strip() == "1.0.0"


def test_not_a_git_checkout_is_a_clear_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(update_service, "REPO_ROOT", tmp_path)
    with pytest.raises(update_service.UpdateError, match="not installed with git"):
        update_service.check()

