"""FastAPI app entry point; launches the FSEQ Tapedeck UI server."""

from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from api import routes_clips, routes_mixer, routes_recorder
from clip_store import ClipStore
from mixer import Timeline
from playback import PlaybackEngine
from recorder import Recorder

BASE_DIR = Path(__file__).resolve().parent
CLIPS_DIR = BASE_DIR / "clips"
EXPORTS_DIR = BASE_DIR / "exports"
STATIC_DIR = BASE_DIR / "static"
PROJECT_FILE = BASE_DIR / "project.json"

HOST = "127.0.0.1"
PORT = 7979


def create_app() -> FastAPI:
    CLIPS_DIR.mkdir(parents=True, exist_ok=True)
    EXPORTS_DIR.mkdir(parents=True, exist_ok=True)

    app = FastAPI(title="FSEQ Tapedeck")

    app.state.clips_dir = CLIPS_DIR
    app.state.exports_dir = EXPORTS_DIR
    app.state.clip_store = ClipStore(CLIPS_DIR)
    app.state.recorder = Recorder(CLIPS_DIR)
    app.state.timeline = Timeline(PROJECT_FILE)
    app.state.playback_engine = PlaybackEngine()

    app.include_router(routes_recorder.router)
    app.include_router(routes_clips.router)
    app.include_router(routes_mixer.router)

    if STATIC_DIR.exists():
        # This is a locally-run tool that gets edited and re-launched often;
        # without this, browsers can go a long time (heuristic freshness
        # based on Last-Modified) before re-checking the UI files with the
        # server, silently serving a stale HTML/JS/CSS after an update.
        @app.middleware("http")
        async def no_cache_static(request, call_next):
            response = await call_next(request)
            if request.url.path == "/" or not request.url.path.startswith("/api"):
                response.headers["Cache-Control"] = "no-cache"
            return response

        app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")

    return app


app = create_app()


def main() -> None:
    uvicorn.run(app, host=HOST, port=PORT)


if __name__ == "__main__":
    main()
