"""Ingest by mapping session (docs/satinav-maps-redesign.md §6, maps redesign M2).

graph-builder no longer writes to `robot.current_map` or a `"default"` map. Every node and
image from a robot goes to the robot's **open mapping session** (`map_sessions`, one per robot
at most) or is dropped:

    reason            when
    no_session        the robot has no open session
    session_paused    its session is paused
    map_not_mapping   the session's map is not `mapping` (draft/ready/archived: defensive, the
                      API keeps the two in step)
    map_deleting      the map is being deleted (object lifecycle DELETING)
    map_missing       the session names a map without a Postgres row
    session_mismatch  the payload carries a `session_id` that is not the open session (robot-side
                      tagging is M3; a payload without `session_id` is accepted)
    datum_changed     geo session: the robot's current datum is not the one the session was
                      started with (a new robot run: its frame moved) and cannot be used to
                      re-anchor the session (see below: a datum in another UTM zone, a map
                      without an origin); a local map's session too. Finish the session and
                      start a new one
    lookup_failed     Postgres could not be asked

Realignment (a robot restart mid-session on a GEO map): the robot takes a new datum at every
navstack start, and a geo map's frame is absolute (UTM grid metres from the map's `geo` origin),
so the session's map_T_session is re-derived from the new datum (map_geo.session_transform) and
stored with it in `map_sessions` (`plan_realign`, `REALIGN_SQL`), and MAP.SESSION_REALIGNED is
recorded. The update is a compare-and-set on the old stored datum, so concurrent ingests agree:
one wins and writes the event, the others re-read the session and use the stored transform.
Nodes stored before keep their map-frame poses; nothing links the last node before the restart
to the first after (edges are proximity edges, as always). A local map has no absolute frame and
still rejects (`datum_changed`), as does a datum outside the map's UTM zone / hemisphere.

The robot is taken from the payload's `robot_name`; any `map_id` in the payload is ignored.

Drops are counted and reported as MAP.INGEST_REJECTED, at most one event per robot and reason
per REJECT_EVENT_INTERVAL_S, carrying the number of nodes and images dropped since the last one
(RejectLimiter; a periodic flush reports drops that no later drop would carry).

The session lookup is cached for SESSION_CACHE_TTL_S (1 s) per robot: one indexed query per robot
and second at most, while a pause/finish through the API reaches ingest within ~1 s (the doc
asks for 2 s). No LISTEN/NOTIFY: the API's map NOTIFY does not name the robot, and a TTL this
short is simpler than keeping a second watcher connection alive.

Poses: a node's robot-frame pose (x, y, yaw) becomes `pose` = map_T_session applied
(packages/utils/map_geo.py), and the document keeps `robot_pose` (as received) and
`session_id`.
"""

import dataclasses
import datetime
import json
import logging
import time
import uuid as uuid_t
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from packages.events.codes import EventCode, Source
from packages.events.emit import Event
from packages.utils import geo, map_geo

logger = logging.getLogger("GraphBuilderService.ingest")

SESSION_CACHE_TTL_S = 1.0
REJECT_EVENT_INTERVAL_S = 60.0
ACCEPTING_STATES = ("mapping",)

NO_SESSION = "no_session"
SESSION_PAUSED = "session_paused"
MAP_NOT_MAPPING = "map_not_mapping"
MAP_DELETING = "map_deleting"
MAP_MISSING = "map_missing"
SESSION_MISMATCH = "session_mismatch"
DATUM_CHANGED = "datum_changed"
LOOKUP_FAILED = "lookup_failed"
REASONS = (NO_SESSION, SESSION_PAUSED, MAP_NOT_MAPPING, MAP_DELETING, MAP_MISSING,
           SESSION_MISMATCH, DATUM_CHANGED, LOOKUP_FAILED)
# Datum comparison tolerances: the datum is a fixed per-run origin, re-sent unchanged.
_DEG_TOL = 1e-9
_M_TOL = 1e-4

# One row per robot at most (partial unique index map_sessions_one_open_per_robot).
OPEN_SESSION_SQL = (
    "SELECT s.session_id, s.map_name, s.paused_at IS NOT NULL, s.map_t_session, "
    "m.lifecycle, m.status->>'state', s.datum, r.spec->'datum', "
    "m.spec->'geo', m.spec->>'type' "
    "FROM map_sessions s LEFT JOIN mapobjectv1 m "
    "ON m.name = s.map_name AND m.lifecycle <> 'DELETED' "
    "LEFT JOIN robotobjectv1 r ON r.name = s.robot_name AND r.lifecycle <> 'DELETED' "
    "WHERE s.robot_name = %s AND s.ended_at IS NULL")
# Compare-and-set on the datum read: only the ingest that saw the old datum wins (rowcount 1).
REALIGN_SQL = ("UPDATE map_sessions SET datum = %s::jsonb, map_t_session = %s::jsonb "
               "WHERE session_id = %s AND ended_at IS NULL AND datum = %s::jsonb")
COUNT_SQL = "UPDATE map_sessions SET node_count = node_count + %s WHERE session_id = %s"


@dataclasses.dataclass(frozen=True)
class OpenSession:
    """The robot's open session as read from Postgres (one row of OPEN_SESSION_SQL)."""
    session_id: str
    map_name: str
    paused: bool
    map_t_session: Dict[str, float]
    map_lifecycle: Optional[str]  # None: no mapobjectv1 row
    map_state: Optional[str]
    session_datum: Optional[Dict[str, Any]] = None  # set for geo sessions only
    robot_datum: Optional[Dict[str, Any]] = None    # the robot's datum now (map_geo shape)
    map_geo: Optional[Dict[str, Any]] = None        # the map's `geo` block (geo maps)
    map_type: Optional[str] = None

    @classmethod
    def from_row(cls, row: Tuple) -> "OpenSession":
        session_id, map_name, paused, transform, lifecycle, state, sdatum, rdatum = row[:8]
        mgeo, mtype = (row[8], row[9]) if len(row) >= 10 else (None, None)
        t = dict(map_geo.IDENTITY)
        t.update({k: float(v) for k, v in (transform or {}).items() if k in t})
        return cls(str(session_id), map_name, bool(paused), t, lifecycle, state or "ready",
                   sdatum or None, map_geo.robot_datum(rdatum or {}), mgeo or None, mtype)


def same_datum(a: Mapping[str, Any], b: Mapping[str, Any]) -> bool:
    """Two datums (map_geo.robot_datum shape) describe the same robot frame."""
    if geo.normalize_frame(a.get("frame")) != geo.normalize_frame(b.get("frame")):
        return False
    for key, tol in (("latitude", _DEG_TOL), ("longitude", _DEG_TOL), ("bearing_deg", _DEG_TOL),
                     ("utm_easting", _M_TOL), ("utm_northing", _M_TOL)):
        va, vb = a.get(key), b.get(key)
        if (va is None) != (vb is None) or (va is not None and abs(float(va) - float(vb)) > tol):
            return False
    return True


@dataclasses.dataclass(frozen=True)
class Resolution:
    """Where a robot's data goes: `session` when accepted, else `reason`."""
    robot_name: str
    session: Optional[OpenSession] = None
    reason: Optional[str] = None
    payload_session_id: Optional[str] = None

    @property
    def accepted(self) -> bool:
        return self.reason is None

    @property
    def map_name(self) -> Optional[str]:
        return self.session.map_name if self.session else None


def decide(robot_name: str, session: Optional[OpenSession],
           payload_session_id: Any = None) -> Resolution:
    """The ingest rule (pure): see the module docstring."""
    psid = str(payload_session_id) if payload_session_id not in (None, "") else None
    if session is None:
        return Resolution(robot_name, None, NO_SESSION, psid)
    if session.map_lifecycle is None:
        reason = MAP_MISSING
    elif session.map_lifecycle == "DELETING":
        reason = MAP_DELETING
    elif session.paused or session.map_state == "paused":
        reason = SESSION_PAUSED
    elif session.map_state not in ACCEPTING_STATES:
        reason = MAP_NOT_MAPPING
    elif psid is not None and psid != session.session_id:
        reason = SESSION_MISMATCH
    elif (session.session_datum is not None and session.robot_datum is not None
          and not same_datum(session.session_datum, session.robot_datum)):
        reason = DATUM_CHANGED
    else:
        reason = None
    return Resolution(robot_name, session, reason, psid)


@dataclasses.dataclass(frozen=True)
class Realign:
    """The new anchor of a geo session after the robot's datum changed."""
    datum: Dict[str, Any]
    map_t_session: Dict[str, float]


def plan_realign(session: OpenSession) -> Optional[Realign]:
    """How to re-anchor a geo session whose robot took a new datum, or None when it cannot be
    (then the ingest rejects with `datum_changed`): a geo session on a geo map with an origin,
    and a new datum that lies in the map's UTM zone and hemisphere. Pure."""
    old, new, geo_block = session.session_datum, session.robot_datum, session.map_geo
    if old is None or new is None or same_datum(old, new):
        return None
    if session.map_type == "local" or not geo_block:
        return None
    try:
        zone, north = int(geo_block["utm_zone"]), bool(geo_block["utm_north"])
        float(geo_block["origin_e"])
        float(geo_block["origin_n"])
        dzone, dnorth, _e, _n = map_geo.datum_utm(new)
        if dzone != zone or dnorth != north:
            return None
        transform = map_geo.session_transform(geo_block, new)
    except (KeyError, TypeError, ValueError):
        return None
    return Realign(dict(new), transform)


def realign_event(robot_name: str, session: OpenSession, plan: Realign,
                  ts: datetime.datetime) -> Event:
    """MAP.SESSION_REALIGNED for a won realignment."""
    t = plan.map_t_session
    return Event(EventCode.MAP_SESSION_REALIGNED, ts, robot_name=robot_name,
                 source=Source.GRAPH_BUILDER,
                 discriminator=(f"session:{session.session_id}:realigned:"
                                f"{t['tx']:.4f}:{t['ty']:.4f}:{t['yaw']:.6f}"),
                 payload={"map_name": session.map_name, "session_id": session.session_id,
                          "map_state": session.map_state, "aligned": True,
                          "map_T_session": dict(t),
                          "old_map_T_session": dict(session.map_t_session),
                          "datum": plan.datum, "old_datum": session.session_datum})


def realign_params(session: OpenSession, plan: Realign) -> Tuple[str, str, uuid_t.UUID, str]:
    """Parameters of REALIGN_SQL."""
    return (json.dumps(plan.datum), json.dumps(plan.map_t_session),
            uuid_t.UUID(session.session_id), json.dumps(session.session_datum))


def map_pose(transform: Mapping[str, float], x: float, y: float,
             yaw: float) -> Tuple[float, float, float]:
    """A robot-frame pose in the map frame: map_T_session applied, yaw wrapped to (-pi, pi]."""
    return map_geo.apply_pose(transform, x, y, yaw)


class SessionResolver:
    """The robot's open session, cached per robot for `ttl` seconds.

    `fetch(robot_name)` returns OPEN_SESSION_SQL's row or None (an async callable; the service
    passes a Postgres query, tests a fake). A failing fetch is not cached."""

    def __init__(self, fetch: Callable[[str], Any], ttl: float = SESSION_CACHE_TTL_S,
                 clock: Callable[[], float] = time.monotonic,
                 realign: Optional[Callable[..., Any]] = None):
        self._fetch = fetch
        # async (robot_name, session, Realign) -> bool: the compare-and-set + event; True = won.
        self._realign = realign
        self.ttl = ttl
        self._clock = clock
        self._cache: Dict[str, Tuple[float, Optional[OpenSession]]] = {}
        self.lookups = 0

    async def open_session(self, robot_name: str) -> Optional[OpenSession]:
        now = self._clock()
        hit = self._cache.get(robot_name)
        if hit is not None and now < hit[0]:
            return hit[1]
        self.lookups += 1
        row = await self._fetch(robot_name)
        session = OpenSession.from_row(row) if row is not None else None
        self._cache[robot_name] = (now + self.ttl, session)
        return session

    async def resolve(self, robot_name: str, payload_session_id: Any = None) -> Resolution:
        try:
            session = await self.open_session(robot_name)
        except Exception as exc:  # noqa: BLE001 - Postgres down: drop, count, keep going
            logger.warning("Session lookup for %s failed: %s", robot_name, exc)
            psid = str(payload_session_id) if payload_session_id not in (None, "") else None
            return Resolution(robot_name, None, LOOKUP_FAILED, psid)
        resolution = decide(robot_name, session, payload_session_id)
        if resolution.reason == DATUM_CHANGED and self._realign is not None:
            resolution = await self._try_realign(robot_name, session, payload_session_id,
                                                 resolution)
        return resolution

    async def _try_realign(self, robot_name: str, session: OpenSession,
                           payload_session_id: Any, rejected: Resolution) -> Resolution:
        """Re-anchor a geo session after a datum change (module docstring), then decide again on
        the session as stored. Losing the compare-and-set to a concurrent ingest, or any failure,
        re-reads (or keeps) the state as it is; still-unusable means the rejection stands."""
        plan = plan_realign(session)
        if plan is None:
            return rejected
        try:
            won = await self._realign(robot_name, session, plan)
            if won:
                logger.info("Realigned session %s of %s to its new datum (map_T_session %s)",
                            session.session_id, robot_name, plan.map_t_session)
            self.invalidate(robot_name)
            fresh = await self.open_session(robot_name)
        except Exception as exc:  # noqa: BLE001 - Postgres down: keep the rejection
            logger.warning("Realigning %s failed: %s", robot_name, exc)
            self.invalidate(robot_name)
            return rejected
        return decide(robot_name, fresh, payload_session_id)

    def invalidate(self, robot_name: Optional[str] = None) -> None:
        if robot_name is None:
            self._cache.clear()
        else:
            self._cache.pop(robot_name, None)


@dataclasses.dataclass
class _Pending:
    since: datetime.datetime
    nodes: int = 0
    images: int = 0
    map_name: Optional[str] = None
    map_state: Optional[str] = None
    session_id: Optional[str] = None
    payload_session_id: Optional[str] = None


class RejectLimiter:
    """Counts drops per (robot, reason) and says when to report them: the first drop at once,
    later ones at most every `interval` seconds, each report carrying every drop since the
    previous one."""

    def __init__(self, interval: float = REJECT_EVENT_INTERVAL_S,
                 clock: Callable[[], float] = time.monotonic,
                 wall: Callable[[], datetime.datetime] = lambda: datetime.datetime.now(
                     datetime.timezone.utc)):
        self.interval = interval
        self._clock = clock
        self._wall = wall
        self._pending: Dict[Tuple[str, str], _Pending] = {}
        self._last_report: Dict[Tuple[str, str], float] = {}
        self.dropped: Dict[str, int] = {"nodes": 0, "images": 0}

    def record(self, resolution: Resolution, kind: str) -> Optional[Event]:
        """Count one dropped `kind` ('node' | 'image'); the event to write now, if any."""
        key = (resolution.robot_name, resolution.reason or LOOKUP_FAILED)
        pending = self._pending.get(key)
        if pending is None:
            pending = self._pending[key] = _Pending(since=self._wall())
        if kind == "node":
            pending.nodes += 1
            self.dropped["nodes"] += 1
        else:
            pending.images += 1
            self.dropped["images"] += 1
        session = resolution.session
        pending.map_name = session.map_name if session else None
        pending.map_state = session.map_state if session else None
        pending.session_id = session.session_id if session else None
        pending.payload_session_id = resolution.payload_session_id
        last = self._last_report.get(key)
        if last is None or self._clock() - last >= self.interval:
            return self._report(key)
        return None

    def due(self) -> List[Event]:
        """Reports for drops still pending once their interval has passed (periodic flush)."""
        now = self._clock()
        return [self._report(key) for key in list(self._pending)
                if now - self._last_report.get(key, float("-inf")) >= self.interval]

    def _report(self, key: Tuple[str, str]) -> Event:
        robot_name, reason = key
        pending = self._pending.pop(key)
        self._last_report[key] = self._clock()
        ts = self._wall()
        return Event(EventCode.MAP_INGEST_REJECTED, ts, robot_name=robot_name,
                     source=Source.GRAPH_BUILDER, discriminator=f"ingest:{reason}",
                     payload={"reason": reason, "dropped_nodes": pending.nodes,
                              "dropped_images": pending.images,
                              "since": pending.since.isoformat(),
                              "map_name": pending.map_name, "map_state": pending.map_state,
                              "session_id": pending.session_id,
                              "payload_session_id": pending.payload_session_id})
