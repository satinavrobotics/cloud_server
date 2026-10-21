# Robot (Raspberry Pi) deploy

The same stack as the cloud, smaller, on a Raspberry Pi 4 (linux/arm64). No sync or share with the cloud. Images are built
on the cloud host for amd64 + arm64, pushed to the private registry, and the Pi only pulls and runs them.

| File | Role |
|---|---|
| `scripts/build_push.sh` | builds the 9 images (7 services + MinIO + the LiveKit SFU mirror) multi-arch and pushes them to `100.85.3.47:5050/sati/…` |
| `docker_compose/mission_dispatch_services_robot.yaml` | override over the main compose file: services off, sizes, registry images |
| `docker_compose/profiles/<robot>.env` + `secrets.env` | the robot's settings (tracked profile) and secrets (untracked); `scripts/render_env.sh <robot>` writes `.env.robot` from them: [CONFIG_PROFILES.md](CONFIG_PROFILES.md). `.env.robot.example` is only a reference of the keys |
| `scripts/deploy_robot.sh` | pull, `up -d --no-build`, wait healthy, roll back to the previous tag |
| `docker_compose/reconstruction_service_robot.yaml` | optional raw reconstruction (no MapAnything), see below |

## What runs on the robot

On: mosquitto, postgres, arangodb, minio, minio-tailnet, mission-dispatch, graph-builder, mission-planner, api, nginx-gateway, livekit-sfu, livekit-sfu-tokens.
`minio-tailnet` stays: presigned URLs are signed for `MINIO_HOST`, which is the Pi's tailnet IP.

Off (profile `cloud-only`): livekit-service (LiveKit Cloud tokens: needs the internet), agent-orchestrator-service (needs an LLM
API). The scene-graph worker (in graph-builder) and planner are on in `.env.robot.example` (`SCENE_GRAPH_WORKER_ENABLED` /
`SCENE_GRAPH_PLANNER_ENABLED`; both default to false in the code), with memory caps in the robot compose file; their real memory use on a Pi is not measured yet.

Robot LAN (the Jetson and the Pis talk over it): each Pi's address is written in its profile and used by compose and by the LAN script.

| Machine | LAN address | Interface | Wi-Fi | Notes |
|---|---|---|---|---|
| Jetson | `192.168.1.50` | | | the robot |
| Pi 4 (`raspberrypi`) | `192.168.1.60` (`pi4.env`) | `eth0`, no gateway | on: the internet and Tailscale come through wlan0 | |
| Pi 5 (`rasp`) | `192.168.1.70` (`pi5.env`) | `eth0`, router `192.168.1.1` | off: the internet and Tailscale come through eth0 | 4 KB-page kernel (`kernel=kernel8.img`) |

`ROBOT_LAN_IP` (profile) makes the gateway (80) and the MQTT broker (1883, 9001) listen on that ONE address as well as loopback and the tailnet,
so the Jetson can use `<ROBOT_LAN_IP>:1883`. Never `0.0.0.0`: the gateway has no login and the broker allows anonymous clients, so whoever can
reach them can drive the robot (on 2026-10-21 a Pi with `0.0.0.0` had both open on its wifi and ethernet; see
[security/open-items-2026-10-21.md](security/open-items-2026-10-21.md)). The robot LAN must hold only the robot's own devices.

`scripts/configure_pi_lan.sh --profile pi4|pi5 check|apply|rollback` (run on the Pi, as root, through Tailscale, never over `eth0`) sets the
Pi's `eth0` address from the profile (`ROBOT_LAN_IP`, `LAN_JETSON_IP`, `LAN_IFACE`, `LAN_WLAN_OFF`, `LAN_GATEWAY`); `check` is read-only, `apply`
undoes itself if its checks fail. After a change to `ROBOT_LAN_IP`, `render_env.sh <profile>` and a deploy recreate the gateway and the broker.

Which network for what: Tailscale for everything that involves a person, the app or the cloud; the robot LAN only for the robot's own link to its Pi.

| Use | Network | Why |
|---|---|---|
| The app to the cloud and to the Pis | Tailscale | Works from anywhere, and it is the one place that checks who is connecting. |
| Cloud to Pi (data transfer, deploys, image pulls, diagnostics) | Tailscale | Same. |
| Restart and deploy buttons (operator actions) | Tailscale | Those endpoints trust the tailnet. |
| The Jetson to its Pi (MQTT orders and state, the Pi's API) | LAN if wanted, else Tailscale | Fast, and it keeps working when the Wi-Fi or internet is down. |
| Video and recording uploads | Tailscale | The Pi's video server and its upload address are bound to the tailnet address only. |

Tailscale authenticates, the LAN does not: the gateway and the broker have no login, which is why the robot LAN holds only the robot's own devices.
The Jetson's orchestrator derives its MQTT, registration and video addresses from one setting (`server.host`); pointing that at a LAN address would
break video and uploads (tailnet-only), so move only the MQTT link to the LAN, and only with a separate MQTT address setting.

No upgrades on a robot: the machines are never upgraded in place (no `apt upgrade`, no `rpi-update`, no unattended upgrades, no kernel or
firmware change). Updates are made and tested on the server and then deployed to the robots with `scripts/build_push.sh` and
`scripts/deploy_robot.sh` (or the app's Deploy button). The Pi 5 in particular runs a pinned 4 KB-page kernel: a firmware or kernel change
can break it. `apt-daily`, `apt-daily-upgrade` and `unattended-upgrades` are disabled on the Pis.

Network: Postgres, ArangoDB, the planner and graph-builder listen on `127.0.0.1` only; mosquitto on `127.0.0.1` and the tailnet IP
(`ROBOT_TAILNET_IP`), plus the single `ROBOT_LAN_IP` address when it is set (a robot on the local network connects its VDA5050 client to `<ROBOT_LAN_IP>:1883`); the API (8000) and the MinIO forwarder are the doors. Mosquitto still allows anonymous clients, so
Tailscale (ACLs) is the boundary: keep the ACL tight. Every container has a memory limit and a rotated log (10 MB x 3);
mosquitto logs errors/warnings only.

Memory: the defaults are for a **4 GB** Pi (limits sum to ~3.7 GB with the gateway and video, so it is tight: watch it; 8 GB values are in `profiles/pi5.env`). `PG_TUNE_*` apply
only at the first initdb of the Postgres volume.

## One-time setup of the Pi

Prerequisites (the script checks them and stops with the reason): 64-bit OS, data on an SSD, Docker Engine with Compose >= 2.24
(`systemctl enable docker`), Tailscale up, `docker info` without "No memory limit support" (else add
`cgroup_enable=memory cgroup_memory=1` to `cmdline.txt`), time sync (the Pi has no RTC; presigned URLs fail on clock skew),
swap or zram, and docker starting after tailscaled (systemd drop-in `After=tailscaled.service`, else mosquitto and
minio-tailnet restart-loop at boot).

```bash
git clone <repo> && cd cloud_server
scripts/setup_robot.sh --deploy --tag <sha>     # setup, then the first deploy
```

`scripts/setup_robot.sh` allows the plain-HTTP registry in `/etc/docker/daemon.json` (asks first, restarts docker), creates
the `sati_pgdata17` volume and the MinIO directory, creates the robot's profile `docker_compose/profiles/<name>.env` (`--profile NAME`,
default this host's name; from this host's tailnet IP: review and commit it) and `docker_compose/secrets.env` with fresh random passwords, and
renders `docker_compose/.env.robot` from them. It never overwrites an existing profile or `secrets.env` and is safe to run again. Never copy the
cloud's `secrets.env`.

## Release (on the cloud host)

```bash
scripts/build_push.sh            # clean tree required; pushes <sha> and :latest. --check builds without pushing.
```

## Deploy (on the Pi)

```bash
git pull
scripts/deploy_robot.sh <sha>    # tag printed by build_push.sh; --pull-only to fetch first; --rollback to go back
```

Rolling back restores the old images but **not** the database schema (the API runs `alembic upgrade` on start); take a
`pg_dump` first when a release has a migration. `deploy_robot.sh` refuses a `.env.robot` that still has placeholders.

`deploy_robot.sh` records the running and the previous tag (`docker_compose/.robot_current_tag`, `.robot_previous_tag`).
If a new tag is not healthy within the timeout, it redeploys the previous one.

## Telling the app what this robot has

The API answers `GET /api/v1/server-info` from its own configuration, so the app knows what is on this server and hides
the rest. On the robot set `SERVER_KIND=robot` in `.env.robot` (see the example). The scene-graph, reconstruction and
semantic entries come from the settings that already switch those services on; "available" means **configured**, not
running (the URL is set, the service may still be down).

## Video (the robot's own LiveKit server)

The robot has no internet at runtime, so video is local: `livekit-sfu` runs on the Pi, the Jetson robots publish to it and
clients (the desktop app) subscribe, exactly as with the cloud's SFU. `livekit-sfu-tokens` hands out the role-scoped tokens.
Both are in `mission_dispatch_services_robot.yaml` and deploy with the rest.

- **Small config** (`docker_compose/livekit/livekit-robot.yaml`): one node, no Redis, no TURN, no STUN, no metrics port, log
  level `warn`, 100 UDP media ports (50000-50100, one per connected peer), memory limit 192 MB (`MEM_LIVEKIT`). Measured idle:
  about 11 MiB (on the cloud host, not on a Pi). LiveKit has no peer-to-peer mode; it always relays, which is what keeps the
  load on the Pi low (it forwards, never transcodes).
- **Where it listens:** signalling (7880) on loopback + `ROBOT_TAILNET_IP`, TCP fallback 7881, media UDP 50000-50100. The token
  service listens on `ROBOT_TAILNET_IP:8008` (robots call `/api/createToken` there; the gateway forwards only the operator route).
  `node_ip` is the tailnet IP, so media goes over Tailscale. If the Pi must work with no Tailscale session at all (LAN only), this
  needs a LAN-IP variant: not built (the gateway's LAN door below carries the REST and websocket traffic; video media still goes to the tailnet IP).
- **Keys:** `LIVEKIT_SFU_API_KEY` / `LIVEKIT_SFU_API_SECRET` in `.env.robot` (`setup_robot.sh` generates a fresh pair). The robots
  and clients are told `ws://<ROBOT_TAILNET_IP>:7880` (`LIVEKIT_SFU_SERVER_URL` overrides it). The robots' video client must be
  pointed at this Pi's token service (`http://<ROBOT_TAILNET_IP>:8008`) instead of the cloud's.
- **Image:** `livekit-server` is mirrored into the private registry by `build_push.sh` (`docker/livekit/Dockerfile`), so the Pi
  pulls it from there with everything else.
- `SERVER_LIVEKIT_AVAILABLE=true` in `.env.robot` tells the app this server has video.
- Not done: an https `wss://` address for a browser dashboard (`tailscale serve`); the desktop app uses `ws://`.

## Gateway (nginx)

The app talks to one address and the gateway forwards by path: `/api/` and `/ws/` to the API (:8000),
`/api/agent/` and `/ws/agent/` to the agent (:8007), `/api/livekit-selfhost/` to the LiveKit token service (:8008, on the tailnet IP on a robot).
The routing is one file, `docker_compose/nginx/routes.conf`, shared by the cloud gateway
(`nginx-gateway.yaml`, which also fronts the Expo dev server) and the robot gateway, so the robot has exactly the
cloud's public paths and no robot-specific code in the client.

It is the `nginx-gateway` service of `mission_dispatch_services_robot.yaml`, so `deploy_robot.sh` pulls, starts, health-checks
and rolls it back with the rest of the stack.

It listens on loopback and `ROBOT_TAILNET_IP` only (`ROBOT_GATEWAY_PORT`, default 80) and serves no UI (the desktop app
brings its own). It sends **no CORS headers** (`nginx/cors-robot.conf`): the desktop app reaches the robot through its own
same-origin relay, and a web page in an operator's browser must not be able to call the robot cross-origin (the cloud gateway's
permissive CORS, `nginx/cors-cloud.conf`, would let any page the operator opens start and stop robot processes). Services that are off on the robot (the agent)
answer 502 on their prefix; `GET /api/v1/server-info` is how the client learns they are absent.
Not yet run on arm64 hardware (the config was tested on the cloud host:
routes, CORS, preflight, WebSocket upgrade, binding only loopback + the tailnet IP).

## Operating it: what has gone wrong, and how it shows

The app's server-health panel (`/api/diagnostics/`, see `SERVER_DIAGNOSTICS.md`) runs these checks continuously. Each
failure below happened once and now has a check and a guard.

| What happened | How it shows | Guard |
|---|---|---|
| The gateway sent the token route to `localhost:8008` but the token service listens on the tailnet IP: 502, no video token | check "The gateway reaches the video token service" fails with the reason | the robot template uses `${ROBOT_TAILNET_IP}`; `deploy_robot.sh` always recreates the gateway |
| The gateway ran on an old config after `git pull` (single-file bind mounts keep the old inode; `nginx -s reload` and an unchanged `up -d` do not help) | a gateway check fails | `deploy_robot.sh` and `start-nginx.sh` recreate the gateway; never rely on a reload after a pull |
| One service recreated by hand on the tag in `.env.robot` while the rest ran the deployed tag (API without `server-info`) | check "All services run the same version"; "The gateway reaches the API" (404) | use `scripts/robot_compose.sh` (it applies `.robot_current_tag`), not bare `docker compose` |
| `SERVER_KIND` or a capability flag missing from `.env.robot`: the Pi called itself the cloud and hid scene graph and reconstruction | check "What the server advertises is running" | `.env.robot.example` lists every flag; compare with it after editing |
| A service near its memory limit | memory bar, finding "Using most of its memory limit" (80%) / "About to run out" (92%); the kernel's kill shows as "Stopped for using too much memory" | the `MEM_*` limits; the host panel shows total memory, swap and disk |

## Raw reconstruction (optional)

Only the raw backend (sensor depth, no MapAnything/torch) is meant for the Pi. See `reconstruction_service_robot.yaml`
(`RECON_MAX_VOXELS` caps the fusion memory; start it with `docker compose -p recon --env-file .env.robot -f …`) and set `RECONSTRUCTION_SERVICE_URL=http://localhost:8009`,
`RECONSTRUCTION_SERVICE_KEY` and `RECONSTRUCTION_CALLBACK_SECRET` in `.env.robot`.

## Not verified yet

Never started on arm64 hardware. To check on the first deploy: the MinIO image's arm64 build, ArangoDB's memory flags,
the API with livekit-service absent, the SFU on arm64 with a real Jetson publisher, real memory use against the limits, and the raw reconstruction speed.
