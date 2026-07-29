"""REST endpoints: rename, delete, get clip info."""

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from clip_store import ClipNotFoundError
from loop import LOOP_MODES, make_loop_clip

router = APIRouter(prefix="/api/clips", tags=["clips"])


class RenameClipRequest(BaseModel):
    name: str


class LoopClipRequest(BaseModel):
    name: str
    mode: str
    crossfade_ms: int = 1000


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


@router.post("/{clip_id}/loop")
async def loop_clip(clip_id: str, body: LoopClipRequest, request: Request):
    """Build a new, seamless looping clip from an existing recording. The source
    clip is left untouched; the loop lands in the library as its own clip."""
    clip_store = request.app.state.clip_store
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="a name is required")
    if body.mode not in LOOP_MODES:
        raise HTTPException(
            status_code=400, detail=f"mode must be one of: {', '.join(LOOP_MODES)}"
        )
    try:
        return make_loop_clip(
            clip_store,
            clip_id,
            name=name,
            mode=body.mode,
            crossfade_ms=body.crossfade_ms,
        )
    except ClipNotFoundError:
        raise HTTPException(status_code=404, detail=f"no clip with id {clip_id!r}")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.delete("/{clip_id}")
async def delete_clip(clip_id: str, request: Request):
    try:
        request.app.state.clip_store.delete_clip(clip_id)
    except ClipNotFoundError:
        raise HTTPException(status_code=404, detail=f"no clip with id {clip_id!r}")
    return {"deleted": clip_id}
