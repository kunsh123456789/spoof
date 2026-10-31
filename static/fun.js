/* Fun mode: confetti, the candy counter, seasonal weather, the little Mac and some hidden surprises.
   It only listens to events from app.js (locspoof:ui / :status / :logo / :palette) and never touches the real work. */
(() => {
  const root = document.documentElement, $ = (id) => document.getElementById(id);
  const reduce = !!(window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches);
  let fun = true, skin = "", dark = false, last = null, awake = null;

  // ---------- the effects canvas ----------
  const cv = document.createElement("canvas"); cv.id = "fx"; cv.setAttribute("aria-hidden", "true"); document.body.append(cv);
  const ctx = cv.getContext("2d"); let W = 0, H = 0;
  function size() { const d = Math.min(window.devicePixelRatio || 1, 2); W = innerWidth; H = innerHeight; cv.width = W * d; cv.height = H * d; ctx.setTransform(d, 0, 0, d, 0, 0); }
  size(); addEventListener("resize", size);
  const parts = []; let running = false, lastT = 0, ambientUntil = 0, ambientKind = "";
  const COLORS = ["#ff5252", "#ffb300", "#ffee58", "#66bb6a", "#42a5f5", "#ab47bc", "#ff7a00", "#ec4899"];
  const rnd = (a, b) => a + Math.random() * (b - a);
  function confetti(x, y, n = 70, spread = 1) {
    if (!fun || reduce) return;
    for (let i = 0; i < n; i++) { const a = rnd(-Math.PI, 0), v = rnd(180, 520) * spread;
      parts.push({ k: "c", x, y, vx: Math.cos(a) * v, vy: Math.sin(a) * v, w: rnd(5, 10), h: rnd(3, 6), rot: rnd(0, 6), vr: rnd(-9, 9), col: COLORS[i % COLORS.length], ttl: rnd(2.2, 3.6), age: 0 }); }
    kick();
  }
  function rain(n = 120) {                                   // confetti falling from the whole top edge
    if (!fun || reduce) return;
    for (let i = 0; i < n; i++) parts.push({ k: "c", x: rnd(0, W), y: rnd(-H * .4, -10), vx: rnd(-40, 40), vy: rnd(40, 200), w: rnd(5, 10), h: rnd(3, 6), rot: rnd(0, 6), vr: rnd(-7, 7), col: COLORS[i % COLORS.length], ttl: rnd(3, 5), age: 0 });
    kick();
  }
  function emojiBurst(glyph, n = 24) {
    if (!fun || reduce) return;
    for (let i = 0; i < n; i++) parts.push({ k: "e", g: glyph, x: rnd(0, W), y: H + 20, vx: rnd(-30, 30), vy: rnd(-260, -120), s: rnd(18, 34), rot: 0, vr: rnd(-1, 1), ttl: rnd(3, 5), age: 0, sway: rnd(0, 6) });
    kick();
  }
  const AMBIENT = { winter: { g: "❄", rate: 6 }, hearts: { g: "♥", col: "#ff5c8d", rate: 3, up: true }, halloween: { g: "🍂", rate: 1.1 } };
  function ambient() { return !reduce && fun && skin && AMBIENT[skin] ? skin : ambientUntil > Date.now() ? ambientKind : ""; }
  function spawnAmbient(kind, dt) {
    const a = AMBIENT[kind]; if (!a) return; const col = kind === "winter" ? (dark ? "#e8f4ff" : "#7fb3e0") : a.col;
    if (Math.random() < a.rate * dt) {
      const up = a.up, s = rnd(10, 24);
      parts.push({ k: "a", g: a.g, col, x: rnd(0, W), y: up ? H + 20 : -20, vx: rnd(-15, 15), vy: (up ? -1 : 1) * rnd(28, 70), s, rot: 0, vr: rnd(-1, 1), ttl: 30, age: 0, sway: rnd(0, 6), alpha: rnd(.45, .9) });
    }
  }
  function kick() { if (!running) { running = true; lastT = performance.now(); requestAnimationFrame(frame); } }
  function frame(t) {
    const dt = Math.min((t - lastT) / 1000, 0.05); lastT = t;
    ctx.clearRect(0, 0, W, H);
    const amb = ambient(); if (amb) spawnAmbient(amb, dt);
    for (let i = parts.length - 1; i >= 0; i--) {
      const p = parts[i]; p.age += dt;
      if (p.k === "c") { p.vy += 900 * dt; p.vx *= 1 - 1.4 * dt; p.vy *= 1 - 0.6 * dt; }
      if (p.k !== "c") p.x += Math.sin(p.age * 1.6 + p.sway) * 18 * dt;
      p.x += p.vx * dt; p.y += p.vy * dt; p.rot += p.vr * dt;
      const gone = p.age > p.ttl || p.y > H + 40 || p.y < -60 || (p.k === "a" && !amb);
      if (gone) { parts.splice(i, 1); continue; }
      ctx.save(); ctx.translate(p.x, p.y); ctx.rotate(p.rot);
      ctx.globalAlpha = p.k === "a" ? (p.alpha || 1) : Math.max(0, Math.min(1, (p.ttl - p.age) * 2));
      if (p.k === "c") { ctx.fillStyle = p.col; ctx.fillRect(-p.w / 2, -p.h / 2, p.w, p.h * Math.abs(Math.cos(p.age * 7 + p.rot))); }
      else { ctx.font = `${p.s}px system-ui, "Apple Color Emoji", sans-serif`; ctx.textAlign = "center"; ctx.textBaseline = "middle"; ctx.fillStyle = p.col || "#000"; ctx.fillText(p.g, 0, 0); }
      ctx.restore();
    }
    if (parts.length || amb) requestAnimationFrame(frame); else { running = false; ctx.clearRect(0, 0, W, H); }
  }
  document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible" && (parts.length || ambient())) kick(); });

  // ---------- the little Mac ----------
  const mascot = document.createElement("div"); mascot.id = "mascot"; mascot.dataset.state = "idle"; mascot.setAttribute("role", "img"); mascot.setAttribute("aria-label", "A friendly little Mac. Click it!");
  mascot.innerHTML = '<div class="mac"><div class="screen"><i class="eye l"></i><i class="eye r"></i><b class="mouth"></b></div><div class="stand"></div><div class="base"></div></div><span class="hat"></span><span class="cup">☕</span><span class="zzz">z<small>z</small></span>';
  document.body.append(mascot);
  const HATS = { halloween: "🎃", winter: "🎅", hearts: "💝" };
  let forced = "", forcedTimer = null, bubbleTimer = null;
  function mascotState() {
    const slept = awake && awake.last_sleep && Date.now() / 1000 - awake.last_sleep.woke < 300;
    return forced || (slept ? "sleep" : awake && awake.active ? "coffee" : last && last.walk && last.walk.active ? "walk" : "idle");
  }
  function paintMascot() { mascot.dataset.state = mascotState(); mascot.querySelector(".hat").textContent = HATS[skin] || ""; }
  function say(text, ms = 4200) {
    mascot.querySelector(".bubble")?.remove(); clearTimeout(bubbleTimer);
    const b = document.createElement("div"); b.className = "bubble"; b.textContent = text; mascot.append(b);
    bubbleTimer = setTimeout(() => b.remove(), ms);
  }
  function force(state, ms) { forced = state; paintMascot(); clearTimeout(forcedTimer); forcedTimer = setTimeout(() => { forced = ""; paintMascot(); }, ms); }
  const QUIPS = {
    sleep: ["zzz… five more minutes…", "Oh! You woke me. The Mac had a nap too."],
    coffee: ["Mmm, coffee. I'm keeping the Mac awake. ☕", "Sipping keeps the location moving!"],
    walk: ["Left, right, left, right…", "Are we there yet?", "Nice day for a walk."],
    idle: ["Hi! I'm Mac-y. 👋", "Tip: press ⌘K / Ctrl+K for the command palette.", "Pssst… try the Konami code.", "Press ? to see every shortcut.", "I don't judge where you've been. 😉"],
  };
  mascot.addEventListener("click", () => {
    const st = mascotState(), list = QUIPS[st] || QUIPS.idle; say(list[Math.floor(Math.random() * list.length)]);
    mascotClicks = mascotClicks.filter((t) => Date.now() - t < 2500); mascotClicks.push(Date.now());
    if (mascotClicks.length >= 7) { mascotClicks = []; force("dance", 4000); say("Whee! 💃"); confetti(innerWidth - 50, innerHeight - 120, 80); }
  });
  let mascotClicks = [];

  // ---------- candy counter + tour finished ----------
  let prev = null, candy = 0;
  function onStatus(s) {
    const w = s.walk || {}, sim = s.simulation || {}, tot = w.kind === "trick_or_treat", box = $("candy");
    if (box) {
      const show = fun && tot && (w.active || candy > 0 && prev?.active);
      if (w.active && tot) { const n = Math.max(0, (w.stop || 1) - (w.state === "waiting" ? 0 : 1)); if (n !== candy) { candy = n; $("candyN").textContent = n; box.classList.remove("bump"); void box.offsetWidth; box.classList.add("bump"); if (n > 0) emojiBurst("🍬", 4); } }
      if (!w.active && !tot) { /* keep the final number a little while after a tour */ }
      box.hidden = !(fun && (tot && w.active || candyHold > Date.now()));
    }
    if (prev && prev.active && !w.active && w.state === "done" && !w.error) {
      if (tot) candyHold = Date.now() + 20000;
      celebrate(tot ? `🎃 Trick-or-treat done — ${candy} house${candy === 1 ? "" : "s"}, so much candy!` : "🎉 Arrived! Trip finished.");
    }
    if (box) box.hidden = !(fun && (tot && w.active || candyHold > Date.now()));
    prev = { active: !!w.active };
    if (!w.active && !(tot)) candy = 0;
    if (sim.active) places(sim);
    awake = s.awake || awake;
  }
  let candyHold = 0;
  function celebrate(msg) {
    if (!fun) return;
    confetti(innerWidth * .3, innerHeight * .75, 80); confetti(innerWidth * .7, innerHeight * .75, 80);
    force("dance", 3500); say(msg, 6000);
  }
  let placeSeen = "";
  function places(sim) {                                      // small surprises for special spots
    const key = sim.lat < 1 && sim.lat > -1 && sim.lon < 1 && sim.lon > -1 ? "null" : sim.lat > 89.5 ? "pole" : sim.lat < -89.5 ? "south" : "";
    if (key === placeSeen) return; placeSeen = key; if (!key || !fun) return;
    const msg = { null: "🏝 Welcome to Null Island — population: one weather buoy.", pole: "🎅 Hello from the North Pole! Every direction is south.", south: "🐧 The South Pole! Every direction is north." }[key];
    say(msg, 7000); window.locspoof?.toast(msg, 6000);
  }

  // ---------- events from the app ----------
  document.addEventListener("locspoof:ui", (e) => {
    const d = e.detail; fun = !!d.fun; skin = d.skin || ""; dark = d.dark;
    root.dataset.fun = fun ? "on" : "off";
    if (!fun) { parts.length = 0; ctx.clearRect(0, 0, W, H); } else if (ambient()) kick();
    paintMascot();
  });
  document.addEventListener("locspoof:status", (e) => { last = e.detail.status; awake = e.detail.awake || awake; onStatus({ ...last, awake }); paintMascot(); });
  let wasSlept = false;
  setInterval(() => { const s = mascotState() === "sleep"; if (wasSlept && !s && fun) say("Good morning! ☀️"); wasSlept = s; paintMascot(); }, 4000);

  // ---------- easter eggs ----------
  let taps = 0;
  document.addEventListener("locspoof:logo", (e) => {
    if (!fun) return; taps = e.detail.taps;
    if (taps >= 5) { force("dance", 4000); say("You found me! 🎉"); confetti(innerWidth / 2, 90, 90); }
  });
  const KONAMI = ["ArrowUp", "ArrowUp", "ArrowDown", "ArrowDown", "ArrowLeft", "ArrowRight", "ArrowLeft", "ArrowRight", "b", "a"]; let kpos = 0;
  document.addEventListener("keydown", (e) => {
    if (!fun) return;
    const k = e.key.length === 1 ? e.key.toLowerCase() : e.key;
    kpos = k === KONAMI[kpos] ? kpos + 1 : (k === KONAMI[0] ? 1 : 0);
    if (kpos === KONAMI.length) { kpos = 0; party(); }
  });
  function party() {
    root.classList.add("party"); rain(180); force("dance", 8000); say("PARTY MODE! 🪩", 6000); window.locspoof?.toast("🪩 Party mode! (it wears off by itself)", 4000);
    setTimeout(() => root.classList.remove("party"), 10000);
  }
  const WORDS = {
    party: ["🪩", "Party!", party], confetti: ["🎊", "Confetti!", () => rain(160)], boo: ["👻", "Boo!", () => { emojiBurst("🦇", 28); emojiBurst("👻", 10); say("Boo! 👻"); }],
    snow: ["❄️", "Let it snow", () => { ambientKind = "winter"; ambientUntil = Date.now() + 15000; kick(); say("Brrr! ❄️"); }],
    hearts: ["💖", "Spread the love", () => { ambientKind = "hearts"; ambientUntil = Date.now() + 15000; kick(); say("Aww 💖"); }],
    candy: ["🍬", "Sweet!", () => emojiBurst("🍬", 40)], coffee: ["☕", "Coffee time", () => { force("coffee", 6000); say("Ahh, coffee. ☕"); }],
    sleep: ["😴", "Nap time", () => { force("sleep", 6000); say("zzz…"); }], hello: ["👋", "Say hi", () => { force("wave", 2500); say("Hello there! 👋"); }],
    pizza: ["🍕", "Pizza rain", () => emojiBurst("🍕", 30)], xyzzy: ["✨", "Magic word", () => say("Nothing happens.")],
  };
  document.addEventListener("locspoof:palette", (e) => {
    if (!fun) return; const w = WORDS[e.detail.q.trim()]; if (!w) return;
    e.detail.add({ ic: w[0], t: w[1], hint: "secret", run: w[2] });
  });

  // ---------- a greeting on the big days ----------
  setTimeout(() => {
    if (!fun) return;
    const d = new Date(), key = `${d.getMonth() + 1}-${d.getDate()}`, big = { "10-31": "🎃 Happy Halloween!", "12-25": "🎄 Merry Christmas!", "2-14": "💖 Happy Valentine's Day!", "1-1": "🎆 Happy New Year!" }[key];
    let seen = ""; try { seen = localStorage.getItem("fun.greeted") || ""; } catch { /* private mode */ }
    if (big && seen !== `${d.getFullYear()}-${key}`) { try { localStorage.setItem("fun.greeted", `${d.getFullYear()}-${key}`); } catch { /* ignore */ } say(big, 7000); confetti(innerWidth / 2, innerHeight * .6, 90); }
  }, 1500);

  window.locspokeFun = { confetti, rain, party };
})();
