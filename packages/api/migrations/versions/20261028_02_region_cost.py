"""run_cells / region_stats: robot class without speed_max, speed basis, dwell, speed-normalised sums

Revision ID: 20261028_02_region_cost
Revises: 20261028_01_run_cells
Create Date: 2026-10-28

docs/planner_region_stats_2026-10-27.md section 10 (decisions 2, 3, 5), docs/region_stats_2026-10-27.md section 12.

- run_cells: `speed_basis` ('factsheet' | 'placeholder' | 'unknown': how far the stored `speed_max` can be trusted, a
  placeholder being the 1.0 a robot reports before it is configured), `class_basis` ('current' | 'run': whose factsheet
  labelled the run; no factsheet history exists, so 'current'), and `dwell_s float4[]`, the seconds waited within
  RUN_CELLS_DWELL_RADIUS_M of the run's start, goal and action nodes per cell (algo_version 2), kept out of `stopped_s`.
  `robot_class` no longer contains speed_max ('<agv_class>|<length>x<width>').
- region_stats: `dwell_s` and the sums normalised by each run's own speed_max (`norm_runs`, `norm_len_m`,
  `norm_moving_m`, `norm_stopped_m`; see packages/utils/region_stats_load.py).

Both tables are caches of the run's raw rows (run_cells is recomputed by the API worker or scripts/backfill_run_cells.py
for any run whose row is not of the current RUN_CELLS_ALGO_VERSION; region_stats is rebuilt from run_cells), and the
summaries of algo_version 1 carry the old class string, so upgrade() deletes them (the worker re-creates them) and
empties region_stats and its builds. downgrade() drops the columns and deletes the algo_version 2 rows the same way.
"""
from alembic import op

revision = "20261028_02_region_cost"
down_revision = "20261028_01_run_cells"
branch_labels = None
depends_on = None

UPGRADE_SQL = """
SET LOCAL lock_timeout = '10s';
DELETE FROM run_cells WHERE algo_version < 2;
DELETE FROM region_stats_builds;
DELETE FROM region_stats;
ALTER TABLE run_cells
  ADD COLUMN speed_basis text NOT NULL DEFAULT 'factsheet',
  ADD COLUMN class_basis text NOT NULL DEFAULT 'current',
  ADD COLUMN dwell_s     float4[] NOT NULL DEFAULT '{}',
  ADD CONSTRAINT run_cells_speed_basis_check CHECK (speed_basis IN ('factsheet', 'placeholder', 'unknown')),
  ADD CONSTRAINT run_cells_class_basis_check CHECK (class_basis IN ('current', 'run'));
ALTER TABLE run_cells
  ALTER COLUMN speed_basis DROP DEFAULT,
  ALTER COLUMN class_basis DROP DEFAULT,
  ALTER COLUMN dwell_s DROP DEFAULT;
ALTER TABLE region_stats
  ADD COLUMN dwell_s        float8 NOT NULL DEFAULT 0,
  ADD COLUMN norm_runs      int    NOT NULL DEFAULT 0,
  ADD COLUMN norm_len_m     float8 NOT NULL DEFAULT 0,
  ADD COLUMN norm_moving_m  float8 NOT NULL DEFAULT 0,
  ADD COLUMN norm_stopped_m float8 NOT NULL DEFAULT 0;
"""

DOWNGRADE_SQL = """
SET LOCAL lock_timeout = '10s';
DELETE FROM run_cells WHERE algo_version >= 2;
DELETE FROM region_stats_builds;
DELETE FROM region_stats;
ALTER TABLE region_stats
  DROP COLUMN norm_stopped_m, DROP COLUMN norm_moving_m, DROP COLUMN norm_len_m, DROP COLUMN norm_runs,
  DROP COLUMN dwell_s;
ALTER TABLE run_cells
  DROP CONSTRAINT run_cells_class_basis_check, DROP CONSTRAINT run_cells_speed_basis_check,
  DROP COLUMN dwell_s, DROP COLUMN class_basis, DROP COLUMN speed_basis;
"""


def upgrade() -> None:
    op.execute(UPGRADE_SQL)


def downgrade() -> None:
    op.execute(DOWNGRADE_SQL)
