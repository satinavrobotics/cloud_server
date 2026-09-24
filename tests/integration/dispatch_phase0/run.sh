#!/usr/bin/env bash
# WP6 integration test: mission-dispatch writing the Phase 0 tables, end to end.
#
# Everything runs on a private, --internal docker network with no published ports, so it
# can run on a host that also runs production (host networking on 5432/1883) without
# touching it. All containers and the network are removed on exit.
#
#   tests/integration/dispatch_phase0/run.sh [TEST_IMAGE]
#
# TEST_IMAGE is a python:3.10 image with the dispatcher's pinned requirements
# (packages/controllers/mission/requirements.txt) plus alembic==1.13.3 and
# SQLAlchemy==2.0.36; see README.md in this directory for a Dockerfile.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
TEST_IMAGE="${1:-wp6-test:py310}"
DB_IMAGE="timescale/timescaledb-ha:pg17.11-ts2.30.1"
MQTT_IMAGE="eclipse-mosquitto:2"
ID="wp6it-$$-$(date +%s)"
NET="$ID-net"
PW="wp6-$(head -c 12 /dev/urandom | od -An -tx1 | tr -d ' \n')"
WORK="$(mktemp -d)"
chmod 777 "$WORK"

cleanup() {
  status=$?
  if [ $status -ne 0 ]; then
    echo "--- dispatcher log (tail) ---"; docker logs --tail 80 "$ID-dispatch" 2>&1 || true
  fi
  docker rm -f "$ID-db" "$ID-mqtt" "$ID-dispatch" "$ID-robot" "$ID-stream" >/dev/null 2>&1 || true
  docker network rm "$NET" >/dev/null 2>&1 || true
  rm -rf "$WORK"
  exit $status
}
trap cleanup EXIT

# Resource caps on every container (this may run on a production host): memory without
# swap, a pid limit, and a hard time limit on everything that is not removed by cleanup.
PY_LIMITS=(--memory=1g --memory-swap=1g --pids-limit=256)
ENVS=(-e PGHOST="$ID-db" -e PGPASSWORD="$PW" -e PGDATABASE=mission -e MQTT_HOST="$ID-mqtt"
      -e POSTGRES_DATABASE_HOST="$ID-db" -e POSTGRES_DATABASE_PORT=5432
      -e POSTGRES_DATABASE_NAME=mission -e POSTGRES_DATABASE_USERNAME=postgres
      -e POSTGRES_DATABASE_PASSWORD="$PW" -e POSTGRES_PASSWORD="$PW"
      -e ARANGO_PASSWORD=unused -e MINIO_ACCESS_KEY=unused -e MINIO_SECRET_KEY=unused
      -e PYTHONPATH=/src -e PYTHONDONTWRITEBYTECODE=1 -e PYTHONUNBUFFERED=1)
run_py() {  # run_py NAME ARGS... : one-shot python container on the test network
  local name=$1; shift
  docker run --rm --name "$ID-$name" --network "$NET" "${PY_LIMITS[@]}" \
    -v "$REPO":/src:ro -v "$WORK":/work -w /src "${ENVS[@]}" "$TEST_IMAGE" \
    timeout -s KILL 300 "$@"
}
step() { echo; echo "=== $* ==="; }

step "network + postgres (TimescaleDB) + mosquitto"
docker network create --internal "$NET" >/dev/null
docker run -d --name "$ID-db" --network "$NET" --memory=2g --memory-swap=2g --pids-limit=512 \
  -e POSTGRES_PASSWORD="$PW" -e POSTGRES_DB=mission -e TIMESCALEDB_TELEMETRY=off \
  -e TS_TUNE_MEMORY=1GB -e TS_TUNE_NUM_CPUS=1 \
  "$DB_IMAGE" postgres -c timescaledb.telemetry_level=off -c timezone=UTC >/dev/null
printf 'listener 1883\nallow_anonymous true\npersistence false\n' > "$WORK/mosquitto.conf"
docker run -d --name "$ID-mqtt" --network "$NET" --memory=256m --memory-swap=256m -v "$WORK/mosquitto.conf":/mosquitto/config/mosquitto.conf:ro \
  "$MQTT_IMAGE" >/dev/null
for _ in $(seq 60); do
  if docker exec "$ID-db" pg_isready -U postgres -d mission >/dev/null 2>&1 &&
     docker exec "$ID-db" psql -U postgres -d mission -tAc \
       "SELECT 1 FROM pg_extension WHERE extname='timescaledb'" 2>/dev/null | grep -q 1; then
    break
  fi
  sleep 2
done

step "alembic upgrade head"
run_py alembic alembic -c packages/api/alembic.ini upgrade head

step "init: object tables, robots, level full, a stale RUNNING run"
run_py init python tests/integration/dispatch_phase0/checks.py init

step "start mission-dispatch and the dummy robot (goal mode)"
docker run -d --name "$ID-dispatch" --network "$NET" "${PY_LIMITS[@]}" -v "$REPO":/src:ro \
  -w /src "${ENVS[@]}" "$TEST_IMAGE" timeout -s KILL 900 python packages/controllers/mission/main.py \
  --mqtt_host "$ID-mqtt" --mqtt_port 1883 --postgres-host "$ID-db" --postgres-db mission \
  --postgres-user postgres --postgres-password "$PW" >/dev/null
docker run -d --name "$ID-robot" --network "$NET" "${PY_LIMITS[@]}" -v "$REPO":/src:ro \
  -w /src "${ENVS[@]}" "$TEST_IMAGE" timeout -s KILL 900 python tests/dummy_robot/dummy_robot.py --mode goal --robot_name dummy_01 \
  --mqtt_host "$ID-mqtt" --no_nodes --no_images --speed 2.0 --tick_period 0.5 >/dev/null

step "mission to COMPLETED: mission_runs row, RUN_STARTED/RUN_FINISHED, telemetry, orphan"
run_py mission python tests/integration/dispatch_phase0/checks.py mission

step "replay a synthetic sequence twice: identical event set"
run_py replay python tests/integration/dispatch_phase0/checks.py replay

step "restart dispatch mid-stream: no spurious events"
docker run -d --name "$ID-stream" --network "$NET" "${PY_LIMITS[@]}" -v "$REPO":/src:ro \
  -w /src "${ENVS[@]}" "$TEST_IMAGE" timeout -s KILL 120 python tests/integration/dispatch_phase0/checks.py stream 55 >/dev/null
sleep 8
run_py snap python tests/integration/dispatch_phase0/checks.py snapshot /work/before.json
docker restart -t 5 "$ID-dispatch" >/dev/null
echo "dispatcher restarted"
sleep 40   # past the 30 s heartbeat timeout, while both robots keep reporting
run_py verify python tests/integration/dispatch_phase0/checks.py no-new-events /work/before.json
if docker logs "$ID-dispatch" 2>&1 | grep -q "Fleet recording hook .* failed"; then
  echo "recording hook errors in the dispatcher log"; exit 1
fi

step "PASSED"
