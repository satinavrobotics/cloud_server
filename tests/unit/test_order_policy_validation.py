"""Validation of the order-policy environment, the nodePolicy wire format and the
id grammar builders."""
import logging

import pytest

from packages.controllers.mission import order_ids, order_policy
from packages.controllers.mission.order_policy import (
    NODE_POLICY_MAX_WAIT_CAP_S, OrderPolicy, clamp_max_wait_s)
from packages.controllers.mission.vda5050_types import vda5050_types as vt


def test_max_wait_clamped_above_cap(caplog):
    with caplog.at_level(logging.WARNING):
        policy = OrderPolicy(node_policy_max_wait_s=500)
    assert policy.node_policy_max_wait_s == NODE_POLICY_MAX_WAIT_CAP_S == 120.0
    assert "NODE_POLICY_MAX_WAIT_S" in caplog.text


def test_max_wait_valid_untouched(caplog):
    with caplog.at_level(logging.WARNING):
        assert OrderPolicy(node_policy_max_wait_s=45).node_policy_max_wait_s == 45
        assert OrderPolicy(node_policy_max_wait_s=120).node_policy_max_wait_s == 120
    assert not caplog.records


@pytest.mark.parametrize("bad", [-1, float("nan"), float("inf"), float("-inf"), "x"])
def test_max_wait_invalid_uses_default(bad):
    assert OrderPolicy(node_policy_max_wait_s=bad).node_policy_max_wait_s == 10.0
    assert clamp_max_wait_s(bad) == 10.0


@pytest.mark.parametrize("raw", ["10s", "abc", "nan", "inf", "-3", ""])
def test_malformed_env_float_falls_back(monkeypatch, caplog, raw):
    monkeypatch.setenv("ROUTE_DEVIATION_XY_PASS_M", raw)
    with caplog.at_level(logging.WARNING):
        assert order_policy._float("ROUTE_DEVIATION_XY_PASS_M", 0.35) == 0.35
    assert "ROUTE_DEVIATION_XY_PASS_M" in caplog.text


def test_env_float_valid_and_unset(monkeypatch):
    monkeypatch.setenv("X_F", " 0.5 ")
    assert order_policy._float("X_F", 1.0) == 0.5
    assert order_policy._float("X_UNSET_F", 1.0) == 1.0


def test_optional_float(monkeypatch, caplog):
    monkeypatch.setenv("X_O", "off")
    assert order_policy._optional_float("X_O", 0.1) is None
    monkeypatch.setenv("X_O", "oops")
    with caplog.at_level(logging.WARNING):
        assert order_policy._optional_float("X_O", 0.1) == 0.1
    assert "X_O" in caplog.text
    monkeypatch.setenv("X_O", "0.2")
    assert order_policy._optional_float("X_O", 0.1) == 0.2


def test_from_env_survives_malformed_values(monkeypatch):
    monkeypatch.setenv("NODE_POLICY_MAX_WAIT_S", "10s")
    monkeypatch.setenv("BLOCKED_NODE_EXCLUSION_MIN", "-5")
    monkeypatch.setenv("ROUTE_DEVIATION_ZERO_IS_UNSET", "flase")
    policy = order_policy.from_env()
    assert policy.node_policy_max_wait_s == 10.0
    assert policy.blocked_node_exclusion_min == 10.0
    assert policy.deviation_zero_is_unset is True  # default, not a silent False


@pytest.mark.parametrize("raw,expected", [
    ("1", True), ("true", True), (" TRUE ", True), ("yes", True), ("on", True),
    ("0", False), ("false", False), ("No", False), ("off", False)])
def test_bool_spellings(monkeypatch, raw, expected):
    monkeypatch.setenv("X_B", raw)
    assert order_policy._bool("X_B", not expected) is expected


@pytest.mark.parametrize("default", [True, False])
def test_bool_typo_warns_and_defaults(monkeypatch, caplog, default):
    monkeypatch.setenv("X_B", "ture")
    with caplog.at_level(logging.WARNING):
        assert order_policy._bool("X_B", default) is default
    assert "X_B" in caplog.text
    assert order_policy._bool("X_B_UNSET", default) is default


# ---- nodePolicy on the wire ----

def _params(action):
    return {p.key: p.value for p in action.actionParameters}


def _route_order(n_waypoints, policy=None):
    from packages.controllers.mission.vda5050_types import vda5050_types as v
    from cloud_common.objects import common, mission, robot
    route = mission.MissionRouteNodeV1(waypoints=[
        common.Pose2D(x=float(i), y=0.0, theta=0.0, map_id="m")
        for i in range(n_waypoints)])
    robo = robot.RobotObjectV1(name="r1", spec={}, status={})
    return v.VDA5050Order.from_route(route, robo, "m1", 3, node_policy=True,
                                     policy=policy)


def test_node_policy_never_on_start_or_last_and_not_skippable():
    order = _route_order(4)
    assert order.nodes[0].actions == []
    assert order.nodes[-1].actions == []
    middle = order.nodes[1:-1]
    assert len(middle) == 3
    for node in middle:
        (action,) = node.actions
        assert action.actionType == vt.NODE_POLICY_ACTION_TYPE
        assert action.actionId == order_ids.node_policy_action_id(node.nodeId)
        assert _params(action)["skippable"] is False
        assert _params(action)["maxWaitS"] == 10.0
        assert "corridorWidth" not in _params(action)


def test_node_policy_wire_maxwait_typed_and_clamped():
    action = vt.VDA5050Action.node_policy("n", max_wait_s=999)
    wire = action.dict()["actionParameters"]
    by_key = {p["key"]: p["value"] for p in wire}
    assert by_key["maxWaitS"] == 120.0 and isinstance(by_key["maxWaitS"], float)
    assert by_key["skippable"] is False
    assert vt.VDA5050Action.node_policy("n", 5).param_dict["maxWaitS"] == 5.0
    assert vt.VDA5050Action.node_policy("n", float("nan")).param_dict["maxWaitS"] == 10.0


@pytest.mark.parametrize("corridor", [None, 0.0, -1.0, float("nan")])
def test_skippable_without_usable_corridor_is_not_skippable(corridor):
    params = vt.VDA5050Action.node_policy(
        "n", 10, skippable=True, corridor_width_m=corridor).param_dict
    assert params["skippable"] is False
    assert "corridorWidth" not in params


def test_skippable_with_corridor():
    params = vt.VDA5050Action.node_policy(
        "n", 10, skippable=True, corridor_width_m=1.5).param_dict
    assert params["skippable"] is True and params["corridorWidth"] == 1.5


# ---- id grammar builders ----

def test_id_builders_exact_format():
    assert order_ids.order_id("m1", 3) == "m1-n3"
    assert order_ids.node_id("m1", 3, 4) == "m1-n3-s4"
    assert order_ids.node_action_id("m1-n3-s0", 3) == "m1-n3-s0-n3"
    assert order_ids.order_action_id("m1-n3", 3) == "m1-n3-s0-n3"
    prefix = order_ids.run_prefix("m1", "abcd", 2)
    assert order_ids.node_id(prefix, 0, 2) == "m1-rabcdv2-n0-s2"


def test_id_builders_round_trip_with_parsers():
    prefix = order_ids.run_prefix("m-1", "abcd", 1)
    order = order_ids.order_id(prefix, 7)
    node = order_ids.node_id(prefix, 7, 6)
    assert order_ids.order_of_node(node) == order
    assert order_ids.order_prefix(order) == prefix
    assert order_ids.order_node_index(order) == 7
    assert order_ids.node_index(node) == 7
    assert order_ids.node_sequence(node) == 6
    assert order_ids.is_node_of(prefix, node)
    assert order_ids.is_order_of(prefix, order)
    assert order_ids.node_of_reference(order_ids.node_policy_action_id(node)) == node


def test_vda5050_types_ids_unchanged():
    order = _route_order(3)
    assert order.orderId == "m1-n3"
    assert [n.nodeId for n in order.nodes] == [
        "m1-n3-s0", "m1-n3-s2", "m1-n3-s4", "m1-n3-s6"]
    assert [(e.edgeId, e.startNodeId, e.endNodeId) for e in order.edges] == [
        ("m1-n3-e1", "m1-n3-s0", "m1-n3-s2"), ("m1-n3-e3", "m1-n3-s2", "m1-n3-s4"),
        ("m1-n3-e5", "m1-n3-s4", "m1-n3-s6")]
