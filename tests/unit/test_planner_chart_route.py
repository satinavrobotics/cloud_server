"""plan_route with the chart graph (SCENE_GRAPH_PLANNER_ENABLED): fakes for store and graph.

Flag off is the legacy planner untouched; every failure of the chart route (snap, crosses_blocked,
unreachable, store exception, ...) falls back to the legacy planner and says why.
"""
import asyncio
import math
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import cloud_common.objects as api_objects
from packages import config
from packages.database.postgres import PostgresDatabase
from packages.scene_graph.api import MissingTiles, Route, Snap
from packages.services.mission_planner import scene_route
from packages.services.mission_planner.server import MissionPlannerService

IDENTITY = {"map_name": "m", "aligned": True, "map_t_session": {"tx": 0.0, "ty": 0.0, "yaw": 0.0}}
H = 0.1


class FakeGraph:
    """A free strip 0 <= x <= 10, -0.5 <= y <= 0.5; cells on a 0.1 lattice."""
    params = SimpleNamespace(lattice=H)

    def __init__(self):
        self.crosses = False
        self.unreachable = False
        self.route_error = None
        self.blocked_seen = "unset"
        self.routes = 0

    def snap(self, x, y, max_dist=None):
        cx = min(max(x, 0.0), 10.0 - 1e-9)
        cy = min(max(y, -0.5), 0.5 - 1e-9)
        px = (math.floor(cx / H) + 0.5) * H
        py = (math.floor(cy / H) + 0.5) * H
        d = math.hypot(px - x, py - y)
        if max_dist is not None and d > max_dist:
            return Snap(None, px, py, d)
        return Snap("c0", px, py, d, self.crosses and x > 10.0)

    def route(self, a, b, blocked=None):
        self.routes += 1
        self.blocked_seen = blocked
        if self.route_error:
            raise self.route_error
        if self.unreachable:
            return None
        nodes = [((i + 0.5) * H, 0.05) for i in range(100)]
        return Route(charts=["c0", "c1"], length=10.0, min_width=0.8, nodes=nodes)


class FakeStore:
    def __init__(self, graph, version=3):
        self.graph, self.version = graph, version
        self.loads = []
        self.meta_error = None
        self.load_error = None
        self.gate = None

    def meta(self, map_id):
        if self.meta_error:
            raise self.meta_error
        return {"version": self.version} if self.version is not None else {}

    def load(self, map_id, version=None):
        self.loads.append((map_id, version))
        if self.gate is not None:
            self.gate.wait(5)
        if self.load_error:
            raise self.load_error
        return self.graph


def _service(store=None):
    with patch("packages.services.mission_planner.server.GraphDatabaseService"), \
            patch("packages.services.mission_planner.server.PostgresDatabase"):
        svc = MissionPlannerService(default_map_id="m")
    robot = api_objects.RobotObjectV1(name="r1", status={})
    robot.status.pose.x, robot.status.pose.y = 0.0, 0.0
    db = AsyncMock(spec=PostgresDatabase)
    svc.database = db
    svc.get_robot_status = AsyncMock(return_value=robot)
    svc._open_session = AsyncMock(return_value=IDENTITY)
    g = MagicMock()
    g.k_nearest_neighbors.side_effect = lambda x, y, k, map_id: (
        [{"node_id": f"n{round(x)}", "x": x, "y": y}], [0.1])
    g.nodes_in_range.return_value = ([{"node_id": "2", "x": 2.0, "y": 0.0}], [0.1])
    # legacy graph: three nodes, marks that the legacy planner ran via shortest_path
    g.shortest_path.return_value = ["1", "2"]
    g.get_node.side_effect = lambda map_id, node_id: {"node_id": node_id, "x": float(node_id), "y": 0.0, "yaw": 0.0}
    svc.graph_db = g
    svc._active_blocked_nodes = AsyncMock(return_value=[])
    if store is not None:
        svc._scene_cache = scene_route.GraphCache(lambda: store, max_entries=2, load_timeout_s=1.0)
    return svc


@pytest.fixture
def on(monkeypatch):
    monkeypatch.setattr(config, "SCENE_GRAPH_PLANNER_ENABLED", True)


def _plan(svc, **kw):
    return svc.plan_route("r1", target_x=10.0, target_y=0.0, map_id="m", **kw)


@pytest.mark.unit
async def test_flag_off_is_the_legacy_planner_and_never_touches_the_store(monkeypatch):
    monkeypatch.setattr(config, "SCENE_GRAPH_PLANNER_ENABLED", False)
    store = FakeStore(FakeGraph())
    svc = _service(store)
    out = await _plan(svc)
    assert out["success"] and out["planner"] == "legacy"
    assert "planner_fallback" not in out
    assert store.loads == [] and out["path"] == ["1", "2"]


@pytest.mark.unit
async def test_success_returns_pose_waypoints_with_distinct_leaf_ids(on):
    graph, store = FakeGraph(), None
    store = FakeStore(graph, version=7)
    svc = _service(store)
    out = await _plan(svc)
    assert out["success"] and out["planner"] == scene_route.PLANNER_SCENE_GRAPH
    assert out["graph_version"] == 7 and store.loads == [("m", 7)]       # version-pinned load
    wps = out["waypoints"]
    assert [w.node_id for w in wps] == out["path"] and len(set(out["path"])) == len(wps) >= 2
    assert (wps[0].x, wps[-1].x) == (pytest.approx(0.05), pytest.approx(9.95))
    assert all(w.map_id == "m" for w in wps)
    assert out["start_node_id"] == out["path"][0] and out["end_node_id"] == out["path"][-1]
    assert out["min_width"] == 0.8 and out["chart_keys"] == ["c0", "c1"]
    assert out["goal"] == {"x": 10.0, "y": 0.0, "map_id": "m", "node_id": out["path"][-1]}
    assert graph.blocked_seen is None                      # no blocked rows: no overlay
    svc.graph_db.shortest_path.assert_not_called()


@pytest.mark.unit
async def test_blocked_rows_become_the_route_overlay_and_limit_simplification(on):
    graph = FakeGraph()
    svc = _service(FakeStore(graph))
    row = {"graph_node_id": "@5.00,0.00", "x": 5.0, "y": 0.0, "robot_x": 2.0, "robot_y": 0.0}
    svc._active_blocked_nodes = AsyncMock(return_value=[row])
    out = await _plan(svc)
    seen = graph.blocked_seen
    assert len(seen.segments) == 1 and seen.discs == []
    (a, b, half) = seen.segments[0]
    assert a == (3.0, 0.0) and b == (5.0, 0.0)             # starts lead_m ahead of the robot
    # no chord may cross the segment: the straight lattice route through it is not simplified over it
    xs = [w.x for w in out["waypoints"]]
    assert not any(p < 3.0 - half and q > 5.0 + half for p, q in zip(xs, xs[1:]))


@pytest.mark.unit
async def test_ignore_exclusions_skips_the_overlay(on):
    graph = FakeGraph()
    svc = _service(FakeStore(graph))
    svc._active_blocked_nodes = AsyncMock(return_value=[{"x": 5.0, "y": 0.0}])
    await _plan(svc, ignore_exclusions=True)
    assert graph.blocked_seen is None
    svc._active_blocked_nodes.assert_not_awaited()


@pytest.mark.unit
async def test_start_too_far_falls_back_to_legacy_with_reason(on):
    svc = _service(FakeStore(FakeGraph()))
    out = await _plan(svc, robot_pose=(-5.0, 0.0))
    assert out["success"] and out["planner"] == "legacy"
    assert out["planner_fallback"] == "start_too_far"


@pytest.mark.unit
async def test_goal_crossing_a_blocked_cell_falls_back(on):
    graph = FakeGraph()
    graph.crosses = True
    svc = _service(FakeStore(graph))
    out = await svc.plan_route("r1", target_x=10.8, target_y=0.0, map_id="m")   # 0.8 m off, within 1.5
    assert out["planner"] == "legacy" and out["planner_fallback"] == "goal_crosses_blocked"
    assert out["success"]


@pytest.mark.unit
async def test_unreachable_and_missing_tiles_fall_back(on):
    graph = FakeGraph()
    svc = _service(FakeStore(graph))
    graph.unreachable = True
    assert (await _plan(svc))["planner_fallback"] == "unreachable"
    graph.unreachable = False
    graph.route_error = MissingTiles([(0, 0)])
    out = await _plan(svc)
    assert out["planner_fallback"] == "missing_tiles" and out["planner"] == "legacy"


@pytest.mark.unit
async def test_store_exception_no_graph_and_load_error_fall_back(on):
    store = FakeStore(FakeGraph())
    svc = _service(store)
    store.meta_error = RuntimeError("arango down")
    assert (await _plan(svc))["planner_fallback"] == "store_error"
    store.meta_error = None
    store.version = None
    assert (await _plan(svc))["planner_fallback"] == "no_graph"
    store.version = 1
    store.load_error = OSError("minio")
    out = await _plan(svc)
    assert out["planner_fallback"] == "load_error" and out["success"]


@pytest.mark.unit
async def test_store_factory_failure_falls_back_and_is_not_retried_at_once(on):
    calls = []

    def factory():
        calls.append(1)
        raise ConnectionError("no minio")
    svc = _service()
    svc._scene_cache = scene_route.GraphCache(factory)
    assert (await _plan(svc))["planner_fallback"] == "store_unavailable"
    assert (await _plan(svc))["planner_fallback"] == "store_unavailable"
    assert len(calls) == 1


@pytest.mark.unit
async def test_unexpected_exception_in_the_route_falls_back(on):
    graph = FakeGraph()
    graph.route_error = ValueError("boom")
    out = await _plan(_service(FakeStore(graph)))
    assert out["planner"] == "legacy" and out["planner_fallback"] == "error" and out["success"]


@pytest.mark.unit
async def test_waypoints_sharing_a_leaf_id_fall_back(on):
    svc = _service(FakeStore(FakeGraph()))
    svc.graph_db.k_nearest_neighbors.side_effect = lambda x, y, k, map_id: (
        [{"node_id": "n", "x": x, "y": y}], [0.1])
    out = await _plan(svc)
    assert out["planner"] == "legacy"
    assert out["planner_fallback"] == "waypoints_without_distinct_leaf_ids"


@pytest.mark.unit
async def test_robot_not_placed_goes_to_legacy_which_reports_it(on):
    svc = _service(FakeStore(FakeGraph()))
    svc._open_session = AsyncMock(return_value=None)
    out = await _plan(svc)
    assert out["failed_at"] == "robot_not_placed" and out["planner_fallback"] == "robot_not_placed"


@pytest.mark.unit
async def test_graph_is_cached_per_version_and_reloaded_on_a_new_one(on):
    store = FakeStore(FakeGraph(), version=1)
    svc = _service(store)
    await _plan(svc)
    await _plan(svc)
    assert store.loads == [("m", 1)]
    store.version = 2
    await _plan(svc)
    assert store.loads == [("m", 1), ("m", 2)]
    store.version = 1                                   # still cached (LRU of 2)
    await _plan(svc)
    assert len(store.loads) == 2
    store.version = 3                                   # evicts the least recently used: 2
    await _plan(svc)
    store.version = 1
    await _plan(svc)
    assert store.loads == [("m", 1), ("m", 2), ("m", 3)]
    store.version = 2
    await _plan(svc)
    assert store.loads[-1] == ("m", 2)


@pytest.mark.unit
async def test_a_cold_load_over_the_timeout_does_not_block_planning_and_fills_the_cache(on):
    store = FakeStore(FakeGraph(), version=1)
    store.gate = threading.Event()
    svc = _service()
    svc._scene_cache = scene_route.GraphCache(lambda: store, load_timeout_s=0.1)
    out = await _plan(svc)
    assert out["planner_fallback"] == "load_timeout" and out["success"]
    out = await _plan(svc)                              # still loading: shared, not restarted
    assert out["planner_fallback"] == "load_timeout" and len(store.loads) == 1
    store.gate.set()
    await asyncio.sleep(0.2)
    out = await _plan(svc)
    assert out["planner"] == scene_route.PLANNER_SCENE_GRAPH and len(store.loads) == 1
