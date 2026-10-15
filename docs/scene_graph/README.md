# Scene graph and planning: entry point (state 2026-10-14, `main`; the deployed commit is in `pipeline_state_2026-10-14.md`)

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

The planner prices a route by clearance (`route_safety.md`): `length x (1 + mu x mean deficit)` with the linear hinge, kappa 1; the
exponential profile (`inflation_profile_design.md`) is in the code and off.

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
| Planner: snap, route, pricing, width gate, fallbacks | `packages/services/mission_planner/scene_route.py`, `server.py`, `node_policy_plan.py` | LACI |
| Pricing (`Safety`, `edge_weight`, hinge and exponential tier) | `packages/scene_graph/api.py`, `engine.py` (`_edge_cost`), `lazy_plan.py` | CIMBI + LACI |
| Close-evidence rule (planes `B`, `Fn`; tile reader `effective_v`) | `engine.py` (`_fold_into`, `_build_F`), `store.py`, `store_codec.py` (`effective_v`, `read_planes`) | CIMBI |
| Lazy planner (tile-loaded `PlanGraph`) | `packages/services/mission_planner/lazy_plan.py` (the only planner mode; `full_load` removed 2026-10-13) | LACI |
| Config and flags | `packages/config.py` (`SCENE_GRAPH_*`), `docker_compose/mission_dispatch_services.yaml`, `.env.example` | both |
| Migration | `packages/api/migrations/versions/20261012_01_blocked_geometry.py` | LACI |

Constants that matter: `FORMAT = 4`, `PIPELINE = "morse/2"` (store; `"morse/2+near5"` for a store made with the close-evidence
rule (tile planes `fn`, `bn`, `val`, and `hold` with its away-from-walls filter; on branch `feat/validated-first`, design part G: the live stores are still the
earlier `"morse/2+near3"` / `"morse/2+near4"` until the graph-builder is deployed and rebuilds them), `LAT = 2` (costmap cells per lattice cell, engine), `RES = 0.05` m costmap cell. `SceneParams` = `robot_half_side`,
`lattice`, `chart_radius` and the optional `near_range_m` / `near_fov_deg` / `near_min_clearance_m` (close evidence and its away-from-walls filter, None = off; `api.py`); nothing else is a
scene parameter. The engine's window cap (20 m side) and pose slack (5 m) are validation constants, not tunables.

## 3. Live state (2026-10-15)

The deployed commit of each service, the deploy log and the live run of 2026-10-13 are in `pipeline_state_2026-10-14.md`
(sections 1, 4 and 7, and its addenda); the planner reports its commit as `build_sha` on `GET /stats`.

* Maps: all 9 are in the graph at generation 4 (pipeline `morse/2+near5`, rebuilt 2026-10-15 02:56 UTC); the checker passes 9 of 9 against the re-pinned expectations.
* Planner: the lazy tile planner only; chart route first, the legacy planner as fallback; clearance pricing at `SCENE_GRAPH_SAFETY_KAPPA`
  1.0 (linear hinge); **turn cost on** (`SCENE_GRAPH_TURN_COST_ENABLED`, lambda from the registered `speed_max / angular_speed_max`,
  operator fallback 0.5 m/rad); **waypoint budget 0.35 m on** with candidate thinning; the width gate is off.
* Graph-builder: **close evidence on** (`SCENE_GRAPH_NEAR_RANGE_M` 1.5, `SCENE_GRAPH_NEAR_MIN_CLEARANCE_M` 0.25): a cell is held only on
  lethal reported from within 1.5 m (true evidence), away from walls.
* Pricing: a lattice node is priced at the worst of its four 5 cm cells (block minimum, design F); structure and widths keep the top-left sample (stored data unchanged).
* Off: the exponential price profile, the apex candidates (`SCENE_GRAPH_WAYPOINT_APEX_ENABLED`, measured no effect, deferred), and validated-first planning (`SCENE_GRAPH_VALIDATED_FIRST`, design G: the strict rule has no detour price, see G.6).
* Not built: order trimming and the replan trigger (`robot_interface_proposals.md`), rate pricing (`formulation.md` section 6).
* Rollback: `scripts/deploy.sh rollback <service>` puts the last pre-deploy image tag back (`runbook.md` section 2, "Deploying").

## 4. What is verified, and how

* **Engine:** independent verifier (`chart_verify.py`, from definitions): 0 violations on the 8 real maps at batch sizes 6 and 48,
  on 13 synthetic worlds and random fields. Batching, arrival order and translation do not change the result (tests).
* **Route:** minimal integer cost and the (y, x) tie rule checked against an independent reference (heapq plus DP over the
  shortest-path DAG), including under an overlay (`test_engine.py`).
* **Store:** load equals save, replay does not bump the version, kill and restart give the same leaf set, delete saga leaves
  nothing (rehearsal on the throw-away stack with the real worker on the captured leaves of all 9 maps).
* **E2E harness:** `tools/scene_graph_e2e/run.sh`, 32 passed in both planner modes while both existed (on `4104b8d`; not re-run on `a93a696`).
* **Pinned unit suite:** the full default selection of `scripts/run_unit_tests_pinned.sh` (py3.10, pydantic 1.9.0); a deploy is refused for a commit without a recorded passing run (`runbook.md` section 2, "Deploying"). The counts are in `pipeline_state_2026-10-14.md`.
* **Measurements:** `formulation.md` statements 25-27 (cost, anchor-chain stretch, route quality) and statement 28 (ties).
* **Clearance pricing:** `route_safety.md` (statements 29-31; `tools/scene_graph_sim/accept_route_safety.py`, the acceptance gates, with `--profile exp` for the exponential tier, see `inflation_profile_validation.md`).
* **Close evidence:** `close_evidence_and_turn_cost_design.md` part A; `tests/unit/scene_graph/test_close_evidence.py` (rule off identical, arrival-order independence, semantics, store round trip, tile readers equal the engine); replay and engine reproduction on office8 (`tools/scene_graph_sim/close_evidence_replay.py`, `close_evidence_engine_check.py`).

## 5. Gaps, CIMBI side

1. **Rate pricing / count planes.** Not built (clearance pricing is: `route_safety.md`). Design, with the planner side's conditions:
   `formulation.md` section 6. Needs per-leaf-key dedup in the engine (delivery is at-least-once) and a versioned count object outside the tiles.
2. **Stale free evidence** (a cell reported lethal and later free from far away, the office8 stairs): the close-evidence rule is live
   (`close_evidence_and_turn_cost_design.md` part A). Open there: the 14 unconfirmed components on office8 (one more pass within 1.5 m would
   settle them) and the other maps, which were not analysed at capture level.
3. **Turn cost and waypoint budget.** Built and live (`close_evidence_and_turn_cost_design.md` parts B, C.2). Lambda comes from the robot's
   registration (B.6); two robots lack the fields (`robot_interface_proposals.md` section 4).
4. **Exact locality** of the chart partition under incremental folds: measured (0.8 m hall, 2.65 m field), not proved.
5. **Corridor optimality:** the corridor search is not proved shortest (worst 1.40 x on 840 real pairs, p90 1.00). Under an overlay
   or a price the search covers the whole graph.
6. **Chart 4-connectivity** is tested, not proved.
7. **Width evidence and F-7:** office03's only inter-part route crosses a 0.10 m link of 3.0 m. The engine reports it; whether it is a
   real passage or a driven-evidence sliver is open (no controller claim is made).
8. **Real data:** evidence is from one site (8 indoor office floors and one simulated hospital). Corridors, doors and outdoor ground are
   synthetic only.
9. **Islands:** charts outside the largest link component are kept and marked (decision: keep). Their cause: free cells touching the
   rest only at a corner (routing is 4-connected).
10. **Dead plane:** `OwnerRaster.dependent` is always all-False and still served; removal waits for the client's agreement.
11. **Stale CIMBI docs** (list in section 7).

## 6. Gaps, LACI side

1. **Width gate.** Off. Not validated against the live controller (the 30 degree turn rule is an assumption; see `runbook.md`
   section 1, "Width gate"). Needs a real factsheet for the robots without one, and a live comparison before it is switched on.
2. **Fallback coverage.** The planner flag is on; of the 16 plans of the live run of 2026-10-13 all were served by the chart planner
   (`pipeline_state_2026-10-14.md` section 4). `GET /stats` reports the `scene_graph` outcomes and cache and the `build_sha`; the
   pricing counters (`priced_plans`, `priced_plans_inexact`) are there too. A longer run is the evidence still wanted.
3. **No per-request A/B.** kappa is a process setting; a baseline needs a restart (`pipeline_state_2026-10-14.md` section 6).
4. **Deploy chain.** `scripts/deploy.sh` gates on a recorded test run and verifies the planner's `build_sha`; the other services'
   images are not labelled (`pipeline_state_2026-10-14.md` section 6).
5. **Client leftovers:** `meta.params` read (the client must not read removed parameters) and the dead `dependent` plane. On hold
   until the client agrees.
6. **Production checks open:** the factsheets of the robots without one (default footprint until then, counted in `default_r_d`;
   speed fields are needed for the turn cost, `close_evidence_and_turn_cost_design.md` B.6).
7. **Occupancy view** (`occupancy_view_proposal.md`): the read API is deployed (`pipeline_state_2026-10-14.md` section 1).

## 7. Documents: what is current, and what is stale

Current and authoritative:
* `README.md` (this file), `formulation.md`, `audit_ledger.md`, `runbook.md` (operations, section 6a islands and the checker),
  `planner_upgrade.md` and `planner_findings_2026-10-13.md` (planner side).
* `route_safety.md` (clearance pricing), `inflation_profile_design.md` and `inflation_profile_validation.md` (the exponential tier,
  off), `close_evidence_and_turn_cost_design.md` (close evidence merged and off; turn cost and waypoint merge designed),
  `pipeline_state_2026-10-14.md` (state record: deployed commits, live run, open items, deploy record),
  `robot_interface_proposals.md` (robot interface: waypoint authority, order trimming, replan trigger, factsheets; proposals, nothing built).

Stale, to be corrected (not rewritten here):
* `handoff_next_session.md`: lists steps already done (merge, generation, pinned run); its "remaining steps" are out of date.
* `integration.md`: has pre-Morse statements (format bump "TODO", "not yet run on a production stack", older sight-range and dependents
  text). The route cost/tie section added in `63b54e4` is current.
* `handoff_laci_morse.md`: expected-count table and the route/cost section are current; the "minor items" list at the top is
  partly done.
* `edge_stats_on_charts_design.md`: a draft, untracked; not a decision.

## 8. How to resume (new person)

1. Read section 1 and `formulation.md`. The statements marked EXACT are the ones to rely on; TESTED and HEURISTIC are not.
2. Run the unit suites: `tests/unit/scene_graph` on the host, and `scripts/run_unit_tests_pinned.sh` with no arguments (the production
   pins; scipy must be in the image; arguments replace the default selection).
3. E2E on the throw-away stack: `tools/scene_graph_e2e/run.sh` (needs docker and the pinned venv).
4. Read-only prod check: `docker exec <graph-builder> python -m tools.scene_graph_check_generation --all --expect tools/scene_graph_expected.json`.
5. Do not write to production (rebuild, generation, flags) without the user's explicit go in the session; the operational steps are
   in `runbook.md` sections 2-4.
