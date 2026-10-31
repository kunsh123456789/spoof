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
recent locations · dark mode · phone-friendly layout.

## Quick start

```bash
cd iphone-location
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python app.py                      # then open http://localhost:8000
```

Python 3.10+ is required. Nothing is installed system-wide; everything lives in `.venv`.

## Make it a Mac app (double-click to open)

After the Quick start works, build a launcher app from the project folder:

```bash
./make_app.sh
```

This creates `~/Applications/iPhone Location.app`. Double-click it (or drag it to your Dock): it starts the
server in a Terminal window and opens http://localhost:8000. If the server is already running it just opens the page.
To quit, press Ctrl+C in that Terminal window (or close it); on iOS 17+ that also restores the real location.

The app points at this project folder, so if you move or rename the folder, run `./make_app.sh` again.
To pick a different location: `./make_app.sh /Applications`.

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
  drawn on the map), set the speed (changeable while moving) and either go to **one destination** (the pin) or build a
  **multi-stop route**: move the pin, press **Add pin as stop**, repeat; reorder with the arrows; optionally set a pause at
  each stop and **Loop**. You must set a starting location first.
* **🎃 Trick or treat**: while this tab is open (and while a trick-or-treat walk is running) the app turns orange and shows a
  "Trick-or-treat mode" banner. Put the pin on a residential street, press **1 · Find houses** (pumpkins appear: real houses
  from OpenStreetMap, ordered into a house-to-house tour), then **2 · Start**. The phone jumps to the first house, walks
  street to street at about 4.5 km/h with natural speed variation, and pauses 20-120 s at every door. Radius and number of
  houses are adjustable. **Stop moving** ends it, leaving the phone where it is.
* **★ Saved**: **Favorites** take a name plus a place name, coordinates like `48.8584, 2.2945`, or nothing (saves the
  current pin). Click one to move the pin there, **＋** adds it as a stop. **Recent** keeps your last 15 locations (each Set
  location and finished route); **★** promotes one to a favorite.
* Files `favorites.json` and `recent.json` live next to `app.py` and are ignored by git.

Notes on the online services: place search (Nominatim), road routing (the free OSM-community OSRM servers at
routing.openstreetmap.de) and house lookup (Overpass API) are free public services with fair-use limits. The app sends a
User-Agent, rate-limits and caches requests; if one is down you get a friendly message, and **Straight line** always works
offline. Large location jumps followed by fast movement are how some apps detect fake GPS, so keep that in mind.

Command-line options: `python app.py --port 8000 --tunnel auto -v` (`-v` = debug logging). The server binds to
127.0.0.1 only and has no authentication — don't expose it to a network.

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
| "Could not look up houses" / "Found only N houses" | The Overpass service is busy (wait a minute), or move the pin to a residential street / increase the radius. |
| Search says "rate-limiting" or "offline" | Nominatim allows ~1 request/second and needs internet. Wait a moment. |
| Map is grey | OpenStreetMap tiles need internet. |
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
locspoof/geocode.py    Nominatim client (User-Agent, rate limit, cache)
locspoof/favorites.py  JSON favorites store
locspoof/movement.py   great-circle math
locspoof/server.py     FastAPI routes
static/                the single-page UI (Leaflet vendored in static/vendor)
tests/                 pytest suite
```

Use this only on your own device, for development, testing or privacy purposes, and respect the terms of any
service or app you use it with.
