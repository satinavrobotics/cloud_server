"""fold_many / refold exception safety: LeafError before any mutation (state bit-identical, retry/bisect works),
EngineFaulted after an exception that escaped the mutation."""
import dataclasses
import hashlib

import numpy as np
import pytest

from packages.scene_graph.api import BBox, EngineFaulted, LeafError, SceneParams
from packages.scene_graph.engine import ChartGraph

from . import chart_worlds as cw

pytestmark = pytest.mark.unit
BIG = BBox(-50, -50, 100, 100)


@pytest.fixture(scope="module")
def leaves():
    w, way = cw.two_rooms()
    return cw.drive(w, way)


def state_hash(g):
    """everything a failed batch must not touch: planes, channels, leaf records, watermark, version, charts, links, raster"""
    h = hashlib.sha1()
    st = g.export_state()
    for a in (st.V, st.T, st.B):
        if a is not None: h.update(np.ascontiguousarray(a).tobytes())
    for k in sorted(st.chan):
        h.update(k.encode()); h.update(np.ascontiguousarray(st.chan[k][0]).tobytes()); h.update(np.ascontiguousarray(st.chan[k][1]).tobytes())
    h.update(repr([(r.key, r.session, r.t, r.x, r.y, r.r_d) for r in st.leaves]).encode())
    h.update(repr((st.watermark, st.version, st.cx0, st.cy0, st.keys, st.anchors)).encode())
    q = g.query(BIG, raster=True)
    h.update(repr(sorted((c.key, c.anchor, c.cells) for c in q.charts)).encode())
    h.update(repr(sorted((l.a, l.b, l.length, l.width) for l in q.links)).encode())
    if q.raster is not None: h.update(q.raster.index.tobytes())
    return h.hexdigest()


def plane_hash(g):
    st = g.export_state()
    return hashlib.sha1(b"".join(np.ascontiguousarray(a).tobytes() for a in (st.V, st.T, st.B))).hexdigest()


def _poison(lf, kind):
    if kind == "nan_pose": return dataclasses.replace(lf, x=float("nan"))
    if kind == "inf_origin": return dataclasses.replace(lf, origin=(float("inf"), 0.0))
    if kind == "nan_yaw": return dataclasses.replace(lf, yaw=float("nan"))
    if kind == "float_window": return dataclasses.replace(lf, window_u8=lf.window_u8.astype(np.float32))
    if kind == "3d_window": return dataclasses.replace(lf, window_u8=lf.window_u8[:, :, None])
    if kind == "empty_window": return dataclasses.replace(lf, window_u8=np.zeros((0, 5), np.uint8))
    if kind == "huge_window": return dataclasses.replace(lf, window_u8=np.zeros((500, 500), np.uint8))
    if kind == "zero_resolution": return dataclasses.replace(lf, resolution=0.0)
    if kind == "negative_rd": return dataclasses.replace(lf, r_d=-0.1)
    if kind == "float_stamp": return dataclasses.replace(lf, stamp_ms=1.5)
    if kind == "bad_channel": return dataclasses.replace(lf, channels={"cost": np.zeros((3, 3), np.uint8)})
    if kind == "empty_key": return dataclasses.replace(lf, key="")
    raise AssertionError(kind)


KINDS = ["nan_pose", "inf_origin", "nan_yaw", "float_window", "3d_window", "empty_window", "huge_window", "zero_resolution",
         "negative_rd", "float_stamp", "bad_channel", "empty_key"]


@pytest.mark.parametrize("kind", KINDS)
def test_a_poison_leaf_raises_leaferror_before_any_mutation_and_the_halves_can_be_retried(leaves, kind):
    base, batch = leaves[:6], leaves[6:12]
    g = ChartGraph(SceneParams()); g.fold_many(base)
    before = state_hash(g)
    poisoned = [batch[0], batch[1], _poison(batch[2], kind), batch[3], batch[4], batch[5]]
    with pytest.raises(LeafError) as e:
        g.fold_many(poisoned)
    assert e.value.key == (batch[2].key if kind != "empty_key" else "")
    assert state_hash(g) == before and g.faulted is None                     # bit-identical, still usable
    # the worker bisects: the good halves go through on the same engine ...
    good = [l for l in poisoned if l.key == l.key and l is not poisoned[2]]
    g.fold_many(good[:2]); g.fold_many(good[2:])
    # ... and the planes equal a reference that never saw the poison
    ref = ChartGraph(SceneParams()); ref.fold_many(base); ref.fold_many(good)
    assert plane_hash(g) == plane_hash(ref)


def test_duplicate_key_with_different_content_in_one_batch_is_refused_and_an_identical_duplicate_is_not(leaves):
    g = ChartGraph(SceneParams()); g.fold_many(leaves[:4])
    before = state_hash(g)
    a = leaves[5]; other = dataclasses.replace(a, stamp_ms=a.stamp_ms + 1)
    with pytest.raises(LeafError) as e:
        g.fold_many([a, other])
    assert e.value.key == a.key and state_hash(g) == before
    g.fold_many([a, a])                                                       # a true duplicate is folded once
    ref = ChartGraph(SceneParams()); ref.fold_many(leaves[:4]); ref.fold_many([a])
    assert plane_hash(g) == plane_hash(ref)


def test_a_leaf_that_needs_an_absurd_canvas_is_refused_unchanged(leaves):
    g = ChartGraph(SceneParams()); g.fold_many(leaves[:6])
    before = state_hash(g)
    far = dataclasses.replace(leaves[7], x=leaves[7].x + 1.0e5, origin=(leaves[7].origin[0] + 1.0e5, leaves[7].origin[1]), key="far")
    with pytest.raises(LeafError):
        g.fold_many([far])
    assert state_hash(g) == before and g.faulted is None


def test_a_memory_error_while_allocating_the_canvas_changes_nothing(leaves, monkeypatch):
    g = ChartGraph(SceneParams()); g.fold_many(leaves[:6])
    before = state_hash(g)
    far = dataclasses.replace(leaves[7], x=leaves[7].x + 30.0, origin=(leaves[7].origin[0] + 30.0, leaves[7].origin[1]), key="far")
    monkeypatch.setattr(g, "_grow_canvas", lambda plan: (_ for _ in ()).throw(MemoryError("no")))
    with pytest.raises(MemoryError):
        g.fold_many([far])
    assert state_hash(g) == before and g.faulted is None


def test_an_exception_after_mutation_began_faults_the_engine(leaves, monkeypatch):
    g = ChartGraph(SceneParams()); g.fold_many(leaves[:6])
    monkeypatch.setattr(g, "_solve", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom in the solver")))
    with pytest.raises(RuntimeError):
        g.fold_many(leaves[6:10])
    assert isinstance(g.faulted, RuntimeError)
    for call in (lambda: g.fold_many(leaves[10:12]), lambda: g.refold(leaves[:2], BIG), lambda: g.query(BIG),
                 lambda: g.export_state(), lambda: g.locate(1.0, 1.0), lambda: g.route("a", "b"),
                 lambda: g.corridor("a", "b"), lambda: g.cover([])):
        with pytest.raises(EngineFaulted) as e:
            call()
        assert isinstance(e.value.cause, RuntimeError) and "boom" in str(e.value)


def test_refold_with_a_poison_leaf_is_unchanged_too(leaves):
    g = ChartGraph(SceneParams()); g.fold_many(leaves[:12])
    before = state_hash(g)
    with pytest.raises(LeafError):
        g.refold([leaves[0], _poison(leaves[1], "nan_pose")], BBox(-5, -5, 20, 20))
    assert state_hash(g) == before
    with pytest.raises(LeafError):
        g.refold([leaves[0]], BBox(float("nan"), 0, 1, 1))
    assert state_hash(g) == before


def test_query_without_parents_never_builds_the_hierarchy(leaves, monkeypatch):
    from packages.scene_graph import engine as eng
    g = ChartGraph(SceneParams()); g.fold_many(leaves)
    calls = []
    monkeypatch.setattr(eng, "hierarchy", lambda *a, **k: calls.append(1) or (_ for _ in ()).throw(AssertionError("built")))
    g._cache.pop("levels", None)
    q = g.query(BIG, level=0, raster=True, parents=False)
    assert q.charts and not calls
