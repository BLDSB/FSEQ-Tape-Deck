"""FSEQ V2 (uncompressed) binary reader/writer.

Header layout (32 bytes total, see project spec):

Offset  Size   Field
------  -----  -----
0       4      Magic "PSEQ"
4       2      Channel data offset (uint16 LE) -- always 32
6       1      Minor version -- 0
7       1      Major version -- 2
8       2      Header length (uint16 LE) -- always 32
10      4      Channel count per frame (uint32 LE)
14      4      Total frame count (uint32 LE)
18      1      Step time, ms
19      1      Flags -- 0 (no compression)
20      2      Universe count -- 0 (ignored by FPP)
22      2      Universe size -- 0 (ignored by FPP)
24      1      Gamma -- 1
25      1      Color order -- 2 (RGB)
26      2      Reserved -- 0
28      4      Reserved -- 0 (padding to reach the 32-byte header length)
"""

import struct
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Iterator, Optional, Tuple, Union

MAGIC = b"PSEQ"
HEADER_LENGTH = 32
DATA_OFFSET = 32
MINOR_VERSION = 0
MAJOR_VERSION = 2
GAMMA = 1
COLOR_ORDER = 2

_HEADER_STRUCT = struct.Struct("<4sHBBHIIBBHHBBHI")
# < little-endian, no padding
#   4s magic, H data_offset, B minor, B major, H header_len,
#   I channel_count, I frame_count, B step_ms, B flags,
#   H universe_count, H universe_size, B gamma, B color_order,
#   H reserved26, I reserved28


@dataclass
class FSEQInfo:
    path: str
    channel_count: int
    frame_count: int
    step_ms: int
    duration_seconds: float


def _pack_header(channel_count: int, frame_count: int, step_ms: int) -> bytes:
    return _HEADER_STRUCT.pack(
        MAGIC,
        DATA_OFFSET,
        MINOR_VERSION,
        MAJOR_VERSION,
        HEADER_LENGTH,
        channel_count,
        frame_count,
        step_ms,
        0,  # flags
        0,  # universe count
        0,  # universe size
        GAMMA,
        COLOR_ORDER,
        0,  # reserved26
        0,  # reserved28
    )


class FSEQWriter:
    """Writes an FSEQ V2 uncompressed file, one frame at a time."""

    def __init__(self, path: Union[str, Path], channel_count: int, step_ms: int):
        if channel_count <= 0:
            raise ValueError("channel_count must be positive")
        if not (0 <= step_ms <= 255):
            raise ValueError("step_ms must fit in a single byte (0-255)")

        self.path = Path(path)
        self.channel_count = channel_count
        self.step_ms = step_ms
        self.frame_count = 0
        self._closed = False

        self._file: BinaryIO = open(self.path, "wb")
        self._file.write(_pack_header(channel_count, 0, step_ms))

    def write_frame(self, data: bytes) -> None:
        if self._closed:
            raise RuntimeError("cannot write to a closed FSEQWriter")
        if len(data) != self.channel_count:
            raise ValueError(
                f"frame has {len(data)} bytes, expected {self.channel_count}"
            )
        self._file.write(data)
        self.frame_count += 1

    def close(self) -> None:
        if self._closed:
            return
        # Patch the frame count now that we know the final value.
        self._file.seek(14)
        self._file.write(struct.pack("<I", self.frame_count))
        self._file.flush()
        self._file.close()
        self._closed = True

    def __enter__(self) -> "FSEQWriter":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()


def content_frame_range(reader: "FSEQReader") -> Tuple[int, int]:
    """``(first, last)`` inclusive frame indices of a sequence's lit content,
    i.e. with leading and trailing all-zero frames excluded. ``(0, -1)`` if
    every frame is dark.

    A dark frame at either end of a sequence is invisible on a single play but
    makes the whole rig blink every time a player wraps around and restarts,
    so anything built to loop trims to this range first.
    """
    first, last = -1, -1
    for i, frame in enumerate(reader.iter_frames()):
        if max(frame) != 0:
            if first < 0:
                first = i
            last = i
    return (0, -1) if first < 0 else (first, last)


class FSEQReader:
    """Reads an FSEQ V2 uncompressed file."""

    def __init__(self, path: Union[str, Path]):
        self.path = Path(path)
        self._file: BinaryIO = open(self.path, "rb")

        raw_header = self._file.read(HEADER_LENGTH)
        if len(raw_header) < HEADER_LENGTH:
            raise ValueError(f"{self.path} is too small to contain an FSEQ header")

        (
            magic,
            data_offset,
            minor,
            major,
            header_len,
            channel_count,
            frame_count,
            step_ms,
            flags,
            universe_count,
            universe_size,
            gamma,
            color_order,
            _reserved26,
            _reserved28,
        ) = _HEADER_STRUCT.unpack(raw_header)

        if magic != MAGIC:
            raise ValueError(f"{self.path} is not an FSEQ file (bad magic: {magic!r})")
        if major != MAJOR_VERSION:
            raise ValueError(f"unsupported FSEQ major version: {major}")
        if flags != 0:
            raise ValueError("compressed FSEQ files are not supported")

        self.data_offset = data_offset
        self.minor_version = minor
        self.major_version = major
        self.header_length = header_len
        self.channel_count = channel_count
        self.frame_count = frame_count
        self.step_ms = step_ms
        self.gamma = gamma
        self.color_order = color_order

    @property
    def duration_seconds(self) -> float:
        return (self.frame_count * self.step_ms) / 1000.0

    @property
    def info(self) -> FSEQInfo:
        return FSEQInfo(
            path=str(self.path),
            channel_count=self.channel_count,
            frame_count=self.frame_count,
            step_ms=self.step_ms,
            duration_seconds=self.duration_seconds,
        )

    def read_frame(self, n: int) -> bytes:
        if not (0 <= n < self.frame_count):
            raise IndexError(f"frame {n} out of range (0-{self.frame_count - 1})")
        offset = self.data_offset + n * self.channel_count
        self._file.seek(offset)
        data = self._file.read(self.channel_count)
        if len(data) != self.channel_count:
            raise ValueError(f"truncated frame {n} in {self.path}")
        return data

    def iter_frames(self) -> Iterator[bytes]:
        self._file.seek(self.data_offset)
        for _ in range(self.frame_count):
            data = self._file.read(self.channel_count)
            if len(data) != self.channel_count:
                raise ValueError(f"truncated frame data in {self.path}")
            yield data

    def close(self) -> None:
        self._file.close()

    def __enter__(self) -> "FSEQReader":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()
