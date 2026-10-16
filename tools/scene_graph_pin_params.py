"""One-off backfill: pin a legacy map's scene-graph params (MapSpecV1.scene_graph_params is None) from the frozen copy the
builder keeps in its Arango manifest (`sg_meta_<map_id>`, meta["params"], written by engine._state_params).

    python -m tools.scene_graph_pin_params                    # dry run: one line per map, pin|skip with the reason and params
    python -m tools.scene_graph_pin_params --apply            # UPDATE the specs (one transaction per map)
    python -m tools.scene_graph_pin_params --map-id ID ...    # only these maps

Not an alembic migration: the source of the values is in Arango.

Key mapping (manifest -> SceneGraphParamsV1):
    chart_radius   -> chart_radius_m
    robot_half_side-> robot_half_side_m
    near_range_m   -> close_evidence_range_m   (the key is written only when close evidence is ON; absent means OFF -> null,
                                                NOT the schema default, which would switch the rule on and change the map)
    place_peak_m   -> place_rule = (place_peak_m is not None)   (place_peak_m/place_path_m are derived from robot_half_side_m)
    lattice, near_min_clearance_m, place_path_m: no spec field; not pinned (reported as `unpinned` in the dry-run line).
Absent keys are therefore read as "off", which is what the manifest means; a missing chart_radius or robot_half_side has no
such reading and the map is skipped.

The UPDATE is guarded in its WHERE clause (spec has no non-null scene_graph_params), so a concurrent create or edit is never
overwritten, and a second run changes nothing.

Risks: pinning freezes the manifest's value even if it is wrong (a map built for the wrong robot needs a rebuild, not a pin).
The builder's params-hash handling for pinned maps is being fixed separately on branch fix/per-map-params-hash; this tool does
not depend on it.
"""
import argparse
import json
import sys
import uuid
from typing import Any, Dict, List, Optional, Tuple

import pydantic

from cloud_common.objects.map import MapObjectV1, SceneGraphParamsV1
from cloud_common.objects.object import ObjectLifecycleV1

MAP_TABLE = MapObjectV1.table_name()
ALIVE = ObjectLifecycleV1.ALIVE.value
# Same channel and payload as packages/api/maps.py SqlStore._notify (the channel is MAP_TABLE there too) and
# PostgresDatabase._notify: "<publisher uuid> <name> <lifecycle>", so running services' watchers refresh the map.
# This tool is its own publisher (a fresh uuid per process, as the API does for one-off writes), so no listener
# takes the notification for its own change.
PUBLISHER_ID = uuid.uuid4()
NOTIFY_SQL = "SELECT pg_notify(%s, %s)"
SELECT_SQL = ("SELECT name, spec FROM " + MAP_TABLE + " WHERE lifecycle = 'ALIVE'"
              " AND (%(names)s::text[] IS NULL OR name = ANY(%(names)s::text[])) ORDER BY name")
# the guard is in the WHERE clause: only a spec still without params is touched
UPDATE_SQL = ("UPDATE " + MAP_TABLE + " SET spec = spec || jsonb_build_object('scene_graph_params', %(params)s::jsonb) "
              "WHERE name = %(name)s AND lifecycle = 'ALIVE' "
              "AND (spec->'scene_graph_params' IS NULL OR jsonb_typeof(spec->'scene_graph_params') = 'null')")
UNPINNED_KEYS = ("lattice", "near_min_clearance_m", "place_path_m")


def params_from_manifest(manifest_params: Any) -> Tuple[Optional[SceneGraphParamsV1], str, List[str]]:
    """(params, "", unpinned keys) or (None, skip reason, [])."""
    if not isinstance(manifest_params, dict) or not manifest_params:
        return None, "manifest has no usable params", []
    for k in ("chart_radius", "robot_half_side"):
        if manifest_params.get(k) is None:
            return None, "manifest params lack %s" % k, []
    try:
        params = SceneGraphParamsV1(
            chart_radius_m=manifest_params["chart_radius"],
            robot_half_side_m=manifest_params["robot_half_side"],
            close_evidence_range_m=manifest_params.get("near_range_m"),
            place_rule=manifest_params.get("place_peak_m") is not None)
    except pydantic.ValidationError as exc:
        return None, "params fail validation: %s" % "; ".join(
            "%s: %s" % (".".join(map(str, e["loc"])), e["msg"]) for e in exc.errors()), []
    return params, "", [k for k in UNPINNED_KEYS if manifest_params.get(k) is not None]


def plan_map(map_id: str, spec: Optional[dict], manifest_params: Any, has_manifest: bool) -> Dict[str, Any]:
    out: Dict[str, Any] = {"map_id": map_id}
    existing = (spec or {}).get("scene_graph_params")
    if existing is not None:
        return dict(out, action="skip", reason="already pinned", params=existing)
    if not has_manifest:
        return dict(out, action="skip", reason="no manifest", params=None)
    params, reason, unpinned = params_from_manifest(manifest_params)
    if params is None:
        return dict(out, action="skip", reason=reason, params=None)
    out.update(action="pin", reason="from manifest" + (" (not pinned: %s)" % ", ".join(unpinned) if unpinned else ""),
               params=json.loads(params.json()))
    return out


def apply_pin(conn: Any, map_id: str, params: dict) -> bool:
    """One transaction: the guarded UPDATE and, only when it changed the row, the map NOTIFY (committed with it).
    True when the row was updated (False: a spec got params in the meantime, nothing is notified)."""
    with conn.transaction():
        cur = conn.execute(UPDATE_SQL, {"name": map_id, "params": json.dumps(params)})
        if cur.rowcount != 1:
            return False
        conn.execute(NOTIFY_SQL, (MAP_TABLE, "%s %s %s" % (PUBLISHER_ID, map_id, ALIVE)))
        return True


def main(argv: Optional[List[str]] = None, conn: Any = None, store: Any = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--map-id", action="append", default=[])
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args(argv)
    if conn is None:
        import psycopg
        from packages import config
        conn = psycopg.connect(
            f"dbname={config.POSTGRES_DATABASE_NAME} user={config.POSTGRES_DATABASE_USERNAME} "
            f"host={config.POSTGRES_DATABASE_HOST} port={config.POSTGRES_DATABASE_PORT} "
            f"password={config.POSTGRES_DATABASE_PASSWORD}")
    if store is None:
        from packages.scene_graph.store import SceneGraphStore
        from packages.topomap_dbs.client import TopomapDatabaseClient
        store = SceneGraphStore.from_client(TopomapDatabaseClient())
    with conn.transaction():
        rows = conn.execute(SELECT_SQL, {"names": args.map_id or None}).fetchall()
    rc = 0
    for name, spec in rows:
        meta = store.docs.meta(name)
        line = plan_map(name, spec, (meta or {}).get("params"), bool(meta))
        if args.apply and line["action"] == "pin":
            if not apply_pin(conn, name, line["params"]):
                line.update(action="skip", reason="spec changed under us (already pinned)")
        print(json.dumps(line, sort_keys=True) + ("" if args.apply else "  [dry run]"))
    return rc


if __name__ == "__main__":
    sys.exit(main())
