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

async function refreshAll() {
  try {
    await refreshShiftSummary();
    document.getElementById("last-updated").textContent =
      `updated ${new Date().toLocaleTimeString()}`;
  } catch (err) {
    document.getElementById("last-updated").textContent = `error: ${err.message}`;
  }
}

refreshAll();
setInterval(refreshAll, POLL_INTERVAL_MS);
