"""run_cells.class_basis: allow 'period' (a run labelled from the factsheet period it started in)

Revision ID: 20261030_06_class_basis_period
Revises: 20261030_05_factsheet_periods
Create Date: 2026-10-30

20261030_05_factsheet_periods made run_cells label a run from its factsheet period (class_basis 'period'), but the CHECK from
20261028_02_region_cost still allowed only ('current', 'run'), so every summary of a robot with periods was refused
(CheckViolation). 'run' was the placeholder name for the same idea and was never written (live: only 'current'), so the CHECK
becomes ('current', 'period'). downgrade() puts the old CHECK back; it fails while 'period' rows exist (relabel them first).
"""
from alembic import op

revision = "20261030_06_class_basis_period"
down_revision = "20261030_05_factsheet_periods"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
SET LOCAL lock_timeout = '10s';
ALTER TABLE run_cells DROP CONSTRAINT run_cells_class_basis_check;
ALTER TABLE run_cells ADD CONSTRAINT run_cells_class_basis_check CHECK (class_basis IN ('current', 'period'));
""")


def downgrade() -> None:
    op.execute("""
SET LOCAL lock_timeout = '10s';
ALTER TABLE run_cells DROP CONSTRAINT run_cells_class_basis_check;
ALTER TABLE run_cells ADD CONSTRAINT run_cells_class_basis_check CHECK (class_basis IN ('current', 'run'));
""")
