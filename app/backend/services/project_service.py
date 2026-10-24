from __future__ import annotations

import shutil

from backend.models.project import Project
from backend.models.timestamps import now_iso
from backend.persistence import layout
from backend.persistence.atomic import DamagedFileError, load_model, write_json_atomic
from backend.persistence.slug import clean_name, make_id


class ProjectNotFoundError(Exception):
    pass


class DuplicateProjectNameError(Exception):
    pass


def create_project(name: str, notes: str = "") -> Project:
    name = clean_name(name, "Project")

    if any(existing.name.strip().lower() == name.lower() for existing in list_projects()):
        raise DuplicateProjectNameError(f"A project named {name!r} already exists")

    project = Project(id=make_id(name), name=name, created_at=now_iso(), notes=notes)
    write_json_atomic(layout.project_file(project.id), project.to_dict())
    return project


def list_projects() -> list[Project]:
    projects_dir = layout.projects_dir()
    if not projects_dir.is_dir():
        return []

    projects = []
    for entry in sorted(projects_dir.iterdir()):
        project_file = entry / "project.json"
        if project_file.is_file():
            try:
                projects.append(load_model(project_file, Project.from_dict))
            except DamagedFileError:
                continue  # listed by damage_service instead, so one bad file never hides the others
    return projects


def get_project(project_id: str) -> Project:
    path = layout.project_file(project_id)
    if not path.is_file():
        raise ProjectNotFoundError(project_id)
    return load_model(path, Project.from_dict)


def rename_project(project_id: str, new_name: str) -> Project:
    new_name = clean_name(new_name, "Project")

    project = get_project(project_id)
    if any(
        existing.id != project_id and existing.name.strip().lower() == new_name.lower()
        for existing in list_projects()
    ):
        raise DuplicateProjectNameError(f"A project named {new_name!r} already exists")

    project.name = new_name
    write_json_atomic(layout.project_file(project_id), project.to_dict())
    return project


def delete_project(project_id: str) -> None:
    project_dir = layout.project_dir(project_id)
    if not project_dir.is_dir():
        raise ProjectNotFoundError(project_id)
    shutil.rmtree(project_dir)
