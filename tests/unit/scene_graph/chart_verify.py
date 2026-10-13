"""Independent verifier of a ChartGraph state, written from the definitions only (no import from morse.py or the engine's
partition code). It reads the public query raster (the owner plane at every level), the navigable mask F, and recomputes the
distance to the non-navigable cells B itself. `verify` returns the number of violations per invariant.

cover        the charts are exactly F: every cell of F in exactly one chart, no other cell in any
chart4       every chart is 4-connected
anchor       the anchor is in its chart and is the chart cell nearest the chart's centroid (ties: more room)
radius       every cell is within geodesic distance 0.8 sight_range (the chart's diameter bound) of its anchor, along cells
             of its own chart
planar       chi(F) = sum_i (1 - holes_i) - arcs + junctions, the Euler identity of the planar map of the partition
             (arcs: maximal chains of interface edges through corners of map degree 2; junction: interior corner with >= 3
             interface edges)
portal       every link's portal is a 4-adjacent pair, one cell in each chart, and the link is at least as wide as 2 min(clr)
             at the portal (clr: physical clearance, the distance to the nearest blocked or unknown cell, recomputed here)
width        the widest link of a chart pair is 2 * the largest min(clr) over the 4-adjacent cell pairs between the charts
persist      at every level above the charts, adjacent regions have persistence min(peak) - saddle >= tau_0 = 2 sqrt(2) h
             (a level above the charts is a union of persistence regions, so never crosses a pass cheaper than tau_0)
nesting      every region of level j-1 lies in exactly one region of level j; the last level has one region per component
region       a chart lies in exactly one persistence region (Chart.region), and the regions are exactly the level-1 image
             of the charts grouped by region
"""
import heapq
import math

import numpy as np
from scipy import ndimage

from packages.scene_graph.api import BBox

C4 = ndimage.generate_binary_structure(2, 1)
C8 = np.ones((3, 3), bool)
RES = 0.05
ALL = BBox(-1e9, -1e9, 1e9, 1e9)
TOL = 1e-6


def _holes(m):
    ys, xs = np.nonzero(m)
    sub = np.pad(m[ys.min():ys.max() + 1, xs.min():xs.max() + 1], 1)
    return ndimage.label(~sub, structure=C8)[1] - 1


def planar_map(lab):
    """(arcs, junctions) of the partition image `lab` (-1 outside F), from the definition (loops over unit edges)"""
    H, W = lab.shape
    L = np.pad(lab, 1, constant_values=-1)
    iface, nb = {}, {}
    for vy in range(H + 1):
        for vx in range(W):
            p, q = L[vy, vx + 1], L[vy + 1, vx + 1]
            if p == q: continue
            e = frozenset(((vy, vx), (vy, vx + 1)))
            if p >= 0 and q >= 0: iface[e] = 1
            else:
                for v in e: nb[v] = nb.get(v, 0) + 1
    for vy in range(H):
        for vx in range(W + 1):
            p, q = L[vy + 1, vx], L[vy + 1, vx + 1]
            if p == q: continue
            e = frozenset(((vy, vx), (vy + 1, vx)))
            if p >= 0 and q >= 0: iface[e] = 1
            else:
                for v in e: nb[v] = nb.get(v, 0) + 1
    at = {}
    for e in iface:
        for v in e: at.setdefault(v, []).append(e)
    joint = {v for v, es in at.items() if len(es) == 2 and nb.get(v, 0) == 0}
    seen, arcs = set(), 0
    for e0 in iface:
        if e0 in seen: continue
        seen.add(e0); stack = [e0]; has_end = False
        while stack:
            e = stack.pop()
            for v in e:
                if v in joint:
                    for e2 in at[v]:
                        if e2 not in seen: seen.add(e2); stack.append(e2)
                else: has_end = True
        arcs += has_end
    J = sum(1 for v, es in at.items() if 0 < v[0] <= H and 0 < v[1] <= W and len(es) >= 3
            and L[v[0]:v[0] + 2, v[1]:v[1] + 2].min() >= 0)
    return arcs, J


def distance(F, h):
    return ndimage.distance_transform_edt(np.pad(F, 1))[1:-1, 1:-1] * h


def crossings(lab, B):
    """{(a, b): [value min(B) of every 4-adjacent cell pair between a < b]}"""
    out = {}
    H, W = lab.shape
    for dy, dx in ((0, 1), (1, 0)):
        A, Bq = lab[:H - dy, :W - dx], lab[dy:, dx:]
        for y, x in np.argwhere((A >= 0) & (Bq >= 0) & (A != Bq)):
            a, b = int(A[y, x]), int(Bq[y, x])
            out.setdefault((min(a, b), max(a, b)), []).append(min(B[y, x], B[y + dy, x + dx]))
    return out


def geodesic(mask, sources, h, cap=math.inf):
    """distance (m) from the nearest of `sources` (list of (y, x)) to every cell of `mask`, along 8-neighbour steps inside
    `mask` (a diagonal needs both orthogonal cells in `mask`); inf where unreached or beyond `cap`"""
    H, W = mask.shape
    d = np.full(mask.shape, np.inf)
    pq = []
    for s in sources:
        d[s] = 0.0; heapq.heappush(pq, (0.0, s))
    while pq:
        dd, (y, x) = heapq.heappop(pq)
        if dd > d[y, x]: continue
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                if not (dy or dx): continue
                y2, x2 = y + dy, x + dx
                if not (0 <= y2 < H and 0 <= x2 < W) or not mask[y2, x2]: continue
                if dy and dx and not (mask[y, x2] and mask[y2, x]): continue
                nd = dd + math.hypot(dy, dx) * h
                if nd < d[y2, x2] - 1e-12 and nd <= cap + 1e-9:
                    d[y2, x2] = nd; heapq.heappush(pq, (nd, (y2, x2)))
    return d


def _image(g, level):
    """(owner index image with -1 outside, keys, ViewResult) of one level over the whole canvas"""
    v = g.query(ALL, level=level, raster=True)
    ra = v.raster
    if ra is None: return None, [], v
    return ra.index.astype(int) - 1, ra.keys, v


def verify(g, world=None, r=None, bbox=None):
    p = g.params
    h = p.lattice
    r = p.robot_half_side if r is None else r
    lab0, keys0, v0 = _image(g, 0)
    out = dict(charts=len(v0.charts), links=len(v0.links))
    out.update(cover=0, chart4=0, anchor=0, radius=0, planar=0, portal=0, width=0, persist=0, nesting=0,
               region=0, holes=0, levels=0, chi_F=0)
    if lab0 is None:
        return out
    ra = v0.raster
    H, W = lab0.shape
    iy0 = int(round((ra.origin[1] - g._cy0 * RES) / (2 * RES))); ix0 = int(round((ra.origin[0] - g._cx0 * RES) / (2 * RES)))
    F = g._build_F()[0][iy0:iy0 + H, ix0:ix0 + W]
    out["cover"] = int(((lab0 >= 0) != F).sum())
    B = distance(F, h)
    free = (g._V != 255) & (g._V < 100)
    clr = (ndimage.distance_transform_edt(free) * RES)[::2, ::2][iy0:iy0 + H, ix0:ix0 + W]
    kpos = {k: i for i, k in enumerate(keys0)}
    anchor = {}
    for c in v0.charts:
        anchor[kpos[c.key]] = (int(math.floor((c.anchor[1] - ra.origin[1]) / ra.step)),
                               int(math.floor((c.anchor[0] - ra.origin[0]) / ra.step)))
    holes = 0
    region = {kpos[c.key]: c.region for c in v0.charts}
    radius = max(0.4 * p.sight_range, h)                 # written from the definition: chart radius 0.4 sight_range
    for j in range(len(keys0)):
        m = lab0 == j
        if not m.any(): out["anchor"] += 1; continue
        if ndimage.label(m, structure=C4)[1] != 1: out["chart4"] += 1
        holes += _holes(m)
        ay, ax = anchor[j]
        if lab0[ay, ax] != j: out["anchor"] += 1; continue
        yy, xx = np.nonzero(m)
        d2 = (yy - yy.mean()) ** 2 + (xx - xx.mean()) ** 2
        if (ay - yy.mean()) ** 2 + (ax - xx.mean()) ** 2 > d2.min() + 1e-9: out["anchor"] += 1
        d = geodesic(m, [(ay, ax)], h)
        out["radius"] += int((d[m] > 2 * radius + 1e-9).any())
    regs = {}
    for j, k in region.items(): regs.setdefault(k, []).append(j)
    # planar identity
    comps = ndimage.label(F, structure=C4)[1]
    chi_F = comps - (ndimage.label(np.pad(~F, 1, constant_values=True), structure=C8)[1] - 1)
    arcs, J = planar_map(lab0)
    out["planar"] = int(chi_F != len(keys0) - holes - arcs + J)
    out["chi_F"] = int(chi_F)
    # links: portal and width; saddle
    cr = crossings(lab0, B)
    crc = crossings(lab0, clr)
    best = {}
    for l in v0.links:
        a, b = sorted((kpos[l.a], kpos[l.b]))
        cells = [(int(math.floor((q[1] - ra.origin[1]) / ra.step)), int(math.floor((q[0] - ra.origin[0]) / ra.step)))
                 for q in l.portal]
        (y0, x0), (y1, x1) = cells
        good = abs(y0 - y1) + abs(x0 - x1) == 1 and {lab0[y0, x0], lab0[y1, x1]} == {a, b}
        good = good and l.width >= 2 * min(clr[y0, x0], clr[y1, x1]) - TOL
        out["portal"] += int(not good)
        best[(a, b)] = max(best.get((a, b), -1.0), l.width)
    for (a, b), w in best.items():
        if abs(w - 2 * max(crc[(a, b)])) > TOL: out["width"] += 1
    # levels: persistence and nesting
    tau0 = 2.0 * math.sqrt(2.0) * h
    top = max((max(c.parent) for c in v0.charts if c.parent), default=0)
    out["levels"] = top + 1
    prev = None
    for lvl in range(top + 1):
        img = lab0 if lvl == 0 else _image(g, lvl)[0]
        K = int(img.max()) + 1 if (img >= 0).any() else 0
        peak = {j: float(B[img == j].max()) for j in range(K)}
        for (a, b), cs in crossings(img, B).items():
            if lvl >= 1 and min(peak[a], peak[b]) - max(cs) < tau0 - TOL: out["persist"] += 1
        if prev is not None:
            for j in np.unique(prev[prev >= 0]):
                if len(np.unique(img[prev == j])) != 1: out["nesting"] += 1
        prev = img
    if (prev >= 0).any():
        out["nesting"] += int(len(np.unique(prev[prev >= 0])) != comps)
    if top >= 1:                                    # charts of one persistence region are in one owner of the first level
        img1 = _image(g, 1)[0]
        for k, js in regs.items():
            if len(np.unique(img1[np.isin(lab0, js)])) != 1: out["region"] += 1
    out["holes"] = int(holes)
    return out


def ok(res):
    return all(res[k] == 0 for k in ("cover", "chart4", "anchor", "radius", "planar", "portal", "width",
                                     "persist", "nesting", "region"))
