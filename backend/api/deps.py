from __future__ import annotations

from fastapi import Request

from backend.job_manager.manager import JobManager


def get_job_manager(request: Request) -> JobManager:
    return request.app.state.job_manager
