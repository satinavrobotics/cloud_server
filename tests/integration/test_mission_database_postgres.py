"""
Integration tests for the Mission Database PostgreSQL backend.

Migrated from packages/database/tests/test_base.py (Bazel unittest).
Tests the full CRUD lifecycle, watch endpoint, and query filtering via
the mission-database HTTP service.

Requires:
  - postgres-test container (via docker-compose.test.yaml)
  - mission-database-test container (via docker-compose.test.yaml)

Run with:
  docker compose -f docker_compose/docker-compose.test.yaml up -d postgres-test mission-database-test
  pytest tests/integration/test_mission_database_postgres.py -v -m integration
"""

import datetime
import os
import time
import unittest
from typing import Any, Dict

import pytest

from tests.sync_db_client import SyncDatabaseClient
import cloud_common.objects as api_objects
from cloud_common.objects.robot import RobotStateV1, RobotTypeV1
from cloud_common.objects.mission import MissionStateV1, MissionNodeV1
from cloud_common.objects.detection_results import DetectedObject, DetectedObjectBoundingBox2D

# Label used to demonstrate spec modification
DEFAULT_LABEL = "test1"

# URL for the mission database service (from environment or compose default)
MISSION_DATABASE_URL = os.getenv("MISSION_DATABASE_URL", "http://localhost:5000")
MISSION_DATABASE_CTRL_URL = os.getenv("MISSION_DATABASE_CTRL_URL", "http://localhost:5001")


@pytest.fixture(scope="module")
def db_client():
    """Wait for the mission database service and return a user-level DatabaseClient."""
    client = SyncDatabaseClient()
    for _ in range(60):
        if client.is_running(timeout=2):
            return client
        time.sleep(1)
    pytest.skip("Mission Database service not available")


@pytest.fixture(scope="module")
def db_controller_client():
    """Return a controller-level DatabaseClient (controller port)."""
    return SyncDatabaseClient()


@pytest.mark.integration
class TestMissionDatabase(unittest.TestCase):
    """
    Base test class for mission database integration tests.

    These are preserved as unittest.TestCase methods since pytest natively
    supports running them, and the assertion style is already established.
    """

    @classmethod
    def setUpClass(cls):
        """Wait for service readiness before running tests."""
        cls.client = SyncDatabaseClient()
        cls.controller_client = SyncDatabaseClient()
        for _ in range(60):
            if cls.client.is_running(timeout=2):
                return
            time.sleep(1)
        raise RuntimeError("Mission Database service not available for integration tests")

    def tearDown(self):
        """Clean up any lingering robots/missions/detections after each test."""
        for obj_type in [api_objects.RobotObjectV1, api_objects.MissionObjectV1, api_objects.DetectionResultsObjectV1]:
            try:
                items = self.client.list(obj_type)
                for item in items:
                    try:
                        self.controller_client.delete(obj_type, item.name)
                    except Exception:
                        pass
            except Exception:
                pass

    def test_insert_fetch(self):
        """Insert and fetch robots, verifying counts and equality."""
        robots = [api_objects.RobotObjectV1(status={}, name=("carter0" + str(i)))
                  for i in range(0, 10)]
        inserted_robots = []

        all_robots = self.client.list(api_objects.RobotObjectV1)
        self.assertCountEqual(all_robots, [])

        while robots:
            new_robot = robots.pop()
            inserted_robots.append(new_robot)
            self.client.create(new_robot)
            all_robots = self.client.list(api_objects.RobotObjectV1)
            self.assertCountEqual(all_robots, inserted_robots)

        robot0 = inserted_robots[0]
        robot1 = inserted_robots[1]
        robot0_from_db = self.client.get(api_objects.RobotObjectV1, robot0.name)
        robot1_from_db = self.client.get(api_objects.RobotObjectV1, robot1.name)
        self.assertEqual(robot0, robot0_from_db)
        self.assertNotEqual(robot0, robot1_from_db)
        self.assertEqual(robot1, robot1_from_db)
        self.assertNotEqual(robot1, robot0_from_db)

    def test_update_spec(self):
        """Update spec of a robot and verify the change is persisted."""
        robot0 = api_objects.RobotObjectV1(status={}, name="carter00")
        robot1 = api_objects.RobotObjectV1(status={}, name="carter01")
        self.client.create(robot0)
        self.client.create(robot1)

        robot0.labels.append(DEFAULT_LABEL)
        robot1.status.pose.x = 1
        self.client.update_spec(robot0)
        self.client.update_spec(robot1)

        robot0_from_db = self.client.get(api_objects.RobotObjectV1, robot0.name)
        robot1_from_db = self.client.get(api_objects.RobotObjectV1, robot1.name)
        self.assertEqual(robot0, robot0_from_db)
        self.assertEqual(robot1.name, robot1_from_db.name)
        self.assertEqual(robot1.labels, robot1_from_db.labels)
        self.assertNotEqual(robot1.status, robot1_from_db.status)

    def test_update_status(self):
        """Update status via controller and verify through user client."""
        robot0 = api_objects.RobotObjectV1(status={}, name="carter00")
        robot1 = api_objects.RobotObjectV1(status={}, name="carter01")
        self.client.create(robot0)
        self.client.create(robot1)

        robot0.labels.append(DEFAULT_LABEL)
        robot1.status.pose.x = 1
        self.controller_client.update_status(robot0)
        self.controller_client.update_status(robot1)

        robot0_from_db = self.client.get(api_objects.RobotObjectV1, robot0.name)
        robot1_from_db = self.client.get(api_objects.RobotObjectV1, robot1.name)
        self.assertEqual(robot1, robot1_from_db)
        self.assertEqual(robot0.name, robot0_from_db.name)
        self.assertEqual(robot0.status, robot0_from_db.status)
        self.assertNotEqual(robot0.labels, robot0_from_db.labels)



    def _setup_carter_robots(self):
        robots = [api_objects.RobotObjectV1(status={}, name="carter0" + str(i))
                  for i in range(0, 10)]
        for robot in robots:
            self.client.create(robot)
        for i, robot in enumerate(robots):
            robot.status.battery_level = i * 10
            robot.status.state = RobotStateV1.IDLE if i % 2 == 0 else RobotStateV1.ON_TASK
            robot.status.factsheet.agv_class = RobotTypeV1.AMR.value
            robot.status.online = i % 4 <= 1
        for robot in robots:
            self.controller_client.update_status(robot)
        return robots

    def _setup_carter_and_arm_robots(self):
        robots = [api_objects.RobotObjectV1(status={}, name="arm01"),
                  api_objects.RobotObjectV1(status={}, name="carter01")]
        for robot in robots:
            self.client.create(robot)
        robots[0].status.factsheet.agv_class = RobotTypeV1.ARM.value
        robots[1].status.factsheet.agv_class = RobotTypeV1.AMR.value
        for robot in robots:
            self.controller_client.update_status(robot)
        return robots

    def test_list_robot_with_battery_state_online(self):
        """Verify list filtering by battery, state, online, and combinations."""
        robots = self._setup_carter_robots()

        all_robots = self.client.list(api_objects.RobotObjectV1)
        assert len(all_robots) == len(robots)

        battery_ge_50 = self.client.list(api_objects.RobotObjectV1, {"min_battery": 50})
        assert len(battery_ge_50) == 5

        battery_ge_90 = self.client.list(api_objects.RobotObjectV1, {"min_battery": 90})
        assert len(battery_ge_90) == 1
        assert robots[9] == battery_ge_90[0]

        battery_ge_91 = self.client.list(api_objects.RobotObjectV1, {"min_battery": 91})
        assert len(battery_ge_91) == 0

        idle_robots = self.client.list(api_objects.RobotObjectV1, {"state": "IDLE"})
        assert len(idle_robots) == 5

        online_robots = self.client.list(api_objects.RobotObjectV1, {"online": True})
        assert len(online_robots) == 6

        output = self.client.list(api_objects.RobotObjectV1, {"min_battery": 55, "state": "IDLE"})
        assert len(output) == 2
        assert robots[6] in output
        assert robots[8] in output

        output = self.client.list(api_objects.RobotObjectV1, {"state": "IDLE", "online": True})
        assert len(output) == 3
        assert robots[0] in output
        assert robots[4] in output
        assert robots[8] in output

        output = self.client.list(api_objects.RobotObjectV1,
                                  {"min_battery": 55, "state": "IDLE", "online": True})
        assert len(output) == 1
        assert robots[8] == output[0]

    def test_list_robot_names(self):
        """Verify filtering by specific robot names."""
        robots = self._setup_carter_robots()

        params: Dict[str, Any] = {
            "names": ["carter01", "carter03", "carter04", "carter09"]
        }
        output = self.client.list(api_objects.RobotObjectV1, params)
        assert len(output) == 4
        assert robots[1] in output
        assert robots[3] in output
        assert robots[4] in output
        assert robots[9] in output

        params = {
            "names": ["carter01", "carter03", "carter04", "carter09"],
            "min_battery": 30,
            "state": "IDLE"
        }
        output = self.client.list(api_objects.RobotObjectV1, params)
        assert len(output) == 1
        assert robots[4] == output[0]

    def test_mission_queries(self):
        """Verify mission list filtering by time range, state, and robot."""
        tree = [MissionNodeV1(selector={})]
        missions = [
            api_objects.MissionObjectV1(
                name="mission0", robot="0", mission_tree=tree, status={}),
            api_objects.MissionObjectV1(
                name="mission1", robot="1", mission_tree=tree, status={}),
            api_objects.MissionObjectV1(
                name="mission2", robot="2", mission_tree=tree, status={})
        ]
        for mission in missions:
            self.client.create(mission)

        missions[0].status.start_timestamp = datetime.datetime(2001, 1, 1)
        missions[0].status.state = MissionStateV1.PENDING
        missions[1].status.start_timestamp = datetime.datetime(2002, 2, 2)
        missions[1].status.state = MissionStateV1.COMPLETED
        missions[2].status.start_timestamp = datetime.datetime(2003, 3, 3)
        missions[2].status.state = MissionStateV1.FAILED

        for mission in missions:
            self.controller_client.update_status(mission)

        returned = self.client.list(api_objects.MissionObjectV1,
                                    {"started_after": datetime.datetime(2001, 1, 1)})
        assert len(returned) == 3

        returned = self.client.list(api_objects.MissionObjectV1,
                                    {"started_after": datetime.datetime(2001, 1, 2)})
        assert len(returned) == 2

        returned = self.client.list(api_objects.MissionObjectV1,
                                    {"started_after": datetime.datetime(2002, 12, 30)})
        assert len(returned) == 1
        assert missions[2] == returned[0]

        returned = self.client.list(api_objects.MissionObjectV1, {
            "started_after": datetime.datetime(2002, 1, 1),
            "started_before": datetime.datetime(2003, 1, 1)
        })
        assert len(returned) == 1
        assert missions[1] == returned[0]

        returned = self.client.list(api_objects.MissionObjectV1, {"most_recent": 1})
        assert len(returned) == 1
        assert missions[2] == returned[0]

        returned = self.client.list(api_objects.MissionObjectV1, {"most_recent": 2})
        assert len(returned) == 2
        assert missions[2] == returned[0]
        assert missions[1] == returned[1]

        returned = self.client.list(api_objects.MissionObjectV1, {"robot": "0"})
        assert len(returned) == 1
        assert missions[0] == returned[0]

    def test_list_arm_names(self):
        """Verify filtering by robot type (AGV class)."""
        robots = self._setup_carter_and_arm_robots()

        output = self.client.list(api_objects.RobotObjectV1,
                                  {"robot_type": [RobotTypeV1.AMR.value]})
        assert len(output) == 1
        assert robots[1] in output

        output = self.client.list(api_objects.RobotObjectV1,
                                  {"robot_type": [RobotTypeV1.ARM.value]})
        assert len(output) == 1
        assert robots[0] in output

    def test_detection_results_push(self):
        """Verify detection results can be created and fetched."""
        robot = api_objects.RobotObjectV1(status={}, name="arm01")
        detection_result = api_objects.DetectionResultsObjectV1(name="arm01")

        self.client.create(robot)
        self.controller_client.create(detection_result)

        robot.status.factsheet.agv_class = RobotTypeV1.ARM.value
        detection_result.status.detected_objects = [
            DetectedObject(bbox2d=DetectedObjectBoundingBox2D())]
        detection_result.status.detected_objects[0].object_id = 4

        self.controller_client.update_status(robot)
        self.controller_client.update_status(detection_result)

        output = self.client.list(api_objects.DetectionResultsObjectV1)
        assert len(output) == 1
        assert detection_result in output
        assert output[0].status.detected_objects[0].object_id == 4
