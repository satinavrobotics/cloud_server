"""cloud_common.objects.map.half_side: half of the smaller footprint side, a default when unknown."""
import pytest

from cloud_common.objects.map import ROBOT_HALF_SIDE_DEFAULT_M, half_side


def test_known_is_half_of_the_smaller_side():
    assert half_side(1.2, 0.7) == (0.35, True)
    assert half_side(0.5, 2) == (0.25, True)


@pytest.mark.parametrize("length,width", [(-1, -1), (1.0, -1), (None, 1.0), (1.0, None),
                                          (float("nan"), 1.0), (1.0, float("inf")), (0, 1.0),
                                          ("abc", 1.0)])
def test_unknown_gives_the_default(length, width):
    assert half_side(length, width) == (ROBOT_HALF_SIDE_DEFAULT_M, False)
    assert half_side(length, width, default=0.4) == (0.4, False)


def test_scene_adapter_reexports_the_same_function():
    from packages.services.graph_builder import scene_adapter
    assert scene_adapter.half_side is half_side
    assert scene_adapter.DEFAULT_HALF_SIDE == ROBOT_HALF_SIDE_DEFAULT_M
