"""
E2E tests for mission dispatch – robot lifecycle scenarios.

Migrated from packages/controllers/mission/tests/robot.py (Bazel py_test).
"""
import datetime
import time
import unittest

import pytest
import paho.mqtt.client as mqtt_client

import packages.controllers.mission.vda5050_types as types
from cloud_common import objects as api_objects
from cloud_common.objects import mission as mission_object
from cloud_common.objects import robot as robot_object
from cloud_common.objects.robot import RobotStateV1
from packages.controllers.mission.tests import client as simulator

from tests.e2e.mission_dispatcher.conftest import (
    TestContext,
    mission_from_waypoint,
    mission_from_waypoints,
    mission_object_generator,
    route_generator,
    action_generator,
    MQTT_TRANSPORT,
    MQTT_WS_PATH,
    MQTT_PORT,
    MQTT_PREFIX,
)

SCENARIO1_WAYPOINTS = [(1, 1), (10, 10), (5, 5)]

MISSION_TREE_1 = [
    route_generator(),
    action_generator(params={}, name="teleop", action_type="pause_order"),
    route_generator(),
]


@pytest.mark.e2e
class TestMissions(unittest.TestCase):
    def test_many_robots(self):
        """Test sending a mission to 5 different robots at the same time"""
        num_robots = 5
        sim_robots, robots, missions = [], [], []
        for i in range(num_robots):
            name = f"test{i:02d}"
            sim_robots.append(simulator.RobotInit(name, i, i))
            robots.append(api_objects.RobotObjectV1(name=name, status={}))
            missions.append(mission_from_waypoint(name, i + 10, i + 5))

        with TestContext(sim_robots) as ctx:
            for robot in robots:
                ctx.db_client.create(robot)
                time.sleep(0.25)
            for mission in missions:
                ctx.db_client.create(mission)
                time.sleep(0.25)

            completed_missions = set()
            for mission in ctx.db_client.watch(api_objects.MissionObjectV1):
                if mission.status.state == mission_object.MissionStateV1.COMPLETED:
                    completed_missions.add(mission.name)
                if len(completed_missions) == len(missions):
                    break
            time.sleep(1)

            db_robots = ctx.db_client.list(api_objects.RobotObjectV1)
            db_missions = ctx.db_client.list(api_objects.MissionObjectV1)

            for mission in db_missions:
                self.assertEqual(mission.status.state,
                                 mission_object.MissionStateV1.COMPLETED)
            for robot in db_robots:
                robot_id = int(robot.name.lstrip("test"))
                self.assertEqual(robot.status.pose.x, robot_id + 10)
                self.assertEqual(robot.status.pose.y, robot_id + 5)

    def test_robot_offline(self):
        """Test that the server labels the robot as offline after not receiving messages"""
        robot = simulator.RobotInit("test01", 0, 0, 0)
        with TestContext([robot], tick_period=2.0) as ctx:
            ctx.db_client.create(api_objects.RobotObjectV1(
                name="test01", heartbeat_timeout=1, status={}))

            expected_online = [False, True, False, True]
            for online, update in zip(expected_online,
                                      ctx.db_client.watch(api_objects.RobotObjectV1)):
                self.assertEqual(update.status.online, online)

    def test_robot_task_state(self):
        """Test if the robot task state is correctly updated"""
        robot = simulator.RobotInit("test01", 0, 0, 0)
        with TestContext([robot]) as ctx:
            ctx.db_client.create(api_objects.RobotObjectV1(name="test01", status={}))
            watcher = ctx.db_client.watch(api_objects.RobotObjectV1)

            first_update = next(watcher)
            self.assertEqual(first_update.status.state, robot_object.RobotStateV1.IDLE)

            ctx.db_client.create(mission_from_waypoint("test01", 10.0, 10.0))

            for update in watcher:
                if update.status.state == robot_object.RobotStateV1.ON_TASK:
                    break

            for update in watcher:
                if update.status.state == robot_object.RobotStateV1.IDLE:
                    self.assertEqual(update.status.pose.x, 10.0)
                    self.assertEqual(update.status.pose.y, 10.0)
                    break

    def test_robot_hardware_version_update(self):
        """Test robot hardware version update"""
        robot = simulator.RobotInit("test01", 0, 0, 0, "map", 0, 0, "NV", "1NV023200CAR00010")
        with TestContext([robot]) as ctx:
            ctx.db_client.create(api_objects.RobotObjectV1(name="test01", status={}))
            time.sleep(0.25)
            ctx.db_client.create(mission_from_waypoints("test01", SCENARIO1_WAYPOINTS))

            watcher = ctx.db_client.watch(api_objects.RobotObjectV1)
            for update in watcher:
                if update.status.online:
                    break
            next_update = next(watcher)

            robot_hardware = next_update.status.hardware_version
            self.assertEqual(robot_hardware.manufacturer, "NV")
            self.assertEqual(robot_hardware.serial_number, "1NV023200CAR00010")

    def test_battery_level(self):
        """Validate battery level"""
        robot = simulator.RobotInit("test01", 0, 0, battery=42)
        with TestContext([robot]) as ctx:
            ctx.db_client.create(api_objects.RobotObjectV1(name="test01", status={}))
            watcher = ctx.db_client.watch(api_objects.RobotObjectV1)
            for update in watcher:
                if update.status.battery_level == 42:
                    break

    def test_charging_transition(self):
        """Validate charging state transition"""
        from tests.e2e.mission_dispatcher.conftest import MQTT_HOST, MQTT_PORT_TCP
        robot = simulator.RobotInit("test01", 0, 0)
        client = mqtt_client.Client(transport=MQTT_TRANSPORT)
        client.ws_set_options(path=MQTT_WS_PATH)
        with TestContext([robot]) as ctx:
            client.connect(MQTT_HOST, MQTT_PORT_TCP)
            ctx.db_client.create(api_objects.RobotObjectV1(name="test01", status={}))

            watcher = ctx.db_client.watch(api_objects.RobotObjectV1)
            for update in watcher:
                if update.status.state == RobotStateV1.IDLE:
                    break

            topic = f"{MQTT_PREFIX}/test01/state"
            message = types.VDA5050State(
                headerId=0,
                timestamp=datetime.datetime.now().isoformat(),
                manufacturer="", serialNumber="",
                orderId="", orderUpdateId=0,
                lastNodeId="", lastNodeSequenceId=0,
                nodeStates=[], edgeStates=[], actionStates=[],
                agvPosition={"x": 0, "y": 0, "theta": 0, "mapId": ""},
                batteryState={"batteryCharge": 50, "charging": True},
                safetyState=types.VDA5050SafetyStatus(
                    eStop=types.VDA5050EStop.NONE, fieldViolation=False))
            client.publish(topic, message.json())
            time.sleep(0.5)
            for update in watcher:
                if update.status.state == RobotStateV1.CHARGING:
                    break

            message.batteryState.charging = False
            client.publish(topic, message.json())
            time.sleep(0.5)
            for update in watcher:
                if update.status.state == RobotStateV1.IDLE:
                    break

    def test_teleop_in_mission(self):
        """Test mission with teleop node"""
        robot = simulator.RobotInit("test01", 0, 0, 0)
        with TestContext([robot]) as ctx:
            ctx.db_client.create(api_objects.RobotObjectV1(name="test01", status={}))
            time.sleep(0.25)
            ctx.db_client.create(mission_object_generator("test01", MISSION_TREE_1))

            watcher = ctx.db_client.watch(api_objects.RobotObjectV1)
            for update in watcher:
                if update.status.state == robot_object.RobotStateV1.TELEOP:
                    break
            time.sleep(5)
            ctx.call_teleop_service(robot_name="test01",
                                    teleop=robot_object.RobotTeleopActionV1.STOP)
            for update in ctx.db_client.watch(api_objects.MissionObjectV1):
                if update.status.state == mission_object.MissionStateV1.COMPLETED:
                    break

            robot_status = ctx.db_client.get(api_objects.RobotObjectV1, "test01").status
            waypoint = MISSION_TREE_1[-1]["route"]["waypoints"][-1]
            self.assertAlmostEqual(robot_status.pose.x, waypoint["x"], places=2)
            self.assertAlmostEqual(robot_status.pose.y, waypoint["y"], places=2)

    def test_teleop_by_user_request(self):
        """Test teleop by user request"""
        robot = simulator.RobotInit("test01", 0, 0, 0)
        with TestContext([robot]) as ctx:
            ctx.db_client.create(api_objects.RobotObjectV1(name="test01", status={}))
            time.sleep(0.25)
            ctx.db_client.create(mission_from_waypoints("test01", SCENARIO1_WAYPOINTS))

            for mission in ctx.db_client.watch(api_objects.MissionObjectV1):
                if mission.status.state == mission_object.MissionStateV1.RUNNING:
                    break
            watcher = ctx.db_client.watch(api_objects.RobotObjectV1)
            ctx.call_teleop_service(robot_name="test01",
                                    teleop=robot_object.RobotTeleopActionV1.START)
            time.sleep(5)
            for update in watcher:
                if update.status.state == robot_object.RobotStateV1.TELEOP:
                    break
            ctx.call_teleop_service(robot_name="test01",
                                    teleop=robot_object.RobotTeleopActionV1.STOP)
            for update in watcher:
                if update.status.state == robot_object.RobotStateV1.ON_TASK:
                    break
            for update in ctx.db_client.watch(api_objects.MissionObjectV1):
                if update.status.state == mission_object.MissionStateV1.COMPLETED:
                    break


if __name__ == "__main__":
    unittest.main()
