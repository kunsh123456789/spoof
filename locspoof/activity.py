"""The activity log: a timeline of what the app did ("Location set to ...", "Trip finished", "Mac slept 12 min")."""
from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from .fileio import atomic_write

log = logging.getLogger("locspoof.activity")


class ActivityLog:
    """Keeps the most recent events in memory and on disk (activity.json), so the timeline survives a restart."""

    def __init__(self, path: Optional[Path] = None, limit: int = 600, clock: Callable[[], float] = time.time):
        self.path, self.limit, self.clock = Path(path) if path else None, limit, clock
        self._lock = threading.Lock()
        self._events: list[dict] = []
        self._next = 1
        if self.path:
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                self._events = [e for e in data if isinstance(e, dict) and {"id", "ts", "text"} <= e.keys()][-limit:]
                self._next = max((e["id"] for e in self._events), default=0) + 1
            except (FileNotFoundError, ValueError, TypeError, OSError):
                pass

    def add(self, text: str, kind: str = "info", level: str = "info", phone: Optional[str] = None, tag: Optional[str] = None) -> dict:
        if level not in ("info", "warn", "error"):
            level = "info"
        with self._lock:
            ev = {"id": self._next, "ts": self.clock(), "kind": kind, "level": level, "text": str(text)[:300], "phone": phone}
            if tag:
                ev["tag"] = tag                  # what happened, for icons: up / down / drop / sleep / switch / remote_up / remote_down
            self._next += 1
            self._events.append(ev)
            del self._events[: max(len(self._events) - self.limit, 0)]
            self._save()
        return ev

    def list(self, after: int = 0, limit: int = 200, kinds: Optional[set] = None) -> list[dict]:
        with self._lock:
            return [e for e in self._events if e["id"] > after and (not kinds or e["kind"] in kinds)][-limit:]

    @property
    def last_id(self) -> int:
        with self._lock:
            return self._next - 1

    def clear(self) -> None:
        with self._lock:
            self._events = []
            self._save()

    def _save(self) -> None:
        if self.path:
            try:
                atomic_write(self.path, json.dumps(self._events).encode("utf-8"))
            except OSError:
                log.debug("could not save the activity log", exc_info=True)
