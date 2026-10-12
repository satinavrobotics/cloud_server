# Scene graph: handoff to CIMBI (algorithm and persistence)

From LACI. CIMBI owns `packages/scene_graph` (the algorithm) and its persistence (snapshot and `sg_*` storage). LACI
owns everything that connects it to the running system (`integration.md`: ingest worker, read API, client, planner,
lifecycle hooks, events, ops). This file is the contract between the two. Anything marked **DECIDE** needs an answer
from both of us before either side builds on it.

> **Superseded below this line.** `packages/scene_graph/api.py` and `store.py` (interface draft 0.9) are the contract;
> sections after the amendments describe v1 types (`Node`, `Dirty.anchors`, `(stamp,id)` feeding, clearance cap, stable
> ids, radius per map) and are kept only as history. Open items for CIMBI: `integration.md`, "Review findings".

## Amendments after CIMBI's reply (v2, 2026-10-12)

Accepted by LACI: Leaf also carries `r_d` and optional extra channel windows; fold order-independent by (capture
time, value), watermark for replay efficiency only; persistent chart keys with lineage by overlap (the client
re-maps through a lineage map, "ids stable forever" is not required); `Route` returns chart keys, length, minimum
width; `query(bbox, level)` returns charts with driven / ever-blocked fractions and links with length, widest-path
width and per-channel integrals; `cover(chart_keys, k)`; no price in the graph; planning on the lattice; no
clearance cap, locality from the read-dependency closure (bounded by chart scale, not map size, to be shown);
snapshot in MinIO is canonical (with a per-cell capture-time plane and the planes the planner prices from), Arango
`sg_*` an index, LACI adds the saga drop; layers are roles, not a single layer; robot size is per leaf (`r_d`),
not per map. Do not build on v1 internals (`_Region`, `Router`, `build_levels`, tau/gamma).

Questions back to CIMBI: (1) the source of "ever blocked" (costmap lethal or robot events delivered to the worker?);
(2) the chart geometry encoding for display (outline polygons vs a raster tile of owner keys); (3) evidence that the
re-solve closure is bounded by scale; (4) which planes the planner needs from the snapshot to price a corridor, and
whether they load per tile; (5) confirm star-shaped charts give few-waypoint straight segments inside a chart.
LACI side: the capture-time source is the robot capture stamp, sanity-checked against receive time (LACI defines the
fallback); `r_d` comes from the robot footprint at ingest.

## Why the scope below

Requirement: one design for small and very large maps. Principle: every operation is local and bounded; a small map
is the degenerate case (one tile, one search). Review findings are folded in (see `integration.md`, "Corrections").

## CIMBI deliverables

### A. Incremental builder
* `fold_many(leaves) -> Dirty`: fold a batch, rebuild once, return the dirty bounding box and the ids of changed
  anchors/links.
* Dirty-region repair: recompute clearance, coarse graph and partition only on the leaf window plus a margin. Cap the
  clearance distance (a few metres) so it is local. Per-leaf cost O(window), independent of map size.
* One shared `_Region` for `links()`, `build_levels()` and planning (today three constructions per publish).
* `SceneGraph.log` optional (unbounded growth in a long-lived worker).
* Level>0 ids keyed `(level, id)`; prefer a stable parent id over "representative member flips".

### B. Frames and input
* `Leaf.from_window(...)`: accepts a decoded u8 window plus pose and yaw in the **map frame** and resolution;
  resamples a rotated window into the lattice (nearest neighbour, unknown mask preserved), aligns cells against the
  payload `resolution` (`Params.res` is 0.05). LACI's adapter supplies decoded array, pose, yaw, resolution, session.

### C. Query API for planning and display (pure, no I/O)
* `locate(x, y) -> node | None`: snap to the nearest navigable coarse node (goal in a wall or unknown cells).
* `route(a, b, blocked=None) -> Route`: node path on the coarse lattice with an optional blocked overlay (disk of
  cells around a position, or a segment); returns the region/portal sequence too. Corridor-then-refine: portal graph
  picks the region sequence, lattice search runs only inside those regions plus a one-region margin; the whole map is
  the corridor on a small map.
* `query(bbox, level) -> anchors, links, hierarchy` for display.
* Return `float('inf')`/`None` explicitly for unreachable; never index with -1 (`_Region.node()` returns -1 off the
  lattice today and `Router.cost` indexes with it).

### D. Persistence (the database-save side)
* `snapshot()/restore()`: state = CellMap chunks (value, owner, visited), anchors, `_next`, per-session `_last`
  (without it the first leaf after a restart skips `clear_path`), params hash. **Tiled**: per tile (groups of the
  existing chunks) plus a small manifest. Objects immutable: `snapshot-{version}/tile-*.npz`, manifest last.
* Arango: per-map `sg_anchors_{id}`, `sg_links_{id}`, `sg_meta_{id}` (same per-map collection pattern as
  `nodes_{id}`, so the delete saga drops them). Anchors and links written in one transaction; `sg_meta.version`
  set last. `sg_meta` also holds the params and their hash, and the ingest **watermark** (see contract).
* A `store` adapter module separate from the pure library (the library keeps no I/O and no config import), reading
  and writing through `TopomapDatabaseClient`.
* Params change (e.g. robot radius) => full rebuild and a new snapshot generation.

### E. Local refold
* `refold(map, leaves_in_order, region_bbox)`: rebuild only the tiles overlapping an area from the leaves that
  overlap them; regions elsewhere keep their ids. LACI supplies the leaves (spatial query on the existing node
  index); no membership table is needed.

### F. Scale gate (acceptance)
Synthetic worlds tiled to about 1M lattice nodes (extend `tests/unit/scene_graph/worlds.py`, `tools/scene_graph_sim`):
* per-leaf update time flat as the map grows;
* plan latency bounded by the corridor, not the map;
* path cost within 1.05x of the full-lattice search;
* incremental builder meets the same invariants/quality metrics as a full rebuild (`metrics.invariants`; equality is
  the wrong test, the result is history-dependent by design);
* to prove or refute: the repair truly depends only on the dirty neighbourhood (design §7 names per-component repair
  as an extension, not as done).

## What LACI assumes (the contract)

1. LACI feeds leaves in `(costmap stamp, node id)` order past a per-map watermark; the library is order-agnostic and
   returns the new watermark with each batch. Replay after restart = restore snapshot, then feed leaves past its
   watermark.
2. One writer per map (Postgres advisory lock held by LACI's worker); the library is not thread-safe per map.
3. The worker calls `fold_many` in batches and publishes at most every T seconds; `Dirty` drives websocket deltas.
4. Reads for the client and planner go through `query`, `locate`, `route` on a version-pinned instance (planner and
   API cache by `(map, version, tile)`).
5. Planner turns `Route` into waypoints (simplified corners, interval cap, line-of-sight on the lattice) on LACI's
   side; the library returns the node path.
6. Robot radius arrives as part of `Params` per map; LACI stores it in `sg_meta` via CIMBI's store.

## Public API: what it means

"Public API" is the Python interface of `packages/scene_graph` (and its `store` adapter): the functions other code may
call. It is not an HTTP API. Three separate processes import it: graph-builder (writer: fold, save), mission-planner
(load, `locate`, `route`) and the API service (load, `query`). Inside the package CIMBI may change anything; only these
signatures are the contract.

## LACI's proposal for the interface (CIMBI to review and amend)

```python
# packages/scene_graph  (pure, no I/O)
class SceneGraph:
    def __init__(self, params: Params): ...
    def fold_many(self, leaves: Sequence[Leaf]) -> Dirty: ...          # one rebuild per batch
    def refold(self, leaves: Sequence[Leaf], bbox: BBox) -> Dirty: ...  # local rebuild of the area
    def locate(self, x: float, y: float) -> Optional[Node]: ...        # nearest navigable node or None
    def route(self, a: Node, b: Node, blocked: Optional[Blocked] = None) -> Optional[Route]: ...
    def query(self, bbox: BBox, level: int = 0) -> View: ...           # anchors, links, hierarchy
    version: int                                                       # bumped per published change

@dataclass
class Leaf:        # built by LACI's adapter, resampled by the library
    key: str; session: str; stamp_ms: int
    x: float; y: float; yaw: float        # pose and window origin in the MAP frame
    resolution: float; window_u8: np.ndarray

@dataclass
class Dirty: bbox: BBox; anchors: List[int]; links: List[Tuple[int, int]]; watermark: Tuple[int, str]
@dataclass
class Route: nodes: List[Node]; length: float; regions: List[int]    # lattice path + region sequence
@dataclass
class Blocked: discs: List[Tuple[float, float, float]]; segments: List[Tuple[Pt, Pt, float]]

# packages/scene_graph/store.py  (CIMBI's persistence, uses TopomapDatabaseClient)
class SceneGraphStore:
    def load(self, map_id: str, version: Optional[int] = None, tiles: Optional[Iterable[Tile]] = None) -> SceneGraph: ...
    def save(self, map_id: str, sg: SceneGraph, dirty: Dirty) -> int: ...   # returns the new version, set last
    def watermark(self, map_id: str) -> Tuple[int, str]: ...
    def meta(self, map_id: str) -> dict: ...                                # params, params hash, generation
    def drop(self, map_id: str) -> None: ...                                # used by the delete saga
```

Notes: `route` and `locate` never raise for "off the lattice" or "unreachable", they return `None` (no `-1` indexing).
`load(tiles=...)` is what lets the planner load only a corridor. Waypoint simplification stays on LACI's side.

## Decisions

### With CIMBI (the seam)
1. **Interface**: the signatures above. CIMBI amends; freeze before LACI starts the worker, planner and API on it.
2. **Storage format** (CIMBI's): `sg_*` documents, `sg_meta` fields (version, params, params hash, generation,
   watermark), tile naming and MinIO layout. LACI's only requirement on it: the API must be able to answer
   `query(bbox, level)` and version-pinned reads without loading the whole map.

### LACI's requirements (decided; CIMBI implements)
3. **Resampling is in the library** (`Leaf` carries the rotated window; the adapter does not straighten it).
4. **Anchor ids are stable across refolds**: remap by maximum overlap with the previous ids. The client keeps
   selections and saved goals valid; churn is a defect, not a trade-off.
5. **Costmap layer**: a graph-builder setting chosen by LACI; the library sees one u8 window per leaf.
6. **Robot radius**: per map, stored in `sg_meta` (LACI supplies it in `Params`); per robot later.
7. **Branching**: both on `feat/scene-graph-v2`, separate directories (CIMBI `packages/scene_graph`,
   `tests/unit/scene_graph`, `tools/scene_graph_sim`; LACI `packages/services/graph_builder`, `packages/api`,
   `packages/services/mission_planner`, `sati-client`); rebase before committing.

## Out of scope for CIMBI

Ingest worker and outbox/sequence, read endpoints and websocket, events, delete-saga hooks, planner and dispatcher
changes, client, deployment. LACI's.
