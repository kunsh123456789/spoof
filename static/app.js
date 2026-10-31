(() => {
  const $ = (id) => document.getElementById(id);
  if (typeof L === "undefined") { document.body.insertAdjacentHTML("afterbegin", "<p style='padding:24px'>The map library failed to load (static/vendor/leaflet missing?).</p>"); return; }
  const store = {
    get(k) { try { return localStorage.getItem(k); } catch { return null; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch { /* private mode etc. */ } },
  };
  const map = L.map("map", { zoomControl: false }).setView([20, 0], 2);
  L.control.zoom({ position: "topright" }).addTo(map);
  // Tiles come through the local server: saved offline copies first, OpenStreetMap live otherwise.
  L.tileLayer("/tiles/{z}/{x}/{y}.png", {
    maxZoom: 19, attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
  }).addTo(map);

  let pin = null, simMarker = null, status = null, busy = false, toastTimer = null, centeredOnSim = false;
  let mode = "walk", kind = "single", tab = "place", stops = [], houses = [], landed = false, area = null, draft = [], drawing = false;
  let routeLayer = null, routeFetched = false;
  const stopLayer = L.layerGroup().addTo(map), houseLayer = L.layerGroup().addTo(map), areaLayer = L.layerGroup().addTo(map);
  const accent = () => getComputedStyle(document.documentElement).getPropertyValue("--accent").trim() || "#0a84ff";

  // ---------- helpers ----------
  async function api(method, path, body) {
    let res;
    try {
      res = await fetch(path, { method, headers: body ? { "Content-Type": "application/json" } : {}, body: body ? JSON.stringify(body) : undefined });
    } catch (e) {
      throw { message: "Cannot reach the local server.", hint: "Is the app still running in Terminal?" };
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
  const fmt4 = (n) => Number(n).toFixed(4);
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

  // ---------- tabs, theme, how-to boxes ----------
  function showTab(name) {
    tab = name;
    document.querySelectorAll("#tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
    document.querySelectorAll(".tabpanel").forEach((p) => (p.hidden = p.id !== `tab-${name}`));
    if (name === "tot") { stopLayer.remove(); areaLayer.addTo(map); } else { stopLayer.addTo(map); areaLayer.remove(); if (drawing) cancelDraw(); }
    store.set("tab", name); updateTheme();
    if (name === "offline") { loadOffline(); scheduleEstimate(); }
  }
  document.querySelectorAll("#tabs button").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab)));
  function updateTheme() {
    const w = status?.walk, on = tab === "tot" || (w?.active && w.kind === "trick_or_treat");
    document.documentElement.dataset.theme = on ? "halloween" : "";
    $("totBanner").hidden = !on;
    if (routeLayer) routeLayer.setStyle({ color: accent() });
    if (typeof renderArea === "function") renderArea();
  }
  document.querySelectorAll("details.howto").forEach((d) => {          // remember which instruction boxes you closed
    if (store.get(`how-${d.id}`) === "closed") d.open = false;
    d.addEventListener("toggle", () => store.set(`how-${d.id}`, d.open ? "open" : "closed"));
  });

  // ---------- pin ----------
  function pinLine() {
    const t = pin ? `📍 Pin: ${fmt4(pin.getLatLng().lat)}, ${fmt4(pin.getLatLng().lng)}` : "📍 No pin yet — click the map to drop one.";
    document.querySelectorAll("[data-pinline]").forEach((e) => (e.textContent = t));
  }
  function setPin(lat, lon, { pan = false } = {}) {
    if (!pin) {
      pin = L.marker([lat, lon], { draggable: true }).addTo(map);
      pin.on("dragend", () => { const p = pin.getLatLng(); fillInputs(p.lat, p.lng); });
    } else pin.setLatLng([lat, lon]);
    fillInputs(lat, lon);
    if (pan) map.setView([lat, lon], Math.max(map.getZoom(), 15));
  }
  function fillInputs(lat, lon) { $("lat").value = fmt(lat); $("lon").value = fmt(lon); pinLine(); }
  function readInputs() {
    const lat = parseFloat($("lat").value), lon = parseFloat($("lon").value);
    if (!Number.isFinite(lat) || !Number.isFinite(lon)) throw { message: "Drop a pin first.", hint: "Click the map, or type a latitude and longitude." };
    if (Math.abs(lat) > 90 || Math.abs(lon) > 180) throw { message: "Latitude must be within ±90 and longitude within ±180." };
    return { lat, lon };
  }
  map.on("click", (e) => (drawing ? addVertex(e.latlng.wrap()) : setPin(e.latlng.lat, e.latlng.lng)));
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
    toast(`Location set to ${fmt4(c.lat)}, ${fmt4(c.lon)}. Check Apple Maps on the phone.`); loadRecent();
  });
  $("clearBtn").onclick = (ev) => guarded(ev.currentTarget, async () => {
    await api("DELETE", "/api/location"); centeredOnSim = false; clearRoute(); toast("Simulation stopped — real GPS restored.");
  });
  $("stopBtn").onclick = (ev) => guarded(ev.currentTarget, async () => { await api("DELETE", "/api/journey"); clearRoute(); toast("Stopped. The phone stays at its current spot."); });
  $("trustBtn").onclick = (ev) => guarded(ev.currentTarget, async () => {
    toast("Look at your iPhone and tap Trust (enter your passcode if asked)…", 8000);
    await api("POST", "/api/trust");
  });

  // path type, destination kind, speed
  document.querySelectorAll(".segbtn[data-mode]").forEach((b) => b.addEventListener("click", () => {
    mode = b.dataset.mode;
    document.querySelectorAll(".segbtn[data-mode]").forEach((x) => { x.classList.toggle("active", x === b); x.setAttribute("aria-checked", x === b); });
    if (mode === "walk") setSpeed(5); else if (mode === "drive") setSpeed(50);
  }));
  document.querySelectorAll(".kindbtn").forEach((b) => b.addEventListener("click", () => {
    kind = b.dataset.kind;
    document.querySelectorAll(".kindbtn").forEach((x) => { x.classList.toggle("active", x === b); x.setAttribute("aria-checked", x === b); });
    $("kindSingle").hidden = kind !== "single"; $("kindMulti").hidden = kind !== "multi";
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
    await startJourney({ stops: [c], mode, speed_kmh: Number($("speed").value) },
      mode === "straight" ? "Moving in a straight line…" : `Following the ${mode === "walk" ? "walking" : "driving"} route…`);
  });

  // ---------- route drawing ----------
  function drawRoute(points) {
    clearRoute(false);
    if (!points || points.length < 2) return;
    routeLayer = L.polyline(points, { color: accent(), weight: 5, opacity: 0.8 }).addTo(map);
    routeFetched = true;
    map.fitBounds(routeLayer.getBounds().pad(0.2), { maxZoom: 17 });
  }
  function clearRoute(resetFlag = true) { if (routeLayer) { routeLayer.remove(); routeLayer = null; } if (resetFlag) routeFetched = false; }

  // ---------- multi-stop ----------
  function renderStops() {
    stopLayer.clearLayers();
    $("stops").replaceChildren(...(stops.length ? stops.map((s, i) => {
      const el = li(`${i + 1}.  ${s.label || `${fmt4(s.lat)}, ${fmt4(s.lon)}`}`);
      el.append(
        miniBtn("↑", "Move up", () => { if (i) { [stops[i - 1], stops[i]] = [stops[i], stops[i - 1]]; renderStops(); } }),
        miniBtn("↓", "Move down", () => { if (i < stops.length - 1) { [stops[i + 1], stops[i]] = [stops[i], stops[i + 1]]; renderStops(); } }),
        miniBtn("✕", "Remove stop", () => { stops.splice(i, 1); renderStops(); }),
      );
      L.marker([s.lat, s.lon], { icon: L.divIcon({ className: "", html: `<div class="num-marker">${i + 1}</div>`, iconSize: [24, 24] }), interactive: false }).addTo(stopLayer);
      return el;
    }) : [li("No stops yet.")]));
    if (stops.length >= 2) L.polyline(stops.map((s) => [s.lat, s.lon]), { color: "#888", weight: 2, dashArray: "4 6", interactive: false }).addTo(stopLayer);
  }
  function addStop(lat, lon, label) {
    if (stops.length >= 59) return toast("That's the maximum number of stops.");
    stops.push({ lat, lon, label }); renderStops(); toast(`Stop ${stops.length} added — see the Move tab.`);
  }
  $("addStopBtn").onclick = () => { try { const c = readInputs(); addStop(c.lat, c.lon); } catch (e) { fail(e); } };
  $("clearStopsBtn").onclick = () => { stops = []; renderStops(); };
  $("routeBtn").onclick = (ev) => guarded(ev.currentTarget, async () => {
    if (!stops.length) throw { message: "Add at least one stop first.", hint: "Drop a pin, then press “Add pin as stop”." };
    const dwell = Math.max(0, Number($("dwell").value) || 0);
    await startJourney({
      stops: stops.map((s) => ({ lat: s.lat, lon: s.lon, dwell_s: dwell })), mode, speed_kmh: Number($("speed").value), loop: $("loop").checked,
    }, `Route started (${stops.length} stop${stops.length > 1 ? "s" : ""}).`);
  });

  // ---------- trick or treat ----------
  const TOT_SPEED = 4.5, FALLBACK_RADIUS = 350, MAX_AREA_KM2 = 4;
  $("houses").addEventListener("input", () => ($("housesOut").textContent = $("houses").value));
  function clearHouses() { houses = []; houseLayer.clearLayers(); $("totInfo").hidden = true; $("totStartBtn").disabled = true; }
  function areaKm2(poly) {                                  // same equirectangular shoelace as the server
    const lat0 = poly.reduce((a, p) => a + p[0], 0) / poly.length * Math.PI / 180, k = Math.PI / 180 * 6371008.8;
    const pts = poly.map(([la, lo]) => [lo * Math.cos(lat0) * k, la * k]);
    let sum = 0; pts.forEach((p, i) => { const q = pts[(i + 1) % pts.length]; sum += p[0] * q[1] - q[0] * p[1]; });
    return Math.abs(sum) / 2 / 1e6;
  }
  function areaText() {
    if (drawing) return `✏️ Drawing: click points around the neighborhood (${draft.length} so far). Click the first point or press Finish to close the shape.`;
    if (area) { const km = areaKm2(area); return `✅ Area drawn: ${km < 0.1 ? `${Math.round(km * 1e6).toLocaleString()} m²` : `${km.toFixed(2)} km²`} · ${area.length} points.${km > MAX_AREA_KM2 ? ` ⚠️ Too big — max ${MAX_AREA_KM2} km².` : ""}`; }
    return `No area drawn — houses will be searched ${FALLBACK_RADIUS} m around the pin. Press “Draw area” to outline a neighborhood instead.`;
  }
  function renderArea() {
    areaLayer.clearLayers();
    const col = accent();
    if (drawing) {
      if (draft.length >= 2) L.polyline(draft, { color: col, weight: 3, dashArray: "6 6", interactive: false }).addTo(areaLayer);
      draft.forEach((p, i) => L.circleMarker(p, { radius: i === 0 ? 8 : 5, color: "#fff", weight: 2, fillColor: col, fillOpacity: 1, interactive: false }).addTo(areaLayer));
    } else if (area) L.polygon(area, { color: col, weight: 3, fillColor: col, fillOpacity: 0.16, interactive: false }).addTo(areaLayer);
    $("areaInfo").textContent = areaText();
    $("drawIdle").hidden = drawing; $("drawControls").hidden = !drawing;
    $("clearAreaBtn").hidden = !area; $("drawBtn").lastChild.textContent = area ? " Redraw area" : " Draw area";
    $("finishBtn").disabled = draft.length < 3;
  }
  function startDraw() {
    drawing = true; draft = []; area = null; clearHouses();
    map.getContainer().style.cursor = "crosshair"; map.doubleClickZoom.disable(); renderArea();
  }
  function endDraw() { drawing = false; map.getContainer().style.cursor = ""; map.doubleClickZoom.enable(); }
  function finishDraw() {
    if (draft.length < 3) return toast("Click at least 3 points to outline an area.");
    area = draft.slice(); draft = []; endDraw(); renderArea();
    map.fitBounds(L.latLngBounds(area).pad(0.2), { maxZoom: 18 });
    if (areaKm2(area) > MAX_AREA_KM2) toast(`That area is too big. Redraw a smaller one (max ${MAX_AREA_KM2} km²).`, 6000);
  }
  function cancelDraw() { draft = []; endDraw(); renderArea(); }
  function addVertex(ll) {
    const p = [ll.lat, ll.lng];
    if (draft.length >= 3) {                               // clicking near the first point closes the shape
      const a = map.latLngToContainerPoint(draft[0]), b = map.latLngToContainerPoint(ll);
      if (a.distanceTo(b) < 14) return finishDraw();
    }
    if (draft.length >= 100) return toast("Maximum 100 points — press Finish.");
    draft.push(p); renderArea();
  }
  $("drawBtn").onclick = startDraw;
  $("finishBtn").onclick = finishDraw;
  $("undoBtn").onclick = () => { draft.pop(); renderArea(); };
  $("cancelDrawBtn").onclick = cancelDraw;
  $("clearAreaBtn").onclick = () => { area = null; clearHouses(); renderArea(); };
  document.addEventListener("keydown", (e) => {
    if (!drawing || /INPUT|TEXTAREA/.test(document.activeElement?.tagName)) return;
    if (e.key === "Escape") cancelDraw(); else if (e.key === "Enter") finishDraw();
    else if ((e.key === "Backspace" || e.key === "z") && draft.length) { e.preventDefault(); draft.pop(); renderArea(); }
  });

  $("findBtn").onclick = (ev) => guarded(ev.currentTarget, async () => {
    if (drawing) throw { message: "Finish drawing your area first.", hint: "Click the first point or press Finish." };
    const body = { houses: Number($("houses").value), radius_m: FALLBACK_RADIUS };
    if (area) { body.polygon = area; if (Number.isFinite(parseFloat($("lat").value))) Object.assign(body, readInputs()); }
    else Object.assign(body, readInputs());                  // no area: needs the pin
    clearHouses();
    $("totInfo").hidden = false; $("totInfo").textContent = "Looking for houses on OpenStreetMap…";
    let source = "online";
    try { const res = await api("POST", "/api/trick-or-treat/plan", body); houses = res.houses; source = res.source; }
    catch (e) { $("totInfo").hidden = true; throw e; }
    houses.forEach((h, i) => L.marker([h.lat, h.lon], { icon: L.divIcon({ className: "", html: `<div class="pumpkin">🎃</div>`, iconSize: [22, 22] }), title: `House ${i + 1}`, interactive: false }).addTo(houseLayer));
    L.polyline(houses.map((h) => [h.lat, h.lon]), { color: "#ff7a00", weight: 2, dashArray: "3 6", interactive: false }).addTo(houseLayer);
    map.fitBounds(L.latLngBounds(houses.map((h) => [h.lat, h.lon])).pad(0.25), { maxZoom: 18 });
    let dist = 0; for (let i = 1; i < houses.length; i++) dist += haversine([houses[i - 1].lat, houses[i - 1].lon], [houses[i].lat, houses[i].lon]);
    const secs = dist / (TOT_SPEED / 3.6) * 1.3 + houses.reduce((a, h) => a + h.dwell_s, 0);
    $("totInfo").textContent = `🎃 ${houses.length} houses · about ${fmtDist(dist)} of walking · roughly ${Math.round(secs / 60)} min. Press “3 · Start” when ready.${source === "offline" ? " 📡 (Using saved offline houses.)" : ""}`;
    $("totStartBtn").disabled = false;
  });
  $("totStartBtn").onclick = (ev) => guarded(ev.currentTarget, async () => {
    if (!houses.length) throw { message: "Find houses first.", hint: "Press “2 · Find houses”." };
    await startJourney({
      stops: houses.map((h) => ({ lat: h.lat, lon: h.lon, dwell_s: h.dwell_s })), mode: "walk", speed_kmh: TOT_SPEED,
      jump_to_first: true, kind: "trick_or_treat", speed_jitter: 0.2,
    }, "Happy Halloween! 🎃 Heading to the first house…");
  });

  // ---------- offline areas ----------
  const packLayer = L.layerGroup().addTo(map);
  let offTimer = null, offPoll = null, offBusy = false;
  const mb = (b) => (b / 1048576).toFixed(1);
  function viewBBox() { const b = map.getBounds(); return { south: b.getSouth(), west: b.getWest(), north: b.getNorth(), east: b.getEast() }; }
  function scheduleEstimate() { clearTimeout(offTimer); offTimer = setTimeout(updateEstimate, 400); }
  async function updateEstimate() {
    if (tab !== "offline" || offPoll) return;
    try {
      const e = await api("POST", "/api/offline/estimate", { name: "x", bbox: viewBBox() });
      $("offEstimate").textContent = `📐 ${e.area_km2} km² on screen · ${e.tiles} map tiles · about ${e.est_mb} MB`; $("offBtn").disabled = false;
    } catch (e) { $("offEstimate").textContent = `⚠️ ${e.message} ${e.hint || ""}`; $("offBtn").disabled = true; }
  }
  map.on("moveend", () => { if (tab === "offline") scheduleEstimate(); });
  async function loadOffline() {
    try {
      const { packs, job } = await api("GET", "/api/offline");
      packLayer.clearLayers();
      packs.forEach((p) => {
        const [s, w, n, e] = p.bbox;
        L.rectangle([[s, w], [n, e]], { color: accent(), weight: 2, dashArray: "6 6", fill: false, className: "pack-rect" }).bindTooltip(`⬇ ${p.name}`, { sticky: true }).addTo(packLayer);
      });
      $("offList").replaceChildren(...(packs.length ? packs.map((p) => {
        const el = li(`${p.name} · ${mb(p.bytes)} MB · ${p.houses} houses`);
        el.title = "Click to show on the map"; el.onclick = () => map.fitBounds([[p.bbox[0], p.bbox[1]], [p.bbox[2], p.bbox[3]]]);
        el.append(miniBtn("✕", `Delete ${p.name}`, async () => {
          if (!confirm(`Delete the offline area “${p.name}”?`)) return;
          try { await api("DELETE", `/api/offline/${p.id}`); loadOffline(); } catch (x) { fail(x); }
        }));
        return el;
      }) : [li("No offline areas yet.")]));
      showJob(job);
    } catch (e) { fail(e); }
  }
  function showJob(job) {
    const box = $("offProgress");
    if (job.state === "running") {
      box.hidden = false; $("offBtn").disabled = true;
      $("offBar").style.width = job.stage === "map tiles" && job.total ? `${Math.round((job.done / job.total) * 100)}%` : "5%";
      $("offStage").textContent = job.stage === "map tiles" ? `Downloading map tiles… ${job.done}/${job.total}` : `Downloading ${job.stage}…`;
      if (!offPoll) offPoll = setInterval(loadOffline, 1000);
      return;
    }
    if (offPoll) { clearInterval(offPoll); offPoll = null; }
    if (job.state === "done" && offBusy) { offBusy = false; box.hidden = true; $("offBtn").disabled = false; toast(`“${job.name}” saved. It will work without internet.`, 6000); scheduleEstimate(); }
    else if (job.state === "error" && offBusy) { offBusy = false; box.hidden = true; $("offBtn").disabled = false; fail(job.error); }
    else if (!offBusy) box.hidden = true;
  }
  $("offBtn").onclick = (ev) => guarded(ev.currentTarget, async () => {
    const name = $("offName").value.trim();
    if (!name) throw { message: "Give this area a name.", hint: "For example “Maple Street”." };
    await api("POST", "/api/offline", { name, bbox: viewBBox() });
    offBusy = true; $("offName").value = ""; loadOffline();
  });
  window.addEventListener("online", () => ($("netNote").hidden = true));
  window.addEventListener("offline", () => ($("netNote").hidden = false));
  if (!navigator.onLine) $("netNote").hidden = false;

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
    try { await api("POST", "/api/favorites", { name, ...where }); loadFavs(); toast(`Saved “${name}” to Favorites.`); } catch (e) { fail(e); }
  }
  function rows(list, { star }) {
    return list.map((f) => {
      const el = li(f.name); el.title = `${fmt(f.lat)}, ${fmt(f.lon)}`;
      el.onclick = () => { setPin(f.lat, f.lon, { pan: true }); toast("Pin moved. Go to the Place tab and press Set location."); };
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
      const m = where.match(/^(-?\d+(?:\.\d+)?)\s*[, ]\s*(-?\d+(?:\.\d+)?)$/);
      let loc;
      if (!where) loc = readInputs();                          // blank -> the current pin
      else if (m) loc = { lat: parseFloat(m[1]), lon: parseFloat(m[2]) };
      else loc = { query: where };                             // a place name, looked up on the server
      await api("POST", "/api/favorites", { name, ...loc });
      $("favName").value = ""; $("favWhere").value = ""; loadFavs(); toast(`Saved “${name}”.`);
    } catch (e) { fail(e); }
  });

  // ---------- status polling + bottom bar ----------
  function nowBar(s) {
    const w = s.walk || {}, sim = s.simulation, tot = w.kind === "trick_or_treat";
    const where = w.total > 1 ? ` ${w.stop}/${w.total}` : "";
    let state = "idle", title = "Real GPS", detail = "Your phone is using its real location.";
    if (w.error) { state = "simulating"; title = "Stopped"; detail = w.error.message + (w.error.hint ? ` ${w.error.hint}` : ""); }
    else if (w.active && w.state === "waiting") {
      state = "moving"; title = tot ? `🎃 At the door — house${where}` : `Paused at stop${where}`; detail = `Moving on in ${fmtSecs(w.wait_left_s)}.`;
    } else if (w.active) {
      state = "moving"; title = tot ? `🎃 Walking to house${where}` : `Moving${where ? ` to stop${where}` : ""}`;
      detail = `${w.speed_kmh.toFixed(1)} km/h · ${fmtDist(w.remaining_m)} to go${w.loop ? " · looping" : ""}`;
    } else if (sim.active) {
      state = "simulating"; title = w.state === "done" ? "Finished — simulating" : "Simulating";
      detail = `${fmt4(sim.lat)}, ${fmt4(sim.lon)}`;
    }
    $("now").dataset.state = state; $("nowTitle").textContent = title; $("nowDetail").textContent = detail;
    $("stopBtn").hidden = !w.active; $("clearBtn").hidden = !sim.active;
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
    if (sim.active) {
      const icon = L.divIcon({ className: "", html: '<div class="sim-marker"></div>', iconSize: [18, 18] });
      if (!simMarker) simMarker = L.marker([sim.lat, sim.lon], { icon, interactive: false, zIndexOffset: -100 }).addTo(map);
      else simMarker.setLatLng([sim.lat, sim.lon]);
      if (!centeredOnSim) { centeredOnSim = true; if (!pin) map.setView([sim.lat, sim.lon], 13); }
    } else if (simMarker) { simMarker.remove(); simMarker = null; }
    nowBar(s);
    const w = s.walk || {};
    if (!landed) { landed = true; if (w.active && w.kind === "trick_or_treat") showTab("tot"); }   // reopened mid-trip: land on the right tab, once
    updateTheme();
    if (!w.active) clearRoute();
    else if (!routeFetched) { routeFetched = true; api("GET", "/api/journey/route").then((r) => { if (!routeLayer && r.route.length) drawRoute(r.route); }).catch(() => {}); }
  }
  async function refresh() {
    try { render(await api("GET", "/api/status")); }
    catch (e) { render({ state: "server_down", error: e, device: null, simulation: { active: false }, walk: {} }); }
  }
  (function loop() { refresh().finally(() => setTimeout(loop, status?.walk?.active ? 1000 : 3000)); })();

  showTab(["place", "move", "tot", "saved", "offline"].includes(store.get("tab")) ? store.get("tab") : "place");
  pinLine(); loadFavs(); loadRecent(); renderStops(); renderArea();
})();
