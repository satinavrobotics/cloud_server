"""Turn a scene-graph lattice route into the planner's plain waypoint poses.

Pure functions, no I/O and no engine import: the planner supplies the line-of-sight test and the
nearest-leaf lookup. The output contract is the one the dispatcher, leg tracker and edge statistics
already rely on (docs/scene_graph/integration.md, "Planner contract"):

* waypoints are `Pose2D` in the map frame, `theta` = bearing to the next waypoint, the last one
  repeats the previous bearing (the request carries no goal heading; the robot's final heading is
  the controller's business);
* `node_id` = the nearest leaf within a threshold, else None;
* `planned_path` is returned only when every waypoint has a leaf id, because the leg tracker pairs
  `waypoints[i]` with `planned_path[i]` and drops topomap ids on any length mismatch.
"""
import asyncio
import bisect
import logging
import math
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from cloud_common.objects import common
from packages.services.mission_planner import node_policy_plan, trajectory

Pt = Tuple[float, float]

PLANNER_SCENE_GRAPH = "scene_graph"
PLANNER_LEGACY = "legacy"


def simplify(points: Sequence[Pt], visible: Callable[[Pt, Pt], bool],
             max_gap: float) -> List[Pt]:
    """Greedy string pulling: from each kept point go to the farthest later point that is visible
    from it and at most `max_gap` away (straight line). The first and last points are always kept;
    with `max_gap` <= 0 no cap applies. Consecutive duplicates are dropped. The candidates of a
    step are the points up to the first one beyond `max_gap`; they are tried from the far end and
    the first visible one wins (the same point the ascending scan ends on, with far fewer
    `visible` calls on a free corridor). The window end starts at a bisect over the cumulative arc
    length (a chord is never longer than its arc, so every point within `max_gap` of arc is inside)
    and is extended by chord tests, which are cheap next to a line of sight."""
    pts: List[Pt] = []
    for p in points:
        if not pts or p != pts[-1]:
            pts.append(p)
    if len(pts) <= 2:
        return pts
    last = len(pts) - 1
    arc = [0.0]
    for k in range(1, len(pts)):
        arc.append(arc[-1] + math.dist(pts[k - 1], pts[k]))
    kept = [pts[0]]
    i = 0
    while i < last:
        if max_gap > 0:
            end = max(i + 1, min(last, bisect.bisect_right(arc, arc[i] + max_gap) - 1))
            while end < last and math.dist(pts[i], pts[end + 1]) <= max_gap:
                end += 1
        else:
            end = last
        best = i + 1                       # the next lattice point is always reachable
        for k in range(end, i + 1, -1):
            if visible(pts[i], pts[k]):
                best = k
                break
        kept.append(pts[best])
        i = best
    return kept


def build_waypoints(points: Sequence[Pt], map_id: str,
                    node_of: Callable[[float, float], Optional[str]],
                    allow_repeats: bool = False
                    ) -> Tuple[List[common.Pose2D], Optional[List[str]]]:
    """(waypoints, planned_path or None) for an already simplified point list.
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
        poses.append(common.Pose2D(x=x, y=y, theta=theta, map_id=map_id, node_id=node_of(x, y)))
    ids = [p.node_id for p in poses]
    # one id per waypoint, no repeats: two waypoints snapped to the same leaf would be a zero-length
    # leg for the leg tracker, so such a route carries no planned_path (its legs are VDA-id only).
    planned = ([str(n) for n in ids]
               if poses and all(n is not None for n in ids)
               and (allow_repeats or len(set(ids)) == len(ids)) else None)
    return poses, planned


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

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass
class ChartPlan:
    waypoints: List[common.Pose2D]
    planned_path: List[str]            # one distinct leaf id per waypoint
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
    # the apex candidates' record {corners (corners of the greedy pull), apexes_used (waypoints placed on an apex)}; None when off
    waypoint_apex: Optional[Dict[str, Any]] = None


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

    def _serve_older(self, map_id: str, version: int):
        """(graph, version) of the newest cached version below `version`, None when there is none;
        ChartFallback("stale_graph") when a newer one has been known for more than `max_stale_s`."""
        older = [k for k in self._graphs if k[0] == map_id and k[1] < version]
        if not older:
            return None
        prev = max(older, key=lambda k: k[1])
        if self._max_stale > 0:
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

    async def get(self, map_id: str, deadline: Optional[float] = None) -> Tuple[Any, int]:
        """(graph, version) of the map's current version; ChartFallback otherwise. `deadline` is an
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
            served = self._serve_older(map_id, key[1])
            if served is None:
                raise ChartFallback("load_error")
            return served
        fut = self._start_load(store, key)
        # A newer version than the cached one: refresh in the background (the load above is single
        # flight per key) and keep serving the newest older version meanwhile, for at most
        # `max_stale_s`; the caller also bounds its age by the ingest lag of THAT version's cursor
        # (`cursor(map)`). A big map takes seconds to load, the worker publishes every few.
        served = self._serve_older(map_id, key[1])
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


def blocked_free_los(graph: Any, overlay: Tuple[list, list]) -> Callable[[Pt, Pt], bool]:
    """Line-of-sight test for `simplify` using only the public API: the straight segment is sampled
    every quarter lattice step, every sample's lattice cell must be navigable (its centre is the
    centre of a free cell: `snap` distance <= half a step; memoised per cell), a step that changes
    both cell indices (a corner crossing) needs both orthogonal neighbours free too (a diagonal
    squeeze between two blocked cells is not a passage), and the segment must clear the transient
    blocked overlay."""
    from packages.utils import blocked_geometry as bg
    h = float(graph.params.lattice)
    discs, segments = overlay
    cells: Dict[Tuple[int, int], bool] = {}

    cell_free = getattr(graph, "free_cell", None)        # the lazy graph answers from its owner tile, no snap

    def free(i: int, j: int) -> bool:
        v = cells.get((i, j))
        if v is None:
            if cell_free is not None:
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
    -1 until the robot sent them) when both are finite and > 0, clamped to [0, Safety.MAX_TURN]: source "factsheet"; else `operator`
    when set (finite, >= 0; clamped): source "config"; else 0, source "off". `reason` is empty for the factsheet and names the
    missing field(s) otherwise. No speed is assumed."""
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
        return min(float(op), Safety.MAX_TURN), "config", reason
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
    if not turn > 0.0:
        return priced
    if priced is not None:
        return replace(priced, turn=float(turn))
    from packages.scene_graph.api import Safety
    from packages.services.mission_planner.lazy_plan import _CLR_CAP_M
    return Safety(margin=_CLR_CAP_M, rate=0.0, turn=float(turn))


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


def chord_cost(graph: Any, safety: Any) -> Callable[[Pt, Pt], float]:
    """The priced cost of a straight piece (a, b): its length times the mean of 1 + rate * deficit over samples every
    `PRICE_SAMPLE_M` (at the middle of equal sub-pieces), the deficit read from the sample's lattice cell (a non-navigable cell
    counts as clearance 0); the plain length when no clearance price is active (`safety` None or rate 0)."""
    if safety is None or float(safety.rate) <= 0.0:
        return math.dist
    h = float(graph.params.lattice)
    mu = float(safety.rate)
    price_cell = getattr(graph, "price_cell", None)
    memo: Dict[Tuple[int, int], float] = {}

    def factor(x: float, y: float) -> float:
        c = (int(math.floor(x / h)), int(math.floor(y / h)))
        f = memo.get(c)
        if f is None:
            if callable(price_cell):
                d = price_cell(c[0], c[1], safety)
            else:                                    # a graph that only has the hinge's deficit_cell
                d = graph.deficit_cell(c[0], c[1], float(safety.margin))
            # a non-navigable cell counts as clearance 0
            f = memo[c] = 1.0 + mu * (float(safety.price(0.0)) if d is None else d)
        return f

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


def priced_visible(visible: Callable[[Pt, Pt], bool], graph: Any, lattice_pts: Sequence[Pt],
                   safety: Any) -> Callable[[Pt, Pt], bool]:
    """Cost-aware line of sight (S11): `visible(a, b)` AND the priced cost of the segment (a, b) is at most that of the
    lattice sub-path between the two points (both found in `lattice_pts`, the route's points). The cost of a straight
    piece is its length times the mean of 1 + rate * deficit over samples every `PRICE_SAMPLE_M` (at the middle of equal
    sub-pieces), the deficit read from the sample's lattice cell (a non-navigable cell counts as clearance 0); the
    sub-path is measured the same way between its consecutive points. Then the final polyline costs no more than the
    lattice path. A point that is not on the route is judged by `visible` alone."""
    cost = chord_cost(graph, safety)
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


# Candidate thinning of the budget solver (`thin_indices`): the chords of the DP run between the indices kept by a Ramer-Douglas-Peucker
# simplification of the lattice path with tolerance THIN_FRACTION * lattice (m), plus the greedy pull's vertices, plus enough points
# to respect the gap cap. 0 = every lattice point is a candidate (the full DP). The cost accounting is not thinned.
WAYPOINT_THIN_FRACTION = 0.5
THIN_SPACING_M = 0.5             # candidates are at most this far apart (a chord may leave a straight run at any of its points)
APEX_TOL_M = 0.35                # an apex candidate lies at most this far from its corner vertex (the goal tolerance)
# Among paths with equal waypoints and slack the apex wins over the vertex of its corner (its clearance is at least the vertex's by
# construction; the slack is clamped at 0, so a chord that cuts the corner ties with the vertex's lattice chord). False: the vertex wins.
APEX_TIES_TO_APEX = True
APEX_MIN_TURN_RAD = math.radians(20.0)      # a vertex of the greedy pull is a corner when the pull turns by at least this


def thin_eps(graph: Any) -> float:
    """The candidate-thinning tolerance (m) of the budget solver for `graph`: `WAYPOINT_THIN_FRACTION` of its lattice step."""
    return max(0.0, float(WAYPOINT_THIN_FRACTION)) * float(getattr(getattr(graph, "params", None), "lattice", 0.0) or 0.0)


def greedy_pull_indices(pts: Sequence[Pt], visible: Callable[[Pt, Pt], bool], max_gap: float) -> List[int]:
    """Lattice indices of the vertices of `simplify(pts, visible, max_gap)` (`pts` without consecutive duplicates)."""
    where: List[int] = []
    pos = 0
    for q in simplify(pts, visible, max_gap):
        pos = pts.index(q, pos)
        where.append(pos)
    return where


def thin_indices(pts: Sequence[Pt], eps: float, keep: Sequence[int], max_gap: float) -> List[int]:
    """Sorted candidate indices of the budget solver: the first, the last, the `keep` indices, the points an iterative
    Ramer-Douglas-Peucker pass keeps at tolerance `eps` (a dropped point is within `eps` of the chord of its neighbours), and, where
    two consecutive candidates are farther apart than `THIN_SPACING_M` (and `max_gap`), the farthest later point within that
    (repeated): consecutive candidates are within the gap cap, and a straight run keeps a point every `THIN_SPACING_M`, where the
    optimum may leave it. Deterministic."""
    n = len(pts)
    mark = [False] * n
    mark[0] = mark[n - 1] = True
    for k in keep:
        mark[k] = True
    stack = [(0, n - 1)]
    while stack:
        a, b = stack.pop()
        if b - a < 2:
            continue
        ax, ay = pts[a]
        dx, dy = pts[b][0] - ax, pts[b][1] - ay
        norm = math.hypot(dx, dy)
        far, fd = -1, eps
        for m in range(a + 1, b):
            if norm > 0.0:
                d = abs(dx * (pts[m][1] - ay) - dy * (pts[m][0] - ax)) / norm
            else:
                d = math.dist(pts[m], pts[a])
            if d > fd:
                far, fd = m, d
        if far >= 0:
            mark[far] = True
            stack.append((a, far))
            stack.append((far, b))
    out = [k for k in range(n) if mark[k]]
    if THIN_SPACING_M > 0:
        step = min(max_gap, THIN_SPACING_M) if max_gap > 0 else THIN_SPACING_M
        full: List[int] = []
        for a, b in zip(out[:-1], out[1:]):
            full.append(a)
            while math.dist(pts[a], pts[b]) > step and b - a > 1:
                m = b - 1
                while m > a + 1 and math.dist(pts[a], pts[m]) > step:
                    m -= 1
                full.append(m)
                a = m
        full.append(out[-1])
        out = full
    return out


def apex_candidates(pts: Sequence[Pt], visible: Callable[[Pt, Pt], bool], max_gap: float, radius: float,
                    clearance: Callable[[Pt], float], tol: float = APEX_TOL_M,
                    pull_idx: Optional[Sequence[int]] = None) -> Tuple[int, Dict[int, Pt]]:
    """(corners, {lattice index k: apex point}) of the lattice path `pts` (no consecutive duplicates). A corner is an interior vertex
    of the greedy pull `simplify(pts, visible, max_gap)` where the pull turns by theta >= `APEX_MIN_TURN_RAD`. Its fillet arc of
    radius R is tangent at t = R tan(theta / 2) along both chords of the pull and its middle, the apex, lies on the inward bisector at
    R (1 / cos(theta / 2) - 1) from the vertex V. R is `radius`, clipped so that t is at most half of the shorter adjacent chord
    and the apex is at most `tol` from V. The apex is a candidate when its clearance is at least that of V (never closer to
    obstacles than the vertex; a point on a non-navigable cell has clearance 0, so it is never one when V is navigable)."""
    where = list(pull_idx) if pull_idx is not None else greedy_pull_indices(pts, visible, max_gap)
    pull = [pts[k] for k in where]
    corners = 0
    out: Dict[int, Pt] = {}
    for m in range(1, len(pull) - 1):
        prev, v, nxt = pull[m - 1], pull[m], pull[m + 1]
        l_in, l_out = math.dist(prev, v), math.dist(v, nxt)
        if l_in <= 0.0 or l_out <= 0.0:
            continue
        u_in = ((v[0] - prev[0]) / l_in, (v[1] - prev[1]) / l_in)
        u_out = ((nxt[0] - v[0]) / l_out, (nxt[1] - v[1]) / l_out)
        theta = math.acos(max(-1.0, min(1.0, u_in[0] * u_out[0] + u_in[1] * u_out[1])))
        if theta < APEX_MIN_TURN_RAD:
            continue
        corners += 1
        half = theta / 2.0
        c = math.cos(half)
        if c <= 1e-6:
            continue
        rise = 1.0 / c - 1.0                       # apex distance per unit radius
        r = min(float(radius), 0.5 * min(l_in, l_out) / math.tan(half), tol / rise)
        bx, by = u_out[0] - u_in[0], u_out[1] - u_in[1]
        norm = math.hypot(bx, by)
        if not r > 0.0 or norm <= 0.0:
            continue
        d = r * rise
        apex = (round(v[0] + d * bx / norm, 6), round(v[1] + d * by / norm, 6))
        if apex != v and clearance(apex) >= clearance(v) - 1e-12:
            out[where[m]] = apex
    return corners, out


def fewest_waypoints(points: Sequence[Pt], visible: Callable[[Pt, Pt], bool], max_gap: float,
                     cost: Callable[[Pt, Pt], float], budget_m: float, end_extra: float = 0.0,
                     apex_radius: float = 0.0, clearance: Optional[Callable[[Pt], float]] = None,
                     corner_visible: Optional[Callable[[Pt, Pt], bool]] = None,
                     info: Optional[Dict[str, int]] = None, thin_eps: float = 0.0) -> List[Pt]:
    """Fewest waypoints under a cost budget (docs/scene_graph/close_evidence_and_turn_cost_design.md C.2), exact.

    `points` are the lattice path p_0..p_{n-1} (consecutive duplicates dropped), `visible` the free-space line of sight,
    `cost` the priced cost of a chord (`chord_cost`), P(i, j) the cost of the lattice sub-path (prefix sums of `cost` over
    consecutive points). A chord (i, j) is allowed when it is visible and at most `max_gap` long (<= 0: no cap), or when
    j = i + 1 (the lattice step is always reachable, as in `simplify`); its slack is max(0, cost(i, j) - P(i, j)), rounded
    up to whole centimetres. The result is a path 0 -> n-1 with the fewest chords whose slacks sum to at most `budget_m`;
    a dynamic program over (point, slack used in cm), O(n * window * D). Ties: fewest waypoints, then least slack, then the
    lexicographically smallest index sequence (deterministic).

    Cost bound: the cost of a chord is at most P(i, j) + its slack, and the chords partition the lattice path, so
    J(final) = sum cost(chord) <= sum P(chord) + sum slack = J(lattice) + sum slack <= J(lattice) + budget_m. The rounding is
    upward, so the true slack never exceeds the counted one. With a budget of 0 only chords no dearer than their lattice
    sub-path are used (the cost bound of `priced_visible`), and the result has the fewest waypoints under that rule: the
    greedy string pull is not minimal-link, so it can keep more. A larger budget never gives more waypoints.
    The chord that ends at the last point may be `end_extra` longer than `max_gap` (the string pull's end-stub merge,
    `MIN_END_STUB_M`, does the same).

    Candidate thinning (`thin_eps` > 0): the DP runs over the indices of `thin_indices` only (the greedy pull's vertices are among
    them, `corner_visible` or `visible` judging it), but every chord's slack is still measured against the ORIGINAL lattice sub-path
    P(i, j) (prefix sums over all points), so the argument above is unchanged and J(final) <= J(lattice) + budget_m holds exactly:
    thinning only removes options. The waypoint count is then the minimum over the thinned set (not below the full DP's; with the
    pull's vertices kept, still not above the pull's when `corner_visible` is the priced line of sight). If the thinned set leaves no
    path (a chord between consecutive candidates not visible), the full DP is run. Off (0): every point is a candidate.

    Apex candidates (`apex_radius` > 0 and `clearance`, C.2 "Apex candidates"): every corner k of the greedy pull
    (`apex_candidates`, judged with `corner_visible`, default `visible`) gets a second node A_k, the apex of the fillet arc of radius
    `apex_radius` through the vertex, as an ALTERNATIVE to the vertex p_k at the same lattice index: a path uses p_k or A_k, never
    both. A chord from or to A_k needs the line of sight, the gap cap on its real length and the same slack rule, with the lattice
    sub-path of the two indices as the reference: A_k stands at index k, so the chords of a path still partition the index range
    0..n-1 and cost(chord) <= P(chord) + slack(chord) holds term by term; the argument above, hence J(final) <= J(lattice) + budget_m,
    is unchanged. A_k is a candidate only when its clearance is at least that of p_k (so a corner waypoint is never closer to an
    obstacle than the vertex it replaces). Without a candidate the nodes and the result are those of the plain solver. Ties:
    fewest waypoints, then least slack, then (`APEX_TIES_TO_APEX`) the apex of a corner before its vertex, then the lexicographically
    smallest sequence of (index, apex-or-vertex) ranks. The slack of a chord that cuts the corner is clamped at 0, like the vertex's,
    so without that rule the vertex would win every such tie and the apex would never be used. The apex is not a point of the lattice path: a plan that uses one
    has no `trajectory`. `info`, when given, gets {"corners" (of the greedy pull), "candidates" (corners that got an apex node), "apexes_used"}."""
    pts: List[Pt] = []
    for p in points:
        if not pts or p != pts[-1]:
            pts.append(p)
    if info is not None:
        info.update({"corners": 0, "candidates": 0, "apexes_used": 0})
    if len(pts) <= 2:
        return pts
    n = len(pts)
    apex: Dict[int, Pt] = {}
    pull_idx = greedy_pull_indices(pts, corner_visible or visible, max_gap) if thin_eps > 0.0 or (
        apex_radius > 0.0 and clearance is not None) else None
    if apex_radius > 0.0 and clearance is not None:
        corners, apex = apex_candidates(pts, corner_visible or visible, max_gap, apex_radius, clearance, pull_idx=pull_idx)
        if info is not None:
            info["corners"], info["candidates"] = corners, len(apex)
    budget = max(0, int(math.ceil(max(0.0, budget_m) * 100.0 - 1e-9)))
    arc = [0.0]
    prefix = [0.0]
    for k in range(1, n):
        arc.append(arc[-1] + math.dist(pts[k - 1], pts[k]))
        prefix.append(prefix[-1] + cost(pts[k - 1], pts[k]))
    if thin_eps > 0.0:
        cand = thin_indices(pts, thin_eps, pull_idx, max_gap)
        out = _budget_dp(pts, cand, apex, arc, prefix, visible, max_gap, cost, budget, end_extra, info)
        if out is not None:
            return out
    return _budget_dp(pts, list(range(n)), apex, arc, prefix, visible, max_gap, cost, budget, end_extra, info)


def _budget_dp(pts: Sequence[Pt], cand: Sequence[int], apex: Dict[int, Pt], arc: Sequence[float], prefix: Sequence[float],
               visible: Callable[[Pt, Pt], bool], max_gap: float, cost: Callable[[Pt, Pt], float], budget: int,
               end_extra: float, info: Optional[Dict[str, int]]) -> Optional[List[Pt]]:
    """The dynamic program of `fewest_waypoints` over the candidate indices `cand` (sorted, first and last included); None when no
    path reaches the last point."""
    n = len(pts)
    pad = APEX_TOL_M if apex else 0.0
    # best[2k + kind][s] = (chords, codes) of the best way to reach node (k, kind) (kind 1 = the apex) having used s cm of slack
    flip = 1 if APEX_TIES_TO_APEX else 0           # the path holds ranks 2k + (kind ^ flip): the smaller rank wins a tie
    best: List[Dict[int, Tuple[int, Tuple[int, ...]]]] = [dict() for _ in range(2 * n)]
    best[0][0] = (0, (flip,))
    for i in cand[:-1]:
        for kind in ((0, 1) if i in apex else (0,)):
            if not best[2 * i + kind]:
                continue
            src = apex[i] if kind else pts[i]
            reach = max_gap + pad if kind == 0 else max_gap + APEX_TOL_M     # window of the chords of this source
            if max_gap > 0:
                end = max(i + 1, min(n - 1, bisect.bisect_right(arc, arc[i] + max_gap) - 1))
                while end < n - 1 and math.dist(src, pts[end + 1]) <= max_gap:
                    end += 1
                end_w = end
                if pad or kind:                          # a chord to an apex node may be a little longer than the arc window
                    end_w = max(end, min(n - 1, bisect.bisect_right(arc, arc[i] + reach) - 1))
                    while end_w < n - 1 and math.dist(src, pts[end_w + 1]) <= reach:
                        end_w += 1
            else:
                end = end_w = n - 1
            lo = bisect.bisect_right(cand, i)
            candidates = cand[lo:bisect.bisect_right(cand, end_w)]
            last = max_gap > 0 and end < n - 1 and math.dist(src, pts[n - 1]) <= max_gap + end_extra
            if last and (not candidates or candidates[-1] != n - 1):
                candidates = candidates + [n - 1]
            elif not candidates and lo < len(cand):
                candidates = [cand[lo]]                  # the next candidate is tried even beyond the window
            nxt = cand[lo] if lo < len(cand) else -1
            for j in candidates:
                for tkind in ((0, 1) if j in apex else (0,)):
                    dst = apex[j] if tkind else pts[j]
                    if kind == 0 and tkind == 0 and j == i + 1:
                        slack = 0
                    else:
                        if (kind or tkind) and max_gap > 0 \
                                and math.dist(src, dst) > max_gap + (end_extra if j == n - 1 else 0.0):
                            continue
                        if kind == 0 and tkind == 0 and j > end and j != nxt and not (j == n - 1 and last):
                            continue                     # a vertex chord stays inside the window of the plain solver
                        if not visible(src, dst):
                            continue
                        ref = prefix[j] - prefix[i]
                        over = cost(src, dst) - ref
                        slack = 0 if over <= ref * 1e-9 + 1e-9 else int(math.ceil(over * 100.0 - 1e-9))
                    if slack > budget:
                        continue
                    target = best[2 * j + tkind]
                    for s, (count, path) in best[2 * i + kind].items():
                        ns = s + slack
                        if ns > budget:
                            continue
                        new = (count + 1, path + (2 * j + (tkind ^ flip),))
                        if ns not in target or new < target[ns]:
                            target[ns] = new
    if not best[2 * (n - 1)]:
        return None
    _, _, path = min((count, s, path) for s, (count, path) in best[2 * (n - 1)].items())
    if info is not None:
        info["apexes_used"] = sum(1 for c in path if (c & 1) ^ flip)
    return [apex[c >> 1] if (c & 1) ^ flip else pts[c >> 1] for c in path]


def budget_pull(graph: Any, safety: Any, budget_m: float) -> Callable[..., List[Pt]]:
    """A `simplify` replacement for the gap fill (`node_policy_plan.insert_planned`): `fewest_waypoints` with this budget.
    It takes the plain line of sight (`unpriced_visible`): the price is in the budget."""
    cost = chord_cost(graph, safety)

    def pull(points: Sequence[Pt], visible: Callable[[Pt, Pt], bool], max_gap: float) -> List[Pt]:
        return fewest_waypoints(points, visible, max_gap, cost, budget_m, thin_eps=thin_eps(graph))
    pull.unpriced_visible = True  # type: ignore[attr-defined]
    return pull


def cell_clearance(graph: Any) -> Optional[Callable[[Pt], float]]:
    """The lattice clearance (m, capped at `lazy_plan._CLR_CAP_M`; 0 on a non-navigable cell) of a point, read through
    `deficit_cell` as in `waypoints_below_floor`; None for a graph without it."""
    if not callable(getattr(graph, "deficit_cell", None)):
        return None
    from packages.services.mission_planner.lazy_plan import _CLR_CAP_M
    h = float(graph.params.lattice)

    def clearance(p: Pt) -> float:
        d = graph.deficit_cell(int(math.floor(p[0] / h)), int(math.floor(p[1] / h)), _CLR_CAP_M)
        return 0.0 if d is None else _CLR_CAP_M - d
    return clearance


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


def plan_on_graph(graph: Any, map_id: str, start_xy: Pt, goal_xy: Pt,
                  overlay: Tuple[list, list], node_of: Callable[[float, float], Optional[str]],
                  start_snap_m: float, goal_snap_m: float, max_gap_m: float,
                  robot_width_m: Optional[float] = None, width_margin_m: float = 0.0,
                  robot_length_m: Optional[float] = None,
                  node_policy_cfg: Optional[Dict[str, Any]] = None, safety_kappa: float = 0.0,
                  width_check: bool = True, trajectory_max_points: int = 0,
                  turn: Optional[Dict[str, Any]] = None, waypoint_budget_m: float = 0.0,
                  waypoint_apex: bool = False) -> ChartPlan:
    """`_plan_on_graph`; a tile of a lazy graph that cannot be read (a garbage-collected version, a backend error)
    is ChartFallback("tile_unavailable"), and the graph is flagged so `GraphCache` drops it."""
    from packages.services.mission_planner.lazy_plan import TileUnavailable
    try:
        return _plan_on_graph(graph, map_id, start_xy, goal_xy, overlay, node_of, start_snap_m, goal_snap_m,
                              max_gap_m, robot_width_m, width_margin_m, robot_length_m,
                              node_policy_cfg, safety_kappa, width_check, trajectory_max_points, turn,
                              waypoint_budget_m, waypoint_apex)
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
                   waypoint_apex: bool = False) -> ChartPlan:
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
    the budget); 0 = the string pull, unchanged. `waypoint_apex` (only with a budget and a turn lambda > 0, `fewest_waypoints`):
    a corner waypoint may sit at the apex of a fillet arc of radius lambda; `ChartPlan.waypoint_apex` is the record; the plan
    then has no `trajectory` when it uses one (an apex is not on the lattice path)."""
    from packages.scene_graph.api import Blocked, MissingTiles
    from packages.services.mission_planner.lazy_plan import CorridorTooLarge
    snap_a = graph.snap(start_xy[0], start_xy[1], start_snap_m)
    problem = snap_problem(snap_a, start_snap_m)
    if problem:
        raise ChartFallback("start_" + problem)
    snap_b = graph.snap(goal_xy[0], goal_xy[1], goal_snap_m)
    problem = snap_problem(snap_b, goal_snap_m)
    if problem:
        raise ChartFallback("goal_" + problem)
    discs, segments = overlay
    blocked = Blocked(discs=list(discs), segments=list(segments)) if (discs or segments) else None
    turn = dict(TURN_NOT_GIVEN) if turn is None else dict(turn)
    safety = safety_for(graph, robot_width_m, robot_length_m, safety_kappa, turn["lambda_m_per_rad"])
    try:
        route = graph.route(snap_a.key, snap_b.key, blocked, start=(snap_a.x, snap_a.y),
                            goal=(snap_b.x, snap_b.y), **({} if safety is None else {"safety": safety}))
    except MissingTiles as err:
        raise ChartFallback("missing_tiles") from err
    except CorridorTooLarge as err:
        raise ChartFallback("corridor_too_large") from err
    if route is None or not route.nodes:
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
    visible = blocked_free_los(graph, overlay)
    plain = visible
    if safety is not None and safety.rate > 0.0:
        visible = priced_visible(visible, graph, pts, safety)
    budget_record = None
    apex_record = None
    if waypoint_budget_m > 0.0:
        apex_info: Dict[str, int] = {}
        radius = float(turn["lambda_m_per_rad"]) if waypoint_apex else 0.0
        pulled = fewest_waypoints(pts, plain, max_gap_m, chord_cost(graph, safety), waypoint_budget_m, MIN_END_STUB_M,
                                  radius, cell_clearance(graph) if radius > 0.0 else None, visible, apex_info, thin_eps(graph))
        if radius > 0.0:
            apex_record = dict(apex_info)
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
        cost = chord_cost(graph, safety)
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
    poses, planned = build_waypoints(pts, map_id, lambda x, y: by_pt[(x, y)], allow_repeats=True)
    if planned is None:
        raise ChartFallback("waypoints_without_distinct_leaf_ids")
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
            graph, poses, range(1, len(poses) - 1), node_policy_cfg.get("robot_width"),
            node_policy_cfg.get("margin", 0.0), node_policy_cfg.get("snap_m", start_snap_m),
            node_policy_cfg.get("budget_s", 0.0), safety=safety)
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
    return ChartPlan(waypoints=poses, planned_path=planned, charts=list(route.charts),
                     min_width=min_width, goal_xy=(snap_b.x, snap_b.y),
                     repeated_leaf_ids=len(set(planned)) != len(planned), node_policy=report, pricing=pricing,
                     trajectory=legs, waypoint_budget=budget_record, waypoint_apex=apex_record)
