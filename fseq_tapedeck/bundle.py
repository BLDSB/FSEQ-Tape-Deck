"""Pack the whole working project -- every recording plus the timeline -- into
a portable ``.ftdproj`` archive (a zip), and import one back.

A project owns its recordings: packing includes *all* clips in the library, not
just the ones placed on the timeline, and importing replaces the current
recordings + timeline with the bundle's. Inside the archive, clip files are
named by their friendly clip name (deduped) so the bundle is browsable; each
clip's ``.json`` sidecar still carries its real ``clip_id``, which is what
import keys on, so timeline placements resolve regardless of the filenames.

Bundle layout::

    project.json          the timeline (placements + settings + name)
    manifest.json         format/version + clip_id -> file map (informational)
    clips/
      Ambient.fseq        friendly-named copies...
      Ambient.json          ...each sidecar carries the real clip_id
"""

import json
import re
import zipfile
from pathlib import Path
from typing import List, Union

from clip_store import ClipNotFoundError, ClipStore

BUNDLE_FORMAT = "ftdproj-bundle"
BUNDLE_VERSION = 2
BUNDLE_SUFFIX = ".ftdproj"

# Settings that describe the project itself travel in the bundle; machine-local
# preferences (where exports go, the open file's display name) do not.
_PORTABLE_SETTINGS = ("channel_count", "step_ms")

_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _safe_filename(name: str, fallback: str) -> str:
    cleaned = _UNSAFE.sub("_", name or "").strip().strip(".")
    return cleaned or fallback


def _unique(base: str, used: set) -> str:
    """A case-insensitively unique filename stem, suffixing ' (2)', ' (3)'..."""
    candidate = base
    i = 2
    while candidate.lower() in used:
        candidate = f"{base} ({i})"
        i += 1
    used.add(candidate.lower())
    return candidate


def _portable_settings(settings: dict) -> dict:
    return {k: settings[k] for k in _PORTABLE_SETTINGS if k in settings}


def pack_project(
    clip_store: ClipStore,
    name: str,
    placements: List[dict],
    settings: dict,
    output_path: Union[str, Path],
) -> dict:
    """Write a portable bundle of the whole project (all library clips + the
    given timeline) to ``output_path``. Returns a summary."""
    output_path = Path(output_path)
    if output_path.suffix.lower() != BUNDLE_SUFFIX:
        output_path = output_path.with_suffix(BUNDLE_SUFFIX)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    packed = []
    used_names = set()

    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:
        project_doc = {
            "format": BUNDLE_FORMAT,
            "version": BUNDLE_VERSION,
            "name": name,
            "settings": _portable_settings(settings),
            "placements": placements,
        }
        zf.writestr("project.json", json.dumps(project_doc, indent=2))

        for meta in clip_store.list_clips():  # every recording, not just placed ones
            clip_id = meta.get("clip_id")
            if not clip_id:
                continue
            try:
                fseq_path = clip_store.get_clip_path(clip_id)
            except ClipNotFoundError:
                continue
            stem = _unique(_safe_filename(meta.get("name", clip_id), clip_id), used_names)
            zf.write(fseq_path, f"clips/{stem}.fseq")
            zf.writestr(f"clips/{stem}.json", json.dumps(meta, indent=2))
            packed.append({"clip_id": clip_id, "file": f"clips/{stem}.fseq"})

        manifest = {
            "format": BUNDLE_FORMAT,
            "version": BUNDLE_VERSION,
            "project": name,
            "clips": packed,
        }
        zf.writestr("manifest.json", json.dumps(manifest, indent=2))

    return {"path": str(output_path), "name": name, "clip_count": len(packed)}


def import_bundle(clip_store: ClipStore, bundle_path: Union[str, Path]) -> dict:
    """Replace the library with a bundle's recordings and return its timeline.

    The bundle is validated before anything is cleared, so a bad file can't wipe
    the current project. Returns the project's placements/settings/name plus the
    number of clips imported.
    """
    bundle_path = Path(bundle_path)
    if not bundle_path.exists():
        raise FileNotFoundError(f"no bundle at {bundle_path}")

    with zipfile.ZipFile(bundle_path, "r") as zf:
        names = set(zf.namelist())
        if "project.json" not in names:
            raise ValueError("not a valid project bundle (missing project.json)")
        project_doc = json.loads(zf.read("project.json"))

        # Validated -- now it's safe to replace the current recordings.
        clip_store.clear()
        imported = 0
        clip_store.clips_dir.mkdir(parents=True, exist_ok=True)
        for entry in sorted(names):
            if not (entry.startswith("clips/") and entry.endswith(".json")):
                continue
            meta = json.loads(zf.read(entry))
            clip_id = meta.get("clip_id")
            fseq_entry = entry[:-5] + ".fseq"
            if not clip_id or fseq_entry not in names:
                continue
            (clip_store.clips_dir / f"{clip_id}.fseq").write_bytes(zf.read(fseq_entry))
            (clip_store.clips_dir / f"{clip_id}.json").write_text(json.dumps(meta, indent=2))
            imported += 1

    return {
        "name": project_doc.get("name", bundle_path.stem),
        "placements": project_doc.get("placements", []),
        "settings": project_doc.get("settings", {}),
        "clips_imported": imported,
    }
