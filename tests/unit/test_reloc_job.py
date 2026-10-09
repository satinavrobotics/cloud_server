"""Starting relocalization from the API (docs/satinav-maps-redesign.md ## 16, packages/api/reloc_job.py).

- `reloc.can_start` / `can_start_reason` on GET /maps/{id}/reloc and placement-suggestions;
- POST .../place {"source": "reloc"} with can_start: 202 and a job (mode "odin": init_pos null; mode
  "assisted": the user's pose as init_pos), the calls in order, none inside a DB transaction;
- the refusals, the legacy check-only placement when can_start is false;
- failure / rollback / timeout / cancel / session closed mid-job;
- the frame is decided in ONE place (map_sessions.reloc_bin_pose / reloc_map_t_session);
- the OrchestratorClient map routes; the HTTP routes (202, GET, DELETE).

The robot relocalizes through its localization facade (GET / PUT /localization); the PUT's own
answers (problem, applied, 503, ...) are in test_localization_facade.py.
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
    """One robot's orchestrator as the reloc job uses it; every call is recorded. `intent` is
    its stored localization intent (GET / PUT /localization)."""

    def __init__(self, held=True, init_pos=None, intent=None, fail=None):
        self.rows = ([{"name": ONBOARD, "valid": True, "meta": {"cloud_map_id": "shed"}}]
                     if held else [])
        self.init_pos = init_pos
        self.intent = dict(intent or {"mode": "relocalization", "map": "old-map"})
        self.fail = fail or {}      # op -> OrchestratorError
        self.calls = []             # (op, arg)
        self.db = None

    @property
    def current_map(self):
        """The map of the stored intent (where the robot relocalizes)."""
        return self.intent.get("map")

    def enter(self, op, arg=None):
        self.calls.append((op, arg))
        if self.db is not None:
            assert self.db.open_tx == 0, f"orchestrator {op} inside a DB transaction"
        if op in self.fail:
            raise self.fail[op]

    def ops(self):
        return [c[0] for c in self.calls]

    def puts(self):
        return [c[1] for c in self.calls if c[0] == "put_localization"]


class RelocClient:
    def __init__(self, orch):
        self.o = orch

    async def list_maps(self, cloud_map_id):
        self.o.enter("list_maps", cloud_map_id)
        return self.o.rows

    async def get_map(self, name):
        self.o.enter("get_map", name)
        if not self.o.rows:     # no stored map at all: the robot does not know it
            raise _http(404, f"map '{name}' not found")
        return {"name": name, "init_pos": self.o.init_pos}

    async def patch_map(self, name, body):
        self.o.enter("patch_map", (name, body))
        self.o.init_pos = body["init_pos"]
        return {}

    async def get_localization(self):
        self.o.enter("get_localization")
        return dict(self.o.intent)

    async def put_localization(self, mode, map_name=None, wait=False, topomap=None):
        self.o.enter("put_localization", (mode, map_name))
        self.o.intent = {"mode": mode, "map": map_name}
        return {"mode": mode, "map": map_name, "applied": True}


async def _get_map_any(self, name):
    self.o.enter("get_map", name)
    return {"name": name, "init_pos": self.o.init_pos}


class _NoMappingOrch:
    """The mapping switch's view of the robot (notify_robot reads it): unreachable, at once."""

    async def list_services(self):
        raise oc.OrchestratorError(oc.UNREACHABLE, "not reachable")

    async def get_localization(self):
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
                        sleep=sleep, timeout=90.0, poll=1.0,
                        confirm_timeout=0.0)   # 0: the job confirms by itself at once
    e = Env(d, orch, CapHolder(), jobs, _switch(), now)
    with patch.object(maps, "open_store", d.store), patch.object(maps, "_utcnow", m1.Clock()):
        yield e


def _robot(db, name="r1", online=True, **status):
    db.robots[name] = RobotObjectV1(
        name=name, ip_address="10.0.0.5", entrypoint_port=8080,
        status=RobotStatusV1(online=online, state="IDLE",
                             pose={"x": 3.0, "y": 4.0, "theta": 0.2}, **status))
    return db.robots[name]


def _localized_after(n, score=0.9, map_id=ONBOARD):
    """on_sleep hook: the robot reports itself localized on `map_id` after `n` polls."""
    def hook(e):
        if e.sleeps >= n:
            st = e.robot().status
            st.position_initialized, st.localization_score = True, score
            st.pose.map_id = map_id
    return hook


# --- 1. the capability flag ------------------------------------------------------------------------

class _CapOrch:
    def __init__(self, held=True, fail=None):
        self.held, self.fail = held, fail

    def client(self, robot):
        o = self

        class C:
            async def list_maps(self, cloud_map_id):
                if o.fail:
                    raise o.fail
                if cloud_map_id is None:   # all stored maps
                    return [{"name": oc.onboard_map_name("shed"), "valid": True}] \
                        if o.held == "named" else [{"name": "other", "valid": True}]
                return [{"name": "n", "valid": True, "meta": {"cloud_map_id": cloud_map_id}}] \
                    if o.held is True else []
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
        (dict(), dict(fail=oc.OrchestratorError(oc.UNREACHABLE, "no route")), "could not be asked"),
    ])
    async def test_can_always_start_what_is_wrong_is_only_a_warning(self, robot, kw, words):
        can, why = await _real_holder(**kw).reloc_capability(_plain_robot(**robot), "shed")
        assert can is True and words in why

    async def test_no_stored_map_on_the_robot_cannot_start(self):
        can, why = await _real_holder(held=False).reloc_capability(_plain_robot(), "shed")
        assert can is False and "does not hold a stored map for 'shed'" in why

    async def test_a_map_named_for_the_cloud_map_counts_as_held(self):
        h = _real_holder(held="named")
        assert await h.held(_plain_robot(), "shed") is True
        assert await h.reloc_capability(_plain_robot(), "shed") == (True, None)

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
        can, why = await h.reloc_capability(_plain_robot(), "shed", held=False)
        assert can is False and "does not hold a stored map for 'shed'" in why

    async def test_on_the_reads(self, env):
        _robot(env.db)
        s = _unplaced(env.db)
        env.db.add_map("shed", type="local", status={"state": "ready"})
        holder = _real_holder()
        out = await maps.placement_suggestions(None, "shed", str(s["session_id"]), holder=holder)
        assert out["reloc"] == {"available": True, "known": True, "source": "orchestrator",
                                "can_start": True, "can_start_reason": None, "warning": None}
        out = await maps.map_reloc(None, holder, "shed", "r1")
        assert out["can_start"] is True and out["can_start_reason"] is None
        out = await maps.map_reloc(None, _real_holder(held=False), "shed", "r1")
        assert out["can_start"] is False and "does not hold" in out["can_start_reason"]
        assert out["warning"] == out["can_start_reason"] and out["available"] is False

    async def test_unknown_robot_cannot_start_on_the_reads(self, env):
        env.db.add_map("shed", type="local", status={"state": "ready"})
        out = await maps.map_reloc(None, _real_holder(), "shed", "ghost")
        assert out["can_start"] is False and out["can_start_reason"]

    def test_timeout_comes_from_config(self):
        from packages import config
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
        # the calls, in order; the stale init_pos is cleared (null), then the robot is switched
        # to relocalization on the stored map
        assert env.orch.calls == [
            ("list_maps", "shed"), ("get_map", ONBOARD),
            ("patch_map", (ONBOARD, {"init_pos": None})), ("get_localization", None),
            ("put_localization", ("relocalization", ONBOARD))]
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

    async def test_no_orchestrator_call_inside_a_db_transaction(self, env):
        # the fake asserts it on every call; make sure the job really made calls and reads
        _robot(env.db)
        s = _unplaced(env.db)
        env.on_sleep = _localized_after(2)
        _, job = await env.run(s["session_id"])
        assert job.state == rj.PLACED and len(env.orch.calls) == 5 and env.db.open_tx == 0

    async def test_initialized_on_another_map_is_not_localized_yet(self, env):
        # the robot reports a map name only once LOCALIZED: an initialized pose in its own frame
        # ("map") or on another stored map is not the relocalization done
        _robot(env.db, position_initialized=True, localization_score=0.8)
        env.robot().status.pose.map_id = "map"
        s = _unplaced(env.db)

        def hook(e):
            if e.sleeps == 2:
                e.robot().status.pose.map_id = "cloud-other"
            if e.sleeps == 4:
                e.robot().status.pose.map_id = ONBOARD
        env.on_sleep = hook
        _, job = await env.run(s["session_id"])
        assert job.state == rj.PLACED and env.sleeps == 4

    async def test_already_localized_on_the_map_places_at_once(self, env):
        _robot(env.db, position_initialized=True)
        env.robot().status.pose.map_id = ONBOARD
        env.orch.intent = {"mode": "relocalization", "map": ONBOARD}
        s = _unplaced(env.db)
        _, job = await env.run(s["session_id"])
        assert job.state == rj.PLACED and env.sleeps == 0
        assert env.orch.puts() == [("relocalization", ONBOARD)]   # odin: no pass-through


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

    async def test_a_placed_session_is_relocalized_again(self, env):
        _robot(env.db, position_initialized=True)
        s = _unplaced(env.db, aligned=True)
        env.on_sleep = _localized_after(1)
        _, job = await env.run(s["session_id"])
        assert job.state == rj.PLACED, job.error
        placed = next(x for x in env.db.sessions if str(x["session_id"]) == str(s["session_id"]))
        assert placed["placement"]["source"] == ms.SOURCE_RELOC and placed["aligned"] is True

    async def test_a_placement_by_someone_else_meanwhile_fails_the_job(self):
        job = SimpleNamespace(map_name="shed", placement_at=None)
        session = {"map_name": "shed", "ended_at": None, "aligned": True,
                   "map_t_session": {"tx": 0, "ty": 0, "yaw": 0},
                   "placement": {"at": "2026-10-08T21:00:00+00:00"}}
        with pytest.raises(rj._Fail, match="placed meanwhile"):
            rj.RelocJobs._check_session(job, session)
        job.placement_at = "2026-10-08T21:00:00+00:00"     # placed before the job: fine
        rj.RelocJobs._check_session(job, session)

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
        assert "put_localization" not in env.orch.ops()
        assert env.orch.init_pos == [1.0, 2.0, 0.0, 0.0, 0.0, 0.0, 1.0]

    async def test_put_refused_restores_the_init_pos_only(self, env):
        prev = [1.0, 2.0, 0.0, 0.0, 0.0, 0.0, 1.0]
        env.orch.init_pos = prev
        env.orch.fail["put_localization"] = _http(409, "a VDA5050 order is active")
        job = await self._failed(env, ASSISTED)
        assert "could not start relocalization on 'cloud-shed'" in job.error
        assert "order is active" in job.error
        assert env.orch.init_pos == prev                       # restored
        # partial=ok: a refusal changed nothing, so the intent is not PUT back
        assert env.orch.puts() == [("relocalization", ONBOARD)]
        assert env.orch.calls[-1] == ("patch_map", (ONBOARD, {"init_pos": prev}))

    @pytest.mark.parametrize("error,words", [
        (_http(502, "the device refused the map"), "502"),
        (_http(503, "driver starting"), "retry in a few seconds"),
        (oc.OrchestratorError(oc.UNREACHABLE, "orchestrator at 10.0.0.5:8080 is not reachable"),
         "not reachable"),
    ])
    async def test_orchestrator_statuses_become_readable_text(self, env, error, words):
        env.orch.fail["put_localization"] = error
        job = await self._failed(env)
        assert "could not start relocalization" in job.error and words in job.error

    async def test_a_put_that_timed_out_is_rolled_back(self, env):
        # a 504 / timeout may still have applied: the previous intent is PUT back
        env.orch.fail["put_localization"] = oc.OrchestratorError(oc.TIMEOUT, "timed out")
        orig = RelocClient.put_localization
        calls = []

        async def once(self, mode, map_name=None, wait=False, topomap=None):
            calls.append(mode)
            if len(calls) == 1:
                self.o.intent = {"mode": mode, "map": map_name}   # it did apply
                raise oc.OrchestratorError(oc.TIMEOUT, "timed out")
            self.o.fail.pop("put_localization", None)
            return await orig(self, mode, map_name, wait, topomap)
        with patch.object(RelocClient, "put_localization", once):
            job = await self._failed(env)
        assert "timed out" in job.error
        assert env.orch.intent == {"mode": "relocalization", "map": "old-map"}

    async def test_a_slam_intent_fails_and_is_never_left(self, env):
        env.orch.intent = {"mode": "slam", "map": None}
        env.orch.init_pos = [1.0, 2.0, 0.0, 0.0, 0.0, 0.0, 1.0]
        job = await self._failed(env, ASSISTED)
        assert "SLAM mapping session is active" in job.error
        assert env.orch.puts() == [] and env.orch.intent["mode"] == "slam"
        assert env.orch.init_pos == [1.0, 2.0, 0.0, 0.0, 0.0, 0.0, 1.0]   # restored

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
        orig = RelocClient.put_localization

        async def flaky(self, mode, map_name=None, wait=False, topomap=None):
            if map_name == "old-map":
                self.o.enter("put_localization", (mode, map_name))
                raise _http(409, "a VDA5050 order is active")
            return await orig(self, mode, map_name, wait, topomap)
        env.on_sleep = lambda e: setattr(e.robot().status, "online", False)
        with patch.object(RelocClient, "put_localization", flaky):
            job = await self._failed(env)
        assert "localization not restored to relocalization" in job.error
        assert "order is active" in job.error

    async def test_timeout_leaves_the_robot_relocalizing_and_restores_nothing(self, env):
        _robot(env.db, position_initialized=False)
        env.orch.init_pos = [1.0, 2.0, 0.0, 0.0, 0.0, 0.0, 1.0]
        s = _unplaced(env.db)
        _, job = await env.run(s["session_id"], ASSISTED)
        assert job.state == rj.FAILED and "did not report a localized position" in job.error
        assert "left relocalizing" in job.error
        assert env.orch.ops().count("patch_map") == 1
        assert env.orch.puts() == [("relocalization", ONBOARD)]
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
        orig = RelocClient.put_localization

        async def slow_put(self, *a, **kw):      # 200 s of robot-side work > the 90 s timeout
            env.clock[0] += 200
            return await orig(self, *a, **kw)
        env.on_sleep = _localized_after(3)
        with patch.object(RelocClient, "put_localization", slow_put):
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

    async def test_rollback_puts_the_previous_intent_back(self, env):
        _robot(env.db)
        s = _unplaced(env.db)
        env.on_sleep = lambda e: setattr(e.robot().status, "online", False)
        _, job = await env.run(s["session_id"])
        assert job.state == rj.FAILED
        assert env.orch.puts() == [("relocalization", ONBOARD), ("relocalization", "old-map")]

    async def test_no_previous_intent_restores_odometry(self, env):
        _robot(env.db)
        env.orch.intent = {"mode": None, "map": None}
        s = _unplaced(env.db)
        env.on_sleep = lambda e: setattr(e.robot().status, "online", False)
        await env.run(s["session_id"])
        assert env.orch.puts()[-1] == ("odometry", None)

    async def test_an_intent_changed_by_someone_else_is_not_overwritten(self, env):
        _robot(env.db)
        s = _unplaced(env.db)

        def hook(e):    # an operator switches the robot to slam meanwhile
            e.orch.intent = {"mode": "slam", "map": None}
            e.robot().status.online = False
        env.on_sleep = hook
        _, job = await env.run(s["session_id"])
        assert job.state == rj.FAILED
        assert env.orch.puts() == [("relocalization", ONBOARD)]
        assert env.orch.intent["mode"] == "slam"

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
        assert env.orch.puts()[-1] == ("relocalization", "old-map")

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
            return httpx.Response(200, json={"name": "lab"})

        def factory(timeout):
            return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=timeout)
        robot = RobotObjectV1(name="r1", ip_address="10.0.0.5", entrypoint_port=8080,
                             status=RobotStatusV1())
        client = oc.OrchestratorClient(robot, http_factory=factory)
        assert (await client.get_map("lab"))["name"] == "lab"
        await client.patch_map("lab", {"init_pos": None})
        assert seen == [("GET", "/maps/lab", None), ("PATCH", "/maps/lab", {"init_pos": None})]

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
