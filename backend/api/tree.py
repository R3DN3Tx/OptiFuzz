from __future__ import annotations

from fastapi import APIRouter, Depends, Response

from backend.api.deps import get_job_manager
from backend.api.schemas import AddTreePathRequest, AddTreeResultRequest, UpdateTreeNodeRequest
from backend.job_manager.manager import JobManager
from backend.services import results_service, tree_service

router = APIRouter(prefix="/api/projects/{project_id}/targets/{target_id}/tree", tags=["tree"])


@router.get("")
def list_tree(
    project_id: str,
    target_id: str,
    job_manager: JobManager = Depends(get_job_manager),
) -> list[dict]:
    jobs = job_manager.list_jobs(target_id)
    results = results_service.list_results(
        project_id, target_id, jobs, live_results=job_manager.live_results()
    )
    return tree_service.tree_views(project_id, target_id, jobs, results)


@router.post("")
def add_path(project_id: str, target_id: str, body: AddTreePathRequest) -> dict:
    node = tree_service.add_path(project_id, target_id, body.path, source="manual")
    return node.to_dict()


@router.post("/from-result")
def add_from_result(project_id: str, target_id: str, body: AddTreeResultRequest) -> dict:
    node = tree_service.add_path(
        project_id,
        target_id,
        body.path,
        source="result",
        job_id=body.job_id,
        status=body.status,
        size=body.size,
    )
    return node.to_dict()


@router.patch("/{node_id}")
def update_node(
    project_id: str,
    target_id: str,
    node_id: str,
    body: UpdateTreeNodeRequest,
    job_manager: JobManager = Depends(get_job_manager),
) -> dict:
    if body.interesting is not None:
        tree_service.set_interesting(project_id, target_id, node_id, body.interesting)
    if body.note is not None:
        tree_service.set_note(project_id, target_id, node_id, body.note)
    node = tree_service.get_node(project_id, target_id, node_id)
    return tree_service.node_to_view(node, job_manager.list_jobs(target_id))


@router.delete("/{node_id}", status_code=204)
def delete_node(project_id: str, target_id: str, node_id: str) -> Response:
    tree_service.delete_node(project_id, target_id, node_id)
    return Response(status_code=204)


@router.delete("/by-path/subtree", status_code=204)
def delete_subtree(project_id: str, target_id: str, path: str) -> Response:
    """Deletes a SYNTHETIC ancestor path (one with no real node/id of its
    own -- see tree_service.delete_subtree's own docstring) by cascading to
    every real node at or under it instead."""
    tree_service.delete_subtree(project_id, target_id, path)
    return Response(status_code=204)
