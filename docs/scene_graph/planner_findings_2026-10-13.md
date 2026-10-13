# Planner upgrade: findings of the 2026-10-13 session (LACI)

The current record of the planner-upgrade work: status, what was measured or reviewed (and how), what was agreed
(section 10) and what is open. Read with `planner_upgrade.md` (plan, Phase 0 result) and `handoff_next_session.md`
(steps). "Verified" = run or read in code; "estimate" = not measured.

## 1. State of the branches

| What | Where | State |
|---|---|---|
| Lazy planner (Phase 0 + hardening) | `lazy_plan.py`, `scene_route.py`, first behind `SCENE_GRAPH_PLANNER_MODE=lazy` (default `full_load` at the time; `full_load` existed until its removal on 2026-10-13, lazy is now the only mode, see section 10) | merged into v2 as `09a4e99`; v2 pushed by CIMBI as `origin/feat/scene-graph-v2` @485f39d (includes the merge) |
| Store read-path hardening (page cache, cached lut, single-flight tiles, byte-budget cache, leases) | `packages/scene_graph/store.py` | taken by CIMBI as `16aee6a` (cherry-pick of `61e7ca3`, authorship kept) |
| e2e `GraphCache(lazy=...)` from `SCENE_GRAPH_PLANNER_MODE` (the switch is removed) | `tests/integration/scene_graph/test_scene_graph_e2e.py` | committed `88a360d`; e2e 32 passed in `full_load` and `lazy` (before the v2 merge); CIMBI re-ran e2e on `16aee6a`: 32 passed |
| Tests after the v2 merge | targeted (lazy planner, scene_graph, planner route, reads) | 339 passed, 3 skipped (MinIO-only), on the v2 merge of 2026-10-13; not re-run since |

Planner flag stays OFF; no database generation was started.

## 2. Lazy planner: what it is

`PlanGraph` answers like `store.load(...).route` from stored tiles: same snap, route cells, length, chart sequence
(`tests/unit/test_lazy_plan.py`, 5 worlds). Reach of a chart cell from its anchor is `2 x chart_radius`
(`_reach()`); under an overlay the engine searches every chart and the reader searches the anchors inside the
ellipse `|a-p|+|p-b| <= L + 2 (reach + h)`. Ties among equal-length routes differed under an overlay at the time of writing (scipy order); the canonical tie rule (section 10) removed that, routes are now identical.
Skipping the overlay ellipse would break engine equality: not adopted.

## 3. Q1: delivery into `fold_many` is at-least-once (code trace, not run)

- Source: polled `costmap_stored_ms` cursor, 60 s scan margin (`scene_worker.py:678,790`). Leaf key = node `_key`;
  stamp frozen on the node, first write wins (`graph_db/server.py:727`).
- Re-delivered although folded: restart/`_engine_fault` (cursor - 60 s, skip off, `_reset_fold_state`), pending-PNG
  hold-back (`_durable` pins the saved cursor, later leaves come again), rebuild resume/fault, backfill
  (`set_node_costmap(s)` rewrites `costmap_stored_ms`: same key and stamp, newer stored_ms).
- Atomic: save = cursor + state in one CAS (`store.py` meta CAS), so crash windows around it do not double.
- `fold_many` has no per-key dedup; planes are idempotent only by the per-cell max-time rule (`engine.py:198`).
  `_leaves[key]` is replaced (`_leaf_put`). The hook exists: `old_rec = self._leaves.get(lf.key)` (`engine.py:347`).
- Required for additive counts (CIMBI accepted, `formulation.md` section 6): dedup on (key, counted stamp) in the
  engine, persist the counted stamp in the leaf record, counts rebuild on change.
- Not verified: upstream duplicate node creation in graph-builder ingest; two concurrent rebuilders; stale
  `cursor_store` on map recreation in one process.

## 4. Q2: pricing inside `route()` (design review, nothing run)

Accepted by CIMBI with these conditions: counts in a separate versioned object outside tile and manifest hashes;
integer weights >= 1, capped (about 4, p <= 0.75), ceil, unit 1e-4 m; explicit quantised `now` (`None` = today's
behaviour); overlay stays hard; `Route.length` geometric plus `Route.cost`; whole-graph exact search with an `exact`
flag, corridor only as seed (priced cost of the corridor route bounds the ellipse; expect more `CorridorTooLarge`);
cost-only tie contract; `api.py:9` amended when it lands. Later format step; does not block generation.
Risk: overlapping leaves are correlated, so `p` is overconfident.

## 5. Arango read model

**What:** the chart graph (charts = vertices, links = weighted edges) held in ArangoDB as a rebuildable index of the
MinIO snapshot; it would replace only the chart-level stage 1 (`PlanGraph` path + rings) and could serve the read
API's chart/link queries. Cell-level search and tiles stay on MinIO either way.

**Benchmark** (ArangoDB 3.12.4-3, synthetic planar grid, container removed; scripts and `res_*.json` in the
scratchpad `bench_arango/`, not in the repo):

| charts | in-process local / random p50 | AQL exact local / random p50 | concurrency 8 workers (AQL / in-process threads) |
|---|---|---|---|
| 2k | 0.39 / 0.43 ms | 3.7 / 13 ms | 842 / 1677 qps |
| 20k | 3.0 / 3.4 ms | 4.9 / 134 ms | 879 / 218 qps |
| 100k | 22.9 / 22.7 ms | 4.6 / 605 ms | 853 / 38 qps |

Open cost saved at 100k about 1.5-2.5 s and 5.6 MB / 400 pages (MinIO GET latency not measured). Write side at 100k:
full rewrite 54 MB / 2.9 s; interval model at 2% change 0.4 s / 1.7 MB.

**Pitfalls (verified in the benchmark):** built-in `SHORTEST_PATH`/`K_SHORTEST_PATHS` are NOT exact (1.5-3% longer);
use `FOR v,e,p IN 1..2000 ANY @from edges OPTIONS {order:'weighted', weightAttribute:'length', uniqueVertices:'global'}
FILTER v._id==@to LIMIT 1`; `uniqueVertices:'path'` is exponential; interval versions need
`p.edges[*].from_v ALL <= @v AND p.edges[*].to_v ALL > @v` with sentinel end 1e6 (a wrong filter gave 43% wrong
routes); with quantized lengths 55% of chart sequences differed between AQL and scipy (length never did): needs the
canonical rule lexicographic min of (integer length, chart-key sequence) in the engine.

**Real maps are tiny:** 5-16 charts each (`handoff_laci_morse.md`: office8 12, office5 5). Both approaches are
equivalent there; the scale question (about 30k charts per km^2 is a guess) is a future option, not a defect.

**Verdict on the earlier "defer" reasons:** whole-map AQL latency is real but only at >= 20k charts; pricing does not
favour either side (AQL traversal is already whole-graph exact; the ellipse is a stage-2 issue both share); the
"second store" already exists in part (`sg_meta` CAS is the commit point, `put_index` lag exists); the tie-break and
interval filter are engineering items. No hard blocker. Concurrency favours Arango (but only against threads; processes
narrow it). Real win: the chart graph becomes queryable (bbox read API, hierarchy, analytics).

**Mirror scope (code read, not run), paths relative to repo root:**
- Hook: after the `sg_meta` CAS in `store.save()` (`packages/scene_graph/store.py:384`), plus `commit_generation`
  (`:738`, never calls `put_index` today: mirror needs a full old/new manifest diff). Diff charts/links against
  `prev`; do not trust `Dirty` (undercounts, heal at `:261`).
- Existing `put_index` (`store.py:389`, `store_backends.py:156`) is not a base: subset of fields, no
  `index_version < v` guard, failure only logs and is never retried, `_col()` resurrects dropped collections.
- Schema: `sg_chart_{id}`, `sg_chartrev_{id}`, `sg_edge_{id}` (`from_v`,`to_v` interval, sentinel 1e6), `sg_ver_{id}`;
  edge `_key` = hash(a, b, portal); add to the prefix lists in `store_backends.py:193` AND
  `packages/topomap_dbs/graph_db/server.py:26`. gc from `store.gc` (keep 5 versions).
- Pin: (epoch, version, generation, params_hash) checked in the same AQL as the read; `mirror_version < version`
  falls back to MinIO; versions restart after drop so the epoch is always part of the pin.
- Gap 1: charts are selected by owned raster cells inside the snapped bbox (`StoredView.query`, `store.py:876`), the
  manifest has no per-chart extent/tile list: exact parity needs one computed at save time; an anchor geo index is only
  an approximation. Gap 2: level > 0 needs the whole graph (`morse.cell_levels`): precompute per level or keep on MinIO.
  Not servable: raster, `owner_plane`, `blocked_chart_tags`, tiles.
- Flags: `SCENE_GRAPH_MIRROR_ENABLED` (off), `SCENE_GRAPH_MIRROR_READS`, `SCENE_GRAPH_MIRROR_MAX_LAG_VERSIONS`.
- Size: CIMBI-owned (`packages/scene_graph`) about 4-6 days; LACI (read path, drop list, worker heal, flags) about
  3-4 days. Tests: crash between CAS and mirror, two workers, rebuild across generations (chart keys/ids change),
  drop without resurrection, version mismatch, equivalence with `StoredView.query`, rebuild-from-MinIO equals
  incremental.

## 6. Optimizations from the design review (ranked, estimates)

1. Tile cache keyed by tile hash with a global byte-budget LRU (today per `PlanGraph`, keyed (tx, ty): every publish
   starts cold). Highest payoff, small change. *Update (measured, `xver.py`, bench rooms worlds): DONE by `16aee6a`, no gap
   left. The store tile cache is keyed `(map, "g{generation}/t/{content hash}")`, shared by all versions and views, byte
   budget 64 MB default; `PlanGraph._read` goes through `store._tile`; the per-`PlanGraph` (tx, ty) layer is a dict of
   references (no I/O, no decode). Per publish only changed tiles are fetched: 60 m 10-15 GETs (1.5-2.3 ms decode), 150 m
   5-8 GETs (0.8-1.7 ms), cold v1 47 / 111 GETs; a fresh `PlanGraph` over a warm store plans as fast as a warm one. A
   process-wide decoded-tile LRU would only duplicate the store cache. Residual: the store budget is not configurable
   from the planner (`server.py::_open_scene_store` uses the default), `PlanGraph.nbytes` double counts shared arrays
   (harmless while `SCENE_GRAPH_CACHE_MAX_BYTES` is 0); revisit with real MinIO numbers.*
2. Measured per-map or per-chart reach instead of `2 x chart_radius` (about 5.4 tiles per chart in the benchmark vs
   about 2.6 expected): about 1.5-2x fewer tiles in stage 2. *Update (measured, 7 worlds + 13
   stress worlds): actual reach is 0.24-0.5 of the 2 x chart_radius bound (max 1.06 rho in one random-obstacle world, so
   a fixed 1.0 rho is unsafe; 2 rho is the tightest bound the engine proves). No LACI-only tightening is safe: a
   per-chart `reach` must be stored (CIMBI, one float per chart record, computed at save, old versions fall back to
   2 rho; changes page hashes, so it rides with a format/pipeline bump). Saving on no-overlay routes: tiles -26..-34%,
   time -19..-21% (bench 60/150 m); overlay routes only 4-11% (the ellipse search dominates). Consumer patch (dormant
   until the field exists, exact on 120/120 routes) kept outside the repo. Do not ask for it alone: add it to the
   next format bump together with the integer-cost step.*
3. Bound the stage-1 `_path` Dijkstra (`limit` with doubling retry): 23 ms at 100k charts, matters above about 50k. *Update: prototyped
   and exact (scipy `limit`, doubling retry, whole-graph fallback, tie guard; identical chart paths on the 5 worlds and
   synthetic 50k-chart grids), NOT merged: local pairs 23 -> 0.5 ms at 100k charts but 3.3 -> 0.15 ms at 20k and no gain
   for cross-map pairs; real maps have 5-16 charts. Redo it around the integer canonical tie rule (ties become common
   with integer weights, the guard would fall back). `directed=True` on the symmetric chart graph alone is -15% at 100k.
   Diff kept outside the repo.*
4. Merge `near` and the `member` limit (two passes of the same ellipse test).

## 7. Open decisions (the user's)

1. Arango mirror: start at all? Recommended shape if yes: read-only mirror behind a flag, exact bbox parity (per-chart
   extent at save time), level > 0 left on MinIO, planner stage 1 only if real chart counts or concurrency justify it.
2. Canonical integer tie-break in the engine: the ask was SENT and AGREED by CIMBI (rule in section 10); not a draft
   any more. What is left is scheduling, which is CIMBI's.
3. Planner optimizations 1-4 above.
4. Everything outward-facing from `handoff_next_session.md` (merge to main, DB generation, rollout).

## 8. Not measured

Real MinIO GET latency and tile sizes; real mission pair distances (local vs whole-map); ellipse size under pricing on
real maps; scipy tie behaviour with integer weights; whether "blocked" counts are inflated costmap or raw.

## 9. Stale docs still to fix

`integration.md`/`runbook.md` (dependents, chart counts, `sight_range`, descent budgets, open-field limit), and the
`handoff_next_session.md` numbers (updated in this session's edit).

## 10. Updates after the session

* **Canonical route ties: agreed by CIMBI** (the ask of section 7 item 2, sent and accepted). Rule:
  - edge weight = `ceil(edge length / 1e-4 m)` x multiplier (integer >= 1; 1 until pricing);
  - among minimal-cost paths take the lexicographic minimum of the sequence of cell keys (chart key string, global
    lattice y, x), cell by cell; never chart index or manifest order (the engine index is per solve);
  - extraction: one Dijkstra from the goal, then walk from the start picking, among the neighbours, the minimal key
    with `w + d_t(v) == d_t(u)`;
  - `Route.length` stays geometric, `Route.cost` is the integer cost; no format bump, no stored state;
  - lands as a separate small CIMBI commit after the current readiness gates; does not block generation.
* **Tests:** LACI owes an equality test lazy `PlanGraph` vs `store.load(...).route` (chart sequence and cells) on the 5
  worlds of `tests/unit/test_lazy_plan.py`; CIMBI adds engine-side tests.
* **Consequence for `lazy_plan.py`:** its own tie order (engine index order from `manifest["order"]`, scipy tie
  behaviour; see `planner_upgrade.md` section 0) must follow the same key rule when the engine change lands; the
  "ties may differ under an overlay" caveat of section 2 is to be re-checked then. Done in `b997c66`: `lazy_plan.py`
  uses the integer cost and the canonical walk; routes are identical with and without an overlay.
* **Branch state:** v2 has `main` merged (`39fd715`) and `7710d7a` on top: a single alembic head
  (`20261012_01_blocked_geometry` re-parented onto `20261013_01_track_map_frame`; both had been written on
  `20261011_01`, so the merge gave two heads and `alembic upgrade head` would have failed at API start). The live DB is
  at `20261013_01`; `20261012_01` is NOT applied there. The pinned unit image was rebuilt with scipy; full pinned
  suite 4158 passed / 0 failed, per CIMBI after `7710d7a` (2026-10-13), not re-run by us.
* **Lazy vs engine equality test** (`tests/unit/test_lazy_plan_equality.py`, new, untracked at the time of writing): 9
  worlds (5 + 4 shifted so pairs straddle tile boundaries), anchor, point, straddle and overlay (disc, segment) pairs:
  no length or reachability divergence; chart sequence and cells identical without overlay; with overlay 5 xfails
  (equal-length ties: cells, once the chart sequence) and 31 xpass at the time of writing. The xfail markers were
  removed in `b997c66` with the canonical tie rule; all cases are plain assertions now. `two_rooms` has 2 charts: little route coverage.
* **Edge statistics storage (decision of the user, 2026-10-13):** statistics should ultimately be stored in ArangoDB,
  on edges. Today observations are in Postgres `edge_observations`, statistics are computed on read and filtered by the
  current Arango edge set. Design draft and consequences: `edge_stats_on_charts_design.md` (section 7) and
  `docs/tracking_redesign_2026-10-13.md` (D1: poses stored, chart pair on read). Not started.
