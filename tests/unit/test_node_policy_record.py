"""The sent-order record says what each nodePolicy said and why (docs/node_policies_v2_plan.md, 3.1/3.2)."""
import pytest

from cloud_common.objects import common, mission, robot
from packages.controllers.mission import order_policy
from packages.controllers.mission.vda5050_types import vda5050_types as v


def _wp(i, **kw):
    return common.Pose2D(x=float(i), y=0.0, map_id="m", **kw)


def _describe(waypoints, policy=None, offset=0):
    policy = policy or order_policy.OrderPolicy()
    route = mission.MissionRouteNodeV1(waypoints=waypoints)
    order = v.VDA5050Order.from_route(
        route, robot.RobotObjectV1(name="r1", spec={}, status={}), "m1", 3,
        node_policy=True, policy=policy)
    return v.describe_node_policies(order, waypoints, policy, offset)


@pytest.mark.unit
def test_every_reason_is_recorded_with_the_wire_values():
    nodes = _describe([
        _wp(0, origin="planned", skippable=True, corridor_width_m=0.8),   # ok
        _wp(1, origin="planned", skippable=True, corridor_width_m=0.0),   # narrow
        _wp(2, origin="user", corridor_width_m=0.6),                      # operator
        _wp(3, origin="planned"),                                         # unknown
        _wp(4, origin="planned", corridor_width_m=0.7),                   # gate only
        _wp(5, origin="user"),                                            # last
    ], offset=2)
    assert [n["reason"] for n in nodes] == [
        "ok", "narrow", "operator", "unknown:no_corridor", "gate_only", "structural_last"]
    assert [n["waypoint_index"] for n in nodes] == [2, 3, 4, 5, 6, 7]
    assert [n["seq"] for n in nodes] == [2, 4, 6, 8, 10, 12]
    assert nodes[0]["skippable"] is True and nodes[0]["corridor"] == 0.8
    assert nodes[1]["corridor"] == 0.0 and nodes[3]["corridor"] is None
    assert nodes[5]["corridor"] is None and nodes[5]["max_wait_s"] is not None


@pytest.mark.unit
def test_skip_switch_off_is_told_apart():
    policy = order_policy.OrderPolicy(node_policy_skip_enabled=False)
    nodes = _describe([_wp(0, origin="planned", skippable=True, corridor_width_m=0.8), _wp(1)],
                      policy=policy)
    assert nodes[0]["reason"] == "skip_disabled" and nodes[0]["skippable"] is False


@pytest.mark.unit
def test_summary_counts_by_reason():
    rec = mission.MissionSentOrderV1(order_id="o", node_policy={
        "sent": True, "withheld_reason": None,
        "nodes": [{"reason": "ok", "skippable": True}, {"reason": "ok", "skippable": True},
                  {"reason": "narrow", "skippable": False}]})
    assert rec.node_policy_summary() == {"sent": True, "withheld_reason": None, "nodes": 3,
                                         "skippable": 2, "reasons": {"ok": 2, "narrow": 1}}
    assert mission.MissionSentOrderV1(order_id="o").node_policy_summary() is None
