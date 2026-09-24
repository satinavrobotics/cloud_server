"""Unit tests for packages/api/telemetry_detectors.py: the API's thermal, ROS-node, recovery and
blocked-goal detectors, diagnostics_ts row extraction and robot_latest rehydration."""

import datetime

import pytest

from packages.api.telemetry_detectors import (
    DiagnosticsDetector, NavSupervisorDetector, down_topics, hottest, robot_ts, stale_sources,
    stamp_ts,
)
from packages.events import schemas
from packages.events.codes import EventCode
from packages.events.emit import build_row
from packages.telemetry_ingest import tables

UTC = datetime.timezone.utc
T0 = datetime.datetime(2026, 9, 24, 12, 0, 0, tzinfo=UTC)

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _strict_payloads():
    previous = schemas.strict_validation()
    schemas.set_strict_validation(True)
    yield
    schemas.set_strict_validation(previous)


def at(seconds):
    return T0 + datetime.timedelta(seconds=seconds)


def diag(temp=60.0, stale=(), down=(), *, ros_health=True, topics=True, gpu_temp=None, **host):
    """Parsed diagnostics as DiagnosticsService._parse_diagnostics returns them."""
    out = {
        "jtop": {"level": 0, "values": {"gpu_percent": 40, "cpu_temp_c": temp,
                                        "gpu_temp_c": gpu_temp, "soc_temp_c": -256,
                                        "power_total_mw": 6200, "power_avg_mw": 5800}},
        "host_stats": {"level": 0, "values": {"cpu_percent": 33.5, "ram_percent": 61.0, **host}},
        "topic_listing": {"level": 0, "values": {"topic_count": 1, "topics": {"/x": ["t"]}}},
    }
    if ros_health:
        out["ros_health"] = {"level": 0, "values": {
            f"{s}_stale": s in stale for s in ("esp32", "gps", "sati_pose")}}
    if topics:
        out["topic_availability"] = {"level": 0, "values": {
            t: {"exists": True, "publishing": t not in down} for t in ("/scan", "/odom")}}
    return out


def codes(events):
    return [(e.code, e.discriminator) for e in events]


def assert_valid(events):
    for event in events:
        build_row(event, strict=True)  # payload matches the code's model


# --- timestamps ----------------------------------------------------------------------------

class TestTimestamps:
    def test_robot_epoch_seconds(self):
        assert robot_ts(T0.timestamp() + 0.25, at(5)) == at(0.25)

    @pytest.mark.parametrize("value", [None, "abc", True, float("nan"), 1234.5, 4e12])
    def test_unusable_robot_time_falls_back_to_receive_time(self, value):
        # 1234.5 is sim time (1970), 4e12 is far in the future
        assert robot_ts(value, at(5)) == at(5)

    def test_ros_stamp(self):
        stamp = {"sec": int(T0.timestamp()), "nanosec": 500_000_000}
        assert stamp_ts(stamp, at(9)) == at(0.5)
        assert stamp_ts(None, at(9)) == at(9)
        assert stamp_ts({"sec": 12, "nanosec": 0}, at(9)) == at(9)


# --- extraction ----------------------------------------------------------------------------

class TestExtraction:
    def test_hottest_ignores_invalid_sensors(self):
        assert hottest(diag(temp=61.0, gpu_temp=70.0)) == (70.0, "gpu")
        assert hottest(diag(temp=None)) == (None, None)  # soc -256 is not a reading
        assert hottest({}) == (None, None)

    def test_node_sets(self):
        d = diag(stale=("gps",), down=("/scan",))
        assert stale_sources(d) == {"gps"}
        assert down_topics(d) == {"/scan"}

    def test_missing_or_empty_blocks_are_unknown(self):
        assert stale_sources(diag(ros_health=False)) is None
        assert down_topics(diag(topics=False)) is None
        failed = {"ros_health": {"level": 2, "values": {}},
                  "topic_availability": {"level": 2, "values": {}}}
        assert stale_sources(failed) is None and down_topics(failed) is None

    def test_diagnostics_ts_row(self):
        result = DiagnosticsDetector("r1").update(at(0), diag(temp=55.0, stale=("gps",),
                                                              down=("/scan", "/odom")))
        assert set(result.row) <= set(tables.DIAGNOSTICS_COLUMNS)
        assert result.row == {"ts": at(0), "robot_name": "r1", "cpu": 33.5, "gpu": 40.0,
                              "ram": 61.0, "temp_max": 55.0, "power_w": 6.2, "nodes_down": 3}
        # GNSS is out of scope: the gnss_* columns are never set.
        assert not any(k.startswith("gnss") for k in result.row)

    def test_row_without_blocks(self):
        result = DiagnosticsDetector("r1").update(at(0), {})
        assert result.events == []
        assert result.row == {"ts": at(0), "robot_name": "r1", "cpu": None, "gpu": None,
                              "ram": None, "temp_max": None, "power_w": None, "nodes_down": None}

    def test_latest_drops_topic_listing(self):
        latest = DiagnosticsDetector("r1").update(at(0), diag()).latest
        assert "topic_listing" not in latest["diagnostics"]
        assert latest["detectors"] == {"thermal_high": False, "sources_down": [],
                                       "topics_down": []}


# --- thermal -------------------------------------------------------------------------------

class TestThermal:
    def run(self, temps, **kwargs):
        detector = DiagnosticsDetector("r1", **kwargs)
        out = []
        for i, temp in enumerate(temps):
            events = detector.update(at(i), diag(temp=temp)).events
            assert_valid(events)
            out.append([e.code for e in events])
        return out

    def test_first_sample_is_a_silent_baseline_even_when_hot(self):
        assert self.run([95.0, 96.0]) == [[], []]

    def test_enter_is_inclusive_at_85(self):
        assert self.run([60.0, 84.9, 85.0]) == [[], [], [EventCode.SYSTEM_THERMAL_HIGH]]

    def test_dead_band_holds_then_exit_is_inclusive_at_78(self):
        assert self.run([60.0, 85.0, 80.0, 78.1, 78.0, 79.0]) == [
            [], [EventCode.SYSTEM_THERMAL_HIGH], [], [], [EventCode.SYSTEM_THERMAL_OK], []]

    def test_payload(self):
        detector = DiagnosticsDetector("r1")
        detector.update(at(0), diag(temp=60.0))
        (high,) = detector.update(at(1), diag(temp=60.0, gpu_temp=88.5)).events
        assert high.payload == {"temp_c": 88.5, "threshold_c": 85.0, "sensor": "gpu"}
        assert high.ts == at(1) and high.robot_name == "r1"
        (ok,) = detector.update(at(2), diag(temp=70.0)).events
        assert ok.payload == {"temp_c": 70.0, "threshold_c": 78.0, "sensor": "cpu"}

    def test_missing_temperature_keeps_state(self):
        assert self.run([60.0, None, 90.0]) == [[], [], [EventCode.SYSTEM_THERMAL_HIGH]]

    def test_custom_thresholds(self):
        assert self.run([60.0, 70.0], high_c=70.0, ok_c=65.0) == [
            [], [EventCode.SYSTEM_THERMAL_HIGH]]


# --- ROS nodes -----------------------------------------------------------------------------

class TestNodes:
    def test_baseline_then_down_and_up(self):
        detector = DiagnosticsDetector("r1")
        assert detector.update(at(0), diag(stale=("esp32",))).events == []
        events = detector.update(at(1), diag(stale=("esp32", "gps"), down=("/scan",))).events
        assert_valid(events)
        assert codes(events) == [(EventCode.SYSTEM_NODE_DOWN, "gps"),
                                 (EventCode.SYSTEM_NODE_DOWN, "/scan")]
        assert events[0].payload == {"node": "gps"}
        events = detector.update(at(2), diag()).events
        assert codes(events) == [(EventCode.SYSTEM_NODE_UP, "esp32"),
                                 (EventCode.SYSTEM_NODE_UP, "gps"),
                                 (EventCode.SYSTEM_NODE_UP, "/scan")]

    def test_absent_or_failed_block_does_not_report_everything_up(self):
        detector = DiagnosticsDetector("r1")
        detector.update(at(0), diag())
        detector.update(at(1), diag(stale=("gps",), down=("/scan",)))
        assert detector.update(at(2), diag(ros_health=False, topics=False)).events == []
        failed = {**diag(), "ros_health": {"level": 2, "values": {}},
                  "topic_availability": {"level": 2, "values": {}}}
        result = detector.update(at(3), failed)
        assert result.events == []
        assert result.row["nodes_down"] is None
        assert result.latest["detectors"]["sources_down"] == ["gps"]
        assert result.latest["detectors"]["topics_down"] == ["/scan"]
        assert codes(detector.update(at(4), diag()).events) == [
            (EventCode.SYSTEM_NODE_UP, "gps"), (EventCode.SYSTEM_NODE_UP, "/scan")]


# --- rehydration ---------------------------------------------------------------------------

class TestDiagnosticsRehydration:
    def test_restart_in_the_same_state_emits_nothing(self):
        before = DiagnosticsDetector("r1")
        before.update(at(0), diag(temp=60.0))
        result = before.update(at(1), diag(temp=90.0, stale=("gps",), down=("/scan",)))
        assert len(result.events) == 3
        after = DiagnosticsDetector.from_latest("r1", result.latest)
        # still hot but inside the dead band, same nodes down: nothing new happened
        assert after.update(at(2), diag(temp=80.0, stale=("gps",), down=("/scan",))).events == []
        assert codes(after.update(at(3), diag(temp=77.0)).events) == [
            (EventCode.SYSTEM_THERMAL_OK, None), (EventCode.SYSTEM_NODE_UP, "gps"),
            (EventCode.SYSTEM_NODE_UP, "/scan")]

    def test_unseeded_detector_in_the_dead_band_would_have_missed_the_exit(self):
        # Why the state is stored: rebuilt from the value alone, 80 °C reads as "not hot".
        detector = DiagnosticsDetector("r1")
        detector.update(at(0), diag(temp=80.0))
        assert detector.update(at(1), diag(temp=77.0)).events == []

    def test_change_while_down_is_reported(self):
        latest = {"detectors": {"thermal_high": False, "sources_down": [], "topics_down": []}}
        detector = DiagnosticsDetector.from_latest("r1", latest)
        assert codes(detector.update(at(0), diag(temp=90.0, stale=("gps",))).events) == [
            (EventCode.SYSTEM_THERMAL_HIGH, None), (EventCode.SYSTEM_NODE_DOWN, "gps")]

    @pytest.mark.parametrize("latest", [None, "junk", {}, {"detectors": "x"},
                                        {"detectors": {"thermal_high": "yes",
                                                       "sources_down": "gps"}}])
    def test_garbage_is_unseeded(self, latest):
        detector = DiagnosticsDetector.from_latest("r1", latest)
        assert detector.update(at(0), diag(temp=95.0, stale=("gps",))).events == []


# --- nav_supervisor ------------------------------------------------------------------------

def nav(state="DRIVE", cause="UNATTRIBUTED", blocked=False, **extra):
    return {"state": state, "attempts": 1, "last_drive_cause": cause,
            "blocked_pending": blocked, "blocked_elapsed_s": 0.0, "blocked_hold_s": 20.0,
            "goal_frame": "map", "goal_x": 1.5, "goal_y": -2.0, **extra}


class TestNavSupervisor:
    def test_recovery_entered_and_exited(self):
        detector = NavSupervisorDetector("r1")
        assert detector.update(at(0), nav()).events == []
        (entered,) = detector.update(at(1), nav("RECOVER", cause="FROZEN")).events
        assert entered.code is EventCode.NAV_RECOVERY_ENTERED
        assert entered.payload == {"cause": "FROZEN"}
        assert detector.update(at(2), nav("RECOVER", cause="FROZEN")).events == []
        (exited,) = detector.update(at(13.5), nav("DRIVE", cause="CONFINED")).events
        assert exited.code is EventCode.NAV_RECOVERY_EXITED
        assert exited.payload == {"cause": "FROZEN", "duration_s": 12.5}
        assert_valid([entered, exited])

    def test_first_sample_in_recover_is_a_baseline(self):
        detector = NavSupervisorDetector("r1")
        assert detector.update(at(0), nav("RECOVER")).events == []
        (exited,) = detector.update(at(5), nav("DRIVE")).events
        assert exited.payload == {"cause": "UNATTRIBUTED", "duration_s": None}

    def test_unknown_state_is_not_a_recovery(self):
        detector = NavSupervisorDetector("r1")
        detector.update(at(0), nav())
        assert detector.update(at(1), nav("UNKNOWN(7)")).events == []
        assert detector.update(at(2), nav()).events == []

    def test_goal_blocked_on_rising_edge_only(self):
        detector = NavSupervisorDetector("r1")
        detector.update(at(0), nav())
        (blocked,) = detector.update(at(1), nav(blocked=True, cause="NEAR_FIELD_BLOCKED")).events
        assert blocked.code is EventCode.NAV_GOAL_BLOCKED
        assert blocked.payload["cause"] == "NEAR_FIELD_BLOCKED"
        assert "hold 20s" in blocked.payload["detail"] and "(1.5, -2)" in blocked.payload["detail"]
        assert_valid([blocked])
        # the robot re-emits at up to 5 Hz while the hold runs
        assert detector.update(at(1.2), nav(blocked=True, blocked_elapsed_s=0.2)).events == []
        assert detector.update(at(2), nav(blocked=False)).events == []
        assert len(detector.update(at(3), nav(blocked=True)).events) == 1

    def test_blocked_at_first_sample_is_a_baseline(self):
        detector = NavSupervisorDetector("r1")
        assert detector.update(at(0), nav(blocked=True)).events == []

    def test_empty_or_bad_payload(self):
        detector = NavSupervisorDetector("r1")
        assert detector.update(at(0), {}).events == []
        assert detector.update(at(1), None).events == []
        assert detector.update(at(2), nav()).events == []  # still the baseline

    def test_rehydrated_recovery_is_not_re_entered_and_keeps_its_duration(self):
        before = NavSupervisorDetector("r1")
        before.update(at(0), nav())
        latest = before.update(at(10), nav("RECOVER", cause="FROZEN", blocked=True)).latest
        after = NavSupervisorDetector.from_latest("r1", latest)
        assert after.update(at(11), nav("RECOVER", cause="FROZEN", blocked=True)).events == []
        (exited,) = after.update(at(40), nav("DRIVE")).events
        assert exited.payload == {"cause": "FROZEN", "duration_s": 30.0}

    @pytest.mark.parametrize("latest", [None, [], {"detectors": None},
                                        {"detectors": {"state": 3, "blocked": "no"}}])
    def test_garbage_is_unseeded(self, latest):
        detector = NavSupervisorDetector.from_latest("r1", latest)
        assert detector.update(at(0), nav("RECOVER", blocked=True)).events == []
