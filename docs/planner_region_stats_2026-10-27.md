# Region statistics in the mission planner (design, 2026-10-27)

Status: **design only**, nothing implemented. Builds on `docs/region_stats_2026-10-27.md` (merged to local `main` 0c16088,
not deployed). Line numbers are of `main` at 0c16088. "Measured" means a read-only query or a read-only run on the cloud box
on 2026-10-27 (nothing written, nothing deployed).

Confidence marks: **[firm]** = I am confident, argued below; **[default]** = a reasonable default, should be tuned by the
replay harness; **[user]** = your call (section 9).

## 0. Summary

* **The integration is one number per chart: excess time per metre.** The stats side produces, per chart of the planned
  graph version, `rho_c >= 0`: the extra time a metre in that chart takes over driving it at the factsheet `speed_max`,
  times `speed_max`. The planner adds `ceil(len_units * (rho_a + rho_b) / 2)` to every lattice edge, on top of today's cost.
  Nothing else in the search changes. [firm]
* **Time is the currency, and today's cost already uses it.** The turn cost is `speed_max / angular_speed_max` metres per
  radian (`scene_route.py:781-796`), which is the rotation time multiplied by `speed_max`. So `J * 1e-4 m` already means "seconds at
  `speed_max`". The stats term fits this unit directly. The clearance price stays what it is today: a safety preference in
  metres, not a time. [firm]
* **Admissibility holds by construction.** Every edge still costs at least its length, so the ellipse bounds and the Dijkstra
  stop in `lazy_plan.py:622-637` and `:677-684` stay valid. A tighter bound (divide by `1 + rho_min`) is optional. [firm]
* **Risk.** Expected time is additive, so it goes inside the search. The tail (p90, block risk) is not additive. It is
  deferred to a later stage: a certainty-equivalent variance term if the builder stores a variance rate, and k chart-path
  alternatives only if a map with real loops gets enough data. [firm on the split, default on the deferral]
* **Modularity.** Terms are pure functions from a chart's statistics chain to seconds per metre, combined by one small
  composer configured by name. The planner sees only an immutable snapshot `RegionPricing`, keyed by
  (map, graph_version, robot_class, stamp, model_hash).
* **Readiness: not ready to steer routes; ready to start building.** The replay harness can start now. The planner plumbing
  can start now and run in shadow mode after deploy and backfill. Turning it on waits for shadow evidence. The maps with data
  have almost no alternative routes (cycle rank 2), so the first real gain is predicted time (ETA), not route choice.

## 1. Corrections to the starting hypothesis (verified in code)

| Hypothesis | What the code does |
|---|---|
| "Stage 1 picks the corridor, Stage 2 searches it" | Only without pricing or an overlay. With a `Safety` (clearance price or turn cost), the corridor route is only a **seed**: the answer is the optimum over every chart inside the ellipse that `J_corridor` implies (`lazy_plan.py:622-637`). Region costs inside Stage 2 therefore already choose the side of a loop. k corridors are not needed for optimality. |
| "Put the stats stamp in the edge-cost cache key" | **There is no edge-cost cache.** Edge costs are computed per request (`lazy_plan.py:539-545`). The caches are graphs per (map, version) (`GraphCache`, `scene_route.py:283`), tiles, and the clearance per tile (`lazy_plan.py:176`, `:295-331`). None of them depends on pricing. The new cache is the snapshot cache (section 4.3). |
| "Multipliers >= 1 relative to speed_max" | Correct, but the term should be **added to** today's cost, not multiplied with it. A multiplier would change what kappa means in every chart (section 5.1). |
| Region-to-cell mapping needs the owner raster | The search already has the chart index of every cell (`ch` from `_cells`, `lazy_plan.py:497`, `:410-432`). The chart factor is `rho[ch]`: an array lookup, with no extra raster read. |

Also: `route()` drops the `Safety` when `rate == 0` and `turn == 0` (`lazy_plan.py:602-603`), which skips the global
refinement (`:622`). With region costs on, that condition must count the region term as a price. Otherwise the plan stays
inside the 1-ring corridor and is not optimal.

## 2. Readiness verdict and prerequisites

**Measured state (live DB, read-only):**

* 342 finished runs. Two maps have enough data: office7 (141 runs, 11-16 Oct) and office9 (135 runs, 16-27 Oct). The next
  map has 17. Recent rate: about 36 runs per week.
* One robot in practice: formidable-peacock has 323 runs, masked-frigatebird 19. That is effectively **one class**:
  `CARRIER|0.50x0.50|0.24`, factsheet `speed_max` 0.24.
* The graphs are small: office9 v216 has 22 charts (13 components), office7 v87 has 11 charts. Both have about 20 m2 of free
  space and **cycle rank 2**. Officemap and office8 also have cycle rank 2. Hospital has cycle rank 45 but only 14 runs.
  Stats can only change the route where a loop exists, so today they would rarely change it.
* Coverage (deployed drive-stats, 90 days; `n` = runs touching the chart): office9 has 9 charts driven, 8 of them with
  `n >= 10`. office7 has 11 driven, 7 with `n >= 10`. 92 % of driven cells on office9 fall inside a chart (1396 of 1516). The
  owner-raster grouping (`drive_stats.regions_of`, `drive_stats.py:73-117`) therefore works on a real graph in the
  drive-stats path.
* Signal: moving speed per chart is 0.13-0.24 m/s against `speed_max` 0.24. That is a moving excess `rho` of 0-0.85, a
  well-behaved range. The stopped share is 0.23-0.92. One chart on office9 shows 69 s/m with 92 % stopped, and one on
  office7 37.5 s/m with 83 % stopped. These are almost certainly start, goal or dock dwell, not transit (prerequisite P3).
* Today's leg predictor, `expected_s` (`run_legs.py:38-54`), gives median `duration_s / expected_s` = 0.78 over 1127 office9
  legs. That is the baseline to beat.
* Plan time (warm, lazy planner in the running planner container, 30 random chart pairs per map): plain 0.9-1.8 ms median,
  priced 1.7-3.3 ms median, 7.2 ms max. The budget is `SCENE_GRAPH_PLAN_BUDGET_S` = 3.0 s (`config.py:758`). Performance is
  not a constraint at this map size.

**Prerequisites before turning it ON:**

| # | What | Blocks |
|---|---|---|
| P1 | Deploy the stats pipeline and backfill `run_cells` (user-gated). Check that the worker writes `region_stats` for office7 and office9 at their current versions. | everything online |
| P2 | Prove the builder and the planner agree on chart assignment. Test: for one version, the chart of every `run_cells` cell via `regions_of` equals the chart from `PlanGraph` `own` -> `_lut` (`lazy_plan.py:156-158`, `:247-267`). Also pin one version across chunks: `regions_of` takes the version from the first chunk only (`drive_stats.py:98`), and each `owner_plane` reads the current version again (`scene_graph_reads.py:425`), so a map over 160 m can mix versions during a rebuild. | planner on |
| P3 | Separate stops at the endpoints from transit stops. `run_cells` cannot tell a goal wait from a stop in traffic. Proposed `algo_version` 2: store stopped time within R of the run's start or goal, or of an order node with an action, as `dwell_s` and keep it out of `stopped_s`. Until then, only the **moving** term is trustworthy. | stop term |
| P4 | Decide the robot-class key **before the backfill** (Q2). `robot_class` includes `speed_max` (`robot_class.py:126-133`) and is read from the robot's **current** factsheet at summary time (`run_cells.py:198`). The backfill would label every historical run with today's factsheet, and any `speed_max` change splits the history. | class fallback |
| P5 | Shadow evidence: predictions are calibrated and beat `expected_s` and plain pace x length on office7 and office9 (section 7). | planner on |

**What can start now, in parallel:** the replay harness (S1). `summarise_rows` is pure, and section 11 of the spec already
computed run cells in memory from live rows, so S1 needs no deploy. The term and snapshot module (S2) and the planner
plumbing with the flag off (S3) can also start now.

## 3. Architecture: the seam

```
run_cells --(API worker)--> region_stats rows ----------------------+
                             (map, version, chart, class|'*')       |
                                                                    v
  packages/utils/region_cost.py (pure, NEW)                         |
    Term registry  --+                                              |
    CostModel(cfg) --+--> build_snapshot(stats_class, stats_pooled, keys, speed_max, model)
                                       |
                                       v
                 RegionPricing (immutable): rho[k] (int per mille, aligned to PlanGraph.keys),
                   rho_min, provenance[k], stamp, model_hash, graph_version, robot_class, status
                                       |
  mission_planner/server.py: snapshot cache (TTL on region_stats_builds.stamp)
                                       |
  scene_route.plan_on_graph(..., region=RegionPricing) -> graph.route(..., region=) -> _search: + edge excess
                                       |
  ChartPlan.pricing["region"] -> response -> MissionRouteNodeV1 / trajectory legs -> run_legs.predicted_s
```

**Fixed (not pluggable):**

* The unit (seconds x `speed_max`, as `1e-4 m` integers).
* The **form** of the in-search term: a non-negative, per-chart, per-metre excess, averaged over an edge's two end cells.
  This is the same convention as the clearance price, `api.edge_weight` (`api.py:161-166`).
* Exact match on graph version, and the provenance record.

**Pluggable:**

* Which terms contribute and with what weights.
* Shrinkage strength and fallback chain.
* The cap.
* Later, ranking terms.

All of this lives in one `CostModel` config. Its `model_hash` is recorded on every plan.

### 3.1 Term interface (small, no framework)

```python
@dataclass(frozen=True)
class Term:
    name: str            # "moving", "stop", later "var"
    version: int         # bump when the formula changes (enters model_hash)
    kind: str            # "excess_s_per_m" (additive, in search) | "route" (ranking only, later)
    def value(self, chain: StatChain, prior: Prior) -> Contribution: ...
        # Contribution(s_per_m >= 0, n_eff, level: "class" | "pooled" | "map" | "prior")
```

* `StatChain` holds the chart's `RegionStat` for the planning class, the chart's pooled `'*'` row, the map-wide sums for the
  class and for the pool (computed from the same rows by the snapshot builder), and `speed_max`.
* Terms read only `region_stats_load.RegionStat` fields (`region_stats_load.py:27-49`). A new figure means a new
  `region_stats` column and an `algo_version` bump, never planner code.
* v1 terms:
  * `moving`: `max(0, (time_s - stopped_s) / len_m - 1 / speed_max)`, shrunk.
  * `stop`: `stopped_s / len_m`, shrunk. It is off until P3 is done.
* `time_s = moving + stopped` exactly. Recovery time sits inside both, so it is **not** a separate term (no double counting).
  The hold after a block is stopped time, so a block term would double count expected time. Blocks belong only to a risk
  term (section 6).

### 3.2 CostModel and snapshot

* `CostModel` = `{terms: [(name, weight)], n0, rho_cap, quantum}` from config (default: `moving` only, weight 1, n0 = 5,
  cap 3.0, quantum 1/1000). `model_hash` = sha1 of the canonical JSON plus the term versions.
* `build_snapshot` is pure. It computes per chart: `rho_c = speed_max * sum_t w_t * value_t`, clamped to
  `[0, rho_cap]` and rounded to the quantum. The rounding makes costs deterministic across processes. A chart with no data
  gets its prior, so the result is never "missing".
* `RegionPricing` is frozen. `rho` is an int32 array aligned to `PlanGraph.keys`: the keys of the **same version**, matched
  by key. `rho_min` = the minimum over all charts. `provenance` counts the fallback levels.

## 4. Statistical soundness

### 4.1 Units [firm]

Time is the natural currency, because it is what the operator waits for and what the statistics measure. The planner's
cost is already in time:

* **Length:** `l / speed_max` seconds becomes `l` metres after multiplying by `speed_max`.
* **Turn:** `lambda = speed_max / angular_speed_max` metres per radian is the rotation time multiplied by `speed_max`
  (`scene_route.py:781-796`).
* **Region excess:** `rho * l` follows the same scaling.

So, with clearance pricing off, `J * 1e-4 / speed_max` is a predicted time. With pricing on, the clearance part is a
preference and must be subtracted to get time. The planner therefore logs `predicted_s` computed separately from the
lattice path (section 7), not from J.

The only number that is not a time is the clearance price. It should stay a separate, explicit preference.

### 4.2 Sparse data and cold start [default]

**Shrinkage** per chart c and term t:

```
x_c = (n_c * xbar_c + n0 * prior) / (n_c + n0)
```

* `n_c` = `runs`, the samples of at least `REGION_STATS_MIN_LEN_M` (`region_stats.py:86-115`).
* `xbar_c` = ratio of sums (`time / len`). This is the length-weighted mean, the right estimator for expected time, which is
  additive. Medians are not additive, so they are not used inside the search.
* n0 = 5 for a start. The replay harness picks the value; `link_cost_estimation_2026-10-13.md` section 6 suggested 2-5.

**Fallback chain** for the prior:

1. **Class map-wide:** the same term over all charts of the map for this class.
2. **Pooled, relative:** `'*'` chart value / `'*'` map-wide value x class map-wide value. Relative slowness transfers
   across classes; absolute pace does not, because `'*'` mixes speeds (`speed_max` is NULL on `'*'`, `region_stats.py:14-17`).
3. **Factsheet:** excess 0.

The prior is the **map-wide** figure, not the factsheet. Otherwise every chart nobody has driven would look faster than
every driven one, and the planner would chase unexplored areas. With one class today, levels 1 and 2 coincide.

### 4.3 Version, staleness, aging

* **Exact graph version only [firm].** The planner can serve an older cached version (`GraphCache.get`, `tolerate_stale`,
  `scene_route.py:541-610`). The snapshot is loaded for the version actually planned on. If that version has no build, the
  term is neutral and the record says `status: "no_build_for_version"`.
  * The worker rebuilds at most `RUN_CELLS_POLL_S` + `REGION_STATS_DEBOUNCE_S` (about 60 s) after a version change.
  * Mapping by key across versions is rejected: a key can keep its name while its cells change (spec, section 1).
* **Snapshot cache** in the planner: a small LRU keyed by (map, version, class, stamp, model_hash). Freshness is checked by
  reading `region_stats_builds.stamp` at most every `REGION_COST_SNAPSHOT_TTL_S` (30 s) per (map, version). This is the same
  pattern as `GraphCache` meta TTL (`scene_route.py:385-404`). The planner already has Postgres (`server.py:111`). The loader
  exists: `region_stats_load.aload` (`region_stats_load.py:78-82`). It is called twice, once for the class and once for
  `'*'`.
* **Aging [user, Q6].** Today: a 90-day window plus the newest 50 samples per (chart, class) (`config.py:689-691`). That is an
  implicit recency weighting. Decay cannot be done in the planner from stored aggregates, which contradicts spec section 6.3
  ("day decay ... the planner's job"). If decay is wanted, the **builder** does it (weighted sums, `algo_version` bump).
  Recommendation: keep the window for v1.
* **Direction:** the stats are undirected. A ramp or a door that differs by direction is averaged. Heading bins are a later
  `algo_version` bump (spec section 10).
* **Unknown `speed_max`:** the term is off for that robot and the record says `no_speed_max` [default]. The two other robots
  report `speed_max` 1.0 with an unknown footprint, which looks like a placeholder; see Q5.

## 5. Search correctness

### 5.1 The edge cost [firm]

```
Ln = weights.edge_costs(Lm, dsum, rate)                    # today, unchanged (lazy_plan.py:545)
   + ceil(Lm / ROUTE_UNIT_M * (rho[ch[R]] + rho[ch[C]]) / 2000 - 1e-6)   # NEW, rho in per mille, 0 when off
```

* **Why additive, not multiplicative:**
  * Multiplying `(1 + rate * d)` by a region factor would scale the clearance penalty by the chart's pace. Kappa's meaning
    would then change per chart and need re-tuning.
  * The additive form leaves today's J exactly unchanged when `rho` = 0, so `region=None` stays bit-identical: the existing
    equality tests (`test_lazy_plan_equality.py`) and the turn-cost tests keep passing untouched.
* **Where it lives:** the sum sits in `lazy_plan._search` (a LACI file, `lazy_plan.py:37`), not in `weights.edge_costs`.
  `packages/scene_graph` (`weights.py`, `api.py`, `engine.py`) is CIMBI's contract. Moving it into `weights.py` later is a
  coordination item, not a prerequisite.
* **Quantisation:** a lattice edge is 1000 or 1414 units. With a 1/1000 quantum on `rho`, the rounding error is below 0.1 %
  of the edge, and the costs are exact integers, so the canonical tie-break (`lazy_plan.py:576-592`) is unchanged.
* **Engine:** `ChartGraph.route` gets no region term. The equality tests run with `region=None`. The region case is tested
  against a brute-force Dijkstra on small fixtures.

### 5.2 Bounds and admissibility [firm]

* **Every bound rests on one fact:** every edge costs at least its length in units.
  * The ellipse after the corridor (`lazy_plan.py:630-631`, `bound = J_corridor * 1e-4`).
  * The Dijkstra stop `jlimit`.
  * The turn search's `restricted` ellipse (`:677-681`).
  * The tile filter `_within` (`:456-467`).

  The additive term is non-negative, so all of them hold.
* **Tighter bound (optional):** every edge costs at least `(1 + rho_min) * length`, so `L <= J * 1e-4 / (1 + rho_min)`.
  Pass `rho_min` and divide in those two places.
* **Ellipse growth:** with `rho_cap` = 3, a route through slow charts gives an ellipse up to 4x longer. More tiles means
  more `CorridorTooLarge`, which returns the valid incumbent with `exact=False` (`:636-637`). The cap and `rho_min` keep
  this in check. On today's maps it does not matter (about 2 ms per plan).
* **Turn search:** it uses `Ln` as the edge weight (`:555`), so the region term flows into it unchanged.

### 5.3 Everything else that prices a path must use the same term [firm]

| Place | Change |
|---|---|
| `route()` | The `safety=None` shortcut (`lazy_plan.py:602-603`) and the refinement condition (`:622`) must treat `region` as a price. |
| Stage 1 `_path` (`:363-370`) | Optionally weight link length by `1 + mean rho` of the two charts: a better seed and a tighter bound. Not needed for optimality. |
| `chord_cost` / `priced_visible` (`scene_route.py:871-938`) | Add `rho` of the sample's chart to the factor. Turn `priced_visible` on also when only `region` is active (`:1126-1127`). Otherwise the string pull may cut across a slow chart that the lattice path avoided. This needs `PlanGraph.chart_index_cell(ix, iy)` (a twin of `free_cell`, `:231-236`). |
| `node_policy_plan._route_leg` (`node_policy_plan.py:58-71`), insert_planned path (`server.py:467`) | Pass `region` next to `safety`. |
| Corridor width, nodePolicy | No change. The region term does not touch `min_width`, `annotate_corridors` widths, gates or `skippable` (contract: `docs/node_policies_v2_plan.md` table, lines 18-29). The robot sees only different waypoints. |

## 6. Risk and k alternatives

**Inside the search, later (S6) [default]:** a certainty-equivalent term `+ (gamma / 2) * v_c`. Here `v_c` (s²/m) is the
per-metre variance rate of chart time under an independent-increments model. The builder would store it as
`pace_var_rate = mean_i (T_i - pbar * l_i)^2 / l_i`. That is additive and admissible, and it covers block and recovery
risk without a separate block term, because those are where the variance comes from. `region_stats` has no variance today:
`pace_p90` is per sample and cannot be summed along a route.

**k alternatives (only if needed) [default]:**

* **Where:** Stage 1. Yen's k shortest paths on the chart graph (tens of nodes, weighted by `length * (1 + mean rho)`), then
  Stage 2 restricted to `_rings(path_i, 1)` for each. This gives routes around different sides of obstacles. Stage 2
  re-penalising alone tends to return near-copies of the same route.
* **How many:** k = 3.
* **Ranking:** `score = J_i + sum of route terms`, for example `beta * P(block on route)` with
  `P = 1 - prod_c (1 - p_c)`, where `p_c` is Beta-shrunk `blocks / runs`.
* **Deterministic tie-break:** (score, J, length, tuple of chart keys).

Do not build this yet. With cycle rank 2 there are at most a few homotopy classes, and the in-search term already picks
between them. Blocks are rare (181 events in total), so `p_c` would be all prior. Revisit when a map with loops (Hospital)
has about 50 runs.

## 7. Observability and evaluation

**Plan record (always, also in shadow).** `ChartPlan.pricing["region"]` (`scene_route.py:224-227`) holds:

* `mode` (off | shadow | on) and `status` (ok | no_build_for_version | no_speed_max | no_stats).
* `model_hash`, `terms`, `stamp`, `graph_version`, `robot_class`, `rho_min`, and fallback counts per level.
* `predicted_s` and `predicted_free_s`: the lattice path's length x (1 / `speed_max` + excess), plus turn time when the
  factsheet has `angular_speed_max`.
* In shadow mode: `alt_route_changed`, `alt_extra_m`, `alt_predicted_s`.

The record is logged next to the existing "Priced plan" line (`server.py:369-380`).

**Persisted:**

* `RouteTrajectoryLegV1.predicted_s` (optional, `mission.py:144-155`). The planner fills it from the leg's trajectory
  cells.
* `MissionRouteNodeV1.cost_model` (optional str, `model_hash:stamp`). Both are kept out of the route digest, like
  `graph_version` and `trajectory` (`controllers/mission/server.py:532-535`). The replan path copies them
  (`:1017-1019`).
* `leg_tracker._plan` (`leg_tracker.py:217-228`) already matches a leg to its trajectory entry (`plan_source` =
  "trajectory"). It copies `predicted_s` the same way `planned_m` is copied.
* One migration adds `run_legs.predicted_s float4` and `run_legs.cost_model text`, next to `expected_s`
  (`fleet_recorder.py:174-178`).

**Offline replay harness (S1, the decision tool):** `scripts/analysis/region_cost_replay.py`. It follows the rolling-origin
rules of `link_cost_estimation_2026-10-13.md` section 7:

* **Method:** for each run in time order, build `region_stats` from earlier runs only, using the pure
  `region_stats.aggregate` (`region_stats.py:86`) with the chart ids from one owner-raster read. Predict the run's time
  **along the cells it actually drove** (its own `run_cells`), and compare with the actual time. No counterfactual and no
  planner are needed for calibration.
* **Metrics:** MAE and bias of the per-run and per-chart-crossing time; p90 coverage; results by count bucket.
* **Baselines:** `expected_s`; plain pace x length, which won the October sketch (`link_cost_estimation` section 7).
* **Route-change replay:** re-plan the historical (start, goal) pairs on the current graph with the model off and on. Count
  route changes and the predicted difference.

**Online:** `REGION_COST_MODE = off | shadow | on`, default **off**, plus a map allowlist `REGION_COST_MAPS`.

* **Shadow:** serve today's route, compute the model's route and prediction alongside it (a few ms), and log the
  difference.
* **Go to `on` per map** when the shadow `predicted_s` vs `duration_s` beats both baselines on recent legs and the changed
  routes are not slower in hindsight.

## 8. Staged plan (sized for subagents)

| Stage | Content | Files | Tests | Depends on |
|---|---|---|---|---|
| S0 | Prerequisites: decide Q2 and P3, deploy and backfill (user), P2 agreement test and version pin in `regions_of` | `api/drive_stats.py`, `tests/unit/test_region_stats.py`, ops | chart equality on a fixture; multi-chunk version pin | user go for deploy |
| S1 | Replay harness, offline, read-only; in-memory `run_cells` from live rows until deployed | `scripts/analysis/region_cost_replay.py` | a small fixture run | none (start now) |
| S2 | `packages/utils/region_cost.py`: `Term`, `CostModel`, `build_snapshot`, `RegionPricing` (pure) | new + config knobs | shrinkage values, fallback levels, cap, quantum, hash stability, unknown `speed_max`, version mismatch | none |
| S3 | Planner plumbing behind `REGION_COST_MODE=off`: `route(..., region=)`, `_search` term, refinement condition, `rho_min` bound, `chord_cost` / `priced_visible`, nodePolicy legs, snapshot cache in `server.py` | `lazy_plan.py`, `scene_route.py`, `node_policy_plan.py`, `server.py` | `region=None` bit-identical (existing suites); brute-force Dijkstra optimum with random `rho`; ellipse-bound property test; determinism; turn search with `rho` | S2 |
| S4 | Observability: pricing record, `predicted_s` on trajectory legs, `cost_model` on the route node (digest-excluded), migration `run_legs.predicted_s` / `cost_model`, leg tracker copy, shadow mode | `scene_route.py`, `server.py`, `cloud_common/objects/mission.py`, `leg_tracker.py`, `fleet_recorder.py`, migration, `test_navstack_deploy*` head pin | digest unchanged, round trip, shadow does not change the served route | S3 |
| S5 | Turn on per map (user call) after shadow evidence | config only | none | S1, S4, P1-P5 |
| S6 | Later: `dwell_s` (P3, `algo_version` 2), `pace_var_rate` column and CE term, optional k alternatives | `cell_raster.py`, `run_cells.py`, `region_stats.py`, `region_cost.py` | per stage | data |

**Risks:**

* **Endpoint dwell** inflates charts near goals (P3). Mitigation: v1 uses `moving` only.
* **Selection bias:** only the routes the planner chose get data. Mitigated by the map-wide prior; real exploration is out
  of scope.
* **Class drift** with the backfill (P4).
* **Small maps hide value:** expect few route changes. The ETA gain comes first.
* **Overlap with clearance:** observed slowness near walls is partly what kappa already prices. With the additive form,
  kappa can be re-tuned down in the replay if needed.
* **Ownership:** keep the changes outside `packages/scene_graph`, or agree them with CIMBI first.

## 9. Open questions for the user

1. **Objective [user].** Is the planner's objective "predicted time, plus clearance as a separate safety preference"? That
   is this design (additive). The alternative is folding clearance into time.
2. **Robot class key [user, decide before backfill].** Keep `speed_max` in `robot_class`, or key by (`agv_class`,
   footprint) and normalise pace by `speed_max`? The second pools history across speed changes. Also: label runs by their
   own factsheet (needs factsheet history), or by the current one?
3. **Endpoint dwell [user].** Approve `algo_version` 2 with `dwell_s` near start, goal and action nodes (radius R?). Until
   then the stop term stays off.
4. **Risk appetite [user].** Risk-neutral (expected time) for v1, with a CE variance term later? Or must a time-critical
   mission type prefer predictable routes now?
5. **Robots without a real `speed_max` [user].** Term off (proposed), or use an observed reference speed?
6. **Aging [user].** Is the 90-day / newest-50 window enough for v1 (proposed), or do you want decay, done in the builder?
7. **First deliverable [user].** Given cycle rank 2 on all maps with data: is "calibrated predicted time in shadow"
   (S1-S4) the right first milestone, with route steering after it?
8. **CIMBI [user].** Keep the term in `lazy_plan.py` (proposed), or move the formula into `packages/scene_graph/weights.py`
   with CIMBI?

## Appendix A. Measurement commands (read-only)

* Runs per map: `SELECT map_id, count(*) ... FROM mission_runs WHERE ended_at IS NOT NULL GROUP BY map_id` (live DB,
  `docker exec docker_compose-postgres-1 psql`).
* Chart coverage: `GET /api/v1/maps/{office9,office7}/drive-stats?days=90` (deployed raw path, `regions[].n`).
* Graph size and cycle rank: `GET /api/v1/maps/{map}/scene-graph?bbox=-90,-90,90,90`, then union-find over `links`.
* Plan time: `lazy_plan.open_plan(store, map)` in the planner container, with `route(a, b)` and
  `route(a, b, safety=safety_for(g, 0.5, 0.5, 1.0))` on 30 seeded random chart pairs. The script was removed afterwards.
* Leg predictor: `percentile_cont(0.5)` of `duration_s / expected_s` on `run_legs` where `map_id = 'office9'`.

## 10. Decisions (user, 2026-10-27)

1. Objective: predicted time; clearance stays a separate safety preference (additive design).
2. Robot class key: (`agv_class`, footprint) without `speed_max`; pace is normalised by the run's own `speed_max`.
   Runs are labelled with the factsheet that was current when they ran, where that history exists; otherwise the
   current one, recorded as such.
3. Endpoint dwell: `algo_version` 2 records waiting within 0.5 m of start, goal and action nodes as `dwell_s`,
   separate from transit stops. The stop term stays off until shadow evidence.
4. Risk-neutral expected time for v1; variance penalty later (S6).
5. Robots without a real `speed_max` (placeholder 1.0 and no footprint): region term off.
6. Keep the 90-day / newest-50 window; no decay for now.
7. First milestone: calibrated predicted time in shadow mode (S1-S4); route steering only after that.
8. The cost-term formula lives in `packages/scene_graph/weights.py` next to `edge_costs` (small pure function;
   tell CIMBI). Region pricing data handling stays outside `packages/scene_graph`.
