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
    # trick or treat
    "tot_realistic": False,  # some houses have their lights off, some long chats at the door
    "tot_kids": False,       # a group of kids: slower, longer pauses
    "tot_avoid_busy": False, # skip houses on big roads and never cross them
    # connection
    "auto_reconnect": True,  # keep going / restore the location after a cable or Wi-Fi hiccup
}


class SettingsStore:
    """``data`` is a plain dict shared (by reference) with the device managers so changes apply live."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.data: dict[str, bool] = dict(DEFAULTS)
        try:
            saved = json.loads(self.path.read_text(encoding="utf-8"))
            self.data.update({k: v for k, v in saved.items() if k in DEFAULTS and isinstance(v, bool)})
        except FileNotFoundError:
            pass
        except (ValueError, AttributeError, OSError):
            log.warning("ignoring unreadable %s", self.path)

    def update(self, changes: dict) -> dict:
        for k, v in changes.items():
            if k not in DEFAULTS:
                raise AppError("bad_setting", f"Unknown setting “{k}”.", status=422)
            if not isinstance(v, bool):
                raise AppError("bad_setting", f"Setting “{k}” must be on or off.", status=422)
        self.data.update(changes)
        try:
            atomic_write(self.path, json.dumps(self.data, indent=2).encode("utf-8"))
        except OSError:
            log.exception("could not save settings")
        return self.data
