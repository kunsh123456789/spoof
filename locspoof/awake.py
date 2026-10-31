"""Keep the Mac awake while the phone needs it, and notice when it slept anyway.

Why: on iOS 17 and later the fake location exists only while this Mac holds the connection to the phone open. A Mac that
goes to sleep drops that connection, and the phone returns to its real GPS. Nothing the app does can change that while the
Mac is truly asleep, so the app does the next best thing: it asks macOS not to sleep (with the macOS ``caffeinate`` tool)
whenever a trip, a live iOS 17+ simulation or an activated schedule needs it.

What this cannot do: closing the lid of a MacBook on battery still sleeps it. Stay plugged in with the lid open (the screen may
go dark), or use clamshell mode (power + external display + keyboard/mouse).
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import time
from typing import Callable, Optional

log = logging.getLogger("locspoof.awake")


class KeepAwake:
    """Holds a ``caffeinate`` process while wanted. The process is tied to this app's pid (``-w``) so macOS ends it as soon as
    the app exits, even if the app is killed or crashes: the Mac can never be left stuck awake."""

    def __init__(self, platform: str = sys.platform, popen: Callable = subprocess.Popen, which: Callable = shutil.which,
                 pid: Callable[[], int] = os.getpid):
        self.platform, self._popen, self._pid = platform, popen, pid
        self.supported = platform == "darwin" and bool(which("caffeinate"))
        self._proc = None
        self._screen = False
        self.reasons: list[str] = []
        self.error: Optional[str] = None
        self.since: Optional[float] = None

    @property
    def active(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def update(self, wanted: bool, reasons: Optional[list[str]] = None, screen: bool = False) -> None:
        """Make the Mac stay awake (or not). Cheap and idempotent: call it as often as you like."""
        self.reasons = list(reasons or []) if wanted else []
        if not self.supported:
            return
        if not wanted:
            self._stop()
            return
        if self.active and screen == self._screen:
            return
        self._stop()                                       # not running yet, died on its own, or the screen option changed
        cmd = ["caffeinate", "-i", "-m", "-s"] + (["-d"] if screen else []) + ["-w", str(self._pid())]
        try:
            self._proc = self._popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self._screen, self.error, self.since = screen, None, time.time()
            log.info("keeping the Mac awake (%s)", ", ".join(self.reasons) or "wanted")
        except OSError as e:
            self._proc, self.error = None, f"Could not start caffeinate: {e}"
            log.warning(self.error)

    def _stop(self) -> None:
        proc, self._proc = self._proc, None
        if proc is not None:
            try:
                if proc.poll() is None:
                    proc.terminate()
                    log.info("the Mac may sleep again")
            except OSError:
                pass
        self.since = None

    def close(self) -> None:
        self._stop()
        self.reasons = []

    def state(self) -> dict:
        return {"supported": self.supported, "platform": self.platform, "active": self.active, "screen": self._screen and self.active,
                "reasons": self.reasons, "since": self.since, "error": self.error}


class SleepWatch:
    """Detects that the computer slept: the wall clock keeps running while the monotonic clock (which does not count sleep)
    does not, so a gap between them is time spent asleep."""

    THRESHOLD_S = 15.0

    def __init__(self, wall: Callable[[], float] = time.time, mono: Callable[[], float] = time.monotonic):
        self._wall, self._mono = wall, mono
        self._last = (wall(), mono())
        self.last_sleep: Optional[dict] = None

    def check(self) -> Optional[float]:
        """Seconds the computer slept since the last check, or None."""
        w, m = self._wall(), self._mono()
        slept = (w - self._last[0]) - (m - self._last[1])
        prev_wall = self._last[0]
        self._last = (w, m)
        if slept >= self.THRESHOLD_S:
            self.last_sleep = {"slept_s": round(slept), "from": prev_wall, "woke": w}
            log.warning("the computer was asleep for about %d s", slept)
            return slept
        return None
