# Mapping time per square metre (2026-10-14)

`GET /api/v1/maps/{map_id}/mapping-stats?robot=` -- implemented in `packages/api/mapping_stats.py` (route in
`packages/api/main.py`, tests `tests/unit/test_mapping_stats.py`, live cross-check `scripts/analysis/mapping_stats_live.py`).
Response shape: `docs/API_REFERENCE.md` ("Map Mapping Statistics"). Read only; status: implemented on a branch, not
deployed. Question answered: "how much time did the MAPPING of this map take, per m^2, and where".

## Definitions

**Sessions.** The `map_sessions` rows of the map with `purpose = 'mapping'` (finished or open; operate sessions are not
mapping). Per session: `wall_s = ended_at - started_at` (an open session up to now), `paused_s` = the paused intervals,
`active_s = wall_s - paused_s`. Pauses come from the `MAP.SESSION_PAUSED` / `MAP.SESSION_RESUMED` events of the session
(`fleet_events`, `payload.session_id`); a pause without a resume lasts to the end of the session (a finish while paused); the
open pause `paused_at` is used when its event is missing; a resume without a pause and a repeated pause are ignored. The
table keeps only the current `paused_at`, so the events are the only history. `headline.active_s` is the sum over all
mapping sessions of the map, including sessions that laid down no nodes (a test of 45 s counts: it was mapping time).

**Mapped area.** `mapped_area_m2` = known cells of the current fused occupancy (`V` plane: free = 0 plus blocked = 100;
255 = unknown is not counted) x 0.05 m x 0.05 m, read tile by tile from the scene-graph store through
`OccupancyReads.known_area` (no engine load; cached per map, epoch and version with the occupancy indexes). `free_area_m2`
and `blocked_area_m2` split it. `scene_graph_area_m2` = the summed `area_m2` of the charts (planner free space with
clearance) over the occupancy extent, as the second figure; it is smaller.

**Rates.** `seconds_per_m2 = active_s / mapped_area_m2`, `minutes_per_100m2 = seconds_per_m2 x 100 / 60`; the same with
the scene-graph area (`*_scene_graph`). They are the whole map's, not per session, because the fused grid has no per-session
history: `sessions[].area_added_m2` is `null` (not derivable). Null when the area is unknown (occupancy off/empty).

**Nodes and images.** `nodes` = nodes ArangoDB holds for the sessions (`session_id`); nodes of a deleted session or legacy
nodes without a session id are not counted. `images` = the camera frames the nodes recorded (`camera_metadata` stamps), a
proxy for the stored images (a node whose image upload was dropped is over-counted). The recorded
`map_sessions.node_count` is shown as `node_count_recorded` for comparison.

## Spatial attribution

Same tile contract as drive-stats: 0.1 m cells, `ix, iy` relative to `origin` (multiple of 6.4 m; absolute with `[0,0]` without a
scene graph), sparse `tiles: [[ix, iy, seconds, nodes]]`. Per session ONE source:

* **Nodes (all sessions today).** The nodes of the session in laying-down order (capture time when every node has one,
  else the cloud receipt time of graph-builder: `packages/utils/session_trace.py`, `capture_time.py`; one clock per
  session). The time between two consecutive nodes is spread along the straight segment between them with the drive-stats
  sampler (each 2.5 cm sample takes an equal share). The seconds of the pair inside a paused interval are taken out first.
  Not attributed (counted in `sessions[].pairs`, the seconds in `gap_s`): a pair with a missing pose or time, `dt <= 0`
  (`reversed`), more than `MAPPING_STATS_MAX_GAP_S` (60 s) of unpaused time (`gap`: the robot stood or uploads stalled), a
  speed above 2 m/s (`jump`) or a segment longer than 10 m (`long`). The time before the first node and after the last is
  not attributed, so `attributed_s < active_s`; the difference is setup time, standing and gaps.
* **Track.** A session with `robot_track_ts` rows tagged `map_session_id` = the session and a stored map pose (`x_map`) uses
  those rows with the drive-stats rules (5 s gap, 2 m/s) and the same pause deduction; its nodes then only count as `nodes`
  per cell. Never both. Live there are none (mapping runs have no `run_id`; nothing records a mapping track yet), so this
  path is covered by tests only.
* `tiles[].nodes` = nodes whose pose is in the cell (all nodes, attributable or not). A cell may have nodes and 0 seconds.

**Regions.** Charts of the current graph version through `drive_stats.regions_of` (owner raster per cell): `seconds`, `nodes`,
`cells` (0.1 m cells of the chart), `area_m2 = cells x 0.01`, `seconds_per_m2 = seconds / area_m2`, `visited` (cells with
time). Looked up at read time, so figures survive chart splits and merges.

## Limits, cache, quality

* Cache: `result_cache.ResultCache` (16 entries, 120 s, single flight) per (map, robot); ETag + `Cache-Control: private,
  max-age=60`. Open sessions advance with `now` (`window.now`), so a cached answer is up to 120 s behind.
* Cost: one sessions query, one events query, one track query (bounded by the sessions' time span), one ArangoDB node query
  per session, one pass over the occupancy tiles (cached), chart views in 160 m chunks.
* `source`: `sessions_nodes`, `sessions_track`, `sessions_without_data` (no nodes, no track), `nodes_without_time`,
  `nodes_outside_session` (node time more than 60 s outside the session: a clock offset or a naive local-time stamp),
  `occupancy` / `scene_graph` / `charts_status` (`ok` | `none` | `unavailable` | `error`; a failure nulls only its own figures).
* **Clocks.** Capture times are UTC with an offset (the robot's clock; only differences matter). The fallback receipt time
  is graph-builder's naive `datetime.now().isoformat()`, read as local time of the API host
  (`docs/tracking_redesign_2026-10-13.md` notes the same); both services run in UTC containers today. A wrong offset shows as
  `nodes_outside_session > 0`. Receipt times also compress bursts (a backlog of uploads arrives together), which spreads
  time unevenly along the path.
* A session is mapping time while it is open and unpaused even when the robot stands still; that is by design (it is the
  operator's time); the gap rule only keeps long standstills out of the spatial figures.

## Live cross-check (2026-10-14, read only)

`scripts/analysis/mapping_stats_live.py` (Postgres SELECTs through `docker exec`, nodes/occupancy/charts through the running
API's GETs; the deployed API has no mapping-stats route yet):

| map | mapping sessions | wall | paused | active | attributed | nodes | mapped area | min / 100 m^2 | chart area | min / 100 m^2 (charts) |
|---|---|---|---|---|---|---|---|---|---|---|
| office7 | 3 (all finished) | 576.9 s | 4.9 s | 572.0 s (0.159 h) | 438.8 s | 105 | 67.99 m^2 | 14.0 | 23.41 m^2 | 40.7 |
| office8 | 1 | 411.9 s | 0 | 411.9 s (0.114 h) | 359.7 s | 98 | 58.91 m^2 | 11.7 | 21.43 m^2 | 32.0 |

office7: one real session (95 nodes, 483 s active, one 4.9 s pause) and two 45 s sessions (0 and 10 nodes). office8: one
session, no pause. All nodes carry robot capture times; 0 nodes without time, 0 outside the session, no gap/jump/long pairs.
389 (office7) / 428 (office8) cells carry time; 9 charts each have mapping time. Responses 9.5 KB / 8.9 KB. No open sessions
and no session without `ended_at` on these maps; no mapping session has track rows.
