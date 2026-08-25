// Fill this in after deploying api/ to Render - keep in sync with app.js.
const API_BASE = "https://granco-monitor.onrender.com/";

const POLL_INTERVAL_MS = 5000;

const MEDALS = ["🥇", "🥈", "🥉"];

// Same 4-band feel as the per-cut Grade column, just against a 0-100
// weekly score instead of a duration ratio - not a measured standard,
// a starting heuristic. Duplicated from app.js (no shared module in
// this no-build-step static site - see e.g. shift-details.js's own
// fmtTs copy for the same pattern).
const SCORE_BANDS = [
  { min: 85, cls: "grade-green" },
  { min: 70, cls: "grade-yellow" },
  { min: 50, cls: "grade-orange" },
];

function scoreClass(score) {
  if (score === null || score === undefined) return "grade-unknown";
  return (SCORE_BANDS.find((b) => score >= b.min) || { cls: "grade-red" }).cls;
}

async function refreshLeaderboard() {
  const res = await fetch(`${API_BASE}/api/shifts/leaderboard`);
  const data = await res.json();

  const list = document.getElementById("leaderboard-list");
  list.innerHTML = "";
  (data.shifts || []).forEach((s, i) => {
    const li = document.createElement("li");
    li.className = `leaderboard-row ${i === 0 ? "rank-1" : ""}`;
    const scoreText = s.score === null || s.score === undefined ? "no data" : `${s.score}%`;
    li.innerHTML = `
      <span class="leaderboard-medal">${MEDALS[i] || ""}</span>
      <span class="leaderboard-shift">${s.shift}</span>
      <span class="leaderboard-score ${scoreClass(s.score)}">${scoreText}</span>
    `;
    list.appendChild(li);
  });
}

async function refreshActiveShifts() {
  const res = await fetch(`${API_BASE}/api/shifts/active-count`);
  const data = await res.json();

  const list = document.getElementById("active-shifts-list");
  list.innerHTML = "";
  for (const s of data.shifts || []) {
    const li = document.createElement("li");
    li.className = "utilization-row";
    const scheduledDays = s.scheduled_days ?? 5;
    const pct = (s.active_count / scheduledDays) * 100;
    const cls = scoreClass(pct);
    const overtimeText = s.overtime_count ? ` (+${s.overtime_count} overtime)` : "";
    li.innerHTML = `
      <span class="utilization-shift">${s.shift}</span>
      <div class="utilization-bar"><div class="utilization-bar-fill ${cls}" style="width: ${pct}%"></div></div>
      <span class="utilization-pct">${s.active_count} of ${scheduledDays}${overtimeText}</span>
    `;
    list.appendChild(li);
  }
}

async function refreshAll() {
  try {
    await Promise.all([refreshLeaderboard(), refreshActiveShifts()]);
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
