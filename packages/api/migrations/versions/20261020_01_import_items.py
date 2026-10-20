"""import_items: what a data transfer inserted that has no row of its own to mark it (mission runs, rosbag recordings)

Revision ID: 20261020_01_import_items
Revises: 20261017_01_map_view_settings
Create Date: 2026-10-20

A map created by a transfer carries `status.imported_from` (packages/migration), so Undo finds it. A finished run (`mission_runs`, immutable
once terminal) and a rosbag in MinIO have nowhere to carry such a marker, so the transfer records each one it actually inserted here:
(import id, kind, key). Undo deletes exactly those, never a run or recording that was already on the server. `key` is the run id (kind
'run') or `robot/bag_id` (kind 'rosbag'). No foreign keys: a run deleted by its mission's delete simply leaves a stale row that Undo skips.

downgrade() drops the table.
"""
from alembic import op

revision = "20261020_01_import_items"
down_revision = "20261017_01_map_view_settings"
branch_labels = None
depends_on = None

UPGRADE_SQL = """
SET LOCAL lock_timeout = '10s';
CREATE TABLE IF NOT EXISTS import_items (
  import_id   text        NOT NULL,
  kind        text        NOT NULL,
  key         text        NOT NULL,
  created_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (import_id, kind, key)
);
"""

DOWNGRADE_SQL = """
SET LOCAL lock_timeout = '10s';
DROP TABLE IF EXISTS import_items;
"""


def upgrade() -> None:
    op.execute(UPGRADE_SQL)


def downgrade() -> None:
    op.execute(DOWNGRADE_SQL)
