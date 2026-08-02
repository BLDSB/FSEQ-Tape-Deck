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
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Union

import numpy as np

from fseq import FSEQInfo, FSEQReader, FSEQWriter
from loop import ClipView, write_crossfade_loop


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

    def replace_state(self, placements: List[dict], settings: dict) -> None:
        """Load a saved project's timeline into this (working) timeline,
        replacing whatever was open, and persist it as the working copy."""
        self.placements = [ClipPlacement(**p) for p in placements]
        self.settings = dict(settings)
        self.save()

    def clear(self) -> None:
        """Empty the working timeline to a fresh, untitled project."""
        self.placements = []
        self.settings.pop("project_name", None)
        self.save()

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


@dataclass
class ExportReport:
    """What the exporter had to change to make the file safe to loop, and what
    it could not fix on its own.

    A player like FPP restarts the file the instant it ends, so any dark frame
    at the head or tail lands right next to lit content at the wrap and reads
    as the whole rig blinking. Those are trimmed. Dark stretches *between*
    clips can't be closed without changing the show's timing, so they are
    reported for the user to fix on the timeline.
    """

    lead_trimmed_ms: int = 0
    tail_trimmed_ms: int = 0
    loop_crossfade_ms: int = 0
    # (start_ms, end_ms) of each interior blackout, in original timeline time.
    blackout_gaps: List[tuple] = field(default_factory=list)

    @property
    def is_loop_safe(self) -> bool:
        return not self.blackout_gaps


@dataclass
class ExportResult:
    info: FSEQInfo
    report: ExportReport


def _scan_blackouts(reader: FSEQReader):
    """One pass over a rendered mix: ``(first, last, interior_runs)`` where
    first/last bound the lit content and interior_runs are ``(start, end)``
    inclusive frame ranges of darkness *inside* that content."""
    dark = [max(frame) == 0 for frame in reader.iter_frames()]
    lit = [i for i, is_dark in enumerate(dark) if not is_dark]
    if not lit:
        return 0, -1, []

    first, last = lit[0], lit[-1]
    runs, run_start = [], None
    for i in range(first, last + 1):
        if dark[i] and run_start is None:
            run_start = i
        elif not dark[i] and run_start is not None:
            runs.append((run_start, i - 1))
            run_start = None
    return first, last, runs


def render_timeline(
    placements: List[ClipPlacement],
    clip_paths: Dict[str, Union[str, Path]],
    channel_count: int,
    step_ms: int,
    output_path: Union[str, Path],
    loop_crossfade_ms: int = 0,
) -> ExportResult:
    """Render a timeline of clip placements to a single output FSEQ file.

    clip_paths maps clip_id -> path to that clip's recorded FSEQ file.

    The result is built to loop: leading/trailing blackout frames are dropped
    (a clip dropped by mouse rarely lands exactly on 0, and that dead air is
    what makes a rig blink when the player wraps), and with
    ``loop_crossfade_ms`` > 0 the mix's tail is dissolved back over its head so
    the wrap itself is seamless. See ExportReport.
    """
    # Render the full timeline to a temp file first: trimming the head and
    # dissolving the tail over the head both need random access to the
    # finished mix, and the mix is too large to hold in memory.
    output_path = Path(output_path)
    temp_path = output_path.with_name(output_path.name + ".rendering")

    readers: Dict[str, FSEQReader] = {}
    try:
        for placement in placements:
            if placement.clip_id not in readers:
                readers[placement.clip_id] = FSEQReader(clip_paths[placement.clip_id])

        windows, total_duration_ms = _compute_windows(placements, readers)
        total_frames = max(1, math.ceil(total_duration_ms / step_ms)) if total_duration_ms > 0 else 0

        writer = FSEQWriter(temp_path, channel_count=channel_count, step_ms=step_ms)
        try:
            for frame_idx in range(total_frames):
                t_ms = frame_idx * step_ms
                merged = _compute_merged_frame(windows, readers, channel_count, t_ms)
                writer.write_frame(merged.tobytes())
        finally:
            writer.close()
    finally:
        for reader in readers.values():
            reader.close()

    try:
        return _finalize_export(temp_path, output_path, step_ms, loop_crossfade_ms)
    finally:
        temp_path.unlink(missing_ok=True)


def _finalize_export(
    temp_path: Path, output_path: Path, step_ms: int, loop_crossfade_ms: int
) -> ExportResult:
    """Trim the rendered mix to its lit content, optionally dissolve its tail
    back over its head, and write the finished file."""
    report = ExportReport()
    reader = FSEQReader(temp_path)
    try:
        first, last, runs = _scan_blackouts(reader)
        report.lead_trimmed_ms = first * step_ms
        report.tail_trimmed_ms = max(0, reader.frame_count - 1 - last) * step_ms
        report.blackout_gaps = [(a * step_ms, (b + 1) * step_ms) for a, b in runs]

        source = ClipView(reader, first, max(0, last - first + 1))
        writer = FSEQWriter(
            output_path, channel_count=reader.channel_count, step_ms=reader.step_ms
        )
        try:
            if loop_crossfade_ms > 0 and source.frame_count >= 2:
                frame_count = write_crossfade_loop(source, writer, loop_crossfade_ms)
                report.loop_crossfade_ms = loop_crossfade_ms
            else:
                for i in range(source.frame_count):
                    writer.write_frame(source.read_frame(i))
                frame_count = source.frame_count
        finally:
            writer.close()

        return ExportResult(
            info=FSEQInfo(
                path=str(output_path),
                channel_count=reader.channel_count,
                frame_count=frame_count,
                step_ms=step_ms,
                duration_seconds=(frame_count * step_ms) / 1000.0,
            ),
            report=report,
        )
    finally:
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
