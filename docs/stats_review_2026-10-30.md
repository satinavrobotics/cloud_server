# Run statistics and planner integration: review and next work (2026-10-30)

Status: **review only, nothing changed.** This is a read-only review of `cloud_server` `main` at `fa52067` (55 commits
ahead of `origin/main`) and `sati-client` `main`. It covers how statistics of previous runs are recorded, aggregated,
served and wired into the planner. **Planner integration stays disabled** (`REGION_COST_MODE=off`). Nothing in the work
plan below turns it on.

Sources:
* four review passes (ingestion, summaries and region stats, read side and client, planner);
* re-checks of the main findings against the code (marked ✔);
* read-only queries on the cloud box.

Confidence: **H** high, **M** medium, **L** low.

Related docs:
* `docs/region_stats_2026-10-27.md` (spec)
* `docs/planner_region_stats_2026-10-27.md` (planner design, §10 decisions)
* `docs/tracking_redesign_2026-10-13.md`
* `docs/drive_stats_2026-10-14.md`

---

## 0. Live state (cloud box, 2026-10-30, read-only)

| What | State |
|---|---|
| API container (`api_delegation_service:latest`, created 2026-10-28 17:03) | Has `run_cells` / `region_stats` / `REGION_COST_*` code. Alembic at `20261028_05_analysis_catalog`. |
| Mission planner container (created 2026-10-27 18:37) | **Predates the region-cost code.** Planning with stats is doubly off: no code, and mode off. |
| `run_cells` | 331 run summaries. `formidable-peacock`: 310 runs labelled `speed_max` 0.24 and 8 labelled 0.36, both `factsheet`. `masked-frigatebird`: 13 runs labelled `CARRIER\|?x?`, `speed_basis=placeholder`. |
| `region_stats_builds` | `ok` for Hospital v6, Officemap v6, office01 v8, office7 v87, office8 v8, office9 v216. No `no_scene_graph` rebuild loop today. |
| `region_stats` | 42 rows. The Hospital rows are class `CARRIER\|?x?` (placeholder factsheet) plus `*`. |
| `edge_observations` / `run_legs` | 1723 / 2499 rows. |
| Factsheets (`/api/v1/robots`) | `formidable-peacock`: speed_max 0.36, angular 0.54, 0.5×0.5, acceleration_max −1. `masked-frigatebird`, `subtle-pelican`, `robot`: placeholder values (speed_max 1.0, everything else −1). |

**Is "off" really off?** Yes (H, verified).
* `server._region_for` returns `(None, "off")` before it touches the snapshot cache.
* With `region=None` the search, Safety dropping, bounds, string pull, corridor code, cache keys and route digest are the same as before.
* `RouteTrajectoryLegV1.dict()` leaves out `predicted_s=None`.
* A misspelt mode becomes `off`, and an empty `REGION_COST_MAPS` list means no map.
* A bad `REGION_COST_TERMS` value is only parsed when the mode is not off, and then it falls back to off.

The one cost while off: `fleet_recorder.LEG_COLUMNS` always writes `predicted_s, cost_model`. That needs migration
`20261028_03` to be applied before mission-dispatch is deployed. The normal "API first" deploy order already guarantees this.

---

## 1. Findings

### 1.1 Correctness

| # | Where | Finding | Failure scenario | Conf. |
|---|---|---|---|---|
| C1 ✔ | `mission_planner/scene_route.py:1023` (`region_times`) | `predicted_s` adds `_turn_rad(pts)/omega` over the **dense lattice** (`times(lattice)`, `:1074`). An off-axis line on the 8-connected lattice is a staircase, so every step counts about 0.785 rad. The `moving` rho already contains turning slowness, and the replay that calibrated the model (`scripts/analysis/region_cost_replay.py:277`, `len*(1+rho)/v`) has no spin term. | 10 m at 22.5° on a 5 cm lattice gives about 78 rad, so about 78 s of fake turning at ω = 1 rad/s (about 145 s at the real 0.54) on top of about 28–40 s of driving. This affects shadow `predicted_s`, `predicted_free_s`, `alt_predicted_s` and the leg `predicted_s` stored in `run_legs`. | H |
| C2 ✔ | `scene_route.py:1340`, `_region_record` → `route_with(region=…)` | Shadow mode runs a **second full `graph.route`** inside the plan future, which is awaited with `plan_wait` (`server.py:~352`) under `SCENE_GRAPH_PLAN_BUDGET_S`. | On a large map the extra search pushes the plan past the budget (`plan_timeout` / `budget_exhausted`), so the legacy planner serves the route. Shadow then changes the served route, which breaks its contract. The orphaned thread also keeps a `_plan_pool` worker busy. | H |
| C3 ✔ | `migration/stores.py:533`, `api/robot_delete.py:57,77` | Only `run_admin.delete_runs` (`:253`) marks `region_stats_builds` stale. The import undo and robot delete remove `mission_runs` (and `run_cells` by cascade) but leave the `ok` build and its `region_stats` rows. `region_stats_2026-10-27.md` §11 says "stores.py needs nothing", which is wrong. | An undone import, or a deleted robot, keeps contributing to the statistics the planner would price with. | H |
| C4 | `api/region_stats.py:206,223-227` vs `run_admin.py:253` | `build_map` reads its inputs (`collect`) outside its write transaction and then upserts `status='ok'` without a condition. | The build collects run R; a delete marks the build stale and removes R; then the build commits `ok` with R inside. R stays in the stats until another run lands on that map. | H (code), M (likelihood) |
| C5 ✔ | `api/run_admin.py:234-256` | `delete_runs` does not delete from `edge_observations`. There is no foreign key: `source_key = '<run_id>:<seq>'`. | A deleted run stays in edge stats until it leaves the newest-20 window or reaches the 365-day retention. | H |
| C6 ✔ | `scripts/backfill_edge_observations.py:48-53` | `LEG_FIELDS` leaves out `moving_s`, `stopped_s` and `recovery_s`, which the live path (`fleet_recorder.py:221-224`) has. `LEGS_SQL` requires `l.map_id IS NOT NULL`, while the live path falls back to the run's map (`fleet_recorder.py:1641`). | The documented recovery after a failed live write (`api/edge_stats.py:306-308`, "run backfill --apply") inserts rows with NULL moving time unless you also pass `--fill-new-columns`. Legs with NULL `map_id` are never backfilled. | H |
| C7 ✔ | `api/drive_stats.py:111-113` (`regions_of`) | `owner_plane` returns None for a chunk with no raster (outside the graph extent), and that sends the **whole map** to `"none"`. | One run crosses a 160 m grid line that the graph does not reach. Drive-stats then has no regions, `build_map` stores `no_scene_graph` at v0, and `maps_needing_build` rebuilds the map on every poll forever. Not happening on today's data. | M-H |
| C8 | `utils/edge_stats.py:55,162-164` | Moving speed `distance/moving_s` has no plausibility check. Only the gross speed is checked. | A 2 m leg of 60 s with `moving_s` 0.1 gives a moving speed of 20 m/s. | M |
| C9 | `controllers/mission/fleet_recorder.py:1102-1106` (`leg_seq`) | Before the adopt op sets `leg_base` after a dispatcher restart, events are tagged seq 1, 2, … and collide with legs stored before the restart. | Recoveries and blocks get attributed to the wrong (old) legs during a short window after the restart. | L-M |

### 1.2 Inconsistencies

| # | Where | Finding | Conf. |
|---|---|---|---|
| I1 | client `utils/edgeStats.ts:30-42`, `utils/mapSeries.ts:27-33`; server region stats | **Three "average speed" numbers.** Overview: the count-weighted mean of edge `median_speed_mps` (waits included, newest 20 per edge, no time window, mapping steps mixed in). Driving: `len/(time−stopped_all)` over 30 days. Region stats: `moving_speed_mps` over 90 days or the newest 50 runs. The server already returns `moving_speed_mps` on edge stats, but the client `EdgeStat` type (`services/mapApi.ts:842-855`) drops it. | H |
| I2 | `api/drive_stats.py:303` vs `utils/run_cells_db.py:53` | `stopped_s` in drive-stats includes dwell (`stopped_all_s`). In region stats `stopped_s` and `stopped_share` cover transit stops only, with `dwell_s` kept apart. The same chart shows two different stopped shares. | H |
| I3 | `run_stats._SCOPE`, `path_efficiency._SCOPE_MAP` vs `drive_stats.fetch_track/fetch_legs`, `run_cells_db.fetch`, `edge_stats_db` | Archived runs are left out of run-stats and path efficiency but counted in drive-stats, run_cells, region stats (the planner's input) and edge stats. Archiving does not mark region builds stale. | H |
| I4 | `api/region_stats.py:112` | The `REGION_STATS_MIN_LEN_M` (1 m) sample gate drops exactly the obstructed passes. A robot that waits 120 s, drives 0.6 m and reroutes adds nothing to `blocks_per_run` or `stopped_share`, so the stats look better than they are where cost matters most. | H (code) |
| I5 | `api/region_stats.py:136` | `pace_*` and `stopped_share` count dwell in `time_s`, while `moving_speed_mps` subtracts it. The module docstring (line 15) describes moving pace as `time_s − stopped_s`, but the code also subtracts dwell. | H |
| I6 | `api/run_cells.py:282,376` | An `algo_version` bump (or the backfill) recomputes every run and **relabels** it with the robot's *current* factsheet (`robot_class`, `speed_max`, `speed_basis`). Live evidence: `formidable-peacock` is labelled 0.24 on 310 runs and 0.36 on 8. The next bump would relabel all 318 as 0.36 and undo the per-run speed normalisation (decision 2, P4). | H |
| I7 | `api/drive_stats.py:271-277` (`summaries_wanted`) | Only `min_speed` is compared with config. `run_cells.params` is written and never read. Changing `DRIVE_STATS_MAX_GAP_S`, `MAX_SPEED_MS` or `RUN_CELLS_DWELL_RADIUS_M` leaves old and new rules mixed with no recompute. | H |
| I8 | `api/region_stats.py:241-264` (`maps_needing_build`) | Builds never expire by age. An idle map keeps samples older than 90 days indefinitely, while the computed-on-read path applies the window. | H |
| I9 | `api/run_cells.py:61` vs `api/drive_stats.py:211` | The summariser assigns rows by `COALESCE(session map, run map)`, the raw path by `r.map_id`, and `_not_summarised` checks `(run_id, r.map_id)` only. A run whose rows are all in another map's session is counted on both maps. | M |
| I10 | `leg_tracker.py:233` vs `api/path_efficiency.py:78` | `EFFICIENCY_STALE_TOL_M` means "both endpoints within tolerance" when writing and "length within tolerance" when reading. `config.py:723` documents only the length meaning. | M |
| I11 | `leg_tracker.py:246-249`, `utils/run_legs.py:383-389` | `expected_s` switched from straight-line to polyline `planned_m` in stage B. The aggregate medians mix both eras, so the ratio shifts with no change in the data. | M |
| I12 | `api/map_series.py:23` vs `api/map_events.py:278` | Within one endpoint, the events series is cached 120 s and its headline 30 s. The run-stats headline is not cached, but its efficiency and series blocks are (120 s). Totals disagree for up to 90 s. | H |
| I13 | `api/run_stats.py` | `hours` drops runs over `RUN_STATS_MAX_RUN_S` (2 h), but the `duration_s` median and P95 include them. | M |
| I14 | client cards | The default windows differ for numbers shown side by side: run-stats, events and drive-stats (client) 30 d; drive-stats server default all-time; mapping-stats all-time; edge stats newest 20; region stats 90 d or 50 runs. No card states its window. | H |
| I15 | `utils/region_cost.py` (`LEVELS`, `Term.value`, `CostModel.rho`) | `LEVELS = ("class","map","pooled","prior")` is labelled best-first, but `pooled` is tried before `map`. Provenance and logs therefore rank `pooled` as worse than the level it is preferred over. | H |
| I16 | `region_snapshots` / `PlanGraph.region_why` | The neutral statuses `stamp_mismatch`, `version_mismatch` and `no_build_for_version` share one cache key (stamp None), so logs and stats show whichever was cached first. `region_why` checks `rho_max <= 0` before the version, so an all-zero snapshot of another version reads as `neutral` and gets `predicted_s`. Neither changes routes. | H (code), L (impact) |
| I17 | edge stats time base | `observed_at` is the robot header time for legs, `capture` (else server time) for mapping, while the purge compares with server `now()`. Clock skew reorders the newest-20 and shifts retention. | L |

### 1.3 Redundancies and dead code

| # | What | Conf. |
|---|---|---|
| R1 | **`edge_observations` / edge stats.** The only reader is `GET /maps/{id}/edge-stats`. Its only consumer is the Overview "Average speed" tile (`useSidebarData.ts:35` → `OverviewTab.tsx:25`), which shows the wrong speed (I1). The planner, `region_cost` and `region_stats` do not read it. It costs two writers (run end, mapping session), an hourly purge with a sequential scan, a full-history window query plus an Arango edge read per cache miss, two indexes, the backfill script, and C5, C6, C8, I17. Region stats spec §2 and §10 already list it for removal. Recommendation: **retire it** (decision D1). | H |
| R2 | Columns written and never read: `run_cells.params`, `frames`, `cell_m`, `class_basis`, `ended_at`. The `run_cells_map_class_idx` index is unused (no caller passes `robot_class`). `run_legs.predicted_s` and `cost_model` are not in `fleet_reads.LEG_COLUMNS:426-431`, so the shadow go/no-go (planner doc §7) needs ad-hoc SQL. `run_legs.from_chart`, `to_chart` and `graph_version` are only echoed. `predicted_s` is `float4` while its siblings are `float8`. | H |
| R3 | Endpoints nobody calls: `run-stats?series=run` (which still loads the whole efficiency window), and `GET /runs/{id}/legs?with_efficiency=true` (the client never passes the flag). `region-stats` is planner-facing, which is fine. | H |
| R4 | Region-cost code nothing uses in production: `RegionPricing.rho_of` (O(N) `keys.index`), `RegionPricing.levels`, `Term.kind`, `Contribution.n_eff`, and a second `MODES` check in `mode_for`. The speed-basis check runs in both `RegionSnapshots.get` and `build_snapshot`. `weights.REGION_RHO_SCALE` duplicates `REGION_COST_QUANTUM` (every caller passes `region.scale`). The term composer (registry, parser, weights, per-term hash) serves one live term; about 60 lines could go. | H |
| R5 | The map default for a leg is applied in three places (`leg_tracker.py:222`, `fleet_recorder.py:585`, `:1641`), with questionable precedence (the run map beats the waypoint map). | M |
| R6 | `leg_tracker.observe` rebuilds `route_table(mission)` on every node, twice for the order-echo branch (`:186,:194`). | L |

### 1.4 Performance

| # | Where | Finding | Conf. |
|---|---|---|---|
| P1 ✔ | client `services/mapApi.ts:1658` | `from = new Date(now − 30d).toISOString()` has millisecond precision, so the drive-stats cache key and the ETag are new on every request. The server cache and 304s never hit. | H |
| P2 ✔ | `api/drive_stats.py:524-527,562` | The client never sends `to`, so `s_end = now` ≠ `end = None`, and the series path calls `load()` a second time (up to `DRIVE_STATS_MAX_ROWS` = 2M track rows plus legs). | H |
| P3 | `api/map_series.py:59-60,147`, `main.py:654-656` | `run-stats?series=day` runs `path_efficiency.TRACK_SQL` twice (`attach`, then `_efficiency` → `load`, bypassing `path_efficiency.CACHE`). It is the heaviest query here: `lag()` over every track row in the window plus a correlated subquery per segment (`path_efficiency.py:209-231`). Both the Health and Driving tabs request it. | H |
| P4 | migrations | Missing indexes: `run_legs (map_id, started_at DESC)` (`fetch_legs` filters by map), `robot_track_ts (run_id, ts)` (path efficiency joins by run), and `edge_observations (observed_at)` (the hourly `PURGE_SQL` scans the whole table and finds 0 rows) if R1 is not done. `drive_stats.runs_with_track` (`:246-252`) has no ts bound, so it scans the whole hypertable. | M (no EXPLAIN run) |
| P5 | `utils/edge_stats_db.py:233-241` | The windowed edge read runs `row_number()` over the map's full history; the index cannot order the pooled `(from,to)` partition, and the `COLLATE "C"` tie keys force a sort. It grows linearly with the 365-day retention. Moot if R1. | H |
| P6 | `run_cells_db.fetch`, `region_stats.collect`, `run_cells.PENDING_SQL`, `maps_needing_build` | Unbounded reads: summaries with no window when there is no `from`; the full 90-day window read although only 50 per chart and class are kept; an anti-join over all finished runs every 30 s; a `DISTINCT map_id` scan plus one topology read per map on every poll. | M (grows with history) |
| P7 | `api/drive_stats.py` (`series_day`, raw path) | `for i in range(n): np.nonzero(ok & (idx == i))` is O(days × segments), up to 120 × 2M. Use one `bincount`. | M |
| P8 | `region_snapshots.get` (shadow/on only) | A load timeout or error is not cached. While Postgres is slow, every plan waits `LOAD_TIMEOUT_S` = 2.0 s out of a 3 s budget and logs a warning. | H |
| P9 | `PlanGraph.region_arg` / `region_why` (shadow/on only) | Every `route()` call copies rho to int64 and compares `tuple(keys)` (O(N) strings), once per leg in enrich and corridor code. `_latest` in the snapshot cache is never evicted. | M |
| P10 | `_region_record` (shadow/on only) | `alt_route_changed` compares exact node lists, so tie-break noise inflates `region_shadow_changed`. | M |
| P11 | mode `on` (future) | With `rho_cap = 3` and `rho_min ≈ 0`, the admissible ellipse can be up to 4× longer, which means more tiles, `CorridorTooLarge`, and `exact=False`. Measure tile counts in shadow before ever turning it on. Admissibility itself holds. | M |
| P12 | `scripts/backfill_edge_observations.py:108-114`, `scripts/backfill_run_cells.py:113-115` | The edge backfill inserts everything in one `unnest` statement (unbounded). The run_cells backfill commits per row, not per run (a multi-map run can end up half written), and re-selects runs that have no rows on every pass. | L |
| P13 | client `useCachedMapStat` (`hooks/mapWindow/…`, line ~37) | A failed fetch stores `null` for the hook's lifetime, so one 503 hides the stat until the map is reopened. Region stats in computed mode put `built_at=utcnow()` in the body, so the ETag changes on every TTL. | M |

Verified correct, no action needed:
* Snapshot keying `(map, version, class, stamp, model_hash)`; the stamp is re-read every 30 s, and a build-row mismatch is refused (bb4a1f9).
* time = moving + stopped + dwell; dwell cells are a subset of the movement cells; drive-stats adds dwell back.
* The graph version is pinned across chunks (with retries); map delete removes all three tables.
* Speed normalisation uses only `speed_basis = factsheet` runs.
* `leg_tracker` copies `predicted_s` only when `plan_source = trajectory`.
* Admissibility of `_geo_bound` with the region term on.

---

## 2. Decisions needed before the work starts

| ID | Question | Recommendation | Affects |
|---|---|---|---|
| D1 | Retire `edge_observations` / edge stats? | **Yes.** Move the Overview tile to drive-stats moving speed (30 d, the same window as the cards next to it), then stop the writers, drop the route, the code and the table. | WP-F, WP-A, WP-G |
| D2 | Should archived runs count in drive-stats, run_cells-based stats and region stats? | **No** (exclude them everywhere, as run-stats already does). Archive and restore mark region builds stale. | WP-B, WP-C, WP-D, WP-E |
| D3 | Turn time in `predicted_s` | **Drop the spin term** (the moving rho already contains turning, and the replay has none). Alternative: count it on the pulled waypoint polyline and add it to the replay. | WP-E |
| D4 | Region sample gate | Count a (run, chart) as a sample when `len ≥ 1 m` **or** `blocks > 0` **or** `stopped+recovery ≥ 10 s`; keep the length gate for pace percentiles only. This changes the stats, so it gets a `region_stats` algorithm marker. | WP-C |
| D5 | Relabelling on recompute | **Carry over** `robot_class`, `speed_max` and `speed_basis` from the existing row. A new `--relabel` flag is the only way to change them. | WP-D |

Until D1 is decided, WP-F runs in "fix" mode instead of "retire" mode (see its two variants).

---

## 3. Work plan: parallel Sonnet subagents

Rules for every package:
* **File ownership is exclusive.** A package only edits the files listed under "Owns". If it needs a change elsewhere, it writes it under "handoff" in its report instead.
* Each package works on its own branch from `main` (`stats/wp-<x>`), in a git worktree (`isolation: worktree`), with **one commit per finding**. Commit messages follow the repo style and carry the attribution lines.
* **Region cost stays off.** No change to the `REGION_COST_MODE` default, `REGION_COST_MAPS`, or any env profile.
* **No deploy, no push.** Merge and deploy are the user's call; when they come, the API container deploys first because it runs alembic.
* **Tests:** unit tests next to the existing ones (`./scripts/run_unit_tests.sh`; for a single file use `pytest tests/unit/<file> -v`). Use the local venv with pydantic 1.10. Pydantic v1 idioms only.
* Client package: `npm run test:unit` and `npm run lint` must pass, and coverage thresholds hold (95 % `utils/`, 85 % `services/`).
* **No new migration outside WP-G.** A package that needs one describes it in its report and WP-G writes it, because the alembic chain is linear.
* **Report:** the commits, test output, a handoff list, and anything left undone (with the reason).

### Wave 1 (all in parallel)

#### WP-A: Client stats (sati-client)
Owns: `sati-client/services/mapApi.ts`, `utils/edgeStats.ts`, `utils/mapSeries.ts`, `hooks/mapWindow/useCachedMapStat*`,
`hooks/mapWindow/useEdgeStats.ts`, `components/**/OverviewTab.tsx`, and the stats cards that show a window, plus their tests.
1. P1: round the default `from` down to the UTC day (`getDriveStats`, and every other stats call that builds a default `from`). Also send `to` (end of the current UTC hour) so the server window is stable.
2. I1 / D1: the Overview "Average speed" reads drive-stats moving speed over the 30-day window. If D1 is "keep", read `moving_speed_mps` from edge stats instead and add it to `EdgeStat`.
3. I14: show the window on each stats card ("last 30 days", "newest 50 runs / 90 d").
4. P13: `useCachedMapStat` does not cache a failed fetch (retry on the next mount or focus).

#### WP-B: Read-side endpoints and drive-stats (server)
Owns: `packages/api/drive_stats.py`, `map_series.py`, `map_events.py`, `run_stats.py`, `path_efficiency.py`,
`result_cache.py`, `fleet_reads.py`, the route wiring for those in `main.py`, and their tests.
1. P2: compare resolved windows (`end=None` → the same `now` for both), so the series reuses `loaded`.
2. P3: `map_series._efficiency` goes through `path_efficiency.CACHE` (same key) or derives from the cached per-leg records. Remove `series=run` (R3) or make it read only the 60 runs.
3. C7: in `regions_of`, a None plane after the version is known is all-zero ids, and the loop continues. Return `"none"` only when there is no graph at all.
4. I7: `summaries_wanted` compares the stored `params` with `_params_json(Params())`; a mismatch falls back to the raw path and says so in `sources`.
5. I9: `_not_summarised` checks for any `run_cells` row of the run, not `(run_id, r.map_id)`.
6. D2 / I3: exclude archived runs in `fetch_track`, `fetch_legs` and `runs_with_track`.
7. P4 (query side): add a ts bound to `runs_with_track`. The index itself is WP-G.
8. P7: vectorise `series_day` (one `bincount` per field).
9. I12: one TTL per endpoint for all its blocks. Move `map_events` cache keys to put `map_id` first (the `result_cache.discard_map` contract).
10. I13: apply the `RUN_STATS_MAX_RUN_S` cap to the duration percentiles (or report the excluded count).
11. R2: add `predicted_s` and `cost_model` to `fleet_reads.LEG_COLUMNS`. R3: remove `with_efficiency` from the legs route, or leave it documented as unused (report which).
12. I2: keep the drive-stats region `stopped_s` name (all stops, as now), but add `dwell_s` and `transit_stopped_s` so it lines up with region stats. Update `docs/API_REFERENCE.md` for every shape change and list them in the handoff for WP-A.

#### WP-C: Region stats builder (server)
Owns: `packages/api/region_stats.py`, and its tests.
1. C4: the build re-reads its input stamp (`max(created_at)` and count of the window's `run_cells`, or a per-map generation counter) inside the write transaction, and does not write `ok` if it changed (it retries next poll). Coordinate the counter column with WP-G.
2. I8: `maps_needing_build` also rebuilds when `built_at` is older than `REGION_STATS_MAX_AGE_S` (new config, default 1 day) or the oldest kept input has left the window.
3. D4 / I4: the new sample gate; the pace percentiles keep the length gate.
4. I5: add a dwell-free `moving_pace` (or rename `pace_*` to say it is gross) and fix the docstring.
5. D2 / I3: `collect` excludes archived runs (via the WP-D `fetch` flag; until it lands, a local join).
6. P6: `collect` keeps only the newest `REGION_STATS_MAX_RUNS` per chart and class in SQL where possible; cache the current graph version per map between polls.

#### WP-D: Run summaries and deletion integrity (server)
Owns: `packages/api/run_cells.py`, `packages/utils/run_cells_db.py`, `scripts/backfill_run_cells.py`,
`packages/api/run_admin.py`, `packages/api/robot_delete.py`, `packages/migration/stores.py`, a new
`packages/utils/region_builds.py`, and their tests.
1. C3 / C4 / I3: `region_builds.mark_stale(cur, *, run_ids=None, robot_name=None, map_id=None)`, one shared helper (it also bumps the generation counter from WP-C/WP-G if adopted). Call it from `delete_runs`, `archive_runs` and restore, the `stores.py` import undo, and robot delete, before the rows go. Correct `region_stats_2026-10-27.md` §11.
2. C5: `delete_runs` deletes the runs' `edge_observations` (`source='leg' AND split_part(source_key, ':', 1) = ANY(run_ids)`), unless D1 retired the table first.
3. Stats caches: call `result_cache.discard_map` for the affected maps from map delete, run delete, archive and restore. `map_delete.py` is shared with WP-F; WP-D owns it for this one call.
4. D5 / I6: on recompute, keep `robot_class`, `speed_max` and `speed_basis` from the existing row; add a `--relabel` backfill flag.
5. D2: add an `exclude_archived` flag (default True) to `run_cells_db.fetch`.
6. P6: `PENDING_SQL` uses a high-water mark on `ended_at` plus a periodic full sweep, instead of an anti-join over all runs every 30 s.
7. P12: the backfill commits per run, and marks a run with no rows as done (an empty marker, or skip with a counted reason).
8. Small edge cases: a pending run whose only map was deleted becomes terminal, not retried every 600 s; in a multi-map run only the run's first position is a start dwell anchor.
9. R2: `params` is now read by WP-B; drop `frames`, `class_basis` and `ended_at` from the writer only if WP-G drops the columns (report the decision; do not drop data this wave).

#### WP-E: Planner region cost (stays off)
Owns: `packages/utils/region_cost.py`, `packages/utils/region_stats_load.py`, `packages/scene_graph/weights.py`,
`packages/services/mission_planner/{region_snapshots,scene_route,lazy_plan,server,node_policy_plan}.py`,
`scripts/analysis/region_cost_replay.py`, and their tests. **Mode off must stay byte-identical**: add a test that plans the
same request with `region=None` before and after, and compares the route digest.
1. C1 / D3: drop the spin term from `region_times`. If the alternative is chosen, compute it on the pulled waypoint polyline and add the same term to the replay.
2. C2: run the shadow `alt` route as a separate fire-and-forget pool task with its own short timeout, after the served plan returns, or skip it when less than half the budget is left. Record `alt_skipped` when skipped.
3. P8: cache a `load_error` neutral snapshot for 5–10 s under the short key; set `LOAD_TIMEOUT_S` to about 0.3 s or derive it from the remaining stage budget.
4. I16: put the status in the neutral cache key (or do not cache neutrals under the full key); `region_why` checks version and keys before `rho_max`.
5. I15: reorder `LEVELS` to class, pooled, map, prior.
6. R4: remove `rho_of`, `levels`, `Term.kind`, `n_eff`, the duplicate `MODES` check, the duplicate speed-basis check and `weights.REGION_RHO_SCALE` (`CostModel.scale` is the only source). Keep the term composer, but collapse it if it stays a one-term model after review; report the line delta.
7. P9: memoise `region_arg` on the snapshot identity; compare `region.keys is graph.keys` first; evict `_latest` with the LRU.
8. P10: `alt_route_changed` uses a threshold (`alt_extra_m > 0.05 m` or a cost delta > 0.5 %), with the raw comparison kept as `alt_nodes_differ`.
9. P11: add tile-count and `exact` fields to the shadow log line so shadow can measure the ellipse growth later.

#### WP-F: Ingestion, legs and edge stats
Owns: `packages/controllers/mission/leg_tracker.py`, `fleet_recorder.py` (stats parts), `packages/utils/run_legs.py`,
`plan_length.py`, `edge_stats.py`, `edge_stats_db.py`, `packages/api/edge_stats.py`, `scripts/backfill_edge_observations.py`,
the `edge_observations` delete line in `packages/api/map_delete.py`, and their tests.

Always:
1. C9: `leg_seq()` returns None while `not run.resolved` (events stay untagged instead of tagging old legs).
2. R5: one map rule for a leg, `waypoint.map_id or run.map_id`, in one helper used by all three places.
3. I10: one stale-plan helper in `plan_length.py` (the endpoint rule) used by `leg_tracker`. The `path_efficiency.py:78` call site changes in wave 2 (WP-B owns that file).
4. I11: `run_legs.aggregate` takes `expected_s` medians only from `plan_source IS NOT NULL` legs, or reports the era split.
5. R6: cache `route_table` per `(order_rev, route_rev)`.

Variant "retire" (D1 = yes, after WP-A ships the new tile):
* stop the run-end and mapping-session writers and the purge loop;
* remove the `edge-stats` route, `edge_stats*.py`, the backfill script and the related config;
* WP-G drops the table in wave 2;
* update `docs/API_REFERENCE.md` and the region stats spec.

Variant "fix" (D1 = no):
* C6: copy `moving_s`, `stopped_s` and `recovery_s` in the backfill; use `COALESCE(l.map_id, r.map_id)`; retire `--fill-new-columns`; chunk the insert (P12).
* C8: reject a moving speed above `EDGE_STATS_MAX_SPEED_MPS`, and set `moving_s = None` when silent plus stopped time covers nearly the whole leg.
* P5: replace the windowed read with `DISTINCT (from,to)` + `LATERAL (… ORDER BY observed_at DESC LIMIT window)`.
* I17: order and retain by a single server-side time column.

#### WP-G: Migrations (one owner for the alembic chain)
Owns: `packages/api/migrations/versions/*` (new files only).
1. P4: index `run_legs (map_id, started_at DESC)`; index `robot_track_ts (run_id, ts)`. It is a TimescaleDB hypertable, so check whether the chunks are compressed and use `CREATE INDEX … WITH (timescaledb.transaction_per_chunk)` if supported. If D1 = keep, add BRIN `edge_observations (observed_at)`.
2. R2: drop `run_cells_map_class_idx`; change `run_legs.predicted_s` to `float8`.
3. C4: the `region_stats_builds.generation` column (or a separate per-map counter table) if WP-C and WP-D adopt it. Agree on the name in the WP-C/WP-D handoffs before wave 1 starts. Default proposal: `region_stats_builds.inputs_gen bigint NOT NULL DEFAULT 0` plus `region_stats_inputs (map_id text PRIMARY KEY, gen bigint)`.
4. One migration per item, chained from `20261028_05_analysis_catalog`, each with a working `downgrade`. Test upgrade and downgrade against a scratch Postgres plus TimescaleDB container, not the live DB.

### Wave 2 (after wave 1 merges)
* WP-B2: `path_efficiency.py:78` uses the `plan_length` stale helper (I10).
* WP-F2 / WP-G2 (D1 = retire): drop `edge_observations`, after the writers have been off for one deploy.
* WP-H (one agent, read-only): check the whole result end to end on a scratch DB built from a dump of the live one.
  * `region_stats` before and after (D4, I5, I8);
  * drive-stats cache hit rate;
  * EXPLAIN of the indexed queries;
  * the mode-off route-digest test;
  * the shadow-mode plan latency on the largest map with `REGION_COST_MODE=shadow` in a test container only.

### Not in this plan (deliberately)
* Turning region cost on, or shadow on the live planner, which waits for the robot factsheets (section 4), the planner deploy and shadow evidence (planner doc P5).
* k alternatives or a variance term (planner doc §6).
* Chart-transit stats (tracking redesign stage 2).

---

## 4. Robot-side dependency

No robot code change is needed. The statistics depend on the VDA5050 **factsheet** and the **`driving`** flag in state messages:
* Three robots publish a placeholder factsheet. Their runs cannot be speed-normalised and form a separate `?x?` class (Hospital region stats today).
* `formidable-peacock` changed `speed_max` 0.24 → 0.36 with no record of when.
* `acceleration_max` is −1 on every robot.

The message to the robot team is in the conversation, not in this file.

### 4.1 Robot team answers (2026-10-30)

* **`driving`** comes from measured wheel odometry, not from navigation state. It is true when |v| ≥ 0.02 m/s or |ω| ≥ 0.05 rad/s, and stays true for 0.5 s after the last such sample. It is false while the wheels are still: waiting at a goal or action, paused, blocked, or waiting for an order. It is true during recovery (spin-in-place included), teleop and hand-moved motion. If odometry is older than 0.5 s, it falls back to the older event-based value.
  * This matches the `leg_tracker` model: `stopped_s` is wheels still, and recovery is inside `moving_s`, which is why we don't subtract both.
  * Each stop is undercounted by up to the 0.5 s hold.
  * Teleop during a mission counts as moving.
* **State rate:** one message per `update_feedback_period` (0.5 s on some robots, 1.0 s by default), plus event messages. The robot never merges messages, `headerId` goes up by 1 per publish, and MQTT is QoS 0, so drops are possible. Expected accuracy of moving time: about the state period plus 0.5 s.
* **Factsheets:** the robot team will send real `speedMax`, `angularSpeedMax`, `accelerationMax`, `length` and `width` for `masked-frigatebird`, `subtle-pelican` and `robot`. They will also say whether `robot` is a test identity, give the date of `formidable-peacock`'s 0.24 → 0.36 change and whether 0.36 is the nav limit, and tell us about every future change of these fields. **Open: waiting for their values.**

Follow-ups this creates (our side, wave 2):

* **W2-1 (leg_tracker, after WP-F merges):** use `headerId` gaps to detect dropped state messages. Today an interval of up to `MAX_INTERVAL_S` = 30 s is credited to the previous message's `driving` value. With a 0.5–1 s period, a gap of more than about 3 periods (or a jump in `headerId`) should count as `silent_s`, not as stopped or moving. This needs the header id to reach `LegTracker` (check what `observe` receives).
* **W2-2:** the server itself coalesces state messages (`controllers/mission/server.py:5008`, `send_message` keeps only the newest unprocessed state). So "never merged" holds on the robot but not on our side. It happened once in the last 72 h on the cloud box, so the impact is low. With W2-1, a coalesced message shows up as a `headerId` gap, and it is counted as silent instead of extrapolated.
* **W2-3:** when the factsheets arrive, mark the region builds of the affected maps stale and re-label those robots' runs. D5 keeps labels by default, so use the backfill `--relabel` for those robots only. If `formidable-peacock`'s 0.36 date is known, re-label its runs after that date as 0.36 and before it as 0.24, and set `class_basis` to `history` for them.

---

## 5. Implementation status (2026-10-30)

Wave 1 is implemented with D1–D5 as recommended. Nothing is pushed or deployed.

* **Server:** integration branch `stats/wave1` (worktree `/home/satiadmin/satinavrobotics/wt/cs-wave1`). It merges `stats/wp-g`, `wp-d`, `wp-c`, `wp-f`, `wp-b` and `wp-e`, plus two fix-up commits: the cache discard list, and three tests that the other branches' changes had made out of date.
  * Unit suite: 67 failed, 7775 passed, 40 errors. The failing set is **identical** to `main` at `fa52067` (67 failed, 7778 passed, 40 errors): pydantic 2 / fastapi env problems, missing `alembic`/`jsonschema`, scene-worker collection errors.
  * Migrations `20261030_01..04` pass upgrade, downgrade and upgrade on a scratch TimescaleDB (same image as live).
* **Client:** branch `stats/wp-a` (worktree `/home/satiadmin/satinavrobotics/wt/client-wp-a`), 5 commits from sati-client `main`.
  * Jest: 6614 passed, 2 failed (`behaviorTreeLiveStatus` reads `deployment_ws/…drive_legs.xml`, which is not on this machine).
  * Coverage thresholds hold.
* **Must ship together:** the server removes `GET /maps/{id}/edge-stats`, and the client no longer calls it.
* **Deploy notes:** deploy api-delegation-service first (it runs alembic, and mission-dispatch needs the migrations).
  * The `robot_track_ts (run_id, ts)` index builds per chunk in autocommit. Check the number of uncompressed rows first (docstring of `20261030_02`).
  * The first region-stats poll after deploy rebuilds every map (builder version 2, new stamps).
  * The planner gets the length-only `predicted_s` and the out-of-band shadow job. The mode is still off.
* **Changed meanings:**
  * Region `stopped_share` = `stopped_s / (time_s − dwell_s)`.
  * The run-stats duration median and P95 skip runs over the 2 h cap.
  * The default windows are rounded (`to` = next UTC hour, `from` = UTC day 30 days before).
  * The planner response's `pricing.region` alt fields moved to the `Region cost alt` log line and `get_stats()["scene_graph"]["region_alt"]`.
* **Open, for the owner:** WP-D changed the start-dwell anchor for multi-map runs without bumping `RUN_CELLS_ALGO_VERSION`, so old multi-map rows keep the old rule until they are recomputed.

Wave 2 (not started):
* WP-B2: `path_efficiency.py:78` → `plan_length.plan_of`, and fix the `EFFICIENCY_STALE_TOL_M` comment.
* WP-G2: drop `edge_observations` and its deletes after one deploy with the writers off.
* W2-1..3 (§4.1).
* WP-H end-to-end check on a dump.
* Add a "retired" note to the edge-stats design docs (`link_cost_estimation`, `scene_graph/edge_stats_on_charts_design.md`).
