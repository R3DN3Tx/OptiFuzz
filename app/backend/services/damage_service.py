from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

from backend.models.job import FuzzJob
from backend.models.project import Project
from backend.models.target import Target
from backend.persistence import layout
from backend.persistence.atomic import (
    DamagedFileError,
    backup_path,
    load_model,
    quarantine,
    read_json,
    restore_backup,
    write_json_atomic,
)
from backend.persistence.paths import workspace_root
from backend.persistence.slug import validate_safe_id
from backend.services import target_service

# What is wrong with the data on disk, and how to put it right. The rest of the app skips a damaged file
# instead of failing (see project_service.list_projects and friends); this service is where the damaged
# files are listed and repaired, so one bad file never takes the whole workspace with it.

_HASH_SUFFIX = re.compile(r"-[0-9a-f]{8}$")


def _name_from_id(identifier: str) -> str:
    return _HASH_SUFFIX.sub("", identifier).replace("-", " ").strip() or identifier


def _entry(kind: str, file: Path, exc: DamagedFileError, *, project_id: str, target_id: str = "", job_id: str = "") -> dict[str, Any]:
    name = {"project": project_id, "target": target_id, "tree": "Tree", "job": job_id}.get(kind, file.stem)
    return {
        "kind": kind,
        "project_id": project_id,
        "target_id": target_id,
        "job_id": job_id,
        "name": _name_from_id(name) if kind in ("project", "target") else name,
        "file": file.name,
        "path": str(file),  # the full location, so the user can find the file
        "reason": exc.reason,
        "can_restore": _has_usable_backup(file),
        # What "rebuild" does: project -> a new project.json named after its folder; target -> needs the
        # URL typed again; tree -> an empty tree; job -> nothing to rebuild (it can only be removed).
        "can_rebuild": kind in ("project", "target", "tree"),
        "needs_base_url": kind == "target",
        "can_remove": kind in ("job", "tree"),
    }


def _has_usable_backup(file: Path) -> bool:
    backup = backup_path(file)
    if not backup.is_file():
        return False
    try:
        read_json(backup)
        return True
    except (OSError, ValueError):
        return False


def _check_tree(path: Path) -> None:
    data = read_json(path)
    if not isinstance(data, dict) or not isinstance(data.get("nodes", []), list):
        raise DamagedFileError(path, "the tree file does not have the expected structure")


def scan(project_id: str | None = None, target_id: str | None = None) -> list[dict[str, Any]]:
    """Every damaged data file, optionally only those of one project / target."""
    found: list[dict[str, Any]] = []
    projects_root = layout.projects_dir()
    if not projects_root.is_dir():
        return found
    for project_dir in sorted(projects_root.iterdir()):
        if not project_dir.is_dir() or (project_id and project_dir.name != project_id):
            continue
        pid = project_dir.name
        project_file = project_dir / "project.json"
        if project_file.is_file() and not target_id:
            try:
                load_model(project_file, Project.from_dict)
            except DamagedFileError as exc:
                found.append(_entry("project", project_file, exc, project_id=pid))
        targets_root = project_dir / "targets"
        if not targets_root.is_dir():
            continue
        for target_dir in sorted(targets_root.iterdir()):
            if not target_dir.is_dir() or (target_id and target_dir.name != target_id):
                continue
            tid = target_dir.name
            target_file = target_dir / "target.json"
            if target_file.is_file():
                try:
                    load_model(target_file, Target.from_dict)
                except DamagedFileError as exc:
                    found.append(_entry("target", target_file, exc, project_id=pid, target_id=tid))
            tree_file = target_dir / "tree.json"
            if tree_file.is_file():
                try:
                    _check_tree(tree_file)
                except DamagedFileError as exc:
                    found.append(_entry("tree", tree_file, exc, project_id=pid, target_id=tid))
            jobs_root = target_dir / "jobs"
            if jobs_root.is_dir():
                for job_file in sorted(jobs_root.glob("*.json")):
                    try:
                        load_model(job_file, FuzzJob.from_dict)
                    except DamagedFileError as exc:
                        found.append(_entry("job", job_file, exc, project_id=pid, target_id=tid, job_id=job_file.stem))
    return found


def describe(path: Path) -> dict[str, Any] | None:
    """The damage entry for a file path inside the workspace (used to explain an error to the UI)."""
    try:
        parts = Path(path).resolve().relative_to(workspace_root().resolve()).parts
    except (ValueError, OSError):
        return None
    reason = "damaged"
    exc = DamagedFileError(Path(path), reason)
    if len(parts) == 3 and parts[0] == "projects" and parts[2] == "project.json":
        return _entry("project", Path(path), exc, project_id=parts[1])
    if len(parts) == 5 and parts[0] == "projects" and parts[2] == "targets":
        if parts[4] == "target.json":
            return _entry("target", Path(path), exc, project_id=parts[1], target_id=parts[3])
        if parts[4] == "tree.json":
            return _entry("tree", Path(path), exc, project_id=parts[1], target_id=parts[3])
    if len(parts) == 6 and parts[0] == "projects" and parts[2] == "targets" and parts[4] == "jobs":
        return _entry("job", Path(path), exc, project_id=parts[1], target_id=parts[3], job_id=Path(parts[5]).stem)
    return None


def _file_for(kind: str, project_id: str, target_id: str, job_id: str) -> Path:
    validate_safe_id(project_id, label="project_id")
    if kind == "project":
        return layout.project_file(project_id)
    validate_safe_id(target_id, label="target_id")
    if kind == "target":
        return layout.target_file(project_id, target_id)
    if kind == "tree":
        return layout.tree_file(project_id, target_id)
    if kind == "job":
        validate_safe_id(job_id, label="job_id")
        return layout.job_file(project_id, target_id, job_id)
    raise ValueError(f"Unknown kind: {kind}")


def repair(kind: str, project_id: str, target_id: str, job_id: str, action: str, base_url: str | None = None) -> dict[str, Any]:
    """Puts one damaged file right. Nothing is deleted: a damaged file is renamed to `<name>.damaged-<date>`."""
    file = _file_for(kind, project_id, target_id, job_id)
    if not file.exists():
        raise ValueError("That file no longer exists")
    if action == "restore":
        if not restore_backup(file):
            raise ValueError("There is no usable backup of this file. Use Rebuild instead.")
        return {"ok": True, "message": "Restored from the last good copy."}
    if action == "remove":
        if kind not in ("job", "tree"):
            raise ValueError("Only a job or a tree can be removed")
        quarantine(file)
        return {"ok": True, "message": "The damaged file was set aside. Raw results on disk were not touched."}
    if action == "rebuild":
        created = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(file.stat().st_mtime))
        if kind == "project":
            quarantine(file)
            write_json_atomic(file, Project(id=project_id, name=_name_from_id(project_id), created_at=created).to_dict())
            return {"ok": True, "message": "Project rebuilt from its folder. Its notes and outputs-mirror folder must be set again."}
        if kind == "target":
            url = target_service.validate_base_url(base_url or "")
            quarantine(file)
            write_json_atomic(file, Target(id=target_id, project_id=project_id, name=_name_from_id(target_id), base_url=url, created_at=created).to_dict())
            return {"ok": True, "message": "Target rebuilt with the URL you typed."}
        if kind == "tree":
            quarantine(file)
            write_json_atomic(file, {"nodes": []})
            return {"ok": True, "message": "The tree was reset to empty."}
        raise ValueError("This kind of file cannot be rebuilt")
    raise ValueError(f"Unknown action: {action}")
