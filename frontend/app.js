// ── Config ──────────────────────────────────────────────────────
const API_BASE = (() => {
  const params = new URLSearchParams(window.location.search);
  const rawQueryApi = params.get("api") || params.get("api_base") || params.get("apiBase");
  const savedApi = localStorage.getItem("GODEYE_API_BASE");
  const queryApi = sanitizeApiBase(rawQueryApi);

  if (queryApi) {
    localStorage.setItem("GODEYE_API_BASE", queryApi);
  }

  const configured = queryApi || sanitizeApiBase(savedApi);
  if (configured) return configured;

  const basePort = Number(window.location.port) || 8000;
  const fallbackPorts = new Set(["8001", "8006", "8007", "8008", "8009", "8010"]);
  const derived = basePort - 80;
  if (derived >= 1024 && derived <= 65535) {
    fallbackPorts.add(String(derived));
  }

  const host = window.location.hostname || "127.0.0.1";
  const proto = window.location.protocol || "http:";
  return `${proto}//${host}:${Array.from(fallbackPorts)[0]}`;
})();
const API_KEY = localStorage.getItem("GODEYE_API_KEY") || "";

// Module-level event store so the delegated row-expand handler can access raw data.
let lastEvents = [];
// Raw narrative text preserved for copy-to-clipboard.
let lastNarrativeText = "";
let lastReplayPayload = {};
let lastReplayMeta = {};
const DIAGNOSTIC_EMPTY = "—";
const PRESET_STORAGE_KEY = "GODEYE_REPLAY_PRESETS";
let currentRoleView = localStorage.getItem("GODEYE_ROLE_VIEW") || "analyst";

function loadPresets() {
  try {
    const parsed = JSON.parse(localStorage.getItem(PRESET_STORAGE_KEY) || "[]");
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

function savePresets(presets) {
  localStorage.setItem(PRESET_STORAGE_KEY, JSON.stringify(presets.slice(0, 30)));
}

function nowLabel() {
  return new Date().toISOString().replace("T", " ").replace("Z", " UTC");
}

function isNetworkError(error) {
  if (!error) return false;
  if (error.name === "TypeError") return true;
  return /Failed to fetch/i.test(String(error));
}

function toBlob(canvas) {
  return new Promise((resolve, reject) => {
    canvas.toBlob((blob) => {
      if (!blob) {
        reject(new Error("Failed to render PNG from card."));
        return;
      }
      resolve(blob);
    }, "image/png");
  });
}

function narrativeStatusMeta(status, fallbackReason = "") {
  const normalized = String(status || "ok");
  if (normalized === "llm_unavailable") {
    return {
      badgeClass: "card-badge--warning",
      title: "Fallback Narrative",
      detail: `LLM output unavailable. ${fallbackReason ? `${fallbackReason} ` : ""}This summary is generated from deterministic event telemetry.`,
    };
  }
  if (normalized === "missing_data") {
    return {
      badgeClass: "card-badge--muted",
      title: "Missing Data",
      detail: "Structured context was incomplete for this window. Narrative may be partial.",
    };
  }
  return {
    badgeClass: "",
    title: "AI Analysis",
    detail: "",
  };
}

function sanitizeApiBase(raw) {
  const base = String(raw || "").trim();
  if (!base) return "";
  const lowered = base.toLowerCase();
  if (lowered === "null" || lowered === "undefined" || lowered === "about:blank") {
    return "";
  }
  const hasProtocol = /^[a-zA-Z][a-zA-Z0-9+.-]*:\/\//.test(base);
  const normalized = hasProtocol ? base : `${window.location.protocol}//${base}`;
  return normalized.replace(/\/+$/, "");
}

function orderedApiCandidates() {
  const host = window.location.hostname || "127.0.0.1";
  const proto = window.location.protocol || "http:";
  const basePort = Number(window.location.port) || 0;
  const derived = basePort - 80;

  const ports = ["8001", "8006", "8007", "8008", "8009", "8010"];
  if (derived >= 1024 && derived <= 65535 && !ports.includes(String(derived))) {
    ports.unshift(String(derived));
  }

  const baseList = [API_BASE, ...ports.map((p) => `${proto}//${host}:${p}`), ...ports.map((p) => `http://127.0.0.1:${p}`)];
  const deduped = [];
  const seen = new Set();
  for (const base of baseList) {
    const candidate = sanitizeApiBase(base);
    if (!candidate || seen.has(candidate)) continue;
    seen.add(candidate);
    deduped.push(candidate);
  }
  return deduped;
}

function purgeBadBase(base) {
  const configured = sanitizeApiBase(localStorage.getItem("GODEYE_API_BASE"));
  if (configured && configured === sanitizeApiBase(base)) {
    localStorage.removeItem("GODEYE_API_BASE");
  }
}

async function resolveApiBase(signal, headers) {
  const candidates = orderedApiCandidates();
  let lastError = "No endpoints reachable.";
  let found = false;

  for (const base of candidates) {
    const healthUrl = `${base}/health`;
    try {
      const res = await fetch(healthUrl, { method: "GET", headers, signal });
      if (!res.ok) {
        const msg = `Health check failed (${res.status}) at ${healthUrl}`;
        lastError = msg;
        continue;
      }
      found = true;
      localStorage.setItem("GODEYE_API_BASE", base);
      return base;
    } catch (e) {
      if (signal.aborted) throw e;
      if (isNetworkError(e)) {
        const msg = `Network error while probing ${healthUrl}`;
        lastError = msg;
        purgeBadBase(base);
        continue;
      }
      throw e;
    }
  }

  if (!found) {
    localStorage.removeItem("GODEYE_API_BASE");
  }

  const sample = candidates.slice(0, 3).map((c) => `${c}/health`).join(", ");
  throw new Error(
    `${lastError}. Tried all candidates. Examples: ${sample}. ` +
    `No backend on expected ports. Start API first (e.g. python -m uvicorn api.replay.api:app --host 127.0.0.1 --port 8001), then rerun with ?api=http://127.0.0.1:8001`
  );
}

async function tryReplay(base, payload, headers, signal) {
  const response = await fetch(`${base}/api/replay`, {
    method: "POST",
    headers,
    body: JSON.stringify(payload),
    signal,
  });
  if (!response.ok) {
    const text = await response.text().catch(() => `HTTP ${response.status}`);
    throw new Error(`HTTP ${response.status}: ${text}`);
  }
  return response.json();
}

// ── Helpers ────────────────────────────────────────────────────
function escapeHtml(str) {
  return String(str ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function inlineMd(text) {
  return text
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .replace(/\*(.+?)\*/g,     "<em>$1</em>")
    .replace(/`([^`]+)`/g,     '<code class="md-code">$1</code>');
}

function renderMarkdown(raw) {
  const escaped = escapeHtml(raw);
  const blocks  = escaped.split(/\n{2,}/);

  return blocks.map(block => {
    const lines = block.split("\n");

    // Headings
    if (/^### /.test(lines[0])) return `<h3 class="md-h3">${inlineMd(lines[0].slice(4))}</h3>`;
    if (/^## /.test(lines[0]))  return `<h2 class="md-h2">${inlineMd(lines[0].slice(3))}</h2>`;
    if (/^# /.test(lines[0]))   return `<h1 class="md-h1">${inlineMd(lines[0].slice(2))}</h1>`;

    // Bullet list — block where every non-empty line starts with - or *
    const listLines = lines.filter(l => l.trim());
    if (listLines.length && listLines.every(l => /^[-*] /.test(l.trim()))) {
      const items = listLines
        .map(l => `<li>${inlineMd(l.replace(/^[-*] /, "").trim())}</li>`)
        .join("");
      return `<ul class="md-list">${items}</ul>`;
    }

    // Numbered list — block where every non-empty line starts with N. or N)
    if (listLines.length && listLines.every(l => /^\d+[.)]\s/.test(l.trim()))) {
      const items = listLines
        .map(l => `<li>${inlineMd(l.replace(/^\d+[.)]\s+/, "").trim())}</li>`)
        .join("");
      return `<ol class="md-list md-olist">${items}</ol>`;
    }

    // Paragraph
    return `<p class="md-p">${lines.map(inlineMd).join("<br>")}</p>`;
  }).join("");
}

function renderNarrativePanel(narrative, status, fallbackReason, query, eventsCount, prevCount) {
  const meta = narrativeStatusMeta(status, fallbackReason);
  const sourceText = meta.detail ? `<div class="narrative-banner ${meta.badgeClass === "card-badge--warning" ? "narrative-banner--warning" : ""}">${meta.detail}</div>` : "";
  const heading = `<div class="narrative-banner"><strong>Question:</strong> ${escapeHtml(query || "What happened?")} · <strong>Window Events:</strong> ${eventsCount}${prevCount != null ? ` · <strong>Baseline Events:</strong> ${prevCount}` : ""}</div>`;
  const body = narrative ? `<div class="narrative-text">${renderMarkdown(narrative)}</div>` : "<div class=\"empty-state\"><p>No narrative returned.</p></div>";
  return `${heading}${sourceText}${body}`;
}

function showToast(message, type = "success") {
  const icons = {
    success: `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="20 6 9 17 4 12"/></svg>`,
    error:   `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>`,
    info:    `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/></svg>`,
  };
  const toast = document.createElement("div");
  toast.className = `toast toast--${type}`;
  toast.innerHTML = (icons[type] ?? "") + escapeHtml(message);
  document.getElementById("toast-container").appendChild(toast);
  setTimeout(() => toast.remove(), 3000);
}

function setStatus(state) {
  const dot   = document.getElementById("statusDot");
  const label = document.getElementById("statusLabel");
  dot.className = "status-dot";
  const map = { idle: ["", "Idle"], running: ["status-dot--running", "Running…"], success: ["status-dot--success", "Complete"], error: ["status-dot--error", "Error"] };
  const [cls, text] = map[state] ?? map.idle;
  if (cls) dot.classList.add(cls);
  label.textContent = text;
}

function showSkeletons(parentId, count = 4) {
  const el = document.getElementById(parentId);
  el.innerHTML = Array.from({ length: count }, (_, i) => {
    const w = ["wide", "medium", "short", "medium"][i % 4];
    return `<div class="skeleton-block skeleton-block--${w}"></div>`;
  }).join("");
}

function applyRoleView(role) {
  currentRoleView = role === "operator" ? "operator" : "analyst";
  localStorage.setItem("GODEYE_ROLE_VIEW", currentRoleView);
  const hideForOperator = ["diagnosticPanel", "matrixCard"];
  hideForOperator.forEach((id) => {
    const el = document.getElementById(id);
    if (!el) return;
    el.style.display = currentRoleView === "operator" ? "none" : "";
  });
}

function initDashboardControls() {
  const panel = document.querySelector(".controls-panel");
  if (!panel || document.getElementById("godeyeExtraControls")) return;
  const host = document.createElement("div");
  host.id = "godeyeExtraControls";
  host.style.display = "grid";
  host.style.gridTemplateColumns = "1fr 1fr";
  host.style.gap = "0.6rem";
  host.style.marginTop = "0.65rem";
  host.innerHTML = `
    <label class="field">
      <span class="field__label">Role View</span>
      <select class="field__input mono" id="roleViewSelect">
        <option value="analyst">Analyst</option>
        <option value="operator">Operator</option>
      </select>
    </label>
    <div class="field">
      <span class="field__label">Replay Presets</span>
      <div style="display:flex; gap:.35rem;">
        <select class="field__input mono" id="presetSelect"></select>
        <button class="btn btn--primary btn--small" id="presetLoadBtn" type="button"><span>Load</span></button>
        <button class="btn btn--primary btn--small" id="presetSaveBtn" type="button"><span>Save</span></button>
        <button class="btn btn--primary btn--small" id="presetDeleteBtn" type="button"><span>Delete</span></button>
      </div>
    </div>
  `;
  panel.appendChild(host);

  const presetSelect = document.getElementById("presetSelect");
  const roleSelect = document.getElementById("roleViewSelect");

  function refreshPresetSelect() {
    const presets = loadPresets();
    presetSelect.innerHTML = `<option value="">Select preset…</option>` + presets.map((p, idx) => `<option value="${idx}">${escapeHtml(p.name)}</option>`).join("");
  }

  roleSelect.value = currentRoleView;
  roleSelect.addEventListener("change", () => applyRoleView(roleSelect.value));
  applyRoleView(roleSelect.value);
  refreshPresetSelect();

  document.getElementById("presetSaveBtn").addEventListener("click", () => {
    const name = `Preset ${nowLabel()}`;
    const presets = loadPresets();
    presets.unshift({
      name,
      fromTime: document.getElementById("fromTime").value.trim(),
      toTime: document.getElementById("toTime").value.trim(),
      region: document.getElementById("region").value.trim(),
      scenario: document.getElementById("scenario").value.trim(),
      query: document.getElementById("query").value.trim(),
    });
    savePresets(presets);
    refreshPresetSelect();
    presetSelect.value = "0";
    showToast("Preset saved.", "success");
  });

  document.getElementById("presetLoadBtn").addEventListener("click", () => {
    const idx = Number(presetSelect.value);
    if (!Number.isInteger(idx) || idx < 0) return;
    const presets = loadPresets();
    const p = presets[idx];
    if (!p) return;
    document.getElementById("fromTime").value = p.fromTime || "";
    document.getElementById("toTime").value = p.toTime || "";
    document.getElementById("region").value = p.region || "";
    document.getElementById("scenario").value = p.scenario || "";
    document.getElementById("query").value = p.query || "";
    showToast("Preset loaded.", "info");
  });

  document.getElementById("presetDeleteBtn").addEventListener("click", () => {
    const idx = Number(presetSelect.value);
    if (!Number.isInteger(idx) || idx < 0) return;
    const presets = loadPresets();
    if (!presets[idx]) return;
    presets.splice(idx, 1);
    savePresets(presets);
    refreshPresetSelect();
    showToast("Preset deleted.", "info");
  });
}

function formatDiagStatusValue(value) {
  if (value === null || value === undefined || value === "") return DIAGNOSTIC_EMPTY;
  return String(value);
}

function setDiagnosticDefaults() {
  document.getElementById("diagApiBase").textContent = "Resolving…";
  document.getElementById("diagNarrativeStatus").textContent = DIAGNOSTIC_EMPTY;
  document.getElementById("diagSummaryStatus").textContent = DIAGNOSTIC_EMPTY;
  document.getElementById("diagObservationsStatus").textContent = DIAGNOSTIC_EMPTY;
  document.getElementById("diagEventsCount").textContent = DIAGNOSTIC_EMPTY;
  document.getElementById("diagPrevEventsCount").textContent = DIAGNOSTIC_EMPTY;
  document.getElementById("diagLastRun").textContent = DIAGNOSTIC_EMPTY;
}

function updateDiagnostics({
  apiBase = "",
  narrativeStatus = "",
  summaryStatus = "",
  observationsStatus = "",
  eventsCount = null,
  prevEventsCount = null,
  lastRun = "",
}) {
  document.getElementById("diagApiBase").textContent = formatDiagStatusValue(apiBase || "Not resolved");
  document.getElementById("diagNarrativeStatus").textContent = formatDiagStatusValue(narrativeStatus);
  document.getElementById("diagSummaryStatus").textContent = formatDiagStatusValue(summaryStatus);
  document.getElementById("diagObservationsStatus").textContent = formatDiagStatusValue(observationsStatus);
  document.getElementById("diagEventsCount").textContent = formatDiagStatusValue(eventsCount);
  document.getElementById("diagPrevEventsCount").textContent = formatDiagStatusValue(prevEventsCount);
  document.getElementById("diagLastRun").textContent = formatDiagStatusValue(lastRun);
}

function buildTimelineFallbackMetrics(events = [], prevEvents = []) {
  const ev = Array.isArray(events) ? events : [];
  const prev = Array.isArray(prevEvents) ? prevEvents : [];
  const axisCounts = {};
  const severityCounts = {};
  const confidences = [];
  const bucketCounts = {};

  ev.forEach((item) => {
    if (!item || typeof item !== "object") return;

    const axis = String(item.axis || "unknown").toLowerCase();
    const severity = String(item.severity || "unknown").toLowerCase();
    axisCounts[axis] = (axisCounts[axis] || 0) + 1;
    severityCounts[severity] = (severityCounts[severity] || 0) + 1;

    const conf = Number(item.confidence);
    if (Number.isFinite(conf)) {
      confidences.push(Math.max(0, Math.min(1, conf)));
    }

    const raw = String(item.start_time || "").replace("Z", "+00:00");
    const dt = new Date(raw);
    if (!Number.isNaN(dt.getTime())) {
      const bucket = `${String(dt.getUTCFullYear()).padStart(4, "0")}-${String(dt.getUTCMonth() + 1).padStart(2, "0")}-${String(dt.getUTCDate()).padStart(2, "0")} ${String(dt.getUTCHours()).padStart(2, "0")}:${String(Math.floor(dt.getUTCMinutes() / 10) * 10).padStart(2, "0")}`;
      bucketCounts[bucket] = (bucketCounts[bucket] || 0) + 1;
    }
  });

  const confidenceAvg = confidences.length ? confidences.reduce((sum, value) => sum + value, 0) / confidences.length : 0;
  const confidenceMin = confidences.length ? Math.min(...confidences) : 0;
  const confidenceMax = confidences.length ? Math.max(...confidences) : 0;

  return {
    event_count: ev.length,
    prev_event_count: prev.length,
    axis_distribution: axisCounts,
    severity_distribution: severityCounts,
    confidence: {
      count: confidences.length,
      avg: Number(confidenceAvg.toFixed(3)),
      min: Number(confidenceMin.toFixed(3)),
      max: Number(confidenceMax.toFixed(3)),
    },
    timeline_buckets: Object.entries(bucketCounts)
      .sort(([a], [b]) => String(a).localeCompare(String(b)))
      .map(([bucket, count]) => ({ bucket, count })),
    retrieval_counts: {},
  };
}

function extractNarrativeUnavailableReason(payload) {
  if (!payload || typeof payload !== "object") return "";
  const status = String(payload.narrative_status || "ok");
  if (status === "ok") return "";
  if (payload.narrative_status_reason) return String(payload.narrative_status_reason);

  const narrative = String(payload.narrative || "").trim().toLowerCase();
  if (narrative.startsWith("narrative unavailable")) {
    return String(payload.narrative);
  }
  return "";
}

function renderSeverityDist(events) {
  const old = document.getElementById("severityDist");
  if (old) old.remove();
  if (!events.length) return;

  const counts = { high: 0, medium: 0, low: 0 };
  events.forEach(ev => {
    const s = String(ev.severity ?? "").toLowerCase();
    if (s in counts) counts[s]++;
  });

  const pills = Object.entries(counts)
    .filter(([, n]) => n > 0)
    .map(([sev, n]) => `<span class="sev-pill sev-pill--${sev}">${n} ${sev}</span>`)
    .join("");
  if (!pills) return;

  const bar = document.createElement("div");
  bar.id = "severityDist";
  bar.className = "severity-dist";
  bar.innerHTML = `<span class="severity-dist__label">Distribution</span>${pills}`;

  const cardBody = document.querySelector(".card-body--table");
  cardBody.insertBefore(bar, cardBody.firstChild);
}

function renderMiniBars(containerId, sourceObj, labelOrder) {
  const container = document.getElementById(containerId);
  if (!container) return;

  const entries = labelOrder.map((label) => ({ label, value: Number(sourceObj[label] || 0) }));

  const allValues = entries.map((e) => e.value);
  const max = Math.max(1, ...allValues);
  container.innerHTML = entries.length
    ? entries
        .map(({ label, value }) => {
          const pct = Math.round((value / max) * 100);
          return `<div class="mini-bars__row"><span class="mini-bars__label">${escapeHtml(label)}</span><div class="mini-bars__track"><div class="mini-bars__fill" style="width:${pct}%"></div></div><span class="mini-bars__count">${value}</span></div>`;
        })
        .join("")
    : `<div class="mini-bars__row"><span class="mini-bars__label">No distribution</span><div class="mini-bars__track"><div class="mini-bars__fill" style="width:0%"></div></div><span class="mini-bars__count">0</span></div>`;
}

function formatConfPct(value) {
  return typeof value === "number" ? `${(value * 100).toFixed(0)}%` : "0%";
}

function renderTimelineBuckets(containerId, buckets) {
  const container = document.getElementById(containerId);
  if (!container) return;

  const rows = Array.isArray(buckets) ? buckets : [];
  if (!rows.length) {
    container.innerHTML = `<div class="empty-state"><p style="padding:0.2rem 0;">No bucketed timeline data.</p></div>`;
    return;
  }

  const max = Math.max(1, ...rows.map((row) => Number(row.count || 0)));
  container.innerHTML = rows
    .map((row) => {
      const bucket = String(row.bucket || "unknown");
      const count = Number(row.count || 0);
      const pct = Math.round((count / max) * 100);
      return `<div class="mini-bars__row"><span class="mini-bars__label">${escapeHtml(bucket)}</span><div class="mini-bars__track"><div class="mini-bars__fill" style="width:${pct}%"></div></div><span class="mini-bars__count">${count}</span></div>`;
    })
        .join("");
}

function normalizeFeedType(value) {
  const raw = String(value || "").trim().toLowerCase();
  if (!raw) return "unknown";
  if (raw === "net") return "net";
  if (raw === "jamming") return "jamming";
  if (raw === "sat_pass") return "sat_pass";
  return raw;
}

function formatObservationFeedLabel(feedType) {
  const labelMap = {
    adsb: "ADS-B",
    ais: "AIS",
    jamming: "JAM",
    net: "NET",
    sat_pass: "SAT",
  };
  return labelMap[normalizeFeedType(feedType)] || "UNK";
}

function extractObservationPosition(obs) {
  const pos = obs?.position;
  if (!pos) return null;
  if (typeof pos === "object") {
    const lat = Number(pos.lat ?? pos.latitude ?? pos.y ?? pos[1]);
    const lon = Number(pos.lon ?? pos.lng ?? pos.longitude ?? pos.x ?? pos[0]);
    if (Number.isFinite(lat) && Number.isFinite(lon)) {
      return { lat, lon };
    }
  }
  if (typeof pos === "string") {
    try {
      const parsed = JSON.parse(pos);
      if (parsed && typeof parsed.lat === "number" && typeof parsed.lon === "number") {
        return { lat: Number(parsed.lat), lon: Number(parsed.lon) };
      }
    } catch {
      return null;
    }
  }

  const lat = Number(pos.lat ?? pos.latitude);
  const lon = Number(pos.lon ?? pos.longitude);
  if (!Number.isFinite(lat) || !Number.isFinite(lon)) return null;
  return { lat, lon };
}

function formatMapCoordinate(v, fallback = "—") {
  if (v == null || Number.isNaN(Number(v))) return fallback;
  return Number(v).toFixed(3);
}

function normalizeRecordId(idValue) {
  if (idValue == null) return "";
  if (typeof idValue === "string") return idValue;
  if (typeof idValue === "object") {
    if (idValue.id) return String(idValue.id);
    if (idValue.record_id) return String(idValue.record_id);
  }
  return String(idValue);
}

function safeObservationPayload(obs) {
  return {
    feedType: normalizeFeedType(obs?.feed_type || obs?.feedType),
    entity: obs?.entity_name || obs?.entityId || obs?.entity || "Unknown",
    time: obs?.time || obs?.timestamp || "—",
    id: normalizeRecordId(obs?.id || obs?._id || ""),
    raw: obs?.raw || {},
  };
}

function renderObservationFallbackList(observations) {
  const rows = observations
    .slice(0, 8)
    .map((obs) => {
      const payload = safeObservationPayload(obs);
      const note = payload.raw && typeof payload.raw === "object" ? payload.raw.note || payload.raw.src : "";
      return `<div class="map-list-row"><span>${escapeHtml(String(payload.id))}</span><span>${escapeHtml(payload.feedType)}</span><span>${escapeHtml(payload.entity)}</span><span>${escapeHtml(payload.time)}</span><span>${escapeHtml(note ? String(note) : "—")}</span></div>`;
    })
    .join("");

  return `
    <div class="map-list">
      <div class="map-list-head">
        <span>ID</span><span>Feed</span><span>Entity</span><span>Time</span><span>Note</span>
      </div>
      ${rows || `<div class="map-list-row"><span style=\"opacity:.8\">No records to list.</span></div>`}
    </div>
  `;
}

async function fetchObservations(base, fromTime, toTime, headers, signal) {
  const url = `${base}/api/observations?from_time=${encodeURIComponent(fromTime)}&to_time=${encodeURIComponent(toTime)}&limit=300`;
  const response = await fetch(url, { headers, signal });
  if (!response.ok) {
    const text = await response.text().catch(() => `HTTP ${response.status}`);
    throw new Error(`Observations query failed (${response.status}): ${text}`);
  }
  const payload = await response.json().catch(() => ({}));
  return Array.isArray(payload.observations) ? payload.observations : [];
}

function renderObservationMap(observations, events, diagnostic = {}) {
  const mapBody = document.getElementById("mapBody");
  const countChip = document.getElementById("observationCount");
  if (!mapBody) return;

  const diag = {
    error: String(diagnostic.error || "").trim(),
    fromTime: String(diagnostic.fromTime || ""),
    toTime: String(diagnostic.toTime || ""),
  };

  if (diag.error) {
    mapBody.innerHTML = `<div class="empty-state"><p class="narrative-banner narrative-banner--warning"><strong>Observation query failed.</strong> ${escapeHtml(diag.error)}</p><p style="margin-top:.45rem">Window: ${escapeHtml(diag.fromTime || "selected start")} → ${escapeHtml(diag.toTime || "selected end")}</p></div>`;
    if (countChip) countChip.textContent = "—";
    return;
  }

  const points = Array.isArray(observations) ? observations : [];
  if (!points.length) {
    mapBody.innerHTML = `<div class=\"empty-state\"><svg width=\"26\" height=\"26\" viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"1.5\" opacity=\"0.22\"><path d=\"M3 6l6 2 6-2 6 2v12l-6-2-6 2-6-2z\"/><path d=\"M9 8v12\"/><path d=\"M15 6v12\"/></svg><p>No observations in selected window.</p><p style=\"margin-top:.45rem\">Try widening the time window or checking whether the selected scenario contains observations.</p></div>`;
    if (countChip) countChip.textContent = "0 observations";
    return;
  }

  const parsedPoints = points
    .map((obs) => {
      const coord = extractObservationPosition(obs);
      if (!coord) return null;
      const safeTime = obs?.time || obs?.timestamp || "";
      const parsedTime = Date.parse(String(safeTime));
      return {
        ...coord,
        ...safeObservationPayload(obs),
        raw: obs,
        timeMs: Number.isFinite(parsedTime) ? parsedTime : null,
      };
    })
    .filter(Boolean);

  if (!parsedPoints.length) {
    mapBody.innerHTML = `<div class=\"empty-state\"><svg width=\"26\" height=\"26\" viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"1.5\" opacity=\"0.22\"><path d=\"M3 6l6 2 6-2 6 2v12l-6-2-6 2-6-2z\"/><path d=\"M9 8v12\"/><path d=\"M15 6v12\"/></svg><p>Observations missing location coordinates.</p><p style=\"margin-top:.45rem\">Enable lat/lon on observation ingestion to render the map view.</p></div>`;
    if (countChip) countChip.textContent = `${points.length} observations`;
    if (points.length) {
      mapBody.innerHTML += renderObservationFallbackList(points);
    }
    return;
  }

  const lats = parsedPoints.map((item) => item.lat);
  const lons = parsedPoints.map((item) => item.lon);
  const minLat = Math.min(...lats);
  const maxLat = Math.max(...lats);
  const minLon = Math.min(...lons);
  const maxLon = Math.max(...lons);
  const latPad = Math.max((maxLat - minLat) * 0.15, 0.2);
  const lonPad = Math.max((maxLon - minLon) * 0.15, 0.2);
  const bounds = {
    minLat: minLat - latPad,
    maxLat: maxLat + latPad,
    minLon: minLon - lonPad,
    maxLon: maxLon + lonPad,
  };
  const latSpan = Math.max(1e-6, bounds.maxLat - bounds.minLat);
  const lonSpan = Math.max(1e-6, bounds.maxLon - bounds.minLon);

  const markerRows = parsedPoints
    .map((item, index) => {
      const x = ((item.lon - bounds.minLon) / lonSpan) * 100;
      const y = (1 - (item.lat - bounds.minLat) / latSpan) * 100;
      return `<button class="map-marker map-marker--${escapeHtml(item.feedType)}" style="left:${x.toFixed(2)}%;top:${y.toFixed(2)}%;" data-feed="${escapeHtml(item.feedType)}" data-feed-label="${escapeHtml(formatObservationFeedLabel(item.feedType))}" data-idx="${index}" title="${escapeHtml(`${item.entity} | ${formatObservationFeedLabel(item.feedType)} | ${item.time} | ${formatMapCoordinate(item.lat)},${formatMapCoordinate(item.lon)}`)}" aria-label="${escapeHtml(`${item.entity} ${formatObservationFeedLabel(item.feedType)} at ${item.time}`)}"></button>`;
    })
    .join("");

  const feedOrder = ["adsb", "ais", "jamming", "net", "sat_pass", "unknown"];
  const feedCounts = {};
  parsedPoints.forEach((item) => {
    feedCounts[item.feedType] = (feedCounts[item.feedType] || 0) + 1;
  });
  const highSeverityFeeds = new Set(
    (Array.isArray(events) ? events : [])
      .filter((ev) => String(ev?.severity || "").toLowerCase() === "high")
      .flatMap((ev) => (Array.isArray(ev?.source_tags) ? ev.source_tags : []))
      .map((tag) => String(tag || "").toLowerCase())
      .filter((tag) => tag && !tag.startsWith("auto-")),
  );
  const anomalyFeeds = new Set(
    (Array.isArray(events) ? events : [])
      .filter((ev) => String(ev?.type || "").toLowerCase() === "anomaly")
      .flatMap((ev) => (Array.isArray(ev?.source_tags) ? ev.source_tags : []))
      .map((tag) => String(tag || "").toLowerCase())
      .filter((tag) => tag && !tag.startsWith("auto-")),
  );
  const timePoints = parsedPoints
    .map((item) => item.timeMs)
    .filter((value) => typeof value === "number")
    .sort((a, b) => a - b);
  const uniqueTimePoints = [...new Set(timePoints)];

  const filterChips = [
    `<button class="map-filter-chip is-active" data-filter="all" type="button">All <span>${parsedPoints.length}</span></button>`,
    ...feedOrder
      .filter((feed) => (feedCounts[feed] || 0) > 0)
      .map((feed) => `<button class="map-filter-chip" data-filter="${feed}" type="button">${escapeHtml(formatObservationFeedLabel(feed))} <span>${feedCounts[feed]}</span></button>`),
  ].join("");
  const modeButtons = `
    <div class="map-mode-bar" id="mapModeBar">
      <button class="map-mode-btn is-active" data-mode="all" type="button">All Records</button>
      <button class="map-mode-btn" data-mode="anomaly" type="button">Only Anomalies</button>
      <button class="map-mode-btn" data-mode="high" type="button">High-Severity Linked</button>
    </div>`;
  const timeScrubber = uniqueTimePoints.length > 1
    ? `<div class="map-time-bar" id="mapTimeBar">
        <span class="map-time-bar__label">Timeline</span>
        <input type="range" min="0" max="${uniqueTimePoints.length - 1}" value="${uniqueTimePoints.length - 1}" step="1" id="mapTimeSlider" />
        <span class="map-time-bar__value" id="mapTimeValue">${escapeHtml(new Date(uniqueTimePoints[uniqueTimePoints.length - 1]).toISOString())}</span>
        <button type="button" class="map-time-play-btn" id="mapTimePlayBtn">Play</button>
      </div>`
    : "";

  const detailPanel = `
    <div class="map-detail" id="mapDetailPanel">
      <div class="map-detail__title">Observation Detail</div>
      <div class="map-detail__body" id="mapDetailBody">
        <div class="map-detail__row"><span>Entity</span><strong>Select a marker</strong></div>
      </div>
    </div>`;

  const mapList = `
    <div class="map-list" id="mapInteractiveList">
      <div class="map-list-head">
        <span>ID</span><span>Feed</span><span>Entity</span><span>Time</span><span>Coords</span>
      </div>
      <div id="mapListRows"></div>
    </div>`;

  const legendHtml = `
    <div class="map-legend">
      <span class="map-legend__item"><span class="map-legend__dot map-marker--adsb" style="display:inline-block"></span>ADS-B</span>
      <span class="map-legend__item"><span class="map-legend__dot map-marker--ais" style="display:inline-block"></span>AIS</span>
      <span class="map-legend__item"><span class="map-legend__dot map-marker--jamming" style="display:inline-block"></span>Jamming</span>
      <span class="map-legend__item"><span class="map-legend__dot map-marker--net" style="display:inline-block"></span>Net</span>
      <span class="map-legend__item"><span class="map-legend__dot map-marker--sat_pass" style="display:inline-block"></span>Sat</span>
      <span class="map-legend__item"><span class="map-legend__dot map-marker--unknown" style="display:inline-block"></span>Unknown</span>
    </div>`;

  const title = `<div class="map-toolbar"><span>Observed records: <strong id="mapVisibleCount">${parsedPoints.length}</strong> / ${points.length}</span><span>${events?.length || 0} derived events</span></div>`;
  mapBody.innerHTML = `${title}${modeButtons}<div class="map-filter-bar">${filterChips}</div>${timeScrubber}<div class="map-canvas">${markerRows}</div>${detailPanel}${legendHtml}${mapList}`;

  const mapCanvas = mapBody.querySelector(".map-canvas");
  const markerEls = Array.from(mapBody.querySelectorAll(".map-marker"));
  const filterEls = Array.from(mapBody.querySelectorAll(".map-filter-chip"));
  const modeEls = Array.from(mapBody.querySelectorAll(".map-mode-btn"));
  const timeSlider = mapBody.querySelector("#mapTimeSlider");
  const timeValue = mapBody.querySelector("#mapTimeValue");
  const timePlayBtn = mapBody.querySelector("#mapTimePlayBtn");
  const detailBody = mapBody.querySelector("#mapDetailBody");
  const listRows = mapBody.querySelector("#mapListRows");
  const visibleCountEl = mapBody.querySelector("#mapVisibleCount");
  let activeFeed = "all";
  let activeMode = "all";
  let timeCursor = uniqueTimePoints.length ? uniqueTimePoints[uniqueTimePoints.length - 1] : null;
  let selectedIdx = null;
  let playTimer = null;

  function renderDetail(item) {
    if (!detailBody) return;
    if (!item) {
      detailBody.innerHTML = `<div class="map-detail__row"><span>Entity</span><strong>No visible marker</strong></div>`;
      return;
    }
    const note = item.raw && typeof item.raw === "object" ? item.raw.note || item.raw.src || "—" : "—";
    detailBody.innerHTML = `
      <div class="map-detail__row"><span>Entity</span><strong>${escapeHtml(item.entity)}</strong></div>
      <div class="map-detail__row"><span>Feed</span><strong>${escapeHtml(formatObservationFeedLabel(item.feedType))}</strong></div>
      <div class="map-detail__row"><span>Time</span><strong>${escapeHtml(item.time)}</strong></div>
      <div class="map-detail__row"><span>Coords</span><strong>${formatMapCoordinate(item.lat)}, ${formatMapCoordinate(item.lon)}</strong></div>
      <div class="map-detail__row"><span>ID</span><strong>${escapeHtml(String(item.id || "—"))}</strong></div>
      <div class="map-detail__row"><span>Note</span><strong>${escapeHtml(String(note))}</strong></div>
    `;
  }

  function renderListRows(visiblePoints) {
    if (!listRows) return;
    listRows.innerHTML = visiblePoints
      .slice()
      .sort((a, b) => String(a.time).localeCompare(String(b.time)))
      .map((item) => {
        const isSelected = item.idx === selectedIdx ? " is-selected" : "";
        return `<button type="button" class="map-list-row${isSelected}" data-idx="${item.idx}"><span>${escapeHtml(String(item.id || "—"))}</span><span>${escapeHtml(formatObservationFeedLabel(item.feedType))}</span><span>${escapeHtml(item.entity)}</span><span>${escapeHtml(item.time)}</span><span>${formatMapCoordinate(item.lat)}, ${formatMapCoordinate(item.lon)}</span></button>`;
      })
      .join("");
  }

  function applyFilter(nextFeed, nextMode = activeMode, nextTimeCursor = timeCursor) {
    activeFeed = nextFeed;
    activeMode = nextMode;
    timeCursor = nextTimeCursor;
    filterEls.forEach((chip) => {
      chip.classList.toggle("is-active", chip.dataset.filter === activeFeed);
    });
    modeEls.forEach((modeBtn) => {
      modeBtn.classList.toggle("is-active", modeBtn.dataset.mode === activeMode);
    });
    if (timeValue && typeof timeCursor === "number") {
      timeValue.textContent = new Date(timeCursor).toISOString();
    }

    let visibleCount = 0;
    markerEls.forEach((marker) => {
      const feed = marker.dataset.feed || "unknown";
      const idx = Number(marker.dataset.idx);
      const item = parsedPoints[idx];
      const feedMatch = activeFeed === "all" || feed === activeFeed;
      const modeMatch = activeMode === "all"
        ? true
        : activeMode === "anomaly"
          ? anomalyFeeds.has(item.feedType)
          : highSeverityFeeds.has(item.feedType);
      const timeMatch = typeof timeCursor === "number"
        ? (typeof item.timeMs === "number" ? item.timeMs <= timeCursor : true)
        : true;
      const show = feedMatch && modeMatch && timeMatch;
      marker.classList.toggle("is-hidden", !show);
      if (show) visibleCount += 1;
    });

    if (visibleCountEl) {
      visibleCountEl.textContent = String(visibleCount);
    }

    const visiblePoints = parsedPoints
      .map((item, idx) => ({ ...item, idx }))
      .filter((item) => activeFeed === "all" || item.feedType === activeFeed)
      .filter((item) => {
        if (activeMode === "all") return true;
        if (activeMode === "anomaly") return anomalyFeeds.has(item.feedType);
        return highSeverityFeeds.has(item.feedType);
      })
      .filter((item) => {
        if (typeof timeCursor !== "number") return true;
        return typeof item.timeMs === "number" ? item.timeMs <= timeCursor : true;
      });

    if (selectedIdx == null || !visiblePoints.some((item) => item.idx === selectedIdx)) {
      selectedIdx = visiblePoints.length ? visiblePoints[0].idx : null;
    }
    markerEls.forEach((marker) => {
      marker.classList.toggle("is-selected", Number(marker.dataset.idx) === selectedIdx);
    });
    renderDetail(selectedIdx == null ? null : parsedPoints[selectedIdx]);
    renderListRows(visiblePoints);
  }

  if (mapCanvas) {
    mapCanvas.addEventListener("click", (event) => {
      const marker = event.target.closest(".map-marker");
      if (!marker) return;
      selectedIdx = Number(marker.dataset.idx);
      applyFilter(activeFeed, activeMode, timeCursor);
    });
  }

  mapBody.querySelector(".map-filter-bar")?.addEventListener("click", (event) => {
    const chip = event.target.closest(".map-filter-chip");
    if (!chip) return;
    applyFilter(chip.dataset.filter || "all", activeMode, timeCursor);
  });

  mapBody.querySelector("#mapModeBar")?.addEventListener("click", (event) => {
    const modeBtn = event.target.closest(".map-mode-btn");
    if (!modeBtn) return;
    applyFilter(activeFeed, modeBtn.dataset.mode || "all", timeCursor);
  });

  if (timeSlider) {
    timeSlider.addEventListener("input", () => {
      const idx = Number(timeSlider.value || "0");
      const nextCursor = uniqueTimePoints[idx];
      applyFilter(activeFeed, activeMode, nextCursor);
    });
  }

  if (timePlayBtn && timeSlider) {
    timePlayBtn.addEventListener("click", () => {
      if (playTimer) {
        clearInterval(playTimer);
        playTimer = null;
        timePlayBtn.textContent = "Play";
        return;
      }
      let idx = Number(timeSlider.value || "0");
      if (idx >= uniqueTimePoints.length - 1) {
        idx = 0;
      }
      timeSlider.value = String(idx);
      applyFilter(activeFeed, activeMode, uniqueTimePoints[idx]);
      timePlayBtn.textContent = "Pause";
      playTimer = setInterval(() => {
        idx += 1;
        if (idx >= uniqueTimePoints.length) {
          clearInterval(playTimer);
          playTimer = null;
          timePlayBtn.textContent = "Play";
          return;
        }
        timeSlider.value = String(idx);
        applyFilter(activeFeed, activeMode, uniqueTimePoints[idx]);
      }, 850);
    });
  }

  mapBody.querySelector("#mapInteractiveList")?.addEventListener("click", (event) => {
    const row = event.target.closest(".map-list-row");
    if (!row) return;
    selectedIdx = Number(row.dataset.idx);
    applyFilter(activeFeed, activeMode, timeCursor);
  });

  applyFilter("all", "all", timeCursor);
  if (countChip) countChip.textContent = `${points.length} observations`;
}

function renderCorrelationMatrix(events) {
  const body = document.getElementById("matrixBody");
  if (!body) return;

  const ev = Array.isArray(events) ? events : [];
  const axes = ["air", "sea", "cyber", "multi", "unknown"];
  const sevLevels = ["high", "medium", "low", "unknown"];
  const matrix = {};
  axes.forEach((axis) => {
    matrix[axis] = {};
    sevLevels.forEach((sev) => {
      matrix[axis][sev] = 0;
    });
  });

  ev.forEach((item) => {
    const axis = String(item?.axis || "unknown").toLowerCase();
    const severity = String(item?.severity || "unknown").toLowerCase();
    if (!matrix[axis]) matrix[axis] = {};
    if (!matrix[axis][severity]) matrix[axis][severity] = 0;
    matrix[axis][severity] += 1;
  });

  const hasData = axes.some((axis) => sevLevels.some((sev) => matrix[axis][sev] > 0));
  if (!hasData) {
    body.innerHTML = `<div class=\"empty-state\"><p>No event data for matrix.</p></div>`;
    return;
  }

  const total = ev.length;
  let rows = "";
  const colMax = Math.max(
    ...sevLevels.map((sev) =>
      Math.max(
        ...axes.map((axis) => Number(matrix[axis][sev] || 0))
      )
    ),
    1
  );

  axes.forEach((axis) => {
    const cells = sevLevels.map((sev) => {
      const count = Number(matrix[axis][sev] || 0);
      const pct = Math.round((count / colMax) * 100);
      const sevClass = `matrix-cell--${escapeHtml(sev)}`;
      const heat = (pct / 100) * 0.78 + 0.18;
      return `<td class="matrix-cell ${sevClass}" style="--heat:${heat.toFixed(2)}"><span class="matrix-count">${count}</span></td>`;
    }).join("");
    rows += `<tr><th>${escapeHtml(axis)}</th>${cells}</tr>`;
  });

  body.innerHTML = `
    <div class="matrix-title">Axis × Severity (count heat-map)</div>
    <div class="matrix-wrap">
      <table class="matrix-table">
        <thead>
          <tr>
            <th></th>
            <th>High</th>
            <th>Medium</th>
            <th>Low</th>
            <th>Unknown</th>
          </tr>
        </thead>
        <tbody>${rows}</tbody>
      </table>
    </div>
    <div class="matrix-total">Total events: ${total}</div>
  `;
}

async function captureVisualCard(cardId, suffix, options = {}) {
  const card = document.getElementById(cardId);
  if (!card || !window.html2canvas) {
    return null;
  }

  const date = new Date().toISOString().replace(/[:.]/g, "-");
  const canvas = await window.html2canvas(card, {
    backgroundColor: "#040a14",
    scale: options.scale || 2.5,
    useCORS: true,
  });
  const fileName = `godeye-${suffix}-${date}.png`;
  const blob = await toBlob(canvas);
  return { fileName, blob };
}

function downloadBlob(blob, fileName) {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = fileName;
  link.click();
  URL.revokeObjectURL(url);
}

async function exportVisuals() {
  const exportBtn = document.getElementById("exportVisualsBtn");
  if (exportBtn) {
    exportBtn.disabled = true;
    exportBtn.textContent = "Exporting…";
  }
  try {
    if (!window.html2canvas) {
      throw new Error("html2canvas not available. Serve via HTTPS or allow CDN script.");
    }
    const captures = await Promise.all([
      captureVisualCard("analyticsCard", "analytics"),
      captureVisualCard("mapCard", "map"),
      captureVisualCard("matrixCard", "axis-severity-matrix"),
    ]);
    captures.forEach((capture) => {
      if (!capture) return;
      downloadBlob(capture.blob, capture.fileName);
    });
    showToast("Export complete: 3 PNG files downloaded.", "success");
  } catch (error) {
    showToast(error.message || "Export failed.", "error");
  } finally {
    if (exportBtn) {
      exportBtn.disabled = false;
      exportBtn.textContent = "Export PNGs";
    }
  }
}

async function exportVisualPack() {
  const exportPackBtn = document.getElementById("exportPackBtn");
  if (exportPackBtn) {
    exportPackBtn.disabled = true;
    exportPackBtn.textContent = "Exporting…";
  }
  try {
    if (!window.html2canvas) {
      throw new Error("html2canvas not available. Serve via HTTPS or allow CDN script.");
    }
    if (!window.JSZip) {
      throw new Error("JSZip not available. Check network/CDN access.");
    }

    const zip = new window.JSZip();
    const date = new Date().toISOString().replace(/[:.]/g, "-");
    const cards = [
      ["analyticsCard", "godeye-analytics"],
      ["mapCard", "godeye-observation-map"],
      ["matrixCard", "godeye-axis-severity-matrix"],
      ["narrativeCard", "godeye-narrative"],
      ["summaryCard", "godeye-summary"],
      ["events-section", "godeye-events"],
    ];

    const captures = await Promise.all(
      cards.map(([cardId, fileNamePrefix]) => captureVisualCard(cardId, fileNamePrefix, { scale: 2.2 })),
    );
    captures.forEach((capture, idx) => {
      if (!capture) return;
      const [_, fileNamePrefix] = cards[idx];
      zip.file(`${fileNamePrefix}-${date}.png`, capture.blob);
    });
    if (Object.keys(lastReplayPayload).length) {
      zip.file(
        "godeye-replay-result.json",
        JSON.stringify(lastReplayPayload, null, 2),
      );
      zip.file(
        "godeye-replay-brief.md",
        buildReplayBriefMarkdown(lastReplayPayload),
      );
    }
    const blob = await zip.generateAsync({ type: "blob" });
    downloadBlob(blob, `godeye-visuals-${date}.zip`);
    showToast("Export complete: visual pack generated.", "success");
  } catch (error) {
    showToast(error.message || "Export failed.", "error");
  } finally {
    if (exportPackBtn) {
      exportPackBtn.disabled = false;
      exportPackBtn.textContent = "Export Pack";
    }
  }
}

async function exportNarrativeAndSummary() {
  const narrativeExportBtn = document.getElementById("exportNarrativeBundleBtn");
  if (narrativeExportBtn) {
    narrativeExportBtn.disabled = true;
    narrativeExportBtn.textContent = "Exporting…";
  }
  try {
    const capture = await captureVisualCard("narrativeCard", "narrative-card", { scale: 2.2 });
    if (capture) {
      downloadBlob(capture.blob, capture.fileName);
      showToast("Narrative image downloaded.", "success");
    } else {
      throw new Error("Narrative card unavailable for capture.");
    }
  } catch (error) {
    showToast(error.message || "Export failed.", "error");
  } finally {
    if (narrativeExportBtn) {
      narrativeExportBtn.disabled = false;
      narrativeExportBtn.textContent = "Export Narrative";
    }
  }
}

function buildReplayBriefMarkdown(meta = {}) {
  const req = meta.request || {};
  const resp = meta.response || {};
  const runtime = resp.runtime_metrics || {};
  const diff = meta.diffSummary || {};
  const lines = [
    "# GodEye Replay Brief",
    "",
    `- Generated: ${meta.generated_at || new Date().toISOString()}`,
    `- Scenario: ${req.scenario || "—"}`,
    `- Window: ${req.from_time || "—"} -> ${req.to_time || "—"}`,
    `- Region: ${req.region || "—"}`,
    `- Query: ${req.query || "—"}`,
    "",
    "## Outcome",
    `- Events: ${Array.isArray(resp.events) ? resp.events.length : 0}`,
    `- Baseline Events: ${Array.isArray(resp.baseline_events) ? resp.baseline_events.length : 0}`,
    `- Narrative Status: ${resp.narrative_status || "unknown"}`,
    `- Summary Status: ${resp.summary_status || "unknown"}`,
    `- LLM Model: ${resp.llm_model_used || "n/a"}`,
    "",
    "## Runtime",
    `- Total: ${runtime.total_ms || 0} ms`,
    `- Reconstruct: ${runtime.total_reconstruct_ms || 0} ms`,
    `- Narrate: ${runtime.narrate_ms || 0} ms`,
    "",
    "## Structured vs Baseline",
    `- New: ${diff.newCount || 0}`,
    `- Escalated: ${diff.escalatedCount || 0}`,
    `- Resolved: ${diff.resolvedCount || 0}`,
  ];
  return lines.join("\n");
}

function computeDiffSummary(events, prevEvents) {
  const cur = Array.isArray(events) ? events : [];
  const prev = Array.isArray(prevEvents) ? prevEvents : [];
  const key = (ev) => `${String(ev.type || "unknown")}|${String(ev.axis || "unknown")}`;
  const curCounts = {};
  const prevCounts = {};
  cur.forEach((ev) => { curCounts[key(ev)] = (curCounts[key(ev)] || 0) + 1; });
  prev.forEach((ev) => { prevCounts[key(ev)] = (prevCounts[key(ev)] || 0) + 1; });

  let newCount = 0;
  let escalatedCount = 0;
  let resolvedCount = 0;
  Object.keys(curCounts).forEach((k) => {
    if (!(k in prevCounts)) newCount += curCounts[k];
    if ((curCounts[k] || 0) > (prevCounts[k] || 0)) escalatedCount += curCounts[k] - (prevCounts[k] || 0);
  });
  Object.keys(prevCounts).forEach((k) => {
    if (!(k in curCounts)) resolvedCount += prevCounts[k];
    if ((prevCounts[k] || 0) > (curCounts[k] || 0)) resolvedCount += prevCounts[k] - (curCounts[k] || 0);
  });
  return { newCount, escalatedCount, resolvedCount };
}

function buildEntitySummary(events) {
  const rows = {};
  (Array.isArray(events) ? events : []).forEach((ev) => {
    (Array.isArray(ev.entities) ? ev.entities : []).forEach((ent) => {
      const name = String(ent?.name || ent?.id || "unknown");
      rows[name] = (rows[name] || 0) + 1;
    });
  });
  return Object.entries(rows).sort((a, b) => b[1] - a[1]).slice(0, 6);
}

function renderAnalytics(events, prevEvents, metrics, replayMeta = {}) {
  const body = document.getElementById("analyticsBody");
  if (!body) return;

  const ev = Array.isArray(events) ? events : [];
  const prev = Array.isArray(prevEvents) ? prevEvents : [];
  const safeMetrics = metrics && typeof metrics === "object" ? metrics : {};

  const axisDist = safeMetrics.axis_distribution || {};
  const severityDist = safeMetrics.severity_distribution || {};
  const conf = safeMetrics.confidence || {};
  const retrieval = safeMetrics.retrieval_counts || {};
  const buckets = Array.isArray(safeMetrics.timeline_buckets) ? safeMetrics.timeline_buckets : [];
  const runtime = replayMeta.runtime_metrics || {};
  const traceUrl = replayMeta.trace_url || "";
  const diff = computeDiffSummary(ev, prev);
  const entityTop = buildEntitySummary(ev);
  const anomalyFeeds = new Set(ev.filter((e) => String(e.type || "").toLowerCase() === "anomaly").flatMap((e) => Array.isArray(e.source_tags) ? e.source_tags : []).map((v) => String(v).toLowerCase()).filter((x) => x && !x.startsWith("auto-")));
  const highFeeds = new Set(ev.filter((e) => String(e.severity || "").toLowerCase() === "high").flatMap((e) => Array.isArray(e.source_tags) ? e.source_tags : []).map((v) => String(v).toLowerCase()).filter((x) => x && !x.startsWith("auto-")));
  const dataQuality = {
    missingCoords: Number(replayMeta.observations_missing_position || 0),
    observationCount: Number(replayMeta.observations_count || 0),
  };

  const highCount = ev.filter((e) => String(e.severity || "").toLowerCase() === "high").length;
  const correlationCount = ev.filter((e) => String(e.type || "").toLowerCase() === "correlation").length;
  const confidenceAvg = typeof conf.avg === "number" ? conf.avg : 0;
  const confMin = typeof conf.min === "number" ? conf.min : 0;
  const confMax = typeof conf.max === "number" ? conf.max : 0;

  const axisHtml = `<div class="analytics-kpi">
    <div class="analytics-kpi__label">Axis Distribution</div>
    <div class="mini-bars mini-bars__axis" id="axisBars"></div>
  </div>`;
  const confHtml = `<div class="analytics-kpi">
    <div class="analytics-kpi__label">Confidence</div>
    <div class="analytics-kpi__value">${formatConfPct(confidenceAvg)} avg (${formatConfPct(confMax)} max)</div>
    <div class="analytics-kpi__sub">Range: ${formatConfPct(confMin)}–${formatConfPct(confMax)} · Events: ${ev.length} · High: ${highCount}</div>
  </div>`;
  const severityHtml = `<div class="analytics-kpi">
    <div class="analytics-kpi__label">Severity Distribution</div>
    <div class="mini-bars mini-bars__severity" id="severityBars"></div>
  </div>`;
  const bucketHtml = `<div class="analytics-kpi">
    <div class="analytics-kpi__label">10-minute Timeline</div>
    <div class="mini-bars mini-bars__timeline" id="timelineBars"></div>
  </div>`;
  const retrievalHtml = `<div class="analytics-kpi">
    <div class="analytics-kpi__label">Retrieval</div>
    <div class="analytics-kpi__value">${retrieval.query_rag || 0} + ${retrieval.entity_graph_rag || 0} docs</div>
    <div class="analytics-kpi__sub">Baseline docs: ${retrieval.baseline_docs || 0} · Correlation events: ${correlationCount} · Prev-window events: ${prev.length}</div>
  </div>`;
  const scorecardHtml = `<div class="analytics-kpi">
    <div class="analytics-kpi__label">Structured vs Baseline</div>
    <div class="analytics-kpi__sub">New: ${diff.newCount} · Escalated: ${diff.escalatedCount} · Resolved: ${diff.resolvedCount}</div>
    <div class="analytics-kpi__sub">Coverage Lift: ${prev.length ? Math.max(0, Math.round(((ev.length - prev.length) / prev.length) * 100)) : ev.length ? 100 : 0}%</div>
  </div>`;
  const alertsHtml = `<div class="analytics-kpi">
    <div class="analytics-kpi__label">Alert Rules</div>
    <div class="analytics-kpi__sub">${highCount > 0 ? `High severity triggers: ${highCount}` : "No high severity trigger."}</div>
    <div class="analytics-kpi__sub">${correlationCount > 0 ? `Correlation trigger: ${correlationCount}` : "No multi-feed correlation trigger."}</div>
  </div>`;
  const runtimeHtml = `<div class="analytics-kpi">
    <div class="analytics-kpi__label">Latency Budget</div>
    <div class="analytics-kpi__sub">Total: ${runtime.total_ms || 0}ms · Reconstruct: ${runtime.total_reconstruct_ms || 0}ms · Narrate: ${runtime.narrate_ms || 0}ms</div>
    <div class="analytics-kpi__sub">Model: ${escapeHtml(replayMeta.llm_model_used || "n/a")}${traceUrl ? ` · <a href="${escapeHtml(traceUrl)}" target="_blank" rel="noopener noreferrer">LangSmith Trace</a>` : ""}</div>
  </div>`;
  const qualityHtml = `<div class="analytics-kpi">
    <div class="analytics-kpi__label">Data Quality</div>
    <div class="analytics-kpi__sub">Observations: ${dataQuality.observationCount} · Missing coords: ${dataQuality.missingCoords}</div>
    <div class="analytics-kpi__sub">${dataQuality.missingCoords > 0 ? "Recommendation: provide lat/lon for all feeds." : "Coordinate coverage is healthy."}</div>
  </div>`;
  const entityHtml = `<div class="analytics-kpi">
    <div class="analytics-kpi__label">Entity Focus</div>
    <div class="analytics-kpi__sub">${entityTop.length ? entityTop.map(([name, count]) => `${escapeHtml(name)} (${count})`).join(" · ") : "No entity links available in this window."}</div>
  </div>`;
  const graphHtml = `<div class="analytics-kpi">
    <div class="analytics-kpi__label">Graph Snapshot</div>
    <div class="analytics-kpi__sub">Feeds anomaly-linked: ${anomalyFeeds.size || 0} · high-linked: ${highFeeds.size || 0}</div>
    <div class="analytics-kpi__sub">Nodes: events(${ev.length}) -> feeds(${new Set(ev.flatMap((e) => Array.isArray(e.source_tags) ? e.source_tags : [])).size})</div>
  </div>`;

  body.innerHTML = `
    <div class="analytics-grid">
      ${axisHtml}
      ${confHtml}
    </div>
    <div class="analytics-grid">
      ${severityHtml}
      ${retrievalHtml}
    </div>
    <div class="analytics-grid">
      ${scorecardHtml}
      ${alertsHtml}
    </div>
    <div class="analytics-grid">
      ${runtimeHtml}
      ${qualityHtml}
    </div>
    <div class="analytics-grid">
      ${entityHtml}
      ${graphHtml}
    </div>
    ${bucketHtml}
  `;

  renderMiniBars("axisBars", axisDist, ["air", "sea", "cyber", "multi", "unknown"]);
  renderMiniBars("severityBars", severityDist, ["high", "medium", "low", "unknown"]);
  renderTimelineBuckets("timelineBars", buckets);
}

function severityBadge(s) {
  const val = String(s ?? "").toLowerCase();
  return `<span class="badge badge--severity-${escapeHtml(val)}">${escapeHtml(val || "—")}</span>`;
}

function axisBadge(a) {
  const val = String(a ?? "").toLowerCase();
  return `<span class="badge badge--axis-${escapeHtml(val)}">${escapeHtml(val || "—")}</span>`;
}

// ── Main replay function ────────────────────────────────────────
let controller = null;

async function runReplay() {
  const fromTime = document.getElementById("fromTime").value.trim();
  const toTime   = document.getElementById("toTime").value.trim();
  const region   = document.getElementById("region").value.trim();
  const scenario = document.getElementById("scenario").value.trim();
  const query    = document.getElementById("query").value.trim();

  if (!fromTime || !toTime) {
    showToast("From and To times are required.", "error");
    return;
  }

  // Update sidebar meta
  document.getElementById("sidebarScenario").textContent = scenario || "—";
  document.getElementById("sidebarRegion").textContent   = region   || "—";
  document.getElementById("sidebarEventCount").textContent = "…";

  // Button state
  const btn = document.getElementById("runBtn");
  btn.disabled = true;
  btn.querySelector(".btn__text").textContent = "Running…";
  btn.querySelector(".btn__spinner").hidden = false;

  setStatus("running");

  // Show skeletons
  showSkeletons("narrativeBody", 6);
  showSkeletons("summaryBody",   4);
  showSkeletons("analyticsBody", 10);
  document.getElementById("matrixBody").innerHTML = `<div class="empty-state"><p>Building matrix…</p></div>`;
  document.getElementById("mapBody").innerHTML = `<div class="empty-state"><p>Loading observations map…</p></div>`;
  document.getElementById("observationCount").textContent = "…";
  document.getElementById("eventsBody").innerHTML = "";
  document.getElementById("tableEmpty").style.display = "none";
  document.getElementById("eventsTable").style.display = "none";
  document.getElementById("eventCount").textContent = "";
  document.getElementById("narrativeBadge").textContent = "";
  document.getElementById("narrativeBadge").classList.remove("card-badge--warning", "card-badge--muted");
  document.getElementById("narrativeSource").hidden = true;
  document.getElementById("narrativeSource").textContent = "";
  document.getElementById("narrativeSource").classList.remove("card-badge--warning", "card-badge--muted");
  document.getElementById("narrativeCopyBtn").hidden = true;
  lastNarrativeText = "";
  lastReplayPayload = {};
  renderSeverityDist([]);  // clear stale distribution bar from previous run
  setDiagnosticDefaults();

  // Abort previous if still running
  if (controller) controller.abort();
  controller = new AbortController();

  try {
    const headers = { "Content-Type": "application/json" };
    if (API_KEY) {
      headers["X-API-Key"] = API_KEY;
    }
    const payload = { mode: "replay", from_time: fromTime, to_time: toTime, region, scenario, query };

    const apiBase = await resolveApiBase(controller.signal, headers);
    document.getElementById("diagApiBase").textContent = apiBase;
    const data = await tryReplay(apiBase, payload, headers, controller.signal);
    let observations = [];
    let observationsError = "";
    try {
      observations = await fetchObservations(apiBase, fromTime, toTime, headers, controller.signal);
    } catch (error) {
      observationsError = error?.message || "Unknown observation fetch error.";
      console.warn("[godeye] observations fetch failed", error);
    }

    // keep it for next run on same host
    localStorage.setItem("GODEYE_API_BASE", apiBase);
    if (!data) {
      throw new Error("Failed to fetch: check API URL and server/CORS settings.");
    }

    const replayEvents = Array.isArray(data.events) ? data.events : [];
    const prevEvents = Array.isArray(data.baseline_events) ? data.baseline_events : [];
    const narrativeText = typeof data.narrative === "string" && data.narrative.trim() ? data.narrative : "";
    const summaryText = typeof data.event_summary === "string" && data.event_summary.trim() ? data.event_summary : "";
    const narrativeStatus = data.narrative_status || (narrativeText ? "ok" : "missing_data");
    const summaryStatus = data.summary_status || (summaryText ? "ok" : "missing_data");
    const metrics = (data.timeline_metrics && typeof data.timeline_metrics === "object") ? data.timeline_metrics : buildTimelineFallbackMetrics(replayEvents, prevEvents);
    const narrativeReason = extractNarrativeUnavailableReason(data) || (narrativeStatus === "missing_data" ? "No narrative returned. Check logs and source data." : "");
    const summaryReason = data.summary_status_reason || (summaryStatus === "missing_data" ? "No summary returned. Check logs and source data." : "");
    const obsMissingPosition = Array.isArray(observations)
      ? observations.filter((obs) => !extractObservationPosition(obs)).length
      : 0;

    // persist the result for export bundle and debugging
    lastReplayPayload = {
      request: {
        from_time: fromTime,
        to_time: toTime,
        region,
        scenario,
        query,
      },
      response: data,
      observations_count: Array.isArray(observations) ? observations.length : 0,
      observations_missing_position: obsMissingPosition,
      diffSummary: computeDiffSummary(replayEvents, prevEvents),
      generated_at: new Date().toISOString(),
    };
    lastReplayMeta = data;

    // ── Narrative
    const narrativeBody = document.getElementById("narrativeBody");
    const narrativeBadge = document.getElementById("narrativeBadge");
    const narrativeSource = document.getElementById("narrativeSource");
    const meta = narrativeStatusMeta(narrativeStatus, narrativeReason || "");

    narrativeBadge.classList.remove("card-badge--warning", "card-badge--muted");
    narrativeBadge.textContent = meta.title;
    if (meta.badgeClass) {
      narrativeBadge.classList.add(meta.badgeClass);
      narrativeSource.textContent = narrativeStatus;
      narrativeSource.hidden = false;
      narrativeSource.classList.remove("card-badge--warning", "card-badge--muted");
      narrativeSource.classList.add(meta.badgeClass);
    } else {
      narrativeSource.hidden = true;
    }
    lastNarrativeText = narrativeText || narrativeReason;
    narrativeBody.innerHTML = renderNarrativePanel(
      lastNarrativeText,
      narrativeStatus,
      narrativeReason,
      query,
      replayEvents.length,
      prevEvents.length,
    );
    document.getElementById("narrativeCopyBtn").hidden = !(Boolean(lastNarrativeText));
    const observationStatus = observationsError
      ? `Failed (${observationsError})`
      : `ok (${Array.isArray(observations) ? observations.length : 0})`;

    // ── Summary
    const summaryBody = document.getElementById("summaryBody");
    if (summaryText) {
      summaryBody.innerHTML = `<div class="narrative-text">${renderMarkdown(summaryText)}</div>`;
      if (summaryStatus !== "ok" && summaryReason) {
        summaryBody.innerHTML += `<div class="narrative-banner narrative-banner--warning" style=\"margin-top:.6rem\">Summary status: ${escapeHtml(summaryStatus)}. ${escapeHtml(summaryReason)}</div>`;
      }
    } else {
      summaryBody.innerHTML = `<div class="empty-state"><p>No summary available.</p><p class="narrative-banner narrative-banner--warning" style="margin-top:.5rem">${escapeHtml(summaryReason || "Summary unavailable.")}</p></div>`;
    }

    // ── Events table
    const events = replayEvents;
    renderAnalytics(events, prevEvents, metrics, {
      ...data,
      observations_count: Array.isArray(observations) ? observations.length : 0,
      observations_missing_position: obsMissingPosition,
    });
    renderCorrelationMatrix(events);
    renderObservationMap(observations, events, {
      error: observationsError,
      fromTime,
      toTime,
    });
    const tbody  = document.getElementById("eventsBody");
    const table  = document.getElementById("eventsTable");
    const empty  = document.getElementById("tableEmpty");

    document.getElementById("sidebarEventCount").textContent = events.length;
    document.getElementById("eventCount").textContent = `${events.length} event${events.length !== 1 ? "s" : ""}`;
    updateDiagnostics({
      apiBase,
      narrativeStatus: narrativeStatus,
      summaryStatus: summaryStatus,
      observationsStatus: `${observationStatus} | model=${data.llm_model_used || "n/a"} | total_ms=${(data.runtime_metrics && data.runtime_metrics.total_ms) || 0}`,
      eventsCount: events.length,
      prevEventsCount: prevEvents.length,
      lastRun: new Date().toLocaleTimeString(),
    });

    lastEvents = events;

    if (events.length === 0) {
      empty.style.display = "flex";
      table.style.display = "none";
      renderSeverityDist([]);
    } else {
      empty.style.display = "none";
      table.style.display = "table";
      tbody.innerHTML = events.map((ev, i) => {
        const id    = escapeHtml(ev.id ?? "");
        const type  = escapeHtml(ev.type ?? "—");
        const start = escapeHtml(String(ev.start_time ?? "—"));
        const tags  = (ev.source_tags ?? []).map(t => `<span class="tag">${escapeHtml(t)}</span>`).join("");

        // Confidence sparkbar
        const confPct = ev.confidence != null ? Math.round(Number(ev.confidence) * 100) : null;
        const confCls = confPct != null && confPct < 40 ? " conf--low" : "";
        const confHtml = confPct != null
          ? `<div class="conf-bar"><div class="conf-bar__track"><div class="conf-bar__fill" style="width:${confPct}%"></div></div><span class="conf-num">${(confPct / 100).toFixed(2)}</span></div>`
          : "—";

        return `<tr class="expandable" data-i="${i}">
          <td class="id-cell">${id}</td>
          <td>${type}</td>
          <td>${axisBadge(ev.axis)}</td>
          <td>${severityBadge(ev.severity)}</td>
          <td class="conf-cell${confCls}">${confHtml}</td>
          <td class="id-cell">${start}</td>
          <td><div class="tags-cell">${tags || "—"}</div></td>
        </tr>`;
      }).join("");
      renderSeverityDist(events);
    }

    setStatus("success");
    showToast(`Replay complete — ${events.length} event${events.length !== 1 ? "s" : ""} found.`, "success");

  } catch (err) {
    if (err.name === "AbortError") return;

    const errHtml = `
      <div class="error-state">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>
        <span>${escapeHtml(err.message)}</span>
      </div>`;

    document.getElementById("narrativeBody").innerHTML = errHtml;
    document.getElementById("summaryBody").innerHTML = `<div class="empty-state"><p>No data.</p></div>`;
    document.getElementById("analyticsBody").innerHTML = `<div class="empty-state"><p>No analytics.</p></div>`;
    document.getElementById("matrixBody").innerHTML = `<div class="empty-state"><p>No matrix.</p></div>`;
    document.getElementById("mapBody").innerHTML = `<div class="empty-state"><p>No map.</p></div>`;
    document.getElementById("observationCount").textContent = "—";
    document.getElementById("sidebarEventCount").textContent = "—";
    if (err.message) {
      updateDiagnostics({
        apiBase: document.getElementById("diagApiBase").textContent || "Not resolved",
        narrativeStatus: "error",
        summaryStatus: "error",
        observationsStatus: "error",
        eventsCount: 0,
        prevEventsCount: 0,
        lastRun: new Date().toLocaleTimeString(),
      });
    }
    setStatus("error");
    showToast(err.message, "error");
  } finally {
    btn.disabled = false;
    btn.querySelector(".btn__text").textContent = "Run Replay";
    btn.querySelector(".btn__spinner").hidden = true;
    controller = null;
  }
}

// ── Event listeners ─────────────────────────────────────────────
document.getElementById("runBtn").addEventListener("click", runReplay);
document.getElementById("exportVisualsBtn").addEventListener("click", exportVisuals);
document.getElementById("exportPackBtn").addEventListener("click", exportVisualPack);
document.getElementById("exportNarrativeBundleBtn").addEventListener("click", exportNarrativeAndSummary);

document.getElementById("query").addEventListener("keydown", e => {
  if (e.key === "Enter") runReplay();
});

// Global Ctrl+Enter / Cmd+Enter shortcut to run replay from anywhere on the page
document.addEventListener("keydown", e => {
  if ((e.ctrlKey || e.metaKey) && e.key === "Enter") runReplay();
});
setDiagnosticDefaults();
initDashboardControls();

// ── Copy narrative to clipboard ──────────────────────────────────
const COPY_ICON = `<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>`;
const CHECK_ICON = `<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="20 6 9 17 4 12"/></svg>`;

document.getElementById("narrativeCopyBtn").addEventListener("click", async () => {
  if (!lastNarrativeText) return;
  const btn = document.getElementById("narrativeCopyBtn");
  try {
    await navigator.clipboard.writeText(lastNarrativeText);
    btn.classList.add("copied");
    btn.innerHTML = CHECK_ICON;
    setTimeout(() => {
      btn.classList.remove("copied");
      btn.innerHTML = COPY_ICON;
    }, 2000);
  } catch {
    showToast("Copy failed — use Ctrl+C.", "error");
  }
});

// ── Expandable event rows (delegated) ───────────────────────────
document.getElementById("eventsBody").addEventListener("click", e => {
  const row = e.target.closest("tr.expandable");
  if (!row) return;

  // Toggle collapse if already open
  const next = row.nextElementSibling;
  if (next && next.classList.contains("row-detail")) {
    next.remove();
    row.classList.remove("expanded");
    return;
  }

  // Close any other open row first
  const openDetail = document.querySelector(".row-detail");
  if (openDetail) {
    openDetail.previousElementSibling?.classList.remove("expanded");
    openDetail.remove();
  }

  const ev = lastEvents[Number(row.dataset.i)];
  if (!ev) return;

  row.classList.add("expanded");

  const regionStr = ev.region ?? (ev.details?.region ?? ev.details?.region_name ?? "—");
  const detailsStr = ev.details && Object.keys(ev.details).length
    ? JSON.stringify(ev.details, null, 0)
    : "—";
  const provenanceStr = [
    `source_tags=${Array.isArray(ev.source_tags) ? ev.source_tags.join(", ") : "—"}`,
    `confidence=${ev.confidence ?? "—"}`,
    `entities=${Array.isArray(ev.entities) ? ev.entities.length : 0}`,
  ].join(" | ");

  const detail = document.createElement("tr");
  detail.className = "row-detail";
  detail.innerHTML = `<td colspan="7"><div class="row-detail__content">
    <div class="row-detail__field">
      <span class="row-detail__key">Scenario</span>
      <span class="row-detail__val">${escapeHtml(ev.scenario ?? "—")}</span>
    </div>
    <div class="row-detail__field">
      <span class="row-detail__key">Region</span>
      <span class="row-detail__val">${escapeHtml(regionStr)}</span>
    </div>
    <div class="row-detail__field">
      <span class="row-detail__key">End Time</span>
      <span class="row-detail__val">${escapeHtml(String(ev.end_time ?? "—"))}</span>
    </div>
    <div class="row-detail__field">
      <span class="row-detail__key">Summary</span>
      <span class="row-detail__val">${escapeHtml(ev.summary ?? "—")}</span>
    </div>
    <div class="row-detail__field">
      <span class="row-detail__key">Details</span>
      <span class="row-detail__val">${escapeHtml(detailsStr)}</span>
    </div>
    <div class="row-detail__field">
      <span class="row-detail__key">Provenance</span>
      <span class="row-detail__val">${escapeHtml(provenanceStr)}</span>
    </div>
  </div></td>`;
  row.after(detail);
});
