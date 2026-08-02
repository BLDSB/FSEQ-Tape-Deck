"""Verify seamless-loop generation (ping-pong + crossfade).

Run directly: python tests/test_loop.py
"""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from clip_store import ClipStore
from fseq import FSEQReader, FSEQWriter
from loop import make_loop_clip, pingpong_indices, render_loop

CHANNEL_COUNT = 4
STEP_MS = 40


def _write_source(path: Path, frame_count: int, lead_black: int = 0, trail_black: int = 0) -> None:
    """A clip whose channel 0 counts up frame-by-frame, so a frame's identity
    is easy to check by its first byte.

    Levels start at 1, not 0: a zero frame is a blackout, which render_loop
    deliberately trims (see test_loop_trims_blackout_frames). lead_black /
    trail_black bracket the content with real blackout frames to exercise
    that trimming.
    """
    with FSEQWriter(path, channel_count=CHANNEL_COUNT, step_ms=STEP_MS) as writer:
        for _ in range(lead_black):
            writer.write_frame(bytes(CHANNEL_COUNT))
        for i in range(frame_count):
            frame = bytes([(i + 1) % 256, 0, 0, 0])
            writer.write_frame(frame)
        for _ in range(trail_black):
            writer.write_frame(bytes(CHANNEL_COUNT))


def _read_all(path: Path):
    reader = FSEQReader(path)
    try:
        return [reader.read_frame(i) for i in range(reader.frame_count)]
    finally:
        reader.close()


def test_pingpong_indices():
    assert pingpong_indices(0) == []
    assert pingpong_indices(1) == [0]
    assert pingpong_indices(2) == [0, 1]
    assert pingpong_indices(5) == [0, 1, 2, 3, 4, 3, 2, 1]  # 2N-2 = 8
    # No index appears twice in a row (no stalled/duplicated frame), and the
    # wrap (last -> first) is also between adjacent source frames.
    idx = pingpong_indices(6)
    seq = idx + [idx[0]]
    assert all(abs(a - b) == 1 for a, b in zip(seq, seq[1:]))
    print("OK: pingpong_indices produces a jump-free pendulum order")


def test_render_pingpong():
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "src.fseq"
        out = Path(tmp) / "out.fseq"
        _write_source(src, 5)

        frame_count = render_loop(src, out, mode="pingpong")
        assert frame_count == 8, frame_count

        frames = _read_all(out)
        first_bytes = [f[0] for f in frames]
        assert first_bytes == [1, 2, 3, 4, 5, 4, 3, 2]

        # Every adjacent pair (including the wrap) moves by exactly one recorded
        # frame -- the guarantee of no color/brightness jump.
        wrapped = first_bytes + [first_bytes[0]]
        assert all(abs(a - b) == 1 for a, b in zip(wrapped, wrapped[1:]))

        reader = FSEQReader(out)
        try:
            assert reader.channel_count == CHANNEL_COUNT
            assert reader.step_ms == STEP_MS
        finally:
            reader.close()
    print("OK: ping-pong render is seamless and inherits clip channel/step")


def test_render_crossfade():
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "src.fseq"
        out = Path(tmp) / "out.fseq"
        _write_source(src, 10)

        # crossfade_ms=120, step=40 -> cf=3 frames; loop_len = 10 - 3 = 7.
        frame_count = render_loop(src, out, mode="crossfade", crossfade_ms=120)
        assert frame_count == 7, frame_count

        frames = _read_all(out)
        # Frames past the blend region (i >= cf) are copied verbatim.
        for i in range(3, 7):
            assert frames[i][0] == i + 1, (i, frames[i][0])
        # Blended frames stay within the range of the two sources they mix.
        for i in range(3):
            lo, hi = sorted((i + 1, i + 8))
            assert lo <= frames[i][0] <= hi, (i, frames[i][0])
    print("OK: crossfade render trims to N-cf and blends within source range")


def test_loop_trims_blackout_frames():
    """Regression: a clip that opens (or ends) dark must not loop through that
    blackout.

    Recordings made before the recorder waited for its listener to come up hold
    the buffer's initial zeros for up to ~1s, and a dark frame at either end
    makes the whole rig blink once per wrap no matter which loop mode is used.
    """
    for mode, kwargs, expected in (("pingpong", {}, 18), ("crossfade", {"crossfade_ms": 120}, 7)):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src.fseq"
            out = Path(tmp) / "out.fseq"
            _write_source(src, 10, lead_black=24, trail_black=3)

            frame_count = render_loop(src, out, mode=mode, **kwargs)
            assert frame_count == expected, (mode, frame_count)

            frames = _read_all(out)
            dark = [i for i, f in enumerate(frames) if max(f) == 0]
            assert not dark, f"{mode} loop still contains blackout frames at {dark}"

    # A clip with no lit frames at all has nothing to loop -- say so clearly
    # rather than emitting a loop that is entirely black.
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "dark.fseq"
        _write_source(src, 0, lead_black=10)
        try:
            render_loop(src, Path(tmp) / "out.fseq", mode="pingpong")
            assert False, "expected ValueError for an all-dark clip"
        except ValueError:
            pass
    print("OK: loops trim leading/trailing blackout frames (no blink at the wrap)")


def test_make_loop_clip():
    with tempfile.TemporaryDirectory() as tmp:
        clips_dir = Path(tmp) / "clips"
        store = ClipStore(clips_dir)

        # Seed a source clip the way the recorder would: .fseq + .json sidecar.
        source_id = "src-clip"
        _write_source(clips_dir / f"{source_id}.fseq", 6)
        (clips_dir / f"{source_id}.json").write_text(json.dumps({
            "clip_id": source_id,
            "name": "Ambient",
            "universes": [1],
            "channel_count": CHANNEL_COUNT,
            "frame_count": 6,
            "step_ms": STEP_MS,
            "protocol": "sacn",
            "created_at": "2026-01-01T00:00:00+00:00",
        }))

        meta = make_loop_clip(store, source_id, name="Ambient (loop)", mode="pingpong")
        assert meta["name"] == "Ambient (loop)"
        assert meta["clip_id"] != source_id
        assert meta["frame_count"] == 10  # 2*6-2
        assert meta["universes"] == [1]
        assert meta["channel_count"] == CHANNEL_COUNT
        assert meta["step_ms"] == STEP_MS
        assert meta["protocol"] == "sacn"

        # The new clip is a first-class library member (both files present).
        assert (clips_dir / f"{meta['clip_id']}.fseq").exists()
        assert store.get_clip(meta["clip_id"])["name"] == "Ambient (loop)"
        # Source clip is untouched.
        assert store.get_clip(source_id)["frame_count"] == 6

        # Error handling.
        try:
            make_loop_clip(store, "nope", name="x", mode="pingpong")
            assert False, "expected ClipNotFoundError"
        except KeyError:
            pass
        try:
            render_loop(clips_dir / f"{source_id}.fseq", clips_dir / "bad.fseq", mode="bogus")
            assert False, "expected ValueError for unknown mode"
        except ValueError:
            pass
    print("OK: make_loop_clip writes a new library clip and leaves the source alone")


if __name__ == "__main__":
    test_pingpong_indices()
    test_render_pingpong()
    test_render_crossfade()
    test_loop_trims_blackout_frames()
    test_make_loop_clip()
    print("\nAll loop tests passed.")
