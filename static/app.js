(() => {
  const $ = (id) => document.getElementById(id);
  if (typeof L === "undefined") { document.body.insertAdjacentHTML("afterbegin", "<p style='padding:24px'>The map library failed to load (static/vendor/leaflet missing?).</p>"); return; }
  const map = L.map("map", { zoomControl: false }).setView([20, 0], 2);
  L.control.zoom({ position: "topright" }).addTo(map);
  L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19, attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
  }).addTo(map);

  let pin = null, simMarker = null, status = null, busy = false, toastTimer = null, centeredOnSim = false;

  // ---------- helpers ----------
  async function api(method, path, body) {
    let res;
    try {
      res = await fetch(path, {
        method, headers: body ? { "Content-Type": "application/json" } : {}, body: body ? JSON.stringify(body) : undefined,
      });
    } catch (e) {
      throw { message: "Cannot reach the local server.", hint: "Is `python app.py` still running?" };
    }
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      const err = data.error || (Array.isArray(data.detail) ? { message: "Invalid input.", hint: data.detail[0]?.msg } : { message: `Error ${res.status}` });
      throw err;
    }
    return data;
  }
  function toast(msg, ms = 3500) {
    const t = $("toast"); t.textContent = msg; t.hidden = false;
    clearTimeout(toastTimer); toastTimer = setTimeout(() => (t.hidden = true), ms);
  }
  const fail = (e) => toast(e.hint ? `${e.message} ${e.hint}` : e.message, 7000);
  const fmt = (n) => Number(n).toFixed(6);
  function fmtDist(m) { return m >= 1000 ? `${(m / 1000).toFixed(2)} km` : `${Math.round(m)} m`; }

  // ---------- pin ----------
  function setPin(lat, lon, { pan = false } = {}) {
    if (!pin) {
      pin = L.marker([lat, lon], { draggable: true }).addTo(map);
      pin.on("dragend", () => { const p = pin.getLatLng(); fillInputs(p.lat, p.lng); });
    } else pin.setLatLng([lat, lon]);
    fillInputs(lat, lon);
    if (pan) map.setView([lat, lon], Math.max(map.getZoom(), 15));
  }
  function fillInputs(lat, lon) { $("lat").value = fmt(lat); $("lon").value = fmt(lon); $("pinHint").textContent = "Drag the pin to fine-tune."; }
  function readInputs() {
    const lat = parseFloat($("lat").value), lon = parseFloat($("lon").value);
    if (!Number.isFinite(lat) || !Number.isFinite(lon)) throw { message: "Enter a valid latitude and longitude, or click the map." };
    if (Math.abs(lat) > 90 || Math.abs(lon) > 180) throw { message: "Latitude must be within ±90 and longitude within ±180." };
    return { lat, lon };
  }
  map.on("click", (e) => setPin(e.latlng.lat, e.latlng.lng));
  for (const id of ["lat", "lon"]) $(id).addEventListener("change", () => {
    try { const { lat, lon } = readInputs(); setPin(lat, lon, { pan: true }); } catch (e) { fail(e); }
  });

  // ---------- actions ----------
  async function guarded(btn, fn) {
    if (busy) return; busy = true; btn.disabled = true;
    try { await fn(); } catch (e) { fail(e); } finally { busy = false; btn.disabled = false; refresh(); }
  }
  $("setBtn").onclick = (ev) => guarded(ev.currentTarget, async () => {
    const c = readInputs();
    await api("POST", "/api/location", c);
    toast(`Location set to ${fmt(c.lat)}, ${fmt(c.lon)}`);
  });
  $("clearBtn").onclick = (ev) => guarded(ev.currentTarget, async () => {
    await api("DELETE", "/api/location"); centeredOnSim = false; toast("Simulation stopped — real GPS restored.");
  });
  $("walkBtn").onclick = (ev) => guarded(ev.currentTarget, async () => {
    const c = readInputs();
    await api("POST", "/api/walk", { ...c, speed_kmh: Number($("speed").value) });
    toast("Moving…");
  });
  $("stopBtn").onclick = (ev) => guarded(ev.currentTarget, async () => { await api("DELETE", "/api/walk"); toast("Stopped."); });
  $("trustBtn").onclick = (ev) => guarded(ev.currentTarget, async () => {
    toast("Look at your iPhone and tap Trust (enter your passcode if asked)…", 8000);
    await api("POST", "/api/trust");
  });
  $("speed").addEventListener("input", () => { $("speedOut").textContent = $("speed").value; });
  $("speed").addEventListener("change", async () => {
    if (status?.walk?.active) try { await api("PATCH", "/api/walk", { speed_kmh: Number($("speed").value) }); } catch (e) { fail(e); }
  });
  document.querySelectorAll(".chipbtn").forEach((b) => b.addEventListener("click", () => {
    $("speed").value = b.dataset.speed; $("speed").dispatchEvent(new Event("input")); $("speed").dispatchEvent(new Event("change"));
  }));

  // ---------- search ----------
  $("searchForm").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const q = $("searchInput").value.trim(); if (!q) return;
    const ul = $("results"); ul.hidden = false; ul.replaceChildren(li("Searching…"));
    try {
      const { results } = await api("GET", `/api/search?q=${encodeURIComponent(q)}`);
      if (!results.length) return ul.replaceChildren(li("No results."));
      ul.replaceChildren(...results.map((r) => {
        const el = li(r.name); el.onclick = () => { setPin(r.lat, r.lon, { pan: true }); ul.hidden = true; }; return el;
      }));
      setPin(results[0].lat, results[0].lon, { pan: true });   // jump to best match; the list lets you pick another
    } catch (e) { ul.hidden = true; fail(e); }
  });
  function li(text) { const el = document.createElement("li"); const s = document.createElement("span"); s.className = "name"; s.textContent = text; el.append(s); return el; }

  // ---------- favorites ----------
  async function loadFavs() {
    try {
      const { favorites } = await api("GET", "/api/favorites");
      $("favs").replaceChildren(...(favorites.length ? favorites.map((f) => {
        const el = li(f.name);
        el.title = `${fmt(f.lat)}, ${fmt(f.lon)}`;
        el.onclick = () => setPin(f.lat, f.lon, { pan: true });
        const del = document.createElement("button"); del.className = "del"; del.textContent = "✕"; del.setAttribute("aria-label", `Delete ${f.name}`);
        del.onclick = async (e) => { e.stopPropagation(); try { await api("DELETE", `/api/favorites/${f.id}`); loadFavs(); } catch (x) { fail(x); } };
        el.append(del); return el;
      }) : [li("No favorites yet.")]));
    } catch (e) { fail(e); }
  }
  $("favForm").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    try {
      const c = readInputs(); const name = $("favName").value.trim();
      if (!name) throw { message: "Type a name for this favorite." };
      await api("POST", "/api/favorites", { name, ...c }); $("favName").value = ""; loadFavs(); toast("Saved.");
    } catch (e) { fail(e); }
  });

  // ---------- status polling ----------
  function render(s) {
    status = s;
    const chip = $("device"), banner = $("banner");
    chip.dataset.state = s.state;
    if (s.state === "ready") {
      $("deviceText").textContent = `${s.device.name} · iOS ${s.device.ios_version}`;
      chip.title = `Method: ${s.device.method}`;
      banner.hidden = true;
    } else {
      $("deviceText").textContent = s.error?.message || "Not ready";
      chip.title = "";
      $("bannerMsg").textContent = s.error?.message || "";
      $("bannerHint").textContent = s.error?.hint || "";
      $("trustBtn").hidden = s.state !== "not_trusted";
      banner.hidden = false;
    }
    const sim = s.simulation;
    $("simState").textContent = sim.active ? `Simulating ${fmt(sim.lat)}, ${fmt(sim.lon)}` : "Not simulating — phone uses its real GPS.";
    if (sim.active) {
      const icon = L.divIcon({ className: "", html: '<div class="sim-marker"></div>', iconSize: [18, 18] });
      if (!simMarker) simMarker = L.marker([sim.lat, sim.lon], { icon, interactive: false, zIndexOffset: -100 }).addTo(map);
      else simMarker.setLatLng([sim.lat, sim.lon]);
      if (!centeredOnSim) { centeredOnSim = true; if (!pin) map.setView([sim.lat, sim.lon], 13); }
    } else if (simMarker) { simMarker.remove(); simMarker = null; }
    const w = s.walk || {};
    $("walkState").textContent = w.error ? `Stopped: ${w.error.message}` : w.active ? `Moving at ${w.speed_kmh} km/h — ${fmtDist(w.remaining_m)} to go` : "";
  }
  async function refresh() {
    try { render(await api("GET", "/api/status")); }
    catch (e) { render({ state: "server_down", error: e, device: null, simulation: { active: false }, walk: {} }); }
  }
  (function loop() { refresh().finally(() => setTimeout(loop, status?.walk?.active ? 1000 : 3000)); })();
  loadFavs();
})();
