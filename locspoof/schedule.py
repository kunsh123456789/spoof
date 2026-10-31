"""Timed plans: a list of steps, each to run at a real-world clock time ("at 8:00 PM start trick-or-treat, at
8:30 PM walk home shakily at 5 km/h").

A plan only runs while it is *activated* AND this app is running with the Mac awake and the phone connected. If the
app was not running at the planned time the step is run late, up to a grace period (default 10 minutes), after which
it is marked "missed" rather than surprising you hours later.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import datetime
from typing import Any, Awaitable, Callable, Optional

from .errors import AppError
from .favorites import FavoritesStore
from .movement import polygon_area_m2, validate_coords

log = logging.getLogger("locspoof.schedule")

STEP_TYPES = ("set_location", "move_to", "route", "trick_or_treat", "clear", "stop")
MODES = ("straight", "walk", "drive")
MAX_STEPS = 50
DEFAULT_GRACE_MIN = 10
MAX_GRACE_MIN = 720
MIN_AREA_M2, MAX_AREA_M2 = 2_000, 4_000_000
# Failures that are usually temporary (phone asleep, cable out, no internet yet): a step keeps retrying until its grace ends.
RETRYABLE = {"no_device", "connection_lost", "device_error", "usbmuxd_unavailable", "device_locked", "houses_unavailable",
             "route_failed", "rate_limited", "search_offline", "connecting", "tunnel_needed"}


def parse_at(value: Any) -> float:
    """A local date-time like '2026-10-31T20:00' -> a timestamp."""
    if not isinstance(value, str):
        raise ValueError("time must be text")
    return datetime.fromisoformat(value.strip()).timestamp()


def step_ts(step: dict) -> Optional[float]:
    """When a step is due: its clock time, or (for "N seconds after the last move") the moment the phone went still plus N.
    None = a relative step whose moment has not come yet."""
    if step.get("after_s") is not None:
        return None if step.get("idle_since") is None else step["idle_since"] + step["after_s"]
    return parse_at(step["at"])


TERMINAL = ("done", "error", "missed", "skipped")


def _num(p: dict, key: str, lo: float, hi: float, default: Optional[float] = None) -> float:
    try:
        v = float(p[key]) if key in p and p[key] not in (None, "") else default
    except (TypeError, ValueError):
        v = None
    if v is None or not lo <= v <= hi:
        raise AppError("bad_step", f"“{key}” must be a number from {lo:g} to {hi:g}.", status=422)
    return v


def _coords(p: dict, prefix: str = "") -> tuple[float, float]:
    try:
        lat, lon = float(p[prefix + "lat"]), float(p[prefix + "lon"])
        validate_coords(lat, lon)
        return lat, lon
    except (KeyError, TypeError, ValueError) as e:
        raise AppError("bad_step", "A step needs a valid place (latitude and longitude).", "Use “Find” to look up an address.", 422) from e


def clean_step(raw: Any, index: int = 0) -> dict:
    if not isinstance(raw, dict):
        raise AppError("bad_step", f"Step {index + 1} is not valid.", status=422)
    t = raw.get("type")
    if t not in STEP_TYPES:
        raise AppError("bad_step", f"Step {index + 1}: unknown action “{t}”.", status=422)
    after_s = None
    if raw.get("after_s") not in (None, ""):                      # "N seconds/minutes after the last move" instead of a clock time
        after_s = int(_num(raw, "after_s", 1, 86400))
        at = ""
    else:
        try:
            at = datetime.fromisoformat(str(raw.get("at", "")).strip()).strftime("%Y-%m-%dT%H:%M:%S")
        except ValueError as e:
            raise AppError("bad_step", f"Step {index + 1} needs a date and time (or a wait after the last move).", status=422) from e
    p = raw.get("params") if isinstance(raw.get("params"), dict) else {}
    out: dict = {}
    if t == "set_location":
        out["lat"], out["lon"] = _coords(p)
        out["label"] = str(p.get("label") or "")[:80]
    elif t == "move_to":
        out["lat"], out["lon"] = _coords(p)
        out["label"] = str(p.get("label") or "")[:80]
        mode = p.get("mode", "walk")
        if mode not in MODES:
            raise AppError("bad_step", f"Step {index + 1}: path type must be straight, walk or drive.", status=422)
        out["mode"] = mode
        out["speed_kmh"] = _num(p, "speed_kmh", 0.5, 400, 5)
        out["shaky"] = bool(p.get("shaky"))
        if p.get("start"):
            sl, so = _coords(p["start"] if isinstance(p["start"], dict) else {})
            out["start"] = {"lat": sl, "lon": so, "label": str((p["start"] or {}).get("label") or "")[:80]}
    elif t == "route":
        rid = str(p.get("route_id") or "")
        if not rid:
            raise AppError("bad_step", f"Step {index + 1}: choose a saved route.", status=422)
        out["route_id"], out["jump"] = rid, bool(p.get("jump", True))
        out["label"] = str(p.get("label") or "")[:80]
    elif t == "trick_or_treat":
        out["houses"] = int(_num(p, "houses", 3, 60, 20))
        out["radius_m"] = int(_num(p, "radius_m", 100, 1500, 350))
        poly = p.get("polygon")
        if poly:
            try:
                pts = [(float(a), float(b)) for a, b in poly]
                for a, b in pts:
                    validate_coords(a, b)
            except (TypeError, ValueError) as e:
                raise AppError("bad_step", f"Step {index + 1}: the area is not valid.", status=422) from e
            if not 3 <= len(pts) <= 100:
                raise AppError("bad_step", f"Step {index + 1}: the area needs 3 to 100 points.", status=422)
            area = polygon_area_m2(pts)
            if not MIN_AREA_M2 <= area <= MAX_AREA_M2:
                raise AppError("bad_step", f"Step {index + 1}: the area must be between 0.002 and 4 km².", status=422)
            out["polygon"] = [[a, b] for a, b in pts]
            if p.get("lat") not in (None, "") and p.get("lon") not in (None, ""):
                out["lat"], out["lon"] = _coords(p)
        else:
            out["lat"], out["lon"] = _coords(p)
        out["label"] = str(p.get("label") or "")[:80]
        for k in ("kids", "avoid_busy", "realistic"):
            out[k] = bool(p.get(k))
    step = {"id": str(raw.get("id") or uuid.uuid4().hex[:8])[:16], "at": at, "type": t, "title": str(raw.get("title") or "")[:100],
            "params": out, "status": "pending", "note": ""}
    if after_s is not None:
        step["after_s"] = after_s
    return step


def clean_plan(raw: Any) -> dict:
    if not isinstance(raw, dict):
        raise AppError("bad_plan", "That plan is not valid.", status=422)
    name = " ".join(str(raw.get("name") or "").split())[:80]
    if not name:
        raise AppError("bad_plan", "Give the plan a name.", status=422)
    steps_raw = raw.get("steps")
    if not isinstance(steps_raw, list) or not 1 <= len(steps_raw) <= MAX_STEPS:
        raise AppError("bad_plan", f"A plan needs 1 to {MAX_STEPS} steps.", status=422)
    try:
        grace = int(raw.get("grace_min", DEFAULT_GRACE_MIN))
    except (TypeError, ValueError):
        grace = -1
    if not 0 <= grace <= MAX_GRACE_MIN:
        raise AppError("bad_plan", f"“Run late steps up to” must be 0 to {MAX_GRACE_MIN} minutes.", status=422)
    steps = [clean_step(s, i) for i, s in enumerate(steps_raw)]
    ids = [s["id"] for s in steps]
    if len(set(ids)) != len(ids):                                # a copied step keeps its id: make them unique
        seen: set[str] = set()
        for s in steps:
            while s["id"] in seen:
                s["id"] = uuid.uuid4().hex[:8]
            seen.add(s["id"])
    if not any("after_s" in s for s in steps):                   # clock times: keep them in time order; waits keep the order you gave
        steps.sort(key=lambda s: parse_at(s["at"]))
    return {"name": name, "device": str(raw.get("device") or "")[:100], "grace_min": grace, "steps": steps}


class SchedulesStore(FavoritesStore):
    """schedules.json: a list of plans, each with its own steps and their live statuses."""

    required = {"id", "name", "steps"}

    def create(self, plan: dict) -> dict:
        item = {"id": uuid.uuid4().hex[:12], "armed": False, "created": time.time(), **plan}
        with self._lock:
            items = self._read()
            items.append(item)
            self._write(items)
        return item

    def get(self, plan_id: str) -> Optional[dict]:
        return next((p for p in self.list() if p["id"] == plan_id), None)

    def replace(self, plan_id: str, plan: dict) -> Optional[dict]:
        """Editing a plan disarms it and resets every step (a half-run plan with changed steps would be confusing)."""
        with self._lock:
            items = self._read()
            for i, old in enumerate(items):
                if old["id"] == plan_id:
                    items[i] = {**old, **plan, "id": plan_id, "armed": False}
                    self._write(items)
                    return items[i]
        return None

    def _mutate(self, plan_id: str, fn: Callable[[dict], Any]) -> Optional[Any]:
        with self._lock:
            items = self._read()
            for p in items:
                if p["id"] == plan_id:
                    result = fn(p)
                    self._write(items)
                    return result if result is not None else p
        return None

    def set_armed(self, plan_id: str, armed: bool, now: float) -> Optional[dict]:
        """Activate / deactivate. Activating marks steps that are already too late as missed (so a plan made for
        yesterday cannot fire a pile of old steps)."""
        skipped = {"n": 0}

        def go(p: dict) -> None:
            p["armed"] = armed
            if armed:
                p["armed_at"] = now
                grace = p.get("grace_min", DEFAULT_GRACE_MIN) * 60
                for s in p["steps"]:
                    if s["status"] == "pending" and s.get("after_s") is None and now - parse_at(s["at"]) > grace:
                        s["status"], s["note"] = "missed", "Already in the past when the plan was activated."
                        skipped["n"] += 1

        plan = self._mutate(plan_id, go)
        if plan is not None:
            plan = dict(plan)
            plan["skipped_on_arm"] = skipped["n"]
        return plan

    def reset(self, plan_id: str) -> Optional[dict]:
        def go(p: dict) -> None:
            p["armed"] = False
            for s in p["steps"]:
                s["status"], s["note"] = "pending", ""
                s.pop("ran_at", None)
                s.pop("idle_since", None)

        return self._mutate(plan_id, go)

    def update_step(self, plan_id: str, step_id: str, **fields: Any) -> Optional[dict]:
        def go(p: dict) -> None:
            for s in p["steps"]:
                if s["id"] == step_id:
                    s.update(fields)

        return self._mutate(plan_id, go)

    def recover_interrupted(self) -> int:
        """At start-up: a step still marked 'running' belonged to a run that was cut short (the app was closed)."""
        n = {"n": 0}
        with self._lock:
            items = self._read()
            for p in items:
                for s in p["steps"]:
                    if s["status"] == "running":
                        s["status"], s["note"] = "error", "Interrupted: the app was closed while this step was running."
                        n["n"] += 1
            if n["n"]:
                self._write(items)
        return n["n"]


class Scheduler:
    """Watches the clock and runs the steps of activated plans at their time."""

    def __init__(self, store: SchedulesStore, execute: Callable[[dict, dict], Awaitable[str]], clock: Callable[[], float] = time.time,
                 tick_every: float = 1.0, retry_every: float = 15.0, on_event: Optional[Callable[[dict, dict, str, str], None]] = None,
                 is_moving: Optional[Callable[[dict], bool]] = None):
        self.store, self.execute, self.clock = store, execute, clock
        self.is_moving = is_moving or (lambda plan: False)   # is this plan's phone still on a trip? ("N min after the last move")
        self.on_event = on_event          # called as (plan, step, status, note) whenever a step changes state
        self.tick_every, self.retry_every = tick_every, retry_every
        self._loop_task: Optional[asyncio.Task] = None
        self._tasks: dict[tuple[str, str], asyncio.Task] = {}

    def _emit(self, plan: dict, step: dict, status: str, note: str = "") -> None:
        if self.on_event is not None:
            try:
                self.on_event(plan, step, status, note)
            except Exception:
                log.debug("on_event failed", exc_info=True)

    async def start(self) -> None:
        n = self.store.recover_interrupted()
        if n:
            log.warning("%d scheduled step(s) were interrupted by the last shutdown", n)
        self._loop_task = asyncio.create_task(self._loop(), name="locspoof-scheduler")

    async def stop(self) -> None:
        tasks = [t for t in [self._loop_task, *self._tasks.values()] if t is not None and not t.done()]
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.wait(tasks, timeout=5)
        self._loop_task = None

    async def _loop(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("scheduler tick failed")          # one bad tick must never end the scheduler
            await asyncio.sleep(self.tick_every)

    def _track_waits(self, plan: dict, now: float) -> dict:
        """Start (or restart) the countdown of "N min after the last move" steps: it runs while the step before has finished
        and the phone is standing still, and starts over if the phone moves again."""
        steps = plan["steps"]
        for i, s in enumerate(steps):
            if s["status"] != "pending" or s.get("after_s") is None:
                continue
            prev_ok = i == 0 or steps[i - 1]["status"] in TERMINAL
            if not prev_ok:
                continue
            if self.is_moving(plan):
                if s.get("idle_since") is not None:
                    self.store.update_step(plan["id"], s["id"], idle_since=None)
            elif s.get("idle_since") is None:
                anchor = max(plan.get("armed_at") or now, 0) if i == 0 else now
                self.store.update_step(plan["id"], s["id"], idle_since=min(anchor, now))
        return self.store.get(plan["id"]) or plan

    async def tick(self) -> None:
        now = self.clock()
        for plan in self.store.list():
            if not plan.get("armed"):
                continue
            plan = self._track_waits(plan, now)
            grace = plan.get("grace_min", DEFAULT_GRACE_MIN) * 60
            due = sorted((s for s in plan["steps"] if s["status"] == "pending" and step_ts(s) is not None and step_ts(s) <= now), key=step_ts)
            if not due:
                continue
            *older, current = due
            for s in older:        # several steps came due together (the Mac slept): only the latest still matters
                late = now - step_ts(s)
                if late <= grace:
                    self.store.update_step(plan["id"], s["id"], status="skipped", note="Skipped: a later step was already due.")
                    self._emit(plan, s, "skipped", "a later step was already due")
                else:
                    self.store.update_step(plan["id"], s["id"], status="missed", note=self._late_note(late, grace))
                    self._emit(plan, s, "missed", self._late_note(late, grace))
            late = now - step_ts(current)
            if late > grace:
                self.store.update_step(plan["id"], current["id"], status="missed", note=self._late_note(late, grace))
                self._emit(plan, current, "missed", self._late_note(late, grace))
                continue
            self.store.update_step(plan["id"], current["id"], status="running", note="Started" + (f" {int(late)} s late." if late > 5 else "."))
            self._emit(plan, current, "running", f"{int(late)} s late" if late > 5 else "")
            key = (plan["id"], current["id"])
            task = asyncio.create_task(self._run_step(plan, current, step_ts(current) + grace))
            self._tasks[key] = task
            task.add_done_callback(lambda _t, k=key: self._tasks.pop(k, None))

    @staticmethod
    def _late_note(late: float, grace: float) -> str:
        return (f"Missed: it was {int(late // 60)} min late (the app was not running or the Mac was asleep) and the limit "
                f"is {int(grace // 60)} min.")

    async def _run_step(self, plan: dict, step: dict, deadline: float) -> None:
        pid, sid = plan["id"], step["id"]
        while True:
            try:
                note = await self.execute(plan, step)
                self.store.update_step(pid, sid, status="done", note=note or "Done.", ran_at=self.clock())
                self._emit(plan, step, "done", note or "")
                log.info("scheduled step done: %s / %s", plan["name"], step["type"])
                return
            except asyncio.CancelledError:
                self.store.update_step(pid, sid, status="error", note="Interrupted: the app was closing.")
                raise
            except AppError as e:
                if e.code in RETRYABLE and self.clock() + self.retry_every < deadline:
                    self.store.update_step(pid, sid, note=f"Waiting to retry: {e.message}")
                    log.info("scheduled step will retry (%s): %s", e.code, e.message)
                    await asyncio.sleep(self.retry_every)
                    continue
                self.store.update_step(pid, sid, status="error", note=f"{e.message} {e.hint or ''}".strip(), ran_at=self.clock())
                self._emit(plan, step, "error", e.message)
                log.warning("scheduled step failed: %s / %s: %s", plan["name"], step["type"], e.message)
                return
            except Exception as e:
                log.exception("scheduled step crashed")
                self.store.update_step(pid, sid, status="error", note=f"Unexpected error: {e}", ran_at=self.clock())
                return

    async def run_now(self, plan_id: str, step_id: str) -> str:
        """Run one step immediately as a test (its schedule status is not changed)."""
        plan = self.store.get(plan_id)
        step = next((s for s in (plan or {}).get("steps", []) if s["id"] == step_id), None)
        if plan is None or step is None:
            raise AppError("not_found", "That step was not found.", status=404)
        return await self.execute(plan, step)

    # -- for the UI and the keep-awake logic ------------------------------------------------------------------

    def next_event(self) -> Optional[dict]:
        now = self.clock()
        best = None
        for plan in self.store.list():
            if not plan.get("armed"):
                continue
            grace = plan.get("grace_min", DEFAULT_GRACE_MIN) * 60
            for s in plan["steps"]:
                at = step_ts(s) if s["status"] == "pending" else None
                if at is not None and now - at <= grace:
                    if best is None or at < best["ts"]:
                        best = {"ts": at, "plan_id": plan["id"], "plan_name": plan["name"], "step_id": s["id"], "type": s["type"],
                                "title": s["title"], "at": datetime.fromtimestamp(at).strftime("%Y-%m-%dT%H:%M:%S"), "in_s": round(at - now)}
        if best:
            best.pop("ts")
        return best

    def wants_awake(self, horizon_s: float = 24 * 3600) -> bool:
        """Is an activated plan going to need the Mac within the next day (or running right now)?"""
        now = self.clock()
        for plan in self.store.list():
            if not plan.get("armed"):
                continue
            grace = plan.get("grace_min", DEFAULT_GRACE_MIN) * 60
            steps = plan["steps"]
            for i, s in enumerate(steps):
                if s["status"] == "running":
                    return True
                if s["status"] != "pending":
                    continue
                if s.get("after_s") is not None:                  # a wait after the last move: the Mac is needed while that chain is under way
                    if i == 0 or steps[i - 1]["status"] in TERMINAL or steps[i - 1]["status"] == "running":
                        return True
                    continue
                if parse_at(s["at"]) - now <= horizon_s and now - parse_at(s["at"]) <= grace:
                    return True
        return False
