"""Ingest worker robustness (batch A): rebuild starvation, resume / fresh pending, load failure
classification, poison leaves, PNG expiry, durable skip cursor, replay margin, runner / health."""
import pytest

from packages.scene_graph.api import BBox, Dirty
from packages.services.graph_builder import scene_runner as sr
from packages.services.graph_builder import scene_worker as sw
from tests.unit.test_scene_worker import (FakeEngine, FakeGuard, FakeStore, Rig, make_node, MAP)


def _nodes(n, start=0, step=1000):
    return [make_node("n%03d" % i, 1000 + (start + i) * step) for i in range(n)]


class PendingStore(FakeStore):
    """FakeStore with the new store API: meta()["pending"], begin_generation(fresh=, params_hash=),
    load(pending=True)."""

    def __init__(self, pending=None, **kw):
        super().__init__(**kw)
        self.pending_meta = pending
        self.begin_calls = []
        self.loaded_pending = 0
        self.pending_engine = None

    def meta(self, map_id):
        out = super().meta(map_id)
        if self.pending_meta:
            out["pending"] = dict(self.pending_meta)
        return out

    def begin_generation(self, map_id, params_hash=None, fresh=False):
        self.begin_calls.append({"params_hash": params_hash, "fresh": fresh})
        if self.pending_meta and not fresh and self.pending_meta.get("params_hash") == params_hash:
            self.pending = self.pending_meta["generation"]
            return self.pending
        self.pending = self.meta_doc.get("generation", 1) + 1
        self.pending_meta = None
        return self.pending

    def load(self, map_id, version=None, tiles=None, pending=False):
        assert pending
        self.loaded_pending += 1
        return self.pending_engine


def _rebuild_rig(nodes, store=None, **cfgkw):
    store = store or FakeStore(version=5, meta={"params_hash": "old", "generation": 1})
    cfg = sw.WorkerConfig(expected_params_hash="new", min_save_interval_s=0, save_every_leaves=1,
                          rebuild_debounce_s=0, **cfgkw)
    fresh = FakeEngine()
    r = Rig(nodes, store=store, cfg=cfg, new_engine=lambda: fresh)
    r.fresh = fresh
    return r


# ---- 1. starvation

def test_rebuild_commits_under_a_steady_trickle_with_batch_min():
    r = _rebuild_rig(_nodes(10), batch_min_leaves=6, batch_max_leaves=48, rebuild_batch=6)
    r.cfg.min_save_interval_s = 5.0
    commits_at = None
    for i in range(60):
        r.source.add(make_node("t%03d" % i, 100_000 + i * 1000))     # a new upload every step
        r.clock.advance(1)
        r.worker.step()
        if r.store.commits and commits_at is None:
            commits_at = i
    assert r.store.commits, "the rebuild never committed under continuous uploads"
    assert commits_at < 30
    # leaves that arrived after the head was reached are folded afterwards, into the live engine
    assert r.worker._rebuild_gen is None
    assert r.fresh.batches, r.fresh.batches


def test_rebuild_max_age_forces_a_commit_when_the_scan_never_reaches_the_head():
    r = _rebuild_rig(_nodes(40), rebuild_max_age_s=30)
    r.cfg.batch_size = 2
    for i in range(80):
        r.source.add(make_node("u%03d" % i, 500_000 + i * 1000))
    # fold slower than arrival: every page is full, so the head is never reached by the scan alone
    for _ in range(6):
        r.worker.step()
    assert not r.store.commits
    r.clock.advance(31)
    for _ in range(4):
        r.worker.step()
    assert r.store.commits and r.worker._rebuild_gen is None


# ---- 2/3. resume / fresh pending

def test_params_rebuild_resumes_a_pending_generation_from_its_cursor():
    store = PendingStore(version=5, meta={"params_hash": "old", "generation": 1},
                         pending={"generation": 2, "version": 3, "params_hash": "new",
                                  "cursor": [4000, "n003"]})
    r = _rebuild_rig(_nodes(8), store=store)
    store.pending_engine = r.fresh
    r.drain()
    assert store.loaded_pending == 1
    assert store.begin_calls[0]["fresh"] is False
    assert r.worker.engine is r.fresh            # the restored engine, not a new empty one
    assert store.commits


def test_resume_scans_from_the_pending_cursor_not_zero():
    store = PendingStore(version=5, meta={"params_hash": "old", "generation": 1},
                         pending={"generation": 2, "version": 3, "params_hash": "new",
                                  "cursor": [500_000, "x"]})
    nodes = [make_node("old", 1000), make_node("late", 600_000)]
    r = _rebuild_rig(nodes, store=store)
    store.pending_engine = r.fresh
    r.drain()
    assert r.fresh.batches == [["late"]]         # "old" is behind the cursor (and the margin)


def test_stale_pending_with_other_params_is_discarded_fresh():
    store = PendingStore(version=5, meta={"params_hash": "old", "generation": 1},
                         pending={"generation": 2, "version": 3, "params_hash": "older",
                                  "cursor": [4000, "n003"]})
    r = _rebuild_rig(_nodes(3), store=store)
    r.drain()
    assert store.loaded_pending == 0
    assert store.begin_calls[0]["fresh"] is True
    assert r.fresh.batches == [["n000", "n001", "n002"]]


def test_delete_request_rebuild_never_resumes_pending():
    store = PendingStore(version=5, meta={"params_hash": "new", "generation": 1},
                         pending={"generation": 2, "version": 3, "params_hash": "new",
                                  "cursor": [4000, "n003"]})
    from tests.unit.test_scene_worker import RequestGuard, REQ
    r = _rebuild_rig(_nodes(3), store=store)
    r.guard = RequestGuard()
    r.guard.request = REQ
    r.worker = r.make()
    r.worker.step()
    assert store.begin_calls and store.begin_calls[0]["fresh"] is True


def test_old_store_api_clears_a_stale_pending_before_begin():
    class Docs:
        def __init__(self):
            self.doc = {"version": 5, "generation": 1, "pending": {"generation": 2, "version": 1,
                                                                   "manifest": "m"}}

        def meta(self, map_id):
            return dict(self.doc)

        def cas_meta(self, map_id, ver, new):
            self.doc = dict(new)
            return True

    class OldStore(FakeStore):
        def __init__(self):
            super().__init__(version=5, meta={"params_hash": "old", "generation": 1})
            self.docs = Docs()
            self.begun_with_pending = None

        def begin_generation(self, map_id):
            self.begun_with_pending = bool(self.docs.doc.get("pending"))
            return super().begin_generation(map_id)

    store = OldStore()
    r = _rebuild_rig(_nodes(2), store=store)
    r.drain()
    assert store.begun_with_pending is False
    assert r.fresh.batches == [["n000", "n001"]]


# ---- 4. engine load failures

class StateMismatch(Exception):
    pass


def _load_rig(exc, can_rebuild, **cfgkw):
    store = FakeStore(version=5, meta={"params_hash": "same", "generation": 1})
    cfg = sw.WorkerConfig(expected_params_hash="same", min_save_interval_s=0, rebuild_debounce_s=0,
                          load_fail_limit=3, **cfgkw)
    calls = {"n": 0}

    def factory(version):
        calls["n"] += 1
        raise exc

    fresh = FakeEngine()
    r = Rig(_nodes(2), store=store, cfg=cfg, engine_factory=factory,
            **({"new_engine": lambda: fresh} if can_rebuild else {}))
    r.worker.engine = None
    r.calls, r.fresh = calls, fresh
    return r


def _bare_worker(r):
    r.worker = sw.SceneWorker(MAP, source=r.source, fetch_png=r.fetch, footprint=lambda x: (0.8, 0.5),
                              store=r.store, cursor_store=r.cursors, guard=r.guard, config=r.cfg,
                              now_ms=r.clock, engine_factory=r.kw["engine_factory"],
                              new_engine=r.kw.get("new_engine"))


def test_deterministic_load_failure_falls_into_a_rebuild_after_the_limit():
    store = FakeStore(version=5, meta={"params_hash": "same", "generation": 1})
    cfg = sw.WorkerConfig(expected_params_hash="same", min_save_interval_s=0, rebuild_debounce_s=0,
                          load_fail_limit=3)
    calls = {"n": 0}

    def factory(version):
        calls["n"] += 1
        raise StateMismatch("restored keys differ")

    fresh = FakeEngine()
    r = Rig(_nodes(2), store=store, cfg=cfg, engine_factory=factory, new_engine=lambda: fresh)
    r.worker = sw.SceneWorker(MAP, source=r.source, fetch_png=r.fetch, footprint=lambda x: (0.8, 0.5),
                              store=store, cursor_store=r.cursors, guard=r.guard, config=cfg,
                              now_ms=r.clock, engine_factory=factory, new_engine=lambda: fresh)
    states = [r.worker.step().state for _ in range(3)]
    assert states[:2] == [sw.BACKOFF, sw.BACKOFF]
    assert calls["n"] == 3                       # not retried again after giving up
    r.drain()
    assert store.commits and fresh.batches
    assert calls["n"] == 3


def test_deterministic_load_failure_without_rebuild_is_terminal():
    store = FakeStore(version=5, meta={"params_hash": "same", "generation": 1})
    cfg = sw.WorkerConfig(expected_params_hash="same", load_fail_limit=2)

    def factory(version):
        raise FileNotFoundError("g1/t/abc")

    r = Rig([], store=store, cfg=cfg, engine_factory=factory)
    w = sw.SceneWorker(MAP, source=r.source, fetch_png=r.fetch, footprint=lambda x: (0.8, 0.5),
                       store=store, cursor_store=r.cursors, guard=r.guard, config=cfg,
                       now_ms=r.clock, engine_factory=factory)
    assert w.step().state == sw.BACKOFF
    assert w.step().state == sw.NEEDS_REBUILD
    assert not r.guard.locked


def test_transient_load_failure_keeps_retrying():
    store = FakeStore(version=5, meta={"params_hash": "same", "generation": 1})
    cfg = sw.WorkerConfig(expected_params_hash="same", load_fail_limit=2)

    def factory(version):
        raise ConnectionError("minio down")

    r = Rig([], store=store, cfg=cfg, engine_factory=factory)
    w = sw.SceneWorker(MAP, source=r.source, fetch_png=r.fetch, footprint=lambda x: (0.8, 0.5),
                       store=store, cursor_store=r.cursors, guard=r.guard, config=cfg,
                       now_ms=r.clock, engine_factory=factory, new_engine=lambda: FakeEngine())
    assert [w.step().state for _ in range(5)] == [sw.BACKOFF] * 5


# ---- 5. poison leaf

def test_poison_leaf_is_bisected_quarantined_and_the_rest_folded():
    events = []
    cfg = sw.WorkerConfig(min_save_interval_s=0, save_every_leaves=1)
    r = Rig(_nodes(6), cfg=cfg, on_event=lambda k, p: events.append((k, p)))
    real = r.engine.fold_many

    def picky(leaves):
        if any(l.key == "n002" for l in leaves):
            raise RuntimeError("cannot fold n002")
        return real(leaves)
    r.engine.fold_many = picky
    assert r.worker.step().state == sw.BACKOFF      # 1st failure
    r.clock.advance(11)
    r.drain()
    assert r.worker.stats.skipped[sw.SKIP_FOLD_ERROR] == 1
    folded = sorted(k for b in r.engine.batches for k in b)
    assert folded == ["n000", "n001", "n003", "n004", "n005"]
    assert r.worker.cursor == (1000 + 5 * 1000, "n005")      # the map moved on
    assert any(k == "fold_failed" and p["stage"] == "poison_leaf" and "n002" in p["detail"]
               for k, p in events)
    assert r.worker.stats.folded == 5


# ---- 6/7. png expiry

def test_expired_png_does_not_advance_the_cursor_past_buffered_leaves():
    cfg = sw.WorkerConfig(min_save_interval_s=100, save_every_leaves=1, batch_min_leaves=5,
                          png_expire_s=10.0, png_retry_s=1.0, replay_margin_ms=60_000)
    r = Rig([make_node("a", 1000), make_node("b", 2000), make_node("c", 3000)], cfg=cfg)
    r.no_png = frozenset({"a"})
    r.worker.step()                          # b, c buffered (batch_min 5), a pending
    assert r.worker.stats.pending == 1 and r.worker.stats.buffered_leaves == 2
    r.clock.advance(11)
    r.worker.step()                          # a expires: buffered as a terminal skip
    assert r.worker.stats.skipped[sw.SKIP_PNG_EXPIRED] == 1
    assert r.worker.cursor < (2000, "b"), r.worker.cursor    # b, c still wait: nothing passed them
    assert r.worker.stats.buffered_leaves == 2
    r.clock.advance(100)                     # the publish interval of a trickle is over
    r.drain()
    assert r.engine.batches == [["b", "c"]]
    assert r.worker.cursor == (3000, "c")


def test_minio_outage_time_is_not_counted_towards_png_expiry():
    cfg = sw.WorkerConfig(min_save_interval_s=0, png_expire_s=10.0, png_retry_s=1.0)
    r = Rig([make_node("a", 1000), make_node("b", 2000)], cfg=cfg)
    r.no_png = frozenset({"a"})
    r.worker.step()
    assert r.worker.stats.pending == 1
    outage = {"on": True}
    real = r.fetch

    def fetch(map_id, key, layer):
        if outage["on"]:
            raise OSError("minio down")
        return real(map_id, key, layer)
    r.worker.fetch_png = fetch
    r.clock.advance(2)
    assert r.worker.step().state == sw.BACKOFF
    r.clock.advance(60)                      # a long outage
    assert r.worker.step().state == sw.BACKOFF
    assert r.worker.stats.skipped[sw.SKIP_PNG_EXPIRED] == 0
    outage["on"] = False
    r.clock.advance(1)
    r.worker.step()
    assert r.worker.stats.skipped[sw.SKIP_PNG_EXPIRED] == 0 and r.worker.stats.pending == 1
    r.clock.advance(11)                      # now it really waited 10 s
    r.drain()
    assert r.worker.stats.skipped[sw.SKIP_PNG_EXPIRED] == 1


def test_expiry_makes_one_last_fetch_first():
    cfg = sw.WorkerConfig(min_save_interval_s=0, png_expire_s=10.0, png_retry_s=100.0)
    r = Rig([make_node("a", 1000)], cfg=cfg)
    r.no_png = frozenset({"a"})
    r.worker.step()
    r.no_png = frozenset()                   # it landed, but the retry interval has not passed
    r.clock.advance(11)
    r.drain()
    assert r.engine.batches == [["a"]] and r.worker.stats.skipped[sw.SKIP_PNG_EXPIRED] == 0


def test_png_retries_per_step_are_capped():
    cfg = sw.WorkerConfig(min_save_interval_s=0, png_retry_s=1.0, png_expire_s=1000.0,
                          png_retries_per_step=2)
    r = Rig(_nodes(6), cfg=cfg)
    r.no_png = frozenset(n["_key"] for n in _nodes(6))
    r.worker.step()
    assert r.worker.stats.pending == 6
    asked = []
    real = r.fetch
    r.worker.fetch_png = lambda m, k, l: (asked.append(k), real(m, k, l))[1]
    r.clock.advance(2)
    r.worker.step()
    assert len(asked) == 2                  # capped; the others wait for the next step
    r.clock.advance(2)
    r.worker.step()
    assert len(asked) == 4


# ---- 8. durable skip-only cursor

def test_skip_only_progress_is_persisted_through_a_cursor_only_save():
    cfg = sw.WorkerConfig(min_save_interval_s=0, save_every_leaves=1)
    skip = [make_node("s%03d" % i, 10_000 + i * 20_000, costmap={}) for i in range(8)]
    r = Rig(skip, cfg=cfg)
    r.store.meta_doc["cursor"] = [0, ""]
    r.drain()
    assert r.worker.stats.skipped["no_layer"] if "no_layer" in r.worker.stats.skipped else r.worker.stats.skipped
    assert r.store.saves, "skip-only progress was never made durable"
    assert r.store.saves[-1][1][0] > 60_000
    assert r.store.saves[-1][2] == []         # no leaves in it


def test_cursor_only_save_not_made_before_the_margin():
    cfg = sw.WorkerConfig(min_save_interval_s=0)
    r = Rig([make_node("s0", 10_000, costmap={}), make_node("s1", 20_000, costmap={})], cfg=cfg)
    r.drain()
    assert r.store.saves == []


# ---- 9. fenced commit

def test_commit_generation_save_fenced_ends_fenced():
    r = _rebuild_rig(_nodes(2))

    def boom(map_id, gen):
        raise sw.SaveFenced("lost the compare-and-set")
    r.store.commit_generation = boom
    res = r.drain(20)
    assert res.state == sw.FENCED and r.worker.state == sw.FENCED


# ---- 10. monotonic clock

def test_intervals_follow_the_monotonic_clock_not_the_wall_clock():
    wall = {"t": 1_000_000}
    mono = {"t": 5_000}
    cfg = sw.WorkerConfig(min_save_interval_s=0, png_expire_s=10.0, png_retry_s=1.0)
    r = Rig([make_node("a", 1000)], cfg=cfg)
    r.no_png = frozenset({"a"})
    r.worker = sw.SceneWorker(MAP, source=r.source, fetch_png=r.fetch, footprint=lambda x: (0.8, 0.5),
                              engine=r.engine, store=r.store, cursor_store=r.cursors, guard=r.guard,
                              config=cfg, now_ms=lambda: wall["t"], mono_ms=lambda: mono["t"])
    r.worker.step()
    wall["t"] += 3_600_000                   # the wall clock jumps an hour (NTP step)
    r.worker.step()
    assert r.worker.stats.skipped[sw.SKIP_PNG_EXPIRED] == 0
    mono["t"] += 11_000
    r.worker.step()
    assert r.worker.stats.skipped[sw.SKIP_PNG_EXPIRED] == 1


# ---- 11. log once

def test_rebuild_request_without_new_engine_logs_once(caplog):
    from tests.unit.test_scene_worker import RequestGuard, REQ
    store = FakeStore(version=5, meta={"params_hash": "same", "generation": 1})
    cfg = sw.WorkerConfig(expected_params_hash="same", rebuild_check_s=1.0, rebuild_debounce_s=0)
    r = Rig([], store=store, cfg=cfg)
    r.guard = RequestGuard()
    r.worker = r.make()
    r.worker.step()
    r.guard.request = REQ
    with caplog.at_level("WARNING"):
        for _ in range(4):
            r.clock.advance(2)
            r.worker.step()
    msgs = [m for m in caplog.messages if "cannot rebuild" in m]
    assert len(msgs) == 1


# ---- 12. replay margin on idle polls

def test_idle_polls_do_not_reread_the_margin_every_time():
    cfg = sw.WorkerConfig(min_save_interval_s=0, replay_margin_ms=60_000, replay_recheck_s=60.0,
                          poll_margin_ms=1000)
    r = Rig([make_node("a", 200_000)], cfg=cfg)
    r.drain()
    r.source.calls.clear()
    for _ in range(5):
        r.worker.step()
    assert r.source.calls and all(c[0] >= 200_000 - 1000 for c in r.source.calls), r.source.calls
    r.clock.advance(61)
    r.source.calls.clear()
    r.worker.step()
    assert r.source.calls[0][0] == 140_000        # the full margin once per recheck interval


# ---- debounce

def test_delete_requests_are_debounced_until_quiet():
    from tests.unit.test_scene_worker import RequestGuard
    store = FakeStore(version=5, meta={"params_hash": "same", "generation": 1})
    cfg = sw.WorkerConfig(expected_params_hash="same", rebuild_check_s=5.0, rebuild_debounce_s=20.0,
                          min_save_interval_s=0)
    fresh = FakeEngine()
    r = Rig(_nodes(2), store=store, cfg=cfg, new_engine=lambda: fresh)
    r.guard = RequestGuard()
    r.worker = r.make()
    r.drain()
    for i in range(4):                       # a burst: a newer request every 6 s
        r.guard.request = {"at": "2026-10-12T10:00:%02d+00:00" % i, "reason": "nodes_deleted", "nodes": 1}
        r.clock.advance(6)
        r.worker.step()
    assert not store.commits and r.worker._rebuild_gen is None
    for _ in range(5):                       # quiet
        r.clock.advance(6)
        r.worker.step()
    assert r.worker._rebuild_gen is not None or store.commits


# ---- throttle / stats

def test_updated_events_are_throttled_per_map():
    events = []
    cfg = sw.WorkerConfig(min_save_interval_s=0, save_every_leaves=1, updated_event_interval_s=60.0)
    r = Rig([], cfg=cfg, on_event=lambda k, p: events.append((k, p)))
    for i in range(5):
        r.source.add(make_node("k%d" % i, 1000 + i * 1000))
        r.clock.advance(5)
        r.drain()
    assert len([1 for k, _ in events if k == "updated"]) == 1
    r.clock.advance(61)
    r.source.add(make_node("z", 900_000))
    r.drain()
    upd = [p for k, p in events if k == "updated"]
    assert len(upd) == 2 and upd[1]["leaves_folded"] == 5


def test_step_budget_stops_reading_but_continues():
    cfg = sw.WorkerConfig(min_save_interval_s=0, step_budget_s=1.0, batch_size=50)
    r = Rig(_nodes(6), cfg=cfg)
    real = r.fetch

    def slow(m, k, l):
        r.clock.advance(0.6)
        return real(m, k, l)
    r.worker.fetch_png = slow
    res = r.worker.step()
    assert res.state == sw.WORKED
    r.drain(20)
    assert sorted(k for b in r.engine.batches for k in b) == ["n%03d" % i for i in range(6)]


# ---- runner / health / guard / host

class _W:
    def __init__(self, map_id, state):
        self.map_id, self.state = map_id, state
        self.stats = sw.SceneWorkerStats()

    def step(self):
        return sw.StepResult(self.state)

    def close(self):
        pass


def test_not_owner_workers_do_not_count_toward_max_maps_and_retry_slowly():
    t = {"v": 0.0}
    states = {"a": sw.NOT_OWNER, "b": sw.NOT_OWNER, "c": sw.IDLE}
    cfg = sr.RunnerConfig(max_maps=1, not_owner_retry_s=30.0)
    runner = sr.SceneRunner(lambda: ["a", "b", "c"], lambda m: _W(m, states[m]), cfg, now=lambda: t["v"])
    runner.tick()
    assert runner.last_state == {"a": sw.NOT_OWNER, "b": sw.NOT_OWNER, "c": sw.IDLE}
    assert runner._due["a"] == 30.0
    snap = runner.snapshot()
    assert snap["maps"]["c"]["state"] == sw.IDLE and snap["maps"]["a"]["last_step_age_s"] == 0.0


def test_host_state_reports_running_and_runner_snapshot():
    from packages.services.graph_builder.scene_host import SceneWorkerHost

    class R:
        def snapshot(self):
            return {"maps": {"m": {"state": "idle"}}}

        def run(self, sleep, stop):
            pass

        def close(self):
            pass
    h = SceneWorkerHost(R())
    assert h.state() == {"running": False, "maps": {"m": {"state": "idle"}}}


def test_guard_connect_has_a_timeout(monkeypatch):
    import sys
    import types
    seen = {}
    fake = types.SimpleNamespace(connect=lambda conninfo, **kw: seen.update(kw) or object())
    monkeypatch.setitem(sys.modules, "psycopg", fake)
    from packages.services.graph_builder import scene_guard
    scene_guard.connect_from_conninfo("dbname=x")()
    assert seen["connect_timeout"] == 5 and seen["autocommit"] is True
