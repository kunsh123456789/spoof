import time

import pytest
from fastapi.testclient import TestClient

from conftest import FakeBackend, FakeHouses, FakeRouter
from locspoof.server import create_app


@pytest.fixture
def env(tmp_path):
    backend, router, houses = FakeBackend(), FakeRouter(), FakeHouses()
    app = create_app(backend=backend, router=router, house_finder=houses, favorites_path=tmp_path / "f.json",
                     recents_path=tmp_path / "r.json", walk_tick=0.01)
    with TestClient(app) as c:
        yield c, backend, router, houses


def wait_idle(c, timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        st = c.get("/api/status").json()
        if not st["walk"]["active"]:
            return st
        time.sleep(0.05)
    pytest.fail("journey did not finish")


def sets(backend):
    return [x[1:] for s in backend.sessions for x in s.calls if x[0] == "set"]


def stop(lat, lon, dwell=0):
    return {"lat": lat, "lon": lon, "dwell_s": dwell}


def test_needs_start_location(env):
    c, *_ = env
    r = c.post("/api/journey", json={"stops": [stop(0, 0.001)], "mode": "straight"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "no_start"


def test_straight_line_is_gradual_and_does_not_use_router(env):
    c, backend, router, _ = env
    c.post("/api/location", json={"lat": 0, "lon": 0})
    r = c.post("/api/journey", json={"stops": [stop(0, 0.0002)], "mode": "straight", "speed_kmh": 400})
    assert r.status_code == 200 and r.json()["route"][0] == [0, 0]
    st = wait_idle(c)
    assert st["simulation"]["lon"] == 0.0002 and router.calls == []
    lons = [p[1] for p in sets(backend)]
    assert len(lons) > 5 and lons == sorted(lons)


@pytest.mark.parametrize("mode", ["walk", "drive"])
def test_road_modes_follow_router_geometry(env, mode):
    c, backend, router, _ = env
    c.post("/api/location", json={"lat": 0, "lon": 0})
    r = c.post("/api/journey", json={"stops": [stop(0.0002, 0.0002)], "mode": mode, "speed_kmh": 400}).json()
    assert router.calls[0][1] == mode
    assert [0, 0.0002] in r["route"]  # the corner of the fake road
    wait_idle(c)
    # every position lies on the L-shaped road (lat 0 first, then lon 0.0002), never the diagonal
    assert all(p[0] == 0 or p[1] == pytest.approx(0.0002) for p in sets(backend))


def test_multi_stop_visits_in_order_and_pauses(env):
    c, backend, _, _ = env
    c.post("/api/location", json={"lat": 0, "lon": 0})
    t0 = time.time()
    c.post("/api/journey", json={"stops": [stop(0, 0.0001, 0.3), stop(0, 0.0002, 0.3)], "mode": "straight", "speed_kmh": 400})
    time.sleep(0.15)
    w = c.get("/api/status").json()["walk"]
    assert w["state"] == "waiting" and w["stop"] == 1 and w["total"] == 2
    st = wait_idle(c)
    assert time.time() - t0 >= 0.6  # two pauses of 0.3 s
    assert st["simulation"]["lon"] == 0.0002
    lons = [p[1] for p in sets(backend)]
    assert 0.0001 in lons and lons.index(0.0001) < lons.index(0.0002)


def test_loop_keeps_going_until_stopped(env):
    c, backend, _, _ = env
    c.post("/api/location", json={"lat": 0, "lon": 0})
    c.post("/api/journey", json={"stops": [stop(0, 0.0001), stop(0, 0.0002)], "mode": "straight", "speed_kmh": 400, "loop": True})
    time.sleep(1.0)
    st = c.get("/api/status").json()
    assert st["walk"]["active"] and st["walk"]["loop"]
    lons = [p[1] for p in sets(backend)]
    assert lons.count(0.0002) >= 2  # went around more than once
    assert c.delete("/api/journey").json()["walk"]["active"] is False
    assert c.get("/api/status").json()["simulation"]["active"]  # stays where it stopped


def test_jump_to_first_teleports_then_visits_rest(env):
    c, backend, _, _ = env
    r = c.post("/api/journey", json={"stops": [stop(10, 10, 0.05), stop(10, 10.0002)], "mode": "straight",
                                     "speed_kmh": 400, "jump_to_first": True, "kind": "trick_or_treat", "speed_jitter": 0.2})
    assert r.status_code == 200 and r.json()["walk"]["kind"] == "trick_or_treat"
    st = wait_idle(c)
    assert sets(backend)[0] == (10, 10) and st["simulation"]["lon"] == 10.0002


def test_finishing_a_journey_adds_recent(env):
    c, *_ = env
    c.post("/api/location", json={"lat": 0, "lon": 0})
    c.post("/api/journey", json={"stops": [stop(0, 0.0002)], "mode": "straight", "speed_kmh": 400})
    wait_idle(c)
    assert c.get("/api/recent").json()["recent"][0]["name"] == "Route end"


def test_speed_patch_and_validation(env):
    c, *_ = env
    c.post("/api/location", json={"lat": 0, "lon": 0})
    assert c.patch("/api/journey", json={"speed_kmh": 10}).status_code == 409
    c.post("/api/journey", json={"stops": [stop(0, 1)], "mode": "straight", "speed_kmh": 5})
    assert c.patch("/api/journey", json={"speed_kmh": 30}).json()["walk"]["speed_kmh"] == 30
    c.delete("/api/journey")
    for bad in [{"speed_kmh": 0}, {"speed_kmh": 1000}, {"mode": "fly"}, {"stops": []}, {"speed_jitter": 0.9},
                {"stops": [stop(0, 1, -1)]}, {"stops": [stop(95, 1)]}]:
        body = {"stops": [stop(0, 1)], "mode": "straight", "speed_kmh": 5, **bad}
        assert c.post("/api/journey", json=body).status_code == 422, bad


def test_setting_location_cancels_journey(env):
    c, *_ = env
    c.post("/api/location", json={"lat": 0, "lon": 0})
    c.post("/api/journey", json={"stops": [stop(0, 1)], "mode": "straight", "speed_kmh": 5})
    c.post("/api/location", json={"lat": 5, "lon": 5})
    assert c.get("/api/status").json()["walk"]["active"] is False


def test_route_endpoint_only_while_moving(env):
    c, *_ = env
    c.post("/api/location", json={"lat": 0, "lon": 0})
    c.post("/api/journey", json={"stops": [stop(0, 1)], "mode": "walk", "speed_kmh": 5})
    assert len(c.get("/api/journey/route").json()["route"]) >= 3
    c.delete("/api/journey")
    assert c.get("/api/journey/route").json()["route"] == []


def test_trick_or_treat_plan_validation(env):
    c, _, _, houses = env
    r = c.post("/api/trick-or-treat/plan", json={"lat": 1, "lon": 2, "radius_m": 300, "houses": 4}).json()
    assert len(r["houses"]) == 4 and houses.calls == [(1, 2, 300, 4)]
    for bad in [{"radius_m": 10}, {"radius_m": 5000}, {"houses": 1}, {"houses": 500}, {"lat": 99}]:
        assert c.post("/api/trick-or-treat/plan", json={"lat": 1, "lon": 2, **bad}).status_code == 422


def test_recents_dedupe_order_label_and_clear(env):
    c, *_ = env
    c.post("/api/location", json={"lat": 1, "lon": 1, "label": "Cafe"})
    c.post("/api/location", json={"lat": 2, "lon": 2})
    c.post("/api/location", json={"lat": 1.00001, "lon": 1.00001})  # ~1.5 m from the first -> same place
    rec = c.get("/api/recent").json()["recent"]
    assert [r["name"] for r in rec] == ["Cafe", "2.00000, 2.00000"] and len(rec) == 2
    for i in range(20):
        c.post("/api/location", json={"lat": 10 + i, "lon": 10})
    assert len(c.get("/api/recent").json()["recent"]) == 15
    c.delete("/api/recent")
    assert c.get("/api/recent").json()["recent"] == []


def test_favorite_by_place_name_without_pin(env, tmp_path):
    from locspoof.geocode import Geocoder
    import httpx
    geo = Geocoder(client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(
        200, json=[{"display_name": "Eiffel Tower", "lat": "48.8582", "lon": "2.2945"}]))), min_interval=0)
    app = create_app(backend=FakeBackend(), geocoder=geo, router=FakeRouter(), house_finder=FakeHouses(),
                     favorites_path=tmp_path / "f.json", recents_path=tmp_path / "r.json")
    with TestClient(app) as c:
        f = c.post("/api/favorites", json={"name": "Paris", "query": "Eiffel Tower"}).json()
        assert (f["lat"], f["lon"]) == (48.8582, 2.2945)
        g = c.post("/api/favorites", json={"name": "Typed", "lat": 1, "lon": 2}).json()
        assert g["lat"] == 1
        assert c.post("/api/favorites", json={"name": "x"}).status_code == 422
