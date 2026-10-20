"""transfer_sync: the one row of settings and status of a server's automatic data sync (packages/migration/autosync.py)

Revision ID: 20261021_01_transfer_sync
Revises: 20261020_01_import_items
Create Date: 2026-10-21

A robot server can push what it records (finished mission runs with their history, and recordings) to another server whenever that server is
reachable. The choice (on/off, where to, how often, what) and the outcome of the last attempt live in this single row (`id` is always 1),
so they survive a restart and the screen shows the same thing on every worker. Additive; downgrade() drops the table.
"""
from alembic import op

revision = "20261021_01_transfer_sync"
down_revision = "20261020_01_import_items"
branch_labels = None
depends_on = None

UPGRADE_SQL = """
SET LOCAL lock_timeout = '10s';
CREATE TABLE IF NOT EXISTS transfer_sync (
  id          smallint    PRIMARY KEY CHECK (id = 1),
  settings    jsonb       NOT NULL DEFAULT '{}',
  status      jsonb       NOT NULL DEFAULT '{}',
  updated_at  timestamptz NOT NULL DEFAULT now()
);
"""

DOWNGRADE_SQL = """
SET LOCAL lock_timeout = '10s';
DROP TABLE IF EXISTS transfer_sync;
"""


def upgrade() -> None:
    op.execute(UPGRADE_SQL)


def downgrade() -> None:
    op.execute(DOWNGRADE_SQL)
