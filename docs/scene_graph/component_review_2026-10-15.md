# Scene graph and planner: component review, 2026-10-15

Purpose: for each component on main, say what it does, whether the choice is principled, what could be reformulated, and whether
it stays. Companion to `handover_2026-10-15.md`. The impartial reviewer's report (read-only, code and docs) is the base; the
grep checks listed under "verified" were run for this note. Nothing here was run, so test results and timings are not part of
the verdicts.

## How a component is judged

- **P1 Grounding.** Is the rule derived from the robot's geometry or the sensor, with its constants named and their source given?
- **P2 Evidence.** Has it been measured on the routes it changes, with a metric and a reference?
- **P3 Cost.** What does it cost in stored format, rebuilds, code paths and runtime?
- **P4 Necessity.** Is there a live caller, a switch with a decision behind it, and no duplicate?

Verdicts: **keep** (principled, live), **keep and clean** (principled, code needs work), **reformulate** (the idea is sound, the
form is not), **move out** (no decision yet, or the evidence is missing), **remove** (rejected, unused, or no effect measured).

## 1. Observation and state (keep)

| Part | What it does | Verdict and reason |
|---|---|---|
| Leaf window and robot pose (`scene_adapter.to_leaf`) | Each capture gives free, lethal or unknown per 5 cm cell, with a capture stamp. | Keep. The inputs are the sensor's; nothing is invented. |
| Per-cell latest-value fold (`V`, `Fn`, `Bn`, stamps as max) | The newest report decides a cell; the order of arrival does not matter. | Keep. A max over capture times is order-independent (P1). Verified: `_fold_into` takes maxima. |

## 2. Close evidence and the navigable structure

| Part | What it does | Verdict and reason |
|---|---|---|
| Close-evidence hold at 1.5 m (`engine._build_F`, planes `Fn`, `Bn`, token `+near3/4/5`) | A lethal from within 1.5 m stays blocked until a newer close free. Far reports do not count either way. | **Keep, clean.** The rule is principled (a far report cannot retract a near one, and a near lethal is trusted until contradicted from close). The radius 1.5 m is a chosen value (P1 partial). Reformulate the radius from the depth sensor's usable range or its noise, and record that source. Measured: removes about 10-12% of navigable cells on office 7 and 8 (re-measured in the handover). |
| Away-from-walls filter, 0.25 m (`_away_from_walls`, plane `hold`) | A held component that stays within 0.25 m of other blocked or unknown cells is released, so wall rims do not block the free beside them. | **Keep, reformulate the implementation.** The threshold equals the robot's half side, which is principled. The implementation runs a distance transform per held component on every solve. A single distance transform over the held set gives the same test in one pass. Measured: gives back about 3% of cells. |
| Navigable mask: 0.5 m square fit and the driven body (`_build_F`, `_erode`, `_evidence`) | A node is navigable if the robot's square fits in free cells, joined to what the body covered. | **Keep, decide one point.** The square fit is the robot's physical limit (P1). The driven-body term admits unknown cells the robot covered; that is defensible for the path it drove, but it lets a route cross grey cells. Decide whether that admission is wanted (it is not in the review's list). |
| Charts, links, Morse partition (`morse.py`, `chart_keys.py`, `chart_radius` 2.0 m) | The navigable space is cut into charts for the corridor search. | **Keep.** It is the structure the search relies on, and the 2.0 m radius is a documented parameter. |

## 3. Pricing (the cost field)

| Part | What it does | Verdict and reason |
|---|---|---|
| Clearance as the minimum over each node's 2x2 cells (`clr_price`) | A node is never priced clearer than the cells it covers. | **Keep.** Conservative and principled (P1). It changes prices, not the mask. Evidence: one map in doc F (P2 partial). |
| Hinge price, margin = circumscribed radius (0.354 m), kappa = 1.0 | Price `max(0, 0.354 - clearance)`, edge multiplied by `1 + kappa x mean price / 0.104`. | **Keep, state kappa as a choice.** The margin is geometry (P1). kappa is a chosen constant; its effect on routes has not been measured against a safety target. Reformulate only by measuring kappa against a stated target (for example, wall distance on the 150-pair harness). |
| Exponential tier (`_exp_safety`, `SCENE_GRAPH_INFLATION_*`, reach 0.8 m, k 6.0) | Price `exp(-k (clearance - 0.25))` below 0.8 m. | **Move out** until the robot values are confirmed. Its values come from a colleague's scratch override, not the robot (design doc section 11). The design doc lists the robot's own values as still open (section 10). It doubles the pricing paths. Principled as a profile; not yet grounded in the robot. |

## 4. Route cost and search

| Part | What it does | Verdict and reason |
|---|---|---|
| Turn cost (`turn.py`, exact (cell, heading) search, lambda from the factsheet) | Each heading change costs lambda metres. | **Keep, reformulate the fallback.** The exact search is principled and tested. The operator fallback (0.5 m/rad, from `.env`) is not derived from the robot. Principled version: lambda from the factsheet's speed and angular speed only; without them, no turn cost, or a logged warning. That removes a number no one measured. |
| Waypoint budget (`fewest_waypoints`, `budget_pull`, thinning) | Chooses output waypoints so the priced cost stays within a slack. | **Keep.** It changes the waypoints only, not the route. The slack (0.35 m) is a chosen value; say so in the config comment. |
| Apex candidates (`apex_candidates`, `APEX_TIES_TO_APEX`, DP rank logic) | Fillet-apex alternatives at corners. | **Remove.** The design's own measurement shows no clearance change (0.381 m vs 0.381 m), 11 apex plans in 150 routes, and the plans lose `trajectory`, which `path_efficiency` reads. Nothing justifies the code. |
| Validated-first (`validated.py`, `val` plane, `_route_vf`, `penalise`) | Routes minimise unvalidated edges first (lexicographic, exact), then price. | **Move out.** The objective is exact (the proof in `validated.py` reads sound), but the premise, that space never checked from close should be avoided, is a policy with no safety outcome measured. Measured cost in the design: routes up to 2.4x longer and plan time 654 ms against 84 ms on a sparse world (unverified here). Reformulate as a soft, measured penalty if wanted. Its store token already exists on the deployed maps: removing the plane needs a second rebuild of each rule-on map. |
| Near-field cone (`near_fov_deg`) | Restricts close evidence to a forward cone. | **Remove.** Design A.6f rejected the cone variant, yet the code still carries it in `SceneParams`, `_state_params`, the fold, `config.py:691`, `mission_dispatch_services.yaml:254` and tests. Verified by grep. |
| Route logic in two places (`engine.route` vs `lazy_plan._search`, `_route_vf`, turn selection) | The same weights and search, rebuilt for the engine and for the lazy reader. | **Reformulate: one weight function, two callers.** The equality of the two is held only by tests. `validated.py` is already the shared piece for penalised weights. Extend that pattern to the plain and priced weights. Not a behaviour change, so it can be verified against the existing equality tests. |
| `deficit_cell` (engine, lazy, api) | Exists only as a capability probe beside `price_cell` (`scene_route.py:810`, `:846`). | **Remove when the probe is replaced** by an explicit capability check. Verified by grep. |
| `priced_only` (`scene_route.py:827`) | Strips validated-first from a Safety. | **Rename or remove** with validated-first. The name says it strips prices; it does not. |

## 5. Store, format and rebuilds

| Part | What it does | Verdict and reason |
|---|---|---|
| Pipeline tokens (`morse/2`, `+near3`, `+near4`, `+near5`) and planes (`fn`, `bn`, `hold`, `val`) | The stored format for each rule. | **Reformulate, minimal change.** Each new token forces a rebuild of every rule-on map. The principled rule: one token per format change, decided before deployment, never per experiment. The `+near3/+near4` names are still defined and `PIPELINE_NEAR`, `PIPELINE_NEAR_HOLD` (`store.py:50-51`) are unreferenced (verified by grep). Remove those constants. |
| Memory guard (`engine.py:160`) | States about 17 bytes per cell. | **Fix the number.** The close-evidence planes add about 16 bytes per cell, so the figure is stale (reviewer). |
| Full-canvas passes per solve (`engine.py:56`, `:658`, `:136`) | Distance transforms and masks on the whole canvas each solve. | **Keep, measure before changing.** The incremental box (`_dirty`, `_hold_changed_box`) already limits the work; the reviewer could not time it. |

## 6. Switches and configuration

| Switch | Default | Verdict |
|---|---|---|
| `SCENE_GRAPH_NEAR_RANGE_M` (1.5 m), `SCENE_GRAPH_NEAR_MIN_CLEARANCE_M` (0.25 m) | on in `.env` | Keep. Documented choices with sources. |
| `SCENE_GRAPH_TURN_COST_ENABLED`, `..._LAMBDA_M_PER_RAD` (0.5) | on in `.env` | Keep the switch; drop the operator fallback number (section 4). |
| `SCENE_GRAPH_WAYPOINT_BUDGET_M` (0.35) | on in `.env` | Keep. |
| `SCENE_GRAPH_SAFETY_KAPPA` (1.0) | default | Keep, record as a choice. |
| `SCENE_GRAPH_VALIDATED_FIRST` (false) | off | Move out with the feature. |
| `SCENE_GRAPH_INFLATION_RADIUS_M`, `_SCALING` | unset | Move out with the exponential tier. |
| `SCENE_GRAPH_WAYPOINT_APEX_ENABLED` | off | Remove with apex. |
| `SCENE_GRAPH_NEAR_FOV_DEG` | unset | Remove with the cone. |
| `EFFICIENCY_*` | defaults | Keep with the analytics. |

Principle: a switch exists only with a decision behind it. Off switches without a decision are removed, not kept as options.

## 7. Planner and API outside the scene graph

| Part | Verdict and reason |
|---|---|
| `scene_route.py` (1,427 lines) | **Reformulate: split.** Budget, thinning and apex (about lines 946-1131) to their own module; apex removed. The rest is the planner's glue. |
| `lazy_plan.py` (719 lines) | Keep; the duplication with the engine is in section 4. |
| API analytics: `path_efficiency.py`, `map_series.py`, `series_axis.py`, `drive_stats.py`, `mapping_stats.py`, `map_events.py`, `main.py` routes (about 2.5k lines) | **Keep on main, review separately.** Not planning code, cached and unit-tested per the reviewer. It reads `route.trajectory`, so it depends on the planner's output shape. |

## 8. Tools (not planner code)

| Tool | Verdict |
|---|---|
| `scene_graph_check_generation.py`, `scene_graph_clear_stale_pending.py`, `scene_graph_expected.json` | Keep. Checker and the stale-pending cleanup shipped in the graph-builder image. |
| `accept_route_safety.py` | Keep. The acceptance harness for pricing. |
| `research/inflation/` (budget30/sweep30 near-copies of budget/sweep) | Move out or prune. Verified by the reviewer's diff: 1 and 4 lines apart. |
| `turn_cost_prototype.py`, `turn_cost_restricted.py` | Remove or archive: superseded by `turn.py`. |
| `close_evidence_replay.py`, `close_evidence_route_changes.py`, `close_evidence_engine_check.py` | Archive: one-off analyses; their numbers are in the design doc. |
| `validated_first_live.py` | Goes with validated-first. |
| `outdoor_world*.py` | Keep only as a benchmark, with a note. |
| `branch_viewer/` (uncommitted) | Keep as a tool. Limitation: it plays `route()`, not the production `plan_on_graph`, so its routes are not the planner's output. Say so in its README (done) and in the handover. |

## 9. What is principled, in one list

- Max-over-time fold, the square-fit mask and the circumscribed-radius margin: principled.
- Block-minimum pricing: principled and conservative.
- Exact turn search and the waypoint slack: principled, tested.
- Close-evidence rule: principled in its logic, with a chosen radius to be sourced.
- The validated-first objective: exact, but its premise is a policy not yet tested for safety.
- Chosen constants (1.5 m, 0.25 m, kappa 1.0, lambda 0.5, slack 0.35 m, reach 0.8 m): each needs its source written next to it,
  or a measurement.

## 10. Decisions for the user (in order of cost)

1. Validated-first: keep the dormant `val` plane, or move it out and pay a second rebuild of every rule-on map. First decide its
   behaviour for an unvalidated start or goal.
2. Remove apex and the near-field cone now (no evidence, rejected design). Recommended.
3. Exponential tier: branch until the robot's own Nav2 values are confirmed, or keep it behind its switch.
4. Turn-cost fallback: remove the operator number, or keep it with a measured justification.
5. Analytics API and research tools: keep on main, or split them out of the planner history.
6. Decide whether grey unknown cells under the robot's body may be routed through (section 2).
