"""Event code registry (docs/satinav-fleet-agent-phase0-v2.md §3.3).

Codes are append-only: never rename or reuse one, only deprecate it.
"""

import dataclasses
import enum
from typing import Dict, Type

from packages.events import schemas


class EventCode(str, enum.Enum):
    MISSION_RUN_STARTED = "MISSION.RUN_STARTED"
    MISSION_RUN_FINISHED = "MISSION.RUN_FINISHED"
    MISSION_NODE_FAILED = "MISSION.NODE_FAILED"
    MISSION_REROUTED = "MISSION.REROUTED"
    MISSION_EDGE_BLOCKED = "MISSION.EDGE_BLOCKED"
    MISSION_CANCEL_REQUESTED = "MISSION.CANCEL_REQUESTED"
    ROBOT_STATE_CHANGED = "ROBOT.STATE_CHANGED"
    ROBOT_ONLINE = "ROBOT.ONLINE"
    ROBOT_OFFLINE = "ROBOT.OFFLINE"
    ROBOT_HEARTBEAT_LOST = "ROBOT.HEARTBEAT_LOST"
    ROBOT_HEARTBEAT_RESTORED = "ROBOT.HEARTBEAT_RESTORED"
    ROBOT_ERROR_RAISED = "ROBOT.ERROR_RAISED"
    ROBOT_ERROR_CLEARED = "ROBOT.ERROR_CLEARED"
    ROBOT_SW_VERSION_CHANGED = "ROBOT.SW_VERSION_CHANGED"
    BATTERY_LOW = "BATTERY.LOW"
    BATTERY_OK = "BATTERY.OK"
    GNSS_RTK_LOST = "GNSS.RTK_LOST"
    GNSS_RTK_RECOVERED = "GNSS.RTK_RECOVERED"
    NAV_RECOVERY_ENTERED = "NAV.RECOVERY_ENTERED"
    NAV_RECOVERY_EXITED = "NAV.RECOVERY_EXITED"
    NAV_GOAL_BLOCKED = "NAV.GOAL_BLOCKED"
    SYSTEM_THERMAL_HIGH = "SYSTEM.THERMAL_HIGH"
    SYSTEM_THERMAL_OK = "SYSTEM.THERMAL_OK"
    SYSTEM_NODE_DOWN = "SYSTEM.NODE_DOWN"
    SYSTEM_NODE_UP = "SYSTEM.NODE_UP"
    MAP_DELETE_FAILED = "MAP.DELETE_FAILED"
    TELEMETRY_RECORDING_CHANGED = "TELEMETRY.RECORDING_CHANGED"


class Severity(str, enum.Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class Source(str, enum.Enum):
    DISPATCH = "dispatch"
    API = "api"


@dataclasses.dataclass(frozen=True)
class CodeMeta:
    severity: Severity
    discriminator_required: bool
    payload_model: Type[schemas.Payload]
    source: Source


_C = EventCode
_S = Severity
_D = Source.DISPATCH
_A = Source.API

# The discriminator is required wherever two events with the same code, robot and
# timestamp can legitimately differ (several errors or nodes in one message), and
# for codes that have no robot.
CODES: Dict[EventCode, CodeMeta] = {
    _C.MISSION_RUN_STARTED: CodeMeta(_S.INFO, True, schemas.RunStarted, _D),
    _C.MISSION_RUN_FINISHED: CodeMeta(_S.INFO, True, schemas.RunFinished, _D),
    _C.MISSION_NODE_FAILED: CodeMeta(_S.ERROR, True, schemas.NodeFailed, _D),
    _C.MISSION_REROUTED: CodeMeta(_S.INFO, False, schemas.Rerouted, _D),
    _C.MISSION_EDGE_BLOCKED: CodeMeta(_S.WARNING, True, schemas.EdgeBlocked, _D),
    _C.MISSION_CANCEL_REQUESTED: CodeMeta(_S.INFO, True, schemas.CancelRequested, _A),
    _C.ROBOT_STATE_CHANGED: CodeMeta(_S.INFO, False, schemas.StateChanged, _D),
    _C.ROBOT_ONLINE: CodeMeta(_S.INFO, False, schemas.Connection, _D),
    _C.ROBOT_OFFLINE: CodeMeta(_S.WARNING, False, schemas.Connection, _D),
    _C.ROBOT_HEARTBEAT_LOST: CodeMeta(_S.ERROR, False, schemas.HeartbeatLost, _D),
    _C.ROBOT_HEARTBEAT_RESTORED: CodeMeta(_S.INFO, False, schemas.HeartbeatRestored, _D),
    _C.ROBOT_ERROR_RAISED: CodeMeta(_S.ERROR, True, schemas.ErrorRaised, _D),
    _C.ROBOT_ERROR_CLEARED: CodeMeta(_S.INFO, True, schemas.ErrorCleared, _D),
    _C.ROBOT_SW_VERSION_CHANGED: CodeMeta(_S.INFO, False, schemas.SwVersionChanged, _D),
    _C.BATTERY_LOW: CodeMeta(_S.WARNING, False, schemas.Battery, _D),
    _C.BATTERY_OK: CodeMeta(_S.INFO, False, schemas.Battery, _D),
    _C.GNSS_RTK_LOST: CodeMeta(_S.WARNING, False, schemas.Rtk, _A),
    _C.GNSS_RTK_RECOVERED: CodeMeta(_S.INFO, False, schemas.Rtk, _A),
    _C.NAV_RECOVERY_ENTERED: CodeMeta(_S.WARNING, False, schemas.RecoveryEntered, _A),
    _C.NAV_RECOVERY_EXITED: CodeMeta(_S.INFO, False, schemas.RecoveryExited, _A),
    _C.NAV_GOAL_BLOCKED: CodeMeta(_S.WARNING, False, schemas.GoalBlocked, _A),
    _C.SYSTEM_THERMAL_HIGH: CodeMeta(_S.WARNING, False, schemas.Thermal, _A),
    _C.SYSTEM_THERMAL_OK: CodeMeta(_S.INFO, False, schemas.Thermal, _A),
    _C.SYSTEM_NODE_DOWN: CodeMeta(_S.ERROR, True, schemas.RosNode, _A),
    _C.SYSTEM_NODE_UP: CodeMeta(_S.INFO, True, schemas.RosNode, _A),
    _C.MAP_DELETE_FAILED: CodeMeta(_S.ERROR, True, schemas.MapDeleteFailed, _A),
    _C.TELEMETRY_RECORDING_CHANGED: CodeMeta(_S.INFO, True, schemas.RecordingChanged, _A),
}


def meta_for(code: EventCode) -> CodeMeta:
    return CODES[EventCode(code)]
