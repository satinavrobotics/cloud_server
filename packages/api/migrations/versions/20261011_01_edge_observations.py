"""edge_observations (per-edge traversal statistics of the topomap)

Revision ID: 20261011_01_edge_observations
Revises: 20261010_01_robot_slam_saves
Create Date: 2026-10-11

One row per observed traversal of a directed topomap edge (from_node -> to_node, ArangoDB node
ids of `map_id`) by a robot, written when a mission run ends (mission-dispatch, from the run's
legs, `source = 'leg'`, `source_key = '<run_id>:<seq>'`) and when a mapping session finishes (the
API, from the session's nodes, `source = 'mapping'`, `source_key = '<session_id>:<step index>'`). The primary
key is the source's own id, so writing the same run or session twice changes nothing.

Only the newest 20 (config.EDGE_STATS_WINDOW) rows of each (map, from, to, robot) are kept
(packages/utils/edge_stats_db.py prunes after every write); the statistics (count, median / p90
time, median speed, recoveries, blocks) are computed from them on read. A plain table, outside
the 30-day sweep of robot_state_ts / diagnostics_ts and without a foreign key to the run (the
numbers outlive a deleted run). No node-id constraint: dispatch has no ArangoDB access, so the
API drops rows whose pair is not an edge when it reads.

Additive: one new table. downgrade() drops it (the statistics are lost, nothing else).
"""
from alembic import op

revision = "20261011_01_edge_observations"
down_revision = "20261010_01_robot_slam_saves"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
SET LOCAL lock_timeout = '10s';
CREATE TABLE IF NOT EXISTS edge_observations (
  map_id       text NOT NULL,
  from_node    text NOT NULL,
  to_node      text NOT NULL,
  robot_name   text NOT NULL,
  source       text NOT NULL CHECK (source IN ('leg', 'mapping')),
  source_key   text NOT NULL,
  observed_at  timestamptz NOT NULL,
  duration_s   double precision NOT NULL CHECK (duration_s > 0),
  distance_m   double precision,
  speed_mps    double precision,
  recoveries   int NOT NULL DEFAULT 0,
  blocks       int NOT NULL DEFAULT 0,
  PRIMARY KEY (map_id, from_node, to_node, robot_name, source, source_key)
);
CREATE INDEX IF NOT EXISTS edge_observations_robot_idx ON edge_observations (robot_name, map_id);  -- robot delete
""")


def downgrade() -> None:
    op.execute("""
SET LOCAL lock_timeout = '10s';
DROP TABLE IF EXISTS edge_observations;
""")
