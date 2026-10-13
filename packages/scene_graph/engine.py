"""Engine behind `api.SceneGraph`: the free-space charts.

    fold     per cell lexicographic max of (capture time, value); unknown carries no key. Commutative, associative,
             idempotent: leaves may arrive in any order, twice, in any batches. `ever_blocked` is the latest capture time at
             which a cell was reported blocked (max-merge); optional channels fold the same way, one plane pair each.
    F(r)     navigable cells: components of (square fits at one of 23 headings  U  driven-and-free W(r)) that touch where a
             robot has been (G). Evidence is the driven polyline of each session, tagged with the half side r_d of its robot.
    charts   the persistence (Morse) partition of F (morse.py): one region per surviving maximum of the distance to the
             non-navigable cells, a pure function of F; persistent keys by overlap with the previous owner plane (chart_keys).
    links    one per arc of the interface between two regions; the portal is the crossing attaining the saddle, `width` the
             physical passage width 2 s + 2 r - h, `length` (the cost) the route anchor -> portal -> anchor inside the two
             regions, and the line integral of every channel along that route.
    levels   coarser partitions by merging adjacent regions of persistence below tau_0 * 2^j (nested by construction); a pure
             function of the charts' clearance and the links' width (morse.levels), so a stored version answers the same.
The graph carries evidence and length, never a price. Pure numpy / scipy, no I/O.

Not implemented here: sparse / tiled in-memory planes (one dense canvas that grows), local F construction (F and the partition
are rebuilt for the whole canvas per batch), persistence (store.py), the leaf-delete winner plane (refold rebuilds the bbox
from the leaves the caller supplies).
"""
import hashlib
import bisect
import math
import sys
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
from scipy import ndimage
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import dijkstra

from .api import (BBox, Blocked, Chart, Dirty, EngineFaulted, Key, Leaf, LeafError, Link, OwnerRaster, Route, SceneGraph,
                  SceneParams, Snap, ViewResult)
from . import morse
from .chart_keys import assign_A, overlaps
from .leafcover import View as CamView
from .leafcover import select_views
from .state import EngineState, LeafRecord, StateMismatch
from .window import to_lattice

RES = 0.05                      # costmap cell (m)
LAT = 2                         # costmap cells per lattice cell (lattice = LAT * RES): the 2:1 assumption lives in this constant
                                # and in the places that do index arithmetic on it (`// 2`, `2 * x`, tiles in store.py)
HEADINGS = 23                   # corner shift r * sqrt2 * dtheta < res / 2 at 90 / 23 degrees
TILE = 64                       # lattice cells per tile side (corridor tiles)
MARGIN = 40                     # canvas growth margin (full-resolution cells)
RESERVED = ("undriven", "ever_blocked", "soft")
ROUTE_UNIT_M = 1e-4        # route cost unit: an edge costs ceil(length / unit) so path costs are exact integers
MAX_WINDOW_M = 20.0         # input validation: a leaf window side above this is refused
POSE_SLACK_M = 5.0          # input validation: a pose may lie this far outside its window's footprint


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
    sp: morse.Space             # the cells of F; sp.clr is the physical clearance (m) of each
    A: List[int]                # anchor cell (index into sp) of each chart
    full: np.ndarray            # chart index per cell of sp
    keys: List[Key]
    charts: List[Chart]
    links: List[dict]
    edges: Tuple[np.ndarray, np.ndarray, np.ndarray]
    plane: np.ndarray           # canvas image: chart index per lattice cell, -1 outside
    dep: np.ndarray             # canvas image, all False: every cell of a chart is its own (kept for the OwnerRaster contract)
    S: np.ndarray               # driven (body covered) image
    peak: np.ndarray            # largest B (distance to the cells outside F) in each chart: the persistence peak
    region: np.ndarray          # persistence region (0..) each chart was cut from


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
    """cells of the digital straight line, ties rounded up in exact integer arithmetic: the line of a translated pair is the
    translated line (np.rint rounds half to even, so the line depended on the canvas origin)"""
    n = max(abs(y1 - y0), abs(x1 - x0), 1); k = np.arange(n + 1, dtype=np.int64)
    return y0 + ((y1 - y0) * k * 2 + n) // (2 * n), x0 + ((x1 - x0) * k * 2 + n) // (2 * n)


class ChartGraph(SceneGraph):
    # A safety bound, not geometry: the dense canvas allocates ~17 B per full-resolution cell, so one leaf with an absurd pose
    # must be refused (LeafError) instead of allocating gigabytes. 2**28 cells = 0.67 km^2 at 0.05 m = ~4.5 GB of planes.
    max_canvas_cells = 1 << 28

    def __init__(self, params: SceneParams = SceneParams()):
        super().__init__(params)
        self.faulted: Optional[BaseException] = None   # set when an exception escaped after the state began to change
        if params.chart_radius < params.robot_half_side - 1e-12:
            raise ValueError("chart_radius below the robot half side")
        self.version = 0
        self._h = params.lattice
        self._leaves: Dict[str, _Rec] = {}
        self._leaf_order: List[Tuple[int, str]] = []      # (capture time, key), kept sorted: export_state never sorts
        self._leaf_recs: Dict[str, LeafRecord] = {}       # exported records, reused until the leaf changes
        self._cx0 = self._cy0 = 0               # full-resolution global cell index of canvas cell (0, 0); even
        self._V = self._T = self._B = None
        self._chan: Dict[str, Tuple[np.ndarray, np.ndarray]] = {}
        self._tau = morse.tau0(params.lattice) * morse.BASE_TAU
        self._ccache: dict = {}                    # morse.subdivide: the regions of the last solve
        self._radius = max(params.chart_radius, params.lattice)
        self._dcache = None                     # (free, distance transform of free in cells) of the last F build
        self._key_plane: Optional[np.ndarray] = None
        self._keys: List[Key] = []
        self._solved: Optional[_Solved] = None
        self._watermark: Tuple[int, str] = (-1, "")
        self._sig: Dict[Key, tuple] = {}
        self._link_sig: set = set()
        self._cache: dict = {}
        self._shift = (0, 0)                    # canvas growth of the batch being solved (lattice cells)
        self._ecache = None                     # (free, square-fits) of the last F build
        self._pl = None                         # driven evidence of the last F build: last point per session and the G / W / S images
        self._pl_new: List[str] = []            # leaves added since then (appended to a session in capture order => incremental)
        self._pl_full = False                   # a leaf was removed or changed: rebuild the evidence
        self._rcache: dict = {}                 # routes from the anchor inside a chart, by chart content

    # ------------------------------------------------------------------ canvas and fold
    def _plan_canvas(self, gx0: int, gy0: int, gx1: int, gy1: int):
        """None when [gx0, gx1) x [gy0, gy1) fits; else the new canvas geometry. Raises LeafError above max_canvas_cells."""
        if self._V is None:
            cx0 = (gx0 - MARGIN) // 2 * 2; cy0 = (gy0 - MARGIN) // 2 * 2
            W = (gx1 + MARGIN - cx0 + 1) // 2 * 2; H = (gy1 + MARGIN - cy0 + 1) // 2 * 2
            plan = dict(fresh=True, cx0=cx0, cy0=cy0, H=H, W=W, oy=0, ox=0, h=0, w=0)
        else:
            h, w = self._V.shape
            if gx0 >= self._cx0 and gy0 >= self._cy0 and gx1 <= self._cx0 + w and gy1 <= self._cy0 + h:
                return None
            nx0 = min(self._cx0, (gx0 - MARGIN) // 2 * 2); ny0 = min(self._cy0, (gy0 - MARGIN) // 2 * 2)
            nx1 = max(self._cx0 + w, gx1 + MARGIN); ny1 = max(self._cy0 + h, gy1 + MARGIN)
            plan = dict(fresh=False, cx0=nx0, cy0=ny0, H=(ny1 - ny0 + 1) // 2 * 2, W=(nx1 - nx0 + 1) // 2 * 2,
                        oy=self._cy0 - ny0, ox=self._cx0 - nx0, h=h, w=w)
        if plan["H"] * plan["W"] > self.max_canvas_cells:
            raise LeafError(None, f"the batch needs a canvas of {plan['H']} x {plan['W']} cells, above the bound "
                                  f"{self.max_canvas_cells} (an absurd pose?)")
        return plan

    def _grow_canvas(self, plan):
        """Allocate the grown planes WITHOUT touching the state (a MemoryError leaves the engine as it was)."""
        H, W, oy, ox, h, w = plan["H"], plan["W"], plan["oy"], plan["ox"], plan["h"], plan["w"]
        if plan["fresh"]:
            return dict(V=np.full((H, W), 255, np.uint8), T=-np.ones((H, W), np.int64), B=-np.ones((H, W), np.int64),
                        chan={}, kp=None)

        def grow(a, fill):
            b = np.full((H, W), fill, a.dtype); b[oy:oy + h, ox:ox + w] = a; return b
        out = dict(V=grow(self._V, 255), T=grow(self._T, -1), B=grow(self._B, -1),
                   chan={k: (grow(v, 255), grow(t, -1)) for k, (v, t) in self._chan.items()}, kp=None)
        if self._key_plane is not None:
            kp = np.full((H // 2, W // 2), -1, self._key_plane.dtype)
            kp[oy // 2:oy // 2 + h // 2, ox // 2:ox // 2 + w // 2] = self._key_plane; out["kp"] = kp
        return out

    def _commit_canvas(self, plan, arrays) -> Tuple[int, int]:
        self._V, self._T, self._B, self._chan = arrays["V"], arrays["T"], arrays["B"], arrays["chan"]
        if arrays["kp"] is not None: self._key_plane = arrays["kp"]
        self._cx0, self._cy0 = plan["cx0"], plan["cy0"]
        return (0, 0) if plan["fresh"] else (plan["oy"] // 2, plan["ox"] // 2)

    def _ensure(self, gx0: int, gy0: int, gx1: int, gy1: int) -> Tuple[int, int]:
        """grow the canvas to hold [gx0, gx1) x [gy0, gy1); returns the shift (rows, cols) in LATTICE cells of old content"""
        plan = self._plan_canvas(gx0, gy0, gx1, gy1)
        return (0, 0) if plan is None else self._commit_canvas(plan, self._grow_canvas(plan))

    @staticmethod
    def _fold_into(V, T, w, gx, gy, cx0, cy0, t, B=None):
        H, W = V.shape
        ox, oy = gx - cx0, gy - cy0
        ys0, xs0 = max(0, oy), max(0, ox); ys1, xs1 = min(H, oy + w.shape[0]), min(W, ox + w.shape[1])
        if ys0 >= ys1 or xs0 >= xs1: return None
        sub = w[ys0 - oy:ys1 - oy, xs0 - ox:xs1 - ox]
        Tc, Vc = T[ys0:ys1, xs0:xs1], V[ys0:ys1, xs0:xs1]
        kn = sub != 255
        win = kn & ((Tc < 0) | (t > Tc) | ((t == Tc) & (sub > Vc)))     # ties: the larger (blocked) value
        Tc[win] = t; Vc[win] = sub[win]
        if B is not None:
            Bc = B[ys0:ys1, xs0:xs1]
            bl = kn & (sub >= 100)
            new_b = bl & (t > Bc)
            Bc[bl] = np.maximum(Bc[bl], t)
            win = win | new_b
        if not win.any(): return None                                   # a replay of what is folded changes nothing
        ys, xs = np.nonzero(win)
        return (int(xs.min()) + xs0, int(ys.min()) + ys0, int(xs.max()) + 1 + xs0, int(ys.max()) + 1 + ys0)

    def _fold_leaf(self, lf: Leaf) -> Tuple[int, int, int, int]:
        w, (x0, y0) = to_lattice(lf.window_u8, lf.resolution, lf.origin, lf.origin_yaw, RES)
        gx, gy = int(round(x0 / RES)), int(round(y0 / RES))
        chans = {k: to_lattice(v, lf.resolution, lf.origin, lf.origin_yaw, RES)[0] for k, v in lf.channels.items()
                 if k not in RESERVED and v is not None}
        return gx, gy, w, chans

    def _prepare(self, leaves: Sequence[Leaf]) -> list:
        """VALIDATION PHASE. Check and rasterise every leaf of the batch before anything is mutated: a LeafError here leaves
        the engine bit-identical, so the caller may retry or bisect the batch. A window side is capped at MAX_WINDOW_M
        (a leaf window is 10 x 10 m by convention; the cap leaves room for other sensors)."""
        cap_m = MAX_WINDOW_M
        out: list = []; seen: Dict[str, Leaf] = {}
        fin = lambda v: isinstance(v, (int, float, np.integer, np.floating)) and not isinstance(v, bool) and math.isfinite(v)
        for lf in leaves:
            k = getattr(lf, "key", None)

            def bad(why, k=k):
                raise LeafError(k, why)
            if not isinstance(k, str) or not k: bad("key must be a non-empty string")
            if k in seen:
                o = seen[k]
                if (o.stamp_ms, o.x, o.y, o.session, o.r_d, tuple(o.origin), o.resolution, o.origin_yaw) != \
                        (lf.stamp_ms, lf.x, lf.y, lf.session, lf.r_d, tuple(lf.origin), lf.resolution, lf.origin_yaw) \
                        or o.window_u8.shape != lf.window_u8.shape or not np.array_equal(o.window_u8, lf.window_u8):
                    bad("duplicate key with different content in one batch")
                continue
            seen[k] = lf
            w = lf.window_u8
            if not isinstance(w, np.ndarray) or w.ndim != 2 or w.dtype != np.uint8: bad("window_u8 must be a 2-D uint8 array")
            if w.size == 0: bad("empty window")
            if not isinstance(lf.stamp_ms, (int, np.integer)) or isinstance(lf.stamp_ms, bool): bad("stamp_ms must be an integer")
            if lf.session is not None and not isinstance(lf.session, str): bad("session must be a string")
            for name in ("x", "y", "yaw", "resolution", "r_d", "origin_yaw"):
                if not fin(getattr(lf, name)): bad(f"{name} must be a finite number")
            if not (isinstance(lf.origin, (tuple, list)) and len(lf.origin) == 2 and fin(lf.origin[0]) and fin(lf.origin[1])):
                bad("origin must be two finite numbers")
            if lf.resolution <= 0: bad("resolution must be > 0")
            if lf.r_d <= 0: bad("r_d must be > 0")
            if max(w.shape) * lf.resolution > cap_m:
                bad(f"window side {max(w.shape) * lf.resolution:.1f} m exceeds {cap_m:.1f} m (MAX_WINDOW_M)")
            for name, cv in (lf.channels or {}).items():
                if name in RESERVED or cv is None: continue
                if not isinstance(cv, np.ndarray) or cv.ndim != 2 or cv.dtype != np.uint8 or cv.shape != w.shape:
                    bad(f"channel {name!r} must be a 2-D uint8 array of the window's shape")
            if lf.view is not None and not all(fin(getattr(lf.view, a)) for a in ("x", "y", "yaw", "fov", "rng")):
                bad("view must have finite fields")
            try:
                gx, gy, wl, chans = self._fold_leaf(lf)
            except Exception as e:                                   # noqa: BLE001 - any resampling failure is the leaf's
                bad(f"cannot resample the window: {e!r}")
            # a pose joins the driven polyline: it must belong to its window. The convention is a window centred on the pose
            # (+- 5 m); POSE_SLACK_M of slack around the footprint admits an off-centre window, nothing more
            sr = POSE_SLACK_M
            ex = max(gx * RES - lf.x, lf.x - (gx + wl.shape[1]) * RES, gy * RES - lf.y, lf.y - (gy + wl.shape[0]) * RES)
            if ex > sr:
                bad(f"pose is {ex:.1f} m outside its window's footprint (bound: {sr:.1f} m)")
            out.append((lf, wl, gx, gy, chans))
        return out

    # ------------------------------------------------------------------ public: fold / refold
    def fold_many(self, leaves: Sequence[Leaf]) -> Dirty:
        return self._apply(leaves, None)

    def refold(self, leaves: Sequence[Leaf], bbox: BBox) -> Dirty:
        """Rebuild the area of `bbox` from `leaves`, which must be every leaf overlapping it that still exists: cells in
        the box are reset, a stored leaf whose pose lies in the box and is not supplied is a deleted one."""
        return self._apply(leaves, bbox)

    def _check(self) -> None:
        if self.faulted is not None: raise EngineFaulted(self.faulted)

    def _apply(self, leaves: Sequence[Leaf], bbox: Optional[BBox]) -> Dirty:
        self._check()
        # ---- validation: nothing is mutated until every leaf is checked, resampled and the canvas is allocated
        if bbox is not None and not all(isinstance(v, (int, float, np.integer, np.floating)) and math.isfinite(v)
                                        for v in (bbox.x0, bbox.y0, bbox.x1, bbox.y1)):
            raise LeafError(None, "bbox must be finite")
        prepared = self._prepare(leaves)
        bounds = None
        for _, wl, gx, gy, _ in prepared:
            b = (gx, gy, gx + wl.shape[1], gy + wl.shape[0])
            bounds = b if bounds is None else (min(bounds[0], b[0]), min(bounds[1], b[1]), max(bounds[2], b[2]), max(bounds[3], b[3]))
        region = None
        if bbox is not None and self._V is not None:
            gx0, gy0 = int(math.floor(bbox.x0 / RES)), int(math.floor(bbox.y0 / RES))
            gx1, gy1 = int(math.ceil(bbox.x1 / RES)), int(math.ceil(bbox.y1 / RES))
            region = (gx0, gy0, gx1, gy1)
        if not prepared and region is None:
            return Dirty(None, [], {}, [], self._watermark)
        plan = grown = None
        if bounds is not None:
            plan = self._plan_canvas(*bounds)                        # LeafError above the canvas bound
            if plan is not None: grown = self._grow_canvas(plan)     # a MemoryError here changes nothing
        # ---- mutation: from here an escaping exception leaves the state undefined => the engine is FAULTED
        old = self._solved
        old_sig, old_link_sig, old_keys = dict(self._sig), set(self._link_sig), list(self._keys)
        try:
            return self._mutate(leaves, bbox, prepared, bounds, region, plan, grown, old, old_sig, old_link_sig, old_keys)
        except BaseException as e:                                   # noqa: BLE001
            self.faulted = e
            raise

    def _mutate(self, leaves, bbox, prepared, bounds, region, plan, grown, old, old_sig, old_link_sig, old_keys) -> Dirty:
        self._pending = []
        if bbox is not None and self._V is not None:
            # a stored leaf of the area that was not supplied is a deleted leaf
            keep = {lf.key for lf in leaves}
            for k in [k for k, r in self._leaves.items()
                      if bbox.x0 <= r.x <= bbox.x1 and bbox.y0 <= r.y <= bbox.y1 and k not in keep]:
                self._leaf_del(k); self._pl_full = True
        shift = (0, 0)
        if plan is not None:
            shift = self._commit_canvas(plan, grown)
        if region is not None and self._V is not None:
            x0, y0 = max(region[0] - self._cx0, 0), max(region[1] - self._cy0, 0)
            x1, y1 = min(region[2] - self._cx0, self._V.shape[1]), min(region[3] - self._cy0, self._V.shape[0])
            if x0 < x1 and y0 < y1:
                self._V[y0:y1, x0:x1] = 255; self._T[y0:y1, x0:x1] = -1; self._B[y0:y1, x0:x1] = -1
                for v, t in self._chan.values():
                    v[y0:y1, x0:x1] = 255; t[y0:y1, x0:x1] = -1
        touched = None                                   # global full-resolution cells whose value, time or blocked time changed

        def grow_touched(b):
            nonlocal touched
            if b is None: return
            g = (b[0] + self._cx0, b[1] + self._cy0, b[2] + self._cx0, b[3] + self._cy0)
            touched = g if touched is None else (min(touched[0], g[0]), min(touched[1], g[1]), max(touched[2], g[2]), max(touched[3], g[3]))
        for lf, w, gx, gy, chans in prepared:
            grow_touched(self._fold_into(self._V, self._T, w, gx, gy, self._cx0, self._cy0, lf.stamp_ms, self._B))
            for name, cw in chans.items():
                if name not in self._chan:
                    self._chan[name] = (np.full(self._V.shape, 255, np.uint8), -np.ones(self._V.shape, np.int64))
                v, t = self._chan[name]
                grow_touched(self._fold_into(v, t, cw, gx, gy, self._cx0, self._cy0, lf.stamp_ms))
            rec = _Rec(lf.session, lf.stamp_ms, lf.x, lf.y, lf.r_d,
                       None if lf.view is None else CamView(lf.key, lf.view.x, lf.view.y, lf.view.yaw, lf.view.fov, lf.view.rng))
            old_rec = self._leaves.get(lf.key)
            if old_rec is None: self._pl_new.append(lf.key)
            elif (old_rec.session, old_rec.t, old_rec.x, old_rec.y, old_rec.r_d) != (rec.session, rec.t, rec.x, rec.y, rec.r_d): self._pl_full = True
            self._leaf_put(lf.key, rec)
            self._watermark = max(self._watermark, (lf.stamp_ms, lf.key))
        birth = max(((lf.stamp_ms, lf.key) for lf in leaves), default=self._watermark)[1]
        # the fold counter moves only when the state did: a cell, a new or changed leaf record, a refold (a replay is a no-op)
        moved = touched is not None or bool(self._pl_new) or self._pl_full or region is not None
        self._cache = {}
        self._shift = shift
        self._solve(shift, birth)
        if moved: self.version += 1
        # the dirty box is where cells actually changed (and the area of a refold), not where windows were laid down
        return self._dirty(old, old_sig, old_link_sig, old_keys, touched, region)

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
                pts.append(((gx - self._cx0) // 2, (gy - self._cy0) // 2, round(r.r_d, 4)))
            for i, (x, y, d) in enumerate(pts):
                m = out.setdefault(d, np.zeros((H, W), bool))
                if 0 <= y < H and 0 <= x < W: m[y, x] = True
                if i + 1 < len(pts) and pts[i + 1][2] == d:
                    xx, yy, _ = pts[i + 1]
                    rr, cc = _line(y, x, yy, xx); ok = (rr >= 0) & (rr < H) & (cc >= 0) & (cc < W); m[rr[ok], cc[ok]] = True
        return out

    def _erode(self, free: np.ndarray, shift: Tuple[int, int]) -> np.ndarray:
        """cells where the robot square fits at one of the headings.  An erosion reads `free` only inside the square's support, so a cell of
        the previous result stays exact unless `free` changed within that support: recompute the changed box widened by the support (plus
        the support again, so the inner part is exact) and splice it into the previous result.  New canvas area is unknown (not free), which
        is what the border value assumed before the canvas grew."""
        sqs = _squares(self.params.robot_half_side)
        c, self._ecache = self._ecache, None
        prev = None
        if c is not None:
            fp, sp_ = c
            if fp.shape == free.shape:
                prev = c
            else:
                oy, ox = 2 * shift[0], 2 * shift[1]
                if oy + fp.shape[0] <= free.shape[0] and ox + fp.shape[1] <= free.shape[1]:
                    f2 = np.zeros_like(free); f2[oy:oy + fp.shape[0], ox:ox + fp.shape[1]] = fp
                    s2 = np.zeros_like(free); s2[oy:oy + fp.shape[0], ox:ox + fp.shape[1]] = sp_
                    prev = (f2, s2)
        if prev is None:
            sq = np.zeros_like(free)
            for st in sqs:
                sq |= ndimage.binary_erosion(free, structure=st, border_value=0)
        else:
            fp, sp_ = prev
            d = fp != free
            if not d.any():
                sq = sp_
            else:
                H, W = free.shape
                pad = max(max(st.shape) for st in sqs)
                rows = np.nonzero(d.any(1))[0]; cols = np.nonzero(d.any(0))[0]
                y0, y1 = max(0, rows[0] - pad), min(H, rows[-1] + 1 + pad)
                x0, x1 = max(0, cols[0] - pad), min(W, cols[-1] + 1 + pad)
                wy0, wy1, wx0, wx1 = max(0, y0 - pad), min(H, y1 + pad), max(0, x0 - pad), min(W, x1 + pad)
                win = free[wy0:wy1, wx0:wx1]
                e = np.zeros_like(win)
                for st in sqs:
                    e |= ndimage.binary_erosion(win, structure=st, border_value=0)
                sq = sp_.copy()
                sq[y0:y1, x0:x1] = e[y0 - wy0:y1 - wy0, x0 - wx0:x1 - wx0]
        self._ecache = (free, sq)
        return sq

    def _clearance_cells(self, free: np.ndarray, shift: Tuple[int, int]) -> np.ndarray:
        """`distance_transform_edt(free)`, recomputed only where it can have changed.  A cell outside the changed box grown by R (the largest
        distance of the previous map) keeps its value: its nearest blocked cell was within R and is untouched, and a newly blocked cell is
        farther than R.  Inside, the transform of a window is exact for every cell whose value does not exceed its distance to the window's
        edge (anything blocked outside the window is farther than that); the window widens until that holds for the whole box.  New canvas
        area counts as changed (it was not part of the previous transform)."""
        c, self._dcache = self._dcache, None
        if c is None:
            dt = ndimage.distance_transform_edt(free)
            self._dcache = (free, dt)
            return dt
        fp, dp = c
        H, W = free.shape
        newarea = None
        if fp.shape != free.shape:
            oy, ox = 2 * shift[0], 2 * shift[1]
            if oy + fp.shape[0] > H or ox + fp.shape[1] > W:
                dt = ndimage.distance_transform_edt(free); self._dcache = (free, dt); return dt
            f2 = np.zeros_like(free); f2[oy:oy + fp.shape[0], ox:ox + fp.shape[1]] = fp
            d2 = np.zeros(free.shape); d2[oy:oy + fp.shape[0], ox:ox + fp.shape[1]] = dp
            newarea = np.ones(free.shape, bool); newarea[oy:oy + fp.shape[0], ox:ox + fp.shape[1]] = False
            fp, dp = f2, d2
        ch = fp != free
        if newarea is not None: ch |= newarea
        if not ch.any():
            self._dcache = (free, dp)
            return dp
        rows = np.nonzero(ch.any(1))[0]; cols = np.nonzero(ch.any(0))[0]
        R = int(math.ceil(float(dp.max()))) + 1
        ey0, ey1 = max(0, rows[0] - R), min(H, rows[-1] + 1 + R)
        ex0, ex1 = max(0, cols[0] - R), min(W, cols[-1] + 1 + R)
        rw = R + 1
        while True:
            wy0, wy1, wx0, wx1 = max(0, ey0 - rw), min(H, ey1 + rw), max(0, ex0 - rw), min(W, ex1 + rw)
            dtw = ndimage.distance_transform_edt(free[wy0:wy1, wx0:wx1])
            sub = dtw[ey0 - wy0:ey1 - wy0, ex0 - wx0:ex1 - wx0]
            yy = np.arange(ey0, ey1)[:, None]; xx = np.arange(ex0, ex1)[None, :]
            inf = np.inf
            bound = np.minimum(np.minimum(np.where(wy0 > 0, yy - wy0 + 1.0, inf), np.where(wy1 < H, wy1 - yy, inf)),
                               np.minimum(np.where(wx0 > 0, xx - wx0 + 1.0, inf), np.where(wx1 < W, wx1 - xx, inf)))
            full_window = wy0 == 0 and wy1 == H and wx0 == 0 and wx1 == W
            if full_window or bool((sub <= bound).all()): break
            rw *= 2
        dt = dp.copy()
        dt[ey0:ey1, ex0:ex1] = sub
        self._dcache = (free, dt)
        return dt

    def _disc(self, thr: float) -> np.ndarray:
        """offsets whose distance, computed exactly as the distance transform does (sqrt of the integer square, times the lattice step), is within thr"""
        if not hasattr(self, "_cache_disc"): self._cache_disc = {}
        if self._cache_disc.get(thr) is None:
            R = int(math.ceil(thr / self._h)) + 1
            dy, dx = np.mgrid[-R:R + 1, -R:R + 1]
            self._cache_disc[thr] = np.sqrt((dy * dy + dx * dx).astype(np.float64)) * self._h <= thr
        return self._cache_disc[thr]

    def _evidence(self, shape: Tuple[int, int], shift: Tuple[int, int]):
        """G (reachability), W (proven for this robot), S (body covered) images from the driven polylines, and P: W of the leaf POSES alone
        (a pose is a measured position, the chord between two poses is an interpolation; P is where an unknown cell is proven free).  Full: the distance transform of each
        robot's polyline raster.  Incremental: leaves only appended to their sessions in capture order add cells to the rasters, and each added
        cell adds exactly its disc to G, W and S (a union of discs is what thresholding the transform of a raster is)."""
        r, h = self.params.robot_half_side, self._h
        H, Wd = shape
        st = self._pl
        new_keys, full = self._pl_new, self._pl_full
        self._pl_new, self._pl_full = [], False
        if st is not None and not full:
            if st["shape"] != shape:
                sy, sx = shift
                oH, oW = st["shape"]
                # a pose cell is floor((g - cx0) / 2) with cx0 even: a canvas shift by k lattice cells moves it by exactly k
                if sy + oH > H or sx + oW > Wd: st = None
                else:
                    def pad(a):
                        b = np.zeros(shape, bool); b[sy:sy + oH, sx:sx + oW] = a; return b
                    st = dict(shape=shape, G=pad(st["G"]), W=pad(st["W"]), S=pad(st["S"]), P=pad(st["P"]),
                              last={k: (t, key, x + sx, y + sy, d) for k, (t, key, x, y, d) in st["last"].items()})
        if st is not None and not full:
            by_s: Dict[str, list] = {}
            for k in new_keys:
                r_ = self._leaves.get(k)
                if r_ is not None: by_s.setdefault(r_.session, []).append((r_.t, k, r_))
            ok = True
            for sess, items in by_s.items():
                items.sort(key=lambda it: (it[0], it[1]))
                lt = st["last"].get(sess)
                if lt is not None and (items[0][0], items[0][1]) <= (lt[0], lt[1]): ok = False; break
            if ok:
                G, Wm, S, Pm = st["G"], st["W"], st["S"], st["P"]
                last = dict(st["last"])
                for sess, items in by_s.items():
                    for t, k, r_ in items:
                        gx, gy = int(math.floor(r_.x / RES)), int(math.floor(r_.y / RES))
                        x, y, d = (gx - self._cx0) // 2, (gy - self._cy0) // 2, round(r_.r_d, 4)
                        cells = []
                        if 0 <= y < H and 0 <= x < Wd:
                            cells.append((np.array([y]), np.array([x])))
                            if d >= r - 1e-9:                      # the pose alone: W of a point
                                se = self._disc((d - r + h) + 1e-9); R = se.shape[0] // 2
                                y0, y1, x0, x1 = max(0, y - R), min(H, y + R + 1), max(0, x - R), min(Wd, x + R + 1)
                                crop = np.zeros((y1 - y0, x1 - x0), bool); crop[y - y0, x - x0] = True
                                Pm[y0:y1, x0:x1] |= ndimage.binary_dilation(crop, structure=se)
                        lt = last.get(sess)
                        if lt is not None and lt[4] == d:
                            rr, cc = _line(lt[3], lt[2], y, x)
                            m = (rr >= 0) & (rr < H) & (cc >= 0) & (cc < Wd)
                            cells.append((rr[m], cc[m]))
                        last[sess] = (t, k, x, y, d)
                        if not cells: continue
                        ry = np.concatenate([c[0] for c in cells]); rx = np.concatenate([c[1] for c in cells])
                        for thr, tgt in ((h + 1e-9, G), (d + 1e-9, S)) + (((d - r + h) + 1e-9, Wm),) * (d >= r - 1e-9):
                            se = self._disc(thr); R = se.shape[0] // 2
                            y0, y1, x0, x1 = max(0, ry.min() - R), min(H, ry.max() + R + 1), max(0, rx.min() - R), min(Wd, rx.max() + R + 1)
                            crop = np.zeros((y1 - y0, x1 - x0), bool); crop[ry - y0, rx - x0] = True
                            tgt[y0:y1, x0:x1] |= ndimage.binary_dilation(crop, structure=se)
                self._pl = dict(shape=shape, G=G, W=Wm, S=S, P=Pm, last=last)
                return G, Wm, S, Pm
        # full rebuild
        G = np.zeros(shape, bool); Wm = np.zeros_like(G); S = np.zeros_like(G); Pm = np.zeros_like(G)
        poses: Dict[float, np.ndarray] = {}
        for rec in self._leaves.values():
            y, x = (int(math.floor(rec.y / RES)) - self._cy0) // 2, (int(math.floor(rec.x / RES)) - self._cx0) // 2
            if 0 <= y < H and 0 <= x < Wd: poses.setdefault(round(rec.r_d, 4), np.zeros(shape, bool))[y, x] = True
        for d, pr in poses.items():
            if d >= r - 1e-9: Pm |= ndimage.distance_transform_edt(~pr) * h <= (d - r + h) + 1e-9
        for d, raster in self._polylines().items():
            dist = ndimage.distance_transform_edt(~raster) * h
            G |= dist <= h + 1e-9; S |= dist <= d + 1e-9
            if d >= r - 1e-9: Wm |= dist <= (d - r + h) + 1e-9
        last = {}
        for k, rec in self._leaves.items():
            gx, gy = int(math.floor(rec.x / RES)), int(math.floor(rec.y / RES))
            cur = last.get(rec.session)
            if cur is None or (rec.t, k) > (cur[0], cur[1]):
                last[rec.session] = (rec.t, k, (gx - self._cx0) // 2, (gy - self._cy0) // 2, round(rec.r_d, 4))
        self._pl = dict(shape=shape, G=G, W=Wm, S=S, P=Pm, last=last)
        return G, Wm, S, Pm

    def _build_F(self, shift: Tuple[int, int] = (0, 0)):
        """(F, clr, S): the navigable lattice cells, the physical clearance of every lattice cell (distance to the nearest
        blocked or unknown cell, m) and the driven (body covered) image"""
        V = self._V
        free = (V != 255) & (V < 100)
        sq = self._erode(free, shift)
        clr = self._clearance_cells(free, shift) * RES
        sq_h, fr_h, clr_h = sq[::LAT, ::LAT], free[::LAT, ::LAT], clr[::LAT, ::LAT]
        G, W, S, P = self._evidence(sq_h.shape, shift)
        # the body stood at a leaf pose: the cells W proves there are free even where the window is unknown (a camera blind spot
        # under the robot); a REPORTED blocked cell stays blocked. Along the chord between two poses only known-free cells count.
        cand = sq_h | (W & fr_h) | (P & (V == 255)[::LAT, ::LAT])
        lab, _ = ndimage.label(cand, structure=np.ones((3, 3)))
        g = G & cand
        keep = np.isin(lab, np.unique(lab[g])) & cand if g.any() else np.zeros_like(cand)
        return keep, clr_h, S

    # ------------------------------------------------------------------ solve
    def _solve(self, shift: Tuple[int, int], birth: str):
        nav, clr, S = self._build_F(shift)
        if self._key_plane is None:
            self._key_plane = -np.ones(nav.shape, np.int32)
        self._retired_map = {}
        if not nav.any():
            self._retired_map = {self._fmt(k): [] for k in self._keys}
            self._solved = None; self._keys = []; self._key_plane = -np.ones(nav.shape, np.int32)
            return
        self._solved = self._evaluate(self._partition(nav), clr, S, birth)

    def _partition(self, nav: np.ndarray) -> "morse.Partition":
        return morse.partition(nav, self._h, self._tau, radius=self._radius, cache=self._ccache)

    def _evaluate(self, part: "morse.Partition", clr, S, birth) -> _Solved:
        sp, A, h = part.sp, [int(a) for a in part.anchor], self._h
        H, W = sp.H, sp.W
        full = part.full.astype(np.int64)
        sp.clr = clr[sp.ys, sp.xs]
        edges = self._lattice_edges(sp, h)
        plane = -np.ones((H, W), np.int32); plane[sp.ys, sp.xs] = full
        # persistent keys
        cells = [(int(sp.xs[a]), int(sp.ys[a])) for a in A]
        prev_keys = list(self._keys)
        keys_t, retired_t = assign_A(prev_keys, self._key_plane, plane, cells, birth, None)
        ov = overlaps(self._key_plane, plane, len(prev_keys)) if prev_keys else {}
        succ: Dict[tuple, List[tuple]] = {}
        for (j, k), n in sorted(ov.items(), key=lambda it: -it[1]):
            if prev_keys[k] in retired_t: succ.setdefault(prev_keys[k], []).append(keys_t[j])
        self._retired_map = {self._fmt(k): [self._fmt(s) for s in succ.get(k, [])] for k in retired_t}
        self._key_plane = plane.copy(); self._keys = list(keys_t)      # chart index per cell; keys in the same order
        keys = [self._fmt(k) for k in keys_t]
        # channels on the lattice
        V = self._V
        v = V[::LAT, ::LAT]
        ch = {"undriven": (~S).astype(float), "ever_blocked": (self._B[::LAT, ::LAT] >= 0).astype(float),
              "soft": np.where((v > 0) & (v < 100), v / 99.0, 0.0)}
        for name, (cv, _) in self._chan.items():
            w = cv[::LAT, ::LAT]; ch[name] = np.where((w > 0) & (w < 100), w / 99.0, 0.0)
        chv = {k: f[sp.ys, sp.xs] for k, f in ch.items()}
        links = self._links(sp, A, full, edges, chv, morse.arcs(sp, part.full, part.f, sp.clr))
        # chart records
        eb = ch["ever_blocked"]
        pose_cell = {}
        for k, rec in self._leaves.items():
            y = int(math.floor(rec.y / RES) - self._cy0) // 2; x = int(math.floor(rec.x / RES) - self._cx0) // 2
            if 0 <= y < H and 0 <= x < W and plane[y, x] >= 0 and S[y, x]:
                pose_cell.setdefault(int(plane[y, x]), []).append((k, y, x))
        charts = []
        peaks = np.full(len(A), -1.0)
        np.maximum.at(peaks, full, part.f)
        order = np.argsort(full, kind="stable"); cut = np.searchsorted(full[order], np.arange(len(A) + 1))
        for j, a in enumerate(A):
            cc = order[cut[j]:cut[j + 1]]
            ay, ax = int(sp.ys[a]), int(sp.xs[a])
            leaf = None
            if j in pose_cell:
                leaf = min(pose_cell[j], key=lambda t: ((t[1] - ay) ** 2 + (t[2] - ax) ** 2, t[0]))[0]
            charts.append(Chart(key=keys[j], anchor=self._cell_xy(ay, ax), cells=int(len(cc)), area_m2=float(len(cc) * h ** 2),
                                clearance=float(sp.clr[a]), driven_frac=float(S[sp.ys[cc], sp.xs[cc]].mean()) if len(cc) else 0.0,
                                ever_blocked_frac=float(eb[sp.ys[cc], sp.xs[cc]].mean()) if len(cc) else 0.0, leaf=leaf,
                                peak=float(peaks[j]), region=int(part.region[j])))
        return _Solved(sp=sp, A=A, full=full, keys=keys, charts=charts, links=links, edges=edges, plane=plane,
                       dep=np.zeros((H, W), bool), S=S, peak=peaks, region=part.region)

    @staticmethod
    def _fmt(k: tuple) -> Key:
        return "%s:%d" % (k[0], k[1])

    def _cell_xy(self, y: int, x: int) -> Tuple[float, float]:
        """map-frame centre of lattice cell (y, x)"""
        return ((self._cx0 + 2 * x + 1) * RES, (self._cy0 + 2 * y + 1) * RES)

    @staticmethod
    def _lattice_edges(sp, h):
        ys, xs, idx, nav = sp.ys, sp.xs, sp.idx, sp.nav
        R, C, Ln = [], [], []
        for dy, dx in ((0, 1), (1, 0), (1, 1), (1, -1)):
            y2, x2 = ys + dy, xs + dx
            ok = (y2 >= 0) & (y2 < sp.H) & (x2 >= 0) & (x2 < sp.W)
            ok[ok] &= nav[y2[ok], x2[ok]]
            a = np.nonzero(ok)[0]; b = idx[y2[a], x2[a]]
            if dy and dx:
                keep = nav[ys[a], x2[a]] & nav[y2[a], xs[a]]; a, b = a[keep], b[keep]
            R.append(a); C.append(b); Ln.append(np.full(len(a), math.hypot(dy, dx) * h))
        return np.concatenate(R), np.concatenate(C), np.concatenate(Ln)

    # ------------------------------------------------------------------ links
    def _links(self, sp, A, full, edges, chv, arcs: "morse.Arcs") -> List[dict]:
        """One link per arc of the interface. Length: the shortest 8-neighbour route anchor -> portal -> anchor, each half inside
        its own region (one Dijkstra per region, served from the last batch while the region and its anchor are unchanged)."""
        R, C, Ln = edges
        k = len(A)
        ec = np.where(full[R] == full[C], full[R], -1)
        order = np.argsort(ec, kind="stable"); ecs = ec[order]
        cut = np.searchsorted(ecs, np.arange(-1, k + 1))
        cells_of = np.split(np.argsort(full, kind="stable"), np.searchsorted(np.sort(full), np.arange(1, k)))
        D, PRED, CJ = [], [], []
        rnew: dict = {}
        for j, a in enumerate(A):
            cj = cells_of[j]                              # cells of the region, ascending
            ys, xs = sp.ys[cj], sp.xs[cj]
            y0, x0 = int(ys.min()), int(xs.min())
            hk = hashlib.blake2b(digest_size=16)
            hk.update((ys - y0).astype(np.int32).tobytes()); hk.update((xs - x0).astype(np.int32).tobytes())
            hk.update(np.array([sp.ys[a] - y0, sp.xs[a] - x0], np.int32).tobytes())
            key = hk.digest()
            hit = self._rcache.get(key)
            if hit is None:
                e = order[cut[j + 1]:cut[j + 2]]
                n = len(cj)
                lr = np.searchsorted(cj, R[e]); lc = np.searchsorted(cj, C[e])
                g = coo_matrix((np.concatenate([Ln[e], Ln[e]]), (np.concatenate([lr, lc]), np.concatenate([lc, lr]))), shape=(n, n)).tocsr()
                hit = dijkstra(g, directed=False, indices=int(np.searchsorted(cj, a)), return_predecessors=True)
            rnew[key] = hit
            D.append(hit[0]); PRED.append(hit[1]); CJ.append(cj)
        self._rcache = rnew

        def route(j, cell):
            """cells of the shortest route anchor -> cell inside chart j: followed in the chart's own index space (one
            lookup in, one out) and mapped back to cell indices at the end"""
            cj, pr = CJ[j], PRED[j]
            goal = int(np.searchsorted(cj, A[j]))
            q = int(np.searchsorted(cj, cell))
            p = [q]
            while q != goal:
                q = int(pr[q])
                if q < 0: return None
                p.append(q)
            return cj[p[::-1]].tolist()
        h = self._h
        out = []
        for a, b, u, v, s, w in zip(arcs.a.tolist(), arcs.b.tolist(), arcs.p.tolist(), arcs.q.tolist(), arcs.s.tolist(),
                                    arcs.g.tolist()):
            du = D[a][int(np.searchsorted(CJ[a], u))]; dv = D[b][int(np.searchsorted(CJ[b], v))]
            pa = route(a, u); pb = route(b, v)
            if pa is None or pb is None or not (np.isfinite(du) and np.isfinite(dv)): continue
            cells = np.array(pa + pb[::-1], np.int64)
            seg = np.hypot(np.diff(sp.ys[cells]).astype(float), np.diff(sp.xs[cells]).astype(float)) * h * 0.5
            integ = {name: float((seg * (f[cells[:-1]] + f[cells[1:]])).sum()) for name, f in chv.items()}
            out.append(dict(a=a, b=b, length=float(du + dv + h), width=2.0 * float(w), saddle=float(s), u=u, v=v,
                            route=cells.tolist(), integ=integ))
        return out

    # ------------------------------------------------------------------ dirty
    def _signatures(self, s: Optional[_Solved]):
        """per chart key (anchor cell, cell count, cell checksum) and the set of (a, b, length, width) of the links; cells are global
        lattice coordinates, so a canvas that grew to the left or top changes nothing"""
        sig, link_sig = {}, set()
        if s is not None:
            gy0, gx0 = self._cy0 // 2, self._cx0 // 2
            ys, xs = np.nonzero(s.plane >= 0)
            lab = s.plane[ys, xs]
            k = len(s.keys)
            cnt = np.bincount(lab, minlength=k)
            chk = np.bincount(lab, weights=((ys.astype(np.int64) + gy0) * 7919 + (xs.astype(np.int64) + gx0)).astype(np.float64), minlength=k)
            for j, key in enumerate(s.keys):
                sig[key] = (int(s.sp.ys[s.A[j]]) + gy0, int(s.sp.xs[s.A[j]]) + gx0, int(cnt[j]), int(round(chk[j])) % 1000003)
            for l in s.links:
                link_sig.add((s.keys[l["a"]], s.keys[l["b"]], round(l["length"], 3), round(l["width"], 3)))
        return sig, link_sig

    def _dirty(self, old: Optional[_Solved], old_sig, old_link_sig, old_keys, bounds, region) -> Dirty:
        s = self._solved
        sig, link_sig = self._signatures(s)
        changed_cells = None
        if s is not None:
            op = None if old is None else old.plane
            if op is not None and op.shape != s.plane.shape:
                sy, sx = self._shift
                if sy + op.shape[0] <= s.plane.shape[0] and sx + op.shape[1] <= s.plane.shape[1]:
                    big = -np.ones(s.plane.shape, op.dtype); big[sy:sy + op.shape[0], sx:sx + op.shape[1]] = op; op = big
            if op is not None and op.shape == s.plane.shape:
                a = np.where(op >= 0, np.array([self._hash(k) for k in old.keys] + [0])[op], 0) if len(old.keys) else np.zeros(s.plane.shape)
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
        the chart diameter (2 x chart_radius). `crosses_blocked`: the straight segment to it passes a known blocked cell."""
        self._check()
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
        diameter = 2.0 * self.params.chart_radius                  # a goal further than a chart from navigable space is not in the graph
        limit = diameter if max_dist is None else min(max_dist, diameter)
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
        self._check()
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

    def _cell_at(self, p: Optional[Tuple[float, float]]) -> Optional[int]:
        """index of the navigable cell containing the map-frame point, None outside navigable space"""
        if p is None: return None
        sp = self._solved.sp
        iy = (int(math.floor(p[1] / RES)) - self._cy0) // 2; ix = (int(math.floor(p[0] / RES)) - self._cx0) // 2
        if not (0 <= iy < sp.H and 0 <= ix < sp.W): return None
        c = int(sp.idx[iy, ix])
        return c if c >= 0 else None

    def route(self, a: Key, b: Key, blocked: Optional[Blocked] = None, start: Optional[Tuple[float, float]] = None,
              goal: Optional[Tuple[float, float]] = None) -> Optional[Route]:
        """Shortest lattice route in navigable space (blocked cells removed) between the anchors of charts a and b, or, when
        `start` / `goal` name a navigable cell of chart a / b, between those cells (no anchor is visited; a point outside
        navigable space, or in another chart, falls back to the anchor of its chart). A route inside one chart reads the
        clearance at that chart's anchor as its width. Searches the chart corridor (the chart path and its neighbours) and returns
        what that finds; the whole graph only when the corridor finds nothing or an overlay is given. The corridor result is not
        proved shortest: on 840 real pairs p90 1.00, worst 1.40 x the true shortest. The path is minimal in integer cost
        (`Route.cost`); among equal-cost paths it is the lexicographic minimum of its (y, x) cell sequence, so every reader of the
        same cells returns the same route. `Route.length` is the geometric length of that path."""
        self._check()
        s = self._solved
        if s is None or a not in s.keys or b not in s.keys: return None
        sp = s.sp; ja, jb = s.keys.index(a), s.keys.index(b)
        src, dst = int(s.A[ja]), int(s.A[jb])
        c0, c1 = self._cell_at(start), self._cell_at(goal)
        if c0 is not None and int(s.full[c0]) == ja: src = c0
        if c1 is not None and int(s.full[c1]) == jb: dst = c1
        R, C, Ln = s.edges
        N = sp.N
        alive = np.ones(N, bool)
        if blocked is not None and (blocked.discs or blocked.segments):
            bm = self._blocked_mask(blocked)
            alive = ~bm[sp.ys, sp.xs]
        if not alive[src] or not alive[dst]: return None

        W = self._cache.get("route_w")                       # integer edge cost: the length in units of ROUTE_UNIT_M, rounded up
        if W is None:
            W = self._cache["route_w"] = np.maximum(np.ceil(Ln / ROUTE_UNIT_M - 1e-6), 1.0)

        def search(allowed):
            """(cost src -> dst, graph, distance of every cell to dst); all integers held exactly in float64"""
            ok = allowed[R] & allowed[C]
            g = coo_matrix((np.concatenate([W[ok], W[ok]]), (np.concatenate([R[ok], C[ok]]), np.concatenate([C[ok], R[ok]]))),
                           shape=(N, N)).tocsr()
            d = dijkstra(g, directed=False, indices=dst)
            return d[src], g, d
        best = None
        keep = self._corridor_charts(ja, jb) if blocked is None else None   # an overlay can send the way outside any corridor
        if keep is not None:
            best = search(alive & np.isin(s.full, list(keep)))
            if not np.isfinite(best[0]): best = None
        if best is None:
            best = search(alive)
            if not np.isfinite(best[0]): return None
        cost, g, d = best
        # Canonical path among the minimal-cost ones: from the start, the neighbour on a shortest path with the smallest cell
        # index. Cells are row-major, so that is the lexicographic minimum of the (y, x) sequence of the path: a rule of the
        # geometry alone (no chart key, history or implementation order), the same for any reader of the same cells.
        path = [src]
        while path[-1] != dst:
            u = path[-1]
            lo, hi = g.indptr[u], g.indptr[u + 1]
            nb = g.indices[lo:hi]
            path.append(int(nb[(g.data[lo:hi] + d[nb]) == d[u]].min()))
        charts: List[Key] = []
        for c in path:
            kk = s.keys[int(s.full[c])]
            if not charts or charts[-1] != kk: charts.append(kk)
        pa = np.array(path)
        length = float(np.hypot(np.diff(sp.ys[pa]), np.diff(sp.xs[pa])).sum() * self._h)
        return Route(charts=charts, length=length, min_width=self._route_width(path, src), cost=int(cost),
                     nodes=[self._cell_xy(int(sp.ys[c]), int(sp.xs[c])) for c in path])

    def _route_width(self, path: List[int], src: int) -> float:
        """Passage width of a route: the narrowest crossing of the chart sequence, each pair of consecutive charts taken at its
        WIDEST arc (Link.width, the strict widest-path width). A pair can have several arcs: the passage and slivers of
        witness cells along a wall; the passage is what the sequence offers, a hugging lattice path through a sliver is
        geometry, not a narrower door. A lattice path hugs corners, so its own clearance is not a width. A route inside one
        chart has no passage: the clearance at its anchor."""
        s = self._solved; sp = s.sp
        best: Dict[Tuple[int, int], float] = {}
        for l in s.links:
            k = (min(l["a"], l["b"]), max(l["a"], l["b"]))
            best[k] = max(best.get(k, 0.0), float(l["width"]))
        w = math.inf
        for c0, c1 in zip(path[:-1], path[1:]):
            j0, j1 = int(s.full[c0]), int(s.full[c1])
            if j0 == j1: continue
            cand = best.get((min(j0, j1), max(j0, j1)))
            # diagonal-only contact: no link; the two cells decide
            w = min(w, cand if cand is not None else 2.0 * float(min(sp.clr[c0], sp.clr[c1])))
        return float(w) if math.isfinite(w) else float(2.0 * sp.clr[s.A[int(s.full[src])]])   # no crossing: the chart's anchor

    # ------------------------------------------------------------------ persistence hooks (store.py)
    @property
    def nbytes(self) -> int:
        """Best estimate of the memory this engine holds: every numpy array reachable from its planes and solver state, each
        counted once (views by their base); containers by their shallow size. For budgeting caches by bytes, not an audit."""
        seen: set = set()

        def walk(o, depth=0) -> int:
            if o is None or depth > 7: return 0
            if isinstance(o, np.ndarray):
                b = o.base if isinstance(o.base, np.ndarray) else o
                if id(b) in seen: return 0
                seen.add(id(b)); return int(b.nbytes)
            if id(o) in seen: return 0
            if isinstance(o, (str, bytes, int, float, bool)): return 0
            if isinstance(o, dict):
                seen.add(id(o)); return sys.getsizeof(o) + sum(walk(v, depth + 1) for v in o.values())
            if isinstance(o, (list, tuple, set)):
                seen.add(id(o)); n = sys.getsizeof(o)
                if o and not isinstance(next(iter(o)), (int, float, str, bytes, bool, tuple)):
                    n += sum(walk(v, depth + 1) for v in o)
                return n
            if hasattr(o, "__dict__"):
                seen.add(id(o)); return sum(walk(v, depth + 1) for v in vars(o).values())
            return 0
        return walk(self.__dict__)

    def _leaf_put(self, key: str, rec: "_Rec") -> None:
        old = self._leaves.get(key)
        if old is not None:
            i = bisect.bisect_left(self._leaf_order, (old.t, key))
            if i < len(self._leaf_order) and self._leaf_order[i] == (old.t, key): del self._leaf_order[i]
        bisect.insort(self._leaf_order, (rec.t, key))
        self._leaves[key] = rec
        self._leaf_recs.pop(key, None)

    def _leaf_del(self, key: str) -> None:
        old = self._leaves.pop(key, None)
        if old is not None:
            i = bisect.bisect_left(self._leaf_order, (old.t, key))
            if i < len(self._leaf_order) and self._leaf_order[i] == (old.t, key): del self._leaf_order[i]
        self._leaf_recs.pop(key, None)

    def _leaf_record(self, key: str) -> LeafRecord:
        r = self._leaf_recs.get(key)
        if r is None:
            l = self._leaves[key]
            r = self._leaf_recs[key] = LeafRecord(key, l.session, l.t, l.x, l.y, l.r_d,
                                                  None if l.view is None else (l.view.x, l.view.y, l.view.yaw, l.view.fov, l.view.rng))
        return r

    def export_state(self) -> EngineState:
        """The resumable state, by reference (the arrays are not copied: the store slices them into tiles)."""
        self._check()
        p = self.params
        st = EngineState(params=dict(robot_half_side=p.robot_half_side, lattice=p.lattice, chart_radius=p.chart_radius),
                         cx0=self._cx0, cy0=self._cy0, V=self._V, T=self._T, B=self._B, chan=dict(self._chan),
                         leaves=[self._leaf_record(k) for _, k in self._leaf_order],
                         version=self.version, watermark=self._watermark)
        s = self._solved
        if s is not None:
            st.keys = list(self._keys)
            st.plane = s.plane
        return st

    def import_state(self, st: EngineState, verify: bool = True) -> None:
        """Resume from `st` on a fresh engine: planes and evidence are taken as they are, then F and the partition are
        rebuilt (both are functions of the planes and the evidence) and must reproduce the stored owner plane cell for cell;
        the stored keys are then the keys of the rebuilt charts. The canvas is padded by MARGIN so that the distance to the
        non-navigable cells near the stored edge sees unknown, as it did before."""
        p = self.params
        if (st.params["robot_half_side"], st.params["lattice"], st.params["chart_radius"]) != \
                (p.robot_half_side, p.lattice, p.chart_radius):
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
        self._leaf_order = sorted((l.t, l.key) for l in st.leaves)
        self._leaf_recs = {l.key: l for l in st.leaves}
        self.version = st.version
        self._watermark = tuple(st.watermark)
        self._cache = {}
        self._keys = [tuple(k) for k in st.keys]
        if not st.keys or st.plane is None:
            self._solved = None; self._key_plane = None; self._keys = []
            self._sig, self._link_sig = {}, set()
            return
        self._key_plane = padl(st.plane.astype(np.int32), -1)
        nav, clr, S = self._build_F()
        part = self._partition(nav)
        plane = -np.ones(nav.shape, np.int32); plane[part.sp.ys, part.sp.xs] = part.full
        bad = np.nonzero((plane != self._key_plane).ravel())[0]
        if len(bad) and verify:
            y, x = divmod(int(bad[0]), nav.shape[1])
            gx = (self._cx0 + 2 * x) // (2 * TILE); gy = (self._cy0 + 2 * y) // (2 * TILE)
            raise StateMismatch("owner plane differs from the stored one in %d cells, first in tile %d,%d" % (len(bad), gx, gy))
        if len(self._keys) != part.K:
            raise StateMismatch("stored key table has %d keys, the rebuilt partition %d charts" % (len(self._keys), part.K))
        self._retired_map = {}
        keys_in = list(self._keys)
        self._solved = self._evaluate(part, clr, S, "")
        if list(self._keys) != keys_in:
            raise StateMismatch("restored keys differ from the stored key table")
        self._sig, self._link_sig = self._signatures(self._solved)

    def check_invariants(self) -> List[str]:
        """The cheap structural subset a worker can run after a load: the charts cover F exactly, every anchor is in its chart
        and every chart is 4-connected.
        Returns the violated invariants (empty = fine)."""
        self._check()
        s = self._solved
        if s is None: return []
        sp = s.sp; out: List[str] = []
        if (s.full < 0).any() or len(s.full) != sp.N:
            out.append("a navigable cell has no chart")
        for j, a in enumerate(s.A):
            if int(s.full[a]) != j:
                out.append("anchor %s not in its chart" % s.keys[j]); continue
            m = np.zeros(s.plane.shape, bool); mine = np.nonzero(s.full == j)[0]; m[sp.ys[mine], sp.xs[mine]] = True
            lab, n = ndimage.label(m, structure=ndimage.generate_binary_structure(2, 1))
            if n != 1: out.append("chart %s has %d 4-components" % (s.keys[j], n))
        return out

    # ----- query / cover
    def _levels(self):
        """[level 0 = every chart] + the nested coarser partitions (morse.levels): per level the parent (elder chart index) of
        every chart and the chart indices that are nodes of the level"""
        if "levels" not in self._cache:
            s = self._solved
            K = len(s.A)
            rc = np.stack([s.sp.ys[s.A], s.sp.xs[s.A]], 1) if K else np.zeros((0, 2), np.int32)
            la = np.array([l["a"] for l in s.links], np.int64); lb = np.array([l["b"] for l in s.links], np.int64)
            hw = np.array([l["saddle"] for l in s.links])
            lv = [dict(nodes=list(range(K)), parent=list(range(K)))]
            for par in morse.cell_levels(s.peak, rc, s.region, la, lb, hw, self._tau):
                lv.append(dict(nodes=np.unique(par).tolist(), parent=par.tolist()))
            self._cache["levels"] = (lv, None, self._tau)
        return self._cache["levels"]

    def query(self, bbox: BBox, level: int = 0, raster: bool = True, parents: bool = True) -> ViewResult:
        self._check()
        s = self._solved
        if s is None:
            return ViewResult(self.version, [], [], None)
        H, W = s.plane.shape
        # lattice window of the bbox
        y0 = max(0, int(math.floor(bbox.y0 / RES) - self._cy0) // 2); y1 = min(H, int(math.ceil(bbox.y1 / RES) - self._cy0) // 2 + 1)
        x0 = max(0, int(math.floor(bbox.x0 / RES) - self._cx0) // 2); x1 = min(W, int(math.ceil(bbox.x1 / RES) - self._cx0) // 2 + 1)
        levels = self._levels()[0] if (parents or level > 0) else []
        level = max(0, min(int(level), len(levels) - 1)) if levels else 0   # a level beyond the top is the top
        sub = s.plane[y0:y1, x0:x1] if y1 > y0 and x1 > x0 else np.zeros((0, 0), np.int32)
        present = set(np.unique(sub[sub >= 0]).tolist())
        group = list(range(len(s.A)))
        node_ids = list(range(len(s.A)))
        if level > 0:
            group = [int(g) for g in levels[level]["parent"]]
            node_ids = [int(n) for n in levels[level]["nodes"]]
        charts = []
        by_group: Dict[int, List[int]] = {}
        for j, g_ in enumerate(group): by_group.setdefault(g_, []).append(j)
        for n in node_ids:
            members = by_group.get(n, [])
            if not any(m_ in present for m_ in members): continue
            c = s.charts[n]
            parents = {}
            for L_ in range(1, len(levels)):
                parents[L_] = s.keys[int(levels[L_]["parent"][n])]
            if level == 0:
                charts.append(Chart(c.key, c.anchor, c.cells, c.area_m2, c.clearance, c.driven_frac, c.ever_blocked_frac, c.leaf, parents, c.peak, c.region))
            else:
                cs = [s.charts[j] for j in members]
                cells = sum(x.cells for x in cs)
                charts.append(Chart(c.key, c.anchor, cells, cells * self._h ** 2, c.clearance,
                                    sum(x.driven_frac * x.cells for x in cs) / max(cells, 1),
                                    sum(x.ever_blocked_frac * x.cells for x in cs) / max(cells, 1), c.leaf, parents,
                                    max(x.peak for x in cs), c.region))
        shown = {c.key for c in charts}
        links = []
        if level == 0:
            for l in s.links:
                ka, kb = s.keys[l["a"]], s.keys[l["b"]]
                if ka in shown and kb in shown:
                    links.append(self._link(l, ka, kb))
        else:
            agg: Dict[Tuple[int, int], dict] = {}
            wide: Dict[Tuple[int, int], float] = {}
            sad: Dict[Tuple[int, int], float] = {}
            for l in s.links:
                ga, gb = group[l["a"]], group[l["b"]]
                if ga == gb: continue
                key = (min(ga, gb), max(ga, gb))
                # the shortest member is the route (ties: the smaller portal), the widest member is the passage
                pk = (l["length"], self._cell_xy(int(s.sp.ys[l["u"]]), int(s.sp.xs[l["u"]])), self._cell_xy(int(s.sp.ys[l["v"]]), int(s.sp.xs[l["v"]])))
                if key not in agg or pk < agg[key]["_pk"]: agg[key] = dict(l, a=key[0], b=key[1], _pk=pk)
                wide[key] = max(wide.get(key, 0.0), l["width"]); sad[key] = max(sad.get(key, 0.0), l["saddle"])
            for key, l in agg.items(): l["width"] = wide[key]; l["saddle"] = sad[key]
            for l in agg.values():
                ka, kb = s.keys[l["a"]], s.keys[l["b"]]
                if ka in shown and kb in shown: links.append(self._link(l, ka, kb))
        rast = None
        if raster and sub.size:
            keys = [c.key for c in charts]
            kidx = {k_: j for j, k_ in enumerate(s.keys)}
            pos = {kidx[k_]: i + 1 for i, k_ in enumerate(keys)}
            lut = np.zeros(len(s.A) + 1, np.uint16)                  # the last slot answers -1 (not a chart cell)
            for j in range(len(s.A)):
                lut[j] = pos.get(group[j] if level > 0 else j, 0)
            idx = lut[sub]
            rast = OwnerRaster(origin=((self._cx0 + 2 * x0) * RES, (self._cy0 + 2 * y0) * RES), step=self._h, index=idx, keys=keys,
                               dependent=s.dep[y0:y1, x0:x1].copy())
        return ViewResult(self.version, charts, links, rast, len(levels))

    def _link(self, l: dict, ka: Key, kb: Key) -> Link:
        s = self._solved
        pu = self._cell_xy(int(s.sp.ys[l["u"]]), int(s.sp.xs[l["u"]])); pv = self._cell_xy(int(s.sp.ys[l["v"]]), int(s.sp.xs[l["v"]]))
        return Link(a=ka, b=kb, length=l["length"], width=l["width"], portal=(pu, pv), channels=dict(l["integ"]), saddle=l["saddle"])

    def cover(self, keys: Sequence[Key], k: int = 2) -> Tuple[List[str], float, bool]:
        """Minimal set of leaf keys whose camera views k-cover the given charts and the blocked cells bounding them."""
        self._check()
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
