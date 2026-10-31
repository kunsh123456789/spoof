"""App-wide on/off switches, persisted to settings.json so they survive restarts."""
from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path

from .errors import AppError
from .fileio import atomic_write

log = logging.getLogger("locspoof.settings")

DEFAULTS: dict[str, bool] = {
    # realism
    "noise": False,          # GPS drift while standing still / moving
    "natural_walk": False,   # varying pace, easing at corners
    "traffic_stops": False,  # random stops at junctions on driving routes
    "cooldown_hint": False,  # show a "wait this long after a big jump" timer
    "remote_enabled": False,  # let a paired phone on the same Wi-Fi control this app (opens a second, protected listener)
    "snap_pin": True,        # a dropped pin moves to the nearest road / path that makes sense
    "california_only": True,  # the map (and search) is limited to California
    # trick or treat
    "tot_realistic": False,  # some houses have their lights off, some long chats at the door
    "tot_start_at_pin": True, # start at the pin and finish back at the pin
    "tot_lights_off": True,  # some houses have their lights off (skipped)
    "tot_kids": False,       # a group of kids: slower, longer pauses
    "tot_avoid_busy": False, # skip houses on big roads and never cross them
    # connection
    "auto_reconnect": True,  # keep going / restore the location after a cable or Wi-Fi hiccup
    # keep the Mac awake
    "keep_awake": True,      # hold the Mac awake while a trip, a live iOS 17+ simulation or an activated schedule needs it
    "keep_awake_open": True, # keep the Mac awake the whole time this app is open (not only while a trip / plan needs it)
    "keep_screen_on": False, # also keep the SCREEN on (off = the screen may go dark, the Mac stays awake)
}


# Adjustable numbers: key -> (default, lowest, highest)
NUMBERS: dict[str, tuple[float, float, float]] = {
    "stop_sign_s": (3.0, 1.0, 30.0),       # wait at a stop sign
    "junction_min_s": (10.0, 1.0, 300.0),  # wait at a junction / traffic light: random between min and max
    "junction_max_s": (35.0, 1.0, 300.0),
    "tot_lights_off_pct": (20.0, 0.0, 60.0),   # percent of houses with the lights off
    "tot_dwell_min_s": (20.0, 0.0, 600.0), # time at each house while trick-or-treating: random between min and max
    "tot_dwell_max_s": (70.0, 0.0, 600.0),
    "remote_port": (8787.0, 1024.0, 65535.0),   # the port the phone remote listens on
}
PAIRS = (("junction_min_s", "junction_max_s"), ("tot_dwell_min_s", "tot_dwell_max_s"))


class SettingsStore:
    """``data`` is a plain dict shared (by reference) with the device managers so changes apply live."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.data: dict = {**DEFAULTS, **{k: v[0] for k, v in NUMBERS.items()}}
        try:
            saved = json.loads(self.path.read_text(encoding="utf-8"))
            self.data.update({k: v for k, v in saved.items() if k in DEFAULTS and isinstance(v, bool)})
            for k, (_, lo, hi) in NUMBERS.items():
                v = saved.get(k)
                if isinstance(v, (int, float)) and not isinstance(v, bool) and lo <= v <= hi:
                    self.data[k] = float(v)
            for a, b in PAIRS:
                if self.data[a] > self.data[b]:
                    self.data[a], self.data[b] = NUMBERS[a][0], NUMBERS[b][0]
        except FileNotFoundError:
            pass
        except (ValueError, AttributeError, OSError):
            log.warning("ignoring unreadable %s", self.path)

    def update(self, changes: dict) -> dict:
        clean: dict = {}
        for k, v in changes.items():
            if k in NUMBERS:
                _, lo, hi = NUMBERS[k]
                if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v or not lo <= v <= hi:
                    raise AppError("bad_setting", f"“{k}” must be a number from {lo:g} to {hi:g}.", status=422)
                clean[k] = float(v)
            elif k not in DEFAULTS:
                raise AppError("bad_setting", f"Unknown setting “{k}”.", status=422)
            elif not isinstance(v, bool):
                raise AppError("bad_setting", f"Setting “{k}” must be on or off.", status=422)
            else:
                clean[k] = v
        merged = {**self.data, **clean}
        for a, b in PAIRS:
            if merged[a] > merged[b]:
                raise AppError("bad_setting", "The shortest time can't be longer than the longest.", "Lower the first number or raise the second.", 422)
        self.data.update(clean)
        try:
            atomic_write(self.path, json.dumps(self.data, indent=2).encode("utf-8"))
        except OSError:
            log.exception("could not save settings")
        return self.data
