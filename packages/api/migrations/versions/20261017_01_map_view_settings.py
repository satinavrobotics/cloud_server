"""map_view_settings (per-map view settings of the map's own 3D view)

Revision ID: 20261017_01_map_view_settings
Revises: 20261015_01_map_semantics
Create Date: 2026-10-17

One row per map: how the map is looked at in its own window (packages/api/map_view_settings.py):
the vertical range of the 3D scene and the semantic classes left out. A small jsonb document, the
client validates what it writes and the API validates it again. No foreign key to mapobjectv1
(created at runtime); the map delete removes the row (packages/api/map_delete.py).

downgrade() drops the table.
"""
from alembic import op

revision = "20261017_01_map_view_settings"
down_revision = "20261015_01_map_semantics"
branch_labels = None
depends_on = None

UPGRADE_SQL = """
SET LOCAL lock_timeout = '10s';
CREATE TABLE IF NOT EXISTS map_view_settings (
  map_name    text PRIMARY KEY,
  settings    jsonb NOT NULL DEFAULT '{}'::jsonb,
  updated_at  timestamptz NOT NULL DEFAULT now()
);
"""

DOWNGRADE_SQL = """
SET LOCAL lock_timeout = '10s';
DROP TABLE IF EXISTS map_view_settings;
"""


def upgrade() -> None:
    op.execute(UPGRADE_SQL)


def downgrade() -> None:
    op.execute(DOWNGRADE_SQL)
