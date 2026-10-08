"""Starting relocalization from the API (docs/satinav-maps-redesign.md ## 16, packages/api/reloc_job.py).

- `reloc.can_start` / `can_start_reason` on GET /maps/{id}/reloc and placement-suggestions;
- POST .../place {"source": "reloc"} with can_start: 202 and a job (mode "odin": init_pos null; mode
  "assisted": the user's pose as init_pos), the calls in order, none inside a DB transaction;
- the refusals, the legacy check-only placement when can_start is false;
- failure / rollback / timeout / cancel / session closed mid-job;
- the frame is decided in ONE place (map_sessions.reloc_bin_pose / reloc_map_t_session);
- the new OrchestratorClient routes; the HTTP routes (202, GET, DELETE).
"""
import asyncio
import contextlib
import math
import os

for _k in ("ARANGO_PASSWORD", "MINIO_ACCESS_KEY", "MINIO_SECRET_KEY", "POSTGRES_PASSWORD"):
    os.environ.setdefault(_k, "test")

from types import SimpleNamespace  # noqa: E402
from unittest.mock import patch  # noqa: E402

import httpx  # noqa: E402
import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402

from cloud_common.objects.robot import RobotObjectV1, RobotStatusV1  # noqa: E402
from packages.api import maps  # noqa: E402
from packages.api import orchestrator_client as oc  # noqa: E402
from packages.api import reloc_job as rj  # noqa: E402
from packages.api.mapping_switch import MappingSwitch  # noqa: E402
from packages.api.orchestrator_maps import OrchestratorMaps  # noqa: E402
from packages.events.codes import EventCode  # noqa: E402
from packages.utils import map_sessions as ms  # noqa: E402
from tests.unit import test_maps_m1 as m1  # noqa: E402
from tests.unit.test_placement_suggestion import SugDb, _status, _unplaced  # noqa: E402

pytestmark = pytest.mark.unit

RELOC = {"source": "reloc"}
INIT = {"x": 2.0, "y": -1.0, "yaw": 0.5}
ASSISTED = {"source": "reloc", "reloc": {"init_pose": INIT}}
ONBOARD = "cloud-shed"


def _http(status, detail):
    return oc.OrchestratorError(oc.HTTP, detail, status=status)


# --- fakes ---------------------------------------------------------------------------------------

class TxDb(SugDb):
    """SugDb that counts the open transactions (an orchestrator call inside one fails)."""
    open_tx = 0

    def store(self, _db, publisher_id):
        inner = super().store(_db, publisher_id)

        @contextlib.asynccontextmanager
        async def cm():
            self.open_tx += 1
            try:
                async with inner as store:
                    yield store
            finally:
                self.open_tx -= 1
        return cm()


class RelocOrch:
    """One robot's orchestrator as the reloc job uses it; every call is recorded."""

    def __init__(self, services=("odin_reloc",), running=(), held=True, init_pos=None,
                 current_map="old-map", fail=None):
        self.services = {n: n in running for n in services}
        self.rows = ([{"name": ONBOARD, "valid": True, "meta": {"cloud_map_id": "shed"}}]
                     if held else [])
        self.init_pos = init_pos
        self.current_map = current_map
        self.fail = fail or {}      # op -> OrchestratorError
        self.calls = []             # (op, arg)
        self.db = None

    def enter(self, op, arg=None):
        self.calls.append((op, arg))
        if self.db is not None:
            assert self.db.open_tx == 0, f"orchestrator {op} inside a DB transaction"
        if op in self.fail:
            raise self.fail[op]

    def ops(self):
        return [c[0] for c in self.calls]


class RelocClient:
    def __init__(self, orch):
        self.o = orch

    async def list_services(self):
        self.o.enter("list_services")
        return [{"name": n, "running": r} for n, r in self.o.services.items()]

    async def list_maps(self, cloud_map_id):
        self.o.enter("list_maps", cloud_map_id)
        return self.o.rows

    async def get_map(self, name):
        self.o.enter("get_map", name)
        if not self.o.rows:     # no stored map at all: the robot does not know it
            raise _http(404, f"map '{name}' not found")
        return {"name": name, "init_pos": self.o.init_pos}

    async def mapping_state(self):    # an older orchestrator: no such route
        raise _http(404, "no such route")

    async def relocalize(self, name):
        self.o.enter("relocalize", name)
        raise _http(404, "no such route")

    async def patch_map(self, name, body):
        self.o.enter("patch_map", (name, body))
        self.o.init_pos = body["init_pos"]
        return {}

    async def get_config_map(self):
        self.o.enter("get_config_map")
        return self.o.current_map

    async def set_config_map(self, name):
        self.o.enter("set_config_map", name)
        self.o.current_map = name
        return name

    async def stop(self, name):
        self.o.enter("stop", name)
        if not self.o.services.get(name):
            raise _http(404, "not currently running")
        self.o.services[name] = False
        return {"success": True}

    async def start(self, name):
        self.o.enter("start", name)
        if self.o.services.get(name):
            raise _http(409, "already running")
        self.o.services[name] = True
        return {"success": True}


async def _get_map_any(self, name):
    self.o.enter("get_map", name)
    return {"name": name, "init_pos": self.o.init_pos}


class _NoMappingOrch:
    """The mapping switch's view of the robot (notify_robot reads it): unreachable, at once."""

    async def list_services(self):
        raise oc.OrchestratorError(oc.UNREACHABLE, "not reachable")


def _switch(cls=MappingSwitch):
    return cls(client_factory=lambda robot: _NoMappingOrch())


class CapHolder:
    """held() and reloc_capability() with fixed answers (the job does not use it)."""

    def __init__(self, held=True, can=True, reason=None):
        self.held_answer, self.can, self.reason = held, can, reason
        self.calls = []

    async def held(self, robot, cloud_map_id, fresh=False):
        self.calls.append(("held", robot.name, fresh))
        return self.held_answer

    async def reloc_capability(self, robot, cloud_map_id, fresh=False, held=None):
        self.calls.append(("cap", robot.name, fresh))
        return self.can, (None if self.can else (self.reason or "no"))


class Env:
    def __init__(self, db, orch, holder, jobs, switch, clock):
        self.db, self.orch, self.holder, self.jobs, self.switch, self.clock = (
            db, orch, holder, jobs, switch, clock)
        self.on_sleep = None
        self.sleeps = 0
        self.block = None      # an asyncio.Event the poll sleep waits on (never set: it hangs)

    async def place(self, sid, body=RELOC, **kw):
        kw.setdefault("reloc_jobs", self.jobs)
        return await maps.place_session(None, "shed", str(sid), body, m1.PUB, "ann",
                                        switch=self.switch, holder=self.holder, **kw)

    async def run(self, sid, body=RELOC):
        out = await self.place(sid, body)
        await self.jobs.wait_all()
        return out, self.jobs.latest("shed", str(sid))

    def robot(self):
        return self.db.robots["r1"]


@pytest.fixture
def env():
    d = TxDb()
    orch = RelocOrch()
    orch.db = d
    now = [0.0]

    async def sleep(dt):
        now[0] += dt
        e.sleeps += 1
        if e.on_sleep is not None:
            e.on_sleep(e)
        await (e.block.wait() if e.block is not None else asyncio.sleep(0))

    jobs = rj.RelocJobs(client_factory=lambda robot: RelocClient(orch), clock=lambda: now[0],
                        sleep=sleep, timeout=90.0, poll=1.0, settle=5.0, confirm_timeout=0.0,
                        candidates=["odin_reloc"])   # 0: the job confirms by itself at once
    e = Env(d, orch, CapHolder(), jobs, _switch(), now)
    with patch.object(maps, "open_store", d.store), patch.object(maps, "_utcnow", m1.Clock()):
        yield e


def _robot(db, name="r1", online=True, **status):
    db.robots[name] = RobotObjectV1(
        name=name, ip_address="10.0.0.5", entrypoint_port=8080,
        status=RobotStatusV1(online=online, state="IDLE",
                             pose={"x": 3.0, "y": 4.0, "theta": 0.2}, **status))
    return db.robots[name]


def _localized_after(n, score=0.9):
    """on_sleep hook: the robot reports itself initialized after `n` polls."""
    def hook(e):
        if e.sleeps >= n:
            st = e.robot().status
            st.position_initialized, st.localization_score = True, score
    return hook


# --- 1. the capability flag ------------------------------------------------------------------------

class _CapOrch:
    def __init__(self, services=("odin_reloc",), held=True, fail=None):
        self.services, self.held, self.fail = services, held, fail

    def client(self, robot):
        o = self

        class C:
            async def list_services(self):
                if o.fail:
                    raise o.fail
                return [{"name": n} for n in o.services]

            async def list_maps(self, cloud_map_id):
                return [{"name": "n", "valid": True, "meta": {"cloud_map_id": cloud_map_id}}] \
                    if o.held else []
        return C()


def _real_holder(**kw):
    return OrchestratorMaps(client_factory=_CapOrch(**kw).client)


def _plain_robot(online=True, address=True):
    extra = {"ip_address": "10.0.0.5", "entrypoint_port": 8080} if address else {}
    return RobotObjectV1(name="r1", status=RobotStatusV1(online=online), **extra)


class TestCapability:
    async def test_can_start(self):
        assert await _real_holder().reloc_capability(_plain_robot(), "shed") == (True, None)

    @pytest.mark.parametrize("robot,kw,words", [
        (dict(online=False), {}, "offline"),
        (dict(address=False), {}, "no registered orchestrator"),
        (dict(), dict(services=("topomap", "sim_topomap")), "no relocalization service"),
        (dict(), dict(held=False), "does not hold"),
        (dict(), dict(fail=oc.OrchestratorError(oc.UNREACHABLE, "no route")), "no route"),
    ])
    async def test_can_always_start_what_is_wrong_is_only_a_warning(self, robot, kw, words):
        can, why = await _real_holder(**kw).reloc_capability(_plain_robot(**robot), "shed")
        assert can is True and words in why

    async def test_unknown_robot_cannot_start(self):
        assert await _real_holder().reloc_capability(None, "shed") == (
            False, "the robot is unknown")

    async def test_unknown_held_is_a_warning(self):
        class Holder(OrchestratorMaps):
            async def held(self, robot, cloud_map_id, fresh=False):
                return None
        h = Holder(client_factory=_CapOrch().client)
        can, why = await h.reloc_capability(_plain_robot(), "shed")
        assert can is True and "could not be asked" in why

    async def test_a_held_answer_the_caller_has_is_not_asked_again(self):
        h = _real_holder()
        assert await h.reloc_capability(_plain_robot(), "shed", held=False) == (
            True, "the robot does not hold a stored map for 'shed'")

    async def test_the_services_read_is_cached_and_invalidated(self):
        calls = []

        class C:
            async def list_services(self):
                calls.append(1)
                return [{"name": "odin_reloc"}]

            async def list_maps(self, cloud_map_id):
                return [{"name": "n", "valid": True, "meta": {"cloud_map_id": cloud_map_id}}]
        h = OrchestratorMaps(client_factory=lambda r: C())
        r = _plain_robot()
        await h.reloc_capability(r, "shed")
        await h.reloc_capability(r, "shed")
        assert len(calls) == 1
        h.invalidate("r1")
        await h.reloc_capability(r, "shed")
        assert len(calls) == 2

    async def test_on_the_reads(self, env):
        _robot(env.db)
        s = _unplaced(env.db)
        env.db.add_map("shed", type="local", status={"state": "ready"})
        holder = _real_holder()
        out = await maps.placement_suggestions(None, "shed", str(s["session_id"]), holder=holder)
        assert out["reloc"] == {"available": True, "known": True, "source": "orchestrator",
                                "can_start": True, "can_start_reason": None, "warning": None,
                                "localization_api": False}
        out = await maps.map_reloc(None, holder, "shed", "r1")
        assert out["can_start"] is True and out["can_start_reason"] is None
        out = await maps.map_reloc(None, _real_holder(services=("topomap",)), "shed", "r1")
        assert out["can_start"] is True and "relocalization service" in out["can_start_reason"]
        assert out["warning"] == out["can_start_reason"]
        assert out["available"] is True    # the held map did not change

    async def test_localization_api_flag(self, env):
        _robot(env.db)
        env.db.add_map("shed", type="local", status={"state": "ready"})
        env.db.add_map("geo1", type="geo", status={"state": "ready"})

        def holder(facade):
            class C:
                async def facade(self):
                    return facade

                async def list_services(self):
                    return [{"name": "odin_reloc"}]

                async def list_maps(self, cloud_map_id):
                    return []
            return OrchestratorMaps(client_factory=lambda robot: C())

        assert (await maps.map_reloc(None, holder(True), "shed", "r1"))["localization_api"] is True
        assert (await maps.map_reloc(None, holder(False), "shed", "r1"))["localization_api"] is False
        assert (await maps.map_reloc(None, holder(True), "geo1", "r1"))["localization_api"] is False
        assert (await maps.map_reloc(None, holder(True), "shed", "ghost"))["localization_api"] is False

    async def test_unknown_robot_cannot_start(self, env):
        env.db.add_map("shed", type="local", status={"state": "ready"})
        out = await maps.map_reloc(None, _real_holder(), "shed", "ghost")
        assert out["can_start"] is False and out["can_start_reason"]

    def test_candidates_come_from_config(self):
        from packages import config
        assert config.RELOC_SERVICE_CANDIDATES == ["odin_reloc"]
        assert config.RELOC_JOB_TIMEOUT_S == 90.0


# --- 2. mode 2: Odin alone -----------------------------------------------------------------------------

class TestConvertBlockedWhileRelocalizing:
    async def test_convert_refused_while_a_job_runs_then_allowed(self, env):
        _robot(env.db)
        s = _unplaced(env.db)
        env.block = asyncio.Event()           # the poll hangs: the job stays active
        await env.place(s["session_id"])
        await asyncio.sleep(0)
        assert env.jobs.active_for_map("shed") is not None
        assert env.jobs.active_for_map("other") is None
        body = {"type": "geo", "latitude": 47.0, "longitude": 19.0}
        with pytest.raises(HTTPException) as err:
            await maps.convert_map_type(None, "shed", body, m1.PUB, reloc_jobs=env.jobs)
        assert err.value.status_code == 409
        assert "A relocalization is running on map" in err.value.detail
        assert env.db.maps["shed"]["spec"]["type"] == "local"
        await env.jobs.cancel(env.jobs.active_for_map("shed"))
        assert env.jobs.active_for_map("shed") is None


class TestModeOdin:
    async def test_happy_path(self, env):
        _robot(env.db, position_initialized=False)
        s = _unplaced(env.db)
        env.orch.init_pos = [9.0, 9.0, 0.0, 0.0, 0.0, 0.0, 1.0]      # a stale seed
        env.on_sleep = _localized_after(3)
        out = await env.place(s["session_id"])
        job = out["reloc_job"]
        assert (job["state"], job["mode"]) == ("preparing", "odin")
        assert job["id"] and job["started_at"] and job["deadline"] and job["step"]
        assert out["session"]["aligned"] is False and env.db.events == []
        await env.jobs.wait_all()
        # the calls, in order; the stale init_pos is cleared (null), the map is selected, the
        # service is started (it was not running)
        assert env.orch.calls == [
            ("list_maps", "shed"), ("get_map", ONBOARD),
            ("patch_map", (ONBOARD, {"init_pos": None})), ("get_config_map", None),
            ("set_config_map", ONBOARD), ("list_services", None), ("start", "odin_reloc")]
        v = env.jobs.latest("shed", str(s["session_id"])).view()
        assert (v["state"], v["step"], v["position_initialized"], v["localization_score"]) == (
            "placed", "done", True, 0.9)
        assert v["auto_confirmed"] is True and v["proposal"]["map_T_session"] == (
            ms.reloc_map_t_session())
        assert env.sleeps == 3
        # placed with the identity, at the robot's pose read when it was initialized
        row = env.db.sessions[-1]
        assert row["aligned"] is True and row["map_t_session"] == ms.reloc_map_t_session()
        assert row["placement"]["source"] == "reloc" and "init_pose" not in row["placement"]
        assert row["placement"]["robot_pose"] == {"x": 3.0, "y": 4.0, "theta": 0.2}
        assert env.db.codes() == [EventCode.MAP_SESSION_PLACED.value]
        assert env.orch.current_map == ONBOARD

    async def test_a_running_service_is_stopped_then_started(self, env):
        _robot(env.db)
        env.orch.services["odin_reloc"] = True
        s = _unplaced(env.db)
        env.on_sleep = _localized_after(1)
        _, job = await env.run(s["session_id"])
        assert job.state == rj.PLACED
        assert env.orch.calls[-2:] == [("stop", "odin_reloc"), ("start", "odin_reloc")]

    async def test_no_orchestrator_call_inside_a_db_transaction(self, env):
        # the fake asserts it on every call; make sure the job really made calls and reads
        _robot(env.db)
        s = _unplaced(env.db)
        env.on_sleep = _localized_after(2)
        _, job = await env.run(s["session_id"])
        assert job.state == rj.PLACED and len(env.orch.calls) == 7 and env.db.open_tx == 0

    async def test_a_stale_initialized_flag_is_not_believed_at_once(self, env):
        _robot(env.db, position_initialized=True, localization_score=0.8)   # before the restart
        s = _unplaced(env.db)
        _, job = await env.run(s["session_id"])
        # no drop was seen: only the settle time (5 s, 1 s polls) lets the flag count
        assert job.state == rj.PLACED and env.sleeps == 5

    async def test_a_dropped_flag_then_true_counts_at_once(self, env):
        _robot(env.db, position_initialized=True)       # stale: from before the restart
        flips = iter([False, True])                      # the restart drops it, then it is true

        def hook(e):
            e.robot().status.position_initialized = next(flips, True)
        env.on_sleep = hook
        s = _unplaced(env.db)
        _, job = await env.run(s["session_id"])
        assert job.state == rj.PLACED and env.sleeps == 2


# --- 3. mode 3: assisted by the user's pose ---------------------------------------------------------------

class TestModeAssisted:
    async def test_happy_path_with_the_exact_init_pos(self, env):
        _robot(env.db)
        s = _unplaced(env.db)
        env.on_sleep = _localized_after(1)
        out, job = await env.run(s["session_id"], ASSISTED)
        assert out["reloc_job"]["mode"] == "assisted"
        patch_call = next(c for c in env.orch.calls if c[0] == "patch_map")
        yaw = 0.5
        assert patch_call[1] == (ONBOARD, {"init_pos": [
            2.0, -1.0, 0.0, 0.0, 0.0, pytest.approx(math.sin(yaw / 2)),
            pytest.approx(math.cos(yaw / 2))]})
        body = patch_call[1][1]["init_pos"]
        assert len(body) == 7 and math.isclose(math.hypot(body[5], body[6]), 1.0)
        assert job.state == rj.PLACED
        placement = env.db.sessions[-1]["placement"]
        assert placement["init_pose"] == INIT and placement["source"] == "reloc"

    async def test_zero_yaw_is_the_identity_quaternion(self, env):
        _robot(env.db)
        s = _unplaced(env.db)
        env.on_sleep = _localized_after(1)
        await env.run(s["session_id"], {"source": "reloc",
                                        "reloc": {"init_pose": {"x": 1.0, "y": 2.0, "yaw": 0.0}}})
        patch_call = next(c for c in env.orch.calls if c[0] == "patch_map")
        assert patch_call[1][1] == {"init_pos": [1.0, 2.0, 0.0, 0.0, 0.0, 0.0, 1.0]}

    async def test_the_init_pose_goes_through_the_frame_function(self, env, monkeypatch):
        monkeypatch.setattr(ms, "reloc_bin_pose", lambda p: {"x": 100.0, "y": 0.0, "yaw": 0.0})
        _robot(env.db)
        s = _unplaced(env.db)
        env.on_sleep = _localized_after(1)
        await env.run(s["session_id"], ASSISTED)
        patch_call = next(c for c in env.orch.calls if c[0] == "patch_map")
        assert patch_call[1][1]["init_pos"][:2] == [100.0, 0.0]

    def test_request_validation(self):
        assert maps.PlaceRequest(**ASSISTED).reloc.init_pose.yaw == 0.5
        for bad in ({"source": "reloc", "reloc": {"init_pose": {"x": 1.0}}},
                    {"source": "reloc", "reloc": {"init_pose": {"x": 1, "y": 1, "yaw": "nan"}}},
                    {"source": "reloc", "reloc": {"other": 1}},
                    {"source": "datum", "reloc": {"init_pose": INIT}},
                    {"pose": INIT, "robot_pose": {"x": 0, "y": 0, "theta": 0},
                     "reloc": {"init_pose": INIT}}):
            with pytest.raises(Exception):
                maps.PlaceRequest(**bad)
        assert maps.PlaceRequest(source="reloc", reloc={}).reloc.init_pose is None


# --- 4. the frame is decided in one place --------------------------------------------------------------------

class TestFrame:
    def test_identity_today(self):
        assert ms.reloc_bin_pose(INIT) == pytest.approx(INIT)
        assert ms.reloc_map_t_session() == {"tx": 0.0, "ty": 0.0, "yaw": 0.0}

    def test_one_definition_drives_both_directions(self, monkeypatch):
        t = {"tx": 10.0, "ty": -2.0, "yaw": 0.5}
        monkeypatch.setattr(ms, "reloc_map_t_session", lambda: dict(t))
        pose = ms.reloc_bin_pose({"x": 11.0, "y": -1.0, "yaw": 0.7})
        # carried back through the transform it is the map pose again
        from packages.utils import map_geo
        x, y, yaw = map_geo.apply_pose(t, pose["x"], pose["y"], pose["yaw"])
        assert (x, y) == pytest.approx((11.0, -1.0))
        assert ms.yaw_difference(yaw, 0.7) < 1e-9

    def test_init_pos_vector(self):
        assert ms.init_pos_vector({"x": 1.0, "y": 2.0, "yaw": math.pi}) == pytest.approx(
            [1.0, 2.0, 0.0, 0.0, 0.0, 1.0, 0.0], abs=1e-12)

    def test_no_other_function_builds_the_frame(self):
        import inspect
        source = inspect.getsource(rj)
        assert "reloc_bin_pose" in source and "reloc_placement" in source
        assert "map_t_session = " not in source and "invert_transform" not in source


# --- 5. refusals ---------------------------------------------------------------------------------------------

class TestRefusals:
    async def _refused(self, env, sid, body=RELOC):
        assert await _status(env.place(sid, body)) == 409
        assert env.orch.calls == [] and env.jobs.latest("shed", str(sid)) is None

    async def test_offline(self, env):
        _robot(env.db, online=False)
        s = _unplaced(env.db)
        await self._refused(env, s["session_id"])

    async def test_driving_no_longer_refuses(self, env):
        _robot(env.db)
        env.db.state_msgs["r1"] = {"driving": True}
        s = _unplaced(env.db)
        env.on_sleep = _localized_after(1)
        out, job = await env.run(s["session_id"])
        assert out["reloc_job"]["state"] == "preparing" and job.state == rj.PLACED

    async def test_mapping_session_open_still_refuses(self, env):
        # user decision 2026-10-08: no reloc while the robot's open session is a mapping one
        _robot(env.db)
        s = _unplaced(env.db, purpose="mapping")
        await self._refused(env, s["session_id"])

    async def test_another_job_running_for_the_robot(self, env):
        _robot(env.db)
        s = _unplaced(env.db)
        gate = asyncio.Event()
        orig = RelocClient.list_maps

        async def slow(self, cloud_map_id):
            await gate.wait()
            return await orig(self, cloud_map_id)
        with patch.object(RelocClient, "list_maps", slow):
            await env.place(s["session_id"])
            assert env.jobs.active_for("r1") is not None
            assert await _status(env.place(s["session_id"])) == 409
            gate.set()
            env.on_sleep = _localized_after(1)
            await env.jobs.wait_all()
        assert env.jobs.active_for("r1") is None

    async def test_pending_slam_save(self, env):
        _robot(env.db)
        s = _unplaced(env.db)
        gate = asyncio.Event()

        class Switch(MappingSwitch):
            def slam_save_pending(self, robot_name):
                return True
        env.switch = _switch(Switch)
        await self._refused(env, s["session_id"])
        gate.set()

    async def test_not_held_means_no_job_and_the_old_check(self, env):
        _robot(env.db)
        env.holder = CapHolder(held=False, can=False, reason="the robot does not hold it")
        s = _unplaced(env.db)
        assert await _status(env.place(s["session_id"])) == 409      # held is not True
        assert env.orch.calls == []

    async def test_init_pose_without_can_start_is_409_and_calls_nothing(self, env):
        _robot(env.db)
        env.holder = CapHolder(can=False, reason="no reloc service")
        s = _unplaced(env.db)
        with pytest.raises(HTTPException) as err:
            await env.place(s["session_id"], ASSISTED)
        assert err.value.status_code == 409 and "no reloc service" in err.value.detail
        assert env.orch.calls == [] and env.db.events == []

    async def test_init_pose_without_a_jobs_registry_is_409(self, env):
        _robot(env.db)
        s = _unplaced(env.db)
        assert await _status(env.place(s["session_id"], ASSISTED, reloc_jobs=None)) == 409

    async def test_already_placed_and_geo_and_finished_are_refused_before_anything(self, env):
        _robot(env.db)
        s = _unplaced(env.db, aligned=True)
        await self._refused(env, s["session_id"])

    async def test_position_not_initialized_does_not_refuse_a_job(self, env):
        _robot(env.db, position_initialized=False)
        s = _unplaced(env.db)
        env.on_sleep = _localized_after(1)
        _, job = await env.run(s["session_id"])
        assert job.state == rj.PLACED


class TestLegacyCheckOnly:
    """can_start false and no init pose: today's behaviour, no robot calls."""

    async def test_identity_placement_200(self, env):
        _robot(env.db, position_initialized=True)
        env.holder = CapHolder(held=True, can=False, reason="no reloc service")
        s = _unplaced(env.db)
        out = await env.place(s["session_id"])
        assert "reloc_job" not in out and out["changed"] is True
        sess = out["session"]
        assert sess["aligned"] is True and sess["placement"]["source"] == "reloc"
        assert sess["map_T_session"] == ms.reloc_map_t_session()
        assert env.orch.calls == [] and env.jobs.latest("shed", str(s["session_id"])) is None

    async def test_not_initialized_still_refuses(self, env):
        _robot(env.db, position_initialized=False)
        env.holder = CapHolder(held=True, can=False, reason="no reloc service")
        s = _unplaced(env.db)
        assert await _status(env.place(s["session_id"])) == 409
        assert env.db.sessions[-1]["aligned"] is False

    async def test_holder_without_capability_is_unchanged(self, env):
        _robot(env.db)

        class OldHolder:
            async def held(self, robot, cloud_map_id, fresh=False):
                return True
        env.holder = OldHolder()
        s = _unplaced(env.db)
        out = await env.place(s["session_id"])
        assert out["changed"] is True and "reloc_job" not in out


# --- 6. failure, rollback, timeout, cancel ---------------------------------------------------------------------

class TestFailures:
    async def _failed(self, env, body=RELOC, **orch):
        _robot(env.db)
        s = _unplaced(env.db)
        job = (await env.run(s["session_id"], body))[1]
        assert job.state == rj.FAILED
        assert env.db.sessions[-1]["aligned"] is False and env.db.events == []
        return job

    async def test_patch_fails_restores_nothing_else(self, env):
        env.orch.init_pos = [1.0, 2.0, 0.0, 0.0, 0.0, 0.0, 1.0]
        env.orch.fail["patch_map"] = _http(409, "meta.yaml is invalid")
        job = await self._failed(env, ASSISTED)
        assert "could not set the initial pose" in job.error and "409" in job.error
        assert "set_config_map" not in env.orch.ops() and "start" not in env.orch.ops()
        assert env.orch.init_pos == [1.0, 2.0, 0.0, 0.0, 0.0, 0.0, 1.0]

    async def test_put_fails_restores_the_init_pos(self, env):
        prev = [1.0, 2.0, 0.0, 0.0, 0.0, 0.0, 1.0]
        env.orch.init_pos = prev

        async def boom(self, name):
            self.o.enter("set_config_map", name)
            raise _http(404, "map file missing")
        with patch.object(RelocClient, "set_config_map", boom):
            job = await self._failed(env, ASSISTED)
        assert "could not select map" in job.error
        assert env.orch.init_pos == prev                       # restored
        assert env.orch.current_map == "old-map" and "start" not in env.orch.ops()

    async def test_start_409_usb_busy_restores_both(self, env):
        prev = [1.0, 2.0, 0.0, 0.0, 0.0, 0.0, 1.0]
        env.orch.init_pos = prev
        env.orch.fail["start"] = _http(409, "odin_usb group busy: odin_driver_gpu is running")
        job = await self._failed(env, ASSISTED)
        assert "odin_usb group busy" in job.error and "USB" in job.error
        assert env.orch.init_pos == prev and env.orch.current_map == "old-map"
        calls = env.orch.calls
        assert calls[-2][0] == "set_config_map" and calls[-2][1] == "old-map"
        assert calls[-1] == ("patch_map", (ONBOARD, {"init_pos": prev}))

    @pytest.mark.parametrize("error,words", [
        (_http(502, "driver died"), "502"),
        (_http(504, "slow"), "504"),
        (oc.OrchestratorError(oc.UNREACHABLE, "orchestrator at 10.0.0.5:8080 is not reachable"),
         "not reachable"),
    ])
    async def test_orchestrator_statuses_become_readable_text(self, env, error, words):
        env.orch.fail["start"] = error
        job = await self._failed(env)
        assert "could not start service 'odin_reloc'" in job.error and words in job.error

    async def test_no_reloc_service_on_the_robot_tries_the_endpoint_and_reports_its_answer(
            self, env):
        env.orch.services = {"topomap": False}
        job = await self._failed(env)
        # not a gate: the endpoint was asked; its 404 plus the missing service are the error
        assert "could not start relocalization" in job.error and "404" in job.error
        assert "no relocalization service" in job.error and "odin_reloc" in job.error
        assert ("relocalize", ONBOARD) in env.orch.calls
        assert env.orch.current_map == "old-map" and env.orch.init_pos is None

    async def test_no_tagged_map_is_not_a_gate_the_robot_answers(self, env):
        env.orch.rows = []     # no stored map tagged for the cloud map
        job = await self._failed(env)
        assert "could not read stored map 'cloud-shed'" in job.error and "404" in job.error
        assert "patch_map" not in env.orch.ops()
        assert any("does not list a stored map" in w for w in job.view()["warnings"])

    async def test_no_tagged_map_but_the_robot_has_it_still_relocalizes(self, env):
        _robot(env.db)
        env.orch.rows = []     # untagged, yet the default name exists on the robot
        s = _unplaced(env.db)
        env.on_sleep = _localized_after(1)
        with patch.object(RelocClient, "get_map", _get_map_any):
            _, job = await env.run(s["session_id"])
        assert job.state == rj.PLACED and ("get_map", ONBOARD) in env.orch.calls

    async def test_rollback_that_fails_is_reported(self, env):
        env.orch.fail["start"] = _http(409, "busy")
        orig = RelocClient.set_config_map
        seen = []

        async def flaky(self, name):
            seen.append(name)
            if name == "old-map":
                raise _http(404, "gone")
            return await orig(self, name)
        with patch.object(RelocClient, "set_config_map", flaky):
            job = await self._failed(env)
        assert "current map not restored" in job.error

    async def test_timeout_leaves_the_driver_running_and_restores_nothing(self, env):
        _robot(env.db, position_initialized=False)
        env.orch.init_pos = [1.0, 2.0, 0.0, 0.0, 0.0, 0.0, 1.0]
        s = _unplaced(env.db)
        _, job = await env.run(s["session_id"], ASSISTED)
        assert job.state == rj.FAILED and "did not report a localized position" in job.error
        assert "left running" in job.error
        assert env.orch.services["odin_reloc"] is True
        assert env.orch.ops().count("patch_map") == 1 and env.orch.ops().count(
            "set_config_map") == 1 and "stop" not in env.orch.ops()
        assert env.orch.current_map == ONBOARD
        assert env.orch.init_pos is not None and env.orch.init_pos[0] == 2.0
        assert env.db.sessions[-1]["aligned"] is False
        assert job.view()["position_initialized"] is False

    async def test_robot_offline_mid_job_still_rolls_back(self, env):
        _robot(env.db)
        prev = [1.0, 2.0, 0.0, 0.0, 0.0, 0.0, 1.0]
        env.orch.init_pos = prev

        def hook(e):
            e.robot().status.online = False
        env.on_sleep = hook
        s = _unplaced(env.db)
        _, job = await env.run(s["session_id"], ASSISTED)
        assert job.state == rj.FAILED and "offline" in job.error
        assert env.orch.current_map == "old-map" and env.orch.init_pos == prev

    async def test_session_finished_mid_job_fails_with_no_placement(self, env):
        _robot(env.db)
        s = _unplaced(env.db)

        def hook(e):
            for row in e.db.sessions:
                row["ended_at"] = m1.T0
        env.on_sleep = hook
        _, job = await env.run(s["session_id"])
        assert job.state == rj.FAILED and "finished" in job.error
        assert env.db.sessions[-1]["aligned"] is False and env.db.events == []

    async def test_session_placed_by_hand_mid_job_fails(self, env):
        _robot(env.db)
        s = _unplaced(env.db)

        def hook(e):
            e.db.sessions[-1]["aligned"] = True
        env.on_sleep = hook
        _, job = await env.run(s["session_id"])
        assert job.state == rj.FAILED and "placed meanwhile" in job.error
        assert env.db.events == []

    async def test_map_converted_to_geo_mid_job_fails_with_no_placement(self, env):
        _robot(env.db)
        s = _unplaced(env.db)

        def hook(e):
            e.db.maps["shed"]["spec"]["type"] = "geo"
        env.on_sleep = hook
        _, job = await env.run(s["session_id"])
        assert job.state == rj.FAILED and "no longer a local map" in job.error
        assert env.db.sessions[-1]["aligned"] is False and env.db.events == []

    async def test_map_converted_just_before_the_placement_fails(self, env):
        _robot(env.db)
        s = _unplaced(env.db)
        rj.RelocJobs._check_session(
            SimpleNamespace(map_name="shed"), dict(s, ended_at=None, aligned=False),
            SimpleNamespace(type="local"))
        with pytest.raises(rj._Fail, match="no longer a local map"):
            rj.RelocJobs._check_session(
                SimpleNamespace(map_name="shed"), dict(s, ended_at=None, aligned=False),
                SimpleNamespace(type="geo"))

    async def test_session_closed_between_the_last_poll_and_the_placement(self, env):
        _robot(env.db)
        s = _unplaced(env.db)
        env.on_sleep = _localized_after(1)
        orig = rj.RelocJobs._place

        async def closing(self, job, db, switch):
            for row in env.db.sessions:
                row["ended_at"] = m1.T0
            return await orig(self, job, db, switch)
        with patch.object(rj.RelocJobs, "_place", closing):
            _, job = await env.run(s["session_id"])
        assert job.state == rj.FAILED and env.db.events == []

    async def test_a_mapping_session_opened_while_waiting_for_the_lock(self, env):
        _robot(env.db)
        s = _unplaced(env.db)
        await env.switch.lock("r1").acquire()
        out = await env.place(s["session_id"])
        env.db.add_session("shed", "r2", "live", ended=False, purpose="mapping")
        env.db.sessions[-1]["robot_name"] = "r1"
        env.switch.lock("r1").release()
        await env.jobs.wait_all()
        job = env.jobs.latest("shed", str(s["session_id"]))
        assert job.state == rj.FAILED and "mapping session" in job.error
        assert env.orch.calls == [] and out["reloc_job"]["state"] == "preparing"


class TestLifecycleFixes:
    """Review fixes: rollback on every failure but a timeout, the deadline from WAITING, the
    SLAM lock, the cancel/commit race, restarting a service the job stopped, mapping vs jobs."""

    async def test_session_finished_mid_job_rolls_back(self, env):
        _robot(env.db)
        prev = [1.0, 2.0, 0.0, 0.0, 0.0, 0.0, 1.0]
        env.orch.init_pos = prev
        s = _unplaced(env.db)
        env.on_sleep = lambda e: [r.update(ended_at=m1.T0) for r in e.db.sessions]
        _, job = await env.run(s["session_id"], ASSISTED)
        assert job.state == rj.FAILED and "finished" in job.error
        assert env.orch.current_map == "old-map" and env.orch.init_pos == prev

    async def test_failed_placement_rolls_back(self, env):
        _robot(env.db)
        s = _unplaced(env.db)
        env.on_sleep = _localized_after(1)
        orig = rj.RelocJobs._check_session
        calls = []

        def check(job, session):       # fine while waiting, refused in the placement
            calls.append(1)
            if len(calls) >= 3:
                raise rj._Fail("the session was finished meanwhile")
            return orig(job, session)
        with patch.object(rj.RelocJobs, "_check_session", staticmethod(check)):
            _, job = await env.run(s["session_id"])
        assert job.state == rj.FAILED and env.db.events == []
        assert env.orch.current_map == "old-map"

    async def test_a_mapping_session_opened_while_waiting_fails_the_job(self, env):
        _robot(env.db)
        s = _unplaced(env.db)

        def hook(e):
            e.db.add_session("shed", "r2", "live", ended=False, purpose="mapping")
            e.db.sessions[-1]["robot_name"] = "r1"
        env.on_sleep = hook
        _, job = await env.run(s["session_id"])
        assert job.state == rj.FAILED and "mapping session" in job.error
        assert env.orch.current_map == "old-map" and env.db.events == []

    async def test_the_deadline_starts_when_waiting_starts(self, env):
        _robot(env.db)
        s = _unplaced(env.db)
        orig = RelocClient.start

        async def slow_start(self, name):      # 200 s of robot-side work > the 90 s timeout
            env.clock[0] += 200
            return await orig(self, name)
        env.on_sleep = _localized_after(3)
        with patch.object(RelocClient, "start", slow_start):
            out, job = await env.run(s["session_id"])
        assert job.state == rj.PLACED
        assert job.deadline_mono == 290.0
        assert job.deadline != out["reloc_job"]["deadline"]     # the estimate was replaced

    async def test_a_pending_slam_save_fails_fast_without_holding_the_robot_lock(self, env):
        _robot(env.db)
        s = _unplaced(env.db)
        await env.switch.slam_lock("r1").acquire()
        try:
            await env.place(s["session_id"])
            await asyncio.wait_for(env.jobs.wait_all(), 2)
            job = env.jobs.latest("shed", str(s["session_id"]))
            assert job.state == rj.FAILED and "SLAM save pending" in job.error
            assert not env.switch.lock("r1").locked() and env.orch.calls == []
        finally:
            env.switch.slam_lock("r1").release()

    async def test_cancel_while_the_placement_commits_ends_placed(self, env):
        _robot(env.db)
        s = _unplaced(env.db)
        env.on_sleep = _localized_after(1)
        entered, gate = asyncio.Event(), asyncio.Event()
        real = env.db.store

        def store(_db, publisher_id):
            @contextlib.asynccontextmanager
            async def cm():
                async with real(_db, publisher_id) as st:
                    yield st
                if publisher_id == m1.PUB:      # the placement: the commit is "in flight"
                    entered.set()
                    await gate.wait()
            return cm()
        with patch.object(maps, "open_store", store):
            await env.place(s["session_id"])
            job = env.jobs.latest("shed", str(s["session_id"]))
            await asyncio.wait_for(entered.wait(), 2)
            cancelling = asyncio.ensure_future(env.jobs.cancel(job))
            await asyncio.sleep(0)
            gate.set()
            done = await asyncio.wait_for(cancelling, 2)
        assert done.state == rj.PLACED and env.db.sessions[-1]["aligned"] is True
        assert env.orch.current_map == ONBOARD      # nothing was rolled back

    async def test_rollback_restarts_a_service_the_job_stopped(self, env):
        _robot(env.db)
        env.orch.services["odin_reloc"] = True
        s = _unplaced(env.db)
        env.on_sleep = lambda e: setattr(e.robot().status, "online", False)
        _, job = await env.run(s["session_id"])
        assert job.state == rj.FAILED
        ops = env.orch.ops()
        assert ops.count("stop") == 2 and ops.count("start") == 2   # job's, then the rollback's
        assert ops[-2:] == ["stop", "start"] and env.orch.services["odin_reloc"] is True

    async def test_a_service_that_was_not_running_is_not_restarted(self, env):
        _robot(env.db)
        s = _unplaced(env.db)
        env.on_sleep = lambda e: setattr(e.robot().status, "online", False)
        await env.run(s["session_id"])
        assert env.orch.ops().count("start") == 1 and "stop" not in env.orch.ops()

    async def test_a_restart_that_fails_is_reported(self, env):
        _robot(env.db)
        env.orch.services["odin_reloc"] = True
        s = _unplaced(env.db)
        env.on_sleep = lambda e: setattr(e.robot().status, "online", False)
        orig = RelocClient.start
        n = []

        async def flaky(self, name):
            n.append(1)
            if len(n) == 2:
                raise _http(409, "usb busy")
            return await orig(self, name)
        with patch.object(RelocClient, "start", flaky):
            _, job = await env.run(s["session_id"])
        assert "not restarted" in job.error

    async def test_starting_a_mapping_session_is_refused_while_a_job_runs(self, env):
        _robot(env.db)
        s = _unplaced(env.db)
        env.block = asyncio.Event()
        await env.place(s["session_id"])
        assert env.jobs.active_for("r1") is not None
        with pytest.raises(HTTPException) as err:
            await maps.start_session(None, "shed", {"robot": "r1", "purpose": "mapping"},
                                     m1.PUB, switch=env.switch, reloc_jobs=env.jobs)
        assert err.value.status_code == 409 and "relocaliz" in err.value.detail
        await env.jobs.cancel(env.jobs.active_for("r1"))


class TestCanStartBlockers:
    """can_start / can_start_reason say what the server would refuse."""

    async def _read(self, env, s, jobs=True):
        return (await maps.placement_suggestions(
            None, "shed", str(s["session_id"]), holder=env.holder, switch=env.switch,
            reloc_jobs=env.jobs if jobs else None))["reloc"]

    async def test_free_robot_can_start(self, env):
        _robot(env.db)
        s = _unplaced(env.db)
        reloc = await self._read(env, s)
        assert reloc["can_start"] is True and reloc["can_start_reason"] is None

    async def test_driving_is_no_blocker(self, env):
        _robot(env.db)
        env.db.state_msgs["r1"] = {"driving": True}
        s = _unplaced(env.db)
        reloc = await self._read(env, s)
        assert reloc["can_start"] is True and reloc["can_start_reason"] is None
        out = await maps.map_reloc(None, env.holder, "shed", "r1", env.switch, env.jobs)
        assert out["can_start"] is True and out["warning"] is None

    async def test_open_mapping_session_is_not_offered(self, env):
        _robot(env.db)
        s = _unplaced(env.db, purpose="mapping")
        reloc = await self._read(env, s)
        assert reloc["can_start"] is False and "mapping session" in reloc["can_start_reason"]
        assert reloc["warning"] == reloc["can_start_reason"]
        assert await _status(env.place(s["session_id"])) == 409
        assert env.orch.calls == []

    async def test_can_start_with_the_warning_text_whenever_the_robot_exists(self, env):
        _robot(env.db, online=False)
        env.holder = CapHolder(held=None, can=True)
        env.holder.reason = None

        async def cap(robot, cloud_map_id, fresh=False, held=None):
            return True, "the robot's orchestrator could not be asked for its stored maps"
        env.holder.reloc_capability = cap
        s = _unplaced(env.db)
        reloc = await self._read(env, s)
        assert reloc["can_start"] is True
        assert reloc["warning"] == reloc["can_start_reason"] and "could not be asked" in (
            reloc["warning"])

    async def test_active_job_is_a_warning_and_the_second_post_is_409(self, env):
        _robot(env.db)
        s = _unplaced(env.db)
        env.block = asyncio.Event()
        await env.place(s["session_id"])
        reloc = await self._read(env, s)
        assert reloc["can_start"] is True and "already running" in reloc["warning"]
        assert await _status(env.place(s["session_id"])) == 409
        await env.jobs.cancel(env.jobs.active_for("r1"))

    async def test_pending_slam_save_is_a_warning(self, env):
        _robot(env.db)
        s = _unplaced(env.db)

        class Switch(MappingSwitch):
            def slam_save_pending(self, robot_name):
                return True
        env.switch = _switch(Switch)
        reloc = await self._read(env, s)
        assert reloc["can_start"] is True and "SLAM" in reloc["warning"]
        assert await _status(env.place(s["session_id"])) == 409

    async def test_without_a_registry_a_running_job_is_not_a_blocker(self, env):
        _robot(env.db)
        s = _unplaced(env.db)
        env.block = asyncio.Event()
        await env.place(s["session_id"])
        assert (await self._read(env, s, jobs=False))["can_start"] is True
        await env.jobs.cancel(env.jobs.active_for("r1"))


class TestCancel:
    async def test_cancel_while_waiting_restores_the_previous_values(self, env):
        _robot(env.db)
        prev = [1.0, 2.0, 0.0, 0.0, 0.0, 0.0, 1.0]
        env.orch.init_pos = prev
        s = _unplaced(env.db)
        started = asyncio.Event()

        def hook(e):
            started.set()
        env.on_sleep = hook
        env.block = asyncio.Event()
        await env.place(s["session_id"], ASSISTED)
        job = env.jobs.latest("shed", str(s["session_id"]))
        await asyncio.wait_for(started.wait(), 2)
        assert job.state == rj.WAITING and env.orch.current_map == ONBOARD
        done = await env.jobs.cancel(job)
        assert done.state == rj.CANCELLED and done.step == "cancelled"
        assert env.orch.current_map == "old-map" and env.orch.init_pos == prev
        assert env.db.sessions[-1]["aligned"] is False and env.db.events == []
        assert "stop" not in env.orch.ops()                     # the service is left alone

    async def test_cancel_before_it_ran(self, env):
        _robot(env.db)
        s = _unplaced(env.db)
        await env.place(s["session_id"])
        job = env.jobs.latest("shed", str(s["session_id"]))
        done = await env.jobs.cancel(job)                        # the task has not started
        assert done.state == rj.CANCELLED and env.orch.calls == []
        assert env.jobs.active_for("r1") is None

    async def test_cancel_a_finished_job_is_409(self, env):
        _robot(env.db)
        s = _unplaced(env.db)
        env.on_sleep = _localized_after(1)
        _, job = await env.run(s["session_id"])
        with pytest.raises(HTTPException) as err:
            await env.jobs.cancel(job)
        assert err.value.status_code == 409

    async def test_a_new_job_can_start_after_a_cancel(self, env):
        _robot(env.db)
        s = _unplaced(env.db)
        await env.place(s["session_id"])
        await env.jobs.cancel(env.jobs.latest("shed", str(s["session_id"])))
        env.on_sleep = _localized_after(1)
        _, job = await env.run(s["session_id"])
        assert job.state == rj.PLACED
        assert env.jobs.latest("shed", str(s["session_id"])) is job


# --- 7. the client and the routes ----------------------------------------------------------------------------------

class TestClientRoutes:
    async def test_new_methods(self):
        seen = []

        def handler(request):
            import json as _json
            seen.append((request.method, request.url.path,
                         _json.loads(request.content) if request.content else None))
            if request.url.path == "/maps/lab" and request.method == "GET":
                return httpx.Response(200, json={"name": "lab", "init_pos": None})
            if request.url.path == "/robot/config/map":
                return httpx.Response(200, json={"current_map": "lab"})
            return httpx.Response(200, json={"name": "lab"})

        def factory(timeout):
            return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=timeout)
        robot = RobotObjectV1(name="r1", ip_address="10.0.0.5", entrypoint_port=8080,
                             status=RobotStatusV1())
        client = oc.OrchestratorClient(robot, http_factory=factory)
        assert (await client.get_map("lab"))["name"] == "lab"
        await client.patch_map("lab", {"init_pos": None})
        assert await client.get_config_map() == "lab"
        assert await client.set_config_map("lab") == "lab"
        await client.set_config_map(None)
        assert seen == [("GET", "/maps/lab", None), ("PATCH", "/maps/lab", {"init_pos": None}),
                        ("GET", "/robot/config/map", None),
                        ("PUT", "/robot/config/map", {"current_map": "lab"}),
                        ("PUT", "/robot/config/map", {"current_map": None})]

    async def test_errors_are_typed(self):
        def factory(timeout):
            return httpx.AsyncClient(transport=httpx.MockTransport(
                lambda r: httpx.Response(422, json={"detail": "bad init_pos"})), timeout=timeout)
        client = oc.OrchestratorClient(RobotObjectV1(name="r1", status=RobotStatusV1(), ip_address="10.0.0.5",
                                                     entrypoint_port=8080), http_factory=factory)
        with pytest.raises(oc.OrchestratorError) as err:
            await client.patch_map("lab", {"init_pos": [1]})
        assert (err.value.kind, err.value.status, err.value.detail) == (oc.HTTP, 422,
                                                                        "bad init_pos")


class TestHttpRoutes:
    async def test_place_is_202_and_get_and_delete(self, env):
        import packages.api.main as main
        _robot(env.db)
        s = _unplaced(env.db)
        sid = str(s["session_id"])
        gate = asyncio.Event()
        env.on_sleep = lambda e: gate.set()
        env.block = asyncio.Event()
        svc = SimpleNamespace(database=None, mapping_switch=env.switch,
                              orchestrator_maps=env.holder, reloc_jobs=env.jobs)
        with patch.object(main, "service", svc), \
                patch.object(main.recording, "request_actor", lambda: "ann"):
            resp = await main.place_map_session("shed", sid, dict(RELOC))
            assert resp.status_code == 202
            import json as _json
            body = _json.loads(resp.body)
            assert body["reloc_job"]["state"] in ("preparing", "starting", "waiting")
            await asyncio.wait_for(gate.wait(), 2)
            got = await main.get_reloc_job("shed", sid)
            assert got["id"] == body["reloc_job"]["id"] and got["state"] == "waiting"
            assert got["mode"] == "odin" and got["deadline"]
            cancelled = await main.cancel_reloc_job("shed", sid)
            assert cancelled["state"] == "cancelled"
            with pytest.raises(HTTPException) as err:
                await main.cancel_reloc_job("shed", sid)
            assert err.value.status_code == 409
            with pytest.raises(HTTPException) as err:
                await main.get_reloc_job("shed", "6f1c0c2e-0000-4000-8000-000000000099")
            assert err.value.status_code == 404

    async def test_legacy_place_stays_200(self, env):
        import packages.api.main as main
        _robot(env.db)
        env.holder = CapHolder(can=False, reason="no")
        s = _unplaced(env.db)
        svc = SimpleNamespace(database=None, mapping_switch=env.switch,
                              orchestrator_maps=env.holder, reloc_jobs=env.jobs)
        with patch.object(main, "service", svc), \
                patch.object(main.recording, "request_actor", lambda: "ann"):
            out = await main.place_map_session("shed", str(s["session_id"]), dict(RELOC))
        assert isinstance(out, dict) and out["changed"] is True


# --- 9. endpoint mode: POST /maps/{name}/relocalize (the real orchestrator) ------------------------

class EndpointOrch(RelocOrch):
    """An orchestrator with POST /maps/{name}/relocalize and no reloc service."""

    def __init__(self, mapping=None, **kw):
        kw.setdefault("services", ())
        super().__init__(**kw)
        self.mapping = mapping or {"active": False, "map": None, "pid": None, "saving": False,
                                   "mode": None, "relocalizing": None}


class EndpointClient(RelocClient):
    async def mapping_state(self):
        self.o.enter("mapping_state")
        return dict(self.o.mapping)

    async def relocalize(self, name):
        self.o.enter("relocalize", name)
        self.o.mapping = {**self.o.mapping, "mode": "relocalization", "relocalizing": name}
        return {"success": True}

    async def stop_mapping(self):
        self.o.enter("stop_mapping")
        if self.o.mapping.get("mode") is None:
            raise _http(404, "nothing running")
        self.o.mapping = {**self.o.mapping, "mode": None, "relocalizing": None, "active": False}
        return {"success": True}


@pytest.fixture
def eenv(env):
    orch = EndpointOrch()
    orch.db = env.db
    env.orch = orch
    env.jobs._client_factory = lambda robot: EndpointClient(orch)
    env.jobs.force_service = False
    return env


class TestEndpointMode:
    async def test_happy_path_assisted(self, eenv):
        env = eenv
        _robot(env.db, position_initialized=False)
        s = _unplaced(env.db)
        env.on_sleep = _localized_after(2)
        out, job = await env.run(s["session_id"], ASSISTED)
        assert out["reloc_job"]["mode"] == "assisted" and job.state == rj.PLACED
        ops = env.orch.ops()
        # no current_map and no service call; relocalize after the PATCH
        assert ops[:3] == ["list_maps", "mapping_state", "get_map"]
        assert ops.index("patch_map") < ops.index("relocalize")
        assert not {"set_config_map", "get_config_map", "list_services", "start", "stop"} & set(ops)
        assert "stop_mapping" not in ops
        assert env.orch.init_pos is not None and env.orch.init_pos[0] == 2.0
        assert ("relocalize", ONBOARD) in env.orch.calls
        row = env.db.sessions[-1]
        assert row["aligned"] is True and row["placement"]["init_pose"] == INIT
        assert env.orch.mapping["mode"] == "relocalization"      # left running after success

    async def test_odin_mode_clears_init_pos(self, eenv):
        env = eenv
        _robot(env.db, position_initialized=False)
        env.orch.init_pos = [9.0, 9.0, 0.0, 0.0, 0.0, 0.0, 1.0]
        s = _unplaced(env.db)
        env.on_sleep = _localized_after(1)
        _, job = await env.run(s["session_id"])
        assert job.state == rj.PLACED and env.orch.init_pos is None
        assert ("patch_map", (ONBOARD, {"init_pos": None})) in env.orch.calls

    async def test_a_previous_relocalization_is_stopped_first(self, eenv):
        env = eenv
        _robot(env.db, position_initialized=False)
        env.orch.mapping.update(mode="relocalization", relocalizing="cloud-old")
        s = _unplaced(env.db)
        env.on_sleep = _localized_after(1)
        _, job = await env.run(s["session_id"])
        assert job.state == rj.PLACED
        ops = env.orch.ops()
        assert ops.index("stop_mapping") < ops.index("relocalize")

    async def test_a_slam_session_is_never_stopped(self, eenv):
        env = eenv
        _robot(env.db)
        env.orch.init_pos = [1.0, 2.0, 0.0, 0.0, 0.0, 0.0, 1.0]
        env.orch.mapping.update(active=True, map="cloud-x", mode="slam")
        s = _unplaced(env.db)
        _, job = await env.run(s["session_id"], ASSISTED)
        assert job.state == rj.FAILED and "SLAM mapping session is active" in job.error
        assert "stop_mapping" not in env.orch.ops() and "relocalize" not in env.orch.ops()
        assert env.orch.init_pos == [1.0, 2.0, 0.0, 0.0, 0.0, 0.0, 1.0]   # restored

    async def test_a_relocalization_not_started_by_the_cloud_is_stopped_with_a_warning(
            self, eenv):
        env = eenv
        _robot(env.db)
        env.orch.mapping.update(mode="relocalization", relocalizing="by-hand")
        s = _unplaced(env.db)
        env.on_sleep = _localized_after(1)
        _, job = await env.run(s["session_id"])
        assert job.state == rj.PLACED
        assert "stop_mapping" in env.orch.ops() and "relocalize" in env.orch.ops()
        assert any("not started by the cloud" in w for w in job.view()["warnings"])

    async def test_relocalize_404_falls_back_to_the_service(self, eenv):
        env = eenv
        _robot(env.db)
        env.orch.services = {"odin_reloc": False}

        async def nope(self, name):
            self.o.enter("relocalize", name)
            raise _http(405, "method not allowed")
        s = _unplaced(env.db)
        env.on_sleep = _localized_after(1)
        with patch.object(EndpointClient, "relocalize", nope):
            _, job = await env.run(s["session_id"])
        assert job.state == rj.PLACED
        assert ("start", "odin_reloc") in env.orch.calls and env.orch.current_map == ONBOARD

    async def test_relocalize_404_without_a_service_fails_with_the_answer(self, eenv):
        env = eenv
        _robot(env.db)

        async def nope(self, name):
            self.o.enter("relocalize", name)
            raise _http(404, "no such route")
        s = _unplaced(env.db)
        with patch.object(EndpointClient, "relocalize", nope):
            _, job = await env.run(s["session_id"])
        assert job.state == rj.FAILED and "404" in job.error
        assert env.orch.current_map == "old-map"        # what the fallback changed is restored

    async def test_a_slam_session_recording_still_fails_and_is_not_stopped(self, eenv):
        env = eenv
        _robot(env.db)
        env.orch.mapping.update(mode="slam", active=True)
        s = _unplaced(env.db)
        _, job = await env.run(s["session_id"])
        assert job.state == rj.FAILED and "SLAM" in job.error
        assert "stop_mapping" not in env.orch.ops()

    async def test_relocalize_409_is_not_stopped_on_rollback(self, eenv):
        env = eenv
        _robot(env.db)
        prev = [1.0, 2.0, 0.0, 0.0, 0.0, 0.0, 1.0]
        env.orch.init_pos = prev
        env.orch.fail["relocalize"] = _http(409, "driver already running")
        s = _unplaced(env.db)
        _, job = await env.run(s["session_id"], ASSISTED)
        assert job.state == rj.FAILED and "409" in job.error and "Odin USB" in job.error
        assert "stop_mapping" not in env.orch.ops() and env.orch.init_pos == prev

    async def test_failure_after_start_stops_the_session_and_restores(self, eenv):
        env = eenv
        _robot(env.db, position_initialized=False)
        prev = [1.0, 2.0, 0.0, 0.0, 0.0, 0.0, 1.0]
        env.orch.init_pos = prev
        s = _unplaced(env.db)

        def hook(e):    # the session is finished mid-job
            e.db.sessions[-1]["ended_at"] = "2026-01-01T00:00:00+00:00"
        env.on_sleep = hook
        _, job = await env.run(s["session_id"], ASSISTED)
        assert job.state == rj.FAILED and "finished" in job.error
        assert env.orch.ops()[-2:] == ["stop_mapping", "patch_map"]
        assert env.orch.mapping["mode"] is None and env.orch.init_pos == prev

    async def test_driver_died_fails_with_a_readable_error(self, eenv):
        env = eenv
        _robot(env.db, position_initialized=False)
        s = _unplaced(env.db)

        def hook(e):    # the driver exits
            e.orch.mapping = {**e.orch.mapping, "mode": None, "relocalizing": None}
        env.on_sleep = hook
        _, job = await env.run(s["session_id"])
        assert job.state == rj.FAILED and "Odin driver stopped" in job.error
        assert env.sleeps <= 4 and env.db.sessions[-1]["aligned"] is False

    async def test_timeout_leaves_the_session_running(self, eenv):
        env = eenv
        _robot(env.db, position_initialized=False)
        env.orch.init_pos = [1.0, 2.0, 0.0, 0.0, 0.0, 0.0, 1.0]
        s = _unplaced(env.db)
        _, job = await env.run(s["session_id"], ASSISTED)
        assert job.state == rj.FAILED and "relocalization session is left running" in job.error
        assert "stop_mapping" not in env.orch.ops()
        assert env.orch.mapping["mode"] == "relocalization"
        assert env.orch.init_pos[0] == 2.0       # not restored

    async def test_cancel_stops_the_session_and_restores(self, eenv):
        env = eenv
        _robot(env.db)
        prev = [1.0, 2.0, 0.0, 0.0, 0.0, 0.0, 1.0]
        env.orch.init_pos = prev
        s = _unplaced(env.db)
        started = asyncio.Event()
        env.on_sleep = lambda e: started.set()
        env.block = asyncio.Event()
        await env.place(s["session_id"], ASSISTED)
        job = env.jobs.latest("shed", str(s["session_id"]))
        await asyncio.wait_for(started.wait(), 2)
        done = await env.jobs.cancel(job)
        assert done.state == rj.CANCELLED
        assert env.orch.mapping["mode"] is None and env.orch.init_pos == prev

    async def test_the_same_map_already_relocalizing_is_restarted(self, eenv):
        env = eenv
        _robot(env.db, position_initialized=False)
        env.orch.mapping.update(mode="relocalization", relocalizing=ONBOARD)
        s = _unplaced(env.db)
        env.on_sleep = _localized_after(1)
        _, job = await env.run(s["session_id"])
        assert job.state == rj.PLACED
        ops = env.orch.ops()
        assert ops.index("stop_mapping") < ops.index("relocalize")

    async def test_cancel_never_stops_a_slam_session_started_meanwhile(self, eenv):
        env = eenv
        _robot(env.db)
        prev = [1.0, 2.0, 0.0, 0.0, 0.0, 0.0, 1.0]
        env.orch.init_pos = prev
        s = _unplaced(env.db)
        started = asyncio.Event()

        def hook(e):    # the relocalization driver is replaced by a SLAM recording (proxy call)
            e.orch.mapping = {"active": True, "map": "cloud-x", "pid": 7, "saving": False,
                              "mode": "slam", "relocalizing": None}
            started.set()
        env.on_sleep = hook
        env.block = asyncio.Event()
        await env.place(s["session_id"], ASSISTED)
        job = env.jobs.latest("shed", str(s["session_id"]))
        await asyncio.wait_for(started.wait(), 2)
        done = await env.jobs.cancel(job)
        assert done.state == rj.CANCELLED
        assert "stop_mapping" not in env.orch.ops()
        assert env.orch.mapping["mode"] == "slam" and env.orch.init_pos == prev

    async def test_a_blip_reading_the_mapping_state_does_not_switch_to_the_service(self, eenv):
        env = eenv
        _robot(env.db, position_initialized=False)
        s = _unplaced(env.db)
        env.on_sleep = _localized_after(1)
        real = EndpointClient.mapping_state
        n = {"calls": 0}

        async def flaky(self):
            n["calls"] += 1
            if n["calls"] == 1:
                raise oc.OrchestratorError(oc.TIMEOUT, "timed out")
            return await real(self)
        with patch.object(EndpointClient, "mapping_state", flaky):
            _, job = await env.run(s["session_id"])
        assert job.state == rj.PLACED and "relocalize" in env.orch.ops()
        assert "start" not in env.orch.ops() and "set_config_map" not in env.orch.ops()

    async def test_an_unreadable_mapping_state_fails_instead_of_guessing_the_service(self, eenv):
        env = eenv
        _robot(env.db)
        s = _unplaced(env.db)

        async def down(self):
            raise oc.OrchestratorError(oc.TIMEOUT, "timed out")
        with patch.object(EndpointClient, "mapping_state", down):
            _, job = await env.run(s["session_id"])
        assert job.state == rj.FAILED and "mapping state" in job.error
        assert "start" not in env.orch.ops() and "relocalize" not in env.orch.ops()

    async def test_an_older_orchestrator_uses_the_service(self, env):
        class Old(RelocClient):
            async def mapping_state(self):
                return {"active": False, "map": None, "pid": None}
        env.jobs._client_factory = lambda robot: Old(env.orch)
        _robot(env.db, position_initialized=False)
        s = _unplaced(env.db)
        env.on_sleep = _localized_after(1)
        _, job = await env.run(s["session_id"])
        assert job.state == rj.PLACED and ("start", "odin_reloc") in env.orch.calls
        assert "relocalize" not in env.orch.ops()

    async def test_force_service_keeps_the_service_path(self, eenv):
        env = eenv
        env.jobs.force_service = True
        env.orch.services = {"odin_reloc": False}
        _robot(env.db, position_initialized=False)
        s = _unplaced(env.db)
        env.on_sleep = _localized_after(1)
        _, job = await env.run(s["session_id"])
        assert job.state == rj.PLACED
        assert ("start", "odin_reloc") in env.orch.calls and "relocalize" not in env.orch.ops()


class _EpCap:
    """Orchestrator for the capability tests: /maps/list, /maps/mapping, /services."""

    def __init__(self, held=True, mapping="new", services=("odin_reloc",), services_fail=None,
                 services_slow=False):
        self.held, self.mapping, self.services = held, mapping, services
        self.services_fail, self.services_slow = services_fail, services_slow
        self.calls = []

    def client(self, robot):
        o = self

        class C:
            async def list_maps(self, cloud_map_id):
                o.calls.append("list_maps")
                return [{"name": "n", "valid": True, "meta": {"cloud_map_id": cloud_map_id}}] \
                    if o.held else []

            async def mapping_state(self):
                o.calls.append("mapping_state")
                if o.mapping == "new":
                    return {"active": False, "mode": None, "relocalizing": None}
                if o.mapping == "old":
                    return {"active": False, "map": None, "pid": None}
                raise o.mapping

            async def list_services(self):
                o.calls.append("list_services")
                if o.services_slow:
                    await asyncio.sleep(3600)
                if o.services_fail:
                    raise o.services_fail
                return [{"name": n} for n in o.services]
        return C()


class TestEndpointCapability:
    async def test_endpoint_path_needs_no_services_call(self):
        o = _EpCap(services=(), services_fail=oc.OrchestratorError(oc.TIMEOUT, "slow"))
        h = OrchestratorMaps(client_factory=o.client)
        assert await h.reloc_capability(_plain_robot(), "shed") == (True, None)
        assert "list_services" not in o.calls

    async def test_a_hanging_services_call_does_not_matter(self):
        o = _EpCap(services_slow=True)
        h = OrchestratorMaps(client_factory=o.client)
        assert await asyncio.wait_for(h.reloc_capability(_plain_robot(), "shed"), 2) == (
            True, None)

    async def test_small_reads_come_before_services(self):
        o = _EpCap(mapping="old")
        h = OrchestratorMaps(client_factory=o.client)
        assert await h.reloc_capability(_plain_robot(), "shed") == (True, None)
        assert o.calls.index("list_services") > o.calls.index("list_maps")
        assert o.calls.index("list_services") > o.calls.index("mapping_state")

    async def test_not_held_is_a_warning_next_to_the_other_findings(self):
        o = _EpCap(held=False, mapping="old")
        can, why = await OrchestratorMaps(client_factory=o.client).reloc_capability(
            _plain_robot(), "shed")
        assert can is True and "does not hold" in why

    async def test_old_orchestrator_falls_back_to_the_service(self):
        o = _EpCap(mapping="old")
        assert await OrchestratorMaps(client_factory=o.client).reloc_capability(
            _plain_robot(), "shed") == (True, None)

    async def test_mapping_state_404_falls_back_to_the_service(self):
        o = _EpCap(mapping=oc.OrchestratorError(oc.HTTP, "nf", status=404))
        assert await OrchestratorMaps(client_factory=o.client).reloc_capability(
            _plain_robot(), "shed") == (True, None)

    async def test_neither_gives_a_reason(self):
        o = _EpCap(mapping="old", services=("topomap",))
        can, why = await OrchestratorMaps(client_factory=o.client).reloc_capability(
            _plain_robot(), "shed")
        assert can is True and "no relocalization service" in why and "older orchestrator" in why

    async def test_endpoint_unreadable_and_no_service(self):
        o = _EpCap(mapping=oc.OrchestratorError(oc.UNREACHABLE, "no route"),
                   services_fail=oc.OrchestratorError(oc.UNREACHABLE, "no route"))
        can, why = await OrchestratorMaps(client_factory=o.client).reloc_capability(
            _plain_robot(), "shed")
        assert can is True and "no route" in why

    async def test_endpoint_answer_is_cached_and_invalidated(self):
        o = _EpCap()
        h = OrchestratorMaps(client_factory=o.client)
        r = _plain_robot()
        await h.reloc_capability(r, "shed")
        await h.reloc_capability(r, "shed")
        assert o.calls.count("mapping_state") == 1
        h.invalidate("r1")
        await h.reloc_capability(r, "shed")
        assert o.calls.count("mapping_state") == 2

    def test_supports_relocalize(self):
        assert oc.supports_relocalize({"mode": None, "relocalizing": None})
        assert not oc.supports_relocalize({"active": False, "map": None, "pid": None})
        assert not oc.supports_relocalize(None)
