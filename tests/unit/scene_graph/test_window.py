import math

import numpy as np
import pytest

from packages.scene_graph.window import BLOCKED, UNKNOWN, to_lattice

pytestmark = pytest.mark.unit


def _world():
    w = np.zeros((120, 160), np.uint8)               # 6 m x 8 m free room at 0.05 m, origin (0, 0)
    w[:3, :] = w[-3:, :] = 100; w[:, :3] = w[:, -3:] = 100
    w[40:60, 70:75] = 100                            # a wall segment
    w[10:30, 100:140] = UNKNOWN                      # an unseen corner
    return w


def _sample(world, origin, yaw, rows, cols, res=0.05):
    """what a robot with a rotated window would deliver: the world seen on a rotated grid"""
    c, s = math.cos(yaw), math.sin(yaw)
    j, i = np.mgrid[0:rows, 0:cols]
    x = origin[0] + ((i + .5) * res) * c - ((j + .5) * res) * s
    y = origin[1] + ((i + .5) * res) * s + ((j + .5) * res) * c
    xi, yi = np.floor(x / 0.05).astype(int), np.floor(y / 0.05).astype(int)
    ok = (xi >= 0) & (xi < world.shape[1]) & (yi >= 0) & (yi < world.shape[0])
    out = np.full((rows, cols), UNKNOWN, np.uint8); out[ok] = world[yi[ok], xi[ok]]
    return out


def test_identity_is_exact():
    w = _world()
    out, o = to_lattice(w, 0.05, (0.0, 0.0), 0.0)
    assert o == (0.0, 0.0) and (out == w).all()


def test_quarter_turn_is_exact():
    w = _world()
    src = _sample(w, (5.0, 1.0), math.pi / 2, 100, 100)      # window rotated by 90 degrees, footprint x 0..5, y 1..6
    out, (x0, y0) = to_lattice(src, 0.05, (5.0, 1.0), math.pi / 2)
    gx, gy = round(x0 / 0.05), round(y0 / 0.05)
    h, wd = out.shape
    truth = w[gy:gy + h, gx:gx + wd]
    assert out.shape == (100, 100) and (out == truth).all()  # a quarter turn permutes cells: exact, no loss


@pytest.mark.parametrize("deg", [17, 45, 133, -60])
def test_arbitrary_rotation_agrees_with_the_world_and_never_invents_values(deg):
    w = _world(); yaw = math.radians(deg)
    src = _sample(w, (3.0, 2.0), yaw, 90, 90)
    out, (x0, y0) = to_lattice(src, 0.05, (3.0, 2.0), yaw)
    gx, gy = round(x0 / 0.05), round(y0 / 0.05)
    h, wd = out.shape
    ys, xs = np.mgrid[0:h, 0:wd]
    ok = (gy + ys >= 0) & (gy + ys < w.shape[0]) & (gx + xs >= 0) & (gx + xs < w.shape[1])
    truth = np.full(out.shape, UNKNOWN, np.uint8); truth[ok] = w[(gy + ys)[ok], (gx + xs)[ok]]
    known = out != UNKNOWN
    agree = (out[known] == truth[known]).mean()
    assert agree > 0.94                                       # edge cells only (nearest neighbour, half a cell)
    assert set(np.unique(out)) <= set(np.unique(src)) | {UNKNOWN}   # values are never mixed


def test_unknown_outside_the_footprint_and_inside_stays_unknown():
    src = np.zeros((20, 20), np.uint8); src[5:10, 5:10] = UNKNOWN
    out, _ = to_lattice(src, 0.05, (0.0, 0.0), math.radians(30))
    assert (out[~np.isin(out, [0])] == UNKNOWN).all()         # nothing but free and unknown appear
    assert (out == UNKNOWN).any() and (out == 0).any()


def test_finer_source_keeps_the_blocked_side():
    src = np.zeros((4, 4), np.uint8); src[1, 1] = BLOCKED     # one blocked 0.025 m cell
    out, _ = to_lattice(src, 0.025, (0.0, 0.0), 0.0, target_res=0.05)
    assert out.shape == (2, 2) and out[0, 0] == BLOCKED and out[1, 1] == 0


def test_coarser_source_replicates():
    src = np.array([[0, 100], [255, 0]], np.uint8)
    out, _ = to_lattice(src, 0.1, (0.0, 0.0), 0.0, target_res=0.05)
    assert out.shape == (4, 4) and out[0, 2] == 100 and out[2, 0] == UNKNOWN and (out[:2, :2] == 0).all()
