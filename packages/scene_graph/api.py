"""Public interface of the scene graph (v2, free-space charts). DRAFT 0.9. Engine: engine.ChartGraph (this module is signatures only).

Three processes import this: graph-builder (writer: fold, save), mission-planner (load, locate, route), API service
(load, query, cover). Everything else in the package may change; these types and signatures are the contract.
No I/O here (see `store.py`), no config import.

Terms: a **chart** is the set of free cells owned by one anchor (the mcmap); every cell of it is a straight drive from
its anchor. A **link** joins two charts that share boundary. A chart **key** never changes while the chart lives.
The graph carries evidence and length, never a price: consumers weigh channels themselves.
"""
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
    place_mult: float = 1.5            # DEPRECATED, no effect on the partition (morse.py has no placement scale). Still part
                                       # of the persisted params (graph-builder's `params_dict` pins that set), so it keeps
                                       # its place in the params hash; remove it with that pin.
    sight_range: float = 5.0           # m: tied to the leaf window (10 x 10 m => one leaf never observes beyond 5 m from
                                       # its pose); caps window sizes and pose offsets and bounds `snap`. Part of the params
                                       # hash: changing it is a new generation.


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
    peak: float = 0.0                  # B at the anchor: the persistence peak (distance to the non-navigable cells, m)


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
    min_width: float                   # narrowest passage along the route
    nodes: List[Pt]                    # lattice path (map-frame centres)


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
        """Chart that owns the nearest navigable cell within `max_dist` (default: sight_range), or None. Never raises,
        never -1. The planner applies its own limits (start ~0.5 m, goal ~1-2 m) and uses `snap` to learn the distance."""
        raise NotImplementedError

    def snap(self, x: float, y: float, max_dist: Optional[float] = None) -> Snap:
        """`locate` with the evidence: the snapped cell centre, the real distance, and whether the straight segment to it
        crosses a known blocked cell. `max_dist` None = sight_range, the outer bound; `key` is None beyond it."""
        raise NotImplementedError

    def route(self, a: Key, b: Key, blocked: Optional[Blocked] = None) -> Optional[Route]:
        """Lattice route in navigable space; None when unreachable."""
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
