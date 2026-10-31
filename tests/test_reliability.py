"""Reliability: hangs, timeouts, responsiveness, graceful stopping (phone sessions faked)."""
import random
import time

import pytest
from fastapi.testclient import TestClient

from conftest import FakeBackend, FakeHouses, FakeRouter
from locspoof.server import create_app


@pytest.fixture
def env(tmp_path):
    backend = FakeBackend()
    app = create_app(backend=backend, router=FakeRouter(), house_finder=FakeHouses(), favorites_path=tmp_path / "f.json",
                     recents_path=tmp_path / "r.json", offline_root=tmp_path / "o", settings_path=tmp_path / "s.json",
                     routes_path=tmp_path / "rt.json", devices_path=tmp_path / "d.json", walk_tick=0.01, ambient_tick=0.02,
                     rng=random.Random(3))
    with TestClient(app) as c:
        c.backend, c.manager, c.dir = backend, app.state.manager, tmp_path
        m = c.manager
        m.call_timeout, m.probe_timeout, m.open_timeout, m.stop_grace, m.close_timeout = 0.4, 0.4, 0.6, 0.5, 0.2
        yield c


def stop(lat, lon, dwell=0, **kw):
    return {"lat": lat, "lon": lon, "dwell_s": dwell, **kw}


def timed(fn, *a, **k):
    t = time.monotonic()
    r = fn(*a, **k)
    return r, time.monotonic() - t


# ---- a phone that stops answering mid-call must not freeze the app -----------------------------------

def test_hanging_call_times_out_and_the_app_stays_usable(env):
    c = env
    assert c.post("/api/location", json={"lat": 1, "lon": 2}).status_code == 200
    c.backend.sessions[0].hang = True
    r, took = timed(c.post, "/api/location", json={"lat": 3, "lon": 4})
    assert took < 5 and r.status_code in (503, 200)                    # returned, did not hang forever
    if r.status_code == 503:
        assert r.json()["error"]["code"] == "connection_lost" and "answer" in r.json()["error"]["message"]
    # the lock was released: status and clear respond promptly
    _, t_status = timed(c.get, "/api/status")
    _, t_clear = timed(c.delete, "/api/location")
    assert t_status < 3 and t_clear < 5
    # and a fresh session works again once the phone wakes up
    assert c.post("/api/location", json={"lat": 5, "lon": 6}).status_code == 200
    assert c.backend.sessions[-1].calls[-1] == ("set", 5, 6)


def test_the_hung_session_is_replaced_not_reused(env):
    c = env
    c.post("/api/location", json={"lat": 1, "lon": 2})
    first = c.backend.sessions[0]
    first.hang = True
    r = c.post("/api/location", json={"lat": 3, "lon": 4})
    assert r.status_code == 200                                         # the retry on a new session succeeded
    assert len(c.backend.sessions) == 2 and c.backend.sessions[1].calls == [("set", 3, 4)]


def test_a_session_that_hangs_on_close_does_not_block_either(env):
    c = env
    c.post("/api/location", json={"lat": 1, "lon": 2})
    s = c.backend.sessions[0]
    s.hang, s.hang_close = True, True
    r, took = timed(c.post, "/api/location", json={"lat": 3, "lon": 4})
    assert took < 5 and r.status_code == 200


def test_probe_that_hangs_is_reported_not_awaited_forever(env):
    c = env
    c.backend.hang_probe = True
    r, took = timed(c.get, "/api/status")
    assert took < 3
    s = r.json()
    assert s["state"] == "connection_lost" and "answer" in s["error"]["message"]


def test_opening_a_session_that_hangs_gives_up_and_recovers(env):
    c = env
    c.backend.hang_open = True
    r, took = timed(c.post, "/api/location", json={"lat": 1, "lon": 2})
    assert took < 5 and r.status_code == 503 and r.json()["error"]["code"] == "connection_lost"
    c.backend.hang_open = False
    assert c.post("/api/location", json={"lat": 1, "lon": 2}).status_code == 200


# ---- the UI must keep getting answers while something slow is happening -------------------------------------

def test_status_answers_immediately_while_a_slow_session_opens(env):
    c = env
    c.manager.open_timeout = 5
    c.backend.open_delay = 1.5                      # e.g. mounting the disk image
    import threading
    t = threading.Thread(target=lambda: c.post("/api/location", json={"lat": 1, "lon": 2}))
    t.start()
    time.sleep(0.3)
    answers = []
    for _ in range(3):
        r, took = timed(c.get, "/api/status")
        answers.append((r.status_code, r.json()["state"], took))
    t.join()
    assert all(code == 200 and took < 0.5 for code, _, took in answers)
    assert {st for _, st, _ in answers} <= {"connecting", "ready"}


def test_status_does_not_probe_the_phone_while_a_trip_is_healthy(env):
    c = env
    c.post("/api/location", json={"lat": 0, "lon": 0})
    c.post("/api/journey", json={"stops": [stop(0, 0.01)], "mode": "straight", "speed_kmh": 5})   # a long, steady trip
    c.get("/api/status")                            # the UI always asks once first
    time.sleep(0.2)
    before = c.backend.probes
    for _ in range(3):
        time.sleep(2.1)                             # past the status cache each time
        assert c.get("/api/status").json()["state"] == "ready"
    assert c.backend.probes == before               # the working connection itself proves the phone is fine
    c.delete("/api/journey")


def test_status_still_detects_an_unplug_when_idle(env):
    c = env
    c.post("/api/location", json={"lat": 0, "lon": 0})
    c.backend.unplug()
    time.sleep(6.5)                                 # longer than the healthy window
    assert c.get("/api/status").json()["state"] == "no_device"


# ---- stopping a trip is graceful ------------------------------------------------------------------------------

def test_stopping_never_cuts_a_device_call_in_half(env):
    c = env
    c.post("/api/location", json={"lat": 0, "lon": 0})
    s = c.backend.sessions[0]
    s.delay = 0.15                                  # every update takes a while
    c.post("/api/journey", json={"stops": [stop(0, 0.01)], "mode": "straight", "speed_kmh": 100})
    time.sleep(0.5)
    r, took = timed(c.delete, "/api/journey")
    assert r.status_code == 200 and took < 3
    assert s.started == s.finished                  # no update was abandoned half way through
    assert c.get("/api/status").json()["walk"]["active"] is False


def test_stopping_during_a_long_pause_returns_quickly(env):
    c = env
    c.manager.stop_grace = 3
    c.post("/api/location", json={"lat": 0, "lon": 0})
    c.post("/api/journey", json={"stops": [stop(0, 0.0002, 600)], "mode": "straight", "speed_kmh": 400})
    for _ in range(100):
        if c.get("/api/status").json()["walk"].get("state") == "waiting":
            break
        time.sleep(0.05)
    _, took = timed(c.delete, "/api/journey")
    assert took < 2


def test_stopping_falls_back_to_a_hard_cancel_if_the_phone_hangs(env):
    c = env
    c.manager.call_timeout = 30                     # so only the stop logic can end the hang
    c.post("/api/location", json={"lat": 0, "lon": 0})
    c.post("/api/journey", json={"stops": [stop(0, 0.01)], "mode": "straight", "speed_kmh": 100})
    time.sleep(0.2)
    c.backend.sessions[0].hang = True
    time.sleep(0.2)
    _, took = timed(c.delete, "/api/journey")
    assert took < 4 and c.get("/api/status").json()["walk"]["active"] is False


def test_starting_a_new_trip_replaces_the_old_one_without_overlap(env):
    c = env
    c.post("/api/location", json={"lat": 0, "lon": 0})
    c.post("/api/journey", json={"stops": [stop(0, 0.01)], "mode": "straight", "speed_kmh": 100})
    time.sleep(0.2)
    c.post("/api/journey", json={"stops": [stop(0.01, 0)], "mode": "straight", "speed_kmh": 100})
    time.sleep(0.3)
    s = c.backend.sessions[0]
    assert s.started == s.finished
    assert c.get("/api/status").json()["walk"]["active"] is True
    c.delete("/api/journey")


# ---- background tasks survive errors ------------------------------------------------------------------------------

def test_ambient_noise_loop_survives_an_error(env, monkeypatch):
    c = env
    c.post("/api/location", json={"lat": 40, "lon": -75})
    calls = {"n": 0}
    real = c.manager._noise.apply

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        return real(*a, **k)

    monkeypatch.setattr(c.manager._noise, "apply", flaky)
    c.patch("/api/settings", json={"noise": True})
    time.sleep(0.5)
    assert calls["n"] > 3 and len(c.backend.sessions[0].calls) > 2      # kept going after the first failure
