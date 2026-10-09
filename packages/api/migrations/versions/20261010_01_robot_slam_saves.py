"""robot_slam_saves (the per-robot SLAM save state survives an API restart)

Revision ID: 20261010_01_robot_slam_saves
Revises: 20261009_01_robot_track
Create Date: 2026-10-10

One row per robot that records a SLAM map for a mapping session, saves it, or whose save failed
(packages/api/slam_save_state.py; the API's MappingSwitch is the only writer). `prev_intent` is the
robot's localization intent before the recording started (what a save or a discard puts back).
After an API restart a `saving` row becomes `failed` (the outcome is unknown) and the robot view's
`slam_save` offers retry / discard.

Additive: one new table. downgrade() drops it (the API then keeps the state in memory only).
"""
from alembic import op

revision = "20261010_01_robot_slam_saves"
down_revision = "20261009_01_robot_track"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
SET LOCAL lock_timeout = '10s';
CREATE TABLE IF NOT EXISTS robot_slam_saves (
  robot_name  text PRIMARY KEY,
  map_name    text,
  session_id  text,
  state       text NOT NULL CHECK (state IN ('recording', 'saving', 'failed')),
  detail      text,
  at          text,
  prev_intent jsonb,
  updated_at  timestamptz NOT NULL DEFAULT now()
);
""")


def downgrade() -> None:
    op.execute("""
SET LOCAL lock_timeout = '10s';
DROP TABLE IF EXISTS robot_slam_saves;
""")
