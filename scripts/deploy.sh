#!/usr/bin/env bash
# Deploy one or more compose services of this checkout: update the checkout, tag the running image for
# rollback, build, recreate, wait for healthy, verify. Run it on the production host, from anywhere.
#
#   scripts/deploy.sh [options] SERVICE...        deploy (SERVICE names as in mission_dispatch_services.yaml)
#   scripts/deploy.sh rollback SERVICE [TAG]      put the image tagged TAG (default: the last pre-deploy tag) back
#
# Options:
#   --ref REF      git ref to deploy (default origin/main); the checkout is fast-forwarded to it
#   --no-pull      deploy the checkout as it is (no fetch, no checkout change)
#   --no-verify    skip the post-deploy checks (scene graph checker, planner /stats)
#   --dry-run      print what would run, change nothing
#   -h, --help     this text
#
# Environment: COMPOSE_FILE (default docker_compose/mission_dispatch_services.yaml), HEALTH_TIMEOUT_S (180),
# DEPLOY_LOG (default ~/.cache/satina-deploy.log). Settings (.env) are never touched here: edit them first.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="${COMPOSE_FILE:-$ROOT/docker_compose/mission_dispatch_services.yaml}"
HEALTH_TIMEOUT_S="${HEALTH_TIMEOUT_S:-180}"
DEPLOY_LOG="${DEPLOY_LOG:-$HOME/.cache/satina-deploy.log}"
PLANNER_PORT="${PLANNER_PORT:-8005}"
REF="origin/main"; PULL=1; VERIFY=1; DRY=0

usage() { sed -n '2,/^set -e/p' "${BASH_SOURCE[0]}" | sed -e '$d' -e 's/^# \{0,1\}//'; }
log() {                                                              # a dry run never writes the log `rollback` reads
    local line; line="$(date -u +%FT%TZ) $*"
    if [ "$DRY" = 1 ]; then echo "[dry-run] $line" >&2; else echo "$line" | tee -a "$DEPLOY_LOG" >&2; fi
}
die() { printf "deploy: %b\n" "$*" >&2; exit 1; }
run() { if [ "$DRY" = 1 ]; then echo "+ $*" >&2; else "$@"; fi; }
dc() { docker compose -f "$COMPOSE_FILE" "$@"; }

mkdir -p "$(dirname "$DEPLOY_LOG")"

container_of() { dc ps -q "$1" | head -n1; }
image_of() { docker inspect -f '{{.Config.Image}}' "$1"; }          # repo:tag the container runs
wait_healthy() {                                                      # $1 service
    local c status t0; c="$(container_of "$1")"; t0=$SECONDS
    [ -n "$c" ] || return 1
    while :; do
        status="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$c")"
        case "$status" in healthy|running) return 0 ;; esac
        [ $((SECONDS - t0)) -lt "$HEALTH_TIMEOUT_S" ] || return 1
        sleep 3
    done
}

do_rollback() {                                                       # $1 service, $2 tag (optional)
    local svc="$1" tag="${2:-}" c repo
    c="$(container_of "$svc")"; [ -n "$c" ] || die "no container for $svc"
    repo="$(image_of "$c")"; repo="${repo%:*}"
    if [ -z "$tag" ]; then
        tag="$(grep -E " $svc pre-" "$DEPLOY_LOG" 2>/dev/null | tail -n1 | sed -E 's/.* (pre-[^ ]+).*/\1/')"
        [ -n "$tag" ] || die "no pre-deploy tag recorded for $svc in $DEPLOY_LOG; pass TAG"
    fi
    docker image inspect "$repo:$tag" >/dev/null 2>&1 || die "image $repo:$tag does not exist"
    log "rollback $svc to $repo:$tag"
    run docker tag "$repo:$tag" "$repo:latest"
    run dc up -d --no-deps --no-build "$svc"
    [ "$DRY" = 1 ] || wait_healthy "$svc" || die "$svc not healthy after rollback"
    log "rollback $svc done"
}

if [ "${1:-}" = rollback ]; then
    shift; [ $# -ge 1 ] || die "usage: deploy.sh rollback SERVICE [TAG]"
    do_rollback "$@"; exit 0
fi

SERVICES=()
while [ $# -gt 0 ]; do
    case "$1" in
        --ref) REF="${2:?--ref needs a value}"; shift ;;
        --no-pull) PULL=0 ;;
        --no-verify) VERIFY=0 ;;
        --dry-run) DRY=1 ;;
        -h|--help) usage; exit 0 ;;
        -*) die "unknown option $1" ;;
        *) SERVICES+=("$1") ;;
    esac
    shift
done
[ ${#SERVICES[@]} -gt 0 ] || { usage; exit 2; }
for s in "${SERVICES[@]}"; do
    dc config --services | grep -qx "$s" || die "unknown service '$s' (see: docker compose -f $COMPOSE_FILE config --services)"
done

cd "$ROOT"
if [ "$PULL" = 1 ]; then
    # tracked changes block the update, except the machine's own .env (tracked here, edited on the host)
    dirty="$(git status --porcelain --untracked-files=no | grep -v ' docker_compose/\.env$' || true)"
    [ -z "$dirty" ] || die "tracked changes in the checkout:\n$dirty"
    git fetch -q origin
    git rev-parse --verify -q "$REF^{commit}" >/dev/null || die "unknown ref $REF"
    run git merge --ff-only -q "$REF" || die "cannot fast-forward the checkout to $REF"
fi
SHA="$(git rev-parse --short HEAD)"
log "deploy $SHA: ${SERVICES[*]}"

for svc in "${SERVICES[@]}"; do
    c="$(container_of "$svc")"
    if [ -n "$c" ]; then
        repo="$(image_of "$c")"; repo="${repo%:*}"
        prev="pre-$(date -u +%Y%m%d-%H%M%S)-$SHA"
        run docker tag "$(docker inspect -f '{{.Image}}' "$c")" "$repo:$prev"
        log "$svc $prev"                                           # `rollback` reads this line
    fi
    run dc build "$svc"
    run dc up -d --no-deps "$svc"
    if [ "$DRY" = 0 ] && ! wait_healthy "$svc"; then
        log "$svc NOT healthy within ${HEALTH_TIMEOUT_S}s"
        docker logs --tail 40 "$(container_of "$svc")" >&2 || true
        [ -z "${prev:-}" ] || do_rollback "$svc" "$prev"
        die "$svc failed to come up; rolled back"
    fi
    log "$svc healthy at $SHA"
    prev=""
done

if [ "$VERIFY" = 1 ] && [ "$DRY" = 0 ]; then
    for svc in "${SERVICES[@]}"; do
        case "$svc" in
            graph-builder-service)
                log "scene graph checker"
                out="$(dc exec -T -w /app graph-builder-service python -m tools.scene_graph_check_generation \
                    --all --expect tools/scene_graph_expected.json)" || { echo "$out" | tail -n 20 >&2; die "scene graph checker failed"; }
                log "$(printf '%s\n' "$out" | head -n1)" ;;
            mission-planner-service)
                log "planner /stats: $(curl -fsS "http://localhost:$PLANNER_PORT/stats" | head -c 400)" ;;
        esac
    done
fi
log "deploy $SHA done"
