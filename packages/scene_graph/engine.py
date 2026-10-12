"""Engine behind `api.SceneGraph`: the free-space charts.

    fold     per cell lexicographic max of (capture time, value); unknown carries no key. Commutative, associative,
             idempotent: leaves may arrive in any order, twice, in any batches. `ever_blocked` is the latest capture time at
             which a cell was reported blocked (max-merge); optional channels fold the same way, one plane pair each.
    F(r)     navigable cells: components of (square fits at one of 23 headings  U  driven-and-free W(r)) that touch where a
             robot has been (G). Evidence is the driven polyline of each session, tagged with the half side r_d of its robot.
    charts   star-shaped cover with an anchor each (chart_solver), re-solved locally per batch (chart_track); persistent keys
             by overlap with the previous owner plane (chart_keys).
    links    one per connected stretch of orthogonal contact; length (the cost) of the shortest route anchor -> contact ->
             anchor inside the two charts, strict widest-path width, line integral of every channel along that route.
The graph carries evidence and length, never a price. Pure numpy / scipy, no I/O.

Not implemented here: sparse / tiled in-memory planes (one dense canvas that grows), local F construction (F is rebuilt for
the whole canvas per batch; only the chart solve is local), persistence (store.py), the leaf-delete winner plane (refold
rebuilds the bbox from the leaves the caller supplies).
"""
import math
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
from scipy import ndimage
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import dijkstra

from .api import BBox, Blocked, Chart, Dirty, Key, Leaf, Link, OwnerRaster, Route, SceneGraph, SceneParams, Snap, ViewResult
from .chart_keys import assign_A, overlaps
from .chart_levels import hierarchy
from .chart_track import Track
from .leafcover import View as CamView
from .leafcover import select_views
from .state import EngineState, LeafRecord
from .window import to_lattice

RES = 0.05                      # costmap cell (m); the lattice is two cells (SceneParams.lattice = 0.1)
HEADINGS = 23                   # corner shift r * sqrt2 * dtheta < res / 2 at 90 / 23 degrees
TILE = 64                       # lattice cells per tile side (corridor tiles)
MARGIN = 40                     # canvas growth margin (full-resolution cells)
RESERVED = ("undriven", "ever_blocked", "soft")


@dataclass
class _Rec:
    session: str
    t: int
    x: float
    y: float
    r_d: float
    view: Optional[CamView]


@dataclass
class _Solved:
    """Everything one solved state answers from (image positions are lattice cells of the canvas)."""
    sp: object
    A: List[int]
    own: np.ndarray
    full: np.ndarray            # chart index per cell (dependents attached), -1 outside F
    keys: List[Key]
    charts: List[Chart]
    links: List[dict]
    edges: Tuple[np.ndarray, np.ndarray, np.ndarray]
    plane: np.ndarray           # canvas image: chart index per lattice cell, -1 outside
    dep: np.ndarray             # canvas image: cell attached by cost, not seen from its anchor
    S: np.ndarray               # driven (body covered) image
    clr: np.ndarray


def _square(theta: float, side: float, res: float = RES) -> np.ndarray:
    n = int(round(side / res)); h = (n + 2) * 2; c = h / 2.0; R = side / res / 2.0
    a = math.radians(theta); ca, sa = math.cos(a), math.sin(a)
    j, i = np.mgrid[0:h, 0:h]
    px, py = i + 0.5 - c, j + 0.5 - c
    u = ca * px + sa * py; v = -sa * px + ca * py
    k = (np.abs(u) <= R + 1e-9) & (np.abs(v) <= R + 1e-9)
    ys, xs = np.nonzero(k)
    return k[ys.min():ys.max() + 1, xs.min():xs.max() + 1]


_SQ: Dict[float, List[np.ndarray]] = {}


def _squares(r: float) -> List[np.ndarray]:
    key = round(r, 4)
    if key not in _SQ:
        _SQ[key] = [_square(k * 90.0 / HEADINGS, 2 * r) for k in range(HEADINGS)]
    return _SQ[key]


def _line(y0: int, x0: int, y1: int, x1: int):
    n = max(abs(y1 - y0), abs(x1 - x0), 1); t = np.arange(n + 1) / n
    return np.rint(y0 + (y1 - y0) * t).astype(int), np.rint(x0 + (x1 - x0) * t).astype(int)


class ChartGraph(SceneGraph):
    def __init__(self, params: SceneParams = SceneParams()):
        super().__init__(params)
        if params.sight_range < params.place - 1e-12:
            raise ValueError("sight_range below the place scale")
        self.version = 0
        self._h = params.lattice
        self._leaves: Dict[str, _Rec] = {}
        self._cx0 = self._cy0 = 0               # full-resolution global cell index of canvas cell (0, 0); even
        self._V = self._T = self._B = None
        self._chan: Dict[str, Tuple[np.ndarray, np.ndarray]] = {}
        self._track: Optional[Track] = None
        self._key_plane: Optional[np.ndarray] = None
        self._keys: List[Key] = []
        self._solved: Optional[_Solved] = None
        self._watermark: Tuple[int, str] = (-1, "")
        self._sig: Dict[Key, tuple] = {}
        self._link_sig: set = set()
        self._cache: dict = {}

    # ------------------------------------------------------------------ canvas and fold
    def _ensure(self, gx0: int, gy0: int, gx1: int, gy1: int) -> Tuple[int, int]:
        """grow the canvas to hold [gx0, gx1) x [gy0, gy1); returns the shift (rows, cols) in LATTICE cells of old content"""
        if self._V is None:
            self._cx0 = (gx0 - MARGIN) // 2 * 2; self._cy0 = (gy0 - MARGIN) // 2 * 2
            W = (gx1 + MARGIN - self._cx0 + 1) // 2 * 2; H = (gy1 + MARGIN - self._cy0 + 1) // 2 * 2
            self._V = np.full((H, W), 255, np.uint8); self._T = -np.ones((H, W), np.int64); self._B = -np.ones((H, W), np.int64)
            return 0, 0
        H, W = self._V.shape
        if gx0 >= self._cx0 and gy0 >= self._cy0 and gx1 <= self._cx0 + W and gy1 <= self._cy0 + H:
            return 0, 0
        nx0 = min(self._cx0, (gx0 - MARGIN) // 2 * 2); ny0 = min(self._cy0, (gy0 - MARGIN) // 2 * 2)
        nx1 = max(self._cx0 + W, gx1 + MARGIN); ny1 = max(self._cy0 + H, gy1 + MARGIN)
        nW = (nx1 - nx0 + 1) // 2 * 2; nH = (ny1 - ny0 + 1) // 2 * 2
        oy, ox = self._cy0 - ny0, self._cx0 - nx0

        def grow(a, fill):
            b = np.full((nH, nW), fill, a.dtype); b[oy:oy + H, ox:ox + W] = a; return b
        self._V = grow(self._V, 255); self._T = grow(self._T, -1); self._B = grow(self._B, -1)
        self._chan = {k: (grow(v, 255), grow(t, -1)) for k, (v, t) in self._chan.items()}
        if self._key_plane is not None:
            kp = np.full((nH // 2, nW // 2), -1, self._key_plane.dtype)
            kp[oy // 2:oy // 2 + H // 2, ox // 2:ox // 2 + W // 2] = self._key_plane; self._key_plane = kp
        self._cx0, self._cy0 = nx0, ny0
        return oy // 2, ox // 2

    @staticmethod
    def _fold_into(V, T, w, gx, gy, cx0, cy0, t, B=None):
        H, W = V.shape
        ox, oy = gx - cx0, gy - cy0
        ys0, xs0 = max(0, oy), max(0, ox); ys1, xs1 = min(H, oy + w.shape[0]), min(W, ox + w.shape[1])
        if ys0 >= ys1 or xs0 >= xs1: return
        sub = w[ys0 - oy:ys1 - oy, xs0 - ox:xs1 - ox]
        Tc, Vc = T[ys0:ys1, xs0:xs1], V[ys0:ys1, xs0:xs1]
        kn = sub != 255
        win = kn & ((Tc < 0) | (t > Tc) | ((t == Tc) & (sub > Vc)))     # ties: the larger (blocked) value
        Tc[win] = t; Vc[win] = sub[win]
        if B is not None:
            Bc = B[ys0:ys1, xs0:xs1]
            bl = kn & (sub >= 100)
            Bc[bl] = np.maximum(Bc[bl], t)

    def _fold_leaf(self, lf: Leaf) -> Tuple[int, int, int, int]:
        w, (x0, y0) = to_lattice(lf.window_u8, lf.resolution, lf.origin, lf.origin_yaw, RES)
        gx, gy = int(round(x0 / RES)), int(round(y0 / RES))
        chans = {k: to_lattice(v, lf.resolution, lf.origin, lf.origin_yaw, RES)[0] for k, v in lf.channels.items()
                 if k not in RESERVED and v is not None}
        self._pending.append((lf, w, gx, gy, chans))
        return gx, gy, gx + w.shape[1], gy + w.shape[0]

    # ------------------------------------------------------------------ public: fold / refold
    def fold_many(self, leaves: Sequence[Leaf]) -> Dirty:
        return self._apply(leaves, None)

    def refold(self, leaves: Sequence[Leaf], bbox: BBox) -> Dirty:
        """Rebuild the area of `bbox` from `leaves`, which must be every leaf overlapping it that still exists: cells in
        the box are reset, a stored leaf whose pose lies in the box and is not supplied is a deleted one."""
        return self._apply(leaves, bbox)

    def _apply(self, leaves: Sequence[Leaf], bbox: Optional[BBox]) -> Dirty:
        old = self._solved
        old_sig, old_link_sig, old_keys = dict(self._sig), set(self._link_sig), list(self._keys)
        self._pending = []
        bounds = None
        for lf in leaves:
            b = self._fold_leaf(lf)
            bounds = b if bounds is None else (min(bounds[0], b[0]), min(bounds[1], b[1]), max(bounds[2], b[2]), max(bounds[3], b[3]))
        region = None
        if bbox is not None and self._V is not None:
            # a stored leaf of the area that was not supplied is a deleted leaf
            keep = {lf.key for lf in leaves}
            for k in [k for k, r in self._leaves.items()
                      if bbox.x0 <= r.x <= bbox.x1 and bbox.y0 <= r.y <= bbox.y1 and k not in keep]:
                del self._leaves[k]
            gx0, gy0 = int(math.floor(bbox.x0 / RES)), int(math.floor(bbox.y0 / RES))
            gx1, gy1 = int(math.ceil(bbox.x1 / RES)), int(math.ceil(bbox.y1 / RES))
            region = (gx0, gy0, gx1, gy1)
        if not self._pending and region is None:
            return Dirty(None, [], {}, [], self._watermark)
        shift = (0, 0)
        if bounds is not None:
            shift = self._ensure(*bounds)
        if region is not None and self._V is not None:
            x0, y0 = max(region[0] - self._cx0, 0), max(region[1] - self._cy0, 0)
            x1, y1 = min(region[2] - self._cx0, self._V.shape[1]), min(region[3] - self._cy0, self._V.shape[0])
            if x0 < x1 and y0 < y1:
                self._V[y0:y1, x0:x1] = 255; self._T[y0:y1, x0:x1] = -1; self._B[y0:y1, x0:x1] = -1
                for v, t in self._chan.values():
                    v[y0:y1, x0:x1] = 255; t[y0:y1, x0:x1] = -1
        for lf, w, gx, gy, chans in self._pending:
            self._fold_into(self._V, self._T, w, gx, gy, self._cx0, self._cy0, lf.stamp_ms, self._B)
            for name, cw in chans.items():
                if name not in self._chan:
                    self._chan[name] = (np.full(self._V.shape, 255, np.uint8), -np.ones(self._V.shape, np.int64))
                v, t = self._chan[name]
                self._fold_into(v, t, cw, gx, gy, self._cx0, self._cy0, lf.stamp_ms)
            self._leaves[lf.key] = _Rec(lf.session, lf.stamp_ms, lf.x, lf.y, lf.r_d,
                                        None if lf.view is None else CamView(lf.key, lf.view.x, lf.view.y, lf.view.yaw, lf.view.fov, lf.view.rng))
            self._watermark = max(self._watermark, (lf.stamp_ms, lf.key))
        birth = max(((lf.stamp_ms, lf.key) for lf in leaves), default=self._watermark)[1]
        self._pending = []
        self._cache = {}
        self._solve(shift, birth)
        self.version += 1
        return self._dirty(old, old_sig, old_link_sig, old_keys, bounds, region)

    # ------------------------------------------------------------------ F(r), evidence
    def _polylines(self) -> Dict[float, np.ndarray]:
        """per r_d: raster (lattice image) of the driven polylines of that robot; consecutive leaves of one session by capture time"""
        H, W = self._V.shape[0] // 2, self._V.shape[1] // 2
        by_sess: Dict[str, list] = {}
        for k, r in self._leaves.items():
            by_sess.setdefault(r.session, []).append((r.t, k, r))
        out: Dict[float, np.ndarray] = {}
        for sess, items in by_sess.items():
            items.sort(key=lambda it: (it[0], it[1]))
            pts = []
            for _, _, r in items:
                gx, gy = int(math.floor(r.x / RES)), int(math.floor(r.y / RES))
                pts.append((round((gx - self._cx0) / 2.0), round((gy - self._cy0) / 2.0), round(r.r_d, 4)))
            for i, (x, y, d) in enumerate(pts):
                m = out.setdefault(d, np.zeros((H, W), bool))
                if 0 <= y < H and 0 <= x < W: m[y, x] = True
                if i + 1 < len(pts) and pts[i + 1][2] == d:
                    xx, yy, _ = pts[i + 1]
                    rr, cc = _line(y, x, yy, xx); ok = (rr >= 0) & (rr < H) & (cc >= 0) & (cc < W); m[rr[ok], cc[ok]] = True
        return out

    def _build_F(self):
        r, h = self.params.robot_half_side, self._h
        V = self._V
        free = (V != 255) & (V < 100)
        sq = np.zeros_like(free)
        for st in _squares(r):
            sq |= ndimage.binary_erosion(free, structure=st, border_value=0)
        clr = ndimage.distance_transform_edt(free) * RES
        sq_h, fr_h, clr_h = sq[::2, ::2], free[::2, ::2], clr[::2, ::2]
        G = np.zeros(sq_h.shape, bool); W = np.zeros_like(G); S = np.zeros_like(G)
        for d, raster in self._polylines().items():
            dist = ndimage.distance_transform_edt(~raster) * h
            G |= dist <= h + 1e-9; S |= dist <= d + 1e-9
            if d >= r - 1e-9: W |= dist <= (d - r + h) + 1e-9
        cand = sq_h | (W & fr_h)
        lab, _ = ndimage.label(cand, structure=np.ones((3, 3)))
        g = G & cand
        keep = np.isin(lab, np.unique(lab[g])) & cand if g.any() else np.zeros_like(cand)
        return keep, clr_h, S

    # ------------------------------------------------------------------ solve
    def _solve(self, shift: Tuple[int, int], birth: str):
        p = self.params
        nav, clr, S = self._build_F()
        if self._track is None:
            self._track = Track(r=p.robot_half_side, mult=p.place_mult, h=self._h, sight_range=p.sight_range)
        elif shift != (0, 0) or (self._track.nav is not None and self._track.nav.shape != nav.shape):
            self._track.grow(shift, nav.shape)          # the canvas grew: carry rows, ownership and anchors, do not re-solve
        if self._key_plane is None:
            self._key_plane = -np.ones(nav.shape, np.int32)
        self._retired_map = {}
        if not nav.any():
            self._retired_map = {self._fmt(k): [] for k in self._keys}
            self._track = Track(r=p.robot_half_side, mult=p.place_mult, h=self._h, sight_range=p.sight_range)
            self._solved = None; self._keys = []; self._key_plane = -np.ones(nav.shape, np.int32)
            return
        sp, A, own, rnd = self._track.step(nav, clr)
        self._solved = self._evaluate(sp, A, own, nav, clr, S, birth)

    def _evaluate(self, sp, A, own, nav, clr, S, birth) -> _Solved:
        H, W = nav.shape
        edges = self._lattice_edges(sp)
        R, C, Ln = edges
        N = sp.N
        full = own.astype(np.int64).copy()
        dep = own < 0
        if dep.any() and (~dep).any():
            G = coo_matrix((np.concatenate([Ln, Ln]), (np.concatenate([R, C]), np.concatenate([C, R]))), shape=(N, N)).tocsr()
            _, _, so = dijkstra(G, directed=False, indices=np.nonzero(~dep)[0], min_only=True, return_predecessors=True)
            ok = dep & (so >= 0); full[ok] = own[so[ok]]
        plane = -np.ones((H, W), np.int32); plane[sp.ys, sp.xs] = full
        depi = np.zeros((H, W), bool); depi[sp.ys[dep & (full >= 0)], sp.xs[dep & (full >= 0)]] = True
        # persistent keys
        cells = [(int(sp.xs[a]), int(sp.ys[a])) for a in A]
        prev_keys = list(self._keys)
        keys_t, retired_t = assign_A(prev_keys, self._key_plane, plane, cells, birth, None)
        ov = overlaps(self._key_plane, plane, len(prev_keys)) if prev_keys else {}
        succ: Dict[tuple, List[tuple]] = {}
        for (j, k), n in sorted(ov.items(), key=lambda it: -it[1]):
            if prev_keys[k] in retired_t: succ.setdefault(prev_keys[k], []).append(keys_t[j])
        self._retired_map = {self._fmt(k): [self._fmt(s) for s in succ.get(k, [])] for k in retired_t}
        kp = -np.ones((H, W), np.int32)
        kp[sp.ys, sp.xs] = np.where(full >= 0, full, -1)       # chart index; keys list below is in the same order
        self._key_plane = kp; self._keys = list(keys_t)
        keys = [self._fmt(k) for k in keys_t]
        # channels on the lattice
        V = self._V
        v = V[::2, ::2]
        ch = {"undriven": (~S).astype(float), "ever_blocked": (self._B[::2, ::2] >= 0).astype(float),
              "soft": np.where((v > 0) & (v < 100), v / 99.0, 0.0)}
        for name, (cv, _) in self._chan.items():
            w = cv[::2, ::2]; ch[name] = np.where((w > 0) & (w < 100), w / 99.0, 0.0)
        chv = {k: f[sp.ys, sp.xs] for k, f in ch.items()}
        links = self._links(sp, A, full, edges, chv)
        # chart records
        eb = ch["ever_blocked"]
        pose_cell = {}
        for k, r in self._leaves.items():
            y = int(math.floor(r.y / RES) - self._cy0) // 2; x = int(math.floor(r.x / RES) - self._cx0) // 2
            if 0 <= y < H and 0 <= x < W and plane[y, x] >= 0 and S[y, x]:
                pose_cell.setdefault(int(plane[y, x]), []).append((k, y, x))
        charts = []
        order = np.argsort(full, kind="stable"); cut = np.searchsorted(full[order], np.arange(len(A) + 1))
        for j, a in enumerate(A):
            cc = order[cut[j]:cut[j + 1]]
            ay, ax = int(sp.ys[a]), int(sp.xs[a])
            leaf = None
            if j in pose_cell:
                leaf = min(pose_cell[j], key=lambda t: ((t[1] - ay) ** 2 + (t[2] - ax) ** 2, t[0]))[0]
            charts.append(Chart(key=keys[j], anchor=self._cell_xy(ay, ax), cells=int(len(cc)), area_m2=float(len(cc) * self._h ** 2),
                                clearance=float(sp.clr[a]), driven_frac=float(S[sp.ys[cc], sp.xs[cc]].mean()) if len(cc) else 0.0,
                                ever_blocked_frac=float(eb[sp.ys[cc], sp.xs[cc]].mean()) if len(cc) else 0.0, leaf=leaf))
        return _Solved(sp=sp, A=list(A), own=own, full=full, keys=keys, charts=charts, links=links, edges=edges, plane=plane,
                       dep=depi, S=S, clr=clr)

    @staticmethod
    def _fmt(k: tuple) -> Key:
        return "%s:%d" % (k[0], k[1])

    def _cell_xy(self, y: int, x: int) -> Tuple[float, float]:
        """map-frame centre of lattice cell (y, x)"""
        return ((self._cx0 + 2 * x + 1) * RES, (self._cy0 + 2 * y + 1) * RES)

    @staticmethod
    def _lattice_edges(sp):
        ys, xs, idx, nav = sp.ys, sp.xs, sp.idx, sp.nav
        R, C, Ln = [], [], []
        for dy, dx in ((0, 1), (1, 0), (1, 1), (1, -1)):
            y2, x2 = ys + dy, xs + dx
            ok = (y2 >= 0) & (y2 < sp.H) & (x2 >= 0) & (x2 < sp.W)
            ok[ok] &= nav[y2[ok], x2[ok]]
            a = np.nonzero(ok)[0]; b = idx[y2[a], x2[a]]
            if dy and dx:
                keep = nav[ys[a], x2[a]] & nav[y2[a], xs[a]]; a, b = a[keep], b[keep]
            R.append(a); C.append(b); Ln.append(np.full(len(a), math.hypot(dy, dx) * 0.1))
        return np.concatenate(R), np.concatenate(C), np.concatenate(Ln)

    # ------------------------------------------------------------------ links
    @staticmethod
    def _widest_strict(sp, full, A, a, b) -> float:
        """twice the largest t such that anchors a and b stay connected through cells of chart a U chart b with clr >= t,
        a diagonal step needing both orthogonal cells in the threshold set too (a squeeze is not a passage of width t)"""
        cells = np.nonzero((full == a) | (full == b))[0]
        order = cells[np.argsort(-sp.clr[cells], kind="stable")]
        par: Dict[int, int] = {}
        ys, xs, idx, H, W = sp.ys, sp.xs, sp.idx, sp.H, sp.W

        def find(x):
            while par[x] != x:
                par[x] = par[par[x]]; x = par[x]
            return x

        def union(u, v):
            ru, rv = find(u), find(v)
            if ru != rv: par[ru] = rv

        def at(y, x):
            if 0 <= y < H and 0 <= x < W:
                i = int(idx[y, x]); return i if i in par else None
            return None
        ia, ib = int(A[a]), int(A[b])
        for u in order.tolist():
            par[u] = u; y, x = int(ys[u]), int(xs[u])
            for dy, dx in ((0, 1), (0, -1), (1, 0), (-1, 0)):
                v = at(y + dy, x + dx)
                if v is not None: union(u, v)
            for dy in (-1, 1):
                for dx in (-1, 1):
                    o = at(y + dy, x + dx); p = at(y + dy, x); q = at(y, x + dx)
                    if o is not None and p is not None and q is not None:
                        union(u, o); union(p, q)
            if ia in par and ib in par and find(ia) == find(ib):
                return 2.0 * float(sp.clr[u])
        return 0.0

    def _links(self, sp, A, full, edges, chv) -> List[dict]:
        R, C, Ln = edges
        N, k = sp.N, len(A)
        # per chart: shortest routes from the anchor inside the chart (one compact graph per chart)
        ec = np.where(full[R] == full[C], full[R], -1)
        order = np.argsort(ec, kind="stable"); ecs = ec[order]
        cut = np.searchsorted(ecs, np.arange(-1, k + 1))
        cells_of = [np.nonzero(full == j)[0] for j in range(k)]
        D, PRED, LOC = [], [], []
        for j, a in enumerate(A):
            cj = cells_of[j]
            loc = -np.ones(N, np.int64); loc[cj] = np.arange(len(cj))
            e = order[cut[j + 1]:cut[j + 2]]
            n = len(cj)
            g = coo_matrix((np.concatenate([Ln[e], Ln[e]]), (np.concatenate([loc[R[e]], loc[C[e]]]), np.concatenate([loc[C[e]], loc[R[e]]]))),
                           shape=(n, n)).tocsr()
            d, pr = dijkstra(g, directed=False, indices=int(loc[a]), return_predecessors=True)
            D.append(d); PRED.append(pr); LOC.append((loc, cj))
        pairs: Dict[Tuple[int, int], List[Tuple[int, int]]] = {}
        for dy, dx in ((0, 1), (1, 0)):
            y2, x2 = sp.ys + dy, sp.xs + dx
            ok = (y2 < sp.H) & (x2 < sp.W); ok[ok] &= sp.nav[y2[ok], x2[ok]]
            a_ = np.nonzero(ok)[0]; b_ = sp.idx[y2[a_], x2[a_]]
            sel = (full[a_] >= 0) & (full[b_] >= 0) & (full[a_] != full[b_])
            for u, v in zip(a_[sel].tolist(), b_[sel].tolist()):
                ju, jv = int(full[u]), int(full[v])
                if ju > jv: u, v, ju, jv = v, u, jv, ju
                pairs.setdefault((ju, jv), []).append((u, v))

        def route(j, cell):
            loc, cj = LOC[j]
            p = [cell]
            while p[-1] != int(A[j]):
                q = PRED[j][loc[p[-1]]]
                if q < 0: return None
                p.append(int(cj[q]))
            return p[::-1]
        out = []
        for (a, b), es in sorted(pairs.items()):
            m = np.zeros((sp.H, sp.W), bool)
            for u, v in es:
                m[sp.ys[u], sp.xs[u]] = True; m[sp.ys[v], sp.xs[v]] = True
            lab, n = ndimage.label(m, structure=np.ones((3, 3)))
            width = self._widest_strict(sp, full, A, a, b)
            for comp in range(1, n + 1):
                best = None
                for u, v in es:
                    if lab[sp.ys[u], sp.xs[u]] != comp: continue
                    du = D[a][LOC[a][0][u]]; dv = D[b][LOC[b][0][v]]
                    tot = du + dv + math.hypot(sp.ys[u] - sp.ys[v], sp.xs[u] - sp.xs[v]) * 0.1
                    if np.isfinite(tot) and (best is None or tot < best[0]): best = (float(tot), u, v)
                if best is None: continue
                tot, u, v = best; pa = route(a, u); pb = route(b, v)
                if pa is None or pb is None: continue
                cells = pa + pb[::-1]
                integ = {}
                for name, f in chv.items():
                    acc = 0.0
                    for x, y in zip(cells[:-1], cells[1:]):
                        acc += math.hypot(sp.ys[x] - sp.ys[y], sp.xs[x] - sp.xs[y]) * 0.1 * 0.5 * (f[x] + f[y])
                    integ[name] = acc
                out.append(dict(a=a, b=b, length=tot, width=width, u=u, v=v, route=cells, integ=integ))
        return out

    # ------------------------------------------------------------------ dirty
    @staticmethod
    def _signatures(s: Optional[_Solved]):
        """per chart key (anchor cell, cell count, cell checksum) and the set of (a, b, length, width) of the links"""
        sig, link_sig = {}, set()
        if s is not None:
            for j, key in enumerate(s.keys):
                m = s.plane == j
                ys, xs = np.nonzero(m)
                sig[key] = (int(s.sp.ys[s.A[j]]), int(s.sp.xs[s.A[j]]), int(m.sum()), int((ys.astype(np.int64) * 7919 + xs).sum() % 1000003))
            for l in s.links:
                link_sig.add((s.keys[l["a"]], s.keys[l["b"]], round(l["length"], 3), round(l["width"], 3)))
        return sig, link_sig

    def _dirty(self, old: Optional[_Solved], old_sig, old_link_sig, old_keys, bounds, region) -> Dirty:
        s = self._solved
        sig, link_sig = self._signatures(s)
        changed_cells = None
        if s is not None:
            if old is not None and old.plane.shape == s.plane.shape:
                a = np.where(old.plane >= 0, np.array([self._hash(k) for k in old.keys] + [0])[old.plane], 0) if len(old.keys) else np.zeros(s.plane.shape)
                b = np.where(s.plane >= 0, np.array([self._hash(k) for k in s.keys] + [0])[s.plane], 0) if len(s.keys) else np.zeros(s.plane.shape)
                changed_cells = a != b
        self._sig, self._link_sig = sig, link_sig
        changed = sorted(k for k in sig if old_sig.get(k) != sig[k])
        retired = {k: v for k, v in getattr(self, "_retired_map", {}).items()}
        for k in list(retired):
            if k not in {self._fmt(x) for x in old_keys}: retired.pop(k)
        links = sorted({(a, b) for (a, b, _, _) in link_sig - old_link_sig})
        # bbox: the folded windows, plus cells whose chart changed
        x0 = y0 = x1 = y1 = None
        if bounds is not None:
            x0, y0, x1, y1 = bounds[0] * RES, bounds[1] * RES, bounds[2] * RES, bounds[3] * RES
        if region is not None:
            rb = (region[0] * RES, region[1] * RES, region[2] * RES, region[3] * RES)
            x0 = rb[0] if x0 is None else min(x0, rb[0]); y0 = rb[1] if y0 is None else min(y0, rb[1])
            x1 = rb[2] if x1 is None else max(x1, rb[2]); y1 = rb[3] if y1 is None else max(y1, rb[3])
        if changed_cells is not None and changed_cells.any():
            ys, xs = np.nonzero(changed_cells)
            cb = ((self._cx0 + 2 * xs.min()) * RES, (self._cy0 + 2 * ys.min()) * RES,
                  (self._cx0 + 2 * xs.max() + 2) * RES, (self._cy0 + 2 * ys.max() + 2) * RES)
            x0 = cb[0] if x0 is None else min(x0, cb[0]); y0 = cb[1] if y0 is None else min(y0, cb[1])
            x1 = cb[2] if x1 is None else max(x1, cb[2]); y1 = cb[3] if y1 is None else max(y1, cb[3])
        bb = None if x0 is None else BBox(x0, y0, x1, y1)
        return Dirty(bbox=bb, changed=changed, retired=retired, links=links, watermark=self._watermark)

    @staticmethod
    def _hash(key: Key) -> int:
        return (hash(key) & 0x7FFFFFFF) + 1

    # ------------------------------------------------------------------ queries
    def _lat_of(self, x: float, y: float) -> Tuple[int, int]:
        return (int(math.floor(y / RES) - self._cy0) // 2, int(math.floor(x / RES) - self._cx0) // 2)

    def _nearest_cache(self):
        s = self._solved
        if "near" not in self._cache:
            nav = s.plane >= 0
            d, (iy, ix) = ndimage.distance_transform_edt(~nav, return_indices=True)
            self._cache["near"] = (d * self._h, iy, ix)
        return self._cache["near"]

    def snap(self, x: float, y: float, max_dist: Optional[float] = None) -> Snap:
        """The navigable cell nearest (x, y) with the real distance; refused (key None) beyond `max_dist`, never beyond
        sight_range. `crosses_blocked`: the straight segment to it passes a known blocked cell."""
        far = Snap(None, None, None, math.inf)
        s = self._solved
        if s is None or self._V is None: return far
        H, W = s.plane.shape
        cy = int(math.floor(y / RES) - self._cy0) // 2; cx = int(math.floor(x / RES) - self._cx0) // 2
        if not (0 <= cy < H and 0 <= cx < W): return far
        _, iy, ix = self._nearest_cache()
        ty, tx = int(iy[cy, cx]), int(ix[cy, cx])
        j = int(s.plane[ty, tx])
        if j < 0: return far
        px, py = self._cell_xy(ty, tx)
        dist = math.hypot(px - x, py - y)
        limit = self.params.sight_range if max_dist is None else min(max_dist, self.params.sight_range)
        n = max(2, int(dist / (RES / 2)) + 1)
        t = np.linspace(0.0, 1.0, n)
        gx = np.floor((x + (px - x) * t) / RES).astype(int) - self._cx0
        gy = np.floor((y + (py - y) * t) / RES).astype(int) - self._cy0
        ok = (gx >= 0) & (gx < self._V.shape[1]) & (gy >= 0) & (gy < self._V.shape[0])
        v = self._V[gy[ok], gx[ok]]
        crosses = bool(((v >= 100) & (v != 255)).any())
        return Snap(s.keys[j] if dist <= limit else None, px, py, dist, crosses)

    def locate(self, x: float, y: float, max_dist: Optional[float] = None) -> Optional[Key]:
        """Chart owning the navigable cell nearest (x, y) within `max_dist` (default one sight range); None otherwise."""
        return self.snap(x, y, max_dist).key

    def _chart_graph(self):
        s = self._solved
        k = len(s.A)
        M = np.full((k, k), np.inf)
        for l in s.links:
            M[l["a"], l["b"]] = M[l["b"], l["a"]] = min(M[l["a"], l["b"]], l["length"])
        return M

    def _corridor_charts(self, ja: int, jb: int) -> Optional[set]:
        M = self._chart_graph()
        g = np.where(np.isfinite(M), M, 0.0)
        d, pr = dijkstra(g, directed=False, indices=ja, return_predecessors=True)
        if not np.isfinite(d[jb]): return None
        path = [jb]
        while path[-1] != ja: path.append(int(pr[path[-1]]))
        keep = set(path)
        for c in path: keep |= {int(n) for n in np.nonzero(np.isfinite(M[c]))[0]}
        return keep

    def corridor(self, a: Key, b: Key, blocked: Optional[Blocked] = None) -> Optional[List[Tuple[int, int]]]:
        s = self._solved
        if s is None or a not in s.keys or b not in s.keys: return None
        keep = self._corridor_charts(s.keys.index(a), s.keys.index(b))
        if keep is None: return None
        cells = np.isin(s.plane, list(keep))
        ys, xs = np.nonzero(cells)
        gy = ys + self._cy0 // 2; gx = xs + self._cx0 // 2
        return sorted({(int(x) // TILE, int(y) // TILE) for x, y in zip(gx, gy)})

    def _blocked_mask(self, blocked: Blocked) -> np.ndarray:
        s = self._solved
        H, W = s.plane.shape
        yy, xx = np.mgrid[0:H, 0:W]
        cx = (self._cx0 + 2 * xx + 1) * RES; cy = (self._cy0 + 2 * yy + 1) * RES
        m = np.zeros((H, W), bool)
        for x, y, r in blocked.discs:
            m |= (cx - x) ** 2 + (cy - y) ** 2 <= r * r
        for (x0, y0), (x1, y1), hw in blocked.segments:
            dx, dy = x1 - x0, y1 - y0; L2 = dx * dx + dy * dy
            t = np.clip(((cx - x0) * dx + (cy - y0) * dy) / L2, 0, 1) if L2 > 0 else np.zeros_like(cx)
            m |= (cx - (x0 + t * dx)) ** 2 + (cy - (y0 + t * dy)) ** 2 <= hw * hw
        return m

    def route(self, a: Key, b: Key, blocked: Optional[Blocked] = None) -> Optional[Route]:
        """Shortest lattice route between the anchors of charts a and b in navigable space (blocked cells removed).
        Searches the chart corridor first and the whole graph if that finds nothing or a longer way."""
        s = self._solved
        if s is None or a not in s.keys or b not in s.keys: return None
        sp = s.sp; ja, jb = s.keys.index(a), s.keys.index(b)
        src, dst = int(s.A[ja]), int(s.A[jb])
        R, C, Ln = s.edges
        N = sp.N
        alive = np.ones(N, bool)
        if blocked is not None and (blocked.discs or blocked.segments):
            bm = self._blocked_mask(blocked)
            alive = ~bm[sp.ys, sp.xs]
        if not alive[src] or not alive[dst]: return None

        def search(allowed):
            ok = allowed[R] & allowed[C]
            n_ = N
            g = coo_matrix((np.concatenate([Ln[ok], Ln[ok]]), (np.concatenate([R[ok], C[ok]]), np.concatenate([C[ok], R[ok]]))),
                           shape=(n_, n_)).tocsr()
            d, pr = dijkstra(g, directed=False, indices=src, return_predecessors=True)
            return d[dst], pr
        best = None
        keep = self._corridor_charts(ja, jb)
        if keep is not None:
            allowed = alive & np.isin(s.full, list(keep))
            dd, pr = search(allowed)
            if np.isfinite(dd): best = (dd, pr)
        if best is None or blocked is not None:
            dd, pr = search(alive)
            if np.isfinite(dd) and (best is None or dd < best[0] - 1e-9): best = (dd, pr)
        if best is None: return None
        dd, pr = best
        path = [dst]
        while path[-1] != src: path.append(int(pr[path[-1]]))
        path = path[::-1]
        charts: List[Key] = []
        for c in path:
            kk = s.keys[int(s.full[c])]
            if not charts or charts[-1] != kk: charts.append(kk)
        return Route(charts=charts, length=float(dd), min_width=float(2.0 * sp.clr[path].min()),
                     nodes=[self._cell_xy(int(sp.ys[c]), int(sp.xs[c])) for c in path])

    # ------------------------------------------------------------------ persistence hooks (store.py)
    def export_state(self) -> EngineState:
        """The resumable state, by reference (the arrays are not copied: the store slices them into tiles)."""
        p = self.params
        st = EngineState(params=dict(robot_half_side=p.robot_half_side, lattice=p.lattice, place_mult=p.place_mult,
                                     sight_range=p.sight_range),
                         cx0=self._cx0, cy0=self._cy0, V=self._V, T=self._T, B=self._B, chan=dict(self._chan),
                         leaves=[LeafRecord(k, r.session, r.t, r.x, r.y, r.r_d,
                                            None if r.view is None else (r.view.x, r.view.y, r.view.yaw, r.view.fov, r.view.rng))
                                 for k, r in sorted(self._leaves.items(), key=lambda it: (it[1].t, it[0]))],
                         version=self.version, watermark=self._watermark)
        s, tr = self._solved, self._track
        if s is not None and tr is not None:
            st.anchors = [(int(y), int(x)) for y, x in tr.anchors]
            st.keys = list(self._keys)
            st.plane = s.plane; st.dep = s.dep; st.rnd = tr.rnd_img
        return st

    def import_state(self, st: EngineState) -> None:
        """Resume from `st` on a fresh engine: planes and evidence are taken as they are, then one local re-solve (from the
        stored anchors, owner and round planes: nothing changed, so it only rebuilds what is derived) restores charts, links
        and keys. The canvas is padded by MARGIN so that clearance near the stored edge sees unknown, as it did before."""
        p = self.params
        if (st.params["robot_half_side"], st.params["lattice"], st.params["place_mult"], st.params["sight_range"]) != \
                (p.robot_half_side, p.lattice, p.place_mult, p.sight_range):
            raise ValueError("state was made with other parameters")
        if self._V is not None:
            raise ValueError("import_state needs a fresh engine")
        pad = MARGIN
        padf = lambda a, fill: np.pad(a, pad, constant_values=fill)
        padl = lambda a, fill: np.pad(a, pad // 2, constant_values=fill)
        self._cx0, self._cy0 = st.cx0 - pad, st.cy0 - pad
        self._V, self._T, self._B = padf(st.V, 255), padf(st.T, -1), padf(st.B, -1)
        self._chan = {k: (padf(v, 255), padf(t, -1)) for k, (v, t) in st.chan.items()}
        self._leaves = {l.key: _Rec(l.session, l.t, l.x, l.y, l.r_d,
                                    None if l.view is None else CamView(l.key, *l.view)) for l in st.leaves}
        self.version = st.version
        self._watermark = tuple(st.watermark)
        self._cache = {}
        self._keys = [tuple(k) for k in st.keys]
        if not st.anchors or st.plane is None:
            self._track = None; self._solved = None; self._key_plane = None
            self._sig, self._link_sig = {}, set()
            return
        self._key_plane = padl(st.plane.astype(np.int32), -1)
        nav, clr, _ = self._build_F()
        tr = Track(r=p.robot_half_side, mult=p.place_mult, h=self._h, sight_range=p.sight_range)
        tr.anchors = [(y + pad // 2, x + pad // 2) for y, x in st.anchors]
        own = np.where(padl(st.dep, False) | (self._key_plane < 0), -1, self._key_plane).astype(np.int32)
        tr.own_img = own
        tr.rnd_img = padl(st.rnd.astype(np.int32), 1 << 30)
        tr.nav = nav.copy(); tr.clr = clr.copy()
        self._track = tr
        self._solve((0, 0), "")
        self._sig, self._link_sig = self._signatures(self._solved)

    # ----- query / cover
    def _levels(self):
        if "levels" not in self._cache:
            s = self._solved
            self._cache["levels"] = hierarchy(len(s.A), s.links)
        return self._cache["levels"]

    def query(self, bbox: BBox, level: int = 0, raster: bool = True) -> ViewResult:
        s = self._solved
        if s is None:
            return ViewResult(self.version, [], [], None)
        H, W = s.plane.shape
        # lattice window of the bbox
        y0 = max(0, int(math.floor(bbox.y0 / RES) - self._cy0) // 2); y1 = min(H, int(math.ceil(bbox.y1 / RES) - self._cy0) // 2 + 1)
        x0 = max(0, int(math.floor(bbox.x0 / RES) - self._cx0) // 2); x1 = min(W, int(math.ceil(bbox.x1 / RES) - self._cx0) // 2 + 1)
        levels = self._levels()[0]
        sub = s.plane[y0:y1, x0:x1] if y1 > y0 and x1 > x0 else np.zeros((0, 0), np.int32)
        present = set(np.unique(sub[sub >= 0]).tolist())
        group = list(range(len(s.A)))
        node_ids = list(range(len(s.A)))
        if 0 < level < len(levels):
            group = [int(g) for g in levels[level]["parent"]]
            node_ids = [int(n) for n in levels[level]["nodes"]]
        charts = []
        for n in node_ids:
            members = [j for j in range(len(s.A)) if group[j] == n]
            if not (present & set(members)): continue
            c = s.charts[n]
            parents = {}
            for L_ in range(1, len(levels)):
                parents[L_] = s.keys[int(levels[L_]["parent"][n])]
            if level == 0:
                charts.append(Chart(c.key, c.anchor, c.cells, c.area_m2, c.clearance, c.driven_frac, c.ever_blocked_frac, c.leaf, parents))
            else:
                cs = [s.charts[j] for j in members]
                cells = sum(x.cells for x in cs)
                charts.append(Chart(c.key, c.anchor, cells, cells * self._h ** 2, c.clearance,
                                    sum(x.driven_frac * x.cells for x in cs) / max(cells, 1),
                                    sum(x.ever_blocked_frac * x.cells for x in cs) / max(cells, 1), c.leaf, parents))
        shown = {c.key for c in charts}
        links = []
        if level == 0:
            for l in s.links:
                ka, kb = s.keys[l["a"]], s.keys[l["b"]]
                if ka in shown and kb in shown:
                    links.append(self._link(l, ka, kb))
        else:
            agg: Dict[Tuple[int, int], dict] = {}
            for l in s.links:
                ga, gb = group[l["a"]], group[l["b"]]
                if ga == gb: continue
                key = (min(ga, gb), max(ga, gb))
                if key not in agg or l["length"] < agg[key]["length"]: agg[key] = dict(l, a=key[0], b=key[1])
                agg[key]["width"] = max(agg[key]["width"], l["width"])
            for l in agg.values():
                ka, kb = s.keys[l["a"]], s.keys[l["b"]]
                if ka in shown and kb in shown: links.append(self._link(l, ka, kb))
        rast = None
        if raster and sub.size:
            keys = [c.key for c in charts]
            pos = {s.keys.index(k_): i + 1 for i, k_ in enumerate(keys)}
            if level > 0:
                lut = {j: pos.get(group[j], 0) for j in range(len(s.A))}
            else:
                lut = {j: pos.get(j, 0) for j in range(len(s.A))}
            idx = np.zeros(sub.shape, np.uint16)
            for j, i in lut.items():
                if i: idx[sub == j] = i
            rast = OwnerRaster(origin=((self._cx0 + 2 * x0) * RES, (self._cy0 + 2 * y0) * RES), step=self._h, index=idx, keys=keys,
                               dependent=s.dep[y0:y1, x0:x1].copy())
        return ViewResult(self.version, charts, links, rast)

    def _link(self, l: dict, ka: Key, kb: Key) -> Link:
        s = self._solved
        pu = self._cell_xy(int(s.sp.ys[l["u"]]), int(s.sp.xs[l["u"]])); pv = self._cell_xy(int(s.sp.ys[l["v"]]), int(s.sp.xs[l["v"]]))
        return Link(a=ka, b=kb, length=l["length"], width=l["width"], portal=(pu, pv), channels=dict(l["integ"]))

    def cover(self, keys: Sequence[Key], k: int = 2) -> Tuple[List[str], float, bool]:
        """Minimal set of leaf keys whose camera views k-cover the given charts and the blocked cells bounding them."""
        s = self._solved
        if s is None: return [], 0.0, True
        idx = [s.keys.index(x) for x in keys if x in s.keys]
        if not idx: return [], 0.0, True
        H, W = s.plane.shape
        m = np.isin(s.plane, idx)
        v = self._V
        solid_full = (v >= 100) & (v != 255)
        free_full = (v != 255) & (v < 100)
        solid_l = np.pad(solid_full, ((0, H * 2 - v.shape[0]), (0, W * 2 - v.shape[1]))).reshape(H, 2, W, 2).any((1, 3))
        bound = ndimage.binary_dilation(m, structure=np.ones((3, 3))) & solid_l & ~m
        wanted = np.nonzero((m | bound).ravel())[0]
        views = [r.view for r in self._leaves.values() if r.view is not None]
        views.sort(key=lambda x: x.key)
        out, ceiling, optimal = select_views(views, free_full, solid_full, RES, (self._cx0 * RES, self._cy0 * RES), wanted, W, k=k)
        return sorted(out), float(ceiling), bool(optimal)
