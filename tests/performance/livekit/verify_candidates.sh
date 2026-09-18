#!/usr/bin/env bash
set -euo pipefail
# While a test client is connecting (run_loadtest.sh, or a real robot in later
# phases), tail the server logs for ICE candidates. Every address must be a
# Tailscale one (100.64.0.0/10, or fd7a:... for IPv6) — anything else means
# node_ip / use_external_ip is misconfigured and a candidate is leaking a
# public or LAN address.
#
# Needs `logging.level: debug` in docker_compose/livekit/livekit.yaml to see candidate lines —
# switch back to `info` afterwards, debug logging is noisy for normal operation.

# Exact match on the SFU's fixed container_name (docker_compose/mission_dispatch_services.yaml).
# A plain `name=livekit` filter also matches livekit-service (the LiveKit Cloud
# token service) and could pick that instead.
CONTAINER="$(docker ps --filter name=^sati_livekit_sfu$ --format '{{.Names}}')"
if [ -z "$CONTAINER" ]; then
  echo "No running sati_livekit_sfu container found (docker compose -f docker_compose/mission_dispatch_services.yaml ps livekit-sfu)."
  exit 1
fi

echo "Watching $CONTAINER logs for ICE candidates (Ctrl+C to stop)."
echo "Expect only 100.x.x.x / fd7a: addresses below:"
docker logs -f "$CONTAINER" 2>&1 | grep -i --line-buffered "candidate"
