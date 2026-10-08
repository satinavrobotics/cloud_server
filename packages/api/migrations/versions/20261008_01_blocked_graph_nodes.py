"""blocked_graph_nodes (offline missions: blocked nodes are kept out of new routes)

Revision ID: 20261008_01_blocked_graph_nodes
Revises: 20261004_01_run_legs
Create Date: 2026-10-08

When a robot drops its order for a blocked node (an edgeBlocked error), mission-dispatch
writes its graph node here with an expiry (BLOCKED_NODE_EXCLUSION_MIN, default 10 minutes);
the mission planner leaves active rows out of the routes it plans, for every robot on the map.
An operator can list and clear them (GET/DELETE /api/v1/maps/{map}/blocked-nodes). nodeBlocked
notes are advisory and write nothing; the 'nodeBlocked' and 'operator' sources are reserved.

- Primary key (map_name, graph_node_id): one row per node; a new report extends its expiry.
  map_name holds the waypoint's map_id (the map key the planner and the API use).
- graph_node_id is "@x,y" (map frame, metres) when the waypoint's graph node is not known; the
  planner then leaves out graph nodes within BLOCKED_NODE_MATCH_RADIUS_M of that position.
- edge_from / edge_to: the graph nodes of the edge the robot was driving (for edge-level
  exclusion later); may be null.

Additive: one new table. downgrade() drops it (the exclusions are short-lived anyway).
"""
from alembic import op

revision = "20261008_01_blocked_graph_nodes"
down_revision = "20261004_01_run_legs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
SET LOCAL lock_timeout = '10s';
CREATE TABLE IF NOT EXISTS blocked_graph_nodes (
  map_name      text NOT NULL,
  graph_node_id text NOT NULL,
  edge_from     text,
  edge_to       text,
  source        text NOT NULL CHECK (source IN ('edgeBlocked', 'nodeBlocked', 'operator')),
  robot_name    text,
  mission_name  text,
  vda_node_id   text,
  reason        text,
  x             double precision,
  y             double precision,
  created_at    timestamptz NOT NULL DEFAULT now(),
  expires_at    timestamptz NOT NULL,
  PRIMARY KEY (map_name, graph_node_id)
);
CREATE INDEX IF NOT EXISTS blocked_graph_nodes_expiry_idx
  ON blocked_graph_nodes (map_name, expires_at);
""")


def downgrade() -> None:
    op.execute("""
SET LOCAL lock_timeout = '10s';
DROP TABLE IF EXISTS blocked_graph_nodes;
""")
