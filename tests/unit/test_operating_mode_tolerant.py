"""An unknown operatingMode keeps the state message (C15)."""
import logging

import pytest

from packages.controllers.mission.vda5050_types import vda5050_types as t

pytestmark = pytest.mark.unit


def _state(mode):
    return t.VDA5050State(
        headerId=1, timestamp="2026-10-01T00:00:00Z", version="2.0.0", manufacturer="m",
        serialNumber="s", nodeStates=[], edgeStates=[], operatingMode=mode)


def test_known_mode_is_still_the_enum():
    assert _state("MANUAL").operatingMode is t.VDA5050OperatingMode.MANUAL
    assert _state("").operatingMode is t.VDA5050OperatingMode.AUTOMATIC


def test_unknown_mode_keeps_the_message_and_logs_once(caplog):
    t._UNKNOWN_MODES_LOGGED.discard("TELEOPERATION")
    with caplog.at_level(logging.WARNING):
        a = _state("TELEOPERATION")
        b = _state("TELEOPERATION")
    assert a.operatingMode == "TELEOPERATION" and b.operatingMode == "TELEOPERATION"
    assert sum("TELEOPERATION" in r.getMessage() for r in caplog.records) == 1
