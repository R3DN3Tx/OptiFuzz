from __future__ import annotations

from fastapi import APIRouter, Depends

from backend.api.deps import get_job_manager
from backend.job_manager.manager import JobManager

router = APIRouter(tags=["engine"])


@router.get("/api/engine/version")
def engine_version(job_manager: JobManager = Depends(get_job_manager)) -> dict:
    return {"version": job_manager.engine_version()}
