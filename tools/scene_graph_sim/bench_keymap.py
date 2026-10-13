"""Cost of commit_generation's key map on a large pair of manifests (a fake engine: synthetic planes, charts, no links).

    python bench_keymap.py [tiles_per_side=44] [charts_per_tile=4]

Builds a live generation and a pending generation of ~tiles_per_side^2 tiles whose owner planes differ only in a few
tiles (a params change that moved a handful of anchors), then times `commit_generation`. Memory backend only."""
import os
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.dirname(os.path.dirname(HERE)), HERE]
from packages.scene_graph.api import BBox, Chart, Dirty, SceneParams, ViewResult  # noqa: E402
from packages.scene_graph.state import EngineState, LeafRecord  # noqa: E402
from packages.scene_graph.store import MemoryBackend, SceneGraphStore  # noqa: E402

TS = int(sys.argv[1]) if len(sys.argv) > 1 else 44
CPT = int(sys.argv[2]) if len(sys.argv) > 2 else 4
SIDE = TS * 128


class Fake:
    def __init__(self, shift_tiles=()):
        self.params = SceneParams()
        h = SIDE // 2
        self.V = np.zeros((SIDE, SIDE), np.uint8)
        self.T = np.broadcast_to(np.int64(1_700_000_000_000), (SIDE, SIDE))
        self.B = np.broadcast_to(np.int64(-1), (SIDE, SIDE))
        n_ch = TS * TS * CPT
        # each tile holds CPT charts as vertical stripes of its 64 lattice columns
        yy, xx = np.mgrid[0:h, 0:h]
        ty, tx = yy // 64, xx // 64
        stripe = (xx % 64) * CPT // 64
        plane = ((ty * TS + tx) * CPT + stripe).astype(np.int32)
        for (a, b) in shift_tiles:                     # re-cut these tiles at another place: owner ids move
            m = (ty == a) & (tx == b)
            plane[m] = ((a * TS + b) * CPT + ((xx[m] % 64) * CPT // 64 + 1) % CPT).astype(np.int32)
        self.plane = plane
        self.keys = [("b%d" % (i // CPT), i % CPT) for i in range(n_ch)]
        self.anchors = [((i // CPT // TS) * 64 + 32, (i // CPT % TS) * 64 + (i % CPT) * (64 // CPT) + 8) for i in range(n_ch)]
        self.leaves = [LeafRecord("lf%d" % i, "s", 1_700_000_000_000 + i, 1.0, 1.0, 0.25) for i in range(1000)]
        self.version = 1; self.watermark = (self.leaves[-1].t, self.leaves[-1].key)

    def export_state(self):
        return EngineState(params=dict(robot_half_side=0.25, lattice=0.1, chart_radius=2.0), cx0=0, cy0=0,
                           V=self.V, T=self.T, B=self.B, leaves=self.leaves, keys=self.keys,
                           plane=self.plane, version=self.version, watermark=self.watermark)

    def query(self, bbox, level=0, raster=True, **kw):
        ch = [Chart("%s:%d" % k, ((2 * x + 1) * 0.05, (2 * y + 1) * 0.05), 100, 1.0, 0.5, 0.5, 0.0, None)
              for k, (y, x) in zip(self.keys, self.anchors)]
        return ViewResult(self.version, ch, [], None)


be = MemoryBackend(); st = SceneGraphStore(be, be, verify_every=0)
live = Fake(); v = st.save("m", live, Dirty(None, [], {}, [], live.watermark), None, {})
gen = st.begin_generation("m")
pend = Fake(shift_tiles=[(3, 4), (10, 11)])
st.save("m", pend, Dirty(None, [], {}, [], pend.watermark), v, {}, generation=gen)
man = st.manifest("m")
print(f"tiles {len(man['tiles'])}, charts {len(live.keys)}")
t = time.process_time(); newv, kmap = st.commit_generation("m", gen); dt = time.process_time() - t
print(f"commit_generation: {dt:.2f} cpu-s, keymap entries {len(kmap)}, charts with a different successor "
      f"{sum(1 for k, s in kmap.items() if s != [k])}")
