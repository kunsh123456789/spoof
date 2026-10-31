"""Timed plans: validation, the store, the scheduler's timing rules, and plans running against fake phones."""
import asyncio
import random
import time
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from conftest import FakeBackend, FakeHouses, FakeRouter
from locspoof.errors import AppError
from locspoof.schedule import Scheduler, SchedulesStore, clean_plan, clean_step, parse_at
from locspoof.server import create_app


def ts(hms, day="2026-10-31"):
    return datetime.fromisoformat(f"{day}T{hms}").timestamp()


class Clock:
    def __init__(self, hms="19:59:50"):
        self.t = ts(hms)

    def __call__(self):
        return self.t

    def set(self, hms, day="2026-10-31"):
        self.t = ts(hms, day)


def step(at, type_="clear", **params):
    return {"at": f"2026-10-31T{at}", "type": type_, "params": params}


def plan(*steps, name="Halloween", grace=10, device=""):
    return {"name": name, "grace_min": grace, "device": device, "steps": list(steps)}


# ---- validation -------------------------------------------------------------------------------------

def test_clean_plan_normalises_sorts_and_resets_status():
    p = clean_plan(plan(step("20:30", "move_to", lat=1, lon=2, speed_kmh=5, shaky=True), step("20:00", "clear"), name="  Night   out "))
    assert p["name"] == "Night out" and [s["at"][11:16] for s in p["steps"]] == ["20:00", "20:30"]
    assert all(s["status"] == "pending" and s["note"] == "" for s in p["steps"])
    assert p["steps"][1]["params"] == {"lat": 1.0, "lon": 2.0, "label": "", "mode": "walk", "speed_kmh": 5.0, "shaky": True}
    assert len({s["id"] for s in p["steps"]}) == 2 and p["grace_min"] == 10 and p["device"] == ""


def test_duplicate_step_ids_are_made_unique():
    a = {**step("20:00"), "id": "same"}
    b = {**step("20:10"), "id": "same"}
    assert len({s["id"] for s in clean_plan(plan(a, b))["steps"]}) == 2


@pytest.mark.parametrize("bad,hint", [
    (plan(), "1 to 50"),
    (plan(*[step("20:00")] * 51), "1 to 50"),
    (plan(step("20:00"), name="  "), "name"),
    (plan(step("20:00"), grace=-1), "minutes"),
    (plan(step("20:00"), grace=99999), "minutes"),
    (plan({"at": "soon", "type": "clear"}), "date and time"),
    (plan({"at": "2026-10-31T20:00", "type": "explode"}), "unknown action"),
    (plan(step("20:00", "set_location", lat=95, lon=0)), "valid place"),
    (plan(step("20:00", "set_location", lat="x", lon=0)), "valid place"),
    (plan(step("20:00", "move_to", lat=1, lon=2, mode="fly")), "path type"),
    (plan(step("20:00", "move_to", lat=1, lon=2, speed_kmh=9999)), "speed_kmh"),
    (plan(step("20:00", "move_to", lat=1, lon=2, start={"lat": 99, "lon": 0})), "valid place"),
    (plan(step("20:00", "route")), "saved route"),
    (plan(step("20:00", "trick_or_treat")), "valid place"),
    (plan(step("20:00", "trick_or_treat", lat=1, lon=2, houses=1)), "houses"),
    (plan(step("20:00", "trick_or_treat", polygon=[[1, 1], [1, 2]])), "3 to 100"),
    (plan(step("20:00", "trick_or_treat", polygon=[[40, -75], [40, -74], [41, -74], [41, -75]])), "4 km"),
    (plan(step("20:00", "trick_or_treat", polygon=[[40, -75], [40, -74.99999], [40.00001, -75]])), "4 km"),
    ("nope", "not valid"),
])
def test_bad_plans_get_clear_messages(bad, hint):
    with pytest.raises(AppError) as e:
        clean_plan(bad)
    assert e.value.status == 422 and hint in (e.value.message + (e.value.hint or ""))


def test_every_step_type_accepts_its_params():
    sq = [[40.0, -75.0], [40.0, -74.996], [40.003, -74.996], [40.003, -75.0]]
    p = clean_plan(plan(
        step("20:00", "trick_or_treat", polygon=sq, houses=15, radius_m=300, kids=True, avoid_busy=True, realistic=True),
        step("20:10", "set_location", lat=1, lon=2, label="Cafe"),
        step("20:20", "route", route_id="abc", jump=False),
        step("20:30", "move_to", lat=3, lon=4, label="Home", mode="drive", speed_kmh=50, start={"lat": 5, "lon": 6}),
        step("20:40", "stop"), step("20:50", "clear")))
    tot = p["steps"][0]["params"]
    assert tot["houses"] == 15 and tot["kids"] and tot["avoid_busy"] and tot["realistic"] and len(tot["polygon"]) == 4
    assert p["steps"][2]["params"] == {"route_id": "abc", "jump": False, "label": ""}
    assert p["steps"][3]["params"]["start"] == {"lat": 5.0, "lon": 6.0, "label": ""}


# ---- the store ----------------------------------------------------------------------------------------------

def test_store_lifecycle_and_safety(tmp_path):
    st = SchedulesStore(tmp_path / "s.json")
    created = st.create(clean_plan(plan(step("20:00"), step("20:30"))))
    assert created["armed"] is False and st.get(created["id"])["name"] == "Halloween"
    st.update_step(created["id"], created["steps"][0]["id"], status="done", note="ok")
    assert st.get(created["id"])["steps"][0]["status"] == "done"
    edited = st.replace(created["id"], clean_plan(plan(step("21:00"), name="Renamed")))
    assert edited["name"] == "Renamed" and edited["armed"] is False and edited["steps"][0]["status"] == "pending"
    assert st.replace("nope", clean_plan(plan(step("21:00")))) is None and st.delete(created["id"]) and not st.delete(created["id"])


def test_arming_marks_steps_already_in_the_past_as_missed(tmp_path):
    st = SchedulesStore(tmp_path / "s.json")
    p = st.create(clean_plan(plan(step("08:00"), step("19:55"), step("20:30"), grace=10)))
    armed = st.set_armed(p["id"], True, ts("20:00"))
    assert armed["armed"] and armed["skipped_on_arm"] == 1                      # 08:00 is long gone; 19:55 is within grace
    status = {s["at"][11:16]: s["status"] for s in armed["steps"]}
    assert status == {"08:00": "missed", "19:55": "pending", "20:30": "pending"}
    assert st.set_armed(p["id"], False, ts("20:00"))["armed"] is False and st.set_armed("nope", True, 0) is None
    reset = st.reset(p["id"])
    assert all(s["status"] == "pending" for s in reset["steps"]) and reset["armed"] is False


def test_interrupted_steps_are_recovered_at_startup(tmp_path):
    st = SchedulesStore(tmp_path / "s.json")
    p = st.create(clean_plan(plan(step("20:00"), step("20:30"))))
    st.update_step(p["id"], p["steps"][0]["id"], status="running")
    assert SchedulesStore(tmp_path / "s.json").recover_interrupted() == 1
    s = SchedulesStore(tmp_path / "s.json").get(p["id"])["steps"][0]
    assert s["status"] == "error" and "Interrupted" in s["note"]


# ---- the scheduler's timing rules (fake clock, fake actions) ------------------------------------------------------

class Harness:
    def __init__(self, tmp_path, hms="19:59:50", **kw):
        self.clock = Clock(hms)
        self.store = SchedulesStore(tmp_path / "s.json")
        self.calls, self.behaviour = [], lambda p, s, n: "did it"
        self.attempts = {}

        async def execute(plan_, step_):
            n = self.attempts[step_["id"]] = self.attempts.get(step_["id"], 0) + 1
            self.calls.append((self.clock.t, step_["type"], step_["id"]))
            return self.behaviour(plan_, step_, n)

        self.sched = Scheduler(self.store, execute, clock=self.clock, tick_every=0.01, retry_every=0.01, **kw)

    def add(self, *steps, armed=True, **kw):
        p = self.store.create(clean_plan(plan(*steps, **kw)))
        if armed:
            self.store.set_armed(p["id"], True, self.clock.t)
        return self.store.get(p["id"])

    async def tick(self):
        await self.sched.tick()
        if self.sched._tasks:
            await asyncio.gather(*list(self.sched._tasks.values()), return_exceptions=True)

    def statuses(self, plan_id):
        return {s["at"][11:16]: s["status"] for s in self.store.get(plan_id)["steps"]}


def run(coro):
    return asyncio.run(coro)


def test_a_step_runs_exactly_once_and_only_at_its_time(tmp_path):
    h = Harness(tmp_path)
    p = h.add(step("20:00", "clear"))

    async def go():
        await h.tick()
        assert h.calls == []                                       # 19:59:50: not yet
        h.clock.set("20:00:00")
        await h.tick()
        await h.tick()
        h.clock.set("20:05:00")
        await h.tick()

    run(go())
    assert len(h.calls) == 1 and h.statuses(p["id"]) == {"20:00": "done"}
    assert h.store.get(p["id"])["steps"][0]["note"] == "did it"


def test_a_plan_that_is_not_activated_never_runs(tmp_path):
    h = Harness(tmp_path)
    h.add(step("20:00"), armed=False)
    h.clock.set("20:00:30")
    run(h.tick())
    assert h.calls == []


def test_steps_run_in_order_at_their_own_times(tmp_path):
    h = Harness(tmp_path)
    p = h.add(step("20:00", "trick_or_treat", lat=1, lon=2), step("20:30", "move_to", lat=3, lon=4, speed_kmh=5, shaky=True), step("21:00", "clear"))

    async def go():
        for hms in ("20:00:00", "20:15:00", "20:30:01", "20:45:00", "21:00:00"):
            h.clock.set(hms)
            await h.tick()

    run(go())
    assert [c[1] for c in h.calls] == ["trick_or_treat", "move_to", "clear"]
    assert [datetime.fromtimestamp(c[0]).strftime("%H:%M") for c in h.calls] == ["20:00", "20:30", "21:00"]
    assert set(h.statuses(p["id"]).values()) == {"done"}


def test_a_step_that_is_a_little_late_still_runs_and_says_so(tmp_path):
    h = Harness(tmp_path)
    p = h.add(step("20:00"))
    h.clock.set("20:04:00")                                        # the Mac was asleep for 4 minutes
    run(h.tick())
    s = h.store.get(p["id"])["steps"][0]
    assert s["status"] == "done" and len(h.calls) == 1


def test_a_step_beyond_the_grace_period_is_marked_missed_and_not_run(tmp_path):
    h = Harness(tmp_path)
    p = h.add(step("20:00"), step("20:30"), grace=10)
    h.clock.set("20:25:00")
    run(h.tick())
    s = h.store.get(p["id"])["steps"]
    assert h.calls == [] and s[0]["status"] == "missed" and "25 min late" in s[0]["note"] and "10 min" in s[0]["note"]
    h.clock.set("20:30:00")                                        # the next step is unaffected
    run(h.tick())
    assert h.statuses(p["id"]) == {"20:00": "missed", "20:30": "done"}


def test_when_several_steps_are_due_together_only_the_latest_runs(tmp_path):
    h = Harness(tmp_path)
    p = h.add(step("20:00", "trick_or_treat", lat=1, lon=2), step("20:10", "move_to", lat=3, lon=4), step("20:20", "clear"), grace=60)
    h.clock.set("20:25:00")                                        # woke up with all three overdue but within grace
    run(h.tick())
    assert [c[1] for c in h.calls] == ["clear"]
    s = h.store.get(p["id"])["steps"]
    assert [x["status"] for x in s] == ["skipped", "skipped", "done"] and "later step" in s[0]["note"]


def test_temporary_failures_are_retried_until_they_work(tmp_path):
    h = Harness(tmp_path)

    def flaky(p, s, n):
        if n < 3:
            raise AppError("no_device", "No iPhone found.", status=503)
        return "finally"

    h.behaviour = flaky
    p = h.add(step("20:00"), grace=10)
    h.clock.set("20:00:00")
    run(h.tick())
    s = h.store.get(p["id"])["steps"][0]
    assert h.attempts[s["id"]] == 3 and s["status"] == "done" and s["note"] == "finally"


def test_a_step_gives_up_retrying_when_its_grace_ends_and_reports_why(tmp_path):
    h = Harness(tmp_path)
    h.behaviour = lambda p, s, n: (_ for _ in ()).throw(AppError("no_device", "No iPhone found.", "Plug it in.", 503))
    p = h.add(step("20:00"), grace=0)                              # no grace: one attempt only
    h.clock.set("20:00:00")
    run(h.tick())
    s = h.store.get(p["id"])["steps"][0]
    assert s["status"] == "error" and "No iPhone found." in s["note"] and "Plug it in." in s["note"] and h.attempts[s["id"]] == 1


def test_permanent_errors_are_not_retried_and_do_not_stop_later_steps(tmp_path):
    h = Harness(tmp_path)
    h.behaviour = lambda p, s, n: (_ for _ in ()).throw(AppError("not_found", "The saved route no longer exists.", status=404)) if s["type"] == "route" else "ok"
    p = h.add(step("20:00", "route", route_id="gone"), step("20:10", "clear"))

    async def go():
        h.clock.set("20:00:00"); await h.tick()
        h.clock.set("20:10:00"); await h.tick()

    run(go())
    assert h.statuses(p["id"]) == {"20:00": "error", "20:10": "done"} and h.attempts[h.store.get(p["id"])["steps"][0]["id"]] == 1


def test_an_unexpected_crash_in_a_step_is_contained(tmp_path):
    h = Harness(tmp_path)
    h.behaviour = lambda p, s, n: 1 / 0
    p = h.add(step("20:00"))
    h.clock.set("20:00:00")
    run(h.tick())
    s = h.store.get(p["id"])["steps"][0]
    assert s["status"] == "error" and "Unexpected error" in s["note"]


def test_the_loop_survives_a_failing_tick_and_stops_cleanly(tmp_path):
    h = Harness(tmp_path)
    n = {"n": 0}
    real = h.sched.tick

    async def flaky_tick():
        n["n"] += 1
        if n["n"] == 1:
            raise RuntimeError("boom")
        await real()

    h.sched.tick = flaky_tick

    async def go():
        await h.sched.start()
        await asyncio.sleep(0.15)
        await h.sched.stop()

    run(go())
    assert n["n"] > 3


def test_stopping_the_scheduler_interrupts_a_running_step_and_says_so(tmp_path):
    h = Harness(tmp_path)

    async def slow(plan_, step_):
        await asyncio.sleep(3600)

    h.sched.execute = slow
    p = h.add(step("20:00"))
    h.clock.set("20:00:00")

    async def go():
        await h.sched.tick()
        await asyncio.sleep(0.05)
        await h.sched.stop()

    run(go())
    s = h.store.get(p["id"])["steps"][0]
    assert s["status"] == "error" and "closing" in s["note"]


def test_next_event_and_awake_hint(tmp_path):
    h = Harness(tmp_path, "19:00:00")
    assert h.sched.next_event() is None and not h.sched.wants_awake()
    h.add({**step("20:00", "trick_or_treat", lat=1, lon=2), "title": "Candy"}, step("20:30"), name="A")
    nxt = h.sched.next_event()
    assert nxt["type"] == "trick_or_treat" and nxt["in_s"] == 3600 and nxt["plan_name"] == "A" and nxt["title"] == "Candy"
    assert h.sched.wants_awake()
    h.clock.set("23:59:00")                                        # both far past their grace: nothing left to wait for
    assert h.sched.next_event() is None and not h.sched.wants_awake()
    h2 = Harness(tmp_path / "x" if False else tmp_path, "06:00:00")
    far = SchedulesStore(tmp_path / "far.json")
    far.set_armed(far.create(clean_plan(plan({"at": "2026-12-25T20:00", "type": "clear"})))["id"], True, h2.clock.t)
    assert not Scheduler(far, lambda *a: None, clock=h2.clock).wants_awake() and Scheduler(far, lambda *a: None, clock=h2.clock).next_event()["in_s"] > 24 * 3600


def test_run_now_is_a_test_that_leaves_the_schedule_alone(tmp_path):
    h = Harness(tmp_path)
    p = h.add(step("20:00"), armed=False)
    sid = p["steps"][0]["id"]
    assert run(h.sched.run_now(p["id"], sid)) == "did it" and h.store.get(p["id"])["steps"][0]["status"] == "pending"
    with pytest.raises(AppError):
        run(h.sched.run_now(p["id"], "nope"))


def test_parse_at_reads_local_times():
    assert parse_at("2026-10-31T20:00") == ts("20:00:00") and parse_at("2026-10-31T20:00:30") == ts("20:00:30")
    with pytest.raises(ValueError):
        parse_at(5)


# ---- whole plans against fake phones ------------------------------------------------------------------------------

@pytest.fixture
def env(tmp_path):
    clock = Clock("19:59:00")
    backend = FakeBackend()
    app = create_app(backend=backend, router=FakeRouter(), house_finder=FakeHouses(), favorites_path=tmp_path / "f.json",
                     recents_path=tmp_path / "r.json", offline_root=tmp_path / "o", settings_path=tmp_path / "s.json",
                     routes_path=tmp_path / "rt.json", devices_path=tmp_path / "d.json", schedules_path=tmp_path / "sch.json",
                     schedule_clock=clock, schedule_tick=0.03, schedule_retry=0.05, walk_tick=0.01, ambient_tick=0.02, rng=random.Random(2))
    with TestClient(app) as c:
        c.clock, c.backend, c.manager, c.dir = clock, backend, app.state.manager, tmp_path
        yield c


def wait_for(fn, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(0.03)
    pytest.fail("condition not met in time")


def steps_of(c, pid):
    return {s["at"][11:16]: s for p in c.get("/api/schedules").json()["plans"] if p["id"] == pid for s in p["steps"]}


def test_the_complete_halloween_plan_runs_by_itself(env):
    c = env
    body = {"name": "Halloween night", "grace_min": 10, "steps": [
        {"at": "2026-10-31T20:00", "type": "trick_or_treat", "params": {"lat": 40.0, "lon": -75.0, "houses": 5, "radius_m": 300, "kids": True}},
        {"at": "2026-10-31T20:30", "type": "move_to", "params": {"lat": 40.001, "lon": -75.0, "label": "Home", "mode": "walk", "speed_kmh": 400, "shaky": True}},
        {"at": "2026-10-31T21:00", "type": "clear", "params": {}},
    ]}
    pid = c.post("/api/schedules", json=body).json()["id"]
    assert c.post(f"/api/schedules/{pid}/arm", json={"armed": True}).json()["armed"] is True
    time.sleep(0.2)
    assert c.backend.sessions == [] and steps_of(c, pid)["20:00"]["status"] == "pending"          # nothing happens before 8 PM

    c.clock.set("20:00:00")                                                                         # 8 PM arrives
    st = wait_for(lambda: steps_of(c, pid)["20:00"]["status"] in ("done", "error") and steps_of(c, pid)["20:00"])
    assert st["status"] == "done" and "Trick-or-treat started" in st["note"]
    w = wait_for(lambda: c.get("/api/status").json()["walk"].get("kind") == "trick_or_treat" and c.get("/api/status").json()["walk"])
    assert w["mode"] == "walk" and abs(w["speed_kmh"] - 3.2) < 0.01                                  # "kids" slowed the walk
    assert c.get("/api/settings").json()["tot_kids"] is False                                       # the global switch was not touched

    c.clock.set("20:30:00")                                                                         # 8:30: the shaky walk home
    st = wait_for(lambda: steps_of(c, pid)["20:30"]["status"] in ("done", "error") and steps_of(c, pid)["20:30"])
    assert st["status"] == "done" and "Home" in st["note"] and "shaky" in st["note"]
    wait_for(lambda: c.get("/api/status").json()["walk"].get("kind") == "route" or not c.get("/api/status").json()["walk"].get("active"))
    wait_for(lambda: not c.get("/api/status").json()["walk"]["active"])                             # arrived (400 km/h)
    sim = c.get("/api/status").json()["simulation"]
    assert (sim["lat"], sim["lon"]) == (40.001, -75.0)
    pts = [x[1:] for s in c.backend.sessions for x in s.calls if x[0] == "set"]
    assert any(abs(p[0] - 40.001) > 1e-6 and abs(p[0] - 40.0) > 1e-6 for p in pts[-40:])             # the walk home visibly wobbled
    assert c.get("/api/settings").json()["noise"] is False and c.manager._realism_override == {}      # per-trip switches, then back to normal

    c.clock.set("21:00:00")
    wait_for(lambda: steps_of(c, pid)["21:00"]["status"] == "done")
    assert c.get("/api/status").json()["simulation"] == {"active": False}                          # real GPS restored
    assert set(s["status"] for s in steps_of(c, pid).values()) == {"done"}


def test_set_location_and_saved_route_steps(env):
    c = env
    rid = c.post("/api/routes", json={"name": "Dog walk", "stops": [{"lat": 10, "lon": 10.0004}], "mode": "straight", "speed_kmh": 400}).json()["id"]
    pid = c.post("/api/schedules", json={"name": "Errands", "steps": [
        {"at": "2026-10-31T20:00", "type": "set_location", "params": {"lat": 10, "lon": 10, "label": "Cafe"}},
        {"at": "2026-10-31T20:10", "type": "route", "params": {"route_id": rid, "jump": False}},
        {"at": "2026-10-31T20:20", "type": "route", "params": {"route_id": "does-not-exist"}},
    ]}).json()["id"]
    c.post(f"/api/schedules/{pid}/arm", json={"armed": True})
    for hms, key in (("20:00:00", "20:00"), ("20:10:00", "20:10"), ("20:20:00", "20:20")):
        c.clock.set(hms)
        wait_for(lambda k=key: steps_of(c, pid)[k]["status"] in ("done", "error"))
    s = steps_of(c, pid)
    assert s["20:00"]["status"] == "done" and "Cafe" in s["20:00"]["note"]
    assert s["20:10"]["status"] == "done" and "Dog walk" in s["20:10"]["note"]
    assert s["20:20"]["status"] == "error" and "no longer exists" in s["20:20"]["note"]
    wait_for(lambda: c.get("/api/status").json()["simulation"].get("lon") == 10.0004)
    assert c.get("/api/recent").json()["recent"][0]["name"] in ("Cafe", "Route end")


def test_moving_with_a_start_point_teleports_first(env):
    c = env
    pid = c.post("/api/schedules", json={"name": "Commute", "steps": [{"at": "2026-10-31T20:00", "type": "move_to", "params": {
        "lat": 50.0, "lon": 20.0004, "mode": "straight", "speed_kmh": 400, "start": {"lat": 50.0, "lon": 20.0}}}]}).json()["id"]
    c.post(f"/api/schedules/{pid}/arm", json={"armed": True})
    c.clock.set("20:00:00")
    wait_for(lambda: steps_of(c, pid)["20:00"]["status"] == "done")
    wait_for(lambda: c.get("/api/status").json()["simulation"].get("lon") == 20.0004)
    assert [x[1:] for x in c.backend.sessions[0].calls if x[0] == "set"][0] == (50.0, 20.0)


def test_a_step_with_nothing_to_move_from_explains_what_to_do(env):
    c = env
    pid = c.post("/api/schedules", json={"name": "Oops", "grace_min": 0, "steps": [
        {"at": "2026-10-31T20:00", "type": "move_to", "params": {"lat": 1, "lon": 2, "mode": "straight"}}]}).json()["id"]
    c.post(f"/api/schedules/{pid}/arm", json={"armed": True})
    c.clock.set("20:00:00")
    s = wait_for(lambda: steps_of(c, pid)["20:00"]["status"] == "error" and steps_of(c, pid)["20:00"])
    assert "starting location" in s["note"]


def test_api_validation_editing_arming_and_deleting(env):
    c = env
    assert c.post("/api/schedules", json={"name": "x", "steps": []}).status_code == 422
    assert c.post("/api/schedules", json={"name": "x", "steps": [{"at": "bad", "type": "clear"}]}).status_code == 422
    assert c.post("/api/schedules/nope/arm", json={"armed": True}).status_code == 404
    assert c.put("/api/schedules/nope", json={"name": "x", "steps": [{"at": "2026-10-31T20:00", "type": "clear"}]}).status_code == 404
    pid = c.post("/api/schedules", json={"name": "P", "steps": [{"at": "2026-10-31T08:00", "type": "clear"}, {"at": "2026-10-31T20:30", "type": "clear"}]}).json()["id"]
    arm = c.post(f"/api/schedules/{pid}/arm", json={"armed": True}).json()
    assert arm["skipped_on_arm"] == 1 and c.get("/api/schedules").json()["next"]["at"].startswith("2026-10-31T20:30")
    edited = c.put(f"/api/schedules/{pid}", json={"name": "P2", "steps": [{"at": "2026-10-31T22:00", "type": "stop"}]}).json()
    assert edited["armed"] is False and edited["name"] == "P2" and c.get("/api/schedules").json()["next"] is None   # editing disarms
    assert c.post(f"/api/schedules/{pid}/reset").json()["armed"] is False
    assert c.delete(f"/api/schedules/{pid}").status_code == 200 and c.delete(f"/api/schedules/{pid}").status_code == 404


def test_a_step_can_be_test_run_without_changing_the_schedule(env):
    c = env
    pid = c.post("/api/schedules", json={"name": "T", "steps": [{"at": "2026-12-31T20:00", "type": "set_location", "params": {"lat": 7, "lon": 8}}]}).json()
    sid = pid["steps"][0]["id"]
    r = c.post(f"/api/schedules/{pid['id']}/steps/{sid}/run")
    assert r.status_code == 200 and "Location set" in r.json()["note"]
    assert c.get("/api/status").json()["simulation"]["lat"] == 7 and steps_of(c, pid["id"])["20:00"]["status"] == "pending"
    assert c.post(f"/api/schedules/{pid['id']}/steps/nope/run").status_code == 404


def test_plans_survive_a_restart_and_still_fire(tmp_path):
    clock = Clock("19:59:00")

    def make():
        return create_app(backend=FakeBackend(), router=FakeRouter(), house_finder=FakeHouses(), favorites_path=tmp_path / "f.json",
                          recents_path=tmp_path / "r.json", offline_root=tmp_path / "o", settings_path=tmp_path / "s.json",
                          routes_path=tmp_path / "rt.json", devices_path=tmp_path / "d.json", schedules_path=tmp_path / "sch.json",
                          schedule_clock=clock, schedule_tick=0.03)

    with TestClient(make()) as c:
        pid = c.post("/api/schedules", json={"name": "Persist", "steps": [{"at": "2026-10-31T20:00", "type": "set_location", "params": {"lat": 3, "lon": 4}}]}).json()["id"]
        c.post(f"/api/schedules/{pid}/arm", json={"armed": True})
    with TestClient(make()) as c2:                                                     # the app was closed and reopened
        plan_ = c2.get("/api/schedules").json()["plans"][0]
        assert plan_["armed"] is True
        clock.set("20:00:00")
        wait_for(lambda: steps_of(c2, pid)["20:00"]["status"] == "done")
        assert c2.get("/api/status").json()["simulation"]["lat"] == 3


def test_a_plan_can_target_a_specific_phone(tmp_path):
    clock = Clock("19:59:00")
    a, b = FakeBackend(udid="00008110-AAAA", name="A"), FakeBackend(udid="00008120-BBBB", name="B")
    by = {a.info.udid: a, b.info.udid: b}

    class Discovery:
        async def list_devices(self):
            return (await a.list_devices()) + (await b.list_devices())

        async def scan_wifi(self, timeout=5.0):
            return []

    app = create_app(backend_factory=lambda u: by[u] if u else a, discovery=Discovery(), router=FakeRouter(), house_finder=FakeHouses(),
                     favorites_path=tmp_path / "f.json", recents_path=tmp_path / "r.json", offline_root=tmp_path / "o",
                     settings_path=tmp_path / "s.json", routes_path=tmp_path / "rt.json", devices_path=tmp_path / "d.json",
                     schedules_path=tmp_path / "sch.json", schedule_clock=clock, schedule_tick=0.03)
    with TestClient(app) as c:
        pid = c.post("/api/schedules", json={"name": "Bob only", "device": "00008120-BBBB",
                                              "steps": [{"at": "2026-10-31T20:00", "type": "set_location", "params": {"lat": 9, "lon": 9}}]}).json()["id"]
        c.post(f"/api/schedules/{pid}/arm", json={"armed": True})
        clock.set("20:00:00")
        wait_for(lambda: steps_of(c, pid)["20:00"]["status"] == "done")
    assert [x[1:] for s in b.sessions for x in s.calls] == [(9, 9)] and a.sessions == []


def test_per_trip_realism_overrides_apply_only_to_that_trip(env):
    c = env
    assert c.get("/api/settings").json()["noise"] is False
    c.post("/api/location", json={"lat": 0, "lon": 0})
    c.post("/api/journey", json={"stops": [{"lat": 0, "lon": 0.0004}], "mode": "straight", "speed_kmh": 100, "realism": {"noise": True, "bogus": True}})
    assert c.manager._realism_override == {"noise": True}                  # unknown keys are ignored
    wait_for(lambda: not c.get("/api/status").json()["walk"]["active"])
    pts = [x[1:] for x in c.backend.sessions[0].calls if x[0] == "set"][1:]
    assert any(p[0] != 0 for p in pts)                                     # the trip wobbled although the global switch is off
    assert c.manager._realism_override == {}
    n = len(c.backend.sessions[0].calls)
    time.sleep(0.3)
    assert len(c.backend.sessions[0].calls) == n                           # and standing still afterwards is not noisy
