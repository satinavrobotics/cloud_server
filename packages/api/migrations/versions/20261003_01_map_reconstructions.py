"""map_reconstructions + fleet_events.source 'reconstruction' (3D reconstruction R3)

Revision ID: 20261003_01_map_reconstructions
Revises: 20261002_01_drop_current_map
Create Date: 2026-10-03

docs/reconstruction/design.md §8.1.

One row per reconstruction job of a map (the gateway in packages/api/reconstruction.py owns
them). At most one active (queued/running) and one current (succeeded) job per map, by partial
unique indexes. No foreign key to mapobjectv1 (created at runtime, as for map_sessions); the
map delete removes the rows (packages/api/map_delete.py).

Beyond §8.1: `cancel_requested_at` (the 60 s cancel grace, §6.6) and `frames_done` /
`frames_total` (the progress the status reports, §9.1).

The fleet_events source CHECK is widened with 'reconstruction' (MAP.RECONSTRUCTION_*), as
20260929_01_maps_m2 did for 'graph_builder'.

downgrade() drops the table (the reconstruction files in MinIO stay; a later upgrade does not
know them and the API's startup orphan sweep removes them), deletes the 'reconstruction'
events and restores the previous CHECK.
"""
from alembic import op

revision = "20261003_01_map_reconstructions"
down_revision = "20261002_01_drop_current_map"
branch_labels = None
depends_on = None

OLD_SOURCES = ("dispatch", "api", "graph_builder")
NEW_SOURCES = ("dispatch", "api", "graph_builder", "reconstruction")
CONSTRAINT = "fleet_events_source_check"
STATES = ("queued", "running", "succeeded", "failed", "cancelled", "superseded")


def _in_list(values) -> str:
    return ", ".join(f"'{v}'" for v in values)


def _swap(sources) -> str:
    return f"""
ALTER TABLE fleet_events DROP CONSTRAINT IF EXISTS {CONSTRAINT};
ALTER TABLE fleet_events ADD CONSTRAINT {CONSTRAINT} CHECK (source IN ({_in_list(sources)}));
"""


def _upgrade_sql() -> str:
    return f"""
SET LOCAL lock_timeout = '10s';
CREATE TABLE IF NOT EXISTS map_reconstructions (
  job_id              uuid PRIMARY KEY,
  map_name            text NOT NULL,
  state               text NOT NULL CHECK (state IN ({_in_list(STATES)})),
  requested_at        timestamptz NOT NULL DEFAULT now(),
  requested_by        text,
  attempts            smallint NOT NULL DEFAULT 0,
  next_try_at         timestamptz,
  dispatched_at       timestamptz,
  started_at          timestamptz,
  finished_at         timestamptz,
  last_contact_at     timestamptz,
  cancel_requested    boolean NOT NULL DEFAULT false,
  cancel_requested_at timestamptz,
  stage               text,
  progress            real NOT NULL DEFAULT 0,
  frames_done         integer,
  frames_total        integer,
  params              jsonb NOT NULL DEFAULT '{{}}'::jsonb,
  inputs              jsonb,
  result              jsonb,
  artifacts           jsonb,
  error               jsonb
);
CREATE UNIQUE INDEX IF NOT EXISTS map_reconstructions_one_active
  ON map_reconstructions (map_name) WHERE state IN ('queued', 'running');
CREATE UNIQUE INDEX IF NOT EXISTS map_reconstructions_one_current
  ON map_reconstructions (map_name) WHERE state = 'succeeded';
CREATE INDEX IF NOT EXISTS map_reconstructions_by_map
  ON map_reconstructions (map_name, requested_at DESC);
CREATE INDEX IF NOT EXISTS map_reconstructions_active
  ON map_reconstructions (state) WHERE state IN ('queued', 'running');
{_swap(NEW_SOURCES)}"""


def _downgrade_sql() -> str:
    return f"""
SET LOCAL lock_timeout = '10s';
DROP TABLE IF EXISTS map_reconstructions;
DELETE FROM fleet_events WHERE source = 'reconstruction';
{_swap(OLD_SOURCES)}"""


def upgrade() -> None:
    op.execute(_upgrade_sql())


def downgrade() -> None:
    op.execute(_downgrade_sql())
