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

    pending = None
    commits = []

    def begin_generation(self, map_id):
        self.pending = self.meta_doc.get("generation", 1) + 1
        return self.pending

    def commit_generation(self, map_id, generation):
        assert generation == self.pending and self.saves, "commit needs a begun generation with a saved state"
        self.v += 1
        self.meta_doc.update(generation=generation, params_hash=self.new_hash)
        self.commits = self.commits + [(generation, self.v)]
        return self.v, {"old:1": ["new:1"]}

    new_hash = "new"

    def save(self, map_id, sg, dirty, *, expected_version, cursor, generation=None):
        self.save_generations = getattr(self, "save_generations", []) + [generation]
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
    r.clock.advance(61)                     # the full margin is re-read every replay_recheck_s
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
    r.clock.advance(3)                      # png_retry_s (2 s) of wall clock
    r.drain()
    assert r.engine.batches == [["a"], ["b"]]
    assert r.worker.stats.pending == 0


def test_png_never_lands_expires_by_wall_clock_not_by_cursor_distance():
    r = Rig([make_node("a", 1000), make_node("b", 2000)])
    r.no_png = frozenset({"a"})
    r.drain()
    # the cursor moves 200 s of stored time in no wall time: "a" must still be waited for
    r.source.add(make_node("c", 200_000))
    r.drain()
    assert r.worker.stats.skipped[sw.SKIP_PNG_EXPIRED] == 0
    assert r.worker.stats.pending == 1
    assert r.worker.cursor[0] < 1001            # the persisted cursor stays below the missing node
    r.clock.advance(61)                         # the replay margin of wall clock
    r.drain()
    assert r.worker.stats.skipped[sw.SKIP_PNG_EXPIRED] == 1
    assert r.worker.stats.pending == 0
    assert r.worker.cursor == (200_000, "c")


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


# ---------------------------------------------------------------- batching, budget, rebuild, events

def _nodes(n, start=1000):
    return [make_node("n%03d" % i, start + i) for i in range(n)]


def test_trickle_below_min_waits_for_the_publish_interval_then_folds():
    cfg = sw.WorkerConfig(batch_min_leaves=6, batch_max_leaves=48, min_save_interval_s=5,
                          save_when_idle=True)
    r = Rig(_nodes(2), cfg=cfg)
    res = r.worker.step()
    assert r.engine.batches == [] and res.state == sw.IDLE
    assert 0 < res.sleep_s <= 2.0
    r.clock.advance(3)
    r.worker.step()
    assert r.engine.batches == []              # still inside the wait
    assert r.worker.cursor == sw.ZERO_CURSOR   # nothing folded: the cursor must not pass buffered leaves
    r.clock.advance(3)
    r.drain()
    assert r.engine.batches == [["n000", "n001"]]
    assert r.worker.cursor == (1001, "n001")


def test_min_reached_folds_at_once():
    cfg = sw.WorkerConfig(batch_min_leaves=6, batch_max_leaves=48, min_save_interval_s=5)
    r = Rig(_nodes(6), cfg=cfg)
    r.worker.step()
    assert len(r.engine.batches) == 1 and len(r.engine.batches[0]) == 6


def test_backlog_is_coalesced_up_to_max_in_one_fold():
    # a slow fold: 100 leaves arrive while one fold runs; the next folds take batch_max at a time
    cfg = sw.WorkerConfig(batch_size=200, batch_min_leaves=6, batch_max_leaves=48, min_save_interval_s=5,
                          save_every_leaves=1000)
    r = Rig(_nodes(7), cfg=cfg)
    r.worker.step()
    assert [len(b) for b in r.engine.batches] == [7]
    for n in _nodes(100, start=5000):
        r.source.add(n)
    r.drain(n=10)
    assert [len(b) for b in r.engine.batches] == [7, 48, 48]      # no small batches while backlogged
    assert r.worker.stats.buffered_leaves == 4                     # the tail is a trickle: it waits
    r.clock.advance(6)
    r.drain()
    assert [len(b) for b in r.engine.batches] == [7, 48, 48, 4]
    assert r.worker.cursor == (5099, "n099")


def test_fold_failure_keeps_buffered_leaves_and_retries():
    cfg = sw.WorkerConfig(batch_min_leaves=2, batch_max_leaves=10, min_save_interval_s=0)
    r = Rig(_nodes(3), cfg=cfg)
    r.engine.raise_once = MemoryError("allocation")      # nothing changed: the engine is kept, the batch retried
    engine = r.engine
    assert r.worker.step().state == sw.BACKOFF
    assert r.worker.cursor == sw.ZERO_CURSOR and r.worker.engine is engine
    r.drain()
    assert r.engine.batches == [["n000", "n001", "n002"]]


def test_fold_fault_drops_the_engine_and_reloads_it_from_the_store():
    loads = []
    reloaded = FakeEngine()

    def factory(version):
        loads.append(version)
        return reloaded
    cfg = sw.WorkerConfig(batch_min_leaves=2, batch_max_leaves=10, min_save_interval_s=0)
    r = Rig(_nodes(3), cfg=cfg, engine_factory=factory)
    first = r.engine
    first.raise_once = RuntimeError("solver")
    assert r.worker.step().state == sw.BACKOFF
    assert r.worker.engine is reloaded and loads == [r.store.v]      # the live version the store holds
    assert r.worker.cursor == sw.ZERO_CURSOR
    r.drain()
    assert first.batches == [] and reloaded.batches == [["n000", "n001", "n002"]]


def test_slow_fold_logs_a_warning_with_map_leaves_seconds(caplog):
    cfg = sw.WorkerConfig(batch_budget_s=10.0)
    r = Rig(_nodes(3), cfg=cfg)
    real = r.engine.fold_many

    def slow(leaves):
        r.clock.advance(12.5)
        return real(leaves)
    r.engine.fold_many = slow
    with caplog.at_level("WARNING"):
        r.worker.step()
    msg = [m for m in caplog.messages if "budget" in m]
    assert msg and MAP in msg[0] and "3 leaves" in msg[0] and "12.5s" in msg[0]
    assert r.worker.stats.slow_folds == 1 and r.worker.stats.max_fold_s == 12.5
    r.engine.fold_many = real
    caplog.clear()
    r.source.add(make_node("x", 9000))
    r.worker.step()
    assert not [m for m in caplog.messages if "budget" in m]


def test_save_passes_the_stored_generation():
    r = Rig([make_node("a", 1000)], store=FakeStore(meta={"params_hash": "h", "generation": 3}))
    r.drain()
    assert r.store.save_generations == [3]


def test_store_stale_version_is_fenced_not_retried():
    class Stale(Exception):
        pass

    class S(FakeStore):
        def save(self, *a, **k):
            raise Stale("expected 3, stored 5")
    r = Rig([make_node("a", 1000)], store=S(), fence_errors=(Stale,))
    assert r.drain().state == sw.FENCED


def test_params_hash_change_rebuilds_in_a_new_generation_then_commits():
    events = []
    store = FakeStore(version=5, meta={"params_hash": "old", "generation": 1, "cursor": (9_000_000, "z")})
    cfg = sw.WorkerConfig(expected_params_hash="new", min_save_interval_s=0)
    fresh = FakeEngine()
    fresh.version = 0
    r = Rig(_nodes(3), store=store, cfg=cfg, new_engine=lambda: fresh,
            on_event=lambda k, p: events.append((k, p)))
    r.engine.version = 99                      # the old engine is ignored (hash mismatch)
    r.worker = r.make()
    r.drain()
    assert r.engine.batches == [] and fresh.batches == [["n000", "n001", "n002"]]   # all leaves, from the start
    assert store.save_generations == [2]       # saved into the pending generation
    assert store.commits == [(2, store.v)]
    assert r.worker.state == "" and r.worker._rebuild_gen is None
    kinds = [k for k, _ in events]
    assert kinds[0] == "rebuild_started" and "rebuild_completed" in kinds
    done = dict(events)["rebuild_completed"]
    assert done["version"] == store.v and done["reason"] == "needs_rebuild" and done["duration_s"] >= 0
    assert "updated" in kinds
    # the publish after the switch is a full refetch hint carrying the lineage
    assert r.published and r.published[-1][0] == store.v
    # later saves go to the new current generation
    r.source.add(make_node("late", 5_000))
    r.drain()
    assert store.save_generations[-1] == 2 and len(store.commits) == 1
    assert fresh.batches[-1] == ["late"]


def test_params_hash_change_without_rebuild_support_still_ends_in_needs_rebuild():
    store = FakeStore(meta={"params_hash": "old", "generation": 1})
    r = Rig([make_node("a", 1000)], store=store, cfg=sw.WorkerConfig(expected_params_hash="new"))
    assert r.worker.step().state == sw.NEEDS_REBUILD


def test_matching_params_hash_does_not_rebuild():
    store = FakeStore(meta={"params_hash": "same", "generation": 1})
    r = Rig([make_node("a", 1000)], store=store, cfg=sw.WorkerConfig(expected_params_hash="same"),
            new_engine=lambda: FakeEngine())
    r.drain()
    assert r.engine.batches == [["a"]] and store.pending is None


def test_events_updated_and_fold_failed_with_consecutive_count():
    events = []
    cfg = sw.WorkerConfig(min_save_interval_s=0)
    r = Rig(_nodes(1), cfg=cfg, on_event=lambda k, p: events.append((k, p)))   # one leaf: no bisect
    real = r.engine.fold_many
    state = {"n": 0}

    def flaky(leaves):
        state["n"] += 1
        if state["n"] <= 3:
            raise MemoryError("solver blew up")
        return real(leaves)
    r.engine.fold_many = flaky
    for _ in range(3):
        assert r.worker.step().state == sw.BACKOFF
    failed = [p for k, p in events if k == "fold_failed"]
    assert [p["consecutive"] for p in failed] == [1, 2]          # 1, 2, (3 held back), 4, 8 ...
    assert failed[0]["stage"] == "fold" and "solver blew up" in failed[0]["detail"]   # a MemoryError: nothing changed
    r.drain()
    upd = [p for k, p in events if k == "updated"]
    assert len(upd) == 1 and upd[0]["leaves_folded"] == 1 and upd[0]["charts_changed"] == 1
    assert r.worker.stats.fold_failures == 0


def test_save_failure_is_reported_as_fold_failed_stage_save():
    events = []
    r = Rig([make_node("a", 1000)], on_event=lambda k, p: events.append((k, p)))
    r.store.fail = 1
    r.drain()
    assert ("fold_failed", {"stage": "save", "detail": "OSError: minio down", "consecutive": 1}) in events


# ---------------------------------------------------------------- needs-rebuild request (node delete)

class RequestGuard(FakeGuard):
    """FakeGuard + the status key `scene_graph_needs_rebuild` with compare-and-clear."""

    def __init__(self):
        super().__init__()
        self.request = None
        self.lookups = 0
        self.clears = []
        self.fail_clear = False

    def rebuild_request(self, map_id):
        self.lookups += 1
        return dict(self.request) if self.request else None

    def clear_rebuild_request(self, map_id, at):
        if self.fail_clear:
            raise RuntimeError("pg down")
        self.clears.append(at)
        if self.request and self.request["at"] == at:
            self.request = None
            return True
        return False


def _request_rig(nodes=(), request=None, version=5, check_s=30.0, **kw):
    events = []
    store = FakeStore(version=version, meta={"params_hash": "same", "generation": 1})
    cfg = sw.WorkerConfig(expected_params_hash="same", min_save_interval_s=0, rebuild_check_s=check_s,
                          rebuild_debounce_s=0)
    fresh = FakeEngine()
    fresh.version = 0
    engines = [fresh]
    r = Rig(nodes, store=store, cfg=cfg, new_engine=lambda: engines.pop(0) if engines else FakeEngine(),
            on_event=lambda k, p: events.append((k, p)), **kw)
    r.guard = RequestGuard()
    r.guard.request = request
    r.engine.version = version
    r.worker = r.make()
    r.events, r.fresh = events, fresh
    return r


REQ = {"at": "2026-10-12T10:00:00+00:00", "reason": "nodes_deleted", "nodes": 2}


def test_request_at_start_rebuilds_into_a_new_generation_and_clears_the_key_at_commit():
    r = _request_rig(_nodes(3), request=REQ)
    r.drain()
    assert r.engine.batches == [] and r.fresh.batches == [["n000", "n001", "n002"]]   # every leaf, new engine
    assert r.store.save_generations == [2] and r.store.commits == [(2, r.store.v)]
    assert r.guard.clears == [REQ["at"]] and r.guard.request is None
    kinds = dict(r.events)
    assert kinds["rebuild_started"] == {"reason": "nodes_deleted"}
    assert kinds["rebuild_completed"]["reason"] == "nodes_deleted"
    assert kinds["rebuild_completed"]["version"] == r.store.v


def test_key_is_not_cleared_before_the_generation_commits():
    r = _request_rig(_nodes(3), request=REQ)
    r.worker.step()                      # starts the rebuild, folds, saves into the pending generation
    assert r.worker._rebuild_gen is not None or r.store.commits
    if not r.store.commits:
        assert r.guard.request is not None and r.guard.clears == []
    r.drain()
    assert r.guard.request is None


def test_newer_delete_during_the_rebuild_keeps_the_key_and_triggers_another_rebuild():
    r = _request_rig(_nodes(3), request=REQ, check_s=1.0)
    r.worker.step()                      # rebuild begun
    newer = {"at": "2026-10-12T10:00:30+00:00", "reason": "nodes_deleted", "nodes": 1}
    r.guard.request = newer              # a delete lands while rebuilding
    r.drain()
    assert r.store.commits and r.guard.clears == [REQ["at"]]
    assert r.guard.request == newer      # the compare did not match: the newer request survived
    r.clock.advance(2)                   # past the check interval: the next poll sees it
    r.drain()
    assert len(r.store.commits) == 2 and r.guard.request is None
    started = [p for k, p in r.events if k == "rebuild_started"]
    assert len(started) == 2


def test_request_mid_run_after_check_interval_starts_a_rebuild_and_forgets_old_state():
    r = _request_rig(_nodes(2), check_s=30.0)
    r.drain()
    assert r.engine.batches == [["n000", "n001"]] and not r.store.commits
    lookups = r.guard.lookups
    r.worker.step()
    assert r.guard.lookups == lookups            # within the interval: no query per step
    r.guard.request = REQ
    r.clock.advance(31)
    r.drain()
    assert r.fresh.batches == [["n000", "n001"]]  # read again from the start
    assert r.store.commits and r.guard.request is None
    assert r.worker.cursor > sw.ZERO_CURSOR


def test_check_is_rate_limited_per_map():
    r = _request_rig(_nodes(1), check_s=30.0)
    r.drain()
    base = r.guard.lookups                        # 1 at start
    for _ in range(20):
        r.worker.step()
    assert r.guard.lookups == base
    r.clock.advance(31)
    r.worker.step()
    assert r.guard.lookups == base + 1


def test_never_saved_graph_just_restarts_the_fold_and_clears():
    r = _request_rig([], request=None, version=0)
    r.engine.version = 0
    r.store.v = 0
    r.worker = r.make()
    r.worker.step()                      # started, idle
    r.guard.request = REQ
    r.clock.advance(31)
    r.worker.step()
    assert r.guard.request is None and r.store.pending is None   # no generation for an empty graph
    assert r.worker.engine is r.fresh


def test_params_rebuild_also_answers_the_request():
    events = []
    store = FakeStore(version=5, meta={"params_hash": "old", "generation": 1})
    fresh = FakeEngine()
    fresh.version = 0
    r = Rig(_nodes(2), store=store, cfg=sw.WorkerConfig(expected_params_hash="new", min_save_interval_s=0),
            new_engine=lambda: fresh, on_event=lambda k, p: events.append((k, p)))
    r.guard = RequestGuard()
    r.guard.request = REQ
    r.worker = r.make()
    r.drain()
    assert len(store.commits) == 1                 # one rebuild, not two
    assert dict(events)["rebuild_completed"]["reason"] == "needs_rebuild"
    assert r.guard.request is None


def test_no_new_engine_support_leaves_the_key_alone():
    store = FakeStore(version=3, meta={"params_hash": "same", "generation": 1})
    r = Rig(_nodes(1), store=store, cfg=sw.WorkerConfig(expected_params_hash="same"))
    r.guard = RequestGuard()
    r.guard.request = REQ
    r.worker = r.make()
    r.drain()
    assert r.engine.batches == [["n000"]] and r.guard.request == REQ


def test_failed_clear_is_retried_and_lookup_errors_do_not_stop_the_worker():
    r = _request_rig(_nodes(2), request=REQ, check_s=1.0)
    r.guard.fail_clear = True
    r.drain()
    assert r.store.commits and r.guard.request == REQ and r.worker._clear_failed_at == REQ["at"]
    r.guard.fail_clear = False
    r.clock.advance(2)
    r.worker.step()
    assert r.guard.request is None and r.worker._clear_failed_at is None
    def boom(map_id):
        raise RuntimeError("pg down")
    r.guard.rebuild_request = boom
    r.clock.advance(2)
    assert r.worker.step().state == sw.IDLE


def test_guard_without_the_lookup_is_ignored():
    r = Rig(_nodes(1), store=FakeStore(version=3, meta={}), new_engine=lambda: FakeEngine())
    r.drain()
    assert r.engine.batches == [["n000"]]
