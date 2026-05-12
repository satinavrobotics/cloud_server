"""
E2E tests for mission dispatch – cancel mission scenarios.

Migrated from packages/controllers/mission/tests/cancel_mission.py (Bazel py_test).
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
    mission_from_waypoints,
)


@pytest.mark.e2e
class TestCancelMissions(unittest.TestCase):
    def test_cancel_pending_mission(self):
        """Test if pending mission gets canceled"""
        robot = simulator.RobotInit("test01", 0, 0, 0)
        with TestContext([robot], tick_period=1.0) as ctx:
            ctx.db_client.create(api_objects.RobotObjectV1(name="test01", status={}))
            time.sleep(0.25)
            self.assertGreater(len(ctx.db_client.list(api_objects.RobotObjectV1)), 0)

            mission_1 = mission_from_waypoint("test01", 10, 10)
            ctx.db_client.create(mission_1)
            time.sleep(0.25)

            mission_2 = mission_from_waypoint("test01", 3, 3)
            ctx.db_client.create(mission_2)

            missions = ctx.db_client.list(api_objects.MissionObjectV1)
            self.assertEqual(len(missions), 2)

            ctx.db_client.cancel_mission(mission_2.name)
            for mission in ctx.db_client.watch(api_objects.MissionObjectV1):
                if mission.status.state.done and mission.name == mission_2.name:
                    self.assertEqual(mission.status.state,
                                     mission_object.MissionStateV1.CANCELED)
                    break

    def test_delete_pending_mission(self):
        """Test if pending mission gets deleted"""
        robot = simulator.RobotInit("test01", 0, 0, 0)
        with TestContext([robot], tick_period=1.0) as ctx:
            ctx.db_client.create(api_objects.RobotObjectV1(name="test01", status={}))
            time.sleep(0.25)
            self.assertGreater(len(ctx.db_client.list(api_objects.RobotObjectV1)), 0)

            mission_1 = mission_from_waypoint("test01", 10, 10)
            ctx.db_client.create(mission_1)
            time.sleep(0.25)

            mission_2 = mission_from_waypoint("test01", 3, 3)
            ctx.db_client.create(mission_2)

            missions = ctx.db_client.list(api_objects.MissionObjectV1)
            self.assertEqual(len(missions), 2)

            ctx.db_client.delete(api_objects.MissionObjectV1, mission_2.name)
            time.sleep(10)

            missions = ctx.db_client.list(api_objects.MissionObjectV1)
            self.assertEqual(len(missions), 1)

    def test_cancel_running_mission(self):
        """Test if running mission gets canceled"""
        robot = simulator.RobotInit("test01", 0, 0, 0)
        with TestContext([robot], tick_period=1.0) as ctx:
            ctx.db_client.create(api_objects.RobotObjectV1(name="test01", status={}))
            time.sleep(0.25)
            self.assertGreater(len(ctx.db_client.list(api_objects.RobotObjectV1)), 0)

            test_mission = mission_from_waypoint("test01", 5, 5)
            ctx.db_client.create(test_mission)

            for mission in ctx.db_client.watch(api_objects.MissionObjectV1):
                if mission.status.state == mission_object.MissionStateV1.RUNNING:
                    break

            ctx.db_client.cancel_mission(test_mission.name)

            for mission in ctx.db_client.watch(api_objects.MissionObjectV1):
                if mission.status.state.done:
                    self.assertEqual(mission.status.state,
                                     mission_object.MissionStateV1.CANCELED)
                    self.assertEqual(mission.status.node_status["0"].state,
                                     mission_object.MissionStateV1.CANCELED)
                    self.assertEqual(
                        len(ctx.db_client.list(api_objects.MissionObjectV1)), 1)
                    break

    def test_delete_running_mission(self):
        """Test if running mission gets deleted after completed"""
        robot = simulator.RobotInit("test01", 0, 0, 0)
        with TestContext([robot], tick_period=1.0) as ctx:
            ctx.db_client.create(api_objects.RobotObjectV1(name="test01", status={}))
            time.sleep(0.25)

            test_mission = mission_from_waypoint("test01", 5, 5)
            ctx.db_client.create(test_mission)

            for mission in ctx.db_client.watch(api_objects.MissionObjectV1):
                if (mission.status.state == mission_object.MissionStateV1.RUNNING
                        and mission.name == test_mission.name):
                    break

            ctx.db_client.delete(api_objects.MissionObjectV1, test_mission.name)
            time.sleep(0.25)
            fetched_mission = ctx.db_client.get(
                api_objects.MissionObjectV1, test_mission.name)
            self.assertEqual(fetched_mission.lifecycle,
                             api_objects.object.ObjectLifecycleV1.PENDING_DELETE)
            self.assertEqual(len(ctx.db_client.list(api_objects.MissionObjectV1)), 1)

            for update in ctx.db_client.watch(api_objects.MissionObjectV1):
                if update.status.state.done:
                    break

            time.sleep(0.25)
            self.assertEqual(len(ctx.db_client.list(api_objects.MissionObjectV1)), 0)

    def test_skip_canceled_mission(self):
        """Test if a mission after a canceled mission gets properly executed"""
        waypoints = [(5, 5), (5, 10), (10, 5)]
        mission_names = ["m1", "m_cancel", "m3"]
        robot = simulator.RobotInit("test01", 0, 0, 0)
        with TestContext([robot]) as ctx:
            ctx.db_client.create(api_objects.RobotObjectV1(name="test01", status={}))
            time.sleep(0.25)
            self.assertGreater(len(ctx.db_client.list(api_objects.RobotObjectV1)), 0)

            for waypoint, name in zip(waypoints, mission_names):
                mission = mission_from_waypoint("test01", waypoint[0], waypoint[1], name)
                ctx.db_client.create(mission)
                if name == "m_cancel":
                    ctx.db_client.cancel_mission(name)

            completed_mission = 0
            for mission in ctx.db_client.watch(api_objects.MissionObjectV1):
                if mission.status.state.done:
                    completed_mission += 1
                    if completed_mission == 3:
                        break

            missions = ctx.db_client.list(api_objects.MissionObjectV1)
            self.assertEqual(len(missions), 3)
            for mission in missions:
                expected_state = mission_object.MissionStateV1.COMPLETED
                if mission.name == "m_cancel":
                    expected_state = mission_object.MissionStateV1.CANCELED
                self.assertEqual(mission.status.state, expected_state)

    def test_cancel_running_mission_run_new_mission(self):
        """Test if canceling a running mission will transition to running a new mission"""
        waypoints = [(10, 10), (3, 3)]
        mission_names = []
        robot = simulator.RobotInit("test01", 0, 0, 0)
        with TestContext([robot], tick_period=0.5) as ctx:
            ctx.db_client.create(api_objects.RobotObjectV1(name="test01", status={}))
            time.sleep(0.25)
            self.assertGreater(len(ctx.db_client.list(api_objects.RobotObjectV1)), 0)

            for waypoint in waypoints:
                mission = mission_from_waypoint("test01", waypoint[0], waypoint[1])
                ctx.db_client.create(mission)
                mission_names.append(mission.name)
                time.sleep(0.25)

            for mission in ctx.db_client.watch(api_objects.MissionObjectV1):
                if (mission.status.state == mission_object.MissionStateV1.RUNNING
                        and mission.name == mission_names[0]):
                    break

            ctx.db_client.cancel_mission(mission_names[0])
            finished_mission = 0
            for mission in ctx.db_client.watch(api_objects.MissionObjectV1):
                if mission.status.state.done:
                    finished_mission += 1
                    if finished_mission == 2:
                        break

            missions = ctx.db_client.list(api_objects.MissionObjectV1)
            self.assertEqual(len(missions), 2)
            idx = 0 if missions[0].name == mission_names[0] else 1
            self.assertEqual(missions[idx].status.state,
                             mission_object.MissionStateV1.CANCELED)
            self.assertEqual(missions[1 - idx].status.state,
                             mission_object.MissionStateV1.COMPLETED)

    def test_delete_completed_mission(self):
        """Test if a completed mission gets deleted"""
        robot = simulator.RobotInit("test01", 0, 0, 0)
        with TestContext([robot]) as ctx:
            ctx.db_client.create(api_objects.RobotObjectV1(name="test01", status={}))
            time.sleep(0.25)

            test_mission = mission_from_waypoint("test01", 1, 1)
            ctx.db_client.create(test_mission)

            for mission in ctx.db_client.watch(api_objects.MissionObjectV1):
                if mission.status.state.done and mission.name == test_mission.name:
                    break

            ctx.db_client.delete(api_objects.MissionObjectV1, test_mission.name)
            time.sleep(0.25)
            self.assertEqual(len(ctx.db_client.list(api_objects.MissionObjectV1)), 0)

    def test_cancel_completed_mission(self):
        """Test if a completed mission can be canceled"""
        robot = simulator.RobotInit("test01", 0, 0, 0)
        with TestContext([robot]) as ctx:
            ctx.db_client.create(api_objects.RobotObjectV1(name="test01", status={}))
            time.sleep(0.25)
            self.assertGreater(len(ctx.db_client.list(api_objects.RobotObjectV1)), 0)

            test_mission = mission_from_waypoint("test01", 1, 1)
            ctx.db_client.create(test_mission)

            for mission in ctx.db_client.watch(api_objects.MissionObjectV1):
                if mission.status.state.done:
                    break

            with self.assertRaises(common.ICSUsageError):
                ctx.db_client.cancel_mission(test_mission.name)


if __name__ == "__main__":
    unittest.main()
