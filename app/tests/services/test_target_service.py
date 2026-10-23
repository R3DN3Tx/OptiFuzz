import pytest

from backend.persistence import layout
from backend.services import project_service as ps
from backend.services import target_service as ts


def test_create_and_get_target(workspace_root) -> None:
    project = ps.create_project("Client A")

    target = ts.create_target(project.id, "www.example.com", "https://www.example.com/")

    fetched = ts.get_target(project.id, target.id)
    assert fetched == target
    assert fetched.project_id == project.id


def test_create_target_pre_creates_isolated_subtree(workspace_root) -> None:
    project = ps.create_project("Client A")

    target = ts.create_target(project.id, "www.example.com", "https://www.example.com/")

    assert layout.jobs_dir(project.id, target.id).is_dir()
    assert layout.outputs_dir(project.id, target.id).is_dir()
    assert layout.sessions_dir(project.id, target.id).is_dir()


def test_create_target_for_missing_project_raises(workspace_root) -> None:
    with pytest.raises(ps.ProjectNotFoundError):
        ts.create_target("does-not-exist", "www.example.com", "https://www.example.com/")


def test_duplicate_target_name_within_same_project_is_rejected(workspace_root) -> None:
    project = ps.create_project("Client A")
    ts.create_target(project.id, "www.example.com", "https://www.example.com/")

    with pytest.raises(ts.DuplicateTargetNameError):
        ts.create_target(project.id, "WWW.EXAMPLE.COM", "https://other/")


def test_same_target_name_allowed_across_different_projects(workspace_root) -> None:
    project_a = ps.create_project("Client A")
    project_b = ps.create_project("Client B")

    target_a = ts.create_target(project_a.id, "api", "https://a.example.com/")
    target_b = ts.create_target(project_b.id, "api", "https://b.example.com/")

    assert target_a.id != target_b.id


def test_rename_target(workspace_root) -> None:
    project = ps.create_project("Client A")
    target = ts.create_target(project.id, "www.example.com", "https://www.example.com/")

    renamed = ts.rename_target(project.id, target.id, "www-renamed")

    assert renamed.id == target.id
    assert ts.get_target(project.id, target.id).name == "www-renamed"


def test_delete_target_removes_it(workspace_root) -> None:
    project = ps.create_project("Client A")
    target = ts.create_target(project.id, "www.example.com", "https://www.example.com/")

    ts.delete_target(project.id, target.id)

    with pytest.raises(ts.TargetNotFoundError):
        ts.get_target(project.id, target.id)


def test_outputs_never_cross_contaminate_between_targets(workspace_root) -> None:
    # Direct implementation of a result written for one target's
    # outputs must never be visible from another target/project's outputs dir.
    project_a = ps.create_project("Client A")
    project_b = ps.create_project("Client B")

    www = ts.create_target(project_a.id, "www.example.com", "https://www.example.com/")
    api = ts.create_target(project_a.id, "api.example.com", "https://api.example.com/")
    api_es = ts.create_target(project_b.id, "api.example.es", "https://api.example.es/")

    (layout.outputs_dir(project_a.id, www.id) / "job_1.txt").write_text("www result")
    (layout.outputs_dir(project_a.id, api.id) / "job_2.txt").write_text("api result")
    (layout.outputs_dir(project_b.id, api_es.id) / "job_3.txt").write_text("api.es result")

    assert [p.name for p in layout.outputs_dir(project_a.id, www.id).iterdir()] == ["job_1.txt"]
    assert [p.name for p in layout.outputs_dir(project_a.id, api.id).iterdir()] == ["job_2.txt"]
    assert [p.name for p in layout.outputs_dir(project_b.id, api_es.id).iterdir()] == ["job_3.txt"]


def test_normalize_fuzz_path_stores_the_directory_dirsearch_really_fuzzes() -> None:
    from backend.services.target_service import normalize_fuzz_path

    assert normalize_fuzz_path("/get-sdk/cookies_sdk/app") == "/get-sdk/cookies_sdk/app/"
    assert normalize_fuzz_path("app") == "/app/"
    assert normalize_fuzz_path("/app/") == "/app/"
    assert normalize_fuzz_path("/") == "/"
    assert normalize_fuzz_path("") == "/"
    assert normalize_fuzz_path("/gs/htc?a=a") == "/gs/htc/?a=a"
    assert normalize_fuzz_path("/gs/htc/?a=a") == "/gs/htc/?a=a"
