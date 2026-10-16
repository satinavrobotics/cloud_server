"""The place rule (packages/scene_graph/place.py): unit tests of the pure functions, the SceneParams contract and the pipeline token.

Traceability: the rule and its two thresholds are the decision recorded in place.py's module docstring; the thresholds come from
the robot (`robot_place_scales`), never from a count of cells.
"""
import math

import numpy as np
import pytest

from packages.scene_graph import place
from packages.scene_graph.api import SceneParams
from packages.scene_graph.engine import _state_params
from packages.scene_graph.store import PIPELINE, PIPELINE_CLOSE, PIPELINE_PLACE, PIPELINE_VALID, _pipeline

pytestmark = pytest.mark.unit


def _strip(n, h=0.1):
    """a 1-cell-wide row of n lattice cells"""
    ys = np.zeros(n, int)
    xs = np.arange(n)
    return ys, xs


def test_robot_scales_are_circumscribed_radius_and_side():
    peak, path = place.robot_place_scales(0.25)
    assert math.isclose(peak, 0.25 * math.sqrt(2)) and math.isclose(path, 0.5)


def test_place_params_derive_from_the_half_side_once():
    peak, path = place.place_params_from_half_side(0.25)
    assert (peak, path) == place.robot_place_scales(0.25)
    with pytest.raises(ValueError):
        place.place_params_from_half_side(0.0)


def test_geodesic_span_of_a_straight_strip_is_its_length():
    ys, xs = _strip(6)
    assert math.isclose(place.geodesic_span(ys, xs, 0.1), 0.5)


def test_geodesic_span_uses_diagonals_not_the_bounding_box():
    # a run of 4 cells then one cell below the last: the path goes 2 steps along the run, then one diagonal step (sqrt 2 h)
    ys = np.array([0, 0, 0, 0, 1]); xs = np.array([0, 1, 2, 3, 3])
    assert math.isclose(place.geodesic_span(ys, xs, 0.1), 0.2 + math.sqrt(2) * 0.1)
    assert place.geodesic_span(np.array([0]), np.array([0]), 0.1) == 0.0


def test_a_peak_at_robot_scale_is_a_place_without_any_driving():
    f = np.array([0.1, 0.2, 0.36, 0.2, 0.1])          # a region whose peak reaches the circumscribed radius (0.354)
    region = np.zeros(5, int)
    keep = place.place_regions(f, region, np.zeros(5, bool), *_strip(5), 0.1, 0.354, 0.5)
    assert keep.all()


def test_a_driven_shallow_corridor_is_a_place_by_traversal():
    n = 7
    f = np.full(n, 0.1)                                # narrower than the robot everywhere
    driven = np.ones(n, bool)                          # the robot drove over the whole strip, 0.6 m long
    keep = place.place_regions(f, np.zeros(n, int), driven, *_strip(n), 0.1, 0.354, 0.5)
    assert keep.all()


def test_a_single_driven_cell_in_a_narrow_region_is_not_a_place():
    f = np.array([0.1])
    keep = place.place_regions(f, np.zeros(1, int), np.ones(1, bool), *_strip(1), 0.1, 0.354, 0.5)
    assert not keep.any()


def test_a_region_is_decided_separately_from_its_neighbours():
    f = np.array([0.1, 0.1, 0.1, 0.5, 0.5])
    region = np.array([0, 0, 0, 1, 1])
    driven = np.array([False, False, False, False, False])
    keep = place.place_regions(f, region, driven, *_strip(5), 0.1, 0.354, 0.5)
    assert not keep[:3].any() and keep[3:].all()


def test_params_place_values_are_both_or_neither_and_positive():
    SceneParams()                                      # off by default
    SceneParams(place_peak_m=0.354, place_path_m=0.5)
    with pytest.raises(ValueError):
        SceneParams(place_peak_m=0.354)
    with pytest.raises(ValueError):
        SceneParams(place_peak_m=0.0, place_path_m=0.5)


def test_pipeline_token_is_unchanged_when_the_rule_is_off():
    assert _pipeline({"robot_half_side": 0.25, "lattice": 0.1, "chart_radius": 2.0}) == PIPELINE
    assert _pipeline({"near_range_m": 1.3}) == PIPELINE_VALID  # the rule-on token now carries val (store.PIPELINE_VALID)
    assert _pipeline({"near_range_m": 1.3, "place_peak_m": 0.354, "place_path_m": 0.5}) == PIPELINE_PLACE
    assert "place_peak_m" not in _state_params(SceneParams(near_range_m=1.3))


# ---- engine: the rule is applied on every path that cuts charts, and off is the plain fold -------------------------------
from packages.scene_graph.api import Leaf  # noqa: E402
from packages.scene_graph.engine import ChartGraph  # noqa: E402

R_CELL = 0.05


def _room_leaf(key, t, x, y):
    """an 8 x 6 m room seen free from the pose (x, y)"""
    w = np.zeros((120, 160), np.uint8)
    return Leaf(key=key, session="s", stamp_ms=t, x=x, y=y, yaw=0.0, resolution=R_CELL, origin=(0.0, 0.0), window_u8=w)


def _drive_room(**kw):
    g = ChartGraph(SceneParams(near_range_m=1.5, **kw))
    for i, x in enumerate(np.arange(1.0, 7.01, 0.5)):
        g.fold_many([_room_leaf("k%d" % i, 1000 + i, float(x), 2.5)])
    return g


def test_rule_off_is_the_plain_partition():
    a = _drive_room()
    b = _drive_room(place_peak_m=None, place_path_m=None)
    assert a._solved.plane.tolist() == b._solved.plane.tolist()


def test_rule_on_keeps_a_room_that_is_a_place_and_stores_the_filtered_plane():
    on = _drive_room(place_peak_m=0.354, place_path_m=0.5)
    off = _drive_room()
    assert on._solved is not None and off._solved is not None
    # the room's peak (about 3 m) is far above the robot's circumscribed radius: nothing is removed, the charts are the same
    assert on._solved.plane.tolist() == off._solved.plane.tolist()
    assert on._place_filter(on._build_F((0, 0))[0]).sum() == on._build_F((0, 0))[0].sum()
