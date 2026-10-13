"""Chart-route hardening: one request deadline, bounded thread pools, cache identity (version
regression / epoch), unbounded-stale refusal, byte and per-map capacity, leaf-lookup errors, width
rule, per-reason counters. Fakes of the store and graph live in test_planner_chart_route."""
import asyncio
import math
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from packages import config
from packages.scene_graph.api import Route
from packages.services.mission_planner import scene_route as sr
from tests.unit.test_planner_chart_route import (  # noqa: F401  (`on` is a fixture)
    FakeGraph, FakeStore, _plan, _service, _width, on)
from tests.unit.test_scene_route import GridGraph, _l_route


class MetaStore(FakeStore):
    """FakeStore whose meta can block, count and carry an epoch."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.meta_gate = None
        self.meta_calls = 0
        self.epoch = None

    def meta(self, map_id):
        self.meta_calls += 1
        if self.meta_gate is not None:
            self.meta_gate.wait(5)
        out = super().meta(map_id)
        if self.epoch is not None and out:
            out = dict(out, epoch=self.epoch)
        return out


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


# ---- deadline: plan_timeout, budget_exhausted, store_timeout, robot_unavailable ---------------------------

@pytest.mark.unit
async def test_plan_timeout_falls_back_and_the_hung_plan_keeps_its_slot(on, monkeypatch):
    monkeypatch.setattr(config, "SCENE_GRAPH_PLAN_BUDGET_S", 0.3)
    gate = threading.Event()
    graph = FakeGraph()
    orig = graph.route

    def slow_route(a, b, blocked=None, start=None, goal=None):
        gate.wait(5)
        return orig(a, b, blocked, start=start, goal=goal)
    graph.route = slow_route
    svc = _service(FakeStore(graph))
    svc._plan_pool = sr.BoundedPool("sg-plan", 1, 1, busy_reason="planner_busy")
    out = await _plan(svc)
    assert out["planner_fallback"] == "plan_timeout" and out["success"]
    assert svc._plan_pool.inflight == 1                 # the thread still runs: its slot is held
    out = await _plan(svc)                              # a second plan would only pile up
    assert out["planner_fallback"] == "planner_busy" and out["success"]
    gate.set()
    for _ in range(50):
        if svc._plan_pool.inflight == 0:
            break
        await asyncio.sleep(0.02)
    assert svc._plan_pool.inflight == 0
    assert (await _plan(svc))["planner"] == sr.PLANNER_SCENE_GRAPH
    svc.cleanup()


@pytest.mark.unit
async def test_total_budget_cuts_a_cold_load_before_the_stage_timeout(on, monkeypatch):
    monkeypatch.setattr(config, "SCENE_GRAPH_TOTAL_BUDGET_S", 0.6)
    store = FakeStore(FakeGraph(), version=1)
    store.gate = threading.Event()
    svc = _service()
    svc._scene_cache = sr.GraphCache(lambda: store, load_timeout_s=5.0)
    t0 = time.monotonic()
    out = await _plan(svc)
    assert out["planner_fallback"] == "budget_exhausted" and out["success"]
    assert time.monotonic() - t0 < 1.5                  # not the 5 s stage timeout
    store.gate.set()
    svc.cleanup()


@pytest.mark.unit
async def test_almost_no_budget_falls_back_at_once_with_the_legacy_planner_intact(on, monkeypatch):
    monkeypatch.setattr(config, "SCENE_GRAPH_TOTAL_BUDGET_S", 0.2)     # under MIN_REMAINING_S
    store = FakeStore(FakeGraph())
    svc = _service(store)
    out = await _plan(svc)
    assert out["planner_fallback"] == "budget_exhausted" and out["success"]
    assert store.loads == [] and out["path"] == ["1", "2"]
    svc.cleanup()


@pytest.mark.unit
async def test_store_timeout_when_meta_hangs(on):
    store = MetaStore(FakeGraph())
    store.meta_gate = threading.Event()
    svc = _service()
    svc._scene_cache = sr.GraphCache(lambda: store, load_timeout_s=0.2)
    out = await _plan(svc)
    assert out["planner_fallback"] == "store_timeout" and out["success"]
    store.meta_gate.set()
    svc.cleanup()


@pytest.mark.unit
async def test_robot_unavailable_is_reported(on):
    svc = _service(FakeStore(FakeGraph()))
    svc.get_robot_status = AsyncMock(return_value=None)
    out = await _plan(svc)
    assert out["planner_fallback"] == "robot_unavailable"
    svc.cleanup()


@pytest.mark.unit
async def test_a_leaf_lookup_database_error_is_leaf_lookup_error_not_no_leaf(on):
    svc = _service(FakeStore(FakeGraph()))
    state = {"n": 0}
    ok = svc.graph_db.k_nearest_neighbors.side_effect

    def flaky(x, y, k, map_id):
        state["n"] += 1
        if state["n"] == 1:
            raise ConnectionError("arango down")
        return ok(x=x, y=y, k=k, map_id=map_id)
    svc.graph_db.k_nearest_neighbors.side_effect = flaky
    out = await _plan(svc)
    assert out["planner_fallback"] == "leaf_lookup_error" and out["success"]
    svc.cleanup()


@pytest.mark.unit
async def test_robot_row_is_read_once_when_the_chart_route_falls_back(on):
    graph = FakeGraph()
    graph.unreachable = True
    svc = _service(FakeStore(graph))
    out = await _plan(svc)
    assert out["planner_fallback"] == "unreachable" and out["success"]
    assert svc.get_robot_status.await_count == 1
    svc.cleanup()


# ---- dedicated executors --------------------------------------------------------------------------------

@pytest.mark.unit
async def test_a_saturated_default_executor_does_not_stall_the_chart_route(on):
    loop = asyncio.get_running_loop()
    release = threading.Event()
    n = (getattr(loop._default_executor, "_max_workers", None) or 64) + 8
    blockers = [loop.run_in_executor(None, release.wait, 10) for _ in range(min(n, 64))]
    await asyncio.sleep(0.05)
    svc = _service(FakeStore(FakeGraph()))
    t0 = time.monotonic()
    out = await _plan(svc)                              # the legacy path would also be fine; chart must serve
    assert out["planner"] == sr.PLANNER_SCENE_GRAPH
    assert time.monotonic() - t0 < 2.0
    release.set()
    await asyncio.gather(*blockers)
    svc.cleanup()


@pytest.mark.unit
async def test_concurrent_first_use_opens_the_store_once_and_meta_is_single_flight():
    opened = []

    def factory():
        opened.append(threading.get_ident())
        time.sleep(0.1)
        return store
    store = MetaStore(FakeGraph(), version=1)
    store.meta_gate = threading.Event()
    cache = sr.GraphCache(factory, max_entries=4, load_timeout_s=2.0)
    tasks = [asyncio.ensure_future(cache.get("m")) for _ in range(6)]
    await asyncio.sleep(0.3)
    store.meta_gate.set()
    results = await asyncio.gather(*tasks)
    assert len(opened) == 1 and store.meta_calls == 1
    assert {v for _, v in results} == {1} and len(store.loads) == 1
    cache.close()


@pytest.mark.unit
async def test_meta_answer_is_reused_for_its_ttl():
    clock = Clock()
    store = MetaStore(FakeGraph(), version=1)
    cache = sr.GraphCache(lambda: store, clock=clock, meta_ttl_s=1.0)
    await cache.get("m")
    await cache.get("m")
    assert store.meta_calls == 1
    clock.t += 1.5
    await cache.get("m")
    assert store.meta_calls == 2
    cache.close()


@pytest.mark.unit
async def test_loads_are_serialised_per_store():
    active, peak = [0], [0]
    lock = threading.Lock()

    class Overlap(FakeStore):
        def load(self, map_id, version=None):
            with lock:
                active[0] += 1
                peak[0] = max(peak[0], active[0])
            time.sleep(0.05)
            with lock:
                active[0] -= 1
            return self.graph
    cache = sr.GraphCache(lambda: Overlap(FakeGraph(), version=1), max_entries=8, load_timeout_s=2.0)
    await asyncio.gather(*(cache.warm(m) for m in ("a", "b", "c")))
    assert peak[0] == 1
    cache.close()


# ---- identity: version regression and epoch --------------------------------------------------------------

@pytest.mark.unit
async def test_version_regression_drops_the_maps_entries_and_reloads():
    store = MetaStore(FakeGraph(), version=5)
    cache = sr.GraphCache(lambda: store, max_entries=8)
    assert (await cache.get("m"))[1] == 5
    await cache.get("other")
    store.version = 2                                   # the map was dropped and rebuilt
    graph, version = await cache.get("m")
    assert version == 2 and ("m", 5) not in cache._graphs
    assert ("m", 2) in cache._graphs and ("other", 5) in cache._graphs
    assert [("m", 2)] == [l for l in store.loads if l[1] == 2]
    cache.close()


@pytest.mark.unit
async def test_epoch_change_with_the_same_version_number_reloads():
    store = MetaStore(FakeGraph(), version=1)
    store.epoch = "e1"
    cache = sr.GraphCache(lambda: store, max_entries=8)
    await cache.get("m")
    await cache.get("m")
    assert len(store.loads) == 1
    store.epoch = "e2"                                  # dropped and recreated: v1 of another history
    await cache.get("m")
    assert len(store.loads) == 2
    assert cache.cursor("m") is None
    cache.close()


@pytest.mark.unit
async def test_a_load_started_under_the_old_epoch_does_not_fill_the_cache():
    store = MetaStore(FakeGraph(), version=3)
    store.epoch = "e1"
    store.gate = threading.Event()
    cache = sr.GraphCache(lambda: store, max_entries=8, load_timeout_s=0.1)
    with pytest.raises(sr.ChartFallback):
        await cache.get("m")                            # load of (m, 3, e1) hangs
    store.epoch, store.version = "e2", 1
    store.gate.set()
    await asyncio.sleep(0.05)
    graph, version = await cache.get("m")               # epoch changed: old load ignored
    assert version == 1 and ("m", 3) not in cache._graphs
    await asyncio.sleep(0.1)
    assert ("m", 3) not in cache._graphs
    cache.close()


# ---- unbounded stale serving -----------------------------------------------------------------------------

@pytest.mark.unit
async def test_older_version_is_refused_once_a_newer_one_has_been_known_too_long():
    clock = Clock()
    store = MetaStore(FakeGraph(), version=1)           # no cursor: the lag check can not help
    cache = sr.GraphCache(lambda: store, max_entries=8, load_timeout_s=1.0, clock=clock,
                          max_stale_s=60.0)
    assert (await cache.get("m"))[1] == 1
    store.version = 2
    store.gate = threading.Event()                      # v2 never finishes loading
    assert (await cache.get("m"))[1] == 1               # first seen: served
    clock.t += 59.0
    assert (await cache.get("m"))[1] == 1
    clock.t += 2.0
    with pytest.raises(sr.ChartFallback) as err:
        await cache.get("m")
    assert err.value.reason == "stale_graph"
    store.gate.set()
    await asyncio.sleep(0.1)
    assert (await cache.get("m"))[1] == 2
    cache.close()


# ---- capacity -------------------------------------------------------------------------------------------

@pytest.mark.unit
async def test_global_lru_evicts_the_least_recently_used_map():
    store = FakeStore(FakeGraph(), version=1)
    cache = sr.GraphCache(lambda: store, max_entries=2)
    for m in ("a", "b"):
        await cache.get(m)
    await cache.get("a")                                # a is now the most recent
    await cache.get("c")
    assert sorted(cache._graphs) == [("a", 1), ("c", 1)]
    cache.close()


@pytest.mark.unit
async def test_versions_per_map_is_configurable():
    store = FakeStore(FakeGraph(), version=1)
    cache = sr.GraphCache(lambda: store, max_entries=8, versions_per_map=1)
    await cache.get("m")
    store.version = 2
    await cache.get("m")                                # v1 serves, v2 loads
    await asyncio.sleep(0.1)
    await cache.get("m")
    assert sorted(cache._graphs) == [("m", 2)]
    cache.close()


@pytest.mark.unit
async def test_byte_budget_uses_the_graphs_nbytes_when_it_has_one():
    class Sized:
        def __init__(self, n):
            self.nbytes = n
    store = FakeStore(Sized(100), version=1)
    cache = sr.GraphCache(lambda: store, max_entries=8, max_bytes=150)
    await cache.get("a")
    await cache.get("b")
    assert sorted(cache._graphs) == [("b", 1)]          # 200 > 150: the older one went
    assert cache.stats()["bytes"] == 100
    plain = sr.GraphCache(lambda: FakeStore(FakeGraph(), version=1), max_entries=8, max_bytes=1)
    await plain.get("a")
    await plain.get("b")
    assert len(plain._graphs) == 2                      # no nbytes: not budgeted
    cache.close()
    plain.close()


@pytest.mark.unit
async def test_prewarm_warms_at_most_the_cache_size_and_says_so(monkeypatch, caplog):
    monkeypatch.setattr(config, "SCENE_GRAPH_PLANNER_ENABLED", True)
    monkeypatch.setattr(config, "SCENE_GRAPH_PREWARM_MAPS", ("a", "b", "c"))
    monkeypatch.setattr(config, "SCENE_GRAPH_CACHE_VERSIONS", 2)
    store = FakeStore(FakeGraph(), version=1)
    svc = _service()
    svc._scene_cache = sr.GraphCache(lambda: store, max_entries=2)
    with caplog.at_level("WARNING"):
        await asyncio.wait_for(svc.start_scene_prewarm(), 2.0)
    assert sorted(store.loads) == [("a", 1), ("b", 1)]
    assert any("SCENE_GRAPH_CACHE_VERSIONS" in r.message for r in caplog.records)
    svc.cleanup()


# ---- end-stub merge, width rule ---------------------------------------------------------------------------

@pytest.mark.unit
def test_end_stub_shorter_than_min_is_merged_into_the_previous_leg():
    graph = FakeGraph()
    plan = sr.plan_on_graph(graph, "m", (0.05, 0.0), (9.95, 0.0), ([], []),
                            lambda x, y: f"n{x:.2f}", 0.5, 1.5, 4.8)
    xs = [round(w.x, 2) for w in plan.waypoints]
    assert xs == [0.05, 4.85, 9.95]                     # without the merge: ..., 9.65, 9.95
    assert len(set(plan.planned_path)) == 3


class LGraph(GridGraph):
    """The L corridor with a route of a given narrowest width."""

    def __init__(self, min_width, nodes=None):
        super().__init__()
        self.min_width = min_width
        self.nodes = nodes if nodes is not None else _l_route()

    def route(self, a, b, blocked=None, start=None, goal=None):
        return Route(charts=["c"], length=18.0, min_width=self.min_width, nodes=self.nodes)


def _l_plan(graph, width, length, margin=0.05, goal=(9.55, 9.5)):
    return sr.plan_on_graph(graph, "m", (0.55, 0.55), goal, ([], []),
                            lambda x, y: f"{x:.2f},{y:.2f}", 0.5, 1.5, 5.0,
                            robot_width_m=width, width_margin_m=margin, robot_length_m=length)


@pytest.mark.unit
def test_width_rule_aligned_needs_width_plus_margin_turning_needs_the_diagonal():
    # the L route turns 90 degrees at the elbow: the robot may rotate there
    need_turn = math.hypot(0.8, 0.5) + 0.05             # 0.943
    with pytest.raises(sr.ChartFallback) as err:
        _l_plan(LGraph(0.9), 0.5, 0.8)                  # 0.9 >= 0.55 aligned, < 0.943 turning
    assert err.value.reason == "too_narrow"
    assert _l_plan(LGraph(need_turn + 0.01), 0.5, 0.8).min_width > need_turn
    # an aligned-only requirement (a straight run on one arm) passes at the aligned width
    arm = [((i + 0.5) * 0.1, 0.55) for i in range(5, 95)]
    straight = sr.plan_on_graph(LGraph(0.6, arm), "m", (0.55, 0.55), (9.55, 0.55), ([], []),
                                lambda x, y: f"{x:.2f}", 0.5, 1.5, 5.0, robot_width_m=0.5,
                                width_margin_m=0.05, robot_length_m=0.8)
    assert straight.waypoints
    with pytest.raises(sr.ChartFallback):               # even aligned: 0.5 + 0.05 > 0.54
        _l_plan(LGraph(0.54), 0.5, 0.8)


@pytest.mark.unit
def test_unknown_width_or_length_degrades_without_rejecting():
    assert _l_plan(LGraph(0.1), None, None).waypoints               # unknown width: never rejects
    with pytest.raises(sr.ChartFallback):
        _l_plan(LGraph(0.6), 0.5, None)                 # no length: square footprint, 0.5*sqrt2+0.05 = 0.757


@pytest.mark.unit
async def test_service_passes_length_and_width_when_the_check_is_enabled(on, monkeypatch):
    monkeypatch.setattr(config, "SCENE_GRAPH_WIDTH_CHECK_ENABLED", True)
    svc = _service(FakeStore(FakeGraph()))               # straight route, min_width 0.8
    _width(svc, 0.7)
    svc.get_robot_status.return_value.status.factsheet.length = 1.0
    assert (await _plan(svc))["planner"] == sr.PLANNER_SCENE_GRAPH   # aligned: 0.7 + 0.05 <= 0.8
    _width(svc, 0.78)
    out = await _plan(svc)
    assert out["planner_fallback"] == "too_narrow"
    svc.cleanup()


# ---- counters, config -------------------------------------------------------------------------------------

@pytest.mark.unit
async def test_stats_count_each_outcome_by_reason(on):
    graph = FakeGraph()
    svc = _service(FakeStore(graph))
    await _plan(svc)
    graph.unreachable = True
    await _plan(svc)
    await _plan(svc)
    stats = svc.get_stats()["scene_graph"]
    assert stats["outcomes"] == {"served": 1, "unreachable": 2}
    assert stats["cache"]["graphs"] == 1 and stats["plan_inflight"] == 0
    svc.cleanup()


@pytest.mark.unit
def test_config_validation_resets_bad_planner_settings_with_one_warning(monkeypatch, caplog):
    monkeypatch.setattr(config, "SCENE_GRAPH_LOAD_TIMEOUT_S", 0.0)
    monkeypatch.setattr(config, "SCENE_GRAPH_TOTAL_BUDGET_S", float("nan"))
    monkeypatch.setattr(config, "SCENE_GRAPH_CACHE_VERSIONS", 0)
    monkeypatch.setattr(config, "SCENE_GRAPH_META_TTL_S", -1.0)
    with caplog.at_level("WARNING", logger="config"):
        config._validate_scene_graph_planner()
    assert config.SCENE_GRAPH_LOAD_TIMEOUT_S == 2.0 and config.SCENE_GRAPH_TOTAL_BUDGET_S == 3.5
    assert config.SCENE_GRAPH_CACHE_VERSIONS == 4 and config.SCENE_GRAPH_META_TTL_S == 0.75
    warnings = [r for r in caplog.records if r.name == "config"]
    assert len(warnings) == 1 and "SCENE_GRAPH_LOAD_TIMEOUT_S" in warnings[0].getMessage()


@pytest.mark.unit
def test_defaults_of_the_new_settings():
    assert config.SCENE_GRAPH_TOTAL_BUDGET_S == 3.5
    assert config.SCENE_GRAPH_CACHE_VERSIONS_PER_MAP == 2
    assert config.SCENE_GRAPH_CACHE_MAX_BYTES == 0
    assert config.SCENE_GRAPH_WIDTH_MARGIN_M == 0.05
    assert config.SCENE_GRAPH_WIDTH_CHECK_ENABLED is False
