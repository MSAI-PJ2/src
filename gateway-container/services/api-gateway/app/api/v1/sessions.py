from fastapi import APIRouter, Depends, HTTPException

from ...contracts.requests import SessionCreateIn
from ...core.auth import require_api_key
from ...session import session_repository

router = APIRouter(dependencies=[Depends(require_api_key)])


@router.post("/sessions")
async def create_session(body: SessionCreateIn | None = None):
    return await session_repository.create(body.session_id if body else None)


@router.get("/sessions/{session_id}")
async def get_session(session_id: str):
    state = await session_repository.snapshot(session_id)
    if state is None:
        raise HTTPException(404, "session not found")
    return state
