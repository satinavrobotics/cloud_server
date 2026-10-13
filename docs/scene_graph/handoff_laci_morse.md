# Scene graph: handoff of the persistence (Morse) partition to LACI

From CIMBI, 2026-10-13. Branch `feat/scene-graph-morse` (from the v2 tip 7843576). Read `formulation.md` for the definition and
`audit_ledger.md` for every finding, derivation and verification behind it.

## What changed for the consumers

| area | before (star charts) | now (persistence partition) |
|---|---|---|
| chart count | 5-19 per 70 m2 office | 1-7 (rooms, corridors); regions may have holes |
| partition | history dependent (batch boundaries) | pure function of F: identical for any batching/arrival order |
| `Chart.dependent` | dependents exist | all False |
| `Link.width` | strict widest path of clearance | twice the PHYSICAL clearance at the arc's widest crossing (same unit as `required_width`, +- 0.05 m) |
| `Route.min_width` | narrowest crossed link | unchanged |
| levels | net-tree on anchors | persistence levels tau0 * 2^j until one region per component; `query(level >= count)` returns the top level |
| `SceneParams` | `sight_range`, `place_mult` read | both deprecated no-ops (kept because `params_dict` is pinned by a test); stop writing them at the next format bump |
| store | FORMAT 2, solver state in snapshots | FORMAT 3, pipeline `morse/1`: no solver state; load = rebuild F, partition, match stored keys; old stores get a new generation |
| cost | hall 31k cells 0.15 s per batch of 6; open fields minutes | 0.03 s; field 86k cells 0.05 s; Hospital 0.7 s |

## What to run before merging into feat/scene-graph-v2

1. `tools/scene_graph_e2e/run.sh` from the morse worktree (needs your pydantic-1 python and secrets).
2. Your scene unit suites (565 passed, 3 skipped on host python in my run; `test_scene_guard` and `test_scene_graph_routes_http` do not
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
* `meta.params` still carries `sight_range`/`place_mult` (`scene_host.py:109`).
* Hospital (open simulation): no longer stalls; keep simulation maps off unless you want them in the graph.
* Update `integration.md` / `runbook.md` wording that assumes dependents, chart counts of 13-19, `sight_range`, descent budgets and the
  open-field limit (maps of 12-15 m now fold in milliseconds).

## Expected result of the database generation (reference for checking it)

Regions/links at the base level from the rehearsal (real exports, batches 6/48; the partition does not depend on batching):

| map | regions | links | | map | regions | links |
|---|---|---|---|---|---|---|
| office8 | 4 | 5 | | office04 | 7 | 4 |
| office01 | 6 | 6 | | office5 | 1 | 0 |
| Officemap | 5 | 6 | | office6 | 3 | 4 |
| office03 | 5 | 3 | | office7 | 4 | 5 |
| Hospital | 7 | 6 | | | | |

(office5 shows 1 region after the driven-unknown rule e3aef5a; it had 2 before.)

Generation order: office8 needs the `costmap_stored_ms` backfill first (your tool, dry run by default; the 7 old maps got the field when
`occupancy_depth` was written); enable the worker per map via `status.scene_graph_enabled` (absent = enabled once the worker flag is on);
verify each map's stored state with `SceneGraphStore.load` and `tests/unit/scene_graph/chart_verify.verify`; compare regions/links with the
table; never enable the planner flag before the maps are generated and checked.
