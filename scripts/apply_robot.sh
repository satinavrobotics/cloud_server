#!/usr/bin/env bash
# Re-render this robot's env file from its profile + local overrides + secrets, then deploy: what the app's "Apply" does.
#
#   scripts/apply_robot.sh [TAG]       TAG defaults to the tag that runs now (docker_compose/.robot_current_tag)
#
# CONFIG_PROFILE names the profile (pi4, pi5, ...); the app's deploy container sets it. Stops before touching anything if the
# render fails (a missing secret, a bad override). Never pulls, installs or upgrades anything: it only applies what is on the machine.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROFILE="${CONFIG_PROFILE:-}"
[ -n "$PROFILE" ] || { echo "apply_robot: CONFIG_PROFILE is not set (which profile is this machine?)" >&2; exit 1; }
TAG="${1:-}"
[ -n "$TAG" ] || TAG="$(cat "$ROOT/docker_compose/.robot_current_tag" 2>/dev/null || true)"
[ -n "$TAG" ] || { echo "apply_robot: no tag given and none is deployed yet" >&2; exit 1; }
echo "apply_robot: profile $PROFILE, tag $TAG"
"$ROOT/scripts/render_env.sh" "$PROFILE"
exec "$ROOT/scripts/deploy_robot.sh" "$TAG"
