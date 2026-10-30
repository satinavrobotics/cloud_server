"""PUT /api/v1/missions/{name} with update_nodes: one parse (malformed -> 400, never 500), {} and
null are no reroute, `force` is a real bool, the blocked-node check uses the robot's pose (all
waypoints without one), the routes are measured concurrently, the planner's keep-out answer
comes back as non-blocking `warnings`, and a planner-measured route keeps its fresh charts."""
import asyncio
import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

for _k in ("ARANGO_PASSWORD", "MINIO_ACCESS_KEY", "MINIO_SECRET_KEY", "POSTGRES_PASSWORD"):
    os.environ.setdefault(_k, "test")

import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402

import cloud_common.objects.mission as mission_object  # noqa: E402
import packages.api.main as api_main  # noqa: E402
from cloud_common.objects import common  # noqa: E402
from cloud_common.objects.robot import RobotObjectV1  # noqa: E402
from packages.api import route_preview  # noqa: E402

pytestmark = pytest.mark.unit
State = mission_object.MissionStateV1


def _wp(x, y=1.0, **kw):
    return {"x": x, "y": y, "theta": 0.0, "map_id": "M", **kw}


def _mission(state=State.RUNNING, planned_path=None):
    m = mission_object.MissionObjectV1(
        name="m1", robot="r1", status={}, timeout=1000, planned_path=planned_path,
        mission_tree=[{"name": "root_sequence", "parent": "root", "sequence": {}},
                      {"name": "a", "parent": "root_sequence",
                       "route": {"waypoints": [_wp(0.0), _wp(1.0), _wp(2.0)]}}])
    m.status.state = state
    return m


class _Db:
    def __init__(self, mission, robot=None):
        self.row, self.robot = mission, robot
        self.spec_writes = []

    async def get_object(self, cls, name):
        if cls is RobotObjectV1:
            if self.robot is None:
                raise RuntimeError("no robot")
            return self.robot
        return cls(name=name, lifecycle=self.row.lifecycle, status=self.row.status.dict(),
                   **json.loads(self.row.spec.json()))

    async def update_spec_fields(self, cls, name, fields, publisher_id):
        self.spec_writes.append(fields)
        self.row = cls(name=name, lifecycle=self.row.lifecycle, status=self.row.status.dict(),
                       **{**json.loads(self.row.spec.json()), **fields})

    async def update_status(self, *a, **k):
        pass


def _robot(x=0.0, y=1.0, online=True, map_id="M"):
    return SimpleNamespace(status=SimpleNamespace(
        online=online, pose=common.Pose2D(x=x, y=y, map_id=map_id),
        factsheet=SimpleNamespace(length=-1.0, width=-1.0)))


async def _put(db, body, planner=None):
    with patch.object(api_main, "service", SimpleNamespace(database=db, mission_planner=planner)):
        return await api_main.update_mission("m1", body)


# ---- (A) validation -------------------------------------------------------------------------
@pytest.mark.parametrize("bad", [
    {"a": "nope"}, {"a": {"waypoints": "x"}}, {"a": {"waypoints": [{"x": "abc"}]}},
    {"a": {"waypoints": [1]}}, [1, 2], "str", {"a": {}}, {"a": {"waypoints": []}}])
async def test_a_malformed_reroute_is_a_400_not_a_500(bad):
    import httpx
    db = _Db(_mission())
    with patch.object(api_main, "service", SimpleNamespace(database=db, mission_planner=None)):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api_main.app),
                                     base_url="http://t") as client:
            resp = await client.put("/api/v1/missions/m1", json={"update_nodes": bad})
    assert resp.status_code == 400, resp.text
    assert db.row.route_rev == 0 and db.spec_writes == []


def test_parse_reroute_names_the_node():
    with pytest.raises(common.ICSUsageError, match="update_nodes\\['a'\\]"):
        mission_object.parse_reroute({"a": {"waypoints": [{"x": "abc"}]}})


@pytest.mark.parametrize("empty", [None, {}])
async def test_empty_or_null_update_nodes_is_no_reroute(empty):
    db = _Db(_mission())
    out = await _put(db, {"update_nodes": empty})
    assert out["route_rev"] == 0 and db.spec_writes == []
    assert mission_object.parse_reroute(empty) is None


@pytest.mark.parametrize("force", ["false", "true", 1, 0, "yes", [True]])
async def test_force_must_be_a_real_bool(force):
    db = _Db(_mission())
    with pytest.raises(HTTPException) as exc:
        await _put(db, {"update_nodes": {"a": {"waypoints": [_wp(5.0)]}}, "force": force})
    assert exc.value.status_code == 422 and db.row.route_rev == 0


async def test_an_unknown_node_is_refused_before_the_planner_is_asked():
    planner = SimpleNamespace(enrich_route=AsyncMock())
    with pytest.raises(common.ICSUsageError, match="does not exist"):
        await _put(_Db(_mission()), {"update_nodes": {"zz": {"waypoints": [_wp(5.0)]}}}, planner)
    planner.enrich_route.assert_not_awaited()


# ---- (C) blocked nodes vs the robot's pose --------------------------------------------------
def _block_at(monkeypatch, x, y):
    row = {"graph_node_id": "n9", "x": x, "y": y, "robot_x": None, "robot_y": None}
    monkeypatch.setattr(api_main.blocked_nodes, "fetch_active", AsyncMock(return_value=[row]))
    return row


async def test_waypoint_at_the_robot_is_exempt_but_another_blocked_node_is_not(monkeypatch):
    _block_at(monkeypatch, 5.0, 1.0)
    route = {"a": {"waypoints": [_wp(5.0), _wp(7.0)]}}
    # robot stands at the first waypoint: nothing blocked
    db = _Db(_mission(), _robot(5.0, 1.0))
    assert (await _put(db, {"update_nodes": route}))["route_rev"] == 1
    # robot elsewhere (client seeded waypoint 0 at the blocked node): 409
    db = _Db(_mission(), _robot(0.0, 1.0))
    with pytest.raises(HTTPException) as exc:
        await _put(db, {"update_nodes": route})
    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "ROUTE_THROUGH_BLOCKED_NODES"
    assert db.row.route_rev == 0
    # force overrides
    assert (await _put(db, {"update_nodes": route, "force": True}))["route_rev"] == 1


@pytest.mark.parametrize("robot", [None, _robot(5.0, 1.0, online=False)])
async def test_without_a_trusted_pose_every_waypoint_is_checked(monkeypatch, robot):
    _block_at(monkeypatch, 5.0, 1.0)
    db = _Db(_mission(), robot)
    with pytest.raises(HTTPException) as exc:
        await _put(db, {"update_nodes": {"a": {"waypoints": [_wp(5.0), _wp(7.0)]}}})
    assert exc.value.status_code == 409


# ---- (B) concurrency, (D) keep-out warnings -------------------------------------------------
async def test_routes_are_measured_concurrently():
    inflight = peak = 0

    async def enrich(robot, map_id, wps, **kw):
        nonlocal inflight, peak
        inflight += 1
        peak = max(peak, inflight)
        await asyncio.sleep(0.02)
        inflight -= 1
        return {"waypoints": wps}

    planner = SimpleNamespace(enrich_route=AsyncMock(side_effect=enrich))
    reroute = {n: {"waypoints": [_wp(0.0), _wp(3.0)]} for n in ("a", "b", "c")}
    out = await route_preview.measure_reroute(planner, "r1", reroute)
    assert peak == 3 and list(out) == ["a", "b", "c"]
    assert planner.enrich_route.await_args.kwargs["timeout"] > 0
    # one failing route does not take the others down
    planner = SimpleNamespace(enrich_route=AsyncMock(side_effect=[RuntimeError("x"),
                              {"waypoints": [_wp(0.0), _wp(3.0)]}]))
    out = await route_preview.measure_reroute(planner, "r1", {n: reroute[n] for n in "ab"})
    assert list(out) == ["a", "b"]


async def test_crossed_keepout_zones_come_back_as_warnings_not_a_409():
    crossed = [{"from_index": 0, "to_index": 1, "zones": ["z1"]}]
    planner = SimpleNamespace(enrich_route=AsyncMock(side_effect=lambda r, m, wps, **kw: {
        "waypoints": wps, "graph_version": 7, "keepout": {"applied": ["z1"], "crossed": crossed}}))
    out = await _put(_Db(_mission()), {"update_nodes": {"a": {"waypoints": [_wp(0.0), _wp(3.0)]}}},
                     planner)
    assert out["route_rev"] == 1
    (warn,) = out["warnings"]
    assert warn["code"] == "ROUTE_CROSSES_KEEPOUT" and warn["node"] == "a" and warn["crossed"] == crossed
    clean = SimpleNamespace(enrich_route=AsyncMock(side_effect=lambda r, m, wps, **kw: {
        "waypoints": wps, "graph_version": 7, "keepout": {"applied": [], "crossed": []}}))
    assert "warnings" not in await _put(
        _Db(_mission()), {"update_nodes": {"a": {"waypoints": [_wp(0.0), _wp(3.0)]}}}, clean)


# ---- (E)/(F) planned_path, charts -----------------------------------------------------------
async def test_a_reroute_clears_planned_path_and_keeps_planner_charts_only_when_measured():
    wps = [_wp(0.0, chart_key="c0"), _wp(9.0, chart_key="c1")]       # moved: stale unless measured
    unmeasured = SimpleNamespace(enrich_route=AsyncMock(side_effect=RuntimeError("down")))
    out = await _put(_Db(_mission(planned_path=["n1", "n2", "n3"])),
                     {"update_nodes": {"a": {"waypoints": wps, "graph_version": 3}}}, unmeasured)
    assert out["planned_path"] is None
    route = out["mission_tree"][1]["route"]
    assert [w["chart_key"] for w in route["waypoints"]] == [None, None] and route["graph_version"] is None
    measured = SimpleNamespace(enrich_route=AsyncMock(side_effect=lambda r, m, w, **kw: {
        "waypoints": w, "graph_version": 4}))
    out = await _put(_Db(_mission()), {"update_nodes": {"a": {"waypoints": wps}}}, measured)
    route = out["mission_tree"][1]["route"]
    assert [w["chart_key"] for w in route["waypoints"]] == ["c0", "c1"] and route["graph_version"] == 4


async def test_update_returns_the_routes_now_in_the_tree_and_refuses_a_finished_mission():
    m = _mission()
    got = await m.update({"a": {"waypoints": [_wp(5.0)]}})
    assert list(got) == ["a"] and m.mission_tree[1].route is got["a"] and m.route_rev == 1
    done = _mission(state=State.COMPLETED)
    with pytest.raises(common.ICSUsageError):
        await done.update({"a": {"waypoints": [_wp(5.0)]}})
