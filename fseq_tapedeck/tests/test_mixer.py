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
        info = render_timeline(
            placements=[placement_a, placement_b],
            clip_paths={"a": clip_a_path, "b": clip_b_path},
            channel_count=channel_count,
            step_ms=step_ms,
            output_path=output_path,
        )

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
        render_timeline(
            placements=placements,
            clip_paths=clip_paths,
            channel_count=channel_count,
            step_ms=step_ms,
            output_path=output_path,
        )

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
    test_render_frame_at_matches_render_timeline()
    test_timeline_persistence()
    print("\nAll mixer tests passed.")
