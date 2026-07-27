// Fill this in after deploying api/ to Render.
const API_BASE = "https://YOUR-API.onrender.com";

const POLL_INTERVAL_MS = 5000;

function fmtTs(iso) {
  if (!iso) return "-";
  return new Date(iso).toLocaleString();
}

function fmtSeconds(value) {
  return value === null || value === undefined ? "-" : Number(value).toFixed(1);
}

async function refreshStatus() {
  const res = await fetch(`${API_BASE}/api/status`);
  const data = await res.json();

  const state = data.state?.state || "UNKNOWN";
  const badge = document.getElementById("state-badge");
  badge.textContent = state;
  badge.className = `badge ${state.toLowerCase()}`;

  document.getElementById("part-number").textContent =
    data.latest_cycle?.part_number || "-";
  document.getElementById("last-cycle-ts").textContent = fmtTs(data.latest_cycle?.ts);
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
      <td>${fmtSeconds(cycle.cycle_duration_s)}</td>
      <td>${fmtSeconds(cycle.theoretical_duration_s)}</td>
      <td>${cycle.is_trim_cut ? "yes" : ""}</td>
    `;
    tbody.appendChild(tr);
  }
}

async function refreshAll() {
  try {
    await Promise.all([refreshStatus(), refreshCycles()]);
    document.getElementById("last-updated").textContent =
      `updated ${new Date().toLocaleTimeString()}`;
  } catch (err) {
    document.getElementById("last-updated").textContent = `error: ${err.message}`;
  }
}

refreshAll();
setInterval(refreshAll, POLL_INTERVAL_MS);
