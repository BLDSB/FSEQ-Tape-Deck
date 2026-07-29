"""Live sACN playback output: streams the rendered timeline mix out over the
network in real time, e.g. so a lighting console's visualizer can show it.

Defaults to unicast (a specific destination IP) rather than multicast --
multicast group membership can silently join the wrong network interface on
machines with multiple adapters (VPNs, WSL, virtual switches), the same
class of issue recorder.py's bind_address option works around for input.
"""

import asyncio
import math
import time
from pathlib import Path
from typing import Dict, List, Optional, Union

import sacn

from mixer import ClipPlacement, render_frame_at

UNIVERSE_CHANNELS = 512


def _universe_count(channel_count: int) -> int:
    return max(1, math.ceil(channel_count / UNIVERSE_CHANNELS))


class PlaybackEngine:
    """Streams a timeline out over sACN so a console's visualizer can show it.

    Two independent pieces of state:

    * ``_active`` -- the sACN source is up and continuously transmitting the
      current frame (the `sacn` library resends the last ``dmx_data`` at its
      configured fps, so a *held* frame keeps streaming with no gaps).
    * ``_running`` -- the real-time advance loop is walking the timeline,
      rendering a new frame every ``step_ms``.

    This split lets the same live source *hold* a single frame while the user
    scrubs or pauses, and *advance* while they play, without ever dropping the
    sACN source (a source dropout can make a console fall back to its own
    look). The engine is independent of any client's playhead clock -- a
    browser's visual playhead is just a display of the same timeline."""

    def __init__(self):
        self._sender: Optional[sacn.sACNsender] = None
        self._task: Optional[asyncio.Task] = None
        self._running = False  # advance loop active (Play)
        self._active = False   # sACN source is up (output enabled)

        self.channel_count = 0
        self.step_ms = 40
        self.total_ms = 0.0
        self.t_ms = 0.0
        self.destination: Optional[str] = None

        self._placements: List[ClipPlacement] = []
        self._clip_paths: Dict[str, Union[str, Path]] = {}
        self._sender_channel_count = 0
        self._sender_destination: Optional[str] = None

    @property
    def is_playing(self) -> bool:
        return self._running

    @property
    def is_active(self) -> bool:
        return self._active

    def status(self) -> dict:
        return {
            "playing": self._running,
            "active": self._active,
            "t_ms": self.t_ms,
            "total_ms": self.total_ms,
            "channel_count": self.channel_count,
            "destination": self.destination,
        }

    def _ensure_sender(self, channel_count: int, destination: Optional[str]) -> None:
        """Bring the sACN source up, reopening it if the channel count or
        destination changed since it was last opened."""
        destination = destination or None
        if (
            self._sender is not None
            and self._sender_channel_count == channel_count
            and self._sender_destination == destination
        ):
            return
        if self._sender is not None:
            self._sender.stop()
            self._sender = None

        universes = _universe_count(channel_count)
        sender = sacn.sACNsender(source_name="FSEQ Tapedeck", fps=40)
        sender.start()
        for universe in range(1, universes + 1):
            sender.activate_output(universe)
            if destination:
                sender[universe].destination = destination
            else:
                sender[universe].multicast = True
        self._sender = sender
        self._sender_channel_count = channel_count
        self._sender_destination = destination

    def _send_frame(self, t_ms: float) -> None:
        if self._sender is None:
            return
        frame = render_frame_at(self._placements, self._clip_paths, self.channel_count, t_ms)
        universes = _universe_count(self.channel_count)
        for universe in range(1, universes + 1):
            start = (universe - 1) * UNIVERSE_CHANNELS
            chunk = frame[start : start + UNIVERSE_CHANNELS]
            if len(chunk) < UNIVERSE_CHANNELS:
                chunk = chunk + bytes(UNIVERSE_CHANNELS - len(chunk))
            self._sender[universe].dmx_data = tuple(chunk)

    async def start(
        self,
        placements: List[ClipPlacement],
        clip_paths: Dict[str, Union[str, Path]],
        channel_count: int,
        step_ms: int,
        total_ms: float,
        destination: Optional[str] = None,
        start_t_ms: float = 0.0,
        play: bool = True,
    ) -> None:
        """Bring the live source up at ``start_t_ms``. With ``play=True`` the
        engine then advances in real time; with ``play=False`` it holds that
        single frame (used for scrubbing/paused output)."""
        if self._running:
            raise RuntimeError("playback is already running")
        if channel_count <= 0:
            raise ValueError("channel_count must be positive")
        if not (0 < step_ms <= 1000):
            raise ValueError(f"step_ms out of range (1-1000), got {step_ms}")

        self.channel_count = channel_count
        self.step_ms = step_ms
        self.total_ms = total_ms
        self.destination = destination or None
        self.t_ms = max(0.0, start_t_ms)
        self._placements = placements
        self._clip_paths = clip_paths

        self._ensure_sender(channel_count, self.destination)
        self._active = True
        self._send_frame(self.t_ms)

        if play:
            self._running = True
            self._task = asyncio.create_task(self._playback_loop())

    async def scrub(
        self,
        placements: List[ClipPlacement],
        clip_paths: Dict[str, Union[str, Path]],
        t_ms: float,
    ) -> None:
        """Move the held playhead and transmit that frame. No-op on the loop
        while advancing -- Play owns the position then."""
        if not self._active:
            raise RuntimeError("output is not active")
        self._placements = placements
        self._clip_paths = clip_paths
        self.t_ms = max(0.0, t_ms)
        if not self._running:
            self._send_frame(self.t_ms)

    async def pause(self) -> None:
        """Stop advancing but keep the source up, holding the current frame."""
        if not self._running:
            return
        self._running = False  # the loop notices on its next iteration and exits
        if self._task:
            await self._task
            self._task = None
        self._send_frame(self.t_ms)

    async def _playback_loop(self) -> None:
        interval = self.step_ms / 1000.0
        next_tick = time.monotonic()
        try:
            while self._running and self.t_ms < self.total_ms:
                self._send_frame(self.t_ms)
                self.t_ms += self.step_ms
                next_tick += interval
                delay = next_tick - time.monotonic()
                if delay > 0:
                    await asyncio.sleep(delay)
                else:
                    next_tick = time.monotonic()  # fell behind; resync rather than free-run
        finally:
            self._running = False  # reached the end; source stays up holding the final look

    async def stop(self) -> None:
        """Tear the live source down completely."""
        self._running = False  # the loop notices on its next iteration and exits
        if self._task:
            await self._task
            self._task = None
        self._active = False
        if self._sender:
            self._sender.stop()
            self._sender = None
        self._sender_channel_count = 0
        self._sender_destination = None
