// Fill this in after deploying api/ to Render - keep in sync with app.js/schedule.js.
const API_BASE = "https://granco-monitor.onrender.com/";

const form = document.getElementById("admin-reset-form");
const errorEl = document.getElementById("admin-reset-error");
const statusEl = document.getElementById("admin-reset-status");

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  errorEl.textContent = "";
  statusEl.textContent = "";

  const apiKey = document.getElementById("admin-reset-key").value;
  const email = document.getElementById("admin-reset-email").value.trim().toLowerCase();
  const newPassword = document.getElementById("admin-reset-password").value;

  try {
    const res = await fetch(`${API_BASE}/api/admin/reset-password`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Api-Key": apiKey },
      body: JSON.stringify({ email, new_password: newPassword }),
    });
    const data = await res.json();
    if (!res.ok) {
      errorEl.textContent = data.error || `Request failed (${res.status})`;
      return;
    }
    statusEl.textContent = `Password reset for ${email}.`;
    form.reset();
  } catch (err) {
    errorEl.textContent = `Network error: ${err.message}`;
  }
});
