"""Payload models, one per event code (docs/satinav-fleet-agent-phase0-v2.md §3.3).

Payloads are validated in one of two modes. Lenient (the default, for
production) logs the error and returns the raw payload flagged with
`_invalid: true`, so the event is still stored. Strict (for tests) raises.
The mode is set explicitly with `set_strict_validation()` or per call.
"""

import datetime
import enum
import json
import logging
from typing import Any, Dict, List, Mapping, Optional, Type

import pydantic

logger = logging.getLogger(__name__)

INVALID_KEY = "_invalid"
INVALID_ERROR_KEY = "_error"


class InvalidPayloadError(ValueError):
    pass


class RunOutcome(str, enum.Enum):
    """Terminal mission_runs states. COMPLETED (not SUCCEEDED) matches MissionStateV1."""
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELED = "CANCELED"
    ABORTED = "ABORTED"
    TIMEOUT = "TIMEOUT"


class RecordingLevel(str, enum.Enum):
    FULL = "full"
    EVENTS_ONLY = "events_only"
    OFF = "off"


class RecordingScope(str, enum.Enum):
    ROBOT = "robot"
    SITE = "site"
    GLOBAL = "global"


class Payload(pydantic.BaseModel):
    class Config:
        extra = pydantic.Extra.forbid


class EmptyPayload(Payload):
    pass


class RunStarted(Payload):
    mission_name: str
    map_id: Optional[str] = None
    recording_level: Optional[RecordingLevel] = None


class RunFinished(Payload):
    mission_name: str
    outcome: RunOutcome
    cause: Optional[str] = None
    passes_completed: Optional[int] = None
    duration_s: Optional[float] = None


class NodeFailed(Payload):
    mission_name: str
    node_id: str
    node_type: Optional[str] = None
    detail: Optional[str] = None


class Rerouted(Payload):
    mission_name: Optional[str] = None
    blocked_edges: List[str] = []
    detail: Optional[str] = None


class EdgeBlocked(Payload):
    mission_name: Optional[str] = None
    edge_id: Optional[str] = None
    detail: Optional[str] = None


class CancelRequested(Payload):
    mission_name: str
    actor: str


class StateChanged(Payload):
    old: Optional[str] = None
    new: str


class Connection(Payload):
    connection_state: Optional[str] = None


class HeartbeatLost(Payload):
    last_seen: datetime.datetime
    timeout_s: float


class HeartbeatRestored(Payload):
    last_seen: Optional[datetime.datetime] = None
    gap_s: float


class ErrorRaised(Payload):
    error_type: str
    error_level: Optional[str] = None
    description: Optional[str] = None


class ErrorCleared(Payload):
    error_type: str
    description: Optional[str] = None


class SwVersionChanged(Payload):
    old: Optional[str] = None
    new: str


class Battery(Payload):
    battery_percent: float
    threshold: float


class Rtk(Payload):
    old_fix: Optional[str] = None
    new_fix: Optional[str] = None
    sats: Optional[int] = None
    h_acc_m: Optional[float] = None


class RecoveryEntered(Payload):
    cause: Optional[str] = None


class RecoveryExited(Payload):
    cause: Optional[str] = None
    duration_s: Optional[float] = None


class GoalBlocked(Payload):
    cause: str
    detail: Optional[str] = None


class Thermal(Payload):
    temp_c: float
    threshold_c: float
    sensor: Optional[str] = None


class RosNode(Payload):
    node: str


class MapDeleteFailed(Payload):
    map_name: str
    attempts: int
    error: Optional[str] = None


class MissionDeleted(Payload):
    """DELETE /api/v1/missions/{name} (packages/api/run_admin.py). `run_ids` lists at most
    run_admin.EVENT_MAX_RUN_IDS of the deleted runs (`run_ids_truncated` says if more)."""
    mission_name: str
    with_reruns: bool = False
    deleted_missions: List[str] = []
    deleted_runs: int
    deleted_events: int
    deleted_trajectory: int
    run_ids: List[str] = []
    run_ids_truncated: bool = False
    robots: List[str] = []


class RunsArchived(Payload):
    """POST /api/v1/runs/archive: the runs whose archive state changed."""
    count: int
    run_ids: List[str] = []
    run_ids_truncated: bool = False
    mission: Optional[str] = None


class RecorderAlert(Payload):
    """A recorder health alert started or ended (packages/api/recorder_health.py, WP13).
    `value` is the measurement that crossed `threshold` (null when there is none, e.g. a
    process that never reported); `raised_at` / `duration_s` are set on CLEARED."""
    alert: str
    process: str
    value: Optional[float] = None
    threshold: float
    raised_at: Optional[datetime.datetime] = None
    duration_s: Optional[float] = None


class MapLifecycle(Payload):
    """MAP.CREATED / MAP.ARCHIVED / MAP.RESTORED (packages/api/maps.py): the map's state after
    the change."""
    map_name: str
    map_type: Optional[str] = None
    state: str
    actor: Optional[str] = None


class MapSession(Payload):
    """MAP.SESSION_STARTED / _PAUSED / _RESUMED / _FINISHED (packages/api/maps.py)."""
    map_name: str
    session_id: str
    map_state: str
    # Maps §14: `mapping` | `operate` (absent on events written before U1: mapping).
    purpose: Optional[str] = None
    services: Optional[List[str]] = None
    aligned: Optional[bool] = None
    map_T_session: Optional[Dict[str, float]] = None
    placement: Optional[Dict[str, Any]] = None
    actor: Optional[str] = None


class MapSessionRealigned(Payload):
    """MAP.SESSION_REALIGNED: a geo session's map_T_session was re-derived from the robot's
    datum. graph-builder (packages/services/graph_builder/ingest.py) when a node arrives after
    the datum changed; mission-dispatch (maps §14, U3) on the datum write itself, which also
    re-places a session the run change had unplaced (`reason` run_changed; `datum` changed
    for a datum change alone). Nodes stored before keep their map-frame poses."""
    map_name: str
    session_id: str
    map_state: Optional[str] = None
    aligned: Optional[bool] = None
    map_T_session: Dict[str, float]
    old_map_T_session: Optional[Dict[str, float]] = None
    datum: Optional[Dict[str, Any]] = None
    old_datum: Optional[Dict[str, Any]] = None
    purpose: Optional[str] = None
    reason: Optional[str] = None


class MapSessionPlaced(Payload):
    """MAP.SESSION_PLACED (packages/api/maps.py, maps §14): the user placed the robot on a local
    map: map_T_session = placed pose (+) robot pose^-1. `placement`: {pose, robot_pose, source,
    actor, at}."""
    map_name: str
    session_id: str
    purpose: Optional[str] = None
    map_T_session: Dict[str, float]
    old_map_T_session: Optional[Dict[str, float]] = None
    placement: Optional[Dict[str, Any]] = None


class MapSessionUnplaced(Payload):
    """MAP.SESSION_UNPLACED (mission-dispatch, maps §14 U3): the robot's run frame reset, so its
    open session is no longer placed. `reason` run_changed; `evidence`: what showed it (the
    VDA5050 header ids: `connection_header_id` / `last_connection_header_id`, or
    `state_header_id` / `last_state_header_id`)."""
    map_name: str
    session_id: str
    purpose: Optional[str] = None
    reason: str
    evidence: Optional[Dict[str, Any]] = None
    old_map_T_session: Optional[Dict[str, float]] = None


class MapDeleted(Payload):
    """MAP.DELETED (packages/api/map_delete.py): the background delete finished; the map's
    Postgres row, sessions, ArangoDB graph and MinIO bucket are gone."""
    map_name: str
    requested_at: Optional[datetime.datetime] = None
    attempts: int = 0


class MapIngestRejected(Payload):
    """MAP.INGEST_REJECTED (packages/services/graph_builder/ingest.py): robot data dropped by
    graph-builder because it had no session to go to. Rate-limited per robot and reason:
    `dropped_nodes` / `dropped_images` count every drop since `since` (the first drop not yet
    reported). `reason`: no_session, not_mapping_session (an operate session), session_paused,
    map_not_mapping, map_deleting, session_unplaced (maps §14: not placed yet),
    map_missing, session_mismatch, datum_changed (not re-anchorable: local map, other UTM
    zone), lookup_failed. `dropped_depth` (3D reconstruction R2): depth images dropped (absent
    on events written before R2)."""
    reason: str
    dropped_nodes: int = 0
    dropped_images: int = 0
    dropped_depth: Optional[int] = None
    since: datetime.datetime
    map_name: Optional[str] = None
    map_state: Optional[str] = None
    session_id: Optional[str] = None
    payload_session_id: Optional[str] = None


class RecordingChanged(Payload):
    old_level: Optional[RecordingLevel] = None
    new_level: RecordingLevel
    scope: RecordingScope
    scope_id: Optional[str] = None
    actor: Optional[str] = None


_strict = False


def set_strict_validation(strict: bool) -> None:
    global _strict
    _strict = bool(strict)


def strict_validation() -> bool:
    return _strict


def validate_payload(model: Type[Payload], payload: Optional[Mapping[str, Any]],
                     strict: Optional[bool] = None) -> Dict[str, Any]:
    """Validate `payload` against `model` and return a JSON-safe dict."""
    if strict is None:
        strict = _strict
    raw = dict(payload or {})
    try:
        return json.loads(model.parse_obj(raw).json())
    except pydantic.ValidationError as exc:
        if strict:
            raise InvalidPayloadError(f"{model.__name__}: {exc}") from exc
        logger.error("Invalid %s payload %r: %s", model.__name__, raw, exc)
        raw[INVALID_KEY] = True
        raw[INVALID_ERROR_KEY] = str(exc)
        return raw
