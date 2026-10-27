"""run_legs / edge_observations: driving time separated from waiting time

Revision ID: 20261027_04_leg_moving
Revises: 20261027_03_run_legs_plan
Create Date: 2026-10-27

Stage C of the edge statistics. A leg's duration_s was the whole time between two reached nodes: driving, waiting
(the robot reported driving false) and silence (no state messages) together, so a per-edge speed
(distance / duration) mixed all three. mission-dispatch now splits it (leg_tracker.py, module docstring):

- run_legs.silent_s: time in gaps between two state messages above 30 s (MAX_INTERVAL_S); the tracker knows nothing
  about it. stopped_s (non-driving time, an interval counts at most 30 s) is unchanged.
- run_legs.moving_s = max(0, duration_s - stopped_s - silent_s). Recovery time is NOT subtracted: it overlaps
  stopped time (and a recovery maneuver may report driving true).
- edge_observations.moving_s, stopped_s, recovery_s: the same figures copied from the leg, so the per-edge
  statistics (median_moving_s, moving_speed_mps) need no join. NULL for a mapping observation (it has no such split).

All five columns are nullable float8 without default (a catalog-only change, no rewrite). NULL on a leg = recorded
before this change (silent time unknown, so moving_s stays NULL; scripts/backfill_edge_observations.py
--fill-new-columns fills stopped_s / recovery_s of old leg observations from run_legs). downgrade() drops the
columns (the values are lost, the legs and observations are not).
"""
from alembic import op

revision = "20261027_04_leg_moving"
down_revision = "20261027_03_run_legs_plan"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
SET LOCAL lock_timeout = '10s';
ALTER TABLE run_legs
  ADD COLUMN moving_s float8,
  ADD COLUMN silent_s float8;
ALTER TABLE edge_observations
  ADD COLUMN moving_s   float8,
  ADD COLUMN stopped_s  float8,
  ADD COLUMN recovery_s float8;
""")


def downgrade() -> None:
    op.execute("""
SET LOCAL lock_timeout = '10s';
ALTER TABLE edge_observations
  DROP COLUMN IF EXISTS recovery_s,
  DROP COLUMN IF EXISTS stopped_s,
  DROP COLUMN IF EXISTS moving_s;
ALTER TABLE run_legs
  DROP COLUMN IF EXISTS silent_s,
  DROP COLUMN IF EXISTS moving_s;
""")
