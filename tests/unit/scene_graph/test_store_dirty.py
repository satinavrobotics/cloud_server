"""The store's incremental dirty set must equal a full hash, at every step, also when a change at the periphery of a large
chart moves owners far from the changed cells (halls with aligned doors, open fields)."""
import numpy as np
import pytest
from scipy.spatial import cKDTree

from packages.scene_graph.api import BBox, SceneParams
from packages.scene_graph.engine import ChartGraph
from packages.scene_graph.store import MemoryBackend, SceneGraphStore

from . import chart_worlds as cw

pytestmark = pytest.mark.unit
BIG = BBox(-1e4, -1e4, 1e4, 1e4)
RES = cw.R


def _hall():
    W, H = 15.0, 3.2
    w = cw._blank(W, H)
    for x in (5.0, 10.0):                                  # partitions with one aligned 1 m gap each
        cw._rect(w, x, 0.2, x + 0.15, H - 0.2, 100); cw._rect(w, x, H / 2 - 0.5, x + 0.15, H / 2 + 0.5, 0)
    way = [(1.0, H / 2), (W - 1.0, H / 2), (W - 1.0, 0.8)]
    change = ((3.2, 0.2, 3.35, 1.3), [(2.0, 0.8), (4.2, 0.8), (4.2, 1.6)])    # a new wall stub at the far side of room 1
    return w, way, change


def _field():
    W, H = 7.0, 7.0
    w = cw._blank(W, H)
    cw._rect(w, 2.0, 2.0, 3.0, 3.0, 100); cw._rect(w, 4.5, 4.0, 5.5, 5.0, 100)
    way = [(1.0, 1.0), (6.0, 1.0), (6.0, 3.2), (1.0, 3.2), (1.0, 5.2), (6.0, 5.2)]
    change = ((6.6, 6.0, 6.8, 6.8), [(5.0, 6.0), (6.0, 6.0), (6.2, 6.4)])     # a corner appears at the periphery
    return w, way, change


def _known(st):
    """global full-res cell -> (value, time) for every known cell"""
    ys, xs = np.nonzero((st.V != 255) | (st.T >= 0))
    return {(int(x) + st.cx0, int(y) + st.cy0): (int(st.V[y, x]), int(st.T[y, x])) for y, x in zip(ys, xs)}


def _owners(view):
    r = view.query(BIG, raster=True).raster
    if r is None: return {}
    ys, xs = np.nonzero(r.index)
    ox, oy = round(r.origin[0] / RES) // 2, round(r.origin[1] / RES) // 2
    return {(ox + int(x), oy + int(y)): r.keys[int(r.index[y, x]) - 1] for y, x in zip(ys, xs)}


@pytest.mark.parametrize("make", [_hall, _field], ids=["hall", "field"])
def test_incremental_dirty_set_equals_full_hash_and_owner_changes_stay_inside_the_pad(make):
    world, way, (rect, cway) = make()
    params = SceneParams(sight_range=2.0)
    pad = 3 * params.sight_range
    leaves = cw.drive(world, way, win=80, step=1.0)
    w2 = world.copy(); cw._rect(w2, *rect, 100)
    later = cw.drive(w2, cway, win=80, step=0.5, session="s2", t0=10_000_000, r_d=0.25)
    b = MemoryBackend(); inc = SceneGraphStore(b, b, verify_every=1, strict=True, clock=lambda: 1.0)
    b2 = MemoryBackend(); full = SceneGraphStore(b2, b2, clock=lambda: 1.0)
    g = ChartGraph(params); v = v2 = None
    prev_known, prev_owner = {}, {}
    worst = 0.0
    batches = [leaves[i:i + 5] for i in range(0, len(leaves), 5)] + [[l] for l in later]
    for batch in batches:
        d = g.fold_many(batch)
        v = inc.save("m", g, d, v, {})            # strict + verify_every=1: raises if the dirty set missed a tile
        v2 = full.save("m", g, d, v2, {}, full=True)
        assert inc.manifest("m", v)["tiles"] == full.manifest("m", v2)["tiles"]
        known = _known(g.export_state()); owner = _owners(inc.view("m"))
        ci = [k for k in set(known) | set(prev_known) if known.get(k) != prev_known.get(k)]
        co = [k for k in set(owner) | set(prev_owner) if owner.get(k) != prev_owner.get(k)]
        if ci and co and prev_known:
            tree = cKDTree(np.array(ci, float) * RES)
            dist, _ = tree.query(np.array(co, float) * 2 * RES + RES)        # lattice cell centre vs full-res cell
            worst = max(worst, float(dist.max()))
        prev_known, prev_owner = known, owner
    assert inc.coverage_mismatches == 0
    assert worst <= pad + 4 * RES, (worst, pad)
    print(f"{make.__name__}: farthest owner change {worst:.2f} m from a changed input cell (sight_range {params.sight_range}, pad {pad})")
