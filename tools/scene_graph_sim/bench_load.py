"""load() time and memory against the size of a map (real engine on a long hall, saved to a memory backend).

    python bench_load.py 1000 4000 16000     # target known lattice cells per map

Per size: engine build seconds, leaves, known lattice cells, engine.nbytes, full save seconds, stored bytes, load seconds,
RSS growth of the process around load(), bytes per known cell. Memory backend; nothing touches a database."""
import gc
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path[:0] = [ROOT]

from packages.scene_graph.api import SceneParams  # noqa: E402
from packages.scene_graph.engine import ChartGraph  # noqa: E402
from packages.scene_graph.store import MemoryBackend, SceneGraphStore  # noqa: E402
from tests.unit.scene_graph import chart_worlds as cw  # noqa: E402


def rss_mb():
    with open("/proc/self/statm") as f:
        return int(f.read().split()[1]) * os.sysconf("SC_PAGE_SIZE") / 1e6


FIELD = os.environ.get("WORLD") == "field"              # WORLD=field: an open square with a lawnmower drive (no walls inside)
for target in (int(a) for a in sys.argv[1:]):
    if FIELD:
        side = max(4.0, (target * 0.01) ** 0.5 + 0.4)
        w = cw._blank(side, side); way = []; y = 1.5; d = 1
        while y < side - 1.2:
            way += [(1.5, y), (side - 1.5, y)] if d > 0 else [(side - 1.5, y), (1.5, y)]
            y += 2.0; d = -d
        leaves = cw.drive(w, way, step=0.6)
    else:
        W = max(4.0, target * 0.01 / 2.6 + 0.4)         # a hall 3 m wide, one door in the middle
        w, way = cw.two_rooms(W=W)
        leaves = cw.drive(w, way)
    t = time.time(); g = ChartGraph(SceneParams())
    for i in range(0, len(leaves), 24): g.fold_many(leaves[i:i + 24])
    build = time.time() - t
    st_ = g.export_state()
    known = int(((st_.plane >= 0)).sum())
    be = MemoryBackend(); st = SceneGraphStore(be, be, verify_every=0)
    t = time.time(); st.save("m", g, g.fold_many([]), None, {}); save = time.time() - t
    stored = sum(len(v) for v in be.blobs["m"].values())
    nb = g.nbytes
    del g, st_; gc.collect()
    r0 = rss_mb(); t = time.time(); h = st.load("m"); load = time.time() - t; r1 = rss_mb()
    print(f"target {target:6d}: leaves {len(leaves):4d} navigable lattice cells {known:6d}  build {build:6.1f}s  save {save:5.2f}s  "
          f"load {load:5.2f}s  engine.nbytes {nb / 1e6:7.1f} MB ({nb / max(known, 1):6.0f} B/cell)  RSS +{r1 - r0:6.1f} MB  "
          f"stored {stored / 1e3:7.1f} KB", flush=True)
    del h; gc.collect()
