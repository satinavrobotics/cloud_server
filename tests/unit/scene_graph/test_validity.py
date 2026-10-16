"""Validity plane (the one-tree scene graph decision): a lattice cell is VALID when it was seen free from within the close-evidence
range and is not held blocked, or when the robot's footprint or driven path covered it (driven). Validity is sticky: a far report
changes the latest value V of a cell but never its close-free stamp Fn, so a far lethal cannot erase it. The planner reports how
many metres of a route are unverified (the metres of its segments whose midpoint cell is not valid).

Checked here: sticky validity (a) and the held case (b), a driven cell valid although unobserved (c), the lazy reader's validity equal
to the engine's on every cell of a world after a store round trip (d), and the unverified length of a route inside and outside the
valid cells (e). The world helpers are the ones of test_place_parity.py. Deterministic, in memory (MemoryBackend): no MinIO.
"""
import math

import numpy as np
import pytest

from packages.scene_graph import store as S
from packages.scene_graph.api import BBox, Leaf, SceneParams
from packages.scene_graph.engine import ChartGraph
from packages.services.mission_planner.lazy_plan import open_plan

from . import chart_worlds as cw

pytestmark = pytest.mark.unit

R = 0.05
H_LAT = 0.1                                     # the lattice step (m) of the default parameters
PATCH = (3.0, 2.5)                              # centre of a 0.5 m lethal patch in an 8 x 6 m room
NEAR, FAR = (4.0, 2.5), (6.5, 2.5)              # capture poses: 1.0 m and 3.5 m from the patch (near range 1.5 m)


def _leaf(key, t, pose, hazard, window=None):
    """an 8 x 6 m window at the origin: free room, the patch lethal when `hazard`, unknown when `window` is given"""
    w = np.zeros((120, 160), np.uint8) if window is None else window
    if hazard: w[int(2.25 / R):int(2.75 / R), int(2.75 / R):int(3.25 / R)] = 100
    return Leaf(key=key, session="s", stamp_ms=t, x=pose[0], y=pose[1], yaw=0.0, resolution=R, origin=(0.0, 0.0), window_u8=w)


def _run(events, **kw):
    """events: [(t, pose, hazard)] folded one batch each, in the given arrival order"""
    g = ChartGraph(SceneParams(near_range_m=1.5, **kw))
    for i, (t, pose, hz) in enumerate(events):
        g.fold_many([_leaf("k%d" % i, t, pose, hz)])
    return g


def _patch_cell(g):
    return int(PATCH[1] / R) - g._cy0, int(PATCH[0] / R) - g._cx0            # full-resolution canvas index of the patch centre


def _valid_at(g, p):
    """the engine's validity of the lattice cell holding the point p (m), from the solved state (canvas lattice image)"""
    ix, iy = math.floor(p[0] / R) // 2, math.floor(p[1] / R) // 2                # the engine's cell of a point (costmap, then lattice)
    return bool(g._solved.val[iy - g._cy0 // 2, ix - g._cx0 // 2])


def test_a_far_lethal_does_not_erase_close_validity():
    """(a) seen free from within range, then reported lethal from far: the latest value is lethal, the cell stays valid"""
    g = _run([(1000, NEAR, False), (2000, FAR, True)])
    assert g._V[_patch_cell(g)] == 100 and g._Fn[_patch_cell(g)] == 1000
    assert _valid_at(g, PATCH)


def test_a_close_lethal_newer_than_the_close_free_holds_the_cell():
    """(b) a close lethal newer than the close free makes the cell invalid (held); a close free after it makes it valid again"""
    g = _run([(1000, NEAR, False), (2000, NEAR, True)])
    assert g._V[_patch_cell(g)] == 100 and not _valid_at(g, PATCH)
    assert _valid_at(_run([(1000, NEAR, True), (2000, NEAR, False)]), PATCH)


def test_a_driven_cell_is_valid_although_unobserved():
    """(c) the robot stood at a pose whose window is unknown: the pose's cell is unobserved (V unknown) and driven, so valid"""
    pose = (1.0, 1.0)
    g = _run([])
    g.fold_many([_leaf("u", 1000, pose, False, window=np.full((120, 160), 255, np.uint8))])
    assert g._V[int(pose[1] / R) - g._cy0, int(pose[0] / R) - g._cx0] == 255
    assert _valid_at(g, pose)
    assert not _valid_at(g, (6.0, 5.0))                  # never driven, never seen: unverified


def _world_run(near_min_clearance_m):
    """a room with a lethal patch, driven around its walls and once past the patch (close and far), then its store round trip"""
    w = np.full((int(4.0 / R), int(9.0 / R)), 100, np.uint8)
    cw._rect(w, 0.2, 0.2, 4.4, 3.8, 0)
    cw._rect(w, 2.75, 2.25, 3.25, 2.75, 100)
    leaves = cw.drive(w, [(1.0, 1.0), (3.6, 1.0), (3.6, 3.2), (1.0, 3.2)], session="a")
    g = ChartGraph(SceneParams(near_range_m=1.5, near_min_clearance_m=near_min_clearance_m))
    for lf in leaves:
        g.fold_many([lf])
    be = S.MemoryBackend()
    st = S.SceneGraphStore(be, be)
    st.save("m", g, g.fold_many([]), None, {"n": 1})
    return g, st


def _engine_valid_cells(g):
    """the global lattice cells the engine holds valid"""
    ys, xs = np.nonzero(g._solved.val)
    return {(int(x) + g._cx0 // 2, int(y) + g._cy0 // 2) for y, x in zip(ys, xs)}


def _store_valid_cells(st, plan):
    """the global lattice cells the lazy reader holds valid, over every stored tile"""
    out = set()
    for tx, ty in st.tiles_in("m", BBox(-1e3, -1e3, 1e3, 1e3)):
        for iy in range(ty * 64, ty * 64 + 64):
            for ix in range(tx * 64, tx * 64 + 64):
                if plan.valid_cell(ix, iy):
                    out.add((ix, iy))
    return out


@pytest.mark.parametrize("mc", [None, 0.25], ids=["rule", "rule+away-from-walls"])
def test_lazy_reader_validity_equals_the_engine_on_every_cell(mc):
    """(d) the lazy reader's `val` equals the engine's on every lattice cell of the world (a store round trip, and a load)"""
    g, st = _world_run(mc)
    plan = open_plan(st, "m")
    engine = _engine_valid_cells(g)
    assert engine and len(engine) < g._solved.val.size                        # some valid cells, some not
    assert _store_valid_cells(st, plan) == engine
    loaded = st.load("m")                                                    # import_state verifies the stored plane too
    assert _engine_valid_cells(loaded) == engine
    assert _valid_at(g, (1.05, 1.05)) and not _valid_at(g, (6.0, 5.0))


def test_unverified_length_of_a_route_inside_the_valid_cells_is_zero_and_outside_is_its_length():
    """(e) a route along the driven path is verified (0 m); a route through unobserved, undriven cells is unverified: the length of
    its segments, a diagonal step sqrt(2) lattice steps and an orthogonal one 1 step"""
    g, st = _world_run(None)
    plan = open_plan(st, "m")
    inside = [(ix, 10) for ix in range(10, 31)]                                # y = 1.0 m: the driven path itself
    assert plan.unverified_length(inside) == 0.0
    outside = [(10, 60), (11, 61), (12, 61), (13, 61)]                          # y = 6 m: never seen, never driven
    assert plan.unverified_length(outside) == pytest.approx(H_LAT * (math.sqrt(2) + 2))
    straight = [(ix, 60) for ix in range(10, 21)]
    assert plan.unverified_length(straight) == pytest.approx(10 * H_LAT)
    assert plan.unverified_length([(10, 60)]) == 0.0                           # a single cell has no segment


def test_unverified_length_refuses_a_store_without_the_rule():
    be = S.MemoryBackend()
    st = S.SceneGraphStore(be, be)
    g = ChartGraph(SceneParams())
    g.fold_many([_leaf("a", 1000, NEAR, False)])
    st.save("m", g, g.fold_many([]), None, {"n": 1})
    with pytest.raises(ValueError, match="close-evidence rule"):
        open_plan(st, "m").unverified_length([(10, 25), (11, 25)])
