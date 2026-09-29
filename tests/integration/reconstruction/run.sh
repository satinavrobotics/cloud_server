#!/usr/bin/env bash
# 3D reconstruction integration test (docs/reconstruction/design.md §11): R2 (graph-builder
# depth ingest over MQTT) and R3 (the gateway in the API, from this checkout) against a STUB
# reconstruction service (stub_service.py) that speaks the handover.md contract. Checks the
# plumbing: presigned URLs work from another container and carry RECONSTRUCTION_MINIO_ENDPOINT
# (MinIO is reached by the stub under the alias `minio-public`, by the API under its container
# name), staging copy, supersede, stale, cancel, map delete mid-job (late PUT and late callback
# harmless, no bucket re-created), stub down -> queued then service_unavailable, stub loses the
# job -> resubmit. Then the migration: downgrade -1 and upgrade again.
#
#   tests/integration/reconstruction/run.sh [TEST_IMAGE]
#
# Harness as tests/integration/maps/run_m2.sh: a private --internal network, no published
# ports, capped memory, every container under `timeout -s KILL`; Postgres (timescaledb-ha),
# ArangoDB, MinIO and mosquitto are the production images. Everything is removed on exit.
# Takes ~4 min (the lost-job poll waits the gateway's 60 s of silence).
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
IMAGE="${1:-wp6-test:py310}"
DB_IMAGE="timescale/timescaledb-ha:pg17.11-ts2.30.1"
PREV=20261002_01_drop_current_map
HEAD=20261003_01_map_reconstructions
ID="reconit-$$-$(date +%s)"
NET="$ID-net"
PW="rc-$(head -c 12 /dev/urandom | od -An -tx1 | tr -d ' \n')"
APW="a-$(head -c 8 /dev/urandom | od -An -tx1 | tr -d ' \n')"
MK="minio$(head -c 4 /dev/urandom | od -An -tx1 | tr -d ' \n')"
MS="minio-$(head -c 12 /dev/urandom | od -An -tx1 | tr -d ' \n')"
SK="stub-$(head -c 8 /dev/urandom | od -An -tx1 | tr -d ' \n')"
CS="cb-$(head -c 12 /dev/urandom | od -An -tx1 | tr -d ' \n')"
CONF="$(mktemp -d)"

cleanup() {
  status=$?
  if [ $status != 0 ]; then
    for c in api gb stub; do
      echo "--- $c log (tail)"; docker logs --tail 40 "$ID-$c" 2>&1 | sed 's/^/    /' || true
    done
  fi
  docker rm -f "$ID-db" "$ID-py" "$ID-arango" "$ID-minio" "$ID-mqtt" "$ID-gb" "$ID-api" \
    "$ID-stub" >/dev/null 2>&1 || true
  docker network rm "$NET" >/dev/null 2>&1 || true
  rm -rf "$CONF"
  exit $status
}
trap cleanup EXIT

ENVS=(-e PGHOST="$ID-db" -e PGPASSWORD="$PW" -e PGDATABASE=mission
      -e POSTGRES_DATABASE_HOST="$ID-db" -e POSTGRES_DATABASE_PORT=5432
      -e POSTGRES_DATABASE_NAME=mission -e POSTGRES_DATABASE_USERNAME=postgres
      -e POSTGRES_DATABASE_PASSWORD="$PW" -e POSTGRES_PASSWORD="$PW"
      -e ARANGO_HOST="$ID-arango" -e ARANGO_PORT=8529 -e ARANGO_PASSWORD="$APW"
      -e DATABASE_NAME=topomap_db
      -e MINIO_HOST="$ID-minio" -e MINIO_PORT=9000 -e MINIO_ACCESS_KEY="$MK" -e MINIO_SECRET_KEY="$MS"
      -e MQTT_HOST="$ID-mqtt" -e MQTT_PORT=1883 -e MQTT_BROKER="$ID-mqtt"
      -e API_URL="http://$ID-api:8000" -e STUB_URL="http://$ID-stub:8009"
      -e MINIO_PUBLIC="minio-public:9000" -e STUB_KEY="$SK"
      -e PYTHONPATH=/src -e PYTHONDONTWRITEBYTECODE=1 -e PYTHONUNBUFFERED=1)
API_ENVS=(-e RECONSTRUCTION_SERVICE_URL="http://$ID-stub:8009" -e RECONSTRUCTION_SERVICE_KEY="$SK"
          -e RECONSTRUCTION_CALLBACK_SECRET="$CS"
          -e RECONSTRUCTION_CALLBACK_BASE_URL="http://$ID-api:8000"
          -e RECONSTRUCTION_MINIO_ENDPOINT="minio-public:9000"
          -e RECONSTRUCTION_QUEUE_TIMEOUT_S=20
          -e GRAPH_BUILDER_WS_URL="ws://$ID-gb:8004" -e MQTT_ENABLED=false)
run_py() {  # run_py ARGS... : one-shot python container on the test network
  timeout -s KILL 600 docker run --rm --name "$ID-py" --network "$NET" --memory=1g --memory-swap=1g \
    --pids-limit=256 -v "$REPO":/src:ro -w /src "${ENVS[@]}" "$IMAGE" timeout -s KILL 300 "$@"
}
serve() {  # serve NAME ARGS... : a long-running python container on the test network
  local name=$1; shift
  docker run -d --name "$ID-$name" --network "$NET" --memory=1g --memory-swap=1g \
    --pids-limit=256 -v "$REPO":/src:ro -w /src "${ENVS[@]}" "$@" >/dev/null
}
psql_db() { docker exec "$ID-db" psql -X -v ON_ERROR_STOP=1 -U postgres -d mission "$@"; }
version() { psql_db -tAc "SELECT version_num FROM alembic_version"; }
step() { echo; echo "=== $* ==="; }
wait_log() {  # wait_log CONTAINER TEXT
  for _ in $(seq 90); do docker logs "$1" 2>&1 | grep -q "$2" && return 0; sleep 1; done
  echo "$1 did not log '$2'"; docker logs --tail 40 "$1"; return 1
}
checks() { run_py python tests/integration/reconstruction/checks.py "$@"; }

step "network + postgres, arangodb, minio (alias minio-public), mosquitto"
docker network create --internal "$NET" >/dev/null
docker run -d --name "$ID-db" --network "$NET" --memory=2g --memory-swap=2g --pids-limit=512 \
  -e POSTGRES_PASSWORD="$PW" -e POSTGRES_DB=mission -e TIMESCALEDB_TELEMETRY=off \
  -e TS_TUNE_MEMORY=1GB -e TS_TUNE_NUM_CPUS=1 --entrypoint timeout \
  "$DB_IMAGE" -s KILL 1200 /docker-entrypoint.sh postgres -c timescaledb.telemetry_level=off \
  -c timezone=UTC -c max_connections=100 >/dev/null
docker run -d --name "$ID-arango" --network "$NET" --memory=2g --memory-swap=2g --pids-limit=512 \
  -e ARANGO_ROOT_PASSWORD="$APW" --entrypoint timeout arangodb/arangodb:latest \
  -s KILL 1200 /entrypoint.sh arangod >/dev/null
docker run -d --name "$ID-minio" --network "$NET" --network-alias minio-public --memory=1g \
  --memory-swap=1g --pids-limit=256 -e MINIO_ROOT_USER="$MK" -e MINIO_ROOT_PASSWORD="$MS" \
  --entrypoint timeout minio/minio:latest -s KILL 1200 minio server /data >/dev/null
printf 'listener 1883\nallow_anonymous true\npersistence false\n' > "$CONF/mosquitto.conf"
chmod 644 "$CONF/mosquitto.conf"; chmod 755 "$CONF"
docker run -d --name "$ID-mqtt" --network "$NET" --memory=256m --memory-swap=256m --pids-limit=64 \
  -v "$CONF/mosquitto.conf":/mosquitto/config/mosquitto.conf:ro --entrypoint timeout \
  eclipse-mosquitto:latest -s KILL 1200 mosquitto -c /mosquitto/config/mosquitto.conf >/dev/null
for _ in $(seq 60); do
  if docker exec "$ID-db" pg_isready -U postgres -d mission >/dev/null 2>&1 &&
     psql_db -tAc "SELECT 1 FROM pg_extension WHERE extname='timescaledb'" 2>/dev/null | grep -q 1 &&
     docker exec "$ID-arango" arangosh --server.password "$APW" --javascript.execute-string \
       'db._version()' >/dev/null 2>&1; then
    break
  fi
  sleep 2
done
docker exec "$ID-arango" arangosh --server.password "$APW" --javascript.execute-string \
  'db._createDatabase("topomap_db")' >/dev/null

step "object tables (created at runtime by PostgresDatabase; U6's migration expects them)"
run_py python -c "import asyncio; from tests.integration.maps.checks import database; \
asyncio.run(database().async_init()); print('  object tables created')"

step "API (this checkout): the entrypoint migrates to head, then uvicorn"
serve api "${API_ENVS[@]}" "$IMAGE" timeout -s KILL 1100 python -m packages.api.entrypoint \
  python -m packages.api.main --host 0.0.0.0 --port 8000
wait_log "$ID-api" "Application startup complete"
[ "$(version)" = "$HEAD" ] || { echo "not at $HEAD: $(version)"; exit 1; }
echo "  migrated to $(version)"
wait_log "$ID-api" "Reconstruction dispatcher running"

step "graph-builder and the stub service (this checkout)"
serve gb "$IMAGE" timeout -s KILL 1100 python -m packages.services.graph_builder.main \
  --host 0.0.0.0 --port 8004
wait_log "$ID-gb" "Application startup complete"
docker run -d --name "$ID-stub" --network "$NET" --memory=512m --memory-swap=512m \
  --pids-limit=128 -v "$REPO":/src:ro -w /src "${ENVS[@]}" "$IMAGE" timeout -s KILL 1100 \
  python tests/integration/reconstruction/stub_service.py >/dev/null
sleep 3

step "seed: map, placed mapping session, depth over MQTT (R2)"
checks seed
step "build: the first reconstruction"
checks build
step "rebuild: supersede, then stale"
checks rebuild
step "cancel a running job"
checks cancel
step "service down: queued, then service_unavailable"
docker stop -t 2 "$ID-stub" >/dev/null
checks down
docker start "$ID-stub" >/dev/null
step "the service loses the job: poll 404 -> resubmit (waits ~60 s)"
checks lost
step "map delete mid-job"
checks delete
docker logs "$ID-api" 2>&1 | grep -q Traceback && { echo "traceback in the API log"; \
  docker logs "$ID-api" 2>&1 | grep -B5 -A20 Traceback | head -80; exit 1; }
docker logs "$ID-gb" 2>&1 | grep -q Traceback && { echo "traceback in graph-builder"; exit 1; }

step "migration: downgrade -1 (-> $PREV), upgrade again"
docker rm -f "$ID-api" "$ID-gb" >/dev/null
run_py alembic -c packages/api/alembic.ini downgrade -1
[ "$(version)" = "$PREV" ] || { echo "not at $PREV"; exit 1; }
[ "$(psql_db -tAc "SELECT to_regclass('map_reconstructions') IS NULL")" = t ] || { echo "table left"; exit 1; }
[ "$(psql_db -tAc "SELECT count(*) FROM fleet_events WHERE source = 'reconstruction'")" = 0 ] || exit 1
echo "  ok: table dropped, reconstruction events removed"
run_py alembic -c packages/api/alembic.ini upgrade head
[ "$(version)" = "$HEAD" ] || { echo "not at $HEAD"; exit 1; }
echo "  ok: upgraded again"

step "PASSED"
