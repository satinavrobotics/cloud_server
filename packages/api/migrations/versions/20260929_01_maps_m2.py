"""fleet_events.source 'graph_builder' (maps redesign M2)

Revision ID: 20260929_01_maps_m2
Revises: 20260928_01_map_sessions
Create Date: 2026-09-29

docs/satinav-maps-redesign.md §6, §13.2.

graph-builder writes MAP.INGEST_REJECTED (packages/services/graph_builder/ingest.py) with
source 'graph_builder', which fleet_events_source_check (20260924_01_phase0_core) does not
allow yet. The CHECK is swapped for one that does. fleet_events is a hypertable with
compression enabled; TimescaleDB propagates the constraint to every chunk, compressed ones
included (checked on timescaledb-ha pg17.11-ts2.30.1, the production image).

Nothing else in M2 needs a schema change: map_sessions (M1) already has node_count and
map_t_session; the legacy node rewrite is ArangoDB data (tools/maps_m2_legacy_nodes.py).

downgrade() deletes the graph_builder rows (only rejection notices) and restores the old CHECK.
"""
from alembic import op

revision = "20260929_01_maps_m2"
down_revision = "20260928_01_map_sessions"
branch_labels = None
depends_on = None

OLD_SOURCES = ("dispatch", "api")
NEW_SOURCES = ("dispatch", "api", "graph_builder")
CONSTRAINT = "fleet_events_source_check"


def _in_list(values) -> str:
    return ", ".join(f"'{v}'" for v in values)


def _swap(sources) -> str:
    return f"""
SET LOCAL lock_timeout = '10s';
ALTER TABLE fleet_events DROP CONSTRAINT IF EXISTS {CONSTRAINT};
ALTER TABLE fleet_events ADD CONSTRAINT {CONSTRAINT} CHECK (source IN ({_in_list(sources)}));
"""


def upgrade() -> None:
    op.execute(_swap(NEW_SOURCES))


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '10s';\n"
               "DELETE FROM fleet_events WHERE source = 'graph_builder';")
    op.execute(_swap(OLD_SOURCES))
