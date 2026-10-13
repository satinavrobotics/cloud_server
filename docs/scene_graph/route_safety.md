# Route safety: clearance-priced lattice route (derivation and implementation contract)

Status: implemented, 2026-10-13, on the tile-lazy planner (the only planner mode: `full_load` no longer exists; the engine's
`route` is the reference implementation the lazy reader is tested against). Owners: CIMBI (engine, `packages/scene_graph`), LACI
(planner, `packages/services/mission_planner`). Statements 29-31 of `formulation.md` point here.

## 0. Problem and evidence

The planner's route minimises geometric length on the lattice F (cells where the robot square fits, plus driven witness cells),
then string-pulls the path. Nothing in the objective refers to clearance, so the route and the waypoints sit on the boundary of
F, which is exactly the robot's inscribed radius r from obstacles. At a waypoint the robot turns, which sweeps the circumscribed
radius r_c = r * sqrt(2) > r.

Measured on the real office8 export (97 leaves, 2143 F cells, 12 charts; 150 random pairs >= 3 m; independent full-resolution
clearance field, unknown cells counted blocked; reference optimum = full-resolution 0.05 m 16-neighbour Dijkstra on the same
feasible set and the same g; `tools/scene_graph_sim/bench_route_safety.py`, ledger L4; the acceptance harness measures the same quantities with the same sampling,
section 7), margin m = r_c = 0.354 m, kappa 1:

| quantity | current planner | priced route (kappa = 1) |
|---|---|---|
| J / J_ref, median / p90 / max | 1.084 / 1.242 / 1.601 | 0.991 / 1.023 / 1.059 |
| median of per-route minimum clearance | 0.209 m | 0.292 m |
| routes with minimum clearance < r (0.25) | 89 of 150 | 33 of 150 |
| mean path length with clearance < m (0.354) | 1.41 m | 0.62 m |
| length ratio median / p90 / max | 1 | 1.015 / 1.078 / 1.230 |

## 1. Definitions

* `g(c)`: physical clearance of a lattice cell c: distance (m) from the cell to the nearest blocked or unknown cell,
  `free = (V != 255) & (V < 100)` on the 0.05 m canvas, Euclidean distance transform of `free` times RES, sampled at the lattice
  cell's even full-resolution cell. This is `sp.clr` in the engine, unchanged.
* `r`: `SceneParams.robot_half_side`, the inscribed radius F guarantees (witness cells excepted).
* `m` (margin, m) `= r_c`, the planning robot's circumscribed radius `hypot(length, width) / 2` from the factsheet (the length is
  the width while unknown); `r * sqrt(2)` when the width is unknown. There is no tolerance constant. If `m - r` is not above the
  lattice clearance quantisation there is no pricing (S14).
* `d(c) = max(0, m - g(c))`: the deficit of a cell.
* `mu` (1/m): the rate, `mu = kappa / (m - r)`; `kappa` is the cost multiplier added at the inscribed floor g = r. `kappa` is the one
  preference constant (`SCENE_GRAPH_SAFETY_KAPPA`, default 1.0, range [0, 10], 0 disables; the range is the measured plateau of S13,
  not a bound of the theory).
* Pricing is off (`safety = None`) when `m - r <= h / sqrt2 = 0.0707 m` (S14) or `m > CAP = 1 m` (S9).
* Edge (a, b), Euclidean step `l`: `w_ab = max(ceil(l / 1e-4 * (1 + mu * (d(a) + d(b)) / 2) - 1e-6), 1)` (integer, units of 1e-4 m;
  the `1e-6` is the guard the unpriced weight already has against float noise just above an integer).
* `J(path) = sum w_ab` is `Route.cost`; `Route.length` stays geometric.

## 2. Statements (derivation record)

| # | Statement | Mark |
|---|---|---|
| S1 | With the pose reference at the footprint centre, a pose is safe at every heading iff g >= r_c. F guarantees only g >= r. | EXACT (geometry) |
| S2 | A hard constraint `g >= m` is not a formulation: it disconnects start from goal. Measured on office8 at m = 0.404 (r_c + 0.05, an earlier setting of this work): 158 of 200 pairs have a bottleneck below m. Lexicographic "minimise exposure, then length" is discontinuous in g: measured detours of 2.2-2.35x for 0.03 m of clearance. | TESTED (research script, ledger L5) |
| S3 | Hazard per unit length vanishes for g >= m and is smooth below, so its first non-zero term is linear in the deficit d. Expected cost per unit length is `1 + mu d`; `J = L + mu A`, `A = integral d ds` (intrusion area). Any smooth hazard, or a clearance-limited speed `v = v0 (1 - k d)`, agrees to first order. | MODEL (first order) |
| S4 | For `mu >= 0` the minimisers of `L + mu A` are the supported points of the Pareto frontier of (length, intrusion): `mu` is the shadow price of `min L subject to A <= A0`. `J*(mu) = min over paths (L + mu A)` is concave, non-decreasing, piecewise linear in `mu`. | EXACT |
| S5 | Layer cake: `(m - g)+ = integral_0^m 1[g < t] dt`, so `A = integral_0^m |path cap {g < t}| dt`: the exposure integrated over the sublevel-set filtration of the Morse function g. Single-level exposure and the max-min bottleneck are a slice and a limit of the same family. | EXACT |
| S6 | `|J_g - J_g'| <= mu * ||g - g'||_inf * L`: J is Lipschitz in clearance, so lattice noise changes it by a bounded amount. | EXACT |
| S7 | Weights are non-negative and additive, so Dijkstra on the lattice is exact for J. `w >= ceil(l / 1e-4 - 1e-6)`, hence `length(path) <= J(path) * 1e-4 + n * 1e-10` m for an n-edge path (the guard only), and `J(path) <= length / 1e-4 + n` units for the unpriced weights. | EXACT |
| S8 | Reduction: `rate = 0` is `safety = None`: today's route, cells and cost, corridor included. Feasibility is unchanged: a route exists with pricing iff it exists without (same edges, finite weights). | EXACT |
| S9 | Because `(m - g)+` is invariant under capping g at m, the weights only need `min(g, m)`: a reader with a halo of at least m / RES costmap cells around a cell computes the same `d` as the engine. The lazy reader uses a halo of ceil(CAP / RES) + 2 = 22 cells around the tile and CAP = 1 m (`_CLR_CAP_M`), so it is exact for margins up to CAP, and never beyond the engine canvas pad MARGIN * RES = 2.0 m (past which the engine's own clearance is not blocked by the canvas edge); above CAP pricing is off. | EXACT (halo), TESTED (P8 at margin = CAP) |
| S10 | Under pricing the length-based corridor is a seed, not a bound: the engine searches the whole graph; the lazy reader searches the cells of the ellipse `|c-a| + |c-b| <= J_corridor * 1e-4` (by S7 no path of cost <= J_corridor leaves it), and keeps the corridor's route if that needs more tiles than the cap (`exact = False`). | EXACT (bound), TESTED (equality) |
| S11 | Waypoint extraction: a shortcut (p_i, p_k) is accepted only if it is visible AND `J(segment) <= J(lattice sub-path p_i..p_k)`, both sampled the same way (every 0.025 m). Then `J(final polyline) <= J(lattice path)`. | EXACT |
| S12 | Discretisation: the 8-neighbour lattice overestimates Euclidean length by up to 8.24 % (`sqrt(1 + (sqrt2 - 1)^2)`); lattice g differs from continuum clearance by <= h/sqrt2 = 0.0707 m (observed max 0.0707 over 2143 cells), at most 2 h/sqrt2 at an arbitrary point of an allowed cell by Lipschitz-1. | EXACT / TESTED (observed maximum: research script, ledger L5) |
| S14 | A margin within the lattice clearance quantisation of r, `m - r <= h / sqrt2 = 0.0707 m`, is unpriced: the clearance of a lattice cell is known only to h / sqrt2 (S12), so a deficit below it is noise, and the rate `kappa / (m - r)` grows without bound as m -> r (a degenerate footprint, 0.5 x 0.0001, has m = r). | EXACT (from S12) |
| S13 | `kappa` is the one preference constant. On office8, m = r_c, `whole` row: kappa 1 / 3 / 10 give J/J_ref 0.991 / 0.985 / 0.977 (median) with p90 1.023 / 1.040 / 1.134, routes under r 33 / 30 / 30, unsafe length 0.62 / 0.49 / 0.36 m, max length ratio 1.230 / 1.283 / 1.661 (ledger L4). The gain in unsafe length per unit of detour falls from kappa 3 on; kappa 1 is a plateau choice, not derived. | HEURISTIC (plateau) |

## 3. Implementation contract

### 3.1 `packages/scene_graph/api.py`
* `@dataclass(frozen=True) class Safety: margin: float; rate: float` (validates `margin > 0`, `rate >= 0`, ValueError).
* `SceneGraph.route(..., safety: Optional[Safety] = None)`; `None` is today's behaviour. `Route.cost` is the integer J,
  `Route.length` geometric. `rate == 0` is `None` (S8).
* `SceneGraph.deficit_cell(ix, iy, margin) -> Optional[float]`: `max(0, margin - g)` of the navigable global lattice cell, None otherwise.
* The module header says that a consumer may ask `route` to price clearance with `Safety`; pricing of observation rates is not built.

### 3.2 `packages/scene_graph/engine.py` (`ChartGraph`, the reference)
* `route(..., safety=None)`: weights per section 1 from `sp.clr`, cached with the key `(margin, rate)` (the last key only); with
  `safety` the corridor is not used (S10): search over `alive`. The canonical tie rule is unchanged (on the integer cost).
* `deficit_cell(ix, iy, margin)` from `sp.clr` and `sp.idx`. No change to the partition, keys, charts, links, widths, store format
  or `_lattice_edges`.

### 3.3 `packages/services/mission_planner`
* `lazy_plan.PlanGraph.route(..., safety=None)`: the engine's objective on the owner tiles. The clearance of a tile comes from the
  stored `v` planes of the tile and a halo of 22 cells (the 8 neighbours are read, their reads checked as one batch with
  `view._need`; an absent tile is unknown, hence blocked); a windowed EDT, capped at CAP = 1 m, exact for margins up to CAP by S9, cached
  per tile in the graph (bounded by `tile_cache`, counted in `PlanGraph.nbytes`); no stored data, no format change. The edge weight is
  `api.edge_weight`, the one function the engine and the reader share. Under
  `safety` it takes the overlay path: corridor route first, then the ellipse of S10 with `limit = J_corridor * (1 + 1e-9) + 1e-9`.
  `PlanGraph.deficit_cell` reads the same tiles.
* `scene_route.safety_for(graph, width, length, kappa)`: the `Safety` of section 1, or None when `kappa <= 0`, the graph has no
  `deficit_cell`, `m - r <= h / sqrt2` (S14) or `m > CAP` (S9).
* `scene_route.priced_visible`: the cost-aware line of sight of S11 (a non-navigable sample cell counts as clearance 0; a point that is
  not on the lattice path is judged by the plain line of sight). `simplify`'s signature is unchanged.
* `plan_on_graph(..., safety_kappa=0.0, width_check=True)`. The footprint (`robot_width_m`, `robot_length_m`) gives the margin
  whether or not `width_check` is set; `width_check` only gates the `too_narrow` comparison. `server.py` passes the factsheet width
  and length always, with `width_check = SCENE_GRAPH_WIDTH_CHECK_ENABLED`, so the gate itself is as before.
* **nodePolicy planning** (`node_policy_plan.py`): both `graph.route` calls (the leg width of `annotate_corridors` / `leg_widths`, and
  the route between two operator waypoints of `insert_planned`) take the same `Safety` (`safety_for` with the robot's footprint, or
  the `r * sqrt(2)` fallback) and the cut of `insert_planned` uses `priced_visible`, so planned and gap-fill waypoints, and the corridor
  widths, belong to the route the planner takes.
* **Pricing report.** A priced plan carries `pricing = {kappa, margin, rate, margin_source ("factsheet" | "map_r"), cost, exact}`
  (`ChartPlan.pricing`, the `pricing` key of the plan response next to `graph_version`); `exact` is the flag of the route call
  itself (`Route.exact`, False when the ellipse search needed more tiles than the cap and the corridor's route stood). One INFO line
  `Priced plan map=... graph_version=... kappa=... margin=... rate=... margin_source=... cost=... exact=... tiles=...` per priced plan,
  and the planner stats (`scene_graph`) count `priced_plans` and `priced_plans_inexact`.
* `config.SCENE_GRAPH_SAFETY_KAPPA` (default 1.0, [0, 10], out of range reset to 1.0 with the usual warning); compose passes
  `SCENE_GRAPH_SAFETY_KAPPA=${SCENE_GRAPH_SAFETY_KAPPA:-1.0}` to the mission planner.

### 3.4 Not in this change
Store format or generation change; a clearance plane; a 16-neighbour lattice; pricing the chart links or the corridor; evidence or
rate pricing (counts, posterior, `now`); anchor or portal waypoints; any change of the tie rule beyond using the integer cost; new
`Route` fields or API responses; client changes; changes to the legacy planner.

## 4. Tests

`tests/unit/scene_graph/test_route_safety.py` (engine; independent reference in `route_safety_worlds.py`: scipy EDT clearance, own
edge list, python-int weights, heapq, canonical walk), `tests/unit/test_lazy_route_safety.py` (lazy), `tests/unit/test_scene_route_safety.py`
(planner, config), `tests/unit/test_node_policy_route_safety.py` (nodePolicy), `tests/unit/test_accept_route_safety.py` (harness smoke).

| P | what is compared |
|---|---|
| P1 reduction | `Safety(m, 0.0)` and `None`: nodes, cost, charts, length, min_width, with and without overlay (engine and lazy); the cache of the priced cost does not leak into the default route |
| P2 completeness | priced reachable iff unpriced reachable on 12 engine worlds (4 structural, 8 random) and the 9 lazy equality worlds, free, a 0.35 m and a 1.5 m disc on the route, and a 3 m disc that cuts pairs |
| P3 optimality | `Route.cost` and the cells equal the in-test reference, also under an overlay; `J(priced) <= J(unpriced cells, priced weights)` |
| P4 dominance | `length(priced) >= length(unpriced)` for 3 safeties on the 12 worlds; pricing does change some route |
| P5 monotone family | over mu in {0 .. 64}: `J*` non-decreasing, length non-decreasing and intrusion non-increasing up to the proved rounding bounds, `J*` concave on three triples (rate 0 from the reference optimum) |
| P6 invariance | batching 1/7/48, shuffled arrival, translation by 10 m, overlay under two batchings |
| P7 lazy == engine | cells, cost, charts, length on the 9 equality worlds (anchor / point / straddle pairs; free, disc and segment overlays); `last["exact"]`; lazy cost == the reference optimum |
| P8 halo | lazy `deficit_cell` == engine's bit for bit for every navigable cell at margins r_c = 0.354 and CAP = 1.0, including cells on tile edges; None off the lattice and in absent tiles; the clearance cache is bounded and counted in `nbytes`; the halo reads are one `_need` batch; a margin above CAP (r_c 1.12) is unpriced by `safety_for` |
| P9 pull | final polyline: all samples in F, `J(final) <= J(lattice path)` by an independent evaluator (engine and lazy), and the unpriced pull violates it on `grid`, `room_loop`, `ring` |
| P10 planner | kappa 0 == legacy and `priced_visible` never called; a graph without `deficit_cell` unchanged; kappa 1: same fallback reason or both served on 5 worlds x engine/lazy x 3 overlays x width known/unknown; engine and lazy give the same waypoints; the footprint gives the margin with the gate off; config range and environment; the server passes kappa, width, length and the gate |
| P12 degenerate margin | `safety_for` is None for 0.5 x 0.0001 (m = r), 0.5 x 0.2 (m - r = 0.019) and just below h / sqrt2, priced just above |
| P13 pricing report | `ChartPlan.pricing` fields and `margin_source`; the response, the log line and the stats counters of the service; `Route.exact` False on the capped ellipse; `edge_weight` |
| P11 nodePolicy | both routes of `node_policy_plan` get one `Safety` (none at kappa 0); the planned-waypoint cut costs no more than the lattice path where the plain cut does; `enrich_route` and `plan_route` pass the same margin |

## 5. Implementation notes

* **Rate 0 is `safety=None`.** S8 says `mu = 0` is today's route. Today's route is the corridor route, which is not proved shortest
  (formulation.md statement 27), so a rate-0 search of the whole graph would sometimes return a cheaper route. P1 pins it. For the same
  reason the length-optimality statements (A4, B1, B2 of the harness) are made against the length optimum of the reference, not against
  the production baseline route.
* **The planner has one mode.** `full_load` was removed (`SCENE_GRAPH_PLANNER_MODE` is ignored); the engine keeps `route(safety=)` as
  the reference implementation the lazy reader is tested against (P7) and that the writer-side tools use.
* **Per-robot r_c** reaches the planner even with the width check off (section 3.3); when the factsheet has no width the margin is
  `r * sqrt(2)`.

## 6. Retracted

Ideas of the research round that this document does not carry, with the reason:

* **The bottleneck threshold F_{m*}** (the largest margin for which start and goal stay connected in `{g >= m*}`) as the planning
  space: it is the max-min limit of S5, not a route objective, and a hard constraint disconnects pairs (S2).
* **Indicator exposure** (the length of the path with g < m, a step function of g): discontinuous in g, so S6 fails and the route
  jumps with lattice noise; the hinge deficit of S3 is its continuous replacement.
* **Lexicographic exposure** (minimise the exposure, then the length): discontinuous (S2: detours of 2.2-2.35 x for 0.03 m of
  clearance). The priced objective gives the same preference with a bounded exchange rate (S4).
* **Anchor chains as waypoints.** Through the anchors the route is a chain of abstract hops, not a clearance-optimal path: in the
  benchmark (ledger L4) `anchor` has the worst J/J_ref and 67 routes under r against 33, `portal` is better but still behind `whole`.
  The anchors stay what formulation.md says they are (a locator and a chart key), never waypoints.
* **The evidence posterior in routing.** Pricing observation rates (counts, `now`) is a separate, later format step
  (formulation.md section 6); nothing here reads it.
* **A slack field from the B plane (time of last report blocked) instead of physical clearance.** Tested worse than the physical
  clearance: median minimum clearance 0.269 against 0.316 m and 50 against 24 routes under r (research script, ledger L5).
* **The penalty-profile table** of the research round is withdrawn pending data.
* **A tracking tolerance tau added to the margin** (m = r_c + tau, tau = 0.05 m in the first version and its config variable): dropped. It was a
  second preference constant on top of kappa with no measurement behind its value; the margin is the geometric r_c (S1).
* **Gating the lazy planner off** (an intermediate version refused kappa > 0 with the lazy planner): obsolete, lazy is the only mode and
  prices.

## 7. Acceptance (`tools/scene_graph_sim/accept_route_safety.py`)

A fast gate over the production path: the export is folded in the engine, saved to `SceneGraphStore(MemoryBackend)`, the restored
engine is the reference and `open_plan` the planner's `PlanGraph`; the same pairs are planned at kappa 0 (baseline) and kappa 1 on the
same graphs (route level and `plan_on_graph` level). Exit code 0 when every gate passes. Gates A are exact (any failure is a bug), B are
theorems of optimality per pair with the ceil slack as the tolerance, C are statistical or an allowance. Notation: n = edges of a
route, U = 1e-4 m, mu as in section 1, o = the length optimum (independent reference, unpriced weights), A the intrusion
`sum l (d_a + d_b) / 2`, L the length.

| Gate | Claim | Derivation | Tolerance |
|---|---|---|---|
| A1 reduction | `Safety(m, 0.0)` and kappa 0 return the baseline route and plan | S8 | 0 (bit for bit) |
| A2 completeness | the set of routable pairs, and of planned pairs, is the same at kappa 0 and kappa 1 | S8: same edges, finite weights | 0 |
| A3 lattice optimality | `J_int(priced route)` equals an independent reference (numpy edge list from the owner plane and the edge rule, scipy Dijkstra, canonical walk), cells too; `J_int(priced) <= J_int(baseline cells under the priced weights)` | S7: Dijkstra is exact for non-negative additive integer weights; canonical rule | 0 |
| A4 length | `L(priced) >= L(o) - n * U` | `cost(o) <= cost(P)` for the unpriced cost, `L/U <= cost <= L/U + n` (S7) | n * 1e-4 m |
| A5 lazy == engine | route nodes and cost equal, at kappa 0 and 1 | both minimise `J_int` with the canonical rule | 0 |
| A6 determinism | a cold graph (empty tile and clearance caches) returns the warm graph's routes | the route is a function of the stored cells | 0 |
| A7 waypoints | every 1 cm sample of the final polyline is in F; `J(final) <= J(lattice path) + n * U`, both sampled every 0.025 m | S11 | n * 1e-4 (sampling alignment of non-collinear pieces) |
| B1 intrusion | `A(priced) <= A(o) + n * U / mu` | `mu A_p + L_p <= mu A_o + L_o` and `L_p >= L_o - n U` | n * 1e-4 / mu |
| B2 detour | `L(priced) <= L(o) (1 + mu A_o / L_o) + n * U` | `L_p + mu A_p <= L_o + mu A_o` and `A_p >= 0` | n * 1e-4 m |
| C1 fallbacks | no pair served at kappa 0 falls back at kappa 1; the reason set is unchanged; `corridor_too_large` is reported | statistical: the ellipse can need more tiles than the corridor | 0 new |
| C2 continuum value | `J_cont(priced) <= 1.0824 J_cont(base) + mu * 0.1414 * L(priced)` per pair (independent 0.05 m clearance, 1 cm samples); the median ratio is reported | S7 on the lattice; 1.0824 = metrication (S12), 0.1414 = 2 h / sqrt 2 clearance quantisation (S12) | as written |
| C3 plan time, warm | p99 of `plan_on_graph` at kappa 1 <= 0.1 * `SCENE_GRAPH_PLAN_BUDGET_S` (0.3 s) | **an allowance, not derived**; warm cache, in-memory store, no network | 0.3 s |
| C3c plan time, cold | the same, with the clearance cache, the tile cache and the store's decoded-tile cache emptied before every plan | **an allowance, not derived**; no network I/O (the live tile read latency comes on top) | 0.3 s |

Output on office8 (`python tools/scene_graph_sim/accept_route_safety.py --data <office8 export>`, 150 pairs, seed 7, kappa 1; 6 s). The table above the gates is the harness's own sampling, the same as `bench_route_safety.py` (vertices and the last point included):

    ### office8: 97 leaves, N=2143 F cells, 12 charts; r=0.250 m=0.354 mu=9.66/m (kappa 1.00); 150 pairs seed 7; routable 150
             median J_cont    min clr    #<r   unsafe m    mean L
    kappa 0          6.04      0.209     89       1.41      5.35
    priced           5.43      0.292     33       0.62      5.52
    length ratio priced / production baseline route (median / p90 / max): 1.010 / 1.064 / 1.233

    A1  reduction              PASS  0 / 150 routes differ, 0 plans differ
          rule: S8: Safety(m, 0.0) and kappa 0 are the baseline, bit for bit (nodes, cost); tolerance 0
    A2  completeness           PASS  routable 150 / 150 (priced 150), planned 150 (priced 150)
          rule: S8: same edges, finite weights, so the routable set is equal; tolerance 0
    A3  lattice optimality     PASS  0 / 150 differ from the reference, 0 with J(priced) > J(baseline cells, priced weights)
          rule: S7: Dijkstra is exact for non-negative additive integer weights; J_int(priced) == reference, <= J_int(baseline cells); tolerance 0
    A4  length                 PASS  0 / 150 priced routes shorter than the length optimum by more than n * 1e-4 (largest shortfall 0.000000 m); the production baseline is longer than that optimum on 0
          rule: o = length optimum (reference, base weights): cost(o) <= cost(P) and L/U <= cost <= L/U + n give L(priced) >= L(o) - n_priced * 1e-4 m (n = edges)
    A5  lazy == engine         PASS  0 / 150 pairs differ (baseline and priced)
          rule: both minimise J_int and share the canonical tie rule: nodes and cost equal; tolerance 0
    A6  determinism            PASS  0 / 150 routes differ between a cold and a warm cache
          rule: the route is a function of the stored cells (S7, canonical rule), not of cache state; tolerance 0
    A7  waypoints              PASS  0 plans leave F (1 cm samples), 0 with J(final) > J(lattice) + n * 1e-4
          rule: S11: a shortcut is accepted only if visible and J(segment) <= J(sub-path), so J(final) <= J(lattice); tolerance n * 1e-4 m
    B1  intrusion              PASS  0 / 150 violate A(priced) <= A(o) + n * 1e-4 / mu (o = length optimum)
          rule: mu A_new + L_new <= mu A_o + L_o (priced optimality) and L_new >= L_o - n*1e-4 (A4): A_new <= A_o + n * 1e-4 / mu (n = max edges of the two)
    B2  detour                 PASS  0 / 150 violate L(priced) <= L(o) (1 + mu dbar_o) + n * 1e-4
          rule: L_new + mu A_new <= L_o + mu A_o and A_new >= 0: L_new <= L_o (1 + mu A_o / L_o) (+ rounding n * 1e-4)
    C1  fallbacks              PASS  0 newly falling back, reasons kappa0 [], kappa [], corridor_too_large 0
          rule: S8/S10 (statistical): the same cells and edges; the ellipse can need more tiles than the corridor: reported
    C2  continuum value        PASS  0 / 150 violate J_cont(priced) <= 1.0824 J_cont(base) + mu * 0.1414 * L(priced); median ratio 0.920
          rule: J_lat(P) <= J_lat(B) (S7) and J_lat vs J_cont differ by the 8-neighbour metrication 1.0824 (S12) and, in the deficit term, twice the clearance quantisation h / sqrt 2 = 0.1414 m per metre times mu
    C3  plan time (ALLOWANCE)  PASS  kappa 0 p50/p95/p99 1.7/2.8/3.4 ms, kappa p50/p95/p99 3.2/5.9/6.9 ms, p50 ratio 1.89, p99 ratio 2.01
          rule: p99 <= 0.1 x SCENE_GRAPH_PLAN_BUDGET_S (3.0 s) = 0.30 s: an ALLOWANCE, not derived; warm tile cache, in-memory store (live tile I/O latency is measured separately)
    C3c plan time cold (ALLOWANCE) PASS  COLD caches per plan: kappa 0 p50/p95/p99 3.1/4.5/5.2 ms, kappa p50/p95/p99 7.1/10.1/11.9 ms, p50 ratio 2.31, p99 ratio 2.28
          rule: the same allowance, p99 <= 0.30 s, with the clearance, tile and decoded-tile caches emptied before every plan (an ALLOWANCE, not derived; no network I/O: the live tile read latency comes on top)

    elapsed 5.5 s; ALL GATES PASS

The harness has teeth: with `priced_visible` replaced by the plain line of sight, A7 fails (104 of 150 plans have `J(final)` above
the lattice path); on a synthetic ring export an early version of A4 against the production baseline failed by 0.86 m, which is how
the corridor baseline's non-optimality (formulation.md statement 27) came to be handled as in section 5.

## 8. Verification ledger

Run in the worktree of branch `route-safety-v3` (from main 6879628) on 2026-10-13. "Pinned" is `scripts/run_unit_tests_pinned.sh`
(Python 3.10, pydantic 1.9.0, the production pins); "host" is the host Python 3.12. `<office8 export>` is the data directory of
`tools/scene_graph_sim/data/office8` of the scene-graph worktree.

**L1. Touched suites, pinned:**

    ./scripts/run_unit_tests_pinned.sh tests/unit/scene_graph/test_route_safety.py tests/unit/test_lazy_route_safety.py \
        tests/unit/test_scene_route_safety.py tests/unit/test_node_policy_route_safety.py tests/unit/test_accept_route_safety.py \
        tests/unit/test_node_policy_plan.py tests/unit/test_node_policy_plan_server.py tests/unit/test_node_policy_enrich_dispatch.py \
        tests/unit/test_planner_chart_route.py tests/unit/test_planner_chart_route_hardening.py tests/unit/test_lazy_plan.py \
        tests/unit/test_lazy_plan_equality.py tests/unit/test_scene_route.py tests/unit/test_config.py
    -> 459 passed in 168 s

**L2. The full unit suite, pinned:**

    ./scripts/run_unit_tests_pinned.sh tests/unit -m unit
    -> 4497 passed, 4 skipped, 539 deselected (other markers) in 220 s

**L3. The new tests, host:**

    python -m pytest -p no:randomly tests/unit/scene_graph/test_route_safety.py tests/unit/test_lazy_route_safety.py \
        tests/unit/test_scene_route_safety.py tests/unit/test_node_policy_route_safety.py tests/unit/test_accept_route_safety.py -q
    -> 202 passed in 93 s   (68 engine, 86 lazy, 43 planner and config, 3 nodePolicy, 2 harness)

**L4. Benchmark, office8** (`bench_route_safety.py`, 97 leaves, 150 pairs, seed 7; its sampling includes every vertex and the last
point, like the harness; every quoted table of this document is from one of the two tools, and they agree on `cur` and `whole`):

    python tools/scene_graph_sim/bench_route_safety.py --data <office8 export> --kappa 1
    ### office8: 97 leaves, N=2143 F cells, 12 charts, 15 links, r=0.250 m=0.354 mu=9.7/m (kappa 1.0), 150 pairs, seed 7
    method      J/J_ref    p90    max |  min clr  #<r unsafe m | len ratio med/p90/max |     ms
    cur           1.084  1.242  1.601 |    0.209   89     1.41 | 1.000 / 1.000 / 1.000 |    2.1
    whole         0.991  1.023  1.059 |    0.292   33     0.62 | 1.015 / 1.078 / 1.230 |    3.7
    corridor      0.991  1.023  1.059 |    0.292   33     0.62 | 1.015 / 1.078 / 1.230 |    4.7
    anchor        1.063  1.172  1.655 |    0.250   67     0.85 | 1.050 / 1.120 / 1.211 |   14.4
    portal        1.005  1.088  1.547 |    0.283   49     0.67 | 1.017 / 1.069 / 1.185 |   14.1
    corridor cell coverage of the lattice (mean, min): 0.777, 0.398

`whole` is the priced route (`route(..., safety=...)`, whole lattice, cost-aware pull); `corridor` searches the length corridor with the
priced weights and equals `whole` on these pairs; `cur` is the planner today. `--kappa 3` and `--kappa 10`, `whole` rows (`cur` is
1.293 / 1.810 / 2.905 and 1.922 / 3.527 / 6.613):

    kappa 3:   whole  J/J_ref 0.985 / 1.040 / 1.172   min clr 0.300   #<r 30   unsafe 0.49 m   length ratio 1.020 / 1.088 / 1.283
    kappa 10:  whole  J/J_ref 0.977 / 1.134 / 1.408   min clr 0.300   #<r 30   unsafe 0.36 m   length ratio 1.025 / 1.100 / 1.661

`J/J_ref` can be below 1 because the reference is a 16-neighbour path on the 0.05 m grid, not the continuum optimum, while the pulled
polyline is evaluated in the continuum.

**L5. Not re-run in this change.** S2 (158 of 200 pairs, the 2.2-2.35 x detours), the S12 observed maximum 0.0707 m, and the
B-plane slack-field comparison of section 6 (research script, a different sampling) come from the research scripts of the session
(not in the repository); the derivations of section 2 do not depend on them.
