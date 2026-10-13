"""Fold, evidence and dirty-set exactness: each incremental computation is checked against the plain definition it replaces."""
import numpy as np
import pytest
from scipy import ndimage

from packages.scene_graph.api import BBox, SceneParams
from packages.scene_graph.engine import ChartGraph, _squares

from . import chart_worlds as cw
from .test_growth import DOWN_RIGHT, UP_LEFT

pytestmark = pytest.mark.unit

def _batches(world_fn, way, batch=3, step=0.4, win=40):
    w, _ = world_fn()
    return w, cw.drive(w, way, step=step, win=win), batch

@pytest.mark.parametrize("way", [UP_LEFT, DOWN_RIGHT], ids=["up_left", "down_right"])
def test_local_erosion_is_the_full_erosion_after_every_batch(way):
    w, leaves, batch = _batches(cw.pillar_room, way)
    g = ChartGraph(SceneParams())
    sqs = _squares(g.params.robot_half_side)
    shapes = set()
    for i in range(0, len(leaves), batch):
        g.fold_many(leaves[i:i + batch])
        free = (g._V != 255) & (g._V < 100)
        full = np.zeros_like(free)
        for st in sqs:
            full |= ndimage.binary_erosion(free, structure=st, border_value=0)
        shapes.add(g._V.shape)
        assert (g._ecache[0] == free).all() and (g._ecache[1] == full).all(), i
    assert len(shapes) > 1                                    # the canvas did grow

def _state(g):
    """{chart key: (anchor map position, frozenset of its cells in map coordinates)} from the public view only"""
    v = g.query(BBox(-50, -50, 50, 50), raster=True)
    r = v.raster
    out = {}
    for i, k in enumerate(r.keys, start=1):
        ys, xs = np.nonzero(r.index == i)
        out[k] = frozenset(zip((np.round(r.origin[0] + (xs + 0.5) * r.step, 3)).tolist(), (np.round(r.origin[1] + (ys + 0.5) * r.step, 3)).tolist()))
    pos = {c.key: (round(c.anchor[0], 3), round(c.anchor[1], 3)) for c in v.charts}
    return {k: (pos[k], out[k]) for k in out}

def test_dirty_changed_is_exact_when_the_canvas_grows_to_the_left_and_top():
    w, leaves, batch = _batches(cw.pillar_room, UP_LEFT)
    g = ChartGraph(SceneParams())
    old = {}
    grew = 0
    for i in range(0, len(leaves), batch):
        had, c0 = g._V is not None, (g._cx0, g._cy0)
        d = g.fold_many(leaves[i:i + batch])
        grew += int(had and (g._cx0, g._cy0) != c0)
        new = _state(g)
        truth = {k for k, v in new.items() if old.get(k) != v}
        assert set(d.changed) == truth, (i, sorted(d.changed), sorted(truth))
        old = new
    assert grew > 0


# ---------------------------------------------------------------------------------------------- round 2 (OPT-2)

def _evidence_by_definition(g):
    """G, W, S as the distance transform of every robot's polyline raster, thresholded (the plain definition `_evidence` replaced)"""
    r, h = g.params.robot_half_side, g._h
    shape = (g._V.shape[0] // 2, g._V.shape[1] // 2)
    G = np.zeros(shape, bool); W = np.zeros_like(G); S = np.zeros_like(G)
    for d, raster in g._polylines().items():
        dist = ndimage.distance_transform_edt(~raster) * h
        G |= dist <= h + 1e-9; S |= dist <= d + 1e-9
        if d >= r - 1e-9: W |= dist <= (d - r + h) + 1e-9
    return G, W, S

@pytest.mark.parametrize("way", [UP_LEFT, DOWN_RIGHT], ids=["up_left", "down_right"])
def test_driven_evidence_is_the_plain_definition_after_every_batch(way):
    w, leaves, batch = _batches(cw.pillar_room, way)
    g = ChartGraph(SceneParams())
    grew = 0
    for i in range(0, len(leaves), batch):
        shape0 = None if g._V is None else g._V.shape
        g.fold_many(leaves[i:i + batch])
        grew += int(shape0 is not None and g._V.shape != shape0)
        G, W, S = _evidence_by_definition(g)
        assert (g._pl["G"] == G).all() and (g._pl["W"] == W).all() and (g._pl["S"] == S).all(), i
    assert grew > 0

def test_driven_evidence_survives_out_of_order_arrival_two_sessions_and_a_deleted_leaf():
    w, leaves, _ = _batches(cw.pillar_room, DOWN_RIGHT)
    rng = np.random.default_rng(2)
    order = rng.permutation(len(leaves))
    g = ChartGraph(SceneParams())
    for i in range(0, len(order), 4):
        part = [leaves[k] for k in order[i:i + 4]]
        for lf in part[::2]: lf.session = "other"                       # a second session, interleaved
        g.fold_many(part)
        G, W, S = _evidence_by_definition(g)
        assert (g._pl["G"] == G).all() and (g._pl["W"] == W).all() and (g._pl["S"] == S).all(), i
    keep = leaves[:len(leaves) // 2]                                    # a refold drops the leaves outside: removal is a rebuild
    g.refold(keep, BBox(-100, -100, 100, 100))
    G, W, S = _evidence_by_definition(g)
    assert (g._pl["G"] == G).all() and (g._pl["W"] == W).all() and (g._pl["S"] == S).all()

def test_evidence_is_exact_across_odd_lattice_shifts_in_both_directions():
    """a pose cell is floor((g - cx0) / 2) with cx0 even, so a canvas shift of k lattice cells moves it by exactly k: the
    incremental driven evidence needs no even-shift restriction. Translations force odd shifts; both growth directions."""
    import dataclasses
    odd = checked = 0
    for dx in (0.0, 0.15):
        for rev in (False, True):
            w, way = cw.two_rooms()
            leaves = [dataclasses.replace(l, x=l.x + dx, y=l.y + dx * 1.7, origin=(l.origin[0] + dx, l.origin[1] + dx * 1.7))
                      for l in cw.drive(w, way)]
            if rev:
                leaves = [dataclasses.replace(l, x=-l.x, y=-l.y, origin=(-l.origin[0] - 6.0, -l.origin[1] - 6.0)) for l in leaves]
            g = ChartGraph(SceneParams())
            for i in range(0, len(leaves), 3):
                shape0 = None if g._V is None else g._V.shape
                g.fold_many(leaves[i:i + 3])
                if shape0 is not None and g._V.shape != shape0 and any(v % 2 for v in g._shift): odd += 1
                G, W, S = _evidence_by_definition(g)
                assert (g._pl["G"] == G).all() and (g._pl["W"] == W).all() and (g._pl["S"] == S).all()
                checked += 1
    assert odd > 0 and checked > 15

def test_engine_version_counts_state_changes_not_calls():
    w, way = cw.two_rooms()
    leaves = cw.drive(w, way)
    g = ChartGraph(SceneParams())
    g.fold_many(leaves)
    v = g.version
    d = g.fold_many(leaves[-5:])                       # a replay: nothing changes
    assert d.bbox is None and g.version == v
    g.fold_many([])
    assert g.version == v
    g.fold_many(cw.drive(w, [(1.0, 0.8), (5.0, 0.8)], session="b", t0=10 ** 6))   # a new session: leaf records and evidence
    assert g.version == v + 1
