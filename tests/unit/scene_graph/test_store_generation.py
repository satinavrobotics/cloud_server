"""Rebuild generations: resume, discard, and no leak of an older attempt's tiles (store.begin_generation / pending)."""
import numpy as np
import pytest

from packages.scene_graph.api import BBox, SceneParams
from packages.scene_graph.engine import ChartGraph
from packages.scene_graph.store import MemoryBackend, SceneGraphStore

from . import chart_worlds as cw
from .chart_verify import ok, verify

pytestmark = pytest.mark.unit
P1, P2, P3 = SceneParams(), SceneParams(sight_range=6.0), SceneParams(sight_range=7.0)
BATCH = 6


@pytest.fixture(scope="module")
def world():
    w, way = cw.two_rooms(W=24.0)                      # several tiles wide (a tile is 6.4 m)
    return w, cw.drive(w, way)


def _store():
    b = MemoryBackend()
    return SceneGraphStore(b, b, clock=lambda: 1_700_000_000.0), b


def _live(store, leaves, P=P1):
    g = ChartGraph(P); v = None
    for i in range(0, len(leaves), BATCH):
        v = store.save("m", g, g.fold_many(leaves[i:i + BATCH]), v, {"i": i + BATCH})
    return v


def _rebuild(store, leaves, gen, P, start=0, stop=None, g=None, v=None):
    g = g or ChartGraph(P)
    for i in range(start, len(leaves) if stop is None else stop, BATCH):
        d = g.fold_many(leaves[i:i + BATCH])
        store.save("m", g, d, v, {"i": i + BATCH}, generation=gen)
    return g


def _planes(store, man):
    out = {}
    for name in man["tiles"]:
        tx, ty = (int(v) for v in name.split(","))
        t = store._tile("m", man, tx, ty)
        out[name] = {k: t[k].tobytes() for k in ("v", "t", "b")}
    return out


def test_resume_after_a_crash_ends_where_an_uninterrupted_rebuild_ends(world):
    w, leaves = world
    done = SceneGraphStore.params_hash(P2)
    # uninterrupted
    su, _ = _store(); v = _live(su, leaves)
    gen = su.begin_generation("m", done, rebuild_batch=BATCH)
    _rebuild(su, leaves, gen, P2, v=v)
    su.commit_generation("m", gen)
    # interrupted after about half, the process is gone, a new worker resumes from meta()["pending"]
    si, _ = _store(); v = _live(si, leaves)
    gen_i = si.begin_generation("m", done, rebuild_batch=BATCH)
    half = (len(leaves) // 2) // BATCH * BATCH
    _rebuild(si, leaves, gen_i, P2, stop=half, v=v)
    pend = si.meta("m")["pending"]
    assert pend["params_hash"] == done and pend["cursor"] == {"i": half} and pend["rebuild_batch"] == BATCH
    assert si.begin_generation("m", done) == gen_i                         # same params: resume, not discard
    g = si.load("m", pending=True)
    _rebuild(si, leaves, gen_i, P2, start=pend["cursor"]["i"], g=g, v=v)
    si.commit_generation("m", gen_i)
    assert ok(verify(si.load("m"), w))
    assert _planes(su, su.manifest("m")) == _planes(si, si.manifest("m"))  # the folded planes are the truth: equal
    assert si.meta("m")["version"] == su.meta("m")["version"] and "pending" not in si.meta("m")


def test_a_params_change_discards_the_pending_and_gc_collects_it(world):
    w, leaves = world
    s, b = _store(); v = _live(s, leaves)
    g2 = s.begin_generation("m", SceneGraphStore.params_hash(P2))
    _rebuild(s, leaves, g2, P2, stop=18, v=v)
    old = {n for n in b.list("m") if n.startswith("g%d/" % g2)}
    assert old
    g3 = s.begin_generation("m", SceneGraphStore.params_hash(P3))
    assert g3 > g2
    p = s.meta("m")["pending"]
    assert p["generation"] == g3 and p["params_hash"] == SceneGraphStore.params_hash(P3) and p["cursor"] is None
    s.gc("m")
    assert not any(n.startswith("g%d/" % g2) for n in b.list("m"))        # the discarded attempt is gone


def test_fresh_discards_even_with_equal_params(world):
    w, leaves = world
    s, _ = _store(); v = _live(s, leaves)
    h = SceneGraphStore.params_hash(P2)
    g2 = s.begin_generation("m", h)
    _rebuild(s, leaves, g2, P2, stop=12, v=v)
    g3 = s.begin_generation("m", h, fresh=True)
    assert g3 > g2 and s.meta("m")["pending"]["cursor"] is None
    assert s.begin_generation("m", h) == g3                                 # and a second call resumes the new one


def test_replay_from_zero_into_an_old_pending_does_not_inherit_its_tiles(world):
    w, leaves = world
    s, _ = _store(); v = _live(s, leaves)
    gen = s.begin_generation("m", SceneGraphStore.params_hash(P2))
    _rebuild(s, leaves, gen, P2, v=v)                                       # a whole attempt is in the pending
    g = ChartGraph(P2)                                                      # the worker lost its engine and replays from 0
    d = g.fold_many(leaves[:BATCH])
    s.save("m", g, d, v, {"i": BATCH}, generation=gen)
    ref, _ = _store()
    ref.save("m", g, d, None, {"i": BATCH})                                 # the same engine state, saved from nothing
    assert s.manifest("m", pending=True)["tiles"] == ref.manifest("m")["tiles"]


def test_a_pending_refuses_an_engine_with_other_parameters(world):
    w, leaves = world
    s, _ = _store(); v = _live(s, leaves)
    gen = s.begin_generation("m", SceneGraphStore.params_hash(P2))
    g = ChartGraph(P3)
    with pytest.raises(ValueError):
        s.save("m", g, g.fold_many(leaves[:BATCH]), v, {}, generation=gen)


def test_meta_has_no_pending_key_without_a_rebuild(world):
    w, leaves = world
    s, _ = _store(); _live(s, leaves)
    assert "pending" not in s.meta("m")
    assert SceneGraphStore.params_hash(P1) == s.meta("m")["params_hash"]


def _reference_keymap(store, old, new):
    """the key map as it was first written: read both tiles of every common tile, unique over stacked (old id, new id) pairs"""
    inv_old = {c["id"]: c["key"] for c in old["charts"]}; inv_new = {c["id"]: c["key"] for c in new["charts"]}
    counts = {}
    for name in set(old["tiles"]) & set(new["tiles"]):
        tx, ty = (int(v) for v in name.split(","))
        a = store._tile("m", old, tx, ty); b = store._tile("m", new, tx, ty)
        if a is None or b is None or "own" not in a or "own" not in b: continue
        m = (a["own"] > 0) & (b["own"] > 0)
        if not m.any(): continue
        pairs, n = np.unique(np.stack([a["own"][m], b["own"][m]], 1), axis=0, return_counts=True)
        for (i, j), c in zip(pairs, n):
            k = (inv_old.get(int(i)), inv_new.get(int(j)))
            if k[0] and k[1]: counts[k] = counts.get(k, 0) + int(c)
    out = {c["key"]: [] for c in old["charts"]}
    for (ko, kn), c in counts.items(): out[ko].append((-c, kn))
    return {k: [s for _, s in sorted(v)] for k, v in out.items()}


@pytest.mark.parametrize("P_new", [P2, P1])                  # a params change; and an identical state (every tile equal)
def test_keymap_with_the_equal_tile_shortcut_equals_the_full_computation(world, P_new):
    from packages.scene_graph.store import _overlap_keymap
    w, leaves = world
    s, _ = _store(); v = _live(s, leaves)
    gen = s.begin_generation("m", fresh=True)
    g = ChartGraph(P_new); d = g.fold_many(leaves)            # one batch: another history than the live generation's
    s.save("m", g, d, v, {"i": 0}, generation=gen)
    old, new = s.manifest("m"), s.manifest("m", pending=True)
    same = sum(1 for n in set(old["tiles"]) & set(new["tiles"]) if old["tiles"][n] == new["tiles"][n])
    assert _overlap_keymap(s, "m", old, new) == _reference_keymap(s, old, new)
    if P_new is P1:
        assert same >= 1                                       # the shortcut path is exercised
