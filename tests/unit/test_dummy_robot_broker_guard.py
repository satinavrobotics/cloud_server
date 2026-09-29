"""The dummy robot's live-broker test must not default to localhost (the production broker)."""

import importlib
import os
import sys
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit

MODULE = "tests.dummy_robot.test_dummy_robot"


def _load(env):
    sys.modules.pop(MODULE, None)
    with patch.dict(os.environ, env, clear=False):
        for key in ("TEST_MQTT_HOST", "TEST_MQTT_PORT"):
            if key not in env:
                os.environ.pop(key, None)
        return importlib.import_module(MODULE)


def test_no_broker_configured_means_skipped_never_localhost():
    try:
        mod = _load({})
        assert mod.TEST_MQTT_HOST == ""
        assert getattr(mod.TestDummyRobotIntegration, "__unittest_skip__", False)
        assert "TEST_MQTT_HOST" in mod.TestDummyRobotIntegration.__unittest_skip_why__
    finally:
        sys.modules.pop(MODULE, None)


def test_broker_from_env_is_used():
    try:
        mod = _load({"TEST_MQTT_HOST": "broker.test", "TEST_MQTT_PORT": "1999"})
        assert (mod.TEST_MQTT_HOST, mod.TEST_MQTT_PORT) == ("broker.test", 1999)
        assert not getattr(mod.TestDummyRobotIntegration, "__unittest_skip__", False)
    finally:
        sys.modules.pop(MODULE, None)


def test_no_hardcoded_localhost_broker_left():
    with open(MODULE.replace(".", "/") + ".py") as f:
        assert 'mqtt_host="localhost"' not in f.read()
