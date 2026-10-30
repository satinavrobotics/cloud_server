"""robot_factsheet_periods (what each robot's factsheet said, from when)

Revision ID: 20261030_05_factsheet_periods
Revises: 20261030_04_region_inputs_gen
Create Date: 2026-10-30

docs/stats_review_2026-10-30.md 4.1 (W2-3), docs/region_stats_2026-10-27.md. A robot keeps its identity (robot_name) when its
speed profile changes; the class is the physical body (agv_class + footprint) and the speed limits are a per-run condition.
Until now every run was labelled from the robot's CURRENT factsheet when it was summarised (run_cells.class_basis 'current'),
so a speed change relabelled history wrongly. This table records the factsheet over time; a run is labelled from the period
it started in (latest valid_from <= started_at).

One row per CHANGE of agv_class / speed_max / angular_speed_max / acceleration_max / length / width (FleetRecorder.on_factsheet
writes it, source 'factsheet'; scripts/seed_factsheet_periods.py writes known history, source 'seed'). valid_from is the
factsheet's header timestamp (received time when absent or in the future). A figure the robot has not sent (-1, negative) is
stored NULL. No foreign key: the robot object is a generic object row, and robot delete removes the periods explicitly.

Additive: one new table. downgrade() drops it (the periods are lost; labels already written to run_cells stay).
"""
from alembic import op

revision = "20261030_05_factsheet_periods"
down_revision = "20261030_04_region_inputs_gen"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
SET LOCAL lock_timeout = '10s';
CREATE TABLE robot_factsheet_periods (
  robot_name           text NOT NULL,
  valid_from           timestamptz NOT NULL,
  agv_class            text,
  speed_max            double precision,
  angular_speed_max    double precision,
  acceleration_max     double precision,
  length               double precision,
  width                double precision,
  source               text NOT NULL CHECK (source IN ('factsheet', 'seed')),
  recorded_at          timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (robot_name, valid_from)
);
""")


def downgrade() -> None:
    op.execute("""
SET LOCAL lock_timeout = '10s';
DROP TABLE robot_factsheet_periods;
""")
