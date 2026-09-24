"""Target tables and their columns (docs/satinav-fleet-agent-phase0-v2.md §3.2, §3.4, §3.5).

These mirror migration 20260924_01_phase0_core exactly; the migration is the source
of truth. `fleet_events` columns come from packages/events/emit.py.
"""

from typing import Dict, FrozenSet, Tuple

from packages.events.emit import TABLE as _EVENTS_TABLE
from packages.events.codes import Source

EVENTS_TABLE = _EVENTS_TABLE  # "fleet_events"
ROBOT_STATE_TABLE = "robot_state_ts"
DIAGNOSTICS_TABLE = "diagnostics_ts"
LATEST_TABLE = "robot_latest"

# §3.4, dispatch-written.
ROBOT_STATE_COLUMNS: Tuple[str, ...] = (
    "ts", "robot_name", "run_id", "x", "y", "yaw", "map_id",
    "battery", "state", "order_id", "last_node", "driving",
)
# §3.4, api-written.
DIAGNOSTICS_COLUMNS: Tuple[str, ...] = (
    "ts", "robot_name", "cpu", "gpu", "ram", "temp_max", "power_w", "nodes_down",
    "gnss_fix", "gnss_sats", "gnss_h_acc_m", "gnss_corr_age_s",
)
TIMESERIES_COLUMNS: Dict[str, Tuple[str, ...]] = {
    ROBOT_STATE_TABLE: ROBOT_STATE_COLUMNS,
    DIAGNOSTICS_TABLE: DIAGNOSTICS_COLUMNS,
}

# §3.5: each host upserts only its own columns. robot_name is the key and updated_at
# is always set to now() by the database.
LATEST_COLUMNS: Tuple[str, ...] = (
    "robot_name", "state_msg", "diagnostics", "nav_supervisor",
    "active_run_id", "site_id", "sw_version", "last_seen", "updated_at",
)
LATEST_JSONB_COLUMNS: FrozenSet[str] = frozenset({"state_msg", "diagnostics", "nav_supervisor"})
LATEST_OWNED_COLUMNS: Dict[Source, FrozenSet[str]] = {
    Source.DISPATCH: frozenset({"state_msg", "active_run_id", "site_id", "sw_version", "last_seen"}),
    Source.API: frozenset({"diagnostics", "nav_supervisor"}),
}


def copy_sql(table: str) -> str:
    """COPY statement for one time-series table."""
    return f"COPY {table} ({', '.join(TIMESERIES_COLUMNS[table])}) FROM STDIN"


def latest_upsert_sql(columns: Tuple[str, ...]) -> str:
    """Upsert of robot_latest touching only `columns` (plus updated_at).

    Parameters: robot_name, then one per column in `columns` order.
    """
    placeholders = ["%s"] + ["%s::jsonb" if c in LATEST_JSONB_COLUMNS else "%s" for c in columns]
    updates = [f"{c} = EXCLUDED.{c}" for c in columns] + ["updated_at = now()"]
    return (
        f"INSERT INTO {LATEST_TABLE} (robot_name, {', '.join(columns)}, updated_at) "
        f"VALUES ({', '.join(placeholders)}, now()) "
        f"ON CONFLICT (robot_name) DO UPDATE SET {', '.join(updates)}"
    )
