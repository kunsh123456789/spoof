"""Several phones at once: one DeviceManager (and one backend) per phone."""
from __future__ import annotations

import logging
import time
from typing import Awaitable, Callable, Optional

from .device import Backend, DeviceManager, norm_udid

log = logging.getLogger("locspoof.fleet")
LIST_CACHE_S = 3.0


class Fleet:
    def __init__(
        self, discovery: Backend, backend_for: Callable[[Optional[str]], Backend],
        make_manager: Callable[[Backend], DeviceManager], single: Optional[DeviceManager] = None,
    ):
        """``single``: always use this one manager (the app was started with an injected backend, as in tests)."""
        self.discovery, self._backend_for, self._make_manager, self._single = discovery, backend_for, make_manager, single
        self.managers: dict[str, DeviceManager] = {}
        self._list_cache: Optional[tuple[float, list[dict]]] = None

    async def devices(self, fresh: bool = False) -> list[dict]:
        """Every visible phone plus what this app is doing with it."""
        if fresh or not self._list_cache or time.monotonic() - self._list_cache[0] > LIST_CACHE_S:
            try:
                self._list_cache = (time.monotonic(), await self.discovery.list_devices())
            except Exception:
                log.debug("device listing failed", exc_info=True)
                self._list_cache = (time.monotonic(), [])
        out = []
        for d in self._list_cache[1]:
            entry = dict(d)
            m = self.managers.get(norm_udid(d["udid"])) if not self._single else self._single
            if m is not None:
                sim = m._sim_state()
                entry["simulating"] = sim["simulation"]["active"]
                entry["moving"] = sim["walk"]["active"]
                if sim["simulation"]["active"]:
                    entry["lat"], entry["lon"] = sim["simulation"]["lat"], sim["simulation"]["lon"]
            out.append(entry)
        return out

    async def enable_developer_mode(self, udid: Optional[str] = None) -> dict:
        """Ask a phone to reveal / enable its Developer Mode switch (a one-off connection to that phone)."""
        backend = self._single.backend if self._single else self._backend_for(udid)
        out = await backend.enable_developer_mode()
        self._list_cache = None
        return out

    async def scan_wifi(self) -> list[dict]:
        found = await self.discovery.scan_wifi()
        self._list_cache = None
        return found

    async def get(self, udid: Optional[str] = None) -> DeviceManager:
        if self._single is not None:
            return self._single
        real = udid
        if not real:
            devs = await self.devices()
            real = devs[0]["udid"] if devs else None
        key = norm_udid(real) if real else "default"
        if key not in self.managers:
            self.managers[key] = self._make_manager(self._backend_for(real))
        return self.managers[key]

    def all_managers(self) -> list[DeviceManager]:
        return [self._single] if self._single else list(self.managers.values())

    async def shutdown(self) -> None:
        for m in self.all_managers():
            await m.shutdown()
