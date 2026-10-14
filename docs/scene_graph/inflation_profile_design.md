# Planner price profile: single-pass design (proposal, 2026-10-14)

Status: **proposal, nothing implemented or deployed.** Written by the lattice_cimbi session with the planner owner's session
(cloud-server-e0, "Laci"). It replaces the linear hinge to the turning radius (`route_safety.md`, live since 2026-10-13) by the
robot's own inflation profile, drops the route-level two-pass floor, and adds a waypoint rule. The exactness proofs of
`route_safety.md` (S4-S11) are unchanged; this file lists what changes. Evidence is in section 9 and reproducible with the scripts in
`tools/scene_graph_sim/research/inflation/`.

## 1. What changes

| | Live today (662bfe8, kappa 1) | This proposal |
|---|---|---|
| Price profile | `phi(g) = (r_c - g)+ / (r_c - r)`, linear, ends at r_c = 0.354 m | `phi(g) = exp(-k (g - r))` for `g < reach`, 0 beyond; k, reach, r from the robot |
| Below the inscribed radius | priced by the same linear function (steeper) | the exponential **continues**; bounded by `exp(k r)` (4.48 for k = 6, r = 0.25) at g = 0 |
| Hard floor | none at r (witness cells with g < r are in F) | **no route-level floor**; a **waypoint-level** floor instead (section 4) |
| Weight | `ceil(len/1e-4 (1 + kappa mean deficit) - 1e-6)` | `ceil(len/1e-4 (1 + kappa mean phi) - 1e-6)` |
| kappa | 1.0, fixed | fleet default 0.75; per (map, robot) value by the budget sweep, capped at 2 |
| 1 m cap, near-r no-pricing rule | present | retired while `reach <= 1.0 m` (the clearance cap of the lazy reader) |

## 2. The profile

* `g(c)`: physical clearance of a lattice cell (distance to the nearest blocked or unknown cell), as today; the reader's clearance
  tile is exact up to its cap (`_CLR_CAP_M = 1.0`), so `reach <= 1.0` is required, 0.8 fits.
* `phi(g) = exp(-k (g - r))` for `g < reach`, else 0. For `g < r` the same formula applies: there is no kink and no second regime,
  so a witness cell is priced consistently with every other cell. Largest value `exp(k r)`; weights stay finite integers and
  `w >= ceil(len/1e-4)` (S7), so Dijkstra stays exact and the ellipse bound `L <= J * 1e-4` (S10) holds.
* The step from `phi(reach)` to 0 (0.037 for k = 6, reach 0.8, r = 0.25) is the robot's own: Nav2's inflation layer has it too.
  A continuous variant (`(phi - phi(reach)) / (1 - phi(reach))`) is possible and is not proposed: it would no longer match the
  robot's profile.
* Edge weight `w = max(ceil(len_units (1 + kappa (phi_a + phi_b)/2) - 1e-6), 1)`, `len_units = len / 1e-4`.
* Robot cost used for diagnostics only: `min(253, 252 phi(g))`, the robot's inflation cost.

## 3. Parameters per robot (tiers)

| Tier | Source | Gives | Used when |
|---|---|---|---|
| 1 geometry | factsheet length / width now, footprint polygon when present | `r_in` (inscribed radius of the footprint at its reference point), `r_c` (largest vertex distance) | always |
| 2 robot costmap | the robot's own **global** costmap (the one Smac 2D plans on) | `reach` = inflation radius, `k` = cost scaling factor, `r_global` = its inscribed radius (footprint inscribed radius + `footprint_padding`) | when readable |
| 3 fallback | geometry only | the geometric hinge `r_in -> r_c` (today's live behaviour) | tier 2 unknown; no invented numbers |
| 0 map | `SceneParams.robot_half_side` | `r_in` | footprint unknown |

Stored per robot as `planner_profile = {r_in, r_c, reach, k, source}`; the plan's pricing record carries `source`.
First robot (confirmed by Laci): r_in 0.25, r_c 0.35 (0.354 geometric), reach 0.8, k 6.0 (Nav2 source defaults are 0.55 and 10.0;
the live values are the ones to use).
Per-robot hard floor: the map's graph F is built for the map's `r`. A robot with a larger `r_in` masks cells with `g < r_in`
at plan time (clearance is available in the reader); a smaller robot cannot use cells outside F. Long robots are a future item.

## 4. The waypoint rule (replaces the two-pass floor)

The robot receives only waypoints (VDA5050 nodes) and plans its own leg to the next one with Smac 2D on its rolling local costmap;
MPPI tracks that path. The route price therefore acts on the robot through waypoint positions, spacing and door choice. The dense
lattice path is display data (`plan trajectory`, 7c34b3c) and is not driven.

**Rule.** No planner-placed waypoint sits on a cell with `g < r_in + h/sqrt2` (`h` = lattice step; 0.321 m for r_in 0.25). The margin
`h/sqrt2` is the lattice clearance quantisation (S12): a lattice value at or above it guarantees `g_continuum >= r_in`.
At the robot's inflation `252 exp(-k (r_in + h/sqrt2 - r_in)) = 252 exp(-0.424) = 165 < 253`, so the leg goal is a valid
Smac cell with margin.

**Algorithm** (inside the cost-aware pull, per kept vertex):

1. Candidate vertices are the points of the lattice path. A candidate `p_k` is admissible only if `g(p_k) >= r_in + h/sqrt2`.
2. From the kept point `p_i` take the farthest admissible `p_k` that is visible, within the gap cap and no dearer than the
   lattice sub-path (`J(segment) <= J(sub-path)`), exactly as today. The change is the admissibility test on `p_k`.
3. A vertex that falls on a witness stretch is therefore **shifted along the lattice path** to the nearest admissible cell
   before or after it; the segment test keeps visibility and the price test keeps `J(final) <= J(lattice)` (A7).
4. If no admissible cell exists between two kept points inside the gap cap, the waypoint is **skipped** (nothing is inserted); the
   stretch is left to the robot's leg planner. A witness stretch longer than the cap cannot be skipped and is reported
   (`pricing.waypoint_floor_exceeded`).
5. Never moved: the robot's start pose, the operator's goal and operator waypoints. An operator waypoint or goal with
   `g < r_in + h/sqrt2` is kept and flagged in the pricing record (`operator_waypoint_below_floor`); the goal snap may move a goal
   to the nearest admissible cell inside `SCENE_GRAPH_GOAL_SNAP_M` only where the operator allows snapping.
6. Planned waypoints inserted by the gap fill (`node_policy_plan`) follow the same rule.

**Interaction with the robot's leg.** Smac 2D rejects cells at cost >= 253 (within the inscribed radius of a lethal cell it
observes); it is cost-aware on the same inflation profile, so inside a leg it already prefers room. A waypoint on a witness cell
is the one place the cloud can hand the robot an invalid goal (the 2026-10-13 `GOAL_BLOCKED`, at 0.35 m, was a lethal the stored map
did not have, not this case). Shifting or skipping such waypoints keeps every goal valid and leaves the crossing of a witness
stretch, if the live lethal is still there, to Smac; if the lethal is stale the robot's costmap clears it and the leg passes.

## 5. kappa and the budget

* Budget statistic (decided): **mean length increase <= 5%** against the kappa 0 route (the extra driving per mission, additive,
  stable at n = 120); tail guard **p90 <= 10%**.
* Fleet default **0.75** meets both on all four maps (office8 mean 3.8%, p90 7.5%; office7 4.6%, 9.2%; office6 4.6%, 9.2%;
  Hospital 0.9%, 4.7%) and reduces robot cost per metre by 39 / 39 / 39 / 60%.
* Per (map, robot profile): the largest grid value (0.25, 0.5, 0.75, 1, 1.5, 2) meeting both rules, capped at 2.
  A sweep takes seconds per map; it is run when the graph version or the robot profile changes and stored with the profile.

## 6. Gates (changes to the acceptance harness)

* A1-A7, B1, B2: unchanged statements (valid for any phi >= 0). A1 reduction becomes "kappa 0 equals the unpriced route".
* New W1: no planner-placed waypoint below `r_in + h/sqrt2` (endpoints and operator waypoints excepted), on all maps.
* New W2: the shortened or shifted polyline still satisfies A7 (`J(final) <= J(lattice)`).
* New P1: `phi` is continuous at `r` (no step), monotone non-increasing, `phi(0) = exp(k r)`.
* C3 cold-cache gate unchanged; reach 0.8 needs the halo of the 1.0 m cap, which exists.

## 7. Pricing record

Per plan: kappa, margin source, profile source, `r_in`, `r_c`, `reach`, `k`, cost, exactness, tiles (as today), plus
`waypoints_shifted`, `waypoints_skipped`, `operator_waypoint_below_floor`.

## 8. Separate engine item (not part of this change): lethal marks inside the swept body

Observation (office8, export of 2026-10-13, engine F): 91 lattice cells of F have `g < 0.25 m` (witness cells). All 91 have a
**lethal** nearest blocker (none unknown), median g 0.20 m (0.05 to 0.22). For **52 of 91 (57%)** the nearest lethal cell lies inside
the driven body-covered image S: the stored map calls lethal a cell the robot's own body swept. The engine keeps reported-blocked
cells blocked (`_build_F`), so those cells stay near-lethal for the planner. Proposed engine item: decide, per cell, whether a
lethal mark inside S that is older than the drive is stale evidence (clear it) or newer (keep it); measure the effect on F, on the
witness count (office8 91, office7 82, office6 128, Hospital 31) and on the 2-17 of 120 pairs that need witness cells. It changes
the format's input rule, so it is CIMBI's change and needs a new generation. Do not fix it in the profile change.

## 9. Evidence and what it does not show

All numbers: 120 random F pairs >= 3 m per map (seed 7), stored scene graph + occupancy read through the cloud read API on
2026-10-14 (office8 v2, office7 v1, office6 v2, Hospital v1), clearance with unknown counted as blocked.

| Result | Value | Script |
|---|---|---|
| kappa sweep, k 6.0, reach 0.8 (robot cost per metre, p90 length increase, share < 0.35 m) | office8 kappa 1: 55.1, 9.7%, 6.3% (kappa 0: 101.1, 0, 23.7%); other maps in the sweep output | `sweep.py` |
| second pass needed | 14 / 6 / 10 / 2% (office8 / 7 / 6 / Hospital) | `two_pass.py` |
| floor changes the route (kappa 1) | 8 / 0 / 0 / 0 of 120; J +5%, length +16% median (+20% max) where it does | `two_pass.py` |
| search time per plan | 0.3 ms (office), 1.4-1.9 ms (Hospital); worst case two-pass 0.9 / 5.4 ms | `two_pass.py` |
| budget rules | table in the harness output of `budget.py` | `budget.py` |
| live run, 4 completed missions, office7 | driven path within median 0.13-0.17 m of the waypoint polyline; length within 0.35 m of obstacles: planned 1.71 m, driven 0.71 m | inline, 2026-10-14 |

Not shown: robot outcomes (stops, recoveries) as a function of the profile; behaviour on operational pairs; the robot's rolling
window, Smac 2D, MPPI and goal-tolerance settings (section 10). The scripts expect the maps JSON that `extract.py` and
`build_data.py` build from the local read API; the data is not stored in the repository.

## 10. Robot values needed

Settled, no more needed: inflation radius 0.8 m, cost scaling factor 6.0, inscribed radius 0.25 m, circumscribed radius 0.35 m,
Smac 2D then MPPI (confirmed by the planner owner).

Still needed, from the robot's Nav2 configuration:

1. **Local costmap rolling window** width and height (m) and resolution: to confirm the 5 m waypoint gap cap fits inside it.
2. **Which costmap the inflation values belong to** (local rolling or global) and whether Smac 2D plans on that one.
3. Optional: `xy_goal_tolerance` of the goal checker (how strictly the robot reaches a waypoint).

Not needed for this change (an earlier version of this section listed about fifty parameters; retracted 2026-10-14): MPPI critic
weights and horizon, Smac 2D internals, obstacle-layer ranges and clearing, `inflate_unknown`, skip / recovery / retry behaviour,
speed limits. They would only help to *predict* how far the driven path changes; the change does not depend on that, and the
2026-10-13 run (driven path within a median 0.13-0.17 m of the waypoint polyline) already bounds it.

## 11. Facts from the robot's navstack configuration (repository, not the live robot)

Source: `deployment_ws/src/sati_ros_navstack/sati_nav_stack_bringup/config/nav2/` (`tier1/navigation_common.yaml`,
`tier1/navigation_sati.yaml`, `tier2/scratch_odin_critic_integration.yaml`). The values Laci gave (inflation radius 0.8, scaling 6.0)
occur in the **scratch Odin override** (a "scratch experiment, untested on hardware" layer, loaded by default by the Odin launch),
not in the tier-1 files (inflation radius 0.55, footprint 0.76 x 0.68). Whether the live robot runs that override is to be confirmed.
In that override:

| Item | Value | Consequence |
|---|---|---|
| Which costmap Smac 2D plans on | the **global** costmap (planner_server owns it); a point robot, cell cost >= 253 blocked, `allow_unknown: false` | answers Laci's question; inflation 0.8 / 6.0 in the global costmap is the one that matters |
| Inscribed radius of the global costmap | **0.30 m**: footprint +-0.25 plus `footprint_padding: 0.05` (NS-58); the local costmap and MPPI keep 0.25 | the profile starts at `g = 0.30`, not 0.25, for the planner's view; Smac refuses cells within 0.30 m of lethal |
| Unknown cells | blocked for Smac (`allow_unknown false`, plus an "unknown ring" of 0.31 m near the robot) | agrees with our convention (unknown counted blocked in `g`) |
| Smac 2D height | `cost_travel_multiplier: 5.0` (default 2.0): a cell's step costs `1 + 5 * cost/252` | the robot's own kappa inside a leg is 5; the config says it prefers detours up to about 2x over skimming a wall |
| Planner `tolerance` | 0.5 m (`tier1/navigation_common.yaml`) | a goal on an invalid cell is tolerated within 0.5 m |
| Goal checker `xy_goal_tolerance` | 0.5 m in the repository (`general_goal_checker`); 0.35 m according to the planner owner (server side) | a waypoint counts as reached within 0.35 to 0.5 m |
| Body reference | `base_body` (centre of the 0.5 m body), axle frame offset handled by the stack | consistent with centre-based clearance |

### Waypoint rule against these tolerances (`wp_rule.py`, kappa 0.75, 120 routes per map, interior waypoints of the pulled route)

| Floor | office8 | office7 | office6 | Hospital |
|---|---|---|---|---|
| 0.30 m (robot's global inscribed): waypoints below it | 14 / 626 (2.2%) | 11 / 684 (1.6%) | 24 / 692 (3.5%) | 0 / 428 |
| 0.321 m (design: 0.25 + h/sqrt2) | 21 (3.4%) | 27 (3.9%) | 55 (7.9%) | 0 |
| 0.371 m (0.30 + h/sqrt2) | 54 (8.6%) | 84 (12.3%) | 104 (15.0%) | 2 (0.5%) |
| shift to the nearest admissible path cell, floor 0.30: median / p90 / max | 0.14 / 0.32 / 0.36 m | 0.10 / 0.30 / 0.30 | 0.20 / 0.41 / 1.20 | none |
| of those shifts, within 0.35 m / within 0.5 m | 86% / 100% | 100% / 100% | 83% / 92% | none |

Reading: the shift the rule makes is, for 83-100% of the affected waypoints, smaller than the robot's own goal tolerance, and Smac's
planner tolerance (0.5 m) already accepts a goal on an invalid cell within 0.5 m. The rule therefore changes the robot's driven path
for few waypoints (shifts above 0.5 m: 0-8% of the affected ones, about 0.1% of all waypoints). Its value is a guard, not a driver.
Recommendation to the planner owner: either keep it as a guard with the floor taken from the robot's own `r_global` (tier 2), or
record the count in the pricing record without moving anything and revisit when recoveries or `GOAL_BLOCKED` events correlate with
waypoints below the floor. Also noted: the kappa that matches the robot's own Smac (5.0) is far above the budget-derived 0.75; the
two act at different levels (inside a leg versus which doors and where waypoints go) and need not be equal.
