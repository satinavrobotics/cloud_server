"""The windowed build of the navigable lattice F (`ChartGraph._build_F` with the changed box) equals the whole-canvas build, bit for
bit, over many random updates: grow, shrink (refold), canvas growth on every side, replays, a state import in the middle."""
import numpy as np
import pytest
from scipy import ndimage

from packages.scene_graph import engine as E
from packages.scene_graph.api import BBox, Leaf, SceneParams
from packages.scene_graph.engine import ChartGraph

pytestmark = pytest.mark.unit

RES = E.RES


class _FullGraph(ChartGraph):
    """the same engine with the changed box withheld: every build is the whole-canvas computation"""
    def _solve(self, shift, birth, box=None):
        return super()._solve(shift, birth, None)


# ---------------------------------------------------------------------------------------------------------- the pieces
def _rand_free(rng, H, W, dens):
    f = rng.random((H, W)) < dens
    return ndimage.binary_dilation(f, iterations=int(rng.integers(0, 3))) if rng.random() < 0.5 else f


@pytest.mark.parametrize("seed", range(8))
def test_clearance_windowed_equals_plain_transform(seed):
    """box + lattice outputs of `_clearance_cells` against scipy over random edits (sparse blockers: long distances, so the reach
    bound is exercised) and canvas growth"""
    rng = np.random.default_rng(100 + seed)
    g = ChartGraph(SceneParams())
    H, W = 2 * int(rng.integers(20, 90)), 2 * int(rng.integers(20, 90))
    free = rng.random((H, W)) < rng.choice([0.5, 0.97, 0.999, 0.9999])
    g._clearance_cells(free, (0, 0), E.RES, None, lattice=True)
    for step in range(14):
        if step in (4, 9):
            sy, sx = int(rng.integers(0, 5)), int(rng.integers(0, 5))
            nf = np.zeros((H + 2 * sy + 8, W + 2 * sx + 6), bool); nf[2 * sy:2 * sy + H, 2 * sx:2 * sx + W] = free
            free, shift, box = nf, (sy, sx), None
            H, W = free.shape
        else:
            shift = (0, 0)
            y0, x0 = int(rng.integers(0, H - 2)), int(rng.integers(0, W - 2))
            y1, x1 = min(H, y0 + int(rng.integers(1, 30))), min(W, x0 + int(rng.integers(1, 30)))
            free = free.copy()
            sub = rng.random((y1 - y0, x1 - x0)) < rng.choice([0.0, 0.3, 0.8, 1.0])
            if rng.random() < 0.5: sub = np.full(sub.shape, bool(rng.random() < 0.5))
            free[y0:y1, x0:x1] = sub
            box = (y0, y1, x0, x1)
        want = ndimage.distance_transform_edt(free)
        price, top = g._clearance_cells(free, shift, E.RES, box, lattice=True)
        full = want * E.RES
        assert np.array_equal(top, full[::E.LAT, ::E.LAT]), (seed, step)
        assert np.array_equal(price, E.block_min(full)), (seed, step)


@pytest.mark.parametrize("seed", range(6))
def test_erode_with_box_equals_full(seed):
    rng = np.random.default_rng(200 + seed)
    a, b = ChartGraph(SceneParams()), ChartGraph(SceneParams())
    H, W = 2 * int(rng.integers(40, 100)), 2 * int(rng.integers(40, 100))
    free = _rand_free(rng, H, W, 0.9)
    a._erode(free, (0, 0), None); b._erode(free, (0, 0), None)
    for step in range(12):
        y0, x0 = int(rng.integers(0, H - 2)), int(rng.integers(0, W - 2))
        y1, x1 = min(H, y0 + int(rng.integers(1, 40))), min(W, x0 + int(rng.integers(1, 40)))
        nf = free.copy(); nf[y0:y1, x0:x1] = rng.random((y1 - y0, x1 - x0)) < rng.choice([0.0, 0.5, 0.95, 1.0])
        want = b._erode(nf, (0, 0), None)
        got = a._erode(nf, (0, 0), (y0, y1, x0, x1))
        assert np.array_equal(got, want), (seed, step)
        free = nf


@pytest.mark.parametrize("mc", [0.25, 0.4])
@pytest.mark.parametrize("seed", range(6))
def test_hold_window_equals_the_whole_plane_filter(seed, mc):
    """`_hold_window` against `_away_from_walls` on the whole canvas, with held structures that cross the window border"""
    rng = np.random.default_rng(300 + seed)
    g = ChartGraph(SceneParams(near_range_m=1.5, near_min_clearance_m=mc))
    H, W = 2 * int(rng.integers(40, 90)), 2 * int(rng.integers(40, 90))
    V = np.where(rng.random((H, W)) < 0.15, 100, 0).astype(np.uint8)
    V[rng.random((H, W)) < 0.05] = 255
    Bn = np.where(rng.random((H, W)) < 0.12, rng.integers(0, 50, (H, W)), -1).astype(np.int32)
    Fn = np.where(rng.random((H, W)) < 0.5, rng.integers(0, 50, (H, W)), -1).astype(np.int32)
    # long held strips, blobs and a held rectangle that touches the canvas edge
    for _ in range(4):
        y, x = int(rng.integers(0, H)), int(rng.integers(0, W))
        if rng.random() < 0.5: V[y, x:x + int(rng.integers(5, 60))] = 0; Bn[y, x:x + 60] = 60
        else: V[y:y + int(rng.integers(5, 60)), x] = 0; Bn[y:y + 60, x] = 60
    g._V, g._Bnr, g._Fnr, g._tep = V, Bn, Fn, None

    def full():
        held = (V < 100) & (Bn >= 0) & (Bn > Fn)
        return _away(held, V >= 100, mc) if held.any() else held

    def _away(held, blocked, m):
        return E._away_from_walls(held, blocked, m)

    g._hold = full()
    for step in range(25):
        y0, x0 = int(rng.integers(0, H - 1)), int(rng.integers(0, W - 1))
        y1, x1 = min(H, y0 + int(rng.integers(1, 25))), min(W, x0 + int(rng.integers(1, 25)))
        for _ in range(2):
            m = rng.random((y1 - y0, x1 - x0)) < rng.choice([0.2, 0.6, 1.0])
            kind = int(rng.integers(0, 4))
            if kind == 0: V[y0:y1, x0:x1][m] = 100
            elif kind == 1: V[y0:y1, x0:x1][m] = 0
            elif kind == 2: Bn[y0:y1, x0:x1][m] = 100 + step
            else: Fn[y0:y1, x0:x1][m] = 100 + step
        want = full()
        got, win = g._hold_window((y0, y1, x0, x1))
        assert np.array_equal(got, want), (seed, step)
        assert win[0] <= y0 and win[1] >= y1 and win[2] <= x0 and win[3] >= x1
        g._hold = got


# ---------------------------------------------------------------------------------------------------------- the whole engine
def _leaf(rng, key, t, x, y, session, size=3.0):
    n = int(size / RES)
    w = np.zeros((n, n), np.uint8)
    for _ in range(int(rng.integers(0, 6))):                              # lethal blobs and unknown patches
        a, b = int(rng.integers(0, n)), int(rng.integers(0, n)); h, v = int(rng.integers(1, 14)), int(rng.integers(1, 14))
        w[a:a + h, b:b + v] = 100 if rng.random() < 0.7 else 255
    if rng.random() < 0.5: w[int(rng.integers(0, n)), :] = 100            # a wall
    return Leaf(key=key, session=session, stamp_ms=t, x=x, y=y, yaw=0.0, resolution=RES,
                origin=(x - size / 2, y - size / 2), window_u8=w)


def _same(a, b, what, planes=True):
    if not planes:                                  # an imported engine has a re-padded canvas: compare in global coordinates
        assert a._keys == b._keys and a.version == b.version and a._sig == b._sig and a._link_sig == b._link_sig, what
        return
    for name in ("_V", "_T", "_B", "_Fn", "_Bn", "_hold", "_val_h", "_clr_price_h", "_free_prev", "_key_plane"):
        x, y = getattr(a, name), getattr(b, name)
        assert (x is None) == (y is None), (what, name)
        if x is not None: assert np.array_equal(x, y), (what, name)
    assert (a._solved is None) == (b._solved is None), what
    if a._solved is not None: assert np.array_equal(a._solved.plane, b._solved.plane), what
    assert a._keys == b._keys and a.version == b.version, what
    assert a._ecache[1].shape == b._ecache[1].shape and np.array_equal(a._ecache[1], b._ecache[1]), what
    da, db = a._dcache[1], b._dcache[1]
    assert np.array_equal(np.sqrt(da, dtype=np.float64) if da.dtype != np.float64 else da,
                          np.sqrt(db, dtype=np.float64) if db.dtype != np.float64 else db), what


@pytest.mark.parametrize("mc,seed", [(None, 0), (0.3, 1), (0.3, 2), (0.3, 3), (0.5, 4), (None, 5), ("off", 6), ("off", 7)])
def test_windowed_build_equals_whole_canvas_over_random_updates(mc, seed):
    rng = np.random.default_rng(1000 + seed)
    if mc == "off": p = SceneParams()
    elif mc is None: p = SceneParams(near_range_m=1.2)
    else: p = SceneParams(near_range_m=1.2, near_min_clearance_m=mc)
    a, b = ChartGraph(p), _FullGraph(p)
    live = {}                                                              # key -> Leaf
    t, n = 1_000_000, 0
    x, y = 2.0, 2.0
    imported = False
    for step in range(40):
        r = rng.random()
        if r < 0.12 and len(live) > 3:                                     # refold: a leaf is deleted
            k = list(live)[int(rng.integers(0, len(live)))]
            lf = live.pop(k)
            box = BBox(lf.x - 1.7, lf.y - 1.7, lf.x + 1.7, lf.y + 1.7)
            keep = [l for l in live.values() if abs(l.x - lf.x) < 3.4 and abs(l.y - lf.y) < 3.4]
            da, db = a.refold(keep, box), b.refold(keep, box)
        else:
            batch = []
            for _ in range(int(rng.integers(1, 5))):
                rr = rng.random()
                if rr < 0.08:                                              # a jump: the canvas grows on some side
                    x += float(rng.choice([-9.0, 9.0, 0.0])); y += float(rng.choice([-9.0, 9.0, 0.0]))
                else:
                    x += float(rng.normal(0, 0.7)); y += float(rng.normal(0, 0.7))
                t += int(rng.integers(-300, 1000)) if rng.random() < 0.2 else 500   # sometimes out of capture order
                n += 1
                lf = _leaf(rng, "k%d" % n, t, x, y, "s%d" % int(rng.integers(0, 2)))
                batch.append(lf); live[lf.key] = lf
            if rng.random() < 0.1 and live:                                # a replay of leaves already folded
                batch.append(list(live.values())[int(rng.integers(0, len(live)))])
            da, db = a.fold_many(batch), b.fold_many(batch)
        assert da == db, step
        _same(a, b, (mc, seed, step), planes=not imported)
        if step == 20:                                                     # a state import in the middle: the next builds start cold
            c = ChartGraph(p); c.import_state(a.export_state())
            a = c; imported = True
            _same(a, b, (mc, seed, "import"), planes=False)
