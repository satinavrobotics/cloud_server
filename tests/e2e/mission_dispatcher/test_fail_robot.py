"""
E2E tests for mission dispatch – robot failure scenarios.

Migrated from packages/controllers/mission/tests/fail_robot.py (Bazel py_test).
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

SCENARIO1_WAYPOINTS = [(1, 1), (10, 10), (5, 5)]
SCENARIO1_EXPECTED_STATUSES = [
    mission_object.MissionStatusV1(state="PENDING", current_node=0),
    mission_object.MissionStatusV1(state="RUNNING", current_node=0),
    mission_object.MissionStatusV1(state="RUNNING", current_node=1),
    mission_object.MissionStatusV1(state="RUNNING", current_node=2),
    mission_object.MissionStatusV1(state="COMPLETED", current_node=2),
]

SCENARIO2_WAYPOINTS = [(1, 1), (10, 10), (5, 5)]
SCENARIO2_EXPECTED_STATUSES = [
    mission_object.MissionStatusV1(state="PENDING", current_node=0),
    mission_object.MissionStatusV1(state="RUNNING", current_node=0),
    mission_object.MissionStatusV1(state="FAILED", current_node=0),
]


@pytest.mark.e2e
class TestMissions(unittest.TestCase):
    def test_warning_mission(self):
        """Test sending a single mission to a robot that always produces a warning"""
        robot = simulator.RobotInit("warning_robot01", 0, 0, 0, "map", 1)
        with TestContext([robot], fail_as_warning=True) as ctx:
            ctx.db_client.create(
                api_objects.RobotObjectV1(name="warning_robot01", status={}))
            time.sleep(0.25)
            ctx.db_client.create(
                mission_from_waypoints("warning_robot01", SCENARIO1_WAYPOINTS))

            for expected_state, update in zip(
                    SCENARIO1_EXPECTED_STATUSES,
                    ctx.db_client.watch(api_objects.MissionObjectV1)):
                self.assertEqual(update.status.state, expected_state.state)
                self.assertEqual(update.status.current_node, expected_state.current_node)

            robot_status = ctx.db_client.get(
                api_objects.RobotObjectV1, "warning_robot01").status
            self.assertEqual(robot_status.pose.x, SCENARIO1_WAYPOINTS[-1][0])
            self.assertEqual(robot_status.pose.y, SCENARIO1_WAYPOINTS[-1][0])

    def test_fatal_mission(self):
        """Test a single mission to a robot that always produces a fatal error"""
        robot = simulator.RobotInit("fatal_robot01", 0, 0, 0, "map", 1)
        with TestContext([robot]) as ctx:
            ctx.db_client.create(
                api_objects.RobotObjectV1(name="fatal_robot01", status={}))
            time.sleep(0.25)
            ctx.db_client.create(
                mission_from_waypoints("fatal_robot01", SCENARIO2_WAYPOINTS))

            for expected_state, update in zip(
                    SCENARIO2_EXPECTED_STATUSES,
                    ctx.db_client.watch(api_objects.MissionObjectV1)):
                self.assertEqual(update.status.state, expected_state.state)
                self.assertEqual(update.status.current_node, expected_state.current_node)


if __name__ == "__main__":
    unittest.main()
