import numpy as np
import pytest
from scipy import ndimage

from packages.scene_graph import engine as E

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("r", [0.25, 0.3])
def test_erode_union_matches_scipy(r):
    sqs = E._squares(r); tbl = E._square_rows(r)
    assert tbl is not None
    rng = np.random.default_rng(7)
    for H, W, dens in [(1, 1, .5), (5, 40, .1), (40, 5, .1), (13, 13, .0), (30, 31, .02), (64, 70, .05), (80, 60, .3),
                       (50, 50, 1e-3), (90, 120, .01), (25, 200, .9)]:
        for _ in range(3):
            free = rng.random((H, W)) > dens
            ref = np.zeros_like(free)
            for st in sqs: ref |= ndimage.binary_erosion(free, structure=st, border_value=0)
            assert np.array_equal(E._erode_union(free, sqs, tbl), ref)
    free = np.ones((60, 70), bool)
    ref = np.zeros_like(free)
    for st in sqs: ref |= ndimage.binary_erosion(free, structure=st, border_value=0)
    assert np.array_equal(E._erode_union(free, sqs, tbl), ref)
    assert not E._erode_union(np.zeros((9, 9), bool), sqs, tbl).any()


def _away_ref(held, blocked, mc):
    lab, n = ndimage.label(held, structure=np.ones((3, 3)))
    if not n: return np.zeros(held.shape, bool)
    if not blocked.any(): return held.copy()
    dist = ndimage.distance_transform_edt(~blocked) * E.RES
    far = np.asarray(ndimage.maximum(dist, lab, index=np.arange(1, n + 1))) > mc
    return np.concatenate(([False], far))[lab] & held


def test_away_from_walls_matches_full_canvas():
    rng = np.random.default_rng(3)
    for H, W in [(1, 1), (30, 40), (120, 90), (200, 200)]:
        for dh in (0.0, 1e-3, 0.02, 0.2):
            for db in (0.0, 0.001, 0.01, 0.1):
                for mc in (0.25, 0.3, 0.0):
                    blocked = rng.random((H, W)) < db
                    held = (rng.random((H, W)) < dh) & ~blocked
                    if dh > 0.01:                                   # blobs, to get real components
                        held = ndimage.binary_dilation(held, iterations=2) & ~blocked
                    assert np.array_equal(E._away_from_walls(held, blocked, mc), _away_ref(held, blocked, mc))


def test_erode_union_bands_match_scipy():
    sqs = E._squares(0.25); tbl = E._square_rows(0.25)
    rng = np.random.default_rng(11)
    for H, W in [(200, 37), (97, 120), (300, 20)]:
        free = rng.random((H, W)) > 0.03
        ref = np.zeros_like(free)
        for st in sqs: ref |= ndimage.binary_erosion(free, structure=st, border_value=0)
        for bc in (1, 300, 1000, 5000):
            assert np.array_equal(E._erode_union(free, sqs, tbl, band_cells=bc), ref)
