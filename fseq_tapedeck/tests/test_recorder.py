"""Phase 2 verification for recorder.py.

Run directly: python tests/test_recorder.py
"""

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import sacn

from fseq import FSEQReader
from recorder import (
    ArtNetListener,
    Recorder,
    SACNListener,
    UniverseBuffer,
    UNIVERSE_CHANNELS,
    list_local_ipv4_addresses,
)


def test_universe_buffer_absolute_mapping():
    # Record universes 1 and 3 (skip 2) -> channel_count should span through
    # universe 3, with universe 2's range left as zero.
    buf = UniverseBuffer([1, 3])
    buf.update(1, bytes([10] * UNIVERSE_CHANNELS))
    buf.update(3, bytes([30] * UNIVERSE_CHANNELS))

    channel_count = 3 * UNIVERSE_CHANNELS
    frame = buf.snapshot_flat(channel_count)

    assert frame[0 : UNIVERSE_CHANNELS] == bytes([10] * UNIVERSE_CHANNELS)
    assert frame[UNIVERSE_CHANNELS : 2 * UNIVERSE_CHANNELS] == bytes([0] * UNIVERSE_CHANNELS)
    assert frame[2 * UNIVERSE_CHANNELS : 3 * UNIVERSE_CHANNELS] == bytes([30] * UNIVERSE_CHANNELS)
    print("OK: UniverseBuffer absolute mapping with a gap universe")


def test_universe_buffer_holds_last_value():
    # Missed frames should retain the last known value, not reset to zero.
    buf = UniverseBuffer([1])
    buf.update(1, bytes([42] * UNIVERSE_CHANNELS))
    first = buf.snapshot_flat(UNIVERSE_CHANNELS)
    second = buf.snapshot_flat(UNIVERSE_CHANNELS)  # no update() between snapshots
    assert first == second == bytes([42] * UNIVERSE_CHANNELS)
    print("OK: UniverseBuffer holds last known value across snapshots")


def test_artnet_packet_parsing():
    universe = 258  # exercises the 15-bit little-endian universe field
    dmx_values = bytes([7] * 512)
    header = (
        b"Art-Net\x00"
        + (0x5000).to_bytes(2, "little")  # OpCode
        + bytes([0, 14])  # protocol version hi/lo (ignored by parser)
        + bytes([0, 0])  # sequence, physical
        + universe.to_bytes(2, "little")  # universe (15-bit LE)
        + len(dmx_values).to_bytes(2, "big")  # length (big-endian per spec)
    )
    packet = header + dmx_values

    parsed = ArtNetListener._parse_artdmx(packet)
    assert parsed is not None
    parsed_universe, parsed_data = parsed
    assert parsed_universe == universe
    assert parsed_data == dmx_values

    # Garbage / non-ArtNet packets must be rejected, not raise.
    assert ArtNetListener._parse_artdmx(b"not artnet") is None
    assert ArtNetListener._parse_artdmx(b"Art-Net\x00" + b"\x00" * 20) is None  # wrong opcode
    print("OK: ArtDMX packet parsing (universe + payload)")


def test_sacn_listener_receives_loopback_packets():
    universe = 7
    buf = UniverseBuffer([universe])
    listener = SACNListener([universe], buf, bind_address="0.0.0.0")
    listener.start()

    sender = sacn.sACNsender(source_name="fseq-tapedeck-test")
    sender.start()
    sender.activate_output(universe)
    sender[universe].destination = "127.0.0.1"
    sender[universe].dmx_data = tuple([99] * 512)

    try:
        deadline = time.monotonic() + 5.0
        while listener.packet_count == 0 and time.monotonic() < deadline:
            time.sleep(0.1)
        assert listener.packet_count > 0, "no sACN packets received over loopback within 5s"
        frame = buf.snapshot_flat(universe * UNIVERSE_CHANNELS)
        received = frame[(universe - 1) * UNIVERSE_CHANNELS : universe * UNIVERSE_CHANNELS]
        assert received == bytes([99] * 512)
        print(f"OK: SACNListener received {listener.packet_count} loopback packet(s), data verified")
    finally:
        sender.stop()
        listener.stop()


def test_list_local_ipv4_addresses_excludes_loopback_and_link_local():
    addresses = list_local_ipv4_addresses()
    assert isinstance(addresses, list)
    for ip in addresses:
        assert not ip.startswith("127."), f"loopback address leaked through: {ip}"
        assert not ip.startswith("169.254."), f"link-local address leaked through: {ip}"
    print(f"OK: list_local_ipv4_addresses() -> {addresses} (loopback/link-local excluded)")


def test_recorder_honors_explicit_bind_address():
    """Regression check: passing an explicit bind_address (the fix for
    multicast-on-the-wrong-interface on machines with multiple adapters)
    must not break reception -- loopback should still work when bound
    directly to 127.0.0.1."""

    async def run():
        recorder = Recorder(clips_dir=Path("_test_clips_tmp3"))
        await recorder.start(
            name="bind-address-test",
            universes=[9],
            step_ms=40,
            protocol="sacn",
            bind_address="127.0.0.1",
        )
        try:
            sender = sacn.sACNsender(source_name="fseq-tapedeck-bind-test")
            sender.start()
            sender.activate_output(9)
            sender[9].destination = "127.0.0.1"
            sender[9].dmx_data = tuple([123] * 512)
            try:
                deadline = time.monotonic() + 5.0
                frame = recorder.current_frame()
                while (not frame or frame[(9 - 1) * UNIVERSE_CHANNELS] != 123) and time.monotonic() < deadline:
                    time.sleep(0.1)
                    frame = recorder.current_frame()
                assert frame[(9 - 1) * UNIVERSE_CHANNELS] == 123, (
                    "no data received when explicitly bound to 127.0.0.1"
                )
            finally:
                sender.stop()
        finally:
            await recorder.stop()

        clip_path = recorder.clips_dir / f"{recorder.clip_id}.fseq"
        clip_path.unlink()
        (recorder.clips_dir / f"{recorder.clip_id}.json").unlink()
        recorder.clips_dir.rmdir()

    asyncio.run(run())
    print("OK: Recorder.start(bind_address=...) receives data on the specified interface")


def test_recorder_end_to_end_with_fake_source():
    """Drive the Recorder's write loop with synthetic buffer updates and
    verify the resulting FSEQ clip file is well-formed and captures data."""

    async def run():
        recorder = Recorder(clips_dir=Path("_test_clips_tmp"))
        await recorder.start(name="e2e-test", universes=[1], step_ms=20, protocol="sacn")

        # Feed a changing value into the buffer while the record loop runs,
        # simulating live incoming DMX data.
        for level in (0, 64, 128, 192, 255):
            recorder._buffer.update(1, bytes([level] * UNIVERSE_CHANNELS))
            await asyncio.sleep(0.05)

        metadata = await recorder.stop()

        assert metadata.channel_count == UNIVERSE_CHANNELS
        assert metadata.frame_count > 0
        assert metadata.protocol == "sacn"

        clip_path = recorder.clips_dir / f"{metadata.clip_id}.fseq"
        assert clip_path.exists()

        reader = FSEQReader(clip_path)
        try:
            assert reader.frame_count == metadata.frame_count
            assert reader.channel_count == UNIVERSE_CHANNELS
            last_frame = reader.read_frame(reader.frame_count - 1)
            # The last frame should reflect the last value written to the buffer.
            assert last_frame == bytes([255] * UNIVERSE_CHANNELS)
        finally:
            reader.close()

        # cleanup
        clip_path.unlink()
        (recorder.clips_dir / f"{metadata.clip_id}.json").unlink()
        recorder.clips_dir.rmdir()

    asyncio.run(run())
    print("OK: Recorder end-to-end record/stop produced a valid, verifiable FSEQ clip")


def test_current_frame_reflects_live_buffer():
    """current_frame() is what powers the recording-preview meter in the UI --
    it must reflect the buffer immediately, not just on a write tick, and must
    be None outside of an active recording."""

    async def run():
        recorder = Recorder(clips_dir=Path("_test_clips_tmp2"))
        assert recorder.current_frame() is None  # nothing recording yet

        await recorder.start(name="preview-test", universes=[1], step_ms=200, protocol="sacn")
        try:
            # Long-ish step_ms so we can check the buffer between ticks.
            recorder._buffer.update(1, bytes([77] * UNIVERSE_CHANNELS))
            frame = recorder.current_frame()
            assert frame == bytes([77] * UNIVERSE_CHANNELS)
            assert len(frame) == recorder.channel_count
        finally:
            await recorder.stop()

        assert recorder.current_frame() is None  # cleared once stopped

        clip_path = recorder.clips_dir / f"{recorder.clip_id}.fseq"
        clip_path.unlink()
        (recorder.clips_dir / f"{recorder.clip_id}.json").unlink()
        recorder.clips_dir.rmdir()

    asyncio.run(run())
    print("OK: current_frame() mirrors the live buffer while recording, None otherwise")


if __name__ == "__main__":
    test_universe_buffer_absolute_mapping()
    test_universe_buffer_holds_last_value()
    test_artnet_packet_parsing()
    test_sacn_listener_receives_loopback_packets()
    test_list_local_ipv4_addresses_excludes_loopback_and_link_local()
    test_recorder_honors_explicit_bind_address()
    test_recorder_end_to_end_with_fake_source()
    test_current_frame_reflects_live_buffer()
    print("\nAll recorder tests passed.")
