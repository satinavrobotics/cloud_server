"""The cloud's robot localization calls on the orchestrator's localization facade
(GET/PUT /localization, POST/GET /localization/save), the only localization routes it uses:
packages/api/orchestrator_client.py, mapping_switch.py (SLAM), reloc_job.py (relocalization),
orchestrator_maps.py (capability), orchestrator_proxy.py.

The robot is a fake HTTP orchestrator behind an httpx.MockTransport, so the real client runs.
"""
import asyncio
import json
import os

for _k in ("ARANGO_PASSWORD", "MINIO_ACCESS_KEY", "MINIO_SECRET_KEY", "POSTGRES_PASSWORD"):
    os.environ.setdefault(_k, "test")

from unittest.mock import AsyncMock, patch  # noqa: E402

import httpx  # noqa: E402
import pytest  # noqa: E402

from cloud_common.objects.robot import RobotObjectV1, RobotStatusV1  # noqa: E402
from packages.api import maps  # noqa: E402
from packages.api import orchestrator_client as oc  # noqa: E402
from packages.api import orchestrator_proxy as proxy  # noqa: E402
from packages.api import reloc_job as rj  # noqa: E402
from packages.api.mapping_switch import (  # noqa: E402
    MappingSwitch, SLAM_ALREADY_RUNNING, SLAM_EXISTS, SLAM_FAILED, SLAM_SAVED, SLAM_STARTED,
)
from packages.api.orchestrator_maps import OrchestratorMaps  # noqa: E402
from tests.unit import test_maps_m1 as m1  # noqa: E402
from tests.unit.test_reloc_job import (  # noqa: E402
    CapHolder, Env, TxDb, _robot, _switch,
)
from tests.unit.test_placement_suggestion import _unplaced  # noqa: E402

pytestmark = pytest.mark.unit

ONBOARD = "cloud-shed"


# Orchestrator routes deprecated by the localization facade: the cloud never calls them.
DEPRECATED = ("/maps/mapping", "/mapping/start", "/relocalize", "/robot/config/map")


class FakeRobot:
    """One robot's orchestrator over HTTP. The deprecated routes are not served (404)."""

    def __init__(self, intent=None, topomap=None):
        self.intent = dict(intent or {"mode": None, "map": None})
        self.topomap = topomap        # None: no mapping API, else whether the topomap runs
        self.log = []                 # (method, path, query, body)
        self.put_error = None         # (status, detail) for the next PUT /localization
        self.put_errors = []          # (status, detail) for the next PUTs, one each, first
        self.put_problem = None       # a partial=ok answer's `problem`
        self.applied = True
        self.save_error = None        # (status, detail) for the POST save
        self.save_polls = ["saving", "done"]    # what successive GET /localization/save say
        self.save_failure = "lidar_save_map failed"
        self.maps = set()             # stored map names
        self.init_pos = None
        self.save_started = False

    def calls(self, method, path):
        return [c for c in self.log if c[0] == method and c[1] == path]

    def deprecated_calls(self):
        return [c for c in self.log if any(d in c[1] for d in DEPRECATED)
                or (c[1].startswith("/maps/") and c[1].endswith("/save"))]

    def handler(self, request):
        path, method = request.url.path, request.method
        body = json.loads(request.content) if request.content else None
        query = dict(request.url.params)
        self.log.append((method, path, query, body))
        R = httpx.Response
        if path == "/localization":
            if method == "GET":
                return R(200, json={**self.intent, **({} if self.topomap is None
                                                      else {"topomap": self.topomap})})
            if self.put_errors or self.put_error:
                status, detail = self.put_errors.pop(0) if self.put_errors else self.put_error
                return R(status, json={"detail": detail})
            new = {"mode": body["mode"], "map": body.get("map")}
            want, extra = body.get("topomap"), {}
            if self.topomap is not None:
                if self.topomap and new != self.intent and want is not False:
                    return R(409, json={"detail": "the topomap runs: send topomap:false"})
                if want is None:
                    extra["topomap"] = "running" if self.topomap else "off"
                elif want == self.topomap:
                    extra["topomap"] = "already_running" if want else "off"
                else:
                    extra["topomap"] = "started" if want else "stopped"
                    self.topomap = want
            self.intent = new
            return R(200, json={**self.intent, "applied": self.applied, "localized": None,
                                "message": "ok", "problem": self.put_problem, **extra})
        if path == "/localization/save":
            if method == "POST":
                if self.save_error:
                    return R(self.save_error[0], json={"detail": self.save_error[1]})
                if self.intent["mode"] != "slam":
                    return R(409, json={"detail": "the driver is not in slam mode"})
                self.maps.add(body["name"])
                self.save_started = body["name"]
                return R(202, json={"started": True, "map": body["name"]})
            if not self.save_started:
                return R(404, json={"detail": "no save"})
            state = self.save_polls.pop(0) if len(self.save_polls) > 1 else self.save_polls[0]
            return R(200, json={"map": self.save_started, "status": state,
                                "error": self.save_failure if state == "failed" else None})
        # services (the topomap the mapping session starts)
        if path == "/services" and method == "GET":
            return R(200, json=[{"name": "topomap", "running": False}])
        if path.startswith("/services/topomap/"):
            return R(200, json={"success": True, "state": {"running": False}})
        if path.startswith("/maps/list"):
            return R(200, json=[{"name": n, "valid": True, "meta": {"cloud_map_id": "shed"}}
                                for n in self.maps])
        if any(d in path for d in DEPRECATED) or path.endswith("/save"):
            return R(404, json={"detail": "Not Found"})
        if path.startswith("/maps/") and method == "GET":
            name = path.split("/")[2]
            if name in self.maps:
                return R(200, json={"name": name, "init_pos": self.init_pos})
            return R(404, json={"detail": f"map '{name}' not found"})
        if path.startswith("/maps/") and method == "PATCH":
            self.init_pos = body["init_pos"]
            return R(200, json={})
        return R(404, json={"detail": "Not Found"})

    def client(self, robot):
        def factory(timeout):
            return httpx.AsyncClient(transport=httpx.MockTransport(self.handler),
                                     timeout=timeout)
        return oc.OrchestratorClient(robot, http_factory=factory)


def _plain_robot():
    return RobotObjectV1(name="r1", ip_address="10.0.0.5", entrypoint_port=8080,
                         status=RobotStatusV1(online=True))


async def _no_sleep(_s):
    await asyncio.sleep(0)


def _slam_switch(fake):
    return MappingSwitch(client_factory=fake.client, sleep=_no_sleep)


def _session_env(fake, slam_map=True):
    """A TxDb with map `yard` (a SLAM map unless `slam_map` is False) and robot r1 on `fake`."""
    d = TxDb()
    d.add_map("yard", type="local", status={"state": "draft"})
    d.maps["yard"]["spec"]["slam_map"] = slam_map
    d.robots["r1"] = _plain_robot()
    switch = _slam_switch(fake)
    switch.on_session, switch.on_state = AsyncMock(), AsyncMock()
    return d, switch


def _puts(fake):
    return [body for _m, _p, _q, body in fake.calls("PUT", "/localization")]


def _labels(out):
    return [(a["service"], a["action"], a["ok"]) for a in out["robot_actions"]]


# --- relocalization capability --------------------------------------------------------------------

class TestCapability:
    async def test_reloc_capability_is_the_stored_map_only(self):
        fake = FakeRobot()
        fake.maps.add(ONBOARD)
        holder = OrchestratorMaps(client_factory=fake.client)
        assert await holder.reloc_capability(_plain_robot(), "shed") == (True, None)
        assert [c[1] for c in fake.log] == ["/maps/list"]     # no /services, no /maps/mapping

    async def test_no_stored_map_cannot_start_and_an_unreadable_robot_warns(self):
        fake = FakeRobot()
        holder = OrchestratorMaps(client_factory=fake.client)
        can, why = await holder.reloc_capability(_plain_robot(), "shed")
        assert can is False and "does not hold a stored map" in why

        def boom(request):
            raise httpx.ConnectError("down")
        down = OrchestratorMaps(client_factory=lambda robot: oc.OrchestratorClient(
            robot, http_factory=lambda timeout: httpx.AsyncClient(
                transport=httpx.MockTransport(boom), timeout=timeout)))
        can, why = await down.reloc_capability(_plain_robot(), "shed")
        assert can is True and "could not be asked" in why


# --- the SLAM recording ----------------------------------------------------------------------------

class TestSlam:
    async def test_start_is_a_mode_switch_and_the_save_restores_the_previous_intent(self):
        fake = FakeRobot(intent={"mode": "relocalization", "map": "lab"})
        switch = _slam_switch(fake)
        res = await switch.start_slam(_plain_robot(), "shed")
        assert res.status == SLAM_STARTED and res.ok
        assert fake.calls("PUT", "/localization")[0][2:] == ({"wait": "false", "partial": "ok"}, {"mode": "slam"})

        res = await switch.save_slam(_plain_robot(), "shed", "sess-1")
        assert res.status == SLAM_SAVED and res.ok and res.notice is None
        post = fake.calls("POST", "/localization/save")[0]
        assert post[2] == {"background": "true"}
        assert post[3] == {"name": ONBOARD, "cloud_map_id": "shed", "cloud_session_id": "sess-1"}
        assert len(fake.calls("GET", "/localization/save")) == 2     # polled until done
        assert fake.intent == {"mode": "relocalization", "map": "lab"}
        assert not fake.deprecated_calls()

    async def test_a_503_start_is_retried(self):
        fake = FakeRobot()
        fake.put_errors = [(503, "the Odin driver is starting")]
        res = await _slam_switch(fake).start_slam(_plain_robot(), "shed")
        assert res.status == SLAM_STARTED, res.warning
        assert len(fake.calls("PUT", "/localization")) == 2

    async def test_a_partial_problem_is_a_failed_start(self):
        fake = FakeRobot()
        fake.put_problem = {"status_code": 504, "detail": "driver started, no status"}
        res = await _slam_switch(fake).start_slam(_plain_robot(), "shed")
        assert res.status == SLAM_FAILED and "driver started, no status" in res.warning

    @pytest.mark.parametrize("prev,target", [
        (None, ("odometry", None)), ({"mode": "slam", "map": None}, ("odometry", None)),
        ({"mode": "relocalization", "map": "lab"}, ("relocalization", "lab")),
        ({"mode": "odometry", "map": "stray"}, ("odometry", None))])
    def test_restore_target(self, prev, target):
        assert oc.restore_target(prev) == target

    async def test_the_default_after_a_save_is_odometry(self):
        fake = FakeRobot()
        switch = _slam_switch(fake)
        await switch.start_slam(_plain_robot(), "shed")
        await switch.save_slam(_plain_robot(), "shed", 1)
        assert fake.intent == {"mode": "odometry", "map": None}

    async def test_a_running_slam_is_not_started_again_and_an_existing_map_is_kept(self):
        fake = FakeRobot(intent={"mode": "slam", "map": None})
        res = await _slam_switch(fake).start_slam(_plain_robot(), "shed")
        assert res.status == SLAM_ALREADY_RUNNING and not fake.calls("PUT", "/localization")
        fake = FakeRobot()
        fake.maps.add(ONBOARD)
        res = await _slam_switch(fake).start_slam(_plain_robot(), "shed")
        assert res.status == SLAM_EXISTS and not fake.calls("PUT", "/localization")

    async def test_the_robots_refusal_is_the_warning(self):
        fake = FakeRobot()
        fake.put_error = (409, "order active: cancel it first")
        res = await _slam_switch(fake).start_slam(_plain_robot(), "shed")
        assert res.status == SLAM_FAILED and not res.ok
        assert "order active: cancel it first" in res.warning

    async def test_a_stopped_driver_is_reported_not_silently_started(self):
        fake = FakeRobot()
        fake.applied = False
        res = await _slam_switch(fake).start_slam(_plain_robot(), "shed")
        assert res.status == SLAM_FAILED and "not started" in res.warning

    async def test_a_failed_save_leaves_the_robot_in_slam(self):
        fake = FakeRobot()
        fake.save_polls = ["saving", "failed"]
        switch = _slam_switch(fake)
        await switch.start_slam(_plain_robot(), "shed")
        res = await switch.save_slam(_plain_robot(), "shed", 1)
        assert res.status == SLAM_FAILED
        assert "lidar_save_map failed" in res.warning and "kept" in res.warning
        assert fake.intent["mode"] == "slam"                  # the unsaved map is not discarded
        assert len(fake.calls("PUT", "/localization")) == 1   # no switch back

    async def test_a_refused_restore_is_reported_on_the_saved_map(self):
        fake = FakeRobot()
        switch = _slam_switch(fake)
        await switch.start_slam(_plain_robot(), "shed")
        orig = fake.handler

        def refuse_put(request):
            if request.method == "PUT":
                return httpx.Response(409, json={"detail": "order active: cancel it first"})
            return orig(request)
        fake.handler = refuse_put
        res = await switch.save_slam(_plain_robot(), "shed", 1)
        assert res.status == SLAM_SAVED and res.ok
        assert "order active: cancel it first" in res.notice
        from packages.api.mapping_switch import slam_save_action
        action = slam_save_action(res)
        assert action["ok"] is False and action["label"].startswith("SLAM map saved, but")

    async def test_a_409_that_is_not_nothing_to_save_fails(self):
        fake = FakeRobot()
        switch = _slam_switch(fake)
        await switch.start_slam(_plain_robot(), "shed")
        fake.save_error = (409, "cloud_map_id 'shed' is held by map 'other'")
        res = await switch.save_slam(_plain_robot(), "shed", 1)
        assert res.status == SLAM_FAILED and "held by map" in res.warning

    async def test_nothing_to_save_when_the_robot_is_not_in_slam(self):
        fake = FakeRobot()
        res = await _slam_switch(fake).save_slam(_plain_robot(), "shed", 1)
        assert res.status == "nothing_to_save" and res.warning is None

    async def test_records_means_the_stored_mode_is_slam(self):
        fake = FakeRobot(intent={"mode": "slam", "map": None})
        assert await _slam_switch(fake).slam_records(_plain_robot(), "shed") is True
        fake.intent = {"mode": "odometry", "map": None}
        assert await _slam_switch(fake).slam_records(_plain_robot(), "shed") is False

    async def test_the_save_outcome_is_the_event(self):
        d = TxDb()
        d.add_map("yard", type="local", status={"state": "draft"})
        d.maps["yard"]["spec"]["slam_map"] = True
        d.robots["r1"] = _plain_robot()
        fake = FakeRobot()
        switch = _slam_switch(fake)
        switch.on_session, switch.on_state = AsyncMock(), AsyncMock()
        with patch.object(maps, "open_store", d.store), patch.object(maps, "_utcnow",
                                                                       m1.Clock()):
            sid = (await maps.start_session(None, "yard", {"robot": "r1"}, m1.PUB, "op",
                                            switch=switch))["session"]["session_id"]
            assert fake.intent["mode"] == "slam"
            await maps.session_action(None, "yard", sid, "finish", m1.PUB, switch=switch)
            await switch.wait_slam_saves()
        assert d.codes()[-1] == "MAP.SLAM_SAVE_DONE"
        assert fake.intent["mode"] == "odometry"


# --- relocalization --------------------------------------------------------------------------------

@pytest.fixture
def renv():
    d = TxDb()
    fake = FakeRobot()
    fake.maps.add(ONBOARD)
    now = [0.0]

    async def sleep(dt):
        now[0] += dt
        e.sleeps += 1
        if e.on_sleep is not None:
            e.on_sleep(e)
        await (e.block.wait() if e.block is not None else asyncio.sleep(0))

    jobs = rj.RelocJobs(client_factory=fake.client, clock=lambda: now[0], sleep=sleep,
                        timeout=90.0, poll=1.0, confirm_timeout=0.0)
    e = Env(d, fake, CapHolder(), jobs, _switch(), now)
    with patch.object(maps, "open_store", d.store), patch.object(maps, "_utcnow", m1.Clock()):
        yield e


def _localized_on(name, after):
    """on_sleep hook: from poll `after` on the robot reports position_initialized and mapId."""
    def hook(e):
        if e.sleeps >= after:
            st = e.robot().status
            st.position_initialized, st.localization_score = True, 0.9
            st.pose.map_id = name
    return hook


class TestRelocFacade:
    async def test_the_vda_state_drives_localized(self, renv):
        _robot(renv.db, position_initialized=True)       # stale true from before
        renv.robot().status.pose.map_id = "map"
        s = _unplaced(renv.db)
        renv.on_sleep = _localized_on(ONBOARD, 3)
        out, job = await renv.run(s["session_id"])
        assert job.state == rj.PLACED, job.error
        put = renv.orch.calls("PUT", "/localization")
        assert [c[2:] for c in put] == [({"wait": "false", "partial": "ok"},
                                         {"mode": "relocalization", "map": ONBOARD})]
        assert renv.sleeps >= 3          # initialized with another mapId did not count
        assert not renv.orch.deprecated_calls()
        assert not renv.orch.calls("GET", "/services")

    async def test_initialized_on_another_map_is_not_localized(self, renv):
        _robot(renv.db)
        s = _unplaced(renv.db)

        def hook(e):
            st = e.robot().status
            st.position_initialized, st.pose.map_id = True, "other-map"
        renv.on_sleep = hook
        _, job = await renv.run(s["session_id"])
        assert job.state == rj.FAILED and "did not report a localized position" in job.error
        assert renv.orch.intent["mode"] == "relocalization"        # timeout: left running

    async def test_an_active_order_is_the_job_error(self, renv):
        _robot(renv.db)
        s = _unplaced(renv.db)
        renv.orch.put_error = (409, "order active: cancel it first")
        _, job = await renv.run(s["session_id"])
        assert job.state == rj.FAILED
        assert "order active: cancel it first" in job.error
        assert len(renv.orch.calls("PUT", "/localization")) == 1     # nothing to put back
        assert renv.orch.intent == {"mode": None, "map": None}

    @pytest.mark.parametrize("status,detail", [
        (502, "Driver refused or failed the switch: MAP_LOAD_FAILED"),
        (503, "VDA5050 state not available"),
        (504, "set_mode did not answer within 30 s"),
    ])
    async def test_other_refusals_fail_the_job_with_the_robots_text(self, renv, status, detail):
        _robot(renv.db)
        s = _unplaced(renv.db)
        renv.orch.put_error = (status, detail)
        _, job = await renv.run(s["session_id"])
        assert job.state == rj.FAILED and detail in job.error
        assert renv.orch.init_pos is None                  # the seed was restored

    async def test_a_slam_intent_is_never_replaced(self, renv):
        _robot(renv.db)
        s = _unplaced(renv.db)
        renv.orch.intent = {"mode": "slam", "map": None}
        _, job = await renv.run(s["session_id"])
        assert job.state == rj.FAILED and "finish it first" in job.error
        assert not renv.orch.calls("PUT", "/localization")

    async def test_stored_but_not_applied_fails_and_is_put_back(self, renv):
        _robot(renv.db)
        s = _unplaced(renv.db)
        renv.orch.applied = False      # no driver runs: stored, nothing would relocalize
        _, job = await renv.run(s["session_id"])
        assert job.state == rj.FAILED and "could not start relocalization" in job.error
        assert renv.orch.intent == {"mode": "odometry", "map": None}

    async def test_a_partial_problem_fails_the_job(self, renv):
        _robot(renv.db)
        s = _unplaced(renv.db)
        renv.orch.put_problem = {"status_code": 504, "detail": "driver started, no status"}
        _, job = await renv.run(s["session_id"])
        assert job.state == rj.FAILED and "driver started, no status" in job.error

    ASSISTED = {"source": "reloc", "reloc": {"init_pose": {"x": 1.0, "y": 2.0, "yaw": 0.0}}}

    def _put_bodies(self, renv):
        return [c[3] for c in renv.orch.calls("PUT", "/localization")]

    async def test_assisted_on_the_current_map_passes_through_odometry(self, renv):
        _robot(renv.db)
        s = _unplaced(renv.db)
        renv.orch.intent = {"mode": "relocalization", "map": ONBOARD}
        renv.on_sleep = _localized_on(ONBOARD, 2)
        _, job = await renv.run(s["session_id"], self.ASSISTED)
        assert job.state == rj.PLACED, job.error
        assert self._put_bodies(renv) == [{"mode": "odometry"},
                                          {"mode": "relocalization", "map": ONBOARD}]

    async def test_assisted_on_another_map_or_odin_mode_is_one_switch(self, renv):
        _robot(renv.db)
        s = _unplaced(renv.db)
        renv.orch.intent = {"mode": "relocalization", "map": ONBOARD}
        renv.on_sleep = _localized_on(ONBOARD, 2)
        _, job = await renv.run(s["session_id"])          # odin mode: the robot is already on it
        assert job.state == rj.PLACED, job.error
        assert self._put_bodies(renv) == [{"mode": "relocalization", "map": ONBOARD}]

    async def test_a_failed_pass_through_restores_the_previous_map(self, renv):
        _robot(renv.db)
        s = _unplaced(renv.db)
        renv.orch.intent = {"mode": "relocalization", "map": ONBOARD}
        # odometry succeeds, the relocalization PUT is refused
        calls = []
        handler = renv.orch.handler

        def second_put_refused(request):
            if request.method == "PUT" and request.url.path == "/localization":
                calls.append(1)
                if len(calls) == 2:
                    return httpx.Response(409, json={"detail": "order active"})
            return handler(request)
        renv.orch.handler = second_put_refused
        _, job = await renv.run(s["session_id"], self.ASSISTED)
        assert job.state == rj.FAILED and "order active" in job.error
        # the job left odometry: the rollback puts the map it relocalized on before back
        assert renv.orch.intent == {"mode": "relocalization", "map": ONBOARD}

    async def test_cancel_puts_the_previous_intent_back(self, renv):
        _robot(renv.db)
        s = _unplaced(renv.db)
        renv.orch.intent = {"mode": "relocalization", "map": "older"}
        renv.block = asyncio.Event()
        await renv.place(s["session_id"])
        for _ in range(20):
            await asyncio.sleep(0)
        job = renv.jobs.active_for("r1")
        assert job.state == rj.WAITING
        assert renv.orch.intent == {"mode": "relocalization", "map": ONBOARD}
        await renv.jobs.cancel(job)
        assert job.state == rj.CANCELLED
        assert renv.orch.intent == {"mode": "relocalization", "map": "older"}

    async def test_cancel_with_no_previous_mode_goes_to_odometry(self, renv):
        _robot(renv.db)
        s = _unplaced(renv.db)
        renv.block = asyncio.Event()
        await renv.place(s["session_id"])
        for _ in range(20):
            await asyncio.sleep(0)
        await renv.jobs.cancel(renv.jobs.active_for("r1"))
        assert renv.orch.intent == {"mode": "odometry", "map": None}

    async def test_a_changed_intent_is_not_overwritten_by_the_rollback(self, renv):
        _robot(renv.db)
        s = _unplaced(renv.db)
        renv.block = asyncio.Event()
        await renv.place(s["session_id"])
        for _ in range(20):
            await asyncio.sleep(0)
        renv.orch.intent = {"mode": "slam", "map": None}     # someone else switched meanwhile
        await renv.jobs.cancel(renv.jobs.active_for("r1"))
        assert renv.orch.intent == {"mode": "slam", "map": None}
        assert len(renv.orch.calls("PUT", "/localization")) == 1

# --- the proxy ---------------------------------------------------------------------------------------

class TestProxyIds:
    SESSION = {"purpose": "mapping", "map_name": "shed", "session_id": "s1"}

    def test_the_facade_save_of_the_sessions_own_map_gets_the_cloud_ids(self):
        body = json.dumps({"name": ONBOARD}).encode()
        out = json.loads(proxy.with_cloud_ids("POST", "localization/save", body, self.SESSION))
        assert out == {"name": ONBOARD, "cloud_map_id": "shed", "cloud_session_id": "s1"}

    def test_another_map_is_left_alone(self):
        body = json.dumps({"name": "other"}).encode()
        assert proxy.with_cloud_ids("POST", "localization/save", body, self.SESSION) == body

    def test_the_deprecated_save_route_gets_no_ids(self):
        body = json.dumps({}).encode()
        assert proxy.with_cloud_ids("POST", f"maps/{ONBOARD}/save", body, self.SESSION) == body


# --- the topomap on the mapping API --------------------------------------------------------------

class TestTopomapOnTheMappingApi:
    async def test_the_view_is_what_the_robot_reports(self):
        fake = FakeRobot(intent={"mode": "slam", "map": None}, topomap=True)
        snap = await _slam_switch(fake).snapshot(_plain_robot(), fresh=True)
        assert snap.mapping_services() == {"topo": "running", "grid": "not_available",
                                           "slam": "running"}
        assert snap.state(None)["status"] == "on"
        assert not [c for c in fake.log if c[1].startswith("/services/topomap")]

    async def test_the_snapshot_keeps_the_body_and_cached_is_dropped_by_invalidate(self):
        fake = FakeRobot(intent={"mode": "slam", "map": None}, topomap=False)
        switch = _slam_switch(fake)
        assert switch.cached("r1") is None
        snap = await switch.snapshot(_plain_robot(), fresh=True)
        assert snap.localization == {"mode": "slam", "map": None, "topomap": False}
        assert switch.cached("r1") is snap
        switch.invalidate("r1")   # the pre-change snapshot is not served to the WS any more
        assert switch.cached("r1") is None

    async def test_a_facade_without_it_reports_slam_and_the_topomap_service(self):
        snap = await _slam_switch(FakeRobot()).snapshot(_plain_robot(), fresh=True)
        assert snap.mapping_services() == {"topo": "not_running", "grid": "not_available",
                                           "slam": "not_running"}

    async def test_a_slam_session_switches_slam_then_the_topomap_and_back(self):
        fake = FakeRobot(intent={"mode": "odometry", "map": None}, topomap=False)
        d, switch = _session_env(fake)
        with patch.object(maps, "open_store", d.store), patch.object(maps, "_utcnow",
                                                                       m1.Clock()):
            out = await maps.start_session(None, "yard", {"robot": "r1"}, m1.PUB, "op",
                                           switch=switch)
            assert _puts(fake) == [{"mode": "slam"}, {"mode": "slam", "topomap": True}]
            assert _labels(out) == [("SLAM recording", "start", True),
                                    ("topomap", "start", True)]
            assert out["mapping_services"]["topo"] == "running"
            sid = out["session"]["session_id"]
            out = await maps.session_action(None, "yard", sid, "finish", m1.PUB, switch=switch)
            await switch.wait_slam_saves()
        # the topomap stops before the save; the save switches back to odometry
        assert _puts(fake)[2:] == [{"mode": "slam", "topomap": False},
                                   {"mode": "odometry", "topomap": False}]
        assert fake.topomap is False and fake.intent["mode"] == "odometry"
        assert d.codes()[-1] == "MAP.SLAM_SAVE_DONE"
        assert not [c for c in fake.log if c[1].startswith("/services/topomap")]
        assert not fake.deprecated_calls()

    async def test_a_topomap_not_started_after_the_switch_is_a_failed_action(self):
        fake = FakeRobot(intent={"mode": "odometry", "map": None}, topomap=False)
        fake.put_problem = {"status_code": 409, "detail": "device reports RELOCALIZING"}
        loc = {"mode": "odometry", "map": None, "topomap": False}
        action = await MappingSwitch._switch_topomap(fake.client(_plain_robot()), loc, True)
        assert not action["ok"] and "RELOCALIZING" in action["detail"]

    async def test_a_relocalized_robot_keeps_its_map(self):
        fake = FakeRobot(intent={"mode": "relocalization", "map": "cloud-yard"}, topomap=False)
        d, switch = _session_env(fake, slam_map=False)
        with patch.object(maps, "open_store", d.store), patch.object(maps, "_utcnow",
                                                                       m1.Clock()):
            out = await maps.start_session(None, "yard", {"robot": "r1"}, m1.PUB, "op",
                                           switch=switch)
        assert _puts(fake) == [{"mode": "relocalization", "map": "cloud-yard", "topomap": True}]
        assert _labels(out) == [("topomap", "start", True)]

    async def test_odometry_is_switched_too_and_a_refusal_never_refuses_the_session(self):
        fake = FakeRobot(intent={"mode": "odometry", "map": None}, topomap=False)
        d, switch = _session_env(fake, slam_map=False)
        with patch.object(maps, "open_store", d.store), patch.object(maps, "_utcnow",
                                                                       m1.Clock()):
            out = await maps.start_session(None, "yard", {"robot": "r1"}, m1.PUB, "op",
                                           switch=switch)
        assert _puts(fake) == [{"mode": "odometry", "topomap": True}]
        assert _labels(out) == [("topomap", "start", True)] and fake.topomap is True

        fake = FakeRobot(intent={"mode": "odometry", "map": None}, topomap=False)
        fake.put_error = (422, "topomap needs slam or relocalization")   # an older robot
        d, switch = _session_env(fake, slam_map=False)
        with patch.object(maps, "open_store", d.store), patch.object(maps, "_utcnow",
                                                                       m1.Clock()):
            out = await maps.start_session(None, "yard", {"robot": "r1"}, m1.PUB, "op",
                                           switch=switch)
        assert _labels(out) == [("topomap", "start", False)]
        assert "topomap needs slam" in out["robot_actions"][0]["detail"]
        assert out["session"]["ended_at"] is None

    async def test_the_robots_refusal_is_the_detail(self):
        fake = FakeRobot(intent={"mode": "slam", "map": None}, topomap=False)
        fake.put_error = (409, "navstack not running")
        actions = await _slam_switch(fake).start(_plain_robot(), ["topo"])
        assert [(a["ok"], a["detail"]) for a in actions] == [
            (False, "the orchestrator answered 409: navstack not running")]

    async def test_already_in_the_wanted_state_is_ok_without_a_call(self):
        fake = FakeRobot(intent={"mode": "slam", "map": None}, topomap=False)
        actions = await _slam_switch(fake).stop(_plain_robot(), ["topo"])
        assert actions[0]["ok"] and actions[0]["label"] == "Topomap service was not running"
        assert not _puts(fake)
