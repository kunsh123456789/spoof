"""The native-window launcher, tested with a fake `webview` module (no display needed)."""
import http.server
import socket
import threading
import urllib.request

import pytest

import locspoof.desktop as desktop
from conftest import FakeBackend, FakeHouses, FakeRouter
from locspoof.runner import ServerThread, is_our_app, pick_port, port_free
from locspoof.server import create_app

HOST = "127.0.0.1"


def free_port():
    with socket.socket() as s:
        s.bind((HOST, 0))
        return s.getsockname()[1]


@pytest.fixture
def factory(tmp_path):
    made = []

    def make(tunnel):
        be = FakeBackend()
        made.append(be)
        return create_app(backend=be, router=FakeRouter(), house_finder=FakeHouses(), favorites_path=tmp_path / "f.json",
                          recents_path=tmp_path / "r.json", offline_root=tmp_path / "o", settings_path=tmp_path / "s.json",
                          routes_path=tmp_path / "rt.json", devices_path=tmp_path / "d.json")

    make.made = made
    return make


class FakeWebview:
    """Records how the window was created and runs `during_start` while the 'window' is open."""

    def __init__(self, during_start=None):
        self.windows, self.start_kwargs, self.during_start = [], None, during_start

    def create_window(self, title, url, **kw):
        self.windows.append((title, url, kw))

    def start(self, **kw):
        self.start_kwargs = kw
        if self.during_start:
            self.during_start(self.windows[0][1])


def get(url):
    with urllib.request.urlopen(url, timeout=5) as r:
        return r.status, r.read()


# ---- runner -------------------------------------------------------------------------------------

def test_port_helpers():
    p = free_port()
    assert port_free(HOST, p) and pick_port(HOST, p) == p
    with socket.socket() as s:
        s.bind((HOST, p))
        assert not port_free(HOST, p)
        other = pick_port(HOST, p)
        assert other != p and port_free(HOST, other)


def test_is_our_app_only_for_this_app(factory):
    p = free_port()
    assert not is_our_app(HOST, p, timeout=0.3)                      # nothing listening
    srv = ServerThread(factory("auto"), HOST, p, log_level="warning")
    srv.start()
    try:
        assert is_our_app(HOST, p)
    finally:
        srv.stop()
    q = free_port()                                                  # some other web server
    other = http.server.HTTPServer((HOST, q), http.server.SimpleHTTPRequestHandler)
    threading.Thread(target=other.serve_forever, daemon=True).start()
    try:
        assert not is_our_app(HOST, q, timeout=0.5)
    finally:
        other.shutdown()


def test_server_thread_starts_serves_and_stops_cleanly(factory):
    p = free_port()
    app = factory("auto")
    srv = ServerThread(app, HOST, p, log_level="warning")
    srv.start()
    assert srv.url == f"http://{HOST}:{p}" and get(srv.url + "/")[0] == 200
    be = factory.made[-1]
    urllib.request.urlopen(urllib.request.Request(srv.url + "/api/location", data=b'{"lat":1,"lon":2}',
                                                  headers={"Content-Type": "application/json"}), timeout=5)
    assert be.sessions and not be.sessions[0].closed
    srv.stop()
    assert not srv.thread.is_alive() and be.sessions[0].closed          # shutdown closed the phone session
    with pytest.raises(Exception):
        get(srv.url + "/", )


def test_server_thread_reports_a_busy_port():
    with socket.socket() as s:
        s.bind((HOST, 0))
        s.listen()
        srv = ServerThread(create_app.__globals__["FastAPI"](), HOST, s.getsockname()[1], log_level="critical")
        with pytest.raises(RuntimeError):
            srv.start(timeout=5)


# ---- desktop main -----------------------------------------------------------------------------------

def test_window_is_created_with_the_right_options_and_server_stops_afterwards(factory):
    p = free_port()
    seen = {}

    def during(url):
        seen["status"] = get(url + "/api/settings")[0]
        seen["url"] = url

    wv = FakeWebview(during)
    assert desktop.main(["--port", str(p)], webview_module=wv, app_factory=factory) == 0
    title, url, kw = wv.windows[0]
    assert title == "iPhone Location" and url == f"http://{HOST}:{p}" and seen == {"status": 200, "url": url}
    assert kw["confirm_close"] is True and kw["text_select"] is True and kw["min_size"] == (960, 640)
    assert isinstance(kw["js_api"], desktop.WindowApi)
    assert wv.start_kwargs["private_mode"] is False and "storage_path" in wv.start_kwargs and wv.start_kwargs["debug"] is False
    assert "fake location" in wv.start_kwargs["localization"]["global.quitConfirmation"]
    with pytest.raises(Exception):
        get(url + "/")                                                  # server was stopped when the window closed
    assert factory.made[-1].sessions == [] or all(s.closed for s in factory.made[-1].sessions)


def test_debug_flag_reaches_the_window(factory):
    wv = FakeWebview()
    desktop.main(["--port", str(free_port()), "--debug"], webview_module=wv, app_factory=factory)
    assert wv.start_kwargs["debug"] is True


def test_attaches_to_an_already_running_app_and_leaves_it_running(factory):
    p = free_port()
    running = ServerThread(factory("auto"), HOST, p, log_level="warning")
    running.start()
    try:
        wv = FakeWebview()
        calls = []
        assert desktop.main(["--port", str(p)], webview_module=wv, app_factory=lambda t: calls.append(t)) == 0
        assert calls == [] and wv.windows[0][1] == f"http://{HOST}:{p}"     # no second server was built
        assert get(f"http://{HOST}:{p}/")[0] == 200                          # the original is still up
    finally:
        running.stop()


def test_busy_port_taken_by_something_else_falls_back_to_a_free_one(factory):
    with socket.socket() as s:
        s.bind((HOST, 0))
        s.listen()
        busy = s.getsockname()[1]
        wv = FakeWebview(lambda url: get(url + "/api/settings"))
        assert desktop.main(["--port", str(busy)], webview_module=wv, app_factory=factory) == 0
        assert wv.windows[0][1] != f"http://{HOST}:{busy}"


def test_browser_mode_opens_the_default_browser(factory, monkeypatch):
    opened, waited = [], []
    monkeypatch.setattr(desktop.webbrowser, "open", lambda u: opened.append(u))
    p = free_port()
    rc = desktop.main(["--port", str(p), "--browser"], app_factory=factory, wait=lambda: waited.append(get(f"http://{HOST}:{p}/")[0]))
    assert rc == 0 and opened == [f"http://{HOST}:{p}"] and waited == [200]


def test_missing_pywebview_without_a_terminal_exits_with_a_clear_code(factory, monkeypatch):
    import builtins, sys
    real = builtins.__import__

    def no_webview(name, *a, **k):
        if name == "webview":
            raise ImportError("no webview")
        return real(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", no_webview)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False, raising=False)
    assert desktop.main(["--port", str(free_port())], app_factory=factory) == 2


def test_a_server_that_cannot_start_returns_an_error_code(monkeypatch):
    class Broken:
        def __init__(self, *a, **k): pass
        def start(self, *a, **k): raise RuntimeError("boom")
        def stop(self): pass

    monkeypatch.setattr(desktop, "ServerThread", Broken)
    assert desktop.main(["--port", str(free_port())], webview_module=FakeWebview(), app_factory=lambda t: object()) == 1


# ---- helpers exposed to the window --------------------------------------------------------------------

def test_open_external_only_allows_web_links(monkeypatch):
    opened = []
    monkeypatch.setattr(desktop.webbrowser, "open", lambda u: opened.append(u))
    api = desktop.WindowApi()
    assert api.open_external("https://www.openstreetmap.org/copyright") is True
    assert api.open_external("HTTP://example.com") is True
    for bad in ("file:///etc/passwd", "javascript:alert(1)", "ftp://x", "/relative", "", None, 42):
        assert api.open_external(bad) is False
    assert opened == ["https://www.openstreetmap.org/copyright", "HTTP://example.com"]


def test_mac_polish_is_harmless_without_pyobjc():
    assert desktop.mac_polish() in (True, False)                          # never raises, whatever the platform


def test_storage_dir_is_a_per_user_folder():
    d = desktop.storage_dir()
    assert str(d).startswith(str(desktop.Path.home()))
