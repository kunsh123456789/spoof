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

  const map = L.map("map", { zoomControl: false, maxBoundsViscosity: 1.0 }).setView([20, 0], 2);
  L.control.zoom({ position: "topright" }).addTo(map);

  let pin = null, simMarker = null, status = null, busy = false, toastTimer = null, centeredOnSim = false;
  let mode = "walk", kind = "single", tab = "place", stops = [], houses = [], pace = { speed_kmh: 4.5, speed_jitter: 0.2 };
  let pinLabel = null, landed = false, area = null, draft = [], drawing = false, gpx = null, device = store.get("device") || "", devices = [];
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

  // ---------- appearance: mode, accent, seasonal skin, map style, fun (kept per computer in localStorage) ----------
  const ui = {
    mode: store.get("ui.mode") || "auto", accent: store.get("ui.accent") || "blue", seasonal: store.get("ui.seasonal") !== "off",
    fun: store.get("ui.fun") !== "off", layer: store.get("ui.layer") || "street", skinOverride: "",
  };
  const seasonSkin = (d = new Date()) => ({ 9: "halloween", 11: "winter", 1: "hearts" })[d.getMonth()] || "";
  const prefersDark = () => !!(window.matchMedia && matchMedia("(prefers-color-scheme: dark)").matches);
  function applyTheme() {
    const root = document.documentElement, w = status?.walk;
    const dark = ui.mode === "dark" || (ui.mode === "auto" && prefersDark());
    const totOn = tab === "tot" || !!(w?.active && w.kind === "trick_or_treat");
    const skin = ui.skinOverride || (totOn ? "halloween" : (ui.seasonal ? seasonSkin() : ""));
    root.dataset.mode = dark ? "dark" : "light"; root.dataset.accent = ui.accent;
    if (skin) root.dataset.skin = skin; else delete root.dataset.skin;
    $("totBanner").hidden = !totOn;
    $("map").classList.toggle("dark-tiles", dark); $("map").classList.toggle("street", ui.layer === "street");
    document.querySelectorAll("#modeSeg .segbtn").forEach((b) => b.classList.toggle("active", b.dataset.colour === ui.mode));
    document.querySelectorAll("#accentSwatches button").forEach((b) => b.classList.toggle("on", b.dataset.accent === ui.accent));
    document.querySelectorAll("#layerSeg .segbtn").forEach((b) => b.classList.toggle("active", b.dataset.layer === ui.layer));
    $("seasonalSwitch").checked = ui.seasonal; $("funSwitch").checked = ui.fun;
    if (routeLayer) routeLayer.setStyle({ color: accent() });
    if (typeof renderArea === "function") renderArea();
    document.dispatchEvent(new CustomEvent("locspoof:ui", { detail: { skin, dark, fun: ui.fun, tab, tot: totOn } }));
  }
  const updateTheme = applyTheme;
  if (window.matchMedia) matchMedia("(prefers-color-scheme: dark)").addEventListener?.("change", applyTheme);
  function setUi(key, value) { ui[key] = value; store.set(`ui.${key}`, typeof value === "boolean" ? (value ? "on" : "off") : value); applyTheme(); }

  // ---------- map styles + tools ----------
  const OSM_CREDIT = '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors';
  const baseLayers = {
    // Street tiles come through the local server: saved offline copies first, OpenStreetMap live otherwise.
    street: L.tileLayer("/tiles/{z}/{x}/{y}.png", { maxZoom: 19, attribution: OSM_CREDIT }),
    satellite: L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}", { maxZoom: 19,
      attribution: "Tiles &copy; Esri — Source: Esri, Maxar, Earthstar Geographics and the GIS User Community" }),
    topo: L.tileLayer("https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png", { maxZoom: 17, subdomains: "abc",
      attribution: 'Map data &copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors, SRTM | Style &copy; <a href="https://opentopomap.org">OpenTopoMap</a> (CC-BY-SA)' }),
  };
  let currentBase = null;
  function showLayer(name) {
    if (!baseLayers[name]) name = "street";
    if (currentBase) map.removeLayer(currentBase);
    currentBase = baseLayers[name].addTo(map); currentBase.bringToBack();
    ui.layer = name; store.set("ui.layer", name); applyTheme();
  }
  const LAYER_ORDER = ["street", "satellite", "topo"];
  const cycleLayer = () => { showLayer(LAYER_ORDER[(LAYER_ORDER.indexOf(ui.layer) + 1) % 3]); toast(`Map style: ${ui.layer}`); };
  L.control.scale({ position: "bottomright", imperial: false }).addTo(map);
  let crosshairOn = false;
  function setCrosshair(on) {
    crosshairOn = on; $("crosshair").hidden = !on; $("crossBtn")?.classList.toggle("on", on); $("pinHereBtn").hidden = !on;
    if (on) toast("Crosshair on: pan the map, then press “Pin here” (or Enter).", 4000);
  }
  function pinAtCenter() { const c = map.getCenter(); setPin(c.lat, c.lng); toast(`Pin placed at ${fmt4(c.lat)}, ${fmt4(c.lng)}`); snapPin(); }
  function centreOnPhone() {
    const sim = status?.simulation;
    if (sim?.active) map.flyTo([sim.lat, sim.lon], Math.max(map.getZoom(), 16), { duration: 0.8 });
    else if (pin) map.flyTo(pin.getLatLng(), Math.max(map.getZoom(), 16), { duration: 0.8 });
    else toast("The phone is not simulating a location yet.");
  }
  const MapTools = L.Control.extend({
    options: { position: "topright" },
    onAdd() {
      const box = L.DomUtil.create("div", "maptools"); L.DomEvent.disableClickPropagation(box);
      const mk = (id, text, title, fn) => { const b = L.DomUtil.create("button", "", box); b.id = id; b.type = "button"; b.textContent = text; b.title = title; b.setAttribute("aria-label", title); b.onclick = fn; return b; };
      mk("locateBtn", "🎯", "Centre the map on the phone (L)", centreOnPhone);
      mk("crossBtn", "✛", "Crosshair for a precise pin (C)", () => setCrosshair(!crosshairOn));
      const here = mk("pinHereBtn", "📌", "Put the pin at the crosshair (Enter)", pinAtCenter); here.hidden = true;
      mk("layerBtn", "🗺", "Next map style (M)", cycleLayer);
      return box;
    },
  });
  new MapTools().addTo(map);

  // ---------- side card: tabs, collapse, resize ----------
  const isMobile = () => window.innerWidth <= 760;
  function setPanelCollapsed(c) {
    document.body.classList.toggle("panel-collapsed", c); $("expandBtn").hidden = !c; store.set("ui.collapsed", c ? "1" : "0");
    if (typeof scheduleEstimate === "function") setTimeout(() => { scheduleEstimate(); }, 380);
  }
  function setPanelWidth(w) {
    if (isMobile()) { document.documentElement.style.removeProperty("--panel-w"); return; }
    w = Math.max(330, Math.min(w, Math.round(window.innerWidth * 0.6), 720)); document.documentElement.style.setProperty("--panel-w", w + "px"); return w;
  }
  function showTab(name, { toggle = false } = {}) {
    if (toggle && name === tab && !isMobile()) { setPanelCollapsed(!document.body.classList.contains("panel-collapsed")); return; }
    if (document.body.classList.contains("panel-collapsed")) setPanelCollapsed(false);
    tab = name;
    document.querySelectorAll("#tabs button").forEach((b) => { b.classList.toggle("active", b.dataset.tab === name); b.setAttribute("aria-selected", b.dataset.tab === name); });
    document.querySelectorAll(".tabpanel").forEach((p) => (p.hidden = p.id !== `tab-${name}`));
    if (name === "tot") { stopLayer.remove(); areaLayer.addTo(map); } else { stopLayer.addTo(map); areaLayer.remove(); if (drawing) cancelDraw(); }
    store.set("tab", name); applyTheme();
    if (name === "offline") { loadOffline(); scheduleEstimate(); }
    if (typeof updateCapture === "function") updateCapture(false);
    if (name === "saved") loadPanic();
    if (name === "schedule") { loadSchedules(); loadRoutes(); }
    if (name === "activity") loadActivity();
    if (name === "phones") { loadDevices(true); loadRemoteTab(); }
    if (name === "place") renderSetup();
    document.querySelector(".body")?.scrollTo?.({ top: 0 });
  }
  document.querySelectorAll("#tabs button").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab, { toggle: true })));
  $("collapseBtn").onclick = () => setPanelCollapsed(true);
  $("expandBtn").onclick = () => setPanelCollapsed(false);
  $("brand").onclick = () => { logoClicks(); };
  (() => {                                                           // drag the right edge of the card to resize it
    const r = $("resizer");
    r.addEventListener("pointerdown", (e) => { r.setPointerCapture(e.pointerId); document.body.classList.add("resizing"); });
    r.addEventListener("pointermove", (e) => { if (r.hasPointerCapture(e.pointerId)) setPanelWidth(e.clientX - 12); });
    const end = (e) => { if (!document.body.classList.contains("resizing")) return; document.body.classList.remove("resizing"); store.set("ui.panelw", String(Math.round($("panel").getBoundingClientRect().width))); };
    r.addEventListener("pointerup", end); r.addEventListener("pointercancel", end);
    r.addEventListener("dblclick", () => { setPanelWidth(400); store.set("ui.panelw", "400"); });
  })();
  window.addEventListener("resize", () => { const w = Number(store.get("ui.panelw")); if (w) setPanelWidth(w); else if (!isMobile()) document.documentElement.style.removeProperty("--panel-w"); });
  document.querySelectorAll("details.howto").forEach((d) => {          // remember which instruction boxes you closed
    if (store.get(`how-${d.id}`) === "closed") d.open = false;
    d.addEventListener("toggle", () => store.set(`how-${d.id}`, d.open ? "open" : "closed"));
  });

  // ---------- California only: the map (and place search) stay inside the state ----------
  const CA = L.latLngBounds([32.4, -124.6], [42.1, -114.0]);
  function applyCalifornia() {
    if (settings.california_only) {
      map.setMinZoom(5); map.options.maxBoundsViscosity = 1.0; map.setMaxBounds(CA.pad(0.15));
      if (!CA.contains(map.getCenter())) map.fitBounds(CA);
    } else { map.setMaxBounds(null); map.setMinZoom(2); }
  }
  // ---------- settings (the on/off switches) ----------
  const SETTING_NEEDS_REPLAN = new Set(["tot_kids", "tot_avoid_busy", "tot_realistic", "tot_lights_off"]);
  function applySettings(force = false) {
    document.querySelectorAll("[data-setting]").forEach((cb) => (cb.checked = !!settings[cb.dataset.setting]));
    document.querySelectorAll("[data-num]").forEach((inp) => { if ((force || document.activeElement !== inp) && settings[inp.dataset.num] !== undefined) inp.value = settings[inp.dataset.num]; });
  }
  document.querySelectorAll("[data-num]").forEach((inp) => inp.addEventListener("change", async () => {   // a number the user can adjust (seconds)
    const key = inp.dataset.num;
    try {
      const v = Number(inp.value);
      if (inp.value.trim() === "" || !Number.isFinite(v)) throw { message: "Type a number of seconds." };
      settings = await api("PATCH", "/api/settings", { [key]: v });
      if (key.startsWith("tot_") && houses.length) { clearHouses(); toast("Time changed — press “Find houses” again to apply it."); }
    } catch (e) { fail(e); }
    applySettings(true);
  }));
  document.querySelectorAll("[data-setting]").forEach((cb) => cb.addEventListener("change", async () => {
    const key = cb.dataset.setting;
    try {
      settings = await api("PATCH", "/api/settings", { [key]: cb.checked });
      if (key === "remote_enabled") loadRemote();
      if (key === "california_only") { applyCalifornia(); toast(cb.checked ? "The map is now limited to California." : "The whole world is available again."); }
      if (SETTING_NEEDS_REPLAN.has(key) && houses.length) { clearHouses(); toast("Option changed — press “Find houses” again to apply it."); }
    } catch (e) { cb.checked = !cb.checked; fail(e); }
    if (status) nowBar(status);
  }));

  // ---------- panic: jump to one chosen safe spot ----------
  let panicSpot = null;
  async function loadPanic() {
    try { panicSpot = (await api("GET", "/api/panic")).location; } catch { return; }
    $("panicInfo").textContent = panicSpot ? `🚨 ${panicSpot.name || "Panic spot"} · ${fmt4(panicSpot.lat)}, ${fmt4(panicSpot.lon)}` : "Not chosen yet — drop a pin on a safe place and press “Use the current pin”.";
    $("panicClearBtn").hidden = !panicSpot; $("panicBtn").classList.toggle("unset", !panicSpot);
  }
  async function setPanic(lat, lon, name) {
    try { panicSpot = (await api("PUT", "/api/panic", { lat, lon, name: name || "" })).location; toast(`🚨 Panic location: ${name || fmt4(lat) + ", " + fmt4(lon)}`); loadPanic(); } catch (e) { fail(e); }
  }
  async function panic() {
    if (!panicSpot) { showTab("saved"); return toast("Choose a panic location first (Saved tab → Panic location).", 6000); }
    try { await api("POST", "/api/panic/go"); clearRoute(); toast(`🚨 Stopped. Phone moved to ${panicSpot.name || "the panic location"}.`, 6000); refresh(); loadRecent(); }
    catch (e) { fail(e); }
  }
  $("panicBtn").onclick = panic;
  $("panicPinBtn").onclick = async () => { try { const c = readInputs(); await setPanic(c.lat, c.lon, pinLabel || labelFor(c.lat, c.lon) || ""); } catch (e) { fail(e); } };
  $("panicClearBtn").onclick = async () => { try { await api("DELETE", "/api/panic"); loadPanic(); toast("Panic location removed."); } catch (e) { fail(e); } };

  // ---------- pin ----------
  function pinLine() {
    const t = pin ? `📍 ${pinLabel ? pinLabel + " · " : "Pin: "}${fmt4(pin.getLatLng().lat)}, ${fmt4(pin.getLatLng().lng)}` : "📍 No pin yet — click the map to drop one.";
    document.querySelectorAll("[data-pinline]").forEach((e) => (e.textContent = t));
  }
  function setPin(lat, lon, { pan = false, label = null } = {}) {
    if (!pin) {
      pin = L.marker([lat, lon], { draggable: true }).addTo(map);
      pin.on("dragend", () => { const p = pin.getLatLng(); pinLabel = null; fillInputs(p.lat, p.lng); schedulePinLookup(); snapPin(); });
    } else pin.setLatLng([lat, lon]);
    pinLabel = label || labelFor(lat, lon);
    fillInputs(lat, lon);
    if (!pinLabel) schedulePinLookup();
    if (pan) map.setView([lat, lon], Math.max(map.getZoom(), 15));
  }
  function fillInputs(lat, lon) { $("lat").value = fmt(lat); $("lon").value = fmt(lon); pinLine(); }
  function readInputs() {
    const lat = parseFloat($("lat").value), lon = parseFloat($("lon").value);
    if (!Number.isFinite(lat) || !Number.isFinite(lon)) throw { message: "Drop a pin first.", hint: "Click the map, or type a latitude and longitude." };
    if (Math.abs(lat) > 90 || Math.abs(lon) > 180) throw { message: "Latitude must be within ±90 and longitude within ±180." };
    return { lat, lon };
  }
  // A pin dropped by hand moves to the nearest road or path that makes sense (best effort; setting "snap_pin").
  let snapSeq = 0;
  async function snapPin() {
    if (!pin || !settings.snap_pin) return;
    const seq = ++snapSeq, { lat, lng } = pin.getLatLng();
    try {
      const r = await api("GET", `/api/snap?lat=${lat}&lon=${lng}&mode=${mode === "drive" ? "drive" : "walk"}`, undefined, { timeout: 15000 });
      const now = pin && pin.getLatLng();
      if (seq !== snapSeq || !r.snapped || !now || Math.abs(now.lat - lat) > 1e-7 || Math.abs(now.lng - lng) > 1e-7) return;   // the user moved on
      setPin(r.lat, r.lon, { snapped: true });
      toast(`📍 Moved the pin ${Math.round(r.moved_m)} m to the nearest road or path.`, 3000);
    } catch { /* offline or busy: the pin stays where it was dropped */ }
  }
  map.on("click", (e) => { if (drawing) return addVertex(e.latlng.wrap()); setPin(e.latlng.lat, e.latlng.lng); snapPin(); });
  for (const id of ["lat", "lon"]) $(id).addEventListener("change", () => {
    try { const { lat, lon } = readInputs(); setPin(lat, lon, { pan: true }); } catch (e) { fail(e); }
  });

  // ---------- actions ----------
  async function guarded(btn, fn) {
    if (busy) return; busy = true; btn.disabled = true;
    try { await fn(); } catch (e) { fail(e); } finally { busy = false; btn.disabled = false; refresh(); }
  }
  $("setBtn").onclick = (ev) => guarded(ev.currentTarget, async () => {
    const c = readInputs(), label = pinLabel || labelFor(c.lat, c.lon);
    await api("POST", "/api/location", { ...c, label });
    toast(`Location set to ${label || fmt4(c.lat) + ", " + fmt4(c.lon)}. Check Apple Maps on the phone.`); loadRecent();
  });
  $("clearBtn").onclick = (ev) => guarded(ev.currentTarget, async () => {
    await api("DELETE", "/api/location"); centeredOnSim = false; clearRoute(); toast("Simulation stopped — real GPS restored.");
  });
  $("stopBtn").onclick = (ev) => guarded(ev.currentTarget, async () => { await api("DELETE", "/api/journey"); clearRoute(); toast("Stopped. The phone stays at its current spot."); });
  $("devBtn").onclick = () => { const d = devices.find((x) => normUdid(x.udid) === normUdid(device)) || devices[0]; showTab("phones"); if (d) enableDeveloperMode(d); };
  $("trustBtn").onclick = (ev) => guarded(ev.currentTarget, async () => {
    toast("Look at your iPhone and tap Trust (enter your passcode if asked)…", 8000);
    await api("POST", "/api/trust");
  });

  // ---------- phones & connection ----------
  let devicesLoaded = false; const knownNames = {};
  async function loadDevices(fresh = false) {
    try { devices = (await api("GET", `/api/devices${fresh ? "?fresh=true" : ""}`)).devices; } catch { return; }
    devices.forEach((d) => (knownNames[normUdid(d.udid)] = d.display_name || d.name));
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
      const shown = d.display_name || d.name;
      const el = li(`${sel ? "✓ " : ""}${d.favorite ? "★ " : ""}${shown}${d.nickname ? ` (${d.name})` : ""}${d.ios_version ? ` · iOS ${d.ios_version}` : ""}${d.paired ? "" : " · not trusted yet"}${d.offline ? " · not connected" : ""}${d.simulating ? " · ● simulating" : ""}`);
      if (sel) el.querySelector(".name").classList.add("phone-sel");
      if (d.offline) el.classList.add("phone-off");
      const badge = document.createElement("span"); badge.className = "badge"; badge.textContent = d.offline ? "💤" : d.connection === "USB" ? "🔌 USB" : "📶 Wi-Fi";
      el.append(badge,
        miniBtn(d.favorite ? "★" : "☆", d.favorite ? "Remove from favorites" : "Make this phone a favorite (it stays in the list and sorts first)", () => patchPhone(d, { favorite: !d.favorite })),
        miniBtn("✎", "Rename this phone", () => renamePhone(el, d)));
      el.title = "Click to control this phone";
      el.onclick = () => selectDevice(d.udid);
      return el;
    }) : (missing ? [] : [li("No phones found. Plug one in, or see Wi-Fi setup below.")])));
    renderPhonesTab();
    otherLayer.clearLayers();
    devices.filter((d) => d.simulating && normUdid(d.udid) !== normUdid(device)).forEach((d) =>
      L.marker([d.lat, d.lon], { icon: L.divIcon({ className: "", html: '<div class="other-phone"></div>', iconSize: [14, 14] }), interactive: false })
        .bindTooltip(d.display_name || d.name, { permanent: false }).addTo(otherLayer));
  }
  async function patchPhone(d, body) {
    try { await api("PATCH", `/api/devices/${encodeURIComponent(d.udid)}`, body); await loadDevices(true); if (status) render(status); }
    catch (e) { fail(e); }
  }
  function renamePhone(row, d) {                       // edit the name right in the list (Enter saves, Esc cancels, empty = its own name)
    const input = document.createElement("input"); input.value = d.nickname || d.name; input.maxLength = 40; input.className = "phonename"; input.setAttribute("aria-label", "Phone name");
    let done = false;
    const finish = async (save) => { if (done) return; done = true; if (save && input.value.trim() !== (d.nickname || d.name)) await patchPhone(d, { nickname: input.value.trim() === d.name ? "" : input.value.trim() }); else renderPhones(); };
    input.onkeydown = (e) => { e.stopPropagation(); if (e.key === "Enter") finish(true); else if (e.key === "Escape") finish(false); };
    input.onclick = (e) => e.stopPropagation(); input.onblur = () => finish(true);
    row.replaceChildren(input); input.focus(); input.select();
  }
  // ---------- the Phones tab: which phone is being controlled, and which phones are the remotes ----------
  function renderPhonesTab() {
    if (tab !== "phones") return;
    $("tpPhones").replaceChildren(...(devices.length ? devices.map((d) => {
      const sel = normUdid(d.udid) === normUdid(device), shown = d.display_name || d.name;
      const el = li(`${d.favorite ? "★ " : ""}${shown}${d.nickname ? ` (${d.name})` : ""}`);
      const info = document.createElement("small"); info.className = "muted sub";
      info.textContent = `iOS ${d.ios_version || "?"} · ${d.offline ? "not connected" : d.connection === "USB" ? "🔌 cable" : "📶 Wi-Fi"}${d.developer_mode === true ? " · 🛠 Developer Mode on" : d.developer_mode === false ? " · 🛠 Developer Mode OFF" : ""}${d.simulating ? " · ● fake location on" : ""}${d.moving ? " · moving" : ""}`;
      el.querySelector(".name").append(document.createElement("br"), info);
      if (sel) { const b = document.createElement("span"); b.className = "badge on"; b.textContent = "▶ controlling"; el.append(b); }
      if (d.developer_mode === false && !d.offline) { const b = miniBtn("🛠 Enable Developer Mode", "This phone needs Developer Mode to be controlled", () => enableDeveloperMode(d)); b.classList.add("devbtn"); el.append(b); }
      el.append(miniBtn(d.favorite ? "★" : "☆", "Favorite", () => patchPhone(d, { favorite: !d.favorite })), miniBtn("✎", "Rename this phone", () => renamePhone(el, d)));
      if (d.offline) el.classList.add("phone-off");
      el.title = sel ? "This is the phone the app is controlling" : "Click to control this phone"; el.onclick = () => selectDevice(d.udid);
      return el;
    }) : [li("No iPhone found. Plug one in (or see Wi-Fi setup in the phone chip).")]));
  }
  const DEV_HELP = {
    restarting: ["🔁 The iPhone is restarting.", "When it is back on: unlock it, tap “Turn On” on the “Turn On Developer Mode?” prompt, and enter the passcode. Then come back here: the phone should show “Developer Mode on”."],
    manual: ["✋ Turn it on yourself on the iPhone.", "Because the phone has a passcode, iOS only allows this by hand. The switch is now visible: Settings → Privacy & Security → Developer Mode → On. The phone restarts; confirm “Turn On” and enter the passcode."],
    already_on: ["✅ Developer Mode is already on.", "Nothing to do. If the app still can't control the phone, unplug it, plug it back in and unlock it."],
  };
  async function enableDeveloperMode(d) {
    if (!confirm(`Enable Developer Mode on “${d.display_name || d.name}”?\n\nThe iPhone may restart. Keep it plugged in and unlocked, and be ready to enter its passcode.`)) return;
    try {
      toast("Talking to the iPhone…", 6000);
      const r = await api("POST", `/api/devices/${encodeURIComponent(d.udid)}/developer-mode`, undefined, { timeout: 40000 }), [title, body] = DEV_HELP[r.state] || ["Done.", ""];
      $("tpDevHelp").hidden = false; $("tpDevHelp").replaceChildren(Object.assign(document.createElement("b"), { textContent: title }), document.createElement("br"), body);
      loadDevices(true);
    } catch (e) { fail(e); }
  }
  // ---------- connection history: every connect / drop / reconnect / sleep, newest first ----------
  const HIST_ICON = { up: "✅", down: "❌", drop: "⚠️", switch: "🔄", sleep: "💤", remote_up: "🎮", remote_down: "🎮" };
  async function loadHistory() {
    if (tab !== "phones") return;
    let events; try { events = (await api("GET", "/api/activity?kinds=link")).events; } catch { return; }
    const dayAgo = Date.now() / 1000 - 86400, recent = events.filter((e) => e.ts > dayAgo);
    const drops = recent.filter((e) => e.tag === "drop" || e.tag === "down").length, sleeps = recent.filter((e) => e.tag === "sleep").length, last = events.filter((e) => e.tag === "drop" || e.tag === "down").pop();
    $("tpHistSummary").textContent = events.length ? `Last 24 hours: ${drops} drop${drops === 1 ? "" : "s"} · ${sleeps} Mac sleep${sleeps === 1 ? "" : "s"}${last ? ` · last drop ${new Date(last.ts * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}` : " · nothing has dropped 🎉"}.` : "Nothing recorded yet. Connections and drops will appear here.";
    let day = ""; const rows = [];
    for (const ev of events.slice().reverse().slice(0, 80)) {
      const dn = dayName(ev.ts); if (dn !== day) { day = dn; const b = document.createElement("li"); b.className = "daybreak"; b.textContent = dn; rows.push(b); }
      const el = document.createElement("li"); el.dataset.level = ev.level; el.dataset.tag = ev.tag || "";
      const ic = document.createElement("span"); ic.className = "ic"; ic.textContent = (ev.tag === "remote_down" ? "📴" : HIST_ICON[ev.tag]) || "•";
      const tx = document.createElement("span"); tx.className = "tx"; tx.textContent = ev.text; const ts = document.createElement("span"); ts.className = "ts"; ts.textContent = new Date(ev.ts * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit", second: "2-digit" });
      el.append(ic, tx, ts); rows.push(el);
    }
    $("tpHistory").replaceChildren(...rows);
  }
  let remoteTabTimer = null;
  async function loadRemoteTab() {
    clearTimeout(remoteTabTimer);
    if (tab !== "phones") return;
    let r; try { r = await api("GET", "/api/remote"); } catch { remoteTabTimer = setTimeout(loadRemoteTab, 4000); return; }
    $("tpRemoteOff").hidden = r.enabled; $("tpPair").hidden = !r.enabled; $("tpUrl").hidden = !r.enabled;
    const best = r.urls.find((u) => u.kind === "tailscale") || r.urls[0];
    $("tpUrl").textContent = best ? `Remote address: ${best.url}` : "";
    $("tpCode").textContent = r.code ? `${r.code.code}  ·  ${Math.max(1, Math.round(r.code.expires_in / 60))} min` : "";
    $("tpRemotes").replaceChildren(...(r.enabled ? (r.devices.length ? r.devices : []).map((d) => {
      const el = li(`${d.online ? "🟢" : "⚪"} ${d.name}`);
      const info = document.createElement("small"); info.className = "muted sub";
      info.textContent = d.online ? `connected now · ${d.via === "tailscale" ? "🌍 Tailscale" : "🏠 home Wi-Fi"}` : `not connected${d.last_seen ? " · last seen " + new Date(d.last_seen * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }) : ""}`;
      el.querySelector(".name").append(document.createElement("br"), info);
      el.append(miniBtn("✎", "Rename this remote", () => renameRemote(el, d)),
        miniBtn("✕", `Forget ${d.name}`, async () => { if (!confirm(`Forget “${d.name}”? It will have to pair again.`)) return; try { await api("DELETE", `/api/remote/devices/${d.id}`); loadRemoteTab(); } catch (e) { fail(e); } }));
      return el;
    }) : []));
    if (r.enabled && !r.devices.length) $("tpRemotes").append(li("No remote phones paired yet. Press “New pairing code”."));
    loadHistory();
    remoteTabTimer = setTimeout(loadRemoteTab, 3000);
  }
  function renameRemote(row, d) {
    const input = document.createElement("input"); input.value = d.name; input.maxLength = 40; input.className = "phonename"; input.setAttribute("aria-label", "Remote phone name"); let done = false;
    const finish = async (save) => { if (done) return; done = true; const v = input.value.trim();
      if (save && v && v !== d.name) { try { await api("PATCH", `/api/remote/devices/${d.id}`, { name: v }); } catch (e) { fail(e); } } loadRemoteTab(); };
    input.onkeydown = (e) => { e.stopPropagation(); if (e.key === "Enter") finish(true); else if (e.key === "Escape") finish(false); };
    input.onclick = (e) => e.stopPropagation(); input.onblur = () => finish(true); row.replaceChildren(input); input.focus(); input.select();
  }
  $("tpPair").onclick = async () => { try { await api("POST", "/api/remote/code"); loadRemoteTab(); } catch (e) { fail(e); } };
  $("tpRemoteOn").onclick = async () => { try { settings = await api("PATCH", "/api/settings", { remote_enabled: true }); applySettings(); loadRemoteTab(); } catch (e) { fail(e); } };
  function selectDevice(udid) {
    if (normUdid(udid) === normUdid(device)) return;
    device = udid; store.set("device", udid);
    status = null; centeredOnSim = false; clearRoute(); if (simMarker) { simMarker.remove(); simMarker = null; }
    renderPhones(); refresh(); toast("Now controlling this phone.");
  }
  $("device").onclick = () => {
    if (document.body.classList.contains("panel-collapsed")) setPanelCollapsed(false);
    const box = $("phonesBox"); box.open = !box.open; if (box.open) { $("panelTop").scrollTo?.({ top: 0 }); box.scrollIntoView?.({ block: "nearest" }); }
  };
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
    await startJourney({ stops: [{ ...c, label: pinLabel || labelFor(c.lat, c.lon) }], mode, speed_kmh: currentSpeed() },
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
    const st = { lat, lon, label, ...extra }; stops.push(st); renderStops(); toast(`Stop ${stops.length} added — see the Move tab.`);
    if (!label) reverseLabel(lat, lon).then((l) => { if (l && !st.label) { st.label = l; renderStops(); } });
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
    await startJourney({ stops: [], path: gpx.points, mode, speed_kmh: currentSpeed(), jump_to_first: jump, title: gpx.name || "the track" }, "Playing the track…");
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
  function clearHouses() { houses = []; arrowSeq++; houseLayer.clearLayers(); $("totInfo").hidden = true; $("totNotes").hidden = true; $("totStartBtn").disabled = true; }
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
    const visit = houses.filter((h) => !h.lights_off);
    houses.forEach((h, i) => {
      const n = h.lights_off ? "" : visit.indexOf(h) + 1;
      const m = L.marker([h.lat, h.lon], {
        icon: L.divIcon({ className: h.lights_off ? "house-off" : "", html: `<div class="pumpkin${n === 1 ? " start" : ""}">${h.lights_off ? "🌑" : "🎃"}${n ? `<b>${n}</b>` : ""}</div>`, iconSize: [24, 24] }),
        title: h.lights_off ? `House ${i + 1} (lights off — skipped)` : `House ${n}${n === 1 ? " — the tour starts here" : ""}`, zIndexOffset: n === 1 ? 500 : 0,
      }).addTo(houseLayer);
      if (!h.lights_off) {
        const box = document.createElement("div"); box.className = "housepop";
        box.append(`House ${n}${n === 1 ? " (start)" : ""} · pause about ${Math.round(h.dwell_s)} s`);
        if (n !== 1) { box.append(document.createElement("br")); const b = document.createElement("button"); b.className = "secondary"; b.textContent = "▶ Start the tour here";
          b.onclick = () => { map.closePopup(); setPin(h.lat, h.lon, { label: `house ${n}` }); $("findBtn").click(); toast("Re-planning the tour from that house…"); }; box.append(b); }
        m.bindPopup(box);
      }
    });
    drawTourArrows(visit);
    map.fitBounds(L.latLngBounds(houses.map((h) => [h.lat, h.lon])).pad(0.25), { maxZoom: 18 });
    let dist = 0; for (let i = 1; i < visit.length; i++) dist += haversine([visit[i - 1].lat, visit[i - 1].lon], [visit[i].lat, visit[i].lon]);
    const secs = dist / (pace.speed_kmh / 3.6) * 1.3 + visit.reduce((a, h) => a + h.dwell_s, 0);
    const off = houses.length - visit.length;
    $("totInfo").textContent = `🎃 ${visit.length} houses${off ? ` (+${off} with lights off)` : ""} · about ${fmtDist(dist)} of walking · roughly ${Math.round(secs / 60)} min at ${pace.speed_kmh} km/h. Press “3 · Start” when ready.${res.source === "offline" ? " 📡 (Using saved offline houses.)" : ""}`;
    $("totNotes").replaceChildren(...(res.notes || []).map((n) => { const e = document.createElement("li"); e.textContent = n; return e; }));
    $("totNotes").hidden = !(res.notes || []).length;
    $("totStartBtn").disabled = false;
  });
  // The road the tour will take, with an arrow every so often showing which way it goes.
  let arrowSeq = 0;
  function tourStart() { try { const c = readInputs(); return Number.isFinite(c.lat) && Number.isFinite(c.lon) ? c : null; } catch { return null; } }
  async function drawTourArrows(visit) {
    const seq = ++arrowSeq, pts = visit.map((h) => [h.lat, h.lon]), st = settings.tot_start_at_pin !== false ? tourStart() : null;
    if (st) { pts.unshift([st.lat, st.lon]); pts.push([st.lat, st.lon]); }                // out from the pin and back to it
    if (pts.length < 2) return;
    let route, note = "";
    try { route = (await api("POST", "/api/route-preview", { points: pts, mode: "walk" }, { timeout: 40000 })).route; }
    catch { route = pts; note = " (couldn't load the roads, so the arrows are straight lines)"; }
    if (seq !== arrowSeq || !houses.length) return;                       // a newer search replaced this one
    L.polyline(route, { color: "#ff7a00", weight: 4, opacity: 0.75, interactive: false }).addTo(houseLayer);
    addArrows(houseLayer, route, "#c2410c");
    if (note) $("totInfo").textContent += note;
  }
  function addArrows(group, pts, color) {
    const P = (p) => L.CRS.EPSG3857.project(L.latLng(p[0], p[1]));
    let total = 0; for (let i = 1; i < pts.length; i++) total += haversine(pts[i - 1], pts[i]);
    const gap = Math.max(35, total / 150); let next = gap / 2, run = 0;
    for (let i = 1; i < pts.length; i++) {
      const seg = haversine(pts[i - 1], pts[i]); if (seg < 0.5) continue;
      while (next <= run + seg) {
        const t = (next - run) / seg, a = pts[i - 1], b = pts[i];
        const at = [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t], pa = P(a), pb = P(b);
        const deg = Math.atan2(pb.x - pa.x, pb.y - pa.y) * 180 / Math.PI;                 // clockwise from north
        L.marker(at, { interactive: false, keyboard: false, icon: L.divIcon({ className: "arrow", iconSize: [16, 16], iconAnchor: [8, 8],
          html: `<svg viewBox="0 0 16 16" style="transform:rotate(${deg.toFixed(1)}deg)"><path d="M8 1 L14 14 L8 10.5 L2 14 Z" fill="${color}" stroke="#fff" stroke-width="1.2" stroke-linejoin="round"/></svg>` }) }).addTo(group);
        next += gap;
      }
      run += seg;
    }
  }
  $("totStartBtn").onclick = (ev) => guarded(ev.currentTarget, async () => {
    const visit = houses.filter((h) => !h.lights_off);
    if (!visit.length) throw { message: "Find houses first.", hint: "Press “2 · Find houses”." };
    const atPin = settings.tot_start_at_pin !== false, start = tourStart();
    if (atPin && start) {                                  // begin at the pin: put the phone there first, then walk out and come back
      await api("POST", "/api/location", { lat: start.lat, lon: start.lon, label: pinLabel || labelFor(start.lat, start.lon) || "the pin" });
    }
    await startJourney({
      stops: visit.map((h, i) => ({ lat: h.lat, lon: h.lon, dwell_s: h.dwell_s, label: `house ${i + 1}` })), mode: "walk", speed_kmh: pace.speed_kmh,
      jump_to_first: !(atPin && start), return_to_start: atPin, kind: "trick_or_treat", speed_jitter: pace.speed_jitter,
    }, `Happy Halloween! 🎃 ${atPin && start ? "Heading out from the pin…" : "Heading to the first house…"}${settings.tot_kids ? " (with the kids)" : ""}`);
  });

  // ---------- offline areas ----------
  const packLayer = L.layerGroup().addTo(map);
  let offTimer = null, offPoll = null, offBusy = false;
  const mb = (b) => (b / 1048576).toFixed(1);
  // What "Download" will save: the part of the map you can really see (not the bit hidden under the side card, top bar or progress bar).
  function captureRect() {
    const m = 20, top = $("topbar").getBoundingClientRect().bottom + m, dock = $("dock").getBoundingClientRect(), panel = $("panel").getBoundingClientRect();
    const hidden = document.body.classList.contains("panel-collapsed");
    let left = m, bottom = innerHeight - m, t = top;
    if (isMobile()) { t = Math.max(top, dock.bottom + m); if (!hidden) bottom = panel.top - m; }
    else { bottom = dock.top - m; if (!hidden) left = panel.right + m; }
    const right = innerWidth - m - 56;                                                 // leave room for the map buttons
    return { left, top: t, right, bottom };
  }
  function viewBBox() {
    const r = captureRect();
    if (r.right - r.left < 80 || r.bottom - r.top < 80) { const b = map.getBounds(); return { south: b.getSouth(), west: b.getWest(), north: b.getNorth(), east: b.getEast() }; }
    const nw = map.containerPointToLatLng([r.left, r.top]), se = map.containerPointToLatLng([r.right, r.bottom]);
    return { south: se.lat, west: nw.lng, north: nw.lat, east: se.lng };
  }
  function updateCapture(bad = false) {
    const box = $("captureBox"), r = captureRect(), show = tab === "offline" && r.right - r.left >= 80 && r.bottom - r.top >= 80;
    box.hidden = !show; if (!show) return;
    Object.assign(box.style, { left: r.left + "px", top: r.top + "px", width: r.right - r.left + "px", height: r.bottom - r.top + "px" });
    box.classList.toggle("bad", bad === true);
  }
  map.on("move zoom resize", () => updateCapture(captureBad)); window.addEventListener("resize", () => updateCapture(captureBad));
  if (window.ResizeObserver) new ResizeObserver(() => updateCapture(captureBad)).observe($("panel"));
  let captureBad = false;
  function scheduleEstimate() { clearTimeout(offTimer); offTimer = setTimeout(updateEstimate, 400); }
  async function updateEstimate() {
    if (tab !== "offline" || offPoll) return;
    try {
      const e = await api("POST", "/api/offline/estimate", { name: "x", bbox: viewBBox() });
      $("offEstimate").textContent = `📐 ${e.area_km2} km² inside the green box · ${e.tiles} map tiles · about ${e.est_mb} MB`; $("offBtn").disabled = false; captureBad = false;
    } catch (e) { $("offEstimate").textContent = `⚠️ ${e.message} ${e.hint || ""}`; $("offBtn").disabled = true; captureBad = true; }
    updateCapture(captureBad);
  }
  map.on("moveend", () => { if (tab === "offline") scheduleEstimate(); });
  document.addEventListener("locspoof:ui", () => updateCapture(captureBad));
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
        const el = li(r.name); el.onclick = () => { setPin(r.lat, r.lon, { pan: true, label: shortName(r.name) }); ul.hidden = true; };
        el.append(miniBtn("＋", "Add as a stop", () => addStop(r.lat, r.lon, shortName(r.name))), miniBtn("★", "Save to favorites", () => saveFavorite(shortName(r.name), { lat: r.lat, lon: r.lon })));
        return el;
      }));
      setPin(results[0].lat, results[0].lon, { pan: true, label: shortName(results[0].name) });
    } catch (e) { ul.hidden = true; fail(e); }
  });

  // ---------- favorites & recent ----------
  async function saveFavorite(name, where) {
    try { await api("POST", "/api/favorites", { name, ...where }); loadFavs(); toast(`Saved “${name}” to Favorites.`); } catch (e) { fail(e); }
  }
  function rows(list, { star }) {
    return list.map((f) => {
      const el = li(f.name); el.title = `${fmt(f.lat)}, ${fmt(f.lon)}`;
      el.onclick = () => { setPin(f.lat, f.lon, { pan: true, label: f.name }); toast("Pin moved. Go to the Place tab and press Set location."); };
      el.append(miniBtn("＋", "Add as a stop", () => addStop(f.lat, f.lon, f.name)));
      if (!star) el.append(miniBtn("🚨", `Make “${f.name}” the panic location`, () => setPanic(f.lat, f.lon, f.name)));
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
  $("nextChip").onclick = () => showTab("schedule"); $("schedNote").onclick = () => showTab("schedule");
  function renderNext() {
    const n = nextEvent;
    const text = n ? `⏰ ${clockText(n.at)} — ${n.title || ({ trick_or_treat: "Trick or treat", move_to: "Walk", route: "Saved route", set_location: "Set location", clear: "Restore real GPS", stop: "Stop" })[n.type]} (${inText(Math.round((Date.parse(n.at) - Date.now()) / 1000))}) · ${n.plan_name}` : "";
    $("schedNote").hidden = !n; $("schedNote").textContent = text;
    $("nextChip").hidden = !n; if (n) $("nextChip").querySelector(".t").textContent = `⏰ ${clockText(n.at)} · ${inText(Math.round((Date.parse(n.at) - Date.now()) / 1000))}`;
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
      const meta = document.createElement("p"); meta.className = "muted"; meta.textContent = `${p.steps.find((s) => s.at) ? dateText(p.steps.find((s) => s.at).at) : "After you activate it"} · runs late steps up to ${p.grace_min} min${p.device ? " · specific phone" : ""}`; card.append(meta);
      const est = estimateSteps(p.steps.map((s) => ({ startMs: s.at ? Date.parse(s.at) : null, when: s.after_s != null ? "after" : "at", afterVal: s.after_s, afterUnit: 1, type: s.type, params: s.params })), "");
      const fin = est.map((e) => e.end ?? e.start).filter((x) => x != null);
      if (fin.length && !est.some((e) => e.secs == null)) { const done = document.createElement("p"); done.className = "muted"; done.textContent = `🕒 Done around ${whenText(Math.max(...fin), p.steps.find((s) => s.at)?.at)} (estimate)`; card.append(done); }
      p.steps.forEach((s) => {
        const line = document.createElement("div"); line.className = "stepline";
        const when = document.createElement("span"); when.className = "when"; when.textContent = s.after_s != null ? `⏱ +${afterText(s.after_s)}` : clockText(s.at);
        if (s.after_s != null) when.title = `${afterText(s.after_s)} after the last move finished`;
        const what = document.createElement("span"); what.textContent = s.title || describeStep(s);
        const ev = est[p.steps.indexOf(s)]; if (ev) { const l = estimateLine(ev), n = document.createElement("span"); n.className = "note" + (l.cls ? " unknown" : ""); n.textContent = l.text; what.append(n); }
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
  function newStep(time, type = "trick_or_treat") { return { id: undefined, time, type, params: defaultParams(type), when: "at", afterVal: 5, afterUnit: 60 }; }
  const afterText = (s) => (s % 3600 === 0 ? `${s / 3600} h` : s % 60 === 0 ? `${s / 60} min` : `${s} s`);
  function defaultParams(type) {
    return ({ move_to: { mode: "walk", speed_kmh: 5, shaky: false }, trick_or_treat: { houses: 20, radius_m: 350, kids: false, avoid_busy: false, realistic: false }, route: { jump: true } })[type] || {};
  }
  function openEditor(plan) {
    const first = plan && plan.steps.find((s) => s.at) ? new Date(plan.steps.find((s) => s.at).at) : new Date();
    editor = {
      id: plan?.id || null, name: plan?.name || "", date: localDate(first), device: plan?.device || "", grace: plan?.grace_min ?? 10,
      steps: plan ? plan.steps.map((s) => ({ id: s.id, time: s.at ? s.at.slice(11, 16) : "", type: s.type, params: JSON.parse(JSON.stringify(s.params)),
        when: s.after_s != null ? "after" : "at", afterUnit: s.after_s != null && s.after_s % 60 === 0 ? 60 : 1, afterVal: s.after_s != null ? (s.after_s % 60 === 0 ? s.after_s / 60 : s.after_s) : 5 })) : [newStep(addMinutes(new Date().toTimeString().slice(0, 5), 60))],
    };
    $("planEditor").hidden = false; $("newPlanBtn").hidden = true; $("editorTitle").textContent = plan ? `Edit “${plan.name}”` : "New plan";
    $("planName").value = editor.name; $("planDate").value = editor.date; $("planGrace").value = editor.grace;
    $("planDevice").replaceChildren(...[["", "Default phone"], ...devices.map((d) => [d.udid, `${d.favorite ? "★ " : ""}${d.display_name || d.name}${d.ios_version ? ` · iOS ${d.ios_version}` : ""}`])].map(([v, t]) => { const o = document.createElement("option"); o.value = v; o.textContent = t; return o; }));
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
      const nums = document.createElement("div"); nums.className = "row"; nums.append(numberField("Houses", () => p.houses, (v) => (p.houses = v), 3, 80), numberField("Radius (m, if no area)", () => p.radius_m, (v) => (p.radius_m = v), 100, 1500)); box.append(nums,
        check("👨‍👩‍👧 Group of kids", () => p.kids, (v) => (p.kids = v)), check("🛣️ Avoid busy roads", () => p.avoid_busy, (v) => (p.avoid_busy = v)), check("🌙 Realistic houses", () => p.realistic, (v) => (p.realistic = v)));
      upd();
    }
    return box;
  }
  // ---- how long each step takes and when it will be done (an estimate from distances, speeds and your settings) ----
  const ROAD = 1.3;                                           // roads are about 30% longer than the straight line
  const durText = (s) => (s < 90 ? `${Math.round(s)} s` : s < 5400 ? `${Math.round(s / 60)} min` : `${Math.floor(s / 3600)} h ${String(Math.round((s % 3600) / 60)).padStart(2, "0")} min`);
  const pathLen = (pts) => pts.reduce((a, p, i) => (i ? a + haversine(pts[i - 1], p) : 0), 0);
  function estimateStep(st, loc) {
    const p = st.params || {};
    switch (st.type) {
      case "set_location": return p.lat === undefined ? { secs: null, note: "Choose a place" } : { secs: 0, loc: [p.lat, p.lon], note: "instant" };
      case "stop": return { secs: 0, note: "instant" };
      case "clear": return { secs: 0, loc: null, note: "instant" };
      case "move_to": {
        if (p.lat === undefined) return { secs: null, note: "Choose a destination" };
        const from = p.start ? [p.start.lat, p.start.lon] : loc, speed = Math.max(Number(p.speed_kmh) || 5, 0.5);
        if (!from) return { secs: null, loc: [p.lat, p.lon], note: "Needs a start place (or the phone's location when it runs)" };
        const dist = haversine(from, [p.lat, p.lon]) * (p.mode === "straight" ? 1 : ROAD);
        return { secs: dist / (speed / 3.6), loc: [p.lat, p.lon], note: `${fmtDist(dist)} at ${speed} km/h` };
      }
      case "route": {
        const r = routesCache.find((x) => x.id === p.route_id); if (!r) return { secs: null, note: "Choose a saved route" };
        if (r.loop) return { secs: null, note: "this route loops until stopped" };
        const pts = r.kind === "track" ? r.path : r.stops.map((s) => [s.lat, s.lon]), k = r.mode === "straight" || r.kind === "track" ? 1 : ROAD;
        let dist = pathLen(pts) * k; const first = pts[0];
        if (p.jump === false && loc && first) dist += haversine(loc, first) * k;
        if (r.return_to_start && first) dist += haversine(pts[pts.length - 1], first) * k;
        const dwell = r.kind === "track" ? 0 : r.stops.reduce((a, s) => a + (s.dwell_s || 0), 0), speed = Math.max(r.speed_kmh || 5, 0.5);
        return { secs: dist / (speed / 3.6) + dwell, loc: r.return_to_start ? first : pts[pts.length - 1], note: `${fmtDist(dist)} at ${speed} km/h${dwell ? ` + ${durText(dwell)} of pauses` : ""}` };
      }
      case "trick_or_treat": {
        if (!p.polygon && p.lat === undefined) return { secs: null, note: "Choose where to trick-or-treat" };
        const n = Number(p.houses) || 20, off = settings.tot_lights_off ? (settings.tot_lights_off_pct || 0) / 100 : 0, visit = Math.max(1, Math.round(n * (1 - off)));
        const lo = settings.tot_dwell_min_s ?? 20, hi = Math.max(settings.tot_dwell_max_s ?? 70, lo), kids = !!p.kids;
        const dwell = visit * (((lo + hi) / 2 + 7 + (p.realistic ? 10 : 0)) * (kids ? 1.6 : 1)), speed = kids ? 3.2 : 4.5;
        let back = 0; if (settings.tot_start_at_pin !== false) back = p.polygon ? 0.5 * Math.sqrt(Math.max(areaKm2(p.polygon), 0.002) * 1e6) : 0.5 * (Number(p.radius_m) || 350);
        const dist = (visit * 45 + back) * ROAD, start = p.polygon ? null : [p.lat, p.lon];
        return { secs: dist / (speed / 3.6) + dwell, loc: settings.tot_start_at_pin !== false && start ? start : undefined, note: `about ${visit} lit houses · ${fmtDist(dist)} walking + ${durText(dwell)} at doors` };
      }
    }
    return { secs: null, note: "" };
  }
  // steps: [{when:"at"|"after", time:"HH:MM", afterVal, afterUnit, startMs?, type, params}]. Returns [{start, secs, end, note, overlap}]
  function estimateSteps(steps, dateStr) {
    let loc = status?.simulation?.active ? [status.simulation.lat, status.simulation.lon] : null, day = new Date(`${dateStr}T00:00`), prevTime = "", prevEnd = null;
    return steps.map((st) => {
      let start = null;
      if (st.startMs != null) start = st.startMs;
      else if (st.when === "after") { const w = Math.round(Number(st.afterVal) * (st.afterUnit || 1)); start = prevEnd != null && Number.isFinite(w) ? prevEnd + w * 1000 : null; }
      else if (st.time && dateStr) { if (prevTime && st.time < prevTime) day.setDate(day.getDate() + 1); prevTime = st.time; start = new Date(`${localDate(day)}T${st.time}`).getTime(); }
      const e = estimateStep(st, loc); if (e.loc !== undefined) loc = e.loc;
      const end = start != null && e.secs != null ? start + e.secs * 1000 : null, overlap = prevEnd != null && start != null && prevEnd > start + 1000 ? prevEnd : null;
      prevEnd = end; return { start, secs: e.secs, end, note: e.note, overlap };
    });
  }
  const whenText = (ms, ref) => new Date(ms).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }) + (ref && new Date(ms).toDateString() !== new Date(ref).toDateString() ? ` (${new Date(ms).toLocaleDateString([], { weekday: "short" })})` : "");
  function estimateLine(e) {
    if (e.secs == null) return { text: `⏱ Can't estimate yet: ${e.note || "missing details"}`, cls: "unknown" };
    const dur = e.secs < 1 ? "instant" : `about ${durText(e.secs)}`;
    return { text: `⏱ ${dur}${e.end != null && e.secs >= 1 ? ` · done around ${whenText(e.end, e.start)}` : ""}${e.note && e.secs >= 1 ? ` · ${e.note}` : ""}`, cls: "" };
  }
  function refreshEstimates() {
    if (!editor) return;
    const est = estimateSteps(editor.steps, $("planDate").value || localDate()), rows = [...document.querySelectorAll("#planSteps .steprow")];
    est.forEach((e, i) => {
      const box = rows[i]?.querySelector(".estimate"); if (!box) return;
      const l = estimateLine(e); box.className = `estimate ${l.cls}`; box.replaceChildren(document.createTextNode(l.text));
      if (e.overlap != null) { const w = document.createElement("div"); w.className = "overlap"; w.textContent = `⚠ The step before is still running until about ${whenText(e.overlap, e.start)}, but this one starts at ${whenText(e.start)} and will take over.`; box.append(w); }
    });
    const starts = est.map((e) => e.start).filter((x) => x != null), ends = est.map((e) => e.end ?? e.start).filter((x) => x != null), unknown = est.some((e) => e.secs == null);
    $("planSummary").textContent = starts.length ? `🕒 The plan starts at ${whenText(Math.min(...starts))} and is done around ${whenText(Math.max(...ends), Math.min(...starts))}${unknown ? " (at least; some steps can't be estimated yet)" : ""}. Estimates use straight-line distances plus 30% for roads, so treat them as a guide.` : "";
  }
  let estTimer = null;
  const soonRefresh = () => { clearTimeout(estTimer); estTimer = setTimeout(refreshEstimates, 120); };
  $("planSteps").addEventListener("input", soonRefresh); $("planSteps").addEventListener("change", soonRefresh); $("planSteps").addEventListener("click", soonRefresh);
  $("planDate").addEventListener("input", soonRefresh);
  const STEP_TYPES = [["trick_or_treat", "🎃 Trick or treat"], ["move_to", "🚶 Walk / drive to…"], ["route", "🧭 Saved route"], ["set_location", "📍 Set location"], ["stop", "⏹ Stop moving"], ["clear", "↩ Restore real GPS"]];
  function renderEditorSteps() {
    $("planSteps").replaceChildren(...editor.steps.map((st, i) => {
      const row = document.createElement("div"); row.className = "steprow";
      // header: number, what it is, move / remove
      const head = document.createElement("div"); head.className = "stephdr";
      const no = document.createElement("span"); no.className = "stepno"; no.textContent = i + 1;
      const title = document.createElement("b"); title.textContent = STEP_TYPES.find((t) => t[0] === st.type)?.[1] || st.type;
      const tools = document.createElement("span"); tools.className = "steptools";
      const swap = (a, b) => { [editor.steps[a], editor.steps[b]] = [editor.steps[b], editor.steps[a]]; renderEditorSteps(); };
      const up = miniBtn("↑", "Move this step up", () => swap(i, i - 1)), down = miniBtn("↓", "Move this step down", () => swap(i, i + 1)); up.disabled = i === 0; down.disabled = i === editor.steps.length - 1;
      tools.append(up, down, miniBtn("✕", "Remove this step", () => { editor.steps.splice(i, 1); renderEditorSteps(); }));
      head.append(no, title, tools);
      // when
      const wsel = document.createElement("select"); wsel.className = "whensel"; wsel.setAttribute("aria-label", "When this step runs");
      [["at", "At a clock time"], ["after", "After the last move"]].forEach(([v, tx]) => { const o = document.createElement("option"); o.value = v; o.textContent = tx; wsel.append(o); });
      wsel.value = st.when; wsel.onchange = () => { st.when = wsel.value; if (st.when === "at" && !st.time) st.time = addMinutes(editor.steps[i - 1]?.time || "20:00", 30); renderEditorSteps(); };
      const t = document.createElement("input"); t.type = "time"; t.value = st.time; t.oninput = () => (st.time = t.value); t.hidden = st.when === "after"; t.setAttribute("aria-label", "Start time");
      const aft = document.createElement("span"); aft.className = "num"; aft.hidden = st.when !== "after";
      const av = document.createElement("input"); av.type = "number"; av.min = "1"; av.max = "86400"; av.step = "1"; av.value = st.afterVal; av.setAttribute("aria-label", "How long to wait"); av.oninput = () => (st.afterVal = Number(av.value));
      const au = document.createElement("select"); au.setAttribute("aria-label", "Seconds or minutes");
      [[1, "secs"], [60, "mins"]].forEach(([v, tx]) => { const o = document.createElement("option"); o.value = v; o.textContent = tx; au.append(o); });
      au.value = st.afterUnit; au.onchange = () => (st.afterUnit = Number(au.value)); aft.append(av, au);
      const whenBox = document.createElement("div"); whenBox.className = "whenbox"; whenBox.append(wsel, t, aft);
      const f1 = document.createElement("div"); f1.className = "field"; f1.append(Object.assign(document.createElement("span"), { className: "flabel", textContent: "When" }), whenBox);
      // action
      const sel = document.createElement("select"); sel.setAttribute("aria-label", "What this step does");
      STEP_TYPES.forEach(([v, tx]) => { const o = document.createElement("option"); o.value = v; o.textContent = tx; sel.append(o); });
      sel.value = st.type; sel.onchange = () => { st.type = sel.value; st.params = defaultParams(st.type); renderEditorSteps(); };
      const f2 = document.createElement("div"); f2.className = "field"; f2.append(Object.assign(document.createElement("span"), { className: "flabel", textContent: "Do" }), sel);
      const est = document.createElement("div"); est.className = "estimate";
      row.append(head, f1, f2, renderParams(st), est); return row;
    }));
    refreshEstimates();
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
      const rel = st.when === "after", waitS = Math.round(Number(st.afterVal) * (st.afterUnit || 1));
      if (rel) { if (!Number.isFinite(waitS) || waitS < 1 || waitS > 86400) throw { message: `Step ${i + 1}: type how long to wait (1 second to 24 hours).` }; }
      else {
        if (!st.time) throw { message: `Step ${i + 1} needs a time.` };
        if (prev && st.time < prev) day.setDate(day.getDate() + 1);          // after midnight: the next day
        prev = st.time;
      }
      const p = { ...st.params };
      if (st.type === "move_to" || st.type === "set_location") { if (p.lat === undefined) throw { message: `Step ${i + 1}: choose a place.`, hint: "Type an address and press Find." }; }
      if (st.type === "trick_or_treat" && !p.polygon && p.lat === undefined) throw { message: `Step ${i + 1}: choose where to trick-or-treat.`, hint: "Find an address, use the pin, or use your drawn area." };
      if (st.type === "route" && !p.route_id) throw { message: `Step ${i + 1}: choose a saved route.` };
      return rel ? { id: st.id, after_s: waitS, type: st.type, params: p } : { id: st.id, at: `${localDate(day)}T${st.time}`, type: st.type, params: p };
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
    try { const b = await api("GET", "/api/battery"); if (b.supported) msg += `  ${b.plugged ? "🔌" : "🔋"} Mac battery ${b.percent}%${b.charging ? " (charging)" : b.plugged ? " (plugged in)" : ""}.`; } catch { /* not important */ }
    $("awakeState").textContent = msg;
    const s = a.last_sleep, fresh = s && Date.now() / 1000 - s.woke < 900;               // show it for 15 minutes after waking
    $("sleepNote").hidden = !fresh;
    if (fresh) $("sleepNote").textContent = `💤 Your Mac was asleep for ${durationText(s.slept_s)} (until ${new Date(s.woke * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}). On iOS 17+ the phone returned to its real GPS meanwhile; trips and schedule steps paused. The app is reconnecting and restoring the location.`;
    if (status) nowBar(status);
  }
  setInterval(loadAwake, 5000);

  // ---------- status polling ----------
  // ---------- addresses: what is at a spot (reverse lookup, cached) ----------
  const addressCache = new Map(), pendingLookups = new Set();
  const addrKey = (lat, lon) => `${Number(lat).toFixed(4)},${Number(lon).toFixed(4)}`;
  const labelFor = (lat, lon) => addressCache.get(addrKey(lat, lon)) || null;
  function reverseLabel(lat, lon) {
    const k = addrKey(lat, lon);
    if (addressCache.has(k)) return Promise.resolve(addressCache.get(k) || null);
    addressCache.set(k, ""); pendingLookups.add(k);                  // "" = looking it up (or it failed)
    return api("GET", `/api/reverse?lat=${lat}&lon=${lon}`, undefined, { timeout: 12000 })
      .then((r) => { const l = r.label || shortName(r.name || ""); addressCache.set(k, l); return l || null; })
      .catch(() => { setTimeout(() => addressCache.delete(k), 30000); return null; })   // try again later (e.g. back online)
      .finally(() => pendingLookups.delete(k));
  }
  let pinTimer = null;
  function schedulePinLookup() {                                      // give a dropped pin an address in the background
    clearTimeout(pinTimer);
    pinTimer = setTimeout(async () => {
      if (!pin) return;
      const { lat, lng } = pin.getLatLng(), l = await reverseLabel(lat, lng);
      if (l && pin && addrKey(pin.getLatLng().lat, pin.getLatLng().lng) === addrKey(lat, lng)) { pinLabel = l; pinLine(); }
    }, 700);
  }

  // ---------- the progress dock: always shows what is going on with the location ----------
  const MODE_ICON = { walk: "🚶", drive: "🚗", straight: "➡️", gpx: "📈" }, MODE_NAME = { walk: "walking", drive: "driving", straight: "straight line", gpx: "track" };
  const timeText = (ms) => new Date(ms).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  function nowBar(s) {
    const w = s.walk || {}, sim = s.simulation || { active: false }, tot = w.kind === "trick_or_treat", dev = s.device;
    let state = "idle", icon = "🛰", title = "Using your real location", step = "", detail = dev ? `${dev.name} · iOS ${dev.ios_version} · your phone is using its real GPS.` : "Your phone is using its real GPS.", pct = null, eta = null;
    const speed = (w.cur_speed_kmh ?? w.speed_kmh ?? 0).toFixed(1);
    const coords = sim.active ? `${fmt4(sim.lat)}, ${fmt4(sim.lon)}` : "";
    const place = sim.active ? (sim.label || labelFor(sim.lat, sim.lon) || (w.active ? null : (reverseLabel(sim.lat, sim.lon).then((l) => l && status && nowBar(status)), null))) : null;
    if (s.state === "server_down") { state = "error"; icon = "⚠️"; title = "Can't reach the app"; detail = "The local server isn't answering. Quit the app and open it again."; }
    else if (sim.reconnecting || w.state === "reconnecting") {
      state = "reconnecting"; icon = "📡"; title = "Reconnecting to the iPhone…";
      detail = settings.auto_reconnect === false ? "Waiting for the iPhone." : "Waiting for the phone: the location and trip carry on by themselves when it is back.";
      if (w.active && w.progress != null) pct = w.progress;
    } else if (s.state === "connecting" && !sim.active) { state = "connecting"; icon = "🔌"; title = "Connecting to the iPhone…"; detail = "This can take a minute the first time (the disk image is mounted)."; }
    else if (s.state !== "ready" && !sim.active) { state = "idle"; icon = "🔌"; title = s.error?.message || "No iPhone connected"; detail = s.error?.hint || "Plug in an iPhone to begin."; }
    else if (w.error) { state = "error"; icon = "⚠️"; title = "The trip stopped"; detail = w.error.message + (w.error.hint ? ` ${w.error.hint}` : ""); }
    else if (w.active && w.state === "waiting") {
      state = "waiting"; pct = w.progress; eta = w.eta_s; step = w.total > 1 ? `${tot ? "house" : "stop"} ${w.stop}/${w.total}` : "";
      if (w.reason === "stop_sign") { icon = "🛑"; title = "Stopped at a stop sign"; }
      else if (w.reason === "traffic") { icon = "🚦"; title = "Stopped at a junction"; }
      else if (tot) { icon = "🎃"; title = `At the door of ${w.dest || "a house"}`; }
      else { icon = "⏸"; title = `Paused at ${w.dest || "a stop"}`; }
      detail = `Moving on in ${fmtSecs(w.wait_left_s)}${w.progress != null ? ` · ${fmtDist(w.done_m || 0)} of ${fmtDist(w.total_m)} done` : ""}`;
    } else if (w.active) {
      state = w.loop ? "looping" : "moving"; pct = w.progress; eta = w.eta_s; step = w.total > 1 ? `${tot ? "house" : "stop"} ${w.stop}/${w.total}` : "";
      icon = tot ? "🎃" : (MODE_ICON[w.cur_mode || w.mode] || "🚶");
      const dest = w.dest || (w.total > 1 ? "the next stop" : "the destination");
      title = w.returning ? "Returning to the start" : w.loop ? `Looping · heading to ${dest}` : `${tot ? "Walking to" : "Heading to"} ${dest}`;
      detail = (w.total_m ? `${fmtDist(w.done_m || 0)} of ${fmtDist(w.total_m)}` : `${fmtDist(w.remaining_m)} to the next stop`) + ` · ${speed} km/h · ${MODE_NAME[w.cur_mode || w.mode] || ""}${w.loop ? " · looping" : ""}`;
    } else if (sim.active && w.state === "done") {
      state = "done"; icon = "✅"; pct = 1; title = `Arrived${sim.label ? " at " + sim.label : ""}`; detail = `Trip finished · ${coords}`;
    } else if (sim.active) {
      state = "static"; icon = "📍"; pct = 1;
      title = place ? `Set to ${place}` : `Set to ${coords}`;
      detail = `${place ? coords + " · " : ""}${sim.since ? "since " + timeText(sim.since * 1000) : "fixed"}${place === null && !sim.label && pendingLookups.has(addrKey(sim.lat, sim.lon)) ? " · looking up the address…" : ""}`;
    }
    const dock = $("dock"); dock.dataset.state = state;
    $("dockIcon").textContent = icon; $("nowTitle").textContent = title; $("dockStep").textContent = step;
    $("nowDetail").textContent = detail + (awakeInfo?.active && state !== "idle" ? " · ☕ Mac kept awake" : "");
    const fill = pct == null ? 0 : Math.max(0, Math.min(1, pct)) * 100;
    $("dockfill").style.width = `${fill}%`; $("dockbar").setAttribute("aria-valuenow", String(Math.round(fill)));
    const etaBox = $("dockEta"); etaBox.hidden = eta == null;
    if (eta != null) etaBox.innerHTML = `<b>${eta <= 1 ? "arriving" : inText(Math.round(eta))}</b>${eta > 1 ? "arrives " + timeText(Date.now() + eta * 1000) : ""}`;
    $("stopBtn").hidden = !w.active; $("clearBtn").hidden = !sim.active;
    $("returnBtn").hidden = !(sim.origin && !w.active && haversine(sim.origin, [sim.lat, sim.lon]) > 3);   // not when already at the start
    const cd = sim.cooldown;
    $("cooldownNote").hidden = !(settings.cooldown_hint && cd);
    if (cd) $("cooldownNote").textContent = `⏱ Jump cooldown: ${clock(cd.remaining_s)} left · ${cd.km} km jump — a real trip would take about this long.`;
    $("awakeChip").hidden = !awakeInfo?.active;
    if (awakeInfo?.active) $("awakeChip").title = `The Mac is being kept awake: ${awakeInfo.reasons.join(", ")}`;
    document.dispatchEvent(new CustomEvent("locspoof:status", { detail: { status: s, state, awake: awakeInfo } }));
  }
  const simIcon = (who) => L.divIcon({ className: "", html: `<div class="sim-marker"><span class="who">${who}</span></div>`, iconSize: [22, 22], iconAnchor: [11, 11] });
  let lastMapTouch = 0;
  map.on("dragstart zoomstart", () => { lastMapTouch = Date.now(); });             // don't fight the user's own panning
  function render(s) {
    status = s;
    const chip = $("device"), banner = $("banner");
    chip.dataset.state = s.state;
    if (s.state === "ready") {
      const d = s.device;
      $("deviceText").textContent = `${knownNames[normUdid(d.udid)] || d.name} · iOS ${d.ios_version}${d.connection === "Wi-Fi" ? " · 📶" : ""}`;
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
      $("devBtn").hidden = s.error?.code !== "developer_mode_off";
    }
    const warns = s.warnings || [];                          // e.g. a leftover fake location AND an unmounted disk image
    $("notice").hidden = !warns.length;
    $("noticeMsg").textContent = warns.map((w) => `ℹ️ ${w.message}`).join("  ");
    $("noticeHint").textContent = warns.map((w) => w.hint).filter(Boolean).join("  ");
    const sim = s.simulation;
    if (sim.active) {
      const who = s.walk?.active ? (s.walk.cur_mode === "drive" ? "🚗" : s.walk.kind === "trick_or_treat" && settings.tot_kids ? "🧒" : "🚶") : "📍";
      if (!simMarker) simMarker = L.marker([sim.lat, sim.lon], { icon: simIcon(who), interactive: false, zIndexOffset: 5000, keyboard: false }).addTo(map);
      else simMarker.setLatLng([sim.lat, sim.lon]);
      if (simMarker._who !== who) { simMarker._who = who; simMarker.setIcon(simIcon(who)); }
      if (s.walk?.active && !map.getBounds().pad(-0.05).contains([sim.lat, sim.lon]) && Date.now() - lastMapTouch > 8000) map.panTo([sim.lat, sim.lon], { animate: true });
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


  // ---------- settings popover (appearance, map style, fun) ----------
  const pops = { settings: $("settingsPop"), palette: $("palette"), shortcuts: $("shortcuts") };
  const scrims = { palette: $("paletteScrim"), shortcuts: $("shortcutsScrim") };
  const isOpen = (n) => !pops[n].hidden;
  function openPop(n) {
    Object.keys(pops).forEach((k) => k !== n && closePop(k));
    pops[n].hidden = false; if (scrims[n]) scrims[n].hidden = false;
    if (n === "palette") { $("paletteInput").value = ""; paletteRender(); $("paletteInput").focus(); }
  }
  function closePop(n) { pops[n].hidden = true; if (scrims[n]) scrims[n].hidden = true; }
  const anyOpen = () => Object.keys(pops).some(isOpen);
  $("settingsBtn").onclick = (e) => { e.stopPropagation(); isOpen("settings") ? closePop("settings") : openPop("settings"); };
  document.addEventListener("pointerdown", (e) => { if (isOpen("settings") && !pops.settings.contains(e.target) && !$("settingsBtn").contains(e.target)) closePop("settings"); });
  Object.entries(scrims).forEach(([n, s]) => (s.onclick = () => closePop(n)));
  document.querySelectorAll("#modeSeg .segbtn").forEach((b) => (b.onclick = () => setUi("mode", b.dataset.colour)));
  document.querySelectorAll("#accentSwatches button").forEach((b) => (b.onclick = () => setUi("accent", b.dataset.accent)));
  document.querySelectorAll("#layerSeg .segbtn").forEach((b) => (b.onclick = () => showLayer(b.dataset.layer)));
  $("seasonalSwitch").onchange = (e) => setUi("seasonal", e.target.checked);
  $("funSwitch").onchange = (e) => setUi("fun", e.target.checked);
  $("openShortcuts").onclick = () => openPop("shortcuts");
  $("openPalette").onclick = () => openPop("palette");
  $("paletteBtn").onclick = () => openPop("palette");
  $("openSetup").onclick = () => { closePop("settings"); store.set("setup.hidden", ""); showTab("place"); renderSetup(true); };
  $("resetUi").onclick = () => { ["mode", "accent", "seasonal", "fun", "layer", "collapsed", "panelw"].forEach((k) => store.set(`ui.${k}`, "")); Object.assign(ui, { mode: "auto", accent: "blue", seasonal: true, fun: true }); document.documentElement.style.removeProperty("--panel-w"); showLayer("street"); toast("Look reset."); };

  // ---------- phone remote (shown inside Settings) ----------
  let remoteTimer = null;
  async function loadRemote() {
    let r; try { r = await api("GET", "/api/remote"); } catch { return; }
    $("remoteBox").hidden = !r.enabled;
    if (!r.enabled) return;
    $("remoteErr").hidden = !(r.error || !r.running);
    $("remoteErr").textContent = r.error ? `⚠️ ${r.error}` : !r.running ? "Starting…" : "";
    $("remoteUrls").replaceChildren(...(r.urls.length ? r.urls.map((u) => { const el = li(u.url); el.querySelector(".name").classList.add("mono"); el.title = "Click to copy"; el.onclick = () => { navigator.clipboard?.writeText(u.url); toast("Address copied."); };
      const tag = document.createElement("span"); tag.className = "badge"; tag.textContent = u.kind === "tailscale" ? "🌍 anywhere" : "🏠 home Wi-Fi"; el.append(tag); return el; }) : [li("Could not find this Mac's address. Is it on Wi-Fi?")]));
    $("remoteAway").hidden = !!(r.tailscale && r.tailscale.running);
    $("remoteCode").textContent = r.code ? `${r.code.code}  ·  ${Math.max(1, Math.round(r.code.expires_in / 60))} min` : "";
    $("remoteDevices").replaceChildren(...(r.devices.length ? r.devices.map((d) => li(`${d.online ? "🟢 " : ""}${d.name}${d.last_seen ? " · seen " + new Date(d.last_seen * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }) : ""}`)).map((el, i) => (el.append(miniBtn("✕", `Forget ${r.devices[i].name}`, async () => { if (!confirm(`Forget “${r.devices[i].name}”? It will have to pair again.`)) return; try { await api("DELETE", `/api/remote/devices/${r.devices[i].id}`); loadRemote(); } catch (e) { fail(e); } })), el)) : [li("No phones paired yet.")]));
  }
  $("remoteCodeBtn").onclick = async () => { try { await api("POST", "/api/remote/code"); loadRemote(); } catch (e) { fail(e); } };
  new MutationObserver(() => { clearInterval(remoteTimer); if (!$("settingsPop").hidden) { loadRemote(); remoteTimer = setInterval(loadRemote, 3000); } }).observe($("settingsPop"), { attributes: true, attributeFilter: ["hidden"] });

  // ---------- activity log ----------
  const dayName = (ts) => { const d = new Date(ts * 1000), t = new Date(); const y = new Date(Date.now() - 864e5);
    return d.toDateString() === t.toDateString() ? "Today" : d.toDateString() === y.toDateString() ? "Yesterday" : d.toLocaleDateString([], { weekday: "long", month: "short", day: "numeric" }); };
  async function loadActivity() {
    let events;
    try { events = (await api("GET", "/api/activity")).events; } catch (e) { return fail(e); }
    const ul = $("activityList"); ul.replaceChildren();
    if (!events.length) { const e = document.createElement("li"); e.className = "daybreak"; e.textContent = "Nothing has happened yet."; ul.append(e); return; }
    let day = "";
    for (const ev of events.slice().reverse()) {
      const dn = dayName(ev.ts);
      if (dn !== day) { day = dn; const b = document.createElement("li"); b.className = "daybreak"; b.textContent = dn; ul.append(b); }
      const row = document.createElement("li"); row.dataset.level = ev.level;
      const ts = document.createElement("span"); ts.className = "ts"; ts.textContent = new Date(ev.ts * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
      const tx = document.createElement("span"); tx.className = "tx"; tx.textContent = ev.text;
      if (ev.phone) { const p = document.createElement("span"); p.className = "ph"; p.textContent = ev.phone; tx.append(p); }
      row.append(ts, tx); ul.append(row);
    }
  }
  $("activityRefresh").onclick = loadActivity;
  $("activityClear").onclick = async () => { if (!confirm("Clear the activity log?")) return; try { await api("DELETE", "/api/activity"); loadActivity(); } catch (e) { fail(e); } };
  setInterval(() => { if (tab === "activity" && document.visibilityState !== "hidden") loadActivity(); }, 5000);

  // ---------- first-run checklist (top of the Place tab) ----------
  function renderSetup(force = false) {
    const card = $("setupCard"), s = status;
    if (!s) { card.hidden = true; return; }
    const ready = s.state === "ready", mounted = !ready || s.device?.ddi_mounted !== false, online = navigator.onLine !== false;
    const items = [
      [!!s.device || ready, "Connect an iPhone with a cable", "Unlock it and tap “Trust” if asked."],
      [s.state !== "not_trusted", "Trust this computer", "Press the button in the banner above."],
      [mounted, "Developer disk image ready", "The first connection can take a minute."],
      [online || ready, "Internet for searches and maps", "Saved offline areas work without it."],
    ];
    const done = items.every((i) => i[0]);
    if ((done && !force) || (store.get("setup.hidden") === "yes" && !force)) { card.hidden = true; return; }
    card.hidden = false; card.replaceChildren();
    const h = document.createElement("h3"); h.textContent = done ? "✅ All set" : "Getting started";
    const x = miniBtn("✕", "Hide this", () => { store.set("setup.hidden", "yes"); card.hidden = true; }); h.append(x); card.append(h);
    const ul = document.createElement("ul");
    for (const [ok, t, hint] of items) {
      const l = document.createElement("li"); if (!ok) l.className = "bad";
      l.innerHTML = `<span class="mk">${ok ? "✅" : "⬜"}</span><span><b></b><small></small></span>`;
      l.querySelector("b").textContent = t; l.querySelector("small").textContent = ok ? "" : hint; ul.append(l);
    }
    card.append(ul);
  }
  document.addEventListener("locspoof:status", () => { if (tab === "place") renderSetup(); });

  // ---------- command palette ----------
  const TABS = [["place", "📍", "Place"], ["move", "🚶", "Move"], ["tot", "🎃", "Trick or treat"], ["schedule", "⏰", "Schedule"], ["saved", "⭐", "Saved"], ["offline", "📥", "Offline"], ["phones", "📱", "Phones"], ["activity", "📜", "Activity"]];
  async function patchSetting(key, value) { try { settings = await api("PATCH", "/api/settings", { [key]: value }); applySettings(); toast(`${key.replace(/_/g, " ")}: ${value ? "on" : "off"}`); } catch (e) { fail(e); } }
  const act = (btn) => () => { const b = $(btn); if (b && !b.hidden && !b.disabled) b.click(); else toast("That isn't available right now."); };
  const commands = () => {
    const c = [];
    TABS.forEach(([t, ic, n], i) => c.push({ ic, t: `Go to ${n}`, hint: String(i + 1), run: () => showTab(t) }));
    c.push({ ic: "🚨", t: "PANIC: go to the panic location", hint: "Shift+P", run: panic });
    c.push({ ic: "⏹", t: "Stop moving", hint: "Space", run: act("stopBtn") }, { ic: "🛰", t: "Restore real GPS", hint: "R", run: act("clearBtn") }, { ic: "↩", t: "Return to start", hint: "B", run: act("returnBtn") });
    c.push({ ic: "🎯", t: "Centre map on the phone", hint: "L", run: centreOnPhone }, { ic: "✛", t: "Toggle crosshair", hint: "C", run: () => setCrosshair(!crosshairOn) },
      { ic: "🗺", t: "Next map style", hint: "M", run: cycleLayer }, { ic: "☰", t: "Hide / show the side card", hint: "\\", run: () => setPanelCollapsed(!document.body.classList.contains("panel-collapsed")) });
    c.push({ ic: "🌓", t: "Colour mode: auto", run: () => setUi("mode", "auto") }, { ic: "☀", t: "Colour mode: light", run: () => setUi("mode", "light") }, { ic: "🌙", t: "Colour mode: dark", run: () => setUi("mode", "dark") });
    c.push({ ic: "🎨", t: `Seasonal themes: turn ${ui.seasonal ? "off" : "on"}`, run: () => setUi("seasonal", !ui.seasonal) }, { ic: "🎉", t: `Fun mode: turn ${ui.fun ? "off" : "on"}`, run: () => setUi("fun", !ui.fun) });
    for (const [k, label] of [["keep_awake", "Keep the Mac awake"], ["auto_reconnect", "Auto-reconnect"], ["realistic_walk", "Realistic walking"]]) if (k in settings) c.push({ ic: "⚙", t: `${label}: turn ${settings[k] ? "off" : "on"}`, run: () => patchSetting(k, !settings[k]) });
    c.push({ ic: "⚙", t: "Settings", hint: ",", run: () => openPop("settings") }, { ic: "⌨", t: "Keyboard shortcuts", hint: "?", run: () => openPop("shortcuts") }, { ic: "✅", t: "Setup checklist", run: () => $("openSetup").click() });
    return c;
  };
  let palItems = [], palSel = 0, palSeq = 0;
  function paletteRender() {
    const q = $("paletteInput").value.trim(), ql = q.toLowerCase(), seq = ++palSeq;
    let items = commands().filter((c) => !ql || c.t.toLowerCase().includes(ql));
    const favs = [...document.querySelectorAll("#favs li .name")].map((e) => e.textContent);
    if (ql) {
      document.querySelectorAll("#favs li").forEach((li) => { const n = li.querySelector(".name")?.textContent || ""; if (n.toLowerCase().includes(ql)) items.push({ ic: "⭐", t: `Show favorite: ${n}`, run: () => li.querySelector(".name").click?.() || li.click() }); });
      routesCache.filter((r) => r.name.toLowerCase().includes(ql)).forEach((r) => items.push({ ic: "🛣", t: `Run saved route: ${r.name}`, run: async () => { try { const res = await api("POST", `/api/routes/${r.id}/start`, { jump_to_first: !status?.simulation?.active }); drawRoute(res.route); refresh(); toast(`Running “${r.name}”…`); } catch (e) { fail(e); } } }));
      items.push({ ic: "🔎", t: `Search for “${q}”`, hint: "Enter", run: () => { showTab("place"); $("searchInput").value = q; $("searchForm").requestSubmit?.(); } });
      document.dispatchEvent(new CustomEvent("locspoof:palette", { detail: { q: ql, add: (it) => { if (seq === palSeq) { items.unshift(it); draw(); } } } }));
    }
    palItems = items.slice(0, 40); palSel = 0; draw();
    function draw() {
      palItems = items.slice(0, 40);
      $("paletteList").replaceChildren(...palItems.map((it, i) => { const l = document.createElement("li"); l.setAttribute("role", "option"); if (i === palSel) l.className = "sel";
        l.innerHTML = '<span class="ic"></span><span class="t"></span><span class="hint"></span>'; l.querySelector(".ic").textContent = it.ic; l.querySelector(".t").textContent = it.t; l.querySelector(".hint").textContent = it.hint || "";
        l.onclick = () => runPalette(i); l.onmousemove = () => { palSel = i; markSel(); }; return l; }));
    }
  }
  function markSel() { [...$("paletteList").children].forEach((l, i) => { l.classList.toggle("sel", i === palSel); if (i === palSel) l.scrollIntoView?.({ block: "nearest" }); }); }
  function runPalette(i) { const it = palItems[i]; if (!it) return; closePop("palette"); try { const r = it.run(); if (r && r.catch) r.catch(fail); } catch (e) { fail(e); } }
  $("paletteInput").addEventListener("input", paletteRender);
  $("paletteInput").addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown") { e.preventDefault(); palSel = Math.min(palSel + 1, palItems.length - 1); markSel(); }
    else if (e.key === "ArrowUp") { e.preventDefault(); palSel = Math.max(palSel - 1, 0); markSel(); }
    else if (e.key === "Enter") { e.preventDefault(); runPalette(palSel); }
  });

  // ---------- keyboard shortcuts ----------
  const typing = (t) => t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.tagName === "SELECT" || t.isContentEditable);
  document.addEventListener("keydown", (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") { e.preventDefault(); isOpen("palette") ? closePop("palette") : openPop("palette"); return; }
    if (e.key === "Escape") { if (anyOpen()) { Object.keys(pops).forEach(closePop); return; } if (drawing) { cancelDraw(); return; } if (crosshairOn) { setCrosshair(false); return; } return; }
    if (e.metaKey || e.ctrlKey || e.altKey || typing(e.target) || anyOpen()) return;
    const k = e.key;
    if (k === "P") { panic(); return; }
    if (k >= "1" && k <= String(TABS.length)) showTab(TABS[Number(k) - 1][0]);
    else if (k === " ") { if (status?.walk?.active) { e.preventDefault(); $("stopBtn").click(); } }
    else if (k === "r" || k === "R") act("clearBtn")();
    else if (k === "b" || k === "B") act("returnBtn")();
    else if (k === "\\") setPanelCollapsed(!document.body.classList.contains("panel-collapsed"));
    else if (k === "/") { e.preventDefault(); showTab("place"); $("searchInput").focus(); }
    else if (k === "c" || k === "C") setCrosshair(!crosshairOn);
    else if (k === "l" || k === "L") centreOnPhone();
    else if (k === "m" || k === "M") cycleLayer();
    else if (k === ",") openPop("settings");
    else if (k === "?") openPop("shortcuts");
    else if (k === "Enter") { if (drawing && !$("finishBtn").disabled) finishDraw(); else if (crosshairOn) pinAtCenter(); }
  });

  // ---------- little extras the fun layer hooks into ----------
  let logoTaps = [];
  function logoClicks() {
    const now = Date.now(); logoTaps = logoTaps.filter((t) => now - t < 1500); logoTaps.push(now);
    document.dispatchEvent(new CustomEvent("locspoof:logo", { detail: { taps: logoTaps.length } }));
    if (logoTaps.length >= 5) logoTaps = [];
  }
  window.addEventListener("online", () => { $("netNote").hidden = true; }); window.addEventListener("offline", () => { $("netNote").hidden = false; });
  window.locspoof = { ui, toast, openPop, showTab, setUi, get status() { return status; } };

  (async function init() {
    try { settings = await api("GET", "/api/settings"); } catch { settings = {}; }
    applySettings(); applyCalifornia();
    await loadDevices(); loadAwake();
    (function loop() { refresh().finally(() => setTimeout(loop, status?.walk?.active ? 1000 : 3000)); })();
  })();

  showLayer(ui.layer); applyTheme();
  { const w = Number(store.get("ui.panelw")); if (w) setPanelWidth(w); }
  showTab(["place", "move", "tot", "schedule", "saved", "offline", "activity", "phones"].includes(store.get("tab")) ? store.get("tab") : "place");
  if (store.get("ui.collapsed") === "1" && !isMobile()) setPanelCollapsed(true);
  pinLine(); loadPanic(); loadFavs(); loadRecent(); loadRoutes(); renderStops(); renderArea(); showGpx();
})();
