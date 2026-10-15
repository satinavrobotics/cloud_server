# Close evidence for blocked cells, a turn cost for routes, and a waypoint merge (design, 2026-10-14)

Status (2026-10-14): **part A (close evidence) is in the engine, merged and off by default** (`SceneParams.near_range_m`, environment
`SCENE_GRAPH_NEAR_RANGE_M` empty); **part B (turn cost) is built on branch `feat/turn-cost`, off by default**
(`SCENE_GRAPH_TURN_COST_ENABLED` false; not merged, not deployed; B.5, B.6, `route_safety.md` section 9); part C (waypoint merge) is
designed and prototyped on a research lattice, not built. The exponential inflation tier stays off. Direction was accepted by Laci for A and B; the user accepted the engine
prototype and asked for it to be merged and deployed. Written by the lattice_cimbi session. Every number below comes from the
scripts named below, run on the office8 leaf export (97 leaves, one session of 6 minutes; no other map's leaf export is available
to these scripts).

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

**Current statement (A.6e, 2026-10-15).** The support channel's lethal and its free are both trustworthy only within `r_det`
(`near_range_m`). Per cell keep three max-merge planes: `B` (latest capture time that reported the cell blocked, from any
distance: exists, feeds `ever_blocked` and the statistics), `Fnear` (latest capture time that reported it free **from a capture
pose within `r_det`**, and inside the optional cone) and `Bn` (latest capture time that reported it **lethal from a capture pose
within `r_det`**, same condition). A cell is held when its latest value is free and `Bn >= 0 and Bn > Fnear` (ties: held). A lethal
from beyond `r_det` is ordinary latest-value evidence: any later free report from anywhere retracts it. The paragraphs below are the
first statement of the rule, which used `B` for the held test and said "a lethal report is evidence from anywhere"; that is what
A.6e replaces.

A free report is evidence only from where the sensor could have seen the hazard; a lethal report is evidence from anywhere.
Per cell keep two max-merge planes:

* `B`: latest capture time that reported the cell blocked (exists);
* `Fnear`: latest capture time that reported the cell free **from a capture pose within `r_det`** (new plane).

The cell is blocked while `B > Fnear` (ties: blocked, the existing tie rule); otherwise the existing latest-value rule applies.
Both planes merge by max, so the fold stays commutative, associative and idempotent: leaves may still arrive in any order.

A sequential statement of the same idea, "a far free report does not override a close lethal one", is **not** a valid fold: a far
free at t2 folded before a close lethal at t1 (t1 < t2) gives the opposite result to the reverse order.

`r_det` is a property of the robot's costmap, so it should come from the robot's own parameters, not from a constant.

**Exact source (robot repo `deployment_ws/src/sati_ros_navstack`, file
`sati_nav_stack_bringup/config/services/navstack_sati_odin.yaml`, key `support.commit_radius_m: 1.5`, since 2026-09-20):** the
costmap's ground-support channel (negative obstacles, stairwells; ROADMAP Stage 3, ON since 2026-09-18) marks a cell lethal only
when it is **within `commit_radius_m` of base_link**, the camera swept it, its ground was never seen over `min_looks: 5` looks
and occupancy calls it free. Notes from the same file: the value was 2.0 then 1.5; "the stairwell margin at 1.5 m is NOT
re-measured"; the camera cone is about 95 x 83 degrees and `support_swept` exempts cells the cone never reached. So:

* a **free report from beyond 1.5 m** says nothing about a support-lethal cell: the channel does not judge it;
* a free report from within 1.5 m but **outside the camera cone** also says nothing (not swept); distance alone over-counts it;
* occupancy lethals from the lidar (`obstacle_max_range 9.0`, `raytrace_max_range 10.0`, lidar layer config in `navigation_lidar.yaml`
  and `lidar_obstacle_layer.yaml`) are visible far away: that is why 20% of the stairs' lethal reports come from beyond 1.5 m
  (A.5), and why a lethal from far is still evidence, as the rule says.

The fold has the leaf's base pose and yaw (and an optional camera `view` with `fov`, `rng`); the office8 export has no `view`
(`None` on all 97 leaves). A cone test therefore needs either the export to carry the view or an assumed forward camera.

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

Sensitivity to the camera cone (`--fov-deg 95`, a forward camera ASSUMED, a free report counts only inside the cone or within
0.3 m): 3520 cells turn blocked (16.4%), navigable F 2143 -> 1757 (-18.0%), 16 components away from walls with 1277 cells (3.19 m2).
The cone makes a retraction rarer, so more stays blocked.

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
* **Component 1** (668 cells, 1.67 m2) **is the stairs** (confirmed by the user, 2026-10-14). Lethal in 95% of 4.3 close captures
  per cell, 5 consecutive; not closer than 0.97 m to any pose; 20% of its lethal reports come from beyond 1.5 m (up to 6 m), so
  the costmap sees the stairs mostly from close but not only: `r_det` is a soft range, not a strict 1.5 m limit. Today 86 of its
  196 lattice cells (0.1 m, 0.86 m2) are inside the navigable space; the rule removes them. The other components are not
  confirmed (the user confirmed the larger one only).
* **Components 3, 5, 7, 9, 11, 13** (about 170 cells): at most 1.8 close captures per cell. Held blocked by the rule while
  nobody ever looked from close; they are unconfirmed either way.

### A.6 Options for the blocked region (decision needed from Laci before any format change)

| Option | Effect on the 15 components | Cost |
|---|---|---|
| 1. The rule as stated (hard until a close free report) | all 1072 cells stay blocked | ghosts persist; needs a way to be re-observed |
| 2. The rule only where the lethal is **confirmed from close**: at least k close lethal reports per cell, k = 3 | components 1, 2, 4, 10, 14 (882 cells, 82%) stay blocked; 190 cells are left as today; **75 cells (11%) of the stairs themselves have fewer than 3 close lethal reports and would stay free** | one more number (k); counts needed (a count plane, or per-leaf dedup); a per-cell count is ragged on a real hazard, a neighbourhood count would be needed |
| 3. Price it (rate pricing, §6 of `formulation.md`, cap p 0.75) | routes avoid it but may enter | not safe for a fall hazard |
| 4. Fix at the source (the robot's costmap persistence of stairs) | none in the server | outside this repo's scope; the orchestrator is not changed for this |

Recommendation: option 1. The largest component is the stairs and a per-cell confirmation count (option 2) would leave 11% of
the stairs free, so the extra parameter buys ghost protection at the price of the hazard it is meant for. The ghost cost of
option 1 is real but not demonstrated on this map: the other 14 components are small (404 cells, 1.0 m2 in total). A second session would show whether any of them is a ghost; it is no longer needed to decide for the stairs.

### A.6b What is built (merged into `main`, off by default; the filter of A.6c is on a branch; held by `Bn`, not `B`, since A.6e)

Read `B >= 0 and B > Fn` below as `Bn >= 0 and Bn > Fn` and the pipeline tokens `+near1` / `+near2` as `+near3` / `+near4` (A.6e);
the text is kept as it was written.

* `SceneParams.near_range_m` and `near_fov_deg` (both None = off, bit-identical to the latest-value fold); a new int64 plane `Fn`
  (allocated only when on; grown, shifted and cropped like `B`); `_fold_into(..., Fn, pose)`; `_build_F` excludes cells with
  `B >= 0 and B > Fn` from `free` (the driven-witness logic unchanged).
* Store: `FORMAT` stays 4. A rule-on store adds the tile plane `fn` and carries the rule in its `params`; `PIPELINE` is `morse/2`
  off and `morse/2+near1` on; a rule-off store is byte-identical to today's. A params mismatch in either direction raises a
  `ValueError` naming `near_range_m` at `import_state`; `store.load()` builds the engine from the manifest params.
* Readers that work from tiles see the rule: `store_codec.effective_v` reads a cell as blocked when `v` is free and `b > fn`
  (the same cells `_build_F` removes); `lazy_plan` keeps `read_planes` (so its clearance, line of sight and snap agree with the
  engine), and `SceneGraphStore.tile_v_plane` (the occupancy view) returns the effective plane. Without this a planner built on tiles
  would measure clearance through the cells the engine held blocked.
* Wiring: `SCENE_GRAPH_NEAR_RANGE_M` / `SCENE_GRAPH_NEAR_FOV_DEG` (config, compose, `.env.example`, `runbook.md` section 1) are read
  by the graph-builder's `scene_host`; the planner needs no setting. Switching it on is a params change: every map is rebuilt in a new
  generation (`runbook.md` section 3). `tools/scene_graph_check_generation.py` expects `morse/2+near1` for a store that carries the rule.
* Reproduction: `tools/scene_graph_sim/close_evidence_engine_check.py` prints, on office8 at 1.5 m, free cells 21518, turn blocked 2685,
  navigable 2143 -> 1889 (with a 95 degree cone 3520 and 2143 -> 1757). `close_evidence_replay.py` is the independent replay and the
  component table of A.4. Tests: `tests/unit/scene_graph/test_close_evidence.py` (12): rule off identical, 4 arrival orders x 3 batch
  sizes identical, the semantic cases plus tie and cone, store round trip and both mismatch errors, the tile readers equal the engine
  (deficit of every navigable cell, the occupancy plane), the checker accepts a rule-on store, config parse.
* Not built: the generation check of `tools/scene_graph_expected.json` is unchanged (the pins are counts, valid for a rule-off store).

### A.6c Away from walls (chosen by Laci, 2026-10-14; built on branch `feat/close-evidence-away-from-walls`, not merged, off by default)

Decision: of the options in A.6, keep the rule of option 1 and drop its rims. The held cells (`V < 100`, `B >= 0 and B > Fn`)
form 8-connected components; a component is KEPT only if at least one of its cells is farther than `near_min_clearance_m`
(0.25 m, euclidean, costmap cells x 0.05 m) from the remaining blocked or unknown cells (`V >= 100`, 255 counts); a component
that stays within that distance everywhere is a wall rim and is released: read as free, exactly as without the rule. A kept
component is held whole, its rim cells included (a stair's edge beside a wall stays blocked).

What it adds, and where:

* `SceneParams.near_min_clearance_m` (None = off, valid only with `near_range_m`; stored with the params only when set, so the
  params hash and the `import_state` mismatch check (both directions) cover it). The fold (`B`, `Fn`) is unchanged and commutative.
* `engine._away_from_walls`, called from `_build_F` once per solve: components of the whole canvas, then per component a
  distance transform of the bounding box padded by `ceil(min_clearance / RES) + 2` cells. Exact: a component cell's distance
  matters only when it is <= `min_clearance`, and the blocked cell that decides it then lies within that many cells of the
  component, inside the padded box; a cropped transform is never below the full one and equals it for every cell within
  `min_clearance` of a blocked cell, so "some cell farther than `min_clearance`" is the same in the crop and on the canvas.
  The result is the plane `hold` (`EngineState.Hold`), which `_build_F` uses and which is stored.
* Store: one more tile plane `hold` (uint8) only with the filter; `PIPELINE` token `morse/2+near2` (the token names the
  planes: `+near1` = `fn`, `+near2` = `fn` + `hold`); a rule-on, filter-off store is `+near1`, with the tiles, pages, segments and
  params hash of the merged build byte for byte (checked on office8: off, rule, rule + cone). `FORMAT` stays 4. A reader that
  does not know `hold` would take the tile-local `b > fn` and hold the rims again, so the token changes: the checker and the
  params hash tell the stores apart.
* Readers: components cross tile edges, so a tile cannot recompute the filter. `store_codec.effective_v` takes `hold` when the
  tile has it (else `b > fn`, as before); `lazy_plan`, `tile_v_plane` and the snap go through it unchanged. `import_state` rebuilds
  `hold` from the planes and refuses a stored one that differs (`StateMismatch`); the owner-plane check covers the rest.
* The dirty box (`Dirty.bbox`, what an incremental save rewrites) includes the cells whose held state changed: a window far
  from a component can decide its far cell, so its rim cells change outside every folded window and chart.
* Wiring: `SCENE_GRAPH_NEAR_MIN_CLEARANCE_M` (`config`, compose, `.env.example`, `runbook.md` section 1) to the graph-builder's
  `scene_host`, like `SCENE_GRAPH_NEAR_RANGE_M`. Switching it on is a params change: every map is rebuilt in a new generation.
  `tools/scene_graph_check_generation.py` accepts a store made with it (pipeline `+near2`, params key).

Measured on office8 (97 leaves, `near_range_m` 1.5, filter 0.25 m):

| | rule only | rule + filter |
|---|---|---|
| cells held (components) | 2685 (318) | 1072 (15) |
| navigable lattice cells (0.1 m) | 2143 -> 1889 (-11.9%) | 2143 -> 1955 (-8.8%) |
| 150 pairs (seed 7, >= 3 m apart, drawn from the rule-off graph, unpriced production route) | 103 same, 13 changed (mean length x 1.023), 34 start or goal no longer navigable, 0 unroutable | 121 same, 4 changed (x 1.059), 25 start or goal no longer navigable, 0 unroutable |

`accept_route_safety.py --near-range 1.5 --near-min-clearance 0.25` (150 pairs, kappa 1): every gate passes, A5 (lazy == engine)
0 / 150, and a gate N1 compares the engine's mask with an independent whole-canvas filter (1072 cells, 15 components, equal).
The default run (no flags) is unchanged. Other maps were not measured: only the office8 leaf export exists here. To run the
same numbers on another map, export its leaves (read-only; `tools/scene_graph_sim/README.md`):
`docker exec -i docker_compose-graph-builder-service-1 python - <map> /tmp/sg_export < tools/scene_graph_sim/export_live.py`,
`docker cp docker_compose-graph-builder-service-1:/tmp/sg_export/. tools/scene_graph_sim/data/<map>/`, then
`python tools/scene_graph_sim/close_evidence_route_changes.py --data tools/scene_graph_sim/data/<map> --rdet 1.5 --min-clearance 0.25`.

### A.6e Ghost fix: lethal evidence only from within range (2026-10-15, branch `feat/close-lethal-hold`, off by default)

The problem. The held test used `B`, the latest time ANY lethal report arrived, from any distance. A cell whose only lethal
evidence came from beyond `r_det` (a person seen from 4 m who then left, retracted by a free report from 4 m) was held as well:
a ghost that no free report from beyond range could clear and that only a close look would. The support channel does not judge
cells beyond `commit_radius_m` (A.2), so a lethal from there is no more "true" than a free from there.

The rule. A new int64 plane `Bn` (-1 = none): the latest capture time that reported the cell lethal from a capture pose within
`near_range_m` (the same distance and optional cone condition as `Fn`). Max-merge, so the fold stays commutative, associative and
idempotent. Held = latest value free and `Bn >= 0 and Bn > Fn` (ties: held); the away-from-walls filter then applies to that held
set exactly as before. `B` is unchanged. Cases: lethal beyond range then free beyond range: free (was held); lethal within range,
free beyond range: held; lethal within range, free within range later: free; lethal beyond range only, never freed: the latest
value, blocked.

What changed. Engine: `_Bn` (allocated only when the rule is on; grown, shifted, cropped and reset in the refold region like
`_Fn`), `_fold_into(..., Bn)`, `_build_F` uses `Bn`, `EngineState.Bn`. Store: tile plane `bn` beside `fn` (only when on),
`FORMAT` stays 4, **pipeline tokens `morse/2+near3` (rule only: `fn`, `bn`) and `morse/2+near4` (rule + filter: `fn`, `bn`,
`hold`)**; `+near1` / `+near2` are no longer defined (`store.RETIRED_PIPELINES`, kept only to be refused). `effective_v` takes
`hold` when the tile has it, else `bn > fn` when `bn` is present, else reads `v` (no fallback to `b > fn`). No setting changed.

Rebuild consequence. The semantics of the stored held set changed, so a store of the earlier rule is not reused: its pipeline token
differs, hence its params hash, so the worker rebuilds every rule-on map in a new generation (`runbook.md` section 3) when the new
code starts. Reading such a store directly (`SceneGraphStore.load`, `import_state`) raises a `ValueError` saying it was made before
this rule and must be rebuilt; it is never silently mis-read. A rule-off store is untouched (byte-identical, `morse/2`).

Measured on office8 (97 leaves, `near_range_m` 1.5, no cone; `close_evidence_engine_check.py`, `--old-rule` for the previous rule):

| | old rule (`B`) | new rule (`Bn`) |
|---|---|---|
| cells held, rule only (components) | 2685 (318) | 1471 (143) |
| navigable lattice cells (0.1 m), rule only | 2143 -> 1889 (-11.9%) | 2143 -> 1919 (-10.5%) |
| cells held with the filter 0.25 m (components) | 1072 (15) | 776 (12) |
| navigable lattice cells with the filter | 2143 -> 1955 (-8.8%) | 2143 -> 1973 (-7.9%) |
| 150 pairs rule only: same / changed / start or goal lost / unroutable | 103 / 13 / 34 / 0 | 109 / 14 / 27 / 0 |
| 150 pairs rule + filter | 121 / 4 / 25 / 0 | 123 / 6 / 21 / 0 |

The stairs component (x 1.2, y -3.5) stays held: 641 of its 668 cells under the old rule (the 27 cells
no lethal report from within range ever covered are no longer held). Gates:
`accept_route_safety.py --near-range 1.5 --near-min-clearance 0.25` all pass (A5 lazy == engine 0 / 150, N1 776 cells and 12
components equal to the independent filter), the default run is unchanged. `close_evidence_replay.py` (independent replay) now
holds by `Bn` too and reports the same 1471 and 776 / 12.

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

Lambda is a per-robot value, see B.6. No speed is hard-coded; the 0.5 m/rad of the prototypes is a sweep value, not a robot value.

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

### B.5 Corridor- or ellipse-restricted search: design and cost on the largest map

Design (exact, no heuristic): the production priced search already restricts to an ellipse. For the expanded search:

1. the plain priced route P0 (as today); its priced cost plus the turn cost of its headings is an incumbent `J_inc >= J*`;
2. optional tighter incumbent: the expanded search on the cells within 3 lattice cells of P0 (a thin band), cost `J_band`;
3. the ellipse `E = {c : |s-c| + |c-t| <= J_inc * 1e-4 m}` (euclidean). Every priced edge weight is at least its length over
   1e-4 m and the turn term is non-negative, so a path of total cost <= J_inc has length <= J_inc * 1e-4 m and lies in E:
   **the restriction is exact**;
4. the expanded (cell, heading) search on E only; the expanded CSR is built per request for E only.

Measured (`tools/scene_graph_sim/turn_cost_restricted.py`, research lattice, scipy, lambda 0.5, pairs >= 3 m, with the band):

| map | cells | pairs, route length median / max (m) | ellipse cells median (of map) | exact stage p50 / p95 / p99 (ms) | plain whole-graph search (ms) | whole-graph expanded optimum |
|---|---|---|---|---|---|---|
| office8 (real) | 2 143 | 30, 4.5 / 7.9 | 1 384 (65%) | 6.1 / 9.5 / 14.7 | 0.3 | equal in 30 of 30 |
| Hospital (real, the largest real map here) | 17 114 | 30, 6.6 / 15.7 | 2 348 (14%) | 13.3 / 40.4 / 46.3 | 1.3 | equal in 30 of 30 |
| synthetic 50 x 25 m (the 112k-cell size of B.3) | 117 070 | 15, 21.8 / 39.7 | 34 189 (29%) | 190 / 481 / 509 | 25.8 | equal in 15 of 15 (earlier run, no band) |

The restricted result equals the whole-graph expanded optimum in every comparison (0 mismatches). Without the band the incumbent
is loose (median 1.6 m above the optimum, because the plain route zigzags) and the ellipse is larger (office8 83% of the map).

Laci's decision (2026-10-14): the search time allowance is 0.5 s and the search is exact, no cap and no band. The plain
euclidean ellipse is not enough on the largest map (pairs >= 30 m: stage p95 557 ms, max 585 ms, 10 of 30 plans above 0.5 s). The
exact fix is a tighter ellipse built from **priced graph distances**:

`E = {c : d(s,c) + d(c,t) <= J_inc}`, d the plain priced distance (two plain searches, which the production priced search
already needs one of). Exact: a path of total cost <= J_inc through c costs at least d(s,c) + d(c,t), because the turn term is
non-negative. It is much tighter than the euclidean ellipse in a cluttered map.

Worst case re-measured on the synthetic 50 x 25 m map (117 070 cells), pairs >= 30 m apart (median 33.5 m, max 46.8 m), with the
band incumbent and the graph-distance ellipse, lambda 0.5, whole plan = plain whole-graph search + restricted stage:

| | ellipse cells median / p95 | restricted stage p50 / p95 / p99 / max (ms) | whole plan p50 / p95 / p99 / max (ms) | above 0.5 s | optimum equal to the whole-graph expanded search |
|---|---|---|---|---|---|
| euclidean ellipse (before) | 62 067 / 103 897 | 346 / 557 / 578 / 585 | 374 / 582 / 598 / 603 | 10 of 30 | not run |
| **graph-distance ellipse (exact)** | 26 168 / 45 496 | 195 / 279 / 320 / 337 | 216 / 296 / 337 / 354 | **0 of 30** | 12 of 12 (second run) |
| Hospital (17 114 cells, graph-distance) | 1 502 / 4 920 | 13 / 34 / 41 / 43 | 15 / 35 / 43 / 45 | 0 of 30 | 30 of 30 |

On the largest map the worst case is 354 ms of the 500 ms allowance (the plain search is whole-graph here; the production corridor
seed makes it cheaper). Research lattice, scipy, build of the expanded CSR in numpy per request; the production lazy reader is not
measured, and tile reads come on top (cold). Transient memory is the expanded CSR of the ellipse: about 1.7 M edges (about 20 MB)
at the median on the 117k-cell map.

Concurrent plans multiply the transient memory (`plan_inflight`); not measured.

Not measured here: the turn term inside `priced_visible` for the pull (it is not built: the pull has no turn term).

**Measured in production (branch `feat/turn-cost`, 2026-10-14).** The design above is built in `engine.ChartGraph.route` and
`lazy_plan.PlanGraph.route` (`packages/scene_graph/turn.py`): incumbent = P0 plus its turn cost or the band optimum (3 cells), then
the graph-distance ellipse (the lazy reader loads the tiles of the Euclidean ellipse of J_inc, which contains it, and keeps the
tile cap). Equal to an independent (cell, heading) reference and to the whole-graph expanded search in every test. On office8
(`accept_route_safety.py --turn-lambda 0.5`, 150 pairs): lattice heading changes per 10 m 16.7 -> 5.5 (the canonical-tie route; the
prototype's 5.5 reproduced), 1.35 -> 0.43 rad/m; waypoints 5.6 -> 4.6 per 10 m, 0.323 -> 0.301 rad/m; plan p99 warm 8.3 -> 20.8 ms,
cold 13.9 -> 22.1 ms; all gates pass at 0.25, 0.5, 1.0. Worst case on the 117 070-cell synthetic map (`inflation_efficiency.py
--sizes 50x25 --pairs 30 --min-pair-m 30 --turn-lambda L`, the production lazy reader and engine, hinge kappa 1, pairs median 36.7 m):

| lambda | engine route p99 / max (ms) | lazy route warm p99 / max | lazy plan cold p99 / max | above 0.5 s |
|---|---|---|---|---|
| 0.5 | 301 / 304 | 414 / 421 | 384 / 392 | 0 of 30 |
| 1.0 | 415 / 425 | 462 / 486 | 512 / 541 | 1 of 30 (cold) |
| 5.0 (clamp) | 702 / 705 | 787 / 789 | 810 / 818 | 12-17 of 30 |

So the 0.5 s allowance holds to lambda 0.5 on the largest map and not from lambda 1.0: the gap J* - d(s, t) grows with lambda, and with
it the plain-distance ellipse. Full table: `route_safety.md` section 9. Concurrent plans still multiply the transient memory; not
measured.

### B.6 Lambda per robot, from registration

Fields (robot object `status.factsheet`, `cloud_common/objects/robot.py::RobotTypeIdentifierV1`, stored as reported by
`FACTSHEET_PHYSICAL_FIELDS`; -1 until the robot has sent them):

| field | VDA5050 | use |
|---|---|---|
| `speed_max` (m/s) | `physicalParameters.speedMax` | v |
| `angular_speed_max` (rad/s) | `physicalParameters.angularSpeedMax` (VDA5050 2.1) | omega |

`lambda = speed_max / angular_speed_max` (m per rad). The factsheet has no nominal (operating) speed, only the maximum, so lambda is
the ratio of the two maxima: the rotation time of the robot expressed as travel distance at maximum speed. If the robot is
commanded below its maximum speed the true ratio is smaller; the maxima are the registered values, nothing is guessed.

Validity: both fields finite and above 0 (the registration code already rejects non-positive values, `smallest valid` 1e-9);
lambda is clamped to the range [0, 5] m/rad so a corrupt factsheet cannot make weights overflow (5 m/rad x pi = 157 000 units).

**Fallback when a field is missing** (-1, None, or not finite): no turn cost for that plan, i.e. today's route exactly (lambda 0).
An operator may set `SCENE_GRAPH_TURN_LAMBDA_M_PER_RAD` (unset by default) to a number that is then used when the factsheet is
incomplete; it is never a speed and is not read when the factsheet is complete. Nothing is hard-coded.

**Pricing record** (the existing per-plan record, `scene_route` pricing dict, log line and `/stats`): a `turn` entry
`{lambda_m_per_rad, source, speed_max, angular_speed_max, reason}` with `source` one of `factsheet`, `config`, `off`; `reason` is
empty or names the missing field (`angular_speed_max missing`). `/stats` counts plans by source so a robot that always falls back is
visible. The route cache key includes lambda (like the frozen `Safety`), so two robots with different lambda never share a result.

Status (2026-10-14): **built on branch `feat/turn-cost`, off by default** (`SCENE_GRAPH_TURN_COST_ENABLED`, default false: lambda 0,
source `off`, reason `disabled` for every plan, so the change alters nothing until it is switched on). `scene_route.turn_lambda`
(pure: factsheet, switch, operator value) and `turn_record`; the server passes the record to plan, navigate and enrich / gap-fill;
`/stats` `scene_graph.turn_sources`. The record is on `ChartPlan.pricing["turn"]`; with a turn and no price (kappa 0, or pricing
impossible) the plan is still turn-costed and its record says `rate` 0, `profile` "none". The edge-weight cache of the engine is keyed
by what the weights depend on (margin, rate, k, origin), not lambda; no route result is cached anywhere.

### B.4 What is not shown

Research lattice, not the production engine; one real map for the quality results; the pull has no turn term (a shortcut is accepted by visibility and priced
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
`simplify`; the search, the format and the stored route are not touched. It is superseded by C.2.

---

## C.2 Waypoints as guidance with a cost budget (replaces the merge; built, off by default)

The exact merge of part C (never costlier) changes 2 of 150 plans on office8: the price bends the corner away from obstacles, so the
midpoint chord is dearer in 62 of 68 visible cases. The statement is a budget, not a merge:

* **Choose the fewest waypoints such that the total slack of the chords is at most `delta`**, a chord's slack being
  `max(0, c(i, j) - P(i, j))` (priced chord cost minus the priced cost of the lattice sub-path it replaces), every chord visible and
  within `SCENE_GRAPH_MAX_WAYPOINT_GAP_M`. Hence `J(final) <= J(lattice) + delta`. Today's pull is `delta = 0` without the minimal-link
  property: the greedy pull is not minimal-link, so the exact result at `delta = 0` can have fewer waypoints at no more cost.
* **Built:** `scene_route.fewest_waypoints` (dynamic program over (point, slack in whole cm, rounded up), O(n * window * D); ties: fewest
  waypoints, least slack, lexicographically smallest indices), `SCENE_GRAPH_WAYPOINT_BUDGET_M` (empty = off, the greedy pull and its
  code path unchanged; finite and <= 1.0, else off with a warning), also used by the gap fill between operator waypoints. Start, goal
  and operator waypoints never move. The last chord may exceed the gap cap by `MIN_END_STUB_M` (what the end-stub merge allows the
  greedy pull); the end-stub merge is then judged against the budget. The plan carries `waypoint_budget` ({`waypoint_budget_m`,
  `waypoints_removed` against the greedy pull of the same plan}); `/stats` has `scene_graph.waypoints_removed`. The turn cost is
  independent.
* **Measured in the production planner** (office8, 150 pairs seed 7, kappa 1, `accept_route_safety.py --waypoint-budget 0.35`, gates
  W1-W3 pass): segments per route 4.17 -> 3.32; routes with a segment under 0.5 m 92 -> 61; polyline length ratio (budget / greedy) mean
  0.996; mean budget used 4.4 cm (max 34.7 cm); plan time p50/p99 3.4/7.2 ms greedy, 18/80 ms budget (warm, in-memory). The research
  prototype's 56 -> 18 counted segments of its own lattice-path polylines (no leaf-id repair, no end-stub merge, no gap cap); the
  production figures differ for that reason and are the ones to use.
* **Open (robot side):** whether the robots honour the VDA5050 node `allowedDeviationXY`, and whether the navigator can mark a node
  reached by cost-to-go progress; the value `delta` (0.35 m, the goal tolerance, is the reference).

## D. Open items

Resolved by Laci 2026-10-14: goal tolerance 0.35 m; the Odin override is live; the Smac tolerance is dropped; robot speeds come from
registration (B.6, per robot, with a fallback); the search allowance is 0.5 s and the search is exact (B.5, no cap, no band).

| # | Item | Owner | Blocks |
|---|---|---|---|
| 1 | Review B.6: lambda = `speed_max / angular_speed_max`, clamp [0, 5], fallback off or the operator value, record fields | Laci | the planner code for lambda |
| 2 | `r_det` source: `support.commit_radius_m` 1.5 in `navstack_sati_odin.yaml` (A.2); the camera cone needs the leaf `view` or an assumed forward camera | Laci | the value of `near_range_m` and `near_fov_deg` |
| 3 | Stairs versus ghost for the 14 unconfirmed components of A.4 (the largest, x 1.3 y -3.5, is the stairs, confirmed by the user) | Laci / user | A.6, recommended option 1 |
| 4 | Whether the rule applies to every blocked cell or only away from walls (rim cells thicken walls by up to 10 cm: navigable F -11.9% everywhere, -8.8% away from walls only) | Laci | A.2 |
| 5 | Switching the rule on in production (`SCENE_GRAPH_NEAR_RANGE_M`): a rebuild of every map; value from the robot's `support.commit_radius_m` (1.5 m), the cone or not (item 2), and the decision of A.6 (item 3) | Laci / user | the live use of A |
| 6 | The expanded search with the graph-distance ellipse in the engine and the lazy reader (B.5), with lambda per robot (B.6) | lattice_cimbi | built on branch `feat/turn-cost`, off by default; needs review, merge, and a decision to switch it on. The 0.5 s allowance holds to lambda 0.5 on the 117k-cell map, not from 1.0 (B.5): either accept that, cap lambda lower than 5, or tighten the ellipse (a lower bound of the turning still needed) |
| 7 | The exponential inflation tier remains off (no environment set) | Laci | live comparison at kappa 0.5, origin 0.30 |
