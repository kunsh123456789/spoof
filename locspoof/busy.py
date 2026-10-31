"""Busy-road geometry for trick-or-treat tours: which houses sit on big roads, and which legs would cross one."""
from __future__ import annotations

import math
from collections import defaultdict
from typing import Iterable, Optional

from .movement import EARTH_RADIUS_M

BUSY_HIGHWAYS = {"motorway", "motorway_link", "trunk", "trunk_link", "primary", "primary_link", "secondary", "secondary_link"}
CELL_M = 100.0
Point = tuple[float, float]


def parse_busy_roads(data: dict) -> list[list[Point]]:
    """Polylines of the busy roads in an Overpass ``out geom`` answer."""
    out = []
    for el in data.get("elements", []):
        if el.get("type") == "way" and el.get("tags", {}).get("highway") in BUSY_HIGHWAYS:
            pts = [(p["lat"], p["lon"]) for p in el.get("geometry", []) if p]
            if len(pts) >= 2:
                out.append(pts)
    return out


def busy_query(lat: float, lon: float, radius_m: int, polygon: Optional[list[Point]] = None) -> str:
    if polygon:
        area = '(poly:"' + " ".join(f"{la:.6f} {lo:.6f}" for la, lo in polygon) + '")'
    else:
        area = f"(around:{radius_m + 150},{lat:.6f},{lon:.6f})"
    hw = "|".join(sorted(BUSY_HIGHWAYS))
    return f'[out:json][timeout:25];way["highway"~"^({hw})$"]{area};out geom;'


def _orient(a, b, c) -> float:
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def _segments_intersect(p1, p2, q1, q2) -> bool:
    """Proper crossing of segment p1-p2 with q1-q2 (touching an end point does not count)."""
    return _orient(q1, q2, p1) * _orient(q1, q2, p2) < 0 and _orient(p1, p2, q1) * _orient(p1, p2, q2) < 0


def _point_seg_dist(p, a, b) -> float:
    dx, dy = b[0] - a[0], b[1] - a[1]
    n = dx * dx + dy * dy
    t = 0.0 if n == 0 else max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / n))
    return math.hypot(p[0] - (a[0] + t * dx), p[1] - (a[1] + t * dy))


class BusyRoads:
    """Projects busy roads to local metres and indexes them on a grid so crossing tests stay cheap."""

    def __init__(self, polylines: Iterable[list[Point]], origin: Point):
        self.lat0, self.lon0 = origin
        self._kx = math.cos(math.radians(self.lat0)) * math.pi / 180 * EARTH_RADIUS_M
        self._ky = math.pi / 180 * EARTH_RADIUS_M
        self.segs: list[tuple[tuple[float, float], tuple[float, float]]] = []
        self._grid: dict[tuple[int, int], list[int]] = defaultdict(list)
        for pl in polylines:
            for a, b in zip(pl, pl[1:]):
                pa, pb = self._xy(a), self._xy(b)
                idx = len(self.segs)
                self.segs.append((pa, pb))
                for cell in self._cells(pa, pb):
                    self._grid[cell].append(idx)

    def __bool__(self) -> bool:
        return bool(self.segs)

    def _xy(self, p: Point) -> tuple[float, float]:
        return (p[1] - self.lon0) * self._kx, (p[0] - self.lat0) * self._ky

    def _cells(self, a, b, pad: float = 0.0):
        x0, x1 = sorted((a[0], b[0]))
        y0, y1 = sorted((a[1], b[1]))
        cx0, cx1 = int((x0 - pad) // CELL_M), int((x1 + pad) // CELL_M)
        cy0, cy1 = int((y0 - pad) // CELL_M), int((y1 + pad) // CELL_M)
        return [(cx, cy) for cx in range(cx0, cx1 + 1) for cy in range(cy0, cy1 + 1)]

    def _candidates(self, a, b, pad: float = 0.0) -> set[int]:
        out: set[int] = set()
        for cell in self._cells(a, b, pad):
            out.update(self._grid.get(cell, ()))
        return out

    def crosses(self, a: Point, b: Point) -> bool:
        """Would walking straight from a to b cross any busy road?"""
        pa, pb = self._xy(a), self._xy(b)
        return any(_segments_intersect(pa, pb, *self.segs[i]) for i in self._candidates(pa, pb))

    def near(self, p: Point, dist_m: float) -> bool:
        """Is the point within ``dist_m`` of a busy road?"""
        q = self._xy(p)
        return any(_point_seg_dist(q, *self.segs[i]) <= dist_m for i in self._candidates(q, q, pad=dist_m))
