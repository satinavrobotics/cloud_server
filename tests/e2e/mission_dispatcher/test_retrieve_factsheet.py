"""
E2E tests for mission dispatch – factsheet retrieval scenarios.

Migrated from packages/controllers/mission/tests/retrieve_factsheet.py (Bazel py_test).
"""
import time
import unittest

import pytest

from cloud_common import objects as api_objects
from cloud_common.objects import robot as robot_object
from packages.controllers.mission.tests import client as simulator

from tests.e2e.mission_dispatcher.conftest import TestContext


@pytest.mark.e2e
class TestRetrieveFactsheet(unittest.TestCase):
    def test_retrieve_factsheet(self):
        """Test if factsheet retrieval is functional"""
        robot_arm = simulator.RobotInit("test01", 0, 0, 0, robot_type="arm")
        robot_amr = simulator.RobotInit("test02", 0, 0, 0, robot_type="amr")
        with TestContext([robot_arm, robot_amr], tick_period=1.0) as ctx:
            ctx.db_client.create(api_objects.RobotObjectV1(name="test01", status={}))
            time.sleep(0.25)
            self.assertGreater(len(ctx.db_client.list(api_objects.RobotObjectV1)), 0)

            time.sleep(2)
            factsheet = ctx.db_client.get(
                robot_object.RobotObjectV1, "test01").status.factsheet
            assert factsheet.agv_class == "FORKLIFT"

            ctx.db_client.create(api_objects.RobotObjectV1(name="test02", status={}))
            time.sleep(0.25)
            self.assertGreater(len(ctx.db_client.list(api_objects.RobotObjectV1)), 1)

            time.sleep(2)
            factsheet = ctx.db_client.get(
                robot_object.RobotObjectV1, "test02").status.factsheet
            assert factsheet.agv_class == "CARRIER"


if __name__ == "__main__":
    unittest.main()
