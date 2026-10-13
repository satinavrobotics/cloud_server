# Scene graph: handoff of the persistence regions and charts to LACI

From CIMBI, 2026-10-13. Everything here is on `feat/scene-graph-v2` (the one branch). Read `formulation.md` for the definition and
`audit_ledger.md` for every finding, derivation and verification behind it.

## What changed for the consumers

| area | before (star charts) | now (persistence partition) |
|---|---|---|
| chart count | 5-19 per 70 m2 office | 8-16 per floor on the 7 real floors (93 in all, previous 102): charts of radius <= 2 m cut from 3-7 persistence regions |
| partition | history dependent (batch boundaries) | pure function of F: identical for any batching/arrival order |
| `Chart.dependent` | dependents exist | all False |
| `Link.width` | strict widest path of clearance | twice the PHYSICAL clearance at the arc's widest crossing (same unit as `required_width`, +- 0.05 m) |
| `Route.min_width` | narrowest crossed link | unchanged |
| levels | net-tree on anchors | level 0 charts, level 1 the persistence regions at tau0 (omitted when identical), then tau0 * 2^j until one region per component; `query(level >= ViewResult.levels)` returns the top level |
| `SceneParams` | `robot_half_side`, `lattice`, `place_mult`, `sight_range` | `robot_half_side`, `lattice`, `chart_radius` (m, default 2.0: the one scene-dependent scale; it also bounds `snap` by the chart diameter). `place_mult` and `sight_range` are removed; the window cap (20 m side) and pose slack (5 m) are engine constants. `scene_host.params_dict` is changed accordingly |
| store | FORMAT 2, solver state in snapshots | FORMAT 4, pipeline `morse/2`: no solver state, a chart record carries `region`; load = rebuild F, partition, match stored keys; old stores get a new generation |
| cost | hall 31k cells 0.15 s per batch of 6; open fields minutes | 1.1-1.5 microseconds per cell per batch: hall 45k 0.07 s, field 86k 0.11 s, 244k-cell open field 0.63 s |

## What to run before merging into feat/scene-graph-v2

1. `tools/scene_graph_e2e/run.sh` from the morse worktree (needs your pydantic-1 python and secrets).
2. Your scene unit suites (565 passed, 3 skipped on host python in the author's run on 2026-10-13, not re-run since; `test_scene_guard` and `test_scene_graph_routes_http` do not
   import on host python, pre-existing).
3. The planner width gate once with a real factsheet (rehearsal F-7: 0.1 m widths where a driven path crosses a gap narrower than the
   default r_d allows).

## Minor items I found for your side (rehearsal 2)

* `SceneGraphUpdated` is rejected after a replay: `scene_runner.py:163` sends `skipped={...}`, `packages/events/schemas.py:419` has no such
  field (event lost, ERROR logged). Add `skipped: Optional[Dict[str, int]]`.
* `reads._check`: return 422 for `level` beyond the map's level count (the engine clamps).
* `duplicates` counter re-counts rows at every poll tick (1000-2500 in 8 s idle).
* One batch-of-6 run ended with 1 leaf unsaved (office6 108 of 109); not reproduced with a 5 s quiescence wait: check the runner's idle
  condition.
* `meta.params` carries `chart_radius` (`scene_host.params_dict`).
* Hospital (open simulation): no longer stalls; keep simulation maps off unless you want them in the graph.
* Update `integration.md` / `runbook.md` wording that assumes dependents, chart counts of 13-19, `sight_range`, descent budgets and the
  open-field limit (maps of 12-15 m now fold in milliseconds).

## Expected result of the database generation (reference for checking it)

Charts / links / persistence regions per map at the default radius (2 m), commit ee64736 (real exports, batches 6 and 48; the
partition does not depend on batching). Hospital was not re-run with charts.

| map | charts | links | regions | | map | charts | links | regions |
|---|---|---|---|---|---|---|---|---|
| office8 | 12 | 15 | 4 | | office04 | 16 | 15 | 7 |
| office01 | 15 | 16 | 6 | | office5 | 5 | 5 | 1 |
| Officemap | 14 | 15 | 5 | | office6 | 14 | 17 | 3 |
| office03 | 8 | 6 | 5 | | office7 | 14 | 17 | 4 |

(Links include the links between charts of one region. Anchors are chart centres.)

Generation order: office8 needs the `costmap_stored_ms` backfill first (your tool, dry run by default; the 7 old maps got the field when
`occupancy_depth` was written); enable the worker per map via `status.scene_graph_enabled` (absent = enabled once the worker flag is on);
verify each map's stored state read-only with `python -m tools.scene_graph_check_generation --all [--expect expected.json]`
(run in the graph-builder container; format, pipeline, params hash, invariants, counts, one route; exit code 1 on any failure) and compare
the counts with the table; never enable the planner flag before the maps are generated and checked.

## Answers to the e2e round (LACI -> CIMBI, 2026-10-13)

* **Rebuild order.** Keep fold in canonical sorted (stamp_ms, key), fixed batches of 48. On the persistence partition the partition is a
  pure function of F, so batching and arrival order change nothing about it (tested: 6 orders and batchings identical). What the
  order still decides: the driven polyline (it is drawn leaf to leaf by capture stamp) and therefore the witness cells of F, and the
  fold's per-cell latest-capture rule. A fixed canonical order is what makes the rebuild reproducible; it is not needed for the regions.
* **Route width (issue A).** `Route.min_width` is now, per crossed chart pair, the widest arc (`Link.width`), minimum over the pairs.
  The sliver arcs remain in `query().links`; the client may hide links narrower than the robot. See `formulation.md` section 4.
* **Level count.** `ViewResult.levels` (engine and stored view): levels 0 .. levels-1 are valid, a level beyond is answered with the
  top. It is 0 when not computed: a query with `parents=False` and `level=0` never builds the hierarchy. For the 422 check call
  `query(bbox, level=0, parents=True, raster=False)` once; it is cached per version.
* **Loops and long corridors.** A constant-width loop or a long corridor is one region; a room is a region when its peak clearance
  exceeds the exit saddle by more than tau0 (about 1.8 m for a 1.2 m corridor). Written in `formulation.md` section 4.
* **put_index race.** Fixed: the index swap is one transaction (cd73b80); no reader sees a truncated index.
* **Hospital.** 0.73 s per batch / 185 MB on the persistence partition; item dropped.
* **Ledger.** `docs/scene_graph/audit_ledger.md` is the single copy.

## Charts and route (added after the e2e round, 2026-10-13)

* **Why.** The persistence regions alone were 2-4x coarser than the old cover: large regions (40-80 % of a floor), anchors far from
  the way, and the planner's path (start -> anchor -> anchor -> goal) up to 4x the shortest. Each region is now cut into charts
  of geodesic radius <= `chart_radius` (2 m): `morse.subdivide`, `formulation.md` section 2 and statements 17-27.
* **Planner.** Call `graph.route(a, b, blocked, start=(x, y), goal=(x, y))` with the snapped cell's x, y: the route runs cell to
  cell, no anchor on the way. Without start/goal it is anchor to anchor, as before.
* **API.** `Chart.region` (persistence region id), `Chart.peak` is the chart's largest B, `ViewResult.levels`.
  Level 1 is now the persistence regions (the old level 1 = 2 tau0 is level 2).
* **Store.** FORMAT 4 / pipeline `morse/2`: an old store has a different params hash and is rebuilt as a new generation.
* **Checks.** Verifier (`chart_verify.py`, from definitions): 0 violations on 7 real floors at batches 6 and 48, 13 worlds, 6 random
  fields; batching independent; unit suites 188 passed.
