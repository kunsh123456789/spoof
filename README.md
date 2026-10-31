# iPhone Location Simulator

A small local web app: open it in your browser, click the map (or search a place), press **Set Location**,
and your USB-connected iPhone reports that GPS position. **Clear / Restore Real** stops the simulation.
Everything runs on localhost — no accounts, no cloud services, no API keys.

* Backend: Python + FastAPI + [`pymobiledevice3`](https://github.com/doronz88/pymobiledevice3)
* Frontend: Leaflet (bundled in `static/vendor`) with OpenStreetMap tiles
* Place search: Nominatim (OpenStreetMap), with a User-Agent, a 1 request/second limit and caching

## Features

device status (name, iOS version, clear errors) · click-to-pin map + manual lat/lon · place search ·
Set / Clear location · **road routes** (walking or driving paths, or a straight line) with adjustable speed ·
**multi-stop routes** (pauses, looping) · **trick-or-treat mode** (orange theme) · saved favorites ·
recent locations · **offline areas** (map, houses and streets saved for use without internet) · **realism switches** (GPS noise, natural walking, traffic stops, jump cooldown) · **return trip** · **per-stop speed schedule** · **GPX import** · **saved routes** · **timed plans** (do things at real clock times) · **keeps the Mac awake** when needed · **several phones at once** · **Wi-Fi connection** · **auto-reconnect** · **disk-image warning** · dark mode · phone-friendly layout.

## Quick start

```bash
cd iphone-location
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python desktop.py                  # opens the app in its own window (or: python app.py, then open http://localhost:8000)
```

Python 3.10+ is required. Nothing is installed system-wide; everything lives in `.venv`.

## Desktop app (its own window)

The simulator can run in its own window instead of a browser tab (macOS uses the built-in WebKit engine through `pywebview`).

```bash
python desktop.py            # opens the window
python desktop.py --browser  # same thing in your default browser (Ctrl+C quits)
```

To get a normal Mac application you can double-click or keep in the Dock, build it once from the project folder:

```bash
./make_app.sh
```

This creates `~/Applications/iPhone Location.app` (with its own icon, no Terminal window). Behavior:

* Closing the window (or Cmd+Q) asks for confirmation and then **quits the app**. On iOS 17 and later that also ends the fake
  location, as before.
* Opening the app again while it is already running just brings the running copy to the front. If the simulator is already running
  from Terminal (`python app.py`), the window attaches to it instead of starting a second server.
* Your tab, switches, closed instruction boxes and chosen phone are remembered between launches.
* Links to other sites (such as the map credits) open in your normal browser.
* Its log is `~/Library/Logs/iPhone Location.log` (kept small: it rotates at 2 MB and keeps 3 old files). If the app crashes while
  *starting*, the reason is in `~/Library/Logs/iPhone Location.startup.log`. `python desktop.py` prints the same thing in the terminal
  (add `-v` for detail, `--debug` for the web inspector).
* The app points at this project folder and its `.venv`, so if you move or rename the folder, run `./make_app.sh` again. After
  `git pull` nothing needs rebuilding. For a different location: `./make_app.sh /Applications`.
* `python app.py` (browser only, no window toolkit needed) still works.

## One-time iPhone setup

1. **Connect** the iPhone to the computer with a USB cable (a data cable, not charge-only). Unlock it.
2. **Trust the computer**: tap **Trust** on the phone's prompt and enter your passcode. If you missed it, press
   **Trust this computer** in the app's red banner.
3. **Enable Developer Mode** (iOS 16+): *Settings → Privacy & Security → Developer Mode → On*, then restart the
   phone and confirm. The option only appears after the phone has been connected to a computer. If it is missing, run
   `python -m pymobiledevice3 amfi reveal-developer-mode` (venv active), then look again.
4. macOS needs nothing else (usbmuxd is built in). On Linux install/start `usbmuxd`. On Windows install
   iTunes or Apple Devices.

The app polls the phone every few seconds and shows its state in the top-right chip.

## How it picks the method (automatic)

| iPhone | Method |
|---|---|
| **iOS 17 and later** | DVT `LocationSimulation` over an RSD tunnel (what `pymobiledevice3 developer dvt simulate-location set` uses) |
| **iOS 16 and earlier** | lockdown service `com.apple.dt.simulatelocation` (`pymobiledevice3 developer simulate-location set`) |

The Developer Disk Image is checked and mounted automatically when missing. This needs an internet
connection the first time (the image is downloaded; iOS 17+ images are personalized via Apple's servers).

### iOS 17+ tunnel

The app tries, in order:

1. a **`tunneld`** you already have running (the classic approach, needs admin rights), then
2. pymobiledevice3's built-in **no-root userspace tunnel** (iOS 17.4+ over USB). Nothing to run — this is
   what normally happens.

If neither works (typically iOS 17.0–17.3), the UI tells you to start `tunneld` yourself. In a separate terminal,
from the project folder:

```bash
sudo .venv/bin/python -m pymobiledevice3 remote tunneld
```

Leave it running; the app picks it up on the next click. Force a mode with `python app.py --tunnel tunneld|userspace`
(default `auto`).

> **Important on iOS 17+:** the fake location only lasts while this app keeps its connection open. If you quit the app
> (Ctrl+C) or unplug the phone, the phone goes back to its real location. That is expected, not a bug. On iOS 16 and
> earlier the fake location persists on the phone until you press **Clear** or reboot it.

## Using it

The panel has four tabs, and each one has a **How it works** box (close it once you know the steps; the app remembers).
The bar at the bottom always shows what the phone is doing, with **Stop moving** and **Restore real GPS** buttons.

* **📍 Place**: click the map or search to drop a pin (drag it to fine-tune), then **Set location**.
* **🚶 Move**: pick **Straight line**, **Walking** or **Driving** (the last two follow real roads from OpenStreetMap and are
  drawn on the map) and set the speed (changeable while moving). Then choose a destination type:
  * **One**: go to the pin.
  * **Multi-stop**: move the pin, press **Add pin as stop**, repeat; reorder with the arrows; optionally set a pause at each stop
    and **Loop**. **Speed schedule:** each stop can have its own path type and speed for the leg that ends there (walk to the car,
    then drive); blank means "use the slider".
  * **GPX file**: choose a `.gpx` track (from a running/hiking/maps app). It is drawn on the map and replayed exactly. Optionally
    jump to the track's start, or press **Use recorded pace** to use the speed it was recorded at.
  **Return to start when finished** adds a leg back to where the trip began; **↩ Return now** does the same after a trip.
  **💾 Save route** keeps a multi-stop route or a GPX track (see Saved). You must set a starting location first (or let a
  GPX/saved route jump to its start).
* **Realism switches** (Move tab, all off by default, saved between runs, apply live):
  * **GPS noise**: a few metres of smooth drift on every position, also while standing still.
  * **Natural walking**: the pace wanders a little and eases off before sharp corners.
  * **Traffic stops**: driving routes pause for a few seconds at some junctions (more likely at sharp turns), like traffic lights.
    These are simulated from the route's shape, not from real traffic-light data.
  * **Jump cooldown timer**: after a location jump of 1 km or more, shows roughly how long a real trip would have taken.
  Not possible with this method: compass **heading** and **altitude** (the location service only accepts latitude/longitude).
* **🎃 Trick or treat**: while this tab is open (and while a trick-or-treat walk is running) the app turns orange and shows a
  "Trick-or-treat mode" banner.
  1. **Draw area**: click points on the map around the neighborhood you want and click the first point again (or press
     **Finish**) to close the shape. **Undo point** removes the last point, **Esc** cancels. The area can be at most 4 km². If you
     skip drawing, houses are searched 350 m around the pin instead.
  2. **Find houses**: 🎃 pumpkins appear for real houses from OpenStreetMap inside your area, ordered into a house-to-house tour.
  3. **Start**: the phone jumps to the first house, walks street to street at about 4.5 km/h with natural speed variation, and
     pauses 20-120 s at every door. **Stop moving** ends it, leaving the phone where it is.
  Options (switches on the tab, saved between runs): **Group of kids** (3.2 km/h and 1.6x longer pauses), **Avoid busy roads**
  (houses within 20 m of a motorway/trunk/primary/secondary road are skipped and the tour never crosses one, so it ends early
  instead; needs road data from OpenStreetMap or a saved offline area, otherwise you get a note), and **Realistic houses** (about
  15% of houses have their lights off and are skipped, shown as 🌑; about 15% become a long chat at the door). Changing an option
  after **Find houses** asks you to press it again.
* **⏰ Schedule** (timed plans): build a plan of steps that run **by themselves at real clock times**, for example *8:00 PM start trick-or-treat
  (kids, realistic houses) → 8:30 PM a shaky 5 km/h walk home to an address → 9:00 PM restore real GPS*.
  1. **＋ New plan**: name, date, which phone, and **Run late steps up to N minutes** (default 10).
  2. **＋ Add step** for each thing: a **time** and an **action**: 🎃 trick or treat (an address, your pin or your drawn area, number of
     houses, kids / avoid busy roads / realistic), 🚶 walk or drive to an address (path type, speed, **shaky** = GPS noise + uneven pace for
     this walk only, optional start place), 🧭 a saved route, 📍 set a location, ⏹ stop moving, or ↩ restore real GPS. Times after midnight roll
     over to the next day.
  3. **Save plan**, then switch **Activate** on. A plan never runs unless it is activated; editing a plan switches it off again.
  4. At each step's time the app does it, then waits for the next. Each step shows ⏳ waiting / ▶ running / ✅ done / ⚠ failed / 🕘 missed / ⏭ skipped
     with a short note, and **⏵** tests a single step right now. The status bar shows the next step and a countdown.
  Rules worth knowing: the app must be **open**, the Mac **awake** and the phone **connected** at that time. A step that is late (the Mac slept) still
  runs up to the late limit and says so; beyond it, it is marked **missed** rather than firing hours later. If several steps are overdue at once only
  the newest runs (the older ones are *skipped*). A step that fails for a temporary reason (phone not connected yet, no internet) is retried every
  15 s until its late limit ends. Activating a plan marks steps that are already in the past as missed. Steps that need the internet (finding houses
  or addresses) need it at that moment, unless an offline area covers the place. The app cannot wake a sleeping Mac; see the next section.
* **★ Saved**: **Saved routes** (▶ runs one, ✎ loads it into the editor, ✕ deletes it), **Favorites** take a name plus a place name, coordinates like `48.8584, 2.2945`, or nothing (saves the
  current pin). Click one to move the pin there, **＋** adds it as a stop. **Recent** keeps your last 15 locations (each Set
  location and finished route); **★** promotes one to a favorite.
* **⬇️ Offline**: while you have internet, zoom the map so a neighborhood fills the screen (up to about 2.4 × 2.4 km), name it
  and press **Download what I see**. The app saves, into the `offline/` folder next to `app.py`:
  * the **map tiles** (zoom 12-17),
  * the **houses** (for trick-or-treat "Find houses"), and
  * the **streets** (for walking/driving paths, using a small built-in router).
  Dashed boxes on the map show saved areas; click one in the list to jump there, ✕ deletes it. The app always tries the online
  services first and uses the saved data automatically when they are unreachable, so inside a saved area everything works with
  no data at all (a "No internet" note appears at the top). Typical size is 3-6 MB per area. To use trick-or-treat offline,
  download the area first, then draw/pin inside it. Offline routing is a simplified router: a short gap in the street data
  (up to 200 m) becomes a straight hop, anything longer reports an error. Straight line mode never needs any data.
* **📱 Phones & connection** (click the phone chip at the top): lists every iPhone the app can see with a **🔌 USB** or **📶 Wi-Fi**
  badge. Click one to control it; each phone keeps its own location and trips, and several can run at once (the others show as grey
  dots on the map). **Auto-reconnect** (on by default): if the cable or Wi-Fi drops, a trip pauses ("📡 Reconnecting…"), and when the
  phone is back the fake location is restored and the trip continues; if it does not come back within 10 minutes the trip stops.
  An unplugged iOS 16-or-earlier phone simply keeps its fake location on its own.
* **Disk image warning**: a blue notice appears if the phone's Developer Disk Image is not mounted (for example after an iOS update or a
  restart), telling you to connect to the internet once and press Set location so it is mounted again.
* Files `favorites.json`, `recent.json`, `settings.json`, `routes.json`, `devices.json` and the `offline/` folder live next to `app.py`
  and are ignored by git.

Notes on the online services: place search (Nominatim), road routing (the free OSM-community OSRM servers at
routing.openstreetmap.de) and house lookup (Overpass API) are free public services with fair-use limits. The app sends a
User-Agent, rate-limits and caches requests (map tiles are fetched by the local server with an identifying User-Agent; saved areas are
kept small on purpose, because OpenStreetMap asks people not to bulk-download tiles); if one is down you get a friendly message, and **Straight line** always works
offline. Large location jumps followed by fast movement are how some apps detect fake GPS, so keep that in mind.

Command-line options: `python app.py --port 8000 --tunnel auto -v` (`-v` = debug logging). The server binds to
127.0.0.1 only and has no authentication — don't expose it to a network.

## Keeping the Mac awake (the Caffeinate update)

**Honest limit first:** on iOS 17 and later the fake location only exists while this Mac holds the connection to the phone open. A Mac that is
truly asleep holds nothing, so the phone goes back to its real GPS until the Mac wakes. No app can change that. iPhones on iOS 16 and earlier keep
their fake location by themselves (but a running trip still stops while the Mac sleeps).

What the app does instead is stop the Mac from sleeping **whenever it is needed**: while a trip is running, while an iOS 17+ fake location is
active, while the app is reconnecting, and while an **activated schedule** has steps coming within the next day. It uses macOS's own `caffeinate`
tool, started with `-w <the app's process id>` so it ends the instant the app does, even if the app crashes: it cannot leave your Mac stuck awake.
Nothing is held when nothing needs it. Switches (in **Phones & connection**, on by default / off by default): **Keep this Mac awake when needed**
and **Keep the screen on too** (off: the display may go dark and save power while the Mac stays awake). The status bar shows "☕ Mac kept awake".
The window app also opts out of macOS **App Nap**, which could otherwise delay timers (and so scheduled steps) while its window is hidden.

What it cannot do:
* **A MacBook on battery sleeps when you close the lid**, whatever the app asks. To run with the lid shut: plug in power and use an external display
  with a keyboard or mouse (**clamshell mode**), or simply keep the lid open and turn the brightness down.
* It cannot **wake** a sleeping Mac for a scheduled step. Leave the Mac awake (plugged in) before the plan's first step.
* If the Mac slept anyway, the app notices (the wall clock jumps while the monotonic clock does not), shows **"💤 Your Mac was asleep for X"** for 15
  minutes, re-checks the phone at once, and with **Auto-reconnect** restores the fake location and carries on.

## What happens when something goes wrong

| Situation | What the app does |
|---|---|
| The phone stops answering (asleep on Wi-Fi, flaky cable) | Every call to the phone is time-limited (12 s). A stuck connection is dropped and replaced by a fresh one; the buttons, status and **Restore real GPS** keep working. |
| Cable or Wi-Fi drops during a trip | With **Auto-reconnect** the trip pauses ("📡 Reconnecting…"), the fake location is restored when the phone is back and the trip continues (gives up after 10 minutes). |
| You press **Stop** | The update being sent finishes first (cutting it in half could confuse the phone's connection); only if the phone is not answering is it cancelled after 3 s. |
| Internet drops | Route, search and house lookups retry (a few seconds apart) and then show a plain message; **Straight line**, typed coordinates and saved offline areas keep working. A trip already started never needs internet. |
| A web page answers instead of the service (hotel/airport Wi-Fi login) | It is recognised as not the real answer: no bad map tile or half-empty offline area is saved, and you get a friendly message. |
| An offline download is interrupted (quit, crash, disk full) | Files are written atomically (never half written); leftovers are cleaned up at the next start; a broken saved tile is repaired automatically; a full disk gives a clear message. |
| The app is killed or crashes during a trip | **iOS 17+:** the fake location ends by itself the moment the connection closes (your real GPS returns). **iOS 16 and earlier:** the phone *keeps* the fake location, so the app writes down where it is and, on the next start, shows it with a notice and a **Restore real GPS** button. |
| The server part of the window app dies | It is restarted automatically (up to 3 times). |
| A settings or data file is corrupt | The app still starts; the bad file is set aside (`*.corrupt.json`) and an empty one is used. |

Everything the app saves (favorites, recents, routes, settings, offline areas) is written crash-safely, so a power cut cannot leave a half-written file.

## Wi-Fi (no cable)

1. Plug the iPhone in **once**, tap Trust, turn on Developer Mode.
2. In **Phones & connection**, switch on **Allow connecting over Wi-Fi** (this changes a setting on the phone and needs the cable).
3. Put the Mac and iPhone on the **same Wi-Fi network** and keep the iPhone awake and unlocked.
4. Unplug. Within a few seconds the phone shows a **📶 Wi-Fi** badge. If it does not, press **Find Wi-Fi phones** (a Bonjour scan, about
   5 seconds; it needs the phone to have been trusted by this app or Finder before). On a Mac you may also need Finder → your iPhone →
   *Show this iPhone when on Wi-Fi*.

How it works: the phone is found through usbmuxd's network list, or through a Bonjour scan. The app then uses the same location
method as with a cable. **Wi-Fi is the least-tested part of this project** (it was written against the library's documented
APIs and unit-tested with mocks, but not run against a real phone) and it can be less reliable than a cable. iOS 17.0-17.3 over
Wi-Fi may need `tunneld` (see above). If Wi-Fi misbehaves, use the cable.

**Several iOS 17+ phones at once:** the built-in no-root tunnel supports only **one** iOS 17+ phone per app. For two or more, start
`tunneld` (`sudo .venv/bin/python -m pymobiledevice3 remote tunneld`); phones on iOS 16 or earlier have no such limit.

## Troubleshooting

| Message / symptom | Fix |
|---|---|
| **No iPhone found** | Different/data cable, other USB port, unlock the phone, replug. On Linux check `usbmuxd` runs. |
| **Cannot reach usbmuxd** | Linux: `sudo systemctl start usbmuxd`. Windows: install iTunes/Apple Devices. |
| **Not trusted** | Tap *Trust* on the phone, or press the in-app **Trust this computer** button. |
| **iPhone is locked** | Unlock it with the passcode, retry. |
| **Developer Mode is off** | Step 3 above; it requires a restart and a confirmation after reboot. |
| **iOS 17+ needs a developer tunnel** | Run the `sudo … tunneld` command above in another terminal. |
| **No Developer Disk Image / service unavailable** | Be online for the first mount; `pip install -U pymobiledevice3` for newly released iOS versions. Replug the phone. |
| Location reverts after a while / when I quit | iOS 17+ simulation ends when the app disconnects (see note above). |
| "Could not get a road route" / "No route" | The routing service is down, or the points aren't connected by roads. Try again, or use Straight line. |
| "Could not look up houses" / "Found only N houses" | The Overpass service is busy (wait a minute), or draw a bigger area over a residential neighborhood (OpenStreetMap needs houses mapped there). |
| "That area is too big" | Draw a smaller area (max 4 km²). |
| A scheduled step shows 🕘 missed | The app was closed or the Mac was asleep at that time by more than the plan's late limit. Keep the app open and the Mac awake (plugged in) before the first step. |
| A scheduled step shows ⚠ failed | Read the note on the step: usually the phone was not connected or there was no internet for the house/address lookup. Use **⏵** to test it. |
| Phone shows "not connected" | The cable or Wi-Fi dropped. With Auto-reconnect on, just reconnect it; the trip and fake location resume by themselves. |
| Phone not listed over Wi-Fi | Follow the Wi-Fi steps above; press **Find Wi-Fi phones**; same network; phone awake. Use the cable if it still fails. |
| "Only one iOS 17+ iPhone can use the built-in tunnel" | Start `tunneld` (see Wi-Fi section) to control several iOS 17+ phones. |
| Blue "Developer Disk Image is not mounted" notice | Be online once, press Set location; the app mounts it. |
| Search says "rate-limiting" or "offline" | Nominatim allows ~1 request/second and needs internet. Wait a moment. |
| Map is grey | OpenStreetMap tiles need internet, or a saved offline area covering this spot (Offline tab). Zooming past level 17 offline shows blank tiles. |
| "That area is too big" (Offline tab) | Zoom the map in until the neighborhood fills the screen (limit about 6 km²). |
| Offline route says "no offline map covers these points" | Download an offline area that includes both the start and the destination. |
| App does not open when double-clicked | Read `~/Library/Logs/iPhone Location.startup.log` (and `iPhone Location.log`). Most often: run `pip install -r requirements.txt` in the project's `.venv` (the window toolkit), then `./make_app.sh` again. |
| Something else | Run `python app.py -v` and read the terminal; every failure logs a full traceback there. |

pymobiledevice3 changes quickly. This project was written against version 11.x (fully async API). If a future
release renames something, the only file that touches it is `locspoof/device.py` (`PmdBackend`).

## Development

```bash
pip install -r requirements-dev.txt
python -m pytest
```

Tests use a fake phone and mocked web services (no hardware or internet needed) and cover the API, error states,
route following, multi-stop/loop/trick-or-treat logic, routing/Overpass parsing, favorites, recents and search rate limiting.

## Layout

```
app.py                 entry point (python app.py)
locspoof/device.py     phone detection, iOS-version-specific sessions, journey runner
locspoof/routing.py    road routing (OSRM) with per-leg geometry
locspoof/trickortreat.py house lookup (Overpass) and tour planning
locspoof/recents.py    recent locations store
locspoof/offline.py    offline packs: tiles + houses + streets, local A* router, downloader
locspoof/realism.py    GPS noise, natural pace, traffic stops, jump cooldown
locspoof/settings.py   the on/off switches (settings.json)
locspoof/gpx.py        GPX import
locspoof/routes.py     saved routes
locspoof/busy.py       busy-road geometry for trick-or-treat
locspoof/fleet.py      one manager per phone
locspoof/schedule.py   timed plans: store, validation, scheduler
locspoof/awake.py      keep the Mac awake (caffeinate) and notice sleep
locspoof/memory.py     per phone: last iOS version, and the fake location an old iOS phone may still hold
locspoof/fileio.py     crash-safe file writing used by every data file
locspoof/geocode.py    Nominatim client (User-Agent, rate limit, cache)
locspoof/favorites.py  JSON favorites store
locspoof/movement.py   great-circle math
locspoof/server.py     FastAPI routes
locspoof/runner.py     server on a background thread, port helpers
locspoof/desktop.py    native window (pywebview) launcher
desktop.py             entry point for the window app
make_app.sh            builds the double-clickable Mac app
assets/icon.png        app icon (regenerate with tools/make_icon.py)
static/                the single-page UI (Leaflet vendored in static/vendor)
tests/                 pytest suite
```

Use this only on your own device, for development, testing or privacy purposes, and respect the terms of any
service or app you use it with.
