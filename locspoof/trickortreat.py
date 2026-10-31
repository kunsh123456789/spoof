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
from .busy import BusyRoads, busy_query, parse_busy_roads
from .movement import Polygon, haversine_m, point_in_polygon

log = logging.getLogger("locspoof.tot")

USER_AGENT = "local-iphone-location-simulator/1.0 (personal localhost tool)"
OVERPASS_URLS = ["https://overpass-api.de/api/interpreter", "https://overpass.kumi.systems/api/interpreter"]
HOUSE_TYPES = "house|residential|detached|semidetached_house|terrace|bungalow|cabin|farm"
MIN_HOUSES = 3
DEDUPE_M = 12.0
Point = tuple[float, float]


def build_query(lat: float, lon: float, radius_m: int, polygon: Optional[Polygon] = None) -> str:
    """Overpass QL for houses inside ``polygon`` (if given) or within ``radius_m`` of the point."""
    if polygon:
        area = '(poly:"' + " ".join(f"{la:.6f} {lo:.6f}" for la, lo in polygon) + '")'
    else:
        area = f"(around:{radius_m},{lat:.6f},{lon:.6f})"
    return (
        "[out:json][timeout:25];(\n"
        f'  way["building"~"^({HOUSE_TYPES})$"]{area};\n'
        f'  way["building"]["addr:housenumber"]{area};\n'
        f'  node["addr:housenumber"]{area};\n'
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


BUSY_ROAD_MARGIN_M = 20.0   # houses closer than this to a big road are skipped when avoiding busy roads
KIDS_DWELL_FACTOR = 1.6
CROSS_TEST_LIMIT = 40        # nearest candidates examined per step when looking for a leg that crosses no big road
LIGHTS_OFF_P, CHAT_P = 0.15, 0.15


def build_plan(
    points: list[Point], center: Point, count: int, rng: Optional[random.Random] = None,
    dwell_range: tuple[float, float] = (20.0, 70.0), *, realistic: bool = False, kids: bool = False,
    busy: Optional[BusyRoads] = None, info: Optional[dict] = None,
) -> list[dict]:
    """Greedy nearest-neighbour tour starting at the house nearest ``center``, with random dwell times
    (about one stop in five is a longer chat at the door).

    * ``busy``: houses within 20 m of a big road are dropped and no leg of the tour may cross one (the tour
      simply ends early rather than crossing).
    * ``kids``: a group of children dawdles, so every pause is 1.6x longer.
    * ``realistic``: about 15% of houses have their lights off (marked ``lights_off``, to be skipped) and
      about 15% turn into a long chat at the door.
    ``info`` (optional) receives {"skipped_busy": n, "cut_by_busy": bool} for the UI.
    """
    rng = rng or random.Random()
    info = info if info is not None else {}
    remaining = dedupe(points)
    info.update(skipped_busy=0, cut_by_busy=False)
    if busy:
        kept = [p for p in remaining if not busy.near(p, BUSY_ROAD_MARGIN_M)]
        info["skipped_busy"] = len(remaining) - len(kept)
        remaining = kept
    if not remaining:
        return []
    cur = min(remaining, key=lambda p: haversine_m(*center, *p))
    tour = []
    while remaining and len(tour) < count:
        remaining.remove(cur)
        tour.append(cur)
        if not remaining:
            break
        ranked = sorted(remaining, key=lambda p: haversine_m(*cur, *p))
        if not busy:
            cur = ranked[0]
            continue
        nxt = next((p for p in ranked[:CROSS_TEST_LIMIT] if not busy.crosses(cur, p)), None)
        if nxt is None:                       # every nearby house is across a big road: stop here
            info["cut_by_busy"] = True
            break
        cur = nxt
    plan = []
    for i, (lat, lon) in enumerate(tour):
        lights_off = realistic and i > 0 and rng.random() < LIGHTS_OFF_P
        dwell = rng.uniform(*dwell_range)
        if rng.random() < 0.2:
            dwell += rng.uniform(20, 50)
        if realistic and rng.random() < CHAT_P:
            dwell += rng.uniform(40, 90)
        if kids:
            dwell *= KIDS_DWELL_FACTOR
        plan.append({"lat": lat, "lon": lon, "dwell_s": 0.0 if lights_off else round(dwell, 1), "lights_off": lights_off})
    return plan


class HouseFinder:
    def __init__(self, client: Optional[httpx.AsyncClient] = None, min_interval: float = 2.0, offline=None):
        self._client = client
        self.offline = offline            # OfflineStore (fallback when Overpass is unreachable)
        self.last_source = "online"
        self.last_notes: list[str] = []
        self._min_interval = min_interval
        self._lock = asyncio.Lock()
        self._last = 0.0

    async def plan(
        self, lat: float, lon: float, radius_m: int, count: int, rng: Optional[random.Random] = None,
        polygon: Optional[Polygon] = None, *, realistic: bool = False, kids: bool = False, avoid_busy: bool = False,
    ) -> list[dict]:
        self.last_notes = []
        try:
            points = await self._fetch(lat, lon, radius_m, polygon)
            self.last_source = "online"
        except AppError as e:
            saved = self.offline.houses_near(lat, lon, radius_m, polygon) if (self.offline and e.code == "houses_unavailable") else []
            if not saved:
                raise
            log.info("Overpass unreachable; using %d houses from offline data", len(saved))
            points, self.last_source = saved, "offline"
        if polygon:  # Overpass may return buildings that merely touch the shape; keep only those inside it
            points = [p for p in points if point_in_polygon(p[0], p[1], polygon)]
        busy = await self._busy_roads(lat, lon, radius_m, polygon) if avoid_busy else None
        info: dict = {}
        plan = build_plan(points, (lat, lon), count, rng, realistic=realistic, kids=kids, busy=busy or None, info=info)
        if avoid_busy and busy is None:
            self.last_notes.append("Could not load road data, so busy roads were not avoided.")
        elif avoid_busy and not busy:
            self.last_notes.append("No major roads found here, so nothing needed avoiding.")
        if info.get("skipped_busy"):
            self.last_notes.append(f"Skipped {info['skipped_busy']} house(s) right next to busy roads.")
        if info.get("cut_by_busy"):
            self.last_notes.append("The tour stops early because the next houses are across a busy road.")
        if len(plan) < MIN_HOUSES:
            where = "inside the area you drew" if polygon else f"within {radius_m} m"
            raise AppError(
                "no_houses",
                f"Found only {len(plan)} house(s) {where}.",
                ("Turn off “Avoid busy roads”, d" if avoid_busy and (info.get("skipped_busy") or info.get("cut_by_busy")) else "D")
                + ("raw a bigger area over a residential neighborhood." if polygon else "rop the pin on a residential street, or draw an area."),
                404,
            )
        return plan

    async def _busy_roads(self, lat: float, lon: float, radius_m: int, polygon: Optional[Polygon]) -> Optional[BusyRoads]:
        """Busy-road geometry from Overpass, else from a saved offline area; None if neither is available."""
        origin = (lat, lon)
        try:
            return BusyRoads(parse_busy_roads(await self.query_json(busy_query(lat, lon, radius_m, polygon))), origin)
        except AppError:
            lines = self.offline.busy_roads(lat, lon, polygon) if self.offline else None
            return BusyRoads(lines, origin) if lines is not None else None

    async def _fetch(self, lat: float, lon: float, radius_m: int, polygon: Optional[Polygon] = None) -> list[Point]:
        return parse_points(await self.query_json(build_query(lat, lon, radius_m, polygon)))

    async def query_json(self, query: str) -> dict:
        """Run an Overpass query (trying the mirror too) and return the parsed JSON."""
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
                        return resp.json()
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
