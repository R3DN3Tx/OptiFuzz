from __future__ import annotations

from backend.models.folder import Folder
from backend.models.timestamps import now_iso
from backend.persistence import layout
from backend.persistence.atomic import DamagedFileError, load_model, write_json_atomic
from backend.persistence.slug import clean_name, make_id
from backend.services import project_service


class FolderNotFoundError(Exception):
    pass


class DuplicateFolderNameError(Exception):
    pass


class FolderNotEmptyError(Exception):
    pass


def list_folders() -> list[Folder]:
    folders_dir = layout.folders_dir()
    if not folders_dir.is_dir():
        return []
    folders = []
    for entry in sorted(folders_dir.glob("*.json")):
        try:
            folders.append(load_model(entry, Folder.from_dict))
        except DamagedFileError:
            continue  # its projects are shown at the top level meanwhile (the frontend treats a missing folder so)
    return folders


def get_folder(folder_id: str) -> Folder:
    path = layout.folder_file(folder_id)
    if not path.is_file():
        raise FolderNotFoundError(folder_id)
    return load_model(path, Folder.from_dict)


def folder_exists(folder_id: str) -> bool:
    return bool(folder_id) and layout.folder_file(folder_id).is_file()


def _check_parent(parent_id: str) -> None:
    if parent_id and not folder_exists(parent_id):
        raise FolderNotFoundError(parent_id)


def _check_unique(name: str, parent_id: str, ignore_id: str = "") -> None:
    if any(
        f.id != ignore_id and f.parent_id == parent_id and f.name.strip().lower() == name.lower()
        for f in list_folders()
    ):
        raise DuplicateFolderNameError(f"A folder named {name!r} already exists here")


def create_folder(name: str, parent_id: str = "") -> Folder:
    name = clean_name(name, "Folder")
    _check_parent(parent_id)
    _check_unique(name, parent_id)
    folder = Folder(id=make_id(name), name=name, created_at=now_iso(), parent_id=parent_id)
    write_json_atomic(layout.folder_file(folder.id), folder.to_dict())
    return folder


def rename_folder(folder_id: str, new_name: str) -> Folder:
    new_name = clean_name(new_name, "Folder")
    folder = get_folder(folder_id)
    _check_unique(new_name, folder.parent_id, ignore_id=folder_id)
    folder.name = new_name
    write_json_atomic(layout.folder_file(folder_id), folder.to_dict())
    return folder


def _is_inside(folder_id: str, ancestor_id: str) -> bool:
    """True when `folder_id` is `ancestor_id` or lies anywhere below it."""
    by_id = {f.id: f for f in list_folders()}
    seen: set[str] = set()
    current = folder_id
    while current and current not in seen:
        if current == ancestor_id:
            return True
        seen.add(current)
        node = by_id.get(current)
        current = node.parent_id if node else ""
    return False


def move_folder(folder_id: str, parent_id: str) -> Folder:
    folder = get_folder(folder_id)
    _check_parent(parent_id)
    if parent_id and _is_inside(parent_id, folder_id):
        raise ValueError("A folder cannot be moved into itself or into one of its own subfolders")
    _check_unique(folder.name, parent_id, ignore_id=folder_id)
    folder.parent_id = parent_id
    write_json_atomic(layout.folder_file(folder_id), folder.to_dict())
    return folder


def delete_folder(folder_id: str) -> None:
    get_folder(folder_id)
    if any(f.parent_id == folder_id for f in list_folders()) or any(
        p.folder_id == folder_id for p in project_service.list_projects()
    ):
        raise FolderNotEmptyError("The folder is not empty: move or delete what is inside it first.")
    path = layout.folder_file(folder_id)
    path.unlink()
    path.with_name(path.name + ".bak").unlink(missing_ok=True)
