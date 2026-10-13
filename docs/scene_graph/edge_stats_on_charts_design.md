# Design note (DRAFT): edge statistics on the chart graph (planner_upgrade.md section 13)

Status: draft, no code changed, untracked. Written 2026-10-13 from a read-only pass over
`/home/satiadmin/satinavrobotics/cloud_server_scene_graph` (branch `feat/scene-graph-v2`) and
`web_stack/cloud_server` (`main`, same statistics files present). "Verified" = read in code in this pass;
"estimate" / "unverified" are marked. Line numbers are of the worktree at the time of writing.

**Most important finding:** a second design already exists and is partly implemented:
`docs/tracking_redesign_2026-10-13.md` (decisions D1-D4 "taken with the user on 2026-10-13", stage 1 implemented on a
branch, stages 2-4 open). It already moves statistics to chart pairs and chooses "store entry/exit pose, look up
chart pair on read via `locate`, no lineage bookkeeping" (D1). This note therefore does not start from zero: section 4
evaluates D1 against the alternatives named in section 13 and section 5 maps the work onto the stages of that doc.

---------------------------------------------------------------------------------------------------------------

## 1. Current data flow (verified)

```
mission_planner  (packages/services/mission_planner/)
  scene_route.py:~790-805   pts = simplify(lattice route) -> ids = [node_of(x,y)]   (leaf snap, SCENE_GRAPH_LEAF_SNAP_M)
                            repair_leaf_ids (:99) -> collapse_repeated_leaf_ids (:135)
                            build_waypoints(pts, map_id, node_of, allow_repeats=True) (:72)
                              waypoints[i] = Pose2D(x, y, theta, map_id, node_id=leaf_i)
                              planned_path = [leaf_0 .. leaf_n]   (None if ANY waypoint has no leaf)
                            None -> raise ChartFallback("waypoints_without_distinct_leaf_ids") (:802) -> legacy planner
                            ChartPlan(waypoints, planned_path, charts=route.charts, repeated_leaf_ids=...) (:212)
  server.py:307-320         response: path = planned_path, start/end_node_id = ids[0]/ids[-1],
                            waypoints, chart_keys = plan.charts, min_width, graph_version, planner
        |
        v  (dispatch)
mission-dispatch (packages/controllers/mission/server.py)
  :993        mission.planned_path = plan.get("planned_path")      (replan of a go-to from the robot's pose)
  :1832       mission.planned_path = message.planned_path          (reroute applied; reroute from API clears it, main.py:2691-2696)
  :3793-3822  _resolve_node_ref: graph_node_id = waypoint.node_id, else planned_path[waypoint_index] only when the mission
              has exactly ONE route node  -> blocked_graph_nodes row (utils/blocked_nodes.py)
  fleet_recorder.py:545-546, 1544-1551  RunInfo.planned_path (:423, :1283) -> UPDATE mission_runs SET planned_path
        |
        v  (state messages)
leg tracker (controllers/mission/leg_tracker.py)
  route_table (:80-91): offset into the concatenated waypoints of all route nodes; planned_path is used only if
                        len(planned_path) == total waypoints, else None (silently: legs get VDA ids only)
  observe (:112-170):   Reach(topomap = planned[offset + wp]) at :163-164; a Leg per consecutive pair of reaches
  Leg (:56-72, :172-195): from_topomap_node / to_topomap_node = leaf ids, straight_m = |waypoint_i - waypoint_{i+1}|
                          (NOT the driven length), planned_m = straight_m, expected_s, duration_s, stopped_s
        |
        v  (run end, fleet_recorder.py)
  run_legs table  (INSERT :168-185; migration 20261004_01_run_legs)         key (run_id, seq)
  _summarize_run -> run_legs.summarize/aggregate (utils/run_legs.py:86,124) -> mission_runs.summary_metrics
  _record_edge_observations (:1589-1620, called :1672): EDGE_LEGS_SQL (:205-209) legs with BOTH topomap ids
        -> edge_stats.leg_observation (utils/edge_stats.py:~70-88): drops src==dst, no map/robot, reject_reason()
        -> edge_stats_db.store (utils/edge_stats_db.py): INSERT ... ON CONFLICT DO NOTHING + PRUNE to newest
           EDGE_STATS_WINDOW=20 (config.py:520) per (map, from_node, to_node, robot)
  second writer: API on mapping-session finish (api/edge_stats.py record_session -> session_observations; source "mapping",
           consecutive session nodes, speed >= EDGE_STATS_MIN_SPEED_MPS; main.py:771-773)
        |
        v  table edge_observations (migration 20261011_01_edge_observations)
          key (map_id, from_node, to_node, robot_name, source, source_key = "<run_id>:<seq>" | "<session>:<idx>")
API read (packages/api/edge_stats.py:50-92)
  GET /api/v1/maps/{map_id}/edge-stats (main.py:830): fetch observations, intersect with ArangoDB edge_set(get_edges_strict)
  NOW (es.on_edges, edge_stats.py:~112), group_stats -> {from_node_id, to_node_id, robot, count, median_s, p90_s,
  median_speed_mps, last_observed_at, recoveries, blocks, sources}
        |
        v
client (web_stack/sati-client): services/mapApi.ts:761-800 EdgeStat, hooks/mapWindow/useEdgeStats.ts,
  utils/edgeStats.ts:12-23 edgeKey = "from\tto" / mergeEdgeStats, components/mapWindow/MapStatsPane.tsx,
  mission window: hooks/useRunStats.ts, components/mission/runMapLayers.ts + MissionMapPane.tsx (colour by leaf edge),
  utils/missionRun.ts:86-87 legEnds(leg) = from_topomap_node || nodeTail(from_vda_node)
```

Exact keys, summarised:

| Stage | Key |
|---|---|
| plan response | `waypoints[i].node_id` == `planned_path[i]` (leaf key string); `chart_keys` = region sequence, NOT aligned to waypoints |
| leg tracker | waypoint index `wp = seq // 2 - 1` within a route node, global index `offset + wp` into `planned_path` |
| `run_legs` | `(run_id, seq)`; `from_topomap_node`, `to_topomap_node` (nullable) |
| `edge_observations` | `(map_id, from_node, to_node, robot_name, source, source_key)` |
| statistics output | `(map_id, from_node_id, to_node_id, robot)` and only if the pair is an Arango edge now |
| blocked rows | `(map_name, graph_node_id)` with `graph_node_id` a leaf key or `"@x,y"` (blocked_nodes.py:158-165) |

### Consumers (file:line, key, computation, persistence)

| # | Consumer | file:line | Key used | Computes | Persists |
|---|---|---|---|---|---|
| C1 | Planner response | `scene_route.py:72-96, 790-805`; `server.py:307-320` | leaf id per waypoint | planned_path pairing | response only |
| C2 | Dispatch mission state | `controllers/mission/server.py:993, 1832` | `planned_path` list | stores it on the mission | mission object (+ `mission_runs.planned_path`, `fleet_recorder.py:1544`, read back `api/fleet_reads.py:398,418`) |
| C3 | Leg tracker | `leg_tracker.py:80-91, 163-164, 187` | index pair into `planned_path`, requires equal length | legs with topomap ids, straight line distance | `run_legs` (`fleet_recorder.py:168-185`) |
| C4 | Run analysis | `utils/run_legs.py:49-124`, `fleet_recorder.py:1580` | `identity(leg)` (VDA node tails, topomap ids) | expected vs actual, per-leg aggregates | `mission_runs.summary_metrics` |
| C5 | Edge observation writer (legs) | `fleet_recorder.py:1589-1620`, `utils/edge_stats.py:~70` | `(from_topomap_node, to_topomap_node)` | one observation per leg, src != dst | `edge_observations` |
| C6 | Edge observation writer (mapping) | `api/edge_stats.py:117-146`, `utils/edge_stats.py:~91` | consecutive session leaf ids | observations | `edge_observations` |
| C7 | Stats read | `api/edge_stats.py:50-92`, `utils/edge_stats.py:~123-170` | leaf pair, filtered by the current Arango edge set | median/p90/speed/recoveries/blocks, window 20 | computed on read |
| C8 | Run legs API | `api/fleet_reads.py:424-440`, `api/main.py:2418-2440` | leaf ids on each leg; `with_chart_keys` adds chart keys derived at READ time (`blocked_geometry.leg_chart_keys`, `blocked_chart_tags.add_leg_chart_keys`) | passes legs through | none |
| C9 | Dispatcher blocked-node resolve | `controllers/mission/server.py:3793-3822, 3848` | `waypoint.node_id` else `planned_path[index]` (single route node only) | graph node id of a blocked waypoint | `blocked_graph_nodes` |
| C10 | Planner blocked-node exclusion | `scene_route.py` (overlay), `utils/blocked_nodes.py:301-394` | graph_node_id / `@x,y` / geometry (`blocked_geometry`) | rows hit on route | n/a |
| C11 | Mission planner mission creation / replay | `mission_planner/server.py:1205-1218, 1355-1370` | `planned_path` stored on mission; else KNN per waypoint | path node ids for reuse | mission |
| C12 | Client | `mapApi.ts:761-800`, `utils/edgeStats.ts:12-23`, `useEdgeStats.ts`, `MapStatsPane.tsx`, `useRunStats.ts`, `runMapLayers.ts`, `missionRun.ts:86` | `from_node_id`+`to_node_id`; `from/to_topomap_node` | colours edges, average speed | in-memory cache |
| C13 | Map delete / robot delete | `api/map_delete.py:88-89` | `map_id` | `DELETE FROM edge_observations` | n/a |
| C14 | Backfill | `scripts/backfill_edge_observations.py` | leaf pairs from `run_legs` and sessions | idempotent re-insert | `edge_observations` |

The "no consumer outside the API route" claim in `tracking_redesign_2026-10-13.md` is true for the planner (nothing
feeds edge weights: `api/edge_stats.py` module docstring says so) but the client does consume `/edge-stats` (C12), so
a key change is an API contract change. Unverified: whether `useRunStats.ts` joins the stats to legs by `legEnds`
(likely, by the import of `edgeKey`; I did not read the join).

---------------------------------------------------------------------------------------------------------------

## 2. Problems with the status quo

1. **Distinct leaf ids per waypoint.** `planned_path[i]` must exist for every waypoint (`build_waypoints` returns
   `None` otherwise; `scene_route.py:800-802` then falls back to legacy). Repair/collapse
   (`:99`, `:135`) mitigate repeats but a leaf-less waypoint still kills a good geometric route. On open maps
   (leaves farther apart than `SCENE_GRAPH_LEAF_SNAP_M`) this is routine, not rare. Verified in code.
2. **Legacy fallback is part of the contract.** `ChartFallback` reasons and the planner `"legacy"` value exist because
   leaf ids are a hard requirement; the dispatcher/leg tracker contract (`leg_tracker.py:20-23`,
   `route_table` `len(planned) == offset`) silently degrades instead of failing, so a mismatch is invisible
   (legs lose topomap ids, statistics vanish for that run, no event). Verified.
3. **Geometric routes thrown away.** A route is a lattice path through charts; its waypoints are a line-of-sight
   simplification, not leaf positions. Pairing `waypoints[i] <-> leaf_i` attributes the time of a leg to the leaf
   edge between two leaves that are merely *near* the endpoints; `straight_m` is the straight line between the
   waypoints (`leg_tracker.py:174-179`), not the leaf-to-leaf distance, so duration and distance already describe
   different geometry than the key they are filed under. Verified.
4. **Wrong keying granularity for what the planner will price.** The planner routes on charts and links; a
   leaf-pair statistic can only be used after mapping back to charts. `on_edges` also silently drops any observation
   whose leaf pair is not an Arango edge now (`edge_stats.py` module docstring), so on a graph with leaves joined only
   within a threshold, legs between non-adjacent leaves are lost (verified from the docstring; loss rate not measured).
5. **Leaf ids are not stable either.** Topomap leaves can be deleted/re-aligned (`refold` after leaf delete or realign,
   `api.py` `refold` docstring); an observation on a deleted edge is ignored, not migrated (`edge_stats.py` docstring).
6. **Two writers of different quality.** Mapping sessions (hand-driven, no recoveries/blocks) share the window of 20
   with mission legs (`tracking_redesign` "Current state"); D4 removes the mapping writer.
7. **Time/distance semantics.** `straight_m` is not driven distance, `duration_s` includes stopped time (`stopped_s`
   is separate); speed in `edge_observations` = straight / total, a lower bound of the real speed. Verified in
   `leg_tracker.py:172-195`.

---------------------------------------------------------------------------------------------------------------

## 3. Target

Statistics are attributed to **a link of the chart graph** `(a, b)` (undirected geometry `Link`, `api.py:80`:
`a, b, length, width, portal, channels, saddle`) and rolled up to charts; the leaf graph is only used for images
(`tracking_redesign` principle, tier 3).

Source of the attribution, in order of preference:

* **Observed path** (best): the run's map-frame poses (stage 1 of `tracking_redesign` already records `x_map, y_map,
  theta_map` on `robot_track_ts`, migration `20261013_01_track_map_frame`) located with `locate` into a sequence of
  charts; each chart change is a transit `(a -> b)` with `t_in, t_out, in_xy, out_xy, distance, speed, recoveries,
  blocks`.
* **Planned path** (fallback when there is no track, level `events_only`): the plan's `chart_keys` plus the leg
  times. `chart_keys` is the region sequence of the route (`server.py:318`, `Route.charts`, `api.py` Route) and
  already needs no leaf ids. To attribute a leg to a chart pair, map each waypoint to its chart with `locate`
  (or compute waypoint -> index in `chart_keys` at plan time and ship `waypoint_chart_idx: List[int]` as an
  additive response field; unverified that this is cheap: needs the route cell -> chart sequence which
  `lazy_plan.py` has).
* **Per-link figures**: count, median/p90 duration, median speed over `Link.length` (use the link's length as the
  denominator when the transit's own distance is missing), recoveries, blocks; plus `width` for context (clearance
  limits speed). Window of N per `(map, a, b, robot)` as today.
* Directed: keep (a -> b) directed in storage (the leaf version is directed too); the link itself is undirected.

What the plan response must additionally carry for the planned-path source: nothing new for `chart_keys`; optionally
the `graph_version` (already present, `server.py:319`) so a transit is stamped with the version used for attribution.

---------------------------------------------------------------------------------------------------------------

## 4. The hard part: keys change across versions and generations

### 4.1 What the code offers (verified)

* **Chart key** = `"{birth_leaf_key}:{counter}"` (`api.py:13`); persistence "Rule A": a new chart takes the key of the
  previous chart it overlaps most, losers get new keys, unclaimed keys are retired and never reused
  (`chart_keys.py:1-5`). CIMBI's measured stability: about 94-95 percent overlap per frame (`integration.md:46`).
* **Retired mapping** per version: `Dirty.retired: key -> successors by cell overlap, largest first, [] = vanished`
  (`api.py:56-61`), persisted in each manifest (`store.py:331`) and read back by `store.lineage(map_id, from_version)`
  (`store.py:653-670`), cumulative and compacted, raises `TooOld` when an intermediate manifest is gone.
* **Generations** (params change or realign): `begin_generation` / `commit_generation` (`store.py:714,738`)
  compute `_overlap_keymap(old, new)` by cell overlap and store it as `retired` of the new version, so lineage walks
  across the switch.
* **Retention**: `keep_last=5` versions (`store.py:152,672-682`). Lineage older than that is not recoverable.
  A statistic older than 5 versions cannot be remapped through `lineage()`. Verified.
* **Link identity**: a link is `(a, b)` + portal; the mirror plan proposes `_key = hash(a, b, portal)` (findings
  section 5). Note that `portal` is geometry that can change when either chart changes by a single cell, so
  the hash is NOT stable across the 94-95 percent of unchanged-key frames: use `(a, b)` only for identity and keep
  portal as an attribute (unverified: whether two distinct links between the same pair of charts can exist; `Link`
  has no multiplicity field and `_chart_graph` takes `min` over `(a,b)`, `engine.py:828-834`, which suggests one
  per pair).
* **Splits and merges**: one key lives on, the other half is born new; lineage gives `[]` or successors by overlap,
  not by *fraction of the link crossed*.

### 4.2 Options

| | A. Stable link id `hash(a,b)` (or `(a,b,portal)`), statistics stored under chart keys | B. Lineage remap table at write or read | C. Re-attribution by geometry (D1 of tracking_redesign): store entry/exit xy, `locate` on read |
|---|---|---|---|
| Idea | Persist `(a_key, b_key)`; keys are "persistent" so mostly still valid | When a key retires, rewrite stored rows via `Dirty.retired` / `lineage()` | Persist poses (+ time, distance); chart pair computed at read time against the current version |
| Survives split | No: the old key keeps half; the other half's stats are lost or attributed to the old key | Yes for the largest successor only (overlap), wrong for a link now on the minor successor | Yes, exact |
| Survives merge | Old key retired: stats orphaned | Maps to the survivor | Yes |
| Survives generation change (params / realign) | No (all keys new unless overlap map applied) | Only via `commit_generation` key map; one hop each | Yes (xy are map-frame; a realign changes the frame: see 4.3) |
| Needs retention | None | Yes: `lineage` TooOld after 5 versions, and a batch job at every version | None |
| Read cost | O(1) join | O(1) after rewrite; rewrite O(rows touched) per version | `locate` per stored pose per read: needs a cache keyed by graph version, or a materialised `(version -> a,b)` column refreshed lazily |
| Write cost | Cheap | Needs a worker on every `scene_graph_updated` | Cheap (pose + time) |
| Storage | small | small | larger (2 poses per observation); same order as `edge_observations` row |
| Failure mode | silent loss | silent mis-attribution across splits; lost history on TooOld | `locate` returns None outside charts (obstacle moved): observation unattributed, counted as such |
| Matches code today | `chart_keys` already in response | `Dirty.retired` and `lineage()` exist | `locate` exists; leg chart tagging at read already done this way (`blocked_geometry.leg_chart_keys` docstring: "derived at read time ... a key that died is not kept") |

### 4.3 Recommendation

**Option C as the source of truth, with a version-stamped cache.** It is what `tracking_redesign` D1 already chose,
what the blocked-geometry/chart-tag work already does for runs and blocked rows (`integration.md:476-487`), and it
is the only option that does not depend on lineage retention. Cost to pay: the read must call `locate` (scene
graph load or lazy tile read) -- the API already has `_scene_graph_reads()` (`api/main.py:587`).

Details that need a decision (see open questions):

1. **Cache**: a materialised table `link_transits(map, robot, run, t, graph_version, a, b, in_xy, out_xy, ...)`
   where `(a, b)` is computed once per `(row, graph_version)` and recomputed lazily when the current version
   differs (cheap when the chart graph is 5-16 charts per map, `findings` section 5). The raw poses are never
   rewritten; the chart pair is a cache and can be discarded.
2. **Realign**: a map realign changes the frame; stored map-frame poses become wrong. `refold` after realign is a
   frame change (`api.py` refold docstring). `tracking_redesign` D2 only says the frame is converted at record time.
   Unverified how realign is detected for stored poses (`map_sessions.run_epoch` and the frame witness are
   mentioned in the doc but I did not read them). Needs the statistics owner.
3. **Fallback to lineage (B)** only for rows that have no usable pose (legs at `events_only` without a track): use
   `chart_keys` of the plan stored with the run and map them through `store.lineage(map, from_version)`; on
   `TooOld`, mark the row `unattributed` rather than guess. This needs `chart_keys` and `graph_version` stored on
   the run (today `mission_runs.planned_path` is stored (`fleet_recorder.py:1544`) but `chart_keys` /
   `graph_version` are not: verified by grep, no persistence in `fleet_recorder.py`; they are only in the plan
   response, `server.py:318-319`, and the dispatcher keeps only `planned_path` at `server.py:993`).
4. **Option A** is acceptable as an accelerator (the key column) but not as truth.

Attribution by portal xy (the third alternative named in section 13): a transit crosses a portal segment
(`Link.portal`); re-attribution by "which portal was crossed" is a stricter variant of C that disambiguates two
adjacent links of one chart. Worth doing for the crossing time (`t_in`/`t_out` at the portal), not for storage.

---------------------------------------------------------------------------------------------------------------

## 5. What changes per consumer, ownership, migration, rollout, tests

### 5.1 Per consumer

| Consumer | Change | Size (estimate) |
|---|---|---|
| C1 planner `scene_route.py` | Make `planned_path` optional for statistics: keep the existing leaf snap only for dispatcher `@x,y` / legacy contracts. Decide option (a) of section 13 (return route with `planned_path=None`, node ids None per waypoint) so `ChartFallback("waypoints_without_distinct_leaf_ids")` goes away; add `chart_keys` per waypoint if wanted. Tests: `tests/unit/test_scene_route*` (names unverified) | 1-2 days |
| C2/C11 dispatch + planner mission | Accept a `None` `planned_path` (dispatcher already falls back to `@x,y`, section 13); store `chart_keys` + `graph_version` on the mission and run | 1 day |
| C3 leg tracker | No longer needs equal-length pairing for statistics; keep emitting legs with the poses of both reaches (map-frame) so a transit can be built without a track. Add `from_xy/to_xy` or reuse `pose` | 1 day |
| C4 run analysis | `run_legs.identity` keys on VDA tails + topomap ids: unchanged; add optional chart attribution beside it. Expected-vs-actual stays leg based | 0.5 day |
| C5 writer | Stop writing leaf observations at run end (stage 3); write transits (stage 2). Post-run job rather than the run-end hot path, because it needs the scene-graph read side that dispatch does not have (docstring `fleet_recorder.py:1589`) | 3-4 days |
| C6 mapping writer | Remove (D4) | 0.5 day |
| C7/C12 read + client | New `GET /api/v1/maps/{id}/link-stats` (`tracking_redesign` stage 2) keyed `(a_key, b_key, robot)`; client `edgeKey` -> link key; `/edge-stats` deprecated one release, then removed | API 2 days; client 2-3 days (all estimates) |
| C8 legs API | Already has `with_chart_keys`; no change except marking which source attributed | 0.5 day |
| C9/C10 blocked nodes | **Out of scope** of statistics: `blocked_graph_nodes` is keyed by `graph_node_id` (leaf or `@x,y`) and already has geometry columns plus read-time chart tags (migration `20261012_01_blocked_geometry`). Only requirement: do not break `planned_path[index]` fallback at `server.py:3820` while `planned_path` may be None | 0 |
| C13/C14 | Extend delete and backfill to the new table; backfill transits from `run_legs` rows with poses | 1-2 days |

Total (estimate, one engineer who knows the code): about 2-3 weeks including client and tests; the dependency on
`tracking_redesign` stages 0-1 (scene graph on `main`, frame recorded) is the schedule risk, not the code.

### 5.2 Ownership

* Library (`packages/scene_graph`, `locate`, `lineage`, keys): **CIMBI**.
* Planner (`scene_route.py`, plan response fields), read API (`scene_graph_reads`, `link-stats` route glue),
  `chart_keys` tagging helpers (`blocked_geometry`, `blocked_chart_tags`): **LACI**
  (per `integration.md:87-94` ownership table and the leg/blocked chart-key work credited there).
* Statistics: leg tracker, `run_legs`, `edge_stats`, `edge_observations` and the tracking redesign. `git log` for
  `packages/utils/edge_stats.py`, `packages/api/edge_stats.py`, `leg_tracker.py` shows author "Laszlo Freund"
  (the account that also commits the tracking-redesign commits `67bff85`, `c9d1a53`, `89f4891`). That is the commit
  author, not a stated owner: **statistics owner is unknown: ask the user** whether those commits are LACI's or a
  separate person's, and who signs off on `link-stats` semantics (window, per-robot, sources).

### 5.3 Migration and compatibility

* Dual write: keep writing `edge_observations` (C5) and add transits until the client reads `link-stats`
  (stage 3 of `tracking_redesign`), then drop the leaf writer. Reasonable because both are post-run, savepointed and
  idempotent (`ON CONFLICT DO NOTHING`).
* Read fallback: `link-stats` falls back to rolling up leaf statistics (leaf -> `locate` of the leaf xy via
  `graph_db.get_node` -> chart) while a map has no transits; mark `source: "leaf_rollup"`.
* Migration for the table follows the rolling-deploy order documented in `tracking_redesign` (API first, then
  dispatch) since dispatch will write the new table.
* Rollback: leaf writer stays until one release after the client switch.

### 5.4 Rollout

1. Land after the scene graph is on `main` and deployed (stage 0). The planner flag is OFF today and no database
   generation was started (`findings` section 1), so nothing in production has chart keys yet.
2. Stage 2 behind a flag (suggested `LINK_STATS_ENABLED`, off); shadow-write transits, compare with leaf stats on
   the rehearsal maps (office8: 12 charts, office5: 5 charts, `findings` section 5).
3. Client switch per map via the existing `planner` field / map flag; keep `/edge-stats` one release.

### 5.5 Tests (proposed)

* Unit: transit builder (pose sequence -> chart changes, hysteresis at portals, `locate` None gaps); window/prune
  per `(map, a, b, robot)`; recompute across `graph_version` after a split, a merge, a retire with `[]`, and a
  `commit_generation` key map.
* Property test: observations re-attributed at version v+1 with `lineage(from_version=v)` agree with C (poses)
  for keys that did not split (pins the option B fallback).
* Integration (Postgres): table, idempotent insert, prune, delete on map delete, backfill equals incremental.
* e2e (`tools/scene_graph_e2e`, not run in this pass): mission on an open map with a leaf-less waypoint no longer
  falls back; link stats appear for the route's charts.
* Contract: existing `tests/unit/test_edge_stats.py` and `test_run_legs.py` stay green during dual write.

---------------------------------------------------------------------------------------------------------------

## 5b. Decision of the user (2026-10-13): statistics are stored in ArangoDB, on edges

Stated as the end state ("ultimately"), not a schedule. Today: observations in Postgres `edge_observations`
(`api/edge_stats.py`, `utils/edge_stats.py`), statistics computed on read, intersected with the current Arango edge set.

Consequences to design (recommendation, not decided; open item 10 below):

* **Two layers.** Raw observations (poses, time, distance, high volume, window of the last 20 per key) stay append-only
  and pruned where the transit job writes them; the per-edge aggregate (count, median, p90, speed, recoveries, blocks,
  window bounds, the graph version it was computed against) is a document attribute on the Arango EDGE (leaf edge now;
  chart link once the read-model mirror exists, whose edge `_key` is hash(a, b, portal) and is NOT stable across
  versions: D1 re-looks-up the pair, so the aggregate is recomputed per graph version, not carried).
* **Why not raw rows only in Arango:** per-leg writes at run rate into the same database that serves path queries; the
  window-of-20 median needs ordered history. Arango as the home of the aggregate keeps the stats next to the edge they
  describe and lets path queries use them (weights) without a Postgres round trip.
* **What it unblocks:** pricing can read stats from the same store as the graph; edge deletion in `graph_db` removes
  its stats with it (today `edge_stats` filters by the current edge set to hide stale rows).
* **Costs:** writer in the post-run transit job (API or graph-builder: open question 2), idempotent upsert keyed
  `(map, edge, robot)`, retention on map/robot delete (C13), backfill from `edge_observations` (C14), and the client
  contract of `/edge-stats` (C12) must stay stable during dual write.

## 6. Open questions (ranked)

1. **Is `tracking_redesign_2026-10-13.md` D1 (poses stored, chart pair looked up on read, no lineage) the accepted
   direction, so that section 13 of `planner_upgrade.md` is just a pointer to it?** The decisions say "taken with the
   user"; confirm this note should not propose a competing scheme.
2. **Who owns statistics** (leg tracker, `run_legs`, `edge_observations`, `link-stats` semantics): unknown, ask the
   user; and who owns the post-run transit job (API or graph-builder).
3. **Source of transits at `events_only` without a track** (the open item of `tracking_redesign`): leg endpoint poses
   plus stored `chart_keys`/`graph_version`, or force `track` on while a go-to runs? The plan response does not reach
   `mission_runs` today.
4. **Map realign / frame change**: what happens to stored map-frame poses and cached chart pairs? `refold` after
   realign changes the frame; D2 only handles record-time conversion.
5. **Attribution rule for a transit that crosses charts without a leg boundary**: charts per leg are many (`leg_chart_keys`
   samples every 0.5 m): one observation per leg with a chart list, or one per chart pair with time split by path
   length? This changes what "count" and "speed" mean per link.
6. **Link identity**: confirm one link per `(a, b)` pair (the engine takes `min` length, `engine.py:828-834`); decide
   whether `portal` is part of identity (recommend no).
7. **Plan-response shape**: ship `planned_path=None` when a waypoint has no leaf (section 13 option (a)) or keep the
   fallback and fix only the statistics path. Affects dispatcher `server.py:3820` and `leg_tracker.py:80-91`.
8. **Window semantic**: last N=20 per `(map, a, b, robot)` mixes versions; with charts splitting, a bigger window
   after a merge? Retention suggested "none, bounded by window + 1-year cap" (`tracking_redesign` Open).
9. **Client contract**: `/edge-stats` is consumed by the client (C12), contrary to the "no consumer" statement in the
   tracking doc; confirm deprecation window.
10. **Directedness**: store `(a -> b)` directed (as leaf stats are) or undirected per link (one-way ramps, slopes).

## 7. Effort summary (all estimates, not measured)

| Part | Estimate |
|---|---|
| Decision + spec of link-stats (questions 1-5) | 0.5-1 day with the user |
| Planner/dispatch changes (C1, C2, C11, C3) | 3-4 days |
| Transit job + table + backfill + delete (C5, C6, C13, C14) | 4-6 days |
| `link-stats` route + legacy rollup + API tests | 2-3 days |
| Client switch + tests | 2-3 days |
| Hardening, rehearsal on pinned maps, rollout flag | 2 days |
| Total | about 3-4 weeks one person, mostly gated by `tracking_redesign` stages 0-1 |

## 8. Not verified in this pass

* The client join of run statistics to legs (`useRunStats.ts`, `runMapLayers.ts` internals).
* Whether realign/frame witness handling in `map_sessions` covers stored `x_map, y_map` (question 4).
* `lazy_plan.py` cost of returning waypoint -> chart indices.
* Existence of the unit test file names listed under C1.
* `web_stack/cloud_server` (`main`) differs from the worktree: only checked that the same statistics files exist
  there; its content was not diffed, and the stage-1 track-map-frame migration is not on `main` (unverified).
10. **ArangoDB as the home of the per-edge aggregate (user decision 2026-10-13):** confirm the split of section 5b
    (raw observations append-only in Postgres, aggregate as an attribute of the Arango edge) or whether raw
    observations should move too; and whether the aggregate attaches to leaf edges first or waits for chart links.
