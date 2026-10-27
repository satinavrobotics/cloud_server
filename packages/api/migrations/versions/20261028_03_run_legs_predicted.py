"""run_legs: the time the planner's region cost model predicted for each leg, and which model

Revision ID: 20261028_03_run_legs_predicted
Revises: 20261028_02_region_cost
Create Date: 2026-10-28

docs/planner_region_stats_2026-10-27.md section 7 (stage S4). The mission planner puts `predicted_s` on every trajectory leg of a plan
it made with the region cost model (also in shadow mode, where the route served is unchanged) and `cost_model` on the route node;
mission-dispatch copies them to the leg it records the way it copies `planned_m` (leg_tracker.py, fleet_recorder.py):

- `predicted_s` float4: seconds the model predicted for the leg (the leg's lattice path at the robot's factsheet speed_max plus the
  excess time of the charts it crosses, plus turn time). NULL: the leg was recorded before this change, has no matching trajectory
  entry (`plan_source` is not 'trajectory'), or the plan was made without a model (mode off, no usable statistics).
- `cost_model` text: `<model_hash>:<stamp>` of the snapshot the prediction came from.

Additive: two nullable columns without default (a catalog-only change, no rewrite), no backfill: older legs stay NULL. downgrade() drops
them (the recorded values are lost, the legs are not).
"""
from alembic import op

revision = "20261028_03_run_legs_predicted"
down_revision = "20261028_02_region_cost"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
SET LOCAL lock_timeout = '10s';
ALTER TABLE run_legs
  ADD COLUMN predicted_s float4,
  ADD COLUMN cost_model  text;
""")


def downgrade() -> None:
    op.execute("""
SET LOCAL lock_timeout = '10s';
ALTER TABLE run_legs
  DROP COLUMN IF EXISTS cost_model,
  DROP COLUMN IF EXISTS predicted_s;
""")
