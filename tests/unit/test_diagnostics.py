"""
Unit tests for DiagnosticsService — MQTT topic parsing, tolerant payload
parsing of the 4 fixed collectors (jtop/host_stats/ros_health/topic_availability),
the in-memory cache, and broadcasting onto the existing "robot_status" WebSocket
bucket.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from packages.api.diagnostics import (
    DiagnosticsService,
    LEVEL_ERROR,
    LEVEL_OK,
    LEVEL_WARN,
    _BT_STATE_TOPIC_RE,
    _BT_TREE_TOPIC_RE,
    _TOPIC_RE,
)

BT_TREE_PAYLOAD = {
    "trees": {
        "nav_to_pose": {"filename": "navigate_to_pose_w_replanning.xml", "xml": "<root>...</root>"},
        "nav_through_poses": {"filename": "navigate_through_poses_w_replanning.xml", "xml": "<root>...</root>"},
    }
}

BT_STATE_PAYLOAD = {
    "nodes": [{"name": "NavigateRecovery", "status": "RUNNING"}],
    "stamp": {"sec": 123, "nanosec": 456},
}

JTOP_BLOCK = {
    "gpu_percent": 42,
    "cpu_temp_c": 51.5,
    "gpu_temp_c": 48.0,
    "soc_temp_c": 45.2,
    "power_total_mw": 6200,
    "power_avg_mw": 5800,
}

HOST_STATS_BLOCK = {
    "cpu_percent": 33.4,
    "ram_percent": 61.2,
    "ram_used_mb": 4820,
    "ram_total_mb": 7861,
    "swap_percent": 0.0,
    "disk_free_gb": 28.4,
    "disk_percent": 55.1,
    "uptime_sec": 183940.2,
    "net": {
        "wlan0": {"tx_bytes_per_sec": 1024.3, "rx_bytes_per_sec": 20481.9},
    },
}

HEALTHY_ROS_HEALTH_BLOCK = {
    "esp32_stale": False,
    "esp32_age_sec": 2.1,
    "esp32_level": 0,
    "esp32_message": "nominal",
    "gps_stale": False,
    "gps_age_sec": 1.8,
    "gps": {"fix": "NARROW_INT"},
    "sati_pose_stale": False,
    "sati_pose_age_sec": 0.9,
    "sati_pose_is_healthy": True,
    "sati_pose_status_message": "ok",
    "sati_pose_gnss_quality": "RTK_FIXED",
}

HEALTHY_TOPIC_AVAILABILITY_BLOCK = {
    "/scan": {"exists": True, "publisher_count": 1, "publishing": True, "age_sec": 0.3},
    "/odom": {"exists": True, "publisher_count": 1, "publishing": True, "age_sec": 0.1},
}

FULL_PAYLOAD = {
    "timestamp": 1751364000.123456,
    "robot_name": "Pincer01",
    "jtop": JTOP_BLOCK,
    "host_stats": HOST_STATS_BLOCK,
    "ros_health": HEALTHY_ROS_HEALTH_BLOCK,
    "topic_availability": HEALTHY_TOPIC_AVAILABILITY_BLOCK,
}


def make_service():
    ws_manager = MagicMock()
    ws_manager.broadcast = AsyncMock()
    return DiagnosticsService(mqtt_host="localhost", mqtt_port=1883, mqtt_keepalive=60, ws_manager=ws_manager), ws_manager


@pytest.mark.unit
class TestTopicRegex:
    def test_extracts_robot_name(self):
        match = _TOPIC_RE.match("Pincer01/diagnostics")
        assert match is not None
        assert match.group(1) == "Pincer01"

    def test_rejects_extra_segments(self):
        assert _TOPIC_RE.match("uagv/v2/RobotCompany/Pincer01/diagnostics").group(1) == "uagv/v2/RobotCompany/Pincer01"

    def test_rejects_non_diagnostics_topic(self):
        assert _TOPIC_RE.match("Pincer01/state") is None


@pytest.mark.unit
class TestParseDiagnostics:
    def test_full_payload_all_collectors_present(self):
        result = DiagnosticsService._parse_diagnostics(FULL_PAYLOAD)
        assert set(result.keys()) == {"jtop", "host_stats", "ros_health", "topic_availability"}
        assert result["jtop"] == {"level": LEVEL_OK, "values": JTOP_BLOCK}
        assert result["host_stats"] == {"level": LEVEL_OK, "values": HOST_STATS_BLOCK}
        assert result["ros_health"]["level"] == LEVEL_OK
        assert result["topic_availability"] == {"level": LEVEL_OK, "values": HEALTHY_TOPIC_AVAILABILITY_BLOCK}

    def test_missing_collector_is_absent_not_defaulted_ok(self):
        payload = {k: v for k, v in FULL_PAYLOAD.items() if k != "host_stats"}
        result = DiagnosticsService._parse_diagnostics(payload)
        assert "host_stats" not in result
        assert "jtop" in result and "ros_health" in result

    def test_topic_availability_absent_when_not_configured(self):
        payload = {k: v for k, v in FULL_PAYLOAD.items() if k != "topic_availability"}
        result = DiagnosticsService._parse_diagnostics(payload)
        assert "topic_availability" not in result

    def test_non_dict_collector_block_ignored(self):
        payload = {**FULL_PAYLOAD, "jtop": None}
        result = DiagnosticsService._parse_diagnostics(payload)
        assert "jtop" not in result

    def test_empty_payload_yields_empty_diagnostics(self):
        assert DiagnosticsService._parse_diagnostics({}) == {}


@pytest.mark.unit
class TestDeriveLevel:
    def test_jtop_and_host_stats_always_ok_when_present(self):
        assert DiagnosticsService._derive_level("jtop", JTOP_BLOCK) == LEVEL_OK
        assert DiagnosticsService._derive_level("host_stats", HOST_STATS_BLOCK) == LEVEL_OK

    def test_ros_health_ok_when_all_sources_healthy(self):
        assert DiagnosticsService._derive_level("ros_health", HEALTHY_ROS_HEALTH_BLOCK) == LEVEL_OK

    @pytest.mark.parametrize("stale_field", ["esp32_stale", "gps_stale", "sati_pose_stale"])
    def test_ros_health_warns_on_any_stale_source(self, stale_field):
        block = {**HEALTHY_ROS_HEALTH_BLOCK, stale_field: True}
        assert DiagnosticsService._derive_level("ros_health", block) == LEVEL_WARN

    def test_ros_health_warns_on_unhealthy_pose(self):
        block = {**HEALTHY_ROS_HEALTH_BLOCK, "sati_pose_is_healthy": False}
        assert DiagnosticsService._derive_level("ros_health", block) == LEVEL_WARN

    def test_ros_health_passes_through_esp32_error_level(self):
        block = {**HEALTHY_ROS_HEALTH_BLOCK, "esp32_level": LEVEL_ERROR}
        assert DiagnosticsService._derive_level("ros_health", block) == LEVEL_ERROR

    def test_topic_availability_ok_when_all_topics_publishing(self):
        assert DiagnosticsService._derive_level("topic_availability", HEALTHY_TOPIC_AVAILABILITY_BLOCK) == LEVEL_OK

    def test_topic_availability_warns_on_missing_topic(self):
        block = {
            **HEALTHY_TOPIC_AVAILABILITY_BLOCK,
            "/behavior_tree_log": {"exists": False, "publisher_count": 0, "publishing": False, "age_sec": None},
        }
        assert DiagnosticsService._derive_level("topic_availability", block) == LEVEL_WARN

    def test_topic_availability_warns_on_stale_topic(self):
        block = {
            **HEALTHY_TOPIC_AVAILABILITY_BLOCK,
            "/odom": {"exists": True, "publisher_count": 1, "publishing": False, "age_sec": 14.2},
        }
        assert DiagnosticsService._derive_level("topic_availability", block) == LEVEL_WARN

    def test_topic_availability_never_errors(self):
        block = {"/scan": {"exists": False, "publisher_count": 0, "publishing": False, "age_sec": None}}
        assert DiagnosticsService._derive_level("topic_availability", block) == LEVEL_WARN

    def test_topic_availability_ok_when_empty(self):
        assert DiagnosticsService._derive_level("topic_availability", {}) == LEVEL_OK


@pytest.mark.unit
class TestHandleDiagnostics:
    @pytest.mark.asyncio
    async def test_caches_and_broadcasts_envelope(self):
        service, ws_manager = make_service()
        diagnostics = DiagnosticsService._parse_diagnostics(FULL_PAYLOAD)

        await service._handle_diagnostics("Pincer01", FULL_PAYLOAD["timestamp"], diagnostics)

        cached = service.get_cached("Pincer01")
        assert cached is not None
        assert cached["type"] == "diagnostics_update"
        assert cached["robot_name"] == "Pincer01"
        assert cached["robot_timestamp"] == FULL_PAYLOAD["timestamp"]
        assert cached["diagnostics"] == diagnostics
        assert "timestamp" in cached  # server receive time, distinct from robot_timestamp

        ws_manager.broadcast.assert_awaited_once_with("robot_status", "Pincer01", cached)

    @pytest.mark.asyncio
    async def test_get_cached_returns_none_before_first_message(self):
        service, _ = make_service()
        assert service.get_cached("NeverSeenRobot") is None

    @pytest.mark.asyncio
    async def test_broadcast_failure_does_not_raise(self):
        service, ws_manager = make_service()
        ws_manager.broadcast.side_effect = RuntimeError("socket closed")

        # Should not raise even though broadcast fails.
        await service._handle_diagnostics("Pincer01", None, {})
        assert service.get_cached("Pincer01") is not None


@pytest.mark.unit
class TestOnDiagnosticsMessage:
    @pytest.fixture
    def loop(self):
        loop = asyncio.new_event_loop()
        yield loop
        loop.close()

    def test_dispatches_parsed_message_to_event_loop(self, loop):
        service, ws_manager = make_service()
        service.set_event_loop(loop)

        msg = MagicMock()
        msg.topic = "Pincer01/diagnostics"
        msg.payload = b'{"jtop": {"gpu_percent": 1}}'

        service._on_diagnostics_message(None, None, msg)
        loop.run_until_complete(asyncio.sleep(0.05))

        cached = service.get_cached("Pincer01")
        assert cached is not None
        assert cached["diagnostics"]["jtop"]["values"] == {"gpu_percent": 1}

    def test_ignores_non_diagnostics_topic(self, loop):
        service, _ = make_service()
        service.set_event_loop(loop)

        msg = MagicMock()
        msg.topic = "Pincer01/state"
        msg.payload = b"{}"

        service._on_diagnostics_message(None, None, msg)
        assert service.get_cached("Pincer01") is None

    def test_malformed_json_does_not_raise(self, loop):
        service, _ = make_service()
        service.set_event_loop(loop)

        msg = MagicMock()
        msg.topic = "Pincer01/diagnostics"
        msg.payload = b"not json"

        service._on_diagnostics_message(None, None, msg)  # must not raise
        assert service.get_cached("Pincer01") is None


@pytest.mark.unit
class TestBtTopicRegex:
    def test_extracts_robot_name_from_tree_topic(self):
        assert _BT_TREE_TOPIC_RE.match("Pincer01/nav2_bt_tree").group(1) == "Pincer01"

    def test_extracts_robot_name_from_state_topic(self):
        assert _BT_STATE_TOPIC_RE.match("Pincer01/nav2_bt_state").group(1) == "Pincer01"

    def test_rejects_non_bt_topic(self):
        assert _BT_TREE_TOPIC_RE.match("Pincer01/diagnostics") is None
        assert _BT_STATE_TOPIC_RE.match("Pincer01/diagnostics") is None


@pytest.mark.unit
class TestHandleBtTree:
    @pytest.mark.asyncio
    async def test_caches_and_broadcasts_envelope(self):
        service, ws_manager = make_service()

        await service._handle_bt_tree("Pincer01", BT_TREE_PAYLOAD["trees"])

        cached = service.get_cached_bt_tree("Pincer01")
        assert cached is not None
        assert cached["type"] == "nav2_bt_tree_update"
        assert cached["robot_name"] == "Pincer01"
        assert cached["trees"] == BT_TREE_PAYLOAD["trees"]

        ws_manager.broadcast.assert_awaited_once_with("robot_status", "Pincer01", cached)

    @pytest.mark.asyncio
    async def test_get_cached_bt_tree_returns_none_before_first_message(self):
        service, _ = make_service()
        assert service.get_cached_bt_tree("NeverSeenRobot") is None

    @pytest.mark.asyncio
    async def test_broadcast_failure_does_not_raise(self):
        service, ws_manager = make_service()
        ws_manager.broadcast.side_effect = RuntimeError("socket closed")

        await service._handle_bt_tree("Pincer01", None)
        assert service.get_cached_bt_tree("Pincer01") is not None


@pytest.mark.unit
class TestHandleBtState:
    @pytest.mark.asyncio
    async def test_caches_and_broadcasts_envelope(self):
        service, ws_manager = make_service()

        await service._handle_bt_state("Pincer01", BT_STATE_PAYLOAD["nodes"], BT_STATE_PAYLOAD["stamp"])

        cached = service.get_cached_bt_state("Pincer01")
        assert cached is not None
        assert cached["type"] == "nav2_bt_state_update"
        assert cached["robot_name"] == "Pincer01"
        assert cached["nodes"] == BT_STATE_PAYLOAD["nodes"]
        assert cached["robot_stamp"] == BT_STATE_PAYLOAD["stamp"]

        ws_manager.broadcast.assert_awaited_once_with("robot_status", "Pincer01", cached)

    @pytest.mark.asyncio
    async def test_get_cached_bt_state_returns_none_before_first_message(self):
        service, _ = make_service()
        assert service.get_cached_bt_state("NeverSeenRobot") is None


@pytest.mark.unit
class TestOnBtTreeMessage:
    @pytest.fixture
    def loop(self):
        loop = asyncio.new_event_loop()
        yield loop
        loop.close()

    def test_dispatches_parsed_message_to_event_loop(self, loop):
        service, _ = make_service()
        service.set_event_loop(loop)

        msg = MagicMock()
        msg.topic = "Pincer01/nav2_bt_tree"
        msg.payload = b'{"trees": {"nav_to_pose": {"filename": "a.xml", "xml": "<root/>"}}}'

        service._on_bt_tree_message(None, None, msg)
        loop.run_until_complete(asyncio.sleep(0.05))

        cached = service.get_cached_bt_tree("Pincer01")
        assert cached is not None
        assert cached["trees"]["nav_to_pose"]["filename"] == "a.xml"

    def test_ignores_non_bt_tree_topic(self, loop):
        service, _ = make_service()
        service.set_event_loop(loop)

        msg = MagicMock()
        msg.topic = "Pincer01/nav2_bt_state"
        msg.payload = b"{}"

        service._on_bt_tree_message(None, None, msg)
        assert service.get_cached_bt_tree("Pincer01") is None

    def test_malformed_json_does_not_raise(self, loop):
        service, _ = make_service()
        service.set_event_loop(loop)

        msg = MagicMock()
        msg.topic = "Pincer01/nav2_bt_tree"
        msg.payload = b"not json"

        service._on_bt_tree_message(None, None, msg)  # must not raise
        assert service.get_cached_bt_tree("Pincer01") is None


@pytest.mark.unit
class TestOnBtStateMessage:
    @pytest.fixture
    def loop(self):
        loop = asyncio.new_event_loop()
        yield loop
        loop.close()

    def test_dispatches_parsed_message_to_event_loop(self, loop):
        service, _ = make_service()
        service.set_event_loop(loop)

        msg = MagicMock()
        msg.topic = "Pincer01/nav2_bt_state"
        msg.payload = b'{"nodes": [{"name": "Root", "status": "RUNNING"}], "stamp": {"sec": 1, "nanosec": 2}}'

        service._on_bt_state_message(None, None, msg)
        loop.run_until_complete(asyncio.sleep(0.05))

        cached = service.get_cached_bt_state("Pincer01")
        assert cached is not None
        assert cached["nodes"] == [{"name": "Root", "status": "RUNNING"}]
        assert cached["robot_stamp"] == {"sec": 1, "nanosec": 2}

    def test_ignores_non_bt_state_topic(self, loop):
        service, _ = make_service()
        service.set_event_loop(loop)

        msg = MagicMock()
        msg.topic = "Pincer01/nav2_bt_tree"
        msg.payload = b"{}"

        service._on_bt_state_message(None, None, msg)
        assert service.get_cached_bt_state("Pincer01") is None

    def test_malformed_json_does_not_raise(self, loop):
        service, _ = make_service()
        service.set_event_loop(loop)

        msg = MagicMock()
        msg.topic = "Pincer01/nav2_bt_state"
        msg.payload = b"not json"

        service._on_bt_state_message(None, None, msg)  # must not raise
        assert service.get_cached_bt_state("Pincer01") is None
