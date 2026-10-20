"""Memory work on the engine is a pure optimisation: int32 time planes (offsets from a per-engine epoch, exact int64 fallback),
the int32 squared-distance cache, the lighter import and dirty box. Every test compares against the plain path."""
import dataclasses
import hashlib

import numpy as np
import pytest
from scipy import ndimage

from packages.scene_graph import engine as E
from packages.scene_graph import store as S
from packages.scene_graph.api import BBox, SceneParams
from packages.scene_graph.engine import ChartGraph
from packages.scene_graph.state import TimePlane

from . import chart_worlds as cw

pytestmark = pytest.mark.unit
BIG = BBox(-50, -50, 100, 100)
P = dict(near_range_m=1.3, near_min_clearance_m=0.25)


@pytest.fixture(scope="module")
def leaves():
    w, way = cw.two_rooms()
    return cw.drive(w, way)


def _shift(leaves, base, step=1000):
    return [dataclasses.replace(l, stamp_ms=base + step * i) for i, l in enumerate(leaves)]


def _digest(g):
    st = g.export_state()
    h = hashlib.sha1()
    for a in (st.V, st.T, st.B, st.Fn, st.Bn, st.Val, st.plane):
        if a is not None:
            assert not isinstance(a, TimePlane)
            h.update(str(a.dtype).encode() if a is st.V else b""); h.update(np.ascontiguousarray(a).tobytes())
    for k in sorted(st.chan):
        for a in st.chan[k]: h.update(np.ascontiguousarray(a).tobytes())
    h.update(repr(([(r.key, r.t) for r in st.leaves], st.keys, st.watermark, st.version)).encode())
    q = g.query(BIG, raster=True)
    h.update(repr(sorted((c.key, c.anchor, c.cells, c.clearance) for c in q.charts)).encode())
    h.update(repr(sorted((l.a, l.b, l.length, l.width) for l in q.links)).encode())
    return h.hexdigest()


def _plain(monkeypatch):
    monkeypatch.setattr(ChartGraph, "_time_epoch", lambda self, stamps: None)


def _fold(leaves, batch=3, **kw):
    g = ChartGraph(SceneParams(**{**P, **kw}))
    ds = []
    for i in range(0, len(leaves), batch):
        d = g.fold_many(leaves[i:i + batch]); ds.append((d.bbox, d.changed, sorted(d.retired.items()), d.links))
    return g, ds


@pytest.mark.parametrize("base", [1_760_000_000_000, 5000, 2 ** 40])
def test_int32_planes_equal_int64_planes(leaves, monkeypatch, base):
    ls = _shift(leaves, base)
    a, da = _fold(ls)
    assert a._tep is not None and a._Tr.dtype == np.int32 and a._Br.dtype == np.int32 and a._Fnr.dtype == np.int32
    _plain(monkeypatch)
    b, db = _fold(ls)
    assert b._tep is None and b._Tr.dtype == np.int64
    assert da == db and _digest(a) == _digest(b)
    assert a._T.dtype == np.int64 and np.array_equal(a._T, b._T) and np.array_equal(a._Bn, b._Bn)
    assert a.nbytes < b.nbytes


def test_lazy_export_slices_like_the_wide_planes(leaves):
    g, _ = _fold(_shift(leaves, 1_760_000_000_000))
    wide, lazy = g.export_state(), g.export_state_lazy()
    assert isinstance(lazy.T, TimePlane) and lazy.T.shape == wide.T.shape and np.array_equal(np.asarray(lazy.Fn), wide.Fn)
    for tx in range(-1, 3):
        for ty in range(-1, 3):
            ta, tb = S.tile_arrays(wide, tx, ty, None), S.tile_arrays(lazy, tx, ty, None)
            assert (ta is None) == (tb is None)
            if ta is not None:
                assert set(ta) == set(tb)
                for k in ta: assert ta[k].dtype == tb[k].dtype and np.array_equal(ta[k], tb[k]), k


@pytest.mark.parametrize("stamp", [2 ** 31, -5, 2 ** 40, 1_760_000_000_000 - 2 ** 31])
def test_a_stamp_out_of_range_switches_to_int64_exactly(leaves, monkeypatch, stamp):
    ls = _shift(leaves, 1_760_000_000_000)
    odd = dataclasses.replace(ls[4], key="odd", stamp_ms=stamp if stamp != 2 ** 31 else 1_760_000_000_000 + 2 ** 31)
    seq = ls[:6] + [odd] + ls[6:]
    a, da = _fold(seq)
    _plain(monkeypatch)
    b, db = _fold(seq)
    assert a._tep is None and a._Tr.dtype == np.int64
    assert da == db and _digest(a) == _digest(b)


def test_the_switch_leaves_the_engine_unchanged_when_allocation_fails(leaves, monkeypatch):
    ls = _shift(leaves, 1_760_000_000_000)
    g, _ = _fold(ls[:8])
    before = _digest(g); ep = g._tep
    odd = dataclasses.replace(ls[9], key="odd", stamp_ms=1_760_000_000_000 + 2 ** 33)

    def boom(self, plan): raise MemoryError("no")
    monkeypatch.setattr(ChartGraph, "_grow_canvas", boom)
    with pytest.raises(MemoryError): g.fold_many([odd])
    assert g.faulted is None and g._tep == ep and g._Tr.dtype == np.int32 and _digest(g) == before


def test_import_state_round_trip_and_epoch(leaves, monkeypatch):
    ls = _shift(leaves, 1_760_000_000_000)
    g, _ = _fold(ls)
    st = g.export_state()
    h1 = ChartGraph(g.params); h1.import_state(st)
    assert h1._tep is not None and h1._Tr.dtype == np.int32 and h1._Fnr.dtype == np.int32
    pad = E.MARGIN
    assert np.array_equal(h1._T[pad:-pad, pad:-pad], g._T) and np.array_equal(h1._Bn[pad:-pad, pad:-pad], g._Bn)
    # the same import with int64 planes gives the same state, and both go on folding alike
    h0 = ChartGraph(g.params)
    with monkeypatch.context() as m:
        m.setattr(E, "_epoch_for", lambda lo, hi: None)
        h0.import_state(st)
    assert h0._tep is None and h0._Tr.dtype == np.int64 and _digest(h0) == _digest(h1)
    more = _shift(leaves, 1_760_000_100_000)[:4]
    h0.fold_many(more); h1.fold_many(more)
    assert _digest(h0) == _digest(h1)
    _plain(monkeypatch)
    # a state whose stamps do not fit one epoch stays int64
    st2 = g.export_state()
    st2.T = st2.T.copy(); st2.T[st2.T >= 0] += 0; k = np.argwhere(st2.T >= 0)[0]; st2.T[tuple(k)] = 2 ** 45
    h2 = ChartGraph(g.params); h2.import_state(st2, verify=False)
    assert h2._tep is None and h2._Tr.dtype == np.int64 and np.array_equal(h2.export_state().T[pad:-pad, pad:-pad], st2.T)


def test_time_property_setter_narrows(leaves):
    g, _ = _fold(_shift(leaves, 1_760_000_000_000))
    old = g._Bn.copy(); g._Bn = g._B
    assert g._Bnr.dtype == np.int32 and np.array_equal(g._Bn, g._B) and not np.array_equal(old, g._Bn)


def test_the_distance_cache_is_int32_and_exact(leaves, monkeypatch):
    ls = _shift(leaves, 1_760_000_000_000)
    a, da = _fold(ls)
    assert a._dcache[1].dtype == np.int32
    monkeypatch.setattr(E, "_edt", lambda f: ndimage.distance_transform_edt(f))          # the float64 cache path
    b, db = _fold(ls)
    assert b._dcache[1].dtype == np.float64
    assert da == db and _digest(a) == _digest(b)


@pytest.mark.parametrize("seed", range(6))
def test_clearance_cells_match_the_plain_transform(seed):
    rng = np.random.default_rng(seed)
    g = ChartGraph(SceneParams())
    H, W = int(rng.integers(40, 150)), int(rng.integers(40, 150))
    free = rng.random((H, W)) < rng.uniform(0.3, 0.97)
    for step in range(8):
        if step in (3, 6):                                       # the canvas grows: old content moves by `shift` lattice cells
            sy, sx = int(rng.integers(0, 6)), int(rng.integers(0, 6))
            nf = np.zeros((H + 2 * sy + 8, W + 2 * sx + 6), bool); nf[2 * sy:2 * sy + H, 2 * sx:2 * sx + W] = free
            free = nf; H, W = free.shape; shift = (sy, sx)
        else:
            shift = (0, 0)
            y0, x0 = int(rng.integers(0, H - 5)), int(rng.integers(0, W - 5))
            free = free.copy(); free[y0:y0 + int(rng.integers(1, 12)), x0:x0 + int(rng.integers(1, 12))] = rng.random() < 0.5
        want = ndimage.distance_transform_edt(free)
        got = g._clearance_cells(free, shift, None if step % 2 else 0.05)
        ref = want if step % 2 else want * 0.05
        assert got.dtype == np.float64 and np.array_equal(got, ref), (seed, step)


def test_edt_squares_are_scipys_distances_bit_for_bit():
    rng = np.random.default_rng(1)
    cases = [np.ones((7, 9), bool), np.zeros((7, 9), bool), np.ones((1, 1), bool), np.zeros((1, 5), bool), np.ones((600, 3), bool)]
    for _ in range(40):
        h, w = int(rng.integers(1, 400)), int(rng.integers(1, 400))
        cases.append(rng.random((h, w)) < rng.uniform(0.0, 1.0))
    big = rng.random((300, 300)) < 0.9995
    cases += [big, big[7:200, 11:250], big[::2, ::3]]              # sparse blockers: long distances; sliced (non-contiguous) inputs
    for f in cases:
        want = ndimage.distance_transform_edt(f)
        got = E._edt(f)
        assert got.dtype == np.int32
        assert np.array_equal(np.sqrt(got, dtype=np.float64), want), f.shape
        assert np.array_equal(E._dt_float(got), want)


def test_edt_falls_back_to_scipy_when_squares_overflow_int32():
    f = np.ones((1, 70000), bool); f[0, 0] = False                # squared distance up to 4.9e9
    got = E._edt(f)
    assert got.dtype == np.float64 and np.array_equal(got, ndimage.distance_transform_edt(f))
