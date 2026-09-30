#!/usr/bin/env bash
# Maps redesign M2 integration test (docs/satinav-maps-redesign.md §6, §12, §13.2): migration
# 20260929_01_maps_m2, tools.maps_m2_legacy_nodes (dry run, apply, idempotent re-run, revert,
# re-apply), and graph-builder ingesting over MQTT by mapping session, driven by session
# start/finish (the PUT /robots/{r}/map shim until U6), pause/resume, rejections and
# MAP.INGEST_REJECTED.
#
#   tests/integration/maps/run_m2.sh [--dump PG_DUMP --arango-dump DIR] [TEST_IMAGE]
#
# Without dumps: a fresh database migrated to the M1 revision with the live map `map` (seed of
# checks.py) and its 5 legacy nodes (checks_m2.py seed). With --dump (a read-only `pg_dump -Fc`
# of production, at the M1 revision) and --arango-dump (a read-only `arangodump` of topomap_db):
# the rehearsal on production data.
#
# Harness as run.sh: a private --internal network, no published ports, capped memory, every
# container under `timeout -s KILL`; Postgres (timescaledb-ha), ArangoDB, MinIO and mosquitto are
# the production images; graph-builder runs from this checkout in the test image. Everything is
# removed on exit.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
DUMP=""; ADUMP=""
while [ "${1:-}" = --dump ] || [ "${1:-}" = --arango-dump ]; do
  case $1 in
    --dump) DUMP="$(cd "$(dirname "$2")" && pwd)/$(basename "$2")" ;;
    --arango-dump) ADUMP="$(cd "$2" && pwd)" ;;
  esac
  shift 2
done
IMAGE="${1:-wp6-test:py310}"
DB_IMAGE="timescale/timescaledb-ha:pg17.11-ts2.30.1"
PREV=20260926_02_recorder_health
M1=20260928_01_map_sessions
HEAD=20260929_01_maps_m2
ID="m2it-$$-$(date +%s)"
NET="$ID-net"
PW="m2-$(head -c 12 /dev/urandom | od -An -tx1 | tr -d ' \n')"
APW="a-$(head -c 8 /dev/urandom | od -An -tx1 | tr -d ' \n')"
MK="minio$(head -c 4 /dev/urandom | od -An -tx1 | tr -d ' \n')"
MS="minio-$(head -c 12 /dev/urandom | od -An -tx1 | tr -d ' \n')"
CONF="$(mktemp -d)"

cleanup() {
  status=$?
  docker rm -f "$ID-db" "$ID-py" "$ID-arango" "$ID-minio" "$ID-mqtt" "$ID-gb" >/dev/null 2>&1 || true
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
      -e MQTT_HOST="$ID-mqtt" -e MQTT_PORT=1883 -e GB_URL="http://$ID-gb:8004"
      -e PYTHONPATH=/src -e PYTHONDONTWRITEBYTECODE=1 -e PYTHONUNBUFFERED=1)
run_py() {  # run_py ARGS... : one-shot python container on the test network
  timeout -s KILL 600 docker run --rm --name "$ID-py" --network "$NET" --memory=1g --memory-swap=1g \
    --pids-limit=256 -v "$REPO":/src:ro -w /src "${ENVS[@]}" "$IMAGE" timeout -s KILL 300 "$@"
}
psql_db() { docker exec "$ID-db" psql -X -v ON_ERROR_STOP=1 -U postgres -d mission "$@"; }
alembic() { run_py alembic -c packages/api/alembic.ini "$@"; }
version() { psql_db -tAc "SELECT version_num FROM alembic_version"; }
step() { echo; echo "=== $* ==="; }
tool() { run_py python -m tools.maps_m2_legacy_nodes "$@"; }

step "network + postgres, arangodb, minio, mosquitto"
docker network create --internal "$NET" >/dev/null
MOUNT=()
[ -n "$DUMP" ] && MOUNT=(-v "$DUMP":/dump/prod.dump:ro)
docker run -d --name "$ID-db" --network "$NET" --memory=2g --memory-swap=2g --pids-limit=512 \
  "${MOUNT[@]}" -e POSTGRES_PASSWORD="$PW" -e POSTGRES_DB=mission -e TIMESCALEDB_TELEMETRY=off \
  -e TS_TUNE_MEMORY=1GB -e TS_TUNE_NUM_CPUS=1 --entrypoint timeout \
  "$DB_IMAGE" -s KILL 900 /docker-entrypoint.sh postgres -c timescaledb.telemetry_level=off \
  -c timezone=UTC -c max_connections=100 >/dev/null
AMOUNT=()
[ -n "$ADUMP" ] && AMOUNT=(-v "$ADUMP":/dump/arango:ro)
docker run -d --name "$ID-arango" --network "$NET" --memory=2g --memory-swap=2g --pids-limit=512 \
  "${AMOUNT[@]}" -e ARANGO_ROOT_PASSWORD="$APW" --entrypoint timeout arangodb/arangodb:latest \
  -s KILL 900 /entrypoint.sh arangod >/dev/null
docker run -d --name "$ID-minio" --network "$NET" --memory=1g --memory-swap=1g --pids-limit=256 \
  -e MINIO_ROOT_USER="$MK" -e MINIO_ROOT_PASSWORD="$MS" --entrypoint timeout minio/minio:latest \
  -s KILL 900 minio server /data >/dev/null
printf 'listener 1883\nallow_anonymous true\npersistence false\n' > "$CONF/mosquitto.conf"
chmod 644 "$CONF/mosquitto.conf"; chmod 755 "$CONF"
docker run -d --name "$ID-mqtt" --network "$NET" --memory=256m --memory-swap=256m --pids-limit=64 \
  -v "$CONF/mosquitto.conf":/mosquitto/config/mosquitto.conf:ro --entrypoint timeout \
  eclipse-mosquitto:latest -s KILL 900 mosquitto -c /mosquitto/config/mosquitto.conf >/dev/null
for _ in $(seq 60); do
  if docker exec "$ID-db" pg_isready -U postgres -d mission >/dev/null 2>&1 &&
     psql_db -tAc "SELECT 1 FROM pg_extension WHERE extname='timescaledb'" 2>/dev/null | grep -q 1 &&
     docker exec "$ID-arango" arangosh --server.password "$APW" --javascript.execute-string \
       'db._version()' >/dev/null 2>&1; then
    break
  fi
  sleep 2
done

if [ -n "$DUMP" ]; then
  step "restore the production dumps ($(du -h "$DUMP" | cut -f1) Postgres; ArangoDB: ${ADUMP:-none})"
  psql_db -qc "SELECT timescaledb_pre_restore();" >/dev/null
  docker exec "$ID-db" pg_restore -U postgres -d mission --no-owner --exit-on-error \
    /dump/prod.dump 2>&1 | grep -v "already exists" || true
  psql_db -qc "SELECT timescaledb_post_restore();" >/dev/null
  echo "  restored at revision $(version)"
  [ "$(version)" = "$M1" ] || { echo "dump is not at $M1"; exit 1; }
  if [ -n "$ADUMP" ]; then
    docker exec "$ID-arango" arangorestore --server.password "$APW" \
      --server.database topomap_db --create-database true --input-directory /dump/arango \
      2>&1 | tail -2
  fi
else
  step "fresh database at $M1 + the live map and its legacy nodes"
  alembic upgrade "$PREV"
  run_py python tests/integration/maps/checks.py seed
  alembic upgrade "$M1"
  run_py python tests/integration/maps/checks_m2.py seed
fi

step "BEFORE"
run_py python tests/integration/maps/checks_m2.py show

step "upgrade: the API entrypoint (advisory lock + alembic upgrade head), as production does"
run_py python -m packages.api.entrypoint true
[ "$(version)" = "$HEAD" ] || { echo "not at $HEAD"; exit 1; }
run_py python tests/integration/maps/checks_m2.py upgraded

step "legacy nodes: dry run"
tool
step "legacy nodes: --apply"
tool --apply
run_py python tests/integration/maps/checks_m2.py legacy
step "legacy nodes: --apply again (idempotent)"
tool --apply | tee "$CONF/again.log"
grep -q "done: 0 node(s) rewritten" "$CONF/again.log" || { echo "second run rewrote nodes"; exit 1; }
step "legacy nodes: --revert --apply (the rollback path), then --apply again"
tool --revert --apply
run_py python tests/integration/maps/checks_m2.py reverted
tool --apply >/dev/null
run_py python tests/integration/maps/checks_m2.py legacy

step "graph-builder (this checkout) on the test network"
timeout -s KILL 600 docker run -d --name "$ID-gb" --network "$NET" --memory=1g --memory-swap=1g \
  --pids-limit=256 -v "$REPO":/src:ro -w /src "${ENVS[@]}" "$IMAGE" timeout -s KILL 500 \
  python -m packages.services.graph_builder.main --host 0.0.0.0 --port 8004 >/dev/null
for _ in $(seq 60); do
  docker logs "$ID-gb" 2>&1 | grep -q "Application startup complete" && break
  sleep 1
done
sleep 2

step "scenario: ingest by session over MQTT"
run_py python tests/integration/maps/checks_m2.py ingest || { docker logs --tail 60 "$ID-gb"; exit 1; }
echo "  graph-builder log (rejections / stored nodes):"
docker logs "$ID-gb" 2>&1 | grep -E "Dropped|Processed node|Traceback|ERROR" | tail -15 | sed 's/^/    /'
docker logs "$ID-gb" 2>&1 | grep -q Traceback && { echo "traceback in graph-builder"; exit 1; }

step "AFTER"
run_py python tests/integration/maps/checks_m2.py show

step "downgrade -1 (-> $M1)"
docker rm -f "$ID-gb" >/dev/null
alembic downgrade -1
[ "$(version)" = "$M1" ] || { echo "not at $M1"; exit 1; }
run_py python tests/integration/maps/checks_m2.py downgraded

step "re-upgrade"
alembic upgrade head
[ "$(version)" = "$HEAD" ] || { echo "not at $HEAD"; exit 1; }
run_py python tests/integration/maps/checks_m2.py upgraded

step "PASSED"
