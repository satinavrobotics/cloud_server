"""map_person_removal (per-map person removal setting)

Revision ID: 20261023_01_map_person_removal
Revises: 20261021_01_map_meshes
Create Date: 2026-10-23

One row per map that made its own choice about person removal (packages/api/person_removal.py,
docs/reconstruction/person_removal.md): {enabled, model, dilate_px}, each null = the server's
default. No row = the server's defaults. No foreign key to mapobjectv1 (created at runtime); the
map delete removes the row (packages/api/map_delete.py). The masks themselves are in MinIO and on
the ArangoDB nodes, not here.

downgrade() drops the table.
"""
from alembic import op

revision = "20261023_01_map_person_removal"
down_revision = "20261021_01_map_meshes"
branch_labels = None
depends_on = None

UPGRADE_SQL = """
SET LOCAL lock_timeout = '10s';
CREATE TABLE IF NOT EXISTS map_person_removal (
  map_name    text PRIMARY KEY,
  settings    jsonb NOT NULL DEFAULT '{}'::jsonb,
  updated_at  timestamptz NOT NULL DEFAULT now()
);
"""

DOWNGRADE_SQL = """
SET LOCAL lock_timeout = '10s';
DROP TABLE IF EXISTS map_person_removal;
"""


def upgrade() -> None:
    op.execute(UPGRADE_SQL)


def downgrade() -> None:
    op.execute(DOWNGRADE_SQL)
