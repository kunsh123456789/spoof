import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from locspoof import errors
from locspoof.device import DeviceInfo


class FakeSession:
    def __init__(self, persists: bool):
        self.persists = persists
        self.calls: list = []
        self.closed = False
        self.fail_next = False
        self.broken = False
        self.hang = False          # set() never returns (a phone that went to sleep mid-call)
        self.delay = 0.0           # set() takes this long
        self.started = 0           # calls entered / completed, to prove none is cut off half way
        self.finished = 0
        self.hang_close = False

    async def set(self, lat, lon):
        self.started += 1
        if self.hang:
            await asyncio.sleep(3600)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.broken:
            raise ConnectionResetError("cable pulled")
        if self.fail_next:
            self.fail_next = False
            raise ConnectionResetError("stale")
        self.calls.append(("set", lat, lon))
        self.finished += 1

    async def clear(self):
        self.calls.append(("clear",))

    async def close(self):
        if self.hang_close:
            await asyncio.sleep(3600)
        self.closed = True


class FakeBackend:
    """Stands in for a phone. `problem` selects which friendly error probe() raises."""

    def __init__(self, version="17.4", problem=None, udid="00008110-TEST", name="Test iPhone", connection="USB"):
        self.info = DeviceInfo(udid, name, version, "iPhone14,5", connection=connection, wifi_enabled=False, ddi_mounted=True)
        self.problem = problem
        self.sessions: list[FakeSession] = []
        self.trusted = 0
        self.probes = 0                  # how many times the phone was probed
        self.hang_probe = False
        self.hang_open = False
        self.open_delay = 0.0
        self.wifi_calls: list[bool] = []
        self.scans = 0
        self.scan_result: list[dict] = []

    async def probe(self):
        self.probes += 1
        if self.hang_probe:
            await asyncio.sleep(3600)
        if self.problem:
            raise getattr(errors, self.problem)()
        return self.info

    async def trust(self):
        self.trusted += 1
        self.problem = None

    async def set_wifi(self, enabled):
        if self.info.connection != "USB":
            from locspoof.errors import AppError
            raise AppError("wifi_needs_usb", "Plug the iPhone in with a cable.", status=409)
        self.wifi_calls.append(enabled)
        self.info.wifi_enabled = enabled

    async def list_devices(self):
        if self.problem == "no_device":
            return []
        i = self.info
        return [{"udid": i.udid, "name": i.name, "ios_version": i.ios_version, "connection": i.connection, "paired": True,
                 "developer_mode": self.developer_mode}]

    developer_mode = True               # what the fake phone reports
    developer_result = {"state": "restarting"}
    developer_calls = 0

    async def enable_developer_mode(self):
        self.developer_calls = self.developer_calls + 1
        if isinstance(self.developer_result, Exception):
            raise self.developer_result
        return dict(self.developer_result)

    async def scan_wifi(self, timeout=5.0):
        self.scans += 1
        return self.scan_result

    def unplug(self):
        """Simulate pulling the cable: existing sessions die, probes say no device."""
        self.problem = "no_device"
        for s in self.sessions:
            s.broken = True

    def replug(self):
        self.problem = None

    async def open_session(self, info):
        if self.hang_open:
            await asyncio.sleep(3600)
        if self.open_delay:
            await asyncio.sleep(self.open_delay)
        s = FakeSession(persists=info.major < 17)
        self.sessions.append(s)
        return s


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path, monkeypatch):
    """Default data files (settings.json, routes.json, favorites.json, offline/...) must never touch the repo."""
    import locspoof.server as server
    monkeypatch.setattr(server, "ROOT", tmp_path / "default-data")


@pytest.fixture
def backend():
    return FakeBackend()


class FakeRouter:
    """Roads that detour via a corner point, so tests can tell road routes from straight lines."""

    def __init__(self):
        self.calls: list = []

    async def route(self, waypoints, mode):
        self.calls.append((list(waypoints), mode))
        legs = []
        for (a, b) in zip(waypoints, waypoints[1:]):
            legs.append([a, (a[0], b[1]), b])  # go along latitude-constant first, then turn
        return legs


class FakeHouses:
    def __init__(self):
        self.calls: list = []
        self.last_notes: list = []

    async def plan(self, lat, lon, radius_m, count, rng=None, polygon=None, **opts):
        self.calls.append((lat, lon, radius_m, count) if polygon is None else (lat, lon, radius_m, count, polygon))
        self.opts = opts
        return [{"lat": lat + i * 0.0001, "lon": lon, "dwell_s": 0.02, "lights_off": False} for i in range(min(count, 5))]
