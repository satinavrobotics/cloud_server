#!/bin/bash
# Run the unit tests on the production pins (Python 3.10, pydantic 1.9.0, psycopg 3.0.15,
# paho-mqtt 1.6.1) in a throw-away container: the repo is mounted, no network, 2 GB memory cap.
# The host Python (3.12) cannot install pydantic 1.9.0, so a host run proves nothing about v1.
# Usage: scripts/run_unit_tests_pinned.sh [pytest args...]   (default: tests/unit -m unit)
set -euo pipefail
REPO=$(cd "$(dirname "$0")/.." && pwd)
IMAGE=${UNIT_TEST_IMAGE:-sati-unit-tests:py310-pinned}

if [ -n "${REBUILD:-}" ] || ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  docker build -f "$REPO/tests/Dockerfile.unit" -t "$IMAGE" "$REPO"
fi

if [ $# -eq 0 ]; then set -- tests/unit -m unit; fi

exec docker run --rm --memory=2g --network=none \
  -v "$REPO":/repo -w /repo \
  -e ARANGO_PASSWORD=x -e MINIO_ACCESS_KEY=x -e MINIO_SECRET_KEY=x -e POSTGRES_PASSWORD=x \
  "$IMAGE" \
  python -m pytest -p no:randomly -p no:cacheprovider -o addopts="" --timeout=120 -q "$@"
