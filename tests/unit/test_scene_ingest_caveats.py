"""Known ingest caveats, against the REAL SceneGraphStore on the in-memory backend: a failing
`commit_generation` is classified (not retried forever), an empty rebuild terminates, and map names
with hyphens / capitals reach the registry (scene_guard)."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_scene_smoke as ts            # noqa: E402  (skips the module when the engine is absent)
import test_scene_review_fixes as rf     # noqa: E402
import test_scene_guard as tg            # noqa: E402

from packages.scene_graph.api import SceneParams                          # noqa: E402
from packages.scene_graph.engine import ChartGraph                        # noqa: E402
from packages.services.graph_builder import scene_guard                   # noqa: E402
from packages.services.graph_builder import scene_worker as sw            # noqa: E402


class RequestGuard(ts.Guard):
    """A guard with a rebuild request that is cleared by `clear_rebuild_request`."""
    def __init__(self, request=None):
        self.request, self.cleared = request, []

    def rebuild_request(self, m):
        return self.request

    def clear_rebuild_request(self, m, at):
        self.cleared.append(at)
        if self.request and self.request.get("at") == at:
            self.request = None


def _cfg(**kw):
    kw.setdefault("rebuild_check_s", 0.001)
    return sw.WorkerConfig(batch_size=10, min_save_interval_s=0, save_every_leaves=1, **kw)


def _built_store():
    store = rf.real_store()
    rf.drive(rf.worker(store, ts.Source(rf.NODES), ChartGraph(SceneParams())))
    return store


def _rebuilding_worker(store, src, guard, events=None, **cfg):
    return rf.worker(store, src, engine_factory=lambda v: store.load("m", v), guard=guard,
                     new_engine=lambda: ChartGraph(SceneParams()), cfg=_cfg(**cfg),
                     on_event=(lambda k, p: events.append((k, p))) if events is not None else None)


def settle(w, n=40):
    """Step a fixed number of times (an empty rebuild needs several caught-up steps)."""
    res = None
    for _ in range(n):
        res = w.step()
        if res.state in sw.TERMINAL:
            break
    return res


def _request():
    return {"at": "2026-10-12T00:00:00", "reason": "nodes_deleted", "nodes": 1}


# ---- 1. commit_generation failures are classified

def test_vanished_pending_generation_restarts_the_rebuild_and_commits():
    store = _built_store()
    src = ts.Source(rf.NODES)
    events = []
    w = _rebuilding_worker(store, src, RequestGuard(_request()), events)
    real_commit, calls = store.commit_generation, []

    def commit(map_id, gen):
        calls.append(gen)
        if len(calls) == 1:               # another process abandoned the pending generation
            meta = dict(store.docs.meta(map_id))
            meta.pop("pending")
            assert store.docs.cas_meta(map_id, meta["version"], meta)
        return real_commit(map_id, gen)

    store.commit_generation = commit
    res = rf.drive(w, 1000)
    assert res.state == sw.IDLE, res
    assert len(calls) == 2 and w.stats.rebuilds == 1          # restarted, then committed
    assert store.meta("m")["generation"] == 2
    assert ("rebuild_completed" in [k for k, _ in events])


def test_commit_with_a_generation_already_live_is_fenced_not_retried():
    store = _built_store()
    w = _rebuilding_worker(store, ts.Source(rf.NODES), RequestGuard(_request()))
    real_commit = store.commit_generation

    def commit(map_id, gen):
        real_commit(map_id, gen)          # "another writer" commits it first ...
        return real_commit(map_id, gen)   # ... and ours finds no pending generation

    store.commit_generation = commit
    res = rf.drive(w, 1000)
    assert res.state == sw.FENCED and "committed or superseded" in res.detail


def test_transient_commit_failure_is_bounded_then_terminal():
    store = _built_store()
    w = _rebuilding_worker(store, ts.Source(rf.NODES), RequestGuard(_request()), commit_retries=3)
    store.commit_generation = lambda m, g: (_ for _ in ()).throw(ValueError("store hiccup"))
    states = []
    for _ in range(1000):
        r = w.step()
        states.append(r.state)
        if r.state in sw.TERMINAL:
            break
    assert r.state == sw.FENCED and "failed 3 times" in r.detail
    assert states.count(sw.BACKOFF) == 2


def test_transient_commit_failure_recovers_without_a_restart():
    store = _built_store()
    w = _rebuilding_worker(store, ts.Source(rf.NODES), RequestGuard(_request()), commit_retries=3)
    real, n = store.commit_generation, [0]

    def flaky(m, g):
        n[0] += 1
        if n[0] == 1:
            raise OSError("minio down")
        return real(m, g)

    store.commit_generation = flaky
    assert rf.drive(w, 1000).state == sw.IDLE and w.stats.rebuilds == 1


# ---- 2. an empty map rebuild terminates

def test_rebuild_of_an_empty_map_drops_the_stale_graph_and_clears_the_request():
    store = _built_store()
    assert store.meta("m")
    g = RequestGuard(_request())
    events, published = [], []
    w = _rebuilding_worker(store, ts.Source([]), g, events, empty_rebuild_confirmations=2)
    w.publish = lambda v, d: published.append(v)
    res = settle(w)
    assert res.state == sw.IDLE, res
    assert g.cleared == [_request()["at"]] and g.request is None
    assert not store.meta("m")                    # nothing stale left to read
    assert w.stats.rebuilds == 1 and published == [0]
    assert [k for k, _ in events] == ["rebuild_started", "rebuild_completed"]
    # the request is not re-answered on later steps
    assert w.step().state == sw.IDLE and len(events) == 2


def test_empty_rebuild_then_new_leaves_build_a_fresh_first_generation():
    store = _built_store()
    src = ts.Source([])
    w = _rebuilding_worker(store, src, RequestGuard(_request()))
    assert settle(w).state == sw.IDLE and not store.meta("m")
    src.nodes.update({n["_key"]: dict(n, scene_stamp_ms=n["costmap_stored_ms"]) for n in rf.NODES})
    res = rf.drive(w, 500)
    assert res.state == sw.IDLE, res
    meta = store.meta("m")
    assert meta and meta["generation"] == 1 and meta["version"] >= 1


def test_empty_rebuild_waits_for_a_second_caught_up_step():
    store = _built_store()
    w = _rebuilding_worker(store, ts.Source([]), RequestGuard(_request()), empty_rebuild_confirmations=3)
    w.step(); w.step()
    assert store.meta("m")                # not yet
    settle(w)
    assert not store.meta("m")


# ---- 3. map names with hyphens / capitals

class _Rows:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows


class _Conn:
    """A registry with the given names; evaluates the slug match the SQL does."""
    def __init__(self, names):
        self.names, self.closed = names, False

    def execute(self, sql, params=None):
        assert "lower(replace(name" in sql
        slugs = set(params[0])
        return _Rows([(n,) for n in self.names if n.lower().replace("_", "-") in slugs])

    def close(self):
        self.closed = True


def _registered(names, candidates):
    return scene_guard.PgGuard(lambda: _Conn(names)).registered_maps(candidates)


def test_registered_maps_resolves_a_hyphenated_name_from_the_lossy_bucket_listing():
    # image_db.list_maps() turns bucket `map-my-map` into `my_map`; the registry name is `my-map`
    assert _registered(["my-map", "other"], ["my_map", "other", "junk"]) == ["my-map", "other"]


def test_registered_maps_resolves_capitals_and_keeps_exact_underscore_names():
    assert _registered(["Site_A", "plain_name"], ["site_a", "plain_name"]) == ["Site_A", "plain_name"]


def test_registered_maps_skips_an_ambiguous_bucket_but_prefers_the_exact_name():
    # `a_b` and `a-b` share the bucket `map-a-b`: the exact one stays, the other is never listed
    assert _registered(["a_b", "a-b"], ["a_b"]) == ["a_b"]
    assert _registered(["a-b", "a-B"], ["a_b"]) == []


def test_worker_lock_and_store_use_the_real_name():
    # the lock key and the store/collection names are built from the registry name, not the bucket slug
    assert scene_guard.lock_key("my-map") != scene_guard.lock_key("my_map")
    store = rf.real_store()
    assert store.meta("my-map") == {}
