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
#   --no-verify    skip the post-deploy checks (scene graph checker, planner /stats, build sha)
#   --skip-test-gate  deploy a sha without a recorded passing full unit run (logged)
#   --dry-run      print what would run, change nothing
#   -h, --help     this text
#
# The test gate: scripts/run_unit_tests_pinned.sh records a pass for the checked-out sha when the default (full)
# suite passes on a clean tree; a deploy of a sha without that record is refused. After a healthy deploy the newest
# KEEP_ROLLBACK (default 3) `pre-*` rollback tags of the service's image are kept, older ones are untagged.
#
# Environment: COMPOSE_FILE (default docker_compose/mission_dispatch_services.yaml), HEALTH_TIMEOUT_S (180),
# DEPLOY_LOG (default ~/.cache/satina-deploy.log), TEST_MARKS (default ~/.cache/satina-tests), KEEP_ROLLBACK (3).
# Settings (.env) are never touched here: edit them first.
set -euo pipefail

SELF_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="${COMPOSE_FILE:-$SELF_ROOT/docker_compose/mission_dispatch_services.yaml}"
ROOT="$(cd "$(dirname "$COMPOSE_FILE")/.." && pwd)"                 # the checkout the compose file belongs to
HEALTH_TIMEOUT_S="${HEALTH_TIMEOUT_S:-180}"
DEPLOY_LOG="${DEPLOY_LOG:-$HOME/.cache/satina-deploy.log}"
PLANNER_PORT="${PLANNER_PORT:-8005}"
TEST_MARKS="${TEST_MARKS:-$HOME/.cache/satina-tests}"
KEEP_ROLLBACK="${KEEP_ROLLBACK:-3}"
REF="origin/main"; PULL=1; VERIFY=1; DRY=0; GATE=1

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

prune_rollback_tags() {                                               # $1 repo, $2 running image id: keep the newest N
    local repo="$1" running="$2" n=0 tag id
    while read -r tag id; do
        n=$((n + 1))
        [ "$n" -gt "$KEEP_ROLLBACK" ] || continue
        [ "$id" != "$running" ] || continue                           # never untag what runs
        log "prune rollback tag $repo:$tag"
        run docker rmi "$repo:$tag" >/dev/null
    done < <(docker images "$repo" --format '{{.Tag}} {{.ID}} {{.CreatedAt}}' | grep '^pre-' | sort -k3,4r | awk '{print $1, $2}')
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
        --skip-test-gate) GATE=0 ;;
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
if [ ! -f "$TEST_MARKS/$SHA" ]; then
    if [ "$GATE" = 1 ]; then
        die "no passing full unit run recorded for $SHA ($TEST_MARKS/$SHA): run scripts/run_unit_tests_pinned.sh on this commit, or pass --skip-test-gate"
    fi
    log "test gate skipped for $SHA (no recorded full unit run)"
fi
export GIT_SHA="$SHA"                                                # compose passes it to the service as BUILD_SHA
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
    if [ "$DRY" = 0 ]; then
        c="$(container_of "$svc")"; repo="$(image_of "$c")"; repo="${repo%:*}"
        prune_rollback_tags "$repo" "$(docker inspect -f '{{.Image}}' "$c" | sed 's/^sha256://' | cut -c1-12)"
    fi
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
                out="$(curl -fsS "http://localhost:$PLANNER_PORT/stats")" || die "planner /stats not reachable"
                log "planner /stats: $(printf '%s' "$out" | head -c 400)"
                printf '%s' "$out" | grep -q "\"build_sha\": *\"$SHA\"" \
                    || die "planner /stats does not report build_sha $SHA: the running image is not this build" ;;
        esac
    done
fi
log "deploy $SHA done"
