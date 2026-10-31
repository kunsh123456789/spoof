"""Start the iPhone location simulator:  python app.py  ->  http://localhost:8000"""
import argparse
import logging

import uvicorn

from locspoof.server import create_app


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--host", default="127.0.0.1", help="keep 127.0.0.1: the app has no authentication")
    p.add_argument(
        "--tunnel",
        choices=["auto", "tunneld", "userspace"],
        default="auto",
        help="iOS 17+ tunnel: auto = use a running tunneld, else the no-root in-process tunnel",
    )
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    print(f"\n  iPhone Location Simulator -> http://localhost:{args.port}\n")
    uvicorn.run(create_app(tunnel_mode=args.tunnel), host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
