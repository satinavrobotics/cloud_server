#!/usr/bin/env bash
# Build the Python service images as one multi-arch manifest (linux/amd64 + linux/arm64 by default) with buildx,
# and push them to the private registry (sati_docker_registry). Nothing here touches the local docker image store,
# the compose file or the running stack: the cloud keeps building its own native images with restart_services.sh /
# scripts/deploy.sh. This is the producer side of a robot (Raspberry Pi) deploy, which only pulls.
#
#   scripts/build_push.sh [options] [SERVICE...]     SERVICE = compose service names (default: all 8)
#
# Options:
#   --check          build only, push nothing (cache only): proves every platform builds, wheels included
#   --platforms P    comma list (default linux/amd64,linux/arm64; env PLATFORMS)
#   --tag TAG        image tag (default: the short git sha; env TAG)
#   --no-latest      do not also push :latest
#   --allow-dirty    push from a tree with uncommitted changes (tagged <sha>-dirty, never :latest)
#   --dry-run        print the commands, run nothing
#   -h, --help       this text
#
# Images land at $REGISTRY/$REPO/<image>:<tag>, where <image> is the default name in mission_dispatch_services.yaml
# (mission_dispatch, api_delegation_service, ...). Layers are cached in the registry (cache/<image>), so a repeat
# build after a small change is fast. A push needs a clean tree: the build context is the working tree, so a sha tag
# on a dirty tree would not name what is in the image.
#
# Environment: REGISTRY (100.85.3.47:5050), REPO (sati), BUILDER (sati-cross), PLATFORMS, TAG.
# The builder must be allowed to push to the plain-HTTP registry (its BuildKit does not read /etc/docker/daemon.json);
# if it does not exist this script creates it with that allowance.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REGISTRY="${REGISTRY:-100.85.3.47:5050}"
REPO="${REPO:-sati}"
BUILDER="${BUILDER:-sati-cross}"
PLATFORMS="${PLATFORMS:-linux/amd64,linux/arm64}"
TAG="${TAG:-}"
CHECK=0; LATEST=1; DIRTY_OK=0; DRY=0

# compose service  image name                  Dockerfile (relative to the repo root; the context is the repo root)
SERVICES=(
    "mission-dispatch|mission_dispatch|packages/controllers/mission/Dockerfile"
    "graph-builder-service|graph_builder_service|packages/services/graph_builder/Dockerfile"
    "mission-planner-service|mission_planner_service|packages/services/mission_planner/Dockerfile"
    "livekit-service|livekit_service|packages/services/livekit/Dockerfile"
    "livekit-sfu-tokens|livekit_sfu_tokens|packages/services/livekit_sfu_tokens/Dockerfile"
    "agent-orchestrator-service|agent_orchestrator_service|packages/services/agent_orchestrator/Dockerfile"
    "api-delegation-service|api_delegation_service|packages/api/Dockerfile"
    # MinIO no longer publishes images: built from source at the pinned release (the robot pulls it from here).
    # The optional 4th field is the build context (default: the repo root).
    "minio|minio|docker/minio/Dockerfile|docker/minio"
    # The LiveKit SFU, mirrored (FROM the pinned upstream image) so the offline robot pulls it from this registry too.
    "livekit-sfu|livekit_sfu|docker/livekit/Dockerfile|docker/livekit"
)

usage() { sed -n '2,/^set -e/p' "${BASH_SOURCE[0]}" | sed -e '$d' -e 's/^# \{0,1\}//'; }
die() { printf "build_push: %b\n" "$*" >&2; exit 1; }
run() { if [ "$DRY" = 1 ]; then printf '[dry-run]'; printf ' %q' "$@"; printf '\n'; else "$@"; fi; }

WANT=()
while [ $# -gt 0 ]; do
    case "$1" in
        --check) CHECK=1 ;;
        --platforms) [ $# -ge 2 ] || die "--platforms needs a value"; PLATFORMS="$2"; shift ;;
        --tag) [ $# -ge 2 ] || die "--tag needs a value"; TAG="$2"; shift ;;
        --no-latest) LATEST=0 ;;
        --allow-dirty) DIRTY_OK=1 ;;
        --dry-run) DRY=1 ;;
        -h|--help) usage; exit 0 ;;
        -*) die "unknown option $1 (see --help)" ;;
        *) WANT+=("$1") ;;
    esac
    shift
done

# --- which services
SELECTED=()
if [ ${#WANT[@]} -eq 0 ]; then
    SELECTED=("${SERVICES[@]}")
else
    for w in "${WANT[@]}"; do
        found=""
        for entry in "${SERVICES[@]}"; do
            [ "${entry%%|*}" = "$w" ] && found="$entry"
        done
        [ -n "$found" ] || die "unknown service '$w'; known: $(printf '%s ' "${SERVICES[@]%%|*}")"
        SELECTED+=("$found")
    done
fi

# --- tag and tree state
SHA="$(git -C "$ROOT" rev-parse --short=12 HEAD)"
DIRTY=0
# untracked files count: the build context is the working tree, so a new file would ship in an image its sha does not have
[ -z "$(git -C "$ROOT" status --porcelain --untracked-files=normal -- . ':!.claude' ':!.serena')" ] || DIRTY=1
if [ "$CHECK" = 0 ] && [ "$DIRTY" = 1 ] && [ "$DIRTY_OK" = 0 ]; then
    die "the working tree has uncommitted changes; commit them, or pass --allow-dirty (tag ${SHA}-dirty, no :latest)"
fi
if [ "$DIRTY" = 1 ]; then LATEST=0; fi
if [ -z "$TAG" ]; then TAG="$SHA"; fi
[ "$DIRTY" = 0 ] || case "$TAG" in *-dirty) ;; *) TAG="${TAG}-dirty" ;; esac

# --- builder
if ! docker buildx inspect "$BUILDER" >/dev/null 2>&1; then
    [ "$DRY" = 1 ] || {
        CFG="$(mktemp)"; trap 'rm -f "$CFG"' EXIT
        printf '[registry."%s"]\n  http = true\n  insecure = true\n' "$REGISTRY" > "$CFG"
        echo "creating buildx builder '$BUILDER'" >&2
        docker buildx create --name "$BUILDER" --driver docker-container --config "$CFG" >/dev/null
    }
fi

echo "build_push: ${#SELECTED[@]} image(s), platforms $PLATFORMS, tag $TAG$([ "$CHECK" = 1 ] && echo ', check only (no push)')" >&2

for entry in "${SELECTED[@]}"; do
    IFS='|' read -r svc image dockerfile context <<<"$entry"
    ref="$REGISTRY/$REPO/$image"
    cache="$REGISTRY/$REPO/cache/$image"
    args=(docker buildx build --builder "$BUILDER" --platform "$PLATFORMS"
          -f "$ROOT/$dockerfile"
          --label "org.opencontainers.image.revision=$SHA"
          --label "org.opencontainers.image.source=cloud_server/$dockerfile"
          --cache-from "type=registry,ref=$cache")
    if [ "$CHECK" = 1 ]; then
        args+=(--output type=cacheonly)
    else
        args+=(--cache-to "type=registry,ref=$cache,mode=max,ignore-error=true" --push -t "$ref:$TAG")
        [ "$LATEST" = 0 ] || args+=(-t "$ref:latest")
    fi
    args+=("$ROOT/${context:-.}")
    echo "== $svc -> $ref:$TAG" >&2
    run "${args[@]}"
done

if [ "$CHECK" = 0 ] && [ "$DRY" = 0 ]; then
    echo "pushed. On the robot: IMAGE_TAG=$TAG" >&2
fi
