"""A rebuild folds its leaves in the canonical order: sorted by (stamp_ms, key), fixed batches of `rebuild_batch`
(48), whatever order the nodes were scanned in; memory stays at node records until a batch is built; a rebuild
resumed after a crash continues the same sequence."""
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_scene_worker import make_node           # noqa: E402
from test_scene_worker_robustness import _rebuild_rig   # noqa: E402

from packages.services.graph_builder import scene_worker as sw   # noqa: E402


def _with_state(engine):
    """FakeEngine + export_state() listing the leaves it folded (what the real engine's state holds)."""
    from types import SimpleNamespace as NS
    held = {}
    fold = engine.fold_many

    def fold_many(leaves):
        out = fold(leaves)
        for l in leaves:
            held[l.key] = NS(key=l.key, t=l.stamp_ms, session=l.session, x=l.x, y=l.y, r_d=l.r_d, view=None)
        return out
    engine.fold_many = fold_many
    engine.export_state = lambda: NS(leaves=list(held.values()))
    return engine


def _stamped(n, stored_of):
    """n leaves whose stamps are distinct and unrelated to key order; `stored_of(i)` their stored_ms."""
    rng = random.Random(7)
    stamps = rng.sample(range(1_000_000, 9_000_000), n)
    return [make_node("k%03d" % i, stored_of(i), scene_stamp_ms=stamps[i]) for i in range(n)], stamps


def _run(nodes, **kw):
    kw.setdefault("rebuild_batch", 48)
    kw.setdefault("batch_size", 25)
    r = _rebuild_rig(nodes, **kw)
    for _ in range(400):
        res = r.worker.step()
        if r.store.commits:
            break
        assert res.state not in sw.TERMINAL
    assert r.store.commits
    return r


def test_batches_of_48_sorted_by_stamp_then_key_whatever_the_scan_order():
    n = 130
    results = []
    for name, stored_of in (("ascending", lambda i: 1000 + i * 10), ("descending", lambda i: 1000 + (n - i) * 10),
                            ("shuffled", None)):
        if stored_of is None:
            perm = random.Random(3).sample(range(n), n)
            stored_of = lambda i, perm=perm: 1000 + perm[i] * 10
        nodes, stamps = _stamped(n, stored_of)
        r = _run(nodes)
        results.append(r.fresh.batches)
    want = [k for _, k in sorted((s, "k%03d" % i) for i, s in enumerate(_stamped(n, lambda i: 0)[1]))]
    for batches in results:
        assert [len(b) for b in batches] == [48, 48, 34]
        assert [k for b in batches for k in b] == want


def test_equal_stamps_fall_back_to_the_key():
    nodes = [make_node("z", 1000), make_node("a", 3000), make_node("m", 2000)]    # one stamp (the receipt) for all
    assert _run(nodes, rebuild_batch=2).fresh.batches == [["a", "m"], ["z"]]


def test_the_scan_fetches_no_png_and_one_batch_of_leaves_is_resident_at_most():
    n = 600
    nodes, _ = _stamped(n, lambda i: 1000 + i)
    r = _rebuild_rig(nodes, rebuild_batch=48, batch_size=50)
    fetched, peak = [], 0
    real = r.worker.fetch_png

    def counting(map_id, key, layer):
        fetched.append(key)
        return real(map_id, key, layer)
    r.worker.fetch_png = counting
    sealed_at = None
    for i in range(1000):
        res = r.worker.step()
        peak = max(peak, r.worker._leaf_count())
        if r.worker._plan is not None and sealed_at is None:
            sealed_at = len(fetched)
        if r.store.commits or res.state in sw.TERMINAL:
            break
    assert r.store.commits and r.worker.stats.folded == n
    assert sealed_at is not None and sealed_at <= 48        # nothing but the first batch was fetched in the scan step
    assert peak <= 48
    assert sorted(fetched) == sorted(set(fetched)) and len(fetched) == n   # each PNG once


def test_resume_after_a_kill_continues_the_same_sorted_sequence_and_skips_what_the_pending_holds():
    n = 100
    nodes, _ = _stamped(n, lambda i: 1000 + (i * 37) % n * 10)
    ref = _run(nodes, rebuild_batch=10, batch_size=20)
    want = [k for b in ref.fresh.batches for k in b]
    assert len(want) == n

    from test_scene_worker_robustness import PendingStore
    store = PendingStore(version=5, meta={"params_hash": "old", "generation": 1})
    r = _rebuild_rig(nodes, store=store, rebuild_batch=10, batch_size=20)
    _with_state(r.fresh)
    for _ in range(100):                         # fold a few batches into the pending generation, then die
        r.worker.step()
        if r.fresh.batches and len(r.fresh.batches) >= 4:
            break
    first = [k for b in r.fresh.batches for k in b]
    assert first == want[:len(first)] and 0 < len(first) < n
    cursor = store.pending_saves[-1][0]
    # the floor: everything below it is folded
    folded_ms = {k: nd["costmap_stored_ms"] for nd in nodes for k in [nd["_key"]]}
    unfolded = [folded_ms[k] for k in want[len(first):]]
    assert cursor[0] <= min(unfolded)
    assert store.commits == []

    # a new process resumes from the pending generation (its engine holds the first leaves)
    store.pending_meta = {"generation": store.pending, "version": 1, "params_hash": "new",
                          "cursor": list(cursor), "begun_ms": 1}
    r2 = _rebuild_rig(nodes, store=store, rebuild_batch=10, batch_size=20)
    store.pending_engine = r.fresh               # what load(pending=True) restores
    r2.fresh = r.fresh
    r2.worker.new_engine = lambda: r.fresh
    for _ in range(400):
        res = r2.worker.step()
        if store.commits or res.state in sw.TERMINAL:
            break
    assert store.commits
    got = [k for b in r.fresh.batches for k in b]
    assert got == want                           # each leaf once, in the canonical order
