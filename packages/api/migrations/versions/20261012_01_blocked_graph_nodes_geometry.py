"""blocked_graph_nodes_geometry (where the robot stood and came from when it reported a block)

Revision ID: 20261012_01_blocked_graph_nodes_geometry
Revises: 20261011_01_edge_observations
Create Date: 2026-10-12

A blocked_graph_nodes row holds the position of the blocked waypoint only. With the sparse
waypoints of a lattice route that waypoint can be far past the obstacle, so the row's
neighbourhood is not where the obstacle is. These columns let the planner and the reroute check
treat the obstacle as a segment from the robot's pose to the waypoint
(packages/utils/blocked_geometry.py):

- robot_x, robot_y: the robot's position (map frame, metres) when it reported the block.
- prev_x, prev_y: the previous waypoint of the route (map frame); kept for a later fit.

Additive: four nullable columns, no backfill (rows written before keep NULLs and the old disc
rule). Must be applied BEFORE the dispatcher / planner / API that read and write them.
downgrade() drops them.
"""
from alembic import op

revision = "20261012_01_blocked_graph_nodes_geometry"
down_revision = "20261011_01_edge_observations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
SET LOCAL lock_timeout = '10s';
ALTER TABLE blocked_graph_nodes
  ADD COLUMN IF NOT EXISTS robot_x double precision,
  ADD COLUMN IF NOT EXISTS robot_y double precision,
  ADD COLUMN IF NOT EXISTS prev_x  double precision,
  ADD COLUMN IF NOT EXISTS prev_y  double precision;
""")


def downgrade() -> None:
    op.execute("""
SET LOCAL lock_timeout = '10s';
ALTER TABLE blocked_graph_nodes
  DROP COLUMN IF EXISTS robot_x,
  DROP COLUMN IF EXISTS robot_y,
  DROP COLUMN IF EXISTS prev_x,
  DROP COLUMN IF EXISTS prev_y;
""")
