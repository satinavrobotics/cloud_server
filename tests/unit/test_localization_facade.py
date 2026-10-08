"""The cloud's robot localization calls on the orchestrator's localization facade
(GET/PUT /localization, POST/GET /localization/save) with a fallback to the deprecated /maps/...
calls of older robots: packages/api/orchestrator_client.py (probe), mapping_switch.py (SLAM),
reloc_job.py (relocalization), orchestrator_maps.py (capability), orchestrator_proxy.py.

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


class FakeRobot:
    """One robot's orchestrator over HTTP. `facade` False: an older orchestrator (404 on
    /localization, the deprecated /maps/... calls instead)."""

    def __init__(self, facade=True, intent=None):
        self.facade = facade
        self.intent = dict(intent or {"mode": None, "map": None})
        self.log = []                 # (method, path, query, body)
        self.put_error = None         # (status, detail) for the next PUT /localization
        self.applied = True
        self.save_error = None        # (status, detail) for the POST save
        self.save_polls = ["saving", "done"]    # what successive GET /localization/save say
        self.save_failure = "lidar_save_map failed"
        self.maps = set()             # stored map names
        self.init_pos = None
        self.old_slam = {"active": False, "map": None}
        self.old_calls = []
        self.save_started = False

    def calls(self, method, path):
        return [c for c in self.log if c[0] == method and c[1] == path]

    def handler(self, request):
        path, method = request.url.path, request.method
        body = json.loads(request.content) if request.content else None
        query = dict(request.url.params)
        self.log.append((method, path, query, body))
        R = httpx.Response
        if path == "/localization":
            if not self.facade:
                return R(404, json={"detail": "Not Found"})
            if method == "GET":
                return R(200, json=self.intent)
            if self.put_error:
                status, detail = self.put_error
                return R(status, json={"detail": detail})
            self.intent = {"mode": body["mode"], "map": body.get("map")}
            return R(200, json={**self.intent, "applied": self.applied, "localized": None,
                                "message": "ok"})
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
        if path == "/maps/mapping" and method == "GET":
            return R(200, json={"active": self.old_slam["active"], "map": self.old_slam["map"],
                                "saving": False, "late_save_sec": 0})
        if path == "/maps/mapping/save":
            return R(200, json={"map": ONBOARD, "status": "done", "error": None})
        if path.endswith("/mapping/start"):
            self.old_calls.append(("start", path))
            self.old_slam = {"active": True, "map": path.split("/")[2]}
            return R(200, json={"started": True})
        if path.endswith("/save"):
            self.old_calls.append(("save", path))
            return R(202, json={"started": True, "map": path.split("/")[2]})
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


@pytest.fixture(autouse=True)
def _fresh_probe_cache():
    oc.clear_facade_cache()
    yield
    oc.clear_facade_cache()


def _plain_robot():
    return RobotObjectV1(name="r1", ip_address="10.0.0.5", entrypoint_port=8080,
                         status=RobotStatusV1(online=True))


async def _no_sleep(_s):
    await asyncio.sleep(0)


def _slam_switch(fake):
    return MappingSwitch(client_factory=fake.client, sleep=_no_sleep)


# --- capability probe ----------------------------------------------------------------------------

class TestProbe:
    async def test_facade_robot_is_probed_once_and_cached(self):
        fake = FakeRobot()
        client = fake.client(_plain_robot())
        assert await client.facade() is True
        assert await client.facade() is True
        assert len(fake.calls("GET", "/localization")) == 1

    async def test_404_is_an_older_robot(self):
        fake = FakeRobot(facade=False)
        assert await fake.client(_plain_robot()).facade() is False
        assert await oc.facade_available(fake.client(_plain_robot())) is False

    async def test_unreachable_is_unknown_and_not_cached(self):
        def boom(request):
            raise httpx.ConnectError("down")
        client = oc.OrchestratorClient(
            _plain_robot(), http_factory=lambda timeout: httpx.AsyncClient(
                transport=httpx.MockTransport(boom), timeout=timeout))
        assert await client.facade() is None
        assert await oc.facade_available(client) is False

    async def test_a_404_from_a_facade_call_forces_a_new_probe(self):
        fake = FakeRobot()
        client = fake.client(_plain_robot())
        assert await client.facade() is True
        fake.facade = False        # the robot was downgraded
        with pytest.raises(oc.OrchestratorError):
            await client.get_localization()
        assert await client.facade() is False

    async def test_a_client_without_the_probe_is_an_older_robot(self):
        assert await oc.facade_available(object()) is False

    async def test_reloc_capability_needs_no_service_on_a_facade_robot(self):
        fake = FakeRobot()
        fake.maps.add(ONBOARD)
        holder = OrchestratorMaps(client_factory=fake.client)
        can, why = await holder.reloc_capability(_plain_robot(), "shed")
        assert (can, why) == (True, None)
        assert not fake.calls("GET", "/services")


# --- the SLAM recording ----------------------------------------------------------------------------

class TestSlam:
    async def test_start_is_a_mode_switch_and_the_save_restores_the_previous_intent(self):
        fake = FakeRobot(intent={"mode": "relocalization", "map": "lab"})
        switch = _slam_switch(fake)
        res = await switch.start_slam(_plain_robot(), "shed")
        assert res.status == SLAM_STARTED and res.ok
        assert fake.calls("PUT", "/localization")[0][2:] == ({"wait": "false"}, {"mode": "slam"})
        assert not fake.old_calls

        res = await switch.save_slam(_plain_robot(), "shed", "sess-1")
        assert res.status == SLAM_SAVED and res.ok and res.notice is None
        post = fake.calls("POST", "/localization/save")[0]
        assert post[2] == {"background": "true"}
        assert post[3] == {"name": ONBOARD, "cloud_map_id": "shed", "cloud_session_id": "sess-1"}
        assert len(fake.calls("GET", "/localization/save")) == 2     # polled until done
        assert fake.intent == {"mode": "relocalization", "map": "lab"}
        assert not fake.old_calls

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

    async def test_an_older_robot_keeps_the_old_calls(self):
        fake = FakeRobot(facade=False)
        switch = _slam_switch(fake)
        res = await switch.start_slam(_plain_robot(), "shed")
        assert res.status == SLAM_STARTED
        assert fake.old_calls == [("start", f"/maps/{ONBOARD}/mapping/start")]
        fake.old_slam = {"active": True, "map": ONBOARD}
        res = await switch.save_slam(_plain_robot(), "shed", 1)
        assert res.status == SLAM_SAVED
        assert ("save", f"/maps/{ONBOARD}/save") in fake.old_calls
        assert not fake.calls("PUT", "/localization")

    async def test_the_orphan_stop_and_reconcile_leave_a_facade_robot_alone(self):
        fake = FakeRobot(intent={"mode": "slam", "map": None})
        switch = _slam_switch(fake)
        assert await switch.stop_orphan_slam(None, _plain_robot(), "gone") is False
        assert await switch._reconcile_slam(None, _plain_robot()) is False
        assert not fake.calls("PUT", "/localization") and not fake.calls("POST",
                                                                          "/maps/mapping/stop")

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
                        timeout=90.0, poll=1.0, settle=5.0, confirm_timeout=0.0,
                        candidates=["odin_reloc"])
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
        assert [c[2:] for c in put] == [({"wait": "false"},
                                         {"mode": "relocalization", "map": ONBOARD})]
        assert renv.sleeps >= 3          # initialized with another mapId did not count
        assert not renv.orch.calls("GET", "/maps/mapping")
        assert not renv.orch.calls("POST", f"/maps/{ONBOARD}/relocalize")

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

    async def test_an_older_robot_uses_the_old_path(self, renv):
        renv.orch.facade = False
        _robot(renv.db)
        s = _unplaced(renv.db)
        _, job = await renv.run(s["session_id"])
        # no facade, no relocalize endpoint, no service: the old path's own error
        assert job.state == rj.FAILED
        assert not renv.orch.calls("PUT", "/localization")
        assert renv.orch.calls("GET", "/services") or renv.orch.calls("GET", "/maps/mapping")


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
