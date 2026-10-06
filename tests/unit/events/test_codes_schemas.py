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
    EventCode.MISSION_DELETED: {"mission_name": "m1", "with_reruns": True,
                                "deleted_missions": ["m1", "m1-rerun-1"], "deleted_runs": 2,
                                "deleted_events": 7, "deleted_trajectory": 40,
                                "run_ids": ["6f1c0c2e-0000-4000-8000-000000000001"],
                                "robots": ["r1"]},
    EventCode.RUN_ARCHIVED: {"count": 1, "run_ids": ["6f1c0c2e-0000-4000-8000-000000000001"]},
    EventCode.RUN_UNARCHIVED: {"count": 2, "run_ids": [], "mission": "m1"},
    EventCode.TELEMETRY_RECORDING_CHANGED: {"old_level": "events_only", "new_level": "off",
                                            "scope": "site", "scope_id": "site-a", "actor": "bob"},
    EventCode.SYSTEM_RECORDER_ALERT_RAISED: {"alert": "writer_queue_high", "process": "api",
                                             "value": 85.0, "threshold": 80.0},
    EventCode.SYSTEM_RECORDER_ALERT_CLEARED: {"alert": "report_stale", "process": "dispatch",
                                              "value": 4.2, "threshold": 60.0, "raised_at": TS,
                                              "duration_s": 120.0},
    EventCode.MAP_CREATED: {"map_name": "yard", "map_type": "geo", "state": "draft"},
    EventCode.MAP_ARCHIVED: {"map_name": "yard", "map_type": "local", "state": "archived"},
    EventCode.MAP_RESTORED: {"map_name": "yard", "state": "ready", "actor": None},
    EventCode.MAP_SESSION_STARTED: {"map_name": "yard", "session_id": "s1",
                                    "map_state": "mapping", "aligned": True,
                                    "map_T_session": {"tx": 1.5, "ty": -2.0, "yaw": 0.01}},
    EventCode.MAP_SESSION_PAUSED: {"map_name": "yard", "session_id": "s1", "map_state": "paused"},
    EventCode.MAP_SESSION_RESUMED: {"map_name": "yard", "session_id": "s1",
                                    "map_state": "mapping"},
    EventCode.MAP_SESSION_FINISHED: {"map_name": "yard", "session_id": "s1",
                                     "map_state": "ready", "aligned": False},
    EventCode.MAP_DELETED: {"map_name": "yard", "requested_at": "2026-09-29T08:00:00+00:00",
                            "attempts": 1},
    EventCode.MAP_SESSION_REALIGNED: {"map_name": "yard", "session_id": "s1",
                                      "map_T_session": {"tx": 1.0, "ty": 2.0, "yaw": 0.1}},
    EventCode.MAP_SESSION_PLACED: {"map_name": "shed", "session_id": "s2", "purpose": "operate",
                                   "map_T_session": {"tx": 1.0, "ty": 2.0, "yaw": 0.1},
                                   "placement": {"pose": {"x": 1, "y": 2, "yaw": 0.1},
                                                 "robot_pose": {"x": 0, "y": 0, "theta": 0},
                                                 "source": "user", "actor": None}},
    EventCode.MAP_SESSION_UNPLACED: {"map_name": "shed", "session_id": "s2",
                                     "purpose": "operate", "reason": "run_changed",
                                     "evidence": {"state_header_id": 0,
                                                  "last_state_header_id": 812}},
    EventCode.MAP_INGEST_REJECTED: {"reason": "session_paused", "dropped_nodes": 3,
                                    "dropped_images": 6, "since": "2026-09-29T08:00:00+00:00",
                                    "map_name": "yard", "map_state": "paused",
                                    "session_id": "s1"},
    EventCode.MAP_TYPE_CHANGED: {"map_name": "lab", "old_type": "local", "new_type": "geo",
                                 "geo": {"utm_zone": 34, "utm_north": True, "origin_e": 1.0,
                                         "origin_n": 2.0, "bearing_deg": 10.0},
                                 "old_geo": None, "operating": ["r1"], "actor": None},
    EventCode.MAP_SLAM_CHANGED: {"map_name": "lab", "slam_map": True, "actor": None},
    EventCode.MAP_RECONSTRUCTION_STARTED: {"map_name": "lab", "job_id": "j1",
                                           "params": {"voxel_m": 0.05}, "nodes_with_depth": 412,
                                           "attempt": 1},
    EventCode.MAP_RECONSTRUCTION_FINISHED: {"map_name": "lab", "job_id": "j1",
                                            "points": 2310455, "nodes_used": 409,
                                            "frames_skipped": {"no_valid_depth": 3},
                                            "voxel_m": 0.05, "duration_s": 58.2},
    EventCode.MAP_RECONSTRUCTION_FAILED: {"map_name": "lab", "job_id": "j1",
                                          "reason": "url_expired", "stage": "integrating",
                                          "message": "GET depth returned 403"},
}


def test_every_code_has_metadata():
    assert set(CODES) == set(EventCode)


def test_codes_are_str_enums_with_spec_values():
    assert EventCode.MISSION_RUN_STARTED == "MISSION.RUN_STARTED"
    assert all(code.value == code.value.upper() and "." in code.value for code in EventCode)
    assert len({code.value for code in EventCode}) == len(EventCode)


def test_severity_and_source_values_match_schema():
    assert [s.value for s in Severity] == ["info", "warning", "error", "critical"]
    # graph_builder: migration 20260929_01_maps_m2 widens fleet_events_source_check;
    # reconstruction: 20261003_01_map_reconstructions.
    assert [s.value for s in Source] == ["dispatch", "api", "graph_builder", "reconstruction"]


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
