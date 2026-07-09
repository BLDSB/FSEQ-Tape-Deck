# FSEQ Tapedeck — Claude Code Project Prompt

## What We Are Building

A desktop application called **FSEQ Tapedeck** that lets a lighting designer:

1. **Record** live sACN (E1.31) or ArtNet DMX data from a lighting console into named clip files
2. **Arrange** those clips on a visual timeline with configurable start times
3. **Fade** clips in and out, and crossfade between overlapping clips
4. **Export** the composed result as a single FSEQ file for playback by FPP (Falcon Player) or xSchedule

This tool fills a gap in the xLights/FPP ecosystem: FSEQ is a render-only format that xLights cannot re-open for editing, and the Data Layer approach has no timeline arrangement or crossfade capability. FSEQ Tapedeck treats DMX recordings like video clips in an NLE — place, trim, fade, and mix them down to a single output FSEQ.

---

## Tech Stack

- **Backend / core logic**: Python 3.11+
- **Frontend / UI**: FastAPI serving a single-page web UI (HTML + vanilla JS + CSS), accessed at `http://localhost:7979` in a browser
- **No Electron, no PyQt** — keep it simple. A browser tab IS the UI.
- **Key Python packages**:
  - `fastapi` + `uvicorn` — web server and REST API
  - `sacn` — sACN / E1.31 listener (`pip install sacn`)
  - `pyartnet` — ArtNet listener (`pip install pyartnet`)
  - `struct` — standard library, used for FSEQ binary I/O
  - `asyncio` — for async recording
  - `numpy` — for fast per-frame channel mixing

> **Note**: When installing packages, always use `pip install --break-system-packages` if needed.

---

## Project Structure

```
fseq_tapedeck/
├── main.py                  # FastAPI app entry point; launches UI server
├── recorder.py              # sACN + ArtNet listener; writes clip files
├── fseq.py                  # FSEQ V2 binary reader/writer
├── mixer.py                 # Timeline model; arranges clips, applies fades, renders output
├── clip_store.py            # Manages the library of recorded clips (JSON metadata)
├── api/
│   ├── routes_recorder.py   # REST endpoints: start/stop recording, list clips
│   ├── routes_mixer.py      # REST endpoints: timeline state, export
│   └── routes_clips.py      # REST endpoints: rename, delete, get clip info
├── static/
│   ├── index.html           # Single-page app shell
│   ├── app.js               # Timeline UI, clip library, transport controls
│   └── style.css            # Dark-themed layout appropriate for a lighting tool
├── clips/                   # Auto-created; stores recorded .fseq clip files
├── exports/                 # Auto-created; stores exported mixed .fseq files
└── project.json             # Persisted timeline arrangement and settings
```

---

## Core Technical Specs

### FSEQ V2 Binary Format

FSEQ is the native playback format for FPP and xSchedule. V2 uncompressed is the target.

```
Offset  Size   Description
------  -----  -----------
0       4      Magic: ASCII "PSEQ"
4       2      Offset to channel data (little-endian uint16) — set to 32
6       1      Minor version: 0
7       1      Major version: 2
8       2      Header length (little-endian uint16) — set to 32
10      4      Channel count per frame (little-endian uint32)
14      4      Total frame count (little-endian uint32)
18      1      Step time in milliseconds (e.g. 25 for 40fps, 50 for 20fps)
19      1      Flags (0 = no compression)
20      2      Universe count (set to 0, ignored by FPP)
22      2      Universe size (set to 0, ignored by FPP)
24      1      Gamma: 1
25      1      Color order: 2 (RGB)
26      2      Reserved: 0
--- channel data follows at byte 32 ---
Each frame: channel_count bytes, one byte per channel (0–255), sequential frames
```

**Key rule**: The total file size = 32 + (channel_count × total_frames).

Implement `fseq.py` with:
- `FSEQWriter(path, channel_count, step_ms)` — opens file, writes header, exposes `write_frame(bytes)` and `close()`
- `FSEQReader(path)` — reads header metadata, exposes `read_frame(n)` and `iter_frames()`
- `FSEQInfo` dataclass: `path`, `channel_count`, `frame_count`, `step_ms`, `duration_seconds`

### sACN / E1.31 Protocol

- UDP multicast, port **5568**
- Each universe carries up to 512 channels (channels 1–512, 0-indexed in packet data)
- Multicast address: `239.255.X.Y` where X = universe >> 8 and Y = universe & 0xFF
- The data payload after the E1.31 wrapper is a raw DMX512 slot array (first byte is the START code, always 0; skip it — data bytes start at index 1)
- Universes are numbered 1–63999
- Use the `sacn` Python library; register a callback per universe

### ArtNet Protocol

- UDP, port **6454**, typically broadcast or unicast
- ArtDMX packet: ID = "Art-Net\0", OpCode = 0x5000
- Universe in ArtDMX header is a 15-bit little-endian value
- Data: up to 512 bytes of DMX channel values (no START code)
- Use `pyartnet` or parse raw UDP with `socket` directly (fallback if pyartnet has issues)

### Recording Model

A **clip** represents one contiguous recording session:
- Metadata (JSON): `clip_id`, `name`, `universes` (list of universe numbers recorded), `channel_count`, `frame_count`, `step_ms`, `created_at`
- Data: stored as an FSEQ file in `clips/` named `{clip_id}.fseq`
- Universe-to-channel mapping: universe U's 512 channels occupy positions `(U-1)*512` through `(U*512)-1` in the frame data
- Support recording a configurable list of universes; pad with zeros any universe with no data received in a given frame

The recorder must:
- Allow recording to start/stop via API call
- Buffer incoming sACN/ArtNet frames in memory aligned to the step_ms clock
- Write completed frames to the FSEQWriter in sequence
- Handle missed frames (insert the last known frame values to keep timing consistent)

### Timeline / Mixer Model

The timeline is a list of **clip placements**:
```json
{
  "clip_id": "abc123",
  "start_ms": 5000,
  "fade_in_ms": 1000,
  "fade_out_ms": 1500,
  "trim_start_ms": 0,
  "trim_end_ms": null
}
```

The **render** process:
1. Determine output duration (max of all clip end times)
2. For each output frame:
   - For each active clip placement at that time:
     - Read the corresponding frame from the clip's FSEQ
     - Compute the fade envelope: linear ramp up during fade_in, linear ramp down during fade_out, 1.0 in between
     - Multiply all channel values by the envelope scalar (0.0–1.0)
   - Merge all active clips using **HTP (Highest Takes Precedence)**: for each channel position, take the highest scaled value across all active clips
   - Write the merged frame to the output FSEQ
3. Save output to `exports/{export_name}.fseq`

The timeline state and all placements are persisted to `project.json` so the session survives restarts.

---

## REST API Endpoints

### Recorder

| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/record/start` | Body: `{name, universes: [1,2,3], step_ms: 40, protocol: "sacn"\|"artnet"}` — starts recording |
| POST | `/api/record/stop` | Stops current recording, finalizes clip file, returns clip metadata |
| GET | `/api/record/status` | Returns `{recording: bool, clip_name, frame_count, elapsed_ms}` |

### Clip Library

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/clips` | Returns list of all clips with metadata |
| GET | `/api/clips/{id}` | Returns metadata for one clip |
| PATCH | `/api/clips/{id}` | Body: `{name}` — rename a clip |
| DELETE | `/api/clips/{id}` | Deletes clip file and metadata |

### Timeline / Mixer

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/timeline` | Returns current timeline state (all placements + settings) |
| POST | `/api/timeline/placements` | Add a clip placement to the timeline |
| PUT | `/api/timeline/placements/{placement_id}` | Update a placement (start_ms, fades, trims) |
| DELETE | `/api/timeline/placements/{placement_id}` | Remove a placement |
| POST | `/api/timeline/export` | Body: `{name, channel_count, step_ms}` — renders and exports FSEQ |
| GET | `/api/timeline/exports` | List of exported FSEQ files with paths |

---

## UI Requirements

The web UI is a **single dark-themed page** with three main panels:

### 1. Clip Library (left panel, ~280px wide)
- List of all recorded clips: name, duration, universe count
- Button: **Record New Clip** → opens a modal with fields for name, universes (comma-separated), protocol selector (sACN / ArtNet), step time
- While recording: show a live frame counter and elapsed time with a **Stop** button
- Click a clip in the list to select it for dragging onto the timeline

### 2. Timeline (center, main area)
- Horizontal timeline with a time ruler (seconds)
- Each clip placement is a colored block on a horizontal track
- Blocks are draggable left/right to adjust `start_ms`
- Each block shows: clip name, duration, fade-in and fade-out handles (drag the left/right edges of the block inward to set fade lengths)
- Overlapping blocks show a crossfade region visually
- Right-click a block → context menu: Remove, Edit (opens a property panel for precise ms values)
- **Play cursor**: a vertical line showing the current preview position (future feature, stub OK for now)
- A **playhead scrubber** below the ruler (draggable)

### 3. Transport / Export (right panel or bottom bar)
- **Export** button → prompts for file name, triggers `/api/timeline/export`
- Output channel count field (total channels in output, e.g. 512 for 1 universe, 1024 for 2)
- Output step time selector (20ms / 25ms / 40ms / 50ms)
- List of completed exports with file path shown

---

## Build Order

Build in this sequence — each phase should be runnable and testable before moving to the next:

### Phase 1: Core File Layer
Build `fseq.py` first with full unit tests. Verify you can write and read back a multi-frame FSEQ file that FPP-compatible tools recognize. Write a small test script that:
- Creates a 5-second FSEQ at 40fps with 512 channels
- Fills each frame with a ramp pattern
- Reads it back and verifies frame data integrity

### Phase 2: Recorder Engine
Build `recorder.py` with sACN support first (ArtNet second). Create a CLI test mode (`python recorder.py --test`) that listens on universe 1 and prints incoming frame counts to the terminal every second. Then add the FSEQWriter integration.

### Phase 3: Mixer Engine
Build `mixer.py` standalone with a test that:
- Creates two synthetic 10-second clips (generated FSEQ files with known patterns)
- Places them with a 2-second overlap and 1-second crossfade on each
- Renders the output FSEQ
- Prints per-frame channel 0 value to verify the HTP merge and fade envelope math

### Phase 4: FastAPI Server + REST API
Wire up `main.py` and all API routes. Test with `curl` or a REST client.

### Phase 5: Web UI
Build the static UI. Start with the clip library and record modal (Phase 5a), then the timeline (Phase 5b), then export (Phase 5c).

---

## Key Decisions and Constraints

- **Step time / frame rate**: Default to **40ms (25fps)**. All clips in a session should use the same step time. If mixing clips with different step times, resample during render (nearest-neighbor is fine for V1).
- **Channel space**: The internal channel space is flat. Universe 1 → channels 0–511, Universe 2 → channels 512–1023, etc. The user configures which universes to record; the recorder only captures those.
- **HTP merge**: For the initial version, use HTP (highest value wins per channel) as the merge mode. Add LTP as a future option.
- **No authentication**: This runs locally, no auth needed.
- **Cross-platform**: Target Windows (primary) and macOS. Avoid OS-specific calls.
- **Clip IDs**: Use `uuid4()` as clip identifiers.
- **project.json** is the single source of truth for timeline state; write it on every mutation.

---

## What Success Looks Like

A user can:
1. Open a browser to `http://localhost:7979`
2. Enter universe numbers matching their console output, hit Record, run their console cue, hit Stop
3. Repeat for additional cues (each becomes a separate clip)
4. Drag the clips onto the timeline, position them, set fade-in and fade-out handles
5. Click Export and get a `.fseq` file they can drop onto FPP or use as a Data Layer in xLights

---

## Start Here

Begin with **Phase 1**. Create the full project directory structure, then implement and test `fseq.py` completely before moving on. Ask for clarification on any ambiguity in the FSEQ format before writing the binary I/O code — getting the header offsets wrong will silently corrupt the output.
