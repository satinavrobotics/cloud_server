"""run_cells (one cell summary per finished run), region_stats (+ builds): per-region driving statistics

Revision ID: 20261028_01_run_cells
Revises: 20261027_04_leg_moving
Create Date: 2026-10-28

docs/region_stats_2026-10-27.md, sections 1 and 4.

- run_cells: ONE row per (run, map): the run's map-frame movement rasterised into 0.1 m cells (the scene graph's
  lattice), as parallel arrays sorted by (ix, iy): length, time, stopped seconds, recovery seconds and blocks per
  cell. `ix, iy` are ABSOLUTE cell indices floor(x / cell_m), so the owner raster of any graph version can be
  looked up with no stored origin and the cells never change meaning when charts split or merge. Written once by
  the API's worker after the run ended (packages/api/run_cells.py); `algo_version` bumps recompute. Arrays rather
  than a row per cell keep a run to one (TOAST-compressed) row. The run row is referenced with ON DELETE CASCADE,
  so deleting a run (or its mission) deletes its summary.
- region_stats: the planner's lookup, per (map, graph_version, chart_key, robot_class) -- '*' = all classes pooled --
  built by grouping the run summaries by the owner raster of that graph version (packages/api/region_stats.py). A
  rebuildable cache: dropping it loses nothing.
- region_stats_builds: one row per (map, graph_version): what was built from what (`stamp` identifies the input set;
  status 'ok' | 'stale' | 'no_scene_graph').

Three new tables, no change to existing ones. downgrade() drops them (run_cells can be recomputed from the retained
track by scripts/backfill_run_cells.py --recompute).
"""
from alembic import op

revision = "20261028_01_run_cells"
down_revision = "20261027_04_leg_moving"
branch_labels = None
depends_on = None

UPGRADE_SQL = """
SET LOCAL lock_timeout = '10s';
CREATE TABLE run_cells (
  run_id         uuid        NOT NULL REFERENCES mission_runs(run_id) ON DELETE CASCADE,
  map_id         text        NOT NULL,
  robot_name     text        NOT NULL,
  robot_class    text        NOT NULL,
  speed_max      float4,
  started_at     timestamptz NOT NULL,
  ended_at       timestamptz,
  source         text        NOT NULL,
  algo_version   int2        NOT NULL,
  cell_m         float4      NOT NULL,
  params         jsonb       NOT NULL,
  frames         jsonb       NOT NULL DEFAULT '[]',
  ix             int4[]      NOT NULL,
  iy             int4[]      NOT NULL,
  len_m          float4[]    NOT NULL,
  time_s         float4[]    NOT NULL,
  stopped_s      float4[]    NOT NULL,
  recovery_s     float4[]    NOT NULL,
  blocks         int2[]      NOT NULL,
  totals         jsonb       NOT NULL,
  created_at     timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (run_id, map_id),
  CONSTRAINT run_cells_source_check CHECK (source IN ('track', 'legs', 'empty'))
);
CREATE INDEX run_cells_map_started_idx ON run_cells (map_id, started_at DESC);
CREATE INDEX run_cells_map_class_idx   ON run_cells (map_id, robot_class, started_at DESC);

CREATE TABLE region_stats (
  map_id         text        NOT NULL,
  graph_version  int         NOT NULL,
  chart_key      text        NOT NULL,
  robot_class    text        NOT NULL,
  runs           int         NOT NULL,
  len_m          float8      NOT NULL,
  time_s         float8      NOT NULL,
  stopped_s      float8      NOT NULL,
  recovery_s     float8      NOT NULL,
  blocks         int         NOT NULL,
  pace_median_s_per_m  float8,
  pace_p90_s_per_m     float8,
  moving_pace_median_s_per_m float8,
  moving_speed_mps     float8,
  stopped_share        float8,
  recovery_s_per_100m  float8,
  blocks_per_run       float8,
  speed_max            float8,
  cells          int         NOT NULL,
  visited        int         NOT NULL,
  first_at       timestamptz,
  last_at        timestamptz,
  stamp          text        NOT NULL,
  built_at       timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (map_id, graph_version, chart_key, robot_class)
);

CREATE TABLE region_stats_builds (
  map_id text NOT NULL, graph_version int NOT NULL, stamp text NOT NULL, runs int NOT NULL,
  status text NOT NULL, detail text, built_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (map_id, graph_version)
);
"""

DOWNGRADE_SQL = """
SET LOCAL lock_timeout = '10s';
DROP TABLE IF EXISTS region_stats_builds;
DROP TABLE IF EXISTS region_stats;
DROP TABLE IF EXISTS run_cells;
"""


def upgrade() -> None:
    op.execute(UPGRADE_SQL)


def downgrade() -> None:
    op.execute(DOWNGRADE_SQL)
