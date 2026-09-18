#!/usr/bin/env bash
set -euo pipefail
# Synthetic load test against the local LiveKit server. Pure software clients —
# no robot hardware, no production config, nothing off this box's own network
# path touched. See docs/livekit_sfu/TESTING.md for phases.
#
# Usage: ./run_loadtest.sh [scenario] [duration]
#   scenario: capacity (default) | dashboard | soak | audio
#   duration: lk load-test --duration value, default 2m

cd "$(dirname "$0")/../../.."   # repo root
set -a; source docker_compose/livekit_sfu.env; set +a
# Loopback by default (phase 1); override for a tailnet run, e.g.
#   LIVEKIT_URL=ws://100.85.3.47:7880 ./run_loadtest.sh capacity 1m
LIVEKIT_URL="${LIVEKIT_URL:-ws://127.0.0.1:7880}"

if ! command -v lk >/dev/null; then
  echo "lk (livekit-cli) not installed. Install with:"
  echo "  curl -sSL https://get.livekit.io/cli | bash"
  exit 1
fi

SCENARIO="${1:-capacity}"
DURATION="${2:-2m}"

# Shapes chosen to match the real workload (many robot-publishers, few
# ops-dashboard subscribers) rather than LiveKit's own "large meeting" /
# "livestream" reference scenarios, which don't match this use case.
case "$SCENARIO" in
  capacity)  ARGS=(--video-publishers 100 --subscribers 5) ;;   # 100 robots, 5 ops watching
  dashboard) ARGS=(--video-publishers 20  --subscribers 20) ;;  # many viewers at once, edge case
  soak)      ARGS=(--video-publishers 100 --subscribers 5) ;;   # same shape, run with a long --duration
  audio)     ARGS=(--audio-publishers 100 --subscribers 5) ;;   # sanity check only, not the real shape
  *) echo "unknown scenario: $SCENARIO (use: capacity|dashboard|soak|audio)"; exit 1 ;;
esac

echo "Scenario '$SCENARIO' against ${LIVEKIT_URL} for ${DURATION}"
echo "Run tests/performance/livekit/monitor.sh in another terminal to capture CPU/bandwidth during this."
lk load-test \
  --url "$LIVEKIT_URL" \
  --api-key "$LIVEKIT_SFU_API_KEY" \
  --api-secret "$LIVEKIT_SFU_API_SECRET" \
  --room "bench-$SCENARIO" \
  --duration "$DURATION" \
  "${ARGS[@]}"
