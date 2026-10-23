import sys
from pathlib import Path

import pytest

from backend.models.job import FuzzJob, JobStatus
from backend.persistence import layout
from backend.persistence.paths import workspace_root
from backend.persistence.slug import make_id, slugify, validate_safe_id


def test_workspace_root_honors_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPTIFUZZ_WORKSPACE_ROOT", r"C:\tmp\optifuzz-test-root")

    assert str(workspace_root()) == r"C:\tmp\optifuzz-test-root"


windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows-specific behavior")
linux_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX-specific behavior")


@windows_only
def test_workspace_root_defaults_under_appdata_on_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPTIFUZZ_WORKSPACE_ROOT", raising=False)
    monkeypatch.setenv("APPDATA", r"C:\Users\someone\AppData\Roaming")

    root = workspace_root()

    assert root == Path(r"C:\Users\someone\AppData\Roaming") / "OptiFuzz" / "workspace"


@linux_only
def test_workspace_root_defaults_under_xdg_data_home_on_linux(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPTIFUZZ_WORKSPACE_ROOT", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", "/home/someone/.xdg-data")

    assert workspace_root() == Path("/home/someone/.xdg-data/OptiFuzz/workspace")


@linux_only
def test_workspace_root_falls_back_to_local_share_on_linux(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPTIFUZZ_WORKSPACE_ROOT", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)

    assert workspace_root() == Path.home() / ".local" / "share" / "OptiFuzz" / "workspace"


def test_slugify_basic() -> None:
    assert slugify("Client A") == "client-a"
    assert slugify("  api.example.com  ") == "api-example-com"


def test_slugify_rejects_empty_result() -> None:
    with pytest.raises(ValueError):
        slugify("***")


def test_make_id_is_slug_plus_unique_suffix() -> None:
    first = make_id("Client A")
    second = make_id("Client A")

    assert first.startswith("client-a-")
    assert first != second


def test_validate_safe_id_rejects_traversal() -> None:
    for bad in ("..", ".", "../secret", "a/b", "a\\b", "C:\\evil", ""):
        with pytest.raises(ValueError):
            validate_safe_id(bad)


def test_validate_safe_id_accepts_make_id_output() -> None:
    generated = make_id("Client A")

    assert validate_safe_id(generated) == generated


def test_layout_paths_are_nested_and_isolated(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("OPTIFUZZ_WORKSPACE_ROOT", str(tmp_path))

    p1, t1 = "client-a-aaaaaaaa", "www-example-com-bbbbbbbb"
    p1b, t2 = "client-a-aaaaaaaa", "api-example-com-cccccccc"

    out1 = layout.outputs_dir(p1, t1)
    out2 = layout.outputs_dir(p1b, t2)

    assert out1 != out2
    assert str(tmp_path) in str(out1)
    assert t1 in str(out1) and t1 not in str(out2)


def test_layout_rejects_traversal_in_ids(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("OPTIFUZZ_WORKSPACE_ROOT", str(tmp_path))

    with pytest.raises(ValueError):
        layout.target_dir("client-a", "../../etc")


def test_job_output_base_for_picks_the_right_naming_generation(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("OPTIFUZZ_WORKSPACE_ROOT", str(tmp_path))
    p, t, job_id = "client-a-aaaaaaaa", "www-example-com-bbbbbbbb", "abc123"

    # No stem at all: oldest jobs, flat with no human-readable name.
    legacy = layout.job_output_base_for(p, t, job_id, "", False)
    assert legacy == layout.job_output_base_legacy(p, t, job_id)
    assert legacy.parent == layout.outputs_dir(p, t)

    # Stem but not flat: the (now superseded) nested-per-job generation --
    # still resolvable for jobs already created this way.
    nested = layout.job_output_base_for(p, t, job_id, "dirsearch_admin", False)
    assert nested == layout.outputs_dir(p, t) / job_id / "dirsearch_admin"

    # Stem and flat: current generation -- outputs/<stem>_<job_id>, no
    # per-job subfolder (explicit user request).
    flat = layout.job_output_base_for(p, t, job_id, "dirsearch_admin", True)
    assert flat == layout.outputs_dir(p, t) / f"dirsearch_admin_{job_id}"
    assert flat.parent == layout.outputs_dir(p, t)


def _job(job_id: str, status: JobStatus, output_stem: str = "dirsearch_admin", completed_lines: int = 0) -> FuzzJob:
    return FuzzJob(
        id=job_id, target_id="t1", project_id="p1", path="/admin",
        config={"url": "https://target.local/"}, status=status,
        created_at="2026-01-01T00:00:00+00:00",
        output_stem=output_stem, output_flat=True,
        completed_lines=completed_lines,
    )


def test_job_output_base_working_is_short_and_job_id_based(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("OPTIFUZZ_WORKSPACE_ROOT", str(tmp_path))
    p, t, job_id = "client-a-aaaaaaaa", "www-example-com-bbbbbbbb", "abc123def456"

    working = layout.job_output_base_working(p, t, job_id)

    assert working.name == f"_working_{job_id}"
    assert working.parent == layout.outputs_dir(p, t)


# job_output_base_current: real, reported MAX_PATH bug -- dirsearch writes
# to the short working name while a job might still be appended to, renamed
# to the full name once it's terminal (JobManager._finalize_output_filename).
# This is the READ side every caller (Results, mirror, delete) should use
# instead of job_output_base_for directly.

def test_job_output_base_current_prefers_the_final_name_when_it_already_exists(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setenv("OPTIFUZZ_WORKSPACE_ROOT", str(tmp_path))
    job = _job("abc123", JobStatus.COMPLETED)
    final_base = layout.job_output_base_for("p1", "t1", job.id, job.output_stem, job.output_flat)
    final_base.parent.mkdir(parents=True, exist_ok=True)
    (final_base.parent / f"{final_base.name}.txt").write_text("done", encoding="utf-8")

    resolved = layout.job_output_base_current("p1", "t1", job)

    assert resolved == final_base


def test_job_output_base_current_falls_back_to_the_working_name_while_active(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setenv("OPTIFUZZ_WORKSPACE_ROOT", str(tmp_path))
    job = _job("abc123", JobStatus.RUNNING)
    working_base = layout.job_output_base_working("p1", "t1", job.id)
    working_base.parent.mkdir(parents=True, exist_ok=True)
    (working_base.parent / f"{working_base.name}.txt").write_text("partial", encoding="utf-8")

    resolved = layout.job_output_base_current("p1", "t1", job)

    assert resolved == working_base


def test_job_output_base_current_self_heals_if_a_terminal_jobs_rename_never_happened(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    # A rename can fail (locked handle, permissions, ...) -- the job is
    # still terminal, its final name still doesn't exist, but its content
    # must stay discoverable at wherever it actually is.
    monkeypatch.setenv("OPTIFUZZ_WORKSPACE_ROOT", str(tmp_path))
    job = _job("abc123", JobStatus.COMPLETED)
    working_base = layout.job_output_base_working("p1", "t1", job.id)
    working_base.parent.mkdir(parents=True, exist_ok=True)
    (working_base.parent / f"{working_base.name}.txt").write_text("stuck", encoding="utf-8")

    resolved = layout.job_output_base_current("p1", "t1", job)

    assert resolved == working_base


@windows_only
def test_long_path_adds_the_extended_length_prefix_on_windows(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    target = tmp_path / "some" / "nested" / "file.txt"

    prefixed = layout.long_path(target)

    assert prefixed.startswith("\\\\?\\")
    assert prefixed.endswith("file.txt")


@linux_only
def test_long_path_is_a_noop_on_linux(tmp_path) -> None:
    assert layout.long_path(tmp_path / "x.txt") == str(tmp_path / "x.txt")


def test_long_path_does_not_double_prefix_an_already_prefixed_path(tmp_path) -> None:
    already = "\\\\?\\" + str(tmp_path.resolve())

    assert layout.long_path(Path(already)) == already


# The full long-path-safe I/O toolkit (paths.py), exercised together against
# ONE genuinely-long path (>260 chars) -- direct proof each helper actually
# works on this real filesystem, not just that it builds the right prefix
# string. A plain Path.is_file()/read_text()/write_text()/unlink() on a path
# this long reproducibly reports "doesn't exist" even once it's really
# there (see paths.py::long_path's own docstring) -- that's the bug these
# exist to fix.

def _windows_long_paths_disabled() -> bool:
    """True on a Windows machine where the system-wide long-path policy is off (the default on a
    desktop install). CI runners enable it, and there a plain Path.is_file() works past MAX_PATH."""
    if sys.platform != "win32":
        return False
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\FileSystem") as key:
            return winreg.QueryValueEx(key, "LongPathsEnabled")[0] != 1
    except OSError:
        return True


def _make_long_path(base: Path) -> Path:
    long_name = "segment-with-some-extra-length-padding-" * 6  # ~240 chars alone
    deep = base
    for part in ("a", "b"):
        deep = deep / part
    target = deep / f"{long_name}.txt"
    assert len(str(target)) > 260
    return target


def test_write_text_and_read_text_round_trip_a_genuinely_long_path(tmp_path: Path) -> None:
    target = _make_long_path(tmp_path)
    target.parent.mkdir(parents=True, exist_ok=True)

    layout.write_text(target, "200 /admin\n")

    assert layout.read_text(target) == "200 /admin\n"


def test_path_is_file_sees_a_file_written_via_write_text_at_a_long_path(tmp_path: Path) -> None:
    target = _make_long_path(tmp_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    layout.write_text(target, "hi")

    assert layout.path_is_file(target) is True
    # A plain Path.is_file() is exactly the bug this whole toolkit fixes --
    # it must NOT see the file, proving the test actually exercises the
    # MAX_PATH boundary rather than a short, coincidentally-fine path.
    if _windows_long_paths_disabled():  # MAX_PATH only bites on Windows without long paths enabled
        assert target.is_file() is False


def test_unlink_removes_a_file_at_a_long_path(tmp_path: Path) -> None:
    target = _make_long_path(tmp_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    layout.write_text(target, "hi")

    layout.unlink(target)

    assert layout.path_is_file(target) is False


def test_unlink_missing_ok_suppresses_only_file_not_found(tmp_path: Path) -> None:
    target = _make_long_path(tmp_path)

    layout.unlink(target, missing_ok=True)  # must not raise


def test_copy_file_copies_content_to_a_genuinely_long_destination(tmp_path: Path) -> None:
    src = tmp_path / "short_source.txt"
    src.write_text("copy me", encoding="utf-8")
    dst = _make_long_path(tmp_path)
    dst.parent.mkdir(parents=True, exist_ok=True)

    layout.copy_file(src, dst)

    assert layout.read_text(dst) == "copy me"
    assert src.is_file()  # the source is untouched by a copy


def test_move_file_moves_content_to_a_genuinely_long_destination(tmp_path: Path) -> None:
    src = tmp_path / "short_source.txt"
    src.write_text("move me", encoding="utf-8")
    dst = _make_long_path(tmp_path)
    dst.parent.mkdir(parents=True, exist_ok=True)

    layout.move_file(src, dst)

    assert layout.read_text(dst) == "move me"
    assert not src.exists()  # the source is gone after a move
