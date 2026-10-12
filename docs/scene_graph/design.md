# Scene graph: free-space places from topo-node costmaps

Status: validated offline on one real map (office8) and six synthetic worlds. Nothing here touches a running
service, the live ArangoDB or MinIO. Integration is scoped in `integration.md`.

## 1. What it is

The map is a lattice of cells. The robot sends, per topo node, a costmap window (`robot/costmap_upload`, already
stored by graph-builder). A **leaf** is such a node: a cell (where the robot stood) plus its window. A leaf is
folded into the cell map once and is not queried again (the topo nodes stay as logs).

The cell map is **partitioned** into regions. The cells of a region are the **mcmap** of its **anchor**, so the
mcmaps slice the map: the cell counts add up to the map's, and stitching them gives the map. An anchor is a cell:
the cell of its region's wide places that is nearest, in traversal cost, to the other cells (the medoid).
Anchors with the seams between them form the graph; the same rules applied to the anchors give the hierarchy.

## 2. Pipeline

```
leaf (cell p, 200x200 window)
 |  fold          newest observation wins, unknown never overwrites; the body (and the path from the previous
 |                leaf of the session) is free; p is marked visited
 v
cell map  --- fields:   clearance c(x) = distance to the nearest blocked or unknown cell
 |                      navigable  = known, not lethal, c >= robot radius
 |                      cost w(x)  = 1 + soft value + exp(-decay (c - radius))
 |  coarsen       blocks of k x k cells, k = largest for which no step can cross a wall (k = 4 for 0.25 m)
 v
partition (on the coarse lattice)          see 3
 |  repair        previous regions are kept; new cells join the nearest region; regions that stop being charts
 |                are re-decomposed alone; neighbours merge only by a margin (hysteresis)
 v
anchors (medoids)  ->  links (one per stretch of shared boundary)  ->  portals  ->  levels
```

## 3. The partition

Free-space structure comes from the clearance field. Two tests decide what is one place.

**Bottleneck.** Flood c downhill from its maxima: each cell joins the basin of its highest neighbour (watershed).
Two basins touch at a saddle, the highest crossing; the passage there is 2 x saddle wide. Basins merge in order of
decreasing saddle when `saddle >= gamma x` the narrower peak: the passage is nearly as wide as the places it joins.
Doors and corridor mouths stay seams without any metre-valued threshold.

**Chart.** Any two cells of a region are within `tau` of straight from each other by a path inside the region:
`(path - free_detour) / straight <= tau`. This is a bounded geodesic dilation, the large-scale part of "locally
Euclidean". A convex place has 1, an L of equal arms sqrt 2, a ring or a U more. Pairs across a *thin* obstacle are
close in space and far in path, so this is what keeps both sides of a wall in different regions, which a test
from the anchor alone does not (a doorway sees both rooms). `free_detour` (4 robot radii) lets an obstacle a few
robot widths across sit inside a place; the extremal pairs of a planar region lie on its boundary, so the test
runs from boundary cells (checked against all pairs: within 2%).

**Budget.** At most `budget` cells per mcmap (one leaf window): a resource bound, not geometry.

**Transit.** A basin whose own clearance is below 1.5 x robot radius is a gap or a corner, not a place; it joins
its widest neighbour.

**Evolution.** The canonical decomposition above is chaotic in open space (a small change of the map reorders
merges), so it is used locally: to start a region, and to re-decompose a region that is no longer a chart; a
region that still fails after that is cut by geodesic bisection. Neighbours merge when the tests hold by
`hysteresis`, and stay apart until they fail by it. Result: per leaf, mean 2.7% of the cells change owner
(office8; most of it while the map first forms).

## 4. Anchors, links, routing, levels

* **Anchor**: medoid of the region's wide places (clearance >= gamma x the peak) under the traversal cost; it moves
  only if the new place is better by `slack`. Measured against the exact medoid: 0 - 4.6% mean cost, on the
  synthetic worlds and office8.
* **Link**: one per connected stretch of boundary between two mcmaps (parallel links = a loop around an island).
  Cost = shortest traversal cost anchor -> portal -> anchor inside the two mcmaps; width = 2 x the narrowest
  clearance on that path. Door of 0.9 m is reported 0.95 m (block pooling, +half a block).
* **Routing** (`routing.Router`): portal graph, up to 6 crossings sampled per seam, in-region cost between
  portals. Stretch against the true shortest path, cross-region pairs: median 1.00 - 1.02, p90 <= 1.05,
  max <= 1.24 (office8 with 6 portals per seam; with 1 per seam: median 1.17, max 2.3, which is why a seam is
  sampled). Routing through anchors only (anchor -> anchor) is much worse (median 1.3 - 1.9) and is not used.
* **Levels** (`levels.build_levels`): the same two tests on the anchors, with the bottleneck threshold relaxed per
  level (gamma / 2^k); a group's anchor is its member nearest to the others; its mcmap is the union of its members'.
  office8: 6 -> 2 -> 1; two rooms with a door: 2 -> 1.

## 5. What the data says (office8, 97 leaves, one session, 0.05 m, 10 m windows)

| | |
|---|---|
| Known cells in the stitched map | 28,196 (70 m^2); a leaf adds a median 73 new cells |
| Fold of one leaf (numpy) | 0.3 ms; whole rebuild per leaf about 20 ms |
| Anchors | 6 (5 - 6 under shuffled arrival order; 7 once) |
| Chart quality | max dilation 1.40; geodesic Voronoi of the same anchors: 3.42 |
| Seams | follow the narrowest passages; 5 - 20% of cells differ from the Voronoi of the same anchors |
| Parameter response | tau 1.2/1.5/2.5 -> 9/6/3 anchors; gamma 0.3-0.5 same, 0.9 -> 16; robot radius 0.15/0.25/0.35 -> 15/6/3 |

## 6. Parameters (`params.py`)

`tau` 1.5 (chart), `gamma` 0.5 (bottleneck), `hysteresis` 0.15, `free_detour` 4 radii, `min_place` 1.5 radii,
`robot_radius` 0.25 m (**placeholder**: the partition depends on it strongly, see above), `budget` 40,000 cells,
`slack` 0.05. Everything else is a cost weight or a margin.

## 7. Limits and open points

* One real map, a single open room with furniture: no door or corridor in real data yet. The synthetic worlds
  cover doors, L, ring, island; a corridor/door map (the office6 bag, or a new run) is the next check.
* The robot radius is not known here; results move with it. Set it per robot.
* The partition is repaired in place per leaf over the whole connected map: O(map) per leaf. Fine for rooms and
  floors (milliseconds to tens of ms); a per-component repair is the extension if one map gets very large.
* Arrival order changes the exact seams (anchor count 4 - 7 over shuffles): the structure is history-dependent by
  design (stability), not canonical.
* Outdoors is untested: it only needs the costmap values, but vegetation costs (`soft_weight`) are not tuned.
* Widths come from block-pooled clearance (+0.05 m typical); a width for hard feasibility decisions should be
  re-measured on the fine grid at the portal.
* `scipy` is used (graph algorithms, distance transforms); it is not in the service requirement files.
* The costmap row order is the nav2 one (row 0 = origin y); verified against the robot side.

## 8. Storage (superseded by `integration.md`)

Anchors, links and the hierarchy fit ArangoDB as `sg_anchors` (vertex: map, level, id, x, y, cells, members),
`sg_links` (edge: cost, length, width, portal, level) and `sg_meta` (parameters, version). `tools/scene_graph_sim/
store_test_arango.py` writes them to the throw-away test instance and a weighted `SHORTEST_PATH` over `sg_links`
returns the expected route. The mcmaps are the owner plane of the cell map: stored as one small raster per anchor
(owned cells, value, visited) next to the node costmaps; an anchor's mcmap is recomputed from them, no leaf is
needed. No leaf membership table is needed.
