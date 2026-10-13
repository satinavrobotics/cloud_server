"""Review fixes of the scene ingest (restart, generation rebuild, PNG fetch errors, terminal reports,
polling cost), against the REAL SceneGraphStore on the in-memory backend and the real ChartGraph where
the finding is about their interplay, and fakes elsewhere."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_scene_smoke as ts            # noqa: E402  (skips the module when the engine is absent)
import test_scene_worker as tw           # noqa: E402

from packages.scene_graph.api import SceneParams                          # noqa: E402
from packages.scene_graph.engine import ChartGraph                        # noqa: E402
from packages.scene_graph.store import SceneGraphStore, StaleVersion      # noqa: E402
from packages.scene_graph.store_backends import MemoryBackend             # noqa: E402
from packages.services.graph_builder import scene_host, scene_runner, scene_sources  # noqa: E402
from packages.services.graph_builder import scene_worker as sw            # noqa: E402

LEAVES = ts.leaves_of(ts.cw.two_rooms)
NODES = [ts.node_of(l, i) for i, l in enumerate(LEAVES)]
BY_KEY = {l.key: l for l in LEAVES}


def _png(map_id, key, layer):
    return ts.png(BY_KEY[key].window_u8)


def worker(store, source, engine=None, **kw):
    cfg = kw.pop("cfg", None) or sw.WorkerConfig(batch_size=10, min_save_interval_s=0, save_every_leaves=1)
    clock = kw.pop("clock", [ts.T0 + 10_000_000])
    fetch = kw.pop("fetch", _png)
    return sw.SceneWorker("m", source=source, fetch_png=fetch, footprint=lambda r: (0.8, 0.5), engine=engine,
                          store=store, cursor_store=sw.MemoryCursorStore(), guard=kw.pop("guard", None) or ts.Guard(),
                          config=cfg, now_ms=lambda: clock[0], fence_errors=(StaleVersion,), **kw)


def drive(w, n=400):
    res = None
    for _ in range(n):
        res = w.step()
        if res.state in sw.TERMINAL or res.state == sw.IDLE:
            break
    return res


def real_store():
    be = MemoryBackend()
    return SceneGraphStore(be, be)


# ---- H1: a restart resumes, it does not go to NEEDS_REBUILD

def test_restart_with_the_real_store_resumes_and_does_not_need_a_rebuild():
    store = real_store()
    w1 = worker(store, ts.Source(NODES), ChartGraph(SceneParams()))
    assert drive(w1).state == sw.IDLE
    stored = store.meta("m")["version"]
    loads = []

    def factory(version):
        loads.append(version)
        return store.load("m", version)

    w2 = worker(store, ts.Source(NODES), engine_factory=factory)
    res = w2.step()
    assert res.state in (sw.WORKED, sw.IDLE), res
    assert loads == [stored]                      # loaded once, after the lock, at the stored version
    assert drive(w2).state == sw.IDLE
    assert w2.state == ""


def test_engine_loaded_before_the_lock_at_another_version_is_reloaded_not_terminal():
    store = real_store()
    src = ts.Source(NODES)
    drive(worker(store, src, ChartGraph(SceneParams())))
    stale = store.load("m")
    # a second save lands between the caller's load and the lock
    extra = ts.node_of(LEAVES[0], 99)
    extra["_key"], extra["costmap_stored_ms"] = "late", ts.T0 + 5_000_000
    src.nodes["late"] = dict(extra, scene_stamp_ms=ts.T0 + 5_000_000)
    BY_KEY["late"] = LEAVES[0]
    drive(worker(store, src, engine_factory=lambda v: store.load("m", v)))
    newest = store.meta("m")["version"]
    reloads = []
    w = worker(store, src, engine=stale, loaded_version=newest - 1,
               engine_factory=lambda v: reloads.append(v) or store.load("m", v))
    assert w.step().state in (sw.WORKED, sw.IDLE)
    assert reloads == [newest] and w.state == ""


def test_engine_is_not_built_by_a_replica_that_does_not_own_the_map():
    built = []

    class Taken(ts.Guard):
        def acquire(self, m):
            return False

    w = worker(real_store(), ts.Source([]), engine_factory=lambda v: built.append(v) or ChartGraph(SceneParams()),
               guard=Taken())
    assert w.step().state == sw.NOT_OWNER and built == [] and w.engine is None


def test_a_failing_engine_load_backs_off_releases_the_lock_and_reports():
    events, g = [], ts.Guard()
    released = []
    g.release = lambda m: released.append(m)

    def boom(v):
        raise RuntimeError("tile missing")

    w = worker(real_store(), ts.Source([]), engine_factory=boom, guard=g, on_event=lambda k, p: events.append((k, p)))
    res = w.step()
    assert res.state == sw.BACKOFF and released == ["m"]
    assert events[0][0] == "fold_failed" and events[0][1]["stage"] == "load"


# ---- H2: a generation rebuild with several saves

def test_generation_rebuild_with_several_saves_commits_against_the_real_store():
    store = real_store()
    src = ts.Source(NODES)
    drive(worker(store, src, ChartGraph(SceneParams())))
    live = store.meta("m")["version"]
    p2 = SceneParams(robot_half_side=0.3)
    w = worker(store, src, ChartGraph(SceneParams()), new_engine=lambda: ChartGraph(p2),
               cfg=sw.WorkerConfig(batch_size=4, min_save_interval_s=0, save_every_leaves=1, rebuild_batch=4,
                                   expected_params_hash=scene_host.expected_params_hash(p2)))
    res = drive(w)
    assert res.state == sw.IDLE, res
    assert w.stats.saves >= 2 and w.stats.rebuilds == 1          # several pending saves, no fence
    meta = store.meta("m")
    assert meta["generation"] == 2 and meta["version"] == live + 1
    assert w.stats.version == meta["version"]


# ---- H3: a fetch error is not a missing PNG

def test_fetch_error_backs_off_without_skipping_or_advancing_past_the_node():
    src = tw.FakeSource([tw.make_node("a", 1000), tw.make_node("b", 2000), tw.make_node("c", 3000)])
    state = {"down": True}

    def fetch(m, key, layer):
        if key == "b" and state["down"]:
            raise OSError("minio down")
        return tw._png()

    r = tw.Rig(src.nodes.values())
    r.fetch = fetch
    r.worker = r.make()
    res = r.worker.step()
    assert res.state == sw.BACKOFF and "png fetch" in res.detail
    assert r.worker.cursor < (2000, "b") and r.worker.stats.pending == 0
    assert not any(r.worker.stats.skipped.values())
    for _ in range(3):                                     # still down: still nothing past "b"
        assert r.worker.step().state == sw.BACKOFF
    assert "c" not in [k for b in r.engine.batches for k in b]
    state["down"] = False
    r.drain()
    assert sorted(k for b in r.engine.batches for k in b) == ["a", "b", "c"]


def test_png_fetcher_tells_not_found_from_an_outage():
    class Err(Exception):
        def __init__(self, code):
            self.code = code

    class Client:
        def __init__(self, outcome):
            self.outcome = outcome

        def get_object(self, bucket, key):
            if isinstance(self.outcome, Exception):
                raise self.outcome
            return self.outcome

    class Resp:
        def read(self):
            return b"png"

        def close(self):
            pass

        def release_conn(self):
            pass

    class Db:
        def __init__(self, outcome):
            self.client = Client(outcome)

        def _bucket_name(self, map_id):
            return "b-" + map_id

        @staticmethod
        def costmap_key(node, layer):
            return f"{node}/costmap/{layer}.png"

    assert scene_sources.png_fetcher(Db(Resp()))("m", "n", "occupancy") == b"png"
    assert scene_sources.png_fetcher(Db(Err("NoSuchKey")))("m", "n", "occupancy") is None
    assert scene_sources.png_fetcher(Db(Err("NoSuchBucket")))("m", "n", "occupancy") is None
    with pytest.raises(Err):
        scene_sources.png_fetcher(Db(Err("InternalError")))("m", "n", "occupancy")
    with pytest.raises(OSError):
        scene_sources.png_fetcher(Db(OSError("timeout")))("m", "n", "occupancy")


# ---- M1: terminal states are reported (runner)

def test_runner_reports_a_terminal_state_once_with_a_fold_failed_event(caplog):
    class W:
        def __init__(self):
            self.n = 0

        def step(self):
            return sw.StepResult(sw.FENCED, detail="writer lock lost")

        def close(self):
            pass

    events = []
    t = [0.0]
    runner = scene_runner.SceneRunner(lambda: ["m"], lambda m: W(), now=lambda: t[0],
                                      config=scene_runner.RunnerConfig(terminal_retry_s=1.0),
                                      on_event=lambda m, k, p: events.append((m, k, p)))
    with caplog.at_level("WARNING"):
        for _ in range(3):
            runner.tick()
            t[0] += 2.0
    assert events == [("m", "fold_failed", {"stage": "fenced", "consecutive": 1, "detail": "writer lock lost"})]
    assert any("fenced" in r.message for r in caplog.records)


def test_worker_logs_its_terminal_state(caplog):
    r = tw.Rig([tw.make_node("a", 1000)])
    r.guard.deleting = True
    with caplog.at_level("WARNING"):
        assert r.worker.step().state == sw.DELETING
    assert any("deleting" in x.message for x in caplog.records)


def test_runner_reports_a_map_starved_by_max_maps_once():
    class W:
        def step(self):
            return sw.StepResult(sw.IDLE, sleep_s=0.0)

        def close(self):
            pass

    events, t = [], [0.0]
    runner = scene_runner.SceneRunner(lambda: ["a", "b"], lambda m: W(), now=lambda: t[0],
                                      config=scene_runner.RunnerConfig(max_maps=1, backoff_s=1.0),
                                      on_event=lambda m, k, p: events.append((m, k, p["stage"])))
    for _ in range(4):
        runner.tick()
        t[0] += 2.0
    assert events == [("b", "fold_failed", "max_maps")]


def test_runner_honours_the_workers_idle_sleep():
    class W:
        def step(self):
            return sw.StepResult(sw.IDLE, sleep_s=17.0)

        def close(self):
            pass

    t = [0.0]
    runner = scene_runner.SceneRunner(lambda: ["m"], lambda m: W(), now=lambda: t[0])
    runner.tick()
    assert runner._due["m"] == 17.0


# ---- M2: a terminal skip behind a waiting leaf is queued and counted once

def test_terminal_skip_behind_a_waiting_leaf_is_not_requeued_or_counted_as_progress():
    good = tw.make_node("a", 1000)
    bad = tw.make_node("z", 1001, costmap={})
    cfg = sw.WorkerConfig(batch_size=100, batch_min_leaves=6, min_save_interval_s=5.0)
    r = tw.Rig([good, bad], cfg=cfg)
    first = r.worker.step()
    assert first.skipped == 1
    for _ in range(5):
        res = r.worker.step()
        assert res.state == sw.IDLE and res.skipped == 0 and res.sleep_s > 0
    assert len(r.worker._buf) == 2 and r.worker.stats.skipped["no_occupancy_layer"] == 1


# ---- M4: the provisional stamp is not persisted

def test_provisional_receipt_stamp_is_not_kept_on_the_node():
    leaves = ts.leaves_of(ts.cw.corridor)[:5]
    nodes = [ts.node_of(l, i) for i, l in enumerate(leaves)]
    src = ts.Source(nodes)
    w = worker(real_store(), src, ChartGraph(SceneParams()),
               fetch=lambda m, k, layer: ts.png(next(l for l in leaves if l.key == k).window_u8))
    drive(w)
    kept = [k for k, n in src.nodes.items() if "scene_stamp_ms" in n]
    assert kept == [l.key for l in leaves[2:]]


# ---- M5: idle backoff, lifecycle rate limit

def test_idle_sleep_doubles_up_to_the_cap_and_resets_on_progress():
    cfg = sw.WorkerConfig(batch_size=100, idle_sleep_s=2.0, idle_max_s=30.0, min_save_interval_s=0)
    r = tw.Rig([], cfg=cfg)
    sleeps = [r.worker.step().sleep_s for _ in range(7)]
    assert sleeps == [2.0, 4.0, 8.0, 16.0, 30.0, 30.0, 30.0]
    r.source.add(tw.make_node("a", 1000))
    assert r.worker.step().state == sw.WORKED
    assert r.drain().sleep_s == 2.0               # back to the base sleep after progress


def test_lifecycle_lookup_is_rate_limited_but_always_made_before_a_save():
    calls = []

    class G(tw.FakeGuard):
        def is_deleting(self, m):
            calls.append(1)
            return False

    cfg = sw.WorkerConfig(batch_size=100, lifecycle_check_s=5.0, min_save_interval_s=0, save_every_leaves=1)
    r = tw.Rig([], cfg=cfg)
    r.guard = G()
    r.worker = r.make()
    for _ in range(5):
        r.worker.step()
    assert len(calls) == 1
    r.source.add(tw.make_node("a", 1000))
    r.worker.step()                       # folds and saves: the save looks again
    assert len(calls) == 2
    r.clock.advance(6)
    r.worker.step()
    assert len(calls) == 3


# ---- L6 / L7

def test_a_failing_lifecycle_query_is_a_backoff_not_deleting():
    class G(tw.FakeGuard):
        fail = True

        def is_deleting(self, m):
            if self.fail:
                raise RuntimeError("pg down")
            return False

    r = tw.Rig([tw.make_node("a", 1000)])
    r.guard = G()
    r.worker = r.make()
    res = r.worker.step()
    assert res.state == sw.BACKOFF and r.worker.state == ""
    r.guard.fail = False
    r.drain()
    assert r.engine.batches == [["a"]]


def test_a_value_error_is_fenced_only_when_the_generation_really_moved():
    class S(tw.FakeStore):
        def save(self, *a, **k):
            raise ValueError("whatever the message says about a generation")

    # generation is the current one: not fenced, a retry
    r = tw.Rig([tw.make_node("a", 1000)], store=S(meta={"generation": 1, "params_hash": "h"}),
               cfg=sw.WorkerConfig(batch_size=10, min_save_interval_s=0, save_every_leaves=1))
    res = r.drain()
    assert res.state == sw.BACKOFF and r.worker.state == ""
    # another writer committed generation 2 since this worker started in 1
    r.store.meta_doc["generation"] = 2
    assert r.worker.step().state in (sw.BACKOFF, sw.FENCED)
    r.clock.advance(60)
    assert r.drain().state == sw.FENCED


# ---- L2 / L8

def test_stop_does_not_close_under_a_thread_that_is_still_stepping():
    import threading
    import time
    release = threading.Event()

    class Runner:
        def __init__(self):
            self.closed = []

        def run(self, sleep, should_stop):
            release.wait()
            self.closed.append("ran")

        def close(self):
            self.closed.append("close")

    class Guard:
        closed = 0

        def close(self):
            Guard.closed += 1

    runner = Runner()
    host = scene_host.SceneWorkerHost(runner, Guard())
    host.start()
    host.stop(timeout=0.2)
    assert runner.closed == [] and Guard.closed == 0          # caller thread closed nothing
    release.set()
    for _ in range(100):
        if Guard.closed:
            break
        time.sleep(0.02)
    assert runner.closed == ["ran", "close"] and Guard.closed == 1


def test_batch_min_above_max_is_clamped_with_a_warning(monkeypatch, caplog):
    from packages import config
    monkeypatch.setattr(config, "SCENE_GRAPH_BATCH_MIN_LEAVES", 100)
    monkeypatch.setattr(config, "SCENE_GRAPH_BATCH_MAX_LEAVES", 10)
    with caplog.at_level("WARNING"):
        cfg = scene_host.worker_config(SceneParams())
    assert (cfg.batch_min_leaves, cfg.batch_max_leaves) == (10, 10)
    assert any("BATCH_MIN_LEAVES" in r.message for r in caplog.records)


# ---- batch A: resume / discard of a pending generation against the REAL store

def _params_rebuild_worker(store, src, p2, clock, max_steps_cfg=None):
    return worker(store, src, ChartGraph(SceneParams()), new_engine=lambda: ChartGraph(p2), clock=clock,
                  cfg=max_steps_cfg or sw.WorkerConfig(
                      batch_size=4, min_save_interval_s=0, save_every_leaves=1, rebuild_batch=4,
                      rebuild_debounce_s=0, expected_params_hash=scene_host.expected_params_hash(p2)))


def test_a_rebuild_interrupted_mid_way_resumes_from_the_pending_cursor_real_store():
    store = real_store()
    src = ts.Source(NODES)
    clock = [ts.T0 + 10_000_000]
    drive(worker(store, src, ChartGraph(SceneParams()), clock=clock))
    p2 = SceneParams(robot_half_side=0.3)
    w1 = _params_rebuild_worker(store, src, p2, clock)
    for _ in range(40):                        # (scan, then sorted folds) until a pending save exists; the process dies
        w1.step()
        if w1.stats.saves >= 1:
            break
    pend = store.meta("m")["pending"]
    assert pend["cursor"] is not None and w1.stats.saves >= 1 and w1.stats.rebuilds == 0
    w1.guard.release("m")
    w2 = _params_rebuild_worker(store, src, p2, clock)
    w2.step()
    assert w2._resume_cursor is not None and tuple(w2._resume_cursor) == tuple(pend["cursor"])
    assert drive(w2).state == sw.IDLE
    assert w2.stats.rebuilds == 1 and store.meta("m")["generation"] == 2


def test_a_stale_pending_of_other_params_is_discarded_not_extended_real_store():
    store = real_store()
    src = ts.Source(NODES)
    clock = [ts.T0 + 10_000_000]
    drive(worker(store, src, ChartGraph(SceneParams()), clock=clock))
    p_old = SceneParams(robot_half_side=0.35)
    w1 = _params_rebuild_worker(store, src, p_old, clock)
    w1.step()
    stale_gen = store.meta("m")["pending"]["generation"]
    w1.guard.release("m")
    p2 = SceneParams(robot_half_side=0.3)
    w2 = _params_rebuild_worker(store, src, p2, clock)
    assert drive(w2).state == sw.IDLE
    meta = store.meta("m")
    assert w2.stats.rebuilds == 1 and meta["generation"] > stale_gen
    assert w2.stats.folded >= len(NODES) - 1       # folded from zero into an empty generation


# ---- engine contract: LeafError / EngineFaulted / begun_ms, against the REAL engine and store

def test_real_engine_rejects_an_oversize_leaf_by_key_and_the_worker_quarantines_it():
    import dataclasses
    import numpy as np
    huge = dataclasses.replace(LEAVES[0], key="huge", window_u8=np.full((500, 500), 255, np.uint8))
    nodes = NODES + [ts.node_of(huge, len(NODES))]
    by_key = dict(BY_KEY, huge=huge)
    events = []
    store = real_store()
    eng = ChartGraph(SceneParams())
    w = worker(store, ts.Source(nodes), eng, fetch=lambda m, k, layer: ts.png(by_key[k].window_u8),
               on_event=lambda k, p: events.append((k, p)),
               cfg=sw.WorkerConfig(batch_size=len(nodes), min_save_interval_s=0, save_every_leaves=1))
    assert drive(w).state == sw.IDLE
    assert eng.faulted is None and w.engine is eng            # a rejected leaf touches nothing
    assert w.stats.skipped[sw.SKIP_FOLD_ERROR] == 1
    assert any(k == "fold_failed" and p["stage"] == "poison_leaf" and "huge" in p["detail"] for k, p in events)
    assert w.stats.folded == len(LEAVES)
    assert ts.shape(store.load("m")) == ts.shape(ts.direct(LEAVES))


def test_real_engine_fault_is_recovered_by_reloading_from_the_real_store():
    half = len(NODES) // 2
    src = ts.Source(NODES[:half])
    store = real_store()
    loads = []

    def factory(v):
        loads.append(v)
        return store.load("m", v)
    cfg = sw.WorkerConfig(batch_size=10, min_save_interval_s=0, save_every_leaves=1)
    w = worker(store, src, ChartGraph(SceneParams()), engine_factory=factory, cfg=cfg)
    assert drive(w).state == sw.IDLE
    first = w.engine

    def boom(*a, **k):
        raise RuntimeError("solver blew up")
    first._mutate = boom
    for n in NODES[half:]:
        src.nodes[n["_key"]] = n
    res = w.step()
    assert res.state == sw.BACKOFF and first.faulted is not None
    assert w.engine is not first and w.engine.faulted is None and loads == [store.meta("m")["version"]]
    assert drive(w).state == sw.IDLE
    assert w.stats.fold_failures == 0
    assert ts.shape(store.load("m")) == ts.shape(ts.direct(LEAVES))


def test_real_store_resume_of_a_delete_triggered_rebuild_follows_begun_ms():
    from tests.unit.test_scene_worker import RequestGuard
    be = MemoryBackend()
    now = [ts.T0 + 10_000_000]
    store = SceneGraphStore(be, be, clock=lambda: now[0] / 1000.0)
    src = ts.Source(NODES)
    p2 = SceneParams(robot_half_side=0.3)
    clock = now

    def rebuilder(request):
        g = RequestGuard()
        g.request = request
        return _params_rebuild_worker_g(store, src, p2, clock, g)

    drive(worker(store, src, ChartGraph(SceneParams()), clock=clock))
    # a delete request made BEFORE the rebuild began; the worker's own wall clock is the store's
    import datetime
    at = datetime.datetime.fromtimestamp((now[0] - 5_000) / 1000.0, datetime.timezone.utc).isoformat()
    req = {"at": at, "reason": "nodes_deleted", "nodes": 1}
    w1 = rebuilder(req)
    for _ in range(40):
        w1.step()
        if w1.stats.saves >= 1:
            break
    pend = store.meta("m")["pending"]
    assert pend["begun_ms"] == now[0] and pend["cursor"] is not None and w1.stats.rebuilds == 0
    w1.guard.release("m")
    now[0] += 60_000                                  # young: resumed
    w2 = rebuilder(req)
    w2.step()
    assert w2._resume_cursor is not None and tuple(w2._resume_cursor) == tuple(pend["cursor"])
    w2.guard.release("m")
    now[0] += 1_000_000                               # older than rebuild_max_age_s: restarted from nothing
    w3 = rebuilder(req)
    w3.step()
    assert w3._resume_cursor is None
    assert drive(w3).state == sw.IDLE and w3.stats.rebuilds == 1


def _params_rebuild_worker_g(store, src, p2, clock, guard):
    return worker(store, src, ChartGraph(SceneParams()), new_engine=lambda: ChartGraph(p2), clock=clock, guard=guard,
                  cfg=sw.WorkerConfig(batch_size=4, min_save_interval_s=0, save_every_leaves=1, rebuild_batch=4,
                                      rebuild_debounce_s=0, expected_params_hash=scene_host.expected_params_hash(p2)))
