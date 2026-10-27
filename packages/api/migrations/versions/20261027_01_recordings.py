"""recordings (registry of robot recordings) + recording_runs / recording_legs / recording_events views

Revision ID: 20261027_01_recordings
Revises: 20261025_01_map_keepouts
Create Date: 2026-10-27

One row per stored recording part: a rosbag2 directory the robot uploaded (rosbags/<robot>/<bag_id>,
sidecar <bag_id>.json, packages/topomap_dbs/rosbag_db/server.py) or, later, an incident bag.

- kind: 'run' (full-run recording: nav bag + optional `_raw` bag + `_params/` + `_host.csv`, grouped by
  the bag prefix such as 20261018_190021_navstack_sati_odin) | 'incident' (sati_bag_recorder: a short
  [t - pre_roll, t + post_roll] bag with incident.yaml).
- during: the TRUE UTC interval, '[]' bounds. From the bag's own metadata.yaml (starting_time +
  duration); when that is unreadable, the sidecar's `recorded_at` (robot local time, Europe/Budapest)
  as a zero-length range (interval_source = 'sidecar_start_only' in `source`); 'empty' when even that
  is missing (overlaps nothing, interval_source = 'none').
- Dedupe rule: UNIQUE (robot_name, group_key, part). A bag uploaded two or three times (same
  description) is ONE row; the ingest keeps the upload that has its object, else the newest.
  group_key is the run prefix (incident: the bag directory name; a legacy bag with no such prefix: its
  bag_id), part is nav | raw | params | host | incident | bag (legacy). content_hash is the object's
  ETag (a hint, not a dedupe key: multipart ETags differ for identical bytes).
- run_id / order_id: the robot-recorded mission context, NULL until robot-side changes fill them.
  run_id is mission_runs.run_id, derived at ingest: the robot only knows the dispatcher's SHORT run id
  (8 hex) and mission_name, and mission_runs.run_id = fleet_recorder.run_uuid(mission_name, short)
  (packages/topomap_dbs/rosbag_db/recordings.mission_context); the raw block (incident.yaml `mission:`
  / the /sati/mission_context JSON) is kept whole in source.mission so the derivation can be redone.
  No foreign keys: association with mission_runs / run_legs / fleet_events is by TIME, computed by the
  views (a bag may span several runs, a run has several bags). `confirmed` = the robot named the run.
- CLOCK-SKEW TOLERANCE: the views widen the run / leg interval by 5 seconds on both sides before the
  overlap test (robot, dispatcher and recorder clocks differ); `overlap_s` is measured WITHOUT the
  widening, so a match by tolerance alone has overlap_s = 0. Change the literal in the three views
  together (tests/integration/test_recordings_postgres.py pins it).
- recording_events has no tolerance: the events whose ts lies inside `during` on the same robot.

The GiST index (robot_name, during) needs btree_gist, which phase0_core already creates;
CREATE EXTENSION IF NOT EXISTS is a no-op there and downgrade() leaves it alone.

downgrade() drops the views and the table INCLUDING ITS DATA (re-run the backfill to rebuild it).
"""
from alembic import op

revision = "20261027_01_recordings"
down_revision = "20261025_01_map_keepouts"
branch_labels = None
depends_on = None

UPGRADE_SQL = """
SET LOCAL lock_timeout = '10s';
CREATE EXTENSION IF NOT EXISTS btree_gist;
CREATE TABLE recordings (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  kind           text NOT NULL CHECK (kind IN ('run', 'incident')),
  robot_name     text NOT NULL,
  during         tstzrange NOT NULL,
  group_key      text NOT NULL,
  part           text NOT NULL,
  storage_bucket text NOT NULL,
  storage_key    text NOT NULL,
  size_bytes     bigint,
  content_hash   text,
  map_id         text,
  git_sha        text,
  image_tag      text,
  run_id         uuid,
  order_id       text,
  source         jsonb NOT NULL DEFAULT '{}',
  created_at     timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT recordings_group_part_key UNIQUE (robot_name, group_key, part)
);
CREATE INDEX recordings_robot_during_idx ON recordings USING gist (robot_name, during);
CREATE INDEX recordings_run_idx ON recordings (run_id) WHERE run_id IS NOT NULL;
CREATE INDEX recordings_order_idx ON recordings (order_id) WHERE order_id IS NOT NULL;
CREATE INDEX recordings_kind_created_idx ON recordings (kind, created_at DESC);

CREATE VIEW recording_runs AS
SELECT rec.id AS recording_id, rec.kind, rec.robot_name, rec.group_key, rec.part,
       r.run_id, r.mission_name, r.started_at AS run_started_at, r.ended_at AS run_ended_at,
       COALESCE(EXTRACT(EPOCH FROM upper(i) - lower(i)), 0)::double precision AS overlap_s,
       (rec.run_id IS NOT NULL AND rec.run_id = r.run_id) AS confirmed
FROM recordings rec
JOIN mission_runs r
  ON r.robot_name = rec.robot_name
 AND rec.during && tstzrange(r.started_at - interval '5 seconds',
                             COALESCE(r.ended_at, now()) + interval '5 seconds', '[]')
CROSS JOIN LATERAL (SELECT rec.during * tstzrange(r.started_at, COALESCE(r.ended_at, now()), '[]') AS i) x;

CREATE VIEW recording_legs AS
SELECT rec.id AS recording_id, rec.kind, rec.robot_name, rec.group_key, rec.part,
       l.run_id, l.seq, l.to_vda_node, l.started_at AS leg_started_at, l.ended_at AS leg_ended_at,
       COALESCE(EXTRACT(EPOCH FROM upper(i) - lower(i)), 0)::double precision AS overlap_s,
       (rec.run_id IS NOT NULL AND rec.run_id = l.run_id) AS confirmed
FROM recordings rec
JOIN run_legs l
  ON l.robot_name = rec.robot_name
 AND rec.during && tstzrange(l.started_at - interval '5 seconds',
                             l.ended_at + interval '5 seconds', '[]')
CROSS JOIN LATERAL (SELECT rec.during * tstzrange(l.started_at, l.ended_at, '[]') AS i) x;

CREATE VIEW recording_events AS
SELECT rec.id AS recording_id, rec.kind, rec.robot_name, rec.group_key, rec.part,
       e.ts, e.event_id, e.run_id, e.code, e.severity, e.payload
FROM recordings rec
JOIN fleet_events e ON e.robot_name = rec.robot_name AND e.ts <@ rec.during;
"""

DOWNGRADE_SQL = """
SET LOCAL lock_timeout = '10s';
DROP VIEW recording_events;
DROP VIEW recording_legs;
DROP VIEW recording_runs;
DROP TABLE recordings;
"""


def upgrade() -> None:
    op.execute(UPGRADE_SQL)


def downgrade() -> None:
    op.execute(DOWNGRADE_SQL)
