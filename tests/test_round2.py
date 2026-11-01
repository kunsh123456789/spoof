"""Panic location, adjustable waits (stop signs / junctions / trick-or-treat houses) and the route preview."""
import random
import time

import pytest
from fastapi.testclient import TestClient

from conftest import FakeBackend, FakeHouses, FakeRouter
from locspoof.realism import traffic_stop
from locspoof.server import create_app
from locspoof.settings import NUMBERS, SettingsStore


@pytest.fixture
def env(tmp_path):
    houses = FakeHouses()
    app = create_app(backend=FakeBackend(), router=FakeRouter(), house_finder=houses, favorites_path=tmp_path / "f.json",
                     recents_path=tmp_path / "r.json", offline_root=tmp_path / "o", settings_path=tmp_path / "s.json",
                     routes_path=tmp_path / "rt.json", devices_path=tmp_path / "d.json", schedules_path=tmp_path / "sch.json",
                     activity_path=tmp_path / "act.json", panic_path=tmp_path / "p.json", walk_tick=0.02, ambient_tick=0.5, rng=random.Random(1))
    with TestClient(app) as c:
        c.app_, c.dir, c.houses = app, tmp_path, houses
        yield c


# ---- numeric settings ----
def test_numeric_settings_have_defaults_and_validate(env):
    s = env.get("/api/settings").json()
    assert s["stop_sign_s"] == 3.0 and s["junction_min_s"] < s["junction_max_s"] and s["tot_dwell_min_s"] < s["tot_dwell_max_s"]
    assert env.patch("/api/settings", json={"stop_sign_s": 5, "junction_min_s": 20, "junction_max_s": 60}).json()["stop_sign_s"] == 5.0
    for bad in ({"stop_sign_s": 0}, {"stop_sign_s": "x"}, {"stop_sign_s": True}, {"junction_max_s": 9999}, {"junction_min_s": 100}, {"tot_dwell_max_s": 5}):
        assert env.patch("/api/settings", json=bad).status_code == 422, bad
    assert env.get("/api/settings").json()["junction_min_s"] == 20.0         # nothing half-applied


def test_numeric_settings_persist_and_bad_files_fall_back(tmp_path):
    p = tmp_path / "s.json"
    SettingsStore(p).update({"stop_sign_s": 7, "tot_dwell_min_s": 5, "tot_dwell_max_s": 9})
    s = SettingsStore(p).data
    assert (s["stop_sign_s"], s["tot_dwell_min_s"], s["tot_dwell_max_s"]) == (7.0, 5.0, 9.0)
    p.write_text('{"stop_sign_s": 9999, "junction_min_s": 200, "junction_max_s": 20, "noise": true}')
    s = SettingsStore(p).data
    assert s["stop_sign_s"] == NUMBERS["stop_sign_s"][0] and s["junction_min_s"] < s["junction_max_s"] and s["noise"] is True


# ---- stop signs vs junctions ----
def test_stop_signs_are_short_and_fixed_and_junctions_are_longer_and_adjustable():
    sign = {traffic_stop(5, random.Random(i), 3, 10, 35) for i in range(300)} - {(0.0, "")}
    assert sign == {(3.0, "stop_sign")}
    waits = [traffic_stop(120, random.Random(i), 3, 10, 35) for i in range(400)]
    junction = [w for w, k in waits if k == "junction"]
    assert junction and all(10 <= w <= 35 for w in junction) and min(junction) > 3
    custom = [w for w, _ in (traffic_stop(120, random.Random(i), 3, 60, 90) for i in range(200)) if w]
    assert custom and all(60 <= w <= 90 for w in custom)


def test_a_drive_waits_at_junctions_for_the_configured_time(env):
    env.patch("/api/settings", json={"traffic_stops": True, "junction_min_s": 1, "junction_max_s": 1, "stop_sign_s": 1})
    env.post("/api/location", json={"lat": 0, "lon": 0})
    env.post("/api/journey", json={"stops": [{"lat": 0.002, "lon": 0.002}], "mode": "drive", "speed_kmh": 400})   # the fake router turns a corner
    reasons = set()
    end = time.time() + 20
    while time.time() < end:
        w = env.get("/api/status").json()["walk"]
        if w.get("state") == "waiting":
            reasons.add(w["reason"])
            assert w["wait_left_s"] <= 1.0 + 0.1
        if not w["active"]:
            break
        time.sleep(0.01)
    assert reasons <= {"traffic", "stop_sign"}


# ---- trick or treat dwell range ----
def test_trick_or_treat_uses_the_dwell_range_from_settings(env):
    env.patch("/api/settings", json={"tot_dwell_min_s": 30, "tot_dwell_max_s": 45})
    env.post("/api/trick-or-treat/plan", json={"lat": 1, "lon": 1, "houses": 5})
    assert env.houses.opts["dwell_range"] == (30.0, 45.0)
    from locspoof.trickortreat import build_plan
    pts = [(1 + i * 0.0003, 1) for i in range(15)]
    plan = build_plan(pts, (1, 1), 15, random.Random(3), (30, 45))
    assert all(30 <= h["dwell_s"] <= 45 + 50 for h in plan)             # one in five gets a longer chat on top
    assert sum(30 <= h["dwell_s"] <= 45 for h in plan) >= 8


# ---- panic ----
def test_panic_needs_a_location_then_jumps_there_and_stops_the_trip(env):
    assert env.get("/api/panic").json() == {"location": None}
    r = env.post("/api/panic/go")
    assert r.status_code == 409 and r.json()["error"]["code"] == "no_panic"
    assert env.put("/api/panic", json={"lat": 51.5, "lon": -0.12, "name": "Home"}).json()["location"]["name"] == "Home"
    assert env.get("/api/panic").json()["location"]["lat"] == 51.5
    env.post("/api/location", json={"lat": 0, "lon": 0})
    env.post("/api/journey", json={"stops": [{"lat": 0.5, "lon": 0.5}], "mode": "straight", "speed_kmh": 10})
    assert env.get("/api/status").json()["walk"]["active"]
    assert env.post("/api/panic/go").status_code == 200
    st = env.get("/api/status").json()
    assert not st["walk"]["active"] and st["simulation"]["lat"] == pytest.approx(51.5) and st["simulation"]["label"] == "Home"
    texts = [e["text"] for e in env.get("/api/activity").json()["events"]]
    assert any("PANIC" in t and "Home" in t for t in texts)
    assert env.delete("/api/panic").json() == {"location": None} and env.post("/api/panic/go").status_code == 409


def test_panic_rejects_bad_coordinates_and_survives_a_corrupt_file(env):
    assert env.put("/api/panic", json={"lat": 123, "lon": 0}).status_code == 422
    (env.dir / "p.json").write_text("{nope")
    assert env.get("/api/panic").json() == {"location": None}


# ---- preview ----
def test_route_preview_returns_the_road_in_order(env):
    pts = [[0, 0], [0, 0.001], [0.001, 0.001]]
    r = env.post("/api/route-preview", json={"points": pts, "mode": "walk"}).json()
    assert len(r["legs"]) == 2 and r["route"][0] == [0, 0] and r["route"][-1] == [0.001, 0.001]
    s = env.post("/api/route-preview", json={"points": pts, "mode": "straight"}).json()
    assert s["legs"] == [[[0, 0], [0, 0.001]], [[0, 0.001], [0.001, 0.001]]]
    assert env.post("/api/route-preview", json={"points": [[0, 0]], "mode": "walk"}).status_code == 422
    assert env.post("/api/route-preview", json={"points": pts, "mode": "fly"}).status_code == 422
    assert env.post("/api/route-preview", json={"points": [[0, 0], [999, 0]], "mode": "walk"}).status_code == 422


# ---- real stop signs / lights (OpenStreetMap) and the California limit ----
class SignalHouses(FakeHouses):
    """A house finder that also answers the stop-sign query, like the real one."""
    def __init__(self, fail=False):
        super().__init__()
        self.fail, self.queries = fail, []

    async def query_json(self, query):
        self.queries.append(query)
        if self.fail:
            raise RuntimeError("Overpass is down")
        return {"elements": [
            {"type": "node", "lat": 0.0, "lon": 0.001, "tags": {"highway": "stop"}},               # on the first road
            {"type": "node", "lat": 0.001, "lon": 0.002, "tags": {"highway": "traffic_signals"}},  # on the second road
            {"type": "node", "lat": 0.0005, "lon": 0.0005, "tags": {"highway": "stop"}},           # a street the route never uses
            {"type": "node", "lat": 0.0, "lon": 0.0015, "tags": {"highway": "crossing"}},          # not a sign
        ]}


def drive_env(tmp_path, houses):
    app = create_app(backend=FakeBackend(), router=FakeRouter(), house_finder=houses, favorites_path=tmp_path / "f.json",
                     recents_path=tmp_path / "r.json", offline_root=tmp_path / "o", settings_path=tmp_path / "s.json",
                     routes_path=tmp_path / "rt.json", devices_path=tmp_path / "d.json", schedules_path=tmp_path / "sch.json",
                     activity_path=tmp_path / "act.json", panic_path=tmp_path / "p.json", walk_tick=0.02, ambient_tick=0.5, rng=random.Random(5))
    return TestClient(app)


def watch_waits(c, timeout=40):
    seen, end = [], time.time() + timeout
    while time.time() < end:
        w = c.get("/api/status").json()["walk"]
        if w.get("state") == "waiting":
            seen.append((w["reason"], round(w["wait_left_s"], 1)))
        if not w["active"]:
            break
        time.sleep(0.01)
    return seen


def test_a_drive_stops_at_the_real_stop_sign_for_the_configured_time(tmp_path):
    houses = SignalHouses()
    with drive_env(tmp_path, houses) as c:
        c.patch("/api/settings", json={"traffic_stops": True, "stop_sign_s": 2, "junction_min_s": 1, "junction_max_s": 1})
        c.post("/api/location", json={"lat": 0, "lon": 0})
        r = c.post("/api/journey", json={"stops": [{"lat": 0.002, "lon": 0.002}], "mode": "drive", "speed_kmh": 200})
        assert r.status_code == 200 and len(houses.queries) == 1 and "traffic_signals" in houses.queries[0]
        waits = watch_waits(c)
        signs = [w for w in waits if w[0] == "stop_sign"]
        assert signs and max(t for _, t in signs) <= 2.0 and max(t for _, t in signs) > 1.0       # one 2-second stop sign wait
        assert {k for k, _ in waits} <= {"stop_sign", "traffic"}
        texts = [e["text"] for e in c.get("/api/activity").json()["events"]]
        assert any("Found 2 real stop sign" in t for t in texts)                                   # the unused side street is ignored


def test_no_lookup_when_traffic_stops_is_off_or_walking(tmp_path):
    houses = SignalHouses()
    with drive_env(tmp_path, houses) as c:
        c.post("/api/location", json={"lat": 0, "lon": 0})
        c.post("/api/journey", json={"stops": [{"lat": 0.002, "lon": 0.002}], "mode": "drive", "speed_kmh": 390})
        watch_waits(c)
        c.patch("/api/settings", json={"traffic_stops": True})
        c.post("/api/journey", json={"stops": [{"lat": 0.002, "lon": 0.002}], "mode": "walk", "speed_kmh": 390})
        watch_waits(c)
        assert houses.queries == []


def test_if_the_lookup_fails_the_drive_still_works_with_random_stops(tmp_path):
    with drive_env(tmp_path, SignalHouses(fail=True)) as c:
        c.patch("/api/settings", json={"traffic_stops": True, "junction_min_s": 1, "junction_max_s": 1})
        c.post("/api/location", json={"lat": 0, "lon": 0})
        assert c.post("/api/journey", json={"stops": [{"lat": 0.002, "lon": 0.002}], "mode": "drive", "speed_kmh": 390}).status_code == 200
        watch_waits(c)
        assert not c.get("/api/status").json()["walk"].get("error")


def test_signals_helpers():
    from locspoof import signals
    path = [(0.0, 0.0), (0.0, 0.002), (0.002, 0.002)]
    data = SignalHouses()
    import asyncio
    found = signals.parse(asyncio.run(data.query_json("")))
    assert len(found) == 3
    on = signals.along(path, found)
    assert [s["kind"] for s in on] == ["stop_sign", "signals"] and all(s["done"] is False for s in on)
    assert signals.bbox_of(path) is not None and signals.bbox_of([(0, 0), (5, 5)]) is None


def test_search_is_limited_to_california_by_default_and_can_be_unlimited(tmp_path):
    import httpx
    from locspoof.geocode import Geocoder
    seen = []

    def handler(request):
        seen.append(dict(request.url.params))
        return httpx.Response(200, json=[{"display_name": "Somewhere", "lat": "37", "lon": "-120"}])

    geo = Geocoder(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), min_interval=0)
    app = create_app(backend=FakeBackend(), router=FakeRouter(), house_finder=FakeHouses(), geocoder=geo, favorites_path=tmp_path / "f.json",
                     recents_path=tmp_path / "r.json", offline_root=tmp_path / "o", settings_path=tmp_path / "s.json",
                     routes_path=tmp_path / "rt.json", devices_path=tmp_path / "d.json", schedules_path=tmp_path / "sch.json",
                     activity_path=tmp_path / "act.json", panic_path=tmp_path / "p.json")
    with TestClient(app) as c:
        assert c.get("/api/settings").json()["california_only"] is True
        assert c.get("/api/search", params={"q": "coffee shop"}).status_code == 200
        assert seen[-1]["bounded"] == "1" and seen[-1]["viewbox"].startswith("-124.6,42.1,-114.0,32.4")
        c.patch("/api/settings", json={"california_only": False})
        c.get("/api/search", params={"q": "tea house"})
        assert "bounded" not in seen[-1]


# ---- snapping a pin to the nearest road ----
class SnapRouter(FakeRouter):
    def __init__(self, result):
        super().__init__()
        self.result, self.asked = result, []

    async def nearest(self, lat, lon, mode="walk"):
        self.asked.append((lat, lon, mode))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def snap_env(tmp_path, router):
    app = create_app(backend=FakeBackend(), router=router, house_finder=FakeHouses(), favorites_path=tmp_path / "f.json",
                     recents_path=tmp_path / "r.json", offline_root=tmp_path / "o", settings_path=tmp_path / "s.json",
                     routes_path=tmp_path / "rt.json", devices_path=tmp_path / "d.json", schedules_path=tmp_path / "sch.json",
                     activity_path=tmp_path / "act.json", panic_path=tmp_path / "p.json")
    return TestClient(app)


def test_snap_moves_the_pin_to_the_nearest_road(tmp_path):
    router = SnapRouter((34.0001, -118.0, 11.1))
    with snap_env(tmp_path, router) as c:
        r = c.get("/api/snap", params={"lat": 34.0002, "lon": -118.0}).json()
        assert r["snapped"] is True and r["lat"] == 34.0001 and 10 < r["moved_m"] < 12.5
        assert router.asked == [(34.0002, -118.0, "walk")]
        c.patch("/api/settings", json={"snap_pin": False})
        assert c.get("/api/snap", params={"lat": 34.0002, "lon": -118.0}).json()["snapped"] is False
        assert len(router.asked) == 1                                    # switched off: the service isn't even asked


@pytest.mark.parametrize("result", [None, RuntimeError("down"), (34.01, -118.0, 900.0)])
def test_snap_leaves_the_pin_alone_when_unsure(tmp_path, result):
    with snap_env(tmp_path, SnapRouter(result)) as c:
        r = c.get("/api/snap", params={"lat": 34.0, "lon": -118.0}).json()
        assert r == {"lat": 34.0, "lon": -118.0, "moved_m": 0.0, "snapped": False}
        assert c.get("/api/snap", params={"lat": 99, "lon": 0}).status_code == 422


def test_the_real_router_asks_osrm_for_the_nearest_road():
    import asyncio
    import httpx
    from locspoof.routing import Router
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200, json={"code": "Ok", "waypoints": [{"location": [-118.0, 34.0001], "distance": 8.5}]})

    router = Router(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), min_interval=0)
    assert asyncio.run(router.nearest(34.0, -118.0)) == (34.0001, -118.0, 8.5)
    assert "/nearest/v1/" in seen[0] and "-118.000000,34.000000" in seen[0]
    broken = Router(client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500))), min_interval=0)
    assert asyncio.run(broken.nearest(34.0, -118.0)) is None


# ---- phone favorites and nicknames ----
def test_phones_can_be_starred_and_renamed_and_it_sticks(tmp_path):
    def app_():
        return create_app(backend=FakeBackend(), router=FakeRouter(), house_finder=FakeHouses(), favorites_path=tmp_path / "f.json",
                          recents_path=tmp_path / "r.json", offline_root=tmp_path / "o", settings_path=tmp_path / "s.json",
                          routes_path=tmp_path / "rt.json", devices_path=tmp_path / "d.json", schedules_path=tmp_path / "sch.json",
                          activity_path=tmp_path / "act.json", panic_path=tmp_path / "p.json")

    with TestClient(app_()) as c:
        phone = c.get("/api/devices").json()["devices"][0]
        assert phone["nickname"] == "" and phone["favorite"] is False and phone["display_name"] == phone["name"]
        udid = phone["udid"]
        r = c.patch(f"/api/devices/{udid}", json={"nickname": "  Mum's   phone ", "favorite": True})
        assert r.status_code == 200 and r.json()["nickname"] == "Mum's phone" and r.json()["favorite"] is True
        phone = c.get("/api/devices").json()["devices"][0]
        assert phone["display_name"] == "Mum's phone" and phone["name"] != "Mum's phone" and phone["favorite"]
        assert c.patch(f"/api/devices/{udid}", json={"nickname": 5}).status_code == 422
        assert c.patch(f"/api/devices/{udid}", json={"favorite": "yes"}).status_code == 422
        assert c.patch("/api/devices/x", json={"favorite": True}).status_code == 422
    with TestClient(app_()) as c:                                              # a restart remembers both
        phone = c.get("/api/devices").json()["devices"][0]
        assert phone["display_name"] == "Mum's phone" and phone["favorite"]
        c.patch(f"/api/devices/{phone['udid']}", json={"nickname": "", "favorite": False})
        phone = c.get("/api/devices").json()["devices"][0]
        assert phone["display_name"] == phone["name"] and not phone["favorite"]


def test_an_unplugged_favorite_phone_is_still_listed_first(tmp_path):
    from locspoof.memory import DeviceMemory
    mem = DeviceMemory(tmp_path / "d.json")
    mem.remember("AAAA-BBBB-CCCC-DDDD", "Old iPhone", "16.1")
    mem.set_meta("AAAA-BBBB-CCCC-DDDD", "Spare", True)
    app = create_app(backend=FakeBackend(), router=FakeRouter(), house_finder=FakeHouses(), favorites_path=tmp_path / "f.json",
                     recents_path=tmp_path / "r.json", offline_root=tmp_path / "o", settings_path=tmp_path / "s.json",
                     routes_path=tmp_path / "rt.json", devices_path=tmp_path / "d.json", schedules_path=tmp_path / "sch.json",
                     activity_path=tmp_path / "act.json", panic_path=tmp_path / "p.json")
    with TestClient(app) as c:
        devs = c.get("/api/devices").json()["devices"]
        assert devs[0]["display_name"] == "Spare" and devs[0]["offline"] is True and devs[0]["favorite"]
        assert devs[1]["offline"] is False


# ---- Developer Mode ----
def test_the_phone_list_shows_developer_mode_and_it_can_be_enabled(tmp_path):
    backend = FakeBackend()
    backend.developer_mode = False
    app = create_app(backend=backend, router=FakeRouter(), house_finder=FakeHouses(), favorites_path=tmp_path / "f.json",
                     recents_path=tmp_path / "r.json", offline_root=tmp_path / "o", settings_path=tmp_path / "s.json",
                     routes_path=tmp_path / "rt.json", devices_path=tmp_path / "d.json", schedules_path=tmp_path / "sch.json",
                     activity_path=tmp_path / "act.json", panic_path=tmp_path / "p.json")
    with TestClient(app) as c:
        phone = c.get("/api/devices").json()["devices"][0]
        assert phone["developer_mode"] is False
        for state in ("restarting", "manual", "already_on"):
            backend.developer_result = {"state": state}
            r = c.post(f"/api/devices/{phone['udid']}/developer-mode")
            assert r.status_code == 200 and r.json() == {"state": state}
        assert backend.developer_calls == 3
        texts = [e["text"] for e in c.get("/api/activity").json()["events"]]
        assert any("Developer Mode" in t and "turn it on in Settings" in t for t in texts)
        from locspoof.errors import AppError
        backend.developer_result = AppError("no_developer_mode", "No switch.", "Only iOS 16+.", 409)
        assert c.post(f"/api/devices/{phone['udid']}/developer-mode").status_code == 409


# ---- connection history ----
def test_connection_events_are_tagged_and_can_be_filtered(tmp_path):
    from locspoof.activity import ActivityLog
    log = ActivityLog(tmp_path / "a.json")
    log.add("Location set", kind="location")
    log.add("iPhone connected (USB)", kind="link", tag="up")
    log.add("Lost the connection", kind="link", level="warn", tag="drop")
    assert [e["text"] for e in log.list(kinds={"link"})] == ["iPhone connected (USB)", "Lost the connection"]
    assert [e.get("tag") for e in log.list(kinds={"link"})] == ["up", "drop"] and "tag" not in log.list()[0]
    assert ActivityLog(tmp_path / "a.json").list(kinds={"link"})[1]["tag"] == "drop"          # survives a restart


def test_a_cable_pull_and_replug_shows_up_in_the_connection_history(tmp_path):
    backend = FakeBackend()
    app = create_app(backend=backend, router=FakeRouter(), house_finder=FakeHouses(), favorites_path=tmp_path / "f.json",
                     recents_path=tmp_path / "r.json", offline_root=tmp_path / "o", settings_path=tmp_path / "s.json",
                     routes_path=tmp_path / "rt.json", devices_path=tmp_path / "d.json", schedules_path=tmp_path / "sch.json",
                     activity_path=tmp_path / "act.json", walk_tick=0.02, ambient_tick=0.5, rng=random.Random(1))
    with TestClient(app) as c:
        c.get("/api/status")
        c.post("/api/location", json={"lat": 1, "lon": 1})
        backend.unplug()
        time.sleep(0.2)
        c.get("/api/status", params={"fresh": "true"})
        backend.replug()
        time.sleep(0.3)
        c.get("/api/status")
        ev = c.get("/api/activity", params={"kinds": "link"}).json()["events"]
        assert ev and all(e["kind"] == "link" for e in ev) and ev[0]["tag"] == "up" and "(🔌 USB)" in ev[0]["text"]
        assert c.get("/api/activity", params={"kinds": "location"}).json()["events"][0]["text"].startswith("Location set")


# ---- trick or treat: more houses, lights-off houses, start and finish at the pin ----
def test_lights_off_chance_is_its_own_setting():
    from locspoof.trickortreat import build_plan
    pts = [(1 + i * 0.0003, 1) for i in range(60)]
    some = build_plan(pts, (1, 1), 60, random.Random(2), lights_off_p=0.3)
    off = [h for h in some if h["lights_off"]]
    assert 8 <= len(off) <= 30 and not some[0]["lights_off"] and all(h["dwell_s"] == 0 for h in off)
    assert not any(h["lights_off"] for h in build_plan(pts, (1, 1), 60, random.Random(2), lights_off_p=0.0))
    assert not any(h["lights_off"] for h in build_plan(pts, (1, 1), 60, random.Random(2)))                      # old default: none
    assert any(h["lights_off"] for h in build_plan(pts, (1, 1), 60, random.Random(2), realistic=True))          # realistic still means 15%


def test_the_tot_settings_and_limits(env):
    s = env.get("/api/settings").json()
    assert s["tot_start_at_pin"] is True and s["tot_lights_off"] is True and s["tot_lights_off_pct"] == 20.0
    assert env.patch("/api/settings", json={"tot_lights_off_pct": 35}).json()["tot_lights_off_pct"] == 35.0
    assert env.patch("/api/settings", json={"tot_lights_off_pct": 90}).status_code == 422
    env.post("/api/trick-or-treat/plan", json={"lat": 1, "lon": 2, "houses": 70})
    assert env.houses.opts["lights_off_p"] == 0.35
    env.patch("/api/settings", json={"tot_lights_off": False})
    env.post("/api/trick-or-treat/plan", json={"lat": 1, "lon": 2, "houses": 80})
    assert env.houses.opts["lights_off_p"] == 0.0
    assert env.post("/api/trick-or-treat/plan", json={"lat": 1, "lon": 2, "houses": 81}).status_code == 422
    assert env.post("/api/route-preview", json={"points": [[0, 0.0001 * i] for i in range(90)], "mode": "straight"}).status_code == 200


def test_a_long_tour_of_70_houses_can_start_and_return_to_the_pin(tmp_path):
    app = create_app(backend=FakeBackend(), router=FakeRouter(), house_finder=FakeHouses(), favorites_path=tmp_path / "f.json",
                     recents_path=tmp_path / "r.json", offline_root=tmp_path / "o", settings_path=tmp_path / "s.json",
                     routes_path=tmp_path / "rt.json", devices_path=tmp_path / "d.json", schedules_path=tmp_path / "sch.json",
                     activity_path=tmp_path / "act.json", walk_tick=0.001, ambient_tick=0.5, rng=random.Random(1))
    with TestClient(app) as c:
        c.post("/api/location", json={"lat": 10.0, "lon": 10.0})                              # the pin
        stops = [{"lat": 10.0 + 0.0002 * i, "lon": 10.0, "dwell_s": 0, "label": f"house {i + 1}"} for i in range(1, 71)]
        r = c.post("/api/journey", json={"stops": stops, "mode": "straight", "speed_kmh": 400, "kind": "trick_or_treat", "return_to_start": True})
        assert r.status_code == 200
        end = time.time() + 40
        while time.time() < end and c.get("/api/status").json()["walk"]["active"]:
            time.sleep(0.05)
        sim = c.get("/api/status").json()["simulation"]
        assert abs(sim["lat"] - 10.0) < 1e-4 and abs(sim["lon"] - 10.0) < 1e-4                # finished back at the pin


# ---- real location when not spoofing ----
def test_where_am_i_uses_the_ip_lookup_and_caches_it(tmp_path):
    import asyncio
    import httpx
    from locspoof.locate import IpLocator
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(200, json={"success": True, "latitude": 37.77, "longitude": -122.42, "city": "San Francisco", "region": "California"})

    loc = IpLocator(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    r = asyncio.run(loc.where())
    assert r == {"ok": True, "lat": 37.77, "lon": -122.42, "label": "San Francisco, California", "approximate": True}
    asyncio.run(loc.where())
    assert len(calls) == 1                                                          # cached
    bad = IpLocator(client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"success": False}))))
    assert asyncio.run(bad.where()) == {"ok": False}
    down = IpLocator(client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: (_ for _ in ()).throw(httpx.ConnectError("offline")))))
    assert asyncio.run(down.where()) == {"ok": False}


def test_the_where_endpoint_and_setting(tmp_path):
    class Fake:
        async def where(self):
            return {"ok": True, "lat": 1.0, "lon": 2.0, "label": "Somewhere", "approximate": True}
    app = create_app(backend=FakeBackend(), router=FakeRouter(), house_finder=FakeHouses(), favorites_path=tmp_path / "f.json",
                     recents_path=tmp_path / "r.json", offline_root=tmp_path / "o", settings_path=tmp_path / "s.json",
                     routes_path=tmp_path / "rt.json", devices_path=tmp_path / "d.json", schedules_path=tmp_path / "sch.json",
                     activity_path=tmp_path / "act.json", locator=Fake())
    with TestClient(app) as c:
        assert c.get("/api/settings").json()["show_real_location"] is True
        assert c.get("/api/where").json()["label"] == "Somewhere"
