"""Topology at the cover's scale is EXACT: chi(served region) = V - E + T - Q, with E counting, per chart pair, the connected
components of the INTERFACE CURVE of the two closed charts (unit edges joined at shared end points). A count over the contact
CELL SET merges two stretches that meet only through one cell of a chart poking toward a hole and then misses the loop
(office01, real depth-derived data: chi_F -3 against -2). These tests pin the counting rule on rasters and on the engine's own
stretch function, and show that filling small holes (diameter < s) would have been wrong (office6 sees its small hole)."""
import numpy as np
import pytest
from scipy import ndimage

from packages.scene_graph.engine import ChartGraph

from .chart_verify import C4, C8, _interface_stretches

pytestmark = pytest.mark.unit


def _notch():
    """chart 0 surrounds a one-cell hole except for a cell N of chart 1 poking in from the right (the office01 pattern)"""
    f = np.full((7, 9), 0, int)
    f[1:6, 6:9] = 1
    f[3, 4:6] = 1                      # N = (3, 4) and its continuation to the bulk of chart 1
    f[3, 3] = -1                       # the hole
    f[:, 8] = np.where(f[:, 8] == 1, 1, f[:, 8])
    return f


def _cell_stretches(full):
    total = 0
    H, W = full.shape
    for a in range(int(full.max()) + 1):
        for b in range(a + 1, int(full.max()) + 1):
            m = np.zeros((H, W), bool)
            for y in range(H):
                for x in range(W):
                    for dy, dx in ((0, 1), (1, 0)):
                        if y + dy < H and x + dx < W and {full[y, x], full[y + dy, x + dx]} == {a, b}:
                            m[y, x] = m[y + dy, x + dx] = True
            if m.any(): total += ndimage.label(m, structure=C8)[1]
    return total


def _chi_nerve(full, E):
    H, W = full.shape
    fp = np.pad(full, 1, constant_values=-1)
    T = Q = 0
    for y in range(-1, H):
        for x in range(-1, W):
            ids = {int(i) for i in fp[y + 1:y + 3, x + 1:x + 3].ravel() if i >= 0}
            if len(ids) == 3: T += 1
            elif len(ids) == 4: T += 4; Q += 1
    return len({int(i) for i in full.ravel() if i >= 0}) - E + T - Q


def _chi_space(full):
    served = np.pad(full >= 0, 1)
    return ndimage.label(served, structure=C4)[1] - (ndimage.label(~served, structure=C8)[1] - 1)


def test_a_notch_around_a_hole_is_two_stretches_not_one():
    f = _notch()
    assert _interface_stretches(f) == 2                       # the top and the bottom of the notch: two components of the curve
    assert _cell_stretches(f) == 1                            # the cell set merges them through the notch cell
    assert _chi_space(f) == 0                                 # one component, one hole
    assert _chi_nerve(f, _interface_stretches(f)) == _chi_space(f)
    assert _chi_nerve(f, _cell_stretches(f)) != _chi_space(f)  # the cell-set count misses the loop: this is the defect


def test_the_engines_stretch_labelling_equals_the_interface_curve_count():
    f = _notch()

    class SP:                                                  # the three arrays _stretches reads
        pass
    sp = SP()
    ys, xs = np.nonzero(f >= 0)
    sp.ys, sp.xs = ys, xs
    idx = -np.ones(f.shape, int); idx[ys, xs] = np.arange(len(ys))
    es = []
    for dy, dx in ((0, 1), (1, 0)):
        for y, x in zip(ys, xs):
            y2, x2 = y + dy, x + dx
            if y2 < f.shape[0] and x2 < f.shape[1] and f[y2, x2] >= 0 and f[y2, x2] != f[y, x]:
                u, v = int(idx[y, x]), int(idx[y2, x2])
                if f[y, x] > f[y2, x2]: u, v = v, u
                es.append((u, v))
    _, n = ChartGraph._stretches(sp, es)
    assert n == 2


def test_a_thin_wall_keeps_its_loop_and_no_hole_is_filled():
    """a 3 m wall (13 cells at 0.1 m, longer than s) between two charts that join at both ends: two stretches, chi 0 = 0"""
    f = np.full((7, 17), 0, int)
    f[4:, 2:] = 1
    f[3, 15:] = 1
    f[3, 2:15] = -1                                           # the wall: a hole of 13 cells
    assert _chi_space(f) == 0
    assert _interface_stretches(f) == 2
    assert _chi_nerve(f, _interface_stretches(f)) == _chi_space(f)


def test_filling_small_holes_would_be_wrong():
    """a hole smaller than s that IS surrounded by a cycle of charts is seen by the nerve (office6); filling it changes chi_F
    by one and breaks the equality. Three charts around one 2-cell hole: E = 3 stretches, T = 1: chi = 0 = chi of the space."""
    f = np.full((6, 6), -1, int)
    f[0:3, 0:6] = 0; f[3:6, 0:3] = 1; f[3:6, 3:6] = 2
    f[2:4, 2:4] = np.array([[0, 0], [1, 2]])
    f[3, 2] = -1; f[3, 3] = -1                                # a hole under the junction
    chi = _chi_space(f)
    assert _chi_nerve(f, _interface_stretches(f)) == chi
    filled = f.copy(); filled[3, 2:4] = 0                     # "fill the small hole": chi_F of the filled region differs
    assert _chi_space(filled) != chi
