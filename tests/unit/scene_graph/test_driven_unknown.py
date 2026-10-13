"""A pose is a measured position: the body stood there, so the cells the evidence proves free around it are free even where the
window is unknown (a camera blind spot under the robot). Along the chord between two poses only known-free cells count, and a
REPORTED blocked cell is never made free."""
import math

import numpy as np
import pytest

from packages.scene_graph.api import SceneParams
from packages.scene_graph.engine import ChartGraph, LAT, RES

from . import chart_worlds as cw

pytestmark = pytest.mark.unit
R = cw.R


def _blind(world, way, radius=0.05, step=0.5):
    """the world with a camera blind spot: unknown within `radius` of every drive point (the cell under the robot, which a
    forward camera never sees). The evidence proves free only what lies within r_d - r + h = 0.1 m of a pose: a wider blind spot
    leaves unknown chord cells, which are not proven and keep the pieces apart (stated, not hidden)."""
    w = world.copy()
    pts = []
    for (x0, y0), (x1, y1) in zip(way[:-1], way[1:]):
        n = max(1, int(math.hypot(x1 - x0, y1 - y0) / step))
        pts += [(x0 + (x1 - x0) * t, y0 + (y1 - y0) * t) for t in np.linspace(0, 1, n, endpoint=False)]
    pts.append(way[-1])
    yy, xx = np.mgrid[0:w.shape[0], 0:w.shape[1]]
    for (x, y) in pts:
        m = (xx * R + R / 2 - x) ** 2 + (yy * R + R / 2 - y) ** 2 <= radius ** 2
        w[m & (w != 100)] = 255
    return w, pts


def _pose_in_F(g, x, y):
    F, _, _ = g._build_F((0, 0))
    gy, gx = (int(math.floor(y / RES)) - g._cy0) // LAT, (int(math.floor(x / RES)) - g._cx0) // LAT
    return bool(F[gy, gx])


def _graph(world, way, batch=None):
    leaves = cw.drive(world, way)
    g = ChartGraph(SceneParams())
    if batch is None: g.fold_many(leaves)
    else:
        for i in range(0, len(leaves), batch): g.fold_many(leaves[i:i + batch])
    return g, leaves


def test_every_pose_is_in_F_despite_the_blind_spot_and_F_never_exceeds_the_reference():
    w, _ = cw.corridor(0.8)
    way = [(1.0, 1.5), (8.0, 1.5), (1.0, 1.5)]                    # there and back inside the corridor
    wb, pts = _blind(w, way)
    g, leaves = _graph(wb, way)
    assert all(_pose_in_F(g, l.x, l.y) for l in leaves)           # a measured position is free space
    ref, _ = _graph(w, way)
    F, _, _ = g._build_F((0, 0))
    Fr, _, _ = ref._build_F((0, 0))
    assert not (F & ~Fr).any()                                    # unknown cells never add free space beyond what is seen


def cw_all():
    from packages.scene_graph.api import BBox
    return BBox(-50, -50, 100, 100)


def test_a_reported_blocked_cell_under_the_blind_spot_stays_blocked():
    w, _ = cw.corridor(0.8)
    way = [(1.0, 1.5), (8.0, 1.5), (1.0, 1.5)]
    wb, _ = _blind(w, way)
    x, y = way[0][0] + 0.5, way[0][1]                              # a pose-neighbour cell
    gy, gx = int(round(y / R)), int(round(x / R))
    wb[gy, gx] = 100                                               # reported blocked inside the blind spot
    g, leaves = _graph(wb, way)
    F, _, _ = g._build_F((0, 0))
    ly, lx = (gy - g._cy0) // LAT, (gx - g._cx0) // LAT
    assert not F[ly, lx]


def test_incremental_evidence_equals_the_full_rebuild_with_the_pose_image():
    w, way = cw.two_rooms()
    wb, _ = _blind(w, way)
    inc, _ = _graph(wb, way, batch=6)
    full, _ = _graph(wb, way)
    Fi, Ci, Si = inc._build_F((0, 0)); Ff, Cf, Sf = full._build_F((0, 0))
    ox = (inc._cx0 - full._cx0) // LAT; oy = (inc._cy0 - full._cy0) // LAT       # canvases may differ in origin: compare absolutely
    assert Fi.sum() == Ff.sum() and Si.sum() == Sf.sum()
    ys, xs = np.nonzero(Fi)
    assert Ff[ys + oy, xs + ox].all()
    # and a forced full rebuild of the incremental engine gives the same images
    inc._pl = None
    Fi2, _, Si2 = inc._build_F((0, 0))
    assert (Fi2 == Fi).all() and (Si2 == Si).all()
