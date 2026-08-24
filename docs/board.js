// Fill this in after deploying api/ to Render - keep in sync with app.js.
const API_BASE = "https://granco-monitor.onrender.com/";

const POLL_INTERVAL_MS = 5000;

function fmtTs(iso) {
  if (!iso) return "-";
  return new Date(iso).toLocaleString();
}

function fmtHours(value) {
  return value === null || value === undefined || value === "" ? "-" : `${value}h`;
}

async function refreshNotes() {
  const res = await fetch(`${API_BASE}/api/notes`);
  const data = await res.json();

  const el = document.getElementById("notes-text");
  const text = (data.text || "").trim();
  el.textContent = text || "No notes posted.";
  el.classList.toggle("notes-empty", !text);

  document.getElementById("notes-meta").textContent = data.updated_ts
    ? `Last updated ${fmtTs(data.updated_ts)} by ${data.updated_by || "-"}`
    : "";
}

function renderShiftTable(tableId, doc, currentPartPrefix) {
  const tbody = document.querySelector(`#${tableId} tbody`);
  tbody.innerHTML = "";
  for (const row of doc?.rows || []) {
    const tr = document.createElement("tr");
    if (currentPartPrefix && row.part_number === currentPartPrefix) {
      tr.classList.add("current-part-row");
    }
    tr.innerHTML = `
      <td>${row.part_number || "-"}</td>
      <td>${row.job_number || "-"}</td>
      <td>${row.racks ?? "-"}</td>
      <td>${fmtHours(row.estimated_hours)}</td>
    `;
    tbody.appendChild(tr);
  }
}

async function refreshBoard() {
  const res = await fetch(`${API_BASE}/api/schedule/board`);
  const data = await res.json();

  document.getElementById("previous-shift-label").textContent =
    data.previous?.date && data.previous?.shift
      ? `Previous — ${data.previous.date} ${data.previous.shift}`
      : "Previous Shift";
  document.getElementById("current-shift-label").textContent =
    data.current?.date && data.current?.shift
      ? `Now — ${data.current.date} ${data.current.shift}`
      : "Current Shift";
  document.getElementById("next-shift-label").textContent =
    data.next?.date && data.next?.shift
      ? `Next — ${data.next.date} ${data.next.shift}`
      : "Next Shift";

  renderShiftTable("previous-shift-table", data.previous, null);
  renderShiftTable("current-shift-table", data.current, data.current_part_prefix);
  renderShiftTable("next-shift-table", data.next, null);
}

async function refreshAll() {
  try {
    await Promise.all([refreshNotes(), refreshBoard()]);
    document.getElementById("last-updated").textContent =
      `updated ${new Date().toLocaleTimeString()}`;
  } catch (err) {
    document.getElementById("last-updated").textContent = `error: ${err.message}`;
  }
}

function init() {
  refreshAll();
  setInterval(refreshAll, POLL_INTERVAL_MS);
}

init();
