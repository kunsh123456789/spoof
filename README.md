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
recent locations · **offline areas** (map, houses and streets saved for use without internet) · **realism switches** (GPS noise, natural walking, traffic stops, jump cooldown) · **return trip** · **per-stop speed schedule** · **GPX import** · **saved routes** · **timed plans** (do things at real clock times) · **keeps the Mac awake** when needed · **several phones at once** · **Wi-Fi connection** · **auto-reconnect** · **disk-image warning** · **always-on progress bar** (address, trip progress, time left) · **top-bar tabs, command palette (Ctrl/⌘ K), keyboard shortcuts, activity log** · light/dark/auto + accent colours · **seasonal themes** · **Fun mode** (confetti, candy counter, little Mac mascot, surprises) · satellite/topo maps · phone-friendly layout.

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
* **Updating the app**: the app *is* this folder, so there is nothing to reinstall. Quit it (Cmd+Q; if the window is closed it may still be running, so check the Dock), run `git pull` in the project folder, then open the app again. Re-run `./make_app.sh` only if you moved the folder or want a new icon. If `requirements.txt` changed, also run `source .venv/bin/activate && pip install -r requirements.txt` first. A change you don't see usually means the old copy was still running.

## Phone remote (control it from your iPhone)

Use your iPhone as a remote control for the Mac app: a map, place search, favorites, trips, saved routes and a big **🚨 panic** button.
It talks to the app on the Mac over your **home Wi-Fi** (both devices on the same network).

1. On the Mac: **⚙ Settings → Phone remote → Control this app from my iPhone**. macOS may ask *"Do you want the application python to accept
   incoming network connections?"*: choose **Allow**. Two addresses appear, for example `http://yourmac.local:8787/remote`.
2. On the iPhone open **Safari** and type that address (the `.local` one keeps working if the Mac's IP changes).
3. On the Mac press **New pairing code**, and type the 6 digits on the iPhone. This is done once per phone.
4. In Safari: **Share → Add to Home Screen**. You now have an app icon that opens the remote full-screen.

The remote has three tabs, and the **🚨 panic button is always on screen**:

* **✅ Status** (opens first; it is for checking that the plan is working): health chips (Mac reachable, iPhone ready, Mac kept awake, plans on), red **alerts** when
  something is wrong (iPhone dropped, trip stopped, a plan step failed or was missed, keep-awake off while a plan is on, the Mac slept; the phone also vibrates and the
  alert shows on every tab), the **next scheduled step** with a countdown, each **plan** with every step's state (waiting / running / done / failed / missed), and the
  **recent activity** log. You can adjust things: a **speed slider** while moving, **Stop**, **Back to start**, **Real GPS**, switch a plan **on or off**, and press **⏵** to run a
  step right now.
The health chips also show the **Mac's battery** (🔋 87%, or 🔌 plugged in), and a red alert appears when it is at 20% or less and not plugged in: a flat Mac sleeps and stops the location. (Macs only; the battery is also shown in the Mac app under Phones & connection → Keep Mac awake.)

* **📍 Places**: search, tap the map, favorites, recents, **Set location here**.
* **🚶 Trip**: walk, drive or straight line to the pin, and run saved routes.

Plans themselves are created and edited on the Mac (Schedule tab); the remote can only watch, switch them on/off and run a step.
If several iPhones are connected there is a phone picker at the top. The top bar always shows what the iPhone is doing and how far along a trip is.

### Using it away from home (other Wi-Fi, mobile data)

The remote normally only works on the same Wi-Fi as the Mac. To use it from anywhere, use the free **Tailscale** app: it puts your Mac and your phone on a
private, encrypted network (no router settings, nothing exposed to the internet). The iPhone that is *being spoofed* must stay connected to the Mac (cable or the
same Wi-Fi), so this is for: **phone A stays at the Mac, phone B is your remote.**

1. Install Tailscale on the Mac (<https://tailscale.com/download>, or the Mac App Store) and on the remote phone (App Store). Sign in with the **same account** on both.
2. Make sure Tailscale shows *Connected* on both (on the phone, its toggle must be on).
3. In the Mac app: ⚙ Settings → Phone remote. A **🌍 anywhere** address appears (something like `http://your-mac.tailnet-name.ts.net:8787/remote`).
4. On the remote phone open **that** address in Safari, pair with the code, and Add to Home Screen. Use only the 🌍 address from then on: it works at home and away. (A phone
   remembers its pairing per address, so a home-Wi-Fi address and the 🌍 address each need to be paired once.)
5. The Mac must be **awake**, online and running the app (see *Keeping the Mac awake*). Mobile data is fine on the phone.

Tailscale traffic is encrypted, which also fixes the "plain http" caveat below for the 🌍 address. If the remote says it can't reach the Mac, check that Tailscale is on in both places.

**Safety:** it is **off by default**. The normal app stays reachable from this Mac only; switching the remote on opens a *second* port (8787) on
your network. Without pairing, a device can only load the remote page. Pairing needs the 6-digit code shown on the Mac (valid 10 minutes, one use, locked
after 5 wrong tries). A paired phone can only control the location: it can never change settings, download maps, edit plans or manage other phones
(those work only on the Mac). The Mac keeps just a hash of each phone's key; **✕** next to a paired phone in Settings cuts it off at once. There is no
encryption on the home Wi-Fi (plain `http`), so only use it on a network you trust, and switch it off when you do not need it.
The Mac must be awake and the app open; the iPhone being controlled can be the same phone you are holding.

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

The tabs sit in the bar at the top of the window (on a phone-sized window they move to the bottom). Click the active tab again to hide or show the side card; drag its right edge to resize it (double-click to reset). Each tab has a **How it works** box (close it once you know the steps; the app remembers).

**The progress bar at the bottom is always there** and says exactly what the phone is doing: *Set to Eiffel Tower, Paris since 8:02 PM*, *Heading to Louvre Museum · 340 m of 1.5 km · in 12 min (arrives 8:30 PM)*, *At the door of house 4*, *Stopped at a junction*, *Reconnecting to the iPhone…* or *Using your real location*. Addresses come from OpenStreetMap (a place you searched for keeps its name). It has the **Stop**, **Return** and **Restore real GPS** buttons. The top bar also shows the next scheduled step, a ☕ when the Mac is being kept awake, and the phone.

### Handy extras

* **Command palette**: `Ctrl/⌘ K`, then type anything: a tab, *stop*, *restore*, *dark*, a favorite, a saved route, or an address to search.
* **Shortcuts** (press `?` to see them all): `1`–`8` tabs · `Space` stop moving · `R` restore real GPS · `B` return to start · `\` hide/show the side card · `/` search · `C` crosshair (precise pin) · `L` centre on the phone · `M` next map style · `,` settings.
* **📶 Connection history** (bottom of the Phones tab): every time an iPhone connected (🔌 cable / 📶 Wi-Fi), dropped, came back or switched between cable and Wi-Fi, a remote phone connected or left, and every time the Mac slept, with a 24-hour summary (drops, sleeps, last drop).
* **📱 Phones** tab: two clearly separate lists. **iPhones on this Mac** (the phones whose location is changed: cable or Wi-Fi, favorite, rename, which one is being controlled, and whether **Developer Mode** is on) and **Remote controls** (the other phones that control the app: 🟢 connected now, over home Wi-Fi or Tailscale, rename, forget). A new iPhone (iOS 16+) with Developer Mode off shows a **🛠 Enable Developer Mode** button: it makes the switch appear on the phone and asks it to turn on. The phone restarts and you confirm “Turn On” on it; a phone with a passcode must be switched on by hand (Settings → Privacy & Security → Developer Mode), and the app says so.
* **⚙ Settings** (top right): light / dark / auto, accent colour, seasonal themes on/off, map style (street, satellite, topo), **Fun mode**, the setup checklist and a reset.
* **📜 Activity** tab: a log of what happened and when (phone connected, location set, trips, schedule steps, Mac asleep…).
* Seasonal themes switch themselves on: orange for Halloween in October, snowy in December, hearts in February (turn off in Settings). Trick-or-treat always turns the app orange.
* **Fun mode** (on by default, one switch to turn it off): confetti when a trip ends, a 🍬 candy counter in the top bar while trick-or-treating, a tiny Mac that sleeps when your Mac slept, sips coffee while the Mac is kept awake and wears a seasonal hat, falling leaves, snow or hearts, and a few surprises nobody will tell you about. 🤫

* **📍 Place**: click the map or search to drop a pin (drag it to fine-tune), then **Set location**. A pin you drop or drag **snaps to the nearest road or
  path that makes sense** (within 150 m; a note tells you how far it moved; turn off with **Snap pins to roads** in Settings). Searched places keep their exact spot.
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
  * **Traffic stops**: a drive waits at **stop signs** (a fixed wait, **3 s** by default) and **traffic lights / junctions** (a random wait,
    **10-35 s** by default; about 4 in 10 lights are green). Both times are adjustable right under the switch. When the internet is available the app
    looks up the **real stop signs and traffic lights along your route** in OpenStreetMap (the activity log says how many it found), so the
    stops happen where a real driver would stop. If that lookup fails (no internet, or a very long route over about 35 km) it falls back to
    simulated stops based on the shape of the route. Walking does not use this.
* **🌴 California only** (Settings, on by default): the map cannot be moved outside California and place search only returns places inside it, which
  keeps things fast and the stop-sign/light data dense. Turn it off in Settings to use the whole world.
  * **Jump cooldown timer**: after a location jump of 1 km or more, shows roughly how long a real trip would have taken.
  Not possible with this method: compass **heading** and **altitude** (the location service only accepts latitude/longitude).
* **🎃 Trick or treat**: while this tab is open (and while a trick-or-treat walk is running) the app turns orange and shows a
  "Trick-or-treat mode" banner.
  1. **Draw area**: click points on the map around the neighborhood you want and click the first point again (or press
     **Finish**) to close the shape. **Undo point** removes the last point, **Esc** cancels. The area can be at most 4 km². If you
     skip drawing, houses are searched 350 m around the pin instead.
  2. **Find houses** (the slider goes up to **70 houses**; **🌑 Some houses have their lights off**, 20% by default and adjustable, are shown dark and skipped): numbered 🎃 pumpkins appear for real houses from OpenStreetMap inside your area, ordered into a house-to-house
     tour, and **arrows along the streets** show the path the tour will take. Click a pumpkin and choose **▶ Start the tour here** to begin at a
     different house (the tour is re-planned from there).
  3. **Start**: by default (**📍 Start and finish at the pin**) the phone begins at your pin, walks out to the houses and walks back to finish at the pin; switch it off
     to jump straight to the first house instead. It walks street to street at about 4.5 km/h with natural speed variation, and
     pauses at every door for a **random time you choose** (**Time at each house**, default 20-70 s; some houses get a longer chat, kids stay 1.6x
     longer). Your own position is a pulsing green dot with a little walker above everything else on the map. **Stop moving** ends it, leaving the phone where it is.
  Options (switches on the tab, saved between runs): **Group of kids** (3.2 km/h and 1.6x longer pauses), **Avoid busy roads**
  (houses within 20 m of a motorway/trunk/primary/secondary road are skipped and the tour never crosses one, so it ends early
  instead; needs road data from OpenStreetMap or a saved offline area, otherwise you get a note), **Some houses have their lights off** (20% by default, adjustable; skipped and shown as 🌑), and **Realistic houses** (about
  15% of houses become a long chat at the door). Changing an option
  after **Find houses** asks you to press it again.
* **⏰ Schedule** (timed plans): build a plan of steps that run **by themselves at real clock times**, for example *8:00 PM start trick-or-treat
  (kids, realistic houses) → 8:30 PM a shaky 5 km/h walk home to an address → 9:00 PM restore real GPS*.
  1. **＋ New plan**: name, date, which phone, and **Run late steps up to N minutes** (default 10).
  2. **＋ Add step** for each thing: **when** (a clock **time**, or **N seconds / minutes after the last move**: it waits until the step before has finished and the
     phone has stopped moving, then counts N; if the phone moves again the countdown restarts) and an **action**: 🎃 trick or treat (an address, your pin or your drawn area, number of
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
* **🚨 Panic button** (top bar, or `Shift+P`): stops any trip and puts the phone at your **Panic location** immediately. Choose that spot in the
  Saved tab (**Use the current pin**, or press 🚨 next to a favorite). If none is chosen, the button takes you there to pick one.
* **★ Saved**: **Saved routes** (▶ runs one, ✎ loads it into the editor, ✕ deletes it), **Favorites** take a name plus a place name, coordinates like `48.8584, 2.2945`, or nothing (saves the
  current pin). Click one to move the pin there, **＋** adds it as a stop. **Recent** keeps your last 15 locations (each Set
  location and finished route); **★** promotes one to a favorite.
* **⬇️ Offline**: while you have internet, zoom the map so a neighborhood fills the **light-green box** (up to about 2.4 × 2.4 km; the box is
  exactly what gets saved, and turns orange if it is too big), name it
  and press **Download what I see**. The app saves, into the `offline/` folder next to `app.py`:
  * the **map tiles** (zoom 12-17),
  * the **houses** (for trick-or-treat "Find houses"), and
  * the **streets** (for walking/driving paths, using a small built-in router).
  Dashed boxes on the map show saved areas; click one in the list to jump there, ✕ deletes it. The app always tries the online
  services first and uses the saved data automatically when they are unreachable, so inside a saved area everything works with
  no data at all (a "No internet" note appears at the top). Typical size is 3-6 MB per area. To use trick-or-treat offline,
  download the area first, then draw/pin inside it. Offline routing is a simplified router: a short gap in the street data
  (up to 200 m) becomes a straight hop, anything longer reports an error. Straight line mode never needs any data.
* **📱 Phones & connection** (click the phone chip at the top; **☆** makes a phone a favorite so it sorts first and stays listed when unplugged, **✎** renames it): lists every iPhone the app can see with a **🔌 USB** or **📶 Wi-Fi**
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
By default the Mac is held awake the whole time the app is open (**Stay awake the whole time the app is open**, so it can't fall asleep while idle and cut off the phone remote); turn that switch off to hold it awake only when something needs it. Switches (in **Phones & connection**, on by default / off by default): **Keep this Mac awake when needed**
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
locspoof/settings.py   the on/off switches and adjustable times (settings.json)
locspoof/panic.py      the panic location
locspoof/remote.py     phone remote: pairing, token guard, second listener
static/remote.*        the phone's remote-control page
locspoof/gpx.py        GPX import
locspoof/routes.py     saved routes
locspoof/busy.py       busy-road geometry for trick-or-treat
locspoof/fleet.py      one manager per phone
locspoof/schedule.py   timed plans: store, validation, scheduler
locspoof/awake.py      keep the Mac awake (caffeinate) and notice sleep
locspoof/memory.py     per phone: last iOS version, and the fake location an old iOS phone may still hold
locspoof/fileio.py     crash-safe file writing used by every data file
locspoof/activity.py   the activity log
locspoof/geocode.py    Nominatim client: search + reverse (User-Agent, rate limit, cache)
locspoof/favorites.py  JSON favorites store
locspoof/movement.py   great-circle math
locspoof/server.py     FastAPI routes
locspoof/runner.py     server on a background thread, port helpers
locspoof/desktop.py    native window (pywebview) launcher
desktop.py             entry point for the window app
make_app.sh            builds the double-clickable Mac app
assets/icon.png        app icon (regenerate with tools/make_icon.py)
static/                the single-page UI: index.html, app.js, style.css; fun.js/fun.css = Fun mode (Leaflet vendored in static/vendor)
tests/                 pytest suite
```

Use this only on your own device, for development, testing or privacy purposes, and respect the terms of any
service or app you use it with.
