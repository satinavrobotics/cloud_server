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
The graph carries evidence and length; `route` can price clearance (api.Safety). Pure numpy / scipy, no I/O.

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

from .api import (BBox, Blocked, Chart, Dirty, EngineFaulted, Key, Leaf, LeafError, Link, OwnerRaster, Route, Safety, SceneGraph,
                  SceneParams, Snap, ViewResult)
from . import morse
from .place import place_regions
from . import turn as turn_search
from . import weights
from .chart_keys import assign_A, overlaps
from .leafcover import View as CamView
from .leafcover import select_views
from .state import EngineState, LeafRecord, StateMismatch, TimePlane, widen_time
from .window import to_lattice

RES = 0.05                      # costmap cell (m)
LAT = 2                         # costmap cells per lattice cell (lattice = LAT * RES): the 2:1 assumption lives in this constant
                                # and in the places that do index arithmetic on it (`// 2`, `2 * x`, tiles in store.py)
HEADINGS = 23                   # corner shift r * sqrt2 * dtheta < res / 2 at 90 / 23 degrees
TILE = 64                       # lattice cells per tile side (corridor tiles)
MARGIN = 40                     # canvas growth margin (full-resolution cells)
RESERVED = ("undriven", "ever_blocked", "soft")
ROUTE_UNIT_M = weights.ROUTE_UNIT_M      # route cost unit (weights.py): path costs are exact integers
MAX_WINDOW_M = 20.0         # input validation: a leaf window side above this is refused
POSE_SLACK_M = 5.0          # input validation: a pose may lie this far outside its window's footprint


# Time planes (T, B, Fn, Bn and the channels' T) are int64 capture times, -1 = never. While every stamp of the engine lies within
# +-2**30 ms of the first batch's earliest, they are held as int32 OFFSETS from a per-engine epoch (offset = stamp - epoch >= 0, -1
# stays -1): every use is a comparison (>, ==, >= 0, max-merge), which a common offset preserves, and everything that leaves the
# engine (export_state, the store) is widened back to the original int64. A stamp outside the range (or a negative one) switches
# the engine, before anything is mutated, to epoch None = the plain int64 planes.
_T_HALF = 1 << 30
_T_MAX = (1 << 31) - 2


def _t_range(a: np.ndarray, rows: int = 512):
    """(min, max) of the non-negative entries of an integer plane, None when it has none; row blocks, so no full-size temporary"""
    lo = hi = None
    for r0 in range(0, a.shape[0], rows):
        blk = np.asarray(a[r0:r0 + rows])
        v = blk[blk >= 0]
        if v.size:
            l, h = int(v.min()), int(v.max())
            lo = l if lo is None else min(lo, l); hi = h if hi is None else max(hi, h)
    return lo, hi


def _epoch_for(lo: Optional[int], hi: Optional[int]) -> Optional[int]:
    """the epoch that holds every stamp in [lo, hi] as an int32 offset with 2**30 ms of room either side, None when they do not fit"""
    if lo is None: return None
    if lo < 0 or hi - lo > _T_HALF - 2: return None
    return lo - _T_HALF


def _edt(free: np.ndarray) -> np.ndarray:
    """`ndimage.distance_transform_edt(free)` the way scipy forms it (feature transform, offsets to the cell, squared, summed, square
    root: every sum is an integer below 2**53, so its root is correctly rounded), but returned as the int32 SQUARED distances when
    they fit, with scipy's own transient (about 40 B per cell: int32 offsets, widened float64 copies) avoided by forming them in row
    blocks. `np.sqrt(result, dtype=np.float64)` equals scipy's float64 result bit for bit. A plane whose squares do not fit int32
    gets scipy's float64 plane."""
    ft = ndimage.distance_transform_edt(free, return_distances=False, return_indices=True)
    H, W = free.shape
    out = np.empty((H, W), np.int32)
    cols = np.arange(W, dtype=np.int64)
    for r0 in range(0, H, 256):
        r1 = min(H, r0 + 256)
        dy = ft[0, r0:r1].astype(np.int64) - np.arange(r0, r1, dtype=np.int64)[:, None]
        dx = ft[1, r0:r1].astype(np.int64) - cols
        sq = dy * dy + dx * dx
        if int(sq.max()) >= (1 << 31):
            del ft
            return ndimage.distance_transform_edt(free)
        out[r0:r1] = sq
    return out


def _dt_float(d: np.ndarray) -> np.ndarray:
    """float64 distances (a new array) of a cache entry or `_edt` result: int32 squared distances -> their roots; float64 -> a copy"""
    return np.sqrt(d, dtype=np.float64) if d.dtype != np.float64 else d.copy()


def _time_prop(name: str):
    """`g._T` etc. read as the original int64 plane (a widened copy while the engine holds offsets); assignment narrows"""
    def get(self):
        a = getattr(self, name)
        return a if a is None or self._tep is None else widen_time(a, self._tep)

    def set_(self, v):
        if v is not None and self._tep is not None:
            v = np.asarray(v, np.int64); off = v - self._tep
            if ((v >= 0) & ((off < 0) | (off > _T_MAX))).any(): raise ValueError("stamp outside the engine's int32 time range")
            v = np.where(v >= 0, off, -1).astype(np.int32)
        setattr(self, name, v)
    return property(get, set_)


def block_min(a: np.ndarray) -> np.ndarray:
    """The minimum of `a` over each LAT x LAT block of costmap cells (block (i, j) covers rows i*LAT.., cols j*LAT..): a lattice cell
    represents its whole block, so its PRICING clearance is the worst of the cells it covers (never clearer than any of them). The
    shape is ceil(H / LAT) x ceil(W / LAT), like `a[::LAT, ::LAT]`; an incomplete trailing block is the minimum of the cells it has."""
    out = a[::LAT, ::LAT].copy()
    for dy in range(LAT):
        for dx in range(LAT):
            if dy or dx:
                v = a[dy::LAT, dx::LAT]
                o = out[:v.shape[0], :v.shape[1]]
                np.minimum(o, v, out=o)
    return out


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
    sp: morse.Space             # the cells of F; sp.clr is the physical clearance (m) of each (top-left cell: structure, widths, stored);
                                # sp.clr_price is the block minimum over the cell's 2 x 2 costmap cells (route pricing only)
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
    val: Optional[np.ndarray]   # bool, as plane: the validity of each lattice cell (close-evidence rule only, else None)


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


_SQ_ROWS: Dict[float, list] = {}


def _square_rows(r: float):
    """the row intervals of `_squares(r)`: per structure (centre, [(dy, dx0, length)]) with the centre at shape // 2 (scipy's
    origin 0), or None for the whole set when a structure row is not one interval (the caller then falls back to scipy)"""
    key = round(r, 4)
    if key not in _SQ_ROWS:
        out = []
        for st in _squares(r):
            cy, cx = st.shape[0] // 2, st.shape[1] // 2
            rows = []
            for i, row in enumerate(st):
                nz = np.nonzero(row)[0]
                if len(nz) == 0: continue                         # an empty row constrains nothing
                if not row[nz[0]:nz[-1] + 1].all(): out = None; break
                rows.append((i - cy, int(nz[0]) - cx, int(len(nz))))
            if out is None: break
            out.append(rows)
        _SQ_ROWS[key] = out
    return _SQ_ROWS[key]


def _erode_union(free: np.ndarray, sqs: List[np.ndarray], rows_tbl, band_cells: int = 1 << 20) -> np.ndarray:
    """OR over the structures of `ndimage.binary_erosion(free, structure=st, border_value=0)`, bit for bit. A structure whose rows are
    intervals fits at (y, x) iff, on every structure row dy with interval [a, a + L), the run of free cells starting at
    (y + dy, x + a) is at least L long: one run-length plane (shared by all structures), one comparison per row. Outside the plane
    the run is 0 (border_value 0), and a run is cut at the right edge, so an interval leaving the plane never fits."""
    H, W = free.shape
    if rows_tbl is None or H == 0 or W == 0:
        sq = np.zeros_like(free)
        for st in sqs: sq |= ndimage.binary_erosion(free, structure=st, border_value=0)
        return sq
    if H * W > 2 * band_cells:
        # bands of rows with a halo of the structure height keep the transient run-length planes small; a row of the result reads
        # only the rows within the structure's reach, so each band's interior is the result of the whole plane
        pad = max(max(st.shape) for st in sqs); B = max(pad, band_cells // W)
        sq = np.zeros_like(free)
        for b0 in range(0, H, B):
            b1 = min(H, b0 + B); lo, hi = max(0, b0 - pad), min(H, b1 + pad)
            sq[b0:b1] = _erode_union(free[lo:hi], sqs, rows_tbl, band_cells=1 << 62)[b0 - lo:b1 - lo]
        return sq
    dt = np.uint16 if W < 65535 else np.int32                       # run lengths <= W
    idx = np.arange(W, dtype=dt)
    nb = np.where(free, dt(W), idx)                             # position of a blocked cell, W for a free one
    nb = np.minimum.accumulate(nb[:, ::-1], axis=1)[:, ::-1]    # next blocked cell at or after x (W: none)
    run = nb - idx                                              # free cells in a row from x on (0 on a blocked cell)
    del nb
    sq = np.zeros_like(free)
    for rows in rows_tbl:
        # fit[y, x] needs run[y + dy, x + a] >= L, and never fits when (y + dy, x + a) is outside for some row
        Y0 = max(max(0, -dy) for dy, a, L in rows); Y1 = min(min(H, H - dy) for dy, a, L in rows)
        X0 = max(max(0, -a) for dy, a, L in rows); X1 = min(min(W, W - a) for dy, a, L in rows)
        if Y0 >= Y1 or X0 >= X1: continue
        fit = None
        for dy, a, L in rows:
            ok = run[Y0 + dy:Y1 + dy, X0 + a:X1 + a] >= L
            if fit is None: fit = ok
            else: fit &= ok
        if fit is not None: sq[Y0:Y1, X0:X1] |= fit
    return sq


def _line(y0: int, x0: int, y1: int, x1: int):
    """cells of the digital straight line, ties rounded up in exact integer arithmetic: the line of a translated pair is the
    translated line (np.rint rounds half to even, so the line depended on the canvas origin)"""
    n = max(abs(y1 - y0), abs(x1 - x0), 1); k = np.arange(n + 1, dtype=np.int64)
    return y0 + ((y1 - y0) * k * 2 + n) // (2 * n), x0 + ((x1 - x0) * k * 2 + n) // (2 * n)


def _state_params(p: SceneParams) -> dict:
    """the parameters stored with a state; the close-evidence keys appear only when the rule is on (an off store is unchanged)"""
    out = dict(robot_half_side=p.robot_half_side, lattice=p.lattice, chart_radius=p.chart_radius)
    if p.near_range_m is not None:
        out["near_range_m"] = p.near_range_m
        if p.near_min_clearance_m is not None: out["near_min_clearance_m"] = p.near_min_clearance_m
    if p.place_peak_m is not None:
        out["place_peak_m"] = p.place_peak_m; out["place_path_m"] = p.place_path_m
    return out


def _away_from_walls(held: np.ndarray, blocked: np.ndarray, min_clearance: float) -> np.ndarray:
    """The `held` cells (full-resolution bool plane) that survive the away-from-walls filter: the 8-connected components of `held`
    with at least one cell farther than `min_clearance` m (euclidean, cells x RES) from every `blocked` cell (V >= 100, unknown
    included). One distance transform of the whole canvas and one per-component maximum: O(canvas), no per-component crops. The
    decision is the one of the per-component crops this replaces: a crop's distance is never below the full one, and the two are
    equal for every cell within min_clearance of a blocked cell, so `max(d) > min_clearance` agrees on both."""
    H, W = held.shape
    rows = np.nonzero(held.any(1))[0]
    if not len(rows): return np.zeros(held.shape, bool)
    cols = np.nonzero(held.any(0))[0]
    # a component's decision reads the distance only of its own cells, and a cell within min_clearance of a blocked cell has that cell
    # within g cells (Chebyshev) of it: crop to the box of `held` grown by g. The cropped transform is never below the full one and
    # equal to it wherever the full one is <= min_clearance; with no blocked cell in the crop every held cell is farther than that.
    g = int(math.ceil(min_clearance / RES)) + 2
    y0, y1 = max(0, int(rows[0]) - g), min(H, int(rows[-1]) + 1 + g)
    x0, x1 = max(0, int(cols[0]) - g), min(W, int(cols[-1]) + 1 + g)
    hc, bc = held[y0:y1, x0:x1], blocked[y0:y1, x0:x1]
    lab, n = ndimage.label(hc, structure=np.ones((3, 3)))
    if not bc.any():
        return held.copy()                                          # no wall near any held cell: every component is away from walls
    dist = ndimage.distance_transform_edt(~bc) * RES
    has = np.zeros(n + 1, bool)
    has[lab[(dist > min_clearance) & hc]] = True                    # a component is kept iff one of its cells is farther than that
    has[0] = False
    out = np.zeros(held.shape, bool)
    out[y0:y1, x0:x1] = has[lab] & hc
    return out


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
        self._tep: Optional[int] = None         # epoch of the time planes below (int32 offsets) or None (plain int64 stamps)
        self._V = self._Tr = self._Br = None    # value, latest capture time of the winner, latest time reported blocked (raw: see _tep)
        self._Fnr = None                        # close-evidence plane (params.near_range_m set): latest FREE time from within range
        self._Bnr = None                        # close-evidence plane: latest LETHAL time from within range (the held test uses it, not B)
        self._hold = None                       # cells the rule holds blocked, after the away-from-walls filter (only with that filter)
        self._hold_prev = (None, 0, 0)          # (_hold, cx0, cy0) before the batch being folded: the dirty box includes its changes
        self._val_h = None                      # validity per lattice cell (close-evidence rule only), set by `_build_F`
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

    _T, _B = _time_prop("_Tr"), _time_prop("_Br")
    _Fn, _Bn = _time_prop("_Fnr"), _time_prop("_Bnr")

    # ------------------------------------------------------------------ canvas and fold
    def _time_epoch(self, stamps: Sequence[int]) -> Optional[int]:
        """The epoch the time planes will have after folding leaves with these capture times: the current one while every stamp
        fits it, the first batch's (fresh engine) when they fit that, else None (int64 planes; never goes back)."""
        if self._V is None:
            return _epoch_for(min(stamps), max(stamps))
        ep = self._tep
        if ep is None: return None
        return ep if all(0 <= s - ep <= _T_MAX for s in stamps) else None

    def _plan_canvas(self, gx0: int, gy0: int, gx1: int, gy1: int, tep=False):
        """None when [gx0, gx1) x [gy0, gy1) fits (and the time planes keep their epoch, `tep`: see `_time_epoch`); else the new
        canvas geometry. Raises LeafError above max_canvas_cells."""
        if self._V is None:
            cx0 = (gx0 - MARGIN) // 2 * 2; cy0 = (gy0 - MARGIN) // 2 * 2
            W = (gx1 + MARGIN - cx0 + 1) // 2 * 2; H = (gy1 + MARGIN - cy0 + 1) // 2 * 2
            plan = dict(fresh=True, cx0=cx0, cy0=cy0, H=H, W=W, oy=0, ox=0, h=0, w=0)
        else:
            h, w = self._V.shape
            if gx0 >= self._cx0 and gy0 >= self._cy0 and gx1 <= self._cx0 + w and gy1 <= self._cy0 + h:
                if tep is False or tep == self._tep: return None
                return dict(fresh=False, cx0=self._cx0, cy0=self._cy0, H=h, W=w, oy=0, ox=0, h=h, w=w, tep=tep)   # time mode switch only
            nx0 = min(self._cx0, (gx0 - MARGIN) // 2 * 2); ny0 = min(self._cy0, (gy0 - MARGIN) // 2 * 2)
            nx1 = max(self._cx0 + w, gx1 + MARGIN); ny1 = max(self._cy0 + h, gy1 + MARGIN)
            plan = dict(fresh=False, cx0=nx0, cy0=ny0, H=(ny1 - ny0 + 1) // 2 * 2, W=(nx1 - nx0 + 1) // 2 * 2,
                        oy=self._cy0 - ny0, ox=self._cx0 - nx0, h=h, w=w)
        plan["tep"] = self._tep if tep is False else tep
        if plan["H"] * plan["W"] > self.max_canvas_cells:
            raise LeafError(None, f"the batch needs a canvas of {plan['H']} x {plan['W']} cells, above the bound "
                                  f"{self.max_canvas_cells} (an absurd pose?)")
        return plan

    def _grow_canvas(self, plan):
        """Allocate the grown planes WITHOUT touching the state (a MemoryError leaves the engine as it was)."""
        H, W, oy, ox, h, w = plan["H"], plan["W"], plan["oy"], plan["ox"], plan["h"], plan["w"]
        tep = plan["tep"]
        tdt = np.int64 if tep is None else np.int32
        if plan["fresh"]:
            return dict(V=np.full((H, W), 255, np.uint8), Tr=-np.ones((H, W), tdt), Br=-np.ones((H, W), tdt),
                        Fnr=None if self.params.near_range_m is None else -np.ones((H, W), tdt),
                        Bnr=None if self.params.near_range_m is None else -np.ones((H, W), tdt),
                        chan={}, kp=None, tep=tep)

        def grow(a, fill):
            b = np.full((H, W), fill, a.dtype); b[oy:oy + h, ox:ox + w] = a; return b

        def grow_t(a):
            """a time plane into the new canvas; an epoch change (int32 offsets -> int64 stamps only) widens it row block by row block"""
            b = np.full((H, W), -1, tdt)
            if tep == self._tep:
                b[oy:oy + h, ox:ox + w] = a
            else:
                for r0 in range(0, h, 512):
                    b[oy + r0:oy + min(h, r0 + 512), ox:ox + w] = widen_time(a[r0:r0 + 512], self._tep)
            return b
        out = dict(V=grow(self._V, 255), Tr=grow_t(self._Tr), Br=grow_t(self._Br),
                   Fnr=None if self._Fnr is None else grow_t(self._Fnr),
                   Bnr=None if self._Bnr is None else grow_t(self._Bnr),
                   chan={k: (grow(v, 255), grow_t(t)) for k, (v, t) in self._chan.items()}, kp=None, tep=tep)
        if self._key_plane is not None:
            kp = np.full((H // 2, W // 2), -1, self._key_plane.dtype)
            kp[oy // 2:oy // 2 + h // 2, ox // 2:ox // 2 + w // 2] = self._key_plane; out["kp"] = kp
        return out

    def _commit_canvas(self, plan, arrays) -> Tuple[int, int]:
        self._V, self._Tr, self._Br, self._chan = arrays["V"], arrays["Tr"], arrays["Br"], arrays["chan"]
        self._Fnr, self._Bnr, self._tep = arrays["Fnr"], arrays["Bnr"], arrays["tep"]
        if arrays["kp"] is not None: self._key_plane = arrays["kp"]
        self._cx0, self._cy0 = plan["cx0"], plan["cy0"]
        return (0, 0) if plan["fresh"] else (plan["oy"] // 2, plan["ox"] // 2)

    def _ensure(self, gx0: int, gy0: int, gx1: int, gy1: int) -> Tuple[int, int]:
        """grow the canvas to hold [gx0, gx1) x [gy0, gy1); returns the shift (rows, cols) in LATTICE cells of old content"""
        plan = self._plan_canvas(gx0, gy0, gx1, gy1)
        return (0, 0) if plan is None else self._commit_canvas(plan, self._grow_canvas(plan))

    @staticmethod
    def _fold_into(V, T, w, gx, gy, cx0, cy0, t, B=None, Fn=None, pose=None, Bn=None):
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
        if Fn is not None:
            # close evidence: a FREE report counts only from a capture pose within range: `pose` = (x, y, range_m) in the map
            # frame. Max-merge like B. The same condition makes a LETHAL report true evidence (Bn): a lethal from beyond the range
            # is ordinary latest-value evidence.
            px, py, rng = pose
            ys = (np.arange(ys0, ys1) + cy0 + 0.5) * RES; xs = (np.arange(xs0, xs1) + cx0 + 0.5) * RES
            dy, dx = (ys - py)[:, None], (xs - px)[None, :]
            near = np.hypot(dy, dx) <= rng
            fr = kn & (sub < 100) & near
            Fc = Fn[ys0:ys1, xs0:xs1]
            win = win | (fr & (t > Fc))
            Fc[fr] = np.maximum(Fc[fr], t)
            if Bn is not None:
                Nc = Bn[ys0:ys1, xs0:xs1]
                ln = kn & (sub >= 100) & near
                win = win | (ln & (t > Nc))
                Nc[ln] = np.maximum(Nc[ln], t)
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
            plan = self._plan_canvas(*bounds, self._time_epoch([int(lf.stamp_ms) for lf, *_ in prepared]))   # LeafError above the bound
            if plan is not None: grown = self._grow_canvas(plan)     # a MemoryError here changes nothing
        # ---- mutation: from here an escaping exception leaves the state undefined => the engine is FAULTED
        old = self._solved
        old_sig, old_link_sig, old_keys = dict(self._sig), set(self._link_sig), list(self._keys)
        self._hold_prev = (self._hold, self._cx0, self._cy0)
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
                self._V[y0:y1, x0:x1] = 255; self._Tr[y0:y1, x0:x1] = -1; self._Br[y0:y1, x0:x1] = -1
                if self._Fnr is not None: self._Fnr[y0:y1, x0:x1] = -1; self._Bnr[y0:y1, x0:x1] = -1
                for v, t in self._chan.values():
                    v[y0:y1, x0:x1] = 255; t[y0:y1, x0:x1] = -1
        touched = None                                   # global full-resolution cells whose value, time or blocked time changed

        def grow_touched(b):
            nonlocal touched
            if b is None: return
            g = (b[0] + self._cx0, b[1] + self._cy0, b[2] + self._cx0, b[3] + self._cy0)
            touched = g if touched is None else (min(touched[0], g[0]), min(touched[1], g[1]), max(touched[2], g[2]), max(touched[3], g[3]))
        for lf, w, gx, gy, chans in prepared:
            pose = None
            if self._Fnr is not None:
                p = self.params
                pose = (lf.x, lf.y, p.near_range_m)
            ts = int(lf.stamp_ms) - (self._tep or 0)                     # the stamp as the time planes hold it
            grow_touched(self._fold_into(self._V, self._Tr, w, gx, gy, self._cx0, self._cy0, ts, self._Br,
                                         self._Fnr, pose, self._Bnr))
            for name, cw in chans.items():
                if name not in self._chan:
                    self._chan[name] = (np.full(self._V.shape, 255, np.uint8),
                                        -np.ones(self._V.shape, np.int64 if self._tep is None else np.int32))
                v, t = self._chan[name]
                grow_touched(self._fold_into(v, t, cw, gx, gy, self._cx0, self._cy0, ts))
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
        dirty = self._dirty(old, old_sig, old_link_sig, old_keys, touched, region)
        self._hold_prev = (None, 0, 0)                   # only the dirty box above reads it: do not keep the previous plane alive
        return dirty

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
            sq = _erode_union(free, sqs, _square_rows(self.params.robot_half_side))
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
                e = _erode_union(win, sqs, _square_rows(self.params.robot_half_side))
                sq = sp_.copy()
                sq[y0:y1, x0:x1] = e[y0 - wy0:y1 - wy0, x0 - wx0:x1 - wx0]
        self._ecache = (free, sq)
        return sq

    def _clearance_cells(self, free: np.ndarray, shift: Tuple[int, int], scale: Optional[float] = None) -> np.ndarray:
        """`distance_transform_edt(free)`, recomputed only where it can have changed.  A cell outside the changed box grown by R (the largest
        distance of the previous map) keeps its value: its nearest blocked cell was within R and is untouched, and a newly blocked cell is
        farther than R.  Inside, the transform of a window is exact for every cell whose value does not exceed its distance to the window's
        edge (anything blocked outside the window is farther than that); the window widens until that holds for the whole box.  New canvas
        area counts as changed (it was not part of the previous transform)."""
        c, self._dcache = self._dcache, None
        # the cache holds the squared distances as int32 (4 B per cell; `_edt`), or scipy's float64 plane when they do not fit; what
        # is returned is always the float64 plane scipy gives
        scaled = lambda dt, owned: dt if scale is None else ((dt.__imul__(scale)) if owned else dt * scale)
        if c is None:
            d = _edt(free); self._dcache = (free, d)
            return scaled(_dt_float(d), True) if d.dtype != np.float64 else scaled(d, False)
        fp, dp = c
        H, W = free.shape
        newarea = None
        if fp.shape != free.shape:
            oy, ox = 2 * shift[0], 2 * shift[1]
            if oy + fp.shape[0] > H or ox + fp.shape[1] > W:
                d = _edt(free); self._dcache = (free, d)
                return scaled(_dt_float(d), True) if d.dtype != np.float64 else scaled(d, False)
            f2 = np.zeros_like(free); f2[oy:oy + fp.shape[0], ox:ox + fp.shape[1]] = fp
            d2 = np.zeros(free.shape, dp.dtype); d2[oy:oy + fp.shape[0], ox:ox + fp.shape[1]] = dp
            newarea = np.ones(free.shape, bool); newarea[oy:oy + fp.shape[0], ox:ox + fp.shape[1]] = False
            fp, dp = f2, d2
        packed = dp.dtype != np.float64
        ch = fp != free
        if newarea is not None: ch |= newarea
        if not ch.any():
            self._dcache = (free, dp)
            return scaled(_dt_float(dp), True) if packed else scaled(dp, False)
        rows = np.nonzero(ch.any(1))[0]; cols = np.nonzero(ch.any(0))[0]
        del ch, newarea
        R = int(math.ceil(math.sqrt(int(dp.max())) if packed else float(dp.max()))) + 1
        ey0, ey1 = max(0, rows[0] - R), min(H, rows[-1] + 1 + R)
        ex0, ex1 = max(0, cols[0] - R), min(W, cols[-1] + 1 + R)
        rw = R + 1
        while True:
            wy0, wy1, wx0, wx1 = max(0, ey0 - rw), min(H, ey1 + rw), max(0, ex0 - rw), min(W, ex1 + rw)
            dw = _edt(free[wy0:wy1, wx0:wx1])
            subd = dw[ey0 - wy0:ey1 - wy0, ex0 - wx0:ex1 - wx0]
            sub = np.sqrt(subd, dtype=np.float64) if subd.dtype != np.float64 else subd
            del dw
            if wy0 == 0 and wy1 == H and wx0 == 0 and wx1 == W: break
            # `sub <= bound` with bound the least of four per-side distances to the window edge (inf on a side that is the canvas
            # edge): tested one side at a time, which is the same set of comparisons without a plane-sized `bound`
            yy = np.arange(ey0, ey1)[:, None]; xx = np.arange(ex0, ex1)[None, :]
            inf = np.inf
            by = np.minimum(np.where(wy0 > 0, yy - wy0 + 1.0, inf), np.where(wy1 < H, wy1 - yy, inf))
            bx = np.minimum(np.where(wx0 > 0, xx - wx0 + 1.0, inf), np.where(wx1 < W, wx1 - xx, inf))
            if bool((sub <= by).all()) and bool((sub <= bx).all()): break
            rw *= 2
        if packed and subd.dtype != np.float64:
            dp[ey0:ey1, ex0:ex1] = subd                          # the cache entry is ours (taken above, or a fresh grown copy)
            self._dcache = (free, dp)
            return scaled(_dt_float(dp), True)
        dt = _dt_float(dp)
        dt[ey0:ey1, ex0:ex1] = sub
        self._dcache = (free, dt)                                # a box that does not fit int32 keeps the plane in float64 from here on
        return scaled(dt, False)

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
        blocked or unknown cell, m) and the driven (body covered) image. With the close-evidence rule it also sets `_val_h`, the
        validity of each lattice cell: valid = (seen free from within range and not held) or driven (G | W | P of the evidence).
        Validity is sticky: a far report changes V but not the close-free stamp Fn, so a far lethal cannot erase it (a
        driven cell is valid unobserved). The lattice sample is the top-left costmap cell, as for F's own free cells (the
        one-tree validity decision)."""
        V = self._V
        free = (V != 255) & (V < 100)
        hold_v = None
        if self._Fnr is not None:
            # close evidence: reported lethal from within range more recently than seen free from within range (and, with
            # near_min_clearance_m, away from walls). A lethal from beyond the range is not true evidence: a later free retracts it.
            lethal = (self._Bnr >= 0) & (self._Bnr > self._Fnr)
            held = (V < 100) & lethal
            if self.params.near_min_clearance_m is not None:
                held = _away_from_walls(held, V >= 100, self.params.near_min_clearance_m) if held.any() else held
                self._hold = held
            free &= ~held
            # the validity test: a blocked cell is held by a close lethal newer than the close free whatever V says (V is that
            # lethal, or a later far report); a free cell is held exactly as above
            hold_v = np.where(V < 100, held, lethal)
        sq = self._erode(free, shift)
        clr = self._clearance_cells(free, shift, RES)         # distance in cells * RES (the cache keeps the unscaled plane)
        self._clr_price_h = block_min(clr)          # pricing only; `clr_h` (top-left) feeds the partition, charts and link widths
        sq_h, fr_h, clr_h = sq[::LAT, ::LAT], free[::LAT, ::LAT], clr[::LAT, ::LAT]
        G, W, S, P = self._evidence(sq_h.shape, shift)
        self._val_h = None if hold_v is None else ((self._Fnr[::LAT, ::LAT] >= 0) & ~hold_v[::LAT, ::LAT]) | (G | W | P)
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
        if self.params.place_peak_m is not None:
            nav = self._place_filter(nav)
        return morse.partition(nav, self._h, self._tau, radius=self._radius, cache=self._ccache)

    def _place_filter(self, nav: np.ndarray) -> np.ndarray:
        """The navigable set without the regions that are not places (scene_graph/place.py). Every path that cuts charts goes
        through here (solve, load verification, export), so the stored owner plane is the filtered set."""
        sp = morse.Space.of(nav)
        if sp.N == 0:
            return nav
        if self._pl is None or self._pl["G"].shape != nav.shape:
            raise RuntimeError("place rule: the driven planes are not aligned with the navigable set")
        f = morse.distance(sp, self._h)
        region = morse.partition(nav, self._h, self._tau).full        # persistence regions (no radius): one id per cell
        driven = (self._pl["G"] | self._pl["W"])[sp.ys, sp.xs]
        keep = place_regions(f, region, driven, sp.ys, sp.xs, self._h, self.params.place_peak_m, self.params.place_path_m)
        out = nav.copy()
        out[sp.ys[~keep], sp.xs[~keep]] = False
        return out

    def _evaluate(self, part: "morse.Partition", clr, S, birth) -> _Solved:
        sp, A, h = part.sp, [int(a) for a in part.anchor], self._h
        H, W = sp.H, sp.W
        full = part.full.astype(np.int64)
        sp.clr = clr[sp.ys, sp.xs]
        sp.clr_price = self._clr_price_h[sp.ys, sp.xs]
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
        ch = {"undriven": (~S).astype(float), "ever_blocked": (self._Br[::LAT, ::LAT] >= 0).astype(float),
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
                       dep=np.zeros((H, W), bool), S=S, peak=peaks, region=part.region, val=self._val_h)

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
                # the key hash of each cell's owner (0 outside every chart: the trailing entry, index -1), compared row block by row
                # block: the same booleans as comparing two full hash planes, without holding them
                lut_a = np.array([self._hash(k) for k in old.keys] + [0]); lut_b = np.array([self._hash(k) for k in s.keys] + [0])
                changed_cells = np.empty(s.plane.shape, bool)
                for r0 in range(0, s.plane.shape[0], 256):
                    changed_cells[r0:r0 + 256] = lut_a[op[r0:r0 + 256]] != lut_b[s.plane[r0:r0 + 256]]
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
        hb = self._hold_changed_box()
        if hb is not None:
            x0 = hb[0] if x0 is None else min(x0, hb[0]); y0 = hb[1] if y0 is None else min(y0, hb[1])
            x1 = hb[2] if x1 is None else max(x1, hb[2]); y1 = hb[3] if y1 is None else max(y1, hb[3])
        if changed_cells is not None and changed_cells.any():
            ys, xs = np.nonzero(changed_cells)
            cb = ((self._cx0 + 2 * xs.min()) * RES, (self._cy0 + 2 * ys.min()) * RES,
                  (self._cx0 + 2 * xs.max() + 2) * RES, (self._cy0 + 2 * ys.max() + 2) * RES)
            x0 = cb[0] if x0 is None else min(x0, cb[0]); y0 = cb[1] if y0 is None else min(y0, cb[1])
            x1 = cb[2] if x1 is None else max(x1, cb[2]); y1 = cb[3] if y1 is None else max(y1, cb[3])
        bb = None if x0 is None else BBox(x0, y0, x1, y1)
        return Dirty(bbox=bb, changed=changed, retired=retired, links=links, watermark=self._watermark)

    def _hold_changed_box(self) -> Optional[Tuple[float, float, float, float]]:
        """Map-frame box of the cells whose held state changed in this solve (away-from-walls filter only). A component can
        gain or lose its far cell from a window elsewhere, so a held cell may change outside every folded window and chart."""
        new = self._hold
        if new is None: return None
        old, ocx, ocy = self._hold_prev
        diff = new
        if old is not None:
            oy, ox = ocy - self._cy0, ocx - self._cx0
            if oy >= 0 and ox >= 0 and oy + old.shape[0] <= new.shape[0] and ox + old.shape[1] <= new.shape[1]:
                diff = new.copy()
                diff[oy:oy + old.shape[0], ox:ox + old.shape[1]] ^= old
            else:
                diff = np.ones(new.shape, bool)             # the canvas shrank (never happens): everything may have changed
        if not diff.any(): return None
        ys, xs = np.nonzero(diff)
        return ((self._cx0 + int(xs.min())) * RES, (self._cy0 + int(ys.min())) * RES,
                (self._cx0 + int(xs.max()) + 1) * RES, (self._cy0 + int(ys.max()) + 1) * RES)

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
              goal: Optional[Tuple[float, float]] = None, safety: Optional[Safety] = None) -> Optional[Route]:
        """Shortest lattice route in navigable space (blocked cells removed) between the anchors of charts a and b, or, when
        `start` / `goal` name a navigable cell of chart a / b, between those cells (no anchor is visited; a point outside
        navigable space, or in another chart, falls back to the anchor of its chart). A route inside one chart reads the
        clearance at that chart's anchor as its width. Searches the chart corridor (the chart path and its neighbours) and returns
        what that finds; the whole graph only when the corridor finds nothing or an overlay is given. The corridor result is not
        proved shortest: on 840 real pairs p90 1.00, worst 1.40 x the true shortest. The path is minimal in integer cost
        (`Route.cost`); among equal-cost paths it is the lexicographic minimum of its (y, x) cell sequence, so every reader of the
        same cells returns the same route. `Route.length` is the geometric length of that path.
        With `safety` the edge cost is the length priced by the clearance deficit (`weights.edge_costs`), J = `Route.cost`; the corridor
        is then not used (it is a length bound, not a bound of J): the whole graph is searched. With `safety.turn` > 0 every change
        of heading adds its turn cost to J (`turn.route`: the exact (cell, heading) search on the cells that can hold a route no
        costlier than a known one); the tie rule is the same.
        """
        self._check()
        turn = 0.0 if safety is None else float(safety.turn)
        if safety is not None and safety.rate == 0.0 and turn == 0.0: safety = None   # no price: today's route, the corridor included (S8)
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

        # the integer edge costs (weights.py, shared with the lazy reader); cached per clearance price
        rate = 0.0 if safety is None else float(safety.rate)
        key = (rate, None if safety is None else (safety.margin, safety.k, safety.origin))
        hit = self._cache.get("route_w")
        if hit is None or hit[0] != key:
            dsum = None
            if rate:
                d = safety.price(sp.clr_price)
                dsum = d[R] + d[C]
            hit = self._cache["route_w"] = (key, weights.edge_costs(Ln, dsum, rate))
        W = hit[1]

        def search(allowed):
            """(cost src -> dst, graph, distance of every cell to dst); all integers held exactly in float64"""
            ok = allowed[R] & allowed[C]
            g = coo_matrix((np.concatenate([W[ok], W[ok]]), (np.concatenate([R[ok], C[ok]]), np.concatenate([C[ok], R[ok]]))),
                           shape=(N, N)).tocsr()
            d = dijkstra(g, directed=False, indices=dst)
            return d[src], g, d
        best = None
        # an overlay can send the way outside any corridor, and so can a price
        keep = self._corridor_charts(ja, jb) if blocked is None and safety is None else None
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
        if turn > 0.0:
            # the plain optimum above is the whole graph's (no corridor under `safety`): P0 of the turn search, d its distances to dst
            ok = alive[R] & alive[C]
            tg = turn_search.Graph(N, R[ok], C[ok], W[ok], turn_search.heading(sp.ys[C[ok]] - sp.ys[R[ok]], sp.xs[C[ok]] - sp.xs[R[ok]]),
                                   sp.ys, sp.xs, plain=g)
            cost, path = turn_search.route(tg, src, dst, path, int(cost), turn_search.table(turn), d_t=d)
        charts: List[Key] = []
        for c in path:
            kk = s.keys[int(s.full[c])]
            if not charts or charts[-1] != kk: charts.append(kk)
        pa = np.array(path)
        length = float(np.hypot(np.diff(sp.ys[pa]), np.diff(sp.xs[pa])).sum() * self._h)
        return Route(charts=charts, length=length, min_width=self._route_width(path, src), cost=int(cost),
                     nodes=[self._cell_xy(int(sp.ys[c]), int(sp.xs[c])) for c in path])

    def price_cell(self, ix: int, iy: int, safety: Safety) -> Optional[float]:
        """`safety.price(g)` of the navigable global lattice cell (ix, iy), None when it is not navigable."""
        g = self._clearance_cell(ix, iy)
        return None if g is None else float(safety.price(g))

    def deficit_cell(self, ix: int, iy: int, margin: float) -> Optional[float]:
        """max(0, margin - g) of the navigable global lattice cell (ix, iy), None when it is not navigable (api.SceneGraph)."""
        g = self._clearance_cell(ix, iy)
        return None if g is None else max(0.0, float(margin) - g)

    def _clearance_cell(self, ix: int, iy: int) -> Optional[float]:
        self._check()
        s = self._solved
        if s is None: return None
        sp = s.sp
        y, x = iy - self._cy0 // 2, ix - self._cx0 // 2
        if not (0 <= y < sp.H and 0 <= x < sp.W): return None
        c = int(sp.idx[y, x])
        if c < 0: return None
        return float(sp.clr_price[c])

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

    def export_state(self, lazy: bool = False) -> EngineState:
        """The resumable state, by reference (the arrays are not copied: the store slices them into tiles). The time planes are
        int64 arrays; while the engine holds them as int32 offsets they are widened copies, unless `lazy`: then they are
        `TimePlane` views whose slices are the same int64 values (what a tile reader wants, without widening the whole canvas)."""
        self._check()
        p = self.params
        ep = self._tep
        if ep is None: wide = lambda a: a
        elif lazy: wide = lambda a: None if a is None else TimePlane(a, ep)
        else: wide = lambda a: None if a is None else widen_time(a, ep)
        st = EngineState(params=_state_params(p),
                         cx0=self._cx0, cy0=self._cy0, V=self._V, T=wide(self._Tr), B=wide(self._Br), Fn=wide(self._Fnr),
                         Bn=wide(self._Bnr), Hold=self._hold,
                         Val=self._val_h, chan={k: (v, wide(t)) for k, (v, t) in self._chan.items()},
                         leaves=[self._leaf_record(k) for _, k in self._leaf_order],
                         version=self.version, watermark=self._watermark)
        s = self._solved
        if s is not None:
            st.keys = list(self._keys)
            st.plane = s.plane
        return st

    def export_state_lazy(self) -> EngineState:
        """`export_state(lazy=True)`: for a reader that slices the planes (the store's save) or wants only the leaf records"""
        return self.export_state(lazy=True)

    def import_state(self, st: EngineState, verify: bool = True, release: bool = False) -> None:
        """Resume from `st` on a fresh engine: planes and evidence are taken as they are, then F and the partition are
        rebuilt (both are functions of the planes and the evidence) and must reproduce the stored owner plane cell for cell;
        the stored keys are then the keys of the rebuilt charts. The canvas is padded by MARGIN so that the distance to the
        non-navigable cells near the stored edge sees unknown, as it did before. `release=True`: the caller will not use `st` again, so
        its V / T / B / Fn / Bn / chan planes are dropped (set to None) as soon as their padded copies exist (lower peak)."""
        p = self.params
        if st.params != _state_params(p):
            raise ValueError("state was made with other parameters (%r, this engine %r): a store written with the close-evidence "
                             "rule (near_range_m) needs an engine with the same near_range_m / near_min_clearance_m, and vice versa"
                             % (st.params, _state_params(p)))
        if self._V is not None:
            raise ValueError("import_state needs a fresh engine")
        pad = MARGIN

        def padf(a, fill):                                   # np.pad(a, pad, constant_values=fill), one allocation
            a = np.asarray(a); b = np.full((a.shape[0] + 2 * pad, a.shape[1] + 2 * pad), fill, a.dtype); b[pad:-pad, pad:-pad] = a
            return b

        def padl(a, fill, dt):                               # np.pad(a.astype(dt), pad // 2, constant_values=fill), one allocation
            q = pad // 2; b = np.full((a.shape[0] + 2 * q, a.shape[1] + 2 * q), fill, dt); b[q:-q, q:-q] = a
            return b
        self._cx0, self._cy0 = st.cx0 - pad, st.cy0 - pad
        self._V = padf(st.V, 255)
        if release: st.V = None
        if p.near_range_m is not None and (st.Fn is None or st.Bn is None or st.Val is None):
            raise ValueError("state has no close-evidence planes (Fn, Bn, Val): it was made before the validity plane (pipeline "
                             "morse/2+valid carries it; the earlier rule-on tokens do not) and must be rebuilt, not resumed")
        # time planes: int32 offsets from an epoch when every stamp of the state (planes and leaves) fits, else int64 as stored
        tplanes = [np.asarray(a) for a in (st.T, st.B) + ((st.Fn, st.Bn) if p.near_range_m is not None else ())]
        tplanes += [np.asarray(t) for _, t in st.chan.values()]
        lo = hi = None
        for a in tplanes:
            l, h = _t_range(a)
            if l is not None: lo = l if lo is None else min(lo, l); hi = h if hi is None else max(hi, h)
        for l in st.leaves:
            lo = int(l.t) if lo is None else min(lo, int(l.t)); hi = int(l.t) if hi is None else max(hi, int(l.t))
        ep = _epoch_for(lo, hi)
        tdt = np.int64 if ep is None else np.int32

        def padt(a):
            a = np.asarray(a); b = np.full((a.shape[0] + 2 * pad, a.shape[1] + 2 * pad), -1, tdt)
            for r0 in range(0, a.shape[0], 512):
                blk = a[r0:r0 + 512]
                if ep is None: b[pad + r0:pad + r0 + len(blk), pad:-pad] = blk
                else:
                    o = blk.astype(np.int64) - ep; o[blk < 0] = -1; b[pad + r0:pad + r0 + len(blk), pad:-pad] = o
            return b
        self._tep = ep
        del tplanes
        a = None
        self._Tr = padt(st.T)
        if release: st.T = None
        self._Br = padt(st.B)
        if release: st.B = None
        self._Fnr = None if p.near_range_m is None else padt(st.Fn)
        self._Bnr = None if p.near_range_m is None else padt(st.Bn)
        if release: st.Fn = st.Bn = None
        self._hold = None
        self._chan = {}
        for k in list(st.chan):
            v, t = st.chan[k]
            self._chan[k] = (padf(v, 255), padt(t))
            if release: del st.chan[k]
            v = t = None
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
            if p.near_range_m is not None:                      # the hold and validity planes of the planes (an export must carry them)
                self._build_F(); self._verify_val(st, verify)
            return
        self._key_plane = padl(st.plane, -1, np.int32)
        nav, clr, S = self._build_F()
        if st.Hold is not None and verify and self._hold is not None:
            if not np.array_equal(padf(st.Hold, False), self._hold):
                raise StateMismatch("hold plane differs from the one rebuilt in %d cells"
                                    % int((padf(st.Hold, False) != self._hold).sum()))
        self._verify_val(st, verify)
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

    def _verify_val(self, st: EngineState, verify: bool) -> None:
        """The stored validity plane must be the one rebuilt from the planes and the evidence (the close-evidence rule only):
        the store's tile readers answer validity from it, so a stored plane that differs would be read as the engine's."""
        if verify and st.Val is not None and self._val_h is not None:
            want = np.pad(st.Val, MARGIN // 2, constant_values=False)
            if not np.array_equal(want, self._val_h):
                raise StateMismatch("validity plane differs from the one rebuilt in %d cells" % int((want != self._val_h).sum()))

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
