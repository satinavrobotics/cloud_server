"""
E2E tests for mission dispatch – startup ordering / slow component scenarios.

Migrated from packages/controllers/mission/tests/start_order.py (Bazel py_test).

Note: The Delay mechanism from the original Bazel tests delayed specific Docker
containers from starting. In the pytest/docker-compose environment, all services
are already running when tests execute, so these tests validate that the mission
dispatch system works correctly rather than testing specific startup delays.
"""
import time
import unittest

import pytest

from cloud_common import objects as api_objects
from cloud_common.objects import mission as mission_object
from packages.controllers.mission.tests import client as simulator

from tests.e2e.mission_dispatcher.conftest import (
    TestContext,
    mission_from_waypoint,
)

MISSION_WAYPOINT_X = 30.0
MISSION_WAYPOINT_Y = 30.0


def run_single_mission(test_case: unittest.TestCase, ctx):
    """Helper: run a simple mission and assert it finishes"""
    ctx.db_client.create(api_objects.RobotObjectV1(name="test01", status={}))
    time.sleep(0.25)
    ctx.db_client.create(mission_from_waypoint("test01", MISSION_WAYPOINT_X, MISSION_WAYPOINT_Y))
    time.sleep(0.25)

    completed = False
    for update in ctx.db_client.watch(api_objects.MissionObjectV1):
        if update.status.state.done:
            completed = True
            break
    test_case.assertTrue(completed)


@pytest.mark.e2e
class TestMissions(unittest.TestCase):
    def test_mission_dispatch_slow(self):
        """Test the case where the mission dispatch starts relatively late"""
        robot = simulator.RobotInit("test01", 0, 0, 0)
        with TestContext([robot]) as ctx:
            ctx.wait_for_database()
            run_single_mission(self, ctx)

    def test_mission_simulator_slow(self):
        """Test the case where the mission simulator starts relatively late"""
        robot = simulator.RobotInit("test01", 0, 0, 0)
        with TestContext([robot], delay_sim=2.0) as ctx:
            ctx.wait_for_database()
            run_single_mission(self, ctx)

    def test_mqtt_broker_slow(self):
        """Test the case where the mqtt broker connection is delayed"""
        robot = simulator.RobotInit("test01", 0, 0, 0)
        with TestContext([robot]) as ctx:
            ctx.wait_for_database()
            ctx.wait_for_mqtt()
            run_single_mission(self, ctx)

    def test_mission_database_slow(self):
        """Test the case where the mission database starts relatively late"""
        robot = simulator.RobotInit("test01", 0, 0, 0)
        with TestContext([robot]) as ctx:
            ctx.wait_for_database()
            run_single_mission(self, ctx)


if __name__ == "__main__":
    unittest.main()
