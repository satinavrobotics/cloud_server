"""robot_track_ts + the 'track' recording level (mission/run analysis, Track level)

Revision ID: 20261009_01_robot_track
Revises: 20261008_01_blocked_graph_nodes
Create Date: 2026-10-09

The recording ladder grows to off < events_only < track < full. At `track` and above
mission-dispatch writes one row per second per robot while a mission runs: pose and speed from
the robot's VDA5050 state (`agvPosition`, `velocity`), tagged with the run and the leg in
progress.

- robot_track_ts: hypertable on ts (1-day chunks), compressed after 3 days, dropped after
  1 year. Pose is the robot's run frame, as in robot_state_ts; the API converts it to the
  map frame when it serves a run's track. `speed` = |(vx, vy)| in m/s, `omega` in rad/s,
  `leg_seq` = run_legs.seq of the leg in progress (NULL between legs). A DELETE of a run's
  rows works on compressed chunks too (run_admin does it).
- mission_runs.recording_level accepts 'track' (the CHECK is replaced; no row changes, and the
  terminal-run trigger only fires on UPDATE).

Additive. downgrade() drops the table (its data is lost) and restores the CHECK; it fails if a
run recorded at 'track' exists (the terminal-run trigger forbids rewriting it).
"""
from alembic import op

revision = "20261009_01_robot_track"
down_revision = "20261008_01_blocked_graph_nodes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
SET LOCAL lock_timeout = '10s';
CREATE TABLE robot_track_ts (
  ts         timestamptz NOT NULL,
  robot_name text NOT NULL,
  run_id     uuid,
  leg_seq    int,
  x          double precision,
  y          double precision,
  theta      double precision,
  speed      real,
  omega      real,
  map_id     text
);
SELECT create_hypertable('robot_track_ts', by_range('ts', INTERVAL '1 day'));
CREATE INDEX robot_track_ts_robot_ts_idx ON robot_track_ts (robot_name, ts DESC);
ALTER TABLE robot_track_ts SET (timescaledb.compress,
                                timescaledb.compress_segmentby = 'robot_name',
                                timescaledb.compress_orderby = 'ts DESC');
SELECT add_compression_policy('robot_track_ts', compress_after => INTERVAL '3 days');
SELECT add_retention_policy('robot_track_ts', drop_after => INTERVAL '1 year');

ALTER TABLE mission_runs DROP CONSTRAINT mission_runs_recording_level_check;
ALTER TABLE mission_runs ADD CONSTRAINT mission_runs_recording_level_check
  CHECK (recording_level IN ('full', 'track', 'events_only', 'off'));
""")


def downgrade() -> None:
    op.execute("""
SET LOCAL lock_timeout = '10s';
ALTER TABLE mission_runs DROP CONSTRAINT mission_runs_recording_level_check;
ALTER TABLE mission_runs ADD CONSTRAINT mission_runs_recording_level_check
  CHECK (recording_level IN ('full', 'events_only', 'off'));
DROP TABLE robot_track_ts;
""")
