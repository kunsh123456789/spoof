"""What the app remembers about each phone between runs (devices.json):

* the last iOS version seen (to notice an iOS update), and
* for phones that KEEP a fake location after we disconnect (iOS 16 and earlier), where that fake location is, so that
  after the app was killed or crashed it can offer to restore the real GPS instead of silently forgetting.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Optional

from .fileio import atomic_write

log = logging.getLogger("locspoof.memory")


def _key(udid: str) -> str:
    return udid.replace("-", "").lower()


class DeviceMemory:
    def __init__(self, path: Path):
        self.path = Path(path)
        try:
            self.data: dict[str, dict] = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(self.data, dict):
                self.data = {}
            self.data = {k: v for k, v in self.data.items() if isinstance(v, dict)}
        except (FileNotFoundError, ValueError, OSError):
            self.data = {}

    def remember(self, udid: str, name: str, version: str) -> Optional[str]:
        """Record the phone and return the iOS version we saw last time (None the first time)."""
        rec = self.data.setdefault(_key(udid), {})
        prev = rec.get("ios_version")
        if prev != version or rec.get("name") != name:
            rec.update(name=name, ios_version=version)
            self._save()
        return prev

    def set_sim(self, udid: str, lat: float, lon: float) -> None:
        self.data.setdefault(_key(udid), {})["sim"] = {"lat": lat, "lon": lon, "at": time.time()}
        self._save()

    def get_sim(self, udid: str) -> Optional[tuple[float, float]]:
        sim = (self.data.get(_key(udid)) or {}).get("sim")
        try:
            return (float(sim["lat"]), float(sim["lon"])) if sim else None
        except (KeyError, TypeError, ValueError):
            return None

    def clear_sim(self, udid: str) -> None:
        rec = self.data.get(_key(udid))
        if rec and rec.pop("sim", None) is not None:
            self._save()

    def meta(self, udid: str) -> dict:
        """The user's own choices for a phone: a nickname and whether it is a favorite."""
        rec = self.data.get(_key(udid)) or {}
        return {"nickname": rec.get("nickname") or "", "favorite": bool(rec.get("favorite"))}

    def set_meta(self, udid: str, nickname=None, favorite=None) -> dict:
        rec = self.data.setdefault(_key(udid), {})
        if nickname is not None:
            nick = " ".join(str(nickname).split())[:40]
            if nick:
                rec["nickname"] = nick
            else:
                rec.pop("nickname", None)
        if favorite is not None:
            if favorite:
                rec["favorite"] = True
            else:
                rec.pop("favorite", None)
        self._save()
        return self.meta(udid)

    def favorites(self) -> list[tuple[str, dict]]:
        return [(k, v) for k, v in self.data.items() if v.get("favorite")]

    def _save(self) -> None:
        try:
            atomic_write(self.path, json.dumps(self.data, indent=2).encode("utf-8"))
        except OSError:
            log.warning("could not save %s", self.path)
