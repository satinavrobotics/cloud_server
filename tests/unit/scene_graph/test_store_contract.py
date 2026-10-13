"""The save / pending contract LACI's worker relies on: begun_ms, cursor-only saves (current and pending), no-op repeats."""
import pytest

from packages.scene_graph.api import SceneParams
from packages.scene_graph.engine import ChartGraph
from packages.scene_graph.store import MemoryBackend, SceneGraphStore

from . import chart_worlds as cw

pytestmark = pytest.mark.unit
P1, P2 = SceneParams(), SceneParams(sight_range=6.0)


class Clock:
    def __init__(self): self.t = 1_700_000_000.0
    def __call__(self): return self.t


@pytest.fixture(scope="module")
def leaves():
    w, way = cw.two_rooms()
    return cw.drive(w, way)


def _setup(leaves):
    b = MemoryBackend(); clock = Clock()
    s = SceneGraphStore(b, b, clock=clock)
    g = ChartGraph(P1); v = s.save("m", g, g.fold_many(leaves), None, {"i": 1})
    return b, clock, s, g, v


def test_begun_ms_is_stamped_kept_across_saves_and_restarted_by_fresh_or_discard(leaves):
    b, clock, s, g, v = _setup(leaves)
    h2 = SceneGraphStore.params_hash(P2)
    clock.t += 10
    gen = s.begin_generation("m", h2, rebuild_batch=48)
    t0 = int(clock.t * 1000)
    assert s.meta("m")["pending"]["begun_ms"] == t0
    clock.t += 5
    g2 = ChartGraph(P2); s.save("m", g2, g2.fold_many(leaves[:8]), v, {"i": 8}, generation=gen)
    clock.t += 5
    s.save("m", g2, g2.fold_many(leaves[8:]), v, {"i": 99}, generation=gen)
    assert s.meta("m")["pending"]["begun_ms"] == t0                      # kept across saves of the pending generation
    s2 = SceneGraphStore(b, b, clock=clock)                              # a new process
    assert s2.meta("m")["pending"]["begun_ms"] == t0
    clock.t += 5
    assert s2.begin_generation("m", h2) == gen                           # resume: unchanged
    assert s2.meta("m")["pending"]["begun_ms"] == t0
    clock.t += 5
    g3 = s2.begin_generation("m", h2, fresh=True)                        # fresh: discards, restarts the clock
    assert g3 > gen and s2.meta("m")["pending"]["begun_ms"] == int(clock.t * 1000) > t0
    clock.t += 5
    g4 = s2.begin_generation("m", SceneGraphStore.params_hash(SceneParams(sight_range=7.0)))   # other params: discards
    assert g4 > g3 and s2.meta("m")["pending"]["begun_ms"] == int(clock.t * 1000)


def test_cursor_only_save_keeps_the_version_moves_the_cursor_and_a_repeat_writes_nothing(leaves):
    b, clock, s, g, v = _setup(leaves)
    puts0 = b.puts
    cas = {"n": 0}
    orig = b.cas_meta
    def counting(*a, **k): cas["n"] += 1; return orig(*a, **k)
    b.cas_meta = counting
    d = g.fold_many([])
    assert s.save("m", g, d, v, {"i": 2}) == v
    assert s.meta("m")["cursor"] == {"i": 2} and s.meta("m")["version"] == v
    assert cas["n"] == 1 and b.puts == puts0                             # one CAS, no object written
    assert s.save("m", g, d, v, {"i": 2}) == v
    assert cas["n"] == 1 and b.puts == puts0                             # identical call: no CAS, no write


def test_cursor_only_save_on_a_pending_generation_moves_the_pending_cursor(leaves):
    b, clock, s, g, v = _setup(leaves)
    gen = s.begin_generation("m", SceneGraphStore.params_hash(P2))
    g2 = ChartGraph(P2)
    pv = s.save("m", g2, g2.fold_many(leaves), v, {"i": 5}, generation=gen)
    assert s.meta("m")["pending"]["cursor"] == {"i": 5}
    cas = {"n": 0}; orig = b.cas_meta
    def counting(*a, **k): cas["n"] += 1; return orig(*a, **k)
    b.cas_meta = counting
    puts0 = b.puts
    assert s.save("m", g2, g2.fold_many([]), v, {"i": 9}, generation=gen) == pv        # nothing new, cursor moved
    m = s.meta("m")
    assert m["pending"]["cursor"] == {"i": 9} and m["pending"]["version"] == pv and m["cursor"] == {"i": 1}
    assert cas["n"] == 1 and b.puts == puts0
    assert s.save("m", g2, g2.fold_many([]), v, {"i": 9}, generation=gen) == pv
    assert cas["n"] == 1 and b.puts == puts0                             # repeat: nothing
