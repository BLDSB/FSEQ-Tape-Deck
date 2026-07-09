"""REST endpoints: start/stop recording, recording status."""

from typing import List, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

router = APIRouter(prefix="/api/record", tags=["recorder"])


class RecordStartRequest(BaseModel):
    name: str
    universes: List[int]
    step_ms: int = 40
    protocol: Literal["sacn", "artnet"] = "sacn"


@router.post("/start")
async def start_recording(body: RecordStartRequest, request: Request):
    recorder = request.app.state.recorder
    if recorder.is_recording:
        raise HTTPException(status_code=409, detail="a recording is already in progress")
    try:
        await recorder.start(
            name=body.name,
            universes=body.universes,
            step_ms=body.step_ms,
            protocol=body.protocol,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return recorder.status()


@router.post("/stop")
async def stop_recording(request: Request):
    recorder = request.app.state.recorder
    if not recorder.is_recording:
        raise HTTPException(status_code=409, detail="no recording is in progress")
    metadata = await recorder.stop()
    return metadata.to_dict()


@router.get("/status")
async def record_status(request: Request):
    recorder = request.app.state.recorder
    return recorder.status()
