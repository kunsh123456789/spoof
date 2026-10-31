"""Settings, realism, return trip, speed schedule, GPX playback, saved routes, cooldown, auto-reconnect."""
import random
import time

import pytest
from fastapi.testclient import TestClient

from conftest import FakeBackend, FakeHouses, FakeRouter
from locspoof.movement import haversine_m
from locspoof.server import create_app


@pytest.fixture
def env(tmp_path):
    backend, router = FakeBackend(), FakeRouter()
    app = create_app(backend=backend, router=router, house_finder=FakeHouses(), favorites_path=tmp_path / "f.json",
                     recents_path=tmp_path / "r.json", offline_root=tmp_path / "o", settings_path=tmp_path / "s.json",
                     routes_path=tmp_path / "routes.json", walk_tick=0.01, ambient_tick=0.02, rng=random.Random(7))
    with TestClient(app) as c:
        c.backend, c.router, c.manager, c.dir = backend, router, app.state.manager, tmp_path
        yield c


def stop(lat, lon, dwell=0, **kw):
    return {"lat": lat, "lon": lon, "dwell_s": dwell, **kw}


def wait_idle(c, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        st = c.get("/api/status").json()
        if not st["walk"]["active"]:
            return st
        time.sleep(0.05)
    pytest.fail("journey did not finish")


def sets(c):
    return [x[1:] for s in c.backend.sessions for x in s.calls if x[0] == "set"]


# ---- settings ---------------------------------------------------------------------------------

def test_settings_defaults_patch_validation_and_persistence(env):
    c = env
    s = c.get("/api/settings").json()
    assert s["noise"] is False and s["auto_reconnect"] is True and "tot_kids" in s
    assert c.patch("/api/settings", json={"noise": True}).json()["noise"] is True
    assert c.patch("/api/settings", json={"bogus": True}).json()["error"]["code"] == "bad_setting"
    assert c.patch("/api/settings", json={"noise": "yes"}).status_code == 422
    assert c.get("/api/settings").json()["noise"] is True
    app2 = create_app(backend=FakeBackend(), settings_path=c.dir / "s.json", favorites_path=c.dir / "f2.json", recents_path=c.dir / "r2.json",
                      routes_path=c.dir / "x.json", offline_root=c.dir / "o2")
    with TestClient(app2) as c2:
        assert c2.get("/api/settings").json()["noise"] is True       # survived a restart


# ---- realism ----------------------------------------------------------------------------------

def test_gps_noise_while_standing_still_keeps_true_position(env):
    c = env
    c.post("/api/location", json={"lat": 40.0, "lon": -75.0})
    time.sleep(0.2)
    assert sets(c) == [(40.0, -75.0)]                                   # noise off: nothing extra is sent
    c.patch("/api/settings", json={"noise": True})
    time.sleep(0.6)
    sent = sets(c)[1:]
    assert len(sent) >= 5 and len(set(sent)) > 3
    assert all(0 < haversine_m(40.0, -75.0, *p) < 10 for p in sent)
    assert c.get("/api/status").json()["simulation"]["lat"] == 40.0     # reported position stays the true one
    c.patch("/api/settings", json={"noise": False})
    time.sleep(0.1)
    n = len(sets(c))
    time.sleep(0.3)
    assert len(sets(c)) == n                                            # stops when switched off


def test_noise_is_added_to_journeys_too(env):
    c = env
    c.patch("/api/settings", json={"noise": True})
    c.post("/api/location", json={"lat": 0, "lon": 0})
    c.post("/api/journey", json={"stops": [stop(0, 0.0004)], "mode": "straight", "speed_kmh": 400})
    st = wait_idle(c)
    assert st["simulation"]["lon"] == 0.0004                            # true end position exact
    assert any(p[0] != 0 for p in sets(c)[1:])                          # but sent points wobble off the line


def test_natural_walking_varies_the_pace(env):
    c = env
    c.post("/api/location", json={"lat": 0, "lon": 0})
    c.post("/api/journey", json={"stops": [stop(0, 0.0006)], "mode": "straight", "speed_kmh": 300})
    wait_idle(c)
    steps = lambda pts: [round(haversine_m(*a, *b), 4) for a, b in zip(pts, pts[1:])][:-1]
    plain = steps(sets(c))
    assert len(set(plain)) <= 2                                         # constant pace
    c.backend.sessions[0].calls.clear()
    c.patch("/api/settings", json={"natural_walk": True})
    c.post("/api/location", json={"lat": 0, "lon": 0})
    c.post("/api/journey", json={"stops": [stop(0, 0.0006)], "mode": "straight", "speed_kmh": 300})
    st = wait_idle(c)
    varied = steps(sets(c)[1:])
    assert len(set(varied)) > 10 and st["simulation"]["lon"] == 0.0006
    assert 0.5 * plain[0] < min(varied) and max(varied) < 1.5 * plain[0]


def test_traffic_stops_pause_driving_legs_only(env, monkeypatch):
    import locspoof.device as dev
    monkeypatch.setattr(dev, "traffic_stop_seconds", lambda angle, rng: 0.25)
    c = env
    c.patch("/api/settings", json={"traffic_stops": True})
    c.post("/api/location", json={"lat": 0, "lon": 0})
    # FakeRouter makes an L-shaped road with a 90-degree corner
    c.post("/api/journey", json={"stops": [stop(0.0004, 0.0004)], "mode": "drive", "speed_kmh": 300})
    seen = set()
    end = time.time() + 15
    while time.time() < end:
        w = c.get("/api/status").json()["walk"]
        if w.get("state") == "waiting":
            seen.add(w.get("reason"))
        if not w["active"]:
            break
        time.sleep(0.03)
    assert "traffic" in seen
    # walking the same route never stops
    c.post("/api/location", json={"lat": 0, "lon": 0})
    t0 = time.time()
    c.post("/api/journey", json={"stops": [stop(0.0004, 0.0004)], "mode": "walk", "speed_kmh": 300})
    for _ in range(100):
        w = c.get("/api/status").json()["walk"]
        assert w.get("reason") != "traffic"
        if not w["active"]:
            break
        time.sleep(0.03)


def test_cooldown_after_big_jump_only(env):
    c = env
    c.post("/api/location", json={"lat": 0, "lon": 0})
    assert "cooldown" not in c.get("/api/status").json()["simulation"]    # first location: nothing to compare with
    c.post("/api/location", json={"lat": 0, "lon": 0.005})                # ~550 m
    assert "cooldown" not in c.get("/api/status").json()["simulation"]
    c.post("/api/location", json={"lat": 0, "lon": 0.5})                  # ~55 km
    cd = c.get("/api/status").json()["simulation"]["cooldown"]
    assert 54 < cd["km"] < 57 and 15 * 60 < cd["total_s"] < 25 * 60 and cd["remaining_s"] <= cd["total_s"]
    c.delete("/api/location")
    assert "cooldown" not in c.get("/api/status").json()["simulation"]


# ---- return trip ------------------------------------------------------------------------------

def test_return_to_start_flag_walks_back_after_the_last_stop(env):
    c = env
    c.post("/api/location", json={"lat": 0, "lon": 0})
    r = c.post("/api/journey", json={"stops": [stop(0, 0.0002)], "mode": "straight", "speed_kmh": 400, "return_to_start": True}).json()
    assert r["route"][0] == [0, 0] and [0, 0.0002] in r["route"] and r["route"][-1] == [0, 0]
    seen_returning = False
    end = time.time() + 15
    while time.time() < end:
        w = c.get("/api/status").json()["walk"]
        seen_returning |= bool(w.get("returning"))
        if not w["active"]:
            break
        time.sleep(0.02)
    st = c.get("/api/status").json()
    assert seen_returning and (st["simulation"]["lat"], st["simulation"]["lon"]) == (0, 0)
    lons = [p[1] for p in sets(c)]
    assert max(lons) == 0.0002 and lons.index(0.0002) < len(lons) - 2     # went there, then came back


def test_return_endpoint_goes_back_to_the_original_start(env):
    c = env
    assert c.post("/api/journey/return").json()["error"]["code"] == "no_origin"
    c.post("/api/location", json={"lat": 0, "lon": 0})
    c.post("/api/journey", json={"stops": [stop(0, 0.0002)], "mode": "straight", "speed_kmh": 400})
    wait_idle(c)
    sim = c.get("/api/status").json()["simulation"]
    assert sim["origin"] == [0, 0] and sim["lon"] == 0.0002
    assert c.post("/api/journey/return").status_code == 200
    st = wait_idle(c)
    assert st["simulation"]["lon"] == 0
    assert c.post("/api/journey/return").json()["error"]["code"] == "already_there"
    c.post("/api/journey", json={"stops": [stop(0, 0.0001)], "mode": "straight", "speed_kmh": 400})
    wait_idle(c)
    c.post("/api/journey/return"); wait_idle(c)
    assert c.get("/api/status").json()["simulation"]["lon"] == 0          # always back to the first start


# ---- speed schedule ---------------------------------------------------------------------------

def test_each_leg_can_have_its_own_mode_and_speed(env):
    c = env
    c.post("/api/location", json={"lat": 0, "lon": 0})
    body = {"mode": "straight", "speed_kmh": 5, "stops": [
        stop(0, 0.0002, mode="walk", speed_kmh=400),        # fast, via road
        stop(0, 0.0004, mode="walk", speed_kmh=100),        # slower, same mode: grouped into one routing call
        stop(0, 0.0006, mode="straight", speed_kmh=400),    # straight again
        stop(0, 0.0008, mode="drive", speed_kmh=400),
    ]}
    assert c.post("/api/journey", json=body).status_code == 200
    wait_idle(c)
    assert [m for _, m in c.router.calls] == ["walk", "drive"]
    assert len(c.router.calls[0][0]) == 3                          # origin + 2 stops in one request
    lons = [p[1] for p in sets(c)]
    leg1 = sum(1 for x in lons if 0 < x <= 0.0002)
    leg2 = sum(1 for x in lons if 0.0002 < x <= 0.0004)
    assert leg2 > 2.5 * leg1                                       # 100 km/h leg takes ~4x the ticks of the 400 km/h leg


def test_live_speed_slider_does_not_override_a_legs_own_speed(env):
    c = env
    c.post("/api/location", json={"lat": 0, "lon": 0})
    c.post("/api/journey", json={"mode": "straight", "speed_kmh": 5, "stops": [stop(0, 0.0003, speed_kmh=40), stop(0, 0.0006)]})
    time.sleep(0.15)
    c.patch("/api/journey", json={"speed_kmh": 400})
    st = c.get("/api/status").json()["walk"]
    assert st["speed_kmh"] == 400 and 35 < st["cur_speed_kmh"] < 45
    c.delete("/api/journey")


def test_bad_leg_settings_rejected(env):
    c = env
    c.post("/api/location", json={"lat": 0, "lon": 0})
    for bad in [stop(0, 1, mode="fly"), stop(0, 1, speed_kmh=9000)]:
        assert c.post("/api/journey", json={"stops": [bad], "mode": "straight"}).status_code == 422


# ---- GPX ---------------------------------------------------------------------------------------

GPX = (b'<gpx xmlns="http://www.topografix.com/GPX/1/1"><trk><name>Loop</name><trkseg>'
       b'<trkpt lat="10.0" lon="10.0"><time>2024-01-01T00:00:00Z</time></trkpt>'
       b'<trkpt lat="10.0" lon="10.0003"><time>2024-01-01T00:00:10Z</time></trkpt>'
       b'<trkpt lat="10.0003" lon="10.0003"><time>2024-01-01T00:00:20Z</time></trkpt></trkseg></trk></gpx>')


def test_gpx_endpoint_and_exact_playback(env):
    c = env
    info = c.post("/api/gpx", content=GPX, headers={"content-type": "application/gpx+xml"}).json()
    assert info["name"] == "Loop" and len(info["points"]) == 3 and info["avg_kmh"] > 5
    assert c.post("/api/gpx", content=b"junk").json()["error"]["code"] == "bad_gpx"
    r = c.post("/api/journey", json={"stops": [], "path": info["points"], "mode": "straight", "speed_kmh": 400, "jump_to_first": True})
    assert r.status_code == 200 and r.json()["route"][0] == [10.0, 10.0]
    st = wait_idle(c)
    assert (st["simulation"]["lat"], st["simulation"]["lon"]) == (10.0003, 10.0003)
    pts = sets(c)
    assert pts[0] == (10.0, 10.0)
    assert all(abs(p[0] - 10.0) < 1e-6 or abs(p[1] - 10.0003) < 1e-6 for p in pts)   # never cuts the corner


def test_gpx_without_jump_walks_to_the_start_first(env):
    c = env
    c.post("/api/location", json={"lat": 10.0, "lon": 9.9995})
    c.post("/api/journey", json={"stops": [], "mode": "straight", "speed_kmh": 400, "path": [[10.0, 10.0], [10.0, 10.0003]]})
    wait_idle(c)
    lons = [p[1] for p in sets(c)]
    assert lons[0] == 9.9995 and lons[1] > 9.9995 and lons[-1] == 10.0003 and lons == sorted(lons)


def test_bad_path_rejected(env):
    c = env
    c.post("/api/location", json={"lat": 0, "lon": 0})
    assert c.post("/api/journey", json={"stops": [], "path": [[1, 1]], "mode": "straight"}).status_code == 422
    assert c.post("/api/journey", json={"stops": [], "path": [[1, 1], [95, 1]], "mode": "straight"}).status_code == 422
    assert c.post("/api/journey", json={"stops": [], "path": [[1, 1], [1]], "mode": "straight"}).status_code == 422


# ---- saved routes -----------------------------------------------------------------------------

def test_save_list_start_and_delete_routes(env):
    c = env
    body = {"name": "  Dog   walk ", "stops": [stop(0, 0.0002, 0.05, mode="straight", speed_kmh=400, label="Park"), stop(0, 0.0004)],
            "mode": "straight", "speed_kmh": 400, "loop": False, "return_to_start": True}
    saved = c.post("/api/routes", json=body).json()
    assert saved["name"] == "Dog walk" and saved["stops"][0]["label"] == "Park" and saved["stops"][0]["speed_kmh"] == 400
    assert c.get("/api/routes").json()["routes"][0]["id"] == saved["id"]
    # starting without a position and without jump fails clearly; with jump it teleports to the first stop
    assert c.post(f"/api/routes/{saved['id']}/start", json={}).json()["error"]["code"] == "no_start"
    assert c.post(f"/api/routes/{saved['id']}/start", json={"jump_to_first": True}).status_code == 200
    st = wait_idle(c)
    assert (st["simulation"]["lat"], st["simulation"]["lon"]) == (0, 0.0002)       # returned to its start (the first stop)
    assert c.delete(f"/api/routes/{saved['id']}").status_code == 200
    assert c.delete(f"/api/routes/{saved['id']}").status_code == 404
    assert c.post("/api/routes/nope/start", json={}).status_code == 404


def test_saved_gpx_track_plays_back(env):
    c = env
    track = [[10.0, 10.0], [10.0, 10.0003], [10.0003, 10.0003]]
    saved = c.post("/api/routes", json={"name": "Run", "kind": "track", "path": track, "mode": "straight", "speed_kmh": 400,
                                        "stops": [stop(10.0003, 10.0003)]}).json()
    assert len(saved["path"]) == 3
    c.post(f"/api/routes/{saved['id']}/start", json={"jump_to_first": True})
    st = wait_idle(c)
    assert (st["simulation"]["lat"], st["simulation"]["lon"]) == (10.0003, 10.0003)


@pytest.mark.parametrize("patch", [{"name": " "}, {"kind": "weird"}, {"mode": "fly"}, {"speed_kmh": 0}, {"stops": []},
                                   {"stops": [stop(95, 0)]}, {"stops": [stop(0, 0, mode="fly")]}, {"kind": "track"}])
def test_route_validation(env, patch):
    c = env
    body = {"name": "x", "stops": [stop(0, 1)], "mode": "walk", "speed_kmh": 5, **patch}
    assert c.post("/api/routes", json=body).status_code in (422, 400)
    assert c.get("/api/routes").json()["routes"] == []


# ---- auto-reconnect ---------------------------------------------------------------------------

def test_journey_waits_out_a_cable_dropout_and_finishes(env):
    c = env
    c.manager.reconnect_poll = 0.02
    c.post("/api/location", json={"lat": 0, "lon": 0})
    c.post("/api/journey", json={"stops": [stop(0, 0.0006)], "mode": "straight", "speed_kmh": 200})
    time.sleep(0.2)
    c.backend.unplug()
    saw = False
    end = time.time() + 5
    while time.time() < end and not saw:
        saw = c.get("/api/status").json()["walk"].get("state") == "reconnecting"
        time.sleep(0.02)
    assert saw
    paused_at = c.get("/api/status").json()["simulation"]["lon"]
    time.sleep(0.2)
    assert c.get("/api/status").json()["simulation"]["lon"] == paused_at          # holds position while unplugged
    c.backend.replug()
    st = wait_idle(c)
    assert st["simulation"]["lon"] == 0.0006 and "error" not in st["walk"]


def test_journey_ends_cleanly_when_auto_reconnect_is_off(env):
    c = env
    c.patch("/api/settings", json={"auto_reconnect": False})
    c.post("/api/location", json={"lat": 0, "lon": 0})
    c.post("/api/journey", json={"stops": [stop(0, 0.0008)], "mode": "straight", "speed_kmh": 100})
    time.sleep(0.1)
    c.backend.unplug()
    end = time.time() + 5
    st = {}
    while time.time() < end:
        st = c.get("/api/status").json()
        if not st["walk"]["active"]:
            break
        time.sleep(0.03)
    assert not st["walk"]["active"]
    # either the status check noticed first (simulation reset) or the journey hit the error itself
    assert st["simulation"] == {"active": False} or st["walk"].get("error", {}).get("code") == "no_device"
    assert "reconnecting" not in st["simulation"]


def test_reconnect_gives_up_after_the_timeout(env):
    c = env
    c.manager.reconnect_poll, c.manager.reconnect_timeout = 0.02, 0.3
    c.post("/api/location", json={"lat": 0, "lon": 0})
    c.post("/api/journey", json={"stops": [stop(0, 0.0008)], "mode": "straight", "speed_kmh": 100})
    time.sleep(0.1)
    c.backend.unplug()
    end = time.time() + 5
    err = None
    while time.time() < end and not err:
        err = c.get("/api/status").json()["walk"].get("error")
        time.sleep(0.05)
    assert err and err["code"] == "no_device"
