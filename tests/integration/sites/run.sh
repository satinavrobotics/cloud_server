#!/usr/bin/env bash
# WP9 integration test: sites CRUD and robot site assignments through the real routes, the
# EXCLUDE constraint on robot_site_assignments, assignment history, site_id on runs/events/
# robot_latest, and recording levels switched through the site layer taking effect in
# mission-dispatch and the API's telemetry writer without a restart (docs/
# satinav-fleet-agent-phase0-v2.md §3.6, §4.2, §5.5, §7 WP9).
#
#   tests/integration/sites/run.sh [DISPATCH_TEST_IMAGE] [API_TEST_IMAGE]
#
# Same harness as tests/integration/recording_policy/run.sh (images, private --internal
# network, no published ports, capped memory, every container under `timeout -s KILL`), so it
# can run on a host that also runs production without touching it. Everything is removed on
# exit.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
DISPATCH_IMAGE="${1:-wp6-test:py310}"
API_IMAGE="${2:-wp5-test:py310}"
DB_IMAGE="timescale/timescaledb-ha:pg17.11-ts2.30.1"
MQTT_IMAGE="eclipse-mosquitto:2"
ID="wp9it-$$-$(date +%s)"
NET="$ID-net"
PW="wp9-$(head -c 12 /dev/urandom | od -An -tx1 | tr -d ' \n')"
WORK="$(mktemp -d)"
chmod 777 "$WORK"

cleanup() {
  status=$?
  if [ $status -ne 0 ]; then
    echo "--- dispatcher log (tail) ---"; docker logs --tail 60 "$ID-dispatch" 2>&1 || true
  fi
  docker rm -f "$ID-db" "$ID-mqtt" "$ID-dispatch" "$ID-alembic" "$ID-init" "$ID-scenario" \
    >/dev/null 2>&1 || true
  docker network rm "$NET" >/dev/null 2>&1 || true
  rm -rf "$WORK"
  exit $status
}
trap cleanup EXIT

PY_LIMITS=(--memory=1g --memory-swap=1g --pids-limit=256)
ENVS=(-e PGHOST="$ID-db" -e PGPASSWORD="$PW" -e PGDATABASE=mission -e MQTT_HOST="$ID-mqtt"
      -e POSTGRES_DATABASE_HOST="$ID-db" -e POSTGRES_DATABASE_PORT=5432
      -e POSTGRES_DATABASE_NAME=mission -e POSTGRES_DATABASE_USERNAME=postgres
      -e POSTGRES_DATABASE_PASSWORD="$PW" -e POSTGRES_PASSWORD="$PW"
      -e ARANGO_PASSWORD=unused -e MINIO_ACCESS_KEY=unused -e MINIO_SECRET_KEY=unused
      -e WORK=/work -e PYTHONPATH=/src -e PYTHONDONTWRITEBYTECODE=1 -e PYTHONUNBUFFERED=1)
run_py() {  # run_py IMAGE NAME ARGS... : one-shot python container on the test network
  local image=$1 name=$2; shift 2
  docker run --rm --name "$ID-$name" --network "$NET" "${PY_LIMITS[@]}" \
    -v "$REPO":/src:ro -v "$WORK":/work -w /src "${ENVS[@]}" "$image" \
    timeout -s KILL 300 "$@"
}
step() { echo; echo "=== $* ==="; }

step "network + postgres (TimescaleDB) + mosquitto"
docker network create --internal "$NET" >/dev/null
docker run -d --name "$ID-db" --network "$NET" --memory=2g --memory-swap=2g --pids-limit=512 \
  -e POSTGRES_PASSWORD="$PW" -e POSTGRES_DB=mission -e TIMESCALEDB_TELEMETRY=off \
  -e TS_TUNE_MEMORY=1GB -e TS_TUNE_NUM_CPUS=1 --entrypoint timeout \
  "$DB_IMAGE" -s KILL 300 /docker-entrypoint.sh postgres -c timescaledb.telemetry_level=off \
  -c timezone=UTC -c track_commit_timestamp=on -c max_connections=100 >/dev/null
printf 'listener 1883\nallow_anonymous true\npersistence false\n' > "$WORK/mosquitto.conf"
docker run -d --name "$ID-mqtt" --network "$NET" --memory=256m --memory-swap=256m \
  -v "$WORK/mosquitto.conf":/mosquitto/config/mosquitto.conf:ro --entrypoint timeout \
  "$MQTT_IMAGE" -s KILL 300 /docker-entrypoint.sh /usr/sbin/mosquitto \
  -c /mosquitto/config/mosquitto.conf >/dev/null
for _ in $(seq 60); do
  if docker exec "$ID-db" pg_isready -U postgres -d mission >/dev/null 2>&1 &&
     docker exec "$ID-db" psql -U postgres -d mission -tAc \
       "SELECT 1 FROM pg_extension WHERE extname='timescaledb'" 2>/dev/null | grep -q 1; then
    break
  fi
  sleep 2
done

step "alembic upgrade head"
run_py "$DISPATCH_IMAGE" alembic alembic -c packages/api/alembic.ini upgrade head

step "init: object tables (incl. siteobjectv1) and the robots"
run_py "$API_IMAGE" init python tests/integration/sites/checks.py init

step "start mission-dispatch"
docker run -d --name "$ID-dispatch" --network "$NET" "${PY_LIMITS[@]}" -v "$REPO":/src:ro \
  -w /src "${ENVS[@]}" "$DISPATCH_IMAGE" timeout -s KILL 300 \
  python packages/controllers/mission/main.py \
  --mqtt_host "$ID-mqtt" --mqtt_port 1883 --postgres-host "$ID-db" --postgres-db mission \
  --postgres-user postgres --postgres-password "$PW" >/dev/null

step "scenario: sites, assignments and levels through the routes"
run_py "$API_IMAGE" scenario python tests/integration/sites/checks.py scenario

if docker logs "$ID-dispatch" 2>&1 | grep -q "Fleet recording hook .* failed"; then
  echo "recording hook errors in the dispatcher log"; exit 1
fi
if docker logs "$ID-dispatch" 2>&1 | grep -qi "site.*watcher failed"; then
  echo "site watcher errors in the dispatcher log"; exit 1
fi
step "PASSED"
