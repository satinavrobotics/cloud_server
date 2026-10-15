# Map stats time series (2026-10-15)

For the map window's chart cards. Opt-in `series` query parameter on four existing endpoints; without it the answers are
unchanged. Code: `packages/api/map_series.py` (run-stats, events-summary), `drive_stats.series_day`,
`mapping_stats.session_series`, axis helpers in `series_axis.py`.

## Rules

* Bucket = UTC day (`t` = ISO day starts). The window is the endpoint's own (default the last 30 days), so the first and
  last day are partial. More than 120 days: 422 (before any query). `series=run` exists only for run-stats; any other
  value is 422.
* Arrays are aligned with `t`. Counts and sums are 0 on a day without data; medians, rates and ratios are null.
* One grouped SQL query per block (`(ts AT TIME ZONE 'UTC')::date` group by) in the same read-only transaction as today;
  numpy and the efficiency legs run in a thread. Cached per (map, request window, robot, series): run-stats and
  events-summary series `EFFICIENCY_CACHE_TTL_S` (120 s), drive/mapping with their ETag cache (key includes `series`).
* A failure of the run-stats series leaves `series: null` plus a note in `data_quality.notes` (like `efficiency`).

## run-stats `?series=day`

`series: {bucket: "day", t, outcomes: {completed, failed, timeout, aborted, canceled}, success_rate, interventions, hours,
duration_median_s, recovery: {legs_with_recovery, median_s, share_of_time}, efficiency: {ratio, extra_pct}, safety:
{episodes}}`. A run belongs to the day it STARTED (outcomes, duration, recovery, efficiency, hours). Teleop interventions
and safety episodes belong to the day of the event. `interventions = canceled + teleop` (cancels + takeovers + teleop of the
headline). `hours`: the union-per-robot rule of the headline, each interval's new part credited to its run's start day, so
the series sums to the headline. `efficiency`: planned / driven and (driven / planned - 1) * 100 of the day's headline legs
(stored plan trajectory + usable track); a day needs only one such leg (the headline needs 3), so thin days are noisy.
Sums of outcomes, hours, interventions and safety equal the headline numbers.

## run-stats `?series=run`

Parallel arrays, the newest 60 finished runs, oldest first: `bucket: "run", t (run start), run_id, outcome (lower case),
duration_s, recovery_s (sum of the run's legs), efficiency_ratio (planned/driven, null without trajectory legs),
intervention (true for a CANCELED run or a teleop session started during the run)`.

## events-summary `?series=day`

`series: {bucket, t, total, safety, by_code: {<code>: [per day]}}`, same episodes and universe as the summary (sums equal
`total_episodes` and `safety.episodes`). `by_code` holds the 6 most frequent codes of the window (keyed like `top`).

## drive-stats `?series=day`

`series: {bucket, t, len_m, time_s, moving_speed_mps, recovery_share, stopped_share, new_tiles, cumulative_tiles, source,
window}`. From the stored map-frame tracks only (runs known only by their legs have no timestamps:
`source.leg_only_runs_not_in_series`). A movement belongs to the day it started. `moving_speed_mps` = length / time of the
movements at or above `min_speed`. `new_tiles` = 0.1 m cells visited that day and on no earlier day OF THE SERIES WINDOW
("first visited" is relative to the window, not all time, so the first day of a window counts every cell it touches);
`cumulative_tiles` is their running total. With explicit `from`/`to` the series window is that; without, the last 30
days (the tiles/regions of the same answer keep their own all-time window).

## mapping-stats `?series=session`

`series: {bucket: "session", t (session start), session_id, active_s, nodes, minutes_per_100m2}`, the newest 30 sessions,
oldest first. Per session (always, in `sessions[]`): `new_cells` = 0.1 m path cells first covered by that session
(sessions in start order), `area_added_m2 = mapped_area_m2 * new_cells / sum(new_cells)` and
`minutes_per_100m2 = active_s / area_added_m2 * 100 / 60`. This is an ESTIMATE: the path is a thin line, the area is the
occupancy's, so the split is by share of new path cells. Null without a mapped area (no occupancy) or a session that
covered no new cell.

## Live check (office7, 30 days, 2026-10-15, read only)

run-stats: 31 buckets, completed `[6, 11, 32, 29]` on 10-12..10-14 (sum 78 = headline), canceled 12, hours sum 4.422
(headline 4.422), success_rate 0.857..0.941, recovery share 0.02..0.13, efficiency one point (10-14: 1.004). events:
total sums to 623 and safety to 60 (= summary). drive: len_m 211.6 and 621.5 m, new tiles 676 and 294, cumulative 970.
Mapping-stats needs ArangoDB and was not run live.
