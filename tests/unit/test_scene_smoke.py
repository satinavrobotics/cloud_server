"""Adapter + worker against the real chart engine (packages/scene_graph/engine.py), in-memory store.

Nodes are built from CIMBI's synthetic worlds, pushed through the production path (PNG decode, stamp rule,
to_leaf, batching, fold, save) and the result is compared with folding the original leaves directly.
"""
import datetime
import importlib.util
import io
import time
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from packages.scene_graph.api import BBox, SceneParams
from packages.services.graph_builder import scene_worker as sw

HERE = Path(__file__).resolve().parent
try:                                     # CIMBI's engine and worlds; absent in a checkout without them
    from packages.scene_graph.engine import ChartGraph
    _spec = importlib.util.spec_from_file_location("chart_worlds_smoke", HERE / "scene_graph" / "chart_worlds.py")
    cw = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(cw)
except (ImportError, FileNotFoundError, AttributeError):
    pytest.skip("scene graph engine / chart_worlds not available", allow_module_level=True)

T0 = 1_760_000_000_000          # a plausible epoch ms; leaves are 1 s apart
ALL = BBox(-5, -5, 30, 30)


def png(arr):
    buf = io.BytesIO()
    Image.fromarray(arr, mode="L").save(buf, format="PNG")
    return buf.getvalue()


def node_of(leaf, i):
    """The stored document the graph-builder would hold for `leaf`."""
    recv = leaf.stamp_ms + 500
    h, w = leaf.window_u8.shape
    return {"_key": leaf.key, "session_id": leaf.session, "robot_name": "r1",
            "timestamp": datetime.datetime.fromtimestamp(recv / 1000, datetime.timezone.utc).replace(tzinfo=None).isoformat(),
            "pose": {"x": leaf.x, "y": leaf.y, "yaw": leaf.yaw}, "costmap_stored_ms": recv,
            "costmap": {"occupancy": {"width": w, "height": h, "resolution": leaf.resolution,
                                      "costmap_stamp_ms": leaf.stamp_ms,
                                      "origin_map": {"x": leaf.origin[0], "y": leaf.origin[1], "yaw": leaf.origin_yaw}}}}


class Recording(ChartGraph):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.seen, self.batch_times = [], []

    def fold_many(self, leaves):
        self.seen.extend(leaves)
        t = time.time()
        try:
            return super().fold_many(leaves)
        finally:
            self.batch_times.append(time.time() - t)


class Source:
    def __init__(self, nodes):
        self.nodes = {n["_key"]: n for n in nodes}

    def set_scene_stamps(self, map_id, stamps):
        for k, ms in stamps.items():
            self.nodes[k].setdefault("scene_stamp_ms", ms)

    def costmap_nodes_since(self, map_id, since_ms=0, after_key="", limit=200):
        rows = sorted((n for n in self.nodes.values() if (n["costmap_stored_ms"], n["_key"]) > (since_ms, after_key)),
                      key=lambda n: (n["costmap_stored_ms"], n["_key"]))
        return rows[:limit]


class Store:
    def __init__(self):
        self.v, self.saves = 0, 0

    def meta(self, map_id):
        return {"version": self.v}

    def save(self, map_id, sg, dirty, *, expected_version, cursor):
        assert expected_version == self.v
        self.v += 1
        self.saves += 1
        return self.v


class Guard:
    def acquire(self, m): return True
    def held(self, m): return True
    def release(self, m): pass
    def is_deleting(self, m): return False


def run_worker(leaves, batch=10, params=SceneParams()):
    nodes = [node_of(l, i) for i, l in enumerate(leaves)]
    src, eng, store = Source(nodes), Recording(params), Store()
    clock = [T0 + 10_000_000]
    worker = sw.SceneWorker(
        "m", source=src, fetch_png=lambda m, k, layer: png(next(l for l in leaves if l.key == k).window_u8),
        footprint=lambda robot: (0.8, 0.5), engine=eng, store=store, cursor_store=sw.MemoryCursorStore(),
        guard=Guard(), config=sw.WorkerConfig(batch_size=batch, replay_margin_ms=60_000, save_every_leaves=1,
                                              min_save_interval_s=0, save_when_idle=True),
        now_ms=lambda: clock[0])
    for _ in range(500):
        res = worker.step()
        if res.state == sw.IDLE:
            break
        assert res.state in (sw.WORKED, sw.IDLE), res
    return worker, eng, store, src


def direct(leaves, batch=10):
    g = ChartGraph(SceneParams())
    for i in range(0, len(leaves), batch):
        g.fold_many(leaves[i:i + batch])
    return g


def shape(g):
    v = g.query(ALL, raster=False)
    return len(v.charts), len(v.links), round(sum(c.area_m2 for c in v.charts), 1)


def leaves_of(world_fn, **kw):
    world, way = world_fn()
    return cw.drive(world, way, t0=T0, **kw)


@pytest.mark.parametrize("name", ["two_rooms", "corridor", "pillar_room", "ring"])
def test_worker_path_builds_the_same_graph_as_folding_the_leaves(name):
    leaves = leaves_of(getattr(cw, name))
    worker, eng, store, _ = run_worker(leaves)
    assert worker.stats.skipped == {} or not any(worker.stats.skipped.values())
    assert sorted(l.key for l in eng.seen) == sorted(l.key for l in leaves)
    assert store.saves >= 1 and worker.cursor[1] != ""
    assert shape(eng) == shape(direct(leaves))


def test_adapter_hands_the_engine_the_original_leaves():
    leaves = leaves_of(cw.two_rooms)
    _, eng, _, _ = run_worker(leaves)
    orig = {l.key: l for l in leaves}
    for got in eng.seen:
        want = orig[got.key]
        assert got.stamp_ms - want.stamp_ms == 500    # capture + the session's minimum transit delay
        assert (got.x, got.y, got.yaw) == (want.x, want.y, want.yaw)
        assert got.origin == pytest.approx(want.origin) and got.resolution == want.resolution
        assert got.origin_yaw == want.origin_yaw
        assert got.r_d == pytest.approx(0.25)           # smaller side 0.5 / 2
        assert got.session == want.session
        np.testing.assert_array_equal(got.window_u8, want.window_u8)


def test_rotated_windows_give_the_same_structure():
    world, way = cw.two_rooms()
    base = cw.drive(world, way, t0=T0)
    rot = [cw.rotated(world, l, 30.0) for l in base]
    _, eng, _, _ = run_worker(rot)
    ref = shape(direct(base))
    got = shape(eng)
    assert got[0] == ref[0] and got[1] == ref[1]
    assert abs(got[2] - ref[2]) / ref[2] < 0.1


def test_replay_and_restart_are_idempotent():
    leaves = leaves_of(cw.pillar_room)
    worker, eng, store, src = run_worker(leaves)
    before = shape(eng)
    # a replay of everything (cursor margin covers it): fold is idempotent, structure unchanged
    worker2 = sw.SceneWorker(
        "m", source=src, fetch_png=lambda m, k, layer: png(next(l for l in leaves if l.key == k).window_u8),
        footprint=lambda robot: (0.8, 0.5), engine=eng, store=store, cursor_store=sw.MemoryCursorStore(),
        guard=Guard(), config=sw.WorkerConfig(batch_size=10, replay_margin_ms=10 ** 12, save_every_leaves=1,
                                              min_save_interval_s=0, save_when_idle=True),
        now_ms=lambda: T0)
    for _ in range(100):
        if worker2.step().state == sw.IDLE:
            break
    assert shape(eng) == before


def test_stored_stamps_are_written_and_reused():
    leaves = leaves_of(cw.corridor)
    _, _, _, src = run_worker(leaves)
    stamped = {k: n.get("scene_stamp_ms") for k, n in src.nodes.items()}
    # the first MIN_OBS leaves of a session are folded under the receipt time while the estimator is
    # not trusted yet; that provisional stamp is not kept on the node (a replay may do better)
    first = [l.key for l in leaves[:2]]
    assert [k for k, v in stamped.items() if v is None] == first
    assert all(v is not None for k, v in stamped.items() if k not in first)
    # the per-session estimator folds the constant 500 ms simulated transit delay into the stamp:
    # the shift is one constant, so spacing and order are those of the capture times
    shifts = {stamped[l.key] - l.stamp_ms for l in leaves if stamped[l.key] is not None}
    assert shifts == {500}


def test_batch_timing_is_reported_through_the_worker_path():
    leaves = leaves_of(cw.two_rooms) + leaves_of(cw.ring)[:0]
    t = time.time()
    _, eng, _, _ = run_worker(leaves, batch=6)
    total = time.time() - t
    assert len(eng.batch_times) >= len(leaves) // 6
    print(f"\nsmoke: {len(leaves)} leaves, {len(eng.batch_times)} batches, fold {sum(eng.batch_times):.2f} s, "
          f"wall {total:.2f} s, adapter+worker overhead {total - sum(eng.batch_times):.2f} s")
