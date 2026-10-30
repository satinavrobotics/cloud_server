"""region_stats_inputs (per-map input generation) + region_stats_builds.inputs_gen

Revision ID: 20261030_04_region_inputs_gen
Revises: 20261030_03_stats_cleanup
Create Date: 2026-10-30

docs/stats_review_2026-10-30.md C4. A region build collects its run_cells outside its write transaction, so a delete (or archive) that
marks the build stale while the build is running could be overwritten by the build's unconditional 'ok'. The fix is a generation:

- region_stats_inputs (map_id PRIMARY KEY, gen bigint): bumped (+1, upsert) by packages/utils/region_builds.mark_stale for every map
  whose build inputs change (run delete, archive, restore, import undo, robot delete, new or recomputed run_cells row). A missing row
  means gen 0.
- region_stats_builds.inputs_gen: the gen the build was made from. The builder reads gen when it collects, re-reads it with
  SELECT ... FOR UPDATE in its write transaction, and does not write 'ok' if it moved.

Additive: one new table, one NOT NULL column with a constant default (catalog-only on PostgreSQL 11+, no rewrite). Existing builds get
inputs_gen 0, which equals the implicit gen of every map, so nothing is marked changed. map_delete must also delete the map's
region_stats_inputs row. downgrade() drops both (the generation is rebuildable state).
"""
from alembic import op

revision = "20261030_04_region_inputs_gen"
down_revision = "20261030_03_stats_cleanup"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
SET LOCAL lock_timeout = '10s';
CREATE TABLE IF NOT EXISTS region_stats_inputs (
  map_id text   PRIMARY KEY,
  gen    bigint NOT NULL DEFAULT 0
);
ALTER TABLE region_stats_builds ADD COLUMN IF NOT EXISTS inputs_gen bigint NOT NULL DEFAULT 0;
""")


def downgrade() -> None:
    op.execute("""
SET LOCAL lock_timeout = '10s';
ALTER TABLE region_stats_builds DROP COLUMN IF EXISTS inputs_gen;
DROP TABLE IF EXISTS region_stats_inputs;
""")
