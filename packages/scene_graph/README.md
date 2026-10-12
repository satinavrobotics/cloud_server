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
