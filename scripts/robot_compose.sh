#!/usr/bin/env bash
# docker compose for the robot stack with the right files, environment file AND image tag, so a hand-typed command cannot
# put one service on a different version than its neighbours (the .env.robot IMAGE_TAG is only a default; the tag that
# scripts/deploy_robot.sh deployed is in docker_compose/.robot_current_tag and wins here).
#
#   scripts/robot_compose.sh up -d --no-build --no-deps graph-builder-service
#   scripts/robot_compose.sh ps | logs -f api-delegation-service | restart nginx-gateway
#
# Prefer scripts/deploy_robot.sh <tag> for a release; this is for changing one service after editing .env.robot.
# Server diagnostics flags a stack whose services run different tags ("All services run the same version").
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DC_DIR="$ROOT/docker_compose"
ENV_FILE="${ENV_FILE:-$DC_DIR/.env.robot}"
[ -f "$ENV_FILE" ] || { echo "robot_compose: no $ENV_FILE" >&2; exit 1; }

if [ -z "${IMAGE_TAG:-}" ] && [ -f "$DC_DIR/.robot_current_tag" ]; then
    IMAGE_TAG="$(tr -d '[:space:]' < "$DC_DIR/.robot_current_tag")"
    export IMAGE_TAG
fi

cd "$DC_DIR"
exec docker compose --env-file "$ENV_FILE" -f mission_dispatch_services.yaml -f mission_dispatch_services_robot.yaml "$@"
