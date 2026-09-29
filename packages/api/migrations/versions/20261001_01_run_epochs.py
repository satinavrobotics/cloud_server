"""robot_run_epochs + map_sessions.run_epoch (maps redesign §14.13, placement reuse; U7)

Revision ID: 20261001_01_run_epochs
Revises: 20260930_01_maps_use
Create Date: 2026-10-01

docs/satinav-maps-redesign.md §14.13.

A local-map session's placement (map_T_session) is valid for the robot's run it was placed in.
"Use" reuses the placement of the robot's last session on the same map when the robot's run has
not changed since that session finished. For that:

- `robot_run_epochs` (one row per robot, written only by mission-dispatch): `epoch` uuid, renewed
  at every run change the dispatcher detects and whenever it cannot prove that the run it sees
  continues the one it saw before (first sight, a dispatcher restart without the header-id
  proof); `continuity_known` false from a dispatcher start until the robot's first state message
  decided; `last_state_header` / `last_state_at` the baseline of that proof (stored every ~10 s);
  `reason` / `evidence` / `started_at` for the operator.
- `map_sessions.run_epoch` uuid: the epoch a session was placed in, stamped when it finishes
  placed (NULL otherwise: never reused).

No foreign keys (robots are rows of robotobjectv1 by name, like robot_latest). Idempotent
(IF NOT EXISTS). downgrade() drops the column and the table: nothing else reads them, and
sessions finished meanwhile simply cannot be reused any more.
"""
from alembic import op

revision = "20261001_01_run_epochs"
down_revision = "20260930_01_maps_use"
branch_labels = None
depends_on = None

REASONS = ("run_changed", "first_seen", "dispatcher_restart")


def _upgrade_sql() -> str:
    reasons = ", ".join(f"'{r}'" for r in REASONS)
    return f"""
SET LOCAL lock_timeout = '10s';
CREATE TABLE IF NOT EXISTS robot_run_epochs (
  robot_name        text PRIMARY KEY,
  epoch             uuid NOT NULL,
  started_at        timestamptz NOT NULL DEFAULT now(),
  reason            text NOT NULL,
  evidence          jsonb,
  continuity_known  boolean NOT NULL DEFAULT true,
  last_state_header bigint,
  last_state_at     timestamptz,
  updated_at        timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE robot_run_epochs DROP CONSTRAINT IF EXISTS robot_run_epochs_reason_check;
ALTER TABLE robot_run_epochs ADD CONSTRAINT robot_run_epochs_reason_check
  CHECK (reason IN ({reasons}));
ALTER TABLE map_sessions ADD COLUMN IF NOT EXISTS run_epoch uuid;
"""


def _downgrade_sql() -> str:
    return """
SET LOCAL lock_timeout = '10s';
ALTER TABLE map_sessions DROP COLUMN IF EXISTS run_epoch;
DROP TABLE IF EXISTS robot_run_epochs;
"""


def upgrade() -> None:
    op.execute(_upgrade_sql())


def downgrade() -> None:
    op.execute(_downgrade_sql())
