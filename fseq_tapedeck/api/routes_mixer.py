"""REST endpoints: timeline state, placements, and export."""

import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from clip_store import ClipNotFoundError
from mixer import render_timeline

router = APIRouter(prefix="/api/timeline", tags=["mixer"])


class AddPlacementRequest(BaseModel):
    clip_id: str
    start_ms: int
    fade_in_ms: int = 0
    fade_out_ms: int = 0
    trim_start_ms: int = 0
    trim_end_ms: Optional[int] = None


class UpdatePlacementRequest(BaseModel):
    start_ms: Optional[int] = None
    fade_in_ms: Optional[int] = None
    fade_out_ms: Optional[int] = None
    trim_start_ms: Optional[int] = None
    trim_end_ms: Optional[int] = None


class ExportRequest(BaseModel):
    name: str
    channel_count: int
    step_ms: int


@router.get("")
async def get_timeline(request: Request):
    timeline = request.app.state.timeline
    return {
        "placements": [asdict(p) for p in timeline.placements],
        "settings": timeline.settings,
    }


@router.post("/placements")
async def add_placement(body: AddPlacementRequest, request: Request):
    timeline = request.app.state.timeline
    try:
        request.app.state.clip_store.get_clip(body.clip_id)
    except ClipNotFoundError:
        raise HTTPException(status_code=404, detail=f"no clip with id {body.clip_id!r}")
    placement = timeline.add_placement(**body.model_dump())
    return asdict(placement)


@router.put("/placements/{placement_id}")
async def update_placement(placement_id: str, body: UpdatePlacementRequest, request: Request):
    timeline = request.app.state.timeline
    fields = body.model_dump(exclude_unset=True)
    try:
        placement = timeline.update_placement(placement_id, **fields)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"no placement with id {placement_id!r}")
    return asdict(placement)


@router.delete("/placements/{placement_id}")
async def remove_placement(placement_id: str, request: Request):
    timeline = request.app.state.timeline
    try:
        timeline.remove_placement(placement_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"no placement with id {placement_id!r}")
    return {"deleted": placement_id}


@router.post("/export")
async def export_timeline(body: ExportRequest, request: Request):
    timeline = request.app.state.timeline
    clip_store = request.app.state.clip_store
    exports_dir: Path = request.app.state.exports_dir

    clip_ids = {p.clip_id for p in timeline.placements}
    clip_paths = {}
    for clip_id in clip_ids:
        try:
            clip_paths[clip_id] = clip_store.get_clip_path(clip_id)
        except ClipNotFoundError:
            raise HTTPException(status_code=404, detail=f"no clip with id {clip_id!r}")

    output_path = exports_dir / f"{body.name}.fseq"
    info = render_timeline(
        placements=timeline.placements,
        clip_paths=clip_paths,
        channel_count=body.channel_count,
        step_ms=body.step_ms,
        output_path=output_path,
    )

    timeline.settings["channel_count"] = body.channel_count
    timeline.settings["step_ms"] = body.step_ms
    timeline.save()

    export_meta = {
        "name": body.name,
        "path": str(output_path),
        "channel_count": info.channel_count,
        "frame_count": info.frame_count,
        "step_ms": info.step_ms,
        "duration_seconds": info.duration_seconds,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    (exports_dir / f"{body.name}.json").write_text(json.dumps(export_meta, indent=2))
    return export_meta


@router.get("/exports")
async def list_exports(request: Request):
    exports_dir: Path = request.app.state.exports_dir
    exports = []
    for meta_path in sorted(exports_dir.glob("*.json")):
        exports.append(json.loads(meta_path.read_text()))
    exports.sort(key=lambda e: e.get("created_at", ""))
    return exports
