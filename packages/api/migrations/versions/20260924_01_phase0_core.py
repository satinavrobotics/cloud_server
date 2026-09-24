"""phase0_core: all Phase 0 tables (docs/satinav-fleet-agent-phase0-v2.md §3)

Revision ID: 20260924_01_phase0_core
Revises: 20260924_00_baseline
Create Date: 2026-09-24

Creates cause_codes (+ seed), mission_runs (+ immutability trigger), fleet_events,
robot_state_ts, diagnostics_ts (hypertables with compression/retention policies), the
robot_state_1m / diagnostics_1m continuous aggregates, robot_latest, robot_site_assignments,
audit_log, idempotency_keys, and mission_trajectory.run_id.

Safety model: fail fast, all-or-nothing. The whole revision runs in ONE transaction
(env.py: transaction_per_migration), including the alembic_version bump. Every CREATE is plain
(no IF NOT EXISTS), so a database that already has any of these objects without the matching
alembic_version aborts with DuplicateTable/DuplicateObject and nothing is left behind.
lock_timeout bounds how long it waits behind live traffic on mission_trajectory.

Not here: siteobjectv1 follows the runtime object-class convention (initialize_database).

downgrade() drops everything above, INCLUDING ITS DATA.
"""
from alembic import op

revision = "20260924_01_phase0_core"
down_revision = "20260924_00_baseline"
branch_labels = None
depends_on = None

# --- Value sets, kept in one place so a later migration can swap a CHECK constraint --------
# mission_runs.state: v2 §3.1 lists SUCCEEDED; decided 2026-09-24 to use COMPLETED instead, to
# match cloud_common/objects/mission.py::MissionStateV1.
RUN_ACTIVE_STATE = "RUNNING"
RUN_TERMINAL_STATES = ("COMPLETED", "FAILED", "CANCELED", "ABORTED", "TIMEOUT")
RUN_STATES = (RUN_ACTIVE_STATE,) + RUN_TERMINAL_STATES
RECORDING_LEVELS = ("full", "events_only", "off")
EVENT_SEVERITIES = ("info", "warning", "error", "critical")
EVENT_SOURCES = ("dispatch", "api")

# Same key as packages/database/postgres.py DB_INIT_LOCK_KEY (0x5A71DB): serializes our
# mission_trajectory CREATE with initialize_database in the other services on a fresh database.
DB_INIT_LOCK_KEY = 5927387

# (code, category, title, description): exactly packages/events/causes.py CAUSE_CODES
# (branch phase0/events), which requires the migration to insert these rows verbatim.
# Codes are append-only: never rename or reuse. category = the prefix before the dot.
CAUSE_CODES = (
    ('NAV.GOAL_UNREACHABLE', 'NAV', 'Goal unreachable', 'No path to the goal could be planned.'),
    ('NAV.RECOVERY_EXHAUSTED', 'NAV', 'Recovery exhausted', 'Navigation recovery behaviours ran out without success.'),
    ('NAV.PATH_BLOCKED', 'NAV', 'Path blocked', 'An edge on the route was blocked by an obstacle.'),
    ('NAV.LOCALIZATION_LOST', 'NAV', 'Localization lost', 'The robot lost its position estimate.'),
    ('GNSS.RTK_LOST', 'GNSS', 'RTK fix lost', 'GNSS dropped out of RTK fixed mode.'),
    ('GNSS.NO_FIX', 'GNSS', 'No GNSS fix', 'GNSS had no position fix.'),
    ('POWER.LOW_BATTERY', 'POWER', 'Low battery', 'Battery level too low to continue.'),
    ('COMMS.HEARTBEAT_LOST', 'COMMS', 'Heartbeat lost', 'The robot stopped reporting state within the heartbeat timeout.'),
    ('OPERATOR.CANCELED', 'OPERATOR', 'Canceled by operator', None),
    ('OPERATOR.ESTOP', 'OPERATOR', 'Emergency stop', 'An emergency stop was triggered.'),
    ('DISPATCH.TIMEOUT', 'DISPATCH', 'Mission timeout', 'The mission exceeded its time limit.'),
    ('DISPATCH.ORPHANED', 'DISPATCH', 'Orphaned run', "The robot no longer reported the run's order after a dispatcher restart."),
    ('DISPATCH.ORDER_REJECTED', 'DISPATCH', 'Order rejected', 'The robot rejected the VDA5050 order.'),
    ('MAP.INVALID', 'MAP', 'Invalid map', 'The map is missing or does not match.'),
    ('HW.FAULT', 'HW', 'Hardware fault', None),
    ('HW.MOTOR_FAULT', 'HW', 'Motor fault', None),
    ('HW.SENSOR_FAULT', 'HW', 'Sensor fault', None),
    ('SW.NODE_CRASH', 'SW', 'Software node crash', 'A robot software node died.'),
    ('SYSTEM.THERMAL', 'SYSTEM', 'Overheating', "The robot's compute overheated."),
    ('UNKNOWN', 'UNKNOWN', 'Unknown cause', 'No rule matched.'),
)


def _in_list(values) -> str:
    return ", ".join("NULL" if v is None else "'" + v.replace("'", "''") + "'" for v in values)


def _seed_values() -> str:
    return ",\n  ".join("(" + _in_list(row) + ")" for row in CAUSE_CODES)


def upgrade() -> None:
    op.execute(f"""
SET LOCAL lock_timeout = '10s';

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'timescaledb') THEN
    RAISE EXCEPTION 'phase0_core needs the timescaledb extension in this database '
                    '(pg17/TimescaleDB cutover runbook §4.3)';
  END IF;
END $$;

SELECT pg_advisory_xact_lock({DB_INIT_LOCK_KEY});

CREATE EXTENSION IF NOT EXISTS btree_gist;

-- §3.7 cause_codes (before mission_runs: FK target) --------------------------------------
CREATE TABLE cause_codes (
  code        text PRIMARY KEY,
  category    text NOT NULL,
  title       text NOT NULL,
  description text
);
INSERT INTO cause_codes (code, category, title, description) VALUES
  {_seed_values()};

-- §3.1 mission_runs ------------------------------------------------------------------------
CREATE TABLE mission_runs (
  run_id           uuid PRIMARY KEY,
  mission_name     text NOT NULL,
  robot_name       text NOT NULL,
  site_id          text,
  map_id           text,
  sw_version       text,
  recording_level  text NOT NULL,
  state            text NOT NULL,
  abort_cause      text REFERENCES cause_codes(code),
  abort_detail     jsonb,
  passes_completed int NOT NULL DEFAULT 0,
  created_by       text,
  mission_tree     jsonb NOT NULL,
  started_at       timestamptz NOT NULL,
  ended_at         timestamptz,
  summary_metrics  jsonb,
  CONSTRAINT mission_runs_state_check CHECK (state IN ({_in_list(RUN_STATES)})),
  CONSTRAINT mission_runs_recording_level_check CHECK (recording_level IN ({_in_list(RECORDING_LEVELS)})),
  CONSTRAINT mission_runs_ended_at_check CHECK ((state = '{RUN_ACTIVE_STATE}') = (ended_at IS NULL))
);
CREATE INDEX mission_runs_robot_started_idx ON mission_runs (robot_name, started_at DESC);
CREATE INDEX mission_runs_site_started_idx  ON mission_runs (site_id, started_at DESC);
CREATE INDEX mission_runs_sw_version_idx    ON mission_runs (sw_version);

-- Once terminal, only summary_metrics may change.
CREATE FUNCTION mission_runs_block_terminal_update() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF OLD.state <> '{RUN_ACTIVE_STATE}'
     AND (to_jsonb(NEW) - 'summary_metrics') IS DISTINCT FROM (to_jsonb(OLD) - 'summary_metrics') THEN
    RAISE EXCEPTION 'mission_runs %: run is terminal (%), only summary_metrics may change',
                    OLD.run_id, OLD.state
      USING ERRCODE = 'restrict_violation';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER mission_runs_immutable_when_terminal
  BEFORE UPDATE ON mission_runs
  FOR EACH ROW EXECUTE FUNCTION mission_runs_block_terminal_update();

-- §3.2 fleet_events: kept indefinitely, compressed after 14 days --------------------------
CREATE TABLE fleet_events (
  ts          timestamptz NOT NULL,
  event_id    uuid NOT NULL,
  robot_name  text,
  run_id      uuid,
  site_id     text,
  code        text NOT NULL,
  severity    text NOT NULL,
  sw_version  text,
  payload     jsonb NOT NULL DEFAULT '{{}}',
  source      text NOT NULL,
  CONSTRAINT fleet_events_event_id_ts_key UNIQUE (event_id, ts),
  CONSTRAINT fleet_events_severity_check CHECK (severity IN ({_in_list(EVENT_SEVERITIES)})),
  CONSTRAINT fleet_events_source_check CHECK (source IN ({_in_list(EVENT_SOURCES)}))
);
SELECT create_hypertable('fleet_events', by_range('ts', INTERVAL '7 days'));
CREATE INDEX fleet_events_robot_ts_idx ON fleet_events (robot_name, ts DESC);
CREATE INDEX fleet_events_code_ts_idx  ON fleet_events (code, ts DESC);
CREATE INDEX fleet_events_run_idx      ON fleet_events (run_id) WHERE run_id IS NOT NULL;
ALTER TABLE fleet_events SET (timescaledb.compress,
                              timescaledb.compress_segmentby = 'robot_name',
                              timescaledb.compress_orderby = 'ts DESC');
SELECT add_compression_policy('fleet_events', compress_after => INTERVAL '14 days');

-- §3.4 telemetry: raw 30 days, compressed after 3 days ------------------------------------
CREATE TABLE robot_state_ts (
  ts         timestamptz NOT NULL,
  robot_name text NOT NULL,
  run_id     uuid,
  x          double precision,
  y          double precision,
  yaw        double precision,
  map_id     text,
  battery    real,
  state      text,
  order_id   text,
  last_node  text,
  driving    boolean
);
SELECT create_hypertable('robot_state_ts', by_range('ts', INTERVAL '1 day'));
CREATE INDEX robot_state_ts_robot_ts_idx ON robot_state_ts (robot_name, ts DESC);
ALTER TABLE robot_state_ts SET (timescaledb.compress,
                                timescaledb.compress_segmentby = 'robot_name',
                                timescaledb.compress_orderby = 'ts DESC');
SELECT add_compression_policy('robot_state_ts', compress_after => INTERVAL '3 days');
SELECT add_retention_policy('robot_state_ts', drop_after => INTERVAL '30 days');

CREATE TABLE diagnostics_ts (
  ts              timestamptz NOT NULL,
  robot_name      text NOT NULL,
  cpu             real,
  gpu             real,
  ram             real,
  temp_max        real,
  power_w         real,
  nodes_down      int,
  gnss_fix        text,
  gnss_sats       smallint,
  gnss_h_acc_m    real,
  gnss_corr_age_s real
);
SELECT create_hypertable('diagnostics_ts', by_range('ts', INTERVAL '1 day'));
CREATE INDEX diagnostics_ts_robot_ts_idx ON diagnostics_ts (robot_name, ts DESC);
ALTER TABLE diagnostics_ts SET (timescaledb.compress,
                                timescaledb.compress_segmentby = 'robot_name',
                                timescaledb.compress_orderby = 'ts DESC');
SELECT add_compression_policy('diagnostics_ts', compress_after => INTERVAL '3 days');
SELECT add_retention_policy('diagnostics_ts', drop_after => INTERVAL '30 days');

-- §3.4 1-minute rollups, kept 2 years. The refresh window (2 days) stays well inside the
-- 30-day raw retention, so a refresh never sees dropped raw chunks and never erases rollups.
CREATE MATERIALIZED VIEW robot_state_1m WITH (timescaledb.continuous) AS
SELECT time_bucket(INTERVAL '1 minute', ts) AS bucket,
       robot_name,
       count(*)              AS samples,
       last(run_id, ts)      AS run_id,
       last(x, ts)           AS x,
       last(y, ts)           AS y,
       last(yaw, ts)         AS yaw,
       last(map_id, ts)      AS map_id,
       avg(battery)          AS battery_avg,
       min(battery)          AS battery_min,
       last(state, ts)       AS state,
       last(order_id, ts)    AS order_id,
       last(last_node, ts)   AS last_node,
       bool_or(driving)      AS driving
FROM robot_state_ts
GROUP BY bucket, robot_name
WITH NO DATA;
SELECT add_continuous_aggregate_policy('robot_state_1m',
  start_offset => INTERVAL '2 days', end_offset => INTERVAL '2 minutes',
  schedule_interval => INTERVAL '5 minutes');
SELECT add_retention_policy('robot_state_1m', drop_after => INTERVAL '2 years');

CREATE MATERIALIZED VIEW diagnostics_1m WITH (timescaledb.continuous) AS
SELECT time_bucket(INTERVAL '1 minute', ts) AS bucket,
       robot_name,
       count(*)                  AS samples,
       avg(cpu)                  AS cpu_avg,
       max(cpu)                  AS cpu_max,
       avg(gpu)                  AS gpu_avg,
       max(gpu)                  AS gpu_max,
       avg(ram)                  AS ram_avg,
       max(ram)                  AS ram_max,
       max(temp_max)             AS temp_max,
       avg(power_w)              AS power_w_avg,
       max(nodes_down)           AS nodes_down_max,
       last(gnss_fix, ts)        AS gnss_fix,
       min(gnss_sats)            AS gnss_sats_min,
       max(gnss_h_acc_m)         AS gnss_h_acc_m_max,
       max(gnss_corr_age_s)      AS gnss_corr_age_s_max
FROM diagnostics_ts
GROUP BY bucket, robot_name
WITH NO DATA;
SELECT add_continuous_aggregate_policy('diagnostics_1m',
  start_offset => INTERVAL '2 days', end_offset => INTERVAL '2 minutes',
  schedule_interval => INTERVAL '5 minutes');
SELECT add_retention_policy('diagnostics_1m', drop_after => INTERVAL '2 years');

-- §3.5 robot_latest -------------------------------------------------------------------------
CREATE TABLE robot_latest (
  robot_name     text PRIMARY KEY,
  state_msg      jsonb,
  diagnostics    jsonb,
  nav_supervisor jsonb,
  active_run_id  uuid,
  site_id        text,
  sw_version     text,
  last_seen      timestamptz,
  updated_at     timestamptz NOT NULL DEFAULT now()
);

-- §3.6 site assignment history --------------------------------------------------------------
CREATE TABLE robot_site_assignments (
  robot_name  text NOT NULL,
  site_id     text NOT NULL,
  valid       tstzrange NOT NULL,
  assigned_by text,
  CONSTRAINT robot_site_assignments_no_overlap EXCLUDE USING gist (robot_name WITH =, valid WITH &&)
);

-- §3.7 audit_log, idempotency_keys ------------------------------------------------------------
CREATE TABLE audit_log (
  id              bigserial PRIMARY KEY,
  ts              timestamptz NOT NULL DEFAULT now(),
  actor           text NOT NULL,
  actor_kind      text NOT NULL,
  method          text,
  route           text,
  resource_kind   text,
  resource_id     text,
  status          int,
  request_id      uuid,
  idempotency_key text,
  diff            jsonb
);

CREATE TABLE idempotency_keys (
  key             text,
  actor           text,
  route           text,
  request_hash    text NOT NULL,
  response_status int,
  response_body   jsonb,
  created_at      timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (key, actor, route)
);
CREATE INDEX idempotency_keys_created_at_idx ON idempotency_keys (created_at);

-- §3.1 mission_trajectory.run_id --------------------------------------------------------------
-- The table itself stays owned by initialize_database. It is only created here (same DDL) so a
-- fresh database, where the API migrates before any service has run initialize_database, can
-- take the new column. On an existing database this is a no-op.
CREATE TABLE IF NOT EXISTS mission_trajectory (
  id         SERIAL PRIMARY KEY,
  mission_id TEXT NOT NULL,
  robot_name TEXT NOT NULL,
  node_id    TEXT NOT NULL,
  seq        INTEGER NOT NULL,
  x          FLOAT NOT NULL,
  y          FLOAT NOT NULL,
  yaw        FLOAT NOT NULL,
  map_id     TEXT NOT NULL,
  ts         TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE mission_trajectory ADD COLUMN run_id uuid;
CREATE INDEX trajectory_run_idx ON mission_trajectory (run_id) WHERE run_id IS NOT NULL;
""")


def downgrade() -> None:
    # Drops all Phase 0 data. Continuous aggregates and hypertables take their policies/jobs
    # with them. mission_trajectory itself is kept (initialize_database owns it).
    op.execute("""
SET LOCAL lock_timeout = '10s';

DROP MATERIALIZED VIEW diagnostics_1m;
DROP MATERIALIZED VIEW robot_state_1m;
DROP TABLE diagnostics_ts;
DROP TABLE robot_state_ts;
DROP TABLE fleet_events;

DROP TABLE mission_runs;
DROP FUNCTION mission_runs_block_terminal_update();

DROP TABLE idempotency_keys;
DROP TABLE audit_log;
DROP TABLE robot_site_assignments;
DROP TABLE robot_latest;
DROP TABLE cause_codes;

DROP INDEX trajectory_run_idx;
ALTER TABLE mission_trajectory DROP COLUMN run_id;

-- Created by upgrade(); RESTRICT, so this fails loudly if anything else started using it.
DROP EXTENSION btree_gist;
""")
