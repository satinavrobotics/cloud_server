"""robot_track_ts (run_id, ts): the index the per-run reads join and filter by

Revision ID: 20261030_02_track_run_idx
Revises: 20261030_01_run_legs_map_idx
Create Date: 2026-10-30

docs/stats_review_2026-10-30.md P4. Path efficiency, run cells and the track fetch select the track of one run (`WHERE run_id = ...`,
usually with a ts bound); the only index so far is (robot_name, ts DESC), so a run's rows were found by scanning every chunk in range.

robot_track_ts is a hypertable (1-day chunks, compressed after 3 days, dropped after 1 year; migration 20261009_01_robot_track).
CREATE INDEX on a hypertable builds the index on every chunk; on a compressed chunk TimescaleDB indexes the uncompressed part
(empty for a fully compressed chunk) and the compressed chunk keeps its own segment index, so compressed chunks do not block it. The
index on a compressed chunk does not help reads of that chunk's compressed rows: they are filtered after decompression, segmented by
robot_name; the benefit is on the recent uncompressed chunks and on the planner pruning by the index-backed ts range.

Cost on the live table: one index build per chunk. A plain CREATE INDEX holds a SHARE lock on the hypertable and all its chunks for the
whole build (ingest of robot_track_ts, i.e. the recorder, waits). `timescaledb.transaction_per_chunk` builds chunk by chunk, each in its
own transaction, which keeps every lock short, but it cannot run inside a transaction block (TimescaleDB refuses it). So upgrade()
runs it in alembic's autocommit_block(): the revision's open transaction is committed first, the statement runs in autocommit, and
alembic_version is bumped afterwards by alembic's own statement in a new transaction. If the build is interrupted, an INVALID index may
be left on the parent: `DROP INDEX robot_track_ts_run_ts_idx` and run the upgrade again. IF NOT EXISTS makes a finished run idempotent.

Estimate the cost before the upgrade (read-only, on the live DB):
  SELECT pg_size_pretty(hypertable_size('robot_track_ts'));                      -- table + indexes + compressed
  SELECT count(*) FILTER (WHERE NOT is_compressed), count(*) FROM timescaledb_information.chunks WHERE hypertable_name = 'robot_track_ts';
The new index is roughly 40-50 bytes per UNCOMPRESSED row (uuid + timestamptz btree entry); build time is roughly sorting that many
rows, on the order of a few seconds per million rows. Rows in compressed chunks are not in the index. Run the upgrade in a quiet
period if the uncompressed part is more than a few million rows. downgrade() drops the index (IF EXISTS).
"""
from alembic import op

revision = "20261030_02_track_run_idx"
down_revision = "20261030_01_run_legs_map_idx"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # No lock_timeout here: SET LOCAL needs a transaction, and each chunk's lock is short anyway.
    with op.get_context().autocommit_block():
        op.execute("""
CREATE INDEX IF NOT EXISTS robot_track_ts_run_ts_idx ON robot_track_ts (run_id, ts)
  WITH (timescaledb.transaction_per_chunk);
""")


def downgrade() -> None:
    op.execute("""
SET LOCAL lock_timeout = '10s';
DROP INDEX IF EXISTS robot_track_ts_run_ts_idx;
""")
