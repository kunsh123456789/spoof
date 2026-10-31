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

from .device import Backend, DeviceManager, JourneyPlan, PmdBackend, Stop
from .errors import AppError
from .favorites import FavoritesStore
from .geocode import Geocoder
from .movement import validate_coords
from .recents import RecentsStore
from .routing import Router
from .trickortreat import HouseFinder

log = logging.getLogger("locspoof.server")
ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"


class Coords(BaseModel):
    lat: float
    lon: float


class LocationRequest(Coords):
    label: Optional[str] = None


class StopModel(Coords):
    dwell_s: float = 0.0


class JourneyRequest(BaseModel):
    stops: list[StopModel]
    mode: str = "walk"
    speed_kmh: float = Field(default=5.0)
    loop: bool = False
    jump_to_first: bool = False
    kind: str = "route"
    speed_jitter: float = 0.0


class SpeedRequest(BaseModel):
    speed_kmh: float


class FavoriteRequest(BaseModel):
    name: str
    lat: Optional[float] = None
    lon: Optional[float] = None
    query: Optional[str] = None   # a place name to look up instead of coordinates


class TrickOrTreatRequest(Coords):
    radius_m: int = 350
    houses: int = 20


def create_app(
    backend: Optional[Backend] = None,
    favorites_path: Optional[Path] = None,
    geocoder: Optional[Geocoder] = None,
    router: Optional[Router] = None,
    house_finder: Optional[HouseFinder] = None,
    recents_path: Optional[Path] = None,
    tunnel_mode: str = "auto",
    walk_tick: float = 1.0,
) -> FastAPI:
    favorites = FavoritesStore(favorites_path or ROOT / "favorites.json")
    recents = RecentsStore(recents_path or ROOT / "recent.json")
    manager = DeviceManager(
        backend or PmdBackend(tunnel_mode), walk_tick=walk_tick,
        on_finish=lambda lat, lon: recents.add(lat, lon, "Route end"),
    )
    geo = geocoder or Geocoder()
    road_router = router or Router()
    houses = house_finder or HouseFinder()

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
    async def set_location(body: LocationRequest):
        validate_coords(body.lat, body.lon)
        result = await manager.set_location(body.lat, body.lon)
        recents.add(body.lat, body.lon, body.label)
        return result

    @app.delete("/api/location")
    async def clear_location():
        return await manager.clear_location()

    @app.post("/api/journey")
    async def start_journey(body: JourneyRequest):
        plan = JourneyPlan(
            stops=[Stop(s.lat, s.lon, s.dwell_s) for s in body.stops], mode=body.mode, speed_kmh=body.speed_kmh,
            loop=body.loop, jump_to_first=body.jump_to_first, kind=body.kind, speed_jitter=body.speed_jitter,
        )
        result = await manager.start_journey(plan, road_router)
        if body.jump_to_first:
            recents.add(body.stops[0].lat, body.stops[0].lon, "Trick-or-treat start" if body.kind == "trick_or_treat" else None)
        return {**result, "route": manager.route_points()}

    @app.get("/api/journey/route")
    async def journey_route():
        return {"route": manager.route_points()}

    @app.patch("/api/journey")
    async def journey_speed(body: SpeedRequest):
        return manager.set_walk_speed(body.speed_kmh)

    @app.delete("/api/journey")
    async def journey_stop():
        return manager.stop_walk()

    @app.post("/api/trick-or-treat/plan")
    async def trick_or_treat_plan(body: TrickOrTreatRequest):
        validate_coords(body.lat, body.lon)
        if not 100 <= body.radius_m <= 1500:
            raise AppError("bad_radius", "Radius must be between 100 and 1500 m.", status=422)
        if not 3 <= body.houses <= 60:
            raise AppError("bad_count", "Number of houses must be between 3 and 60.", status=422)
        plan = await houses.plan(body.lat, body.lon, body.radius_m, body.houses)
        return {"houses": plan}

    @app.get("/api/recent")
    async def list_recent():
        return {"recent": recents.list()}

    @app.delete("/api/recent")
    async def clear_recent():
        recents.clear()
        return {"ok": True}

    @app.get("/api/search")
    async def search(q: str):
        return {"results": await geo.search(q)}

    @app.get("/api/favorites")
    async def list_favorites():
        return {"favorites": favorites.list()}

    @app.post("/api/favorites")
    async def add_favorite(body: FavoriteRequest):
        lat, lon = body.lat, body.lon
        if lat is None or lon is None:
            if not (body.query and body.query.strip()):
                raise AppError("bad_coords", "Give coordinates or a place to look up.", status=422)
            found = await geo.search(body.query, limit=1)
            if not found:
                raise AppError("not_found", f"No place found for \"{body.query}\".", "Try a different spelling.", 404)
            lat, lon = found[0]["lat"], found[0]["lon"]
        return favorites.add(body.name, lat, lon)

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
