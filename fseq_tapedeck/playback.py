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
    """Walks a timeline in real time, rendering and transmitting one frame
    per step_ms via sACN. Independent of any client's own playhead clock --
    a browser's visual playhead is just a display of the same timeline, not
    the thing driving this output."""

    def __init__(self):
        self._sender: Optional[sacn.sACNsender] = None
        self._task: Optional[asyncio.Task] = None
        self._running = False

        self.channel_count = 0
        self.step_ms = 40
        self.total_ms = 0.0
        self.t_ms = 0.0
        self.destination: Optional[str] = None

    @property
    def is_playing(self) -> bool:
        return self._running

    def status(self) -> dict:
        return {
            "playing": self._running,
            "t_ms": self.t_ms,
            "total_ms": self.total_ms,
        }

    async def start(
        self,
        placements: List[ClipPlacement],
        clip_paths: Dict[str, Union[str, Path]],
        channel_count: int,
        step_ms: int,
        total_ms: float,
        destination: Optional[str] = None,
        start_t_ms: float = 0.0,
    ) -> None:
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

        universes = _universe_count(channel_count)
        self._sender = sacn.sACNsender(source_name="FSEQ Tapedeck", fps=40)
        self._sender.start()
        for universe in range(1, universes + 1):
            self._sender.activate_output(universe)
            if self.destination:
                self._sender[universe].destination = self.destination
            else:
                self._sender[universe].multicast = True

        self._running = True
        self._task = asyncio.create_task(
            self._playback_loop(placements, clip_paths)
        )

    async def _playback_loop(
        self,
        placements: List[ClipPlacement],
        clip_paths: Dict[str, Union[str, Path]],
    ) -> None:
        interval = self.step_ms / 1000.0
        next_tick = time.monotonic()
        universes = _universe_count(self.channel_count)
        try:
            while self._running and self.t_ms < self.total_ms:
                frame = render_frame_at(placements, clip_paths, self.channel_count, self.t_ms)

                for universe in range(1, universes + 1):
                    start = (universe - 1) * UNIVERSE_CHANNELS
                    chunk = frame[start : start + UNIVERSE_CHANNELS]
                    if len(chunk) < UNIVERSE_CHANNELS:
                        chunk = chunk + bytes(UNIVERSE_CHANNELS - len(chunk))
                    self._sender[universe].dmx_data = tuple(chunk)

                self.t_ms += self.step_ms
                next_tick += interval
                delay = next_tick - time.monotonic()
                if delay > 0:
                    await asyncio.sleep(delay)
                else:
                    next_tick = time.monotonic()  # fell behind; resync rather than free-run
        finally:
            self._running = False
            if self._sender:
                self._sender.stop()
                self._sender = None

    async def stop(self) -> None:
        if not self._running:
            return
        self._running = False  # the loop notices on its next iteration and exits
        if self._task:
            await self._task
        self._task = None
