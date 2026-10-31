"""Recently used locations (most recent first), stored in a local JSON file."""
from __future__ import annotations

import time

from .favorites import FavoritesStore
from .movement import haversine_m, validate_coords

MAX_RECENT = 15
SAME_PLACE_M = 15.0


class RecentsStore(FavoritesStore):
    def add(self, lat: float, lon: float, label: str | None = None) -> dict:  # type: ignore[override]
        validate_coords(lat, lon)
        with self._lock:
            items = self._read()
            old = next((i for i in items if haversine_m(i["lat"], i["lon"], lat, lon) <= SAME_PLACE_M), None)
            if old:
                items.remove(old)
            name = (label or "").strip()[:80] or (old["name"] if old and not _is_coords(old["name"]) else "") or _coords(lat, lon)
            item = {"id": old["id"] if old else f"{int(time.time() * 1000):x}", "name": name, "lat": lat, "lon": lon}
            items.insert(0, item)
            self._write(items[:MAX_RECENT])
            return item

    def clear(self) -> None:
        with self._lock:
            self._write([])


def _coords(lat: float, lon: float) -> str:
    return f"{lat:.5f}, {lon:.5f}"


def _is_coords(name: str) -> bool:
    parts = name.split(",")
    try:
        return len(parts) == 2 and all(float(p) is not None for p in parts)
    except ValueError:
        return False
