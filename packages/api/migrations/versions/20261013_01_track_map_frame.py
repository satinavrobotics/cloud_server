"""robot_track_ts: map-frame pose at record time; mission_runs: level override + level marks

Revision ID: 20261013_01_track_map_frame
Revises: 20261011_01_edge_observations
Create Date: 2026-10-13

Tracking redesign stage 1 (docs/tracking_redesign_2026-10-13.md, decision D2).

- robot_track_ts gains five NULLABLE columns, written by mission-dispatch with the row when the
  robot's open map session is placed (map_sessions.aligned) and its map_T_session is the one
  of the robot's current run: `x_map`, `y_map`, `theta_map` (the pose in the session's map
  frame, converted at record time), `map_session_id` (the session whose map_T_session did it)
  and `run_epoch` (robot_run_epochs.epoch the robot was in). All NULL for an unplaced robot and
  for every row recorded before this revision (the API then falls back to guessing one
  transform for the whole run, fleet_reads.track_transform). On the row rather than on
  mission_runs: a run can be re-placed or change session mid-run.
- mission_runs.level_override (boolean, default false): the run's recording_level comes from
  the mission or a one-off raise, so it holds for the whole run and not only its first
  segment (fleet_reads.level_segments).
- mission_runs.level_marks (jsonb, NULL until used): list of {ts, level} written by
  mission-dispatch when the robot's effective level changes while the run is open.

Rolling deploy, in this order (the new mission-dispatch writes the new columns and has no
fallback for mission_runs): 1. deploy the API (it runs this migration at start; the new /track
reads level_marks and tolerates old rows), 2. wait until the migration has run
(`alembic current` / the API healthy), 3. only then deploy mission-dispatch. A dispatch started
before the migration cannot insert run rows (level_override) or write level marks; only its
track rows survive, without the map-frame columns (the writer retries a COPY refused with
UndefinedColumn once with the old column list). Rolling back: dispatch first, then the API.

Additive and metadata-only. ADD COLUMN of a nullable column without a default on a compressed
hypertable is supported by TimescaleDB 2.30 (the compressed chunks get the column too; no chunk
is decompressed or rewritten), and the compression and retention policies are untouched. The
new columns are not in compress_segmentby / compress_orderby. mission_runs.level_override has a
constant default (PostgreSQL 11+: no table rewrite). The terminal-run trigger compares row
images, so a terminal run's new columns stay frozen like the others.

downgrade() drops the columns (the stored map-frame poses are lost).
"""
from alembic import op

revision = "20261013_01_track_map_frame"
down_revision = "20261011_01_edge_observations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
SET LOCAL lock_timeout = '10s';
ALTER TABLE robot_track_ts
  ADD COLUMN IF NOT EXISTS x_map           double precision,
  ADD COLUMN IF NOT EXISTS y_map           double precision,
  ADD COLUMN IF NOT EXISTS theta_map       double precision,
  ADD COLUMN IF NOT EXISTS map_session_id  uuid,
  ADD COLUMN IF NOT EXISTS run_epoch       uuid;
ALTER TABLE mission_runs
  ADD COLUMN IF NOT EXISTS level_override boolean NOT NULL DEFAULT false,
  ADD COLUMN IF NOT EXISTS level_marks    jsonb;
""")


def downgrade() -> None:
    op.execute("""
SET LOCAL lock_timeout = '10s';
ALTER TABLE robot_track_ts
  DROP COLUMN IF EXISTS x_map, DROP COLUMN IF EXISTS y_map, DROP COLUMN IF EXISTS theta_map,
  DROP COLUMN IF EXISTS map_session_id, DROP COLUMN IF EXISTS run_epoch;
ALTER TABLE mission_runs
  DROP COLUMN IF EXISTS level_override, DROP COLUMN IF EXISTS level_marks;
""")
