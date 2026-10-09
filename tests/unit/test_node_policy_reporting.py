"""The robot reports nodePolicy actions as it likes (robot team, 2026-10-09): every one
listed in actionStates, FINISHED on accepting the order, or FAILED "Action handler not
found" before their release, or FAILED when an order is cancelled. The dispatcher must not
wait on, complete or fail anything because of them.
"""
import pytest

import packages.controllers.mission.vda5050_types as types
from packages.controllers.mission import order_ids, order_policy
from packages.services.agent_orchestrator.triggers import _failed_action_ids, detect_events
from tests.unit.test_mission_lifecycle_fixes import (
    State, _cancel_done, _make_robot, _mission, _order_id, _orders, _rerouted, _route,
    _start, _state, _tree)

Status = types.VDA5050ActionStatus
NOT_FOUND = "Action handler not found"


def _policies(order, status, seqs=(2, 4, 6, 8), description=None):
    return [types.VDA5050ActionState(
        actionId=order_ids.node_policy_action_id(f"{order}-s{s}"),
        actionType=types.NODE_POLICY_ACTION_TYPE, actionStatus=status,
        resultDescription=description or "") for s in seqs]


def _action_mission():
    return _mission(tree=_tree({"name": "act", "parent": "root_sequence",
                                "action": {"action_type": "dock_robot",
                                           "action_parameters": {}}}))


def _own(order, status):
    return types.VDA5050ActionState(actionId=f"{order}-s0-n1", actionType="dock_robot",
                                    actionStatus=status)


@pytest.fixture(autouse=True)
def _policy_on(monkeypatch):
    monkeypatch.setattr(order_policy, "_current",
                        order_policy.OrderPolicy(node_policy_mode="on"))


# ---------------------------------------------------------------------------
# Route missions
# ---------------------------------------------------------------------------
@pytest.mark.unit
@pytest.mark.parametrize("status, description", [
    (Status.FINISHED, None),
    (Status.FAILED, NOT_FOUND),
])
async def test_a_route_completes_whatever_the_node_policies_report(status, description):
    r, _ = _make_robot()
    m = await _start(r, _mission(tree=_tree(_route("a", n=5))))
    order = _order_id(r)
    assert any(n["actions"] for n in _orders(r)[0]["nodes"])
    policies = _policies(order, status, description=description)

    await r._on_client_message(_state(order, actions=policies))
    assert m.status.state == State.RUNNING
    await r._on_client_message(_state(order, actions=policies, last_node_id=f"{order}-s4",
                                      last_seq=4))
    assert m.status.state == State.RUNNING and m.status.task_status["a"] == 1
    await r._on_client_message(_state(order, actions=policies[:2], last_node_id=f"{order}-s10",
                                      last_seq=10))
    assert m.status.state == State.COMPLETED
    assert m.status.node_status["a"].state == State.COMPLETED


@pytest.mark.unit
async def test_a_route_completes_with_empty_node_and_edge_states_and_mixed_policies():
    r, _ = _make_robot()
    m = await _start(r, _mission(tree=_tree(_route("a", n=3))))
    order = _order_id(r)
    mixed = (_policies(order, Status.FAILED, (2,), NOT_FOUND)
             + _policies(order, Status.FINISHED, (4,))
             + _policies(order, Status.WAITING, (6,)))
    await r._on_client_message(_state(order, actions=mixed, last_node_id=f"{order}-s6",
                                      last_seq=6))
    assert m.status.state == State.COMPLETED


@pytest.mark.unit
async def test_failed_node_policies_do_not_fail_a_running_route():
    r, _ = _make_robot()
    m = await _start(r, _mission(tree=_tree(_route("a", n=4))))
    order = _order_id(r)
    failed = _policies(order, Status.FAILED, description=NOT_FOUND)
    for seq in (0, 2, 4):
        await r._on_client_message(_state(order, actions=failed, last_node_id=f"{order}-s{seq}",
                                          last_seq=seq))
        assert m.status.state == State.RUNNING
        assert m.status.node_status["a"].state != State.FAILED
    r._cancel_mission_timeout()


# ---------------------------------------------------------------------------
# Action-order missions
# ---------------------------------------------------------------------------
@pytest.mark.unit
@pytest.mark.parametrize("position", ["first", "last"])
@pytest.mark.parametrize("policy_status, description", [
    (Status.FAILED, NOT_FOUND), (Status.FINISHED, None)])
async def test_an_action_node_follows_only_its_own_state(position, policy_status,
                                                          description):
    r, _ = _make_robot()
    m = await _start(r, _action_mission())
    order = _order_id(r)
    stale = _policies(order, policy_status, seqs=(2, 4, 6), description=description)

    def listing(own):
        return [*stale, own] if position == "first" else [own, *stale]

    await r._on_client_message(_state(order, actions=listing(_own(order, Status.RUNNING))))
    assert m.status.node_status["act"].state == State.RUNNING
    await r._on_client_message(_state(order, actions=listing(_own(order, Status.FINISHED))))
    assert m.status.node_status["act"].state == State.COMPLETED
    r._cancel_mission_timeout()


@pytest.mark.unit
async def test_an_action_node_fails_only_by_its_own_failure():
    r, _ = _make_robot()
    m = await _start(r, _action_mission())
    order = _order_id(r)
    stale = _policies(order, Status.FAILED, description=NOT_FOUND)
    await r._on_client_message(_state(order, actions=[*stale]))
    assert m.status.node_status["act"].state == State.RUNNING
    await r._on_client_message(_state(order, actions=[*stale, _own(order, Status.FAILED)]))
    assert m.status.node_status["act"].state == State.FAILED
    r._cancel_mission_timeout()


@pytest.mark.unit
async def test_a_foreign_node_policy_is_not_taken_for_the_action_nodes_state():
    r, _ = _make_robot()
    m = await _start(r, _action_mission())
    order = _order_id(r)
    foreign = types.VDA5050ActionState(
        actionId=order_ids.node_policy_action_id("m1-rold-n0-s2"),
        actionType=types.NODE_POLICY_ACTION_TYPE, actionStatus=Status.FAILED,
        resultDescription=NOT_FOUND)
    await r._on_client_message(_state(order, actions=[foreign]))
    assert m.status.node_status["act"].state == State.RUNNING
    r._cancel_mission_timeout()


# ---------------------------------------------------------------------------
# cancelOrder ack behind many FAILED nodePolicy entries
# ---------------------------------------------------------------------------
@pytest.mark.unit
@pytest.mark.parametrize("before", [True, False])
async def test_a_cancel_ack_is_seen_among_many_failed_node_policies(before):
    r, _ = _make_robot()
    await _start(r, _mission())
    order = _order_id(r)
    await r._on_client_message(_state(order))
    await r._on_mission_change(_rerouted())
    failed = _policies(order, Status.FAILED, seqs=range(2, 2 + 2 * 25, 2),
                       description="order canceled")
    assert len(failed) >= 20
    ack = _cancel_done(r)
    actions = failed + [ack] if before else [ack] + failed
    await r._on_client_message(_state(order, actions=actions))

    assert not r._has_outstanding_cancel()
    assert len(_orders(r)) == 2                      # the rerouted order went out
    r._cancel_mission_timeout()


@pytest.mark.unit
async def test_failed_node_policies_alone_do_not_resolve_a_cancel():
    r, _ = _make_robot()
    await _start(r, _mission())
    order = _order_id(r)
    await r._on_client_message(_state(order))
    await r._on_mission_change(_rerouted())
    await r._on_client_message(_state(order, actions=_policies(order, Status.FAILED)))
    assert r._has_outstanding_cancel()
    r._cancel_mission_timeout()


# ---------------------------------------------------------------------------
# Agent triggers
# ---------------------------------------------------------------------------
@pytest.mark.unit
def test_failed_action_ids_ignores_node_policies_but_keeps_real_failures():
    state = {"actionStates": [
        {"actionId": "m1-n1-s2-policy", "actionType": "nodePolicy", "actionStatus": "FAILED"},
        {"actionId": "dock", "actionType": "dock_robot", "actionStatus": "FAILED"},
        {"actionId": "m1-n1-s4-policy", "actionType": "nodePolicy", "actionStatus": "FAILED"},
        {"actionId": "ok", "actionType": "dock_robot", "actionStatus": "FINISHED"}]}
    assert _failed_action_ids(state) == {"dock"}
    assert _failed_action_ids({"actionStates": state["actionStates"][::2][:1]}) == set()
    assert _failed_action_ids({}) == set()


@pytest.mark.unit
def test_detect_events_raises_no_action_failed_for_node_policies_only():
    curr = {"actionStates": [{"actionId": "p", "actionType": "nodePolicy",
                              "actionStatus": "FAILED",
                              "resultDescription": NOT_FOUND}]}
    assert not [e for e in detect_events({}, curr) if e.type == "action_failed"]
