"""The mapping switch through the robot's orchestrator (docs/satinav-maps-redesign.md §15).

- packages/api/orchestrator_client.py: the orchestrator's routes, error kinds, the address;
- packages/api/mapping_switch.py: service name resolution (`topomap` real / `sim_topomap` sim),
  start (order, already running, failure -> HTTP errors, rollback of a partial start), stop
  (best effort, offline), the state views and their cache;
- packages/api/maps.py: opening a session starts the services inside its transaction (a failed
  start leaves nothing behind), pause / finish stop them after the commit (a failed stop still
  closes the session), resume starts them again, replace keeps what the new session runs;
- the routes and the robot view fill `mapping_state` / `mapping_services` from the orchestrator.

The in-memory store is the M1 one with robot locks (tests/unit/test_maps_m2.py ShimDb).
"""
import asyncio
import json
import os
from types import SimpleNamespace

for _k in ("ARANGO_PASSWORD", "MINIO_ACCESS_KEY", "MINIO_SECRET_KEY", "POSTGRES_PASSWORD"):
    os.environ.setdefault(_k, "test")

from unittest.mock import AsyncMock, MagicMock, patch  # noqa: E402

import httpx  # noqa: E402
import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402

import packages.api.main as main  # noqa: E402
from packages.api import maps  # noqa: E402
from packages.api import orchestrator_client as oc  # noqa: E402
from packages.api.mapping_switch import MappingSwitch, Snapshot  # noqa: E402
from tests.unit import test_maps_m1 as m1  # noqa: E402
from tests.unit.test_maps_m2 import ShimDb  # noqa: E402

pytestmark = pytest.mark.unit


# --- fakes -------------------------------------------------------------------------------------

class FakeOrch:
    """One robot's orchestrator: the services it lists and which run; every call is recorded."""

    def __init__(self, services=("topomap",), running=(), reachable=True, fail=None):
        self.services = {n: n in running for n in services}
        self.reachable = reachable
        self.fail = fail or {}        # (op, name) -> OrchestratorError
        self.calls = []

    def _enter(self, op, name=None):
        self.calls.append((op, name))
        if not self.reachable:
            raise oc.OrchestratorError(oc.UNREACHABLE, "orchestrator at 10.0.0.5:8080 is not "
                                                       "reachable (ConnectError)")
        if (op, name) in self.fail:
            raise self.fail[(op, name)]


class FakeClient:
    def __init__(self, orch):
        self.orch = orch

    async def list_services(self):
        self.orch._enter("list")
        return [{"name": n, "running": r} for n, r in self.orch.services.items()]

    async def status(self, name):
        self.orch._enter("status", name)
        if name not in self.orch.services:
            raise oc.OrchestratorError(oc.HTTP, "not found", status=404)
        return {"name": name, "state": {"running": self.orch.services[name], "pid": 42,
                                        "started_at": "2026-09-30T10:00:00Z"}}

    async def start(self, name):
        self.orch._enter("start", name)
        if name not in self.orch.services:
            raise oc.OrchestratorError(oc.HTTP, f"Service '{name}' not found", status=404)
        if self.orch.services[name]:
            raise oc.OrchestratorError(oc.HTTP, "already running", status=409)
        self.orch.services[name] = True
        return {"success": True}

    async def stop(self, name):
        self.orch._enter("stop", name)
        if not self.orch.services.get(name):
            raise oc.OrchestratorError(oc.HTTP, "not currently running", status=404)
        self.orch.services[name] = False
        return {"success": True}


def make_switch(orchs, **kw):
    """A MappingSwitch whose robots' orchestrators are `orchs` {robot name: FakeOrch}."""
    return MappingSwitch(client_factory=lambda robot: FakeClient(orchs[robot.name]), **kw)


def robot(name="r1", online=True, address=True):
    from cloud_common.objects.robot import RobotObjectV1, RobotStatusV1
    extra = {"ip_address": "10.0.0.5", "entrypoint_port": 8080} if address else {}
    return RobotObjectV1(name=name, status=RobotStatusV1(online=online), **extra)


def add_robot(db, name="r1", online=True, address=True):
    db.robots[name] = robot(name, online, address)


@pytest.fixture
def db():
    d = ShimDb()
    with patch.object(maps, "open_store", d.store), patch.object(maps, "_utcnow", m1.Clock()):
        yield d


def ops(orch, *kinds):
    return [c for c in orch.calls if c[0] in kinds]


# --- the client --------------------------------------------------------------------------------

class TestClient:
    def _client(self, handler):
        def factory(timeout):
            return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=timeout)
        return oc.OrchestratorClient(robot(), http_factory=factory)

    async def test_routes(self):
        seen = []

        def handler(request):
            seen.append((request.method, request.url.path, request.url.host, request.url.port))
            return httpx.Response(200, json=[{"name": "topomap"}] if request.url.path == "/services"
                                  else {"success": True})

        client = self._client(handler)
        assert await client.list_services() == [{"name": "topomap"}]
        await client.status("topomap")
        await client.start("topomap")
        await client.stop("topomap")
        assert seen == [("GET", "/services", "10.0.0.5", 8080),
                        ("GET", "/services/topomap/status", "10.0.0.5", 8080),
                        ("POST", "/services/topomap/start", "10.0.0.5", 8080),
                        ("POST", "/services/topomap/stop", "10.0.0.5", 8080)]

    async def test_http_error_carries_status_and_detail(self):
        client = self._client(lambda r: httpx.Response(409, json={"detail": "already running"}))
        with pytest.raises(oc.OrchestratorError) as err:
            await client.start("topomap")
        assert (err.value.kind, err.value.status, err.value.detail) == (
            oc.HTTP, 409, "already running")

    async def test_unreachable_and_timeout(self):
        def refuse(request):
            raise httpx.ConnectError("refused")

        def slow(request):
            raise httpx.ReadTimeout("slow")

        with pytest.raises(oc.OrchestratorError) as err:
            await self._client(refuse).list_services()
        assert err.value.kind == oc.UNREACHABLE and "10.0.0.5:8080" in err.value.detail
        with pytest.raises(oc.OrchestratorError) as err:
            await self._client(slow).start("topomap")
        assert err.value.kind == oc.TIMEOUT

    async def test_no_address(self):
        client = oc.OrchestratorClient(robot(address=False))
        assert oc.orchestrator_address(robot(address=False)) is None
        assert oc.orchestrator_address(robot()) == ("10.0.0.5", 8080)
        with pytest.raises(oc.OrchestratorError) as err:
            await client.list_services()
        assert err.value.kind == oc.NO_ADDRESS


# --- name resolution -----------------------------------------------------------------------------

class TestResolution:
    @pytest.mark.parametrize("listed,expected", [
        (["topomap", "other"], "topomap"),          # the real robot
        (["sim_topomap", "other"], "sim_topomap"),  # the sim
        (["sim_topomap", "topomap"], "topomap"),    # candidate order wins
    ])
    async def test_first_candidate_the_orchestrator_lists(self, listed, expected):
        orch = FakeOrch(services=listed)
        switch = make_switch({"r1": orch})
        assert await switch.start(robot(), ["topo"]) == {"topo": "started"}
        assert ops(orch, "start") == [("start", expected)]

    async def test_no_candidate_listed_is_a_409_naming_the_candidates(self):
        switch = make_switch({"r1": FakeOrch(services=["something"])})
        with pytest.raises(HTTPException) as err:
            await switch.start(robot(), ["topo"])
        assert err.value.status_code == 409
        assert "topomap, sim_topomap" in err.value.detail and "'topo'" in err.value.detail

    def test_candidates_come_from_config(self):
        from packages import config
        assert config.MAPPING_SERVICE_CANDIDATES["topo"][:2] == ["topomap", "sim_topomap"]


# --- start ---------------------------------------------------------------------------------------

class TestStart:
    async def test_lists_then_starts(self):
        orch = FakeOrch()
        switch = make_switch({"r1": orch})
        assert await switch.start(robot(), ["topo"]) == {"topo": "started"}
        assert orch.calls == [("list", None), ("start", "topomap")]
        assert orch.services["topomap"] is True

    async def test_already_running_is_fine(self):
        orch = FakeOrch(running=["topomap"])
        assert await make_switch({"r1": orch}).start(robot(), ["topo"]) == {
            "topo": "already_running"}

    @pytest.mark.parametrize("error,status,words", [
        (oc.OrchestratorError(oc.UNREACHABLE, "orchestrator at 10.0.0.5:8080 is not reachable "
                                              "(ConnectError)"), 502, "not reachable"),
        (oc.OrchestratorError(oc.TIMEOUT, "orchestrator at 10.0.0.5:8080 timed out"), 504,
         "timed out"),
        (oc.OrchestratorError(oc.HTTP, "entrypoint failed", status=500), 502,
         "answered 500: entrypoint failed"),
        (oc.OrchestratorError(oc.HTTP, "Service 'topomap' not found", status=404), 502,
         "answered 404"),
    ])
    async def test_failed_start_is_a_clear_http_error(self, error, status, words):
        orch = FakeOrch(fail={("start", "topomap"): error})
        with pytest.raises(HTTPException) as err:
            await make_switch({"r1": orch}).start(robot(), ["topo"])
        assert err.value.status_code == status
        assert "Could not start mapping service 'topo' on robot 'r1'" in err.value.detail
        assert words in err.value.detail

    async def test_unreachable_before_anything_started(self):
        orch = FakeOrch(reachable=False)
        with pytest.raises(HTTPException) as err:
            await make_switch({"r1": orch}).start(robot(), ["topo"])
        assert err.value.status_code == 502 and ops(orch, "start") == []

    async def test_no_registered_address(self):
        # the real client refuses without an address
        switch = MappingSwitch()
        with pytest.raises(HTTPException) as err:
            await switch.start(robot(address=False), ["topo"])
        assert err.value.status_code == 502 and "no registered orchestrator" in err.value.detail

    async def test_a_partial_start_is_stopped_again(self):
        orch = FakeOrch(services=["topomap", "grid"],
                        fail={("start", "grid"): oc.OrchestratorError(oc.HTTP, "boom", status=500)})
        with pytest.raises(HTTPException):
            await make_switch({"r1": orch}).start(robot(), ["topo", "grid"])
        assert ops(orch, "start", "stop") == [("start", "topomap"), ("start", "grid"),
                                              ("stop", "topomap")]
        assert orch.services["topomap"] is False


# --- stop ----------------------------------------------------------------------------------------

class TestStop:
    async def test_stops(self):
        orch = FakeOrch(running=["topomap"])
        result = await make_switch({"r1": orch}).stop(robot(), ["topo"])
        assert result.ok and result.services == {"topo": "stopped"}
        assert orch.services["topomap"] is False

    async def test_not_running_is_fine(self):
        result = await make_switch({"r1": FakeOrch()}).stop(robot(), ["topo"])
        assert result.ok and result.services == {"topo": "already_stopped"}

    async def test_offline_robot_is_a_warning_never_an_exception(self):
        result = await make_switch({"r1": FakeOrch(reachable=False)}).stop(robot(), ["topo"])
        assert not result.ok and result.services == {"topo": "failed"}
        assert "'r1'" in result.warning and "not reachable" in result.warning

    async def test_a_failed_stop_is_a_warning(self):
        orch = FakeOrch(running=["topomap"], fail={
            ("stop", "topomap"): oc.OrchestratorError(oc.HTTP, "kill failed", status=500)})
        result = await make_switch({"r1": orch}).stop(robot(), ["topo"])
        assert not result.ok and "kill failed" in result.warning

    async def test_no_address(self):
        result = await MappingSwitch().stop(robot(address=False), ["topo"])
        assert not result.ok and "no registered orchestrator" in result.warning


# --- state ---------------------------------------------------------------------------------------

def open_session(**kw):
    return {"session_id": "s1", "map": "yard", "purpose": "mapping", "state": "mapping",
            "aligned": True, **kw}


class TestState:
    async def snap(self, orch, **robot_kw):
        return await make_switch({"r1": orch}).snapshot(robot(**robot_kw))

    async def test_running_and_capturing_is_on(self):
        snap = await self.snap(FakeOrch(running=["topomap"]))
        state = snap.state(open_session(node_count=7))
        assert state["status"] == "on" and state["online"] is True and state["enabled"] is True
        assert state["service"] == "topo" and state["session_id"] == "s1"
        assert state["map"] == "yard" and state["nodes_sent"] == 7
        assert state["since"] == "2026-09-30T10:00:00Z" and state["source"] == "orchestrator"
        assert state["orchestrator_service"] == "topomap"
        assert {"stamp", "received_at"} <= set(state)
        assert snap.mapping_service() == "running"
        assert snap.mapping_services() == {"topo": "running", "grid": "not_available"}

    async def test_running_but_paused_or_unplaced_is_off(self):
        snap = await self.snap(FakeOrch(running=["topomap"]))
        assert snap.state(open_session(state="paused"))["status"] == "off"
        assert snap.state(open_session(aligned=False))["status"] == "off"
        assert snap.state(open_session(state="paused"))["online"] is True

    async def test_stopped_service(self):
        snap = await self.snap(FakeOrch())
        state = snap.state(open_session())
        assert state["status"] == "off" and state["online"] is False and state["since"] is None
        assert snap.mapping_service() == "not_running"
        assert snap.mapping_services()["topo"] == "not_running"

    async def test_no_session_reports_the_service_only(self):
        state = (await self.snap(FakeOrch(running=["topomap"]))).state(None)
        assert state["status"] == "on" and state["session_id"] is None and state["map"] is None

    async def test_an_operate_session_is_not_a_mapping_session(self):
        state = (await self.snap(FakeOrch())).state(open_session(purpose="operate"))
        assert state["session_id"] is None

    async def test_unreachable_orchestrator(self):
        snap = await self.snap(FakeOrch(reachable=False))
        state = snap.state(open_session())
        assert state["status"] == "unreachable" and state["online"] is False
        assert "not reachable" in state["error"]
        assert snap.mapping_services() == {"topo": "not_available", "grid": "not_available"}
        assert snap.mapping_service() == "not_running"

    async def test_no_topo_service_on_the_robot(self):
        snap = await self.snap(FakeOrch(services=["something"]))
        assert snap.state(open_session()) is None
        assert snap.mapping_services()["topo"] == "not_available"

    async def test_grid_service_is_reported(self):
        snap = await self.snap(FakeOrch(services=["topomap", "grid"], running=["grid"]))
        assert snap.mapping_services() == {"topo": "not_running", "grid": "running"}

    async def test_offline_robot_and_no_address_are_not_asked(self):
        orch = FakeOrch()
        switch = make_switch({"r1": orch})
        for kw in ({"online": False}, {"address": False}):
            snap = await switch.snapshot(robot(**kw), fresh=True)
            assert snap.reachable is None and snap.state(open_session()) is None
            assert snap.mapping_services() == {"topo": "not_available", "grid": "not_available"}
        assert orch.calls == []

    async def test_cached_for_the_ttl_and_dropped_by_a_switch(self):
        now = [0.0]
        orch = FakeOrch()
        switch = make_switch({"r1": orch}, ttl=5.0, clock=lambda: now[0])
        r = robot()
        first = await switch.snapshot(r)
        assert await switch.snapshot(r) is first and len(ops(orch, "list")) == 1
        now[0] = 6.0
        assert await switch.snapshot(r) is not first and len(ops(orch, "list")) == 2
        await switch.start(r, ["topo"])          # a switch invalidates the cache
        assert (await switch.snapshot(r)).mapping_service() == "running"
        again = await switch.snapshot(r, fresh=True)
        assert again is not await switch.snapshot(r, fresh=True)

    async def test_snapshots_of_many_robots(self):
        orchs = {"a": FakeOrch(running=["topomap"]), "b": FakeOrch()}
        found = await make_switch(orchs).snapshots([robot("a"), robot("b")])
        assert found["a"].mapping_service() == "running"
        assert found["b"].mapping_service() == "not_running"

    def test_the_view_is_json(self):
        json.dumps(Snapshot(reachable=True, services={"topo": {
            "orchestrator": "topomap", "running": True, "pid": 1,
            "started_at": None}}).state(open_session()))


# --- maps: the session's transaction and the switch ---------------------------------------------------

def prepare(db, orch=None, **switch_kw):
    db.add_map("yard", type="local", status={"state": "draft"})
    add_robot(db)
    orch = orch or FakeOrch()
    switch = make_switch({"r1": orch}, **switch_kw)
    switch.on_session = AsyncMock()
    switch.on_state = AsyncMock()
    return orch, switch


async def start(db, switch, **body):
    return await maps.start_session(None, "yard", {"robot": "r1", **body}, m1.PUB, "op",
                                    switch=switch)


class TestStartSession:
    async def test_opening_starts_the_service_after_the_session_exists(self, db):
        orch, switch = prepare(db)
        out = await start(db, switch)
        assert orch.services["topomap"] is True and db.open_session("r1")
        assert out["robot_notified"] is True
        assert out["mapping_switch"] == {"topo": "started"}
        assert out["mapping_service"] == "running"
        assert out["mapping_services"] == {"topo": "running", "grid": "not_available"}
        assert out["mapping_state"]["service"] == "topo"
        assert out["mapping_state"]["session_id"] == out["session"]["session_id"]
        assert out["mapping_state"]["status"] == "on"
        assert "mapping_warning" not in out
        assert db.codes() == ["MAP.SESSION_STARTED"]
        switch.on_session.assert_awaited_once()
        switch.on_state.assert_awaited_once()

    @pytest.mark.parametrize("orch,status", [
        (FakeOrch(reachable=False), 502),
        (FakeOrch(services=["nothing"]), 409),
        (FakeOrch(fail={("start", "topomap"): oc.OrchestratorError(oc.TIMEOUT, "timed out")}),
         504),
        (FakeOrch(fail={("start", "topomap"): oc.OrchestratorError(oc.HTTP, "x", status=500)}),
         502),
    ])
    async def test_a_failed_start_leaves_no_session(self, db, orch, status):
        _, switch = prepare(db, orch)
        with pytest.raises(HTTPException) as err:
            await start(db, switch)
        assert err.value.status_code == status
        assert "Could not start mapping service 'topo'" in err.value.detail
        assert db.open_session("r1") == [] and db.events == []
        assert db.maps["yard"]["status"]["state"] == "draft"
        switch.on_session.assert_not_awaited()

    async def test_no_registered_orchestrator_is_a_502(self, db):
        db.add_map("yard", type="local", status={"state": "draft"})
        add_robot(db, address=False)
        with pytest.raises(HTTPException) as err:
            await maps.start_session(None, "yard", {"robot": "r1"}, m1.PUB, switch=MappingSwitch())
        assert err.value.status_code == 502 and db.open_session("r1") == []

    async def test_operate_session_touches_no_service(self, db):
        db.add_map("yard", type="local", status={"state": "ready"})
        add_robot(db)
        orch = FakeOrch()
        switch = make_switch({"r1": orch})
        out = await maps.start_session(None, "yard", {"robot": "r1", "purpose": "operate"},
                                       m1.PUB, switch=switch)
        assert ops(orch, "start", "stop") == [] and "mapping_switch" not in out
        assert out["robot_notified"] is True

    async def test_without_a_switch_the_response_is_unchanged(self, db):
        db.add_map("yard", type="local", status={"state": "draft"})
        add_robot(db)
        out = await maps.start_session(None, "yard", {"robot": "r1"}, m1.PUB)
        assert set(out) == {"map_id", "map_state", "changed", "session", "replaced_session"}

    async def test_refused_start_calls_nothing(self, db):
        db.add_map("yard", type="local")
        add_robot(db, online=False)
        orch = FakeOrch()
        with pytest.raises(HTTPException) as err:
            await start(db, make_switch({"r1": orch}))
        assert err.value.status_code == 409 and orch.calls == []

    async def test_replace_failure_keeps_the_old_session_and_its_service(self, db):
        orch, switch = prepare(db)
        db.add_map("lot", type="local", status={"state": "draft"})
        await maps.start_session(None, "lot", {"robot": "r1"}, m1.PUB, switch=switch)
        orch.fail[("start", "topomap")] = oc.OrchestratorError(oc.HTTP, "x", status=500)
        orch.services["topomap"] = False
        with pytest.raises(HTTPException):
            await start(db, switch, replace=True)
        [old] = db.open_session("r1")
        assert old["map_name"] == "lot" and old["ended_at"] is None

    async def test_replace_keeps_the_service_the_new_session_runs(self, db):
        orch, switch = prepare(db)
        db.add_map("lot", type="local", status={"state": "draft"})
        await maps.start_session(None, "lot", {"robot": "r1"}, m1.PUB, switch=switch)
        orch.calls.clear()
        out = await start(db, switch, replace=True)
        assert out["replaced_session"]["map_name"] == "lot"
        assert orch.services["topomap"] is True          # never stopped
        assert ("stop", "topomap") not in orch.calls
        assert out["robot_notified"] is True

    async def test_replace_with_operate_stops_the_replaced_mapping_service(self, db):
        orch, switch = prepare(db)
        db.add_map("lot", type="local", status={"state": "ready"})
        await start(db, switch)
        out = await maps.start_session(None, "lot", {"robot": "r1", "purpose": "operate",
                                                     "replace": True}, m1.PUB, switch=switch)
        assert orch.services["topomap"] is False
        assert out["mapping_switch"] == {"topo": "stopped"}


class TestSessionActions:
    async def act(self, db, switch, sid, action, map_name="yard"):
        return await maps.session_action(None, map_name, sid, action, m1.PUB, switch=switch)

    async def test_pause_stops_resume_starts_finish_stops(self, db):
        orch, switch = prepare(db)
        sid = (await start(db, switch))["session"]["session_id"]
        out = await self.act(db, switch, sid, "pause")
        assert orch.services["topomap"] is False and out["robot_notified"] is True
        assert out["mapping_switch"] == {"topo": "stopped"}
        assert out["session"]["state"] == "paused"
        assert out["mapping_state"]["status"] == "off"
        out = await self.act(db, switch, sid, "resume")
        assert orch.services["topomap"] is True and out["mapping_switch"] == {"topo": "started"}
        assert out["mapping_state"]["status"] == "on"
        out = await self.act(db, switch, sid, "finish")
        assert orch.services["topomap"] is False and out["map_state"] == "ready"
        assert [c for c in orch.calls if c[0] in ("start", "stop")] == [
            ("start", "topomap"), ("stop", "topomap"), ("start", "topomap"),
            ("stop", "topomap")]

    async def test_a_paused_session_means_a_stopped_service(self, db):
        orch, switch = prepare(db)
        sid = (await start(db, switch))["session"]["session_id"]
        await self.act(db, switch, sid, "pause")
        snap = await switch.snapshot(db.robots["r1"], fresh=True)
        assert snap.mapping_service() == "not_running"
        assert snap.state({"session_id": sid, "map": "yard", "state": "paused",
                           "aligned": True})["status"] == "off"

    async def test_finish_of_an_offline_robot_closes_the_session_and_says_so(self, db):
        orch, switch = prepare(db)
        sid = (await start(db, switch))["session"]["session_id"]
        orch.reachable = False
        out = await self.act(db, switch, sid, "finish")
        assert out["changed"] is True and out["session"]["state"] == "finished"
        assert out["map_state"] == "ready" and db.open_session("r1") == []
        assert out["robot_notified"] is False
        assert "could not be stopped" in out["mapping_warning"] and "'r1'" in out["mapping_warning"]
        assert out["mapping_switch"] == {"topo": "failed"}
        assert out["mapping_state"]["status"] == "unreachable"
        assert "MAP.SESSION_FINISHED" in db.codes()

    async def test_pause_of_an_offline_robot_pauses_and_warns(self, db):
        orch, switch = prepare(db)
        sid = (await start(db, switch))["session"]["session_id"]
        orch.reachable = False
        out = await self.act(db, switch, sid, "pause")
        assert out["session"]["state"] == "paused" and out["robot_notified"] is False
        assert out["mapping_warning"]

    async def test_a_repeat_retries_the_stop(self, db):
        orch, switch = prepare(db)
        sid = (await start(db, switch))["session"]["session_id"]
        orch.reachable = False
        await self.act(db, switch, sid, "finish")
        orch.reachable = True
        out = await self.act(db, switch, sid, "finish")
        assert out["changed"] is False and out["robot_notified"] is True
        assert orch.services["topomap"] is False

    async def test_a_failed_resume_start_keeps_the_session_paused(self, db):
        orch, switch = prepare(db)
        sid = (await start(db, switch))["session"]["session_id"]
        await self.act(db, switch, sid, "pause")
        orch.reachable = False
        with pytest.raises(HTTPException) as err:
            await self.act(db, switch, sid, "resume")
        assert err.value.status_code == 502
        [s] = db.open_session("r1")
        assert s["paused_at"] is not None
        assert db.maps["yard"]["status"]["state"] == "paused"
        assert db.codes()[-1] == "MAP.SESSION_PAUSED"

    async def test_resume_of_an_offline_robot_is_409_without_calling_it(self, db):
        orch, switch = prepare(db)
        sid = (await start(db, switch))["session"]["session_id"]
        await self.act(db, switch, sid, "pause")
        db.robots["r1"].status.online = False
        orch.calls.clear()
        with pytest.raises(HTTPException) as err:
            await self.act(db, switch, sid, "resume")
        assert err.value.status_code == 409 and orch.calls == []

    async def test_noop_resume_makes_sure_the_service_runs(self, db):
        orch, switch = prepare(db)
        sid = (await start(db, switch))["session"]["session_id"]
        orch.services["topomap"] = False            # it died meanwhile
        out = await self.act(db, switch, sid, "resume")
        assert out["changed"] is False and orch.services["topomap"] is True

    async def test_finishing_an_old_session_never_stops_the_current_ones_service(self, db):
        orch, switch = prepare(db)
        db.add_map("old", type="local")
        old = db.add_session("old", "r1", "live", ended=True)
        await start(db, switch)
        orch.calls.clear()
        out = await self.act(db, switch, str(old["session_id"]), "finish", "old")
        assert out["changed"] is False and orch.services["topomap"] is True
        assert ops(orch, "stop") == []

    async def test_operate_session_finish_stops_nothing(self, db):
        db.add_map("yard", type="local", status={"state": "ready"})
        add_robot(db)
        orch = FakeOrch(running=["topomap"])
        switch = make_switch({"r1": orch})
        out = await maps.start_session(None, "yard", {"robot": "r1", "purpose": "operate"},
                                       m1.PUB, switch=switch)
        await maps.session_action(None, "yard", out["session"]["session_id"], "finish", m1.PUB,
                                  switch=switch)
        assert ops(orch, "stop") == []

    async def test_place_leaves_the_services_alone(self, db):
        orch, switch = prepare(db)
        sid = (await start(db, switch))["session"]["session_id"]
        orch.calls.clear()
        # the first session of an empty local map is placed already: 409, and no service call
        with pytest.raises(HTTPException):
            await maps.place_session(None, "yard", sid, {
                "pose": {"x": 0, "y": 0, "yaw": 0},
                "robot_pose": {"x": 0, "y": 0, "theta": 0}}, m1.PUB, switch=switch)
        assert ops(orch, "start", "stop") == []

    async def test_unknown_action_and_session(self, db):
        _, switch = prepare(db)
        with pytest.raises(HTTPException) as err:
            await self.act(db, switch, "not-a-uuid", "explode")
        assert err.value.status_code == 404
        with pytest.raises(HTTPException) as err:
            await self.act(db, switch, "6f1c0c2e-0000-4000-8000-000000000009", "pause")
        assert err.value.status_code == 404


# --- summary, routes, robot view -------------------------------------------------------------------------

class TestViews:
    async def test_summary_mapping_state(self, db):
        orch, switch = prepare(db)
        out = await maps.session_summary(None, "yard", switch)
        assert out["mapping_state"] is None and out["mapping_services"] is None
        await start(db, switch)
        out = await maps.session_summary(None, "yard", switch)
        assert out["mapping_state"]["status"] == "on"
        assert out["mapping_state"]["session_id"] == out["open"]["session_id"]
        assert out["mapping_service"] == "running"
        assert out["mapping_services"] == {"topo": "running", "grid": "not_available"}
        out = await maps.session_summary(None, "yard")
        assert out["mapping_state"] is None and out["mapping_service"] is None

    async def test_summary_of_an_unreachable_robot_does_not_fail(self, db):
        orch, switch = prepare(db)
        await start(db, switch)
        orch.reachable = False
        switch.invalidate("r1")
        out = await maps.session_summary(None, "yard", switch)
        assert out["mapping_state"]["status"] == "unreachable"

    async def test_routes_pass_the_switch(self, db):
        orch, switch = prepare(db)
        svc = MagicMock()
        svc.database = None
        svc.mapping_switch = switch
        svc.graph_db.get_map_stats.return_value = {"node_count": 0}
        with patch.object(main, "service", svc):
            out = await main.start_map_session("yard", {"robot": "r1"})
            sid = out["session"]["session_id"]
            out = await main.map_session_action("yard", sid, "pause")
        assert out["robot_notified"] is True and orch.services["topomap"] is False

    async def test_robot_view(self):
        orchs = {"r1": FakeOrch(running=["topomap"]), "r2": FakeOrch()}
        svc = MagicMock()
        svc.mapping_switch = make_switch(orchs)
        robots = [robot("r1"), robot("r2"), robot("r3", online=False)]
        svc.database.get_object = AsyncMock(return_value=robots[0])
        svc.database.list_objects = AsyncMock(return_value=robots)
        sessions = {"r1": {"session_id": "s1", "map": "yard", "purpose": "mapping",
                           "state": "mapping", "aligned": True}}
        with patch.object(main, "service", svc), \
                patch.object(maps, "robot_sessions", AsyncMock(return_value=sessions)):
            one = await main.get_robot("r1")
            many = await main.list_robots()
        assert one["mapping_state"]["status"] == "on" and one["name"] == "r1"
        assert one["mapping_state"]["session_id"] == "s1" and one["session"]["map"] == "yard"
        assert one["mapping_services"] == {"topo": "running", "grid": "not_available"}
        by_name = {r["name"]: r for r in many}
        assert by_name["r2"]["mapping_state"]["status"] == "off"
        assert by_name["r2"]["mapping_services"]["topo"] == "not_running"
        assert by_name["r3"]["mapping_state"] is None
        assert by_name["r3"]["mapping_services"]["topo"] == "not_available"
        json.dumps(many, default=str)

    async def test_ws_broadcast(self):
        from packages.api.server import ApiDelegationService
        sent = []

        class WS:
            async def broadcast(self, *args):
                sent.append(args)

        fake = SimpleNamespace(ws_manager=WS(), logger=None)
        await ApiDelegationService._broadcast_mapping_state(fake, "r1", "topo", {"status": "on"})
        [(kind, robot_name, message)] = sent
        assert (kind, robot_name, message["type"]) == ("robot_status", "r1",
                                                       "mapping_state_update")
        assert message["mapping_state"] == {"status": "on"} and message["service"] == "topo"


class TestRemoved:
    def test_the_mqtt_switch_is_gone(self):
        import importlib
        with pytest.raises(ImportError):
            importlib.import_module("packages.api.mapping_control")
        from packages.utils import map_sessions as ms
        assert not hasattr(ms, "set_payload") and not hasattr(ms, "set_topic")
        assert not hasattr(main, "robot_mapping_off")
        assert not [r for r in main.app.routes if getattr(r, "path", "").endswith("/mapping/off")]

    def test_the_proxy_and_the_client_find_the_orchestrator_the_same_way(self):
        import inspect
        from packages.api import orchestrator_proxy
        assert "orchestrator_address" in inspect.getsource(orchestrator_proxy)
