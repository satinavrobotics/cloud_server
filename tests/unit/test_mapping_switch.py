"""The mapping switch through the robot's orchestrator (docs/satinav-maps-redesign.md §15).

- packages/api/orchestrator_client.py: the orchestrator's routes, error kinds, the address;
- packages/api/mapping_switch.py: service name resolution (`topomap` real / `sim_topomap` sim),
  the state views and their cache (read only);
- packages/api/maps.py: opening / resuming a mapping session starts its services on the robot's
  orchestrator, pausing / finishing / deleting the robot stops them, SLAM is driven too, all
  outside every DB transaction. Switching NEVER blocks or undoes the session: a failure is only
  reported in the response's `robot_actions` ({service, action, ok, label, detail});
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
        # SLAM through the localization facade: `slam` is the slam mode (and, for the tests, the
        # onboard map it records: the last one start_slam looked up), `mode` the stored mode
        # otherwise; the maps that already have a file
        self.slam = {"active": False, "map": None}
        self.mode = "odometry"
        self.last_map = None
        self.slam_files = set()
        self.slam_log = []            # (op, onboard map, body or None)
        self.slam_save_gate = None    # an asyncio.Event a save waits for
        self.slam_save_tx = []        # open DB transactions when a save arrived
        self.save_results = []        # per started save: {"status": "done"|"failed", error}
        self.save_status = None       # what GET /localization/save reports
        self.busy_starts = 0          # saves answered 502 "Map transfer already in progress"

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


async def _get_map(self, name):
    await self.orch._slam("slam_get_map", name, check_tx=False)
    self.orch.last_map = name
    if name not in self.orch.slam_files:
        raise _http(404, f"map '{name}' not found")
    return {"name": name}


async def _get_localization(self):
    """GET /localization: the stored intent (no `topomap`: no mapping API, /services runs it)."""
    o = self.orch
    await o._slam("slam_state", check_tx=False)
    return {"mode": "slam", "map": None} if o.slam["active"] else {"mode": o.mode, "map": None}


async def _put_localization(self, mode, map_name=None, wait=False, topomap=None):
    o = self.orch
    if mode == "slam":
        await o._slam("slam_start", o.last_map, {"mode": mode})
        o.slam = {"active": True, "map": o.last_map}
    else:
        await o._slam("slam_restore", None, {"mode": mode, "map": map_name})
        o.slam, o.mode = {"active": False, "map": None}, mode
    return {"mode": mode, "map": map_name, "applied": True, "problem": None}


async def _save_localization(self, name, cloud_map_id, cloud_session_id):
    """POST /localization/save?background=true: validates, then runs the save per the
    orchestrator's `save_results` script (default: done at once). The robot stays in slam."""
    body = {"cloud_map_id": cloud_map_id, "cloud_session_id": cloud_session_id}
    o = self.orch
    await o._slam("slam_save", name, body)
    if o.slam_save_gate is not None:
        await o.slam_save_gate.wait()
    if o.busy_starts:
        o.busy_starts -= 1
        raise _http(502, "Map transfer already in progress")
    if not o.slam["active"]:
        raise _http(409, "the driver is not in slam mode")
    outcome = o.save_results.pop(0) if o.save_results else {"status": "done"}
    if outcome["status"] == "done":
        o.slam_files.add(name)
    o.save_status = {"map": name, "status": outcome["status"], "error": outcome.get("error")}
    return {"started": True, "map": name}


async def _save_status(self):
    await self.orch._slam("slam_save_status", check_tx=False)
    if self.orch.save_status is None:
        raise _http(404, "no save")
    return dict(self.orch.save_status)


FakeOrch._slam = _slam
FakeClient.get_map = _get_map
FakeClient.get_localization = _get_localization
FakeClient.put_localization = _put_localization
FakeClient.save_localization = _save_localization
FakeClient.localization_save_status = _save_status


async def _no_sleep(_seconds):
    await asyncio.sleep(0)


def make_switch(orchs, **kw):
    """A MappingSwitch whose robots' orchestrators are `orchs` {robot name: FakeOrch}."""
    kw.setdefault("sleep", _no_sleep)
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
        client = switch._client_factory(robot())
        assert await switch.resolve(client, ["topo"]) == {"topo": expected}

    async def test_no_candidate_listed_resolves_to_none(self):
        switch = make_switch({"r1": FakeOrch(services=["something"])})
        client = switch._client_factory(robot())
        assert await switch.resolve(client, ["topo"]) == {"topo": None}

    def test_candidates_come_from_config(self):
        from packages import config
        assert config.MAPPING_SERVICE_CANDIDATES["topo"][:2] == ["topomap", "sim_topomap"]


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
        assert snap.mapping_services() == {"topo": "running", "grid": "not_available",
                                           "slam": "not_running"}

    async def test_running_but_unplaced_is_off_paused_is_on(self):
        snap = await self.snap(FakeOrch(running=["topomap"]))
        assert snap.state(open_session(state="paused"))["status"] == "on"  # still captures
        assert snap.state(open_session(aligned=False))["status"] == "off"
        assert snap.state(open_session(aligned=False))["online"] is True

    async def test_stopped_service(self):
        snap = await self.snap(FakeOrch())
        state = snap.state(open_session())
        assert state["status"] == "off" and state["online"] is False and state["since"] is None
        assert snap.mapping_service() == "not_running"
        assert snap.mapping_services()["topo"] == "not_running"

    async def test_no_session_reports_the_service_only(self):
        state = (await self.snap(FakeOrch(running=["topomap"]))).state(None)
        assert state["status"] == "on" and state["session_id"] is None and state["map"] is None

    async def test_an_operate_session_is_reported_too(self):
        state = (await self.snap(FakeOrch(running=["topomap"]))).state(
            open_session(purpose="operate", state="operating"))
        assert state["session_id"] == "s1" and state["status"] == "on"

    async def test_unreachable_orchestrator(self):
        snap = await self.snap(FakeOrch(reachable=False))
        state = snap.state(open_session())
        assert state["status"] == "unreachable" and state["online"] is False
        assert "not reachable" in state["error"]
        assert snap.mapping_services() == {"topo": "not_available", "grid": "not_available",
                                           "slam": "not_available"}
        assert snap.mapping_service() == "not_running"

    async def test_no_topo_service_on_the_robot(self):
        snap = await self.snap(FakeOrch(services=["something"]))
        assert snap.state(open_session()) is None
        assert snap.mapping_services()["topo"] == "not_available"

    async def test_grid_service_is_reported(self):
        snap = await self.snap(FakeOrch(services=["topomap", "grid"], running=["grid"]))
        assert snap.mapping_services() == {"topo": "not_running", "grid": "running",
                                           "slam": "not_running"}

    async def test_offline_robot_and_no_address_are_not_asked(self):
        orch = FakeOrch()
        switch = make_switch({"r1": orch})
        for kw in ({"online": False}, {"address": False}):
            snap = await switch.snapshot(robot(**kw), fresh=True)
            assert snap.reachable is None and snap.state(open_session()) is None
            assert snap.mapping_services() == {"topo": "not_available", "grid": "not_available",
                                               "slam": "not_available"}
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
        orch.services["topomap"] = True            # the user starts it
        switch.invalidate("r1")
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


def acts(out, *keys):
    """The (service, action, ok) of the response's robot actions."""
    return [(a["service"], a["action"], a["ok"]) for a in out["robot_actions"]]


class TestStop:
    async def test_stops(self):
        orch = FakeOrch(running=["topomap"])
        [a] = await make_switch({"r1": orch}).stop(robot(), ["topo"])
        assert a == {"service": "topomap", "action": "stop", "ok": True,
                     "label": "Topomap service stopped", "detail": None}
        assert orch.services["topomap"] is False

    async def test_not_running_is_fine(self):
        [a] = await make_switch({"r1": FakeOrch()}).stop(robot(), ["topo"])
        assert a["ok"] is True and a["label"] == "Topomap service was not running"

    async def test_offline_robot_is_a_failed_action_never_an_exception(self):
        [a] = await make_switch({"r1": FakeOrch(reachable=False)}).stop(robot(), ["topo"])
        assert a["ok"] is False and a["action"] == "stop" and "not reachable" in a["detail"]
        assert a["label"].startswith("Could not stop topomap: ")

    async def test_a_failed_stop_reports_the_orchestrators_text(self):
        orch = FakeOrch(running=["topomap"], fail={
            ("stop", "topomap"): oc.OrchestratorError(oc.HTTP, "kill failed", status=500)})
        [a] = await make_switch({"r1": orch}).stop(robot(), ["topo"])
        assert a["ok"] is False and "kill failed" in a["detail"]

    async def test_no_address(self):
        [a] = await MappingSwitch().stop(robot(address=False), ["topo"])
        assert a["ok"] is False and "no registered orchestrator" in a["detail"]


class TestStart:
    async def test_starts_and_reports_with_the_real_service_name(self):
        for name in ("topomap", "sim_topomap"):
            orch = FakeOrch(services=[name])
            [a] = await make_switch({"r1": orch}).start(robot(), ["topo"])
            assert a == {"service": name, "action": "start", "ok": True,
                         "label": "Topomap service started", "detail": None}
            assert orch.services[name] is True

    async def test_already_running_is_ok(self):
        [a] = await make_switch({"r1": FakeOrch(running=["topomap"])}).start(robot(), ["topo"])
        assert a["ok"] is True and a["label"] == "Topomap service already running"

    @pytest.mark.parametrize("orch,text", [
        (FakeOrch(reachable=False), "not reachable"),
        (FakeOrch(services=["nothing"]), "has no such service"),
        (FakeOrch(fail={("start", "topomap"): oc.OrchestratorError(oc.TIMEOUT, "timed out")}),
         "timed out"),
        (FakeOrch(fail={("start", "topomap"): oc.OrchestratorError(oc.HTTP, "x", status=500)}),
         "answered 500: x"),
    ])
    async def test_failures_are_reported_never_raised(self, orch, text):
        [a] = await make_switch({"r1": orch}).start(robot(), ["topo"])
        assert a["ok"] is False and a["action"] == "start" and text in a["detail"]
        assert a["label"] == f"Could not start {a['service']}: {a['detail']}"

    async def test_no_address(self):
        [a] = await MappingSwitch().start(robot(address=False), ["topo"])
        assert a["ok"] is False and "no registered orchestrator" in a["detail"]


class TestServiceSwitching:
    """Opening / resuming starts the services, pausing / finishing stops them, and a failure
    never blocks or undoes the user's action (it is only reported in `robot_actions`)."""

    async def act(self, db, switch, sid, action, map_name="yard"):
        return await maps.session_action(None, map_name, sid, action, m1.PUB, switch=switch)

    async def test_opening_starts_the_service_after_the_session_exists(self, db):
        orch, switch = prepare(db)
        out = await start(db, switch)
        assert orch.services["topomap"] is True and db.open_session("r1")
        assert out["robot_actions"] == [{
            "service": "topomap", "action": "start", "ok": True,
            "label": "Topomap service started", "detail": None}]
        assert out["robot_notified"] is True and "mapping_warning" not in out
        assert out["mapping_service"] == "running"
        assert out["mapping_services"] == {"topo": "running", "grid": "not_available",
                                          "slam": "not_running"}
        assert out["mapping_state"]["service"] == "topo"
        assert out["mapping_state"]["session_id"] == out["session"]["session_id"]
        assert out["mapping_state"]["status"] == "on"
        assert db.codes() == ["MAP.SESSION_STARTED"]
        switch.on_session.assert_awaited_once()
        switch.on_state.assert_awaited_once()

    @pytest.mark.parametrize("orch,text", [
        (FakeOrch(reachable=False), "not reachable"),
        (FakeOrch(services=["nothing"]), "has no such service"),
        (FakeOrch(fail={("start", "topomap"): oc.OrchestratorError(oc.TIMEOUT, "timed out")}),
         "timed out"),
        (FakeOrch(fail={("start", "topomap"): oc.OrchestratorError(oc.HTTP, "x", status=500)}),
         "answered 500"),
    ])
    async def test_a_failed_start_keeps_the_session_open_and_reports_it(self, db, orch, text):
        _, switch = prepare(db, orch)
        out = await start(db, switch)                  # no 502 / 504 / 409
        assert db.open_session("r1") and out["session"]["state"] == "mapping"
        assert db.codes() == ["MAP.SESSION_STARTED"]   # nothing closed again
        assert db.maps["yard"]["status"]["state"] == "mapping"
        [a] = out["robot_actions"]
        assert a["ok"] is False and a["action"] == "start" and text in a["detail"]
        assert a["label"].startswith("Could not start ")
        assert out["robot_notified"] is False and a["label"] in out["mapping_warning"]

    async def test_the_start_runs_outside_every_transaction(self, db):
        orch, switch = prepare(db)    # FakeOrch asserts db.open_tx == 0 on every call
        await start(db, switch)
        assert ops(orch, "start") == [("start", "topomap")]

    async def test_pause_stops_resume_starts_finish_stops(self, db):
        orch, switch = prepare(db)
        sid = (await start(db, switch))["session"]["session_id"]
        out = await self.act(db, switch, sid, "pause")
        assert orch.services["topomap"] is False
        assert out["robot_actions"] == [{
            "service": "topomap", "action": "stop", "ok": True,
            "label": "Topomap service stopped", "detail": None}]
        assert out["session"]["state"] == "paused" and out["mapping_state"]["status"] == "off"
        out = await self.act(db, switch, sid, "resume")
        assert orch.services["topomap"] is True
        assert acts(out) == [("topomap", "start", True)]
        assert out["mapping_state"]["status"] == "on"
        out = await self.act(db, switch, sid, "finish")
        # no nodes and no SLAM map: the map goes back to draft (item 6)
        assert orch.services["topomap"] is False and out["map_state"] == "draft"
        assert acts(out) == [("topomap", "stop", True)]
        assert ops(orch, "start", "stop") == [
            ("start", "topomap"), ("stop", "topomap"), ("start", "topomap"),
            ("stop", "topomap")]

    async def test_finish_of_an_offline_robot_closes_the_session_and_reports(self, db):
        orch, switch = prepare(db)
        sid = (await start(db, switch))["session"]["session_id"]
        orch.reachable = False
        out = await self.act(db, switch, sid, "finish")
        assert out["changed"] is True and out["session"]["state"] == "finished"
        assert out["map_state"] == "draft" and db.open_session("r1") == []
        [a] = out["robot_actions"]
        assert a["ok"] is False and a["action"] == "stop" and "not reachable" in a["detail"]
        assert out["robot_notified"] is False and out["mapping_warning"] == a["label"]
        assert out["mapping_state"]["status"] == "unreachable"

    async def test_pause_of_an_offline_robot_pauses_and_reports(self, db):
        orch, switch = prepare(db)
        sid = (await start(db, switch))["session"]["session_id"]
        orch.reachable = False
        out = await self.act(db, switch, sid, "pause")
        assert out["session"]["state"] == "paused" and acts(out) == [("topomap", "stop", False)]

    async def test_a_failed_resume_start_keeps_the_session_mapping(self, db):
        orch, switch = prepare(db)
        sid = (await start(db, switch))["session"]["session_id"]
        await self.act(db, switch, sid, "pause")
        orch.reachable = False
        out = await self.act(db, switch, sid, "resume")        # no 502, no re-pause
        assert out["changed"] is True and out["session"]["state"] == "mapping"
        assert acts(out) == [("topomap", "start", False)]
        assert db.codes()[-1] == "MAP.SESSION_RESUMED"

    async def test_resume_of_an_offline_robot_is_allowed(self, db):
        orch, switch = prepare(db)
        sid = (await start(db, switch))["session"]["session_id"]
        await self.act(db, switch, sid, "pause")
        db.robots["r1"].status.online = False
        out = await self.act(db, switch, sid, "resume")
        assert out["changed"] is True and out["session"]["state"] == "mapping"
        assert acts(out) == [("topomap", "start", True)]   # the orchestrator is reached anyway

    async def test_a_repeat_retries_the_stop_and_the_start(self, db):
        orch, switch = prepare(db)
        sid = (await start(db, switch))["session"]["session_id"]
        await self.act(db, switch, sid, "pause")
        orch.services["topomap"] = True            # somebody started it by hand
        out = await self.act(db, switch, sid, "pause")
        assert out["changed"] is False and acts(out) == [("topomap", "stop", True)]
        assert orch.services["topomap"] is False
        await self.act(db, switch, sid, "resume")
        out = await self.act(db, switch, sid, "resume")
        assert out["changed"] is False and out["robot_actions"][0]["label"] == (
            "Topomap service already running")

    async def test_replace_stops_the_old_topomap_and_starts_the_new_ones(self, db):
        # stopped before the SLAM switch (the mapping API refuses a mode change while it runs)
        orch, switch = prepare(db)
        first = (await start(db, switch))["session"]["session_id"]
        out = await start(db, switch, replace=True)
        assert out["replaced_session"]["session_id"] == first
        assert orch.services["topomap"] is True
        assert acts(out) == [("topomap", "stop", True), ("topomap", "start", True)]

    async def test_replace_with_operate_stops_the_replaced_mapping_service(self, db):
        orch, switch = prepare(db)
        await start(db, switch)
        db.sessions[0]["node_count"] = 5      # the replaced session mapped something
        out = await start(db, switch, replace=True, purpose="operate")
        assert acts(out) == [("topomap", "stop", True)] and orch.services["topomap"] is False

    async def test_replace_with_a_failing_start_still_replaces(self, db):
        orch, switch = prepare(db)
        first = (await start(db, switch))["session"]["session_id"]
        orch.reachable = False
        out = await start(db, switch, replace=True)
        assert out["replaced_session"]["session_id"] == first
        assert [s["ended_at"] is None for s in db.sessions] == [False, True]
        assert acts(out) == [("topomap", "stop", False), ("topomap", "start", False)]

    async def test_finishing_an_old_session_never_stops_the_current_ones_service(self, db):
        orch, switch = prepare(db)
        db.add_map("lot", type="local", status={"state": "draft"})
        old = (await start(db, switch))["session"]["session_id"]
        await maps.start_session(None, "lot", {"robot": "r1", "replace": True}, m1.PUB,
                                 switch=switch)
        orch.calls.clear()
        out = await self.act(db, switch, old, "finish")      # already ended: a repeat
        assert out["changed"] is False and out["robot_actions"] == []
        assert orch.services["topomap"] is True and ops(orch, "stop") == []

    async def test_operate_session_touches_no_service(self, db):
        db.add_map("yard", type="local", status={"state": "ready"})
        add_robot(db)
        orch = FakeOrch()
        switch = make_switch({"r1": orch})
        out = await maps.start_session(None, "yard", {"robot": "r1", "purpose": "operate"},
                                       m1.PUB, switch=switch)
        fin = await maps.session_action(None, "yard", out["session"]["session_id"], "finish",
                                        m1.PUB, switch=switch)
        assert ops(orch, "start", "stop") == []
        assert out["robot_actions"] == [] and fin["robot_actions"] == []

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

    async def test_place_leaves_the_services_alone(self, db):
        orch, switch = prepare(db)
        sid = (await start(db, switch))["session"]["session_id"]
        orch.calls.clear()
        # the first session of an empty local map is placed already; re-placing it by hand is
        # allowed (with a warning) and makes no service call
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
    return [(op, name) for op, name, _ in orch.slam_log
            if op not in ("slam_state", "slam_get_map")]


class TestSlam:
    async def test_start_ok_before_the_topomap_and_outside_a_transaction(self, db):
        orch, switch = slam_prepare(db)
        out = await start(db, switch)
        assert "slam_warning" not in out
        assert orch.slam == {"active": True, "map": "cloud-yard"}
        assert [e for e in orch.slam_log if e[0] == "slam_start"] == [
            ("slam_start", "cloud-yard", {"mode": "slam"})]
        calls = [c for c in orch.calls if c[0] in ("start", "slam_start")]
        assert calls == [("slam_start", "cloud-yard"), ("start", "topomap")]   # SLAM first
        assert out["mapping_services"] == {"topo": "running", "grid": "not_available",
                                           "slam": "running"}

    async def test_robot_actions_list_the_slam_start_and_the_topomap_start(self, db):
        _, switch = slam_prepare(db)
        out = await start(db, switch)
        assert out["robot_actions"] == [
            {"service": "SLAM recording", "action": "start", "ok": True,
             "label": "SLAM recording started", "detail": None},
            {"service": "topomap", "action": "start", "ok": True,
             "label": "Topomap service started", "detail": None}]
        assert "slam_warning" not in out

    async def test_a_failed_slam_start_is_a_failed_action_and_a_warning(self, db):
        orch, switch = slam_prepare(db)
        orch.fail[("slam_start", "cloud-yard")] = oc.OrchestratorError(oc.HTTP, "boom",
                                                                       status=500)
        out = await start(db, switch)
        slam = out["robot_actions"][0]
        assert slam["ok"] is False and slam["action"] == "start" and "boom" in slam["detail"]
        assert slam["detail"] == "boom"
        assert slam["label"] == "SLAM recording not started: boom"
        assert out["slam_warning"] == slam["label"] and db.open_session("r1")

    async def test_finish_reports_the_background_save_and_pause_resume_no_slam(self, db):
        orch, switch = slam_prepare(db)
        sid = (await start(db, switch))["session"]["session_id"]
        out = await maps.session_action(None, "yard", sid, "pause", m1.PUB, switch=switch)
        assert [a["service"] for a in out["robot_actions"]] == ["topomap"]
        out = await maps.session_action(None, "yard", sid, "resume", m1.PUB, switch=switch)
        assert [a["service"] for a in out["robot_actions"]] == ["SLAM recording", "topomap"]
        out = await maps.session_action(None, "yard", sid, "finish", m1.PUB, switch=switch)
        await switch.wait_slam_saves()
        assert out["robot_actions"] == [
            {"service": "topomap", "action": "stop", "ok": True,
             "label": "Topomap service stopped", "detail": None},
            {"service": "SLAM recording", "action": "save", "ok": True,
             "label": "SLAM map save started", "detail": None}]

    async def test_the_background_save_outcome_is_an_event(self, db):
        orch, switch = slam_prepare(db)
        sid = (await start(db, switch))["session"]["session_id"]
        await maps.session_action(None, "yard", sid, "finish", m1.PUB, switch=switch)
        await switch.wait_slam_saves()
        assert db.codes()[-1] == "MAP.SLAM_SAVE_DONE"
        payload = db.events[-1]["payload"]
        assert payload["map_name"] == "yard" and payload["session_id"] == sid
        assert payload["status"] == "saved" and payload["detail"] is None

    async def test_a_failed_background_save_is_an_event(self, db):
        orch, switch = slam_prepare(db)
        sid = (await start(db, switch))["session"]["session_id"]
        orch.fail[("slam_save", "cloud-yard")] = oc.OrchestratorError(oc.HTTP, "driver refused",
                                                                      status=502)
        await maps.session_action(None, "yard", sid, "finish", m1.PUB, switch=switch)
        await switch.wait_slam_saves()
        assert db.codes()[-1] == "MAP.SLAM_SAVE_FAILED"
        assert "driver refused" in db.events[-1]["payload"]["detail"]
        assert db.events[-1]["payload"]["label"].startswith("SLAM map not saved: ")

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
        assert fin["map_state"] == "draft" and "slam_warning" not in fin   # nothing saved
        assert ("slam_restore", None) not in slam_ops(orch)   # nothing saved, nothing restored

    @pytest.mark.parametrize("error", [
        oc.OrchestratorError(oc.UNREACHABLE, "not reachable"),
        oc.OrchestratorError(oc.TIMEOUT, "timed out"),
        oc.OrchestratorError(oc.HTTP, "boom", status=500),
    ])
    async def test_start_failures_only_warn(self, db, error):
        orch, switch = slam_prepare(db)
        orch.fail[("slam_start", "cloud-yard")] = error
        out = await start(db, switch)
        assert out["slam_warning"].startswith("SLAM recording not started: ")
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
            async def get_map(self, name):
                raise RuntimeError("bug")

        res = await MappingSwitch(client_factory=lambda r: Broken()).start_slam(robot(), "yard")
        assert res.status == "failed" and "bug" in res.warning

    async def test_flag_unset_does_nothing(self, db):
        orch, switch = prepare(db)                    # slam_map unset
        sid = (await start(db, switch))["session"]["session_id"]
        await maps.session_action(None, "yard", sid, "finish", m1.PUB, switch=switch)
        await switch.wait_slam_saves()
        assert slam_ops(orch) == []

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
        assert slam_ops(orch) == [] and "slam_warning" not in out

    async def test_pause_does_not_touch_slam_and_resume_finds_it_running(self, db):
        orch, switch = slam_prepare(db)
        sid = (await start(db, switch))["session"]["session_id"]
        orch.slam_log.clear()
        await maps.session_action(None, "yard", sid, "pause", m1.PUB, switch=switch)
        await maps.session_action(None, "yard", sid, "resume", m1.PUB, switch=switch)
        await switch.wait_slam_saves()
        assert slam_ops(orch) == [] and orch.slam["active"] is True

    async def test_finish_saves_in_the_background_with_the_exact_body(self, db):
        orch, switch = slam_prepare(db)
        sid = (await start(db, switch))["session"]["session_id"]
        orch.slam_save_gate = asyncio.Event()
        out = await maps.session_action(None, "yard", sid, "finish", m1.PUB, switch=switch)
        # the request is answered while the save is still running; no nodes, no saved SLAM
        # map yet: draft (the saved map makes it ready, below)
        assert out["map_state"] == "draft" and "slam_warning" not in out
        assert switch.slam_save_view("r1")["state"] == "saving"
        await asyncio.sleep(0)
        assert switch.slam_save_pending("r1")
        orch.slam_save_gate.set()
        await switch.wait_slam_saves()
        saves = [b for op, name, b in orch.slam_log if op == "slam_save"]
        assert saves == [{"cloud_map_id": "yard", "cloud_session_id": sid}]
        # saved, then the intent from before the slam mode is PUT back
        assert orch.slam == {"active": False, "map": None} and "cloud-yard" in orch.slam_files
        assert [b for op, _, b in orch.slam_log if op == "slam_restore"] == [
            {"mode": "odometry", "map": None}]
        assert not switch.slam_save_pending("r1")
        switch.on_slam_done.assert_called_with("r1")
        assert switch.slam_save_view("r1") is None
        assert db.maps["yard"]["status"]["state"] == "ready"
        assert db.maps["yard"]["status"]["slam_saved_at"]

    async def test_a_start_while_the_save_is_pending_starts_after_it(self, db):
        """Item 1: finish a SLAM session, start a new one within the save window: nothing is
        sent to the robot meanwhile (the switch back would kill it), and when the save ended the
        new session's SLAM recording and topomap start; the new session is never killed."""
        orch, switch = slam_prepare(db)
        sid = (await start(db, switch))["session"]["session_id"]
        orch.slam_save_gate = asyncio.Event()
        await maps.session_action(None, "yard", sid, "finish", m1.PUB, switch=switch)
        await asyncio.sleep(0)
        orch.calls.clear()
        orch.slam_log.clear()
        db.add_map("lot", type="local", slam_map=True, status={"state": "draft"})
        out = await maps.start_session(None, "lot", {"robot": "r1"}, m1.PUB, switch=switch)
        assert "slam_warning" not in out and db.open_session("r1")
        assert [(a["service"], a["action"], a["ok"]) for a in out["robot_actions"]] == [
            ("SLAM recording", "start", True), ("topomap", "start", True)]
        assert all("previous SLAM map" in a["label"] for a in out["robot_actions"])
        assert ops(orch, "start", "slam_start") == []          # deferred
        new_sid = out["session"]["session_id"]
        orch.slam_save_gate.set()
        await switch.wait_slam_saves()
        # saved, back to odometry (topomap off), then the new session's SLAM + topomap started
        order = [c for c in slam_ops(orch) if c[0] in ("slam_save", "slam_restore",
                                                         "slam_start")]
        assert order == [("slam_restore", None), ("slam_start", "cloud-lot")]
        assert orch.services["topomap"] is True and orch.slam["active"] is True
        restarted = [e for e in db.events if e["code"] == "MAP.SESSION_SERVICES_RESTARTED"]
        assert len(restarted) == 1
        assert restarted[0]["payload"]["session_id"] == new_sid
        assert restarted[0]["payload"]["reason"] == "slam_save_done"

    async def test_save_failure_leaves_the_robot_in_slam_and_warns(self, db):
        orch, switch = slam_prepare(db)
        sid = (await start(db, switch))["session"]["session_id"]
        orch.fail[("slam_save", "cloud-yard")] = oc.OrchestratorError(oc.HTTP, "driver refused",
                                                                      status=502)
        await maps.session_action(None, "yard", sid, "finish", m1.PUB, switch=switch)
        await switch.wait_slam_saves()
        assert ("slam_restore", None) not in slam_ops(orch)
        assert orch.slam == {"active": True, "map": "cloud-yard"}
        # the result a caller of save_slam sees
        res = await switch.save_slam(db.robots["r1"], "yard", sid)
        assert res.status == "failed" and "driver refused" in res.warning
        assert "left in SLAM mode" in res.warning
        assert orch.slam["active"] is True and ("slam_restore", None) not in slam_ops(orch)

    async def test_save_polls_until_done(self, db):
        orch, switch = slam_prepare(db)
        await start(db, switch)
        res = await switch.save_slam(db.robots["r1"], "yard", "S")
        assert res.status == "saved" and "cloud-yard" in orch.slam_files
        assert orch.slam["active"] is False
        assert ("slam_save_status", None) in slam_ops(orch)

    async def test_a_save_that_ran_out_of_time_is_retried_once(self, db):
        orch, switch = slam_prepare(db)
        await start(db, switch)
        orch.save_results = [{"status": "failed", "error": "Map save did not finish in time"}]
        res = await switch.save_slam(db.robots["r1"], "yard", "S")
        assert res.status == "saved" and "cloud-yard" in orch.slam_files
        assert [op for op, _ in slam_ops(orch) if op == "slam_save"] == ["slam_save"] * 2

    async def test_a_busy_driver_is_retried_once(self, db):
        orch, switch = slam_prepare(db)
        await start(db, switch)
        orch.busy_starts = 1     # "Map transfer already in progress"
        res = await switch.save_slam(db.robots["r1"], "yard", "S")
        assert res.status == "saved"

    async def test_a_second_timeout_fails_and_leaves_the_robot_in_slam(self, db):
        orch, switch = slam_prepare(db)
        await start(db, switch)
        orch.save_results = [{"status": "failed", "error": "did not finish in time"}] * 2
        res = await switch.save_slam(db.robots["r1"], "yard", "S")
        assert res.status == "failed" and "left in SLAM mode" in res.warning
        assert orch.slam["active"] is True and ("slam_restore", None) not in slam_ops(orch)
        assert [op for op, _ in slam_ops(orch) if op == "slam_save"] == ["slam_save"] * 2

    async def test_a_refusal_is_not_retried(self, db):
        orch, switch = slam_prepare(db)
        await start(db, switch)
        orch.save_results = [{"status": "failed", "error": "driver refused"}]
        res = await switch.save_slam(db.robots["r1"], "yard", "S")
        assert res.status == "failed" and orch.slam["active"] is True
        assert [op for op, _ in slam_ops(orch) if op == "slam_save"] == ["slam_save"]

    async def test_not_in_slam_is_nothing_to_save(self, db):
        orch, switch = slam_prepare(db)
        res = await switch.save_slam(db.robots["r1"], "yard", "S")
        assert res.status == "nothing_to_save" and res.warning is None

    async def test_a_refused_save_in_slam_is_a_failure_not_nothing_to_save(self, db):
        orch, switch = slam_prepare(db)
        await start(db, switch)
        orch.fail[("slam_save", "cloud-yard")] = _http(409, "cloud_map_id held by 'x'")
        res = await switch.save_slam(db.robots["r1"], "yard", "S")
        assert res.status == "failed" and "held" in res.warning

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
        # the old topomap stops, the old SLAM map is saved (awaited), then the new one starts
        assert [(a["action"], a["ok"]) for a in out["robot_actions"]] == [
            ("stop", True), ("save", True), ("start", True), ("start", True)]
        assert out["robot_actions"][1]["label"] == "SLAM map saved"
        saved = [b for op, _, b in orch.slam_log if op == "slam_save"]
        assert saved == [{"cloud_map_id": "lot", "cloud_session_id":
                          lot["session"]["session_id"]}]
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
        assert out["mapping_state"]["status"] == "on"    # the session started the topomap
        orch.services["topomap"] = False
        switch.invalidate("r1")
        out = await maps.session_summary(None, "yard", switch)
        assert out["mapping_state"]["status"] == "off"
        orch.services["topomap"] = True
        switch.invalidate("r1")
        out = await maps.session_summary(None, "yard", switch)
        assert out["mapping_state"]["status"] == "on"
        assert out["mapping_state"]["session_id"] == out["open"]["session_id"]
        assert out["mapping_service"] == "running"
        assert out["mapping_services"] == {"topo": "running", "grid": "not_available",
                                          "slam": "not_running"}
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
        assert one["mapping_services"] == {"topo": "running", "grid": "not_available",
                                          "slam": "not_running"}
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


class TestSlamIsASessionService:
    """`slam` in a session's services decides whether SLAM is recorded (not the map flag)."""

    async def test_explicit_topo_on_a_slam_map_does_not_start_slam(self, db):
        orch, switch = slam_prepare(db)
        out = await start(db, switch, services=["topo"])
        assert out["session"]["services"] == ["topo"]
        assert slam_ops(orch) == [] and [a["service"] for a in out["robot_actions"]] == ["topomap"]
        assert "slam_warning" not in out
        fin = await maps.session_action(None, "yard", out["session"]["session_id"], "finish",
                                        m1.PUB, switch=switch)
        await switch.wait_slam_saves()
        assert slam_ops(orch) == [] and [a["service"] for a in fin["robot_actions"]] == ["topomap"]

    async def test_topo_and_slam_starts_both(self, db):
        orch, switch = slam_prepare(db)
        out = await start(db, switch, services=["topo", "slam"])
        assert out["session"]["services"] == ["topo", "slam"]
        assert orch.slam == {"active": True, "map": "cloud-yard"}

    async def test_omitted_services_on_a_slam_map_is_topo_and_slam(self, db):
        orch, switch = slam_prepare(db)
        out = await start(db, switch)
        assert out["session"]["services"] == ["topo", "slam"]
        assert orch.slam["active"] is True

    async def test_omitted_services_on_a_plain_map_is_topo(self, db):
        orch, switch = prepare(db)
        out = await start(db, switch)
        assert out["session"]["services"] == ["topo"] and slam_ops(orch) == []

    async def test_empty_services_open_the_session_and_start_nothing(self, db):
        orch, switch = slam_prepare(db)
        out = await start(db, switch, services=[])
        assert out["session"]["services"] == [] and db.open_session("r1")
        assert out["robot_actions"] == [] and slam_ops(orch) == []
        assert not [c for c in orch.calls if c[0] in ("start", "stop")]
        sid = out["session"]["session_id"]
        for action in ("pause", "resume", "finish"):
            res = await maps.session_action(None, "yard", sid, action, m1.PUB, switch=switch)
            assert res["robot_actions"] == []
        await switch.wait_slam_saves()
        assert not [c for c in orch.calls if c[0] in ("start", "stop")] and slam_ops(orch) == []

    async def test_slam_alone_records_without_the_topomap(self, db):
        orch, switch = slam_prepare(db)
        out = await start(db, switch, services=["slam"])
        assert orch.slam["active"] is True and orch.services.get("topomap") is not True
        assert [a["action"] for a in out["robot_actions"]] == ["start"]

    async def test_slam_on_a_plain_local_map_makes_it_a_slam_map(self, db):
        orch, switch = prepare(db)
        assert not db.maps["yard"]["spec"].get("slam_map")
        out = await start(db, switch, services=["topo", "slam"])
        assert db.maps["yard"]["spec"]["slam_map"] is True
        assert "MAP.SLAM_CHANGED" in db.codes() and orch.slam["active"] is True
        assert out["session"]["services"] == ["topo", "slam"]

    async def test_slam_on_a_geo_map_is_400(self, db):
        orch, switch = prepare(db)
        db.maps["yard"]["spec"]["type"] = "geo"
        with pytest.raises(HTTPException) as err:
            await start(db, switch, services=["slam"])
        assert err.value.status_code == 400 and "geo map" in err.value.detail
        assert not db.open_session("r1")

    async def test_resume_restarts_slam_only_when_in_services(self, db):
        orch, switch = slam_prepare(db)
        sid = (await start(db, switch, services=["topo", "slam"]))["session"]["session_id"]
        await maps.session_action(None, "yard", sid, "pause", m1.PUB, switch=switch)
        orch.slam = {"active": False, "map": None}    # the driver went away meanwhile
        out = await maps.session_action(None, "yard", sid, "resume", m1.PUB, switch=switch)
        assert orch.slam == {"active": True, "map": "cloud-yard"}
        assert [a["action"] for a in out["robot_actions"]] == ["start", "start"]

        await maps.session_action(None, "yard", sid, "finish", m1.PUB, switch=switch)
        await switch.wait_slam_saves()
        orch.slam = {"active": False, "map": None}
        orch.slam_log.clear()
        sid2 = (await start(db, switch, services=["topo"]))["session"]["session_id"]
        await maps.session_action(None, "yard", sid2, "pause", m1.PUB, switch=switch)
        await maps.session_action(None, "yard", sid2, "resume", m1.PUB, switch=switch)
        assert slam_ops(orch) == []

    async def test_finish_of_a_session_from_before_saves_when_slam_records_its_map(self, db):
        orch, switch = slam_prepare(db)
        sid = (await start(db, switch))["session"]["session_id"]
        for s in db.sessions:
            if str(s["session_id"]) == sid:
                s["services"] = ["topo"]            # opened before `slam` existed
        await maps.session_action(None, "yard", sid, "finish", m1.PUB, switch=switch)
        await switch.wait_slam_saves()
        assert [op for op, _, _ in orch.slam_log if op == "slam_save"] == ["slam_save"]


@pytest.mark.unit
class TestSaveFollowUp:
    async def test_a_follow_up_runs_after_its_save_task_is_done(self):
        switch = make_switch({})
        seen = []

        async def save(robot, map_name, session_id):
            from packages.api.mapping_switch import SLAM_NOTHING_TO_SAVE, SlamResult
            return SlamResult(SLAM_NOTHING_TO_SAVE)
        switch.save_slam = save

        async def follow_up():
            seen.append(switch.slam_save_pending("r1"))

        async def on_result(result):
            switch.spawn(follow_up(), after_save_of="r1")
            await asyncio.sleep(0)
            assert seen == []           # not before the save task finished
        switch.schedule_slam_save(robot(), "yard", "s1", on_result=on_result, track=False)
        await switch.wait_slam_saves()
        assert seen == [False]


def test_slam_busy_reports_recording_saving_failed():
    sw = MappingSwitch()
    assert sw.slam_busy("r1") is None
    for state in ("recording", "saving", "failed"):
        sw._set_state_now("r1", state, "m", "s")
        assert sw.slam_busy("r1") == state
    sw._set_state_now("r1", None)
    assert sw.slam_busy("r1") is None
