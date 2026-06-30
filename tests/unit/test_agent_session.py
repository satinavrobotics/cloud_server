"""
Unit tests for RobotAgentSession.

Uses a degraded (no-API-key) FleetAgent so no broker, key, or network is needed.
"""

import pytest

from packages.services.agent_orchestrator.session import RobotAgentSession
from packages.services.agent_orchestrator.agent import FleetAgent


def _session(history_size: int = 100) -> RobotAgentSession:
    agent = FleetAgent(api_key=None, model="claude-haiku-4-5")
    return RobotAgentSession("amr-1", agent, battery_low_threshold=20.0,
                             history_size=history_size)


@pytest.mark.unit
class TestDetect:
    def test_detect_updates_baseline(self):
        s = _session()
        state = {"operatingMode": "AUTOMATIC", "safetyState": {"eStop": "NONE"}, "errors": []}
        assert s.last_state is None
        s.detect(state)
        assert s.last_state == state

    def test_detect_is_edge_triggered_across_messages(self):
        s = _session()
        normal = {"operatingMode": "AUTOMATIC", "safetyState": {"eStop": "NONE"}, "errors": []}
        assert s.detect(normal) == []          # first sight, nothing standing
        assert s.detect(dict(normal)) == []    # identical -> quiet
        estop = {"operatingMode": "AUTOMATIC", "safetyState": {"eStop": "MANUAL"}, "errors": []}
        events = s.detect(estop)
        assert {e.type for e in events} == {"estop_engaged"}


@pytest.mark.unit
class TestSummarizeAndRecord:
    def test_summarize_returns_body_without_appending(self):
        s = _session()
        curr = {"safetyState": {"eStop": "MANUAL"},
                "errors": [{"errorType": "x", "errorLevel": "FATAL", "errorDescription": "drive"}]}
        events = s.detect(curr)
        body = s.summarize(events, curr)
        # summarize must NOT touch history (that write belongs on the event loop)
        assert len(s.insights) == 0
        assert body["robot"] == "amr-1"
        assert body["severity"] == "critical"
        assert body["degraded"] is True
        assert "id" not in body and "timestamp" not in body

    def test_record_appends_and_recent_is_newest_first(self):
        s = _session()
        s.record({"id": 1, "severity": "info", "summary": "a"})
        s.record({"id": 2, "severity": "warning", "summary": "b"})
        recent = s.recent_insights()
        assert [i["id"] for i in recent] == [2, 1]

    def test_history_caps_at_maxlen(self):
        s = _session(history_size=3)
        for i in range(5):
            s.record({"id": i, "summary": str(i)})
        assert len(s.insights) == 3
        assert [i["id"] for i in s.recent_insights()] == [4, 3, 2]


@pytest.mark.unit
class TestStatsSnapshot:
    def test_snapshot_shape(self):
        s = _session()
        snap = s.stats_snapshot()
        assert snap == {"robot": "amr-1", "insights": 0, "has_state": False}
        s.detect({"operatingMode": "AUTOMATIC"})
        s.record({"id": 1})
        snap = s.stats_snapshot()
        assert snap["insights"] == 1 and snap["has_state"] is True
