"""Turn a scene-graph lattice route into the planner's plain waypoint poses.

Pure functions, no I/O and no engine import: the planner supplies the line-of-sight test and the
nearest-leaf lookup. The output contract is the one the dispatcher, leg tracker and edge statistics
already rely on (docs/scene_graph/integration.md, "Planner contract"):

* waypoints are `Pose2D` in the map frame, `theta` = bearing to the next waypoint, the last one
  repeats the previous bearing (the request carries no goal heading; the robot's final heading is
  the controller's business);
* `node_id` = the nearest leaf within a threshold, else None;
* `chart_key` = the chart that owns the waypoint's place on the graph the plan was made on (the plan only
  knows the ordered list of charts it crosses, not which waypoint is in which, so `chart_of` asks the
  graph: `graph.snap` of the waypoint, an array lookup per waypoint); None where the graph has no chart
  within reach. Recording data (run_legs.from_chart / to_chart), never sent to the robot;
* `planned_path` is returned only when every waypoint has a leaf id, because the leg tracker pairs
  `waypoints[i]` with `planned_path[i]` and drops topomap ids on any length mismatch.
"""
import asyncio
import logging
import math
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from cloud_common.objects import common
from packages import config
from packages.services.mission_planner import node_policy_plan, trajectory
from packages.services.mission_planner.waypoints import fewest_waypoints, simplify, thin_eps  # noqa: F401  (re-exported)

Pt = Tuple[float, float]

PLANNER_SCENE_GRAPH = "scene_graph"
PLANNER_LEGACY = "legacy"


def build_waypoints(points: Sequence[Pt], map_id: str,
                    node_of: Callable[[float, float], Optional[str]],
                    allow_repeats: bool = False,
                    chart_of: Optional[Callable[[float, float], Optional[str]]] = None
                    ) -> Tuple[List[common.Pose2D], Optional[List[str]]]:
    """(waypoints, planned_path or None) for an already simplified point list. `chart_of` (optional) gives each
    waypoint its `chart_key`.
    `planned_path` is None unless every waypoint has a leaf id, and (unless `allow_repeats`) a
    distinct one. The last waypoint repeats the previous bearing (a one-point route has theta 0)."""
    pts = list(points)
    poses: List[common.Pose2D] = []
    for i, (x, y) in enumerate(pts):
        if i + 1 < len(pts):
            nx, ny = pts[i + 1]
            theta = math.atan2(ny - y, nx - x)
        elif i > 0:
            theta = poses[-1].theta
        else:
            theta = 0.0
        poses.append(common.Pose2D(x=x, y=y, theta=theta, map_id=map_id, node_id=node_of(x, y),
                                   chart_key=chart_of(x, y) if chart_of is not None else None))
    ids = [p.node_id for p in poses]
    # one id per waypoint, no repeats: two waypoints snapped to the same leaf would be a zero-length
    # leg for the leg tracker, so such a route carries no planned_path (its legs are VDA-id only).
    planned = ([str(n) for n in ids]
               if poses and all(n is not None for n in ids)
               and (allow_repeats or len(set(ids)) == len(ids)) else None)
    return poses, planned


def chart_key_at(graph: Any, x: float, y: float) -> Optional[str]:
    """The chart owning the navigable cell nearest (x, y) on `graph` (`graph.snap`, up to the chart diameter), as a
    string; None when there is none or the graph cannot say. A waypoint of a route that was just planned lies on a
    navigable cell, so this is its own chart. Never raises, except TileUnavailable (a lazy graph's tile that cannot
    be read: the caller's fallback handles it)."""
    from packages.services.mission_planner.lazy_plan import TileUnavailable
    try:
        key = graph.snap(x, y).key
    except TileUnavailable:
        raise
    except Exception as err:  # pylint: disable=broad-except
        _log.warning("chart lookup failed at (%.2f, %.2f): %s", x, y, err)
        return None
    return None if key is None else str(key)


def repair_leaf_ids(points: Sequence[Pt], ids: Sequence[Optional[str]],
                    visible: Callable[[Pt, Pt], bool], max_gap: float = 0.0
                    ) -> Tuple[List[Pt], List[Optional[str]]]:
    """Make leaf ids distinct by dropping interior waypoints: an interior point whose id is None or
    already used by a kept point goes when the kept predecessor sees its successor (and the new leg
    stays within `max_gap`). Both ends are always kept; a duplicate end drops the kept point before
    it the same way. What cannot be repaired stays (the caller then finds the ids not distinct)."""
    n = len(points)
    if n <= 2:
        return list(points), list(ids)
    kp: List[Pt] = [points[0]]
    ki: List[Optional[str]] = [ids[0]]
    used = {ids[0]} if ids[0] is not None else set()

    def can_join(a: Pt, b: Pt) -> bool:
        return (max_gap <= 0 or math.dist(a, b) <= max_gap) and visible(a, b)

    for i in range(1, n - 1):
        if ids[i] is not None and ids[i] not in used:
            kp.append(points[i]); ki.append(ids[i]); used.add(ids[i])
        elif not can_join(kp[-1], points[i + 1]):
            kp.append(points[i]); ki.append(ids[i])
            if ids[i] is not None:
                used.add(ids[i])
    # the goal end: when its id repeats a kept interior point, drop that point and the ones after
    # it if the point before them sees the goal (a repeat of the start id is a real collision)
    end_id = ids[-1]
    if end_id is not None and end_id in ki[1:]:
        j = ki.index(end_id, 1)
        if can_join(kp[j - 1], points[-1]):
            del kp[j:]
            del ki[j:]
    kp.append(points[-1]); ki.append(ids[-1])
    return kp, ki


def collapse_repeated_leaf_ids(points: Sequence[Pt], ids: Sequence[Optional[str]],
                               visible: Callable[[Pt, Pt], bool], max_gap: float = 0.0
                               ) -> Tuple[List[Pt], List[Optional[str]]]:
    """Last step of the leaf-id repair (after `repair_leaf_ids`): merge what is still repeated.

    Consecutive waypoints on one leaf are one waypoint where the line of sight allows (the earlier
    interior one goes, the end points stay). A whole route that sits on ONE leaf (every waypoint
    within the snap radius of the same leaf: a short hop between sparse leaves, start and goal on
    one leaf) becomes the minimal two-waypoint path [start, goal] when the start sees the goal
    (and the gap cap allows it), else it is left as is. A repeat that cannot be merged without
    cutting a corner stays: the geometry is what is served, the caller accepts repeated ids (a
    leg between two waypoints of one leaf is not an edge: edge statistics ignore src == dst).
    A route with a waypoint without a leaf is returned unchanged."""
    pts, out = list(points), list(ids)
    if len(pts) < 2 or any(i is None for i in out):
        return pts, out
    if len(set(out)) == 1:
        a, b = pts[0], pts[-1]
        if a == b or ((max_gap <= 0 or math.dist(a, b) <= max_gap) and visible(a, b)):
            return ([a, b] if a != b else [b]), ([out[0], out[-1]] if a != b else [out[-1]])
        return pts, out
    kp: List[Pt] = [pts[0]]
    ki: List[Optional[str]] = [out[0]]
    for i in range(1, len(pts)):
        if out[i] == ki[-1] and i < len(pts) - 1 and len(kp) >= 1:
            nxt = pts[i + 1]
            if (max_gap <= 0 or math.dist(kp[-1], nxt) <= max_gap) and visible(kp[-1], nxt):
                continue                      # the later repeat goes; the next one is tested from kp[-1]
        kp.append(pts[i])
        ki.append(out[i])
    return kp, ki


# Why a start or goal cannot be used (failed_at "find_robot_node" / "find_target_node" text).
SNAP_NO_SPACE = "no_navigable_space"
SNAP_TOO_FAR = "too_far"
SNAP_CROSSES_BLOCKED = "crosses_blocked"
BLOCKED_BY_KEEPOUT = "blocked_by_keepout"   # ChartFallback reason: only the operator's keep-out zones cut the goal off (never fallen back on)


def unverified_length_m(graph: Any, nodes: List[Pt]) -> Optional[float]:
    """Metres of the route's lattice path that close evidence does not validate: PlanGraph.unverified_length over the path's
    global cells (ix, iy) = floor(centre / lattice). None when the graph cannot say (no such method, or a store made without
    the close-evidence rule: its validity plane does not exist), not "the whole route"."""
    fn = getattr(graph, "unverified_length", None)
    lattice = getattr(getattr(graph, "params", None), "lattice", None)
    if fn is None or not lattice:
        return None
    cells = [(math.floor(x / lattice), math.floor(y / lattice)) for x, y in nodes]
    try:
        return float(fn(cells))
    except ValueError:
        return None


def snap_problem(snap: Any, max_dist: float) -> Optional[str]:
    """None when `snap` (the library's Snap: key, distance, crosses_blocked) may be used, else the
    reason. The planner never moves a point across a known blocked cell or further than `max_dist`
    (a start is the robot's own position: small; a goal may be tapped in a wall: a little larger)."""
    if getattr(snap, "key", None) is None or not math.isfinite(getattr(snap, "distance", math.inf)):
        return SNAP_NO_SPACE if not math.isfinite(getattr(snap, "distance", math.inf)) else SNAP_TOO_FAR
    if snap.distance > max_dist:
        return SNAP_TOO_FAR
    if getattr(snap, "crosses_blocked", False):
        return SNAP_CROSSES_BLOCKED
    return None


# ---------------------------------------------------------------------------------------------
# Chart-graph route: version-pinned graph cache and the plan itself (feature-flagged in server.py).
# Every failure is a `ChartFallback(reason)`; the caller then runs the legacy planner unchanged.
# ---------------------------------------------------------------------------------------------
_log = logging.getLogger("MissionPlanner.scene_route")


MIN_END_STUB_M = 0.5
# Less than this of the request's budget left at a stage start: fall back now, the legacy planner
# keeps its share of the dispatcher's replan timeout.
MIN_REMAINING_S = 0.3
# A heading change between two consecutive waypoints above this is a turn in place for the width
# rule (the controller may rotate on the spot at a waypoint; below it, it steers through).
TURN_IN_PLACE_RAD = math.radians(30.0)


class ChartFallback(Exception):
    """The chart route cannot serve this request; `reason` is a short machine-readable text."""

    def __init__(self, reason: str, info: Optional[Dict[str, Any]] = None):
        super().__init__(reason)
        self.reason = reason
        self.info = info          # what the reason carries for the caller (blocked_by_keepout: the zones)


@dataclass
class ChartPlan:
    waypoints: List[common.Pose2D]
    planned_path: Optional[List[str]]  # one leaf id per waypoint; None when an end has no leaf in reach
    charts: List[str]
    min_width: float
    goal_xy: Pt                        # snapped goal cell centre
    repeated_leaf_ids: bool = False    # some consecutive waypoints share a leaf id (not mergeable)
    # nodePolicy skip data: {"total", "skippable", "unavailable"}; None when it was not asked for
    node_policy: Optional[Dict[str, Any]] = None
    # clearance pricing of this plan: {kappa, margin, rate, margin_source ("factsheet" | "map_r"), cost (Route.cost), exact
    # (the route call's own flag), turn (`turn_record`)}; None when the plan is neither priced nor turn-costed (rate 0 and a
    # turn cost: the clearance fields are 0 / None, profile "none")
    pricing: Optional[Dict[str, Any]] = None
    # the lattice path cut per leg (trajectory.split): [{from_index, to_index, points}], None when off, over the cap
    # or not derivable
    trajectory: Optional[List[Dict[str, Any]]] = None
    # the waypoint budget's record {waypoint_budget_m, waypoints_removed (against the greedy pull of the same plan)}; None when off
    waypoint_budget: Optional[Dict[str, Any]] = None
    # the goal lies beyond goal_snap_m from free space: {goal_xy (the tap), free_xy (the snapped free cell), distance_from_free_m,
    # unverified_length_m (metres of the route not validated by close evidence, PlanGraph.unverified_length; None when the store
    # has no validity plane), limit_m}; None when within
    goal_far: Optional[Dict[str, Any]] = None
    # the map has keep-out zones: {applied: [ids], unresolved: [{id, reason}], detour (a zone forced a longer route), forced_by:
    # [ids of the zones the unconstrained route would cross], extra_length_m}; None when the map has none
    keepout: Optional[Dict[str, Any]] = None
    # `model_hash:stamp` of the region cost snapshot the plan's predicted times come from (pricing["region"]["cost_model"]); None when
    # no snapshot was used (mode off, no usable statistics). Recording data, never part of the route digest
    cost_model: Optional[str] = None


class BoundedPool:
    """A small thread pool of its own (the default executor is shared by everything else in the
    service) with a cap on tasks queued or running: a hung call cannot pile work up without bound,
    the caller falls back `busy_reason` instead. The count drops when the task ENDS (also when the
    awaiting request gave up on it), so a plan that outlives its `wait_for` keeps its slot."""

    def __init__(self, name: str, workers: int, max_inflight: Optional[int] = None,
                 busy_reason: str = "busy"):
        self.name = name
        self._ex = ThreadPoolExecutor(max_workers=max(1, int(workers)), thread_name_prefix=name)
        self._cap = None if max_inflight is None else max(1, int(max_inflight))
        self._busy = busy_reason
        self._n = 0
        self._lock = threading.Lock()

    @property
    def inflight(self) -> int:
        return self._n

    def _release(self, _fut) -> None:
        with self._lock:
            self._n -= 1

    def submit(self, fn: Callable, *args) -> "asyncio.Future":
        """Awaitable result of `fn(*args)` on this pool; ChartFallback(busy_reason) when full."""
        with self._lock:
            if self._cap is not None and self._n >= self._cap:
                raise ChartFallback(self._busy)
            self._n += 1
        try:
            cf = self._ex.submit(fn, *args)
        except RuntimeError as err:                 # pool shut down
            self._release(None)
            raise ChartFallback("executor_closed") from err
        cf.add_done_callback(self._release)
        return asyncio.wrap_future(cf, loop=asyncio.get_running_loop())

    def close(self) -> None:
        self._ex.shutdown(wait=False, cancel_futures=True)


class GraphCache:
    """Loaded scene graphs by (map, version), a small LRU. `store_factory()` returns the store (called
    lazily, in a worker thread, because it connects). `get` never waits longer than `load_timeout_s`
    nor past the request's `deadline`: a cold load keeps running in its thread, shared by later
    requests, and fills the cache when it ends; this request falls back. A load that FAILS backs the
    map off (exponential from `backoff_base_s`, capped at `backoff_max_s`, reset by the next
    successful load): until it ends no new load is started for the map's versions, a request with an
    older version cached is served from it and one without falls back `load_error`.

    Identity: the store's `meta` answer names a version and (feature-detected) an `epoch`, else its
    `generation`. Version numbers restart after a drop and a rebuild, so when a map's epoch token
    changes or its version REGRESSES every entry of the map (graphs, cursors, served marks, loads in
    flight) is dropped: no graph of another history is ever served under a version number.

    Staleness: an older version is served while a newer one loads, but only for `max_stale_s` after
    the newer version was first seen (0 = unbounded, the caller's cursor lag check then decides);
    beyond it `get` raises ChartFallback("stale_graph").

    Threads: loads run on their own 2-thread pool and serialise on a lock (the store keeps shared
    caches); meta, store opening and the caller's lag queries run on a second bounded pool; the
    default executor is never used. A meta answer is cached for `meta_ttl_s` and shared by
    concurrent requests (one query in flight per map). The memory bound is `max_entries` graphs in
    all, `versions_per_map` of one map, and `max_bytes` of reported `nbytes` (0 = off)."""

    RETRY_STORE_S = 30.0
    BACKOFF_BASE_S = 5.0
    BACKOFF_MAX_S = 300.0

    def __init__(self, store_factory: Callable[[], Any], max_entries: int = 4,
                 load_timeout_s: float = 2.0, clock: Callable[[], float] = None,
                 versions_per_map: int = 2, backoff_base_s: Optional[float] = None,
                 backoff_max_s: Optional[float] = None, max_stale_s: float = 0.0,
                 meta_ttl_s: float = 0.0, max_bytes: int = 0, load_workers: int = 2,
                 io_workers: int = 4, lazy: bool = False, lazy_kw: Optional[Dict[str, Any]] = None):
        self._lazy = bool(lazy)                     # True (the service always): PlanGraph; False: store.load, a test seam
        self._lazy_kw = dict(lazy_kw or {})
        self._factory = store_factory
        self._max = max(1, int(max_entries))
        self._per_map = max(1, int(versions_per_map))
        self._max_bytes = max(0, int(max_bytes))
        self._max_stale = max(0.0, float(max_stale_s))
        self._meta_ttl = max(0.0, float(meta_ttl_s))
        self._timeout = float(load_timeout_s)
        self._clock = clock or time.monotonic
        self._store: Any = None
        self._store_lock = threading.Lock()         # single-flight store open
        self._load_lock = threading.Lock()          # one store.load at a time
        self._store_failed_at: Optional[float] = None
        self._load_pool = BoundedPool("sg-load", load_workers)
        self._io_pool = BoundedPool("sg-io", io_workers, 4 * max(1, int(io_workers)),
                                    busy_reason="store_busy")
        self._graphs: "OrderedDict[Tuple[str, int], Any]" = OrderedDict()
        self._sizes: Dict[Tuple[str, int], int] = {}
        self._loading: Dict[Tuple[str, int], "asyncio.Future"] = {}
        self._cursors: Dict[Tuple[str, int], Any] = {}   # (map, version) -> its ingest cursor
        self._served: Dict[str, int] = {}                # map -> version `get` last returned
        self._known: Dict[str, Tuple[int, Any]] = {}     # map -> (current version, epoch token)
        self._first_seen: Dict[Tuple[str, int], float] = {}   # (map, version) -> when first known
        self._meta_cache: Dict[str, Tuple[float, dict]] = {}
        self._meta_inflight: Dict[str, "asyncio.Future"] = {}
        self._backoff_base = self.BACKOFF_BASE_S if backoff_base_s is None else float(backoff_base_s)
        self._backoff_max = self.BACKOFF_MAX_S if backoff_max_s is None else float(backoff_max_s)
        self._failures: Dict[str, Tuple[int, float]] = {}   # map -> (consecutive failures, retry at)

    # ---- plumbing ----------------------------------------------------------------------------
    def io(self, fn: Callable, *args) -> "asyncio.Future":
        """`fn(*args)` on the bounded I/O pool (never the default executor); awaitable."""
        return self._io_pool.submit(fn, *args)

    def close(self) -> None:
        self._load_pool.close()
        self._io_pool.close()

    def stats(self) -> Dict[str, Any]:
        return {"graphs": len(self._graphs), "bytes": sum(self._sizes.values()),
                "loading": len(self._loading), "load_inflight": self._load_pool.inflight,
                "io_inflight": self._io_pool.inflight, "backed_off_maps": len(self._failures)}

    def _open_store(self):
        if self._store is not None:
            return self._store
        with self._store_lock:                      # callers arrive from several pool threads
            if self._store is not None:
                return self._store
            if self._store_failed_at is not None and \
                    self._clock() - self._store_failed_at < self.RETRY_STORE_S:
                raise ChartFallback("store_unavailable")
            try:
                self._store = self._factory()
            except Exception as err:  # pylint: disable=broad-except
                self._store_failed_at = self._clock()
                _log.warning("scene graph store unavailable: %s", err)
                raise ChartFallback("store_unavailable") from err
            return self._store

    def _load_serialised(self, store, map_id: str, version: int):
        with self._load_lock:
            if self._lazy:                      # the manifest only; tiles are read per request
                from packages.services.mission_planner.lazy_plan import open_plan
                return open_plan(store, map_id, version, **self._lazy_kw)
            return store.load(map_id, version)

    async def _meta(self, store, map_id: str, timeout: Optional[float]) -> dict:
        """store.meta(map): the cached answer while younger than the TTL, else the one query in
        flight per map (shared by every waiter). Raises what the query raises."""
        hit = self._meta_cache.get(map_id)
        if hit is not None and self._clock() - hit[0] < self._meta_ttl:
            return hit[1]
        fut = self._meta_inflight.get(map_id)
        if fut is None:
            fut = self._io_pool.submit(store.meta, map_id)
            self._meta_inflight[map_id] = fut

            def done(f, map_id=map_id):
                if self._meta_inflight.get(map_id) is f:
                    del self._meta_inflight[map_id]
                if not f.cancelled() and f.exception() is None:
                    self._meta_cache[map_id] = (self._clock(), f.result() or {})
            fut.add_done_callback(done)
        return await asyncio.wait_for(asyncio.shield(fut), timeout) or {}

    # ---- identity ----------------------------------------------------------------------------
    @staticmethod
    def _token(meta: dict) -> Any:
        """What names the history the version numbers belong to: the store's epoch when it reports
        one (a drop and recreate restarts the numbers under a new epoch), else the generation."""
        if meta.get("epoch") is not None:
            return ("epoch", str(meta["epoch"]))
        if meta.get("generation") is not None:
            return ("generation", meta["generation"])
        return None

    def _drop_map(self, map_id: str) -> None:
        for key in [k for k in self._graphs if k[0] == map_id]:
            del self._graphs[key]
            self._sizes.pop(key, None)
        for table in (self._cursors, self._first_seen, self._loading):
            for key in [k for k in table if k[0] == map_id]:
                del table[key]          # a load in flight finishes into nothing (see `done`)
        self._served.pop(map_id, None)
        self._failures.pop(map_id, None)
        self._known.pop(map_id, None)

    def _note_meta(self, map_id: str, version: int, token: Any) -> None:
        known = self._known.get(map_id)
        if known is not None and (token != known[1] or version < known[0]):
            why = "epoch changed" if token != known[1] else f"version regressed {known[0]} -> {version}"
            _log.warning("scene graph of map %s: %s; cached graphs dropped", map_id, why)
            self._drop_map(map_id)
        self._known[map_id] = (version, token)
        self._first_seen.setdefault((map_id, version), self._clock())
        cached = [k[1] for k in self._graphs if k[0] == map_id]
        floor = min(cached) if cached else version
        for key in [k for k in self._first_seen if k[0] == map_id and k[1] < floor]:
            del self._first_seen[key]

    # ---- bookkeeping -------------------------------------------------------------------------
    def _backed_off(self, map_id: str) -> bool:
        failure = self._failures.get(map_id)
        return failure is not None and self._clock() < failure[1]

    def _note_failure(self, map_id: str) -> None:
        count = self._failures.get(map_id, (0, 0.0))[0] + 1
        delay = min(self._backoff_max, self._backoff_base * (2 ** min(count - 1, 30)))
        self._failures[map_id] = (count, self._clock() + delay)

    def _drop_unavailable(self) -> None:
        """Evict the graphs flagged `unavailable` (a lazy graph that failed to read a tile: its version was garbage
        collected, or the backend failed): the next request re-opens the map's current version."""
        for key in [k for k, g in self._graphs.items() if getattr(g, "unavailable", False) is True]:
            _log.warning("scene graph %s dropped: a tile could not be read", key)
            self._evict(key)
            if self._served.get(key[0]) == key[1]:
                del self._served[key[0]]

    def _evict(self, key) -> None:
        self._graphs.pop(key, None)
        self._sizes.pop(key, None)

    def _insert(self, key, graph) -> None:
        self._failures.pop(key[0], None)
        self._graphs[key] = graph
        self._graphs.move_to_end(key)
        size = getattr(graph, "nbytes", None)          # feature-detected; absent: not budgeted
        self._sizes[key] = int(size) if isinstance(size, (int, float)) \
            and not isinstance(size, bool) and size > 0 else 0
        # at most `versions_per_map` graphs of one map (lowest versions go first), then the global
        # LRU, then the byte budget; the graph just inserted is never the victim.
        same = sorted((k for k in self._graphs if k[0] == key[0] and k != key), key=lambda k: k[1])
        while len(same) + 1 > self._per_map:
            self._evict(same.pop(0))
        while len(self._graphs) > self._max:
            self._evict(next(k for k in self._graphs if k != key))
        while self._max_bytes and len(self._graphs) > 1 \
                and sum(self._sizes.values()) > self._max_bytes:
            self._evict(next(k for k in self._graphs if k != key))

    def _start_load(self, store, key) -> "asyncio.Future":
        """The single-flight load of `key`: started once, shared by every waiter, fills the cache
        when it ends (also after the request that started it gave up)."""
        fut = self._loading.get(key)
        if fut is not None:
            return fut
        fut = self._load_pool.submit(self._load_serialised, store, key[0], key[1])
        self._loading[key] = fut

        def done(f, key=key):
            if self._loading.get(key) is not f:       # the map's history was dropped meanwhile
                if not f.cancelled():
                    f.exception()
                return
            del self._loading[key]
            if f.cancelled() or f.exception() is not None:
                if not f.cancelled():
                    self._note_failure(key[0])
                    _log.warning("scene graph load %s failed: %s", key, f.exception())
                return
            self._insert(key, f.result())
        fut.add_done_callback(done)
        return fut

    def _serve_older(self, map_id: str, version: int, tolerate_stale: bool = False):
        """(graph, version) of the newest cached version below `version`, None when there is none;
        ChartFallback("stale_graph") when a newer one has been known for more than `max_stale_s` (unless `tolerate_stale`)."""
        older = [k for k in self._graphs if k[0] == map_id and k[1] < version]
        if not older:
            return None
        prev = max(older, key=lambda k: k[1])
        if self._max_stale > 0 and not tolerate_stale:
            since = [t for (m, v), t in self._first_seen.items() if m == map_id and v > prev[1]]
            if since and self._clock() - min(since) > self._max_stale:
                raise ChartFallback("stale_graph")
        self._graphs.move_to_end(prev)
        self._served[map_id] = prev[1]
        return self._graphs[prev], prev[1]

    # ---- API ---------------------------------------------------------------------------------
    async def warm(self, map_id: str) -> Optional[int]:
        """Load the map's current version into the cache, waiting as long as it takes (no request
        timeout; for pre-warming). Returns the version, None when the map has no graph. Raises on
        a store / load error."""
        store = await self._io_pool.submit(self._open_store)
        meta = await self._meta(store, map_id, None)
        version = meta.get("version")
        if version is None:
            return None
        key = (map_id, int(version))
        self._note_meta(map_id, key[1], self._token(meta))
        self._cursors[key] = meta.get("cursor")
        if key not in self._graphs:
            await asyncio.shield(self._start_load(store, key))
        return key[1]

    def cursor(self, map_id: str) -> Any:
        """The ingest cursor `[costmap_stored_ms, node key]` stored with the version `get` last
        returned for the map (None when unknown)."""
        return self._cursors.get((map_id, self._served.get(map_id, -1)))

    async def get(self, map_id: str, deadline: Optional[float] = None, tolerate_stale: bool = False) -> Tuple[Any, int]:
        """(graph, version) of the map's current version; ChartFallback otherwise. `tolerate_stale`: an older cached version is
        served however long the newer one has been known (the caller says so in its answer; docs/keepout_zones.md). `deadline` is an
        absolute event-loop time (the request's budget end): no wait goes past it, and a timeout
        caused by it is reported `budget_exhausted`, not the stage's own reason."""
        loop = asyncio.get_running_loop()
        stage_end = loop.time() + self._timeout
        if deadline is not None and deadline - loop.time() < MIN_REMAINING_S:
            raise ChartFallback("budget_exhausted")

        def left() -> float:
            end = stage_end if deadline is None else min(stage_end, deadline)
            return max(0.01, end - loop.time())

        def reason(stage: str) -> str:
            return "budget_exhausted" if deadline is not None and deadline <= stage_end else stage

        try:
            store = await asyncio.wait_for(self._io_pool.submit(self._open_store), left())
            meta = await self._meta(store, map_id, left())
        except ChartFallback:
            raise
        except asyncio.TimeoutError as err:
            raise ChartFallback(reason("store_timeout")) from err
        except Exception as err:  # pylint: disable=broad-except
            _log.warning("scene graph meta of map %s failed: %s", map_id, err)
            raise ChartFallback("store_error") from err
        version = meta.get("version")
        if version is None:
            raise ChartFallback("no_graph")
        key = (map_id, int(version))
        self._note_meta(map_id, key[1], self._token(meta))
        self._cursors[key] = meta.get("cursor")
        if len(self._cursors) > 8 * self._max:
            for old in [k for k in self._cursors if k not in self._graphs and k != key]:
                del self._cursors[old]
        self._drop_unavailable()
        if key in self._graphs:
            self._graphs.move_to_end(key)
            self._served[map_id] = key[1]
            return self._graphs[key], key[1]
        if key not in self._loading and self._backed_off(map_id):
            # the last load of this map failed a moment ago: do not start another per request
            served = self._serve_older(map_id, key[1], tolerate_stale)
            if served is None:
                raise ChartFallback("load_error")
            return served
        fut = self._start_load(store, key)
        # A newer version than the cached one: refresh in the background (the load above is single
        # flight per key) and keep serving the newest older version meanwhile, for at most
        # `max_stale_s`; the caller also bounds its age by the ingest lag of THAT version's cursor
        # (`cursor(map)`). A big map takes seconds to load, the worker publishes every few.
        served = self._serve_older(map_id, key[1], tolerate_stale)
        if served is not None:
            return served
        try:
            graph = await asyncio.wait_for(asyncio.shield(fut), left())
        except asyncio.TimeoutError as err:
            # cold load (nothing older cached for this map): this request falls back
            raise ChartFallback(reason("load_timeout")) from err
        except Exception as err:  # pylint: disable=broad-except
            raise ChartFallback("load_error") from err
        self._served[map_id] = key[1]
        return graph, key[1]


# Maps explicitly switched off (`status.scene_graph_enabled` a JSON false): the same query as
# graph_builder.scene_guard.DISABLED_SQL (pinned equal by a test; the planner image does not
# import the graph-builder service).
DISABLED_MAPS_SQL = ("SELECT name FROM mapobjectv1 WHERE name = ANY(%s) "
                     "AND lifecycle <> 'DELETED' AND status->%s = 'false'::jsonb")
ENABLED_KEY = "scene_graph_enabled"


async def disabled_maps(database: Any, map_ids: Sequence[str]) -> set:
    """The `map_ids` whose `status.scene_graph_enabled` is false. RAISES when the registry cannot
    be asked (the caller decides; the pre-warm then loads every map)."""
    names = sorted({str(m) for m in map_ids if m})
    if not names:
        return set()
    async with database.connection() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute(DISABLED_MAPS_SQL, (names, ENABLED_KEY))
            return {str(r[0]) for r in await cursor.fetchall()}


def version_lag_s(graph_db: Any, map_id: str, cursor: Any,
                  now_ms: Optional[float] = None) -> Optional[float]:
    """How stale the loaded graph version is, in seconds: the age of the oldest costmap stored in
    the topomap after the version's ingest cursor (`store.meta(map)["cursor"]`, the worker's
    `(costmap_stored_ms, node key)`), 0.0 when nothing is waiting. None = unknown (no cursor, no
    such query, database error): the caller must not reject on it. One indexed AQL query with
    limit 1; blocking (run it in a thread). `costmap_stored_ms` is the database clock: the
    planner and ArangoDB are expected to share a host or an NTP-synced clock."""
    try:
        ms, key = int(cursor[0]), str(cursor[1])
    except (TypeError, ValueError, IndexError, KeyError):
        return None
    try:
        rows = graph_db.costmap_nodes_since(map_id, ms, key, 1)
    except Exception as err:  # pylint: disable=broad-except
        _log.warning("scene graph lag of map %s unknown: %s", map_id, err)
        return None
    if not isinstance(rows, list):
        return None
    if not rows:
        return 0.0
    try:
        stored = float(rows[0]["costmap_stored_ms"])
    except (TypeError, ValueError, KeyError):
        return None
    now = time.time() * 1000.0 if now_ms is None else float(now_ms)
    return max(0.0, (now - stored) / 1000.0)


def blocked_free_los(graph: Any, overlay: Tuple[list, list], keepout_keys: Any = None) -> Callable[[Pt, Pt], bool]:
    """Line-of-sight test for `simplify` using only the public API: the straight segment is sampled
    every quarter lattice step, every sample's lattice cell must be navigable (its centre is the
    centre of a free cell: `snap` distance <= half a step; memoised per cell), a step that changes
    both cell indices (a corner crossing) needs both orthogonal neighbours free too (a diagonal
    squeeze between two blocked cells is not a passage), and the segment must clear the transient
    blocked overlay. `keepout_keys` (a set of `scene_graph.api.cell_keys` ints, the keep-out zones' cells): a cell in it is not
    free."""
    from packages.utils import blocked_geometry as bg
    h = float(graph.params.lattice)
    discs, segments = overlay
    cells: Dict[Tuple[int, int], bool] = {}
    if keepout_keys:
        from packages.scene_graph.api import CELL_OFF, CELL_W
    else:
        keepout_keys = None

    cell_free = getattr(graph, "free_cell", None)        # the lazy graph answers from its owner tile, no snap

    def free(i: int, j: int) -> bool:
        v = cells.get((i, j))
        if v is None:
            if keepout_keys is not None and (j + CELL_OFF) * CELL_W + (i + CELL_OFF) in keepout_keys:
                v = False
            elif cell_free is not None:
                v = bool(cell_free(i, j))
            else:
                s = graph.snap((i + 0.5) * h, (j + 0.5) * h, 0.5 * h)
                v = s.key is not None and s.distance <= 0.5 * h
            cells[(i, j)] = v
        return v

    def visible(a: Pt, b: Pt) -> bool:
        for (cx, cy, r) in discs:
            if bg.point_segment_distance((cx, cy), a, b) <= r:
                return False
        for (p, q, hw) in segments:
            if bg.segment_segment_distance(a, b, p, q) <= hw:
                return False
        n = max(1, int(math.ceil(math.dist(a, b) / (0.25 * h))))
        pi = pj = None
        for k in range(n + 1):
            t = k / n
            x, y = a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1])
            i, j = int(math.floor(x / h)), int(math.floor(y / h))
            if not free(i, j):
                return False
            if pi is not None and i != pi and j != pj and not (free(pi, j) and free(i, pj)):
                return False
            pi, pj = i, j
        return True

    return visible


def turns_in_place(points: Sequence[Pt], threshold: float = TURN_IN_PLACE_RAD) -> bool:
    """Whether the waypoint polyline changes heading by more than `threshold` at any waypoint (the
    robot may rotate on the spot there; the rotation, not the straight run, then sets the width
    needed). The start heading is not known to the planner and is not considered."""
    bearings = [math.atan2(q[1] - p[1], q[0] - p[0]) for p, q in zip(points[:-1], points[1:])
                if p != q]
    for b0, b1 in zip(bearings[:-1], bearings[1:]):
        d = abs(math.atan2(math.sin(b1 - b0), math.cos(b1 - b0)))
        if d > threshold:
            return True
    return False


def required_width(robot_width_m: float, robot_length_m: Optional[float], turns: bool,
                   margin_m: float = 0.0) -> float:
    """The passage width a robot needs. Aligned with the passage the lateral extent is its width;
    where it must turn in place the footprint sweeps its circumscribed circle, diameter
    hypot(length, width) (a square footprint: 2 r' sqrt(2) for r' the half side; the length is the
    width while the factsheet has none). `margin_m` covers the lattice error of Route.min_width
    (+-lattice/2 = 0.05 m)."""
    w = float(robot_width_m)
    if turns:
        length = float(robot_length_m) if robot_length_m is not None \
            and math.isfinite(robot_length_m) and robot_length_m > 0.0 else w
        w = math.hypot(length, w)
    return w + max(0.0, float(margin_m))


def inscribed_radius(graph: Any, robot_width_m: Optional[float], robot_length_m: Optional[float]) -> Optional[float]:
    """The robot's inscribed radius r_in: half the smaller side of the factsheet footprint (the width alone while the length is
    unknown), else the map's r (`robot_half_side`); None when neither is a usable number."""
    if robot_width_m is not None and math.isfinite(robot_width_m) and robot_width_m > 0.0:
        side = float(robot_width_m)
        if robot_length_m is not None and math.isfinite(robot_length_m) and robot_length_m > 0.0:
            side = min(side, float(robot_length_m))
        return side / 2.0
    r = getattr(getattr(graph, "params", None), "robot_half_side", None)
    return float(r) if r is not None and 0.0 < float(r) < math.inf else None


def _exp_safety(graph: Any, robot_width_m: Optional[float], robot_length_m: Optional[float], kappa: float) -> Any:
    """The exponential tier (inflation_profile_design.md) from the SCENE_GRAPH_INFLATION_* settings, or None when it is not
    configured or invalid (a warning for an invalid one: the caller then prices with the hinge)."""
    from packages import config
    from packages.scene_graph.api import Safety
    from packages.services.mission_planner.lazy_plan import _CLR_CAP_M
    reach, k = config.SCENE_GRAPH_INFLATION_RADIUS_M, config.SCENE_GRAPH_INFLATION_SCALING
    if reach is None or k is None:
        return None
    origin = config.SCENE_GRAPH_GLOBAL_INSCRIBED_M
    if origin is None:
        origin = inscribed_radius(graph, robot_width_m, robot_length_m)
    # the kappa of the exponential tier is 0.5 unless the operator set SCENE_GRAPH_SAFETY_KAPPA
    kap = float(kappa) if config.SCENE_GRAPH_SAFETY_KAPPA_EXPLICIT else 0.5
    try:
        if origin is None or not reach <= _CLR_CAP_M:
            raise ValueError("origin %r unknown or reach %r above the clearance cap %r" % (origin, reach, _CLR_CAP_M))
        return Safety(margin=float(reach), rate=kap, k=float(k), origin=float(origin))
    except ValueError as err:
        _log.warning("inflation profile invalid (%s); pricing with the hinge", err)
        return None


def _number(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def turn_lambda(factsheet: Any, enabled: bool, operator: Optional[float]) -> Tuple[float, str, str]:
    """(lambda m per rad, source, reason) of the turn cost of a robot's plans (docs/scene_graph/close_evidence_and_turn_cost_design.md
    B.6); pure: the caller passes SCENE_GRAPH_TURN_COST_ENABLED (`enabled`) and SCENE_GRAPH_TURN_LAMBDA_M_PER_RAD (`operator`).
    Off (0, "off", "disabled") unless `enabled`. Otherwise speed_max / angular_speed_max of the factsheet (`RobotTypeIdentifierV1`;
    -1 until the robot sent them) when both are finite and > 0, clamped to [0, Safety.MAX_TURN]: source "factsheet"; else the fallback
    `operator` when set (finite, >= 0), capped at config.TURN_LAMBDA_FALLBACK_MAX_M_PER_RAD (0.7): source "config"; else 0,
    source "off". `reason` is empty for the factsheet and names the missing field(s) otherwise. No speed is assumed."""
    from packages.scene_graph.api import Safety
    if not enabled:
        return 0.0, "off", "disabled"
    got = {}
    for name in ("speed_max", "angular_speed_max"):
        v = _number(getattr(factsheet, name, None))
        got[name] = v if v is not None and math.isfinite(v) and v > 0.0 else None
    if None not in got.values():
        return min(max(got["speed_max"] / got["angular_speed_max"], 0.0), Safety.MAX_TURN), "factsheet", ""
    reason = ", ".join(n for n, v in got.items() if v is None) + " missing"
    op = operator
    if op is not None and math.isfinite(op) and op >= 0.0:
        return min(float(op), config.TURN_LAMBDA_FALLBACK_MAX_M_PER_RAD), "config", reason
    if op is not None:
        reason += "; SCENE_GRAPH_TURN_LAMBDA_M_PER_RAD=%r ignored" % (op,)
    return 0.0, "off", reason


def turn_record(factsheet: Any, enabled: bool, operator: Optional[float]) -> Dict[str, Any]:
    """The `turn` entry of the pricing record: {lambda_m_per_rad, source, speed_max, angular_speed_max, reason} (`turn_lambda`; the
    two fields as read from the factsheet, None when absent)."""
    lam, source, reason = turn_lambda(factsheet, enabled, operator)
    return {"lambda_m_per_rad": lam, "source": source, "speed_max": _number(getattr(factsheet, "speed_max", None)),
            "angular_speed_max": _number(getattr(factsheet, "angular_speed_max", None)), "reason": reason}


# the turn entry of a plan whose caller gave no robot (tools, tests): no turn cost
TURN_NOT_GIVEN = {"lambda_m_per_rad": 0.0, "source": "off", "speed_max": None, "angular_speed_max": None, "reason": "no robot"}


def safety_for(graph: Any, robot_width_m: Optional[float], robot_length_m: Optional[float],
               kappa: float, turn: float = 0.0) -> Any:
    """The `Safety` of a plan: the clearance pricing (`_pricing_for`) with the turn cost `turn` (m per rad, `turn_lambda`) on
    top. Pricing off and `turn` > 0: Safety(margin = the clearance cap, rate 0, turn), the length plus the turn cost (the margin
    is not read at rate 0). None for a graph without `deficit_cell` (a graph that cannot take a Safety), or no price and no turn."""
    if not callable(getattr(graph, "deficit_cell", None)):
        return None
    priced = _pricing_for(graph, robot_width_m, robot_length_m, kappa)
    from packages.scene_graph.api import Safety
    from packages.services.mission_planner.lazy_plan import _CLR_CAP_M
    if turn > 0.0:
        priced = replace(priced, turn=float(turn)) if priced is not None else Safety(margin=_CLR_CAP_M, rate=0.0, turn=float(turn))
    return priced


def _pricing_for(graph: Any, robot_width_m: Optional[float], robot_length_m: Optional[float],
                 kappa: float) -> Any:
    """The `Safety` (clearance pricing, docs/scene_graph/route_safety.md section 1) of a plan, or None for the
    legacy shortest route: kappa <= 0, or a graph without `deficit_cell`. Two tiers. Exponential
    (docs/scene_graph/inflation_profile_design.md, experimental): when SCENE_GRAPH_INFLATION_RADIUS_M and
    SCENE_GRAPH_INFLATION_SCALING are set and valid (`_exp_safety`). Otherwise the hinge: None also for a margin that does not
    exceed the map's inscribed radius r by more than the lattice clearance quantisation h / sqrt(2) (S12: the price would be
    noise, and the rate kappa / (margin - r) unbounded), or above the clearance cap of the lazy reader
    (`lazy_plan._CLR_CAP_M`, 1 m: pricing is off beyond it). margin = r_c, the circumscribed radius hypot(length, width) / 2
    of the planning robot (the length is the width while unknown), r * sqrt(2) while the width is unknown;
    rate = kappa / (margin - r)."""
    if not kappa > 0.0 or not callable(getattr(graph, "deficit_cell", None)):
        return None
    exp = _exp_safety(graph, robot_width_m, robot_length_m, kappa)
    if exp is not None:
        return exp
    r = getattr(getattr(graph, "params", None), "robot_half_side", None)
    if r is None or not (0.0 < float(r) < math.inf):
        return None
    r = float(r)
    if robot_width_m is not None and math.isfinite(robot_width_m) and robot_width_m > 0.0:
        length = float(robot_length_m) if robot_length_m is not None \
            and math.isfinite(robot_length_m) and robot_length_m > 0.0 else float(robot_width_m)
        r_c = math.hypot(length, float(robot_width_m)) / 2.0
    else:
        r_c = r * math.sqrt(2.0)
    margin = r_c
    h = float(getattr(getattr(graph, "params", None), "lattice", 0.1))
    from packages.services.mission_planner.lazy_plan import _CLR_CAP_M
    if not margin - r > h / math.sqrt(2.0) or margin > _CLR_CAP_M:
        return None
    from packages.scene_graph.api import Safety
    return Safety(margin=margin, rate=float(kappa) / (margin - r))


PRICE_SAMPLE_M = 0.025          # spacing of the samples that measure the priced cost of a straight segment


def region_arg(graph: Any, region: Any) -> Optional[tuple]:
    """`graph.region_arg(region)` (rho per chart, scale, rho_min) when the graph can price a region snapshot and this one prices it, else None."""
    fn = getattr(graph, "region_arg", None)
    return fn(region) if callable(fn) and region is not None else None


def _sampled(factor: Callable[[float, float], float]) -> Callable[[Pt, Pt], float]:
    """The cost of a straight piece (a, b): its length times the mean of `factor` over samples every `PRICE_SAMPLE_M` (at the
    middle of equal sub-pieces)."""
    def cost(a: Pt, b: Pt) -> float:
        length = math.dist(a, b)
        if length == 0.0:
            return 0.0
        n = max(1, int(math.ceil(length / PRICE_SAMPLE_M - 1e-9)))
        acc = 0.0
        for k in range(n):
            t = (k + 0.5) / n
            acc += factor(a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1]))
        return length * acc / n

    return cost


def chord_cost(graph: Any, safety: Any, region: Any = None) -> Callable[[Pt, Pt], float]:
    """The priced cost of a straight piece (a, b): its length times the mean of 1 + rate * deficit + rho over samples every
    `PRICE_SAMPLE_M` (at the middle of equal sub-pieces), the deficit read from the sample's lattice cell (a non-navigable cell
    counts as clearance 0) and rho (excess time per metre x speed_max, `region`: a `RegionPricing`) from the chart that owns it
    (0 where none does); the plain length when no clearance price and no region term is active (`safety` None or rate 0)."""
    reg = region_arg(graph, region)
    if (safety is None or float(safety.rate) <= 0.0) and reg is None:
        return math.dist
    h = float(graph.params.lattice)
    priced = safety is not None and float(safety.rate) > 0.0
    mu = float(safety.rate) if priced else 0.0
    price_cell = getattr(graph, "price_cell", None)
    memo: Dict[Tuple[int, int], float] = {}

    def factor(x: float, y: float) -> float:
        c = (int(math.floor(x / h)), int(math.floor(y / h)))
        f = memo.get(c)
        if f is None:
            f = 1.0
            if priced:
                if callable(price_cell):
                    d = price_cell(c[0], c[1], safety)
                else:                                    # a graph that only has the hinge's deficit_cell
                    d = graph.deficit_cell(c[0], c[1], float(safety.margin))
                # a non-navigable cell counts as clearance 0
                f = 1.0 + mu * (float(safety.price(0.0)) if d is None else d)
            if reg is not None:
                j = graph.chart_index_cell(c[0], c[1])
                if j >= 0:
                    f += float(reg[0][j]) / reg[1]       # the lattice edge's (rho_a + rho_b) / (2 scale), per sample
            memo[c] = f
        return f

    return _sampled(factor)


def chord_excess(graph: Any, region: Any) -> Callable[[Pt, Pt], float]:
    """The metres-at-speed_max of excess time of a straight piece (a, b): its length times the mean of rho / scale (`chord_cost`'s
    sampling), 0 when `region` prices nothing on this graph."""
    reg = region_arg(graph, region)
    if reg is None:
        return lambda a, b: 0.0
    h = float(graph.params.lattice)

    def factor(x: float, y: float) -> float:
        j = graph.chart_index_cell(int(math.floor(x / h)), int(math.floor(y / h)))
        return float(reg[0][j]) / reg[1] if j >= 0 else 0.0

    return _sampled(factor)


def priced_visible(visible: Callable[[Pt, Pt], bool], graph: Any, lattice_pts: Sequence[Pt],
                   safety: Any, region: Any = None) -> Callable[[Pt, Pt], bool]:
    """Cost-aware line of sight (S11): `visible(a, b)` AND the priced cost of the segment (a, b) is at most that of the
    lattice sub-path between the two points (both found in `lattice_pts`, the route's points). The cost of a straight
    piece is its length times the mean of 1 + rate * deficit over samples every `PRICE_SAMPLE_M` (at the middle of equal
    sub-pieces), the deficit read from the sample's lattice cell (a non-navigable cell counts as clearance 0); the
    sub-path is measured the same way between its consecutive points. Then the final polyline costs no more than the
    lattice path. A point that is not on the route is judged by `visible` alone. With `region` the cost also holds the chart's
    rho (`chord_cost`), so a shortcut never cuts across a slow chart that the lattice path avoided."""
    cost = chord_cost(graph, safety, region)
    pts: List[Pt] = []
    for p in lattice_pts:
        if not pts or p != pts[-1]:
            pts.append(p)
    prefix = [0.0]
    for p, q in zip(pts[:-1], pts[1:]):
        prefix.append(prefix[-1] + cost(p, q))
    where: Dict[Pt, int] = {}
    for i, p in enumerate(pts):
        where.setdefault(p, i)

    def priced(a: Pt, b: Pt) -> bool:
        if not visible(a, b):
            return False
        ia, ib = where.get(a), where.get(b)
        if ia is None or ib is None:
            return True
        lo, hi = (ia, ib) if ia <= ib else (ib, ia)
        sub = prefix[hi] - prefix[lo]
        return cost(a, b) <= sub * (1.0 + 1e-9) + 1e-9

    return priced


def budget_pull(graph: Any, safety: Any, budget_m: float, region: Any = None) -> Callable[..., List[Pt]]:
    """A `simplify` replacement for the gap fill (`node_policy_plan.insert_planned`): `fewest_waypoints` with this budget.
    It takes the plain line of sight (`unpriced_visible`): the price is in the budget."""
    cost = chord_cost(graph, safety, region)

    def pull(points: Sequence[Pt], visible: Callable[[Pt, Pt], bool], max_gap: float) -> List[Pt]:
        return fewest_waypoints(points, visible, max_gap, cost, budget_m, thin_eps=thin_eps(graph))
    pull.unpriced_visible = True  # type: ignore[attr-defined]
    return pull


def region_times(graph: Any, region: Any, turn: Optional[Dict[str, Any]]) -> Optional[Callable[[Sequence[Pt]], Tuple[float, float]]]:
    """`times(points) -> (predicted_s, predicted_free_s)` of a polyline for the planning robot, or None when there is nothing to predict
    with: no snapshot, one that is not `active`, of another version / charts than the graph's, or a robot without a real speed_max.
    predicted_free_s = length / speed_max; predicted_s adds the excess time of the charts the polyline runs through (rho x length /
    speed_max, `chord_excess`). Length-based like the replay that calibrated the model (scripts/analysis/region_cost_replay.py): no
    turn time (the moving figures already hold the turning slowness; counting the heading changes of the dense lattice path would
    add about 0.8 rad per step of a staircase)."""
    why = getattr(graph, "region_why", None)
    if region is None or not callable(why) or not region.active or why(region) not in ("priced", "neutral"):
        return None
    speed = _number((turn or {}).get("speed_max"))
    if speed is None or not (math.isfinite(speed) and speed > 0.0):
        return None
    excess = chord_excess(graph, region)

    def times(pts: Sequence[Pt]) -> Tuple[float, float]:
        length = sum(math.dist(p, q) for p, q in zip(pts[:-1], pts[1:]))
        return (length + sum(excess(p, q) for p, q in zip(pts[:-1], pts[1:]))) / speed, length / speed

    return times


def annotate_predicted(legs: Optional[List[Dict[str, Any]]], times: Optional[Callable[[Sequence[Pt]], Tuple[float, float]]]) -> None:
    """Put `predicted_s` (seconds, mm) on every trajectory leg, from the leg's own polyline (`region_times`); in place."""
    if times is None or not legs:
        return
    for item in legs:
        item["predicted_s"] = round(times([tuple(p) for p in item["points"]])[0], 3)


def cost_model_of(region: Any) -> Optional[str]:
    """`model_hash:stamp` of a snapshot (what the route node records), None without one."""
    return None if region is None or region.stamp is None else "%s:%s" % (region.model_hash, region.stamp)


def waypoints_below_floor(graph: Any, poses: Sequence[Any], safety: Any, floor: Optional[float]) -> int:
    """Number of interior planner-placed waypoints (in `_plan_on_graph` every interior waypoint is one) whose lattice clearance is below `floor` (a guard that only counts: no
    waypoint is moved). The start, the goal and operator waypoints are not counted; a waypoint on a non-navigable cell counts
    (clearance 0). The clearance is read through `deficit_cell` at margin `_CLR_CAP_M` (g = cap - deficit, exact below the cap)."""
    if floor is None or not callable(getattr(graph, "deficit_cell", None)):
        return 0
    from packages.services.mission_planner.lazy_plan import _CLR_CAP_M
    h = float(graph.params.lattice)
    n = 0
    for w in poses[1:-1]:
        if getattr(w, "origin", None) == common.WAYPOINT_ORIGIN_USER:
            continue
        d = graph.deficit_cell(int(math.floor(w.x / h)), int(math.floor(w.y / h)), _CLR_CAP_M)
        if d is None or _CLR_CAP_M - d < floor - 1e-9:
            n += 1
    return n


def _region_record(graph: Any, region: Any, mode: str, turn: Dict[str, Any], route: Any, lattice: Sequence[Pt],
                   legs: Optional[List[Dict[str, Any]]], priced: bool, route_with: Callable[..., Any]) -> Dict[str, Any]:
    """`pricing["region"]` of a plan (docs/planner_region_stats_2026-10-27.md 7): the snapshot's identity and status (`status` is the
    graph's verdict when the snapshot is not of this version / these charts), `mode`, `served` ("priced" | "unpriced"), the fallback
    counts per level, and with a usable snapshot and a factsheet speed_max `predicted_s` / `predicted_free_s` of the served lattice
    path (`region_times`), `cost_model`, and `predicted_s` on the trajectory `legs`. Shadow adds what the priced route would have been
    (`route_with(region=...)`: best effort): `alt_route_changed`, `alt_extra_m`, `alt_predicted_s`, `alt_cost`."""
    why = graph.region_why(region) if callable(getattr(graph, "region_why", None)) else "unsupported_graph"
    rec: Dict[str, Any] = {**region.record(), "mode": mode, "served": "priced" if priced else "unpriced"}
    if why in ("version_mismatch", "keys_mismatch", "unsupported_graph"):
        rec["status"] = why
    times = region_times(graph, region, turn)
    if times is None:
        return rec
    rec["predicted_s"], rec["predicted_free_s"] = (round(v, 3) for v in times(lattice))
    rec["cost_model"] = cost_model_of(region)
    annotate_predicted(legs, times)
    if mode == "shadow" and why == "priced":
        saved = getattr(graph, "last", None)
        try:
            alt = route_with(region=region)
            if alt is not None and alt.nodes:
                alt_pts = [lattice[0]] + [tuple(p) for p in alt.nodes] + [lattice[-1]]
                rec.update(alt_route_changed=[tuple(p) for p in alt.nodes] != [tuple(p) for p in route.nodes],
                           alt_extra_m=round(float(alt.length) - float(route.length), 3),
                           alt_predicted_s=round(times(alt_pts)[0], 3), alt_cost=int(alt.cost))
        except Exception as err:  # pylint: disable=broad-except
            rec["alt_error"] = type(err).__name__
        finally:
            if saved is not None:
                graph.last = saved                           # the served route's search stays what `last` says
    return rec


def _keepout_detour(graph: Any, keep: Any, transient: Any, snap_a: Any, snap_b: Any, route: Any,
                    report: Dict[str, Any]) -> None:
    """Fill `report` (detour, forced_by, extra_length_m) when the route found WITH the keep-out zones is not the one WITHOUT them:
    the zones are only asked about when one can lie on a route no longer than ours (a zone farther from the start plus the goal
    than that is not on any such route), so a plan far from every zone costs no second search. Best effort: a failed second
    search leaves the report as it is."""
    from packages.scene_graph.api import MissingTiles
    from packages.services.mission_planner.lazy_plan import CorridorTooLarge
    a, b = (snap_a.x, snap_a.y), (snap_b.x, snap_b.y)
    limit = float(route.length) * 1.25 + 2.0

    def near(box: Any) -> bool:
        if box is None:
            return False
        dx = lambda p: max(box[0] - p[0], 0.0, p[0] - box[2])      # noqa: E731
        dy = lambda p: max(box[1] - p[1], 0.0, p[1] - box[3])      # noqa: E731
        return math.hypot(dx(a), dy(a)) + math.hypot(dx(b), dy(b)) <= limit
    if not any(near(z.box) for z in keep.applied):
        return
    try:
        free = graph.route(snap_a.key, snap_b.key, transient, start=a, goal=b)
    except (MissingTiles, CorridorTooLarge):
        return
    if free is None or not free.nodes:
        return
    from packages.services.mission_planner import keepout_overlay as kov
    hit = keep.zones_hit(kov.cells_of_points([tuple(p) for p in free.nodes], float(graph.params.lattice)))
    if hit:
        report["detour"] = True
        report["forced_by"] = [z.id for z in hit]
        report["extra_length_m"] = round(max(0.0, float(route.length) - float(free.length)), 3)


def plan_on_graph(graph: Any, map_id: str, start_xy: Pt, goal_xy: Pt,
                  overlay: Tuple[list, list], node_of: Callable[[float, float], Optional[str]],
                  start_snap_m: float, goal_snap_m: float, max_gap_m: float,
                  robot_width_m: Optional[float] = None, width_margin_m: float = 0.0,
                  robot_length_m: Optional[float] = None,
                  node_policy_cfg: Optional[Dict[str, Any]] = None, safety_kappa: float = 0.0,
                  width_check: bool = True, trajectory_max_points: int = 0,
                  turn: Optional[Dict[str, Any]] = None, waypoint_budget_m: float = 0.0,
                  keepout_for: Optional[Callable[[Any], Any]] = None, region: Any = None,
                  region_mode: str = "off") -> ChartPlan:
    """`_plan_on_graph`; a tile of a lazy graph that cannot be read (a garbage-collected version, a backend error)
    is ChartFallback("tile_unavailable"), and the graph is flagged so `GraphCache` drops it."""
    from packages.services.mission_planner.lazy_plan import TileUnavailable
    try:
        return _plan_on_graph(graph, map_id, start_xy, goal_xy, overlay, node_of, start_snap_m, goal_snap_m,
                              max_gap_m, robot_width_m, width_margin_m, robot_length_m,
                              node_policy_cfg, safety_kappa, width_check, trajectory_max_points, turn,
                              waypoint_budget_m, keepout_for, region, region_mode)
    except TileUnavailable as err:
        _log.warning("scene graph tile unavailable (%s); the graph is dropped", err)
        raise ChartFallback("tile_unavailable") from err


def _plan_on_graph(graph: Any, map_id: str, start_xy: Pt, goal_xy: Pt,
                   overlay: Tuple[list, list], node_of: Callable[[float, float], Optional[str]],
                   start_snap_m: float, goal_snap_m: float, max_gap_m: float,
                   robot_width_m: Optional[float] = None, width_margin_m: float = 0.0,
                   robot_length_m: Optional[float] = None,
                   node_policy_cfg: Optional[Dict[str, Any]] = None, safety_kappa: float = 0.0,
                   width_check: bool = True, trajectory_max_points: int = 0,
                   turn: Optional[Dict[str, Any]] = None, waypoint_budget_m: float = 0.0,
                  keepout_for: Optional[Callable[[Any], Any]] = None, region: Any = None,
                   region_mode: str = "off") -> ChartPlan:
    """Snap, route around the overlay, simplify, convert to poses. Blocking (run it in a thread).
    Raises ChartFallback for every case the legacy planner must take over. `robot_width_m` is the
    planning robot's footprint width (None / <= 0: unknown, never rejects; `robot_length_m` likewise
    optional): the route is `too_narrow` when its narrowest passage is under `required_width` (the
    width, or the footprint diagonal when the simplified route turns in place, plus
    `width_margin_m`); the caller gates it by SCENE_GRAPH_WIDTH_CHECK_ENABLED. `node_of` may raise:
    that is `leaf_lookup_error`, a point with no leaf near it returns None. `node_policy_cfg`
    ({robot_width, margin, snap_m, budget_s}): every waypoint but the first and the last is made
    skippable where the graph gives the room around it (go-here, product rule 1); the outcome is
    `ChartPlan.node_policy`.
    `safety_kappa` > 0 prices the route's clearance (`safety_for`: it needs the graph's `deficit_cell` and a margin
    above the map's inscribed radius; otherwise the route is the shortest, as with 0) and makes the line of sight
    cost-aware (`priced_visible`); the footprint (`robot_width_m`, `robot_length_m`) then gives the margin even with
    `width_check` False, which only gates the `too_narrow` comparison. `trajectory_max_points` > 0 adds
    `ChartPlan.trajectory`, the lattice path cut at the output waypoints (omitted when it has more points). `turn` is the robot's
    `turn_record` (None: no turn cost): its lambda goes into the route's `Safety` and the record into `ChartPlan.pricing`; the
    string pull has no turn term (a shortcut is judged by visibility and the priced cost only). `waypoint_budget_m` > 0 replaces
    the string pull by `fewest_waypoints` with that slack budget (the line of sight is then the plain one, the price is in
    the budget); 0 = the string pull, unchanged. `keepout_for(graph)` (blocking; None: the map has none) is the operator's
    keep-out zones resolved on this graph (keepout_overlay.Resolution): their cells are removed from the route search and the line
    of sight like the overlay, `ChartPlan.keepout` says what they did, and a goal only they cut off is
    ChartFallback(BLOCKED_BY_KEEPOUT, info) which the caller must report, not hand to the legacy planner. `region` is the planning robot's
    `RegionPricing` and `region_mode` "off" | "shadow" | "on" (`region_cost.mode_for`; docs/planner_region_stats_2026-10-27.md 5, 7):
    "on" prices the route (search, string pull, corridor legs) with the region term; "shadow" serves today's route and records, in
    `pricing["region"]`, the predicted time of it and what the priced route would have been; "off" (or no snapshot) does nothing."""
    from packages.scene_graph.api import Blocked, MissingTiles
    from packages.services.mission_planner.lazy_plan import CorridorTooLarge
    snap_a = graph.snap(start_xy[0], start_xy[1], start_snap_m)
    problem = snap_problem(snap_a, start_snap_m)
    if problem:
        raise ChartFallback("start_" + problem)
    # A goal is not refused for its distance from free space: beyond goal_snap_m (the map's close-evidence range) it is
    # planned with a `goal_far` notice. Refused only when the engine finds no free cell in reach, or the nearest one is
    # cut off by a known blocked cell.
    snap_b = graph.snap(goal_xy[0], goal_xy[1])
    problem = snap_problem(snap_b, math.inf)
    if problem:
        raise ChartFallback("goal_" + problem)
    goal_far = None
    if snap_b.distance > goal_snap_m:
        goal_far = {"goal_xy": (float(goal_xy[0]), float(goal_xy[1])), "free_xy": (snap_b.x, snap_b.y),
                    "distance_from_free_m": float(snap_b.distance), "unverified_length_m": None,
                    "limit_m": float(goal_snap_m)}
    discs, segments = overlay
    keep = keepout_for(graph) if keepout_for is not None else None
    kcells = keep.cells if keep is not None and len(keep.cells) else None
    transient = Blocked(discs=list(discs), segments=list(segments)) if (discs or segments) else None
    blocked = Blocked(discs=list(discs), segments=list(segments), cells=kcells) if (discs or segments or kcells is not None) else None
    turn = dict(TURN_NOT_GIVEN) if turn is None else dict(turn)
    safety = safety_for(graph, robot_width_m, robot_length_m, safety_kappa, turn["lambda_m_per_rad"])
    # the region term prices the served route only in mode "on" (and only when it prices something on this graph); "shadow" and "off" serve today's
    region = region if region_mode in ("shadow", "on") else None
    serve = region if region_mode == "on" and region_arg(graph, region) is not None else None
    extra = {} if safety is None else {"safety": safety}
    if serve is not None:
        extra["region"] = serve
    try:
        route = graph.route(snap_a.key, snap_b.key, blocked, start=(snap_a.x, snap_a.y),
                            goal=(snap_b.x, snap_b.y), **extra)
    except MissingTiles as err:
        raise ChartFallback("missing_tiles") from err
    except CorridorTooLarge as err:
        raise ChartFallback("corridor_too_large") from err
    if route is None or not route.nodes:
        if kcells is not None:
            # cut off by the keep-out zones alone? (the transient overlay and the zones are told apart by routing without the zones)
            try:
                without = graph.route(snap_a.key, snap_b.key, transient, start=(snap_a.x, snap_a.y),
                                      goal=(snap_b.x, snap_b.y))
            except (MissingTiles, CorridorTooLarge):
                without = None
            if without is not None and without.nodes:
                h = float(graph.params.lattice)
                from packages.services.mission_planner import keepout_overlay as kov
                hit = keep.zones_hit(kov.cells_of_points([(snap_a.x, snap_a.y), (snap_b.x, snap_b.y)] + [tuple(p) for p in without.nodes], h))
                raise ChartFallback(BLOCKED_BY_KEEPOUT, {
                    **keep.summary(), "forced_by": [z.id for z in hit],
                    "start_in_zone": bool(keep.zones_hit(kov.cells_of_points([(snap_a.x, snap_a.y)], h))),
                    "goal_in_zone": bool(keep.zones_hit(kov.cells_of_points([(snap_b.x, snap_b.y)], h)))})
        if blocked is not None:
            # unreachable only because of the overlay? say so (the legacy planner then reports the
            # blocking rows as failed_at "blocked_nodes").
            try:
                free = graph.route(snap_a.key, snap_b.key, None, start=(snap_a.x, snap_a.y),
                                   goal=(snap_b.x, snap_b.y))
            except (MissingTiles, CorridorTooLarge):
                free = None
            if free is not None and free.nodes:
                raise ChartFallback("blocked_by_overlay")
        raise ChartFallback("unreachable")
    check_width = width_check and robot_width_m is not None and math.isfinite(robot_width_m) and robot_width_m > 0.0
    min_width = float(route.min_width)
    if check_width and min_width < required_width(robot_width_m, robot_length_m, False,
                                                  width_margin_m):
        raise ChartFallback("too_narrow")       # not even aligned: no need to simplify
    # The lattice route runs from the snapped start cell to the snapped goal cell (point to point); the
    # prepend/append only matters when an end falls back to its chart anchor.
    pts = [(snap_a.x, snap_a.y)] + [tuple(p) for p in route.nodes] + [(snap_b.x, snap_b.y)]
    lattice = list(pts)                    # the dense path, before the waypoints are picked from it
    visible = blocked_free_los(graph, overlay, keep.keyset if kcells is not None else None)
    plain = visible
    if (safety is not None and safety.rate > 0.0) or serve is not None:
        visible = priced_visible(visible, graph, pts, safety, serve)
    budget_record = None
    if waypoint_budget_m > 0.0:
        pulled = fewest_waypoints(pts, plain, max_gap_m, chord_cost(graph, safety, serve), waypoint_budget_m, MIN_END_STUB_M,
                                  thin_eps(graph))
        budget_record = {"waypoint_budget_m": waypoint_budget_m,
                         "waypoints_removed": max(0, len(simplify(pts, visible, max_gap_m)) - len(pulled))}
        pts = pulled
    else:
        pts = simplify(pts, visible, max_gap_m)
    # The gap cap can leave a stub of a few lattice steps at the end (two waypoints on one leaf =
    # a zero-length leg): merge it into the previous leg when that stays in line of sight.
    # With the waypoint budget the merge is judged against the budget instead: visible, and the polyline stays within
    # J(lattice) + budget.
    if budget_record is not None:
        cost = chord_cost(graph, safety, serve)
        j_limit = sum(cost(p, q) for p, q in zip(lattice[:-1], lattice[1:])) + waypoint_budget_m

        def stub_ok(k: int) -> bool:
            rest = pts[:k] + pts[k + 1:]
            return plain(pts[k - 1], pts[k + 1]) and sum(cost(p, q) for p, q in zip(rest[:-1], rest[1:])) <= j_limit
    else:
        def stub_ok(k: int) -> bool:
            return visible(pts[k - 1], pts[k + 1])
    while len(pts) >= 3 and math.dist(pts[-2], pts[-1]) < MIN_END_STUB_M and stub_ok(len(pts) - 2):
        del pts[-2]
    if check_width and min_width < required_width(robot_width_m, robot_length_m,
                                                  turns_in_place(pts), width_margin_m):
        raise ChartFallback("too_narrow")
    try:
        ids = [node_of(x, y) for (x, y) in pts]
    except Exception as err:  # pylint: disable=broad-except
        _log.warning("scene graph leaf lookup failed: %s", err)
        raise ChartFallback("leaf_lookup_error") from err
    # A shared leaf (two waypoints in one leaf's reach) or a waypoint with no leaf near it is not
    # a reason to give the route up: drop such interior waypoints where the line of sight allows.
    pts, ids = repair_leaf_ids(pts, ids, visible, max_gap_m)
    # What is still repeated is merged where the line of sight allows; a route on one leaf becomes
    # [start, goal]; an unmergeable repeat is served as is (only a waypoint WITHOUT a leaf falls
    # back: planned_path needs one id per waypoint).
    pts, ids = collapse_repeated_leaf_ids(pts, ids, visible, max_gap_m)
    by_pt = dict(zip(pts, ids))
    poses, planned = build_waypoints(pts, map_id, lambda x, y: by_pt[(x, y)], allow_repeats=True,
                                     chart_of=lambda x, y: chart_key_at(graph, x, y))
    # An END without a leaf within reach is not a reason to give the plan up either (and lose its
    # corridors to the legacy planner): it is served with planned_path None, a route of legs known
    # by their VDA ids only (docs/node_policies_v2_plan.md, step 2.1).
    legs = None
    if trajectory_max_points > 0:
        try:
            legs = trajectory.capped(trajectory.split(lattice, [(p.x, p.y) for p in poses]),
                                     trajectory_max_points)
        except Exception as err:  # pylint: disable=broad-except
            _log.warning("trajectory not derived (%s)", err)
    report = None
    if node_policy_cfg is not None:
        report = node_policy_plan.annotate_corridors(
            graph, poses, range(0, len(poses) - 1), node_policy_cfg.get("robot_width"),
            node_policy_cfg.get("margin", 0.0), node_policy_cfg.get("snap_m", start_snap_m),
            node_policy_cfg.get("budget_s", 0.0), safety=safety,
            blocked=Blocked(cells=kcells) if kcells is not None else None, region=serve)
    pricing = None
    if safety is not None and safety.rate == 0.0:            # a turn cost alone: no clearance fields
        pricing = {"kappa": 0.0, "margin": None, "rate": 0.0, "margin_source": None, "cost": int(route.cost),
                   "exact": bool(getattr(route, "exact", True)), "profile": "none", "k": None, "origin": None, "reach": None,
                   "floor": None, "waypoints_below_floor": 0, "turn": turn}
    elif safety is not None:
        known = robot_width_m is not None and math.isfinite(robot_width_m) and robot_width_m > 0.0
        exp = safety.k is not None
        floor = safety.origin if exp else inscribed_radius(graph, robot_width_m, robot_length_m)
        pricing = {"kappa": float(safety.rate if exp else safety_kappa), "margin": safety.margin, "rate": safety.rate,
                   "margin_source": "factsheet" if known else "map_r", "cost": int(route.cost),
                   "exact": bool(getattr(route, "exact", True)),
                   "profile": "exp" if exp else "hinge", "k": safety.k, "origin": safety.origin, "reach": safety.margin,
                   "floor": floor,
                   "waypoints_below_floor": waypoints_below_floor(graph, poses, safety, floor), "turn": turn}
    rec = None
    if region is not None:
        rec = _region_record(graph, region, region_mode, turn, route, lattice, legs, serve is not None,
                             lambda **kw: graph.route(snap_a.key, snap_b.key, blocked, start=(snap_a.x, snap_a.y),
                                                      goal=(snap_b.x, snap_b.y), **{**extra, **kw}))
        if pricing is None:                                  # the region record alone: the clearance fields are 0 / None
            pricing = {"kappa": 0.0, "margin": None, "rate": 0.0, "margin_source": None, "cost": int(route.cost),
                       "exact": bool(getattr(route, "exact", True)), "profile": "none", "k": None, "origin": None, "reach": None,
                       "floor": None, "waypoints_below_floor": 0, "turn": turn}
        pricing["region"] = rec
    keepout_report = None
    if keep is not None and keep.zones:
        keepout_report = {**keep.summary(), "detour": False, "forced_by": [], "extra_length_m": None}
        if kcells is not None:
            _keepout_detour(graph, keep, transient, snap_a, snap_b, route, keepout_report)
    if goal_far is not None:
        goal_far["unverified_length_m"] = unverified_length_m(graph, route.nodes)
    return ChartPlan(waypoints=poses, planned_path=planned, charts=list(route.charts),
                     min_width=min_width, goal_xy=(snap_b.x, snap_b.y),
                     repeated_leaf_ids=planned is not None and len(set(planned)) != len(planned), node_policy=report, pricing=pricing,
                     trajectory=legs, waypoint_budget=budget_record, goal_far=goal_far, keepout=keepout_report,
                     cost_model=None if rec is None else rec.get("cost_model"))
