"""Multiple phones, Wi-Fi, disk-image warnings, and the real backend's targeting (library calls mocked)."""
import asyncio
import time
import types

import pytest
from fastapi.testclient import TestClient

import locspoof.device as dev
from conftest import FakeBackend, FakeHouses, FakeRouter
from locspoof.device import PmdBackend, norm_udid
from locspoof.errors import AppError
from locspoof.server import create_app


def mk(tmp_path, **kw):
    return create_app(house_finder=FakeHouses(), router=FakeRouter(), favorites_path=tmp_path / "f.json", recents_path=tmp_path / "r.json",
                      offline_root=tmp_path / "o", settings_path=tmp_path / "s.json", routes_path=tmp_path / "rt.json",
                      devices_path=tmp_path / "d.json", walk_tick=0.01, ambient_tick=0.02, **kw)


# ---- disk image warning ---------------------------------------------------------------------------

def test_ddi_warning_and_ios_update_notice(tmp_path):
    be = FakeBackend(version="17.4")
    with TestClient(mk(tmp_path, backend=be)) as c:
        s = c.get("/api/status").json()
        assert s["warnings"] == [] and s["device"]["ddi_mounted"] is True
        be.info.ddi_mounted = False
        time.sleep(2.1)
        w = c.get("/api/status").json()["warnings"]
        assert len(w) == 1 and w[0]["code"] == "ddi_missing" and "not mounted" in w[0]["message"] and "internet" in w[0]["hint"]
        be.info.ios_version = "17.5"                       # an iOS update since the last time we saw this phone
        time.sleep(2.1)
        w = c.get("/api/status").json()["warnings"]
        assert "17.4 → 17.5" in w[0]["message"] and "again" in w[0]["message"]
        be.info.ddi_mounted = True                         # mounted now: warning goes away
        time.sleep(2.1)
        assert c.get("/api/status").json()["warnings"] == []
        be.info.ddi_mounted = None                         # unknown: no false alarm
        time.sleep(2.1)
        assert c.get("/api/status").json()["warnings"] == []


def test_ios_version_memory_survives_restart(tmp_path):
    be = FakeBackend(version="17.4")
    with TestClient(mk(tmp_path, backend=be)) as c:
        c.get("/api/status")
    be2 = FakeBackend(version="17.6")
    be2.info.ddi_mounted = False
    with TestClient(mk(tmp_path, backend=be2)) as c:
        assert "17.4 → 17.6" in c.get("/api/status").json()["warnings"][0]["message"]


# ---- Wi-Fi switch + connection type ---------------------------------------------------------------

def test_wifi_switch_and_connection_label(tmp_path):
    be = FakeBackend()
    with TestClient(mk(tmp_path, backend=be)) as c:
        d = c.get("/api/status").json()["device"]
        assert d["connection"] == "USB" and d["wifi_enabled"] is False
        s = c.post("/api/wifi", json={"enabled": True}).json()
        assert be.wifi_calls == [True] and s["device"]["wifi_enabled"] is True
        assert c.post("/api/wifi", json={"enabled": "yes"}).status_code == 422
    wifi = FakeBackend(connection="Wi-Fi")
    with TestClient(mk(tmp_path, backend=wifi)) as c:
        assert c.get("/api/status").json()["device"]["connection"] == "Wi-Fi"
        r = c.post("/api/wifi", json={"enabled": False})
        assert r.status_code == 409 and r.json()["error"]["code"] == "wifi_needs_usb"
        # location control works the same over Wi-Fi
        assert c.post("/api/location", json={"lat": 1, "lon": 2}).status_code == 200


def test_device_list_and_wifi_scan_endpoints(tmp_path):
    be = FakeBackend()
    be.scan_result = [{"udid": "X", "name": "Kitchen iPhone", "ios_version": "16.0"}]
    with TestClient(mk(tmp_path, backend=be)) as c:
        devs = c.get("/api/devices").json()["devices"]
        assert [d["name"] for d in devs] == ["Test iPhone"] and devs[0]["simulating"] is False
        c.post("/api/location", json={"lat": 3, "lon": 4})
        d = c.get("/api/devices?fresh=true").json()["devices"][0]
        assert d["simulating"] is True and (d["lat"], d["lon"]) == (3, 4)
        assert c.post("/api/devices/scan").status_code == 200 and be.scans == 1


# ---- several phones at once -----------------------------------------------------------------------

@pytest.fixture
def two_phones(tmp_path):
    a = FakeBackend(udid="00008110-AAAA", name="Alice's iPhone", version="17.4")
    b = FakeBackend(udid="00008120-BBBB", name="Bob's iPhone", version="15.7")
    by = {a.info.udid: a, b.info.udid: b}

    class Discovery:
        async def list_devices(self):
            return (await a.list_devices()) + (await b.list_devices())

        async def scan_wifi(self, timeout=5.0):
            return []

    made = []

    def factory(udid):
        made.append(udid)
        return by[udid] if udid else a

    with TestClient(mk(tmp_path, backend_factory=factory, discovery=Discovery())) as c:
        c.a, c.b, c.made = a, b, made
        yield c


def hdr(udid):
    return {"X-Device": udid}


def test_phones_are_controlled_independently(two_phones):
    c = two_phones
    assert [d["name"] for d in c.get("/api/devices").json()["devices"]] == ["Alice's iPhone", "Bob's iPhone"]
    sa = c.get("/api/status", headers=hdr("00008110-AAAA")).json()
    sb = c.get("/api/status", headers=hdr("00008120-BBBB")).json()
    assert sa["device"]["name"] == "Alice's iPhone" and sb["device"]["name"] == "Bob's iPhone" and sb["device"]["ios_version"] == "15.7"
    c.post("/api/location", json={"lat": 10, "lon": 10}, headers=hdr("00008110-AAAA"))
    c.post("/api/location", json={"lat": -20, "lon": 30}, headers=hdr("00008120-BBBB"))
    assert [x[1:] for s in c.a.sessions for x in s.calls] == [(10, 10)]
    assert [x[1:] for s in c.b.sessions for x in s.calls] == [(-20, 30)]
    devs = {d["name"]: d for d in c.get("/api/devices?fresh=true").json()["devices"]}
    assert devs["Alice's iPhone"]["lat"] == 10 and devs["Bob's iPhone"]["lat"] == -20
    c.delete("/api/location", headers=hdr("00008120-BBBB"))                 # clearing Bob leaves Alice alone
    devs = {d["name"]: d for d in c.get("/api/devices?fresh=true").json()["devices"]}
    assert devs["Alice's iPhone"]["simulating"] is True and devs["Bob's iPhone"]["simulating"] is False


def test_both_phones_can_run_journeys_at_once(two_phones):
    c = two_phones
    for u, lat in (("00008110-AAAA", 0.0), ("00008120-BBBB", 5.0)):
        c.post("/api/location", json={"lat": lat, "lon": 0}, headers=hdr(u))
        c.post("/api/journey", json={"stops": [{"lat": lat, "lon": 0.0004}], "mode": "straight", "speed_kmh": 200}, headers=hdr(u))
    deadline = time.time() + 15
    while time.time() < deadline:
        w = [c.get("/api/status", headers=hdr(u)).json()["walk"]["active"] for u in ("00008110-AAAA", "00008120-BBBB")]
        if not any(w):
            break
        time.sleep(0.05)
    for be, lat in ((c.a, 0.0), (c.b, 5.0)):
        pts = [x[1:] for s in be.sessions for x in s.calls]
        assert pts[-1] == (lat, 0.0004) and all(p[0] == lat or abs(p[0] - lat) < 1e-6 for p in pts)   # each stays on its own line


def test_default_phone_is_the_first_and_udid_spelling_is_forgiving(two_phones):
    c = two_phones
    assert c.get("/api/status").json()["device"]["name"] == "Alice's iPhone"          # no header: first phone
    c.post("/api/location", json={"lat": 1, "lon": 1}, headers=hdr("00008110AAAA"))   # no dash
    c.post("/api/location", json={"lat": 2, "lon": 2}, headers=hdr("00008110-aaaa"))  # lower case
    assert len(c.a.sessions) == 1 and [x[1:] for x in c.a.sessions[0].calls] == [(1, 1), (2, 2)]   # one manager, one session
    assert norm_udid("00008110-AAAA") == norm_udid("00008110aaaa")


def test_one_phone_unplugged_does_not_affect_the_other(two_phones):
    c = two_phones
    c.post("/api/location", json={"lat": 1, "lon": 1}, headers=hdr("00008110-AAAA"))
    c.post("/api/location", json={"lat": 2, "lon": 2}, headers=hdr("00008120-BBBB"))
    c.a.unplug()                                    # Alice runs iOS 17: her simulation dies with the cable -> will be restored
    time.sleep(2.1)
    sa = c.get("/api/status", headers=hdr("00008110-AAAA")).json()
    sb = c.get("/api/status", headers=hdr("00008120-BBBB")).json()
    assert sa["state"] == "no_device" and sa["simulation"]["active"] and sa["simulation"]["reconnecting"] is True
    assert sb["state"] == "ready" and sb["simulation"]["active"] and "reconnecting" not in sb["simulation"]
    c.a.replug()
    c.b.unplug()                                    # Bob runs iOS 15: the phone itself keeps the fake location, nothing to restore
    time.sleep(2.1)
    sa = c.get("/api/status", headers=hdr("00008110-AAAA")).json()
    sb = c.get("/api/status", headers=hdr("00008120-BBBB")).json()
    assert sa["state"] == "ready" and "reconnecting" not in sa["simulation"] and sa["simulation"]["lat"] == 1
    assert sb["state"] == "no_device" and sb["simulation"]["active"] and "reconnecting" not in sb["simulation"]


# ---- real backend: finding the phone ----------------------------------------------------------------

def mux(serial, kind):
    from pymobiledevice3.usbmux import MuxDevice
    return MuxDevice(devid=1, serial=serial, connection_type=kind)


@pytest.fixture
def fake_usbmux(monkeypatch):
    state = {"devices": []}

    async def list_devices(usbmux_address=None):
        return list(state["devices"])

    monkeypatch.setattr("pymobiledevice3.usbmux.list_devices", list_devices)
    dev._WIFI_FOUND.clear()
    yield state
    dev._WIFI_FOUND.clear()


def run(coro):
    return asyncio.run(coro)


def test_target_prefers_usb_then_wifi_then_bonjour(fake_usbmux):
    be = PmdBackend()
    with pytest.raises(AppError) as e:
        run(be._resolve())
    assert e.value.code == "no_device"
    fake_usbmux["devices"] = [mux("AAA", "Network")]
    t = run(be._resolve())
    assert (t.serial, t.connection, t.bonjour) == ("AAA", "Wi-Fi", False)
    fake_usbmux["devices"] = [mux("AAA", "Network"), mux("AAA", "USB")]
    assert run(be._resolve()).connection == "USB"                                   # cable wins over Wi-Fi
    fake_usbmux["devices"] = []
    dev._WIFI_FOUND["bbb"] = {"udid": "BBB", "name": "n", "ios_version": "17.0", "seen": time.monotonic()}
    t = run(be._resolve())
    assert (t.serial, t.connection, t.bonjour) == ("BBB", "Wi-Fi", True)
    dev._WIFI_FOUND["bbb"]["seen"] -= dev.WIFI_FOUND_TTL_S + 1                       # stale scan results expire
    with pytest.raises(AppError):
        run(be._resolve())


def test_target_filters_by_udid_ignoring_dashes(fake_usbmux):
    fake_usbmux["devices"] = [mux("00008110AAAA", "USB"), mux("00008120BBBB", "USB")]
    assert run(PmdBackend(udid="00008120-BBBB")._resolve()).serial == "00008120BBBB"
    assert run(PmdBackend()._resolve()).serial == "00008110AAAA"                    # no udid: first
    with pytest.raises(AppError):
        run(PmdBackend(udid="00009999-CCCC")._resolve())


class FakeLockdown:
    def __init__(self, udid="U-1", name="Test", version="17.4", paired=True, devmode=True, wifi=True):
        self.udid, self.paired, self.product_version = udid, paired, version
        self.all_values = {"DeviceName": name, "ProductType": "iPhone14,5"}
        self._dm, self._wifi, self.closed, self.wifi_set = devmode, wifi, False, None

    async def close(self): self.closed = True
    async def get_developer_mode_status(self): return self._dm
    async def get_enable_wifi_connections(self): return self._wifi
    async def set_enable_wifi_connections(self, v): self.wifi_set = v


@pytest.fixture
def be_with(monkeypatch, fake_usbmux):
    def make(lockdown, ddi=True, serial="U-1", kind="USB"):
        fake_usbmux["devices"] = [mux(serial, kind)] if serial else []
        be = PmdBackend()
        seen = {}

        async def _lockdown(target, autopair=False):
            seen["target"] = target
            return lockdown

        async def _ddi(_ld):
            return ddi

        monkeypatch.setattr(be, "_lockdown", _lockdown)
        monkeypatch.setattr(be, "_ddi_mounted", _ddi)
        be.seen = seen
        return be

    return make


def test_probe_reports_connection_wifi_flag_and_ddi(be_with):
    ld = FakeLockdown(wifi=True)
    info = run(be_with(ld, ddi=False, kind="Network").probe())
    assert (info.name, info.ios_version, info.connection, info.wifi_enabled, info.ddi_mounted) == ("Test", "17.4", "Wi-Fi", True, False)
    assert ld.closed
    assert run(be_with(FakeLockdown(), ddi=True).probe()).connection == "USB"


@pytest.mark.parametrize("ld,code", [(FakeLockdown(paired=False), "not_trusted"), (FakeLockdown(devmode=False), "developer_mode_off")])
def test_probe_friendly_errors_and_closes(be_with, ld, code):
    with pytest.raises(AppError) as e:
        run(be_with(ld).probe())
    assert e.value.code == code and ld.closed


def test_old_ios_skips_developer_mode_check(be_with):
    ld = FakeLockdown(version="15.7", devmode=False)
    assert run(be_with(ld).probe()).ios_version == "15.7"


def test_set_wifi_needs_a_cable(be_with):
    ld = FakeLockdown()
    run(be_with(ld).set_wifi(True))
    assert ld.wifi_set is True and ld.closed
    ld2 = FakeLockdown()
    with pytest.raises(AppError) as e:
        run(be_with(ld2, kind="Network").set_wifi(True))
    assert e.value.code == "wifi_needs_usb" and ld2.wifi_set is None


def test_list_devices_merges_usb_wifi_and_bonjour(fake_usbmux, monkeypatch):
    fake_usbmux["devices"] = [mux("AAA", "Network"), mux("AAA", "USB"), mux("CCC", "Network")]
    dev._WIFI_FOUND["bbb"] = {"udid": "BBB", "name": "n", "ios_version": "17.0", "seen": time.monotonic()}
    be = PmdBackend()
    names = {"AAA": "Alice", "BBB": "Bob", "CCC": "Cleo"}

    async def _lockdown(target, autopair=False):
        return FakeLockdown(udid=target.serial, name=names[target.serial], paired=target.serial != "CCC")

    monkeypatch.setattr(be, "_lockdown", _lockdown)
    out = run(be.list_devices())
    assert [(d["udid"], d["name"], d["connection"], d["paired"]) for d in out] == [
        ("AAA", "Alice", "USB", True), ("BBB", "Bob", "Wi-Fi", True), ("CCC", "Cleo", "Wi-Fi", False)]


def test_scan_wifi_collects_bonjour_phones_and_closes_everything(monkeypatch, fake_usbmux):
    closed = {"gen": False}

    async def fake_get(udid=None, pair_records=None, only_paired=False, timeout=0):
        try:
            for i in (1, 2):
                yield f"192.168.1.{i}", FakeLockdown(udid=f"P-{i}", name=f"Phone {i}", version="17.2")
        finally:
            closed["gen"] = True

    monkeypatch.setattr("pymobiledevice3.lockdown.get_mobdev2_lockdowns", fake_get)
    out = run(PmdBackend().scan_wifi(timeout=0.1))
    assert [(d["udid"], d["name"]) for d in out] == [("P-1", "Phone 1"), ("P-2", "Phone 2")] and closed["gen"]
    assert set(dev._WIFI_FOUND) == {"p1", "p2"}
    fake_usbmux["devices"] = []
    assert run(PmdBackend(udid="P-2")._resolve()).bonjour is True                      # a scanned phone is now reachable


# ---- real backend: iOS 17+ tunnel choices ----------------------------------------------------------

def test_second_ios17_phone_gets_a_clear_tunneld_hint(monkeypatch):
    from pymobiledevice3.exceptions import PyMobileDevice3Exception

    class BusyTunnel:
        def __init__(self, serial=None, autopair=True, remotepairing_fallback=True):
            self.args = (serial, remotepairing_fallback)

        async def aopen(self):
            raise PyMobileDevice3Exception("A userspace tunnel is already active in this process; only one userspace tunnel per process is supported.")

    monkeypatch.setattr("pymobiledevice3.remote.userspace_tunnel.UserspaceRsdTunnel", BusyTunnel)
    be = PmdBackend(tunnel_mode="userspace")
    with pytest.raises(AppError) as e:
        run(be._open_rsd(dev._Target("U-1", "USB")))
    assert e.value.code == "second_phone_needs_tunneld" and "tunneld" in e.value.hint and e.value.status == 409


def test_wifi_enables_remote_pairing_fallback_but_usb_does_not(monkeypatch):
    seen = []

    class Tunnel:
        def __init__(self, serial=None, autopair=True, remotepairing_fallback=True):
            seen.append(remotepairing_fallback)

        async def aopen(self):
            return "rsd"

        async def aclose(self):
            pass

    monkeypatch.setattr("pymobiledevice3.remote.userspace_tunnel.UserspaceRsdTunnel", Tunnel)
    be = PmdBackend(tunnel_mode="userspace")
    assert run(be._open_rsd(dev._Target("U", "USB")))[0] == "rsd" and run(be._open_rsd(dev._Target("U", "Wi-Fi")))[0] == "rsd"
    assert seen == [False, True]


def test_bonjour_tunnel_hook_is_swapped_in_and_always_restored(monkeypatch):
    original = object()
    module = types.SimpleNamespace(_create_no_root_tunnel_provider=original)
    seen = {}

    class Tunnel:
        async def aopen(self):
            seen["hook"] = module._create_no_root_tunnel_provider
            raise RuntimeError("boom")

    be = PmdBackend()
    with pytest.raises(RuntimeError):
        run(be._aopen_via_bonjour(Tunnel(), module, dev._Target("B", "Wi-Fi", bonjour=True)))
    assert seen["hook"] is not original and module._create_no_root_tunnel_provider is original      # restored even on failure


def test_bonjour_lockdown_returns_first_paired_phone_and_errors_when_none(monkeypatch):
    async def found(udid=None, only_paired=False, timeout=0, pair_records=None):
        assert only_paired and udid == "P-1"
        yield "10.0.0.5", FakeLockdown(udid="P-1")

    monkeypatch.setattr("pymobiledevice3.lockdown.get_mobdev2_lockdowns", found)
    assert run(PmdBackend._bonjour_lockdown("P-1")).udid == "P-1"

    async def nothing(udid=None, only_paired=False, timeout=0, pair_records=None):
        return
        yield

    monkeypatch.setattr("pymobiledevice3.lockdown.get_mobdev2_lockdowns", nothing)
    with pytest.raises(AppError) as e:
        run(PmdBackend._bonjour_lockdown("P-1"))
    assert e.value.code == "no_device"


def test_ddi_check_is_cached_and_failure_means_unknown(monkeypatch):
    calls = {"n": 0}

    class Mounter:
        def __init__(self, lockdown): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def is_image_mounted(self, t):
            calls["n"] += 1
            return False

    monkeypatch.setattr("pymobiledevice3.services.mobile_image_mounter.MobileImageMounterService", Mounter)
    monkeypatch.setattr("pymobiledevice3.services.mobile_image_mounter.image_type_for_device", lambda ld: "Developer")
    be = PmdBackend()
    assert run(be._ddi_mounted(object())) is False and run(be._ddi_mounted(object())) is False and calls["n"] == 1

    class Broken(Mounter):
        async def is_image_mounted(self, t): raise RuntimeError("service missing")

    monkeypatch.setattr("pymobiledevice3.services.mobile_image_mounter.MobileImageMounterService", Broken)
    assert run(PmdBackend()._ddi_mounted(object())) is None
