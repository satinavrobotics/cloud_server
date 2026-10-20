"""The fast routines of morse.py against the straightforward code they replaced (kept here verbatim as references): same
arrays, same merge sequence, same cache keys, on many random inputs."""
import hashlib
import heapq
import math

import numpy as np
import pytest
from scipy import ndimage
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import dijkstra

from packages.scene_graph import morse

pytestmark = pytest.mark.unit
TOL = morse.TOL


def _field(seed):
    rng = np.random.default_rng(seed)
    H, W = int(rng.integers(6, 80)), int(rng.integers(6, 80))
    F = rng.random((H, W)) < rng.uniform(0.3, 0.95)
    if seed % 2:
        F = ndimage.binary_opening(F, iterations=1) | (rng.random((H, W)) < 0.02)
    if seed % 5 == 0:
        F = ndimage.gaussian_filter(rng.random((H, W)), 2.0) > 0.45
    return F


# ----------------------------------------------------------------------------------------------------------- references
def ref_merge_tree(peakv, peakr, lo, hi, s, tau):
    peakv = dict(enumerate(np.asarray(peakv, float).tolist())) if not isinstance(peakv, dict) else dict(peakv)
    peakr = dict(enumerate(np.asarray(peakr).tolist())) if not isinstance(peakr, dict) else dict(peakr)
    sad = {}
    for a, b, v in zip(np.asarray(lo).tolist(), np.asarray(hi).tolist(), np.asarray(s, float).tolist()):
        sad.setdefault(a, {})[b] = v
        sad.setdefault(b, {})[a] = v
    ver = {r: 0 for r in peakv}
    heap = []
    limit = tau - TOL

    def push(a, b):
        p = min(peakv[a], peakv[b]) - sad[a][b]
        if p < limit:
            ka, kb = (a, b) if peakr[a] < peakr[b] else (b, a)
            heapq.heappush(heap, (p, -sad[a][b], peakr[ka], peakr[kb], a, b, ver[a], ver[b]))
    for a in sad:
        for b in sad[a]:
            if a < b:
                push(a, b)
    out = []
    while heap:
        p, _, _, _, a, b, va, vb = heapq.heappop(heap)
        if a not in ver or b not in ver or ver[a] != va or ver[b] != vb or b not in sad.get(a, {}):
            continue
        keep, drop = (a, b) if (peakv[a], -peakr[a]) >= (peakv[b], -peakr[b]) else (b, a)
        out.append((p, keep, drop))
        if peakv[a] == peakv[b]:
            peakr[keep] = min(peakr[a], peakr[b])
        peakv[keep] = max(peakv[a], peakv[b])
        sk, sd = sad[keep], sad[drop]
        sk.pop(drop, None)
        sd.pop(keep, None)
        for x, v in sd.items():
            nv = max(sk.get(x, -math.inf), v)
            sk[x] = nv
            sx = sad[x]
            sx.pop(drop, None)
            sx[keep] = nv
        del sad[drop], ver[drop], peakv[drop], peakr[drop]
        ver[keep] += 1
        for x in sk:
            push(keep, x) if keep < x else push(x, keep)
    return out


def ref_centres(sp, lab, f, K):
    cnt = np.bincount(lab, minlength=K)
    cy = np.bincount(lab, weights=sp.ys, minlength=K) / np.maximum(cnt, 1)
    cx = np.bincount(lab, weights=sp.xs, minlength=K) / np.maximum(cnt, 1)
    d2 = np.round((sp.ys - cy[lab]) ** 2 + (sp.xs - cx[lab]) ** 2, 9)
    o = np.lexsort((np.arange(sp.N), -f, d2, lab))
    first = np.concatenate([[True], lab[o][1:] != lab[o][:-1]])
    return o[first].astype(np.int64)


def ref_raw_basins(sp, rank):
    N = sp.N
    nb = np.stack(sp.neighbours())
    rn = np.where(nb >= 0, rank[np.maximum(nb, 0)], morse.BIG)
    arg = rn.argmin(0)
    has = rn[arg, np.arange(N)] < rank
    par = np.where(has, nb[arg, np.arange(N)], np.arange(N)).astype(np.int64)
    while True:
        nxt = par[par]
        if np.array_equal(nxt, par):
            break
        par = nxt
    roots = np.unique(par)
    roots = roots[np.argsort(rank[roots], kind="stable")]
    bid = np.empty(N, np.int64)
    pos = np.empty(N, np.int64)
    pos[roots] = np.arange(len(roots))
    bid[:] = pos[par]
    return bid, roots


def ref_neighbours(sp):
    P = np.pad(sp.idx, 1, constant_values=-1)
    y, x = sp.ys + 1, sp.xs + 1
    return P[y - 1, x], P[y, x + 1], P[y + 1, x], P[y, x - 1]


def ref_edges8(sp, h, region=None):
    R, C, L = [], [], []
    for dy, dx in ((0, 1), (1, 0), (1, 1), (1, -1)):
        y2, x2 = sp.ys + dy, sp.xs + dx
        ok = (y2 >= 0) & (y2 < sp.H) & (x2 >= 0) & (x2 < sp.W)
        ok[ok] &= sp.nav[y2[ok], x2[ok]]
        a = np.nonzero(ok)[0]
        b = sp.idx[y2[a], x2[a]]
        if dy and dx:
            keep = sp.nav[sp.ys[a], x2[a]] & sp.nav[y2[a], sp.xs[a]]
            a, b = a[keep], b[keep]
            if region is not None:
                o1, o2 = sp.idx[sp.ys[a], x2[a]], sp.idx[y2[a], sp.xs[a]]
                keep = (region[o1] == region[a]) & (region[o2] == region[a])
                a, b = a[keep], b[keep]
        if region is not None:
            keep = region[a] == region[b]
            a, b = a[keep], b[keep]
        R.append(a); C.append(b); L.append(np.full(len(a), math.hypot(dy, dx) * h))
    return np.concatenate(R), np.concatenate(C), np.concatenate(L)


def ref_subdivide(sp, f, region, radius, h, cache=None):
    N = sp.N
    R, C, L = ref_edges8(sp, h, region)
    eo = np.argsort(region[R], kind="stable")
    R, C, L = R[eo], C[eo], L[eo]
    nR = int(region.max()) + 1
    ecut = np.searchsorted(region[R], np.arange(nR + 1))
    co = np.argsort(region, kind="stable")
    ccut = np.searchsorted(region[co], np.arange(nR + 1))
    q = np.rint(f / h).astype(np.int64)
    lab = np.empty(N, np.int32)
    seeds = []
    used = {}
    for r in range(nR):
        cells = np.sort(co[ccut[r]:ccut[r + 1]])
        n = len(cells)
        ck = None
        if cache is not None:
            ys, xs = sp.ys[cells], sp.xs[cells]
            hk = hashlib.blake2b(digest_size=16)
            for a in ((ys - ys.min()).astype(np.int32), (xs - xs.min()).astype(np.int32), q[cells].astype(np.int32)):
                hk.update(a.tobytes())
            hk.update(np.array([radius, h], np.float64).tobytes())
            ck = hk.digest()
            hit = cache.get(ck)
            if hit is not None:
                used[ck] = hit
                lab[cells] = hit[0] + len(seeds)
                seeds.extend(int(cells[i]) for i in hit[1])
                continue
        e0, e1 = ecut[r], ecut[r + 1]
        lr, lc = np.searchsorted(cells, R[e0:e1]), np.searchsorted(cells, C[e0:e1])
        G = coo_matrix((np.concatenate([L[e0:e1], L[e0:e1]]), (np.concatenate([lr, lc]), np.concatenate([lc, lr]))),
                       shape=(n, n)).tocsr()
        order = np.argsort(-q[cells], kind="stable")
        covered = np.zeros(n, bool)
        best_d = np.full(n, np.inf)
        best_s = np.full(n, -1, np.int64)
        base = len(seeds)
        ys, xs = sp.ys[cells], sp.xs[cells]
        y0, x0 = int(ys.min()), int(xs.min())
        img = -np.ones((int(ys.max()) - y0 + 1, int(xs.max()) - x0 + 1), np.int64)
        img[ys - y0, xs - x0] = np.arange(n)
        rad = int(math.ceil(radius / h)) + 1
        pos = 0
        while True:
            while pos < n and covered[order[pos]]:
                pos += 1
            if pos == n:
                break
            s = int(order[pos])
            sy, sx = int(ys[s]) - y0, int(xs[s]) - x0
            win = img[max(0, sy - rad):sy + rad + 1, max(0, sx - rad):sx + rad + 1]
            loc = win[win >= 0]
            sub = G[loc][:, loc]
            d = dijkstra(sub, directed=False, indices=int(np.nonzero(loc == s)[0][0]), limit=radius + TOL)
            hit = np.isfinite(d)
            reach = loc[hit]
            covered[reach] = True
            dd = d[hit]
            better = dd < best_d[reach] - TOL
            best_d[reach[better]] = dd[better]
            best_s[reach[better]] = len(seeds) - base
            seeds.append(int(cells[s]))
        lab[cells] = best_s + base
        if ck is not None:
            used[ck] = (best_s.astype(np.int32), [int(np.searchsorted(cells, c)) for c in seeds[base:]])
    if cache is not None:
        cache.clear(); cache.update(used)
    return lab, np.array(seeds, np.int64)


# ----------------------------------------------------------------------------------------------------------- tests
@pytest.mark.parametrize("seed", range(40))
def test_group_order_is_the_stable_argsort(seed):
    rng = np.random.default_rng(seed)
    K = int(rng.choice([1, 2, 7, 300, 65535, 65536, 70000]))
    lab = rng.integers(0, K, int(rng.integers(1, 5000))).astype(rng.choice([np.int32, np.int64]))
    assert np.array_equal(morse.group_order(lab, K), np.argsort(lab, kind="stable"))


@pytest.mark.parametrize("seed", range(30))
def test_neighbours_edges_and_raw_basins_match(seed):
    F = _field(seed)
    sp = morse.Space.of(F)
    if sp.N == 0:
        return
    for a, b in zip(sp.neighbours(), ref_neighbours(sp)):
        assert a.dtype == b.dtype and np.array_equal(a, b)
    for a, b in zip(morse.edges8(sp, 0.1), ref_edges8(sp, 0.1)):
        assert a.dtype == b.dtype and np.array_equal(a, b)
    f = morse.distance(sp, 0.1)
    rank = morse.ranks(f)
    bid, roots = morse.raw_basins(sp, rank)
    rb, rr = ref_raw_basins(sp, rank)
    assert np.array_equal(bid, rb) and np.array_equal(roots, rr)
    # ranks that are not a permutation take the general path
    r2 = (rank // 2).astype(np.int64)
    assert all(np.array_equal(x, y) for x, y in zip(morse.raw_basins(sp, r2), ref_raw_basins(sp, r2)))


@pytest.mark.parametrize("seed", range(30))
def test_edges_within_a_region_match(seed):
    F = _field(seed)
    sp = morse.Space.of(F)
    if sp.N == 0:
        return
    rng = np.random.default_rng(seed)
    region = ndimage.label(F, structure=np.ones((3, 3)))[0][sp.ys, sp.xs].astype(np.int32) - 1
    region = (region * 3 + (rng.random(sp.N) < 0.3)).astype(np.int32)           # break regions up: diagonals across a pass
    for a, b in zip(morse.edges8(sp, 0.1, region), ref_edges8(sp, 0.1, region)):
        assert a.dtype == b.dtype and np.array_equal(a, b)


@pytest.mark.parametrize("seed", range(40))
def test_merge_tree_is_the_same_sequence(seed):
    F = _field(seed)
    sp = morse.Space.of(F)
    if sp.N < 3:
        return
    f = morse.distance(sp, 0.1); rank = morse.ranks(f)
    bid, roots = morse.raw_basins(sp, rank)
    i, j, v = morse.crossings(sp, bid, f)
    lo, hi, s = morse.saddles(bid, i, j, v)
    rng = np.random.default_rng(seed)
    cases = [(f[roots], rank[roots], lo, hi, s)]
    if len(lo) > 3:
        perm = rng.permutation(len(lo))                                          # unsorted pairs
        cases.append((f[roots], rank[roots], lo[perm], hi[perm], s[perm]))
        cases.append((f[roots], rank[roots], np.r_[lo, lo[:2]], np.r_[hi, hi[:2]], np.r_[s, s[:2] * 0.5]))   # duplicate pairs
        cases.append((dict(enumerate(f[roots].tolist())), rank[roots], lo, hi, s))                           # dict peaks
    for pv, pr, a, b, c in cases:
        for tau in (morse.tau0(0.1), 3 * morse.tau0(0.1), math.inf):
            got, want = morse.merge_tree(pv, pr, a, b, c, tau), ref_merge_tree(pv, pr, a, b, c, tau)
            assert got == want
            assert all(type(x[0]) is float and type(x[1]) is int and type(x[2]) is int for x in got)


@pytest.mark.parametrize("seed", range(30))
def test_merge_tree_with_ties_on_plateaus(seed):
    rng = np.random.default_rng(seed)
    n = int(rng.integers(2, 60))
    peak = rng.integers(1, 5, n).astype(float)                                   # many equal peaks
    rank = rng.permutation(n)
    pairs = {(int(a), int(b)) for a, b in zip(rng.integers(0, n, 3 * n), rng.integers(0, n, 3 * n)) if a < b}
    lo = np.array([p[0] for p in sorted(pairs)], np.int64); hi = np.array([p[1] for p in sorted(pairs)], np.int64)
    s = rng.integers(0, 4, len(lo)).astype(float)
    for tau in (0.5, 2.0, 10.0, math.inf):
        assert morse.merge_tree(peak, rank, lo, hi, s, tau) == ref_merge_tree(peak, rank, lo, hi, s, tau)


@pytest.mark.parametrize("seed", range(40))
def test_centres_match(seed):
    F = _field(seed)
    sp = morse.Space.of(F)
    if sp.N == 0:
        return
    f = morse.distance(sp, 0.1)
    rng = np.random.default_rng(seed)
    K = int(rng.integers(1, max(2, min(sp.N, 60))))
    lab = np.sort(rng.integers(0, K, sp.N)) if seed % 2 else rng.integers(0, K, sp.N)
    lab = np.unique(lab, return_inverse=True)[1].astype(np.int32)                # dense labels
    K = int(lab.max()) + 1
    assert np.array_equal(morse.centres(sp, lab, f, K), ref_centres(sp, lab, f, K))
    f2 = np.round(f / 0.1)                                                       # plateaus: ties on room
    assert np.array_equal(morse.centres(sp, lab, f2, K), ref_centres(sp, lab, f2, K))


@pytest.mark.parametrize("seed", range(24))
def test_subdivide_and_its_cache_match(seed):
    F = _field(seed)
    sp = morse.Space.of(F)
    if sp.N == 0:
        return
    h, tau = 0.1, morse.tau0(0.1)
    radius = [0.25, 0.4, 1.0][seed % 3]
    f = morse.distance(sp, h); rank = morse.ranks(f)
    bid, roots = morse.raw_basins(sp, rank)
    i, j, v = morse.crossings(sp, bid, f)
    lo, hi, s = morse.saddles(bid, i, j, v)
    surv = morse.survivors(len(roots), morse.merge_tree(f[roots], rank[roots], lo, hi, s, tau))
    keep = np.unique(surv); new = np.empty(len(roots), np.int64); new[keep] = np.arange(len(keep))
    reg = new[surv[bid]].astype(np.int32)
    c_ref, c_new = {}, {}
    for rep in range(3):                                                         # cold, warm, warm again
        a = ref_subdivide(sp, f, reg, radius, h, c_ref)
        b = morse.subdivide(sp, f, reg, radius, h, c_new)
        assert np.array_equal(a[0], b[0]) and np.array_equal(a[1], b[1])
        assert list(c_ref) == list(c_new)
        for k in c_ref:
            assert np.array_equal(c_ref[k][0], c_new[k][0]) and c_ref[k][1] == c_new[k][1]
    a = ref_subdivide(sp, f, reg, radius, h)
    b = morse.subdivide(sp, f, reg, radius, h)
    assert np.array_equal(a[0], b[0]) and np.array_equal(a[1], b[1])
    # the partition carries the edges the engine reads
    p = morse.partition(F, h, tau, radius)
    for x, y in zip(p.edges, ref_edges8(sp, h)):
        assert np.array_equal(x, y)


def test_row_sums_of_a_contiguous_matrix_are_the_sums_of_the_rows():
    """engine._links sums the per-channel products with one `sum(axis=1)`: bit for bit the 1-D sum of each row"""
    rng = np.random.default_rng(3)
    for n in list(range(1, 200)) + [333, 1000, 4097]:
        for nch in (1, 3, 5, 8):
            a = rng.random((nch, n)) * rng.choice([1.0, 1e3, 1e-3])
            a[rng.random((nch, n)) < 0.3] = 0.0
            r = a.sum(axis=1)
            assert all(r[i] == a[i].sum() for i in range(nch))
