"""Per-save cost, leaf ordering, parents-free queries and memory estimates (LACI's second review, items 2 and 6)."""
import dataclasses

import numpy as np
import pytest

import packages.scene_graph.store as S
from packages.scene_graph.api import BBox, SceneParams
from packages.scene_graph.engine import ChartGraph
from packages.scene_graph.store import MemoryBackend, SceneGraphStore

from . import chart_worlds as cw

pytestmark = pytest.mark.unit
BIG = BBox(-5, -5, 60, 20)


@pytest.fixture(scope="module")
def hall():
    w, way = cw.two_rooms(W=30.0)
    return w, cw.drive(w, way)


def test_a_save_that_changes_one_region_packs_and_checks_only_that_much(hall, monkeypatch):
    w, leaves = hall
    monkeypatch.setattr(S, "SUPER", 1)                    # one page per tile: several pages without a 100 m hall
    be = MemoryBackend(); st = SceneGraphStore(be, be, verify_every=0)
    g = ChartGraph(SceneParams()); v = None
    for i in range(0, len(leaves) - 6, 6):
        v = st.save("m", g, g.fold_many(leaves[i:i + 6]), v, {"i": i})
    n_pages = len(st.manifest("m")["pages"])
    assert n_pages >= 3
    calls = {"pack_json": 0, "exists": 0}
    real, ex = S.pack_json, be.exists
    monkeypatch.setattr(S, "pack_json", lambda o: (calls.__setitem__("pack_json", calls["pack_json"] + 1), real(o))[1])
    monkeypatch.setattr(be, "exists", lambda m, k: (calls.__setitem__("exists", calls["exists"] + 1), ex(m, k))[1])
    d = g.fold_many(leaves[-6:])                          # the last leaves: the far end of the hall only
    v2 = st.save("m", g, d, v, {"i": 999})
    assert v2 == v + 1
    assert calls["pack_json"] < n_pages                   # untouched pages were reused by content, not re-packed
    assert calls["exists"] <= calls["pack_json"] + 12     # and not probed: only what is new is checked
    # exactness: the page table equals the one a from-scratch save of the same engine writes
    ref_b = MemoryBackend(); ref = SceneGraphStore(ref_b, ref_b, verify_every=0)
    ref.save("m", g, d, None, {"i": 999})
    assert st.manifest("m")["pages"] == ref.manifest("m")["pages"]
    assert st.manifest("m")["tiles"] == ref.manifest("m")["tiles"]


def test_leaf_order_is_incremental_and_equals_a_sort(hall):
    w, leaves = hall
    g = ChartGraph(SceneParams())
    g.fold_many(leaves[::3]); g.fold_many(leaves[1::3]); g.fold_many(leaves[2::3])   # out-of-order arrival across batches
    g.fold_many([dataclasses.replace(leaves[5], stamp_ms=leaves[5].stamp_ms + 500_000)])   # a leaf re-folded with a new stamp
    got = [(l.t, l.key) for l in g.export_state().leaves]
    assert got == sorted(got) and len({k for _, k in got}) == len(got) == len(leaves)
    again = g.export_state().leaves
    assert all(a is b for a, b in zip(g.export_state().leaves, again))      # records are reused, not rebuilt
    g.refold([l for l in leaves[:20] if l.key != leaves[3].key], BBox(-5, -5, 12, 20))
    assert leaves[3].key not in {l.key for l in g.export_state().leaves}
    got = [(l.t, l.key) for l in g.export_state().leaves]
    assert got == sorted(got)


def test_query_without_parents_equals_with_parents_minus_the_hierarchy(hall):
    w, leaves = hall
    g = ChartGraph(SceneParams()); g.fold_many(leaves)
    a, b = g.query(BIG, raster=False), g.query(BIG, raster=False, parents=False)
    assert [(c.key, c.cells, c.anchor) for c in a.charts] == [(c.key, c.cells, c.anchor) for c in b.charts]
    assert all(c.parent == {} for c in b.charts) and any(c.parent for c in a.charts) or len(a.charts) < 3
    assert [(l.a, l.b, l.length) for l in a.links] == [(l.a, l.b, l.length) for l in b.links]
    be = MemoryBackend(); st = SceneGraphStore(be, be)
    st.save("m", g, g.fold_many([]), None, {})
    v = st.view("m")
    c1, c2 = v.query(BIG, raster=False), v.query(BIG, raster=False, parents=False)
    assert [(c.key, c.cells) for c in c1.charts] == [(c.key, c.cells) for c in c2.charts]
    assert all(c.parent == {} for c in c2.charts)


def test_nbytes_counts_the_planes_and_grows_with_the_map(hall):
    w, leaves = hall
    small = ChartGraph(SceneParams()); small.fold_many(leaves[:8])
    big = ChartGraph(SceneParams()); big.fold_many(leaves)
    st = big.export_state()
    planes = st.V.nbytes + st.T.nbytes + st.B.nbytes
    assert big.nbytes >= planes and big.nbytes > small.nbytes > 0
    be = MemoryBackend(); store = SceneGraphStore(be, be)
    store.save("m", big, big.fold_many([]), None, {})
    view = store.view("m"); before = view.nbytes
    view.query(BIG, raster=True)
    assert view.nbytes > before > 0
