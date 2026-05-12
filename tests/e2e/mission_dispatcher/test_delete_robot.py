"""
E2E tests for mission dispatch – delete robot scenarios.

Migrated from packages/controllers/mission/tests/delete_robot.py (Bazel py_test).
"""
import time
import unittest

import pytest

from cloud_common import objects as api_objects
from cloud_common.objects import mission as mission_object
from cloud_common.objects import robot as robot_object
from packages.controllers.mission.tests import client as simulator

from tests.e2e.mission_dispatcher.conftest import (
    TestContext,
    mission_from_waypoint,
)


@pytest.mark.e2e
class TestDeleteRobot(unittest.TestCase):
    def test_delete_idle_robot(self):
        """Test if an idle robot is correctly deleted"""
        robot = simulator.RobotInit("test01", 0, 0, 0)
        with TestContext([robot]) as ctx:
            ctx.db_client.create(api_objects.RobotObjectV1(name="test01", status={}))
            time.sleep(0.25)
            self.assertGreater(len(ctx.db_client.list(api_objects.RobotObjectV1)), 0)

            ctx.db_client.delete(api_objects.RobotObjectV1, "test01")
            time.sleep(10)

            self.assertEqual(len(ctx.db_client.list(api_objects.RobotObjectV1)), 0)

    def test_delete_on_task_robot(self):
        """Test if the server kills the robot correctly when executing a mission"""
        robot = simulator.RobotInit("test01", 0, 0, 0)
        with TestContext([robot], tick_period=1.0) as ctx:
            ctx.db_client.create(api_objects.RobotObjectV1(name="test01", status={}))
            time.sleep(0.25)
            self.assertGreater(len(ctx.db_client.list(api_objects.RobotObjectV1)), 0)

            mission = mission_from_waypoint("test01", 50, 50)
            ctx.db_client.create(mission)

            for update in ctx.db_client.watch(api_objects.RobotObjectV1):
                if update.status.state == robot_object.RobotStateV1.ON_TASK:
                    break

            ctx.db_client.delete(api_objects.RobotObjectV1, "test01")
            robot_objects = ctx.db_client.list(api_objects.RobotObjectV1)

            self.assertGreater(len(robot_objects), 0)
            self.assertEqual(robot_objects[0].lifecycle,
                             api_objects.object.ObjectLifecycleV1.PENDING_DELETE)

            for update in ctx.db_client.watch(api_objects.MissionObjectV1):
                if update.status.state.done:
                    self.assertEqual(update.status.state,
                                     mission_object.MissionStateV1.FAILED)
                    break
            time.sleep(1)

            robot_objects = ctx.db_client.list(api_objects.RobotObjectV1)
            self.assertEqual(len(robot_objects), 0)


if __name__ == "__main__":
    unittest.main()
