# Trust model (as of 2026-10-21)

## The barriers

1. **The tailnet (Tailscale).** The one real barrier. The cloud server, the Pi 4 and the desktop app talk to each other over it. A peer on
   the tailnet is trusted; there is no login on the API, the gateway or the orchestrators.
2. **The operations token.** Needed for the restart and deploy actions (`/api/diagnostics/operations/...`) from anywhere that is not
   the tailnet or the machine itself. From the tailnet no token is needed (`OPERATIONS_TRUST_TAILNET=true`, decided 2026-10-21: "fine on
   the tailnet for now"). The caller's address comes from nginx (`X-Real-IP`, set from the connection, so a client cannot choose it).
3. **Secrets in per-machine files.** Passwords and keys live in `docker_compose/secrets.env` on each machine (untracked, mode 600), never
   in git. Settings are tracked as profiles. See [CONFIG_PROFILES.md](../CONFIG_PROFILES.md).

## What is NOT protected

- **No login on the API or the gateway.** Anyone who can reach port 80 of the cloud box or a Pi can use the whole API, including
  starting missions. The robot's gateway listens on loopback and the tailnet address only (the LAN door, `ROBOT_LAN_IP`, was closed on
  2026-10-21). The cloud box's gateway listens on `0.0.0.0:80`, so what protects it is the network around that machine, not the
  machine. Check that port 80 is not reachable from outside the office network.
- **MQTT allows anonymous clients** (`packages/utils/mosquitto.sh`: `allow_anonymous true`, "for testing only"). On a Pi the broker is
  now loopback + tailnet; on the cloud box see `MQTT_PORT_TCP` / `MQTT_PORT_WEBSOCKET` in the compose file for where it listens.
- **The diagnostics service can restart containers and start a deploy container.** Its Docker socket mount says `:ro`, which does not
  stop writes. Whoever may call the operations endpoints (any tailnet peer; anyone else with the token) can restart any service and run
  `scripts/deploy_robot.sh <git sha>` with the repo mounted. The tag must be a git sha and the script is the repo's own, so a caller
  cannot choose what runs, but they can roll a robot to any image that is in the registry.
- **The robot orchestrators have no API auth** by decision (2026-10-11), so the new ESP32 hardware reset and the memory reset are
  protected only by being switched off in `config.yaml`, by their guards and by the tailnet.

## Who is on the tailnet

Every device on the tailnet is trusted with all of the above. Treat a lost laptop or phone as a loss of that trust: remove the device
in the Tailscale admin console.
