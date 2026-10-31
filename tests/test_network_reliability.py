"""Reliability against flaky networks: retries, partial answers, bad downloads, interrupted writes."""
import asyncio
import errno
import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import FakeBackend, FakeHouses, FakeRouter
from locspoof import offline as offmod
from locspoof.errors import AppError
from locspoof.geocode import Geocoder
from locspoof.offline import OfflineDownloader, OfflineStore, tiles_for_bbox
from locspoof.routing import Router
from locspoof.server import create_app
from locspoof.trickortreat import HouseFinder

PNG = b"\x89PNG\r\n\x1a\nFAKE"
BOX = (39.998, -75.002, 40.004, -74.994)


def run(coro):
    return asyncio.run(coro)


def osrm_ok(req):
    raw = req.url.path.rsplit("/", 1)[1].split(";")
    pts = [tuple(map(float, p.split(","))) for p in raw]
    legs = [{"steps": [{"geometry": {"coordinates": [list(a), list(b)]}}]} for a, b in zip(pts, pts[1:])]
    return httpx.Response(200, json={"code": "Ok", "routes": [{"legs": legs}]})


def flaky(failures, then=osrm_ok, exc=None):
    """Fails `failures` times (HTTP 503, or the given exception), then behaves."""
    state = {"n": 0}

    def handler(req):
        state["n"] += 1
        if state["n"] <= failures:
            if exc:
                raise exc
            return httpx.Response(503)
        return then(req)

    handler.state = state
    return handler


def router(handler, **kw):
    r = Router(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), min_interval=0, **kw)
    r.retry_delays = (0, 0)
    return r


WPS = [(40.0, -75.0), (40.001, -75.0)]


# ---- routing ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("exc", [None, httpx.ConnectError("down"), httpx.ReadTimeout("slow")])
def test_routing_retries_transient_failures(exc):
    h = flaky(2, exc=exc)
    legs = run(router(h).route(WPS, "walk"))
    assert len(legs) == 1 and h.state["n"] == 3                           # two failures, then success


def test_routing_gives_up_with_a_friendly_error_after_bounded_attempts():
    h = flaky(10_000)
    with pytest.raises(AppError) as e:
        run(router(h).route(WPS, "walk"))
    assert e.value.code == "route_failed" and h.state["n"] <= 8


def test_routing_does_not_retry_rate_limits_or_no_route():
    n = {"n": 0}

    def limited(req):
        n["n"] += 1
        return httpx.Response(429)

    with pytest.raises(AppError) as e:
        run(router(limited).route(WPS, "walk"))
    assert e.value.code == "rate_limited" and n["n"] == 1
    n["n"] = 0

    def noroute(req):
        n["n"] += 1
        return httpx.Response(200, json={"code": "NoRoute"})

    with pytest.raises(AppError) as e:
        run(router(noroute).route(WPS, "walk"))
    assert e.value.code == "no_route" and n["n"] == 1


def test_routing_survives_html_instead_of_json():
    h = flaky(1, then=osrm_ok)
    page = lambda req: httpx.Response(200, text="<html>Please log in to the hotel Wi-Fi</html>")
    with pytest.raises(AppError) as e:
        run(router(page).route(WPS, "walk"))
    assert e.value.code == "route_failed"                                 # friendly, not a crash


def test_offline_street_lookup_does_not_block_the_event_loop(tmp_path):
    class SlowStore:
        def route(self, wps, mode):
            time.sleep(0.4)                                               # heavy graph load / A* search
            return [[wps[0], wps[1]]]

    r = Router(client=httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(503))), min_interval=0, offline=SlowStore())
    r.retry_delays = (0, 0)

    async def go():
        ticks = []

        async def ticker():
            for _ in range(30):
                ticks.append(time.monotonic())
                await asyncio.sleep(0.02)

        t = asyncio.create_task(ticker())
        legs = await r.route(WPS, "walk")
        await t
        return legs, ticks

    legs, ticks = run(go())
    assert legs == [[WPS[0], WPS[1]]]
    gaps = [b - a for a, b in zip(ticks, ticks[1:])]
    assert max(gaps) < 0.2                                                # the loop kept running during the 0.4 s search


# ---- Overpass ----------------------------------------------------------------------------------------------

def finder(handler, **kw):
    f = HouseFinder(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), min_interval=0, **kw)
    f.retry_delays = (0, 0)
    return f


GOOD = {"elements": [{"type": "way", "center": {"lat": 40 + i * 0.0003, "lon": -75}} for i in range(6)]}


def test_overpass_partial_answer_is_not_accepted_as_complete():
    calls = []

    def handler(req):
        calls.append(str(req.url))
        if "overpass-api.de" in str(req.url):                             # busy server: 200 but cut short
            return httpx.Response(200, json={"remark": "runtime error: Query timed out in \"query\" at line 1", "elements": GOOD["elements"][:1]})
        return httpx.Response(200, json=GOOD)

    f = finder(handler)
    plan = run(f.plan(40, -75, 300, 5))
    assert len(plan) == 5 and len(calls) == 2 and "kumi" in calls[1]       # fell through to the mirror for the full answer


def test_overpass_retries_both_servers_before_giving_up():
    n = {"n": 0}

    def down(req):
        n["n"] += 1
        return httpx.Response(504)

    with pytest.raises(AppError) as e:
        run(finder(down).plan(40, -75, 300, 5))
    assert e.value.code == "houses_unavailable" and 2 <= n["n"] <= 6
    ok_late = flaky(2, then=lambda req: httpx.Response(200, json=GOOD))
    assert len(run(finder(ok_late).plan(40, -75, 300, 5))) == 5            # succeeds on a later attempt


def test_overpass_bad_json_and_memory_errors_are_handled():
    for body in (httpx.Response(200, text="<html>rate limited</html>"),
                 httpx.Response(200, json={"remark": "runtime error: Query run out of memory", "elements": []})):
        with pytest.raises(AppError) as e:
            run(finder(lambda req, b=body: b).plan(40, -75, 300, 5))
        assert e.value.code == "houses_unavailable"


# ---- place search ------------------------------------------------------------------------------------------

def geo(handler):
    g = Geocoder(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), min_interval=0)
    g.retry_delays = (0,)
    return g


def test_search_retries_once_on_a_server_error():
    h = flaky(1, then=lambda req: httpx.Response(200, json=[{"display_name": "Paris", "lat": "48.85", "lon": "2.35"}]))
    assert run(geo(h).search("paris")) == [{"name": "Paris", "lat": 48.85, "lon": 2.35}] and h.state["n"] == 2


@pytest.mark.parametrize("response", [
    httpx.Response(200, text="<html>captive portal</html>"),
    httpx.Response(200, json=[{"display_name": "x"}]),                    # missing lat/lon
    httpx.Response(200, json={"error": "nope"}),                          # not a list
    httpx.Response(200, json=[{"display_name": "x", "lat": "abc", "lon": "1"}]),
])
def test_search_malformed_answers_become_friendly_errors_not_500s(response):
    with pytest.raises(AppError) as e:
        run(geo(lambda req: response).search("paris"))
    assert e.value.code == "search_failed" and e.value.status == 502


def test_search_endpoint_never_returns_a_bare_500(tmp_path):
    app = create_app(backend=FakeBackend(), router=FakeRouter(), house_finder=FakeHouses(), geocoder=geo(lambda req: httpx.Response(200, text="oops")),
                     favorites_path=tmp_path / "f.json", recents_path=tmp_path / "r.json", offline_root=tmp_path / "o",
                     settings_path=tmp_path / "s.json", routes_path=tmp_path / "rt.json", devices_path=tmp_path / "d.json")
    with TestClient(app) as c:
        r = c.get("/api/search", params={"q": "paris"})
        assert r.status_code == 502 and r.json()["error"]["code"] == "search_failed"


# ---- offline downloads ------------------------------------------------------------------------------------

class FakeFinder:
    def __init__(self, partial=False):
        self.partial = partial

    async def query_json(self, q):
        if '["highway"]' in q:
            return {"elements": []}
        return {"elements": [{"type": "way", "center": {"lat": 40.0, "lon": -75.0}}]}


def tile_client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def download(store, handler, bbox=BOX):
    async def go():
        dl = OfflineDownloader(store, FakeFinder(), client=tile_client(handler), tile_delay=0)
        dl.retry_delay = 0
        await dl.start("Maple", bbox)
        await dl._task
        return dl.job
    return run(go())


def test_a_login_page_is_not_saved_as_a_map_tile(tmp_path):
    store = OfflineStore(tmp_path)
    job = download(store, lambda req: httpx.Response(200, text="<html>Connect to hotel Wi-Fi</html>", headers={"content-type": "text/html"}))
    assert job["state"] == "error" and job["error"]["code"] == "tiles_failed"
    assert list((tmp_path / "tiles").rglob("*.png")) == [] and store.list_packs() == []


def test_a_few_bad_tiles_are_skipped_but_a_good_download_succeeds(tmp_path):
    store = OfflineStore(tmp_path)
    n = {"n": 0}

    def handler(req):
        n["n"] += 1
        return httpx.Response(500) if n["n"] in (3, 4) else httpx.Response(200, content=PNG)

    assert download(store, handler)["state"] == "done"
    saved = list((tmp_path / "tiles").rglob("*.png"))
    assert all(p.read_bytes().startswith(b"\x89PNG") for p in saved) and len(saved) >= len(tiles_for_bbox(BOX)) - 2


def test_interrupted_writes_leave_no_corrupt_tile_and_a_retry_repairs_them(tmp_path):
    store = OfflineStore(tmp_path)
    z, x, y = tiles_for_bbox(BOX)[0]
    path = store.tile_path(z, x, y)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x89PNG\r\n")[:0] if False else path.write_bytes(b"<half written")      # a crash left garbage behind
    assert download(store, lambda req: httpx.Response(200, content=PNG))["state"] == "done"
    assert path.read_bytes() == PNG                                                 # the corrupt file was replaced
    assert not list((tmp_path / "tiles").rglob("*.tmp"))


def test_writes_are_atomic_even_if_the_process_dies_mid_write(tmp_path, monkeypatch):
    store = OfflineStore(tmp_path)
    real_replace = offmod.os.replace

    def dying(src, dst):
        raise KeyboardInterrupt("killed between write and rename")

    monkeypatch.setattr(offmod.os, "replace", dying)
    with pytest.raises(KeyboardInterrupt):
        offmod.atomic_write(store.tile_path(5, 1, 1), PNG)
    assert not store.tile_path(5, 1, 1).exists()                                    # the real file was never touched
    monkeypatch.setattr(offmod.os, "replace", real_replace)
    offmod.atomic_write(store.tile_path(5, 1, 1), PNG)
    assert store.tile_path(5, 1, 1).read_bytes() == PNG


def test_a_full_disk_gives_a_clear_error_and_cleans_up(tmp_path, monkeypatch):
    store = OfflineStore(tmp_path)

    def full(path, data):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(offmod, "atomic_write", full)
    job = download(store, lambda req: httpx.Response(200, content=PNG))
    assert job["state"] == "error" and job["error"]["code"] == "disk_error" and "space" in job["error"]["message"].lower()
    assert store.list_packs() == [] and not any((tmp_path / "packs").iterdir())


def test_a_partial_overpass_answer_never_becomes_a_saved_area(tmp_path):
    class Partial:
        async def query_json(self, q):
            raise AppError("houses_unavailable", "Overpass timed out", status=502)

    store = OfflineStore(tmp_path)

    async def go():
        dl = OfflineDownloader(store, Partial(), client=tile_client(lambda r: httpx.Response(200, content=PNG)), tile_delay=0)
        await dl.start("Maple", BOX)
        await dl._task
        return dl.job

    assert run(go())["state"] == "error" and store.list_packs() == []


def test_startup_cleans_up_leftovers_of_an_interrupted_download(tmp_path):
    (tmp_path / "packs" / "half-done-abc123").mkdir(parents=True)
    (tmp_path / "packs" / "half-done-abc123" / "roads.json").write_text("{")
    (tmp_path / "tiles" / "12" / "1").mkdir(parents=True)
    (tmp_path / "tiles" / "12" / "1" / "2.png.tmp").write_bytes(b"x")
    done = tmp_path / "packs" / "good-1"
    done.mkdir()
    (done / "pack.json").write_text(json.dumps({"id": "good-1", "name": "g", "bbox": list(BOX), "created": 1, "bytes": 1, "houses": 0, "tiles": []}))
    store = OfflineStore(tmp_path)
    assert not (tmp_path / "packs" / "half-done-abc123").exists() and not list((tmp_path / "tiles").rglob("*.tmp"))
    assert [p["id"] for p in store.list_packs()] == ["good-1"]                      # complete areas are untouched


def test_tile_endpoint_repairs_a_corrupt_saved_tile_by_refetching(tmp_path):
    live = {"n": 0}

    def handler(req):
        live["n"] += 1
        return httpx.Response(200, content=PNG)

    app = create_app(backend=FakeBackend(), router=FakeRouter(), house_finder=FakeHouses(), favorites_path=tmp_path / "f.json",
                     recents_path=tmp_path / "r.json", offline_root=tmp_path / "o", settings_path=tmp_path / "s.json",
                     routes_path=tmp_path / "rt.json", devices_path=tmp_path / "d.json", tile_client=tile_client(handler))
    with TestClient(app) as c:
        p = OfflineStore(tmp_path / "o").tile_path(5, 3, 3)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"corrupt")
        r = c.get("/tiles/5/3/3.png")
        assert r.status_code == 200 and r.content == PNG and live["n"] == 1            # served from the live source instead
        assert not p.exists()                                                           # and the bad copy was removed
