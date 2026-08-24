// Fill this in after deploying api/ to Render - keep in sync with app.js.
const API_BASE = "https://granco-monitor.onrender.com/";

function fmtTs(iso) {
  if (!iso) return "-";
  return new Date(iso).toLocaleString();
}

function fmtVal(value) {
  return value === null || value === undefined || value === "" ? "-" : value;
}

async function refreshRecipes() {
  const q = document.getElementById("recipes-search").value.trim();
  const params = new URLSearchParams();
  if (q) params.set("q", q);

  const res = await fetch(`${API_BASE}/api/recipes?${params.toString()}`);
  const data = await res.json();

  const staleBanner = document.getElementById("recipes-stale-banner");
  const syncedAtEl = document.getElementById("recipes-synced-at");
  if (data.stale) {
    staleBanner.textContent = data.synced_at
      ? `Recipe data hasn't updated since ${fmtTs(data.synced_at)} - recipe_sync may be down.`
      : "No recipe data has ever synced - recipe_sync may not be running.";
    staleBanner.classList.remove("hidden");
  } else {
    staleBanner.classList.add("hidden");
  }
  syncedAtEl.textContent = data.synced_at ? `Last synced ${fmtTs(data.synced_at)}` : "Never synced";

  const tbody = document.querySelector("#recipes-table tbody");
  tbody.innerHTML = "";
  for (const r of data.recipes || []) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${fmtVal(r.name)}</td>
      <td>${fmtVal(r.index)}</td>
      <td>${fmtVal(r.batch_height)}</td>
      <td>${fmtVal(r.batch_width)}</td>
      <td>${fmtVal(r.parts_per_cut)}</td>
      <td>${fmtVal(r.blade_feed_rate)}</td>
      <td>${fmtVal(r.cut_length)}</td>
      <td>${fmtVal(r.auto_trim_distance)}</td>
      <td>${fmtVal(r.quantity)}</td>
      <td>${fmtVal(r.unit)}</td>
      <td>${fmtVal(r.backgauge_pressure)}</td>
      <td>${fmtVal(r.side_clamp_pressure)}</td>
      <td>${fmtVal(r.top_clamp_pressure)}</td>
      <td>${fmtVal(r.csq)}</td>
      <td>${fmtVal(r.ccs)}</td>
    `;
    tbody.appendChild(tr);
  }

  document.getElementById("last-updated").textContent = `updated ${new Date().toLocaleTimeString()}`;
}

document.getElementById("recipes-search-btn").addEventListener("click", refreshRecipes);
document.getElementById("recipes-search").addEventListener("keydown", (e) => {
  if (e.key === "Enter") refreshRecipes();
});

refreshRecipes();
