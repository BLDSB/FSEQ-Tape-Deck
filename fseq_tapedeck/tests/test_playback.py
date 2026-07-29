"""Verification for playback.py: streams a rendered timeline out over real
sACN (loopback) and confirms a receiver actually sees the merged, faded
values changing over time.

Run directly: python tests/test_playback.py
"""

import asyncio
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import sacn

from fseq import FSEQWriter
from mixer import ClipPlacement
from playback import PlaybackEngine


def make_constant_clip(path: Path, channel_count: int, step_ms: int, duration_s: float, value: int) -> None:
    frame_count = int(duration_s * 1000 / step_ms)
    with FSEQWriter(path, channel_count=channel_count, step_ms=step_ms) as writer:
        for _ in range(frame_count):
            writer.write_frame(bytes([value] * channel_count))


def test_playback_engine_streams_real_sacn_over_loopback():
    channel_count = 512
    step_ms = 40

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        clip_a_path = tmp / "a.fseq"
        clip_b_path = tmp / "b.fseq"
        make_constant_clip(clip_a_path, channel_count, step_ms, duration_s=4, value=100)
        make_constant_clip(clip_b_path, channel_count, step_ms, duration_s=4, value=220)

        # A: 0-2s at 100. B: starts at 2s at 220. No overlap needed here --
        # this test is about transmission, crossfade math is covered elsewhere.
        placement_a = ClipPlacement(clip_id="a", start_ms=0)
        placement_b = ClipPlacement(clip_id="b", start_ms=2000)
        placements = [placement_a, placement_b]
        clip_paths = {"a": clip_a_path, "b": clip_b_path}

        received_values = []
        stop_flag = threading.Event()

        receiver = sacn.sACNreceiver(bind_address="0.0.0.0")

        def on_packet(packet):
            received_values.append(packet.dmxData[0])

        receiver.register_listener("universe", on_packet, universe=1)
        receiver.start()

        async def run():
            engine = PlaybackEngine()
            await engine.start(
                placements=placements,
                clip_paths=clip_paths,
                channel_count=channel_count,
                step_ms=step_ms,
                total_ms=4000,
                destination="127.0.0.1",
                start_t_ms=0,
            )
            assert engine.is_playing

            # Poll until we've seen both the early (100) and later (220) values,
            # or give up after a generous timeout.
            deadline = time.monotonic() + 8.0
            while time.monotonic() < deadline:
                if 100 in received_values and 220 in received_values:
                    break
                await asyncio.sleep(0.1)

            await engine.stop()
            assert not engine.is_playing

        asyncio.run(run())
        receiver.stop()
        stop_flag.set()

        assert 100 in received_values, f"never received the clip A value; got {set(received_values)}"
        assert 220 in received_values, f"never received the clip B value; got {set(received_values)}"

    print("OK: PlaybackEngine streamed real, correctly time-varying sACN data over loopback")


def test_playback_engine_rejects_concurrent_start():
    async def run():
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            clip_path = tmp / "a.fseq"
            make_constant_clip(clip_path, 512, 40, duration_s=2, value=50)
            placements = [ClipPlacement(clip_id="a", start_ms=0)]
            clip_paths = {"a": clip_path}

            engine = PlaybackEngine()
            await engine.start(
                placements=placements,
                clip_paths=clip_paths,
                channel_count=512,
                step_ms=40,
                total_ms=2000,
                destination="127.0.0.1",
            )
            try:
                raised = False
                try:
                    await engine.start(
                        placements=placements,
                        clip_paths=clip_paths,
                        channel_count=512,
                        step_ms=40,
                        total_ms=2000,
                        destination="127.0.0.1",
                    )
                except RuntimeError:
                    raised = True
                assert raised, "starting playback twice should raise RuntimeError"
            finally:
                await engine.stop()

    asyncio.run(run())
    print("OK: PlaybackEngine rejects a second concurrent start")


if __name__ == "__main__":
    test_playback_engine_streams_real_sacn_over_loopback()
    test_playback_engine_rejects_concurrent_start()
    print("\nAll playback tests passed.")
