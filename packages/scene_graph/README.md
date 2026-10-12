# packages/scene_graph

Pure library (numpy + scipy, no I/O, no config import). Design and results: `docs/scene_graph/design.md`.

| module | contents |
|---|---|
| `params.py` | `Params`: tau, gamma, hysteresis, robot radius, budget, cost weights |
| `cellmap.py` | `CellMap`: sparse chunked lattice, planes value / owner / visited; `fold`, `crop` |
| `graph.py` | clearance and cost fields, coarse lattice, graph, bottleneck |
| `partition.py` | basins, saddles, dilation, `decompose`, `bisect`, `settle` (repair in place) |
| `builder.py` | `SceneGraph.add_leaf`, anchors, `links()` |
| `levels.py` | `build_levels`: the same rules on the anchors |
| `routing.py` | `Router`: portal graph, route cost between cells |
| `metrics.py` | invariants and shape numbers |

```python
sg = SceneGraph(Params(robot_radius=0.3))
for leaf in leaves:                 # Leaf(key, x, y, i0, j0, window_u8, session)
    sg.add_leaf(leaf)
sg.anchors, sg.links(), build_levels(sg), Router(sg).cost(node_a, node_b)
```

## v2: free-space charts (the engine behind `api.SceneGraph`)

| module | contents |
|---|---|
| `api.py` | the contract: `SceneParams`, `Leaf`, `Dirty`, `Chart`, `Link`, `Route`, `ViewResult`, `OwnerRaster`, `SceneGraph` |
| `engine.py` | `ChartGraph(SceneGraph)`: fold, F(r), local re-solve, links, keys, locate / route / query / corridor / cover |
| `chart_solver.py`, `chart_track.py` | canonical star-shaped cover and its per-batch local update |
| `chart_keys.py`, `chart_levels.py` | persistent keys by overlap; hierarchy as a derived view |
| `window.py`, `leafcover.py`, `depth_occupancy.py` | rotated / rescaled windows; minimal leaf set; occupancy from depth |
| `store.py` | persistence signatures only (not implemented) |

```python
from packages.scene_graph.api import SceneParams, Leaf
from packages.scene_graph.engine import ChartGraph
g = ChartGraph(SceneParams(robot_half_side=0.25))
dirty = g.fold_many(leaves)            # any order, duplicates allowed
view = g.query(bbox)                   # charts, links, owner raster
```
Tests: `tests/unit/scene_graph/test_engine.py`, judged by the independent verifier `chart_verify.py`.
Real data: `tools/scene_graph_sim/run_engine.py office8 --verify`.
