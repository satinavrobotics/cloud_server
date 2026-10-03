#!/usr/bin/env bash
# Map type conversion (docs/satinav-maps-redesign.md §17) on real Postgres: a fresh database
# migrated to head through the API entrypoint, then tests/integration/maps/checks_type.py.
#
#   tests/integration/maps/run_type.sh [TEST_IMAGE]
#
# Same harness as run.sh: a private --internal network, no published ports, capped memory,
# every container under `timeout -s KILL`, so it can run on a host that also runs production
# without touching it. Everything is removed on exit. No migration of its own (§17 adds none).
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
IMAGE="${1:-wp6-test:py310}"
DB_IMAGE="timescale/timescaledb-ha:pg17.11-ts2.30.1"
ID="typeit-$$-$(date +%s)"
NET="$ID-net"
PW="ty-$(head -c 12 /dev/urandom | od -An -tx1 | tr -d ' \n')"

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
step() { echo; echo "=== $* ==="; }

step "network + postgres ($DB_IMAGE)"
docker network create --internal "$NET" >/dev/null
docker run -d --name "$ID-db" --network "$NET" --memory=2g --memory-swap=2g --pids-limit=512 \
  -e POSTGRES_PASSWORD="$PW" -e POSTGRES_DB=mission -e TIMESCALEDB_TELEMETRY=off \
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

step "object tables (created at runtime by PostgresDatabase, not by Alembic)"
run_py python -c "import asyncio; from tests.integration.maps.checks import database; asyncio.run(database().async_init())"

step "migrate to head: the API entrypoint (advisory lock + alembic upgrade head)"
run_py python -m packages.api.entrypoint true
echo "  at $(psql_db -tAc 'SELECT version_num FROM alembic_version')"

step "scenario: convert_map_type and the datum placement on real Postgres"
run_py python tests/integration/maps/checks_type.py scenario

step "PASSED"
