#!/usr/bin/env bash
set -euo pipefail
# Run alongside run_loadtest.sh (separate terminal) to capture resource usage
# for the pass/fail thresholds in docs/livekit_sfu/TESTING.md.

OUT="${1:-/tmp/livekit-bench-$(date +%s).log}"
echo "Logging to $OUT (Ctrl+C to stop)"

# Exact match on the SFU's fixed container_name (docker_compose/mission_dispatch_services.yaml).
# A plain `name=livekit` filter also matches livekit-service (the LiveKit Cloud
# token service) and could pick that instead.
CONTAINER="$(docker ps --filter name=^sati_livekit_sfu$ --format '{{.Names}}')"

# NOTE: docker_compose/mission_dispatch_services.yaml runs with network_mode: host (needed for the
# wide UDP ICE port range), which means `docker stats`' NET I/O column is
# always 0B/0B for this container -- Docker attributes host-network traffic to
# the host's interfaces, not the container, so it structurally can't see it.
# CPU%/MEM are unaffected (cgroup-based, not network-namespace-based) and stay
# accurate. Bandwidth is tracked separately below via tailscale0's own byte
# counters (confirmed during phase 1 testing: docker stats showed 0B/0B while
# `ip -s link show tailscale0` showed real, substantial traffic).
read_ts0_bytes() {
  ip -s link show tailscale0 2>/dev/null | awk '/RX:/{getline; rx=$1} /TX:/{getline; tx=$1} END{print rx, tx}'
}
prev="$(read_ts0_bytes)"
prev_rx="${prev%% *}"; prev_tx="${prev##* }"

{
  echo "=== $(date) — container: ${CONTAINER:-none found} ==="
  while true; do
    date +%T
    if [ -n "${CONTAINER:-}" ]; then
      docker stats --no-stream "$CONTAINER"
    fi
    cur="$(read_ts0_bytes)"
    cur_rx="${cur%% *}"; cur_tx="${cur##* }"
    echo "tailscale0: rx=$(( (cur_rx - prev_rx) / 5 ))B/s tx=$(( (cur_tx - prev_tx) / 5 ))B/s (5s window)"
    prev_rx="$cur_rx"; prev_tx="$cur_tx"
    # LiveKit's own metrics (see prometheus.port in docker_compose/livekit/livekit.yaml)
    curl -s http://127.0.0.1:6789/metrics 2>/dev/null \
      | grep -E '^(livekit_|go_goroutines|process_cpu)' || true
    echo "---"
    sleep 5
  done
} | tee -a "$OUT"
