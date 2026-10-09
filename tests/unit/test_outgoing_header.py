"""Outgoing order / instantActions headers: UTC Z millisecond timestamp, identity, headerId
that continues above an earlier dispatcher process's."""
import re

import pytest

import packages.controllers.mission.server as server_module
import packages.controllers.mission.vda5050_types as types
from tests.unit.test_mission_lifecycle_fixes import (
    _make_robot, _mission, _orders, _published, _start, _state)

TS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")


async def _order_and_instant(r):
    await _start(r, _mission())
    await r._send_instant_action(types.VDA5050Action(
        actionType=types.VDA5050InstantActionType.FACTSHEET_REQUEST, actionId="a"))
    return _orders(r)[-1], _published(r, "/instantActions")[-1]


@pytest.mark.unit
async def test_timestamps_are_utc_z_with_milliseconds():
    r, _ = _make_robot()
    order, instant = await _order_and_instant(r)
    assert TS.match(order["timestamp"]) and TS.match(instant["timestamp"])


@pytest.mark.unit
def test_utc_timestamp_converts_aware_times():
    import datetime
    t = datetime.datetime(2026, 10, 9, 13, 22, 33, 123987,
                          tzinfo=datetime.timezone(datetime.timedelta(hours=2)))
    assert types.utc_timestamp(t) == "2026-10-09T11:22:33.123Z"


@pytest.mark.unit
async def test_identity_is_filled_from_topic_then_robot_report():
    r, _ = _make_robot()
    r._manufacturer = "RobotCompany"        # what a "uagv/v2/RobotCompany" prefix gives
    order, instant = await _order_and_instant(r)
    assert order["serialNumber"] == instant["serialNumber"] == "r1"
    assert order["manufacturer"] == instant["manufacturer"] == "RobotCompany"   # topic fallback
    reported = _state("x")
    reported.manufacturer = "Acme"
    await r._on_client_message(reported)
    await r._send_instant_action(types.VDA5050Action(
        actionType=types.VDA5050InstantActionType.FACTSHEET_REQUEST, actionId="b"))
    assert _published(r, "/instantActions")[-1]["manufacturer"] == "Acme"


@pytest.mark.unit
async def test_header_id_continues_above_previous_run_after_restart(monkeypatch):
    clock = [server_module.HEADER_ID_EPOCH + 1000.0]
    monkeypatch.setattr(server_module._wall_time, "time", lambda: clock[0])
    r1, _ = _make_robot()
    for _ in range(10):
        r1._next_header_id("order")
    last_before = r1._header_ids["order"] - 1
    clock[0] += 5.0                                     # restart 5 s later
    r2, _ = _make_robot()
    assert r2._next_header_id("order") > last_before


@pytest.mark.unit
def test_initial_header_id_fits_uint32_for_decades():
    year = 365 * 86400
    assert server_module.initial_header_id(server_module.HEADER_ID_EPOCH + 30 * year) < 2 ** 32
    assert server_module.initial_header_id(0) == 0


@pytest.mark.unit
def test_manufacturer_defaults_to_the_topic_prefix_component():
    r, _ = _make_robot()
    r2 = server_module.Robot("r2", r._database, r._mqtt_client, "uagv/v2/Acme", r._robot_server)
    assert r2._manufacturer == "Acme"
