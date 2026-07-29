"""Timeline model and HTP render engine.

A timeline is a list of clip placements. Rendering walks the output frame
clock, resolves which placements are active at each frame, applies each
one's fade envelope, and merges overlapping clips with HTP (highest value
per channel wins). Channel alignment across clips relies on the same
absolute universe-based channel mapping used by recorder.py, so channel
index N always means the same real-world DMX address in every clip.
"""

import json
import math
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, List, Optional, Union

import numpy as np

from fseq import FSEQInfo, FSEQReader, FSEQWriter


@dataclass
class ClipPlacement:
    clip_id: str
    start_ms: int
    fade_in_ms: int = 0
    fade_out_ms: int = 0
    trim_start_ms: int = 0
    trim_end_ms: Optional[int] = None
    # When > 0, this placement dissolves in from whatever is playing beneath it
    # over its first crossfade_ms, instead of HTP-merging. A true dissolve
    # (A*(1-w) + B*w) moves every channel smoothly from the outgoing look to
    # this one with no HTP dip/bump. Supersedes fade_in_ms during that window.
    crossfade_ms: int = 0
    placement_id: str = ""

    def __post_init__(self):
        if not self.placement_id:
            self.placement_id = str(uuid.uuid4())


def _smoothstep(u: float) -> float:
    """Ease-in/ease-out 0->1 ramp. Used to weight a crossfade so the dissolve
    has no abrupt change in rate at either end of the transition."""
    u = max(0.0, min(1.0, u))
    return u * u * (3.0 - 2.0 * u)


def fade_envelope(local_ms: float, duration_ms: float, fade_in_ms: int, fade_out_ms: int) -> float:
    """Linear ramp up during fade_in, linear ramp down during fade_out, 1.0 between.

    Computing both ramps and taking their minimum handles the case where
    fade_in and fade_out overlap (a placement shorter than fade_in+fade_out)
    without a separate special case.
    """
    ramp_in = 1.0 if fade_in_ms <= 0 else min(1.0, local_ms / fade_in_ms)
    ramp_out = 1.0 if fade_out_ms <= 0 else min(1.0, (duration_ms - local_ms) / fade_out_ms)
    return max(0.0, min(ramp_in, ramp_out))


class Timeline:
    """Manages timeline placements and persists them to project.json."""

    def __init__(self, project_path: Union[str, Path] = "project.json"):
        self.project_path = Path(project_path)
        self.placements: List[ClipPlacement] = []
        self.settings: dict = {"channel_count": 512, "step_ms": 40}
        if self.project_path.exists():
            self._load()

    def _load(self) -> None:
        data = json.loads(self.project_path.read_text())
        self.placements = [ClipPlacement(**p) for p in data.get("placements", [])]
        self.settings = data.get("settings", self.settings)

    def save(self) -> None:
        data = {
            "placements": [asdict(p) for p in self.placements],
            "settings": self.settings,
        }
        self.project_path.write_text(json.dumps(data, indent=2))

    def _find(self, placement_id: str) -> ClipPlacement:
        for p in self.placements:
            if p.placement_id == placement_id:
                return p
        raise KeyError(f"no placement with id {placement_id!r}")

    def add_placement(
        self,
        clip_id: str,
        start_ms: int,
        fade_in_ms: int = 0,
        fade_out_ms: int = 0,
        trim_start_ms: int = 0,
        trim_end_ms: Optional[int] = None,
    ) -> ClipPlacement:
        placement = ClipPlacement(
            clip_id=clip_id,
            start_ms=start_ms,
            fade_in_ms=fade_in_ms,
            fade_out_ms=fade_out_ms,
            trim_start_ms=trim_start_ms,
            trim_end_ms=trim_end_ms,
        )
        self.placements.append(placement)
        self.save()
        return placement

    def update_placement(self, placement_id: str, **fields) -> ClipPlacement:
        """Apply only the fields present in `fields` (including explicit None)."""
        placement = self._find(placement_id)
        for key, value in fields.items():
            setattr(placement, key, value)
        self.save()
        return placement

    def remove_placement(self, placement_id: str) -> None:
        self._find(placement_id)  # raise if missing
        self.placements = [p for p in self.placements if p.placement_id != placement_id]
        self.save()


def _placement_duration_ms(placement: ClipPlacement, clip_frame_count: int, clip_step_ms: int) -> int:
    clip_duration_ms = clip_frame_count * clip_step_ms
    end_ms = placement.trim_end_ms if placement.trim_end_ms is not None else clip_duration_ms
    return max(0, end_ms - placement.trim_start_ms)


def _compute_windows(placements: List[ClipPlacement], readers: Dict[str, FSEQReader]):
    """Each placement's active (placement, start_ms, duration_ms) window, plus
    the overall timeline duration implied by them (0 if there are none)."""
    windows = []
    end_times_ms = [0]
    for placement in placements:
        reader = readers[placement.clip_id]
        duration_ms = _placement_duration_ms(placement, reader.frame_count, reader.step_ms)
        if duration_ms <= 0:
            continue
        windows.append((placement, placement.start_ms, duration_ms))
        end_times_ms.append(placement.start_ms + duration_ms)
    return windows, max(end_times_ms)


def _scaled_layer(
    placement: ClipPlacement,
    reader: FSEQReader,
    channel_count: int,
    t_ms: float,
    start_ms: float,
    duration_ms: float,
    fade_in_override: Optional[int] = None,
) -> np.ndarray:
    """One placement's fade-scaled frame at t_ms, aligned onto the output
    channel space (absolute universe mapping means index N is the same real
    channel across clips of different sizes). fade_in_override lets a crossfade
    suppress the placement's own fade-in for the incoming edge."""
    local_ms = t_ms - start_ms
    source_ms = placement.trim_start_ms + local_ms
    source_frame_idx = int(round(source_ms / reader.step_ms))
    source_frame_idx = max(0, min(reader.frame_count - 1, source_frame_idx))

    source_arr = np.frombuffer(reader.read_frame(source_frame_idx), dtype=np.uint8)
    aligned = np.zeros(channel_count, dtype=np.uint8)
    n = min(len(source_arr), channel_count)
    aligned[:n] = source_arr[:n]

    fade_in = placement.fade_in_ms if fade_in_override is None else fade_in_override
    envelope = fade_envelope(local_ms, duration_ms, fade_in, placement.fade_out_ms)
    return np.clip(aligned.astype(np.float64) * envelope, 0, 255).astype(np.uint8)


def _compute_merged_frame(windows, readers: Dict[str, FSEQReader], channel_count: int, t_ms: float) -> np.ndarray:
    """The single frame active at t_ms: normal placements HTP-merge as before;
    a placement inside its crossfade window instead dissolves over the mix
    beneath it, so transitions move smoothly with no HTP dip or bump."""
    base = np.zeros(channel_count, dtype=np.uint8)
    crossfading = []

    for placement, start_ms, duration_ms in windows:
        if not (start_ms <= t_ms < start_ms + duration_ms):
            continue

        cf_ms = getattr(placement, "crossfade_ms", 0) or 0
        if cf_ms > 0 and t_ms < start_ms + cf_ms:
            crossfading.append((start_ms, placement, duration_ms))
            continue

        scaled = _scaled_layer(
            placement, readers[placement.clip_id], channel_count, t_ms, start_ms, duration_ms
        )
        base = np.maximum(base, scaled)  # HTP merge (unchanged for normal clips)

    if not crossfading:
        return base

    # Dissolve each incoming placement over everything beneath it. Applied in
    # start order so stacked crossfades compose predictably.
    acc = base.astype(np.float64)
    for start_ms, placement, duration_ms in sorted(crossfading, key=lambda w: w[0]):
        incoming = _scaled_layer(
            placement, readers[placement.clip_id], channel_count, t_ms, start_ms, duration_ms,
            fade_in_override=0,  # the crossfade replaces the incoming fade-in
        ).astype(np.float64)
        w = _smoothstep((t_ms - start_ms) / placement.crossfade_ms)
        acc = acc * (1.0 - w) + incoming * w

    return np.clip(np.round(acc), 0, 255).astype(np.uint8)


def render_timeline(
    placements: List[ClipPlacement],
    clip_paths: Dict[str, Union[str, Path]],
    channel_count: int,
    step_ms: int,
    output_path: Union[str, Path],
) -> FSEQInfo:
    """Render a timeline of clip placements to a single output FSEQ file.

    clip_paths maps clip_id -> path to that clip's recorded FSEQ file.
    """
    readers: Dict[str, FSEQReader] = {}
    try:
        for placement in placements:
            if placement.clip_id not in readers:
                readers[placement.clip_id] = FSEQReader(clip_paths[placement.clip_id])

        windows, total_duration_ms = _compute_windows(placements, readers)
        total_frames = max(1, math.ceil(total_duration_ms / step_ms)) if total_duration_ms > 0 else 0

        writer = FSEQWriter(output_path, channel_count=channel_count, step_ms=step_ms)
        try:
            for frame_idx in range(total_frames):
                t_ms = frame_idx * step_ms
                merged = _compute_merged_frame(windows, readers, channel_count, t_ms)
                writer.write_frame(merged.tobytes())
        finally:
            writer.close()

        return FSEQInfo(
            path=str(output_path),
            channel_count=channel_count,
            frame_count=total_frames,
            step_ms=step_ms,
            duration_seconds=(total_frames * step_ms) / 1000.0,
        )
    finally:
        for reader in readers.values():
            reader.close()


def render_frame_at(
    placements: List[ClipPlacement],
    clip_paths: Dict[str, Union[str, Path]],
    channel_count: int,
    t_ms: float,
) -> bytes:
    """Compute a single HTP-merged frame at time t_ms without writing a file.

    Used for scrubbing/playback preview in the UI, where re-rendering a full
    export on every tick would be wasteful.
    """
    readers: Dict[str, FSEQReader] = {}
    try:
        for placement in placements:
            if placement.clip_id not in readers:
                readers[placement.clip_id] = FSEQReader(clip_paths[placement.clip_id])
        windows, _total_duration_ms = _compute_windows(placements, readers)
        merged = _compute_merged_frame(windows, readers, channel_count, t_ms)
        return merged.tobytes()
    finally:
        for reader in readers.values():
            reader.close()


def placement_duration_ms(placement: ClipPlacement, clip_path: Union[str, Path]) -> int:
    """A single placement's on-timeline duration in ms, after trims."""
    reader = FSEQReader(clip_path)
    try:
        return _placement_duration_ms(placement, reader.frame_count, reader.step_ms)
    finally:
        reader.close()


def timeline_duration_ms(
    placements: List[ClipPlacement],
    clip_paths: Dict[str, Union[str, Path]],
) -> float:
    """The timeline's overall duration in ms (0 if there are no placements)."""
    readers: Dict[str, FSEQReader] = {}
    try:
        for placement in placements:
            if placement.clip_id not in readers:
                readers[placement.clip_id] = FSEQReader(clip_paths[placement.clip_id])
        _windows, total_duration_ms = _compute_windows(placements, readers)
        return total_duration_ms
    finally:
        for reader in readers.values():
            reader.close()
