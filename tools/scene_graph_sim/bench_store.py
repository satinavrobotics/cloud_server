"""Size and time of the snapshot store.

    python bench_store.py office8          # real leaves through the engine, saved per batch
    python bench_store.py synthetic 400    # a 400 x 400 m canvas of rooms, ~mostly unknown, saved as one state

Memory backend only; nothing touches a database. Prints bytes per known cell (raw planes vs stored objects), objects and
bytes per incremental save, and save / load seconds.
"""
import os
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path[:0] = [ROOT, HERE]
from packages.scene_graph.api import BBox, Chart, Dirty, Link, SceneParams, ViewResult  # noqa: E402
from packages.scene_graph.state import EngineState, LeafRecord  # noqa: E402
from packages.scene_graph.store import MemoryBackend, SceneGraphStore  # noqa: E402


def _stored(b, m):
    return sum(len(v) for v in b.blobs.get(m, {}).values())


def office8():
    from run_engine import load
    from packages.scene_graph.engine import ChartGraph
    leaves = load("office8")
    b = MemoryBackend(); store = SceneGraphStore(b, b)
    g = ChartGraph(SceneParams()); v = None
    print("batch  objects_new  bytes_new   total_bytes  save_s")
    for i in range(0, len(leaves), 6):
        d = g.fold_many(leaves[i:i + 6])
        before = set(b.list("o8")); nb = _stored(b, "o8")
        t = time.time(); v = store.save("o8", g, d, v, {"i": i}); dt = time.time() - t
        new = set(b.list("o8")) - before
        print(f"{i // 6:5d}  {len(new):10d}  {_stored(b, 'o8') - nb:9d}  {_stored(b, 'o8'):11d}  {dt:6.3f}")
    st = g.export_state()
    known = int(((st.V != 255) | (st.T >= 0)).sum()); raw = st.V.nbytes + st.T.nbytes + st.B.nbytes
    man = store.manifest("o8")
    tile_bytes = sum(len(b.blobs["o8"]["g1/t/" + h]) for h in man["tiles"].values())
    print(f"known cells {known}; canvas planes raw {raw} B ({raw / max(known, 1):.0f} B/known cell); "
          f"stored tiles {tile_bytes} B ({tile_bytes / max(known, 1):.1f} B/known cell, {len(man['tiles'])} tiles); "
          f"total objects {len(b.list('o8'))}, {_stored(b, 'o8')} B")
    t = time.time(); h = store.load("o8"); print(f"load (planes + evidence + one re-solve): {time.time() - t:.2f} s")
    t = time.time(); view = store.view("o8"); view.query(BBox(-100, -100, 100, 100)); print(f"view+query without engine: {time.time() - t:.3f} s")


def synthetic(side_m=400):
    rng = np.random.default_rng(0)
    n = int(side_m / 0.05) // 2 * 2
    V = np.full((n, n), 255, np.uint8); T = np.full((n, n), -1, np.int64); B = np.full((n, n), -1, np.int64)
    # a grid of rooms (6 x 6 m of free cells, walls, unknown between): ~12 % of the area known
    pitch, room = int(10 / 0.05), int(6 / 0.05)
    t0 = 1_700_000_000_000
    for y in range(0, n - pitch, pitch):
        for x in range(0, n - pitch, pitch):
            V[y:y + room, x:x + room] = 0; V[y:y + room, x] = 100; V[y, x:x + room] = 100
            T[y:y + room, x:x + room] = t0 + rng.integers(0, 3_600_000)
    known = int((V != 255).sum())
    H2 = n // 2
    plane = -np.ones((H2, H2), np.int32); plane[(V[::2, ::2] == 0)] = 0
    st = EngineState(params=dict(robot_half_side=0.25, lattice=0.1, place_mult=1.5, sight_range=5.0), cx0=0, cy0=0, V=V, T=T, B=B,
                     leaves=[LeafRecord("n%d" % i, "s", t0 + i * 1000, float(i), 0.0, 0.25) for i in range(5000)],
                     anchors=[(0, 0)], keys=[("n0", 0)], plane=plane, dep=np.zeros((H2, H2), bool),
                     rnd=np.where(plane >= 0, 1, 1 << 30).astype(np.int32), watermark=(t0, "n0"))

    class G:
        params = SceneParams(); export_state = staticmethod(lambda: st)
        def query(self, *a, **k): return ViewResult(1, [Chart("n0:0", (0.0, 0.0), 1, 0.01, 0.3, 0.0, 0.0, None)], [], None)
    b = MemoryBackend(); store = SceneGraphStore(b, b)
    t = time.time(); v = store.save("syn", G(), Dirty(None, [], {}, [], (t0, "n0")), None, {}); dt = time.time() - t
    man = store.manifest("syn")
    tb = sum(len(b.blobs["syn"]["g1/t/" + h]) for h in man["tiles"].values())
    raw = V.nbytes + T.nbytes + B.nbytes
    print(f"canvas {side_m} m: {n}x{n} cells, known {known} ({known / (n * n):.1%}); raw planes {raw / 1e6:.0f} MB "
          f"({raw / known:.0f} B/known cell); stored {tb / 1e6:.2f} MB in {len(man['tiles'])} tiles of {(n // 128) ** 2} "
          f"({tb / known:.2f} B/known cell); {len(b.list('syn'))} objects; first save {dt:.2f} s")
    # an incremental save: one room changes
    st.V[0:room, 0:room] = 100; st.T[0:room, 0:room] = t0 + 4_000_000
    before = set(b.list("syn")); nb = _stored(b, "syn")
    d = Dirty(BBox(0, 0, room * 0.05, room * 0.05), [], {}, [], (t0 + 4_000_000, "n1"))
    t = time.time(); v = store.save("syn", G(), d, v, {"i": 1}); dt = time.time() - t
    new = set(b.list("syn")) - before
    print(f"incremental save (one room): {len(new)} new objects, {_stored(b, 'syn') - nb} B, {dt:.3f} s")


def leaves(n=100_000):
    """save CPU with many leaf records: the first save packs every segment, later saves only the changed ones"""
    t0 = 1_700_000_000_000
    side = 20 / 0.05
    V = np.zeros((int(side), int(side)), np.uint8); T = np.full(V.shape, t0, np.int64); B = np.full(V.shape, -1, np.int64)
    recs = [LeafRecord("n%07d" % i, "s%d" % (i % 3), t0 + i * 1000, float(i % 400) * 0.05, float(i // 400) * 0.05, 0.25) for i in range(n)]
    H2 = V.shape[0] // 2
    plane = np.zeros((H2, H2), np.int32)
    st = EngineState(params=dict(robot_half_side=0.25, lattice=0.1, place_mult=1.5, sight_range=5.0), cx0=0, cy0=0, V=V, T=T, B=B,
                     leaves=recs, anchors=[(0, 0)], keys=[("n0", 0)], plane=plane, dep=np.zeros((H2, H2), bool),
                     rnd=np.ones((H2, H2), np.int32), watermark=(t0, "n0"))

    class G:
        params = SceneParams(); export_state = staticmethod(lambda: st)
        def query(self, *a, **k): return ViewResult(1, [Chart("n0:0", (0.0, 0.0), 1, 0.01, 0.3, 0.0, 0.0, None)], [], None)
    b = MemoryBackend(); store = SceneGraphStore(b, b, verify_every=0)
    c = time.process_time(); v = store.save("l", G(), Dirty(None, [], {}, [], (t0, "n0")), None, {}); c1 = time.process_time() - c
    nseg = len([k for k in b.list("l") if "/l/" in k])
    for step in (1, 2):
        import dataclasses
        st.leaves = [dataclasses.replace(r) for r in st.leaves] + [LeafRecord("z%07d" % (step * 100 + j), "s0", t0 + (n + step * 100 + j) * 1000, 0.0, 0.0, 0.25) for j in range(50)]
        before = set(b.list("l"))
        c = time.process_time(); v = store.save("l", G(), Dirty(None, [], {}, [], (t0 + (n + step * 100 + 49) * 1000, "z")), v, {"s": step})
        c2 = time.process_time() - c
        print(f"{n} leaves: first save {c1:.2f} cpu-s ({nseg} segments); +50 leaves: {c2:.3f} cpu-s, "
              f"{len(set(b.list('l')) - before)} new objects")


if __name__ == "__main__":
    if sys.argv[1] == "office8": office8()
    elif sys.argv[1] == "leaves": leaves(int(sys.argv[2]) if len(sys.argv) > 2 else 100_000)
    else: synthetic(int(sys.argv[2]) if len(sys.argv) > 2 else 400)
