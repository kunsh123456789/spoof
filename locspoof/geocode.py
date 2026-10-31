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
USER_AGENT = "local-iphone-location-simulator/1.0 (personal localhost tool)"
MIN_INTERVAL_S = 1.1
CACHE_SIZE = 128


class Geocoder:
    def __init__(self, client: Optional[httpx.AsyncClient] = None, min_interval: float = MIN_INTERVAL_S):
        self._client = client
        self._min_interval = min_interval
        self.retry_delays = (1.5,)
        self._lock = asyncio.Lock()
        self._last_request = 0.0
        self._cache: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()

    async def search(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        q = " ".join(query.split())
        if len(q) < 2:
            raise AppError("bad_query", "Type at least 2 characters to search.", status=422)
        key = q.lower()
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]

        async with self._lock:  # serialises requests so the rate limit holds across callers
            wait = self._last_request + self._min_interval - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                resp = await self._get_retry(q, limit)
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

    async def _get_retry(self, q: str, limit: int) -> httpx.Response:
        """One retry after a pause if the service errors (5xx) or cannot be reached."""
        for i in range(len(self.retry_delays) + 1):
            try:
                resp = await self._get(q, limit)
                if resp.status_code < 500 or i == len(self.retry_delays):
                    return resp
            except AppError as e:
                if e.code != "search_offline" or i == len(self.retry_delays):
                    raise
            await asyncio.sleep(self.retry_delays[i])
        raise AssertionError("unreachable")

    async def _get(self, q: str, limit: int) -> httpx.Response:
        params = {"q": q, "format": "jsonv2", "limit": str(limit)}
        headers = {"User-Agent": USER_AGENT, "Accept-Language": "en"}
        try:
            if self._client is not None:
                return await self._client.get(NOMINATIM_URL, params=params, headers=headers, timeout=10)
            async with httpx.AsyncClient() as c:
                return await c.get(NOMINATIM_URL, params=params, headers=headers, timeout=10)
        except httpx.HTTPError as e:
            log.error("Nominatim request failed: %r", e)
            raise AppError(
                "search_offline", "Could not reach the search service.", "Check your internet connection.", 502
            ) from e
