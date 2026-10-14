# Inflation price profile: implementation and validation (experimental, 2026-10-14)

Merged into `main` and deployed on 2026-10-14 (planner `cac9f03`) with the exponential tier **off**: the three environment variables
are empty, so the live objective is still the linear hinge at kappa 1. The research scripts of the design are in
`tools/scene_graph_sim/research/inflation/`. Specification: `inflation_profile_design.md` sections 1-7, 11, 12. This file states
what was built, every test of the campaign with its number and verdict, the exact commands, and what the numbers do and do not
support.

## 1. What was built (and only that)

| Item | Where |
|---|---|
| Second tier of `api.Safety`: optional `k`, `origin`; `price(g)` = `exp(-k (g - origin))` for `g < margin` (= reach), else 0; `rate` = kappa, no normalisation; with `k` None the hinge is unchanged (`max(0, margin - g)`, same float operations) | `packages/scene_graph/api.py` |
| Validation in `Safety`: margin > 0, k > 0, origin > 0, all finite, k and origin set together, and `k * origin <= 10` (my addition: keeps `exp(k origin)` <= 22026 so every weight is a finite integer held exactly in float64) | `Safety.__post_init__` |
| One shared pure price, used by the engine (`ChartGraph._edge_cost`, the `route_ws` cache key is now the frozen `Safety`) and the lazy reader (`PlanGraph._search`) before the one `edge_weight`; `price_cell` on both graphs for the line of sight | `engine.py`, `lazy_plan.py` |
| Tier selection `safety_for`: exponential when `SCENE_GRAPH_INFLATION_RADIUS_M` and `SCENE_GRAPH_INFLATION_SCALING` are set and valid (reach <= `_CLR_CAP_M` 1.0), origin = `SCENE_GRAPH_GLOBAL_INSCRIBED_M`, else half the smaller footprint side, else the map's r; kappa = `SCENE_GRAPH_SAFETY_KAPPA` when set explicitly, else 0.5; invalid -> hinge with a warning; kappa <= 0 still disables | `scene_route.py`, `config.py` |
| `priced_visible` prices a sample through `price_cell` (non-navigable cell = price at clearance 0) | `scene_route.py` |
| Pricing record: `profile`, `k`, `origin`, `reach`, `floor` (origin for exp, inscribed radius for hinge), `waypoints_below_floor` (count of interior planner waypoints under the floor; non-navigable counts; moves nothing); log line and `/stats` (`priced_plans_exp`, `waypoints_below_floor`) | `scene_route._plan_on_graph`, `server.py` |
| Pass-through of the three variables (default empty = hinge); `SCENE_GRAPH_SAFETY_KAPPA` is now passed empty by default and empty means 1.0 (so "explicit" is detectable; the hinge value is unchanged) | `docker_compose/mission_dispatch_services.yaml`, `.env.example` |

Not changed: the hard graph F, the waypoint cap, node policy semantics, the legacy planner, the engine format. No per-robot store,
no endpoint. `waypoints_below_floor` is counted for the go-here plan only (`_plan_on_graph`); gap-fill waypoints of
`enrich_route` are not counted.

## 2. Commands

```
# unit tests (pinned: Python 3.10, pydantic 1.9.0)
./scripts/run_unit_tests_pinned.sh tests/unit/test_inflation_profile.py tests/unit/test_scene_route_safety.py \
    tests/unit/test_lazy_route_safety.py tests/unit/scene_graph/test_route_safety.py tests/unit/test_node_policy_route_safety.py -q
./scripts/run_unit_tests_pinned.sh                       # F: the full suite

# A: production-path gates, office8 engine export, 150 pairs seed 7 (D = .../cloud_server_scene_graph/tools/scene_graph_sim/data/office8)
python tools/scene_graph_sim/accept_route_safety.py --data $D --pairs 150 --kappa 1                      # hinge (regression)
python tools/scene_graph_sim/accept_route_safety.py --data $D --pairs 150 --profile exp --k 6 --reach 0.8 --origin 0.3 --kappa 0.5
python tools/scene_graph_sim/accept_route_safety.py --data $D --pairs 150 --profile exp --k 10 --reach 1.0 --origin 0.25 --kappa 2
python tools/scene_graph_sim/accept_route_safety.py --data $D --pairs 150 --seed 11 --profile exp --k 4 --reach 0.55 --origin 0.3 --kappa 0.25

# B, C: lattice-level grid and perturbation (maps.json = research data of the 4 maps)
python tools/scene_graph_sim/inflation_campaign.py grid    --maps maps.json --out grid.json --jobs 22
python tools/scene_graph_sim/inflation_campaign_report.py grid.json
python tools/scene_graph_sim/inflation_campaign.py perturb --maps maps.json --out perturb.json --eps 0.07

# D: synthetic worlds
python tools/scene_graph_sim/inflation_efficiency.py --pairs 30 --sizes 7x5,20x10,50x24

# E: operational pairs (read-only GETs on localhost:8000)
python tools/scene_graph_sim/inflation_operational.py --maps maps.json
```

## 3. Results

### A. Production-path gates (office8 engine export, 97 leaves, 2143 F cells, 12 charts, 150 pairs)

Exp profile k 6, reach 0.8, origin 0.30, kappa 0.5 (the design's reference), the gate set of
`accept_route_safety.py` unchanged in statement; B1 and B2 are stated for the price phi (derivation printed by the harness):
with J = L + kappa A (+ rounding n 1e-4), J(P) <= J(o) and L(P) >= L(o) - n 1e-4 give A(P) <= A(o) + n 1e-4 / kappa (B1), and with
A(P) >= 0, L(P) <= L(o) (1 + kappa phibar_o) + n 1e-4 (B2).

| Id | What | Result (exp 6 / 0.8 / 0.30 / kappa 0.5) | Verdict |
|---|---|---|---|
| A1 | kappa 0 equals the unpriced route bit for bit (nodes, cost; lazy and engine; plans) | 0 / 150 routes, 0 plans differ | PASS |
| A2 | routable sets equal | 150 / 150 routable, 150 priced, 150 planned | PASS |
| A3 | lattice optimum equals the independent reference; J(priced) <= J(baseline cells, priced weights) | 0 / 150 differ, 0 dominated | PASS |
| A4 | length >= length optimum - n 1e-4 | 0 / 150 violations (largest shortfall 0.000000 m) | PASS |
| A5 | lazy == engine (nodes, cost) | 0 / 150 differ | PASS |
| A6 | cold == warm | 0 / 150 differ | PASS |
| A7 | waypoints in F and J(final) <= J(lattice) + n 1e-4 | 0 plans leave F, 0 exceed | PASS |
| B1 | A_phi(priced) <= A_phi(base) + n 1e-4 / kappa | 0 / 150 violations | PASS |
| B2 | L(priced) <= L(base) (1 + kappa phibar_base) + n 1e-4 | 0 / 150 violations | PASS |
| C1 | no new fallback | 0 newly falling back, no reason at kappa 0 or kappa | PASS |
| C2 | continuum value bound (hinge Lipschitz argument) | not evaluated for the exp tier: the bound uses the hinge's unit slope, the exp price has slope up to k e^{k origin} and a step at reach | NOT RUN |
| C3 | plan time warm | baseline p50/p95/p99 1.8 / 2.8 / 3.0 ms, exp 3.8 / 6.7 / 7.6 ms (allowance 300 ms) | PASS |
| C3c | plan time cold | baseline 3.3 / 4.7 / 6.1 ms, exp 8.0 / 11.9 / 14.6 ms | PASS |

Other parameter sets on the same export, all gates: hinge kappa 1 (regression of the unchanged tier): ALL PASS, warm p99 6.2 ms,
cold 12.0 ms; exp k 10 / reach 1.0 / origin 0.25 / kappa 2: ALL PASS (warm p99 10.1, cold 14.1 ms); exp k 4 / reach 0.55 /
origin 0.30 / kappa 0.25, seed 11 pairs: ALL PASS (warm p99 7.5, cold 11.9 ms). Production baseline vs exp (kappa 0.5) route length
ratio: median 1.021, p90 1.072, max 1.192.

Unit tests (new, `tests/unit/test_inflation_profile.py`, plus the changed assertion of the pricing record in
`test_scene_route_safety.py`): price properties (continuous at origin, monotone non-increasing, exp(k origin) at 0, zero at and
after reach, finite integer weights >= the unpriced weight); the hinge price equals `max(0, margin - g)` bit for bit; engine ==
lazy on the equality worlds (`test_lazy_plan_equality.ALL`) including tile-edge shifted worlds for four profiles incl. reach 0.8, 1.0, 0.55 and kappa 0 ==
unpriced; `price_cell` equal between the readers; degenerate inputs (k 0, negative, huge, NaN, inf; reach <= 0, NaN, > 1.0; origin
<= 0, inf; origin > reach and reach below r stay finite; footprint unknown; margin <= r); the guard count against an independent
count with waypoint positions asserted unchanged when the guard is stubbed out; tier selection from config. Result in section F.

| Id | What | Verdict |
|---|---|---|
| U-price | price properties | PASS |
| U-parity | engine == lazy, exp tier, tile edges, reach 0.8 / 1.0 / 0.55, kappa 0 | PASS |
| U-degenerate | invalid / degenerate inputs raise in `Safety`, fall back with a warning in the planner | PASS |
| U-guard | count equals independent count; positions unchanged | PASS |
| U-tier | tier selection from config | PASS |

### B. Robustness grid (lattice level, 4 maps, 3 seeds x 120 pairs; 96 cells x 5 kappas)

k {4, 6, 8, 10} x reach {0.55, 0.8, 1.0} x origin {0.25, 0.30} x kappa {0.25, 0.5, 0.75, 1, 2}; 172 800 pair routes. The model is the
research lattice (same weights, same cost-aware pull), not the production engine.

| Id | What | Result | Verdict |
|---|---|---|---|
| B-reach | no pair that was reachable becomes unreachable | 0 of 172 800 | PASS |
| B-exact | integer J of the path recomputed from the formula == Dijkstra distance; J(priced) <= J(baseline path, priced weights) | 0 / 172 800 violations each | PASS |
| B-A4 | L(priced) >= L(base) - n 1e-4 | 0 violations | PASS |
| B-B1 / B-B2 | generalised intrusion / detour bounds | 0 / 0 violations | PASS |

Metrics over the grid (seed mean; length increase of the pulled polyline against kappa 0, robot cost reduction = 1 - cost per
metre / cost per metre at kappa 0 under the profile's own cost, share of length with clearance < 0.35 m):

| map | mean length increase: min / median / max | p90: min / median / max | cost reduction: min / median / max | share < 0.35 m: min / median / max (at kappa 0, grid mean: office8 22%, office7 29%, office6 32%, Hospital 7%) |
|---|---|---|---|---|
| office8 | 0.5% / 4.7% / 11.2% | 1.9% / 9.3% / 19.2% | 7% / 46% / 73% | 2.7% / 6.0% / 17.6% |
| office7 | 0.3% / 5.0% / 12.5% | 1.7% / 10.4% / 22.6% | 7% / 45% / 71% | 3.1% / 7.1% / 23.5% |
| office6 | 0.3% / 5.0% / 12.6% | 1.7% / 9.7% / 21.5% | 7% / 45% / 68% | 5.9% / 9.2% / 25.3% |
| Hospital | -0.1% / 0.9% / 4.9% | 0.6% / 3.5% / 14.8% | 26% / 68% / 93% | 0.1% / 0.3% / 2.6% |

Reference cell k 6, reach 0.8, origin 0.30 (mean / p90 length increase, cost reduction, share < 0.35 m):

| kappa | office8 | office7 | office6 | Hospital |
|---|---|---|---|---|
| 0.25 | 1.2 / 3.3 %, 16 %, 13.4 % | 1.2 / 3.6 %, 14 %, 19.5 % | 1.2 / 3.2 %, 16 %, 20.3 % | 0.1 / 1.0 %, 37 %, 2.1 % |
| 0.5 | 3.5 / 7.1 %, 34 %, 7.0 % | 3.8 / 8.2 %, 32 %, 8.8 % | 3.8 / 7.8 %, 33 %, 10.5 % | 0.7 / 2.9 %, 57 %, 0.5 % |
| 0.75 | 5.4 / 10.1 %, 45 %, 5.3 % | 5.9 / 11.5 %, 43 %, 5.9 % | 5.7 / 11.1 %, 43 %, 8.0 % | 1.3 / 4.4 %, 68 %, 0.2 % |
| 1 | 6.9 / 12.9 %, 50 %, 4.4 % | 7.4 / 13.8 %, 48 %, 4.7 % | 7.0 / 12.9 %, 47 %, 7.2 % | 1.7 / 5.7 %, 75 %, 0.2 % |
| 2 | 10.3 / 17.7 %, 58 %, 3.1 % | 10.7 / 18.9 %, 56 %, 3.6 % | 10.1 / 18.4 %, 55 %, 6.3 % | 3.0 / 9.2 %, 87 %, 0.1 % |

This reproduces the design's sweep (office kappa 0.5: 3.3 / 4.2 / 4.4% mean there, 3.5 / 3.8 / 3.8% here with three seeds) and the
statement that kappa 0.5 meets the budget and 0.75 does not on the office maps at origin 0.30.

Smoothness (largest difference between neighbouring grid cells, seed mean, one step along one axis; each over all other axes and maps):

| axis | mean length increase | p90 | cost reduction | share < 0.35 m |
|---|---|---|---|---|
| k (4 -> 6 -> 8 -> 10) | 2.9 points (office6, kappa 2) | 6.8 points (Hospital, kappa 2) | 12.5 points | 3.8 points |
| reach (0.55 -> 0.8 -> 1.0) | 2.8 points | 5.9 points | 25.0 points | 3.4 points |
| origin (0.25 -> 0.30) | 2.2 points | 4.3 points | 14.0 points | 6.3 points |
| kappa (0.25 -> 0.5 -> 0.75 -> 1 -> 2) | 5.6 points (1 -> 2, office6) | 9.2 points | 28.6 points (Hospital 0.25 -> 0.5) | 10.6 points |

Verdict B-smooth: no discontinuity; the largest single-step changes are the kappa steps (a doubling of kappa from 1 to 2 adds
5.6 points of mean length; that is the proportional response, not a jump) and the robot-cost reduction, which is not comparable
across reach/origin (the cost is evaluated under each cell's own profile, so a change of reach changes the unit). The change of
length per step is bounded; mean and p90 are non-decreasing in kappa in 573 of 576 (cell, seed, metric) series, the 3 exceptions are
dips of the pull (a straightening that happens to fit), not trends. PASS.

Budget rule (mean <= 5% and p90 <= 10%): the largest grid kappa meeting both, per (map, k, reach, origin) and seed. No cell has a
seed with no admissible kappa. The same kappa is chosen in all three seeds in 65 of 96 cells; the seeds differ by one grid step in 29
cells and by two steps in 2; never more. Chosen kappa counts over the 24 cells x 3 seeds of a map: office8 0.5: 39, 0.75: 19, 1: 14;
office7 0.25: 6, 0.5: 35, 0.75: 25, 1: 6; office6 0.25: 9, 0.5: 29, 0.75: 26, 1: 8; Hospital 0.75: 1, 1: 12, 2: 59. At the
design's reference cell (k 6, reach 0.8, origin 0.30), on the seed mean, the rule gives 0.5 on the three office maps and 2 (the grid
maximum) on Hospital.
Seed spread of the mean length increase of one cell: median 0.4 points, max 2.1 points. Verdict B-budget: **stable to one grid step
in 98% of cells (94 of 96), seed-invariant in 68% (65 of 96)** (2 of 96 cells move two steps); the rule selects kappa in {0.5, 0.75} for office maps and
the choice depends on origin and reach more than on the seed. PARTIAL: the rule is stable enough to pick a fleet default (0.5) but a
per-map calibrated value would change by one step with a re-draw of 120 pairs in about a third of the cells (31 of 96).

### C. Perturbation (lattice, clearance noise uniform +-0.07 m on g, 40 pairs x 3 seeds per map, k 6, reach 0.8, kappa 0.5 and 1)

| map / origin / kappa | route changed | dJ per metre mean / max (under clean g) | literal S6 bound kappa eps per m | ex-post S6 bound holds |
|---|---|---|---|---|
| office8 0.30 / 0.5 | 91% | 0.0037 / 0.0268 | 0.035 | 100% |
| office8 0.30 / 1.0 | 92% | 0.0065 / 0.0537 | 0.070 | 100% |
| office7 0.30 / 0.5 | 86% | 0.0033 / 0.0303 | 0.035 | 100% |
| office6 0.30 / 0.5 | 93% | 0.0036 / 0.0362 | 0.035 | 100% |
| Hospital 0.30 / 0.5 | 68% | 0.0007 / 0.0071 | 0.035 | 100% |

(all 16 rows: `inflation_campaign.py perturb`; range of "route changed" 66-96%, mean dJ per metre 0.04-0.69%, per-pair max 0.7-5.4%.)
The lattice route is not robust under quantisation-sized noise: it changes on 66-96% of the pairs, as expected in a field with many
near-ties, but the **cost of the changed route under the true clearance stays within 0.04-0.7% per metre on average**. The S6 bound
with the hinge's unit Lipschitz constant (mu eps L per route, here kappa eps per metre) is exceeded by the largest single pair in
a few cells (only office6 origin 0.30 kappa 0.5: max 0.0362 > 0.035; the other 15 rows are below it) because the exp price slope
reaches k e^{k origin} (36 at g 0) and has a step at reach. The bound that holds by construction is the ex-post one (kappa times
the summed |phi - phi'| along both paths plus rounding): 100% of 1920 pairs. Verdict C: route identity NOT robust to
+-0.07 m noise (and this is not claimed); J change bounded and small; literal unit-slope S6 bound not valid for the exp profile (FAIL
as a literal statement in 1 of 16 rows, by 0.001 m per metre on the single worst pair), ex-post bound PASS.

### D. Efficiency (synthetic worlds from `random_world`, 30 pairs >= 3 m, route call only; ms p50 / p95 / p99)

| world | F cells | reader | unpriced | hinge | exp |
|---|---|---|---|---|---|
| 7x5 | 2 169 | engine whole-graph | 0.8 / 0.8 / 0.9 | 0.6 / 0.7 / 0.8 | 0.6 / 0.6 / 0.7 |
| | | lazy warm | 1.0 / 1.2 / 1.2 | 1.8 / 2.1 / 2.3 | 2.0 / 2.4 / 2.4 |
| | | lazy cold | 1.5 / 1.8 / 1.9 | 4.0 / 5.5 / 7.0 | 3.6 / 3.9 / 3.9 |
| 20x10 | 16 774 | engine | 2.1 / 3.3 / 3.5 | 3.8 / 4.3 / 4.4 | 3.9 / 4.1 / 4.2 |
| | | lazy warm | 2.5 / 4.5 / 4.6 | 3.9 / 10.1 / 10.8 | 5.0 / 11.3 / 13.1 |
| | | lazy cold | 4.2 / 6.1 / 8.5 | 8.0 / 14.7 / 15.3 | 8.0 / 16.0 / 16.1 |
| 50x24 | 112 228 | engine | 7.6 / 14.8 / 16.9 | 45.1 / 57.3 / 60.1 | 43.1 / 52.2 / 55.3 |
| | | lazy warm | 6.4 / 13.5 / 14.7 | 17.5 / 73.0 / 79.0 | 15.1 / 68.2 / 82.9 |
| | | lazy cold | 9.0 / 16.3 / 18.0 | 24.3 / 85.1 / 87.2 | 24.2 / 87.7 / 89.3 |

Office8 production path (plan_on_graph, 150 pairs, see A): hinge kappa 1 warm p50/p99 3.2 / 6.2 ms, cold 7.6 / 12.0 ms; exp warm
3.8 / 7.6 ms, cold 8.0 / 14.6 ms; baseline 1.7-1.8 / 3.0 ms warm, 3.3 / 5.3-6.1 ms cold.

Scaling: the priced search is roughly linear in F cells (engine 4 ms at 17k, 45 ms at 112k, i.e. 6.7x cells for 11x time;
the unpriced route searches only a corridor). The exp tier against the hinge tier on the same world: ratio of p50 between 0.86
and 1.28 (largest: 20x10 lazy warm 5.0 vs 3.9 ms), of p99 between 0.56 and 1.23 (largest: office8 production warm 7.6 vs 6.2 ms,
20x10 lazy warm 13.1 vs 10.8 ms), i.e. **no more than about 1.3x** and equal within noise on the large world. Priced searches cost
up to 6x the unpriced one in both tiers (the unpriced route searches a corridor, the priced one the ellipse / whole graph);
all plan times at 112k cells stay far below the 3 s plan budget (route call p99 under 0.1 s). Memory:
the lazy clearance cache is one float64 tile (64 x 64, 32 KB) per touched tile:
0.07 MB / 0.26 MB / 1.05 MB for the three worlds after a warm pass (16-30 B per F cell; the number of tiles touched bounds it, not the
profile: it is independent of reach, the 1.0 m halo is always read). Engine `sp.clr` 8 B per F cell (0.02 / 0.13 / 0.90 MB).
Verdict D: PASS (exp <= ~1.3x hinge; scale roughly linear). Timing was taken on a shared 24-core host; single runs, p99 over 30 pairs.

### E. Operational pairs (office7, the 40 missions of runs?limit=40, all 2026-10-13, formidable-peacock; lattice model of office7)

First and last waypoint of each mission's stored route (40 distinct pairs, all snap within 0.5 m of an F cell, all one component).
Planned on the research lattice of office7 (F and clearance of the read API of 2026-10-14; not the engine of that day's
version), same pull. Hinge = live tier (margin 0.354, r 0.25, kappa 1); robot cost per metre under k 6 / reach 0.8 / origin 0.30.

| tier | length vs kappa 0: median / mean / p90 / max | robot cost per m | cost vs hinge | length vs hinge (mean) | share < 0.35 m |
|---|---|---|---|---|---|
| kappa 0 | 1 | 126.2 | | | 30.7% |
| hinge kappa 1 | 1.029 / 1.049 / 1.090 / 1.544 | 99.7 | 1.000 | 1.000 | 9.5% |
| exp kappa 0.5 | 1.043 / 1.043 / 1.073 / 1.127 | 84.3 | 0.845 | 0.999 | 9.9% |
| exp kappa 0.75 | 1.081 / 1.084 / 1.117 / 1.560 | 64.4 | 0.646 | 1.033 | 3.5% |
| exp kappa 1.0 | 1.113 / 1.103 / 1.145 / 1.587 | 55.5 | 0.557 | 1.052 | 3.1% |

At kappa 0.5 the exp tier drives the same distance as today's hinge (ratio 0.999) at 15% lower robot inflation cost per metre and
the same share of length near obstacles (9.9% vs 9.5%): it is not "closer" in that statistic, it moves the length from the very high
cost cells to the mid-cost band. 3 of 40 pairs have identical length under hinge and exp 0.5. Verdict E: informative, no gate.
Not a robot outcome (see section 5).

### F. Full pinned suite

`./scripts/run_unit_tests_pinned.sh` (default full run, commit a0e1586 plus this file): **4651 passed, 4 skipped, 539 deselected, 0 failed**
in 258 s (Python 3.10, pydantic 1.9.0). Verdict PASS. The suite before this change was not re-run for a count; the changed existing
assertion is the key set of the pricing record in `test_scene_route_safety.py`.

## 4. Pricing record on the office8 production path (waypoint guard)

150 pairs: hinge kappa 1: 476 interior waypoints, 1 below the floor 0.25 m (0.2%); exp kappa 0.5: 433 interior, 16 below 0.30 m
(3.7%); exp kappa 1.0: 549 interior, 9 below (1.6%). Positions are never changed by the guard (unit test). The design's 2.2% at
kappa 0.75 is the same order.

## 5. What can be claimed

* The exponential tier is implemented as a second tier of the one `Safety` type; `k` None is bit-for-bit today's hinge (the
  existing safety tests and the hinge run of the production harness pass unchanged).
* On the production path (engine export of office8, 150 pairs, four parameter sets) the exactness gates A1-A7, B1, B2, C1 hold
  with zero violations: kappa 0 is the unpriced route, the priced route is the lattice optimum of the integer weights (equal to
  an independent reference), the lazy reader and the whole-graph engine are identical, cold equals warm, waypoints stay in F and
  cost no more than the lattice path, and the generalised intrusion / detour bounds hold.
* At lattice level on four maps, 96 parameter cells x 5 kappas x 3 seeds (172 800 routes): no pair becomes unreachable, the
  optimum and the generalised bounds hold in every one, and length increase and cost reduction vary smoothly (non-decreasing in kappa in 573 of 576 series;
  no step larger than the proportional kappa step).
* Cost: at most about 1.3x the hinge tier in plan time and no extra stored data (clearance tiles of the 1 m halo already exist);
  112k F cells plan in under 0.1 s p99.
* With the design's reference parameters the budget rule (mean <= 5%, p90 <= 10%) picks kappa 0.5 on the three office maps; the
  length increase is 3.5-3.8% mean, 7-8% p90, robot inflation cost 32-34% lower, share of length under 0.35 m from 22-32% (kappa 0) to 7-11% (office maps, reference cell).
  On the 40 missions the robot drove on 2026-10-13 (lattice model) kappa 0.5 costs the same distance as the live hinge at 15% lower
  inflation cost.
* A guard count exists and is a count only.

## 6. What is not shown

* **No robot outcome data.** Nothing here says the robot stops less, recovers less or reaches goals more often with the exp profile;
  the robot cost is the diagnostic min(253, 252 phi), not an observed quantity. The waypoint guard exists to collect that evidence.
* The pairs are random (>= 3 m apart in one component) or the 40 missions of one robot on one day; not a workload sample.
* The robot values (inflation radius 0.8, scaling 6.0, global inscribed radius 0.30) come from the repository's scratch override;
  that the live robot runs them is not confirmed (design section 12 items 1-4). The profile is only as right as these.
* B, C and E run on the research lattice (the stored F and clearance, the same weights and pull), not the production engine; the
  production path ran on the one office8 export. The lattice model's clearance is cm-rounded and capped at 2.55 m; the
  production reader caps at 1.0 m (identical for reach <= 1.0).
* The route is not invariant to clearance noise of one lattice quantisation (66-96% of routes change); only the J change is small.
  The literal unit-slope S6 bound does not hold for the exp profile.
* The budget rule's selected kappa is stable to one grid step, not seed-invariant; a per-map calibration was not built or tested.
* Gap-fill waypoints of `enrich_route` are not counted by the guard; `operator_waypoint_below_floor` of the design is not implemented.
* Timing is single runs on a shared host on in-memory stores; live tile I/O latency comes on top.
* The reach step at `reach` (price jumps from phi(reach) to 0) is the robot's own and is not smoothed; its effect is inside the
  numbers above but was not studied separately. `k * origin <= 10` is a guard of this implementation, not of the design.
* Nothing deployed; `docker_compose/.env` untouched; the compose default for `SCENE_GRAPH_SAFETY_KAPPA` changed from `1.0` to empty
  (config default 1.0): behaviour identical unless a variable is set.
