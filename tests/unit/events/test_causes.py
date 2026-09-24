"""Unit tests for packages/events/causes.py: one case per rule plus the fallback."""

import pytest

from packages.events.causes import CAUSE_CODES, RULES, UNKNOWN, CauseInput, classify, match

pytestmark = pytest.mark.unit


def err(error_type, description="", level="WARNING"):
    return {"errorType": error_type, "errorLevel": level, "errorDescription": description}


# rule name -> an input that rule (and no earlier rule) must match
RULE_CASES = {
    "operator_canceled": CauseInput(outcome="CANCELED", errors=[err("motorFault")]),
    "estop": CauseInput(outcome="FAILED", errors=[err("safety", "Emergency stop pressed")]),
    "heartbeat_lost": CauseInput(outcome="FAILED", heartbeat_lost=True,
                                 nav_reasoning="Low battery, heading home"),
    "low_battery": CauseInput(outcome="FAILED", errors=[err("batteryLow", "", "FATAL")]),
    "thermal": CauseInput(outcome="FAILED", errors=[err("jetson", "GPU overheating")]),
    "motor_fault": CauseInput(outcome="FAILED", errors=[err("motorFault", "left wheel", "FATAL")]),
    "sensor_fault": CauseInput(outcome="FAILED", errors=[err("lidarTimeout", "no scans for 2 s")]),
    "node_crash": CauseInput(outcome="FAILED", errors=[err("nodeDied", "/nav2/controller")]),
    "rtk_lost": CauseInput(outcome="FAILED", nav_reasoning="Pausing: RTK lost, waiting for fix"),
    "no_gnss_fix": CauseInput(outcome="FAILED", nav_reasoning="GNSS no fix"),
    "localization_lost": CauseInput(outcome="FAILED", errors=[err("localizationLost")]),
    "recovery_exhausted": CauseInput(outcome="FAILED",
                                     nav_reasoning="Max recoveries reached, giving up"),
    "path_blocked": CauseInput(outcome="FAILED", errors=[err("edgeBlocked", "e12 blocked")]),
    "goal_unreachable": CauseInput(outcome="FAILED", errors=[err("noRouteError", "")]),
    "order_rejected": CauseInput(outcome="FAILED", errors=[err("validationError", "bad node id")]),
    "map_invalid": CauseInput(outcome="FAILED", errors=[err("mapError", "map mismatch")]),
    "hardware_fault": CauseInput(outcome="FAILED", errors=[err("hardwareError", "", "FATAL")]),
    "mission_timeout": CauseInput(outcome="TIMEOUT"),
}


def test_every_rule_has_a_case():
    assert set(RULE_CASES) == {r.name for r in RULES}


def test_rule_names_are_unique():
    assert len({r.name for r in RULES}) == len(RULES)


@pytest.mark.parametrize("rule_name", list(RULE_CASES))
def test_rule(rule_name):
    rule = match(RULE_CASES[rule_name])
    assert rule is not None and rule.name == rule_name


@pytest.mark.parametrize("inp", [
    CauseInput(),
    CauseInput(outcome="FAILED"),
    CauseInput(outcome="FAILED", errors=[err("somethingNew", "never seen before")],
               nav_reasoning="Driving to waypoint 3"),
    CauseInput(outcome="COMPLETED"),
], ids=["empty", "failed-bare", "unmatched-text", "completed"])
def test_unmatched_input_maps_to_unknown(inp):
    assert match(inp) is None
    assert classify(inp.outcome, inp.errors, inp.nav_reasoning, inp.heartbeat_lost) == UNKNOWN


def test_first_match_wins():
    inp = CauseInput(outcome="TIMEOUT", errors=[err("edgeBlocked")], heartbeat_lost=True)
    assert classify(inp.outcome, inp.errors, heartbeat_lost=True) == "COMMS.HEARTBEAT_LOST"
    assert classify("TIMEOUT", [err("edgeBlocked")]) == "NAV.PATH_BLOCKED"


def test_status_dict_errors_are_accepted():
    assert classify("FAILED", {"edgeBlocked": "e12"}) == "NAV.PATH_BLOCKED"


def test_error_objects_are_accepted():
    class Level:
        value = "FATAL"

    class Vda5050Error:
        errorType = "motorFault"
        errorLevel = Level()
        errorDescription = None

    assert classify("FAILED", [Vda5050Error()]) == "HW.MOTOR_FAULT"


def test_every_rule_cause_is_seeded():
    seeded = {c.code for c in CAUSE_CODES}
    assert {r.cause for r in RULES} <= seeded
    assert UNKNOWN in seeded


def test_seed_codes_are_unique_and_categorised():
    codes = [c.code for c in CAUSE_CODES]
    assert len(codes) == len(set(codes))
    for c in CAUSE_CODES:
        assert c.title
        assert c.code == UNKNOWN or c.code.split(".")[0] == c.category
