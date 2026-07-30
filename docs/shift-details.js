// Fill this in after deploying api/ to Render - keep in sync with app.js.
const API_BASE = "https://granco-monitor.onrender.com/";

const POLL_INTERVAL_MS = 5000;

function fmtTs(iso) {
  if (!iso) return "-";
  return new Date(iso).toLocaleString();
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

function fmtNum(value) {
  return value === null || value === undefined ? "-" : value;
}

function fmtTimeWindow(startIso, endIso) {
  if (!startIso) return "-";
  const start = new Date(startIso).toLocaleTimeString();
  if (!endIso || endIso === startIso) return start;
  return `${start} - ${new Date(endIso).toLocaleTimeString()}`;
}

// null until the user (or the initial load) has picked a specific shift -
// while null, the API's own "current shift" default is used instead.
function pickedShiftLabel() {
  const date = document.getElementById("shift-date").value;
  const shift = document.getElementById("shift-select").value;
  return date && shift ? `${date} - ${shift}` : null;
}

async function refreshShiftSummary() {
  const picked = pickedShiftLabel();
  const url = picked
    ? `${API_BASE}/api/shift/summary?shift_label=${encodeURIComponent(picked)}`
    : `${API_BASE}/api/shift/summary`;
  const res = await fetch(url);
  const data = await res.json();

  document.getElementById("shift-label").textContent = data.shift_label || "-";

  // On first load nothing's been picked yet - sync the date/shift fields
  // to whatever the API resolved as "current" so the picker starts on the
  // shift actually being shown, instead of blank.
  if (data.shift_label && !picked) {
    const [datePart, shiftPart] = data.shift_label.split(" - ");
    document.getElementById("shift-date").value = datePart;
    document.getElementById("shift-select").value = shiftPart;
  }

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

  const clockinTbody = document.querySelector("#clockin-sessions-table tbody");
  clockinTbody.innerHTML = "";
  for (const cs of data.clockin_sessions || []) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${cs.employee_name ?? "-"}</td>
      <td>${fmtTs(cs.clockin_ts)}</td>
      <td>${cs.still_clocked_in ? "still clocked in" : fmtTs(cs.clockout_ts)}</td>
      <td>${fmtHMS(cs.duration_seconds)}</td>
    `;
    clockinTbody.appendChild(tr);
  }

  const prodTbody = document.querySelector("#production-table tbody");
  prodTbody.innerHTML = "";
  for (const p of data.production_by_part || []) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${fmtTimeWindow(p.window_start, p.window_end)}</td>
      <td>${p.input_part ?? "-"}</td>
      <td>${p.output_part ?? "-"}</td>
      <td>${fmtNum(p.plc_pieces)}</td>
      <td>${fmtNum(p.plc_cut_count)}</td>
      <td>${fmtNum(p.plex_pieces)}</td>
      <td>${fmtNum(p.plex_event_count)}</td>
    `;
    prodTbody.appendChild(tr);
  }
}

async function refreshAll() {
  try {
    await refreshShiftSummary();
    document.getElementById("last-updated").textContent =
      `updated ${new Date().toLocaleTimeString()}`;
  } catch (err) {
    document.getElementById("last-updated").textContent = `error: ${err.message}`;
  }
}

document.getElementById("shift-date").addEventListener("change", refreshAll);
document.getElementById("shift-select").addEventListener("change", refreshAll);

refreshAll();
setInterval(refreshAll, POLL_INTERVAL_MS);
