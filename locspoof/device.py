"""Device control: detection, location sessions (iOS 16- / iOS 17+) and gradual movement.

``PmdBackend`` is the only place that talks to pymobiledevice3. ``DeviceManager`` holds the
app state (current session, simulated position, walk task) and is testable with a fake backend.

Key facts about the two simulation methods:

* iOS 17+: the location comes from the DVT "instruments" service, reached through an RSD tunnel.
  The simulation lasts only while that DVT connection stays open, so the manager keeps one
  session open until you clear the location (or quit the app).
* iOS 16 and earlier: ``com.apple.dt.simulatelocation`` over lockdown (needs the Developer Disk
  Image mounted). The phone keeps the fake location until cleared or rebooted.
"""
from __future__ import annotations

import asyncio
import itertools
import logging
import random
import time
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional, Protocol

from . import errors
from .errors import AppError
from .movement import PathFollower, validate_coords

log = logging.getLogger("locspoof.device")

WALK_TICK_S = 1.0
MIN_SPEED_KMH, MAX_SPEED_KMH = 0.5, 400.0
MAX_STOPS = 60
MAX_DWELL_S = 900.0
MODES = ('straight', 'walk', 'drive')
STATUS_CACHE_S = 2.0


@dataclass
class DeviceInfo:
    udid: str
    name: str
    ios_version: str
    product_type: str = ""

    @property
    def major(self) -> int:
        try:
            return int(self.ios_version.split(".")[0])
        except ValueError:
            return 0

    def to_dict(self) -> dict:
        return {
            "udid": self.udid,
            "name": self.name,
            "ios_version": self.ios_version,
            "product_type": self.product_type,
            "method": "dvt-tunnel (iOS 17+)" if self.major >= 17 else "lockdown (iOS 16 and earlier)",
        }


class LocationSession(Protocol):
    """An open channel to the phone's location simulator."""

    persists: bool  # does the phone keep the fake location after the session is closed?

    async def set(self, lat: float, lon: float) -> None: ...
    async def clear(self) -> None: ...
    async def close(self) -> None: ...


class Backend(Protocol):
    async def probe(self) -> DeviceInfo: ...
    async def trust(self) -> None: ...
    async def open_session(self, info: DeviceInfo) -> LocationSession: ...


# ---------------------------------------------------------------------------------------------
# pymobiledevice3 backend
# ---------------------------------------------------------------------------------------------

def translate_exception(exc: BaseException) -> AppError:
    """Map pymobiledevice3 / OS exceptions to friendly errors."""
    if isinstance(exc, AppError):
        return exc
    from pymobiledevice3 import exceptions as E

    if isinstance(exc, E.NoDeviceConnectedError):
        return errors.no_device()
    if isinstance(exc, E.DeveloperModeIsNotEnabledError):
        return errors.developer_mode_off()
    if isinstance(exc, E.PasswordRequiredError) or isinstance(exc, E.PasscodeRequiredError):
        return AppError("device_locked", "The iPhone is locked.", "Unlock the iPhone with its passcode and try again.", 409)
    if isinstance(exc, (E.NotPairedError, E.PairingError, E.NotTrustedError)):
        return errors.not_trusted()
    if isinstance(exc, E.TunneldConnectionError):
        return errors.tunnel_needed()
    if isinstance(exc, E.UserspaceTunnelUnavailableError):
        return errors.tunnel_needed()
    if isinstance(exc, E.DeveloperDiskImageNotFoundError):
        return AppError(
            "ddi_not_found",
            "No Developer Disk Image is available for this iOS version.",
            "Update pymobiledevice3 (pip install -U pymobiledevice3) or update/downgrade iOS.",
            502,
        )
    if isinstance(exc, E.InvalidServiceError):
        return AppError(
            "service_unavailable",
            "The iPhone did not offer the location simulation service.",
            "Developer Mode must be on and the Developer Disk Image mounted. Reconnect the phone and retry; "
            "on iOS 17+ also check the tunnel.",
            502,
        )
    if isinstance(exc, E.ConnectionFailedToUsbmuxdError):
        return AppError(
            "usbmuxd_unavailable",
            "Cannot reach usbmuxd (the service that talks to USB iPhones).",
            "On macOS it is built in. On Linux install/start usbmuxd; on Windows install iTunes or Apple Devices.",
            503,
        )
    if isinstance(exc, (E.ConnectionTerminatedError, ConnectionError, OSError, asyncio.TimeoutError)):
        return AppError(
            "connection_lost", "Lost the connection to the iPhone.", "Check the cable, unlock the phone and retry.", 503
        )
    return AppError("device_error", f"Unexpected device error: {exc}", "See the terminal for the full traceback.", 500)


class _LegacySession:
    """iOS 16 and earlier: lockdown ``com.apple.dt.simulatelocation``."""

    persists = True

    def __init__(self, lockdown: Any):
        self._lockdown = lockdown

    async def _svc(self):
        from pymobiledevice3.services.simulate_location import DtSimulateLocation

        return DtSimulateLocation(self._lockdown)

    async def set(self, lat: float, lon: float) -> None:
        await (await self._svc()).set(lat, lon)

    async def clear(self) -> None:
        await (await self._svc()).clear()

    async def close(self) -> None:
        await self._lockdown.close()


class _DvtSession:
    """iOS 17+: DVT LocationSimulation over an RSD tunnel; must stay open to keep the fake location."""

    persists = False

    def __init__(self, stack: AsyncExitStack, simulation: Any):
        self._stack = stack
        self._sim = simulation

    async def set(self, lat: float, lon: float) -> None:
        await self._sim.set(lat, lon)

    async def clear(self) -> None:
        await self._sim.clear()

    async def close(self) -> None:
        await self._stack.aclose()


class PmdBackend:
    def __init__(self, tunnel_mode: str = "auto"):
        if tunnel_mode not in ("auto", "tunneld", "userspace"):
            raise ValueError("tunnel_mode must be auto, tunneld or userspace")
        self.tunnel_mode = tunnel_mode
        self._udid: Optional[str] = None
        self._last_problem: Optional[str] = None

    async def _find_serial(self) -> str:
        from pymobiledevice3 import usbmux

        devices = await usbmux.list_devices()
        usb = [d for d in devices if d.is_usb]
        if not usb:
            raise errors.no_device()
        return usb[0].serial

    async def _lockdown(self, serial: str, autopair: bool = False):
        from pymobiledevice3.lockdown import create_using_usbmux

        return await create_using_usbmux(serial=serial, autopair=autopair, connection_type="USB")

    async def probe(self) -> DeviceInfo:
        try:
            serial = await self._find_serial()
            lockdown = await self._lockdown(serial)
            try:
                # create_using_usbmux() already validated the pairing and opened the session; calling
                # validate_pairing() again makes the phone answer "a lockdown session is already active".
                if not lockdown.paired:
                    raise errors.not_trusted()
                info = DeviceInfo(
                    udid=serial,
                    name=lockdown.all_values.get("DeviceName") or "iPhone",
                    ios_version=lockdown.product_version,
                    product_type=lockdown.all_values.get("ProductType") or "",
                )
                # Developer Mode exists from iOS 16; earlier versions have no such switch.
                if info.major >= 16 and not await lockdown.get_developer_mode_status():
                    raise errors.developer_mode_off()
                self._udid = serial
                self._last_problem = None
                return info
            finally:
                await lockdown.close()
        except AppError as e:
            self._note_probe_problem(e.code)
            raise
        except Exception as e:
            err = translate_exception(e)
            if self._note_probe_problem(err.code):
                log.exception("probe failed")  # full traceback once per distinct problem, not every poll
            raise err from e

    def _note_probe_problem(self, code: str) -> bool:
        new = code != self._last_problem
        if new:
            log.warning("device problem: %s", code)
        self._last_problem = code
        return new

    async def trust(self) -> None:
        """Trigger the on-phone 'Trust This Computer?' prompt and wait for the answer."""
        try:
            serial = await self._find_serial()
            lockdown = await self._lockdown(serial, autopair=False)
            try:
                if not lockdown.paired:
                    await lockdown.pair(timeout=60)
            finally:
                await lockdown.close()
        except AppError:
            raise
        except Exception as e:
            log.exception("trust failed")
            raise translate_exception(e) from e

    # -- sessions ---------------------------------------------------------------------------

    async def open_session(self, info: DeviceInfo) -> LocationSession:
        try:
            if info.major >= 17:
                return await self._open_dvt(info)
            return await self._open_legacy(info)
        except AppError:
            raise
        except Exception as e:
            log.exception("could not open location session")
            raise translate_exception(e) from e

    async def _open_legacy(self, info: DeviceInfo) -> LocationSession:
        from pymobiledevice3.exceptions import AlreadyMountedError
        from pymobiledevice3.services.mobile_image_mounter import (
            MobileImageMounterService,
            auto_mount,
            image_type_for_device,
        )

        lockdown = await self._lockdown(info.udid)
        try:
            async with MobileImageMounterService(lockdown) as mounter:
                mounted = await mounter.is_image_mounted(image_type_for_device(lockdown))
            if not mounted:
                log.info("Developer Disk Image not mounted; mounting (may download it)...")
                try:
                    await auto_mount(lockdown)
                except AlreadyMountedError:
                    pass
        except BaseException:
            await lockdown.close()
            raise
        return _LegacySession(lockdown)

    async def _open_rsd(self, udid: str) -> tuple[Any, Callable[[], Awaitable[None]]]:
        """Return (rsd, closer) using a running tunneld first, else the no-root userspace tunnel."""
        from pymobiledevice3.exceptions import TunneldConnectionError, UserspaceTunnelUnavailableError

        if self.tunnel_mode in ("auto", "tunneld"):
            from pymobiledevice3.tunneld.api import get_tunneld_device_by_udid

            try:
                rsd = await get_tunneld_device_by_udid(udid)
            except TunneldConnectionError:
                rsd = None
                log.info("tunneld is not running")
            if rsd is not None:
                log.info("using tunnel from tunneld")
                return rsd, rsd.close
            if self.tunnel_mode == "tunneld":
                raise errors.tunnel_needed()

        from pymobiledevice3.remote.userspace_tunnel import UserspaceRsdTunnel

        log.info("starting in-process userspace tunnel (no root needed, iOS 17.4+)")
        tunnel = UserspaceRsdTunnel(serial=udid, remotepairing_fallback=False)
        try:
            rsd = await tunnel.aopen()
        except UserspaceTunnelUnavailableError as e:
            log.warning("userspace tunnel unavailable: %s", e)
            raise errors.tunnel_needed() from e
        return rsd, tunnel.aclose

    async def _open_dvt(self, info: DeviceInfo) -> LocationSession:
        from pymobiledevice3.services.dvt.instruments.dvt_provider import DvtProvider
        from pymobiledevice3.services.dvt.instruments.location_simulation import LocationSimulation
        from pymobiledevice3.services.mobile_image_mounter import (
            MobileImageMounterService,
            auto_mount,
            image_type_for_device,
        )
        from pymobiledevice3.exceptions import AlreadyMountedError

        rsd, closer = await self._open_rsd(info.udid)
        stack = AsyncExitStack()
        stack.push_async_callback(closer)
        try:
            try:
                async with MobileImageMounterService(rsd) as mounter:
                    mounted = await mounter.is_image_mounted(image_type_for_device(rsd))
                if not mounted:
                    log.info("Developer Disk Image not mounted; mounting (needs internet)...")
                    try:
                        await auto_mount(rsd)
                    except AlreadyMountedError:
                        pass
            except Exception:
                # Let the DVT connect below decide; it fails clearly if the image really is missing.
                log.exception("DDI check/mount failed; continuing")
            dvt = await stack.enter_async_context(DvtProvider(rsd))
            sim = await stack.enter_async_context(LocationSimulation(dvt))
        except BaseException:
            await stack.aclose()
            raise
        return _DvtSession(stack, sim)


# ---------------------------------------------------------------------------------------------
# State manager
# ---------------------------------------------------------------------------------------------

class DeviceManager:
    def __init__(
        self, backend: Backend, walk_tick: float = WALK_TICK_S,
        on_finish: Optional[Callable[[float, float], None]] = None,
    ):
        self.backend = backend
        self.walk_tick = walk_tick
        self.on_finish = on_finish  # called with the final position when a journey completes
        self._route_points: list[list[float]] = []
        self._lock = asyncio.Lock()
        self._session: Optional[LocationSession] = None
        self._info: Optional[DeviceInfo] = None
        self._position: Optional[tuple[float, float]] = None
        self._walk_task: Optional[asyncio.Task] = None
        self._walk: dict = {}
        self._status_cache: tuple[float, dict] | None = None

    # -- status -----------------------------------------------------------------------------

    async def status(self, force: bool = False) -> dict:
        now = time.monotonic()
        if not force and self._status_cache and now - self._status_cache[0] < STATUS_CACHE_S:
            return {**self._status_cache[1], **self._sim_state()}
        async with self._lock:
            try:
                info = await self.backend.probe()
                base = {"state": "ready", "device": info.to_dict(), "error": None}
                self._info = info
            except AppError as e:
                base = {"state": e.code, "device": None, "error": e.to_dict()}
                if e.code == "no_device":
                    await self._device_gone()
        self._status_cache = (time.monotonic(), base)
        return {**base, **self._sim_state()}

    def _sim_state(self) -> dict:
        sim = {"active": self._position is not None}
        if self._position:
            sim["lat"], sim["lon"] = self._position
        walk = dict(self._walk)
        walk["active"] = bool(self._walk_task and not self._walk_task.done())
        return {"simulation": sim, "walk": walk}

    async def _device_gone(self) -> None:
        """Called under the lock when the phone vanished."""
        if self._session is not None:
            persists = self._session.persists
            await self._drop_session()
            if not persists:
                self._position = None
                self._cancel_walk()

    async def trust(self) -> None:
        async with self._lock:
            await self.backend.trust()
        self._status_cache = None

    # -- sessions ---------------------------------------------------------------------------

    async def _drop_session(self) -> None:
        session, self._session = self._session, None
        if session is not None:
            try:
                await session.close()
            except Exception:
                log.exception("error while closing session")

    async def _ensure_session(self) -> LocationSession:
        if self._session is None:
            info = await self.backend.probe()  # raises friendly errors (no device / trust / dev mode)
            self._info = info
            self._session = await self.backend.open_session(info)
        return self._session

    async def _run(self, action: Callable[[LocationSession], Awaitable[None]]) -> None:
        """Run an action on the session; on failure reconnect once (stale USB/tunnel connections)."""
        last: Optional[BaseException] = None
        for attempt in (1, 2):
            try:
                session = await self._ensure_session()
                await action(session)
                return
            except AppError as e:
                if e.code in ("no_device", "not_trusted", "developer_mode_off", "tunnel_needed", "device_locked"):
                    await self._drop_session()
                    raise
                last = e
            except Exception as e:
                log.exception("device action failed (attempt %d)", attempt)
                last = e
            await self._drop_session()
        raise translate_exception(last) if last else AppError("device_error", "Unknown error")

    # -- public operations ------------------------------------------------------------------

    async def set_location(self, lat: float, lon: float) -> dict:
        validate_coords(lat, lon)
        self._cancel_walk()
        async with self._lock:
            await self._run(lambda s: s.set(lat, lon))
            self._position = (lat, lon)
        log.info("location set to %.6f, %.6f", lat, lon)
        self._status_cache = None
        return self._sim_state()

    async def clear_location(self) -> dict:
        self._cancel_walk()
        async with self._lock:
            had_session = self._session is not None
            if self._session is not None or self._legacy_possible():
                try:
                    await self._run(lambda s: s.clear())
                finally:
                    # iOS 17+: closing the DVT session also ends the simulation.
                    await self._drop_session()
            self._position = None
        log.info("location simulation cleared (session was %s)", "open" if had_session else "closed")
        self._status_cache = None
        return self._sim_state()

    def _legacy_possible(self) -> bool:
        """Legacy phones keep a fake location after we disconnect, so clearing must reach the phone
        even when this app restarted. iOS 17+ simulations die with their connection."""
        return self._info is None or self._info.major < 17

    async def shutdown(self) -> None:
        self._cancel_walk()
        async with self._lock:
            await self._drop_session()

    # -- journeys (single destination, multi-stop routes, trick-or-treat) ---------------------

    def _cancel_walk(self) -> None:
        if self._walk_task and not self._walk_task.done():
            self._walk_task.cancel()
        self._walk_task = None
        self._walk = {}
        self._route_points = []

    @staticmethod
    def _check_speed(speed_kmh: float) -> float:
        if not (MIN_SPEED_KMH <= speed_kmh <= MAX_SPEED_KMH):
            raise AppError("bad_speed", f"Speed must be between {MIN_SPEED_KMH} and {MAX_SPEED_KMH} km/h.", status=422)
        return float(speed_kmh)

    def route_points(self) -> list[list[float]]:
        return self._route_points if self._walk_task and not self._walk_task.done() else []

    async def start_journey(self, plan: "JourneyPlan", router: Any = None) -> dict:
        """Follow the stops in order from the current simulated position (straight lines or road routes)."""
        stops = plan.stops
        if not 1 <= len(stops) <= MAX_STOPS:
            raise AppError("bad_stops", f"A route needs 1 to {MAX_STOPS} stops.", status=422)
        for st in stops:
            validate_coords(st.lat, st.lon)
            if not 0 <= st.dwell_s <= MAX_DWELL_S:
                raise AppError("bad_dwell", f"Pause at a stop must be 0-{int(MAX_DWELL_S)} seconds.", status=422)
        if plan.mode not in MODES:
            raise AppError("bad_mode", "Mode must be straight, walk or drive.", status=422)
        if not 0 <= plan.speed_jitter <= 0.5:
            raise AppError("bad_jitter", "Speed variation must be between 0 and 0.5.", status=422)
        speed = self._check_speed(plan.speed_kmh)
        if self._position is None and not plan.jump_to_first:
            raise AppError(
                "no_start", "Set a starting location first.", "Use \"Set Location\" once, then start moving from there.", 409
            )
        self._cancel_walk()

        first_dwell, first_target = 0.0, 0
        if plan.jump_to_first:
            first_dwell, first_target = stops[0].dwell_s, 1
            origin = (stops[0].lat, stops[0].lon)
        else:
            origin = self._position
        assert origin is not None
        targets = stops[first_target:]

        legs = await self._build_legs(
            [origin] + [(t.lat, t.lon) for t in targets],
            [first_target + i for i in range(len(targets))],
            [t.dwell_s for t in targets],
            plan.mode, router,
        )
        cycle: list[_Leg] = []
        if plan.loop and len(stops) >= 2:
            ring = [stops[-1]] + stops  # last -> first -> ... -> last, repeated forever
            cycle = await self._build_legs(
                [(t.lat, t.lon) for t in ring], list(range(len(stops))), [t.dwell_s for t in stops], plan.mode, router
            )

        if plan.jump_to_first:
            # Only teleport once every route is known to work, so a routing failure never strands the phone.
            await self.set_location(*origin)
        self._route_points = [[lat, lon] for leg in legs for lat, lon in leg.points]
        self._walk = {
            "kind": plan.kind, "mode": plan.mode, "speed_kmh": speed, "loop": bool(cycle), "total": len(stops),
            "stop": first_target, "state": "moving", "remaining_m": 0.0, "wait_left_s": 0.0,
        }
        self._walk_task = asyncio.create_task(self._journey_loop(plan, first_dwell, legs, cycle))
        return self._sim_state()

    async def _build_legs(self, waypoints, indices, dwells, mode, router) -> "list[_Leg]":
        if len(waypoints) < 2:
            return []
        if mode == "straight":
            polylines = [[a, b] for a, b in zip(waypoints, waypoints[1:])]
        else:
            polylines = await router.route(waypoints, mode)
        return [_Leg(pl, idx, d) for pl, idx, d in zip(polylines, indices, dwells)]

    def set_walk_speed(self, speed_kmh: float) -> dict:
        speed_kmh = self._check_speed(speed_kmh)
        if not (self._walk_task and not self._walk_task.done()):
            raise AppError("no_walk", "Not currently moving.", status=409)
        self._walk["speed_kmh"] = speed_kmh
        return self._sim_state()

    def stop_walk(self) -> dict:
        """Stop moving but stay at the current simulated position."""
        self._cancel_walk()
        return self._sim_state()

    async def _journey_loop(self, plan: "JourneyPlan", first_dwell: float, legs: "list[_Leg]", cycle: "list[_Leg]") -> None:
        try:
            if first_dwell:
                self._walk["stop"] = 1
                await self._dwell(first_dwell)
            for leg in itertools.chain(legs, itertools.cycle(cycle) if cycle else ()):
                await self._follow(leg, plan.speed_jitter)
                if leg.dwell_s:
                    await self._dwell(leg.dwell_s)
            self._walk.update(state="done", remaining_m=0.0, wait_left_s=0.0)
            if self._position and self.on_finish:
                self.on_finish(*self._position)
            log.info("journey finished")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("journey aborted")
            self._walk["error"] = translate_exception(e).to_dict()

    async def _follow(self, leg: "_Leg", jitter: float) -> None:
        follower = PathFollower(leg.points)
        factor = 1 + random.uniform(-jitter, jitter) if jitter else 1.0
        self._walk.update(state="moving", stop=leg.index + 1, wait_left_s=0.0)
        while True:
            step_m = self._walk["speed_kmh"] / 3.6 * factor * self.walk_tick
            lat, lon, done = follower.advance(step_m)
            async with self._lock:
                await self._run(lambda s: s.set(lat, lon))
                self._position = (lat, lon)
            self._walk["remaining_m"] = follower.remaining_m
            if done:
                return
            await asyncio.sleep(self.walk_tick)

    async def _dwell(self, seconds: float) -> None:
        self._walk.update(state="waiting", wait_left_s=seconds, remaining_m=0.0)
        end = time.monotonic() + seconds
        while (left := end - time.monotonic()) > 0:
            self._walk["wait_left_s"] = left
            await asyncio.sleep(min(self.walk_tick, left))
        self._walk["wait_left_s"] = 0.0


@dataclass
class Stop:
    lat: float
    lon: float
    dwell_s: float = 0.0


@dataclass
class JourneyPlan:
    stops: list[Stop]
    mode: str = "walk"            # straight | walk | drive
    speed_kmh: float = 5.0
    loop: bool = False
    jump_to_first: bool = False   # teleport to the first stop instead of walking there
    kind: str = "route"           # route | trick_or_treat
    speed_jitter: float = 0.0     # +/- fraction of speed, re-rolled per leg


@dataclass
class _Leg:
    points: list
    index: int                    # which stop (0-based) this leg arrives at
    dwell_s: float = 0.0
