"""Unit tests for multi-waypoint missions a robot carries out on its own when it loses its
connection mid-route (robot team, 2026-10-08).

- DIGEST: adding optional waypoint fields does not change a stored route's digest.
- ACKS: an order action listed in actionStates does not hide a cancelOrder ack behind it,
  and does not complete or fail an action node.
- BLOCKED: a robot that drops its order because a node is blocked is not re-sent the
  same route; a stale "canceled" while the robot executes is ignored.
- OFFLINE: the mission timeout does not run while the robot is offline.
- DEVIATION: allowedDeviationXY/Theta on every node, from the order policy unless set.
- NODE POLICY: nodePolicy actions on pass-through nodes, typed values, stable ids, gating.
"""
import asyncio
import datetime
import json

import pytest

import cloud_common.objects as api_objects
import cloud_common.objects.common as common
import cloud_common.objects.mission as mission_object
import cloud_common.objects.robot as robot_object
import packages.controllers.mission.server as server_module
import packages.controllers.mission.vda5050_types as types
from packages.controllers.mission import order_ids, order_policy
from tests.unit.test_mission_lifecycle_fixes import (
    State, _cancel_done, _make_robot, _mission, _order_id, _orders, _rerouted, _route,
    _start, _state, _tree)

POLICY = order_policy.OrderPolicy()


def _robot_obj(x=0.0, y=0.0):
    r = api_objects.RobotObjectV1(name="r1", status={})
    r.status.pose.x, r.status.pose.y = x, y
    return r


def _route_of(*waypoints):
    return mission_object.MissionRouteNodeV1(waypoints=list(waypoints))


def _policy_state(node_id, status=types.VDA5050ActionStatus.WAITING):
    return types.VDA5050ActionState(
        actionId=order_ids.node_policy_action_id(node_id),
        actionType=types.NODE_POLICY_ACTION_TYPE, actionStatus=status)


def _edge_blocked(node_id):
    return types.VDA5050Error(
        errorType="edgeBlocked", errorLevel=types.VDA5050ErrorLevel.WARNING,
        errorDescription="Edge blocked: waypoint 1 unreachable",
        errorReferences=[types.VDA5050ErrorReference(referenceKey="nodeId",
                                                     referenceValue=node_id)])


def _info(state, mission_status):
    state.information = [types.VDA5050Info(
        infoType="missionStatus", infoDescription=mission_status, infoLevel="INFO")]
    return state


# ---------------------------------------------------------------------------
# DIGEST
# ---------------------------------------------------------------------------
@pytest.mark.unit
def test_a_stored_routes_digest_is_unchanged_by_the_new_waypoint_fields():
    # As a route stored before Pose2D.node_id existed reads back (every field explicit);
    # the digest was computed with the model of 9436f20.
    stored = {"waypoints": [
        {"allowedDeviationTheta": 0.0, "allowedDeviationXY": 0.1, "map_id": "M",
         "theta": 0.5, "x": 1.0, "y": 2.0},
        {"allowedDeviationTheta": 0.0, "allowedDeviationXY": 0.1, "map_id": "",
         "theta": 0.0, "x": 3.0, "y": 4.0}]}
    route = mission_object.MissionRouteNodeV1(**stored)
    assert server_module._route_digest(route) == "1375510c23c826f4"


# ---------------------------------------------------------------------------
# ACKS
# ---------------------------------------------------------------------------
@pytest.mark.unit
async def test_a_cancel_ack_listed_before_node_policy_states_is_seen():
    r, _ = _make_robot()
    await _start(r, _mission())
    order = _order_id(r)
    await r._on_client_message(_state(order))         # the robot took the order
    await r._on_mission_change(_rerouted())          # a cancelOrder for the reroute
    # Order action states after the instant action's: scanned from the end, they come
    # first; the ack must not be missed (it used to stop at the first one).
    actions = [_cancel_done(r)] + [_policy_state(f"{order}-s{s}") for s in (2, 4, 6)]
    await r._on_client_message(_state(order, actions=actions))

    assert not r._has_outstanding_cancel()
    assert len(_orders(r)) == 2                      # the rerouted order went out
    r._cancel_mission_timeout()


def _action_mission():
    return _mission(tree=_tree({"name": "act", "parent": "root_sequence",
                                "action": {"action_type": "dock_robot",
                                           "action_parameters": {}}}))


@pytest.mark.unit
async def test_an_action_node_is_not_completed_by_another_actions_state():
    r, _ = _make_robot()
    m = await _start(r, _action_mission())
    order = _order_id(r)
    stale = _policy_state("m1-rold-n0-s2", types.VDA5050ActionStatus.FINISHED)
    own = types.VDA5050ActionState(actionId=f"{order}-s0-n1", actionType="dock_robot",
                                   actionStatus=types.VDA5050ActionStatus.RUNNING)
    await r._on_client_message(_state(order, actions=[stale, own]))
    assert m.status.node_status["act"].state == State.RUNNING

    own.actionStatus = types.VDA5050ActionStatus.FINISHED
    await r._on_client_message(_state(order, actions=[stale, own]))
    assert m.status.node_status["act"].state == State.COMPLETED
    r._cancel_mission_timeout()


@pytest.mark.unit
async def test_an_action_node_with_no_action_states_does_not_raise():
    r, _ = _make_robot()
    m = await _start(r, _action_mission())
    await r._on_client_message(_state(_order_id(r), actions=[]))
    assert m.status.node_status["act"].state == State.RUNNING
    r._cancel_mission_timeout()


@pytest.mark.unit
def test_a_fatal_error_on_a_node_policy_action_names_its_mission_node():
    r, _ = _make_robot()
    m = _mission()
    r._current_mission = m
    ref = order_ids.node_policy_action_id("m1-n1-s4")
    msg = _state("m1-n1")
    msg.errors = [types.VDA5050Error(
        errorType="actionFailed", errorLevel=types.VDA5050ErrorLevel.FATAL,
        errorDescription="bad nodePolicy",
        errorReferences=[types.VDA5050ErrorReference(referenceKey="actionId",
                                                     referenceValue=ref)])]
    assert r.get_mission_errors(msg)
    assert m.status.node_status["a"].error_msg == "bad nodePolicy"


@pytest.mark.unit
def test_node_ids_read_back_through_a_node_policy_action_id():
    ref = order_ids.node_policy_action_id("m1-rab12v3-n2-s6")
    assert order_ids.node_index(ref) == 2
    assert order_ids.node_of_reference(ref) == "m1-rab12v3-n2-s6"
    assert order_ids.node_index("m1-n2-s0-n2") == 2          # a mission action's id


# ---------------------------------------------------------------------------
# BLOCKED
# ---------------------------------------------------------------------------
@pytest.mark.unit
async def test_a_robot_dropping_its_order_for_a_blocked_node_is_not_resent_the_route():
    r, _ = _make_robot()
    m = await _start(r, _mission(tree=_tree(_route("a", n=3))))
    order = _order_id(r)
    s = _info(_state(order), "canceled")
    s.errors = [_edge_blocked(f"{order}-s4")]
    for _ in range(3):
        await r._on_client_message(s.copy(deep=True))

    assert len(_orders(r)) == 1
    assert m.status.order_rev == 0
    assert m.status.blocked and m.status.blocked_waypoint_index == 1
    assert m.status.state == State.RUNNING
    r._cancel_mission_timeout()


@pytest.mark.unit
async def test_a_dropped_order_while_blocked_waits_for_the_reroute():
    r, _ = _make_robot()
    m = await _start(r, _mission(tree=_tree(_route("a", n=3))))
    order = _order_id(r)
    blocked = _state(order)
    blocked.errors = [_edge_blocked(f"{order}-s4")]
    await r._on_client_message(blocked)
    assert m.status.blocked
    # The robot later drops the order without repeating the error.
    await r._on_client_message(_info(_state(order), "canceled"))
    assert len(_orders(r)) == 1 and m.status.order_rev == 0
    r._cancel_mission_timeout()


@pytest.mark.unit
async def test_a_stale_canceled_while_the_robot_executes_is_ignored():
    r, _ = _make_robot()
    m = await _start(r, _mission())
    order = _order_id(r)
    s = _info(_state(order), "canceled")
    s.nodeStates = [types.VDA5050NodeState(nodeId=f"{order}-s4", sequenceId=4)]
    await r._on_client_message(s)
    await r._on_client_message(s.copy(deep=True))
    assert len(_orders(r)) == 1 and m.status.order_rev == 0
    r._cancel_mission_timeout()


# ---------------------------------------------------------------------------
# OFFLINE
# ---------------------------------------------------------------------------
@pytest.mark.unit
async def test_the_mission_timeout_does_not_run_while_the_robot_is_offline():
    r, _ = _make_robot()
    m = _mission()
    m.timeout = datetime.timedelta(seconds=0.2)
    await _start(r, m)
    order = _order_id(r)
    r._robot_object.heartbeat_timeout = datetime.timedelta(seconds=0.05)
    await r._on_client_message(_state(order))         # online; heartbeat armed
    await asyncio.sleep(0.1)                           # heartbeat lapses: offline
    assert r._robot_object.status.online is False
    assert r._mission_timeout_task is None and r._timeout_paused is not None
    await asyncio.sleep(0.3)                           # longer than the whole timeout
    assert m.status.state == State.RUNNING

    # Back online, it carried on: the progress it made offline arrives in one state.
    await r._on_client_message(_state(order, last_node_id=f"{order}-s4", last_seq=4))
    assert m.status.state == State.COMPLETED
    assert not [a for a in r._current_instant_actions.values()
                if a.actionType == types.VDA5050InstantActionType.CANCEL_ORDER]


@pytest.mark.unit
async def test_the_rest_of_the_timeout_runs_after_the_robot_is_back():
    r, _ = _make_robot()
    m = _mission()
    m.timeout = datetime.timedelta(seconds=0.15)
    await _start(r, m)
    r._pause_mission_timeout()
    r._robot_object.status.online = False
    await asyncio.sleep(0.2)
    assert m.status.state == State.RUNNING
    await r._on_client_message(_state(_order_id(r)))
    assert r._mission_timeout_task is not None
    await asyncio.sleep(0.25)
    assert m.status.state == State.FAILED


@pytest.mark.unit
async def test_pausing_can_be_turned_off(monkeypatch):
    monkeypatch.setattr(order_policy, "_current",
                        order_policy.OrderPolicy(timeout_pause_offline=False))
    r, _ = _make_robot()
    await _start(r, _mission())
    task = r._mission_timeout_task
    r._pause_mission_timeout()
    assert r._mission_timeout_task is task and r._timeout_paused is None
    r._cancel_mission_timeout()


# ---------------------------------------------------------------------------
# DEVIATION
# ---------------------------------------------------------------------------
def _positions(order):
    return [n.nodePosition for n in order.nodes]


@pytest.mark.unit
def test_pass_through_nodes_are_wide_and_the_last_node_is_tight():
    order = types.VDA5050Order.from_route(
        _route_of({"x": 1}, {"x": 2}, {"x": 3}), _robot_obj(), "m1", 1, policy=POLICY)
    xy = [p.allowedDeviationXY for p in _positions(order)]
    theta = [p.allowedDeviationTheta for p in _positions(order)]
    assert xy == [POLICY.deviation_xy_start_m, 0.35, 0.35, 0.1]
    assert theta == [POLICY.deviation_theta_pass_rad, POLICY.deviation_theta_pass_rad,
                     POLICY.deviation_theta_pass_rad, POLICY.deviation_theta_final_rad]


@pytest.mark.unit
def test_a_waypoints_own_tolerance_is_kept_and_zero_means_unset():
    order = types.VDA5050Order.from_route(
        _route_of({"x": 1, "allowedDeviationXY": 0.5}, {"x": 2, "allowedDeviationXY": 0},
                  {"x": 3, "allowedDeviationXY": 0.05, "allowedDeviationTheta": 0.2}),
        _robot_obj(), "m1", 1, policy=POLICY)
    p = _positions(order)
    assert [n.allowedDeviationXY for n in p[1:]] == [0.5, 0.35, 0.05]
    assert p[3].allowedDeviationTheta == 0.2
    strict = order_policy.OrderPolicy(deviation_zero_is_unset=False)
    order = types.VDA5050Order.from_route(_route_of({"x": 1, "allowedDeviationXY": 0}),
                                          _robot_obj(), "m1", 1, policy=strict)
    assert order.nodes[1].nodePosition.allowedDeviationXY == 0


@pytest.mark.unit
def test_a_one_waypoint_route_and_a_resumed_routes_last_node_are_tight():
    order = types.VDA5050Order.from_route(_route_of({"x": 1}), _robot_obj(), "m1", 1,
                                          policy=POLICY)
    assert order.nodes[-1].nodePosition.allowedDeviationXY == 0.1


@pytest.mark.unit
def test_move_and_action_orders_get_the_tight_tolerance_where_position_matters():
    move = types.VDA5050Order.from_move(mission_object.MissionMoveNodeV1(distance=1.0),
                                        _robot_obj(), "m1", 1)
    assert move.nodes[0].nodePosition.allowedDeviationXY == 0.35
    assert move.nodes[1].nodePosition.allowedDeviationXY == 0.1
    action = types.VDA5050Order.from_action(
        mission_object.MissionActionNodeV1(action_type="dock_robot"), _robot_obj(), "m1", 1)
    assert action.nodes[0].nodePosition.allowedDeviationXY == 0.1


@pytest.mark.unit
def test_the_order_policy_reads_the_environment(monkeypatch):
    monkeypatch.setenv("ROUTE_DEVIATION_XY_PASS_M", "0.4")
    monkeypatch.setenv("VDA5050_NODE_POLICY_MODE", "ON")
    monkeypatch.setenv("MISSION_TIMEOUT_PAUSE_OFFLINE", "false")
    p = order_policy.from_env()
    assert p.deviation_xy_pass_m == 0.4 and p.node_policy_mode == "on"
    assert p.timeout_pause_offline is False and p.deviation_xy_final_m == 0.1


@pytest.mark.unit
def test_a_planner_waypoint_names_its_graph_node_and_sets_no_tolerance():
    pose = common.Pose2D(x=1.0, y=2.0, map_id="M", node_id="17")
    assert pose.allowedDeviationXY is None and pose.node_id == "17"


# ---------------------------------------------------------------------------
# NODE POLICY
# ---------------------------------------------------------------------------
def _policy_order(n=3):
    route = _route_of(*({"x": float(i)} for i in range(n)))
    return types.VDA5050Order.from_route(route, _robot_obj(), "m1-rab12", 1,
                                         node_policy=True, policy=POLICY)


@pytest.mark.unit
def test_node_policy_goes_on_pass_through_nodes_only():
    order = _policy_order(3)
    carrying = [n.sequenceId for n in order.nodes if n.actions]
    assert carrying == [2, 4]                         # not the start (0), not the last (6)
    for node in order.nodes[1:3]:
        (action,) = node.actions
        assert action.actionType == "nodePolicy"
        assert action.blockingType == types.VDA5050ActionBlockingType.NONE
        assert action.actionId == f"{node.nodeId}-policy"


@pytest.mark.unit
def test_node_policy_values_are_typed_on_the_wire():
    wire = json.loads(_policy_order(2).json())
    params = {p["key"]: p["value"] for p in wire["nodes"][1]["actions"][0]["actionParameters"]}
    assert params == {"skippable": False, "maxWaitS": 10.0}
    with_corridor = types.VDA5050Action.node_policy("n-s2", 5, skippable=True,
                                                    corridor_width_m=1.0)
    assert json.loads(with_corridor.json())["actionParameters"][2] == \
        {"key": "corridorWidth", "value": 1.0}


@pytest.mark.unit
def test_mission_action_parameters_are_still_sent_as_strings():
    action = types.VDA5050Action.from_mission_action(
        mission_object.MissionActionNodeV1(action_type="dock_robot",
                                           action_parameters={"dock": 3, "x": "a"}),
        "m1-n1-s0", 1)
    assert {p.key: p.value for p in action.actionParameters} == {"dock": "3", "x": "a"}


@pytest.mark.unit
def test_no_node_policy_without_the_flag():
    order = types.VDA5050Order.from_route(_route_of({"x": 1}, {"x": 2}), _robot_obj(),
                                          "m1", 1, policy=POLICY)
    assert not any(n.actions for n in order.nodes)


def _factsheet_robot(r, actions):
    r._robot_object.status.factsheet.custom_actions = [
        robot_object.CustomActionV1(action_type=a) for a in actions]


@pytest.mark.unit
@pytest.mark.parametrize("mode, advertised, expected", [
    ("factsheet", ["nodePolicy"], True),
    ("factsheet", ["dock_robot"], False),
    ("on", [], True),
    ("off", ["nodePolicy"], False),
])
async def test_node_policy_is_sent_per_mode_and_factsheet(monkeypatch, mode, advertised,
                                                          expected):
    monkeypatch.setattr(order_policy, "_current",
                        order_policy.OrderPolicy(node_policy_mode=mode))
    r, _ = _make_robot()
    _factsheet_robot(r, advertised)
    await _start(r, _mission(tree=_tree(_route("a", n=3))))
    (order,) = _orders(r)
    assert any(n["actions"] for n in order["nodes"]) is expected
    r._cancel_mission_timeout()


@pytest.mark.unit
async def test_a_resend_carries_the_same_node_policy_ids(monkeypatch):
    monkeypatch.setattr(order_policy, "_current",
                        order_policy.OrderPolicy(node_policy_mode="on"))
    r, _ = _make_robot()
    await _start(r, _mission(tree=_tree(_route("a", n=3))))
    r._order_sent_at -= 100
    await r._on_client_message(_state("elsewhere-n0"))
    first, again = _orders(r)
    assert first["nodes"] == again["nodes"]
    r._cancel_mission_timeout()


# ---------------------------------------------------------------------------
# FRAME (characterization of what goes out; the fix waits for the robot team)
# ---------------------------------------------------------------------------
import math  # noqa: E402
from unittest.mock import AsyncMock, MagicMock  # noqa: E402

from packages.utils import map_geo  # noqa: E402

T = {"tx": 10.0, "ty": 0.0, "yaw": math.pi / 2}


def _session(transform=T):
    return {"session_id": "s1", "map_name": "M", "purpose": "operate", "aligned": True,
            "map_t_session": dict(transform), "datum": None}


def _map_route(name="a", points=((10.0, 1.0), (10.0, 2.0), (10.0, 3.0))):
    return {"name": name, "parent": "root_sequence", "route": {"waypoints": [
        {"x": x, "y": y, "theta": 0.0, "map_id": "M", "node_id": f"g{i}"}
        for i, (x, y) in enumerate(points)]}}


async def _start_on_map(transform=T, tree=None):
    r, db = _make_robot()
    r._read_open_session = AsyncMock(return_value=_session(transform))
    m = await _start(r, _mission(tree=tree or _tree(_map_route())))
    return r, db, m


@pytest.mark.unit
async def test_each_order_records_the_frame_it_was_sent_in():
    r, _, m = await _start_on_map()
    frame = m.status.sent_order.frame
    assert frame["applied"] == "inverse" and frame["map_name"] == "M"
    assert frame["session_id"] == "s1" and frame["map_t_session"] == T

    (order,) = _orders(r)
    # Session-frame positions, labelled with the map's id; the start node has none.
    x, y = map_geo.apply_transform(map_geo.invert_transform(T), 10.0, 1.0)
    assert order["nodes"][1]["nodePosition"]["x"] == pytest.approx(x)
    assert order["nodes"][1]["nodePosition"]["y"] == pytest.approx(y)
    assert [n["nodePosition"]["mapId"] for n in order["nodes"]] == ["", "M", "M", "M"]
    # A robot that took mapId at its word and applied map_T_session once more would
    # drive somewhere else by a constant offset: the double transform.
    gx, gy = map_geo.apply_transform(T, *map_geo.apply_transform(T, x, y))
    assert math.hypot(gx - 10.0, gy - 1.0) > 1.0
    r._cancel_mission_timeout()


@pytest.mark.unit
async def test_an_identity_session_and_a_mapless_route_are_recorded_too():
    r, _, m = await _start_on_map(transform={"tx": 0.0, "ty": 0.0, "yaw": 0.0})
    assert m.status.sent_order.frame["applied"] == "identity"
    r._cancel_mission_timeout()
    r, _ = _make_robot()
    m = await _start(r, _mission())
    assert m.status.sent_order.frame == {"applied": "none", "map_id_sent": ""}
    r._cancel_mission_timeout()


# ---------------------------------------------------------------------------
# NODE REPORTS
# ---------------------------------------------------------------------------
def _skipped(node_id, level=types.VDA5050ErrorLevel.WARNING):
    return types.VDA5050Error(
        errorType="nodeSkipped", errorLevel=level, errorDescription="node blocked; skipped",
        errorReferences=[types.VDA5050ErrorReference(referenceKey="nodeId",
                                                     referenceValue=node_id)])


def _note(node_id, info_type="nodeOffset", **refs):
    references = [types.VDA5050InfoReference(referenceKey="nodeId", referenceValue=node_id)]
    references += [types.VDA5050InfoReference(referenceKey=k, referenceValue=str(v))
                   for k, v in refs.items()]
    return types.VDA5050Info(infoType=info_type, infoReferences=references,
                             infoDescription=f"{info_type} note", infoLevel="INFO")


@pytest.mark.unit
async def test_a_skipped_node_is_kept_once_and_changes_nothing_else():
    r, _, m = await _start_on_map()
    order = _order_id(r)
    s = _state(order, last_node_id=f"{order}-s2", last_seq=2)
    s.errors = [_skipped(f"{order}-s4")]
    for _ in range(3):
        await r._on_client_message(s.copy(deep=True))

    (skipped,) = m.status.skipped_nodes
    assert (skipped.node_id, skipped.mission_node, skipped.waypoint_index,
            skipped.graph_node_id) == (f"{order}-s4", "a", 1, "g1")
    assert m.status.state == State.RUNNING and not m.status.blocked
    assert len(_orders(r)) == 1
    assert r._robot_server.fleet_recorder.node_skipped.call_count == 1
    r._cancel_mission_timeout()


@pytest.mark.unit
async def test_a_skip_reported_fatal_does_not_fail_the_mission():
    r, _, m = await _start_on_map()
    order = _order_id(r)
    s = _state(order)
    s.errors = [_skipped(f"{order}-s4", types.VDA5050ErrorLevel.FATAL)]
    await r._on_client_message(s)
    assert m.status.state == State.RUNNING and len(m.status.skipped_nodes) == 1
    r._cancel_mission_timeout()


@pytest.mark.unit
async def test_reports_on_another_runs_nodes_are_ignored():
    r, _, m = await _start_on_map()
    s = _state(_order_id(r))
    s.errors = [_skipped("m1-rzzzz9999-n1-s4")]
    s.information = [_note("other-n1-s2", offsetX=0.1, offsetY=0.0)]
    await r._on_client_message(s)
    assert m.status.skipped_nodes == [] and m.status.node_notes == []
    r._cancel_mission_timeout()


@pytest.mark.unit
async def test_a_node_offset_is_kept_in_the_order_frame_and_the_map_frame():
    r, _, m = await _start_on_map()
    order = _order_id(r)
    s = _state(order)
    s.information = [_note(f"{order}-s2", offsetX=0.2, offsetY=0.0)]
    await r._on_client_message(s)
    await r._on_client_message(s.copy(deep=True))       # repeated: same note, seen again

    (note,) = m.status.node_notes
    assert note.offset == {"dx": 0.2, "dy": 0.0, "dtheta": 0.0}
    # The order was sent in a session frame turned 90 degrees against the map.
    assert note.offset_map["dx"] == pytest.approx(0.0, abs=1e-9)
    assert note.offset_map["dy"] == pytest.approx(0.2)
    assert (note.waypoint_index, note.graph_node_id) == (0, "g0")
    assert m.status.state == State.RUNNING and len(_orders(r)) == 1
    r._cancel_mission_timeout()


@pytest.mark.unit
async def test_offsets_that_all_point_one_way_are_flagged_once_as_a_frame_error():
    points = tuple((10.0, float(i)) for i in range(7))
    r, _, m = await _start_on_map(transform={"tx": 0.0, "ty": 0.0, "yaw": 0.0},
                                  tree=_tree(_map_route(points=points)))
    order = _order_id(r)
    recorder = r._robot_server.fleet_recorder
    for seq in (2, 4, 6, 8):
        s = _state(order)
        s.information = [_note(f"{order}-s{seq}", offsetX=0.3, offsetY=0.02)]
        await r._on_client_message(s)
    assert not m.status.offset_summary.suspected_frame_error
    for seq in (10, 12):
        s = _state(order)
        s.information = [_note(f"{order}-s{seq}", offsetX=0.31, offsetY=0.0)]
        await r._on_client_message(s)

    summary = m.status.offset_summary
    assert summary.n == 6 and summary.suspected_frame_error
    assert summary.consistency > 0.99 and summary.mean_dx == pytest.approx(0.303, abs=1e-3)
    assert recorder.frame_offset_suspected.call_count == 1
    r._cancel_mission_timeout()


@pytest.mark.unit
async def test_scattered_offsets_are_not_a_frame_error():
    points = tuple((10.0, float(i)) for i in range(7))
    r, _, m = await _start_on_map(transform={"tx": 0.0, "ty": 0.0, "yaw": 0.0},
                                  tree=_tree(_map_route(points=points)))
    order = _order_id(r)
    for seq, (dx, dy) in zip((2, 4, 6, 8, 10, 12),
                             ((0.3, 0), (-0.3, 0), (0, 0.3), (0, -0.3), (0.3, 0), (-0.3, 0))):
        s = _state(order)
        s.information = [_note(f"{order}-s{seq}", offsetX=dx, offsetY=dy)]
        await r._on_client_message(s)
    assert not m.status.offset_summary.suspected_frame_error
    r._cancel_mission_timeout()


@pytest.mark.unit
async def test_node_notes_are_capped_newest_first(monkeypatch):
    monkeypatch.setattr(server_module, "MISSION_NODE_NOTES_MAX", 3)
    r, _, m = await _start_on_map()
    order = _order_id(r)
    for info_type in ("a", "b", "c", "d"):
        s = _state(order)
        s.information = [_note(f"{order}-s2", info_type=info_type)]
        await r._on_client_message(s)
    assert [n.info_type for n in m.status.node_notes] == ["d", "c", "b"]
    r._cancel_mission_timeout()


@pytest.mark.unit
async def test_a_new_pass_starts_without_the_last_passes_reports():
    r, _, m = await _start_on_map()
    m.repeat = 2
    order = _order_id(r)
    s = _state(order)
    s.errors = [_skipped(f"{order}-s4")]
    s.information = [_note(f"{order}-s2", offsetX=0.1, offsetY=0.0)]
    await r._on_client_message(s)
    assert m.status.skipped_nodes and m.status.node_notes
    await r._on_client_message(_state(order, last_node_id=f"{order}-s6", last_seq=6))
    assert m.status.passes_completed == 1
    assert m.status.skipped_nodes == [] and m.status.node_notes == []
    r._cancel_mission_timeout()


# ---------------------------------------------------------------------------
# RECONNECT: progress made offline arrives at once
# ---------------------------------------------------------------------------
@pytest.mark.unit
async def test_progress_that_jumps_several_nodes_is_taken_in_one_step():
    r, _ = _make_robot()
    m = await _start(r, _mission(tree=_tree(_route("a", n=5))))
    order = _order_id(r)
    await r._on_client_message(_state(order, last_node_id=f"{order}-s2", last_seq=2))
    assert m.status.task_status["a"] == 0
    s = _state(order, last_node_id=f"{order}-s8", last_seq=8)
    s.errors = [_skipped(f"{order}-s6")]
    await r._on_client_message(s)
    assert m.status.task_status["a"] == 3 and len(m.status.skipped_nodes) == 1
    assert m.status.state == State.RUNNING
    await r._on_client_message(_state(order, last_node_id=f"{order}-s10", last_seq=10))
    assert m.status.state == State.COMPLETED


@pytest.mark.unit
async def test_a_jump_on_a_resumed_order_counts_from_its_offset():
    r, _ = _make_robot()
    m = await _start(r, _mission(tree=_tree(_route("a", n=6))))
    m.status.sent_order.waypoint_offset = 2        # as an order sent after waypoint 1
    order = _order_id(r)
    await r._on_client_message(_state(order, last_node_id=f"{order}-s6", last_seq=6))
    assert m.status.task_status["a"] == 4
    r._cancel_mission_timeout()


# ---------------------------------------------------------------------------
# AGENT TRIGGERS
# ---------------------------------------------------------------------------
@pytest.mark.unit
def test_the_agent_sees_a_skip_as_info_and_ignores_failed_node_policies():
    from packages.services.agent_orchestrator.triggers import detect_events
    curr = {"errors": [{"errorType": "nodeSkipped", "errorLevel": "WARNING",
                        "errorDescription": "skipped",
                        "errorReferences": [{"referenceKey": "nodeId",
                                             "referenceValue": "m1-n1-s4"}]}],
            "actionStates": [{"actionId": "m1-n1-s2-policy", "actionType": "nodePolicy",
                              "actionStatus": "FAILED"},
                             {"actionId": "dock", "actionType": "dock_robot",
                              "actionStatus": "FAILED"}]}
    events = detect_events({}, curr)
    kinds = {(e.type, e.severity) for e in events}
    assert ("node_skipped", "info") in kinds
    assert [e.detail for e in events if e.type == "action_failed"] == ["Action 'dock' FAILED"]


# ---------------------------------------------------------------------------
# REROUTE HYGIENE: cancel dwell, resend cap
# ---------------------------------------------------------------------------
from types import SimpleNamespace  # noqa: E402
import time as _time  # noqa: E402


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def monotonic(self):
        return self.t


@pytest.fixture
def clock(monkeypatch):
    c = _Clock()
    monkeypatch.setattr(server_module, "time",
                        SimpleNamespace(monotonic=c.monotonic, time=_time.time))
    return c


def _cancel_actions(r):
    return [a for a in r._current_instant_actions.values()
            if a.actionType == types.VDA5050InstantActionType.CANCEL_ORDER]


@pytest.mark.unit
async def test_a_reroute_does_not_cancel_an_order_version_the_robot_has_not_seen(clock):
    r, _ = _make_robot()
    await _start(r, _mission())
    order = _order_id(r)
    await r._on_mission_change(_rerouted())
    assert not _cancel_actions(r)                       # held back
    # The robot reports the order: now it is cancelled, once.
    await r._on_client_message(_state(order))
    assert len(_cancel_actions(r)) == 1
    await r._on_client_message(_state(order))
    assert len(_cancel_actions(r)) == 1
    r._cancel_mission_timeout()


@pytest.mark.unit
async def test_a_held_reroute_cancel_goes_out_after_the_dwell(clock):
    r, _ = _make_robot()
    await _start(r, _mission())
    await r._on_mission_change(_rerouted())
    await r._on_client_message(_state("elsewhere-n0"))
    assert not _cancel_actions(r)
    clock.t += POLICY.cancel_min_dwell_s
    await r._on_client_message(_state("elsewhere-n0"))
    assert len(_cancel_actions(r)) == 1
    r._cancel_mission_timeout()


@pytest.mark.unit
async def test_a_mission_cancel_is_never_held(clock):
    r, _ = _make_robot()
    m = await _start(r, _mission())
    cancelled = _mission()
    cancelled.needs_canceled = True
    await r._on_mission_change(cancelled)
    assert m.needs_canceled and len(_cancel_actions(r)) == 1
    r._cancel_mission_timeout()


@pytest.mark.unit
async def test_an_order_the_robot_does_not_take_is_resent_at_most_three_times(clock):
    r, _ = _make_robot()
    await _start(r, _mission())
    for _ in range(30):
        clock.t += r.ORDER_RESEND_MAX_S
        await r._on_client_message(_state("elsewhere-n0"))
    assert len(_orders(r)) == 1 + r.ORDER_MAX_RESENDS
    r._cancel_mission_timeout()


# ---------------------------------------------------------------------------
# BLOCKED-NODE EXCLUSION
# ---------------------------------------------------------------------------
from packages.utils import blocked_nodes  # noqa: E402


class _Cursor:
    def __init__(self, store):
        self.store = store
        self.rows = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, sql, params):
        self.store.append((sql, params))

    async def fetchall(self):
        return self.rows

    async def fetchone(self):
        return self.rows[0] if self.rows else None


class _Conn:
    def __init__(self, store, rows=None):
        self.store, self.rows = store, rows or []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def cursor(self):
        c = _Cursor(self.store)
        c.rows = self.rows
        return c


@pytest.mark.unit
async def test_an_edge_blocked_report_keeps_its_graph_node_out_of_new_routes():
    r, db, m = await _start_on_map()
    writes = []
    db.connection = MagicMock(side_effect=lambda: _Conn(writes))
    order = _order_id(r)
    s = _state(order)
    s.errors = [_edge_blocked(f"{order}-s4")]
    await r._on_client_message(s)
    await r._on_client_message(s.copy(deep=True))      # repeated: written once
    await asyncio.sleep(0)

    upserts = [p for sql, p in writes if sql == blocked_nodes.UPSERT_SQL]
    assert len(upserts) == 1
    (map_name, graph_node, edge_from, edge_to, source, robot, mission, vda, _reason,
     x, y, expires_in) = upserts[0]
    assert (map_name, graph_node, edge_from, edge_to, source) == ("M", "g1", "g0", "g1",
                                                                  "edgeBlocked")
    assert (robot, mission, vda, x, y) == ("r1", "m1", f"{order}-s4", 10.0, 2.0)
    assert expires_in == POLICY.blocked_node_exclusion_min * 60
    r._cancel_mission_timeout()


@pytest.mark.unit
async def test_a_blocked_waypoint_without_a_graph_node_is_kept_out_by_position():
    route = {"name": "a", "parent": "root_sequence", "route": {"waypoints": [
        {"x": 1.0, "y": 1.0, "map_id": "M"}, {"x": 2.0, "y": 1.0, "map_id": "M"}]}}
    r, db, m = await _start_on_map(transform={"tx": 0.0, "ty": 0.0, "yaw": 0.0},
                                   tree=_tree(route))
    writes = []
    db.connection = MagicMock(side_effect=lambda: _Conn(writes))
    order = _order_id(r)
    s = _state(order)
    s.errors = [_edge_blocked(f"{order}-s4")]
    await r._on_client_message(s)
    await asyncio.sleep(0)
    (params,) = [p for sql, p in writes if sql == blocked_nodes.UPSERT_SQL]
    assert params[1] == "@2.00,1.00"
    r._cancel_mission_timeout()


@pytest.mark.unit
async def test_a_failed_exclusion_write_does_not_disturb_the_mission():
    r, db, m = await _start_on_map()
    db.connection = MagicMock(side_effect=RuntimeError("db down"))
    order = _order_id(r)
    s = _state(order)
    s.errors = [_edge_blocked(f"{order}-s4")]
    await r._on_client_message(s)
    await asyncio.sleep(0)
    assert m.status.blocked and m.status.state == State.RUNNING
    r._cancel_mission_timeout()


@pytest.mark.unit
def test_the_path_search_avoids_excluded_nodes_and_reports_none_when_it_cannot():
    edges = [{"from": a, "to": b} for a, b in
             (("1", "2"), ("2", "3"), ("1", "4"), ("4", "5"), ("5", "3"), ("3", "6"))]
    assert blocked_nodes.shortest_path_avoiding(edges, "1", "6", set()) == \
        ["1", "2", "3", "6"]
    assert blocked_nodes.shortest_path_avoiding(edges, "1", "6", {"2"}) == \
        ["1", "4", "5", "3", "6"]
    assert blocked_nodes.shortest_path_avoiding(edges, "1", "6", {"3"}) is None
    assert blocked_nodes.shortest_path_avoiding(edges, "1", "6", {"6"}) is None
    # The start is where the robot is: never excluded.
    assert blocked_nodes.shortest_path_avoiding(edges, "1", "3", {"1"}) == ["1", "2", "3"]


@pytest.mark.unit
def test_position_rows_exclude_the_graph_nodes_near_them():
    rows = [{"graph_node_id": "7"}, {"graph_node_id": blocked_nodes.synthetic_id(5.0, 5.0)}]
    nodes = [{"node_id": "a", "pose": {"x": 5.2, "y": 5.1}}, {"node_id": "b", "x": 9, "y": 9}]
    assert blocked_nodes.excluded_node_ids(rows, nodes, 0.5) == {"7", "a"}
    assert blocked_nodes.position_of("@1.50,-2.00") == (1.5, -2.0)
    assert blocked_nodes.position_of("17") is None
    with pytest.raises(ValueError):
        blocked_nodes.upsert_params("M", "1", "guess", 60)


def _planner(blocked_rows, edges, aql_path=None):
    from packages.services.mission_planner.server import MissionPlannerService
    svc = MissionPlannerService.__new__(MissionPlannerService)
    svc.logger = MagicMock()
    svc.graph_db = MagicMock()
    svc.graph_db.get_edges.return_value = edges
    svc.graph_db.get_all_nodes.return_value = []
    svc.graph_db.shortest_path.return_value = aql_path
    svc.database = MagicMock()
    svc.database.connection = MagicMock(side_effect=lambda: _Conn([], blocked_rows))
    return svc


_EDGES = [{"from": a, "to": b} for a, b in (("1", "2"), ("2", "3"), ("1", "4"), ("4", "3"))]


def _row(node, expires="2026-10-08T12:10:00+00:00"):
    return (("M", node, None, None, "edgeBlocked", "r1", "m1", "m1-n1-s4", "blocked", 1.0,
             1.0, None, expires))


@pytest.mark.unit
async def test_the_planner_routes_around_a_blocked_node():
    svc = _planner([_row("2")], _EDGES, aql_path=["1", "2", "3"])
    rows = await svc._active_blocked_nodes("M")
    assert rows[0]["graph_node_id"] == "2"
    assert svc._path_avoiding("1", "3", "M", rows) == (["1", "4", "3"], None, [])


@pytest.mark.unit
async def test_the_planner_refuses_when_every_route_goes_through_a_blocked_node():
    svc = _planner([], _EDGES, aql_path=["1", "2", "3"])
    path, error, rows = svc._path_avoiding("1", "3", "M", [blocked_nodes.row_dict(_row("2")),
                                                           blocked_nodes.row_dict(_row("4"))])
    # Only the row on the shortest path is named: it is the one the detour failed around.
    assert path is None and "No route avoids" in error and "node(s) 2 " in error
    assert [r["graph_node_id"] for r in rows] == ["2"]
    path, error, rows = svc._path_avoiding("1", "3", "M", [blocked_nodes.row_dict(_row("3"))])
    assert path is None and "goal node 3" in error and rows[0]["graph_node_id"] == "3"


@pytest.mark.unit
async def test_without_listable_edges_the_graphs_path_is_used_only_if_it_avoids_them():
    rows = [blocked_nodes.row_dict(_row("2"))]
    assert _planner([], [], aql_path=["1", "4", "3"])._path_avoiding("1", "3", "M", rows) \
        == (["1", "4", "3"], None, [])
    path, _, _ = _planner([], [], aql_path=["1", "2", "3"])._path_avoiding("1", "3", "M",
                                                                           rows)
    assert path is None


@pytest.mark.unit
async def test_an_unreadable_table_plans_without_exclusions():
    svc = _planner([], _EDGES)
    svc.database.connection = MagicMock(side_effect=RuntimeError("no table"))
    assert await svc._active_blocked_nodes("M") == []


# ---------------------------------------------------------------------------
# API: reroute through a blocked node, list and clear
# ---------------------------------------------------------------------------
@pytest.mark.unit
async def test_the_api_finds_a_reroute_through_a_blocked_node(monkeypatch):
    import packages.api.main as api_main
    service = MagicMock()
    service.database.connection = MagicMock(
        side_effect=lambda: _Conn([], [_row("17")[:9] + (5.0, 5.0) + _row("17")[11:]]))
    monkeypatch.setattr(api_main, "service", service)
    start = {"x": 9.0, "y": 9.0, "map_id": "M", "node_id": "1"}
    by_id = {"a": {"waypoints": [start, {"x": 0.0, "y": 0.0, "map_id": "M", "node_id": "17"}]}}
    by_position = {"a": {"waypoints": [start, {"x": 5.1, "y": 5.0, "map_id": "M"}]}}
    clear = {"a": {"waypoints": [{"x": 0.0, "y": 0.0, "map_id": "M", "node_id": "3"},
                                 {"x": 1.0, "y": 0.0, "map_id": ""}]}}
    assert [r["graph_node_id"] for r in await api_main._reroute_through_blocked(by_id)] == \
        ["17"]
    assert len(await api_main._reroute_through_blocked(by_position)) == 1
    assert await api_main._reroute_through_blocked(clear) == []


@pytest.mark.unit
async def test_the_api_clears_a_blocked_node_or_says_it_was_not_blocked(monkeypatch):
    from fastapi import HTTPException
    import packages.api.main as api_main
    service = MagicMock()
    writes = []
    service.database.connection = MagicMock(side_effect=lambda: _Conn(writes, [(True,)]))
    monkeypatch.setattr(api_main, "service", service)
    assert (await api_main.clear_blocked_node("M", "17"))["cleared"] is True
    assert writes[-1] == (blocked_nodes.DELETE_SQL, ("M", "17"))
    service.database.connection = MagicMock(side_effect=lambda: _Conn(writes, []))
    with pytest.raises(HTTPException) as err:
        await api_main.clear_blocked_node("M", "18")
    assert err.value.status_code == 404
