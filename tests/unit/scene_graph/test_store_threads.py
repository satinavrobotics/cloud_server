"""One SceneGraphStore used from many threads (the planner's executor): the expansion cache must not raise or mix results."""
import random
import sys
import threading

import pytest

from packages.scene_graph.api import BBox, SceneParams
from packages.scene_graph.engine import ChartGraph
from packages.scene_graph.store import MemoryBackend, SceneGraphStore

from . import chart_worlds as cw

pytestmark = pytest.mark.unit
BIG = BBox(-5, -5, 40, 20)


def _sig(q):
    return (sorted((c.key, c.cells) for c in q.charts), sorted((l.a, l.b, round(l.length, 6)) for l in q.links))


def test_concurrent_reads_with_a_one_entry_cache_are_exact():
    w, way = cw.two_rooms(W=24.0); leaves = cw.drive(w, way)
    be = MemoryBackend(); st = SceneGraphStore(be, be, keep_last=50)
    g = ChartGraph(SceneParams()); v = None
    for i in range(0, len(leaves), 6):
        v = st.save("m", g, g.fold_many(leaves[i:i + 6]), v, {"i": i})
    versions = list(range(1, v + 1))
    serial = {ver: _sig(st.view("m", ver).query(BIG, raster=False)) for ver in versions}
    loaded = {ver: sorted(c.key for c in st.load("m", ver).query(BIG, raster=False).charts) for ver in (versions[0], versions[-1])}
    st.cache_size = 1                                    # every read evicts: the worst case for the cache
    errors, seen = [], []

    def work(seed):
        r = random.Random(seed)
        try:
            for _ in range(40):
                ver = r.choice(versions)
                assert _sig(st.view("m", ver).query(BIG, raster=False)) == serial[ver]
                if r.random() < 0.1:
                    ver = r.choice(list(loaded))
                    assert sorted(c.key for c in st.load("m", ver).query(BIG, raster=False).charts) == loaded[ver]
                seen.append(ver)
        except Exception as e:                           # noqa: BLE001
            errors.append(repr(e))

    old = sys.getswitchinterval(); sys.setswitchinterval(1e-6)
    try:
        ts = [threading.Thread(target=work, args=(i,)) for i in range(16)]
        [t.start() for t in ts]; [t.join() for t in ts]
    finally:
        sys.setswitchinterval(old)
    assert not errors, errors[:3]
    assert len(seen) >= 16 * 40 and len(st._expanded) <= 1 and not st._inflight


def test_single_flight_expands_a_manifest_once_for_concurrent_readers():
    w, way = cw.two_rooms(); leaves = cw.drive(w, way)
    be = MemoryBackend(); st = SceneGraphStore(be, be)
    g = ChartGraph(SceneParams()); st.save("m", g, g.fold_many(leaves), None, {})
    st2 = SceneGraphStore(be, be)                        # a cold cache
    calls = []
    real = st2._expand
    st2._expand = lambda m, n: (calls.append(n), real(m, n))[1]
    ts = [threading.Thread(target=lambda: st2.manifest("m")) for _ in range(12)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert len(calls) == 1
