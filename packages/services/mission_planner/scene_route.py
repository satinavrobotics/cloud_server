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
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from cloud_common.objects import common

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


def plan_on_graph(graph: Any, map_id: str, start_xy: Pt, goal_xy: Pt,
                  overlay: Tuple[list, list], node_of: Callable[[float, float], Optional[str]],
                  start_snap_m: float, goal_snap_m: float, max_gap_m: float,
                  robot_width_m: Optional[float] = None, width_margin_m: float = 0.0,
                  robot_length_m: Optional[float] = None) -> ChartPlan:
    """`_plan_on_graph`; a tile of a lazy graph that cannot be read (a garbage-collected version, a backend error)
    is ChartFallback("tile_unavailable"), and the graph is flagged so `GraphCache` drops it."""
    from packages.services.mission_planner.lazy_plan import TileUnavailable
    try:
        return _plan_on_graph(graph, map_id, start_xy, goal_xy, overlay, node_of, start_snap_m, goal_snap_m,
                              max_gap_m, robot_width_m, width_margin_m, robot_length_m)
    except TileUnavailable as err:
        _log.warning("scene graph tile unavailable (%s); the graph is dropped", err)
        raise ChartFallback("tile_unavailable") from err


def _plan_on_graph(graph: Any, map_id: str, start_xy: Pt, goal_xy: Pt,
                   overlay: Tuple[list, list], node_of: Callable[[float, float], Optional[str]],
                   start_snap_m: float, goal_snap_m: float, max_gap_m: float,
                   robot_width_m: Optional[float] = None, width_margin_m: float = 0.0,
                   robot_length_m: Optional[float] = None) -> ChartPlan:
    """Snap, route around the overlay, simplify, convert to poses. Blocking (run it in a thread).
    Raises ChartFallback for every case the legacy planner must take over. `robot_width_m` is the
    planning robot's footprint width (None / <= 0: unknown, never rejects; `robot_length_m` likewise
    optional): the route is `too_narrow` when its narrowest passage is under `required_width` (the
    width, or the footprint diagonal when the simplified route turns in place, plus
    `width_margin_m`); the caller gates it by SCENE_GRAPH_WIDTH_CHECK_ENABLED. `node_of` may raise:
    that is `leaf_lookup_error`, a point with no leaf near it returns None."""
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
    try:
        route = graph.route(snap_a.key, snap_b.key, blocked, start=(snap_a.x, snap_a.y),
                            goal=(snap_b.x, snap_b.y))
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
    check_width = robot_width_m is not None and math.isfinite(robot_width_m) and robot_width_m > 0.0
    min_width = float(route.min_width)
    if check_width and min_width < required_width(robot_width_m, robot_length_m, False,
                                                  width_margin_m):
        raise ChartFallback("too_narrow")       # not even aligned: no need to simplify
    # The lattice route runs from the snapped start cell to the snapped goal cell (point to point); the
    # prepend/append only matters when an end falls back to its chart anchor.
    pts = [(snap_a.x, snap_a.y)] + [tuple(p) for p in route.nodes] + [(snap_b.x, snap_b.y)]
    visible = blocked_free_los(graph, overlay)
    pts = simplify(pts, visible, max_gap_m)
    # The gap cap can leave a stub of a few lattice steps at the end (two waypoints on one leaf =
    # a zero-length leg): merge it into the previous leg when that stays in line of sight.
    while len(pts) >= 3 and math.dist(pts[-2], pts[-1]) < MIN_END_STUB_M \
            and visible(pts[-3], pts[-1]):
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
    return ChartPlan(waypoints=poses, planned_path=planned, charts=list(route.charts),
                     min_width=min_width, goal_xy=(snap_b.x, snap_b.y),
                     repeated_leaf_ids=len(set(planned)) != len(planned))
