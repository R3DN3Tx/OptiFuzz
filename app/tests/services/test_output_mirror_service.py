from pathlib import Path

from backend.persistence import layout
from backend.services import output_mirror_service, project_service, target_service


def test_mirror_output_file_copies_into_a_slugified_per_target_subfolder(workspace_root, tmp_path: Path) -> None:
    project = project_service.create_project("Client A")
    target = target_service.create_target(project.id, "api.example.com/v1", "https://api.example.com/v1/")
    project = output_mirror_service.set_mirror_dir(project.id, str(tmp_path / "mirror"))

    txt_path = tmp_path / "dirsearch_root.txt"
    txt_path.write_text("200 /admin\n", encoding="utf-8")
    output_mirror_service.mirror_output_file(project, target.name, txt_path)

    mirrored = output_mirror_service.mirror_target_subdir(project, target.name) / "dirsearch_root.txt"
    assert mirrored.is_file()
    assert mirrored.read_text(encoding="utf-8") == "200 /admin\n"


def test_remove_mirrored_file_deletes_the_copy_without_touching_the_original(workspace_root, tmp_path: Path) -> None:
    project = project_service.create_project("Client A")
    target = target_service.create_target(project.id, "target1", "http://target1/")
    project = output_mirror_service.set_mirror_dir(project.id, str(tmp_path / "mirror"))

    txt_path = tmp_path / "dirsearch_root.txt"
    txt_path.write_text("200 /admin\n", encoding="utf-8")
    output_mirror_service.mirror_output_file(project, target.name, txt_path)
    mirrored = output_mirror_service.mirror_target_subdir(project, target.name) / "dirsearch_root.txt"
    assert mirrored.is_file()

    output_mirror_service.remove_mirrored_file(project, target.name, "dirsearch_root.txt")

    assert not mirrored.exists()
    assert txt_path.is_file()  # the primary copy is never touched


def test_set_mirror_dir_moves_already_mirrored_files_to_the_new_path(workspace_root, tmp_path: Path) -> None:
    project = project_service.create_project("Client A")
    target = target_service.create_target(project.id, "target1", "http://target1/")
    old_dir = tmp_path / "mirror_a"
    new_dir = tmp_path / "mirror_b"

    project = output_mirror_service.set_mirror_dir(project.id, str(old_dir))
    txt_path = tmp_path / "dirsearch_root.txt"
    txt_path.write_text("200 /admin\n", encoding="utf-8")
    output_mirror_service.mirror_output_file(project, target.name, txt_path)
    old_mirrored = output_mirror_service.mirror_target_subdir(project, target.name) / "dirsearch_root.txt"
    assert old_mirrored.is_file()

    project = output_mirror_service.set_mirror_dir(project.id, str(new_dir))

    new_mirrored = output_mirror_service.mirror_target_subdir(project, target.name) / "dirsearch_root.txt"
    assert new_mirrored.is_file()
    assert not old_mirrored.exists()
    assert not old_dir.is_dir()  # cleaned up once empty


def test_set_mirror_dir_never_touches_unrelated_content_in_the_old_directory(workspace_root, tmp_path: Path) -> None:
    # Real, reported bug: changing the mirror dir used to move EVERY child of
    # the old directory (then rmdir() it) -- if the pentester had pointed the
    # mirror at a folder they also kept other files in, those got swept up
    # and moved too, and the folder itself vanished from under them.
    project = project_service.create_project("Client A")
    target = target_service.create_target(project.id, "target1", "http://target1/")
    old_dir = tmp_path / "mirror_a"
    new_dir = tmp_path / "mirror_b"

    project = output_mirror_service.set_mirror_dir(project.id, str(old_dir))
    txt_path = tmp_path / "dirsearch_root.txt"
    txt_path.write_text("200 /admin\n", encoding="utf-8")
    output_mirror_service.mirror_output_file(project, target.name, txt_path)

    # Content that has nothing to do with OptiFuzz, already sitting in the
    # directory the pentester chose as their mirror root.
    unrelated_file = old_dir / "my_notes.txt"
    unrelated_file.write_text("do not touch", encoding="utf-8")
    unrelated_dir = old_dir / "some_other_tool_output"
    unrelated_dir.mkdir()
    (unrelated_dir / "data.csv").write_text("a,b,c\n", encoding="utf-8")

    project = output_mirror_service.set_mirror_dir(project.id, str(new_dir))

    new_mirrored = output_mirror_service.mirror_target_subdir(project, target.name) / "dirsearch_root.txt"
    assert new_mirrored.is_file()  # our own content still migrated
    assert unrelated_file.read_text(encoding="utf-8") == "do not touch"
    assert (unrelated_dir / "data.csv").is_file()
    assert old_dir.is_dir()  # never deleted -- unrelated content is still in it


def test_set_mirror_dir_backfills_already_completed_jobs_not_just_future_ones(
    workspace_root, tmp_path: Path
) -> None:
    # Real complaint: "changing the mirror path only moves stuff already in
    # the mirror -- fuzzes that finished BEFORE any mirror dir was ever
    # configured never show up there, only new ones do". Setting a mirror
    # dir (first time or changing it) must also backfill every target's
    # already-completed .txt reports straight from the primary outputs/
    # storage, not just carry over what happened to already be mirrored.
    project = project_service.create_project("Client A")
    target = target_service.create_target(project.id, "target1", "http://target1/")

    # A job that finished BEFORE any mirror dir existed -- lives only in the
    # primary outputs/ storage, never touched mirror_output_file() at all.
    outputs_dir = layout.outputs_dir(project.id, target.id)
    outputs_dir.mkdir(parents=True, exist_ok=True)
    (outputs_dir / "dirsearch_target1_root_abc123.txt").write_text("200 /admin\n", encoding="utf-8")
    (outputs_dir / "dirsearch_target1_root_abc123.json").write_text("{}", encoding="utf-8")
    # A legacy, nested-per-job layout output should also be found (rglob).
    nested = outputs_dir / "job2"
    nested.mkdir(parents=True, exist_ok=True)
    (nested / "dirsearch_target1_admin.txt").write_text("403 /secret\n", encoding="utf-8")

    project = output_mirror_service.set_mirror_dir(project.id, str(tmp_path / "mirror"))

    mirror_dir = output_mirror_service.mirror_target_subdir(project, target.name)
    assert (mirror_dir / "dirsearch_target1_root_abc123.txt").read_text(encoding="utf-8") == "200 /admin\n"
    assert (mirror_dir / "dirsearch_target1_admin.txt").read_text(encoding="utf-8") == "403 /secret\n"
    assert not (mirror_dir / "dirsearch_target1_root_abc123.json").exists()  # txt-only, by design


def test_default_mirror_dir_placeholder_points_at_the_projects_own_dir(workspace_root) -> None:
    project = project_service.create_project("Client A")
    placeholder = output_mirror_service.default_mirror_dir_placeholder(project.id)
    assert placeholder == str(layout.project_dir(project.id))
