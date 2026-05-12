"""
E2E tests for mission dispatch – mission scenarios.

Migrated from packages/controllers/mission/tests/mission.py (Bazel py_test).
"""
import math
import time
import unittest

import pytest

from cloud_common import objects as api_objects
from cloud_common.objects import mission as mission_object
from packages.controllers.mission.tests import client as simulator
from packages.controllers.mission.tests import mission_examples

from tests.e2e.mission_dispatcher.conftest import (
    TestContext,
    mission_from_waypoints,
    mission_object_generator,
)

DEFAULT_MISSION_X = 10.0
DEFAULT_MISSION_Y = 10.0

SCENARIO1_WAYPOINTS = [(1, 1), (10, 10), (5, 5)]

SCENARIO1_EXPECTED_STATUSES = [
    mission_object.MissionStatusV1(state="PENDING", current_node=0),
    mission_object.MissionStatusV1(state="RUNNING", current_node=0),
    mission_object.MissionStatusV1(state="RUNNING", current_node=1),
    mission_object.MissionStatusV1(state="RUNNING", current_node=2),
    mission_object.MissionStatusV1(state="COMPLETED", current_node=2),
]


@pytest.mark.e2e
class TestMissions(unittest.TestCase):
    def test_long_mission(self):
        """Test sending a very long mission to a single robot"""
        robot = simulator.RobotInit("test01", 0, 0, 0)
        with TestContext([robot]) as ctx:
            ctx.db_client.create(api_objects.RobotObjectV1(name="test01", status={}))
            time.sleep(0.25)
            ctx.db_client.create(
                mission_object_generator("test01", mission_examples.MISSION_TREE_LONG))

            for update in ctx.db_client.watch(api_objects.MissionObjectV1):
                if update.status.state == mission_object.MissionStateV1.COMPLETED:
                    break

            robot_status = ctx.db_client.get(api_objects.RobotObjectV1, "test01").status
            waypoint = mission_examples.MISSION_TREE_LONG[-1]["route"]["waypoints"][-1]
            self.assertAlmostEqual(robot_status.pose.x, waypoint["x"], places=2)
            self.assertAlmostEqual(robot_status.pose.y, waypoint["y"], places=2)

    def test_single_mission(self):
        """Test sending a single mission to a single robot"""
        robot = simulator.RobotInit("test01", 0, 0, 0)
        with TestContext([robot]) as ctx:
            ctx.db_client.create(api_objects.RobotObjectV1(name="test01", status={}))
            time.sleep(0.25)
            ctx.db_client.create(mission_from_waypoints("test01", SCENARIO1_WAYPOINTS))

            for expected_state, update in zip(
                    SCENARIO1_EXPECTED_STATUSES,
                    ctx.db_client.watch(api_objects.MissionObjectV1)):
                self.assertEqual(update.status.state, expected_state.state)
                self.assertEqual(update.status.current_node, expected_state.current_node)

            robot_status = ctx.db_client.get(api_objects.RobotObjectV1, "test01").status
            self.assertEqual(robot_status.pose.x, SCENARIO1_WAYPOINTS[-1][0])
            self.assertEqual(robot_status.pose.y, SCENARIO1_WAYPOINTS[-1][0])

    def test_robot_object_second(self):
        """Test creating a mission before the robot object exists"""
        robot = simulator.RobotInit("test01", 0, 0, 0)
        with TestContext([robot]) as ctx:
            ctx.db_client.create(mission_from_waypoints("test01", SCENARIO1_WAYPOINTS))
            time.sleep(0.25)
            ctx.db_client.create(api_objects.RobotObjectV1(name="test01", status={}))

            for expected_state, update in zip(
                    SCENARIO1_EXPECTED_STATUSES,
                    ctx.db_client.watch(api_objects.MissionObjectV1)):
                self.assertEqual(update.status.state, expected_state.state)
                self.assertEqual(update.status.current_node, expected_state.current_node)

    def test_mission_failure(self):
        """Test a sequence of 4 missions PASS, FAIL, PASS, FAIL"""
        expected_states = [
            mission_object.MissionStatusV1(state="PENDING", current_node=0),
            mission_object.MissionStatusV1(state="PENDING", current_node=0),
            mission_object.MissionStatusV1(state="PENDING", current_node=0),
            mission_object.MissionStatusV1(state="PENDING", current_node=0),
            mission_object.MissionStatusV1(state="RUNNING", current_node=0),
            mission_object.MissionStatusV1(state="COMPLETED", current_node=0),
            mission_object.MissionStatusV1(state="RUNNING", current_node=0),
            mission_object.MissionStatusV1(state="FAILED", current_node=0,
                                           failure_reason="Failure period reached"),
            mission_object.MissionStatusV1(state="RUNNING", current_node=0),
            mission_object.MissionStatusV1(state="COMPLETED", current_node=0),
            mission_object.MissionStatusV1(state="RUNNING", current_node=0),
            mission_object.MissionStatusV1(state="FAILED", current_node=0,
                                           failure_reason="Failure period reached"),
        ]
        robot = simulator.RobotInit("test01", 0, 0, 0, "map", 2)
        with TestContext([robot]) as ctx:
            watcher = ctx.db_client.watch(api_objects.MissionObjectV1)
            for i in range(4):
                mission = mission_from_waypoints(
                    "test01", [(i * 2 + 1, i * 2 + 1)], "mission_" + str(i))
                ctx.db_client.create(mission)
            ctx.db_client.create(api_objects.RobotObjectV1(name="test01", status={}))

            for expected_state, update in zip(expected_states, watcher):
                self.assertEqual(update.status.state, expected_state.state)
                self.assertEqual(update.status.current_node, expected_state.current_node)

    def test_timeout(self):
        """Test sending a mission that times out"""
        expected_statuses = [
            mission_object.MissionStatusV1(state="PENDING"),
            mission_object.MissionStatusV1(state="RUNNING"),
            mission_object.MissionStatusV1(state="FAILED",
                                           failure_reason="Mission timed out"),
        ]
        robot = simulator.RobotInit("test01", 0, 0, 0)
        with TestContext([robot]) as ctx:
            ctx.db_client.create(api_objects.RobotObjectV1(name="test01", status={}))
            time.sleep(0.25)
            watcher = ctx.db_client.watch(api_objects.MissionObjectV1)
            mission = mission_from_waypoints("test01", [(15, 15)])
            mission.timeout = 1
            ctx.db_client.create(mission)

            for expected_status, update in zip(expected_statuses, watcher):
                self.assertEqual(update.status.state, expected_status.state)
                if update.status.state == mission_object.MissionStateV1.FAILED:
                    self.assertEqual(update.status.failure_reason,
                                     expected_status.failure_reason)

    def test_mission_move_node(self):
        """Test sending a mission with move nodes"""
        from tests.e2e.mission_dispatcher.conftest import move_generator
        robot = simulator.RobotInit("test01", 1, 1, math.pi / 4)
        move_mission = [
            move_generator(move={"distance": 1}),
            move_generator(move={"rotation": math.pi / 4}),
        ]
        with TestContext([robot]) as ctx:
            ctx.db_client.create(api_objects.RobotObjectV1(name="test01", status={}))
            time.sleep(3)
            ctx.db_client.create(mission_object_generator("test01", move_mission))

            for update in ctx.db_client.watch(api_objects.MissionObjectV1):
                if update.status.state == mission_object.MissionStateV1.COMPLETED:
                    break

            updated_robot_status = ctx.db_client.get(
                api_objects.RobotObjectV1, "test01").status
            self.assertAlmostEqual(updated_robot_status.pose.x, 1.71, places=2)
            self.assertAlmostEqual(updated_robot_status.pose.y, 1.71, places=2)
            self.assertAlmostEqual(updated_robot_status.pose.theta, math.pi / 2, places=2)


if __name__ == "__main__":
    unittest.main()
