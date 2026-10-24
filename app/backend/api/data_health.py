from __future__ import annotations

from fastapi import APIRouter

from backend.api.schemas import RepairRequest
from backend.services import damage_service, notice_service

router = APIRouter(tags=["data-health"])


@router.get("/api/damaged")
def list_damaged(project_id: str | None = None, target_id: str | None = None) -> list[dict]:
    """Damaged data files (a project, target, tree or job that cannot be read), optionally of one project / target."""
    return damage_service.scan(project_id, target_id)


@router.post("/api/damaged/repair")
def repair_damaged(body: RepairRequest) -> dict:
    return damage_service.repair(
        body.kind, body.project_id, body.target_id, body.job_id, body.action, base_url=body.base_url
    )


@router.get("/api/notices")
def list_notices() -> list[dict]:
    """Things found at start-up that Settings should show (a restored settings file, a busy port), until seen."""
    return notice_service.list_notices()


@router.post("/api/notices/{notice_id}/seen")
def mark_notice_seen(notice_id: str) -> dict:
    notice_service.mark_seen(notice_id)
    return {"ok": True}
