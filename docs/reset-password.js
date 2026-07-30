// Fill this in after deploying api/ to Render - keep in sync with app.js.
const API_BASE = "https://granco-monitor.onrender.com/";

const resetCard = document.getElementById("reset-card");
const successCard = document.getElementById("reset-success-card");
const missingTokenCard = document.getElementById("reset-missing-token-card");
const resetForm = document.getElementById("reset-form");
const resetError = document.getElementById("reset-error");

const token = new URLSearchParams(window.location.search).get("token");

if (!token) {
  resetCard.classList.add("hidden");
  missingTokenCard.classList.remove("hidden");
} else {
  resetForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const newPassword = document.getElementById("new-password").value;
    const confirmPassword = document.getElementById("confirm-password").value;
    resetError.textContent = "";

    if (newPassword !== confirmPassword) {
      resetError.textContent = "Passwords don't match.";
      return;
    }

    try {
      const res = await fetch(`${API_BASE}/api/reset-password`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token, new_password: newPassword }),
      });
      const data = await res.json();
      if (!res.ok) {
        resetError.textContent = data.error || "Something went wrong.";
        return;
      }
      resetCard.classList.add("hidden");
      successCard.classList.remove("hidden");
    } catch (err) {
      resetError.textContent = `Network error: ${err.message}`;
    }
  });
}
