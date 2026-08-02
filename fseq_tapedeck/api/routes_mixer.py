"""REST endpoints: timeline state, placements, and export."""

import asyncio
import json
import os
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from clip_store import ClipNotFoundError
from mixer import (
    placement_duration_ms,
    render_frame_at,
    render_timeline,
    timeline_duration_ms,
)

router = APIRouter(prefix="/api/timeline", tags=["mixer"])


def _effective_exports_dir(request: Request) -> Path:
    """The folder exports are written to and listed from: the user-chosen
    directory if set (persisted in project.json), else the app default."""
    configured = request.app.state.timeline.settings.get("export_dir")
    if configured:
        return Path(configured)
    return request.app.state.exports_dir


def _resolve_clip_paths(placements, clip_store):
    clip_paths = {}
    for clip_id in {p.clip_id for p in placements}:
        try:
            clip_paths[clip_id] = clip_store.get_clip_path(clip_id)
        except ClipNotFoundError:
            raise HTTPException(status_code=404, detail=f"no clip with id {clip_id!r}")
    return clip_paths


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
    crossfade_ms: Optional[int] = None


class CrossfadeRequest(BaseModel):
    placement_id_a: str
    placement_id_b: str
    duration_ms: int = 1000


class ExportRequest(BaseModel):
    name: str
    channel_count: int
    step_ms: int
    # > 0 dissolves the finished mix's tail back over its head so a player that
    # restarts the file (FPP) wraps with no visible jump.
    loop_crossfade_ms: int = 0


class PlaybackStartRequest(BaseModel):
    channel_count: int
    step_ms: int = 40
    destination: Optional[str] = None
    start_t_ms: float = 0.0


class PlaybackScrubRequest(BaseModel):
    t_ms: float
    channel_count: int
    step_ms: int = 40
    destination: Optional[str] = None


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


def _find_placement(timeline, placement_id: str):
    for placement in timeline.placements:
        if placement.placement_id == placement_id:
            return placement
    raise HTTPException(status_code=404, detail=f"no placement with id {placement_id!r}")


@router.post("/crossfade")
async def auto_crossfade(body: CrossfadeRequest, request: Request):
    """Arrange two placements into a smooth cross-dissolve. The earlier clip is
    the outgoing one; the later clip is moved to overlap it by duration_ms and
    marked to dissolve in, so the transition has no sudden jump in color or
    brightness -- no manual fade tweaking required."""
    timeline = request.app.state.timeline
    clip_store = request.app.state.clip_store

    if body.placement_id_a == body.placement_id_b:
        raise HTTPException(status_code=400, detail="pick two different clips to crossfade")
    if body.duration_ms <= 0:
        raise HTTPException(status_code=400, detail="crossfade length must be positive")

    pa = _find_placement(timeline, body.placement_id_a)
    pb = _find_placement(timeline, body.placement_id_b)

    # Earlier clip = outgoing, later clip = incoming.
    outgoing, incoming = (pa, pb) if pa.start_ms <= pb.start_ms else (pb, pa)

    try:
        out_path = clip_store.get_clip_path(outgoing.clip_id)
        in_path = clip_store.get_clip_path(incoming.clip_id)
    except ClipNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    out_dur = placement_duration_ms(outgoing, out_path)
    in_dur = placement_duration_ms(incoming, in_path)

    # Can't dissolve for longer than either clip actually plays.
    duration = min(body.duration_ms, out_dur, in_dur)
    if duration <= 0:
        raise HTTPException(status_code=400, detail="one of the clips is too short to crossfade")

    # Overlap the incoming clip onto the tail of the outgoing one so the
    # outgoing clip covers the whole transition and ends exactly as it finishes.
    new_start = max(0, outgoing.start_ms + out_dur - duration)

    timeline.update_placement(
        incoming.placement_id,
        start_ms=new_start,
        fade_in_ms=0,
        crossfade_ms=duration,
    )
    timeline.update_placement(outgoing.placement_id, fade_out_ms=0)

    return {
        "outgoing": asdict(_find_placement(timeline, outgoing.placement_id)),
        "incoming": asdict(_find_placement(timeline, incoming.placement_id)),
        "duration_ms": duration,
    }


@router.get("/frame")
async def get_frame_at(t_ms: float, request: Request, channel_count: Optional[int] = None):
    """A single HTP-merged, fade-scaled frame at time t_ms -- used to drive
    live timeline playback/scrubbing preview without generating a file."""
    timeline = request.app.state.timeline
    clip_store = request.app.state.clip_store

    cc = channel_count if channel_count is not None else timeline.settings.get("channel_count", 512)
    clip_paths = _resolve_clip_paths(timeline.placements, clip_store)

    frame_bytes = render_frame_at(
        placements=timeline.placements,
        clip_paths=clip_paths,
        channel_count=cc,
        t_ms=max(0.0, t_ms),
    )
    return {"t_ms": t_ms, "channels": list(frame_bytes)}


@router.post("/playback/start")
async def start_playback(body: PlaybackStartRequest, request: Request):
    """Stream the current timeline mix out over sACN in real time, e.g. so
    a console's visualizer can show it. Defaults to multicast; pass
    `destination` (the console's IP) for unicast, which is more reliable
    on machines with multiple network adapters (VPNs, WSL, ...)."""
    timeline = request.app.state.timeline
    clip_store = request.app.state.clip_store
    engine = request.app.state.playback_engine

    if engine.is_playing:
        raise HTTPException(status_code=409, detail="playback is already running")

    clip_paths = _resolve_clip_paths(timeline.placements, clip_store)
    total_ms = timeline_duration_ms(timeline.placements, clip_paths)

    try:
        await engine.start(
            placements=timeline.placements,
            clip_paths=clip_paths,
            channel_count=body.channel_count,
            step_ms=body.step_ms,
            total_ms=total_ms,
            destination=body.destination,
            start_t_ms=body.start_t_ms,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return engine.status()


@router.post("/playback/scrub")
async def scrub_playback(body: PlaybackScrubRequest, request: Request):
    """Send the frame at ``t_ms`` to the console live and hold it there, so a
    console's visualizer follows the timeline scrubber. Brings the live source
    up on first use, then just moves the held playhead on subsequent calls."""
    timeline = request.app.state.timeline
    clip_store = request.app.state.clip_store
    engine = request.app.state.playback_engine

    clip_paths = _resolve_clip_paths(timeline.placements, clip_store)

    try:
        if engine.is_active:
            await engine.scrub(
                placements=timeline.placements,
                clip_paths=clip_paths,
                t_ms=body.t_ms,
            )
        else:
            total_ms = timeline_duration_ms(timeline.placements, clip_paths)
            await engine.start(
                placements=timeline.placements,
                clip_paths=clip_paths,
                channel_count=body.channel_count,
                step_ms=body.step_ms,
                total_ms=total_ms,
                destination=body.destination,
                start_t_ms=body.t_ms,
                play=False,
            )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return engine.status()


@router.post("/playback/pause")
async def pause_playback(request: Request):
    """Stop advancing but keep the live source up, holding the current frame."""
    engine = request.app.state.playback_engine
    await engine.pause()
    return engine.status()


@router.post("/playback/stop")
async def stop_playback(request: Request):
    engine = request.app.state.playback_engine
    was_active = engine.is_active
    await engine.stop()
    return {"stopped": was_active}


@router.get("/playback/status")
async def playback_status(request: Request):
    engine = request.app.state.playback_engine
    return engine.status()


@router.post("/export")
async def export_timeline(body: ExportRequest, request: Request):
    timeline = request.app.state.timeline
    clip_store = request.app.state.clip_store
    exports_dir: Path = _effective_exports_dir(request)
    exports_dir.mkdir(parents=True, exist_ok=True)

    clip_paths = _resolve_clip_paths(timeline.placements, clip_store)

    output_path = exports_dir / f"{body.name}.fseq"
    result = render_timeline(
        placements=timeline.placements,
        clip_paths=clip_paths,
        channel_count=body.channel_count,
        step_ms=body.step_ms,
        output_path=output_path,
        loop_crossfade_ms=body.loop_crossfade_ms,
    )
    info, report = result.info, result.report

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
        # What the exporter did to make the file loop cleanly, so the UI can
        # say so rather than silently changing the show's length.
        "lead_trimmed_ms": report.lead_trimmed_ms,
        "tail_trimmed_ms": report.tail_trimmed_ms,
        "loop_crossfade_ms": report.loop_crossfade_ms,
        "blackout_gaps": [list(g) for g in report.blackout_gaps],
    }
    (exports_dir / f"{body.name}.json").write_text(json.dumps(export_meta, indent=2))
    return export_meta


@router.get("/exports")
async def list_exports(request: Request):
    exports_dir: Path = _effective_exports_dir(request)
    exports = []
    if exports_dir.exists():
        for meta_path in sorted(exports_dir.glob("*.json")):
            # A user-chosen export folder may hold unrelated .json files, so
            # only accept our own export sidecars (a dict written beside an
            # .fseq); skip anything unreadable, non-JSON, or a different shape.
            try:
                data = json.loads(meta_path.read_text())
            except (OSError, ValueError):
                continue
            if isinstance(data, dict) and "name" in data and "path" in data:
                exports.append(data)
    exports.sort(key=lambda e: e.get("created_at", ""))
    return exports


class ExportDirRequest(BaseModel):
    path: str


def _export_dir_payload(request: Request) -> dict:
    effective = _effective_exports_dir(request)
    default = request.app.state.exports_dir
    return {"path": str(effective), "is_default": Path(effective) == Path(default)}


@router.get("/export-dir")
async def get_export_dir(request: Request):
    return _export_dir_payload(request)


@router.put("/export-dir")
async def set_export_dir(body: ExportDirRequest, request: Request):
    """Choose where exports are saved. A blank path resets to the default."""
    timeline = request.app.state.timeline
    raw = body.path.strip()
    if not raw:
        timeline.settings.pop("export_dir", None)
        timeline.save()
        return _export_dir_payload(request)

    path = Path(raw).expanduser()
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise HTTPException(status_code=400, detail=f"cannot use that folder: {exc}")

    timeline.settings["export_dir"] = str(path)
    timeline.save()
    return _export_dir_payload(request)


@router.post("/export-dir/open")
async def open_export_dir(request: Request):
    """Open the current export folder in the OS file browser (local tool)."""
    if sys.platform != "win32":
        raise HTTPException(status_code=400, detail="opening a folder is only supported on Windows")
    exports_dir = _effective_exports_dir(request)
    exports_dir.mkdir(parents=True, exist_ok=True)
    os.startfile(str(exports_dir))  # noqa: S606 -- opens Explorer, no shell, local desktop tool
    return {"opened": True, "path": str(exports_dir)}


# A native folder picker on the machine running the server (which, for this
# local tool, is the user's own machine). A GUI spawned by a background
# process opens *without* foreground focus and lands behind the browser, so
# the dialog is parented to an invisible, top-most owner form that is actually
# shown and activated -- that reliably pulls the modal to the front. The
# initial folder is passed via an env var, never interpolated into the script,
# so there is nothing to inject.
_FOLDER_PICKER_PS = """
$ProgressPreference = 'SilentlyContinue'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$owner = New-Object System.Windows.Forms.Form
$owner.StartPosition = 'CenterScreen'
$owner.Size = New-Object System.Drawing.Size(1, 1)
$owner.Opacity = 0
$owner.ShowInTaskbar = $false
$owner.TopMost = $true
$script:picked = $null
$owner.Add_Shown({
    $owner.Activate()
    $dialog = New-Object System.Windows.Forms.FolderBrowserDialog
    $dialog.Description = 'Choose where to save FSEQ exports'
    if ($env:FSEQ_INIT_DIR) { $dialog.SelectedPath = $env:FSEQ_INIT_DIR }
    if ($dialog.ShowDialog($owner) -eq [System.Windows.Forms.DialogResult]::OK) {
        $script:picked = $dialog.SelectedPath
    }
    $owner.Close()
})
[System.Windows.Forms.Application]::Run($owner)
if ($script:picked) { [Console]::Out.Write($script:picked) }
"""


def _encoded_ps(script: str) -> str:
    """PowerShell -EncodedCommand expects base64 of UTF-16LE; using it avoids
    all shell/newline quoting concerns for a multi-line script."""
    import base64

    return base64.b64encode(script.encode("utf-16-le")).decode("ascii")


@router.post("/export-dir/browse")
async def browse_export_dir(request: Request):
    """Pop a native folder picker on the server's desktop and return the chosen
    path (null if the user cancelled). The client persists it via PUT."""
    if sys.platform != "win32":
        raise HTTPException(status_code=400, detail="the folder picker is only supported on Windows")

    env = dict(os.environ)
    env["FSEQ_INIT_DIR"] = str(_effective_exports_dir(request))
    try:
        proc = await asyncio.create_subprocess_exec(
            "powershell.exe", "-NoProfile", "-STA", "-ExecutionPolicy", "Bypass",
            "-EncodedCommand", _encoded_ps(_FOLDER_PICKER_PS),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        stdout, stderr = await proc.communicate()
    except (OSError, NotImplementedError) as exc:
        raise HTTPException(status_code=500, detail=f"could not open the folder picker: {exc}")

    if proc.returncode != 0:
        detail = stderr.decode("utf-8", "replace").strip() or "folder picker failed"
        raise HTTPException(status_code=500, detail=detail)

    picked = stdout.decode("utf-8", "replace").strip()
    return {"path": picked or None}
