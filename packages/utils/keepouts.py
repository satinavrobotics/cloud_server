"""Keep-out zones: regions and edges of a map's scene graph an operator switched off by hand, so the mission
planner never routes through them (docs/keepout_zones.md).

Persistent, per map, in their own table `map_keepouts` (migration 20261025_01): no expiry, and nothing here
touches `blocked_graph_nodes`, whose expiry purge (blocked_nodes.PURGE_SQL) cannot reach these rows. The
planner turns the enabled rows into the same `Blocked` overlay segments the robot-reported blocks use
(packages/services/mission_planner/keepout_overlay.py).

A row's `spec` (jsonb) is one of
    region by chart  {"chart_key": "<scene-graph chart key>"}          stable across scene-graph versions (rule A)
    region by shape  {"polygon": [[x, y], ...]}                          map frame, metres
    edge             {"from": <chart key | [x, y]>, "to": <chart key | [x, y]>}   the link between two charts
Chart keys are the stable identifier the engine has; polygons are for what has no chart yet. A point in an
edge is a convenience: the API replaces it by the chart key at that point when the graph can be read.

This module is pure (validation, JSON shape, revision) plus the SQL the API and the planner share.
"""
import hashlib
import json
import logging
import math
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

TABLE = "map_keepouts"
KINDS = ("region", "edge")
MAX_PER_MAP = 200
MIN_POLYGON_POINTS, MAX_POLYGON_POINTS = 3, 200
MAX_LABEL = 120
MAX_KEY = 200
MAX_COORD = 1.0e6            # metres; anything beyond is a typo, and keeps the lattice arithmetic exact
MIN_POLYGON_AREA_M2 = 1.0e-4
MAX_POLYGON_SIDE_M = 200.0     # bounding box side: at 0.1 m cells a zone stays within the planner's cell budget

APPLIED, UNRESOLVED = "applied", "unresolved"

COLUMNS = ("id", "kind", "label", "enabled", "spec", "created_at", "updated_at")
LIST_SQL = (f"SELECT {', '.join(COLUMNS)} FROM {TABLE} WHERE map_name = %s ORDER BY created_at, id")
ENABLED_SQL = (f"SELECT {', '.join(COLUMNS)} FROM {TABLE} WHERE map_name = %s AND enabled ORDER BY created_at, id")
# params: (id, map, kind, label, spec json, map, MAX_PER_MAP); no row back = the map is at its cap
INSERT_SQL = (f"INSERT INTO {TABLE} (id, map_name, kind, label, enabled, spec) "
              f"SELECT %s, %s, %s, %s, true, %s::jsonb WHERE (SELECT count(*) FROM {TABLE} WHERE map_name = %s) < %s "
              f"RETURNING {', '.join(COLUMNS)}")
UPDATE_SQL = (f"UPDATE {TABLE} SET enabled = %s, label = %s, updated_at = now() WHERE map_name = %s AND id = %s "
              f"RETURNING {', '.join(COLUMNS)}")
GET_SQL = f"SELECT {', '.join(COLUMNS)} FROM {TABLE} WHERE map_name = %s AND id = %s"
DELETE_SQL = f"DELETE FROM {TABLE} WHERE map_name = %s AND id = %s RETURNING id"


class KeepoutError(Exception):
    """A request the API refuses: `status` is the HTTP status, `code` the machine-readable reason."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def _bad(code: str, message: str) -> KeepoutError:
    return KeepoutError(422, code, message)


def _number(value: Any, what: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _bad("invalid_keepout", f"{what} must be a number")
    v = float(value)
    if not math.isfinite(v) or abs(v) > MAX_COORD:
        raise _bad("invalid_keepout", f"{what} must be finite and within +-{MAX_COORD:g} m")
    return v


def _point(value: Any, what: str) -> List[float]:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise _bad("invalid_keepout", f"{what} must be [x, y]")
    return [_number(value[0], what + " x"), _number(value[1], what + " y")]


def polygon_area(poly: Sequence[Sequence[float]]) -> float:
    """Absolute shoelace area (m2)."""
    s = 0.0
    for i, (x0, y0) in enumerate(poly):
        x1, y1 = poly[(i + 1) % len(poly)]
        s += x0 * y1 - x1 * y0
    return abs(s) / 2.0


def _polygon(value: Any) -> List[List[float]]:
    if not isinstance(value, (list, tuple)):
        raise _bad("invalid_polygon", "region.polygon must be a list of [x, y] points")
    if not MIN_POLYGON_POINTS <= len(value) <= MAX_POLYGON_POINTS:
        raise _bad("invalid_polygon",
                   f"region.polygon needs {MIN_POLYGON_POINTS} to {MAX_POLYGON_POINTS} points, got {len(value)}")
    poly = [_point(p, f"region.polygon[{i}]") for i, p in enumerate(value)]
    if len(poly) > 3 and poly[0] == poly[-1]:
        poly = poly[:-1]                                  # a closed ring: the repeated point is not a vertex
    if len(poly) < MIN_POLYGON_POINTS or polygon_area(poly) < MIN_POLYGON_AREA_M2:
        raise _bad("invalid_polygon", "region.polygon has no area")
    xs, ys = [p[0] for p in poly], [p[1] for p in poly]
    if max(xs) - min(xs) > MAX_POLYGON_SIDE_M or max(ys) - min(ys) > MAX_POLYGON_SIDE_M:
        raise _bad("invalid_polygon", f"region.polygon must fit in a {MAX_POLYGON_SIDE_M:g} m square")
    return poly


def _chart_key(value: Any, what: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > MAX_KEY:
        raise _bad("invalid_keepout", f"{what} must be a non-empty string of at most {MAX_KEY} characters")
    return value


def _label(value: Any) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise _bad("invalid_keepout", "label must be a string or null")
    value = value.strip()
    if len(value) > MAX_LABEL:
        raise _bad("invalid_keepout", f"label must be at most {MAX_LABEL} characters")
    return value or None


def _endpoint(value: Any, what: str) -> Any:
    """An edge end: a chart key (string) or a map-frame point [x, y]."""
    if isinstance(value, str):
        return _chart_key(value, what)
    return _point(value, what)


def parse_create(body: Any) -> Tuple[str, Optional[str], Dict[str, Any]]:
    """(kind, label, spec) of a POST body, or KeepoutError(422). The spec is the normalised stored form."""
    if not isinstance(body, Mapping):
        raise _bad("invalid_keepout", "body must be a JSON object")
    kind = body.get("kind")
    if kind not in KINDS:
        raise _bad("invalid_keepout", f"kind must be one of {', '.join(KINDS)}")
    label = _label(body.get("label"))
    if kind == "region":
        region = body.get("region")
        if not isinstance(region, Mapping):
            raise _bad("invalid_keepout", "a region keep-out needs a `region` object")
        if body.get("edge") is not None:
            raise _bad("invalid_keepout", "a region keep-out must not carry an `edge`")
        has_key, has_poly = region.get("chart_key") is not None, region.get("polygon") is not None
        if has_key == has_poly:
            raise _bad("invalid_keepout", "region needs exactly one of chart_key or polygon")
        if has_key:
            return kind, label, {"chart_key": _chart_key(region["chart_key"], "region.chart_key")}
        return kind, label, {"polygon": _polygon(region["polygon"])}
    edge = body.get("edge")
    if not isinstance(edge, Mapping):
        raise _bad("invalid_keepout", "an edge keep-out needs an `edge` object")
    if body.get("region") is not None:
        raise _bad("invalid_keepout", "an edge keep-out must not carry a `region`")
    a, b = _endpoint(edge.get("from"), "edge.from"), _endpoint(edge.get("to"), "edge.to")
    if a == b:
        raise _bad("invalid_keepout", "edge.from and edge.to must differ")
    return kind, label, {"from": a, "to": b}


def parse_patch(body: Any) -> Dict[str, Any]:
    """The fields a PATCH body sets: {"enabled": bool} and/or {"label": str|None}; 422 if neither."""
    if not isinstance(body, Mapping):
        raise _bad("invalid_keepout", "body must be a JSON object")
    unknown = set(body) - {"enabled", "label"}
    if unknown:
        raise _bad("invalid_keepout", f"unknown field(s): {', '.join(sorted(unknown))}")
    out: Dict[str, Any] = {}
    if "enabled" in body:
        if not isinstance(body["enabled"], bool):
            raise _bad("invalid_keepout", "enabled must be true or false")
        out["enabled"] = body["enabled"]
    if "label" in body:
        out["label"] = _label(body["label"])
    if not out:
        raise _bad("invalid_keepout", "nothing to change: send enabled and/or label")
    return out


def _iso(ts: Any) -> Optional[str]:
    if ts is None:
        return None
    if hasattr(ts, "isoformat"):
        import datetime
        if getattr(ts, "tzinfo", None) is None:
            ts = ts.replace(tzinfo=datetime.timezone.utc)
        return ts.astimezone(datetime.timezone.utc).isoformat().replace("+00:00", "Z")
    return str(ts)


def row_dict(row: Sequence[Any]) -> Dict[str, Any]:
    """A table row as a plain dict (spec parsed, timestamps ISO strings)."""
    out = dict(zip(COLUMNS, row))
    spec = out.get("spec")
    out["spec"] = spec if isinstance(spec, dict) else json.loads(spec or "{}")
    out["created_at"], out["updated_at"] = _iso(out.get("created_at")), _iso(out.get("updated_at"))
    return out


def to_keepout(row: Mapping[str, Any], applies: str = APPLIED, reason: Optional[str] = None) -> Dict[str, Any]:
    """The API's Keepout object of a row. `applies`: applied | unresolved (`reason` says why, additive)."""
    spec = row["spec"]
    out: Dict[str, Any] = {"id": row["id"], "kind": row["kind"], "label": row.get("label"),
                           "enabled": bool(row["enabled"]), "created_at": row.get("created_at"),
                           "applies": applies}
    if row["kind"] == "region":
        out["region"] = ({"chart_key": spec["chart_key"]} if "chart_key" in spec
                         else {"polygon": [list(p) for p in spec["polygon"]]})
    else:
        out["edge"] = {"from": spec["from"], "to": spec["to"]}
    if reason:
        out["reason"] = reason
    return out


def revision(rows: Sequence[Mapping[str, Any]]) -> str:
    """A short token that changes whenever any row is added, removed, enabled/disabled or relabelled: the planner's
    cache key and the API's `revision`. Stable for the same rows."""
    h = hashlib.sha1()
    for r in sorted(rows, key=lambda r: str(r["id"])):
        h.update(f"{r['id']}|{int(bool(r['enabled']))}|{r.get('updated_at')}|".encode())
        h.update(json.dumps(r["spec"], sort_keys=True).encode())
    return h.hexdigest()[:16]


def undefined_table(err: BaseException) -> bool:
    """True for PostgreSQL's undefined_table (SQLSTATE 42P01), as psycopg raises it."""
    return type(err).__name__ == "UndefinedTable" or getattr(err, "sqlstate", None) == "42P01"


async def fetch(database: Any, map_id: str, enabled_only: bool = False) -> List[Dict[str, Any]]:
    """The map's keep-out rows (`database` has psycopg's connection()), oldest first. A database whose migration
    20261025_01 has not run has no table and so no zones: that is [] (logged), any other failure raises (the planner
    refuses to plan rather than plan as if there were none)."""
    try:
        async with database.connection() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(ENABLED_SQL if enabled_only else LIST_SQL, (map_id,))
                return [row_dict(r) for r in await cursor.fetchall()]
    except Exception as err:  # noqa: BLE001
        if not undefined_table(err):
            raise
        logger.warning("map_keepouts does not exist (migration 20261025_01 not applied): no keep-out zones")
        return []
