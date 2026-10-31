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
import logging
import time
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional, Protocol

from . import errors
from .errors import AppError
from .movement import step_towards, haversine_m, validate_coords

log = logging.getLogger("locspoof.device")

WALK_TICK_S = 1.0
MIN_SPEED_KMH, MAX_SPEED_KMH = 0.5, 400.0
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
                if not await lockdown.validate_pairing():
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
                if not await lockdown.validate_pairing():
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
    def __init__(self, backend: Backend, walk_tick: float = WALK_TICK_S):
        self.backend = backend
        self.walk_tick = walk_tick
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

    # -- walking ----------------------------------------------------------------------------

    def _cancel_walk(self) -> None:
        if self._walk_task and not self._walk_task.done():
            self._walk_task.cancel()
        self._walk_task = None
        self._walk = {}

    async def start_walk(self, lat: float, lon: float, speed_kmh: float) -> dict:
        validate_coords(lat, lon)
        speed_kmh = self._check_speed(speed_kmh)
        if self._position is None:
            raise AppError(
                "no_start", "Set a starting location first.", "Use \"Set Location\" once, then walk from there.", 409
            )
        self._cancel_walk()
        self._walk = {"dest": [lat, lon], "speed_kmh": speed_kmh, "remaining_m": haversine_m(*self._position, lat, lon)}
        self._walk_task = asyncio.create_task(self._walk_loop(lat, lon))
        return self._sim_state()

    def set_walk_speed(self, speed_kmh: float) -> dict:
        speed_kmh = self._check_speed(speed_kmh)
        if not (self._walk_task and not self._walk_task.done()):
            raise AppError("no_walk", "Not currently walking.", status=409)
        self._walk["speed_kmh"] = speed_kmh
        return self._sim_state()

    def stop_walk(self) -> dict:
        """Stop moving but stay at the current simulated position."""
        self._cancel_walk()
        return self._sim_state()

    @staticmethod
    def _check_speed(speed_kmh: float) -> float:
        if not (MIN_SPEED_KMH <= speed_kmh <= MAX_SPEED_KMH):
            raise AppError("bad_speed", f"Speed must be between {MIN_SPEED_KMH} and {MAX_SPEED_KMH} km/h.", status=422)
        return float(speed_kmh)

    async def _walk_loop(self, dest_lat: float, dest_lon: float) -> None:
        try:
            while True:
                lat, lon = self._position  # type: ignore[misc]
                speed_mps = self._walk["speed_kmh"] / 3.6
                nlat, nlon, arrived = step_towards(lat, lon, dest_lat, dest_lon, speed_mps * self.walk_tick)
                async with self._lock:
                    await self._run(lambda s: s.set(nlat, nlon))
                    self._position = (nlat, nlon)
                self._walk["remaining_m"] = haversine_m(nlat, nlon, dest_lat, dest_lon)
                if arrived:
                    log.info("walk finished at %.6f, %.6f", nlat, nlon)
                    return
                await asyncio.sleep(self.walk_tick)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("walk aborted")
            err = translate_exception(e)
            self._walk["error"] = err.to_dict()
