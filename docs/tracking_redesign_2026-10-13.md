# Tracking, recording levels and statistics: redesign (2026-10-13)

Status: **design; stage 1 implemented on a branch (not deployed, see "Stage 1 status").** Decisions below were taken with the user on 2026-10-13. Facts about the current
state come from a read-only survey of `main` (dedfd54) and the scene-graph docs on `feat/scene-graph-v2`
(`docs/scene_graph/integration.md`, `planner_upgrade.md` section 13). Re-check a cited line before relying on it.

## Principle

Three tiers, no mixing:

1. **Raw movement** (`robot_track_ts`, 1 Hz): replay of a run. Not a source of statistics by itself.
2. **Leaf graph** (topomap nodes/edges, images): what a mission or mapping session laid down. Used for image
   visualization during a run only (`session_trace`, the mission replay). Never a key for statistics or planning.
3. **Statistics**: only between **high-level nodes** (scene-graph charts and the links between them). Planner edge
   weights, when they come, read these.

## Decisions

| # | Question | Decision |
|---|---|---|
| D1 | Attribution under chart split/merge | Store each traversal as entry/exit pose (map frame), time and distance. The chart pair is looked up **on read** against the current graph version (`locate`). History survives splits and merges; no lineage bookkeeping. |
| D2 | Frame problem | Convert **at record time**: the recorder writes map-frame x, y (and theta) beside the run-frame pose when the robot's placed session is known, null otherwise. Needs a direct run-to-session link (`map_sessions.run_epoch` exists, the track reader does not use it). Existing runs stay in the run frame. `track_transform` stays only as the fallback for old rows. |
| D3 | Levels | `events_only` (the default) also records cheap **chart transits**, one row per chart-to-chart traversal, computed after the run. `track` stays 1 Hz for replay; its retention drops from 1 year to about 90 days (aggregates survive). |
| D4 | Mapping-drive observations | Dropped from statistics (hand-driven speed is not how a mission drives). They stay in `session_trace` for the images. Removes the second writer of `edge_observations`. |

## Current state worth fixing (survey, unverified lines)

* No consumer of `edge_observations` / `GET /maps/{id}/edge-stats` exists outside the API route: changing the key is cheap.
* The same movement is stored four ways: `robot_track_ts`, `run_legs`, `edge_observations` (copy of `run_legs`), mapping
  node timestamps. `robot_state_ts` (full) and `mission_trajectory` carry two more overlapping pose series.
* Speed/time derivations are duplicated (`session_trace.step`, `edge_stats`, `run_legs`, the track's own speed,
  `capture_time`); `TRAJECTORY_GRACE` is defined in both `fleet_reads.py` and `fleet_recorder.py`.
* Track `ts` gate uses receive time, rows use robot time; mapping uses capture or ingest time (naive local time).
* A level that changes mid-run leaves unmarked gaps; at events_only `/track` is empty and indistinguishable from
  "not recorded" without reading the level.
* `edge_observations`: window of 20 mixes mapping and leg sources; not cleaned on map delete (check `map_delete.py`,
  171a5e3 claims a cleanup); crashed or orphaned runs write no observations until the backfill script runs.
* `run_legs` and `mission_runs` have no retention.

## Target

* **Recorder**: writes map-frame pose with the track row (D2). Run start records the session link (run epoch).
* **Transit job** (after run end, in the API or graph-builder, which can load the scene-graph read side): walks the
  run's map-frame poses, `locate`s the owner chart per pose, emits a transit when the chart changes: `{run, robot,
  map, t_in, t_out, in_xy, out_xy, distance, speed, recoveries, blocks, graph_version}`. At `events_only` there is no
  track, so the transit source there is the leg poses (the legs' own start/end in map frame) and the planned route's
  chart keys (`chart_keys` in the plan response): decide in stage 2 which feeds the job without a track; **open**.
* **Stats read**: group transits by (chart a, chart b) via `locate` at the current version, last N per
  (map, a, b, robot), same count/median/p90/speed/recoveries/blocks as `edge_stats`; roll-ups to parent charts on read.
* **One speed/time module**: `packages/utils/` function used by traces, legs, transits. Delete the duplicates.
* **Remove**: leaf-keyed `edge_observations` writers and the mapping writer; `mission_trajectory` if nothing reads it
  (verify); the duplicate grace constant. Keep `run_legs` for mission analysis (expected vs actual).
* **Markers**: write the level that applied to each leg/track segment (or a gap marker) so a mid-run level change is
  visible; make `/track` say `recorded: false` with the level instead of an empty list.

## Stages (each scoped, tested on the pins, deployed on its own)

0. **Prerequisite**: scene graph merged to `main` and deployed (`feat/scene-graph-v2`, other session). Nothing in
   stages 2-4 starts before `locate` and the read API are on `main`.
1. **Frame at record time** (D2) + level markers + `/track` "not recorded" flag. No scene-graph dependency; can go first.
2. **Transits** (D1, D3): table + job + read route `GET /maps/{id}/link-stats`; backfill from existing runs where the
   track is convertible.
3. **Retire leaf statistics** (D4): stop the mapping writer, route `/edge-stats` deprecated, then removed; migration to
   drop `edge_observations` after one release; client Stats tabs move to link statistics.
4. **Cleanup**: one speed/time module, retention (track 90 days; `run_legs` policy), duplicate constants, orphan rows.

## Stage 1 status (implemented, not deployed)

* Migration `20261013_01_track_map_frame`: `robot_track_ts` gains nullable `x_map`, `y_map`, `theta_map`,
  `map_session_id`, `run_epoch`; `mission_runs` gains `level_override` (bool, default false) and
  `level_marks` (jsonb). Verified on TimescaleDB 2.30.1 with a compressed chunk
  (`tests/integration/test_track_map_frame_postgres.py`, opt-in `TRACK_TEST_DSN`).
* Recorder: `MapFrame` cached per robot (`FleetRecorder.set_frame` / `invalidate_frame`); dispatch
  refreshes it from the frame check's 5 s session cache (`Robot._refresh_recorder_frame`) only while a
  mission runs and the track is recorded, and drops it on run change / frame change / pending header drop.
  Session and run epoch are on the row, not on `mission_runs`: a run can be re-placed or change session.
* Reader: `/track` prefers the stored map pose, falls back to `track_transform` only for rows without it,
  and returns `recorded`, `recording_level`, `level_marks` (and `unplaced_points`).
* Markers: dispatch appends `{ts, level}` to `mission_runs.level_marks` when a run's effective level
  changes; `level_segments` applies `level_override` to every segment. No new event code or table.
* `TRAJECTORY_GRACE` now derives from `config.RECORDER_TRAJECTORY_GRACE_S` in both places.
* Not done (as scoped): `track_transform` removal (old rows), transits, anything scene-graph.

## Client consequences

* Map window Stats: average speed from link statistics; corridor width and regions from the scene-graph read API.
* Mission window Stats tab: the trajectory and speed/time colouring stay (good as is); the laid-down leaf nodes only
  serve the image viewing; link colours come from transits once stage 2 is in.
* The run-frame limitation disappears for runs recorded after stage 1.

## Open

* Source of transits at `events_only` without a track (see Target).
* Whether `Chart.leaf` thumbnails replace the leaf-node images for the mission replay.
* Retention for transits (suggest none, bounded by the last-N window per link + a one-year age cap).
