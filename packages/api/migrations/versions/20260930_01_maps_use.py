"""map_sessions: purpose, services, placement (maps redesign §14, operate sessions; U1)

Revision ID: 20260930_01_maps_use
Revises: 20260929_01_maps_m2
Create Date: 2026-09-30

docs/satinav-maps-redesign.md §14.2, §14.4.

A robot uses a map through its one open session (map_sessions_one_open_per_robot, unchanged),
whose `purpose` is 'mapping' (adds data, as before) or 'operate' (uses the map, adds nothing).

- `purpose`   text NOT NULL DEFAULT 'mapping' (every existing row is a mapping session);
- `services`  text[]: the mapping services a mapping session switches on (e.g. {topo});
              NULL for operate. Existing mapping rows get {topo} (what M3 ran);
- `placement` jsonb: how a local-map session was put on the map ({pose, robot_pose, source,
              actor, at}; plus unplaced_reason/unplaced_at once a run change invalidates it).

Constraints: purpose in (mapping, operate); a legacy session is a mapping session; only mapping
sessions have services. `kind` (live/legacy) stays provenance. The unique indexes are unchanged.
`aligned` keeps its column and now means "placed": map_T_session is valid for the robot's
current run. Open local sessions that are not aligned (later M1/M2 sessions on a local map)
stay unplaced and capture nothing until placed; the deploy script lists them.

Idempotent (IF NOT EXISTS / DROP ... IF EXISTS before ADD). downgrade() deletes the operate
sessions (they own no nodes; their robots become mapless), then drops the constraints and the
columns. The mapping sessions stay as M3 knew them.
"""
from alembic import op

revision = "20260930_01_maps_use"
down_revision = "20260929_01_maps_m2"
branch_labels = None
depends_on = None

PURPOSES = ("mapping", "operate")
CONSTRAINTS = ("map_sessions_purpose_check", "map_sessions_legacy_mapping_check",
               "map_sessions_services_check")


def _in_list(values) -> str:
    return ", ".join(f"'{v}'" for v in values)


def _upgrade_sql() -> str:
    drops = "\n".join(f"ALTER TABLE map_sessions DROP CONSTRAINT IF EXISTS {c};"
                      for c in CONSTRAINTS)
    return f"""
SET LOCAL lock_timeout = '10s';
ALTER TABLE map_sessions ADD COLUMN IF NOT EXISTS purpose text NOT NULL DEFAULT 'mapping';
ALTER TABLE map_sessions ADD COLUMN IF NOT EXISTS services text[];
ALTER TABLE map_sessions ADD COLUMN IF NOT EXISTS placement jsonb;
UPDATE map_sessions SET services = '{{topo}}' WHERE purpose = 'mapping' AND services IS NULL;
{drops}
ALTER TABLE map_sessions ADD CONSTRAINT map_sessions_purpose_check
  CHECK (purpose IN ({_in_list(PURPOSES)}));
ALTER TABLE map_sessions ADD CONSTRAINT map_sessions_legacy_mapping_check
  CHECK (kind <> 'legacy' OR purpose = 'mapping');
ALTER TABLE map_sessions ADD CONSTRAINT map_sessions_services_check
  CHECK (purpose = 'mapping' OR services IS NULL);
"""


def _downgrade_sql() -> str:
    drops = "\n".join(f"ALTER TABLE map_sessions DROP CONSTRAINT IF EXISTS {c};"
                      for c in CONSTRAINTS)
    return f"""
SET LOCAL lock_timeout = '10s';
DELETE FROM map_sessions WHERE purpose = 'operate';
{drops}
ALTER TABLE map_sessions DROP COLUMN IF EXISTS placement;
ALTER TABLE map_sessions DROP COLUMN IF EXISTS services;
ALTER TABLE map_sessions DROP COLUMN IF EXISTS purpose;
"""


def upgrade() -> None:
    op.execute(_upgrade_sql())


def downgrade() -> None:
    op.execute(_downgrade_sql())
