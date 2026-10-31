import asyncio
import json
import random

import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import FakeBackend, FakeHouses, FakeRouter
from locspoof.busy import BusyRoads, busy_query, parse_busy_roads
from locspoof.errors import AppError
from locspoof.movement import haversine_m
from locspoof.offline import OfflineStore
from locspoof.server import create_app
from locspoof.trickortreat import HouseFinder, build_plan

CENTER = (40.0, -75.0)
# an east-west primary road along lat 40.000 from lon -75.01 to -74.99
ROAD = [(40.0, -75.01), (40.0, -74.99)]


def way(highway, pts):
    return {"type": "way", "tags": {"highway": highway}, "geometry": [{"lat": a, "lon": b} for a, b in pts]}


def houses_grid(rows_south=4, rows_north=4, cols=6, gap=0.0003):
    """Houses in rows south (lat<40) and north (lat>40) of the road, 30 m apart, none within 20 m of it."""
    south = [(40.0 - 0.0004 - r * gap, -75.001 + c * gap) for r in range(rows_south) for c in range(cols)]
    north = [(40.0 + 0.0004 + r * gap, -75.001 + c * gap) for r in range(rows_north) for c in range(cols)]
    return south, north


# ---- geometry ------------------------------------------------------------------------------------

def test_busy_roads_crossing_and_proximity():
    b = BusyRoads([ROAD], CENTER)
    assert b and b.crosses((39.999, -75.0), (40.001, -75.0))               # straight across
    assert not b.crosses((39.999, -75.0), (39.9995, -74.999))              # stays on one side
    assert not b.crosses((40.001, -75.0), (40.002, -74.999))
    assert b.near((40.00005, -75.0), 20) and not b.near((40.0004, -75.0), 20)   # 5 m vs ~44 m
    assert not BusyRoads([], CENTER) and not BusyRoads([ROAD], CENTER).crosses((39.9, -76), (39.9, -75.9))


def test_parse_busy_roads_filters_by_highway_class():
    data = {"elements": [way("primary", ROAD), way("residential", ROAD), way("secondary_link", ROAD), way("footway", ROAD),
                         {"type": "way", "tags": {"highway": "trunk"}, "geometry": [{"lat": 1, "lon": 1}]}, {"type": "node"}]}
    assert len(parse_busy_roads(data)) == 2
    assert 'around:550,40.000000,-75.000000' in busy_query(40, -75, 400) and "poly:" in busy_query(40, -75, 400, [(1, 1), (1, 2), (2, 2)])


# ---- tour planning -------------------------------------------------------------------------------

def test_avoid_busy_drops_roadside_houses_and_never_crosses_the_road():
    south, north = houses_grid()
    roadside = [(40.00005, -75.0005), (39.99995, -75.0), (40.00004, -74.9995)]       # ~5 m from the road
    pts = south + north + roadside
    busy = BusyRoads([ROAD], CENTER)
    info = {}
    plan = build_plan(pts, (39.9995, -75.0), 20, random.Random(1), busy=busy, info=info)      # fits on the south side
    got = [(p["lat"], p["lon"]) for p in plan]
    assert len(plan) == 20 and all(lat < 40.0 for lat, _ in got)                              # whole tour stays south
    assert not any(p in got for p in roadside) and info["skipped_busy"] == 3 and info["cut_by_busy"] is False
    assert all(not busy.crosses(a, b) for a, b in zip(got, got[1:]))
    # asking for more than exist on this side: the tour just ends (cut) instead of crossing
    plan2 = build_plan(pts, (39.9995, -75.0), 100, random.Random(1), busy=busy, info=info)
    assert info["cut_by_busy"] is True and len(plan2) == len(south) and all(p["lat"] < 40.0 for p in plan2)
    # without the toggle the tour happily crosses and includes roadside houses
    free = build_plan(pts, (39.9995, -75.0), 100, random.Random(1))
    assert any(lat > 40.0 for lat, _ in [(p["lat"], p["lon"]) for p in free]) and len(free) == len(pts)


def test_avoid_busy_routes_around_when_a_side_street_exists():
    """Houses on the same side connected along the road are not 'across'; only true crossings are blocked."""
    south, _ = houses_grid(rows_north=0)
    plan = build_plan(south, (39.9995, -75.0), 100, random.Random(2), busy=BusyRoads([ROAD], CENTER))
    assert len(plan) == len(south)


def test_kids_make_every_pause_longer_by_the_same_factor():
    south, north = houses_grid()
    base = build_plan(south + north, CENTER, 20, random.Random(5))
    kids = build_plan(south + north, CENTER, 20, random.Random(5), kids=True)
    assert [(p["lat"], p["lon"]) for p in base] == [(p["lat"], p["lon"]) for p in kids]
    for a, b in zip(base, kids):
        assert b["dwell_s"] == pytest.approx(a["dwell_s"] * 1.6, abs=0.15)


def test_realistic_houses_have_lights_off_and_long_chats():
    pts = [(40 + i * 0.0003, -75.0) for i in range(300)]
    plain = build_plan(pts, CENTER, 300, random.Random(6))
    real = build_plan(pts, CENTER, 300, random.Random(6), realistic=True)
    assert not any(p["lights_off"] for p in plain)
    off = [p for p in real if p["lights_off"]]
    assert 0.08 * 300 < len(off) < 0.22 * 300 and not real[0]["lights_off"] and all(p["dwell_s"] == 0 for p in off)
    visited = [p["dwell_s"] for p in real if not p["lights_off"]]
    assert sum(visited) / len(visited) > sum(p["dwell_s"] for p in plain) / len(plain) + 3     # chats add time


# ---- HouseFinder (Overpass mocked) ---------------------------------------------------------------

def overpass(handler_busy="ok", houses=None):
    south, north = houses_grid()
    pts = houses if houses is not None else south + north + [(40.00005, -75.0005)]

    def handler(req):
        q = req.content.decode()
        if "primary" in q:                                   # the busy-roads query
            return httpx.Response(200, json={"elements": [way("primary", ROAD)]}) if handler_busy == "ok" else httpx.Response(504)
        return httpx.Response(200, json={"elements": [{"type": "way", "center": {"lat": a, "lon": b}} for a, b in pts]})

    return HouseFinder(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), min_interval=0)


def test_finder_avoid_busy_end_to_end_with_notes():
    hf = overpass()
    plan = asyncio.run(hf.plan(39.9995, -75.0, 400, 60, random.Random(1), avoid_busy=True))
    assert all(p["lat"] < 40.0 for p in plan)
    assert any("Skipped 1 house" in n for n in hf.last_notes) and any("across a busy road" in n for n in hf.last_notes)
    assert asyncio.run(overpass().plan(39.9995, -75.0, 400, 60, random.Random(1))) != plan      # toggle off: crosses freely


def test_finder_notes_when_road_data_unavailable_or_no_big_roads():
    hf = overpass(handler_busy="down")
    plan = asyncio.run(hf.plan(39.9995, -75.0, 400, 10, random.Random(1), avoid_busy=True))
    assert len(plan) == 10 and hf.last_notes == ["Could not load road data, so busy roads were not avoided."]

    def no_roads(req):
        q = req.content.decode()
        if "primary" in q:
            return httpx.Response(200, json={"elements": []})
        return httpx.Response(200, json={"elements": [{"type": "way", "center": {"lat": 40 + i * 0.0003, "lon": -75}} for i in range(8)]})

    hf = HouseFinder(client=httpx.AsyncClient(transport=httpx.MockTransport(no_roads)), min_interval=0)
    asyncio.run(hf.plan(40, -75, 400, 5, random.Random(1), avoid_busy=True))
    assert hf.last_notes == ["No major roads found here, so nothing needed avoiding."]


def test_finder_error_hint_mentions_the_toggle_when_avoiding_leaves_too_few():
    south, _ = houses_grid(rows_south=1, rows_north=0, cols=2)
    hf = overpass(houses=south + [(40.00005, -75.0005), (40.00004, -74.9995)])      # 2 fine houses + 2 roadside
    with pytest.raises(AppError) as e:
        asyncio.run(hf.plan(39.9995, -75.0, 400, 10, avoid_busy=True))
    assert e.value.code == "no_houses" and "Avoid busy roads" in e.value.hint


def test_busy_roads_come_from_the_offline_pack_when_overpass_is_down(tmp_path):
    store = OfflineStore(tmp_path)
    store.new_pack_dir("p")
    box = [39.99, -75.01, 40.01, -74.99]
    (store.packs_dir / "p" / "roads.json").write_text(json.dumps({"elements": [way("primary", ROAD), way("residential", [(39.999, -75.0), (39.999, -74.999)])]}))
    south, north = houses_grid()
    (store.packs_dir / "p" / "houses.json").write_text(json.dumps(south + north))
    store.write_meta("p", {"id": "p", "name": "p", "bbox": box, "created": 1, "bytes": 1, "houses": 48, "tiles": []})
    hf = HouseFinder(client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(504))), min_interval=0, offline=store)
    plan = asyncio.run(hf.plan(39.9995, -75.0, 400, 60, random.Random(1), avoid_busy=True))
    assert hf.last_source == "offline" and plan and all(p["lat"] < 40.0 for p in plan)
    assert store.busy_roads(10, 10) is None


# ---- settings -> plan endpoint ---------------------------------------------------------------------

@pytest.fixture
def api(tmp_path):
    finder = FakeHouses()
    app = create_app(backend=FakeBackend(), router=FakeRouter(), house_finder=finder, favorites_path=tmp_path / "f.json",
                     recents_path=tmp_path / "r.json", offline_root=tmp_path / "o", settings_path=tmp_path / "s.json",
                     routes_path=tmp_path / "rt.json")
    with TestClient(app) as c:
        c.finder = finder
        yield c


def test_plan_endpoint_applies_toggles_and_recommends_pace(api):
    body = {"lat": 1, "lon": 2, "houses": 5}
    r = api.post("/api/trick-or-treat/plan", json=body).json()
    assert api.finder.opts == {"realistic": False, "kids": False, "avoid_busy": False, "dwell_range": (20.0, 70.0), "lights_off_p": 0.2}
    assert r["pace"] == {"speed_kmh": 4.5, "speed_jitter": 0.2} and r["notes"] == [] and r["houses"][0]["lights_off"] is False
    api.patch("/api/settings", json={"tot_kids": True, "tot_avoid_busy": True, "tot_realistic": True})
    r = api.post("/api/trick-or-treat/plan", json=body).json()
    assert api.finder.opts == {"realistic": True, "kids": True, "avoid_busy": True, "dwell_range": (20.0, 70.0), "lights_off_p": 0.2}
    assert r["pace"] == {"speed_kmh": 3.2, "speed_jitter": 0.3}
