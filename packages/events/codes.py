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
    MISSION_DELETED = "MISSION.DELETED"
    RUN_ARCHIVED = "RUN.ARCHIVED"
    RUN_UNARCHIVED = "RUN.UNARCHIVED"
    TELEMETRY_RECORDING_CHANGED = "TELEMETRY.RECORDING_CHANGED"
    SYSTEM_RECORDER_ALERT_RAISED = "SYSTEM.RECORDER_ALERT_RAISED"
    SYSTEM_RECORDER_ALERT_CLEARED = "SYSTEM.RECORDER_ALERT_CLEARED"
    MAP_CREATED = "MAP.CREATED"
    MAP_ARCHIVED = "MAP.ARCHIVED"
    MAP_RESTORED = "MAP.RESTORED"
    MAP_SESSION_STARTED = "MAP.SESSION_STARTED"
    MAP_SESSION_PAUSED = "MAP.SESSION_PAUSED"
    MAP_SESSION_RESUMED = "MAP.SESSION_RESUMED"
    MAP_SESSION_FINISHED = "MAP.SESSION_FINISHED"
    MAP_DELETED = "MAP.DELETED"
    MAP_INGEST_REJECTED = "MAP.INGEST_REJECTED"
    MAP_SESSION_REALIGNED = "MAP.SESSION_REALIGNED"
    MAP_SESSION_PLACED = "MAP.SESSION_PLACED"
    MAP_SESSION_UNPLACED = "MAP.SESSION_UNPLACED"
    MAP_TYPE_CHANGED = "MAP.TYPE_CHANGED"
    MAP_SLAM_CHANGED = "MAP.SLAM_CHANGED"
    MAP_RECONSTRUCTION_STARTED = "MAP.RECONSTRUCTION_STARTED"
    MAP_RECONSTRUCTION_FINISHED = "MAP.RECONSTRUCTION_FINISHED"
    MAP_RECONSTRUCTION_FAILED = "MAP.RECONSTRUCTION_FAILED"


class Severity(str, enum.Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class Source(str, enum.Enum):
    DISPATCH = "dispatch"
    API = "api"
    # Maps redesign M2: MAP.INGEST_REJECTED (packages/services/graph_builder/ingest.py).
    # fleet_events_source_check allows it from migration 20260929_01_maps_m2 on.
    GRAPH_BUILDER = "graph_builder"
    # 3D reconstruction R3: MAP.RECONSTRUCTION_* (packages/api/reconstruction.py, the gateway
    # in the API). Allowed from migration 20261003_01_map_reconstructions on.
    RECONSTRUCTION = "reconstruction"


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
_G = Source.GRAPH_BUILDER
_R = Source.RECONSTRUCTION

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
    _C.MISSION_DELETED: CodeMeta(_S.INFO, True, schemas.MissionDeleted, _A),
    _C.RUN_ARCHIVED: CodeMeta(_S.INFO, True, schemas.RunsArchived, _A),
    _C.RUN_UNARCHIVED: CodeMeta(_S.INFO, True, schemas.RunsArchived, _A),
    _C.TELEMETRY_RECORDING_CHANGED: CodeMeta(_S.INFO, True, schemas.RecordingChanged, _A),
    _C.SYSTEM_RECORDER_ALERT_RAISED: CodeMeta(_S.WARNING, True, schemas.RecorderAlert, _A),
    _C.SYSTEM_RECORDER_ALERT_CLEARED: CodeMeta(_S.INFO, True, schemas.RecorderAlert, _A),
    # Maps redesign M1 (packages/api/maps.py). Discriminators: `map:<name>:<state>` for the
    # map codes (no robot), `session:<session_id>:<action>` for the session codes.
    _C.MAP_CREATED: CodeMeta(_S.INFO, True, schemas.MapLifecycle, _A),
    _C.MAP_ARCHIVED: CodeMeta(_S.INFO, True, schemas.MapLifecycle, _A),
    _C.MAP_RESTORED: CodeMeta(_S.INFO, True, schemas.MapLifecycle, _A),
    _C.MAP_SESSION_STARTED: CodeMeta(_S.INFO, True, schemas.MapSession, _A),
    _C.MAP_SESSION_PAUSED: CodeMeta(_S.INFO, True, schemas.MapSession, _A),
    _C.MAP_SESSION_RESUMED: CodeMeta(_S.INFO, True, schemas.MapSession, _A),
    _C.MAP_SESSION_FINISHED: CodeMeta(_S.INFO, True, schemas.MapSession, _A),
    # Maps redesign M2. MAP.DELETED: discriminator `map:<name>:deleted:<requested_at>`
    # (packages/api/map_delete.py); MAP.INGEST_REJECTED: `ingest:<reason>`, rate-limited per
    # robot and reason (packages/services/graph_builder/ingest.py).
    _C.MAP_DELETED: CodeMeta(_S.INFO, True, schemas.MapDeleted, _A),
    _C.MAP_INGEST_REJECTED: CodeMeta(_S.WARNING, True, schemas.MapIngestRejected, _G),
    # A geo session re-anchored to the robot's new datum after a robot restart (graph-builder).
    # Discriminator `session:<id>:realigned:<tx>:<ty>:<yaw>`.
    _C.MAP_SESSION_REALIGNED: CodeMeta(_S.INFO, True, schemas.MapSessionRealigned, _G),
    # Maps §14 (operate sessions and placement). PLACED: the user put the robot on a local map
    # (packages/api/maps.py; discriminator `session:<id>:placed:<tx>:<ty>:<yaw>:<ts>`).
    # UNPLACED: the robot's run frame reset (a restart), so its session's map_T_session is no
    # longer valid (mission-dispatch; `session:<id>:unplaced:<ts>`). A geo session re-placed
    # from the new datum is MAP.SESSION_REALIGNED with source dispatch.
    _C.MAP_SESSION_PLACED: CodeMeta(_S.INFO, True, schemas.MapSessionPlaced, _A),
    _C.MAP_SESSION_UNPLACED: CodeMeta(_S.WARNING, True, schemas.MapSessionUnplaced, _D),
    # A map converted geo <-> local (POST /api/v1/maps/{id}/type, packages/api/maps.py). No robot;
    # discriminator `map:<name>:type:<new type>:<ts>`.
    _C.MAP_TYPE_CHANGED: CodeMeta(_S.INFO, True, schemas.MapTypeChanged, _A),
    # slam_map of a local map switched on/off (PATCH /api/v1/maps/{id}, packages/api/maps.py).
    # No robot; discriminator `map:<name>:slam:<bool>:<ts>`.
    _C.MAP_SLAM_CHANGED: CodeMeta(_S.INFO, True, schemas.MapSlamChanged, _A),
    # 3D reconstruction (docs/reconstruction/design.md §9.3). No robot; discriminator
    # `map:<name>:reconstruction:<job_id>:<state>`. STARTED on the job's first progress
    # callback; FAILED also for a cancel (reason cancelled / map_deleting).
    _C.MAP_RECONSTRUCTION_STARTED: CodeMeta(_S.INFO, True, schemas.ReconstructionStarted, _R),
    _C.MAP_RECONSTRUCTION_FINISHED: CodeMeta(_S.INFO, True, schemas.ReconstructionFinished,
                                             _R),
    _C.MAP_RECONSTRUCTION_FAILED: CodeMeta(_S.WARNING, True, schemas.ReconstructionFailed, _R),
}


def meta_for(code: EventCode) -> CodeMeta:
    return CODES[EventCode(code)]
