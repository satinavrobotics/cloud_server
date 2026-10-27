# Region statistics from stored per-run cell summaries (implementation scope, 2026-10-27)

Status: **implemented on branch `stats/region-stats` (off `main` b6dc517), not merged or deployed**; see section 11 for what differs from this text, and section 12 for the changes of 2026-10-28 for the planner (branch `stats/planner-region-cost`).
Supersedes, for statistics, the "chart transits / link-stats" stage of `docs/tracking_redesign_2026-10-13.md` and
`docs/scene_graph/edge_stats_on_charts_design.md`. Line numbers below are of `main` at b6dc517; re-check before relying on them.

## 1. Decision (what and why)

* **Source of truth: one cell summary per run** (`run_cells`). Written once after a run ends: its map-frame movement
  rasterised into 0.1 m cells (the scene graph's lattice / owner-raster step) with per-cell length, time, stopped,
  recovery seconds and blocks. Exactly what `packages/api/drive_stats.py` computes on every read today, stored.
  Cells never change meaning when charts split, merge or regenerate, so no lineage bookkeeping is needed.
* **Stored aggregate for the planner: `region_stats`.** Per `(map, graph_version, chart_key, robot_class)`, built by
  grouping the run summaries by the owner raster of that graph version. A rebuildable cache: it can be dropped and
  rebuilt from `run_cells` at any time. The planner (later, out of scope) will load it per (map, version, stamp) as a
  lookup of about N charts, so pricing a route costs nothing extra.
* **Per robot class, not per robot**, for the aggregate (more samples per number, works for a new robot of a known
  class). Per-robot figures stay available for display by computing on read from `run_cells` (drive-stats `?robot=`).
* Not stored on the scene-graph manifest (immutable per version; stats change per run) and not in ArangoDB (the
  planner does not read Arango).

Why not edge/link keyed: the planner has no leaf edges; per-edge samples are sparse; leaf ids and chart pairs are not
stable keys. See the conversation summary in memory `stats-review-2026-10-27`.

## 2. Out of scope (do NOT do in this work)

* Planner changes (weights, multipliers, ranking). Only provide the pure loader in section 6.4.
* Removing `edge_observations`, `/edge-stats`, the leg/mapping writers, the edge retention loop (a later stage, after
  the client moved).
* sati-client changes (a later stage; the drive-stats response shape must stay compatible, see 6.5).
* `robot_track_ts` retention changes, map realign handling (see open items), `satibot_orchestrator` (never edit it).
* Deploying anything. Commit on the branch only; do not merge, push or deploy.

## 3. Facts the implementation relies on (verified 2026-10-27)

* Rasteriser pieces in `packages/api/drive_stats.py`: `Params` (:50), `cell_key`/`key_cells` (:61-70), `Segments`,
  `track_segments` (:80), `recovery_overlap` (:101), `Accumulator` (:135) with `add`/`merged_with`/`tiles`, `Tiles`
  (:198), `leg_movements` (:208), `plane_lookup` (:231), `regions_of` (:244, reads `reads.owner_plane` +
  `reads.view` per 160 m chunk), `region_rows` (:291). DB reads `fetch_track` (:337), `fetch_recoveries` (:352),
  `fetch_legs` (:365), `runs_with_track` (:376), `load` (:398), `compute` (:414), `series_day` (:466).
  Track rows carry `(run_id, ts, x_map, y_map, map_session_id, run_epoch)` (grouping key `(r[4], r[5])`).
* Leg fallback needs leaf node positions from ArangoDB (`graph.get_all_nodes(map_id)`, drive_stats.py:589-595): only
  the API service has both Postgres and Arango plus the scene-graph read side. **So the summariser runs in the API
  service**, not in the dispatcher's recorder.
* Run end in the dispatcher: `fleet_recorder.py:1710-1711` (`_summarize_run`, `_record_edge_observations`). Leave it
  unchanged; the API worker picks up finished runs from `mission_runs` (`ended_at IS NOT NULL`).
* Track rows of a run can arrive after the run row is finished (`TRAJECTORY_GRACE`, `config.RECORDER_TRAJECTORY_GRACE_S`).
  The worker must wait at least that long after `ended_at`.
* Recoveries: `fleet_events` code `NAV.RECOVERY_EXITED` with `duration_s` (interval `ts - duration_s .. ts`).
  Blocks: codes `NAV.GOAL_BLOCKED` and `MISSION.EDGE_BLOCKED` (fleet_recorder.py:200); their payload has no pose,
  only `ts` (and `leg_seq` for EDGE_BLOCKED).
* Robot class: there is no type id. `RobotStatusV1.factsheet: RobotTypeIdentifierV1` (cloud_common/objects/robot.py:101,
  :216) has `agv_class`, `length`, `width`, `speed_max` (-1 = unknown). Where the API gets the robot object: the same
  way other API reads do (find it; e.g. the robots read in `api/main.py`).
* Multi-worker API: background singletons use a Postgres advisory-lock election (`packages/api/telemetry.py:7`,
  `map_delete.py`, `entrypoint.advisory_lock_key`). Reuse that pattern.
* Migrations: `packages/api/migrations/versions/`, date ids, head `20261027_04_leg_moving`; `alembic_version` column
  is 32 chars max. `tests/unit/test_navstack_deploy*` pins the migration head: update it.
* Unit test env: `/tmp/lkvenv` (pydantic 1.10, scipy, py_trees). Baseline on b6dc517: 67 failed / 39 errors, all
  pre-existing. New failures are not acceptable.
* The client reads drive-stats `regions` and `tiles` (`sati-client/services/mapApi.ts:1627-1670`) and does not send
  `min_speed`.

## 4. Data model

### 4.1 Migration `20261028_01_run_cells` (one file, both tables)

```sql
CREATE TABLE run_cells (
  run_id         uuid        NOT NULL,          -- same type as mission_runs.run_id (check)
  map_id         text        NOT NULL,
  robot_name     text        NOT NULL,
  robot_class    text        NOT NULL,          -- section 4.3; '' when the factsheet is unknown
  speed_max      float4,                        -- factsheet speed_max at summary time, NULL if unknown
  started_at     timestamptz NOT NULL,
  ended_at       timestamptz,
  source         text        NOT NULL,          -- 'track' | 'legs' | 'empty'
  algo_version   int2        NOT NULL,          -- RUN_CELLS_ALGO_VERSION; bump = recompute
  cell_m         float4      NOT NULL,          -- 0.1
  params         jsonb       NOT NULL,          -- min_speed, max_gap_s, max_speed, leg_max_m used
  frames         jsonb       NOT NULL DEFAULT '[]',  -- distinct [map_session_id, run_epoch] of the track rows
  ix             int4[]      NOT NULL,          -- absolute cell indices floor(x / cell_m), sorted by (ix, iy)
  iy             int4[]      NOT NULL,
  len_m          float4[]    NOT NULL,
  time_s         float4[]    NOT NULL,
  stopped_s      float4[]    NOT NULL,
  recovery_s     float4[]    NOT NULL,
  blocks         int2[]      NOT NULL,
  totals         jsonb       NOT NULL,          -- {len_m, time_s, stopped_s, recovery_s, blocks, segments, rejected,
                                                --  recovery_unattributed_s, blocks_unattributed, legs_skipped}
  created_at     timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (run_id, map_id)
);
CREATE INDEX run_cells_map_started_idx ON run_cells (map_id, started_at DESC);
CREATE INDEX run_cells_map_class_idx   ON run_cells (map_id, robot_class, started_at DESC);

CREATE TABLE region_stats (
  map_id         text        NOT NULL,
  graph_version  int         NOT NULL,
  chart_key      text        NOT NULL,
  robot_class    text        NOT NULL,          -- '*' = all classes pooled
  runs           int         NOT NULL,          -- distinct runs with len_m >= REGION_STATS_MIN_LEN_M in the chart
  len_m          float8      NOT NULL,
  time_s         float8      NOT NULL,
  stopped_s      float8      NOT NULL,
  recovery_s     float8      NOT NULL,
  blocks         int         NOT NULL,
  pace_median_s_per_m  float8,                  -- over per-run samples (section 6.3); NULL when runs = 0
  pace_p90_s_per_m     float8,
  moving_pace_median_s_per_m float8,
  moving_speed_mps     float8,                  -- len_m / (time_s - stopped_s)
  stopped_share        float8,                  -- stopped_s / time_s
  recovery_s_per_100m  float8,
  blocks_per_run       float8,
  speed_max            float8,                  -- the class's factsheet speed_max (NULL for '*' or unknown)
  cells          int         NOT NULL,          -- the chart's cells (from the view)
  visited        int         NOT NULL,          -- cells of the chart with any movement
  first_at       timestamptz,
  last_at        timestamptz,
  stamp          text        NOT NULL,          -- section 6.3: identifies the input set
  built_at       timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (map_id, graph_version, chart_key, robot_class)
);
CREATE TABLE region_stats_builds (              -- one row per (map, version): what was built from what
  map_id text NOT NULL, graph_version int NOT NULL, stamp text NOT NULL, runs int NOT NULL,
  status text NOT NULL, detail text, built_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (map_id, graph_version));
```

Downgrade drops the three tables. Arrays (not one row per cell) keep a run to one row: about 2-5 k cells for a few
hundred metres (estimate), TOAST-compressed. `ix, iy` are absolute (not relative to an origin) so the owner raster
lookup needs no stored origin.

### 4.2 Map delete / run delete

Extend `packages/api/map_delete.py` (the `DELETE FROM edge_observations` place, :88-89) with `run_cells`,
`region_stats`, `region_stats_builds` for the map. Find where a run row is deleted (operator run delete) and delete
its `run_cells` rows there too, and mark the map's region stats stale (6.3).

### 4.3 Robot class

**Changed 2026-10-28 (section 12):** `robot_class = f"{agv_class or '-'}|{length:.2f}x{width:.2f}"` from the robot's factsheet, with
any field that is -1 written as `?`; `''` when the robot or factsheet is not found (or none of the three was sent). `speed_max` is
no longer part of the class; it is stored per run with a `speed_basis`. One pure function
`packages/utils/robot_class.py:robot_class(factsheet) -> str` with tests. (The planner uses the same function on the
planning robot's factsheet, so it must be importable without the API.)

## 5. Code layout

| File | New / changed | Content |
|---|---|---|
| `packages/utils/cell_raster.py` | new | Moved unchanged from drive_stats: `CELL_M`, `SAMPLE_DIV`, `MAX_SAMPLES_PER_SEGMENT`, `CHUNK_SEGMENTS`, `_OFF`, `Params`, `cell_key`, `key_cells`, `Segments`, `track_segments`, `recovery_overlap`, `Accumulator`, `Tiles`, `leg_movements`. Plus new `block_cells` (6.1). Pure numpy, no I/O, no FastAPI. |
| `packages/api/drive_stats.py` | changed | Imports the moved names from `cell_raster` (keep re-exports so existing tests and imports work). Section 6.5. |
| `packages/utils/run_cells_db.py` | new | SQL + encode/decode between `run_cells` rows and `Tiles`-like arrays; `insert(cur, row)` (`ON CONFLICT (run_id, map_id) DO UPDATE` so a recompute replaces), `fetch(cur, map_id, start, end, robot, robot_class, limit)` returning numpy arrays per run. |
| `packages/api/run_cells.py` | new | `summarise_run(db, graph, robots, run_id) -> List[row]` (one per map of the run) and the worker (6.2). |
| `packages/api/region_stats.py` | new | `build_map(db, reads, map_id, version=None)`, the endpoint function (6.4), its cache. |
| `packages/utils/region_stats_load.py` | new | Pure loader for the planner later: `load(cur, map_id, graph_version, robot_class) -> Dict[chart_key, RegionStat]` (dataclass) + `stamp`. No API imports. |
| `packages/api/main.py` | changed | Start/stop the worker in the lifespan; route `GET /api/v1/maps/{map_id}/region-stats`. |
| `packages/config.py` | changed | Knobs in section 7. |
| `scripts/backfill_run_cells.py` | new | 6.2 backfill. |
| migration | new | 4.1. |
| `docs/API_REFERENCE.md`, `docs/drive_stats_2026-10-14.md` | changed | New route; drive-stats source change. |

## 6. Behaviour

### 6.1 Summarising one run (`summarise_run`)

For each map the run has data on:

1. **Track source**: the run's `robot_track_ts` rows with `x_map IS NOT NULL` (all of them, no window, no row cap),
   ordered by ts, grouped by `(map_session_id, run_epoch)` exactly as `compute` does; `track_segments` with default
   `Params`; recoveries of the run via `recovery_overlap`; `stopped = dt where dist < min_speed * dt`.
   `source = 'track'`.
2. **Legs source**, only when the run has no map-frame track row at all (same rule as `runs_with_track`): the run's
   `run_legs` with both topomap ids and `duration_s > 0`, node positions from Arango, `leg_movements`.
   `source = 'legs'`.
3. Neither: write a row with empty arrays and `source = 'empty'` so the worker does not retry it forever.
4. **Blocks** (new, `cell_raster.block_cells`): for each block event of the run (codes in section 3) find the
   segment whose `[t0, t1]` contains the event ts (else the nearest segment end within `max_gap_s`) and add 1 to the
   cell of the segment's position interpolated at ts. For the legs source: EDGE_BLOCKED with `leg_seq` -> that leg's
   end node cell; otherwise by ts within the leg's time span if `run_legs` has it; else count in
   `totals.blocks_unattributed`. Count ONE per event (the existing double-count check found 7 of 181 pair within
   30 s; leave as is).
5. Accumulate with one `Accumulator(1)` per run; output the sorted absolute cell indices and the per-cell sums.
   Totals of the arrays must equal the segment totals (test).
6. `robot_class`, `speed_max` from the robot's factsheet (4.3). `algo_version = config.RUN_CELLS_ALGO_VERSION` (start 1).

Run the numpy work in `asyncio.to_thread`.

### 6.2 Worker and backfill

* One worker per API deployment: advisory-lock election like `telemetry.py` (key `run_cells_worker`). Every
  `RUN_CELLS_POLL_S`:
  1. Select up to `RUN_CELLS_BATCH` runs with `ended_at IS NOT NULL`, `ended_at < now() - grace`
     (grace = `RECORDER_TRAJECTORY_GRACE_S + RUN_CELLS_EXTRA_GRACE_S`), `map_id IS NOT NULL`, and no `run_cells` row
     with the current `algo_version`. Oldest first.
  2. `summarise_run`, insert, commit per run. A failing run is logged and skipped for `RUN_CELLS_RETRY_S` (in-memory
     set is enough), never blocks the batch.
  3. Collect the maps touched; mark them dirty for region stats (6.3).
* Runs that end while a map is being deleted: insert must not resurrect rows (check the map still exists in the same
  transaction, like the recorder's `MISSION_SQL` check).
* `RUN_CELLS_ENABLED` (default true) turns the worker off.
* Backfill script: same function over all finished runs (optionally `--map`, `--since`, `--recompute` to ignore the
  existing rows). Idempotent. Same CLI style as `scripts/backfill_edge_observations.py`.

### 6.3 Building region stats (`build_map`)

* Input: the map's `run_cells` rows with `started_at >= now() - REGION_STATS_WINDOW_DAYS`, the current graph version's
  owner raster (`regions_of` logic: `reads.owner_plane` per chunk, `plane_lookup`; reuse, do not copy) and chart cell
  counts (`reads.view`).
* Per run: map its cells to chart ids; per (run, chart): len, time, stopped, recovery, blocks. A (run, chart) pair is a
  **sample** when its `len_m >= REGION_STATS_MIN_LEN_M` (default 1.0; drops boundary clips). Per (chart, class) keep
  the newest `REGION_STATS_MAX_RUNS` samples (default 50) by `started_at`.
* Per (chart, class) and per (chart, '*'): sums over the kept samples; `pace = time_s / len_m` per sample;
  median and p90 over samples (numpy `percentile`, linear); `moving_pace` with `time_s - stopped_s`; derived ratios as
  in 4.1 (NULL on zero denominators). `speed_max` = the class's (from its runs; NULL if mixed or unknown).
  Charts with no samples get no row.
* `stamp` = sha1 of `(algo_version, window, min_len, max_runs, sorted (run_id, created_at) of the inputs)`,
  first 16 hex. Write all rows of `(map, version)` in one transaction (delete + insert) plus the
  `region_stats_builds` row. Keep only the newest `REGION_STATS_KEEP_VERSIONS` (default 3) versions per map.
* When it runs: the worker, for each dirty map, debounced by `REGION_STATS_DEBOUNCE_S` (default 30 s); and on each
  poll for every map that has `run_cells` rows but whose latest `region_stats_builds.graph_version` is not the current
  graph version (find the cheapest way to read the current version per map; `reads` has it; check). No scene graph
  for the map -> build row with `status = 'no_scene_graph'`, no stats rows. Full rebuild per map, not incremental:
  percentiles cannot be updated incrementally and the work is small (measure it, 6.6).
* Day decay, priors and shrinkage are the planner's job later. The table stores `runs` so it can shrink.

### 6.4 Read route

`GET /api/v1/maps/{map_id}/region-stats?robot_class=&version=&robot=`

* Default: the stored rows of the newest built version (`robot_class` default `*`), plus `{graph_version, stamp,
  built_at, window_days, min_len_m, max_runs, current_graph_version, stale: bool}`. 404 unknown map.
* `robot=` (one robot, display only) or a `version` that is not stored: compute on read with the same function as
  `build_map` (without writing), cached in a `ResultCache` (`packages/api/result_cache.py`) keyed by
  (map, version, robot, stamp of the inputs).
* Response field names = the column names. Document in `docs/API_REFERENCE.md`.

### 6.5 Drive-stats and series read from `run_cells`

* `drive_stats.load/compute` gets a second source: sum the `run_cells` of the runs in the window (filter by
  `started_at` and `robot_name`) into `Tiles` (same `n` = distinct runs per cell, same `pair_tile/pair_run`). Runs
  finished but not summarised yet (and still-running runs) come from the existing raw path, so the answer is never
  missing recent runs: `raw runs = runs in window - runs with a current run_cells row`.
* Config `DRIVE_STATS_SOURCE = "summaries" | "raw"` (default `summaries`); `raw` = today's behaviour exactly. A
  request with a non-default `min_speed` uses `raw` (the summaries froze the default; say so in `source`).
* `source` block gains `summary_runs`, `raw_runs`; keep all existing keys (fill the ones that no longer apply with 0)
  so the client does not break. `partial` is true only when the raw part hit `DRIVE_STATS_MAX_ROWS`.
* `series_day` from summaries: a run's movement is attributed to the UTC day of its `started_at` (documented change:
  today a movement belongs to the day it started; runs over midnight shift). `new_tiles` / `cumulative_tiles` from the
  per-run cell sets. Raw runs keep the old per-segment rule.
* `regions` in drive-stats keep their current shape (sums from tiles); they are display figures, not the planner's.

### 6.6 Measurements to report (not gates)

On the cloud box DB, read-only, with a copy of the code on the branch (no deploy): bytes per `run_cells` row
(median / max over 50 backfilled runs written to a scratch schema or computed without insert), `summarise_run`
time, `build_map` time for the biggest map, drive-stats latency summaries vs raw for the default window. If writing to
the live DB is needed, do NOT; compute in memory and report.

## 7. Config (`packages/config.py`, env-overridable like the neighbours)

`RUN_CELLS_ENABLED=True`, `RUN_CELLS_ALGO_VERSION=1`, `RUN_CELLS_POLL_S=30`, `RUN_CELLS_BATCH=20`,
`RUN_CELLS_EXTRA_GRACE_S=30`, `RUN_CELLS_RETRY_S=600`, `REGION_STATS_WINDOW_DAYS=90`, `REGION_STATS_MIN_LEN_M=1.0`,
`REGION_STATS_MAX_RUNS=50`, `REGION_STATS_DEBOUNCE_S=30`, `REGION_STATS_KEEP_VERSIONS=3`,
`REGION_STATS_CACHE_TTL_S` / `_ENTRIES` (like the drive-stats ones), `DRIVE_STATS_SOURCE="summaries"`.

## 8. Tests (unit, `/tmp/lkvenv`)

* `cell_raster`: moved functions keep their existing tests green (move/re-point test imports only if needed);
  `block_cells` cases (inside segment, gap, no segment, leg fallback).
* `robot_class`: unknown fields, rounding, empty factsheet.
* `summarise_run` on fixture rows: totals equal segment totals; track preferred over legs; `empty` source; two frame
  groups; recoveries spread; per-map split.
* `run_cells_db`: encode/decode round trip, upsert replaces.
* `build_map` with a fake `reads` (owner plane fixture like the drive-stats tests): split of a chart between two
  versions re-groups the same cells (the key property); samples below min length dropped; newest-N cut; '*' row;
  percentile values; empty map; no scene graph.
* drive-stats: **equality test**: for fixture data, `summaries` and `raw` produce the same `tiles` and `regions`
  (float tolerance) when every run is summarised; mixed summarised / unsummarised runs counted once.
* Worker: election skip, grace, retry set, dirty map debounce (fake clock).
* Migration head pin in `test_navstack_deploy*`.
* Integration (optional, opt-in DSN like `test_track_map_frame_postgres.py`): the migration on TimescaleDB.

## 9. Delivery

* Commits on `stats/region-stats`, one per step: (1) `cell_raster` move, no behaviour change; (2) migration +
  `robot_class` + `run_cells_db` + `summarise_run` + worker + backfill; (3) `region_stats` build + route + loader;
  (4) drive-stats/series from summaries; (5) docs. Run the unit suite after each; report counts vs baseline.
* No merge, push or deploy. Report: what was done, test counts, measurements (6.6), open items.

## 10. Open items (record answers here)

* Map realign: stored map-frame cells shift. Proposal: record `frames` per row (done) and recompute affected runs from
  the track while it is retained; after that, transform cell centres. Not in this scope.
* Direction (heading bins per cell): not now; `algo_version` bump adds it later.
* Per-link portal delay: a later aggregate over cells near `Link.portal`, no new recording needed.
* Retire `edge_observations` / `/edge-stats` after the client moves to region stats.

## 11. Implementation notes (2026-10-28)

Where the code differs from, or fills in, the text above:

* `run_cells.run_id` has a foreign key to `mission_runs(run_id) ON DELETE CASCADE` (like `run_legs`): deleting a run or a
  mission deletes its summaries, and an insert for a vanished run fails instead of resurrecting it. `run_admin` only marks the
  maps' builds `stale` before the delete; `packages/migration/stores.py` needs nothing. `run_cells.source` has a CHECK.
* "No window" for the track read of one run is a time bound for chunk pruning only: `started_at - 60 s .. ended_at + 1 h`
  (`run_cells.ROW_WINDOW_*`); rows of a run lie inside.
* A track row belongs to the map of its session (`map_sessions.map_name`, else the run's map); a run therefore gets one row
  per map it has rows on (normally one). The raw drive-stats path still attributes all rows of a run to `mission_runs.map_id`.
* Blocks: `block_cells` / `leg_block_cells` return the cells of the events that found a place; an event no segment is within
  `max_gap_s` of is counted in `totals.blocks_unattributed`. Block-only cells are stored (blocks > 0, other figures 0) and are
  not tiles in drive-stats.
* Worker: `WriterElection` of `telemetry.py` is reused (lock `run_cells_worker`, `on_tick` = one pass every `RUN_CELLS_POLL_S`).
  Runs whose map is missing or `DELETING` are not selected. Dirty maps are held in memory (first change + debounce); maps
  whose newest build is not of the current graph version (`SceneGraphReads.topology`, one manifest read) or is `stale` are
  added every pass, so a restart or another worker's deletes are picked up. A map without a scene graph gets a build row
  with `graph_version = 0`, `status = 'no_scene_graph'`.
* `region_stats`: only the CURRENT graph version can be built or computed on read (the read side serves the owner raster of
  the current version only); another `version` that is not stored is a 404. `robot_class '*'` keeps the newest
  `REGION_STATS_MAX_RUNS` samples of the chart over all classes. `runs` = the samples kept. Route extras: `status`, `runs`,
  `robot_class`, `robot`, `source` (`stored` | `computed`).
* Drive-stats: the summaries part takes runs by `started_at` in the window (see `docs/drive_stats_2026-10-14.md`); `source`
  gained `basis`, `basis_reason`, `summary_runs`, `raw_runs`.

Measurements (6.6), read-only on the cloud box DB (342 runs, 25 k map-frame track rows; nothing written, no deploy; the
`run_cells` tables do not exist there, so the rows were computed in memory from the real rows):

* 275 of the 342 runs have cells; per row 77 cells median, 624 max; payload (int4 ix, iy, 4 x float4, int2 blocks)
  2.0 kB median, 16 kB max raw, 0.65 kB / 6.4 kB zlib level 3 (about what TOAST pglz gives): much smaller than the 2-5 k cells
  estimated, the runs are short.
* `summarise_rows` 0.4 ms median, 2.8 ms max per run; reading a run's rows 1.7 ms median, 12.5 ms max.
* `region_stats.aggregate` for the biggest map (office9, 135 runs, 1516 cells, a synthetic 16-chart raster): 2.7 ms. The real
  owner-raster read was not measured (it needs the topomap store); `build_map` is that plus one transaction of a few rows.
* drive-stats on office9: raw 124-170 ms DB + 19-24 ms compute (13.8 k rows, 1516 tiles); from summaries about 4 ms decode
  + 4 ms compute for the 135 runs (table read not measured: the table is not deployed). Tiles equal (1516 = 1516).

## 12. Changes for the planner region cost (2026-10-28, branch `stats/planner-region-cost`)

Decisions 2, 3 and 5 of `docs/planner_region_stats_2026-10-27.md` section 10. New migration `20261028_02_region_cost` (the
`20261028_01` file is unchanged); `RUN_CELLS_ALGO_VERSION` is 2.

* **Class key without speed.** `robot_class` = `<agv_class>|<L>x<W>` (section 4.3). A speed change no longer splits the history.
  The live robots: formidable-peacock `CARRIER|0.50x0.50` (speed_max 0.24); masked-frigatebird and subtle-pelican `CARRIER|?x?`
  (length = width = -1, speed_max 1.0). The 19 runs of masked-frigatebird are on Hospital and LOCAL, none on office7/office9.
* **Speed per run, with its basis.** `run_cells.speed_max` is the factsheet value at summary time and `run_cells.speed_basis`
  says how far it can be used: `factsheet`, `placeholder` (a speed_max equal to `ROBOT_PLACEHOLDER_SPEED_MAX` = 1.0 with no
  footprint, length and width both unknown) or `unknown` (missing, negative, zero). The rule is `robot_class.speed_basis`.
* **Factsheet at run time.** There is no factsheet history anywhere in the database (2026-10-27, checked): `robotobjectv1.status`
  and `robot_latest.state_msg` hold the latest only; `mission_runs` has `sw_version` but no factsheet figures; `fleet_events`,
  `robot_state_ts` and `audit_log` carry none (no `speed_max` / `factsheet` text in any of them); the retained MQTT factsheet is also
  the latest only. So every run is labelled with the factsheet current at summary time and `run_cells.class_basis = 'current'`
  (`'run'` is reserved for a source that does not exist yet; `summarise_rows(..., class_basis=)`). Runs summarised by the worker
  a minute after they end are as good as `'run'`; a backfill labels old runs with today's factsheet.
* **Normalised sums in `region_stats`.** Per row: `norm_runs`, `norm_len_m`, `norm_moving_m`, `norm_stopped_m`, over the kept
  samples whose run has `speed_basis = 'factsheet'`: metres, moving seconds x the run's own speed_max and stopped seconds x
  speed_max (so runs of different speeds add up). `norm_moving_m / norm_len_m` is 1 for a robot driving at its speed_max; the
  moving excess is that ratio minus 1. Placeholder and unknown-speed runs stay in the raw sums and out of the normalised ones.
* **Dwell (algo_version 2).** A track segment that stood still (`dist < min_speed * dt`) with its midpoint within
  `RUN_CELLS_DWELL_RADIUS_M` (0.5 m) of the run's start (its first track position), its goal (the last waypoint of the last route
  node of `mission_runs.mission_tree`) or an action node (the last waypoint of the route node before an action node of the tree)
  is `run_cells.dwell_s` (per cell) and not `stopped_s`; `time_s` still holds it: time = moving + stopped + dwell. Waypoints are
  in the map frame of their `map_id`; anchors of another map are ignored. The legs source has no positions in time, no dwell.
  `region_stats.dwell_s` sums it; `stopped_s` / `stopped_share` are transit stops only; moving time, `moving_speed_mps` and the
  moving pace are taken after dwell as well. drive-stats from the summaries adds the dwell back to `stopped` (its figures are
  unchanged). Runs whose summary is of an older algo_version have no current summary, so the worker and
  `scripts/backfill_run_cells.py` pick them up (the migration also deletes the algo 1 rows and the stats built from them).
* **`regions_of` pins one graph version.** The first 160 m chunk fixes the version, the chart-cell views are asked for it, and a
  chunk or view of another version restarts the read (`REGION_VERSION_TRIES` = 3), then status `error`.
* **Agreement test** `tests/unit/test_region_chart_agreement.py`: `regions_of` and `PlanGraph._cells` / `owner_window` give every
  cell the same chart on the lazy-plan fixture worlds.
* Deviations from the text above: `region_stats.moving_*` now subtract dwell; the class strings of section 4.3 changed; rows
  have the five new columns of the migration; `run_cells` the three (`speed_basis`, `class_basis`, `dwell_s`).

