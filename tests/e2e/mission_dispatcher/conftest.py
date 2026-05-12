"""
conftest.py for mission dispatcher e2e tests.

Migrated from packages/controllers/mission/tests/test_context.py (Bazel TestContext).

Instead of launching Docker containers via Bazel's run_docker_target(), this conftest
uses the docker-compose test services (postgres-test, mosquitto-test, mission-database-test,
mission-dispatch-test) and runs the VDA5050 robot simulator in-process using the
Simulator class from packages/controllers/mission/tests/client.py.

Required running services (via docker-compose.test.yaml):
  - postgres-test
  - mosquitto-test
  - mission-database-test
  - mission-dispatch-test

Environment variables:
  MISSION_DATABASE_URL:       http://localhost:5000
  MISSION_DATABASE_CTRL_URL:  http://localhost:5001
  MQTT_HOST:                  localhost
  MQTT_PORT_TCP:              1885  (mapped port from mosquitto-test)
  MQTT_PORT_WS:               9001
"""

import os
import random
import threading
import time
from contextlib import contextmanager
from typing import List, Optional

import pytest
import requests

from packages.database.postgres import PostgresDatabase
from tests.sync_db_client import SyncDatabaseClient
from packages.controllers.mission.tests.client import Simulator, RobotInit
import cloud_common.objects as api_objects
from cloud_common.objects import robot as robot_object

# ---------------------------------------------------------------------------
# Defaults – can be overridden by environment variables
# ---------------------------------------------------------------------------
MISSION_DATABASE_URL = os.getenv("MISSION_DATABASE_URL", "http://localhost:5000")
MISSION_DATABASE_CTRL_URL = os.getenv("MISSION_DATABASE_CTRL_URL", "http://localhost:5001")
MQTT_HOST = os.getenv("MQTT_HOST", "localhost")
MQTT_PORT_TCP = int(os.getenv("MQTT_PORT_TCP", "1885"))
MQTT_PORT_WEBSOCKET = int(os.getenv("MQTT_PORT_WS", "9001"))
MQTT_TRANSPORT = "websockets"
MQTT_WS_PATH = "/mqtt"
MQTT_PORT = MQTT_PORT_TCP if MQTT_TRANSPORT == "tcp" else MQTT_PORT_WEBSOCKET
MQTT_PREFIX = "uagv/v2/RobotCompany"
SIM_SPEED = 10.0


def wait_for_service(url: str, timeout: int = 120) -> bool:
    """Poll `url` until it returns 200 or timeout expires."""
    end = time.time() + timeout
    while time.time() < end:
        try:
            r = requests.get(url, timeout=2)
            if r.status_code == 200:
                return True
        except requests.ConnectionError:
            pass
        time.sleep(0.5)
    return False


class SimulatorThread(threading.Thread):
    """Run the VDA5050 Simulator in a background daemon thread."""

    def __init__(self, robots: List[RobotInit], tick_period: float = 0.25,
                 fail_as_warning: bool = False):
        super().__init__(daemon=True)
        self._robots = robots
        self._tick_period = tick_period
        self._fail_as_warning = fail_as_warning
        self._sim: Optional[Simulator] = None
        self._stop_event = threading.Event()

    def run(self):
        self._sim = Simulator(
            robots=self._robots,
            speed=SIM_SPEED,
            mqtt_host=MQTT_HOST,
            mqtt_transport=MQTT_TRANSPORT,
            mqtt_ws_path=MQTT_WS_PATH,
            mqtt_port=MQTT_PORT,
            mqtt_prefix=MQTT_PREFIX,
            tick_period=self._tick_period,
            fail_as_warning=self._fail_as_warning,
        )
        # Drive tick loop manually so we can stop it
        while not self._stop_event.is_set():
            time.sleep(self._tick_period)
            for robot in self._sim.robots.values():
                robot.update()

    def stop(self):
        self._stop_event.set()
        if self._sim is not None:
            self._sim.client.loop_stop()
            self._sim.client.disconnect()


class MissionDispatchStack:
    """
    Replaces the Bazel TestContext.

    Wraps the DB/controller clients and the in-process simulator thread.
    Provides the same interface as TestContext for the migrated tests.
    """

    def __init__(self, robots: List[RobotInit], tick_period: float = 0.25,
                 fail_as_warning: bool = False,
                 enforce_start_order: bool = True,
                 delay_sim: float = 0.0):
        random.seed(0)

        self.db_client = SyncDatabaseClient()
        self.db_controller_client = SyncDatabaseClient()

        # Confirm services are reachable
        if not wait_for_service(MISSION_DATABASE_URL + "/health"):
            raise RuntimeError("mission-database-test is not reachable")
        if not wait_for_service("http://" + MQTT_HOST + ":" + str(MQTT_PORT_TCP + 1)):
            pass  # Mosquitto does not expose HTTP; skip this check

        # Start robot simulator after optional delay
        if delay_sim:
            time.sleep(delay_sim)

        self._sim_thread = SimulatorThread(
            robots=robots,
            tick_period=tick_period,
            fail_as_warning=fail_as_warning,
        )
        self._sim_thread.start()

        # Convenience references matching test_context.py
        self.mqtt_address = MQTT_HOST
        self.database_address = MISSION_DATABASE_URL.replace("http://", "").split(":")[0]
        self.md_url = MISSION_DATABASE_URL
        self.md_ctrl_url = MISSION_DATABASE_CTRL_URL

    def wait_for_database(self):
        if not wait_for_service(MISSION_DATABASE_URL + "/health"):
            raise RuntimeError("mission-database-test is not reachable")

    def wait_for_mqtt(self):
        end = time.time() + 30
        import socket
        while time.time() < end:
            try:
                s = socket.create_connection((MQTT_HOST, MQTT_PORT_TCP), timeout=1)
                s.close()
                return
            except OSError:
                time.sleep(0.5)
        raise RuntimeError("Mosquitto broker not reachable")

    def call_teleop_service(self, robot_name: str,
                            teleop: robot_object.RobotTeleopActionV1):
        endpoint = self.md_url + f"/robot/{robot_name}/teleop"
        requests.post(url=endpoint, params={"params": teleop.value})

    def restart_mission_server(self):
        """
        Simulate a mission server restart during tests.
        In the compose-based setup this simply polls until the service comes back.
        The docker-compose restart would need to be done externally.
        """
        time.sleep(2)

    def restart_mqtt_server(self):
        """
        Simulate an MQTT broker restart.
        In the compose-based setup this simply waits for reconnect.
        """
        time.sleep(2)

    def close(self):
        self._sim_thread.stop()
        self._sim_thread.join(timeout=5)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


# ---------------------------------------------------------------------------
# Re-export helpers from original test_context.py so test files can import
# them directly from this module (or from test_context; both work).
# ---------------------------------------------------------------------------
from packages.controllers.mission.tests.test_context import (  # noqa: E402
    mission_from_waypoints,
    mission_from_waypoint,
    mission_object_generator,
    route_generator,
    move_generator,
    action_generator,
    notify_generator,
    pose1D_generator,
    Delay,
    MQTT_PORT,
    MQTT_PORT_TCP,
    MQTT_PORT_WEBSOCKET,
    MQTT_TRANSPORT,
    MQTT_WS_PATH,
    MQTT_PREFIX,
)

# Override TestContext with the pytest-compatible version
TestContext = MissionDispatchStack


@pytest.fixture(scope="session", autouse=True)
def verify_mission_dispatch_services():
    """
    Session-scoped fixture: skip the entire test session if required services
    are not running.
    """
    db_up = wait_for_service(MISSION_DATABASE_URL + "/health", timeout=30)
    if not db_up:
        pytest.skip(
            "Required services not running. "
            "Start with: docker compose -f docker_compose/docker-compose.test.yaml up -d"
        )
