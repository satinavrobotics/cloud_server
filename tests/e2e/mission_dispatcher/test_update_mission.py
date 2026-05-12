"""
E2E tests for mission dispatch – update mission scenarios.

Migrated from packages/controllers/mission/tests/update_mission.py (Bazel py_test).
"""
import time
import unittest

import pytest

from cloud_common import objects as api_objects
from cloud_common.objects import mission as mission_object
from cloud_common.objects import common
from packages.controllers.mission.tests import client as simulator

from tests.e2e.mission_dispatcher.conftest import (
    TestContext,
    mission_from_waypoint,
    mission_object_generator,
    route_generator,
    action_generator,
)

WAYPOINT_1 = (10, 10)
WAYPOINT_2 = (5, 5)
WAYPOINT_3 = (3, 3)

MISSION_TREE = [
    route_generator(),
    {"name": "selector_1", "selector": {}, "parent": "root"},
    action_generator(params={"should_fail": 1, "time": 3}, parent="selector_1"),
    {"name": "sequence_1", "sequence": {}, "parent": "selector_1"},
    route_generator(parent="sequence_1"),
    route_generator(parent="sequence_1"),
    route_generator(),
]


@pytest.mark.e2e
class TestUpdateMissions(unittest.TestCase):
    def test_update_pending_mission(self):
        """Test if pending mission gets updated"""
        robot = simulator.RobotInit("test01", 0, 0, 0)
        with TestContext([robot]) as ctx:
            ctx.db_client.create(api_objects.RobotObjectV1(name="test01", status={}))
            time.sleep(0.25)
            self.assertGreater(len(ctx.db_client.list(api_objects.RobotObjectV1)), 0)

            mission_1 = mission_from_waypoint("test01", WAYPOINT_1[0], WAYPOINT_1[1])
            ctx.db_client.create(mission_1)
            time.sleep(0.25)

            mission_2 = mission_from_waypoint("test01", WAYPOINT_2[0], WAYPOINT_2[1])
            ctx.db_client.create(mission_2)

            missions = ctx.db_client.list(api_objects.MissionObjectV1)
            self.assertEqual(len(missions), 2)

            update_nodes = {"0": {"waypoints": [
                {"x": WAYPOINT_3[0], "y": WAYPOINT_3[1], "theta": 0}]}}
            ctx.db_client.update_mission(mission_2.name, update_nodes)

            for mission in ctx.db_client.watch(api_objects.MissionObjectV1):
                if mission.status.state.done and mission.name == mission_2.name:
                    self.assertEqual(mission.status.state,
                                     mission_object.MissionStateV1.COMPLETED)
                    break

            robot_status = ctx.db_client.get(api_objects.RobotObjectV1, "test01").status
            self.assertAlmostEqual(robot_status.pose.x, WAYPOINT_3[0], places=2)
            self.assertAlmostEqual(robot_status.pose.y, WAYPOINT_3[1], places=2)

    def test_update_running_mission(self):
        """Test if running mission gets updated"""
        robot = simulator.RobotInit("test01", 0, 0, 0)
        with TestContext([robot]) as ctx:
            ctx.db_client.create(api_objects.RobotObjectV1(name="test01", status={}))
            time.sleep(0.25)
            self.assertGreater(len(ctx.db_client.list(api_objects.RobotObjectV1)), 0)

            mission_1 = mission_object_generator("test01", MISSION_TREE)
            ctx.db_client.create(mission_1)
            time.sleep(0.25)

            update_nodes = {"6": {"waypoints": [
                {"x": WAYPOINT_2[0], "y": WAYPOINT_2[1], "theta": 0}]}}
            ctx.db_client.update_mission(mission_1.name, update_nodes)

            for mission in ctx.db_client.watch(api_objects.MissionObjectV1):
                if mission.status.state.done and mission.name == mission_1.name:
                    self.assertEqual(mission.status.state,
                                     mission_object.MissionStateV1.COMPLETED)
                    break

            robot_status = ctx.db_client.get(api_objects.RobotObjectV1, "test01").status
            self.assertAlmostEqual(robot_status.pose.x, WAYPOINT_2[0], places=2)
            self.assertAlmostEqual(robot_status.pose.y, WAYPOINT_2[1], places=2)

    def test_update_completed_mission(self):
        """Test if completed mission update raises an error"""
        robot = simulator.RobotInit("test01", 0, 0, 0)
        with TestContext([robot]) as ctx:
            ctx.db_client.create(api_objects.RobotObjectV1(name="test01", status={}))
            time.sleep(0.25)
            self.assertGreater(len(ctx.db_client.list(api_objects.RobotObjectV1)), 0)

            mission_1 = mission_from_waypoint("test01", WAYPOINT_3[0], WAYPOINT_3[1])
            ctx.db_client.create(mission_1)
            time.sleep(0.25)

            for mission in ctx.db_client.watch(api_objects.MissionObjectV1):
                if mission.status.state.done and mission.name == mission_1.name:
                    self.assertEqual(mission.status.state,
                                     mission_object.MissionStateV1.COMPLETED)
                    break

            update_nodes = {"0": {"waypoints": [
                {"x": WAYPOINT_1[0], "y": WAYPOINT_1[1], "theta": 0}]}}
            with self.assertRaises(common.ICSUsageError):
                ctx.db_client.update_mission(mission_1.name, update_nodes)


if __name__ == "__main__":
    unittest.main()
