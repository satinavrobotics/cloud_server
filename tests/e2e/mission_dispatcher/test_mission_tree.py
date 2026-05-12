"""
E2E tests for mission dispatch – mission tree / behavior tree scenarios.

Migrated from packages/controllers/mission/tests/mission_tree.py (Bazel py_test).
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
    mission_object_generator,
    route_generator,
    action_generator,
    notify_generator,
    pose1D_generator,
)

# ---------------------------------------------------------------------------
# Mission tree definitions (ported from mission_tree.py)
# ---------------------------------------------------------------------------

MISSION_TREE_1 = [
    route_generator(),
    action_generator(params={"should_fail": 0, "time": 1}),
    action_generator(params={"should_fail": 0, "time": 2}),
    route_generator(),
]
SCENARIO1_EXPECTED_STATUSES = [
    mission_object.MissionStatusV1(state="PENDING", current_node=0),
    mission_object.MissionStatusV1(state="RUNNING", current_node=0),
    mission_object.MissionStatusV1(state="RUNNING", current_node=1),
    mission_object.MissionStatusV1(state="RUNNING", current_node=2),
    mission_object.MissionStatusV1(state="RUNNING", current_node=3),
    mission_object.MissionStatusV1(
        state="COMPLETED", current_node=3,
        node_status={
            "root": {"state": "COMPLETED"},
            "0": {"state": "COMPLETED"},
            "1": {"state": "COMPLETED"},
            "2": {"state": "COMPLETED"},
            "3": {"state": "COMPLETED"},
        }),
]

MISSION_TREE_2 = [
    route_generator(),
    action_generator(params={"should_fail": 1, "time": 3}),
    route_generator(),
]
SCENARIO2_EXPECTED_STATUSES = [
    mission_object.MissionStatusV1(state="PENDING", current_node=0),
    mission_object.MissionStatusV1(state="RUNNING", current_node=0),
    mission_object.MissionStatusV1(state="RUNNING", current_node=1),
    mission_object.MissionStatusV1(
        state="FAILED", current_node=1,
        node_status={
            "root": {"state": "FAILED"},
            "0": {"state": "COMPLETED"},
            "1": {"state": "FAILED", "error_msg": "Action failure"},
            "2": {"state": "PENDING"},
        }),
]

MISSION_TREE_3 = [
    route_generator(),
    {"name": "selector_1", "selector": {}, "parent": "root"},
    action_generator(params={"should_fail": 1, "time": 3}, parent="selector_1"),
    route_generator(parent="selector_1"),
]
SCENARIO3_EXPECTED_STATUSES = [
    mission_object.MissionStatusV1(state="PENDING", current_node=0),
    mission_object.MissionStatusV1(state="RUNNING", current_node=0),
    mission_object.MissionStatusV1(state="RUNNING", current_node=2),
    mission_object.MissionStatusV1(state="RUNNING", current_node=3),
    mission_object.MissionStatusV1(
        state="COMPLETED", current_node=3,
        node_status={
            "root": {"state": "COMPLETED"},
            "0": {"state": "COMPLETED"},
            "selector_1": {"state": "COMPLETED"},
            "2": {"state": "FAILED", "error_msg": "Action failure"},
            "3": {"state": "COMPLETED"},
        }),
]

MISSION_TREE_4 = [
    route_generator(),
    {"name": "sequence_1", "sequence": {}, "parent": "root"},
    action_generator(params={"should_fail": 1, "time": 3}, parent="sequence_1"),
    route_generator(parent="sequence_1"),
]
SCENARIO4_EXPECTED_STATUSES = [
    mission_object.MissionStatusV1(state="PENDING", current_node=0),
    mission_object.MissionStatusV1(state="RUNNING", current_node=0),
    mission_object.MissionStatusV1(state="RUNNING", current_node=2),
    mission_object.MissionStatusV1(
        state="FAILED", current_node=2,
        node_status={
            "root": {"state": "FAILED"},
            "0": {"state": "COMPLETED"},
            "sequence_1": {"state": "FAILED"},
            "2": {"state": "FAILED", "error_msg": "Action failure"},
            "3": {"state": "PENDING"},
        }),
]

MISSION_TREE_5 = [
    route_generator(),
    {"name": "selector_1", "selector": {}, "parent": "root"},
    action_generator(params={"should_fail": 1, "time": 3}, parent="selector_1"),
    {"name": "sequence_1", "sequence": {}, "parent": "selector_1"},
    route_generator(parent="sequence_1"),
    route_generator(parent="sequence_1"),
    route_generator(),
]
SCENARIO5_EXPECTED_STATUSES = [
    mission_object.MissionStatusV1(state="PENDING", current_node=0),
    mission_object.MissionStatusV1(state="RUNNING", current_node=0),
    mission_object.MissionStatusV1(state="RUNNING", current_node=2),
    mission_object.MissionStatusV1(state="RUNNING", current_node=4),
    mission_object.MissionStatusV1(state="RUNNING", current_node=5),
    mission_object.MissionStatusV1(state="RUNNING", current_node=6),
    mission_object.MissionStatusV1(
        state="COMPLETED", current_node=6,
        node_status={
            "root": {"state": "COMPLETED"},
            "0": {"state": "COMPLETED"},
            "selector_1": {"state": "COMPLETED"},
            "2": {"state": "FAILED", "error_msg": "Action failure"},
            "sequence_1": {"state": "COMPLETED"},
            "4": {"state": "COMPLETED"},
            "5": {"state": "COMPLETED"},
            "6": {"state": "COMPLETED"},
        }),
]

MISSION_TREE_6 = [
    route_generator(),
    action_generator(params={"should_fail": 0, "time": 1}, parent="root", name="pickup"),
    {"name": "selector_1", "selector": {}, "parent": "root"},
    action_generator(params={"should_fail": 1, "time": 1},
                     parent="selector_1", name="fake_failure_route"),
    {"name": "sequence_1", "sequence": {}, "parent": "selector_1"},
    route_generator(parent="sequence_1"),
    action_generator(params={"should_fail": 0, "time": 1},
                     parent="sequence_1", name="dropoff"),
    {"name": "constant_node", "constant": {"success": "false"}, "parent": "sequence_1"},
    action_generator(params={"should_fail": 0, "time": 1},
                     parent="root", name="dropoff_at_goal"),
]
SCENARIO6_EXPECTED_STATUSES = [
    mission_object.MissionStatusV1(state="PENDING", current_node=0),
    mission_object.MissionStatusV1(state="RUNNING", current_node=0),
    mission_object.MissionStatusV1(state="RUNNING", current_node=1),
    mission_object.MissionStatusV1(state="RUNNING", current_node=3),
    mission_object.MissionStatusV1(state="RUNNING", current_node=5),
    mission_object.MissionStatusV1(state="RUNNING", current_node=6),
    mission_object.MissionStatusV1(
        state="FAILED", current_node=7,
        node_status={
            "root": {"state": "FAILED"},
            "0": {"state": "COMPLETED"},
            "pickup": {"state": "COMPLETED"},
            "selector_1": {"state": "FAILED"},
            "fake_failure_route": {"state": "FAILED", "error_msg": "Action failure"},
            "sequence_1": {"state": "FAILED"},
            "5": {"state": "COMPLETED"},
            "dropoff": {"state": "COMPLETED"},
            "constant_node": {"state": "FAILED"},
            "dropoff_at_goal": {"state": "PENDING"},
        }),
]

# Trees 7-9 use notify_generator; url is patched at test time
MISSION_TREE_7 = [
    route_generator(),
    notify_generator(url="", json_data={
        "labels": [], "battery": {"critical_level": 0.1},
        "heartbeat_timeout": 30, "name": "bob"
    }),
]
SCENARIO7_EXPECTED_STATUSES = [
    mission_object.MissionStatusV1(state="PENDING", current_node=0),
    mission_object.MissionStatusV1(state="RUNNING", current_node=0),
    mission_object.MissionStatusV1(state="RUNNING", current_node=1),
    mission_object.MissionStatusV1(
        state="COMPLETED", current_node=1,
        node_status={
            "root": {"state": "COMPLETED"},
            "0": {"state": "COMPLETED"},
            "1": {"state": "COMPLETED"},
        }),
]

MISSION_TREE_8 = [
    notify_generator(url="", json_data={
        "labels": [], "battery": {"critical_level": 0.1},
        "heartbeat_timeout": 30, "name": "bob"
    }),
]
SCENARIO8_EXPECTED_STATUSES = [
    mission_object.MissionStatusV1(state="PENDING", current_node=0),
    mission_object.MissionStatusV1(state="RUNNING", current_node=0),
    mission_object.MissionStatusV1(
        state="COMPLETED", current_node=0,
        node_status={
            "root": {"state": "COMPLETED"},
            "0": {"state": "COMPLETED"},
        }),
]

MISSION_TREE_9 = [
    route_generator(),
    notify_generator(url="", json_data={
        "labels": [], "battery": {"critical_level": 0.1},
        "heartbeat_timeout": 30, "name": "test01"  # duplicate name triggers failure
    }),
]
SCENARIO9_EXPECTED_STATUSES = [
    mission_object.MissionStatusV1(state="PENDING", current_node=0),
    mission_object.MissionStatusV1(state="RUNNING", current_node=0),
    mission_object.MissionStatusV1(state="RUNNING", current_node=1),
    mission_object.MissionStatusV1(
        state="FAILED", current_node=1,
        node_status={
            "root": {"state": "FAILED"},
            "0": {"state": "COMPLETED"},
            "1": {"state": "FAILED"},
        }),
]

MISSION_TREE_10 = [
    {
        "route": {
            "waypoints": [
                {"x": pose1D_generator(), "y": pose1D_generator(),
                 "theta": 0, "allowedDeviationXY": 0},
                {"x": pose1D_generator(), "y": pose1D_generator(),
                 "theta": 0, "allowedDeviationXY": 1},
                {"x": pose1D_generator(), "y": pose1D_generator(),
                 "theta": 0, "allowedDeviationXY": 1},
                {"x": pose1D_generator(), "y": pose1D_generator(),
                 "theta": 0, "allowedDeviationXY": 0},
                {"x": pose1D_generator(), "y": pose1D_generator(),
                 "theta": 0, "allowedDeviationXY": 1},
                {"x": pose1D_generator(), "y": pose1D_generator(),
                 "theta": 0, "allowedDeviationXY": 0},
            ]
        },
        "parent": "root",
    }
]
SCENARIO10_EXPECTED_STATUSES = [
    mission_object.MissionStatusV1(state="PENDING", current_node=0),
    mission_object.MissionStatusV1(state="RUNNING", current_node=0),
    mission_object.MissionStatusV1(state="RUNNING", current_node=0,
                                   task_status={"0": 0}),
    mission_object.MissionStatusV1(state="RUNNING", current_node=0,
                                   task_status={"0": 1}),
    mission_object.MissionStatusV1(state="RUNNING", current_node=0,
                                   task_status={"0": 2}),
    mission_object.MissionStatusV1(state="COMPLETED", current_node=0,
                                   task_status={"0": 2}),
]


def _db_url(ctx) -> str:
    """Return the base URL for the mission-database user-facing endpoint."""
    from tests.e2e.mission_dispatcher.conftest import MISSION_DATABASE_URL
    return MISSION_DATABASE_URL


@pytest.mark.e2e
class TestMissionTree(unittest.TestCase):
    """Test mission tree / behavior tree execution"""

    def test_single_layer_mission_tree(self):
        """Test single layer tree with routes and actions"""
        robot = simulator.RobotInit("test01", 0, 0, 0)
        with TestContext([robot]) as ctx:
            ctx.db_client.create(api_objects.RobotObjectV1(name="test01", status={}))
            time.sleep(0.25)
            ctx.db_client.create(mission_object_generator("test01", MISSION_TREE_1))
            for expected_state, update in zip(
                    SCENARIO1_EXPECTED_STATUSES,
                    ctx.db_client.watch(api_objects.MissionObjectV1)):
                self.assertEqual(update.status.state, expected_state.state)
                self.assertEqual(update.status.current_node, expected_state.current_node)
                if update.status.state == mission_object.MissionStateV1.COMPLETED:
                    self.assertEqual(update.status.node_status, expected_state.node_status)
                    break

            robot_status = ctx.db_client.get(api_objects.RobotObjectV1, "test01").status
            waypoint = MISSION_TREE_1[-1]["route"]["waypoints"][-1]
            self.assertAlmostEqual(robot_status.pose.x, waypoint["x"], places=2)
            self.assertAlmostEqual(robot_status.pose.y, waypoint["y"], places=2)

    def test_single_layer_tree_with_action_failure(self):
        """Test single layer tree with routes and failure action"""
        robot = simulator.RobotInit("test01", 0, 0, 0)
        with TestContext([robot]) as ctx:
            ctx.db_client.create(api_objects.RobotObjectV1(name="test01", status={}))
            time.sleep(0.25)
            ctx.db_client.create(mission_object_generator("test01", MISSION_TREE_2))
            for expected_state, update in zip(
                    SCENARIO2_EXPECTED_STATUSES,
                    ctx.db_client.watch(api_objects.MissionObjectV1)):
                self.assertEqual(update.status.state, expected_state.state)
                self.assertEqual(update.status.current_node, expected_state.current_node)
                if update.status.state == mission_object.MissionStateV1.FAILED:
                    self.assertEqual(update.status.node_status, expected_state.node_status)
                    break

    def test_selection_node_with_failure_action(self):
        """Test two-layer tree with selector node and failure action"""
        robot = simulator.RobotInit("test01", 0, 0, 0)
        with TestContext([robot]) as ctx:
            ctx.db_client.create(api_objects.RobotObjectV1(name="test01", status={}))
            time.sleep(0.25)
            ctx.db_client.create(mission_object_generator("test01", MISSION_TREE_3))
            for expected_state, update in zip(
                    SCENARIO3_EXPECTED_STATUSES,
                    ctx.db_client.watch(api_objects.MissionObjectV1)):
                self.assertEqual(update.status.state, expected_state.state)
                self.assertEqual(update.status.current_node, expected_state.current_node)
                if update.status.state == mission_object.MissionStateV1.COMPLETED:
                    self.assertEqual(update.status.node_status, expected_state.node_status)
                    break

    def test_sequence_node_with_failure_action(self):
        """Test two-layer tree with sequence node and failure action"""
        robot = simulator.RobotInit("test01", 0, 0, 0)
        with TestContext([robot]) as ctx:
            ctx.db_client.create(api_objects.RobotObjectV1(name="test01", status={}))
            time.sleep(0.25)
            ctx.db_client.create(mission_object_generator("test01", MISSION_TREE_4))
            for expected_state, update in zip(
                    SCENARIO4_EXPECTED_STATUSES,
                    ctx.db_client.watch(api_objects.MissionObjectV1)):
                self.assertEqual(update.status.state, expected_state.state)
                self.assertEqual(update.status.current_node, expected_state.current_node)
                if update.status.state == mission_object.MissionStateV1.FAILED:
                    self.assertEqual(update.status.node_status, expected_state.node_status)
                    break

    def test_three_layer_behavior_tree(self):
        """Test three-layer tree with selector and sequence control nodes"""
        robot = simulator.RobotInit("test01", 0, 0, 0)
        with TestContext([robot]) as ctx:
            ctx.db_client.create(api_objects.RobotObjectV1(name="test01", status={}))
            time.sleep(0.25)
            ctx.db_client.create(mission_object_generator("test01", MISSION_TREE_5))
            for expected_state, update in zip(
                    SCENARIO5_EXPECTED_STATUSES,
                    ctx.db_client.watch(api_objects.MissionObjectV1)):
                self.assertEqual(update.status.state, expected_state.state)
                self.assertEqual(update.status.current_node, expected_state.current_node)
                if update.status.state == mission_object.MissionStateV1.COMPLETED:
                    self.assertEqual(update.status.node_status, expected_state.node_status)
                    break

            robot_status = ctx.db_client.get(api_objects.RobotObjectV1, "test01").status
            waypoint = MISSION_TREE_5[-1]["route"]["waypoints"][-1]
            self.assertAlmostEqual(robot_status.pose.x, waypoint["x"], places=2)
            self.assertAlmostEqual(robot_status.pose.y, waypoint["y"], places=2)

    def test_naming(self):
        """Test if certain names will trigger node translation failure"""
        robot = simulator.RobotInit("test01", 0, 0, 0)
        mission_tree = [
            route_generator(name="route-node"),
            action_generator(params={"should_fail": 0, "time": 1}, name="action-node"),
        ]
        expected_statuses = [
            mission_object.MissionStatusV1(state="PENDING", current_node=0),
            mission_object.MissionStatusV1(state="RUNNING", current_node=0),
            mission_object.MissionStatusV1(state="RUNNING", current_node=1),
            mission_object.MissionStatusV1(state="COMPLETED", current_node=1),
        ]
        with TestContext([robot]) as ctx:
            ctx.db_client.create(api_objects.RobotObjectV1(name="test01", status={}))
            time.sleep(0.25)
            mission = mission_object_generator("test01", mission_tree)
            mission.name = "my-new-mission"
            ctx.db_client.create(mission)
            for expected_state, update in zip(
                    expected_statuses,
                    ctx.db_client.watch(api_objects.MissionObjectV1)):
                self.assertEqual(update.status.state, expected_state.state)
                self.assertEqual(update.status.current_node, expected_state.current_node)
                if update.status.state == mission_object.MissionStateV1.COMPLETED:
                    break

    def test_duplicate_node_name(self):
        """Test if mission fails when there are duplicate node names"""
        robot = simulator.RobotInit("test01", 0, 0, 0)
        mission_tree = [
            route_generator(name="route-node", parent="root"),
            route_generator(name="route-node", parent="root"),
        ]
        with TestContext([robot]) as ctx:
            ctx.db_client.create(api_objects.RobotObjectV1(name="test01", status={}))
            time.sleep(0.25)
            with self.assertRaises(common.ICSUsageError) as cm:
                ctx.db_client.create(mission_object_generator("test01", mission_tree))
            self.assertIn("route-node", str(cm.exception))
            self.assertIn("repeated", str(cm.exception))

    def test_nonexist_parent(self):
        """Test if mission fails when parent doesn't exist"""
        robot = simulator.RobotInit("test01", 0, 0, 0)
        mission_tree = [route_generator(name="route-node", parent="root-1")]
        with TestContext([robot]) as ctx:
            ctx.db_client.create(api_objects.RobotObjectV1(name="test01", status={}))
            time.sleep(0.25)
            with self.assertRaises(common.ICSUsageError) as cm:
                ctx.db_client.create(mission_object_generator("test01", mission_tree))
            self.assertIn("root-1", str(cm.exception))
            self.assertIn("route-node", str(cm.exception))

    def test_restart_behavior_tree_halfway(self):
        """Test if behavior works well if we pick up a mission halfway"""
        robot = simulator.RobotInit("test01", 0, 0, 0)
        restart_once = False
        with TestContext([robot]) as ctx:
            ctx.db_client.create(api_objects.RobotObjectV1(name="test01", status={}))
            time.sleep(0.25)
            ctx.db_client.create(mission_object_generator("test01", MISSION_TREE_5))

            completed = False
            watcher = ctx.db_client.watch(api_objects.MissionObjectV1)
            for update in watcher:
                if (not restart_once
                        and update.status.node_status.get("selector_1", {})
                        .get("state") == "RUNNING"):
                    ctx.restart_mission_server()
                    restart_once = True
                    continue
                if update.status.state == mission_object.MissionStateV1.COMPLETED:
                    completed = True
                    break
            self.assertTrue(completed)

    def test_constant_node(self):
        """Test three-layer tree with the constant node"""
        robot = simulator.RobotInit("test01", 0, 0, 0)
        with TestContext([robot]) as ctx:
            ctx.db_client.create(api_objects.RobotObjectV1(name="test01", status={}))
            time.sleep(0.25)
            ctx.db_client.create(mission_object_generator("test01", MISSION_TREE_6))
            for expected_state, update in zip(
                    SCENARIO6_EXPECTED_STATUSES,
                    ctx.db_client.watch(api_objects.MissionObjectV1)):
                self.assertEqual(update.status.state, expected_state.state)
                self.assertEqual(update.status.current_node, expected_state.current_node)
                if update.status.state in (
                        mission_object.MissionStateV1.COMPLETED,
                        mission_object.MissionStateV1.FAILED):
                    self.assertEqual(update.status.node_status, expected_state.node_status)
                    break

    def test_route_with_notify_node(self):
        """Test simple tree with notify node"""
        robot = simulator.RobotInit("test01", 0, 0, 0)
        with TestContext([robot]) as ctx:
            from tests.e2e.mission_dispatcher.conftest import MISSION_DATABASE_URL
            tree = list(MISSION_TREE_7)  # shallow copy
            tree[1] = dict(tree[1])
            tree[1]["notify"] = dict(tree[1].get("notify", {}))
            tree[1]["notify"]["url"] = f"{MISSION_DATABASE_URL}/robot"
            ctx.db_client.create(api_objects.RobotObjectV1(name="test01", status={}))
            time.sleep(0.25)
            ctx.db_client.create(mission_object_generator("test01", tree))
            for expected_state, update in zip(
                    SCENARIO7_EXPECTED_STATUSES,
                    ctx.db_client.watch(api_objects.MissionObjectV1)):
                self.assertEqual(update.status.state, expected_state.state)
                self.assertEqual(update.status.current_node, expected_state.current_node)
                if update.status.state == mission_object.MissionStateV1.COMPLETED:
                    self.assertEqual(update.status.node_status, expected_state.node_status)
                    break

    def test_single_notify_node(self):
        """Test tree with single notify node"""
        robot = simulator.RobotInit("test01", 0, 0, 0)
        with TestContext([robot]) as ctx:
            from tests.e2e.mission_dispatcher.conftest import MISSION_DATABASE_URL
            tree = list(MISSION_TREE_8)
            tree[0] = dict(tree[0])
            tree[0]["notify"] = dict(tree[0].get("notify", {}))
            tree[0]["notify"]["url"] = f"{MISSION_DATABASE_URL}/robot"
            ctx.db_client.create(api_objects.RobotObjectV1(name="test01", status={}))
            time.sleep(0.25)
            ctx.db_client.create(mission_object_generator("test01", tree))
            for expected_state, update in zip(
                    SCENARIO8_EXPECTED_STATUSES,
                    ctx.db_client.watch(api_objects.MissionObjectV1)):
                self.assertEqual(update.status.state, expected_state.state)
                self.assertEqual(update.status.current_node, expected_state.current_node)
                if update.status.state == mission_object.MissionStateV1.COMPLETED:
                    self.assertEqual(update.status.node_status, expected_state.node_status)
                    break

    def test_failed_notify_node(self):
        """Test simple tree with failed notify node (duplicate name)"""
        robot = simulator.RobotInit("test01", 0, 0, 0)
        with TestContext([robot]) as ctx:
            from tests.e2e.mission_dispatcher.conftest import MISSION_DATABASE_URL
            tree = list(MISSION_TREE_9)
            tree[1] = dict(tree[1])
            tree[1]["notify"] = dict(tree[1].get("notify", {}))
            tree[1]["notify"]["url"] = f"{MISSION_DATABASE_URL}/robot"
            ctx.db_client.create(api_objects.RobotObjectV1(name="test01", status={}))
            time.sleep(0.25)
            ctx.db_client.create(mission_object_generator("test01", tree))
            for expected_state, update in zip(
                    SCENARIO9_EXPECTED_STATUSES,
                    ctx.db_client.watch(api_objects.MissionObjectV1)):
                self.assertEqual(update.status.state, expected_state.state)
                self.assertEqual(update.status.current_node, expected_state.current_node)
                if update.status.state in (
                        mission_object.MissionStateV1.COMPLETED,
                        mission_object.MissionStateV1.FAILED):
                    self.assertEqual(update.status.node_status, expected_state.node_status)
                    break

    def test_task_status(self):
        """Test mission task status tracking"""
        robot = simulator.RobotInit("test01", 0, 0, 0)
        with TestContext([robot]) as ctx:
            ctx.db_client.create(api_objects.RobotObjectV1(name="test01", status={}))
            time.sleep(0.25)
            ctx.db_client.create(mission_object_generator("test01", MISSION_TREE_10))

            for expected_state, update in zip(
                    SCENARIO10_EXPECTED_STATUSES,
                    ctx.db_client.watch(api_objects.MissionObjectV1)):
                self.assertEqual(update.status.state, expected_state.state)
                self.assertEqual(update.status.current_node, expected_state.current_node)
                self.assertEqual(update.status.task_status, expected_state.task_status)
                if update.status.state == mission_object.MissionStateV1.COMPLETED:
                    self.assertEqual(update.status.task_status, expected_state.task_status)
                    break


if __name__ == "__main__":
    unittest.main()
