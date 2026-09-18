"""Unit tests for the force-cancel-order operator escape hatch (RobotSpecV1.

needs_order_cancel / POST /api/v1/robots/{name}/cancel-order).

Field motivation: a robot can end up holding a VDA5050 order nothing tracks
anymore -- the mission that dispatched it hit a client-side error, was
force-failed by a timeout, or was otherwise abandoned server-side -- and keep
reporting that stale orderId forever, rejecting every subsequently dispatched
order as "An order is running" (see test_mission_timeout_cancel.py and the
"zombie order" incident this was built to recover from without hand-publishing
raw MQTT). `_on_robot_change` must send a cancelOrder the moment it sees this
flag rise, independent of whether any mission is currently tracked for the
robot, and clear it back to False so it can't re-fire on the next echo of the
same write.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock

import cloud_common.objects as api_objects
import packages.controllers.mission.vda5050_types as types
from packages.controllers.mission.server import Robot
from packages.database.postgres import PostgresDatabase


def _make_robot():
    db = AsyncMock(spec=PostgresDatabase)
    db.update_status = AsyncMock()
    db.update_spec = AsyncMock()
    client = MagicMock()
    server = MagicMock()
    server.disable_request_factsheet = True
    server.push_telemetry = False
    server.mission_ctrl_url = None
    r = Robot("r1", db, client, "prefix", server)
    r._robot_object = api_objects.RobotObjectV1(name="r1", status={})
    r._robot_object.status.online = True
    r._robot_object.status.factsheet.agv_class = "CARRIER"
    return r, db


@pytest.mark.unit
async def test_needs_order_cancel_sends_cancel_order_with_no_mission_tracked():
    """The whole point: this must work even when _current_mission is None --
    unlike the ordinary cancel path, which only ever fires for a tracked mission."""
    r, db = _make_robot()
    r._current_mission = None
    r._send_instant_action = AsyncMock()

    update = api_objects.RobotObjectV1(name="r1", status={})
    update.status.online = True
    update.status.factsheet.agv_class = "CARRIER"
    update.needs_order_cancel = True

    await r._on_robot_change(update)

    r._send_instant_action.assert_awaited_once()
    sent_action = r._send_instant_action.await_args.args[0]
    assert sent_action.actionType == types.VDA5050InstantActionType.CANCEL_ORDER


@pytest.mark.unit
async def test_needs_order_cancel_is_cleared_after_sending():
    """Must self-clear (and persist that clear) so the next echo of the same
    underlying write doesn't send a second cancelOrder."""
    r, db = _make_robot()
    r._send_instant_action = AsyncMock()

    update = api_objects.RobotObjectV1(name="r1", status={})
    update.status.online = True
    update.status.factsheet.agv_class = "CARRIER"
    update.needs_order_cancel = True

    await r._on_robot_change(update)

    assert r._robot_object.needs_order_cancel is False
    db.update_spec.assert_awaited_once()


@pytest.mark.unit
async def test_needs_order_cancel_does_not_refire_while_already_false_to_false():
    """A normal robot-change echo with the flag steady at False must not send
    anything -- only a rising edge (False -> True) triggers a cancel."""
    r, _ = _make_robot()
    r._send_instant_action = AsyncMock()

    update = api_objects.RobotObjectV1(name="r1", status={})
    update.status.online = True
    update.status.factsheet.agv_class = "CARRIER"
    update.needs_order_cancel = False

    await r._on_robot_change(update)

    r._send_instant_action.assert_not_awaited()


def _cancel_request():
    update = api_objects.RobotObjectV1(name="r1", status={})
    update.status.online = True
    update.status.factsheet.agv_class = "CARRIER"
    update.needs_order_cancel = True
    return update


@pytest.mark.unit
async def test_stale_true_echo_does_not_send_a_second_cancel_order():
    """A second echo that still reads True (the API's write re-read before our
    clear lands) must not mint a second cancelOrder while the first is still
    outstanding -- same one-cancel-at-a-time rule as the explicit-cancel path."""
    r, _ = _make_robot()
    r._send_instant_action = AsyncMock()

    await r._on_robot_change(_cancel_request())
    await r._on_robot_change(_cancel_request())

    r._send_instant_action.assert_awaited_once()
    assert r._robot_object.needs_order_cancel is False


@pytest.mark.unit
async def test_cleared_spec_is_what_gets_persisted_and_cancel_is_tracked():
    r, db = _make_robot()
    r._send_instant_action = AsyncMock()

    await r._on_robot_change(_cancel_request())

    persisted_spec = db.update_spec.await_args.args[2]
    assert persisted_spec.needs_order_cancel is False
    assert r._has_outstanding_cancel()


@pytest.mark.unit
async def test_request_already_pending_when_the_robot_is_first_seen_still_fires():
    """Regression: the dispatcher was down/restarting when the operator asked, so
    the very first robot object it sees already carries needs_order_cancel=True.
    An edge check against the previous object never fired here and never cleared
    the flag, leaving the escape hatch dead exactly when it was needed."""
    r, db = _make_robot()
    r._robot_object = None
    r._send_instant_action = AsyncMock()
    r._try_start_mission = AsyncMock()
    r._check_robot_online = AsyncMock()

    await r._on_robot_change(_cancel_request())

    r._send_instant_action.assert_awaited_once()
    assert r._send_instant_action.await_args.args[0].actionType == \
        types.VDA5050InstantActionType.CANCEL_ORDER
    assert r._robot_object.needs_order_cancel is False
    db.update_spec.assert_awaited_once()
