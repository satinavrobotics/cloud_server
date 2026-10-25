"""Keep-out zones as planner overlay (docs/keepout_zones.md): the enabled `map_keepouts` rows of a map resolved, against one
scene-graph version, to the lattice cells the route search must not use (`Blocked.cells`), the same overlay machinery the
robot-reported blocks use.

* region by chart key: the cells the chart owns (the engine's owner plane around its anchor);
* region by polygon: the lattice cells whose centre is inside the polygon (needs no graph);
* edge: the cells of the two charts that touch the other chart (8-neighbourhood), a two-cell-thick seal along their whole shared
  boundary, so no crossing between exactly those two charts is left (their other neighbours stay open, except where a cell of the
  seal also touches one).

A zone that cannot be mapped onto this version (the chart no longer exists, no link joins the two charts, a point is off the graph,
a polygon holds no cell) is `unresolved`: the planner ignores it and says so.

Resolution depends on the graph version and the keep-out revision only (not on the robot), so it is cached per
(map, version, revision) and the planner's hot path pays a dict lookup. The graph needs `params`, `snap`, `chart_anchors()`,
`chart_links()` and `owner_window()` (ChartGraph and PlanGraph have them).
"""
import math
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

import numpy as np
from scipy import ndimage

from packages.scene_graph.api import cell_keys
from packages.utils import keepouts as ko

Pt = Tuple[float, float]

MAX_CELLS = 2_000_000          # lattice cells of all zones of a map together (200 x 200 m at 0.1 m); beyond it the plan is refused
RESOLVED_CACHE = 16            # (map, version, revision) resolutions kept
ROWS_TTL_S = 2.0               # how long the planner trusts the rows it read, per map (an edit is live within this)

R_NO_CHART = "chart_not_found"
R_NO_LINK = "no_link_between_charts"
R_OFF_GRAPH = "point_off_graph"
R_EMPTY = "no_cells"
R_NO_GRAPH = "no_scene_graph"


class KeepoutTooLarge(Exception):
    """The zones together hold more cells than MAX_CELLS: refused, never silently dropped."""


@dataclass
class Zone:
    id: str
    label: Optional[str]
    kind: str
    applies: str                          # keepouts.APPLIED | keepouts.UNRESOLVED
    reason: Optional[str]
    cells: np.ndarray                     # sorted unique int64 `cell_keys`
    box: Optional[Tuple[float, float, float, float]]   # map-frame bbox of the cells


@dataclass
class Resolution:
    revision: str
    version: Optional[int]
    zones: List[Zone] = field(default_factory=list)
    cells: np.ndarray = field(default_factory=lambda: np.zeros(0, np.int64))   # union of the applied zones
    _keyset: Optional[frozenset] = field(default=None, repr=False, compare=False)

    @property
    def keyset(self) -> frozenset:
        """`cells` as a set of ints (the line-of-sight test), made once."""
        if self._keyset is None:
            self._keyset = frozenset(self.cells.tolist())
        return self._keyset

    @property
    def applied(self) -> List[Zone]:
        return [z for z in self.zones if z.applies == ko.APPLIED]

    @property
    def unresolved(self) -> List[Dict[str, Any]]:
        return [{"id": z.id, "reason": z.reason} for z in self.zones if z.applies == ko.UNRESOLVED]

    def __bool__(self) -> bool:
        return len(self.cells) > 0

    def zones_hit(self, cells: np.ndarray) -> List[Zone]:
        """The applied zones holding any of `cells` (keys)."""
        if not len(cells):
            return []
        return [z for z in self.applied if len(z.cells) and np.isin(cells, z.cells).any()]

    def summary(self) -> Dict[str, Any]:
        return {"applied": [z.id for z in self.applied], "unresolved": self.unresolved}


# --- cells of a zone ---------------------------------------------------------------------------------------------------

def _keys_of_mask(mask: np.ndarray, ix0: int, iy0: int) -> np.ndarray:
    ys, xs = np.nonzero(mask)
    return np.unique(cell_keys(xs + ix0, ys + iy0))


def polygon_cells(poly: Sequence[Sequence[float]], h: float) -> np.ndarray:
    """Keys of the lattice cells whose centre is inside `poly` (even-odd rule)."""
    xs = [p[0] for p in poly]
    ys = [p[1] for p in poly]
    ix0, ix1 = int(math.floor(min(xs) / h)), int(math.floor(max(xs) / h))
    iy0, iy1 = int(math.floor(min(ys) / h)), int(math.floor(max(ys) / h))
    if (ix1 - ix0 + 1) * (iy1 - iy0 + 1) > MAX_CELLS:
        raise KeepoutTooLarge("polygon")
    gx, gy = np.meshgrid((np.arange(ix0, ix1 + 1) + 0.5) * h, (np.arange(iy0, iy1 + 1) + 0.5) * h)
    inside = np.zeros(gx.shape, bool)
    n = len(poly)
    for i in range(n):
        (xa, ya), (xb, yb) = poly[i], poly[(i + 1) % n]
        if ya == yb:
            continue
        crosses = (ya > gy) != (yb > gy)
        with np.errstate(divide="ignore", invalid="ignore"):
            x_at = xa + (gy - ya) * (xb - xa) / (yb - ya)
        inside ^= crosses & (gx < x_at)
    return _keys_of_mask(inside, ix0, iy0)


def _box_around(points: Sequence[Pt], pad: float) -> Tuple[float, float, float, float]:
    return (min(p[0] for p in points) - pad, min(p[1] for p in points) - pad,
            max(p[0] for p in points) + pad, max(p[1] for p in points) + pad)


def _chart_mask(graph: Any, anchors: Mapping[str, Pt], keys: Sequence[str]):
    """(ix0, iy0, {key: bool mask}) over a window holding the charts `keys`."""
    pad = float(graph.params.chart_radius) + 3.0 * float(graph.params.lattice)
    ix0, iy0, idx, names = graph.owner_window(*_box_around([anchors[k] for k in keys], pad))
    where = {k: i for i, k in enumerate(names)}
    return ix0, iy0, {k: (idx == where[k]) if k in where else np.zeros(idx.shape, bool) for k in keys}


def chart_cells(graph: Any, anchors: Mapping[str, Pt], key: str) -> np.ndarray:
    ix0, iy0, masks = _chart_mask(graph, anchors, [key])
    return _keys_of_mask(masks[key], ix0, iy0)


def edge_cells(graph: Any, anchors: Mapping[str, Pt], a: str, b: str) -> np.ndarray:
    """The seal along the shared boundary of charts a and b (see the module doc)."""
    ix0, iy0, masks = _chart_mask(graph, anchors, [a, b])
    ma, mb = masks[a], masks[b]
    ring = np.ones((3, 3), bool)
    seal = (ndimage.binary_dilation(ma, ring) & mb) | (ndimage.binary_dilation(mb, ring) & ma)
    return _keys_of_mask(seal, ix0, iy0)


def _box_of(cells: np.ndarray, h: float) -> Optional[Tuple[float, float, float, float]]:
    if not len(cells):
        return None
    ix = (cells % (1 << 21)) - (1 << 20)
    iy = (cells // (1 << 21)) - (1 << 20)
    return (float(ix.min()) * h, float(iy.min()) * h, float(ix.max() + 1) * h, float(iy.max() + 1) * h)


# --- resolution ----------------------------------------------------------------------------------------------------------

def resolve(graph: Optional[Any], rows: Sequence[Mapping[str, Any]], version: Optional[int] = None) -> Resolution:
    """Resolve the (enabled) `rows` against `graph` (None: no scene graph, only polygons resolve). Raises KeepoutTooLarge, and
    whatever a tile read raises (TileUnavailable): a failed read is not a quiet "unresolved"."""
    res = Resolution(ko.revision(rows), version)
    h = float(graph.params.lattice) if graph is not None else 0.1
    anchors: Optional[Dict[str, Pt]] = None
    links = None
    total = 0
    for row in rows:
        spec, cells, reason = row["spec"], np.zeros(0, np.int64), None
        if "polygon" in spec:
            cells = polygon_cells(spec["polygon"], h)
            reason = None if len(cells) else R_EMPTY
        elif graph is None:
            reason = R_NO_GRAPH
        else:
            if anchors is None:
                anchors, links = graph.chart_anchors(), graph.chart_links()
            if "chart_key" in spec:
                key = spec["chart_key"]
                if key not in anchors:
                    reason = R_NO_CHART
                else:
                    cells = chart_cells(graph, anchors, key)
                    reason = None if len(cells) else R_EMPTY
            else:
                ends = []
                for end in (spec["from"], spec["to"]):
                    if isinstance(end, str):
                        ends.append(end if end in anchors else None)
                    else:
                        snap = graph.snap(float(end[0]), float(end[1]))
                        ends.append(snap.key if snap.key in anchors else None)
                if None in ends:
                    reason = R_NO_CHART if any(isinstance(e, str) for e in (spec["from"], spec["to"])) else R_OFF_GRAPH
                elif ends[0] == ends[1] or frozenset(ends) not in links:
                    reason = R_NO_LINK
                else:
                    cells = edge_cells(graph, anchors, ends[0], ends[1])
                    reason = None if len(cells) else R_EMPTY
        total += len(cells)
        if total > MAX_CELLS:
            raise KeepoutTooLarge(row["id"])
        res.zones.append(Zone(str(row["id"]), row.get("label"), row["kind"],
                              ko.UNRESOLVED if reason else ko.APPLIED, reason, cells, _box_of(cells, h)))
    applied = [z.cells for z in res.zones if z.applies == ko.APPLIED and len(z.cells)]
    res.cells = np.unique(np.concatenate(applied)) if applied else np.zeros(0, np.int64)
    return res


def cells_of_points(points: Sequence[Pt], h: float) -> np.ndarray:
    """Keys of the lattice cells the points fall in."""
    if not len(points):
        return np.zeros(0, np.int64)
    a = np.asarray(points, float)
    return np.unique(cell_keys(np.floor(a[:, 0] / h), np.floor(a[:, 1] / h)))


def legs_cross(res: Resolution, a: Pt, b: Pt, h: float) -> List[Zone]:
    """The applied zones whose cells the straight leg a -> b passes (sampled every quarter lattice step)."""
    if not res:
        return []
    n = max(1, int(math.ceil(math.dist(a, b) / (0.25 * h))))
    pts = [(a[0] + (b[0] - a[0]) * k / n, a[1] + (b[1] - a[1]) * k / n) for k in range(n + 1)]
    return res.zones_hit(cells_of_points(pts, h))


# --- the planner's cache -----------------------------------------------------------------------------------------------------

class KeepoutOverlays:
    """Per-map keep-out state of one planner process: the enabled rows (re-read after ROWS_TTL_S) and the resolutions per
    (map, graph version, revision). `load` is `async (map_id) -> rows` (keepouts.fetch with enabled_only)."""

    def __init__(self, load: Callable[[str], Any], clock: Callable[[], float],
                 ttl_s: float = ROWS_TTL_S, max_resolved: int = RESOLVED_CACHE) -> None:
        self._load, self._clock, self._ttl = load, clock, ttl_s
        self._rows: Dict[str, Tuple[float, List[Dict[str, Any]], str]] = {}
        self._resolved: "OrderedDict[Tuple[str, Any, str], Resolution]" = OrderedDict()
        self._max = max_resolved
        self._lock = threading.Lock()

    async def rows(self, map_id: str) -> Tuple[List[Dict[str, Any]], str]:
        """(enabled rows, revision) of the map. A read that fails RAISES: the planner must not plan around a zone it could
        not read as if there were none."""
        now = self._clock()
        hit = self._rows.get(map_id)
        if hit is not None and now - hit[0] < self._ttl:
            return hit[1], hit[2]
        rows = list(await self._load(map_id))
        rev = ko.revision(rows) if rows else ""
        self._rows[map_id] = (now, rows, rev)
        return rows, rev

    def invalidate(self, map_id: Optional[str] = None) -> None:
        if map_id is None:
            self._rows.clear()
        else:
            self._rows.pop(map_id, None)

    def resolve(self, map_id: str, version: Any, revision: str, rows: Sequence[Mapping[str, Any]],
                graph: Optional[Any]) -> Resolution:
        """The cached resolution; blocking (tile reads) when it has to be made: call it from the plan thread."""
        key = (map_id, version, revision)
        with self._lock:
            hit = self._resolved.get(key)
            if hit is not None:
                self._resolved.move_to_end(key)
                return hit
        res = resolve(graph, rows, version if isinstance(version, int) else None)
        with self._lock:
            self._resolved[key] = res
            while len(self._resolved) > self._max:
                self._resolved.popitem(last=False)
        return res


# --- the legacy (graph node) planner: polygon zones only ---------------------------------------------------------------------

@dataclass
class LegacyOutcome:
    path: List[str]
    error: Optional[str]
    report: Dict[str, Any]


def point_in_polygon(x: float, y: float, poly: Sequence[Sequence[float]]) -> bool:
    """Even-odd rule."""
    inside = False
    n = len(poly)
    for i in range(n):
        (xa, ya), (xb, yb) = poly[i], poly[(i + 1) % n]
        if (ya > y) != (yb > y) and x < xa + (y - ya) * (xb - xa) / (yb - ya):
            inside = not inside
    return inside


def leg_crosses_polygon(a: Pt, b: Pt, poly: Sequence[Sequence[float]]) -> bool:
    """Whether the straight leg a -> b enters the polygon (an end inside, or the leg crosses its boundary)."""
    from packages.utils import blocked_geometry as bg
    if point_in_polygon(a[0], a[1], poly) or point_in_polygon(b[0], b[1], poly):
        return True
    n = len(poly)
    return any(bg.segments_intersect(a, b, tuple(poly[i]), tuple(poly[(i + 1) % n])) for i in range(n))


def legacy_polygon_path(rows: Sequence[Mapping[str, Any]], nodes: Sequence[Mapping[str, Any]],
                        edges: Sequence[Mapping[str, Any]], path: Sequence[str], start: str, end: str) -> LegacyOutcome:
    """Keep the legacy planner's graph-node `path` out of the polygon zones in `rows`: the path itself if it clears them,
    else the fewest-hops path that passes no node inside a zone and no edge that enters one (the robot's own start node, and
    the legs that leave it, are exempt: a robot inside a zone may drive out), else an error. Chart and edge zones are not
    handled here (the caller refuses those)."""
    from packages.utils import blocked_nodes
    polys = [(str(r["id"]), r["spec"]["polygon"]) for r in rows if "polygon" in r["spec"]]
    ids = [i for i, _ in polys]
    xy = {str(n.get("node_id", n.get("_key"))): blocked_nodes.node_xy(n) for n in nodes}
    report: Dict[str, Any] = {"applied": ids, "unresolved": [], "detour": False, "forced_by": [], "extra_length_m": None}

    def zones_of(a: str, b: Optional[str] = None) -> List[str]:
        pa = xy.get(a)
        pb = xy.get(b) if b is not None else None
        out = []
        for zid, poly in polys:
            if b is None:
                hit = pa is not None and point_in_polygon(pa[0], pa[1], poly)
            else:
                hit = pa is not None and pb is not None and a != start and leg_crosses_polygon(pa, pb, poly)
            if hit:
                out.append(zid)
        return out

    def path_hits(p: Sequence[str]) -> List[str]:
        hit: List[str] = []
        for i, n in enumerate(p):
            if n != start:
                hit += zones_of(n)
            if i + 1 < len(p):
                hit += zones_of(p[i], p[i + 1])
        return list(dict.fromkeys(hit))

    forced = path_hits(path)
    if not forced:
        return LegacyOutcome(list(path), None, report)
    excluded = {n for n in xy if n != start and xy[n] is not None and zones_of(n)}
    crosses = lambda a, b: bool(zones_of(a, b))      # noqa: E731
    detour = blocked_nodes.shortest_path_avoiding(edges, start, end, excluded, crosses) if edges else None
    if detour is None or path_hits(detour):
        report["forced_by"] = forced
        return LegacyOutcome(list(path), "Every route to the goal goes through a keep-out zone (manually switched off "
                             "regions of the map): " + ", ".join(forced) + ". Choose another goal or turn the zone off.",
                             report)
    report.update(detour=True, forced_by=forced)
    return LegacyOutcome([str(n) for n in detour], None, report)
