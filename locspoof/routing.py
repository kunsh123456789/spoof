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
    def __init__(self, client: Optional[httpx.AsyncClient] = None, min_interval: float = 1.1, offline=None):
        self._client = client
        self.offline = offline            # OfflineStore (fallback when the routing service is unreachable)
        self.retry_delays = (0.8, 2.0)    # pauses between attempts when the service errors or does not answer
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
                    resp = await self._get_retry(f"{BASE}/{server}/route/v1/{profile}/{coords}", params)
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
        if data is None and self.offline:
            legs = await asyncio.to_thread(self.offline.route, waypoints, mode)   # graph loading + A* must not stall the app
            if legs:
                log.info("routing service unreachable; used offline street data")
                return legs
        if data is None:
            raise AppError(
                "route_failed",
                "Could not get a road route.",
                "The routing service may be down or unreachable, and no offline map covers these points. Check your internet, download this area in the Offline tab, or use Straight line.",
                502,
            )
        legs = self._parse(data, waypoints)
        self._cache[key] = legs
        while len(self._cache) > 32:
            self._cache.popitem(last=False)
        return legs

    async def nearest(self, lat: float, lon: float, mode: str = "walk") -> Optional[tuple[float, float, float]]:
        """The closest spot on a real road/path: (lat, lon, metres moved). None if the service cannot say (offline, busy)."""
        if mode not in ENDPOINTS:
            mode = "walk"
        key = ("nearest", mode, round(lat, 5), round(lon, 5))
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]                      # type: ignore[return-value]
        server, profile = ENDPOINTS[mode][0]
        async with self._lock:
            wait = self._last + self._min_interval - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                resp = await self._get(f"{BASE}/{server}/nearest/v1/{profile}/{lon:.6f},{lat:.6f}", {"number": "1"})
            finally:
                self._last = time.monotonic()
        try:
            wp = resp.json()["waypoints"][0]               # type: ignore[union-attr]
            res = (float(wp["location"][1]), float(wp["location"][0]), float(wp.get("distance", 0.0)))
        except (AttributeError, ValueError, KeyError, IndexError, TypeError):
            return None
        self._cache[key] = res                           # type: ignore[assignment]
        while len(self._cache) > 200:
            self._cache.popitem(last=False)
        return res

    async def _get_retry(self, url: str, params: dict) -> Optional[httpx.Response]:
        """GET, trying again after a pause when the service errors (5xx) or does not answer at all."""
        resp = None
        for i in range(len(self.retry_delays) + 1):
            resp = await self._get(url, params)
            if resp is not None and resp.status_code < 500:
                return resp
            if i < len(self.retry_delays):
                log.info("routing service not ready (%s); retrying", "no answer" if resp is None else resp.status_code)
                await asyncio.sleep(self.retry_delays[i])
        return resp

    async def _get(self, url: str, params: dict) -> Optional[httpx.Response]:
        headers = {"User-Agent": USER_AGENT}
        try:
            if self._client is not None:
                return await self._client.get(url, params=params, headers=headers, timeout=httpx.Timeout(20, connect=5))
            async with httpx.AsyncClient() as c:
                return await c.get(url, params=params, headers=headers, timeout=httpx.Timeout(20, connect=5))
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
