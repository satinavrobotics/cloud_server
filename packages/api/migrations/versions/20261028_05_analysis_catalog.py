"""analysis_catalog: the analyzer manifests' declared metadata, stored by the bag analysis worker

Revision ID: 20261028_05_analysis_catalog
Revises: 20261028_04_bag_analysis
Create Date: 2026-10-28

Why: the HTTP catalog was derived from stored results and so could not say a metric's unit, direction,
headline flag or description, nor an analyzer's title. The runner declares them in each analyzer manifest
(`run_analysis.py --list`); the worker upserts what it learns here and the API serves it.

One row per (analyzer, analyzer_version, decl_hash). decl_hash is the sha256 of the canonical JSON of the
metrics declaration. The same analyzer + version + declaration seen again only moves last_seen_at (and
refreshes the descriptive columns: title, owner, description, profiles, extractor_version, analysis_image,
tool_git_sha, which do not change what a number means). A CHANGED metrics declaration under the same
analyzer + version (a version bump forgotten upstream) is kept as a NEW row, never silently overwritten; the
API reads the newest-seen row per (analyzer, analyzer_version).

All values were validated and bounded by the worker (packages/services/bag_analysis/catalog.py) and are
untrusted text for any reader. `metrics` = {name: {unit, direction, description, headline}}.

downgrade() drops the table including its data (the worker re-learns it at its next start).
"""
from alembic import op

revision = "20261028_05_analysis_catalog"
down_revision = "20261028_04_bag_analysis"
branch_labels = None
depends_on = None

UPGRADE_SQL = """
SET LOCAL lock_timeout = '10s';
CREATE TABLE analysis_catalog (
  analyzer          text NOT NULL,
  analyzer_version  integer NOT NULL CHECK (analyzer_version >= 1),
  decl_hash         text NOT NULL CHECK (decl_hash ~ '^[0-9a-f]{64}$'),
  title             text,
  owner             text,
  description       text,
  profiles          text[] NOT NULL DEFAULT '{}',
  metrics           jsonb NOT NULL,
  extractor_version integer,
  analysis_image    text,
  tool_git_sha      text,
  first_seen_at     timestamptz NOT NULL DEFAULT now(),
  last_seen_at      timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (analyzer, analyzer_version, decl_hash)
);
CREATE INDEX analysis_catalog_latest_idx ON analysis_catalog (analyzer, analyzer_version DESC, last_seen_at DESC);
"""

DOWNGRADE_SQL = """
SET LOCAL lock_timeout = '10s';
DROP TABLE analysis_catalog;
"""


def upgrade() -> None:
    op.execute(UPGRADE_SQL)


def downgrade() -> None:
    op.execute(DOWNGRADE_SQL)
