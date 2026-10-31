(() => {
  const $ = (id) => document.getElementById(id);
  if (typeof L === "undefined") { document.body.insertAdjacentHTML("afterbegin", "<p style='padding:24px'>The map library failed to load (static/vendor/leaflet missing?).</p>"); return; }
  const store = {
    get(k) { try { return localStorage.getItem(k); } catch { return null; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch { /* private mode etc. */ } },
  };
  // Links to other sites (e.g. the map credits) must open in the system browser: in the desktop window a plain
  // link would navigate away and replace the app.
  document.addEventListener("click", (e) => {
    const a = e.target.closest ? e.target.closest("a[href]") : null;
    if (!a) return;
    let u; try { u = new URL(a.href, location.href); } catch { return; }
    if ((u.protocol === "http:" || u.protocol === "https:") && u.origin !== location.origin) {
      e.preventDefault();
      if (window.pywebview && window.pywebview.api && window.pywebview.api.open_external) window.pywebview.api.open_external(u.href);
      else window.open(u.href, "_blank", "noopener");
    }
  });

  const map = L.map("map", { zoomControl: false }).setView([20, 0], 2);
  L.control.zoom({ position: "topright" }).addTo(map);
  // Tiles come through the local server: saved offline copies first, OpenStreetMap live otherwise.
  L.tileLayer("/tiles/{z}/{x}/{y}.png", {
    maxZoom: 19, attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
  }).addTo(map);

  let pin = null, simMarker = null, status = null, busy = false, toastTimer = null, centeredOnSim = false;
  let mode = "walk", kind = "single", tab = "place", stops = [], houses = [], pace = { speed_kmh: 4.5, speed_jitter: 0.2 };
  let landed = false, area = null, draft = [], drawing = false, gpx = null, device = store.get("device") || "", devices = [];
  let routeLayer = null, routeFetched = false, settings = {};
  const stopLayer = L.layerGroup().addTo(map), houseLayer = L.layerGroup().addTo(map), areaLayer = L.layerGroup().addTo(map);
  const gpxLayer = L.layerGroup().addTo(map), otherLayer = L.layerGroup().addTo(map);
  const accent = () => getComputedStyle(document.documentElement).getPropertyValue("--accent").trim() || "#0a84ff";

  // ---------- helpers ----------
  async function api(method, path, body, opts = {}) {
    const headers = {};
    if (body !== undefined) headers["Content-Type"] = opts.raw ? "application/gpx+xml" : "application/json";
    if (device) headers["X-Device"] = device;                       // which phone this request is about
    let res;
    try {
      res = await fetch(path, { method, headers, body: body === undefined ? undefined : (opts.raw ? body : JSON.stringify(body)) });
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
  const clock = (s) => `${Math.floor(s / 60)}:${String(Math.round(s % 60)).padStart(2, "0")}`;
  const normUdid = (u) => String(u || "").replace(/-/g, "").toLowerCase();
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
  const currentSpeed = () => Number($("speed").value);

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

  // ---------- settings (the on/off switches) ----------
  const SETTING_NEEDS_REPLAN = new Set(["tot_kids", "tot_avoid_busy", "tot_realistic"]);
  function applySettings() {
    document.querySelectorAll("[data-setting]").forEach((cb) => (cb.checked = !!settings[cb.dataset.setting]));
  }
  document.querySelectorAll("[data-setting]").forEach((cb) => cb.addEventListener("change", async () => {
    const key = cb.dataset.setting;
    try {
      settings = await api("PATCH", "/api/settings", { [key]: cb.checked });
      if (SETTING_NEEDS_REPLAN.has(key) && houses.length) { clearHouses(); toast("Option changed — press “Find houses” again to apply it."); }
    } catch (e) { cb.checked = !cb.checked; fail(e); }
    if (status) nowBar(status);
  }));

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

  // ---------- phones & connection ----------
  let devicesLoaded = false; const knownNames = {};
  async function loadDevices(fresh = false) {
    try { devices = (await api("GET", `/api/devices${fresh ? "?fresh=true" : ""}`)).devices; } catch { return; }
    devices.forEach((d) => (knownNames[normUdid(d.udid)] = d.name));
    const present = devices.some((d) => normUdid(d.udid) === normUdid(device));
    // Pick a phone on first load (or when none is chosen yet) but never switch away mid-session: an unplugged
    // phone must stay selected so its reconnect state is visible and commands never go to the wrong phone.
    if ((!devicesLoaded || !device) && !present && devices.length) { device = devices[0].udid; store.set("device", device); }
    devicesLoaded = true;
    renderPhones();
  }
  function renderPhones() {
    const n = devices.length;
    $("phoneCount").hidden = n < 2; $("phoneCount").textContent = n;
    const missing = device && !devices.some((d) => normUdid(d.udid) === normUdid(device));
    const gone = missing ? [(() => { const el = li(`✓ ${knownNames[normUdid(device)] || "Selected phone"} · not connected`); el.querySelector(".name").classList.add("phone-sel"); el.title = "Waiting for this phone to come back"; return el; })()] : [];
    $("phoneList").replaceChildren(...gone, ...(n ? devices.map((d) => {
      const sel = normUdid(d.udid) === normUdid(device);
      const el = li(`${sel ? "✓ " : ""}${d.name}${d.ios_version ? ` · iOS ${d.ios_version}` : ""}${d.paired ? "" : " · not trusted yet"}${d.simulating ? " · ● simulating" : ""}`);
      if (sel) el.querySelector(".name").classList.add("phone-sel");
      const badge = document.createElement("span"); badge.className = "badge"; badge.textContent = d.connection === "USB" ? "🔌 USB" : "📶 Wi-Fi";
      el.append(badge); el.title = "Click to control this phone";
      el.onclick = () => selectDevice(d.udid);
      return el;
    }) : (missing ? [] : [li("No phones found. Plug one in, or see Wi-Fi setup below.")])));
    otherLayer.clearLayers();
    devices.filter((d) => d.simulating && normUdid(d.udid) !== normUdid(device)).forEach((d) =>
      L.marker([d.lat, d.lon], { icon: L.divIcon({ className: "", html: '<div class="other-phone"></div>', iconSize: [14, 14] }), interactive: false })
        .bindTooltip(d.name, { permanent: false }).addTo(otherLayer));
  }
  function selectDevice(udid) {
    if (normUdid(udid) === normUdid(device)) return;
    device = udid; store.set("device", udid);
    status = null; centeredOnSim = false; clearRoute(); if (simMarker) { simMarker.remove(); simMarker = null; }
    renderPhones(); refresh(); toast("Now controlling this phone.");
  }
  $("device").onclick = () => { $("phonesBox").open = !$("phonesBox").open; };
  $("scanBtn").onclick = (ev) => guarded(ev.currentTarget, async () => {
    toast("Searching the Wi-Fi network… (about 5 seconds)", 6000);
    devices = (await api("POST", "/api/devices/scan")).devices; renderPhones();
    toast(devices.some((d) => d.connection !== "USB") ? "Found a phone on Wi-Fi." : "No phone found on Wi-Fi. Check the setup steps below.", 6000);
  });
  $("wifiSwitch").addEventListener("change", async () => {
    const cb = $("wifiSwitch"), want = cb.checked;
    try { await api("POST", "/api/wifi", { enabled: want }); toast(want ? "Wi-Fi connections allowed. You can unplug the cable now." : "Wi-Fi connections turned off."); }
    catch (e) { cb.checked = !want; fail(e); }
    refresh();
  });
  setInterval(() => loadDevices(false), 5000);

  // ---------- path type, destination kind, speed ----------
  document.querySelectorAll(".segbtn[data-mode]").forEach((b) => b.addEventListener("click", () => {
    mode = b.dataset.mode;
    document.querySelectorAll(".segbtn[data-mode]").forEach((x) => { x.classList.toggle("active", x === b); x.setAttribute("aria-checked", x === b); });
    if (mode === "walk") setSpeed(5); else if (mode === "drive") setSpeed(50);
  }));
  function setKind(k) {
    kind = k;
    document.querySelectorAll(".kindbtn").forEach((x) => { x.classList.toggle("active", x.dataset.kind === k); x.setAttribute("aria-checked", x.dataset.kind === k); });
    $("kindSingle").hidden = k !== "single"; $("kindMulti").hidden = k !== "multi"; $("kindGpx").hidden = k !== "gpx";
  }
  document.querySelectorAll(".kindbtn").forEach((b) => b.addEventListener("click", () => setKind(b.dataset.kind)));
  function setMode(m) { document.querySelector(`.segbtn[data-mode="${m}"]`)?.classList.add("active"); document.querySelectorAll(".segbtn[data-mode]").forEach((x) => x.classList.toggle("active", x.dataset.mode === m)); mode = m; }
  function setSpeed(v) { $("speed").value = v; $("speed").dispatchEvent(new Event("input")); $("speed").dispatchEvent(new Event("change")); }
  $("speed").addEventListener("input", () => { $("speedOut").textContent = $("speed").value; });
  $("speed").addEventListener("change", async () => {
    if (status?.walk?.active && status.walk.kind !== "trick_or_treat")
      try { await api("PATCH", "/api/journey", { speed_kmh: currentSpeed() }); } catch (e) { fail(e); }
  });
  document.querySelectorAll(".chipbtn").forEach((b) => b.addEventListener("click", () => setSpeed(b.dataset.speed)));

  async function startJourney(body, okMsg) {
    const res = await api("POST", "/api/journey", { return_to_start: $("returnTrip").checked, ...body });
    drawRoute(res.route); toast(okMsg);
  }
  $("walkBtn").onclick = (ev) => guarded(ev.currentTarget, async () => {
    const c = readInputs();
    await startJourney({ stops: [c], mode, speed_kmh: currentSpeed() },
      mode === "straight" ? "Moving in a straight line…" : `Following the ${mode === "walk" ? "walking" : "driving"} route…`);
  });
  $("returnBtn").onclick = (ev) => guarded(ev.currentTarget, async () => {
    const res = await api("POST", "/api/journey/return"); drawRoute(res.route); toast("Heading back to the start…");
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

  // ---------- multi-stop route (each stop can have its own path type + speed) ----------
  function renderStops() {
    stopLayer.clearLayers();
    $("stops").replaceChildren(...(stops.length ? stops.map((s, i) => {
      const el = li(`${i + 1}.  ${s.label || `${fmt4(s.lat)}, ${fmt4(s.lon)}`}`);
      el.classList.add("stoprow");
      el.append(
        miniBtn("↑", "Move up", () => { if (i) { [stops[i - 1], stops[i]] = [stops[i], stops[i - 1]]; renderStops(); } }),
        miniBtn("↓", "Move down", () => { if (i < stops.length - 1) { [stops[i + 1], stops[i]] = [stops[i], stops[i + 1]]; renderStops(); } }),
        miniBtn("✕", "Remove stop", () => { stops.splice(i, 1); renderStops(); }),
      );
      const sched = document.createElement("div"); sched.className = "sched";
      const sel = document.createElement("select"); sel.title = "Path type for the leg that ends at this stop";
      [["", "Path: default"], ["straight", "Straight"], ["walk", "Walk"], ["drive", "Drive"]].forEach(([v, t]) => { const o = document.createElement("option"); o.value = v; o.textContent = t; sel.append(o); });
      sel.value = s.mode || ""; sel.onchange = () => (s.mode = sel.value);
      const sp = document.createElement("input"); sp.type = "number"; sp.min = 1; sp.max = 400; sp.placeholder = "km/h"; sp.value = s.speed || "";
      sp.title = "Speed for the leg that ends at this stop (blank = the speed slider)"; sp.oninput = () => (s.speed = sp.value);
      sched.append(sel, sp); el.append(sched);
      L.marker([s.lat, s.lon], { icon: L.divIcon({ className: "", html: `<div class="num-marker">${i + 1}</div>`, iconSize: [24, 24] }), interactive: false }).addTo(stopLayer);
      return el;
    }) : [li("No stops yet.")]));
    if (stops.length >= 2) L.polyline(stops.map((s) => [s.lat, s.lon]), { color: "#888", weight: 2, dashArray: "4 6", interactive: false }).addTo(stopLayer);
  }
  function addStop(lat, lon, label, extra = {}) {
    if (stops.length >= 59) return toast("That's the maximum number of stops.");
    stops.push({ lat, lon, label, ...extra }); renderStops(); toast(`Stop ${stops.length} added — see the Move tab.`);
  }
  $("addStopBtn").onclick = () => { try { const c = readInputs(); addStop(c.lat, c.lon); } catch (e) { fail(e); } };
  $("clearStopsBtn").onclick = () => { stops = []; renderStops(); };
  const stopPayload = (dwell) => stops.map((s) => ({ lat: s.lat, lon: s.lon, dwell_s: dwell, mode: s.mode || null, speed_kmh: s.speed ? Number(s.speed) : null, label: s.label || null }));
  $("routeBtn").onclick = (ev) => guarded(ev.currentTarget, async () => {
    if (!stops.length) throw { message: "Add at least one stop first.", hint: "Drop a pin, then press “Add pin as stop”." };
    const dwell = Math.max(0, Number($("dwell").value) || 0);
    await startJourney({ stops: stopPayload(dwell), mode, speed_kmh: currentSpeed(), loop: $("loop").checked },
      `Route started (${stops.length} stop${stops.length > 1 ? "s" : ""}).`);
  });
  $("saveRouteBtn").onclick = (ev) => guarded(ev.currentTarget, async () => {
    if (!stops.length) throw { message: "Add at least one stop first." };
    const name = $("routeName").value.trim();
    if (!name) throw { message: "Give the route a name.", hint: "Type it in the box next to Save." };
    await api("POST", "/api/routes", { name, kind: "stops", stops: stopPayload(Math.max(0, Number($("dwell").value) || 0)), mode,
      speed_kmh: currentSpeed(), loop: $("loop").checked, return_to_start: $("returnTrip").checked });
    $("routeName").value = ""; loadRoutes(); toast(`Saved “${name}” — find it in the Saved tab.`);
  });

  // ---------- GPX import ----------
  function showGpx() {
    gpxLayer.clearLayers();
    if (!gpx) { $("gpxInfo").hidden = true; $("gpxOpts").hidden = true; return; }
    L.polyline(gpx.points, { color: "#8b5cf6", weight: 4, opacity: 0.8, interactive: false }).addTo(gpxLayer);
    map.fitBounds(L.latLngBounds(gpx.points).pad(0.2), { maxZoom: 17 });
    $("gpxInfo").hidden = false; $("gpxOpts").hidden = false;
    $("gpxInfo").textContent = `📈 ${gpx.name || "Track"} · ${gpx.points.length.toLocaleString()} points · ${fmtDist(gpx.distance_m)}${gpx.avg_kmh ? ` · recorded pace ${gpx.avg_kmh} km/h` : ""}`;
    $("gpxPaceBtn").hidden = !gpx.avg_kmh;
    if (!$("gpxName").value) $("gpxName").value = gpx.name || "";
  }
  $("gpxFile").addEventListener("change", async () => {
    const file = $("gpxFile").files[0]; if (!file) return;
    try { gpx = await api("POST", "/api/gpx", await file.arrayBuffer(), { raw: true }); showGpx(); toast("GPX loaded."); }
    catch (e) { gpx = null; showGpx(); fail(e); }
  });
  $("gpxPaceBtn").onclick = () => { if (gpx?.avg_kmh) { setSpeed(Math.min(120, Math.max(1, Math.round(gpx.avg_kmh)))); toast(`Speed set to the recorded pace (${gpx.avg_kmh} km/h).`); } };
  const trackEnd = () => gpx.points[gpx.points.length - 1];
  $("gpxStartBtn").onclick = (ev) => guarded(ev.currentTarget, async () => {
    if (!gpx) throw { message: "Choose a GPX file first." };
    const jump = $("gpxJump").checked || !status?.simulation?.active;
    await startJourney({ stops: [], path: gpx.points, mode, speed_kmh: currentSpeed(), jump_to_first: jump }, "Playing the track…");
  });
  $("saveTrackBtn").onclick = (ev) => guarded(ev.currentTarget, async () => {
    if (!gpx) throw { message: "Choose a GPX file first." };
    const name = $("gpxName").value.trim();
    if (!name) throw { message: "Give the track a name." };
    const [lat, lon] = trackEnd();
    await api("POST", "/api/routes", { name, kind: "track", path: gpx.points, stops: [{ lat, lon }], mode, speed_kmh: currentSpeed(), return_to_start: $("returnTrip").checked });
    loadRoutes(); toast(`Saved “${name}” — find it in the Saved tab.`);
  });

  // ---------- saved routes ----------
  async function loadRoutes() {
    try {
      const { routes } = await api("GET", "/api/routes");
      $("routes").replaceChildren(...(routes.length ? routes.map((r) => {
        const what = r.kind === "track" ? `GPX track · ${r.path.length.toLocaleString()} points` : `${r.stops.length} stop${r.stops.length > 1 ? "s" : ""}`;
        const el = li(`${r.name} · ${what} · ${r.mode} ${r.speed_kmh} km/h${r.loop ? " · loop" : ""}`);
        el.title = "Click ✎ to edit, ▶ to run";
        el.append(
          miniBtn("▶", "Run this route", async () => {
            if (busy) return;
            try {
              const res = await api("POST", `/api/routes/${r.id}/start`, { jump_to_first: !status?.simulation?.active });
              drawRoute(res.route); toast(`Running “${r.name}”…`); refresh();
            } catch (e) { fail(e); }
          }),
          miniBtn("✎", "Load into the editor", () => loadRouteIntoEditor(r)),
          miniBtn("✕", `Delete ${r.name}`, async () => {
            if (!confirm(`Delete the saved route “${r.name}”?`)) return;
            try { await api("DELETE", `/api/routes/${r.id}`); loadRoutes(); } catch (x) { fail(x); }
          }),
        );
        return el;
      }) : [li("No saved routes yet. Save one from the Move tab.")]));
    } catch (e) { fail(e); }
  }
  function loadRouteIntoEditor(r) {
    setMode(r.mode); setSpeed(r.speed_kmh); $("returnTrip").checked = !!r.return_to_start; $("loop").checked = !!r.loop;
    if (r.kind === "track") {
      gpx = { points: r.path, name: r.name, distance_m: r.path.slice(1).reduce((a, p, i) => a + haversine(r.path[i], p), 0), avg_kmh: null };
      $("gpxName").value = r.name; setKind("gpx"); showGpx();
    } else {
      stops = r.stops.map((s) => ({ lat: s.lat, lon: s.lon, label: s.label || "", mode: s.mode || "", speed: s.speed_kmh || "" }));
      $("dwell").value = r.stops[0]?.dwell_s || 0; $("routeName").value = r.name; setKind("multi"); renderStops();
    }
    showTab("move"); toast(`Loaded “${r.name}”. Edit it, then Start or Save.`);
  }

  // ---------- trick or treat ----------
  const FALLBACK_RADIUS = 350, MAX_AREA_KM2 = 4;
  $("houses").addEventListener("input", () => ($("housesOut").textContent = $("houses").value));
  function clearHouses() { houses = []; houseLayer.clearLayers(); $("totInfo").hidden = true; $("totNotes").hidden = true; $("totStartBtn").disabled = true; }
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
  function startDraw() { drawing = true; draft = []; area = null; clearHouses(); map.getContainer().style.cursor = "crosshair"; map.doubleClickZoom.disable(); renderArea(); }
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
    if (!drawing || /INPUT|TEXTAREA|SELECT/.test(document.activeElement?.tagName)) return;
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
    let res;
    try { res = await api("POST", "/api/trick-or-treat/plan", body); }
    catch (e) { $("totInfo").hidden = true; throw e; }
    houses = res.houses; pace = res.pace;
    houses.forEach((h, i) => L.marker([h.lat, h.lon], {
      icon: L.divIcon({ className: h.lights_off ? "house-off" : "", html: `<div class="pumpkin">${h.lights_off ? "🌑" : "🎃"}</div>`, iconSize: [22, 22] }),
      title: h.lights_off ? `House ${i + 1} (lights off — skipped)` : `House ${i + 1}`, interactive: false,
    }).addTo(houseLayer));
    const visit = houses.filter((h) => !h.lights_off);
    L.polyline(visit.map((h) => [h.lat, h.lon]), { color: "#ff7a00", weight: 2, dashArray: "3 6", interactive: false }).addTo(houseLayer);
    map.fitBounds(L.latLngBounds(houses.map((h) => [h.lat, h.lon])).pad(0.25), { maxZoom: 18 });
    let dist = 0; for (let i = 1; i < visit.length; i++) dist += haversine([visit[i - 1].lat, visit[i - 1].lon], [visit[i].lat, visit[i].lon]);
    const secs = dist / (pace.speed_kmh / 3.6) * 1.3 + visit.reduce((a, h) => a + h.dwell_s, 0);
    const off = houses.length - visit.length;
    $("totInfo").textContent = `🎃 ${visit.length} houses${off ? ` (+${off} with lights off)` : ""} · about ${fmtDist(dist)} of walking · roughly ${Math.round(secs / 60)} min at ${pace.speed_kmh} km/h. Press “3 · Start” when ready.${res.source === "offline" ? " 📡 (Using saved offline houses.)" : ""}`;
    $("totNotes").replaceChildren(...(res.notes || []).map((n) => { const e = document.createElement("li"); e.textContent = n; return e; }));
    $("totNotes").hidden = !(res.notes || []).length;
    $("totStartBtn").disabled = false;
  });
  $("totStartBtn").onclick = (ev) => guarded(ev.currentTarget, async () => {
    const visit = houses.filter((h) => !h.lights_off);
    if (!visit.length) throw { message: "Find houses first.", hint: "Press “2 · Find houses”." };
    await startJourney({
      stops: visit.map((h) => ({ lat: h.lat, lon: h.lon, dwell_s: h.dwell_s })), mode: "walk", speed_kmh: pace.speed_kmh,
      jump_to_first: true, kind: "trick_or_treat", speed_jitter: pace.speed_jitter,
    }, `Happy Halloween! 🎃 Heading to the first house…${settings.tot_kids ? " (with the kids)" : ""}`);
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
    if (sim.reconnecting || w.state === "reconnecting") {
      state = "reconnecting"; title = "📡 Reconnecting…";
      detail = settings.auto_reconnect === false ? "Waiting for the iPhone." : "Waiting for the iPhone. The location and route carry on automatically when it is back.";
    } else if (w.error) { state = "simulating"; title = "Stopped"; detail = w.error.message + (w.error.hint ? ` ${w.error.hint}` : ""); }
    else if (w.active && w.state === "waiting") {
      state = "moving";
      if (w.reason === "traffic") { title = "🚦 Stopped at a junction"; }
      else title = tot ? `🎃 At the door — house${where}` : `Paused at stop${where}`;
      detail = `Moving on in ${fmtSecs(w.wait_left_s)}.`;
    } else if (w.active) {
      state = "moving";
      title = w.returning ? "↩ Returning to start" : tot ? `🎃 Walking to house${where}` : `Moving${where ? ` to stop${where}` : ""}`;
      detail = `${(w.cur_speed_kmh ?? w.speed_kmh).toFixed(1)} km/h · ${fmtDist(w.remaining_m)} to go${w.loop ? " · looping" : ""}`;
    } else if (sim.active) {
      state = "simulating"; title = w.state === "done" ? "Finished — simulating" : "Simulating";
      detail = `${fmt4(sim.lat)}, ${fmt4(sim.lon)}`;
    }
    $("now").dataset.state = state; $("nowTitle").textContent = title; $("nowDetail").textContent = detail;
    $("stopBtn").hidden = !w.active; $("clearBtn").hidden = !sim.active;
    $("returnBtn").hidden = !(sim.origin && !w.active && haversine(sim.origin, [sim.lat, sim.lon]) > 3);   // not when already at the start
    const cd = sim.cooldown;
    $("cooldownNote").hidden = !(settings.cooldown_hint && cd);
    if (cd) $("cooldownNote").textContent = `⏱ Jump cooldown: ${clock(cd.remaining_s)} left · ${cd.km} km jump — a real trip would take about this long.`;
  }
  function render(s) {
    status = s;
    const chip = $("device"), banner = $("banner");
    chip.dataset.state = s.state;
    if (s.state === "ready") {
      const d = s.device;
      $("deviceText").textContent = `${d.name} · iOS ${d.ios_version}${d.connection === "Wi-Fi" ? " · 📶" : ""}`;
      chip.title = `Phones & connection (${d.connection}; ${d.method})`; banner.hidden = true;
      const wifi = $("wifiSwitch"); wifi.checked = !!d.wifi_enabled; wifi.disabled = d.connection !== "USB" || d.wifi_enabled === null;
      $("wifiSmall").textContent = d.connection !== "USB" ? "Connected over Wi-Fi. Plug in a cable to change this."
        : "Turn this on while the phone is plugged in, then you can unplug it.";
    } else {
      $("deviceText").textContent = s.error?.message || "Not ready"; chip.title = "Phones & connection";
      $("bannerMsg").textContent = s.error?.message || ""; $("bannerHint").textContent = s.error?.hint || "";
      $("trustBtn").hidden = s.state !== "not_trusted"; banner.hidden = false;
    }
    const warn = (s.warnings || [])[0];
    $("notice").hidden = !warn;
    if (warn) { $("noticeMsg").textContent = `ℹ️ ${warn.message}`; $("noticeHint").textContent = warn.hint || ""; }
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
    catch (e) { render({ state: "server_down", error: e, device: null, simulation: { active: false }, walk: {}, warnings: [] }); }
  }

  (async function init() {
    try { settings = await api("GET", "/api/settings"); } catch { settings = {}; }
    applySettings();
    await loadDevices();
    (function loop() { refresh().finally(() => setTimeout(loop, status?.walk?.active ? 1000 : 3000)); })();
  })();

  showTab(["place", "move", "tot", "saved", "offline"].includes(store.get("tab")) ? store.get("tab") : "place");
  pinLine(); loadFavs(); loadRecent(); loadRoutes(); renderStops(); renderArea(); showGpx();
})();
