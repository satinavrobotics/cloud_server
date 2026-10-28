"""analysis_jobs + analysis_results + analysis_latest (bag analysis pipeline, server side)

Revision ID: 20261028_04_bag_analysis
Revises: 20261028_03_run_legs_predicted
Create Date: 2026-10-28

Contract and design: docs/bag_analysis_contract_2026-10-28.md, docs/bag_analysis_server_2026-10-28.md.

analysis_jobs: the work queue of packages/services/bag_analysis. One row asks "analyse this recording".
- analyzer_ids NULL = every analyzer that applies; otherwise 'id' or 'id:profile' entries. `scope` is the
  canonical text form of that choice ('*' or the sorted entries joined by ','), kept as a real column
  because array_to_string is not immutable and so cannot sit in an index.
- trigger: 'auto' (the scheduler, BAG_ANALYSIS_AUTO) | 'request' (an operator / the future API).
- status: queued -> running -> done | failed; cancelled is set by an operator. attempts counts claims.
  A job stuck 'running' (a crashed worker) is put back to 'queued' by the worker's startup sweep, or to
  'failed' once attempts reach the limit.
- Dedupe: UNIQUE (recording_id, trigger, scope) WHERE status IN ('queued', 'running'): the same work is
  never queued twice; finished jobs stay as history and do not block a new one.
- analysis_jobs_queue_idx serves the claim (oldest queued first, SELECT ... FOR UPDATE SKIP LOCKED).

analysis_results: one row per (recording, analyzer, analyzer_version, profile, extractor_version).
- metrics / tables / notes / producer / inputs / artifacts are the envelope's parts in queryable form,
  `envelope` is the validated result.json VERBATIM (what the analyzer wrote), so nothing is lost when a
  column is later added. artifacts[].path is relative to the object-store prefix documented in the doc.
- Insert only. A conflict on the key is a no-op (the stored result stays), EXCEPT that a stored row with
  status 'error' is replaced by a new one: an error says "the analysis failed or its output was invalid",
  and a re-run after the fix must be able to supersede it. An ok / warn / n/a row is never overwritten:
  a changed meaning is a new analyzer_version, a changed extractor a new extractor_version.
- Rows reference recordings ON DELETE CASCADE (the registry never deletes today; a manual cleanup of a
  recording removes its analysis rows, the stored artifacts in MinIO are not touched).

analysis_latest: per (recording, analyzer, profile) the newest result: highest analyzer_version, then
extractor_version, then created_at.

downgrade() drops the view and both tables INCLUDING THEIR DATA (re-run the analysis to rebuild it).
"""
from alembic import op

revision = "20261028_04_bag_analysis"
down_revision = "20261028_03_run_legs_predicted"
branch_labels = None
depends_on = None

UPGRADE_SQL = """
SET LOCAL lock_timeout = '10s';
CREATE TABLE analysis_jobs (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  recording_id uuid NOT NULL REFERENCES recordings (id) ON DELETE CASCADE,
  analyzer_ids text[],
  scope        text NOT NULL DEFAULT '*',
  requested_by text,
  trigger      text NOT NULL CHECK (trigger IN ('auto', 'request')),
  status       text NOT NULL DEFAULT 'queued'
               CHECK (status IN ('queued', 'running', 'done', 'failed', 'cancelled')),
  attempts     integer NOT NULL DEFAULT 0,
  created_at   timestamptz NOT NULL DEFAULT now(),
  started_at   timestamptz,
  finished_at  timestamptz,
  error        text,
  log_tail     text
);
CREATE UNIQUE INDEX analysis_jobs_active_key ON analysis_jobs (recording_id, trigger, scope)
  WHERE status IN ('queued', 'running');
CREATE INDEX analysis_jobs_queue_idx ON analysis_jobs (created_at, id) WHERE status = 'queued';
CREATE INDEX analysis_jobs_running_idx ON analysis_jobs (started_at) WHERE status = 'running';
CREATE INDEX analysis_jobs_recording_idx ON analysis_jobs (recording_id, created_at DESC);

CREATE TABLE analysis_results (
  recording_id      uuid NOT NULL REFERENCES recordings (id) ON DELETE CASCADE,
  analyzer          text NOT NULL,
  analyzer_version  integer NOT NULL,
  profile           text NOT NULL,
  extractor_version integer NOT NULL,
  status            text NOT NULL CHECK (status IN ('ok', 'warn', 'n/a', 'error')),
  metrics           jsonb NOT NULL DEFAULT '{}',
  tables            jsonb NOT NULL DEFAULT '{}',
  notes             jsonb NOT NULL DEFAULT '[]',
  producer          jsonb NOT NULL DEFAULT '{}',
  inputs            jsonb NOT NULL DEFAULT '{}',
  artifacts         jsonb NOT NULL DEFAULT '[]',
  envelope          jsonb NOT NULL,
  created_at        timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (recording_id, analyzer, analyzer_version, profile, extractor_version)
);

CREATE VIEW analysis_latest AS
SELECT DISTINCT ON (recording_id, analyzer, profile) *
FROM analysis_results
ORDER BY recording_id, analyzer, profile, analyzer_version DESC, extractor_version DESC, created_at DESC;
"""

DOWNGRADE_SQL = """
SET LOCAL lock_timeout = '10s';
DROP VIEW analysis_latest;
DROP TABLE analysis_results;
DROP TABLE analysis_jobs;
"""


def upgrade() -> None:
    op.execute(UPGRADE_SQL)


def downgrade() -> None:
    op.execute(DOWNGRADE_SQL)
