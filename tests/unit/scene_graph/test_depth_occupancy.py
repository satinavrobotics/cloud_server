
import numpy as np
import pytest

from packages.scene_graph.depth_occupancy import (FREE, OCCUPIED, UNKNOWN, floor_level, occupancy_from_depth,
                                                  quat_matrix)

pytestmark = pytest.mark.unit

CAM = dict(fx=174.0, fy=174.0, cx=224.0, cy=168.0, valid_range_m=(0.2, 10.0))
T = dict(x=0.117, y=0.033, z=0.26, qx=-0.4987, qy=0.5014, qz=-0.5041, qw=0.4958)    # optical z -> base +x
POSE = dict(x=0.0, y=0.0, z=-0.25, qx=0.0, qy=0.0, qz=0.0, qw=1.0)


def _scene(wall_x=3.0, floor_z=0.0, box=None, seed=0, valid=0.25):
    """depth image of: flat floor at base-relative height floor_z, a wall at x = wall_x, optional box (x0,x1,y0,y1,bottom,top), heights relative to base"""
    H, W = 336, 448
    v, u = np.mgrid[0:H, 0:W]
    ray_opt = np.stack([(u - CAM["cx"]) / CAM["fx"], (v - CAM["cy"]) / CAM["fy"], np.ones_like(u, float)], -1)
    R = quat_matrix(T["qx"], T["qy"], T["qz"], T["qw"])
    ray = ray_opt @ R.T                                   # base frame; |ray_opt.z| = 1 so t is the optical depth
    c = np.array([T["x"], T["y"], T["z"]])
    t_floor = np.where(ray[..., 2] < 0, (floor_z - c[2]) / ray[..., 2], np.inf)
    t_wall = np.where(ray[..., 0] > 0, (wall_x - c[0]) / ray[..., 0], np.inf)
    t = np.minimum(t_floor, t_wall)
    if box:
        x0, x1, y0, y1, bottom, top = box
        tb = np.where(ray[..., 0] > 0, (x0 - c[0]) / ray[..., 0], np.inf)
        yy = c[1] + tb * ray[..., 1]; zz = c[2] + tb * ray[..., 2]
        hit = (yy >= y0) & (yy <= y1) & (zz >= bottom) & (zz <= top) & (tb < t)
        t = np.where(hit, tb, t)
    t = np.where(np.isfinite(t) & (t > 0.2) & (t < 10), t, 0.0)
    rng = np.random.default_rng(seed)
    t = np.where(rng.random(t.shape) < valid, t, 0.0)       # sparse validity like the real sensor
    return np.round(t / 0.001).astype(np.uint16)


def _run(depth, **kw):
    return occupancy_from_depth(depth, 0.001, CAM, T, POSE, origin=(-1.0, -4.0), shape=(160, 160), **kw)


def test_floor_is_free_wall_is_occupied_behind_is_unknown():
    w = _run(_scene(wall_x=3.0))
    col = lambda x: int((x + 1.0) / 0.05)
    row = int((0.0 + 4.0) / 0.05)
    assert w[row, col(1.0)] == FREE and w[row, col(2.5)] == FREE
    assert w[row, col(3.0)] == OCCUPIED                       # the wall
    assert w[row, col(3.6)] == UNKNOWN                        # behind it nothing was seen
    assert set(np.unique(w).tolist()) <= {FREE, OCCUPIED, UNKNOWN}


def test_floor_not_at_base_height_is_still_floor():
    # the floor 8 cm BELOW base z (a different robot / ramp): per-leaf floor level must absorb it
    w = _run(_scene(wall_x=3.0, floor_z=-0.08))
    row = int(4.0 / 0.05)
    assert w[row, int((1.5 + 1.0) / 0.05)] == FREE and w[row, int((3.0 + 1.0) / 0.05)] == OCCUPIED


def test_height_band_obstacle_vs_overhang():
    sel = lambda w: w[int(3.7 / 0.05):int(4.3 / 0.05), int(3.0 / 0.05):int(3.3 / 0.05)]
    low = _run(_scene(box=(2.0, 2.2, -0.3, 0.3, 0.0, 0.3)))                 # 30 cm object on the floor: inside 0.5 m
    assert (sel(low) == OCCUPIED).any()
    over = _scene(box=(2.0, 2.2, -0.3, 0.3, 0.7, 0.9))                     # a shelf whose underside is above the robot
    assert not (sel(_run(over, robot_height=0.5)) == OCCUPIED).any()
    assert (sel(_run(over, robot_height=1.0)) == OCCUPIED).any()          # the band is the robot's height, a parameter


def test_floor_level_is_the_dominant_plane():
    rng = np.random.default_rng(1)
    h = np.concatenate([rng.normal(0.07, 0.02, 4000), rng.uniform(0.3, 2.5, 3000)])
    assert floor_level(h) == pytest.approx(0.07, abs=0.01)


def test_empty_depth_is_all_unknown():
    w = _run(np.zeros((336, 448), np.uint16))
    assert (w == UNKNOWN).all()


def test_chunked_ray_march_gives_the_same_window():
    depth = _scene(wall_x=3.0, box=(1.5, 1.8, -0.3, 0.3, 0.0, 0.3))
    whole = _run(depth, ray_cells=10 ** 12)
    for cells in (1, 997, 50_000):
        assert np.array_equal(_run(depth, ray_cells=cells), whole)
