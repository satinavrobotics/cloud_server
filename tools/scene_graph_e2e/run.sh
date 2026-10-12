#!/usr/bin/env bash
# Scene graph end-to-end harness: brings up a throw-away ArangoDB + MinIO + Postgres
# (docker_compose/scene_graph_e2e.yaml, project `sg-e2e`, own network, loopback-only ports 28529 /
# 29000 / 25432), runs tests/integration/scene_graph against them with the real services and
# tears the stack down (volumes included) on exit, also on failure or Ctrl-C.
#
#   tools/scene_graph_e2e/run.sh                  # the whole module
#   tools/scene_graph_e2e/run.sh -k planner       # extra arguments go to pytest
#
# Environment:
#   SG_E2E_PYTHON       python with the test requirements (default: python3). Needs numpy, scipy,
#                       Pillow, python-arango, minio, psycopg (3), fastapi and pydantic 1.x
#   SG_E2E_KEEP_STACK=1 leave the containers up afterwards (then `down -v` yourself)
#   SG_E2E_*_PORT       the published ports (SG_E2E_ARANGO_PORT, _MINIO_PORT, _POSTGRES_PORT)
#   SG_E2E_ALEMBIC_IMAGE  docker image with alembic + sqlalchemy + psycopg used for
#                       `alembic upgrade head` when alembic is not importable here (default
#                       wp6-test:py310); without both, the revisions' SQL is replayed
#   SG_E2E_MIGRATE=local|docker|sql  force one migration method
#
# It never touches the production stack: no shared names, ports, networks or volumes. The images
# are pinned to the same digests / tags as docker_compose/mission_dispatch_services.yaml
# (ArangoDB, MinIO, timescaledb-ha pg17.11-ts2.30.1); override with SG_E2E_{ARANGO,MINIO,POSTGRES}_IMAGE.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
COMPOSE=(docker compose -p sg-e2e -f "$REPO/docker_compose/scene_graph_e2e.yaml")
PY="${SG_E2E_PYTHON:-python3}"

export SG_E2E_ARANGO_PORT="${SG_E2E_ARANGO_PORT:-28529}"
export SG_E2E_MINIO_PORT="${SG_E2E_MINIO_PORT:-29000}"
export SG_E2E_POSTGRES_PORT="${SG_E2E_POSTGRES_PORT:-25432}"

command -v docker >/dev/null || { echo "docker is not installed" >&2; exit 2; }
docker info >/dev/null 2>&1 || { echo "the docker daemon is not reachable" >&2; exit 2; }
"$PY" - <<'PYCHECK' || { echo "SG_E2E_PYTHON ($PY) lacks test requirements" >&2; exit 2; }
import importlib, sys
missing = [m for m in ("numpy", "scipy", "PIL", "arango", "minio", "psycopg", "fastapi", "pydantic", "pytest")
           if importlib.util.find_spec(m) is None]
if missing:
    sys.exit("missing modules: " + ", ".join(missing))
import pydantic
if int(pydantic.VERSION.split(".")[0]) != 1:
    sys.exit(f"pydantic {pydantic.VERSION}: the services pin pydantic 1.x")
PYCHECK

for port in "$SG_E2E_ARANGO_PORT" "$SG_E2E_MINIO_PORT" "$SG_E2E_POSTGRES_PORT"; do
  if ss -ltn 2>/dev/null | awk '{print $4}' | grep -Eq "[:.]${port}$"; then
    # a leftover of an earlier run of this harness is fine (it is removed below), anything else is not
    if ! docker ps --filter name=sg-e2e- --format '{{.Ports}}' | grep -q ":${port}->"; then
      echo "port $port is in use by something that is not this harness's stack" >&2
      exit 2
    fi
  fi
done

cleanup() {
  status=$?
  if [ "${SG_E2E_KEEP_STACK:-0}" = 1 ]; then
    echo "stack left running (project sg-e2e): ${COMPOSE[*]} down -v"
  else
    "${COMPOSE[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
  fi
  exit $status
}
trap cleanup EXIT INT TERM

echo "=== stack: ArangoDB :$SG_E2E_ARANGO_PORT  MinIO :$SG_E2E_MINIO_PORT  Postgres :$SG_E2E_POSTGRES_PORT"
"${COMPOSE[@]}" down -v --remove-orphans >/dev/null 2>&1 || true   # fresh volumes, always
"${COMPOSE[@]}" up -d --wait

export SCENE_GRAPH_E2E=1
export PYTHONPATH="$REPO${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONDONTWRITEBYTECODE=1
# placeholders for modules that insist on secrets at import; the harness passes its own values
export ARANGO_PASSWORD="${ARANGO_PASSWORD:-unused}" MINIO_ACCESS_KEY="${MINIO_ACCESS_KEY:-unused}"
export MINIO_SECRET_KEY="${MINIO_SECRET_KEY:-unused}" POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-unused}"

cd "$REPO"
echo "=== pytest tests/integration/scene_graph"
ARGS=("$@")
if [ ${#ARGS[@]} -eq 0 ]; then ARGS=(tests/integration/scene_graph); fi
# -o addopts="" drops tests/pytest.ini's --showlocals (it would dump the 120 x 120 windows);
# no pytest-randomly: the stages are ordered fixtures, randomisation only reshuffles the log
"$PY" -m pytest "${ARGS[@]}" -p no:randomly -p no:cacheprovider -o addopts="" -v -ra --tb=short -s \
  --timeout=1200 2>&1 | grep -av '^\(INFO\|DEBUG\) '
exit "${PIPESTATUS[0]}"
