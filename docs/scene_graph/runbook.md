# Scene graph v2: operator runbook

Scope: deploy, switch on, watch and roll back the scene graph (ingest worker in graph-builder, read API, planner
route). Design and contracts: `integration.md`. Everything below is checked against the source on
`feat/scene-graph-v2`. The worker, store, read API, saga and planner are exercised end to end against real ArangoDB / MinIO / Postgres by the e2e harness (`tools/scene_graph_e2e/run.sh`); no production stack has run them yet (see Known limitations).

Components: **graph-builder** runs the ingest worker (one thread, all maps); **api-delegation-service** serves
`/api/v1/maps/{id}/scene-graph*`, owns the Postgres migrations and the map-delete saga; **mission-planner-service**
plans on the graph; **mission-dispatch** writes blocked-node rows (needs the migration). State lives in MinIO
(`scene_graph/` in the map's bucket, source of truth) and ArangoDB (`sg_*_{map}`, rebuildable index).

## 1. Settings

All read once at process start (restart to change). Defaults in `packages/config.py`.

| Setting | Default | Read by | Meaning |
|---|---|---|---|
| `SCENE_GRAPH_WORKER_ENABLED` | false | graph-builder, **API** | graph-builder: start the ingest worker. API: serve reads (false answers `available: false`), always run the delete-saga drop. (Node delete marks `scene_graph_needs_rebuild` regardless of this flag.) |
| `SCENE_GRAPH_PLANNER_ENABLED` | false | mission-planner | try the chart route first; every failure falls back to the legacy planner |
| `SCENE_GRAPH_BATCH_MIN_LEAVES` / `_MAX_LEAVES` | 6 / 48 | graph-builder | fold once 6 leaves wait; at most 48 per `fold_many`. min > max is clamped to max (warning) |
| `SCENE_GRAPH_PUBLISH_INTERVAL_S` | 5.0 | graph-builder | floor between two saves of one map, and the longest a lone leaf waits for a batch |
| `SCENE_GRAPH_BATCH_BUDGET_S` | 10.0 | graph-builder | a fold slower than this logs a warning (not aborted) |
| `SCENE_GRAPH_IDLE_S` / `_IDLE_MAX_S` | 2.0 / 30.0 | graph-builder | idle poll of a caught-up map, doubling to the max; a quiet map is noticed up to 30 s late |
| `SCENE_GRAPH_LIFECYCLE_CHECK_S` | 5.0 | graph-builder | max interval of the Postgres lifecycle/lock check (always run right before a save) |
| `SCENE_GRAPH_START_SNAP_M` / `_GOAL_SNAP_M` | 0.5 / 1.5 | planner | how far the robot start / the goal may be moved onto free space |
| `SCENE_GRAPH_LEAF_SNAP_M` | 1.0 | planner | a waypoint gets a leaf id only when a leaf is within this |
| `SCENE_GRAPH_MAX_WAYPOINT_GAP_M` | 5.0 | planner | largest gap between waypoints |
| `SCENE_GRAPH_LOAD_TIMEOUT_S` | 2.0 | planner | one shared deadline for store open + meta + cold graph load; the lag query gets its own |
| `SCENE_GRAPH_PLAN_BUDGET_S` | 3.0 | planner | snap + route + simplify + leaf lookup (dispatcher replans with a 5 s timeout) |
| `SCENE_GRAPH_CACHE_VERSIONS` | 4 | planner | loaded graphs kept per process, keyed (map, version) |
| `SCENE_GRAPH_MAX_VERSION_LAG_S` | 120.0 | planner | graph older than the ingest by more than this -> `stale_graph`; 0 disables |
| `SCENE_GRAPH_WIDTH_MARGIN_M` | 0.0 | planner | route narrower than factsheet width + margin -> `too_narrow` (only when the check is enabled). `Route.min_width` carries the lattice error (+-0.05 m): when you enable the check set this to 0.05 (half a lattice step); the config default is still 0.0 |
| `SCENE_GRAPH_WIDTH_CHECK_ENABLED` | false | planner | gates `too_narrow`. Off by default, to be enabled deliberately: `Route.min_width` is now the narrowest `Link.width` the route crosses (fixed in the library), see "Width gate" below |
| `SCENE_GRAPH_DELETE_LOCK_TIMEOUT_S` / `_DEADLINE_S` | 30.0 / 3600.0 | **API** | delete saga: poll the worker's lock this long per attempt; a busy lock (a fold is running) does not consume `MAP_DELETE_MAX_ATTEMPTS` until it has been busy for the deadline in total |
| `SCENE_GRAPH_PREWARM_MAPS` / `_CONCURRENCY` | "" / 2 | planner | graphs loaded in the background at startup (needs the planner flag); maps with `status.scene_graph_enabled` = false are skipped (a failed registry query loads every map) |

Worker robustness settings (graph-builder, read by `scene_host`; forwarded by compose):

| Setting | Default | Meaning |
|---|---|---|
| `SCENE_GRAPH_MAX_MAPS` | 64 | maps one process owns at once (each holds a Postgres connection); more are reported once as `fold_failed` stage `max_maps` |
| `SCENE_GRAPH_SAVE_EVERY_LEAVES` / `_SAVE_EVERY_S` | 50 / 30.0 | save a snapshot after this many folded leaves / seconds (never faster than the publish interval) |
| `SCENE_GRAPH_REPLAY_MARGIN_S` / `_REPLAY_RECHECK_S` | 60.0 / 60.0 | how far below the furthest node a restart re-reads (late visible costmaps); the whole margin is re-read at most this often |
| `SCENE_GRAPH_REBUILD_MAX_AGE_S` | 900.0 | a rebuild older than this is committed at its next save even if uploads keep arriving |
| `SCENE_GRAPH_REBUILD_BATCH` | 48 | leaves per fold inside a rebuild (the canonical batching, see integration.md "Ingest ordering") |
| `SCENE_GRAPH_REBUILD_DEBOUNCE_S` | 30.0 | a delete-triggered rebuild starts only after this long without a newer request |
| `SCENE_GRAPH_LOAD_FAIL_LIMIT` | 3 | an engine load failing deterministically this many times falls into a rebuild |
| `SCENE_GRAPH_STEP_BUDGET_S` / `_SLOW_STEP_S` / `_STATS_LOG_S` | 0 (off) / 30.0 / 60.0 | stop reading nodes in one step after this long / log a step slower than this / one stats line per map this often |

Not env-configurable (`WorkerConfig`/`RunnerConfig` defaults): nodes read per step 200, PNG retry 2 s / expiry 60 s,
rebuild-request check 30 s, backoff 10 s, `commit_retries` 5, `empty_rebuild_confirmations` 2, map discovery 60 s,
terminal retry 60 s, needs_rebuild retry 300 s. Planner `GraphCache` load back-off: 5 s doubling to 300 s per map
(`BACKOFF_BASE_S` / `BACKOFF_MAX_S`), reset by the next successful load.

**Compose.** `mission_dispatch_services.yaml` forwards `SCENE_GRAPH_WORKER_ENABLED` and the worker tuning
variables to `graph-builder-service` (with `stop_grace_period: 35s`), `SCENE_GRAPH_WORKER_ENABLED` and the delete-lock
settings to `api-delegation-service`, and the planner variables to `mission-planner-service`, all as
`${VAR:-default}` with the worker and the planner off. `mission-dispatch` waits for `api-delegation-service` to be
healthy (its entrypoint migrates before it serves). graph-builder also gets
`POSTGRES_DATABASE_{PORT,NAME,USERNAME,PASSWORD}` (`POSTGRES_DATABASE_HOST` falls back to `POSTGRES_HOST`).
`mission-planner-service` needs the ARANGO_* and MINIO_* secrets (already present). Planner-only additions:
`SCENE_GRAPH_WIDTH_CHECK_ENABLED` (default false), `SCENE_GRAPH_PREWARM_MAPS` (comma list; default: every listed map
with a stored graph), `SCENE_GRAPH_PREWARM_CONCURRENCY` (2); all three are forwarded by compose.

**Postgres connections.** Each owned map holds one dedicated connection (its advisory lock), plus two per graph-builder
process (lifecycle/guard and event writes): connections needed = owned maps + 2, against the server's
`max_connections` (shared with the API pool, dispatcher and planner). With many maps cap what one process owns with
`SCENE_GRAPH_MAX_MAPS` (default 64; maps over the cap are reported once as `fold_failed` stage `max_maps` and not
ingested), and size `max_connections` for it.

**Width gate.** With `SCENE_GRAPH_WIDTH_CHECK_ENABLED=true` the planner compares `Route.min_width` (the narrowest
`Link.width` the route crosses; carries the lattice error of +-0.05 m) with the robot: `min_width >= 2 r' + margin` for
a passage the robot drives straight through (r' = half the footprint's smaller side, the `r_d` rule), and
`min_width >= 2 r' sqrt(2) + margin` where it must turn in place, margin 0.05 (`SCENE_GRAPH_WIDTH_MARGIN_M`). The planner
today applies the first form against the factsheet width; the turn-in-place bound is for judging a map by hand. The
flag is not enabled by default; check a few known corridors first (`min_width` is reported in every plan response).

## 2. Deploy order

Each step gates the next. Flags stay false until the step that names them.

1. **Images.** numpy 1.26.4 + scipy 1.11.4 in graph-builder, planner and API; each image copies `packages/scene_graph/`.
2. **Migration first.** `20261012_01_blocked_geometry` (four nullable `blocked_graph_nodes` columns, `prev_x`/`prev_y` reserved and unused). It
   runs when the API container starts (`packages.api.entrypoint`: `alembic upgrade head` under the `migrations`
   advisory lock). Start/update `api-delegation-service` first and wait for healthy. Check:
   `SELECT column_name FROM information_schema.columns WHERE table_name='blocked_graph_nodes' AND column_name IN ('robot_x','robot_y','prev_x','prev_y');` (4 rows; only `robot_x`/`robot_y` are read or written).
   Compose makes `mission-dispatch` wait for the API to be healthy. The planner cannot (the API waits for it), so keep
   the order by hand. If a service does run ahead of the migration, `packages/utils/blocked_nodes.py` falls back to
   the column-less statements (the block is stored and read without the robot's pose: the old disc rule, one warning
   per write) instead of failing.
3. **Dispatcher, planner, graph-builder, API code** with all flags still false. Behaviour unchanged.
4. **Backfill before the first ingest.** Nodes without `costmap_stored_ms` are invisible to the worker. The tool ships
   in the **graph-builder** image (it needs `scene_leaf`, ARANGO_*, MINIO_*; the API image has none of the three):
   `docker compose -f docker_compose/mission_dispatch_services.yaml exec graph-builder-service python -m tools.scene_costmap_stored_backfill`
   (dry run, prints per map `nodes/already/to_stamp/stamped/skipped/sources`, progress lines on stderr), then
   `... --apply [--map ID ...] [--batch 500]`. Idempotent, resumable. The time of a node is its own `timestamp`
   (millisecond resolution, receipt order); MinIO last-modified (1 s resolution) is only the fallback for nodes
   without one and is calibrated to the node-timestamp axis by the median offset of a sample, so MinIO is hardly
   touched. Nodes with a timestamp are stamped first, so an interrupted run keeps them. `skipped` = nodes with no
   usable time: never ingested. If a worker already ingested a map before the backfill, force a rebuild of that map
   afterwards (section 3). Check that the tool is runnable in an image:
   `docker run --rm --entrypoint python graph_builder_service:latest -c "import tools.scene_costmap_stored_backfill"`.
5. **Ingest.** Set `SCENE_GRAPH_WORKER_ENABLED=true` on graph-builder **and** API, restart both. To canary one map,
   switch every other map off first (section 3): the default is "all maps".
6. **Verify** (section 6) on at least one real map: worker started, `MAP.SCENE_GRAPH_UPDATED` events, read API
   `available: true`, no `fold_failed`, reasonable lag.
7. **Planner last.** `SCENE_GRAPH_PLANNER_ENABLED=true` on mission-planner-service. Watch `planner` /
   `planner_fallback` on plan and navigation responses and the planner log line `Chart planner not used (<reason>)`.

## 3. Per-map switch and rebuild trigger

Both live in `mapobjectv1.status` (jsonb). There is no API for either yet; use SQL.

| Want | SQL |
|---|---|
| Disable a map | `UPDATE mapobjectv1 SET status = status \|\| '{"scene_graph_enabled": false}' WHERE name = '<map>';` |
| Re-enable | `UPDATE mapobjectv1 SET status = status - 'scene_graph_enabled' WHERE name = '<map>';` |
| Force a full rebuild | `UPDATE mapobjectv1 SET status = status \|\| jsonb_build_object('scene_graph_needs_rebuild', jsonb_build_object('at', now()::text, 'reason', 'manual', 'nodes', 0)) WHERE name = '<map>';` |

* `scene_graph_enabled`: only a JSON `false` counts; absent/true follows the global flag, and `true` cannot start a map
  while `SCENE_GRAPH_WORKER_ENABLED` is off. Read in the map discovery (every 60 s): a running worker is dropped
  within about 2 minutes (two discoveries), lock released. A disabled map keeps its graph and its reads; its
  graph goes stale. Reported as `MAP.SCENE_GRAPH_DISABLED_SKIPPED {reason: "map_not_enabled"}` at most hourly per map.
* `scene_graph_needs_rebuild` `{at, reason, nodes}`: set automatically by node delete, always (no flag), once before
  the delete runs (`reason: nodes_deleting`) and again after it with a fresh `at` (`nodes_deleted`). A delete that
  fails or finds nothing therefore costs one spurious rebuild, never a lost one. The worker looks at start and then every 30 s per map. It folds every leaf again into a pending generation;
  readers and the planner keep the old version until the commit. At commit the key is removed only if its `at` is
  unchanged, so a delete that arrived mid-rebuild causes one more rebuild. `at` must be non-empty and should be
  unique per request (it is the compare-and-clear token). `reason` is free text, echoed in the rebuild events.
  A map with no saved graph (version 0) simply starts fresh and the request is cleared. A crash mid-rebuild leaves
  the key, so the restart rebuilds again.
* A params change (`SceneParams`: `robot_half_side`, `lattice`, `place_mult`, `sight_range`, or the store pipeline
  version) changes the params hash; the worker detects it at start and rebuilds the same way
  (`reason: "needs_rebuild"`).
* There is no supported standalone "drop the graph" tool; the graph is removed by the map delete saga only.

## 4. Rollout and rollback

**Roll out** per map: backfill -> enable -> verify -> (later, globally) planner flag. Keep the planner off until the
graph of every map you care about is at least at the latest upload (lag under `SCENE_GRAPH_MAX_VERSION_LAG_S`).

**Rollback, fastest first** (all are reversible; no data migration is involved):

| Level | Action | Effect |
|---|---|---|
| Planner | `SCENE_GRAPH_PLANNER_ENABLED=false`, restart planner | immediate legacy planning; responses lose `planner*` fields |
| One map's ingest | `scene_graph_enabled: false` (section 3) | worker released within ~2 min; graph stays, goes stale |
| All ingest | `SCENE_GRAPH_WORKER_ENABLED=false` on graph-builder, restart | no worker. If also set false on the API, reads answer `available: false` for every map; node delete still sets the marker; the delete saga still drops graphs it finds. **Turn the planner off first**, or it plans on a graph that ages into `stale_graph` |
| Schema | do not downgrade unless required | the migration is additive/nullable; old code ignores the columns. `downgrade` drops them (rows lose robot pose, back to the disc rule) |

A restart of graph-builder is safe at any point: the advisory lock goes with the connection, the cursor is stored with
every version, unsaved folds are re-read (fold is idempotent). `stop()` waits up to 30 s for a running fold (compose gives the container `stop_grace_period: 35s`).

## 5. Events (`fleet_events`, source `graph_builder`, no robot)

`SELECT ts, code, payload FROM fleet_events WHERE code LIKE 'MAP.SCENE_GRAPH_%' ORDER BY ts DESC LIMIT 50;`
Every payload has `map_name`. Not stored/emitted when the event DB write fails (logged only).

| Event (severity) | Payload | Meaning | Recommended alert |
|---|---|---|---|
| `MAP.SCENE_GRAPH_UPDATED` (info) | `version`, `leaves_folded`, `charts_changed` | a version was saved and published (at most one per publish interval per map). Also once after a rebuild commit with `leaves_folded: 0` | None per event. **Absence** is the signal: uploads flowing for a map but no UPDATED for > 5 min -> ingest stalled |
| `MAP.SCENE_GRAPH_FOLD_FAILED` (warning) | `stage`, `detail` (300 chars), `consecutive` | see stage table | by stage, below |
| `MAP.SCENE_GRAPH_REBUILD_STARTED` (info) | `reason` | pending generation begun (`needs_rebuild` = params hash, `nodes_deleted`, `manual`/other = request text) | none; info |
| `MAP.SCENE_GRAPH_REBUILD_COMPLETED` (info) | `reason`, `version`, `duration_s` | pending generation committed; readers switched | alert if no COMPLETED follows a STARTED within your budget (no duration measured yet; start at 1 h). **`version == 0`: ticket** (below) |
| `MAP.SCENE_GRAPH_DISABLED_SKIPPED` (info) | `reason` | map skipped (`map_not_enabled`; `worker_disabled` is reserved in the schema, never emitted: with the flag off no worker runs) | alert only for a map you did not switch off |

`fold_failed` stages (a failure in a row is reported at 1, 2, 4, 8, ... consecutive; there is no recovery event, the
next UPDATED is the recovery):

| `stage` | Cause | Worker behaviour | Alert |
|---|---|---|---|
| `fold`, `tiles` | engine error in `fold_many`, or MissingTiles not resolved by one reload | batch stays buffered, retried every 10 s | `consecutive >= 4` page; 1-2 is noise. Persisting = engine bug on that map's data |
| `save` | store save failed (MinIO/Arango), or generation commit failed (`detail` starts `commit:`) | folded state kept in memory, retried every 10 s | `consecutive >= 4` page: store down or full |
| `load` | loading the stored version into an engine failed at worker start | retried every 10 s | `consecutive >= 4` page |
| `fenced` (terminal) | writer lock lost, save refused (stale version / another writer), generation superseded, or commit failed 5 times | worker closed, lock released, retried after 60 s from the stored state | one-off: info (Postgres connection drop, another instance took the lock). Repeating (> 3 in 10 min): page, two writers or flapping DB |
| `deleting` (terminal) | registry row DELETING or missing | worker closed, retried after 60 s | ignore during a map delete; otherwise ticket (registry row missing for a live bucket) |
| `needs_rebuild` (terminal) | params hash or generation mismatch the worker could not rebuild itself | retried after 300 s | ticket. Should not occur with the production wiring (the worker rebuilds params changes itself) |
| `max_maps` | the process already owns its maximum of maps (default 64); this map is not ingested (reported once) | none | ticket: raise `SCENE_GRAPH_MAX_MAPS` (mind Postgres connections, section 1) or disable unused maps |

**Empty rebuild (`REBUILD_COMPLETED` with `version: 0`)**: a rebuild found no foldable leaf (every node deleted, or
none has a usable occupancy costmap) twice in a row and dropped the stored graph. Reads now say `available: false` and
the planner reports `no_graph`. Expected after deleting all nodes of a map; otherwise check the map's nodes /
costmaps. Alert on `version = 0` in the payload.

Only logs, no event or endpoint: skip counters (`no_occupancy_layer`, `no_pose`, `no_origin`, `no_stamp`, `bad_png`,
`size_mismatch`, `bad_resolution`, `png_missing_expired`), `default_r_d` (footprint unknown), fold timings, pending
PNGs, version lag. Grep the graph-builder log for `scene worker <map>` / `scene runner <map>`.

## 6. Verification checklist

1. graph-builder log: `scene graph worker started`. `scene graph worker not started: <ExcType>: ...` means the worker
   is **off** and the service runs without it (import, store or Postgres failure; the flag is true).
2. Within a minute of an upload: `MAP.SCENE_GRAPH_UPDATED` for the map.
3. `GET /api/v1/maps/{map}/scene-graph?bbox=x0,y0,x1,y1` (map-frame metres, snapped to a 6.4 m grid, max 4,000,000
   lattice cells, level 0-8): `available: true`, `version` rising with uploads. `/scene-graph/raster` (u16 owner
   plane, ETag) and `/scene-graph/lineage?from_version=N` (409 beyond 256 versions or when too old: reselect).
4. Planner (flag on): a plan response has `planner: "scene_graph"`, `graph_version`, `chart_keys`, `min_width`; or
   `planner: "legacy"` with `planner_fallback` (section 7).
5. Delete saga on a test map: it takes the worker's `scene_graph:<map>` lock (polls up to
   `SCENE_GRAPH_DELETE_LOCK_TIMEOUT_S` per attempt, retries while a fold holds it), drops `sg_*` and `scene_graph/`
   objects, then the rest.

## 7. Planner fallback reasons (`planner_fallback`)

Present only when the planner flag is on and the legacy planner served the request. The legacy result is always
valid; a fallback is a cost, not an outage. Each is also logged as `Chart planner not used (<reason>)`.

| Reason | Meaning | Operational reading |
|---|---|---|
| `robot_unavailable` | robot status not found | robot offline/unknown; legacy will fail the same way |
| `robot_not_placed` | robot has no pose in this map | localise the robot; legacy reports it to the caller |
| `store_unavailable` | scene graph store could not be built (retried after 30 s) | planner lacks ARANGO_*/MINIO_* env or store down |
| `store_timeout` / `store_error` | store open or `meta` timed out (> `LOAD_TIMEOUT_S`) / raised | Arango/MinIO slow or down; check their health |
| `no_graph` | map has no saved version | map not enabled, never ingested, or emptied (version 0 rebuild) |
| `load_timeout` | cold graph load took > `LOAD_TIMEOUT_S`; it continues in the background and fills the cache | only when no older version of the map is cached (first request after restart; pre-warm avoids it). A newer version is loaded in the background (single flight) while the newest cached one is served, bounded by `stale_graph`. Persistent on a quiet map = graph too large for the timeout |
| `load_error` | `store.load` raised, or the map is in load back-off after a failed load (5 s doubling to 300 s; no new load per request; an older cached version is served meanwhile) | store/snapshot problem; see worker `save` failures and planner log (`scene graph load ... failed`) |
| `stale_graph` | oldest costmap stored after the version's cursor is older than `MAX_VERSION_LAG_S` | worker behind, stalled, or map switched off while uploads continue. Unknown lag (no cursor, query error) never rejects |
| `start_no_navigable_space` / `goal_no_navigable_space` | no free cell near the point | robot pose or goal outside mapped free space |
| `start_too_far` / `goal_too_far` | nearest free cell beyond the snap limit (0.5 m / 1.5 m) | pose inaccurate or goal tapped in a wall; raise snap only with care |
| `start_crosses_blocked` / `goal_crosses_blocked` | the snap would cross a known blocked cell | robot/goal inside an ever-blocked region |
| `blocked_by_overlay` | route exists without the active blocked-node rows, not with them | an active block (expiry pending) stands in the way; legacy then reports `blocked_nodes` |
| `unreachable` | no route in the chart graph | real disconnection, or map not yet fully ingested |
| `missing_tiles` | the corridor needs tiles not loaded | snapshot incomplete/inconsistent; investigate the version |
| `too_narrow` | `min_width` < robot factsheet width + margin; **inactive unless `SCENE_GRAPH_WIDTH_CHECK_ENABLED`** (see "Width gate") | route has a passage narrower than this robot (width only, not length) |
| `waypoints_without_distinct_leaf_ids` | a waypoint has no leaf within 1.0 m, or two waypoints share one | sparse leaf coverage on the route; raise `LEAF_SNAP_M` or map more |
| `blocked_row_on_route` | a blocked row without a robot pose (old disc rule) hits the final waypoints | legacy planner reports/detours it |
| `plan_timeout` | snap + route + simplify + leaf lookup > `PLAN_BUDGET_S` (the thread keeps running) | map/route too large for the budget; see capacity |
| `error` | unexpected exception (logged with repr) | read the planner log |

## 8. Capacity and timing

* **Ingest cadence.** Reads 200 nodes per step; folds when 6 leaves wait or the oldest waited 5 s; a backlog goes 48
  leaves per fold. Saves are floored at 5 s apart per map, and also when 50 unsaved leaves / 30 s / idle. A
  publish therefore comes at most every 5 s per map, usually slower under load.
* **One thread, all maps.** The runner steps maps round-robin, one batch each. One slow fold delays every other map
  by that fold. Max `SCENE_GRAPH_MAX_MAPS` (64) maps in memory.
* **Fold cost (CIMBI prototype, per-leaf local update, `sight_range` 5 m; not the production `fold_many`).** Walled
  floors 4.2k / 12.6k / 31k cells: 0.44 / 0.73 / 0.71 s per leaf. A 48-leaf batch at that rate is about 20-35 s, above
  the 10 s budget: expect `slow fold` warnings on backlog; raising `BATCH_BUDGET_S` only silences them. Estimates,
  not measurements of this service.
* **Open-field maps are not viable yet.** 4.7k-cell open field: 3.1 s per leaf (worst 44 s); larger fields did not
  finish. Keep such maps disabled and the planner away from them until CIMBI's sight-range cap lands.
* **Backfill of an existing map** is hours at the builder's cost (big batches, resumable). Rebuild cost is the same
  full refold; there is no per-extent re-solve.
* **Planner.** Warm path must fit 3 s (5 s dispatcher timeout). A cold load is waited for 2 s; a new version loads in the
  background while the older cached one is served (2 versions per map, cache of 4 versions per process). Read API: result cache 96 MiB, 2 loaded graphs,
  8 tile views.
* **Restart replay.** The cursor is the lower of the in-memory copy and the store's; replay margin
  `SCENE_GRAPH_REPLAY_MARGIN_S` (60 s); a replay that folds nothing new no longer publishes a version; a rebuild
  restarts from zero.

## 9. Known limitations

* **No authorization** on the three scene-graph routes (nor a rate limit); they follow `/maps/{id}/graph`.
* **`r_d` rule**: half of the *smaller* footprint side; unknown footprint (-1) uses 0.25 m and is counted
  (`default_r_d`, log only). Optimistic for a long robot: a passage it cannot turn in can look navigable, and
  `too_narrow` uses the same width-only rule. Revisit before enabling the planner for a robot with length >> width.
* **Not run on production.** Worker, store, guard, saga and planner pass the e2e harness against real ArangoDB / MinIO /
  Postgres (throw-away containers, synthetic worlds). The first run on the production stack is still the verification
  of timing, volumes and the fold cost above.
* **Bucket-slug collisions.** MinIO buckets are `lower(name)` with `_` -> `-`. Two registered maps that map to one
  bucket (`a_b` / `a-b`, `My_Map` / `my-map`) cannot be told apart: the worker skips both and logs `candidate ...
  matches several registered maps`. Rename one. No event is emitted for this.
* **Per-extent re-solve not built.** Node delete (and any params change) is a full rebuild of the map; during it the
  old version is served, and only after it do deletions disappear from the planner. Realign triggers no rebuild.
* **Stale leaf stamps.** A leaf folded under a provisional receipt-time stamp can be folded again under an earlier
  corrected one; it can look "too new" in cells it won until newer data arrives.
* **No metrics endpoint**; stats are in process memory. The cursor store is process-local (the durable cursor is in
  `sg_meta`).
* **Per-map switch has no API**, only SQL; no `scene_graph_enabled` event on a service-level disable.
* **Planner flag is global**, not per map; maps without a graph just fall back (`no_graph`).

## 10. Troubleshooting

| Symptom | Check | Action |
|---|---|---|
| Flag true but no UPDATED events, no log line | graph-builder env actually has `SCENE_GRAPH_WORKER_ENABLED` (compose forwards it; check `.env`) | set it in `.env`, recreate container |
| `scene graph worker not started: ...` | exception text in the log (numpy/scipy import, store, Postgres) | fix the cause, restart graph-builder; the service stays up without the worker |
| Map never ingested, no events | map registered and live in `mapobjectv1`; bucket exists; not `scene_graph_enabled: false`; bucket-slug collision log | fix registry/switch; rename colliding map |
| Nodes uploaded before the feature never appear | backfill report: `to_stamp` / `skipped` | run backfill `--apply`, then force a rebuild (section 3) |
| UPDATED stops, uploads continue | log for `scene runner <map>`; fold_failed events; `NOT_OWNER` (another replica holds the lock) | fix per stage; if another replica owns it, that replica ingests |
| `fold_failed` stage `fold`/`tiles` repeating | `detail`; worker log traceback | report to CIMBI with the map; disable the map meanwhile |
| `fold_failed` stage `save` repeating | MinIO/Arango health and free space | restore store; worker retries by itself |
| `fold_failed` stage `fenced` repeating | two graph-builder instances, Postgres connection drops | stop the extra instance; check Postgres `idle` connection kills |
| Deleted nodes still visible in the chart graph | `scene_graph_needs_rebuild` on the map status; API worker flag | wait for rebuild; check REBUILD_STARTED/COMPLETED; set manually if absent |
| REBUILD_STARTED without COMPLETED | worker log `generation commit failed (n/5)`, fold failures | store problem; after 5 commit failures it ends `fenced` and restarts the rebuild |
| Read API `available: false` | API worker flag; map has a saved version; worker state | enable flag on the API; ingest the map |
| Read API 422 "bbox is N cells" / "N charts" | request size | smaller bbox or higher level |
| Read API 409 "tiles changed" / lineage 409 | version moved during the read / span > 256 / too old | retry; for lineage reselect from current charts |
| Planner always `load_timeout` | no cached version yet: map size, MinIO latency, pre-warm failed (planner log) | wait for the background load, set `SCENE_GRAPH_PREWARM_MAPS`, or raise `LOAD_TIMEOUT_S` within the 5 s dispatcher budget |
| Planner always `stale_graph` | lag: worker stalled, behind, or map disabled while uploads continue | fix ingest, re-enable the map, or (last resort) `MAX_VERSION_LAG_S` higher / 0 |
| Planner `store_unavailable` | planner env has ARANGO_*/MINIO_*; retry is every 30 s | fix env/network, restart planner |
| Route clips a known obstacle / robot refuses leg | active `blocked-nodes` rows (`GET /api/v1/maps/{id}/blocked-nodes?with_chart_keys=true`; run legs: `GET /api/v1/runs/{id}/legs?with_chart_keys=true`); migration applied | clear/expire row; check migration |
| Blocked rows missing pose (disc rule only) | columns NULL: rows written before the migration or without localization | none; expire naturally |
| Map delete stuck at `scene_graph` | `scene_graph: lock ...` in the map's `delete_error`, `delete_attempts` not rising; the worker holds the lock during a long fold | retried with backoff without consuming attempts until `SCENE_GRAPH_DELETE_LOCK_DEADLINE_S`; if persistent, disable the map and wait for release (~2 min) |
| Open-field map extremely slow / `slow fold` warnings | fold times in the log | disable the map; keep the planner off for it |
