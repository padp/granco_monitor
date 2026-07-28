// Fill this in after deploying api/ to Render.
const API_BASE = "https://granco-monitor.onrender.com/";

const POLL_INTERVAL_MS = 5000;

function fmtTs(iso) {
  if (!iso) return "-";
  return new Date(iso).toLocaleString();
}

function fmtSeconds(value) {
  return value === null || value === undefined ? "-" : Number(value).toFixed(1);
}

function fmtMinutesAgo(seconds) {
  if (seconds === null || seconds === undefined) return "-";
  const mins = Math.floor(seconds / 60);
  return mins < 1 ? "under a minute ago" : `${mins}m ago`;
}

async function refreshStatus() {
  const res = await fetch(`${API_BASE}/api/status`);
  const data = await res.json();

  const state = data.state?.state || "UNKNOWN";
  const badge = document.getElementById("state-badge");
  const stalled = Boolean(data.stalled);
  badge.textContent = stalled ? "STALLED" : state;
  badge.className = `badge ${stalled ? "stalled" : state.toLowerCase()}`;

  document.getElementById("part-number").textContent =
    data.latest_cycle?.part_number || "-";
  document.getElementById("last-cycle-ts").textContent =
    `${fmtTs(data.latest_cycle?.ts)} (${fmtMinutesAgo(data.seconds_since_last_cut)})`;
}

async function refreshStaffing() {
  const res = await fetch(`${API_BASE}/api/staffing/current`);
  const data = await res.json();

  const badge = document.getElementById("staffing-badge");
  const understaffed = Boolean(data.understaffed);
  badge.textContent = `${data.count ?? 0} staffed`;
  badge.className = `badge ${understaffed ? "understaffed" : "staffed"}`;

  document.getElementById("staffing-operators").textContent =
    (data.operators || []).join(", ") || "-";
}

function fmtHMS(totalSeconds) {
  const s = Math.round(totalSeconds || 0);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  return `${h}h ${m}m`;
}

function fmtPct(categoryPct, key) {
  const value = categoryPct?.[key];
  return value === undefined ? "-" : `${value.toFixed(0)}%`;
}

async function refreshShiftSummary() {
  const res = await fetch(`${API_BASE}/api/shift/summary`);
  const data = await res.json();

  document.getElementById("shift-label").textContent = data.shift_label || "-";

  const tbody = document.querySelector("#shift-summary-table tbody");
  tbody.innerHTML = "";
  for (const op of data.operators || []) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${op.employee_name ?? "-"}</td>
      <td>${fmtHMS(op.total_seconds)}</td>
      <td>${fmtPct(op.category_pct, "production")}</td>
      <td>${fmtPct(op.category_pct, "setup")}</td>
      <td>${fmtPct(op.category_pct, "break")}</td>
      <td>${fmtPct(op.category_pct, "idle")}</td>
      <td>${fmtPct(op.category_pct, "other")}</td>
    `;
    tbody.appendChild(tr);
  }
}

async function refreshCycles() {
  const res = await fetch(`${API_BASE}/api/cycles/recent?limit=50`);
  const data = await res.json();

  const tbody = document.querySelector("#cycles-table tbody");
  tbody.innerHTML = "";
  for (const cycle of data.cycles || []) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${fmtTs(cycle.ts)}</td>
      <td>${cycle.part_number ?? "-"}</td>
      <td>${cycle.cut_number ?? "-"}</td>
      <td>${fmtSeconds(cycle.cycle_duration_s)}</td>
      <td>${fmtSeconds(cycle.theoretical_duration_s)}</td>
      <td>${fmtSeconds(cycle.cut_length)}</td>
      <td>${fmtSeconds(cycle.backgauge_position)}</td>
      <td>${cycle.parts_per_cut ?? "-"}</td>
      <td>${cycle.is_trim_cut ? "Trim" : ""}</td>
    `;
    tbody.appendChild(tr);
  }
}

async function refreshAll() {
  try {
    await Promise.all([refreshStatus(), refreshCycles(), refreshStaffing(), refreshShiftSummary()]);
    document.getElementById("last-updated").textContent =
      `updated ${new Date().toLocaleTimeString()}`;
  } catch (err) {
    document.getElementById("last-updated").textContent = `error: ${err.message}`;
  }
}

refreshAll();
setInterval(refreshAll, POLL_INTERVAL_MS);
