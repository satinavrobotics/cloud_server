"""robotobjectv1: drop spec.current_map (maps redesign §14, U6)

Revision ID: 20261002_01_drop_current_map
Revises: 20261001_01_run_epochs
Create Date: 2026-10-02

docs/satinav-maps-redesign.md §14.3, §14.14.

A robot's map is its one open session (map_sessions, §14.2). U6 removes `current_map` from the
robot model (RobotSpecV1) and every reader and writer; PUT /api/v1/robots/{r}/map answers 410.
The model would ignore the stale key on read (pydantic v1, Extra.ignore), but partial spec
writes (`spec || patch`) would carry it forever and raw-SQL readers could still see it, so the
key is removed from every robot row. Nothing else changes; no NOTIFY is needed (no service
reads the field any more).

Idempotent (`spec ? 'current_map'` selects only rows that still have the key).

downgrade() restores nothing that was lost on purpose: it writes `current_map` = the map of
the robot's OPEN session, for robots that have one (what the pre-U6 code would have shown for
them); the others stay without the key, which the pre-U6 model reads as null (mapless). The old
`GEO` / `LOCAL` sentinels are not recreated.
"""
from alembic import op

revision = "20261002_01_drop_current_map"
down_revision = "20261001_01_run_epochs"
branch_labels = None
depends_on = None


def _upgrade_sql() -> str:
    return """
SET LOCAL lock_timeout = '10s';
UPDATE robotobjectv1 SET spec = spec - 'current_map' WHERE spec ? 'current_map';
"""


def _downgrade_sql() -> str:
    return """
SET LOCAL lock_timeout = '10s';
UPDATE robotobjectv1 r
   SET spec = r.spec || jsonb_build_object('current_map', s.map_name)
  FROM map_sessions s
 WHERE s.robot_name = r.name AND s.ended_at IS NULL;
"""


def upgrade() -> None:
    op.execute(_upgrade_sql())


def downgrade() -> None:
    op.execute(_downgrade_sql())
