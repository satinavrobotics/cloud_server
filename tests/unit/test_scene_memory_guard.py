"""Graph-builder memory safety: the cgroup guard (hysteresis, pause of intake, refused loads), the restart-loop
marker, the memory numbers of /stats, store.gc housekeeping, engine.nbytes of the link memo, and the small ingest
economies (thumbnail removal, active-mission TTL, run-id index). Mocked cgroup files and clocks; no output of the
scene graph is touched."""
import asyncio
import json
import logging
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import numpy as np
import pytest

from packages.services.graph_builder import scene_memory as sm
from packages.services.graph_builder import scene_runner as sr
from packages.services.graph_builder import scene_worker as sw
from tests.unit.test_scene_worker import FakeEngine, FakeStore, MAP, Rig, make_node

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------- cgroup reader

def _cg(tmp_path, current=None, limit=None, events=None, v1=False, rss_kb=None):
    root = tmp_path / "cg"
    root.mkdir(exist_ok=True)
    if v1:
        (root / "memory").mkdir(exist_ok=True)
        if current is not None:
            (root / "memory" / "memory.usage_in_bytes").write_text(str(current))
        if limit is not None:
            (root / "memory" / "memory.limit_in_bytes").write_text(str(limit))
    else:
        if current is not None:
            (root / "memory.current").write_text(str(current))
        if limit is not None:
            (root / "memory.max").write_text(str(limit))
        if events is not None:
            (root / "memory.events").write_text(events)
    status = tmp_path / "status"
    status.write_text("Name:\tx\nVmRSS:\t%d kB\nThreads:\t3\n" % rss_kb if rss_kb else "Name:\tx\n")
    return sm.CgroupReader(str(root), str(status))


def test_reader_v2_numbers_and_events(tmp_path):
    r = _cg(tmp_path, 500, 1000, "low 0\nhigh 0\nmax 4\noom 2\noom_kill 1\n", rss_kb=2048)
    assert (r.current(), r.limit(), r.rss()) == (500, 1000, 2048 * 1024)
    assert r.events() == {"oom": 2, "oom_kill": 1}
    assert sm.process_stats(r) == {"rss_bytes": 2048 * 1024, "cgroup_current_bytes": 500,
                                   "cgroup_max_bytes": 1000, "oom": 2, "oom_kill": 1}


def test_reader_v2_max_means_no_limit_and_v1_falls_back(tmp_path):
    assert _cg(tmp_path, 5, "max").limit() is None
    (tmp_path / "x").mkdir()
    r1 = _cg(tmp_path / "x", 7, 9, v1=True)
    assert (r1.current(), r1.limit()) == (7, 9)
    (tmp_path / "y").mkdir()
    assert _cg(tmp_path / "y", 7, 1 << 62, v1=True).limit() is None


def test_reader_absent_everything_is_none(tmp_path):
    r = sm.CgroupReader(str(tmp_path / "nope"), str(tmp_path / "nostatus"))
    assert (r.current(), r.limit(), r.rss(), r.events()) == (None, None, None, {})


# ---------------------------------------------------------------- guard hysteresis

class FakeReader:
    def __init__(self, cur, lim=1000):
        self.cur, self.lim = cur, lim

    def current(self):
        return self.cur

    def limit(self):
        return self.lim

    def events(self):
        return {}

    def rss(self):
        return 1


def _guard(reader, **kw):
    t = {"v": 0.0}
    g = sm.MemoryGuard(0.8, 0.7, reader=reader, now=lambda: t["v"], sample_s=1.0, **kw)
    return g, t


def test_guard_pauses_above_high_resumes_below_low_with_one_warning_each(caplog):
    r = FakeReader(500)
    up, down = [], []
    g, t = _guard(r, on_high=lambda: up.append(1), on_low=lambda: down.append(1))
    caplog.set_level(logging.WARNING, logger="GraphBuilderService.scene_memory")
    assert not g.paused
    r.cur = 850
    t["v"] += 2
    assert g.paused and up == [1]
    r.cur = 750                         # between low and high: stays paused (hysteresis)
    t["v"] += 2
    assert g.paused and down == []
    for _ in range(3):
        t["v"] += 2
        assert g.check()
    r.cur = 650
    t["v"] += 2
    assert not g.paused and down == [1] and up == [1]
    warns = [rec for rec in caplog.records if rec.levelno == logging.WARNING]
    assert len(warns) == 2 and "850MB" not in warns[0].getMessage() and "paused" in warns[0].getMessage()
    assert "resumed" in warns[1].getMessage() and g.transitions == 2


def test_guard_samples_at_most_once_per_interval():
    r = FakeReader(900)
    g, t = _guard(r)
    assert g.paused
    r.cur = 100
    assert g.paused                     # not sampled again within sample_s
    t["v"] += 1.5
    assert not g.paused


def test_guard_without_cgroup_or_limit_is_off():
    for reader in (FakeReader(None, None), FakeReader(900, None), FakeReader(900, 0)):
        g, t = _guard(reader)
        assert not g.paused
    off = sm.MemoryGuard(0.0, 0.0, reader=FakeReader(999))
    assert not off.paused


def test_guard_hook_failure_does_not_raise():
    g, t = _guard(FakeReader(900), on_high=Mock(side_effect=RuntimeError("x")))
    assert g.paused


# ---------------------------------------------------------------- worker: intake pause

class Switch:
    def __init__(self):
        self.paused = False


def test_paused_guard_stops_intake_and_nothing_is_lost():
    sw_ = Switch()
    r = Rig([make_node("a", 1000), make_node("b", 2000)], memory=sw_)
    sw_.paused = True
    for _ in range(3):
        res = r.worker.step()
    assert res.state == sw.IDLE and r.engine.batches == [] and r.worker.stats.memory_paused_steps == 3
    assert not r.worker.settled()
    sw_.paused = False
    r.drain()
    assert r.engine.batches == [["a", "b"]] and r.cursors.get(MAP) == (2000, "b")


def test_paused_guard_still_folds_what_is_buffered():
    sw_ = Switch()
    cfg = sw.WorkerConfig(batch_size=100, save_every_leaves=1, min_save_interval_s=0, batch_min_leaves=2,
                          batch_max_leaves=2)
    r = Rig([make_node("a", 1000), make_node("b", 2000), make_node("c", 3000)], cfg=cfg, memory=sw_)
    r.worker.step()                     # reads all three, folds two (batch max 2)
    sw_.paused = True
    r.drain()
    assert r.worker.stats.folded >= 2


# ---------------------------------------------------------------- worker: load gate + marker

def _lazy(gate, nodes=()):
    loads = []

    def factory(version):
        loads.append(version)
        return FakeEngine()
    cfg = sw.WorkerConfig(batch_size=100, save_every_leaves=1, min_save_interval_s=0, lazy_load=True)
    r = Rig(nodes, cfg=cfg, engine_factory=factory, load_gate=gate)
    r.engine = None
    r.worker = r.make()
    r.loads = loads
    return r


def test_memory_high_refuses_another_engine_load_then_allows_it():
    sw_ = Switch()
    gate = sm.LoadGate(sw_, None, retry_s=5.0)
    r = _lazy(gate, [make_node("a", 5000)])
    sw_.paused = True
    res = r.worker.step()               # paused: nothing pulled, so no load is attempted
    assert r.loads == [] and r.worker.engine is None
    res = r.worker._ensure_engine(3)    # a direct load attempt is refused
    assert res.state == sw.BACKOFF and "memory high" in res.detail and r.loads == []
    sw_.paused = False
    r.drain()
    assert r.loads == [3] and r.worker.engine is not None


def test_marker_written_during_load_and_cleared_after(tmp_path):
    path = tmp_path / "marker.json"
    seen = {}
    marker = sm.LoadMarker(str(path), 600, clock=lambda: 1000.0)

    def factory(version):
        seen["during"] = json.loads(path.read_text())
        return FakeEngine()
    cfg = sw.WorkerConfig(batch_size=100, lazy_load=True)
    r = Rig([], cfg=cfg, engine_factory=factory, load_gate=sm.LoadGate(None, marker))
    r.engine = None
    r.worker = r.make()
    assert r.worker._ensure_engine(3) is None
    assert seen["during"] == {"maps": {MAP: 1000.0}} and not path.exists()


def test_marker_cleared_when_the_load_raises(tmp_path):
    path = tmp_path / "marker.json"
    marker = sm.LoadMarker(str(path), 600)
    cfg = sw.WorkerConfig(batch_size=100, lazy_load=True)
    r = Rig([], cfg=cfg, engine_factory=Mock(side_effect=IOError("down")), load_gate=sm.LoadGate(None, marker))
    r.engine = None
    r.worker = r.make()
    assert r.worker._ensure_engine(3).state == sw.BACKOFF
    assert not path.exists()


def test_dead_process_marker_blocks_that_map_until_cooldown(tmp_path, caplog):
    path = tmp_path / "marker.json"
    path.write_text(json.dumps({"maps": {MAP: 1000.0}}))
    now = {"v": 1100.0}
    caplog.set_level(logging.ERROR, logger="GraphBuilderService.scene_memory")
    marker = sm.LoadMarker(str(path), 600, clock=lambda: now["v"])
    assert any(MAP in rec.getMessage() and rec.levelno == logging.ERROR for rec in caplog.records)
    assert marker.snapshot()["blocked"] == {MAP: 500.0}
    r = _lazy(sm.LoadGate(None, marker, retry_s=10.0))
    r.worker.config.backoff_s = 10.0
    res = r.worker._ensure_engine(3)
    assert res.state == sw.BACKOFF and "cooldown" in res.detail and r.loads == []
    assert marker.blocked_for("other") == 0
    now["v"] = 1700.0                   # older than the cooldown: auto-load is allowed again
    assert r.worker._ensure_engine(3) is None and r.loads == [3]
    assert marker.snapshot()["blocked"] == {}


def test_unreadable_marker_is_ignored(tmp_path):
    path = tmp_path / "marker.json"
    path.write_text("{not json")
    assert sm.LoadMarker(str(path), 600).dead == {}


# ---------------------------------------------------------------- runner report

class _W:
    def __init__(self, nbytes, canvas):
        self.stats = sw.SceneWorkerStats(resident=True, resident_bytes=nbytes, canvas=canvas)

    def step(self):
        return sw.StepResult(sw.IDLE)

    def close(self):
        pass


def test_runner_reports_rss_cgroup_and_engines_and_logs_every_60s(tmp_path, caplog):
    reader = _cg(tmp_path, 600 << 20, 1000 << 20, "oom 1\noom_kill 0\n", rss_kb=300 * 1024)
    t = {"v": 0.0}
    guard = sm.MemoryGuard(0.8, 0.7, reader=reader, now=lambda: t["v"])
    workers = {"m1": _W(123 << 20, (400, 500))}
    runner = sr.SceneRunner(lambda: ["m1"], lambda m: workers[m], sr.RunnerConfig(), now=lambda: t["v"],
                            memory=guard)
    caplog.set_level(logging.INFO, logger="GraphBuilderService.scene_runner")
    runner.tick()
    runner.tick()
    lines = [r.getMessage() for r in caplog.records if "memory rss" in r.getMessage()]
    assert len(lines) == 1 and "rss=300MB" in lines[0] and "600MB/1000MB" in lines[0] and "400x500" in lines[0]
    t["v"] = 30
    runner.tick()
    assert len([r for r in caplog.records if "memory rss" in r.getMessage()]) == 1
    t["v"] = 61
    runner.tick()
    assert len([r for r in caplog.records if "memory rss" in r.getMessage()]) == 2
    snap = runner.snapshot()["memory"]
    assert snap["rss_bytes"] == 300 * 1024 * 1024 and snap["oom"] == 1 and snap["oom_kill"] == 0
    assert snap["engines"] == {"m1": {"nbytes": 123 << 20, "canvas": (400, 500)}} and snap["paused"] is False


def test_worker_records_the_canvas_with_nbytes():
    class Sized(FakeEngine):
        nbytes = 77
        _V = np.zeros((6, 9), np.float32)
    r = Rig([make_node("a", 1000)])
    r.worker.engine = Sized()
    r.worker._account(force=True)
    assert r.worker.stats.resident_bytes == 77 and r.worker.stats.canvas == (6, 9)


# ---------------------------------------------------------------- store.gc housekeeping

class GcStore(FakeStore):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.gcs = []

    def gc(self, map_id, keep_last=None, leased=()):
        self.gcs.append((map_id, keep_last, list(leased)))
        return {"deleted": 4}


def _gc_rig(every, store=None, nodes=()):
    cfg = sw.WorkerConfig(batch_size=100, save_every_leaves=1, min_save_interval_s=0, gc_every_saves=every)
    return Rig(nodes, cfg=cfg, store=store or GcStore())


def test_gc_every_n_saves_keeps_the_live_version_leased():
    r = _gc_rig(2)
    for i in range(4):
        r.source.add(make_node("n%d" % i, 1000 + i * 1000))
        r.worker.step()
    assert r.worker.stats.saves == 4
    assert len(r.store.gcs) == 2
    assert r.store.gcs[-1] == (MAP, None, [r.store.v]) and r.worker.stats.gc_runs == 2
    assert r.worker.stats.gc_deleted == 8


def test_gc_on_idle_after_a_save_and_only_once():
    r = _gc_rig(50, nodes=[make_node("a", 1000)])
    r.drain()
    for _ in range(3):
        r.worker.step()
    assert len(r.store.gcs) == 1


def test_gc_is_off_by_default_and_skipped_when_the_store_moved():
    r = _gc_rig(0, nodes=[make_node("a", 1000)])
    r.drain()
    assert r.store.gcs == []
    r2 = _gc_rig(1, nodes=[make_node("a", 1000)])
    r2.worker.step()
    n = len(r2.store.gcs)
    r2.store.v += 1                     # another writer: the live version is not the one we expect
    r2.worker._saves_since_gc = 1
    r2.worker._gc()
    assert len(r2.store.gcs) == n


def test_gc_failure_is_swallowed():
    store = GcStore()
    store.gc = Mock(side_effect=IOError("minio"))
    r = _gc_rig(1, store=store, nodes=[make_node("a", 1000)])
    assert r.drain().state == sw.IDLE and r.worker.stats.saves == 1


# ---------------------------------------------------------------- real store: gc keeps live + pending; tile cache scale

def test_real_store_gc_keeps_the_live_version_and_the_pending_generation():
    from packages.scene_graph.api import SceneParams
    from packages.scene_graph.engine import ChartGraph
    from packages.scene_graph.store import MemoryBackend, SceneGraphStore
    from tests.unit.scene_graph import chart_worlds as cw
    w, way = cw.two_rooms(W=24.0)
    leaves = cw.drive(w, way)
    b = MemoryBackend()
    store = SceneGraphStore(b, b, keep_last=2, clock=lambda: 1_700_000_000.0)
    g, v = ChartGraph(SceneParams()), None
    for i in range(0, len(leaves), 6):
        v = store.save("m", g, g.fold_many(leaves[i:i + 6]), v, {"i": i})
    p2 = SceneParams(chart_radius=2.4)
    gen = store.begin_generation("m", SceneGraphStore.params_hash(p2))
    g2 = ChartGraph(p2)
    store.save("m", g2, g2.fold_many(leaves[:12]), v, {"i": 12}, generation=gen)
    before = store.load("m", v).query(__import__("packages.scene_graph.api", fromlist=["BBox"]).BBox(-1e4, -1e4, 1e4, 1e4), raster=False)
    res = store.gc("m", leased=[v])
    assert res["deleted"] > 0 and v in res["kept_versions"]
    after = store.load("m", v).query(__import__("packages.scene_graph.api", fromlist=["BBox"]).BBox(-1e4, -1e4, 1e4, 1e4), raster=False)
    assert sorted(c.key for c in before.charts) == sorted(c.key for c in after.charts)
    pend = store.load("m", pending=True)            # the pending generation survived the collection
    assert pend is not None
    nv, _ = store.commit_generation("m", gen)
    assert nv == v + 1


def test_tile_cache_scale_shrinks_and_restores():
    from packages.scene_graph.store import MemoryBackend, SceneGraphStore
    b = MemoryBackend()
    store = SceneGraphStore(b, b, tile_cache_bytes=1000)
    for i in range(10):
        store._tile_put(("m", "t%d" % i), {"a": np.zeros(10, np.uint8) if False else np.zeros(100, np.uint8)})
    assert store._tile_bytes == 1000
    store.scale_tile_cache(0.25)
    assert store.tile_cache_bytes == 250 and store._tile_bytes <= 250 and len(store._tiles) == 2
    store.scale_tile_cache(1.0)
    assert store.tile_cache_bytes == 1000


# ---------------------------------------------------------------- engine.nbytes counts the link memo

def test_nbytes_counts_the_lmemo_route_arrays():
    from packages.scene_graph.api import SceneParams
    from packages.scene_graph.engine import ChartGraph
    g = ChartGraph(SceneParams())
    base = g.nbytes
    ys, xs = np.zeros(50_000, np.int32), np.zeros(50_000, np.int32)
    g._lmemo = (("a", "b"), {"k": (1.5, {"a": 1.0}, ys, xs)})      # (length float, integrals, ys, xs): first item is a float
    assert g.nbytes - base >= ys.nbytes + xs.nbytes


# ---------------------------------------------------------------- image_db: thumbnails only of an image that existed

@patch("packages.topomap_dbs.minio_base.Minio")
def test_store_image_skips_thumbnail_removal_for_a_new_key(mock_minio):
    from minio.error import S3Error
    from packages.topomap_dbs.image_db.server import ImageDatabaseService
    client = Mock()
    client.list_buckets.return_value = []
    client.bucket_exists.return_value = True
    mock_minio.return_value = client
    svc = ImageDatabaseService()
    client.stat_object.side_effect = S3Error(Mock(), "NoSuchKey", "x", "r", "id", "h")
    assert svc.store_image(image_data=b"x", image_id="i", node_id="n", map_id="m") is True
    client.remove_object.assert_not_called()
    client.stat_object.side_effect = None
    assert svc.store_image(image_data=b"x", image_id="i", node_id="n", map_id="m") is True
    assert client.remove_object.call_count == len(svc.SIZES)


# ---------------------------------------------------------------- graph_db: run-id index

def test_last_run_id_ensures_its_index_once():
    from packages.topomap_dbs.graph_db.server import GraphDatabaseService
    s = GraphDatabaseService.__new__(GraphDatabaseService)
    s.logger = logging.getLogger("t")
    s.db = MagicMock()
    s.db.has_collection.return_value = True
    s.db.aql.execute.return_value = iter(["run-1"])
    assert s.last_run_id("m", "r", "s") == "run-1"
    s.db.aql.execute.return_value = iter(["run-2"])
    assert s.last_run_id("m", "r", "s") == "run-2"
    s.db.collection.assert_called_once_with("nodes_m")
    s.db.collection.return_value.add_persistent_index.assert_called_once_with(fields=["robot_name", "session_id"])
    s._forget_collection("nodes_m")
    s.db.aql.execute.return_value = iter([])
    s.last_run_id("m", "r", "s")
    assert s.db.collection.return_value.add_persistent_index.call_count == 2


def test_last_run_id_still_answers_when_the_index_cannot_be_made():
    from packages.topomap_dbs.graph_db.server import GraphDatabaseService
    s = GraphDatabaseService.__new__(GraphDatabaseService)
    s.logger = logging.getLogger("t")
    s.db = MagicMock()
    s.db.has_collection.return_value = True
    s.db.collection.return_value.add_persistent_index.side_effect = RuntimeError("no")
    s.db.aql.execute.return_value = iter(["run-1"])
    assert s.last_run_id("m", "r", "s") == "run-1"


# ---------------------------------------------------------------- graph-builder: active mission TTL

def _gb(monkeypatch, ttl, missions):
    from packages.services.graph_builder import server as gbs
    s = gbs.GraphBuilderService.__new__(gbs.GraphBuilderService)
    s.logger = logging.getLogger("t")
    s.database = Mock()
    s.database.list_objects = AsyncMock(side_effect=missions)
    monkeypatch.setattr(gbs._gb_config, "GB_ACTIVE_MISSION_TTL_S", ttl, raising=False)
    clock = {"v": 100.0}
    monkeypatch.setattr(gbs.time, "monotonic", lambda: clock["v"])
    return s, clock


def test_active_mission_is_cached_per_robot_within_the_ttl(monkeypatch):
    m = SimpleNamespace(register_map=False)
    s, clock = _gb(monkeypatch, 1.0, [[m], [], []])
    assert asyncio.run(s._active_mission("r1")) == (m, False)
    assert asyncio.run(s._active_mission("r1")) == (m, False)
    assert s.database.list_objects.await_count == 1
    clock["v"] += 1.5                   # TTL over: asked again, the answer changed
    assert asyncio.run(s._active_mission("r1")) == (None, True)
    assert asyncio.run(s._active_mission("r2")) == (None, True)          # another robot: its own entry
    assert s.database.list_objects.await_count == 3


def test_active_mission_failure_is_not_cached_and_ttl_zero_asks_every_time(monkeypatch):
    s, clock = _gb(monkeypatch, 1.0, [RuntimeError("pg"), []])
    assert asyncio.run(s._active_mission("r1")) == (None, True)
    assert asyncio.run(s._active_mission("r1")) == (None, True)
    assert s.database.list_objects.await_count == 2
    s0, _ = _gb(monkeypatch, 0.0, [[], []])
    asyncio.run(s0._active_mission("r1")); asyncio.run(s0._active_mission("r1"))
    assert s0.database.list_objects.await_count == 2
