"""run_legs: the plan each leg was recorded against, and the charts of its two waypoints

Revision ID: 20261027_03_run_legs_plan
Revises: 20261027_02_edge_obs_history
Create Date: 2026-10-27

Stage B of the edge statistics. Until now `planned_m` was the straight line between the leg's two waypoints
(== `straight_m`), so everything computed from it (expected_s, the run's distance_m, the per-edge distance and
speed) used the straight line, not the route the planner made. mission-dispatch now has the live route when it
records a leg and writes the real plan length (packages/utils/plan_length.py, leg_tracker.py):

- `plan_source` text: how `planned_m` was found. 'trajectory' = the length of the planner's dense path between the
  two waypoints (the route node's stored trajectory entry, whose ends are the leg's ends); 'straight' = the
  straight line (the leg has no trajectory entry: first leg of an order, across route nodes, no trajectory);
  'stale' = an entry exists but its ends are not the leg's (a reroute), the straight line is used. NULL = the leg
  was recorded before this change (planned_m == straight_m), or it has no straight line and so no plan
  (straight_m NULL: the leg starts at the robot's own position).
- `from_chart`, `to_chart` text: the scene-graph chart (Pose2D.chart_key) of the leg's start and end waypoint, as
  the planner stored it with the route; NULL for a base node or a route without chart keys.
- `graph_version` int: the scene-graph version those keys were made on (MissionRouteNodeV1.graph_version).

Additive: four nullable columns without default (a catalog-only change, no rewrite), no backfill: older legs
stay NULL. downgrade() drops them (the recorded values are lost, the legs are not).
"""
from alembic import op

revision = "20261027_03_run_legs_plan"
down_revision = "20261027_02_edge_obs_history"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
SET LOCAL lock_timeout = '10s';
ALTER TABLE run_legs
  ADD COLUMN plan_source   text,
  ADD COLUMN from_chart    text,
  ADD COLUMN to_chart      text,
  ADD COLUMN graph_version int,
  ADD CONSTRAINT run_legs_plan_source_check
    CHECK (plan_source IS NULL OR plan_source IN ('trajectory', 'straight', 'stale'));
""")


def downgrade() -> None:
    op.execute("""
SET LOCAL lock_timeout = '10s';
ALTER TABLE run_legs
  DROP CONSTRAINT IF EXISTS run_legs_plan_source_check,
  DROP COLUMN IF EXISTS graph_version,
  DROP COLUMN IF EXISTS to_chart,
  DROP COLUMN IF EXISTS from_chart,
  DROP COLUMN IF EXISTS plan_source;
""")
