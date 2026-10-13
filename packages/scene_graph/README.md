# packages/scene_graph

Pure library (numpy + scipy, no I/O, no config import). Definition and verification: `docs/scene_graph/formulation.md`, `docs/scene_graph/audit_ledger.md`.

## The engine behind `api.SceneGraph`

| module | contents |
|---|---|
| `api.py` | the contract: `SceneParams`, `Leaf`, `Dirty`, `Chart`, `Link`, `Route`, `ViewResult`, `OwnerRaster`, `SceneGraph` |
| `engine.py` | `ChartGraph(SceneGraph)`: fold, F(r), one partition per batch, links, keys, locate / route / query / corridor / cover |
| `morse.py` | the persistence regions of F cut into bounded charts, their arcs and levels: pure functions of F, h and the chart radius |
| `chart_keys.py` | persistent keys by overlap |
| `window.py`, `leafcover.py`, `depth_occupancy.py` | rotated / rescaled windows; minimal leaf set; occupancy from depth |
| `store.py`, `store_codec.py`, `store_backends.py` | content-addressed snapshots: tiles, pages, leaf segments, manifest, CAS version |
| `state.py` | `EngineState`: what `export_state` / `import_state` carry |

```python
from packages.scene_graph.api import SceneParams, Leaf
from packages.scene_graph.engine import ChartGraph
g = ChartGraph(SceneParams(robot_half_side=0.25))     # chart_radius = 2.0 m: the one scene-dependent scale
dirty = g.fold_many(leaves)            # any order, duplicates allowed
view = g.query(bbox)                   # charts, links, owner raster
route = g.route(a, b, start=(x, y), goal=(x, y))   # point to point inside the corridor of charts
```
Tests: `tests/unit/scene_graph/test_engine.py`, judged by the independent verifier `chart_verify.py`.
Real data: `tools/scene_graph_sim/run_engine.py office8 --verify`.
