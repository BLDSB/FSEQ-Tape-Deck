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

            time.sleep(0.2)  # let a handful of frames get written

            status = client.get("/api/record/status").json()
            assert status["recording"] is True
            assert status["frame_count"] > 0

            resp = client.post("/api/record/stop")
            assert resp.status_code == 200, resp.text
            clip = resp.json()
            assert clip["frame_count"] > 0
            assert clip["channel_count"] == 512
            clip_id = clip["clip_id"]

            # Stopping again with nothing recording -> 409.
            assert client.post("/api/record/stop").status_code == 409

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

            # --- Cleanup via API ---
            assert client.delete(f"/api/timeline/placements/{placement_id}").status_code == 200
            assert client.delete(f"/api/timeline/placements/{placement_id}").status_code == 404

            assert client.delete(f"/api/clips/{clip_id}").status_code == 200
            assert client.delete(f"/api/clips/{clip_id}").status_code == 404

    print("OK: full REST API flow (record -> stop -> clip -> placement -> export) verified end-to-end")


if __name__ == "__main__":
    test_full_api_flow()
    print("\nAll API tests passed.")
