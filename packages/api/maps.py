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
    effective_type,
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


class PlaceRequest(pydantic.BaseModel):
    """POST .../sessions/{sid}/place, and `placement` on start."""
    pose: MapPose
    robot_pose: RobotPose

    class Config:
        extra = pydantic.Extra.forbid


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
    row = await store.lock_map(name)
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
    """The map's datum_* fields for a geo map origin: a 'utm' datum at the origin, bearing 0,
    i.e. exactly the map frame, for the old client/planner (map/load `transform`)."""
    from packages.utils import geo as geo_mod
    lat, lon = geo_mod.utm_to_latlon(geo["origin_e"], geo["origin_n"], geo["utm_zone"],
                                     geo["utm_north"])
    return {"datum_latitude": lat, "datum_longitude": lon, "datum_bearing_deg": 0.0,
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
    mine = await store.open_sessions_of_robot(robot_name)
    if mine:
        raise HTTPException(409, f"Robot '{robot_name}' already has an open "
                                 f"{ms.purpose_of(mine[0])} session on map "
                                 f"'{mine[0]['map_name']}' (pass replace: true to end it)")
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
                        switch: Optional[Any] = None) -> Dict[str, Any]:
    """POST /api/v1/maps/{id}/sessions/{sid}/place `{pose: {x, y, yaw}, robot_pose: {x, y,
    theta}}`: put the robot on a local map. Sets map_T_session, aligned (placed) and
    placement; MAP.SESSION_PLACED (from then on graph-builder keeps the session's nodes; the
    services are not touched). 404 unknown map/session; 409: finished, geo map (placed
    by its datum), an already placed MAPPING session (re-placing would split its nodes;
    alignment after the fact is M6), the robot offline, driving or moved (Q-U7). An operate
    session can be re-placed at any time (a correction)."""
    req = parse_body(PlaceRequest, data)
    try:
        uuid.UUID(str(session_id))
    except ValueError:
        raise HTTPException(404, f"Did not find session \"{session_id}\"") from None
    now = _utcnow()
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
                raise HTTPException(409, f"Map '{map_name}' is a geo map: its sessions are "
                                         "placed by the robot's datum")
            if ms.purpose_of(session) == ms.MAPPING and ms.is_placed(session):
                raise HTTPException(409, f"Mapping session {session_id} is already placed; "
                                         "re-placing it would split its nodes (finish it and "
                                         "start a new session to continue from elsewhere)")
            robot = await store.robot(session["robot_name"])
            if robot is None:
                raise HTTPException(404, f"Did not find \"robot\" with name "
                                         f"\"{session['robot_name']}\"")
            if not robot.status.online:
                raise HTTPException(409, f"Robot '{robot.name}' is offline")
            await check_robot_still(store, robot, req.robot_pose.dict())
            old = session.get("map_t_session") if ms.is_placed(session) else None
            placement = _placement_record(req, ms.SOURCE_USER, actor, now)
            transform = ms.placement_transform(placement["pose"], placement["robot_pose"])
            session.update(map_t_session=transform, aligned=True, placement=placement)
            await store.update_session(session_id, map_t_session=transform, aligned=True,
                                       placement=placement)
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
