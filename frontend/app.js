// ── Config ──────────────────────────────────────────────────────
const API_BASE = "http://localhost:8001";

// Module-level event store so the delegated row-expand handler can access raw data.
let lastEvents = [];
// Raw narrative text preserved for copy-to-clipboard.
let lastNarrativeText = "";

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
  document.getElementById("eventsBody").innerHTML = "";
  document.getElementById("tableEmpty").style.display = "none";
  document.getElementById("eventsTable").style.display = "none";
  document.getElementById("eventCount").textContent = "";
  document.getElementById("narrativeBadge").textContent = "";
  document.getElementById("narrativeCopyBtn").hidden = true;
  lastNarrativeText = "";
  renderSeverityDist([]);  // clear stale distribution bar from previous run

  // Abort previous if still running
  if (controller) controller.abort();
  controller = new AbortController();

  try {
    const res = await fetch(`${API_BASE}/api/replay`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mode: "replay", from_time: fromTime, to_time: toTime, region, scenario, query }),
      signal: controller.signal,
    });

    if (!res.ok) {
      const text = await res.text().catch(() => `HTTP ${res.status}`);
      throw new Error(`HTTP ${res.status}: ${text}`);
    }

    const data = await res.json();

    // ── Narrative
    const narrativeBody = document.getElementById("narrativeBody");
    if (data.narrative) {
      lastNarrativeText = data.narrative;
      narrativeBody.innerHTML = `<div class="narrative-text">${renderMarkdown(data.narrative)}</div>`;
      document.getElementById("narrativeBadge").textContent = "AI Analysis";
      document.getElementById("narrativeCopyBtn").hidden = false;
    } else {
      lastNarrativeText = "";
      narrativeBody.innerHTML = `<div class="empty-state"><p>No narrative returned.</p></div>`;
      document.getElementById("narrativeCopyBtn").hidden = true;
    }

    // ── Summary
    const summaryBody = document.getElementById("summaryBody");
    if (data.event_summary) {
      summaryBody.innerHTML = `<div class="narrative-text">${renderMarkdown(data.event_summary)}</div>`;
    } else {
      summaryBody.innerHTML = `<div class="empty-state"><p>No summary available.</p></div>`;
    }

    // ── Events table
    const events = data.events ?? [];
    const tbody  = document.getElementById("eventsBody");
    const table  = document.getElementById("eventsTable");
    const empty  = document.getElementById("tableEmpty");

    document.getElementById("sidebarEventCount").textContent = events.length;
    document.getElementById("eventCount").textContent = `${events.length} event${events.length !== 1 ? "s" : ""}`;

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
    document.getElementById("sidebarEventCount").textContent = "—";
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

document.getElementById("query").addEventListener("keydown", e => {
  if (e.key === "Enter") runReplay();
});

// Global Ctrl+Enter / Cmd+Enter shortcut to run replay from anywhere on the page
document.addEventListener("keydown", e => {
  if ((e.ctrlKey || e.metaKey) && e.key === "Enter") runReplay();
});

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

  const regionStr = ev.region ? JSON.stringify(ev.region) : "—";
  const detailsStr = ev.details && Object.keys(ev.details).length
    ? JSON.stringify(ev.details, null, 0)
    : "—";

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
  </div></td>`;
  row.after(detail);
});
