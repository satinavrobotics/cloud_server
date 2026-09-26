"""run_archive: mission_runs.archived_at (archive / restore runs, POST /api/v1/runs/archive)

Revision ID: 20260926_01_run_archive
Revises: 20260925_01_idempotency
Create Date: 2026-09-26

- `mission_runs.archived_at timestamptz NULL`: set when a run is archived, NULL again when it is
  restored. Archived runs are hidden from GET /api/v1/runs by default and nothing else changes.
- mission_runs_block_terminal_update() (phase0_core) lets only summary_metrics change on a
  terminal run. Archiving is exactly a change to a terminal run, so the function now ignores
  archived_at as well. Everything else about a terminal run stays immutable.

No index: mission_runs has one row per run (thousands, not millions), every list query is
already ordered by (started_at, run_id), and `archived_at IS NULL` is a cheap filter on that
scan. Add a partial index only if the list query ever shows up as slow.

Additive: one nullable column (no rewrite, no default) and a function body swap. downgrade()
restores the phase0_core function and drops the column (archive state is lost; runs are not).
"""
from alembic import op

revision = "20260926_01_run_archive"
down_revision = "20260925_01_idempotency"
branch_labels = None
depends_on = None

RUN_ACTIVE_STATE = "RUNNING"


def _function(ignored: str, may_change: str) -> str:
    return f"""
CREATE OR REPLACE FUNCTION mission_runs_block_terminal_update() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF OLD.state <> '{RUN_ACTIVE_STATE}'
     AND (to_jsonb(NEW) {ignored}) IS DISTINCT FROM (to_jsonb(OLD) {ignored}) THEN
    RAISE EXCEPTION 'mission_runs %: run is terminal (%), only {may_change} may change',
                    OLD.run_id, OLD.state
      USING ERRCODE = 'restrict_violation';
  END IF;
  RETURN NEW;
END $$;
"""


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '10s';\n"
               "ALTER TABLE mission_runs ADD COLUMN archived_at timestamptz;\n"
               + _function("- 'summary_metrics' - 'archived_at'",
                         "summary_metrics and archived_at"))


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '10s';\n"
               + _function("- 'summary_metrics'", "summary_metrics")
               + "ALTER TABLE mission_runs DROP COLUMN archived_at;\n")
