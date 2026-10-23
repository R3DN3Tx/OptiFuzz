from __future__ import annotations

import shlex
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException

from backend.api.deps import get_job_manager
from backend.api.schemas import CreateJobRequest
from backend.job_manager.manager import JobManager
from backend.models.job_config import DIRSEARCH_DEFAULT_HEADERS, DirsearchJobConfig
from backend.persistence import app_settings
from backend.persistence import layout
from backend.persistence.paths import path_is_file
from backend.services import output_mirror_service, output_naming, project_service, target_service

router = APIRouter(tags=["jobs"])


@router.get("/api/dirsearch-default-headers")
def dirsearch_default_headers() -> dict:
    """The headers dirsearch adds on its own -- what New Fuzz lets you switch off per job."""
    return {"headers": [{"name": n, "value": v} for n, v in DIRSEARCH_DEFAULT_HEADERS.items()]}


def _effective_default_extensions() -> tuple[str, ...]:
    """What dirsearch uses when a job has no extensions: Settings' list, dirsearch's own when nothing
    is configured, none when "use no extensions" is chosen."""
    configured = app_settings.get_default_extensions_fallback()
    value = app_settings.DIRSEARCH_DEFAULT_EXTENSIONS if configured is None else configured
    return tuple(part.strip() for part in value.split(",") if part.strip())


@router.post("/api/projects/{project_id}/targets/{target_id}/jobs")
def create_job(
    project_id: str,
    target_id: str,
    body: CreateJobRequest,
    job_manager: JobManager = Depends(get_job_manager),
) -> dict:
    target = target_service.get_target(project_id, target_id)

    wordlist_paths = [Path(p) for p in body.wordlist_files]
    missing = [str(p) for p in wordlist_paths if not p.is_file()]
    if missing:
        raise HTTPException(
            status_code=400, detail=f"Wordlist file(s) not found: {', '.join(missing)}"
        )

    try:
        extra_args = tuple(shlex.split(body.extra_args))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid custom options: {exc}") from exc

    disabled_headers = tuple(dict.fromkeys(n.strip().lower() for n in body.disabled_default_headers))
    unknown = [n for n in disabled_headers if n not in DIRSEARCH_DEFAULT_HEADERS]
    if unknown:
        raise HTTPException(
            status_code=400, detail=f"Not a dirsearch default header: {', '.join(unknown)}"
        )

    path = target_service.normalize_fuzz_path(body.path)
    url = target_service.resolve_fuzz_url(target.base_url, path)
    config = DirsearchJobConfig(
        url=url,
        extensions=tuple(body.extensions),
        force_extensions=body.force_extensions,
        threads=body.threads,
        http_method=body.http_method,
        headers=body.headers,
        cookie=body.cookie,
        user_agent=body.user_agent,
        follow_redirects=body.follow_redirects,
        include_status_codes=tuple(body.include_status_codes),
        exclude_status_codes=tuple(body.exclude_status_codes),
        timeout=body.timeout,
        proxy=body.proxy,
        max_rate=body.max_rate,
        delay=body.delay,
        extra_args=extra_args,
        disabled_default_headers=disabled_headers,
        default_extensions=() if body.extensions else _effective_default_extensions(),
    )

    output_stem = output_naming.build_output_stem(
        app_settings.get_output_filename_template(),
        target_name=target.name,
        url=url,
        path=path,
    )
    if body.authenticated:
        # Double underscore, deliberately -- explicit request, to stay
        # visually distinct from a real path segment (a single "_" is
        # already how output_naming.py joins path segments together).
        output_stem = f"{output_stem}__AUTH"

    job = job_manager.create_job(
        project_id=project_id,
        target_id=target_id,
        path=path,
        config=config,
        wordlist_files=wordlist_paths,
        pause_on_status=body.pause_on_status,
        output_stem=output_stem,
        output_flat=True,  # current naming scheme -- see layout.job_output_base_flat
    )
    return job.to_dict()


@router.get("/api/projects/{project_id}/targets/{target_id}/jobs")
def list_jobs(
    project_id: str,
    target_id: str,
    job_manager: JobManager = Depends(get_job_manager),
) -> list[dict]:
    del project_id  # JobManager indexes purely by target_id (globally unique)
    return [job.to_dict() for job in job_manager.list_jobs(target_id)]


@router.get("/api/jobs/{job_id}")
def get_job(job_id: str, job_manager: JobManager = Depends(get_job_manager)) -> dict:
    return job_manager.get_job(job_id).to_dict()


def _mirror_info(job_id: str, job_manager: JobManager) -> dict:
    """Where the outputs-mirror copy of a job's raw .txt is, for the "Raw" column of the job details."""
    job = job_manager.get_job(job_id)
    project = project_service.get_project(job.project_id)
    target = target_service.get_target(job.project_id, job.target_id)
    # The name the copy was really made with (the final one, or the working one if the rename never worked).
    filename = f"{layout.job_output_base_current(job.project_id, job.target_id, job).name}.txt"
    path = output_mirror_service.mirror_file_path(project, target.name, filename)
    return {
        "configured": path is not None,
        "filename": filename,
        "path": str(path) if path is not None else None,
        "exists": path is not None and path_is_file(path),
    }


@router.get("/api/jobs/{job_id}/raw-mirror")
def get_raw_mirror(job_id: str, job_manager: JobManager = Depends(get_job_manager)) -> dict:
    return _mirror_info(job_id, job_manager)


@router.post("/api/jobs/{job_id}/raw-mirror/open")
def open_raw_mirror(job_id: str, job_manager: JobManager = Depends(get_job_manager)) -> dict:
    info = _mirror_info(job_id, job_manager)
    if not info["configured"]:
        raise HTTPException(status_code=400, detail="No outputs mirror is set for this project")
    if not info["exists"]:
        raise HTTPException(status_code=404, detail="The mirrored copy does not exist yet (the job has not finished)")
    try:
        output_mirror_service.open_file(Path(info["path"]))
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Could not open the file: {exc}") from exc
    return info


@router.post("/api/jobs/{job_id}/stop")
def stop_job(job_id: str, job_manager: JobManager = Depends(get_job_manager)) -> dict:
    return job_manager.stop_job(job_id).to_dict()


@router.post("/api/jobs/{job_id}/pause")
def pause_job(job_id: str, job_manager: JobManager = Depends(get_job_manager)) -> dict:
    return job_manager.pause_job(job_id).to_dict()


@router.post("/api/jobs/{job_id}/resume")
def resume_job(
    job_id: str, repeat_word: bool = True, job_manager: JobManager = Depends(get_job_manager),
) -> dict:
    return job_manager.resume_job(job_id, repeat_word=repeat_word).to_dict()


@router.delete("/api/jobs/{job_id}", status_code=204)
def delete_job(job_id: str, job_manager: JobManager = Depends(get_job_manager)) -> None:
    job_manager.delete_job(job_id)
