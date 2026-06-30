"""
Unit tests for the Agent Orchestrator trigger detection.

Pure logic — no MQTT broker, no Anthropic API key, no Docker required.
"""

import pytest

from packages.services.agent_orchestrator.triggers import (
    detect_events, max_severity, FiredEvent,
)


def _types(events):
    return {e.type for e in events}


@pytest.mark.unit
class TestQuietTelemetry:
    def test_identical_states_fire_nothing(self):
        state = {"operatingMode": "AUTOMATIC", "batteryState": {"batteryCharge": 90.0}}
        assert detect_events(state, dict(state)) == []

    def test_pose_only_change_fires_nothing(self):
        prev = {"operatingMode": "AUTOMATIC", "agvPosition": {"x": 1.0, "y": 1.0}}
        curr = {"operatingMode": "AUTOMATIC", "agvPosition": {"x": 2.0, "y": 3.0}}
        assert detect_events(prev, curr) == []


@pytest.mark.unit
class TestErrors:
    def test_new_warning_error(self):
        prev = {"errors": []}
        curr = {"errors": [{"errorType": "sensorFault", "errorLevel": "WARNING",
                            "errorDescription": "lidar degraded"}]}
        events = detect_events(prev, curr)
        assert _types(events) == {"new_error"}
        assert events[0].severity == "warning"

    def test_new_fatal_error_is_critical(self):
        prev = {"errors": []}
        curr = {"errors": [{"errorType": "motorFault", "errorLevel": "FATAL",
                            "errorDescription": "drive stopped"}]}
        events = detect_events(prev, curr)
        assert events[0].severity == "critical"

    def test_persistent_error_does_not_refire(self):
        err = {"errorType": "x", "errorLevel": "WARNING", "errorDescription": "d"}
        prev = {"errors": [err]}
        curr = {"errors": [dict(err)]}
        assert detect_events(prev, curr) == []

    def test_first_sight_standing_error_fires_once(self):
        curr = {"errors": [{"errorType": "x", "errorLevel": "FATAL", "errorDescription": "d"}]}
        events = detect_events(None, curr)
        assert _types(events) == {"new_error"}


@pytest.mark.unit
class TestEdgeBlocked:
    def _edge_blocked(self):
        return {
            "errorType": "edgeBlocked",
            "errorLevel": "WARNING",
            "errorDescription": "Edge blocked: Waypoint 1 unreachable after retries",
            "errorReferences": [
                {"referenceKey": "nodeId", "referenceValue": "m1-n0-s4"},
                {"referenceKey": "edgeId", "referenceValue": "m1-e3"},
            ],
        }

    def test_edge_blocked_fires_dedicated_event(self):
        events = detect_events({"errors": []}, {"errors": [self._edge_blocked()]})
        assert _types(events) == {"edge_blocked"}
        assert events[0].severity == "warning"
        # Detail carries the references so the agent can name the blocked edge.
        assert "node m1-n0-s4" in events[0].detail
        assert "edge m1-e3" in events[0].detail

    def test_edge_blocked_without_edge_reference(self):
        err = self._edge_blocked()
        err["errorReferences"] = [{"referenceKey": "nodeId", "referenceValue": "m1-n0-s4"}]
        events = detect_events({"errors": []}, {"errors": [err]})
        assert _types(events) == {"edge_blocked"}
        assert "edge" not in events[0].detail

    def test_edge_blocked_does_not_refire(self):
        prev = {"errors": [self._edge_blocked()]}
        curr = {"errors": [self._edge_blocked()]}
        assert detect_events(prev, curr) == []


@pytest.mark.unit
class TestSafetyState:
    def test_estop_engaged(self):
        prev = {"safetyState": {"eStop": "NONE", "fieldViolation": False}}
        curr = {"safetyState": {"eStop": "MANUAL", "fieldViolation": False}}
        events = detect_events(prev, curr)
        assert _types(events) == {"estop_engaged"}
        assert events[0].severity == "critical"

    def test_field_violation(self):
        prev = {"safetyState": {"eStop": "NONE", "fieldViolation": False}}
        curr = {"safetyState": {"eStop": "NONE", "fieldViolation": True}}
        assert _types(detect_events(prev, curr)) == {"field_violation"}

    def test_estop_persists_no_refire(self):
        prev = {"safetyState": {"eStop": "MANUAL", "fieldViolation": False}}
        curr = {"safetyState": {"eStop": "MANUAL", "fieldViolation": False}}
        assert detect_events(prev, curr) == []

    def test_estop_clearing_does_not_fire(self):
        prev = {"safetyState": {"eStop": "MANUAL", "fieldViolation": False}}
        curr = {"safetyState": {"eStop": "NONE", "fieldViolation": False}}
        assert detect_events(prev, curr) == []


@pytest.mark.unit
class TestOperatingMode:
    def test_mode_change_fires_warning(self):
        prev = {"operatingMode": "AUTOMATIC"}
        curr = {"operatingMode": "MANUAL"}
        events = detect_events(prev, curr)
        assert _types(events) == {"operating_mode_change"}
        assert events[0].severity == "warning"

    def test_first_sight_mode_does_not_fire(self):
        # No baseline -> can't call it a change.
        assert detect_events(None, {"operatingMode": "MANUAL"}) == []


@pytest.mark.unit
class TestActions:
    def test_action_failed(self):
        prev = {"actionStates": [{"actionId": "a1", "actionStatus": "RUNNING"}]}
        curr = {"actionStates": [{"actionId": "a1", "actionStatus": "FAILED"}]}
        events = detect_events(prev, curr)
        assert _types(events) == {"action_failed"}
        assert events[0].severity == "critical"

    def test_failed_action_does_not_refire(self):
        prev = {"actionStates": [{"actionId": "a1", "actionStatus": "FAILED"}]}
        curr = {"actionStates": [{"actionId": "a1", "actionStatus": "FAILED"}]}
        assert detect_events(prev, curr) == []


@pytest.mark.unit
class TestBattery:
    def test_downward_crossing_fires(self):
        prev = {"batteryState": {"batteryCharge": 25.0}}
        curr = {"batteryState": {"batteryCharge": 18.0}}
        events = detect_events(prev, curr, battery_low_threshold=20.0)
        assert _types(events) == {"battery_low"}

    def test_staying_low_does_not_refire(self):
        prev = {"batteryState": {"batteryCharge": 15.0}}
        curr = {"batteryState": {"batteryCharge": 14.0}}
        assert detect_events(prev, curr, battery_low_threshold=20.0) == []

    def test_above_threshold_quiet(self):
        prev = {"batteryState": {"batteryCharge": 90.0}}
        curr = {"batteryState": {"batteryCharge": 80.0}}
        assert detect_events(prev, curr, battery_low_threshold=20.0) == []

    def test_first_sight_low_battery_fires(self):
        curr = {"batteryState": {"batteryCharge": 10.0}}
        assert _types(detect_events(None, curr, battery_low_threshold=20.0)) == {"battery_low"}


@pytest.mark.unit
class TestMultipleAndSeverity:
    def test_multiple_events_in_one_message(self):
        prev = {"operatingMode": "AUTOMATIC",
                "safetyState": {"eStop": "NONE"}, "errors": []}
        curr = {"operatingMode": "MANUAL",
                "safetyState": {"eStop": "MANUAL"},
                "errors": [{"errorType": "e", "errorLevel": "FATAL", "errorDescription": "d"}]}
        events = detect_events(prev, curr)
        assert _types(events) == {"operating_mode_change", "estop_engaged", "new_error"}

    def test_max_severity_rolls_up(self):
        events = [
            FiredEvent("a", "info", ""),
            FiredEvent("b", "warning", ""),
            FiredEvent("c", "critical", ""),
        ]
        assert max_severity(events) == "critical"

    def test_max_severity_empty(self):
        assert max_severity([]) == "info"


@pytest.mark.unit
class TestMalformedInput:
    def test_missing_fields_are_safe(self):
        assert detect_events({}, {}) == []

    def test_garbage_battery_is_ignored(self):
        curr = {"batteryState": {"batteryCharge": "not-a-number"}}
        assert detect_events(None, curr) == []
