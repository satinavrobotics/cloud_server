"""map_sessions: mapping sessions, and type/state of every existing map (maps redesign M1)

Revision ID: 20260928_01_map_sessions
Revises: 20260926_02_recorder_health
Create Date: 2026-09-28

docs/satinav-maps-redesign.md §4, §12.

Schema
------
`map_sessions`: one row per mapping session (one robot adding data to one map for one run).
`map_t_session` is the doc's `map_T_session` (Postgres folds unquoted names to lower case):
{tx, ty, yaw} from the run's robot frame into the map frame. `kind` is 'live' for sessions
started through POST /api/v1/maps/{id}/sessions and 'legacy' for the one synthetic session per
pre-M1 map (below). At most one open session (ended_at IS NULL) per robot: partial unique
index, mapped to 409 by the API. One-open-session-per-map is deliberately NOT a constraint
(multi-robot mapping, doc §10). No foreign key to mapobjectv1: that table is created at
runtime by initialize_database, not here; the API's map delete removes a map's sessions.

Data (idempotent: only maps without a `type` are classified, the legacy session is ON CONFLICT
DO NOTHING on its partial unique index)
-------------------------------------------------------------------------------------------
Every mapobjectv1 row (except DELETED):

- spec.type = 'geo' if the datum is set and not (0, 0), else 'local'. A geo map gets
  spec.geo = {utm_zone, utm_north, origin_e, origin_n}: the datum's own UTM point (a 'utm'
  datum's reported zone/hemisphere/easting/northing when present, else the lat/lon projected in
  its longitude's zone; packages/utils/map_geo.py). The datum_* fields are kept: they describe
  the frame the legacy nodes are stored in (nodes are not rewritten in M1).
- status.state = 'ready'.
- one ended, aligned 'legacy' session: robot_name 'legacy', datum = the map's datum (null
  without one), map_t_session = identity, node_count = the row's stored status.node_count.

Nothing is created for ArangoDB collections without a Postgres row, and robot.current_map is
not touched (graph-builder still ingests by it until M2).

downgrade() drops map_sessions and removes type/geo and state/open_session_id/grid_version from
the map rows (old images ignore them anyway). Maps created by the M1 routes stay as plain maps;
a geo map that got its origin from a session keeps the datum_* fields that session wrote.
"""
import json

from alembic import op

revision = "20260928_01_map_sessions"
down_revision = "20260926_02_recorder_health"
branch_labels = None
depends_on = None

LEGACY_ROBOT = "legacy"
SESSION_KINDS = ("live", "legacy")
IDENTITY = {"tx": 0.0, "ty": 0.0, "yaw": 0.0}
SPEC_KEYS = ("type", "geo")
STATUS_KEYS = ("state", "open_session_id", "grid_version")


def _in_list(values) -> str:
    return ", ".join(f"'{v}'" for v in values)


def _schema() -> str:
    return f"""
SET LOCAL lock_timeout = '10s';
CREATE TABLE map_sessions (
  session_id     uuid PRIMARY KEY,
  map_name       text NOT NULL,
  robot_name     text NOT NULL,
  kind           text NOT NULL DEFAULT 'live',
  started_at     timestamptz NOT NULL DEFAULT now(),
  paused_at      timestamptz,
  ended_at       timestamptz,
  datum          jsonb,
  map_t_session  jsonb NOT NULL,
  aligned        boolean NOT NULL,
  node_count     int NOT NULL DEFAULT 0,
  CONSTRAINT map_sessions_kind_check CHECK (kind IN ({_in_list(SESSION_KINDS)})),
  CONSTRAINT map_sessions_legacy_ended_check CHECK (kind <> 'legacy' OR ended_at IS NOT NULL),
  CONSTRAINT map_sessions_paused_open_check CHECK (paused_at IS NULL OR ended_at IS NULL)
);
CREATE UNIQUE INDEX map_sessions_one_open_per_robot ON map_sessions (robot_name)
  WHERE ended_at IS NULL;
CREATE UNIQUE INDEX map_sessions_one_legacy_per_map ON map_sessions (map_name)
  WHERE kind = 'legacy';
CREATE INDEX map_sessions_map_idx ON map_sessions (map_name, started_at);
"""


def classify_row(spec: dict) -> dict:
    """The spec patch for one pre-M1 map row: {'type': ..., 'geo': ...}."""
    from packages.utils import map_geo  # the API image's code; pure functions
    map_type, geo = map_geo.classify(spec or {})
    return {"type": map_type, "geo": geo}


def legacy_session_datum(spec: dict):
    from packages.utils import map_geo
    return map_geo.map_datum(spec or {})


def _classify_maps(bind) -> None:
    exists = bind.exec_driver_sql("SELECT to_regclass('mapobjectv1') IS NOT NULL").scalar()
    if not exists:
        return  # fresh database: initialize_database creates the table later, empty
    rows = bind.exec_driver_sql(
        "SELECT name, spec, status FROM mapobjectv1 WHERE lifecycle <> 'DELETED' "
        "ORDER BY name FOR UPDATE").fetchall()
    for name, spec, status in rows:
        spec = spec or {}
        status = status or {}
        if not spec.get("type"):
            bind.exec_driver_sql(
                "UPDATE mapobjectv1 SET spec = spec || %s::jsonb WHERE name = %s",
                (json.dumps(classify_row(spec)), name))
        if not status.get("state"):
            bind.exec_driver_sql(
                "UPDATE mapobjectv1 SET status = status || %s::jsonb WHERE name = %s",
                (json.dumps({"state": "ready"}), name))
        datum = legacy_session_datum(spec)
        bind.exec_driver_sql(
            "INSERT INTO map_sessions (session_id, map_name, robot_name, kind, started_at, "
            "ended_at, datum, map_t_session, aligned, node_count) "
            "VALUES (gen_random_uuid(), %s, %s, 'legacy', now(), now(), %s::jsonb, %s::jsonb, "
            "true, %s) ON CONFLICT (map_name) WHERE kind = 'legacy' DO NOTHING",
            (name, LEGACY_ROBOT, json.dumps(datum) if datum is not None else None,
             json.dumps(IDENTITY), int(status.get("node_count") or 0)))


def upgrade() -> None:
    op.execute(_schema())
    if op.get_context().as_sql:
        op.execute("-- data step (classify mapobjectv1 rows, legacy sessions) runs online only")
        return
    _classify_maps(op.get_bind())


def downgrade() -> None:
    spec_minus = " - ".join(f"'{k}'" for k in SPEC_KEYS)
    status_minus = " - ".join(f"'{k}'" for k in STATUS_KEYS)
    op.execute(f"""
SET LOCAL lock_timeout = '10s';
DROP TABLE IF EXISTS map_sessions;
DO $$
BEGIN
  IF to_regclass('mapobjectv1') IS NOT NULL THEN
    UPDATE mapobjectv1 SET spec = spec - {spec_minus}, status = status - {status_minus};
  END IF;
END $$;
""")
