"""Ingest worker at scale: the step pool, idle-engine eviction and the memory budget, the PNG prefetch
pool and the faster decode, the metrics, plus the review fixes of the same pass (rebuild commit with
buffered leaves, bounded MemoryError retry). Fakes only."""
import io
import struct
import threading
import time
import zlib
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image

from packages.services.graph_builder import scene_adapter as sa
from packages.services.graph_builder import scene_runner as sr
from packages.services.graph_builder import scene_worker as sw
from tests.unit.test_scene_worker import (FakeEngine, FakeGuard, FakeStore, MAP, Rig, _png, make_node)


def _nodes(n, start=0, step=1000):
    return [make_node("n%03d" % i, 1000 + (start + i) * step) for i in range(n)]


# ---------------------------------------------------------------- fake workers for the runner

class StepWorker:
    """A runner-facing fake: `step()` runs `body(self)` and returns a StepResult."""
    def __init__(self, map_id, body=None, cold=False, evictable=True):
        self.map_id, self.body, self.cold = map_id, body, cold
        self.stats = sw.SceneWorkerStats()
        self.steps, self.closed, self.evicted = 0, False, 0
        self._evictable = evictable
        self.running = 0
        self.max_running = 0
        self.lock = threading.Lock()

    def step(self):
        with self.lock:
            self.running += 1
            self.max_running = max(self.max_running, self.running)
        try:
            self.steps += 1
            return self.body(self) if self.body else sw.StepResult(sw.IDLE, sleep_s=0.0)
        finally:
            with self.lock:
                self.running -= 1

    def evictable(self):
        return self._evictable and self.stats.resident

    def evict(self):
        self.evicted += 1
        self.stats.resident, self.stats.resident_bytes = False, 0
        return True

    def close(self):
        self.closed = True


def _runner(workers, **cfg):
    cfg.setdefault("threads", 2)
    return sr.SceneRunner(lambda: list(workers), lambda m: workers[m], config=sr.RunnerConfig(**cfg))


def _drive(runner, until, timeout=10.0, before_stop=None):
    stop = {"v": False}
    th = threading.Thread(target=lambda: runner.run(lambda s: time.sleep(0.005), lambda: stop["v"],
                                                    tick_sleep_s=0.01))
    th.start()
    end = time.time() + timeout
    try:
        while time.time() < end and not until():
            time.sleep(0.005)
    finally:
        if before_stop is not None:
            before_stop()
        stop["v"] = True
        th.join(15)
    assert not th.is_alive()


# ---------------------------------------------------------------- 1. step pool

def test_a_slow_map_does_not_starve_the_others():
    gate = threading.Event()
    slow = StepWorker("slow", body=lambda w: (gate.wait(5), sw.StepResult(sw.WORKED))[1])
    fast = {m: StepWorker(m, body=lambda w: sw.StepResult(sw.WORKED)) for m in ("a", "b", "c")}
    workers = {"slow": slow, **fast}
    runner = _runner(workers, threads=2)
    _drive(runner, lambda: all(w.steps >= 20 for w in fast.values()), before_stop=gate.set)
    assert slow.steps == 1                          # parked inside its first step the whole time
    assert all(w.steps >= 20 for w in fast.values())


def test_one_step_per_map_at_a_time_and_at_most_threads_at_once():
    cur, peak, lock = {"n": 0}, {"n": 0}, threading.Lock()

    def body(w):
        with lock:
            cur["n"] += 1
            peak["n"] = max(peak["n"], cur["n"])
        time.sleep(0.002)
        with lock:
            cur["n"] -= 1
        return sw.StepResult(sw.WORKED)
    workers = {"m%d" % i: StepWorker("m%d" % i, body=body) for i in range(6)}
    runner = _runner(workers, threads=3)
    _drive(runner, lambda: all(w.steps >= 15 for w in workers.values()))
    assert all(w.max_running == 1 for w in workers.values())
    assert 2 <= peak["n"] <= 3


def test_always_working_maps_get_even_turns():
    workers = {m: StepWorker(m, body=lambda w: sw.StepResult(sw.WORKED)) for m in "abcd"}
    runner = _runner(workers, threads=1)
    _drive(runner, lambda: sum(w.steps for w in workers.values()) >= 200)
    counts = [w.steps for w in workers.values()]
    assert max(counts) - min(counts) <= 2, counts


def test_cold_loads_never_take_the_last_thread():
    cur, peak, lock = {"n": 0}, {"n": 0}, threading.Lock()

    def body(w):
        with lock:
            cur["n"] += 1
            peak["n"] = max(peak["n"], cur["n"])
        time.sleep(0.01)
        with lock:
            cur["n"] -= 1
        w.cold = False
        return sw.StepResult(sw.IDLE, sleep_s=30)
    workers = {"c%d" % i: StepWorker("c%d" % i, body=body, cold=True) for i in range(6)}
    runner = _runner(workers, threads=3)
    _drive(runner, lambda: all(w.steps >= 1 for w in workers.values()))
    assert peak["n"] <= 2                            # threads - 1


def test_a_map_dropped_while_its_step_runs_is_closed_after_the_step():
    entered, release = threading.Event(), threading.Event()

    def body(w):
        entered.set()
        release.wait(5)
        return sw.StepResult(sw.WORKED)
    w = StepWorker("a", body=body)
    listing = {"v": ["a"]}
    runner = sr.SceneRunner(lambda: listing["v"], lambda m: w,
                            config=sr.RunnerConfig(threads=1, discover_s=0.0))
    runner.tick()
    assert entered.wait(5)
    listing["v"] = []
    runner.tick()
    runner.tick()                                   # absent in two discoveries: dropped, but still stepping
    assert not w.closed
    release.set()
    for _ in range(200):
        runner.tick()
        if w.closed:
            break
        time.sleep(0.005)
    assert w.closed
    runner.close()


def test_close_waits_for_the_running_step_and_the_runner_can_run_again():
    release = threading.Event()
    w = StepWorker("a", body=lambda w: (release.wait(0.05), sw.StepResult(sw.IDLE, sleep_s=60))[1])
    runner = _runner({"a": w}, threads=2)
    runner.tick()
    runner.close()
    assert w.closed and not runner._inflight
    w.closed = False
    runner._due.clear()
    runner.tick()
    assert runner._pool is not None
    runner.close()


def test_a_step_that_raises_drops_the_worker_and_backs_off():
    def boom(w):
        raise RuntimeError("x")
    w = StepWorker("a", body=boom)
    runner = _runner({"a": w}, threads=1, backoff_s=5.0)
    runner.tick()
    for _ in range(100):
        if runner.tick() or w.closed:
            break
        time.sleep(0.005)
    assert w.closed
    runner.close()


# ---------------------------------------------------------------- 2. eviction / budget

def _lazy_rig(nodes, **cfgkw):
    loads = []

    def factory(version):
        loads.append(version)
        return FakeEngine()
    base = dict(batch_size=100, save_every_leaves=1, min_save_interval_s=0, lazy_load=True)
    cfg = sw.WorkerConfig(**{**base, **cfgkw})
    r = Rig(nodes, cfg=cfg, engine_factory=factory)
    r.engine = None
    r.worker = r.make()
    r.loads = loads
    return r


def test_lazy_load_loads_on_the_first_fold_not_at_start():
    r = _lazy_rig([])
    for _ in range(3):
        assert r.worker.step().state == sw.IDLE
    assert r.loads == [] and r.worker.engine is None and r.worker.cold
    r.source.add(make_node("a", 5000))
    r.drain()
    assert r.loads == [3] and r.worker.engine.batches == [["a"]]
    assert r.worker.stats.loads == 1 and r.worker.stats.resident


def test_evict_drops_the_engine_and_the_next_leaves_reload_without_refolding():
    r = _lazy_rig(_nodes(3))
    r.drain()
    first = r.worker.engine
    assert first.batches and r.worker.evictable()
    saves_before, version_at_evict = len(r.store.saves), r.store.v
    assert r.worker.evict()
    assert r.worker.engine is None and r.worker.stats.evictions == 1 and not r.worker.stats.resident
    assert r.worker.stats.resident_bytes == 0
    assert len(r.store.saves) == saves_before                       # nothing was unsaved
    r.clock.advance(120)                                           # the margin is re-read: all seen already
    for _ in range(3):
        r.worker.step()
    assert r.worker.engine is None and len(r.loads) == 1            # an idle map stays unloaded
    r.source.add(make_node("late", 500_000))
    r.drain()
    assert len(r.loads) == 2 and r.loads[-1] == version_at_evict    # reloaded at the saved version
    assert r.worker.engine.batches == [["late"]]                    # no replay of what was folded


def test_evict_saves_unsaved_folds_first_and_keeps_the_engine_when_the_save_fails():
    r = _lazy_rig(_nodes(2), save_every_s=1e9)
    r.cfg.save_every_leaves = 100
    r.cfg.save_when_idle = False
    r.worker.step()
    r.worker.step()
    assert r.worker._dirty is not None and not r.store.saves
    r.store.fail = 1
    assert not r.worker.evict()
    assert r.worker.engine is not None and r.worker._dirty is not None
    assert r.worker.evict()
    assert len(r.store.saves) == 1 and r.worker.engine is None and r.worker._dirty is None
    assert r.store.meta_doc["cursor"] == r.worker.cursor


def test_not_evictable_with_buffered_or_pending_leaves_or_a_terminal_state():
    r = _lazy_rig([], batch_min_leaves=5, min_save_interval_s=100)
    r.source.add(make_node("a", 5000))
    r.worker.step()
    assert r.worker._buf and not r.worker.evictable()
    r2 = _lazy_rig([make_node("p", 5000)])
    r2.no_png = frozenset({"p"})
    r2.worker.step()
    assert r2.worker._pending and not r2.worker.evictable()
    r3 = _lazy_rig(_nodes(1))
    r3.drain()
    assert r3.worker.evictable()
    r3.guard.deleting = True
    r3.worker.step()
    assert not r3.worker.evictable() and not r3.worker.evict()


def test_evicting_a_rebuild_reloads_the_saved_pending_generation():
    from tests.unit.test_scene_worker_robustness import PendingStore, _rebuild_rig
    store = PendingStore(version=5, meta={"params_hash": "old", "generation": 1})
    r = _rebuild_rig(_nodes(8), store=store, rebuild_batch=2, batch_size=2)
    reloaded = FakeEngine()
    store.pending_engine = reloaded
    for _ in range(30):
        r.worker.step()
        if r.worker._rebuild_saved:
            break
    assert r.worker._rebuild_saved and r.worker._rebuild_gen is not None
    store.pending_meta = {"generation": store.pending, "version": 1, "params_hash": "new",
                          "cursor": list(r.worker.cursor), "begun_ms": 1}
    r.worker._buf.clear()
    r.worker._buffered.clear()
    assert r.worker.evict()
    assert r.worker.engine is None
    r.drain(n=30)
    assert store.loaded_pending == 1 and r.worker.engine is reloaded
    assert store.commits


def test_runner_evicts_the_least_recently_active_idle_engines_over_the_cap():
    def resident(m, nb=10):
        w = StepWorker(m)
        w.stats.resident, w.stats.resident_bytes = True, nb
        return w
    workers = {m: resident(m) for m in "abcd"}
    clock = {"t": 1000.0}
    runner = sr.SceneRunner(lambda: list(workers), lambda m: workers[m], now=lambda: clock["t"],
                            config=sr.RunnerConfig(threads=0, max_resident=2, evict_idle_s=30.0))
    runner._active_at.update({"a": 100.0, "b": 200.0, "c": 995.0, "d": 300.0})
    for m in workers:
        runner._workers[m] = workers[m]
    runner._known = sorted(workers)
    runner._enforce_budget(clock["t"], inline=True)
    # c was active 5 s ago (not idle): a, b (oldest) go, d stays under the cap of 2
    assert [m for m, w in workers.items() if w.evicted] == ["a", "b"]
    assert runner.evictions == 2 and not runner.over_budget


def test_runner_byte_cap_and_over_budget_when_everything_is_active():
    workers = {m: StepWorker(m) for m in "ab"}
    for w in workers.values():
        w.stats.resident, w.stats.resident_bytes = True, 600
    runner = sr.SceneRunner(lambda: [], lambda m: None, now=lambda: 100.0,
                            config=sr.RunnerConfig(threads=0, max_resident_bytes=1000, evict_idle_s=60.0))
    runner._workers.update(workers)
    runner._active_at.update({"a": 99.0, "b": 98.0})
    runner._enforce_budget(100.0, inline=True)
    assert not any(w.evicted for w in workers.values()) and runner.over_budget
    runner._active_at.update({"a": 0.0, "b": 10.0})
    runner._enforce_budget(100.0, inline=True)
    assert workers["a"].evicted == 1 and not workers["b"].evicted and not runner.over_budget


def test_pooled_runner_evicts_on_the_pool_and_never_a_busy_map():
    workers = {m: StepWorker(m, body=lambda w: sw.StepResult(sw.IDLE, sleep_s=60)) for m in "abc"}
    for w in workers.values():
        w.stats.resident, w.stats.resident_bytes = True, 5
    runner = _runner(workers, threads=2, max_resident=1, evict_idle_s=0.0)
    _drive(runner, lambda: runner.evictions >= 2)
    assert sum(w.evicted for w in workers.values()) == 2


def test_nbytes_is_feature_detected_and_refreshed_after_a_fold():
    class Sized(FakeEngine):
        n = 1000

        @property
        def nbytes(self):
            return self.n
    r = _lazy_rig(_nodes(1))
    r.factory_engine = Sized()
    r.worker.engine_factory = lambda v: r.factory_engine
    r.drain()
    assert r.worker.stats.resident_bytes == 1000
    r.factory_engine.n = 5000
    r.source.add(make_node("more", 900_000))
    r.clock.advance(20)
    r.drain()
    assert r.worker.stats.resident_bytes == 5000
    r2 = _lazy_rig(_nodes(1))
    r2.drain()
    assert r2.worker.stats.resident and r2.worker.stats.resident_bytes is None    # the engine cannot say


# ---------------------------------------------------------------- 3. prefetch

def _prefetch_rig(nodes, threads=2, queue=64, **kw):
    pre = sw.PngPrefetch(threads, queue)
    seen = []

    class R(Rig):
        def fetch(self, map_id, key, layer):
            seen.append((key, threading.current_thread().name))
            return super().fetch(map_id, key, layer)
    r = R(nodes, prefetch=pre, **kw)
    r.pre, r.seen = pre, seen
    return r


def test_prefetch_fetches_on_the_pool_and_the_result_is_the_same():
    r = _prefetch_rig(_nodes(6))
    plain = Rig(_nodes(6))
    r.drain()
    plain.drain()
    assert r.engine.batches == plain.engine.batches
    assert {n for _k, n in r.seen} <= {n for _k, n in r.seen if n.startswith("scene-prefetch")}
    assert len(r.seen) == 6 and r.worker.stats.prefetch_hits == 6
    snap = r.pre.snapshot()
    assert snap["outstanding"] == 0 and snap["queue_depth"] == 0 and snap["hits"] == 6
    r.pre.close()


def test_prefetch_is_bounded_and_the_rest_is_fetched_inline():
    r = _prefetch_rig(_nodes(8), queue=3)
    r.drain()
    assert r.engine.batches and sum(len(b) for b in r.engine.batches) == 8
    assert r.worker.stats.prefetch_hits == 3
    assert r.pre.snapshot()["rejected"] >= 1 and r.pre.snapshot()["outstanding"] == 0
    r.pre.close()


def test_prefetch_decode_error_is_a_bad_png_skip_and_missing_png_stays_pending():
    r = _prefetch_rig([make_node("bad", 1000), make_node("gone", 2000), make_node("ok", 3000)])
    r.pngs = {"bad": b"not a png"}
    r.no_png = frozenset({"gone"})
    r.drain()
    assert r.worker.stats.skipped[sa.SKIP_BAD_PNG] == 1
    assert "gone" in r.worker._pending
    assert r.engine.batches == [["ok"]]
    r.pre.close()


def test_prefetch_fetch_error_backs_off_without_skipping_and_frees_the_slots():
    r = _prefetch_rig(_nodes(4))
    calls = {"n": 0}
    real = r.fetch

    def flaky(map_id, key, layer):
        calls["n"] += 1
        if key == "n001":
            raise OSError("minio down")
        return real(map_id, key, layer)
    r.fetch = flaky
    r.worker.fetch_png = flaky
    res = r.worker.step()
    assert res.state == sw.BACKOFF and "png fetch" in res.detail
    assert r.pre.snapshot()["outstanding"] == 0
    assert not r.worker.stats.skipped
    r.worker.fetch_png = real
    r.clock.advance(20)
    r.drain()
    assert sum(len(b) for b in r.engine.batches) == 4
    r.pre.close()


def test_prefetch_pool_can_be_closed_and_used_again():
    pre = sw.PngPrefetch(1, 4)
    t = pre.submit(lambda m, k, l: _png(), "m", "k", "l")
    assert t.result()[1].shape == (4, 4)
    pre.close()
    t = pre.submit(lambda m, k, l: None, "m", "k", "l")
    assert t.result() == (None, None, None)
    pre.close()


# ---------------------------------------------------------------- faster decode

def _chunk(kind, body):
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)


def _encode(arr, filters):
    h, w = arr.shape
    rows, prev = [], np.zeros(w, np.int32)
    for y in range(h):
        cur, ft = arr[y].astype(np.int32), filters[y % len(filters)]
        left = np.concatenate([[0], cur[:-1]])
        ul = np.concatenate([[0], prev[:-1]])
        if ft == 0:
            f = cur
        elif ft == 1:
            f = cur - left
        elif ft == 2:
            f = cur - prev
        elif ft == 3:
            f = cur - ((left + prev) >> 1)
        else:
            p = left + prev - ul
            pa, pb, pc = abs(p - left), abs(p - prev), abs(p - ul)
            f = cur - np.where((pa <= pb) & (pa <= pc), left, np.where(pb <= pc, prev, ul))
        rows.append(bytes([ft]) + (f & 255).astype(np.uint8).tobytes())
        prev = cur
    return (sa.PNG_SIGNATURE + _chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 0, 0, 0, 0))
            + _chunk(b"IDAT", zlib.compress(b"".join(rows))) + _chunk(b"IEND", b""))


def test_decode_matches_every_filter_type_in_mixed_rows():
    rng = np.random.default_rng(7)
    arr = rng.integers(0, 256, (37, 53), dtype=np.uint8)
    arr[5:20, 10:40] = 100
    for filters in ([0], [1], [2], [3], [4], [0, 1, 2, 3, 4], [4, 3, 4, 1]):
        out = sa.decode_png_gray8(_encode(arr, filters))
        assert np.array_equal(out, arr), filters


def test_unknown_filter_type_is_rejected():
    png = _encode(np.zeros((2, 3), np.uint8), [0])
    raw_start = png.index(b"IDAT") + 4
    length = struct.unpack(">I", png[raw_start - 8:raw_start - 4])[0]
    raw = bytearray(zlib.decompress(png[raw_start:raw_start + length]))
    raw[0] = 9
    bad = (sa.PNG_SIGNATURE + _chunk(b"IHDR", struct.pack(">IIBBBBB", 3, 2, 8, 0, 0, 0, 0))
           + _chunk(b"IDAT", zlib.compress(bytes(raw))) + _chunk(b"IEND", b""))
    with pytest.raises(sa.PngError):
        sa.decode_png_gray8(bad)


# ---------------------------------------------------------------- 4. metrics

def test_snapshot_reports_residency_pool_and_prefetch():
    w = StepWorker("a", body=lambda w: sw.StepResult(sw.IDLE, sleep_s=60))
    w.stats.resident, w.stats.resident_bytes, w.stats.evictions = True, 1234, 2
    pre = sw.PngPrefetch(1, 5)
    runner = sr.SceneRunner(lambda: ["a"], lambda m: w, prefetch=pre,
                            config=sr.RunnerConfig(threads=2, max_resident=4))
    runner.tick()
    for _ in range(100):
        if runner.tick() or w.steps:
            break
        time.sleep(0.005)
    runner.tick()
    snap = runner.snapshot()
    assert snap["maps"]["a"]["resident_bytes"] == 1234 and snap["maps"]["a"]["evictions"] == 2
    assert snap["resident"] == {"engines": 1, "bytes": 1234, "bytes_unknown": 0, "max_engines": 4,
                                "max_bytes": 0, "evictions": 0, "over_budget": False}
    assert snap["pool"]["threads"] == 2 and "queue_depth" in snap["pool"]
    assert snap["prefetch"]["max_pending"] == 5
    runner.close()


def test_queue_depth_counts_due_maps_without_a_thread():
    gate = threading.Event()
    workers = {m: StepWorker(m, body=lambda w: (gate.wait(5), sw.StepResult(sw.IDLE, sleep_s=60))[1])
               for m in "abcd"}
    runner = _runner(workers, threads=1)
    runner.tick()
    assert runner.snapshot()["pool"] == {"threads": 1, "inflight": 1, "queue_depth": 3, "cold_inflight": 0}
    gate.set()
    runner.close()


# ---------------------------------------------------------------- review fixes

def test_a_rebuild_does_not_commit_while_leaves_are_still_buffered():
    from tests.unit.test_scene_worker_robustness import _rebuild_rig
    r = _rebuild_rig(_nodes(7), rebuild_batch=2)         # one page holds all 7: the head is reached at once
    folded_at_commit = []
    real_commit = r.store.commit_generation

    def commit(map_id, gen):
        folded_at_commit.append(sum(len(b) for b in r.fresh.batches))
        return real_commit(map_id, gen)
    r.store.commit_generation = commit
    r.worker.step()
    assert not r.store.commits              # the page was scanned; only a batch of it is folded, the rest waits
    r.drain(n=30)
    assert r.store.commits
    assert folded_at_commit == [7]


def test_the_fake_store_models_pending_saves_without_moving_the_live_version():
    from tests.unit.test_scene_worker_robustness import _rebuild_rig
    r = _rebuild_rig(_nodes(4), rebuild_batch=2)
    live = r.store.v
    r.worker.step()
    assert r.store.saves and r.store.v == live            # pending save: the live version is untouched
    r.drain(n=20)
    assert r.store.commits and r.store.v == live + 1


def _memory_rig(nodes, limit, fails_for):
    cfg = sw.WorkerConfig(batch_size=100, save_every_leaves=1, min_save_interval_s=0,
                          memory_retry_limit=limit, load_fail_limit=3)
    events = []
    r = Rig(nodes, cfg=cfg, on_event=lambda k, p: events.append((k, p)))
    real = r.engine.fold_many

    def fold(leaves):
        if fails_for(leaves):
            raise MemoryError("allocation")
        return real(leaves)
    r.engine.fold_many = fold
    r.events = events
    return r


def test_a_batch_that_keeps_running_out_of_memory_is_bisected_and_the_leaf_quarantined():
    r = _memory_rig(_nodes(5), limit=3, fails_for=lambda ls: any(l.key == "n002" for l in ls))
    states = []
    for _ in range(12):
        res = r.worker.step()
        states.append(res.state)
        if res.state == sw.IDLE:
            break
    assert states[:2] == [sw.BACKOFF, sw.BACKOFF]
    assert sw.IDLE in states or states[-1] == sw.WORKED
    r.drain()
    folded = {k for b in r.engine.batches for k in b}
    assert folded == {"n000", "n001", "n003", "n004"}
    assert r.worker.stats.skipped[sw.SKIP_FOLD_ERROR] == 1
    assert r.worker.stats.memory_isolations == 1
    assert any(k == "fold_failed" and p["stage"] == "poison_leaf" for k, p in r.events)


def test_a_transient_memory_error_below_the_limit_is_still_just_retried():
    box = {"n": 0}

    def fails(ls):
        box["n"] += 1
        return box["n"] <= 2
    r = _memory_rig(_nodes(3), limit=3, fails_for=fails)
    assert r.worker.step().state == sw.BACKOFF
    assert r.worker.step().state == sw.BACKOFF
    r.drain()
    assert r.engine.batches == [["n000", "n001", "n002"]]
    assert r.worker.stats.memory_isolations == 0 and not r.worker.stats.skipped


def test_memory_pressure_that_bisects_into_foldable_halves_loses_nothing():
    r = _memory_rig(_nodes(6), limit=2, fails_for=lambda ls: len(ls) > 2)
    for _ in range(10):
        r.worker.step()
    folded = [k for b in r.engine.batches for k in b]
    assert sorted(folded) == ["n%03d" % i for i in range(6)]
    assert not r.worker.stats.skipped


# ---------------------------------------------------------------- host / config wiring

def test_runner_config_from_settings_and_validation(monkeypatch):
    from packages import config
    from packages.services.graph_builder import scene_host
    cfg = scene_host.runner_config(SimpleNamespace(
        SCENE_GRAPH_MAX_MAPS=64, SCENE_GRAPH_SLOW_STEP_S=30.0, SCENE_GRAPH_WORKER_THREADS=0,
        SCENE_GRAPH_MAX_RESIDENT_ENGINES=-3, SCENE_GRAPH_MAX_RESIDENT_BYTES=1 << 30,
        SCENE_GRAPH_EVICT_IDLE_S=45.0))
    assert (cfg.threads, cfg.max_resident, cfg.max_resident_bytes, cfg.evict_idle_s) == (1, 0, 1 << 30, 45.0)
    monkeypatch.setattr(config, "SCENE_GRAPH_WORKER_THREADS", 0)
    monkeypatch.setattr(config, "SCENE_GRAPH_PREFETCH_QUEUE", -1)
    monkeypatch.setattr(config, "SCENE_GRAPH_EVICT_IDLE_S", float("nan"))
    monkeypatch.setattr(config, "SCENE_GRAPH_MAX_RESIDENT_BYTES", -5)
    config._validate_scene_graph_worker_scale()
    assert config.SCENE_GRAPH_WORKER_THREADS >= 1 and config.SCENE_GRAPH_PREFETCH_QUEUE == 64
    assert config.SCENE_GRAPH_EVICT_IDLE_S == 60.0 and config.SCENE_GRAPH_MAX_RESIDENT_BYTES == 0
