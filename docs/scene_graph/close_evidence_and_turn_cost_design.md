# Close evidence for blocked cells, a turn cost for routes, and a waypoint merge (design, 2026-10-14)

Status: **plan and prototype only.** Nothing here is built, merged or deployed; no stored format changes; the exponential
inflation tier stays off. Direction was accepted by Laci for parts A and B; part C is a separate item; part D lists what is open.
Written by the lattice_cimbi session. Every number comes from the two scripts named below, run on the office8 leaf export
(97 leaves, one session of 6 minutes; no other map's leaf export is available to these scripts).

```
python tools/scene_graph_sim/close_evidence_replay.py --data <office8 export> [--rdet 1.5] [--away 0.25]
python tools/scene_graph_sim/turn_cost_prototype.py   --data <office8 export> --pairs 150 --forms
```

Both are read-only research scripts (not the engine's fold, not the production search).

---

## A. Close evidence for blocked cells (stairs and similar)

### A.1 The problem

The costmap marks stairs lethal only from about 1.5 m. A robot that passed at 3 m reports the same cells free. The fold keeps,
per cell, the value of the latest capture from any distance (`engine._fold_into`, ties go to blocked), and `F` is built from that
latest value (`_build_F`: `V < 100`). The plane `B` (latest blocked time) exists but only feeds the `ever_blocked` statistics
channel; it does not gate `F`. So a cell reported lethal once is free again after one later free report from far away.

### A.2 The rule (colleague's proposal, stated so that it stays a fold)

A free report is evidence only from where the sensor could have seen the hazard; a lethal report is evidence from anywhere.
Per cell keep two max-merge planes:

* `B`: latest capture time that reported the cell blocked (exists);
* `Fnear`: latest capture time that reported the cell free **from a capture pose within `r_det`** (new plane).

The cell is blocked while `B > Fnear` (ties: blocked, the existing tie rule); otherwise the existing latest-value rule applies.
Both planes merge by max, so the fold stays commutative, associative and idempotent: leaves may still arrive in any order.

A sequential statement of the same idea, "a far free report does not override a close lethal one", is **not** a valid fold: a far
free at t2 folded before a close lethal at t1 (t1 < t2) gives the opposite result to the reverse order.

`r_det` is a property of the robot's costmap (its obstacle detection range), so it should come from the robot's costmap
parameters, not from a constant. The 1.5 m below is the value reported for the stairs, not a verified parameter.

What it does not change: how a cell reported blocked and never free is treated by the driven-witness cells (`W & free` in
`_build_F`): a body that stood on a cell still proves it free. Needs a stored plane (a format step, a rebuild); not built.

### A.3 Replay on office8

Capture pose distance, today's fold order, `r_det` 1.5 m. The replay's folded `V` equals the engine's `_V` exactly.

| | |
|---|---|
| free cells (5 cm) | 21518 |
| turn blocked by the rule | 2685 (12.5%) |
| of which on a wall rim (within 10 cm of a blocked cell) | 1814 (68%) |
| connected components | 318 |
| components with a cell more than 25 cm from any blocked cell | **15, 1072 cells, 2.68 m2** |
| navigable lattice cells (0.1 m, production `_build_F` on the changed map) | 2143 -> 1889 (-11.9%) |
| 150 sample routes (priced lattice route, same pairs) | 69 same, 47 change, 34 start or goal no longer navigable |

Sensitivity to `r_det`: cells that turn blocked 3612 / 2685 / 1444 at 1.0 / 1.5 / 2.0 m; components away from walls 16 / 15 / 6.

### A.4 The 15 components away from walls

Close = capture pose within 1.5 m of the cell. "Never seen free from close" = share of the component's cells with no free report
from a close capture at all. Positions are map-frame metres of the component's centroid. All 97 leaves are one 6-minute session.

| # | cells | x | y | max dist to a blocked cell (m) | close captures per cell | lethal share, close | far captures per cell | lethal share, far | never seen free from close | nearest robot pose (m) |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | 668 | 1.3 | -3.5 | 0.67 | 4.3 | 0.95 | 36.0 | 0.03 | 0.79 | 0.97 |
| 2 | 135 | 1.6 | -0.4 | 0.25 | 9.2 | 0.71 | 19.9 | 0.19 | 0.37 | 0.83 |
| 3 | 106 | -1.6 | -5.0 | 0.32 | 0.2 | 0.91 | 23.5 | 0.34 | 0.98 | 1.64 |
| 4 | 72 | 2.0 | 0.6 | 0.29 | 4.8 | 0.75 | 19.7 | 0.40 | 0.21 | 1.08 |
| 5 | 43 | 0.7 | -4.2 | 0.38 | 1.2 | 0.92 | 31.4 | 0.17 | 0.93 | 1.55 |
| 6 | 11 | 0.2 | -4.2 | 0.45 | 2.2 | 0.92 | 49.0 | 0.00 | 0.82 | 1.43 |
| 7 | 9 | -1.5 | 1.4 | 0.25 | 1.1 | 0.90 | 56.4 | 0.00 | 0.89 | 1.37 |
| 8 | 8 | -4.7 | -4.8 | 0.29 | 8.0 | 0.31 | 27.0 | 0.00 | 0.00 | 1.23 |
| 9 | 8 | -3.6 | 1.6 | 0.32 | 1.8 | 0.71 | 69.4 | 0.01 | 0.75 | 1.46 |
| 10 | 6 | 0.3 | 0.5 | 0.32 | 10.0 | 0.58 | 25.2 | 0.01 | 0.17 | 0.46 |
| 11-15 | 1-2 each | | | 0.28-0.35 | 0-5 | 0-1.0 | 28-68 | 0-0.21 | 0-1.0 | 1.1-1.6 |

What the time order of the largest component shows (C = close capture, f = far capture, B = mostly lethal, . = mostly free), in
leaf order: 38 far free captures, then **5 consecutive close captures, all lethal**, then 28 far free captures. Component 2:
close lethal runs of 3 and 7 captures, a close free capture between them. Component 3: only far captures, 7 consecutive lethal.

Of the 3401 lethal reports on the largest component, 2719 are from within 1.5 m and **682 (20%) are from farther, up to 6 m (95th
percentile 4.0 m)**. So that region is not detected only inside 1.5 m. It is not the strictly range-limited signature the stairs
description predicts.

### A.5 Stairs versus ghost: the trade-off

| | A real static hazard (stairs, a ledge, glass) | A transient object (a person, a cart, a door) |
|---|---|---|
| today's fold | free again after one far free report: **the robot is routed into it** | correct: free once it left |
| rule, hard persistence (B > Fnear) | blocked until a close free look: correct | blocked until a close free look: a ghost region |
| cost of being wrong | a fall or a collision | a region the planner will not enter and nobody re-looks at |

The ghost cost has a failure mode that needs naming: a blocked region is not routed through, so nobody comes within `r_det` to
free it. The existing escapes are a body driving over a cell (witness cells), or a route passing within `r_det` on its way elsewhere.

What this data can and cannot separate:

* **Cannot:** static versus moved. One 6-minute session, all captures within 0.1 h; there is no second pass over the same spot,
  and nothing after the close captures that came close again. A person who stood there for 5 consecutive captures and left looks
  the same as a hazard.
* **Component 1** (668 cells, 1.67 m2): lethal in 95% of 4.3 close captures per cell and 5 consecutive; not closer than 0.97 m
  to any pose; 20% of its lethal reports come from beyond 1.5 m. Consistent with a real object that the costmap sees mostly when
  close. Not a clean 1.5 m stair signature.
* **Components 3, 5, 7, 9, 11, 13** (about 170 cells): at most 1.8 close captures per cell. Held blocked by the rule while
  nobody ever looked from close; they are unconfirmed either way.

### A.6 Options for the blocked region (decision needed from Laci before any format change)

| Option | Effect on the 15 components | Cost |
|---|---|---|
| 1. The rule as stated (hard until a close free report) | all 1072 cells stay blocked | ghosts persist; needs a way to be re-observed |
| 2. The rule only where the lethal is **confirmed from close**: at least k close lethal reports per cell, k = 3 | components 1, 2, 4, 10, 14 (882 cells, 82%) stay blocked; 190 cells are left as today | one more number (k); counts needed (a count plane, or per-leaf dedup) |
| 3. Price it (rate pricing, §6 of `formulation.md`, cap p 0.75) | routes avoid it but may enter | not safe for a fall hazard |
| 4. Fix at the source (the robot's costmap persistence of stairs) | none in the server | outside this repo's scope; the orchestrator is not changed for this |

Recommendation: option 1 is the principled rule; option 2 limits the ghost cost at the price of a parameter and a count plane.
The data cannot choose between them; **one more session over the same area, with the robot driving within 1.5 m of component 1
after a quiet period, would** (a stair stays lethal, a ghost is retracted by `Fnear`). Where the real stairs are is also not yet
confirmed against these components.

### A.7 What is not shown

Only office8, one session. No other map's leaf data was used. The rule was replayed, not run in the engine. `r_det` is not read
from the robot. The effect on charts, links and the checker (the topology of the changed F) was not run.

---

## B. Turn cost for the route search

### B.1 Formulation

State `(cell, incoming heading)` over the 8 headings; an edge adds a turn term `lambda * |dtheta|` (metres per radian, angle
proportional, **no fixed per-turn cost**, as Laci asked) to the existing priced integer weight; the start heading is free. The
search stays an exact Dijkstra. `sum |dtheta|` is the polyline's total turning, `integral |kappa| ds`, which does not depend on
the lattice step (a bending energy `kappa^2` of a polyline corner would). `lambda = v_nom / omega_max` is the travel time of
rotating (J = v * T for a stop-and-turn robot); MPPI blends turns, so it is an upper bound. The previous bounds B1/B2 carry an
added `lambda * total turning of the length optimum` term.

Until Laci supplies `v_nom` and `omega_max`, the working range is lambda 0.25 to 1.0 m/rad (0.5 corresponds to 0.5 m/s over 1 rad/s,
an assumption, not a robot value).

### B.2 Prototype results (office8, 150 pairs seed 7, kappa 1 hinge, research lattice, then the production string pull)

| form | lattice heading changes per 10 m | lattice rad/m | lattice turns >= 90 deg per 100 m | waypoint rad/m | waypoint turns > 45 deg per 100 m | routes with a waypoint segment < 0.5 m |
|---|---|---|---|---|---|---|
| none | 21.1 | 1.66 | 1.0 | 0.32 | 8.3 | 67 / 150 |
| linear 0.25 | 5.6 | 0.44 | 0.1 | 0.32 | 7.9 | 47 |
| linear 0.5 | 5.5 | 0.43 | 0.1 | 0.31 | 7.8 | 45 |
| linear 1.0 | 5.4 | 0.42 | 0.1 | 0.31 | 7.5 | 44 |
| linear 0.5 + 0.2 m per turn | 5.1 | 0.42 | 2.8 | 0.31 | 8.3 | 31 |
| linear 0.5 + 0.4 m per turn | 4.7 | 0.42 | 5.7 | 0.30 | 9.7 | 26 |
| quadratic 0.63 d^2 | 5.5 | 0.43 | 0.1 | 0.31 | 7.7 | 45 |
| 0.2 + quadratic | 5.4 | 0.42 | 0.1 | 0.31 | 7.5 | 44 |
| linear + hard 45 deg | 5.5 | 0.43 | 0.1 | 0.31 | 7.8 | 45 |
| linear + hard 90 deg | 5.5 | 0.43 | 0.1 | 0.31 | 7.8 | 45 |

(The "none" row uses the prototype's non-canonical ties: 21.1 changes per 10 m; the live canonical-tie route has 16.7.) Pulled
length against lambda 0: median +0.1 to +0.2%, p90 +2.3%, max +4.4%. 0 pairs fail in any variant.

Reading: the linear term alone removes the grid zigzag and the sharp turns; a hard limit and a quadratic term reproduce it (the
remaining 90 deg turns are forced by the geometry); a fixed cost per turn lowers the short-segment routes but makes sharp turns
more frequent (it prefers one 90 deg turn to two 45 deg ones). The turn cost does not remove the short waypoint segments left by
the string pull (part C).

### B.3 Production cost estimate (not measured in the engine)

Production runs scipy `csgraph.dijkstra` on the corridor or the whole-graph (`lazy_plan.PlanGraph`, `ChartGraph.route`). I timed
the same call on an 8-neighbour grid, plain and with the (cell, heading) expansion, one source, with predecessors:

| lattice cells | plain: CSR nnz, MB, search | expanded: CSR nnz, MB, search | search ratio | expanded build |
|---|---|---|---|---|
| 1936 (office8 size) | 15 k, 0.2 MB, 0.4 ms | 120 k, 1.5 MB, 3.3 ms | 9x | 4 ms |
| 10 000 | 79 k, 1.0 MB, 1.4 ms | 630 k, 7.9 MB, 21 ms | 15x | 17 ms |
| 49 729 | 395 k, 4.9 MB, 8.8 ms | 3.2 M, 39.5 MB, 130 ms | 15x | 100 ms |
| 111 556 (largest map) | 888 k, 11 MB, 29 ms | 7.1 M, 89 MB, 319 ms | 11x | 235 ms |

States are 8 N, edges 8 times the directed edge count. Against the measured office8 plan time (warm p99 9.4 ms, cold 13.4 ms
priced), a 9-15x search factor gives an expected 85-140 ms warm and 120-200 ms cold: inside the 0.3 s allowance. On the largest
map a whole-graph expanded search is 319 ms plus 235 ms of build and 89 MB transient per plan, above the allowance. Therefore:

* the expanded graph must be built only for the corridor or ellipse that the priced search already restricts to, not for the
  whole map; the ratio of that subgraph is not measured;
* concurrent plans multiply the transient memory (`plan_inflight`); not measured;
* both `engine.ChartGraph.route` and `lazy_plan.PlanGraph._search`, and the harness `Reference`, must carry the same change.

### B.4 What is not shown

Research lattice, not the production engine; one map; the pull has no turn term (a shortcut is accepted by visibility and priced
length only); the start heading is free (the robot's yaw is available and could be used); lambda is an assumption until the robot
values arrive.

---

## C. Waypoint merge for short segments (separate item, not part of A or B)

Observed on office8, kappa 1 production plans: interior waypoint segments under 0.5 m occur in 68 of 150 routes (47 at kappa 0).
With the turn cost 45 of 150 remain: 60 segments, median 0.28 m, a 40-45 degree turn on each side; 41 of the 60 can be replaced
by their midpoint with both chords visible. The string pull can only keep points of the lattice path, so two waypoints at one
corner remain.

Statement: two consecutive interior waypoints closer than the goal tolerance carry no information the robot can act on (it
accepts a goal within the tolerance). Replace them by their midpoint when both neighbouring chords are visible and the priced
cost of the new polyline is not larger than the cost of the replaced one; otherwise keep both. It is a post-step to
`simplify`; the search, the format and the stored route are not touched. It is not built and the interaction with the turn cost
is not measured.

---

## D. Open items

| # | Item | Owner | Blocks |
|---|---|---|---|
| 1 | Nominal speed `v_nom` and maximum turning speed `omega_max` of the robot | Laci | lambda (B.1) |
| 2 | Goal tolerance: 0.35 m (server) or 0.5 m (repo) | Laci | part C merge distance |
| 3 | Is the Odin override live (inflation 0.8, scaling 6.0, global inscribed 0.30)? | Laci | per-robot profile values |
| 4 | Smac planner tolerance (0.5 in the repo) as run | Laci | whether waypoint shifts matter |
| 5 | The costmap obstacle detection range of the robot (for `r_det`) | Laci | A.2 |
| 6 | Where are the stairs, and are the components of A.4 (largest at x -0.4 to 2.4, y -4.5 to -2.8) stairs, furniture or ghosts | Laci / user | A.6 option choice |
| 7 | One more session driving within 1.5 m of component 1 after a quiet period | user | A.6 |
| 8 | Stairs-versus-ghost decision (A.6) before any new plane or rebuild | Laci | the format step |
| 9 | Production-engine measurement of the turn cost on the corridor or ellipse subgraph (B.3) | lattice_cimbi | an engine change |
| 10 | The exponential inflation tier remains off (no environment set) | Laci | live comparison at kappa 0.5, origin 0.30 once 3 and 4 are answered |
