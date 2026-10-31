import asyncio
import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import FakeBackend, FakeHouses, FakeRouter
from locspoof.errors import AppError
from locspoof.movement import haversine_m
from locspoof.offline import (
    MAX_TILES, OfflineDownloader, OfflineStore, RoadGraph, bbox_contains, lonlat_to_tile, roads_query,
    tiles_for_bbox, validate_bbox,
)
from locspoof.routing import Router
from locspoof.server import create_app
from locspoof.trickortreat import HouseFinder

PNG = b"\x89PNG\r\n\x1a\nFAKE"
BOX = (39.998, -75.002, 40.004, -74.994)   # ~0.55 km x 0.68 km


# ---- tiles & limits -----------------------------------------------------------------------

def test_tile_math_known_values():
    assert lonlat_to_tile(0, 0, 1) == (1, 1)
    assert lonlat_to_tile(51.5074, -0.1278, 10) == (511, 340)    # London
    assert lonlat_to_tile(85, 179.99, 3) == (7, 0)


def test_tiles_for_bbox_counts_and_bounds():
    tiles = tiles_for_bbox(BOX)
    zs = sorted({z for z, *_ in tiles})
    assert zs == [12, 13, 14, 15, 16, 17] and len(tiles) < 200
    z, x, y = max(tiles)  # highest zoom tile must contain a corner of the box
    assert lonlat_to_tile(BOX[0], BOX[1], 17)[0] <= x


@pytest.mark.parametrize("bbox,code", [
    ((39.0, -75.0, 41.0, -73.0), "area_too_big"),
    ((40.0, -75.0, 40.0001, -74.9999), "area_too_small"),
    ((40.0, -75.0, 39.0, -74.0), "bad_area"),
    ((40.0, 170.0, 41.0, 190.0), "bad_area"),
])
def test_validate_bbox_rejects(bbox, code):
    with pytest.raises(AppError) as e:
        validate_bbox(bbox)
    assert e.value.code == code


# ---- local street router ------------------------------------------------------------------

def way(wid, nodes, **tags):
    return {"type": "way", "id": wid, "nodes": [n[0] for n in nodes],
            "geometry": [{"lat": n[1], "lon": n[2]} for n in nodes], "tags": {"highway": "residential", **tags}}


N = {1: (40.000, -75.000), 2: (40.000, -74.999), 3: (40.000, -74.998), 4: (40.000, -74.997),
     5: (40.001, -74.998), 6: (40.001, -74.997), 20: (40.0035, -74.999), 21: (40.0035, -74.998)}


def n(i):
    return (i, *N[i])


ROADS = {"elements": [
    way(1, [n(1), n(2), n(3), n(4)]),
    way(2, [n(3), n(5), n(6)]),
    way(3, [n(6), n(4)], oneway="yes"),                    # one-way shortcut north -> south
    way(4, [n(20), n(21)], highway="footway"),             # isolated footpath
    {"type": "way", "id": 9, "nodes": [], "tags": {}},     # junk element must be ignored
]}


def near(i, d=0.00001):
    return (N[i][0] + d, N[i][1] + d)


def test_walk_route_follows_streets_and_keeps_exact_endpoints():
    g = RoadGraph.from_overpass(ROADS)
    a, b = near(1), near(6)
    path = g.route(a, b, "walk")
    assert path[0] == a and path[-1] == b
    assert N[3] in path and N[5] in path                   # via the junction, not a diagonal
    assert all(haversine_m(*p, *q) < 120 for p, q in zip(path, path[1:]))


def test_drive_respects_oneway_but_walk_does_not():
    g = RoadGraph.from_overpass(ROADS)
    walk = g.route(near(4), near(6), "walk")
    assert N[3] not in walk and len(walk) == 4             # a, n4, n6, b  (direct, against the one-way)
    drive = g.route(near(4), near(6), "drive")
    assert N[3] in drive and N[5] in drive                 # forced around the loop
    assert N[4] in g.route(near(6), near(4), "drive")      # with the one-way direction is direct
    assert N[3] not in g.route(near(6), near(4), "drive")


def test_footpath_only_area_walkable_but_not_drivable_and_far_points_uncovered():
    g = RoadGraph.from_overpass(ROADS)
    assert g.route(near(20), near(21), "walk") is not None
    assert g.route(near(20), near(21), "drive") is None
    assert g.route((41.0, -75.0), near(1), "walk") is None  # > 150 m from any street


def test_motorways_are_not_walkable():
    g = RoadGraph.from_overpass({"elements": [way(1, [n(1), n(2)], highway="motorway")]})
    assert g.route(near(1), near(2), "walk") is None and g.route(near(1), near(2), "drive") is not None


# ---- downloader / store -------------------------------------------------------------------

class FakeFinder:
    def __init__(self, fail=False):
        self.queries, self.fail = [], fail

    async def query_json(self, q):
        self.queries.append(q)
        if self.fail:
            raise AppError("houses_unavailable", "down", status=502)
        if '["highway"]' in q:
            return ROADS
        return {"elements": [{"type": "way", "center": {"lat": 40.0002 + i * 0.0003, "lon": -74.9995}} for i in range(6)]}


def tile_client(fail_after=None, hits=None):
    count = {"n": 0}

    def handler(req):
        count["n"] += 1
        if hits is not None:
            hits.append(str(req.url))
            assert req.headers["user-agent"].startswith("local-iphone")
        if fail_after is not None and count["n"] > fail_after:
            return httpx.Response(500)
        return httpx.Response(200, content=PNG)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def run_download(store, name="Maple St", bbox=BOX, finder=None, client=None):
    async def go():
        dl = OfflineDownloader(store, finder or FakeFinder(), client=client or tile_client(), tile_delay=0)
        await dl.start(name, bbox)
        await dl._task
        return dl.job
    return asyncio.run(go())


def test_download_creates_complete_pack(tmp_path):
    store = OfflineStore(tmp_path)
    hits = []
    job = run_download(store, client=tile_client(hits=hits))
    assert job["state"] == "done"
    (pack,) = store.list_packs()
    assert pack["name"] == "Maple St" and pack["houses"] == 6 and pack["bytes"] > 0 and "tiles" not in pack
    assert (tmp_path / "packs" / pack["id"] / "roads.json").exists()
    assert all(store.tile_path(z, x, y).read_bytes() == PNG for z, x, y in tiles_for_bbox(BOX))
    assert len(hits) == len(tiles_for_bbox(BOX)) and hits[0].startswith("https://tile.openstreetmap.org/12/")


def test_store_serves_routes_and_houses_from_pack_and_ignores_uncovered_points(tmp_path):
    store = OfflineStore(tmp_path)
    run_download(store)
    legs = store.route([near(1), near(6), near(4)], "walk")
    assert len(legs) == 2 and legs[0][0] == near(1) and legs[1][-1] == near(4)
    assert store.route([near(1), (41, -75)], "walk") is None            # one point outside every pack
    houses = store.houses_near(40.0005, -74.9995, 300)
    assert len(houses) == 6
    poly = [(40.0, -75.0), (40.0, -74.99), (40.0008, -74.99), (40.0008, -75.0)]
    assert 0 < len(store.houses_near(40.0004, -74.995, 300, poly)) < 6   # only those inside the polygon
    assert store.houses_near(50, 50, 500) == []


def test_delete_keeps_tiles_still_used_by_another_pack(tmp_path):
    store = OfflineStore(tmp_path)
    run_download(store, "A", BOX)
    shifted = (BOX[0], BOX[1] + 0.004, BOX[2], BOX[3] + 0.004)         # overlaps A, shares many tiles
    run_download(store, "B", shifted)
    a, b = store.list_packs()[1], store.list_packs()[0]
    tiles_a, tiles_b = set(tiles_for_bbox(BOX)), set(tiles_for_bbox(shifted))
    shared, only_a = tiles_a & tiles_b, tiles_a - tiles_b
    assert shared and only_a
    assert store.delete(a["id"])
    assert all(store.tile_path(*t).exists() for t in shared)           # still needed by B
    assert not any(store.tile_path(*t).exists() for t in only_a)
    assert not store.delete(a["id"]) and not store.delete("../etc")
    assert [p["name"] for p in store.list_packs()] == ["B"]


def test_download_failures_leave_no_partial_pack(tmp_path):
    store = OfflineStore(tmp_path)
    job = run_download(store, finder=FakeFinder(fail=True))
    assert job["state"] == "error" and job["error"]["code"] == "houses_unavailable"
    job = run_download(store, client=tile_client(fail_after=3))
    assert job["state"] == "error" and job["error"]["code"] == "tiles_failed"
    assert store.list_packs() == [] and not any((tmp_path / "packs").iterdir())


def test_start_validation_and_estimate(tmp_path):
    dl = OfflineDownloader(OfflineStore(tmp_path), FakeFinder(), client=tile_client(), tile_delay=0)
    est = dl.estimate(BOX)
    assert est["tiles"] == len(tiles_for_bbox(BOX)) <= MAX_TILES and est["area_km2"] > 0.3 and est["est_mb"] > 0
    with pytest.raises(AppError) as e:
        asyncio.run(dl.start("  ", BOX))
    assert e.value.code == "bad_name"


# ---- fallbacks: online first, offline when unreachable --------------------------------------

def test_router_uses_offline_streets_only_when_service_unreachable(tmp_path):
    store = OfflineStore(tmp_path)
    run_download(store)

    def down(req):
        raise httpx.ConnectError("no internet")

    r = Router(client=httpx.AsyncClient(transport=httpx.MockTransport(down)), min_interval=0, offline=store)
    legs = asyncio.run(r.route([near(1), near(6)], "walk"))
    assert legs[0][0] == near(1) and N[5] in legs[0]
    with pytest.raises(AppError) as e:                                   # uncovered -> clear error + hint
        asyncio.run(r.route([near(1), (45.0, 5.0)], "walk"))
    assert e.value.code == "route_failed" and "Offline tab" in e.value.hint

    def ok(req):                                                         # when online, the online answer wins
        return httpx.Response(200, json={"code": "Ok", "routes": [{"legs": [{"steps": [{"geometry": {"coordinates": [[-75.0, 40.0], [-74.9, 40.1]]}}]}]}]})

    r2 = Router(client=httpx.AsyncClient(transport=httpx.MockTransport(ok)), min_interval=0, offline=store)
    assert (40.1, -74.9) in asyncio.run(r2.route([near(1), near(6)], "walk"))[0]


def test_house_finder_falls_back_to_offline_houses(tmp_path):
    store = OfflineStore(tmp_path)
    run_download(store)
    hf = HouseFinder(client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(504))), min_interval=0, offline=store)
    plan = asyncio.run(hf.plan(40.0005, -74.9995, 300, 5))
    assert len(plan) == 5 and hf.last_source == "offline"
    with pytest.raises(AppError):                                        # outside any pack: original error
        asyncio.run(hf.plan(50, 50, 300, 5))


# ---- HTTP API ------------------------------------------------------------------------------

@pytest.fixture
def api(tmp_path):
    hits = []
    app = create_app(backend=FakeBackend(), router=FakeRouter(), house_finder=FakeFinderForApp(), favorites_path=tmp_path / "f.json",
                     recents_path=tmp_path / "r.json", offline_root=tmp_path / "off", tile_client=tile_client(hits=hits), tile_delay=0)
    with TestClient(app) as c:
        c.hits = hits
        yield c


class FakeFinderForApp(FakeHouses, FakeFinder):
    def __init__(self):
        FakeHouses.__init__(self)
        FakeFinder.__init__(self)


BODY = {"name": "Maple St", "bbox": {"south": BOX[0], "west": BOX[1], "north": BOX[2], "east": BOX[3]}}


def wait_job(c, timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        job = c.get("/api/offline").json()["job"]
        if job["state"] != "running":
            return job
        time.sleep(0.05)
    pytest.fail("download did not finish")


def test_offline_api_full_cycle_and_tile_serving(api):
    est = api.post("/api/offline/estimate", json=BODY).json()
    assert est["tiles"] > 10
    assert api.post("/api/offline", json=BODY).status_code == 200
    job = wait_job(api)
    assert job["state"] == "done"
    listing = api.get("/api/offline").json()
    pack = listing["packs"][0]
    assert pack["name"] == "Maple St" and pack["houses"] == 6
    # a tile inside the pack is served from disk (no extra network request)
    z, x, y = tiles_for_bbox(BOX)[0]
    before = len(api.hits)
    r = api.get(f"/tiles/{z}/{x}/{y}.png")
    assert r.status_code == 200 and r.content == PNG and r.headers["content-type"] == "image/png"
    assert len(api.hits) == before
    # a tile outside the pack is proxied live from OSM
    r = api.get("/tiles/5/3/3.png")
    assert r.status_code == 200 and len(api.hits) == before + 1
    # delete
    assert api.delete(f"/api/offline/{pack['id']}").status_code == 200
    assert api.get("/api/offline").json()["packs"] == []
    assert api.delete(f"/api/offline/{pack['id']}").status_code == 404


def test_offline_api_validation(api):
    big = {**BODY, "bbox": {"south": 30, "west": -80, "north": 41, "east": -70}}
    assert api.post("/api/offline/estimate", json=big).json()["error"]["code"] == "area_too_big"
    assert api.post("/api/offline", json={**BODY, "name": ""}).json()["error"]["code"] == "bad_name"
    assert api.post("/api/offline", json={"name": "x"}).status_code == 422


def test_second_download_while_running_is_rejected(tmp_path):
    slow = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=PNG)))
    app = create_app(backend=FakeBackend(), router=FakeRouter(), house_finder=FakeFinderForApp(), favorites_path=tmp_path / "f.json",
                     recents_path=tmp_path / "r.json", offline_root=tmp_path / "off", tile_client=slow, tile_delay=0.05)
    with TestClient(app) as c:
        assert c.post("/api/offline", json=BODY).status_code == 200
        r = c.post("/api/offline", json=BODY)
        assert r.status_code == 409 and r.json()["error"]["code"] == "busy"
        c.delete("/api/location")  # (no-op) keep the app loop busy a moment
        assert wait_job(c)["state"] == "done"


def test_tile_endpoint_rejects_bad_coordinates_and_handles_offline(tmp_path):
    def down(req):
        raise httpx.ConnectError("no internet")

    app = create_app(backend=FakeBackend(), router=FakeRouter(), house_finder=FakeHouses(), favorites_path=tmp_path / "f.json",
                     recents_path=tmp_path / "r.json", offline_root=tmp_path / "off",
                     tile_client=httpx.AsyncClient(transport=httpx.MockTransport(down)))
    with TestClient(app) as c:
        assert c.get("/tiles/3/9/0.png").status_code == 404       # x out of range
        assert c.get("/tiles/25/0/0.png").status_code == 404
        assert c.get("/tiles/3/1/1.png").status_code == 404       # not saved and no internet
        assert c.get("/tiles/3/../../x.png").status_code in (404, 422)


def make_pack(store, roads, bbox=BOX):
    store.new_pack_dir("gap-test")
    (store.packs_dir / "gap-test" / "roads.json").write_text(json.dumps(roads))
    store.write_meta("gap-test", {"id": "gap-test", "name": "gap", "bbox": list(bbox), "created": 1, "bytes": 1, "houses": 0, "tiles": []})


def test_offline_short_unroutable_legs_become_straight_but_long_ones_fail(tmp_path):
    def two_streets(gap_deg):
        a = way(1, [(1, 40.0000, -75.0000), (2, 40.0000, -74.9990)])
        b = way(2, [(3, 40.0000 + gap_deg, -75.0000), (4, 40.0000 + gap_deg, -74.9990)])   # not connected to street A
        return {"elements": [a, b]}

    store = OfflineStore(tmp_path / "near")
    make_pack(store, two_streets(0.0009))                              # streets ~100 m apart
    a, b = (40.0000, -74.9995), (40.0009, -74.9995)
    (leg,) = store.route([a, b], "walk")
    assert leg == [a, b]                                              # short gap -> straight hop
    store = OfflineStore(tmp_path / "far")
    make_pack(store, two_streets(0.0030))                              # ~330 m apart: too long to guess
    assert store.route([(40.0000, -74.9995), (40.0030, -74.9995)], "walk") is None
