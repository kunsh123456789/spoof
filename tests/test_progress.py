"""Backend for the always-on progress bar and the activity timeline."""
import random
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import FakeBackend, FakeHouses, FakeRouter
from locspoof.activity import ActivityLog
from locspoof.geocode import Geocoder, short_label
from locspoof.movement import haversine_m
from locspoof.server import create_app


def make(tmp_path, geocoder=None, **kw):
    backend = kw.pop("backend", None) or FakeBackend()
    app = create_app(backend=backend, router=FakeRouter(), house_finder=FakeHouses(), geocoder=geocoder, favorites_path=tmp_path / "f.json",
                     recents_path=tmp_path / "r.json", offline_root=tmp_path / "o", settings_path=tmp_path / "s.json",
                     routes_path=tmp_path / "rt.json", devices_path=tmp_path / "d.json", schedules_path=tmp_path / "sch.json",
                     activity_path=tmp_path / "act.json", walk_tick=0.02, ambient_tick=0.5, rng=random.Random(1), **kw)
    return app, backend


@pytest.fixture
def env(tmp_path):
    app, backend = make(tmp_path)
    with TestClient(app) as c:
        c.backend, c.dir = backend, tmp_path
        yield c


def stop(lat, lon, dwell=0, label=None):
    return {"lat": lat, "lon": lon, "dwell_s": dwell, "label": label}


def walk(c):
    return c.get("/api/status").json()["walk"]


def collect(c, until_done=True, timeout=20):
    """Poll the status until the trip ends; return every walk snapshot."""
    seen, end = [], time.time() + timeout
    while time.time() < end:
        w = walk(c)
        seen.append(w)
        if not w["active"]:
            return seen
        time.sleep(0.02)
    pytest.fail("trip did not finish")


# ---- progress ---------------------------------------------------------------------------------------------

def test_a_trip_reports_total_progress_destination_and_eta(env):
    c = env
    c.post("/api/location", json={"lat": 0, "lon": 0, "label": "Start cafe"})
    c.post("/api/journey", json={"stops": [stop(0, 0.001, label="Library")], "mode": "straight", "speed_kmh": 100})
    snaps = [s for s in collect(c) if s.get("total_m")]
    expected = haversine_m(0, 0, 0, 0.001)
    assert snaps[0]["total_m"] == pytest.approx(expected, rel=0.02) and snaps[0]["dest"] == "Library" and snaps[0]["cur_mode"] == "straight"
    prog = [s["progress"] for s in snaps if s.get("progress") is not None]
    assert prog == sorted(prog) and prog[0] < 0.5 < prog[-1] <= 1.0                        # moves steadily forward
    etas = [s["eta_s"] for s in snaps if s.get("eta_s") is not None and s["state"] == "moving"]
    assert etas[0] > etas[-1] >= 0 and etas[0] == pytest.approx(expected / (100 / 3.6), rel=0.25)   # ~ distance / speed
    done = walk(c)
    assert done["state"] == "done" and done["progress"] == 1.0 and done["eta_s"] == 0 and done["done_m"] == pytest.approx(done["total_m"], rel=0.01)


def test_progress_and_eta_account_for_pauses_at_stops(env):
    c = env
    c.post("/api/location", json={"lat": 0, "lon": 0})
    c.post("/api/journey", json={"stops": [stop(0, 0.0004, 3, "A"), stop(0, 0.0008, 0, "B")], "mode": "straight", "speed_kmh": 100})
    time.sleep(0.15)
    first = walk(c)
    assert first["eta_s"] >= 3                                                              # the 3 s pause is still to come
    snaps = collect(c)
    waiting = [s for s in snaps if s["state"] == "waiting"]
    assert waiting and all(s["dest"] == "A" and 0 < s["progress"] < 1 for s in waiting)    # paused at A, half way overall
    prog = [s["progress"] for s in snaps if s.get("progress") is not None]
    assert prog == sorted(prog)
    left = [s["eta_s"] for s in waiting if s["eta_s"] is not None]
    assert left == sorted(left, reverse=True) and left[-1] < left[0]                       # the countdown ticks down while waiting


def test_the_destination_changes_leg_by_leg_like_trick_or_treat_houses(env):
    c = env
    c.post("/api/location", json={"lat": 0, "lon": 0})
    stops = [stop(0, 0.0002 * (i + 1), 0, "house %d" % (i + 1)) for i in range(4)]
    c.post("/api/journey", json={"stops": stops, "mode": "straight", "speed_kmh": 100})
    dests = []
    for s in collect(c):
        if s.get("dest") and (not dests or dests[-1] != s["dest"]):
            dests.append(s["dest"])
    assert dests == ["house 1", "house 2", "house 3", "house 4"]
    assert walk(c)["total"] == 4


def test_a_return_trip_counts_the_way_back_and_names_it(env):
    c = env
    c.post("/api/location", json={"lat": 0, "lon": 0})
    one_way = haversine_m(0, 0, 0, 0.0004)
    c.post("/api/journey", json={"stops": [stop(0, 0.0004, 0, "Shop")], "mode": "straight", "speed_kmh": 100, "return_to_start": True})
    snaps = collect(c)
    assert snaps[0]["total_m"] == pytest.approx(2 * one_way, rel=0.03)
    assert [s["dest"] for s in snaps if s.get("returning")][0] == "the start" and snaps[-1]["state"] == "done"


def test_a_looping_trip_has_no_total_or_eta(env):
    c = env
    c.post("/api/location", json={"lat": 0, "lon": 0})
    c.post("/api/journey", json={"stops": [stop(0, 0.0003, 0, "A"), stop(0, 0.0006, 0, "B")], "mode": "straight", "speed_kmh": 100, "loop": True})
    time.sleep(0.4)
    w = walk(c)
    assert w["loop"] is True and w["total_m"] is None and w["progress"] is None and w["eta_s"] is None and w["dest"]
    c.delete("/api/journey")


def test_a_gpx_trip_is_named_after_the_track(env):
    c = env
    track = [[10.0, 10.0], [10.0, 10.0004], [10.0004, 10.0004]]
    c.post("/api/journey", json={"stops": [], "path": track, "mode": "straight", "speed_kmh": 100, "jump_to_first": True, "title": "Park loop"})
    snaps = collect(c)
    assert snaps[0]["dest"] == "Park loop" and snaps[0]["total_m"] == pytest.approx(haversine_m(10, 10, 10, 10.0004) + haversine_m(10, 10.0004, 10.0004, 10.0004), rel=0.03)
    assert c.get("/api/status").json()["simulation"]["label"] == "Park loop"                 # the jump to the start was labelled too


def test_the_spot_is_labelled_while_standing_and_unlabelled_in_transit(env):
    c = env
    before = time.time()
    c.post("/api/location", json={"lat": 1, "lon": 2, "label": "12 Maple Street"})
    sim = c.get("/api/status").json()["simulation"]
    assert sim["label"] == "12 Maple Street" and before - 1 <= sim["since"] <= time.time() + 1
    c.post("/api/journey", json={"stops": [stop(1, 2.0006, 0, "Home")], "mode": "straight", "speed_kmh": 30})
    time.sleep(0.15)
    assert "label" not in c.get("/api/status").json()["simulation"]                          # on the move: no fixed place
    collect(c)
    assert c.get("/api/status").json()["simulation"]["label"] == "Home"                       # arrived: now it is "at Home"
    c.delete("/api/location")
    assert c.get("/api/status").json()["simulation"] == {"active": False}
    c.post("/api/location", json={"lat": 1, "lon": 2})
    assert "label" not in c.get("/api/status").json()["simulation"]                          # no label given: coordinates are shown instead


# ---- reverse geocoding --------------------------------------------------------------------------------------------------

REV = {"display_name": "12, Maple Street, Springfield, Greene County, Missouri, 65801, United States",
       "address": {"house_number": "12", "road": "Maple Street", "city": "Springfield", "state": "Missouri"}}


def geo(handler):
    g = Geocoder(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), min_interval=0)
    g.retry_delays = (0,)
    return g


def test_short_labels_read_like_an_address_not_a_paragraph():
    assert short_label(REV) == "12 Maple Street, Springfield"
    assert short_label({"address": {"road": "Rue de Rivoli", "town": "Paris"}}) == "Rue de Rivoli, Paris"
    assert short_label({"address": {"amenity": "Cafe Luna", "village": "Bray"}}) == "Cafe Luna, Bray"
    assert short_label({"display_name": "Somewhere, Far, Away, Land", "address": {}}) == "Somewhere, Far"
    assert short_label({}) == ""


def test_reverse_endpoint_returns_a_label_and_caches(tmp_path):
    calls = []

    def handler(req):
        calls.append(dict(req.url.params))
        return httpx.Response(200, json=REV)

    app, _ = make(tmp_path, geocoder=geo(handler))
    with TestClient(app) as c:
        r = c.get("/api/reverse", params={"lat": 37.20003, "lon": -93.29999}).json()
        assert r["label"] == "12 Maple Street, Springfield" and r["name"].startswith("12, Maple Street")
        c.get("/api/reverse", params={"lat": 37.20004, "lon": -93.29998})                      # a few metres away: same cache entry
        assert len(calls) == 1 and calls[0]["format"] == "jsonv2" and calls[0]["zoom"] == "18"


def test_reverse_endpoint_failures_are_friendly(tmp_path):
    for handler, status, code in (
        (lambda r: httpx.Response(200, json={"error": "Unable to geocode"}), 404, "not_found"),
        (lambda r: httpx.Response(200, text="<html>login</html>"), 502, "search_failed"),
        (lambda r: httpx.Response(429), 429, "rate_limited"),
        (lambda r: (_ for _ in ()).throw(httpx.ConnectError("down")), 502, "search_offline"),
    ):
        app, _ = make(tmp_path, geocoder=geo(handler))
        with TestClient(app) as c:
            r = c.get("/api/reverse", params={"lat": 1, "lon": 2})
            assert r.status_code == status and r.json()["error"]["code"] == code
    app, _ = make(tmp_path, geocoder=geo(lambda r: httpx.Response(200, json=REV)))
    with TestClient(app) as c:
        assert c.get("/api/reverse", params={"lat": 95, "lon": 2}).status_code == 422
        assert c.get("/api/reverse", params={"lat": "x", "lon": 2}).status_code == 422


# ---- the activity log -----------------------------------------------------------------------------------------------------

def events(c, after=0):
    return c.get("/api/activity", params={"after": after}).json()


def texts(c):
    return [e["text"] for e in events(c)["events"]]


def test_the_timeline_records_what_happened_in_order(env):
    c = env
    c.get("/api/status")
    c.post("/api/location", json={"lat": 0, "lon": 0, "label": "Home"})
    c.post("/api/journey", json={"stops": [stop(0, 0.0004, 0, "Shop")], "mode": "straight", "speed_kmh": 100})
    collect(c)
    c.delete("/api/location")
    t = texts(c)
    assert t[0].startswith("iPhone connected") and "Location set to Home" in t[1]
    assert any(x.startswith("Trip started") and "Shop" in x and "100 km/h" in x for x in t)
    assert any(x.startswith("Trip finished") and "Shop" in x for x in t) and t[-1] == "Real GPS restored"
    ev = events(c)
    assert [e["id"] for e in ev["events"]] == sorted(e["id"] for e in ev["events"]) and ev["last_id"] == ev["events"][-1]["id"]
    assert ev["events"][1]["kind"] == "location" and ev["events"][1]["phone"] == "Test iPhone" and isinstance(ev["events"][1]["ts"], float)


def test_events_can_be_fetched_incrementally_and_cleared(env):
    c = env
    c.post("/api/location", json={"lat": 0, "lon": 0})
    first = events(c)
    c.post("/api/location", json={"lat": 1, "lon": 1})
    assert len(events(c, after=first["last_id"])["events"]) == 1
    assert events(c, after=10_000)["events"] == []
    assert c.delete("/api/activity").status_code == 200 and events(c)["events"] == []


def test_stopping_and_errors_and_reconnects_are_logged(env):
    c = env
    c.manager = c.app.state.manager
    c.manager.reconnect_poll = 0.02
    c.post("/api/location", json={"lat": 0, "lon": 0})
    c.post("/api/journey", json={"stops": [stop(0, 0.01, 0, "Far")], "mode": "straight", "speed_kmh": 100})
    time.sleep(0.2)
    c.backend.unplug()
    for _ in range(100):
        if walk(c).get("state") == "reconnecting":
            break
        time.sleep(0.03)
    c.backend.replug()
    time.sleep(0.5)
    c.delete("/api/journey")
    t = texts(c)
    assert any("Lost the connection" in x for x in t) and any("reconnected" in x for x in t) and "Trip stopped" in t, t
    lost = [e for e in events(c)["events"] if "Lost the connection" in e["text"]][0]
    assert lost["level"] == "warn" and lost["kind"] == "link"


def test_a_failing_trip_is_logged_as_an_error(env):
    c = env
    c.post("/api/location", json={"lat": 0, "lon": 0})
    c.patch("/api/settings", json={"auto_reconnect": False})
    c.post("/api/journey", json={"stops": [stop(0, 0.01)], "mode": "straight", "speed_kmh": 100})
    time.sleep(0.1)
    c.backend.sessions[0].broken = True
    c.backend.problem = "no_device"
    for _ in range(100):
        if any("stopped by an error" in x for x in texts(c)) or not walk(c)["active"]:
            break
        time.sleep(0.03)
    assert any(e["level"] == "error" for e in events(c)["events"]) or any("not ready" in x for x in texts(c))


def test_the_log_survives_a_restart_trims_itself_and_ignores_a_corrupt_file(tmp_path):
    log = ActivityLog(tmp_path / "a.json", limit=5)
    for i in range(8):
        log.add(f"event {i}")
    assert [e["text"] for e in log.list()] == [f"event {i}" for i in range(3, 8)]
    again = ActivityLog(tmp_path / "a.json", limit=5)
    assert [e["text"] for e in again.list()][-1] == "event 7" and again.add("next")["id"] == 9      # ids keep counting
    (tmp_path / "bad.json").write_text("{nope")
    assert ActivityLog(tmp_path / "bad.json").list() == []
    assert ActivityLog().add("memory only", level="weird")["level"] == "info"                       # unknown levels are normalised
    assert len(ActivityLog(limit=3).list()) == 0


def test_scheduled_steps_and_mac_sleep_appear_in_the_timeline(tmp_path):
    from datetime import datetime
    base = datetime.fromisoformat("2026-10-31T19:59:50").timestamp()
    clock = {"t": base}
    app, backend = make(tmp_path, schedule_clock=lambda: clock["t"], schedule_tick=0.03)
    with TestClient(app) as c:
        pid = c.post("/api/schedules", json={"name": "Night", "steps": [{"at": "2026-10-31T20:00", "type": "set_location", "title": "Go home",
                                                                       "params": {"lat": 1, "lon": 2, "label": "Home"}}]}).json()["id"]
        c.post(f"/api/schedules/{pid}/arm", json={"armed": True})
        clock["t"] = datetime.fromisoformat("2026-10-31T20:00:01").timestamp()
        for _ in range(100):
            if any("done" in x for x in texts(c)):
                break
            time.sleep(0.05)
        t = texts(c)
        assert any(x.startswith("⏰ Night · Go home: started") for x in t) and any("Go home: done" in x and "Location set to Home" in x for x in t)
