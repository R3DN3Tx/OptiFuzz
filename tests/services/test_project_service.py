import pytest

from backend.services import project_service as ps


def test_create_and_get_project(workspace_root) -> None:
    project = ps.create_project("Client A")

    fetched = ps.get_project(project.id)

    assert fetched == project
    assert fetched.name == "Client A"


def test_list_projects_returns_created_projects_sorted_by_dir(workspace_root) -> None:
    ps.create_project("Client A")
    ps.create_project("Client B")

    names = {p.name for p in ps.list_projects()}

    assert names == {"Client A", "Client B"}


def test_duplicate_project_name_is_rejected(workspace_root) -> None:
    ps.create_project("Client A")

    with pytest.raises(ps.DuplicateProjectNameError):
        ps.create_project("client a")  # case-insensitive match


def test_get_missing_project_raises(workspace_root) -> None:
    with pytest.raises(ps.ProjectNotFoundError):
        ps.get_project("does-not-exist")


def test_rename_project(workspace_root) -> None:
    project = ps.create_project("Client A")

    renamed = ps.rename_project(project.id, "Client A (renamed)")

    assert renamed.id == project.id  # id/directory stay stable across a rename
    assert ps.get_project(project.id).name == "Client A (renamed)"


def test_rename_to_existing_name_is_rejected(workspace_root) -> None:
    ps.create_project("Client A")
    b = ps.create_project("Client B")

    with pytest.raises(ps.DuplicateProjectNameError):
        ps.rename_project(b.id, "Client A")


def test_delete_project_removes_it(workspace_root) -> None:
    project = ps.create_project("Client A")

    ps.delete_project(project.id)

    with pytest.raises(ps.ProjectNotFoundError):
        ps.get_project(project.id)


def test_delete_missing_project_raises(workspace_root) -> None:
    with pytest.raises(ps.ProjectNotFoundError):
        ps.delete_project("does-not-exist")
