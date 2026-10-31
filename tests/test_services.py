import asyncio
import random

import httpx
import pytest

from locspoof.errors import AppError
from locspoof.movement import PathFollower, haversine_m
from locspoof.routing import Router
from locspoof.trickortreat import HouseFinder, build_plan, build_query, dedupe, parse_points


# ---- PathFollower -------------------------------------------------------------------------

def test_path_follower_crosses_vertices_and_finishes():
    pts = [(0, 0), (0, 0.001), (0.001, 0.001)]
    total = haversine_m(*pts[0], *pts[1]) + haversine_m(*pts[1], *pts[2])
    f = PathFollower(pts)
    assert f.remaining_m == pytest.approx(total, rel=1e-6)
    lat, lon, done = f.advance(haversine_m(*pts[0], *pts[1]) + 10)  # past the corner
    assert not done and lon == pytest.approx(0.001, abs=1e-6) and lat > 0
    *_, done = f.advance(1e6)
    assert done and f.pos == (0.001, 0.001) and f.remaining_m == pytest.approx(0, abs=1e-6)


def test_path_follower_single_point_is_done_immediately():
    f = PathFollower([(1, 2)])
    assert f.advance(5) == (1, 2, True)


# ---- Router (mocked OSRM) -----------------------------------------------------------------

def osrm_body(waypoints):
    legs = []
    for (la1, lo1), (la2, lo2) in zip(waypoints, waypoints[1:]):
        mid = [lo1, la2]  # snapped corner
        legs.append({"steps": [{"geometry": {"coordinates": [[lo1 + 1e-5, la1], mid]}},
                               {"geometry": {"coordinates": [mid, [lo2 - 1e-5, la2]]}}]})
    return {"code": "Ok", "routes": [{"legs": legs}]}


def parse_coords(url: httpx.URL):
    raw = url.path.rsplit("/", 1)[1]
    return [(float(b), float(a)) for a, b in (p.split(",") for p in raw.split(";"))]


def make_router(handler, interval=0):
    return Router(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), min_interval=interval)


def test_router_splits_legs_uses_profiles_and_caches():
    seen = []

    def handler(req):
        seen.append(req)
        return httpx.Response(200, json=osrm_body(parse_coords(req.url)))

    r = make_router(handler)
    wps = [(10.0, 20.0), (10.001, 20.002), (10.002, 20.0)]
    legs = asyncio.run(r.route(wps, "walk"))
    assert len(legs) == 2
    assert legs[0][0] == wps[0] and legs[0][-1] == wps[1] and legs[1][0] == wps[1] and legs[1][-1] == wps[2]
    assert (10.001, 20.0) in legs[0]  # route geometry (not just the straight line) came through
    assert "routed-foot" in seen[0].url.path and seen[0].headers["user-agent"].startswith("local-iphone")
    asyncio.run(r.route(wps, "walk"))   # cached
    assert len(seen) == 1
    asyncio.run(r.route(wps, "drive"))
    assert "routed-car" in seen[1].url.path


def test_router_falls_back_to_second_profile_name():
    paths = []

    def handler(req):
        paths.append(req.url.path)
        if "/driving/" in req.url.path:
            return httpx.Response(400, json={"code": "InvalidUrl"})
        return httpx.Response(200, json=osrm_body(parse_coords(req.url)))

    legs = asyncio.run(make_router(handler).route([(1.0, 1.0), (1.001, 1.001)], "walk"))
    assert len(legs) == 1 and len(paths) == 2


def test_router_errors_are_friendly():
    r = make_router(lambda req: httpx.Response(200, json={"code": "NoRoute"}))
    with pytest.raises(AppError) as e:
        asyncio.run(r.route([(1.0, 1.0), (50.0, 50.0)], "drive"))
    assert e.value.code == "no_route" and "driving" in e.value.message

    def down(req):
        raise httpx.ConnectError("x")

    with pytest.raises(AppError) as e:
        asyncio.run(make_router(down).route([(1.0, 1.0), (1.1, 1.1)], "walk"))
    assert e.value.code == "route_failed"

    with pytest.raises(AppError) as e:
        asyncio.run(make_router(lambda req: httpx.Response(429)).route([(1.0, 1.0), (1.1, 1.1)], "walk"))
    assert e.value.code == "rate_limited"

    with pytest.raises(AppError) as e:
        asyncio.run(make_router(lambda req: httpx.Response(200, json={"code": "Ok", "routes": []})).route([(1.0, 1.0), (1.1, 1.1)], "walk"))
    assert e.value.code == "route_failed"


# ---- Trick-or-treat -----------------------------------------------------------------------

def grid(n=6, step=0.0003):
    return [(40 + i * step, -75 + j * step) for i in range(n) for j in range(n)]


def test_parse_points_handles_ways_and_nodes():
    data = {"elements": [{"type": "way", "center": {"lat": 1, "lon": 2}}, {"type": "node", "lat": 3, "lon": 4}, {"type": "way"}]}
    assert parse_points(data) == [(1.0, 2.0), (3.0, 4.0)]


def test_dedupe_merges_address_node_and_building():
    assert len(dedupe([(40, -75), (40.00002, -75), (40.001, -75)])) == 2


def test_build_plan_is_a_connected_tour_with_pauses():
    pts = grid()
    center = (40.0007, -74.9993)
    plan = build_plan(pts, center, 12, random.Random(1))
    assert len(plan) == 12 and len({(p["lat"], p["lon"]) for p in plan}) == 12
    first = (plan[0]["lat"], plan[0]["lon"])
    assert haversine_m(*first, *center) == min(haversine_m(*center, *p) for p in pts)
    hops = [haversine_m(plan[i]["lat"], plan[i]["lon"], plan[i + 1]["lat"], plan[i + 1]["lon"]) for i in range(11)]
    assert max(hops) < 60  # house-to-house, no big jumps (grid spacing is ~33 m)
    assert all(20 <= p["dwell_s"] <= 140 for p in plan) and len({p["dwell_s"] for p in plan}) > 5


def test_build_plan_empty():
    assert build_plan([], (0, 0), 5) == []


def test_query_is_overpass_ql_with_radius():
    q = build_query(40.5, -75.25, 350)
    assert "around:350,40.500000,-75.250000" in q and '"building"' in q and "addr:housenumber" in q and "out center" in q


def test_house_finder_uses_overpass_then_mirror_and_errors():
    urls = []

    def handler(req):
        urls.append(str(req.url))
        if "overpass-api.de" in str(req.url):
            return httpx.Response(504)
        els = [{"type": "way", "center": {"lat": lat, "lon": lon}} for lat, lon in grid(3)]
        return httpx.Response(200, json={"elements": els})

    hf = HouseFinder(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), min_interval=0)
    plan = asyncio.run(hf.plan(40, -75, 300, 5, random.Random(2)))
    assert len(plan) == 5 and len(urls) == 2 and "kumi" in urls[1]

    hf = HouseFinder(client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"elements": []}))), min_interval=0)
    with pytest.raises(AppError) as e:
        asyncio.run(hf.plan(40, -75, 300, 5))
    assert e.value.code == "no_houses"

    hf = HouseFinder(client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500))), min_interval=0)
    with pytest.raises(AppError) as e:
        asyncio.run(hf.plan(40, -75, 300, 5))
    assert e.value.code == "houses_unavailable"
