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

    async def set(self, lat, lon):
        if self.fail_next:
            self.fail_next = False
            raise ConnectionResetError("stale")
        self.calls.append(("set", lat, lon))

    async def clear(self):
        self.calls.append(("clear",))

    async def close(self):
        self.closed = True


class FakeBackend:
    """Stands in for a phone. `problem` selects which friendly error probe() raises."""

    def __init__(self, version="17.4", problem=None):
        self.info = DeviceInfo("00008110-TEST", "Test iPhone", version, "iPhone14,5")
        self.problem = problem
        self.sessions: list[FakeSession] = []
        self.trusted = 0

    async def probe(self):
        if self.problem:
            raise getattr(errors, self.problem)()
        return self.info

    async def trust(self):
        self.trusted += 1
        self.problem = None

    async def open_session(self, info):
        s = FakeSession(persists=info.major < 17)
        self.sessions.append(s)
        return s


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

    async def plan(self, lat, lon, radius_m, count, rng=None, polygon=None):
        self.calls.append((lat, lon, radius_m, count) if polygon is None else (lat, lon, radius_m, count, polygon))
        return [{"lat": lat + i * 0.0001, "lon": lon, "dwell_s": 0.02} for i in range(min(count, 5))]
