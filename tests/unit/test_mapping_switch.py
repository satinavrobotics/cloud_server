"""The mapping switch through the robot's orchestrator (docs/satinav-maps-redesign.md §15).

- packages/api/orchestrator_client.py: the orchestrator's routes, error kinds, the address;
- packages/api/mapping_switch.py: service name resolution (`topomap` real / `sim_topomap` sim),
  start (order, already running, failure -> HTTP errors, rollback of a partial start), stop
  (best effort, offline), the state views and their cache;
- packages/api/maps.py: the orchestrator is never called inside a DB transaction: opening a
  session commits, then starts the services (a failed start closes the session again, paired
  events, a draft map back to draft), replace starts first (a failed start changes nothing),
  resume commits then starts (a failed start pauses again), pause / finish stop them after the
  commit (a failed stop still closes the session), replace keeps what the new session runs;
- the routes and the robot view fill `mapping_state` / `mapping_services` from the orchestrator.

The in-memory store is the M1 one with robot locks (tests/unit/test_maps_m2.py ShimDb).
"""
import asyncio
import contextlib
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
        self.db = None                # set by prepare(): the DB whose transactions must be closed
        self.delay = 0.0
        # SLAM (GET/POST /maps/...): the running driver and the maps that already have a file
        self.slam = {"active": False, "map": None}
        self.slam_files = set()
        self.slam_log = []            # (op, onboard map, body or None)
        self.slam_save_gate = None    # an asyncio.Event a save waits for
        self.slam_save_tx = []        # open DB transactions when a save arrived
        self.slam_saving = False      # what GET /maps/mapping reports as `saving`
        self.slam_reports_saving = True   # False: an older orchestrator without the field

    def _enter(self, op, name=None):
        self.calls.append((op, name))
        if self.db is not None:
            assert self.db.open_tx == 0, f"orchestrator {op} inside a DB transaction"
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
        if self.orch.delay:
            await asyncio.sleep(self.orch.delay)
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


async def _slam(self, op, onboard=None, body=None, check_tx=True):
    self.calls.append((op, onboard))
    self.slam_log.append((op, onboard, body))
    if op == "slam_save":
        self.slam_save_tx.append(self.db.open_tx if self.db is not None else 0)
    elif check_tx and self.db is not None:
        assert self.db.open_tx == 0, f"orchestrator {op} inside a DB transaction"
    if not self.reachable:
        raise oc.OrchestratorError(oc.UNREACHABLE, "orchestrator at 10.0.0.5:8080 is not "
                                                   "reachable (ConnectError)")
    if (op, onboard) in self.fail:
        raise self.fail[(op, onboard)]


def _http(status, detail):
    return oc.OrchestratorError(oc.HTTP, detail, status=status)


async def _slam_start(self, onboard, overwrite=False):
    await self.orch._slam("slam_start", onboard, {"overwrite": overwrite})
    o = self.orch
    if o.slam["active"]:
        raise _http(409, "a mapping session/driver is already running (stop it first)")
    if onboard in o.slam_files and not overwrite:
        raise _http(409, f"Map '{onboard}' already has a map file; pass overwrite=true to "
                         "replace it")
    o.slam = {"active": True, "map": onboard}
    return {"active": True}


async def _slam_save(self, onboard, cloud_map_id, cloud_session_id, stop_after=True):
    body = {"cloud_map_id": cloud_map_id, "cloud_session_id": cloud_session_id,
            "stop_after": stop_after}
    o = self.orch
    await o._slam("slam_save", onboard, body)
    if o.slam_save_gate is not None:
        await o.slam_save_gate.wait()
    if not o.slam["active"]:
        raise _http(409, "No mapping session is running")
    if o.slam["map"] != onboard:
        raise _http(409, f"Running session is mapping '{o.slam['map']}', not '{onboard}'")
    o.slam_files.add(onboard)
    if stop_after:
        o.slam = {"active": False, "map": None}
    return {"name": onboard}


async def _slam_stop(self):
    await self.orch._slam("slam_stop")
    if not self.orch.slam["active"]:
        raise _http(404, "No mapping session is running")
    self.orch.slam = {"active": False, "map": None}
    return {"success": True}


async def _slam_state(self):
    await self.orch._slam("slam_state", check_tx=False)
    out = {"active": self.orch.slam["active"], "map": self.orch.slam["map"], "pid": 9}
    if self.orch.slam_reports_saving:
        out["saving"] = self.orch.slam_saving
    return out


FakeOrch._slam = _slam
FakeClient.start_slam = _slam_start
FakeClient.save_slam = _slam_save
FakeClient.stop_slam = _slam_stop
FakeClient.slam_state = _slam_state


def make_switch(orchs, **kw):
    """A MappingSwitch whose robots' orchestrators are `orchs` {robot name: FakeOrch}."""
    return MappingSwitch(client_factory=lambda robot: FakeClient(orchs[robot.name]), **kw)


def robot(name="r1", online=True, address=True):
    from cloud_common.objects.robot import RobotObjectV1, RobotStatusV1
    extra = {"ip_address": "10.0.0.5", "entrypoint_port": 8080} if address else {}
    return RobotObjectV1(name=name, status=RobotStatusV1(online=online), **extra)


def add_robot(db, name="r1", online=True, address=True):
    db.robots[name] = robot(name, online, address)


class TxTracking(ShimDb):
    """ShimDb that counts the open transactions (a FakeOrch call inside one fails)."""
    open_tx = 0

    @contextlib.asynccontextmanager
    async def store(self, _db, _publisher_id):
        self.open_tx += 1
        try:
            async with super().store(_db, _publisher_id) as store:
                yield store
        finally:
            self.open_tx -= 1


@pytest.fixture
def db():
    d = TxTracking()
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

    async def test_concurrent_reads_share_one_fetch(self):
        orch = FakeOrch()
        orch.delay = 0.05
        orig = FakeClient.list_services

        async def slow_list(client):
            await asyncio.sleep(orch.delay)
            return await orig(client)

        switch = make_switch({"r1": orch})
        r = robot()
        with patch.object(FakeClient, "list_services", slow_list):
            snaps = await asyncio.gather(*(switch.snapshot(r) for _ in range(5)))
        assert all(s is snaps[0] for s in snaps) and len(ops(orch, "list")) == 1

    async def test_a_read_in_flight_during_a_switch_is_not_cached(self):
        orch = FakeOrch()
        switch = make_switch({"r1": orch})
        r = robot()
        task = asyncio.ensure_future(switch.snapshot(r))
        await asyncio.sleep(0)               # the read has started
        switch.invalidate("r1")              # a switch happened meanwhile
        await task
        assert "r1" not in switch._cache

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
    orch.db = db
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
        # committed, then closed again: the STARTED event has its FINISHED, nothing is left open
        assert db.open_session("r1") == []
        assert db.codes() == ["MAP.SESSION_STARTED", "MAP.SESSION_FINISHED"]
        assert db.maps["yard"]["status"] == {"state": "draft", "open_session_id": None}
        [closed] = db.sessions
        assert closed["ended_at"] is not None
        switch.on_state.assert_not_awaited()

    async def test_the_start_runs_outside_every_transaction(self, db):
        orch, switch = prepare(db)
        await start(db, switch)                       # FakeOrch asserts open_tx == 0
        assert ("start", "topomap") in orch.calls

    async def test_a_failed_start_of_a_ready_map_leaves_it_ready(self, db):
        orch, switch = prepare(db, FakeOrch(reachable=False))
        db.maps["yard"]["status"] = {"state": "ready"}
        with pytest.raises(HTTPException):
            await start(db, switch)
        assert db.maps["yard"]["status"]["state"] == "ready" and db.open_session("r1") == []

    async def test_the_robots_starts_serialize(self, db):
        orch, switch = prepare(db)
        orch.delay = 0.05
        db.add_map("lot", type="local", status={"state": "draft"})
        first = asyncio.ensure_future(start(db, switch))
        await asyncio.sleep(0.01)                     # first is inside its (slow) start
        second = asyncio.ensure_future(maps.start_session(
            None, "lot", {"robot": "r1"}, m1.PUB, "op", switch=switch))
        ok, refused = await asyncio.gather(first, second, return_exceptions=True)
        assert isinstance(ok, dict) and isinstance(refused, HTTPException)
        assert refused.status_code == 409 and "already has an open" in refused.detail
        assert [s["map_name"] for s in db.open_session("r1")] == ["yard"]

    async def test_a_failed_close_still_raises_the_start_error(self, db):
        orch, switch = prepare(db, FakeOrch(reachable=False))
        real = maps._finish_in

        async def broken(*args, **kw):
            raise RuntimeError("db down")

        with patch.object(maps, "_finish_in", broken):
            with pytest.raises(HTTPException) as err:
                await start(db, switch)
        assert err.value.status_code == 502

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
        events = list(db.codes())
        orch.fail[("start", "topomap")] = oc.OrchestratorError(oc.HTTP, "x", status=500)
        orch.services["topomap"] = False
        with pytest.raises(HTTPException):
            await start(db, switch, replace=True)
        [old] = db.open_session("r1")
        assert old["map_name"] == "lot" and old["ended_at"] is None
        # start first: no session, no event, no map state change (nothing to compensate)
        assert db.codes() == events and len(db.sessions) == 1
        assert db.maps["yard"]["status"]["state"] == "draft"
        assert db.maps["lot"]["status"]["state"] == "mapping"

    async def test_replace_starts_before_it_commits(self, db):
        orch, switch = prepare(db)
        db.add_map("lot", type="local", status={"state": "draft"})
        await maps.start_session(None, "lot", {"robot": "r1"}, m1.PUB, switch=switch)
        await maps.session_action(None, "lot", db.open_session("r1")[0]["session_id"], "pause",
                                  m1.PUB, switch=switch)
        seen = []

        class Spy(list):
            def append(self, call):
                seen.append((call, [s["map_name"] for s in db.open_session("r1")]))
                super().append(call)

        orch.calls = Spy(orch.calls)
        out = await start(db, switch, replace=True)
        assert (("start", "topomap"), ["lot"]) in seen   # the old session was still the open one
        assert out["session"]["map_name"] == "yard"
        assert [s["map_name"] for s in db.open_session("r1")] == ["yard"]

    async def test_replace_that_fails_to_commit_stops_what_it_started(self, db):
        orch, switch = prepare(db)
        db.add_map("lot", type="local", status={"state": "draft"})
        await maps.start_session(None, "lot", {"robot": "r1"}, m1.PUB, switch=switch)
        await maps.session_action(None, "lot", db.open_session("r1")[0]["session_id"], "pause",
                                  m1.PUB, switch=switch)
        assert orch.services["topomap"] is False
        real = maps._open_tx
        state = {"n": 0}

        async def flaky(*args, **kw):
            state["n"] += 1
            if not kw.get("dry_run") and state["n"] == 2:
                raise HTTPException(409, "changed meanwhile")
            return await real(*args, **kw)

        with patch.object(maps, "_open_tx", flaky):
            with pytest.raises(HTTPException) as err:
                await start(db, switch, replace=True)
        assert err.value.status_code == 409
        assert ("start", "topomap") in orch.calls
        assert orch.services["topomap"] is False        # started, then stopped again
        assert [s["map_name"] for s in db.open_session("r1")] == ["lot"]

    async def test_replace_validation_errors_come_before_the_orchestrator(self, db):
        orch, switch = prepare(db)
        db.add_map("lot", type="local", status={"state": "draft"})
        await maps.start_session(None, "lot", {"robot": "r1"}, m1.PUB, switch=switch)
        db.robots["r1"].status.online = False
        orch.calls.clear()
        with pytest.raises(HTTPException) as err:
            await start(db, switch, replace=True)
        assert err.value.status_code == 409 and orch.calls == []

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
        # resumed, then paused again: the events pair up
        assert db.codes()[-3:] == ["MAP.SESSION_PAUSED", "MAP.SESSION_RESUMED",
                                   "MAP.SESSION_PAUSED"]

    async def test_resume_starts_outside_the_transaction(self, db):
        orch, switch = prepare(db)
        sid = (await start(db, switch))["session"]["session_id"]
        await self.act(db, switch, sid, "pause")
        await self.act(db, switch, sid, "resume")     # FakeOrch asserts open_tx == 0
        assert orch.services["topomap"] is True

    async def test_a_failed_noop_resume_changes_nothing(self, db):
        orch, switch = prepare(db)
        sid = (await start(db, switch))["session"]["session_id"]
        orch.services["topomap"] = False
        orch.fail[("start", "topomap")] = oc.OrchestratorError(oc.TIMEOUT, "timed out")
        events = list(db.codes())
        with pytest.raises(HTTPException) as err:
            await self.act(db, switch, sid, "resume")
        assert err.value.status_code == 504 and db.codes() == events
        assert db.open_session("r1")[0]["paused_at"] is None

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

# --- SLAM map ----------------------------------------------------------------------------------------

def slam_prepare(db, **kw):
    orch, switch = prepare(db, **kw)
    db.maps["yard"]["spec"]["slam_map"] = True
    switch.on_slam_done = MagicMock()
    return orch, switch


def slam_ops(orch):
    return [(op, name) for op, name, _ in orch.slam_log if op != "slam_state"]


class TestSlamReconcile:
    """A save lost (robot offline at finish / API restart) is recovered from derived state."""

    async def lost(self, db, **kw):
        orch, switch = slam_prepare(db, **kw)
        sid = (await start(db, switch))["session"]["session_id"]
        await maps.session_action(None, "yard", sid, "finish", m1.PUB)   # no switch: no save
        assert orch.slam == {"active": True, "map": "cloud-yard"}
        return orch, switch, sid

    async def test_saves_the_lost_map_with_the_newest_ended_session(self, db):
        orch, switch, sid = await self.lost(db)
        assert await switch.reconcile_slam_saves(db, [db.robots["r1"]]) == ["r1"]
        await switch.wait_slam_saves()
        saved = [b for op, _, b in orch.slam_log if op == "slam_save"]
        assert saved == [{"cloud_map_id": "yard", "cloud_session_id": sid, "stop_after": True}]
        assert orch.slam["active"] is False

    @pytest.mark.parametrize("change", ["saving", "no_field", "open_session", "no_slam_map",
                                        "other_map", "idle", "no_address"])
    async def test_skips_when_a_save_is_not_provably_lost(self, db, change):
        orch, switch, _ = await self.lost(db)
        if change == "saving":
            orch.slam_saving = True            # a POST is in flight
        elif change == "no_field":
            orch.slam_reports_saving = False   # older orchestrator: unknown is not safe
        elif change == "open_session":
            await maps.start_session(None, "yard", {"robot": "r1"}, m1.PUB)
        elif change == "no_slam_map":
            db.maps["yard"]["spec"]["slam_map"] = False
        elif change == "other_map":
            orch.slam = {"active": True, "map": "somebody-else"}
        elif change == "idle":
            orch.slam = {"active": False, "map": None}
        elif change == "no_address":
            db.robots["r1"] = robot("r1", address=False)
        assert await switch.reconcile_slam_saves(db, [db.robots["r1"]]) == []
        await switch.wait_slam_saves()
        assert not [op for op, _, _ in orch.slam_log if op == "slam_save"]

    async def test_one_robot_failing_does_not_block_the_others(self, db):
        orch, switch, _ = await self.lost(db)
        add_robot(db, "r2")
        bad = FakeOrch(reachable=False)
        switch._client_factory = lambda r: FakeClient({"r1": orch, "r2": bad}[r.name])
        found = await switch.reconcile_slam_saves(db, [db.robots["r2"], db.robots["r1"]])
        assert found == ["r1"]
        with patch.object(maps, "open_store", side_effect=RuntimeError("db down")):
            assert await switch.reconcile_slam_saves(db, [db.robots["r1"]]) == []  # no raise
        await switch.wait_slam_saves()


class TestOrphanSlam:
    """A driver recording `cloud-<X>` for a deleted cloud map X is stopped."""

    async def orphan(self, db, **kw):
        orch, switch = slam_prepare(db, **kw)
        orch.slam = {"active": True, "map": "cloud-gone"}   # no map `gone` in the cloud
        return orch, switch

    async def test_reconcile_stops_the_driver_of_a_missing_map(self, db):
        orch, switch = await self.orphan(db)
        assert await switch.reconcile_slam_saves(db, [db.robots["r1"]]) == []
        assert [op for op, _, _ in orch.slam_log if op == "slam_stop"] == ["slam_stop"]
        assert not [1 for op, _, _ in orch.slam_log if op == "slam_save"]
        assert orch.slam["active"] is False
        switch.on_slam_done.assert_called_once_with("r1")

    async def test_stop_helper_stops_by_map_name(self, db):
        orch, switch = await self.orphan(db)
        assert await switch.stop_orphan_slam(db, db.robots["r1"], "other") is False
        assert orch.slam["active"] is True
        assert await switch.stop_orphan_slam(db, db.robots["r1"], "gone") is True
        assert orch.slam["active"] is False

    @pytest.mark.parametrize("change", ["map_exists", "archived", "saving", "no_field",
                                        "open_session", "save_pending", "idle", "foreign",
                                        "no_address"])
    async def test_not_stopped(self, db, change):
        orch, switch = await self.orphan(db)
        if change == "map_exists":
            orch.slam = {"active": True, "map": "cloud-yard"}
        elif change == "archived":
            db.maps["yard"]["lifecycle"] = "ARCHIVED"
            orch.slam = {"active": True, "map": "cloud-yard"}
        elif change == "saving":
            orch.slam_saving = True
        elif change == "no_field":
            orch.slam_reports_saving = False
        elif change == "open_session":
            await maps.start_session(None, "yard", {"robot": "r1"}, m1.PUB)
        elif change == "save_pending":
            task = MagicMock()
            task.done.return_value = False
            switch._slam_tasks["r1"] = task
        elif change == "idle":
            orch.slam = {"active": False, "map": None}
        elif change == "foreign":
            orch.slam = {"active": True, "map": "somebody-else"}
        elif change == "no_address":
            db.robots["r1"] = robot("r1", address=False)
        before = orch.slam["active"]
        assert await switch.stop_orphan_slam(db, db.robots["r1"]) is False
        assert not [1 for op, _, _ in orch.slam_log if op == "slam_stop"]
        assert orch.slam["active"] is before

    async def test_orchestrator_error_is_swallowed(self, db, caplog):
        orch, switch = await self.orphan(db)
        orch.fail[("slam_stop", None)] = oc.OrchestratorError(oc.UNREACHABLE, "down")
        with caplog.at_level("WARNING"):
            assert await switch.stop_orphan_slam(db, db.robots["r1"], "gone") is False
        assert "gone" in caplog.text and "r1" in caplog.text
        orch.reachable = False
        assert await switch.stop_orphan_slam(db, db.robots["r1"]) is False
        with patch.object(maps, "open_store", side_effect=RuntimeError("db down")):
            assert await switch.stop_orphan_slam(db, db.robots["r1"]) is False


class TestSlamReconcilePeriodic:
    async def test_runs_again_every_interval_and_skips_offline_robots(self):
        conn = MagicMock(closed=False)
        conn.execute = AsyncMock(return_value=MagicMock(fetchone=AsyncMock(return_value=(True,))))
        conn.close = AsyncMock()
        database = MagicMock(dedicated_connection=AsyncMock(return_value=conn))
        switch = MappingSwitch()
        switch.reconcile_slam_saves = AsyncMock(return_value=[])
        robots = [robot("on"), robot("off", online=False)]
        switch.start_slam_reconcile(database, AsyncMock(return_value=robots), interval_s=0.01)
        for _ in range(100):
            if switch.reconcile_slam_saves.await_count >= 2:
                break
            await asyncio.sleep(0.01)
        await switch.stop_slam_reconcile()
        assert switch.reconcile_slam_saves.await_count >= 2
        assert [r.name for r in switch.reconcile_slam_saves.await_args[0][1]] == ["on"]
        assert conn.close.await_count >= 2   # the lock is released every round


class TestSlamReconcileStartup:
    @pytest.mark.parametrize("leader", [True, False])
    async def test_only_the_lock_holder_reconciles_and_the_lock_is_released(self, leader):
        conn = MagicMock(closed=False)
        conn.execute = AsyncMock(return_value=MagicMock(fetchone=AsyncMock(return_value=(leader,))))
        conn.close = AsyncMock()
        database = MagicMock(dedicated_connection=AsyncMock(return_value=conn))
        switch = MappingSwitch()
        switch.reconcile_slam_saves = AsyncMock(return_value=[])
        list_robots = AsyncMock(return_value=[])
        switch.start_slam_reconcile(database, list_robots)
        await switch._reconcile_task
        assert switch.reconcile_slam_saves.await_count == int(leader)
        conn.close.assert_awaited_once()

    async def test_never_raises(self):
        database = MagicMock(dedicated_connection=AsyncMock(side_effect=OSError("down")))
        switch = MappingSwitch()
        switch.start_slam_reconcile(database, AsyncMock())
        await switch._reconcile_task
        await switch.stop_slam_reconcile()


class TestSlam:
    async def test_start_ok_after_the_topomap_and_outside_a_transaction(self, db):
        orch, switch = slam_prepare(db)
        out = await start(db, switch)
        assert "slam_warning" not in out
        assert orch.slam == {"active": True, "map": "cloud-yard"}
        assert orch.slam_log[-1] == ("slam_start", "cloud-yard", {"overwrite": False})
        calls = [c for c in orch.calls if c[0] in ("start", "slam_start")]
        assert calls == [("start", "topomap"), ("slam_start", "cloud-yard")]
        # SLAM is no session service
        assert out["mapping_switch"] == {"topo": "started"}
        assert out["mapping_services"] == {"topo": "running", "grid": "not_available"}

    async def test_a_run_already_recording_this_map_is_fine(self, db):
        orch, switch = slam_prepare(db)
        orch.slam = {"active": True, "map": "cloud-yard"}
        out = await start(db, switch)
        assert "slam_warning" not in out
        assert not [c for c in slam_ops(orch) if c[0] == "slam_start"]

    async def test_existing_map_file_is_a_warning_and_nothing_to_save_later(self, db):
        orch, switch = slam_prepare(db)
        orch.slam_files.add("cloud-yard")
        out = await start(db, switch)
        assert out["slam_warning"] == "SLAM map already exists, not re-recorded"
        assert db.open_session("r1") and orch.slam["active"] is False
        sid = out["session"]["session_id"]
        fin = await maps.session_action(None, "yard", sid, "finish", m1.PUB, switch=switch)
        await switch.wait_slam_saves()
        assert fin["map_state"] == "ready" and "slam_warning" not in fin
        assert ("slam_stop", None) not in slam_ops(orch)   # nothing to save, no stop

    async def test_another_run_active_is_a_warning_and_is_left_alone(self, db):
        orch, switch = slam_prepare(db)
        orch.slam = {"active": True, "map": "somebody-else"}
        out = await start(db, switch)
        assert "already running" in out["slam_warning"] and db.open_session("r1")
        sid = out["session"]["session_id"]
        await maps.session_action(None, "yard", sid, "finish", m1.PUB, switch=switch)
        await switch.wait_slam_saves()
        assert orch.slam == {"active": True, "map": "somebody-else"}
        assert ("slam_stop", None) not in slam_ops(orch)

    @pytest.mark.parametrize("error", [
        oc.OrchestratorError(oc.UNREACHABLE, "not reachable"),
        oc.OrchestratorError(oc.TIMEOUT, "timed out"),
        oc.OrchestratorError(oc.HTTP, "boom", status=500),
    ])
    async def test_start_failures_only_warn(self, db, error):
        orch, switch = slam_prepare(db)
        orch.fail[("slam_start", "cloud-yard")] = error
        out = await start(db, switch)
        assert out["slam_warning"] and "'yard'" in out["slam_warning"]
        assert db.open_session("r1") and orch.services["topomap"] is True
        assert db.codes() == ["MAP.SESSION_STARTED"]       # nothing rolled back

    async def test_switch_start_never_raises(self, db):
        orch, switch = slam_prepare(db)
        orch.reachable = False
        res = await switch.start_slam(db.robots["r1"], "yard")
        assert res.status == "failed" and "not reachable" in res.warning
        res = await make_switch({"r1": orch}).start_slam(robot(address=False), "yard")
        assert res.status == "failed"

        class Broken:
            async def slam_state(self):
                raise RuntimeError("bug")

        res = await MappingSwitch(client_factory=lambda r: Broken()).start_slam(robot(), "yard")
        assert res.status == "failed" and "bug" in res.warning

    async def test_flag_unset_does_nothing(self, db):
        orch, switch = prepare(db)                    # slam_map unset
        sid = (await start(db, switch))["session"]["session_id"]
        await maps.session_action(None, "yard", sid, "finish", m1.PUB, switch=switch)
        await switch.wait_slam_saves()
        assert orch.slam_log == []

    async def test_geo_map_does_nothing(self, db):
        db.add_map("geo1", type="geo", slam_map=True)    # cannot be created, but stored
        assert await maps._slam_wanted(None, "geo1") is False
        db.add_map("loc", type="local", slam_map=True)
        assert await maps._slam_wanted(None, "loc") is True
        db.add_map("old", type="local")
        assert await maps._slam_wanted(None, "old") is False

    async def test_operate_sessions_never_record(self, db):
        orch, switch = slam_prepare(db)
        db.maps["yard"]["status"] = {"state": "ready"}
        out = await start(db, switch, purpose="operate")
        await maps.session_action(None, "yard", out["session"]["session_id"], "finish",
                                  m1.PUB, switch=switch)
        await switch.wait_slam_saves()
        assert orch.slam_log == [] and "slam_warning" not in out

    async def test_pause_and_resume_do_not_touch_slam(self, db):
        orch, switch = slam_prepare(db)
        sid = (await start(db, switch))["session"]["session_id"]
        orch.slam_log.clear()
        await maps.session_action(None, "yard", sid, "pause", m1.PUB, switch=switch)
        await maps.session_action(None, "yard", sid, "resume", m1.PUB, switch=switch)
        await switch.wait_slam_saves()
        assert orch.slam_log == [] and orch.slam["active"] is True

    async def test_finish_saves_in_the_background_with_the_exact_body(self, db):
        orch, switch = slam_prepare(db)
        sid = (await start(db, switch))["session"]["session_id"]
        orch.slam_save_gate = asyncio.Event()
        out = await maps.session_action(None, "yard", sid, "finish", m1.PUB, switch=switch)
        # the request is answered while the save is still running
        assert out["map_state"] == "ready" and "slam_warning" not in out
        await asyncio.sleep(0)
        assert switch.slam_save_pending("r1")
        orch.slam_save_gate.set()
        await switch.wait_slam_saves()
        saves = [b for op, name, b in orch.slam_log if op == "slam_save"]
        assert saves == [{"cloud_map_id": "yard", "cloud_session_id": sid, "stop_after": True}]
        assert orch.slam == {"active": False, "map": None} and "cloud-yard" in orch.slam_files
        assert not switch.slam_save_pending("r1")
        switch.on_slam_done.assert_called_with("r1")

    async def test_a_start_is_refused_while_the_save_is_pending(self, db):
        orch, switch = slam_prepare(db)
        sid = (await start(db, switch))["session"]["session_id"]
        orch.slam_save_gate = asyncio.Event()
        await maps.session_action(None, "yard", sid, "finish", m1.PUB, switch=switch)
        await asyncio.sleep(0)
        out = await start(db, switch)
        assert "still being saved" in out["slam_warning"] and db.open_session("r1")
        orch.slam_save_gate.set()
        await switch.wait_slam_saves()

    async def test_save_failure_stops_the_driver_and_warns(self, db):
        orch, switch = slam_prepare(db)
        sid = (await start(db, switch))["session"]["session_id"]
        orch.fail[("slam_save", "cloud-yard")] = oc.OrchestratorError(oc.HTTP, "driver refused",
                                                                      status=502)
        await maps.session_action(None, "yard", sid, "finish", m1.PUB, switch=switch)
        await switch.wait_slam_saves()
        assert ("slam_stop", None) in slam_ops(orch)
        assert orch.slam["active"] is False
        # the result a caller of save_slam sees
        orch.slam = {"active": True, "map": "cloud-yard"}
        res = await switch.save_slam(db.robots["r1"], "yard", sid)
        assert res.status == "failed" and "driver refused" in res.warning
        assert orch.slam["active"] is False

    async def test_save_failure_leaves_a_foreign_run_alone(self, db):
        orch, switch = slam_prepare(db)
        orch.slam = {"active": True, "map": "other"}
        orch.fail[("slam_save", "cloud-yard")] = oc.OrchestratorError(oc.TIMEOUT, "slow")
        res = await switch.save_slam(db.robots["r1"], "yard", "S")
        assert res.status == "failed" and orch.slam == {"active": True, "map": "other"}

    async def test_unreachable_save_never_raises(self, db):
        orch, switch = slam_prepare(db)
        orch.reachable = False
        res = await switch.save_slam(db.robots["r1"], "yard", "S")
        assert res.status == "failed" and res.warning

    async def test_finish_of_an_offline_robot_warns_without_a_save(self, db):
        orch, switch = slam_prepare(db)
        sid = (await start(db, switch))["session"]["session_id"]
        db.robots["r1"].status.online = False
        out = await maps.session_action(None, "yard", sid, "finish", m1.PUB, switch=switch)
        assert "not saved" in out["slam_warning"]
        assert not [b for op, _, b in orch.slam_log if op == "slam_save"]

    async def test_replace_saves_the_old_map_before_starting_the_new(self, db):
        orch, switch = slam_prepare(db)
        db.add_map("lot", type="local", slam_map=True, status={"state": "draft"})
        lot = await maps.start_session(None, "lot", {"robot": "r1"}, m1.PUB, switch=switch)
        assert orch.slam["map"] == "cloud-lot"
        out = await start(db, switch, replace=True)
        order = [c for c in slam_ops(orch) if c[0] in ("slam_start", "slam_save")]
        assert order == [("slam_start", "cloud-lot"), ("slam_save", "cloud-lot"),
                         ("slam_start", "cloud-yard")]
        saved = [b for op, _, b in orch.slam_log if op == "slam_save"]
        assert saved == [{"cloud_map_id": "lot", "cloud_session_id":
                          lot["session"]["session_id"], "stop_after": True}]
        assert orch.slam == {"active": True, "map": "cloud-yard"}
        assert "slam_warning" not in out
        assert orch.slam_save_tx == [0]               # not inside a DB transaction

    async def test_replace_with_a_failing_save_still_starts_and_warns(self, db):
        orch, switch = slam_prepare(db)
        db.add_map("lot", type="local", slam_map=True, status={"state": "draft"})
        await maps.start_session(None, "lot", {"robot": "r1"}, m1.PUB, switch=switch)
        orch.fail[("slam_save", "cloud-lot")] = oc.OrchestratorError(oc.HTTP, "x", status=500)
        out = await start(db, switch, replace=True)
        assert "'lot' not saved" in out["slam_warning"]
        assert out["session"]["map_name"] == "yard"

    async def test_without_a_switch_nothing_happens(self, db):
        db.add_map("yard", type="local", slam_map=True, status={"state": "draft"})
        add_robot(db)
        out = await maps.start_session(None, "yard", {"robot": "r1"}, m1.PUB)
        assert "slam_warning" not in out


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
        svc.reloc_jobs.active_for.return_value = None
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
                           "state": "mapping", "aligned": True, "node_count": 7}}
        with patch.object(main, "service", svc), \
                patch.object(maps, "robot_sessions", AsyncMock(return_value=sessions)):
            one = await main.get_robot("r1")
            many = await main.list_robots()
        assert one["mapping_state"]["status"] == "on" and one["name"] == "r1"
        assert one["mapping_state"]["session_id"] == "s1" and one["session"]["map"] == "yard"
        assert one["mapping_state"]["nodes_sent"] == 7      # the open session's node count
        assert one["mapping_services"] == {"topo": "running", "grid": "not_available"}
        by_name = {r["name"]: r for r in many}
        assert by_name["r1"]["mapping_state"]["nodes_sent"] == 7
        assert by_name["r2"]["mapping_state"]["nodes_sent"] is None   # no open session
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
