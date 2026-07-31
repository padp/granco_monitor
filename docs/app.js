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

  const operatorNames = (data.operators || []).join(", ") || "-";
  const asOf = data.as_of ? ` (as of ${fmtTs(data.as_of)})` : "";
  document.getElementById("staffing-operators").textContent = `${operatorNames}${asOf}`;
}

// Grade thresholds are actual/theoretical duration ratios, not a measured
// standard - a starting heuristic to flag cuts worth a second look, same
// spirit as this project's other not-yet-measured constants. The four
// base categories (Great/Good/Slow/Poor) and their color are fixed here;
// the emoji/wording/points shown for each are entirely data-driven from
// grade-flavors.json (see loadGradeFlavors below), so new flavor text
// can be added there without touching this file.
const GRADE_CATEGORIES = [
  { max: 1.15, name: "Great", cls: "grade-green" },
  { max: 1.4, name: "Good", cls: "grade-yellow" },
  { max: 2.0, name: "Slow", cls: "grade-orange" },
];
const POOR_CATEGORY = { name: "Poor", cls: "grade-red" };

let gradeFlavors = null;

async function loadGradeFlavors() {
  const res = await fetch("grade-flavors.json");
  gradeFlavors = await res.json();
}

// A simple stable hash (not Math.random()) so the same cut always shows
// the same flavor variant across poll refreshes instead of visibly
// reshuffling every few seconds - variety comes from different cuts
// landing on different variants, not from one row flickering in place.
function stableIndex(seed, length) {
  let hash = 0;
  for (let i = 0; i < seed.length; i++) {
    hash = (hash * 31 + seed.charCodeAt(i)) | 0;
  }
  return Math.abs(hash) % length;
}

function categoryFor(ratio) {
  return GRADE_CATEGORIES.find((c) => ratio <= c.max) || POOR_CATEGORY;
}

function flavorFor(categoryName, seed) {
  const entry = gradeFlavors?.[categoryName];
  const variants = entry?.variants;
  if (!variants || !variants.length) {
    return { emoji: "", text: categoryName, points: entry?.points ?? 0 };
  }
  const variant = variants[stableIndex(seed, variants.length)];
  return { emoji: variant.emoji, text: variant.text, points: entry.points ?? 0 };
}

function gradeCellHtml(cycle) {
  // Trim/reload cycles aren't graded: their theoretical_duration_s only
  // covers the blade stroke, not the reload/advance time (that term is
  // deliberately left out server-side until it's actually measured - see
  // collector/config.py's BACKGAUGE_RETURN_TIME_S), so comparing a trim
  // cut's actual time against it would always look artificially bad.
  if (cycle.is_trim_cut) return "-";

  const actual = cycle.cycle_duration_s;
  const theoretical = cycle.theoretical_duration_s;
  if (actual === null || actual === undefined || !theoretical) return "-";

  const ratio = actual / theoretical;
  const category = categoryFor(ratio);
  const flavor = flavorFor(category.name, cycle.ts || "");
  const fireClass = category.name === "Great" ? "grade-fire" : "";
  return `<span class="grade-pill ${category.cls} ${fireClass}" title="${category.name} · ${ratio.toFixed(2)}x theoretical">
    <span class="grade-dot"></span><span class="grade-label">${flavor.emoji} ${flavor.text}</span>
    <span class="grade-points">+${flavor.points}</span>
  </span>`;
}

async function refreshSchedule() {
  const res = await fetch(`${API_BASE}/api/schedule/current`);
  const data = await res.json();

  document.getElementById("schedule-label").textContent =
    data.date && data.shift ? `${data.date} — ${data.shift}` : "-";

  const tbody = document.querySelector("#schedule-mirror-table tbody");
  tbody.innerHTML = "";
  for (const row of data.rows || []) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${row.part_number || "-"}</td>
      <td>${row.job_number || "-"}</td>
      <td>${row.racks ?? "-"}</td>
      <td>${row.scheduled_time || "-"}</td>
    `;
    tbody.appendChild(tr);
  }
}

const MEDALS = ["🥇", "🥈", "🥉"];

// Same 4-band feel as the per-cut Grade column, just against a 0-100
// weekly score instead of a duration ratio - not a measured standard,
// a starting heuristic.
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

  document.getElementById("leaderboard-window").textContent =
    data.window_days ? `Last ${data.window_days} days` : "-";

  const list = document.getElementById("leaderboard-list");
  list.innerHTML = "";
  (data.shifts || []).forEach((s, i) => {
    const li = document.createElement("li");
    li.className = `leaderboard-row ${i === 0 ? "rank-1" : ""}`;
    const scoreText = s.score === null || s.score === undefined ? "no data" : `${s.score}%`;
    const effText = s.efficiency_pct === null || s.efficiency_pct === undefined ? "-" : `${s.efficiency_pct}%`;
    const gradeText = s.grade_score === null || s.grade_score === undefined ? "-" : `${s.grade_score}%`;
    li.innerHTML = `
      <span class="leaderboard-medal">${MEDALS[i] || ""}</span>
      <span class="leaderboard-shift">
        ${s.shift}
        <div class="leaderboard-breakdown">eff ${effText} &middot; grade ${gradeText}</div>
      </span>
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

async function refreshCycles() {
  const res = await fetch(`${API_BASE}/api/cycles/recent?limit=50`);
  const data = await res.json();

  const tbody = document.querySelector("#cycles-table tbody");
  tbody.innerHTML = "";
  for (const cycle of data.cycles || []) {
    const tr = document.createElement("tr");
    if (cycle.is_trim_cut) tr.classList.add("trim-row");
    tr.innerHTML = `
      <td>${fmtTs(cycle.ts)}</td>
      <td>${cycle.part_number ?? "-"}</td>
      <td>${cycle.cut_number ?? "-"}</td>
      <td>${fmtSeconds(cycle.cycle_duration_s)}</td>
      <td>${fmtSeconds(cycle.theoretical_duration_s)}</td>
      <td>${fmtSeconds(cycle.cut_length)}</td>
      <td>${cycle.parts_per_cut ?? "-"}</td>
      <td>${cycle.is_trim_cut ? "Trim / Reload" : ""}</td>
      <td>${gradeCellHtml(cycle)}</td>
    `;
    tbody.appendChild(tr);
  }
}

async function refreshAll() {
  try {
    await Promise.all([
      refreshStatus(),
      refreshCycles(),
      refreshStaffing(),
      refreshSchedule(),
      refreshLeaderboard(),
      refreshActiveShifts(),
    ]);
    document.getElementById("last-updated").textContent =
      `updated ${new Date().toLocaleTimeString()}`;
  } catch (err) {
    document.getElementById("last-updated").textContent = `error: ${err.message}`;
  }
}

async function init() {
  await loadGradeFlavors();
  refreshAll();
  setInterval(refreshAll, POLL_INTERVAL_MS);
}

init();
