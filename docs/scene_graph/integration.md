# Scene graph: integration plan

Status: proposal, revised for the v2 algorithm (free-space charts) and CIMBI's reply of 2026-10-12. Companion to
`design.md` (v1 algorithm, being replaced) and `handoff_cimbi.md` (the contract with CIMBI). The library interface is
**not frozen**; items marked *(pending interface)* depend on it.

**Ownership.** CIMBI: the algorithm (`packages/scene_graph`) and its persistence (snapshot, tile layout, `sg_*`
formats). LACI: everything else here (ingest worker, read API, client, planner, lifecycle hooks, events, ops).

## Decisions

1. **The leaf graph stays.** Topo nodes and edges (`nodes_{id}`, `edges_{id}`, `GET /maps/{id}/graph`) remain the
   record of what each mission and session laid down (logs: `session_trace`, `edge-stats`, run views). The high-level
   graph is *additional*, derived from the leaves; it is what the map window displays and what planning uses.
2. **Planner output keeps its shape**: `/api/v1/plan` returns `waypoints` as poses; dispatcher, order building, mapId
   and frame handling are untouched. See "Planner contract".
3. **The builder is a separate worker inside the graph-builder service**, outside the MQTT/upload path and
   `_upload_lock`, fed from the database, one writer per map (advisory lock).
4. **Planning searches the cost lattice inside a corridor of charts**, not anchor to anchor (v2 measured anchor-hub
   stretch 1.15 mean / 1.58 max). Charts, links and levels serve display, "go to place" and the corridor.
5. **The snapshot in MinIO (`map-{id}/scene_graph/`) is the single source of truth; Arango `sg_*` is a rebuildable
   index.** Both are needed for lifecycle: the snapshot dies with the bucket, the index needs an explicit drop in
   the delete saga (`graph_db.delete_map` drops only `nodes_`/`edges_`/graph).
6. **Costmap layers are roles, not a choice.** The library takes occupancy (the only channel that shapes the
   partition) plus optional channels; graph-builder maps stored layer names to roles by configuration. Old
   recordings get an `occupancy_depth` layer from depth: a separate backfill step, pending CIMBI's user.
7. **Costmap row order is verified** (nav2, row 0 = origin y). No open item.

## What v2 changes (vs the v1-based plan)

* **Order-independent fold** by (capture time, value); unknown never overwrites. Folding a leaf twice is a no-op, so
  delivery can be at-least-once and the strict `(stamp, id)` ordering and per-leaf `sg_folded` flag are dropped. The
  watermark remains only to make restart replay cheap.
* **Capture time is now data.** The snapshot carries a per-cell capture-time plane (without it the fold is
  order-dependent again). Capture stamps are compared across robots and sessions, so ingest must define the time
  source: the robot capture stamp (`costmap_stamp_ms` / the node's capture time), sanity-checked against server
  receive time, with a documented fallback when the clocks disagree beyond a bound. *(LACI)*
* **Per-leaf robot size.** `Leaf` carries `r_d`, the half-side of the robot that drove. This replaces "robot radius
  per map". The planner serves a different robot than the one that mapped, so a route is checked against its own
  robot: `Route` returns the minimum width along it and the planner compares that with the planning robot's width.
  Source of `r_d`: the robot's footprint (robot object / orchestrator config), stamped at ingest. *(LACI)*
* **Keys are persistent chart keys with lineage** (about 94-95% overlap per frame). A key can retire, split or merge,
  so the API and websocket carry a lineage map (retired key -> successor keys) with each version, and the client
  re-maps selections and saved goals through it. Stable-id-forever is not promised.
* **Planes the planner needs.** The planning price is computed at plan time from planes (driven/witnessed, worst
  costmap value ever seen, ever-blocked), not stored in the graph. The planner must load those planes for the
  corridor's tiles, version-pinned, not just charts and links.
* **Blocked events feed the graph.** `query` reports an ever-blocked fraction per chart; the source of "blocked" is
  not defined yet (costmap lethal? robot `edgeBlocked` / `blocked_graph_nodes` events?). If events, the dispatcher
  side delivers them to the worker. **Open question to CIMBI.**
* **Locality from the re-solve's read-dependency closure**, not a clearance cap (CIMBI). Condition we hold them to:
  the closure is bounded by the chart scale, not by map size; it is the first thing the scale gate measures.
* **`cover(chart_keys, k) -> leaf keys`** (pose-only, minimal set) is a new integration point: "reconstruct this
  room" via the reconstruction gateway (`packages/api/reconstruction.py`) instead of whole-map manifests.
* **Star-shaped charts** (every cell seen from the anchor along a near-optimal straight segment) allow very few
  waypoints inside one chart. Candidate optimization for waypoint simplification; to confirm with CIMBI.
* Dropped from v1: `Router`, `_Region`, `build_levels`, tau/gamma, the `_last`/`_next` snapshot list, the "full
  refold on delete" claim (order-independence lets a delete recompute only the cells that leaf won).

## Scale design (small and very large maps)

Principle: every operation is local and bounded; a small map is the degenerate case.

* **Incremental builder**: local re-solve over the read-dependency closure (CIMBI). Per-leaf cost independent of map
  size. Gate below.
* **Planning = corridor, then refine**: the chart graph selects the chart sequence; the lattice search runs only
  inside those charts plus a one-chart margin. On a small map the corridor is the whole map.
* **Tiled persistence**: snapshot per tile plus a manifest; the planner loads only the tiles a corridor touches,
  cached by `(map, version, tile)`. Tiles are storage only; charts cross tile borders.
* **Local re-solve on delete or realign**: leaf delete recomputes the cells that leaf won from the remaining leaves
  overlapping them (spatial query on the existing node index; no membership table). Realign shifts a session
  wholesale: a background rebuild with an atomic version switch.
* **Serve by view**: `GET .../scene-graph?bbox=&level=`; hierarchy levels are zoom level of detail; the websocket
  carries the dirty bbox, changed keys and lineage.
* **Gate** (CIMBI, section F of the handoff): synthetic worlds to about 1M lattice nodes; per-leaf update flat in
  map size; plan latency bounded by the corridor; path cost within 1.05x of the full-lattice search; invariants as
  after a full rebuild. Everything about large scale is a projection until it passes.

## Pieces

| # | Piece | Owner | Scope |
|---|---|---|---|
| 1 | Library: algorithm | CIMBI | v2 charts, local re-solve, `fold_many`, `refold`, `locate`, `route`, `query`, `cover`, resampling of rotated windows, key lineage. Interface in `handoff_cimbi.md` *(pending interface)* |
| 2 | Library: persistence | CIMBI | Tiled snapshot including the capture-time and witness/worst/blocked planes, manifest, `sg_*` index, `sg_meta` (version set last, params and hash, generation, watermark), `store.load/save/watermark/drop` *(pending interface)* |
| 3 | Ingest worker | LACI | Per-map single writer (advisory lock) in graph-builder. Pulls not-yet-folded leaves from Arango (at-least-once, idempotent fold), builds `Leaf` (decode PNG, pose and yaw in map frame, resolution, session, capture stamp with the time-source rule, `r_d`, channels by role), batches into `fold_many`, snapshots every N leaves / T seconds / idle, publishes at most every T seconds, emits `Dirty` to the websocket. `run_id` stamped on the node at the same write as the costmap record |
| 4 | Lifecycle | LACI | Saga: drop `sg_*_{id}` (index) in `map_delete`; snapshot dies with the bucket. Node delete -> debounced local re-solve. Realign (`MAP.SESSION_REALIGNED`, check whether `origin_map` and node poses are rewritten) -> background rebuild. Backfill script for existing maps (precedent: edge-stats) incl. `occupancy_depth` when released |
| 5 | Read API | LACI | `GET /maps/{id}/scene-graph?bbox=&level=&version=` (charts with driven / ever-blocked fractions, links with length, width and channel integrals, hierarchy, version, ETag, lineage); `scene_graph_updated {version, dirty, lineage}` on `/ws/map/{id}`; `MAP.SCENE_GRAPH_*` events. Chart geometry encoding (outline polygons vs raster tile) is open with CIMBI |
| 6 | Client | LACI | `SceneGraph` types, `useSceneGraph`, chart / link layers (driven vs not-driven free space is new information worth showing); map window and goal picking use it; selections and saved goals re-mapped through lineage; `/graph` feeds only per-run log views (`RunStatsTab`, trace). Thumbnails: a representative visited leaf per chart |
| 7 | Planner | LACI | Behind a flag, with per-request fallback to the old search when the scene graph is missing, stale (version lag over a threshold) or start/goal not navigable after snapping. `locate` -> `route` (corridor then refine, price computed from planes at plan time) -> simplified waypoints. Check `Route.min_width` against the planning robot. Blocked nodes and `edgeBlocked` become a transient overlay on the lattice (disk around the position, segment for an edge) |
| 8 | Reconstruction link | LACI | `cover(chart_keys, k)` into the reconstruction manifest for room-level jobs; needs CIMBI's `cover` |
| 9 | Ops | LACI | Dependencies (scipy and whatever v2 needs), Dockerfile/compose, CLAUDE.md, metrics (build latency, version lag, closure size), integration test on real Arango/Postgres |

Dropped: "link cost from observation" (edge observations accrue per leaf edge; routes no longer follow leaf edges).

## Planner contract

* `waypoints[i].node_id`: the nearest leaf within a threshold, else `None`. The dispatcher resolves a robot-reported
  blocked node by `waypoints[i].node_id` with a synthetic `@x,y` fallback, so position matching still works.
* `start_node_id` / `end_node_id` / `path` / `path_length`: filled from the snapped leaf pair and the lattice path
  length; the first-node drop logic keeps the same inputs.
* New response fields (additive, optional): `min_width`, `chart_keys` of the route.

## Order

* **Now, independent of the interface**: time-source rule and capture stamp at ingest; `r_d` source (robot footprint
  lookup); layer-to-role configuration in graph-builder; `run_id` stamping; delete-saga drop hook; `bbox`/`level`
  route skeleton and websocket message shape; planner flag, fallback and waypoint-simplification stub; test harness
  against a fake `SceneGraphStore`.
* **After the interface freeze**: ingest worker against the real library, read API, client layers, planner on
  `route`. The planner switches on last, per map, after a corridor/door map has validated the structure (the one real
  map so far is a single room).

## Open

* CIMBI: source of "ever blocked"; chart geometry encoding for the client; whether the closure is bounded by scale;
  `occupancy_depth` backfill timing.
* LACI: time-source fallback bound; where the robot footprint (`r_d`) lives; the leaf threshold for
  `waypoints[i].node_id`.
