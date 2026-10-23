from __future__ import annotations

from fastapi import APIRouter, Depends

from backend.api.deps import get_job_manager
from backend.api.schemas import HideRulesRequest, MarkSeenRequest
from backend.job_manager.manager import JobManager
from backend.services import results_service

router = APIRouter(tags=["results"])


@router.get("/api/projects/{project_id}/targets/{target_id}/results")
def list_results(
    project_id: str,
    target_id: str,
    job_manager: JobManager = Depends(get_job_manager),
) -> list[dict]:
    jobs = job_manager.list_jobs(target_id)
    results = results_service.list_results(
        project_id, target_id, jobs, live_results=job_manager.live_results()
    )
    seen_keys = results_service.get_seen_keys(project_id, target_id)
    return results_service.annotate_new(results, seen_keys)


@router.get("/api/projects/{project_id}/targets/{target_id}/results/hide-rules")
def get_hide_rules(project_id: str, target_id: str) -> list[dict]:
    return results_service.get_hide_rules(project_id, target_id)


@router.put("/api/projects/{project_id}/targets/{target_id}/results/hide-rules")
def set_hide_rules(project_id: str, target_id: str, body: HideRulesRequest) -> list[dict]:
    return results_service.set_hide_rules(project_id, target_id, body.rules)


@router.post("/api/projects/{project_id}/targets/{target_id}/results/mark-seen")
def mark_results_seen(project_id: str, target_id: str, body: MarkSeenRequest) -> dict:
    results_service.mark_seen(project_id, target_id, body.keys)
    return {"ok": True}
