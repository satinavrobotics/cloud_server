#!/usr/bin/env bash
# WP10 integration test: the read endpoints (/runs, /runs/{id}, /runs/{id}/timeline, /events,
# /robots/{name}/recording) against real TimescaleDB after `alembic upgrade head`: keyset
# pagination and filters, 404/422, timeline tracks (raw, downsampled, rollup fallback),
# trajectory, `not_recorded` intervals across robot/site/assignment/global level switches made
# through the real routes, the effective level, and the READ ONLY + statement_timeout guard
# (docs/satinav-fleet-agent-phase0-v2.md §4.3, §5.5, §7 WP10).
#
#   tests/integration/fleet_reads/run.sh [ALEMBIC_IMAGE] [API_TEST_IMAGE]
#
# Same harness as tests/integration/sites/run.sh (private --internal network, no published
# ports, capped memory, every container under `timeout -s KILL`), so it can run on a host that
# also runs production without touching it. Only Postgres is needed: runs, events and time
# series are seeded, level changes go through the API routes. Everything is removed on exit.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
ALEMBIC_IMAGE="${1:-wp6-test:py310}"
API_IMAGE="${2:-wp5-test:py310}"
DB_IMAGE="timescale/timescaledb-ha:pg17.11-ts2.30.1"
ID="wp10it-$$-$(date +%s)"
NET="$ID-net"
PW="wp10-$(head -c 12 /dev/urandom | od -An -tx1 | tr -d ' \n')"
WORK="$(mktemp -d)"
chmod 777 "$WORK"

cleanup() {
  status=$?
  docker rm -f "$ID-db" "$ID-alembic" "$ID-init" "$ID-scenario" >/dev/null 2>&1 || true
  docker network rm "$NET" >/dev/null 2>&1 || true
  rm -rf "$WORK"
  exit $status
}
trap cleanup EXIT

PY_LIMITS=(--memory=1g --memory-swap=1g --pids-limit=256)
ENVS=(-e PGHOST="$ID-db" -e PGPASSWORD="$PW" -e PGDATABASE=mission
      -e POSTGRES_DATABASE_HOST="$ID-db" -e POSTGRES_DATABASE_PORT=5432
      -e POSTGRES_DATABASE_NAME=mission -e POSTGRES_DATABASE_USERNAME=postgres
      -e POSTGRES_DATABASE_PASSWORD="$PW" -e POSTGRES_PASSWORD="$PW"
      -e ARANGO_PASSWORD=unused -e MINIO_ACCESS_KEY=unused -e MINIO_SECRET_KEY=unused
      -e TELEMETRY_INGEST_ENABLED=false
      -e WORK=/work -e PYTHONPATH=/src -e PYTHONDONTWRITEBYTECODE=1 -e PYTHONUNBUFFERED=1)
run_py() {  # run_py IMAGE NAME ARGS... : one-shot python container on the test network
  local image=$1 name=$2; shift 2
  docker run --rm --name "$ID-$name" --network "$NET" "${PY_LIMITS[@]}" \
    -v "$REPO":/src:ro -v "$WORK":/work -w /src "${ENVS[@]}" "$image" \
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
run_py "$ALEMBIC_IMAGE" alembic alembic -c packages/api/alembic.ini upgrade head

step "init: object tables and the robots"
run_py "$API_IMAGE" init python tests/integration/fleet_reads/checks.py init

step "scenario: level switches, seeded history, read routes"
run_py "$API_IMAGE" scenario python tests/integration/fleet_reads/checks.py scenario

step "PASSED"
