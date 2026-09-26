#!/usr/bin/env bash
# Exit-checker integration test (WP13): seeds the Phase 0 exit scenario on pg17/TimescaleDB with
# the real recording code (fleet_recorder hooks, run worker, TelemetryWriter, the API's robot
# route for level changes, recorder_health rows), then runs tools/phase0_exit_check.py on it:
# it must PASS (and change nothing), and FAIL exactly the checks whose data is then tampered.
#
#   tests/integration/phase0_exit_check/run.sh [TEST_IMAGE]
#
# Same harness as tests/integration/run_admin/run.sh (private --internal network, no published
# ports, every container capped at 2 GB and under `timeout -s KILL 300`), so it can run on a
# host that also runs production without touching it. Everything is removed on exit.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
IMAGE="${1:-wp6-test:py310}"
DB_IMAGE="timescale/timescaledb-ha:pg17.11-ts2.30.1"
ID="exitcheck-$$-$(date +%s)"
NET="$ID-net"
PW="xc-$(head -c 12 /dev/urandom | od -An -tx1 | tr -d ' \n')"
WORK="$(mktemp -d)"
chmod 777 "$WORK"

cleanup() {
  status=$?
  docker rm -f "$ID-db" "$ID-alembic" "$ID-init" "$ID-seed" "$ID-verify" "$ID-tamper" \
    >/dev/null 2>&1 || true
  docker network rm "$NET" >/dev/null 2>&1 || true
  rm -rf "$WORK"
  exit $status
}
trap cleanup EXIT

PY_LIMITS=(--memory=2g --memory-swap=2g --pids-limit=256)
ENVS=(-e PGHOST="$ID-db" -e PGPASSWORD="$PW" -e PGDATABASE=mission
      -e POSTGRES_DATABASE_HOST="$ID-db" -e POSTGRES_DATABASE_PORT=5432
      -e POSTGRES_DATABASE_NAME=mission -e POSTGRES_DATABASE_USERNAME=postgres
      -e POSTGRES_DATABASE_PASSWORD="$PW" -e POSTGRES_PASSWORD="$PW"
      -e ARANGO_PASSWORD=unused -e MINIO_ACCESS_KEY=unused -e MINIO_SECRET_KEY=unused
      -e TELEMETRY_INGEST_ENABLED=false
      -e WORK=/work -e PYTHONPATH=/src -e PYTHONDONTWRITEBYTECODE=1 -e PYTHONUNBUFFERED=1)
run_py() {  # run_py NAME ARGS... : one-shot python container on the test network
  local name=$1; shift
  docker run --rm --name "$ID-$name" --network "$NET" "${PY_LIMITS[@]}" \
    -v "$REPO":/src:ro -v "$WORK":/work -w /src "${ENVS[@]}" "$IMAGE" \
    timeout -s KILL 300 "$@"
}
step() { echo; echo "=== $* ==="; }

step "network + postgres (TimescaleDB)"
docker network create --internal "$NET" >/dev/null
docker run -d --name "$ID-db" --network "$NET" --memory=2g --memory-swap=2g --pids-limit=512 \
  -e POSTGRES_PASSWORD="$PW" -e POSTGRES_DB=mission -e TIMESCALEDB_TELEMETRY=off \
  -e TS_TUNE_MEMORY=1GB -e TS_TUNE_NUM_CPUS=1 --entrypoint timeout \
  "$DB_IMAGE" -s KILL 300 /docker-entrypoint.sh postgres -c timescaledb.telemetry_level=off \
  -c timezone=UTC -c max_connections=100 >/dev/null
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

step "init: object tables, robot, site + assignment"
run_py init python tests/integration/phase0_exit_check/checks.py init

step "seed: the exit scenario through the real recording code"
run_py seed python tests/integration/phase0_exit_check/checks.py seed

step "verify: tools/phase0_exit_check.py PASSES, read-only"
run_py verify python tests/integration/phase0_exit_check/checks.py verify

step "tamper: one violation per kind, exactly those checks FAIL"
run_py tamper python tests/integration/phase0_exit_check/checks.py tamper

step "PASSED"
