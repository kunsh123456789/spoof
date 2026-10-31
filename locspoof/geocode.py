"""Nominatim (OpenStreetMap) place search, following the public usage policy:
identifying User-Agent, at most one request per second, and caching of repeated queries."""
from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from typing import Any, Optional

import httpx

from .errors import AppError

log = logging.getLogger("locspoof.geocode")

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
REVERSE_URL = "https://nominatim.openstreetmap.org/reverse"
USER_AGENT = "local-iphone-location-simulator/1.0 (personal localhost tool)"
MIN_INTERVAL_S = 1.1
CACHE_SIZE = 128


def short_label(data: dict) -> str:
    """A short name for a reverse-geocoding answer: "12 Maple Street, Springfield" rather than the full address."""
    a = data.get("address") or {}
    street = " ".join(x for x in (a.get("house_number"), a.get("road") or a.get("pedestrian") or a.get("footway") or a.get("path")) if x)
    first = street or a.get("amenity") or a.get("building") or a.get("tourism") or a.get("leisure") or a.get("shop") or ""
    city = a.get("city") or a.get("town") or a.get("village") or a.get("hamlet") or a.get("suburb") or a.get("municipality") or a.get("county") or ""
    parts = [p for p in (first, city) if p]
    return ", ".join(parts) if parts else ", ".join(x.strip() for x in str(data.get("display_name", "")).split(",")[:2]).strip(", ")


class Geocoder:
    def __init__(self, client: Optional[httpx.AsyncClient] = None, min_interval: float = MIN_INTERVAL_S):
        self._client = client
        self._min_interval = min_interval
        self.retry_delays = (1.5,)
        self._lock = asyncio.Lock()
        self._last_request = 0.0
        self._cache: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()

    async def search(self, query: str, limit: int = 5, box: Optional[tuple] = None) -> list[dict[str, Any]]:
        q = " ".join(query.split())
        if len(q) < 2:
            raise AppError("bad_query", "Type at least 2 characters to search.", status=422)
        key = q.lower() + ('|box' if box else '')
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]

        async with self._lock:  # serialises requests so the rate limit holds across callers
            wait = self._last_request + self._min_interval - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                resp = await self._get_retry(q, limit, box)
            finally:
                self._last_request = time.monotonic()

        if resp.status_code == 429:
            raise AppError("rate_limited", "Search service is rate-limiting us.", "Wait a few seconds and retry.", 429)
        if resp.status_code >= 400:
            log.error("Nominatim returned %s: %s", resp.status_code, resp.text[:200])
            raise AppError("search_failed", f"Search service error ({resp.status_code}).", status=502)
        try:
            results = [{"name": str(r["display_name"]), "lat": float(r["lat"]), "lon": float(r["lon"])} for r in resp.json()]
        except (ValueError, KeyError, TypeError) as e:
            # an HTML page from a hotel/airport Wi-Fi, a changed answer format, ...
            log.error("Nominatim sent an unusable answer: %r (%s)", e, resp.text[:120])
            raise AppError("search_failed", "The search service sent an unusable answer.",
                           "Check your internet connection (a Wi-Fi login page can cause this) and try again.", 502) from e
        self._cache[key] = results
        while len(self._cache) > CACHE_SIZE:
            self._cache.popitem(last=False)
        return results

    async def _request_retry(self, url: str, params: dict) -> httpx.Response:
        """One retry after a pause if the service errors (5xx) or cannot be reached."""
        for i in range(len(self.retry_delays) + 1):
            try:
                resp = await self._request(url, params)
                if resp.status_code < 500 or i == len(self.retry_delays):
                    return resp
            except AppError as e:
                if e.code != "search_offline" or i == len(self.retry_delays):
                    raise
            await asyncio.sleep(self.retry_delays[i])
        raise AssertionError("unreachable")

    async def _get_retry(self, q: str, limit: int, box: Optional[tuple] = None) -> httpx.Response:
        params = {"q": q, "format": "jsonv2", "limit": str(limit)}
        if box:                                                    # only look inside this rectangle (south, west, north, east)
            s, w, n, e = box
            params.update(viewbox=f"{w},{n},{e},{s}", bounded="1")
        return await self._request_retry(NOMINATIM_URL, params)

    async def reverse(self, lat: float, lon: float) -> dict[str, Any]:
        """The address at a spot: {"label": short text for the progress bar, "name": the full address}."""
        key = f"rev:{lat:.4f}:{lon:.4f}"
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]              # type: ignore[return-value]
        async with self._lock:
            wait = self._last_request + self._min_interval - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                resp = await self._request_retry(REVERSE_URL, {"lat": f"{lat:.6f}", "lon": f"{lon:.6f}", "format": "jsonv2", "zoom": "18", "addressdetails": "1"})
            finally:
                self._last_request = time.monotonic()
        if resp.status_code == 429:
            raise AppError("rate_limited", "Search service is rate-limiting us.", "Wait a few seconds and retry.", 429)
        if resp.status_code >= 400:
            raise AppError("search_failed", f"Search service error ({resp.status_code}).", status=502)
        try:
            data = resp.json()
        except ValueError as e:
            raise AppError("search_failed", "The search service sent an unusable answer.", status=502) from e
        if not isinstance(data, dict) or "error" in data or not data.get("display_name"):
            raise AppError("not_found", "No address found at this spot.", status=404)
        out = {"label": short_label(data), "name": str(data["display_name"])}
        self._cache[key] = out                   # type: ignore[assignment]
        while len(self._cache) > CACHE_SIZE:
            self._cache.popitem(last=False)
        return out

    async def _request(self, url: str, params: dict) -> httpx.Response:
        headers = {"User-Agent": USER_AGENT, "Accept-Language": "en"}
        try:
            if self._client is not None:
                return await self._client.get(url, params=params, headers=headers, timeout=10)
            async with httpx.AsyncClient() as c:
                return await c.get(url, params=params, headers=headers, timeout=10)
        except httpx.HTTPError as e:
            log.error("Nominatim request failed: %r", e)
            raise AppError(
                "search_offline", "Could not reach the search service.", "Check your internet connection.", 502
            ) from e
