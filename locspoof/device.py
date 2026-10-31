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
from .movement import PathFollower, haversine_m, validate_coords
from .realism import NoiseModel, WalkModel, cooldown_minutes, traffic_stop_seconds

log = logging.getLogger("locspoof.device")

WALK_TICK_S = 1.0
MIN_SPEED_KMH, MAX_SPEED_KMH = 0.5, 400.0
MAX_STOPS = 60
MAX_DWELL_S = 900.0
MODES = ('straight', 'walk', 'drive')
STATUS_CACHE_S = 2.0
AMBIENT_TICK_S = 2.5
RECONNECT_TIMEOUT_S = 600.0
RECONNECT_POLL_S = 2.0
MAX_PATH_POINTS = 20_000
CALL_TIMEOUT_S = 12.0       # one location update; a phone that sleeps mid-call must never freeze the app
PROBE_TIMEOUT_S = 20.0
OPEN_TIMEOUT_S = 300.0      # opening a session can include mounting the disk image
CLOSE_TIMEOUT_S = 5.0
REALISM_KEYS = ("noise", "natural_walk", "traffic_stops")
MEMORY_SAVE_EVERY_S = 30.0    # while a trip runs, the phone's remembered position is saved at most this often
LIST_INFO_TTL_S = 60.0        # how long a phone's name/iOS version is reused when listing phones
LIST_QUERY_TIMEOUT_S = 8.0
STOP_GRACE_S = 3.0          # how long Stop waits for the update in flight before cancelling it
HEALTHY_WINDOW_S = 6.0      # a device call succeeded this recently = the phone is fine, no need to probe it
STALE_WARNING = {
    "code": "stale_simulation",
    "message": "This iPhone may still be using the fake location from your last session.",
    "hint": "iPhones on iOS 16 and earlier keep a fake location until it is cleared, even after the app was closed. "
            "Press Restore real GPS to clear it.",
}
TRANSIENT_CODES = {"no_device", "connection_lost", "device_error", "usbmuxd_unavailable"}


@dataclass
class DeviceInfo:
    udid: str
    name: str
    ios_version: str
    product_type: str = ""
    connection: str = "USB"                 # "USB" | "Wi-Fi"
    wifi_enabled: Optional[bool] = None     # "connect over Wi-Fi" switch on the phone (None = unknown)
    ddi_mounted: Optional[bool] = None      # Developer Disk Image mounted? (None = unknown)

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
            "connection": self.connection,
            "wifi_enabled": self.wifi_enabled,
            "ddi_mounted": self.ddi_mounted,
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
    async def set_wifi(self, enabled: bool) -> None: ...
    async def list_devices(self) -> list[dict]: ...
    async def scan_wifi(self, timeout: float = 5.0) -> list[dict]: ...


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


def norm_udid(udid: str) -> str:
    """usbmux sometimes drops the dash of a UDID; compare without it."""
    return udid.replace("-", "").lower()


@dataclass
class _Target:
    serial: str
    connection: str                  # "USB" | "Wi-Fi"
    bonjour: bool = False            # found by Bonjour (no usbmux entry); reached through a TCP lockdown


_WIFI_FOUND: dict[str, dict] = {}    # norm udid -> {"udid", "name", "ios_version", "seen"}: phones found by a Wi-Fi scan
WIFI_FOUND_TTL_S = 300.0


def _fresh_wifi() -> dict[str, dict]:
    now = time.monotonic()
    for k in [k for k, v in _WIFI_FOUND.items() if now - v["seen"] > WIFI_FOUND_TTL_S]:
        del _WIFI_FOUND[k]
    return _WIFI_FOUND


class PmdBackend:
    """Everything that talks to pymobiledevice3, for ONE phone (``udid``; None = the first one found)."""

    def __init__(self, tunnel_mode: str = "auto", udid: Optional[str] = None):
        if tunnel_mode not in ("auto", "tunneld", "userspace"):
            raise ValueError("tunnel_mode must be auto, tunneld or userspace")
        self.tunnel_mode = tunnel_mode
        self.udid = udid
        self._last_problem: Optional[str] = None
        self._ddi_cache: Optional[tuple[float, Optional[bool]]] = None
        self._list_cache: Optional[tuple[float, list[dict]]] = None
        self._info_cache: dict[tuple[str, str], tuple[float, dict]] = {}   # (serial, connection) -> (when, entry)

    # -- finding the phone --------------------------------------------------------------------

    async def _resolve(self) -> _Target:
        """USB if plugged in, else Wi-Fi as listed by usbmuxd, else a phone found by a Bonjour scan."""
        from pymobiledevice3 import usbmux

        devices = [d for d in await usbmux.list_devices() if not self.udid or d.matches_udid(self.udid)]
        usb = [d for d in devices if d.is_usb]
        if usb:
            return _Target(usb[0].serial, "USB")
        net = [d for d in devices if d.is_network]
        if net:
            return _Target(net[0].serial, "Wi-Fi")
        for key, found in _fresh_wifi().items():
            if not self.udid or key == norm_udid(self.udid):
                return _Target(found["udid"], "Wi-Fi", bonjour=True)
        raise errors.no_device()

    async def _lockdown(self, target: _Target, autopair: bool = False):
        if target.bonjour:
            return await self._bonjour_lockdown(target.serial)
        from pymobiledevice3.lockdown import create_using_usbmux

        return await create_using_usbmux(
            serial=target.serial, autopair=autopair, connection_type="USB" if target.connection == "USB" else "Network"
        )

    @staticmethod
    async def _bonjour_lockdown(udid: Optional[str], timeout: float = 4.0):
        from pymobiledevice3.lockdown import get_mobdev2_lockdowns

        gen = get_mobdev2_lockdowns(udid=udid, only_paired=True, timeout=timeout)
        try:
            async for _ip, lockdown in gen:
                return lockdown
        finally:
            await gen.aclose()
        raise errors.no_device()

    # -- discovery (all phones) ------------------------------------------------------------------

    async def list_devices(self) -> list[dict]:
        """Every phone we can see: [{udid, name, ios_version, connection, paired}]. Cached for a few seconds."""
        if self._list_cache and time.monotonic() - self._list_cache[0] < 4.0:
            return self._list_cache[1]
        from pymobiledevice3 import usbmux

        found: dict[str, _Target] = {}
        try:
            for d in await usbmux.list_devices():
                key = norm_udid(d.serial)
                if d.is_usb or key not in found:
                    found[key] = _Target(d.serial, "USB" if d.is_usb else "Wi-Fi")
        except Exception as e:
            raise translate_exception(e) from e
        for key, f in _fresh_wifi().items():
            found.setdefault(key, _Target(f["udid"], "Wi-Fi", bonjour=True))
        out = []
        now = time.monotonic()
        for key, t in found.items():
            cached = self._info_cache.get((t.serial, t.connection))
            if cached and now - cached[0] < LIST_INFO_TTL_S:        # don't re-handshake a phone we just looked at
                out.append(dict(cached[1]))
                continue
            entry = {"udid": t.serial, "name": "iPhone", "ios_version": "", "connection": t.connection, "paired": False}
            try:
                ld = await asyncio.wait_for(self._lockdown(t), LIST_QUERY_TIMEOUT_S)
                try:
                    entry.update(name=ld.all_values.get("DeviceName") or "iPhone", ios_version=ld.product_version, paired=bool(ld.paired))
                    entry["udid"] = ld.udid or t.serial
                finally:
                    await ld.close()
                self._info_cache[(t.serial, t.connection)] = (now, dict(entry))
            except Exception as e:                                  # includes a phone that does not answer in time
                log.debug("could not query %s: %r", t.serial, e)
            out.append(entry)
        for k in [k for k in self._info_cache if k not in {(t.serial, t.connection) for t in found.values()}]:
            del self._info_cache[k]                                 # forget phones that went away
        out.sort(key=lambda e: (e["connection"] != "USB", e["name"]))
        self._list_cache = (time.monotonic(), out)
        return out

    async def scan_wifi(self, timeout: float = 5.0) -> list[dict]:
        """Look for paired phones on the local network (Bonjour). They stay usable for a few minutes."""
        from pymobiledevice3.lockdown import get_mobdev2_lockdowns

        found = []
        gen = get_mobdev2_lockdowns(only_paired=True, timeout=timeout)
        try:
            async for _ip, ld in gen:
                try:
                    udid = ld.udid
                    entry = {"udid": udid, "name": ld.all_values.get("DeviceName") or "iPhone",
                             "ios_version": ld.product_version, "seen": time.monotonic()}
                    _WIFI_FOUND[norm_udid(udid)] = entry
                    found.append({k: v for k, v in entry.items() if k != "seen"})
                finally:
                    await ld.close()
        except Exception as e:
            log.warning("Wi-Fi scan failed: %r", e)
        finally:
            await gen.aclose()
        self._list_cache = None
        return found

    # -- one phone ------------------------------------------------------------------------------

    async def probe(self) -> DeviceInfo:
        try:
            target = await self._resolve()
            lockdown = await self._lockdown(target)
            try:
                if not lockdown.paired:
                    raise errors.not_trusted()
                info = DeviceInfo(
                    udid=lockdown.udid or target.serial,
                    name=lockdown.all_values.get("DeviceName") or "iPhone",
                    ios_version=lockdown.product_version,
                    product_type=lockdown.all_values.get("ProductType") or "",
                    connection=target.connection,
                )
                # Developer Mode exists from iOS 16; earlier versions have no such switch.
                if info.major >= 16 and not await lockdown.get_developer_mode_status():
                    raise errors.developer_mode_off()
                info.wifi_enabled = await self._optional(lockdown.get_enable_wifi_connections())
                info.ddi_mounted = await self._ddi_mounted(lockdown)
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

    @staticmethod
    async def _optional(coro):
        try:
            return await coro
        except Exception:
            return None

    async def _ddi_mounted(self, lockdown) -> Optional[bool]:
        """Is the Developer Disk Image mounted? None when it cannot be determined. Cached briefly."""
        if self._ddi_cache and time.monotonic() - self._ddi_cache[0] < 60:
            return self._ddi_cache[1]
        from pymobiledevice3.services.mobile_image_mounter import MobileImageMounterService, image_type_for_device

        try:
            async with MobileImageMounterService(lockdown) as mounter:
                value: Optional[bool] = await mounter.is_image_mounted(image_type_for_device(lockdown))
        except Exception as e:
            log.debug("could not check the disk image: %r", e)
            value = None
        self._ddi_cache = (time.monotonic(), value)
        return value

    def _note_probe_problem(self, code: str) -> bool:
        new = code != self._last_problem
        if new:
            log.warning("device problem: %s", code)
        self._last_problem = code
        return new

    async def trust(self) -> None:
        """Trigger the on-phone 'Trust This Computer?' prompt and wait for the answer (needs a cable)."""
        try:
            target = await self._resolve()
            lockdown = await self._lockdown(target, autopair=False)
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

    async def set_wifi(self, enabled: bool) -> None:
        """Turn 'connect over Wi-Fi' on or off in the phone. Only possible while it is on a cable."""
        try:
            target = await self._resolve()
            if target.connection != "USB":
                raise AppError("wifi_needs_usb", "Plug the iPhone in with a cable to change this.",
                               "The Wi-Fi switch can only be changed over USB.", 409)
            lockdown = await self._lockdown(target)
            try:
                if not lockdown.paired:
                    raise errors.not_trusted()
                await lockdown.set_enable_wifi_connections(enabled)
            finally:
                await lockdown.close()
            self._ddi_cache = None
        except AppError:
            raise
        except Exception as e:
            log.exception("set_wifi failed")
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

        lockdown = await self._lockdown(await self._resolve())
        try:
            async with MobileImageMounterService(lockdown) as mounter:
                mounted = await mounter.is_image_mounted(image_type_for_device(lockdown))
            if not mounted:
                log.info("Developer Disk Image not mounted; mounting (may download it)...")
                try:
                    await auto_mount(lockdown)
                except AlreadyMountedError:
                    pass
            self._ddi_cache = None
        except BaseException:
            await lockdown.close()
            raise
        return _LegacySession(lockdown)

    async def _open_rsd(self, target: _Target) -> tuple[Any, Callable[[], Awaitable[None]]]:
        """Return (rsd, closer) using a running tunneld first, else the no-root userspace tunnel."""
        from pymobiledevice3.exceptions import (
            PyMobileDevice3Exception,
            TunneldConnectionError,
            UserspaceTunnelUnavailableError,
        )

        if self.tunnel_mode in ("auto", "tunneld"):
            from pymobiledevice3.tunneld.api import get_tunneld_device_by_udid

            try:
                rsd = await get_tunneld_device_by_udid(target.serial)
            except TunneldConnectionError:
                rsd = None
                log.info("tunneld is not running")
            if rsd is not None:
                log.info("using tunnel from tunneld")
                return rsd, rsd.close
            if self.tunnel_mode == "tunneld":
                raise errors.tunnel_needed()

        from pymobiledevice3.remote import userspace_tunnel

        log.info("starting in-process userspace tunnel (no root needed, iOS 17.4+) over %s", target.connection)
        tunnel = userspace_tunnel.UserspaceRsdTunnel(
            serial=target.serial, remotepairing_fallback=(target.connection == "Wi-Fi")
        )
        try:
            if target.bonjour:
                rsd = await self._aopen_via_bonjour(tunnel, userspace_tunnel, target)
            else:
                rsd = await tunnel.aopen()
        except UserspaceTunnelUnavailableError as e:
            log.warning("userspace tunnel unavailable: %s", e)
            raise errors.tunnel_needed() from e
        except PyMobileDevice3Exception as e:
            if "already active" in str(e):
                raise AppError(
                    "second_phone_needs_tunneld",
                    "Only one iOS 17+ iPhone can use the built-in tunnel at a time.",
                    f"To control several iOS 17+ phones at once, run in a separate terminal:  {errors.TUNNELD_CMD}",
                    409,
                ) from e
            raise
        return rsd, tunnel.aclose

    async def _aopen_via_bonjour(self, tunnel: Any, module: Any, target: _Target) -> Any:
        """Open the userspace tunnel for a phone that has no usbmux entry (found over Wi-Fi by Bonjour).

        The library builds its tunnel provider from a usbmux lockdown; here we hand it one made from the
        Bonjour (TCP) lockdown instead, by swapping that single hook for the duration of the call.
        """
        from pymobiledevice3.remote.tunnel_service import CoreDeviceTunnelProxy

        original = module._create_no_root_tunnel_provider

        async def provider(serial, autopair, remotepairing_fallback=True):
            lockdown = await self._bonjour_lockdown(target.serial)
            try:
                return await CoreDeviceTunnelProxy.create(lockdown), lockdown
            except BaseException:
                await lockdown.close()
                raise

        module._create_no_root_tunnel_provider = provider
        try:
            return await tunnel.aopen()
        finally:
            module._create_no_root_tunnel_provider = original

    async def _open_dvt(self, info: DeviceInfo) -> LocationSession:
        from pymobiledevice3.exceptions import AlreadyMountedError
        from pymobiledevice3.services.dvt.instruments.dvt_provider import DvtProvider
        from pymobiledevice3.services.dvt.instruments.location_simulation import LocationSimulation
        from pymobiledevice3.services.mobile_image_mounter import (
            MobileImageMounterService,
            auto_mount,
            image_type_for_device,
        )

        rsd, closer = await self._open_rsd(await self._resolve())
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
                self._ddi_cache = None
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
        settings: Optional[dict] = None, ambient_tick: float = AMBIENT_TICK_S, rng: Optional[random.Random] = None,
        memory: Any = None,
    ):
        self.backend = backend
        self.walk_tick = walk_tick
        self.ambient_tick = ambient_tick
        self.on_finish = on_finish  # called with the final position when a journey completes
        self.settings: dict = settings if settings is not None else {}   # shared, live on/off switches
        self.memory = memory                                             # DeviceMemory (optional)
        self.reconnect_timeout, self.reconnect_poll = RECONNECT_TIMEOUT_S, RECONNECT_POLL_S
        self.call_timeout, self.probe_timeout, self.open_timeout = CALL_TIMEOUT_S, PROBE_TIMEOUT_S, OPEN_TIMEOUT_S
        self.close_timeout, self.stop_grace, self.healthy_window = CLOSE_TIMEOUT_S, STOP_GRACE_S, HEALTHY_WINDOW_S
        self._last_ok = 0.0                                    # when a device call last succeeded
        self._last_base: Optional[dict] = None                 # the last "ready"/error part of status
        self._stop_requested = False                           # asks the running trip to stop at a safe point
        self._realism_override: dict = {}                      # switches that apply to the running trip only
        self._sim_saved = float("-inf")                        # when the position was last written to devices.json
        self._stale_notice = False                             # the position came from a previous session (app was killed)
        self._rng = rng or random.Random()
        self._noise = NoiseModel(rng=self._rng)
        self._route_points: list[list[float]] = []
        self._lock = asyncio.Lock()
        self._session: Optional[LocationSession] = None
        self._info: Optional[DeviceInfo] = None
        self._position: Optional[tuple[float, float]] = None   # the true (noise-free) simulated position
        self._origin: Optional[tuple[float, float]] = None     # where the last journey started (for "return")
        self._last_plan: Optional["JourneyPlan"] = None
        self._lost = False                                     # phone vanished; will restore when it returns
        self._cooldown: Optional[dict] = None
        self._walk_task: Optional[asyncio.Task] = None
        self._ambient_task: Optional[asyncio.Task] = None
        self._walk: dict = {}
        self._status_cache: tuple[float, dict] | None = None

    def _on(self, key: str) -> bool:
        """A realism/option switch: this trip's own override if it has one, else the global setting."""
        if key in self._realism_override:
            return bool(self._realism_override[key])
        return bool(self.settings.get(key))

    def _journey_active(self) -> bool:
        return bool(self._walk_task and not self._walk_task.done())

    def awake_reasons(self) -> list[str]:
        """Why this phone needs the Mac to stay awake right now (empty = it does not)."""
        out = []
        if self._journey_active():
            out.append("a trip is running")
        elif self._position is not None and (self._lost or not self._persists()):
            # iOS 17+: the fake location lasts only while this Mac holds the connection open
            out.append("the fake location needs the connection")
        return out

    def on_wake(self) -> None:
        """The Mac just woke from sleep: forget what we believed about the connection, so it is re-checked at once."""
        self._last_ok = 0.0
        self._status_cache = None

    def _persists(self) -> bool:
        """Does the phone keep the fake location after we disconnect? (iOS 16 and earlier only)"""
        if self._session is not None:
            return self._session.persists
        return self._info is not None and self._info.major < 17

    # -- status -----------------------------------------------------------------------------

    def _healthy(self) -> bool:
        """A session is open and a call to the phone worked a moment ago: the phone is demonstrably fine."""
        return (self._session is not None and self._info is not None and self._last_base is not None
                and self._last_base.get("state") == "ready" and time.monotonic() - self._last_ok < self.healthy_window)

    async def status(self, force: bool = False) -> dict:
        now = time.monotonic()
        if not force and self._status_cache and now - self._status_cache[0] < STATUS_CACHE_S:
            return self._compose(self._status_cache[1])
        if not force and self._healthy():
            # Skip the handshake: it would only compete with the trip for the phone (slow on Wi-Fi).
            return self._compose(self._last_base)       # type: ignore[arg-type]
        if self._lock.locked() and not force:
            # Something slow is using the phone (opening a session, mounting the disk image...): answer at once.
            base = self._last_base or {"state": "connecting", "device": None, "error": None, "warnings": []}
            return self._compose(base)
        async with self._lock:
            try:
                info = await asyncio.wait_for(self.backend.probe(), self.probe_timeout)
                base = {"state": "ready", "device": info.to_dict(), "error": None, "warnings": self._warnings(info)}
                self._info = info
                self._resume_from_memory(info)
                if self._lost and self._position and self._on("auto_reconnect") and not self._journey_active():
                    await self._restore_position()
            except asyncio.TimeoutError:
                log.warning("the phone did not answer the status check within %.0f s", self.probe_timeout)
                base = {"state": "connection_lost", "device": None, "error": errors.timeout().to_dict(), "warnings": []}
            except AppError as e:
                base = {"state": e.code, "device": None, "error": e.to_dict(), "warnings": []}
                if e.code == "no_device":
                    await self._device_gone()
        self._last_base = base
        self._status_cache = (time.monotonic(), base)
        return self._compose(base)

    def _compose(self, base: dict) -> dict:
        """The answer for /api/status: the phone part plus the live simulation/trip state (and the stale-location notice)."""
        out = {**base, **self._sim_state()}
        if self._stale_notice:
            out["warnings"] = [*base.get("warnings", []), STALE_WARNING]
        return out

    def _warnings(self, info: DeviceInfo) -> list[dict]:
        """Non-blocking heads-ups about the phone (shown as a notice, not an error)."""
        prev = self.memory.remember(info.udid, info.name, info.ios_version) if self.memory else None
        out = []
        if info.ddi_mounted is False:
            updated = prev and prev != info.ios_version
            out.append({
                "code": "ddi_missing",
                "message": (f"iOS was updated ({prev} → {info.ios_version}), so the Developer Disk Image must be mounted again."
                            if updated else "The Developer Disk Image is not mounted on this iPhone."),
                "hint": "Connect this Mac to the internet once, then press Set location. The app downloads and mounts it "
                        "automatically (it can take a minute). It also needs mounting again after the phone restarts.",
            })
        return out

    def _remember_sim(self, force: bool = False) -> None:
        """Phones on iOS 16 and earlier keep the fake location even if this app dies, so write down where it is
        (iOS 17+ drops it with the connection: nothing to remember). Throttled: never one disk write per step."""
        if not (self.memory and self._info and self._position and self._persists()):
            return
        now = time.monotonic()
        if force or now - self._sim_saved >= MEMORY_SAVE_EVERY_S:
            self._sim_saved = now
            self.memory.set_sim(self._info.udid, *self._position)

    def _forget_sim(self) -> None:
        if self.memory and self._info:
            self.memory.clear_sim(self._info.udid)

    def _resume_from_memory(self, info: DeviceInfo) -> None:
        """After a restart: if this phone was left with a fake location, show it (with a Restore button)."""
        if self.memory is None or self._position is not None or info.major >= 17:
            return
        rec = self.memory.get_sim(info.udid)
        if rec:
            log.info("this phone may still have a fake location from the last session: %.6f, %.6f", *rec)
            self._position, self._stale_notice = rec, True
            self._ensure_ambient()

    async def set_wifi(self, enabled: bool) -> None:
        async with self._lock:
            await self.backend.set_wifi(enabled)
        self._status_cache = None

    async def _restore_position(self) -> None:
        """The phone is back after a disconnect: put the simulated location back (under the lock)."""
        try:
            pos = self._position
            await self._run(lambda s: s.set(*pos))  # type: ignore[misc]
            self._lost = False
            log.info("phone reconnected; simulated location restored")
        except AppError:
            log.info("phone is back but could not restore the location yet")

    def _sim_state(self) -> dict:
        sim: dict = {"active": self._position is not None}
        if self._position:
            sim["lat"], sim["lon"] = self._position
            if self._lost:
                sim["reconnecting"] = True
            if self._origin:
                sim["origin"] = list(self._origin)
        cd = self._cooldown_state()
        if cd:
            sim["cooldown"] = cd
        walk = dict(self._walk)
        walk["active"] = self._journey_active()
        return {"simulation": sim, "walk": walk}

    def _cooldown_state(self) -> Optional[dict]:
        if not self._cooldown:
            return None
        left = self._cooldown["until"] - time.monotonic()
        if left <= 0:
            self._cooldown = None
            return None
        return {"km": self._cooldown["km"], "total_s": self._cooldown["total_s"], "remaining_s": round(left)}

    def _note_jump(self, prev: Optional[tuple[float, float]], new: tuple[float, float]) -> None:
        if prev is None:
            return
        km = haversine_m(*prev, *new) / 1000
        wait = cooldown_minutes(km) * 60
        if wait > 0:
            self._cooldown = {"km": round(km, 1), "total_s": round(wait), "until": time.monotonic() + wait}

    async def _device_gone(self) -> None:
        """Called under the lock when the phone vanished."""
        if self._session is not None:
            persists = self._session.persists
            await self._drop_session()
            if not persists:
                if self._on("auto_reconnect") and self._position is not None:
                    self._lost = True          # keep the position and any journey; resume when it returns
                else:
                    self._position = None
                    self._origin = None
                    self._cancel_walk()

    async def trust(self) -> None:
        async with self._lock:
            await self.backend.trust()
        self._status_cache = None

    # -- sessions ---------------------------------------------------------------------------

    async def _drop_session(self) -> None:
        session, self._session = self._session, None
        self._last_ok = 0.0
        if session is not None:
            try:
                await asyncio.wait_for(session.close(), self.close_timeout)
            except asyncio.TimeoutError:
                log.warning("closing the old phone session took too long; abandoning it")
            except Exception:
                log.exception("error while closing session")

    async def _ensure_session(self) -> LocationSession:
        if self._session is None:
            info = await asyncio.wait_for(self.backend.probe(), self.probe_timeout)  # friendly errors: no device / trust / dev mode
            self._info = info
            self._session = await asyncio.wait_for(self.backend.open_session(info), self.open_timeout)
        return self._session

    async def _run(self, action: Callable[[LocationSession], Awaitable[None]]) -> None:
        """Run an action on the session; on failure reconnect once (stale USB/tunnel connections).

        Every step is time-limited: a phone that goes to sleep mid-call must not keep the device lock forever.
        """
        last: Optional[BaseException] = None
        for attempt in (1, 2):
            try:
                session = await self._ensure_session()
                await asyncio.wait_for(action(session), self.call_timeout)
                self._last_ok = time.monotonic()
                return
            except asyncio.TimeoutError:
                log.warning("the phone did not answer within the time limit (attempt %d)", attempt)
                last = errors.timeout()
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

    async def _push(self, lat: float, lon: float) -> None:
        """Send one position of a journey to the phone (with GPS noise if enabled).

        With auto-reconnect on, a vanished phone makes this wait (retrying) instead of failing the journey.
        """
        send = self._noise.apply(lat, lon, self.walk_tick) if self._on("noise") else (lat, lon)
        deadline = time.monotonic() + self.reconnect_timeout
        while True:
            try:
                async with self._lock:
                    await self._run(lambda s: s.set(*send))
                    self._position = (lat, lon)
                self._remember_sim()
                self._lost = False
                if self._walk.get("state") == "reconnecting":
                    self._walk["state"] = "moving"
                return
            except AppError as e:
                if not (self._on("auto_reconnect") and e.code in TRANSIENT_CODES) or time.monotonic() > deadline:
                    raise
                self._lost = True
                self._walk["state"] = "reconnecting"
                await asyncio.sleep(self.reconnect_poll)
                self._check_stop()

    # -- public operations ------------------------------------------------------------------

    async def set_location(self, lat: float, lon: float) -> dict:
        validate_coords(lat, lon)
        await self._stop_journey()
        prev = self._position
        async with self._lock:
            await self._run(lambda s: s.set(lat, lon))
            self._position = (lat, lon)
            self._lost = False
        self._note_jump(prev, (lat, lon))
        self._stale_notice = False
        self._remember_sim(force=True)
        self._ensure_ambient()
        log.info("location set to %.6f, %.6f", lat, lon)
        self._status_cache = None
        return self._sim_state()

    async def clear_location(self) -> dict:
        await self._stop_journey()
        self._cancel_ambient()
        async with self._lock:
            had_session = self._session is not None
            if self._session is not None or self._legacy_possible():
                try:
                    await self._run(lambda s: s.clear())
                finally:
                    # iOS 17+: closing the DVT session also ends the simulation.
                    await self._drop_session()
            self._position = None
            self._origin = None
            self._cooldown = None
            self._lost = False
            self._stale_notice = False
            self._forget_sim()
        log.info("location simulation cleared (session was %s)", "open" if had_session else "closed")
        self._status_cache = None
        return self._sim_state()

    def _legacy_possible(self) -> bool:
        """Legacy phones keep a fake location after we disconnect, so clearing must reach the phone
        even when this app restarted. iOS 17+ simulations die with their connection."""
        return self._info is None or self._info.major < 17

    async def shutdown(self) -> None:
        await self._stop_journey()
        self._cancel_ambient()
        async with self._lock:
            await self._drop_session()

    # -- ambient GPS noise while standing still ---------------------------------------------

    def _ensure_ambient(self) -> None:
        if self._ambient_task is None or self._ambient_task.done():
            self._ambient_task = asyncio.create_task(self._ambient_loop())

    def _cancel_ambient(self) -> None:
        if self._ambient_task and not self._ambient_task.done():
            self._ambient_task.cancel()
        self._ambient_task = None

    async def _ambient_loop(self) -> None:
        while True:
            await asyncio.sleep(self.ambient_tick)
            try:
                if not (self._on("noise") and self._position and not self._journey_active() and self._session and not self._lost):
                    continue
                lat, lon = self._noise.apply(*self._position, self.ambient_tick)
                async with self._lock:
                    if self._position and not self._journey_active():
                        await self._run(lambda s: s.set(lat, lon))
            except asyncio.CancelledError:
                raise
            except AppError as e:
                log.debug("ambient noise update skipped: %s", e.code)
                if e.code in TRANSIENT_CODES and self._on("auto_reconnect") and not self._persists():
                    self._lost = True
            except Exception:
                log.exception("ambient noise update failed")      # keep going: this loop must never die

    # -- journeys (single destination, multi-stop routes, GPX tracks, trick-or-treat) -----------

    async def _stop_journey(self) -> None:
        """Stop the running trip at a safe point: let the update in flight finish (cutting a call to the phone in
        half could leave its connection confused), and only cancel hard if the phone is not answering."""
        task = self._walk_task
        if task is not None and not task.done():
            self._stop_requested = True
            try:
                done, _ = await asyncio.wait({task}, timeout=self.stop_grace)
                if not done:
                    log.warning("the trip did not stop within %.0f s; cancelling it", self.stop_grace)
                    task.cancel()
                    await asyncio.wait({task}, timeout=2.0)
            finally:
                self._stop_requested = False
        self._walk_task = None
        self._walk = {}
        self._route_points = []
        self._realism_override = {}
        self._remember_sim(force=True)

    def _check_stop(self) -> None:
        if self._stop_requested:
            raise asyncio.CancelledError()

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
        return self._route_points if self._journey_active() else []

    async def start_journey(self, plan: "JourneyPlan", router: Any = None) -> dict:
        """Follow the stops (or a GPX path) from the current simulated position."""
        stops = plan.stops
        if not 1 <= len(stops) <= MAX_STOPS:
            raise AppError("bad_stops", f"A route needs 1 to {MAX_STOPS} stops.", status=422)
        for st in stops:
            validate_coords(st.lat, st.lon)
            if not 0 <= st.dwell_s <= MAX_DWELL_S:
                raise AppError("bad_dwell", f"Pause at a stop must be 0-{int(MAX_DWELL_S)} seconds.", status=422)
            if st.mode is not None and st.mode not in MODES:
                raise AppError("bad_mode", "Mode must be straight, walk or drive.", status=422)
            if st.speed_kmh is not None:
                self._check_speed(st.speed_kmh)
        if plan.mode not in MODES:
            raise AppError("bad_mode", "Mode must be straight, walk or drive.", status=422)
        if not 0 <= plan.speed_jitter <= 0.5:
            raise AppError("bad_jitter", "Speed variation must be between 0 and 0.5.", status=422)
        speed = self._check_speed(plan.speed_kmh)
        path = [tuple(p) for p in plan.path] if plan.path else None
        if path is not None:
            if not 2 <= len(path) <= MAX_PATH_POINTS:
                raise AppError("bad_path", f"A track needs 2 to {MAX_PATH_POINTS} points.", status=422)
            for p in path:
                validate_coords(*p)
        if self._position is None and not plan.jump_to_first:
            raise AppError(
                "no_start", "Set a starting location first.", "Use \"Set Location\" once, then start moving from there.", 409
            )
        await self._stop_journey()

        first_dwell, first_target = 0.0, 0
        if plan.jump_to_first:
            origin = path[0] if path else (stops[0].lat, stops[0].lon)
            if not path:
                first_dwell, first_target = stops[0].dwell_s, 1
        else:
            origin = self._position
        assert origin is not None
        targets = [] if path else stops[first_target:]

        if path:
            legs: list[_Leg] = []
            if haversine_m(*origin, *path[0]) > 2:   # walk/drive from where we are to the track's start
                legs += await self._build_legs([origin, path[0]], [0], [0.0], [plan.mode], [None], router)
            legs.append(_Leg(path, 0, 0.0, "gpx", None))
        else:
            legs = await self._build_legs(
                [origin] + [(t.lat, t.lon) for t in targets], [first_target + i for i in range(len(targets))],
                [t.dwell_s for t in targets], [t.mode or plan.mode for t in targets], [t.speed_kmh for t in targets], router,
            )
        cycle: list[_Leg] = []
        if plan.loop and not path and len(stops) >= 2:
            ring = [stops[-1]] + stops  # last -> first -> ... -> last, repeated forever
            cycle = await self._build_legs(
                [(t.lat, t.lon) for t in ring], list(range(len(stops))), [t.dwell_s for t in stops],
                [t.mode or plan.mode for t in stops], [t.speed_kmh for t in stops], router,
            )
        if plan.return_to_start and not cycle:
            end = legs[-1].points[-1] if legs else origin
            if haversine_m(*end, *origin) > 3:
                back = await self._build_legs([end, origin], [max(len(stops) - 1, 0)], [0.0], [plan.mode], [None], router)
                for leg in back:
                    leg.returning = True
                legs += back

        if plan.jump_to_first:
            # Only teleport once every route is known to work, so a routing failure never strands the phone.
            await self.set_location(*origin)
        self._origin = origin
        self._last_plan = plan
        self._stale_notice = False
        self._remember_sim(force=True)
        self._route_points = [[lat, lon] for leg in legs for lat, lon in leg.points]
        self._realism_override = {k: bool(v) for k, v in (plan.realism or {}).items() if k in REALISM_KEYS}
        self._walk = {
            "kind": plan.kind, "mode": plan.mode, "speed_kmh": speed, "cur_speed_kmh": speed, "loop": bool(cycle),
            "total": 1 if path else len(stops), "stop": first_target, "state": "moving", "remaining_m": 0.0,
            "wait_left_s": 0.0, "returning": False, "reason": None,
        }
        self._walk_task = asyncio.create_task(self._journey_loop(plan, first_dwell, legs, cycle))
        return self._sim_state()

    async def return_to_origin(self, router: Any = None) -> dict:
        """Go back to where the last journey started, using the same path type and pace."""
        if self._origin is None:
            raise AppError("no_origin", "There is no start point to return to yet.", "Start a route first.", 409)
        if self._position is None:
            raise AppError("no_start", "Set a starting location first.", status=409)
        if haversine_m(*self._position, *self._origin) < 3:
            raise AppError("already_there", "You are already at the start point.", status=409)
        last = self._last_plan
        speed = self._walk.get("speed_kmh") or (last.speed_kmh if last else 5.0)
        origin = self._origin
        plan = JourneyPlan(
            stops=[Stop(*origin)], mode=last.mode if last else "walk", speed_kmh=speed,
            kind=last.kind if last else "route", speed_jitter=last.speed_jitter if last else 0.0,
        )
        result = await self.start_journey(plan, router)
        self._origin = origin   # a second "return" still goes to the original start
        return result

    async def _build_legs(self, waypoints, indices, dwells, modes, speeds, router) -> "list[_Leg]":
        if len(waypoints) < 2:
            return []
        polylines: list = [None] * len(modes)
        i = 0
        while i < len(modes):                      # one routing request per run of legs with the same mode
            j = i
            while j + 1 < len(modes) and modes[j + 1] == modes[i]:
                j += 1
            seg = waypoints[i:j + 2]
            polylines[i:j + 1] = [[a, b] for a, b in zip(seg, seg[1:])] if modes[i] == "straight" else await router.route(seg, modes[i])
            i = j + 1
        return [_Leg(pl, idx, d, m, sp) for pl, idx, d, m, sp in zip(polylines, indices, dwells, modes, speeds)]

    def set_walk_speed(self, speed_kmh: float) -> dict:
        speed_kmh = self._check_speed(speed_kmh)
        if not self._journey_active():
            raise AppError("no_walk", "Not currently moving.", status=409)
        self._walk["speed_kmh"] = speed_kmh
        return self._sim_state()

    async def stop_walk(self) -> dict:
        """Stop moving but stay at the current simulated position."""
        await self._stop_journey()
        return self._sim_state()

    async def _journey_loop(self, plan: "JourneyPlan", first_dwell: float, legs: "list[_Leg]", cycle: "list[_Leg]") -> None:
        try:
            if first_dwell:
                self._walk["stop"] = 1
                await self._dwell(first_dwell)
            for leg in itertools.chain(legs, itertools.cycle(cycle) if cycle else ()):
                await self._follow(leg, plan)
                if leg.dwell_s:
                    await self._dwell(leg.dwell_s)
            self._walk.update(state="done", remaining_m=0.0, wait_left_s=0.0, returning=False)
            self._realism_override = {}
            self._remember_sim(force=True)
            if self._position and self.on_finish:
                self.on_finish(*self._position)
            log.info("journey finished")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("journey aborted")
            self._walk["error"] = translate_exception(e).to_dict()

    async def _follow(self, leg: "_Leg", plan: "JourneyPlan") -> None:
        follower = PathFollower(leg.points)
        factor = 1 + random.uniform(-plan.speed_jitter, plan.speed_jitter) if plan.speed_jitter else 1.0
        pace = WalkModel(self._rng)
        self._walk.update(state="moving", stop=leg.index + 1, wait_left_s=0.0, returning=leg.returning, reason=None)
        while True:
            self._check_stop()
            speed = leg.speed_kmh or self._walk["speed_kmh"]      # a leg's own speed beats the live slider
            natural = 1.0
            if self._on("natural_walk"):
                dist, angle = follower.upcoming_turn()
                natural = pace.factor(self.walk_tick, dist, angle)
            lat, lon, done = follower.advance(speed / 3.6 * factor * natural * self.walk_tick)
            await self._push(lat, lon)
            self._walk["cur_speed_kmh"] = speed * factor * natural
            self._walk["remaining_m"] = follower.remaining_m
            if done:
                return
            if self._on("traffic_stops") and leg.mode == "drive":
                for angle in follower.passed_turns:
                    secs = traffic_stop_seconds(angle, self._rng)
                    if secs:
                        await self._dwell(secs, "traffic")
                        self._walk.update(state="moving", reason=None)
                        break
            await asyncio.sleep(self.walk_tick)
            self._check_stop()

    async def _dwell(self, seconds: float, reason: Optional[str] = None) -> None:
        self._walk.update(state="waiting", wait_left_s=seconds, remaining_m=0.0, reason=reason)
        end = time.monotonic() + seconds
        while (left := end - time.monotonic()) > 0:
            self._walk["wait_left_s"] = left
            await asyncio.sleep(min(self.walk_tick, left))
            self._check_stop()
        self._walk["wait_left_s"] = 0.0


@dataclass
class Stop:
    lat: float
    lon: float
    dwell_s: float = 0.0
    mode: Optional[str] = None         # path type for the leg that ARRIVES at this stop (None = plan default)
    speed_kmh: Optional[float] = None  # pace for that leg (None = the live speed setting)


@dataclass
class JourneyPlan:
    stops: list[Stop]
    mode: str = "walk"            # straight | walk | drive
    speed_kmh: float = 5.0
    loop: bool = False
    jump_to_first: bool = False   # teleport to the first stop (or track start) instead of walking there
    kind: str = "route"           # route | trick_or_treat
    speed_jitter: float = 0.0     # +/- fraction of speed, re-rolled per leg
    return_to_start: bool = False  # walk back to the starting point when finished
    path: Optional[list] = None   # an exact polyline (GPX track) to follow instead of routing between stops
    realism: Optional[dict] = None  # per-trip overrides of the realism switches, e.g. {"noise": True, "natural_walk": True}


@dataclass
class _Leg:
    points: list
    index: int                    # which stop (0-based) this leg arrives at
    dwell_s: float = 0.0
    mode: str = "walk"
    speed_kmh: Optional[float] = None
    returning: bool = False
