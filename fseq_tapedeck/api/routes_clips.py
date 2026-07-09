"""REST endpoints: rename, delete, get clip info."""

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from clip_store import ClipNotFoundError

router = APIRouter(prefix="/api/clips", tags=["clips"])


class RenameClipRequest(BaseModel):
    name: str


@router.get("")
async def list_clips(request: Request):
    return request.app.state.clip_store.list_clips()


@router.get("/{clip_id}")
async def get_clip(clip_id: str, request: Request):
    try:
        return request.app.state.clip_store.get_clip(clip_id)
    except ClipNotFoundError:
        raise HTTPException(status_code=404, detail=f"no clip with id {clip_id!r}")


@router.patch("/{clip_id}")
async def rename_clip(clip_id: str, body: RenameClipRequest, request: Request):
    try:
        return request.app.state.clip_store.rename_clip(clip_id, body.name)
    except ClipNotFoundError:
        raise HTTPException(status_code=404, detail=f"no clip with id {clip_id!r}")


@router.delete("/{clip_id}")
async def delete_clip(clip_id: str, request: Request):
    try:
        request.app.state.clip_store.delete_clip(clip_id)
    except ClipNotFoundError:
        raise HTTPException(status_code=404, detail=f"no clip with id {clip_id!r}")
    return {"deleted": clip_id}
