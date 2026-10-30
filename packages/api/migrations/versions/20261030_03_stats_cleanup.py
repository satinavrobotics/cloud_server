"""run_cells: drop the unused (map_id, robot_class, started_at) index; run_legs.predicted_s float4 -> float8

Revision ID: 20261030_03_stats_cleanup
Revises: 20261030_02_track_run_idx
Create Date: 2026-10-30

docs/stats_review_2026-10-30.md R2.

- run_cells_map_class_idx (migration 20261028_01_run_cells) is not used: no reader passes `robot_class` to run_cells_db.fetch, and
  run_cells_map_started_idx covers the (map_id, started_at) reads. Dropping it saves a write per finished run. downgrade() recreates it.
- run_legs.predicted_s was added as float4 (20261028_03_run_legs_predicted) while its sibling columns are float8. The USING cast
  widens the stored float4 value (e.g. 12.3 becomes 12.300000190734863); run_legs is small, so the rewrite is quick. downgrade()
  narrows it back to real.
"""
from alembic import op

revision = "20261030_03_stats_cleanup"
down_revision = "20261030_02_track_run_idx"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
SET LOCAL lock_timeout = '10s';
DROP INDEX IF EXISTS run_cells_map_class_idx;
ALTER TABLE run_legs ALTER COLUMN predicted_s TYPE double precision USING predicted_s::double precision;
""")


def downgrade() -> None:
    op.execute("""
SET LOCAL lock_timeout = '10s';
ALTER TABLE run_legs ALTER COLUMN predicted_s TYPE real USING predicted_s::real;
CREATE INDEX IF NOT EXISTS run_cells_map_class_idx ON run_cells (map_id, robot_class, started_at DESC);
""")
