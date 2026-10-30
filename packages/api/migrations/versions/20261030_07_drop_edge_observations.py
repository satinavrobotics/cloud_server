"""Drop edge_observations (edge statistics are retired)

Revision ID: 20261030_07_drop_edge_observations
Revises: 20261030_06_class_basis_period
Create Date: 2026-10-30

docs/stats_review_2026-10-30.md D1 / WP-G2. The writers, the /edge-stats route and the purge loop went with the deploy of
20261030_01-05; nothing reads or writes the table any more, and map delete / robot delete stop deleting from it in the same
commit. downgrade() recreates the table empty (20261011_01_edge_observations + 20261027_02_edge_obs_history); the rows are gone.
"""
from alembic import op

revision = "20261030_07_drop_edge_observations"
down_revision = "20261030_06_class_basis_period"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
SET LOCAL lock_timeout = '10s';
DROP TABLE IF EXISTS edge_observations;
""")


def downgrade() -> None:
    op.execute("""
SET LOCAL lock_timeout = '10s';
CREATE TABLE IF NOT EXISTS edge_observations (
  map_id       text NOT NULL,
  from_node    text NOT NULL,
  to_node      text NOT NULL,
  robot_name   text NOT NULL,
  source       text NOT NULL CHECK (source IN ('leg', 'mapping')),
  source_key   text NOT NULL,
  observed_at  timestamptz NOT NULL,
  duration_s   double precision NOT NULL CHECK (duration_s > 0),
  distance_m   double precision,
  speed_mps    double precision,
  recoveries   int NOT NULL DEFAULT 0,
  blocks       int NOT NULL DEFAULT 0,
  PRIMARY KEY (map_id, from_node, to_node, robot_name, source, source_key)
);
CREATE INDEX IF NOT EXISTS edge_observations_robot_idx ON edge_observations (robot_name, map_id);
CREATE INDEX IF NOT EXISTS edge_observations_history_idx
  ON edge_observations (map_id, from_node, to_node, robot_name, observed_at DESC);
""")
