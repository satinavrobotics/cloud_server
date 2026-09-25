#!/usr/bin/env bash
# WP11 integration test (docs/satinav-fleet-agent-phase0-v2.md §6 F1-F3): migration
# 20260925_01_idempotency on pg17/TimescaleDB (applied by the API entrypoint under its advisory
# lock, then downgraded and re-applied), Idempotency-Key through the real routes and
# IdempotencyStore on real Postgres (replay, 422, concurrent duplicates across pools, expiry,
# lease, purge), and the map delete saga's SQL, per-map lock and MAP.DELETE_FAILED.
#
#   tests/integration/wp11/run.sh [API_TEST_IMAGE]
#
# Same harness as tests/integration/sites/run.sh (private --internal network, no published
# ports, capped memory, every container under `timeout -s KILL`), so it can run on a host that
# also runs production without touching it. Everything is removed on exit.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
API_IMAGE="${1:-wp5-test:py310}"
DB_IMAGE="timescale/timescaledb-ha:pg17.11-ts2.30.1"
ID="wp11it-$$-$(date +%s)"
NET="$ID-net"
PW="wp11-$(head -c 12 /dev/urandom | od -An -tx1 | tr -d ' \n')"

cleanup() {
  status=$?
  docker rm -f "$ID-db" "$ID-alembic" "$ID-entry" "$ID-schema" "$ID-init" "$ID-scenario" \
    >/dev/null 2>&1 || true
  docker network rm "$NET" >/dev/null 2>&1 || true
  exit $status
}
trap cleanup EXIT

PY_LIMITS=(--memory=1g --memory-swap=1g --pids-limit=256)
ENVS=(-e PGHOST="$ID-db" -e PGPASSWORD="$PW" -e PGDATABASE=mission
      -e POSTGRES_DATABASE_HOST="$ID-db" -e POSTGRES_DATABASE_PORT=5432
      -e POSTGRES_DATABASE_NAME=mission -e POSTGRES_DATABASE_USERNAME=postgres
      -e POSTGRES_DATABASE_PASSWORD="$PW" -e POSTGRES_PASSWORD="$PW"
      -e ARANGO_PASSWORD=unused -e MINIO_ACCESS_KEY=unused -e MINIO_SECRET_KEY=unused
      -e PYTHONPATH=/src -e PYTHONDONTWRITEBYTECODE=1 -e PYTHONUNBUFFERED=1)
run_py() {  # run_py NAME ARGS... : one-shot python container on the test network
  local name=$1; shift
  docker run --rm --name "$ID-$name" --network "$NET" "${PY_LIMITS[@]}" \
    -v "$REPO":/src:ro -w /src "${ENVS[@]}" "$API_IMAGE" timeout -s KILL 300 "$@"
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

step "alembic upgrade to phase0_core (production's current head)"
run_py alembic alembic -c packages/api/alembic.ini upgrade 20260924_01_phase0_core

step "API entrypoint: migrate to head under the advisory lock, then exec"
run_py entry python -m packages.api.entrypoint true
run_py schema python tests/integration/wp11/checks.py schema

step "downgrade one revision and upgrade again"
run_py alembic alembic -c packages/api/alembic.ini downgrade 20260924_01_phase0_core
docker exec "$ID-db" psql -U postgres -d mission -tAc \
  "SELECT count(*) FROM information_schema.columns WHERE table_name='idempotency_keys'
     AND column_name='completed_at'" | grep -qx 0
echo "  ok: downgrade removed completed_at"
run_py alembic alembic -c packages/api/alembic.ini upgrade head
run_py schema python tests/integration/wp11/checks.py schema

step "init: object tables and a robot"
run_py init python tests/integration/wp11/checks.py init

step "scenario: F3 and F1 on real Postgres"
run_py scenario python tests/integration/wp11/checks.py scenario

step "PASSED"
