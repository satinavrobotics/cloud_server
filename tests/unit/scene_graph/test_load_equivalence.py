"""store.load() reproduces the SAVED state: same charts, keys, links, owner plane (restore, not re-solve), a replay of folded
leaves is a no-op (empty Dirty, same version, no new objects), and continuing after a load equals continuing without one."""
import dataclasses

import numpy as np
import pytest

from packages.scene_graph.api import BBox, LeafError, SceneParams
from packages.scene_graph.engine import ChartGraph, _line
from packages.scene_graph.state import StateMismatch
from packages.scene_graph.store import MemoryBackend, SceneGraphStore

from . import chart_worlds as cw
from .chart_verify import ok, verify

pytestmark = pytest.mark.unit
BIG = BBox(-50, -50, 100, 100)
WORLDS = ["two_rooms", "corridor", "pillar_room", "ring", "island"]


def _batches(name, size=7):
    w, way = getattr(cw, name)()
    ls = cw.drive(w, way)
    return w, [ls[i:i + size] for i in range(0, len(ls), size)]


def _sig(g):
    q = g.query(BIG, raster=True); r = q.raster
    ys, xs = np.nonzero(r.index)
    ox, oy = round(r.origin[0] / 0.05) // 2, round(r.origin[1] / 0.05) // 2
    own = {(ox + int(x), oy + int(y)): r.keys[int(r.index[y, x]) - 1] for y, x in zip(ys, xs)}
    dep = {(ox + int(x), oy + int(y)) for y, x in zip(*np.nonzero(r.dependent))}
    charts = sorted((c.key, c.anchor, c.cells, round(c.driven_frac, 6), round(c.ever_blocked_frac, 6), c.leaf) for c in q.charts)
    links = sorted((l.a, l.b, round(l.length, 6), round(l.width, 6), l.portal, tuple(sorted(l.channels.items()))) for l in q.links)
    return charts, links, own, dep


def _saved(name, size=7):
    w, bs = _batches(name, size)
    be = MemoryBackend(); st = SceneGraphStore(be, be)
    g = ChartGraph(SceneParams()); v = None
    for b in bs:
        v = st.save("m", g, g.fold_many(b), v, {"n": 1})
    return w, bs, be, st, g, v


@pytest.mark.parametrize("name", WORLDS)
def test_load_is_bit_identical_to_the_saved_state(name):
    w, bs, be, st, g, v = _saved(name)
    l = st.load("m")
    assert _sig(l) == _sig(g)
    assert ok(verify(l, w))
    assert l.check_invariants() == [] and g.check_invariants() == []


@pytest.mark.parametrize("name", ["two_rooms", "pillar_room"])
def test_replay_of_folded_leaves_is_a_no_op(name):
    w, bs, be, st, g, v = _saved(name)
    l = st.load("m"); n = len(be.list("m"))
    for batch in (bs[-1], [x for b in bs for x in b]):
        d = l.fold_many(batch)
        assert d.bbox is None and d.changed == [] and d.retired == {} and d.links == []
        assert st.save("m", l, d, v, {"n": 2}) == v
    assert len(be.list("m")) == n
    assert st.meta("m")["cursor"] == {"n": 2}                      # the cursor moved in place, the version did not


@pytest.mark.parametrize("name", ["corridor", "ring"])
def test_continuing_after_a_load_equals_continuing_without_one(name):
    w, bs = _batches(name, 5)
    assert len(bs) >= 3
    for split in (1, len(bs) // 2):
        be = MemoryBackend(); st = SceneGraphStore(be, be)
        g = ChartGraph(SceneParams()); v = None
        for b in bs[:split]:
            v = st.save("m", g, g.fold_many(b), v, {})
        l = st.load("m")
        for b in bs[split:]:
            g.fold_many(b); l.fold_many(b)
        assert _sig(l) == _sig(g)
        assert [k for k in l._keys] == [k for k in g._keys]


def test_a_tampered_owner_plane_is_refused_naming_the_tile():
    w, bs, be, st, g, v = _saved("two_rooms")
    man = st.manifest("m", v)
    import packages.scene_graph.store as S
    t = st._tile("m", man, *[int(x) for x in sorted(man["tiles"])[0].split(",")])
    assert "own" in t
    orig = S.SceneGraphStore._tile

    def lying(self, map_id, man_, tx, ty):
        a = orig(self, map_id, man_, tx, ty)
        if a is not None and "own" in a and not getattr(lying, "done", False):
            a = dict(a); o = a["own"].copy(); i = np.argwhere(o > 0)[0]
            o[tuple(i)] = 0; a["own"] = o; lying.done = True       # un-own one chart cell
        return a
    S.SceneGraphStore._tile = lying
    try:
        with pytest.raises(StateMismatch, match="tile"):
            st.load("m")
    finally:
        S.SceneGraphStore._tile = orig


def test_the_digital_line_of_a_translated_pair_is_the_translated_line():
    # np.rint rounded half to even: the line depended on the canvas origin (a loaded engine has another one)
    for dy, dx in ((1, 2), (3, 7), (2, 5), (4, 9), (-3, 5), (7, -2)):
        a = _line(0, 0, dy, dx)
        for sh in (1, 2, 3, 7):
            b = _line(sh, 2 * sh + 1, dy + sh, dx + 2 * sh + 1)
            assert np.array_equal(b[0], a[0] + sh) and np.array_equal(b[1], a[1] + 2 * sh + 1)


def test_a_pose_must_belong_to_its_window():
    w, bs = _batches("two_rooms")
    leaf = bs[0][0]
    g = ChartGraph(SceneParams())
    far = dataclasses.replace(leaf, key="far", x=leaf.x + 3.0e6)
    with pytest.raises(LeafError, match="outside its window"):
        g.fold_many([far])
    assert g.export_state().V is None                                # nothing was folded
    # at the footprint edge plus the pose slack: accepted; a bit beyond: refused
    from packages.scene_graph.engine import POSE_SLACK_M
    sr = POSE_SLACK_M
    wl = leaf.window_u8.shape[1] * leaf.resolution
    x_edge = leaf.origin[0] + wl + sr - 1e-6
    ok_leaf = dataclasses.replace(leaf, key="edge", x=x_edge)
    g.fold_many([ok_leaf])
    over = dataclasses.replace(leaf, key="over", x=leaf.origin[0] + wl + sr + 0.5)
    with pytest.raises(LeafError):
        g.fold_many([over])
    assert "over" not in g._leaves
