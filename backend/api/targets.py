from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response

from backend.api.deps import get_job_manager
from backend.api.schemas import (
    CreateTargetRequest,
    RenameRequest,
    TargetConcurrencyRequest,
    TargetFuzzDefaultsRequest,
)
from backend.job_manager.manager import JobManager
from backend.models.job import TERMINAL_STATUSES, JobStatus
from backend.persistence import app_settings
from backend.services import export_service, project_service, target_service

router = APIRouter(prefix="/api/projects/{project_id}/targets", tags=["targets"])


@router.post("")
def create_target(project_id: str, body: CreateTargetRequest) -> dict:
    return target_service.create_target(project_id, body.name, body.base_url).to_dict()


@router.get("")
def list_targets(
    project_id: str, job_manager: JobManager = Depends(get_job_manager)
) -> list[dict]:
    targets = []
    for t in target_service.list_targets(project_id):
        data = t.to_dict()
        running = [job for job in job_manager.list_jobs(t.id) if job.status == JobStatus.RUNNING]
        data["is_fuzzing"] = bool(running)
        # For the "currently fuzzing" dot's hover list (path + %) on the
        # Targets grid -- a last-checkpointed percent (not the live one
        # JOBS gets over SSE), good enough for a hover summary without
        # wiring SSE into this page too.
        data["running_jobs"] = [
            {
                "path": job.path,
                "percent": round(job.completed_lines / job.total_wordlist_lines * 100)
                if job.total_wordlist_lines else 0,
            }
            for job in running
        ]
        targets.append(data)
    return targets


@router.get("/{target_id}")
def get_target(
    project_id: str, target_id: str, job_manager: JobManager = Depends(get_job_manager)
) -> dict:
    data = target_service.get_target(project_id, target_id).to_dict()
    data["max_concurrent_jobs"] = job_manager.get_target_concurrency(target_id)
    return data


@router.get("/{target_id}/concurrency")
def get_target_concurrency(
    target_id: str, job_manager: JobManager = Depends(get_job_manager)
) -> dict:
    return {"max_concurrent_jobs": job_manager.get_target_concurrency(target_id)}


@router.put("/{target_id}/concurrency")
def set_target_concurrency(
    target_id: str,
    body: TargetConcurrencyRequest,
    job_manager: JobManager = Depends(get_job_manager),
) -> dict:
    value = job_manager.set_target_concurrency(target_id, body.max_concurrent_jobs)
    return {"max_concurrent_jobs": value}


@router.patch("/{target_id}")
def rename_target(project_id: str, target_id: str, body: RenameRequest) -> dict:
    return target_service.rename_target(project_id, target_id, body.name).to_dict()


@router.get("/{target_id}/fuzz-defaults")
def get_fuzz_defaults(target_id: str) -> dict:
    """Whatever was last configured (never launched) in this target's own
    New Fuzz form -- {} if nothing's been saved yet, so the frontend can
    fall back to its own hardcoded defaults without a special-case."""
    return app_settings.get_target_fuzz_defaults(target_id) or {}


@router.put("/{target_id}/fuzz-defaults")
def set_fuzz_defaults(target_id: str, body: TargetFuzzDefaultsRequest) -> dict:
    data = body.model_dump()
    app_settings.set_target_fuzz_defaults(target_id, data)
    return data


@router.get("/{target_id}/export")
def export_target(
    project_id: str,
    target_id: str,
    hide_rules: str | None = None,
    job_manager: JobManager = Depends(get_job_manager),
) -> Response:
    """Spec §61-65: a single self-contained HTML snapshot of this Target,
    downloadable and openable from `file:///` with no backend running.
    `Content-Disposition: attachment` (not just returning text/html inline)
    so the desktop app's WebView2 view -- a real Chromium browser control --
    triggers its own native "Save As" dialog the same way any other browser
    would, no custom pywebview save-file bridge needed.

    `hide_rules` (2026-09-24, explicit request): whatever Results hide rules
    are active in the live app AT EXPORT TIME (frontend/app.js's own
    `resultsHideFilters`, a purely client-side/localStorage concept the
    backend otherwise has no visibility into at all) -- the export's own
    "Export target (HTML)" button sends its current value here, JSON-encoded,
    so the export opens with the SAME rows already hidden instead of
    everything visible again. Still just seeds the export's own copy, which
    stays fully editable from inside the export afterwards (add/remove hide
    rules there, same as the live app) -- this only affects the STARTING
    state."""
    project = project_service.get_project(project_id)
    target = target_service.get_target(project_id, target_id)
    body = export_service.export_target_html(project_id, target_id, job_manager, hide_rules_json=hide_rules)
    filename = export_service.export_filename(project.name, target.name)
    return Response(
        content=body,
        media_type="text/html",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.delete("/{target_id}", status_code=204)
def delete_target(
    project_id: str,
    target_id: str,
    job_manager: JobManager = Depends(get_job_manager),
) -> Response:
    active = [
        job for job in job_manager.list_jobs(target_id) if job.status not in TERMINAL_STATUSES
    ]
    if active:
        raise HTTPException(
            status_code=409,
            detail=f"Target has {len(active)} active job(s); stop them before deleting.",
        )
    target_service.delete_target(project_id, target_id)
    return Response(status_code=204)
