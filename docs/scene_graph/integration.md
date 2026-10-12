# Scene graph: integration plan

Status (2026-10-13): built on `feat/scene-graph-v2` and exercised end to end against real ArangoDB / MinIO / Postgres
by the e2e harness (below); not yet run on a production stack. The sections below are the design and the decisions
behind it; "Order and status" and "Open defects" are the current state. Operations (settings, deploy order, rollback,
events, troubleshooting): `runbook.md`, not repeated here. Companion to `design.md` (v1 algorithm, replaced) and
`handoff_cimbi.md` (the contract with CIMBI). Items marked *(pending interface)* were written before the library
interface was fixed; read them against `packages/scene_graph/api.py`.

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
   the delete saga (`graph_db.delete_map` drops `nodes_`/`edges_`/graph and, since the scene-graph work, also the map's
   `sg_*` collections; the saga's scene step additionally removes the `scene_graph/` objects under the worker's lock).
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

### Planner wiring (`plan_route`, `SCENE_GRAPH_PLANNER_ENABLED`, default off)

`MissionPlannerService.plan_route` tries the chart graph first when the flag is on and otherwise runs the legacy
steps unchanged. The chart route needs all of: a placed robot, a stored graph, snaps within limits, a route, and one
distinct leaf id per waypoint. Anything else is a fallback (`planner: "legacy"`, `planner_fallback: <reason>`).

1. **Graph**: `SceneGraphStore.meta(map)["version"]` pins the version; `store.load(map, version)` runs in a thread and
   is cached by `(map, version)` (LRU, `SCENE_GRAPH_CACHE_VERSIONS`). A cold load is waited for at most
   `SCENE_GRAPH_LOAD_TIMEOUT_S`; it continues in the background (shared by later requests, fills the cache), the
   request falls back (`load_timeout`).
2. **Start/goal**: robot position through `_robot_xy_in_map`, then `snap` with `SCENE_GRAPH_START_SNAP_M` /
   `SCENE_GRAPH_GOAL_SNAP_M`; refused by `snap_problem` (`start_`/`goal_` + `no_navigable_space` | `too_far` |
   `crosses_blocked`).
3. **Overlay**: active `blocked_graph_nodes` rows (none with `ignore_exclusions`) go through
   `blocked_geometry.overlay(rows, MATCH_RADIUS_M, half_width)` into `Blocked(discs, segments)` and are passed to
   `route()` (`plan_on_graph`). The same overlay is applied in the line-of-sight test of the simplification, so no
   shortened leg crosses it. A route that exists without the overlay but not with it: `blocked_by_overlay`.
4. **Route**: `route(start_key, goal_key, blocked)`. Lattice route runs anchor to anchor, so the point list is
   `[snapped start] + route.nodes + [snapped goal]`. `None` -> `unreachable`, `MissingTiles` -> `missing_tiles`.
5. **Waypoints**: `simplify` (line of sight = every half-step sample is a free lattice cell by `snap`, memoised per
   cell; gap cap `SCENE_GRAPH_MAX_WAYPOINT_GAP_M`; a stub under 0.5 m at the end is merged) -> `build_waypoints` with
   the nearest leaf within `SCENE_GRAPH_LEAF_SNAP_M` (`graph_db.k_nearest_neighbors`). `planned_path` = those ids
   and `start_node_id`/`end_node_id` its ends. If any waypoint has no leaf or two share one: fallback
   (`waypoints_without_distinct_leaf_ids`).
6. **Budget**: snap + route + simplify + leaf lookup within `SCENE_GRAPH_PLAN_BUDGET_S` (`plan_timeout`).

Response additions: `planner` (`"scene_graph"` | `"legacy"`), `min_width`, `chart_keys`, `graph_version`,
`planner_fallback` (only when the flag is on and the legacy planner ran; on plan and navigation responses). Reasons
(meaning and operational reading: `runbook.md` section 7): `robot_unavailable`, `robot_not_placed`,
`store_unavailable`, `store_timeout`, `store_error`, `no_graph`, `load_timeout`, `load_error`, `stale_graph`,
`start_*`, `goal_*`, `blocked_by_overlay`, `unreachable`, `missing_tiles`, `too_narrow`,
`waypoints_without_distinct_leaf_ids`, `blocked_row_on_route`, `plan_timeout`, `error`.

* **`stale_graph`**: the oldest costmap stored after the loaded version's ingest cursor is older than
  `SCENE_GRAPH_MAX_VERSION_LAG_S` (default 120, 0 disables). Unknown lag never rejects.
* **Stale-version serving**: when a new version is still loading at the timeout, the newest version already loaded
  for the map is used, bounded by the lag test against that version's own cursor; `load_timeout` is raised only
  with nothing loaded.
* **`too_narrow`**: `Route.min_width` < factsheet width + `SCENE_GRAPH_WIDTH_MARGIN_M`. `Route.min_width` is the
  narrowest `Link.width` the route crosses and carries the lattice error (+-0.05 m, half a lattice step), so the rule is
  `min_width >= 2 r' + 0.05` for an aligned passage and `>= 2 r' sqrt(2) + 0.05` where the robot must turn in place
  (r' = `r_d`, half the smaller footprint side). Gated by `SCENE_GRAPH_WIDTH_CHECK_ENABLED`, **default off** (enable
  deliberately after checking known corridors; the margin default in `config.py` is still 0.0, the planner owner
  decides whether it becomes 0.05); with it off `min_width` is still reported.
* **`blocked_row_on_route`**: the legacy point rule on the final waypoints' leaf ids, applied only to rows without a
  robot pose (those the overlay cannot place); the legacy planner then reports or detours them.
* The H5 first-node-behind test and the `failed_at` legacy-rule overlay do not apply to chart routes: the route
  starts at the snapped robot position, so there is no node behind it.

## Order and status (2026-10-13)

* **Done (LACI)**: ingest adapter, stamp rule and per-session clock, stable stamp, worker, lock and lifecycle
  guard, runner and host (`scene_host`, started by graph-builder `main.py` behind `SCENE_GRAPH_WORKER_ENABLED`),
  `costmap_stored_ms` cursor and backfill tool, generation rebuild and rebuild request, per-map switch, delete-saga
  drop, planner (`plan_route`, `plan_on_graph`) behind `SCENE_GRAPH_PLANNER_ENABLED` with the width, lag and
  overlay checks and `planner_fallback` on plan and navigation responses, blocked overlay wired into `route()`,
  read API and websocket message, planner Dockerfile and compose environment (numpy/scipy in the requirements, MinIO/Arango
  secrets and the planner variables in compose), authorization audit (result: none, below), ingest hardening,
  operator runbook, end-to-end harness, client wiring (sati-client, see its history).
* **Done (CIMBI)**: engine behind `api.py`, `store.py` (versioned snapshots, CAS, generations); per-batch cost
  rounds 1 and 2; route passage width (`Route.min_width`), store version semantics (cursor and canvas are not in the
  content hash), `meta()` epoch / `created_ms`, BUILD target `packages/scene_graph`, real-MinIO adapter test.
* **Verified**: `tools/scene_graph_e2e/run.sh` brings up a throw-away ArangoDB + MinIO + Postgres
  (`docker_compose/scene_graph_e2e.yaml`, images pinned like production) and runs `tests/integration/scene_graph`, plus
  strict xfails that pin the open defects below (a fixed defect fails the run, so its marker gets removed).
  `tests/unit/scene_graph/test_store_minio.py` runs the store against a real MinIO when `SG_TEST_MINIO_ENDPOINT` is set.
* **Open**: "Open defects". Not verified on a production-sized map or stack.

### Measurements (e2e / bench, 2026-10-13)

* 90 x 90 m, 225 leaves: `store.load` 28.5 s, `route` 62 ms, `plan_on_graph` about 100 ms, warm `snap` about 10 us.
  A cold load on a map this size exceeds `SCENE_GRAPH_LOAD_TIMEOUT_S`; the stale-version serving above keeps the
  planner on the chart route while a new version loads.
* Open rooms of 12-15 m: a fold takes minutes (engine); open-field maps stay off (runbook section 8).

### Open defects

| Owner | Defect | Effect / decision |
|---|---|---|
| CIMBI | Fold result depends on capture-stamp order: `min_width` ranges 0.1..0.64 over permutations of the same leaves | breaks the order-independence claim; strict xfail |
| CIMBI | Restart replay still bumps the version: the canvas is out of `content_hash` now, but each chart's canvas-relative `cell` is still hashed (e2e: `cell` [87, 88] vs [67, 68] after a reload) | a restart publishes a version with no new data; strict xfail |
| CIMBI | Growth-batch fix; open-field decision | pending |

Fixed since the previous status: `Route.min_width` is the passage width (`Route.min_width` / `ChartGraph.route`; `too_narrow`
can now be enabled, see the planner section), the canvas is out of `content_hash`, `store.meta()` carries
`epoch` and `created_ms`, the real-MinIO adapter test exists, `TopomapDatabaseClient.delete_map` (dead; the saga calls
`graph_db`/`image_db` directly) was removed.

Known limits: `r_d` = half the smaller footprint side (decision pending, "Decisions for LACI's user"); no per-extent
re-solve (every delete or params change is a whole-map rebuild); bucket-slug collisions skip both maps. There is no
Bazel workspace in this repository: the BUILD files (`packages/scene_graph`, graph_builder, api, mission_planner) are
kept syntactically plausible and have never been built; the Dockerfiles are what ships.

### Scene ingest: behavior fixed after review (2026-10-12)

* **Restart**: the engine is built after the writer lock is held (`engine_factory(stored_version)`, pinned to the
  version read under the lock), so a replica that does not own a map holds no engine. `engine.version` (the engine's
  fold counter, `manifest.engine_version`) is not compared with the store's snapshot `version`; they are different
  counters, and comparing them sent every restart to `needs_rebuild`.
* **Rebuild**: while a pending generation is being rebuilt every save expects the LIVE version (a pending save
  returns the pending generation's own counter); `commit_generation` returns the new live one.
* **PNG fetch**: "object not there" (NoSuchKey / NoSuchBucket) is kept pending and asked again every
  `png_retry_s`, given up after `png_expire_s` of wall clock (default: the replay margin, 60 s); the persisted
  cursor stays below the oldest pending node. A fetch that raises (MinIO down) is a BACKOFF: reading stops at that
  node, nothing past it is handled, no leaf is skipped.
* **Stamp**: a stamp is kept on the node (first write wins) when it is final. The receipt-time stamp that a
  session gets until its clock has `min_obs` observations is NOT kept: it is never newer than the receipt and a
  replay may use the better estimate. Not kept means a leaf folded under the placeholder can be folded again
  under an earlier corrected stamp; the earlier fold stays in the cells it won (newest capture wins), so the
  cost is a leaf that is slightly "too new" in those cells until newer data arrives.
* **Terminal states** (`fenced`, `needs_rebuild`, `deleting`) are logged at warning and reported once per
  transition as `MAP.SCENE_GRAPH_FOLD_FAILED` with `stage` = the state; a map starved by `max_maps` is reported
  once with `stage = "max_maps"`. A lifecycle query that fails is a BACKOFF, never "deleting".
* **Polling**: an idle map backs off from `SCENE_GRAPH_IDLE_S` (2 s) up to `SCENE_GRAPH_IDLE_MAX_S` (30 s); the
  lifecycle / lock lookup runs at most every `SCENE_GRAPH_LIFECYCLE_CHECK_S` (5 s) and always before a save; the
  cursor query returns only the fields the worker reads and does not check the collection every time.

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
   robot. Implemented since: per-session clock offset in the adapter.
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
   map_id}`, so only selections need lineage re-mapping. Client wiring landed in sati-client.
9. **Ops.** graph-builder and mission-planner need numpy and scipy (pinned 1.26.4 / 1.11.4); the API scipy; each
   Dockerfile (and BUILD) must COPY `packages/scene_graph/`. Pillow turned out not needed: PNG decode is `zlib` +
   numpy in `scene_adapter`.
   `packages/scene_graph/__init__.py` still exports v1 names that clash with `api.py`.
10. **Event codes** need no migration (`Source.GRAPH_BUILDER` is allowed): `EventCode` + `CODES` + payload class +
    client mapping.
11. **Planner budget.** The dispatcher replans with a 5 s timeout: corridor, tile load, route and simplify must
    fit (tiles cached by `(map, version, tile)`), else it falls back silently. Define a budget; log the fallback.
   (Done: `SCENE_GRAPH_PLAN_BUDGET_S`, the `Chart planner not used (<reason>)` log, `planner_fallback`.)
12. **Backfill** of existing maps: hours at the builder's measured cost; big `fold_many` batches, one snapshot,
    resumable, per-map planner flag gated on "backfill complete and validated".
13. **Authorization.** Audit result: there is **no** authorization anywhere in the API (not on `/maps/{id}/graph`,
    not on the three scene-graph routes), so "follows `/maps/{id}/graph`" means none. Recommended design: one
    `authorize_map` dependency on every map-scoped route, run **before** the read cache, 404 for a map the caller may
    not see (no existence leak), responses `Cache-Control: private`. Not built (an API-wide change). Bbox area and
    raster size are capped (done); no rate limit.
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
* Leg recording on lattice routes: chart keys for new run views (derived at read time,
  `GET /runs/{id}/legs?with_chart_keys=true`), leaf-edge views for legacy only.

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
* Resolved: time-source fallback bound (7 days, in `scene_leaf`); robot footprint (`r_d`) from the factsheet; leaf
  threshold for `waypoints[i].node_id` (`SCENE_GRAPH_LEAF_SNAP_M`); `run_id` (derived at read time, below).
* Remaining: "Open defects" under "Order and status".


## Blocked overlay (built; 2026-10-12/13)

`packages/utils/blocked_geometry.py` (+ tests): the obstacle is the segment from `lead` ahead of the robot's pose to
the blocked waypoint; `route_hits_row` tests the route's legs against it; `overlay` yields `(discs, segments)` for
`Blocked`. Rows without a robot pose keep the old disc rule.

1. **Half width** is half the *smaller* footprint side from the planning robot's factsheet
   (`half_width_for(length, width)`, floor 0.05 m); the constant `BLOCKED_SEGMENT_HALF_WIDTH_M` (0.5) only while the
   footprint is unknown (-1). **The lead scales with it** (`lead_for`: at least `LEAD_M` 1.0 and half width + 0.5),
   keeping lead > half width (or a leg leaving the robot sideways is "blocked" by its own start). The planner
   (`_overlay_half_width`) passes the robot's value; the reroute check (`_reroute_through_blocked` in
   `packages/api/main.py`, from `PUT /missions/{name}` with `update_nodes`) takes the mission's robot's factsheet the
   same way (`_mission_half_width`, one `RobotObjectV1` read); an unreadable robot keeps the constant.
2. **Migration `20261012_01_blocked_geometry`** (additive, nullable, no backfill; downgrade drops them). Four
   columns, `robot_x, robot_y, prev_x, prev_y`; only `robot_x/robot_y` are written (robot position in the map frame
   at the report, from the reporting state, taken to the map frame by the order's transform). `prev_x`/`prev_y` are
   no longer written and nothing reads them. The upsert is `COALESCE(EXCLUDED.robot_x, blocked_graph_nodes.robot_x)`
   (same for y): a re-report without a position keeps the earlier one. **Deploy order: migration first** (no
   fallback for an unmigrated database; runbook section 2).
3. `blocked_nodes.rows_hit(..., polyline=)` adds `route_hits_row` to the point rule; the API's
   `_reroute_through_blocked` passes the route's waypoints, the legacy planner's `_path_avoiding` does the same for
   its detour. Rows without a pose: behaviour as before.
4. **Wired**: the planner hands `overlay(...)` to `route()` and to the line-of-sight test (`plan_on_graph`);
   `blocked_by_overlay` and `blocked_row_on_route` are the fallbacks.
5. Stops under `lead` (conservative, kept): `row_segment` clips the start to the waypoint's own distance, so the
   segment runs from the robot's pose to the waypoint; a detour leaving sideways from a robot that stopped under a
   half width from the obstacle is refused until the block expires or is cleared.
6. **Chart keys are derived at read time, nothing is stored**: `leg_chart_keys(polyline, locate)` and
   `row_chart_keys(row, locate)` (`blocked_geometry`, `blocked_chart_tags`). `GET .../blocked-nodes?with_chart_keys=true`
   adds `chart_keys` per row, best effort (no graph, no pose or a store error leaves it out). **Run legs the same way,
   no migration**: `GET /api/v1/runs/{run_id}/legs?with_chart_keys=true` adds `chart_keys` to each leg: the charts the
   straight line from the position of `from_topomap_node` to that of `to_topomap_node` crosses
   (`blocked_chart_tags.add_leg_chart_keys`; node positions by `graph_db.get_node`, at most 400 distinct nodes, one
   owner-plane read per map, 2 s). Absent on every leg when the map has no readable graph, `null` for a leg whose node
   positions are unknown. The stored legs are untouched, so a legacy run gets keys too. The mission aggregate
   (`/missions/{name}/legs`) is not tagged.
8. **Edge rows are not segments (documented limit)**: a row with `edge_from`/`edge_to` but no robot pose (reported
   after an offline gap, or written before the pose columns) has only the position of the blocked waypoint, so it is
   a disc (point rule) like any pose-less row. The position of `edge_from` is not in the row (`prev_x`/`prev_y` are not
   written, item 2) and a segment from the previous waypoint would also condemn the stretch the robot just drove.
   Making it a segment needs a graph lookup of `edge_from` in the reader (planner, API) or writing `prev_x/prev_y`
   again; not done.
7. **`run_id` of a blocked row** is derived from `vda_node_id` (`run_id_of`: the dispatcher's run prefix) and
   resolved against `mission_runs` (`resolve_run_ids`: the run of the same mission and robot that started last
   before the report). Exact for the first pass of a mission; a heuristic for repeat passes (the recorder keeps the
   first pass's row) and for runs adopted after a dispatcher restart. A failed lookup leaves the derived id.

## Rebuild request and per-map switch (2026-10-12)

Both live in the map object's `status` (jsonb), read by graph-builder through `PgGuard` (`scene_guard.py`).

**`status.scene_graph_needs_rebuild`** `{at, reason, nodes}`: set by `maps.delete_nodes` unconditionally (no flag),
first BEFORE the destructive delete (`reason: "nodes_deleting"`; a crash or a failing delete cannot lose the signal, a
spurious rebuild is harmless) and again after it with a fresh `at` (`reason: "nodes_deleted"`), so a rebuild that began
before the delete finished cannot clear the second request. The worker looks it up once at start and then at most every
`WorkerConfig.rebuild_check_s` (30 s) per map. On a request it does what a params-hash mismatch does:
`begin_generation` + `new_engine`, cursor back to zero, every leaf refolded into the pending generation, readers keep
the old version until `commit_generation`. `REBUILD_STARTED/COMPLETED` carry the request's `reason` (`nodes_deleted` from node delete; `needs_rebuild`
stays the params-hash reason). After the commit the key is removed only if its `at` still equals the one rebuilt for
(`UPDATE ... WHERE status->key->>'at' = at`), so a delete that landed during the rebuild survives and causes one more
rebuild. A failed clear is retried at the next check. Notes: a graph that was never saved is simply restarted in place
(no generation); a rebuild already running for a params change also answers the request; a crash mid-rebuild leaves
the key set, so the restart rebuilds again. The rebuild is a full refold (the delete-local refold of "Scale design" is
still CIMBI's `refold`), and unsaved folds of the old engine are discarded. The clear does not `NOTIFY` the object
watchers (nothing watches this key).

**`status.scene_graph_enabled`**: default (key absent) follows `SCENE_GRAPH_WORKER_ENABLED`. A JSON `false` excludes
the map: the runner skips it (a running worker is dropped within two discoveries, about 2 minutes) and reports
`MAP.SCENE_GRAPH_DISABLED_SKIPPED {reason: "map_not_enabled"}` at most once an hour per map. Only `false` counts, and
`true` does not switch a map on while the global flag is off (the worker is not started then). Set it with SQL, e.g.
`UPDATE mapobjectv1 SET status = status || '{"scene_graph_enabled": false}' WHERE name = '<map>'`; there is no API
for it yet. The flags are read in the runner's discovery query (every 60 s, one query for all maps); a failed query
keeps the previous answer.

**Worker-disabled event at startup: not done.** `MAP.SCENE_GRAPH_DISABLED_SKIPPED` requires `map_name`, so a
service-level "worker off" would be one event per map on every restart, and it would need a registry query and
event-DB write from a feature that is off. The existing startup log and the per-map event above cover it.

## Read contract for a map without a graph (2026-10-13)

* `GET /maps/{id}/scene-graph` and `/scene-graph/lineage`: 200 with `available: false` (no graph yet, worker flag
  off on the API, or a dropped graph); `/scene-graph/raster`: 404. All three answer `Cache-Control: no-store`, since
  the answer changes the moment a graph is built.
* `scene_graph_updated` with `version: 0` is the hint "no graph" (an empty rebuild dropped it): clients drop the
  view; it is not a version to pin.
* The identity of a version is its **epoch** `(created_ms, generation)` from the manifest, part of the ETag and of
  every read-cache key: a map recreated under the same id restarts at version 1 but never matches an old ETag.
  Responses are never `immutable`. A drop (delete saga, empty rebuild) purges the map's read caches.

## Ingest ordering (2026-10-13)

* **Canonical rebuild contract**: a rebuild folds the leaves sorted by `(stamp_ms, key)` in fixed batches of 48
  (`rebuild_batch`; no time-based flush until the head of the nodes is reached). The same leaves therefore give the same
  charts after every rebuild. Live batches differ by design: they fold in arrival order with time- and size-based
  flushes, so a live-built graph and a rebuilt one over the same leaves can differ in chart partition.
* **Store, planned by the engine and used by the worker for resume** (CIMBI): `begin_generation(fresh / params_hash)`,
  `meta()["pending"]`, `load(pending=True)`, exported exceptions. Until they land the worker restarts a rebuild from zero
  after a crash.

## Ingest classification (2026-10-13)

* **`commit_generation` failures** are classified: FENCED when the live version is already at or past ours; a
  rebuild restart when our pending generation vanished; otherwise a bounded BACKOFF (`commit_retries`, 5) that then
  ends FENCED. Reported as `fold_failed` stage `save`, detail `commit: ...`.
* **Empty rebuild**: after `empty_rebuild_confirmations` (2) caught-up steps with no foldable leaf the worker calls
  `store.drop`, publishes version 0 and emits `rebuild_completed` (`version: 0`); the request is cleared.
* **`fold_failed` stages**: `fold`, `tiles`, `save`, `load`, and the terminal `deleting`, `fenced`, `needs_rebuild`,
  `max_maps` (table and alerts: runbook section 5). `max_maps` is the per-process cap on owned maps (default 64; `SCENE_GRAPH_MAX_MAPS`); each owned map holds one Postgres connection.
* **Registered maps** are matched by bucket slug (lower-case, `_` equal to `-`, `scene_guard.bucket_slug`); two
  registered maps with one slug are both skipped with an error log (no event).
* **PNG decode** is `zlib` + numpy (no Pillow).
