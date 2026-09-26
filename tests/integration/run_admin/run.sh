#!/usr/bin/env bash
# Run archive / mission delete integration test: migration 20260926_01_run_archive on
# pg17/TimescaleDB (upgrade, downgrade, upgrade), GET /runs?archived=, POST /runs/archive,
# DELETE /missions/{name}[?with_reruns=true] through the real routes (including deletes from a
# compressed fleet_events chunk), dispatch's run writes vs archived_at, and fleet_recorder's
# late-finish guard.
#
#   tests/integration/run_admin/run.sh [TEST_IMAGE]
#
# Same harness as tests/integration/fleet_reads/run.sh (private --internal network, no
# published ports, every container capped at 2 GB and under `timeout -s KILL 300`), so it can
# run on a host that also runs production without touching it. Everything is removed on exit.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
IMAGE="${1:-wp5-test:py310}"
DB_IMAGE="timescale/timescaledb-ha:pg17.11-ts2.30.1"
ID="runadmin-$$-$(date +%s)"
NET="$ID-net"
PW="ra-$(head -c 12 /dev/urandom | od -An -tx1 | tr -d ' \n')"
WORK="$(mktemp -d)"
chmod 777 "$WORK"

cleanup() {
  status=$?
  docker rm -f "$ID-db" "$ID-alembic" "$ID-schema" "$ID-init" "$ID-scenario" \
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

step "alembic upgrade to 20260925_01_idempotency (production's current head), then head"
run_py alembic alembic -c packages/api/alembic.ini upgrade 20260925_01_idempotency
run_py alembic alembic -c packages/api/alembic.ini upgrade head
run_py schema python tests/integration/run_admin/checks.py schema head

step "downgrade one revision and upgrade again"
run_py alembic alembic -c packages/api/alembic.ini downgrade 20260925_01_idempotency
run_py schema python tests/integration/run_admin/checks.py schema down
run_py alembic alembic -c packages/api/alembic.ini upgrade head
run_py schema python tests/integration/run_admin/checks.py schema head

step "init: object tables and the robots"
run_py init python tests/integration/run_admin/checks.py init

step "scenario: archive, delete, dispatch writes"
run_py scenario python tests/integration/run_admin/checks.py scenario

step "PASSED"
