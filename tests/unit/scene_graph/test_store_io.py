"""Store write path: the index swap is atomic, and existence checks are only made where they can matter."""
import copy
import os

import pytest

from packages.scene_graph.store_backends import ArangoMinioBackend, MemoryBackend

from .test_store import _FakeDb

pytestmark = pytest.mark.unit


class _Txn:
    """Stream transaction over a snapshot of the named collections; commit swaps them in, abort drops them."""

    def __init__(self, db, write, fail_after=None):
        self.db, self.write, self.snap = db, write, {n: copy.deepcopy(db.cols[n]) for n in write}
        self.fail_after, self.n, self.aql = fail_after, 0, self

    def execute(self, q, bind_vars=None):
        self.n += 1
        if self.fail_after is not None and self.n > self.fail_after: raise RuntimeError("crash inside the transaction")
        c = bind_vars["@c"]
        if q.startswith("FOR d IN @@c REMOVE"): self.snap[c].clear()
        elif q.startswith("FOR d IN @docs INSERT"):
            for d in bind_vars["docs"]: self.snap[c][d["_key"]] = copy.deepcopy(d)
        elif q.startswith("UPDATE 'meta'"): self.snap[c]["meta"]["index_version"] = bind_vars["v"]
        else: raise AssertionError(q)
        return iter([])

    def commit_transaction(self):
        for n in self.write: self.db.cols[n] = self.snap[n]
        self.db.committed += 1

    def abort_transaction(self): self.db.aborted += 1


class _TxnDb(_FakeDb):
    def __init__(self):
        super().__init__(); self.fail_after = None; self.committed = self.aborted = 0

    def begin_transaction(self, write=None):
        return _Txn(self, write, self.fail_after)


def _backend():
    db = _TxnDb()
    c = type("C", (), {})(); c.graph = type("G", (), {"db": db})(); c.image = None
    return ArangoMinioBackend(c), db


A = lambda v, n=3: [{"key": f"c{i}", "cells": i, "area": 1.0} for i in range(n)]
L = lambda v: [{"a": "c0", "b": "c1", "length": float(v), "width": 1.0, "channels": {}}]


def test_index_swap_is_one_transaction_and_replaces_everything():
    be, db = _backend()
    be.cas_meta("m", None, {"version": 1})
    be.put_index("m", 1, A(1, 3), L(1))
    be.put_index("m", 2, A(2, 2), L(2))
    assert db.committed == 2 and db.aborted == 0
    assert sorted(db.cols["sg_anchors_m"]) == ["c0", "c1"] and {d["version"] for d in db.cols["sg_anchors_m"].values()} == {2}
    assert {d["version"] for d in db.cols["sg_links_m"].values()} == {2}
    assert be.index_version("m") == 2


@pytest.mark.parametrize("after", [0, 1, 2, 3])
def test_a_crash_inside_the_swap_leaves_the_previous_index_whole(after):
    be, db = _backend()
    be.cas_meta("m", None, {"version": 1})
    be.put_index("m", 1, A(1, 3), L(1))
    before = copy.deepcopy(db.cols)
    db.fail_after = after
    with pytest.raises(RuntimeError):
        be.put_index("m", 2, A(2, 2), L(2))
    assert db.aborted == 1 and db.cols == before and be.index_version("m") == 1


def test_memory_backend_semantics_unchanged():
    mb = MemoryBackend()
    mb.put_index("m", 3, [{"key": "a"}], [])
    assert mb.index_version("m") == 3


@pytest.mark.skipif(not os.path.exists(os.path.expanduser("~/.sg_test_arango.env")), reason="no throw-away ArangoDB")
def test_real_arango_reader_never_sees_an_empty_index():
    """Runs tools/scene_graph_sim/smoke_index_swap.py logic against the throw-away ArangoDB (127.0.0.1:18529) when it is up."""
    import subprocess, sys
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    try:
        r = subprocess.run([sys.executable, os.path.join(root, "tools/scene_graph_sim/smoke_index_swap.py")], capture_output=True,
                           text=True, timeout=60, env={**os.environ, "ROUNDS": "10"})
    except Exception as e:                                       # server not reachable: not this test's business
        pytest.skip(str(e))
    if r.returncode != 0 and "Connection" in (r.stderr + r.stdout):
        pytest.skip("throw-away ArangoDB not reachable")
    assert r.returncode == 0, r.stderr[-400:]
    assert "min anchors seen 300 (expect 300)" in r.stdout and "index_version has no docs: 0" in r.stdout, r.stdout


# ---- existence checks -------------------------------------------------------------------------------------------------
class _Counting(MemoryBackend):
    def __init__(self):
        super().__init__(); self.heads = 0
    def exists(self, map_id, key):
        self.heads += 1
        return super().exists(map_id, key)


def _grow_counting(batch=6, leaves_from=None):
    from packages.scene_graph.api import SceneParams
    from packages.scene_graph.engine import ChartGraph
    from packages.scene_graph.store import SceneGraphStore
    from . import chart_worlds as cw
    w, way = cw.two_rooms(); leaves = cw.drive(w, way)
    b = _Counting(); st = SceneGraphStore(b, b, clock=lambda: 1.0)
    g = ChartGraph(SceneParams()); v = None
    for i in range(0, len(leaves), batch):
        v = st.save("m", g, g.fold_many(leaves[i:i + batch]), v, {"i": i})
    return st, b, g, v


def test_save_makes_no_existence_check_and_state_is_unchanged():
    st, b, g, v = _grow_counting()
    assert b.heads == 0                                         # was one HEAD per new object per save
    from packages.scene_graph.api import BBox
    big = BBox(-5, -5, 20, 20)
    assert sorted(c.key for c in st.load("m").query(big, raster=False).charts) == sorted(c.key for c in g.query(big, raster=False).charts)
    assert st.gc("m")["kept_versions"]                           # nothing referenced went missing


def test_a_retry_after_a_failed_commit_puts_again_and_ends_consistent():
    """no memo of uncommitted puts: another process's gc may have collected them in between"""
    from packages.scene_graph.api import SceneParams
    from packages.scene_graph.engine import ChartGraph
    from packages.scene_graph.store import SceneGraphStore
    from . import chart_worlds as cw
    w, way = cw.two_rooms(); leaves = cw.drive(w, way)
    b = _Counting(); st = SceneGraphStore(b, b, clock=lambda: 1.0)
    g = ChartGraph(SceneParams()); d = g.fold_many(leaves[:8])
    b.fail_on = lambda kind, name: (_ for _ in ()).throw(RuntimeError("crash")) if kind == "meta" else None
    with pytest.raises(RuntimeError):
        st.save("m", g, d, None, {})
    st.gc("m")                                                  # the orphans of the failed attempt are collected
    b.fail_on = None
    assert st.save("m", g, d, None, {}) == 1                    # and the retry still commits a complete version
    assert b.heads == 0
    assert all(b.exists("m", "g1/p/%s" % h) for h in st.manifest("m")["pages"].values())
