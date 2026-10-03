"""Typed maps, map lifecycle and sessions (docs/satinav-maps-redesign.md §2-§4, §7, §14).

    POST   /api/v1/maps                                  create_map()     -> draft
    GET    /api/v1/maps?type=&state=&include_archived=   filter_maps()
    GET    /api/v1/maps/{id}                             session_summary() (added to the old body)
    PATCH  /api/v1/maps/{id}                             patch_map()      description only
    POST   /api/v1/maps/{id}/sessions                    start_session()
    GET    /api/v1/maps/{id}/sessions?limit=&before=     session_history()
    POST   /api/v1/maps/{id}/sessions/{sid}/pause|resume|finish   session_action()
    POST   /api/v1/maps/{id}/sessions/{sid}/place        place_session()
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
- placing (start with `placement`, or place_session) is refused (409) while the robot drives
  (an active order, or a velocity in its last state) and when its pose differs from the pose
  the user saw by more than sensor noise (0.02 m / 0.5 deg, decision Q-U7).

A session that is not placed keeps nothing (graph-builder rejects its nodes with
`session_unplaced`; its service still runs), gets no route orders on that map and no planned paths.

Map lifecycle: draft -> mapping <-> paused -> ready (finish) -> archived -> ready|draft
(restore). Only MAPPING sessions move it; operate sessions leave the map state alone.
Archive and delete are refused while ANY session is open (the message names the robots,
decision Q-U2). Repeating pause/resume/finish/archive/restore on a map or session already in
that state is a no-op (`changed: false`, no event). pause/resume are for mapping sessions only.

The mapping switch (docs/satinav-maps-redesign.md §15, packages/api/mapping_switch.py): the
mapping services of a session (`services`, today `topo`) run on the robot's orchestrator.
Opening a mapping session STARTS them and resuming a paused one starts them again. The
orchestrator call (up to ORCHESTRATOR_START_TIMEOUT_S) is NEVER made inside a DB transaction (it
would hold row locks that mission-dispatch and other writers wait on): the change is committed
first, then the services are started, and a failed start (robot or orchestrator unreachable, no
such service, an error) is compensated in a new transaction and the call fails with the same
502/504/409 as before: a new session is closed again (MAP.SESSION_FINISHED pairs its
MAP.SESSION_STARTED; a draft map goes back to draft), a resumed one is paused again
(MAP.SESSION_PAUSED). `replace` starts the services FIRST (a validation-only transaction that is
rolled back, then the start, then the real transaction), because the replaced session cannot be
cleanly reopened: a failed start leaves everything, events included, as it was.
Pause and finish STOP them after the commit, best effort: an offline robot's session is closed
anyway and the response says `robot_notified: false` with a `mapping_warning`. The session
still gates the nodes (graph-builder drops a node with no open, unpaused, placed session).

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
from typing import Any, AsyncIterator, Callable, Dict, List, Mapping, Optional, Tuple

import psycopg
import pydantic
from fastapi import HTTPException

from cloud_common.objects.map import (
    MAP_STATES, MAP_TYPES, MapObjectV1, MapSpecV1, MapStatusV1, effective_state,
    effective_type, has_real_datum,
)
from cloud_common.objects.object import ObjectLifecycleV1
from cloud_common.objects.robot import RobotObjectV1
from packages.api.mapping_switch import Snapshot, StopResult
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


class PatchMapRequest(pydantic.BaseModel):
    description: Optional[str] = None

    class Config:
        extra = pydantic.Extra.forbid


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
        if not value:
            raise ValueError("at least one mapping service")
        return list(dict.fromkeys(value))

    def session_services(self) -> Optional[List[str]]:
        if self.purpose != ms.MAPPING:
            return None
        return list(self.services or ms.DEFAULT_SERVICES)


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
                                          "Postgres, ArangoDB and MinIO)"}])
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
        "services": (list(row.get("services") or ms.DEFAULT_SERVICES)
                     if purpose == ms.MAPPING else None),
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
        nodes = arango_node_count(req.name)
        if nodes:
            raise HTTPException(409, f"ArangoDB already has {nodes} nodes for map "
                                     f"'{req.name}' (no Postgres row); choose another name")
    spec = json.loads(MapSpecV1(description=req.description, type=req.type).json())
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


async def patch_map(db: Any, name: str, data: Any, publisher_id: uuid.UUID) -> Dict[str, Any]:
    req = parse_body(PatchMapRequest, data)
    changes = req.dict(exclude_unset=True)
    async with open_store(db, publisher_id) as store:
        row = await _lock_alive_map(store, name)
        if changes:
            await store.update_map(row, spec=changes)
            row.spec.update(changes)
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
                        source: Optional[str] = None) -> None:
    """The 404/409 refusals of POST .../place that need no orchestrator answer, in the order
    they apply. Run once before the (slow) orchestrator read of a `reloc` placement and again
    inside the transaction. `source` "datum": the geo-map placement from the robot's datum."""
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
    if ms.purpose_of(session) == ms.MAPPING and ms.is_placed(session):
        raise HTTPException(409, f"Mapping session {session_id} is already placed; "
                                 "re-placing it would split its nodes (finish it and "
                                 "start a new session to continue from elsewhere)")
    if robot is None:
        raise HTTPException(404, f"Did not find \"robot\" with name "
                                 f"\"{session['robot_name']}\"")
    if not robot.status.online:
        raise HTTPException(409, f"Robot '{robot.name}' is offline")
    if reloc:
        if ms.is_placed(session):
            raise HTTPException(409, f"Session {session_id} is already placed")
        if robot.status.position_initialized is False:
            raise HTTPException(409, f"Robot '{robot.name}' is not relocalised: it reports its "
                                     "position as not initialized")


async def reloc_status(db: Any, holder: Optional[Any], map_name: str, session_id: str
                       ) -> Optional[Dict[str, Any]]:
    """`reloc` of the placement-suggestions answer: {available, known, source} for an unplaced,
    open session on a local map, else None. `available`: the robot's orchestrator holds a
    stored map for this cloud map, so no manual initial position is needed (place it with
    `source: "reloc"`); `known` false: the orchestrator could not be asked (then `available` is
    false: manual placement). A GET: no row locks and the cached held read (place_session asks
    afresh). Never raises."""
    try:
        row, session, robot = await _reloc_inputs(db, map_name, session_id)
        if (row is None or row.type != "local" or session is None
                or session["ended_at"] is not None or ms.is_placed(session)):
            return None
        held = None
        if holder is not None and robot is not None:
            held = await holder.held(robot, map_name)
    except Exception:  # noqa: BLE001
        logger.exception("Reloc status of session %s not readable", session_id)
        return None
    return {"available": held is True, "known": held is not None, "source": "orchestrator"}


async def map_reloc(db: Any, holder: Optional[Any], map_name: str, robot_name: str
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
                return {"available": False, "known": True, "source": "orchestrator"}
            robot = await store.robot(robot_name)
    except _SCHEMA_ERRORS as exc:
        raise _undefined_table(exc) from exc
    held: Optional[bool] = None
    if robot is not None and holder is not None:
        try:
            held = await holder.held(robot, map_name)
        except Exception:  # noqa: BLE001
            logger.exception("Stored map of robot %s not readable", robot_name)
    return {"available": held is True, "known": held is not None, "source": "orchestrator"}


async def placement_suggestions(db: Any, map_name: str, session_id: str,
                                holder: Optional[Any] = None) -> Dict[str, Any]:
    """The placement suggestions (_placement_suggestions) plus `reloc` (reloc_status): null, or
    {available, known, source}; with `available` the client skips the manual placement."""
    out = await _placement_suggestions(db, map_name, session_id)
    out["reloc"] = await reloc_status(db, holder, map_name, session_id)
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
                              actor: Optional[str]) -> Dict[str, Any]:
    now = _utcnow()
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
            new_state = READY if await store.sessions(name) else DRAFT
            code = EventCode.MAP_RESTORED
        await store.update_map(row, status={"state": new_state})
        await store.emit(_map_event(code, name, row.type, new_state, actor, now))
    return {"map_id": name, "state": new_state, "changed": True}


async def archive_map(db: Any, name: str, publisher_id: uuid.UUID,
                      actor: Optional[str] = None) -> Dict[str, Any]:
    return await _archive_or_restore(db, name, True, publisher_id, actor)


async def restore_map(db: Any, name: str, publisher_id: uuid.UUID,
                      actor: Optional[str] = None) -> Dict[str, Any]:
    return await _archive_or_restore(db, name, False, publisher_id, actor)


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
            "approx_location": None, "former_datum": None}


def _conversion_notes(new_type: str, sessions: List[Mapping[str, Any]],
                      datums: Mapping[str, Optional[Dict[str, Any]]]) -> List[str]:
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
    if new_type == "geo":
        notes.append("Robots without a GNSS datum cannot start a session on this map now "
                     "(a geo map is placed by the robot's datum)")
    else:
        notes.append("The map is no longer georeferenced: no street tiles, no GPS goals; "
                     "robots are placed by hand or by relocalization")
    return notes


async def convert_map_type(db: Any, name: str, data: Any, publisher_id: uuid.UUID,
                           actor: Optional[str] = None) -> Dict[str, Any]:
    """POST /api/v1/maps/{id}/type: convert geo <-> local (ConvertTypeRequest). Nothing moves:
    the map-frame coordinates of nodes, edges, reconstruction results, sessions and missions
    stay valid. One transaction (map row locked, so it serialises with session starts):

    - 404 unknown map; 409 deleting, already of that type, or an open (or paused) MAPPING
      session (its nodes would land in a frame whose meaning changes under them); 422 body;
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
            old_geo = row.spec.get("geo")
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
            "warnings": _conversion_notes(req.type, open_sessions, datums)}


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

def _map_has_nodes(row: MapRow, previous: List[Dict[str, Any]],
                   arango_node_count: Optional[Callable[[str], int]]) -> bool:
    """Whether a local map already holds data (then a new mapping session must be placed,
    decision Q-U4): ArangoDB's count when available, else the sessions' and the row's counts."""
    if arango_node_count is not None:
        try:
            return int(arango_node_count(row.name) or 0) > 0
        except Exception:  # noqa: BLE001 - fall back to the stored counts
            logger.warning("ArangoDB node count of %s failed; using the stored counts", row.name)
    return (any(int(s.get("node_count") or 0) > 0 for s in previous)
            or int(row.status.get("node_count") or 0) > 0)


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


async def check_robot_still(store: Any, robot: RobotObjectV1,
                            robot_pose: Mapping[str, Any]) -> None:
    """409 unless the robot stands still where the user saw it (decision Q-U7): no active
    order, no velocity, and its pose now within sensor noise of `robot_pose`."""
    state = robot.status.state.value if robot.status.state is not None else None
    mission_open = (await store.robot_mission_open(robot.name)
                    if state in ("ON_TASK", "MAP_DEPLOYMENT") else None)
    reason = ms.driving_reason(state, await store.robot_state_msg(robot.name), mission_open)
    if reason is not None:
        raise HTTPException(409, f"Robot '{robot.name}' is driving ({reason}); stop it and "
                                 "place it again")
    pose = robot.status.pose
    moved = ms.pose_moved(robot_pose, {"x": pose.x, "y": pose.y, "theta": pose.theta})
    if moved is not None:
        raise HTTPException(409, f"Robot '{robot.name}' moved: {moved}; keep it still and "
                                 "place it again")


def _placement_record(req: PlaceRequest, source: str, actor: Optional[str],
                      now: datetime.datetime) -> Dict[str, Any]:
    return {"pose": req.pose.dict(), "robot_pose": req.robot_pose.dict(), "source": source,
            "actor": actor, "at": now.isoformat()}


async def _start_in(store: Any, row: MapRow, robot: Optional[RobotObjectV1], robot_name: str,
                    now: datetime.datetime, actor: Optional[str],
                    req: Optional[StartSessionRequest] = None,
                    carried: Optional[Mapping[str, Any]] = None,
                    arango_node_count: Optional[Callable[[str], int]] = None
                    ) -> Dict[str, Any]:
    """Start a session on the locked map `row` inside the caller's transaction (the rules of
    the module docstring); the new session row. The robot's previous open session must already
    be finished (`replace`); `carried` is that session when it was on this map."""
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
        await check_robot_still(store, robot, req.placement.robot_pose.dict())
        placement = _placement_record(req.placement, ms.SOURCE_USER, actor, now)
    carried = carried if carried is not None and ms.is_placed(carried) else None
    if carried is None and placement is None and row.type == "local":
        # §14.13: the robot's last session on this map, finished placed in the run it is
        # still in, lends its placement (the robot has not restarted since).
        carried = ms.reusable_session(previous, robot_name,
                                      await store.robot_run_epoch(robot_name))
    has_nodes = (row.type == "local" and placement is None and carried is None
                 and _map_has_nodes(row, previous, arango_node_count))
    plan = plan_session(row, robot, previous, purpose, placement, carried, has_nodes)
    session = {"session_id": str(uuid.uuid4()), "map_name": map_name,
               "robot_name": robot_name, "kind": "live", "purpose": purpose,
               "services": req.session_services(), "placement": plan["placement"],
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
                     restore_state: Optional[str] = None) -> Optional[str]:
    """Finish the open `session` inside the caller's transaction; the map's state afterwards.
    A mapping session makes its map `ready` unless another mapping session is still open; an
    operate session leaves the map as it is. `row`: the locked map row, None when it is gone.
    `restore_state`: the state the map returns to instead of `ready` (a session whose start
    failed, on a draft map)."""
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
        others = [s for s in await store.sessions(row.name)
                  if s["ended_at"] is None and str(s["session_id"]) != session_id
                  and ms.purpose_of(s) == ms.MAPPING]
        status = ({"state": restore_state or READY, "open_session_id": None} if not others else
                  {"open_session_id": str(others[0]["session_id"])})
        await store.update_map(row, status=status)
        row.status.update(status)
    map_state = row.state
    await store.emit(_session_event(EventCode.MAP_SESSION_FINISHED, session, map_state, actor,
                                    now))
    return map_state


def _services_of(session: Mapping[str, Any]) -> List[str]:
    """The mapping services a session runs (none for an operate session)."""
    if ms.purpose_of(session) != ms.MAPPING:
        return []
    return list(session.get("services") or ms.DEFAULT_SERVICES)


async def start_services(switch: Optional[Any], session: Mapping[str, Any],
                         robot: Optional[RobotObjectV1]) -> Dict[str, str]:
    """Start the session's mapping services on the robot's orchestrator. Never call it inside
    a DB transaction (the call can take ORCHESTRATOR_START_TIMEOUT_S): a failure raises
    HTTPException and the caller compensates. {} for an operate session or without a switch."""
    services = _services_of(session)
    if switch is None or not services:
        return {}
    if robot is None:
        raise HTTPException(404, f"Did not find \"robot\" with name \"{session['robot_name']}\"")
    return await switch.start(robot, services)


async def stop_services(db: Any, switch: Optional[Any], robot: Optional[RobotObjectV1],
                        session: Mapping[str, Any]) -> Optional[StopResult]:
    """After a session change committed: stop the mapping services of `session` (paused or
    finished) unless the robot's open mapping session still runs them. Best effort, never
    raises; None when there was nothing to stop."""
    services = _services_of(session)
    if switch is None or not services:
        return None
    try:
        async with open_store(db, uuid.uuid4()) as store:
            mine = await store.open_sessions_of_robot(session["robot_name"])
        keep = {s for o in mine if ms.purpose_of(o) == ms.MAPPING and o["paused_at"] is None
                for s in _services_of(o)}
        services = [s for s in services if s not in keep]
        if not services:
            return None
        if robot is None:
            return StopResult(
                {s: "failed" for s in services},
                f"robot '{session['robot_name']}' not found: its mapping service(s) "
                f"{', '.join(services)} were not stopped")
        return await switch.stop(robot, services)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Mapping services of robot %s not stopped", session["robot_name"])
        return StopResult({s: "failed" for s in services},
                          f"mapping service(s) on robot '{session['robot_name']}' could not be "
                          f"stopped: {exc}")


async def notify_robot(switch: Optional[Any], db: Any, robot_name: str,
                       with_service: bool = False, started: Optional[Mapping[str, str]] = None,
                       stopped: Optional[StopResult] = None) -> Dict[str, Any]:
    """After a session change has COMMITTED (and its services were started / stopped): push the
    robot's `session` to /ws/robot/{robot} (switch.on_session) and describe the switch. Never
    raises. The response keys (additive): `robot_notified` (false when a mapping service could
    not be stopped), `mapping_warning` (only then: what), `mapping_switch` (only when a service
    was started or stopped: {service: started | already_running | stopped | already_stopped |
    failed}), `mapping_state`, and with `with_service` `mapping_service` ("running" |
    "not_running") and `mapping_services` ({service: running | not_running | not_available})."""
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
    if stopped is not None and not stopped.ok:
        # The orchestrator did not answer the stop: do not wait for it again for the state.
        snap = Snapshot(reachable=False, error=stopped.warning)
    elif robot is not None:
        try:
            snap = await switch.snapshot(robot, fresh=True)
        except Exception:  # noqa: BLE001
            logger.exception("Mapping state of robot %s not readable", robot_name)
            snap = Snapshot(reachable=None)
    else:
        snap = Snapshot(reachable=None)
    session_row = dict(current, state=ms.session_state(current)) if current else None
    state = snap.state(session_row)
    out: Dict[str, Any] = {"robot_notified": stopped is None or stopped.ok,
                           "mapping_state": state}
    if stopped is not None and stopped.warning:
        out["mapping_warning"] = stopped.warning
    done = dict(started or {})
    done.update(stopped.services if stopped is not None else {})
    if done:
        out["mapping_switch"] = done
    if with_service:
        out["mapping_service"] = snap.mapping_service()
        out["mapping_services"] = snap.mapping_services()
    on_state = getattr(switch, "on_state", None)
    if on_state is not None and done:
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


def _robot_lock(switch: Optional[Any], robot_name: str) -> Any:
    """The switch's per-robot lock (serialises starts and stops of one robot's services), or a
    no-op without a switch."""
    return switch.lock(robot_name) if switch is not None else contextlib.nullcontext()


async def start_session(db: Any, map_name: str, data: Any, publisher_id: uuid.UUID,
                        actor: Optional[str] = None, switch: Optional[Any] = None,
                        arango_node_count: Optional[Callable[[str], int]] = None
                        ) -> Dict[str, Any]:
    """POST /api/v1/maps/{id}/sessions `{robot, purpose?, services?, placement?, replace?}`
    (rules: module docstring). With `replace` the robot's open session (any map, any purpose)
    is finished in the same transaction; a refused start keeps it. A mapping session's
    services are started on the robot's orchestrator OUTSIDE any transaction: after the commit
    (a failed start: 502/504/409, the new session is closed again, the map's state restored) or,
    with `replace`, before it (a failed start: 502/504/409, nothing changed, the replaced session
    stays open); afterwards the replaced session's services that the new one does not run are
    stopped (best effort). The response:
    {map_id, map_state, changed, session, replaced_session} + notify_robot's keys."""
    req = parse_body(StartSessionRequest, data)
    async with _robot_lock(switch, req.robot):
        return await _start_session(db, map_name, req, publisher_id, actor, switch,
                                    arango_node_count)


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


async def _open_tx(db: Any, map_name: str, req: Any, publisher_id: uuid.UUID,
                   actor: Optional[str], arango_node_count: Optional[Callable[[str], int]],
                   now: datetime.datetime, dry_run: bool = False) -> _Opened:
    """The session's transaction (validation, `replace`'s finish, the insert, the map state,
    the events). `dry_run`: everything is checked and computed, then rolled back. No
    orchestrator call in here."""
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
                await _finish_in(store, rows.get(current["map_name"]), current, now, actor)
                out.replaced = current
                if current["map_name"] == map_name:
                    carried = current
            out.prior_state = row.state
            out.session = await _start_in(store, row, robot, req.robot, now, actor, req, carried,
                                          arango_node_count)
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
                         arango_node_count: Optional[Callable[[str], int]]) -> Dict[str, Any]:
    now = _utcnow()
    args = (db, map_name, req, publisher_id, actor, arango_node_count, now)
    started: Dict[str, str] = {}
    opened: Optional[_Opened] = None
    if switch is not None and req.replace and req.purpose == ms.MAPPING:
        # Start first: the replaced session cannot be cleanly reopened, so nothing is committed
        # until the services run. The dry run keeps the validation errors (409 offline, ...)
        # ahead of the orchestrator's; a failed start changes nothing, events included.
        preview = await _open_tx(*args, dry_run=True)
        if preview.replaced is not None:
            started = await start_services(switch, preview.session, preview.robot)
            try:
                opened = await _open_tx(*args)
            except BaseException:
                await asyncio.shield(_unstart(switch, preview.robot, started))
                raise
    if opened is None:
        # Commit first: a failed start closes the new session again (compensation).
        opened = await _open_tx(*args)
        try:
            started = await start_services(switch, opened.session, opened.robot)
        except BaseException:
            await asyncio.shield(_close_unstarted(db, switch, opened, publisher_id, actor))
            raise
    session, replaced, robot = opened.session, opened.replaced, opened.robot
    stopped: Optional[StopResult] = None
    if replaced is not None:
        stopped = await stop_services(db, switch, robot, replaced)
    out = {"map_id": map_name, "map_state": opened.map_state, "changed": True,
           "session": session_dict(session),
           "replaced_session": session_dict(replaced) if replaced else None}
    out.update(await notify_robot(switch, db, req.robot, with_service=True, started=started,
                                  stopped=stopped))
    return out


async def _unstart(switch: Any, robot: RobotObjectV1, started: Mapping[str, str]) -> None:
    """The real transaction failed after the services were started: stop the ones this call
    started (one that was already running is not ours). Best effort, never raises."""
    mine = [svc for svc, what in started.items() if what == "started"]
    if switch is None or not mine:
        return
    try:
        await switch.stop(robot, mine)
    except Exception:  # noqa: BLE001
        logger.exception("Mapping services %s of robot %s not stopped after a refused start",
                         mine, robot.name)


async def _push_session(switch: Optional[Any], db: Any, robot_name: str) -> None:
    """After a compensation: push the robot's `session` (which never officially existed for a
    moment) again. Never raises."""
    on_session = getattr(switch, "on_session", None)
    if on_session is None:
        return
    try:
        async with open_store(db, uuid.uuid4()) as store:
            mine = await store.open_sessions_of_robot(robot_name)
        await on_session(robot_name, ms.robot_session_view(mine[0] if mine else None))
    except Exception:  # noqa: BLE001
        logger.exception("Session update for robot %s not pushed", robot_name)


async def _close_unstarted(db: Any, switch: Optional[Any], opened: _Opened,
                           publisher_id: uuid.UUID, actor: Optional[str]) -> None:
    """The session committed but its services did not start: close it (its MAP.SESSION_STARTED
    gets its MAP.SESSION_FINISHED; the map returns to its state before, so a draft stays a
    draft). No node can have come from it: its service is not running, and a node that still
    arrives (a service left running by an earlier session) is an ordinary node of a now closed
    session. Best effort: if this fails the session stays open and can be finished by hand."""
    session = opened.session
    try:
        async with open_store(db, publisher_id) as store:
            row = await store.lock_map(session["map_name"])
            current = await store.lock_session(session["session_id"])
            if current is not None and current["ended_at"] is None:
                await _finish_in(store, row, current, _utcnow(), actor,
                                 restore_state=opened.prior_state
                                 if opened.prior_state in (DRAFT, READY) else None)
    except Exception:  # noqa: BLE001
        logger.exception("Session %s of robot %s (failed start) not closed",
                         session["session_id"], session["robot_name"])
        return
    await _push_session(switch, db, session["robot_name"])


async def _repause(db: Any, switch: Optional[Any], session: Mapping[str, Any],
                   publisher_id: uuid.UUID, actor: Optional[str]) -> None:
    """A resume committed but its services did not start: pause the session again (its
    MAP.SESSION_RESUMED gets a MAP.SESSION_PAUSED). Best effort, like _close_unstarted."""
    try:
        async with open_store(db, publisher_id) as store:
            row = await store.lock_map(session["map_name"])
            current = await store.lock_session(session["session_id"])
            if current is not None and current["ended_at"] is None \
                    and current["paused_at"] is None:
                now = _utcnow()
                current["paused_at"] = now
                await store.update_session(str(current["session_id"]), paused_at=now)
                if row is not None:
                    await store.update_map(row, status={"state": PAUSED})
                await store.emit(_session_event(EventCode.MAP_SESSION_PAUSED, current, PAUSED,
                                                actor, now))
    except Exception:  # noqa: BLE001
        logger.exception("Session %s of robot %s (failed resume) not paused again",
                         session["session_id"], session["robot_name"])
        return
    await _push_session(switch, db, session["robot_name"])


async def place_session(db: Any, map_name: str, session_id: str, data: Any,
                        publisher_id: uuid.UUID, actor: Optional[str] = None,
                        switch: Optional[Any] = None,
                        holder: Optional[Any] = None) -> Dict[str, Any]:
    """POST /api/v1/maps/{id}/sessions/{sid}/place `{pose: {x, y, yaw}, robot_pose: {x, y,
    theta}}`: put the robot on a local map. Sets map_T_session, aligned (placed) and
    placement; MAP.SESSION_PLACED (from then on graph-builder keeps the session's nodes; the
    services are not touched). 404 unknown map/session; 409: finished, geo map (placed
    by its datum), an already placed MAPPING session (re-placing would split its nodes;
    alignment after the fact is M6), the robot offline, driving or moved (Q-U7). An operate
    session can be re-placed at any time (a correction).

    `source: "reloc"` (body `{source: "reloc"}`, no poses): the robot relocalises on a stored
    map its orchestrator holds (`holder`: packages/api/orchestrator_maps.py). Placed with
    ms.reloc_placement() (identity, D0 assumption); the robot-still check is skipped.
    Refused 409 when the session is already placed, the robot is offline or reports its position
    as not initialized, or the orchestrator does not (or cannot be asked to) hold the map. A low
    localization score does not refuse: it shows as `localization_warning` on the robot view. The
    manual placement path is unchanged."""
    req = parse_body(PlaceRequest, data)
    try:
        uuid.UUID(str(session_id))
    except ValueError:
        raise HTTPException(404, f"Did not find session \"{session_id}\"") from None
    now = _utcnow()
    reloc = req.source == ms.SOURCE_RELOC
    held: Optional[bool] = None
    if reloc:
        # Cheap refusals first (offline, already placed, ...): the orchestrator read is slow,
        # and it is never made inside a DB transaction.
        try:
            row, session, robot = await _reloc_inputs(db, map_name, session_id)
        except _SCHEMA_ERRORS as exc:
            raise _undefined_table(exc) from exc
        _placement_refusals(map_name, session_id, session, _alive(row, map_name), robot, True)
        if holder is not None:
            try:
                held = await holder.held(robot, map_name, fresh=True)
            except Exception:  # noqa: BLE001
                logger.exception("Stored map of session %s not readable", session_id)
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
                await check_robot_still(store, robot, req.robot_pose.dict())
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
    out.update(await notify_robot(switch, db, session["robot_name"]))
    return out


async def session_action(db: Any, map_name: str, session_id: str, action: str,
                         publisher_id: uuid.UUID, actor: Optional[str] = None,
                         switch: Optional[Any] = None) -> Dict[str, Any]:
    """pause / resume / finish (see the module docstring). Resume commits, then starts the
    session's services on the robot's orchestrator (a failed start: 502/504/409, the session is
    paused again; a no-op repeat starts them again if they are not running).
    Pause and finish stop them after the commit, best effort (a no-op repeat retries the stop):
    when that fails the session is closed anyway and the response has `robot_notified: false`
    and `mapping_warning`. A service another open session of the robot still runs is kept."""
    if action not in SESSION_ACTIONS:
        raise HTTPException(404, f"Unknown session action {action!r}")
    try:
        uuid.UUID(str(session_id))
    except ValueError:
        raise HTTPException(404, f"Did not find mapping session \"{session_id}\"") from None
    robot_name = None
    if switch is not None:  # the robot's lock is taken before the transaction, as in start
        async with open_store(db, uuid.uuid4()) as store:
            found = await store.lock_session(session_id)
        robot_name = found["robot_name"] if found is not None else None
    async with _robot_lock(switch, robot_name):
        out, robot, session = await _session_action(db, map_name, session_id, action,
                                                    publisher_id, actor, switch)
        started: Dict[str, str] = {}
        stopped: Optional[StopResult] = None
        if action == "resume" and switch is not None:
            try:
                started = await _start_for_resume(switch, session, robot)
            except BaseException:
                if out["changed"]:  # a no-op repeat has nothing to undo
                    await asyncio.shield(_repause(db, switch, session, publisher_id, actor))
                raise
        if action != "resume":
            stopped = await stop_services(db, switch, robot, session)
        out.update(await notify_robot(switch, db, out["session"]["robot_name"], started=started,
                                      stopped=stopped))
    return out


async def _session_action(db: Any, map_name: str, session_id: str, action: str,
                          publisher_id: uuid.UUID, actor: Optional[str],
                          switch: Optional[Any] = None
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
                map_state = await _finish_in(store, row, session, now, actor)
                return ({"map_id": map_name, "map_state": map_state, "changed": True,
                         "session": session_dict(session)}, robot, session)
            if ended:
                raise HTTPException(409, f"Mapping session {session_id} is finished")
            if ms.purpose_of(session) != ms.MAPPING:
                raise HTTPException(409, f"Session {session_id} is an operate session: "
                                         "only mapping sessions pause (finish it to stop "
                                         "using the map)")
            if action == "resume":
                _check_resume_robot(switch, robot)
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


def _check_resume_robot(switch: Optional[Any], robot: Optional[RobotObjectV1]) -> None:
    """Inside the resume's transaction (no call): an offline robot's service cannot start."""
    if switch is not None and robot is not None and not robot.status.online:
        raise HTTPException(409, f"Robot '{robot.name}' is offline: its mapping service "
                                 "cannot be started")


async def _start_for_resume(switch: Optional[Any], session: Mapping[str, Any],
                            robot: Optional[RobotObjectV1]) -> Dict[str, str]:
    """After the resume committed (or on a repeat): start the session's services."""
    _check_resume_robot(switch, robot)
    return await start_services(switch, session, robot)


def _unchanged(map_name: str, map_state: str, session: Mapping[str, Any]) -> Dict[str, Any]:
    return {"map_id": map_name, "map_state": map_state, "changed": False,
            "session": session_dict(session)}
