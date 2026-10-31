"""Control the app from your iPhone (or any phone/tablet on the same Wi-Fi): pairing, tokens and the LAN listener.

How it stays safe:
* It is OFF by default. The normal app still listens on this Mac only (127.0.0.1).
* When you switch it on, a SECOND listener opens on the local network. Anyone who reaches it can only load the remote page and
  ask to pair; everything else needs a token that this Mac handed out after you typed the pairing code shown on the Mac.
* A paired phone is only allowed a short list of "remote control" requests (see ``ALLOWED``). It can never change settings,
  download maps, edit plans or manage other phones; those only work from this Mac.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import secrets
import shutil
import socket
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

import uvicorn

from .errors import AppError
from .fileio import atomic_write

log = logging.getLogger("locspoof.remote")

LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost", "testclient"}
CODE_TTL_S = 600
MAX_WRONG_GUESSES = 5
LOCK_S = 60
ONLINE_S = 12            # a paired phone counts as connected if it talked to the Mac in the last few seconds (the remote polls every 1-3 s)

# What a paired phone may do: (method, path regex)
ALLOWED = [(m, re.compile(p)) for m, p in [
    ("GET", r"^/api/status$"), ("POST", r"^/api/location$"), ("DELETE", r"^/api/location$"),
    ("POST", r"^/api/journey$"), ("DELETE", r"^/api/journey$"), ("PATCH", r"^/api/journey$"),
    ("POST", r"^/api/journey/return$"), ("GET", r"^/api/journey/route$"),
    ("GET", r"^/api/search$"), ("GET", r"^/api/reverse$"), ("GET", r"^/api/snap$"),
    ("GET", r"^/api/favorites$"), ("GET", r"^/api/recent$"), ("GET", r"^/api/routes$"), ("POST", r"^/api/routes/[A-Za-z0-9_-]+/start$"),
    ("GET", r"^/api/panic$"), ("POST", r"^/api/panic/go$"), ("GET", r"^/api/devices$"),
    # monitoring and light adjustments: read the plans / health / log, switch a plan on or off, run one step now
    ("GET", r"^/api/schedules$"), ("POST", r"^/api/schedules/[A-Za-z0-9_-]+/arm$"), ("POST", r"^/api/schedules/[A-Za-z0-9_-]+/steps/[A-Za-z0-9_-]+/run$"),
    ("GET", r"^/api/awake$"), ("GET", r"^/api/activity$"), ("GET", r"^/api/battery$"),
    ("GET", r"^/api/remote/me$"),
]]
TILES = re.compile(r"^/tiles/\d+/\d+/\d+\.png$")
# Reachable before pairing (the page itself and the files it needs)
PUBLIC = [(m, re.compile(p)) for m, p in [
    ("GET", r"^/remote$"), ("GET", r"^/remote\.webmanifest$"), ("GET", r"^/static/remote\.(js|css)$"), ("GET", r"^/static/icon\.png$"),
    ("GET", r"^/static/vendor/leaflet/.+"), ("POST", r"^/api/remote/pair$"),
]]


def is_local(host: Optional[str]) -> bool:
    return (host or "") in LOCAL_HOSTS


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class RemoteStore:
    """Paired phones (remote.json keeps only a hash of each token) and the one-time pairing code."""

    def __init__(self, path: Path, clock=time.time):
        self.path, self.clock = Path(path), clock
        self._lock = threading.Lock()
        self._code: Optional[dict] = None
        self._locked_until = 0.0
        self._seen: dict[str, float] = {}
        self._live: dict[str, tuple[float, str]] = {}      # id -> (last request time, address it came from): who is connected right now
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
            self.devices: list[dict] = [x for x in d if isinstance(x, dict) and {"id", "hash", "name"} <= x.keys()]
        except (FileNotFoundError, ValueError, TypeError, OSError):
            self.devices = []

    def _save(self) -> None:
        try:
            atomic_write(self.path, json.dumps(self.devices, indent=2).encode("utf-8"))
        except OSError:
            log.warning("could not save %s", self.path)

    def new_code(self) -> dict:
        with self._lock:
            self._code = {"code": f"{secrets.randbelow(1_000_000):06d}", "expires": self.clock() + CODE_TTL_S, "wrong": 0}
            return {"code": self._code["code"], "expires_in": CODE_TTL_S}

    def current_code(self) -> Optional[dict]:
        with self._lock:
            c = self._code
            if not c or c["expires"] <= self.clock():
                return None
            return {"code": c["code"], "expires_in": int(c["expires"] - self.clock())}

    def pair(self, code: str, name: str) -> str:
        """Check the code typed on the phone; on success hand out a long-lived token (the code is single use)."""
        with self._lock:
            now = self.clock()
            if now < self._locked_until:
                raise AppError("pair_locked", "Too many wrong codes.", f"Wait a minute, then ask for a new code on the Mac.", 429)
            c = self._code
            if not c or c["expires"] <= now:
                raise AppError("no_code", "There is no pairing code right now.", "On the Mac: Settings → Phone remote → New pairing code.", 409)
            if not hmac.compare_digest(str(code).strip(), c["code"]):
                c["wrong"] += 1
                if c["wrong"] >= MAX_WRONG_GUESSES:
                    self._code, self._locked_until = None, now + LOCK_S
                    raise AppError("pair_locked", "Too many wrong codes.", "Ask for a new code on the Mac.", 429)
                raise AppError("bad_code", "That code is not right.", f"{MAX_WRONG_GUESSES - c['wrong']} tries left.", 403)
            self._code = None
            token = secrets.token_urlsafe(32)
            self.devices.append({"id": uuid.uuid4().hex[:10], "hash": _hash(token), "name": (" ".join(str(name).split()) or "Phone")[:40],
                                 "created": now, "last_seen": now})
            self._save()
            return token

    def verify(self, token: str, ip: str = "") -> Optional[dict]:
        if not token:
            return None
        h = _hash(token)
        with self._lock:
            for d in self.devices:
                if hmac.compare_digest(d["hash"], h):
                    now = self.clock()
                    self._live[d["id"]] = (now, ip)
                    if now - self._seen.get(d["id"], 0) > 30:          # don't rewrite the file on every request
                        self._seen[d["id"]] = now
                        d["last_seen"] = now
                        self._save()
                    return d
        return None

    def revoke(self, device_id: str) -> bool:
        with self._lock:
            kept = [d for d in self.devices if d["id"] != device_id]
            if len(kept) == len(self.devices):
                return False
            self.devices = kept
            self._save()
            return True

    def rename(self, device_id: str, name: str) -> bool:
        name = " ".join(str(name).split())[:40]
        if not name:
            raise AppError("bad_name", "Give the phone a name.", status=422)
        with self._lock:
            for d in self.devices:
                if d["id"] == device_id:
                    d["name"] = name
                    self._save()
                    return True
        return False

    def listing(self) -> list[dict]:
        with self._lock:
            now = self.clock()
            out = []
            for d in self.devices:
                at, ip = self._live.get(d["id"], (0.0, ""))
                via = "tailscale" if ip.startswith("100.") and 64 <= int((ip.split(".") + ["0"] * 4)[1] or 0) <= 127 else ("wifi" if ip else "")
                out.append({"id": d["id"], "name": d["name"], "created": d["created"], "last_seen": max(at, d.get("last_seen") or 0) or None,
                            "online": bool(at and now - at < ONLINE_S), "ip": ip, "via": via})
            return out


def check_request(store: RemoteStore, method: str, path: str, auth: str, query_token: str, ip: str = "") -> Optional[tuple[int, str, str]]:
    """None = allowed; otherwise (status, message, hint) for a request that came from the network."""
    if any(m == method and p.match(path) for m, p in PUBLIC):
        return None
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    if not token and TILES.match(path):
        token = query_token                                       # map tiles are <img> requests, which cannot send a header
    if not store.verify(token, ip):
        return 401, "Not paired.", "Open the remote page and enter the pairing code shown on the Mac."
    if TILES.match(path) and method == "GET":
        return None
    if any(m == method and p.match(path) for m, p in ALLOWED):
        return None
    return 403, "The phone remote can't do that.", "That function is only available in the app on the Mac."


def lan_addresses() -> list[str]:
    """This Mac's addresses on the local network (best effort), plus its Bonjour name."""
    found: list[str] = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:      # no packet is sent: this only picks the outgoing interface
            s.connect(("10.255.255.255", 1))
            found.append(s.getsockname()[0])
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127.") and ip not in found:
                found.append(ip)
    except OSError:
        pass
    return found


def parse_ifconfig(text: str) -> list[str]:
    """Addresses in Tailscale's range (100.64.0.0/10) from `ifconfig` output."""
    out = []
    for m in re.finditer(r"\binet (\d+)\.(\d+)\.(\d+)\.(\d+)", text or ""):
        a, b, c, d = map(int, m.groups())
        if a == 100 and 64 <= b <= 127:
            out.append(f"{a}.{b}.{c}.{d}")
    return out


def parse_tailscale_status(text: str) -> dict:
    """{"running": bool, "dns": "mac.tailnet.ts.net" | None, "ips": [...]} from `tailscale status --json`."""
    try:
        d = json.loads(text)
        me = d.get("Self") or {}
        return {"running": d.get("BackendState") == "Running", "dns": (me.get("DNSName") or "").rstrip(".") or None,
                "ips": [i for i in me.get("TailscaleIPs", []) if "." in i]}
    except (ValueError, AttributeError, TypeError):
        return {"running": False, "dns": None, "ips": []}


_ts_cache: tuple[float, dict] = (0.0, {})


def tailscale_info(max_age: float = 15.0) -> dict:
    """Is Tailscale (a free private network app) set up on this Mac? Best effort, cached for a few seconds."""
    global _ts_cache
    if time.monotonic() - _ts_cache[0] < max_age and _ts_cache[1]:
        return _ts_cache[1]
    info = {"installed": False, "running": False, "dns": None, "ips": []}
    cli = shutil.which("tailscale") or next((p for p in ("/Applications/Tailscale.app/Contents/MacOS/Tailscale",) if Path(p).exists()), None)
    info["installed"] = bool(cli)
    try:
        info["ips"] = parse_ifconfig(subprocess.run(["ifconfig"], capture_output=True, text=True, timeout=3).stdout)
    except (OSError, subprocess.SubprocessError):
        pass
    if cli:
        try:
            st = parse_tailscale_status(subprocess.run([cli, "status", "--json"], capture_output=True, text=True, timeout=4).stdout)
            info["running"], info["dns"] = st["running"], st["dns"]
            info["ips"] = st["ips"] or info["ips"]
        except (OSError, subprocess.SubprocessError):
            pass
    info["running"] = info["running"] or bool(info["ips"])
    info["installed"] = info["installed"] or bool(info["ips"])
    _ts_cache = (time.monotonic(), info)
    return info


def bonjour_name() -> Optional[str]:
    n = socket.gethostname()
    if not n or n == "localhost":
        return None
    return n if n.endswith(".local") else n.split(".")[0] + ".local"


class RemoteListener:
    """The second server (0.0.0.0) that the phone talks to. Started and stopped from the Mac's settings."""

    def __init__(self, app, port: int = 8787):
        self.app, self.port = app, port
        self.server: Optional[uvicorn.Server] = None
        self.thread: Optional[threading.Thread] = None
        self.error: Optional[str] = None

    @property
    def running(self) -> bool:
        return bool(self.thread and self.thread.is_alive() and self.server and self.server.started)

    def start(self, port: Optional[int] = None) -> None:
        if port:
            self.port = port
        if self.thread and self.thread.is_alive():
            return
        self.error = None
        # lifespan "off": the main server already runs the app's background jobs; this one only answers requests
        self.server = uvicorn.Server(uvicorn.Config(self.app, host="0.0.0.0", port=self.port, log_level="warning", lifespan="off"))

        def run() -> None:
            try:
                self.server.run()
            except SystemExit:
                self.error = f"Could not open port {self.port} (is something else using it?)"
                log.error(self.error)
            except Exception as e:
                self.error = f"The remote listener stopped: {e}"
                log.exception("remote listener crashed")

        self.thread = threading.Thread(target=run, name="locspoof-remote", daemon=True)
        self.thread.start()
        end = time.monotonic() + 5
        while time.monotonic() < end and self.thread.is_alive() and not self.server.started:
            time.sleep(0.05)
        if not self.thread.is_alive() and not self.error:
            self.error = f"Could not open port {self.port}."

    def stop(self) -> None:
        if self.server:
            self.server.should_exit = True
        if self.thread:
            self.thread.join(5)
        self.thread = self.server = None
