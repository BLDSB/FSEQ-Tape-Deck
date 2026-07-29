"""Portable project bundle (.ftdproj): pack all recordings + timeline, and
open one (replacing the current project). Plus the new/open/save REST flow.

Run directly: python tests/test_bundle.py
"""

import json
import sys
import tempfile
import time
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient

import main
from bundle import import_bundle, pack_project
from clip_store import ClipStore
from fseq import FSEQWriter

CH = 4
STEP = 40


def _seed_clip(clips_dir: Path, clip_id: str, name: str, frames: int = 3) -> None:
    with FSEQWriter(clips_dir / f"{clip_id}.fseq", channel_count=CH, step_ms=STEP) as w:
        for i in range(frames):
            w.write_frame(bytes([i % 256, 0, 0, 0]))
    (clips_dir / f"{clip_id}.json").write_text(json.dumps({
        "clip_id": clip_id, "name": name, "universes": [1],
        "channel_count": CH, "frame_count": frames, "step_ms": STEP,
        "protocol": "sacn", "created_at": "2026-01-01T00:00:00+00:00",
    }))


def test_pack_includes_all_recordings():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        clip_store = ClipStore(root / "clips")

        # Two recordings that share a name; only one is on the timeline.
        _seed_clip(root / "clips", "id1", "Dup")
        _seed_clip(root / "clips", "id2", "Dup")
        placements = [{"clip_id": "id1", "start_ms": 0, "placement_id": "p1"}]

        bundle = root / "Show.ftdproj"
        result = pack_project(clip_store, "Show", placements, {"channel_count": CH, "step_ms": STEP}, bundle)
        # BOTH recordings are packed, not just the placed one.
        assert result["clip_count"] == 2, result
        assert bundle.exists()

        with zipfile.ZipFile(bundle) as zf:
            entries = set(zf.namelist())
            assert "project.json" in entries
            # Friendly-named + deduped inside the bundle.
            assert "clips/Dup.fseq" in entries
            assert "clips/Dup (2).fseq" in entries
            ids = {json.loads(zf.read(e))["clip_id"]
                   for e in entries if e.startswith("clips/") and e.endswith(".json")}
            assert ids == {"id1", "id2"}
            proj = json.loads(zf.read("project.json"))
            assert len(proj["placements"]) == 1
    print("OK: pack includes every recording (not just timeline clips), deduped by name")


def test_open_replaces_library():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        store = ClipStore(root / "clips")

        # Project A: clips a1, a2. Pack it.
        _seed_clip(root / "clips", "a1", "Alpha")
        _seed_clip(root / "clips", "a2", "Beta")
        bundle_a = root / "A.ftdproj"
        pack_project(store, "A", [{"clip_id": "a1", "start_ms": 0}], {}, bundle_a)

        # Now the working library is a different set (b1). Opening A must REPLACE
        # it -> only a1/a2 remain, b1 is gone.
        store.clear()
        _seed_clip(root / "clips", "b1", "Gamma")
        res = import_bundle(store, bundle_a)
        assert res["clips_imported"] == 2
        ids = {c["clip_id"] for c in store.list_clips()}
        assert ids == {"a1", "a2"}, ids  # b1 removed
        assert res["name"] == "A"
        assert [p["clip_id"] for p in res["placements"]] == ["a1"]

        # A bad bundle must NOT wipe the current library.
        bad = root / "bad.ftdproj"
        with zipfile.ZipFile(bad, "w") as zf:
            zf.writestr("not-a-project.txt", "nope")
        try:
            import_bundle(store, bad)
            assert False, "expected ValueError for a bundle with no project.json"
        except ValueError:
            pass
        assert {c["clip_id"] for c in store.list_clips()} == {"a1", "a2"}  # untouched
    print("OK: open replaces the library; a malformed bundle leaves it intact")


def test_project_rest_flow():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        main.CLIPS_DIR = root / "clips"
        main.EXPORTS_DIR = root / "exports"
        main.PROJECT_FILE = root / "project.json"
        app = main.create_app()

        with TestClient(app) as client:
            # Record two clips; place only one on the timeline.
            ids = []
            for nm in ("one", "two"):
                client.post("/api/record/start",
                            json={"name": nm, "universes": [1], "step_ms": 20, "protocol": "sacn"})
                time.sleep(0.12)
                ids.append(client.post("/api/record/stop").json()["clip_id"])
            client.post("/api/timeline/placements", json={"clip_id": ids[0], "start_ms": 0})

            assert client.get("/api/project").json()["current"] is None

            # Save the whole project (explicit path -> no dialog). Both clips packed.
            bundle = str(root / "MyShow.ftdproj")
            saved = client.post("/api/project/save", json={"path": bundle})
            assert saved.status_code == 200, saved.text
            assert saved.json()["saved"] is True
            assert saved.json()["clip_count"] == 2
            assert saved.json()["current"] == "MyShow"
            assert client.get("/api/project").json()["current"] == "MyShow"

            # New wipes everything: no clips, empty timeline, untitled.
            newed = client.post("/api/project/new").json()
            assert newed["current"] is None
            assert newed["placements"] == []
            assert client.get("/api/clips").json() == []

            # Open the bundle back: recordings + timeline return, project named
            # after the file.
            opened = client.post("/api/project/open", json={"path": bundle})
            assert opened.status_code == 200, opened.text
            body = opened.json()
            assert body["opened"] is True
            assert body["clips_imported"] == 2
            assert body["current"] == "MyShow"
            assert len(body["placements"]) == 1
            assert len(client.get("/api/clips").json()) == 2

            # Opening a missing file -> 404.
            assert client.post("/api/project/open", json={"path": str(root / "nope.ftdproj")}).status_code == 404

    print("OK: new/open/save REST flow verified (all recordings travel with the project)")


if __name__ == "__main__":
    test_pack_includes_all_recordings()
    test_open_replaces_library()
    test_project_rest_flow()
    print("\nAll bundle tests passed.")
