from __future__ import annotations

from fastapi import APIRouter

from backend.services import wordlist_service

router = APIRouter(tags=["wordlists"])


@router.get("/api/wordlists")
def list_wordlists() -> dict:
    return wordlist_service.list_wordlists()
