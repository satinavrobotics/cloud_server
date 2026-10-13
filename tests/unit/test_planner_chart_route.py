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
        self.only_free = False         # reachable only without an overlay

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
        if self.unreachable or (self.only_free and blocked is not None):
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
        self.cursor = None

    def meta(self, map_id):
        if self.meta_error:
            raise self.meta_error
        if self.version is None:
            return {}
        return {"version": self.version, "cursor": self.cursor} if self.cursor else {"version": self.version}

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
    # byte-identical to before the chart planner: no planner / planner_fallback keys at all
    assert out["success"] and "planner" not in out and "planner_fallback" not in out
    assert store.loads == [] and out["path"] == ["1", "2"]
    assert "scene_graph" not in svc.get_stats()


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
async def test_a_route_on_one_leaf_is_served_as_two_waypoints(on):
    """Every waypoint snaps to one leaf (sparse leaves): the route is served, not given up. (The
    10 m fake strip is longer than the waypoint gap cap, so the 2-waypoint collapse itself is
    covered in test_scene_route.)"""
    svc = _service(FakeStore(FakeGraph()))
    svc.graph_db.k_nearest_neighbors.side_effect = lambda x, y, k, map_id: (
        [{"node_id": "n", "x": x, "y": y}], [0.1])
    out = await _plan(svc)
    assert out["planner"] == "scene_graph" and not out.get("planner_fallback")
    assert out["path"] == ["n"] * len(out["waypoints"]) and len(out["waypoints"]) >= 2
    assert out["start_node_id"] == out["end_node_id"] == "n"


@pytest.mark.unit
async def test_a_waypoint_without_a_leaf_still_falls_back(on):
    svc = _service(FakeStore(FakeGraph()))
    svc.graph_db.k_nearest_neighbors.side_effect = lambda x, y, k, map_id: ([], [])
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
    out = await _plan(svc)                              # v1 serves while v2 loads in the background
    assert out["graph_version"] == 1
    await asyncio.sleep(0.2)
    assert (await _plan(svc))["graph_version"] == 2
    assert store.loads == [("m", 1), ("m", 2)]
    store.version = 3                                   # v2 serves (newest older), then 3 evicts v1
    assert (await _plan(svc))["graph_version"] == 2
    await asyncio.sleep(0.2)
    assert (await _plan(svc))["graph_version"] == 3
    assert store.loads == [("m", 1), ("m", 2), ("m", 3)]


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


# ---- robot width, version lag, rows the overlay cannot place ---------------------------------------

def _width(svc, value):
    svc.get_robot_status.return_value.status.factsheet.width = value


@pytest.mark.unit
async def test_route_narrower_than_the_robot_falls_back_too_narrow(on, monkeypatch):
    monkeypatch.setattr(config, "SCENE_GRAPH_WIDTH_CHECK_ENABLED", True)
    graph = FakeGraph()                                    # the fake route's min_width is 0.8
    svc = _service(FakeStore(graph))
    _width(svc, 1.0)
    out = await _plan(svc)
    assert out["planner"] == "legacy" and out["planner_fallback"] == "too_narrow" and out["success"]
    _width(svc, 0.6)
    out = await _plan(svc)
    assert out["planner"] == scene_route.PLANNER_SCENE_GRAPH and out["min_width"] == 0.8


@pytest.mark.unit
async def test_unknown_robot_width_never_rejects_and_margin_applies(on, monkeypatch):
    monkeypatch.setattr(config, "SCENE_GRAPH_WIDTH_CHECK_ENABLED", True)
    svc = _service(FakeStore(FakeGraph()))
    for unknown in (-1, 0, None):
        _width(svc, unknown)
        assert (await _plan(svc))["planner"] == scene_route.PLANNER_SCENE_GRAPH
    _width(svc, 0.7)
    monkeypatch.setattr(config, "SCENE_GRAPH_WIDTH_MARGIN_M", 0.2)
    assert (await _plan(svc))["planner_fallback"] == "too_narrow"


def _lagging(svc, store, age_s, rows="one"):
    store.cursor = [1_000, "k1"]
    import time as _t
    waiting = [{"costmap_stored_ms": _t.time() * 1000 - age_s * 1000, "_key": "k2"}]
    svc.graph_db.costmap_nodes_since.side_effect = None
    svc.graph_db.costmap_nodes_since.return_value = waiting if rows == "one" else []


@pytest.mark.unit
async def test_graph_lagging_the_ingest_falls_back_stale_graph(on, monkeypatch):
    monkeypatch.setattr(config, "SCENE_GRAPH_MAX_VERSION_LAG_S", 60.0)
    store = FakeStore(FakeGraph())
    svc = _service(store)
    _lagging(svc, store, 300)
    out = await _plan(svc)
    assert out["planner"] == "legacy" and out["planner_fallback"] == "stale_graph"
    svc.graph_db.costmap_nodes_since.assert_called_with("m", 1000, "k1", 1)   # the version's own cursor
    _lagging(svc, store, 5)                               # a fresh upload is not lag
    assert (await _plan(svc))["planner"] == scene_route.PLANNER_SCENE_GRAPH
    _lagging(svc, store, 0, rows="none")                  # nothing waiting
    assert (await _plan(svc))["planner"] == scene_route.PLANNER_SCENE_GRAPH


@pytest.mark.unit
async def test_unknown_lag_or_disabled_check_never_rejects(on, monkeypatch):
    monkeypatch.setattr(config, "SCENE_GRAPH_MAX_VERSION_LAG_S", 60.0)
    store = FakeStore(FakeGraph())
    svc = _service(store)
    store.cursor = None                                    # no cursor saved: unknown
    svc.graph_db.costmap_nodes_since.return_value = [{"costmap_stored_ms": 1}]
    assert (await _plan(svc))["planner"] == scene_route.PLANNER_SCENE_GRAPH
    store.cursor = [1, "k"]
    svc.graph_db.costmap_nodes_since.side_effect = RuntimeError("arango down")
    assert (await _plan(svc))["planner"] == scene_route.PLANNER_SCENE_GRAPH
    svc.graph_db.costmap_nodes_since.side_effect = None
    monkeypatch.setattr(config, "SCENE_GRAPH_MAX_VERSION_LAG_S", 0.0)
    assert (await _plan(svc))["planner"] == scene_route.PLANNER_SCENE_GRAPH
    assert svc.graph_db.costmap_nodes_since.call_count == 1  # no cursor: no query; error: one; disabled: none


@pytest.mark.unit
async def test_reachable_only_without_the_overlay_is_reported_blocked_by_overlay(on):
    graph = FakeGraph()
    graph.only_free = True
    svc = _service(FakeStore(graph))
    svc._active_blocked_nodes = AsyncMock(return_value=[
        {"graph_node_id": "@5.00,0.00", "x": 5.0, "y": 0.0}])
    out = await _plan(svc)
    assert out["planner"] == "legacy" and out["planner_fallback"] == "blocked_by_overlay"
    graph.unreachable = True                              # unreachable even without it
    assert (await _plan(svc))["planner_fallback"] == "unreachable"


@pytest.mark.unit
async def test_blocked_graph_node_id_on_the_route_goes_to_the_legacy_planner(on):
    """A row naming a topomap node id (no position, no robot pose) places nothing in the overlay;
    the legacy point rule on the waypoints' leaf ids still applies."""
    graph = FakeGraph()
    svc = _service(FakeStore(graph))
    svc._active_blocked_nodes = AsyncMock(return_value=[{"graph_node_id": "n5"}])
    out = await _plan(svc)                                # leaf ids are n<round(x)>: the route passes n5
    assert out["planner"] == "legacy" and out["planner_fallback"] == "blocked_row_on_route"
    svc._active_blocked_nodes = AsyncMock(return_value=[{"graph_node_id": "n77"}])
    assert (await _plan(svc))["planner"] == scene_route.PLANNER_SCENE_GRAPH


@pytest.mark.unit
async def test_a_slow_new_version_routes_on_the_loaded_older_one_bounded_by_its_lag(on, monkeypatch):
    import time as _t
    monkeypatch.setattr(config, "SCENE_GRAPH_MAX_VERSION_LAG_S", 60.0)
    store = FakeStore(FakeGraph(), version=1)
    store.cursor = [1_000, "k1"]
    svc = _service()
    svc._scene_cache = scene_route.GraphCache(lambda: store, max_entries=3, load_timeout_s=0.1)
    # v1's cursor has an old unfolded upload behind it, v2's has none
    svc.graph_db.costmap_nodes_since.side_effect = lambda m, ms, key, n: (
        [{"costmap_stored_ms": _t.time() * 1000 - 300_000, "_key": "k2"}] if ms == 1_000 else [])
    monkeypatch.setattr(config, "SCENE_GRAPH_MAX_VERSION_LAG_S", 0.0)     # v1 is fresh while unchecked
    assert (await _plan(svc))["graph_version"] == 1
    store.version, store.cursor = 2, [2_000, "k9"]
    store.gate = threading.Event()
    out = await _plan(svc)                              # v2 loading: v1 serves
    assert out["planner"] == scene_route.PLANNER_SCENE_GRAPH and out["graph_version"] == 1
    monkeypatch.setattr(config, "SCENE_GRAPH_MAX_VERSION_LAG_S", 60.0)
    out = await _plan(svc)                              # v1's own cursor says it is too old
    assert out["planner_fallback"] == "stale_graph"
    store.gate.set()
    await asyncio.sleep(0.2)
    out = await _plan(svc)
    assert out["planner"] == scene_route.PLANNER_SCENE_GRAPH and out["graph_version"] == 2


@pytest.mark.unit
async def test_width_check_is_off_by_default_and_never_refuses_on_width(on):
    assert config.SCENE_GRAPH_WIDTH_CHECK_ENABLED is False
    svc = _service(FakeStore(FakeGraph()))              # fake min_width 0.8
    _width(svc, 5.0)
    out = await _plan(svc)
    assert out["planner"] == scene_route.PLANNER_SCENE_GRAPH and out["min_width"] == 0.8


# ---- cache churn: background refresh, bounded versions ------------------------------------------

@pytest.mark.unit
async def test_newer_version_refreshes_in_background_single_flight_and_serves_old_meanwhile(on):
    store = FakeStore(FakeGraph(), version=1)
    cache = scene_route.GraphCache(lambda: store, max_entries=8, load_timeout_s=1.0)
    assert (await cache.get("m"))[1] == 1
    store.version = 2
    store.gate = threading.Event()
    for _ in range(3):
        _, v = await asyncio.wait_for(cache.get("m"), 0.5)      # immediate, not waiting for the load
        assert v == 1
    await asyncio.sleep(0.05)
    assert store.loads == [("m", 1), ("m", 2)]                  # one load for three requests
    store.gate.set()
    await asyncio.sleep(0.2)
    assert (await cache.get("m"))[1] == 2 and len(store.loads) == 2


@pytest.mark.unit
async def test_cache_keeps_at_most_two_versions_per_map(on):
    store = FakeStore(FakeGraph(), version=1)
    cache = scene_route.GraphCache(lambda: store, max_entries=8, load_timeout_s=1.0)
    await cache.get("m")
    for v in (2, 3, 4):
        store.version = v
        await cache.get("m")
        await asyncio.sleep(0.1)
        await cache.get("m")
    assert sorted(k for k in cache._graphs if k[0] == "m") == [("m", 3), ("m", 4)]


@pytest.mark.unit
async def test_a_failed_background_refresh_keeps_serving_the_old_version(on):
    store = FakeStore(FakeGraph(), version=1)
    cache = scene_route.GraphCache(lambda: store, max_entries=8, load_timeout_s=1.0)
    await cache.get("m")
    store.version, store.load_error = 2, RuntimeError("boom")
    assert (await cache.get("m"))[1] == 1
    await asyncio.sleep(0.1)
    assert (await cache.get("m"))[1] == 1


# ---- pre-warm ----------------------------------------------------------------------------------------

@pytest.mark.unit
async def test_prewarm_loads_configured_maps_and_never_raises(monkeypatch):
    monkeypatch.setattr(config, "SCENE_GRAPH_PLANNER_ENABLED", True)
    monkeypatch.setattr(config, "SCENE_GRAPH_PREWARM_MAPS", ("a", "bad", "none"))
    store = FakeStore(FakeGraph(), version=7)
    orig_meta, orig_load = store.meta, store.load
    store.meta = lambda m: {} if m == "none" else orig_meta(m)

    def load(m, v=None):
        if m == "bad":
            raise RuntimeError("boom")
        return orig_load(m, v)
    store.load = load
    svc = _service()
    svc._scene_cache = scene_route.GraphCache(lambda: store, max_entries=8)
    task = svc.start_scene_prewarm()
    assert task is not None
    await asyncio.wait_for(task, 2.0)
    assert ("a", 7) in svc._scene_cache._graphs and ("none", 7) not in svc._scene_cache._graphs


@pytest.mark.unit
async def test_prewarm_defaults_to_listed_maps_and_is_off_with_the_planner(monkeypatch):
    monkeypatch.setattr(config, "SCENE_GRAPH_PREWARM_MAPS", ())
    store = FakeStore(FakeGraph(), version=2)
    svc = _service()
    svc._scene_cache = scene_route.GraphCache(lambda: store, max_entries=8)
    svc.graph_db.list_maps.return_value = ["m1", "m2"]
    monkeypatch.setattr(config, "SCENE_GRAPH_PLANNER_ENABLED", False)
    assert svc.start_scene_prewarm() is None and store.loads == []
    monkeypatch.setattr(config, "SCENE_GRAPH_PLANNER_ENABLED", True)
    await asyncio.wait_for(svc.start_scene_prewarm(), 2.0)
    assert sorted(store.loads) == [("m1", 2), ("m2", 2)]


@pytest.mark.unit
async def test_prewarm_store_failure_is_logged_not_fatal(monkeypatch):
    monkeypatch.setattr(config, "SCENE_GRAPH_PLANNER_ENABLED", True)
    monkeypatch.setattr(config, "SCENE_GRAPH_PREWARM_MAPS", ("a",))
    svc = _service()

    def factory():
        raise RuntimeError("no store")
    svc._scene_cache = scene_route.GraphCache(factory)
    await asyncio.wait_for(svc.start_scene_prewarm(), 2.0)


# ---- pre-warm skips maps switched off; load back-off ---------------------------------------------

class _SwitchDb:
    """`connection()` -> cursor answering the disabled-maps query for the names in `off`."""

    def __init__(self, off=(), fail=False):
        self.off, self.fail, self.sql = set(off), fail, []

    def connection(self):
        db = self

        class _Ctx:
            async def __aenter__(self):
                if db.fail:
                    raise RuntimeError("down")
                return self

            async def __aexit__(self, *exc):
                return False

            def cursor(self):
                return self

            async def execute(self, sql, params):
                db.sql.append((sql, params))
                self.names = params[0]

            async def fetchall(self):
                return [(n,) for n in self.names if n in db.off]
        return _Ctx()


@pytest.mark.unit
def test_disabled_maps_sql_is_the_graph_builders():
    from packages.services.graph_builder import scene_guard
    assert scene_route.DISABLED_MAPS_SQL == scene_guard.DISABLED_SQL
    assert scene_route.ENABLED_KEY == scene_guard.ENABLED_KEY


@pytest.mark.unit
async def test_prewarm_skips_maps_switched_off_and_does_not_load_them(monkeypatch):
    monkeypatch.setattr(config, "SCENE_GRAPH_PLANNER_ENABLED", True)
    monkeypatch.setattr(config, "SCENE_GRAPH_PREWARM_MAPS", ())
    store = FakeStore(FakeGraph(), version=2)
    svc = _service()
    svc._scene_cache = scene_route.GraphCache(lambda: store, max_entries=8)
    svc.graph_db.list_maps.return_value = ["m1", "m2", "m3"]
    svc.database = _SwitchDb(off={"m2"})
    await asyncio.wait_for(svc.start_scene_prewarm(), 2.0)
    assert sorted(store.loads) == [("m1", 2), ("m3", 2)]
    assert svc.database.sql[0][1][1] == "scene_graph_enabled"


@pytest.mark.unit
async def test_prewarm_configured_maps_are_filtered_too_and_a_failed_query_loads_all(monkeypatch):
    monkeypatch.setattr(config, "SCENE_GRAPH_PLANNER_ENABLED", True)
    monkeypatch.setattr(config, "SCENE_GRAPH_PREWARM_MAPS", ("a", "b"))
    store = FakeStore(FakeGraph(), version=1)
    svc = _service()
    svc._scene_cache = scene_route.GraphCache(lambda: store, max_entries=8)
    svc.database = _SwitchDb(off={"a"})
    await asyncio.wait_for(svc.start_scene_prewarm(), 2.0)
    assert store.loads == [("b", 1)]
    store.loads.clear()
    svc2 = _service()
    svc2._scene_cache = scene_route.GraphCache(lambda: store, max_entries=8)
    svc2.database = _SwitchDb(fail=True)
    await asyncio.wait_for(svc2.start_scene_prewarm(), 2.0)
    assert sorted(store.loads) == [("a", 1), ("b", 1)]


class _Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


@pytest.mark.unit
async def test_failing_load_backs_off_exponentially_capped_and_resets_on_success(on):
    clock = _Clock()
    store = FakeStore(FakeGraph(), version=1)
    store.load_error = RuntimeError("boom")
    cache = scene_route.GraphCache(lambda: store, max_entries=8, load_timeout_s=1.0, clock=clock,
                                   backoff_base_s=10.0, backoff_max_s=35.0)
    with pytest.raises(scene_route.ChartFallback) as err:
        await cache.get("m")
    assert err.value.reason == "load_error" and len(store.loads) == 1
    for _ in range(3):                                  # within the 10 s: no new load, same reason
        with pytest.raises(scene_route.ChartFallback) as err:
            await cache.get("m")
        assert err.value.reason == "load_error"
    assert len(store.loads) == 1
    clock.t += 10.0                                     # window over: one retry, which fails
    with pytest.raises(scene_route.ChartFallback):
        await cache.get("m")
    assert len(store.loads) == 2
    clock.t += 19.0                                     # second window is 20 s
    with pytest.raises(scene_route.ChartFallback):
        await cache.get("m")
    assert len(store.loads) == 2
    clock.t += 1.0
    with pytest.raises(scene_route.ChartFallback):
        await cache.get("m")
    assert len(store.loads) == 3
    clock.t += 35.0                                     # third would be 40 s: capped at 35 s
    with pytest.raises(scene_route.ChartFallback):
        await cache.get("m")
    assert len(store.loads) == 4
    assert cache._failures["m"][1] - clock.t == 35.0
    store.load_error = None                             # recovered: loads, resets the back-off
    clock.t += 35.0
    graph, version = await cache.get("m")
    assert version == 1 and "m" not in cache._failures


@pytest.mark.unit
async def test_back_off_is_per_map_and_a_failed_refresh_is_not_retried_per_request(on):
    clock = _Clock()
    store = FakeStore(FakeGraph(), version=1)
    cache = scene_route.GraphCache(lambda: store, max_entries=8, load_timeout_s=1.0, clock=clock,
                                   backoff_base_s=10.0)
    await cache.get("m")
    store.version, store.load_error = 2, RuntimeError("boom")
    assert (await cache.get("m"))[1] == 1               # refresh starts, fails in the background
    await asyncio.sleep(0.1)
    n = len(store.loads)
    for _ in range(3):
        assert (await cache.get("m"))[1] == 1           # older version served, no new load
    assert len(store.loads) == n
    store.load_error = None
    assert (await cache.get("other"))[1] == 2           # another map is not held back
    clock.t += 10.0
    assert (await cache.get("m"))[1] == 1               # retry starts ...
    await asyncio.sleep(0.1)
    assert (await cache.get("m"))[1] == 2 and "m" not in cache._failures
