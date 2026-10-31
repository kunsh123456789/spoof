"""FastAPI application: JSON API under /api plus the static single-page UI."""
from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, Header, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .device import Backend, DeviceManager, JourneyPlan, PmdBackend, Stop, norm_udid
from .fleet import Fleet
from .memory import DeviceMemory
from .activity import ActivityLog
from .awake import KeepAwake, SleepWatch
from .errors import AppError
from .favorites import FavoritesStore
from .gpx import MAX_BYTES as GPX_MAX_BYTES, parse_gpx
from .geocode import Geocoder
import httpx

from .movement import haversine_m, point_in_polygon, polygon_area_m2, polygon_centroid, validate_coords
from .offline import PNG_SIGNATURE, OfflineDownloader, OfflineStore, fetch_tile, is_png
from .recents import RecentsStore
from .routes import RoutesStore
from .routing import Router
from .schedule import SchedulesStore, Scheduler, clean_plan
from . import battery as battery_mod
from . import signals as signals_mod
from .california import CA_BOX
from .panic import PanicStore
from .remote import RemoteListener, RemoteStore, bonjour_name, check_request, is_local, lan_addresses, tailscale_info
from .settings import SettingsStore
from .trickortreat import HouseFinder

log = logging.getLogger("locspoof.server")
ROOT = Path(__file__).resolve().parent.parent
MAX_BODY_BYTES = 25 * 1024 * 1024
STATIC = ROOT / "static"


class Coords(BaseModel):
    lat: float
    lon: float


class LocationRequest(Coords):
    label: Optional[str] = None


class StopModel(Coords):
    dwell_s: float = 0.0
    mode: Optional[str] = None
    speed_kmh: Optional[float] = None
    label: Optional[str] = None


class JourneyRequest(BaseModel):
    stops: list[StopModel]
    mode: str = "walk"
    speed_kmh: float = Field(default=5.0)
    loop: bool = False
    jump_to_first: bool = False
    kind: str = "route"
    speed_jitter: float = 0.0
    return_to_start: bool = False
    path: Optional[list[list[float]]] = None   # exact GPX polyline [[lat, lon], ...]
    realism: Optional[dict[str, bool]] = None  # switches for this trip only (noise / natural_walk / traffic_stops)
    title: Optional[str] = None                # a name for the whole trip (shown in the progress bar)


class RouteRequest(BaseModel):
    name: str
    kind: str = "stops"
    stops: list[StopModel]
    path: Optional[list[list[float]]] = None
    mode: str = "walk"
    speed_kmh: float = 5.0
    loop: bool = False
    return_to_start: bool = False


class RouteStartRequest(BaseModel):
    jump_to_first: Optional[bool] = None


class SpeedRequest(BaseModel):
    speed_kmh: float


class FavoriteRequest(BaseModel):
    name: str
    lat: Optional[float] = None
    lon: Optional[float] = None
    query: Optional[str] = None   # a place name to look up instead of coordinates


MAX_AREA_M2 = 4_000_000  # 4 km^2: keeps the free Overpass query small and the tour walkable


class BBoxModel(BaseModel):
    south: float
    west: float
    north: float
    east: float

    def tup(self):
        return self.south, self.west, self.north, self.east


class OfflineRequest(BaseModel):
    name: str = ""
    bbox: BBoxModel


class ScheduleRequest(BaseModel):
    name: str
    device: str = ""
    grace_min: int = 10
    steps: list[dict]


class ArmRequest(BaseModel):
    armed: bool


class PanicRequest(BaseModel):
    lat: float
    lon: float
    name: str = ""


class PreviewRequest(BaseModel):
    points: list[list[float]]
    mode: str = "walk"


class TrickOrTreatRequest(BaseModel):
    lat: Optional[float] = None
    lon: Optional[float] = None
    radius_m: int = 350            # used only when no polygon is drawn
    houses: int = 20
    polygon: Optional[list[list[float]]] = None  # [[lat, lon], ...] the area to trick-or-treat in


def create_app(
    backend: Optional[Backend] = None,
    favorites_path: Optional[Path] = None,
    geocoder: Optional[Geocoder] = None,
    router: Optional[Router] = None,
    house_finder: Optional[HouseFinder] = None,
    recents_path: Optional[Path] = None,
    offline_root: Optional[Path] = None,
    settings_path: Optional[Path] = None,
    devices_path: Optional[Path] = None,
    backend_factory=None,
    discovery: Optional[Backend] = None,
    routes_path: Optional[Path] = None,
    schedules_path: Optional[Path] = None,
    schedule_clock=time.time,
    schedule_tick: float = 1.0,
    schedule_retry: float = 15.0,
    keep_awake: Optional[KeepAwake] = None,
    sleep_watch: Optional[SleepWatch] = None,
    awake_interval: float = 2.5,
    activity_path: Optional[Path] = None,
    panic_path: Optional[Path] = None,
    remote_path: Optional[Path] = None,
    remote_listener=None,
    ambient_tick: float = 2.5,
    rng=None,
    tile_client: Optional[httpx.AsyncClient] = None,
    tile_delay: float = 0.3,
    tunnel_mode: str = "auto",
    walk_tick: float = 1.0,
) -> FastAPI:
    favorites = FavoritesStore(favorites_path or ROOT / "favorites.json")
    recents = RecentsStore(recents_path or ROOT / "recent.json")
    settings = SettingsStore(settings_path or ROOT / "settings.json")
    routes = RoutesStore(routes_path or ROOT / "routes.json")
    schedules = SchedulesStore(schedules_path or ROOT / "schedules.json")
    activity = ActivityLog(activity_path or ROOT / "activity.json")
    panic = PanicStore(panic_path or ROOT / "panic.json")
    remote_store = RemoteStore(remote_path or ROOT / "remote.json")
    awake = keep_awake if keep_awake is not None else KeepAwake()
    watch = sleep_watch if sleep_watch is not None else SleepWatch()
    memory = DeviceMemory(devices_path or ROOT / "devices.json")

    async def signal_source(bbox):
        """Real stop signs / traffic lights in a box, from OpenStreetMap (None = not available: random stops are used)."""
        query = getattr(houses, "query_json", None)
        return signals_mod.parse(await query(signals_mod.build_query(bbox))) if query else None

    def make_manager(be: Backend) -> DeviceManager:
        return DeviceManager(
            be, walk_tick=walk_tick, settings=settings.data, ambient_tick=ambient_tick, rng=rng, memory=memory, activity=activity, signal_source=signal_source,
            on_finish=lambda lat, lon: recents.add(lat, lon, "Route end"),
        )

    if backend is not None:                       # one injected backend (tests): a single phone
        single = make_manager(backend)
        fleet = Fleet(backend, lambda u: backend, make_manager, single=single)
    elif backend_factory is not None:             # injected per-phone backends (multi-phone tests)
        fleet = Fleet(discovery or backend_factory(None), backend_factory, make_manager)
        single = None
    else:
        fleet = Fleet(PmdBackend(tunnel_mode), lambda u: PmdBackend(tunnel_mode, udid=u), make_manager)
        single = None
    geo = geocoder or Geocoder()
    offline = OfflineStore(offline_root or ROOT / "offline")
    road_router = router or Router(offline=offline)
    houses = house_finder or HouseFinder(offline=offline)
    downloader = OfflineDownloader(offline, houses, client=tile_client, tile_delay=tile_delay)
    downloader.on_event = lambda text, level: activity.add(text, kind="offline", level=level)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await asyncio.to_thread(remote_apply)
        await scheduler.start()
        awake_task = asyncio.create_task(awake_loop(), name="locspoof-awake")
        yield
        awake_task.cancel()
        await asyncio.wait({awake_task}, timeout=3)
        await asyncio.to_thread(remote.stop)
        await scheduler.stop()
        awake.close()
        # Closing the session also ends an iOS 17+ simulation; the phones return to real GPS.
        await fleet.shutdown()

    app = FastAPI(title="iPhone Location Simulator", lifespan=lifespan)
    app.state.manager = single
    app.state.fleet = fleet
    remote = remote_listener if remote_listener is not None else RemoteListener(app)
    app.state.remote = remote

    def remote_apply() -> None:
        """Open or close the phone-remote listener to match the settings (runs in a thread: opening a port can take a moment)."""
        want = bool(settings.data.get("remote_enabled"))
        port = int(settings.data.get("remote_port", 8787))
        if want and (not remote.running or remote.port != port):
            if remote.running:
                remote.stop()
            remote.start(port)
        elif not want and (remote.running or remote.thread):
            remote.stop()

    @app.middleware("http")
    async def lan_guard(request: Request, call_next):
        """Requests from this Mac behave as always. Anything arriving over the network must be a paired phone doing something
        on the short allow-list (see remote.py)."""
        host = request.client.host if request.client else ""
        if is_local(host):
            return await call_next(request)
        if not settings.data.get("remote_enabled"):
            return JSONResponse({"error": {"code": "remote_off", "message": "The phone remote is off.", "hint": None}}, status_code=403)
        bad = check_request(remote_store, request.method, request.url.path, request.headers.get("authorization", ""), request.query_params.get("t", ""), host)
        if bad:
            return JSONResponse({"error": {"code": "forbidden" if bad[0] == 403 else "unauthorized", "message": bad[1], "hint": bad[2]}}, status_code=bad[0])
        return await call_next(request)

    async def get_manager(x_device: Optional[str] = Header(default=None)) -> DeviceManager:
        """The phone a request is about: the X-Device header (a UDID), else the first phone found."""
        return await fleet.get(x_device)


    @app.middleware("http")
    async def limit_body(request: Request, call_next):
        # Nothing legitimate is anywhere near this big; refusing early keeps one bad request from eating memory.
        length = request.headers.get("content-length", "")
        if length.isdigit() and int(length) > MAX_BODY_BYTES:
            return JSONResponse({"error": {"code": "too_large", "message": "That request is too large.",
                                           "hint": "Files over 25 MB are not accepted."}}, status_code=413)
        return await call_next(request)

    @app.middleware("http")
    async def no_cache(request: Request, call_next):
        # The UI files change between versions; never let the browser show a stale copy.
        resp = await call_next(request)
        if request.url.path == "/" or request.url.path.startswith("/static"):
            resp.headers["Cache-Control"] = "no-cache"
        return resp

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
    async def status(manager: DeviceManager = Depends(get_manager)):
        return await manager.status()

    @app.post("/api/trust")
    async def trust(manager: DeviceManager = Depends(get_manager)):
        await manager.trust()
        return await manager.status(force=True)

    @app.post("/api/location")
    async def set_location(body: LocationRequest, manager: DeviceManager = Depends(get_manager)):
        validate_coords(body.lat, body.lon)
        result = await manager.set_location(body.lat, body.lon, body.label)
        recents.add(body.lat, body.lon, body.label)
        return result

    @app.delete("/api/location")
    async def clear_location(manager: DeviceManager = Depends(get_manager)):
        return await manager.clear_location()

    def plan_from(body: JourneyRequest) -> JourneyPlan:
        stops = [Stop(x.lat, x.lon, x.dwell_s, x.mode or None, x.speed_kmh or None, (x.label or None)) for x in body.stops]
        path = None
        if body.path:
            path = [tuple(p) for p in body.path if len(p) == 2]
            if len(path) != len(body.path):
                raise AppError("bad_path", "The track has an invalid point.", status=422)
            stops = stops or [Stop(path[-1][0], path[-1][1])]
        return JourneyPlan(
            stops=stops, mode=body.mode, speed_kmh=body.speed_kmh, loop=body.loop, jump_to_first=body.jump_to_first,
            kind=body.kind, speed_jitter=body.speed_jitter, return_to_start=body.return_to_start, path=path, realism=body.realism,
            title=(body.title or None),
        )

    def route_request(r: dict, jump: bool) -> JourneyRequest:
        """A saved route as a journey request."""
        return JourneyRequest(
            stops=[StopModel(**{k: v for k, v in s_.items() if k in StopModel.model_fields}) for s_ in r["stops"]],
            path=r.get("path"), mode=r["mode"], speed_kmh=r["speed_kmh"], loop=r.get("loop", False),
            return_to_start=r.get("return_to_start", False), jump_to_first=jump, title=r.get("name"),
        )

    async def run_journey(body: JourneyRequest, manager: DeviceManager):
        result = await manager.start_journey(plan_from(body), road_router)
        if body.jump_to_first:
            first = body.path[0] if body.path else [body.stops[0].lat, body.stops[0].lon]
            recents.add(first[0], first[1], "Trick-or-treat start" if body.kind == "trick_or_treat" else None)
        return {**result, "route": manager.route_points()}

    @app.post("/api/journey")
    async def start_journey(body: JourneyRequest, manager: DeviceManager = Depends(get_manager)):
        return await run_journey(body, manager)

    @app.post("/api/journey/return")
    async def journey_return(manager: DeviceManager = Depends(get_manager)):
        result = await manager.return_to_origin(road_router)
        return {**result, "route": manager.route_points()}

    @app.get("/api/journey/route")
    async def journey_route(manager: DeviceManager = Depends(get_manager)):
        return {"route": manager.route_points()}

    @app.patch("/api/journey")
    async def journey_speed(body: SpeedRequest, manager: DeviceManager = Depends(get_manager)):
        return manager.set_walk_speed(body.speed_kmh)

    @app.delete("/api/journey")
    async def journey_stop(manager: DeviceManager = Depends(get_manager)):
        return await manager.stop_walk()

    async def plan_tot(body: TrickOrTreatRequest, opts: dict) -> dict:
        """Find houses for a trick-or-treat walk. ``opts``: realistic / kids / avoid_busy (global switches, or a
        scheduled step's own choices)."""
        if not 3 <= body.houses <= 60:
            raise AppError("bad_count", "Number of houses must be between 3 and 60.", status=422)
        polygon = None
        if body.polygon:
            if not 3 <= len(body.polygon) <= 100 or any(len(p) != 2 for p in body.polygon):
                raise AppError("bad_area", "The area needs 3 to 100 points.", status=422)
            polygon = [(p[0], p[1]) for p in body.polygon]
            for la, lo in polygon:
                validate_coords(la, lo)
            area = polygon_area_m2(polygon)
            if area < 2_000:
                raise AppError("area_too_small", "That area is tiny.", "Draw a larger area around several houses.", 422)
            if area > MAX_AREA_M2:
                raise AppError(
                    "area_too_big", f"That area is too big ({area / 1e6:.1f} km²).",
                    f"Draw a smaller area (at most {MAX_AREA_M2 / 1e6:.0f} km²).", 422,
                )
            if body.lat is not None and body.lon is not None and point_in_polygon(body.lat, body.lon, polygon):
                lat, lon = body.lat, body.lon       # start near the pin if it is inside the area
            else:
                lat, lon = polygon_centroid(polygon)
        else:
            if body.lat is None or body.lon is None:
                raise AppError("no_pin", "Drop a pin or draw an area first.", status=422)
            validate_coords(body.lat, body.lon)
            if not 100 <= body.radius_m <= 1500:
                raise AppError("bad_radius", "Radius must be between 100 and 1500 m.", status=422)
            lat, lon = body.lat, body.lon
        lo, hi = settings.data["tot_dwell_min_s"], settings.data["tot_dwell_max_s"]
        plan = await houses.plan(lat, lon, body.radius_m, body.houses, polygon=polygon, dwell_range=(lo, max(lo, hi)), **opts)
        kids = opts["kids"]
        return {
            "houses": plan, "source": getattr(houses, "last_source", "online"), "notes": getattr(houses, "last_notes", []),
            # recommended pace for the walk: a group of kids dawdles
            "pace": {"speed_kmh": 3.2 if kids else 4.5, "speed_jitter": 0.3 if kids else 0.2},
        }

    @app.post("/api/trick-or-treat/plan")
    async def trick_or_treat_plan(body: TrickOrTreatRequest):
        opts = {"realistic": settings.data["tot_realistic"], "kids": settings.data["tot_kids"],
                "avoid_busy": settings.data["tot_avoid_busy"]}
        return await plan_tot(body, opts)

    # ---- timed plans ----
    async def execute_step(plan: dict, step: dict) -> str:
        """Do one scheduled step through the same code paths as the buttons in the UI."""
        manager = await fleet.get(plan.get("device") or None)
        t, p = step["type"], step["params"]
        if t == "set_location":
            await manager.set_location(p["lat"], p["lon"], p.get("label") or None)
            recents.add(p["lat"], p["lon"], p.get("label") or None)
            return "Location set to " + (p.get("label") or "%.5f, %.5f" % (p["lat"], p["lon"])) + "."
        if t == "move_to":
            if p.get("start"):
                await manager.set_location(p["start"]["lat"], p["start"]["lon"])
            req = JourneyRequest(stops=[StopModel(lat=p["lat"], lon=p["lon"], label=p.get("label") or None)], mode=p["mode"], speed_kmh=p["speed_kmh"],
                                 realism={"noise": True, "natural_walk": True} if p.get("shaky") else None)
            await run_journey(req, manager)
            return "Heading to %s at %g km/h (%s%s)." % (p.get("label") or "the destination", p["speed_kmh"], p["mode"], ", shaky" if p.get("shaky") else "")
        if t == "route":
            r = routes.get(p["route_id"])
            if not r:
                raise AppError("not_found", "The saved route for this step no longer exists.", "Edit the plan and choose another route.", 404)
            await run_journey(route_request(r, p.get("jump", True)), manager)
            return "Route “%s” started." % r["name"]
        if t == "trick_or_treat":
            body = TrickOrTreatRequest(lat=p.get("lat"), lon=p.get("lon"), radius_m=p["radius_m"], houses=p["houses"], polygon=p.get("polygon"))
            res = await plan_tot(body, {"realistic": p["realistic"], "kids": p["kids"], "avoid_busy": p["avoid_busy"]})
            visit = [h for h in res["houses"] if not h["lights_off"]]
            req = JourneyRequest(
                stops=[StopModel(lat=h["lat"], lon=h["lon"], dwell_s=h["dwell_s"], label="house %d" % (i + 1)) for i, h in enumerate(visit)], mode="walk",
                speed_kmh=res["pace"]["speed_kmh"], jump_to_first=True, kind="trick_or_treat", speed_jitter=res["pace"]["speed_jitter"],
            )
            await run_journey(req, manager)
            return "Trick-or-treat started: %d houses.%s" % (len(visit), (" " + " ".join(res["notes"])) if res.get("notes") else "")
        if t == "clear":
            await manager.clear_location()
            return "Real GPS restored."
        await manager.stop_walk()
        return "Stopped moving."

    def on_step_event(plan: dict, step: dict, status: str, note: str) -> None:
        verb = {"running": "started", "done": "done", "error": "failed", "missed": "missed", "skipped": "skipped"}.get(status, status)
        activity.add("⏰ %s · %s: %s%s" % (plan["name"], step["title"] or step["type"].replace("_", " "), verb, (" — " + note) if note else ""),
                     kind="schedule", level="error" if status == "error" else "warn" if status in ("missed", "skipped") else "info")

    def plan_phone_moving(plan: dict) -> bool:
        """Is the phone this plan is about still on a trip? (for steps that wait N minutes after the last move)"""
        ms = fleet.all_managers()
        want = (plan.get("device") or "").lower()
        for m in ms:
            if not want or (m._info and norm_udid(m._info.udid) == norm_udid(want)):
                if m._journey_active():
                    return True
        return False

    scheduler = Scheduler(schedules, execute_step, clock=schedule_clock, tick_every=schedule_tick, retry_every=schedule_retry, on_event=on_step_event,
                          is_moving=plan_phone_moving)
    app.state.scheduler = scheduler
    app.state.awake = awake

    awake_was = {"on": False}
    remote_was: dict[str, bool] = {}

    async def awake_tick() -> None:
        """Decide whether the Mac must stay awake, and notice if it slept (then re-check every phone at once)."""
        slept = watch.check()
        if slept is not None:
            activity.add("💤 The Mac was asleep for about %d min: re-checking the phone" % max(round(slept / 60), 1), kind="link", level="warn", tag="sleep")
            for m in fleet.all_managers():
                m.on_wake()
        for d in remote_store.listing():                         # note remote phones coming and going
            was = remote_was.get(d["id"])
            remote_was[d["id"]] = d["online"]
            if was is not None and was != d["online"]:
                activity.add("🎮 Remote “%s” %s%s" % (d["name"], "connected" if d["online"] else "disconnected", (" (%s)" % ("🌍 Tailscale" if d["via"] == "tailscale" else "🏠 Wi-Fi")) if d["online"] and d["via"] else ""),
                             kind="link", level="info" if d["online"] else "warn", tag="remote_up" if d["online"] else "remote_down")
        reasons: list[str] = []
        if settings.data["keep_awake_open"]:
            reasons.append("the app is open")
        for m in fleet.all_managers():
            reasons += m.awake_reasons()
        if scheduler.wants_awake():
            reasons.append("an activated schedule is waiting")
        want = settings.data["keep_awake"] and bool(reasons)
        awake.update(want, reasons, settings.data["keep_screen_on"])
        real = [r for r in reasons if r != "the app is open"]          # "just open" is always on: not worth a log line each time
        want_real = settings.data["keep_awake"] and bool(real)
        if want_real != awake_was["on"]:
            awake_was["on"] = want_real
            activity.add("☕ Keeping the Mac awake: %s" % ", ".join(real) if want_real else "Nothing needs the Mac awake now", kind="mac")

    async def awake_loop() -> None:
        while True:
            try:
                await awake_tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("keep-awake check failed")
            await asyncio.sleep(awake_interval)

    @app.get("/api/awake")
    async def awake_state():
        st = awake.state()
        st["enabled"] = bool(settings.data["keep_awake"])
        st["last_sleep"] = watch.last_sleep
        return st

    @app.get("/api/battery")
    async def get_battery():
        return await asyncio.to_thread(battery_mod.read)

    @app.get("/api/schedules")
    async def list_schedules():
        return {"plans": schedules.list(), "now": schedule_clock(), "next": scheduler.next_event()}

    @app.post("/api/schedules")
    async def create_schedule(body: ScheduleRequest):
        return schedules.create(clean_plan(body.model_dump()))

    @app.put("/api/schedules/{plan_id}")
    async def edit_schedule(plan_id: str, body: ScheduleRequest):
        plan = schedules.replace(plan_id, clean_plan(body.model_dump()))
        if plan is None:
            raise AppError("not_found", "That plan was not found.", status=404)
        return plan

    @app.delete("/api/schedules/{plan_id}")
    async def delete_schedule(plan_id: str):
        if not schedules.delete(plan_id):
            raise AppError("not_found", "That plan was not found.", status=404)
        return {"ok": True}

    @app.post("/api/schedules/{plan_id}/arm")
    async def arm_schedule(plan_id: str, body: ArmRequest):
        plan = schedules.set_armed(plan_id, body.armed, schedule_clock())
        if plan is None:
            raise AppError("not_found", "That plan was not found.", status=404)
        return plan

    @app.post("/api/schedules/{plan_id}/reset")
    async def reset_schedule(plan_id: str):
        plan = schedules.reset(plan_id)
        if plan is None:
            raise AppError("not_found", "That plan was not found.", status=404)
        return plan

    @app.post("/api/schedules/{plan_id}/steps/{step_id}/run")
    async def run_schedule_step(plan_id: str, step_id: str):
        """Try one step right now (it does not change the plan's schedule)."""
        return {"note": await scheduler.run_now(plan_id, step_id)}

    # ---- offline packs ----
    @app.get("/api/offline")
    async def offline_list():
        return {"packs": offline.list_packs(), "job": downloader.job}

    @app.post("/api/offline/estimate")
    async def offline_estimate(body: OfflineRequest):
        return downloader.estimate(body.bbox.tup())

    @app.post("/api/offline")
    async def offline_start(body: OfflineRequest):
        return {"job": await downloader.start(body.name, body.bbox.tup())}

    @app.delete("/api/offline/{pack_id}")
    async def offline_delete(pack_id: str):
        if not offline.delete(pack_id):
            raise AppError("not_found", "Offline area not found.", status=404)
        return {"ok": True}

    @app.get("/tiles/{z}/{x}/{y}.png")
    async def tile(z: int, x: int, y: int):
        """Map tiles: the saved copy if there is one, otherwise fetched live from OpenStreetMap."""
        if not (0 <= z <= 19 and 0 <= x < 2 ** z and 0 <= y < 2 ** z):
            return Response(status_code=404)
        path = offline.tile_path(z, x, y)
        if path.exists():
            if is_png(path):
                return FileResponse(path, media_type="image/png", headers={"Cache-Control": "public, max-age=86400"})
            log.warning("removing a corrupt saved tile: %s", path)       # e.g. left by a crash; fetch it fresh instead
            path.unlink(missing_ok=True)
        try:
            resp = await fetch_tile(tile_client, z, x, y)
        except httpx.HTTPError:
            return Response(status_code=404)
        if resp.status_code != 200 or not resp.content.startswith(PNG_SIGNATURE):
            return Response(status_code=404)
        return Response(resp.content, media_type="image/png", headers={"Cache-Control": "public, max-age=3600"})

    # ---- phones ----
    async def decorated_devices(fresh: bool = False) -> list[dict]:
        """Connected phones with the user's nickname / favorite star; favorites that are unplugged are listed too."""
        out = []
        for d in await fleet.devices(fresh=fresh):
            meta = memory.meta(d["udid"])
            out.append({**d, **meta, "display_name": meta["nickname"] or d["name"], "offline": False})
        seen = {norm_udid(d["udid"]) for d in out}
        for key, rec in memory.favorites():
            if key not in seen:
                meta = memory.meta(key)
                name = rec.get("name") or "Saved phone"
                out.append({"udid": key, "name": name, "ios_version": rec.get("ios_version"), "connection": None, "paired": True, **meta,
                            "display_name": meta["nickname"] or name, "offline": True})
        out.sort(key=lambda d: (not d["favorite"], d["offline"], d["display_name"].lower()))
        return out

    @app.get("/api/devices")
    async def list_devices(fresh: bool = False):
        return {"devices": await decorated_devices(fresh)}

    @app.post("/api/devices/{udid}/developer-mode")
    async def enable_developer_mode(udid: str):
        """Reveal the Developer Mode switch on a phone (iOS 16+) and ask it to turn the mode on."""
        out = await fleet.enable_developer_mode(udid)
        name = next((d["name"] for d in await fleet.devices(fresh=True) if norm_udid(d["udid"]) == norm_udid(udid)), "iPhone")
        activity.add("🛠 Developer Mode: %s on %s" % ({"already_on": "already on", "restarting": "turning on (the phone restarts)", "manual": "switch revealed (turn it on in Settings)"}[out["state"]], name), kind="phone")
        return out

    @app.patch("/api/devices/{udid}")
    async def patch_device(udid: str, body: dict):
        """Rename a phone ({"nickname": "Mum's phone"}, "" = back to its own name) and/or star it ({"favorite": true})."""
        if "nickname" in body and not isinstance(body["nickname"], str):
            raise AppError("bad_request", "The name must be text.", status=422)
        if "favorite" in body and not isinstance(body["favorite"], bool):
            raise AppError("bad_request", "favorite must be true or false.", status=422)
        if not 4 <= len(udid) <= 64:
            raise AppError("bad_request", "That is not a phone id.", status=422)
        meta = memory.set_meta(udid, body.get("nickname"), body.get("favorite"))
        return {**meta, "udid": udid}

    @app.post("/api/devices/scan")
    async def scan_wifi():
        """Look for paired iPhones on the local network (Bonjour)."""
        await fleet.scan_wifi()
        return {"devices": await decorated_devices(True)}

    @app.post("/api/wifi")
    async def set_wifi(body: dict, manager: DeviceManager = Depends(get_manager)):
        if not isinstance(body.get("enabled"), bool):
            raise AppError("bad_request", "Send {\"enabled\": true or false}.", status=422)
        await manager.set_wifi(body["enabled"])
        return await manager.status(force=True)

    # ---- panic button: jump to one chosen safe spot ----
    @app.get("/api/panic")
    async def get_panic():
        return {"location": panic.get()}

    @app.put("/api/panic")
    async def put_panic(body: PanicRequest):
        item = panic.set(body.lat, body.lon, body.name)
        activity.add("🚨 Panic location set to %s" % (item["name"] or "%.5f, %.5f" % (item["lat"], item["lon"])), kind="location")
        return {"location": item}

    @app.delete("/api/panic")
    async def delete_panic():
        panic.clear()
        return {"location": None}

    @app.post("/api/panic/go")
    async def panic_go(manager: DeviceManager = Depends(get_manager)):
        spot = panic.get()
        if spot is None:
            raise AppError("no_panic", "No panic location chosen yet.", "Pick one in the Saved tab (“Panic location”) first.", 409)
        name = spot["name"] or "the panic location"
        activity.add("🚨 PANIC: stopped everything and moved to %s" % name, kind="location", level="warn")
        result = await manager.set_location(spot["lat"], spot["lon"], name)     # also stops any trip
        recents.add(spot["lat"], spot["lon"], name)
        return result

    SNAP_MAX_M = 150.0                    # farther than this the spot is probably a park / field: leave the pin where it is

    @app.get("/api/snap")
    async def snap(lat: float, lon: float, mode: str = "walk"):
        """Move a spot to the nearest road or path (best effort: if the service can't say, the spot stays)."""
        validate_coords(lat, lon)
        none = {"lat": lat, "lon": lon, "moved_m": 0.0, "snapped": False}
        if not settings.data["snap_pin"]:
            return none
        finder = getattr(road_router, "nearest", None)
        try:
            hit = await asyncio.wait_for(finder(lat, lon, mode), 12) if finder else None
        except Exception:
            hit = None
        if hit is None or hit[2] > SNAP_MAX_M:
            return none
        moved = haversine_m(lat, lon, hit[0], hit[1])
        return {"lat": hit[0], "lon": hit[1], "moved_m": round(moved, 1), "snapped": moved >= 1.0}

    # ---- preview of the road a trip will take (drawn with arrows) ----
    @app.post("/api/route-preview")
    async def route_preview(body: PreviewRequest):
        pts = [tuple(p) for p in body.points if len(p) == 2]
        if not 2 <= len(pts) <= 61 or len(pts) != len(body.points):
            raise AppError("bad_stops", "A preview needs 2 to 61 points.", status=422)
        if body.mode not in ("straight", "walk", "drive"):
            raise AppError("bad_mode", "Mode must be straight, walk or drive.", status=422)
        for la, lo in pts:
            validate_coords(la, lo)
        if body.mode == "straight":
            legs = [[list(a), list(b)] for a, b in zip(pts, pts[1:])]
        else:
            legs = [[list(p) for p in leg] for leg in await road_router.route(pts, body.mode)]
        return {"route": [p for leg in legs for p in leg], "legs": legs}

    @app.get("/api/settings")
    async def get_settings():
        return settings.data

    @app.patch("/api/settings")
    async def patch_settings(body: dict):
        out = settings.update(body)
        if "remote_enabled" in body or "remote_port" in body:
            await asyncio.to_thread(remote_apply)
        return out

    # ---- phone remote (managed from this Mac; the phone only pairs, and asks who it is) ----
    @app.get("/api/remote")
    async def remote_state():
        port = int(settings.data.get("remote_port", 8787))
        ts = await asyncio.to_thread(tailscale_info)
        urls = []
        if ts["running"]:                                  # works from anywhere (and at home too)
            urls += [{"url": f"http://{ts['dns']}:{port}/remote", "kind": "tailscale"}] if ts["dns"] else []
            urls += [{"url": f"http://{ip}:{port}/remote", "kind": "tailscale"} for ip in ts["ips"]]
        bn = bonjour_name()
        if bn:
            urls.append({"url": f"http://{bn}:{port}/remote", "kind": "wifi"})
        urls += [{"url": f"http://{a}:{port}/remote", "kind": "wifi"} for a in lan_addresses() if a not in ts["ips"]]
        return {"enabled": bool(settings.data.get("remote_enabled")), "running": bool(remote.running), "port": port, "error": remote.error,
                "urls": urls, "tailscale": {"installed": ts["installed"], "running": ts["running"]}, "code": remote_store.current_code(), "devices": remote_store.listing()}

    @app.post("/api/remote/code")
    async def remote_new_code():
        return remote_store.new_code()

    @app.delete("/api/remote/devices/{device_id}")
    async def remote_revoke(device_id: str):
        if not remote_store.revoke(device_id):
            raise AppError("not_found", "That phone is not paired.", status=404)
        return {"ok": True}

    @app.patch("/api/remote/devices/{device_id}")
    async def remote_rename(device_id: str, body: dict):
        if not remote_store.rename(device_id, str(body.get("name", ""))):
            raise AppError("not_found", "That phone is not paired.", status=404)
        return {"ok": True}

    @app.post("/api/remote/pair")
    async def remote_pair(body: dict):
        token = remote_store.pair(str(body.get("code", "")), str(body.get("name", "")))
        activity.add("📱 A phone was paired with the remote: %s" % (str(body.get("name") or "Phone")[:40]), kind="remote")
        return {"token": token}

    @app.get("/api/remote/me")
    async def remote_me():
        return {"ok": True}

    @app.get("/remote")
    async def remote_page():
        return FileResponse(STATIC / "remote.html")

    @app.get("/remote.webmanifest")
    async def remote_manifest():
        return JSONResponse({"name": "iPhone Location Remote", "short_name": "Location", "start_url": "/remote", "scope": "/", "display": "standalone",
                             "background_color": "#0b1220", "theme_color": "#0b1220", "icons": [{"src": "/static/icon.png", "sizes": "512x512", "type": "image/png"}]},
                            media_type="application/manifest+json")

    @app.post("/api/gpx")
    async def gpx_import(request: Request):
        data = await request.body()
        return parse_gpx(data[: GPX_MAX_BYTES + 1])

    @app.get("/api/routes")
    async def list_routes():
        return {"routes": routes.list()}

    @app.post("/api/routes")
    async def save_route(body: RouteRequest):
        stops = [x.model_dump() for x in body.stops]
        return routes.add(body.name, body.kind, stops, body.path, body.mode, body.speed_kmh, body.loop, body.return_to_start)

    @app.delete("/api/routes/{route_id}")
    async def delete_route(route_id: str):
        if not routes.delete(route_id):
            raise AppError("not_found", "Saved route not found.", status=404)
        return {"ok": True}

    @app.post("/api/routes/{route_id}/start")
    async def start_saved_route(route_id: str, body: RouteStartRequest, manager: DeviceManager = Depends(get_manager)):
        r = routes.get(route_id)
        if not r:
            raise AppError("not_found", "Saved route not found.", status=404)
        jump = body.jump_to_first if body.jump_to_first is not None else False
        return await run_journey(route_request(r, jump), manager)

    @app.get("/api/reverse")
    async def reverse(lat: float, lon: float):
        """What is at this spot? Used to show an address in the progress bar."""
        validate_coords(lat, lon)
        return await geo.reverse(lat, lon)

    @app.get("/api/activity")
    async def get_activity(after: int = 0, kinds: str = ""):
        wanted = {k for k in kinds.split(",") if k} or None
        return {"events": activity.list(after, kinds=wanted), "last_id": activity.last_id}

    @app.delete("/api/activity")
    async def clear_activity():
        activity.clear()
        return {"ok": True}

    @app.get("/api/recent")
    async def list_recent():
        return {"recent": recents.list()}

    @app.delete("/api/recent")
    async def clear_recent():
        recents.clear()
        return {"ok": True}

    @app.get("/api/search")
    async def search(q: str):
        return {"results": await geo.search(q, box=CA_BOX if settings.data["california_only"] else None)}

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
