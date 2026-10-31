import time

import httpx
import pytest
from fastapi.testclient import TestClient

from locspoof.geocode import Geocoder
from locspoof.server import create_app
from conftest import FakeBackend, FakeHouses, FakeRouter


@pytest.fixture
def make_client(tmp_path):
    def make(backend, **kw):
        kw.setdefault("router", FakeRouter())
        kw.setdefault("house_finder", FakeHouses())
        app = create_app(backend=backend, favorites_path=tmp_path / "fav.json", recents_path=tmp_path / "recent.json",
                         walk_tick=0.01, **kw)
        return TestClient(app)
    return make


def test_status_ready(make_client, backend):
    with make_client(backend) as c:
        s = c.get("/api/status").json()
    assert s["state"] == "ready" and s["device"]["name"] == "Test iPhone"
    assert s["device"]["ios_version"] == "17.4" and "dvt" in s["device"]["method"]
    assert s["simulation"] == {"active": False}


@pytest.mark.parametrize("problem,code", [("no_device", "no_device"), ("not_trusted", "not_trusted"),
                                           ("developer_mode_off", "developer_mode_off")])
def test_status_errors_are_friendly(make_client, problem, code):
    with make_client(FakeBackend(problem=problem)) as c:
        s = c.get("/api/status").json()
        assert s["state"] == code and s["device"] is None
        assert s["error"]["message"] and s["error"]["hint"]
        r = c.post("/api/location", json={"lat": 1, "lon": 2})
        assert r.status_code in (409, 503) and r.json()["error"]["code"] == code


def test_trust_flow(make_client):
    b = FakeBackend(problem="not_trusted")
    with make_client(b) as c:
        assert c.get("/api/status").json()["state"] == "not_trusted"
        assert c.post("/api/trust").json()["state"] == "ready"
    assert b.trusted == 1


def test_set_and_clear_ios17(make_client, backend):
    with make_client(backend) as c:
        r = c.post("/api/location", json={"lat": 48.85837, "lon": 2.294481})
        assert r.status_code == 200 and r.json()["simulation"]["lat"] == 48.85837
        sess = backend.sessions[0]
        assert sess.calls == [("set", 48.85837, 2.294481)] and not sess.closed  # DVT session stays open
        c.post("/api/location", json={"lat": 1, "lon": 2})
        assert len(backend.sessions) == 1  # reused
        assert c.delete("/api/location").json()["simulation"] == {"active": False}
        assert sess.calls[-1] == ("clear",) and sess.closed
    # clearing with nothing active on iOS 17+ is a no-op, not an error


def test_clear_when_idle_ios17_noop(make_client, backend):
    with make_client(backend) as c:
        c.get("/api/status")  # learns iOS version
        assert c.delete("/api/location").status_code == 200
    assert backend.sessions == []


def test_legacy_clear_reaches_phone_even_without_prior_session(make_client):
    b = FakeBackend(version="15.7")
    with make_client(b) as c:
        assert c.delete("/api/location").status_code == 200
    assert b.sessions[0].calls == [("clear",)]


def test_shutdown_closes_dvt_session(make_client, backend):
    with make_client(backend) as c:
        c.post("/api/location", json={"lat": 1, "lon": 2})
    assert backend.sessions[0].closed


def test_reconnects_once_on_stale_session(make_client, backend):
    with make_client(backend) as c:
        c.post("/api/location", json={"lat": 1, "lon": 2})
        backend.sessions[0].fail_next = True
        assert c.post("/api/location", json={"lat": 3, "lon": 4}).status_code == 200
    assert len(backend.sessions) == 2 and backend.sessions[1].calls == [("set", 3, 4)]


def test_device_unplugged_resets_dvt_state(make_client, backend):
    with make_client(backend) as c:
        c.post("/api/location", json={"lat": 1, "lon": 2})
        backend.problem = "no_device"
        s = c.get("/api/status?x=1")
        time.sleep(2.1)  # status cache
        s = c.get("/api/status").json()
        assert s["state"] == "no_device" and s["simulation"] == {"active": False}


@pytest.mark.parametrize("body", [{"lat": 91, "lon": 0}, {"lat": 0, "lon": 200}, {"lat": "x", "lon": 0}, {}])
def test_bad_coordinates_rejected(make_client, backend, body):
    with make_client(backend) as c:
        assert c.post("/api/location", json=body).status_code == 422
    assert backend.sessions == []


def test_favorites_crud(make_client, backend, tmp_path):
    with make_client(backend) as c:
        assert c.get("/api/favorites").json() == {"favorites": []}
        f = c.post("/api/favorites", json={"name": " Home ", "lat": 1.5, "lon": 2.5}).json()
        assert f["name"] == "Home"
        assert c.post("/api/favorites", json={"name": "  ", "lat": 1, "lon": 2}).status_code == 422
        assert c.post("/api/favorites", json={"name": "x", "lat": 100, "lon": 2}).status_code == 422
        assert len(c.get("/api/favorites").json()["favorites"]) == 1
        assert c.delete(f"/api/favorites/{f['id']}").status_code == 200
        assert c.delete(f"/api/favorites/{f['id']}").status_code == 404
    assert (tmp_path / "fav.json").exists()


def test_corrupt_favorites_file_recovers(make_client, backend, tmp_path):
    (tmp_path / "fav.json").write_text("{not json")
    with make_client(backend) as c:
        assert c.get("/api/favorites").json() == {"favorites": []}
    assert (tmp_path / "fav.corrupt.json").exists()


def test_search_uses_user_agent_caches_and_rate_limits():
    seen = []

    def handler(req: httpx.Request):
        seen.append((time.monotonic(), req))
        return httpx.Response(200, json=[{"display_name": "Eiffel Tower, Paris", "lat": "48.8582", "lon": "2.2945"}])

    geo = Geocoder(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), min_interval=0.3)
    app = create_app(backend=FakeBackend(), geocoder=geo)
    with TestClient(app) as c:
        r = c.get("/api/search", params={"q": "Eiffel Tower"}).json()["results"]
        assert r == [{"name": "Eiffel Tower, Paris", "lat": 48.8582, "lon": 2.2945}]
        c.get("/api/search", params={"q": "eiffel   tower"})  # cached
        c.get("/api/search", params={"q": "Louvre"})
        assert c.get("/api/search", params={"q": "a"}).status_code == 422
    assert len(seen) == 2
    assert "User-Agent" in seen[0][1].headers and "iphone" in seen[0][1].headers["user-agent"].lower()
    assert seen[1][0] - seen[0][0] >= 0.29
    assert seen[0][1].url.host == "nominatim.openstreetmap.org"


def test_search_offline_and_429():
    def boom(req):
        raise httpx.ConnectError("down")

    geo = Geocoder(client=httpx.AsyncClient(transport=httpx.MockTransport(boom)), min_interval=0)
    with TestClient(create_app(backend=FakeBackend(), geocoder=geo)) as c:
        r = c.get("/api/search", params={"q": "paris"})
        assert r.status_code == 502 and r.json()["error"]["code"] == "search_offline"
    geo = Geocoder(client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(429))), min_interval=0)
    with TestClient(create_app(backend=FakeBackend(), geocoder=geo)) as c:
        assert c.get("/api/search", params={"q": "paris"}).status_code == 429


def test_index_and_static_served(make_client, backend):
    with make_client(backend) as c:
        assert "leaflet" in c.get("/").text.lower()
        assert c.get("/static/app.js").status_code == 200
