"""Pure geometry helpers for gradual movement (no device access)."""
from __future__ import annotations

import math

EARTH_RADIUS_M = 6_371_008.8


def validate_coords(lat: float, lon: float) -> None:
    if not (math.isfinite(lat) and math.isfinite(lon)):
        raise ValueError("Coordinates must be finite numbers.")
    if not -90 <= lat <= 90:
        raise ValueError("Latitude must be between -90 and 90.")
    if not -180 <= lon <= 180:
        raise ValueError("Longitude must be between -180 and 180.")


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))


def step_towards(
    lat: float, lon: float, dest_lat: float, dest_lon: float, distance_m: float
) -> tuple[float, float, bool]:
    """Move ``distance_m`` along the great circle towards the destination.

    Returns ``(lat, lon, arrived)``; arrives exactly on the destination when the
    remaining distance is within the step.
    """
    total = haversine_m(lat, lon, dest_lat, dest_lon)
    if total <= distance_m or total < 0.01:
        return dest_lat, dest_lon, True
    f = distance_m / total
    d = total / EARTH_RADIUS_M
    p1, l1 = math.radians(lat), math.radians(lon)
    p2, l2 = math.radians(dest_lat), math.radians(dest_lon)
    a = math.sin((1 - f) * d) / math.sin(d)
    b = math.sin(f * d) / math.sin(d)
    x = a * math.cos(p1) * math.cos(l1) + b * math.cos(p2) * math.cos(l2)
    y = a * math.cos(p1) * math.sin(l1) + b * math.cos(p2) * math.sin(l2)
    z = a * math.sin(p1) + b * math.sin(p2)
    return math.degrees(math.atan2(z, math.hypot(x, y))), math.degrees(math.atan2(y, x)), False


class PathFollower:
    """Walks a polyline [(lat, lon), ...] by distance; handles steps that span several vertices."""

    def __init__(self, points: list[tuple[float, float]]):
        if not points:
            raise ValueError("A path needs at least one point.")
        self.points = [tuple(p) for p in points]
        self.pos = self.points[0]
        self._next = 1
        self._remaining = sum(haversine_m(*a, *b) for a, b in zip(self.points, self.points[1:]))

    @property
    def remaining_m(self) -> float:
        return max(self._remaining, 0.0)

    @property
    def done(self) -> bool:
        return self._next >= len(self.points)

    def advance(self, distance_m: float) -> tuple[float, float, bool]:
        """Move ``distance_m`` along the path. Returns (lat, lon, done)."""
        while distance_m > 0 and not self.done:
            target = self.points[self._next]
            seg = haversine_m(*self.pos, *target)
            if seg <= distance_m:
                distance_m -= seg
                self._remaining -= seg
                self.pos = target
                self._next += 1
            else:
                lat, lon, _ = step_towards(*self.pos, *target, distance_m)
                self._remaining -= distance_m
                self.pos = (lat, lon)
                distance_m = 0
        return self.pos[0], self.pos[1], self.done
