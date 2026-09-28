"""Maps redesign M2: move legacy topo nodes into their map's frame (docs/satinav-maps-redesign.md
§3, §12 "Left for M2", §13.2). One-off, idempotent, dry run by default.

    python -m tools.maps_m2_legacy_nodes [--map NAME ...] [--apply]
    python -m tools.maps_m2_legacy_nodes [--map NAME ...] --revert [--include-live] [--apply]

Run it where the API's environment is set, i.e. inside the API container (the image ships
tools/): `docker exec <api> python -m tools.maps_m2_legacy_nodes ...`.

Before M2 graph-builder stored node poses as the robot sent them, i.e. in the frame of the map's
legacy datum (datum_*). Since M2 a node's `pose` is in the map frame (a geo map: UTM grid metres
in its zone from `geo.origin_e/n`), `robot_pose` keeps the pose as received, and `session_id`
names its session. For every live map (or the --map ones) with its M1 `legacy` session:

- geo map with an origin: T = map_geo.session_transform(geo, legacy datum) (the legacy
  session's datum, else the map's datum_*). An 'enu' datum (or a 'utm' one of another zone)
  gives the grid convergence (-1.445 deg for `map`); a 'utm' datum at the origin is identity.
  Every node without `robot_pose`: robot_pose = its pose, pose = T applied, session_id = the
  legacy session. The legacy session's map_t_session = T. The map's datum_* become the origin
  as a 'utm' datum with bearing 0 (maps.origin_as_legacy_datum), so the old read paths
  (POST /map/load `transform`, GET /maps/{id}/graph, the planner's datum fallback, the client's
  utils/mapTransform.ts, which handles a 'utm' datum exactly) describe the map frame the nodes
  are now in.
- local map (or geo without an origin): identity; only robot_pose and session_id are added.
- the legacy session's node_count = its nodes in ArangoDB (M1 copied the row's stale count),
  and the map's status node_count/edge_count = ArangoDB's.

Nodes that already have `robot_pose` (M2 ingest, or an earlier run of this tool) are never
touched, so a second run changes nothing. A map without a legacy session is reported, not
changed (the frame of its nodes is unknown).

--revert undoes it from the stored robot_pose: pose = robot_pose, robot_pose and session_id
removed, for the legacy session's nodes (--include-live: every node with robot_pose, i.e. also
nodes M2 ingest stored in the map frame); the legacy session back to identity and the map's
datum_* back to the legacy session's datum. For a rollback to the pre-M2 images.

ArangoDB: one AQL UPDATE per map (a single query is atomic on a single server); Postgres: one
transaction per map, with the map NOTIFY. ArangoDB first: a failure between the two is fixed
by running the tool again.
"""

import argparse
import dataclasses
import json
import math
import sys
import uuid
from typing import Any, Dict, List, Optional

from packages.utils import map_geo

LEGACY = "legacy"


@dataclasses.dataclass
class MapPlan:
    name: str
    map_type: str
    legacy_session_id: Optional[str]
    transform: Dict[str, float]
    node_updates: List[Dict[str, Any]]      # [{_key, pose}] (new map-frame pose)
    already: int                            # nodes that already have robot_pose
    legacy_nodes_after: int                 # nodes tagged with the legacy session afterwards
    session_patch: Dict[str, Any]           # map_sessions columns to set
    spec_patch: Dict[str, Any]              # mapobjectv1 spec keys to set
    status_patch: Dict[str, Any]            # mapobjectv1 status keys to set
    notes: List[str]

    @property
    def changes(self) -> bool:
        return bool(self.node_updates or self.session_patch or self.spec_patch
                    or self.status_patch)


def _same(a: Any, b: Any, tol: float = 1e-9) -> bool:
    if isinstance(a, (int, float)) and isinstance(b, (int, float)) \
            and not isinstance(a, bool) and not isinstance(b, bool):
        return abs(float(a) - float(b)) <= tol
    return a == b


def _patch(current: Dict[str, Any], wanted: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in wanted.items() if not _same(current.get(k), v)}


def _pose(doc: Dict[str, Any]) -> Dict[str, float]:
    p = doc.get("pose") or {}
    return {"x": float(p.get("x", 0.0)), "y": float(p.get("y", 0.0)),
            "yaw": float(p.get("yaw", p.get("theta", 0.0)) or 0.0)}


def _same_transform(a: Optional[Dict[str, Any]], b: Dict[str, float]) -> bool:
    return a is not None and all(_same(float(a.get(k, 0.0)), b[k], 1e-12) for k in b)


def plan_map(name: str, spec: Dict[str, Any], status: Dict[str, Any],
             legacy: Optional[Dict[str, Any]], nodes: List[Dict[str, Any]],
             edge_count: int) -> MapPlan:
    """What the tool does to one map (pure). `legacy`: the map's legacy map_sessions row
    (session_id, datum, map_t_session, node_count) or None; `nodes`: all its node documents."""
    from cloud_common.objects.map import MapObjectV1, effective_type
    from packages.api.maps import origin_as_legacy_datum
    obj = MapObjectV1(name=name, status=status or {}, **(spec or {}))
    map_type = effective_type(obj)
    notes: List[str] = []
    geo_block = spec.get("geo") if map_type == "geo" else None
    counts = {"node_count": len(nodes), "edge_count": int(edge_count)}
    status_patch = _patch(status or {}, counts)
    if legacy is None:
        pending = sum(1 for n in nodes if "robot_pose" not in n)
        if pending:
            notes.append(f"no legacy session: {pending} node(s) without robot_pose left as "
                         "they are (frame unknown)")
        return MapPlan(name, map_type, None, dict(map_geo.IDENTITY), [], len(nodes) - pending,
                       0, {}, {}, status_patch, notes)
    sid = str(legacy["session_id"])
    t = dict(map_geo.IDENTITY)
    spec_patch: Dict[str, Any] = {}
    if geo_block:
        datum = legacy.get("datum") or map_geo.map_datum(spec)
        if datum is None:
            notes.append("geo map without a legacy datum: identity")
        else:
            t = map_geo.session_transform(geo_block, datum)
        spec_patch = _patch(spec, origin_as_legacy_datum(geo_block))
    elif map_type == "geo":
        notes.append("geo map without an origin yet: identity, datum_* unchanged")
    updates, already, tagged = [], 0, 0
    for doc in nodes:
        if "robot_pose" in doc:
            already += 1
            tagged += doc.get("session_id") == sid
            continue
        p = _pose(doc)
        x, y, yaw = map_geo.apply_pose(t, p["x"], p["y"], p["yaw"])
        updates.append({"_key": doc["_key"], "pose": {"x": x, "y": y, "yaw": yaw}})
    session_patch: Dict[str, Any] = {}
    if not _same_transform(legacy.get("map_t_session"), t):
        session_patch["map_t_session"] = t
    legacy_after = tagged + len(updates)
    if int(legacy.get("node_count") or 0) != legacy_after:
        session_patch["node_count"] = legacy_after
    return MapPlan(name, map_type, sid, t, updates, already, legacy_after, session_patch,
                   spec_patch, status_patch, notes)


def plan_revert(name: str, spec: Dict[str, Any], legacy: Optional[Dict[str, Any]],
                nodes: List[Dict[str, Any]], include_live: bool) -> MapPlan:
    """--revert (pure): nodes back to robot_pose; legacy session identity; datum_* back to the
    legacy session's datum."""
    sid = str(legacy["session_id"]) if legacy else None
    updates = [{"_key": d["_key"], "pose": d["robot_pose"]} for d in nodes
               if "robot_pose" in d and (include_live or (sid and d.get("session_id") == sid))]
    session_patch: Dict[str, Any] = {}
    spec_patch: Dict[str, Any] = {}
    notes: List[str] = []
    if legacy is not None:
        if not _same_transform(legacy.get("map_t_session"), map_geo.IDENTITY):
            session_patch["map_t_session"] = dict(map_geo.IDENTITY)
        datum = legacy.get("datum")
        if datum:
            spec_patch = _patch(spec, {
                "datum_latitude": datum["latitude"], "datum_longitude": datum["longitude"],
                "datum_bearing_deg": float(datum.get("bearing_deg") or 0.0),
                "datum_frame": datum.get("frame") or "enu",
                "datum_utm_zone": datum.get("utm_zone"), "datum_utm_north": datum.get("utm_north"),
                "datum_utm_easting": datum.get("utm_easting"),
                "datum_utm_northing": datum.get("utm_northing")})
    else:
        notes.append("no legacy session")
    return MapPlan(name, spec.get("type") or "?", sid, dict(map_geo.IDENTITY), updates, 0, 0,
                   session_patch, spec_patch, {}, notes)


# --- I/O ---------------------------------------------------------------------------------------

UPDATE_AQL = """
FOR u IN @updates
  LET d = DOCUMENT(@@col, u._key)
  FILTER d != null AND !HAS(d, 'robot_pose')
  UPDATE d WITH {pose: u.pose, robot_pose: d.pose, session_id: @sid} IN @@col
  COLLECT WITH COUNT INTO n
  RETURN n
"""
REVERT_AQL = """
FOR u IN @updates
  LET d = DOCUMENT(@@col, u._key)
  FILTER d != null AND HAS(d, 'robot_pose')
  UPDATE d WITH {pose: d.robot_pose, robot_pose: null, session_id: null} IN @@col
    OPTIONS {keepNull: false}
  COLLECT WITH COUNT INTO n
  RETURN n
"""


class Arango:
    def __init__(self):
        from arango import ArangoClient
        from packages import config
        client = ArangoClient(hosts=f"http://{config.ARANGO_HOST}:{config.ARANGO_PORT}")
        self.db = client.db(config.DATA_BASE_NAME, username=config.ARANGO_USERNAME,
                            password=config.ARANGO_PASSWORD or "openSesame")

    def nodes(self, name: str) -> List[Dict[str, Any]]:
        col = f"nodes_{name}"
        if not self.db.has_collection(col):
            return []
        return list(self.db.aql.execute("FOR d IN @@col RETURN d", bind_vars={"@col": col}))

    def edge_count(self, name: str) -> int:
        col = f"edges_{name}"
        return self.db.collection(col).count() if self.db.has_collection(col) else 0

    def apply(self, name: str, updates: List[Dict[str, Any]], sid: Optional[str],
              revert: bool) -> int:
        if not updates:
            return 0
        bind = {"@col": f"nodes_{name}", "updates": updates}
        if not revert:
            bind["sid"] = sid
        return next(iter(self.db.aql.execute(REVERT_AQL if revert else UPDATE_AQL,
                                             bind_vars=bind)), 0)


class Postgres:
    def __init__(self):
        import psycopg
        from packages import config
        self.conn = psycopg.connect(
            f"dbname={config.POSTGRES_DATABASE_NAME} user={config.POSTGRES_DATABASE_USERNAME} "
            f"host={config.POSTGRES_DATABASE_HOST} port={config.POSTGRES_DATABASE_PORT} "
            f"password={config.POSTGRES_DATABASE_PASSWORD}")

    def maps(self, names: Optional[List[str]]) -> List[tuple]:
        sql = ("SELECT name, spec, status FROM mapobjectv1 WHERE lifecycle = 'ALIVE'"
               + (" AND name = ANY(%s)" if names else "") + " ORDER BY name")
        with self.conn.transaction():
            return self.conn.execute(sql, (names,) if names else ()).fetchall()

    def legacy(self, name: str) -> Optional[Dict[str, Any]]:
        with self.conn.transaction():
            row = self.conn.execute(
                "SELECT session_id, datum, map_t_session, node_count FROM map_sessions "
                "WHERE map_name = %s AND kind = %s", (name, LEGACY)).fetchone()
        if row is None:
            return None
        return dict(zip(("session_id", "datum", "map_t_session", "node_count"), row))

    def apply(self, plan: MapPlan) -> None:
        with self.conn.transaction():
            if plan.session_patch:
                cols = ", ".join(f"{k} = %s" + ("::jsonb" if k == "map_t_session" else "")
                                 for k in plan.session_patch)
                vals = [json.dumps(v) if k == "map_t_session" else v
                        for k, v in plan.session_patch.items()]
                self.conn.execute(f"UPDATE map_sessions SET {cols} WHERE session_id = %s",
                                  (*vals, uuid.UUID(plan.legacy_session_id)))
            if plan.spec_patch or plan.status_patch:
                self.conn.execute(
                    "UPDATE mapobjectv1 SET spec = spec || %s::jsonb, "
                    "status = status || %s::jsonb WHERE name = %s AND lifecycle = 'ALIVE'",
                    (json.dumps(plan.spec_patch), json.dumps(plan.status_patch), plan.name))
                self.conn.execute("SELECT pg_notify('mapobjectv1', %s)",
                                  (f"{uuid.uuid4()} {plan.name} ALIVE",))


def _fmt_t(t: Dict[str, float]) -> str:
    return (f"tx={t['tx']:.3f} m ty={t['ty']:.3f} m yaw={math.degrees(t['yaw']):.4f} deg")


def report(plan: MapPlan, nodes_by_key: Dict[str, Dict[str, Any]], revert: bool) -> None:
    print(f"map {plan.name!r} ({plan.map_type}), legacy session {plan.legacy_session_id}")
    if not revert:
        print(f"  map_T_session(legacy) = {_fmt_t(plan.transform)}")
        print(f"  nodes: {len(plan.node_updates)} to rewrite, {plan.already} already have "
              f"robot_pose; legacy session nodes afterwards: {plan.legacy_nodes_after}")
    else:
        print(f"  nodes: {len(plan.node_updates)} back to robot_pose")
    for u in plan.node_updates[:5]:
        old = _pose(nodes_by_key[u["_key"]])
        new = u["pose"]
        print(f"    {u['_key']}: ({old['x']:.3f}, {old['y']:.3f}, {old['yaw']:.4f}) -> "
              f"({new['x']:.3f}, {new['y']:.3f}, {new['yaw']:.4f})")
    if len(plan.node_updates) > 5:
        print(f"    ... {len(plan.node_updates) - 5} more")
    for label, patch in (("map_sessions(legacy)", plan.session_patch),
                         ("spec", plan.spec_patch), ("status", plan.status_patch)):
        if patch:
            print(f"  {label} := {json.dumps(patch, sort_keys=True)}")
    for note in plan.notes:
        print(f"  note: {note}")
    if not plan.changes:
        print("  nothing to do")


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--map", action="append", dest="maps", help="only this map (repeatable)")
    ap.add_argument("--apply", action="store_true", help="write (default: dry run)")
    ap.add_argument("--revert", action="store_true", help="undo from robot_pose")
    ap.add_argument("--include-live", action="store_true",
                    help="with --revert: also nodes of live (M2) sessions")
    args = ap.parse_args(argv)
    pg, arango = Postgres(), Arango()
    mode = ("REVERT" if args.revert else "MIGRATE") + (" (apply)" if args.apply else " (dry run)")
    print(f"maps_m2_legacy_nodes: {mode}")
    total = 0
    for name, spec, status in pg.maps(args.maps):
        spec, status = spec or {}, status or {}
        legacy = pg.legacy(name)
        nodes = arango.nodes(name)
        if args.revert:
            plan = plan_revert(name, spec, legacy, nodes, args.include_live)
        else:
            plan = plan_map(name, spec, status, legacy, nodes, arango.edge_count(name))
        report(plan, {d["_key"]: d for d in nodes}, args.revert)
        if args.apply and plan.changes:
            n = arango.apply(name, plan.node_updates, plan.legacy_session_id, args.revert)
            pg.apply(plan)
            print(f"  applied: {n} node(s) rewritten, Postgres updated")
            total += n
    print(f"done: {total} node(s) rewritten" if args.apply else "dry run: nothing written")
    return 0


if __name__ == "__main__":
    sys.exit(main())
