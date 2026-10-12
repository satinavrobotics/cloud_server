"""Turn a scene-graph lattice route into the planner's plain waypoint poses.

Pure functions, no I/O and no engine import: the planner supplies the line-of-sight test and the
nearest-leaf lookup. The output contract is the one the dispatcher, leg tracker and edge statistics
already rely on (docs/scene_graph/integration.md, "Planner contract"):

* waypoints are `Pose2D` in the map frame, `theta` = bearing to the next waypoint, the last one the
  goal heading;
* `node_id` = the nearest leaf within a threshold, else None;
* `planned_path` is returned only when every waypoint has a leaf id, because the leg tracker pairs
  `waypoints[i]` with `planned_path[i]` and drops topomap ids on any length mismatch.
"""
import asyncio
import logging
import math
import time
from collections import OrderedDict
from dataclasses import dataclass, field
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
    step are the points up to the first one beyond `max_gap`, so the cost is O(points x window),
    not O(points^2): a 1 km route on a 0.1 m lattice has 10 000 points."""
    pts: List[Pt] = []
    for p in points:
        if not pts or p != pts[-1]:
            pts.append(p)
    if len(pts) <= 2:
        return pts
    kept = [pts[0]]
    i, last = 0, len(pts) - 1
    while i < last:
        best = i + 1                       # the next lattice point is always reachable
        for k in range(i + 2, last + 1):
            if max_gap > 0 and math.dist(pts[i], pts[k]) > max_gap:
                break
            if visible(pts[i], pts[k]):
                best = k
        kept.append(pts[best])
        i = best
    return kept


def build_waypoints(points: Sequence[Pt], map_id: str,
                    node_of: Callable[[float, float], Optional[str]],
                    goal_theta: Optional[float] = None
                    ) -> Tuple[List[common.Pose2D], Optional[List[str]]]:
    """(waypoints, planned_path or None) for an already simplified point list.
    `planned_path` is None unless every waypoint has a distinct leaf id."""
    pts = list(points)
    poses: List[common.Pose2D] = []
    for i, (x, y) in enumerate(pts):
        if i + 1 < len(pts):
            nx, ny = pts[i + 1]
            theta = math.atan2(ny - y, nx - x)
        elif goal_theta is not None:
            theta = goal_theta
        elif i > 0:
            theta = poses[-1].theta
        else:
            theta = 0.0
        poses.append(common.Pose2D(x=x, y=y, theta=theta, map_id=map_id, node_id=node_of(x, y)))
    ids = [p.node_id for p in poses]
    # one id per waypoint, no repeats: two waypoints snapped to the same leaf would be a zero-length
    # leg for the leg tracker, so such a route carries no planned_path (its legs are VDA-id only).
    planned = ([str(n) for n in ids]
               if poses and all(n is not None for n in ids) and len(set(ids)) == len(ids) else None)
    return poses, planned


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
    length: float
    start_xy: Pt                       # snapped start / goal cells
    goal_xy: Pt
    version: Optional[int] = None


class GraphCache:
    """Loaded scene graphs by (map, version), a small LRU. `store_factory()` returns the store (called
    lazily, in a worker thread, because it connects). `get` never waits longer than `load_timeout_s`:
    a cold load keeps running in its thread, shared by later requests, and fills the cache when it
    ends; this request falls back."""

    RETRY_STORE_S = 30.0

    def __init__(self, store_factory: Callable[[], Any], max_entries: int = 4,
                 load_timeout_s: float = 2.0, clock: Callable[[], float] = None):
        import time
        self._factory = store_factory
        self._max = max(1, int(max_entries))
        self._timeout = float(load_timeout_s)
        self._clock = clock or time.monotonic
        self._store: Any = None
        self._store_failed_at: Optional[float] = None
        self._graphs: "OrderedDict[Tuple[str, int], Any]" = OrderedDict()
        self._loading: Dict[Tuple[str, int], "asyncio.Future"] = {}
        self.loads = 0                 # store.load calls started (tests, metrics)
        self._cursors: Dict[str, Any] = {}   # map -> ingest cursor of the version `get` last returned

    def _open_store(self):
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

    def _insert(self, key, graph) -> None:
        self._graphs[key] = graph
        self._graphs.move_to_end(key)
        while len(self._graphs) > self._max:
            self._graphs.popitem(last=False)

    def cursor(self, map_id: str) -> Any:
        """The ingest cursor `[costmap_stored_ms, node key]` stored with the version `get` last
        returned for the map (None when unknown)."""
        return self._cursors.get(map_id)

    async def get(self, map_id: str) -> Tuple[Any, int]:
        """(graph, version) of the map's current version; ChartFallback otherwise."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._timeout

        def left() -> float:
            return max(0.01, deadline - loop.time())

        try:
            store = await asyncio.wait_for(loop.run_in_executor(None, self._open_store), left())
            meta = await asyncio.wait_for(loop.run_in_executor(None, store.meta, map_id), left())
        except ChartFallback:
            raise
        except asyncio.TimeoutError as err:
            raise ChartFallback("store_timeout") from err
        except Exception as err:  # pylint: disable=broad-except
            _log.warning("scene graph meta of map %s failed: %s", map_id, err)
            raise ChartFallback("store_error") from err
        version = (meta or {}).get("version")
        if version is None:
            raise ChartFallback("no_graph")
        self._cursors[map_id] = (meta or {}).get("cursor")
        key = (map_id, int(version))
        if key in self._graphs:
            self._graphs.move_to_end(key)
            return self._graphs[key], key[1]
        fut = self._loading.get(key)
        if fut is None:
            self.loads += 1
            fut = loop.run_in_executor(None, store.load, map_id, key[1])
            self._loading[key] = fut

            def done(f, key=key):
                self._loading.pop(key, None)
                if f.cancelled() or f.exception() is not None:
                    if not f.cancelled():
                        _log.warning("scene graph load %s failed: %s", key, f.exception())
                    return
                self._insert(key, f.result())
            fut.add_done_callback(done)
        try:
            graph = await asyncio.wait_for(asyncio.shield(fut), left())
        except asyncio.TimeoutError as err:
            raise ChartFallback("load_timeout") from err
        except Exception as err:  # pylint: disable=broad-except
            raise ChartFallback("load_error") from err
        return graph, key[1]


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
    every half lattice step, every sample's lattice cell must be navigable (its centre is the centre
    of a free cell: `snap` distance <= half a step; memoised per cell), and the segment must clear
    the transient blocked overlay."""
    from packages.utils import blocked_geometry as bg
    h = float(graph.params.lattice)
    discs, segments = overlay
    cells: Dict[Tuple[int, int], bool] = {}

    def free(i: int, j: int) -> bool:
        v = cells.get((i, j))
        if v is None:
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
        n = max(1, int(math.dist(a, b) / (0.5 * h)))
        for k in range(n + 1):
            t = k / n
            x, y = a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1])
            if not free(int(math.floor(x / h)), int(math.floor(y / h))):
                return False
        return True

    return visible


def plan_on_graph(graph: Any, map_id: str, start_xy: Pt, goal_xy: Pt,
                  overlay: Tuple[list, list], node_of: Callable[[float, float], Optional[str]],
                  start_snap_m: float, goal_snap_m: float, max_gap_m: float,
                  robot_width_m: Optional[float] = None, width_margin_m: float = 0.0) -> ChartPlan:
    """Snap, route around the overlay, simplify, convert to poses. Blocking (run it in a thread).
    Raises ChartFallback for every case the legacy planner must take over. `robot_width_m` is the
    planning robot's footprint width (None / <= 0: unknown, never rejects): a route whose narrowest
    passage is under it plus `width_margin_m` is `too_narrow`."""
    from packages.scene_graph.api import Blocked, MissingTiles
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
        route = graph.route(snap_a.key, snap_b.key, blocked)
    except MissingTiles as err:
        raise ChartFallback("missing_tiles") from err
    if route is None or not route.nodes:
        if blocked is not None:
            # unreachable only because of the overlay? say so (the legacy planner then reports the
            # blocking rows as failed_at "blocked_nodes").
            try:
                free = graph.route(snap_a.key, snap_b.key, None)
            except MissingTiles:
                free = None
            if free is not None and free.nodes:
                raise ChartFallback("blocked_by_overlay")
        raise ChartFallback("unreachable")
    if robot_width_m is not None and math.isfinite(robot_width_m) and robot_width_m > 0.0 \
            and float(route.min_width) < float(robot_width_m) + max(0.0, width_margin_m):
        raise ChartFallback("too_narrow")
    # The lattice route runs anchor to anchor; the real ends are the snapped start and goal cells.
    pts = [(snap_a.x, snap_a.y)] + [tuple(p) for p in route.nodes] + [(snap_b.x, snap_b.y)]
    visible = blocked_free_los(graph, overlay)
    pts = simplify(pts, visible, max_gap_m)
    # The gap cap can leave a stub of a few lattice steps at the end (two waypoints on one leaf =
    # a zero-length leg): merge it into the previous leg when that stays in line of sight.
    while len(pts) >= 3 and math.dist(pts[-2], pts[-1]) < MIN_END_STUB_M \
            and visible(pts[-3], pts[-1]):
        del pts[-2]
    poses, planned = build_waypoints(pts, map_id, node_of)
    if planned is None:
        raise ChartFallback("waypoints_without_distinct_leaf_ids")
    return ChartPlan(waypoints=poses, planned_path=planned, charts=list(route.charts),
                     min_width=float(route.min_width), length=float(route.length),
                     start_xy=(snap_a.x, snap_a.y), goal_xy=(snap_b.x, snap_b.y))
