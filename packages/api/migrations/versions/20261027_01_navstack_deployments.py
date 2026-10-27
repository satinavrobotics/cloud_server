"""navstack_deployments (deploy log of the navstack image on a robot) + robot_software_periods view

Revision ID: 20261027_01_navstack_deployments
Revises: 20261027_01_recordings
Create Date: 2026-10-27

One row per deploy / rollback / pull-only request made through the server (packages/api/navstack_deploy.py,
docs/navstack_deploy.md). The job updates the row once per step; `steps` is the list of
{name, state, started_at, finished_at, detail} (plus the orchestrator operation id of a pull or switch step,
kept there so a restarted API resumes polling it).

- kind: deploy | rollback | pull.   mode: image | dev (a pull is always image).
- state: queued | preflight | pull | switch | verify | done | failed | failed_verify | cancelled.
  The first five are non-final. At most ONE non-final row per robot: the partial unique index
  navstack_deployments_one_running (a second request gets 409 instead of racing).
- to_tag / to_git_sha: the target (NULL tag for dev mode). from_tag / from_sw_version: what the robot reported
  when the request was made (from_sw_version = robot_latest.sw_version verbatim, the dev string included).
- requested_by: the X-Sati-Operator header as the client sent it. UNVERIFIED (the API has no auth).

robot_software_periods: when each build ran, from the `done` deploy / rollback rows (a pull changes nothing that
runs, a failed or cancelled row is not a period). valid_from is the finished_at of the row (the verify step
saw the new build id then; the switch itself was up to ~2 minutes earlier), valid_to the next done row's, NULL
for the current period. Recordings and runs made in dev mode, or before this log existed, keep their own
recordings.image_tag / git_sha and mission_runs.sw_version columns: join with COALESCE, this view is the
fallback-free source only for what was deployed through the server.

downgrade() drops the view and the table INCLUDING THE DEPLOY LOG.
"""
from alembic import op

revision = "20261027_01_navstack_deployments"
down_revision = "20261027_01_recordings"
branch_labels = None
depends_on = None

UPGRADE_SQL = """
SET LOCAL lock_timeout = '10s';
CREATE TABLE navstack_deployments (
  id               uuid PRIMARY KEY,
  robot_name       text NOT NULL,
  kind             text NOT NULL CHECK (kind IN ('deploy', 'rollback', 'pull')),
  mode             text NOT NULL CHECK (mode IN ('image', 'dev')),
  to_tag           text,
  from_tag         text,
  from_sw_version  text,
  to_git_sha       text,
  state            text NOT NULL CHECK (state IN ('queued', 'preflight', 'pull', 'switch', 'verify',
                                                  'done', 'failed', 'failed_verify', 'cancelled')),
  steps            jsonb NOT NULL DEFAULT '[]',
  error            text,
  requested_by     text,
  requested_at     timestamptz NOT NULL DEFAULT now(),
  started_at       timestamptz,
  finished_at      timestamptz
);
CREATE UNIQUE INDEX navstack_deployments_one_running ON navstack_deployments (robot_name)
  WHERE state NOT IN ('done', 'failed', 'failed_verify', 'cancelled');
CREATE INDEX navstack_deployments_robot_idx ON navstack_deployments (robot_name, requested_at DESC);

CREATE VIEW robot_software_periods AS
SELECT robot_name, mode, to_tag AS tag, to_git_sha AS git_sha, id AS deployment_id,
       finished_at AS valid_from,
       lead(finished_at) OVER (PARTITION BY robot_name ORDER BY finished_at) AS valid_to
FROM navstack_deployments
WHERE state = 'done' AND kind IN ('deploy', 'rollback');
"""

DOWNGRADE_SQL = """
SET LOCAL lock_timeout = '10s';
DROP VIEW robot_software_periods;
DROP TABLE navstack_deployments;
"""


def upgrade() -> None:
    op.execute(UPGRADE_SQL)


def downgrade() -> None:
    op.execute(DOWNGRADE_SQL)
