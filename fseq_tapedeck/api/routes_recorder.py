"""REST endpoints: start/stop recording, recording status."""

from typing import List, Literal, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from recorder import NoDataRecordedError, list_local_ipv4_addresses

router = APIRouter(prefix="/api/record", tags=["recorder"])


class RecordStartRequest(BaseModel):
    name: str
    universes: List[int]
    step_ms: int = 40
    protocol: Literal["sacn", "artnet"] = "sacn"
    bind_address: Optional[str] = None


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
            bind_address=body.bind_address,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return recorder.status()


@router.get("/network-interfaces")
async def network_interfaces():
    """Candidate local IPv4 addresses to bind the listener to.

    Useful on machines with multiple network adapters (VPNs, WSL, virtual
    switches) where binding to 0.0.0.0 can join sACN multicast on the wrong
    interface and silently miss a console's packets.
    """
    return {"addresses": list_local_ipv4_addresses()}


@router.post("/stop")
async def stop_recording(request: Request):
    recorder = request.app.state.recorder
    if not recorder.is_recording:
        raise HTTPException(status_code=409, detail="no recording is in progress")
    try:
        metadata = await recorder.stop()
    except NoDataRecordedError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return metadata.to_dict()


@router.get("/status")
async def record_status(request: Request):
    recorder = request.app.state.recorder
    return recorder.status()


@router.get("/frame")
async def record_frame(request: Request):
    """The live in-memory buffer -- lets the UI show incoming console data
    is actually arriving while a recording is in progress."""
    recorder = request.app.state.recorder
    frame = recorder.current_frame()
    if frame is None:
        raise HTTPException(status_code=409, detail="no recording is in progress")
    return {"channels": list(frame), "universes": recorder.universes, "channel_count": recorder.channel_count}
