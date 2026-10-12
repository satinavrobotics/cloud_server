import numpy as np
import pytest

from packages.scene_graph import Params
from packages.scene_graph.graph import bottleneck, coarsen, fields, lattice_graph

pytestmark = pytest.mark.unit
P = Params()


def _room(h=60, w=80):
    v = np.zeros((h, w), np.uint8)
    v[0, :] = v[-1, :] = v[:, 0] = v[:, -1] = 100
    return v


def test_clearance_is_distance_to_the_nearest_blocked_or_unknown_cell():
    v = _room()
    f = fields(v, P)
    assert f.clear[30, 40] == pytest.approx(min(30, 40) * P.res, abs=P.res)
    v[30, 40] = 255                                # unknown counts as blocked
    assert fields(v, P).clear[30, 41] == pytest.approx(P.res)


def test_cells_closer_than_the_robot_radius_are_not_navigable():
    f = fields(_room(), P)
    assert not f.nav[3, 40] and f.nav[30, 40]
    assert not f.nav[0, 40]                        # lethal


def test_costs_grow_towards_walls_and_with_soft_values():
    v = _room()
    v[30, 30:50] = 50
    f = fields(v, P)
    assert f.cost[10, 40] < f.cost[6, 40]          # nearer the wall: dearer
    assert np.isinf(f.cost[0, 40])


def test_coarse_step_cannot_cross_a_wall():
    """Two navigable cells on opposite sides of a thin wall must not become neighbours in the coarse graph."""
    v = np.zeros((40, 120), np.uint8)
    v[:, 60] = 100                                 # a wall one cell thick
    f = fields(v, P)
    c = coarsen(f, P.block)
    g, idx = lattice_graph(c.nav, c.cost, P.block * P.res)
    left = idx[c.nav.shape[0] // 2, (60 // P.block) - 3]
    right = idx[c.nav.shape[0] // 2, (60 // P.block) + 3]
    from scipy.sparse.csgraph import connected_components
    _, comp = connected_components(g.length, directed=False)
    assert comp[left] != comp[right]


def test_bottleneck_is_the_narrowest_point_on_the_tree_path():
    pred = np.array([-1, 0, 1, 2, 1])
    c = np.array([5.0, 4.0, 2.0, 3.0, 9.0])
    assert bottleneck(pred, c, 0).tolist() == [5.0, 4.0, 2.0, 2.0, 4.0]
