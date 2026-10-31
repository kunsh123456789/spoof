"""The Caffeinate update: keeping the Mac awake when needed, and noticing when it slept anyway."""
import random
import subprocess
import time

import pytest
from fastapi.testclient import TestClient

from conftest import FakeBackend, FakeHouses, FakeRouter
from locspoof.awake import KeepAwake, SleepWatch
from locspoof.server import create_app


class FakeProc:
    def __init__(self, cmd):
        self.cmd, self.terminated, self._dead = cmd, False, False

    def poll(self):
        return 1 if (self.terminated or self._dead) else None

    def terminate(self):
        self.terminated = True

    def die(self):
        self._dead = True


class FakePopen:
    def __init__(self, fail=None):
        self.procs, self.fail = [], fail

    def __call__(self, cmd, **kw):
        if self.fail:
            raise self.fail
        p = FakeProc(cmd)
        self.procs.append(p)
        return p


def mac(popen, **kw):
    return KeepAwake(platform="darwin", popen=popen, which=lambda name: "/usr/bin/caffeinate", pid=lambda: 4242, **kw)


# ---- the caffeinate process ---------------------------------------------------------------------------

def test_starts_caffeinate_tied_to_this_apps_pid():
    pop = FakePopen()
    ka = mac(pop)
    ka.update(True, ["a trip is running"])
    cmd = pop.procs[0].cmd
    assert cmd[0] == "caffeinate" and {"-i", "-m", "-s"} <= set(cmd) and "-d" not in cmd         # the screen may go dark
    assert cmd[-2:] == ["-w", "4242"]                                                           # ends by itself when the app does
    st = ka.state()
    assert st["active"] and st["reasons"] == ["a trip is running"] and st["screen"] is False and st["error"] is None


def test_is_idempotent_and_stops_when_no_longer_needed():
    pop = FakePopen()
    ka = mac(pop)
    for _ in range(5):
        ka.update(True, ["x"])
    assert len(pop.procs) == 1                                                                  # not a process per check
    ka.update(False)
    assert pop.procs[0].terminated and not ka.active and ka.state()["reasons"] == []
    ka.update(False)                                                                            # stopping twice is harmless
    ka.update(True, ["y"])
    assert len(pop.procs) == 2 and ka.active


def test_the_screen_option_restarts_it_with_display_awake_flag():
    pop = FakePopen()
    ka = mac(pop)
    ka.update(True, ["x"], screen=False)
    ka.update(True, ["x"], screen=True)
    assert pop.procs[0].terminated and "-d" in pop.procs[1].cmd and ka.state()["screen"] is True
    ka.update(True, ["x"], screen=False)
    assert "-d" not in pop.procs[2].cmd


def test_restarts_if_caffeinate_dies_on_its_own():
    pop = FakePopen()
    ka = mac(pop)
    ka.update(True, ["x"])
    pop.procs[0].die()
    assert not ka.active
    ka.update(True, ["x"])
    assert len(pop.procs) == 2 and ka.active


def test_cannot_start_means_a_message_not_a_crash():
    ka = mac(FakePopen(fail=OSError("not permitted")))
    ka.update(True, ["x"])
    assert not ka.active and "not permitted" in ka.state()["error"]


def test_unsupported_platforms_do_nothing():
    pop = FakePopen()
    for platform, which in (("linux", lambda n: "/usr/bin/caffeinate"), ("win32", lambda n: None), ("darwin", lambda n: None)):
        ka = KeepAwake(platform=platform, popen=pop, which=which)
        ka.update(True, ["x"])
        assert not ka.supported and not ka.active and ka.state()["supported"] is False
    assert pop.procs == []


def test_close_releases_everything():
    pop = FakePopen()
    ka = mac(pop)
    ka.update(True, ["x"])
    ka.close()
    assert pop.procs[0].terminated and not ka.active


def test_the_real_caffeinate_command_is_valid_on_this_machine():
    import shutil
    if not shutil.which("caffeinate"):
        pytest.skip("caffeinate exists only on macOS")
    ka = KeepAwake()
    ka.update(True, ["real"])
    try:
        assert ka.active
    finally:
        ka.close()


# ---- noticing sleep -----------------------------------------------------------------------------------------------

class Clocks:
    def __init__(self):
        self.wall, self.mono = 1_000_000.0, 500.0

    def tick(self, awake_s=2.5, asleep_s=0.0):
        self.wall += awake_s + asleep_s
        self.mono += awake_s                                     # the monotonic clock does not count sleep


def test_sleep_is_the_gap_between_the_wall_clock_and_the_monotonic_clock():
    c = Clocks()
    w = SleepWatch(wall=lambda: c.wall, mono=lambda: c.mono)
    for _ in range(10):
        c.tick()
        assert w.check() is None                                  # ordinary time passing
    c.tick(asleep_s=600)                                          # the lid was closed for 10 minutes
    slept = w.check()
    assert 595 < slept < 605 and 595 < w.last_sleep["slept_s"] < 605 and w.last_sleep["woke"] == c.wall
    assert w.last_sleep["woke"] - w.last_sleep["from"] > 600
    c.tick()
    assert w.check() is None and w.last_sleep["slept_s"] > 595    # remembered until the next sleep


def test_small_clock_adjustments_are_not_mistaken_for_sleep():
    c = Clocks()
    w = SleepWatch(wall=lambda: c.wall, mono=lambda: c.mono)
    c.tick(asleep_s=3)                                            # an NTP nudge or a busy moment
    assert w.check() is None and w.last_sleep is None


# ---- when the app asks for the Mac to stay awake ---------------------------------------------------------------------

class SpyAwake:
    supported = True
    active = False

    def __init__(self):
        self.calls = []
        self.error = None

    def update(self, wanted, reasons=None, screen=False):
        self.calls.append((wanted, list(reasons or []), screen))

    def state(self):
        w, r, s = self.calls[-1] if self.calls else (False, [], False)
        return {"supported": True, "platform": "darwin", "active": w, "screen": s and w, "reasons": r, "since": None, "error": None}

    def close(self):
        self.calls.append(("closed",))

    @property
    def last(self):
        return next((c for c in reversed(self.calls) if c[0] != "closed"), None)


def make(tmp_path, backend, **kw):
    spy = kw.pop("keep_awake", None) or SpyAwake()
    app = create_app(backend=backend, router=FakeRouter(), house_finder=FakeHouses(), favorites_path=tmp_path / "f.json",
                     recents_path=tmp_path / "r.json", offline_root=tmp_path / "o", settings_path=tmp_path / "s.json",
                     routes_path=tmp_path / "rt.json", devices_path=tmp_path / "d.json", schedules_path=tmp_path / "sch.json",
                     keep_awake=spy, awake_interval=0.03, walk_tick=0.01, ambient_tick=0.5, rng=random.Random(1), **kw)
    return app, spy


def wait_for(fn, timeout=8):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(0.03)
    pytest.fail("condition not met in time")


def test_modern_ios_simulation_keeps_the_mac_awake_until_it_is_cleared(tmp_path):
    app, spy = make(tmp_path, FakeBackend(version="17.4"))
    with TestClient(app) as c:
        time.sleep(0.2)
        assert spy.last[0] is False                                                  # idle: no reason to stay awake
        c.post("/api/location", json={"lat": 1, "lon": 2})
        wait_for(lambda: spy.last[0] is True)
        assert "connection" in spy.last[1][0]
        c.delete("/api/location")
        wait_for(lambda: spy.last[0] is False)
    assert spy.calls[-1] == ("closed",)                                              # released when the app quits


def test_old_ios_idle_simulation_does_not_need_the_mac_awake(tmp_path):
    app, spy = make(tmp_path, FakeBackend(version="15.7"))
    with TestClient(app) as c:
        c.post("/api/location", json={"lat": 1, "lon": 2})                           # the phone itself keeps this
        time.sleep(0.3)
        assert spy.last[0] is False


def test_a_running_trip_keeps_the_mac_awake_even_on_old_ios_and_releases_it_after(tmp_path):
    app, spy = make(tmp_path, FakeBackend(version="15.7"))
    with TestClient(app) as c:
        c.post("/api/location", json={"lat": 0, "lon": 0})
        c.post("/api/journey", json={"stops": [{"lat": 0, "lon": 0.0003}], "mode": "straight", "speed_kmh": 30})
        wait_for(lambda: spy.last[0] is True and "trip" in spy.last[1][0])
        wait_for(lambda: not c.get("/api/status").json()["walk"]["active"])
        wait_for(lambda: spy.last[0] is False)


def test_an_activated_schedule_keeps_the_mac_awake_until_it_is_finished(tmp_path):
    from datetime import datetime
    base = datetime.fromisoformat("2026-10-31T19:00:00").timestamp()
    app, spy = make(tmp_path, FakeBackend(version="17.4"), schedule_clock=lambda: base)
    with TestClient(app) as c:
        pid = c.post("/api/schedules", json={"name": "P", "steps": [{"at": "2026-10-31T20:00", "type": "clear"}]}).json()["id"]
        time.sleep(0.2)
        assert spy.last[0] is False                                                  # not activated: the Mac may sleep
        c.post(f"/api/schedules/{pid}/arm", json={"armed": True})
        wait_for(lambda: spy.last[0] is True and "schedule" in spy.last[1][-1])
        c.post(f"/api/schedules/{pid}/arm", json={"armed": False})
        wait_for(lambda: spy.last[0] is False)


def test_the_switches_are_respected(tmp_path):
    app, spy = make(tmp_path, FakeBackend(version="17.4"))
    with TestClient(app) as c:
        c.post("/api/location", json={"lat": 1, "lon": 2})
        wait_for(lambda: spy.last[0] is True and spy.last[2] is False)               # the screen is allowed to sleep
        c.patch("/api/settings", json={"keep_screen_on": True})
        wait_for(lambda: spy.last[2] is True)
        c.patch("/api/settings", json={"keep_awake": False})
        wait_for(lambda: spy.last[0] is False)                                       # the user said never
        c.patch("/api/settings", json={"keep_awake": True})
        wait_for(lambda: spy.last[0] is True)


def test_awake_endpoint_reports_state_and_the_last_sleep(tmp_path):
    clocks = Clocks()
    watch = SleepWatch(wall=lambda: clocks.wall, mono=lambda: clocks.mono)
    backend = FakeBackend(version="17.4")
    app, spy = make(tmp_path, backend, sleep_watch=watch)
    with TestClient(app) as c:
        c.post("/api/location", json={"lat": 1, "lon": 2})
        st = wait_for(lambda: c.get("/api/awake").json()["active"] and c.get("/api/awake").json())
        assert st["supported"] and st["enabled"] and st["last_sleep"] is None and st["reasons"]
        manager = app.state.manager
        manager._last_ok = time.monotonic()                                          # looks "healthy" (recently talked to the phone)
        clocks.tick(asleep_s=1800)                                                   # the Mac slept for 30 minutes
        slept = wait_for(lambda: c.get("/api/awake").json()["last_sleep"])
        assert 1790 < slept["slept_s"] < 1810
        assert manager._last_ok == 0.0                                               # after waking, the connection is re-checked at once


def test_a_crash_in_the_awake_loop_is_contained(tmp_path):
    spy = SpyAwake()
    boom = {"n": 0}
    orig = spy.update

    def flaky(*a, **k):
        boom["n"] += 1
        if boom["n"] == 1:
            raise RuntimeError("boom")
        return orig(*a, **k)

    spy.update = flaky
    app, _ = make(tmp_path, FakeBackend(version="17.4"), keep_awake=spy)
    with TestClient(app) as c:
        time.sleep(0.4)
        assert boom["n"] > 3 and c.get("/api/status").status_code == 200
