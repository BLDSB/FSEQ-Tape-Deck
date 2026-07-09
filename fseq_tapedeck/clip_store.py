"""Manages the library of recorded clips.

recorder.py writes each clip as `{clip_id}.fseq` plus a `{clip_id}.json`
metadata sidecar in the clips directory. ClipStore is the read/query/mutate
layer on top of that directory -- it does not know how to record, only how
to list, look up, rename, and delete what's already there.
"""

import json
from pathlib import Path
from typing import List, Union


class ClipNotFoundError(KeyError):
    pass


class ClipStore:
    def __init__(self, clips_dir: Union[str, Path] = "clips"):
        self.clips_dir = Path(clips_dir)
        self.clips_dir.mkdir(parents=True, exist_ok=True)

    def _meta_path(self, clip_id: str) -> Path:
        return self.clips_dir / f"{clip_id}.json"

    def _fseq_path(self, clip_id: str) -> Path:
        return self.clips_dir / f"{clip_id}.fseq"

    def list_clips(self) -> List[dict]:
        clips = []
        for meta_path in sorted(self.clips_dir.glob("*.json")):
            clips.append(json.loads(meta_path.read_text()))
        clips.sort(key=lambda c: c.get("created_at", ""))
        return clips

    def get_clip(self, clip_id: str) -> dict:
        meta_path = self._meta_path(clip_id)
        if not meta_path.exists():
            raise ClipNotFoundError(clip_id)
        return json.loads(meta_path.read_text())

    def get_clip_path(self, clip_id: str) -> Path:
        fseq_path = self._fseq_path(clip_id)
        if not fseq_path.exists():
            raise ClipNotFoundError(clip_id)
        return fseq_path

    def rename_clip(self, clip_id: str, name: str) -> dict:
        metadata = self.get_clip(clip_id)
        metadata["name"] = name
        self._meta_path(clip_id).write_text(json.dumps(metadata, indent=2))
        return metadata

    def delete_clip(self, clip_id: str) -> None:
        meta_path = self._meta_path(clip_id)
        if not meta_path.exists():
            raise ClipNotFoundError(clip_id)
        meta_path.unlink()
        fseq_path = self._fseq_path(clip_id)
        if fseq_path.exists():
            fseq_path.unlink()
