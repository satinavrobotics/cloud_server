"""Typed maps, map lifecycle and mapping sessions (docs/satinav-maps-redesign.md §2-§4, §7; M1).

    POST   /api/v1/maps                                  create_map()     -> draft
    GET    /api/v1/maps?type=&state=&include_archived=   filter_maps()
    GET    /api/v1/maps/{id}                             session_summary() (added to the old body)
    PATCH  /api/v1/maps/{id}                             patch_map()      description only
    POST   /api/v1/maps/{id}/sessions                    start_session()
    POST   /api/v1/maps/{id}/sessions/{sid}/pause|resume|finish   session_action()
    POST   /api/v1/maps/{id}/archive|restore             archive_map() / restore_map()
    DELETE /api/v1/maps/{id}                             refuse_open_session() guards it

    PUT    /api/v1/robots/{r}/map                        assign_robot_map() DEPRECATED shim (M2)

The old map routes POST /map/load and PUT /maps/{id}/datum are unchanged. PUT
/robots/{r}/map (the old client's "assign map") drives sessions since M2 (assign_robot_map,
below) and still writes robot.current_map for the consumers that read it; it goes away with the
client's Maps page (M4).

Storage: the map is its `mapobjectv1` row (spec.type/geo, status.state/open_session_id; the
object `lifecycle` ALIVE/DELETING stays the delete bookkeeping), sessions are `map_sessions`
rows (migration 20260928_01_map_sessions). Every write here is ONE transaction on a pooled
connection (PostgresDatabase.connection()): the map row is locked FOR UPDATE first, so writes to
one map serialise; the session row, the map status, the `<publisher> <name> <lifecycle>` NOTIFY
on the map table and the MAP.* event commit together. The event is written in a savepoint: a
failing event write is logged and never fails the change.

Session rules (M1):
- the robot must exist (404) and be online (409); a robot has at most one open session anywhere
  (409; the partial unique index backs this up);
- one open session per map (409). The schema allows several (multi-robot mapping, doc §10), the
  API does not yet;
- geo map: the robot's current datum is required (409 without one). The first session of a geo
  map without an origin sets it (doc Q1): `geo` = the datum's UTM point in its own zone, and the
  map's legacy datum_* fields (when unset) = that origin as a 'utm' datum, so the old client and
  planner place the map exactly. map_T_session: packages/utils/map_geo.py;
- local map: identity; aligned only for the map's first session (a later one waits for M6);
- since M2 graph-builder ingests by the robot's open session
  (packages/services/graph_builder/ingest.py), so a session is what makes a robot's nodes land
  in a map;
- M3: after every session change commits (start, pause, resume, finish, the shim), the robot's
  retained `{prefix}/{robot}/mapping/set` is published from its open session (notify_robot;
  contract in packages/api/mapping_control.py). A publish failure never fails the call: the
  response says `robot_notified: false`. A session starts even when the robot's topomap
  service is not running (`mapping_service: "not_running"`, doc Q3).

Lifecycle: draft -> mapping <-> paused -> ready (finish) -> archived -> ready|draft (restore).
Archive and delete are refused while a session is open. Repeating pause/resume/finish/archive/
restore on a map or session already in that state is a no-op (`changed: false`, no event).
"""

import contextlib
import datetime
import json
import logging
import re
import uuid
from typing import Any, AsyncIterator, Callable, Dict, List, Mapping, Optional

import psycopg
import pydantic
from fastapi import HTTPException

from cloud_common.objects.map import (
    MAP_STATES, MAP_TYPES, MapObjectV1, MapSpecV1, MapStatusV1, effective_state,
    effective_type,
)
from cloud_common.objects.object import ObjectLifecycleV1
from cloud_common.objects.robot import RobotObjectV1
from packages.api.mapping_control import force_off_payload, set_payload
from packages.events.codes import EventCode, Source
from packages.events.emit import Event, emit
from packages.utils import map_geo

logger = logging.getLogger("ApiDelegationService.maps")

MAP_TABLE = MapObjectV1.table_name()
ROBOT_TABLE = RobotObjectV1.table_name()
SESSIONS_TABLE = "map_sessions"
DELETING = ObjectLifecycleV1.DELETING.value
ALIVE = ObjectLifecycleV1.ALIVE.value

DRAFT, MAPPING, PAUSED, READY, ARCHIVED = MAP_STATES
OPEN_STATES = (MAPPING, PAUSED)
SESSION_ACTIONS = ("pause", "resume", "finish")
# Robot-map sentinels of the old API (robot.current_map); never real map names.
RESERVED_NAMES = frozenset({"GEO", "LOCAL"})
# The name is the key in Postgres, ArangoDB (nodes_<name>) and MinIO (bucket map-<name>,
# lower-cased, '_' -> '-'; 63 characters at most), so it is restricted to what all three take.
NAME_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9_-]{0,57}[A-Za-z0-9])?$")
SUMMARY_MAX_SESSIONS = 50

SESSION_COLUMNS = ("session_id", "map_name", "robot_name", "kind", "started_at", "paused_at",
                   "ended_at", "datum", "map_t_session", "aligned", "node_count")

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
            raise ValueError(f"{value!r} is reserved (robot-map sentinel)")
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


class StartSessionRequest(pydantic.BaseModel):
    robot: str

    class Config:
        extra = pydantic.Extra.forbid


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
        return effective_state(self.obj.status)


def _iso(ts: Any) -> Any:
    return ts.isoformat() if isinstance(ts, datetime.datetime) else ts


def session_dict(row: Mapping[str, Any]) -> Dict[str, Any]:
    """A session as the API returns it (map_t_session is shown as map_T_session, doc §4)."""
    state = ("finished" if row.get("ended_at") is not None
             else "paused" if row.get("paused_at") is not None else "mapping")
    return {
        "session_id": str(row["session_id"]),
        "map_name": row["map_name"],
        "robot_name": row["robot_name"],
        "kind": row.get("kind", "live"),
        "state": state,
        "started_at": _iso(row.get("started_at")),
        "paused_at": _iso(row.get("paused_at")),
        "ended_at": _iso(row.get("ended_at")),
        "datum": row.get("datum"),
        "map_T_session": row.get("map_t_session"),
        "aligned": row.get("aligned"),
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
        """The robot row FOR UPDATE (serialises two assigns of one robot)."""
        await self.cursor.execute(
            f"SELECT name, lifecycle, spec, status FROM {ROBOT_TABLE} WHERE name = %s "
            "AND lifecycle <> 'DELETED' FOR UPDATE", (name,))
        row = await self.cursor.fetchone()
        if row is None:
            return None
        name, lifecycle, spec, status = row
        return RobotObjectV1(name=name, lifecycle=ObjectLifecycleV1[lifecycle], status=status,
                             **spec)

    async def set_current_map(self, robot: RobotObjectV1, value: Optional[str]) -> None:
        """robot.current_map only (spec || patch, like update_spec_fields) and the robot NOTIFY
        the dispatcher's watcher reads."""
        await self.cursor.execute(
            f"UPDATE {ROBOT_TABLE} SET spec = spec || %s::jsonb WHERE name = %s",
            (json.dumps({"current_map": value}), robot.name))
        await self.cursor.execute("SELECT pg_notify(%s, %s)", (
            ROBOT_TABLE, f"{self.publisher_id} {robot.name} {robot.lifecycle.value}"))

    async def _sessions(self, where: str, params: tuple) -> List[Dict[str, Any]]:
        await self.cursor.execute(
            f"SELECT {', '.join(SESSION_COLUMNS)} FROM {SESSIONS_TABLE} WHERE {where} "
            "ORDER BY started_at, session_id", params)
        return [dict(zip(SESSION_COLUMNS, r)) for r in await self.cursor.fetchall()]

    async def sessions(self, map_name: str) -> List[Dict[str, Any]]:
        return await self._sessions("map_name = %s", (map_name,))

    async def open_sessions_of_robot(self, robot_name: str) -> List[Dict[str, Any]]:
        return await self._sessions("robot_name = %s AND ended_at IS NULL", (robot_name,))

    async def open_sessions(self) -> List[Dict[str, Any]]:
        return await self._sessions("ended_at IS NULL", ())

    async def robot_names(self) -> List[str]:
        await self.cursor.execute(f"SELECT name FROM {ROBOT_TABLE} WHERE lifecycle <> 'DELETED'")
        return [r[0] for r in await self.cursor.fetchall()]

    async def lock_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        await self.cursor.execute(
            f"SELECT {', '.join(SESSION_COLUMNS)} FROM {SESSIONS_TABLE} "
            "WHERE session_id = %s FOR UPDATE", (uuid.UUID(str(session_id)),))
        row = await self.cursor.fetchone()
        return dict(zip(SESSION_COLUMNS, row)) if row is not None else None

    async def insert_session(self, session: Dict[str, Any]) -> None:
        try:
            await self.cursor.execute(
                f"INSERT INTO {SESSIONS_TABLE} (session_id, map_name, robot_name, kind, "
                "started_at, datum, map_t_session, aligned, node_count) "
                "VALUES (%s, %s, %s, 'live', %s, %s::jsonb, %s::jsonb, %s, 0)",
                (uuid.UUID(str(session["session_id"])), session["map_name"], session["robot_name"],
                 session["started_at"],
                 json.dumps(session["datum"]) if session["datum"] is not None else None,
                 json.dumps(session["map_t_session"]), session["aligned"]))
        except psycopg.errors.UniqueViolation as exc:
            raise HTTPException(409, f"Robot {session['robot_name']!r} already has an open "
                                     "mapping session") from exc

    async def update_session(self, session_id: str, **fields: Any) -> None:
        cols = ", ".join(f"{k} = %s" for k in fields)
        await self.cursor.execute(f"UPDATE {SESSIONS_TABLE} SET {cols} WHERE session_id = %s",
                                  (*fields.values(), uuid.UUID(str(session_id))))

    async def emit(self, event: Event) -> None:
        """The event in a savepoint: its failure is logged and never fails the change."""
        try:
            async with self.conn.transaction():
                await emit(self.conn, event)
            stats["written"] += 1
        except Exception:  # noqa: BLE001
            stats["failed"] += 1
            logger.exception("Could not write %s (the map change still commits)", event.code)


@contextlib.asynccontextmanager
async def open_store(db: Any, publisher_id: uuid.UUID) -> AsyncIterator[SqlStore]:
    """One transaction: commits on a clean exit, rolls back on an exception."""
    async with db.connection() as conn:
        async with conn.cursor() as cursor:
            yield SqlStore(conn, cursor, publisher_id)


def _undefined_table(exc: Exception) -> HTTPException:
    return HTTPException(503, "Mapping sessions are not available (database migrations not "
                              "applied)")


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
                          "aligned": session.get("aligned"),
                          "map_T_session": session.get("map_t_session"), "actor": actor})


def map_view(obj: MapObjectV1) -> Dict[str, Any]:
    """obj.dict() with the effective type/state filled in (old rows have neither)."""
    data = obj.dict()
    data["type"] = effective_type(obj)
    data.setdefault("status", {})["state"] = effective_state(obj.status)
    return data


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


async def patch_map(db: Any, name: str, data: Any, publisher_id: uuid.UUID) -> Dict[str, Any]:
    req = parse_body(PatchMapRequest, data)
    changes = req.dict(exclude_unset=True)
    async with open_store(db, publisher_id) as store:
        row = await _lock_alive_map(store, name)
        if changes:
            await store.update_map(row, spec=changes)
            row.spec.update(changes)
    return map_view(MapObjectV1(name=name, status=row.status, **row.spec))


async def session_summary(db: Any, name: str, control: Optional[Any] = None
                          ) -> Dict[str, Any]:
    """The `sessions` block of GET /api/v1/maps/{id}: counts, the open session, and the newest
    SUMMARY_MAX_SESSIONS sessions (newest first). M3: `mapping_state` / `mapping_service` of
    the open session's robot (null without an open session; packages/api/mapping_control.py)."""
    try:
        async with open_store(db, uuid.uuid4()) as store:
            rows = await store.sessions(name)
    except psycopg.errors.UndefinedTable as exc:
        raise _undefined_table(exc) from exc
    items = [session_dict(r) for r in reversed(rows)]
    open_items = [s for s in items if s["state"] != "finished"]
    open_session = open_items[0] if open_items else None
    summary = {"count": len(items), "open": open_session,
               "unaligned": sum(1 for s in items if s["aligned"] is False),
               "items": items[:SUMMARY_MAX_SESSIONS],
               "mapping_state": None, "mapping_service": None}
    if open_session is not None and control is not None:
        robot = open_session["robot_name"]
        summary["mapping_state"] = control.state(robot)
        summary["mapping_service"] = control.mapping_service(robot)
    return summary


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
            if open_sessions or state in OPEN_STATES:
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


REFUSE_OPEN_SESSION_SQL = (f"SELECT session_id FROM {SESSIONS_TABLE} "
                           "WHERE map_name = %s AND ended_at IS NULL LIMIT 1")
LOCK_MAP_SQL = f"SELECT 1 FROM {MAP_TABLE} WHERE name = %s FOR UPDATE"


async def refuse_open_session(cursor: Any, map_id: str) -> None:
    """DELETE /api/v1/maps/{id} guard, run by MapDeleter.request() in its own transaction before
    the map is marked DELETING: the map row lock serialises it with a session start."""
    await cursor.execute(LOCK_MAP_SQL, (map_id,))
    await cursor.execute(REFUSE_OPEN_SESSION_SQL, (map_id,))
    if await cursor.fetchone() is not None:
        raise HTTPException(409, f"Map '{map_id}' has an open mapping session; finish it "
                                 "before deleting the map")


# --- sessions ----------------------------------------------------------------------------------

def plan_session(row: MapRow, robot: RobotObjectV1, previous: List[Dict[str, Any]]
                 ) -> Dict[str, Any]:
    """What a new session on `row` by `robot` records, and what it changes on the map:
    {'datum', 'map_t_session', 'aligned', 'spec'} (pure; 409 if a geo map has no datum)."""
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
                "aligned": True, "spec": spec_patch}
    return {"datum": None, "map_t_session": dict(map_geo.IDENTITY), "aligned": not previous,
            "spec": spec_patch}


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


async def _start_in(store: Any, row: MapRow, robot: Optional[RobotObjectV1], robot_name: str,
                    now: datetime.datetime, actor: Optional[str]) -> Dict[str, Any]:
    """Start a session on the locked map `row` inside the caller's transaction (the rules of
    the module docstring); the new session row."""
    map_name = row.name
    if row.state == ARCHIVED:
        raise HTTPException(409, f"Map '{map_name}' is archived; restore it first")
    if robot is None:
        raise HTTPException(404, f"Did not find \"robot\" with name \"{robot_name}\"")
    if not robot.status.online:
        raise HTTPException(409, f"Robot '{robot_name}' is offline")
    mine = await store.open_sessions_of_robot(robot_name)
    if mine:
        raise HTTPException(409, f"Robot '{robot_name}' already has an open mapping "
                                 f"session on map '{mine[0]['map_name']}'")
    previous = await store.sessions(map_name)
    if any(s["ended_at"] is None for s in previous):
        raise HTTPException(409, f"Map '{map_name}' already has an open mapping "
                                 "session (one robot per map for now)")
    plan = plan_session(row, robot, previous)
    session = {"session_id": str(uuid.uuid4()), "map_name": map_name,
               "robot_name": robot_name, "kind": "live", "started_at": now,
               "paused_at": None, "ended_at": None, "datum": plan["datum"],
               "map_t_session": plan["map_t_session"], "aligned": plan["aligned"],
               "node_count": 0}
    await store.insert_session(session)
    await store.update_map(row, spec=plan["spec"] or None,
                           status={"state": MAPPING, "open_session_id": session["session_id"]})
    row.spec.update(plan["spec"] or {})
    row.status.update(state=MAPPING, open_session_id=session["session_id"])
    await store.emit(_session_event(EventCode.MAP_SESSION_STARTED, session, MAPPING, actor, now))
    return session


async def _finish_in(store: Any, row: MapRow, session: Dict[str, Any], now: datetime.datetime,
                     actor: Optional[str]) -> str:
    """Finish the open `session` of the locked map `row` inside the caller's transaction; the
    map's state afterwards (ready unless another session is still open)."""
    session_id = str(session["session_id"])
    session.update(ended_at=now, paused_at=None)
    await store.update_session(session_id, ended_at=now, paused_at=None)
    others = [s for s in await store.sessions(row.name)
              if s["ended_at"] is None and str(s["session_id"]) != session_id]
    status = ({"state": READY, "open_session_id": None} if not others else
              {"open_session_id": str(others[0]["session_id"])})
    await store.update_map(row, status=status)
    row.status.update(status)
    map_state = status.get("state", row.state)
    await store.emit(_session_event(EventCode.MAP_SESSION_FINISHED, session, map_state, actor,
                                    now))
    return map_state


async def notify_robot(control: Optional[Any], db: Any, robot_name: str,
                       with_service: bool = False) -> Dict[str, Any]:
    """M3: after a session change has COMMITTED, publish the robot's retained
    `{prefix}/{robot}/mapping/set` from its open session as it is now (re-read, so the newest
    committed state is what the robot ends up with; per-robot lock). Never raises: a failure
    is logged and reported as `robot_notified: false` (the API call itself succeeded). The
    response keys (additive): robot_notified, mapping_state, and with `with_service`
    mapping_service ("running" | "not_running", doc Q3)."""
    if control is None:
        return {}
    ok = False
    try:
        async with control.lock(robot_name):
            async with open_store(db, uuid.uuid4()) as store:
                mine = await store.open_sessions_of_robot(robot_name)
            ok = await control.publish_set(robot_name, set_payload(mine[0] if mine else None))
    except Exception:  # noqa: BLE001
        logger.exception("Mapping switch for robot %s not published", robot_name)
    out: Dict[str, Any] = {"robot_notified": ok, "mapping_state": control.state(robot_name)}
    if with_service:
        out["mapping_service"] = control.mapping_service(robot_name)
    return out


async def robot_mapping_off(db: Any, robot_name: str, control: Any) -> Dict[str, Any]:
    """POST /robots/{r}/mapping/off: turn off a robot's capture when it has NO open session
    (e.g. a local `~/set_enabled true` left on: its nodes are ignored). Publishes the retained
    no-session set message with `force: true` (mapping_control.force_off_payload), which the
    robot applies even if unchanged. 404 unknown robot; 409 while the robot has an open
    session (that session's pause / finish is the way to stop it). Checked under the robot's
    publish lock, so a session started concurrently is not overwritten by this message."""
    async with control.lock(robot_name):
        async with open_store(db, uuid.uuid4()) as store:
            robot = await store.robot(robot_name)
            mine = await store.open_sessions_of_robot(robot_name)
        if robot is None:
            raise HTTPException(status_code=404, detail=f"Robot {robot_name} not found")
        if mine:
            raise HTTPException(
                status_code=409,
                detail=f"Robot {robot_name} has an open mapping session: finish or pause the "
                       f"session on map {mine[0]['map_name']}")
        ok = await control.publish_set(robot_name, force_off_payload())
    return {"robot_notified": ok, "mapping_state": control.state(robot_name)}


async def sync_all_robots(control: Any, db: Any) -> Dict[str, bool]:
    """Re-publish every robot's set message (on every API (re)connect to the broker: the
    broker keeps no retained messages across its own restart, and this also covers sessions
    opened before M3). Robots without an open session get `enabled: false`."""
    async with open_store(db, uuid.uuid4()) as store:
        names = await store.robot_names()
        open_rows = await store.open_sessions()
    by_robot = {s["robot_name"]: s for s in open_rows}
    results: Dict[str, bool] = {}
    for name in sorted(set(names) | set(by_robot)):
        async with control.lock(name):
            async with open_store(db, uuid.uuid4()) as store:  # fresh: a change may have won
                mine = await store.open_sessions_of_robot(name)
            results[name] = await control.publish_set(name,
                                                      set_payload(mine[0] if mine else None))
    logger.info("Mapping switch re-published for %d robots (%d not acknowledged)",
                len(results), sum(1 for ok in results.values() if not ok))
    return results


async def start_session(db: Any, map_name: str, data: Any, publisher_id: uuid.UUID,
                        actor: Optional[str] = None, control: Optional[Any] = None
                        ) -> Dict[str, Any]:
    req = parse_body(StartSessionRequest, data)
    now = _utcnow()
    try:
        async with open_store(db, publisher_id) as store:
            row = await _lock_alive_map(store, map_name)
            robot = await store.robot(req.robot)
            session = await _start_in(store, row, robot, req.robot, now, actor)
    except psycopg.errors.UndefinedTable as exc:
        raise _undefined_table(exc) from exc
    out = {"map_id": map_name, "map_state": MAPPING, "changed": True,
           "session": session_dict(session)}
    out.update(await notify_robot(control, db, req.robot, with_service=True))
    return out


async def session_action(db: Any, map_name: str, session_id: str, action: str,
                         publisher_id: uuid.UUID, actor: Optional[str] = None,
                         control: Optional[Any] = None) -> Dict[str, Any]:
    """pause / resume / finish (see the module docstring). M3: then the robot's set message
    (also on a no-op repeat, which re-sends the current state)."""
    out = await _session_action(db, map_name, session_id, action, publisher_id, actor)
    out.update(await notify_robot(control, db, out["session"]["robot_name"]))
    return out


async def _session_action(db: Any, map_name: str, session_id: str, action: str,
                          publisher_id: uuid.UUID, actor: Optional[str]) -> Dict[str, Any]:
    if action not in SESSION_ACTIONS:
        raise HTTPException(404, f"Unknown session action {action!r}")
    try:
        uuid.UUID(str(session_id))
    except ValueError:
        raise HTTPException(404, f"Did not find mapping session \"{session_id}\"") from None
    now = _utcnow()
    try:
        async with open_store(db, publisher_id) as store:
            row = await _lock_alive_map(store, map_name)
            session = await store.lock_session(session_id)
            if session is None or session["map_name"] != map_name:
                raise HTTPException(404, f"Did not find mapping session \"{session_id}\" on "
                                         f"map \"{map_name}\"")
            ended = session["ended_at"] is not None
            paused = session["paused_at"] is not None
            if action == "finish":
                if ended:
                    return _unchanged(map_name, row.state, session)
                map_state = await _finish_in(store, row, session, now, actor)
                return {"map_id": map_name, "map_state": map_state, "changed": True,
                        "session": session_dict(session)}
            else:
                if ended:
                    raise HTTPException(409, f"Mapping session {session_id} is finished")
                if (action == "pause") == paused:
                    return _unchanged(map_name, row.state, session)
                stamp = now if action == "pause" else None
                session["paused_at"] = stamp
                await store.update_session(session_id, paused_at=stamp)
                status = {"state": PAUSED if action == "pause" else MAPPING}
                code = (EventCode.MAP_SESSION_PAUSED if action == "pause"
                        else EventCode.MAP_SESSION_RESUMED)
            await store.update_map(row, status=status)
            map_state = status.get("state", row.state)
            await store.emit(_session_event(code, session, map_state, actor, now))
    except psycopg.errors.UndefinedTable as exc:
        raise _undefined_table(exc) from exc
    return {"map_id": map_name, "map_state": map_state, "changed": True,
            "session": session_dict(session)}


def _unchanged(map_name: str, map_state: str, session: Mapping[str, Any]) -> Dict[str, Any]:
    return {"map_id": map_name, "map_state": map_state, "changed": False,
            "session": session_dict(session)}


# --- the old "assign map" (DEPRECATED shim, maps redesign M2) --------------------------------

class AssignRobotMapRequest(pydantic.BaseModel):
    """PUT /api/v1/robots/{r}/map. `map_id`: a map name, the old 'GEO' / 'LOCAL' sentinels, or
    null / '' to clear."""
    map_id: Optional[str] = None


def _new_map_type(robot: Optional[RobotObjectV1]) -> str:
    """The type of a map the shim creates: the M1 rule (geo iff a real datum), applied to the
    robot's datum, as POST /map/load applies it to the datum it is given."""
    return "geo" if robot is not None and map_geo.robot_datum(robot.datum) is not None else "local"


async def assign_robot_map(db: Any, robot_name: str, map_id: Optional[str],
                           publisher_id: uuid.UUID, actor: Optional[str] = None,
                           arango_node_count: Optional[Callable[[str], int]] = None,
                           control: Optional[Any] = None) -> Dict[str, Any]:
    """DEPRECATED (goes away with the client's Maps page, M4): the old client's "assign map",
    PUT /api/v1/robots/{r}/map, now drives mapping sessions so live mapping keeps working.

    - a real map name: finish the robot's open session if it is on another map; create the map
      if it does not exist (draft, typed from the robot's datum, MAP.CREATED; name rules and
      409s as POST /api/v1/maps); start a session on it (the rules and errors of POST
      /api/v1/maps/{id}/sessions: robot online, geo needs a datum, one open session per map,
      not archived). Re-assigning the map the robot is already mapping changes nothing.
    - 'GEO' / 'LOCAL' / null / '': finish the robot's open session, if any.

    robot.current_map is still written (the sentinel, the name, or null): the old client, the
    run recorder and the bag metadata read it (docs/satinav-maps-redesign.md §13.2). All of it
    is one transaction: a refused session start leaves the old session, the map list and
    current_map as they were."""
    target = (map_id or "").strip() or None
    sentinel = target is None or target in RESERVED_NAMES
    now = _utcnow()
    created = False
    finished: Optional[Dict[str, Any]] = None
    session: Optional[Dict[str, Any]] = None
    try:
        async with open_store(db, publisher_id) as store:
            robot = await store.lock_robot(robot_name)
            if robot is None:
                raise HTTPException(404, f"Did not find \"robot\" with name \"{robot_name}\"")
            mine = await store.open_sessions_of_robot(robot_name)
            current = mine[0] if mine else None
            if not sentinel and current is not None and current["map_name"] == target:
                session = current  # already mapping this map (maybe paused): nothing to do
            else:
                names = sorted({n for n in (None if sentinel else target,
                                            current["map_name"] if current else None) if n})
                rows: Dict[str, Optional[MapRow]] = {}
                for name in names:  # lock in name order: two assigns never deadlock
                    rows[name] = await store.lock_map(name)
                row = None
                if not sentinel:
                    row = rows[target]
                    if row is None:
                        row = await _create_for_assign(store, target, robot, now, actor,
                                                       arango_node_count)
                        created = True
                    elif row.lifecycle == DELETING:
                        raise HTTPException(409, f"Map '{target}' is being deleted")
                if current is not None:
                    old_row = rows.get(current["map_name"])
                    if old_row is not None:
                        await _finish_in(store, old_row, current, now, actor)
                    else:  # its map row is gone: close the session anyway
                        current.update(ended_at=now, paused_at=None)
                        await store.update_session(str(current["session_id"]), ended_at=now,
                                                   paused_at=None)
                    finished = current
                if row is not None:
                    session = await _start_in(store, row, robot, robot_name, now, actor)
            if robot.current_map != target:
                await store.set_current_map(robot, target)
    except psycopg.errors.UndefinedTable as exc:
        raise _undefined_table(exc) from exc
    out = {"success": True, "robot_name": robot_name, "current_map": target,
           "deprecated": "PUT /api/v1/robots/{robot}/map: use POST /api/v1/maps/{id}/sessions "
                         "and .../sessions/{sid}/finish",
           "map_created": created,
           "finished_session": session_dict(finished) if finished else None,
           "session": session_dict(session) if session else None}
    out.update(await notify_robot(control, db, robot_name, with_service=True))
    return out


async def _create_for_assign(store: Any, name: str, robot: RobotObjectV1,
                             now: datetime.datetime, actor: Optional[str],
                             arango_node_count: Optional[Callable[[str], int]]) -> MapRow:
    """A map for the shim's "NEW MAP" flow, with create_map()'s checks; the new locked row."""
    map_type = _new_map_type(robot)
    req = parse_body(CreateMapRequest, {"name": name, "type": map_type})
    if arango_node_count is not None:
        nodes = arango_node_count(req.name)
        if nodes:
            raise HTTPException(409, f"ArangoDB already has {nodes} nodes for map "
                                     f"'{req.name}' (no Postgres row); choose another name")
    clash = [n for n in await store.map_names()
             if n != req.name and bucket_key(n) == bucket_key(req.name)]
    if clash:
        raise HTTPException(409, f"Map name '{req.name}' collides with existing map "
                                 f"'{clash[0]}' (same image bucket)")
    spec = json.loads(MapSpecV1(type=map_type).json())
    status = json.loads(MapStatusV1(state=DRAFT).json())
    if not await store.insert_map(req.name, spec, status):
        raise HTTPException(409, f"Map '{req.name}' already exists")
    await store.emit(_map_event(EventCode.MAP_CREATED, req.name, map_type, DRAFT, actor, now))
    row = await store.lock_map(req.name)
    if row is None:  # pragma: no cover - just inserted in this transaction
        raise HTTPException(500, f"Map '{req.name}' vanished")
    return row
