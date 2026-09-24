#!/bin/bash
# WP7 item 4 (docs/satinav-fleet-agent-phase0-v2.md §7): the API with 2 uvicorn workers writes
# Phase 0 telemetry exactly once, and when the writer worker is killed the other takes over
# without duplicate or spurious events.
#
#   tests/integration/api_telemetry_failover/run.sh      (from the repo root; needs Docker)
#
# Everything runs on a private Docker network with NO published ports: a throwaway TimescaleDB
# (migrated by the API entrypoint's `alembic upgrade head`), mosquitto, ArangoDB and MinIO, the
# API image built from packages/api/Dockerfile under a test tag, and a synthetic robot. It never
# touches the production containers, ports (5432/1883/8000) or image tags. All containers and
# the network are removed on exit.
#
# Phases: (1) publish 15 s: exactly one worker holds the lock and has pool connections, the
# rows/events match the published sequence; (2) SIGKILL the writer, publish 15 s: the other
# worker took over; (3) restart, publish 20 s and SIGKILL the writer 6 s in; then verify.py
# recomputes every thermal/node transition from the stored diagnostics_ts rows and checks
# fleet_events is exactly that set, with no duplicate ids and strictly alternating NAV events.
set -euo pipefail
cd "$(dirname "$0")/../../.."
HERE=tests/integration/api_telemetry_failover
P=wp7it
NET=$P-net
IMG=$P-api:test
PG_PW=wp7test

cleanup() {
  docker rm -f $P-api $P-pg $P-mqtt $P-arango $P-minio $P-pub >/dev/null 2>&1 || true
  docker network rm $NET >/dev/null 2>&1 || true
}
trap cleanup EXIT
cleanup

q() { docker exec $P-pg psql -U postgres -d mission -tAc "$1"; }
HOLDER="SELECT a.application_name FROM pg_locks l JOIN pg_stat_activity a USING (pid)
        WHERE l.locktype='advisory' AND l.granted AND a.application_name LIKE 'api-telemetry-election-%'"
POOLS="SELECT string_agg(DISTINCT application_name, ',') FROM pg_stat_activity
       WHERE application_name ~ '^api-telemetry-[0-9]+$'"
publish() {  # seconds, first sample index
  docker run --rm --name $P-pub --network $NET -v "$PWD/$HERE":/it:ro --entrypoint python $IMG \
    /it/publish.py $P-mqtt "$1" "$2" 2>/dev/null
}
wait_for_workers() {
  for _ in $(seq 90); do
    n=$(q "SELECT count(*) FROM pg_stat_activity WHERE application_name LIKE 'api-telemetry-election-%'" || echo 0)
    [ "$n" = 2 ] && [ -n "$(q "$HOLDER")" ] && return 0
    sleep 1
  done
  return 1
}
kill_writer() {
  local pid; pid=$(q "$HOLDER"); pid=${pid##*-}
  echo "SIGKILL writer worker $pid"
  docker exec $P-api python -c "import os, signal; os.kill($pid, signal.SIGKILL)"
}

echo "== build $IMG"
docker build -q -t $IMG -f packages/api/Dockerfile . >/dev/null
docker network create $NET >/dev/null

echo "== start infrastructure"
docker run -d --name $P-pg --network $NET -e POSTGRES_PASSWORD=$PG_PW -e POSTGRES_DB=mission \
  timescale/timescaledb-ha:pg17.11-ts2.30.1 >/dev/null
docker run -d --name $P-mqtt --network $NET eclipse-mosquitto:2 mosquitto -c /mosquitto-no-auth.conf >/dev/null
docker run -d --name $P-arango --network $NET -e ARANGO_ROOT_PASSWORD=x arangodb/arangodb:latest >/dev/null
docker run -d --name $P-minio --network $NET -e MINIO_ROOT_USER=wp7minio -e MINIO_ROOT_PASSWORD=wp7minio123 \
  minio/minio:latest server /data >/dev/null
for _ in $(seq 60); do docker exec $P-pg pg_isready -U postgres -h localhost -q && break; sleep 1; done
sleep 3  # the image restarts postgres once after initdb
for _ in $(seq 30); do q "SELECT 1" >/dev/null 2>&1 && break; sleep 1; done
q "CREATE EXTENSION IF NOT EXISTS timescaledb" >/dev/null
# Global recording level 'full' so diagnostics_ts is written (§4.2 layer 3).
q "CREATE TABLE IF NOT EXISTS settingsobjectv1 (name VARCHAR(100) PRIMARY KEY NOT NULL,
   lifecycle VARCHAR(100) NOT NULL, spec jsonb NOT NULL, status jsonb NOT NULL)" >/dev/null
q "INSERT INTO settingsobjectv1 VALUES ('global','ALIVE','{\"telemetry_recording\":\"full\"}','{}')" >/dev/null

echo "== start the API with 2 workers"
docker run -d --name $P-api --network $NET \
  -e ARANGO_HOST=$P-arango -e ARANGO_PASSWORD=x \
  -e MINIO_HOST=$P-minio -e MINIO_ACCESS_KEY=wp7minio -e MINIO_SECRET_KEY=wp7minio123 \
  -e POSTGRES_PASSWORD=$PG_PW -e POSTGRES_DATABASE_HOST=$P-pg -e POSTGRES_DATABASE_PORT=5432 \
  -e POSTGRES_DATABASE_NAME=mission -e POSTGRES_DATABASE_USERNAME=postgres \
  -e POSTGRES_DATABASE_PASSWORD=$PG_PW -e MQTT_HOST=$P-mqtt -e MQTT_PORT=1883 -e MQTT_ENABLED=false \
  -e TELEMETRY_ELECTION_RETRY_S=1 -e TELEMETRY_ELECTION_CHECK_S=1 \
  $IMG python -m uvicorn packages.api.main:app --host 0.0.0.0 --port 18000 --workers 2 >/dev/null
if ! wait_for_workers; then
  # Two fresh workers can race creating the Arango database on a first start (pre-existing,
  # unrelated to this test); the database exists now, so a restart comes up cleanly.
  echo "   (worker failed on first start; restarting once)"
  docker restart $P-api >/dev/null
  wait_for_workers
fi
echo "   alembic: $(q "SELECT version_num FROM alembic_version")"
echo "   lock holder: $(q "$HOLDER"); ingest pools: $(q "$POOLS")"

echo "== phase 1: steady state"
publish 15 0
sleep 2
echo "   rows $(q "SELECT count(*) FROM diagnostics_ts") (60 published), events: $(q "SELECT string_agg(code || '=' || n, ' ') FROM (SELECT code, count(*) n FROM fleet_events GROUP BY 1 ORDER BY 1) s")"
[ "$(q "$POOLS" | tr ',' '\n' | wc -l)" = 1 ] || { echo "FAIL: more than one worker has ingest pools"; exit 1; }

echo "== phase 2: kill the writer between messages"
before=$(q "$HOLDER")
kill_writer
publish 15 60
sleep 2
after=$(q "$HOLDER")
echo "   holder $before -> $after; ingest pools: $(q "$POOLS")"
[ -n "$after" ] && [ "$after" != "$before" ] || { echo "FAIL: no takeover"; exit 1; }

echo "== phase 3: restart, kill the writer mid-stream"
docker restart $P-api >/dev/null
wait_for_workers
publish 20 1000 &
sleep 6
kill_writer
wait
sleep 2
echo "   holder: $(q "$HOLDER"); ingest pools: $(q "$POOLS")"

echo "== verify"
docker run --rm --network $NET -v "$PWD/$HERE":/it:ro --entrypoint python $IMG \
  /it/verify.py "host=$P-pg dbname=mission user=postgres password=$PG_PW" | tee /dev/stderr \
  | grep -q "RESULT: PASS"
echo "== PASS"
