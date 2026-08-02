"""sACN / ArtNet listener and FSEQ clip recorder.

Channel mapping is absolute and universe-based: universe U's 512 channels
occupy positions (U-1)*512 .. (U*512)-1 in a clip's flat channel space,
regardless of which other universes were selected for recording. A clip's
channel_count is therefore max(universes) * 512, with any in-between,
unselected universes left as zero.
"""

import argparse
import asyncio
import json
import socket
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union

import sacn

from fseq import FSEQWriter

SACN_PORT = 5568
ARTNET_PORT = 6454
ARTNET_ID = b"Art-Net\x00"
ARTNET_OPCODE_DMX = 0x5000
UNIVERSE_CHANNELS = 512

# Once the first universe's data arrives, how long to keep waiting for the
# remaining selected universes before starting anyway. Universes stream
# independently and land a few ms apart, so a brief wait means frame 0 has
# every universe in it; the cap means a console that only sends some of the
# selected universes still records instead of waiting forever.
PRIME_GRACE_MS = 250


class NoDataRecordedError(RuntimeError):
    """Raised by Recorder.stop() when no DMX ever arrived, so there are no
    frames to save."""


def list_local_ipv4_addresses() -> List[str]:
    """Best-effort list of this machine's non-loopback IPv4 addresses.

    On Windows especially, binding a multicast receiver to "0.0.0.0" can
    join the multicast group on the wrong network interface when multiple
    adapters are active (VPNs, WSL, virtual switches, ...), silently
    dropping a console's sACN packets. Offering the real interface IPs
    lets a user pick the one the console actually lives on.
    """
    addresses = set()
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            addresses.add(info[4][0])
    except socket.gaierror:
        pass
    return sorted(ip for ip in addresses if not ip.startswith("127.") and not ip.startswith("169.254."))


class UniverseBuffer:
    """Thread-safe store of the latest 512-channel frame per universe.

    A universe with no packets yet (or that has stopped receiving them)
    simply keeps its last-known values, which is exactly the "hold last
    frame" behavior the recorder needs for missed frames.
    """

    def __init__(self, universes: List[int]):
        self._lock = threading.Lock()
        self._data: Dict[int, bytearray] = {
            u: bytearray(UNIVERSE_CHANNELS) for u in universes
        }
        self._seen: set = set()

    def update(self, universe: int, values: bytes) -> None:
        with self._lock:
            if universe in self._data:
                n = min(len(values), UNIVERSE_CHANNELS)
                self._data[universe][:n] = values[:n]
                self._seen.add(universe)

    @property
    def has_data(self) -> bool:
        """Whether any selected universe has received data yet. Until this is
        true the buffer is still all zeros -- recording it would bake a
        blackout into the head of the clip."""
        with self._lock:
            return bool(self._seen)

    @property
    def is_primed(self) -> bool:
        """Whether *every* selected universe has received data, so a snapshot
        is a complete look rather than a partially-lit one."""
        with self._lock:
            return len(self._seen) == len(self._data)

    def snapshot_flat(self, channel_count: int) -> bytes:
        frame = bytearray(channel_count)
        with self._lock:
            for universe, values in self._data.items():
                offset = (universe - 1) * UNIVERSE_CHANNELS
                frame[offset : offset + UNIVERSE_CHANNELS] = values
        return bytes(frame)


class SACNListener:
    """Listens for sACN (E1.31) data on the given universes via the sacn lib."""

    def __init__(
        self,
        universes: List[int],
        buffer: UniverseBuffer,
        bind_address: str = "0.0.0.0",
    ):
        self.universes = universes
        self.buffer = buffer
        self._receiver = sacn.sACNreceiver(bind_address=bind_address)
        self.packet_count = 0
        self._count_lock = threading.Lock()

    def _on_packet(self, packet) -> None:
        self.buffer.update(packet.universe, bytes(packet.dmxData))
        with self._count_lock:
            self.packet_count += 1

    def start(self) -> None:
        for universe in self.universes:
            self._receiver.register_listener(
                "universe", self._on_packet, universe=universe
            )
            try:
                self._receiver.join_multicast(universe)
            except Exception as exc:
                # Multicast join can fail depending on the network interface
                # (common on Windows with multiple adapters/VPNs bound to
                # "0.0.0.0"); unicast/broadcast sources still get through
                # without it, so this is not fatal -- just surfaced for
                # troubleshooting in the server console.
                print(
                    f"[sACN] multicast join failed for universe {universe}: {exc} "
                    f"(unicast sources will still work; if your console sends "
                    f"multicast, try setting a specific network interface IP)"
                )
        self._receiver.start()

    def stop(self) -> None:
        self._receiver.stop()


class ArtNetListener:
    """Listens for raw ArtDMX packets on UDP port 6454.

    pyartnet is send-only (it targets fixtures/nodes), so ArtNet reception
    is done here via a raw UDP socket per the ArtDMX packet spec.
    """

    def __init__(
        self,
        universes: List[int],
        buffer: UniverseBuffer,
        bind_address: str = "0.0.0.0",
    ):
        self.universes = set(universes)
        self.buffer = buffer
        self.packet_count = 0
        self._count_lock = threading.Lock()
        self.bind_address = bind_address
        self._sock: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._running = False

    @staticmethod
    def _parse_artdmx(data: bytes) -> Optional[Tuple[int, bytes]]:
        if len(data) < 18 or data[:8] != ARTNET_ID:
            return None
        opcode = data[8] | (data[9] << 8)
        if opcode != ARTNET_OPCODE_DMX:
            return None
        universe = data[14] | (data[15] << 8)
        length = (data[16] << 8) | data[17]
        dmx_data = data[18 : 18 + length]
        return universe, dmx_data

    def _listen(self) -> None:
        self._sock.settimeout(0.5)
        while self._running:
            try:
                data, _addr = self._sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                break
            parsed = self._parse_artdmx(data)
            if parsed is None:
                continue
            universe, dmx_data = parsed
            if universe in self.universes:
                self.buffer.update(universe, dmx_data)
                with self._count_lock:
                    self.packet_count += 1

    def start(self) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((self.bind_address, ARTNET_PORT))
        self._running = True
        self._thread = threading.Thread(target=self._listen, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._sock:
            self._sock.close()
        if self._thread:
            self._thread.join(timeout=2)


@dataclass
class ClipMetadata:
    clip_id: str
    name: str
    universes: List[int]
    channel_count: int
    frame_count: int
    step_ms: int
    protocol: str
    created_at: str

    def to_dict(self) -> dict:
        return asdict(self)


class Recorder:
    """Coordinates a protocol listener + FSEQWriter to capture one clip."""

    def __init__(self, clips_dir: Union[str, Path] = "clips"):
        self.clips_dir = Path(clips_dir)
        self.clips_dir.mkdir(parents=True, exist_ok=True)

        self._listener: Optional[Union[SACNListener, ArtNetListener]] = None
        self._buffer: Optional[UniverseBuffer] = None
        self._writer: Optional[FSEQWriter] = None
        self._clip_path: Optional[Path] = None
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._waiting_for_data = False

        self.clip_id: Optional[str] = None
        self.name: Optional[str] = None
        self.universes: List[int] = []
        self.step_ms: int = 40
        self.protocol: str = "sacn"
        self.channel_count: int = 0
        self.frame_count: int = 0
        self.started_at: Optional[float] = None

    @property
    def is_recording(self) -> bool:
        return self._running

    def current_frame(self) -> Optional[bytes]:
        """The live in-memory buffer snapshot -- exactly what would be
        written on the next tick. Used to preview incoming console data
        while a recording is in progress, without touching the clip file."""
        if not self._running or self._buffer is None:
            return None
        return self._buffer.snapshot_flat(self.channel_count)

    def status(self) -> dict:
        elapsed_ms = 0
        if self._running and self.started_at is not None:
            elapsed_ms = int((time.monotonic() - self.started_at) * 1000)
        return {
            "recording": self._running,
            "clip_name": self.name,
            "frame_count": self.frame_count,
            "elapsed_ms": elapsed_ms,
            # Armed, but holding off the first frame until DMX actually
            # arrives -- see _await_first_data().
            "waiting_for_data": self._waiting_for_data,
        }

    async def start(
        self,
        name: str,
        universes: List[int],
        step_ms: int = 40,
        protocol: str = "sacn",
        bind_address: Optional[str] = None,
    ) -> None:
        if self._running:
            raise RuntimeError("a recording is already in progress")
        if not universes:
            raise ValueError("universes must be a non-empty list")
        if not (0 <= step_ms <= 255):
            raise ValueError(f"step_ms must fit in a single byte (0-255), got {step_ms}")

        self.clip_id = str(uuid.uuid4())
        self.name = name
        self.universes = sorted(universes)
        self.step_ms = step_ms
        self.protocol = protocol
        self.channel_count = max(self.universes) * UNIVERSE_CHANNELS
        self.frame_count = 0

        bind_address = bind_address or "0.0.0.0"
        self._buffer = UniverseBuffer(self.universes)
        if protocol == "sacn":
            self._listener = SACNListener(self.universes, self._buffer, bind_address=bind_address)
        elif protocol == "artnet":
            self._listener = ArtNetListener(self.universes, self._buffer, bind_address=bind_address)
        else:
            raise ValueError(f"unknown protocol: {protocol!r}")
        self._listener.start()

        try:
            self._clip_path = self.clips_dir / f"{self.clip_id}.fseq"
            self._writer = FSEQWriter(
                self._clip_path, channel_count=self.channel_count, step_ms=self.step_ms
            )
        except Exception:
            # Don't leave the listener's socket/thread running if the clip
            # file couldn't be created.
            self._listener.stop()
            self._listener = None
            raise

        self._running = True
        self._waiting_for_data = True
        self.started_at = time.monotonic()
        self._task = asyncio.create_task(self._record_loop())

    async def _await_first_data(self) -> None:
        """Hold off the first frame until real DMX is flowing.

        A listener does not start delivering the instant it is created: the
        socket has to bind and (for sACN) join multicast, which can take up to
        about a second. Recording through that startup captures the buffer's
        initial zeros, baking a blackout into the head of the clip -- which
        shows up as every light blinking each time the finished sequence wraps
        around and restarts. Waiting for the first packet means frame 0 is
        real, lit data.
        """
        while self._running and not self._buffer.has_data:
            await asyncio.sleep(0.005)
        # First universe is in; give the rest a moment to land so frame 0 is a
        # complete look rather than a partially-lit one.
        deadline = time.monotonic() + PRIME_GRACE_MS / 1000.0
        while self._running and not self._buffer.is_primed and time.monotonic() < deadline:
            await asyncio.sleep(0.005)

    async def _record_loop(self) -> None:
        await self._await_first_data()
        self._waiting_for_data = False
        # Elapsed time counts from the first recorded frame, not from arming,
        # so it stays in step with frame_count.
        self.started_at = time.monotonic()

        interval = self.step_ms / 1000.0
        next_tick = time.monotonic()
        while self._running:
            next_tick += interval
            frame = self._buffer.snapshot_flat(self.channel_count)
            self._writer.write_frame(frame)
            self.frame_count += 1
            delay = next_tick - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            else:
                next_tick = time.monotonic()  # fell behind; resync rather than free-run

    async def stop(self) -> ClipMetadata:
        if not self._running:
            raise RuntimeError("no recording is in progress")
        self._running = False
        self._waiting_for_data = False
        if self._task:
            await self._task

        if self.frame_count == 0 and self._buffer.has_data:
            # Stopped in the window between the first packet landing and the
            # record loop's first tick. Data did arrive, so capture it rather
            # than throwing away a (very short) recording.
            self._writer.write_frame(self._buffer.snapshot_flat(self.channel_count))
            self.frame_count = 1

        self._listener.stop()
        self._writer.close()

        if self.frame_count == 0:
            # Stopped while still waiting for the first packet -- there is no
            # clip to save, and an empty .fseq would only break later.
            if self._clip_path is not None:
                self._clip_path.unlink(missing_ok=True)
            raise NoDataRecordedError(
                f"no {self.protocol} data arrived on universe(s) "
                f"{', '.join(str(u) for u in self.universes)}, so nothing was recorded"
            )

        metadata = ClipMetadata(
            clip_id=self.clip_id,
            name=self.name,
            universes=self.universes,
            channel_count=self.channel_count,
            frame_count=self.frame_count,
            step_ms=self.step_ms,
            protocol=self.protocol,
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        meta_path = self.clips_dir / f"{self.clip_id}.json"
        meta_path.write_text(json.dumps(metadata.to_dict(), indent=2))
        return metadata


def _cli_test_mode(universe: int, protocol: str) -> None:
    buffer = UniverseBuffer([universe])
    listener: Union[SACNListener, ArtNetListener]
    if protocol == "sacn":
        listener = SACNListener([universe], buffer)
    else:
        listener = ArtNetListener([universe], buffer)
    listener.start()
    print(f"Listening for {protocol} on universe {universe}. Ctrl+C to stop.")
    last_count = 0
    try:
        while True:
            time.sleep(1.0)
            count = listener.packet_count
            print(f"universe {universe}: {count - last_count} packets/sec (total {count})")
            last_count = count
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        listener.stop()


def main() -> None:
    parser = argparse.ArgumentParser(description="FSEQ Tapedeck recorder")
    parser.add_argument(
        "--test",
        action="store_true",
        help="listen-only test mode: prints incoming packet counts once per second",
    )
    parser.add_argument("--universe", type=int, default=1)
    parser.add_argument("--protocol", choices=["sacn", "artnet"], default="sacn")
    args = parser.parse_args()

    if args.test:
        _cli_test_mode(universe=args.universe, protocol=args.protocol)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
