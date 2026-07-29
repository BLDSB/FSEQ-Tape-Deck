"""REST endpoints for the single working project: new / open / save.

A project is the whole working state -- every recording in the library plus the
timeline -- saved as one portable ``.ftdproj`` bundle. There is no separate
project library: New clears everything, Open replaces everything from a bundle,
Save writes everything to a bundle. The open bundle's display name rides in the
timeline settings (``project_name``) so it survives a restart.
"""

from dataclasses import asdict
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from bundle import BUNDLE_SUFFIX, import_bundle, pack_project
from native_dialogs import open_file_dialog, save_file_dialog

router = APIRouter(prefix="/api/project", tags=["project"])

_BUNDLE_FILTER = "FSEQ Tapedeck project (*.ftdproj)|*.ftdproj|All files (*.*)|*.*"


class OpenSaveRequest(BaseModel):
    # An explicit path bypasses the native dialog (used by tests and any
    # non-interactive caller); omit it to pop the dialog.
    path: Optional[str] = None


def _timeline_state(timeline) -> dict:
    return {
        "placements": [asdict(p) for p in timeline.placements],
        "settings": timeline.settings,
    }


def _default_dialog_dir(request: Request) -> str:
    """Where the file dialogs start: the chosen export folder if set, else the
    app's default exports dir."""
    configured = request.app.state.timeline.settings.get("export_dir")
    return str(configured) if configured else str(request.app.state.exports_dir)


@router.get("")
async def project_status(request: Request):
    return {"current": request.app.state.timeline.settings.get("project_name")}


@router.post("/new")
async def new_project(request: Request):
    """Start a fresh, empty project: delete every recording and clear the
    timeline."""
    request.app.state.clip_store.clear()
    request.app.state.timeline.clear()
    return {"current": None, **_timeline_state(request.app.state.timeline)}


@router.post("/save")
async def save_project(body: OpenSaveRequest, request: Request):
    """Pack the whole project (all recordings + timeline) to a .ftdproj bundle.
    With no explicit path, pops a native Save As dialog on the server's
    desktop."""
    clip_store = request.app.state.clip_store
    timeline = request.app.state.timeline
    current = timeline.settings.get("project_name") or "Untitled"

    output_path = body.path
    if not output_path:
        try:
            output_path = await save_file_dialog(
                title="Save project",
                filter_str=_BUNDLE_FILTER,
                initial_dir=_default_dialog_dir(request),
                initial_name=f"{current}{BUNDLE_SUFFIX}",
            )
        except RuntimeError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        if not output_path:
            return {"cancelled": True}

    stem = Path(output_path).stem
    placements = [asdict(p) for p in timeline.placements]
    try:
        result = pack_project(clip_store, stem, placements, timeline.settings, output_path)
    except OSError as exc:
        raise HTTPException(status_code=400, detail=f"could not write bundle: {exc}")

    timeline.settings["project_name"] = stem
    timeline.save()
    return {"saved": True, "current": stem, **result}


@router.post("/open")
async def open_project(body: OpenSaveRequest, request: Request):
    """Open a .ftdproj bundle, replacing all current recordings + the timeline.
    With no explicit path, pops a native Open dialog on the server's desktop."""
    clip_store = request.app.state.clip_store
    timeline = request.app.state.timeline

    bundle_path = body.path
    if not bundle_path:
        try:
            bundle_path = await open_file_dialog(
                title="Open project",
                filter_str=_BUNDLE_FILTER,
                initial_dir=_default_dialog_dir(request),
            )
        except RuntimeError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        if not bundle_path:
            return {"cancelled": True}

    try:
        result = import_bundle(clip_store, bundle_path)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except (ValueError, OSError) as exc:
        raise HTTPException(status_code=400, detail=f"could not open project: {exc}")

    # The file the user picked is the project's identity; keep the local export
    # folder preference rather than whatever the bundle was made with.
    stem = Path(bundle_path).stem
    settings = dict(result["settings"])
    if "export_dir" in timeline.settings:
        settings["export_dir"] = timeline.settings["export_dir"]
    settings["project_name"] = stem
    timeline.replace_state(result["placements"], settings)

    return {
        "opened": True,
        "current": stem,
        "clips_imported": result["clips_imported"],
        **_timeline_state(timeline),
    }
