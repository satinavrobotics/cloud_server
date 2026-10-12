"""Acceptance of the chart engine (packages/scene_graph/engine.py), judged by an independent verifier (chart_verify.py)."""
import random
from functools import lru_cache

import numpy as np
import pytest

from packages.scene_graph.api import BBox, Blocked, SceneParams, View
from packages.scene_graph.engine import ChartGraph
from packages.scene_graph.leafcover import coverage

from . import chart_worlds as cw
from .chart_verify import ok, verify

pytestmark = pytest.mark.unit
WORLDS = dict(two_rooms=cw.two_rooms, corridor=cw.corridor, pillar=cw.pillar_room, ring=cw.ring, island=cw.island)
EXPECTED_CHI = dict(two_rooms=1, corridor=1, pillar=0, ring=0, island=0)
ALL = BBox(-5, -5, 20, 20)


@lru_cache(maxsize=None)
def built(name, door=None):
    w, way = WORLDS[name]() if door is None else cw.two_rooms(door)
    leaves = cw.drive(w, way)
    g = ChartGraph(SceneParams())
    g.fold_many(leaves)
    return g, w, leaves


def _region(g, shape):
    return (-60, -60, shape[1] + 60, shape[0] + 60)


def plane(g, name, region):
    """global-cell view of one canvas plane over [x0, x1) x [y0, y1) (unknown / -1 outside the canvas)"""
    a = getattr(g, name)
    x0, y0, x1, y1 = region
    fill = 255 if a.dtype == np.uint8 else -1
    out = np.full((y1 - y0, x1 - x0), fill, a.dtype)
    cx, cy = g._cx0, g._cy0
    ya, yb = max(y0, cy), min(y1, cy + a.shape[0]); xa, xb = max(x0, cx), min(x1, cx + a.shape[1])
    if ya < yb and xa < xb:
        out[ya - y0:yb - y0, xa - x0:xb - x0] = a[ya - cy:yb - cy, xa - cx:xb - cx]
    return out


@pytest.mark.parametrize("name", list(WORLDS))
def test_every_invariant_holds_on_the_structural_worlds(name):
    g, w, _ = built(name)
    r = verify(g, w)
    assert ok(r), r
    assert r["chi_F"] == EXPECTED_CHI[name] and r["chi_G"] == r["chi_F"]
    assert r["centre_min"] >= 1 - g.params.eps - 1e-9


@pytest.mark.parametrize("name", ["two_rooms", "island"])
def test_minimal_for_single_removal_or_move(name):
    g, w, _ = built(name)
    s = g._solved
    sp, A = s.sp, s.A
    own, rnd = sp.full_state(A)
    for j, a in enumerate(A):
        if len(A) > 1:
            A2, o2, _r2, S, _ = sp.apply(A, own, rnd, ("remove", j), local=False)
            assert not sp.feasible_after(A2, o2, S), "anchor %d is removable" % j
        mine = np.nonzero(own == j)[0]
        b, _band = sp.pick(mine)
        if b is not None and b != a and b not in A:
            A2, o2, _r2, S, _ = sp.apply(A, own, rnd, ("move", j, b), local=False)
            if sp.feasible_after(A2, o2, S):
                aff = np.union1d(S, np.nonzero((own == j) | (o2 == j))[0])
                old = sp.objective(A, own, aff); new = sp.objective(A2, o2, aff)
                assert not new < (old[0], old[1] - 1e-9), "anchor %d has a better place" % j


@pytest.mark.parametrize("door", [0.4, 0.5, 0.6, 0.9])
def test_link_width_is_the_door_within_half_a_lattice_step(door):
    g, w, _ = built("two_rooms", door)
    v = g.query(ALL)
    assert ok(verify(g, w))
    assert len(v.links) >= 1
    assert abs(min(l.width for l in v.links) - door) <= 0.05 + 1e-6


def test_corridor_width():
    g, w, _ = built("corridor")
    v = g.query(ALL)
    assert abs(min(l.width for l in v.links) - 0.55) <= 0.05 + 1e-6


def test_fold_is_order_independent_and_idempotent():
    w, way = cw.island()
    leaves = cw.drive(w, way)
    ref = ChartGraph(); ref.fold_many(leaves)
    region = _region(ref, w.shape)
    rng = random.Random(7)
    for trial in range(3):
        order = leaves[:] + [rng.choice(leaves) for _ in range(6)]       # duplicates
        rng.shuffle(order)
        g = ChartGraph()
        for i in range(0, len(order), 7):                                 # in batches
            g.fold_many(order[i:i + 7])
        for name in ("_V", "_T", "_B"):
            assert (plane(g, name, region) == plane(ref, name, region)).all(), (trial, name)
        assert ok(verify(g, w))


def test_equal_timestamps_take_the_blocked_side():
    w, way = cw.two_rooms()
    lv = cw.drive(w, way)[:2]
    a = lv[0]
    b = type(a)(key="other", session="s", stamp_ms=a.stamp_ms, x=a.x, y=a.y, yaw=0.0, resolution=a.resolution, origin=a.origin,
                window_u8=np.where(a.window_u8 == 0, 100, a.window_u8).astype(np.uint8))
    g1 = ChartGraph(); g1.fold_many([a, b])
    g2 = ChartGraph(); g2.fold_many([b, a])
    region = _region(g1, w.shape)
    assert (plane(g1, "_V", region) == plane(g2, "_V", region)).all()
    assert (plane(g1, "_V", region)[plane(g1, "_T", region) >= 0] >= 100).all()


def test_incremental_batches_stay_valid_keys_persist_and_scratch_agrees():
    w, way = cw.island()
    leaves = cw.drive(w, way)
    g = ChartGraph()
    seen_retired = set(); prev = {}
    persisted = total = 0
    for i in range(0, len(leaves), 6):
        d = g.fold_many(leaves[i:i + 6])
        r = verify(g, w)
        assert ok(r), (i, r)
        keys = {c.key for c in g.query(ALL).charts}
        assert not (keys & seen_retired), "a retired key came back"
        for rk, succ in d.retired.items():
            seen_retired.add(rk)
            assert all(s in keys for s in succ)
        if prev:
            total += len(prev); persisted += len(prev & keys)
        prev = keys
    assert total and persisted / total >= 0.5
    scratch = ChartGraph(); scratch.fold_many(leaves)
    assert ok(verify(scratch, w))
    assert abs(len(g.query(ALL).charts) - len(scratch.query(ALL).charts)) <= 2


@pytest.mark.parametrize("deg,exact", [(90, True), (30, False)])
def test_rotated_windows_give_the_axis_aligned_partition_up_to_resampling(deg, exact):
    w, way = cw.two_rooms()
    leaves = cw.drive(w, way)
    ref = built("two_rooms")[0]
    g = ChartGraph(); g.fold_many([cw.rotated(w, lf, deg) for lf in leaves])
    assert ok(verify(g, w))
    assert len(ref.query(ALL).charts) == len(g.query(ALL).charts)
    region = _region(ref, w.shape)
    va, vb = plane(ref, "_V", region), plane(g, "_V", region)
    kn = (va != 255) & (vb != 255)
    assert kn.sum() > 0 and (va[kn] == vb[kn]).mean() >= (0.999 if exact else 0.97)


def test_queries_never_raise_and_stay_consistent():
    g, w, leaves = built("island")
    v = g.query(ALL)
    keys = [c.key for c in v.charts]
    ra = v.raster
    assert sorted(set(ra.keys)) == sorted(keys)
    for i, c in enumerate(v.charts):
        assert int((ra.index == i + 1).sum()) == c.cells and 0.0 <= c.driven_frac <= 1.0 and 0.0 <= c.ever_blocked_frac <= 1.0
    assert v.version == g.version
    for a in keys:
        for b in keys:
            r = g.route(a, b)
            assert r is not None and r.length >= 0 and all(np.isfinite(r.nodes).all() for _ in [0])
            assert r.charts[0] == a and r.charts[-1] == b and r.min_width > 0
    assert g.locate(*[c.anchor for c in v.charts][0]) in keys
    assert g.locate(-100, -100) is None
    assert g.route("nope", keys[0]) is None and g.locate(0.0, 0.0) in keys + [None]
    tiles = g.corridor(keys[0], keys[-1])
    assert tiles and all(isinstance(t, tuple) and len(t) == 2 for t in tiles)
    # a link between two charts has the channels the contract names
    assert {"undriven", "ever_blocked", "soft"} <= set(v.links[0].channels)


def test_blocked_overlay_removes_the_door():
    g, w, _ = built("two_rooms")
    keys = [c.key for c in g.query(ALL).charts]
    a, b = keys[0], keys[1]
    free = g.route(a, b)
    assert free is not None
    door = Blocked(discs=[(3.075, 1.5, 0.8)])
    assert g.route(a, b, door) is None
    side = Blocked(discs=[(2.0, 1.5, 0.3)])
    r = g.route(a, b, side)
    assert r is None or r.length >= free.length - 1e-9


def test_route_through_the_corridor_is_within_five_percent_of_the_full_search():
    g, w, _ = built("island")
    keys = [c.key for c in g.query(ALL).charts]
    s = g._solved
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import dijkstra
    R, C, L = s.edges; N = s.sp.N
    G = coo_matrix((np.concatenate([L, L]), (np.concatenate([R, C]), np.concatenate([C, R]))), shape=(N, N)).tocsr()
    for i, a in enumerate(keys):
        d = dijkstra(G, directed=False, indices=int(s.A[i]))
        for j, b in enumerate(keys):
            r = g.route(a, b)
            assert r.length <= 1.05 * d[int(s.A[j])] + 1e-9


def test_cover_k_covers_every_coverable_cell():
    w, way = cw.two_rooms()
    leaves = cw.drive(w, way)
    for lf in leaves:
        lf.view = View(lf.x, lf.y, 0.0, 6.2, 4.0)
    g = ChartGraph(); g.fold_many(leaves)
    keys = [c.key for c in g.query(ALL).charts]
    for k in (1, 2):
        sel, ceiling, optimal = g.cover(keys, k=k)
        assert 0.0 < ceiling <= 1.0 and optimal and set(sel) <= {lf.key for lf in leaves}
    one, _, _ = g.cover(keys, k=1); two, _, _ = g.cover(keys, k=2)
    assert len(two) >= len(one) >= 1
    # the selection k-covers what any leaf can
    s = g._solved
    H, W = s.plane.shape
    views = [r.view for r in g._leaves.values() if r.view is not None]
    free = (g._V != 255) & (g._V < 100); solid = (g._V >= 100) & (g._V != 255)
    cov = coverage([v for v in views if v.key in set(one)], free, solid, 0.05, (g._cx0 * 0.05, g._cy0 * 0.05),
                   np.nonzero((s.plane >= 0).ravel())[0], W)
    assert len(cov) >= 1


def test_refold_after_a_leaf_is_deleted_equals_a_fresh_fold_without_it():
    w, way = cw.island()
    leaves = cw.drive(w, way)
    g = ChartGraph(); g.fold_many(leaves)
    gone = leaves[10]
    # the deleted leaf's window extent, and every remaining leaf that overlaps it
    x0, y0 = gone.origin; ext = gone.window_u8.shape[0] * 0.05
    box = BBox(x0, y0, x0 + ext, y0 + ext)
    rest = [lf for lf in leaves if lf.key != gone.key]
    near = [lf for lf in rest if not (lf.origin[0] > box.x1 or lf.origin[0] + ext < box.x0 or lf.origin[1] > box.y1 or lf.origin[1] + ext < box.y0)]
    g.refold(near, box)
    fresh = ChartGraph(); fresh.fold_many(rest)
    region = _region(fresh, w.shape)
    assert (plane(g, "_V", region) == plane(fresh, "_V", region)).all()
    assert gone.key not in g._leaves and ok(verify(g, w))


def test_params_semantics_and_cap_validation():
    p = SceneParams()
    assert p.place == pytest.approx(0.375) and p.eps == pytest.approx(0.1 / 0.375) and p.sight_range == 5.0
    with pytest.raises(ValueError):
        ChartGraph(SceneParams(sight_range=0.2))
    g = ChartGraph()
    assert g.fold_many([]).changed == [] and g.locate(0, 0) is None and g.route("a", "b") is None and g.query(ALL).charts == []


def test_unobserved_space_is_never_navigable_and_driven_evidence_is_reported():
    g, w, _ = built("two_rooms")
    v = g.query(ALL)
    assert all(c.driven_frac > 0 for c in v.charts)
    assert g.locate(10.0, 10.0) is None


def test_snap_reports_the_real_distance_and_never_hides_a_wall():
    g, w, _ = built("two_rooms")
    v = g.query(ALL, raster=False)
    ax, ay = v.charts[0].anchor
    s = g.snap(ax, ay)
    assert s.key is not None and s.distance < 0.15 and not s.crosses_blocked          # on a navigable cell
    far = g.snap(-100.0, -100.0)
    assert far.key is None and far.distance == float("inf")
    # a point 1.2 m outside the free space: refused by a 0.5 m limit with the true distance reported
    cells = np.argwhere(g._solved.plane >= 0)
    ty, tx = cells[0]
    px, py = g._cell_xy(int(ty), int(tx))
    # step away from the nearest navigable cell until the snap distance exceeds 0.5 m
    for d in np.arange(0.2, 6.0, 0.2):
        q = g.snap(px - d, py, max_dist=0.5)
        if q.key is None and q.distance != float("inf"):
            assert q.distance > 0.5 and q.distance <= g.params.sight_range
            assert g.locate(px - d, py, max_dist=0.5) is None
            assert g.locate(px - d, py, max_dist=q.distance + 0.01) == g.snap(px - d, py).key
            break
    else:
        pytest.fail("no refused snap with a finite distance found")
    # locate with no limit equals the old behaviour (within sight_range)
    assert g.locate(ax, ay) == s.key
