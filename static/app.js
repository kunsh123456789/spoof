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
    const ctl = new AbortController();
    const timer = setTimeout(() => ctl.abort(), opts.timeout || 60000);   // a stuck request must never wedge the buttons
    try {
      res = await fetch(path, { method, headers, signal: ctl.signal, body: body === undefined ? undefined : (opts.raw ? body : JSON.stringify(body)) });
    } catch (e) {
      throw e && e.name === "AbortError"
        ? { message: "The app did not answer in time.", hint: "It may be busy talking to the iPhone. Wait a moment and try again." }
        : { message: "Cannot reach the local server.", hint: "Is the app still running? Quit it and open it again." };
    } finally { clearTimeout(timer); }
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
    if (name === "schedule") { loadSchedules(); loadRoutes(); }
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
      routesCache = routes;
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

  // ---------- schedule (timed plans) ----------
  let plans = [], nextEvent = null, editor = null, routesCache = [];
  const STATUS_LABEL = { pending: "⏳ waiting", running: "▶ running", done: "✅ done", error: "⚠ failed", missed: "🕘 missed", skipped: "⏭ skipped" };
  const clockText = (at) => new Date(at).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  const dateText = (at) => new Date(at).toLocaleDateString([], { weekday: "short", month: "short", day: "numeric" });
  function inText(sec) {
    if (sec <= 0) return "now";
    if (sec < 90) return `in ${sec} s`;
    if (sec < 5400) return `in ${Math.round(sec / 60)} min`;
    const h = Math.floor(sec / 3600), m = Math.round((sec % 3600) / 60);
    return h >= 24 ? `in ${Math.round(h / 24)} day${Math.round(h / 24) > 1 ? "s" : ""}` : `in ${h} h ${m} min`;
  }
  function describeStep(s) {
    const p = s.params || {}, place = p.label || (p.lat !== undefined ? `${fmt4(p.lat)}, ${fmt4(p.lon)}` : "?");
    switch (s.type) {
      case "set_location": return `📍 Set location: ${place}`;
      case "move_to": return `${p.mode === "drive" ? "🚗 Drive" : p.mode === "straight" ? "➡️ Move" : "🚶 Walk"} to ${place} at ${p.speed_kmh} km/h${p.shaky ? " · shaky" : ""}${p.start ? ` (starting at ${p.start.label || "a set place"})` : ""}`;
      case "route": return `🧭 Run saved route${p.label ? `: ${p.label}` : ""}${p.jump === false ? "" : " (jump to its start)"}`;
      case "trick_or_treat": return `🎃 Trick-or-treat · ${p.polygon ? "your drawn area" : (p.label || "near the pin")} · ${p.houses} houses${p.kids ? " · kids" : ""}${p.avoid_busy ? " · avoid busy roads" : ""}${p.realistic ? " · realistic" : ""}`;
      case "clear": return "↩ Restore real GPS";
      default: return "⏹ Stop moving";
    }
  }
  async function loadSchedules() {
    try {
      const r = await api("GET", "/api/schedules");
      plans = r.plans; nextEvent = r.next; renderPlans(); renderNext();
    } catch { /* the status bar already shows server problems */ }
  }
  function renderNext() {
    const n = nextEvent;
    const text = n ? `⏰ ${clockText(n.at)} — ${n.title || ({ trick_or_treat: "Trick or treat", move_to: "Walk", route: "Saved route", set_location: "Set location", clear: "Restore real GPS", stop: "Stop" })[n.type]} (${inText(Math.round((Date.parse(n.at) - Date.now()) / 1000))}) · ${n.plan_name}` : "";
    $("schedNote").hidden = !n; $("schedNote").textContent = text;
    $("schedNext").textContent = n ? `Next: ${text.slice(2)}` : (plans.some((p) => p.armed) ? "Activated plans have nothing left to run." : "No activated plans. Create one, save it, then switch Activate on.");
  }
  function renderPlans() {
    $("plans").replaceChildren(...plans.map((p) => {
      const card = document.createElement("div"); card.className = `plan${p.armed ? " armed" : ""}`;
      const head = document.createElement("div"); head.className = "planhead";
      const name = document.createElement("span"); name.className = "planname"; name.textContent = p.name;
      head.append(name,
        miniBtn("✎", "Edit this plan (editing turns it off)", () => openEditor(p)),
        miniBtn("↺", "Reset every step to waiting", async () => { try { await api("POST", `/api/schedules/${p.id}/reset`); loadSchedules(); } catch (e) { fail(e); } }),
        miniBtn("✕", "Delete this plan", async () => { if (!confirm(`Delete the plan “${p.name}”?`)) return; try { await api("DELETE", `/api/schedules/${p.id}`); loadSchedules(); } catch (e) { fail(e); } }));
      const sw = document.createElement("label"); sw.className = "sw";
      const cb = document.createElement("input"); cb.type = "checkbox"; cb.checked = !!p.armed;
      const tr = document.createElement("span"); tr.className = "track";
      const tx = document.createElement("span"); tx.innerHTML = `<b>Activate</b><small>${p.armed ? "ON — steps run by themselves at their times (the app must stay open and the Mac awake)." : "OFF — nothing will run."}</small>`;
      sw.append(cb, tr, tx);
      cb.addEventListener("change", async () => {
        try {
          const r = await api("POST", `/api/schedules/${p.id}/arm`, { armed: cb.checked });
          if (cb.checked && r.skipped_on_arm) toast(`${r.skipped_on_arm} step${r.skipped_on_arm > 1 ? "s were" : " was"} already in the past and will be skipped.`, 6000);
          else toast(cb.checked ? "Plan activated." : "Plan deactivated.");
        } catch (e) { cb.checked = !cb.checked; fail(e); }
        loadSchedules();
      });
      card.append(head, sw);
      const meta = document.createElement("p"); meta.className = "muted"; meta.textContent = `${dateText(p.steps[0].at)} · runs late steps up to ${p.grace_min} min${p.device ? " · specific phone" : ""}`; card.append(meta);
      p.steps.forEach((s) => {
        const line = document.createElement("div"); line.className = "stepline";
        const when = document.createElement("span"); when.className = "when"; when.textContent = clockText(s.at);
        const what = document.createElement("span"); what.textContent = s.title || describeStep(s);
        if (s.note) { const n = document.createElement("span"); n.className = "note"; n.textContent = s.note; what.append(n); }
        const right = document.createElement("span"); right.style.display = "flex"; right.style.gap = "4px"; right.style.alignItems = "center";
        const b = document.createElement("span"); b.className = "sbadge"; b.dataset.s = s.status; b.textContent = STATUS_LABEL[s.status] || s.status;
        right.append(b, miniBtn("⏵", "Test this step now", async () => {
          if (!confirm(`Run this step right now?\n\n${describeStep(s)}`)) return;
          try { const r = await api("POST", `/api/schedules/${p.id}/steps/${s.id}/run`); toast(r.note); refresh(); } catch (e) { fail(e); }
        }));
        line.append(when, what, right); card.append(line);
      });
      return card;
    }));
  }

  // -- the plan editor
  const localDate = (d = new Date()) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
  const addMinutes = (hhmm, m) => { const [h, mi] = hhmm.split(":").map(Number); const t = (h * 60 + mi + m) % 1440; return `${String(Math.floor(t / 60)).padStart(2, "0")}:${String(t % 60).padStart(2, "0")}`; };
  function newStep(time, type = "trick_or_treat") { return { id: undefined, time, type, params: defaultParams(type) }; }
  function defaultParams(type) {
    return ({ move_to: { mode: "walk", speed_kmh: 5, shaky: false }, trick_or_treat: { houses: 20, radius_m: 350, kids: false, avoid_busy: false, realistic: false }, route: { jump: true } })[type] || {};
  }
  function openEditor(plan) {
    const first = plan ? new Date(plan.steps[0].at) : new Date();
    editor = {
      id: plan?.id || null, name: plan?.name || "", date: localDate(first), device: plan?.device || "", grace: plan?.grace_min ?? 10,
      steps: plan ? plan.steps.map((s) => ({ id: s.id, time: s.at.slice(11, 16), type: s.type, params: JSON.parse(JSON.stringify(s.params)) })) : [newStep(addMinutes(new Date().toTimeString().slice(0, 5), 60))],
    };
    $("planEditor").hidden = false; $("newPlanBtn").hidden = true; $("editorTitle").textContent = plan ? `Edit “${plan.name}”` : "New plan";
    $("planName").value = editor.name; $("planDate").value = editor.date; $("planGrace").value = editor.grace;
    $("planDevice").replaceChildren(...[["", "Default phone"], ...devices.map((d) => [d.udid, `${d.name}${d.ios_version ? ` · iOS ${d.ios_version}` : ""}`])].map(([v, t]) => { const o = document.createElement("option"); o.value = v; o.textContent = t; return o; }));
    $("planDevice").value = editor.device; renderEditorSteps();
    $("planEditor").scrollIntoView({ block: "nearest" });
  }
  function closeEditor() { editor = null; $("planEditor").hidden = true; $("newPlanBtn").hidden = false; }
  async function lookup(q) {
    if (!q.trim()) throw { message: "Type a place or address first." };
    const { results } = await api("GET", `/api/search?q=${encodeURIComponent(q)}`);
    if (!results.length) throw { message: `No place found for “${q}”.`, hint: "Try a different spelling." };
    return { lat: results[0].lat, lon: results[0].lon, label: shortName(results[0].name) };
  }
  function pickerRow(placeholder, getObj, setObj, extra = []) {
    const wrap = document.createElement("div");
    const row = document.createElement("div"); row.className = "row";
    const inp = document.createElement("input"); inp.placeholder = placeholder; inp.value = getObj()?.label || "";
    const find = document.createElement("button"); find.type = "button"; find.className = "secondary"; find.textContent = "Find";
    const note = document.createElement("p"); note.className = "found";
    const show = () => { const o = getObj(); note.className = "found"; note.textContent = o && o.lat !== undefined ? `✓ ${o.label || "Place"} (${fmt4(o.lat)}, ${fmt4(o.lon)})` : ""; };
    find.onclick = async () => { try { setObj(await lookup(inp.value)); inp.value = getObj().label; show(); } catch (e) { note.className = "found bad"; note.textContent = e.message; } };
    inp.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); find.click(); } });
    row.append(inp, find, ...extra.map((x) => x(show, inp))); wrap.append(row, note); show();
    return wrap;
  }
  const pinButton = (setObj) => (show, inp) => { const b = document.createElement("button"); b.type = "button"; b.className = "secondary"; b.textContent = "📍 Pin"; b.title = "Use the pin on the map";
    b.onclick = () => { try { const c = readInputs(); setObj({ lat: c.lat, lon: c.lon, label: "the pin" }); inp.value = "the pin"; show(); } catch (e) { fail(e); } }; return b; };
  function check(label, get, set) {
    const l = document.createElement("label"); l.className = "check"; const c = document.createElement("input"); c.type = "checkbox"; c.checked = !!get(); c.onchange = () => set(c.checked);
    l.append(c, document.createTextNode(" " + label)); return l;
  }
  function numberField(label, get, set, min, max) {
    const l = document.createElement("label"); l.textContent = label; const i = document.createElement("input"); i.type = "number"; i.min = min; i.max = max; i.value = get(); i.oninput = () => set(Number(i.value)); l.append(i); return l;
  }
  function renderParams(st) {
    const box = document.createElement("div"); box.className = "params";
    const p = st.params;
    const main = { get: () => p, set: (o) => Object.assign(p, o) };
    if (st.type === "set_location") box.append(pickerRow("Place or address", () => (p.lat !== undefined ? p : null), main.set, [pinButton(main.set)]));
    if (st.type === "move_to") {
      box.append(pickerRow("Destination (address)", () => (p.lat !== undefined ? p : null), main.set, [pinButton(main.set)]));
      const mode = document.createElement("select"); [["walk", "Walk (real footpaths)"], ["drive", "Drive (real roads)"], ["straight", "Straight line"]].forEach(([v, t]) => { const o = document.createElement("option"); o.value = v; o.textContent = t; mode.append(o); });
      mode.value = p.mode; mode.onchange = () => (p.mode = mode.value);
      const speed = numberField("Speed (km/h)", () => p.speed_kmh, (v) => (p.speed_kmh = v), 1, 400);
      const r = document.createElement("div"); r.className = "row"; r.append(mode, speed); box.append(r,
        check("Shaky (GPS noise + uneven pace)", () => p.shaky, (v) => (p.shaky = v)));
      const hint = document.createElement("p"); hint.className = "muted"; hint.textContent = "Starts from wherever the phone is then. To start somewhere specific, set it here:"; box.append(hint,
        pickerRow("Start at (optional)", () => p.start || null, (o) => (p.start = o)));
    }
    if (st.type === "route") {
      const sel = document.createElement("select"); const none = document.createElement("option"); none.value = ""; none.textContent = routesCache.length ? "Choose a saved route…" : "No saved routes yet (save one in the Move tab)"; sel.append(none);
      routesCache.forEach((r) => { const o = document.createElement("option"); o.value = r.id; o.textContent = r.name; sel.append(o); });
      sel.value = p.route_id || ""; sel.onchange = () => { p.route_id = sel.value; p.label = sel.selectedOptions[0]?.textContent || ""; };
      box.append(sel, check("Jump to the route's start first", () => p.jump !== false, (v) => (p.jump = v)));
    }
    if (st.type === "trick_or_treat") {
      const where = document.createElement("p"); where.className = "found"; const upd = () => { where.className = "found"; where.textContent = p.polygon ? `✓ Using your drawn area (${p.polygon.length} points)` : p.lat !== undefined ? `✓ Around ${p.label || "the pin"}` : "Choose where to trick-or-treat:"; };
      const set = (o) => { delete p.polygon; Object.assign(p, o); upd(); };
      const areaBtn = document.createElement("button"); areaBtn.type = "button"; areaBtn.className = "secondary"; areaBtn.textContent = "✏️ Use my drawn area"; areaBtn.title = "The area drawn in the Trick or treat tab";
      areaBtn.onclick = () => { if (!area) return fail({ message: "Draw an area first.", hint: "Use “Draw area” in the Trick or treat tab." }); p.polygon = area.map((q) => [...q]); delete p.lat; delete p.lon; p.label = "drawn area"; upd(); };
      box.append(where, pickerRow("Neighborhood (address)", () => (!p.polygon && p.lat !== undefined ? p : null), set, [pinButton(set)]));
      const r = document.createElement("div"); r.className = "row"; r.append(areaBtn); box.append(r);
      const nums = document.createElement("div"); nums.className = "row"; nums.append(numberField("Houses", () => p.houses, (v) => (p.houses = v), 3, 60), numberField("Radius (m, if no area)", () => p.radius_m, (v) => (p.radius_m = v), 100, 1500)); box.append(nums,
        check("👨‍👩‍👧 Group of kids", () => p.kids, (v) => (p.kids = v)), check("🛣️ Avoid busy roads", () => p.avoid_busy, (v) => (p.avoid_busy = v)), check("🌙 Realistic houses", () => p.realistic, (v) => (p.realistic = v)));
      upd();
    }
    return box;
  }
  function renderEditorSteps() {
    $("planSteps").replaceChildren(...editor.steps.map((st, i) => {
      const row = document.createElement("div"); row.className = "steprow";
      const head = document.createElement("div"); head.className = "stephead";
      const t = document.createElement("input"); t.type = "time"; t.value = st.time; t.oninput = () => (st.time = t.value);
      const sel = document.createElement("select");
      [["trick_or_treat", "🎃 Trick or treat"], ["move_to", "🚶 Walk / drive to…"], ["route", "🧭 Saved route"], ["set_location", "📍 Set location"], ["stop", "⏹ Stop moving"], ["clear", "↩ Restore real GPS"]]
        .forEach(([v, tx]) => { const o = document.createElement("option"); o.value = v; o.textContent = tx; sel.append(o); });
      sel.value = st.type; sel.onchange = () => { st.type = sel.value; st.params = defaultParams(st.type); renderEditorSteps(); };
      head.append(t, sel, miniBtn("✕", "Remove this step", () => { editor.steps.splice(i, 1); renderEditorSteps(); }));
      row.append(head, renderParams(st)); return row;
    }));
  }
  $("newPlanBtn").onclick = () => { loadRoutes(); loadDevices(); openEditor(null); };
  $("cancelPlanBtn").onclick = closeEditor;
  $("addPlanStepBtn").onclick = () => { if (editor.steps.length >= 50) return toast("A plan can have at most 50 steps."); editor.steps.push(newStep(addMinutes(editor.steps.at(-1)?.time || "20:00", 30), "move_to")); renderEditorSteps(); };
  $("savePlanBtn").onclick = (ev) => guarded(ev.currentTarget, async () => {
    editor.name = $("planName").value.trim(); editor.date = $("planDate").value; editor.device = $("planDevice").value; editor.grace = Number($("planGrace").value);
    if (!editor.name) throw { message: "Give the plan a name." };
    if (!editor.date) throw { message: "Choose the date." };
    let day = new Date(`${editor.date}T00:00`), prev = "";
    const steps = editor.steps.map((st, i) => {
      if (!st.time) throw { message: `Step ${i + 1} needs a time.` };
      if (prev && st.time < prev) day.setDate(day.getDate() + 1);          // after midnight: the next day
      prev = st.time;
      const p = { ...st.params };
      if (st.type === "move_to" || st.type === "set_location") { if (p.lat === undefined) throw { message: `Step ${i + 1}: choose a place.`, hint: "Type an address and press Find." }; }
      if (st.type === "trick_or_treat" && !p.polygon && p.lat === undefined) throw { message: `Step ${i + 1}: choose where to trick-or-treat.`, hint: "Find an address, use the pin, or use your drawn area." };
      if (st.type === "route" && !p.route_id) throw { message: `Step ${i + 1}: choose a saved route.` };
      return { id: st.id, at: `${localDate(day)}T${st.time}`, type: st.type, params: p };
    });
    const body = { name: editor.name, device: editor.device, grace_min: editor.grace, steps };
    if (editor.id) await api("PUT", `/api/schedules/${editor.id}`, body); else await api("POST", "/api/schedules", body);
    closeEditor(); await loadSchedules(); toast("Plan saved. Switch Activate on to let it run.", 5000);
  });
  setInterval(() => { if (document.visibilityState !== "hidden") loadSchedules(); }, 3000);

  // ---------- keep the Mac awake ----------
  let awakeInfo = null;
  const durationText = (s) => (s >= 3600 ? `${Math.floor(s / 3600)} h ${Math.round((s % 3600) / 60)} min` : s >= 90 ? `${Math.round(s / 60)} min` : `${Math.round(s)} s`);
  async function loadAwake() {
    try { awakeInfo = await api("GET", "/api/awake"); } catch { return; }
    const a = awakeInfo;
    let msg;
    if (!a.supported) msg = `Keeping the Mac awake works on macOS only (this computer: ${a.platform}). Keep your computer awake yourself.`;
    else if (a.error) msg = `⚠️ ${a.error}`;
    else if (!a.enabled) msg = "Off: your Mac may fall asleep, which ends the fake location on iOS 17+.";
    else if (a.active) msg = `☕ Keeping your Mac awake now — ${a.reasons.join(", ")}.${a.screen ? "" : " The screen may still turn off."}`;
    else msg = "Nothing needs it right now, so your Mac may sleep when idle.";
    $("awakeState").textContent = msg;
    const s = a.last_sleep, fresh = s && Date.now() / 1000 - s.woke < 900;               // show it for 15 minutes after waking
    $("sleepNote").hidden = !fresh;
    if (fresh) $("sleepNote").textContent = `💤 Your Mac was asleep for ${durationText(s.slept_s)} (until ${new Date(s.woke * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}). On iOS 17+ the phone returned to its real GPS meanwhile; trips and schedule steps paused. The app is reconnecting and restoring the location.`;
    if (status) nowBar(status);
  }
  setInterval(loadAwake, 5000);

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
    if (s.state === "connecting" && state === "idle") { title = "Connecting to the iPhone…"; detail = "This can take a minute the first time (the disk image is mounted)."; state = "reconnecting"; }
    $("now").dataset.state = state; $("nowTitle").textContent = title; $("nowDetail").textContent = detail;
    if (awakeInfo?.active && (w.active || sim.active || state !== "idle")) detail += " · ☕ Mac kept awake";
    $("nowDetail").textContent = detail;
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
    } else if (s.state === "connecting") {
      $("deviceText").textContent = "Connecting…"; chip.title = "Talking to the iPhone (mounting the disk image can take a minute)"; banner.hidden = true;
    } else {
      $("deviceText").textContent = s.error?.message || "Not ready"; chip.title = "Phones & connection";
      $("bannerMsg").textContent = s.error?.message || ""; $("bannerHint").textContent = s.error?.hint || "";
      $("trustBtn").hidden = s.state !== "not_trusted"; banner.hidden = false;
    }
    const warns = s.warnings || [];                          // e.g. a leftover fake location AND an unmounted disk image
    $("notice").hidden = !warns.length;
    $("noticeMsg").textContent = warns.map((w) => `ℹ️ ${w.message}`).join("  ");
    $("noticeHint").textContent = warns.map((w) => w.hint).filter(Boolean).join("  ");
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
    await loadDevices(); loadAwake();
    (function loop() { refresh().finally(() => setTimeout(loop, status?.walk?.active ? 1000 : 3000)); })();
  })();

  showTab(["place", "move", "tot", "schedule", "saved", "offline"].includes(store.get("tab")) ? store.get("tab") : "place");
  pinLine(); loadFavs(); loadRecent(); loadRoutes(); renderStops(); renderArea(); showGpx();
})();
