"""The Mac's battery (macOS `pmset`), so the phone remote can warn before the Mac runs flat and sleeps."""
from __future__ import annotations

import re
import subprocess
import sys
import time
from typing import Callable, Optional

_cache: tuple[float, dict] = (0.0, {})


def parse_pmset(text: str) -> dict:
    """{"supported", "percent", "plugged", "charging", "minutes_left"} from `pmset -g batt` output."""
    out = {"supported": False, "percent": None, "plugged": None, "charging": None, "minutes_left": None}
    if not text:
        return out
    m = re.search(r"(\d{1,3})%", text)
    if not m:
        return out                                                       # a desktop Mac: no battery
    out["supported"], out["percent"] = True, min(100, int(m.group(1)))
    out["plugged"] = "AC Power" in text.split("\n")[0] if "Now drawing from" in text else None
    low = text.lower()
    out["charging"] = bool(re.search(r"\b(charging|finishing charge)\b", low)) and "discharging" not in low and "not charging" not in low
    t = re.search(r"(\d+):(\d{2}) remaining", text)
    if t:
        out["minutes_left"] = int(t.group(1)) * 60 + int(t.group(2))
    return out


def read(runner: Optional[Callable[[], str]] = None, max_age: float = 10.0) -> dict:
    """Battery state, cached for a few seconds (the remote asks every few seconds)."""
    global _cache
    if runner is None and time.monotonic() - _cache[0] < max_age and _cache[1]:
        return _cache[1]
    try:
        if runner is not None:
            text = runner()
        elif sys.platform == "darwin":
            text = subprocess.run(["pmset", "-g", "batt"], capture_output=True, text=True, timeout=4).stdout
        else:
            text = ""
    except (OSError, subprocess.SubprocessError):
        text = ""
    info = parse_pmset(text)
    if runner is None:
        _cache = (time.monotonic(), info)
    return info
