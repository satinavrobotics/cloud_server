"""recorder_health: one row per recording process (WP13, GET /api/v1/health/recording)

Revision ID: 20260926_02_recorder_health
Revises: 20260926_01_run_archive
Create Date: 2026-09-26

mission-dispatch (fleet_recorder) and the API's elected telemetry writer each upsert their own
row every few seconds (packages/telemetry_ingest/health.py): queue depth/capacity, dropped
rows, spilled events pending and since when, flush ages, the heartbeat sweep lag (dispatch)
and the writer-election role (api). The API row also carries the alert evaluator's active
alerts (packages/api/recorder_health.py), so every API worker (and an API restart) sees the
same alert state. `reported_at` is always the database's now().

Two rows in total, rewritten in place: no hypertable, no retention. Nothing else reads or
writes it. downgrade() drops the table (only the current health snapshot is lost; the alert
history is in fleet_events).
"""
from alembic import op

revision = "20260926_02_recorder_health"
down_revision = "20260926_01_run_archive"
branch_labels = None
depends_on = None

PROCESSES = ("dispatch", "api")


def upgrade() -> None:
    allowed = ", ".join(f"'{p}'" for p in PROCESSES)
    op.execute(f"""
SET LOCAL lock_timeout = '10s';
CREATE TABLE recorder_health (
  process      text PRIMARY KEY,
  pid          int,
  hostname     text,
  role         text,
  started_at   timestamptz,
  reported_at  timestamptz NOT NULL DEFAULT now(),
  report       jsonb NOT NULL DEFAULT '{{}}',
  alerts       jsonb NOT NULL DEFAULT '[]',
  CONSTRAINT recorder_health_process_check CHECK (process IN ({allowed}))
);
""")


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '10s';\nDROP TABLE IF EXISTS recorder_health;\n")
