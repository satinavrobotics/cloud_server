"""Unit tests for packages/events/ids.py."""

import datetime
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from packages.events.codes import EventCode
from packages.events.ids import EVENT_NAMESPACE, event_id, normalize_ts, ts_key

pytestmark = pytest.mark.unit

PROJECT_ROOT = Path(__file__).resolve().parents[3]
UTC = datetime.timezone.utc
TS = datetime.datetime(2026, 9, 24, 12, 0, 0, 123456, tzinfo=UTC)


def test_key_format_is_pinned():
    expected = uuid.uuid5(EVENT_NAMESPACE,
                          "ROBOT.ERROR_RAISED|r1|2026-09-24T12:00:00.123456Z|motorFault")
    assert event_id(EventCode.ROBOT_ERROR_RAISED, "r1", TS, "motorFault") == expected


def test_namespace_is_pinned():
    assert EVENT_NAMESPACE == uuid.UUID("5b0f6c1e-3d0a-4e8e-9a51-7c2f0d6b8e14")


def test_enum_and_string_code_give_same_id():
    assert event_id(EventCode.BATTERY_LOW, "r1", TS) == event_id("BATTERY.LOW", "r1", TS)


def test_same_instant_in_other_timezone_gives_same_id():
    cest = datetime.timezone(datetime.timedelta(hours=2))
    assert event_id("BATTERY.LOW", "r1", TS.astimezone(cest)) == event_id("BATTERY.LOW", "r1", TS)


def test_naive_timestamp_is_treated_as_utc():
    assert event_id("BATTERY.LOW", "r1", TS.replace(tzinfo=None)) == event_id("BATTERY.LOW", "r1", TS)


def test_microseconds_are_significant():
    later = TS + datetime.timedelta(microseconds=1)
    assert event_id("BATTERY.LOW", "r1", later) != event_id("BATTERY.LOW", "r1", TS)


@pytest.mark.parametrize("change", [
    {"code": "BATTERY.OK"}, {"robot_name": "r2"}, {"discriminator": "x"},
])
def test_each_key_part_changes_the_id(change):
    base = {"code": "BATTERY.LOW", "robot_name": "r1", "ts": TS, "discriminator": None}
    assert event_id(**{**base, **change}) != event_id(**base)


def test_none_and_empty_parts_are_equivalent():
    assert event_id("MAP.DELETE_FAILED", None, TS, "m") == event_id("MAP.DELETE_FAILED", "", TS, "m")


def test_normalize_ts_returns_aware_utc():
    cest = datetime.timezone(datetime.timedelta(hours=2))
    out = normalize_ts(TS.astimezone(cest))
    assert out.tzinfo is UTC and out == TS
    assert ts_key(TS) == "2026-09-24T12:00:00.123456Z"


def test_normalize_ts_rejects_non_datetime():
    with pytest.raises(TypeError):
        normalize_ts("2026-09-24T12:00:00Z")


def test_same_id_across_processes():
    script = (
        "import datetime;"
        "from packages.events.ids import event_id;"
        "ts = datetime.datetime(2026, 9, 24, 14, 0, 0, 123456,"
        " tzinfo=datetime.timezone(datetime.timedelta(hours=2)));"
        "print(event_id('ROBOT.ERROR_RAISED', 'r1', ts, 'motorFault'))"
    )
    env = {**os.environ, "PYTHONPATH": str(PROJECT_ROOT), "PYTHONHASHSEED": "random"}
    ids = {
        subprocess.run([sys.executable, "-c", script], cwd=PROJECT_ROOT, env=env,
                       capture_output=True, text=True, check=True).stdout.strip()
        for _ in range(2)
    }
    assert ids == {str(event_id(EventCode.ROBOT_ERROR_RAISED, "r1", TS, "motorFault"))}
