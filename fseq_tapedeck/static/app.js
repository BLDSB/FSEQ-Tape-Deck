"use strict";

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

const state = {
  clips: [],
  timeline: { placements: [], settings: {} },
  selectedClipId: null,
  pxPerSec: 60,
  contextPlacementId: null,
  recordingPoll: null,
  statusPoll: null,
  recordMeterPoll: null,
  playing: false,
  playheadMs: 0,
  rafId: null,
  lastTickTs: null,
  lastFrameFetchTs: 0,
  frameFetchInFlight: false,
  consoleOutputActive: false,
  scrubSendInFlight: false,
  scrubPendingMs: null,
};

const CLIP_COLORS = [
  "#7fd4ff", "#ffd27f", "#a8ff7f", "#ff9ecb",
  "#c6a8ff", "#7fffe0", "#ffb37f", "#9ecbff",
];

// Must match --scrubber-thumb-w in style.css.
const SCRUBBER_THUMB_W = 14;

function pxPerMs() {
  return state.pxPerSec / 1000;
}

function colorForClip(clipId) {
  const idx = state.clips.findIndex((c) => c.clip_id === clipId);
  return CLIP_COLORS[(idx < 0 ? 0 : idx) % CLIP_COLORS.length];
}

function clipById(clipId) {
  return state.clips.find((c) => c.clip_id === clipId) || null;
}

// ---------------------------------------------------------------------------
// API helpers
// ---------------------------------------------------------------------------

async function apiCall(method, path, body) {
  const opts = { method, headers: {} };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const resp = await fetch(path, opts);
  let data = null;
  try {
    data = await resp.json();
  } catch (e) {
    data = null;
  }
  if (!resp.ok) {
    const detail = (data && data.detail) || resp.statusText;
    throw new Error(detail);
  }
  return data;
}

const api = {
  get: (path) => apiCall("GET", path),
  post: (path, body) => apiCall("POST", path, body ?? {}),
  put: (path, body) => apiCall("PUT", path, body ?? {}),
  patch: (path, body) => apiCall("PATCH", path, body ?? {}),
  del: (path) => apiCall("DELETE", path),
};

// ---------------------------------------------------------------------------
// Clip library
// ---------------------------------------------------------------------------

async function refreshClips() {
  state.clips = await api.get("/api/clips");
  renderClipList();
  renderTimeline(); // colors/labels depend on clip metadata
}

function formatDuration(frameCount, stepMs) {
  const seconds = (frameCount * stepMs) / 1000;
  return `${seconds.toFixed(1)}s`;
}

function renderClipList() {
  const list = document.getElementById("clip-list");
  list.innerHTML = "";

  if (state.clips.length === 0) {
    const li = document.createElement("li");
    li.className = "clip-item-empty";
    li.textContent = "No clips recorded yet.";
    list.appendChild(li);
    return;
  }

  for (const clip of state.clips) {
    const li = document.createElement("li");
    li.className = "clip-item" + (state.selectedClipId === clip.clip_id ? " selected" : "");
    li.draggable = true;
    li.dataset.clipId = clip.clip_id;

    const name = document.createElement("span");
    name.className = "clip-name";
    name.textContent = clip.name;

    const meta = document.createElement("span");
    meta.className = "clip-meta";
    meta.textContent = `${formatDuration(clip.frame_count, clip.step_ms)} · ${clip.universes.length} universe(s)`;

    const actions = document.createElement("div");
    actions.className = "clip-item-actions";

    const addBtn = document.createElement("button");
    addBtn.className = "btn btn-small clip-add-btn";
    addBtn.textContent = "+ Add to Timeline";
    addBtn.title = "Add this clip to the end of the timeline";
    addBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      addClipToTimeline(clip.clip_id, timelineEndMs());
    });

    const loopBtn = document.createElement("button");
    loopBtn.className = "btn btn-small clip-loop-btn";
    loopBtn.textContent = "Loop…";
    loopBtn.title = "Build a new, seamless looping clip from this one";
    loopBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      openLoopModal(clip);
    });

    const renameBtn = document.createElement("button");
    renameBtn.className = "btn btn-small clip-rename-btn";
    renameBtn.textContent = "Rename…";
    renameBtn.title = "Rename this clip";
    renameBtn.addEventListener("click", async (e) => {
      e.stopPropagation();
      const name = prompt(`Rename "${clip.name}" to:`, clip.name);
      if (name === null) return; // cancelled
      const trimmed = name.trim();
      if (!trimmed || trimmed === clip.name) return;
      try {
        await api.patch(`/api/clips/${clip.clip_id}`, { name: trimmed });
        await refreshClips();
      } catch (err) {
        alert(`Could not rename clip: ${err.message}`);
      }
    });

    const deleteBtn = document.createElement("button");
    deleteBtn.className = "btn btn-small btn-danger clip-delete-btn";
    deleteBtn.textContent = "Delete";
    deleteBtn.title = "Delete this clip";
    deleteBtn.addEventListener("click", async (e) => {
      e.stopPropagation();
      const placementCount = state.timeline.placements.filter((p) => p.clip_id === clip.clip_id).length;
      const message = placementCount > 0
        ? `"${clip.name}" is used in ${placementCount} timeline placement(s). Deleting it will leave those pointing at a missing clip. Delete anyway?`
        : `Delete "${clip.name}"? This cannot be undone.`;
      if (!confirm(message)) return;
      try {
        await api.del(`/api/clips/${clip.clip_id}`);
        if (state.selectedClipId === clip.clip_id) state.selectedClipId = null;
        await refreshClips();
      } catch (err) {
        alert(`Could not delete clip: ${err.message}`);
      }
    });

    actions.appendChild(addBtn);
    actions.appendChild(loopBtn);
    actions.appendChild(renameBtn);
    actions.appendChild(deleteBtn);

    li.appendChild(name);
    li.appendChild(meta);
    li.appendChild(actions);

    li.addEventListener("click", () => {
      state.selectedClipId = clip.clip_id;
      renderClipList();
    });

    li.addEventListener("dragstart", (e) => {
      e.dataTransfer.setData("text/plain", clip.clip_id);
      e.dataTransfer.effectAllowed = "copy";
      state.selectedClipId = clip.clip_id;
      renderClipList();
    });

    list.appendChild(li);
  }
}

// ---------------------------------------------------------------------------
// Timeline rendering
// ---------------------------------------------------------------------------

function placementDurationMs(placement) {
  const clip = clipById(placement.clip_id);
  if (!clip) return 0;
  const clipDurationMs = clip.frame_count * clip.step_ms;
  const end = placement.trim_end_ms != null ? placement.trim_end_ms : clipDurationMs;
  return Math.max(0, end - placement.trim_start_ms);
}

function timelineTotalMs() {
  let maxEnd = 30000; // minimum visible span
  for (const p of state.timeline.placements) {
    maxEnd = Math.max(maxEnd, p.start_ms + placementDurationMs(p));
  }
  return maxEnd + 5000; // trailing padding
}

async function refreshTimeline() {
  state.timeline = await api.get("/api/timeline");
  renderTimeline();
}

function renderRuler(totalMs) {
  const ruler = document.getElementById("timeline-ruler");
  ruler.innerHTML = "";
  ruler.style.width = `${totalMs * pxPerMs()}px`;
  const totalSeconds = Math.ceil(totalMs / 1000);
  for (let s = 0; s <= totalSeconds; s++) {
    const tick = document.createElement("div");
    tick.className = "ruler-tick";
    tick.style.left = `${s * state.pxPerSec}px`;
    tick.textContent = `${s}s`;
    ruler.appendChild(tick);
  }
}

function renderTimeline() {
  const totalMs = timelineTotalMs();
  renderRuler(totalMs);

  const track = document.getElementById("timeline-track");
  const cursor = document.getElementById("play-cursor");
  track.innerHTML = "";
  track.appendChild(cursor);
  track.style.width = `${totalMs * pxPerMs()}px`;

  const scrubber = document.getElementById("scrubber");
  scrubber.max = String(totalMs);
  // Span the full timeline so the blue thumb tracks the red play-cursor. Width
  // is the track content width plus one thumb width; the extra thumb width plus
  // the row's negative left margin cancel the native thumb inset so the thumb
  // center travels exactly edge-to-edge. Keep SCRUBBER_THUMB_W in sync with
  // --scrubber-thumb-w in style.css.
  scrubber.style.width = `${totalMs * pxPerMs() + SCRUBBER_THUMB_W}px`;

  for (const placement of state.timeline.placements) {
    track.appendChild(buildClipBlock(placement));
  }

  setPlayheadVisual(state.playheadMs);
}

function buildClipBlock(placement) {
  const clip = clipById(placement.clip_id);
  const durationMs = placementDurationMs(placement);
  const ppms = pxPerMs();

  const block = document.createElement("div");
  block.className = "clip-block";
  block.dataset.placementId = placement.placement_id;
  block.style.left = `${placement.start_ms * ppms}px`;
  block.style.width = `${Math.max(6, durationMs * ppms)}px`;
  block.style.background = colorForClip(placement.clip_id);

  const label = document.createElement("div");
  label.className = "clip-block-label";
  label.textContent = clip ? `${clip.name} (${(durationMs / 1000).toFixed(1)}s)` : "(missing clip)";
  block.appendChild(label);

  if (placement.fade_in_ms > 0) {
    const overlay = document.createElement("div");
    overlay.className = "fade-overlay";
    overlay.style.left = "0";
    overlay.style.width = `${Math.min(durationMs, placement.fade_in_ms) * ppms}px`;
    block.appendChild(overlay);
  }
  if (placement.fade_out_ms > 0) {
    const overlay = document.createElement("div");
    overlay.className = "fade-overlay fade-out-overlay";
    overlay.style.right = "0";
    overlay.style.width = `${Math.min(durationMs, placement.fade_out_ms) * ppms}px`;
    block.appendChild(overlay);
  }
  if (placement.crossfade_ms > 0) {
    const overlay = document.createElement("div");
    overlay.className = "crossfade-overlay";
    overlay.style.width = `${Math.min(durationMs, placement.crossfade_ms) * ppms}px`;
    overlay.title = `Crossfades in over ${(placement.crossfade_ms / 1000).toFixed(1)}s`;
    block.appendChild(overlay);
  }

  const fadeInHandle = document.createElement("div");
  fadeInHandle.className = "fade-handle fade-in";
  block.appendChild(fadeInHandle);

  const fadeOutHandle = document.createElement("div");
  fadeOutHandle.className = "fade-handle fade-out";
  block.appendChild(fadeOutHandle);

  attachBlockDragging(block, placement);
  attachFadeHandleDragging(fadeInHandle, placement, "fade_in_ms", +1);
  attachFadeHandleDragging(fadeOutHandle, placement, "fade_out_ms", -1);

  block.addEventListener("contextmenu", (e) => {
    e.preventDefault();
    openContextMenu(e.clientX, e.clientY, placement.placement_id);
  });

  block.addEventListener("click", (e) => {
    e.stopPropagation();
    document.querySelectorAll(".clip-block.selected").forEach((b) => b.classList.remove("selected"));
    block.classList.add("selected");
  });

  return block;
}

// ---------------------------------------------------------------------------
// Drag-and-drop from clip library onto the timeline
// ---------------------------------------------------------------------------

async function addClipToTimeline(clipId, startMs) {
  try {
    await api.post("/api/timeline/placements", {
      clip_id: clipId,
      start_ms: Math.max(0, Math.round(startMs)),
      fade_in_ms: 0,
      fade_out_ms: 0,
      trim_start_ms: 0,
      trim_end_ms: null,
    });
    await refreshTimeline();
  } catch (err) {
    alert(`Could not place clip: ${err.message}`);
  }
}

// End of the last placement on the timeline (0 if the timeline is empty),
// used as the default drop point for the "Add to Timeline" button.
function timelineEndMs() {
  let end = 0;
  for (const p of state.timeline.placements) {
    end = Math.max(end, p.start_ms + placementDurationMs(p));
  }
  return end;
}

function initTimelineDropTarget() {
  const track = document.getElementById("timeline-track");
  track.addEventListener("dragover", (e) => {
    e.preventDefault();
    e.dataTransfer.dropEffect = "copy";
  });
  track.addEventListener("drop", (e) => {
    e.preventDefault();
    const clipId = e.dataTransfer.getData("text/plain");
    if (!clipId) return;
    const rect = track.getBoundingClientRect();
    const offsetX = e.clientX - rect.left + track.scrollLeft;
    addClipToTimeline(clipId, offsetX / pxPerMs());
  });
}

// ---------------------------------------------------------------------------
// Block repositioning (drag left/right to change start_ms)
// ---------------------------------------------------------------------------

function attachBlockDragging(block, placement) {
  block.addEventListener("mousedown", (e) => {
    if (e.target.classList.contains("fade-handle")) return;
    e.preventDefault();
    const startX = e.clientX;
    const originalStart = placement.start_ms;
    const ppms = pxPerMs();

    function onMove(ev) {
      const deltaMs = (ev.clientX - startX) / ppms;
      const newStart = Math.max(0, Math.round(originalStart + deltaMs));
      block.style.left = `${newStart * ppms}px`;
      block.dataset.pendingStart = String(newStart);
    }

    async function onUp() {
      document.removeEventListener("mousemove", onMove);
      document.removeEventListener("mouseup", onUp);
      const pending = block.dataset.pendingStart;
      if (pending !== undefined) {
        delete block.dataset.pendingStart;
        try {
          await api.put(`/api/timeline/placements/${placement.placement_id}`, {
            start_ms: Number(pending),
          });
        } catch (err) {
          alert(`Could not move clip: ${err.message}`);
        }
        await refreshTimeline();
      }
    }

    document.addEventListener("mousemove", onMove);
    document.addEventListener("mouseup", onUp);
  });
}

// ---------------------------------------------------------------------------
// Fade handle dragging (drag block edges inward to set fade lengths)
// ---------------------------------------------------------------------------

function attachFadeHandleDragging(handle, placement, field, sign) {
  handle.addEventListener("mousedown", (e) => {
    e.preventDefault();
    e.stopPropagation();
    const startX = e.clientX;
    const original = placement[field] || 0;
    const durationMs = placementDurationMs(placement);
    const ppms = pxPerMs();

    function onMove(ev) {
      const deltaMs = ((ev.clientX - startX) / ppms) * sign;
      const next = Math.max(0, Math.min(durationMs, Math.round(original + deltaMs)));
      handle.dataset.pendingValue = String(next);
      // live visual feedback: re-render just this block's overlay width
      const block = handle.closest(".clip-block");
      const overlaySelector = field === "fade_in_ms" ? ".fade-overlay:not(.fade-out-overlay)" : ".fade-overlay.fade-out-overlay";
      let overlay = block.querySelector(overlaySelector);
      if (!overlay) {
        overlay = document.createElement("div");
        overlay.className = field === "fade_in_ms" ? "fade-overlay" : "fade-overlay fade-out-overlay";
        if (field === "fade_in_ms") overlay.style.left = "0"; else overlay.style.right = "0";
        block.appendChild(overlay);
      }
      overlay.style.width = `${Math.min(durationMs, next) * ppms}px`;
    }

    async function onUp() {
      document.removeEventListener("mousemove", onMove);
      document.removeEventListener("mouseup", onUp);
      const pending = handle.dataset.pendingValue;
      if (pending !== undefined) {
        delete handle.dataset.pendingValue;
        try {
          await api.put(`/api/timeline/placements/${placement.placement_id}`, {
            [field]: Number(pending),
          });
        } catch (err) {
          alert(`Could not adjust fade: ${err.message}`);
        }
        await refreshTimeline();
      }
    }

    document.addEventListener("mousemove", onMove);
    document.addEventListener("mouseup", onUp);
  });
}

// ---------------------------------------------------------------------------
// Context menu (Edit / Remove)
// ---------------------------------------------------------------------------

function openContextMenu(x, y, placementId) {
  state.contextPlacementId = placementId;
  const menu = document.getElementById("context-menu");
  menu.style.left = `${x}px`;
  menu.style.top = `${y}px`;
  menu.classList.remove("hidden");
}

function closeContextMenu() {
  document.getElementById("context-menu").classList.add("hidden");
  state.contextPlacementId = null;
}

function initContextMenu() {
  document.addEventListener("click", () => closeContextMenu());

  document.getElementById("ctx-remove").addEventListener("click", async () => {
    const id = state.contextPlacementId;
    closeContextMenu();
    if (!id) return;
    try {
      await api.del(`/api/timeline/placements/${id}`);
      await refreshTimeline();
    } catch (err) {
      alert(`Could not remove placement: ${err.message}`);
    }
  });

  document.getElementById("ctx-edit").addEventListener("click", () => {
    const id = state.contextPlacementId;
    closeContextMenu();
    if (!id) return;
    openEditModal(id);
  });

  document.getElementById("ctx-crossfade").addEventListener("click", () => {
    const id = state.contextPlacementId;
    closeContextMenu();
    if (!id) return;
    openCrossfadeModal(id);
  });
}

// ---------------------------------------------------------------------------
// Auto crossfade
// ---------------------------------------------------------------------------

// The placement that starts soonest after `placement` on the timeline -- the
// natural target to dissolve into.
function nextPlacementAfter(placement) {
  let best = null;
  for (const p of state.timeline.placements) {
    if (p.placement_id === placement.placement_id) continue;
    if (p.start_ms < placement.start_ms) continue;
    if (p.start_ms === placement.start_ms && p.placement_id <= placement.placement_id) continue;
    if (best === null || p.start_ms < best.start_ms) best = p;
  }
  return best;
}

function openCrossfadeModal(placementId) {
  const from = state.timeline.placements.find((p) => p.placement_id === placementId);
  if (!from) return;
  const to = nextPlacementAfter(from);
  if (!to) {
    alert("There's no clip after this one to crossfade into. Add or drag another clip so it starts later, then try again.");
    return;
  }

  const fromClip = clipById(from.clip_id);
  const toClip = clipById(to.clip_id);
  const modal = document.getElementById("crossfade-modal");
  modal.dataset.fromId = from.placement_id;
  modal.dataset.toId = to.placement_id;
  document.getElementById("crossfade-from").textContent = fromClip ? fromClip.name : "this clip";
  document.getElementById("crossfade-to").textContent = toClip ? toClip.name : "the next clip";

  // Default to 1s, but never longer than the shorter of the two clips.
  const maxLen = Math.max(1, Math.min(placementDurationMs(from), placementDurationMs(to)));
  document.getElementById("crossfade-duration-ms").value = String(Math.min(1000, maxLen));
  document.getElementById("crossfade-modal-error").classList.add("hidden");
  modal.classList.remove("hidden");
}

function initCrossfadeModal() {
  document.getElementById("btn-cancel-crossfade").addEventListener("click", () => {
    document.getElementById("crossfade-modal").classList.add("hidden");
  });

  document.getElementById("btn-apply-crossfade").addEventListener("click", async () => {
    const modal = document.getElementById("crossfade-modal");
    const errEl = document.getElementById("crossfade-modal-error");
    errEl.classList.add("hidden");
    const duration = Number(document.getElementById("crossfade-duration-ms").value);
    if (!Number.isFinite(duration) || duration <= 0) {
      errEl.textContent = "Enter a crossfade length greater than 0.";
      errEl.classList.remove("hidden");
      return;
    }
    try {
      await api.post("/api/timeline/crossfade", {
        placement_id_a: modal.dataset.fromId,
        placement_id_b: modal.dataset.toId,
        duration_ms: Math.round(duration),
      });
      modal.classList.add("hidden");
      await refreshTimeline();
    } catch (err) {
      errEl.textContent = err.message;
      errEl.classList.remove("hidden");
    }
  });
}

// ---------------------------------------------------------------------------
// Loop clip modal
// ---------------------------------------------------------------------------

const LOOP_MODE_HINTS = {
  pingpong:
    "Plays forward then back to the start. No jump anywhere in color or " +
    "brightness; motion reverses on the way back. Best for symmetric or " +
    "ambient looks. The loop is about twice the clip's length.",
  crossfade:
    "Keeps motion moving forward by dissolving the end back into the start. " +
    "Seamless at the loop point, but blends the start and end together over " +
    "the crossfade length and trims the loop slightly.",
};

function syncLoopModeUI() {
  const mode = document.getElementById("loop-mode").value;
  document.getElementById("loop-mode-hint").textContent = LOOP_MODE_HINTS[mode] || "";
  // The crossfade length only applies to the crossfade style.
  document.getElementById("loop-crossfade-row").classList.toggle("hidden", mode !== "crossfade");
}

function openLoopModal(clip) {
  const modal = document.getElementById("loop-modal");
  modal.dataset.clipId = clip.clip_id;
  document.getElementById("loop-source-name").textContent = clip.name;
  document.getElementById("loop-name").value = `${clip.name} (loop)`;
  document.getElementById("loop-mode").value = "pingpong";
  document.getElementById("loop-crossfade-ms").value = "1000";
  document.getElementById("loop-modal-error").classList.add("hidden");
  syncLoopModeUI();
  modal.classList.remove("hidden");
}

function initLoopModal() {
  document.getElementById("loop-mode").addEventListener("change", syncLoopModeUI);

  document.getElementById("btn-cancel-loop").addEventListener("click", () => {
    document.getElementById("loop-modal").classList.add("hidden");
  });

  document.getElementById("btn-apply-loop").addEventListener("click", async () => {
    const modal = document.getElementById("loop-modal");
    const errEl = document.getElementById("loop-modal-error");
    errEl.classList.add("hidden");

    const name = document.getElementById("loop-name").value.trim();
    if (!name) {
      errEl.textContent = "Enter a name for the new looped clip.";
      errEl.classList.remove("hidden");
      return;
    }
    const mode = document.getElementById("loop-mode").value;
    const crossfadeMs = Number(document.getElementById("loop-crossfade-ms").value);
    if (mode === "crossfade" && (!Number.isFinite(crossfadeMs) || crossfadeMs <= 0)) {
      errEl.textContent = "Enter a crossfade length greater than 0.";
      errEl.classList.remove("hidden");
      return;
    }

    try {
      await api.post(`/api/clips/${modal.dataset.clipId}/loop`, {
        name,
        mode,
        crossfade_ms: Math.round(crossfadeMs),
      });
      modal.classList.add("hidden");
      await refreshClips();
    } catch (err) {
      errEl.textContent = err.message;
      errEl.classList.remove("hidden");
    }
  });
}

// ---------------------------------------------------------------------------
// Edit placement modal
// ---------------------------------------------------------------------------

function openEditModal(placementId) {
  const placement = state.timeline.placements.find((p) => p.placement_id === placementId);
  if (!placement) return;

  document.getElementById("edit-modal").dataset.placementId = placementId;
  document.getElementById("edit-start-ms").value = placement.start_ms;
  document.getElementById("edit-fade-in-ms").value = placement.fade_in_ms;
  document.getElementById("edit-fade-out-ms").value = placement.fade_out_ms;
  document.getElementById("edit-crossfade-ms").value = placement.crossfade_ms || 0;
  document.getElementById("edit-trim-start-ms").value = placement.trim_start_ms;
  document.getElementById("edit-trim-end-ms").value = placement.trim_end_ms ?? "";
  document.getElementById("edit-modal").classList.remove("hidden");
}

function initEditModal() {
  document.getElementById("btn-cancel-edit").addEventListener("click", () => {
    document.getElementById("edit-modal").classList.add("hidden");
  });

  document.getElementById("btn-save-edit").addEventListener("click", async () => {
    const modal = document.getElementById("edit-modal");
    const placementId = modal.dataset.placementId;
    const trimEndRaw = document.getElementById("edit-trim-end-ms").value;

    const body = {
      start_ms: Number(document.getElementById("edit-start-ms").value),
      fade_in_ms: Number(document.getElementById("edit-fade-in-ms").value),
      fade_out_ms: Number(document.getElementById("edit-fade-out-ms").value),
      crossfade_ms: Number(document.getElementById("edit-crossfade-ms").value),
      trim_start_ms: Number(document.getElementById("edit-trim-start-ms").value),
      trim_end_ms: trimEndRaw === "" ? null : Number(trimEndRaw),
    };

    try {
      await api.put(`/api/timeline/placements/${placementId}`, body);
      modal.classList.add("hidden");
      await refreshTimeline();
    } catch (err) {
      alert(`Could not save placement: ${err.message}`);
    }
  });
}

// ---------------------------------------------------------------------------
// Record New Clip modal
// ---------------------------------------------------------------------------

function showRecordError(message) {
  const el = document.getElementById("record-modal-error");
  el.textContent = message;
  el.classList.remove("hidden");
}

function clearRecordError() {
  const el = document.getElementById("record-modal-error");
  el.textContent = "";
  el.classList.add("hidden");
}

function setRecordingFormVisible(visible) {
  document.getElementById("record-name").parentElement.classList.toggle("hidden", !visible);
  document.getElementById("record-universes").parentElement.classList.toggle("hidden", !visible);
  document.getElementById("record-protocol").parentElement.classList.toggle("hidden", !visible);
  document.getElementById("record-step-ms").parentElement.classList.toggle("hidden", !visible);
  document.getElementById("record-bind-address").parentElement.classList.toggle("hidden", !visible);
  document.getElementById("record-bind-address-hint").classList.toggle("hidden", !visible);
  document.getElementById("btn-start-record").classList.toggle("hidden", !visible);
  document.getElementById("recording-live").classList.toggle("hidden", visible);
}

async function populateBindAddressOptions() {
  const datalist = document.getElementById("record-bind-address-options");
  try {
    const data = await api.get("/api/record/network-interfaces");
    datalist.innerHTML = "";
    for (const addr of data.addresses) {
      const option = document.createElement("option");
      option.value = addr;
      datalist.appendChild(option);
    }
  } catch (e) {
    // non-essential convenience feature; the field still works as free text
  }
}

function openRecordModal() {
  clearRecordError();
  document.getElementById("record-name").value = "";
  document.getElementById("record-universes").value = "";
  document.getElementById("record-bind-address").value = "";
  setRecordingFormVisible(true);
  document.getElementById("record-modal").classList.remove("hidden");
  populateBindAddressOptions();
}

function closeRecordModal() {
  document.getElementById("record-modal").classList.add("hidden");
}

function parseUniverses(raw) {
  const parts = raw.split(",").map((s) => s.trim()).filter((s) => s.length > 0);
  const universes = parts.map((s) => parseInt(s, 10));
  if (universes.length === 0 || universes.some((u) => !Number.isInteger(u) || u < 1)) {
    return null;
  }
  return universes;
}

function updateHeaderRecordIndicator(status) {
  const indicator = document.getElementById("record-indicator");
  indicator.classList.toggle("hidden", !status.recording);
  if (status.recording) {
    const seconds = (status.elapsed_ms / 1000).toFixed(1);
    document.getElementById("record-indicator-text").textContent =
      `Recording "${status.clip_name}" – ${status.frame_count}f / ${seconds}s`;
  }
}

function updateModalLiveStats(status) {
  document.getElementById("recording-frame-count").textContent = status.frame_count;
  document.getElementById("recording-elapsed").textContent = (status.elapsed_ms / 1000).toFixed(1);
}

function startStatusPolling() {
  if (state.statusPoll) return;
  state.statusPoll = setInterval(async () => {
    try {
      const status = await api.get("/api/record/status");
      updateHeaderRecordIndicator(status);
      if (!document.getElementById("record-modal").classList.contains("hidden")) {
        updateModalLiveStats(status);
      }
      if (!status.recording) {
        clearInterval(state.statusPoll);
        state.statusPoll = null;
        stopRecordMeterPolling();
      }
    } catch (e) {
      // ignore transient poll errors
    }
  }, 500);
}

function startRecordMeterPolling() {
  if (state.recordMeterPoll) return;
  state.recordMeterPoll = setInterval(async () => {
    if (document.getElementById("record-modal").classList.contains("hidden")) return;
    try {
      const data = await api.get("/api/record/frame");
      drawChannelMeter("record-channel-meter", data.channels);
    } catch (e) {
      // recording likely just stopped; startStatusPolling's own check cleans this up
    }
  }, 150);
}

function stopRecordMeterPolling() {
  if (state.recordMeterPoll) {
    clearInterval(state.recordMeterPoll);
    state.recordMeterPoll = null;
  }
}

function initRecordModal() {
  document.getElementById("btn-new-clip").addEventListener("click", openRecordModal);
  document.getElementById("btn-cancel-record").addEventListener("click", closeRecordModal);

  document.getElementById("btn-start-record").addEventListener("click", async () => {
    clearRecordError();
    const name = document.getElementById("record-name").value.trim();
    const universes = parseUniverses(document.getElementById("record-universes").value);
    const protocol = document.getElementById("record-protocol").value;
    const stepMs = Number(document.getElementById("record-step-ms").value);
    const bindAddress = document.getElementById("record-bind-address").value.trim() || null;

    if (!name) {
      showRecordError("Name is required.");
      return;
    }
    if (!universes) {
      showRecordError("Enter at least one valid universe number (e.g. 1,2,3).");
      return;
    }

    try {
      await api.post("/api/record/start", {
        name,
        universes,
        step_ms: stepMs,
        protocol,
        bind_address: bindAddress,
      });
      setRecordingFormVisible(false);
      startStatusPolling();
      startRecordMeterPolling();
    } catch (err) {
      showRecordError(err.message);
    }
  });

  document.getElementById("btn-stop-record").addEventListener("click", async () => {
    try {
      await api.post("/api/record/stop");
      stopRecordMeterPolling();
      closeRecordModal();
      await refreshClips();
    } catch (err) {
      alert(`Could not stop recording: ${err.message}`);
    }
  });
}

// ---------------------------------------------------------------------------
// Export panel
// ---------------------------------------------------------------------------

async function refreshExports() {
  const exports = await api.get("/api/timeline/exports");
  const list = document.getElementById("exports-list");
  list.innerHTML = "";
  if (exports.length === 0) {
    const li = document.createElement("li");
    li.className = "clip-item-empty";
    li.textContent = "No exports yet.";
    list.appendChild(li);
    return;
  }
  for (const exp of exports) {
    const li = document.createElement("li");
    li.className = "export-item";
    const name = document.createElement("span");
    name.className = "export-name";
    name.textContent = `${exp.name} (${exp.duration_seconds.toFixed(1)}s)`;
    const path = document.createElement("span");
    path.className = "export-path";
    path.textContent = exp.path;
    li.appendChild(name);
    li.appendChild(path);
    list.appendChild(li);
  }
}

async function refreshExportDir() {
  try {
    const info = await api.get("/api/timeline/export-dir");
    const input = document.getElementById("export-dir-path");
    // Show the default's implicit path via placeholder only, so the field
    // stays empty (= "use default") until the user picks somewhere explicit.
    input.value = info.is_default ? "" : info.path;
  } catch (e) {
    // non-fatal; the field just stays as-is
  }
}

async function saveExportDir(path) {
  const info = await api.put("/api/timeline/export-dir", { path });
  const input = document.getElementById("export-dir-path");
  input.value = info.is_default ? "" : info.path;
  await refreshExports();
}

function initExportDirControls() {
  document.getElementById("export-dir-path").addEventListener("change", async (e) => {
    try {
      await saveExportDir(e.target.value.trim());
    } catch (err) {
      alert(`Could not set the export folder: ${err.message}`);
      await refreshExportDir();
    }
  });

  document.getElementById("btn-browse-export-dir").addEventListener("click", async () => {
    const btn = document.getElementById("btn-browse-export-dir");
    btn.disabled = true;
    try {
      const { path } = await api.post("/api/timeline/export-dir/browse");
      if (path) await saveExportDir(path);
    } catch (err) {
      alert(`Could not open the folder picker: ${err.message}`);
    } finally {
      btn.disabled = false;
    }
  });

  document.getElementById("btn-open-export-dir").addEventListener("click", async () => {
    try {
      await api.post("/api/timeline/export-dir/open");
    } catch (err) {
      alert(`Could not open the export folder: ${err.message}`);
    }
  });
}

function initExportPanel() {
  initExportDirControls();

  document.getElementById("btn-export").addEventListener("click", () => {
    document.getElementById("export-name").value = "";
    document.getElementById("export-modal-error").classList.add("hidden");
    document.getElementById("export-modal").classList.remove("hidden");
  });

  document.getElementById("btn-cancel-export").addEventListener("click", () => {
    document.getElementById("export-modal").classList.add("hidden");
  });

  document.getElementById("btn-confirm-export").addEventListener("click", async () => {
    const errEl = document.getElementById("export-modal-error");
    errEl.classList.add("hidden");
    const name = document.getElementById("export-name").value.trim();
    if (!name) {
      errEl.textContent = "File name is required.";
      errEl.classList.remove("hidden");
      return;
    }
    const channelCount = Number(document.getElementById("export-channel-count").value);
    const stepMs = Number(document.getElementById("export-step-ms").value);

    try {
      await api.post("/api/timeline/export", { name, channel_count: channelCount, step_ms: stepMs });
      document.getElementById("export-modal").classList.add("hidden");
      await refreshExports();
    } catch (err) {
      errEl.textContent = err.message;
      errEl.classList.remove("hidden");
    }
  });
}

// ---------------------------------------------------------------------------
// Playhead scrubber (visual stub — no playback engine yet)
// ---------------------------------------------------------------------------

function initScrubber() {
  const scrubber = document.getElementById("scrubber");
  scrubber.addEventListener("input", () => {
    const ms = Number(scrubber.value);
    state.playheadMs = ms;
    setPlayheadVisual(ms);
    fetchAndDrawFrame(ms);
    if (state.consoleOutputActive && !state.playing) {
      sendScrubToConsole(ms);
    }
  });
}

// ---------------------------------------------------------------------------
// Playback + live channel meter
// ---------------------------------------------------------------------------

const FRAME_FETCH_INTERVAL_MS = 80;

function currentMeterChannelCount() {
  const raw = Number(document.getElementById("export-channel-count").value);
  return Number.isFinite(raw) && raw > 0 ? Math.round(raw) : 512;
}

function setPlayheadVisual(ms) {
  const totalMs = timelineTotalMs();
  document.getElementById("scrubber").value = String(ms);
  document.getElementById("play-cursor").style.left = `${ms * pxPerMs()}px`;
  document.getElementById("playhead-label").textContent =
    `${(ms / 1000).toFixed(1)}s / ${(totalMs / 1000).toFixed(1)}s`;
}

async function fetchAndDrawFrame(ms) {
  if (state.frameFetchInFlight) return;
  state.frameFetchInFlight = true;
  try {
    const data = await api.get(
      `/api/timeline/frame?t_ms=${Math.round(ms)}&channel_count=${currentMeterChannelCount()}`
    );
    drawChannelMeter("channel-meter", data.channels);
  } catch (e) {
    // transient fetch errors during playback are not worth interrupting the user for
  } finally {
    state.frameFetchInFlight = false;
  }
}

function drawChannelMeter(canvasId, channels) {
  const canvas = document.getElementById(canvasId);
  const rect = canvas.getBoundingClientRect();
  if (canvas.width !== rect.width) canvas.width = rect.width;
  if (canvas.height !== rect.height) canvas.height = rect.height;

  const ctx = canvas.getContext("2d");
  const w = canvas.width;
  const h = canvas.height;
  ctx.clearRect(0, 0, w, h);

  if (!channels || channels.length === 0) return;

  const barWidth = w / channels.length;
  const accent = getComputedStyle(document.documentElement).getPropertyValue("--accent").trim() || "#4f8cff";

  for (let i = 0; i < channels.length; i++) {
    const level = channels[i] / 255;
    const barHeight = level * h;
    ctx.globalAlpha = 0.25 + 0.75 * level;
    ctx.fillStyle = accent;
    ctx.fillRect(i * barWidth, h - barHeight, Math.max(1, barWidth - 1), barHeight);
  }
  ctx.globalAlpha = 1;
}

function playbackTick() {
  if (!state.playing) return;
  const now = performance.now();

  const deltaMs = now - state.lastTickTs;
  state.lastTickTs = now;
  state.playheadMs += deltaMs;

  const totalMs = timelineTotalMs();
  if (state.playheadMs >= totalMs) {
    state.playheadMs = totalMs;
    setPlayheadVisual(state.playheadMs);
    fetchAndDrawFrame(state.playheadMs);
    stopPlayback();
    return;
  }

  setPlayheadVisual(state.playheadMs);
  if (now - state.lastFrameFetchTs >= FRAME_FETCH_INTERVAL_MS) {
    state.lastFrameFetchTs = now;
    fetchAndDrawFrame(state.playheadMs);
  }
}

// Live console (sACN) output. The "Send to console" checkbox opts in to
// streaming the current look out live: it holds the frame under the playhead
// while scrubbing/paused and advances while playing, without dropping the
// source. See PlaybackEngine in playback.py for the hold-vs-advance split.

function consoleOutputEnabled() {
  return document.getElementById("console-output-enabled").checked;
}

function consoleOutputParams() {
  return {
    channel_count: currentMeterChannelCount(),
    step_ms: state.timeline.settings.step_ms || 40,
    destination: document.getElementById("console-output-destination").value.trim() || null,
  };
}

function setConsoleOutputStatus(mode) {
  // mode: "playing" | "holding" | "" (off)
  const el = document.getElementById("console-output-status");
  if (!mode) {
    el.textContent = "";
    return;
  }
  const p = consoleOutputParams();
  const target = p.destination ? `to ${p.destination}` : "(multicast)";
  const verb = mode === "playing" ? "sending" : "holding";
  el.textContent = `● ${verb} ${p.channel_count}ch ${target}`;
}

// Start (or transition to) advancing output for Play.
async function startConsolePlay() {
  try {
    await api.post("/api/timeline/playback/start", {
      ...consoleOutputParams(),
      start_t_ms: state.playheadMs,
    });
    state.consoleOutputActive = true;
    setConsoleOutputStatus("playing");
  } catch (err) {
    setConsoleOutputStatus("");
    alert(`Could not start sending to console: ${err.message}`);
  }
}

// Bring the source up holding a single frame (checkbox toggle / destination change).
async function holdConsoleAt(ms) {
  try {
    await api.post("/api/timeline/playback/scrub", {
      ...consoleOutputParams(),
      t_ms: ms,
    });
    state.consoleOutputActive = true;
    if (!state.playing) setConsoleOutputStatus("holding");
  } catch (err) {
    setConsoleOutputStatus("");
    alert(`Could not send to console: ${err.message}`);
  }
}

// Coalescing throttle: scrubber "input" fires rapidly, so only one request is
// in flight at a time and the most recent position is always sent last.
function sendScrubToConsole(ms) {
  state.scrubPendingMs = ms;
  if (state.scrubSendInFlight) return;
  flushScrubToConsole();
}

async function flushScrubToConsole() {
  if (state.scrubPendingMs === null) return;
  const ms = state.scrubPendingMs;
  state.scrubPendingMs = null;
  state.scrubSendInFlight = true;
  try {
    await api.post("/api/timeline/playback/scrub", {
      ...consoleOutputParams(),
      t_ms: ms,
    });
    if (!state.playing) setConsoleOutputStatus("holding");
  } catch (err) {
    // transient errors mid-scrub are not worth interrupting the drag for
  } finally {
    state.scrubSendInFlight = false;
    if (state.scrubPendingMs !== null) flushScrubToConsole();
  }
}

// Keep the source up but stop advancing, holding the current frame.
async function pauseConsoleOutput() {
  if (!state.consoleOutputActive) return;
  try {
    await api.post("/api/timeline/playback/pause");
    setConsoleOutputStatus("holding");
  } catch (err) {
    // already stopped server-side -- fine
  }
}

// Tear the source down completely.
async function stopConsoleOutput() {
  if (!state.consoleOutputActive) return;
  state.consoleOutputActive = false;
  setConsoleOutputStatus("");
  try {
    await api.post("/api/timeline/playback/stop");
  } catch (err) {
    // already stopped server-side (e.g. it reached the end on its own) -- fine
  }
}

async function startPlayback() {
  if (state.playing) return;
  if (state.playheadMs >= timelineTotalMs()) {
    state.playheadMs = 0;
  }
  state.playing = true;
  state.lastTickTs = performance.now();
  state.lastFrameFetchTs = 0;
  document.getElementById("btn-play-pause").innerHTML = "&#10074;&#10074; Pause";
  // setInterval (not requestAnimationFrame) so playback keeps advancing even
  // when the tab/pane isn't visibly compositing -- rAF fully pauses then.
  state.rafId = setInterval(playbackTick, 50);

  if (consoleOutputEnabled()) {
    await startConsolePlay();
  }
}

async function stopPlayback() {
  state.playing = false;
  if (state.rafId) clearInterval(state.rafId);
  state.rafId = null;
  document.getElementById("btn-play-pause").innerHTML = "&#9658; Play";
  // Keep the live source up (holding the current frame) if the user still
  // wants output; only fully stop when they've opted out.
  if (consoleOutputEnabled()) {
    await pauseConsoleOutput();
  } else {
    await stopConsoleOutput();
  }
}

function initConsoleOutput() {
  document.getElementById("console-output-enabled").addEventListener("change", (e) => {
    if (e.target.checked) {
      if (state.playing) {
        startConsolePlay();
      } else {
        holdConsoleAt(state.playheadMs);
      }
    } else {
      stopConsoleOutput();
    }
  });
  // Retarget a live (held) source when the destination IP changes -- the sACN
  // sender has to be reopened for a new destination, so stop and re-hold.
  document.getElementById("console-output-destination").addEventListener("change", async () => {
    if (state.consoleOutputActive && !state.playing) {
      await stopConsoleOutput();
      await holdConsoleAt(state.playheadMs);
    }
  });
}

function initPlayback() {
  document.getElementById("btn-play-pause").addEventListener("click", () => {
    if (state.playing) {
      stopPlayback();
    } else {
      startPlayback();
    }
  });
  setPlayheadVisual(0);
}

// ---------------------------------------------------------------------------
// Project (new / open / save) -- the whole working state (all recordings + the
// timeline) is one portable .ftdproj bundle. See routes_project.py / bundle.py.
// ---------------------------------------------------------------------------

function updateProjectHeader() {
  const el = document.getElementById("current-project-name");
  const name = state.timeline.settings && state.timeline.settings.project_name;
  if (name) {
    el.textContent = name;
    el.classList.remove("untitled");
    el.title = name;
  } else {
    el.textContent = "Untitled";
    el.classList.add("untitled");
    el.title = "This project hasn't been saved yet";
  }
}

// New/Open discard the current project (recordings included), so guard when
// there's anything to lose. Returns true if it's OK to proceed.
function confirmDiscardProject(action) {
  const hasWork = state.clips.length > 0 || state.timeline.placements.length > 0;
  if (!hasWork) return true;
  return confirm(`${action} will delete the current recordings and clear the timeline. Save first if you want to keep them. Continue?`);
}

async function applyLoadedProject() {
  // Shared tail of new/open: the library and timeline both changed on the
  // server, so pull everything and reset the playhead.
  state.playheadMs = 0;
  await refreshClips();
  await refreshTimeline();
  updateProjectHeader();
  fetchAndDrawFrame(0);
}

async function newProject() {
  if (!confirmDiscardProject("Starting a new project")) return;
  try {
    await api.post("/api/project/new");
    await applyLoadedProject();
  } catch (err) {
    alert(`Could not start a new project: ${err.message}`);
  }
}

async function openProject() {
  if (!confirmDiscardProject("Opening a project")) return;
  const btn = document.getElementById("btn-project-open");
  const prev = btn.textContent;
  btn.disabled = true;
  btn.textContent = "Opening...";
  try {
    const res = await api.post("/api/project/open");
    if (res.cancelled) return;
    await applyLoadedProject();
    alert(`Opened "${res.current}" - ${res.clips_imported} recording(s) loaded.`);
  } catch (err) {
    alert(`Could not open project: ${err.message}`);
  } finally {
    btn.disabled = false;
    btn.textContent = prev;
  }
}

async function saveProject() {
  const btn = document.getElementById("btn-project-save");
  const prev = btn.textContent;
  btn.disabled = true;
  btn.textContent = "Saving...";
  try {
    const res = await api.post("/api/project/save");
    if (res.cancelled) return;
    // The saved file's name becomes the project name; reflect it in the header.
    await refreshTimeline();
    updateProjectHeader();
    alert(`Saved "${res.current}" (${res.clip_count} recording(s)) to:\n${res.path}`);
  } catch (err) {
    alert(`Could not save project: ${err.message}`);
  } finally {
    btn.disabled = false;
    btn.textContent = prev;
  }
}

function initProjectControls() {
  document.getElementById("btn-project-new").addEventListener("click", newProject);
  document.getElementById("btn-project-open").addEventListener("click", openProject);
  document.getElementById("btn-project-save").addEventListener("click", saveProject);
}

// ---------------------------------------------------------------------------
// Zoom
// ---------------------------------------------------------------------------

function initZoom() {
  document.getElementById("btn-zoom-in").addEventListener("click", () => {
    state.pxPerSec = Math.min(400, Math.round(state.pxPerSec * 1.4));
    renderTimeline();
  });
  document.getElementById("btn-zoom-out").addEventListener("click", () => {
    state.pxPerSec = Math.max(10, Math.round(state.pxPerSec / 1.4));
    renderTimeline();
  });
}

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------

async function init() {
  initTimelineDropTarget();
  initContextMenu();
  initEditModal();
  initCrossfadeModal();
  initLoopModal();
  initRecordModal();
  initExportPanel();
  initScrubber();
  initZoom();
  initPlayback();
  initConsoleOutput();
  initProjectControls();

  await refreshClips();
  await refreshTimeline();
  updateProjectHeader();
  await refreshExportDir();
  await refreshExports();

  try {
    const status = await api.get("/api/record/status");
    updateHeaderRecordIndicator(status);
    if (status.recording) startStatusPolling();
  } catch (e) {
    // server not reachable yet on first paint; ignore
  }
}

document.addEventListener("DOMContentLoaded", init);
