"""idempotency: make idempotency_keys usable by the Idempotency-Key middleware (WP11 F3)

Revision ID: 20260925_01_idempotency
Revises: 20260924_01_phase0_core
Create Date: 2026-09-25

phase0_core created idempotency_keys (v2 §3.7) but nothing wrote to it. The middleware
(packages/api/idempotency.py) needs two things the table lacks:

- `completed_at`: NULL while the first request with a key is still running (the in-progress
  marker a concurrent duplicate sees), set together with response_status/response_body when
  it finishes. The CHECK keeps the three consistent.
- a default for `actor`, which is part of the primary key and so NOT NULL. The API has no
  authentication, so every key is stored with actor '' until it does.

Additive only: one nullable column, a default and a CHECK that every existing row (there are
none in production) satisfies. downgrade() removes exactly these.
"""
from alembic import op

revision = "20260925_01_idempotency"
down_revision = "20260924_01_phase0_core"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
SET LOCAL lock_timeout = '10s';

ALTER TABLE idempotency_keys ALTER COLUMN actor SET DEFAULT '';
ALTER TABLE idempotency_keys ADD COLUMN completed_at timestamptz;
ALTER TABLE idempotency_keys ADD CONSTRAINT idempotency_keys_completed_check
  CHECK ((completed_at IS NULL) = (response_status IS NULL));
""")


def downgrade() -> None:
    op.execute("""
SET LOCAL lock_timeout = '10s';

ALTER TABLE idempotency_keys DROP CONSTRAINT idempotency_keys_completed_check;
ALTER TABLE idempotency_keys DROP COLUMN completed_at;
ALTER TABLE idempotency_keys ALTER COLUMN actor DROP DEFAULT;
""")
