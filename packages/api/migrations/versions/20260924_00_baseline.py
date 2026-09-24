"""baseline: the schema as of the pg17/TimescaleDB cutover (2026-09-24)

Revision ID: 20260924_00_baseline
Revises:
Create Date: 2026-09-24

Intentionally empty. The existing tables (robotobjectv1, missionobjectv1, mapobjectv1,
settingsobjectv1, detectionresultsobjectv1, mission_trajectory) are created at runtime by
packages/database/postgres.py::initialize_database and stay owned by it.

Existing databases are marked with `alembic stamp 20260924_00_baseline`; an empty upgrade to
it is equivalent, so a fresh database needs no stamp.
"""

revision = "20260924_00_baseline"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
