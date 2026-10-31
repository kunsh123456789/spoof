"""Road routing through OSRM-compatible servers (free, no API key).

Uses the FOSSGIS-hosted OSRM instances at routing.openstreetmap.de, which have separate walking and
driving profiles. These are community servers: keep usage light (we cache and send at most one
request per second). A whole multi-stop route is fetched in ONE request; the response is split back
into one polyline per leg.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from typing import Optional

import httpx

from .errors import AppError

log = logging.getLogger("locspoof.routing")

USER_AGENT = "local-iphone-location-simulator/1.0 (personal localhost tool)"
BASE = "https://routing.openstreetmap.de"
# (server path, profile segment). The FOSSGIS servers pick the profile from the server path and
# expect "driving" in the URL; the second candidate is a fallback in case that changes.
ENDPOINTS = {
    "walk": [("routed-foot", "driving"), ("routed-foot", "foot")],
    "drive": [("routed-car", "driving"), ("routed-car", "car")],
}
MAX_WAYPOINTS = 90
LabelPoint = tuple[float, float]


def _dedupe(points: list[LabelPoint]) -> list[LabelPoint]:
    out: list[LabelPoint] = []
    for p in points:
        if not out or out[-1] != p:
            out.append(p)
    return out


class Router:
    def __init__(self, client: Optional[httpx.AsyncClient] = None, min_interval: float = 1.1):
        self._client = client
        self._min_interval = min_interval
        self._lock = asyncio.Lock()
        self._last = 0.0
        self._cache: OrderedDict[tuple, list[list[LabelPoint]]] = OrderedDict()

    async def route(self, waypoints: list[LabelPoint], mode: str) -> list[list[LabelPoint]]:
        """Return one polyline per leg (len(waypoints) - 1 legs); each starts/ends on the exact waypoint."""
        if mode not in ENDPOINTS:
            raise ValueError(f"Unknown routing mode {mode!r}")
        if len(waypoints) < 2:
            return []
        if len(waypoints) > MAX_WAYPOINTS:
            raise AppError("too_many_stops", f"Routes can have at most {MAX_WAYPOINTS - 1} stops.", status=422)
        key = (mode, tuple((round(a, 6), round(b, 6)) for a, b in waypoints))
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]

        coords = ";".join(f"{lon:.6f},{lat:.6f}" for lat, lon in waypoints)
        params = {"overview": "false", "geometries": "geojson", "steps": "true"}
        data = None
        async with self._lock:
            for server, profile in ENDPOINTS[mode]:
                wait = self._last + self._min_interval - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(wait)
                try:
                    resp = await self._get(f"{BASE}/{server}/route/v1/{profile}/{coords}", params)
                finally:
                    self._last = time.monotonic()
                if resp is None:
                    continue
                if resp.status_code == 429:
                    raise AppError("rate_limited", "The routing service is busy.", "Wait a few seconds and retry.", 429)
                try:
                    body = resp.json()
                except ValueError:
                    body = {}
                if resp.status_code == 200 and body.get("code") == "Ok":
                    data = body
                    break
                if body.get("code") == "NoRoute":
                    raise AppError(
                        "no_route",
                        f"No {'walking' if mode == 'walk' else 'driving'} route between those points.",
                        "Pick points connected by roads, or switch to Straight line.",
                        422,
                    )
                log.warning("routing attempt failed (%s %s): %s", server, profile, resp.status_code)
        if data is None:
            raise AppError(
                "route_failed",
                "Could not get a road route.",
                "The routing service may be down or unreachable. Check your internet, or use Straight line.",
                502,
            )
        legs = self._parse(data, waypoints)
        self._cache[key] = legs
        while len(self._cache) > 32:
            self._cache.popitem(last=False)
        return legs

    async def _get(self, url: str, params: dict) -> Optional[httpx.Response]:
        headers = {"User-Agent": USER_AGENT}
        try:
            if self._client is not None:
                return await self._client.get(url, params=params, headers=headers, timeout=20)
            async with httpx.AsyncClient() as c:
                return await c.get(url, params=params, headers=headers, timeout=20)
        except httpx.HTTPError as e:
            log.error("routing request failed: %r", e)
            return None

    @staticmethod
    def _parse(data: dict, waypoints: list[LabelPoint]) -> list[list[LabelPoint]]:
        try:
            legs_raw = data["routes"][0]["legs"]
            legs = []
            for i, leg in enumerate(legs_raw):
                pts: list[LabelPoint] = [waypoints[i]]
                for step in leg["steps"]:
                    pts.extend((lat, lon) for lon, lat in step["geometry"]["coordinates"])
                pts.append(waypoints[i + 1])
                legs.append(_dedupe(pts))
        except (KeyError, IndexError, TypeError, ValueError) as e:
            log.error("unexpected routing response: %r", e)
            raise AppError("route_failed", "The routing service sent an unexpected answer.", status=502) from e
        if len(legs) != len(waypoints) - 1:
            raise AppError("route_failed", "The routing service sent an incomplete route.", status=502)
        return legs
