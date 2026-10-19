#!/usr/bin/env bash
# Deploy the stack on the robot (Raspberry Pi): pull the images that scripts/build_push.sh pushed, start them, wait for
# healthy, and go back to the previous tag if they do not come up. Nothing is built on the Pi.
#
#   scripts/deploy_robot.sh [options] [TAG]          TAG = image tag (git sha from build_push.sh; default: IMAGE_TAG in the env file)
#
# Options:
#   --env-file F     environment file (default docker_compose/.env.robot)
#   --pull-only      pull the images, change nothing running
#   --rollback       redeploy the tag recorded before the last deploy (docker_compose/.robot_previous_tag)
#   --timeout S      seconds to wait for healthy (default 300)
#   --dry-run        print the docker commands, run nothing
#   -h, --help       this text
#
# The running tag is recorded in docker_compose/.robot_current_tag, the one before it in .robot_previous_tag.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DC_DIR="$ROOT/docker_compose"
ENV_FILE="$DC_DIR/.env.robot"
TIMEOUT_S=300
PULL_ONLY=0; ROLLBACK=0; DRY=0; TAG=""
CUR="$DC_DIR/.robot_current_tag"; PREV="$DC_DIR/.robot_previous_tag"

usage() { sed -n '2,/^set -e/p' "${BASH_SOURCE[0]}" | sed -e '$d' -e 's/^# \{0,1\}//'; }
die() { printf "deploy_robot: %b\n" "$*" >&2; exit 1; }
log() { printf "deploy_robot: %b\n" "$*" >&2; }

while [ $# -gt 0 ]; do
    case "$1" in
        --env-file) [ $# -ge 2 ] || die "--env-file needs a value"; ENV_FILE="$2"; shift ;;
        --pull-only) PULL_ONLY=1 ;;
        --rollback) ROLLBACK=1 ;;
        --timeout) [ $# -ge 2 ] || die "--timeout needs a value"; TIMEOUT_S="$2"; shift ;;
        --dry-run) DRY=1 ;;
        -h|--help) usage; exit 0 ;;
        -*) die "unknown option $1 (see --help)" ;;
        *) [ -z "$TAG" ] || die "one TAG only"; TAG="$1" ;;
    esac
    shift
done

[ -f "$ENV_FILE" ] || die "no $ENV_FILE (copy docker_compose/.env.robot.example and fill it in)"
case "$TIMEOUT_S" in ''|*[!0-9]*) die "--timeout needs a number of seconds" ;; esac
if grep -qE 'CHANGE_ME|100\.x\.y\.z' "$ENV_FILE" && [ "$DRY" = 0 ]; then die "$ENV_FILE still has placeholders (CHANGE_ME / 100.x.y.z)"; fi
if [ "$ROLLBACK" = 1 ]; then
    [ -s "$PREV" ] || die "no previous tag recorded ($PREV)"
    TAG="$(cat "$PREV")"
fi
[ -n "$TAG" ] || TAG="$(sed -n 's/^IMAGE_TAG=\([^ #]*\).*/\1/p' "$ENV_FILE" | tail -1)"
[ -n "$TAG" ] || die "no tag: pass one, or set IMAGE_TAG in $ENV_FILE"

dc() {
    IMAGE_TAG="$TAG" docker compose --env-file "$ENV_FILE" \
        -f "$DC_DIR/mission_dispatch_services.yaml" -f "$DC_DIR/mission_dispatch_services_robot.yaml" "$@"
}
# dry-run prints the compose command; other commands are printed as given
run() {
    if [ "$DRY" = 1 ]; then
        printf '[dry-run]'
        if [ "$1" = dc ]; then shift; printf ' docker compose --env-file %q -f mission_dispatch_services.yaml -f mission_dispatch_services_robot.yaml' "$ENV_FILE"; fi
        printf ' %q' "$@"; printf '\n'
    else "$@"; fi
}

# Compose >= 2.24 is needed for `!override` / `!reset` in the robot layer.
ver="$(docker compose version --short 2>/dev/null | sed 's/^v//')"
[ -n "$ver" ] || die "docker compose is not available"
[ "$(printf '%s\n2.24.0\n' "$ver" | sort -V | head -1)" = "2.24.0" ] || die "docker compose $ver is too old (need >= 2.24)"

log "tag $TAG, env $ENV_FILE"
run dc config --quiet
run dc pull
[ "$PULL_ONLY" = 0 ] || { log "pulled; nothing restarted"; exit 0; }

old=""; [ ! -s "$CUR" ] || old="$(cat "$CUR")"

# Every service of the robot stack must exist and be running; those with a healthcheck must be healthy.
wait_healthy() {
    local deadline=$(( $(date +%s) + TIMEOUT_S )) bad want
    want="$(dc config --services | sort)"
    while :; do
        bad="$( { dc ps -a --format '{{.Service}} {{.State}} {{.Health}}' | awk '$2!="running" || ($3!="" && $3!="healthy") {print $1 "(" $2 "/" ($3==""?"-":$3) ")"}'
                  comm -23 <(printf '%s\n' "$want") <(dc ps -a --format '{{.Service}}' | sort) | sed 's/$/(missing)/'; } | tr '\n' ' ')"
        [ -z "$bad" ] && return 0
        [ "$(date +%s)" -lt "$deadline" ] || { log "not healthy after ${TIMEOUT_S}s: $bad"; return 1; }
        sleep 5
    done
}

# up -d itself fails when a dependency is unhealthy: that is a failed deploy too, not an abort.
up_ok=1
run dc up -d --no-build --remove-orphans || up_ok=0
# Always recreate the gateway: its config files are bind-mounted, and `git pull` replaces a file (new inode) while a running
# container keeps the old one, so an unchanged compose definition would leave nginx on stale config (a 502 for a route that was
# fixed in git). Recreating takes a few seconds.
[ "$up_ok" = 0 ] || run dc up -d --no-build --no-deps --force-recreate nginx-gateway || up_ok=0

if [ "$DRY" = 1 ]; then log "dry run: would wait up to ${TIMEOUT_S}s for healthy"; exit 0; fi

if [ "$up_ok" = 1 ] && wait_healthy; then
    [ "$old" = "$TAG" ] || { [ -z "$old" ] || printf '%s\n' "$old" > "$PREV"; }
    printf '%s\n' "$TAG" > "$CUR"
    log "up, healthy, tag $TAG"
    exit 0
fi

dc ps -a >&2 || true
if [ -n "$old" ] && [ "$old" != "$TAG" ] && [ "$ROLLBACK" = 0 ]; then
    log "rolling back to $old (database migrations are NOT undone: the API runs alembic upgrade on start)"
    TAG="$old"
    if dc up -d --no-build --remove-orphans && wait_healthy; then log "rolled back to $old"; else log "rollback to $old is not healthy either"; fi
    die "deploy of the new tag failed; back on $old"
fi
die "deploy failed (no previous tag to roll back to)"
