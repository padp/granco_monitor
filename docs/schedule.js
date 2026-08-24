// Fill this in after deploying api/ to Render - keep in sync with app.js.
const API_BASE = "https://granco-monitor.onrender.com/";

const TOKEN_KEY = "scheduleToken";
const EMAIL_KEY = "scheduleEmail";

const authCard = document.getElementById("auth-card");
const scheduleCard = document.getElementById("schedule-card");
const authForm = document.getElementById("auth-form");
const authTitle = document.getElementById("auth-title");
const authSubmit = document.getElementById("auth-submit");
const authToggle = document.getElementById("auth-toggle");
const authError = document.getElementById("auth-error");

let authMode = "login"; // or "signup"

function getToken() {
  return localStorage.getItem(TOKEN_KEY);
}

function setSession(token, email) {
  localStorage.setItem(TOKEN_KEY, token);
  localStorage.setItem(EMAIL_KEY, email);
}

function clearSession() {
  localStorage.removeItem(TOKEN_KEY);
  localStorage.removeItem(EMAIL_KEY);
}

function showAuth(message) {
  authCard.classList.remove("hidden");
  scheduleCard.classList.add("hidden");
  authError.textContent = message || "";
}

function showSchedule() {
  authCard.classList.add("hidden");
  scheduleCard.classList.remove("hidden");
  document.getElementById("current-email").textContent = localStorage.getItem(EMAIL_KEY) || "-";
}

async function loadNotes() {
  try {
    const res = await fetch(`${API_BASE}/api/notes`);
    const data = await res.json();
    document.getElementById("notes-textarea").value = data.text || "";
  } catch {
    // leave the textarea as-is - the save button will still work
  }
}

document.getElementById("notes-save-btn").addEventListener("click", async () => {
  const status = document.getElementById("notes-save-status");
  const token = getToken();
  if (!token) {
    showAuth("Your session expired - please log in again.");
    return;
  }

  status.textContent = "Saving...";
  try {
    const res = await fetch(`${API_BASE}/api/notes`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
      body: JSON.stringify({ text: document.getElementById("notes-textarea").value }),
    });
    if (res.status === 401) {
      clearSession();
      showAuth("Your session expired - please log in again.");
      return;
    }
    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      status.textContent = data.error || "Save failed.";
      return;
    }
    status.textContent = "Saved.";
  } catch (err) {
    status.textContent = `Network error: ${err.message}`;
  }
});

authToggle.addEventListener("click", () => {
  authMode = authMode === "login" ? "signup" : "login";
  authTitle.textContent = authMode === "login" ? "Log In" : "Create Account";
  authSubmit.textContent = authMode === "login" ? "Log In" : "Create Account";
  authToggle.textContent = authMode === "login" ? "Need an account? Create one" : "Already have an account? Log in";
  authError.textContent = "";
});

authForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  const email = document.getElementById("auth-email").value.trim();
  const password = document.getElementById("auth-password").value;
  const endpoint = authMode === "login" ? "/api/login" : "/api/signup";

  try {
    const res = await fetch(`${API_BASE}${endpoint}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email, password }),
    });
    const data = await res.json();
    if (!res.ok) {
      authError.textContent = data.error || "Something went wrong.";
      return;
    }
    setSession(data.token, data.email);
    showSchedule();
    await loadCurrentShiftDefaults();
    await loadScheduleRows();
    await loadNotes();
  } catch (err) {
    authError.textContent = `Network error: ${err.message}`;
  }
});

document.getElementById("logout-btn").addEventListener("click", async () => {
  const token = getToken();
  clearSession();
  showAuth();
  if (token) {
    try {
      await fetch(`${API_BASE}/api/logout`, {
        method: "POST",
        headers: { Authorization: `Bearer ${token}` },
      });
    } catch {
      // best-effort only - the client-side token is already gone either way
    }
  }
});

function currentRows() {
  const rows = [];
  for (const tr of document.querySelectorAll("#schedule-table tbody tr")) {
    rows.push({
      part_number: tr.querySelector(".col-part").value.trim(),
      job_number: tr.querySelector(".col-job").value.trim(),
      racks: tr.querySelector(".col-racks").value ? Number(tr.querySelector(".col-racks").value) : null,
      estimated_hours: tr.querySelector(".col-hours").value ? Number(tr.querySelector(".col-hours").value) : null,
    });
  }
  return rows;
}

function addRow(row) {
  const tbody = document.querySelector("#schedule-table tbody");
  const tr = document.createElement("tr");
  const r = row || {};
  tr.innerHTML = `
    <td><input type="text" class="col-part" value="${r.part_number ?? ""}"></td>
    <td><input type="text" class="col-job" value="${r.job_number ?? ""}"></td>
    <td><input type="number" min="0" class="col-racks" value="${r.racks ?? ""}"></td>
    <td><input type="number" min="0" step="0.01" placeholder="e.g. 2.25" class="col-hours" value="${r.estimated_hours ?? ""}"></td>
    <td><button type="button" class="btn btn-link remove-row">Remove</button></td>
  `;
  tr.querySelector(".remove-row").addEventListener("click", () => tr.remove());
  tbody.appendChild(tr);
}

document.getElementById("add-row-btn").addEventListener("click", () => addRow());

async function loadCurrentShiftDefaults() {
  // Only used to pick a sensible starting date/shift when the page opens
  // cold - after that the date/shift fields are just whatever the user picks.
  try {
    const res = await fetch(`${API_BASE}/api/schedule/current`);
    const data = await res.json();
    document.getElementById("schedule-date").value = data.date;
    document.getElementById("schedule-shift").value = data.shift;
  } catch {
    document.getElementById("schedule-date").value = new Date().toISOString().slice(0, 10);
  }
}

async function loadScheduleRows() {
  const date = document.getElementById("schedule-date").value;
  const shift = document.getElementById("schedule-shift").value;
  if (!date || !shift) return;

  const tbody = document.querySelector("#schedule-table tbody");
  tbody.innerHTML = "";
  document.getElementById("save-status").textContent = "";

  const res = await fetch(`${API_BASE}/api/schedule?date=${date}&shift=${encodeURIComponent(shift)}`);
  const data = await res.json();
  for (const row of data.rows || []) addRow(row);
}

document.getElementById("schedule-date").addEventListener("change", loadScheduleRows);
document.getElementById("schedule-shift").addEventListener("change", loadScheduleRows);

document.getElementById("save-btn").addEventListener("click", async () => {
  const status = document.getElementById("save-status");
  const token = getToken();
  if (!token) {
    showAuth("Your session expired - please log in again.");
    return;
  }

  status.textContent = "Saving...";
  const body = {
    date: document.getElementById("schedule-date").value,
    shift: document.getElementById("schedule-shift").value,
    rows: currentRows(),
  };

  try {
    const res = await fetch(`${API_BASE}/api/schedule`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
      body: JSON.stringify(body),
    });
    if (res.status === 401) {
      clearSession();
      showAuth("Your session expired - please log in again.");
      return;
    }
    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      status.textContent = data.error || "Save failed.";
      return;
    }
    status.textContent = "Saved.";
  } catch (err) {
    status.textContent = `Network error: ${err.message}`;
  }
});

async function init() {
  if (!getToken()) {
    showAuth();
    return;
  }
  showSchedule();
  await loadCurrentShiftDefaults();
  await loadScheduleRows();
  await loadNotes();
}

init();
