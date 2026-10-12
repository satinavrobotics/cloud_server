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
| 4 | Lifecycle | LACI | Saga: drop `sg_*_{id}` (index) in `map_delete`; snapshot dies with the bucket. Node delete -> debounced local re-solve. Realign: see "Review findings" (node poses are not rewritten, so no rebuild trigger unless the legacy tool runs). Backfill script for existing maps (precedent: edge-stats) incl. `occupancy_depth` when released |
| 5 | Read API | LACI | `GET /maps/{id}/scene-graph?bbox=&level=&version=` (charts with driven / ever-blocked fractions, links with length, width and channel integrals, hierarchy, version, ETag, lineage); `scene_graph_updated {version, dirty, lineage}` on `/ws/map/{id}`; `MAP.SCENE_GRAPH_*` events. Chart geometry encoding (outline polygons vs raster tile) is open with CIMBI |
| 6 | Client | LACI | `SceneGraph` types, `useSceneGraph`, chart / link layers (driven vs not-driven free space is new information worth showing); map window and goal picking use it; selections and saved goals re-mapped through lineage; `/graph` feeds only per-run log views (`RunStatsTab`, trace). Thumbnails: a representative visited leaf per chart |
| 7 | Planner | LACI | Behind a flag, with per-request fallback to the old search when the scene graph is missing, stale (version lag over a threshold) or start/goal not navigable after snapping. `locate` -> `route` (corridor then refine, price computed from planes at plan time) -> simplified waypoints. Check `Route.min_width` against the planning robot. Blocked nodes and `edgeBlocked` become a transient overlay on the lattice (disk around the position, segment for an edge) |
| 8 | Reconstruction link | LACI | `cover(chart_keys, k)` into the reconstruction manifest for room-level jobs; needs CIMBI's `cover` |
| 9 | Ops | LACI | Dependencies (scipy and whatever v2 needs), Dockerfile/compose, CLAUDE.md, metrics (build latency, version lag, closure size), integration test on real Arango/Postgres |

Dropped: "link cost from observation" (edge observations accrue per leaf edge; routes no longer follow leaf edges).

## Planner contract

* `waypoints[i].node_id`: the nearest leaf within a threshold, else `None`. The dispatcher resolves a robot-reported
  blocked node by `waypoints[i].node_id` with a synthetic `@x,y` fallback, so position matching still works.
* `start_node_id` / `end_node_id`: the snapped leaf pair. See "Review findings" for `planned_path` and the rest of
  the contract (supersedes this section where they differ).
* New response fields (additive, optional): `min_width`, `chart_keys` of the route.

## Order

* **Now, independent of the interface**: time-source rule and capture stamp at ingest; `r_d` source (robot footprint
  lookup); layer-to-role configuration in graph-builder; `run_id` stamping; delete-saga drop hook; `bbox`/`level`
  route skeleton and websocket message shape; planner flag, fallback and waypoint-simplification stub; test harness
  against a fake `SceneGraphStore`.
* **After the interface freeze**: ingest worker against the real library, read API, client layers, planner on
  `route`. The planner switches on last, per map, after a corridor/door map has validated the structure (the one real
  map so far is a single room).

## Interface draft 0.9 (CIMBI, 2026-10-12): resolved and consequences

Types are in `packages/scene_graph/api.py` and `store.py` (CIMBI's, pinned by `test_api_contract.py`).

* **Rotation**: `Leaf.origin_yaw`; the library resamples. The adapter passes `costmap.{layer}.origin_map` through.
* **Geometry**: `query(bbox, level, raster=True)` -> `ViewResult.raster` (`OwnerRaster`: u16 owner index plane,
  keys, `dependent` plane). No polygons from the library: the API serves the raster (PNG/u16, per bbox) and the
  client derives outlines. `Chart.leaf` gives a thumbnail leaf.
* **Lineage**: `Dirty.retired: key -> successors` (largest overlap first, `[]` = vanished), persisted per version
  (`lineage-{version}.json`; `store.lineage(map_id, from_version)` follows). The API exposes it; the client walks
  forward from the version of its selection.
* **Corridor**: chart graph always loaded (small); cells tiled. `corridor(a, b, blocked) -> tiles`, then planner
  loads tiles, then `route`; `route` raises `MissingTiles` and never does I/O.
* **Ever blocked** = costmap lethal (100) in any folded window, OR-merged. Robot blocked events are not an input to
  the library; they stay the planner's transient `Blocked` overlay. No event delivery from the dispatcher.
* **Closure bound**: bounded by the sight extent of the affected anchors, not by chart scale. Flat where walls bound
  sight (7k -> 17k cells: 1.7 -> 2.2 s per leaf), grows with sight extent in long halls and open fields (slope 0.6).
  No sight-range cap yet; CIMBI raises it for the outdoor case. **Consequence for us**: the "per-leaf cost flat in
  map size" claim holds for indoor maps only until a cap exists; the worker must batch (one re-solve per batch,
  debounced) and the publish interval is a setting, not a constant; the scale gate adds an open-field world.

## Review findings (2026-10-12, two read-only reviews; claims spot-checked)

Verified in code unless marked. Supersedes earlier statements here and in `handoff_cimbi.md` where they differ.

### Changes to the plan (LACI)

1. **Planner contract.** `/plan` returns `planned_path` (no `path_length`; `path_length` exists only on
   NavigationResponse and is a node count). The leg tracker, edge-stats and `_resolve_node_ref` assume
   `waypoints[i] <-> planned_path[i]`, same length, one id each; `leg_tracker.route_table` silently drops topomap
   ids on a length mismatch (degrades to VDA ids only, no crash). Therefore:
   * waypoints carry `map_id`, x, y, `theta` = path bearing (final = goal heading), `node_id` = nearest leaf within a
     threshold else None;
   * `planned_path` is set only when every waypoint has a leaf id (legacy fallback routes); otherwise None;
   * `min_width`, `chart_keys`, `planner` ("scene_graph" | "legacy") and the graph `version` are added to
     PlanResponse (its constructor lists fields one by one) and NavigationResponse; `failed_at` values, the
     `blocked_nodes` rows and `ignore_exclusions` stay for the client;
   * the robot position goes through the existing `_robot_xy_in_map` before `locate`; the H5 "drop first node if
     behind" test is not applied to lattice routes;
   * leg recording and edge-stats on lattice routes: legs are not leaf edges, so they are tagged by chart key; the
     leaf-edge run views keep working for legacy routes only (decision pending).
2. **Blocked overlay.** A `blocked_graph_nodes` row holds the x,y of the blocked waypoint and edge node ids, not
   positions; with sparse waypoints a disc at the far waypoint is not where the obstacle is, and the reroute check
   tests waypoints only. Add columns (robot pose at the block, previous waypoint x,y) by migration, and
   segment-versus-disc tests in `blocked_nodes.rows_hit` and `_reroute_through_blocked`. `route() -> None` maps to the
   existing `failed_at` values; an incomplete graph or `MissingTiles` is never reported as "unreachable".
3. **Ingest cursor (the previous design was wrong).** The costmap is written to the node after the node (buffered
   or offline uploads, second layers), and a capture-time watermark never discovers a leaf stamped below it. The
   cursor is server-side and monotone: `stored_ms` set in `_save_layers` with the costmap record, an index on it,
   pull by `(stored_ms, key)` with a replay margin. Capture stamps stay inside the fold. `Dirty.watermark` is
   opaque to LACI.
4. **Clock skew.** The 7-day age bound (`scene_leaf`) lets an ahead clock beat newer correct observations
   permanently. Replace with a per-session offset (receipt of session start / telemetry minus robot stamp) applied
   to the whole session, clamped to receipt, whole-session fallback to receipt beyond a bound of minutes; metric per
   robot. Not yet implemented: `scene_leaf` still has the per-leaf rule.
5. **A version exists only as the return of `save()`;** publish = save; websocket and ETag after the manifest and
   `sg_meta` write; version numbers monotonic across restarts and generations, assigned by the store.
6. **Lifecycle.** The worker checks map lifecycle (`DELETING`) before every save so it cannot recreate `sg_*` or the
   bucket after the drop; save is a compare-and-set on the expected previous version (fencing for a zombie writer);
   `scene_graph` added to `NON_NODE_PREFIXES`; the real bucket name is `bucket_name(map_id)`, not `map-{id}`.
   **Node delete** happens in the API process: capture the leaves' window extents before delete and deliver them to
   the worker (outbox table or NOTIFY). **Realign** rewrites no node poses (only `map_sessions`' transform): no
   rebuild trigger (to verify against `plan_realign`); only the legacy pose-rewrite tool needs a rebuild.
   A params change (robot size, sight range) is a new generation: background rebuild, old version served until the
   atomic switch.
7. **Raster over HTTP.** Browser decoders reduce 16-bit PNG to 8 bits: serve raw little-endian u16 with a small
   header (octet-stream), with ETag/version.
8. **Client.** `MapWebSocket.handleMessage` ignores unknown types and the publisher has no replay: add a
   `scene_graph_updated` branch, refetch on `onConnect`, `since_version` catch-up. Goals are stored as `{x, y,
   map_id}`, so only selections need lineage re-mapping. The sati-client repo has no `feat/scene-graph-v2` branch.
9. **Ops.** graph-builder needs numpy, scipy and Pillow; mission-planner numpy and scipy; the API scipy (numpy
   1.26.4 pinned: pick a compatible scipy); each Dockerfile (and BUILD) must COPY `packages/scene_graph/`.
   `packages/scene_graph/__init__.py` still exports v1 names that clash with `api.py`.
10. **Event codes** need no migration (`Source.GRAPH_BUILDER` is allowed): `EventCode` + `CODES` + payload class +
    client mapping.
11. **Planner budget.** The dispatcher replans with a 5 s timeout: corridor, tile load, route and simplify must
    fit (tiles cached by `(map, version, tile)`), else it falls back silently. Define a budget; log the fallback.
12. **Backfill** of existing maps: hours at the builder's measured cost; big `fold_many` batches, one snapshot,
    resumable, per-map planner flag gated on "backfill complete and validated".
13. **Authorization** on the new routes follows `/maps/{id}/graph`; cap bbox area and raster size; rate limit.
14. **Tests**: property tests for idempotency (duplicate delivery, shuffled order, different batch splits: same cells
    and planes) and a plan -> order -> VDA5050 schema validation test.

### For CIMBI

* Version-pinned reads: `sg_anchors/sg_links` are unversioned (latest only), so `?version=N` cannot return chart
  attributes or links for N: put them in the manifest/tiles, or pin cells only.
* Time plane `u32` ms wraps at 49.7 days: define a base in the manifest or use seconds/u64.
* `store.py` lacks `lineage()`, snapshot retention/GC (keep last K plus leased/pinned, shared tiles, compacted
  cumulative lineage with "too old, reselect"), and the watermark lives in both manifest and `sg_meta` (keep one).
* Rebuild: `save` has no generation; a fresh instance has no lineage to the old charts: need begin/commit generation
  and a key mapping by overlap, or an in-place refold over the union bbox.
* Writer tile loading: `MissingTiles` only covers `route`; `fold_many`/`refold` need `tiles_for(leaves)` or lazy
  backing, plus peak memory in the scale gate. The closure is also not bounded by tiles.
* Winner-leaf plane (for "recompute the cells a deleted leaf won") is not among the planes; delete needs it or a
  full refold of the leaf's bbox.
* Which `r` partitions the free space (`robot_half_side` per map vs `r_d` per leaf) and what a smaller or larger
  robot sees; `ever_blocked` OR-merge of lethal cells fills with people and noise: provenance or decay, and what the
  `occupancy` layer contains (inflated, dynamic obstacles) and which of 1..99 is navigable.
* `View` (for `cover`) has no source in the node documents; defer piece 8 or define it.
* `0.05 m` window cells against `lattice=0.1`: say which resolution `window.to_lattice` targets.

### Decisions for LACI's user

* `run_id`: derive later from session and time (recommended) or look up at ingest.
* `r_d` rule (decided by LACI, **to be rethought**): rectangle footprint -> half of the *smaller* side (width);
  -1 until the factsheet arrives -> params default, counted. Known cost: optimistic for a long robot (a passage it
  cannot turn in can look navigable); `Route.min_width` is checked against the same rule so it does not cover length.
  Revisit before the planner switches on for a robot with length >> width.
* Leg recording on lattice routes: chart keys for new run views, leaf-edge views for legacy only.

### CIMBI's answers to the store requests (2026-10-12; store.py semantics are theirs)

* `save(map_id, sg, dirty, expected_version, cursor, generation=None)`: compare-and-set on `sg_meta.version`
  (raises `StaleVersion`), `cursor` = opaque JSON persisted in the same step; one watermark only (`sg_meta`).
  `meta()` -> `{}` for a never-saved map, else `{version, generation, params, params_hash, cursor}`.
* Per-version manifest holds charts + links (version-pinned reads); `sg_*` Arango docs index the CURRENT version only.
* Time plane int64 ms; `ever_blocked` becomes a time plane (latest capture time reported blocked, max-merge):
  consumers decay by age, no provenance needed.
* `lineage(map_id, from_version)` compacted cumulative, "too old, reselect" signal; retention = last K (default 5)
  + leased versions; tiles shared by content hash. `begin_generation`/commit with a pointer switch and a persisted
  old->new key map. `tiles_for(leaves)`; `fold_many` raises `MissingTiles` before touching state.
* Delete: no winner plane; the time plane identifies the cells a leaf won (time == its capture stamp): refold the
  bbox from the overlapping leaves. **Consequence for our clock correction**: the stamp a leaf is folded under must
  be stable (see "Open"), or that lookup and idempotency break.
* `Leaf.r_d` is evidence only (W(r) needs r_d >= r); `SceneParams.robot_half_side` per map partitions free space.
  Only 100/255 block; 1..99 is the soft channel (`occupancy_depth` has only 0/100/255).
* `cover()` view: from the node's depth record via `leafcover.view_from_node(key, pose, camera.T_base_cam, camera.fx,
  camera.width, camera.valid_range_m[1], h=0.1, p=0.2)`; no extra node field. Piece 8 is unblocked.
* Backfill: `occupancy_depth` written on 7 maps / 438 nodes via `set_node_costmaps` (verified, restore path in
  CIMBI's scratchpad); `office8` has `occupancy`.

### Scale gate with `sight_range = 5.0` (CIMBI prototype, Python, per-leaf local update, 2026-10-12)

* Walled halls are flat: 4.2k / 12.6k / 31k cells = 0.44 / 0.73 / 0.71 s per leaf (slope 0.24); without the cap
  0.60 s / 27 s / did not finish. **Met for walled floors.**
* **Open fields not met**: a 4.7k-cell field took 3.1 s per leaf (worst 44 s); larger ones did not finish. Row
  recomputation is to be bounded by the leaf's own 5 m window in the port. Budget: do not assume per-leaf latency in
  open space; the worker batches and the planner switch stays off for open-field maps until the gate passes.
* `office8`: the cap never binds; keys 14 kept / 0 born / 0 retired. On 12 of 36 synthetic cases the partition
  changes (far dead-end cells become dependents). `sight_range` is in the params hash.
* Links are one per connected stretch of lattice-adjacent boundary cells (diagonal-only contact does not link).

## Open

* CIMBI: sight-range cap for open fields; `occupancy_depth` backfill timing; store `lineage()` and engine.
* LACI: **stable per-leaf stamp** (done): the worker writes the stamp it first computed onto the node
  (`scene_stamp_ms`, first write wins, written BEFORE the fold so a replay after a crash cannot recompute another
  one) and `leaf_stamp` always prefers it. Nodes folded before this change get their stamp at their next fold.
* LACI: time-source fallback bound (7 days, implemented in `scene_leaf`); where the robot footprint (`r_d`) lives; the
  leaf threshold for `waypoints[i].node_id`; `run_id` (derive later from session and time, or look up at ingest).


## Blocked overlay: draft for review (nothing below is applied)

Done and unwired: `packages/utils/blocked_geometry.py` (+ tests): the obstacle is the segment from `LEAD_M`
(1.0 m, must exceed `HALF_WIDTH_M` 0.5 m or a leg leaving the robot sideways is "blocked" by its own start) ahead
of the robot's pose to the blocked waypoint; `route_hits_row` tests the route's legs against it; `overlay` yields
`(discs, segments)` for `Blocked`. Rows without a robot pose keep the old disc rule.

Needs your go-ahead (changes shared tables and the dispatcher):

1. Migration `20261012_01_blocked_graph_nodes_geometry` (additive, nullable, no backfill; downgrade drops them):
   `ALTER TABLE blocked_graph_nodes ADD COLUMN IF NOT EXISTS robot_x double precision,
   ADD COLUMN IF NOT EXISTS robot_y double precision, ADD COLUMN IF NOT EXISTS prev_x double precision,
   ADD COLUMN IF NOT EXISTS prev_y double precision;`
   `prev_x/prev_y` (previous waypoint) is kept for a later fit; the overlay does not use it yet.
2. `blocked_nodes.COLUMNS`/`UPSERT_SQL`/`upsert_params` gain the four columns. Must ship **after** the migration
   (ACTIVE_FOR_MAP_SQL selects them), same release, migration first.
3. mission-dispatch's edgeBlocked handler passes the robot's pose (latest robot row in map frame) and the previous
   waypoint; rows from before keep NULLs.
4. `_reroute_through_blocked` (API) and `blocked_nodes.rows_hit` additionally call `route_hits_row` on the route's
   polyline; the planner passes `overlay(...)` to `route()` when the scene-graph planner is on.
5. Open: the 1.0 m lead assumes obstacles are seen at more than 1 m; a robot that stops at 0.6 m reports a segment
   of near zero length (clipped to the waypoint) which is a disc in effect. Acceptable, or use the costmap's
   lethal cells at the block instead (needs the dispatcher to send them; not proposed).
