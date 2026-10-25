"""map_keepouts (operator keep-out zones of a map's scene graph)

Revision ID: 20261025_01_map_keepouts
Revises: 20261023_01_map_person_removal
Create Date: 2026-10-25

Regions and edges of a map's scene graph an operator switched off by hand, so the mission planner never routes
through them (packages/utils/keepouts.py, docs/keepout_zones.md). One row per zone: `spec` is
{"chart_key"} | {"polygon": [[x, y], ...]} for a region, {"from", "to"} (chart key or [x, y]) for an edge.
Persistent: no expiry, and not the blocked_graph_nodes table, whose purge must never reach these rows. No foreign
key to mapobjectv1 (created at runtime); the map delete removes the rows (packages/api/map_delete.py).

downgrade() drops the table.
"""
from alembic import op

revision = "20261025_01_map_keepouts"
down_revision = "20261023_01_map_person_removal"
branch_labels = None
depends_on = None

UPGRADE_SQL = """
SET LOCAL lock_timeout = '10s';
CREATE TABLE IF NOT EXISTS map_keepouts (
  id          text PRIMARY KEY,
  map_name    text NOT NULL,
  kind        text NOT NULL CHECK (kind IN ('region', 'edge')),
  label       text,
  enabled     boolean NOT NULL DEFAULT true,
  spec        jsonb NOT NULL,
  created_at  timestamptz NOT NULL DEFAULT now(),
  updated_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS map_keepouts_map_idx ON map_keepouts (map_name, created_at);
"""

DOWNGRADE_SQL = """
SET LOCAL lock_timeout = '10s';
DROP INDEX IF EXISTS map_keepouts_map_idx;
DROP TABLE IF EXISTS map_keepouts;
"""


def upgrade() -> None:
    op.execute(UPGRADE_SQL)


def downgrade() -> None:
    op.execute(DOWNGRADE_SQL)
