# Scene graph v2: operator runbook

Scope: deploy, switch on, watch and roll back the scene graph (ingest worker in graph-builder, read API, planner
route). Design and contracts: `integration.md`. Everything below is checked against the source on
`feat/scene-graph-v2`. The worker, store, read API, saga and planner are exercised end to end against real ArangoDB / MinIO / Postgres by the e2e harness (`tools/scene_graph_e2e/run.sh`); no production stack has run them yet (see Known limitations).

Components: **graph-builder** runs the ingest worker (one runner thread schedules every map; the steps run on a
pool of `SCENE_GRAPH_WORKER_THREADS` threads, one step per map at a time); **api-delegation-service** serves
`/api/v1/maps/{id}/scene-graph*`, owns the Postgres migrations and the map-delete saga; **mission-planner-service**
plans on the graph; **mission-dispatch** writes blocked-node rows (needs the migration, falls back without it).
State lives in MinIO (`scene_graph/` in the map's bucket, source of truth) and ArangoDB (`sg_*_{map}`, rebuildable
index). Sections 11 (worker lifecycle and `/health`) and 12 (read API) are the reference for what the worker and
the reads do; sections 1-10 are the procedure.

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
| `SCENE_GRAPH_TOTAL_BUDGET_S` | 3.5 | planner | ONE monotonic deadline per chart-route request over every stage (robot, store, graph load, lag, blocked rows, plan). The dispatcher replans with a 5 s timeout and the legacy planner needs the rest after a fallback, so keep this well under 5. Each wait is `min(its own limit, what is left)`; under 0.3 s left the request falls back at once (`budget_exhausted`) |
| `SCENE_GRAPH_LOAD_TIMEOUT_S` | 2.0 | planner | longest one stage waits for store open + meta + a cold graph load (the load continues in the background); also the wait for the lag query |
| `SCENE_GRAPH_PLAN_BUDGET_S` | 3.0 | planner | snap + route + simplify + leaf lookup (a wait on the plan thread; the thread keeps running). Also bounded by the total budget |
| `SCENE_GRAPH_PLAN_MAX_INFLIGHT` | 4 | planner | plans running or hung on the 2-thread plan pool; beyond it a request falls back `planner_busy` |
| `SCENE_GRAPH_CACHE_VERSIONS` | 4 | planner | loaded graphs kept per process in total (LRU over maps) |
| `SCENE_GRAPH_CACHE_VERSIONS_PER_MAP` | 2 | planner | of one map: the one served and the one being refreshed |
| `SCENE_GRAPH_CACHE_MAX_BYTES` | 0 (off) | planner | byte budget of the cache over the graphs' reported `nbytes`; the newest graph is always kept. Size it from the container memory limit and the `cache.bytes` the planner reports (section 7); there is no per-cell figure measured on the current engine, see section 8 |
| `SCENE_GRAPH_PLANNER_MODE` | (removed) | planner | no longer a switch: the planner always uses the lazy tile planner (manifest only, owner tiles of each request's corridor; see "Planner modes" below). `full_load` existed until its removal on 2026-10-13. If set to anything but `lazy` the planner only logs a warning; remove it from the `.env` |
| `SCENE_GRAPH_LAZY_MAX_TILES` | 400 | planner | most owner tiles one request may read; more falls back `corridor_too_large` |
| `SCENE_GRAPH_LAZY_TILE_CACHE` | 512 | planner | owner tiles kept per loaded graph (about 35 KB each by the config comment; not measured here) |
| `SCENE_GRAPH_META_TTL_S` | 0.75 | planner | a `store.meta` answer is reused this long per map and shared by concurrent requests (one query in flight) |
| `SCENE_GRAPH_MAX_VERSION_LAG_S` | 120.0 | planner | graph older than the ingest by more than this -> `stale_graph`; also how long a newer version may have been known (in the store's meta) while an older cached one is served; 0 disables both |
| `SCENE_GRAPH_WIDTH_MARGIN_M` | 0.05 | planner | added to the robot's needed passage width (the lattice error of `Route.min_width`, lattice / 2). Only used when the check is enabled |
| `SCENE_GRAPH_WIDTH_CHECK_ENABLED` | false | planner | gates `too_narrow`. Off by default; enable only after the validation in "Width gate" below |
| `SCENE_GRAPH_SAFETY_KAPPA` | 1.0 | planner | clearance pricing of the chart route and of the planned / gap-fill waypoints (`route_safety.md`): the cell cost multiplier added at the inscribed floor, margin = the planning robot's circumscribed radius (factsheet width and length, `r * sqrt(2)` when unknown). Range 0 - 10 (out of range: reset to 1.0 with a warning); the range is the measured plateau, not a bound of the theory; 0 = the shortest route. Pricing is also off for a margin above 1 m or within 0.07 m of the map's inscribed radius. Read from the stored `v` tiles, no extra storage; independent of the width gate |
| `SCENE_GRAPH_INFLATION_RADIUS_M` / `_INFLATION_SCALING` / `_GLOBAL_INSCRIBED_M` | empty (off) | planner | the exponential price profile (`inflation_profile_design.md`, experimental): radius (m, <= 1.0) and scaling both set and valid = price `exp(-scaling (g - origin))` below the radius, kappa 0.5 unless `SCENE_GRAPH_SAFETY_KAPPA` is set explicitly; origin = `_GLOBAL_INSCRIBED_M`, else the footprint's inscribed radius, else the map's r. Empty = the linear hinge above. Off in production |
| `SCENE_GRAPH_NEAR_RANGE_M` / `_NEAR_FOV_DEG` / `_NEAR_MIN_CLEARANCE_M` | empty (off) | graph-builder | close evidence (`close_evidence_and_turn_cost_design.md` part A): a cell the costmap reported lethal stays blocked until a free report from a capture pose within this range (m) of it (the robot's `support.commit_radius_m`, 1.5 m, is the reference); the optional cone (degrees, around the base yaw) also has to contain the cell. Empty = the latest report wins, as before, and the stored format is unchanged. **Setting it changes the params hash, so every map is rebuilt in a new generation** (section 3), and the stores written carry the pipeline `morse/2+near1` and one more tile plane `fn`. `_NEAR_MIN_CLEARANCE_M` (needs `_NEAR_RANGE_M`; 0.25 is the reference, set on a branch not yet in production) is the away-from-walls filter: a held region stays held only when a cell of its connected component is farther than this (m) from every other blocked or unknown cell, so wall rims read as without the rule; it adds the tile plane `hold` and the pipeline `morse/2+near2` (the rule alone stays `+near1`, byte-identical). Planner readers need no setting: they take the rule and the filter from the stored tiles |
| `SCENE_GRAPH_TURN_COST_ENABLED` / `SCENE_GRAPH_TURN_LAMBDA_M_PER_RAD` | false / empty | planner | turn cost of the chart route and of the planned / gap-fill waypoints' routes (`close_evidence_and_turn_cost_design.md` B.1, B.6): every change of heading of the lattice route adds lambda * \|dtheta\| to its cost; the search stays exact. lambda (m per rad) is per robot: factsheet `speed_max / angular_speed_max`, clamped to [0, 5]; `_LAMBDA_M_PER_RAD` is used only for a robot whose factsheet lacks one of the two (else that robot gets no turn cost). Off (the default) = no turn cost for any robot, the routes are bit-identical to before. The string pull (waypoints) has no turn term. Each plan's `pricing.turn` says the lambda and its source (`factsheet` / `config` / `off`) and `/stats` `scene_graph.turn_sources` counts plans by source |
| `SCENE_GRAPH_NEAR_RANGE_M` / `_NEAR_FOV_DEG` | empty (off) | graph-builder | close evidence (`close_evidence_and_turn_cost_design.md` part A): a cell the costmap reported lethal stays blocked until a free report from a capture pose within this range (m) of it (the robot's `support.commit_radius_m`, 1.5 m, is the reference); the optional cone (degrees, around the base yaw) also has to contain the cell. Empty = the latest report wins, as before, and the stored format is unchanged. **Setting it changes the params hash, so every map is rebuilt in a new generation** (section 3), and the stores written carry the pipeline `morse/2+near1` and one more tile plane `fn`. Planner readers need no setting: they take the rule from the stored tiles |
| `SCENE_GRAPH_WAYPOINT_BUDGET_M` | empty (off) | planner | waypoint budget (`close_evidence_and_turn_cost_design.md` C.2): the string pull is replaced by the exact fewest-waypoints path whose total priced-cost excess over the lattice path is at most this many metres (final cost <= lattice cost + budget; chords visible and within `SCENE_GRAPH_MAX_WAYPOINT_GAP_M`; start, goal and operator waypoints never move). Empty or <= 0 = off, the greedy pull unchanged; not finite or above 1.0 = off with a warning. Reference 0.35. Counter `scene_graph.waypoints_removed` in `/stats`, `waypoint_budget` ({`waypoint_budget_m`, `waypoints_removed`}) in the plan and a `Waypoint budget plan` log line. Independent of the turn cost |
| `SCENE_GRAPH_DELETE_LOCK_TIMEOUT_S` / `_DEADLINE_S` | 30.0 / 3600.0 | **API** | delete saga: poll the worker's lock this long per attempt; a busy lock (a fold is running) does not consume `MAP_DELETE_MAX_ATTEMPTS` until it has been busy for the deadline in total |
| `SCENE_GRAPH_PREWARM_MAPS` / `_CONCURRENCY` | "" / 2 | planner | graphs loaded in the background at startup (needs the planner flag); empty = every map the graph database lists with a stored graph, limited to `CACHE_VERSIONS` maps (the rest are named in a warning); maps with `status.scene_graph_enabled` = false are skipped (a failed registry query loads every map) |

**Planner modes.** There is one mode: the lazy tile planner (`full_load` existed until its removal on 2026-10-13; production has run lazy
since 2026-10-13 19:00 UTC). Rollback for a planner problem: `SCENE_GRAPH_PLANNER_ENABLED=false` (legacy planner) or deploy the previous
image (`scripts/deploy.sh rollback mission-planner-service`, see "Deploying" in section 2). Definition, fallbacks (`corridor_too_large`,
`tile_unavailable`) and wiring: `integration.md`, "Planner modes".

Worker robustness settings (graph-builder, read by `scene_host`; forwarded by compose):

| Setting | Default | Meaning |
|---|---|---|
| `SCENE_GRAPH_MAX_MAPS` | 64 | maps one process owns at once (each holds a Postgres connection); more are reported once as `fold_failed` stage `max_maps` |
| `SCENE_GRAPH_SAVE_EVERY_LEAVES` / `_SAVE_EVERY_S` | 50 / 30.0 | save a snapshot after this many folded leaves / seconds (never faster than the publish interval) |
| `SCENE_GRAPH_REPLAY_MARGIN_S` / `_REPLAY_RECHECK_S` | 60.0 / 60.0 | how far below the furthest node a restart re-reads (late visible costmaps); the whole margin is re-read at most this often |
| `SCENE_GRAPH_RECEIPT_WINDOW_S` | 300.0 | `costmap_stored_ms` is a leaf's receipt time only within this many seconds of the node's own time (`timestamp`, else `capture_time`); farther away it is a bulk write (a backfilled `occupancy_depth`), and the node's own time is used. `<= 0` always trusts it; NaN / inf fall back to 300 |
| `SCENE_GRAPH_SKIP_FOLDED` | true | restart replay skips leaves the stored state already holds (key, stamp, session, pose, `r_d`, view; stored at or before the state's cursor): skip reason `already_folded`. `false` folds the replay margin again |
| `SCENE_GRAPH_REBUILD_MAX_AGE_S` | 900.0 | a rebuild older than this is committed at its next save even if uploads keep arriving |
| `SCENE_GRAPH_REBUILD_BATCH` | 48 | leaves per fold inside a rebuild (the canonical batching, see integration.md "Ingest ordering") |
| `SCENE_GRAPH_REBUILD_DEBOUNCE_S` | 30.0 | a delete-triggered rebuild starts only after this long without a newer request |
| `SCENE_GRAPH_LOAD_FAIL_LIMIT` | 3 | an engine load failing deterministically this many times falls into a rebuild |
| `SCENE_GRAPH_STEP_BUDGET_S` / `_SLOW_STEP_S` / `_STATS_LOG_S` | 0 (off) / 30.0 / 60.0 | stop reading nodes in one step after this long / log a step slower than this / one stats line per map this often |
| `SCENE_GRAPH_MEMORY_RETRY_LIMIT` | 3 | the same batch running out of memory this many times in a row is bisected; the leaf that still fails alone is quarantined |

Worker scale (graph-builder, one process, many maps; sizing in section 8):

| Setting | Default | Meaning |
|---|---|---|
| `SCENE_GRAPH_WORKER_THREADS` | min(8, CPU count) in code; **4 in compose** (compose cannot compute the CPU count, and an empty value does not parse) | size of the step pool: how many maps fold at once. A cold engine load never takes the last thread, so loaded maps keep running while a big map loads |
| `SCENE_GRAPH_LAZY_LOAD` | true | a map's engine is loaded by its first fold, not at worker start: an idle map holds no engine |
| `SCENE_GRAPH_MAX_RESIDENT_ENGINES` | 32 | engines kept in memory at once (0 = no cap) |
| `SCENE_GRAPH_MAX_RESIDENT_BYTES` | 0 (off) | byte cap over the engines' `nbytes`. **Set this to about 60-70 % of the container memory limit** |
| `SCENE_GRAPH_EVICT_IDLE_S` | 60.0 | an engine is evicted only after its map was quiet this long (saved first, reloaded by the next leaves) |
| `SCENE_GRAPH_PREFETCH_THREADS` / `_QUEUE` | 4 / 64 | PNG fetch + decode pool shared by all maps (0 threads = off) and the most PNGs held prefetched at once |

Not env-configurable (`WorkerConfig`/`RunnerConfig` defaults): nodes read per step 200, PNG retry 2 s / expiry = the
replay margin (60 s, wall clock), poll margin 5 s, PNG re-asks per step 20, rebuild-request check 30 s, backoff 10 s,
`commit_retries` 5, `empty_rebuild_confirmations` 2, `UPDATED` event at most every 60 s per map, `engine.nbytes`
refresh 10 s, map discovery 60 s, lock held elsewhere (`not_owner`) retry 30 s, terminal retry 60 s, needs_rebuild
retry 300 s. Planner `GraphCache`: load back-off 5 s doubling to 300 s per map (`BACKOFF_BASE_S` / `BACKOFF_MAX_S`,
reset by the next successful load), store open retried after 30 s, loads on a 2-thread pool serialised on one lock,
store/meta/lag I/O on a 4-thread pool (cap 16 queued or running, then `store_busy`), plan on a 2-thread pool.

**Compose.** `mission_dispatch_services.yaml` forwards every `SCENE_GRAPH_*` variable of `packages/config.py` to the
service that reads it, all as `${VAR:-default}` with the code's default and the worker and the planner off:
`SCENE_GRAPH_WORKER_ENABLED`, the worker tuning and the worker scale variables to `graph-builder-service` (with
`stop_grace_period: 35s`), `SCENE_GRAPH_WORKER_ENABLED` and the delete-lock settings to `api-delegation-service`,
and the planner variables to `mission-planner-service`. A variable added to `config.py` must be added to compose
and `.env.example` too (check: `grep -o 'SCENE_GRAPH_[A-Z_]*' packages/config.py | sort -u` against both). `mission-dispatch` waits for `api-delegation-service` to be
healthy (its entrypoint migrates before it serves). graph-builder also gets
`POSTGRES_DATABASE_{PORT,NAME,USERNAME,PASSWORD}` (`POSTGRES_DATABASE_HOST` falls back to `POSTGRES_HOST`).
`mission-planner-service` needs the ARANGO_* and MINIO_* secrets (already present). Planner-only additions:
`SCENE_GRAPH_WIDTH_CHECK_ENABLED` (default false), `SCENE_GRAPH_PREWARM_MAPS` (comma list; default: every listed map
with a stored graph), `SCENE_GRAPH_PREWARM_CONCURRENCY` (2),
`SCENE_GRAPH_LAZY_MAX_TILES` (400), `SCENE_GRAPH_LAZY_TILE_CACHE` (512).

**Postgres connections.** Each owned map holds one dedicated connection (its advisory lock). On top of that a step
that does not hold its lock yet, and the discovery, open short-lived connections: with the step pool up to
`SCENE_GRAPH_WORKER_THREADS` at once, plus a few fixed ones (footprint lookups, event writes). Budget
`max_connections` for maps + threads + a few, per graph-builder process, shared with the API pool, dispatcher and
planner. With many maps cap what one process owns with `SCENE_GRAPH_MAX_MAPS` (default 64; maps over the cap are
reported once as `fold_failed` stage `max_maps` and not ingested; a map whose lock another replica holds does not
count against the cap).

**MinIO connections.** minio-py's default HTTP pool keeps 10 connections per host and the code does not enlarge it.
Step threads, prefetch threads (`PREFETCH_THREADS`) and the store's own I/O share it per process: with
`WORKER_THREADS + PREFETCH_THREADS` well above 10 expect urllib3 "Connection pool is full, discarding connection"
warnings (harmless but slower, a connection is opened per overflow request). Keep the sum near 10 or raise the pool
in a follow-up.

**Width gate.** With `SCENE_GRAPH_WIDTH_CHECK_ENABLED=true` the planner compares `Route.min_width` (the narrowest
`Link.width` the route crosses; it carries the lattice error of +-0.05 m) with what the planning robot needs
(factsheet width and length; a missing length is taken as the width):

* a passage the robot drives straight through: `min_width >= width + margin` (= `2 r' + 0.05` for r' half the
  width);
* a route that must turn in place: `min_width >= hypot(length, width) + margin` (the circumscribed circle).

`margin` is `SCENE_GRAPH_WIDTH_MARGIN_M` (default 0.05, the lattice error). The aligned form is checked on the raw
route first (no need to simplify a route that is too narrow even straight); the turn form on the simplified
waypoints. A turn is any heading change above **30 degrees** between two consecutive waypoints
(`TURN_IN_PLACE_RAD`); the start heading is not known to the planner and is not considered. The 30 degrees is an
assumption about the controller (it may rotate on the spot at a waypoint above it and steers through below it) and
**has not been validated against the real controller**: do that before enabling the flag. Procedure: with the flag
off every plan response already reports `min_width`; compare it with a few corridors you know the robot passes
(and ones it refuses), check a route with a sharp corner in a narrow passage, then enable on one planner instance.
`too_narrow` falls back to the legacy planner, so a wrong threshold costs planner coverage, not a stuck robot.

## 2. Deploy order

Each step gates the next. Flags stay false until the step that names them.

1. **Images.** numpy 1.26.4 + scipy 1.11.4 in graph-builder, planner and API; each image copies `packages/scene_graph/`.
2. **Migration first.** `20261012_01_blocked_geometry` (four nullable `blocked_graph_nodes` columns; `prev_x`/`prev_y` are reserved and unused, nothing writes or reads them). Its revision is parented on
   `20261013_01_track_map_frame`, so `alembic upgrade head` applies that one first and then this one (single head).
   As of 2026-10-13 (CIMBI) `20261012_01` is not applied in the live DB. It
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
   (dry run, prints per map `nodes/already/to_stamp/stamped/skipped/far/sources`, progress lines on stderr), then
   `... --apply [--map ID ...] [--batch 500]`. Idempotent, resumable. The time of a node is its own `timestamp`
   (millisecond resolution, receipt order); MinIO last-modified (1 s resolution) is only the fallback for nodes
   without one and is calibrated to the node-timestamp axis by the median offset of a sample, so MinIO is hardly
   touched. A node already stamped is left alone even when its stamp is far from its own time (more than
   `SCENE_GRAPH_RECEIPT_WINDOW_S`: a layer written long after capture); the report counts those as `far`
   (the worker uses the node's own time as their receipt, so they need no fix). Nodes with a timestamp are stamped first, so an interrupted run keeps them. `skipped` = nodes with no
   usable time: never ingested. If a worker already ingested a map before the backfill, force a rebuild of that map
   afterwards (section 3). Check that the tool is runnable in an image:
   `docker run --rm --entrypoint python graph_builder_service:latest -c "import tools.scene_costmap_stored_backfill"`.
5. **Ingest.** Set `SCENE_GRAPH_WORKER_ENABLED=true` on graph-builder **and** API, restart both. To canary one map,
   switch every other map off first (section 3): the default is "all maps".
   After the first generation (and after any rebuild) run the read-only gate in the **graph-builder** image:
   `docker compose -f docker_compose/mission_dispatch_services.yaml exec graph-builder-service python -m tools.scene_graph_check_generation --all`
   (`--map ID` for one map, `--expect expected.json` with `{"<map id>": {"charts": N, "links": M}}` to pin the base-level
   counts). It reports format, pipeline, params hash, invariants, charts / links / levels / widths and one route; exit
   0 = every map passed (FORMAT 4, pipeline `morse/2`, invariants hold), 1 = a failure.
6. **Verify** (section 6) on at least one real map: worker started, `MAP.SCENE_GRAPH_UPDATED` events, read API
   `available: true`, no `fold_failed`, reasonable lag.
7. **Planner last.** `SCENE_GRAPH_PLANNER_ENABLED` stays **false by default** (code, compose and `.env.example`) and is
   switched on deliberately, after step 6 on the maps that matter and with their lag under
   `SCENE_GRAPH_MAX_VERSION_LAG_S`:
   1. Optional: list the maps in `SCENE_GRAPH_PREWARM_MAPS` so the first request after the restart does not meet a
      cold load (a cold full load can exceed `LOAD_TIMEOUT_S` on a large map: 12.6 s at 150 x 150 m on the synthetic Morse bench, `planner_upgrade.md` section 0; measure yours with the check tool; the lazy planner avoids the cold load).
   2. Leave `SCENE_GRAPH_WIDTH_CHECK_ENABLED=false` (section 1, "Width gate").
   3. Set `SCENE_GRAPH_PLANNER_ENABLED=true` on mission-planner-service and recreate it (all settings are read at
      start).
   4. Watch `planner` / `planner_fallback` on plan and navigation responses, the planner log line
      `Chart planner not used (<reason>)` and `get_stats()["scene_graph"]` (section 7): `outcomes` should be
      dominated by `served`; anything else is a fallback to the legacy planner and costs coverage only.
   5. Rollback is section 4, first row (flag false, restart): immediate.

### Deploying

`scripts/deploy.sh`:

```
scripts/deploy.sh [--ref REF] [--no-pull] [--no-verify] [--skip-test-gate] [--dry-run] SERVICE...
scripts/deploy.sh rollback SERVICE [TAG]
```

For each service it tags the running image `pre-<time>-<sha>`, builds, recreates, waits until healthy and rolls back
automatically on failure. After a deploy it runs the checker for `graph-builder-service`, and for
`mission-planner-service` it reads `/stats` and requires `build_sha` to equal the deployed commit (skip the checks with
`--no-verify`). `rollback` restores the named tag (default: the latest `pre-*` tag). Log: `~/.cache/satina-deploy.log`.
It does not edit `.env`.

Rules the script enforces (2026-10-14):

* **Deploy what the remote has.** The default ref is `origin/main` (the checkout is fast-forwarded to it); `--no-pull`
  deploys the checkout as it is and is for a rehearsal or an emergency, not for a release. Push first.
* **Test gate.** `scripts/run_unit_tests_pinned.sh` with no arguments writes `~/.cache/satina-tests/<short sha>` when the
  full suite passes on a clean tracked tree. A deploy of a sha without that record is refused; `--skip-test-gate`
  overrides and is logged.
* **Build identity.** The script exports `GIT_SHA`; compose passes it to the planner as `BUILD_SHA`, `/stats` shows
  `build_sha`, and the post-deploy check fails if the running image does not report the deployed sha.
* **Retention.** After a healthy deploy the newest `KEEP_ROLLBACK` (default 3) `pre-*` tags of that service's image are
  kept; older tags are untagged, never the image a container runs.

Not covered: a functional plan smoke test (a canned plan on a known pair) and image labels for services other than the
planner; both are recorded as open in `docs/scene_graph/pipeline_state_2026-10-14.md`.

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
* A params change (`SceneParams`: `robot_half_side`, `lattice`, `chart_radius`, `near_range_m`, `near_fov_deg`, `near_min_clearance_m`, or the store pipeline
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
every version, unsaved folds are re-read (fold is idempotent). A restart of a caught-up worker folds nothing and saves nothing:
the replay margin is re-read, but every leaf the stored state holds is skipped as `already_folded` (with
`SKIP_FOLDED=true`; the stored leaves are read from the manifest segments, no engine load). `stop()` waits up to 30 s for a running fold (compose gives the container `stop_grace_period: 35s`).

## 5. Events (`fleet_events`, source `graph_builder`, no robot)

`SELECT ts, code, payload FROM fleet_events WHERE code LIKE 'MAP.SCENE_GRAPH_%' ORDER BY ts DESC LIMIT 50;`
Every payload has `map_name`. Not stored/emitted when the event DB write fails (logged only).

| Event (severity) | Payload | Meaning | Recommended alert |
|---|---|---|---|
| `MAP.SCENE_GRAPH_UPDATED` (info) | `version`, `leaves_folded`, `charts_changed`, `skipped` | a version was saved and published. The event is **trailing-edge throttled** to one per 60 s per map (worker config `updated_event_interval_s`, not an env var): a save inside the interval is held (latest version, charts and leaves summed) and emitted when the interval is over, so the last save is never lost; the websocket publish is not throttled. A save that only moved the cursor (the store kept the version) announces nothing. The payload also carries `skipped` (cumulative skip counters of the worker by reason, omitted when all are zero) Also once after a rebuild commit with `leaves_folded: 0` | None per event. **Absence** is the signal: uploads flowing for a map but no UPDATED for > 5 min -> ingest stalled |
| `MAP.SCENE_GRAPH_FOLD_FAILED` (warning) | `stage`, `detail` (300 chars), `consecutive` | see stage table | by stage, below |
| `MAP.SCENE_GRAPH_REBUILD_STARTED` (info) | `reason`, `resumed` (only when true) | pending generation begun (`needs_rebuild` = params hash, `nodes_deleted`, `engine_faulted`, `manual`/other = request text); `resumed: true` = an interrupted pending generation was continued from its cursor | none; info |
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
| `poison_leaf` | the engine rejected one leaf in validation (`LeafError`, nothing mutated); `detail` = `<node key>: <reason>` | that leaf is quarantined at once (counted as skip `fold_error`, the cursor moves past it) and the rest of the batch is folded; there is no two-failures rule. Reported per leaf, `consecutive` is always 1 | ticket per leaf: the node's costmap is unfoldable; the graph lacks that one leaf. Many in a row = bad data source |
| `engine_faulted` | `fold_many` raised after the engine began to mutate (`EngineFaulted`, or any non-`LeafError` exception): its state is undefined; `consecutive` counts faults in a row without a good fold | the engine is dropped, reloaded from the store (live version, or the saved pending generation of a rebuild) and the leaves since the durable cursor are read again. `LOAD_FAIL_LIMIT` in a row fall into a rebuild (reason `engine_faulted`); a fault inside that rebuild ends `needs_rebuild` | `consecutive >= 2` ticket (engine bug on that map's data), reaching a `needs_rebuild` page |

**Empty rebuild (`REBUILD_COMPLETED` with `version: 0`)**: a rebuild found no foldable leaf (every node deleted, or
none has a usable occupancy costmap) twice in a row and dropped the stored graph. Reads now say `available: false` and
the planner reports `no_graph`. Expected after deleting all nodes of a map; otherwise check the map's nodes /
costmaps. Alert on `version = 0` in the payload.

Fold timings are log-only. Skip counters (`no_occupancy_layer`, `no_pose`,
`no_origin`, `no_stamp`, `bad_png`, `size_mismatch`, `bad_resolution`, `png_missing_expired`, `fold_error`,
`already_folded`), pending PNGs and version lag, and the counters `already_folded`, `default_r_d`, `views_built`,
`views_missing`, `view_failures` are per map in graph-builder `/health` (section 11) and in the periodic stats log.
The first skip of each reason (not `already_folded`) is logged with the node key; the rest are only counted.
`already_folded` is not an error: the stored state already holds that leaf (restart replay).
`default_r_d` counts leaves folded with the default footprint (0.25 m): the node's robot has no `robotobjectv1`
factsheet row yet (or `length` / `width` is -1), or the node has no `robot_name`. Production check:
`SELECT name, status->'factsheet'->>'length', status->'factsheet'->>'width', status->'factsheet'->>'height' FROM robotobjectv1 WHERE name IN ('formidable-peacock','masked-frigatebird');`
(the worker asks again after `footprint_retry_s`). Views: `views_built` leaves have a camera view for `cover()`,
`views_missing` have no depth record (robot-costmap maps: expected), `view_failures` have a depth record with an
unusable camera block (leaf still folded, without a view; first one logged). Grep the graph-builder log for
`scene worker <map>` / `scene runner <map>`.

## 6. Verification checklist

1. graph-builder log: `scene graph worker started`. `scene graph worker not started: <ExcType>: ...` means the worker
   is **off** and the service runs without it (import, store or Postgres failure; the flag is true).
2. Within a minute of an upload: `MAP.SCENE_GRAPH_UPDATED` for the map (events are throttled to one per 60 s per map;
   the first one is immediate; `/health` `maps.<map>.version` rises on every publish).
3. `GET /api/v1/maps/{map}/scene-graph?bbox=x0,y0,x1,y1` (map-frame metres, snapped outward to a 6.4 m grid, at most
   4,000,000 lattice cells = 31 x 31 snap tiles = 198.4 m a side at 0.1 m, 422 above; level 0-8): `available: true`,
   `version` rising with uploads, `epoch` an integer (also header `X-Scene-Graph-Epoch`). `/scene-graph/raster` (u16
   owner plane, ETag) and `/scene-graph/lineage?from_version=N` (409 beyond 256 versions or when too old: reselect).
   Full contract: section 12.
4. Planner (flag on): a plan response has `planner: "scene_graph"`, `graph_version`, `chart_keys`, `min_width`; or
   `planner: "legacy"` with `planner_fallback` (section 7).
5. Delete saga on a test map: it takes the worker's `scene_graph:<map>` lock (polls up to
   `SCENE_GRAPH_DELETE_LOCK_TIMEOUT_S` per attempt, retries while a fold holds it), drops `sg_*` and `scene_graph/`
   objects, then the rest.

## 6a. Post-generation check and islands (read-only)

`python -m tools.scene_graph_check_generation --all --expect tools/scene_graph_expected.json` (in the graph-builder container;
the expectations file ships in its image). Per map it reports format, pipeline, params hash, invariants, charts, links, levels,
link widths, and one route between the two most distant charts of the LARGEST link component.

* **Islands.** Charts outside the largest link component are free cells that no route can reach: the robot body does not squeeze
  through a corner, so routing is 4-connected while the free space may touch itself only diagonally. They are kept in the graph
  and marked: the report's `islands` field lists `{count, cells, charts[{key, cells, anchor}]}`. `--expect` pins the count per
  map. A change of the pinned count is a failure to investigate, not a number to update silently. Pinned values (2026-10-13):
  office8 0, office01 3, Officemap 1, office03 1, office04 4, office5 0, office6 0, office7 0.
* **Route check.** Runs only between reachable charts; a failure there means the largest component itself cannot route, which
  is a defect, not an island.

## 7. Planner fallback reasons (`planner_fallback`)

Present only when the planner flag is on and the legacy planner served the request. The legacy result is always
valid; a fallback is a cost, not an outage. Each is also logged as `Chart planner not used (<reason>)` and counted in
the planner's `get_stats()["scene_graph"]`: `outcomes` (`served` or a reason -> count since start), `cache`
(`graphs`, `bytes`, `loading`, `load_inflight`, `io_inflight`, `backed_off_maps`) and `plan_inflight`.

Budget: one deadline of `SCENE_GRAPH_TOTAL_BUDGET_S` (3.5 s) covers every stage of the attempt, inside the
dispatcher's 5 s replan timeout; the rest is the legacy planner's. A stage that would wait past the deadline reports
`budget_exhausted` instead of its own timeout reason.

| Reason | Meaning | Operational reading |
|---|---|---|
| `budget_exhausted` | the request's total budget (3.5 s) ran out in some stage, or under 0.3 s was left at a stage start | slow store/DB/robot lookup or a heavy plan. Persistent: look at which stage is slow (planner log, `outcomes`), not at this reason |
| `planner_busy` | `SCENE_GRAPH_PLAN_MAX_INFLIGHT` plans are running or hung on the plan pool (a timed-out plan keeps its slot until it ends) | plans slower than arrivals, or a hung engine call; legacy serves meanwhile |
| `store_busy` | the I/O pool (store open / meta / lag query) has 16 calls queued or running | Arango/MinIO stalled; check their health |
| `executor_closed` | a pool was shut down | planner is stopping; ignore during a restart |
| `robot_unavailable` | robot status not found | robot offline/unknown; legacy will fail the same way |
| `robot_not_placed` | robot has no pose in this map | localise the robot; legacy reports it to the caller |
| `store_unavailable` | scene graph store could not be built (retried after 30 s) | planner lacks ARANGO_*/MINIO_* env or store down |
| `store_timeout` / `store_error` | store open or `meta` timed out (> `LOAD_TIMEOUT_S`) / raised | Arango/MinIO slow or down; check their health |
| `no_graph` | map has no saved version | map not enabled, never ingested, or emptied (version 0 rebuild) |
| `load_timeout` | cold graph load took > `LOAD_TIMEOUT_S`; it continues in the background and fills the cache | only when no older version of the map is cached (first request after restart; pre-warm avoids it). A newer version is loaded in the background (single flight) while the newest cached one is served, bounded by `stale_graph`. Persistent on a quiet map = graph too large for the timeout |
| `load_error` | `store.load` raised, or the map is in load back-off after a failed load (5 s doubling to 300 s; no new load per request; an older cached version is served meanwhile) | store/snapshot problem; see worker `save` failures and planner log (`scene graph load ... failed`) |
| `stale_graph` | the oldest costmap stored after the version's cursor is older than `MAX_VERSION_LAG_S`; or a newer version has been known for longer than that while an older cached one is served | worker behind, stalled, or map switched off while uploads continue. Unknown lag (no cursor, query error) never rejects |
| `start_no_navigable_space` / `goal_no_navigable_space` | no free cell near the point | robot pose or goal outside mapped free space |
| `start_too_far` / `goal_too_far` | nearest free cell beyond the snap limit (0.5 m / 1.5 m) | pose inaccurate or goal tapped in a wall; raise snap only with care |
| `start_crosses_blocked` / `goal_crosses_blocked` | the snap would cross a known blocked cell | robot/goal inside an ever-blocked region |
| `blocked_by_overlay` | route exists without the active blocked-node rows, not with them | an active block (expiry pending) stands in the way; legacy then reports `blocked_nodes` |
| `unreachable` | no route in the chart graph | real disconnection, or map not yet fully ingested |
| `missing_tiles` | the corridor needs tiles not loaded | snapshot incomplete/inconsistent; investigate the version |
| `corridor_too_large` | the request's corridor needs more than `LAZY_MAX_TILES` owner tiles | very long route on a big map. The default `SCENE_GRAPH_LAZY_MAX_TILES` is 400 owner tiles; a tile is 64 lattice cells = 6.4 m a side, so this caps the tiles read per request, not a distance. Raise it if memory allows, or accept the legacy planner |
| `tile_unavailable` | a stored tile of the cached version could not be read (a garbage-collected version, MinIO error). The graph is dropped from the cache; the next request re-opens the current version | a one-off after a gc is normal; persistent = MinIO problem or `keep_last` too small for the served stale version (`keep_last` is fixed at 5 in code, not env-configurable: a planner that serves a stale version across more than 5 publishes will see this reason) |
| `too_narrow` | `min_width` < what the robot needs (width + margin straight, `hypot(length, width)` + margin where it turns); **inactive unless `SCENE_GRAPH_WIDTH_CHECK_ENABLED`** (see "Width gate") | route has a passage this robot cannot take |
| `leaf_lookup_error` | the nearest-leaf query (one kNN per waypoint) raised | Arango problem; see planner log (`scene graph leaf lookup failed`) |
| `waypoints_without_distinct_leaf_ids` | after dropping interior waypoints where line of sight allows, a waypoint still has no leaf within `LEAF_SNAP_M` (1.0 m). Waypoints that share a leaf no longer cause it: consecutive repeats are merged where line of sight allows, a route on one leaf becomes `[start, goal]`, an unmergeable repeat is served as is (the name is historic) | sparse leaf coverage on the route; raise `LEAF_SNAP_M` or map more |
| `blocked_row_on_route` | a blocked row without a robot pose (old disc rule) hits the final waypoints | legacy planner reports/detours it |
| `plan_timeout` | snap + route + simplify + leaf lookup > `PLAN_BUDGET_S` (the thread keeps running and holds its slot) | map/route too large for the budget; see capacity |
| `error` | unexpected exception (logged with repr) | read the planner log |

Response notes: the last waypoint repeats the previous waypoint's bearing (the request carries no goal heading, so
there is no `goal_theta`); `end_node_id` is the leaf nearest the last waypoint (within `LEAF_SNAP_M`, up to 1 m
away) and `target_node_position` the snapped goal cell centre (within `GOAL_SNAP_M` of the tap), so the two may
differ by up to about 1 m (the legacy planner returns a node and its own position).

## 8. Capacity and timing

* **Ingest cadence.** Reads 200 nodes per step; folds when 6 leaves wait or the oldest waited 5 s; a backlog goes 48
  leaves per fold. Saves are floored at 5 s apart per map, and also when 50 unsaved leaves / 30 s / idle. A
  publish therefore comes at most every 5 s per map, usually slower under load.
* **Many maps, bounded memory.** Maps are stepped on a pool of `SCENE_GRAPH_WORKER_THREADS` threads (one step per
  map at a time, the due map that has waited longest first), so one slow fold delays only the maps that need that
  thread, not all of them. At most `SCENE_GRAPH_MAX_MAPS` (64) workers per process; engines resident in memory are
  bounded separately (idle eviction after `EVICT_IDLE_S`, `MAX_RESIDENT_ENGINES`, `MAX_RESIDENT_BYTES`; an engine
  with unsaved folds is saved before it is dropped, never lost). When every resident engine is active the budget is
  exceeded and `over_budget` shows in `/health` instead of anything being torn down.
* **Sizing (estimates, not measurements of this service).**
  * Memory: per resident engine not measured on the current (Morse) engine; the earlier per-cell figure came from the
    previous engine and is not carried over. Read `/health` `resident.bytes` after the first generation and set
    `MAX_RESIDENT_BYTES` from that, to roughly 60-70 % of the graph-builder
    container's memory limit: the rest is for the fold's temporaries (a fold can need a multiple of the engine),
    decoded PNGs (`PREFETCH_QUEUE` x a costmap) and the process. A cap above the container limit means an OOM kill
    instead of an eviction. `MemoryError` inside a fold is handled (backoff, then bisect), an OOM kill is not.
  * `WORKER_THREADS` vs cores: each fold is mostly CPU-bound numpy/scipy; more threads than physical cores only
    interleave them (and multiply peak memory, since every running fold has temporaries). The default is
    `min(8, cores)`.
  * Connections: Postgres = owned maps + threads + a few; MinIO's default pool is 10 per host (section 1).
* **Fold cost (CIMBI-measured on Morse, per batch, not re-measured on the production stack).** The partition is
  a function of the folded cells and costs about 1.1-1.5 us per cell per batch: a 45k-cell hall 0.07 s, an 86k-cell
  field 0.11 s, a 244k-cell open field 0.63 s (`handoff_laci_morse.md`). That is well under `BATCH_BUDGET_S` (10 s);
  a `slow fold` warning now points at the store or the data, not at the engine.
* **Open-field / simulation maps.** No engine limit is known on Morse (see the figures above). Keep them
  `scene_graph_enabled=false` until a measurement on the production stack exists.
* **Backfill of an existing map** is resumable and runs at the builder's fold cost (large batches); its duration
  on Morse has not been measured. Rebuild cost is the same full refold; there is no per-extent re-solve.
* **Planner.** The whole chart-route attempt must fit 3.5 s (5 s dispatcher timeout); the plan itself 3 s of that.
  A cold load is waited for at most 2 s; a new version loads in the background while the older cached one is served
  (2 versions per map, 4 per process, optional byte cap). The planner is lazy: opening is the
  manifest only (10 ms at 60 m, 69 ms at 150 m on the same bench; the former `full_load` cold load, `store.load` of the whole version, took 1.9 s and 12.6 s there, `planner_upgrade.md` section 0) and the cost scales with the corridor. Read API: result cache 96 MiB / 64 results, 2 loaded graphs, 8 tile views.
* **Restart replay.** The cursor is the lower of the in-memory copy and the store's; replay margin
  `SCENE_GRAPH_REPLAY_MARGIN_S` (60 s); a replay that folds nothing new publishes no version (same
  content hash); a rebuild restarts from zero unless it can be resumed (section 11).

## 9. Known limitations

Open defects are kept in `integration.md`, "Open defects"; the operational ones:

* **Live vs rebuilt graph.** The partition is a pure function of the folded cells, so a live-built and a rebuilt
  graph have the same charts and links; only the driven polyline and the per-cell latest-capture evidence follow the
  capture-stamp order.
* **Blocked-row pose pairing is only SQL-tested.** The upsert keeps the robot pose and the waypoint it was reported
  with as one pair (a robot pose is never combined with another report's waypoint); the unit tests pin the SQL text,
  and no e2e test runs it against Postgres.
* **Migration `20261012_01` `prev_x` / `prev_y`** are reserved columns: nothing writes or reads them (an edge row
  without a robot pose stays a disc). Its revision is parented on `20261013_01_track_map_frame`; as of 2026-10-13 it
  is not applied in the live DB.
* **No authorization** on the three scene-graph routes (nor a rate limit); they follow `/maps/{id}/graph`. The
  design is written down (integration.md, finding 13) and deferred by the owner as an API-wide change.
* **`r_d` rule**: half of the *smaller* footprint side; unknown footprint (-1) uses 0.25 m and is counted
  (`default_r_d`, in `/health` and the log). Optimistic for a long robot: a passage it cannot turn in can look navigable at ingest,
  and `Route.min_width` is compared with the same width (the planner's turn rule, enabled with the width check,
  does use the length). Decision deferred by the owner; revisit before enabling the planner for a robot with
  length >> width.
* **Not run on production.** Worker, store, guard, saga and planner pass the e2e harness against real ArangoDB / MinIO /
  Postgres (throw-away containers, synthetic worlds). The first run on the production stack is still the verification
  of timing, volumes and the fold cost above.
* **Cold graph load was the planner's weak point in the removed `full_load` mode** (section 8). The tile-lazy planner (Phase 0)
  is now the only mode; phases 1-3 are plan only (`planner_upgrade.md`).
* **Bucket-slug collisions.** MinIO buckets are `lower(name)` with `_` -> `-`. Two registered maps that map to one
  bucket (`a_b` / `a-b`, `My_Map` / `my-map`) cannot be told apart: the worker skips both and logs `candidate ...
  matches several registered maps`. Rename one. No event is emitted for this.
* **Per-extent re-solve not built.** Node delete (and any params change) is a full rebuild of the map; during it the
  old version is served, and only after it do deletions disappear from the planner. Realign triggers no rebuild.
* **Stale leaf stamps.** A leaf folded under a provisional receipt-time stamp can be folded again under an earlier
  corrected one; it can look "too new" in cells it won until newer data arrives.
* **Delete-triggered rebuild resume and clock skew.** A pending generation is resumed only when its `begun_ms` (the
  graph-builder's wall clock) is not before the delete request's `at` (the API's wall clock): two clocks compared.
  On one host (the compose default, `network_mode: host`) that is exact. With split hosts, if the graph-builder
  clock runs *ahead* of the API clock by more than the time between the generation's start and the delete, a
  generation that predates the delete can be resumed and keep the deleted leaf until the next rebuild (the other
  direction only restarts a resumable generation: safe, slower). Keep both hosts on NTP.
* **Metrics are `/health` and `get_stats()` only**; no Prometheus endpoint. Counters are per process and reset on
  restart. The cursor store is process-local (the durable cursor is in `sg_meta`).
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
| Read API 422 "bbox is N cells" / "N charts" | request size (over 198.4 m a side at 0.1 m, snapped) | smaller bbox or higher level; the client tiles (section 12) |
| Read API 409 "tiles changed" / lineage 409 | version moved during the read / span > 256 / too old | retry; for lineage reselect from current charts |
| Read API 409 `epoch_changed` | the map was recreated or its graph dropped and rebuilt since the `?epoch=` the client sent | drop cached views and selections, refetch without a pin (clients do this themselves) |
| Read API 503 + `Retry-After: 2` | store (Arango/MinIO) down or timing out; never "no graph" | fix the store; clients back off and keep their last view |
| Read API 404 on `?version=N` | N does not exist yet, or is no longer retained | read without `version` |
| Planner always `load_timeout` | no cached version yet: map size, MinIO latency, pre-warm failed (planner log) | wait for the background load, set `SCENE_GRAPH_PREWARM_MAPS`, or raise `LOAD_TIMEOUT_S` within the 5 s dispatcher budget |
| Planner `budget_exhausted` / `planner_busy` / `store_busy` often | `get_stats()["scene_graph"]` (`io_inflight`, `plan_inflight`, `outcomes`); planner log; store latency | slow Arango/MinIO/Postgres or a hung plan: fix the dependency; pools drain by themselves, a restart clears hung threads |
| Planner always `stale_graph` | lag: worker stalled, behind, or map disabled while uploads continue | fix ingest, re-enable the map, or (last resort) `MAX_VERSION_LAG_S` higher / 0 |
| Planner `store_unavailable` | planner env has ARANGO_*/MINIO_*; retry is every 30 s | fix env/network, restart planner |
| Route clips a known obstacle / robot refuses leg | active `blocked-nodes` rows (`GET /api/v1/maps/{id}/blocked-nodes?with_chart_keys=true`; run legs: `GET /api/v1/runs/{id}/legs?with_chart_keys=true`); migration applied | clear/expire row; check migration |
| Blocked rows missing pose (disc rule only) | columns NULL: rows written before the migration or without localization | none; expire naturally |
| Map delete stuck at `scene_graph` | `scene_graph: lock ...` in the map's `delete_error`, `delete_attempts` not rising; the worker holds the lock during a long fold | retried with backoff without consuming attempts until `SCENE_GRAPH_DELETE_LOCK_DEADLINE_S`; if persistent, disable the map and wait for release (~2 min) |
| `slow fold` warnings | fold times in the log, store latency | find the slow dependency (MinIO/Arango) or the data; as a last resort disable the map |
| Planner `corridor_too_large` often | `outcomes` | raise `SCENE_GRAPH_LAZY_MAX_TILES` if memory allows, or accept the legacy planner |
| Planner `tile_unavailable` | MinIO health, version gc (`keep_last` 5) | one-off after a gc is normal |
| graph-builder OOM-killed / restarts under many maps | `/health` `resident.bytes` vs `max_bytes`, container memory limit | set `SCENE_GRAPH_MAX_RESIDENT_BYTES` below the limit (section 8), lower `WORKER_THREADS` |
| `/health` `resident.over_budget: true` | every resident engine is active; nothing is evicted | expected under sustained load of many maps; raise the cap/limit or reduce `WORKER_THREADS` |
| `fold_failed` stage `poison_leaf` | `detail` names the node key | the leaf is already quarantined and skipped; inspect that node's costmap, or ignore a single one |
| `fold_failed` stage `engine_faulted` repeating, then REBUILD_STARTED reason `engine_faulted` | worker log traceback | report to CIMBI with the map; the worker reloaded/rebuilt itself; `needs_rebuild` terminal means the rebuild faulted too |

## 11. Worker lifecycle and `/health`

How the ingest worker behaves; everything here is per map, inside one graph-builder process.

**Ingest.** Nodes are read in `(costmap_stored_ms, node key)` order past the cursor, minus the replay margin
(`SCENE_GRAPH_REPLAY_MARGIN_S`, 60 s, full margin at start / after a failure / every `REPLAY_RECHECK_S`; other polls
re-read 5 s). A node whose PNG is confirmed missing stays pending, is asked again every 2 s and is dropped after 60 s
of wall clock (`png_missing_expired`); the persisted cursor stays below the oldest pending node. A PNG fetch that
raises (MinIO down) is not a verdict: reading stops at that node and backs off. Leaves are buffered and folded in one
`fold_many` per batch (6 .. 48 leaves, or the oldest waited 5 s).

**Saves and cursor.** A version is the return of `store.save`; saves are floored at the publish interval. The cursor
is saved with the version. Skip-only progress (nothing to save) moves the cursor in memory and becomes durable as a
cursor-only save once it is more than the replay margin ahead of the stored one; the store updates `meta.cursor` in
place, so it is cheap. During a rebuild the same cursor-only save moves the *pending* generation's cursor (once the
pending generation holds a saved state).

**Rebuild** (params-hash change, node delete, `engine_faulted`, manual request):

1. A new generation is begun (`begin_generation`); readers and the planner keep the old version.
2. Every leaf is folded again into the pending generation in **fixed batches of `SCENE_GRAPH_REBUILD_BATCH` (48)**,
   with no time-based flush until the head of the nodes is reached, so the same nodes give the same chart partition
   after every rebuild. (The rebuild scans in `(costmap_stored_ms, key)` order, then folds in `(stamp_ms, key)` order in batches of
   48; the partition does not depend on batching.)
3. Committed (`commit_generation`) when the scan reached the head and nothing is buffered or pending; a rebuild
   older than `SCENE_GRAPH_REBUILD_MAX_AGE_S` (900 s) is committed at its next save even if uploads keep arriving.
4. The request key is cleared only if its `at` is unchanged (a delete that arrived mid-rebuild causes one more).
   An empty rebuild (no foldable leaf twice in a row) drops the graph and reports version 0 (section 5).

A **delete-triggered** rebuild is debounced: it starts only after the request has been the newest for
`SCENE_GRAPH_REBUILD_DEBOUNCE_S` (30 s; a burst of deletes is one rebuild). A pending generation found at
(re)start is **resumed from its cursor** when it has the expected params hash and, if a delete request exists,
only when its `begun_ms` is not before the request's `at` (it cannot hold a deleted leaf; store clock vs API clock,
see Known limitations) **and** it is younger than `REBUILD_MAX_AGE_S`; an old-format record without `begun_ms`, an
older generation or one with another params hash starts empty. A mid-run request always starts a fresh generation.
`REBUILD_STARTED` carries `resumed: true` when it continued one.

**Failures.**

| Failure | Handling |
|---|---|
| Engine load fails deterministically (corrupt / mismatched stored graph: `StateMismatch`, `TooOld`, `KeyError`, `ValueError`, ...) | `fold_failed` events (stage `load`), retried every 10 s; after `SCENE_GRAPH_LOAD_FAIL_LIMIT` (3) in a row the worker falls into a rebuild instead of loading the whole map every backoff (or `needs_rebuild` when it cannot rebuild). Transient failures (store down) never count |
| `LeafError` (one leaf rejected in validation, nothing mutated) | that leaf is quarantined **immediately by the key the error names** (event `fold_failed` stage `poison_leaf`), the rest of the batch is folded. No two-failures rule. Only an error naming no leaf of the batch bisects it |
| `EngineFaulted` or any other exception from a fold (engine state undefined) | stage `engine_faulted`: engine dropped, reloaded from the store (live version, or the saved pending generation of a rebuild), reads rewind to the durable cursor. `LOAD_FAIL_LIMIT` faults in a row without a good fold -> rebuild with reason `engine_faulted`; a fault inside that rebuild (or no way to rebuild) -> `NEEDS_REBUILD` |
| `MemoryError` | nothing changed: backoff and retry; the same batch failing `SCENE_GRAPH_MEMORY_RETRY_LIMIT` (3) in a row is bisected and a leaf that still fails alone is quarantined like a poison leaf (`memory_isolations` counter) |
| Save fails | folded state kept in memory, retried every 10 s (stage `save`) |
| Fenced (lock lost, stale expected version, generation superseded) | terminal: worker closed, lock released, map retried after 60 s |

**Eviction.** An idle engine (nothing buffered, pending or unsaved) of a map quiet for `EVICT_IDLE_S` is saved and
dropped when the resident count or bytes exceed the caps; the next leaves reload it from the store. Seen-map, cursors
and rebuild state are kept, so nothing is re-folded.

### `/health` `scene_graph_worker` (graph-builder, `GET /health`)

`{enabled, running, workers, maps, pool, resident, prefetch}`; `{enabled: false, running: false}` when the worker is
off, `{enabled: true, error}` when the snapshot itself failed.

| Field | Meaning |
|---|---|
| `maps.<map>.state` | last step state: `worked`, `idle`, `backoff`, `not_owner`, `deleting`, `fenced`, `needs_rebuild` |
| `maps.<map>.last_step_age_s` | seconds since the map was last stepped (a large value on a worked map = starved or stuck) |
| `maps.<map>.version`, `pending`, `buffered`, `version_lag_s`, `skipped{}` | published version, nodes waiting for a PNG, leaves waiting for a batch, age of the oldest unsaved fold, skip counters by reason |
| `maps.<map>.already_folded`, `default_r_d`, `views_built`, `views_missing`, `view_failures` | leaves skipped because the stored state holds them; leaves folded with the default footprint; leaves folded with a camera view / without a depth record / with an unusable camera block |
| `maps.<map>.resident`, `resident_bytes` | an engine is in memory; its `nbytes` (null = unknown, 0 when not resident) |
| `maps.<map>.loads`, `evictions`, `prefetch_hits` | engine loads through the factory, evictions, leaves whose PNG came from the prefetch pool |
| `pool.threads`, `inflight`, `queue_depth`, `cold_inflight` | step pool size; steps running; due maps that found no free thread at the last tick; running steps that are cold engine loads |
| `resident.engines`, `bytes`, `bytes_unknown`, `max_engines`, `max_bytes`, `evictions`, `over_budget` | engines in memory and their bytes (`bytes_unknown` = engines that cannot say), the caps, evictions so far, every resident engine active so the cap is exceeded |
| `prefetch.threads`, `max_pending`, `outstanding`, `queue_depth`, `submitted`, `rejected`, `hits` | PNG prefetch pool (absent when `PREFETCH_THREADS=0`) |

Reading it: `pool.queue_depth` persistently above 0 with `inflight == threads` means more due maps than threads
(raise `WORKER_THREADS` if cores allow, else it is just load); `cold_inflight` equal to `threads - 1` for long is a
big map loading; `resident.bytes` near `max_bytes` with rising `evictions` is the cap working.

## 12. Read API contract (api-delegation-service)

Routes: `GET /api/v1/maps/{id}/scene-graph?bbox=&level=&version=&raster=&epoch=`, `.../scene-graph/raster?bbox=&level=
&version=&plane=owner|dependent&epoch=`, `.../scene-graph/lineage?from_version=&epoch=`. No authorization (section 9). `plane=dependent` is accepted but always all-zero since FORMAT 4; do not build on it.

| Topic | Behaviour |
|---|---|
| `available` | `false` (200) only for genuine absence: the map has no saved graph (never built, dropped by an empty rebuild) or `SCENE_GRAPH_WORKER_ENABLED` is off on the API. Raster: 404. All three answer `Cache-Control: no-store` in that case. A store failure is never `available: false` |
| Epoch | an integer (48-bit hash of the incarnation `(created_ms, generation)`), in the body (`epoch`) and header `X-Scene-Graph-Epoch`; a map recreated under the same id restarts at version 1 with another epoch |
| `?epoch=` pin | a pin that no longer matches is **409** with body `{"detail": {"error": "epoch_changed", "epoch": <current>, "current_epoch": <current>, "version": <current>}}` (+ header, `no-store`); also on a map that lost its graph (`epoch: null`) |
| ETag / 304 | JSON ETag over (epoch, version, level, raster flag, parents flag, snapped bbox); raster ETag over (epoch, version, plane, level, snapped bbox). A matching `If-None-Match` answers 304 before anything is computed, with the headers |
| Cache-Control | no `version` in the request: `no-cache` (revalidate every time); with `version`: `public, max-age=60`; never `immutable` (the URL does not carry the epoch). Map without a graph: `no-store`. 503 and 409: `no-store` |
| 503 | transient store/object-store failure (also the topomap client not up yet): `Retry-After: 2`, body `{"detail": {"error": "scene_graph_unavailable", "retry_after_s": 2}}` |
| Area cap | the bbox is snapped outward to the 6.4 m tile grid and capped at 4,000,000 lattice cells: **31 snap tiles = 198.4 m a side at 0.1 m** (32 would be 4.19 M cells); 422 above. Also 422: more than 20,000 charts in the answer, more than 65,534 chart keys in a raster (u16 plane), `level` outside 0..8 |
| Versions | `?version=N` above the current or negative: 404 `does not exist`; an older version that is no longer retained: 404 `not retained`. 409 "tiles changed" when the snapshot moved under a read: retry |
| Lineage | 409 when `from_version` is more than 256 versions behind or the store no longer retains the span: reselect from current charts |
| Caches | per process: results 64 / 96 MiB, loaded graphs 2, tile views 8, lineages 32; all purged for a map when its graph disappears; keys carry the epoch |

**Chart keys on blocked nodes and run legs** (`GET /api/v1/maps/{id}/blocked-nodes?with_chart_keys=true`,
`GET /api/v1/runs/{id}/legs?with_chart_keys=true`). Derived at read time, nothing is stored. Best effort, bounded:
at most 6 owner planes are read per request (rows/legs are grouped into as few planes as the area cap allows;
what does not fit is left untagged), at most 400 distinct topomap nodes are looked up for legs, and the whole
tagging has a 3 s timeout. The response carries `chart_keys_status` (`ok`: everything tagged or nothing to tag;
`partial`: some items untagged because of the limits; `unavailable`: no graph, a store error or the timeout, nothing
tagged), `chart_keys_truncated` (bool) and `chart_keys_truncated_count`. For blocked nodes they are top-level
fields next to `blocked_nodes`; for run legs top-level next to `items`. A leg with unknown node positions has
`chart_keys: null`. The mission aggregate (`/missions/{name}/legs`) is not tagged. A cell outside every chart tags as
no key (no snapping to the nearest navigable cell).

**Blocked-node rows.** `run_id` is derived from the VDA node id and resolved against `mission_runs` with bounds:
at most 500 rows, 100 distinct missions and the newest 5,000 runs per call (newest rows first; the rest keep the
derived id). The upsert pairs the poses: the robot pose (`robot_x`/`robot_y`) and the waypoint (`x`/`y`) of a row
always come from one report. A re-report without a pose, while the block is still active and holds a complete
pose pair, keeps the earlier pair (including its waypoint); on an expired row the new report alone starts the block.
Without the migration the dispatcher writes and reads the column-less statements (disc rule, one warning per write).

**Clients (sati-client).** The map window draws the chart layer from these routes: a view wider than one request
(31 snap cells, ~198 m) is cut along a fixed 192 m tile grid into at most 4 tiles, more than that shows the banner
"Scene graph: zoom in to see charts" and fetches nothing; a newer request aborts the one in flight, nothing is
fetched while the browser tab is hidden (refetch on return, a 304 when unchanged); a 503 / network failure keeps the
last view on screen (`stale`) and retries with back-off (1 s doubling to 30 s, honouring `Retry-After`); the epoch is
remembered per map and sent as `?epoch=` (a 409 `epoch_changed`, or a websocket hint of another epoch, drops cached
views and refetches); a `scene_graph_updated` hint with `version: 0` means "no graph". Chart selection was removed
(it was unused).
