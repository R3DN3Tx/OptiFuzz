from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from backend.api.deps import get_job_manager
from backend.job_manager.manager import JobManager
from backend.services import update_service

router = APIRouter(tags=["update"])


@router.get("/api/update/check")
def check_for_update() -> dict:
    try:
        return update_service.check()
    except update_service.UpdateError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/api/update/apply")
def apply_update(job_manager: JobManager = Depends(get_job_manager)) -> dict:
    try:
        # Running/queued jobs are paused first (resumable from their checkpoint), but only once we
        # know the update can actually go ahead -- a refused update must not pause anything.
        paused = job_manager.pause_all() if update_service.check()["can_update"] else 0
        result = update_service.apply()
        return {**result, "paused_jobs": paused}
    except update_service.UpdateError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/api/update/restart")
def restart_after_update() -> dict:
    # Desktop app only: closes the window and starts OptiFuzz again by itself.
    try:
        update_service.restart()
    except update_service.UpdateError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"restarting": True}
