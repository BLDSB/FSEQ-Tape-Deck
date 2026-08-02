"""Phase 4 verification: exercise the full REST API with FastAPI's TestClient.

Run directly: python tests/test_api.py
"""

import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient

import main
from fseq import FSEQReader


def build_test_app(tmp_dir: Path):
    main.CLIPS_DIR = tmp_dir / "clips"
    main.EXPORTS_DIR = tmp_dir / "exports"
    main.PROJECT_FILE = tmp_dir / "project.json"
    return main.create_app()


def test_full_api_flow():
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        app = build_test_app(tmp_dir)

        with TestClient(app) as client:
            # --- Recorder ---
            status = client.get("/api/record/status").json()
            assert status["recording"] is False

            # No live frame preview when nothing is recording.
            assert client.get("/api/record/frame").status_code == 409

            resp = client.post(
                "/api/record/start",
                json={"name": "cue-1", "universes": [1], "step_ms": 20, "protocol": "sacn"},
            )
            assert resp.status_code == 200, resp.text
            assert resp.json()["recording"] is True

            # Recording while already recording -> 409.
            dup = client.post(
                "/api/record/start",
                json={"name": "dup", "universes": [1], "step_ms": 20, "protocol": "sacn"},
            )
            assert dup.status_code == 409

            # The recorder holds its first frame until DMX actually arrives (so
            # clips never open on a blackout); feed the buffer directly to stand
            # in for a live console.
            app.state.recorder._buffer.update(1, bytes([128] * 512))
            time.sleep(0.2)  # let a handful of frames get written

            status = client.get("/api/record/status").json()
            assert status["recording"] is True
            assert status["frame_count"] > 0

            live_frame = client.get("/api/record/frame").json()
            assert live_frame["channel_count"] == 512
            assert len(live_frame["channels"]) == 512
            assert live_frame["universes"] == [1]

            resp = client.post("/api/record/stop")
            assert resp.status_code == 200, resp.text
            clip = resp.json()
            assert clip["frame_count"] > 0
            assert clip["channel_count"] == 512
            clip_id = clip["clip_id"]

            # Stopping again with nothing recording -> 409.
            assert client.post("/api/record/stop").status_code == 409

            # Live frame preview is gone again once recording has stopped.
            assert client.get("/api/record/frame").status_code == 409

            # --- Clips ---
            clips = client.get("/api/clips").json()
            assert any(c["clip_id"] == clip_id for c in clips)

            got = client.get(f"/api/clips/{clip_id}").json()
            assert got["clip_id"] == clip_id

            renamed = client.patch(f"/api/clips/{clip_id}", json={"name": "renamed-cue"}).json()
            assert renamed["name"] == "renamed-cue"

            assert client.get("/api/clips/does-not-exist").status_code == 404

            # --- Timeline placements ---
            timeline = client.get("/api/timeline").json()
            assert timeline["placements"] == []

            placement = client.post(
                "/api/timeline/placements",
                json={"clip_id": clip_id, "start_ms": 0, "fade_in_ms": 0, "fade_out_ms": 0},
            ).json()
            placement_id = placement["placement_id"]

            bad_placement = client.post(
                "/api/timeline/placements",
                json={"clip_id": "no-such-clip", "start_ms": 0},
            )
            assert bad_placement.status_code == 404

            updated = client.put(
                f"/api/timeline/placements/{placement_id}", json={"fade_in_ms": 50}
            ).json()
            assert updated["fade_in_ms"] == 50
            assert updated["start_ms"] == 0  # untouched fields preserved

            timeline = client.get("/api/timeline").json()
            assert len(timeline["placements"]) == 1

            # --- Auto-crossfade arrangement ---
            clip_meta = next(c for c in client.get("/api/clips").json() if c["clip_id"] == clip_id)
            clip_dur = clip_meta["frame_count"] * clip_meta["step_ms"]
            assert clip_dur > 0

            second = client.post(
                "/api/timeline/placements",
                json={"clip_id": clip_id, "start_ms": clip_dur + 5000},
            ).json()
            second_id = second["placement_id"]

            # Ask for an over-long crossfade; it clamps to the clip length and
            # overlaps the incoming clip fully onto the outgoing one.
            cf = client.post(
                "/api/timeline/crossfade",
                json={"placement_id_a": placement_id, "placement_id_b": second_id, "duration_ms": 10_000_000},
            )
            assert cf.status_code == 200, cf.text
            cfj = cf.json()
            assert cfj["duration_ms"] == clip_dur
            assert cfj["incoming"]["crossfade_ms"] == clip_dur
            assert cfj["incoming"]["fade_in_ms"] == 0
            assert cfj["incoming"]["start_ms"] == 0  # start + dur - dur
            assert cfj["outgoing"]["fade_out_ms"] == 0

            # Crossfading a clip with itself is rejected.
            assert client.post(
                "/api/timeline/crossfade",
                json={"placement_id_a": placement_id, "placement_id_b": placement_id, "duration_ms": 100},
            ).status_code == 400

            # Persisted crossfade_ms survives a round-trip through the API.
            assert any(
                p["crossfade_ms"] == clip_dur for p in client.get("/api/timeline").json()["placements"]
            )

            # Clean up so the export section below sees a single placement again.
            assert client.delete(f"/api/timeline/placements/{second_id}").status_code == 200

            # --- Live frame preview (used for playback/scrubbing) ---
            frame = client.get(
                "/api/timeline/frame", params={"t_ms": 0, "channel_count": 512}
            ).json()
            assert frame["t_ms"] == 0
            assert len(frame["channels"]) == 512
            assert all(0 <= v <= 255 for v in frame["channels"])

            far_frame = client.get(
                "/api/timeline/frame", params={"t_ms": 999999, "channel_count": 512}
            ).json()
            assert all(v == 0 for v in far_frame["channels"])  # nothing active that far out

            # --- Live sACN playback output ---
            status = client.get("/api/timeline/playback/status").json()
            assert status["playing"] is False
            assert status["active"] is False

            start_playback = client.post(
                "/api/timeline/playback/start",
                json={"channel_count": 512, "step_ms": 40, "destination": "127.0.0.1"},
            )
            assert start_playback.status_code == 200, start_playback.text
            assert start_playback.json()["playing"] is True

            dup_playback = client.post(
                "/api/timeline/playback/start",
                json={"channel_count": 512, "step_ms": 40, "destination": "127.0.0.1"},
            )
            assert dup_playback.status_code == 409

            status = client.get("/api/timeline/playback/status").json()
            assert status["playing"] is True

            stop_playback = client.post("/api/timeline/playback/stop")
            assert stop_playback.status_code == 200, stop_playback.text
            assert stop_playback.json()["stopped"] is True

            # stop is idempotent now (the client stops output on checkbox-off
            # even when only holding a scrubbed frame, never playing)
            second_stop = client.post("/api/timeline/playback/stop")
            assert second_stop.status_code == 200
            assert second_stop.json()["stopped"] is False

            status = client.get("/api/timeline/playback/status").json()
            assert status["playing"] is False

            # --- Live scrub output: source holds a frame without advancing ---
            scrub = client.post(
                "/api/timeline/playback/scrub",
                json={"t_ms": 100, "channel_count": 512, "destination": "127.0.0.1"},
            )
            assert scrub.status_code == 200, scrub.text
            assert scrub.json()["active"] is True
            assert scrub.json()["playing"] is False

            # moving the held playhead on the already-live source
            scrub2 = client.post(
                "/api/timeline/playback/scrub",
                json={"t_ms": 250, "channel_count": 512, "destination": "127.0.0.1"},
            )
            assert scrub2.status_code == 200
            assert scrub2.json()["t_ms"] == 250

            # pause is a no-op while merely holding, but keeps the source up
            pause = client.post("/api/timeline/playback/pause")
            assert pause.status_code == 200
            assert pause.json()["active"] is True

            assert client.post("/api/timeline/playback/stop").json()["stopped"] is True
            status = client.get("/api/timeline/playback/status").json()
            assert status["active"] is False

            # --- Export ---
            export = client.post(
                "/api/timeline/export",
                json={"name": "export-1", "channel_count": 512, "step_ms": 20},
            ).json()
            assert export["frame_count"] > 0
            export_path = Path(export["path"])
            assert export_path.exists()

            reader = FSEQReader(export_path)
            try:
                assert reader.channel_count == 512
            finally:
                reader.close()

            exports = client.get("/api/timeline/exports").json()
            assert any(e["name"] == "export-1" for e in exports)

            # --- Configurable export directory ---
            dir_info = client.get("/api/timeline/export-dir").json()
            assert dir_info["is_default"] is True

            custom_dir = tmp_dir / "picked exports"
            set_dir = client.put(
                "/api/timeline/export-dir", json={"path": str(custom_dir)}
            ).json()
            assert set_dir["is_default"] is False
            assert Path(set_dir["path"]) == custom_dir
            assert custom_dir.exists()  # created on set

            # Exporting now lands in the chosen folder, and the list follows it.
            export2 = client.post(
                "/api/timeline/export",
                json={"name": "export-2", "channel_count": 512, "step_ms": 20},
            ).json()
            assert Path(export2["path"]).parent == custom_dir
            names = [e["name"] for e in client.get("/api/timeline/exports").json()]
            assert names == ["export-2"]  # the old default-folder export is not listed here

            # A user-chosen folder can hold unrelated .json files (arrays, other
            # shapes); listing must skip them, not crash.
            (custom_dir / "unrelated-array.json").write_text("[1, 2, 3]")
            (custom_dir / "unrelated-obj.json").write_text('{"hello": "world"}')
            listed = client.get("/api/timeline/exports")
            assert listed.status_code == 200, listed.text
            assert [e["name"] for e in listed.json()] == ["export-2"]

            # Blank path resets to the default.
            reset = client.put("/api/timeline/export-dir", json={"path": ""}).json()
            assert reset["is_default"] is True
            names = [e["name"] for e in client.get("/api/timeline/exports").json()]
            assert "export-1" in names

            # --- Cleanup via API ---
            assert client.delete(f"/api/timeline/placements/{placement_id}").status_code == 200
            assert client.delete(f"/api/timeline/placements/{placement_id}").status_code == 404

            assert client.delete(f"/api/clips/{clip_id}").status_code == 200
            assert client.delete(f"/api/clips/{clip_id}").status_code == 404

    print("OK: full REST API flow (record -> stop -> clip -> placement -> export) verified end-to-end")


if __name__ == "__main__":
    test_full_api_flow()
    print("\nAll API tests passed.")
