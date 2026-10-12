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
TIMEOUT_S = 2.0
MAX_BBOX_M = 400.0            # a bigger area is not worth a plane; the reads cap cells anyway


def _points(rows: Sequence[Dict[str, Any]]) -> List[Tuple[float, float]]:
    pts: List[Tuple[float, float]] = []
    for row in rows:
        seg = blocked_geometry.row_segment(row)
        if seg is not None:
            pts.extend(seg)
        else:
            wp = blocked_geometry._waypoint(row)  # pylint: disable=protected-access
            if wp is not None:
                pts.append(wp)
    return pts


def plane_locator(origin: Tuple[float, float], step: float, shape: Tuple[int, int],
                  index: Any, keys: Sequence[str]) -> Callable[[float, float], Optional[str]]:
    """`locate(x, y)` over an owner plane (row-major, row 0 at the origin's y)."""
    rows, cols = shape

    def locate(x: float, y: float) -> Optional[str]:
        c, r = int(math.floor((x - origin[0]) / step)), int(math.floor((y - origin[1]) / step))
        if not (0 <= r < rows and 0 <= c < cols):
            return None
        i = int(index[r, c])
        return keys[i - 1] if 0 < i <= len(keys) else None
    return locate


async def _locator(reads: Any, map_id: str, pts: Sequence[Tuple[float, float]]
                   ) -> Optional[Callable[[float, float], Optional[str]]]:
    from packages.scene_graph.api import BBox
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    box = BBox(min(xs) - PAD_M, min(ys) - PAD_M, max(xs) + PAD_M, max(ys) + PAD_M)
    if box.x1 - box.x0 > MAX_BBOX_M or box.y1 - box.y0 > MAX_BBOX_M:
        return None
    view = await reads.view(map_id, box, 0, None, True)
    if not view.get("available") or not view.get("raster"):
        return None
    meta = view["raster"]
    body, headers = await reads.raster(map_id, box, 0, view["version"], "owner")
    if body is None:
        return None
    rows, cols = (int(v) for v in headers["X-Raster-Shape"].split(","))
    index = np.frombuffer(body, dtype="<u2").reshape(rows, cols)
    return plane_locator((meta["origin"][0], meta["origin"][1]), float(meta["step"]),
                         (rows, cols), index, meta["keys"])


async def add_chart_keys(rows: List[Dict[str, Any]], map_id: str, get_reads: Callable[[], Any],
                         timeout_s: float = TIMEOUT_S) -> bool:
    """Set `chart_keys` on each row (in place); True when it did. Never raises."""
    try:
        pts = _points(rows)
        if not pts:
            return False
        locate = await asyncio.wait_for(_locator(get_reads(), map_id, pts), timeout_s)
        if locate is None:
            return False
        for row in rows:
            row["chart_keys"] = blocked_geometry.row_chart_keys(row, locate)
        return True
    except Exception as err:  # pylint: disable=broad-except
        logging.getLogger(__name__).info("No chart keys for blocked nodes of map %s: %s",
                                         map_id, err or type(err).__name__)
        return False
