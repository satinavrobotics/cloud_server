#!/usr/bin/env bash
# Maps redesign M1 integration test: migration 20260928_01_map_sessions (upgrade through the
# API entrypoint, downgrade, re-upgrade) and packages/api/maps.py on real Postgres
# (docs/satinav-maps-redesign.md §4, §7, §12).
#
#   tests/integration/maps/run.sh [--dump FILE] [TEST_IMAGE]
#
# Without --dump: a fresh database at 20260926_02_recorder_health seeded with pre-M1 maps.
# With --dump FILE (a `pg_dump -Fc` of production, taken read-only): the rehearsal. The dump is
# restored into the throwaway database (TimescaleDB pre/post restore) and migrated from there.
#
# Same harness as tests/integration/sites/run.sh: a private --internal network, no published
# ports, capped memory, every container under `timeout -s KILL`, so it can run on a host that
# also runs production without touching it. Everything is removed on exit.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
DUMP=""
if [ "${1:-}" = --dump ]; then DUMP="$(cd "$(dirname "$2")" && pwd)/$(basename "$2")"; shift 2; fi
IMAGE="${1:-wp6-test:py310}"
DB_IMAGE="timescale/timescaledb-ha:pg17.11-ts2.30.1"
PREV=20260926_02_recorder_health
NEW=20260928_01_map_sessions
# Head since maps M2 (20260929_01_maps_m2, fleet_events source graph_builder): the API
# entrypoint upgrades to it. M2's own rehearsal (legacy nodes, ingest) is run_m2.sh.
HEAD=20260929_01_maps_m2
ID="m1it-$$-$(date +%s)"
NET="$ID-net"
PW="m1-$(head -c 12 /dev/urandom | od -An -tx1 | tr -d ' \n')"

cleanup() {
  status=$?
  docker rm -f "$ID-db" "$ID-py" >/dev/null 2>&1 || true
  docker network rm "$NET" >/dev/null 2>&1 || true
  exit $status
}
trap cleanup EXIT

ENVS=(-e PGHOST="$ID-db" -e PGPASSWORD="$PW" -e PGDATABASE=mission
      -e POSTGRES_DATABASE_HOST="$ID-db" -e POSTGRES_DATABASE_PORT=5432
      -e POSTGRES_DATABASE_NAME=mission -e POSTGRES_DATABASE_USERNAME=postgres
      -e POSTGRES_DATABASE_PASSWORD="$PW" -e POSTGRES_PASSWORD="$PW"
      -e ARANGO_PASSWORD=unused -e MINIO_ACCESS_KEY=unused -e MINIO_SECRET_KEY=unused
      -e PYTHONPATH=/src -e PYTHONDONTWRITEBYTECODE=1 -e PYTHONUNBUFFERED=1)
run_py() {  # run_py ARGS... : one-shot python container on the test network
  timeout -s KILL 600 docker run --rm --name "$ID-py" --network "$NET" --memory=1g --memory-swap=1g \
    --pids-limit=256 -v "$REPO":/src:ro -w /src "${ENVS[@]}" "$IMAGE" timeout -s KILL 300 "$@"
}
psql_db() { docker exec "$ID-db" psql -X -v ON_ERROR_STOP=1 -U postgres -d mission "$@"; }
alembic() { run_py alembic -c packages/api/alembic.ini "$@"; }
version() { psql_db -tAc "SELECT version_num FROM alembic_version"; }
step() { echo; echo "=== $* ==="; }

step "network + postgres ($DB_IMAGE)"
docker network create --internal "$NET" >/dev/null
MOUNT=()
[ -n "$DUMP" ] && MOUNT=(-v "$DUMP":/dump/prod.dump:ro)
docker run -d --name "$ID-db" --network "$NET" --memory=2g --memory-swap=2g --pids-limit=512 \
  "${MOUNT[@]}" -e POSTGRES_PASSWORD="$PW" -e POSTGRES_DB=mission -e TIMESCALEDB_TELEMETRY=off \
  -e TS_TUNE_MEMORY=1GB -e TS_TUNE_NUM_CPUS=1 --entrypoint timeout \
  "$DB_IMAGE" -s KILL 600 /docker-entrypoint.sh postgres -c timescaledb.telemetry_level=off \
  -c timezone=UTC -c max_connections=100 >/dev/null
for _ in $(seq 60); do
  if docker exec "$ID-db" pg_isready -U postgres -d mission >/dev/null 2>&1 &&
     psql_db -tAc "SELECT 1 FROM pg_extension WHERE extname='timescaledb'" 2>/dev/null |
       grep -q 1; then
    break
  fi
  sleep 2
done

if [ -n "$DUMP" ]; then
  step "restore the production dump ($(du -h "$DUMP" | cut -f1))"
  psql_db -qc "SELECT timescaledb_pre_restore();" >/dev/null
  docker exec "$ID-db" pg_restore -U postgres -d mission --no-owner --exit-on-error \
    /dump/prod.dump 2>&1 | grep -v "already exists" || true
  psql_db -qc "SELECT timescaledb_post_restore();" >/dev/null
  echo "  restored at revision $(version)"
  [ "$(version)" = "$PREV" ] || { echo "dump is not at $PREV"; exit 1; }
else
  step "fresh database at $PREV + pre-M1 maps"
  alembic upgrade "$PREV"
  run_py python tests/integration/maps/checks.py seed
fi

step "BEFORE"
run_py python tests/integration/maps/checks.py show

step "upgrade: the API entrypoint (advisory lock + alembic upgrade head), as production does"
run_py python -m packages.api.entrypoint true
[ "$(version)" = "$HEAD" ] || { echo "not at $HEAD"; exit 1; }

step "AFTER"
run_py python tests/integration/maps/checks.py show
run_py python tests/integration/maps/checks.py upgraded

step "scenario: maps routes' logic on real Postgres"
run_py python tests/integration/maps/checks.py scenario

step "downgrade to $PREV (M2, then M1)"
alembic downgrade "$PREV"
[ "$(version)" = "$PREV" ] || { echo "not at $PREV"; exit 1; }
run_py python tests/integration/maps/checks.py downgraded
run_py python tests/integration/maps/checks.py show

step "re-upgrade (idempotent data step)"
alembic upgrade head
[ "$(version)" = "$HEAD" ] || { echo "not at $HEAD"; exit 1; }
run_py python tests/integration/maps/checks.py upgraded

step "PASSED"
