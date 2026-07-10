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
};

const CLIP_COLORS = [
  "#7fd4ff", "#ffd27f", "#a8ff7f", "#ff9ecb",
  "#c6a8ff", "#7fffe0", "#ffb37f", "#9ecbff",
];

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

    const addBtn = document.createElement("button");
    addBtn.className = "btn btn-small clip-add-btn";
    addBtn.textContent = "+ Add to Timeline";
    addBtn.title = "Add this clip to the end of the timeline";
    addBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      addClipToTimeline(clip.clip_id, timelineEndMs());
    });

    li.appendChild(name);
    li.appendChild(meta);
    li.appendChild(addBtn);

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

  for (const placement of state.timeline.placements) {
    track.appendChild(buildClipBlock(placement));
  }
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
  document.getElementById("btn-start-record").classList.toggle("hidden", !visible);
  document.getElementById("recording-live").classList.toggle("hidden", visible);
}

function openRecordModal() {
  clearRecordError();
  document.getElementById("record-name").value = "";
  document.getElementById("record-universes").value = "";
  setRecordingFormVisible(true);
  document.getElementById("record-modal").classList.remove("hidden");
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
      }
    } catch (e) {
      // ignore transient poll errors
    }
  }, 500);
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

    if (!name) {
      showRecordError("Name is required.");
      return;
    }
    if (!universes) {
      showRecordError("Enter at least one valid universe number (e.g. 1,2,3).");
      return;
    }

    try {
      await api.post("/api/record/start", { name, universes, step_ms: stepMs, protocol });
      setRecordingFormVisible(false);
      startStatusPolling();
    } catch (err) {
      showRecordError(err.message);
    }
  });

  document.getElementById("btn-stop-record").addEventListener("click", async () => {
    try {
      await api.post("/api/record/stop");
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

function initExportPanel() {
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
  const cursor = document.getElementById("play-cursor");
  scrubber.addEventListener("input", () => {
    const ms = Number(scrubber.value);
    cursor.style.left = `${ms * pxPerMs()}px`;
  });
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
  initRecordModal();
  initExportPanel();
  initScrubber();
  initZoom();

  await refreshClips();
  await refreshTimeline();
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
