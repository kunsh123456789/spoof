/* The phone remote: talks to the app on your Mac over the Wi-Fi. Everything here goes through the Mac's API with a pairing token. */
(() => {
  const $ = (id) => document.getElementById(id);
  const store = { get(k) { try { return localStorage.getItem(k); } catch { return null; } }, set(k, v) { try { localStorage.setItem(k, v); } catch { /* private mode */ } }, del(k) { try { localStorage.removeItem(k); } catch { /* ignore */ } } };
  let token = store.get("remote.token") || "", device = store.get("remote.device") || "", status = null, pin = null, simMarker = null, map = null, mode = "walk";
  let pinLabel = null, following = false, busy = false;
  const fmt4 = (n) => Number(n).toFixed(4);

  // ---------- talking to the Mac ----------
  async function api(method, path, body, opts = {}) {
    const headers = { Authorization: `Bearer ${token}` };
    if (body !== undefined) headers["Content-Type"] = "application/json";
    if (device) headers["X-Device"] = device;
    const ctl = new AbortController(), timer = setTimeout(() => ctl.abort(), opts.timeout || 20000);
    let res;
    try { res = await fetch(path, { method, headers, signal: ctl.signal, body: body === undefined ? undefined : JSON.stringify(body) }); }
    catch (e) { throw { message: e && e.name === "AbortError" ? "The Mac did not answer in time." : "Can't reach the Mac.", hint: "Is the Mac awake with the app open, and are you on its Wi-Fi (or is Tailscale turned on)?", offline: true }; }
    finally { clearTimeout(timer); }
    const data = await res.json().catch(() => ({}));
    if (res.status === 401) { showPair(); throw { message: "Not paired.", unauth: true }; }
    if (!res.ok) throw data.error || { message: `Error ${res.status}` };
    return data;
  }
  function toast(msg, ms = 3200) { const t = $("rtoast"); t.textContent = msg; t.hidden = false; clearTimeout(toast.t); toast.t = setTimeout(() => (t.hidden = true), ms); }
  const fail = (e) => { if (!e.unauth) toast(e.hint ? `${e.message} ${e.hint}` : e.message, 6000); };
  async function act(btn, fn) { if (busy) return; busy = true; btn && (btn.disabled = true); try { await fn(); } catch (e) { fail(e); } finally { busy = false; btn && (btn.disabled = false); poll(); } }

  // ---------- pairing ----------
  function showPair() { $("app").hidden = true; $("pair").hidden = false; token = ""; store.del("remote.token"); if (!$("pairName").value) $("pairName").value = /iPad/.test(navigator.userAgent) ? "My iPad" : "My iPhone"; }
  $("pairBtn").onclick = async () => {
    $("pairMsg").textContent = "";
    try {
      const r = await fetch("/api/remote/pair", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ code: $("pairCode").value.trim(), name: $("pairName").value.trim() }) });
      const d = await r.json().catch(() => ({}));
      if (!r.ok) throw d.error || { message: "Could not pair." };
      token = d.token; store.set("remote.token", token); start();
    } catch (e) { $("pairMsg").textContent = [e.message, e.hint].filter(Boolean).join(" ") || "Can't reach the Mac."; }
  };
  $("pairCode").addEventListener("input", () => { $("pairCode").value = $("pairCode").value.replace(/\D/g, "").slice(0, 6); if ($("pairCode").value.length === 6) $("pairBtn").click(); });

  // ---------- map ----------
  function initMap() {
    if (map) return;
    map = L.map("rmap", { zoomControl: false, attributionControl: false }).setView([37.2, -119.5], 6);
    L.tileLayer(`/tiles/{z}/{x}/{y}.png?t=${encodeURIComponent(token)}`, { maxZoom: 19 }).addTo(map);
    L.control.attribution({ position: "bottomleft", prefix: false }).addTo(map).addAttribution("© OpenStreetMap");
    map.on("click", (e) => setPin(e.latlng.lat, e.latlng.lng));
    map.on("dragstart", () => (following = false));
  }
  function setPin(lat, lon, label = null, pan = false) {
    if (!pin) { pin = L.marker([lat, lon], { draggable: true }).addTo(map); pin.on("dragend", () => { const p = pin.getLatLng(); setPin(p.lat, p.lng); }); } else pin.setLatLng([lat, lon]);
    pinLabel = label; pinInfo(); if (pan) map.setView([lat, lon], Math.max(map.getZoom(), 15));
    if (!label) lookup(lat, lon);
  }
  async function lookup(lat, lon) { try { const r = await api("GET", `/api/reverse?lat=${lat}&lon=${lon}`, undefined, { timeout: 8000 }); const p = pin && pin.getLatLng(); if (p && Math.abs(p.lat - lat) < 1e-7 && Math.abs(p.lng - lon) < 1e-7 && r.label) { pinLabel = r.label; pinInfo(); } } catch { /* offline: coordinates are fine */ } }
  function pinInfo() { $("pinInfo").textContent = pin ? `📍 ${pinLabel ? pinLabel + " · " : ""}${fmt4(pin.getLatLng().lat)}, ${fmt4(pin.getLatLng().lng)}` : "Tap the map to drop a pin."; }
  $("rlocate").onclick = () => { const s = status?.simulation; if (s?.active) { following = true; map.flyTo([s.lat, s.lon], Math.max(map.getZoom(), 16)); } else toast("The iPhone is using its real location."); };

  // ---------- status ----------
  const fmtDist = (m) => (m >= 1000 ? `${(m / 1000).toFixed(1)} km` : `${Math.round(m)} m`);
  const inText = (s) => (s < 60 ? `${Math.round(s)} s` : s < 5400 ? `${Math.round(s / 60)} min` : `${(s / 3600).toFixed(1)} h`);
  function render(s) {
    status = s; const w = s.walk || {}, sim = s.simulation || {}; let title = "Using the real location", sub = s.device ? `${s.device.name} · real GPS${realArea ? " · 📍 near " + realArea.label : ""}` : "", pct = 0, dot = "ok";
    if (s.state !== "ready" && !sim.active) { title = s.error?.message || "No iPhone connected"; sub = s.error?.hint || "Plug the iPhone into the Mac."; dot = s.state === "connecting" ? "busy" : "bad"; }
    else if (sim.reconnecting || w.state === "reconnecting") { title = "Reconnecting to the iPhone…"; dot = "busy"; }
    else if (w.active) {
      const dest = w.dest || "the destination"; pct = (w.progress || 0) * 100; dot = "busy";
      title = w.state === "waiting" ? (w.reason === "stop_sign" ? "🛑 Stop sign" : w.reason === "traffic" ? "🚦 Waiting at a light" : `⏸ At ${dest}`) : `${w.cur_mode === "drive" ? "🚗" : "🚶"} Heading to ${dest}`;
      sub = `${fmtDist(w.done_m || 0)} of ${fmtDist(w.total_m || 0)}${w.eta_s ? " · " + inText(w.eta_s) + " left" : ""}`;
    } else if (sim.active) { title = `📍 ${sim.label || `${fmt4(sim.lat)}, ${fmt4(sim.lon)}`}`; sub = sim.label ? `${fmt4(sim.lat)}, ${fmt4(sim.lon)}` : "Location is set"; pct = 100; }
    $("rtitle").textContent = title; $("rsub").textContent = sub; $("rdot").className = `dot ${dot}`; $("rfill").style.width = `${pct}%`;
    if (sim.active) {
      if (!simMarker) { simMarker = L.marker([sim.lat, sim.lon], { icon: L.divIcon({ className: "", html: '<div class="sim"></div>', iconSize: [22, 22] }), interactive: false, zIndexOffset: 1000 }).addTo(map); if (!pin) map.setView([sim.lat, sim.lon], 15); }
      else simMarker.setLatLng([sim.lat, sim.lon]);
      if (following) map.panTo([sim.lat, sim.lon], { animate: false });
    } else if (simMarker) { simMarker.remove(); simMarker = null; }
    $("returnBtn").disabled = !sim.origin || w.active;
  }
  // where the Mac is (roughly where the iPhone really is), when nothing is being spoofed
  let realArea = null, realDot = null;
  async function loadArea() {
    try { const w = await api("GET", "/api/where", undefined, { timeout: 10000 }); realArea = w.ok ? w : null; } catch { /* offline */ }
    if (realArea && map && !status?.simulation?.active) { if (!realDot) realDot = L.circleMarker([realArea.lat, realArea.lon], { radius: 8, color: "#fff", weight: 3, fillColor: "#2f80ed", fillOpacity: 1, interactive: false }).addTo(map); }
    if (status?.simulation?.active && realDot) { realDot.remove(); realDot = null; }
    setTimeout(loadArea, 300000);
  }
  let pollTimer = null;
  async function poll() {
    clearTimeout(pollTimer);
    try { render(await api("GET", "/api/status", undefined, { timeout: 8000 })); renderMonitor(); } catch (e) { if (e.unauth) return; renderMonitor(e); $("rtitle").textContent = "Can't reach the Mac"; $("rsub").textContent = e.hint || ""; $("rdot").className = "dot bad"; }
    pollTimer = setTimeout(poll, status?.walk?.active ? 1000 : 2500);
  }

  // ---------- keeping an eye on the plan: health, alerts, plans, recent activity ----------
  let plans = [], nextEv = null, awake = null, events = [], lastAlertSig = "", batt = null;
  const clockText = (at) => new Date(at).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  const STEP_BADGE = { pending: "⏳ waiting", running: "▶ running", done: "✅ done", error: "⚠ failed", missed: "🕘 missed", skipped: "⏭ skipped" };
  const stepTitle = (s) => s.title || ({ trick_or_treat: "Trick or treat", move_to: "Walk / drive", route: "Saved route", set_location: "Set location", clear: "Restore real GPS", stop: "Stop moving" })[s.type] || s.type;
  async function monitor() {
    try {
      const [sc, aw, ac, bt] = await Promise.all([api("GET", "/api/schedules", undefined, { timeout: 8000 }), api("GET", "/api/awake", undefined, { timeout: 8000 }), api("GET", "/api/activity", undefined, { timeout: 8000 }), api("GET", "/api/battery", undefined, { timeout: 8000 })]);
      plans = sc.plans; nextEv = sc.next; awake = aw; batt = bt; events = ac.events.slice(-8).reverse(); renderMonitor();
    } catch (e) { if (!e.unauth) renderMonitor(e); }
    clearTimeout(monitor.t); monitor.t = setTimeout(monitor, 5000);
  }
  function problems(offlineErr) {
    const out = [], s = status || {}, w = s.walk || {}, sim = s.simulation || {};
    if (offlineErr) out.push("Can't reach the Mac. Is it awake with the app open, and is this phone on its Wi-Fi (or Tailscale)?");
    if (s.state && s.state !== "ready" && !sim.active) out.push(`iPhone: ${s.error?.message || "not connected"}${s.error?.hint ? " " + s.error.hint : ""}`);
    if (sim.reconnecting || w.state === "reconnecting") out.push("The iPhone dropped off. The app is reconnecting…");
    if (w.error) out.push(`The trip stopped: ${w.error.message}`);
    const armed = plans.filter((p) => p.armed);
    armed.forEach((p) => p.steps.forEach((st) => { if (st.status === "error") out.push(`Plan “${p.name}”: ${stepTitle(st)} failed. ${st.note || ""}`); else if (st.status === "missed") out.push(`Plan “${p.name}”: ${stepTitle(st)} was missed.`); }));
    if (armed.length && awake && awake.supported && !awake.enabled) out.push("“Keep the Mac awake” is off: the Mac may sleep and stop the plan.");
    if (batt?.supported && batt.plugged === false && batt.percent <= 20) out.push(`The Mac's battery is at ${batt.percent}%${batt.minutes_left ? ` (about ${Math.round(batt.minutes_left / 60 * 10) / 10} h left)` : ""} and it is not plugged in. If it runs flat the Mac sleeps and the location stops.`);
    if (awake?.last_sleep && Date.now() / 1000 - awake.last_sleep.woke < 900) out.push(`The Mac was asleep for ${Math.round(awake.last_sleep.slept_s / 60)} min (until ${clockText(awake.last_sleep.woke * 1000)}).`);
    return out;
  }
  function chip(cls, text) { const c = document.createElement("span"); c.className = `chip ${cls}`; c.textContent = text; return c; }
  function renderMonitor(offlineErr) {
    const s = status || {}, probs = problems(offlineErr), armed = plans.filter((p) => p.armed);
    const phoneOk = s.state === "ready";
    $("health").replaceChildren(
      chip(offlineErr ? "bad" : "ok", offlineErr ? "🔗 Mac unreachable" : "🔗 Mac connected"),
      chip(phoneOk ? "ok" : "bad", phoneOk ? `📱 ${s.device?.name || "iPhone"}${s.device?.connection === "Wi-Fi" ? " (Wi-Fi)" : ""}` : "📱 iPhone not ready"),
      awake ? chip(awake.active ? "ok" : (awake.enabled ? "ok" : "warn"), awake.active ? "☕ Mac kept awake" : awake.enabled ? "☕ Keep-awake on" : "☕ Mac may sleep") : chip("warn", "☕ …"),
      ...(batt?.supported ? [chip(batt.plugged ? "ok" : batt.percent <= 10 ? "bad" : batt.percent <= 20 ? "warn" : "ok", `${batt.plugged ? "🔌" : "🔋"} Mac ${batt.percent}%${batt.charging ? " charging" : batt.plugged ? " plugged in" : ""}`)] : []),
      chip(armed.length ? "ok" : "warn", armed.length ? `⏰ ${armed.length} plan${armed.length > 1 ? "s" : ""} on` : "⏰ no plan on"));
    $("alerts").hidden = !probs.length; $("alerts").replaceChildren(...probs.map((p) => Object.assign(document.createElement("div"), { textContent: "⚠ " + p })));
    const bar = $("alertbar"); bar.hidden = !probs.length || $("p-status").hidden === false; if (probs.length) bar.textContent = `⚠ ${probs[0]}${probs.length > 1 ? `  (+${probs.length - 1} more)` : ""}`;
    const sig = probs.join("|"); if (sig && sig !== lastAlertSig) navigator.vibrate?.([120, 60, 120]); lastAlertSig = sig;
    $("nextUp").textContent = nextEv ? `⏰ ${clockText(nextEv.at)} (${inText(Math.max(0, nextEv.in_s))}) — ${nextEv.title || nextEv.type.replace("_", " ")} · ${nextEv.plan_name}` : (armed.length ? "Active plans have nothing left to run." : "No plan is switched on.");
    $("plans").replaceChildren(...(plans.length ? plans.map((p) => {
      const card = document.createElement("div"); card.className = "plan";
      const head = document.createElement("div"); head.className = "ph"; const nm = document.createElement("b"); nm.textContent = p.name;
      const sw = document.createElement("label"); sw.className = "sw"; const cb = document.createElement("input"); cb.type = "checkbox"; cb.checked = !!p.armed; sw.append(cb, document.createElement("i"));
      cb.onchange = () => act(null, async () => { try { await api("POST", `/api/schedules/${p.id}/arm`, { armed: cb.checked }); toast(cb.checked ? "Plan switched on." : "Plan switched off."); } catch (e) { cb.checked = !cb.checked; throw e; } monitor(); });
      head.append(nm, sw); card.append(head);
      p.steps.forEach((st) => {
        const row = document.createElement("div"); row.className = "step";
        const when = document.createElement("span"); when.className = "when"; when.textContent = st.after_s != null ? `+${st.after_s >= 60 ? Math.round(st.after_s / 60) + "m" : st.after_s + "s"}` : clockText(st.at);
        const what = document.createElement("span"); what.className = "what"; what.textContent = stepTitle(st); if (st.note) { const n = document.createElement("small"); n.textContent = st.note; what.append(n); }
        const b = document.createElement("span"); b.className = `badge ${st.status}`; b.textContent = STEP_BADGE[st.status] || st.status; row.append(when, what, b);
        if (st.status === "pending" || st.status === "error" || st.status === "missed") { const go = document.createElement("button"); go.className = "sec go"; go.textContent = "⏵"; go.setAttribute("aria-label", "Run this step now");
          go.onclick = () => { if (!confirm(`Run “${stepTitle(st)}” right now?`)) return; act(go, async () => { const r = await api("POST", `/api/schedules/${p.id}/steps/${st.id}/run`); toast(r.note || "Done."); monitor(); }); }; row.append(go); }
        card.append(row);
      });
      return card;
    }) : [Object.assign(document.createElement("p"), { className: "hint", textContent: "No plans yet. Create them in the app on the Mac (Schedule tab)." })]));
    $("activity").replaceChildren(...(events.length ? events.map((e) => { const el = document.createElement("li"); el.className = e.level; const t = document.createElement("span"); t.className = "t"; t.textContent = new Date(e.ts * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
      const x = document.createElement("span"); x.className = "name"; x.style.whiteSpace = "normal"; x.textContent = e.text; el.append(t, x); return el; }) : [Object.assign(document.createElement("li"), { className: "empty", textContent: "Nothing yet." })]));
    const w = s.walk || {};
    $("liveWrap").hidden = !w.active; if (w.active && document.activeElement !== $("liveSpeed")) { $("liveSpeed").value = Math.round(w.speed_kmh || 5); $("liveSpeedOut").textContent = $("liveSpeed").value; }
  }
  $("liveSpeed").oninput = () => ($("liveSpeedOut").textContent = $("liveSpeed").value);
  $("liveSpeed").onchange = () => act(null, async () => { await api("PATCH", "/api/journey", { speed_kmh: Number($("liveSpeed").value) }); toast(`Speed changed to ${$("liveSpeed").value} km/h.`); });
  $("alertbar").onclick = () => $("rtabs").querySelector('[data-p="status"]').click();

  // ---------- buttons ----------
  $("stopBtn").onclick = (e) => act(e.currentTarget, async () => { await api("DELETE", "/api/journey"); toast("Stopped. The iPhone stays where it is."); });
  $("returnBtn").onclick = (e) => act(e.currentTarget, async () => { await api("POST", "/api/journey/return"); toast("Heading back to the start."); });
  $("restoreBtn").onclick = (e) => act(e.currentTarget, async () => { await api("DELETE", "/api/location"); toast("Real GPS restored."); });
  $("rpanic").onclick = (e) => act(e.currentTarget, async () => { await api("POST", "/api/panic/go"); navigator.vibrate?.(60); toast("🚨 Moved to the panic location."); });
  $("setBtn").onclick = (e) => act(e.currentTarget, async () => {
    if (!pin) throw { message: "Tap the map (or pick a place) first." };
    const { lat, lng } = pin.getLatLng(); await api("POST", "/api/location", { lat, lon: lng, label: pinLabel }); toast(`Location set${pinLabel ? " to " + pinLabel : ""}.`); loadLists();
  });
  $("goBtn").onclick = (e) => act(e.currentTarget, async () => {
    if (!pin) throw { message: "Tap the map (or pick a place) first." };
    const { lat, lng } = pin.getLatLng(), jump = !status?.simulation?.active;
    await api("POST", "/api/journey", { stops: [{ lat, lon: lng, label: pinLabel }], mode, speed_kmh: Number($("tripSpeed").value), jump_to_first: false });
    toast(`Heading to ${pinLabel || "the pin"}…`);
  });
  document.querySelectorAll("#modeSeg button").forEach((b) => (b.onclick = () => { mode = b.dataset.m; document.querySelectorAll("#modeSeg button").forEach((x) => x.classList.toggle("on", x === b)); }));
  $("tripSpeed").oninput = () => ($("tripSpeedOut").textContent = $("tripSpeed").value);
  document.querySelectorAll("#rtabs button").forEach((b) => (b.onclick = () => { document.querySelectorAll("#rtabs button").forEach((x) => x.classList.toggle("on", x === b)); document.querySelectorAll(".pane").forEach((p) => (p.hidden = p.id !== `p-${b.dataset.p}`)); renderMonitor(); }));

  // ---------- places ----------
  const li = (text, onClick, ...btns) => { const el = document.createElement("li"); const s = document.createElement("span"); s.className = "name"; s.textContent = text; el.append(s, ...btns); if (onClick) el.onclick = onClick; return el; };
  $("searchForm").onsubmit = async (e) => {
    e.preventDefault(); const q = $("q").value.trim(); if (q.length < 2) return;
    try {
      const { results } = await api("GET", `/api/search?q=${encodeURIComponent(q)}`); const ul = $("results"); ul.hidden = false;
      ul.replaceChildren(...(results.length ? results.map((r) => li(r.name, () => { setPin(r.lat, r.lon, r.name.split(",").slice(0, 2).join(",").trim(), true); ul.hidden = true; })) : [Object.assign(li("Nothing found."), { className: "empty" })]));
    } catch (err) { fail(err); }
  };
  async function loadLists() {
    try {
      const { favorites } = await api("GET", "/api/favorites"), { recent } = await api("GET", "/api/recent"), { routes } = await api("GET", "/api/routes");
      const place = (p) => li(p.name || `${fmt4(p.lat)}, ${fmt4(p.lon)}`, () => setPin(p.lat, p.lon, p.name || null, true));
      $("favs").replaceChildren(...(favorites.length ? favorites.map(place) : [Object.assign(li("No favorites yet (add them on the Mac)."), { className: "empty" })]));
      $("recent").replaceChildren(...(recent.length ? recent.map(place) : [Object.assign(li("Nothing yet."), { className: "empty" })]));
      $("routes").replaceChildren(...(routes.length ? routes.map((r) => li(r.name, null, Object.assign(document.createElement("button"), { textContent: "▶", onclick: () => act(null, async () => { await api("POST", `/api/routes/${r.id}/start`, { jump_to_first: !status?.simulation?.active }); toast(`Running “${r.name}”…`); }) }))) : [Object.assign(li("No saved routes yet (save them on the Mac)."), { className: "empty" })]));
    } catch (e) { fail(e); }
  }

  // ---------- phones ----------
  async function loadPhones() {
    try {
      const { devices } = await api("GET", "/api/devices"), sel = $("rphone");
      sel.hidden = devices.length < 2; sel.replaceChildren(...devices.map((d) => Object.assign(document.createElement("option"), { value: d.udid, textContent: `${d.favorite ? "★ " : ""}${d.display_name || d.name}` })));
      if (device && devices.some((d) => d.udid === device)) sel.value = device; else if (devices.length) device = sel.value = devices[0].udid;
    } catch { /* the status line tells what is wrong */ }
  }
  $("rphone").onchange = () => { device = $("rphone").value; store.set("remote.device", device); if (simMarker) { simMarker.remove(); simMarker = null; } poll(); };

  async function start() {
    $("pair").hidden = true; $("app").hidden = false; initMap();
    try { await api("GET", "/api/remote/me"); } catch (e) { if (e.unauth) return; }
    loadPhones(); loadLists(); poll(); monitor(); loadArea(); setTimeout(() => map.invalidateSize(), 200);
  }
  window.addEventListener("resize", () => map && map.invalidateSize());
  document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible" && token) poll(); });
  if (token) start(); else showPair();
})();
