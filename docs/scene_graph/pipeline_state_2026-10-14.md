# Scene graph pipeline: state of 2026-10-14 (after the clearance-pricing integration)

> **Status: CIMBI semi-ready, scene graph + lattice planner.** All 9 real maps are in the graph (8 office maps and
> Hospital), the lazy chart planner with clearance pricing is deployed and verified, and the checker passes.
> Further live runs are expected; improvement is not ruled out, but no bottleneck is tracked at present. The open items
> of section 6 are candidates, not blockers.

Written by the lattice_cimbi session (CIMBI engine + LACI planner side), at the end of the 2026-10-13 / 14 integration.
Every number below was read from a command or a log in that session; "not verified" says what was not.
Definitions and derivations of the planner objective are in `route_safety.md`; the deploy rules are in `runbook.md`
("Deploying"). This file is a state record, not a design.

## 1. What runs where

| Service | Deployed commit | Source | Note |
|---|---|---|---|
| mission-planner-service | `662bfe8` since 2026-10-13 23:30:44Z (`/stats` reports `build_sha`) | `scripts/deploy.sh` from `origin/main`, log `~/.cache/satina-deploy.log` | lazy planner only; clearance pricing on, `SCENE_GRAPH_SAFETY_KAPPA=1.0`; width check off |
| api-delegation-service | `b48b7ad` since 21:13:34Z | deploy log | occupancy view read API (another session's branch) |
| graph-builder-service, mission-dispatch | not in the deploy log (started about 3 h before this write-up) | not verified | graph checker passes on 9 of 9 maps (section 3) |

`origin/main` = the planner commit above; local and remote were identical after the push of 2026-10-14 01:23 CEST
(13 commits: route-safety v2, v3, the occupancy view, the deploy chain).

## 2. The planner objective (live since 20:44Z, v3 since 23:24Z)

Edge cost = length x (1 + mu x mean deficit), deficit = max(0, r_c - clearance), r_c = planning robot half-diagonal
(fallback r x sqrt2), mu = kappa / (r_c - r), kappa = 1 (`SCENE_GRAPH_SAFETY_KAPPA`; 0 is today's route exactly).
Priced off when r_c - r <= h/sqrt2 or r_c above the 1.0 m clearance cap. Waypoint shortcuts only if not costlier by the
same measure. Acceptance (office8, 150 pairs, through the lazy path, `tools/scene_graph_sim/accept_route_safety.py`):
all 13 gates pass (A1-A7, B1-B2, C1-C3 and the cold-cache C3c); baseline vs priced: median minimum clearance
0.209 -> 0.292 m, routes under the inscribed radius 89 -> 33, unsafe length 1.41 -> 0.62 m, length ratio median 1.010 /
p90 1.064 / max 1.233 (vertex-sampled). The pinned full unit suite passed on the merged tree (4536 passed) and on
`2938605` (4537 passed, 4 skipped).

## 3. Pipeline health at the time of writing

* `tools.scene_graph_check_generation --all --expect tools/scene_graph_expected.json` (graph-builder image, read-only):
  **8 of 8 maps passed**; FORMAT 4, generation 1 on every map.
* Charts / links / levels, office7 (map of the 2026-10-13 run): 14 / 17 / 3, no islands; office8: 12 / 15 / 3.
  Maps with islands (1-6 isolated cells, pinned by the expected file): 4 of 8.
* **Hospital** (an open simulation, 35 nodes with costmaps) was enabled on 2026-10-13 23:56Z by removing its
  `status.scene_graph_enabled=false` flag (`costmap_stored_ms` was already present on all 35 nodes, so no backfill).
  The worker folded it from scratch; the checker passes (`--map Hospital`): FORMAT 4, pipeline `morse/2`, version 1,
  generation 1, **54 charts, 97 links, 4 levels**, no problems; driven fraction mean 0.068 (34 of 54 charts were never
  driven, expected for a simulation walked once), 23 islands of 7261 cells (regions seen but not connected to the driven
  component), width min / median 0.64 / 2.82 m, checker route 11 charts, 17.0 m. The worker folds its leaves with the
  default r_d 0.25 m because the simulated robot (`masked-frigatebird`) has no factsheet row. Not yet pinned in
  `tools/scene_graph_expected.json` (a graph-builder image change); no Hospital plan has been served yet.
* Planner (`/stats`, at 23:30Z, build_sha 662bfe8): 8 graphs warmed, no loads in flight, no backed-off map, 0 priced plans since the restart.

## 4. The live run of 2026-10-13 (formidable-peacock, map office7, planner 6879628, kappa 1)

* 8 navigate missions 20:53-21:44Z, 16 plans, **all served by the chart planner, no fallback, no error**; end-to-end
  plan latency (robot status to mission created) 22-59 ms, median 34 ms (n = 8, warm).
* 4 completed (22-62 s, 0 recoveries, 0 blocks); 3 cancelled by the operator; 1 failed (safety reflex latched,
  `clearance=0.219 m`, 2 recoveries); 1 existing mission (`RegionTest`: 2 recoveries, 3 blocks, not from this planner).
* Planned waypoints on the stored occupancy of office7 (decoded through the occupancy view API): 36 of 36 on free cells;
  minimum clearance per route 0.25-0.30 m; 18 % of the 44.5 m planned length below r_c; about 8-9 of 36 waypoints
  below r_c (the turn-vertex rule is not enforced; see section 6).
* `NAV.GOAL_BLOCKED` at waypoint (-0.65, 3.55) of mission 205858: stored map free, clearance 0.35 m, the robot's own
  navigation held the goal blocked. Cause per the map owner: the robot costmap did not mark a lethal region (stale free
  evidence). This is a map-evidence failure; clearance pricing cannot see it.
* Mission 210116 stayed 42 min with 0 legs: robot side (`navigationNotReady`, pose LOST, TF monitor silent).
* Not recorded in this run: per-plan pricing (kappa, margin, cost, exactness; added in v3, live from 23:24Z), a
  kappa = 0 baseline on office7.

## 5. What is recorded for the next run

* Per plan (v3): `pricing` in the plan response and one `Priced plan ...` log line (kappa, margin, margin source, cost,
  exactness, tiles, map, graph version); `/stats` counts priced plans and inexact ones.
* Per run: `run_legs` (duration, planned and straight length, recoveries, recovery seconds, blocks), `mission_runs`
  planned path, `NAV.*` events, track at level `track`. These are reachable through the local API without database
  credentials (`/api/v1/runs`, `/api/v1/runs/{id}/legs`).
* Attribution: the deploy log gives sha and time; after `662bfe8` `/stats` reports `build_sha`.

## 6. Open, ranked (nothing here is implemented)

1. **Stale free evidence** (the 2026-10-13 goal-blocked case): planes `B` (latest blocked time), `T`, `ever_blocked`
   exist and are not priced; rate pricing is designed and agreed (`formulation.md` section 6), not built.
2. **Turn vertices:** the derived rule `g >= r_c` at every heading change is not enforced; the price is soft.
3. **Calibration of the risk law** from `run_legs` and the NAV events against the clearance profile of the legs
   (kappa 1 is a plateau choice, not measured). Data exist through the API; the join with node poses is not built.
4. **No per-request A/B:** kappa is a process setting; a baseline needs a restart with `SCENE_GRAPH_SAFETY_KAPPA=0`.
5. **Tolerance:** one safety reflex at 0.219 m (planned minimum 0.25 m) suggests tracking error of a few cm; one event,
   sensor definition not verified, tolerance removed from the formulation.
6. **Deploy chain:** no functional plan smoke test; build identity only for the planner; other services' images are not
   labelled.
7. **Housekeeping (Hospital is done, section 3):** 9 untracked leftover directories under `.claude/worktrees` (no `.git`, about 110 MB, contents not
   inspected); the 4 unmerged branches `worktree-agent-a0e1c9ae` (a dispatch logging commit), `worktree-agent-a4b44503`
   (superseded designs), `spike/splat-2026-10-13`, and three patch-equivalent branches checked out by other sessions;
   the uncommitted `docker_compose/.env` still has the stale `SCENE_GRAPH_PLANNER_MODE=lazy` lines; the office8 export
   used by the benchmark lives only in the gitignored data directory of the `cloud_server_scene_graph` worktree.

## 7. Deploy record of the integration

| Time (UTC) | Commit | Result |
|---|---|---|
| 2026-10-13 20:44 | `6879628` | planner deployed with `--no-pull`; healthy; first priced plans served |
| 2026-10-13 23:24 | `2938605` | planner deployed from `origin/main` with the gate; healthy; tags pruned; the identity check **failed**: `/stats` did not report `build_sha` (the response model dropped it) |
| 2026-10-13 23:30 | `662bfe8` | fix: `build_sha` declared in `StatsResponse`; test requires every `get_stats()` key to be a model field. Pushed, full pinned suite 4537 passed / 4 skipped, deployed from `origin/main`, healthy, `/stats` reports `build_sha` 662bfe8, 1 more rollback tag pruned |

## 8. Addendum (later on 2026-10-14): what was merged and deployed after this record

Written by the lattice_cimbi session. Sections 1 to 7 above are the state at the time of the first write-up and are kept as written.

| Time (UTC) | Commit | Service | Result |
|---|---|---|---|
| 20:34 | `cac9f03` | mission-planner-service | the exponential price tier in the code, off (`SCENE_GRAPH_INFLATION_*` empty); healthy; `/stats` `build_sha` `cac9f03` |
| 20:51 | `a4a0728` | mission-planner-service | close-evidence rule in the engine and the tile readers, off (`SCENE_GRAPH_NEAR_*` empty); healthy; `build_sha` `a4a0728` |
| 20:52 | `a4a0728` | graph-builder-service | healthy; checker 8 of 9: `office7` failed its pins (16 charts, 15 links, 3 islands against 14 / 17 / 0, stored version 5: the map grew in the live run, no code or params change, pipeline `morse/2`) |
| after | the commit that re-pins office7 and Hospital | graph-builder-service | see `~/.cache/satina-deploy.log` for the sha and the checker result |

* Pinned unit suite on `a4a0728`: 4673 passed, 4 skipped (production pins, default selection); the acceptance harness on office8
  (`accept_route_safety.py`, 150 pairs, kappa 1): all gates pass, unchanged.
* The expectations file now pins `office7` at 16 / 15 / 3 and `Hospital` at 54 / 97 / 23 (a deliberate re-pin: the data changed, the code did not).
* Open item 1 of section 6 (stale free evidence): the mechanism is built and off by default (`close_evidence_and_turn_cost_design.md`
  part A); what remains is the decision to switch it on (a rebuild of every map). New open items: the turn cost (part B) and the waypoint
  merge (part C) are designed, not built; the robot's speeds are read from the registration factsheet (B.6), the nominal values are to be added.
* Branch hygiene of 2026-10-14: the branches `exp/inflation-profile`, `design/inflation-profile`, `design/close-evidence-turn-cost`,
  `proto/close-evidence-plane` and `integrate/close-evidence` are merged into `main` and removed.

## 9. Addendum (2026-10-15): live features, generation 3, what is deployed

| Time (UTC) | Commit | Service | What |
|---|---|---|---|
| 2026-10-14 21:43 | `1beabfd` | planner, graph-builder | away-from-walls filter in the code, off |
| 2026-10-14 21:57 | `1beabfd` | graph-builder | close evidence on (env set by the user): every map rebuilt to generation 2 |
| 2026-10-14 22:07 to 22:23 | `d071bd6`, `72f056a` | planner | turn cost (on, env set by the user), waypoint budget 0.35 m (on) |
| 2026-10-15 01:15 | `e17d22c` | graph-builder, planner | the lethal-from-within-range rule: every map rebuilt to generation 3 (`morse/2+near4`) |
| 2026-10-15 01:29 | `828a442` | graph-builder | the ArangoDB meta write is a REPLACE (a rebuild left a stale `pending` record on every map; cleared with `tools/scene_graph_clear_stale_pending.py`) |
| 2026-10-15 01:37 | `eb4cf5c` | planner, graph-builder | waypoint candidate thinning (long open routes 618 -> 74 ms), apex candidates (off), checker pins re-pinned; checker 9 of 9 |

* Pinned unit suite at `eb4cf5c`: 4968 passed, 4 skipped. Acceptance gates on office8 pass (A1-A7, B, C, W1-W5, N1).
* Navigable change against the rule-off graph at generation 3: Hospital and office5 0%, office01 -0.6%, Officemap -3.2%, office03 -6.5%,
  office04 -7.4%, office8 -7.9%, office7 -9.1%, office6 -17.0%. Only office8 was analysed at capture level (the leaf export exists there only).
* Open: `office6` -17% and `office7` -9.1% are not understood at capture level (export their leaves with
  `tools/scene_graph_sim/export_live.py` and run `close_evidence_replay.py`); the 14 unconfirmed office8 components; no robot-outcome data yet;
  order trimming, replan trigger and the factsheets of two robots are proposals (`robot_interface_proposals.md`).

