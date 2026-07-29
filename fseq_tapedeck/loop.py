"""Generate a seamless looping clip from a recording.

Two loop styles, both jump-free at the seam, each producing a brand-new clip
(its own id + metadata sidecar) and leaving the source recording untouched:

* ``pingpong`` -- plays the clip forward then back toward the start (frames
  ``0..N-1`` then ``N-2..1``). Every step is between two physically adjacent
  recorded frames, so there is provably no color/brightness jump anywhere,
  including the wrap: each turning point is a smooth pendulum reversal with no
  stalled or duplicated frame. Motion reverses on the return trip, which is
  invisible on symmetric/ambient looks but noticeable on directional ones.
  The result is ``2N-2`` frames.

* ``crossfade`` -- keeps motion moving forward the whole time by dissolving the
  clip's tail back over its head. The last ``crossfade_ms`` are blended into
  the first ``crossfade_ms`` with a smoothstep weight so the wrap is seamless.
  It costs a little content (the loop is ``N - crossfade_frames`` long) and
  blends the start and end together, but never reverses direction.
"""

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Union

import numpy as np

from clip_store import ClipStore
from fseq import FSEQReader, FSEQWriter

LOOP_MODES = ("pingpong", "crossfade")


def _smoothstep(u: float) -> float:
    """Ease-in/ease-out 0->1 ramp, so a crossfade has no abrupt change in rate
    at either end of the blend (same weighting mixer.py uses for dissolves)."""
    u = max(0.0, min(1.0, u))
    return u * u * (3.0 - 2.0 * u)


def pingpong_indices(frame_count: int) -> List[int]:
    """Source frame order for a forward-then-back loop: ``0..N-1`` then
    ``N-2..1``. The endpoints (``N-1`` and ``0``) each appear once, so when the
    result is repeated every join is between two adjacent recorded frames --
    a smooth pendulum reversal at each turning point, never a duplicated
    frame or a jump."""
    if frame_count <= 1:
        return list(range(frame_count))
    return list(range(frame_count)) + list(range(frame_count - 2, 0, -1))


def _write_pingpong(reader: FSEQReader, writer: FSEQWriter) -> int:
    indices = pingpong_indices(reader.frame_count)
    for i in indices:
        writer.write_frame(reader.read_frame(i))
    return len(indices)


def _write_crossfade(reader: FSEQReader, writer: FSEQWriter, crossfade_ms: int) -> int:
    """Dissolve the tail back over the head so the loop wraps seamlessly with
    forward-only motion. See module docstring for the tradeoffs."""
    n = reader.frame_count
    step_ms = reader.step_ms
    if n < 2:
        raise ValueError("clip is too short to build a crossfade loop")
    if crossfade_ms <= 0:
        raise ValueError("crossfade length must be positive")

    cf = max(1, round(crossfade_ms / step_ms))
    # Cap the overlap at half the clip so the loop keeps a positive length and
    # the seam stays well behaved.
    cf = min(cf, n // 2)
    loop_len = n - cf  # frames in the finished loop

    for i in range(loop_len):
        if i < cf:
            # Blend the head frame i with the overlapped tail frame that wraps
            # onto it; weight ramps 0->1 so the tail hands off to the head.
            head = np.frombuffer(reader.read_frame(i), dtype=np.uint8).astype(np.float64)
            tail = np.frombuffer(reader.read_frame(i + loop_len), dtype=np.uint8).astype(np.float64)
            w = _smoothstep((i + 1) / cf)
            blended = np.clip(np.round(tail * (1.0 - w) + head * w), 0, 255).astype(np.uint8)
            writer.write_frame(blended.tobytes())
        else:
            writer.write_frame(reader.read_frame(i))
    return loop_len


def render_loop(
    source_path: Union[str, Path],
    output_path: Union[str, Path],
    mode: str,
    crossfade_ms: int = 0,
) -> int:
    """Write a looped FSEQ built from ``source_path`` to ``output_path``.

    Returns the frame count of the written loop. The output inherits the
    source clip's channel count and step time.
    """
    if mode not in LOOP_MODES:
        raise ValueError(f"unknown loop mode: {mode!r}")

    reader = FSEQReader(source_path)
    try:
        if reader.frame_count <= 0:
            raise ValueError("cannot loop an empty clip")
        writer = FSEQWriter(
            output_path, channel_count=reader.channel_count, step_ms=reader.step_ms
        )
        try:
            if mode == "pingpong":
                return _write_pingpong(reader, writer)
            return _write_crossfade(reader, writer, crossfade_ms)
        finally:
            writer.close()
    finally:
        reader.close()


def make_loop_clip(
    clip_store: ClipStore,
    source_clip_id: str,
    name: str,
    mode: str,
    crossfade_ms: int = 0,
) -> dict:
    """Build a new looping clip in the library from an existing recording.

    Writes ``{new_id}.fseq`` + ``{new_id}.json`` into the clip store's
    directory, copying the source clip's universes/channel_count/step_ms/
    protocol so the loop behaves like any other clip. Returns the new clip's
    metadata dict.
    """
    source_meta = clip_store.get_clip(source_clip_id)  # raises ClipNotFoundError
    source_path = clip_store.get_clip_path(source_clip_id)

    new_id = str(uuid.uuid4())
    output_path = clip_store.clips_dir / f"{new_id}.fseq"
    try:
        frame_count = render_loop(source_path, output_path, mode, crossfade_ms)
    except Exception:
        # Don't leave a half-written .fseq with no metadata sidecar behind.
        if output_path.exists():
            output_path.unlink()
        raise

    metadata = {
        "clip_id": new_id,
        "name": name,
        "universes": source_meta.get("universes", []),
        "channel_count": source_meta.get("channel_count", 0),
        "frame_count": frame_count,
        "step_ms": source_meta.get("step_ms", 0),
        "protocol": source_meta.get("protocol", "sacn"),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    (clip_store.clips_dir / f"{new_id}.json").write_text(json.dumps(metadata, indent=2))
    return metadata
