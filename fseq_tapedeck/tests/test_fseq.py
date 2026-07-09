"""Phase 1 verification: write a multi-frame FSEQ file and read it back exactly.

Run directly: python tests/test_fseq.py
"""

import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fseq import FSEQReader, FSEQWriter, DATA_OFFSET, HEADER_LENGTH, MAGIC


def ramp_frame(frame_index: int, channel_count: int) -> bytes:
    return bytes((frame_index + c) % 256 for c in range(channel_count))


def test_write_and_read_back(tmp_path):
    channel_count = 512
    step_ms = 25  # 40 fps
    duration_seconds = 5
    frame_count = int(duration_seconds * 1000 / step_ms)

    path = tmp_path / "ramp.fseq"

    with FSEQWriter(path, channel_count=channel_count, step_ms=step_ms) as writer:
        for i in range(frame_count):
            writer.write_frame(ramp_frame(i, channel_count))

    # Raw file size sanity check per spec: 32 + channel_count * frame_count
    expected_size = HEADER_LENGTH + channel_count * frame_count
    assert path.stat().st_size == expected_size, (
        f"file size {path.stat().st_size} != expected {expected_size}"
    )

    with open(path, "rb") as f:
        magic = f.read(4)
        assert magic == MAGIC
        f.seek(4)
        (data_offset,) = struct.unpack("<H", f.read(2))
        assert data_offset == DATA_OFFSET
        f.seek(10)
        (ch_count,) = struct.unpack("<I", f.read(4))
        (frm_count,) = struct.unpack("<I", f.read(4))
        assert ch_count == channel_count
        assert frm_count == frame_count

    reader = FSEQReader(path)
    try:
        assert reader.channel_count == channel_count
        assert reader.frame_count == frame_count
        assert reader.step_ms == step_ms
        assert reader.data_offset == DATA_OFFSET
        assert abs(reader.duration_seconds - duration_seconds) < 1e-9

        # Spot-check a handful of frames via read_frame
        for i in (0, 1, frame_count // 2, frame_count - 1):
            assert reader.read_frame(i) == ramp_frame(i, channel_count)

        # Full sequential integrity check via iter_frames
        for i, frame in enumerate(reader.iter_frames()):
            expected = ramp_frame(i, channel_count)
            assert frame == expected, f"frame {i} mismatch"
        assert i == frame_count - 1

        # Out-of-range frame access should raise
        try:
            reader.read_frame(frame_count)
        except IndexError:
            pass
        else:
            raise AssertionError("expected IndexError for out-of-range frame")
    finally:
        reader.close()

    print(f"OK: wrote/read {frame_count} frames x {channel_count} channels "
          f"({expected_size} bytes) round-tripped correctly")


if __name__ == "__main__":
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        test_write_and_read_back(Path(tmp))
