"""Open the simulator in a native window (macOS WebKit via pywebview) instead of a browser tab.

    python desktop.py            # window
    python desktop.py --browser  # same server, opened in the default browser (Ctrl+C to quit)
"""
from __future__ import annotations

import argparse
import logging
import sys
import webbrowser
from pathlib import Path
from typing import Callable, Optional

from .runner import ServerSupervisor, is_our_app, pick_port, port_free

APP_NAME = "iPhone Location"
HOST = "127.0.0.1"
ICON = Path(__file__).resolve().parent.parent / "assets" / "icon.png"
QUIT_TEXT = (
    "Quit iPhone Location?\n\nOn iOS 17 and later, quitting also ends the fake location "
    "(your real GPS comes back). iOS 16 and earlier keep it until you restore it."
)
log = logging.getLogger("locspoof.desktop")


class WindowApi:
    """Methods the page can call as ``window.pywebview.api.<name>()``."""

    def open_external(self, url: str) -> bool:
        """Open a link in the system browser (only http/https, never file: or custom schemes)."""
        if isinstance(url, str) and url.lower().startswith(("http://", "https://")):
            webbrowser.open(url)
            return True
        return False


def log_path() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Logs" / f"{APP_NAME}.log"
    return Path.home() / ".iphone-location" / "app.log"


def setup_logging(verbose: bool = False, path: Optional[Path] = None) -> Path:
    """Log to the console and to a size-limited file (2 MB x 3 files), so the log can never grow without bound."""
    from logging.handlers import RotatingFileHandler

    path = path or log_path()
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    for h in [h for h in root.handlers if getattr(h, "_locspoof", False)]:
        root.removeHandler(h)                                    # calling this twice must not duplicate output
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(RotatingFileHandler(path, maxBytes=2_000_000, backupCount=3, encoding="utf-8"))
    except OSError:
        pass                                                     # an unwritable log folder must not stop the app
    for h in handlers:
        h.setFormatter(fmt)
        h._locspoof = True  # type: ignore[attr-defined]
        root.addHandler(h)
    return path


def storage_dir() -> Path:
    mac = Path.home() / "Library" / "Application Support" / APP_NAME
    return mac if sys.platform == "darwin" else Path.home() / ".iphone-location" / "webview"


def mac_polish(icon: Path = ICON) -> bool:
    """Give the Python process the app's name and icon in the Dock and menu bar (macOS, best effort)."""
    try:
        from AppKit import NSApplication, NSImage  # type: ignore
        from Foundation import NSBundle  # type: ignore

        info = NSBundle.mainBundle().infoDictionary()
        info["CFBundleName"] = APP_NAME
        if icon.exists():
            NSApplication.sharedApplication().setApplicationIconImage_(NSImage.alloc().initByReferencingFile_(str(icon)))
        return True
    except Exception:
        return False


def _block_until_interrupted() -> None:
    import time

    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass


def main(
    argv: Optional[list[str]] = None, webview_module=None,
    app_factory: Optional[Callable[[str], object]] = None, wait: Callable[[], None] = _block_until_interrupted,
) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--port", type=int, default=8000, help="preferred port (another is used if it is busy)")
    p.add_argument("--tunnel", choices=["auto", "tunneld", "userspace"], default="auto")
    p.add_argument("--browser", action="store_true", help="open the default browser instead of a window")
    p.add_argument("--debug", action="store_true", help="enable the web inspector in the window")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)
    setup_logging(args.verbose)

    if app_factory is None:
        from .server import create_app
        app_factory = lambda tunnel: create_app(tunnel_mode=tunnel)  # noqa: E731

    # One app, one server: if the simulator is already running (e.g. started from Terminal), just show it.
    server: Optional[ServerSupervisor] = None
    port = args.port
    if not port_free(HOST, port) and is_our_app(HOST, port):
        log.info("the simulator is already running on port %d; opening a window onto it", port)
        url = f"http://{HOST}:{port}"
    else:
        port = pick_port(HOST, args.port)
        server = ServerSupervisor(lambda: app_factory(args.tunnel), HOST, port)
        try:
            server.start()
        except Exception as e:
            log.error("could not start the server: %s", e)
            return 1
        url = server.url
    log.info("serving at %s", url)

    try:
        if webview_module is None and not args.browser:
            try:
                import webview as webview_module  # type: ignore
            except ImportError:
                log.error("pywebview is not installed. Run:  pip install -r requirements.txt")
                if not sys.stdout.isatty():
                    return 2
                args.browser = True          # started from a terminal: fall back to the browser
        if args.browser:
            webbrowser.open(url)
            print(f"\n  {APP_NAME} -> {url}   (Ctrl+C to quit)\n")
            wait()
            return 0

        mac_polish()
        webview_module.create_window(
            APP_NAME, url, width=1280, height=860, min_size=(960, 640), text_select=True,
            confirm_close=True, js_api=WindowApi(), background_color="#0f1115",
        )
        webview_module.start(
            debug=args.debug, private_mode=False,            # keep localStorage (tabs, preferences) between launches
            storage_path=str(storage_dir()), localization={"global.quitConfirmation": QUIT_TEXT},
        )
        return 0
    finally:
        if server is not None:
            log.info("shutting down")
            server.stop()
