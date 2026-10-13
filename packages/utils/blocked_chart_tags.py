"""Read-time scene-graph chart keys for blocked-node rows (the API's `?with_chart_keys=true`).

Only the scene-graph read side's public surface is used (`view(raster=True)` and `raster()`
of one padded bbox around the rows, which the reads cache): the owner plane (u16 little-endian,
0 = no chart, i = keys[i - 1]) is sampled as the `locate` of `blocked_geometry.row_chart_keys`.
A cell outside every chart is `None`, so unlike the engine's `locate` there is no snapping to
the nearest navigable cell. Everything here is best effort: any problem (flag off, no graph,
bbox over the read limits, timeout, store error) leaves the rows untouched.
"""
import asyncio
import logging
import math
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np

from packages.utils import blocked_geometry

PAD_M = 1.0
TIMEOUT_S = 3.0
MAX_PLANES = 6                # owner planes read per request (each is up to the reads' cell cap); the rest is truncated
MAX_LEG_NODES = 400           # distinct topomap nodes looked up per request (one query each)

Pt = Tuple[float, float]
Locate = Callable[[float, float], Optional[str]]
# status of a tagging request (the `chart_keys_status` field of the endpoints)
OK, PARTIAL, UNAVAILABLE = "ok", "partial", "unavailable"


class TagResult:
    """What a tagging call did: `status` ok (everything tagged or nothing to tag), partial (some items left untagged
    because of the read limits: `truncated` of them), unavailable (no graph, a store error or the timeout: nothing
    tagged). `bool(result)` is "anything was tagged"."""

    def __init__(self, status: str = OK, tagged: int = 0, truncated: int = 0) -> None:
        self.status, self.tagged, self.truncated = status, tagged, truncated

    def __bool__(self) -> bool:
        return self.tagged > 0

    def fields(self) -> Dict[str, Any]:
        return {"chart_keys_status": self.status, "chart_keys_truncated": self.truncated > 0,
                "chart_keys_truncated_count": self.truncated}

    def __repr__(self) -> str:
        return f"TagResult({self.status}, tagged={self.tagged}, truncated={self.truncated})"


def _points(rows: Sequence[Dict[str, Any]]) -> List[Tuple[float, float]]:
    pts: List[Tuple[float, float]] = []
    for row in rows:
        pts.extend(_row_points(row))
    return pts


def _row_points(row: Dict[str, Any]) -> List[Pt]:
    seg = blocked_geometry.row_segment(row)
    if seg is not None:
        return list(seg)
    wp = blocked_geometry._waypoint(row)  # pylint: disable=protected-access
    return [wp] if wp is not None else []


def plane_locator(origin: Tuple[float, float], step: float, shape: Tuple[int, int],
                  index: Any, keys: Sequence[str]) -> Locate:
    """`locate(x, y)` over an owner plane (row-major, row 0 at the origin's y)."""
    rows, cols = shape

    def locate(x: float, y: float) -> Optional[str]:
        c, r = int(math.floor((x - origin[0]) / step)), int(math.floor((y - origin[1]) / step))
        if not (0 <= r < rows and 0 <= c < cols):
            return None
        i = int(index[r, c])
        return keys[i - 1] if 0 < i <= len(keys) else None
    return locate


def _box(pts: Sequence[Pt]) -> Tuple[float, float, float, float]:
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return min(xs) - PAD_M, min(ys) - PAD_M, max(xs) + PAD_M, max(ys) + PAD_M


def _union(boxes: Sequence[Tuple[float, float, float, float]]) -> Tuple[float, float, float, float]:
    return (min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes))


async def _plane(reads: Any, map_id: str, box: Tuple[float, float, float, float]) -> Optional[Locate]:
    """The locator over one bbox: one `reads.owner_plane` call (one meta read, no hierarchy). None: no graph.
    Raises AreaTooLarge (422) when the box is over the reads' cell cap, other HTTPExceptions as they are."""
    from packages.scene_graph.api import BBox
    plane = await reads.owner_plane(map_id, BBox(*box), 0)
    if plane is None:
        return None
    rows, cols = plane["shape"]
    index = np.asarray(plane["index"]).reshape(rows, cols)
    return plane_locator(plane["origin"], plane["step"], (rows, cols), index, plane["keys"])


def _is_area_cap(exc: BaseException) -> bool:
    return type(exc).__name__ == "AreaTooLarge"


async def _tag_items(reads: Any, map_id: str, items: List[Tuple[Any, Tuple[float, float, float, float]]],
                     apply: Callable[[Any, Locate], Any], staged: List[Tuple[Any, Any]]) -> TagResult:
    """Tag `items` ((item, padded bbox) pairs): one plane for all when they fit the read limits, else the
    items are split by their centres (longer axis, median) into up to MAX_PLANES groups; what still does not
    fit is left untagged and counted as truncated. `apply(item, locate)` returns the item's keys; (item, keys) pairs are appended to `staged`, so a caller
    that fails midway has touched nothing."""
    result = TagResult()
    pending: List[List[Tuple[Any, Tuple[float, float, float, float]]]] = [items]
    planes = 0
    gone = False
    while pending:
        group = pending.pop(0)
        if not group:
            continue
        if planes >= MAX_PLANES:
            result.truncated += len(group)
            continue
        box = _union([b for _, b in group])
        try:
            planes += 1
            locate = await _plane(reads, map_id, box)
        except Exception as exc:  # pylint: disable=broad-except
            if not _is_area_cap(exc):
                raise
            if len(group) == 1:
                result.truncated += 1
                continue
            wide_x = (box[2] - box[0]) >= (box[3] - box[1])
            group.sort(key=lambda it: (it[1][0] + it[1][2]) if wide_x else (it[1][1] + it[1][3]))
            half = len(group) // 2
            pending[:0] = [group[:half], group[half:]]
            planes -= 1                  # a refused request read nothing
            continue
        if locate is None:
            gone = True
            break
        for item, _ in group:
            staged.append((item, apply(item, locate)))
            result.tagged += 1
    if gone:
        return TagResult(UNAVAILABLE)
    if result.truncated:
        result.status = PARTIAL
    return result


async def add_chart_keys(rows: List[Dict[str, Any]], map_id: str, get_reads: Callable[[], Any],
                         timeout_s: float = TIMEOUT_S) -> TagResult:
    """Set `chart_keys` on each row (in place) from the charts its obstacle crosses. The read area is not
    capped at a fixed size: rows are grouped into as few owner planes as the reads' limits allow; rows that
    cannot be (too far apart, or one bigger than a plane) stay untagged and are counted in `truncated`
    (status partial). Never raises: any failure is status unavailable with no row touched."""
    try:
        items = []
        for row in rows:
            pts = _row_points(row)
            if pts:
                items.append((row, _box(pts)))
        if not items:
            return TagResult()

        staged: List[Tuple[Any, Any]] = []
        result = await asyncio.wait_for(
            _tag_items(get_reads(), map_id, items, lambda row, locate: blocked_geometry.row_chart_keys(row, locate),
                       staged), timeout_s)
        if result.status != UNAVAILABLE:
            for row, keys in staged:
                row["chart_keys"] = keys
        return result
    except Exception as err:  # pylint: disable=broad-except
        logging.getLogger(__name__).info("No chart keys for blocked nodes of map %s: %s",
                                         map_id, err or type(err).__name__)
        return TagResult(UNAVAILABLE)


async def add_leg_chart_keys(legs: List[Dict[str, Any]],
                             node_xy: Callable[[str, str], Optional[Tuple[float, float]]],
                             get_reads: Callable[[], Any], timeout_s: float = TIMEOUT_S) -> TagResult:
    """Set `chart_keys` on each run leg (in place) from the scene-graph charts its straight
    topomap-node-to-node line crosses (`blocked_geometry.leg_chart_keys`). A leg whose nodes have no
    position gets `chart_keys: None`; a map without a readable graph is left untouched. At most
    MAX_LEG_NODES distinct nodes are looked up per request (one query each): the legs past that are left
    untagged and counted in `truncated` (status partial); so are legs that do not fit the read limits (see
    `add_chart_keys`). `node_xy(map_id, node_id)` is blocking (run in a thread). Never raises."""
    try:
        by_map: Dict[str, List[Dict[str, Any]]] = {}
        nodes: set = set()
        truncated = 0
        for leg in legs:
            if leg.get("map_id") and leg.get("from_topomap_node") and leg.get("to_topomap_node"):
                m = str(leg["map_id"])
                pair = {(m, str(leg["from_topomap_node"])), (m, str(leg["to_topomap_node"]))}
                if len(nodes | pair) > MAX_LEG_NODES:
                    truncated += 1
                    continue
                nodes |= pair
                by_map.setdefault(m, []).append(leg)
        if not by_map:
            return TagResult(PARTIAL if truncated else OK, 0, truncated)
        loop = asyncio.get_running_loop()
        total = TagResult(OK, 0, truncated)
        staged: List[Tuple[Dict[str, Any], Any]] = []

        async def tag_map(map_id: str, group: List[Dict[str, Any]]) -> TagResult:
            xy: Dict[str, Optional[Tuple[float, float]]] = {}
            for node in {str(leg[k]) for leg in group for k in ("from_topomap_node", "to_topomap_node")}:
                xy[node] = await loop.run_in_executor(None, node_xy, map_id, node)
            items, unknown = [], []
            for leg in group:
                a, b = xy.get(str(leg["from_topomap_node"])), xy.get(str(leg["to_topomap_node"]))
                if a is None or b is None:
                    unknown.append((leg, None))
                else:
                    items.append((leg, _box([a, b])))

            def apply(leg: Dict[str, Any], locate: Locate) -> Any:
                a, b = xy[str(leg["from_topomap_node"])], xy[str(leg["to_topomap_node"])]
                return blocked_geometry.leg_chart_keys([a, b], locate)[0]
            r = await _tag_items(get_reads(), map_id, items, apply, staged) if items else TagResult()
            if r.status != UNAVAILABLE:
                staged.extend(unknown)       # a leg without a position is None only where the graph is readable
            return r

        async def run() -> None:
            statuses = []
            for m, g in by_map.items():
                r = await tag_map(m, g)
                total.tagged += r.tagged
                total.truncated += r.truncated
                statuses.append(r.status)
            if statuses and all(st == UNAVAILABLE for st in statuses):
                total.status = UNAVAILABLE
            elif total.truncated or UNAVAILABLE in statuses or PARTIAL in statuses:
                total.status = PARTIAL
        await asyncio.wait_for(run(), timeout_s)
        for leg, keys in staged:
            leg["chart_keys"] = keys
        return total
    except Exception as err:  # pylint: disable=broad-except
        logging.getLogger(__name__).info("No chart keys for run legs: %s", err or type(err).__name__)
        return TagResult(UNAVAILABLE)
