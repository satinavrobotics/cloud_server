"""Typed maps, map lifecycle and sessions (docs/satinav-maps-redesign.md §2-§4, §7, §14).

    POST   /api/v1/maps                                  create_map()     -> draft
    GET    /api/v1/maps?type=&state=&include_archived=   filter_maps()
    GET    /api/v1/maps/{id}                             session_summary() (added to the old body)
    PATCH  /api/v1/maps/{id}                             patch_map()      description only
    POST   /api/v1/maps/{id}/sessions                    start_session()
    GET    /api/v1/maps/{id}/sessions?limit=&before=     session_history()
    POST   /api/v1/maps/{id}/sessions/{sid}/pause|resume|finish   session_action()
    POST   /api/v1/maps/{id}/sessions/{sid}/place        place_session() (202 + a reloc job)
    GET|DELETE /api/v1/maps/{id}/sessions/{sid}/reloc-job  packages/api/reloc_job.py
    POST   /api/v1/maps/{id}/archive|restore             archive_map() / restore_map()
    POST   /api/v1/maps/{id}/type                        convert_map_type() geo <-> local
    DELETE /api/v1/maps/{id}                             refuse_open_session() guards it

The old map routes POST /map/load and PUT /maps/{id}/datum are unchanged. The old "assign map"
(PUT /robots/{r}/map, a shim over sessions since M2) and robot.current_map were removed in U6:
the robot's map is its open session; the route answers 410 for one release (main.py).

Storage: the map is its `mapobjectv1` row (spec.type/geo, status.state/open_session_id; the
object `lifecycle` ALIVE/DELETING stays the delete bookkeeping), sessions are `map_sessions`
rows (migrations 20260928_01_map_sessions, 20260930_01_maps_use). Every write here is ONE
transaction on a pooled connection (PostgresDatabase.connection()): the robot row (when the
call names a robot) and then the map rows (in name order) are locked FOR UPDATE first, so
writes to one map or robot serialise; the session row, the map status, the
`<publisher> <name> <lifecycle>` NOTIFY on the map table and the MAP.* event commit together.
The event is written in a savepoint: a failing event write is logged and never fails the change.

Sessions (maps §14): a robot uses a map through its ONE open session (partial unique index
map_sessions_one_open_per_robot), whose `purpose` is `mapping` (the robot adds data) or
`operate` (it uses the map and adds nothing). Both carry map_T_session, where the robot's
current run frame sits in the map frame; `aligned` = *placed* = that transform is valid for the
robot's current run (packages/utils/map_sessions.py).

Start rules:
- the robot must exist (404) and be online (409); it has no other open session (409), unless
  `replace: true`, which finishes that session in the same transaction;
- the map is not archived or being deleted (409); `operate` needs data (409 on a draft map);
  `mapping` needs no other open mapping session on the map (409); operate sessions are not
  limited (several robots may use a map, also while another robot maps it, decision Q-U5);
- geo map: the robot's current datum is required (409) and places the session. The first
  session of a geo map without an origin sets it (doc Q1): `geo` = the datum's UTM point in its
  own zone, and the map's legacy datum_* fields (when unset) = that origin as a 'utm' datum.
  `placement` on a geo map is 422;
- local map: the first mapping session of an EMPTY map is identity and placed (the run defines
  the frame). Otherwise the session is placed by `placement` ({pose in the map frame, the
  robot's own pose}) or, with `replace` on the same map, carried over from the robot's placed
  session; else it starts NOT placed. A mapping session on a local map that has nodes must be
  placed before capture turns on (decision Q-U4);
- placing (start with `placement`, or place_session) is never refused because the robot drives
  (an active order, or a velocity in its last state) or because its pose differs from the pose
  the user saw by more than sensor noise (0.02 m / 0.5 deg): those only add `warnings`
  (decision 2026-10-08, placement_warnings()).

A session that is not placed keeps nothing (graph-builder rejects its nodes with
`session_unplaced`; its service still runs), gets no route orders on that map and no planned paths.
graph-builder also keeps nothing from an OPERATE session (`operate_session`) or a PAUSED one
(`session_paused`): only an open, unpaused, placed mapping session adds data.

Map lifecycle: draft -> mapping <-> paused -> ready (finish) -> archived -> ready|draft
(restore). Only MAPPING sessions move it; operate sessions leave the map state alone. A finish
(also through `replace` or a robot delete) and a restore make the map `ready` only when it holds
data: nodes (ArangoDB's count, read BEFORE the transaction; the stored counts when it cannot be
read) or a saved SLAM map (`status.slam_saved_at`, set when a SLAM save of the map succeeded,
which also turns a `draft` map `ready`); otherwise it is (back to) `draft`, where operate
sessions are refused.
Archive and delete are refused while ANY session is open (the message names the robots,
decision Q-U2). Repeating pause/resume/finish/archive/restore on a map or session already in
that state is a no-op (`changed: false`, no event). pause/resume are for mapping sessions only.

Robot services (docs/satinav-maps-redesign.md §14.16, packages/api/mapping_switch.py): the
mapping services of a session (`services`, today `topo` = the orchestrator's `topomap` /
`sim_topomap`) run on the robot's orchestrator. Opening a mapping session STARTS them, resuming
starts them again, pausing and finishing STOP them (unless another open, unpaused mapping session
of the robot runs them), deleting the robot stops them. The orchestrator call (up to
ORCHESTRATOR_START_TIMEOUT_S) is NEVER made inside a DB transaction (it would hold row locks
that mission-dispatch and other writers wait on): the change is committed first, then the
services are started / stopped. The switching NEVER blocks or undoes the user's action: no
502/504/409, no compensating close / pause; whatever failed (robot offline, orchestrator
unreachable, no such service) is only reported. Every robot-side action is listed in the
response's `robot_actions` ([{service, action, ok, label, detail}], also the SLAM ones below);
`robot_notified` is false and `mapping_warning` carries the joined failure texts when one failed.
While the robot's previous SLAM map is still being saved (a finish's background save, minutes)
a start / resume does not touch the robot: its actions say the services start after the save,
and when the save ended (saved, failed or nothing to save) restart_session_services() starts
them (SLAM first, then the topomap; a failed save refuses the new SLAM recording until it is
retried or discarded). The same restart runs after mission-dispatch reports a run change of
the robot (NOTIFY ms.RUN_CHANGED_CHANNEL: a driver / orchestrator restart killed the services)
and after a discarded save; each restart is MAP.SESSION_SERVICES_RESTARTED (or _RESTART_FAILED)
with its `robot_actions`, for a non-paused MAPPING session only.

SLAM maps (`slam` in a mapping session's `services`; `slam_map` on a local map is set at
creation, changed with PATCH while no mapping session is open and no save is pending, cleared by
converting to geo, and set by a session start that asks for `slam` on a plain local map; a geo
map answers 400). `services` omitted = topo (+ slam on a slam_map map); an explicit list,
including [], is taken exactly. A MAPPING session with `slam` also records a SLAM map on the robot's orchestrator, named
onboard_map_name(map). Right after the session opened (outside any transaction, under the
robot's lock) the server calls start_slam; finishing the session saves it in a BACKGROUND task
(minutes: background save, polled; packages/api/mapping_switch.py; its outcome is the event
MAP.SLAM_SAVE_DONE / MAP.SLAM_SAVE_FAILED). Replace saves the replaced session's SLAM map first,
awaited only when the NEW session is a mapping session that records SLAM (one driver per
robot), else in the background. A failed save leaves the robot in slam: the robot view's
`slam_save` shows it and POST /api/v1/robots/{r}/slam-save/retry | discard (slam_save_retry,
slam_save_discard) end it. A SLAM failure never fails or undoes a session: the
response carries `slam_warning` and the matching `robot_actions`. Resume starts the recording again
when `slam` is in the services; pause and operate sessions never touch SLAM; finish saves a session's
SLAM map when `slam` is in its services or (older sessions) the map is slam_map and the robot is
recording it.

Type conversion (convert_map_type, docs/satinav-maps-redesign.md §17): geo -> local drops the
georeference, local -> geo adds one ({latitude, longitude} of a map-frame `anchor`, plus the
map's rotation against the grid, stored as geo.bearing_deg). The map-frame coordinates never
change, so nodes, edges, reconstruction results, every session's map_T_session and stored mission
waypoints stay valid. Refused (409) while the map has an open (or paused) mapping session; open
operate sessions keep their placement.
"""

import asyncio
import contextlib
import datetime
import json
import logging
import math
import re
import uuid
from typing import (Any, AsyncIterator, Awaitable, Callable, Dict, List, Mapping, Optional,
                    Sequence, Tuple)

import psycopg
import pydantic
from fastapi import HTTPException

from cloud_common.objects.map import (
    MAP_STATES, MAP_TYPES, MapObjectV1, MapSpecV1, MapStatusV1, effective_state,
    effective_type, has_real_datum,
)
from cloud_common.objects.object import ObjectLifecycleV1
from cloud_common.objects.robot import RobotObjectV1
from packages.api.mapping_switch import (
    SLAM_FAILED, SLAM_SAVED, SLAM_SERVICE, Snapshot, SlamResult, action_name, robot_action,
    service_action, slam_save_action, slam_start_action, START, STOP)
from packages.api.orchestrator_client import onboard_map_name  # noqa: F401 - re-exported
from packages.config import RUN_CHANGE_RESTART_RETRY_S, RUN_CHANGE_RESTART_TRIES
from packages.events.codes import EventCode, Source
from packages.events.emit import Event, emit
from packages.utils import map_geo
from packages.utils import map_sessions as ms

logger = logging.getLogger("ApiDelegationService.maps")

MAP_TABLE = MapObjectV1.table_name()
ROBOT_TABLE = RobotObjectV1.table_name()
SESSIONS_TABLE = "map_sessions"
MISSION_TABLE = "missionobjectv1"
DELETING = ObjectLifecycleV1.DELETING.value
ALIVE = ObjectLifecycleV1.ALIVE.value

DRAFT, MAPPING, PAUSED, READY, ARCHIVED = MAP_STATES
OPEN_STATES = (MAPPING, PAUSED)
SESSION_ACTIONS = ("pause", "resume", "finish")
# The old API's mapless sentinels (robot.current_map, removed in U6). Still never map names:
# old missions and mission_runs rows carry them as map ids, and must not attach to a map.
RESERVED_NAMES = frozenset({"GEO", "LOCAL"})
# The name is the key in Postgres, ArangoDB (nodes_<name>) and MinIO (bucket map-<name>,
# lower-cased, '_' -> '-'; 63 characters at most), so it is restricted to what all three take.
NAME_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9_-]{0,57}[A-Za-z0-9])?$")
SUMMARY_MAX_SESSIONS = 50
HISTORY_DEFAULT_LIMIT = 50
HISTORY_MAX_LIMIT = 200

SESSION_COLUMNS = ms.SESSION_COLUMNS
# robot_latest.state_msg older than this is not used for the "robot drives" check.
STATE_MSG_MAX_AGE = "30 seconds"
JSONB_SESSION_COLUMNS = frozenset({"datum", "map_t_session", "placement"})

# Event counters for health/debugging (as packages/api/recording.py).
stats: Dict[str, int] = {"written": 0, "failed": 0}


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def bucket_key(name: str) -> str:
    """What the name becomes in MinIO (packages/topomap_dbs/minio_base.py::_bucket_name)."""
    return name.lower().replace("_", "-")


# --- request bodies ----------------------------------------------------------------------------

class CreateMapRequest(pydantic.BaseModel):
    name: str
    type: str
    description: Optional[str] = None
    slam_map: bool = False

    class Config:
        extra = pydantic.Extra.forbid

    @pydantic.validator("name")
    def _name(cls, value):  # noqa: N805 - pydantic v1 validator
        if not NAME_RE.match(value):
            raise ValueError("map name must be 1-59 characters of letters, digits, '_' or '-', "
                             "starting and ending with a letter or digit")
        if value in RESERVED_NAMES:
            raise ValueError(f"{value!r} is reserved (the old mapless sentinel)")
        return value

    @pydantic.validator("type")
    def _type(cls, value):  # noqa: N805
        if value not in MAP_TYPES:
            raise ValueError(f"type must be one of {', '.join(MAP_TYPES)}")
        return value

    @pydantic.validator("slam_map")
    def _slam_map(cls, value, values):  # noqa: N805
        if value and values.get("type") not in (None, "local"):
            raise ValueError("slam_map is only for local maps")
        return value


class PatchMapRequest(pydantic.BaseModel):
    description: Optional[str] = None
    display_name: Optional[str] = None   # shown instead of the (unchangeable) name; ""/null clears
    slam_map: Optional[bool] = None   # a LOCAL map only, no open session / pending save (409)

    class Config:
        extra = pydantic.Extra.forbid

    @pydantic.validator("display_name")
    def _display_name(cls, value):  # noqa: N805 - pydantic v1 validator
        value = (value or "").strip()
        if len(value) > 80:
            raise ValueError("display_name must be at most 80 characters")
        return value or None


def _finite(cls, value: float) -> float:  # noqa: N805 - a pydantic v1 validator
    if not math.isfinite(value):
        raise ValueError("must be a finite number")
    return value


class MapPose(pydantic.BaseModel):
    """A pose in the map frame (metres, radians CCW)."""
    x: float
    y: float
    yaw: float

    class Config:
        extra = pydantic.Extra.forbid

    _check = pydantic.validator("x", "y", "yaw", allow_reuse=True)(_finite)


class RobotPose(pydantic.BaseModel):
    """The robot's own pose (robot.status.pose: its run frame) as the user saw it."""
    x: float
    y: float
    theta: float

    class Config:
        extra = pydantic.Extra.forbid

    _check = pydantic.validator("x", "y", "theta", allow_reuse=True)(_finite)


PLACE_SOURCES = (ms.SOURCE_LAST_POSITION, ms.SOURCE_RELOC, ms.SOURCE_DATUM)
# Placed by the server from what the robot reports: no pose / robot_pose in the body.
POSELESS_SOURCES = (ms.SOURCE_RELOC, ms.SOURCE_DATUM)


class RelocOptions(pydantic.BaseModel):
    """`reloc` of POST .../place with source "reloc": `init_pose` {x, y, yaw} in the cloud MAP
    frame = relocalize with Odin assisted by that pose (else Odin alone)."""
    init_pose: Optional[MapPose] = None

    class Config:
        extra = pydantic.Extra.forbid


class PlaceRequest(pydantic.BaseModel):
    """POST .../sessions/{sid}/place, and `placement` on start."""
    pose: Optional[MapPose] = None
    robot_pose: Optional[RobotPose] = None
    # Optional: "last_position" when the user accepted a placement suggestion (recorded in
    # placement.source; the pose and robot_pose are what counts). Absent: a manual placement.
    # "reloc" (POST .../place only): the robot relocalises itself on the stored map its
    # orchestrator holds; no pose or robot_pose is needed (the server records the robot's pose
    # and places with ms.reloc_placement(): the identity, D0 assumption).
    # "datum" (POST .../place only, geo maps): place an unplaced geo session from the robot's
    # CURRENT GNSS datum (what the dispatcher does when a new datum arrives); no poses.
    source: Optional[str] = None
    # "reloc" only: {init_pose: {x, y, yaw}} (cloud map frame) = relocalize assisted by it.
    reloc: Optional[RelocOptions] = None

    class Config:
        extra = pydantic.Extra.forbid

    @pydantic.validator("source")
    def _source(cls, value):  # noqa: N805
        if value is not None and value not in PLACE_SOURCES:
            raise ValueError("source must be one of "
                             f"{', '.join(map(repr, PLACE_SOURCES))} when given")
        return value

    @pydantic.root_validator(skip_on_failure=True)
    def _poses(cls, values):  # noqa: N805
        if values.get("reloc") is not None and values.get("source") != ms.SOURCE_RELOC:
            raise ValueError(f"reloc is only for source {ms.SOURCE_RELOC!r}")
        if values.get("source") not in POSELESS_SOURCES and (
                values.get("pose") is None or values.get("robot_pose") is None):
            raise ValueError("pose and robot_pose are required (except for source "
                             f"{ms.SOURCE_RELOC!r} or {ms.SOURCE_DATUM!r})")
        return values


class StartSessionRequest(pydantic.BaseModel):
    robot: str
    purpose: str = ms.MAPPING
    services: Optional[List[str]] = None
    placement: Optional[PlaceRequest] = None
    replace: bool = False

    class Config:
        extra = pydantic.Extra.forbid

    @pydantic.validator("purpose")
    def _purpose(cls, value):  # noqa: N805
        if value not in ms.PURPOSES:
            raise ValueError(f"purpose must be one of {', '.join(ms.PURPOSES)}")
        return value

    @pydantic.validator("services")
    def _services(cls, value, values):  # noqa: N805
        if value is None:
            return value
        if values.get("purpose") == ms.OPERATE:
            raise ValueError("services are for mapping sessions (purpose 'mapping')")
        unknown = [s for s in value if s not in ms.KNOWN_SERVICES]
        if unknown:
            raise ValueError(f"unknown mapping service(s) {', '.join(map(repr, unknown))}; "
                             f"known: {', '.join(ms.KNOWN_SERVICES)}")
        return list(dict.fromkeys(value))   # [] is allowed: the session starts nothing

    def session_services(self, slam_map: bool = False) -> Optional[List[str]]:
        """The services the new session records. Omitted: topo, plus slam on a slam_map map;
        an explicit list (even []) is taken exactly."""
        if self.purpose != ms.MAPPING:
            return None
        if self.services is None:
            return [*ms.DEFAULT_SERVICES, *([ms.SLAM] if slam_map else [])]
        return list(self.services)


def _unprocessable(exc: pydantic.ValidationError) -> HTTPException:
    return HTTPException(422, [{"loc": ["body", *err["loc"]], "msg": err["msg"],
                                "type": err["type"]} for err in exc.errors()])


def parse_body(model: Any, data: Any) -> Any:
    """`model` from a JSON body; 422 in FastAPI's shape on anything else. PATCH with `name`
    says why (rename is not in M1)."""
    if not isinstance(data, Mapping):
        raise HTTPException(422, [{"loc": ["body"], "msg": "expected a JSON object",
                                   "type": "type_error.dict"}])
    if model is PatchMapRequest and "name" in data:
        raise HTTPException(422, [{"loc": ["body", "name"], "type": "value_error",
                                   "msg": "a map cannot be renamed (the name keys its data in "
                                          "Postgres, ArangoDB and MinIO); set display_name "
                                          "instead"}])
    try:
        return model(**data)
    except pydantic.ValidationError as exc:
        raise _unprocessable(exc) from exc


def check_filters(type_: Optional[str], state: Optional[str]) -> None:
    for loc, value, allowed in (("type", type_, MAP_TYPES), ("state", state, MAP_STATES)):
        if value is not None and value not in allowed:
            raise HTTPException(422, [{"loc": ["query", loc], "type": "value_error",
                                       "msg": f"must be one of {', '.join(allowed)}"}])


# --- rows --------------------------------------------------------------------------------------

class MapRow:
    """A locked mapobjectv1 row: raw spec/status JSON plus the parsed object."""

    def __init__(self, name: str, lifecycle: str, spec: Dict[str, Any],
                 status: Dict[str, Any]):
        self.name = name
        self.lifecycle = lifecycle
        self.spec = dict(spec or {})
        self.status = dict(status or {})
        self.obj = MapObjectV1(name=name, lifecycle=ObjectLifecycleV1[lifecycle],
                               status=self.status, **self.spec)

    @property
    def type(self) -> str:
        return effective_type(self.obj)

    @property
    def state(self) -> str:
        """The lifecycle state as it is now in this transaction (self.status is kept up to
        date by the writes here; self.obj is the row as read)."""
        return effective_state(MapStatusV1(**self.status))


def _iso(ts: Any) -> Any:
    return ts.isoformat() if isinstance(ts, datetime.datetime) else ts


def session_dict(row: Mapping[str, Any]) -> Dict[str, Any]:
    """A session as the API returns it (map_t_session is shown as map_T_session, doc §4).
    `state`: mapping | paused | operating | finished. `aligned` = placed (§14.2)."""
    purpose = ms.purpose_of(row)
    return {
        "session_id": str(row["session_id"]),
        "map_name": row["map_name"],
        "robot_name": row["robot_name"],
        "kind": row.get("kind", "live"),
        "purpose": purpose,
        "services": _services_of(row, orchestrator_only=False) if purpose == ms.MAPPING
        else None,
        "state": ms.session_state(row),
        "started_at": _iso(row.get("started_at")),
        "paused_at": _iso(row.get("paused_at")),
        "ended_at": _iso(row.get("ended_at")),
        "datum": row.get("datum"),
        "map_T_session": row.get("map_t_session"),
        "aligned": row.get("aligned"),
        "placement": row.get("placement"),
        "node_count": row.get("node_count", 0),
    }


# --- the SQL store -----------------------------------------------------------------------------

class SqlStore:
    """The statements of one transaction (unit tests substitute an in-memory store)."""

    def __init__(self, conn: Any, cursor: Any, publisher_id: uuid.UUID):
        self.conn = conn
        self.cursor = cursor
        self.publisher_id = publisher_id

    async def lock_map(self, name: str) -> Optional[MapRow]:
        await self.cursor.execute(
            f"SELECT name, lifecycle, spec, status FROM {MAP_TABLE} WHERE name = %s "
            "AND lifecycle <> 'DELETED' FOR UPDATE", (name,))
        row = await self.cursor.fetchone()
        return MapRow(*row) if row is not None else None

    async def get_map(self, name: str) -> Optional[MapRow]:
        """The map row without a lock (read endpoints must not queue behind writers)."""
        await self.cursor.execute(
            f"SELECT name, lifecycle, spec, status FROM {MAP_TABLE} WHERE name = %s "
            "AND lifecycle <> 'DELETED'", (name,))
        row = await self.cursor.fetchone()
        return MapRow(*row) if row is not None else None

    async def map_names(self) -> List[str]:
        await self.cursor.execute(f"SELECT name FROM {MAP_TABLE}")
        return [r[0] for r in await self.cursor.fetchall()]

    async def insert_map(self, name: str, spec: Dict[str, Any], status: Dict[str, Any]) -> bool:
        await self.cursor.execute(
            f"INSERT INTO {MAP_TABLE} (name, lifecycle, spec, status) "
            "VALUES (%s, %s, %s::jsonb, %s::jsonb) ON CONFLICT (name) DO NOTHING",
            (name, ALIVE, json.dumps(spec), json.dumps(status)))
        if not self.cursor.rowcount:
            return False
        await self._notify(name, ALIVE)
        return True

    async def update_map(self, row: MapRow, spec: Optional[Dict[str, Any]] = None,
                         status: Optional[Dict[str, Any]] = None) -> None:
        """Merge `spec` / `status` keys into the row (jsonb ||) and NOTIFY."""
        await self.cursor.execute(
            f"UPDATE {MAP_TABLE} SET spec = spec || %s::jsonb, status = status || %s::jsonb "
            "WHERE name = %s", (json.dumps(spec or {}), json.dumps(status or {}), row.name))
        await self._notify(row.name, row.lifecycle)

    async def _notify(self, name: str, lifecycle: str) -> None:
        # Same payload as PostgresDatabase._notify, so a PostgresWatcher(MapObjectV1) reads it.
        await self.cursor.execute("SELECT pg_notify(%s, %s)",
                                  (MAP_TABLE, f"{self.publisher_id} {name} {lifecycle}"))

    async def robot(self, name: str) -> Optional[RobotObjectV1]:
        await self.cursor.execute(
            f"SELECT name, lifecycle, spec, status FROM {ROBOT_TABLE} WHERE name = %s "
            "AND lifecycle <> 'DELETED'", (name,))
        row = await self.cursor.fetchone()
        if row is None:
            return None
        name, lifecycle, spec, status = row
        return RobotObjectV1(name=name, lifecycle=ObjectLifecycleV1[lifecycle], status=status,
                             **spec)

    async def lock_robot(self, name: str) -> Optional[RobotObjectV1]:
        """The robot row FOR UPDATE (serialises two session changes of one robot)."""
        await self.cursor.execute(
            f"SELECT name, lifecycle, spec, status FROM {ROBOT_TABLE} WHERE name = %s "
            "AND lifecycle <> 'DELETED' FOR UPDATE", (name,))
        row = await self.cursor.fetchone()
        if row is None:
            return None
        name, lifecycle, spec, status = row
        return RobotObjectV1(name=name, lifecycle=ObjectLifecycleV1[lifecycle], status=status,
                             **spec)

    async def robot_state_msg(self, name: str) -> Optional[Dict[str, Any]]:
        """The robot's last VDA5050 state message (robot_latest.state_msg, written by
        mission-dispatch's recorder, merged about once a second), for the "robot drives" check;
        None when there is none or it is older than STATE_MSG_MAX_AGE (then only the robot
        state counts). In a savepoint: a missing table never aborts the caller's
        transaction."""
        try:
            async with self.conn.transaction():
                await self.cursor.execute(
                    "SELECT state_msg FROM robot_latest WHERE robot_name = %s "
                    "AND updated_at > now() - %s::interval", (name, STATE_MSG_MAX_AGE))
                row = await self.cursor.fetchone()
        except Exception as exc:  # noqa: BLE001
            logger.warning("robot_latest of %s not readable (%s); the placement check uses "
                           "the robot state only", name, exc)
            return None
        return dict(row[0]) if row is not None and row[0] else None

    async def robot_mission_open(self, name: str) -> Optional[bool]:
        """Whether the robot has a PENDING or RUNNING (alive) mission, for the "robot drives"
        check (map_sessions.driving_reason); None when it cannot be read. In a savepoint."""
        try:
            async with self.conn.transaction():
                await self.cursor.execute(
                    f"SELECT EXISTS (SELECT 1 FROM {MISSION_TABLE} WHERE spec->>'robot' = %s "
                    "AND lifecycle = 'ALIVE' AND status->>'state' IN ('PENDING', 'RUNNING'))",
                    (name,))
                row = await self.cursor.fetchone()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Missions of %s not readable (%s); the placement check uses the "
                           "robot state", name, exc)
            return None
        return bool(row[0]) if row is not None else None

    async def robot_run_epoch(self, name: str) -> Optional[Tuple[Any, Any]]:
        """The robot's (run epoch, continuity_known) from robot_run_epochs (written by
        mission-dispatch, §14.13), None without a row or when it cannot be read (then nothing
        is reused). In a savepoint: a missing table never aborts the caller's transaction."""
        try:
            async with self.conn.transaction():
                await self.cursor.execute(ms.RUN_EPOCH_OF_SQL, (name,))
                row = await self.cursor.fetchone()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Run epoch of %s not readable (%s); no placement is reused",
                           name, exc)
            return None
        return (row[0], row[1]) if row is not None else None

    async def robot_state_pose(self, name: str, start: Optional[datetime.datetime] = None,
                               end: Optional[datetime.datetime] = None, first: bool = False
                               ) -> Optional[Dict[str, Any]]:
        """The robot's last (`first`: earliest) recorded pose {ts, x, y, theta} (run frame)
        in [start, end) from robot_state_ts; None when there is none or the table cannot be
        read. In a savepoint: a missing table never aborts the caller's transaction."""
        try:
            async with self.conn.transaction():
                await self.cursor.execute(
                    ms.STATE_POSE_FIRST_SQL if first else ms.STATE_POSE_LAST_SQL,
                    (name, start, start, end, end))
                row = await self.cursor.fetchone()
        except Exception as exc:  # noqa: BLE001
            logger.warning("robot_state_ts of %s not readable (%s); no state-history "
                           "placement suggestion", name, exc)
            return None
        if row is None:
            return None
        return {"ts": row[0], "x": row[1], "y": row[2], "theta": row[3]}

    async def robot_run_start(self, name: str) -> Optional[Tuple[Any, Any]]:
        """(started_at, reason) of the robot's current run epoch (robot_run_epochs); None
        without a row or when it cannot be read. In a savepoint."""
        try:
            async with self.conn.transaction():
                await self.cursor.execute(ms.RUN_START_SQL, (name,))
                row = await self.cursor.fetchone()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Run epoch of %s not readable (%s)", name, exc)
            return None
        return (row[0], row[1]) if row is not None else None

    async def _sessions(self, where: str, params: tuple, order: str = "started_at, session_id",
                        limit: Optional[int] = None) -> List[Dict[str, Any]]:
        sql = (f"SELECT {', '.join(SESSION_COLUMNS)} FROM {SESSIONS_TABLE} WHERE {where} "
               f"ORDER BY {order}")
        if limit is not None:
            sql += " LIMIT %s"
            params = (*params, int(limit))
        await self.cursor.execute(sql, params)
        return [dict(zip(SESSION_COLUMNS, r)) for r in await self.cursor.fetchall()]

    async def sessions(self, map_name: str) -> List[Dict[str, Any]]:
        return await self._sessions("map_name = %s", (map_name,))

    async def sessions_page(self, map_name: str, limit: int,
                            before: Optional[str]) -> List[Dict[str, Any]]:
        """Newest first; `before` = the session_id of the last item of the previous page."""
        desc = "started_at DESC, session_id DESC"
        if before is None:
            return await self._sessions("map_name = %s", (map_name,), desc, limit)
        return await self._sessions(
            f"map_name = %s AND (started_at, session_id) < (SELECT started_at, session_id "
            f"FROM {SESSIONS_TABLE} WHERE session_id = %s)",
            (map_name, uuid.UUID(str(before))), desc, limit)

    async def session_count(self, map_name: str) -> int:
        await self.cursor.execute(f"SELECT count(*) FROM {SESSIONS_TABLE} WHERE map_name = %s",
                                  (map_name,))
        row = await self.cursor.fetchone()
        return int(row[0]) if row else 0

    async def open_sessions_of_robot(self, robot_name: str) -> List[Dict[str, Any]]:
        return await self._sessions("robot_name = %s AND ended_at IS NULL", (robot_name,))

    async def open_sessions(self) -> List[Dict[str, Any]]:
        return await self._sessions("ended_at IS NULL", ())

    async def session(self, session_id: str) -> Optional[Dict[str, Any]]:
        """One session by id, without a lock."""
        await self.cursor.execute(
            f"SELECT {', '.join(SESSION_COLUMNS)} FROM {SESSIONS_TABLE} "
            "WHERE session_id = %s", (uuid.UUID(str(session_id)),))
        row = await self.cursor.fetchone()
        return dict(zip(SESSION_COLUMNS, row)) if row is not None else None

    async def lock_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        await self.cursor.execute(
            f"SELECT {', '.join(SESSION_COLUMNS)} FROM {SESSIONS_TABLE} "
            "WHERE session_id = %s FOR UPDATE", (uuid.UUID(str(session_id)),))
        row = await self.cursor.fetchone()
        return dict(zip(SESSION_COLUMNS, row)) if row is not None else None

    async def insert_session(self, session: Dict[str, Any]) -> None:
        try:
            await self.cursor.execute(
                f"INSERT INTO {SESSIONS_TABLE} (session_id, map_name, robot_name, kind, purpose, "
                "services, placement, started_at, datum, map_t_session, aligned, node_count) "
                "VALUES (%s, %s, %s, 'live', %s, %s, %s::jsonb, %s, %s::jsonb, %s::jsonb, %s, 0)",
                (uuid.UUID(str(session["session_id"])), session["map_name"], session["robot_name"],
                 session.get("purpose") or ms.MAPPING, session.get("services"),
                 _json_or_none(session.get("placement")), session["started_at"],
                 _json_or_none(session["datum"]), json.dumps(session["map_t_session"]),
                 session["aligned"]))
        except psycopg.errors.UniqueViolation as exc:
            raise HTTPException(409, f"Robot {session['robot_name']!r} already has an open "
                                     "session") from exc

    async def update_session(self, session_id: str, **fields: Any) -> None:
        cols = ", ".join(f"{k} = %s::jsonb" if k in JSONB_SESSION_COLUMNS else f"{k} = %s"
                         for k in fields)
        values = [_json_or_none(v) if k in JSONB_SESSION_COLUMNS else v
                  for k, v in fields.items()]
        await self.cursor.execute(f"UPDATE {SESSIONS_TABLE} SET {cols} WHERE session_id = %s",
                                  (*values, uuid.UUID(str(session_id))))

    async def emit(self, event: Event) -> None:
        """The event in a savepoint: its failure is logged and never fails the change."""
        try:
            async with self.conn.transaction():
                await emit(self.conn, event)
            stats["written"] += 1
        except Exception:  # noqa: BLE001
            stats["failed"] += 1
            logger.exception("Could not write %s (the map change still commits)", event.code)


def _json_or_none(value: Any) -> Optional[str]:
    return json.dumps(value) if value is not None else None


@contextlib.asynccontextmanager
async def open_store(db: Any, publisher_id: uuid.UUID) -> AsyncIterator[SqlStore]:
    """One transaction: commits on a clean exit, rolls back on an exception."""
    async with db.connection() as conn:
        async with conn.cursor() as cursor:
            yield SqlStore(conn, cursor, publisher_id)


def _undefined_table(exc: Exception) -> HTTPException:
    return HTTPException(503, "Mapping sessions are not available (database migrations not "
                              "applied)")


_SCHEMA_ERRORS = (psycopg.errors.UndefinedTable, psycopg.errors.UndefinedColumn)


# --- helpers -----------------------------------------------------------------------------------

async def _lock_alive_map(store: Any, name: str) -> MapRow:
    return _alive(await store.lock_map(name), name)


def _alive(row: Optional[MapRow], name: str) -> MapRow:
    if row is None:
        raise HTTPException(404, f"Did not find \"map\" with name \"{name}\"")
    if row.lifecycle == DELETING:
        raise HTTPException(409, f"Map '{name}' is being deleted")
    return row


def _map_event(code: EventCode, row_name: str, map_type: Optional[str], state: str,
               actor: Optional[str], ts: datetime.datetime) -> Event:
    return Event(code, ts, source=Source.API, discriminator=f"map:{row_name}:{state}",
                 payload={"map_name": row_name, "map_type": map_type, "state": state,
                          "actor": actor})


def _session_event(code: EventCode, session: Mapping[str, Any], map_state: str,
                   actor: Optional[str], ts: datetime.datetime) -> Event:
    action = code.value.rsplit("_", 1)[-1].lower()
    return Event(code, ts, robot_name=session["robot_name"], source=Source.API,
                 discriminator=f"session:{session['session_id']}:{action}",
                 payload={"map_name": session["map_name"],
                          "session_id": str(session["session_id"]), "map_state": map_state,
                          "purpose": ms.purpose_of(session),
                          "services": session.get("services"),
                          "aligned": session.get("aligned"),
                          "map_T_session": session.get("map_t_session"),
                          "placement": session.get("placement"), "actor": actor})


def _placed_event(session: Mapping[str, Any], ts: datetime.datetime,
                  old_transform: Optional[Mapping[str, float]]) -> Event:
    t = session["map_t_session"]
    return Event(EventCode.MAP_SESSION_PLACED, ts, robot_name=session["robot_name"],
                 source=Source.API,
                 discriminator=(f"session:{session['session_id']}:placed:{t['tx']:.4f}:"
                                f"{t['ty']:.4f}:{t['yaw']:.6f}:{ts.isoformat()}"),
                 payload={"map_name": session["map_name"],
                          "session_id": str(session["session_id"]),
                          "purpose": ms.purpose_of(session), "map_T_session": dict(t),
                          "old_map_T_session": dict(old_transform) if old_transform else None,
                          "placement": session.get("placement")})


def map_view(obj: MapObjectV1) -> Dict[str, Any]:
    """obj.dict() with the effective type/state filled in (old rows have neither)."""
    data = obj.dict()
    data["type"] = effective_type(obj)
    data.setdefault("status", {})["state"] = effective_state(obj.status)
    return data


def _robots_phrase(sessions: List[Mapping[str, Any]]) -> str:
    parts = [f"{s['robot_name']} ({'mapping' if ms.purpose_of(s) == ms.MAPPING else 'using'})"
             for s in sorted(sessions, key=lambda s: str(s["robot_name"]))]
    return ", ".join(parts)


# --- maps --------------------------------------------------------------------------------------

async def create_map(db: Any, data: Any, publisher_id: uuid.UUID, actor: Optional[str] = None,
                     arango_node_count: Optional[Callable[[str], int]] = None
                     ) -> Dict[str, Any]:
    """A new draft map. 409 if the name exists (or is being deleted), if it collides with an
    existing map's MinIO bucket, or if ArangoDB already holds nodes under it (a map without a
    Postgres row: adopt it with POST /map/load, or delete it first)."""
    req = parse_body(CreateMapRequest, data)
    if arango_node_count is not None:
        nodes = await asyncio.to_thread(arango_node_count, req.name)
        if nodes:
            raise HTTPException(409, f"ArangoDB already has {nodes} nodes for map "
                                     f"'{req.name}' (no Postgres row); choose another name")
    spec = json.loads(MapSpecV1(description=req.description, type=req.type,
                                        slam_map=req.slam_map).json())
    status = json.loads(MapStatusV1(state=DRAFT).json())
    now = _utcnow()
    async with open_store(db, publisher_id) as store:
        clash = [n for n in await store.map_names()
                 if n != req.name and bucket_key(n) == bucket_key(req.name)]
        if clash:
            raise HTTPException(409, f"Map name '{req.name}' collides with existing map "
                                     f"'{clash[0]}' (same image bucket)")
        if not await store.insert_map(req.name, spec, status):
            raise HTTPException(409, f"Map '{req.name}' already exists")
        await store.emit(_map_event(EventCode.MAP_CREATED, req.name, req.type, DRAFT, actor, now))
    obj = MapObjectV1(name=req.name, status=status, **spec)
    return map_view(obj)


def filter_maps(maps: List[MapObjectV1], type_: Optional[str] = None,
                state: Optional[str] = None, include_archived: bool = False
                ) -> List[Dict[str, Any]]:
    """GET /api/v1/maps: DELETING maps hidden (as before), archived ones unless asked for."""
    check_filters(type_, state)
    out = []
    for m in maps:
        if m.lifecycle == ObjectLifecycleV1.DELETING:
            continue
        view = map_view(m)
        s = view["status"]["state"]
        if state is not None and s != state:
            continue
        if state is None and s == ARCHIVED and not include_archived:
            continue
        if type_ is not None and view["type"] != type_:
            continue
        out.append(view)
    return out


def apply_graph_counts(views: List[Dict[str, Any]], map_stats: Callable[[str], Mapping[str, Any]]
                       ) -> List[Dict[str, Any]]:
    """Overwrite status.node_count / status.edge_count of each map view with the graph's own
    counts (what GET /maps/{id} reports; ArangoDB is the source of truth). The Postgres row
    counters are not maintained after M1 and would be stale. Blocking: run in a thread."""
    for view in views:
        stats = map_stats(view["name"]) or {}
        ok = "error" not in stats
        st = view.setdefault("status", {})
        st["node_count"] = int(stats.get("node_count") or 0) if ok else 0
        st["edge_count"] = int(stats.get("edge_count") or 0) if ok else 0
    return views


async def patch_map(db: Any, name: str, data: Any, publisher_id: uuid.UUID,
                    switch: Optional[Any] = None, actor: Optional[str] = None) -> Dict[str, Any]:
    """PATCH /api/v1/maps/{id}: `description`, `display_name` and `slam_map` (bool). The name is
    not changeable (it keys the data); `display_name` is the name shown for it (null clears).

    `slam_map` (turn the SLAM recording of a LOCAL map on or off after creation) is refused with
    409 for a geo map, while the map has an open (or paused) mapping session, and while a SLAM
    save is pending on a robot that has sessions on this map (`switch`); a null `slam_map` is
    422. Only the flag changes: turning it OFF never touches the map recorded on a robot (the
    onboard map `cloud-<map>` stays listed in its orchestrator, and a SLAM-driver run that is
    still unsaved is exactly what the pending-save refusal covers); turning it ON again later
    keeps an existing onboard map file ("SLAM map already exists, not re-recorded"), so the
    next mapping session does not overwrite it. A real change emits MAP.SLAM_CHANGED. The
    response is the map view, as before."""
    req = parse_body(PatchMapRequest, data)
    changes = req.dict(exclude_unset=True)
    if "slam_map" in changes and changes["slam_map"] is None:
        raise HTTPException(422, [{"loc": ["body", "slam_map"], "type": "type_error.none",
                                   "msg": "slam_map must be true or false"}])
    now = _utcnow()
    try:
        async with open_store(db, publisher_id) as store:
            row = await _lock_alive_map(store, name)
            if "slam_map" in changes:
                if row.type != "local":
                    raise HTTPException(409, f"Map '{name}' is a geo map: SLAM maps are only "
                                             "for local maps")
                sessions = await store.sessions(name)
                mapping = [s for s in sessions
                           if s["ended_at"] is None and ms.purpose_of(s) == ms.MAPPING]
                if mapping or row.state in OPEN_STATES:
                    who = _robots_phrase(mapping) if mapping else "a robot"
                    raise HTTPException(409, f"Map '{name}' has an open mapping session "
                                             f"({who}); finish it before changing slam_map")
                if switch is not None:
                    for robot in sorted({str(s["robot_name"]) for s in sessions}):
                        if switch.slam_save_pending(robot):
                            raise HTTPException(409, f"Robot '{robot}' is still saving a SLAM "
                                                     f"map of '{name}'; try again when it is "
                                                     "done")
                if bool(row.spec.get("slam_map")) == changes["slam_map"]:
                    del changes["slam_map"]   # no change, no event
                else:
                    await store.emit(Event(
                        EventCode.MAP_SLAM_CHANGED, now, source=Source.API,
                        discriminator=f"map:{name}:slam:{changes['slam_map']}:{now.isoformat()}",
                        payload={"map_name": name, "slam_map": changes["slam_map"],
                                 "actor": actor}))
            if changes:
                await store.update_map(row, spec=changes)
                row.spec.update(changes)
    except _SCHEMA_ERRORS as exc:
        raise _undefined_table(exc) from exc
    return map_view(MapObjectV1(name=name, status=row.status, **row.spec))


async def session_summary(db: Any, name: str, switch: Optional[Any] = None,
                          map_type: Optional[str] = None) -> Dict[str, Any]:
    """The `sessions` block of GET /api/v1/maps/{id}: counts, the open MAPPING session, the
    robots using the map (`operating`), and the newest SUMMARY_MAX_SESSIONS sessions (newest
    first; the full history is GET .../sessions). `mapping_state` / `mapping_service` /
    `mapping_services` of the open mapping session's robot, read from its orchestrator (null
    without an open session; packages/api/mapping_switch.py). §14.13: `placement_reusable` {robot: from_session_id}
    for a `local` map: the robots whose start on this map without a placement would be placed
    from their last session here (same run; with `replace: true` if they use another map now);
    {} otherwise. A hint: the start decides again when it runs."""
    reusable: Dict[str, str] = {}
    try:
        async with open_store(db, uuid.uuid4()) as store:
            rows = await store.sessions(name)
            if map_type == "local":
                here = {r["robot_name"] for r in rows if r["ended_at"] is None}
                for robot in sorted({r["robot_name"] for r in rows} - here):
                    last = ms.reusable_session(rows, robot, await store.robot_run_epoch(robot))
                    if last is not None:
                        reusable[robot] = str(last["session_id"])
    except _SCHEMA_ERRORS as exc:
        raise _undefined_table(exc) from exc
    items = [session_dict(r) for r in reversed(rows)]
    open_mapping = [s for s in items if s["state"] in ("mapping", "paused")]
    open_session = open_mapping[0] if open_mapping else None
    summary = {"count": len(items), "open": open_session,
               "operating": [{"robot": s["robot_name"], "session_id": s["session_id"],
                              "aligned": s["aligned"]}
                             for s in items if s["state"] == "operating"],
               "unaligned": sum(1 for s in items
                                if s["aligned"] is False and s["purpose"] == ms.MAPPING),
               "items": items[:SUMMARY_MAX_SESSIONS],
               "placement_reusable": reusable,
               "mapping_state": None, "mapping_service": None, "mapping_services": None}
    if open_session is not None and switch is not None:
        snap = await _snapshot_of(db, switch, open_session["robot_name"])
        summary["mapping_state"] = snap.state(open_session)
        summary["mapping_service"] = snap.mapping_service()
        summary["mapping_services"] = snap.mapping_services()
    return summary


async def _snapshot_of(db: Any, switch: Any, robot_name: str, fresh: bool = False) -> Snapshot:
    """The robot's mapping services as its orchestrator reports them. Never raises."""
    try:
        async with open_store(db, uuid.uuid4()) as store:
            robot = await store.robot(robot_name)
        if robot is None:
            return Snapshot(reachable=None)
        return await switch.snapshot(robot, fresh=fresh)
    except Exception:  # noqa: BLE001
        logger.exception("Mapping state of robot %s not readable", robot_name)
        return Snapshot(reachable=None)


async def session_history(db: Any, name: str, limit: Optional[int] = None,
                          before: Optional[str] = None) -> Dict[str, Any]:
    """GET /api/v1/maps/{id}/sessions?limit=&before=: every session of the map, newest first,
    paged. `before`: the `next_before` of the previous page (a session id). 404 unknown map."""
    limit = HISTORY_DEFAULT_LIMIT if limit is None else int(limit)
    if not 1 <= limit <= HISTORY_MAX_LIMIT:
        raise HTTPException(422, [{"loc": ["query", "limit"], "type": "value_error",
                                   "msg": f"must be between 1 and {HISTORY_MAX_LIMIT}"}])
    if before is not None:
        try:
            before = str(uuid.UUID(str(before)))
        except ValueError:
            raise HTTPException(422, [{"loc": ["query", "before"], "type": "value_error",
                                       "msg": "must be a session id"}]) from None
    try:
        async with open_store(db, uuid.uuid4()) as store:
            row = await store.lock_map(name)
            if row is None:
                raise HTTPException(404, f"Did not find \"map\" with name \"{name}\"")
            rows = await store.sessions_page(name, limit + 1, before)
            total = await store.session_count(name)
    except _SCHEMA_ERRORS as exc:
        raise _undefined_table(exc) from exc
    more = len(rows) > limit
    items = [session_dict(r) for r in rows[:limit]]
    return {"map_id": name, "count": total, "items": items,
            "next_before": items[-1]["session_id"] if more and items else None}


def _parse_ts(value: Any) -> Optional[datetime.datetime]:
    if isinstance(value, datetime.datetime):
        return value if value.tzinfo else value.replace(tzinfo=datetime.timezone.utc)
    if isinstance(value, str):
        try:
            ts = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return ts if ts.tzinfo else ts.replace(tzinfo=datetime.timezone.utc)
    return None


async def _run_start_pose(store: Any, robot_name: str, lower: Optional[datetime.datetime]
                          ) -> Optional[Dict[str, Any]]:
    """The robot's first recorded pose at or after `lower` (when its current run began), in
    the run frame; None when unknown (the caller then assumes the odometry origin)."""
    if lower is None:
        return None
    return await store.robot_state_pose(robot_name, start=lower, first=True)


def _suggestion(basis: str, found: Mapping[str, Any], at: Any, from_session_id: Any
                ) -> Dict[str, Any]:
    return {"source": ms.SOURCE_LAST_POSITION, "basis": basis,
            "map_T_session": found["map_T_session"], "pose": found["pose"],
            "robot_pose": found["robot_pose"],
            "at": _iso(at), "from_session_id": str(from_session_id)}


async def _reloc_inputs(db: Any, map_name: str, session_id: str
                        ) -> Tuple[Optional[MapRow], Optional[Dict[str, Any]],
                                   Optional[RobotObjectV1]]:
    """(map row, session, its robot) for the reloc reads: one short transaction, no row lock
    (these run on GET paths and before the orchestrator is asked). None for what is missing."""
    async with open_store(db, uuid.uuid4()) as store:
        row = await store.get_map(map_name)
        session = await store.session(session_id) if row is not None else None
        if session is not None and session["map_name"] != map_name:
            session = None
        robot = await store.robot(session["robot_name"]) if session is not None else None
    return row, session, robot


def datum_placement(map_geo_block: Optional[Mapping[str, Any]], robot: Optional[RobotObjectV1]
                    ) -> Optional[Dict[str, Any]]:
    """Where the robot's CURRENT GNSS datum puts its run frame on a geo map: {map_T_session,
    datum, robot_pose (its pose now, run frame), pose (that pose in the map frame)}, or None
    when the datum cannot place it (no datum, another UTM zone, no map origin). The map's
    rotation (geo.bearing_deg, a local map converted to geo) is included."""
    if robot is None:
        return None
    datum = map_geo.robot_datum(robot.datum)
    transform = ms.geo_transform_for(map_geo_block, "geo", datum)
    if transform is None:
        return None
    at = robot.status.pose
    rx, ry, rt = (float(at.x), float(at.y), float(at.theta)) if at is not None else (0.0, 0.0, 0.0)
    x, y, yaw = map_geo.apply_pose(transform, rx, ry, rt)
    return {"map_T_session": transform, "datum": datum,
            "robot_pose": {"x": rx, "y": ry, "theta": rt}, "pose": {"x": x, "y": y, "yaw": yaw}}


def datum_suggestion(map_geo_block: Optional[Mapping[str, Any]], robot: Optional[RobotObjectV1],
                     session: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """The placement suggestion of an unplaced session on a GEO map: the robot's current datum
    (accept with POST .../place {"source": "datum"}). `datum_after_unplace`: whether the datum
    changed after the session was unplaced (true: it is the new run's; false: it may be the
    previous run's, e.g. not re-sent yet, or a fixed sim anchor that never changes; null:
    unknown). `at`: when the robot's datum last changed (null: unknown)."""
    found = datum_placement(map_geo_block, robot)
    if found is None:
        return None
    changed_at = _parse_ts(getattr(robot, "datum_changed_at", None))
    unplaced_at = _parse_ts((session.get("placement") or {}).get("unplaced_at"))
    after = None if changed_at is None or unplaced_at is None else changed_at >= unplaced_at
    return {"source": ms.SOURCE_DATUM, "basis": "robot_datum", **found,
            "at": _iso(changed_at), "datum_after_unplace": after}


def _placement_refusals(map_name: str, session_id: str, session: Optional[Mapping[str, Any]],
                        row: MapRow, robot: Optional[RobotObjectV1], reloc: bool,
                        source: Optional[str] = None, check_initialized: bool = True) -> None:
    """The 404/409 refusals of POST .../place that need no orchestrator answer, in the order
    they apply. Run once before the (slow) orchestrator read of a `reloc` placement and again
    inside the transaction. `source` "datum": the geo-map placement from the robot's datum.
    `check_initialized` false: a relocalization job does not refuse a robot whose position is
    not initialized (that is what it is about to fix)."""
    if session is None or session["map_name"] != map_name:
        raise HTTPException(404, f"Did not find session \"{session_id}\" on map "
                                 f"\"{map_name}\"")
    if session["ended_at"] is not None:
        raise HTTPException(409, f"Session {session_id} is finished")
    by_datum = source == ms.SOURCE_DATUM
    if row.type == "geo" and not by_datum:
        raise HTTPException(409, f"Map '{map_name}' is a geo map: its sessions are "
                                 "placed by the robot's datum (source \"datum\")")
    if row.type != "geo" and by_datum:
        raise HTTPException(409, f"Map '{map_name}' is a local map: it has no georeference "
                                 "to place a robot by its datum; place it by hand or by "
                                 "relocalization")
    if by_datum and ms.is_placed(session):
        raise HTTPException(409, f"Session {session_id} is already placed (a placed geo "
                                 "session follows the robot's datum by itself)")
    if robot is None:
        raise HTTPException(404, f"Did not find \"robot\" with name "
                                 f"\"{session['robot_name']}\"")
    if not robot.status.online:
        raise HTTPException(409, f"Robot '{robot.name}' is offline")
    if reloc:   # a placed session may be relocalized again, like "Place again" by hand
        if check_initialized and robot.status.position_initialized is False:
            raise HTTPException(409, f"Robot '{robot.name}' is not relocalised: it reports its "
                                     "position as not initialized")


async def _start_blockers(db: Any, robot: RobotObjectV1, switch: Optional[Any],
                          reloc_jobs: Optional[Any]) -> Optional[str]:
    """Why a reloc job cannot start for this robot right now besides mapping_blocker (the real
    races RelocJobs.start refuses with 409): another job runs, a SLAM save is pending, or a
    failed SLAM save left the robot in slam. A driving robot does not block (decision
    2026-10-08). None when nothing is in the way. Never raises."""
    try:
        if reloc_jobs is not None and reloc_jobs.active_for(robot.name) is not None:
            return f"relocalization is already running for robot '{robot.name}'"
        if switch is not None and switch.slam_save_pending(robot.name):
            return f"robot '{robot.name}' is still saving a SLAM map"
        if switch is not None and switch.slam_save_failed(robot.name) is not None:
            return SLAM_FAILED_REASON.format(robot=robot.name)
    except Exception:  # noqa: BLE001
        logger.exception("Reloc blockers of robot %s not readable", robot.name)
    return None


SLAM_FAILED_REASON = ("the robot '{robot}' is still in SLAM mode after a failed save: retry or "
                      "discard it")


def slam_mapping_session(sessions: Sequence[Mapping[str, Any]]) -> Optional[Mapping[str, Any]]:
    """The open MAPPING session among `sessions` that records a SLAM map (`slam` in its
    services), or None: relocalization is refused while one is open (its recording is the
    robot's localization mode). A topomap-only mapping session does not block it."""
    for s in sessions:
        if (s.get("ended_at") is None and ms.purpose_of(s) == ms.MAPPING
                and ms.SLAM in _services_of(s, orchestrator_only=False)):
            return s
    return None


def slam_session_reason(robot_name: str, session: Mapping[str, Any]) -> str:
    return (f"robot '{robot_name}' has an open mapping session that records a SLAM map "
            f"({session['map_name']}): relocalization would discard that recording; finish "
            "the session first")


async def mapping_blocker(db: Any, robot: RobotObjectV1) -> Optional[str]:
    """Reloc is not offered while the robot's open session is a MAPPING session that records a
    SLAM map (its recording IS the robot's localization mode; decision D, 2026-10-09: a
    topomap-only mapping session no longer blocks it). The reason, else None (also when
    unreadable)."""
    try:
        async with open_store(db, uuid.uuid4()) as store:
            open_sessions = await store.open_sessions_of_robot(robot.name)
    except Exception:  # noqa: BLE001
        logger.exception("Open sessions of robot %s not readable", robot.name)
        return None
    found = slam_mapping_session(open_sessions)
    return slam_session_reason(robot.name, found) if found is not None else None


async def _can_start(holder: Optional[Any], robot: Optional[RobotObjectV1], map_name: str,
                     held: Optional[bool], fresh: bool = False, db: Any = None,
                     switch: Optional[Any] = None, reloc_jobs: Optional[Any] = None,
                     blockers: bool = False) -> Tuple[bool, Optional[str]]:
    """(can_start, why) for the reloc reads and place_session. can_start is the holder's
    reloc_capability (true for a known robot whose orchestrator can be asked); with `blockers`
    it is also false while mapping_blocker() refuses (an open mapping session that records a
    SLAM map), and `why` then says so. Otherwise `why` is a NON-blocking warning (offline, no
    orchestrator, stored maps unreadable; with `blockers` also a running job, a pending or a
    failed SLAM save, which place_session refuses with 409). Never raises."""
    ask = getattr(holder, "reloc_capability", None)
    if ask is None or robot is None:
        return False, ("the robot is unknown" if robot is None
                       else "relocalization cannot be started from here")
    try:
        can, why = await ask(robot, map_name, fresh=fresh, held=held)
    except Exception:  # noqa: BLE001
        logger.exception("Reloc capability of robot %s not readable", robot.name)
        return True, "the robot's relocalization capability could not be read"
    if blockers:
        mapping = await mapping_blocker(db, robot)
        if mapping is not None:
            return False, mapping
        blocked = await _start_blockers(db, robot, switch, reloc_jobs)
        if blocked is not None:
            why = f"{blocked}; {why}" if why else blocked
    return bool(can), why


_GEO_RELOC = "a geo map is placed by its datum, not relocalized"


async def reloc_status(db: Any, holder: Optional[Any], map_name: str, session_id: str,
                       switch: Optional[Any] = None, reloc_jobs: Optional[Any] = None
                       ) -> Optional[Dict[str, Any]]:
    """`reloc` of the placement-suggestions answer: {available, known, source} for an unplaced,
    open session on a local map, else None. `available`: the robot's orchestrator holds a
    stored map for this cloud map, so no manual initial position is needed (place it with
    `source: "reloc"`); `known` false: the orchestrator could not be asked (then `available` is
    false: manual placement). `can_start` (additive): _can_start() (source "reloc" then answers
    202 with a job; see reloc_job.py): false while the robot's open mapping session records a
    SLAM map (`can_start_reason` says so); `warning` (= `can_start_reason`, kept for
    compatibility): otherwise a non-blocking text on what may make the job fail (robot offline,
    no orchestrator, stored maps unreadable, another job running, a SLAM save pending or
    failed), else null. A GET: no row locks and the cached held read (place_session asks afresh).
    Never raises."""
    try:
        row, session, robot = await _reloc_inputs(db, map_name, session_id)
        if (row is None or row.type != "local" or session is None
                or session["ended_at"] is not None):
            return None
        held = None
        if holder is not None and robot is not None:
            held = await holder.held(robot, map_name)
        can, why = await _can_start(holder, robot, map_name, held, db=db, switch=switch,
                                    reloc_jobs=reloc_jobs, blockers=True)
    except Exception:  # noqa: BLE001
        logger.exception("Reloc status of session %s not readable", session_id)
        return None
    return {"available": held is True, "known": held is not None, "source": "orchestrator",
            "can_start": can, "can_start_reason": why, "warning": why}


async def map_reloc(db: Any, holder: Optional[Any], map_name: str, robot_name: str,
                    switch: Optional[Any] = None, reloc_jobs: Optional[Any] = None
                    ) -> Dict[str, Any]:
    """GET /api/v1/maps/{id}/reloc?robot=: the same {available, known, source} as `reloc` of the
    placement suggestions, before any session exists (cached read, no row lock). 404 unknown
    map. A geo map is placed by the datum, not relocalised: {available: false, known: true}. An
    unknown or offline robot, no holder or an orchestrator that cannot be asked: known false."""
    robot = None
    try:
        async with open_store(db, uuid.uuid4()) as store:
            row = await store.get_map(map_name)
            if row is None:
                raise HTTPException(404, f"Did not find \"map\" with name \"{map_name}\"")
            if row.type == "geo":
                return {"available": False, "known": True, "source": "orchestrator",
                        "can_start": False, "warning": _GEO_RELOC,
                        "can_start_reason": _GEO_RELOC}
            robot = await store.robot(robot_name)
    except _SCHEMA_ERRORS as exc:
        raise _undefined_table(exc) from exc
    held: Optional[bool] = None
    if robot is not None and holder is not None:
        try:
            held = await holder.held(robot, map_name)
        except Exception:  # noqa: BLE001
            logger.exception("Stored map of robot %s not readable", robot_name)
    can, why = await _can_start(holder, robot, map_name, held, db=db, switch=switch,
                                reloc_jobs=reloc_jobs, blockers=True)
    return {"available": held is True, "known": held is not None, "source": "orchestrator",
            "can_start": can, "can_start_reason": why, "warning": why}


async def robot_stored_maps(db: Any, holder: Optional[Any], robot_name: str) -> Dict[str, Any]:
    """GET /api/v1/robots/{robot}/stored-maps: {known, maps: [{cloud_map_id, name, valid,
    saved_at, size_bytes}]}, the cloud maps the robot's orchestrator holds as stored SLAM maps
    (one cached /maps/list read). `known` false (maps []): robot offline, no orchestrator or
    not askable. 404 unknown robot."""
    try:
        async with open_store(db, uuid.uuid4()) as store:
            robot = await store.robot(robot_name)
    except _SCHEMA_ERRORS as exc:
        raise _undefined_table(exc) from exc
    if robot is None:
        raise HTTPException(404, f"Did not find \"robot\" with name \"{robot_name}\"")
    found = None
    if holder is not None:
        try:
            found = await holder.stored(robot)
        except Exception:  # noqa: BLE001
            logger.exception("Stored maps of robot %s not readable", robot_name)
    return {"known": found is not None, "maps": found or []}


async def placement_suggestions(db: Any, map_name: str, session_id: str,
                                holder: Optional[Any] = None, switch: Optional[Any] = None,
                                reloc_jobs: Optional[Any] = None) -> Dict[str, Any]:
    """The placement suggestions (_placement_suggestions) plus `reloc` (reloc_status): null, or
    {available, known, source}; with `available` the client skips the manual placement."""
    out = await _placement_suggestions(db, map_name, session_id)
    out["reloc"] = await reloc_status(db, holder, map_name, session_id, switch, reloc_jobs)
    return out


async def _placement_suggestions(db: Any, map_name: str, session_id: str) -> Dict[str, Any]:
    """GET /api/v1/maps/{id}/sessions/{sid}/placement-suggestions: "last position on this map"
    for an UNPLACED session (the robot restarted: run_changed). At most one suggestion, the
    first source that works:

    - `unplace_snapshot`: the dispatcher stored the robot's last pose (old run frame) in
      placement.last_robot_pose when it unplaced the session; with the session's own
      map_t_session (kept by the unplace) that is where the robot stood on the map;
    - `state_history`: a session unplaced before that was stored: the robot's last
      robot_state_ts row before placement.unplaced_at (robot clock vs server clock: a skew of a
      few seconds can shift the cut), with the session's own map_t_session;
    - `finished_session`: the robot's most recent finished session on this map that ended
      placed: its last robot_state_ts row while it ran, with that session's map_t_session.

    The suggestion's map_T_session pairs the last map pose with the robot's FIRST pose in its
    current run (robot_pose; the odometry origin when unknown), so it stays right when the
    robot drove since the restart. Accepting is the ordinary POST .../place with `pose` and
    the live robot pose. 404 unknown map/session; 409 finished session; a placed session: no
    suggestions. A GEO map instead offers the robot's current datum (datum_suggestion, source
    "datum"; accepted with POST .../place {"source": "datum"}), or nothing without one."""
    try:
        sid = str(uuid.UUID(str(session_id)))
    except ValueError:
        raise HTTPException(404, f"Did not find session \"{session_id}\"") from None
    out: Dict[str, Any] = {"map_id": map_name, "session_id": sid, "suggestions": []}
    try:
        async with open_store(db, uuid.uuid4()) as store:
            row = await store.lock_map(map_name)
            if row is None:
                raise HTTPException(404, f"Did not find \"map\" with name \"{map_name}\"")
            session = await store.lock_session(sid)
            if session is None or session["map_name"] != map_name:
                raise HTTPException(404, f"Did not find session \"{sid}\" on map "
                                         f"\"{map_name}\"")
            if session["ended_at"] is not None:
                raise HTTPException(409, f"Session {sid} is finished")
            if ms.is_placed(session):
                return out
            robot_name = session["robot_name"]
            if row.type == "geo":
                # A geo session is placed by the robot's datum: offer the current one.
                found = datum_suggestion(row.spec.get("geo"), await store.robot(robot_name),
                                         session)
                if found is not None:
                    out["suggestions"].append(found)
                return out
            placement = session.get("placement") or {}
            unplaced_at = _parse_ts(placement.get("unplaced_at"))
            epoch = await store.robot_run_start(robot_name)
            epoch_start = (_parse_ts(epoch[0]) if epoch is not None and epoch[1] == "run_changed"
                           else None)

            # a. the dispatcher's snapshot
            found = None
            if session.get("map_t_session") is not None:
                lower = unplaced_at or epoch_start
                start = await _run_start_pose(store, robot_name, lower)
                last = placement.get("last_robot_pose")
                if last is not None:
                    found = ms.last_position_suggestion(session["map_t_session"], last, start)
                    if found is not None:
                        out["suggestions"].append(_suggestion(
                            "unplace_snapshot", found, placement.get("unplaced_at"), sid))
                        return out
                # b. the robot's recorded history before the unplace
                if last is None and unplaced_at is not None:
                    seen = await store.robot_state_pose(robot_name, end=unplaced_at)
                    if seen is not None:
                        found = ms.last_position_suggestion(session["map_t_session"], seen,
                                                            start)
                        if found is not None:
                            out["suggestions"].append(_suggestion(
                                "state_history", found, seen["ts"], sid))
                            return out
            # c. the robot's last finished session on this map that ended placed
            for old in await store.sessions_page(map_name, 50, None):
                if (old["robot_name"] != robot_name or old["ended_at"] is None
                        or not ms.is_placed(old) or old.get("map_t_session") is None
                        or str(old["session_id"]) == sid):
                    continue
                ended = _parse_ts(old["ended_at"])
                seen = await store.robot_state_pose(
                    robot_name, start=_parse_ts(old["started_at"]), end=ended)
                if seen is None:
                    continue
                lower = max((t for t in (ended, epoch_start) if t is not None), default=None)
                start = await _run_start_pose(store, robot_name, lower)
                found = ms.last_position_suggestion(old["map_t_session"], seen, start)
                if found is not None:
                    out["suggestions"].append(_suggestion(
                        "finished_session", found, seen["ts"], old["session_id"]))
                break
    except _SCHEMA_ERRORS as exc:
        raise _undefined_table(exc) from exc
    return out


async def _archive_or_restore(db: Any, name: str, archive: bool, publisher_id: uuid.UUID,
                              actor: Optional[str],
                              arango_node_count: Optional[Callable[[str], int]] = None
                              ) -> Dict[str, Any]:
    now = _utcnow()
    counts = {} if archive else await _node_counts(arango_node_count, [name])
    async with open_store(db, publisher_id) as store:
        row = await _lock_alive_map(store, name)
        state = row.state
        if archive:
            if state == ARCHIVED:
                return {"map_id": name, "state": state, "changed": False}
            open_sessions = [s for s in await store.sessions(name) if s["ended_at"] is None]
            if open_sessions:
                raise HTTPException(409, f"Map '{name}' is in use by "
                                         f"{_robots_phrase(open_sessions)}; finish those "
                                         "sessions before archiving")
            if state in OPEN_STATES:
                raise HTTPException(409, f"Map '{name}' has an open mapping session; finish "
                                         "it before archiving")
            new_state, code = ARCHIVED, EventCode.MAP_ARCHIVED
        else:
            if state != ARCHIVED:
                return {"map_id": name, "state": state, "changed": False}
            # the same rule as a finish: `ready` only with data (nodes or a saved SLAM map)
            new_state = (READY if _has_data(row, await store.sessions(name), counts.get(name))
                         else DRAFT)
            code = EventCode.MAP_RESTORED
        await store.update_map(row, status={"state": new_state})
        await store.emit(_map_event(code, name, row.type, new_state, actor, now))
    return {"map_id": name, "state": new_state, "changed": True}


async def archive_map(db: Any, name: str, publisher_id: uuid.UUID,
                      actor: Optional[str] = None) -> Dict[str, Any]:
    return await _archive_or_restore(db, name, True, publisher_id, actor)


async def restore_map(db: Any, name: str, publisher_id: uuid.UUID,
                      actor: Optional[str] = None,
                      arango_node_count: Optional[Callable[[str], int]] = None
                      ) -> Dict[str, Any]:
    """Archived -> `ready` when the map holds data (_has_data: nodes or a saved SLAM map), else
    `draft`."""
    return await _archive_or_restore(db, name, False, publisher_id, actor, arango_node_count)


# --- type conversion (geo <-> local) -----------------------------------------------------------

class MapPoint(pydantic.BaseModel):
    """A point in the map frame (metres)."""
    x: float = 0.0
    y: float = 0.0

    class Config:
        extra = pydantic.Extra.forbid

    _check = pydantic.validator("x", "y", allow_reuse=True)(_finite)


GEO_FIELDS = ("latitude", "longitude", "bearing_deg", "frame", "anchor", "utm_zone", "utm_north")


class ConvertTypeRequest(pydantic.BaseModel):
    """POST /api/v1/maps/{id}/type. `{"type": "local"}`, or `{"type": "geo", latitude, longitude,
    bearing_deg?, frame?, anchor?, utm_zone?, utm_north?}`: the map-frame point `anchor`
    (default the origin) is at (latitude, longitude) and the map's +X axis points `bearing_deg`
    from east, counter-clockwise (the datum_bearing_deg convention): from UTM grid east for
    frame "utm" (default), from true east at the anchor for "enu"."""
    type: str
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    bearing_deg: Optional[float] = None
    frame: Optional[str] = None
    anchor: Optional[MapPoint] = None
    utm_zone: Optional[int] = None
    utm_north: Optional[bool] = None

    class Config:
        extra = pydantic.Extra.forbid

    @pydantic.validator("type")
    def _type(cls, value):  # noqa: N805
        if value not in MAP_TYPES:
            raise ValueError(f"type must be one of {', '.join(MAP_TYPES)}")
        return value

    @pydantic.validator("latitude", "longitude", "bearing_deg")
    def _finite_or_none(cls, value):  # noqa: N805
        if value is not None and not math.isfinite(value):
            raise ValueError("must be a finite number")
        return value

    @pydantic.validator("frame")
    def _frame(cls, value):  # noqa: N805
        if value is not None and value not in ("utm", "enu"):
            raise ValueError("frame must be 'utm' or 'enu'")
        return value

    @pydantic.validator("utm_zone")
    def _zone(cls, value):  # noqa: N805
        if value is not None and not 1 <= value <= 60:
            raise ValueError("utm_zone must be 1 .. 60")
        return value

    @pydantic.root_validator(skip_on_failure=True)
    def _fields(cls, values):  # noqa: N805
        given = [k for k in GEO_FIELDS if values.get(k) is not None]
        if values["type"] == "local":
            if given:
                raise ValueError(f"{', '.join(given)}: only for a conversion to geo")
            return values
        lat, lon = values.get("latitude"), values.get("longitude")
        if lat is None or lon is None:
            raise ValueError("latitude and longitude are required for a conversion to geo")
        if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
            raise ValueError("latitude must be -90 .. 90 and longitude -180 .. 180")
        if not has_real_datum(lat, lon):
            raise ValueError("(0, 0) is the 'no location' placeholder, not a location")
        return values


def _former_datum(row: MapRow, now: datetime.datetime) -> Optional[Dict[str, Any]]:
    """What a geo map's georeference becomes on its way to local (`former_datum`): its geo
    block, or for a geo map without one (a draft whose first session never came) its legacy
    datum_*; None without either."""
    block = row.spec.get("geo")
    if not block:
        datum = map_geo.map_datum(row.spec)
        if datum is None:
            return None
        block = map_geo.geo_of_datum_frame(datum)
    out = map_geo.former_datum_of(block)
    out["converted_at"] = now.isoformat()
    return out


LOCAL_DATUM_PATCH = {"datum_latitude": None, "datum_longitude": None, "datum_bearing_deg": 0.0,
                     "datum_frame": "enu", "datum_utm_zone": None, "datum_utm_north": None,
                     "datum_utm_easting": None, "datum_utm_northing": None}


def plan_type_change(row: MapRow, req: ConvertTypeRequest, now: datetime.datetime
                     ) -> Dict[str, Any]:
    """The spec patch of a conversion (pure). Map-frame coordinates are never touched:
    - to local: type, no `geo`, no datum_*, `former_datum` = the old georeference, and the
      old origin as the map's `approx_location` (a hint for pins and sorting);
    - to geo: `geo` from the anchor (map_geo.geo_from_anchor: zone, origin, bearing_deg), the
      datum_* = that origin as a 'utm' datum with the map's rotation (what the client's
      `transform` and the planner's legacy path read), no approx_location / former_datum.
    422 for an anchor that cannot be georeferenced (outside UTM, a zone too far away)."""
    if req.type == "local":
        former = _former_datum(row, now)
        patch: Dict[str, Any] = {"type": "local", "geo": None, **LOCAL_DATUM_PATCH,
                                 "former_datum": former}
        if former is not None:
            patch["approx_location"] = {"latitude": former["latitude"],
                                        "longitude": former["longitude"], "accuracy_m": None,
                                        "source": "manual", "set_at": now.isoformat()}
        return patch
    anchor = req.anchor or MapPoint()
    try:
        block = map_geo.geo_from_anchor(
            req.latitude, req.longitude, anchor.x, anchor.y, req.bearing_deg or 0.0,
            frame=req.frame, utm_zone=req.utm_zone, utm_north=req.utm_north)
    except ValueError as exc:
        raise HTTPException(422, [{"loc": ["body"], "type": "value_error",
                                   "msg": str(exc)}]) from exc
    return {"type": "geo", "geo": block, **origin_as_legacy_datum(block),
            "approx_location": None, "former_datum": None, "slam_map": False}


def _conversion_notes(new_type: str, sessions: List[Mapping[str, Any]],
                      datums: Mapping[str, Optional[Dict[str, Any]]],
                      slam_cleared: bool = False) -> List[str]:
    """What the conversion means for the robots using the map (the response's `warnings`)."""
    notes: List[str] = []
    for s in sorted(sessions, key=lambda x: str(x["robot_name"])):
        robot = s["robot_name"]
        has_datum = datums.get(robot) is not None
        if ms.is_placed(s):
            notes.append(f"{robot} keeps its placement (the map frame did not move)"
                         + (": after its next restart it is placed by its GNSS datum"
                            if new_type == "geo" and has_datum else "")
                         + ("; it has no GNSS datum, so after its next restart it cannot be "
                            "placed on this geo map (convert it back or stop using it)"
                            if new_type == "geo" and not has_datum else ""))
        elif new_type == "geo":
            notes.append(f"{robot} is not placed: "
                         + ("place it from its GNSS datum (placement suggestion "
                            "source \"datum\")" if has_datum else
                            "it has no GNSS datum, so it cannot be placed on a geo map"))
        else:
            notes.append(f"{robot} is not placed: place it by hand or by relocalization")
    if slam_cleared:
        notes.append("This map no longer records a SLAM map (only local maps do); a SLAM map "
                     "already saved on a robot is kept there")
    if new_type == "geo":
        notes.append("Robots without a GNSS datum cannot start a session on this map now "
                     "(a geo map is placed by the robot's datum)")
    else:
        notes.append("The map is no longer georeferenced: no street tiles, no GPS goals; "
                     "robots are placed by hand or by relocalization")
    return notes


async def convert_map_type(db: Any, name: str, data: Any, publisher_id: uuid.UUID,
                           actor: Optional[str] = None,
                           reloc_jobs: Optional[Any] = None) -> Dict[str, Any]:
    """POST /api/v1/maps/{id}/type: convert geo <-> local (ConvertTypeRequest). Nothing moves:
    the map-frame coordinates of nodes, edges, reconstruction results, sessions and missions
    stay valid. One transaction (map row locked, so it serialises with session starts):

    - 404 unknown map; 409 deleting, already of that type, or an open (or paused) MAPPING
      session (its nodes would land in a frame whose meaning changes under them) or a running
      relocalization job (`reloc_jobs`) of this map (it would place a session with the
      identity transform on a map that is no longer local); 422 body;
    - open OPERATE sessions stay open and keep map_T_session (it is in map-frame terms). To
      geo: a placed session gets the robot's current datum stamped as its `datum`, so the
      dispatcher does not re-derive it from the very same datum (it does from the next
      different one, as on any geo map); an unplaced one is placed from the datum later
      (the dispatcher, or POST .../place {"source": "datum"}). To local: nothing to do (a
      local session ignores datums);
    - MAP.TYPE_CHANGED (source api).
    The response: {map_id, changed, old_type, type, map (as GET /maps lists it), operating:
    [{robot, session_id, aligned}], warnings: [..]}."""
    req = parse_body(ConvertTypeRequest, data)
    now = _utcnow()
    try:
        async with open_store(db, publisher_id) as store:
            row = await _lock_alive_map(store, name)
            old_type = row.type
            if req.type == old_type:
                raise HTTPException(409, f"Map '{name}' is already a {old_type} map")
            open_sessions = [s for s in await store.sessions(name) if s["ended_at"] is None]
            mapping = [s for s in open_sessions if ms.purpose_of(s) == ms.MAPPING]
            if mapping or row.state in OPEN_STATES:
                who = _robots_phrase(mapping) if mapping else "a robot"
                raise HTTPException(409, f"Map '{name}' has an open mapping session "
                                         f"({who}); finish it before converting the map "
                                         "(nodes arriving meanwhile would land in a frame "
                                         "whose meaning changed)")
            if reloc_jobs is not None and reloc_jobs.active_for_map(name) is not None:
                raise HTTPException(409, f"A relocalization is running on map '{name}'; wait "
                                         "for it to finish or cancel it before converting")
            old_geo = row.spec.get("geo")
            slam_cleared = bool(row.spec.get("slam_map")) and req.type == "geo"
            patch = plan_type_change(row, req, now)
            await store.update_map(row, spec=patch)
            row.spec.update(patch)
            datums: Dict[str, Optional[Dict[str, Any]]] = {}
            for s in open_sessions:
                robot = await store.robot(s["robot_name"])
                datum = map_geo.robot_datum(robot.datum) if robot is not None else None
                if req.type == "geo" and ms.geo_transform_for(patch["geo"], "geo",
                                                              datum) is None:
                    datum = None  # none, or in another zone: cannot place it on this map
                datums[s["robot_name"]] = datum
                if req.type == "geo" and ms.is_placed(s) and datum is not None:
                    locked = await store.lock_session(str(s["session_id"]))
                    if locked is not None and locked["ended_at"] is None and ms.is_placed(locked):
                        await store.update_session(str(s["session_id"]), datum=datum)
            operating = [{"robot": s["robot_name"], "session_id": str(s["session_id"]),
                          "aligned": s.get("aligned")}
                         for s in sorted(open_sessions, key=lambda x: str(x["robot_name"]))]
            await store.emit(Event(
                EventCode.MAP_TYPE_CHANGED, now, source=Source.API,
                discriminator=f"map:{name}:type:{req.type}:{now.isoformat()}",
                payload={"map_name": name, "old_type": old_type, "new_type": req.type,
                         "geo": patch.get("geo"), "old_geo": old_geo,
                         "operating": [o["robot"] for o in operating], "actor": actor}))
    except _SCHEMA_ERRORS as exc:
        raise _undefined_table(exc) from exc
    obj = MapObjectV1(name=name, lifecycle=ObjectLifecycleV1[row.lifecycle], status=row.status,
                      **row.spec)
    return {"map_id": name, "changed": True, "old_type": old_type, "type": req.type,
            "map": map_view(obj), "operating": operating,
            "warnings": _conversion_notes(req.type, open_sessions, datums, slam_cleared)}


REFUSE_OPEN_SESSION_SQL = (f"SELECT robot_name, purpose FROM {SESSIONS_TABLE} "
                           "WHERE map_name = %s AND ended_at IS NULL ORDER BY robot_name")
LOCK_MAP_SQL = f"SELECT 1 FROM {MAP_TABLE} WHERE name = %s FOR UPDATE"


async def refuse_open_session(cursor: Any, map_id: str) -> None:
    """DELETE /api/v1/maps/{id} guard, run by MapDeleter.request() in its own transaction before
    the map is marked DELETING: the map row lock serialises it with a session start. 409 while
    ANY session is open (mapping or operate), naming the robots (decision Q-U2)."""
    await cursor.execute(LOCK_MAP_SQL, (map_id,))
    await cursor.execute(REFUSE_OPEN_SESSION_SQL, (map_id,))
    rows = await cursor.fetchall()
    if rows:
        sessions = [{"robot_name": r[0], "purpose": r[1]} for r in rows]
        raise HTTPException(409, f"Map '{map_id}' is in use by {_robots_phrase(sessions)}; "
                                 "finish those sessions before deleting the map")


# --- sessions ----------------------------------------------------------------------------------

async def _node_counts(arango_node_count: Optional[Callable[[str], int]],
                       names: Sequence[Optional[str]]) -> Dict[str, Optional[int]]:
    """{map: ArangoDB node count} read BEFORE a transaction (the count is a blocking
    python-arango call: in a thread, never while row locks are held). None for a map whose
    count failed; {} without a counter. Never raises."""
    out: Dict[str, Optional[int]] = {}
    if arango_node_count is None:
        return out
    for name in dict.fromkeys(n for n in names if n):
        try:
            out[name] = int(await asyncio.to_thread(arango_node_count, name) or 0)
        except Exception:  # noqa: BLE001 - fall back to the stored counts
            logger.warning("ArangoDB node count of %s failed; using the stored counts", name)
            out[name] = None
    return out


def _map_has_nodes(row: MapRow, previous: Sequence[Mapping[str, Any]],
                   count: Optional[int]) -> bool:
    """Whether the map holds nodes (then a new mapping session on a local map must be placed,
    decision Q-U4): ArangoDB's `count` (read before the transaction) when known, else the
    sessions' and the row's stored counts."""
    if count is not None:
        return count > 0
    return (any(int(s.get("node_count") or 0) > 0 for s in previous)
            or int(row.status.get("node_count") or 0) > 0)


def _has_data(row: MapRow, sessions: Sequence[Mapping[str, Any]], count: Optional[int]) -> bool:
    """Whether the map holds data, i.e. is `ready` rather than `draft` after its last mapping
    session finished (or when it is restored): nodes (_map_has_nodes) or a saved SLAM map
    (status.slam_saved_at)."""
    return bool(row.status.get("slam_saved_at")) or _map_has_nodes(row, sessions, count)


def plan_session(row: MapRow, robot: RobotObjectV1, previous: List[Dict[str, Any]],
                 purpose: str = ms.MAPPING, placement: Optional[Dict[str, Any]] = None,
                 carried: Optional[Mapping[str, Any]] = None, has_nodes: bool = False
                 ) -> Dict[str, Any]:
    """What a new session on `row` by `robot` records, and what it changes on the map:
    {'datum', 'map_t_session', 'aligned', 'placement', 'spec'} (pure; 409 if a geo map has no
    datum). `placement`: {pose, robot_pose, source, actor, at} from the request; `carried`: the
    robot's placed session on this local map that is being replaced (same run)."""
    map_type = row.type
    spec_patch: Dict[str, Any] = {}
    if row.obj.type is None:
        spec_patch["type"] = map_type
    if map_type == "geo":
        datum = map_geo.robot_datum(robot.datum)
        if datum is None:
            raise HTTPException(409, f"Map '{row.name}' is a geo map and robot "
                                     f"'{robot.name}' has no datum; start the robot's "
                                     "localization (GNSS) first")
        geo = row.spec.get("geo")
        if not geo:
            geo = map_geo.geo_from_datum(datum)
            spec_patch["geo"] = geo
            if row.obj.datum_latitude is None:
                spec_patch.update(origin_as_legacy_datum(geo))
        return {"datum": datum, "map_t_session": map_geo.session_transform(geo, datum),
                "aligned": True, "placement": None, "spec": spec_patch}
    if placement is not None:
        return {"datum": None,
                "map_t_session": ms.placement_transform(placement["pose"],
                                                        placement["robot_pose"]),
                "aligned": True, "placement": placement, "spec": spec_patch}
    if carried is not None and ms.is_placed(carried):
        return {"datum": None, "map_t_session": ms.transform_of(carried.get("map_t_session")),
                "aligned": True,
                "placement": {"source": ms.SOURCE_SESSION,
                              "from_session_id": str(carried["session_id"]),
                              "at": _iso(_utcnow())},
                "spec": spec_patch}
    first_of_empty = purpose == ms.MAPPING and not has_nodes
    return {"datum": None, "map_t_session": dict(map_geo.IDENTITY), "aligned": first_of_empty,
            "placement": None, "spec": spec_patch}


def origin_as_legacy_datum(geo: Mapping[str, Any]) -> Dict[str, Any]:
    """The map's datum_* fields for a geo map origin: a 'utm' datum at the origin with the map's
    rotation as its bearing (0 unless the map was converted from local), i.e. exactly the map
    frame, for the client and the planner (map/load and graph `transform`)."""
    from packages.utils import geo as geo_mod
    lat, lon = geo_mod.utm_to_latlon(geo["origin_e"], geo["origin_n"], geo["utm_zone"],
                                     geo["utm_north"])
    return {"datum_latitude": lat, "datum_longitude": lon,
            "datum_bearing_deg": float(geo.get("bearing_deg") or 0.0),
            "datum_frame": "utm", "datum_utm_zone": geo["utm_zone"],
            "datum_utm_north": geo["utm_north"], "datum_utm_easting": geo["origin_e"],
            "datum_utm_northing": geo["origin_n"]}


async def placement_warnings(store: Any, robot: RobotObjectV1,
                             robot_pose: Optional[Mapping[str, Any]]) -> List[str]:
    """Non-blocking notes for a MANUAL placement (decision 2026-10-08: the user can always place
    a robot by hand): the robot drives, or moved from `robot_pose` (the pose the user saw)."""
    out: List[str] = []
    driving = await _driving_text(store, robot)
    if driving is not None:
        out.append(f"Robot '{robot.name}' is driving ({driving}): the placement may be off")
    if robot_pose is not None:
        pose = robot.status.pose
        moved = ms.pose_moved(robot_pose, {"x": pose.x, "y": pose.y, "theta": pose.theta})
        if moved is not None:
            out.append(f"Robot '{robot.name}' moved: {moved}; the placement may be off")
    return out


async def _driving_text(store: Any, robot: RobotObjectV1) -> Optional[str]:
    state = robot.status.state.value if robot.status.state is not None else None
    mission_open = (await store.robot_mission_open(robot.name)
                    if state in ("ON_TASK", "MAP_DEPLOYMENT") else None)
    return ms.driving_reason(state, await store.robot_state_msg(robot.name), mission_open)


def _placement_record(req: PlaceRequest, source: str, actor: Optional[str],
                      now: datetime.datetime) -> Dict[str, Any]:
    return {"pose": req.pose.dict(), "robot_pose": req.robot_pose.dict(), "source": source,
            "actor": actor, "at": now.isoformat()}


async def _start_in(store: Any, row: MapRow, robot: Optional[RobotObjectV1], robot_name: str,
                    now: datetime.datetime, actor: Optional[str],
                    req: Optional[StartSessionRequest] = None,
                    carried: Optional[Mapping[str, Any]] = None,
                    node_count: Optional[int] = None,
                    warnings: Optional[List[str]] = None
                    ) -> Dict[str, Any]:
    """Start a session on the locked map `row` inside the caller's transaction (the rules of
    the module docstring); the new session row. The robot's previous open session must already
    be finished (`replace`); `carried` is that session when it was on this map. `node_count`:
    the map's ArangoDB node count, read before the transaction (None: the stored counts)."""
    req = req or StartSessionRequest(robot=robot_name)
    purpose = req.purpose
    map_name = row.name
    if row.state == ARCHIVED:
        raise HTTPException(409, f"Map '{map_name}' is archived; restore it first")
    if robot is None:
        raise HTTPException(404, f"Did not find \"robot\" with name \"{robot_name}\"")
    if not robot.status.online:
        raise HTTPException(409, f"Robot '{robot_name}' is offline")
    # _open_tx already resolved the robot's open session (refused it, or replaced/finished it)
    # before calling us, so the robot has none here; no need to check again.
    if purpose == ms.OPERATE and row.state == DRAFT:
        raise HTTPException(409, f"Map '{map_name}' is a draft without data; there is nothing "
                                 "to use yet (start mapping it)")
    previous = await store.sessions(map_name)
    if purpose == ms.MAPPING and any(s["ended_at"] is None and ms.purpose_of(s) == ms.MAPPING
                                     for s in previous):
        raise HTTPException(409, f"Map '{map_name}' already has an open mapping "
                                 "session (one robot maps a map at a time)")
    placement = None
    if req.placement is not None:
        if row.type == "geo":
            raise HTTPException(422, [{"loc": ["body", "placement"], "type": "value_error",
                                       "msg": "a geo map is placed by the robot's datum; "
                                              "placement is for local maps"}])
        if req.placement.source in POSELESS_SOURCES:
            raise HTTPException(422, [{"loc": ["body", "placement", "source"],
                                       "type": "value_error",
                                       "msg": f"a {req.placement.source} placement is made "
                                              "with POST .../place after the session "
                                              "started"}])
        if warnings is not None:
            warnings += await placement_warnings(store, robot, req.placement.robot_pose.dict())
        placement = _placement_record(req.placement, ms.SOURCE_USER, actor, now)
    carried = carried if carried is not None and ms.is_placed(carried) else None
    if carried is None and placement is None and row.type == "local":
        # §14.13: the robot's last session on this map, finished placed in the run it is
        # still in, lends its placement (the robot has not restarted since).
        carried = ms.reusable_session(previous, robot_name,
                                      await store.robot_run_epoch(robot_name))
    has_nodes = (row.type == "local" and placement is None and carried is None
                 and _map_has_nodes(row, previous, node_count))
    services = req.session_services(row.type == "local" and bool(row.spec.get("slam_map")))
    if services is not None and ms.SLAM in services and row.type != "local":
        # Only a geo map cannot record one (the SLAM map is a local frame; see PATCH slam_map).
        raise HTTPException(400, f"Map '{map_name}' is a geo map: a SLAM map can only be "
                                 "recorded for a local map")
    plan = plan_session(row, robot, previous, purpose, placement, carried, has_nodes)
    if services is not None and ms.SLAM in services and not row.spec.get("slam_map"):
        # The user chose to record a SLAM map for this map: it becomes a slam_map map.
        plan["spec"] = {**(plan["spec"] or {}), "slam_map": True}
        await store.emit(Event(
            EventCode.MAP_SLAM_CHANGED, now, source=Source.API,
            discriminator=f"map:{map_name}:slam:True:{now.isoformat()}",
            payload={"map_name": map_name, "slam_map": True, "actor": actor}))
    session = {"session_id": str(uuid.uuid4()), "map_name": map_name,
               "robot_name": robot_name, "kind": "live", "purpose": purpose,
               "services": services, "placement": plan["placement"],
               "started_at": now, "paused_at": None, "ended_at": None, "datum": plan["datum"],
               "map_t_session": plan["map_t_session"], "aligned": plan["aligned"],
               "node_count": 0}
    await store.insert_session(session)
    if purpose == ms.MAPPING:
        await store.update_map(row, spec=plan["spec"] or None,
                               status={"state": MAPPING, "open_session_id": session["session_id"]})
        row.status.update(state=MAPPING, open_session_id=session["session_id"])
    elif plan["spec"]:
        await store.update_map(row, spec=plan["spec"])
    row.spec.update(plan["spec"] or {})
    await store.emit(_session_event(EventCode.MAP_SESSION_STARTED, session, row.state, actor,
                                    now))
    return session


async def _finish_in(store: Any, row: Optional[MapRow], session: Dict[str, Any],
                     now: datetime.datetime, actor: Optional[str],
                     node_count: Optional[int] = None) -> Optional[str]:
    """Finish the open `session` inside the caller's transaction; the map's state afterwards.
    A mapping session makes its map `ready` when it holds data (_has_data: nodes or a saved
    SLAM map) and `draft` otherwise, unless another mapping session is still open; an operate
    session leaves the map as it is. `row`: the locked map row, None when it is gone.
    `node_count`: the map's ArangoDB node count read before the transaction (None: the stored
    counts)."""
    session_id = str(session["session_id"])
    # §14.13: the run epoch the placement belongs to, so a later session on this map in the
    # same run can reuse it (None: never reused).
    stamp = ms.epoch_to_stamp(session, await store.robot_run_epoch(session["robot_name"])
                              if ms.is_placed(session) else None)
    run_epoch = uuid.UUID(stamp) if stamp is not None else None
    session.update(ended_at=now, paused_at=None, run_epoch=run_epoch)
    await store.update_session(session_id, ended_at=now, paused_at=None, run_epoch=run_epoch)
    if row is None:
        return None
    if ms.purpose_of(session) == ms.MAPPING:
        sessions = await store.sessions(row.name)
        others = [s for s in sessions
                  if s["ended_at"] is None and str(s["session_id"]) != session_id
                  and ms.purpose_of(s) == ms.MAPPING]
        state = READY if _has_data(row, sessions, node_count) else DRAFT
        status = ({"state": state, "open_session_id": None} if not others else
                  {"open_session_id": str(others[0]["session_id"])})
        await store.update_map(row, status=status)
        row.status.update(status)
    map_state = row.state
    await store.emit(_session_event(EventCode.MAP_SESSION_FINISHED, session, map_state, actor,
                                    now))
    return map_state


def _services_of(session: Mapping[str, Any], orchestrator_only: bool = True) -> List[str]:
    """The services a MAPPING session runs, as stored (null = a row from before the column:
    topo; an empty list is a session that runs nothing); [] for an operate session.
    `orchestrator_only` (default) leaves out `slam`, which is no orchestrator service (it is
    driven by start_slam / save_slam)."""
    if ms.purpose_of(session) != ms.MAPPING:
        return []
    stored = session.get("services")
    services = list(ms.DEFAULT_SERVICES) if stored is None else list(stored)
    return [s for s in services if s != ms.SLAM] if orchestrator_only else services


async def start_services(switch: Optional[Any], session: Mapping[str, Any],
                         robot: Optional[RobotObjectV1]) -> List[Dict[str, Any]]:
    """Start the session's mapping services on the robot's orchestrator: the robot actions
    (mapping_switch.robot_action). Never call it inside a DB transaction (the call can take
    ORCHESTRATOR_START_TIMEOUT_S). NEVER raises or fails the caller: a failure is an action with
    ok false. [] for an operate session or without a switch."""
    services = _services_of(session)
    if switch is None or not services:
        return []
    if robot is None:
        return [service_action(svc, START, "failed", f"robot '{session['robot_name']}' not found")
                for svc in services]
    try:
        return await switch.start(robot, services)
    except Exception as exc:  # noqa: BLE001 - the switch does not raise; belt and braces
        logger.exception("Mapping services of robot %s not started", session["robot_name"])
        return [service_action(svc, START, "failed", str(exc)) for svc in services]


async def stop_services(db: Any, switch: Optional[Any], robot: Optional[RobotObjectV1],
                        session: Mapping[str, Any], keep: bool = True) -> List[Dict[str, Any]]:
    """After a session change committed: stop the mapping services of `session` (paused or
    finished) unless (`keep`) the robot's open, unpaused mapping session runs them (a repeated
    finish of an OLD session must not stop the robot's current session's services). A replaced
    session's are always stopped (`keep` false): the new session starts its own after the SLAM
    switch, and the robot refuses a mode change while its topomap runs. The robot actions; never
    raises; [] when there was nothing to stop."""
    services = _services_of(session)
    if switch is None or not services:
        return []
    try:
        if keep:
            async with open_store(db, uuid.uuid4()) as store:
                mine = await store.open_sessions_of_robot(session["robot_name"])
            running = {s for o in mine
                       if ms.purpose_of(o) == ms.MAPPING and o["paused_at"] is None
                       for s in _services_of(o)}
            services = [s for s in services if s not in running]
        if not services:
            return []
        if robot is None:
            return [service_action(svc, STOP, "failed",
                                   f"robot '{session['robot_name']}' not found")
                    for svc in services]
        return await switch.stop(robot, services)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Mapping services of robot %s not stopped", session["robot_name"])
        return [service_action(svc, STOP, "failed", str(exc)) for svc in services]


def slam_save_reporter(db: Any, session: Mapping[str, Any], switch: Optional[Any] = None,
                       event: bool = True, follow_up: bool = True
                       ) -> Callable[[SlamResult], Awaitable[None]]:
    """The `on_result` of a SLAM save of `session`'s map:
    - a saved map: the map's status.slam_saved_at is set and a `draft` map becomes `ready`
      (it holds data now; mark_slam_saved);
    - `event`: MAP.SLAM_SAVE_DONE / MAP.SLAM_SAVE_FAILED (nothing for 'nothing to save');
    - `follow_up` (with `switch`): the robot's open, unpaused mapping session gets its services
      (re)started in the background (restart_session_services): a start deferred while the
      save ran, or a topomap the switch back to the previous mode stopped.
    Raises only what the event write raises (the switch logs it)."""
    snapshot = dict(session)

    async def report(result: SlamResult) -> None:
        robot_name = snapshot["robot_name"]
        if follow_up and switch is not None:
            reason = {SLAM_SAVED: RESTART_SAVE_DONE, SLAM_FAILED: RESTART_SAVE_FAILED}.get(
                result.status, RESTART_SAVE_DONE)
            # after this save task is done: the restart must not see it as pending
            switch.spawn(restart_session_services(db, switch, robot_name, reason),
                         after_save_of=robot_name)
        if result.status == SLAM_SAVED:
            await mark_slam_saved(db, snapshot["map_name"])
        if not event or result.status not in (SLAM_SAVED, SLAM_FAILED):
            return
        saved = result.status == SLAM_SAVED
        action = slam_save_action(result)
        now = _utcnow()
        sid = str(snapshot["session_id"])
        ev = Event(
            EventCode.MAP_SLAM_SAVE_DONE if saved else EventCode.MAP_SLAM_SAVE_FAILED, now,
            robot_name=robot_name, source=Source.API,
            discriminator=f"session:{sid}:slam_save:{now.isoformat()}",
            payload={"map_name": snapshot["map_name"], "session_id": sid,
                     "status": "saved" if saved else "failed", "label": action["label"],
                     "detail": result.warning or result.notice})
        async with open_store(db, uuid.uuid4()) as store:
            await store.emit(ev)

    return report


async def mark_slam_saved(db: Any, map_name: str) -> None:
    """A SLAM map of `map_name` was saved on a robot: status.slam_saved_at, and a `draft` map
    (its sessions finished without nodes) becomes `ready`. Never raises."""
    try:
        async with open_store(db, uuid.uuid4()) as store:
            row = await store.lock_map(map_name)
            if row is None or row.lifecycle == DELETING:
                return
            status: Dict[str, Any] = {"slam_saved_at": _utcnow().isoformat()}
            if row.state == DRAFT:
                status["state"] = READY
            await store.update_map(row, status=status)
    except Exception:  # noqa: BLE001
        logger.exception("Saved SLAM map of %s not recorded on the map", map_name)


def _warning_of(actions: Sequence[Mapping[str, Any]]) -> Optional[str]:
    failed = [a["label"] for a in actions if not a["ok"]]
    return "; ".join(failed) if failed else None


async def notify_robot(switch: Optional[Any], db: Any, robot_name: str,
                       with_service: bool = False,
                       actions: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """After a session change has COMMITTED (and its services were started / stopped): push the
    robot's `session` to /ws/robot/{robot} (switch.on_session) and describe the robot's mapping
    state. Never raises. The response keys (additive): `robot_actions` (only when `actions` is
    given: what the API did on the robot, see module docstring), `robot_notified` (false when
    one failed), `mapping_warning` (only then: the failures joined), `mapping_state`, and with
    `with_service` `mapping_service` ("running" | "not_running") and `mapping_services`
    ({service: running | not_running | not_available}), the state READ from the orchestrator."""
    if switch is None:
        return {}
    current: Optional[Dict[str, Any]] = None
    robot: Optional[RobotObjectV1] = None
    try:
        async with open_store(db, uuid.uuid4()) as store:
            mine = await store.open_sessions_of_robot(robot_name)
            robot = await store.robot(robot_name)
        current = mine[0] if mine else None
    except Exception:  # noqa: BLE001
        logger.exception("Open session of robot %s not readable", robot_name)
    on_session = getattr(switch, "on_session", None)
    if on_session is not None:
        try:
            await on_session(robot_name, ms.robot_session_view(current))
        except Exception:  # noqa: BLE001
            logger.exception("Session update for robot %s not pushed", robot_name)
    if robot is not None:
        try:
            snap = await switch.snapshot(robot, fresh=True)
        except Exception:  # noqa: BLE001
            logger.exception("Mapping state of robot %s not readable", robot_name)
            snap = Snapshot(reachable=None)
    else:
        snap = Snapshot(reachable=None)
    session_row = dict(current, state=ms.session_state(current)) if current else None
    state = snap.state(session_row)
    warning = _warning_of(actions or [])
    out: Dict[str, Any] = {"robot_notified": warning is None, "mapping_state": state}
    if actions is not None:
        out["robot_actions"] = list(actions)
    if warning:
        out["mapping_warning"] = warning
    if with_service:
        out["mapping_service"] = snap.mapping_service()
        out["mapping_services"] = snap.mapping_services()
    on_state = getattr(switch, "on_state", None)
    if on_state is not None and any(a["action"] in (START, STOP) for a in actions or []):
        try:
            await on_state(robot_name, ms.TOPO, state)
        except Exception:  # noqa: BLE001
            logger.exception("Mapping state update for robot %s not pushed", robot_name)
    return out


async def robot_sessions(db: Any) -> Dict[str, Dict[str, Any]]:
    """{robot: its `session` view} for every robot with an open session (one query); robots
    without one are absent (their `session` is null). Never raises: without the table (or on
    any error) every robot reads as mapless and the error is logged."""
    try:
        async with open_store(db, uuid.uuid4()) as store:
            rows = await store.open_sessions()
    except Exception:  # noqa: BLE001
        logger.exception("Open sessions not readable; robots are shown without `session`")
        return {}
    return {r["robot_name"]: ms.robot_session_view(r) for r in rows}


class OpenSessionCache:
    """robot_sessions(), cached for `ttl` seconds: the robot WebSocket pushes a robot_update per
    robot state message, and each carries the robot's `session`. Session changes through this
    API invalidate it; changes by mission-dispatch (unplace, re-place) show within `ttl`."""

    def __init__(self, db: Any, ttl: float = 1.0, clock: Optional[Callable[[], float]] = None):
        import time
        self._db = db
        self.ttl = ttl
        self._clock = clock or time.monotonic
        self._until = float("-inf")
        self._views: Dict[str, Dict[str, Any]] = {}

    async def get(self, robot_name: str) -> Optional[Dict[str, Any]]:
        return (await self.all()).get(robot_name)

    async def all(self) -> Dict[str, Dict[str, Any]]:
        now = self._clock()
        if now >= self._until:
            self._views = await robot_sessions(self._db)
            self._until = now + self.ttl
        return self._views

    def invalidate(self) -> None:
        self._until = float("-inf")


async def _slam_wanted(db: Any, map_name: str) -> bool:
    """Whether `map_name` is a local map recording a SLAM map (read from its row, in a
    transaction of its own that is closed before any orchestrator call). False on any error."""
    try:
        async with open_store(db, uuid.uuid4()) as store:
            row = await store.get_map(map_name)
        return (row is not None and row.type == "local"
                and bool(row.spec.get("slam_map")))
    except Exception:  # noqa: BLE001
        logger.exception("slam_map of map %s not readable; SLAM is skipped", map_name)
        return False


def _slam_session(session: Mapping[str, Any]) -> bool:
    return ms.purpose_of(session) == ms.MAPPING


async def _start_slam(db: Any, switch: Any, session: Mapping[str, Any],
                      robot: Optional[RobotObjectV1]
                      ) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    """Before the topomap of the MAPPING `session` starts (session start, resume, restart):
    start the map's SLAM recording when the session asked for it (`slam` in its services). (the
    warning, the robot action), both None when it did not; never raises, never blocks the
    session."""
    if (switch is None or not _slam_session(session)
            or ms.SLAM not in _services_of(session, orchestrator_only=False)):
        return None, None
    try:
        if robot is None:
            reason = "robot not found"
            return (f"SLAM recording not started: {reason}",
                    slam_start_action(SlamResult(SLAM_FAILED, f"SLAM recording not started: "
                                                              f"{reason}", reason=reason)))
        result = await switch.start_slam(robot, session["map_name"], session.get("session_id"))
        return result.warning, slam_start_action(result)
    except Exception as exc:  # noqa: BLE001
        logger.exception("SLAM start for map %s failed", session["map_name"])
        warning = f"SLAM recording not started: {exc}"
        return warning, slam_start_action(SlamResult(SLAM_FAILED, warning, reason=str(exc)))


async def _slam_recorded(db: Any, switch: Any, session: Mapping[str, Any],
                         robot: Optional[RobotObjectV1]) -> bool:
    """Whether `session` recorded a SLAM map that finish / replace should save: `slam` is in its
    services, or (a session opened before the option existed) its map is a slam_map map and the
    robot's SLAM driver is recording it right now."""
    if ms.SLAM in _services_of(session, orchestrator_only=False):
        return True
    if robot is None or not await _slam_wanted(db, session["map_name"]):
        return False
    return await switch.slam_records(robot, session["map_name"])


async def _save_slam(db: Any, switch: Any, session: Mapping[str, Any],
                     robot: Optional[RobotObjectV1], wait: bool, track: bool = True
                     ) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    """After a MAPPING `session` ended: save the map's SLAM map (the map is read now). `wait`
    awaits the save (replace by a session that records SLAM: the driver must be free for it),
    otherwise it runs in the background (its end is MAP.SLAM_SAVE_DONE / _FAILED, then the
    robot's open mapping session gets its services started: slam_save_reporter) and only an
    immediate problem (robot gone / offline) fails; that one leaves the robot's SLAM save state
    `failed` (retry / discard). `track` false (a deleted robot): no SLAM save state, no
    follow-up. (the warning, the robot action), both None when SLAM is not wanted."""
    if switch is None or not _slam_session(session):
        return None, None
    try:
        if not await _slam_recorded(db, switch, session, robot):
            return None, None
        if robot is None or not robot.status.online:
            warning = (f"SLAM map of '{session['map_name']}' not saved: robot "
                       f"'{session['robot_name']}' is not reachable")
            if track:
                await switch.mark_failed(session["robot_name"], session["map_name"],
                                         session["session_id"], warning)
            return warning, slam_save_action(detail=warning)
        task = switch.schedule_slam_save(
            robot, session["map_name"], session["session_id"],
            on_result=slam_save_reporter(db, session, switch, event=not wait,
                                         follow_up=track and not wait),
            track=track)
        if wait:
            result = await asyncio.shield(task)
            return result.warning, slam_save_action(result)
        return None, slam_save_action()
    except Exception as exc:  # noqa: BLE001
        logger.exception("SLAM save for map %s failed", session["map_name"])
        warning = f"SLAM map not saved: {exc}"
        return warning, slam_save_action(detail=warning)


# `reason` of restart_session_services (MAP.SESSION_SERVICES_RESTARTED)
RESTART_RUN_CHANGED = "run_changed"            # mission-dispatch saw a new robot run
RESTART_SAVE_DONE = "slam_save_done"           # the previous SLAM save ended (or had nothing)
RESTART_SAVE_FAILED = "slam_save_failed"       # the previous SLAM save failed
RESTART_SLAM_DISCARDED = "slam_discarded"      # the failed save was discarded


def _deferred_actions(session: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """The robot actions of a mapping session whose start waits for the robot's pending SLAM
    save (ok: nothing failed; they run when the save ended)."""
    out = []
    services = _services_of(session, orchestrator_only=False)
    for svc in sorted(services, key=lambda x: x != ms.SLAM):   # SLAM first, as when started
        name = SLAM_SERVICE if svc == ms.SLAM else action_name(svc)
        label = ms.SERVICE_LABELS.get(svc, svc) if svc != ms.SLAM else "SLAM recording"
        out.append(robot_action(name, START, True, f"{label} starts when the robot has saved "
                                                   "its previous SLAM map"))
    return out


async def start_session_services(db: Any, switch: Optional[Any], session: Mapping[str, Any],
                                 robot: Optional[RobotObjectV1]
                                 ) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """Start a MAPPING `session` on the robot (opening, resume, restart): its SLAM recording
    first (on the mapping API the topomap needs the slam mode), then its orchestrator services.
    While the robot's previous SLAM map is still being saved nothing is sent to the robot (the
    switch back after the save would stop it again, and a mode change is refused meanwhile):
    the actions say the services start after the save, and the save's follow-up starts them.
    (robot actions, slam warning); never raises; ([], None) for an operate session. Call it
    under the robot's lock, outside any DB transaction."""
    if switch is None or ms.purpose_of(session) != ms.MAPPING:
        return [], None
    if switch.slam_save_pending(session["robot_name"]):
        return _deferred_actions(session), None
    slam_warning, slam_action = await _start_slam(db, switch, session, robot)
    actions = [slam_action] if slam_action else []
    actions += await start_services(switch, session, robot)
    return actions, slam_warning


async def restart_session_services(db: Any, switch: Any, robot_name: str, reason: str,
                                   locked: bool = False, report_failure: bool = True
                                   ) -> Optional[List[Dict[str, Any]]]:
    """Start the services of the robot's open, UNPAUSED MAPPING session again (SLAM first):
    after mission-dispatch reported a run change (a driver / orchestrator restart killed them),
    after the robot's previous SLAM save ended (a start deferred meanwhile, or a topomap the
    switch back stopped) and after a discarded save. Under the robot's lock (`locked`: the
    caller holds it). Reported as MAP.SESSION_SERVICES_RESTARTED (_RESTART_FAILED when an
    action failed; not reported at all with `report_failure` false, for a retry) with the robot
    actions, and pushed like any session change (notify_robot). The robot actions, or None when
    there was nothing to restart (no such session, robot unknown or offline). Never raises."""
    if switch is None:
        return None
    lock = contextlib.nullcontext() if locked else switch.lock(robot_name)
    try:
        async with lock:
            async with open_store(db, uuid.uuid4()) as store:
                mine = await store.open_sessions_of_robot(robot_name)
                robot = await store.robot(robot_name)
            session = mine[0] if mine else None
            if (session is None or ms.purpose_of(session) != ms.MAPPING
                    or session.get("paused_at") is not None or robot is None
                    or not robot.status.online):
                return None
            actions, warning = await start_session_services(db, switch, session, robot)
            if not actions:
                return actions
            ok = all(a["ok"] for a in actions)
            if not ok and not report_failure:
                return actions
            now = _utcnow()
            sid = str(session["session_id"])
            event = Event(
                EventCode.MAP_SESSION_SERVICES_RESTARTED if ok
                else EventCode.MAP_SESSION_SERVICES_RESTART_FAILED, now,
                robot_name=robot_name, source=Source.API,
                discriminator=f"session:{sid}:restart:{reason}:{now.isoformat()}",
                payload={"map_name": session["map_name"], "session_id": sid, "reason": reason,
                         "ok": ok, "robot_actions": actions, "slam_warning": warning})
            async with open_store(db, uuid.uuid4()) as store:
                await store.emit(event)
            log = logger.info if ok else logger.warning
            log("Services of session %s of %s restarted (%s): %s", sid, robot_name, reason,
                "; ".join(a["label"] for a in actions))
            await notify_robot(switch, db, robot_name, actions=actions)
            return actions
    except Exception:  # noqa: BLE001
        logger.exception("Services of robot %s not restarted (%s)", robot_name, reason)
        return None


async def restart_after_run_change(db: Any, switch: Any, robot_name: str,
                                   tries: int = RUN_CHANGE_RESTART_TRIES,
                                   delay_s: float = RUN_CHANGE_RESTART_RETRY_S,
                                   sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
                                   ) -> Optional[List[Dict[str, Any]]]:
    """restart_session_services() after mission-dispatch reported a new robot run (NOTIFY
    ms.RUN_CHANGED_CHANNEL): a robot that is still coming up may refuse, so a restart with a
    failed action is tried again `delay_s` later, `tries` times in all; only the last failure
    is reported. Never raises."""
    actions = None
    for attempt in range(max(1, tries)):
        last = attempt + 1 >= tries
        actions = await restart_session_services(db, switch, robot_name, RESTART_RUN_CHANGED,
                                                 report_failure=last)
        if actions is None or last or all(a["ok"] for a in actions):
            return actions
        await sleep(delay_s)
    return actions


async def slam_save_retry(db: Any, switch: Optional[Any], robot_name: str) -> Dict[str, Any]:
    """POST /api/v1/robots/{robot}/slam-save/retry: save the robot's SLAM map again after a
    failed save (the robot view's `slam_save.state` "failed"), in the background like a finish
    (MAP.SLAM_SAVE_DONE / _FAILED; `slam_save` is "saving" meanwhile). {robot_actions: [the
    save action]}. 404 unknown robot; 409 nothing to retry (no failed save, or a save runs).
    An offline robot is a failed action (the state stays failed), never an error."""
    robot = await _robot_or_404(db, robot_name)
    if switch is None:
        raise HTTPException(409, f"Robot '{robot_name}' has no failed SLAM save to retry")
    async with switch.lock(robot_name):
        if switch.slam_save_pending(robot_name):
            raise HTTPException(409, f"Robot '{robot_name}' is already saving a SLAM map")
        failed = switch.slam_save_failed(robot_name)
        if failed is None:
            raise HTTPException(409, f"Robot '{robot_name}' has no failed SLAM save to retry")
        session = {"session_id": failed.get("session_id"), "map_name": failed.get("map"),
                   "robot_name": robot_name, "purpose": ms.MAPPING, "services": [ms.SLAM]}
        if not robot.status.online:
            detail = f"robot '{robot_name}' is offline"
            return {"robot_actions": [slam_save_action(detail=detail)]}
        switch.schedule_slam_save(robot, session["map_name"], session["session_id"],
                                  on_result=slam_save_reporter(db, session, switch))
        return {"robot_actions": [slam_save_action()]}


async def slam_save_discard(db: Any, switch: Optional[Any], robot_name: str) -> Dict[str, Any]:
    """POST /api/v1/robots/{robot}/slam-save/discard: leave slam WITHOUT saving after a failed
    save: the robot's previous localization intent (odometry when unknown) is PUT back with the
    topomap off, the failed state is cleared, and the robot's open, unpaused mapping session
    gets its services started again. {robot_actions: [the discard (`stop` of "SLAM recording"),
    then the restart's]}. 404 unknown robot; 409 nothing to discard (no failed save, or a save
    runs). A failed switch is a failed action (the state stays failed), never an error."""
    robot = await _robot_or_404(db, robot_name)
    if switch is None:
        raise HTTPException(409, f"Robot '{robot_name}' has no failed SLAM save to discard")
    async with switch.lock(robot_name):
        if switch.slam_save_pending(robot_name):
            raise HTTPException(409, f"Robot '{robot_name}' is saving a SLAM map; wait for it")
        if switch.slam_save_failed(robot_name) is None:
            raise HTTPException(409, f"Robot '{robot_name}' has no failed SLAM save to discard")
        if not robot.status.online:
            detail = f"robot '{robot_name}' is offline"
            return {"robot_actions": [robot_action(SLAM_SERVICE, STOP, False,
                                                   f"SLAM recording not discarded: {detail}",
                                                   detail)]}
        action = await switch.discard_slam(robot)
        actions = [action]
        if action["ok"]:
            actions += await restart_session_services(
                db, switch, robot_name, RESTART_SLAM_DISCARDED, locked=True) or []
    return {"robot_actions": actions}


async def _robot_or_404(db: Any, robot_name: str) -> RobotObjectV1:
    try:
        async with open_store(db, uuid.uuid4()) as store:
            robot = await store.robot(robot_name)
    except _SCHEMA_ERRORS as exc:
        raise _undefined_table(exc) from exc
    if robot is None:
        raise HTTPException(404, f"Did not find \"robot\" with name \"{robot_name}\"")
    return robot


def _robot_lock(switch: Optional[Any], robot_name: str) -> Any:
    """The switch's per-robot lock (serialises starts and stops of one robot's services), or a
    no-op without a switch."""
    return switch.lock(robot_name) if switch is not None else contextlib.nullcontext()


async def start_session(db: Any, map_name: str, data: Any, publisher_id: uuid.UUID,
                        actor: Optional[str] = None, switch: Optional[Any] = None,
                        arango_node_count: Optional[Callable[[str], int]] = None,
                        reloc_jobs: Optional[Any] = None) -> Dict[str, Any]:
    """POST /api/v1/maps/{id}/sessions `{robot, purpose?, services?, placement?, replace?}`
    (rules: module docstring). With `replace` the robot's open session (any map, any purpose)
    is finished in the same transaction; a refused start keeps it. After the commit a mapping
    session's services are started on the robot's orchestrator (and the replaced session's are
    stopped); a failure there never fails or undoes the session, it is reported in
    `robot_actions` (docs/satinav-maps-redesign.md §14.16). The response:
    {map_id, map_state, changed, session, replaced_session} + notify_robot's keys.
    A MAPPING session that records a SLAM map is refused (409) while a relocalization job
    (`reloc_jobs`) runs for the robot: the job changes the localization mode that IS the
    recording (a topomap-only mapping session may start meanwhile). The ArangoDB node counts
    (`arango_node_count`, blocking) are read before the transaction, in a thread."""
    req = parse_body(StartSessionRequest, data)
    async with _robot_lock(switch, req.robot):
        if (reloc_jobs is not None and req.purpose == ms.MAPPING
                and reloc_jobs.active_for(req.robot) is not None
                and (ms.SLAM in req.services if req.services is not None
                     else await _slam_wanted(db, map_name))):
            raise HTTPException(409, f"Robot '{req.robot}' is relocalizing: wait for the "
                                     "relocalization job to finish or cancel it before "
                                     "recording a SLAM map")
        names = [map_name, await _open_session_map(db, req.robot)]
        counts = await _node_counts(arango_node_count, names)
        return await _start_session(db, map_name, req, publisher_id, actor, switch, counts)


async def _open_session_map(db: Any, robot_name: str) -> Optional[str]:
    """The map of the robot's open session (read without a lock, before the transaction, for
    the node count a `replace` finish needs), or None. Never raises."""
    try:
        async with open_store(db, uuid.uuid4()) as store:
            mine = await store.open_sessions_of_robot(robot_name)
        return mine[0]["map_name"] if mine else None
    except Exception:  # noqa: BLE001 - the transaction decides; only a count is missed
        return None


class _DryRun(Exception):
    """Ends a validation-only transaction: rolls it back."""


class _Opened:
    """What one start transaction decided: the new session, the robot, the replaced session,
    the map's state after and before."""
    session: Dict[str, Any]
    robot: RobotObjectV1
    replaced: Optional[Dict[str, Any]] = None
    map_state: str = ""
    prior_state: str = ""
    warnings: Optional[List[str]] = None


async def _open_tx(db: Any, map_name: str, req: Any, publisher_id: uuid.UUID,
                   actor: Optional[str], node_counts: Mapping[str, Optional[int]],
                   now: datetime.datetime, dry_run: bool = False) -> _Opened:
    """The session's transaction (validation, `replace`'s finish, the insert, the map state,
    the events). `dry_run`: everything is checked and computed, then rolled back. No
    orchestrator call (and no ArangoDB call: `node_counts` were read before) in here."""
    out = _Opened()
    try:
        async with open_store(db, publisher_id) as store:
            robot = await store.lock_robot(req.robot)
            if robot is None:
                # Lock the map anyway, for its 404 / 409 first (as before U1).
                await _lock_alive_map(store, map_name)
                raise HTTPException(404, f"Did not find \"robot\" with name \"{req.robot}\"")
            mine = await store.open_sessions_of_robot(req.robot)
            current = mine[0] if mine else None
            names = sorted({map_name, *([current["map_name"]] if current else [])})
            rows: Dict[str, Optional[MapRow]] = {}
            for name in names:  # lock in name order: two starts never deadlock
                rows[name] = await store.lock_map(name)
            row = rows[map_name]
            if row is None:
                raise HTTPException(404, f"Did not find \"map\" with name \"{map_name}\"")
            if row.lifecycle == DELETING:
                raise HTTPException(409, f"Map '{map_name}' is being deleted")
            carried = None
            if current is not None:
                if not req.replace:
                    raise HTTPException(409, f"Robot '{req.robot}' already has an open "
                                             f"{ms.purpose_of(current)} session on map "
                                             f"'{current['map_name']}' (pass replace: true to "
                                             "end it)")
                if not robot.status.online:
                    raise HTTPException(409, f"Robot '{req.robot}' is offline")
                await _finish_in(store, rows.get(current["map_name"]), current, now, actor,
                                 node_counts.get(current["map_name"]))
                out.replaced = current
                if current["map_name"] == map_name:
                    carried = current
            out.prior_state = row.state
            out.warnings = []
            out.session = await _start_in(store, row, robot, req.robot, now, actor, req, carried,
                                          node_counts.get(map_name), out.warnings)
            out.robot = robot
            out.map_state = row.state
            if dry_run:
                raise _DryRun()
    except _DryRun:
        pass
    except _SCHEMA_ERRORS as exc:
        raise _undefined_table(exc) from exc
    return out


async def _start_session(db: Any, map_name: str, req: Any, publisher_id: uuid.UUID,
                         actor: Optional[str], switch: Optional[Any],
                         node_counts: Mapping[str, Optional[int]]) -> Dict[str, Any]:
    now = _utcnow()
    opened = await _open_tx(db, map_name, req, publisher_id, actor, node_counts, now)
    session, replaced, robot = opened.session, opened.replaced, opened.robot
    slam_warnings: List[str] = []
    actions: List[Dict[str, Any]] = []
    if replaced is not None:
        # the old topomap stops first (no mode change while it runs), then the old SLAM map is
        # saved: awaited only when the new session records SLAM (one driver per robot), else
        # in the background (a new mapping session then starts when the save ended)
        actions += await stop_services(db, switch, robot, replaced, keep=False)
        wait = (ms.purpose_of(session) == ms.MAPPING
                and ms.SLAM in _services_of(session, orchestrator_only=False))
        old, old_action = await _save_slam(db, switch, replaced, robot, wait=wait)
        if old:
            slam_warnings.append(old)
        if old_action:
            actions.append(old_action)
    new_actions, new = await start_session_services(db, switch, session, robot)
    if new:
        slam_warnings.append(new)
    actions += new_actions
    out = {"map_id": map_name, "map_state": opened.map_state, "changed": True,
           "session": session_dict(session),
           "replaced_session": session_dict(replaced) if replaced else None}
    out.update(await notify_robot(switch, db, req.robot, with_service=True, actions=actions))
    if slam_warnings:
        out["slam_warning"] = "; ".join(slam_warnings)
    if opened.warnings:
        out["warnings"] = list(opened.warnings)
    return out


async def place_session(db: Any, map_name: str, session_id: str, data: Any,
                        publisher_id: uuid.UUID, actor: Optional[str] = None,
                        switch: Optional[Any] = None,
                        holder: Optional[Any] = None,
                        reloc_jobs: Optional[Any] = None) -> Dict[str, Any]:
    """POST /api/v1/maps/{id}/sessions/{sid}/place `{pose: {x, y, yaw}, robot_pose: {x, y,
    theta}}`: put the robot on a local map. Sets map_T_session, aligned (placed) and
    placement; MAP.SESSION_PLACED (from then on graph-builder keeps the session's nodes; the
    services are not touched). 404 unknown map/session; 409: finished, geo map (placed
    by its datum, except `source: "datum"`), the robot offline. NOT refused, only `warnings`
    (decision 2026-10-08): the robot drives or moved since its pose was shown, and re-placing
    an already placed MAPPING session (it splits its nodes: those before keep the old
    placement). An operate session can be re-placed at any time (a correction).

    `source: "reloc"` (body `{source: "reloc"}`, no poses): the robot relocalises on a stored
    map its orchestrator holds (`holder`: packages/api/orchestrator_maps.py). Placed with
    ms.reloc_placement() (identity, D0 assumption); the robot-still check is skipped.
    A placed session may be relocalized again (as it may be placed again by hand). Refused 409
    when the robot is offline or reports its position as not initialized, or the orchestrator does not (or cannot be asked to) hold the map. A low
    localization score does not refuse: it shows as `localization_warning` on the robot view. The
    manual placement path is unchanged.

    When the holder says relocalization can be STARTED for this robot and map (`reloc.can_start`,
    OrchestratorMaps.reloc_capability) and `reloc_jobs` (packages/api/reloc_job.py) is given,
    `source: "reloc"` does not place at once: it starts a job and returns `{..., "session":
    <unplaced>, "reloc_job": {id, state, step, started_at, deadline, mode}}` (the route answers
    202; poll GET .../reloc-job, DELETE cancels). `{"source": "reloc", "reloc": {"init_pose":
    {x, y, yaw}}}` (cloud map frame) relocalizes assisted by that pose; it needs a job (409 when
    `can_start` is false). Without `can_start` a bare `source: "reloc"` is the check-only
    placement described above, unchanged. When the orchestrator could start it but the robot
    has an open mapping session that records a SLAM map, a job runs, or a SLAM save is pending
    or failed, the answer is 409 (the same reasons the reads give as `can_start_reason`; a
    driving robot and a topomap-only mapping session do not refuse it)."""
    req = parse_body(PlaceRequest, data)
    try:
        uuid.UUID(str(session_id))
    except ValueError:
        raise HTTPException(404, f"Did not find session \"{session_id}\"") from None
    now = _utcnow()
    reloc = req.source == ms.SOURCE_RELOC
    init_pose = req.reloc.init_pose if req.reloc is not None else None
    held: Optional[bool] = None
    capable = False
    if reloc:
        # Cheap refusals first (offline, already placed, ...): the orchestrator read is slow,
        # and it is never made inside a DB transaction. The "position not initialized" refusal
        # waits until we know whether a job (which fixes exactly that) can run.
        has_capability = getattr(holder, "reloc_capability", None) is not None
        try:
            row, session, robot = await _reloc_inputs(db, map_name, session_id)
        except _SCHEMA_ERRORS as exc:
            raise _undefined_table(exc) from exc
        _placement_refusals(map_name, session_id, session, _alive(row, map_name), robot, True,
                            check_initialized=not has_capability and init_pose is None)
        if holder is not None:
            try:
                held = await holder.held(robot, map_name, fresh=True)
            except Exception:  # noqa: BLE001
                logger.exception("Stored map of session %s not readable", session_id)
        why = None
        if has_capability:
            capable, why = await _can_start(holder, robot, map_name, held, fresh=True)
        if init_pose is not None and not (capable and reloc_jobs is not None):
            raise HTTPException(
                409, f"Relocalization with an initial pose cannot be started for robot "
                     f"'{robot.name}': {why or 'not available'}")
        if capable and reloc_jobs is not None:
            blocked = (await mapping_blocker(db, robot)
                       or await _start_blockers(db, robot, switch, reloc_jobs))
            if blocked is not None:  # the reads said so too (can_start false + this reason)
                raise HTTPException(409, f"Relocalization cannot be started for robot "
                                         f"'{robot.name}': {blocked}")
            job = await reloc_jobs.start(
                db, switch, map_name, str(session_id), robot.name,
                init_pose.dict() if init_pose is not None else None, actor, publisher_id)
            return {"map_id": map_name, "map_state": row.state, "changed": False,
                    "session": session_dict(session), "reloc_job": job.view()}
        if has_capability and robot.status.position_initialized is False and init_pose is None:
            raise HTTPException(409, f"Robot '{robot.name}' is not relocalised: it reports its "
                                     "position as not initialized")
    place_warnings: List[str] = []
    try:
        async with open_store(db, publisher_id) as store:
            row = await _lock_alive_map(store, map_name)
            session = await store.lock_session(session_id)
            robot = await store.robot(session["robot_name"]) if session is not None else None
            _placement_refusals(map_name, session_id, session, row, robot, reloc, req.source)
            by_datum = req.source == ms.SOURCE_DATUM
            from_datum = None
            if by_datum:
                from_datum = datum_placement(row.spec.get("geo"), robot)
                if from_datum is None:
                    raise HTTPException(
                        409, f"Robot '{robot.name}' has no GNSS datum that places it on map "
                             f"'{map_name}' (none, or in another UTM zone than the map's)")
                req.robot_pose = RobotPose(**from_datum["robot_pose"])
                req.pose = MapPose(**from_datum["pose"])
                transform = from_datum["map_T_session"]
            elif reloc:
                if held is not True:
                    raise HTTPException(
                        409, f"Robot '{robot.name}' does not hold a stored map for map "
                             f"'{map_name}' (or its orchestrator could not be asked); place it "
                             "manually")
                at = robot.status.pose
                req.robot_pose = RobotPose(x=at.x, y=at.y, theta=at.theta)
                pose, transform = ms.reloc_placement(req.robot_pose.dict())
                req.pose = MapPose(**pose)
            else:
                place_warnings += await placement_warnings(store, robot, req.robot_pose.dict())
            if (not reloc and not by_datum and ms.purpose_of(session) == ms.MAPPING
                    and ms.is_placed(session)):
                place_warnings.append(
                    f"Mapping session {session_id} was already placed: re-placing it splits "
                    "its nodes (those before keep the old placement)")
            old = session.get("map_t_session") if ms.is_placed(session) else None
            placement = _placement_record(req, req.source or ms.SOURCE_USER, actor, now)
            if not reloc and not by_datum:
                transform = ms.placement_transform(placement["pose"], placement["robot_pose"])
            fields: Dict[str, Any] = {"map_t_session": transform, "aligned": True,
                                      "placement": placement}
            if from_datum is not None:
                # The session's datum is what the dispatcher compares the next datum with.
                placement["datum"] = from_datum["datum"]
                fields["datum"] = from_datum["datum"]
            session.update(fields)
            await store.update_session(session_id, **fields)
            await store.emit(_placed_event(session, now, old))
            map_state = row.state
    except _SCHEMA_ERRORS as exc:
        raise _undefined_table(exc) from exc
    out = {"map_id": map_name, "map_state": map_state, "changed": True,
           "session": session_dict(session)}
    if place_warnings:
        out["warnings"] = place_warnings
    out.update(await notify_robot(switch, db, session["robot_name"]))
    return out


async def unplace_session(db: Any, map_name: str, session_id: str, publisher_id: uuid.UUID,
                          actor: Optional[str] = None,
                          reloc_jobs: Optional[Any] = None) -> Dict[str, Any]:
    """POST /api/v1/maps/{id}/sessions/{sid}/unplace: mark a placed session on a LOCAL map as
    not placed (aligned = false, placement.unplaced_reason = "manual"; the old
    map_T_session and the robot's last pose are kept, so the "last position" suggestion works).
    MAP.SESSION_UNPLACED.

    PURPOSE: a developer / test hook and a "my placement looks wrong, redo it" action. The
    system unplaces by itself when the robot's run changes (the dispatcher); this exists so the
    placement and relocalization flows can be re-run without restarting robot services. It is
    not a normal part of using a map.

    404 unknown map/session; 409: finished, a geo map (the datum re-places it at once, so it is
    pointless), or a relocalization job runs for the session. A MAPPING session is unplaced
    with a `warnings` entry (graph-builder drops its new nodes until it is placed again).
    Unplacing an unplaced session changes nothing (`changed: false`)."""
    try:
        uuid.UUID(str(session_id))
    except ValueError:
        raise HTTPException(404, f"Did not find session \"{session_id}\"") from None
    now = _utcnow()
    unplace_warnings: List[str] = []
    try:
        async with open_store(db, publisher_id) as store:
            row = await _lock_alive_map(store, map_name)
            session = await store.lock_session(session_id)
            if session is None or session["map_name"] != map_name:
                raise HTTPException(404, f"Did not find session \"{session_id}\" on map "
                                         f"\"{map_name}\"")
            if session["ended_at"] is not None:
                raise HTTPException(409, f"Session {session_id} is finished")
            if row.type == "geo":
                raise HTTPException(409, f"Map '{map_name}' is a geo map: its sessions follow "
                                         "the robot's datum and are placed again at once")
            if (reloc_jobs is not None
                    and reloc_jobs.active_for(session["robot_name"]) is not None):
                raise HTTPException(409, f"A relocalization job is running for session "
                                         f"{session_id}; cancel it first")
            if not ms.is_placed(session):
                return _unchanged(map_name, row.state, session)
            old = session.get("map_t_session")
            if ms.purpose_of(session) == ms.MAPPING:
                unplace_warnings.append(
                    f"Session {session_id} is a mapping session: while it is unplaced "
                    "graph-builder drops its new nodes; place it again soon")
            placement = ms.unplaced_placement(session.get("placement"), ms.UNPLACED_MANUAL,
                                              now)
            placement["actor"] = actor
            session.update({"aligned": False, "placement": placement})
            await store.update_session(session_id, aligned=False, placement=placement)
            await store.emit(Event(
                EventCode.MAP_SESSION_UNPLACED, now, robot_name=session["robot_name"],
                source=Source.API,
                discriminator=f"session:{session_id}:unplaced:{now.isoformat()}",
                payload={"map_name": map_name, "session_id": str(session_id),
                         "purpose": ms.purpose_of(session), "reason": ms.UNPLACED_MANUAL,
                         "actor": actor,
                         "old_map_T_session": dict(old) if old else None}))
            map_state = row.state
    except _SCHEMA_ERRORS as exc:
        raise _undefined_table(exc) from exc
    out = {"map_id": map_name, "map_state": map_state, "changed": True,
           "session": session_dict(session)}
    if unplace_warnings:
        out["warnings"] = unplace_warnings
    return out


async def session_action(db: Any, map_name: str, session_id: str, action: str,
                         publisher_id: uuid.UUID, actor: Optional[str] = None,
                         switch: Optional[Any] = None,
                         arango_node_count: Optional[Callable[[str], int]] = None,
                         reloc_jobs: Optional[Any] = None) -> Dict[str, Any]:
    """pause / resume / finish (see the module docstring). After the commit, resume starts the
    session's services on the robot's orchestrator (SLAM first when `slam` is in them) and
    pause / finish stop them; a repeat retries. What failed never fails or undoes the action:
    `robot_actions` reports it. Resume has the guards of a start: 409 while a relocalization
    job runs for the robot and the session records a SLAM map; while the robot's previous SLAM
    map is still being saved the services start when the save ended. Finish also saves the
    SLAM map of a mapping session that recorded one in the background, and makes the map
    `ready` or `draft` (_finish_in; the ArangoDB count is read before the transaction)."""
    if action not in SESSION_ACTIONS:
        raise HTTPException(404, f"Unknown session action {action!r}")
    try:
        uuid.UUID(str(session_id))
    except ValueError:
        raise HTTPException(404, f"Did not find mapping session \"{session_id}\"") from None
    robot_name = None
    found = None
    if switch is not None or action == "resume":  # the robot's lock is taken before the tx
        async with open_store(db, uuid.uuid4()) as store:
            found = await store.lock_session(session_id)
        robot_name = found["robot_name"] if found is not None else None
    counts = (await _node_counts(arango_node_count, [map_name]) if action == "finish" else {})
    async with _robot_lock(switch, robot_name):
        if (action == "resume" and found is not None and reloc_jobs is not None
                and reloc_jobs.active_for(found["robot_name"]) is not None
                and ms.SLAM in _services_of(found, orchestrator_only=False)):
            raise HTTPException(409, f"Robot '{found['robot_name']}' is relocalizing: wait for "
                                     "the relocalization job to finish or cancel it before "
                                     "resuming a session that records a SLAM map")
        out, robot, session = await _session_action(db, map_name, session_id, action,
                                                    publisher_id, actor, switch,
                                                    counts.get(map_name))
        slam_warning = None
        if action == "resume":
            actions, slam_warning = await start_session_services(db, switch, session, robot)
        else:
            actions = await stop_services(db, switch, robot, session)
        if action == "finish" and out["changed"]:
            slam_warning, slam_action = await _save_slam(db, switch, session, robot, wait=False)
            if slam_action:
                actions.append(slam_action)
        out.update(await notify_robot(switch, db, out["session"]["robot_name"], actions=actions))
        if slam_warning:
            out["slam_warning"] = slam_warning
    return out


async def _session_action(db: Any, map_name: str, session_id: str, action: str,
                          publisher_id: uuid.UUID, actor: Optional[str],
                          switch: Optional[Any] = None, node_count: Optional[int] = None
                          ) -> Tuple[Dict[str, Any], Optional[RobotObjectV1], Dict[str, Any]]:
    """(response, the session's robot, the session row)."""
    now = _utcnow()
    robot: Optional[RobotObjectV1] = None
    try:
        async with open_store(db, publisher_id) as store:
            row = await _lock_alive_map(store, map_name)
            session = await store.lock_session(session_id)
            if session is None or session["map_name"] != map_name:
                raise HTTPException(404, f"Did not find mapping session \"{session_id}\" on "
                                         f"map \"{map_name}\"")
            if switch is not None:
                robot = await store.robot(session["robot_name"])
            ended = session["ended_at"] is not None
            paused = session["paused_at"] is not None
            if action == "finish":
                if ended:
                    return _unchanged(map_name, row.state, session), robot, session
                map_state = await _finish_in(store, row, session, now, actor, node_count)
                return ({"map_id": map_name, "map_state": map_state, "changed": True,
                         "session": session_dict(session)}, robot, session)
            if ended:
                raise HTTPException(409, f"Mapping session {session_id} is finished")
            if ms.purpose_of(session) != ms.MAPPING:
                raise HTTPException(409, f"Session {session_id} is an operate session: "
                                         "only mapping sessions pause (finish it to stop "
                                         "using the map)")
            if (action == "pause") == paused:  # a repeat (resume: the caller starts the services)
                return _unchanged(map_name, row.state, session), robot, session
            stamp = now if action == "pause" else None
            session["paused_at"] = stamp
            await store.update_session(session_id, paused_at=stamp)
            status = {"state": PAUSED if action == "pause" else MAPPING}
            code = (EventCode.MAP_SESSION_PAUSED if action == "pause"
                    else EventCode.MAP_SESSION_RESUMED)
            await store.update_map(row, status=status)
            map_state = status.get("state", row.state)
            await store.emit(_session_event(code, session, map_state, actor, now))
    except _SCHEMA_ERRORS as exc:
        raise _undefined_table(exc) from exc
    out = {"map_id": map_name, "map_state": map_state, "changed": True,
           "session": session_dict(session)}
    return out, robot, session


def _unchanged(map_name: str, map_state: str, session: Mapping[str, Any]) -> Dict[str, Any]:
    return {"map_id": map_name, "map_state": map_state, "changed": False,
            "session": session_dict(session)}
