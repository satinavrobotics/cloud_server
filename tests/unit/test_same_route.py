"""The one definition of route sameness (cloud_common.objects.mission.same_route / route_digest)."""
import pytest

from cloud_common.objects import common
from cloud_common.objects import mission as m


def _wps(n=3, **kw):
    return [common.Pose2D(x=float(i), y=0.0, map_id="m", node_id=f"n{i}", **kw) for i in range(n)]


def _route(wps=None, **kw):
    return m.MissionRouteNodeV1(waypoints=wps or _wps(), **kw)


LEG = m.RouteTrajectoryLegV1(from_index=0, to_index=1, points=[[0.0, 0.0], [1.0, 0.0]])


@pytest.mark.unit
def test_chart_key_is_ignored():
    assert m.same_route(_route(_wps(chart_key="c")), _route())
    assert m.route_digest(_route(_wps(chart_key="c"))) == m.route_digest(_route())


@pytest.mark.unit
def test_trajectory_graph_version_cost_model_are_ignored():
    a = _route(trajectory=[LEG], graph_version=4, cost_model="h:1")
    assert m.same_route(a, _route())
    assert m.route_digest(a) == m.route_digest(_route())


@pytest.mark.unit
def test_a_waypoint_change_is_detected():
    moved = _wps()
    moved[1].x = 9.0
    assert not m.same_route(_route(), _route(moved))
    assert m.route_digest(_route()) != m.route_digest(_route(moved))
    assert not m.same_route(_route(), _route(_wps(2)))


@pytest.mark.unit
def test_order_matters():
    wps = _wps()
    assert not m.same_route(_route(wps), _route(list(reversed(wps))))


@pytest.mark.unit
def test_none_empty_and_plain_lists():
    assert m.same_route(None, None) and m.same_route(None, []) and m.same_route([], None)
    assert not m.same_route(None, _route())
    assert m.same_route(_wps(), _route())
