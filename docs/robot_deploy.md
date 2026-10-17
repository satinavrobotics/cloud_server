# Robot (Raspberry Pi) deploy

The same stack as the cloud, smaller, on a Raspberry Pi 4 (linux/arm64). No sync or share with the cloud. Images are built
on the cloud host for amd64 + arm64, pushed to the private registry, and the Pi only pulls and runs them.

| File | Role |
|---|---|
| `scripts/build_push.sh` | builds the 8 images (7 services + MinIO) multi-arch and pushes them to `100.85.3.47:5050/sati/…` |
| `docker_compose/mission_dispatch_services_robot.yaml` | override over the main compose file: services off, sizes, registry images |
| `docker_compose/.env.robot.example` | the robot's environment (copy to `.env.robot`, fresh secrets) |
| `scripts/deploy_robot.sh` | pull, `up -d --no-build`, wait healthy, roll back to the previous tag |
| `docker_compose/reconstruction_service_robot.yaml` | optional raw reconstruction (no MapAnything), see below |

## What runs on the robot

On: mosquitto, postgres, arangodb, minio, minio-tailnet, mission-dispatch, graph-builder, mission-planner, api.
`minio-tailnet` stays: presigned URLs are signed for `MINIO_HOST`, which is the Pi's tailnet IP.

Off (profile `cloud-only`): livekit-service (LiveKit Cloud tokens), livekit-sfu and livekit-sfu-tokens (self-hosted video),
agent-orchestrator-service (needs an LLM API). `SCENE_GRAPH_WORKER_ENABLED` / `SCENE_GRAPH_PLANNER_ENABLED` default to false.

Network: Postgres, ArangoDB, the planner and graph-builder listen on `127.0.0.1` only; mosquitto on `127.0.0.1` and the tailnet IP
(`ROBOT_TAILNET_IP`); the API (8000) and the MinIO forwarder are the doors. Mosquitto still allows anonymous clients, so
Tailscale (ACLs) is the boundary: keep the ACL tight. Every container has a memory limit and a rotated log (10 MB x 3);
mosquitto logs errors/warnings only.

Memory: the defaults are for a **4 GB** Pi (limits sum to ~3.4 GB; 8 GB values are in `.env.robot.example`). `PG_TUNE_*` apply
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
the `sati_pgdata17` volume and the MinIO directory, and writes `docker_compose/.env.robot` with fresh random passwords and
this host's tailnet IP. It never overwrites an existing `.env.robot` and is safe to run again. Never copy the cloud's
`docker_compose/.env`.

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

## Raw reconstruction (optional)

Only the raw backend (sensor depth, no MapAnything/torch) is meant for the Pi. See `reconstruction_service_robot.yaml`
(`RECON_MAX_VOXELS` caps the fusion memory; start it with `docker compose -p recon --env-file .env.robot -f …`) and set `RECONSTRUCTION_SERVICE_URL=http://localhost:8009`,
`RECONSTRUCTION_SERVICE_KEY` and `RECONSTRUCTION_CALLBACK_SECRET` in `.env.robot`.

## Not verified yet

Never started on arm64 hardware. To check on the first deploy: the MinIO image's arm64 build, ArangoDB's memory flags,
the API with livekit-service absent, real memory use against the limits, and the raw reconstruction speed.
