"""Saved routes: named multi-stop routes and imported GPX tracks, stored in routes.json."""
from __future__ import annotations

import time
import uuid

from .errors import AppError
from .favorites import FavoritesStore
from .movement import validate_coords

MODES = ("straight", "walk", "drive")
MAX_STOPS, MAX_PATH = 60, 20_000


class RoutesStore(FavoritesStore):
    required = {"id", "name", "kind"}

    def add(self, name: str, kind: str, stops: list[dict], path: list | None, mode: str, speed_kmh: float,  # type: ignore[override]
            loop: bool = False, return_to_start: bool = False) -> dict:
        name = " ".join(name.split())[:80]
        if not name:
            raise AppError("bad_name", "Give the route a name.", status=422)
        if kind not in ("stops", "track"):
            raise AppError("bad_route", "Route kind must be stops or track.", status=422)
        if mode not in MODES:
            raise AppError("bad_route", "Mode must be straight, walk or drive.", status=422)
        if not 0.5 <= speed_kmh <= 400:
            raise AppError("bad_route", "Speed must be between 0.5 and 400 km/h.", status=422)
        if not 1 <= len(stops) <= MAX_STOPS:
            raise AppError("bad_route", f"A route needs 1 to {MAX_STOPS} stops.", status=422)
        clean = []
        for s in stops:
            try:
                validate_coords(float(s["lat"]), float(s["lon"]))
                smode, sspeed = s.get("mode") or None, s.get("speed_kmh") or None
                if smode is not None and smode not in MODES:
                    raise ValueError("bad stop mode")
                if sspeed is not None and not 0.5 <= float(sspeed) <= 400:
                    raise ValueError("bad stop speed")
                clean.append({"lat": float(s["lat"]), "lon": float(s["lon"]), "dwell_s": max(0.0, float(s.get("dwell_s", 0))),
                              "mode": smode, "speed_kmh": float(sspeed) if sspeed else None, "label": str(s.get("label") or "")[:60]})
            except (KeyError, TypeError, ValueError) as e:
                raise AppError("bad_route", f"One of the stops is invalid ({e}).", status=422) from e
        item = {"id": uuid.uuid4().hex[:12], "name": name, "kind": kind, "stops": clean, "mode": mode,
                "speed_kmh": float(speed_kmh), "loop": bool(loop), "return_to_start": bool(return_to_start), "created": time.time()}
        if kind == "track":
            if not path or not 2 <= len(path) <= MAX_PATH:
                raise AppError("bad_route", "A track needs 2 or more points.", status=422)
            try:
                item["path"] = [[round(float(a), 6), round(float(b), 6)] for a, b in path]
                for a, b in item["path"]:
                    validate_coords(a, b)
            except (TypeError, ValueError) as e:
                raise AppError("bad_route", "The track has an invalid point.", status=422) from e
        with self._lock:
            items = self._read()
            items.append(item)
            self._write(items)
        return item

    def get(self, route_id: str) -> dict | None:
        return next((i for i in self.list() if i["id"] == route_id), None)
