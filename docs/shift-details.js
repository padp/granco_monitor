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

function fmtPph(value) {
  return value === null || value === undefined ? "-" : `${value}/hr`;
}

// Unlike fmtTimeWindow (used by the single-shift production table above,
// where the date is implied by the picked shift), part-session results
// can span any date range, so the date has to be shown too.
function fmtDateTimeWindow(startIso, endIso) {
  if (!startIso) return "-";
  const start = new Date(startIso).toLocaleString();
  if (!endIso || endIso === startIso) return start;
  return `${start} - ${new Date(endIso).toLocaleString()}`;
}

async function searchPartSessions() {
  const params = new URLSearchParams();
  const partNumber = document.getElementById("pts-part-number").value.trim();
  const dateFrom = document.getElementById("pts-date-from").value;
  const dateTo = document.getElementById("pts-date-to").value;
  const shift = document.getElementById("pts-shift").value;
  if (partNumber) params.set("part_number", partNumber);
  if (dateFrom) params.set("date_from", dateFrom);
  if (dateTo) {
    // date_to is compared as an exclusive upper bound server-side (same
    // [start, end) convention as everything else in this app) - bump it
    // to the next day so picking "Date To" actually includes that whole day.
    const exclusiveEnd = new Date(`${dateTo}T00:00:00`);
    exclusiveEnd.setDate(exclusiveEnd.getDate() + 1);
    params.set("date_to", exclusiveEnd.toISOString().slice(0, 10));
  }
  if (shift) params.set("shift", shift);

  const res = await fetch(`${API_BASE}/api/part-sessions?${params.toString()}`);
  const data = await res.json();

  const allTime = data.all_time || {};
  document.getElementById("pts-all-time").textContent =
    `All-time: PLC ${fmtNum(allTime.plc_pieces)} pieces (${fmtPph(allTime.plc_pieces_per_hour)}) - ` +
    `Plex ${fmtNum(allTime.plex_pieces)} pieces (${fmtPph(allTime.plex_pieces_per_hour)})`;

  const byShiftTbody = document.querySelector("#pts-by-shift-table tbody");
  byShiftTbody.innerHTML = "";
  for (const s of data.by_shift || []) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${s.shift}</td>
      <td>${fmtNum(s.plc_pieces)}</td>
      <td>${fmtPph(s.plc_pieces_per_hour)}</td>
      <td>${fmtNum(s.plex_pieces)}</td>
      <td>${fmtPph(s.plex_pieces_per_hour)}</td>
    `;
    byShiftTbody.appendChild(tr);
  }

  const sessionsTbody = document.querySelector("#pts-sessions-table tbody");
  sessionsTbody.innerHTML = "";
  for (const s of data.sessions || []) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${fmtDateTimeWindow(s.window_start, s.window_end)}</td>
      <td>${s.input_part ?? "-"}</td>
      <td>${s.output_part ?? "-"}</td>
      <td>${fmtNum(s.plc_pieces)}</td>
      <td>${fmtPph(s.plc_pieces_per_hour)}</td>
      <td>${fmtNum(s.plex_pieces)}</td>
      <td>${fmtPph(s.plex_pieces_per_hour)}</td>
    `;
    sessionsTbody.appendChild(tr);
  }
}

document.getElementById("pts-search").addEventListener("click", searchPartSessions);

refreshAll();
setInterval(refreshAll, POLL_INTERVAL_MS);
