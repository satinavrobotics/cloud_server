#!/bin/bash
# Run the unit tests on the production pins (Python 3.10, pydantic 1.9.0, psycopg 3.0.15,
# paho-mqtt 1.6.1) in a throw-away container: the repo is mounted, no network, 2 GB memory cap.
# The host Python (3.12) cannot install pydantic 1.9.0, so a host run proves nothing about v1.
# Usage: scripts/run_unit_tests_pinned.sh [pytest args...]   (default: tests/unit -m unit)
#
# A pass of the DEFAULT (full) run on a clean tracked tree is recorded as $TEST_MARKS/<short sha> (default
# ~/.cache/satina-tests): scripts/deploy.sh refuses a sha without that record. A run with arguments records nothing.
set -euo pipefail
REPO=$(cd "$(dirname "$0")/.." && pwd)
IMAGE=${UNIT_TEST_IMAGE:-sati-unit-tests:py310-pinned}
TEST_MARKS="${TEST_MARKS:-$HOME/.cache/satina-tests}"

if [ -n "${REBUILD:-}" ] || ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  docker build -f "$REPO/tests/Dockerfile.unit" -t "$IMAGE" "$REPO"
fi

FULL=0
if [ $# -eq 0 ]; then set -- tests/unit -m unit; FULL=1; fi

rc=0
docker run --rm --memory=2g --network=none \
  -v "$REPO":/repo -w /repo \
  -e ARANGO_PASSWORD=x -e MINIO_ACCESS_KEY=x -e MINIO_SECRET_KEY=x -e POSTGRES_PASSWORD=x \
  -e PYTHONDONTWRITEBYTECODE=1 \
  "$IMAGE" \
  python -m pytest -p no:randomly -p no:cacheprovider -o addopts="" --timeout=120 -q "$@" || rc=$?

if [ "$rc" = 0 ] && [ "$FULL" = 1 ]; then
  # the tested tree is the commit only when no tracked file differs (the machine's own .env is not code)
  dirty="$(git -C "$REPO" status --porcelain --untracked-files=no | grep -v ' docker_compose/\.env$' || true)"
  if [ -z "$dirty" ]; then
    mkdir -p "$TEST_MARKS"
    sha="$(git -C "$REPO" rev-parse --short HEAD)"
    printf '%s %s\n' "$(date -u +%FT%TZ)" "tests/unit -m unit passed" > "$TEST_MARKS/$sha"
    echo "recorded a passing full run for $sha ($TEST_MARKS/$sha)" >&2
  else
    echo "full run passed, but tracked changes exist: no record written" >&2
  fi
fi
exit "$rc"
