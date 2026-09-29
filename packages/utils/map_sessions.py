"""Open sessions: purpose, placement and the mapping switch (docs/satinav-maps-redesign.md §14).

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

A session that is not placed captures nothing, gets no route orders and no planned paths.

Pure functions plus the SQL that several services share (the API, mission-dispatch,
mission-planner, graph-builder); no I/O here. The mapping switch payload (`set_payload`, the
contract of packages/api/mapping_control.py) lives here too, because mission-dispatch publishes
it after it unplaces or re-places a session (U3) and must not import packages.api.
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
                   "map_t_session", "aligned", "node_count")

# The robot's open session with its map (one row at most: map_sessions_one_open_per_robot).
ROBOT_SESSION_SQL = (
    "SELECT s.session_id, s.map_name, s.purpose, s.aligned, s.map_t_session, s.datum, "
    "s.placement, s.paused_at, m.spec->'geo', m.spec->>'type' "
    "FROM map_sessions s LEFT JOIN mapobjectv1 m "
    "ON m.name = s.map_name AND m.lifecycle <> 'DELETED' "
    "WHERE s.robot_name = %s AND s.ended_at IS NULL")
ROBOT_SESSION_KEYS = ("session_id", "map_name", "purpose", "aligned", "map_t_session", "datum",
                      "placement", "paused_at", "map_geo", "map_type")


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


def driving_reason(robot_state: Optional[str], state_msg: Optional[Mapping[str, Any]]
                   ) -> Optional[str]:
    """Why the robot counts as driving (decision Q-U7: it must stand still while placed), or
    None. `robot_state`: RobotStatusV1.state (ON_TASK / MAP_DEPLOYMENT = an active order);
    `state_msg`: the robot's last VDA5050 state message (robot_latest.state_msg) or None."""
    if robot_state in ("ON_TASK", "MAP_DEPLOYMENT"):
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


# --- the mapping switch (contract: packages/api/mapping_control.py) -------------------------------

def capture_enabled(session: Optional[Mapping[str, Any]]) -> bool:
    """Capture is on only for an open, unpaused, placed MAPPING session (§14.5)."""
    return (session is not None and purpose_of(session) == MAPPING
            and session.get("ended_at") is None and session.get("paused_at") is None
            and is_placed(session))


def set_payload(open_session: Optional[Mapping[str, Any]],
                now: Optional[datetime.datetime] = None) -> Dict[str, Any]:
    """What the robot should capture, from its open session (a map_sessions row, or None).

    - no open session, or an `operate` session: the no-session payload (off, nulls);
    - a mapping session: its id and map; `enabled` only when it is unpaused and placed;
    - `services`: the mapping services to run (a robot without `services` handling runs topo).
    """
    issued_at = (now or datetime.datetime.now(datetime.timezone.utc)).isoformat()
    if open_session is None or purpose_of(open_session) != MAPPING:
        return {"enabled": False, "session_id": None, "map": None, "services": [],
                "issued_at": issued_at}
    return {"enabled": capture_enabled(open_session),
            "session_id": str(open_session["session_id"]),
            "map": open_session["map_name"],
            "services": list(open_session.get("services") or DEFAULT_SERVICES),
            "issued_at": issued_at}


def set_topic(prefix: str, robot: str) -> str:
    return f"{prefix.rstrip('/')}/{robot}/mapping/set"


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
