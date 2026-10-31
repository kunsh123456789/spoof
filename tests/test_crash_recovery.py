"""The app is killed or crashes: state must stay truthful, files intact, the server must come back."""
import json
import os
import random
import threading
import time

import pytest
from fastapi.testclient import TestClient

import locspoof.fileio as fileio
from conftest import FakeBackend, FakeHouses, FakeRouter
from locspoof.memory import DeviceMemory
from locspoof.runner import ServerSupervisor, ServerThread
from locspoof.server import create_app


def make_app(tmp, backend, **kw):
    return create_app(backend=backend, router=FakeRouter(), house_finder=FakeHouses(), favorites_path=tmp / "f.json",
                      recents_path=tmp / "r.json", offline_root=tmp / "o", settings_path=tmp / "s.json", routes_path=tmp / "rt.json",
                      devices_path=tmp / "d.json", walk_tick=0.01, ambient_tick=0.02, rng=random.Random(1), **kw)


def stop(lat, lon, dwell=0):
    return {"lat": lat, "lon": lon, "dwell_s": dwell}


def wait_idle(c, timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        st = c.get("/api/status").json()
        if not st["walk"]["active"]:
            return st
        time.sleep(0.05)
    pytest.fail("trip did not finish")


# ---- a phone that keeps its fake location (iOS 16 and earlier) -------------------------------------------------

def test_old_ios_fake_location_survives_an_app_restart_and_can_be_restored(tmp_path):
    phone = FakeBackend(version="15.7")
    with TestClient(make_app(tmp_path, phone)) as c:
        c.post("/api/location", json={"lat": 12.5, "lon": 34.5})
    # "kill -9": the app never got to clear anything. A brand new app starts with the same data folder.
    with TestClient(make_app(tmp_path, phone)) as c2:
        s = c2.get("/api/status").json()
        assert s["simulation"]["active"] and (s["simulation"]["lat"], s["simulation"]["lon"]) == (12.5, 34.5)
        w = [x for x in s["warnings"] if x["code"] == "stale_simulation"]
        assert w and "still" in w[0]["message"] and "Restore" in w[0]["hint"]
        c2.delete("/api/location")                                          # the user presses Restore real GPS
        assert phone.sessions[-1].calls[-1] == ("clear",)
        after = c2.get("/api/status").json()
        assert after["simulation"] == {"active": False} and not [x for x in after["warnings"] if x["code"] == "stale_simulation"]
    with TestClient(make_app(tmp_path, phone)) as c3:                       # and it stays cleared
        assert c3.get("/api/status").json()["simulation"] == {"active": False}


def test_modern_ios_needs_no_recovery_because_the_fake_location_ends_with_the_connection(tmp_path):
    phone = FakeBackend(version="17.4")
    with TestClient(make_app(tmp_path, phone)) as c:
        c.post("/api/location", json={"lat": 12.5, "lon": 34.5})
    memory_file = tmp_path / "d.json"
    assert not memory_file.exists() or "sim" not in memory_file.read_text()      # nothing was written down
    with TestClient(make_app(tmp_path, phone)) as c2:
        s = c2.get("/api/status").json()
        assert s["simulation"] == {"active": False} and not [x for x in s["warnings"] if x["code"] == "stale_simulation"]


def test_the_remembered_location_follows_a_trip_and_its_end(tmp_path):
    phone = FakeBackend(version="15.7")
    with TestClient(make_app(tmp_path, phone)) as c:
        c.post("/api/location", json={"lat": 0, "lon": 0})
        c.post("/api/journey", json={"stops": [stop(0, 0.0004)], "mode": "straight", "speed_kmh": 100})
        wait_idle(c)
        mem = json.loads((tmp_path / "d.json").read_text())
        (rec,) = [v["sim"] for v in mem.values() if "sim" in v]
        assert (rec["lat"], rec["lon"]) == (0, 0.0004)                       # where the trip ended
        c.post("/api/journey", json={"stops": [stop(0.01, 0.0004)], "mode": "straight", "speed_kmh": 100})
        time.sleep(0.4)
        c.delete("/api/journey")                                             # stopped part way
        here = c.get("/api/status").json()["simulation"]
        rec = [v["sim"] for v in json.loads((tmp_path / "d.json").read_text()).values() if "sim" in v][0]
        assert (rec["lat"], rec["lon"]) == (here["lat"], here["lon"]) and here["lat"] > 0


def test_memory_writes_during_a_long_trip_are_throttled(tmp_path, monkeypatch):
    saves = {"n": 0}
    real = DeviceMemory._save

    def counting(self):
        saves["n"] += 1
        return real(self)

    monkeypatch.setattr(DeviceMemory, "_save", counting)
    phone = FakeBackend(version="15.7")
    with TestClient(make_app(tmp_path, phone)) as c:
        c.post("/api/location", json={"lat": 0, "lon": 0})
        c.post("/api/journey", json={"stops": [stop(0, 0.01)], "mode": "straight", "speed_kmh": 50})
        time.sleep(1.0)                                                      # ~100 ticks
        c.delete("/api/journey")
    assert saves["n"] < 12                                                   # not one disk write per step


def test_user_actions_replace_the_stale_notice(tmp_path):
    phone = FakeBackend(version="15.7")
    with TestClient(make_app(tmp_path, phone)) as c:
        c.post("/api/location", json={"lat": 1, "lon": 2})
    with TestClient(make_app(tmp_path, phone)) as c2:
        assert any(x["code"] == "stale_simulation" for x in c2.get("/api/status").json()["warnings"])
        c2.post("/api/location", json={"lat": 5, "lon": 6})                  # the user sets something new
        time.sleep(2.1)
        assert not any(x["code"] == "stale_simulation" for x in c2.get("/api/status").json()["warnings"])


def test_device_memory_keeps_the_ios_version_and_the_simulation_together(tmp_path):
    m = DeviceMemory(tmp_path / "d.json")
    m.remember("00008110-AAAA", "Alice", "15.7")
    m.set_sim("00008110-AAAA", 1.5, 2.5)
    m.remember("00008110-aaaa", "Alice", "15.8")                             # an iOS update must not forget the simulation
    again = DeviceMemory(tmp_path / "d.json")
    assert again.get_sim("00008110AAAA") == (1.5, 2.5) and again.remember("00008110-AAAA", "Alice", "15.8") == "15.8"
    again.clear_sim("00008110-AAAA")
    assert DeviceMemory(tmp_path / "d.json").get_sim("00008110-AAAA") is None


# ---- files are never left half written ------------------------------------------------------------------------------

def test_atomic_write_survives_a_crash_between_write_and_rename(tmp_path, monkeypatch):
    target = tmp_path / "data.json"
    fileio.atomic_write(target, b'{"v": 1}')
    monkeypatch.setattr(fileio.os, "replace", lambda *a: (_ for _ in ()).throw(KeyboardInterrupt("killed")))
    with pytest.raises(KeyboardInterrupt):
        fileio.atomic_write(target, b'{"v": 2, "huge": "' + b"x" * 5000 + b'"}')
    monkeypatch.undo()
    assert json.loads(target.read_text()) == {"v": 1}                        # the old version is intact
    assert not list(tmp_path.glob("*.tmp"))


def test_every_data_file_is_flushed_to_disk(tmp_path, monkeypatch):
    synced = []
    monkeypatch.setattr(fileio.os, "fsync", lambda fd: synced.append(fd))
    phone = FakeBackend(version="15.7")
    with TestClient(make_app(tmp_path, phone)) as c:
        c.post("/api/favorites", json={"name": "Home", "lat": 1, "lon": 2})
        c.patch("/api/settings", json={"noise": True})
        c.post("/api/routes", json={"name": "r", "stops": [stop(0, 1)], "mode": "walk", "speed_kmh": 5})
        c.post("/api/location", json={"lat": 3, "lon": 4})
    assert len(synced) >= 5                                                  # favorites, recents, settings, routes, devices


@pytest.mark.parametrize("name,garbage", [("f.json", "{not json"), ("r.json", "[1, 2, 3]"), ("s.json", "\x00\x00"),
                                          ("rt.json", '{"a": 1}'), ("d.json", "nope")])
def test_corrupt_data_files_never_stop_the_app_from_starting(tmp_path, name, garbage):
    (tmp_path / name).write_text(garbage)
    with TestClient(make_app(tmp_path, FakeBackend())) as c:
        assert c.get("/api/status").status_code == 200
        assert c.get("/api/favorites").status_code == 200 and c.get("/api/routes").status_code == 200
        assert c.get("/api/recent").status_code == 200 and c.get("/api/settings").status_code == 200
        c.post("/api/favorites", json={"name": "ok", "lat": 1, "lon": 2})
        assert c.post("/api/location", json={"lat": 1, "lon": 2}).status_code == 200


def test_a_huge_request_is_refused_not_swallowed(tmp_path):
    with TestClient(make_app(tmp_path, FakeBackend())) as c:
        r = c.post("/api/gpx", content=b"x" * (26 * 1024 * 1024))
        assert r.status_code == 413 and r.json()["error"]["code"] == "too_large"
        assert c.get("/api/status").status_code == 200                       # and the app is fine afterwards


# ---- the server itself comes back if its thread dies --------------------------------------------------------------

def free_port():
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def get(url):
    import urllib.request
    with urllib.request.urlopen(url, timeout=3) as r:
        return r.status


def test_supervisor_restarts_a_server_that_died_unexpectedly(tmp_path):
    builds = []

    def build():
        builds.append(1)
        return make_app(tmp_path, FakeBackend())

    port = free_port()
    sup = ServerSupervisor(build, "127.0.0.1", port, check_every=0.1)
    sup.start()
    try:
        assert get(sup.url + "/api/settings") == 200
        sup.current.server.should_exit = True                                # the server thread dies by itself
        sup.current.thread.join(5)
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                if get(sup.url + "/api/settings") == 200:
                    break
            except Exception:
                time.sleep(0.1)
        assert get(sup.url + "/api/settings") == 200 and len(builds) == 2 and sup.restarts == 1
    finally:
        sup.stop()
    with pytest.raises(Exception):
        get(sup.url + "/api/settings")


def test_supervisor_gives_up_after_repeated_crashes_and_stop_never_restarts(tmp_path):
    port = free_port()
    sup = ServerSupervisor(lambda: make_app(tmp_path, FakeBackend()), "127.0.0.1", port, check_every=0.05, max_restarts=2)
    sup.start()
    for _ in range(3):
        deadline = time.time() + 8
        while time.time() < deadline and not sup.current.thread.is_alive():
            time.sleep(0.05)
        sup.current.server.should_exit = True
        sup.current.thread.join(5)
        time.sleep(0.6)
    assert sup.restarts == 2 and sup.gave_up
    sup.stop()
    restarts = sup.restarts
    time.sleep(0.3)
    assert sup.restarts == restarts                                           # a deliberate stop is never "repaired"
