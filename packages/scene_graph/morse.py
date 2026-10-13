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
The engine's radius is CHART_RADIUS * sight_range; `levels` above the charts are `cell_levels`.

Counting identity (any tie order): chi(F) = #maxima - sum(saddle multiplicities) (B has no minima). Nothing here reads a chart
index, a leaf or the canvas: the engine calls `partition` once per batch.
"""
import hashlib
import heapq
import math
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import numpy as np
from scipy import ndimage
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import connected_components, dijkstra

TAU0_FACTOR = 2.0 * math.sqrt(2.0)
CHART_RADIUS = 0.4                  # chart radius = CHART_RADIUS * sight_range: diameter 0.8 of what one leaf observes
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

    def neighbours(self) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        """cell index of the N, E, S, W 4-neighbour of every cell (-1: not in F)"""
        P = np.pad(self.idx, 1, constant_values=-1)
        y, x = self.ys + 1, self.xs + 1
        return P[y - 1, x], P[y, x + 1], P[y + 1, x], P[y, x - 1]


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


def ranks(f: np.ndarray) -> np.ndarray:
    """rank of every cell in the order (-f, row, col); cells are row-major, so a stable sort of -f is that order"""
    order = np.argsort(-f, kind="stable")
    r = np.empty(len(f), np.int64)
    r[order] = np.arange(len(f))
    return r


def raw_basins(sp: Space, rank: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
    """(basin id per cell, root cell per basin). Basin ids follow the rank of the roots."""
    N = sp.N
    nb = np.stack(sp.neighbours())                                          # (4, N)
    rn = np.where(nb >= 0, rank[np.maximum(nb, 0)], BIG)
    arg = rn.argmin(0)
    has = rn[arg, np.arange(N)] < rank
    par = np.where(has, nb[arg, np.arange(N)], np.arange(N)).astype(np.int64)
    while True:                                                             # pointer doubling: ranks fall along every chain
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


def crossings(sp: Space, lab: np.ndarray, f: np.ndarray):
    """4-adjacent cell pairs (i, j) in different regions: (i, j, value min(f_i, f_j)); each unordered pair once (E and S)"""
    _, E, S, _ = sp.neighbours()
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
    o = np.lexsort((-val, code))
    code, v = code[o], val[o]
    first = np.concatenate([[True], code[1:] != code[:-1]])
    c = code[first]
    return c // n, c % n, v[first]


# ---------------------------------------------------------------------------------------------------------------- merge
def merge_tree(peakv, peakr, lo, hi, s, tau: float) -> List[Tuple[float, int, int]]:
    """The merges of persistence below `tau`, smallest persistence first: [(p, keep, drop)] on the region ids given.
    peakv / peakr: peak value and peak rank per region (the elder of a pair is the higher peak, ties the smaller rank);
    (lo, hi, s): adjacent pairs and their saddle. Pure union-find on region ids, O((R + S) log S)."""
    peakv = dict(enumerate(np.asarray(peakv, float).tolist())) if not isinstance(peakv, dict) else dict(peakv)
    peakr = dict(enumerate(np.asarray(peakr).tolist())) if not isinstance(peakr, dict) else dict(peakr)
    sad: Dict[int, Dict[int, float]] = {}
    for a, b, v in zip(np.asarray(lo).tolist(), np.asarray(hi).tolist(), np.asarray(s, float).tolist()):
        sad.setdefault(a, {})[b] = v
        sad.setdefault(b, {})[a] = v
    ver = {r: 0 for r in peakv}
    heap: list = []
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
    out: List[Tuple[float, int, int]] = []
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
    o = np.lexsort((np.arange(sp.N), -f, d2, lab))
    first = np.concatenate([[True], lab[o][1:] != lab[o][:-1]])
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


def edges8(sp: Space, h: float, region: Optional[np.ndarray] = None) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """8-neighbour lattice edges inside F as (row cell, column cell, length m); a diagonal needs both orthogonal cells in F,
    as the engine's route does, so geodesic distances here are the distances routes have. With `region`, an edge joins cells of
    one region only, and a diagonal also needs both orthogonal cells in that region (a cell of another region beside the
    step is across a pass: the step would be a way round it)."""
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


def subdivide(sp: Space, f: np.ndarray, region: np.ndarray, radius: float, h: float,
              cache: Optional[dict] = None) -> Tuple[np.ndarray, np.ndarray]:
    """(chart per cell, net point cell per chart): every persistence region cut into geodesic Voronoi cells of a greedy net.
    Cells are taken in the order of room (B quantised to the lattice step, ties in scan order: translation invariant, and
    stable when the canvas grows, which keeps the relative scan order); a
    cell with no net point within geodesic `radius` INSIDE ITS REGION becomes one. Hence every cell of a region is within
    `radius` of a net point (covering), net points of one region are further than `radius` apart (packing), the first net
    point of a region is a cell of maximal room, and no chart crosses a pass. Charts are the Voronoi cells of the net points
    (geodesic in the region): connected, and every cell reaches its net point without leaving its chart. The anchor of a
    chart is its centre (`centres`), not the net point. `cache` (a dict the caller keeps
    between calls) serves a region whose cells and room are unchanged from the last call; it holds the last call's regions only."""
    N = sp.N
    R, C, L = edges8(sp, h, region)
    eo = np.argsort(region[R], kind="stable")
    R, C, L = R[eo], C[eo], L[eo]
    nR = int(region.max()) + 1
    ecut = np.searchsorted(region[R], np.arange(nR + 1))
    co = np.argsort(region, kind="stable")
    ccut = np.searchsorted(region[co], np.arange(nR + 1))
    q = np.rint(f / h).astype(np.int64)
    lab = np.empty(N, np.int32)
    seeds: List[int] = []
    used: dict = {}
    for r in range(nR):                                          # a region at a time: distances never leave it
        cells = np.sort(co[ccut[r]:ccut[r + 1]])                 # ascending cell index = scan order
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
            sub = G[loc][:, loc]
            d = dijkstra(sub, directed=False, indices=int(np.nonzero(loc == s)[0][0]),
                         limit=radius + TOL)
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
    bid, roots = raw_basins(sp, rank)
    R = len(roots)
    i, j, val = crossings(sp, bid, f)
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
    full, seeds = subdivide(sp, f, reg, radius, h, cache)
    return Partition(sp, f, full, len(seeds), centres(sp, full, f, len(seeds)), R, len(tree), reg[seeds].astype(np.int64),
                     len(keep), seeds)


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
    bnd = np.zeros(nV, np.int64)
    for nb, v0, v1 in ((Nn, y * W1 + x, y * W1 + x + 1), (E, y * W1 + x + 1, (y + 1) * W1 + x + 1),
                       (S, (y + 1) * W1 + x, (y + 1) * W1 + x + 1), (Wn, y * W1 + x, (y + 1) * W1 + x)):
        m = nb < 0
        bnd += np.bincount(v0[m], minlength=nV) + np.bincount(v1[m], minlength=nV)
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
