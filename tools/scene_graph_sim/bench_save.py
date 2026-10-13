"""CPU and RPC cost of one incremental save on a large state (a fake engine: synthetic planes, charts, links, leaves).

    python bench_save.py [charts=20000] [leaves=100000] [side_cells=4096]

Prints, for a save that adds ~50 leaves and dirties one tile: CPU seconds, how many tiles / pages / segments were packed,
how many `blobs.exists` / `put` calls were made, and the first (full) save for comparison. Memory backend only."""
import os
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.dirname(os.path.dirname(HERE))]
import packages.scene_graph.store as S  # noqa: E402
from packages.scene_graph.api import BBox, Chart, Dirty, Link, SceneParams, ViewResult  # noqa: E402
from packages.scene_graph.state import EngineState, LeafRecord  # noqa: E402
from packages.scene_graph.store import MemoryBackend, SceneGraphStore  # noqa: E402

N_CH = int(sys.argv[1]) if len(sys.argv) > 1 else 20000
N_LV = int(sys.argv[2]) if len(sys.argv) > 2 else 100000
SIDE = int(sys.argv[3]) if len(sys.argv) > 3 else 4096
RES = 0.05


class Counting(MemoryBackend):
    def __init__(self):
        super().__init__(); self.exists_calls = 0; self.put_calls = 0
    def exists(self, m, k): self.exists_calls += 1; return super().exists(m, k)
    def put(self, m, k, d): self.put_calls += 1; return super().put(m, k, d)


class FakeSG:
    def __init__(self):
        rng = np.random.default_rng(1)
        self.params = SceneParams()
        self.V = np.zeros((SIDE, SIDE), np.uint8); self.V[::7, :] = 100
        self.T = np.full((SIDE, SIDE), 1_700_000_000_000, np.int64); self.B = np.full((SIDE, SIDE), -1, np.int64)
        h = SIDE // 2
        side = int(np.ceil(np.sqrt(N_CH)))
        yy, xx = np.mgrid[0:h, 0:h]
        self.plane = ((yy * side // h) * side + (xx * side // h)).astype(np.int32)
        self.plane[self.plane >= N_CH] = N_CH - 1
        self.keys = [("k%06d" % (i // 3), i % 3) for i in range(N_CH)]
        pitch = h // side
        self.anchors = [((i // side) * pitch + pitch // 2, (i % side) * pitch + pitch // 2) for i in range(N_CH)]
        self.leaves = [LeafRecord("lf%07d" % i, "s%d" % (i % 4), 1_700_000_000_000 + i * 1000, float(rng.uniform(0, SIDE * RES)),
                                  float(rng.uniform(0, SIDE * RES)), 0.25) for i in range(N_LV)]
        self.version = 1; self.watermark = (self.leaves[-1].t, self.leaves[-1].key)
        self.changed = []

    def export_state(self):
        return EngineState(params=dict(robot_half_side=0.25, lattice=0.1, chart_radius=2.0), cx0=0, cy0=0,
                           V=self.V, T=self.T, B=self.B, leaves=list(self.leaves), keys=self.keys,
                           plane=self.plane, version=self.version, watermark=self.watermark)

    def query(self, bbox, level=0, raster=True, **kw):
        h = SIDE // 2; side = int(np.ceil(np.sqrt(N_CH))); pitch = h // side
        ch = []
        for i, k in enumerate(self.keys):
            y, x = self.anchors[i]
            ch.append(Chart("%s:%d" % k, ((2 * x + 1) * RES, (2 * y + 1) * RES), 100, 1.0, 0.5, 0.5, 0.0, None))
        lk = []
        for i in range(N_CH):
            if i % side != side - 1 and i + 1 < N_CH:
                lk.append(Link(ch[i].key, ch[i + 1].key, 1.0 + (i % 5), 0.9, ((0.0, 0.0), (0.1, 0.1)), {"undriven": 0.0}))
        return ViewResult(self.version, ch, lk, None)


def counted(be):
    cnt = {"pack": 0, "pack_json": 0}
    p, pj = S.pack, S.pack_json
    def pack(*a, **k): cnt["pack"] += 1; return p(*a, **k)
    def pack_json(*a, **k): cnt["pack_json"] += 1; return pj(*a, **k)
    S.pack, S.pack_json = pack, pack_json
    return cnt, (p, pj)


be = Counting(); st = SceneGraphStore(be, be, verify_every=0)
sg = FakeSG()
cnt, orig = counted(be)
t = time.process_time(); v = st.save("m", sg, Dirty(None, [], {}, [], sg.watermark), None, {"c": 0}); dt = time.process_time() - t
print(f"charts {N_CH}, leaves {N_LV}, canvas {SIDE}x{SIDE} cells ({(SIDE // 128) ** 2} tiles)")
print(f"first save : {dt:6.2f} cpu-s  pack(tiles+segments)={cnt['pack']}  pack_json(pages)={cnt['pack_json']}  exists={be.exists_calls}  puts={be.put_calls}")
for rep in range(3):
    for k in ("pack", "pack_json"): cnt[k] = 0
    be.exists_calls = be.put_calls = 0
    n0 = len(sg.leaves)
    for i in range(50):
        sg.leaves.append(LeafRecord("new%d_%d" % (rep, i), "s0", sg.leaves[-1].t + 1000, 10.0 + i * 0.01, 10.0, 0.25))
    sg.watermark = (sg.leaves[-1].t, sg.leaves[-1].key)
    sg.T[100:140, 100:140] = sg.leaves[-1].t; sg.version += 1                       # one tile changes
    d = Dirty(BBox(5.0, 5.0, 7.0, 7.0), [], {}, [], sg.watermark)
    t = time.process_time(); v = st.save("m", sg, d, v, {"c": rep + 1}); dt = time.process_time() - t
    print(f"save +50   : {dt:6.2f} cpu-s  pack(tiles+segments)={cnt['pack']}  pack_json(pages)={cnt['pack_json']}  exists={be.exists_calls}  puts={be.put_calls}")
