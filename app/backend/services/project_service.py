from __future__ import annotations

import shutil

from backend.models.project import Project
from backend.models.timestamps import now_iso
from backend.persistence import layout
from backend.persistence.atomic import read_json, write_json_atomic
from backend.persistence.slug import make_id


class ProjectNotFoundError(Exception):
    pass


class DuplicateProjectNameError(Exception):
    pass


def create_project(name: str, notes: str = "") -> Project:
    name = name.strip()
    if not name:
        raise ValueError("Project name is required")

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
            projects.append(Project.from_dict(read_json(project_file)))
    return projects


def get_project(project_id: str) -> Project:
    path = layout.project_file(project_id)
    if not path.is_file():
        raise ProjectNotFoundError(project_id)
    return Project.from_dict(read_json(path))


def rename_project(project_id: str, new_name: str) -> Project:
    new_name = new_name.strip()
    if not new_name:
        raise ValueError("Project name is required")

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
