"""The incremental solve (`ChartGraph._evaluate`, `_links`, `chart_keys.overlaps_cells`, `morse.arcs`) is a pure optimisation:
each fast routine against the plain code it replaced, on many random inputs, and the memoised engine against an engine whose
link memo is dropped before every batch (every link recomputed from the charts' routes)."""
import numpy as np
import pytest
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import connected_components, dijkstra

from packages.scene_graph import engine as E
from packages.scene_graph import morse
from packages.scene_graph.api import Leaf, SceneParams
from packages.scene_graph.chart_keys import overlaps, overlaps_cells
from packages.scene_graph.engine import ChartGraph, _csr_sym

pytestmark = pytest.mark.unit
RES = 0.05


# ------------------------------------------------------------------------------------------------------- overlaps_cells
@pytest.mark.parametrize("seed", range(60))
def test_overlaps_cells_equals_overlaps(seed):
    rng = np.random.default_rng(seed)
    H, W = int(rng.integers(3, 60)), int(rng.integers(3, 60))
    F = rng.random((H, W)) < rng.uniform(0.1, 1.0)
    ys, xs = np.nonzero(F)
    K = int(rng.integers(1, 40 if seed % 3 else 3000))
    lab = rng.integers(0, K, len(ys)).astype(np.int64)
    nprev = int(rng.integers(0, 40 if seed % 4 else 3000))
    plane = -np.ones((H, W), np.int32); plane[ys, xs] = lab
    prev = rng.integers(-1, max(nprev, 1), (H, W)).astype(np.int32)
    if nprev == 0:
        prev[:] = -1
    want = overlaps(prev, plane, nprev)
    got = overlaps_cells(prev, ys.astype(np.int32), xs.astype(np.int32), lab, nprev)
    assert got == want and list(got) == list(want)          # same pairs, same counts, same order


# ---------------------------------------------------------------------------------------------------------- _csr_sym
def _lattice_edges(rng, n):
    idx = np.arange(n * n).reshape(n, n)
    R, C, L = [], [], []
    keep = rng.random((n, n)) < 0.9
    for dy, dx in ((0, 1), (1, 0), (1, 1), (1, -1)):
        for y in range(n):
            for x in range(n):
                y2, x2 = y + dy, x + dx
                if 0 <= y2 < n and 0 <= x2 < n and keep[y, x] and keep[y2, x2]:
                    R.append(idx[y, x]); C.append(idx[y2, x2]); L.append(0.1 * (2 ** 0.5 if dy and dx else 1))
    return np.array(R, np.int64), np.array(C, np.int64), np.array(L), n * n


@pytest.mark.parametrize("seed", range(25))
def test_csr_sym_equals_coo(seed):
    rng = np.random.default_rng(seed)
    R, C, L, N = _lattice_edges(rng, int(rng.integers(1, 14)))
    p = rng.permutation(len(R))                                   # the order of the edges must not matter
    R, C, L = R[p], C[p], L[p]
    want = coo_matrix((np.concatenate([L, L]), (np.concatenate([R, C]), np.concatenate([C, R]))), shape=(N, N)).tocsr()
    got = _csr_sym(R, C, L, N)
    for f in ("indptr", "indices", "data"):
        assert np.array_equal(getattr(want, f), getattr(got, f)) and getattr(want, f).dtype == getattr(got, f).dtype
    if N:
        s = int(rng.integers(0, N))
        dw, pw = dijkstra(want, directed=False, indices=s, return_predecessors=True)
        dg, pg = dijkstra(got, directed=False, indices=s, return_predecessors=True)
        assert np.array_equal(dw, dg) and np.array_equal(pw, pg)


def test_csr_sym_repeated_pair_takes_the_conversion():
    R = np.array([0, 1, 0]); C = np.array([1, 2, 1]); L = np.array([1.0, 2.0, 3.0])
    want = coo_matrix((np.concatenate([L, L]), (np.concatenate([R, C]), np.concatenate([C, R]))), shape=(3, 3)).tocsr()
    got = _csr_sym(R, C, L, 3)
    assert np.array_equal(want.toarray(), got.toarray())


# ------------------------------------------------------------------------------------------------------------- arcs
def ref_arcs(sp, full, f, g=None):
    """morse.arcs as it was before the corner ids were formed for the selected cells only (full-length formulas)"""
    W1 = sp.W + 1
    Nn, Ee, Ss, Wn = sp.neighbours()
    y, x = sp.ys.astype(np.int64), sp.xs.astype(np.int64)
    ei, ej, ev0, ev1 = [], [], [], []
    for nb, v0, v1 in ((Ee, y * W1 + x + 1, (y + 1) * W1 + x + 1), (Ss, (y + 1) * W1 + x, (y + 1) * W1 + x + 1)):
        i = np.nonzero(nb >= 0)[0]
        j = nb[i].astype(np.int64)
        k = full[i] != full[j]
        ei.append(i[k]); ej.append(j[k]); ev0.append(v0[i[k]]); ev1.append(v1[i[k]])
    ei, ej, ev0, ev1 = (np.concatenate(v) for v in (ei, ej, ev0, ev1))
    nE = len(ei)
    if nE == 0:
        z = np.zeros(0, np.int64)
        return morse.Arcs(z, z, z, z, np.zeros(0), np.zeros(0), z)
    nV = (sp.H + 1) * W1
    bv = []
    for nb, v0, v1 in ((Nn, y * W1 + x, y * W1 + x + 1), (Ee, y * W1 + x + 1, (y + 1) * W1 + x + 1),
                       (Ss, (y + 1) * W1 + x, (y + 1) * W1 + x + 1), (Wn, y * W1 + x, (y + 1) * W1 + x)):
        m = nb < 0
        bv.append(v0[m]); bv.append(v1[m])
    bnd = np.bincount(np.concatenate(bv), minlength=nV)
    ifc = np.bincount(np.concatenate([ev0, ev1]), minlength=nV)
    joint = (ifc == 2) & (bnd == 0)
    inc_v = np.concatenate([ev0, ev1]); inc_e = np.concatenate([np.arange(nE), np.arange(nE)])
    m = joint[inc_v]
    inc_v, inc_e = inc_v[m], inc_e[m]
    o = np.argsort(inc_v, kind="stable")
    inc_e = inc_e[o]
    u, v = inc_e[0::2], inc_e[1::2]
    adj = coo_matrix((np.ones(len(u), np.int8), (u, v)), shape=(nE, nE))
    n_arcs, arc = connected_components(adj, directed=False)
    la, lb = full[ei], full[ej]
    lo, hi = np.minimum(la, lb), np.maximum(la, lb)
    p = np.where(la == lo, ei, ej); q = np.where(la == lo, ej, ei)
    val = np.minimum(f[ei], f[ej])
    o = np.lexsort((sp.xs[q], sp.ys[q], sp.xs[p], sp.ys[p], -val, arc))
    arc_o = arc[o]
    first = np.concatenate([[True], arc_o[1:] != arc_o[:-1]])
    best = o[first]
    size = np.bincount(arc, minlength=n_arcs)[arc_o[first]]
    if g is None:
        gv = val[best]
    else:
        gm = np.minimum(g[ei], g[ej])[o]
        gv = np.maximum.reduceat(gm, np.nonzero(first)[0])
    res = morse.Arcs(lo[best].astype(np.int64), hi[best].astype(np.int64), p[best].astype(np.int64), q[best].astype(np.int64),
                     val[best], gv, size)
    k = np.lexsort((sp.xs[res.q], sp.ys[res.q], sp.xs[res.p], sp.ys[res.p], res.b, res.a))
    return morse.Arcs(res.a[k], res.b[k], res.p[k], res.q[k], res.s[k], res.g[k], res.size[k])


@pytest.mark.parametrize("seed", range(30))
def test_arcs_equals_reference(seed):
    rng = np.random.default_rng(seed)
    H, W = int(rng.integers(8, 70)), int(rng.integers(8, 70))
    F = rng.random((H, W)) < rng.uniform(0.5, 1.0)
    part = morse.partition(F, 0.1, morse.tau0(0.1), radius=float(rng.choice([0.3, 0.6, 1.0])))
    if part.sp.N == 0:
        return
    g = rng.random(part.sp.N) if seed % 2 else None
    want = ref_arcs(part.sp, part.full, part.f, g)
    got = morse.arcs(part.sp, part.full, part.f, g)
    for fld in ("a", "b", "p", "q", "s", "g", "size"):
        assert np.array_equal(getattr(want, fld), getattr(got, fld)) and getattr(want, fld).dtype == getattr(got, fld).dtype


# ------------------------------------------------------------------------------------------------------------ engine
def _world(seed):
    rng = np.random.default_rng(seed)
    WX, WY = float(rng.uniform(14, 26)), float(rng.uniform(10, 16))
    T = np.zeros((int(WY / RES), int(WX / RES)), np.uint8)
    T[:8, :] = 100; T[-8:, :] = 100; T[:, :8] = 100; T[:, -8:] = 100
    pitch, shelf, aisle = 3.6, 1.2, 2.4
    y = 1.5; ycs = []
    while y + pitch < WY - 1:
        x0 = int(rng.uniform(2.0, 4.0) / RES); x1 = int((WX - rng.uniform(2.0, 4.0)) / RES)
        T[int((y + aisle) / RES):int((y + aisle + shelf) / RES), x0:x1] = 100
        ycs.append(y + aisle / 2); y += pitch
    for _ in range(int(rng.integers(0, 5))):                      # pillars
        cy, cx = rng.integers(20, T.shape[0] - 30), rng.integers(20, T.shape[1] - 30)
        T[cy:cy + 6, cx:cx + 6] = 100
    step = float(rng.choice([0.5, 0.75]))
    path = []
    for i, yc in enumerate(ycs):
        xs = np.arange(1.5, WX - 1.5, step)
        if i % 2: xs = xs[::-1]
        for x in xs: path.append((float(x), float(yc)))
        if i + 1 < len(ycs):
            for yy in np.arange(yc + step, ycs[i + 1], step): path.append((float(xs[-1]), float(yy)))
    return rng, T, path


def _leaves(seed, n):
    rng, T, path = _world(seed)
    out = []
    yy, xx = np.mgrid[0:140, 0:140]
    far = np.hypot(yy - 70, xx - 70) * RES > 3.2
    for i, (x, y) in enumerate(path[:n]):
        x0, y0 = x - 3.5, y - 3.5
        gx, gy = int(round(x0 / RES)), int(round(y0 / RES))
        w = np.full((140, 140), 255, np.uint8)
        sy0, sx0 = max(gy, 0), max(gx, 0); sy1, sx1 = min(gy + 140, T.shape[0]), min(gx + 140, T.shape[1])
        if sy0 < sy1 and sx0 < sx1:
            w[sy0 - gy:sy1 - gy, sx0 - gx:sx1 - gx] = T[sy0:sy1, sx0:sx1]
        if rng.random() < 0.4:                                    # a transient obstacle
            cy, cx = int(rng.integers(10, 120)), int(rng.integers(10, 120))
            w[cy:cy + 5, cx:cx + 5] = 100
        w[(rng.random((140, 140)) < 0.002) & (w < 100)] = 100
        w[far] = 255
        ch = {"occ2": np.where(w == 255, 255, np.minimum(w, 99)).astype(np.uint8)} if i % 2 == 0 else {}
        out.append(Leaf(key=f"n{i:05d}", session="s1" if i < len(path) // 2 else "s2", stamp_ms=1_760_000_000_000 + 1000 * i,
                        x=x, y=y, yaw=0.0, resolution=RES, origin=(gx * RES, gy * RES), window_u8=w,
                        r_d=0.25, channels=ch))
    return rng, out


def _snap(g):
    s = g._solved
    if s is None:
        return None
    return dict(charts=s.charts, links=s.links, keys=s.keys, A=s.A, peak=s.peak.tolist(), plane=s.plane.tobytes(),
                retired=dict(g._retired_map), qk=g._keys, kp=g._key_plane.tobytes())


@pytest.mark.parametrize("seed,near", [(0, 1.3), (1, None), (2, 1.3), (3, None), (4, 1.3)])
def test_memoised_links_equal_full_recomputation(seed, near):
    rng, leaves = _leaves(seed, 70)
    prm = SceneParams(robot_half_side=0.25, lattice=0.1, chart_radius=1.0 + 0.5 * (seed % 3), near_range_m=near,
                      near_min_clearance_m=None if near is None else 0.25)
    A, B = ChartGraph(prm), ChartGraph(prm)
    i = 0
    hits = 0
    while i < len(leaves):
        n = int(rng.integers(1, 7))
        batch = leaves[i:i + n]; i += n
        if rng.random() < 0.15 and i > 4:                          # an old leaf delivered again, out of order
            batch = batch + [leaves[int(rng.integers(0, i - 1))]]
        B._lmemo = None                                            # B: no memo, every link recomputed
        da, db = A.fold_many(batch), B.fold_many(batch)
        assert _snap(A) == _snap(B)
        assert (da.bbox, da.changed, da.links, sorted(da.retired.items())) == (db.bbox, db.changed, db.links, sorted(db.retired.items()))
        if A._lmemo is not None:
            hits += len(A._lmemo[1])
    assert hits > 0                                                # the memo was in use
    # an import round trip keeps the memoised engine equal to a fresh one
    st = A.export_state()
    C = ChartGraph(prm); C.import_state(st)
    assert _snap(C)["links"] == _snap(A)["links"]


def test_channel_rows_equal_the_plane_expressions():
    rng, leaves = _leaves(7, 40)
    g = ChartGraph(SceneParams(robot_half_side=0.25, lattice=0.1, chart_radius=1.5, near_range_m=1.3, near_min_clearance_m=0.25))
    for i in range(0, len(leaves), 5):
        g.fold_many(leaves[i:i + 5])
        s = g._solved
        chv, fm, src, eb = g._channel_rows(s.sp, s.S)
        v = g._V[::2, ::2]
        ch = {"undriven": (~s.S).astype(float), "ever_blocked": (g._Br[::2, ::2] >= 0).astype(float),
              "soft": np.where((v > 0) & (v < 100), v / 99.0, 0.0)}
        for name, (cv, _) in g._chan.items():
            w = cv[::2, ::2]; ch[name] = np.where((w > 0) & (w < 100), w / 99.0, 0.0)
        assert list(chv) == list(ch) and fm.dtype == np.float64
        for k, f in ch.items():
            assert np.array_equal(chv[k], f[s.sp.ys, s.sp.xs])
        assert src is not None and src.dtype == np.uint8 and src.shape == (s.sp.N, len(ch))
