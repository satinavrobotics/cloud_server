"""Persistence (Morse) partition of the navigable lattice F, as pure functions of F and the lattice step h.

    B(x)      distance from a cell of F to the nearest cell NOT in F (cell centres; outside the canvas is not F). B = h on the
              boundary of F.  The total order  x < y  <=>  (B(x) > B(y)) or (equal and (row, col) smaller)  is translation
              invariant, so everything below is a function of the shape of F, not of where the canvas sits.
    basins    every cell hangs on its earliest (smallest rank) 4-neighbour that is earlier than itself; the cells with none are
              the maxima. A raw basin is the set of cells that reach one maximum.
    merge     the pair of adjacent regions (a, b) has persistence  p = min(peak a, peak b) - s(a, b)  where s is the largest
              min(B) over the 4-adjacent cell pairs that cross from a to b. Pairs are merged, smallest p first, while p < tau;
              the survivor is the region with the higher peak. The p of the pair popped next never decreases (a merged region
              has the higher peak and a saddle that is at least either old one), so the regions of every smaller tau are a
              prefix of the same sequence: the levels tau_j = tau_0 * 2^j are nested by construction.
    tau_0     2 sqrt(2) h: the smallest persistence that cannot be produced by the discretisation of one smooth ridge (a peak
              cell and its saddle can differ by up to that on the lattice, the bound is attained).
    anchor    the cell of maximal B of a region; a plateau resolves to the plateau cell nearest its centroid, ties by (row, col).
    link      one per ARC of the interface: a maximal chain of unit edges between two regions, joined at corners where exactly
              two interface edges and no boundary edge meet. Its portal is the crossing pair attaining s over the arc.
    width     is NOT a function of B: F is the robot-fit set plus driven witness cells, so B at a crossing says how far the
              cell is from the edge of F, not how wide the passage is (a driven 0.4 m gap has a 3-cell F strip). The engine
              measures it on the physical clearance field g and reports 2 * max over the arc's crossings of min(g) (arcs(.., g)).

Charts: with a radius each region is cut into geodesic Voronoi cells of a greedy net taken in the order of room (`subdivide`):
every cell is within `radius` of its anchor, anchors of a region are further than `radius` apart, no chart crosses a pass.
The engine's radius is SceneParams.chart_radius; `levels` above the charts are `cell_levels`.

Counting identity (any tie order): chi(F) = #maxima - sum(saddle multiplicities) (B has no minima). Nothing here reads a chart
index, a leaf or the canvas: the engine calls `partition` once per batch.

`PartitionSolver` is `partition` with a memory: it keeps the previous solve and returns, for every F, exactly the Partition
`partition` returns, at a cost that follows what changed. B is recomputed on the box of the changed cells grown by a bound on
B (a cell farther from every changed cell than its old B keeps it); a region whose cell set and room are those of a region of
the previous solve is served from it (the greedy net is a function of those); the other regions are cut again. The previous
solve is compared with F cell by cell, so the canvas shift it is given is a hint that can only cost time.
"""
import hashlib
import heapq
import math
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import numpy as np
from scipy import ndimage
from scipy.sparse import coo_matrix, csr_matrix
from scipy.sparse.csgraph import connected_components, dijkstra

TAU0_FACTOR = 2.0 * math.sqrt(2.0)
BASE_TAU = 1                        # the base partition merges persistence below BASE_TAU * tau_0; level j: * 2^j
TOL = 1e-9                          # persistence comparisons: p < tau - TOL merges (p exactly tau is kept apart)
BIG = np.iinfo(np.int64).max


def tau0(h: float) -> float:
    return TAU0_FACTOR * h


@dataclass
class Space:
    """The cells of F as arrays, row-major: cell i is (ys[i], xs[i]); `idx` is the image of cell indices (-1 outside F)."""
    H: int
    W: int
    ys: np.ndarray
    xs: np.ndarray
    idx: np.ndarray
    clr: Optional[np.ndarray] = None    # per cell, set by the caller that knows r (see `clearance`)

    @property
    def N(self) -> int:
        return len(self.ys)

    @property
    def nav(self) -> np.ndarray:
        return self.idx >= 0

    @staticmethod
    def of(F: np.ndarray) -> "Space":
        H, W = F.shape
        ys, xs = np.nonzero(F)
        idx = -np.ones((H, W), np.int32)
        idx[ys, xs] = np.arange(len(ys), dtype=np.int32)
        return Space(H, W, ys.astype(np.int32), xs.astype(np.int32), idx)

    def padded(self) -> Tuple[np.ndarray, np.ndarray, int]:
        """(the cell-index image with a ring of -1 around it, flattened; the flat place of every cell in it; its row length)"""
        Wp = self.W + 2
        P = np.full((self.H + 2) * Wp, -1, np.int32)
        P.reshape(self.H + 2, Wp)[1:-1, 1:-1] = self.idx
        return P, (self.ys.astype(np.int64) + 1) * Wp + self.xs + 1, Wp

    def neighbours(self) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        """cell index of the N, E, S, W 4-neighbour of every cell (-1: not in F)"""
        P, pos, Wp = self.padded()
        return P[pos - Wp], P[pos + 1], P[pos + Wp], P[pos - 1]


# ---------------------------------------------------------------------------------------------------------------- function
def distance(sp: Space, h: float) -> np.ndarray:
    """B per cell of `sp`: exact Euclidean distance to the nearest non-F cell, times h. The transform runs on the bounding
    box of F plus a ring of non-F cells: a non-F cell outside the ring is never nearer than the ring cell on the way to it."""
    if sp.N == 0:
        return np.zeros(0)
    y0, y1, x0, x1 = int(sp.ys.min()), int(sp.ys.max()), int(sp.xs.min()), int(sp.xs.max())
    sub = np.zeros((y1 - y0 + 3, x1 - x0 + 3), bool)
    sub[sp.ys - y0 + 1, sp.xs - x0 + 1] = True
    d = ndimage.distance_transform_edt(sub)
    return d[sp.ys - y0 + 1, sp.xs - x0 + 1] * h


def group_order(lab: np.ndarray, K: int) -> np.ndarray:
    """np.argsort(lab, kind="stable") for labels in [0, K): the same permutation (a stable sort has exactly one result), found by
    numpy's radix sort when the labels fit 16 bits"""
    if K <= 65535 and len(lab):
        return np.argsort(lab.astype(np.uint16), kind="stable")
    return np.argsort(lab, kind="stable")


def ranks(f: np.ndarray) -> np.ndarray:
    """rank of every cell in the order (-f, row, col); cells are row-major, so a stable sort of -f is that order"""
    order = np.argsort(-f, kind="stable")
    r = np.empty(len(f), np.int64)
    r[order] = np.arange(len(f))
    return r


def raw_basins(sp: Space, rank: np.ndarray, nb: Optional[tuple] = None) -> Tuple[np.ndarray, np.ndarray]:
    """(basin id per cell, root cell per basin). Basin ids follow the rank of the roots. `nb`: sp.neighbours() when known."""
    N = sp.N
    if rank.dtype.kind in "iu" and len(rank) == N and N and int(rank.min()) >= 0 and int(rank.max()) < N and bool((np.bincount(rank, minlength=N) == 1).all()):
        # ranks are a permutation of 0..N-1 (what `ranks` returns): the earliest neighbour is the cell holding the least
        # neighbour rank, found on the padded rank image without the cell indices of the neighbours
        P, pos, Wp = sp.padded()
        RP = np.full(len(P), BIG, np.int64)
        RP[pos] = rank
        m = np.minimum(np.minimum(RP[pos - Wp], RP[pos + 1]), np.minimum(RP[pos + Wp], RP[pos - 1]))
        has = m < rank
        cell_of = np.empty(N, np.int64); cell_of[rank] = np.arange(N)
        par = np.where(has, cell_of[np.minimum(m, N - 1)], np.arange(N)).astype(np.int64)
    else:
        nb = np.stack(sp.neighbours() if nb is None else nb)                # (4, N)
        rn = np.where(nb >= 0, rank[np.maximum(nb, 0)], BIG)
        arg = rn.argmin(0)
        has = rn[arg, np.arange(N)] < rank
        par = np.where(has, nb[arg, np.arange(N)], np.arange(N)).astype(np.int64)
    while True:                                                             # pointer doubling: ranks fall along every chain
        nxt = par[par]
        if np.array_equal(nxt, par):
            break
        par = nxt
    roots = np.nonzero(par == np.arange(N))[0]                              # the fixed points: the values of par, ascending
    roots = roots[np.argsort(rank[roots], kind="stable")]
    bid = np.empty(N, np.int64)
    pos = np.empty(N, np.int64)
    pos[roots] = np.arange(len(roots))
    bid[:] = pos[par]
    return bid, roots


def raw_basins_f(sp: Space, f: np.ndarray, pad: tuple) -> Tuple[np.ndarray, np.ndarray]:
    """`raw_basins(sp, ranks(f))` without ranks: (basin id per cell, root cell per basin, roots in rank order). A neighbour precedes
    a cell iff it has the larger f, or an equal f and a smaller (row, col): that is the N and W neighbour; among the preceding
    ones the earliest is the one of largest f, then N, W, E, S. `pad`: `sp.padded()`."""
    N = sp.N
    P, pos, Wp = pad
    FP = np.full(len(P), -np.inf)
    FP[pos] = f
    cand = np.stack([FP[pos - Wp], FP[pos - 1], FP[pos + 1], FP[pos + Wp]])          # N, W, E, S
    arg = cand.argmax(0)                                                              # the first of equal values: N, W, E, S
    ar = np.arange(N)
    bf = cand[arg, ar]
    has = (bf > f) | ((bf == f) & (arg < 2))
    par = np.where(has, P[pos + np.array([-Wp, -1, 1, Wp])[arg]], ar).astype(np.int64)
    while True:                                                                       # pointer doubling: ranks fall along every chain
        nxt = par[par]
        if np.array_equal(nxt, par):
            break
        par = nxt
    roots = np.flatnonzero(par == ar)
    roots = roots[np.argsort(-f[roots], kind="stable")]
    pr = np.empty(N, np.int64)
    pr[roots] = np.arange(len(roots))
    return pr[par], roots


def crossings(sp: Space, lab: np.ndarray, f: np.ndarray, nb: Optional[tuple] = None):
    """4-adjacent cell pairs (i, j) in different regions: (i, j, value min(f_i, f_j)); each unordered pair once (E and S)"""
    _, E, S, _ = sp.neighbours() if nb is None else nb
    out = []
    for nb in (E, S):
        i = np.nonzero(nb >= 0)[0]
        j = nb[i].astype(np.int64)
        k = lab[i] != lab[j]
        out.append((i[k], j[k]))
    i = np.concatenate([o[0] for o in out]); j = np.concatenate([o[1] for o in out])
    return i, j, np.minimum(f[i], f[j])


def saddles(lab: np.ndarray, i: np.ndarray, j: np.ndarray, val: np.ndarray):
    """(lo, hi, s): per adjacent region pair the largest crossing value"""
    if len(i) == 0:
        z = np.zeros(0, np.int64)
        return z, z, np.zeros(0)
    a, b = lab[i], lab[j]
    lo, hi = np.minimum(a, b).astype(np.int64), np.maximum(a, b).astype(np.int64)
    n = int(hi.max()) + 1
    code = lo * n + hi
    o = np.argsort(code, kind="stable")                      # per pair the largest value: the first of a (-val, code) sort
    code, v = code[o], val[o]
    first = np.concatenate([[True], code[1:] != code[:-1]])
    c = code[first]
    return c // n, c % n, np.maximum.reduceat(v, np.flatnonzero(first))


# ---------------------------------------------------------------------------------------------------------------- merge
def _merge_lists(pv_arr, pr_arr, lo_a, hi_a, s_a, limit: float) -> List[Tuple[float, int, int]]:
    """`merge_tree` for region ids 0..R-1 and unique pairs lo < hi in ascending code order: the same pops, merges and output, with
    lists indexed by region id instead of dicts (a merged-away region has version -1: its entries are stale)."""
    R = len(pv_arr)
    pv, pr = pv_arr.tolist(), pr_arr.tolist()
    lo_l, hi_l, s_l = lo_a.tolist(), hi_a.tolist(), s_a.tolist()
    sad: list = [None] * R
    for a, b, v in zip(lo_l, hi_l, s_l):
        da = sad[a]
        if da is None:
            da = sad[a] = {}
        da[b] = v
        db = sad[b]
        if db is None:
            db = sad[b] = {}
        db[a] = v
    ver = [0] * R
    p0 = np.minimum(pv_arr[lo_a], pv_arr[hi_a]) - s_a
    k0 = np.nonzero(p0 < limit)[0]
    ra, rb = pr_arr[lo_a[k0]], pr_arr[hi_a[k0]]
    heap = list(zip(p0[k0].tolist(), (-s_a[k0]).tolist(), np.minimum(ra, rb).tolist(), np.maximum(ra, rb).tolist(),
                    lo_a[k0].tolist(), hi_a[k0].tolist(), [0] * len(k0), [0] * len(k0)))
    heapq.heapify(heap)
    out: List[Tuple[float, int, int]] = []
    heappush, heappop = heapq.heappush, heapq.heappop
    while heap:
        p, _, _, _, a, b, va, vb = heappop(heap)
        if ver[a] != va or ver[b] != vb or b not in sad[a]:
            continue
        pa, pb = pv[a], pv[b]
        keep, drop = (a, b) if (pa, -pr[a]) >= (pb, -pr[b]) else (b, a)
        out.append((p, keep, drop))
        if pa == pb:
            ra_, rb_ = pr[a], pr[b]
            pr[keep] = ra_ if ra_ < rb_ else rb_
        pk = pv[keep] = pa if pa > pb else pb
        sk, sd = sad[keep], sad[drop]
        del sk[drop]
        del sd[keep]
        for x, v in sd.items():
            old = sk.get(x)
            nv = v if old is None or v > old else old
            sk[x] = nv
            sx = sad[x]
            del sx[drop]
            sx[keep] = nv
        sad[drop] = None
        ver[drop] = -1
        vk = ver[keep] = ver[keep] + 1
        rk = pr[keep]
        for x, v in sk.items():                    # push(min(keep, x), max(keep, x)), inlined
            px = pv[x]
            if keep < x:
                p = (px if px < pk else pk) - v
                if p < limit:
                    rx = pr[x]
                    heappush(heap, (p, -v, rk, rx, keep, x, vk, ver[x]) if rk < rx else (p, -v, rx, rk, keep, x, vk, ver[x]))
            else:
                p = (pk if pk < px else px) - v
                if p < limit:
                    rx = pr[x]
                    heappush(heap, (p, -v, rk, rx, x, keep, ver[x], vk) if rk < rx else (p, -v, rx, rk, x, keep, ver[x], vk))
    return out


def merge_tree(peakv, peakr, lo, hi, s, tau: float) -> List[Tuple[float, int, int]]:
    """The merges of persistence below `tau`, smallest persistence first: [(p, keep, drop)] on the region ids given.
    peakv / peakr: peak value and peak rank per region (the elder of a pair is the higher peak, ties the smaller rank);
    (lo, hi, s): adjacent pairs and their saddle. Pure union-find on region ids, O((R + S) log S)."""
    arrays = not isinstance(peakv, dict) and not isinstance(peakr, dict)
    pv_arr = np.asarray(peakv, float) if not isinstance(peakv, dict) else None
    pr_arr = np.asarray(peakr) if not isinstance(peakr, dict) else None
    lo_a, hi_a, s_a = np.asarray(lo), np.asarray(hi), np.asarray(s, float)
    if (arrays and len(lo_a) and int(lo_a.min()) >= 0 and int(hi_a.max()) < len(pv_arr) and bool((lo_a < hi_a).all())
            and (len(lo_a) == 1 or bool((np.diff(lo_a.astype(np.int64) * len(pv_arr) + hi_a) > 0).all()))):
        return _merge_lists(pv_arr, pr_arr, lo_a, hi_a, s_a, tau - TOL)
    peakv = dict(enumerate(pv_arr.tolist())) if pv_arr is not None else dict(peakv)
    peakr = dict(enumerate(pr_arr.tolist())) if pr_arr is not None else dict(peakr)
    lo_l, hi_l, s_l = lo_a.tolist(), hi_a.tolist(), s_a.tolist()
    sad: Dict[int, Dict[int, float]] = {}
    for a, b, v in zip(lo_l, hi_l, s_l):
        sad.setdefault(a, {})[b] = v
        sad.setdefault(b, {})[a] = v
    ver = {r: 0 for r in peakv}
    heap: list = []
    limit = tau - TOL
    # The heap entries (p, -s, peak ranks, a, b, versions) are pairwise distinct (a pair is pushed once per version of its
    # regions), so the order they pop in does not depend on how the heap was filled: the first fill is vectorised and heapified.
    n_pairs = len(lo_l)
    if (arrays and n_pairs and int(lo_a.min()) >= 0 and int(hi_a.max()) < len(pv_arr) and bool((lo_a < hi_a).all())
            and (n_pairs == 1 or bool((np.diff(lo_a.astype(np.int64) * len(pv_arr) + hi_a) > 0).all()))):
        # unique pairs lo < hi: the dict above holds exactly these (lo, hi, s)
        p0 = np.minimum(pv_arr[lo_a], pv_arr[hi_a]) - s_a
        k0 = np.nonzero(p0 < limit)[0]
        ra, rb = pr_arr[lo_a[k0]], pr_arr[hi_a[k0]]
        heap = list(zip(p0[k0].tolist(), (-s_a[k0]).tolist(), np.minimum(ra, rb).tolist(), np.maximum(ra, rb).tolist(),
                        lo_a[k0].tolist(), hi_a[k0].tolist(), [0] * len(k0), [0] * len(k0)))
        heapq.heapify(heap)
    else:
        for a in sad:
            for b in sad[a]:
                if a < b:
                    p = min(peakv[a], peakv[b]) - sad[a][b]
                    if p < limit:
                        ka, kb = (a, b) if peakr[a] < peakr[b] else (b, a)
                        heapq.heappush(heap, (p, -sad[a][b], peakr[ka], peakr[kb], a, b, ver[a], ver[b]))
    out: List[Tuple[float, int, int]] = []
    heappush, heappop = heapq.heappush, heapq.heappop
    while heap:
        p, _, _, _, a, b, va, vb = heappop(heap)
        if ver.get(a, -1) != va or ver.get(b, -1) != vb or b not in sad.get(a, ()):       # versions are >= 0: -1 = merged away
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
        vk = ver[keep] = ver[keep] + 1
        pk, rk = peakv[keep], peakr[keep]
        for x, v in sk.items():                    # push(min(keep, x), max(keep, x)), inlined
            px = peakv[x]
            if keep < x:
                p = (px if px < pk else pk) - v
                if p < limit:
                    rx = peakr[x]
                    heappush(heap, (p, -v, rk, rx, keep, x, vk, ver[x]) if rk < rx else (p, -v, rx, rk, keep, x, vk, ver[x]))
            else:
                p = (pk if pk < px else px) - v
                if p < limit:
                    rx = peakr[x]
                    heappush(heap, (p, -v, rk, rx, x, keep, ver[x], vk) if rk < rx else (p, -v, rx, rk, x, keep, ver[x], vk))
    return out


def survivors(n: int, tree, tau: Optional[float] = None) -> np.ndarray:
    """final region id (one of the original ids) of every original region after the merges of `tree` below `tau`
    (all of them when None). The p of a tree never decreases: the merges below tau are a prefix."""
    parent = np.arange(n)
    for p, keep, drop in tree:
        if tau is not None and not p < tau - TOL:
            break
        parent[drop] = keep
    while True:
        nxt = parent[parent]
        if np.array_equal(nxt, parent):
            return parent
        parent = nxt


# ---------------------------------------------------------------------------------------------------------------- anchors
def anchors(sp: Space, lab: np.ndarray, f: np.ndarray, K: int) -> np.ndarray:
    """cell index of the anchor of each of the K regions: maximal f; a plateau resolves to the plateau cell nearest the
    plateau centroid, ties by (row, col) (cells are row-major, so a lower index is a lower (row, col))"""
    fmax = np.full(K, -1.0)
    np.maximum.at(fmax, lab, f)
    top = np.nonzero(f >= fmax[lab] - 1e-9)[0]
    t = lab[top]
    cnt = np.bincount(t, minlength=K)
    cy = np.bincount(t, weights=sp.ys[top], minlength=K) / np.maximum(cnt, 1)
    cx = np.bincount(t, weights=sp.xs[top], minlength=K) / np.maximum(cnt, 1)
    d2 = (sp.ys[top] - cy[t]) ** 2 + (sp.xs[top] - cx[t]) ** 2
    o = np.lexsort((top, d2, t))
    t, top = t[o], top[o]
    first = np.concatenate([[True], t[1:] != t[:-1]])
    out = np.full(K, -1, np.int64)
    out[t[first]] = top[first]
    return out


def centres(sp: Space, lab: np.ndarray, f: np.ndarray, K: int) -> np.ndarray:
    """cell index of the centre of each of the K charts: the chart cell nearest the chart's centroid (ties: more room, then
    (row, col)). The net point that a chart was cut around sits where the cut put it, often at the side of a small chart; the
    centre is what stands for the chart."""
    cnt = np.bincount(lab, minlength=K)
    cy = np.bincount(lab, weights=sp.ys, minlength=K) / np.maximum(cnt, 1)
    cx = np.bincount(lab, weights=sp.xs, minlength=K) / np.maximum(cnt, 1)
    d2 = np.round((sp.ys - cy[lab]) ** 2 + (sp.xs - cx[lab]) ** 2, 9)
    if len(lab) == 0 or int(lab.max()) >= K or (cnt == 0).any():
        o = np.lexsort((np.arange(sp.N), -f, d2, lab))
        first = np.concatenate([[True], lab[o][1:] != lab[o][:-1]])
        return o[first].astype(np.int64)
    # the first cell of every chart in the order (chart, d2, -f, index): the cells of least d2 (cell indices ascending within a
    # chart), then the first of those by (-f, index). The same cell as the full lexsort, which sorts the whole canvas.
    go = group_order(lab, K)
    cut = np.concatenate([[0], np.cumsum(cnt)])
    d2o = d2[go]
    cand = go[d2o == np.repeat(np.minimum.reduceat(d2o, cut[:-1]), cnt)]
    o = cand[np.lexsort((cand, -f[cand], lab[cand]))]
    lo = lab[o]
    first = np.concatenate([[True], lo[1:] != lo[:-1]])
    return o[first].astype(np.int64)


# ---------------------------------------------------------------------------------------------------------------- partition
@dataclass
class Partition:
    sp: Space
    f: np.ndarray               # B per cell
    full: np.ndarray            # chart (cell of the cover) index per cell of F, charts in priority order of their anchors
    K: int                      # charts
    anchor: np.ndarray          # cell index of each chart's anchor
    raw: int                    # raw basins before merging
    merges: int
    region: np.ndarray          # persistence region (index 0..R-1, rank order of the peaks) of each chart
    regions: int
    seed: np.ndarray            # cell index of the net point each chart was cut around (== anchor without a radius)
    edges: Optional[tuple] = None   # (row cell, column cell, length) of the lattice edges inside F (`edges_full`), with a radius
    edge_cuts: Optional[tuple] = None


def edges_full(sp: Space, h: float, pad: Optional[tuple] = None) -> Tuple[np.ndarray, np.ndarray, np.ndarray, Tuple[int, int, int, int, int]]:
    """The 8-neighbour lattice edges inside F as (row cell, column cell, length m) and the offsets of the four direction blocks
    (E, S, SE, SW; the last two are the diagonals): a diagonal needs both orthogonal cells in F, as the engine's route does.
    One flat gather per direction on the padded cell-index image (a neighbour outside the canvas reads the pad, -1); `pad`:
    `sp.padded()` when the caller has it already."""
    P, pos, Wp = sp.padded() if pad is None else pad
    R, C, cuts = [], [], [0]
    dirs = ((0, 1), (1, 0), (1, 1), (1, -1))
    for dy, dx in dirs:
        nbv = P[pos + (dy * Wp + dx)]
        ok = nbv >= 0
        if dy and dx:
            ok &= (P[pos + dx] >= 0) & (P[pos + dy * Wp] >= 0)
        a = np.flatnonzero(ok)
        R.append(a); C.append(nbv[a])
        cuts.append(cuts[-1] + len(a))
    L = np.empty(cuts[-1])
    for k, (dy, dx) in enumerate(dirs):
        L[cuts[k]:cuts[k + 1]] = math.hypot(dy, dx) * h
    return np.concatenate(R), np.concatenate(C), L, tuple(cuts)


def edges_within(sp: Space, edges, cuts, region: np.ndarray, cells: Optional[np.ndarray] = None):
    """`edges_full` restricted to one region: an edge joins cells of one region only, and a diagonal also needs both orthogonal
    cells in that region (a cell of another region beside the step is across a pass: the step would be a way round it). With
    `cells` (bool per cell) only the edges whose row cell is in `cells`. Order as in `edges_full`."""
    R, C, L = edges
    k = np.arange(len(R)) if cells is None else np.nonzero(cells[R])[0]
    keep = region[R[k]] == region[C[k]]
    k = k[keep]
    a, b = R[k], C[k]
    diag = k >= cuts[2]
    if diag.any():
        d = np.nonzero(diag)[0]
        o1 = sp.idx[sp.ys[a[d]], sp.xs[b[d]]]
        o2 = sp.idx[sp.ys[b[d]], sp.xs[a[d]]]
        ok = np.ones(len(k), bool)
        ok[d] = (region[o1] == region[a[d]]) & (region[o2] == region[a[d]])
        k = k[ok]
    return R[k], C[k], L[k]


def edges8(sp: Space, h: float, region: Optional[np.ndarray] = None) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """8-neighbour lattice edges inside F as (row cell, column cell, length m); a diagonal needs both orthogonal cells in F,
    as the engine's route does, so geodesic distances here are the distances routes have. With `region`, an edge joins cells of
    one region only, and a diagonal also needs both orthogonal cells in that region (a cell of another region beside the
    step is across a pass: the step would be a way round it)."""
    R, C, L, cuts = edges_full(sp, h)
    if region is None:
        return R, C, L
    return edges_within(sp, (R, C, L), cuts, region)


def subdivide(sp: Space, f: np.ndarray, region: np.ndarray, radius: float, h: float,
              cache: Optional[dict] = None, edges=None, cuts=None) -> Tuple[np.ndarray, np.ndarray]:
    """(chart per cell, net point cell per chart): every persistence region cut into geodesic Voronoi cells of a greedy net.
    Cells are taken in the order of room (B quantised to the lattice step, ties in scan order: translation invariant, and
    stable when the canvas grows, which keeps the relative scan order); a
    cell with no net point within geodesic `radius` INSIDE ITS REGION becomes one. Hence every cell of a region is within
    `radius` of a net point (covering), net points of one region are further than `radius` apart (packing), the first net
    point of a region is a cell of maximal room, and no chart crosses a pass. Charts are the Voronoi cells of the net points
    (geodesic in the region): connected, and every cell reaches its net point without leaving its chart. The anchor of a
    chart is its centre (`centres`), not the net point. `cache` (a dict the caller keeps
    between calls) serves a region whose cells and room are unchanged from the last call; it holds the last call's regions only.
    `edges`, `cuts`: `edges_full(sp, h)` when the caller has it already."""
    N = sp.N
    nR = int(region.max()) + 1
    co = group_order(region, nR)                                 # cells of a region, ascending cell index = scan order
    ccut = np.concatenate([[0], np.cumsum(np.bincount(region, minlength=nR))])
    q = np.rint(f / h).astype(np.int64)
    lab = np.empty(N, np.int32)
    seeds: List[int] = []
    used: dict = {}
    keys: List[Optional[bytes]] = [None] * nR
    hits: List[Optional[tuple]] = [None] * nR
    if cache is not None:                                        # the content key of every region, in one pass
        yo, xo = sp.ys[co], sp.xs[co]
        starts = ccut[:-1]
        Y = (yo - np.repeat(np.minimum.reduceat(yo, starts), np.diff(ccut))).astype(np.int32)
        X = (xo - np.repeat(np.minimum.reduceat(xo, starts), np.diff(ccut))).astype(np.int32)
        Q = q[co].astype(np.int32)
        tail = np.array([radius, h], np.float64).tobytes()
        for r in range(nR):
            a0, a1 = int(ccut[r]), int(ccut[r + 1])
            hk = hashlib.blake2b(digest_size=16)
            hk.update(Y[a0:a1]); hk.update(X[a0:a1]); hk.update(Q[a0:a1])
            hk.update(tail)
            keys[r] = hk.digest()
            hits[r] = cache.get(keys[r])
    miss = np.array([hit is None for hit in hits], bool)
    if miss.any():                                               # edges of the regions to cut, grouped by region
        if edges is None:
            *edges, cuts = edges_full(sp, h)
        R, C, L = edges_within(sp, edges, cuts, region, miss[region])
        eo = group_order(region[R], nR)
        R, C, L = R[eo], C[eo], L[eo]
        ecut = np.concatenate([[0], np.cumsum(np.bincount(region[R], minlength=nR))])
    for r in range(nR):                                          # a region at a time: distances never leave it
        cells = co[ccut[r]:ccut[r + 1]]
        n = len(cells)
        ck, hit = keys[r], hits[r]
        if hit is not None:
            used[ck] = hit
            lab[cells] = hit[0] + len(seeds)
            seeds.extend(int(cells[i]) for i in hit[1])
            continue
        e0, e1 = ecut[r], ecut[r + 1]
        lr, lc = np.searchsorted(cells, R[e0:e1]), np.searchsorted(cells, C[e0:e1])
        G = coo_matrix((np.concatenate([L[e0:e1], L[e0:e1]]), (np.concatenate([lr, lc]), np.concatenate([lc, lr]))),
                       shape=(n, n)).tocsr()
        order = np.argsort(-q[cells], kind="stable")             # room first, ties in scan order
        covered = np.zeros(n, bool)
        best_d = np.full(n, np.inf)
        best_s = np.full(n, -1, np.int64)
        base = len(seeds)
        ys, xs = sp.ys[cells], sp.xs[cells]
        y0, x0 = int(ys.min()), int(xs.min())
        img = -np.ones((int(ys.max()) - y0 + 1, int(xs.max()) - x0 + 1), np.int64)
        img[ys - y0, xs - x0] = np.arange(n)
        rad = int(math.ceil(radius / h)) + 1                     # a search never leaves the box of this half side
        Gp, Gx, Gd = G.indptr, G.indices, G.data
        place = np.full(n, -1, np.int64)                         # cell of the region -> its place in the box being searched
        pos = 0
        while True:
            while pos < n and covered[order[pos]]:
                pos += 1
            if pos == n:
                break
            s = int(order[pos])
            sy, sx = int(ys[s]) - y0, int(xs[s]) - x0
            win = img[max(0, sy - rad):sy + rad + 1, max(0, sx - rad):sx + rad + 1]
            loc = win[win >= 0]                                  # cells of the box: the only ones within `radius`
            # the subgraph of G on the box cells (loc is ascending, so the places keep the cell order): the rows of `loc`, the
            # entries whose column is in the box. Its distances do not depend on the order of the entries within a row.
            m = len(loc)
            place[loc] = np.arange(m)
            st, ln = Gp[loc], Gp[loc + 1] - Gp[loc]
            row = np.repeat(np.arange(m), ln)
            flat = np.arange(len(row)) - np.repeat(np.cumsum(ln) - ln, ln) + np.repeat(st, ln)
            col = place[Gx[flat]]
            kept = col >= 0
            sub = csr_matrix((Gd[flat][kept], col[kept].astype(np.int32),
                              np.concatenate([[0], np.cumsum(np.bincount(row[kept], minlength=m))]).astype(np.int32)), shape=(m, m))
            d = dijkstra(sub, directed=False, indices=int(place[s]), limit=radius + TOL)
            place[loc] = -1
            hit = np.isfinite(d)
            reach = loc[hit]
            covered[reach] = True
            dd = d[hit]
            better = dd < best_d[reach] - TOL                    # nearer seed wins, a tie keeps the earlier (higher room) one
            best_d[reach[better]] = dd[better]
            best_s[reach[better]] = len(seeds) - base
            seeds.append(int(cells[s]))
        lab[cells] = best_s + base
        if ck is not None:
            used[ck] = (best_s.astype(np.int32), [int(np.searchsorted(cells, c)) for c in seeds[base:]])
    if cache is not None:
        cache.clear(); cache.update(used)
    return lab, np.array(seeds, np.int64)


def partition(F: np.ndarray, h: float, tau: float, radius: Optional[float] = None, cache: Optional[dict] = None) -> Partition:
    """The persistence regions of F at `tau` (merge pairs of persistence below tau), and, with `radius`, each cut into charts
    of geodesic radius <= radius (`subdivide`). Without `radius` every region is one chart."""
    sp = Space.of(F)
    if sp.N == 0:
        z = np.zeros(0, np.int64)
        return Partition(sp, np.zeros(0), np.zeros(0, np.int32), 0, z, 0, 0, z, 0, z)
    f = distance(sp, h)
    rank = ranks(f)
    nb = sp.neighbours()
    bid, roots = raw_basins(sp, rank, nb)
    R = len(roots)
    i, j, val = crossings(sp, bid, f, nb)
    lo, hi, s = saddles(bid, i, j, val)
    tree = merge_tree(f[roots], rank[roots], lo, hi, s, tau)
    surv = survivors(R, tree)
    keep = np.unique(surv)                                    # ascending raw id = ascending peak rank
    new = np.empty(R, np.int64)
    new[keep] = np.arange(len(keep))
    reg = new[surv[bid]].astype(np.int32)
    if radius is None:
        a = anchors(sp, reg, f, len(keep))
        return Partition(sp, f, reg, len(keep), a, R, len(tree), np.arange(len(keep)), len(keep), a)
    *edges, cuts = edges_full(sp, h)
    full, seeds = subdivide(sp, f, reg, radius, h, cache, edges, cuts)
    return Partition(sp, f, full, len(seeds), centres(sp, full, f, len(seeds)), R, len(tree), reg[seeds].astype(np.int64),
                     len(keep), seeds, tuple(edges), cuts)


# ---------------------------------------------------------------------------------------------------------------- incremental
def _next_uncovered(covered: np.ndarray, order: np.ndarray, pos: int, n: int) -> int:
    """the first position >= pos of `order` whose cell is not covered (n when none); looks at growing chunks"""
    step = 64
    while pos < n:
        seg = order[pos:pos + step]
        nz = np.flatnonzero(~covered[seg])
        if len(nz):
            return pos + int(nz[0])
        pos += len(seg)
        step = min(step * 4, 1 << 16)
    return n


_BOX_MIN = 6000        # a region of more cells than this is searched in a box around each net point (see `_cut_regions`)


def _cut_regions(sp: Space, q: np.ndarray, region: np.ndarray, rids: np.ndarray, radius: float, h: float, pad: Optional[tuple] = None):
    """The greedy net of `subdivide` for the regions `rids` only: (cells of those regions grouped by region and ascending within,
    chart (index of the net point within its region) per entry of those cells, net point cells per region in net order).
    The same algorithm as `subdivide` (room order, geodesic ball of the region graph, nearer net point wins, a tie keeps the
    earlier one); the graph of all the regions is built in one pass from the cell-index image instead of from `edges_full`, and
    a region of at most `_BOX_MIN` cells is searched on its own graph without cutting a box out of it (the distances inside
    the radius are the same: the box holds every cell within the radius)."""
    N, H, W = sp.N, sp.H, sp.W
    nR = int(region.max()) + 1
    flag = np.zeros(nR, bool); flag[rids] = True
    mcells = np.flatnonzero(flag[region])
    rg = region[mcells]
    o = group_order(rg, nR)
    mc, rgs = mcells[o], rg[o]
    Nm = len(mc)
    rcut = np.concatenate([[0], np.cumsum(np.bincount(rgs, minlength=nR)[rids])])
    loc = np.full(N, -1, np.int32); loc[mc] = np.arange(Nm, dtype=np.int32)
    ys, xs = sp.ys[mc], sp.xs[mc]
    P, pos, Wp = sp.padded() if pad is None else pad
    pm = pos[mc]
    Ra, Ca, La = [], [], []
    for dy, dx in ((0, 1), (1, 0), (1, 1), (1, -1)):       # one half of the 8-neighbour edges inside the region (as `edges_within`)
        nbv = P[pm + (dy * Wp + dx)]
        a = np.flatnonzero(nbv >= 0)
        b = nbv[a]
        k = region[b] == rgs[a]
        a, b = a[k], b[k]
        if dy and dx:                                      # a diagonal also needs both orthogonal cells in the region
            o1 = P[pm[a] + dx]; o2 = P[pm[a] + dy * Wp]
            k = (o1 >= 0) & (o2 >= 0)
            a, b, o1, o2 = a[k], b[k], o1[k], o2[k]
            k = (region[o1] == rgs[a]) & (region[o2] == rgs[a])
            a, b = a[k], b[k]
        Ra.append(a); Ca.append(loc[b]); La.append(np.full(len(a), math.hypot(dy, dx) * h))
    rows = np.concatenate(Ra + Ca); cols = np.concatenate(Ca + Ra); dat = np.concatenate(La + La)
    eo = np.argsort(rows, kind="stable")
    cols, dat = cols[eo].astype(np.int32), dat[eo]
    indptr = np.concatenate([[0], np.cumsum(np.bincount(rows, minlength=Nm))])
    mlab = np.empty(Nm, np.int32)
    seed_cells: List[np.ndarray] = []
    rad = int(math.ceil(radius / h)) + 1                       # a search never leaves the box of this half side
    for t in range(len(rids)):
        c0, c1 = int(rcut[t]), int(rcut[t + 1])
        n = c1 - c0
        cells = mc[c0:c1]
        e0, e1 = int(indptr[c0]), int(indptr[c1])
        Gp, Gx, Gd = (indptr[c0:c1 + 1] - e0).astype(np.int32), cols[e0:e1] - np.int32(c0), dat[e0:e1]
        G = csr_matrix((Gd, Gx, Gp), shape=(n, n))
        order = np.argsort(-q[cells], kind="stable")             # room first, ties in scan order
        covered = np.zeros(n, bool)
        best_d = np.full(n, np.inf)
        best_s = np.full(n, -1, np.int64)
        sl: List[int] = []
        if n > _BOX_MIN:
            ysr, xsr = ys[c0:c1], xs[c0:c1]
            y0, x0 = int(ysr.min()), int(xsr.min())
            img = -np.ones((int(ysr.max()) - y0 + 1, int(xsr.max()) - x0 + 1), np.int64)
            img[ysr - y0, xsr - x0] = np.arange(n)
            place = np.full(n, -1, np.int64)
        pos = 0
        while True:
            pos = _next_uncovered(covered, order, pos, n)
            if pos == n:
                break
            s = int(order[pos])
            if n <= _BOX_MIN:
                d = dijkstra(G, directed=True, indices=s, limit=radius + TOL)
                reach = np.flatnonzero(np.isfinite(d))
                dd = d[reach]
            else:
                sy, sx = int(ysr[s]) - y0, int(xsr[s]) - x0
                win = img[max(0, sy - rad):sy + rad + 1, max(0, sx - rad):sx + rad + 1]
                lc = win[win >= 0]
                m = len(lc)
                place[lc] = np.arange(m)
                st, ln = Gp[lc], Gp[lc + 1] - Gp[lc]
                row = np.repeat(np.arange(m), ln)
                flat = np.arange(len(row)) - np.repeat(np.cumsum(ln) - ln, ln) + np.repeat(st, ln)
                col = place[Gx[flat]]
                kept = col >= 0
                sub = csr_matrix((Gd[flat][kept], col[kept].astype(np.int32),
                                  np.concatenate([[0], np.cumsum(np.bincount(row[kept], minlength=m))]).astype(np.int32)), shape=(m, m))
                d = dijkstra(sub, directed=False, indices=int(place[s]), limit=radius + TOL)
                place[lc] = -1
                hit = np.isfinite(d)
                reach = lc[hit]
                dd = d[hit]
            covered[reach] = True
            better = dd < best_d[reach] - TOL                    # nearer seed wins, a tie keeps the earlier (higher room) one
            best_d[reach[better]] = dd[better]
            best_s[reach[better]] = len(sl)
            sl.append(int(cells[s]))
        mlab[c0:c1] = best_s
        seed_cells.append(np.array(sl, np.int64))
    return mc, mlab, seed_cells


@dataclass
class _State:
    """what a solve leaves for the next one (all per cell of the solve's own Space, so nothing is canvas sized)"""
    sp: Space
    f: np.ndarray
    full: np.ndarray            # chart per cell
    reg: np.ndarray             # persistence region per cell
    q: np.ndarray               # room in lattice steps per cell
    seeds: np.ndarray           # net point cell per chart, regions ascending and net order within
    nR: int


class PartitionSolver:
    """`partition(F, h, tau, radius)` kept between calls: the same Partition, cost that follows what changed.

    `update(F, shift)` returns exactly the Partition `partition(F, h, tau, radius)` returns. `shift` = (rows, cols) the canvas of
    the previous call was moved by inside this one (an old cell (y, x) is (y + sy, x + sx) now); a wrong or missing shift costs
    time only: the previous solve is laid over the new canvas as ANOTHER valid problem instance and compared with F cell by
    cell, so nothing here depends on the shift being right. The translation invariance of everything below (the rank order
    (-f, row, col) and the scan order are) is what makes a laid-over previous solve a solution of that instance."""

    def __init__(self, h: float, tau: float, radius: float):
        self.h, self.tau, self.radius = h, tau, radius
        self._st: Optional[_State] = None

    def reset(self):
        self._st = None

    def update(self, F: np.ndarray, shift: Tuple[int, int] = (0, 0)) -> Partition:
        st, self._st = self._st, None                      # an exception leaves no state behind
        if F.dtype != bool:
            F = F != 0
        part, new = self._solve(F, shift, st)
        self._st = new
        return part

    # -------------------------------------------------------------------------------------------------------- helpers
    @staticmethod
    def _lay(st: Optional[_State], sp: Space, shift: Tuple[int, int]):
        """(image of previous cell indices on this canvas (-1: not a previous cell), (sy, sx)) or (None, None)"""
        if st is None:
            return None, None
        H0, W0 = st.sp.H, st.sp.W
        for sy, sx in (tuple(shift), (0, 0)):
            if sy >= 0 and sx >= 0 and sy + H0 <= sp.H and sx + W0 <= sp.W:
                if (sy, sx) == (0, 0) and (H0, W0) == (sp.H, sp.W):
                    return st.sp.idx, (0, 0)
                oi = -np.ones((sp.H, sp.W), np.int32)
                oi[sy:sy + H0, sx:sx + W0] = st.sp.idx
                return oi, (sy, sx)
        return None, None

    def _distance(self, F, sp: Space, st: _State, oi: np.ndarray, off, oc: np.ndarray) -> np.ndarray:
        """B per cell, recomputed only where it can have changed. B(x) is the distance to the nearest non-F cell. With D the cells
        where F differs from the previous F and B0 the previous B: if no cell of D is nearer to x than B0(x), the nearest
        non-F cell of the previous F is still non-F and no cell that is non-F now (a cell of D, or a previous non-F cell) is
        nearer, so B(x) = B0(x). Hence only cells within B0 of D change, and B0 <= g outside the box of D grown by g for the g
        found below. The transform of that box (plus a margin) is exact where its nearest non-F cell is nearer than the margin
        (checked, the margin grows until it holds); scipy's transform of a cell depends on the integer geometry only."""
        h = self.h
        H, W = sp.H, sp.W
        D = (oi >= 0) != F
        ry = np.flatnonzero(D.any(1))
        f = np.where(oc >= 0, st.f[np.maximum(oc, 0)], 0.0)
        if len(ry) == 0:
            return f
        cx = np.flatnonzero(D.any(0))
        y0, y1, x0, x1 = int(ry[0]), int(ry[-1]), int(cx[0]), int(cx[-1])
        oy, ox = st.sp.ys + off[0], st.sp.xs + off[1]
        fo = st.f
        g = 0
        for _ in range(6):
            out = (oy < y0 - g) | (oy > y1 + g) | (ox < x0 - g) | (ox > x1 + g)
            need = int(math.ceil(float(fo[out].max()) / h + 1e-6)) if out.any() else 0
            if need <= g:
                break
            g = need
        else:
            g = int(math.ceil(float(fo.max()) / h + 1e-6))
        wy0, wy1, wx0, wx1 = max(0, y0 - g), min(H - 1, y1 + g), max(0, x0 - g), min(W - 1, x1 + g)
        pad = g + 2
        tries = 0
        while True:
            ey0, ey1, ex0, ex1 = max(0, wy0 - pad), min(H - 1, wy1 + pad), max(0, wx0 - pad), min(W - 1, wx1 + pad)
            sub = np.zeros((ey1 - ey0 + 3, ex1 - ex0 + 3), bool)
            sub[1:-1, 1:-1] = F[ey0:ey1 + 1, ex0:ex1 + 1]
            dd = ndimage.distance_transform_edt(sub)
            inner = dd[wy0 - ey0 + 1:wy1 - ey0 + 2, wx0 - ex0 + 1:wx1 - ex0 + 2]
            # a margin ring that is not the canvas edge holds invented non-F cells: where the nearest non-F cell found is nearer
            # than the ring, every cell outside the margin is at least as far as the ring, so the value is the true one
            inf = float(max(H, W) + 4)
            yy = np.arange(wy0, wy1 + 1)
            xx = np.arange(wx0, wx1 + 1)
            rpd = np.minimum(np.full(len(yy), inf) if ey0 == 0 else yy - ey0 + 1, np.full(len(yy), inf) if ey1 == H - 1 else ey1 - yy + 1)
            cpd = np.minimum(np.full(len(xx), inf) if ex0 == 0 else xx - ex0 + 1, np.full(len(xx), inf) if ex1 == W - 1 else ex1 - xx + 1)
            if bool((inner < np.minimum(rpd[:, None], cpd[None, :])).all()):
                break
            pad = pad * 2 if tries == 0 else max(H, W)               # a second miss: the whole canvas
            tries += 1
        i0, i1 = int(np.searchsorted(sp.ys, wy0)), int(np.searchsorted(sp.ys, wy1, side="right"))
        sel = i0 + np.flatnonzero((sp.xs[i0:i1] >= wx0) & (sp.xs[i0:i1] <= wx1))
        f[sel] = inner[sp.ys[sel] - wy0, sp.xs[sel] - wx0] * h
        return f

    def _solve(self, F: np.ndarray, shift, st: Optional[_State]):
        h, tau, radius = self.h, self.tau, self.radius
        sp = Space.of(F)
        if sp.N == 0:
            z = np.zeros(0, np.int64)
            return Partition(sp, np.zeros(0), np.zeros(0, np.int32), 0, z, 0, 0, z, 0, z), None
        oi, off = self._lay(st, sp, shift)
        if oi is None:
            st = None
            f = distance(sp, h)
            oc = None
        else:
            oc = oi[sp.ys, sp.xs]
            f = self._distance(F, sp, st, oi, off, oc)
        pad = sp.padded()
        P, pos, Wp = pad
        nb = (P[pos - Wp], P[pos + 1], P[pos + Wp], P[pos - 1])
        bid, roots = raw_basins_f(sp, f, pad)
        R = len(roots)
        i, j, val = crossings(sp, bid, f, nb)
        lo, hi, s = saddles(bid, i, j, val)
        # the ranks of the peaks are only ever compared: their position among the roots (rank order) orders them the same way
        tree = merge_tree(f[roots], np.arange(R), lo, hi, s, tau)
        surv = survivors(R, tree)
        keep = np.unique(surv)
        new = np.empty(R, np.int64)
        new[keep] = np.arange(len(keep))
        reg = new[surv[bid]].astype(np.int32)
        *edges, cuts = edges_full(sp, h, pad)
        q = np.rint(f / h).astype(np.int64)
        full, seeds = self._subdivide(sp, q, reg, len(keep), st, oc, off, pad)
        part = Partition(sp, f, full, len(seeds), centres(sp, full, f, len(seeds)), R, len(tree), reg[seeds].astype(np.int64),
                         len(keep), seeds, tuple(edges), cuts)
        return part, _State(sp, f, full, reg, q.astype(np.int32), seeds, len(keep))

    def _subdivide(self, sp: Space, q: np.ndarray, reg: np.ndarray, nR: int, st: Optional[_State], oc, off, pad=None):
        """`subdivide` with the regions that did not change served from the previous solve. A region is served when it is the
        same set of cells as a region of the previous solve (every cell was a previous cell of one previous region, equally many)
        with the same room per cell: the greedy net of a region is a function of its cell set (graph, scan order) and its room,
        translation invariant, so its net points and the charts are the previous ones."""
        N = sp.N
        h, radius = self.h, self.radius
        hit = np.zeros(nR, bool)
        if st is not None:
            rep = np.empty(nR, np.int64)
            rep[reg] = np.arange(N)                                # a cell of every region (any one: all are compared below)
            ocf = oc[rep]
            ro = np.where(ocf >= 0, st.reg[np.maximum(ocf, 0)], -1).astype(np.int64)      # previous region of each region
            okc = (oc >= 0) & (st.reg[np.maximum(oc, 0)] == ro[reg]) & (st.q[np.maximum(oc, 0)] == q)
            nbad = np.bincount(reg[~okc], minlength=nR)
            cnt = np.bincount(reg, minlength=nR)
            cnt_old = np.bincount(st.reg, minlength=st.nR)
            hit = (nbad == 0) & (ro >= 0) & (cnt == cnt_old[np.maximum(ro, 0)])
        miss_ids = np.flatnonzero(~hit)
        nseed = np.zeros(nR, np.int64)
        if st is not None:
            nS_old = np.bincount(st.reg[st.seeds], minlength=st.nR)
            base_old = np.concatenate([[0], np.cumsum(nS_old)])
            nseed[hit] = nS_old[ro[hit]]
        if len(miss_ids):
            mc, mlab, sc = _cut_regions(sp, q, reg, miss_ids, radius, h, pad)
            nseed[miss_ids] = [len(a) for a in sc]
        base = np.concatenate([[0], np.cumsum(nseed)])
        lab = np.empty(N, np.int32)
        seeds = np.empty(int(base[-1]), np.int64)
        if len(miss_ids):
            lab[mc] = mlab + base[reg[mc]].astype(np.int32)
            for r, a in zip(miss_ids.tolist(), sc):
                seeds[base[r]:base[r] + len(a)] = a
        if hit.any():
            hc = np.flatnonzero(hit[reg])                         # cells of the served regions
            lab[hc] = (st.full[oc[hc]] - base_old[ro[reg[hc]]] + base[reg[hc]]).astype(np.int32)
            ro_new = np.full(st.nR, -1, np.int64)
            ro_new[ro[hit]] = np.flatnonzero(hit)
            sr = st.reg[st.seeds]
            k = np.flatnonzero(ro_new[sr] >= 0)                   # previous net points of the served regions
            pos = base[ro_new[sr[k]]] + (k - base_old[sr[k]])
            seeds[pos] = sp.idx[st.sp.ys[st.seeds[k]] + off[0], st.sp.xs[st.seeds[k]] + off[1]]
        return lab, seeds


# ---------------------------------------------------------------------------------------------------------------- interface
@dataclass
class Arcs:
    a: np.ndarray               # lower region index of each arc
    b: np.ndarray
    p: np.ndarray               # portal cell in a
    q: np.ndarray               # portal cell in b
    s: np.ndarray               # crossing value of the portal (max over the arc) of f
    g: np.ndarray               # max over the arc's crossings of min(g) for a second field g (physical clearance), else s
    size: np.ndarray            # unit edges of the arc


def arcs(sp: Space, full: np.ndarray, f: np.ndarray, g: Optional[np.ndarray] = None) -> Arcs:
    """Arcs of the interface between regions (see module doc), vectorised. Corner (vy, vx) of the lattice has id
    vy * (W + 1) + vx; a unit edge between horizontally adjacent cells (y, x), (y, x+1) runs (y, x+1)-(y+1, x+1), between
    vertically adjacent cells (y, x), (y+1, x) it runs (y+1, x)-(y+1, x+1)."""
    W1 = sp.W + 1
    Nn, E, S, Wn = sp.neighbours()
    y, x = sp.ys.astype(np.int64), sp.xs.astype(np.int64)
    ei, ej, ev0, ev1 = [], [], [], []
    for nb, v0, v1 in ((E, y * W1 + x + 1, (y + 1) * W1 + x + 1), (S, (y + 1) * W1 + x, (y + 1) * W1 + x + 1)):
        i = np.nonzero(nb >= 0)[0]
        j = nb[i].astype(np.int64)
        k = full[i] != full[j]
        ei.append(i[k]); ej.append(j[k]); ev0.append(v0[i[k]]); ev1.append(v1[i[k]])
    ei, ej, ev0, ev1 = (np.concatenate(v) for v in (ei, ej, ev0, ev1))
    nE = len(ei)
    if nE == 0:
        z = np.zeros(0, np.int64)
        return Arcs(z, z, z, z, np.zeros(0), np.zeros(0), z)
    nV = (sp.H + 1) * W1
    # boundary edges: a cell of F next to a cell that is not
    bv = []
    for nb, v0, v1 in ((Nn, y * W1 + x, y * W1 + x + 1), (E, y * W1 + x + 1, (y + 1) * W1 + x + 1),
                       (S, (y + 1) * W1 + x, (y + 1) * W1 + x + 1), (Wn, y * W1 + x, (y + 1) * W1 + x)):
        m = nb < 0
        bv.append(v0[m]); bv.append(v1[m])
    bnd = np.bincount(np.concatenate(bv), minlength=nV)
    ifc = np.bincount(np.concatenate([ev0, ev1]), minlength=nV)
    # an arc continues through a corner of map degree exactly 2 that is made of two interface edges
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
    res = Arcs(lo[best].astype(np.int64), hi[best].astype(np.int64), p[best].astype(np.int64), q[best].astype(np.int64),
               val[best], gv, size)
    k = np.lexsort((sp.xs[res.q], sp.ys[res.q], sp.xs[res.p], sp.ys[res.p], res.b, res.a))
    return Arcs(res.a[k], res.b[k], res.p[k], res.q[k], res.s[k], res.g[k], res.size[k])


# ---------------------------------------------------------------------------------------------------------------- levels
def levels(clear: np.ndarray, anchor_rc: np.ndarray, la: np.ndarray, lb: np.ndarray, half_width: np.ndarray,
           tau_base: float) -> List[np.ndarray]:
    """Parent of every base region at levels 1.., tau_j = tau_base * 2^j: the elder base region of the level region it falls
    in. A pure function of what a stored chart and a stored link carry: the chart's peak B (`clear`), its anchor cell (tie
    order) and the link's saddle B (`half_width`); the pair saddle is the largest of its links. The depth is dynamic: levels
    follow until every connected component is ONE region (the largest persistence of the merge sequence is the depth: no
    scale is assumed)."""
    K = len(clear)
    if K == 0:
        return []
    rc = np.asarray(anchor_rc)
    order = np.lexsort((rc[:, 1], rc[:, 0], -np.asarray(clear, float)))
    rank = np.empty(K, np.int64); rank[order] = np.arange(K)
    if len(la):
        lo, hi = np.minimum(la, lb).astype(np.int64), np.maximum(la, lb).astype(np.int64)
        code = lo * K + hi
        o = np.lexsort((-np.asarray(half_width, float), code))
        code, hw = code[o], np.asarray(half_width, float)[o]
        first = np.concatenate([[True], code[1:] != code[:-1]])
        lo, hi, s = code[first] // K, code[first] % K, hw[first]
    else:
        lo = hi = np.zeros(0, np.int64); s = np.zeros(0)
    tree = merge_tree(clear, rank, lo, hi, s, math.inf)
    out, tau = [], tau_base
    while tree and tree[-1][0] >= tau_base - TOL:           # merges at or above the base persistence are what the levels add
        tau *= 2.0
        out.append(survivors(K, tree, tau))
        if tau - TOL > tree[-1][0]:
            break
    return out


def cell_levels(peak: np.ndarray, anchor_rc: np.ndarray, region: np.ndarray, la: np.ndarray, lb: np.ndarray,
                saddle: np.ndarray, tau_base: float) -> List[np.ndarray]:
    """Parent (the elder chart: highest peak, then anchor (row, col)) of every chart at the levels above the charts: level 1 =
    the persistence regions at tau_base, then tau_base * 2^j as `levels`. Levels that change nothing are left out. A pure
    function of what a stored chart (peak B, anchor cell, region) and a stored link (saddle B) carry. The region of a chart is
    the persistence region it was cut from; its peak is the largest peak of its charts and its pair saddle the largest saddle
    of the links between its charts, which is what the merge sequence of the base regions saw."""
    K = len(peak)
    if K == 0:
        return []
    peak = np.asarray(peak, float); rc = np.asarray(anchor_rc); region = np.asarray(region, np.int64)
    nR = int(region.max()) + 1
    o = np.lexsort((rc[:, 1], rc[:, 0], -peak))                   # charts, elder first
    elder = np.full(nR, -1, np.int64)
    first = o[np.unique(region[o], return_index=True)[1]]         # first (= elder) chart of every region
    elder[region[first]] = first
    out: List[np.ndarray] = []
    par = elder[region]
    if not np.array_equal(par, np.arange(K)):
        out.append(par)
    rpeak = np.full(nR, -1.0)
    np.maximum.at(rpeak, region, peak)
    ra, rb = region[la], region[lb]
    cross = ra != rb
    for rpar in levels(rpeak, rc[elder], ra[cross], rb[cross], np.asarray(saddle, float)[cross], tau_base):
        nxt = elder[rpar[region]]
        if not out or not np.array_equal(nxt, out[-1]):
            out.append(nxt)
    return out
