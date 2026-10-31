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
