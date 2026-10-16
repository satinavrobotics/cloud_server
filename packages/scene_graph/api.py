"""Public interface of the scene graph (v2, free-space charts). DRAFT 0.9. Engine: engine.ChartGraph (this module is signatures only).

Three processes import this: graph-builder (writer: fold, save), mission-planner (load, locate, route), API service
(load, query, cover). Everything else in the package may change; these types and signatures are the contract.
No I/O here (see `store.py`), no config import.

Terms: a **chart** is the set of free cells owned by one anchor (the mcmap); every cell of it is a straight drive from
its anchor. A **link** joins two charts that share boundary. A chart **key** never changes while the chart lives.
The graph carries evidence and length. A consumer may ask `route` to price clearance with `Safety` (docs/scene_graph/route_safety.md),
and to charge for turning (`Safety.turn`, docs/scene_graph/close_evidence_and_turn_cost_design.md part B); pricing
of observation rates (counts, posterior) is not built: consumers weigh those channels themselves.
"""
import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

Key = str                      # persistent chart key: "{birth_leaf_key}:{counter}"
Pt = Tuple[float, float]       # map-frame metres


@dataclass(frozen=True)
class BBox:
    x0: float; y0: float; x1: float; y1: float


@dataclass(frozen=True)
class SceneParams:
    robot_half_side: float = 0.25      # r (m), per map, set by the cloud server
    lattice: float = 0.1               # h (m)
    chart_radius: float = 2.0          # m: the one scene-dependent scale. Every chart is within this geodesic distance of its
                                       # net point (diameter <= 2 x chart_radius); `snap` is bounded by the chart diameter.
                                       # Part of the params hash: changing it is a new generation.
    near_range_m: Optional[float] = None   # close evidence: a FREE report clears a cell blocked by a later-or-equal report only
                                           # when captured from a pose within this range (m). None = off (latest value wins).
    near_min_clearance_m: Optional[float] = None   # with near_range_m: "away from walls". A held cell stays held only as part of an
                                           # 8-connected component of held cells with a cell farther than this (m) from every other
                                           # blocked or unknown cell; wall rims (components within it everywhere) read as without
                                           # the rule. None = every held cell is held.
    place_peak_m: Optional[float] = None   # place rule (scene_graph/place.py): a persistence region of the navigable set is a place
                                           # when its peak clearance reaches this (m). Set from the robot's circumscribed radius
                                           # (docs: the robot fits at the peak). Both place_* values or neither; None = off.
    place_path_m: Optional[float] = None   # place rule: or when its driven footprint spans at least this path length (m), the robot
                                           # has traversed it. Set from the robot's length.

    def __post_init__(self):
        mc = self.near_min_clearance_m
        if mc is not None:
            if self.near_range_m is None:
                raise ValueError("SceneParams.near_min_clearance_m needs near_range_m")
            if not (0.0 < mc < float("inf")):
                raise ValueError("SceneParams.near_min_clearance_m must be finite and > 0, got %r" % (mc,))
        if (self.place_peak_m is None) != (self.place_path_m is None):
            raise ValueError("SceneParams.place_peak_m and place_path_m are set together or not at all")
        for name, v in (("place_peak_m", self.place_peak_m), ("place_path_m", self.place_path_m)):
            if v is not None and not (0.0 < v < float("inf")):
                raise ValueError("SceneParams.%s must be finite and > 0, got %r" % (name, v))


@dataclass(frozen=True)
class View:
    """Camera of a leaf, in the map frame (for `cover`)."""
    x: float; y: float; yaw: float; fov: float; rng: float


@dataclass
class Leaf:
    key: str                           # topo node key
    session: str
    stamp_ms: int                      # capture time: the only ordering that matters
    x: float; y: float; yaw: float     # base pose in the MAP frame
    resolution: float                  # m per cell of the windows
    origin: Pt                         # map-frame position of window cell (0, 0)
    window_u8: np.ndarray              # occupancy: 0 free .. 99 soft, 100 occupied, 255 unknown
    r_d: float = 0.25                  # half side of the robot that drove this leaf
    origin_yaw: float = 0.0            # window rotation in the map frame; the library resamples (nearest, unknown kept)
    channels: Dict[str, np.ndarray] = field(default_factory=dict)   # optional extra u8 layers (never shape the partition)
    view: Optional[View] = None


@dataclass
class Dirty:
    bbox: Optional[BBox]
    changed: List[Key]
    retired: Dict[Key, List[Key]]      # retired key -> successor keys by cell overlap (largest first); [] = vanished
    links: List[Tuple[Key, Key]]
    watermark: Tuple[int, str]         # (stamp_ms, leaf key) of the last leaf folded


@dataclass
class Chart:
    key: Key
    anchor: Pt
    cells: int
    area_m2: float
    clearance: float
    driven_frac: float                 # share of the chart's cells a body covered
    ever_blocked_frac: float           # share of cells reported blocked in any leaf
    leaf: Optional[str] = None         # a driven leaf of this chart nearest its anchor (thumbnails), None if never driven
    parent: Dict[int, Key] = field(default_factory=dict)   # level -> parent key (derived view)
    peak: float = 0.0                  # largest B in the chart: the persistence peak (distance to the non-navigable cells, m)
    region: int = 0                    # persistence region (0..) this chart was cut from (charts of one region never cross a pass)


@dataclass
class Link:
    a: Key; b: Key
    length: float                      # cost of the link = length (m) along the shortest route inside the two charts
    width: float                       # twice the largest physical clearance over the crossings of this arc (m)
    portal: Tuple[Pt, Pt]
    channels: Dict[str, float] = field(default_factory=dict)   # line integral per channel along that route
    saddle: float = 0.0                # B at the widest crossing of the pair (m): with `peak`, all the levels need


@dataclass
class OwnerRaster:
    """Display encoding of chart geometry: the owner plane over a bbox (4-connected cells, truth of the partition).
    Outlines are derived by the client; holes and dependents are exact this way."""
    origin: Pt                         # map-frame position of cell (0, 0)
    step: float                        # lattice step (m)
    index: np.ndarray                  # u16 [rows, cols]: 0 = not a chart cell, i > 0 = keys[i - 1]
    keys: List[Key]
    dependent: np.ndarray              # bool [rows, cols]: cell attached to the chart by cost, not seen from the anchor


@dataclass
class ViewResult:
    version: int
    charts: List[Chart]
    links: List[Link]
    raster: Optional[OwnerRaster] = None
    levels: int = 0                # number of levels, 0 .. levels-1 valid (a level beyond the top is answered with the top);
                                   # 0 = not computed: a query with parents=False and level=0 never builds the hierarchy


@dataclass
class Snap:
    """Where a point lands on the navigable graph. `distance` is the real distance to the snapped cell (inf when there is
    none), reported even when the snap is refused (`key` None); `crosses_blocked` says the straight segment from the
    point to the snapped cell passes a known blocked cell (the snap would move a goal across a wall)."""
    key: Optional[Key]
    x: Optional[float]
    y: Optional[float]
    distance: float
    crosses_blocked: bool = False


@dataclass
class Route:
    charts: List[Key]                  # region sequence
    length: float
    min_width: float                   # narrowest passage along the route: per crossed chart pair its widest arc (slivers beside a passage do not count)
    nodes: List[Pt]                    # lattice path (map-frame centres)
    cost: int = 0                      # integer path cost: the length in units of 1e-4 m, each edge rounded up. Among paths of equal
                                       # cost the route is the lexicographic minimum of its (y, x) cell sequence (reproducible)
    exact: bool = True                 # False: a priced / overlay search over the tile cap returned the corridor's route (valid, maybe
                                       # costlier than the optimum); the engine's routes are always exact


def edge_weight(len_units, deficit_sum, rate):
    """Integer edge cost of a priced lattice edge (docs/scene_graph/route_safety.md section 1): `len_units` is the Euclidean length
    in cost units (length / 1e-4 m), `deficit_sum` the sum of the prices (`Safety.price`: the hinge deficit max(0, margin -
    clearance), or the exponential profile) of its two cells; works on numbers and arrays. `rate` 0 gives the unpriced weight
    ceil(len_units - 1e-6), at least 1."""
    return np.maximum(np.ceil(len_units * (1.0 + rate * deficit_sum / 2.0) - 1e-6), 1.0)


def turn_weight(turn, steps):
    """Integer turn cost (units of 1e-4 m) of a heading change between two consecutive lattice edges: `steps` multiples of pi / 4
    (0..4, the change wrapped to [0, pi]), `turn` (m per radian) the price of a radian: ceil(turn * |dtheta| / 1e-4 - 1e-6), 0 for no
    change. The one formula the engine, the lazy reader and the references share; works on numbers and arrays."""
    return np.maximum(np.ceil(turn * (np.asarray(steps, float) * (math.pi / 4.0)) / 1e-4 - 1e-6), 0.0)


@dataclass(frozen=True)
class Safety:
    """Clearance pricing of a route, two tiers of one type. The price p(c) of a lattice cell c (g: its physical clearance, m) is
    `price(g)`; an edge of Euclidean length l between cells a and b costs l * (1 + rate * (p(a) + p(b)) / 2) (`edge_weight`).
    Hinge tier (k None): p = max(0, margin - g), `margin` (m) the clearance from which a cell is free of charge, `rate` (1/m) the
    multiplier per metre of deficit. Exponential tier (k set; docs/scene_graph/inflation_profile_design.md): p = exp(-k (g - origin))
    for g < margin (= the reach of the robot's inflation), 0 beyond; `rate` is then the dimensionless multiplier kappa. Below
    `origin` the same formula continues (no kink). rate = 0 is the unpriced route, identical to `safety=None`.
    Turn cost (`turn`, m per radian, docs/scene_graph/close_evidence_and_turn_cost_design.md B.1): every change of heading between two
    consecutive lattice edges adds `turn_weight(turn, steps)`; the start and the goal heading are free. It applies on top of the
    weights above, also at rate 0 (the length plus the turn cost; `margin` is then not read). rate = 0 and turn = 0 is `safety=None`."""
    margin: float
    rate: float
    k: Optional[float] = None
    origin: Optional[float] = None
    turn: float = 0.0

    # the exponential tier is bounded so that every weight is a finite integer held exactly in float64: exp(k * origin) <= e^10
    MAX_K_ORIGIN = 10.0
    # a turn of pi costs at most 5 m (157 080 units): a corrupt robot value cannot make a weight overflow
    MAX_TURN = 5.0

    def __post_init__(self):
        if not self.margin > 0.0:
            raise ValueError("Safety.margin must be > 0, got %r" % (self.margin,))
        if not self.rate >= 0.0:
            raise ValueError("Safety.rate must be >= 0, got %r" % (self.rate,))
        if not (0.0 <= self.turn <= self.MAX_TURN):          # also NaN
            raise ValueError("Safety.turn must be finite, in [0, %g], got %r" % (self.MAX_TURN, self.turn))
        if (self.k is None) != (self.origin is None):
            raise ValueError("Safety.k and Safety.origin are set together")
        if self.k is not None:
            for name, v in (("margin", self.margin), ("rate", self.rate), ("k", self.k), ("origin", self.origin)):
                if not (0.0 <= v < float("inf")):
                    raise ValueError("Safety.%s must be finite, got %r" % (name, v))
            if not (self.k > 0.0 and self.origin > 0.0):
                raise ValueError("Safety.k and Safety.origin must be > 0, got %r, %r" % (self.k, self.origin))
            if self.k * self.origin > self.MAX_K_ORIGIN:
                raise ValueError("Safety: k * origin must be <= %g, got %r" % (self.MAX_K_ORIGIN, self.k * self.origin))

    def price(self, g):
        """price of clearance `g` (m; a number or an array), >= 0. The one formula the engine, the lazy reader and the line of sight share."""
        if self.k is None:
            return np.maximum(0.0, self.margin - g)
        return np.where(g < self.margin, np.exp(-self.k * (g - self.origin)), 0.0)


@dataclass
class Blocked:
    discs: List[Tuple[float, float, float]] = field(default_factory=list)            # x, y, radius
    segments: List[Tuple[Pt, Pt, float]] = field(default_factory=list)               # a, b, half width


class MissingTiles(Exception):
    def __init__(self, tiles): super().__init__(f"tiles not loaded: {tiles}"); self.tiles = tiles


class LeafError(ValueError):
    """A leaf (or its batch) is unusable: raised by fold_many / refold in the validation phase, BEFORE anything is
    mutated, so the engine state is bit-identical afterwards and the batch can be retried or bisected. `key` names the leaf."""
    def __init__(self, key, reason):
        super().__init__(f"leaf {key!r}: {reason}"); self.key = key; self.reason = reason


class EngineFaulted(RuntimeError):
    """An exception escaped after the engine began to mutate (solver or internal): its state is undefined. Every later call
    raises this until a new engine is built (store.load); `cause` is the original exception."""
    def __init__(self, cause):
        super().__init__(f"engine faulted: {cause!r}; rebuild it from the store"); self.cause = cause


class SceneGraph:
    """The contract. The engine is `packages.scene_graph.engine.ChartGraph` (a subclass); this base raises
    NotImplementedError so a caller can be tested against the signatures alone."""

    version: int = 0

    def __init__(self, params: SceneParams = SceneParams()): self.params = params

    def fold_many(self, leaves: Sequence[Leaf]) -> Dirty:
        """Fold a batch (any order: per cell the newest capture time wins, unknown never overwrites, ties go to the
        blocked side), then re-solve once, only where the batch can change the result."""
        raise NotImplementedError

    def refold(self, leaves: Sequence[Leaf], bbox: BBox) -> Dirty:
        """Rebuild the area from the given leaves (after a leaf delete or a realign)."""
        raise NotImplementedError

    def locate(self, x: float, y: float, max_dist: Optional[float] = None) -> Optional[Key]:
        """Chart that owns the nearest navigable cell within `max_dist` (default: the chart diameter, 2 x chart_radius), or None. Never raises,
        never -1. The planner applies its own limits (start ~0.5 m, goal ~1-2 m) and uses `snap` to learn the distance."""
        raise NotImplementedError

    def snap(self, x: float, y: float, max_dist: Optional[float] = None) -> Snap:
        """`locate` with the evidence: the snapped cell centre, the real distance, and whether the straight segment to it
        crosses a known blocked cell. `max_dist` None = the chart diameter, the outer bound; `key` is None beyond it."""
        raise NotImplementedError

    def route(self, a: Key, b: Key, blocked: Optional[Blocked] = None, start: Optional[Pt] = None,
              goal: Optional[Pt] = None, safety: Optional[Safety] = None) -> Optional[Route]:
        """Lattice route in navigable space between charts a and b; None when unreachable. It runs from the anchor of a to
        the anchor of b, unless `start` / `goal` (map-frame points, e.g. a Snap's x, y) name the cells to route between:
        then the route is point to point and no anchor is visited. A point outside navigable space, or in a chart other than
        a / b, falls back to the anchor of its chart. A chart is large, its anchor is not on the way.

        The route minimises the objective J, the sum of the integer edge costs (units of 1e-4 m, each edge rounded up):
        its Euclidean length, or with `safety` the length priced by the clearance deficit (see `Safety`). `Route.cost` is
        the integer value of J, `Route.length` the geometric length of the path. `safety=None` is the unpriced route."""
        raise NotImplementedError

    def deficit_cell(self, ix: int, iy: int, margin: float) -> Optional[float]:
        """max(0, margin - g) for the navigable global lattice cell (ix, iy) (centre ((ix + .5) h, (iy + .5) h)), g its physical
        clearance (m); None when the cell is not navigable."""
        raise NotImplementedError

    def query(self, bbox: BBox, level: int = 0, raster: bool = True, parents: bool = True) -> ViewResult:
        """Charts and links of an area; `raster` adds the owner plane for drawing; `parents=False` skips the hierarchy view
        (Chart.parent stays empty) for callers that need only charts and links, e.g. the store's save."""
        raise NotImplementedError

    def corridor(self, a: Key, b: Key, blocked: Optional[Blocked] = None) -> Optional[List[Tuple[int, int]]]:
        """Tiles a route from a to b needs (the chart graph, anchors and links, is always loaded and small; the
        cells are tiled): chart-level shortest path, plus one chart of margin. None when unreachable.
        The planner loads these tiles, then calls `route`; `route` on a graph that lacks a tile of its corridor
        raises `MissingTiles` naming them (it never fetches by itself: the library has no I/O)."""
        raise NotImplementedError

    def cover(self, keys: Sequence[Key], k: int = 2) -> Tuple[List[str], float, bool]:
        """Minimal set of leaf keys whose camera views k-cover the given charts (and their bounding surfaces):
        (leaf keys, ceiling = coverable fraction, optimal). Pose only, no image or depth is read."""
        raise NotImplementedError
