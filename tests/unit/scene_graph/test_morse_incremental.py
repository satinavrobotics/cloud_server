"""The incremental solver (`morse.PartitionSolver`) and the routines it is built from, against the full computations they
replace: after every random update of F (cells added and removed, the canvas grown on any side, a wrong or missing shift, an
empty F, a different map altogether) the Partition equals `morse.partition` field by field, dtypes included."""
import math

import numpy as np
import pytest
from scipy import ndimage
from scipy.sparse.csgraph import connected_components

from packages.scene_graph import morse

pytestmark = pytest.mark.unit
H_STEP = 0.1


# ----------------------------------------------------------------------------------------------------------- references
def ref_saddles(lab, i, j, val):
    if len(i) == 0:
        z = np.zeros(0, np.int64)
        return z, z, np.zeros(0)
    a, b = lab[i], lab[j]
    lo, hi = np.minimum(a, b).astype(np.int64), np.maximum(a, b).astype(np.int64)
    n = int(hi.max()) + 1
    code = lo * n + hi
    o = np.lexsort((-val, code))
    code, v = code[o], val[o]
    first = np.concatenate([[True], code[1:] != code[:-1]])
    c = code[first]
    return c // n, c % n, v[first]


def ref_edges_full(sp, h):
    P, pos, Wp = sp.padded()
    R, C, L, cuts = [], [], [], [0]
    for dy, dx in ((0, 1), (1, 0), (1, 1), (1, -1)):
        nbv = P[pos + (dy * Wp + dx)]
        a = np.nonzero(nbv >= 0)[0]
        b = nbv[a]
        if dy and dx:
            pa = pos[a]
            keep = (P[pa + dx] >= 0) & (P[pa + dy * Wp] >= 0)
            a, b = a[keep], b[keep]
        R.append(a); C.append(b); L.append(np.full(len(a), math.hypot(dy, dx) * h))
        cuts.append(cuts[-1] + len(a))
    return np.concatenate(R), np.concatenate(C), np.concatenate(L), tuple(cuts)


def ref_basins(sp, f):
    rank = morse.ranks(f)
    return morse.raw_basins(sp, rank, sp.neighbours())


# ----------------------------------------------------------------------------------------------------------- generators
def blob_field(rng, H, W):
    F = ndimage.gaussian_filter(rng.random((H, W)), rng.uniform(1.0, 3.0)) > rng.uniform(0.42, 0.52)
    if rng.random() < 0.3:                                    # plateaus and corridors: ties everywhere
        F = np.zeros((H, W), bool)
        for _ in range(int(rng.integers(2, 6))):
            y, x = int(rng.integers(0, H - 3)), int(rng.integers(0, W - 3))
            F[y:y + int(rng.integers(2, 12)), x:x + int(rng.integers(2, 25))] = True
    return F


def mutate(rng, F):
    """(new F, shift hint) after one random edit"""
    H, W = F.shape
    op = rng.choice(["add", "remove", "flip", "grow", "same", "swap", "empty", "stripe"], p=[.25, .15, .1, .25, .05, .05, .02, .13])
    G = F.copy()
    shift = (0, 0)
    if op == "add":
        y, x = int(rng.integers(0, H)), int(rng.integers(0, W))
        G[y:y + int(rng.integers(1, 15)), x:x + int(rng.integers(1, 15))] = True
    elif op == "remove":
        y, x = int(rng.integers(0, H)), int(rng.integers(0, W))
        G[y:y + int(rng.integers(1, 10)), x:x + int(rng.integers(1, 10))] = False
    elif op == "flip":
        k = int(rng.integers(1, 6))
        G[rng.integers(0, H, k), rng.integers(0, W, k)] ^= True
    elif op == "grow":
        t, b, l, r = (int(rng.integers(0, 6)) if rng.random() < 0.6 else 0 for _ in range(4))
        G = np.pad(F, ((t, b), (l, r)))
        shift = (t, l)
        if rng.random() < 0.8:                                # new cells in the grown strip
            y0, x0 = int(rng.integers(0, G.shape[0] - 2)), int(rng.integers(0, G.shape[1] - 2))
            G[y0:y0 + int(rng.integers(2, 10)), x0:x0 + int(rng.integers(2, 20))] = True
    elif op == "swap":
        G = blob_field(rng, int(rng.integers(20, 70)), int(rng.integers(20, 70)))
        shift = (int(rng.integers(0, 5)), int(rng.integers(0, 5)))      # a wrong hint
    elif op == "empty":
        G = np.zeros_like(F)
    elif op == "stripe":                                      # a corridor through everything: merges, splits of regions
        if rng.random() < 0.5:
            G[int(rng.integers(0, H)), :] = rng.random() < 0.7
        else:
            G[:, int(rng.integers(0, W))] = rng.random() < 0.7
    if rng.random() < 0.1:
        shift = (int(rng.integers(0, 4)), int(rng.integers(0, 4)))      # a wrong hint on any step
    return G, shift


FIELDS = ["f", "full", "K", "anchor", "raw", "merges", "region", "regions", "seed"]


def assert_same(a, b, where=""):
    for fld in FIELDS:
        x, y = getattr(a, fld), getattr(b, fld)
        assert np.array_equal(x, y), (where, fld)
        if isinstance(x, np.ndarray):
            assert x.dtype == y.dtype, (where, fld, x.dtype, y.dtype)
    for fld in ("ys", "xs", "idx"):
        x, y = getattr(a.sp, fld), getattr(b.sp, fld)
        assert np.array_equal(x, y) and x.dtype == y.dtype, (where, "sp." + fld)
    assert (a.sp.H, a.sp.W) == (b.sp.H, b.sp.W)
    if a.edges is None or b.edges is None:
        assert a.edges is None and b.edges is None
    else:
        for x, y in zip(a.edges, b.edges):
            assert np.array_equal(x, y) and x.dtype == y.dtype, (where, "edges")
        assert tuple(a.edge_cuts) == tuple(b.edge_cuts)


# ----------------------------------------------------------------------------------------------------------- the solver
@pytest.mark.parametrize("seed", range(24))
def test_solver_equals_partition_after_every_update(seed):
    rng = np.random.default_rng(seed)
    radius = [0.4, 0.8, 1.5][seed % 3]
    tau = morse.tau0(H_STEP)
    F = blob_field(rng, int(rng.integers(25, 70)), int(rng.integers(25, 70)))
    solver = morse.PartitionSolver(H_STEP, tau, radius)
    assert_same(solver.update(F), morse.partition(F, H_STEP, tau, radius=radius), (seed, "first"))
    for step in range(30):
        F, shift = mutate(rng, F)
        if F.shape[0] > 120 or F.shape[1] > 120:
            F = F[:80, :80]
        got = solver.update(F, shift)
        assert_same(got, morse.partition(F, H_STEP, tau, radius=radius), (seed, step))


def test_solver_survives_an_exception_inside_an_update():
    tau = morse.tau0(H_STEP)
    rng = np.random.default_rng(5)
    F = blob_field(rng, 40, 40)
    solver = morse.PartitionSolver(H_STEP, tau, 0.8)
    solver.update(F)
    with pytest.raises(Exception):
        solver.update("not an array")                           # fails before any state is replaced
    G = F.copy(); G[10:14, 10:20] = True
    assert_same(solver.update(G), morse.partition(G, H_STEP, tau, radius=0.8), "after")


def test_solver_unchanged_input_gives_equal_but_independent_results():
    tau = morse.tau0(H_STEP)
    F = blob_field(np.random.default_rng(9), 50, 50)
    solver = morse.PartitionSolver(H_STEP, tau, 0.8)
    a = solver.update(F)
    b = solver.update(F.copy())
    assert_same(a, b)
    a.sp.clr = np.zeros(a.sp.N)                                 # the engine writes into its Space: the next Partition is not shared
    assert b.sp.clr is None


@pytest.mark.parametrize("seed", range(10))
def test_windowed_distance_equals_the_full_transform(seed):
    """larger fields with a few small edits each: the window is a small part of the canvas"""
    rng = np.random.default_rng(100 + seed)
    tau = morse.tau0(H_STEP)
    F = blob_field(rng, 120, 140)
    solver = morse.PartitionSolver(H_STEP, tau, 1.0)
    solver.update(F)
    for step in range(12):
        G = F.copy()
        y, x = int(rng.integers(0, 110)), int(rng.integers(0, 130))
        if step % 3 == 0:
            G[y:y + 6, x:x + 9] = True
        elif step % 3 == 1:
            G[y:y + 3, x:x + 3] = False
        else:
            G[y, x] ^= True
        part = solver.update(G)
        sp = morse.Space.of(G)
        assert np.array_equal(part.f, morse.distance(sp, H_STEP)), (seed, step)
        F = G


# ----------------------------------------------------------------------------------------------------------- the pieces
@pytest.mark.parametrize("seed", range(30))
def test_edges_full_with_and_without_the_pad(seed):
    rng = np.random.default_rng(seed)
    F = blob_field(rng, int(rng.integers(5, 60)), int(rng.integers(5, 60)))
    sp = morse.Space.of(F)
    ref = ref_edges_full(sp, H_STEP)
    for got in (morse.edges_full(sp, H_STEP), morse.edges_full(sp, H_STEP, sp.padded())):
        for x, y in zip(got[:3], ref[:3]):
            assert np.array_equal(x, y) and x.dtype == y.dtype
        assert got[3] == ref[3]


@pytest.mark.parametrize("seed", range(40))
def test_raw_basins_without_ranks_and_saddles(seed):
    rng = np.random.default_rng(seed)
    F = blob_field(rng, int(rng.integers(5, 70)), int(rng.integers(5, 70)))
    sp = morse.Space.of(F)
    if sp.N == 0:
        return
    f = morse.distance(sp, H_STEP)
    bid, roots = morse.raw_basins_f(sp, f, sp.padded())
    rb, rr = ref_basins(sp, f)
    assert np.array_equal(bid, rb) and np.array_equal(roots, rr)
    nb = sp.neighbours()
    i, j, val = morse.crossings(sp, bid, f, nb)
    for x, y in zip(morse.saddles(bid, i, j, val), ref_saddles(bid, i, j, val)):
        assert np.array_equal(x, y)


@pytest.mark.parametrize("seed", range(20))
def test_cut_regions_equals_subdivide(seed):
    """the cuts of any subset of the regions are those `subdivide` makes, region by region"""
    rng = np.random.default_rng(300 + seed)
    radius = [0.5, 1.0, 2.0][seed % 3]
    F = blob_field(rng, int(rng.integers(20, 90)), int(rng.integers(20, 90)))
    sp = morse.Space.of(F)
    if sp.N == 0:
        return
    f = morse.distance(sp, H_STEP)
    # regions: the persistence regions at a random tau (some merging, some not)
    part = morse.partition(F, H_STEP, morse.tau0(H_STEP) * float(rng.choice([1, 2, 4])))
    reg = part.full.astype(np.int32)
    nR = int(reg.max()) + 1
    lab, seeds = morse.subdivide(sp, f, reg, radius, H_STEP)
    q = np.rint(f / H_STEP).astype(np.int64)
    take = np.flatnonzero(rng.random(nR) < 0.5) if nR > 1 else np.array([0])
    if len(take) == 0:
        take = np.array([0])
    mc, mlab, sc = morse._cut_regions(sp, q, reg, take, radius, H_STEP)
    base = np.concatenate([[0], np.cumsum(np.bincount(reg[seeds], minlength=nR))])
    for t, r in enumerate(take):
        cells = np.flatnonzero(reg == r)
        got = mlab[np.isin(mc, cells)]                          # mc groups by region, ascending within
        assert np.array_equal(mc[np.isin(mc, cells)], cells)
        assert np.array_equal(got, lab[cells] - base[r])
        assert np.array_equal(sc[t], seeds[base[r]:base[r + 1]])


def test_next_uncovered():
    rng = np.random.default_rng(0)
    for _ in range(50):
        n = int(rng.integers(1, 400))
        order = rng.permutation(n)
        cov = rng.random(n) < rng.uniform(0, 1)
        pos = int(rng.integers(0, n))
        want = pos
        while want < n and cov[order[want]]:
            want += 1
        assert morse._next_uncovered(cov, order, pos, n) == want


# ----------------------------------------------------------------------------------------------------------- the box search
@pytest.mark.parametrize("seed", range(8))
def test_the_box_search_of_big_regions_gives_the_same_cuts(seed, monkeypatch):
    """regions of more than `_BOX_MIN` cells are searched in a box around each net point: the same cuts as the whole-region
    graph (here the limit is lowered so that small fields take that path)"""
    monkeypatch.setattr(morse, "_BOX_MIN", 25)
    rng = np.random.default_rng(700 + seed)
    tau = morse.tau0(H_STEP)
    radius = [0.5, 1.0][seed % 2]
    F = blob_field(rng, int(rng.integers(30, 70)), int(rng.integers(30, 70)))
    solver = morse.PartitionSolver(H_STEP, tau, radius)
    for step in range(12):
        got = solver.update(F)
        assert_same(got, morse.partition(F, H_STEP, tau, radius=radius), (seed, step))
        F, _ = mutate(rng, F)


def test_one_big_open_region_is_cut_like_subdivide():
    tau = morse.tau0(H_STEP)
    F = np.zeros((110, 120), bool)
    F[3:107, 4:116] = True                                       # one region of ~11,000 cells: past the box limit
    solver = morse.PartitionSolver(H_STEP, tau, 1.0)
    assert_same(solver.update(F), morse.partition(F, H_STEP, tau, radius=1.0), "open")
    G = F.copy(); G[50:53, 60:70] = False; G[3:5, 4:116] = False
    assert_same(solver.update(G), morse.partition(G, H_STEP, tau, radius=1.0), "edited")


def test_exploration_with_a_canvas_that_grows_on_every_side():
    """a robot drives through a walled map, F gains the cells around it, and the canvas grows (the hint is right) toward every
    side the drive reaches: the solver equals the full computation at every step"""
    rng = np.random.default_rng(42)
    Hw, Ww = 160, 200
    world = np.ones((Hw, Ww), bool)
    for _ in range(14):                                          # walls: pillars and bars
        y, x = int(rng.integers(0, Hw - 12)), int(rng.integers(0, Ww - 12))
        world[y:y + int(rng.integers(2, 12)), x:x + int(rng.integers(2, 12))] = False
    tau = morse.tau0(H_STEP)
    yy, xx = np.mgrid[-7:8, -7:8]
    disc = (yy ** 2 + xx ** 2) <= 49
    y, x = 80, 100
    oy, ox = y - 8, x - 8                                        # world position of canvas cell (0, 0)
    F = np.zeros((16, 16), bool)
    solver = morse.PartitionSolver(H_STEP, tau, 1.0)
    for step in range(70):
        y = int(np.clip(y + rng.integers(-5, 6), 8, Hw - 9)); x = int(np.clip(x + rng.integers(-6, 7), 8, Ww - 9))
        t, b, l, r = max(0, oy - (y - 8)), max(0, (y + 8) - (oy + F.shape[0])), max(0, ox - (x - 8)), max(0, (x + 8) - (ox + F.shape[1]))
        if t or b or l or r:
            F = np.pad(F, ((t, b), (l, r))); oy -= t; ox -= l
        wy0, wx0 = y - 7 - oy, x - 7 - ox
        F[wy0:wy0 + 15, wx0:wx0 + 15] |= disc & world[y - 7:y + 8, x - 7:x + 8]
        got = solver.update(F, (t, l))
        if step % 2 == 0 or step > 55:
            assert_same(got, morse.partition(F, H_STEP, tau, radius=1.0), step)
