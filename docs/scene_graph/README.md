# Scene graph and planning: entry point (state 2026-10-13, code at `a93a696` on `main`)

Read this first. It says what is built, where each part lives, what runs live, and what is still open, on both sides:
**CIMBI** (algorithm, store, checker: `packages/scene_graph`, `tools/scene_graph_check_generation.py`) and **LACI**
(worker, read API, planner, client, ops: `packages/services/graph_builder`, `packages/api`, `packages/services/mission_planner`).
Every claim names its source. Items marked *reported* come from LACI's production checks and were not re-run by CIMBI.

## 1. What the scene graph is

A hierarchical topological graph of the robot's free space, built from its costmap windows.

* **F** (the navigable cells): lattice cells (step 0.1 m) where a robot of half side `robot_half_side` (0.25 m) can stand, from the
  folded costmap plus the driven poses. `formulation.md` section 2.
* **Charts** (level 0): F cut into compact pieces, each within geodesic distance `chart_radius` (2.0 m, the one scene-dependent
  scale) of its anchor, anchor = chart centre. A chart never crosses a pass.
* **Persistence regions** (level 1): charts grouped by the Morse/persistence partition of F at tau0 = 2 sqrt(2) h. Regions are the
  places separated by a real narrowing.
* **Links**: one per interface arc between two charts, with `width` (physical passage width, m), `length`, and per-channel
  evidence integrals (`undriven`, `ever_blocked`, `soft`).
* **Levels above regions**: tau0 * 2^j, until one region per connected component (`morse.cell_levels`).
* **Route**: point-to-point lattice search inside the corridor of charts picked on the chart graph. Output: `charts`, `nodes`,
  `length` (geometric m), `cost` (integer, 1e-4 m units, each edge rounded up), `min_width` (narrowest crossed passage).
  Equal-cost routes resolve to the lexicographic minimum of their (y, x) cell sequence (`88e5651`).

Definitions and the verification status of every statement: `formulation.md` (EXACT / TESTED / HEURISTIC). The derivation and
refuted claims: `audit_ledger.md`. The 2026-10-13 artifact with the figures: https://claude.ai/artifact/2zJP73VSBuxEaHs9W2ga2q

## 2. Where it lives (code)

| Part | Path | Owner |
|---|---|---|
| Contract (types, `SceneParams`, `Route`, `Chart`, `Link`) | `packages/scene_graph/api.py` | CIMBI |
| Engine: fold, F, partition, links, route, query | `packages/scene_graph/engine.py`, `morse.py` | CIMBI |
| Persistence (tiles, pages, manifest, CAS, generations) | `packages/scene_graph/store.py`, `store_codec.py`, `store_backends.py`, `state.py` | CIMBI |
| Independent verifier (tests only) | `tests/unit/scene_graph/chart_verify.py` | CIMBI |
| Post-generation checker and pins | `tools/scene_graph_check_generation.py`, `tools/scene_graph_expected.json` | CIMBI |
| Backfill of `costmap_stored_ms` | `tools/scene_costmap_stored_backfill.py` | LACI tool |
| Ingest worker, adapter, host | `packages/services/graph_builder/scene_*.py` | LACI |
| Read API | `packages/api/scene_graph_reads.py` | LACI |
| Planner: snap, route, width gate, fallbacks | `packages/services/mission_planner/scene_route.py`, `server.py` | LACI |
| Lazy planner (tile-loaded `PlanGraph`) | `packages/services/mission_planner/lazy_plan.py` (`SCENE_GRAPH_PLANNER_MODE=lazy`) | LACI |
| Config and flags | `packages/config.py` (`SCENE_GRAPH_*`), `docker_compose/mission_dispatch_services.yaml`, `.env.example` | both |
| Migration | `packages/api/migrations/versions/20261012_01_blocked_geometry.py` | LACI |

Constants that matter: `FORMAT = 4`, `PIPELINE = "morse/2"` (store), `LAT = 2` (costmap cells per lattice cell, engine),
`RES = 0.05` m costmap cell. `SceneParams` = `robot_half_side`, `lattice`, `chart_radius` (`api.py`); nothing else is a scene
parameter. The engine's window cap (20 m side) and pose slack (5 m) are validation constants, not tunables.

## 3. Live state (as reported by LACI, 2026-10-13)

* `main` = `feat/scene-graph-v2` = `a93a696`. Production runs it on graph-builder, API and planner.
* Worker: all 8 live office maps ingesting (`office5`, `office6`, `office7`, `office8`, `office01`, `office03`, `office04`,
  `Officemap`). Hospital and the two stray test maps are switched off (`scene_graph_enabled=false`).
* Stored graphs: checker `--all --expect tools/scene_graph_expected.json` gives 8 of 8 (LACI, reported). Pinned counts
  (charts / links / islands): office8 12/15/0, office01 15/16/3, Officemap 14/15/1, office03 8/6/1, office04 16/15/4,
  office5 5/5/0, office6 14/17/0, office7 14/17/0.
* Planner: `SCENE_GRAPH_PLANNER_ENABLED=true`, mode `full_load`, `SCENE_GRAPH_CACHE_VERSIONS=8` (it was 4, which pre-warmed only 4
  of 8 maps), all 8 maps pre-warmed at 16:19 UTC (LACI, reported). Hospital and the two test maps are skipped by pre-warm and fall
  back to the legacy planner. The width gate (`SCENE_GRAPH_WIDTH_CHECK_ENABLED`) is off.
* Rollback tags on the production hosts: `:pre-scenev2-20261013` (before the first deploy), `:pre-7655b10` (before the hash fix),
  `:pre-a93a696` (graph-builder image, before the 16:19 UTC rebuild; the other two were also tagged on API and planner images).
* Production read-only facts (LACI, 2026-10-13): alembic head `20261012_01_blocked_geometry` applied; factsheets: `formidable-peacock`
  0.5 x 0.5 m, `masked-frigatebird` and `subtle-pelican` have none (default footprint, counted in `default_r_d`).

## 4. What is verified, and how

* **Engine:** independent verifier (`chart_verify.py`, from definitions): 0 violations on the 8 real maps at batch sizes 6 and 48,
  on 13 synthetic worlds and random fields. Batching, arrival order and translation do not change the result (tests).
* **Route:** minimal integer cost and the (y, x) tie rule checked against an independent reference (heapq plus DP over the
  shortest-path DAG), including under an overlay (`test_engine.py`).
* **Store:** load equals save, replay does not bump the version, kill and restart give the same leaf set, delete saga leaves
  nothing (rehearsal on the throw-away stack with the real worker on the captured leaves of all 9 maps).
* **E2E harness:** `tools/scene_graph_e2e/run.sh`, 32 passed in both planner modes (on `4104b8d`; not re-run on `a93a696`).
* **Pinned unit suite:** 4212 passed on the production pins (py3.10, pydantic 1.9.0), on `4104b8d`.
* **Measurements:** `formulation.md` statements 25-27 (cost, anchor-chain stretch, route quality) and statement 28 (ties).

## 5. Gaps, CIMBI side

1. **Rate pricing / count planes.** Not built. Design, with the planner side's conditions: `formulation.md` section 6. Needs
   per-leaf-key dedup in the engine (delivery is at-least-once) and a versioned count object outside the tiles.
2. **Exact locality** of the chart partition under incremental folds: measured (0.8 m hall, 2.65 m field), not proved.
3. **Corridor optimality:** the corridor search is not proved shortest (worst 1.40 x on 840 real pairs, p90 1.00). Under an overlay
   the search covers the whole graph.
4. **Chart 4-connectivity** is tested, not proved.
5. **Width evidence and F-7:** office03's only inter-part route crosses a 0.10 m link of 3.0 m. The engine reports it; whether it is a
   real passage or a driven-evidence sliver is open (no controller claim is made).
6. **Real data:** all evidence is from one site (8 indoor office floors). Corridors, doors and outdoor ground are synthetic only.
7. **Islands:** charts outside the largest link component are kept and marked (decision: keep). Their cause: free cells touching the
   rest only at a corner (routing is 4-connected).
8. **Dead plane:** `OwnerRaster.dependent` is always all-False and still served; removal waits for the client's agreement.
9. **Stale CIMBI docs** (list in section 7).

## 6. Gaps, LACI side

1. **Lazy planner tie alignment.** `lazy_plan.py` must choose the same route as the engine (integer cost, then (y, x)). Done on
   branch `cleanup/lazy-tie-rule` at commit `b997c66` (off `a93a696`, not pushed): aligned to integer cost then (y, x), the 5 xfails removed,
   `SCENE_GRAPH_PLANNER_MODE` defaults to `lazy` (`full_load` kept; an unknown value logs a warning). Verified: lazy equality, lazy plan
   and cache-lazy 130 passed; scene graph and planner subset 292 passed, 3 skipped (MinIO). NOT yet run: the Docker e2e in lazy mode
   and the pydantic 1.x unit suite. Production stays `full_load`. Review and merge: LACI.
2. **Width gate.** Off. Not validated against the live controller (the 30 degree turn rule is an assumption; see `runbook.md`
   section 1, "Width gate"). Needs a real factsheet for the two robots without one, and a live comparison before it is switched on.
3. **Planner flag on production** runs the scene-graph route with the legacy planner as fallback; coverage of the fallbacks has to
   be watched (`runbook.md` section 7). Canary history (LACI, all UTC, 2026-10-13):
   * worker first enabled with only office8 switched on, the other maps `scene_graph_enabled=false`;
   * office7 re-enabled next, then all 8 office maps together on the user's go; Hospital and the two test maps stay off;
   * planner flag turned on after the user's "i allow planner flag" (recreate at 16:16); pre-warm and cache-size fix at 16:19;
   * observed so far: one office7 `/api/v1/plan` at 16:25:07 (plan only), no fallback line logged, so served by the chart planner by
     inference. Fallback-reason coverage is still UNOBSERVED.
   * Gap: `GET /stats` on the planner drops the `scene_graph` outcomes block (StatsResponse filters it), so served and fallback counts
     cannot be read from outside. A small fix, not made.
4. **Client leftovers:** `meta.params` read (the client must not read removed parameters) and the dead `dependent` plane. On hold
   until the client agrees.
5. **Checker in the live image:** `a93a696` is deployed; the expectations file is not in the image until `63b54e4` is merged and
   graph-builder rebuilt (until then the `--expect` run needs `docker cp tools/scene_graph_expected.json` into the container). The
   docs branch adds the file and the rules. Merge to `main` waits for the user's go.
6. **Production checks still open:** whether the factsheets for the two robots are to be added (default footprint until then), and the
   `default_r_d` counter review.
7. **Occupancy view proposal** (`occupancy_view_proposal.md`, untracked in the shared worktree, LACI's draft): not decided.

## 7. Documents: what is current, and what is stale

Current and authoritative:
* `README.md` (this file), `formulation.md`, `audit_ledger.md`, `runbook.md` (operations, section 6a islands and the checker),
  `planner_upgrade.md` and `planner_findings_2026-10-13.md` (planner side).

Stale, to be corrected (not rewritten here):
* `handoff_next_session.md`: lists steps already done (merge, generation, pinned run); its "remaining steps" are out of date.
* `integration.md`: has pre-Morse statements (format bump "TODO", "not yet run on a production stack", older sight-range and dependents
  text). The route cost/tie section added in `63b54e4` is current.
* `handoff_laci_morse.md`: expected-count table and the route/cost section are current; the "minor items" list at the top is
  partly done.
* `edge_stats_on_charts_design.md`: a draft, untracked; not a decision.

## 8. How to resume (new person)

1. Read section 1 and `formulation.md`. The statements marked EXACT are the ones to rely on; TESTED and HEURISTIC are not.
2. Run the unit suites: `tests/unit/scene_graph` (host, 202 passed, 3 skipped at `7655b10`; re-count on the branch) and `scripts/run_unit_tests_pinned.sh tests/unit -m unit` (the
   production pins; scipy must be in the image).
3. E2E on the throw-away stack: `tools/scene_graph_e2e/run.sh` (needs docker and the pinned venv).
4. Read-only prod check: `docker exec <graph-builder> python -m tools.scene_graph_check_generation --all --expect tools/scene_graph_expected.json`.
5. Do not write to production (rebuild, generation, flags) without the user's explicit go in the session; the operational steps are
   in `runbook.md` sections 2-4.
