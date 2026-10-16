"""The place rule: which regions of the navigable set are places (charts and anchors) and which are not.

Decision (exposing-parameters thread, one-tree merge): a region is a place when the robot can be there or has been there.
  * topology gives the regions: the persistence regions of the distance-to-blocked function `f` (a region is the basin of one
    of its peaks; `morse.partition` merges the peaks whose persistence is below tau);
  * fit gives the first way to be a place: the peak of the region reaches the robot's circumscribed radius (`peak_m`), so the
    robot fits at the centre of its widest point;
  * traversal gives the second: the driven footprint inside the region spans at least the robot's length (`path_m`), so the
    robot has already been through it, even where it is narrower than the robot (a shallow corridor the robot drove through).
A region that is neither is not a place: it is removed from the navigable set before the charts are cut. No count threshold is
used. The span is the geodesic (8-connected path) diameter of the driven cells, not their bounding box.

Pure functions, no engine state: the engine calls `place_regions` with its own planes.
"""
import math

import numpy as np
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import dijkstra

TOL = 1e-9
SQRT2 = math.sqrt(2.0)


def robot_place_scales(half_side_m: float):
    """(peak_m, path_m) of a square robot of half side `half_side_m`: the circumscribed radius and the side length."""
    return half_side_m * SQRT2, 2.0 * half_side_m


def geodesic_span(ys: np.ndarray, xs: np.ndarray, h: float) -> float:
    """The longest shortest-path distance (m) between two cells of a set, moving between 8-neighbours (a diagonal step costs
    sqrt(2) h). Only the largest connected piece of the set is measured; 0 for fewer than two cells."""
    n = len(ys)
    if n < 2:
        return 0.0
    index = {(int(y), int(x)): i for i, (y, x) in enumerate(zip(ys, xs))}
    rows, cols, vals = [], [], []
    for (y, x), i in index.items():
        for dy, dx in ((0, 1), (1, 0), (1, 1), (1, -1)):
            j = index.get((y + dy, x + dx))
            if j is not None:
                w = h * (SQRT2 if dy and dx else 1.0)
                rows += [i, j]; cols += [j, i]; vals += [w, w]
    graph = coo_matrix((vals, (rows, cols)), shape=(n, n)).tocsr()
    first = dijkstra(graph, directed=False, indices=0)
    a = int(np.argmax(np.where(np.isfinite(first), first, -1.0)))
    second = dijkstra(graph, directed=False, indices=a)
    return float(second[np.isfinite(second)].max())


def place_regions(f: np.ndarray, region: np.ndarray, driven: np.ndarray, ys: np.ndarray, xs: np.ndarray, h: float,
                  peak_m: float, path_m: float) -> np.ndarray:
    """Per cell of the navigable set (cell i at (ys[i], xs[i]), distance f[i] in m, persistence region region[i], driven[i] True
    where the robot's footprint or its driven path covers the cell): True where the cell's region is a place."""
    keep = np.zeros(len(region), bool)
    for r in range(int(region.max()) + 1):
        cells = np.nonzero(region == r)[0]
        if len(cells) == 0:
            continue
        if f[cells].max() >= peak_m - TOL:
            keep[cells] = True
            continue
        driven_cells = cells[driven[cells]]
        if len(driven_cells) and geodesic_span(ys[driven_cells], xs[driven_cells], h) >= path_m - TOL:
            keep[cells] = True
    return keep
