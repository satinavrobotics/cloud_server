"""Independent verifier of a ChartGraph state, written from the definitions only (it uses the public query raster, the raw
world for clearance and the navigable mask; it does not import the solver). Returns counts of violations per invariant.

cover     every 4-connected piece of free space holding a ball of radius s has an anchor, and no unowned pocket does
star      every owned cell is reached from its anchor by a classical Bresenham line (or the digital-drive tie) inside its
          chart, without cutting a corner
chart4    charts are 4-connected
band      clearance at the anchor >= (1 - eps) * peak clearance of its chart (eps = h / s)
topology  chi(served region) = V - E + T - Q of the link graph, EXACT (no hole is filled). E counts, per chart pair, the
          connected components of the INTERFACE CURVE of the two closed charts (unit edges between differing cells, joined
          when they share an end point). Counting components of the contact CELL SET instead merges two stretches that
          meet only through one cell of a chart poking toward a hole, and loses the loop around that hole (office01).
width     link width = widest path (maximin clearance x 2) inside the union of the two charts, 4-connected flood fill
"""
import math

import numpy as np
from scipy import ndimage

from packages.scene_graph.api import BBox

C4 = ndimage.generate_binary_structure(2, 1)
C8 = np.ones((3, 3), bool)
RES = 0.05


def _interface_stretches(full):
    """sum over chart pairs of the connected components of their interface curve (unit edges joined at shared end points)"""
    H, W = full.shape
    pairs = {}
    for y in range(H):
        for x in range(W):
            a = full[y, x]
            if a < 0: continue
            if x + 1 < W:
                b = full[y, x + 1]
                if b >= 0 and b != a: pairs.setdefault((min(a, b), max(a, b)), []).append(((y, x + 1), (y + 1, x + 1)))
            if y + 1 < H:
                b = full[y + 1, x]
                if b >= 0 and b != a: pairs.setdefault((min(a, b), max(a, b)), []).append(((y + 1, x), (y + 1, x + 1)))
    total = 0
    for segs in pairs.values():
        par = {}

        def f(v):
            while par.setdefault(v, v) != v:
                par[v] = par[par[v]]; v = par[v]
            return v
        for u, w in segs: par[f(u)] = f(w)
        total += len({f(u) for u, _ in segs})
    return total


def _bres(x0, y0, x1, y1):
    pts = []; dx = abs(x1 - x0); dy = -abs(y1 - y0); sx = 1 if x0 < x1 else -1; sy = 1 if y0 < y1 else -1; err = dx + dy
    while True:
        pts.append((x0, y0))
        if x0 == x1 and y0 == y1: break
        e2 = 2 * err
        if e2 >= dy: err += dy; x0 += sx
        if e2 <= dx: err += dx; y0 += sy
    return pts


def _dda(x0, y0, x1, y1):
    n = max(abs(x1 - x0), abs(y1 - y0)); pts = []
    for k in range(n + 1):
        t = k / n if n else 0
        fx = (x1 - x0) * t; fy = (y1 - y0) * t
        pts.append((x0 + int(math.copysign(math.floor(abs(fx) + 0.5), fx)), y0 + int(math.copysign(math.floor(abs(fy) + 0.5), fy))))
    return pts


def _line_ok(pts, own, nav, j):
    H, W = own.shape
    for i, (x, y) in enumerate(pts):
        if not (0 <= x < W and 0 <= y < H) or own[y, x] != j: return False
        if i:
            px, py = pts[i - 1]
            if px != x and py != y and not (nav[py, x] and nav[y, px]): return False
    return True


def verify(g, world, r=0.25, bbox=None):
    """g: ChartGraph; world: raw u8 occupancy at 0.05 m with its cell (0, 0) at the map origin (used for the query window unless `bbox`)."""
    p = g.params
    s = p.place; eps = p.eps
    b = bbox or BBox(-5.0, -5.0, world.shape[1] * RES + 5.0, world.shape[0] * RES + 5.0)
    v = g.query(b, raster=True)
    ra = v.raster
    out = dict(charts=len(v.charts), links=len(v.links))
    if ra is None:
        return dict(out, cover=0, star=0, chart4=0, band=0, topo=0, width=0)
    H, W = ra.index.shape
    full = ra.index.astype(int) - 1
    own = np.where(ra.dependent, -1, full)
    keypos = {k: i for i, k in enumerate(ra.keys)}
    sol = g._solved
    iy0 = int(round((ra.origin[1] - g._cy0 * RES) / (2 * RES))); ix0 = int(round((ra.origin[0] - g._cx0 * RES) / (2 * RES)))
    nav = sol.sp.nav[iy0:iy0 + H, ix0:ix0 + W]
    # clearance recomputed here from the folded map (the engine's input plane), unknown counting as blocked
    free = (g._V != 255) & (g._V < 100)
    clr = (ndimage.distance_transform_edt(free) * RES)[::2, ::2][iy0:iy0 + H, ix0:ix0 + W]
    cell = lambda c: (int(math.floor((c.anchor[0] - ra.origin[0]) / ra.step)), int(math.floor((c.anchor[1] - ra.origin[1]) / ra.step)))
    A = [cell(c) for c in v.charts if c.key in keypos]
    A = [None] * len(ra.keys)
    for c in v.charts:
        if c.key in keypos: A[keypos[c.key]] = cell(c)
    # cover
    lab, n = ndimage.label(nav, structure=C4)
    anch = np.zeros(n + 1, bool)
    for (x, y) in A:
        if 0 <= y < H and 0 <= x < W: anch[lab[y, x]] = True
    bad = 0
    for c in range(1, n + 1):
        m = lab == c
        if clr[m].max() >= s - 1e-9 and not anch[c]: bad += 1
    pl, pn = ndimage.label(nav & (own < 0), structure=C4)
    for c in range(1, pn + 1):
        if clr[pl == c].max() >= s - 1e-9: bad += 1
    out["cover"] = bad
    # star, chart4, band
    star = chart4 = band = 0; cent = []
    for j, (ax, ay) in enumerate(A):
        m = own == j
        if not m[ay, ax]: star += 1; continue
        for (y, x) in np.argwhere(m):
            if (x, y) == (ax, ay): continue
            if _line_ok(_bres(ax, ay, x, y), own, nav, j) or _line_ok(_dda(ax, ay, x, y), own, nav, j): continue
            star += 1
        if ndimage.label(m, structure=C4)[1] != 1: chart4 += 1
        peak = clr[m].max(); ratio = clr[ay, ax] / peak if peak > 0 else 1.0; cent.append(ratio)
        if ratio < (1 - eps) - 1e-9: band += 1
    out.update(star=star, chart4=chart4, band=band, centre_min=min(cent) if cent else None)
    # topology
    served = full >= 0
    pad = np.pad(served, 1)
    comps = ndimage.label(pad, structure=C4)[1]
    holes = ndimage.label(~pad, structure=C8)[1] - 1
    fp = np.pad(full, 1, constant_values=-1)
    a_ = fp[1:H + 1, 1:W + 1]
    pairs = {}
    for dy, dx in ((0, 1), (1, 0)):
        b_ = fp[1 + dy:H + 1 + dy, 1 + dx:W + 1 + dx]
        mk = (a_ >= 0) & (b_ >= 0) & (a_ != b_)
        for y, x in np.argwhere(mk):
            key = (min(int(a_[y, x]), int(b_[y, x])), max(int(a_[y, x]), int(b_[y, x])))
            pairs.setdefault(key, []).extend([(y, x), (y + dy, x + dx)])
    E = _interface_stretches(full)
    T = Q = 0
    for y in range(-1, H):
        for x in range(-1, W):
            ids = {int(i) for i in fp[y + 1:y + 3, x + 1:x + 3].ravel() if i >= 0}
            if len(ids) == 3: T += 1
            elif len(ids) == 4: T += 4; Q += 1
    chi_F = comps - holes; chi_G = len(A) - E + T - Q
    out.update(chi_F=int(chi_F), chi_G=int(chi_G), E=int(E), topo=int(chi_F != chi_G or E != len(v.links)))
    # widths
    wbad = 0
    for l in v.links:
        a, b2 = keypos[l.a], keypos[l.b]
        u = (full == a) | (full == b2); pa, pb = A[a], A[b2]
        vals = np.unique(np.round(clr[u] * 2, 9)); vals = vals[vals <= 2 * min(clr[pa[1], pa[0]], clr[pb[1], pb[0]]) + 1e-9]
        best = 0.0; lo, hi = 0, len(vals) - 1
        while lo <= hi:
            mid = (lo + hi) // 2; t = vals[mid]
            l2, _ = ndimage.label(u & (clr * 2 >= t - 1e-9), structure=C4)
            if l2[pa[1], pa[0]] > 0 and l2[pa[1], pa[0]] == l2[pb[1], pb[0]]: best = t; lo = mid + 1
            else: hi = mid - 1
        if abs(best - l.width) > 1e-6: wbad += 1
    out["width"] = wbad
    return out


def ok(res):
    return all(res[k] == 0 for k in ("cover", "star", "chart4", "band", "topo", "width"))
