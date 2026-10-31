"""FastAPI application: JSON API under /api plus the static single-page UI."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .device import Backend, DeviceManager, PmdBackend
from .errors import AppError
from .favorites import FavoritesStore
from .geocode import Geocoder
from .movement import validate_coords

log = logging.getLogger("locspoof.server")
ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"


class Coords(BaseModel):
    lat: float
    lon: float


class WalkRequest(Coords):
    speed_kmh: float = Field(default=5.0)


class SpeedRequest(BaseModel):
    speed_kmh: float


class FavoriteRequest(Coords):
    name: str


def create_app(
    backend: Optional[Backend] = None,
    favorites_path: Optional[Path] = None,
    geocoder: Optional[Geocoder] = None,
    tunnel_mode: str = "auto",
    walk_tick: float = 1.0,
) -> FastAPI:
    manager = DeviceManager(backend or PmdBackend(tunnel_mode), walk_tick=walk_tick)
    favorites = FavoritesStore(favorites_path or ROOT / "favorites.json")
    geo = geocoder or Geocoder()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        # Closing the session also ends an iOS 17+ simulation; the phone returns to real GPS.
        await manager.shutdown()

    app = FastAPI(title="iPhone Location Simulator", lifespan=lifespan)
    app.state.manager = manager

    @app.exception_handler(AppError)
    async def app_error_handler(_: Request, exc: AppError):
        log.warning("%s: %s", exc.code, exc.message)
        return JSONResponse({"error": exc.to_dict()}, status_code=exc.status)

    @app.exception_handler(ValueError)
    async def value_error_handler(_: Request, exc: ValueError):
        return JSONResponse({"error": {"code": "bad_request", "message": str(exc), "hint": None}}, status_code=422)

    @app.exception_handler(Exception)
    async def unhandled(_: Request, exc: Exception):
        log.exception("unhandled error")
        return JSONResponse(
            {"error": {"code": "internal", "message": "Something went wrong.", "hint": "See the terminal log."}},
            status_code=500,
        )

    @app.get("/api/status")
    async def status():
        return await manager.status()

    @app.post("/api/trust")
    async def trust():
        await manager.trust()
        return await manager.status(force=True)

    @app.post("/api/location")
    async def set_location(body: Coords):
        validate_coords(body.lat, body.lon)
        return await manager.set_location(body.lat, body.lon)

    @app.delete("/api/location")
    async def clear_location():
        return await manager.clear_location()

    @app.post("/api/walk")
    async def walk(body: WalkRequest):
        return await manager.start_walk(body.lat, body.lon, body.speed_kmh)

    @app.patch("/api/walk")
    async def walk_speed(body: SpeedRequest):
        return manager.set_walk_speed(body.speed_kmh)

    @app.delete("/api/walk")
    async def walk_stop():
        return manager.stop_walk()

    @app.get("/api/search")
    async def search(q: str):
        return {"results": await geo.search(q)}

    @app.get("/api/favorites")
    async def list_favorites():
        return {"favorites": favorites.list()}

    @app.post("/api/favorites")
    async def add_favorite(body: FavoriteRequest):
        return favorites.add(body.name, body.lat, body.lon)

    @app.delete("/api/favorites/{fav_id}")
    async def delete_favorite(fav_id: str):
        if not favorites.delete(fav_id):
            raise AppError("not_found", "Favorite not found.", status=404)
        return {"ok": True}

    @app.get("/")
    async def index():
        return FileResponse(STATIC / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app
