"""SceneWorker against fakes only (no Arango / MinIO / Postgres / MQTT)."""
import datetime
import io

import numpy as np
import pytest
from PIL import Image

from packages.scene_graph.api import BBox, Dirty, MissingTiles
from packages.services.graph_builder import scene_worker as sw

RECEIPT = datetime.datetime(2026, 10, 12, 12, 0, 0)
MAP = "m1"


def _png(w=4, h=4):
    buf = io.BytesIO()
    Image.fromarray(np.zeros((h, w), np.uint8), mode="L").save(buf, format="PNG")
    return buf.getvalue()


def make_node(key, stored_ms, **over):
    n = {"_key": key, "session_id": "s", "timestamp": RECEIPT.isoformat(), "robot_name": "r1",
         "pose": {"x": 1.0, "y": 2.0, "yaw": 0.0}, "costmap_stored_ms": stored_ms,
         "costmap": {"occupancy": {"width": 4, "height": 4, "resolution": 0.05,
                                   "origin_map": {"x": 0.0, "y": 0.0, "yaw": 0.0}}}}
    n.update(over)
    return n


class FakeSource:
    def __init__(self, nodes=()):
        self.nodes = {n["_key"]: n for n in nodes}
        self.calls = []
        self.fail = False

    def add(self, node):
        self.nodes[node["_key"]] = node

    def set_scene_stamps(self, map_id, stamps):
        if getattr(self, "fail_stamps", False):
            raise RuntimeError("arango down")
        self.stamp_writes = getattr(self, "stamp_writes", [])
        self.stamp_writes.append(dict(stamps))
        for k, ms in stamps.items():
            self.nodes[k].setdefault("scene_stamp_ms", ms)     # first write wins

    def costmap_nodes_since(self, map_id, since_ms=0, after_key="", limit=200):
        self.calls.append((since_ms, after_key, limit))
        if self.fail:
            raise RuntimeError("arango down")
        rows = sorted((n for n in self.nodes.values() if n.get("costmap_stored_ms") is not None
                       and (n["costmap_stored_ms"], n["_key"]) > (since_ms, after_key)),
                      key=lambda n: (n["costmap_stored_ms"], n["_key"]))
        return rows[:limit]


class FakeEngine:
    version = 3

    def __init__(self):
        self.batches = []
        self.stamps = {}
        self.raise_once = None

    def fold_many(self, leaves):
        if self.raise_once is not None:
            exc, self.raise_once = self.raise_once, None
            raise exc
        self.batches.append([l.key for l in leaves])
        self.stamps.update({l.key: l.stamp_ms for l in leaves})
        return Dirty(bbox=BBox(0, 0, 1, 1), changed=[l.key for l in leaves], retired={}, links=[],
                     watermark=(max(l.stamp_ms for l in leaves), leaves[-1].key))


class FakeStore:
    def __init__(self, version=3, meta=None):
        self.v = version
        self.meta_doc = dict(meta or {})
        self.saves = []
        self.fail = 0
        self.fence = False

    def meta(self, map_id):
        return {"version": self.v, **self.meta_doc}

    def save(self, map_id, sg, dirty, *, expected_version, cursor):
        if self.fence or expected_version != self.v:
            raise sw.SaveFenced("expected %s, store has %s" % (expected_version, self.v))
        if self.fail:
            self.fail -= 1
            raise IOError("minio down")
        self.v += 1
        self.saves.append((self.v, cursor, list(dirty.changed)))
        self.meta_doc["cursor"] = cursor
        return self.v


class FakeGuard:
    def __init__(self):
        self.deleting = False
        self.locked = False
        self.available = True
        self.lost = False

    def acquire(self, map_id):
        self.locked = self.available
        return self.locked

    def held(self, map_id):
        return self.locked and not self.lost

    def release(self, map_id):
        self.locked = False

    def is_deleting(self, map_id):
        return self.deleting


class Clock:
    def __init__(self):
        self.t = 1_000_000

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += int(s * 1000)


class Rig:
    def __init__(self, nodes=(), cfg=None, store=None, cursor_store=None, pngs=None, **kw):
        self.source, self.engine = FakeSource(nodes), FakeEngine()
        self.store = store or FakeStore()
        self.guard, self.clock = FakeGuard(), Clock()
        self.cursors = cursor_store or sw.MemoryCursorStore()
        self.pngs = {} if pngs is None else pngs     # key -> bytes; missing key => not landed
        self.published = []
        self.cfg = cfg or sw.WorkerConfig(batch_size=100, replay_margin_ms=60_000, save_every_leaves=1,
                                          min_save_interval_s=0, save_when_idle=True)
        self.kw = kw
        self.worker = self.make()

    def fetch(self, map_id, key, layer):
        return self.pngs.get(key, _png()) if self.pngs is not None and key not in self.no_png else None

    no_png = frozenset()

    def make(self):
        return sw.SceneWorker(MAP, source=self.source, fetch_png=self.fetch,
                              footprint=lambda robot: (0.8, 0.5), engine=self.engine, store=self.store,
                              cursor_store=self.cursors, guard=self.guard, config=self.cfg,
                              now_ms=self.clock, publish=lambda v, d: self.published.append((v, d.changed)),
                              **self.kw)

    def drain(self, n=10):
        res = None
        for _ in range(n):
            res = self.worker.step()
            if res.state != sw.WORKED:
                break
        return res


def test_folds_saves_publishes_and_advances_cursor():
    r = Rig([make_node("a", 1000), make_node("b", 2000)])
    res = r.drain()
    assert res.state == sw.IDLE
    assert r.engine.batches == [["a", "b"]]
    assert r.store.saves == [(4, (2000, "b"), ["a", "b"])]
    assert r.published == [(4, ["a", "b"])]
    assert r.cursors.get(MAP) == (2000, "b") and r.worker.cursor == (2000, "b")
    assert r.worker.stats.folded == 2 and r.worker.stats.saves == 1 and r.worker.stats.version == 4


def test_late_landing_costmap_found_through_margin():
    r = Rig([make_node("a", 100_000)])
    r.drain()
    # a node whose costmap became visible late, stored_ms below the committed cursor but within margin
    r.source.add(make_node("late", 80_000))
    r.drain()
    assert r.engine.batches == [["a"], ["late"]]
    # outside the margin (60 s) it is not found
    r.source.add(make_node("too_late", 10_000))
    r.drain()
    assert r.engine.batches == [["a"], ["late"]]


def test_png_not_landed_is_retried_then_folded():
    r = Rig([make_node("a", 1000), make_node("b", 2000)])
    r.no_png = frozenset({"b"})
    r.drain()
    assert r.engine.batches == [["a"]]
    assert r.worker.stats.pending == 1
    r.no_png = frozenset()
    r.drain()
    assert r.engine.batches == [["a"], ["b"]]
    assert r.worker.stats.pending == 0


def test_png_never_lands_expires_after_margin():
    r = Rig([make_node("a", 1000), make_node("b", 2000)])
    r.no_png = frozenset({"a"})
    r.drain()
    r.source.add(make_node("c", 200_000))
    r.drain()
    assert r.worker.stats.skipped[sw.SKIP_PNG_EXPIRED] == 1
    assert r.worker.stats.pending == 0


def test_duplicate_delivery_does_not_double_count():
    r = Rig([make_node("a", 1000)])
    r.drain()
    for _ in range(3):
        r.worker.step()
    assert r.engine.batches == [["a"]]
    assert r.worker.stats.folded == 1
    assert r.worker.stats.duplicates >= 1
    assert len(r.store.saves) == 1


def test_cursor_not_advanced_when_save_fails_and_retried():
    r = Rig([make_node("a", 1000)], cfg=sw.WorkerConfig(save_every_leaves=100, min_save_interval_s=0))
    r.store.fail = 1
    res = r.worker.step()           # fold
    res = r.worker.step()           # idle save fails
    assert res.state == sw.BACKOFF
    assert r.worker.cursor == sw.ZERO_CURSOR and r.cursors.get(MAP) is None
    assert r.worker.stats.save_failures == 1
    r.clock.advance(1)
    res = r.drain()
    assert r.store.saves and r.worker.cursor == (1000, "a")
    assert r.engine.batches == [["a"]]      # folded once, saved on retry
    assert r.published and r.published[0][1] == ["a"]


def test_deleting_map_no_fold_no_save():
    r = Rig([make_node("a", 1000)])
    r.guard.deleting = True
    res = r.worker.step()
    assert res.state == sw.DELETING
    assert r.engine.batches == [] and r.store.saves == [] and r.source.calls == []
    assert not r.guard.locked
    assert r.worker.step().state == sw.DELETING


def test_deleting_between_fold_and_save_does_not_save():
    r = Rig([make_node("a", 1000)], cfg=sw.WorkerConfig(save_every_leaves=100, min_save_interval_s=0,
                                                         save_when_idle=True))
    r.worker.step()
    r.guard.deleting = True
    res = r.worker.step()
    assert res.state == sw.DELETING and r.store.saves == []


def test_fenced_save_stops_worker():
    r = Rig([make_node("a", 1000)])
    r.store.fence = True
    res = r.drain()
    assert res.state == sw.FENCED
    assert r.worker.cursor == sw.ZERO_CURSOR and r.cursors.get(MAP) is None
    r.source.add(make_node("b", 2000))
    assert r.worker.step().state == sw.FENCED
    assert r.engine.batches == [["a"]]
    assert not r.guard.locked


def test_stale_expected_version_is_fenced():
    r = Rig([make_node("a", 1000)], cfg=sw.WorkerConfig(save_every_leaves=100, min_save_interval_s=0))
    r.worker.step()
    r.store.v += 5        # another writer got in
    assert r.drain().state == sw.FENCED


def test_lost_lock_is_fenced():
    r = Rig([make_node("a", 1000)], cfg=sw.WorkerConfig(save_every_leaves=100, min_save_interval_s=0))
    r.worker.step()
    r.guard.lost = True
    assert r.worker.step().state == sw.FENCED
    assert r.store.saves == []


def test_not_owner_when_lock_taken():
    r = Rig([make_node("a", 1000)])
    r.guard.available = False
    res = r.worker.step()
    assert res.state == sw.NOT_OWNER and r.source.calls == []


def test_saves_rate_limited():
    cfg = sw.WorkerConfig(batch_size=1, save_every_leaves=1, min_save_interval_s=10, save_every_s=1000)
    r = Rig([make_node(k, i * 1000) for i, k in enumerate("abcd", 1)], cfg=cfg)
    for _ in range(6):
        r.worker.step()
    assert len(r.store.saves) == 1          # first save, then held back by the floor
    assert r.worker.stats.unsaved_leaves == 3
    assert r.worker.stats.folded == 4
    r.clock.advance(11)
    r.worker.step()
    assert len(r.store.saves) == 2
    assert r.store.saves[-1][2] == ["b", "c", "d"]      # merged dirty of the held-back batches
    assert r.worker.cursor == (4000, "d")


def test_save_by_leaf_count_and_by_time():
    cfg = sw.WorkerConfig(batch_size=2, save_every_leaves=3, save_every_s=30, min_save_interval_s=0,
                          save_when_idle=False)
    r = Rig([make_node(k, i * 1000) for i, k in enumerate("abcd", 1)], cfg=cfg)
    r.drain()
    assert len(r.store.saves) == 1 and r.worker.stats.unsaved_leaves == 0     # 4 folded >= 3
    r.source.add(make_node("e", 9000))
    r.drain()
    assert len(r.store.saves) == 1          # 1 unsaved, not idle-saving, not old enough
    r.clock.advance(31)
    r.drain()
    assert len(r.store.saves) == 2


def test_skip_reasons_counted():
    bad_pose = make_node("nopose", 1000, pose={})
    no_layer = make_node("nolayer", 2000, costmap={})
    r = Rig([bad_pose, no_layer, make_node("badpng", 3000), make_node("ok", 4000)],
            pngs={"badpng": b"garbage"})
    r.drain()
    assert dict(r.worker.stats.skipped) == {"no_pose": 1, "no_occupancy_layer": 1, "bad_png": 1}
    assert r.engine.batches == [["ok"]]
    assert r.worker.cursor == (4000, "ok")


def test_unknown_footprint_counted_and_cached():
    calls = []

    def fp(robot):
        calls.append(robot)
        return -1, -1
    r = Rig([make_node("a", 1000), make_node("b", 2000)])
    r.worker.footprint = fp
    r.drain()
    assert r.worker.stats.default_r_d == 2
    assert calls == ["r1"]


def test_restart_resumes_from_persisted_cursor():
    r = Rig([make_node("a", 1000), make_node("b", 2000)])
    r.drain()
    nodes = list(r.source.nodes.values()) + [make_node("c", 300_000)]
    r2 = Rig(nodes, store=r.store, cursor_store=r.cursors)
    r2.engine.version = r.store.v
    r2.drain()
    assert r2.source.calls[0][0] == 0          # cursor 2000 ms minus the 60 s margin, floored
    assert r2.worker.cursor == (300_000, "c")
    # a/b are within the margin of the cursor (2000 ms < 60 s), so they replay: fold is idempotent.
    far = Rig(nodes, store=FakeStore(), cursor_store=sw.MemoryCursorStore())
    far.cursors.put(MAP, (500_000, "z"))
    far.source.add(make_node("d", 600_000))
    far.drain()
    assert far.engine.batches == [["d"]]
    assert far.source.calls[0][0] == 500_000 - 60_000


def test_restart_takes_lower_of_cursor_store_and_meta():
    store = FakeStore(meta={"cursor": (1000, "a")})
    cs = sw.MemoryCursorStore()
    cs.put(MAP, (900_000, "q"))
    r = Rig([make_node("n", 1500)], store=store, cursor_store=cs)
    r.drain()
    assert r.engine.batches == [["n"]]


def test_batch_bound_respected_and_drains():
    nodes = [make_node("n%02d" % i, 1000 + i) for i in range(25)]
    cfg = sw.WorkerConfig(batch_size=10, save_every_leaves=1000, min_save_interval_s=0, save_when_idle=True)
    r = Rig(nodes, cfg=cfg)
    r.drain(n=20)
    assert [len(b) for b in r.engine.batches] == [10, 10, 5]
    assert all(c[2] == 10 for c in r.source.calls)
    assert r.worker.cursor == (1024, "n24")
    assert len(r.store.saves) == 1


def test_params_hash_mismatch_needs_rebuild():
    seen = []
    store = FakeStore(meta={"params_hash": "old", "generation": 1})
    cfg = sw.WorkerConfig(expected_params_hash="new")
    r = Rig([make_node("a", 1000)], store=store, cfg=cfg, on_needs_rebuild=seen.append)
    res = r.worker.step()
    assert res.state == sw.NEEDS_REBUILD and seen == ["params_hash"]
    assert r.engine.batches == [] and r.store.saves == []


def test_generation_mismatch_needs_rebuild():
    store = FakeStore(meta={"params_hash": "h", "generation": 1})
    cfg = sw.WorkerConfig(expected_params_hash="h", expected_generation=2)
    r = Rig([make_node("a", 1000)], store=store, cfg=cfg)
    assert r.worker.step().state == sw.NEEDS_REBUILD


def test_missing_tiles_loaded_and_retried_once():
    loaded = []
    r = Rig([make_node("a", 1000)], load_tiles=lambda sg, tiles: loaded.append(list(tiles)))
    r.engine.raise_once = MissingTiles([(0, 0)])
    r.drain()
    assert loaded == [[(0, 0)]]
    assert r.engine.batches == [["a"]] and r.worker.stats.missing_tiles == 1


def test_missing_tiles_twice_backs_off_without_advancing():
    class Always(FakeEngine):
        def fold_many(self, leaves):
            raise MissingTiles([(1, 1)])
    r = Rig([make_node("a", 1000)], load_tiles=lambda sg, t: None)
    r.engine = Always()
    r.worker = r.make()
    res = r.worker.step()
    assert res.state == sw.BACKOFF and res.sleep_s > 0
    assert r.worker.cursor == sw.ZERO_CURSOR and r.store.saves == []


def test_source_error_backs_off():
    r = Rig([make_node("a", 1000)])
    r.source.fail = True
    res = r.worker.step()
    assert res.state == sw.BACKOFF and r.worker.stats.source_errors == 1
    r.source.fail = False
    r.drain()
    assert r.engine.batches == [["a"]]


def test_idle_state_sleeps():
    r = Rig([])
    res = r.worker.step()
    assert res.state == sw.IDLE and res.sleep_s == r.cfg.idle_sleep_s


def test_merge_dirty():
    a = Dirty(BBox(0, 0, 1, 1), ["x"], {"k": ["m"]}, [("a", "b")], (5, "x"))
    b = Dirty(BBox(-1, 0, 0.5, 3), ["x", "y"], {}, [("a", "b"), ("b", "c")], (4, "y"))
    m = sw.merge_dirty(a, b)
    assert m.bbox == BBox(-1, 0, 1, 3) and m.changed == ["x", "y"]
    assert m.links == [("a", "b"), ("b", "c")] and m.watermark == (5, "x") and m.retired == {"k": ["m"]}


def test_session_clock_corrects_an_ahead_robot_stamp():
    base = int(RECEIPT.timestamp() * 1000)
    ahead = 120_000                          # inside the 300 s sanity window: the per-leaf rule accepts it
    nodes = []
    for i in range(5):
        n = make_node(f"n{i}", base + i * 1000)
        n["costmap"]["occupancy"]["costmap_stamp_ms"] = base + i * 1000 + ahead
        nodes.append(n)
    r = Rig(nodes)
    r.drain()
    assert set(r.engine.stamps) == {f"n{i}" for i in range(5)}
    for i in range(5):
        assert r.engine.stamps[f"n{i}"] == base + i * 1000     # never later than its receipt
    assert r.worker.clock.offset_ms("s") == -ahead


def test_first_fold_stamp_is_kept_on_the_node_and_reused():
    base = int(RECEIPT.timestamp() * 1000)
    r = Rig([make_node("a", base)])
    r.drain()
    kept = r.source.nodes["a"]["scene_stamp_ms"]
    assert r.engine.stamps["a"] == kept and r.source.stamp_writes == [{"a": kept}]
    # the node is re-read (margin / restart) and folded again: same stamp, no second write
    r.source.nodes["a"]["costmap_stored_ms"] = base + 5     # a re-stored costmap
    r.drain()
    assert r.engine.stamps["a"] == kept
    assert r.source.stamp_writes == [{"a": kept}]


def test_stamp_write_failure_means_no_fold():
    r = Rig([make_node("a", int(RECEIPT.timestamp() * 1000))])
    r.source.fail_stamps = True
    res = r.worker.step()
    assert res.state == sw.BACKOFF and r.engine.batches == []
    assert r.worker.cursor == (0, "")
