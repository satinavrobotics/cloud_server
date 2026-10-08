"""run_legs + mission_runs.planned_path (mission/run analysis, phases 1-2)

Revision ID: 20261004_01_run_legs
Revises: 20261003_01_map_reconstructions
Create Date: 2026-10-04

A leg is one robot move from one topomap node to the next. mission-dispatch writes one row per
leg from the robot's VDA5050 state (each change of lastNodeId), stamped with the robot's own
header time (`started_at` / `ended_at`) and with the dispatcher's receive time
(`received_started_at` / `received_ended_at`).

- run_legs: primary key (run_id, seq), seq = leg order in the run (1-based). A plain table, not
  a hypertable: like mission_runs and fleet_events it is kept indefinitely, so no retention
  policy applies (the 30-day sweep is only on robot_state_ts / diagnostics_ts). Legs are
  deleted with their run (ON DELETE CASCADE: DELETE /api/v1/missions and run_admin delete the
  mission_runs rows).
- `recoveries`, `recovery_s`, `blocks` are filled when the run ends, from the NAV.RECOVERY_* /
  NAV.GOAL_BLOCKED / MISSION.EDGE_BLOCKED events that carry the leg in their payload
  (`leg_seq`).
- mission_runs.planned_path jsonb: the mission's planned_path (topomap node ids) copied onto the
  run when it starts. Written at insert only; the terminal-run trigger is untouched.

Additive: one new table and one nullable column (no rewrite). downgrade() drops both (the leg
data is lost, runs are not).
"""
from alembic import op

revision = "20261004_01_run_legs"
down_revision = "20261003_01_map_reconstructions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
SET LOCAL lock_timeout = '10s';
ALTER TABLE mission_runs ADD COLUMN planned_path jsonb;
CREATE TABLE run_legs (
  run_id              uuid NOT NULL REFERENCES mission_runs(run_id) ON DELETE CASCADE,
  seq                 int NOT NULL,
  mission_name        text NOT NULL,
  robot_name          text NOT NULL,
  pass_index          int NOT NULL DEFAULT 0,
  order_rev           int NOT NULL DEFAULT 0,
  from_vda_node       text,
  to_vda_node         text NOT NULL,
  from_topomap_node   text,
  to_topomap_node     text,
  map_id              text,
  started_at          timestamptz NOT NULL,
  ended_at            timestamptz NOT NULL,
  received_started_at timestamptz,
  received_ended_at   timestamptz,
  duration_s          double precision NOT NULL,
  stopped_s           double precision NOT NULL DEFAULT 0,
  straight_m          double precision,
  planned_m           double precision,
  expected_s          double precision,
  recoveries          int NOT NULL DEFAULT 0,
  recovery_s          double precision NOT NULL DEFAULT 0,
  blocks              int NOT NULL DEFAULT 0,
  PRIMARY KEY (run_id, seq)
);
CREATE INDEX run_legs_mission_idx ON run_legs (mission_name, started_at DESC);
CREATE INDEX run_legs_robot_idx   ON run_legs (robot_name, started_at DESC);
""")


def downgrade() -> None:
    op.execute("""
SET LOCAL lock_timeout = '10s';
DROP TABLE run_legs;
ALTER TABLE mission_runs DROP COLUMN planned_path;
""")
