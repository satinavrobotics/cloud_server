# Scene graph: the persistence (Morse) partition of navigable free space

Status 2026-10-13. This is the definition the code implements (`packages/scene_graph/morse.py`, engine in `engine.py`) and the
statements it relies on. Every statement is marked **EXACT** (proved, with an independent numeric check), **TESTED** (measured,
n given) or **HEURISTIC** (a choice, not derived). The derivation record, with the statements that were refuted on the way, is in
`audit_ledger.md`; the reference research code and its independent verifier were `theory/morse2.py` and `theory/verify2.py`
(kept in the session scratchpad; the production verifier is `tests/unit/scene_graph/chart_verify.py`).

## 1. What it is for

A robot's costmap windows are folded into one map (`engine.py`: per-cell newest-capture-time wins, unknown never overwrites,
order independent). From that map and the driven poses the engine derives **F**, the set of lattice cells (step h = 0.1 m) a robot
of half side r can stand on. The scene graph is a partition of F into **regions** (the mcmaps), the **links** between regions
(passages with a physical width), persistent **keys**, and a **hierarchy of levels**. Routing runs on the lattice inside F;
the graph serves display, goal selection, corridor selection, passage widths and stable ids.

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
* **anchor**: the cell of maximal B of a region (plateau: the plateau cell nearest its centroid; ties by (row, col)).
* **link**: one per arc of the interface curve of a region pair (a maximal chain of unit edges between the two regions, joined at
  shared endpoints). **portal**: the crossing pair attaining s over the arc. **Link.width**: twice the largest, over the
  arc's crossings, of the minimum PHYSICAL clearance (distance to blocked cells) of the crossing pair: a passage width in the same
  unit as a robot's `required_width`, +- h/2. **Link.length**: shortest 8-neighbour route anchor -> portal -> anchor inside the
  union of the two regions. Channel integrals are taken along that route.
* **keys**: a region inherits the key of the previous region it shares most cells with (ties: older key); never reused.

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
| 12 | Cost: about 0.55-1.0 microseconds per cell per batch for the partition (hall 31k cells 0.033 s per batch of 6; field 86k 0.048 s) | TESTED | linear; vectorised numpy except the heap merge and the per-arc route loop |
| 13 | Scale invariance: the same world rasterised at h and 2h gives equal region counts and arcs at every physical tau, widest crossing within 2h | TESTED | rooms and open field |
| 14 | The base level coincides with rooms and corridors (2-7 regions per 70 m2 office), 2-4x coarser than the old star-chart cover | TESTED | 8 real maps |
| 15 | The choice of level 0 = tau0 (not 2 tau0) and the exposed level list tau0 * 2^j | HEURISTIC | 2 tau0 persisted worse and collapsed office8 to one region |
| 16 | Tie order (row, col) is a convention; rot90 / mirror equivariance holds only without plateau ties | HEURISTIC | |

## 4. What this partition is not

* Not star-shaped: 2-8 % of a region's cells are not straight-line visible from its anchor at tau0. No consumer uses it (checked by
  grep: route runs on the lattice; cover, snap, raster, widths and ids use cells, anchors and links).
* Not minimal and not a cover by convex or visibility-closed pieces; regions may have holes (a pillar in a room is one region).
* Not canonical in the persistence sense below tau0: levels finer than tau0 are not exposed.

## 5. Generality (indoors and outdoors)

* No environment constant: every number derives from h (lattice), r (robot) and the data. `morse.py` takes h as an argument.
* The levels continue until one region per component, so an outdoor map gets as many levels as its scale needs.
* The lattice is two costmap cells (`LAT = 2` in `engine.py`: `// 2` and `2*x+1` in `_cell_xy`, snap, query windows, `import_state`
  padding and pose cells; `TILE`, `RES`; `store.py`: `TILE_F`, `TILE_L`, tile slicing). A coarser lattice for a large outdoor
  site is a parameterisation of these places, not a change of the partition.
* Validation data is indoor (8 single-floor offices of one robot); outdoor behaviour is tested on synthetic worlds only
  (open field, 30 x 30 m ring road): frontier flicker bounded (ring road: 0 transient keys, mean churn 1.2 %).

## 6. Open

* Exact locality: persistence pairing is global in a component; the engine recomputes the whole map per batch (affordable at the
  measured cost) and writes only content-hash-changed objects.
* The base level and the exposed level list are consumer choices (HEURISTIC 15).
* Real corridors, doors and open space from the robot's own costmap are absent from the data.
* Vectorisation of the per-arc route loop for very large maps.
