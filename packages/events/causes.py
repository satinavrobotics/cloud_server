"""Cause classification (docs/satinav-fleet-agent-phase0-v2.md §3.7).

`CAUSE_CODES` is the seed for the `cause_codes` table; the Alembic migration
must insert exactly these rows. `RULES` is an ordered first-match list over a
run's outcome, the robot's raw VDA5050 errors and its navReasoning line.
"""

import dataclasses
import re
from typing import Any, Callable, List, Mapping, Optional, Tuple

UNKNOWN = "UNKNOWN"


@dataclasses.dataclass(frozen=True)
class CauseCode:
    code: str
    category: str
    title: str
    description: Optional[str] = None


CAUSE_CODES: Tuple[CauseCode, ...] = (
    CauseCode("NAV.GOAL_UNREACHABLE", "NAV", "Goal unreachable",
              "No path to the goal could be planned."),
    CauseCode("NAV.RECOVERY_EXHAUSTED", "NAV", "Recovery exhausted",
              "Navigation recovery behaviours ran out without success."),
    CauseCode("NAV.PATH_BLOCKED", "NAV", "Path blocked",
              "An edge on the route was blocked by an obstacle."),
    CauseCode("NAV.LOCALIZATION_LOST", "NAV", "Localization lost",
              "The robot lost its position estimate."),
    CauseCode("GNSS.RTK_LOST", "GNSS", "RTK fix lost",
              "GNSS dropped out of RTK fixed mode."),
    CauseCode("GNSS.NO_FIX", "GNSS", "No GNSS fix", "GNSS had no position fix."),
    CauseCode("POWER.LOW_BATTERY", "POWER", "Low battery", "Battery level too low to continue."),
    CauseCode("COMMS.HEARTBEAT_LOST", "COMMS", "Heartbeat lost",
              "The robot stopped reporting state within the heartbeat timeout."),
    CauseCode("OPERATOR.CANCELED", "OPERATOR", "Canceled by operator", None),
    CauseCode("OPERATOR.ESTOP", "OPERATOR", "Emergency stop", "An emergency stop was triggered."),
    CauseCode("DISPATCH.TIMEOUT", "DISPATCH", "Mission timeout",
              "The mission exceeded its time limit."),
    CauseCode("DISPATCH.ORPHANED", "DISPATCH", "Orphaned run",
              "The robot no longer reported the run's order after a dispatcher restart."),
    CauseCode("DISPATCH.ORDER_REJECTED", "DISPATCH", "Order rejected",
              "The robot rejected the VDA5050 order."),
    CauseCode("MAP.INVALID", "MAP", "Invalid map", "The map is missing or does not match."),
    CauseCode("HW.FAULT", "HW", "Hardware fault", None),
    CauseCode("HW.MOTOR_FAULT", "HW", "Motor fault", None),
    CauseCode("HW.SENSOR_FAULT", "HW", "Sensor fault", None),
    CauseCode("SW.NODE_CRASH", "SW", "Software node crash", "A robot software node died."),
    CauseCode("SYSTEM.THERMAL", "SYSTEM", "Overheating", "The robot's compute overheated."),
    CauseCode(UNKNOWN, "UNKNOWN", "Unknown cause", "No rule matched."),
)


@dataclasses.dataclass(frozen=True)
class CauseInput:
    """Everything the rules look at. `errors` accepts VDA5050 error objects,
    dicts with errorType/errorLevel/errorDescription, or the dispatcher's
    {errorType: description} status dict."""
    outcome: Optional[str] = None
    errors: Any = ()
    nav_reasoning: Optional[str] = None
    heartbeat_lost: bool = False


@dataclasses.dataclass(frozen=True)
class _Error:
    type: str
    level: str
    description: str


def _field(error: Any, name: str) -> str:
    value = error.get(name) if isinstance(error, Mapping) else getattr(error, name, None)
    if value is not None and hasattr(value, "value"):
        value = value.value
    return "" if value is None else str(value)


def _normalize_errors(errors: Any) -> List[_Error]:
    if not errors:
        return []
    if isinstance(errors, Mapping):
        return [_Error(str(k), "", "" if v is None else str(v)) for k, v in errors.items()]
    return [_Error(_field(e, "errorType"), _field(e, "errorLevel"),
                   _field(e, "errorDescription")) for e in errors]


class _Facts:
    def __init__(self, inp: CauseInput):
        self.outcome = (inp.outcome.value if hasattr(inp.outcome, "value")
                        else inp.outcome or "").upper()
        self.errors = _normalize_errors(inp.errors)
        self.error_types = {e.type.lower() for e in self.errors}
        self.text = " | ".join(
            [f"{e.type} {e.description}" for e in self.errors] + [inp.nav_reasoning or ""]
        ).lower()
        self.heartbeat_lost = inp.heartbeat_lost

    def mentions(self, pattern: str) -> bool:
        return re.search(pattern, self.text) is not None


@dataclasses.dataclass(frozen=True)
class Rule:
    name: str
    cause: str
    matches: Callable[[_Facts], bool]


RULES: Tuple[Rule, ...] = (
    Rule("operator_canceled", "OPERATOR.CANCELED", lambda f: f.outcome == "CANCELED"),
    Rule("estop", "OPERATOR.ESTOP", lambda f: f.mentions(r"e-?stop|emergency[ _.-]?stop")),
    Rule("heartbeat_lost", "COMMS.HEARTBEAT_LOST", lambda f: f.heartbeat_lost),
    Rule("low_battery", "POWER.LOW_BATTERY",
         lambda f: f.mentions(r"battery[ _.-]?(low|critical|empty)|low[ _.-]?battery")),
    Rule("thermal", "SYSTEM.THERMAL", lambda f: f.mentions(r"overheat|thermal|over[ _-]?temp")),
    Rule("motor_fault", "HW.MOTOR_FAULT", lambda f: f.mentions(r"motor|drive[ _-]?fault")),
    Rule("sensor_fault", "HW.SENSOR_FAULT",
         lambda f: f.mentions(r"(lidar|camera|imu|sensor)\w*[ _-]?(fault|fail|error|timeout)")),
    Rule("node_crash", "SW.NODE_CRASH",
         lambda f: f.mentions(r"node\w*[ _-]?(crash|died|down)|process (died|crashed)")),
    Rule("rtk_lost", "GNSS.RTK_LOST", lambda f: f.mentions(r"rtk[ _-]?(lost|float|drop)")),
    Rule("no_gnss_fix", "GNSS.NO_FIX", lambda f: f.mentions(r"(gnss|gps)[ _-]?(no[ _-]?fix|lost)")),
    Rule("localization_lost", "NAV.LOCALIZATION_LOST",
         lambda f: f.mentions(r"locali[sz]ation[ _-]?(lost|fail)|lost[ _-]?locali[sz]ation")),
    Rule("recovery_exhausted", "NAV.RECOVERY_EXHAUSTED",
         lambda f: f.mentions(r"recover\w*[ _-]?(exhausted|failed)|max[ _-]?recover")),
    Rule("path_blocked", "NAV.PATH_BLOCKED",
         lambda f: "edgeblocked" in f.error_types or f.mentions(r"path[ _-]?blocked")),
    Rule("goal_unreachable", "NAV.GOAL_UNREACHABLE",
         lambda f: "norouteerror" in f.error_types
         or f.mentions(r"unreachable|no[ _-]?path|planning[ _-]?failed")),
    Rule("order_rejected", "DISPATCH.ORDER_REJECTED",
         lambda f: bool(f.error_types & {"ordererror", "orderupdateerror", "validationerror"})),
    Rule("map_invalid", "MAP.INVALID", lambda f: f.mentions(r"map[ _-]?(invalid|mismatch|not found|unknown)")),
    Rule("hardware_fault", "HW.FAULT", lambda f: f.mentions(r"hardware|\bhw\b")),
    Rule("mission_timeout", "DISPATCH.TIMEOUT", lambda f: f.outcome == "TIMEOUT"),
)


def match(inp: CauseInput) -> Optional[Rule]:
    facts = _Facts(inp)
    for rule in RULES:
        if rule.matches(facts):
            return rule
    return None


def classify(outcome: Optional[str] = None, errors: Any = (),
             nav_reasoning: Optional[str] = None, heartbeat_lost: bool = False) -> str:
    rule = match(CauseInput(outcome, errors, nav_reasoning, heartbeat_lost))
    return rule.cause if rule is not None else UNKNOWN
