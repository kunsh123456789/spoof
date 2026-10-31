"""Trick-or-treat route planning: find houses near a point (OpenStreetMap via Overpass) and order
them into a believable house-to-house tour with random pauses at each door."""
from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Optional

import httpx

from .errors import AppError
from .movement import haversine_m

log = logging.getLogger("locspoof.tot")

USER_AGENT = "local-iphone-location-simulator/1.0 (personal localhost tool)"
OVERPASS_URLS = ["https://overpass-api.de/api/interpreter", "https://overpass.kumi.systems/api/interpreter"]
HOUSE_TYPES = "house|residential|detached|semidetached_house|terrace|bungalow|cabin|farm"
MIN_HOUSES = 3
DEDUPE_M = 12.0
Point = tuple[float, float]


def build_query(lat: float, lon: float, radius_m: int) -> str:
    around = f"(around:{radius_m},{lat:.6f},{lon:.6f})"
    return (
        "[out:json][timeout:25];(\n"
        f'  way["building"~"^({HOUSE_TYPES})$"]{around};\n'
        f'  way["building"]["addr:housenumber"]{around};\n'
        f'  node["addr:housenumber"]{around};\n'
        ");out center 800;"
    )


def parse_points(data: dict) -> list[Point]:
    pts = []
    for el in data.get("elements", []):
        src = el.get("center") or el
        if "lat" in src and "lon" in src:
            pts.append((float(src["lat"]), float(src["lon"])))
    return pts


def dedupe(points: list[Point], min_dist: float = DEDUPE_M) -> list[Point]:
    kept: list[Point] = []
    for p in points:
        if all(haversine_m(*p, *q) >= min_dist for q in kept):
            kept.append(p)
    return kept


def build_plan(
    points: list[Point], center: Point, count: int, rng: Optional[random.Random] = None,
    dwell_range: tuple[float, float] = (20.0, 70.0),
) -> list[dict]:
    """Greedy nearest-neighbour tour starting at the house nearest ``center``, with random dwell times
    (about one stop in five is a longer chat at the door)."""
    rng = rng or random.Random()
    remaining = dedupe(points)
    if not remaining:
        return []
    cur = min(remaining, key=lambda p: haversine_m(*center, *p))
    tour = []
    while remaining and len(tour) < count:
        remaining.remove(cur)
        tour.append(cur)
        if remaining:
            cur = min(remaining, key=lambda p: haversine_m(*cur, *p))
    plan = []
    for lat, lon in tour:
        dwell = rng.uniform(*dwell_range)
        if rng.random() < 0.2:
            dwell += rng.uniform(20, 50)
        plan.append({"lat": lat, "lon": lon, "dwell_s": round(dwell, 1)})
    return plan


class HouseFinder:
    def __init__(self, client: Optional[httpx.AsyncClient] = None, min_interval: float = 2.0):
        self._client = client
        self._min_interval = min_interval
        self._lock = asyncio.Lock()
        self._last = 0.0

    async def plan(self, lat: float, lon: float, radius_m: int, count: int, rng: Optional[random.Random] = None) -> list[dict]:
        points = await self._fetch(lat, lon, radius_m)
        plan = build_plan(points, (lat, lon), count, rng)
        if len(plan) < MIN_HOUSES:
            raise AppError(
                "no_houses",
                f"Found only {len(plan)} house(s) within {radius_m} m.",
                "Move the pin to a residential street, or increase the radius.",
                404,
            )
        return plan

    async def _fetch(self, lat: float, lon: float, radius_m: int) -> list[Point]:
        query = build_query(lat, lon, radius_m)
        last_err = "no response"
        async with self._lock:
            for url in OVERPASS_URLS:
                wait = self._last + self._min_interval - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(wait)
                try:
                    resp = await self._post(url, query)
                except httpx.HTTPError as e:
                    last_err = repr(e)
                    continue
                finally:
                    self._last = time.monotonic()
                if resp.status_code == 200:
                    try:
                        return parse_points(resp.json())
                    except ValueError:
                        last_err = "invalid JSON"
                        continue
                last_err = f"HTTP {resp.status_code}"
        log.error("Overpass failed: %s", last_err)
        raise AppError(
            "houses_unavailable",
            "Could not look up houses from OpenStreetMap.",
            "The free Overpass service may be busy or offline. Wait a minute and try again.",
            502,
        )

    async def _post(self, url: str, query: str) -> httpx.Response:
        headers = {"User-Agent": USER_AGENT}
        if self._client is not None:
            return await self._client.post(url, data={"data": query}, headers=headers, timeout=40)
        async with httpx.AsyncClient() as c:
            return await c.post(url, data={"data": query}, headers=headers, timeout=40)
