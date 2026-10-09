"""Ingest into the robot's open session's map (docs/satinav-maps-redesign.md §6, §14).

graph-builder no longer writes to the robot's old `current_map` (removed in U6) or a
`"default"` map. The topomap runs on a robot while it has an open mapping session (the API starts
and stops it through the robot's orchestrator, packages/api/mapping_switch.py; it may also be
started by hand there). Every node and image from that robot goes to the map of the robot's
**open session** (`map_sessions`, one per robot at most) using that session's map_T_session,
whatever state the map is in, when that session is an unpaused MAPPING session (an operate session
adds no data, decision B 2026-10-09). A `session_id` in the payload is not checked (the robot does
not know cloud sessions; a different one is only logged at debug). Otherwise it is dropped:

    reason            when
    no_session        the robot has no open session (no current map)
    map_deleting      the map is being deleted (object lifecycle DELETING)
    map_missing       the session names a map without a Postgres row
    operate_session   its session is an operate session (the robot uses the map, adds nothing)
    session_paused    its mapping session is paused (a node still in flight after the pause)
    session_unplaced  its session is not placed (maps §14: no transform yet, until the robot is
                      placed on the map; any session after the robot's run frame reset, until it
                      is placed again)
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
Only a placed session is realigned here; one that a run change unplaced (maps §14 U3) is
re-placed by mission-dispatch when the robot's new datum arrives, and until then its nodes are
rejected (`session_unplaced`) instead of being placed with the old transform.

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

Depth (3D reconstruction R2, docs/reconstruction/design.md §5): a `robot/depth_upload` message
is resolved like an image and dropped with kind `depth` (`dropped_depth`). Its camera
parameters go onto the ArangoDB node as `depth.{camera}` (`depth_record`), with the robot's
full 6-DoF pose at the depth stamp converted into the map frame (`pose3d_map`: x, y and yaw
change with map_T_session; z, roll and pitch do not).

Costmap (`robot/costmap_upload`): one occupancy PNG (u8, 0..100 occupied, 255 unknown) per node
and layer, resolved like depth and dropped with kind `costmap` (`dropped_costmap`). Its record
goes onto the node as `costmap.{layer}` (`costmap_record`), with the grid origin converted into
the map frame (`origin_map`, and `origin_pose3d_map` when the robot sent `origin_pose3d`).
"""

import dataclasses
import datetime
import json
import logging
import math
import time
import uuid as uuid_t
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from packages.events.codes import EventCode, Source
from packages.events.emit import Event
from packages.utils import map_geo, map_sessions

logger = logging.getLogger("GraphBuilderService.ingest")

SESSION_CACHE_TTL_S = 1.0
REJECT_EVENT_INTERVAL_S = 60.0

NO_SESSION = "no_session"
SESSION_UNPLACED = "session_unplaced"
OPERATE_SESSION = "operate_session"
SESSION_PAUSED = "session_paused"
MAP_DELETING = "map_deleting"
MAP_MISSING = "map_missing"
DATUM_CHANGED = "datum_changed"
LOOKUP_FAILED = "lookup_failed"
REASONS = (NO_SESSION, MAP_DELETING, MAP_MISSING, OPERATE_SESSION, SESSION_PAUSED,
           SESSION_UNPLACED, DATUM_CHANGED, LOOKUP_FAILED)

# One row per robot at most (partial unique index map_sessions_one_open_per_robot).
OPEN_SESSION_SQL = (
    "SELECT s.session_id, s.map_name, s.paused_at IS NOT NULL, s.map_t_session, "
    "m.lifecycle, m.status->>'state', s.datum, r.spec->'datum', "
    "m.spec->'geo', m.spec->>'type', s.purpose, s.aligned "
    "FROM map_sessions s LEFT JOIN mapobjectv1 m "
    "ON m.name = s.map_name AND m.lifecycle <> 'DELETED' "
    "LEFT JOIN robotobjectv1 r ON r.name = s.robot_name AND r.lifecycle <> 'DELETED' "
    "WHERE s.robot_name = %s AND s.ended_at IS NULL")
# Compare-and-set on the datum read: only the ingest that saw the old datum wins (rowcount 1).
# Only a placed session (maps §14): an unplaced one waits for mission-dispatch's re-placement.
REALIGN_SQL = ("UPDATE map_sessions SET datum = %s::jsonb, map_t_session = %s::jsonb "
               "WHERE session_id = %s AND ended_at IS NULL AND aligned AND datum = %s::jsonb")
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
    purpose: str = map_sessions.MAPPING
    aligned: bool = True  # placed (maps §14)

    @classmethod
    def from_row(cls, row: Tuple) -> "OpenSession":
        session_id, map_name, paused, transform, lifecycle, state, sdatum, rdatum = row[:8]
        mgeo, mtype = (row[8], row[9]) if len(row) >= 10 else (None, None)
        purpose, aligned = (row[10], row[11]) if len(row) >= 12 else (None, True)
        t = dict(map_geo.IDENTITY)
        t.update({k: float(v) for k, v in (transform or {}).items() if k in t})
        return cls(str(session_id), map_name, bool(paused), t, lifecycle, state or "ready",
                   sdatum or None, map_geo.robot_datum(rdatum or {}), mgeo or None, mtype,
                   purpose or map_sessions.MAPPING, aligned is not False)


same_datum = map_sessions.same_datum


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
    """The ingest rule (pure): see the module docstring. Only an unpaused mapping session
    takes data; the map's state and a differing payload session_id do not reject."""
    psid = str(payload_session_id) if payload_session_id not in (None, "") else None
    if session is None:
        return Resolution(robot_name, None, NO_SESSION, psid)
    if session.map_lifecycle is None:
        reason = MAP_MISSING
    elif session.map_lifecycle == "DELETING":
        reason = MAP_DELETING
    elif session.purpose == map_sessions.OPERATE:
        reason = OPERATE_SESSION
    elif session.paused:
        reason = SESSION_PAUSED
    elif not session.aligned:
        reason = SESSION_UNPLACED
    elif (session.session_datum is not None and session.robot_datum is not None
          and not same_datum(session.session_datum, session.robot_datum)):
        reason = DATUM_CHANGED
    else:
        reason = None
    if reason is None and psid is not None and psid != session.session_id:
        logger.debug("Payload session_id %s of %s differs from its open session %s: accepted",
                     psid, robot_name, session.session_id)
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
    if old is None or new is None or same_datum(old, new) or not session.aligned:
        return None
    transform = map_sessions.geo_transform_for(geo_block, session.map_type, new)
    if transform is None:
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


# --- depth (3D reconstruction R2) --------------------------------------------------------------

DEPTH_ENCODING = "u16_mm"
DEPTH_CONTENT_TYPE = "image/png"
POSE3D_KEYS = ("x", "y", "z", "qx", "qy", "qz", "qw")


class DepthPayloadError(ValueError):
    """A robot/depth_upload message that cannot be stored (missing or malformed fields)."""


def pose3d_map(transform: Mapping[str, float], pose3d: Mapping[str, Any]) -> Dict[str, float]:
    """A robot-frame 6-DoF pose {x, y, z, qx, qy, qz, qw} in the map frame.

    map_T_session is a rotation about z by `yaw` plus an x/y translation, so the position's
    x/y are rotated and shifted, z is kept, and the orientation is Rz(yaw) * q (Hamilton): the
    heading turns by `yaw`, roll and pitch are unchanged. The quaternion is normalized."""
    x, y = map_geo.apply_transform(transform, float(pose3d["x"]), float(pose3d["y"]))
    qx, qy, qz, qw = (float(pose3d[k]) for k in ("qx", "qy", "qz", "qw"))
    n = math.sqrt(qx * qx + qy * qy + qz * qz + qw * qw)
    if not n or not math.isfinite(n):
        raise DepthPayloadError("robot_pose3d has a zero or invalid quaternion")
    qx, qy, qz, qw = qx / n, qy / n, qz / n, qw / n
    h = float(transform.get("yaw", 0.0)) / 2.0
    cz, sz = math.cos(h), math.sin(h)  # Rz(yaw) as a quaternion: (0, 0, sz, cz)
    return {"x": x, "y": y, "z": float(pose3d["z"]),
            "qx": cz * qx - sz * qy,
            "qy": cz * qy + sz * qx,
            "qz": cz * qz + sz * qw,
            "qw": cz * qw - sz * qz}


def _pose3d(value: Any) -> Optional[Dict[str, float]]:
    if value in (None, {}):
        return None
    if not isinstance(value, Mapping) or any(k not in value for k in POSE3D_KEYS):
        raise DepthPayloadError(f"robot_pose3d needs {', '.join(POSE3D_KEYS)}")
    try:
        pose = {k: float(value[k]) for k in POSE3D_KEYS}
    except (TypeError, ValueError) as exc:
        raise DepthPayloadError(f"robot_pose3d: {exc}") from exc
    if not all(math.isfinite(v) for v in pose.values()):
        raise DepthPayloadError("robot_pose3d has a non-finite value")
    return pose


def check_depth_payload(payload: Mapping[str, Any]) -> None:
    """Raise DepthPayloadError unless `payload` is a storable robot/depth_upload message
    (design.md §4.3). Cheap: the PNG itself is not decoded."""
    for key in ("session_node_id", "robot_name", "camera_name", "depth_data"):
        if payload.get(key) in (None, ""):
            raise DepthPayloadError(f"missing {key}")
    camera = str(payload["camera_name"])
    if "/" in camera or camera in (".", ".."):
        raise DepthPayloadError(f"invalid camera_name {camera!r}")
    if not isinstance(payload.get("camera"), Mapping):
        raise DepthPayloadError("missing camera block")
    encoding = payload.get("depth_encoding", DEPTH_ENCODING)
    if encoding != DEPTH_ENCODING:
        raise DepthPayloadError(f"unsupported depth_encoding {encoding!r}")
    content_type = payload.get("content_type", DEPTH_CONTENT_TYPE)
    if content_type != DEPTH_CONTENT_TYPE:
        raise DepthPayloadError(f"unsupported content_type {content_type!r}")
    try:
        scale = float(payload.get("depth_scale", 0.001))
    except (TypeError, ValueError) as exc:
        raise DepthPayloadError(f"depth_scale: {exc}") from exc
    if not (scale > 0 and math.isfinite(scale)):
        raise DepthPayloadError("depth_scale must be > 0")
    _pose3d(payload.get("robot_pose3d"))


def depth_record(payload: Mapping[str, Any], session: "OpenSession") -> Dict[str, Any]:
    """The node's `depth.{camera}` value (design.md §5) for an accepted depth message:
    the camera block as sent, the scale and stamps, the session, and (when the robot sent
    `robot_pose3d`) that pose plus `pose3d_map`, converted with the session's map_T_session."""
    check_depth_payload(payload)
    record: Dict[str, Any] = {
        "camera": dict(payload["camera"]),
        "depth_scale": float(payload.get("depth_scale", 0.001)),
        "depth_encoding": DEPTH_ENCODING,
        "depth_stamp_ms": payload.get("depth_stamp_ms"),
        "rgb_stamp_ms": payload.get("rgb_stamp_ms"),
        "session_id": session.session_id,
    }
    pose = _pose3d(payload.get("robot_pose3d"))
    if pose is not None:
        record["robot_pose3d"] = pose
        record["pose3d_map"] = pose3d_map(session.map_t_session, pose)
    return record


# --- costmap (`robot/costmap_upload`) ----------------------------------------------------------

COSTMAP_ENCODING = "u8_occ100_unknown255"
COSTMAP_CONTENT_TYPE = "image/png"
COSTMAP_DATA_KEY = "costmap_data"  # the only payload field not stored on the node (PNG: MinIO)


class CostmapPayloadError(ValueError):
    """A robot/costmap_upload message that cannot be stored (missing or malformed fields)."""


def _finite(payload: Mapping[str, Any], name: str) -> float:
    try:
        value = float(payload[name])
    except (KeyError, TypeError, ValueError) as exc:
        raise CostmapPayloadError(f"{name}: {exc}") from exc
    if not math.isfinite(value):
        raise CostmapPayloadError(f"{name} is not finite")
    return value


def _positive_int(payload: Mapping[str, Any], name: str) -> int:
    value = payload.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise CostmapPayloadError(f"{name} must be a positive integer")
    return value


def _costmap_origin(payload: Mapping[str, Any]) -> Dict[str, float]:
    origin = payload.get("origin")
    if not isinstance(origin, Mapping):
        raise CostmapPayloadError("missing origin")
    return {k: _finite(origin, k) for k in ("x", "y", "yaw")}


def _costmap_pose3d(value: Any) -> Optional[Dict[str, float]]:
    if value in (None, {}):
        return None
    try:
        pose = _pose3d(value)
    except DepthPayloadError as exc:
        raise CostmapPayloadError(str(exc).replace("robot_pose3d", "origin_pose3d")) from exc
    if not math.sqrt(sum(pose[k] ** 2 for k in ("qx", "qy", "qz", "qw"))):
        raise CostmapPayloadError("origin_pose3d has a zero quaternion")
    return pose


def check_costmap_payload(payload: Mapping[str, Any]) -> None:
    """Raise CostmapPayloadError unless `payload` is a storable robot/costmap_upload message.
    Cheap: the PNG is neither decoded nor base64-validated here (that happens on save)."""
    for key in ("session_node_id", "robot_name", "layer", COSTMAP_DATA_KEY):
        if payload.get(key) in (None, ""):
            raise CostmapPayloadError(f"missing {key}")
    layer = str(payload["layer"])
    if "/" in layer or layer in (".", ".."):
        raise CostmapPayloadError(f"invalid layer {layer!r}")
    encoding = payload.get("costmap_encoding", COSTMAP_ENCODING)
    if encoding != COSTMAP_ENCODING:
        raise CostmapPayloadError(f"unsupported costmap_encoding {encoding!r}")
    content_type = payload.get("content_type", COSTMAP_CONTENT_TYPE)
    if content_type != COSTMAP_CONTENT_TYPE:
        raise CostmapPayloadError(f"unsupported content_type {content_type!r}")
    _positive_int(payload, "width")
    _positive_int(payload, "height")
    if not _finite(payload, "resolution") > 0:
        raise CostmapPayloadError("resolution must be > 0")
    _costmap_origin(payload)
    _costmap_pose3d(payload.get("origin_pose3d"))


def costmap_record(payload: Mapping[str, Any], session: "OpenSession") -> Dict[str, Any]:
    """The node's `costmap.{layer}` value for an accepted costmap message: every field but the
    PNG as sent, `origin_map` (the grid origin through the session's map_T_session, exactly as a
    node pose), `origin_pose3d_map` when `origin_pose3d` was sent, and `session_id`."""
    check_costmap_payload(payload)
    record: Dict[str, Any] = {k: v for k, v in payload.items() if k != COSTMAP_DATA_KEY}
    record["layer"] = str(payload["layer"])
    record["costmap_encoding"] = COSTMAP_ENCODING
    record["content_type"] = COSTMAP_CONTENT_TYPE
    origin = _costmap_origin(payload)
    x, y, yaw = map_pose(session.map_t_session, origin["x"], origin["y"], origin["yaw"])
    record["origin_map"] = {"x": x, "y": y, "yaw": yaw}
    pose = _costmap_pose3d(payload.get("origin_pose3d"))
    if pose is not None:
        record["origin_pose3d"] = pose
        record["origin_pose3d_map"] = pose3d_map(session.map_t_session, pose)
    record["session_id"] = session.session_id
    return record


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
    depth: int = 0
    costmap: int = 0
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
        self.dropped: Dict[str, int] = {"nodes": 0, "images": 0, "depth": 0,
                                 "costmap": 0}

    def record(self, resolution: Resolution, kind: str) -> Optional[Event]:
        """Count one dropped `kind` ('node' | 'image' | 'depth' | 'costmap'); the event to write now, if
        any."""
        key = (resolution.robot_name, resolution.reason or LOOKUP_FAILED)
        pending = self._pending.get(key)
        if pending is None:
            pending = self._pending[key] = _Pending(since=self._wall())
        if kind == "node":
            pending.nodes += 1
            self.dropped["nodes"] += 1
        elif kind == "depth":
            pending.depth += 1
            self.dropped["depth"] += 1
        elif kind == "costmap":
            pending.costmap += 1
            self.dropped["costmap"] += 1
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
                              "dropped_depth": pending.depth,
                              "dropped_costmap": pending.costmap,
                              "since": pending.since.isoformat(),
                              "map_name": pending.map_name, "map_state": pending.map_state,
                              "session_id": pending.session_id,
                              "payload_session_id": pending.payload_session_id})
