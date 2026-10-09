"""Order resends near the end of the order / of a dead orderId, and nodeSkipped robustness."""
import pytest

import packages.controllers.mission.server as server_module
import packages.controllers.mission.vda5050_types as types
from packages.controllers.mission import order_ids
from tests.unit.test_dispatch_reconnect_resends import _Clock  # noqa: F401
from tests.unit.test_dispatch_reconnect_resends import clock  # noqa: F401
from tests.unit.test_mission_lifecycle_fixes import (
    State, _make_robot, _mission, _order_id, _orders, _route, _start, _state, _tree)

Status = types.VDA5050ActionStatus


def _skipped(*nodes):
    return [types.VDA5050Error(
        errorType="nodeSkipped", errorLevel=types.VDA5050ErrorLevel.WARNING,
        errorDescription=f"skipped {n}",
        errorReferences=[types.VDA5050ErrorReference(referenceKey="nodeId",
                                                     referenceValue=n)]) for n in nodes]


@pytest.mark.unit
def test_two_skipped_nodes_do_not_collide_in_the_status_errors():
    d = server_module.vda5050_errors_to_status_dict(_skipped("o-n1-s4", "o-n1-s6"))
    assert len(d) == 2 and set(d.values()) == {"skipped o-n1-s4", "skipped o-n1-s6"}


@pytest.mark.unit
async def test_no_resend_when_the_robot_is_at_the_second_to_last_node(clock):  # noqa: F811
    r, _ = _make_robot()
    await _start(r, _mission(tree=_tree(_route("a", n=5))))
    order = _order_id(r)
    sent = len(_orders(r))
    clock.t += r.ORDER_RESEND_MAX_S
    await r._on_client_message(_state("elsewhere-n0", last_node_id=f"{order}-s8", last_seq=8))
    assert len(_orders(r)) == sent
    clock.t += r.ORDER_RESEND_MAX_S
    await r._on_client_message(_state("elsewhere-n0"))
    assert len(_orders(r)) == sent + 1                    # control: still resent elsewhere


@pytest.mark.unit
async def test_a_failed_order_is_republished_under_a_new_revision(clock):  # noqa: F811
    r, _ = _make_robot()
    m = await _start(r, _mission())
    order = _order_id(r)
    r._dead_order_ids.add(order)
    await r._send_order()
    assert r._pending_send == server_module.NEW_REVISION
    assert len(_orders(r)) == 1
    await r._flush_pending_send()
    assert len(_orders(r)) == 2 and _orders(r)[1]["orderId"] != order
    assert m.status.order_rev == 1


@pytest.mark.unit
async def test_failed_status_marks_the_order_dead():
    r, _ = _make_robot()
    await _start(r, _mission())
    order = _order_id(r)
    msg = _state(order)
    msg.information = [types.VDA5050Info(infoType="missionStatus", infoLevel="INFO",
                                         infoDescription="failed")]
    await r._on_client_message(msg)
    assert order in r._dead_order_ids


@pytest.mark.unit
async def test_unresolvable_skip_is_not_remembered_and_taken_once_resolvable():
    r, _ = _make_robot()
    m = await _start(r, _mission(tree=_tree(_route("a", n=5))))
    order = _order_id(r)
    msg = _state(order)
    msg.errors = _skipped("unknown-n9-s4")
    r._process_node_reports(msg)
    assert not r._node_reports_seen and not m.status.skipped_nodes
    msg.errors = _skipped(f"{order}-s4")
    r._process_node_reports(msg)
    assert len(m.status.skipped_nodes) == 1 and r._node_reports_seen


@pytest.mark.unit
async def test_action_failed_node_skipped_is_not_a_node_failure():
    r, _ = _make_robot()
    m = await _start(r, _mission(tree=_tree({"name": "act", "parent": "root_sequence",
                                             "action": {"action_type": "dock_robot",
                                                        "action_parameters": {}}})))
    order = _order_id(r)
    act = types.VDA5050ActionState(actionId=f"{order}-s0-n1", actionType="dock_robot",
                                   actionStatus=Status.FAILED,
                                   resultDescription="node skipped")
    await r._on_client_message(_state(order, actions=[act]))
    assert m.status.node_status["act"].state != State.FAILED
    assert m.status.state != State.FAILED
