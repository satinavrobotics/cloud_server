# Drive statistics per tile and region (2026-10-14)

`GET /api/v1/maps/{map_id}/drive-stats?from=&to=&robot=&min_speed=` -- implemented in `packages/api/drive_stats.py`
(route in `packages/api/main.py`, tests `tests/unit/test_drive_stats.py`, live cross-check
`scripts/analysis/drive_stats_live.py`). Response shape and the tile-index definition are in `docs/API_REFERENCE.md`
("Map Drive Statistics"); this note holds the rules, the limits and the numbers. Status: implemented on a branch,
not deployed. It builds on stage 1 of `docs/tracking_redesign_2026-10-13.md` (map-frame pose on `robot_track_ts` rows)
and is a read model only: nothing is written.

## What is counted

**Track runs.** Rows of `robot_track_ts` with a stored map pose (`x_map`, `y_map`) of the runs whose
`mission_runs.map_id` is the map. Rows are ordered per run; a pair of consecutive rows (same run, same
`map_session_id` and `run_epoch`) is one movement over the straight segment between them:

| rule | value | why |
|---|---|---|
| `dt <= 0` | rejected | clock disorder |
| `dt > 5 s` (`DRIVE_STATS_MAX_GAP_S`) | rejected | rows are 1.5 s apart at level `track`; a gap is a recording hole, not a straight drive |
| displacement / dt > 2 m/s (`DRIVE_STATS_MAX_SPEED_MS`) | rejected | pose jump / teleport (robots top out near 0.7 m/s) |
| session or run epoch changes | pair not formed | the frame may differ |
| displacement / dt < `min_speed` (default 0.05 m/s, `DRIVE_STATS_MIN_SPEED_MS`) | whole `dt` counted as `stopped_s` | at rest the localisation jitters by cm |

Rejected pairs are counted in `source.track_pairs_rejected` (not in any tile).

**Attribution to tiles.** A tile is a 0.1 m cell of the scene-graph owner raster (`cell_m = step = 0.1`; the lattice is
the same one the 5 cm occupancy cells halve). The segment is sampled every 2.5 cm (`cell/4`); each sample carries an equal
share of the segment's length, `dt`, recovery seconds and stopped seconds, and goes to the cell it lies in. This is an
approximation of exact cell-crossing lengths with an error below 2.5 cm per cell boundary; a cell clipped by less than
that can be missed. A zero-length segment (robot standing) goes entirely to its cell.

**Coverage `n`.** The number of distinct runs with at least one sample in the tile (a run passing a tile three
times counts once, so `n` is "how many independent drives", not passes).

**Recovery seconds.** `NAV.RECOVERY_EXITED` events of the track runs carry `duration_s`; the interval is
`ts - duration_s .. ts` (`NAV.RECOVERY_ENTERED` has only `cause` and `leg_seq`, no position). The seconds that the
interval shares with a segment go to that segment's cells (so `recovery_s <= time_s`). Recovery time in a hole of the track
(no segment overlaps) is lost for the tiles and reported as `source.recovery_unattributed_s`.

**Leg fallback.** A run on the map that has **no** stored map-frame track row anywhere (any time, not only in the
window) is counted from its `run_legs` instead: leaf node to leaf node straight line (positions from ArangoDB), `duration_s`
spread over the line, `stopped_s` and `recovery_s` as the leg recorded them. A leg is skipped (`source.legs_skipped`) when a
node has no position, its duration is not positive or the straight length exceeds 10 m (`DRIVE_STATS_LEG_MAX_M`). Legs
of a run that does have a track are dropped (`source.legs_dropped_run_has_track`), so no run is ever counted from both
sources. Legs without a `from` node (the first leg of a run, from the robot pose) cannot be placed and are not read.
Legs are coarse: a leg's time is spread evenly along the straight line, so tile speeds from legs are averages over the
leg. `source.tiles_track` / `tiles_leg_only` give the tiles known from tracks and the extra tiles only legs reach.

**Regions.** The owner raster of the current graph version (`SceneGraphReads.owner_plane`, in chunks of 160 m so a request
stays under the reads' 4M-cell cap, hitting the reads' own cache) is sampled at each tile; sums are per chart key, `n` is
the number of distinct runs through the chart, `cells` the chart's size from the scene-graph view, `visited` the number of
its tiles with drive data. Tiles on no chart are in no region. No engine is loaded; when the scene graph is off, has no
graph or a read fails, `regions` is `[]`, `source.scene_graph` is `none` / `error` and tiles use absolute indices.

## Limits and caching

* At most `DRIVE_STATS_MAX_ROWS` (2,000,000) track rows are read, newest first; more sets `source.partial` (no 413: a
  partial answer is more useful for a lens, and the flag is explicit). Legs are capped by the same number.
* SQL runs in the fleet read transaction (read only, `FLEET_READ_STATEMENT_TIMEOUT_MS` 5 s, then 503). The numpy work runs
  in a thread; the event loop is never blocked.
* Server cache: 16 entries, 120 s, per (map, from, to, robot, min_speed), single flight. The key does not contain the graph
  version (it is only known after the regions are read), so after a scene-graph rebuild an answer can be up to 120 s old.
  Clients revalidate with the `ETag` (a hash of the body) and `Cache-Control: private, max-age=60`.
* The body is compact JSON (arrays, rounded: length 3 decimals, seconds 2).

## Live cross-check (office7, 2026-10-14, read only)

`scripts/analysis/drive_stats_live.py` runs the real aggregation on the live rows (Postgres SELECTs through
`docker exec`, scene graph through the running API's GETs):

* inputs: 7,516 track rows with a map pose (48 runs, 2026-10-13 16:48 onward), 195 recovery events, 61 legs of 31 runs
  without a track (94 further legs dropped because their run has a track);
* 7,464 track segments (4 pairs rejected); 1,033 tiles: 939 from tracks, 94 only from legs (9.1 %);
* coverage `n`: max 29, median 4, mean 6.0; 634 tiles with `n >= 3`;
* totals: 880 m driven, 11,284 s, 1,066 s in recovery (3.1 s unattributed), 5,831 s stopped (the robots stand a lot:
  overall mean speed 0.078 m/s); tile speed (length / time) p5/25/50/75/95 = 0.030 / 0.078 / 0.136 / 0.185 / 0.232 m/s;
* 270 tiles with recovery time, 528 with stopped time;
* scene graph version 5, 9 charts with data (the busiest: 57 runs, 178 m, 120 s recovery), response 33 KB.

## Risks and open points

* **Tile speed from stopped time.** `len_m / time_s` includes standing and recovery time; the lenses should show
  `len_m / (time_s - stopped_s)` ("moving speed") separately from the time lost. Both are derivable from the fields.
* **Chart splits/merges.** Region figures are recomputed against the current graph version on every (uncached) read, so
  history survives splits and merges (decision D1 of the tracking redesign); tile figures never depend on the graph.
* **Frame errors.** Rows carry the map pose converted at record time; a wrong placement shows as drive data through
  walls. The frame witness unplaces such sessions; rows recorded before that are not removed.
* **Old runs.** Runs before 2026-10-13 16:48 have no map-frame rows and appear through their legs only (coarser).
* **Mapping drives** are not in the statistics (they are not mission runs, tracking redesign D4).
* **Memory:** 2 M rows are ~100 MB of Python tuples while loaded; the cap bounds it. A larger window needs paging.
* **Wide windows on busy maps:** the join `robot_track_ts` x `mission_runs` relies on the `ts` chunk exclusion; an
  unbounded window reads every chunk (compressed ones decompress). Clients should pass `from`.
