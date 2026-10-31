"""The phone remote: pairing, the network guard, and the Mac-side management."""
import random
import time

import pytest
from fastapi.testclient import TestClient

from conftest import FakeBackend, FakeHouses, FakeRouter
from locspoof.remote import RemoteStore, check_request
from locspoof.server import create_app


class FakeListener:
    def __init__(self):
        self.running, self.port, self.thread, self.error, self.calls = False, 0, None, None, []

    def start(self, port=None):
        self.running, self.port, self.thread = True, port or self.port, object()
        self.calls.append(("start", port))

    def stop(self):
        self.running, self.thread = False, None
        self.calls.append(("stop",))


def make(tmp_path, listener=None):
    return create_app(backend=FakeBackend(), router=FakeRouter(), house_finder=FakeHouses(), favorites_path=tmp_path / "f.json",
                      recents_path=tmp_path / "r.json", offline_root=tmp_path / "o", settings_path=tmp_path / "s.json",
                      routes_path=tmp_path / "rt.json", devices_path=tmp_path / "d.json", schedules_path=tmp_path / "sch.json",
                      activity_path=tmp_path / "act.json", panic_path=tmp_path / "p.json", remote_path=tmp_path / "rem.json",
                      remote_listener=listener or FakeListener(), walk_tick=0.02, ambient_tick=0.5, rng=random.Random(1))


@pytest.fixture
def mac(tmp_path):
    listener = FakeListener()
    with TestClient(make(tmp_path, listener)) as c:
        c.listener = listener
        c.app_ = c.app
        c.tmp = tmp_path
        yield c


def phone(mac):
    """A client that looks like it comes from another device on the Wi-Fi."""
    return TestClient(mac.app, client=("192.168.1.50", 5000))


def pair(mac, p, name="Kunsh's iPhone"):
    mac.patch("/api/settings", json={"remote_enabled": True})
    code = mac.post("/api/remote/code").json()["code"]
    r = p.post("/api/remote/pair", json={"code": code, "name": name})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["token"]}


# ---- off by default ----
def test_the_remote_is_off_by_default_and_the_network_is_refused(mac):
    assert mac.get("/api/settings").json()["remote_enabled"] is False and mac.listener.calls == []
    p = phone(mac)
    for path in ("/remote", "/api/status", "/api/remote/pair"):
        assert p.get(path).status_code == 403 and p.get(path).json()["error"]["code"] == "remote_off"
    assert mac.get("/api/status").status_code == 200                        # this Mac is unaffected


def test_switching_it_on_and_off_opens_and_closes_the_listener(mac):
    mac.patch("/api/settings", json={"remote_enabled": True, "remote_port": 9123})
    assert mac.listener.running and mac.listener.port == 9123
    mac.patch("/api/settings", json={"remote_enabled": False})
    assert not mac.listener.running
    assert mac.patch("/api/settings", json={"remote_port": 80}).status_code == 422      # privileged / invalid ports are refused


# ---- pairing ----
def test_pairing_needs_the_code_shown_on_the_mac(mac):
    mac.patch("/api/settings", json={"remote_enabled": True})
    p = phone(mac)
    assert p.get("/remote").status_code == 200 and p.get("/remote.webmanifest").status_code == 200         # the page loads without a token
    assert p.post("/api/remote/pair", json={"code": "123456", "name": "x"}).json()["error"]["code"] == "no_code"
    st = mac.get("/api/remote").json()
    assert st["enabled"] and st["code"] is None and st["devices"] == [] and st["port"] == 8787
    code = mac.post("/api/remote/code").json()["code"]
    assert len(code) == 6 and code.isdigit() and mac.get("/api/remote").json()["code"]["code"] == code
    wrong = "000000" if code != "000000" else "111111"
    r = p.post("/api/remote/pair", json={"code": wrong, "name": "x"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "bad_code"
    ok = p.post("/api/remote/pair", json={"code": code, "name": "  Kunsh's   iPhone "})
    assert ok.status_code == 200 and len(ok.json()["token"]) > 30
    assert p.post("/api/remote/pair", json={"code": code, "name": "again"}).json()["error"]["code"] == "no_code"      # single use
    assert [d["name"] for d in mac.get("/api/remote").json()["devices"]] == ["Kunsh's iPhone"]
    assert "token" not in str(mac.get("/api/remote").json()) and "hash" not in str(mac.get("/api/remote").json())


def test_guessing_the_code_locks_pairing(mac):
    mac.patch("/api/settings", json={"remote_enabled": True})
    code = mac.post("/api/remote/code").json()["code"]
    wrong = "000000" if code != "000000" else "111111"
    p = phone(mac)
    codes = [p.post("/api/remote/pair", json={"code": wrong, "name": "x"}).json()["error"]["code"] for _ in range(6)]
    assert codes == ["bad_code"] * 4 + ["pair_locked"] * 2
    assert p.post("/api/remote/pair", json={"code": code, "name": "x"}).status_code == 429           # even the right code, while locked


def test_codes_expire():
    clock = {"t": 1000.0}
    store = RemoteStore(__import__("pathlib").Path("/nonexistent/x.json"), clock=lambda: clock["t"])
    code = store.new_code()["code"]
    clock["t"] += 601
    assert store.current_code() is None
    with pytest.raises(Exception) as e:
        store.pair(code, "x")
    assert "no_code" in str(getattr(e.value, "code", e.value))


# ---- what a paired phone may do ----
def test_a_paired_phone_can_control_but_not_administer(mac):
    p = phone(mac)
    h = pair(mac, p)
    assert p.get("/api/status").status_code == 401                          # no token
    assert p.get("/api/remote/me", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert p.get("/api/remote/me", headers=h).status_code == 200
    assert p.post("/api/location", json={"lat": 37.0, "lon": -120.0}, headers=h).status_code == 200
    assert p.get("/api/status", headers=h).json()["simulation"]["active"]
    for method, path, body in [("GET", "/api/favorites", None), ("GET", "/api/recent", None), ("GET", "/api/devices", None),
                               ("POST", "/api/journey", {"stops": [{"lat": 37.001, "lon": -120.0}], "mode": "straight", "speed_kmh": 5}),
                               ("DELETE", "/api/journey", None), ("DELETE", "/api/location", None)]:
        assert p.request(method, path, json=body, headers=h).status_code == 200, (method, path)
    # everything else is Mac-only
    for method, path in [("GET", "/api/settings"), ("PATCH", "/api/settings"), ("GET", "/api/remote"), ("POST", "/api/remote/code"),
                         ("DELETE", "/api/remote/devices/abc"), ("DELETE", "/api/activity"), ("POST", "/api/schedules"), ("PUT", "/api/schedules/x"), ("DELETE", "/api/schedules/x"), ("POST", "/api/schedules/x/reset"),
                         ("POST", "/api/offline"), ("PUT", "/api/panic"), ("POST", "/api/devices/scan"), ("DELETE", "/api/favorites/x"), ("POST", "/api/wifi")]:
        r = p.request(method, path, json={}, headers=h)
        assert r.status_code == 403 and r.json()["error"]["code"] == "forbidden", (method, path, r.status_code)
    assert mac.get("/api/settings").status_code == 200                       # the Mac itself can still do all of it


def test_tiles_accept_the_token_in_the_url(mac):
    p = phone(mac)
    h = pair(mac, p)
    token = h["Authorization"][7:]
    assert p.get("/tiles/5/5/12.png").status_code == 401
    assert p.get(f"/tiles/5/5/12.png?t=wrong").status_code == 401
    assert p.get(f"/tiles/5/5/12.png?t={token}").status_code != 401
    assert p.get(f"/api/status?t={token}").status_code == 401                # the query token is for tiles only


def test_revoking_a_phone_cuts_it_off_and_tokens_survive_a_restart(mac):
    p = phone(mac)
    h = pair(mac, p)
    assert p.get("/api/remote/me", headers=h).status_code == 200
    with TestClient(make(mac.tmp), client=("192.168.1.50", 5000)) as again:           # the app restarts: still paired
        again.app.state.remote  # noqa
        assert again.get("/api/remote/me", headers=h).status_code == 200
    dev = mac.get("/api/remote").json()["devices"][0]["id"]
    assert mac.delete(f"/api/remote/devices/{dev}").status_code == 200 and mac.delete(f"/api/remote/devices/{dev}").status_code == 404
    assert p.get("/api/remote/me", headers=h).status_code == 401
    assert "Bearer" not in (mac.tmp / "rem.json").read_text()


def test_only_a_hash_of_the_token_is_stored(mac):
    p = phone(mac)
    h = pair(mac, p)
    assert h["Authorization"][7:] not in (mac.tmp / "rem.json").read_text()


def test_check_request_pure(tmp_path):
    s = RemoteStore(tmp_path / "x.json")
    assert check_request(s, "GET", "/remote", "", "") is None
    assert check_request(s, "GET", "/api/status", "", "")[0] == 401
    assert check_request(s, "GET", "/static/app.js", "", "")[0] == 401
    assert check_request(s, "POST", "/api/remote/step", "", "")[0] == 401           # the full Mac UI is not served to the network


# ---- reaching the Mac from anywhere (Tailscale) ----
def test_tailscale_detection_parsers():
    from locspoof.remote import parse_ifconfig, parse_tailscale_status
    text = "lo0: inet 127.0.0.1 netmask 0xff\nen0: inet 192.168.1.5 netmask 0xffffff00\nutun4: inet 100.101.102.103 --> 100.101.102.103 netmask 0xffffffff\nen9: inet 100.200.1.1"
    assert parse_ifconfig(text) == ["100.101.102.103"]                                   # 100.200.x is not Tailscale's range
    st = parse_tailscale_status('{"BackendState": "Running", "Self": {"DNSName": "macbook.tail1234.ts.net.", "TailscaleIPs": ["100.1.2.3", "fd7a::1"]}}')
    assert st == {"running": True, "dns": "macbook.tail1234.ts.net", "ips": ["100.1.2.3"]}
    assert parse_tailscale_status("garbage") == {"running": False, "dns": None, "ips": []}
    assert parse_tailscale_status('{"BackendState": "Stopped"}')["running"] is False


def test_the_mac_lists_tailscale_addresses_first_and_a_tailscale_client_still_needs_pairing(mac, monkeypatch):
    import locspoof.server as server
    monkeypatch.setattr(server, "tailscale_info", lambda: {"installed": True, "running": True, "dns": "macbook.tail1234.ts.net", "ips": ["100.1.2.3"]})
    mac.patch("/api/settings", json={"remote_enabled": True})
    r = mac.get("/api/remote").json()
    assert [u["kind"] for u in r["urls"]][:2] == ["tailscale", "tailscale"] and r["urls"][0]["url"] == "http://macbook.tail1234.ts.net:8787/remote"
    assert r["tailscale"] == {"installed": True, "running": True}
    far = TestClient(mac.app, client=("100.64.9.9", 4000))                              # a phone arriving over Tailscale
    assert far.get("/api/status").status_code == 401 and far.get("/remote").status_code == 200
    h = pair(mac, far)
    assert far.get("/api/status", headers=h).status_code == 200 and far.get("/api/settings", headers=h).status_code == 403


# ---- telling the phones apart ----
def test_paired_phones_show_who_is_online_where_and_can_be_renamed(mac):
    import locspoof.remote as rm
    a, b = phone(mac), TestClient(mac.app, client=("100.100.5.5", 4000))
    ha = pair(mac, a, "Kunsh's iPad")
    hb = pair(mac, b, "Remote iPhone")
    a.get("/api/remote/me", headers=ha)
    b.get("/api/remote/me", headers=hb)
    devs = {d["name"]: d for d in mac.get("/api/remote").json()["devices"]}
    assert devs["Kunsh's iPad"]["online"] and devs["Kunsh's iPad"]["via"] == "wifi" and devs["Kunsh's iPad"]["ip"] == "192.168.1.50"
    assert devs["Remote iPhone"]["online"] and devs["Remote iPhone"]["via"] == "tailscale"
    st = mac.app.state                                              # time passes with no requests: shown as not connected
    store_ids = [d["id"] for d in mac.get("/api/remote").json()["devices"]]
    assert len(store_ids) == 2
    mac.patch(f"/api/remote/devices/{devs['Remote iPhone']['id']}", json={"name": "  Away   phone "})
    assert "Away phone" in [d["name"] for d in mac.get("/api/remote").json()["devices"]]
    assert mac.patch(f"/api/remote/devices/{store_ids[0]}", json={"name": "  "}).status_code == 422
    assert mac.patch("/api/remote/devices/nope", json={"name": "x"}).status_code == 404
    assert a.patch(f"/api/remote/devices/{store_ids[0]}", json={"name": "hacked"}, headers=ha).status_code == 403      # only from the Mac


def test_a_phone_that_went_quiet_is_not_online(tmp_path):
    clock = {"t": 100.0}
    s = RemoteStore(tmp_path / "x.json", clock=lambda: clock["t"])
    code = s.new_code()["code"]
    token = s.pair(code, "P")
    s.verify(token, "192.168.0.9")
    assert s.listing()[0]["online"] is True
    clock["t"] += 30
    assert s.listing()[0]["online"] is False and s.listing()[0]["last_seen"] == 100.0


def test_a_paired_phone_can_monitor_plans_and_adjust_them(mac):
    p = phone(mac)
    h = pair(mac, p)
    for path in ("/api/schedules", "/api/awake", "/api/activity"):
        assert p.get(path, headers=h).status_code == 200, path
    plan = mac.post("/api/schedules", json={"name": "Night", "steps": [{"at": "2099-01-01T20:00", "type": "clear", "params": {}}]}).json()
    assert p.post(f"/api/schedules/{plan['id']}/arm", json={"armed": True}, headers=h).status_code == 200
    assert p.get("/api/schedules", headers=h).json()["plans"][0]["armed"] is True
    sid = plan["steps"][0]["id"]
    assert p.post(f"/api/schedules/{plan['id']}/steps/{sid}/run", headers=h).status_code == 200
    assert p.patch("/api/journey", json={"speed_kmh": 9}, headers=h).status_code in (200, 409)        # allowed (409 = not moving)
