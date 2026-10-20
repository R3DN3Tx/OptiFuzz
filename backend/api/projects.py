from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response

from backend.api.deps import get_job_manager
from backend.api.schemas import CreateProjectRequest, RenameRequest, SetOutputsMirrorDirRequest
from backend.job_manager.manager import JobManager
from backend.models.job import TERMINAL_STATUSES, JobStatus
from backend.persistence.slug import slugify
from backend.services import export_service, output_mirror_service, project_service, target_service

router = APIRouter(prefix="/api/projects", tags=["projects"])


@router.post("")
def create_project(body: CreateProjectRequest) -> dict:
    return project_service.create_project(body.name, body.notes).to_dict()


@router.get("")
def list_projects(job_manager: JobManager = Depends(get_job_manager)) -> list[dict]:
    projects = []
    for p in project_service.list_projects():
        data = p.to_dict()
        # Same "currently fuzzing" dot the Targets grid shows, one level up: every
        # running job of every target in this project, with its target's name.
        running = [
            {
                "target": t.name,
                "path": job.path,
                "percent": round(job.completed_lines / job.total_wordlist_lines * 100)
                if job.total_wordlist_lines else 0,
            }
            for t in target_service.list_targets(p.id)
            for job in job_manager.list_jobs(t.id)
            if job.status == JobStatus.RUNNING
        ]
        data["is_fuzzing"] = bool(running)
        data["running_jobs"] = running
        projects.append(data)
    return projects


@router.get("/{project_id}")
def get_project(project_id: str) -> dict:
    return project_service.get_project(project_id).to_dict()


@router.patch("/{project_id}")
def rename_project(project_id: str, body: RenameRequest) -> dict:
    return project_service.rename_project(project_id, body.name).to_dict()


@router.get("/{project_id}/export-all")
def export_all_targets(
    project_id: str, hide_rules: str | None = None, job_manager: JobManager = Depends(get_job_manager)
) -> Response:
    """Every target in this Project, bundled into one .zip -- 2026-09-24,
    explicit request. Same self-contained HTML each target's own
    "Export target (HTML)" button produces, one file per target inside the
    archive (see export_service.export_all_targets_zip's own docstring).
    `hide_rules` -- see export_target's own docstring in
    backend/api/targets.py, same mechanism, applied to every target here."""
    project = project_service.get_project(project_id)
    zip_bytes = export_service.export_all_targets_zip(project_id, job_manager, hide_rules_json=hide_rules)
    filename = f"{slugify(project.name)}_Optifuzz_Reports.zip"
    return Response(
        content=zip_bytes,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/{project_id}/export-all-combined")
def export_all_targets_combined(
    project_id: str, job_manager: JobManager = Depends(get_job_manager)
) -> Response:
    """Every target in this Project, bundled into ONE self-contained HTML
    page -- the alternative format offered by the same "Export all
    targets" button's own dropdown arrow in the live app (export_all_targets
    above is the other one, a .zip of separate per-target files). Reproduces
    the live app's own Project -> Targets -> Target-workspace navigation
    (search, card grid, click a target to open its JOBS/RESULTS/TREE, click
    the project name to go back) in one page -- see
    export_service.export_project_html's own docstring."""
    project = project_service.get_project(project_id)
    html = export_service.export_project_html(project_id, job_manager)
    filename = export_service.export_all_filename(project.name)
    return Response(
        content=html,
        media_type="text/html",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/{project_id}/outputs-mirror")
def get_outputs_mirror(project_id: str) -> dict:
    project = project_service.get_project(project_id)
    return {
        "dir": project.outputs_mirror_dir,
        "placeholder": output_mirror_service.default_mirror_dir_placeholder(project_id),
    }


@router.put("/{project_id}/outputs-mirror")
def set_outputs_mirror(project_id: str, body: SetOutputsMirrorDirRequest) -> dict:
    project = output_mirror_service.set_mirror_dir(project_id, body.dir)
    return {
        "dir": project.outputs_mirror_dir,
        "placeholder": output_mirror_service.default_mirror_dir_placeholder(project_id),
    }


@router.delete("/{project_id}", status_code=204)
def delete_project(
    project_id: str,
    job_manager: JobManager = Depends(get_job_manager),
) -> Response:
    active_count = sum(
        1
        for target in target_service.list_targets(project_id)
        for job in job_manager.list_jobs(target.id)
        if job.status not in TERMINAL_STATUSES
    )
    if active_count:
        raise HTTPException(
            status_code=409,
            detail=f"Project has {active_count} active job(s) across its targets; stop them before deleting.",
        )
    project_service.delete_project(project_id)
    return Response(status_code=204)
