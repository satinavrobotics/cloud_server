"""edge_observations: index for the windowed read (raw observations are kept)

Revision ID: 20261027_02_edge_obs_history
Revises: 20261027_01_navstack_deployments
Create Date: 2026-10-27

Stage A of the edge statistics: edge_observations is append-only. The writers no longer prune to
the newest 20 (config.EDGE_STATS_WINDOW) rows of each (map, from, to, robot); the window is applied
on read (packages/utils/edge_stats_db.fetch: row_number() over (from, to[, robot]) ordered by
observed_at DESC, source DESC, source_key DESC, keep rn <= window), and rows older than
EDGE_STATS_RETENTION_DAYS (default 365, 0 = forever) are deleted in batches by mission-dispatch.

The read needs the rows of one map grouped by edge and robot and newest first inside each group:
(map_id, from_node, to_node, robot_name, observed_at DESC). The primary key has the same prefix but
ends in (source, source_key), which is not the time order, so it cannot serve the window. The
tie-break columns are left out on purpose (ties on the same millisecond are rare; Postgres sorts
only the tied rows, an incremental sort). The pooled read (per edge across robots) reads the map's
rows and sorts per edge; its result is cached.

Index creation: plain CREATE INDEX inside the revision's transaction, as every migration here
(CONCURRENTLY cannot run in it), with a lock_timeout so it fails fast instead of queueing behind a
writer. The table held at most 20 rows per edge and robot until now, so the build is milliseconds;
it blocks the run-end writer only for that long.

The old migration's docstring says only the newest 20 are kept: superseded by this one.
Additive: one index. downgrade() drops it (history already written stays; nothing is lost).
"""
from alembic import op

revision = "20261027_02_edge_obs_history"
down_revision = "20261027_01_navstack_deployments"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
SET LOCAL lock_timeout = '10s';
CREATE INDEX IF NOT EXISTS edge_observations_history_idx
  ON edge_observations (map_id, from_node, to_node, robot_name, observed_at DESC);
""")


def downgrade() -> None:
    op.execute("""
SET LOCAL lock_timeout = '10s';
DROP INDEX IF EXISTS edge_observations_history_idx;
""")
