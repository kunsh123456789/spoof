"""Real stop signs and traffic lights from OpenStreetMap, so a drive waits where a real driver would."""
from __future__ import annotations

import math
from typing import Optional

from .movement import haversine_m

MAX_SPAN_DEG = 0.35        # a route bigger than this (about 35 km) is not looked up: random stops are used instead
NEAR_ROUTE_M = 14.0        # a sign counts as "on the route" when the road passes this close
Signal = dict              # {"lat", "lon", "kind": "stop_sign" | "signals"}


def bbox_of(points: list, pad: float = 0.0008) -> Optional[tuple[float, float, float, float]]:
    if not points:
        return None
    lats, lons = [p[0] for p in points], [p[1] for p in points]
    s, n, w, e = min(lats) - pad, max(lats) + pad, min(lons) - pad, max(lons) + pad
    return None if (n - s > MAX_SPAN_DEG or e - w > MAX_SPAN_DEG) else (s, w, n, e)


def build_query(bbox: tuple[float, float, float, float]) -> str:
    s, w, n, e = bbox
    return f'[out:json][timeout:25];(node["highway"="stop"]({s:.6f},{w:.6f},{n:.6f},{e:.6f});node["highway"="traffic_signals"]({s:.6f},{w:.6f},{n:.6f},{e:.6f}););out 3000;'


def parse(data: dict) -> list[Signal]:
    out = []
    for el in (data or {}).get("elements", []):
        try:
            hw = el["tags"]["highway"]
            lat, lon = float(el["lat"]), float(el["lon"])
        except (KeyError, TypeError, ValueError):
            continue
        if hw in ("stop", "traffic_signals"):
            out.append({"lat": lat, "lon": lon, "kind": "stop_sign" if hw == "stop" else "signals"})
    return out


def _dist_to_segment_m(p, a, b) -> float:
    k = math.cos(math.radians(p[0])) * 111320.0
    ax, ay, bx, by, px, py = (a[1] - p[1]) * k, (a[0] - p[0]) * 110540.0, (b[1] - p[1]) * k, (b[0] - p[0]) * 110540.0, 0.0, 0.0
    dx, dy = bx - ax, by - ay
    t = 0.0 if dx == dy == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(ax + t * dx - px, ay + t * dy - py)


def along(path: list, signals: list[Signal], start_skip_m: float = 15.0) -> list[Signal]:
    """The signals the road passes (within NEAR_ROUTE_M), as fresh copies in driving order, none at the very start."""
    if len(path) < 2:
        return []
    found = []
    cum = 0.0
    for a, b in zip(path, path[1:]):
        seg = haversine_m(*a, *b)
        for s in signals:
            if abs(s["lat"] - a[0]) > 0.003 and abs(s["lat"] - b[0]) > 0.003 and not min(a[0], b[0]) - 0.0004 <= s["lat"] <= max(a[0], b[0]) + 0.0004:
                continue
            if _dist_to_segment_m((s["lat"], s["lon"]), a, b) <= NEAR_ROUTE_M and haversine_m(s["lat"], s["lon"], *path[0]) > start_skip_m:
                found.append((cum + haversine_m(*a, s["lat"], s["lon"]), s))
        cum += seg
    found.sort(key=lambda t: t[0])
    seen, out = [], []
    for _, s in found:                          # one signal can touch two segments; keep it once
        if (s["lat"], s["lon"]) not in seen:
            seen.append((s["lat"], s["lon"]))
            out.append({**s, "done": False})
    return out
