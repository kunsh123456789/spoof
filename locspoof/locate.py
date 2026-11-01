"""An approximate "where am I" for the Mac, from its internet address (used when the phone is not being spoofed).

This is the Mac's area (city level), NOT the iPhone's GPS: iOS gives a computer no way to read the phone's real location.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

import httpx

log = logging.getLogger("locspoof.locate")
URL = "https://ipwho.is/"
TTL_S = 600.0


class IpLocator:
    def __init__(self, client: Optional[httpx.AsyncClient] = None, clock=time.monotonic):
        self._client, self._clock = client, clock
        self._cache: Optional[tuple[float, dict]] = None

    async def where(self) -> dict:
        if self._cache and self._clock() - self._cache[0] < TTL_S:
            return self._cache[1]
        out: dict = {"ok": False}
        try:
            if self._client is not None:
                r = await self._client.get(URL, timeout=6)
            else:
                async with httpx.AsyncClient() as c:
                    r = await c.get(URL, timeout=6)
            d = r.json()
            if d.get("success", True) and d.get("latitude") is not None:
                label = ", ".join(x for x in (d.get("city"), d.get("region")) if x)
                out = {"ok": True, "lat": float(d["latitude"]), "lon": float(d["longitude"]), "label": label or d.get("country") or "your area", "approximate": True}
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as e:
            log.info("could not look up the Mac's area: %r", e)
        if out["ok"]:
            self._cache = (self._clock(), out)
        return out
