# packages/scene_graph

Pure library (numpy + scipy, no I/O, no config import). Design: `docs/scene_graph/design.md`.

## The engine behind `api.SceneGraph`

| module | contents |
|---|---|
| `api.py` | the contract: `SceneParams`, `Leaf`, `Dirty`, `Chart`, `Link`, `Route`, `ViewResult`, `OwnerRaster`, `SceneGraph` |
| `engine.py` | `ChartGraph(SceneGraph)`: fold, F(r), one partition per batch, links, keys, locate / route / query / corridor / cover |
| `morse.py` | the persistence (Morse) partition of F, its arcs and levels: pure functions of F and h, no hidden scale |
| `chart_keys.py` | persistent keys by overlap |
| `window.py`, `leafcover.py`, `depth_occupancy.py` | rotated / rescaled windows; minimal leaf set; occupancy from depth |
| `store.py`, `store_codec.py`, `store_backends.py` | content-addressed snapshots: tiles, pages, leaf segments, manifest, CAS version |
| `state.py` | `EngineState`: what `export_state` / `import_state` carry |

```python
from packages.scene_graph.api import SceneParams, Leaf
from packages.scene_graph.engine import ChartGraph
g = ChartGraph(SceneParams(robot_half_side=0.25))
dirty = g.fold_many(leaves)            # any order, duplicates allowed
view = g.query(bbox)                   # charts, links, owner raster
```
Tests: `tests/unit/scene_graph/test_engine.py`, judged by the independent verifier `chart_verify.py`.
Real data: `tools/scene_graph_sim/run_engine.py office8 --verify`.
