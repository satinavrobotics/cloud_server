"""run_legs (map_id, started_at DESC): the index fetch_legs filters and orders by

Revision ID: 20261030_01_run_legs_map_idx
Revises: 20261028_05_analysis_catalog
Create Date: 2026-10-30

docs/stats_review_2026-10-30.md P4. The map-scoped read side (fetch_legs: path efficiency, map series) selects the legs of one map in a
time window, newest first; the only index on run_legs so far is (mission_name, started_at DESC) and the primary key (run_id, seq).

A plain table, so a plain CREATE INDEX (it blocks writes to run_legs while it builds; the table is small, one row per leg, so that is
a fraction of a second). IF NOT EXISTS so a re-run after a hand-made index is harmless. downgrade() drops it.
"""
from alembic import op

revision = "20261030_01_run_legs_map_idx"
down_revision = "20261028_05_analysis_catalog"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
SET LOCAL lock_timeout = '10s';
CREATE INDEX IF NOT EXISTS run_legs_map_started_idx ON run_legs (map_id, started_at DESC);
""")


def downgrade() -> None:
    op.execute("""
SET LOCAL lock_timeout = '10s';
DROP INDEX IF EXISTS run_legs_map_started_idx;
""")
