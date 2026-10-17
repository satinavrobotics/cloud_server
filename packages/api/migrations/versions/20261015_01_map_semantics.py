"""map_semantics + fleet_events.source 'semantic' (semantic layer gateway)

Revision ID: 20261015_01_map_semantics
Revises: 20261014_01_run_cause_codes
Create Date: 2026-10-17

docs/semantic/design.md. One row per semantic job of a map (the gateway in
packages/api/semantic.py owns them), the same shape as map_reconstructions: at most one
active (queued/running) and one current (succeeded) job per map by partial unique indexes, no
foreign key to mapobjectv1 (the map delete removes the rows, packages/api/map_delete.py).

The fleet_events source CHECK is widened with 'semantic' (MAP.SEMANTIC_*); every source allowed
at the previous head is kept.

downgrade() drops the table (the files in MinIO stay; a later upgrade does not know them and
the API's startup orphan sweep removes them), deletes the 'semantic' events and restores the
previous CHECK.
"""
from alembic import op

revision = "20261015_01_map_semantics"
down_revision = "20261014_01_run_cause_codes"
branch_labels = None
depends_on = None

OLD_SOURCES = ("dispatch", "api", "graph_builder", "reconstruction")
NEW_SOURCES = OLD_SOURCES + ("semantic",)
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
CREATE TABLE IF NOT EXISTS map_semantics (
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
CREATE UNIQUE INDEX IF NOT EXISTS map_semantics_one_active
  ON map_semantics (map_name) WHERE state IN ('queued', 'running');
CREATE UNIQUE INDEX IF NOT EXISTS map_semantics_one_current
  ON map_semantics (map_name) WHERE state = 'succeeded';
CREATE INDEX IF NOT EXISTS map_semantics_by_map
  ON map_semantics (map_name, requested_at DESC);
CREATE INDEX IF NOT EXISTS map_semantics_active
  ON map_semantics (state) WHERE state IN ('queued', 'running');
{_swap(NEW_SOURCES)}"""


def _downgrade_sql() -> str:
    return f"""
SET LOCAL lock_timeout = '10s';
DROP TABLE IF EXISTS map_semantics;
DELETE FROM fleet_events WHERE source = 'semantic';
{_swap(OLD_SOURCES)}"""


def upgrade() -> None:
    op.execute(_upgrade_sql())


def downgrade() -> None:
    op.execute(_downgrade_sql())
