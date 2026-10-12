import math

import numpy as np
import pytest

from packages.scene_graph.leafcover import (View, cam_yaw_offset, cover, cover_exact, range_cap, select_views, view_from_node,
                                            visible)

pytestmark = pytest.mark.unit
RES = 0.05


def test_camera_axis_is_robot_forward_for_the_robot_extrinsic():
    assert abs(cam_yaw_offset((-0.4987, 0.5014, -0.5041, 0.4958))) < 0.02


def test_range_cap_is_where_one_valid_pixel_per_cell_remains():
    # p * (fx*h/z)^2 = 1 at z = fx*h*sqrt(p)
    z = range_cap(174.0, 0.1, 0.2)
    assert z == pytest.approx(174.0 * 0.1 * math.sqrt(0.2))
    assert 0.2 * (174.0 * 0.1 / z) ** 2 == pytest.approx(1.0)


def test_view_uses_the_smaller_of_sensor_range_and_density_cap():
    v = view_from_node("a", {"x": 0, "y": 0, "yaw": 0.0}, {"x": 0.1, "y": 0, "qx": -0.4987, "qy": 0.5014, "qz": -0.5041, "qw": 0.4958},
                       174.0, 448, 10.0, 0.1, 0.2)
    assert v.rng == pytest.approx(range_cap(174.0, 0.1, 0.2)) and v.rng < 10.0
    assert v.x == pytest.approx(0.1) and v.fov == pytest.approx(2 * math.atan(448 / 348.0))


def _room():
    free = np.zeros((40, 60), bool); free[5:35, 5:55] = True
    solid = np.zeros_like(free); solid[4, 4:56] = solid[35, 4:56] = solid[4:36, 4] = solid[4:36, 55] = True
    return free, solid


def test_a_ray_sees_the_wall_it_stops_at_but_not_behind_it():
    free, solid = _room(); free[:, 30] = False; solid[:, 30] = True       # inner wall at x = 30
    v = View("a", 10.5 * RES, 20.5 * RES, 0.0, math.radians(30), 2.0)
    s = visible(v, free, solid, RES, (0.0, 0.0)); xs = s % 60
    assert 30 in xs and xs.max() == 30


def test_unknown_stops_the_ray_and_is_not_counted_as_seen():
    free, solid = _room(); free[20, 20] = False                            # unknown cell (not free, not solid)
    v = View("a", 10.5 * RES, 20.5 * RES, 0.0, math.radians(0.5), 2.0)
    s = visible(v, free, solid, RES, (0.0, 0.0))
    assert 20 not in (s % 60)[s // 60 == 20] and (s % 60).max() < 20


def test_select_returns_the_minimal_k2_set_and_the_ceiling():
    free, solid = _room()
    views = [View("w", 8.5 * RES, 20.5 * RES, 0.0, math.radians(100), 2.4),     # looks east
             View("e", 51.5 * RES, 20.5 * RES, math.pi, math.radians(100), 2.4),  # looks west
             View("w2", 9.5 * RES, 10.5 * RES, 0.0, math.radians(100), 2.4),
             View("far", 8.5 * RES, 20.5 * RES, math.pi, math.radians(100), 2.4)]  # faces the wall behind it
    W = 30
    wanted = np.array([r * W + c for r in range(3, 17) for c in range(3, 27)])   # most of the room, lattice step 0.1
    keys, ceiling, optimal = select_views(views, free, solid, RES, (0.0, 0.0), wanted, W, k=1)
    assert optimal and 0 < len(keys) <= 3 and ceiling > 0.5


def test_cover_prefers_the_leaf_that_sees_more_and_reports_the_ceiling():
    sights = {"a": np.array([1, 2, 3, 4]), "b": np.array([3, 4, 5]), "c": np.array([9])}
    out, ceiling = cover(sights, np.array([1, 2, 3, 4, 5, 6]), k=1)
    assert [lf for lf, _ in out] == ["a", "b"]
    assert out[-1][1] == pytest.approx(5 / 6) and ceiling == pytest.approx(5 / 6)


def test_k2_exact_is_not_larger_than_greedy_and_covers_twice():
    sights = {"a": np.array([1, 2]), "b": np.array([1, 2]), "c": np.array([2, 3]), "d": np.array([3, 1])}
    greedy, _ = cover(sights, np.array([1, 2, 3]), k=2)
    exact, optimal = cover_exact(sights, np.array([1, 2, 3]), k=2)
    assert optimal and len(exact) <= len(greedy)
    seen = np.zeros(4, int)
    for lf in exact:
        for c in sights[lf]: seen[c] += 1
    assert (seen[1:] >= 2).all()
