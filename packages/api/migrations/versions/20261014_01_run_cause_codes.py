"""run_cause_codes: four cause codes for live failure texts + mission_runs map index

Revision ID: 20261014_01_run_cause_codes
Revises: 20261012_01_blocked_geometry
Create Date: 2026-10-14

mission_runs.abort_cause REFERENCES cause_codes(code), so the classifier rules added in
packages/events/causes.py (SAFETY.REFLEX_LATCHED, HW.BASE_NOT_RESPONDING,
DISPATCH.ORDER_NOT_ACCEPTED, MAP.NOT_PLACED) need their seed rows first. Also an index for the
per-map statistics (GET /api/v1/maps/{id}/run-stats, GET /api/v1/runs?map=).

Rolling deploy: the rows are inserted by the API container's entrypoint (`alembic upgrade
head`), and mission-dispatch waits for the API to be healthy in the compose file. If
mission-dispatch is ever recreated with the new image while the old schema is still in place,
a run that ends with one of the new causes fails its end-of-run UPDATE on the foreign key
(the recorder's write error path; not verified what it does with the run afterwards). So: deploy
the API (migration) first, or both together; never dispatch alone. Idempotent
(ON CONFLICT DO NOTHING; IF NOT EXISTS). downgrade() removes the index and the four rows,
except that a row still referenced by a run is kept (the delete is skipped for it).
"""
from alembic import op

revision = "20261014_01_run_cause_codes"
down_revision = "20261012_01_blocked_geometry"
branch_labels = None
depends_on = None

NEW_CAUSES = (
    ("SAFETY.REFLEX_LATCHED", "SAFETY", "Safety reflex latched",
     "The robot's safety reflex stopped it and stayed latched."),
    ("HW.BASE_NOT_RESPONDING", "HW", "Robot base not responding",
     "The robot's base controller stopped reporting (e.g. no odometry)."),
    ("DISPATCH.ORDER_NOT_ACCEPTED", "DISPATCH", "Order not accepted",
     "The robot kept reporting another order and never adopted the dispatched one."),
    ("MAP.NOT_PLACED", "MAP", "Robot not placed on the map",
     "The robot has no placed session on the map of the route."),
)


def _q(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def upgrade() -> None:
    values = ",\n  ".join("(" + ", ".join(_q(v) for v in row) + ")" for row in NEW_CAUSES)
    op.execute(f"""
SET LOCAL lock_timeout = '10s';
INSERT INTO cause_codes (code, category, title, description) VALUES
  {values}
ON CONFLICT (code) DO NOTHING;
CREATE INDEX IF NOT EXISTS mission_runs_map_started_idx ON mission_runs (map_id, started_at DESC);
""")


def downgrade() -> None:
    codes = ", ".join(_q(row[0]) for row in NEW_CAUSES)
    op.execute(f"""
SET LOCAL lock_timeout = '10s';
DROP INDEX IF EXISTS mission_runs_map_started_idx;
DELETE FROM cause_codes c WHERE c.code IN ({codes})
  AND NOT EXISTS (SELECT 1 FROM mission_runs r WHERE r.abort_cause = c.code);
""")
