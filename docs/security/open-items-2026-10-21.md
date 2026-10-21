# Open security items (2026-10-21)

Decision of 2026-10-21: write these down, do not work on them yet (except the one marked done). Ordered by what an attacker would get.

## 1. Robot LAN door was open: DONE 2026-10-21
The Pi 4's `.env.robot` had `ROBOT_LAN_IP=0.0.0.0` (set by the network configuration work). After a deploy recreated the stack, the
robot's gateway (port 80) and MQTT broker (1883, 9001) listened on every interface, the Pi's `eth0` and `wlan0` included: no login on
the gateway, anonymous MQTT. Anyone on that LAN could start missions and publish robot commands.
Fixed: `ROBOT_LAN_IP` removed from `profiles/pi4.env` and `pi5.env`, rendered and redeployed. The gateway and broker listen on loopback
and the tailnet address only; the Pi's LAN address no longer answers.
If a robot on the local network must reach the broker, do it on purpose: a single LAN address (not `0.0.0.0`) and put authentication on
the broker first (see item 3).

## 2. LiveKit Cloud API key and secret are in git history
`LIVEKIT_API_KEY` / `LIVEKIT_API_SECRET` for the LiveKit Cloud project were committed in the old tracked `docker_compose/.env` and in the
four `docker_compose/.env.robot.bak-*` files (commit `73dcacf`, still in history). They are in `secrets.env` now and no longer tracked,
but anyone who can read the repo's history can read them and mint tokens for that project (join rooms, publish and subscribe to video,
use the quota). Decision 2026-10-21: not rotated yet.
Fix: create a new key pair in the LiveKit Cloud dashboard, put it in `secrets.env` (`LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET`), render,
recreate `livekit-service`, delete the old key in the dashboard. Rotating makes the history copy useless; no history rewrite is needed.

## 3. MQTT has no authentication or encryption
`allow_anonymous true`, plain TCP and WebSocket. Anyone who can reach the broker can read robot state and publish VDA5050 orders.
Fix: a password file and per-client users (dispatcher, graph builder, each robot), `allow_anonymous false`, then TLS. Needs every robot's
client config changed together: plan it as its own change.

## 4. No login on the API, the gateway or the orchestrators
See [trust-model.md](trust-model.md). The tailnet is the barrier. Fix when it matters: an auth layer at the gateway (a shared token for
the app, or OIDC), then per-role limits for the dangerous routes (operations, orchestrator hardware, transfer).

## 5. Operator actions run without a token from the tailnet
`OPERATIONS_TRUST_TAILNET=true` (decided 2026-10-21). Every tailnet peer can restart services and deploy a git sha on a robot. The token
still guards callers that are not on the tailnet. To require the token everywhere: set `OPERATIONS_TRUST_TAILNET=false` in the profiles
and render; the app then asks for the token once per server.

## 6. Old passwords remain in git history
The Postgres, ArangoDB and MinIO passwords and user names that were in the tracked `docker_compose/.env` and the `.env.robot.bak-*`
files were rotated on the cloud server on 2026-10-21, so the history copies open nothing. They are still readable. A history rewrite
(`git filter-repo`) would remove them but needs a force-push of `main` and every clone re-cloned: not worth it unless the repo is shared
outside the team.

## 7. A backup of the Pi's env file with live secrets
`~/env.robot.backup` on the Pi 4 (mode 600) is a copy of `.env.robot` from before the profile change and holds the Pi's current
secrets. Delete it once the profile setup has proven itself. `docker_compose/secrets.env` on each machine is the real store: keep it
out of backups that leave the machine, and never `git add -A` there (it is in `.gitignore`).

## 8. Reconstruction, semantic and mesh service keys
`RECONSTRUCTION_*`, `SEMANTIC_*` and `MESH_*` keys and callback secrets are in `secrets.env` and were never committed. No action; listed
so a later rotation includes them.

## Not security, but could take the system down
- The cloud box's disk is at 98%. Postgres and MinIO stop if it fills.
- The Pi 4 reports under-voltage (`vcgencmd get_throttled` = `0x50005`): replace the power supply.
