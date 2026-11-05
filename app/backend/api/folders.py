from __future__ import annotations

from fastapi import APIRouter, Response

from backend.api.schemas import CreateFolderRequest, MoveRequest, RenameRequest
from backend.services import folder_service

router = APIRouter(prefix="/api/folders", tags=["folders"])


@router.get("")
def list_folders() -> list[dict]:
    return [f.to_dict() for f in folder_service.list_folders()]


@router.post("")
def create_folder(body: CreateFolderRequest) -> dict:
    return folder_service.create_folder(body.name, body.parent_id).to_dict()


@router.patch("/{folder_id}")
def rename_folder(folder_id: str, body: RenameRequest) -> dict:
    return folder_service.rename_folder(folder_id, body.name).to_dict()


@router.post("/{folder_id}/move")
def move_folder(folder_id: str, body: MoveRequest) -> dict:
    return folder_service.move_folder(folder_id, body.folder_id).to_dict()


@router.delete("/{folder_id}", status_code=204)
def delete_folder(folder_id: str) -> Response:
    folder_service.delete_folder(folder_id)
    return Response(status_code=204)
