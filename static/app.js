(() => {
  const $ = (id) => document.getElementById(id);
  if (typeof L === "undefined") { document.body.insertAdjacentHTML("afterbegin", "<p style='padding:24px'>The map library failed to load (static/vendor/leaflet missing?).</p>"); return; }
  const map = L.map("map", { zoomControl: false }).setView([20, 0], 2);
  L.control.zoom({ position: "topright" }).addTo(map);
  L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19, attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
  }).addTo(map);

  let pin = null, simMarker = null, status = null, busy = false, toastTimer = null, centeredOnSim = false;
  let mode = "walk", stops = [], houses = [];
  let routeLayer = null, routeFetched = false, stopLayer = L.layerGroup().addTo(map), houseLayer = L.layerGroup().addTo(map);

  // ---------- helpers ----------
  async function api(method, path, body) {
    let res;
    try {
      res = await fetch(path, { method, headers: body ? { "Content-Type": "application/json" } : {}, body: body ? JSON.stringify(body) : undefined });
    } catch (e) {
      throw { message: "Cannot reach the local server.", hint: "Is `python app.py` still running?" };
    }
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw data.error || (Array.isArray(data.detail) ? { message: "Invalid input.", hint: data.detail[0]?.msg } : { message: `Error ${res.status}` });
    return data;
  }
  function toast(msg, ms = 3500) {
    const t = $("toast"); t.textContent = msg; t.hidden = false;
    clearTimeout(toastTimer); toastTimer = setTimeout(() => (t.hidden = true), ms);
  }
  const fail = (e) => toast(e.hint ? `${e.message} ${e.hint}` : e.message, 7000);
  const fmt = (n) => Number(n).toFixed(6);
  const fmtDist = (m) => (m >= 1000 ? `${(m / 1000).toFixed(2)} km` : `${Math.round(m)} m`);
  const fmtSecs = (s) => (s >= 60 ? `${Math.floor(s / 60)} min ${Math.round(s % 60)} s` : `${Math.round(s)} s`);
  function li(text) { const el = document.createElement("li"); const s = document.createElement("span"); s.className = "name"; s.textContent = text; el.append(s); return el; }
  function miniBtn(label, title, fn) {
    const b = document.createElement("button"); b.className = "miniBtn"; b.textContent = label; b.title = title; b.setAttribute("aria-label", title);
    b.onclick = (e) => { e.stopPropagation(); fn(); }; return b;
  }
  function haversine(a, b) {
    const R = 6371008.8, r = Math.PI / 180, dp = (b[0] - a[0]) * r, dl = (b[1] - a[1]) * r;
    const x = Math.sin(dp / 2) ** 2 + Math.cos(a[0] * r) * Math.cos(b[0] * r) * Math.sin(dl / 2) ** 2;
    return 2 * R * Math.asin(Math.sqrt(x));
  }
  const shortName = (name) => name.split(",").slice(0, 2).join(",").trim();

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
    toast(`Location set to ${fmt(c.lat)}, ${fmt(c.lon)}`); loadRecent();
  });
  $("clearBtn").onclick = (ev) => guarded(ev.currentTarget, async () => {
    await api("DELETE", "/api/location"); centeredOnSim = false; clearRoute(); toast("Simulation stopped — real GPS restored.");
  });
  $("stopBtn").onclick = (ev) => guarded(ev.currentTarget, async () => { await api("DELETE", "/api/journey"); clearRoute(); toast("Stopped."); });
  $("trustBtn").onclick = (ev) => guarded(ev.currentTarget, async () => {
    toast("Look at your iPhone and tap Trust (enter your passcode if asked)…", 8000);
    await api("POST", "/api/trust");
  });

  // path type + speed
  document.querySelectorAll(".segbtn").forEach((b) => b.addEventListener("click", () => {
    mode = b.dataset.mode;
    document.querySelectorAll(".segbtn").forEach((x) => { x.classList.toggle("active", x === b); x.setAttribute("aria-checked", x === b); });
    if (mode === "walk") setSpeed(5); else if (mode === "drive") setSpeed(50);
  }));
  function setSpeed(v) { $("speed").value = v; $("speed").dispatchEvent(new Event("input")); $("speed").dispatchEvent(new Event("change")); }
  $("speed").addEventListener("input", () => { $("speedOut").textContent = $("speed").value; });
  $("speed").addEventListener("change", async () => {
    if (status?.walk?.active && status.walk.kind !== "trick_or_treat")
      try { await api("PATCH", "/api/journey", { speed_kmh: Number($("speed").value) }); } catch (e) { fail(e); }
  });
  document.querySelectorAll(".chipbtn").forEach((b) => b.addEventListener("click", () => setSpeed(b.dataset.speed)));

  async function startJourney(body, okMsg) {
    const res = await api("POST", "/api/journey", body);
    drawRoute(res.route); toast(okMsg);
  }
  $("walkBtn").onclick = (ev) => guarded(ev.currentTarget, async () => {
    const c = readInputs();
    await startJourney({ stops: [c], mode, speed_kmh: Number($("speed").value) }, mode === "straight" ? "Moving in a straight line…" : `Following the ${mode === "walk" ? "walking" : "driving"} route…`);
  });

  // ---------- route drawing ----------
  function drawRoute(points) {
    clearRoute(false);
    if (!points || points.length < 2) return;
    routeLayer = L.polyline(points, { color: getComputedStyle(document.documentElement).getPropertyValue("--accent").trim() || "#0a84ff", weight: 5, opacity: 0.75, className: "route-line" }).addTo(map);
    routeFetched = true;
    map.fitBounds(routeLayer.getBounds().pad(0.2), { maxZoom: 17 });
  }
  function clearRoute(resetFlag = true) { if (routeLayer) { routeLayer.remove(); routeLayer = null; } if (resetFlag) routeFetched = false; }

  // ---------- multi-stop ----------
  function renderStops() {
    stopLayer.clearLayers();
    $("stops").replaceChildren(...stops.map((s, i) => {
      const el = li(`${i + 1}.  ${s.label || `${fmt(s.lat)}, ${fmt(s.lon)}`}`);
      el.append(
        miniBtn("↑", "Move up", () => { if (i) { [stops[i - 1], stops[i]] = [stops[i], stops[i - 1]]; renderStops(); } }),
        miniBtn("↓", "Move down", () => { if (i < stops.length - 1) { [stops[i + 1], stops[i]] = [stops[i], stops[i + 1]]; renderStops(); } }),
        miniBtn("✕", "Remove stop", () => { stops.splice(i, 1); renderStops(); }),
      );
      L.marker([s.lat, s.lon], { icon: L.divIcon({ className: "", html: `<div class="num-marker">${i + 1}</div>`, iconSize: [24, 24] }), interactive: false }).addTo(stopLayer);
      return el;
    }));
    if (stops.length >= 2) L.polyline(stops.map((s) => [s.lat, s.lon]), { color: "#888", weight: 2, dashArray: "4 6", interactive: false }).addTo(stopLayer);
  }
  function addStop(lat, lon, label) { if (stops.length >= 59) return toast("That's the maximum number of stops."); stops.push({ lat, lon, label }); $("routeBox").open = true; renderStops(); }
  $("addStopBtn").onclick = () => { try { const c = readInputs(); addStop(c.lat, c.lon); } catch (e) { fail(e); } };
  $("clearStopsBtn").onclick = () => { stops = []; renderStops(); };
  $("routeBtn").onclick = (ev) => guarded(ev.currentTarget, async () => {
    if (!stops.length) throw { message: "Add at least one stop first." };
    const dwell = Math.max(0, Number($("dwell").value) || 0);
    await startJourney({
      stops: stops.map((s) => ({ lat: s.lat, lon: s.lon, dwell_s: dwell })), mode, speed_kmh: Number($("speed").value), loop: $("loop").checked,
    }, `Route started (${stops.length} stop${stops.length > 1 ? "s" : ""}).`);
  });

  // ---------- trick or treat ----------
  const TOT_SPEED = 4.5;
  function setTheme(on) { document.documentElement.dataset.theme = on ? "halloween" : ""; $("title").textContent = "iPhone Location"; }
  $("totOn").addEventListener("change", () => {
    const on = $("totOn").checked; $("totBody").hidden = !on; setTheme(on);
    if (on) $("totBox").open = true; else clearHouses();
    if (routeLayer) routeLayer.setStyle({ color: getComputedStyle(document.documentElement).getPropertyValue("--accent").trim() });
  });
  $("radius").addEventListener("input", () => ($("radiusOut").textContent = $("radius").value));
  $("houses").addEventListener("input", () => ($("housesOut").textContent = $("houses").value));
  function clearHouses() { houses = []; houseLayer.clearLayers(); $("totInfo").textContent = ""; $("totStartBtn").disabled = true; }
  $("findBtn").onclick = (ev) => guarded(ev.currentTarget, async () => {
    const c = readInputs(); clearHouses();
    $("totInfo").textContent = "Looking for houses…";
    try {
      const res = await api("POST", "/api/trick-or-treat/plan", { ...c, radius_m: Number($("radius").value), houses: Number($("houses").value) });
      houses = res.houses;
    } catch (e) { $("totInfo").textContent = ""; throw e; }
    houses.forEach((h, i) => L.marker([h.lat, h.lon], { icon: L.divIcon({ className: "", html: `<div class="pumpkin">🎃</div>`, iconSize: [22, 22] }), title: `House ${i + 1}`, interactive: false }).addTo(houseLayer));
    L.polyline(houses.map((h) => [h.lat, h.lon]), { color: "#ff7a00", weight: 2, dashArray: "3 6", interactive: false }).addTo(houseLayer);
    map.fitBounds(L.latLngBounds(houses.map((h) => [h.lat, h.lon])).pad(0.25), { maxZoom: 18 });
    let dist = 0; for (let i = 1; i < houses.length; i++) dist += haversine([houses[i - 1].lat, houses[i - 1].lon], [houses[i].lat, houses[i].lon]);
    const secs = dist / (TOT_SPEED / 3.6) * 1.3 + houses.reduce((a, h) => a + h.dwell_s, 0);
    $("totInfo").textContent = `${houses.length} houses · about ${fmtDist(dist)} of walking · roughly ${Math.round(secs / 60)} min.`;
    $("totStartBtn").disabled = false;
  });
  $("totStartBtn").onclick = (ev) => guarded(ev.currentTarget, async () => {
    if (!houses.length) throw { message: "Find houses first." };
    await startJourney({
      stops: houses.map((h) => ({ lat: h.lat, lon: h.lon, dwell_s: h.dwell_s })), mode: "walk", speed_kmh: TOT_SPEED,
      jump_to_first: true, kind: "trick_or_treat", speed_jitter: 0.2,
    }, "Happy Halloween! 🎃 Heading out…");
  });

  // ---------- search ----------
  $("searchForm").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const q = $("searchInput").value.trim(); if (!q) return;
    const ul = $("results"); ul.hidden = false; ul.replaceChildren(li("Searching…"));
    try {
      const { results } = await api("GET", `/api/search?q=${encodeURIComponent(q)}`);
      if (!results.length) return ul.replaceChildren(li("No results."));
      ul.replaceChildren(...results.map((r) => {
        const el = li(r.name); el.onclick = () => { setPin(r.lat, r.lon, { pan: true }); ul.hidden = true; };
        el.append(miniBtn("＋", "Add as a stop", () => addStop(r.lat, r.lon, shortName(r.name))), miniBtn("★", "Save to favorites", () => saveFavorite(shortName(r.name), { lat: r.lat, lon: r.lon })));
        return el;
      }));
      setPin(results[0].lat, results[0].lon, { pan: true });
    } catch (e) { ul.hidden = true; fail(e); }
  });

  // ---------- favorites & recent ----------
  async function saveFavorite(name, where) {
    try { await api("POST", "/api/favorites", { name, ...where }); loadFavs(); toast(`Saved “${name}”.`); } catch (e) { fail(e); }
  }
  function rows(list, { star }) {
    return list.map((f) => {
      const el = li(f.name); el.title = `${fmt(f.lat)}, ${fmt(f.lon)}`;
      el.onclick = () => setPin(f.lat, f.lon, { pan: true });
      el.append(miniBtn("＋", "Add as a stop", () => addStop(f.lat, f.lon, f.name)));
      if (star) el.append(miniBtn("★", "Save to favorites", () => saveFavorite(f.name, { lat: f.lat, lon: f.lon })));
      else el.append(miniBtn("✕", `Delete ${f.name}`, async () => { try { await api("DELETE", `/api/favorites/${f.id}`); loadFavs(); } catch (x) { fail(x); } }));
      return el;
    });
  }
  async function loadFavs() {
    try { const { favorites } = await api("GET", "/api/favorites"); $("favs").replaceChildren(...(favorites.length ? rows(favorites, { star: false }) : [li("No favorites yet.")])); } catch (e) { fail(e); }
  }
  async function loadRecent() {
    try { const { recent } = await api("GET", "/api/recent"); $("recent").replaceChildren(...(recent.length ? rows(recent, { star: true }) : [li("Nothing yet.")])); } catch (e) { fail(e); }
  }
  $("clearRecent").onclick = async () => { try { await api("DELETE", "/api/recent"); loadRecent(); } catch (e) { fail(e); } };
  $("favForm").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    try {
      const name = $("favName").value.trim();
      if (!name) throw { message: "Type a name for this favorite." };
      const where = $("favWhere").value.trim();
      let loc;
      const m = where.match(/^(-?\d+(?:\.\d+)?)\s*[, ]\s*(-?\d+(?:\.\d+)?)$/);
      if (!where) loc = readInputs();                          // blank -> the current pin
      else if (m) loc = { lat: parseFloat(m[1]), lon: parseFloat(m[2]) };
      else loc = { query: where };                             // a place name, looked up on the server
      await api("POST", "/api/favorites", { name, ...loc });
      $("favName").value = ""; $("favWhere").value = ""; loadFavs(); toast(`Saved “${name}”.`);
    } catch (e) { fail(e); }
  });

  // ---------- status polling ----------
  function walkText(w) {
    if (w.error) return `Stopped: ${w.error.message}`;
    const tot = w.kind === "trick_or_treat", label = tot ? "House" : "Stop", icon = tot ? "🎃 " : "";
    const where = w.total > 1 ? `${label} ${w.stop}/${w.total}` : "";
    if (w.active && w.state === "waiting") return `${icon}${tot ? "At the door" : "Paused"}${where ? ` (${where})` : ""} — moving on in ${fmtSecs(w.wait_left_s)}`;
    if (w.active) return `${icon}${tot ? "Walking" : "Moving"} at ${w.speed_kmh.toFixed(1)} km/h${where ? ` → ${where}` : ""} — ${fmtDist(w.remaining_m)} to go${w.loop ? " · looping" : ""}`;
    return w.state === "done" ? "Finished." : "";
  }
  function render(s) {
    status = s;
    const chip = $("device"), banner = $("banner");
    chip.dataset.state = s.state;
    if (s.state === "ready") {
      $("deviceText").textContent = `${s.device.name} · iOS ${s.device.ios_version}`;
      chip.title = `Method: ${s.device.method}`; banner.hidden = true;
    } else {
      $("deviceText").textContent = s.error?.message || "Not ready"; chip.title = "";
      $("bannerMsg").textContent = s.error?.message || ""; $("bannerHint").textContent = s.error?.hint || "";
      $("trustBtn").hidden = s.state !== "not_trusted"; banner.hidden = false;
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
    $("walkState").textContent = walkText(w);
    if (w.active && w.kind === "trick_or_treat" && !$("totOn").checked) { $("totOn").checked = true; $("totOn").dispatchEvent(new Event("change")); }
    if (!w.active) clearRoute();
    else if (!routeFetched) { routeFetched = true; api("GET", "/api/journey/route").then((r) => { if (!routeLayer && r.route.length) drawRoute(r.route); }).catch(() => {}); }
  }
  async function refresh() {
    try { render(await api("GET", "/api/status")); }
    catch (e) { render({ state: "server_down", error: e, device: null, simulation: { active: false }, walk: {} }); }
  }
  (function loop() { refresh().finally(() => setTimeout(loop, status?.walk?.active ? 1000 : 3000)); })();
  loadFavs(); loadRecent(); renderStops();
})();
