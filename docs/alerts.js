// Fill this in after deploying api/ to Render - keep in sync with app.js/schedule.js.
const API_BASE = "https://granco-monitor.onrender.com/";

// Same keys schedule.js uses (not "alertsToken"/etc.) - deliberately, so
// logging in on either page counts as logged in on both. This is one
// shared account system, not a per-page one; the key names just happen to
// be named after the page that introduced them first.
const TOKEN_KEY = "scheduleToken";
const EMAIL_KEY = "scheduleEmail";

// The manual trigger builder (condition rows, presets, the recipient/
// test-send/create form) that used to live here was removed - see
// padp/alert-assistant, a chat-driven front end for the same
// api/alerts.py this page has always talked to. This page is now
// read-only: view, enable/disable, and delete whatever alerts already
// exist (still needs a login, same as before, since those are writes).
// See git history for the removed builder's code if it's ever needed
// again.

const authCard = document.getElementById("auth-card");
const alertsCard = document.getElementById("alerts-card");
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
  alertsCard.classList.add("hidden");
  authError.textContent = message || "";
}

function showAlerts() {
  authCard.classList.add("hidden");
  alertsCard.classList.remove("hidden");
  document.getElementById("current-email").textContent = localStorage.getItem(EMAIL_KEY) || "-";
}

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
    showAlerts();
    await loadExistingAlerts();
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
      await fetch(`${API_BASE}/api/logout`, { method: "POST", headers: { Authorization: `Bearer ${token}` } });
    } catch {
      // best-effort only - the client-side token is already gone either way
    }
  }
});

function renderExistingAlerts(alertList) {
  const container = document.getElementById("existing-alerts");
  container.innerHTML = "";
  if (alertList.length === 0) {
    const p = document.createElement("p");
    p.className = "form-status";
    p.textContent = "No alerts created yet.";
    container.appendChild(p);
    return;
  }

  for (const rule of alertList) {
    const card = document.createElement("div");
    card.className = "alert-card";

    const header = document.createElement("div");
    header.className = "alert-card-header";

    const left = document.createElement("div");
    const strong = document.createElement("strong");
    strong.textContent = rule.label || "Untitled alert";
    left.appendChild(strong);
    if (rule.recipient_email) {
      const span = document.createElement("span");
      span.className = "form-status";
      span.textContent = ` · to ${rule.recipient_email}`;
      left.appendChild(span);
    }
    if (rule.repeat === "one_time") {
      const span = document.createElement("span");
      span.className = "form-status";
      span.textContent = " · one-time";
      left.appendChild(span);
    }
    header.appendChild(left);

    const right = document.createElement("div");
    const toggleBtn = document.createElement("button");
    toggleBtn.type = "button";
    toggleBtn.className = "btn btn-secondary";
    toggleBtn.textContent = rule.active ? "Disable" : "Enable";
    toggleBtn.addEventListener("click", () => toggleActive(rule));
    const deleteBtn = document.createElement("button");
    deleteBtn.type = "button";
    deleteBtn.className = "btn btn-secondary";
    deleteBtn.textContent = "Delete";
    deleteBtn.addEventListener("click", () => deleteRule(rule));
    right.appendChild(toggleBtn);
    right.appendChild(deleteBtn);
    header.appendChild(right);

    card.appendChild(header);

    if (!rule.active) {
      const p = document.createElement("p");
      p.className = "form-status";
      p.textContent = "Disabled — won't post to Teams.";
      card.appendChild(p);
    }

    const list = document.createElement("ul");
    list.className = "trigger-list";
    for (const t of rule.triggers) {
      const li = document.createElement("li");
      li.className = "trigger-list-item";
      const span = document.createElement("span");
      span.textContent = t.description;
      li.appendChild(span);
      list.appendChild(li);
    }
    card.appendChild(list);

    container.appendChild(card);
  }
}

async function toggleActive(rule) {
  const token = getToken();
  try {
    const res = await fetch(`${API_BASE}/api/alerts/${rule._id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
      body: JSON.stringify({ active: !rule.active }),
    });
    if (res.status === 401) {
      clearSession();
      showAuth("Your session expired - please log in again.");
      return;
    }
    if (!res.ok) {
      window.alert("Could not update alert.");
      return;
    }
    await loadExistingAlerts();
  } catch (err) {
    window.alert(`Network error: ${err.message}`);
  }
}

async function deleteRule(rule) {
  if (!window.confirm(`Delete "${rule.label || "this alert"}"? Nothing will post to Teams for it anymore.`)) return;
  const token = getToken();
  try {
    const res = await fetch(`${API_BASE}/api/alerts/${rule._id}`, {
      method: "DELETE",
      headers: { Authorization: `Bearer ${token}` },
    });
    if (res.status === 401) {
      clearSession();
      showAuth("Your session expired - please log in again.");
      return;
    }
    if (!res.ok) {
      window.alert("Could not delete alert.");
      return;
    }
    await loadExistingAlerts();
  } catch (err) {
    window.alert(`Network error: ${err.message}`);
  }
}

async function loadExistingAlerts() {
  const res = await fetch(`${API_BASE}/api/alerts`);
  const data = await res.json();
  renderExistingAlerts(data.alerts || []);
}

async function init() {
  if (!getToken()) {
    showAuth();
    return;
  }
  showAlerts();
  await loadExistingAlerts();
}

init();
