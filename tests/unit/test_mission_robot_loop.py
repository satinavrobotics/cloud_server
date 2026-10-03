"""Regression guard for the dispatcher's per-robot message loop (Robot.run).

It used to wrap `await self._messages.get()` in the same broad `except Exception` as the
handlers. A queue that fails on every call (in the unit tests: an asyncio.Queue bound to a
previous test's event loop, from a Robot built in a sync test) then spun forever, logging a
warning per iteration -- millions per second, all kept by pytest's log capture, until the
host ran out of memory. A failing queue must end the loop; a failing handler must not.
"""
import asyncio

import pytest

pytest.importorskip("py_trees")

from unittest.mock import AsyncMock, MagicMock, patch  # noqa: E402

import cloud_common.objects as api_objects  # noqa: E402
from packages.controllers.mission.server import Robot  # noqa: E402
from packages.database.postgres import PostgresDatabase  # noqa: E402

pytestmark = pytest.mark.unit

MAX_WARNINGS = 20


def _robot():
    server = MagicMock()
    server.push_telemetry = False
    with patch.object(Robot, "run", new=AsyncMock()):   # no background loop from __init__
        robot = Robot("r1", AsyncMock(spec=PostgresDatabase), MagicMock(), "p", server)
    warnings = []

    def warning(message):
        warnings.append(message)
        if len(warnings) >= MAX_WARNINGS:                # a hot loop: stop it, bounded
            robot._alive = False
    robot.warning = warning
    return robot, warnings


async def test_a_failing_queue_ends_the_loop_instead_of_spinning():
    robot, warnings = _robot()
    robot._messages = MagicMock()
    robot._messages.get = AsyncMock(side_effect=RuntimeError("bound to a different event loop"))
    with pytest.raises(RuntimeError):
        await asyncio.wait_for(Robot.run(robot), timeout=2)
    assert robot._messages.get.await_count == 1
    assert warnings == []


async def test_a_failing_handler_is_logged_and_the_loop_goes_on():
    robot, warnings = _robot()
    handled = []

    async def on_robot_change(message):
        handled.append(message.name)
        if len(handled) == 1:
            raise ValueError("handler bug")
        robot._alive = False
    robot._on_robot_change = on_robot_change
    await robot._messages.put(api_objects.RobotObjectV1(name="a", status={}))
    await robot._messages.put(api_objects.RobotObjectV1(name="b", status={}))
    await asyncio.wait_for(Robot.run(robot), timeout=2)
    assert handled == ["a", "b"]
    assert len(warnings) == 1 and "handler bug" in warnings[0]


def test_datum_changed_ignores_jitter_but_not_moves_frame_or_bearing():
    from packages.controllers.mission.server import _datum_changed
    from cloud_common.objects.robot import RobotDatumV1 as D
    base = D(latitude=47.0, longitude=19.0, bearing_deg=359.9)
    assert _datum_changed(None, base)
    assert _datum_changed(D(), base)                      # no previous position
    assert not _datum_changed(base, D(latitude=47.000005, longitude=19.0, bearing_deg=0.1))
    assert _datum_changed(base, D(latitude=47.0001, longitude=19.0, bearing_deg=359.9))
    assert _datum_changed(base, D(latitude=47.0, longitude=19.0, bearing_deg=5.0))
    assert _datum_changed(base, D(latitude=47.0, longitude=19.0, bearing_deg=359.9, frame="utm"))


# --- approximate position (map-location plan B) ---------------------------------------------

def _approx(**kw):
    from packages.controllers.mission.vda5050_types import RobotApproxPosition
    base = dict(latitude=47.0, longitude=19.0, accuracy_m=2.0, fix_quality="rtk", source="gnss")
    base.update(kw)
    return RobotApproxPosition(**base)


def test_approx_position_payload_validation():
    import pydantic
    from packages.controllers.mission.vda5050_types import RobotApproxPosition
    assert _approx(stamp="not a time").stamp is None
    assert _approx(stamp=1700000000).stamp is not None
    assert RobotApproxPosition(latitude=1, longitude=2).source == "gnss"
    for bad in (dict(latitude=91), dict(longitude=-181), dict(accuracy_m=-1)):
        with pytest.raises(pydantic.ValidationError):
            _approx(**bad)


def test_approx_position_changed_threshold():
    from packages.controllers.mission.server import _approx_position_changed
    import cloud_common.objects.robot as ro
    import datetime
    old = ro.RobotApproxPositionV1(latitude=47.0, longitude=19.0, accuracy_m=2.0,
                                   fix_quality="rtk", source="gnss",
                                   stored_at=datetime.datetime.now(datetime.timezone.utc))
    assert _approx_position_changed(None, _approx())
    assert not _approx_position_changed(old, _approx(latitude=47.00002))     # ~2 m
    assert _approx_position_changed(old, _approx(latitude=47.0001))          # ~11 m
    assert _approx_position_changed(old, _approx(accuracy_m=5.0))
    assert _approx_position_changed(old, _approx(source="manual"))


def test_approx_position_tolerates_accuracy_jitter_but_refreshes_when_stale():
    import datetime
    from packages.controllers.mission.server import _approx_position_changed
    import cloud_common.objects.robot as ro
    now = datetime.datetime(2026, 10, 3, 12, 0, tzinfo=datetime.timezone.utc)

    def stored(age_s=10, **kw):
        base = dict(latitude=47.0, longitude=19.0, accuracy_m=2.0, fix_quality="rtk",
                    source="gnss", stored_at=now - datetime.timedelta(seconds=age_s))
        return ro.RobotApproxPositionV1(**{**base, **kw})

    assert not _approx_position_changed(stored(), _approx(accuracy_m=2.1), now)   # 5 %
    assert not _approx_position_changed(stored(), _approx(accuracy_m=1.7), now)   # 15 %
    assert _approx_position_changed(stored(), _approx(accuracy_m=2.6), now)       # 30 %
    assert _approx_position_changed(stored(), _approx(accuracy_m=None), now)
    assert _approx_position_changed(stored(accuracy_m=None), _approx(), now)
    assert not _approx_position_changed(stored(accuracy_m=None), _approx(accuracy_m=None), now)
    # A parked robot: nothing changed, but the stored copy ages out.
    assert not _approx_position_changed(stored(age_s=299), _approx(), now)
    assert _approx_position_changed(stored(age_s=301), _approx(), now)
    assert _approx_position_changed(stored(stored_at=None), _approx(), now)


def test_approx_position_reads_the_old_received_at_name():
    import datetime
    import cloud_common.objects.robot as ro
    stamp = "2026-10-03T10:00:00+00:00"
    old = ro.RobotApproxPositionV1(latitude=1.0, longitude=2.0, received_at=stamp)
    assert old.stored_at == datetime.datetime(2026, 10, 3, 10, tzinfo=datetime.timezone.utc)
    assert "received_at" not in old.dict()


def _approx_robot():
    robot, _ = _robot()
    robot._robot_object = api_objects.RobotObjectV1(name="r1", status={})
    robot._replace_geo_session = AsyncMock()
    return robot


async def test_approx_position_is_stored_in_status_only():
    robot = _approx_robot()
    await robot._process_approx_position_message(_approx())
    pos = robot._robot_object.status.approx_position
    assert (pos.latitude, pos.longitude, pos.source) == (47.0, 19.0, "gnss")
    assert pos.stored_at is not None
    robot._database.update_status.assert_awaited_once()
    robot._database.update_spec_fields.assert_not_called()
    robot._database.update_spec.assert_not_called()
    robot._replace_geo_session.assert_not_called()
    assert robot._robot_object.spec.datum.latitude is None


async def test_approx_position_skips_small_moves_and_rejects_zero():
    robot = _approx_robot()
    await robot._process_approx_position_message(_approx())
    await robot._process_approx_position_message(_approx(latitude=47.00001))
    assert robot._database.update_status.await_count == 1
    await robot._process_approx_position_message(_approx(latitude=47.001))
    assert robot._database.update_status.await_count == 2
    await robot._process_approx_position_message(_approx(latitude=0.0, longitude=0.0))
    assert robot._database.update_status.await_count == 2
    assert robot._robot_object.status.approx_position.latitude == 47.001


async def test_approx_position_reaches_the_handler_through_the_loop():
    robot = _approx_robot()
    seen = []

    async def handler(msg):
        seen.append(msg)
        robot._alive = False
    robot._process_approx_position_message = handler
    await robot._messages.put(_approx())
    await asyncio.wait_for(Robot.run(robot), timeout=2)
    assert len(seen) == 1


def test_mqtt_on_message_routes_approx_position_topic():
    import json
    from packages.controllers.mission.server import ClientApproxPositionMessage, RobotServer
    server = MagicMock()
    server._mqtt_prefix = "uagv/v2/sati"
    queued = []
    server._enqueue = lambda q, obj: queued.append(obj)
    msg = MagicMock(topic="uagv/v2/sati/r1/approx_position",
                    payload=json.dumps({"latitude": 47.0, "longitude": 19.0}))
    RobotServer._mqtt_on_message(server, None, None, msg)
    assert len(queued) == 1 and isinstance(queued[0], ClientApproxPositionMessage)
    assert queued[0].name == "r1" and queued[0].payload.latitude == 47.0
    server.warning.assert_not_called()
    # out of range -> warned, not queued
    msg.payload = json.dumps({"latitude": 99, "longitude": 19.0})
    RobotServer._mqtt_on_message(server, None, None, msg)
    assert len(queued) == 1 and server.warning.called
