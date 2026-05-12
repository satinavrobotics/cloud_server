"""
E2E tests for mission dispatch – server resilience (restart, MQTT reconnection).

Migrated from packages/controllers/mission/tests/server.py (Bazel py_test).

Note: test_restart_from_database and test_mqtt_reconnection verify the service
handles reconnection gracefully. In the docker-compose environment, restart_mission_server()
and restart_mqtt_server() wait for automatic reconnection rather than killing containers.
"""
import time
import unittest

import pytest

from cloud_common import objects as api_objects
from cloud_common.objects import mission as mission_object
from packages.controllers.mission.tests import client as simulator

from tests.e2e.mission_dispatcher.conftest import (
    TestContext,
    mission_from_waypoints,
)

SCENARIO1_WAYPOINTS = [(1, 1), (5, 5)]
SCENARIO1_EXPECTED_STATUSES = [
    mission_object.MissionStatusV1(state="PENDING", current_node=0),
    mission_object.MissionStatusV1(state="RUNNING", current_node=0),
    mission_object.MissionStatusV1(state="RUNNING", current_node=1),
    mission_object.MissionStatusV1(state="COMPLETED", current_node=1),
]


@pytest.mark.e2e
class TestMissionServer(unittest.TestCase):
    def test_client_update_freq(self):
        """Test a mission with different update frequencies of the client simulator"""
        tick_periods = [1, 0.1, 0.01]
        for tick_period in tick_periods:
            robot = simulator.RobotInit("test01", 0, 0, 0)
            with TestContext([robot], tick_period=tick_period) as ctx:
                ctx.db_client.create(api_objects.RobotObjectV1(name="test01", status={}))
                time.sleep(0.25)
                ctx.db_client.create(
                    mission_from_waypoints("test01", SCENARIO1_WAYPOINTS))

                for expected_state, update in zip(
                        SCENARIO1_EXPECTED_STATUSES,
                        ctx.db_client.watch(api_objects.MissionObjectV1)):
                    self.assertEqual(update.status.state, expected_state.state)
                    self.assertEqual(update.status.current_node,
                                     expected_state.current_node)

    def test_restart_from_database(self):
        """Test if MD can restart from the database (resilience check)"""
        robot = simulator.RobotInit("test01", 0, 0, 0)
        restart_once = False
        with TestContext([robot]) as ctx:
            ctx.db_client.create(api_objects.RobotObjectV1(name="test01", status={}))
            time.sleep(0.25)
            ctx.db_client.create(
                mission_from_waypoints("test01", SCENARIO1_WAYPOINTS))

            completed = False
            watcher = ctx.db_client.watch(api_objects.MissionObjectV1)
            for update in watcher:
                if not restart_once and update.status.state == "RUNNING":
                    ctx.restart_mission_server()
                    restart_once = True
                    continue
                if update.status.state == mission_object.MissionStateV1.COMPLETED:
                    completed = True
                    break
            self.assertTrue(completed)

    def test_mqtt_reconnection(self):
        """Test if MD is able to handle MQTT reconnection"""
        robot = simulator.RobotInit("test01", 0, 0, 0)
        restart_once = False
        with TestContext([robot]) as ctx:
            ctx.db_client.create(api_objects.RobotObjectV1(name="test01", status={}))
            time.sleep(0.25)
            ctx.db_client.create(
                mission_from_waypoints("test01", SCENARIO1_WAYPOINTS))

            completed = False
            watcher = ctx.db_client.watch(api_objects.MissionObjectV1)
            for update in watcher:
                if not restart_once and update.status.state == "RUNNING":
                    ctx.restart_mqtt_server()
                    restart_once = True
                    continue
                if update.status.state == mission_object.MissionStateV1.COMPLETED:
                    completed = True
                    break
            self.assertTrue(completed)


if __name__ == "__main__":
    unittest.main()
