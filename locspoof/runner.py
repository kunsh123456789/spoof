"""Run the web server on a background thread (so a native window can own the main thread)."""
from __future__ import annotations

import json
import logging
import socket
import threading
import time
import urllib.request
from typing import Optional

import uvicorn

log = logging.getLogger("locspoof.runner")


def port_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False


def pick_port(host: str, preferred: int) -> int:
    """The preferred port if it is free, otherwise any free one."""
    if port_free(host, preferred):
        return preferred
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((host, 0))
        return s.getsockname()[1]


def is_our_app(host: str, port: int, timeout: float = 1.5) -> bool:
    """Is an iPhone Location Simulator already answering on this port?"""
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/api/settings", timeout=timeout) as r:
            data = json.loads(r.read())
        return isinstance(data, dict) and "auto_reconnect" in data and "noise" in data
    except Exception:
        return False


class ServerThread:
    """uvicorn in a daemon thread with a clean start/stop (stopping runs the app's shutdown, which
    closes phone sessions and so ends an iOS 17+ fake location)."""

    def __init__(self, app, host: str, port: int, log_level: str = "info"):
        self.host, self.port = host, port
        self.server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level=log_level))
        self.thread = threading.Thread(target=self._run, name="locspoof-server", daemon=True)

    def _run(self) -> None:
        try:
            self.server.run()
        except SystemExit as e:        # uvicorn exits (code 3) when it cannot bind; start() reports that
            log.error("the server exited during startup (code %s)", e.code)

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def start(self, timeout: float = 20.0) -> None:
        self.thread.start()
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if self.server.started:
                return
            if not self.thread.is_alive():
                raise RuntimeError("the server stopped while starting (is the port in use?)")
            time.sleep(0.05)
        raise RuntimeError("the server did not start in time")

    def stop(self, timeout: float = 15.0) -> None:
        self.server.should_exit = True
        self.thread.join(timeout)
        if self.thread.is_alive():
            log.warning("server did not stop within %.0f s", timeout)


class ServerSupervisor:
    """Keeps a server running: if its thread dies unexpectedly it is rebuilt and started again (a few times).

    A deliberate stop() is never "repaired". State held in memory (such as a running trip) does not survive a restart,
    but whatever the app saved to disk (see memory.py) does.
    """

    def __init__(self, build_app, host: str, port: int, check_every: float = 2.0, max_restarts: int = 3, log_level: str = "info"):
        self.build_app, self.host, self.port, self.log_level = build_app, host, port, log_level
        self.check_every, self.max_restarts = check_every, max_restarts
        self.restarts = 0
        self.gave_up = False
        self.current: Optional[ServerThread] = None
        self._stopping = threading.Event()
        self._monitor: Optional[threading.Thread] = None

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def _launch(self) -> None:
        self.current = ServerThread(self.build_app(), self.host, self.port, self.log_level)
        self.current.start()

    def start(self) -> None:
        self._launch()
        self._monitor = threading.Thread(target=self._watch, name="locspoof-supervisor", daemon=True)
        self._monitor.start()

    def _watch(self) -> None:
        while not self._stopping.wait(self.check_every):
            if self.current is not None and self.current.thread.is_alive():
                continue
            if self.restarts >= self.max_restarts:
                self.gave_up = True
                log.error("the server stopped %d times; giving up", self.restarts + 1)
                return
            self.restarts += 1
            log.error("the server stopped unexpectedly; restarting it (%d/%d)", self.restarts, self.max_restarts)
            try:
                self._launch()
            except Exception:
                log.exception("could not restart the server")

    def stop(self, timeout: float = 15.0) -> None:
        self._stopping.set()
        if self._monitor is not None:
            self._monitor.join(timeout)
        if self.current is not None:
            self.current.stop(timeout)
