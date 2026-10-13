"""Per-waypoint nodePolicy fields reach the order: skippable only with a corridor, maxWaitS override,
the kill switch, the last node never has a policy (docs/node_policies_2026-10-13.md)."""
import pytest

from cloud_common.objects import common, mission, robot
from packages.controllers.mission import order_policy
from packages.controllers.mission.vda5050_types import vda5050_types as v


def _order(waypoints, policy=None):
    route = mission.MissionRouteNodeV1(waypoints=waypoints)
    robo = robot.RobotObjectV1(name="r1", spec={}, status={})
    return v.VDA5050Order.from_route(route, robo, "m1", 3, node_policy=True,
                                     policy=policy or order_policy.OrderPolicy())


def _wp(i, **kw):
    return common.Pose2D(x=float(i), y=0.0, map_id="m", **kw)


def _params(order, j):
    """nodePolicy params of route waypoint j (order node j + 1)."""
    acts = [a for a in order.nodes[j + 1].actions if a.actionType == v.NODE_POLICY_ACTION_TYPE]
    return acts[0].param_dict if acts else None


@pytest.mark.unit
def test_default_waypoints_are_not_skippable_and_carry_no_corridor():
    order = _order([_wp(i) for i in range(4)])
    for j in range(3):
        assert _params(order, j) == {"skippable": False, "maxWaitS": 10.0}
    assert _params(order, 3) is None            # the last node has no policy


@pytest.mark.unit
def test_skippable_with_corridor_is_sent_together():
    order = _order([_wp(0, skippable=True, corridor_width_m=0.8), _wp(1), _wp(2)])
    assert _params(order, 0) == {"skippable": True, "maxWaitS": 10.0, "corridorWidth": 0.8}
    assert _params(order, 1)["skippable"] is False


@pytest.mark.unit
@pytest.mark.parametrize("corridor", [None, 0.0])
def test_skippable_without_a_usable_corridor_is_not_skippable(corridor):
    order = _order([_wp(0, skippable=True, corridor_width_m=corridor), _wp(1), _wp(2)])
    assert _params(order, 0) == {"skippable": False, "maxWaitS": 10.0}


@pytest.mark.unit
def test_corridor_without_skippable_is_not_sent():
    order = _order([_wp(0, corridor_width_m=0.8), _wp(1)])
    assert _params(order, 0) == {"skippable": False, "maxWaitS": 10.0}


@pytest.mark.unit
def test_last_waypoint_is_never_skippable_even_if_it_asks():
    order = _order([_wp(0), _wp(1, skippable=True, corridor_width_m=1.0)])
    assert _params(order, 1) is None


@pytest.mark.unit
def test_max_wait_override_is_clamped_by_the_helper():
    order = _order([_wp(0, max_wait_s=30.0), _wp(1, max_wait_s=9999.0), _wp(2, max_wait_s=0.0),
                    _wp(3)])
    assert _params(order, 0)["maxWaitS"] == 30.0
    assert _params(order, 1)["maxWaitS"] == order_policy.NODE_POLICY_MAX_WAIT_CAP_S
    assert _params(order, 2)["maxWaitS"] == 0.0


@pytest.mark.unit
def test_kill_switch_sends_nothing_skippable():
    policy = order_policy.OrderPolicy(node_policy_skip_enabled=False)
    order = _order([_wp(0, skippable=True, corridor_width_m=0.8), _wp(1)], policy)
    assert _params(order, 0) == {"skippable": False, "maxWaitS": 10.0}


@pytest.mark.unit
def test_no_policy_when_the_order_is_built_without_node_policy():
    route = mission.MissionRouteNodeV1(
        waypoints=[_wp(0, skippable=True, corridor_width_m=0.8), _wp(1)])
    robo = robot.RobotObjectV1(name="r1", spec={}, status={})
    order = v.VDA5050Order.from_route(route, robo, "m1", 3, node_policy=False)
    assert all(not n.actions for n in order.nodes)


@pytest.mark.unit
def test_waypoint_fields_default_unchanged_and_validate():
    p = common.Pose2D(x=1.0)
    assert (p.skippable, p.max_wait_s, p.corridor_width_m, p.origin) == (None,) * 4
    assert common.Pose2D(origin="planned").origin == "planned"
    for bad in ({"max_wait_s": -1}, {"max_wait_s": float("nan")},
                {"corridor_width_m": float("inf")}, {"origin": "robot"}):
        with pytest.raises(common.ICSUsageError):
            common.Pose2D(**bad)
