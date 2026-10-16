from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from backend.api import engine_info, jobs, projects, results, settings, stream, targets, tree, update, wordlists
from backend.engine.dirsearch import DirsearchEngine
from backend.job_manager.manager import JobManager, JobNotFoundError
from backend.services.project_service import DuplicateProjectNameError, ProjectNotFoundError
from backend.services.target_service import DuplicateTargetNameError, TargetNotFoundError
from backend.services.tree_service import TreeNodeNotFoundError

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"

app = FastAPI(title="OptiFuzz")
app.state.job_manager = JobManager(DirsearchEngine())


@app.middleware("http")
async def _no_cache_frontend(request: Request, call_next):
    """The frontend is actively being edited every session -- a stale
    cached copy of app.js/styles.css/index.html silently serving old
    behavior (while curl/API tests against the backend look fine) is a
    much worse failure mode than the tiny perf cost of never caching a
    handful of small local files."""
    response = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-store"
    return response

app.include_router(projects.router)
app.include_router(targets.router)
app.include_router(jobs.router)
app.include_router(results.router)
app.include_router(stream.router)
app.include_router(engine_info.router)
app.include_router(wordlists.router)
app.include_router(settings.router)
app.include_router(tree.router)
app.include_router(update.router)

if FRONTEND_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(str(FRONTEND_DIR / "index.html"))


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


def _not_found_handler(_request: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(status_code=404, content={"detail": str(exc)})


def _conflict_handler(_request: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(status_code=409, content={"detail": str(exc)})


def _bad_request_handler(_request: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(status_code=400, content={"detail": str(exc)})


for exc_type in (ProjectNotFoundError, TargetNotFoundError, JobNotFoundError, TreeNodeNotFoundError):
    app.add_exception_handler(exc_type, _not_found_handler)

for exc_type in (DuplicateProjectNameError, DuplicateTargetNameError):
    app.add_exception_handler(exc_type, _conflict_handler)

app.add_exception_handler(ValueError, _bad_request_handler)
