# Scene graph: persistence regions cut into bounded charts, over navigable free space

Status 2026-10-13 (charts added the same day). This is the definition the code implements (`packages/scene_graph/morse.py`, engine in `engine.py`) and the
statements it relies on. Every statement is marked **EXACT** (proved, with an independent numeric check), **TESTED** (measured,
n given) or **HEURISTIC** (a choice, not derived). The derivation record, with the statements that were refuted on the way, is in
`audit_ledger.md`; the reference research code and its independent verifier were `theory/morse2.py` and `theory/verify2.py`
(kept in the session scratchpad; the production verifier is `tests/unit/scene_graph/chart_verify.py`).

## 1. What it is for

A robot's costmap windows are folded into one map (`engine.py`: per-cell newest-capture-time wins, unknown never overwrites,
order independent). From that map and the driven poses the engine derives **F**, the set of lattice cells (step h = 0.1 m) a robot
of half side r can stand on. The scene graph is a partition of F into **charts** (the mcmaps), each cut from one persistence
**region**, the **links** between charts (passages with a physical width), persistent **keys**, and a **hierarchy of levels**
(charts, regions, coarser regions). Routing runs on the lattice inside F, point to point inside a corridor of charts picked on
the chart graph; the graph serves display, goal selection, corridor selection, passage widths and stable ids. An anchor is an
identity and a display point, never a waypoint.

## 2. Definitions

* **F** (unchanged from the evidence model): components of {robot square fits at one of 23 headings} U {cells within d - r + h of a
  driven pose of a robot of half side d >= r that are not reported blocked} that contain a cell within h of a driven pose.
  F is 4-connected, its complement 8-connected. chi(F) = components4 - holes8.
* **B(x)** = Euclidean distance (cell centres) from x in F to the nearest cell not in F; B = h on the boundary of F.
* **rank**: x precedes y iff B(x) > B(y), or equal and (row, col) smaller. Translation invariant.
* **basin**: every cell hangs on its earliest 4-neighbour that precedes it; cells with none are maxima. Raw basins are
  4-connected.
* **persistence** of a pair of adjacent basins: p = min(peak a, peak b) - s(a, b), s = the largest min(B) over 4-adjacent crossing
  pairs. **merge**: pairs are merged smallest p first while p < tau (survivor: higher peak). Level tau_j = tau0 * 2^j with
  **tau0 = 2 sqrt(2) h**; level 0 is the base partition (`BASE_TAU = 1`), levels continue until every component is one region.
* **chart radius** rho = `SceneParams.chart_radius` = 2.0 m (HEURISTIC 26): the one scale that depends on the site. A chart's
  diameter is at most 2 rho; `snap` is bounded by it.
* **chart** (`subdivide`): inside each region, cells are taken in the order of room (B / h rounded, ties in scan order); a cell
  whose geodesic distance (8-neighbour steps inside the region, a diagonal needing both side cells in the region) to every earlier
  **net point** exceeds rho becomes one. A chart is the set of cells whose nearest net point it is (distance, then earlier net
  point). A region smaller than rho is one chart. The first net point of a region is a cell of maximal B.
* **anchor** (`centres`): the chart cell nearest the chart's centroid (ties: more room, then (row, col)). The net point sits where
  the cut put it, often at the side of a small chart (measured: its eccentricity was 1.52 x the minimum on charts under 150
  cells); the centre is what stands for the chart (1.07 x).
* **link**: one per arc of the interface curve of a chart pair (a maximal chain of unit edges between the two charts, joined at
  shared endpoints; a pair of charts of one region has arcs too). **portal**: the crossing pair attaining s over the arc. **Link.width**: twice the largest, over the
  arc's crossings, of the minimum PHYSICAL clearance (distance to blocked cells) of the crossing pair: a passage width in the same
  unit as a robot's `required_width`, +- h/2. **Link.length**: shortest 8-neighbour route anchor -> portal -> anchor inside the
  union of the two charts. Channel integrals are taken along that route.
* **keys**: a chart inherits the key of the previous chart it shares most cells with (ties: older key); never reused.
* **levels**: level 0 = the charts; level 1 = the persistence regions at tau0 (left out when every region is one chart); then
  tau0 * 2^j until one region per component. Every level is nested in the next. A chart stores its region, peak and anchor; the
  levels are a pure function of those and the links' saddles (`morse.cell_levels`).

## 3. Statements

| # | Statement | Mark | Check |
|---|---|---|---|
| 1 | B has no interior minimum for any tie order: every cell of F has a 4-neighbour strictly lower (toward its nearest non-F cell) | EXACT | elementary; 0 counter-cases in verifier on 8 maps + 300 random masks |
| 2 | chi(F) = #maxima - sum of saddle multiplicities (counting identity of the sweep, any order) | EXACT | per-step chi recomputed by scipy, 0 failures over 8 maps + 300 masks x 4 orders x 3 symmetries |
| 3 | With regions carrying hole counts h_i: chi(F) = sum_i (1 - h_i) - A_open + J_int (planar-map identity; arcs through degree-2 corners, closed loops count 0, interior corners with >= 3 interface edges) | EXACT | verified on 8 real maps + 5 worlds at every level |
| 4 | Raw basins are 4-connected but not always disks (a pillar in a room: 164 of 3000 random shapes); regions are not required to be disks | EXACT | |
| 5 | The partitions of increasing tau are nested (prefix property of the merge sequence); a parent's anchor is its best child's anchor, never created at merge time | EXACT | 13/13 worlds |
| 6 | One boundary flip changes B by at most sqrt(2) h, so persistence below tau0 = 2 sqrt(2) h is not resolvable from a lattice of step h; the bound is attained | EXACT | empirical supremum equals the bound |
| 7 | The partition, anchors and links are a pure function of F (and h): independent of leaf arrival order, batching and history | EXACT | worker == direct fold for batchings 6, 48, 1, one batch, reversed, shuffled on 8 maps |
| 8 | Translation invariance of partition, anchors, links | EXACT | |
| 9 | Link value s(A, B) equals the maximin path value between the two peaks up to tau (join-tree theorem) | EXACT | 62/62 at tau0 against an independent flood fill |
| 10 | The persistence-threshold region count is not stable near the threshold: a hard threshold cannot be stable | TESTED | oscillation only where a persistence value sits within 2 delta of tau (delta = the perturbation of B); 11 % of near pairs at tau0, 6 % at 2 tau0, 0 at 4 tau0 (office8, 55 frames) |
| 11 | Key persistence per frame (office8, 55 frames, level 0): 0.952 (cell persistence 0.984) | TESTED | the star-chart engine measured the same way: 0.980 / 0.973 |
| 12 | Cost of the persistence regions alone: 0.55-1.0 microseconds per cell per batch (hall 31k cells 0.033 s per batch of 6) | TESTED | linear; vectorised numpy except the heap merge and the per-arc route loop |
| 13 | Scale invariance: the same world rasterised at h and 2h gives equal region counts and arcs at every physical tau, widest crossing within 2h | TESTED | rooms and open field |
| 14 | The persistence regions are rooms and corridors (3-7 per 70 m2 office), 2-4x coarser than the old star-chart cover; the charts (statement 26) restore the old scale (8-16 per floor, 93 in all against 102) | TESTED | 7 real maps |
| 15 | The choice of level 0 = tau0 (not 2 tau0) and the exposed level list tau0 * 2^j | HEURISTIC | 2 tau0 persisted worse and collapsed office8 to one region |
| 16 | Tie order (row, col) is a convention; rot90 / mirror equivariance holds only without plateau ties | HEURISTIC | |
| 17 | A chart lies in exactly one persistence region: no chart crosses a pass; the charts of a region partition it | EXACT | by construction; verifier `region`, `nesting` |
| 18 | Covering: every cell of a chart is within geodesic distance rho of its net point, so any two cells are within 2 rho and the anchor is within 2 rho of every cell of its chart | EXACT | an uncovered cell becomes a net point; `test_morse`; verifier `radius` (2 rho from the anchor) on 7 real maps x 2 batchings, 13 worlds, 6 random fields |
| 19 | Packing: two net points of one region are further than rho apart | EXACT | distance is symmetric and a net point is only made from an uncovered cell; `test_morse` |
| 20 | A chart is 8-connected and every cell reaches its net point along a shortest path inside the chart | EXACT | the nearer-point rule with ties to the earlier point is consistent along shortest paths; `test_morse` (Voronoi check) |
| 20a | The anchor (nearest cell to the centroid) has eccentricity 1.07 x the chart's minimum (median), 1.15 x (p90), 1.24 x (max); the net point had 1.20 / 1.77 / 1.93 | TESTED | 84 charts of 20 cells or more on the 7 floors, exact eccentricity |
| 21 | A chart is 4-connected | TESTED | 0 violations in all of the above; the 8-neighbour metric could leave a one-cell diagonal pinch at a bisector |
| 22 | Charts are a pure function of F, h and rho: independent of arrival order and batching, translation invariant (ties in scan order, which a growing canvas keeps) | EXACT | worker == direct fold, 8 maps x batchings 6 and 48 |
| 23 | Locality: owner changes after a batch lie within 0.8 m (hall) and 2.65 m (field) of changed input cells; the same bound as the regions alone for the field | TESTED | `test_store_dirty`; the nearer-anchor tie rule must be canonical (a library multi-source search that picked ties by heap order moved 20 of 30 charts for a 6-cell change) |
| 24 | Key persistence per frame, one leaf per fold: 0.97-0.98 (office8, office7), 0.95 (office6); a corridor grown from either end 0.98 | TESTED | 95-109 frames per floor |
| 25 | Cost with charts: 1.1-1.5 microseconds per cell per batch (hall 45k 0.069 s, ring 53k 0.059 s, field 86k 0.108 s); 2.6 microseconds on a 244k-cell field that is one region (0.63 s) | TESTED | a search per anchor inside a window of side 2 rho; unchanged regions are served from the last solve |
| 26 | rho = 2.0 m default: the sweep rho = 1.5 / 2.0 / 2.5 / 3.5 m gives 128 / 93 / 66 / 51 anchors on the 7 floors and an abstract (anchor chain) stretch p90 of 1.57-1.75 / 1.63-1.83 / 1.79-1.91 / 1.74-2.07; 2.0 matches the previous cover (102 anchors, 1.56-1.67) | HEURISTIC | one physical scale: below it the count grows as 1 / rho^2 without a better stretch |
| 27 | A route searched point to point inside the corridor (charts on the chart path plus their neighbours) reaches the true shortest path on every partition (p90 1.00; the worst of 840 pairs 1.40, the corridor result is not proved shortest); through the two anchors it does not (p90 1.3 - 2.3x on the regions alone) | TESTED | 120 random pairs >= 3 m per floor |
| 28 | `route` minimises an exact integer cost (each lattice edge ceil(length / 1e-4 m), summed exactly in float64) and among equal-cost paths returns the lexicographic minimum of its (y, x) cell sequence: a rule of the geometry alone (cells are row-major), so any reader of the same cells returns the same route and chart sequence; `Route.cost` is the integer, `Route.length` the geometric length of that path | EXACT | independent reference (heapq Dijkstra + DP over the shortest-path DAG) equal on 3 worlds, under an overlay, for batching 1 / 5 / 7 / 48, shuffled arrival and a translated world; lengths on the large worlds equal the previous implementation to 0.0000 m; latency unchanged (4 - 6 ms median on 45k - 86k cells) |

## 4. What this partition is not

* Not star-shaped: 2-8 % of a region's cells are not straight-line visible from its anchor at tau0. No consumer uses it (checked by
  grep: route runs on the lattice; cover, snap, raster, widths and ids use cells, anchors and links).
* Not minimal and not a cover by convex or visibility-closed pieces; regions may have holes (a pillar in a room is one region),
  and so may a chart when the obstacle is smaller than the chart: a loop is a cycle of the chart graph only when its obstacle
  is larger than about a chart. The loop of a constant-width ring corridor is cut into charts that do form a cycle.
* Not locally defined: an anchor is placed relative to higher-room cells within rho, a chain along a ridge. Measured locality is
  statement 23; it is not proved.
* Not canonical in the persistence sense below tau0: levels finer than tau0 are not exposed.
* Not a loop detector. A corridor loop of constant width is a single ridge of B with no peak per corner, every raw basin differs
  by less than tau0 and the loop is merged whole: ONE region (a region may have holes). Long corridors are likewise one region.
  A room is its own region only when its peak B exceeds the saddle of the corridor leaving it by more than tau0 (about a 1.8 m
  room for a 1.2 m corridor). Cycles of rooms joined by corridors are cycles of the link graph and are represented there.
* A route's `min_width` is a property of its chart sequence: per crossed chart pair the WIDEST arc (Link.width), minimum over
  the pairs. A pair can have several arcs (the passage and witness slivers along a wall, 0.1 - 0.3 m), a lattice path that hugs
  a sliver is geometry, not a narrower door. Consequence: a genuinely narrower second door between the same two regions is
  not reported when a wider one exists. EXACT as a definition, TESTED on the room loop under shuffled stamps.

## 5. Generality (indoors and outdoors)

* One environment scale: rho (statement 26), a physical size in metres like h and r. Everything else derives from h, r and the
  data. `morse.py` takes h and rho as arguments. An outdoor site wanting larger charts raises `chart_radius`; up to about one storage tile (6.4 m) no storage setting changes.
* The levels continue until one region per component, so an outdoor map gets as many levels as its scale needs.
* The lattice is two costmap cells (`LAT = 2` in `engine.py`: `// 2` and `2*x+1` in `_cell_xy`, snap, query windows, `import_state`
  padding and pose cells; `TILE`, `RES`; `store.py`: `TILE_F`, `TILE_L`, tile slicing). A coarser lattice for a large outdoor
  site is a parameterisation of these places, not a change of the partition.
* Validation data is indoor (8 single-floor offices of one robot); outdoor behaviour is tested on synthetic worlds only
  (open field, 30 x 30 m ring road): frontier flicker bounded (ring road: 0 transient keys, mean churn 1.2 %).

## 6. Open

* **Rate pricing (not built; later format step).** Price a cell by the age-decayed posterior rate at which it was seen blocked, cost
  per cell length / (1 - p), p capped at 0.75, integer weights >= 1, an overlay stays hard. Conditions agreed with the planner side:
  the counts are a separate versioned object (not in tiles or the manifest hash); counts need time buckets or a last-look plane
  to decay; `route(now=...)` takes an explicit quantised time and `now=None` is today's behaviour; the engine never reads a clock;
  `Route.length` stays geometric and `Route.cost` is added; under pricing the corridor is a seed, not exact, so the search covers the
  whole graph and an `exact` flag is exposed; ties are canonical on integer cost (landed: statement 28; the rule is on (y, x), not on chart
  keys, which are history dependent). Counting needs per-leaf-key dedup in the engine
  because delivery is at-least-once (restart, rebuild resume, backfill re-deliver): same (t, session, x, y, r_d, view) adds nothing,
  absent adds, changed triggers a counts rebuild; the counted stamp is persisted in the leaf record. `api.py` "evidence and length,
  never a price" is amended when this lands.

* Exact locality: persistence pairing is global in a component; the engine recomputes the persistence regions of the whole map per
  batch (affordable at the measured cost), serves unchanged regions' charts from the last solve, and writes only
  content-hash-changed objects.
* A tiny chart is a free component of three cells or fewer where the robot turned in place (7 on the 7 floors); it has no links.
* Anchor density: rho is one number for all sites; a site with 30 m halls and 1 m corridors might want a radius that follows the
  local room (rho proportional to B, capped). Not needed for the indoor data and not built.
* The base level and the exposed level list are consumer choices (HEURISTIC 15).
* Real corridors, doors and open space from the robot's own costmap are absent from the data.
* Vectorisation of the per-arc route loop for very large maps.
