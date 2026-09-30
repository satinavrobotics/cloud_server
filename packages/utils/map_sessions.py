"""Open sessions: purpose and placement (docs/satinav-maps-redesign.md §14).

A robot uses a map through its open session (`map_sessions`, one per robot at most). The
session's `purpose` is `mapping` (the robot adds data to the map) or `operate` (it uses the map
for missions and display and adds nothing). Both carry `map_t_session` (`map_T_session`):
where the robot's CURRENT run frame sits in the map frame. `aligned` means "map_T_session is
valid for the robot's current run"; the UI calls it *placed*.

Where map_T_session comes from:

- geo map: the robot's datum (map_geo.session_transform), re-derived when the datum changes
  (graph-builder and, since U3, the dispatcher on the datum write);
- local map: placement. The user puts the robot on the map (pose in the map frame) while the
  robot reports its own pose: map_T_session = P_map (+) P_robot^-1 (placement_transform);
- the first mapping session of an empty local map: identity (the run defines the map frame).

A session that is not placed keeps no nodes, gets no route orders and no planned paths.

Pure functions plus the SQL that several services share (the API, mission-dispatch,
mission-planner, graph-builder); no I/O here. (The robot's mapping services are switched by the
API through the robot's orchestrator: packages/api/mapping_switch.py.)
"""

import datetime
import math
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from packages.utils import geo, map_geo

MAPPING, OPERATE = "mapping", "operate"
PURPOSES = (MAPPING, OPERATE)
TOPO, GRID = "topo", "grid"
# Mapping services a session can switch on (§14.5). `grid` is reserved for sati_grid_mapping,
# which does not exist yet: accepted, and robots that do not run it ignore it.
KNOWN_SERVICES = (TOPO, GRID)
DEFAULT_SERVICES = (TOPO,)

# The robot must stand still while it is placed (decision Q-U7). These are sensor noise, not a
# movement allowance: the pose shown to the user and the pose at confirm may differ by this much.
PLACE_POSITION_TOL_M = 0.02
PLACE_YAW_TOL_RAD = math.radians(0.5)
# A velocity below this counts as standing (odometry noise on a robot that does not move).
STILL_LINEAR_MPS = 0.01
STILL_ANGULAR_RADPS = 0.01

# Placement sources (session.placement.source).
SOURCE_USER = "user"          # POST .../place or `placement` on start
SOURCE_SESSION = "session"    # carried over from the robot's previous session (replace, same run)
SOURCE_DATUM = "datum"        # a geo session re-placed from the robot's datum (U3)
UNPLACED_RUN_CHANGED = "run_changed"

SESSION_COLUMNS = ("session_id", "map_name", "robot_name", "kind", "purpose", "services",
                   "placement", "started_at", "paused_at", "ended_at", "datum",
                   "map_t_session", "aligned", "node_count", "run_epoch")

# The robot's open session with its map (one row at most: map_sessions_one_open_per_robot).
ROBOT_SESSION_SQL = (
    "SELECT s.session_id, s.map_name, s.purpose, s.aligned, s.map_t_session, s.datum, "
    "s.placement, s.paused_at, m.spec->'geo', m.spec->>'type', s.services "
    "FROM map_sessions s LEFT JOIN mapobjectv1 m "
    "ON m.name = s.map_name AND m.lifecycle <> 'DELETED' "
    "WHERE s.robot_name = %s AND s.ended_at IS NULL")
ROBOT_SESSION_KEYS = ("session_id", "map_name", "purpose", "aligned", "map_t_session", "datum",
                      "placement", "paused_at", "map_geo", "map_type", "services")


def robot_session_from_row(row: Optional[Tuple]) -> Optional[Dict[str, Any]]:
    """One ROBOT_SESSION_SQL row as a dict (None stays None)."""
    if row is None:
        return None
    s = dict(zip(ROBOT_SESSION_KEYS, row))
    s["session_id"] = str(s["session_id"])
    s["purpose"] = s.get("purpose") or MAPPING
    s["map_t_session"] = transform_of(s.get("map_t_session"))
    return s


def transform_of(value: Any) -> Dict[str, float]:
    t = dict(map_geo.IDENTITY)
    t.update({k: float(v) for k, v in (value or {}).items() if k in t and v is not None})
    return t


def purpose_of(session: Mapping[str, Any]) -> str:
    return session.get("purpose") or MAPPING


def is_placed(session: Mapping[str, Any]) -> bool:
    return session.get("aligned") is True


# --- placement math ------------------------------------------------------------------------------

def compose(a: Mapping[str, float], b: Mapping[str, float]) -> Dict[str, float]:
    """a (+) b: first b, then a (both {tx, ty, yaw})."""
    x, y = map_geo.apply_transform(a, float(b["tx"]), float(b["ty"]))
    return {"tx": x, "ty": y, "yaw": map_geo.normalize_yaw(float(a["yaw"]) + float(b["yaw"]))}


def placement_transform(pose: Mapping[str, Any], robot_pose: Mapping[str, Any]
                        ) -> Dict[str, float]:
    """map_T_session = P_map (+) P_robot^-1: the robot's own pose `robot_pose` {x, y, theta}
    (its run frame) lands exactly on the placed pose `pose` {x, y, yaw} (map frame)."""
    p_map = {"tx": float(pose["x"]), "ty": float(pose["y"]), "yaw": float(pose["yaw"])}
    p_robot = {"tx": float(robot_pose["x"]), "ty": float(robot_pose["y"]),
               "yaw": float(robot_pose["theta"])}
    return compose(p_map, map_geo.invert_transform(p_robot))


def yaw_difference(a: float, b: float) -> float:
    return abs(map_geo.normalize_yaw(float(a) - float(b)))


def pose_moved(shown: Mapping[str, Any], now: Mapping[str, Any],
               tol_m: float = PLACE_POSITION_TOL_M, tol_yaw: float = PLACE_YAW_TOL_RAD
               ) -> Optional[str]:
    """Why the robot's pose `now` {x, y, theta} is not the pose the user saw (`shown`), or None
    when they agree within sensor noise."""
    d = math.hypot(float(now["x"]) - float(shown["x"]), float(now["y"]) - float(shown["y"]))
    dyaw = yaw_difference(now["theta"], shown["theta"])
    if d > tol_m:
        return f"it moved {d:.3f} m since its pose was shown (noise {tol_m} m)"
    if dyaw > tol_yaw:
        return (f"it turned {math.degrees(dyaw):.2f} deg since its pose was shown "
                f"(noise {math.degrees(tol_yaw):.1f} deg)")
    return None


def driving_reason(robot_state: Optional[str], state_msg: Optional[Mapping[str, Any]],
                   mission_open: Optional[bool] = None) -> Optional[str]:
    """Why the robot counts as driving (decision Q-U7: it must stand still while placed), or
    None. `robot_state`: RobotStatusV1.state (ON_TASK / MAP_DEPLOYMENT = an active order);
    `state_msg`: the robot's last VDA5050 state message (robot_latest.state_msg) or None;
    `mission_open`: whether the robot has a PENDING or RUNNING mission (None = unknown).

    ON_TASK is mission-dispatch's summary of "I am running a mission for this robot". When the
    robot has no open mission at all, it cannot be executing one of our orders: the state is
    stale and the robot's own fresh state message decides (driving, velocity, remaining
    nodeStates). Without a fresh state message the stored state still refuses."""
    if robot_state in ("ON_TASK", "MAP_DEPLOYMENT") and not (
            mission_open is False and state_msg is not None):
        return f"it has an active order (state {robot_state})"
    msg = state_msg or {}
    if msg.get("driving") is True:
        return "it reports driving"
    vel = msg.get("velocity") or {}
    try:
        vx, vy = float(vel.get("vx") or 0.0), float(vel.get("vy") or 0.0)
        omega = float(vel.get("omega") or 0.0)
    except (TypeError, ValueError):
        vx = vy = omega = 0.0
    if math.hypot(vx, vy) > STILL_LINEAR_MPS or abs(omega) > STILL_ANGULAR_RADPS:
        return f"its velocity is not zero (vx {vx:.3f}, vy {vy:.3f}, omega {omega:.3f})"
    if msg.get("nodeStates"):
        return "it still has order nodes to drive"
    return None


# --- datum and geo re-placement ------------------------------------------------------------------

# Datum comparison tolerances: the datum is a fixed per-run origin, re-sent unchanged.
_DEG_TOL = 1e-9
_M_TOL = 1e-4


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


def geo_transform_for(map_geo_block: Optional[Mapping[str, Any]], map_type: Optional[str],
                      datum: Optional[Mapping[str, Any]]) -> Optional[Dict[str, float]]:
    """map_T_session of a geo map for a robot datum, or None when the datum cannot place the
    robot on it: a local map, a geo map without an origin, no datum, or a datum in another UTM
    zone / hemisphere than the map."""
    if map_type == "local" or not map_geo_block or datum is None:
        return None
    try:
        zone, north = int(map_geo_block["utm_zone"]), bool(map_geo_block["utm_north"])
        float(map_geo_block["origin_e"])
        float(map_geo_block["origin_n"])
        dzone, dnorth, _e, _n = map_geo.datum_utm(datum)
        if dzone != zone or dnorth != north:
            return None
        return map_geo.session_transform(map_geo_block, datum)
    except (KeyError, TypeError, ValueError):
        return None


def plan_geo_replace(session: Optional[Mapping[str, Any]], datum: Optional[Mapping[str, Any]],
                     trust_same_datum: bool) -> Optional[Tuple[Dict[str, float], str]]:
    """What a robot datum message does to the robot's open session (maps §14 U3, the
    dispatcher on the datum write): (map_T_session, reason) to store (placed), or None.

    - a geo session a run change unplaced: re-placed from this datum (reason `run_changed`).
      The same datum as the session's counts only when `trust_same_datum` (the message is not
      a retained re-delivery at the dispatcher's (re)subscribe, which may be the OLD run's);
    - a placed geo session whose datum changed: re-derived (reason `datum`, §13.4);
    - anything else (no session, a local map, a datum that cannot place the robot on the map:
      another UTM zone, no origin): None.
    `session`: ROBOT_SESSION_SQL's dict; `datum`: map_geo.robot_datum() shape."""
    if session is None or datum is None:
        return None
    transform = geo_transform_for(session.get("map_geo"), session.get("map_type"), datum)
    if transform is None:
        return None
    old = session.get("datum")
    changed = old is None or not same_datum(old, datum)
    if not is_placed(session):
        if changed or trust_same_datum:
            return transform, UNPLACED_RUN_CHANGED
        return None
    if changed:
        return transform, "datum"
    return None


# SQL of the dispatcher's session writes (maps §14 U3).
UNPLACE_SQL = (
    "UPDATE map_sessions SET aligned = false, "
    "placement = COALESCE(placement, '{}'::jsonb) || %s::jsonb "
    "WHERE robot_name = %s AND ended_at IS NULL AND aligned "
    "RETURNING session_id, map_name, purpose, map_t_session")
# Compare-and-set, like graph-builder's REALIGN_SQL: only the writer that read this state wins.
REPLACE_SQL = (
    "UPDATE map_sessions SET datum = %s::jsonb, map_t_session = %s::jsonb, aligned = true, "
    "placement = %s::jsonb "
    "WHERE session_id = %s AND ended_at IS NULL AND aligned = %s "
    "AND datum IS NOT DISTINCT FROM %s::jsonb")


# --- the robot's run epoch: placement reuse across sessions (§14.13) -------------------------------
#
# robot_run_epochs (migration 20261001_01_run_epochs), one row per robot, written only by
# mission-dispatch: `epoch` is a fresh uuid at every run change it detects and whenever it cannot
# prove that the run it sees continues the one it saw before (first sight of the robot, a
# dispatcher restart without the proof below). `continuity_known` is false from a dispatcher start
# until the robot's first state message decided (proved: same epoch; else: a new one). A finished
# session stamps the epoch it was placed in (`map_sessions.run_epoch`, at finish, only while
# placed and continuity_known); a new session on the same local map without a placement carries
# that session's map_T_session when the robot's epoch is still the same (reusable_session).

# At most this many state messages per second from one VDA5050 client (sati_vda5050_client sends
# about 1/s plus event-driven ones). Used only to PROVE continuity across a dispatcher restart:
# a new client process started after the last header id we stored cannot have sent more than
# elapsed * this many messages, so a header id at or above that (and above the stored one) is the
# old process. Too high only costs a missed reuse (the user places again), never a wrong one.
MAX_STATE_RATE_HZ = 20.0
# How often the dispatcher stores a robot's last state header id (the baseline of that proof).
RUN_HEADER_PERSIST_S = 10.0
RUN_EPOCH_TABLE = "robot_run_epochs"
REASON_RUN_CHANGED, REASON_FIRST_SEEN, REASON_DISPATCHER_RESTART = (
    "run_changed", "first_seen", "dispatcher_restart")

RUN_EPOCH_UNVERIFY_ALL_SQL = (
    f"UPDATE {RUN_EPOCH_TABLE} SET continuity_known = false, updated_at = now() "
    "WHERE continuity_known")
RUN_EPOCH_READ_SQL = (
    f"SELECT epoch, continuity_known, last_state_header, "
    "EXTRACT(EPOCH FROM (now() - last_state_at))::float8 "
    f"FROM {RUN_EPOCH_TABLE} WHERE robot_name = %s")
RUN_EPOCH_CONFIRM_SQL = (
    f"UPDATE {RUN_EPOCH_TABLE} SET continuity_known = true, last_state_header = %s, "
    "last_state_at = now(), updated_at = now() WHERE robot_name = %s AND epoch = %s")
# (robot_name, epoch, reason, evidence json, last_state_header or NULL)
RUN_EPOCH_NEW_SQL = (
    f"INSERT INTO {RUN_EPOCH_TABLE} (robot_name, epoch, started_at, reason, evidence, "
    "continuity_known, last_state_header, last_state_at, updated_at) "
    "VALUES (%s, %s, now(), %s, %s::jsonb, true, %s, now(), now()) "
    "ON CONFLICT (robot_name) DO UPDATE SET epoch = EXCLUDED.epoch, "
    "started_at = EXCLUDED.started_at, reason = EXCLUDED.reason, evidence = EXCLUDED.evidence, "
    "continuity_known = true, last_state_header = EXCLUDED.last_state_header, "
    "last_state_at = EXCLUDED.last_state_at, updated_at = EXCLUDED.updated_at")
RUN_EPOCH_HEADER_SQL = (
    f"UPDATE {RUN_EPOCH_TABLE} SET last_state_header = %s, last_state_at = now() "
    "WHERE robot_name = %s AND epoch = %s AND continuity_known")
# The API: the robot's epoch, for stamping a finished session and for reuse.
RUN_EPOCH_OF_SQL = f"SELECT epoch, continuity_known FROM {RUN_EPOCH_TABLE} WHERE robot_name = %s"


def run_continues(stored_header: Any, elapsed_s: Any, header: Any,
                  max_rate_hz: float = MAX_STATE_RATE_HZ) -> bool:
    """Whether a robot's first state message after a dispatcher (re)start (headerId `header`)
    PROVABLY comes from the same VDA5050 client process as the last one the dispatcher stored
    (`stored_header`, `elapsed_s` seconds ago). The client numbers its state messages from 0 per
    process; a process started after the stored message has sent at most elapsed * max_rate
    since. So: `header` above the stored one AND at least elapsed * max_rate. Anything unknown
    or unparsable is not a proof."""
    try:
        stored, elapsed, hid = int(stored_header), float(elapsed_s), int(header)
    except (TypeError, ValueError):
        return False
    if elapsed < 0 or not math.isfinite(elapsed):
        return False
    return hid > stored and hid >= elapsed * max_rate_hz


def epoch_to_stamp(session: Mapping[str, Any], epoch_row: Optional[Tuple[Any, Any]]
                   ) -> Optional[str]:
    """The run epoch a session that is finishing records (map_sessions.run_epoch): the robot's
    current epoch while the session is placed and the dispatcher knows the run is continuous;
    else None (the session can never be reused)."""
    if not is_placed(session) or epoch_row is None:
        return None
    epoch, known = epoch_row
    return str(epoch) if epoch is not None and known is True else None


def reusable_session(sessions: Iterable[Mapping[str, Any]], robot_name: str,
                     epoch_row: Optional[Tuple[Any, Any]]) -> Optional[Mapping[str, Any]]:
    """The finished session of `robot_name` whose placement a new session on the same LOCAL map
    reuses (§14.13), or None. `sessions`: that map's sessions (any robot); `epoch_row`: the
    robot's (epoch, continuity_known). Only the robot's most recent finished session on the
    map counts, and only if it ended placed (a run change unplaces, so an unplaced end never
    counts), stamped an epoch, and that epoch is still the robot's, with continuity known."""
    if epoch_row is None:
        return None
    epoch, known = epoch_row
    if epoch is None or known is not True:
        return None
    mine = [s for s in sessions
            if s.get("robot_name") == robot_name and s.get("ended_at") is not None]
    if not mine:
        return None
    last = max(mine, key=lambda s: (s["ended_at"], str(s.get("session_id"))))
    if not is_placed(last) or last.get("run_epoch") is None:
        return None
    return last if str(last["run_epoch"]) == str(epoch) else None


# --- the robot view --------------------------------------------------------------------------------

def session_state(row: Mapping[str, Any]) -> str:
    """finished | paused | mapping | operating."""
    if row.get("ended_at") is not None:
        return "finished"
    if purpose_of(row) == OPERATE:
        return "operating"
    return "paused" if row.get("paused_at") is not None else "mapping"


def robot_session_view(row: Optional[Mapping[str, Any]]) -> Optional[Dict[str, Any]]:
    """The robot's derived, read-only `session` key (§14.3): its open session or None.
    `map_T_session` is null while the session is not placed (never a guessed identity)."""
    if row is None:
        return None
    placed = is_placed(row)
    placement = row.get("placement") or None
    return {"session_id": str(row["session_id"]), "map": row["map_name"],
            "purpose": purpose_of(row), "state": session_state(row), "aligned": placed,
            "map_T_session": transform_of(row.get("map_t_session")) if placed else None,
            "unplaced_reason": (placement or {}).get("unplaced_reason") if not placed else None}


def by_robot(rows: Iterable[Mapping[str, Any]]) -> Dict[str, Mapping[str, Any]]:
    return {r["robot_name"]: r for r in rows}


def unplaced_placement(placement: Optional[Mapping[str, Any]], reason: str,
                       at: datetime.datetime, evidence: Optional[Mapping[str, Any]] = None
                       ) -> Dict[str, Any]:
    """The placement column after a session became unplaced: the last placement is kept for
    reference, with why and when it stopped being valid."""
    out = dict(placement or {})
    out.update({"unplaced_reason": reason, "unplaced_at": at.isoformat()})
    if evidence:
        out["unplaced_evidence"] = dict(evidence)
    return out


def names(rows: Iterable[Mapping[str, Any]], key: str = "robot_name") -> List[str]:
    return sorted({str(r[key]) for r in rows})
