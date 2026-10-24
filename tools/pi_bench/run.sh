#!/usr/bin/env bash
# Run the Pi-like bench against a worktree.
# Usage: tools/pi_bench/run.sh <repo> <out-dir> [rate=1.0] [factor=1] [extra bench args...]
# Container: 1 CPU, 896 MiB (the graph-builder limit of the robot compose file), no swap, repo mounted read-only.
# Uses only `docker run --rm` (host networking is not used and no running container is touched).
set -u
REPO="$(cd "${1:?repo}" && pwd)"
OUT="${2:?out dir}"
RATE="${3:-1.0}"
FACTOR="${4:-1}"
shift 2
[ $# -gt 0 ] && shift
[ $# -gt 0 ] && shift
IMAGE="${BENCH_IMAGE:-sati-unit-tests:py310-pinned}"
HERE="$(cd "$(dirname "$0")" && pwd)"
mkdir -p "$OUT"
OUT="$(cd "$OUT" && pwd)"
tag="r${RATE}_f${FACTOR}"

MOUNTS=()
[ -n "${BENCH_OFFICE8:-}" ] && MOUNTS=(-v "$BENCH_OFFICE8":/office8:ro)   # --world office8: dir with nodes.json + <key>.png

run() {
  docker run --rm --user "$(id -u):$(id -g)" --cpus=1 --memory=896m --memory-swap=896m -e PYTHONDONTWRITEBYTECODE=1 \
    -v "$REPO":/repo:ro -v "$HERE":/bench:ro -v "$OUT":/work "${MOUNTS[@]}" "$IMAGE" python3 -I /bench/bench.py "$@" --repo /repo
  rc=$?
  [ "$rc" = 137 ] && echo "OOM-KILLED (exit 137) in: $*" >&2
  return $rc
}

if [ "${ONLY:-}" != restart ]; then
  run ingest --rate "$RATE" --factor "$FACTOR" --store "/work/store_$tag" --out "/work/ingest_$tag.json" "$@" || exit $?
fi
# ONLY=restart: just the cold restart on a store an earlier run left in <out-dir>
run restart --rate "$RATE" --store "/work/store_$tag" --out "/work/restart_$tag.json" --max-real-s "${RESTART_MAX_REAL_S:-600}" "$@"
