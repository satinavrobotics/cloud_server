"""Unit tests for packages/events/codes.py and schemas.py."""

import datetime
import json

import pytest

from packages.events import schemas
from packages.events.codes import CODES, EventCode, Severity, Source, meta_for

pytestmark = pytest.mark.unit

TS = datetime.datetime(2026, 9, 24, 12, 0, 0, tzinfo=datetime.timezone.utc)

VALID_PAYLOADS = {
    EventCode.MISSION_RUN_STARTED: {"mission_name": "m1", "map_id": "map", "recording_level": "full"},
    EventCode.MISSION_RUN_FINISHED: {"mission_name": "m1", "outcome": "FAILED",
                                     "cause": "NAV.GOAL_UNREACHABLE", "passes_completed": 2},
    EventCode.MISSION_NODE_FAILED: {"mission_name": "m1", "node_id": "n3"},
    EventCode.MISSION_REROUTED: {"mission_name": "m1", "blocked_edges": ["e1", "e2"]},
    EventCode.MISSION_EDGE_BLOCKED: {"mission_name": "m1", "edge_id": "e1"},
    EventCode.MISSION_CANCEL_REQUESTED: {"mission_name": "m1", "actor": "alice"},
    EventCode.ROBOT_STATE_CHANGED: {"old": "IDLE", "new": "ON_TASK"},
    EventCode.ROBOT_ONLINE: {"connection_state": "ONLINE"},
    EventCode.ROBOT_OFFLINE: {"connection_state": "CONNECTIONBROKEN"},
    EventCode.ROBOT_HEARTBEAT_LOST: {"last_seen": TS, "timeout_s": 10},
    EventCode.ROBOT_HEARTBEAT_RESTORED: {"last_seen": TS, "gap_s": 42.5},
    EventCode.ROBOT_ERROR_RAISED: {"error_type": "motorFault", "error_level": "FATAL"},
    EventCode.ROBOT_ERROR_CLEARED: {"error_type": "motorFault"},
    EventCode.ROBOT_SW_VERSION_CHANGED: {"old": None, "new": "jetson-2026.09.1+gabc123"},
    EventCode.BATTERY_LOW: {"battery_percent": 19.5, "threshold": 20},
    EventCode.BATTERY_OK: {"battery_percent": 25, "threshold": 25},
    EventCode.GNSS_RTK_LOST: {"old_fix": "RTK_FIXED", "new_fix": "RTK_FLOAT", "sats": 12},
    EventCode.GNSS_RTK_RECOVERED: {"old_fix": "RTK_FLOAT", "new_fix": "RTK_FIXED"},
    EventCode.NAV_RECOVERY_ENTERED: {"cause": "stuck"},
    EventCode.NAV_RECOVERY_EXITED: {"cause": "stuck", "duration_s": 3.2},
    EventCode.NAV_GOAL_BLOCKED: {"cause": "goal_in_obstacle"},
    EventCode.SYSTEM_THERMAL_HIGH: {"temp_c": 86.0, "threshold_c": 85.0, "sensor": "gpu"},
    EventCode.SYSTEM_THERMAL_OK: {"temp_c": 77.0, "threshold_c": 78.0},
    EventCode.SYSTEM_NODE_DOWN: {"node": "/nav2/controller"},
    EventCode.SYSTEM_NODE_UP: {"node": "/nav2/controller"},
    EventCode.MAP_DELETE_FAILED: {"map_name": "site-a", "attempts": 5, "error": "minio timeout"},
    EventCode.TELEMETRY_RECORDING_CHANGED: {"old_level": "events_only", "new_level": "off",
                                            "scope": "site", "scope_id": "site-a", "actor": "bob"},
}


def test_every_code_has_metadata():
    assert set(CODES) == set(EventCode)


def test_codes_are_str_enums_with_spec_values():
    assert EventCode.MISSION_RUN_STARTED == "MISSION.RUN_STARTED"
    assert all(code.value == code.value.upper() and "." in code.value for code in EventCode)
    assert len({code.value for code in EventCode}) == len(EventCode)


def test_severity_and_source_values_match_schema():
    assert [s.value for s in Severity] == ["info", "warning", "error", "critical"]
    assert [s.value for s in Source] == ["dispatch", "api"]


def test_meta_for_accepts_plain_string():
    assert meta_for("BATTERY.LOW").severity is Severity.WARNING
    with pytest.raises(ValueError):
        meta_for("NOT.A_CODE")


def test_every_code_has_a_valid_example():
    assert set(VALID_PAYLOADS) == set(EventCode)


@pytest.mark.parametrize("code", list(EventCode), ids=lambda c: c.value)
def test_valid_payload_round_trips(code):
    out = schemas.validate_payload(meta_for(code).payload_model, VALID_PAYLOADS[code])
    assert schemas.INVALID_KEY not in out


def test_validated_payload_is_json_safe():
    out = schemas.validate_payload(schemas.HeartbeatLost, {"last_seen": TS, "timeout_s": 10})
    assert out["timeout_s"] == 10.0
    assert isinstance(out["last_seen"], str) and out["last_seen"].startswith("2026-09-24T12:00:00")
    assert json.loads(json.dumps(out)) == out


@pytest.mark.parametrize("payload", [
    {},
    {"mission_name": "m1", "outcome": "EXPLODED"},
    {"mission_name": "m1", "outcome": "FAILED", "surprise": 1},
], ids=["missing", "bad-enum", "extra-key"])
def test_strict_mode_raises(payload):
    with pytest.raises(schemas.InvalidPayloadError):
        schemas.validate_payload(schemas.RunFinished, payload)


def test_lenient_mode_flags_and_keeps_raw_payload(caplog):
    raw = {"mission_name": "m1", "outcome": "EXPLODED"}
    out = schemas.validate_payload(schemas.RunFinished, raw, strict=False)
    assert out["_invalid"] is True
    assert out["outcome"] == "EXPLODED"
    assert "outcome" in out["_error"]
    assert "_invalid" not in raw
    assert "Invalid RunFinished payload" in caplog.text


def test_module_switch_controls_default_mode():
    schemas.set_strict_validation(False)
    assert schemas.validate_payload(schemas.RosNode, {})["_invalid"] is True
    schemas.set_strict_validation(True)
    with pytest.raises(schemas.InvalidPayloadError):
        schemas.validate_payload(schemas.RosNode, {})
