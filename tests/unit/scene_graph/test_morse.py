"""The persistence partition (packages/scene_graph/morse.py): pinned against a compact reference, translation and batch
determinism, scale invariance across the lattice step, dynamic level depth."""
import inspect
import math

import numpy as np
import pytest
from scipy import ndimage

from packages.scene_graph import morse
from packages.scene_graph.api import SceneParams
from packages.scene_graph.engine import ChartGraph

from . import chart_worlds as cw
from .chart_verify import ok, verify

pytestmark = pytest.mark.unit
H = 0.1


def _random_F(seed, shape=(46, 64)):
    rng = np.random.default_rng(seed)
    F = ndimage.gaussian_filter(rng.random(shape), 2.0) > 0.5
    return ndimage.binary_opening(F, iterations=1) | (rng.random(shape) > 0.995)


def _reference_labels(F, h, tau):
    """merge by definition, from scratch after every merge: smallest persistence first (ties: larger saddle, smaller peak ranks,
    smaller ids), the elder (higher peak, smaller rank) survives. No union-find, no folded saddle maps."""
    sp = morse.Space.of(F)
    f = morse.distance(sp, h)
    rank = morse.ranks(f)
    lab, _ = morse.raw_basins(sp, rank)
    while True:
        i, j, v = morse.crossings(sp, lab, f)
        best = None
        pairs = {}
        for a, b, val in zip(lab[i].tolist(), lab[j].tolist(), v.tolist()):
            k = (min(a, b), max(a, b))
            pairs[k] = max(pairs.get(k, -1.0), val)
        peak = {}
        for c in range(sp.N):
            r = int(lab[c])
            if r not in peak or (f[c], -rank[c]) > (peak[r][0], -peak[r][1]):
                peak[r] = (float(f[c]), int(rank[c]))
        for (a, b), s in pairs.items():
            p = min(peak[a][0], peak[b][0]) - s
            if p < tau - morse.TOL:
                ka, kb = sorted((a, b), key=lambda x: peak[x][1])
                key = (p, -s, peak[ka][1], peak[kb][1], a, b)
                if best is None or key < best[0]:
                    best = (key, a, b)
        if best is None:
            break
        _, a, b = best
        keep, drop = (a, b) if (peak[a][0], -peak[a][1]) >= (peak[b][0], -peak[b][1]) else (b, a)
        lab = np.where(lab == drop, keep, lab)
    img = -np.ones(F.shape, np.int64)
    ids = {r: n for n, r in enumerate(sorted(set(lab.tolist()), key=lambda r: min(rank[lab == r])))}
    img[sp.ys, sp.xs] = [ids[int(r)] for r in lab]
    return img


@pytest.mark.parametrize("seed", range(6))
@pytest.mark.parametrize("mult", [1, 2, 4])
def test_the_union_find_merge_equals_the_compact_reference(seed, mult):
    F = _random_F(seed)
    tau = morse.tau0(H) * mult
    P = morse.partition(F, H, tau)
    img = -np.ones(F.shape, np.int64); img[P.sp.ys, P.sp.xs] = P.full
    assert (img == _reference_labels(F, H, tau)).all()


@pytest.mark.parametrize("seed", range(3))
def test_the_merge_sequence_has_non_decreasing_persistence_so_levels_are_prefixes(seed):
    F = _random_F(seed)
    sp = morse.Space.of(F); f = morse.distance(sp, H); rank = morse.ranks(f)
    lab, roots = morse.raw_basins(sp, rank)
    i, j, v = morse.crossings(sp, lab, f)
    lo, hi, s = morse.saddles(lab, i, j, v)
    tree = morse.merge_tree(f[roots], rank[roots], lo, hi, s, math.inf)
    p = [t[0] for t in tree]
    assert p == sorted(p)
    # a coarser tau is the finer one continued: the regions of tau are the prefix of the same sequence
    fine = morse.survivors(len(roots), tree, morse.tau0(H))
    coarse = morse.survivors(len(roots), tree, 4 * morse.tau0(H))
    assert all(coarse[fine == r].tolist() == [coarse[r]] * int((fine == r).sum()) for r in np.unique(fine))


def test_the_partition_is_translation_invariant():
    F = _random_F(1)
    P = morse.partition(F, H, morse.tau0(H))
    a = -np.ones(F.shape, np.int64); a[P.sp.ys, P.sp.xs] = P.full
    G = np.pad(F, ((7, 3), (11, 5)))
    Q = morse.partition(G, H, morse.tau0(H))
    b = -np.ones(G.shape, np.int64); b[Q.sp.ys, Q.sp.xs] = Q.full
    assert (b[7:7 + F.shape[0], 11:11 + F.shape[1]] == a).all()


def test_no_scale_is_hidden_in_the_partition_module():
    src = inspect.getsource(morse)
    assert "0.1" not in src and "0.05" not in src and "5.0" not in src
    assert morse.tau0(0.1) == pytest.approx(2 * math.sqrt(2) * 0.1) and morse.tau0(0.2) == pytest.approx(2 * morse.tau0(0.1))


# ----------------------------------------------------------------------------------------- scale invariance across h
def _disc_world(h, extent=12.0):
    """F of a robot-eroded world rasterised at lattice step h: cell (i, j) is the point ((j + .5) h, (i + .5) h)"""
    n = int(round(extent / h))
    y, x = (np.mgrid[0:n, 0:n] + 0.5) * h
    return x, y, n


def _rooms(h):
    x, y, n = _disc_world(h)
    F = np.zeros((n, n), bool)
    for x0 in (0.5, 4.5, 8.5):
        F |= (x > x0) & (x < x0 + 3.0) & (y > 1.0) & (y < 5.0)
    F |= (x > 3.4) & (x < 4.6) & (y > 2.7) & (y < 3.3)                    # doors, 1.2 m
    F |= (x > 7.4) & (x < 8.6) & (y > 2.7) & (y < 3.3)
    return F


def _field(h):
    x, y, n = _disc_world(h)
    F = (x > 0.5) & (x < 11.5) & (y > 0.5) & (y < 11.5)
    for cx, cy, r in ((3.0, 3.0, 0.9), (8.0, 4.0, 0.7), (5.5, 8.5, 1.1)):
        F &= (x - cx) ** 2 + (y - cy) ** 2 > r * r
    return F


@pytest.mark.parametrize("world", [_rooms, _field])
def test_region_counts_and_link_widths_agree_between_h_and_2h(world):
    """tau scales with h: the same physical persistence is tau_0 * 2^(j+1) at h and tau_0 * 2^j at 2h"""
    fine, coarse = world(0.1), world(0.2)
    for j in range(0, 3):
        k1 = morse.partition(fine, 0.1, morse.tau0(0.1) * 2 ** (j + 1)).K
        k2 = morse.partition(coarse, 0.2, morse.tau0(0.2) * 2 ** j).K
        assert abs(k1 - k2) <= 2, (j, k1, k2)
    P1 = morse.partition(fine, 0.1, morse.tau0(0.2))
    P2 = morse.partition(coarse, 0.2, morse.tau0(0.2))
    A1, A2 = morse.arcs(P1.sp, P1.full, P1.f), morse.arcs(P2.sp, P2.full, P2.f)
    assert abs(len(A1.a) - len(A2.a)) <= 2
    # the widest crossing agrees within two lattice steps of the coarser grid
    w1, w2 = 2 * max(A1.s.tolist(), default=0.0), 2 * max(A2.s.tolist(), default=0.0)
    assert abs(w1 - w2) <= 2 * 0.2 + 1e-9


# ------------------------------------------------------------------------------------------------------ engine level
def _built(world, batch):
    w, way = world()
    leaves = cw.drive(w, way)
    g = ChartGraph(SceneParams())
    for i in range(0, len(leaves), batch):
        g.fold_many(leaves[i:i + batch])
    return g, w, leaves


def _cells(g):
    s = g._solved
    gy0, gx0 = g._cy0 // 2, g._cx0 // 2
    out = {}
    for j in range(len(s.A)):
        m = np.nonzero(s.full == j)[0]
        out[j] = frozenset(zip((s.sp.ys[m] + gy0).tolist(), (s.sp.xs[m] + gx0).tolist()))
    return sorted(out.values(), key=lambda c: min(c))


@pytest.mark.parametrize("name", ["two_rooms", "island", "ring"])
def test_the_partition_is_the_same_for_every_batch_split_and_arrival_order(name):
    world = getattr(cw, name)
    ref, _, leaves = _built(world, 1000)
    base = _cells(ref)
    assert ok(verify(ref))
    rng = np.random.default_rng(3)
    for batch in (1, 5, 17):
        order = [leaves[k] for k in rng.permutation(len(leaves))]
        g = ChartGraph(SceneParams())
        for i in range(0, len(order), batch):
            g.fold_many(order[i:i + batch])
        assert _cells(g) == base, (name, batch)


def test_levels_run_until_one_region_per_component_and_nest():
    g, w, _ = _built(cw.two_rooms, 1000)
    lv = g._levels()[0]
    s = g._solved
    assert len(lv[0]["nodes"]) == len(s.A)
    counts = [len(set(l["parent"])) for l in lv]
    assert counts == sorted(counts, reverse=True) and counts[-1] == 1
    for a, b in zip(lv[:-1], lv[1:]):                                    # nested: a level is a coarsening of the one below
        assert all(b["parent"][p] == b["parent"][a["parent"][p]] for p in range(len(s.A)))
    assert ok(verify(g))
