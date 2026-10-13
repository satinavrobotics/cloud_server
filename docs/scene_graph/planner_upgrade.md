# Planner upgrade: read-only, tile-lazy chart planner

Status: **Phase 0 implemented** on `feat/scene-graph-lazy-planner` (section 0 below); phases 1-3 are plan only. Written 2026-10-13 from a read-only scoping pass over branch
`feat/scene-graph-v2`. File:line citations are as of that date. Numbers marked *est.* are estimates, not
measurements. Related: `integration.md` (planner wiring, measurements), `runbook.md`, `handoff_cimbi.md`.

## 0. Phase 0 result (2026-10-13)

`packages/services/mission_planner/lazy_plan.py` (`PlanGraph`, `open_plan`), selected by
`SCENE_GRAPH_PLANNER_MODE=lazy` (default `full_load`; `SCENE_GRAPH_LAZY_MAX_TILES`, `SCENE_GRAPH_LAZY_TILE_CACHE`).
No format change, no engine change; it reads `StoredView` (`_own`, `_cache`) and the manifest, so Phase 1 only moves
that code next to the engine.

* **Correctness** (`tests/unit/test_lazy_plan.py`, 5 worlds incl. a 24 x 18 m room grid): anchor-to-anchor and
  point-to-point routes equal `store.load(...).route` in length, charts and cells; with a blocked disc the length is
  equal (route cells may differ among EQUAL-length routes: scipy's tie-break over a subset graph); `free_cell` equals
  the snap test; `plan_on_graph` gives the same plan; a corridor over the cap raises `CorridorTooLarge`.
* **Engine order matters**: chart indices must follow `manifest["order"]` (engine order), not the id-sorted charts:
  Dijkstra ties break by index (3 of 64 chart paths differed on the ring world before).
* **Overlay**: the engine searches every chart; the lazy reader searches the charts whose anchors lie in the ellipse
  `|a-p|+|p-b| <= L + 2 (0.4 sight + h)` for the corridor's length L, which holds every route of length <= L
  (exact, not a heuristic). Costly when the corridor is badly blocked (150 m world: 309 tiles, 0.8 s).
* **min_width** equals the engine where the chart pair has a link; a diagonal-only contact reads the charts' anchor
  clearance (needs the clearance plane, Phase 3).
* **Known store delta**: `StoredView.snap` and `ChartGraph.snap` can pick different cells at equal lattice-index
  distance (argmin vs EDT index); the real distance then differs by up to 1 cell. CIMBI-owned; not gating.
* **Bench** (`tools/mission_planner_bench`, rooms world, this host):

| World | store.load | open_plan | route cold / warm (lazy) | route (full) | plan_on_graph lazy / full | graph |
|---|---|---|---|---|---|---|
| 60 x 60 m | 1.9 s | 10 ms | 30 / 23 ms | 21 ms | 63 / 48 ms | 3.8 MB |
| 150 x 150 m | 12.6 s | 69 ms | 83 / 66 ms | 371 ms | 222 / 450 ms | 17 MB |

  The 17 MB at 150 m is the manifest (charts and links as Python objects) plus the tile LRU; it grows with charts,
  not with area covered by a request. The remaining O(map) cost is `store.manifest` (`_expand` of every page).

### Planner-side hardening (branch `wip/lazy-planner-side`)

* **Single-flight tile fetch**: `PlanGraph._read` replaces the one serialising lock by per-tile in-flight
  de-duplication (a leader reads, waiters wait on its event); the view's `_own` is pointed at it, so `snap` and the
  windows share the flight. Different tiles are read in parallel, the same tile once; an exception reaches every
  waiter as `TileUnavailable` and is not cached. Test: counting backend + thread pool.
* **Bounded overlay search**: the corridor's route under the overlay (length L) is in the ellipse's chart set, so L
  bounds the optimum: the second search runs with scipy `limit=L`, drops tiles, cells and plane cells that lie outside
  the cell ellipse `|c-a|+|c-b| <= L` (a lattice step is at least the Euclidean one; the plane keeps one extra step for
  the diagonals' orthogonals). Same lengths, same cells (equality tests unchanged). Bench 100 m world, blocking disc
  on the middle of a corner-to-corner route: 380-420 ms -> 270-300 ms (cold tiles), 210 -> 153 tiles.
* **Not adopted: skipping the ellipse when no blocked element touches the corridor route.** The engine searches the
  WHOLE graph under any non-empty overlay, which can be shorter than the corridor route even when the overlay is far
  away. Counterexample, `_grid_world` (4 x 3 rooms), `s017:0 -> s242:1`: no overlay 15.797 m (corridor), a disc
  at (-500, -500) r 0.1 15.358 m (whole graph). Pinned by `test_far_overlay_keeps_the_whole_graph_answer`.
* **`tile_unavailable`**: a tile read that fails (a gc'd version: `FileNotFoundError`, or a backend error) raises
  `TileUnavailable`, flags `PlanGraph.unavailable`; `plan_on_graph` turns it into `ChartFallback("tile_unavailable")`
  and `GraphCache.get` evicts flagged graphs, so the next request re-opens the current version.

Open for Phase 1/2: manifest expand, per-request tile fetch cost against MinIO (the memory backend hides it).

## 1. Problem

`MissingTiles`, `corridor()` and the tiled store exist, but the planner does not use them.
`GraphCache` calls `SceneGraphStore.load(map, version)` (`scene_route.py:207`), which:

* reads every tile and every leaf segment, allocates the full canvas;
* restores the full **writer** engine (`ChartGraph.import_state`, `engine.py:917-956`): erosion over 23 headings,
  EDT, `Track` (`img_rows`, `tried`), `_solve`, `_evaluate`, `_links`, `_signatures`, caches; `T`/`B` int64 planes;
* `store.load(tiles=...)` is not a partial load: it only checks the set is a superset of the manifest tiles
  (`store.py:468-475`).

Measured with `tools/mission_planner_bench` (synthetic, fully known rooms world, tracemalloc):

| Area | Load | Memory |
|---|---|---|
| 30 x 30 m | 2.8 s | ~205 MB |
| 60 x 60 m | 11.6 s | ~920 MB |
| 90 x 90 m | 28.5 s | ~2 GB (extrapolated) |

About 0.25 MB/m^2 and 3 ms/m^2: **~250 GB and ~50 min per km^2.** A breakdown of the 30 m case shows the weight is
writer state (`_track.img_rows`, `_solved.edges`, `_dcache`, int64 `_T`/`_B`), which the planner never reads.
`SCENE_GRAPH_CACHE_VERSIONS=4` (one global LRU) cannot serve multi-km^2 maps or tens of maps, and every publish
pins a second copy while the new version loads. Caveat: dense synthetic world; real maps with unknown space will
be lower, but not by orders of magnitude.

The legacy planner held nothing in-process (ArangoDB `shortest_path` / `k_nearest_neighbors`).

## 2. Corrections to earlier assumptions

* **`routing.Router` is v1 and not on the v2 path** (`routing.py:18-30`; only `test_worlds.py:126` uses it;
  `integration.md:61` lists it as dropped, `handoff_cimbi.md:22`: do not build on v1 internals). There are no
  portal costs, per-region matrices or `per_seam` in v2. "Stage 1" in v2 is `ChartGraph._corridor_charts`
  (`engine.py:779-788`): Dijkstra over the chart graph (anchors + `Link.length`), the path plus all 1-hop
  neighbours.
* **`route()` never raises `MissingTiles` today.** `api.py:178-181` documents it, but it is raised only in
  `store.load(tiles=)` and `StoredView._need` (`store.py:698-703`).
* `ChartGraph.corridor()` (`engine.py:790-798`) ignores its `blocked` argument and needs the whole in-RAM plane.
* `_chart_graph` / `chart_levels.hierarchy` build dense k x k float64 matrices (`engine.py:771-777`): ~3 GB at
  20k charts. A reader must use `scipy.sparse` and `dijkstra(limit=)`.

## 3. Target design

Per request, work scales with the corridor, not the map:

1. **Open** the pinned version: meta + manifest + chart-graph pages (charts, links). No cells.
2. **Snap** start and goal (loads at most 4 tiles; `StoredView.snap`, `store.py:523/797`, already exists and is
   pinned to the engine by `tests/unit/scene_graph/test_store.py:356`).
3. **Stage 1** on the sparse chart graph: shortest chart path; `keep` = path charts + `hops` rings
   (hops=1 = current engine margin). Optional chart-level overlay pruning: drop links whose persisted portal xy
   lies in a blocked disc/segment, recompute the chart path.
4. **Corridor tiles** = union of per-chart tile lists for `keep`; **fetch** them (parallel, tile LRU).
5. **Stage 2**: assemble a dense lattice window over the loaded tiles (`own` plane -> chart index, mask by
   `keep`), build edges with the engine's `_lattice_edges` rule (diagonals need both orthogonal cells in F, cost
   `hypot * 0.1`), CSR, `dijkstra`. Blocked overlay masks node centres in the window. Same corridor search as
   `ChartGraph.route` (`engine.py:834-836`), so it is exact within the corridor.
6. `simplify` unchanged; LOS uses `free_cells` (below). `build_waypoints` unchanged (leaf lookup still ArangoDB).

### Minimal data the reader needs (everything exists in the store except NEW)

| Item | Resolution / dtype | Source | Used for |
|---|---|---|---|
| Owner plane `own` | lattice 0.1 m, 64x64/tile, i32 key id (0 = none), includes dependents | tile blob `own` (`store.py:100-105`) | F / `s.full`, edges, snap, LOS |
| Solid mask | 0.05 m, 128x128/tile, from `v` (u8; 1 bit enough) | tile blob `v` | `crosses_blocked` (`v>=100 & v!=255`) |
| Charts | key, id, anchor xy/cell, clearance | manifest pages `charts` (`store.py:272-277`) | anchors, `2*clr[src]` |
| Links | a, b, length, width, portal (u, v xy) | manifest pages `links` | chart graph, min_width |
| Chart -> tile set (+ bbox) | small int lists | **NEW** | corridor tiles |
| Per-cell clearance | lattice, u8/f16 capped ~2 m | **NEW** (derivable from `v` + halo) | diagonal-contact min_width only |
| canvas, params | scalars | manifest | frame offsets |

`own` alone reproduces the engine's `R, C, Ln` exactly: unanchored nav components are unreachable, and a diagonal
step's orthogonal cells are 4-adjacent to an F cell, hence also in `plane`. Anchor lattice cell is
`floor(anchor_x / RES) // 2`; use an epsilon against float rounding.

## 4. API proposal

```
# store.py (CIMBI)
store.open_plan(map_id, version=None) -> PlanGraph     # meta + manifest + chart graph; no cells
class PlanGraph:                                        # read-only; bound to (map, epoch, generation, manifest)
    version: int; params: SceneParams; keys: list[Key]
    def snap(x, y, max_dist=None) -> Snap               # same semantics as ChartGraph.snap
    def corridor(a, b, blocked=None, hops=1) -> list[(tx, ty)] | None
    def route(a, b, blocked=None, tiles=None, hops=1) -> Route | None   # MissingTiles(missing) if a corridor tile is absent
    def free_cells(ix, iy) -> ndarray[bool]             # LOS: plane >= 0; unloaded tile -> False
    def prefetch(tiles) -> None
```

`PlanGraph` is the natural extension of `StoredView`, not a new concept. It must live next to the engine so the
lattice-edge rule, tie-breaks and cost have one definition.

**Failure / widening policy (LACI):** hops=1; if `None`, or length > stretch x estimate, or the overlay hits the
route: hops=2, 3; then bbox expansion; hard cap on tiles per request (e.g. 400) -> fallback `corridor_too_large`
(legacy). Needed because with a blocked overlay the engine unconditionally searches the whole graph too
(`engine.py:840-847`); a lazy reader cannot, so a detour may leave the corridor. An unloaded tile in LOS is "not
free": safe, because `simplify` always accepts the next lattice point (`scene_route.py:57`); worst case is a
less-shortened path.

## 5. Caching

* **Chart graph** per `(map, epoch, generation, manifest)` in columnar numpy (keys, anchors, link arrays,
  key_ids, tile-hash lookup via page index). Keep current + previous per map, LRU across maps by bytes.
  *est.* ~7 MB/map at 30k charts (the current `_expand` dicts are 5-10x that and expand every page).
* **Tile LRU** keyed `(map, generation, plan-tile hash)`, byte budget (LACI config, e.g. 256 MB). Content hashes
  mean unchanged tiles are shared across versions. *est.* 25-40 KB per tile in memory.
* **Per request** *est.* tens of MB transient (CSR/dijkstra state dominates), vs 205 MB - 920 MB+ today.
* **New version:** only changed pages/tiles are fetched; in-flight requests stay pinned to their `PlanGraph`.
  The "serve the older version while the new one loads" logic (`scene_route.py:318-333`) and `load_timeout`
  become mostly unnecessary; keep the `stale_graph` lag check. New generation = cold namespace.
* **Prewarm** (`server.py:132`) becomes open + chart-graph pages.
* Key everything by `epoch` too (`store.py:411-413`): version numbers reset after drop/recreate. Tile fetches
  need single-flight per hash (model: `store.py:395-420`).

## 6. Store changes (all additive; bump `FORMAT` to 2, readers tolerate 1)

1. **Plan-tile object** `g{gen}/n/{hash}`: `own` + solid bitmask (+ optional `clr`), *est.* 1-5 KB compressed,
   referenced from the page tile table next to the tile hash. Today a fetch decompresses ~300 KB raw including
   `T`/`B`/`rnd`.
2. **Per-chart `tiles` + bbox** in page chart records (~10-30 B/chart), derivable at save time from `st.plane`.
3. **Chart graph as columnar blobs**, per page (dirty-only writes) or one blob (~0.5-2 MB/publish at 30k charts,
   *est.*).
4. **Bypass `_expand`**: pages are addressable by `(tx//8, ty//8)` via `manifest["pages"]`; fetch only the pages
   for the tiles needed. (`_expanded` caches 8 manifests total across maps, `store.py:159`.)
5. **`gc` leases**: planner does not lease; `keep_last=5` (`store.py:554-580`). A pinned read can hit
   `FileNotFoundError`; planner must catch it and re-pin, with a distinct fallback reason.

## 7. Ownership

| CIMBI (`packages/scene_graph`, `store.py`, format) | LACI (planner, integration) |
|---|---|
| plan-tile object, chart->tile sets, columnar chart graph, `FORMAT` bump | `GraphCache` opens a `PlanGraph` per request |
| `store.open_plan`, `PlanGraph` (`route/snap/corridor/free_cells`) | `blocked_free_los` takes `free_cells` instead of `graph.snap` |
| byte-budget tile cache (store-internal), `gc` leases | widening policy, new fallback reasons, config knobs |
| remove dense k x k matrices from the reader path | metrics, bench, runbook, prewarm semantics |
| equality tests vs `ChartGraph.route`; fix `api.py` `corridor`/`MissingTiles` docs | port `GraphCache` tests (`test_planner_chart_route.py:103-650`) |

Leaf lookup (`node_of`, `server.py:215-223`, ArangoDB `k_nearest_neighbors`) stays a per-waypoint dependency.
Per the integration split: no commit and no message to CIMBI without the user's go-ahead.

## 8. Correctness risks

1. **Route exactness.** Stage 2 equals the engine's corridor search; differences come from the engine's
   whole-graph fallback under an overlay, and from the corridor being a poor guide (anchor-hub stretch up to
   1.58, `integration.md:22`). Gate: <= 1.05x of the full-lattice search (`integration.md:80`). A sparse
   `_corridor_charts` must be tested bit-for-bit against the dense one (zero-as-no-edge handling).
2. **Snap across tile edges / tie-breaks.** `StoredView.snap` windows ~51 lattice cells (<= 4 tiles, ring
   doubling). Known deltas: `np.argmin` vs scipy EDT tie-break; out-of-canvas returns `far` in the engine but
   searches the extent when stored; engine canvas has `MARGIN` padding after `load`. Needs randomized
   equivalence on larger worlds with tile-straddling points.
3. **LOS.** Unloaded or empty tile -> not free. Verify `snap(...).distance <= 0.5h` equals `plane >= 0` at exact
   `(i + 0.5) h` centres in float64.
4. **Overlay.** A disc centred outside the corridor but overlapping it must still apply (mask by position, not
   tile membership). `alive[src]`/`alive[dst]` False -> None in both (`engine.py:826`).
5. **min_width.** Needs `clr` for diagonal-only contacts and nearest-link matching by cell position (inverse of
   `_cell_xy`). The planner already treats it as defective (`server.py:226`, `WIDTH_CHECK_ENABLED` off): keep
   non-gating in phase 1.
6. **Version consistency.** Chart graph and tiles from the same manifest; tile hashes only from the pinned
   manifest's pages, never "current tile at (tx, ty)"; key by `epoch`; handle gc `FileNotFoundError`.
7. **`crosses_blocked`** predicate must be identical (`v >= 100 & v != 255`, 0.025 m step); empty tile = unknown.
8. **Dense matrices** in `_chart_graph`/`hierarchy` must be sparse before multi-km^2 maps.

## 9. Phasing

* **Phase 0 (LACI only, no format change):** `PlanGraph` shim over `store.view(...)`: snap = `StoredView.snap`;
  route = window from `StoredView._window` + sparse chart-graph corridor; corridor tiles from a bbox-expanded
  read. Validates correctness and the estimates against the bench.
* **Phase 1 (CIMBI), trimmed after review:** (1) a per-page expansion cache keyed by page hash (a new version
  re-reads only the changed pages, not every `_expand`); (2) a public read API next to the engine, in ENGINE chart
  order (replaces `_own` / `_cache` / `_idx` / `manifest["order"]` poking); (3) single-flight tile fetch, a byte-budget
  tile cache and leases so gc cannot remove a version a request is reading; (4) a cached id -> index table; (5) optional
  columnar pages. Dropped: stored chart -> tile sets (derivable from the chart cells and the reach, `_tiles_for`),
  separate plan tiles, `open_plan`/`PlanGraph` as new store types (they stay in the planner).
* **Phase 2 (LACI):** `GraphCache` open-per-request, `free_cells` LOS, widening policy, fallback reasons
  (`corridor_too_large`, `tile_unavailable`: done on the planner side), config, runbook, prewarm.
* **Phase 3:** portal-node or hierarchy-level stage 1 for multi-km^2; clearance plane for `min_width`; replace
  `SCENE_GRAPH_CACHE_VERSIONS` by a byte budget.
* Keep `SCENE_GRAPH_PLANNER_ENABLED`; add a `full_load | lazy` mode flag so rollback is config-only. Keep the
  full-load path until equality is proven.

## 10. Tests and acceptance

* Extend `test_store.py` view/snap equality (`:325-352`, `:356`) to larger worlds and straddling points.
* New: `PlanGraph.route(...).length` vs `store.load(...).route(...).length` on random pairs over
  `tests/unit/scene_graph/worlds.py` / `chart_worlds.py`, with and without overlay; also `charts`, `min_width`
  where defined, node count.
* `test_scene_route.py`: `free_cells` LOS equals the `snap`-based LOS.
* Integration: tile fetch against real MinIO; concurrent publish + plan; gc during a pinned read.
* Bench: lazy mode reporting RSS, tiles and bytes fetched, per-stage ms (open, snap, corridor, fetch, route,
  simplify) at 30/60/90 m and a multi-km^2 synthetic built by tiling saved sub-worlds or writing tiles directly
  (open 12-15 m rooms make the fold slow).

Acceptance (proposed):

* warm plan p95 within the 3 s budget (`SCENE_GRAPH_PLAN_BUDGET_S`), target well under 1 s for 200 m routes on
  1 km^2;
* cold open <= 2 s, independent of map area (28.5 s at 90 x 90 m today);
* RSS per map bounded by tile LRU budget + chart graph; 1 km^2 x 10 maps < 1 GB total;
* route length ratio vs full load <= 1.05 (mean ~1.00); zero cases where lazy says unreachable and full finds a
  route (those must fall back);
* unchanged-tile hit rate after a publish > 90%.

## 11. Open questions (ranked)

1. Is `min_width` in scope for the lazy path (clearance plane), or non-gating until the engine defect is fixed?
2. Separate plan-tile object vs reuse of the existing tile blob (format bump vs ~300 KB raw per fetch).
   Measure compressed sizes on a real map first.
3. Stage 1 quality: anchor-hub (stretch up to 1.58) vs portal-node stage 1 from persisted `Link.portal` (bounds
   `|p-q| <= d <= |p-a|+|a-q|`, corridor choice only) vs persisted intra-chart distances (manifest size cost).
4. Chart graph at multi-km^2: one columnar blob vs per page; whether stage 1 needs a hierarchy level (not stored,
   dense today). Need real chart counts per km^2 (30k/km^2 here is a guess).
5. Where the tile cache lives and who owns the byte budget; shared with the API's tile views (`runbook.md:247`).
6. Widening policy values: hops, tile cap, when to fall back to legacy.
7. gc leases vs re-pin on `FileNotFoundError` (`keep_last=5` is enough only if publishes are slow vs request
   time; publish floor 5 s).
8. Snap tie-break rule and out-of-canvas behaviour to standardize.
9. Keep or drop stale-version serving (open is cheap).
10. Leaf lookup: keep ArangoDB `k_nearest_neighbors` or use `Chart.leaf` from the manifest. See also section 13
    (leaf snapping and edge statistics, marked for revision).

## 12. Not verified

* Real-map chart density, compressed tile sizes, any per-request latency/RSS of the lazy path (all *est.*).
* Bit-for-bit tie handling of a sparse `_corridor_charts`.
* `ArangoMinioBackend.get` per-object latency (`store_backends.py:96`) and allowed fetch parallelism.
* `own` dtype: i32 after `ids_lut`; u16 breaks if ids exceed 65535 (`next_id` is never reused).

## 13. Needs revision soon: leaf-id snapping and edge statistics

Decision (2026-10-13): **keep as is for now**, but both items below are marked for revision soon.

* **Leaf-id snapping (step 6 / `build_waypoints`).** Every waypoint keeps its exact `x, y, theta`; the nearest
  leaf id (`graph_db.k_nearest_neighbors`, `SCENE_GRAPH_LEAF_SNAP_M`) is only a label for consumers keyed by
  topomap node id (`planned_path`, `mission_runs.planned_path`, leg tracker, edge statistics, dispatcher
  blocked-node resolve). The planner falls back to legacy when a waypoint has no leaf or two share one
  (`waypoints_without_distinct_leaf_ids`); only `planned_path` needs that strictness, the route does not. On large
  open maps this throws away good geometric routes. Options when revisited: (a) return the route with
  `planned_path=None` / partial and `node_id=None` (dispatcher already falls back to `@x,y`); (b) key the
  statistics by chart instead of leaf.
* **Edge statistics should move to the high-level graph.** Today they are per low-level (leaf / topomap) edge,
  paired through `waypoints[i]` <-> `planned_path[i]`. In the revision they should be attributed to the
  high-level graph (charts and links: `chart_keys` is already in the plan response, `Link` carries length and
  width) rather than leaf nodes. That removes the need for distinct leaf ids per waypoint and the legacy
  fallback above. Touches the run analysis and leg tracker, not just the planner; design it with the
  statistics owner, and note that chart keys change across graph versions (use lineage, `Dirty.retired`).

## Critical files

`packages/scene_graph/store.py`, `engine.py`, `api.py`;
`packages/services/mission_planner/scene_route.py`, `server.py`;
`tools/mission_planner_bench/bench_plan_on_graph.py`.
