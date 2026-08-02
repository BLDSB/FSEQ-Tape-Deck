"""Phase 3 verification for mixer.py.

Run directly: python tests/test_mixer.py
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fseq import FSEQReader, FSEQWriter
from mixer import ClipPlacement, Timeline, fade_envelope, render_frame_at, render_timeline


def make_constant_clip(path: Path, channel_count: int, step_ms: int, duration_s: float, value: int) -> None:
    frame_count = int(duration_s * 1000 / step_ms)
    with FSEQWriter(path, channel_count=channel_count, step_ms=step_ms) as writer:
        for _ in range(frame_count):
            writer.write_frame(bytes([value] * channel_count))


def test_fade_envelope_math():
    # No fades: always full.
    assert fade_envelope(local_ms=0, duration_ms=1000, fade_in_ms=0, fade_out_ms=0) == 1.0
    assert fade_envelope(local_ms=999, duration_ms=1000, fade_in_ms=0, fade_out_ms=0) == 1.0

    # Fade in: linear ramp 0 -> 1 over fade_in_ms.
    assert fade_envelope(local_ms=0, duration_ms=1000, fade_in_ms=500, fade_out_ms=0) == 0.0
    assert fade_envelope(local_ms=250, duration_ms=1000, fade_in_ms=500, fade_out_ms=0) == 0.5
    assert fade_envelope(local_ms=500, duration_ms=1000, fade_in_ms=500, fade_out_ms=0) == 1.0

    # Fade out: linear ramp 1 -> 0 over the last fade_out_ms.
    assert fade_envelope(local_ms=500, duration_ms=1000, fade_in_ms=0, fade_out_ms=500) == 1.0
    assert fade_envelope(local_ms=750, duration_ms=1000, fade_in_ms=0, fade_out_ms=500) == 0.5
    assert fade_envelope(local_ms=1000, duration_ms=1000, fade_in_ms=0, fade_out_ms=500) == 0.0

    # Overlapping fade in/out (short placement): envelope is the min of both ramps.
    env = fade_envelope(local_ms=100, duration_ms=200, fade_in_ms=150, fade_out_ms=150)
    assert 0.0 <= env <= 1.0
    print("OK: fade_envelope linear ramps and overlap clamping")


def test_htp_crossfade_render():
    channel_count = 512
    step_ms = 40  # 25 fps

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        clip_a_path = tmp / "a.fseq"
        clip_b_path = tmp / "b.fseq"
        make_constant_clip(clip_a_path, channel_count, step_ms, duration_s=10, value=100)
        make_constant_clip(clip_b_path, channel_count, step_ms, duration_s=10, value=200)

        # Clip A: 0s - 10s, fades out over its last 1s.
        # Clip B: starts at 8s (2s overlap with A's tail), fades in over its first 1s.
        placement_a = ClipPlacement(clip_id="a", start_ms=0, fade_in_ms=0, fade_out_ms=1000)
        placement_b = ClipPlacement(clip_id="b", start_ms=8000, fade_in_ms=1000, fade_out_ms=0)

        output_path = tmp / "out.fseq"
        result = render_timeline(
            placements=[placement_a, placement_b],
            clip_paths={"a": clip_a_path, "b": clip_b_path},
            channel_count=channel_count,
            step_ms=step_ms,
            output_path=output_path,
        )
        info = result.info

        # Both clips are lit end to end and butt up against 0, so the exporter's
        # loop-safety trim has nothing to remove and frame indices still map
        # straight onto timeline time.
        assert result.report.lead_trimmed_ms == 0
        assert result.report.tail_trimmed_ms == 0
        assert result.report.blackout_gaps == []

        assert info.channel_count == channel_count
        # Output spans 18s total (b ends at 8s+10s=18s), a ends at 10s.
        expected_frames = round(18000 / step_ms)
        assert abs(info.frame_count - expected_frames) <= 1

        reader = FSEQReader(output_path)
        try:
            def ch0(t_ms: int) -> int:
                idx = round(t_ms / step_ms)
                idx = max(0, min(reader.frame_count - 1, idx))
                return reader.read_frame(idx)[0]

            # Before any overlap: only A active, full value (100), HTP with nothing else.
            assert ch0(0) == 100
            assert ch0(4000) == 100

            # At the exact start of A's fade-out (t=9000ms, local_ms=0 in a 1000ms fade-out
            # window since A's placement duration is 10000ms): envelope ~1.0 still.
            print(f"ch0(9000)={ch0(9000)}")

            # Deep into the crossfade region (t=9500ms): A is fading out (~50%),
            # B is fading in (~50% through its own fade-in, local_ms=1500 > fade_in
            # so B is already at full 1.0 -- B's fade_in is only 1000ms starting at 8000ms,
            # so by 9500ms B is already fully up). HTP takes the higher scaled value.
            val_9500 = ch0(9500)
            print(f"ch0(9500)={val_9500}")
            # A at local_ms=9500 (duration 10000, fade_out 1000): ramp_out=(10000-9500)/1000=0.5 -> 100*0.5=50
            # B at local_ms=1500 (fade_in 1000, already past ramp): full 200
            # HTP max(50, 200) = 200
            assert val_9500 == 200

            # Right at B's start (t=8000ms): A still near full (local_ms=8000, ramp_out=(10000-8000)/1000
            # clamped to 1.0 -> full 100), B just starting fade-in (~0). HTP should favor A.
            val_8000 = ch0(8000)
            print(f"ch0(8000)={val_8000}")
            assert val_8000 == 100

            # Midpoint of B's fade-in while A is also fading out (t=8500ms):
            # A: local_ms=8500, ramp_out=(10000-8500)/1000=1.0 (clamped) -> 100
            # B: local_ms=500, ramp_in=500/1000=0.5 -> 200*0.5=100
            # HTP max(100,100)=100
            val_8500 = ch0(8500)
            print(f"ch0(8500)={val_8500}")
            assert val_8500 == 100

            # After A ends (t=15000ms), only B active at full value.
            assert ch0(15000) == 200

            # Near the very end (t=17999ms), still B, full value (no fade-out on B).
            assert ch0(17959) == 200
        finally:
            reader.close()

    print("OK: HTP merge + crossfade envelope math verified frame-by-frame on channel 0")


def test_crossfade_dissolve_has_no_htp_dip():
    """The whole point of the crossfade: a channel that is bright in BOTH clips
    must stay bright through the transition. HTP + opposing fades would dip it
    to ~half at the midpoint; a true dissolve holds it flat."""
    channel_count = 512
    step_ms = 40

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        clip_a_path = tmp / "a.fseq"
        clip_b_path = tmp / "b.fseq"
        # Same value (200) on every channel in both clips.
        make_constant_clip(clip_a_path, channel_count, step_ms, duration_s=10, value=200)
        make_constant_clip(clip_b_path, channel_count, step_ms, duration_s=10, value=200)

        # A: 0-10s. B: dissolves in over 8-10s (2s crossfade). Auto-arrange would
        # also clear A's fade_out and B's fade_in -- set here directly.
        placement_a = ClipPlacement(clip_id="a", start_ms=0, fade_out_ms=0)
        placement_b = ClipPlacement(clip_id="b", start_ms=8000, crossfade_ms=2000)

        clip_paths = {"a": clip_a_path, "b": clip_b_path}

        def ch0(t_ms):
            return render_frame_at([placement_a, placement_b], clip_paths, channel_count, t_ms)[0]

        # Across the entire crossfade window the value never dips below 200.
        for t in (8000, 8400, 8800, 9000, 9200, 9600, 9960):
            assert ch0(t) == 200, f"dip at t={t}: {ch0(t)} (HTP regression would give ~100)"
        # And after the transition, still 200 (B alone).
        assert ch0(12000) == 200

    print("OK: crossfade dissolve holds a mutually-bright channel flat (no HTP dip)")


def test_crossfade_dissolve_is_smooth_and_monotonic():
    """A channel that differs between clips moves smoothly and monotonically
    from the outgoing value to the incoming one, never overshooting either."""
    channel_count = 512
    step_ms = 40

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        clip_a_path = tmp / "a.fseq"
        clip_b_path = tmp / "b.fseq"
        make_constant_clip(clip_a_path, channel_count, step_ms, duration_s=10, value=200)
        make_constant_clip(clip_b_path, channel_count, step_ms, duration_s=10, value=60)

        placement_a = ClipPlacement(clip_id="a", start_ms=0, fade_out_ms=0)
        placement_b = ClipPlacement(clip_id="b", start_ms=8000, crossfade_ms=2000)
        clip_paths = {"a": clip_a_path, "b": clip_b_path}

        def ch0(t_ms):
            return render_frame_at([placement_a, placement_b], clip_paths, channel_count, t_ms)[0]

        # Endpoints: exactly the outgoing value at the start, incoming at the end.
        assert ch0(8000) == 200
        assert ch0(12000) == 60  # after the window, B alone

        samples = [ch0(t) for t in range(8000, 10000, 200)]
        # Monotonic non-increasing, and every value stays within [60, 200]
        # (a convex blend can never overshoot, so no dip/bump).
        for earlier, later in zip(samples, samples[1:]):
            assert later <= earlier, f"not monotonic: {samples}"
        assert all(60 <= v <= 200 for v in samples), samples

        # Smoothstep midpoint (w=0.5): 200*0.5 + 60*0.5 = 130.
        assert abs(ch0(9000) - 130) <= 1, ch0(9000)

    print("OK: crossfade dissolve is smooth, monotonic, and never overshoots")


def test_render_frame_at_matches_render_timeline():
    """render_frame_at (used for live playback preview) must agree with
    render_timeline's per-frame output -- it's the same merge logic, just
    called on demand for one timestamp instead of writing a whole file."""
    channel_count = 512
    step_ms = 40

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        clip_a_path = tmp / "a.fseq"
        clip_b_path = tmp / "b.fseq"
        make_constant_clip(clip_a_path, channel_count, step_ms, duration_s=10, value=100)
        make_constant_clip(clip_b_path, channel_count, step_ms, duration_s=10, value=220)

        placement_a = ClipPlacement(clip_id="a", start_ms=0, fade_out_ms=1000)
        placement_b = ClipPlacement(clip_id="b", start_ms=8000, fade_in_ms=1000)
        placements = [placement_a, placement_b]
        clip_paths = {"a": clip_a_path, "b": clip_b_path}

        output_path = tmp / "out.fseq"
        result = render_timeline(
            placements=placements,
            clip_paths=clip_paths,
            channel_count=channel_count,
            step_ms=step_ms,
            output_path=output_path,
        )
        # Frame index -> timeline time only holds while nothing was trimmed.
        assert result.report.lead_trimmed_ms == 0

        reader = FSEQReader(output_path)
        try:
            for t_ms in (0, 4000, 8000, 8480, 8760, 9000, 9500, 11000):
                idx = max(0, min(reader.frame_count - 1, round(t_ms / step_ms)))
                expected = reader.read_frame(idx)
                actual = render_frame_at(placements, clip_paths, channel_count, idx * step_ms)
                assert actual == expected, f"mismatch at t_ms={t_ms}: {actual[0]} != {expected[0]}"
        finally:
            reader.close()

    print("OK: render_frame_at agrees with render_timeline's per-frame output")


def _peaks(path: Path):
    reader = FSEQReader(path)
    try:
        return [max(reader.read_frame(i)) for i in range(reader.frame_count)]
    finally:
        reader.close()


def _wrap_jump(path: Path) -> int:
    """Biggest per-channel change from the last frame back to the first -- what
    a player shows at the instant it restarts the file."""
    reader = FSEQReader(path)
    try:
        last = reader.read_frame(reader.frame_count - 1)
        first = reader.read_frame(0)
        return max(abs(a - b) for a, b in zip(last, first))
    finally:
        reader.close()


def test_export_is_loop_safe():
    """Regression: an exported mix must not begin or end on a blackout.

    A clip dragged onto the timeline lands wherever the mouse was, so its
    start_ms is rarely exactly 0. That dead air used to render as black frames
    at the head of the file -- invisible on a single play, but every light in
    the rig blinks when FPP wraps around and restarts.
    """
    channel_count, step_ms = 512, 25

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        clip = tmp / "a.fseq"
        # Lit in every frame, so any darkness in the export came from the
        # timeline, never from the clip's own content.
        with FSEQWriter(clip, channel_count=channel_count, step_ms=40) as writer:
            for i in range(100):
                writer.write_frame(bytes([60 + (i * 2) % 180] * channel_count))
        clip_paths = {"a": clip}

        # The reported show: the same clip at the start and at the end, dropped
        # by mouse so the first one starts a few ms after zero.
        placements = [
            ClipPlacement(clip_id="a", start_ms=37),
            ClipPlacement(clip_id="a", start_ms=4037),
        ]

        plain = tmp / "plain.fseq"
        result = render_timeline(placements, clip_paths, channel_count, step_ms, plain)
        assert 0 not in _peaks(plain), "export still opens/closes on a blackout"
        assert result.report.lead_trimmed_ms == 50, result.report
        assert result.report.blackout_gaps == []
        assert result.report.is_loop_safe

        # Seamless loop: the tail dissolves back over the head, so the wrap
        # itself is invisible rather than merely non-black.
        looped = tmp / "looped.fseq"
        looped_result = render_timeline(
            placements, clip_paths, channel_count, step_ms, looped, loop_crossfade_ms=1000
        )
        assert 0 not in _peaks(looped)
        assert looped_result.report.loop_crossfade_ms == 1000
        assert looped_result.info.frame_count < result.info.frame_count  # crossfade costs length
        assert _wrap_jump(looped) < _wrap_jump(plain), "crossfade did not smooth the wrap"

        # A gap *between* clips can't be closed without shifting the show's
        # timing, so it must be reported rather than silently removed.
        gapped = tmp / "gapped.fseq"
        gap_result = render_timeline(
            [ClipPlacement(clip_id="a", start_ms=37),
             ClipPlacement(clip_id="a", start_ms=4337)],
            clip_paths, channel_count, step_ms, gapped,
        )
        assert gap_result.report.blackout_gaps == [(4050, 4350)], gap_result.report
        assert not gap_result.report.is_loop_safe
        assert 0 in _peaks(gapped)  # the interior gap is still there, as rendered

    print("OK: exports trim head/tail blackout, can seal the wrap, and flag interior gaps")


def test_timeline_persistence(tmp_path=None):
    import tempfile as _tempfile

    with _tempfile.TemporaryDirectory() as tmp:
        project_path = Path(tmp) / "project.json"
        timeline = Timeline(project_path=project_path)
        assert timeline.placements == []

        p = timeline.add_placement(clip_id="clip-1", start_ms=1000, fade_in_ms=200)
        assert project_path.exists()

        reloaded = Timeline(project_path=project_path)
        assert len(reloaded.placements) == 1
        assert reloaded.placements[0].clip_id == "clip-1"
        assert reloaded.placements[0].start_ms == 1000
        assert reloaded.placements[0].placement_id == p.placement_id

        reloaded.update_placement(p.placement_id, start_ms=2000, trim_end_ms=None)
        reloaded2 = Timeline(project_path=project_path)
        assert reloaded2.placements[0].start_ms == 2000

        reloaded2.remove_placement(p.placement_id)
        reloaded3 = Timeline(project_path=project_path)
        assert reloaded3.placements == []

    print("OK: Timeline persists placements to project.json across reloads")


if __name__ == "__main__":
    test_fade_envelope_math()
    test_htp_crossfade_render()
    test_crossfade_dissolve_has_no_htp_dip()
    test_crossfade_dissolve_is_smooth_and_monotonic()
    test_render_frame_at_matches_render_timeline()
    test_export_is_loop_safe()
    test_timeline_persistence()
    print("\nAll mixer tests passed.")
